import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, MagicMock
from unittest.mock import patch

from app.db import (
    normalize_pg_url,
    translate_query_for_postgres,
    DbRow,
    CursorResultWrapper,
    is_postgres_target,
    POSTGRES_REQUIRED_COLUMNS,
    PostgresConnectionWrapper,
    validate_existing_database,
)


class PostgresDbUnitTests(unittest.TestCase):
    def _validation_engine(self, schema_rows, user_count=1, query_error=None):
        conn = Mock()
        if query_error is not None:
            conn.execute.side_effect = query_error
        else:
            schema_result = Mock()
            schema_result.fetchall.return_value = schema_rows
            users_result = Mock()
            users_result.scalar.return_value = user_count
            conn.execute.side_effect = [schema_result, users_result]
        connection_context = MagicMock()
        connection_context.__enter__.return_value = conn
        engine = Mock()
        engine.connect.return_value = connection_context
        return engine

    def test_production_postgres_validation_requires_complete_schema(self):
        rows = [(table, column) for table, columns in POSTGRES_REQUIRED_COLUMNS.items()
                for column in columns]
        engine = self._validation_engine(rows, user_count=3)
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://user:pass@host/db"}), \
             patch("app.db.get_pg_engine", return_value=engine):
            target, metric = validate_existing_database()
        self.assertEqual((target, metric), ("postgresql", 3))

    def test_production_postgres_validation_refuses_incomplete_schema(self):
        rows = [(table, column) for table, columns in POSTGRES_REQUIRED_COLUMNS.items()
                for column in columns]
        rows = [(table, column) for table, column in rows
                if not (table == "memory_versions" and column == "reason")]
        engine = self._validation_engine(rows)
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://user:pass@host/db"}), \
             patch("app.db.get_pg_engine", return_value=engine):
            with self.assertRaisesRegex(RuntimeError, "schema is incomplete"):
                validate_existing_database()
        self.assertEqual(engine.connect.return_value.__enter__.return_value.execute.call_count, 1)

    def test_production_postgres_query_errors_fail_closed(self):
        engine = self._validation_engine([], query_error=RuntimeError("permission denied"))
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://user:pass@host/db"}), \
             patch("app.db.get_pg_engine", return_value=engine):
            with self.assertRaisesRegex(RuntimeError, "permission denied"):
                validate_existing_database()

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

    def test_neon_sslmode_url_uses_pg8000_ssl_context_without_unsupported_query(self):
        from unittest.mock import MagicMock
        from app.db import get_pg_engine

        url = (
            "postgresql://user:pass@ep-example-pooler.us-east-2.aws.neon.tech/"
            "neondb?sslmode=require&channel_binding=require&application_name=memory-atlas"
        )
        with patch("app.db.create_engine", return_value=MagicMock()) as create_engine:
            get_pg_engine(url)

        engine_url = create_engine.call_args.args[0]
        connect_args = create_engine.call_args.kwargs["connect_args"]
        self.assertIn("application_name=memory-atlas", engine_url)
        self.assertNotIn("sslmode", engine_url)
        self.assertNotIn("channel_binding", engine_url)
        self.assertIn("ssl_context", connect_args)
        self.assertTrue(hasattr(connect_args["ssl_context"], "load_default_certs"))

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
