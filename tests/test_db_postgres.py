import unittest
from types import SimpleNamespace
from unittest.mock import Mock, MagicMock

from app.db import (
    normalize_pg_url,
    translate_query_for_postgres,
    DbRow,
    CursorResultWrapper,
    is_postgres_target,
    PostgresConnectionWrapper,
)


class PostgresDbUnitTests(unittest.TestCase):
    def test_normalize_pg_url(self):
        self.assertEqual(
            normalize_pg_url("postgres://user:pass@host/db"),
            "postgresql+pg8000://user:pass@host/db",
        )
        self.assertEqual(
            normalize_pg_url("postgresql://user:pass@host/db"),
            "postgresql+pg8000://user:pass@host/db",
        )
        self.assertEqual(
            normalize_pg_url("postgresql+pg8000://user:pass@host/db"),
            "postgresql+pg8000://user:pass@host/db",
        )

    def test_is_postgres_target(self):
        self.assertTrue(is_postgres_target("postgres://user:pass@host/db"))
        self.assertTrue(is_postgres_target("postgresql://user:pass@host/db"))
        self.assertTrue(is_postgres_target("postgresql+pg8000://user:pass@host/db"))
        self.assertFalse(is_postgres_target("data/memory.sqlite3"))
        self.assertFalse(is_postgres_target(":memory:"))
        self.assertFalse(is_postgres_target(""))
        self.assertFalse(is_postgres_target(None))

    def test_translate_query_for_postgres(self):
        # 1. BEGIN IMMEDIATE -> BEGIN
        sql, params = translate_query_for_postgres("BEGIN IMMEDIATE", None)
        self.assertEqual(sql, "BEGIN")

        # 2. updated_at=CURRENT_TIMESTAMP -> updated_at=CURRENT_TIMESTAMP::text
        sql, params = translate_query_for_postgres(
            "UPDATE memories SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (1,)
        )
        self.assertIn("updated_at=CURRENT_TIMESTAMP::text", sql)
        self.assertEqual(params, {"p1": 1})

        # 3. LIKE -> ILIKE
        sql, params = translate_query_for_postgres(
            "SELECT * FROM memories WHERE content LIKE ? OR category LIKE ?",
            ("%test%", "%cat%"),
        )
        self.assertIn("ILIKE :p1", sql)
        self.assertIn("ILIKE :p2", sql)
        self.assertEqual(params, {"p1": "%test%", "p2": "%cat%"})

        # 4. INSERT into serial tables appends RETURNING id
        sql, params = translate_query_for_postgres(
            "INSERT INTO users(username, password_hash) VALUES(?, ?)", ("alice", "hash")
        )
        self.assertTrue(sql.endswith("RETURNING id"))
        self.assertEqual(params, {"p1": "alice", "p2": "hash"})

        # 5. INSERT into non-serial table (sessions) does NOT append RETURNING id
        sql, params = translate_query_for_postgres(
            "INSERT INTO sessions VALUES(?, ?, ?)", ("tok", 1, "exp")
        )
        self.assertFalse(sql.endswith("RETURNING id"))
        self.assertEqual(params, {"p1": "tok", "p2": 1, "p3": "exp"})

    def test_db_row_mapping_and_indexing(self):
        raw_row = SimpleNamespace(
            _mapping={"id": 10, "username": "alice", "active": True},
            __getitem__=lambda s, idx: [10, "alice", True][idx],
        )
        row = DbRow(raw_row)
        self.assertEqual(row["id"], 10)
        self.assertEqual(row["username"], "alice")
        self.assertEqual(row[0], 10)
        self.assertEqual(row[1], "alice")
        self.assertEqual(dict(row), {"id": 10, "username": "alice", "active": True})
        self.assertIn("username", row)
        self.assertEqual(len(row), 3)
        self.assertEqual(row.get("missing", "default"), "default")


if __name__ == "__main__":
    unittest.main()
