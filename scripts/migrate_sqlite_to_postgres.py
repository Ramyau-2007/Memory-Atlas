"""Repeatable, idempotent migration tool from SQLite to Neon PostgreSQL.

Preserves all existing data, accounts, chat history, memories, versions,
invitations, and sessions without modifying the original SQLite database.
"""

import argparse
import logging
import os
import sqlite3
import sys
from pathlib import Path

# Add project root to path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.db import (
    connect,
    get_pg_engine,
    init_postgres_db,
    is_postgres_target,
    normalize_pg_url,
    PostgresConnectionWrapper,
)
from sqlalchemy import text

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("migration")

TABLE_DEFINITIONS = [
    {
        "table": "users",
        "columns": ["id", "username", "password_hash", "created_at", "disabled_at"],
        "pk": "id",
        "update_columns": ["username", "password_hash", "disabled_at"],
        "has_sequence": True,
    },
    {
        "table": "invitations",
        "columns": ["id", "token_hash", "created_at", "expires_at", "redeemed_at", "revoked_at"],
        "pk": "id",
        "update_columns": [],  # DO NOTHING
        "has_sequence": False,
    },
    {
        "table": "sessions",
        "columns": ["token_hash", "user_id", "expires_at"],
        "pk": "token_hash",
        "update_columns": [],  # DO NOTHING
        "has_sequence": False,
    },
    {
        "table": "conversations",
        "columns": ["id", "user_id", "title", "summary", "created_at"],
        "pk": "id",
        "update_columns": ["title", "summary"],
        "has_sequence": True,
    },
    {
        "table": "messages",
        "columns": ["id", "conversation_id", "role", "content", "created_at"],
        "pk": "id",
        "update_columns": ["content"],
        "has_sequence": True,
    },
    {
        "table": "memories",
        "columns": [
            "id", "user_id", "content", "category", "priority", "source",
            "reason", "temporary", "deleted_at", "created_at", "updated_at",
            "source_message_id",
        ],
        "pk": "id",
        "update_columns": [
            "content", "category", "priority", "source", "reason", "temporary",
            "deleted_at", "updated_at", "source_message_id",
        ],
        "has_sequence": True,
    },
    {
        "table": "memory_versions",
        "columns": ["id", "memory_id", "version", "content", "category", "priority", "reason", "created_at"],
        "pk": "id",
        "update_columns": ["content", "category", "priority", "reason"],
        "has_sequence": True,
    },
    {
        "table": "temporary_context",
        "columns": ["id", "user_id", "content", "expires_at"],
        "pk": "id",
        "update_columns": ["content", "expires_at"],
        "has_sequence": True,
    },
    {
        "table": "memory_events",
        "columns": ["id", "user_id", "memory_id", "event_type", "created_at"],
        "pk": "id",
        "update_columns": [],  # DO NOTHING
        "has_sequence": True,
    },
]


def open_sqlite_readonly(path: str) -> sqlite3.Connection:
    """Open SQLite in strict read-only mode to guarantee original database cannot be modified."""
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"SQLite database file not found at: {resolved}")
    uri = resolved.as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    integrity = conn.execute("PRAGMA quick_check(1)").fetchone()
    if not integrity or integrity[0] != "ok":
        raise RuntimeError(f"SQLite database at {resolved} failed integrity check.")
    return conn


def extract_sqlite_table_data(conn: sqlite3.Connection, table_name: str, expected_cols: list[str]) -> list[dict]:
    # Check which columns actually exist in the SQLite table
    existing_cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
    cols_to_select = [c for c in expected_cols if c in existing_cols]
    if not cols_to_select:
        return []
    col_str = ", ".join(f'"{c}"' for c in cols_to_select)
    cursor = conn.execute(f"SELECT {col_str} FROM {table_name}")
    rows = []
    for r in cursor.fetchall():
        row_dict = {}
        for c in expected_cols:
            row_dict[c] = r[c] if c in existing_cols else None
        rows.append(row_dict)
    return rows


def build_upsert_query(def_dict: dict) -> str:
    table = def_dict["table"]
    cols = def_dict["columns"]
    pk = def_dict["pk"]
    update_cols = def_dict["update_columns"]

    cols_str = ", ".join(f'"{c}"' for c in cols)
    params_str = ", ".join(f":{c}" for c in cols)

    if update_cols:
        updates = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in update_cols)
        conflict_clause = f"ON CONFLICT (\"{pk}\") DO UPDATE SET {updates}"
    else:
        conflict_clause = f"ON CONFLICT (\"{pk}\") DO NOTHING"

    return f"INSERT INTO {table} ({cols_str}) VALUES ({params_str}) {conflict_clause}"


def get_pg_table_counts(engine) -> dict[str, int]:
    counts = {}
    with engine.connect() as conn:
        for tdef in TABLE_DEFINITIONS:
            table = tdef["table"]
            try:
                c = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
                counts[table] = int(c or 0)
            except Exception:
                counts[table] = 0
    return counts


