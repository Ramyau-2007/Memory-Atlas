"""Fail-fast SQLite-to-PostgreSQL migration for an empty destination.

Preserves all existing data, accounts, chat history, memories, versions,
invitations, and sessions without modifying the original SQLite database.
"""

import argparse
import logging
import os
import re
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

# Add project root to path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.db import (
    get_pg_engine,
    init_postgres_db,
    is_postgres_target,
)
from sqlalchemy import text
from sqlalchemy.exc import (
    DBAPIError,
    IntegrityError,
    InterfaceError,
    OperationalError,
    ProgrammingError,
    SQLAlchemyError,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("migration")

_PHASE_MESSAGES = {
    "SQLite source preparation": "SQLite source preparation failed; details redacted.",
    "PostgreSQL connection / initial count check": "PostgreSQL connection or initial count check failed; connection details redacted.",
    "PostgreSQL transaction acquisition": "PostgreSQL transaction acquisition failed; connection details redacted.",
    "Schema initialization": "PostgreSQL schema initialization failed; details redacted.",
    "Table locking and destination recheck": "PostgreSQL table locking or destination recheck failed; details redacted.",
    "Data insertion": "PostgreSQL data insertion failed; details redacted.",
    "Sequence reset": "PostgreSQL sequence reset failed; details redacted.",
    "Final row-count verification": "Final PostgreSQL row-count verification failed; details redacted.",
    "PostgreSQL transaction commit": "PostgreSQL transaction commit failed; details redacted.",
    "Post-commit reporting": "PostgreSQL transaction committed, but success reporting failed; inspect the destination before retrying.",
}
_SEQUENCE_SUBPHASE_MESSAGES = {
    "Sequence lookup using pg_get_serial_sequence": "PostgreSQL serial sequence lookup failed; details redacted.",
    "Maximum primary-key ID query": "PostgreSQL maximum primary-key ID query failed; details redacted.",
    "ALTER SEQUENCE statement generation": "PostgreSQL ALTER SEQUENCE statement generation failed; details redacted.",
    "ALTER SEQUENCE execution": "PostgreSQL ALTER SEQUENCE execution failed; details redacted.",
}
_SAFE_EXCEPTION_TYPES = {
    AssertionError: "AssertionError",
    DBAPIError: "DBAPIError",
    IntegrityError: "IntegrityError",
    InterfaceError: "InterfaceError",
    KeyError: "KeyError",
    OperationalError: "OperationalError",
    OSError: "OSError",
    ProgrammingError: "ProgrammingError",
    RuntimeError: "RuntimeError",
    SQLAlchemyError: "SQLAlchemyError",
    sqlite3.Error: "SQLiteError",
    TypeError: "TypeError",
    ValueError: "ValueError",
}


def _safe_exception_type_name(exc: BaseException) -> str:
    """Return a fixed class label; never expose dynamically chosen type names."""
    return _SAFE_EXCEPTION_TYPES.get(type(exc), "UnexpectedError")


@contextmanager
def _migration_phase(phase: str):
    """Attach a safe phase label to a failing exception without changing it."""
    try:
        yield
    except Exception as exc:
        if not hasattr(exc, "migration_phase"):
            try:
                exc.migration_phase = phase
            except Exception:
                pass
        raise


@contextmanager
def _migration_subphase(subphase: str):
    """Attach a validated sequence-reset subphase without replacing the outer phase."""
    try:
        yield
    except Exception as exc:
        if not hasattr(exc, "migration_subphase"):
            try:
                exc.migration_subphase = subphase
            except Exception:
                pass
        raise


@contextmanager
def _migration_transaction(engine):
    """Keep one engine transaction while tagging acquisition and commit failures separately."""
    with _migration_phase("PostgreSQL transaction acquisition"):
        transaction_context = engine.begin()
        connection = transaction_context.__enter__()

    try:
        yield connection
    except BaseException:
        operation_exc_info = sys.exc_info()
        try:
            transaction_context.__exit__(*operation_exc_info)
        except BaseException as rollback_exc:
            # Keep the operation failure primary. Never format the rollback exception.
            try:
                logger.error(
                    "Rollback failed; exception_type=%s; diagnostic=Rollback failure details redacted.",
                    _safe_exception_type_name(rollback_exc),
                )
            except Exception:
                pass
        raise
    else:
        with _migration_phase("PostgreSQL transaction commit"):
            transaction_context.__exit__(None, None, None)


def format_safe_failure(exc: Exception, phase: str | None = None) -> str:
    """Format an allowlisted diagnostic; never include exception text or traceback."""
    phase = phase or getattr(exc, "migration_phase", "SQLite source preparation")
    if phase not in _PHASE_MESSAGES:
        phase = "SQLite source preparation"

    sqlstate = None
    driver_error = getattr(exc, "orig", exc)
    for candidate in (getattr(driver_error, "sqlstate", None), getattr(driver_error, "pgcode", None),
                      getattr(exc, "sqlstate", None), getattr(exc, "pgcode", None)):
        if isinstance(candidate, str) and re.fullmatch(r"[0-9A-Z]{5}", candidate):
            sqlstate = candidate
            break

    subphase = getattr(exc, "migration_subphase", None)
    if subphase not in _SEQUENCE_SUBPHASE_MESSAGES or phase != "Sequence reset":
        subphase = None
    diagnostic = _SEQUENCE_SUBPHASE_MESSAGES[subphase] if subphase else _PHASE_MESSAGES[phase]
    subphase_field = f"; subphase={subphase}" if subphase else ""
    return (
        f"Migration failed phase={phase}{subphase_field}; exception_type={_safe_exception_type_name(exc)}; "
        f"sqlstate={sqlstate or 'unavailable'}; diagnostic={diagnostic}"
    )

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
        with _migration_subphase("Sequence lookup using pg_get_serial_sequence"):
            sequence = conn.execute(text(
                "SELECT pg_get_serial_sequence(:table_name, :column_name)"
            ), {"table_name": table, "column_name": pk}).scalar()
            if not sequence:
                raise RuntimeError(f"PostgreSQL serial sequence is missing for {table}.{pk}.")

        with _migration_subphase("Maximum primary-key ID query"):
            next_value = conn.execute(text(
                f"SELECT COALESCE(MAX({pk}), 0) + 1 FROM {table}"
            )).scalar()
            if next_value is None:
                raise RuntimeError(f"Could not determine the next sequence value for {table}.{pk}.")

        with _migration_subphase("ALTER SEQUENCE statement generation"):
            alter_sequence = conn.execute(text("""
                SELECT format(
                    'ALTER SEQUENCE %s RESTART WITH %s',
                    CAST(:sequence_name AS regclass), :next_value
                )
            """), {"sequence_name": sequence, "next_value": int(next_value)}).scalar()
            if not alter_sequence:
                raise RuntimeError(f"Could not prepare the sequence update for {table}.{pk}.")

        with _migration_subphase("ALTER SEQUENCE execution"):
            conn.exec_driver_sql(alter_sequence)


class _MigrationConnectionAdapter:
    """Pass raw PostgreSQL DDL through the active transaction connection."""
    def __init__(self, connection):
        self.connection = connection

    def execute(self, statement: str):
        return self.connection.exec_driver_sql(statement)


def run_migration(sqlite_path: str, pg_url: str, apply: bool = False) -> dict:
    with _migration_phase("SQLite source preparation"):
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

    with _migration_phase("PostgreSQL connection / initial count check"):
        engine = get_pg_engine(pg_url)
        initial_pg_counts = get_pg_table_counts(engine)

    logger.info("================ MIGRATION SUMMARY ================")
    logger.info("Source: local SQLite database")
    logger.info("Target: external PostgreSQL")
    logger.info("Mode: %s", "APPLY (Writing changes)" if apply else "DRY-RUN (Inspection only)")
    logger.info("---------------------------------------------------")
    logger.info("%-20s %-15s %-15s", "Table", "SQLite Rows", "Current PG Rows")
    logger.info("---------------------------------------------------")
    for tdef in TABLE_DEFINITIONS:
        table = tdef["table"]
        logger.info("%-20s %-15d %-15d", table, sqlite_counts[table], initial_pg_counts.get(table, 0))
    logger.info("---------------------------------------------------")

    with _migration_phase("PostgreSQL connection / initial count check"):
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
    with _migration_transaction(engine) as conn:
        with _migration_phase("Schema initialization"):
            init_postgres_db(_MigrationConnectionAdapter(conn))

        with _migration_phase("Table locking and destination recheck"):
            table_list = ", ".join(tdef["table"] for tdef in TABLE_DEFINITIONS)
            conn.exec_driver_sql(f"LOCK TABLE {table_list} IN ACCESS EXCLUSIVE MODE")
            locked_counts = _pg_table_counts(conn)
            if any(locked_counts.values()):
                raise RuntimeError(
                    "Destination PostgreSQL database contains existing Memory Atlas data; refusing migration. "
                    "Use a new empty database."
                )

        with _migration_phase("Data insertion"):
            for tdef in TABLE_DEFINITIONS:
                table = tdef["table"]
                rows = sqlite_data[table]
                if not rows:
                    continue
                insert_sql = text(build_insert_query(tdef))
                logger.info("Migrating %d row(s) into '%s'...", len(rows), table)
                conn.execute(insert_sql, rows)

        with _migration_phase("Sequence reset"):
            logger.info("Synchronizing PostgreSQL sequences transactionally...")
            reset_sequences(conn)

        with _migration_phase("Final row-count verification"):
            final_pg_counts = _pg_table_counts(conn)
            if final_pg_counts != sqlite_counts:
                raise RuntimeError(
                    "Migration row-count verification failed; rolling back. "
                    f"SQLite counts: {sqlite_counts}; PostgreSQL counts: {final_pg_counts}"
                )
    with _migration_phase("Post-commit reporting"):
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
        print("ERROR: PostgreSQL connection URL is not configured.", file=sys.stderr)
        sys.exit(1)

    if not is_postgres_target(args.pg_url):
        print("ERROR: Target must be a PostgreSQL URL.", file=sys.stderr)
        sys.exit(1)

    try:
        run_migration(args.sqlite_path, args.pg_url, apply=args.apply)
    except Exception as exc:
        logger.error(format_safe_failure(exc))
        sys.exit(2)


if __name__ == "__main__":
    main()
