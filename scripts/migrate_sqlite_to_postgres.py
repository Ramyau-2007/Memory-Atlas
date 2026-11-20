"""Fail-fast SQLite-to-PostgreSQL migration for an empty destination.

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
    get_pg_engine,
    init_postgres_db,
    is_postgres_target,
    normalize_pg_url,
)
from sqlalchemy import text

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("migration")

TABLE_DEFINITIONS = [
    {
        "table": "users",
        "columns": ["id", "username", "password_hash", "created_at", "disabled_at"],
        "pk": "id",
        "has_sequence": True,
    },
    {
        "table": "invitations",
        "columns": ["id", "token_hash", "created_at", "expires_at", "redeemed_at", "revoked_at"],
        "pk": "id",
        "has_sequence": False,
    },
    {
        "table": "sessions",
        "columns": ["token_hash", "user_id", "expires_at"],
        "pk": "token_hash",
        "has_sequence": False,
    },
    {
        "table": "conversations",
        "columns": ["id", "user_id", "title", "summary", "created_at"],
        "pk": "id",
        "has_sequence": True,
    },
    {
        "table": "messages",
        "columns": ["id", "conversation_id", "role", "content", "created_at"],
        "pk": "id",
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
        "has_sequence": True,
    },
    {
        "table": "memory_versions",
        "columns": ["id", "memory_id", "version", "content", "category", "priority", "reason", "created_at"],
        "pk": "id",
        "has_sequence": True,
    },
    {
        "table": "temporary_context",
        "columns": ["id", "user_id", "content", "expires_at"],
        "pk": "id",
        "has_sequence": True,
    },
    {
        "table": "memory_events",
        "columns": ["id", "user_id", "memory_id", "event_type", "created_at"],
        "pk": "id",
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


def build_insert_query(def_dict: dict) -> str:
    table = def_dict["table"]
    cols = def_dict["columns"]
    cols_str = ", ".join(f'"{c}"' for c in cols)
    params_str = ", ".join(f":{c}" for c in cols)
    return f"INSERT INTO {table} ({cols_str}) VALUES ({params_str})"


def _pg_table_counts(conn) -> dict[str, int]:
    """Count application rows; missing tables mean zero, query errors propagate."""
    result = conn.execute(text("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = current_schema()
    """))
    existing_tables = {row[0] for row in result.fetchall()}
    counts = {}
    for tdef in TABLE_DEFINITIONS:
        table = tdef["table"]
        if table not in existing_tables:
            counts[table] = 0
            continue
        count = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
        if count is None:
            raise RuntimeError(f"Could not read the row count for PostgreSQL table '{table}'.")
        counts[table] = int(count)
    return counts


def get_pg_table_counts(engine) -> dict[str, int]:
    """Read target counts without modifying PostgreSQL; failures are fatal."""
    with engine.connect() as conn:
        return _pg_table_counts(conn)


def reset_sequences(conn) -> None:
    """Restart serial sequences transactionally after preserving source IDs."""
    for tdef in TABLE_DEFINITIONS:
        if not tdef["has_sequence"]:
            continue
        table, pk = tdef["table"], tdef["pk"]
        sequence = conn.execute(text(
            "SELECT pg_get_serial_sequence(:table_name, :column_name)"
        ), {"table_name": table, "column_name": pk}).scalar()
        if not sequence:
            raise RuntimeError(f"PostgreSQL serial sequence is missing for {table}.{pk}.")
        next_value = conn.execute(text(
            f"SELECT COALESCE(MAX({pk}), 0) + 1 FROM {table}"
        )).scalar()
        if next_value is None:
            raise RuntimeError(f"Could not determine the next sequence value for {table}.{pk}.")
        alter_sequence = conn.execute(text("""
            SELECT format(
                'ALTER SEQUENCE %s RESTART WITH %s',
                CAST(:sequence_name AS regclass), :next_value
            )
        """), {"sequence_name": sequence, "next_value": int(next_value)}).scalar()
        if not alter_sequence:
            raise RuntimeError(f"Could not prepare the sequence update for {table}.{pk}.")
        conn.exec_driver_sql(alter_sequence)


class _MigrationConnectionAdapter:
    """Pass raw PostgreSQL DDL through the active transaction connection."""
    def __init__(self, connection):
        self.connection = connection

    def execute(self, statement: str):
        return self.connection.exec_driver_sql(statement)


def run_migration(sqlite_path: str, pg_url: str, apply: bool = False) -> dict:
    sqlite_conn = open_sqlite_readonly(sqlite_path)
    try:
        source_tables = {row[0] for row in sqlite_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        required_source_tables = {"users", "sessions", "conversations", "messages", "memories"}
        if not required_source_tables.issubset(source_tables):
            missing = sorted(required_source_tables - source_tables)
            raise RuntimeError(f"SQLite source is missing required application tables: {missing}")
        sqlite_data = {}
        sqlite_counts = {}
        for tdef in TABLE_DEFINITIONS:
            table = tdef["table"]
            rows = extract_sqlite_table_data(sqlite_conn, table, tdef["columns"])
            sqlite_data[table] = rows
            sqlite_counts[table] = len(rows)
    finally:
        sqlite_conn.close()

    engine = get_pg_engine(pg_url)

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

    if apply and any(initial_pg_counts.values()):
        raise RuntimeError(
            "Destination PostgreSQL database contains existing Memory Atlas data; refusing migration. "
            "Use a new empty database."
        )

    if not apply:
        logger.info("DRY RUN completed. No database changes were made.")
        logger.info("To perform actual migration, pass --apply flag.")
        return {
            "mode": "dry-run",
            "sqlite_counts": sqlite_counts,
            "pg_counts": initial_pg_counts,
        }

    # DDL, application rows, sequence restarts, and verification share one
    # transaction. Table locks close the gap between the initial read-only
    # preflight and writes, including races with a concurrently starting app.
    logger.info("Initializing / verifying PostgreSQL schema transactionally...")
    with engine.begin() as conn:
        init_postgres_db(_MigrationConnectionAdapter(conn))
        table_list = ", ".join(tdef["table"] for tdef in TABLE_DEFINITIONS)
        conn.exec_driver_sql(f"LOCK TABLE {table_list} IN ACCESS EXCLUSIVE MODE")
        locked_counts = _pg_table_counts(conn)
        if any(locked_counts.values()):
            raise RuntimeError(
                "Destination PostgreSQL database contains existing Memory Atlas data; refusing migration. "
                "Use a new empty database."
            )

        for tdef in TABLE_DEFINITIONS:
            table = tdef["table"]
            rows = sqlite_data[table]
            if not rows:
                continue
            insert_sql = text(build_insert_query(tdef))
            logger.info("Migrating %d row(s) into '%s'...", len(rows), table)
            conn.execute(insert_sql, rows)

        logger.info("Synchronizing PostgreSQL sequences transactionally...")
        reset_sequences(conn)
        final_pg_counts = _pg_table_counts(conn)
        if final_pg_counts != sqlite_counts:
            raise RuntimeError(
                "Migration row-count verification failed; rolling back. "
                f"SQLite counts: {sqlite_counts}; PostgreSQL counts: {final_pg_counts}"
            )
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