def reset_sequences(engine) -> None:
    with engine.connect() as conn:
        with conn.begin():
            for tdef in TABLE_DEFINITIONS:
                if tdef["has_sequence"]:
                    table = tdef["table"]
                    pk = tdef["pk"]
                    seq_query = text(f"""
                        SELECT setval(
                            pg_get_serial_sequence('{table}', '{pk}'),
                            COALESCE((SELECT MAX({pk}) FROM {table}), 1),
                            COALESCE((SELECT COUNT(*) > 0 FROM {table}), false)
                        )
                    """)
                    try:
                        conn.execute(seq_query)
                    except Exception as e:
                        logger.warning("Could not reset sequence for %s: %s", table, e)


def run_migration(sqlite_path: str, pg_url: str, apply: bool = False) -> dict:
    sqlite_conn = open_sqlite_readonly(sqlite_path)
    engine = get_pg_engine(pg_url)

    # 1. Read SQLite record counts
    sqlite_data = {}
    sqlite_counts = {}
    for tdef in TABLE_DEFINITIONS:
        table = tdef["table"]
        rows = extract_sqlite_table_data(sqlite_conn, table, tdef["columns"])
        sqlite_data[table] = rows
        sqlite_counts[table] = len(rows)
    sqlite_conn.close()

    # 2. Check initial target counts
    initial_pg_counts = get_pg_table_counts(engine)

    logger.info("================ MIGRATION SUMMARY ================")
    logger.info("SQLite Database: %s", Path(sqlite_path).resolve())
    logger.info("Target: PostgreSQL (%s)", normalize_pg_url(pg_url).split("@")[-1])
    logger.info("Mode: %s", "APPLY (Writing changes)" if apply else "DRY-RUN (Inspection only)")
    logger.info("---------------------------------------------------")
    logger.info("%-20s %-15s %-15s", "Table", "SQLite Rows", "Current PG Rows")
    logger.info("---------------------------------------------------")
    for tdef in TABLE_DEFINITIONS:
        table = tdef["table"]
        logger.info("%-20s %-15d %-15d", table, sqlite_counts[table], initial_pg_counts.get(table, 0))
    logger.info("---------------------------------------------------")

    if not apply:
        logger.info("DRY RUN completed. No database changes were made.")
        logger.info("To perform actual migration, pass --apply flag.")
        return {
            "mode": "dry-run",
            "sqlite_counts": sqlite_counts,
            "pg_counts": initial_pg_counts,
        }

    # 3. Apply schema if missing
    logger.info("Initializing / verifying PostgreSQL schema...")
    wrapper = PostgresConnectionWrapper(engine)
    init_postgres_db(wrapper)

    # 4. Migrate each table in dependency order
    with engine.connect() as conn:
        with conn.begin():
            for tdef in TABLE_DEFINITIONS:
                table = tdef["table"]
                rows = sqlite_data[table]
                if not rows:
                    continue
                upsert_sql = text(build_upsert_query(tdef))
                logger.info("Migrating %d row(s) into '%s'...", len(rows), table)
                # Batch execute upsert
                conn.execute(upsert_sql, rows)

    # 5. Reset auto-increment sequences
    logger.info("Synchronizing PostgreSQL sequences to prevent ID clashes...")
    reset_sequences(engine)

    # 6. Verify final counts
    final_pg_counts = get_pg_table_counts(engine)
    logger.info("================ VERIFICATION ================")
    logger.info("%-20s %-15s %-15s", "Table", "SQLite Rows", "Final PG Rows")
    logger.info("---------------------------------------------------")
    for tdef in TABLE_DEFINITIONS:
        table = tdef["table"]
        s_count = sqlite_counts[table]
        p_count = final_pg_counts.get(table, 0)
        match = "✓" if p_count >= s_count else "✗"
        logger.info("%-20s %-15d %-15d %s", table, s_count, p_count, match)
    logger.info("---------------------------------------------------")
    logger.info("Migration completed successfully!")

    return {
        "mode": "applied",
        "sqlite_counts": sqlite_counts,
        "initial_pg_counts": initial_pg_counts,
        "final_pg_counts": final_pg_counts,
    }


def main():
    parser = argparse.ArgumentParser(description="Migrate Memory Atlas SQLite database to PostgreSQL.")
    parser.add_argument(
        "--sqlite-path",
        default=os.getenv("DATABASE_PATH", "data/memory.sqlite3"),
        help="Path to SQLite database file (default: data/memory.sqlite3)",
    )
    parser.add_argument(
        "--pg-url",
        default=os.getenv("DATABASE_URL", ""),
        help="Target PostgreSQL connection URL (defaults to DATABASE_URL env var)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually apply changes to PostgreSQL (without this flag, dry-run is performed)",
    )
    args = parser.parse_args()

    if not args.pg_url:
        print("ERROR: PostgreSQL connection URL must be specified via --pg-url or DATABASE_URL env var.", file=sys.stderr)
        sys.exit(1)

    if not is_postgres_target(args.pg_url):
        print(f"ERROR: Target URL '{args.pg_url}' is not a valid PostgreSQL URL.", file=sys.stderr)
        sys.exit(1)

    try:
        run_migration(args.sqlite_path, args.pg_url, apply=args.apply)
    except Exception as exc:
        logger.error("Migration failed: %s", exc, exc_info=True)
        sys.exit(2)


if __name__ == "__main__":
    main()
