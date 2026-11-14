import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.migrate_sqlite_to_postgres import (
    TABLE_DEFINITIONS,
    build_upsert_query,
    extract_sqlite_table_data,
    open_sqlite_readonly,
    run_migration,
)
from app.db import init_db, connect


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

    def test_build_upsert_query(self):
        user_def = next(d for d in TABLE_DEFINITIONS if d["table"] == "users")
        query = build_upsert_query(user_def)
        self.assertIn('INSERT INTO users', query)
        self.assertIn('ON CONFLICT ("id") DO UPDATE SET', query)
        self.assertIn('"username" = EXCLUDED."username"', query)

        inv_def = next(d for d in TABLE_DEFINITIONS if d["table"] == "invitations")
        inv_query = build_upsert_query(inv_def)
        self.assertIn('ON CONFLICT ("id") DO NOTHING', inv_query)

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
        with patch("scripts.migrate_sqlite_to_postgres.get_pg_engine") as mock_engine, \
             patch("scripts.migrate_sqlite_to_postgres.get_pg_table_counts", return_value={"users": 0}):
            result = run_migration(
                self.sqlite_path, "postgresql+pg8000://user:pass@host/db", apply=False
            )
            self.assertEqual(result["mode"], "dry-run")
            self.assertEqual(result["sqlite_counts"]["users"], 1)
            self.assertEqual(result["sqlite_counts"]["conversations"], 1)
            self.assertEqual(result["sqlite_counts"]["messages"], 1)
            self.assertEqual(result["sqlite_counts"]["memories"], 1)
            self.assertEqual(result["sqlite_counts"]["memory_versions"], 1)
            mock_engine.return_value.connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
