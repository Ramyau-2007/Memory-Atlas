import os
import builtins
import types
import pytest
from unittest.mock import patch, MagicMock

# Import modules under test
from app import db, auth, demo_admin
from fastapi import HTTPException

# Helper to reset pg engine cache between tests
def clear_pg_engine_cache():
    db._pg_engines.clear()

# -------------------------------------------------------------------
# PostgreSQL URL normalization and SSL handling
# -------------------------------------------------------------------

def test_normalize_pg_url_handles_sslmode_require():
    url = "postgresql://user:pass@remotehost/db?sslmode=require"
    normalized = db.normalize_pg_url(url)
    assert normalized.startswith("postgresql+pg8000://")
    # get_pg_engine should add ssl_context when hostname is remote and sslmode=require
    clear_pg_engine_cache()
    with patch("app.db.create_engine") as mock_create_engine:
        mock_engine = MagicMock()
        mock_create_engine.return_value = mock_engine
        engine = db.get_pg_engine(url)
        # engine should be cached
        assert engine is mock_engine
        # extract the connect_args passed to create_engine
        args, kwargs = mock_create_engine.call_args
        connect_args = kwargs.get("connect_args", {})
        # ssl_context should be present because sslmode=require forces SSL
        assert "ssl_context" in connect_args
        # Should be an SSLContext instance
        assert hasattr(connect_args["ssl_context"], "load_default_certs")

# -------------------------------------------------------------------
# Duplicate username handling
# -------------------------------------------------------------------

def test_register_duplicate_username_sqlite(tmp_path):
    # Use a temporary SQLite file
    db_path = tmp_path / "test.db"
    # Initialize DB schema
    from app.db import init_db
    init_db(str(db_path))
    # First registration succeeds
    user_id = auth.register("alice", "password123", db_path=str(db_path))
    assert isinstance(user_id, int)
    # Second registration with same username should raise 409
    with pytest.raises(HTTPException) as excinfo:
        auth.register("alice", "anotherpass", db_path=str(db_path))
    assert excinfo.value.status_code == 409

def test_register_duplicate_username_postgres(monkeypatch):
    # Force PostgreSQL target via DATABASE_URL env var
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@host/db")
    # Mock connect to simulate IntegrityError with pgcode 23505
    class MockConn:
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, tb):
            pass
        def execute(self, *args, **kwargs):
            # Simulate IntegrityError from SQLAlchemy
            from sqlalchemy.exc import IntegrityError
            orig = types.SimpleNamespace(pgcode="23505")
            raise IntegrityError(statement=args[0], params=args[1] if len(args) > 1 else None, orig=orig)
    monkeypatch.setattr(auth, "connect", lambda *_: MockConn())
    with pytest.raises(HTTPException) as excinfo:
        auth.register("bob", "securepass")
    assert excinfo.value.status_code == 409

# -------------------------------------------------------------------
# CLI database error handling in demo_admin
# -------------------------------------------------------------------

def test_demo_admin_cli_error_handling(monkeypatch, capsys):
    # Force a ValueError inside the command handling
    monkeypatch.setattr(demo_admin, "create_invitation", lambda *_: (_ for _ in ()).throw(ValueError("bad")))
    # Call main with the invite command
    exit_code = demo_admin.main(["invite"])
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "Could not complete the command" in captured.err
    assert "Confirm DATABASE_PATH" in captured.err
