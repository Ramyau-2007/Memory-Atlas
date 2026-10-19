import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import app.db as database
from app.db import init_db
from app.demo_admin import main
from app.invitations import invitations_required


class DemoAdminTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_path = database.DATABASE_PATH
        database.DATABASE_PATH = str(Path(self.temp.name) / 'demo.sqlite3')
        init_db()

    def tearDown(self):
        database.DATABASE_PATH = self.original_path
        self.temp.cleanup()

    def test_operator_cli_displays_code_once_and_stores_only_hash(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(['invite', '--expires-days', '2']), 0)
        lines = output.getvalue().splitlines()
        code = lines[-1]
        self.assertTrue(code.startswith('ma1.'))
        self.assertEqual(output.getvalue().count(code), 1)
        invitation_id = lines[0].split(': ', 1)[1]
        with database.connect() as db:
            row = db.execute('SELECT token_hash,expires_at FROM invitations WHERE id=?', (invitation_id,)).fetchone()
            self.assertIsNotNone(row)
            self.assertNotEqual(row['token_hash'], code)
            self.assertTrue(row['expires_at'])

    def test_operator_cli_can_revoke_invites_and_disable_reenable_accounts(self):
        with database.connect() as db:
            db.execute("INSERT INTO users(username,password_hash) VALUES('member','hash')")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(['invite']), 0)
        invitation_id = output.getvalue().splitlines()[0].split(': ', 1)[1]
        self.assertEqual(main(['revoke-invite', invitation_id]), 0)
        with database.connect() as db:
            db.execute("INSERT INTO sessions VALUES('token-hash',1,'2099-01-01T00:00:00+00:00')")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['disable-user', 'member']), 0)
        with database.connect() as db:
            user = db.execute("SELECT disabled_at FROM users WHERE username='member'").fetchone()
            self.assertTrue(user['disabled_at'])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions WHERE user_id=1').fetchone()[0], 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['enable-user', 'member']), 0)
        with database.connect() as db:
            self.assertIsNone(db.execute("SELECT disabled_at FROM users WHERE username='member'").fetchone()['disabled_at'])

    def test_invitation_policy_defaults_open_only_in_development(self):
        with patch.dict(os.environ, {'APP_ENV':'development'}, clear=True):
            self.assertFalse(invitations_required())
        with patch.dict(os.environ, {'APP_ENV':'production'}, clear=True):
            self.assertTrue(invitations_required())
            with patch.dict(os.environ, {'DEMO_INVITATIONS_REQUIRED':'false'}):
                self.assertFalse(invitations_required())

    def test_account_disable_column_migration_preserves_legacy_user(self):
        legacy_path = Path(self.temp.name) / 'legacy.sqlite3'
        with database.connect(str(legacy_path)) as db:
            db.execute("CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
            db.execute("INSERT INTO users(id,username,password_hash) VALUES(7,'legacy','existing-hash')")
        init_db(str(legacy_path))
        with database.connect(str(legacy_path)) as db:
            columns = {row[1] for row in db.execute('PRAGMA table_info(users)')}
            row = tuple(db.execute('SELECT id,username,password_hash,disabled_at FROM users WHERE id=7').fetchone())
        self.assertIn('disabled_at', columns)
        self.assertEqual(row, (7, 'legacy', 'existing-hash', None))


if __name__ == '__main__':
    unittest.main()
