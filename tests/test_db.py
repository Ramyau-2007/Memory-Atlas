import tempfile
import unittest
from pathlib import Path

from app.db import connect, init_db, validate_existing_database


class ProductionDatabaseValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_database_is_rejected_without_creating_a_file(self):
        missing = self.root / "expected.sqlite3"
        with self.assertRaisesRegex(RuntimeError, "Refusing to initialize an empty database"):
            validate_existing_database(str(missing))
        self.assertFalse(missing.exists())

    def test_empty_or_unrecognized_database_is_rejected_without_schema_changes(self):
        empty = self.root / "empty.sqlite3"
        empty.touch()
        with self.assertRaisesRegex(RuntimeError, "integrity or schema validation"):
            validate_existing_database(str(empty))
        with connect(str(empty)) as db:
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        self.assertEqual(tables, [])

    def test_valid_database_is_checked_read_only_and_existing_account_remains(self):
        path = self.root / "existing.sqlite3"
        init_db(str(path))
        with connect(str(path)) as db:
            db.execute("INSERT INTO users(username,password_hash) VALUES(?,?)", ("test-user", "test-hash"))

        resolved, size = validate_existing_database(str(path))
        self.assertEqual(Path(resolved), path.resolve())
        self.assertGreater(size, 0)
        with connect(str(path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
