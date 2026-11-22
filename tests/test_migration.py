import os
import re
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from sqlalchemy.exc import ProgrammingError

from scripts.migrate_sqlite_to_postgres import (
    TABLE_DEFINITIONS,
    _migration_phase,
    _migration_transaction,
    build_insert_query,
    extract_sqlite_table_data,
    format_safe_failure,
    get_pg_table_counts,
    open_sqlite_readonly,
    run_migration,
)
from app.db import init_db, connect


class FakeResult:
    def __init__(self, rows=None, value=None):
        self.rows = rows or []
        self.value = value

    def fetchall(self):
        return self.rows

    def scalar(self):
        return self.value


class FakeProgrammingDriverError(Exception):
    sqlstate = "42601"


class FakePostgresConnection:
    def __init__(self, tables=None, counts=None, fail_count=False, fail_insert=False, fail_sequence_subphase=None):
        self.tables = set(tables or ())
        self.counts = {name: 0 for name in self.tables}
        self.counts.update(counts or {})
        self.fail_count = fail_count
        self.fail_insert = fail_insert
        self.fail_sequence_subphase = fail_sequence_subphase
        self.ddl = []
        self.locked = False

    def execute(self, statement, params=None):
        sql = str(statement)
        if "information_schema.tables" in sql:
            return FakeResult([(table,) for table in sorted(self.tables)])
        match = re.match(r"SELECT COUNT\(\*\) FROM (\w+)", sql)
        if match:
            if self.fail_count:
                raise RuntimeError("simulated PostgreSQL count query failure")
            return FakeResult(value=self.counts.get(match.group(1), 0))
        if "pg_get_serial_sequence" in sql:
            if self.fail_sequence_subphase == "Sequence lookup using pg_get_serial_sequence":
                self._raise_sequence_programming_error()
            return FakeResult(value=f"public.{params['table_name']}_{params['column_name']}_seq")
        if "COALESCE(MAX(" in sql:
            if self.fail_sequence_subphase == "Maximum primary-key ID query":
                self._raise_sequence_programming_error()
            return FakeResult(value=2)
        if "ALTER SEQUENCE" in sql and "format(" in sql:
            if self.fail_sequence_subphase == "ALTER SEQUENCE statement generation":
                self._raise_sequence_programming_error()
            return FakeResult(value=f"ALTER SEQUENCE {params['sequence_name']} RESTART WITH {params['next_value']}")
        match = re.match(r"INSERT INTO (\w+)", sql)
        if match:
            if self.fail_insert:
                raise RuntimeError("duplicate key value violates unique constraint")
            self.counts[match.group(1)] = self.counts.get(match.group(1), 0) + len(params)
            return FakeResult()
        raise AssertionError(f"Unexpected SQL in migration test: {sql}")

    @staticmethod
    def _raise_sequence_programming_error():
        raise ProgrammingError(
            "synthetic sequence SQL failure",
            {},
            FakeProgrammingDriverError("synthetic fake SQL detail"),
        )

    def exec_driver_sql(self, statement):
        self.ddl.append(statement)
        if statement.startswith("ALTER SEQUENCE") and self.fail_sequence_subphase == "ALTER SEQUENCE execution":
            self._raise_sequence_programming_error()
        match = re.match(r"CREATE TABLE IF NOT EXISTS (\w+)", statement)
        if match:
            self.tables.add(match.group(1))
            self.counts.setdefault(match.group(1), 0)
        if statement.startswith("LOCK TABLE"):
            self.locked = True
        return FakeResult()


class FakePostgresEngine:
    def __init__(self, tables=None, counts=None, fail_count=False, fail_insert=False, fail_sequence_subphase=None):
        self.connection = FakePostgresConnection(
            tables, counts, fail_count, fail_insert, fail_sequence_subphase
        )
        self.begin_calls = 0
        self.commits = 0
        self.rollbacks = 0

    @contextmanager
    def connect(self):
        yield self.connection

    @contextmanager
    def begin(self):
        self.begin_calls += 1
        tables = set(self.connection.tables)
        counts = dict(self.connection.counts)
        ddl_length = len(self.connection.ddl)
        try:
            yield self.connection
        except Exception:
            self.connection.tables = tables
            self.connection.counts = counts
            del self.connection.ddl[ddl_length:]
            self.rollbacks += 1
            raise
        else:
            self.commits += 1


