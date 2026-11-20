import os
import re
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from scripts.migrate_sqlite_to_postgres import (
    TABLE_DEFINITIONS,
    build_insert_query,
    extract_sqlite_table_data,
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


class FakePostgresConnection:
    def __init__(self, tables=None, counts=None, fail_count=False, fail_insert=False):
        self.tables = set(tables or ())
        self.counts = {name: 0 for name in self.tables}
        self.counts.update(counts or {})
        self.fail_count = fail_count
        self.fail_insert = fail_insert
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
            return FakeResult(value="public.test_sequence")
        if "COALESCE(MAX(" in sql:
            return FakeResult(value=2)
        if "ALTER SEQUENCE" in sql and "format(" in sql:
            return FakeResult(value="ALTER SEQUENCE public.test_sequence RESTART WITH 2")
        match = re.match(r"INSERT INTO (\w+)", sql)
        if match:
            if self.fail_insert:
                raise RuntimeError("duplicate key value violates unique constraint")
            self.counts[match.group(1)] = self.counts.get(match.group(1), 0) + len(params)
            return FakeResult()
        raise AssertionError(f"Unexpected SQL in migration test: {sql}")

    def exec_driver_sql(self, statement):
        self.ddl.append(statement)
        match = re.match(r"CREATE TABLE IF NOT EXISTS (\w+)", statement)
        if match:
            self.tables.add(match.group(1))
            self.counts.setdefault(match.group(1), 0)
        if statement.startswith("LOCK TABLE"):
            self.locked = True
        return FakeResult()


class FakePostgresEngine:
    def __init__(self, tables=None, counts=None, fail_count=False, fail_insert=False):
        self.connection = FakePostgresConnection(tables, counts, fail_count, fail_insert)
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
            with self.assertRaisesRegex(RuntimeError, "duplicate key"):
                run_migration(self.sqlite_path, "postgresql+pg8000://user:pass@host/db", apply=True)
        self.assertEqual(engine.commits, 0)
        self.assertEqual(engine.rollbacks, 1)
        self.assertEqual(engine.connection.tables, set())
        self.assertEqual(engine.connection.counts, {})
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


if __name__ == "__main__":
    unittest.main()