class FakeDriverError(Exception):
    sqlstate = "08006"


class FakeSqlAlchemyError(Exception):
    def __init__(self):
        self.orig = FakeDriverError(
            "postgresql://fake_user:fake_password@fake-host.test/neondb?sslmode=require&token=fake_token"
        )
        super().__init__(
            "connection failed for postgresql://fake_user:fake_password@fake-host.test/neondb"
            "?sslmode=require&token=fake_token"
        )


class FakeTransactionContext:
    def __init__(self, fail_acquisition=False, fail_commit=False, fail_rollback=False):
        self.fail_acquisition = fail_acquisition
        self.fail_commit = fail_commit
        self.fail_rollback = fail_rollback
        self.rollback_calls = 0
        self.commit_calls = 0

    def __enter__(self):
        if self.fail_acquisition:
            raise RuntimeError("fake acquisition failure with password=fake_password")
        return object()

    def __exit__(self, exc_type, exc, traceback):
        if exc_type is not None:
            self.rollback_calls += 1
            if self.fail_rollback:
                raise RuntimeError("rollback failed for fake_user:fake_password@fake-host.test")
            return False
        self.commit_calls += 1
        if self.fail_commit:
            raise RuntimeError("fake commit failure for fake-host.test")
        return False


class FakeTransactionEngine:
    def __init__(self, transaction_context):
        self.transaction_context = transaction_context

    def begin(self):
        return self.transaction_context


class MigrationDiagnosticTests(unittest.TestCase):
    def test_phase_context_assigns_all_migration_operation_phases(self):
        phases = (
            "PostgreSQL connection / initial count check",
            "Schema initialization",
            "Table locking and destination recheck",
            "Data insertion",
            "Sequence reset",
            "Final row-count verification",
            "Post-commit reporting",
        )
        for phase in phases:
            with self.subTest(phase=phase):
                with self.assertRaisesRegex(RuntimeError, "synthetic failure") as raised:
                    with _migration_phase(phase):
                        raise RuntimeError("synthetic failure with fake_password")
                diagnostic = format_safe_failure(raised.exception)
                self.assertIn(f"phase={phase}", diagnostic)
                self.assertNotIn("fake_password", diagnostic)

    def test_transaction_acquisition_failure_gets_its_own_phase(self):
        engine = FakeTransactionEngine(FakeTransactionContext(fail_acquisition=True))

        with self.assertRaisesRegex(RuntimeError, "fake acquisition failure") as raised:
            with _migration_transaction(engine):
                self.fail("transaction body must not run")

        diagnostic = format_safe_failure(raised.exception)
        self.assertIn("phase=PostgreSQL transaction acquisition", diagnostic)
        self.assertNotIn("fake_password", diagnostic)

    def test_transaction_commit_failure_gets_its_own_phase(self):
        transaction = FakeTransactionContext(fail_commit=True)
        engine = FakeTransactionEngine(transaction)

        with self.assertRaisesRegex(RuntimeError, "fake commit failure") as raised:
            with _migration_transaction(engine):
                pass

        diagnostic = format_safe_failure(raised.exception)
        self.assertIn("phase=PostgreSQL transaction commit", diagnostic)
        self.assertNotIn("fake-host.test", diagnostic)
        self.assertEqual(transaction.commit_calls, 1)

    def test_transaction_body_failure_rolls_back_and_keeps_operation_phase(self):
        transaction = FakeTransactionContext()
        engine = FakeTransactionEngine(transaction)

        with self.assertRaisesRegex(RuntimeError, "synthetic insert failure") as raised:
            with _migration_transaction(engine):
                with _migration_phase("Data insertion"):
                    raise RuntimeError("synthetic insert failure")

        self.assertEqual(transaction.rollback_calls, 1)
        self.assertEqual(transaction.commit_calls, 0)
        self.assertIn("phase=Data insertion", format_safe_failure(raised.exception))

    def test_rollback_failure_keeps_original_operation_failure_primary(self):
        transaction = FakeTransactionContext(fail_rollback=True)
        engine = FakeTransactionEngine(transaction)
        operation_error = RuntimeError("operation failed with operation-secret")

        with self.assertLogs("migration", level="ERROR") as captured_logs:
            with self.assertRaises(RuntimeError) as raised:
                with _migration_transaction(engine):
                    with _migration_phase("Data insertion"):
                        raise operation_error

        self.assertIs(raised.exception, operation_error)
        self.assertEqual(transaction.rollback_calls, 1)
        self.assertEqual(transaction.commit_calls, 0)
        primary_diagnostic = format_safe_failure(raised.exception)
        self.assertIn("phase=Data insertion", primary_diagnostic)
        self.assertIn("exception_type=RuntimeError", primary_diagnostic)
        self.assertNotIn("operation-secret", primary_diagnostic)

        rollback_diagnostic = " ".join(captured_logs.output)
        self.assertIn("Rollback failed", rollback_diagnostic)
        self.assertIn("exception_type=RuntimeError", rollback_diagnostic)
        self.assertIn("Rollback failure details redacted.", rollback_diagnostic)
        for sensitive in ("fake_user", "fake_password", "fake-host.test", "operation-secret"):
            self.assertNotIn(sensitive, rollback_diagnostic)


class MigrationUnitTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.sqlite_path = str(Path(self.temp_dir.name) / "source.sqlite3")
        init_db(self.sqlite_path)
        with connect(self.sqlite_path) as db:
            db.execute(
                "INSERT INTO users (id, username, password_hash) VALUES (1, 'alice', 'hash1')"
            )
            db.execute(
                "INSERT INTO conversations (id, user_id, title) VALUES (10, 1, 'Test Chat')"
            )
            db.execute(
                "INSERT INTO messages (id, conversation_id, role, content) VALUES (100, 10, 'user', 'Hello')"
            )
            db.execute(
                "INSERT INTO memories (id, user_id, content, category, priority, source, reason) VALUES (50, 1, 'Likes Python', 'preference', 3, 'conv', 'stated')"
            )
            db.execute(
                "INSERT INTO memory_versions (id, memory_id, version, content, category, priority, reason) VALUES (501, 50, 1, 'Likes Python', 'preference', 3, 'stated')"
            )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_safe_failure_diagnostic_redacts_fake_connection_details(self):
        diagnostic = format_safe_failure(FakeSqlAlchemyError(), "Schema initialization")

        self.assertIn("exception_type=UnexpectedError", diagnostic)
        self.assertIn("sqlstate=08006", diagnostic)
        self.assertIn("phase=Schema initialization", diagnostic)
        self.assertIn("PostgreSQL schema initialization failed; details redacted.", diagnostic)
        for secret_fragment in (
            "fake_user", "fake_password", "fake-host.test", "neondb",
            "sslmode", "token", "fake_token", "postgresql://",
        ):
            self.assertNotIn(secret_fragment, diagnostic)

    def test_safe_failure_diagnostic_omits_invalid_sqlstate(self):
        error = RuntimeError("password=fake_password; host=fake-host.test")
        error.sqlstate = "not-a-sqlstate"

        diagnostic = format_safe_failure(error, "Data insertion")

        self.assertIn("exception_type=RuntimeError", diagnostic)
        self.assertIn("sqlstate=unavailable", diagnostic)
        self.assertIn("phase=Data insertion", diagnostic)
        self.assertNotIn("fake_password", diagnostic)
        self.assertNotIn("fake-host.test", diagnostic)

    def test_dynamic_exception_class_name_is_not_reported(self):
        sensitive_class_name = "credential_fake_password_host_fake-host"
        dynamic_error_type = type(sensitive_class_name, (Exception,), {})
        error = dynamic_error_type("password=fake_password host=fake-host.test")

        diagnostic = format_safe_failure(error, "Data insertion")

        self.assertIn("exception_type=UnexpectedError", diagnostic)
        self.assertNotIn(sensitive_class_name, diagnostic)
        self.assertNotIn("fake_password", diagnostic)
        self.assertNotIn("fake-host.test", diagnostic)

    def test_insert_query_never_uses_conflict_updates_or_ignores(self):
        user_def = next(d for d in TABLE_DEFINITIONS if d["table"] == "users")
        query = build_insert_query(user_def)
        self.assertIn('INSERT INTO users', query)
        self.assertNotIn("ON CONFLICT", query)

        inv_def = next(d for d in TABLE_DEFINITIONS if d["table"] == "invitations")
        self.assertNotIn("ON CONFLICT", build_insert_query(inv_def))

    def test_open_sqlite_readonly(self):
        conn = open_sqlite_readonly(self.sqlite_path)
        cursor = conn.cursor()
        count = cursor.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        self.assertEqual(count, 1)

        # Confirm read-only mode rejects writes
        with self.assertRaises(sqlite3.OperationalError):
            cursor.execute("INSERT INTO users (username, password_hash) VALUES ('hacker', 'bad')")
        conn.close()

    def test_extract_sqlite_table_data(self):
        conn = open_sqlite_readonly(self.sqlite_path)
        users = extract_sqlite_table_data(
            conn, "users", ["id", "username", "password_hash", "created_at", "disabled_at"]
        )
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]["username"], "alice")
        self.assertEqual(users[0]["id"], 1)
        conn.close()

    def test_dry_run_does_not_modify_anything(self):
        engine = FakePostgresEngine()
        with patch("scripts.migrate_sqlite_to_postgres.get_pg_engine", return_value=engine):
            result = run_migration(
                self.sqlite_path, "postgresql+pg8000://user:pass@host/db", apply=False
            )
            self.assertEqual(result["mode"], "dry-run")
            self.assertEqual(result["sqlite_counts"]["users"], 1)
            self.assertEqual(result["sqlite_counts"]["conversations"], 1)
            self.assertEqual(result["sqlite_counts"]["messages"], 1)
            self.assertEqual(result["sqlite_counts"]["memories"], 1)
            self.assertEqual(result["sqlite_counts"]["memory_versions"], 1)
        self.assertEqual(engine.begin_calls, 0)
        self.assertEqual(engine.connection.ddl, [])
        with connect(self.sqlite_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)

    def test_apply_refuses_nonempty_destination_before_writing(self):
        engine = FakePostgresEngine(tables={"users"}, counts={"users": 1})
        with patch("scripts.migrate_sqlite_to_postgres.get_pg_engine", return_value=engine):
            with self.assertRaisesRegex(RuntimeError, "contains existing Memory Atlas data"):
                run_migration(self.sqlite_path, "postgresql+pg8000://user:pass@host/db", apply=True)
        self.assertEqual(engine.begin_calls, 0)
        self.assertEqual(engine.connection.counts["users"], 1)
        self.assertEqual(engine.connection.ddl, [])

    def test_target_count_query_errors_fail_closed(self):
        engine = FakePostgresEngine(tables={"users"}, fail_count=True)
        with self.assertRaisesRegex(RuntimeError, "count query failure"):
            get_pg_table_counts(engine)

    def test_duplicate_id_insert_failure_rolls_back_schema_and_rows(self):
        engine = FakePostgresEngine(fail_insert=True)
        with patch("scripts.migrate_sqlite_to_postgres.get_pg_engine", return_value=engine):
            with self.assertRaisesRegex(RuntimeError, "duplicate key") as raised:
                run_migration(self.sqlite_path, "postgresql+pg8000://user:pass@host/db", apply=True)
        self.assertEqual(engine.commits, 0)
        self.assertEqual(engine.rollbacks, 1)
        self.assertEqual(raised.exception.migration_phase, "Data insertion")
        self.assertEqual(engine.connection.tables, set())
        self.assertEqual(engine.connection.counts, {})
        self.assertEqual(engine.connection.ddl, [])

    def test_post_commit_reporting_failure_is_not_reported_as_rollback(self):
        engine = FakePostgresEngine()

        def fail_post_commit_report(message, *args):
            if message == "================ VERIFICATION ================":
                raise RuntimeError("synthetic reporting failure with fake detail")

        with patch("scripts.migrate_sqlite_to_postgres.get_pg_engine", return_value=engine), \
             patch("scripts.migrate_sqlite_to_postgres.logger.info", side_effect=fail_post_commit_report):
            with self.assertRaisesRegex(RuntimeError, "synthetic reporting failure") as raised:
                run_migration(
                    self.sqlite_path, "postgresql+pg8000://user:pass@host/db", apply=True
                )

        self.assertEqual(engine.commits, 1)
        self.assertEqual(engine.rollbacks, 0)
        self.assertEqual(raised.exception.migration_phase, "Post-commit reporting")
        diagnostic = format_safe_failure(raised.exception)
        self.assertIn("phase=Post-commit reporting", diagnostic)
        self.assertIn("transaction committed", diagnostic)
        self.assertNotIn("synthetic reporting failure", diagnostic)

    def test_sequence_reset_programming_errors_report_each_subphase_and_roll_back(self):
        subphases = (
            "Sequence lookup using pg_get_serial_sequence",
            "Maximum primary-key ID query",
            "ALTER SEQUENCE statement generation",
            "ALTER SEQUENCE execution",
        )
        for subphase in subphases:
            with self.subTest(subphase=subphase):
                engine = FakePostgresEngine(fail_sequence_subphase=subphase)
                with patch("scripts.migrate_sqlite_to_postgres.get_pg_engine", return_value=engine):
                    with self.assertRaises(ProgrammingError) as raised:
                        run_migration(
                            self.sqlite_path,
                            "postgresql+pg8000://user:pass@host/db",
                            apply=True,
                        )

                self.assertEqual(raised.exception.migration_phase, "Sequence reset")
                self.assertEqual(raised.exception.migration_subphase, subphase)
                diagnostic = format_safe_failure(raised.exception)
                self.assertIn("phase=Sequence reset", diagnostic)
                self.assertIn(f"subphase={subphase}", diagnostic)
                self.assertIn("exception_type=ProgrammingError", diagnostic)
                self.assertIn("sqlstate=42601", diagnostic)
                self.assertNotIn("synthetic fake SQL detail", diagnostic)
                self.assertEqual(engine.commits, 0)
                self.assertEqual(engine.rollbacks, 1)
                self.assertEqual(engine.connection.tables, set())
                self.assertEqual(engine.connection.ddl, [])

    def test_successful_migration_commits_verified_rows(self):
        engine = FakePostgresEngine()
        with patch("scripts.migrate_sqlite_to_postgres.get_pg_engine", return_value=engine):
            result = run_migration(self.sqlite_path, "postgresql+pg8000://user:pass@host/db", apply=True)
        self.assertEqual(result["mode"], "applied")
        self.assertEqual(engine.commits, 1)
        self.assertEqual(engine.rollbacks, 0)
        self.assertTrue(engine.connection.locked)
        self.assertEqual(engine.connection.counts, result["sqlite_counts"])
        sequence_resets = [
            statement for statement in engine.connection.ddl
            if statement.startswith("ALTER SEQUENCE")
        ]
        self.assertEqual(len(sequence_resets), 7)
        self.assertTrue(all(statement.endswith("RESTART WITH 2") for statement in sequence_resets))


if __name__ == "__main__":
    unittest.main()
