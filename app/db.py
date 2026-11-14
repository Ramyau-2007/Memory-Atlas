import collections.abc
import logging
import os
import re
import sqlite3
import ssl
import sys
from importlib.abc import MetaPathFinder
from importlib.machinery import SourceFileLoader, ModuleSpec
from pathlib import Path
from urllib.parse import urlsplit


# ---------------------------------------------------------------------------
# Windows Application Control Protection Hook
# ---------------------------------------------------------------------------
class PurePythonFinder(MetaPathFinder):
    """Bypasses compiled .pyd extensions for SQLAlchemy on Windows when
    Application Control / WDAC policies block native DLL execution.
    """
    def find_spec(self, fullname, path, target=None):
        if fullname.startswith("sqlalchemy"):
            if path:
                for entry in path:
                    subname = fullname.split(".")[-1]
                    py_file = Path(entry) / f"{subname}.py"
                    init_file = Path(entry) / subname / "__init__.py"
                    if py_file.is_file():
                        return ModuleSpec(fullname, SourceFileLoader(fullname, str(py_file)), origin=str(py_file))
                    if init_file.is_file():
                        spec = ModuleSpec(fullname, SourceFileLoader(fullname, str(init_file)), origin=str(init_file), is_package=True)
                        spec.submodule_search_locations = [str(init_file.parent)]
                        return spec
        return None


if not any(isinstance(finder, PurePythonFinder) for finder in sys.meta_path):
    sys.meta_path.insert(0, PurePythonFinder())

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

DATABASE_PATH = os.getenv("DATABASE_PATH", "data/memory.sqlite3")
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

_pg_engines: dict[str, Engine] = {}


def is_postgres_target(target: str | None = None) -> bool:
    if not target:
        return False
    lowered = target.strip().lower()
    return lowered.startswith(("postgres://", "postgresql://", "postgresql+pg8000://"))


def normalize_pg_url(url: str) -> str:
    url = url.strip()
    if url.startswith("postgres://"):
        return "postgresql+pg8000://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+pg8000://" + url[len("postgresql://"):]
    if not url.startswith("postgresql+pg8000://"):
        return "postgresql+pg8000://" + url
    return url


def get_pg_engine(url: str | None = None) -> Engine:
    raw_url = (url or DATABASE_URL).strip()
    if not raw_url:
        raise ValueError("DATABASE_URL is not configured.")
    normalized = normalize_pg_url(raw_url)
    if normalized not in _pg_engines:
        connect_args = {}
        try:
            # Parse both normalized URL (without sslmode) and original URL (may contain sslmode)
            parsed = urlsplit(normalized)
            original_parsed = urlsplit(raw_url)
            # Extract sslmode from original URL query parameters
            query_params = dict(qc.split("=") for qc in original_parsed.query.split("&") if "=" in qc)
            sslmode = query_params.get("sslmode")
            hostname = parsed.hostname
            # Enforce SSL for remote hosts or when sslmode=require is specified
            if hostname and hostname not in ("localhost", "127.0.0.1"):
                if sslmode == "require" or sslmode is None:
                    connect_args["ssl_context"] = ssl.create_default_context()
            elif sslmode == "require":
                # Localhost with explicit require
                connect_args["ssl_context"] = ssl.create_default_context()
        except Exception:
            connect_args["ssl_context"] = ssl.create_default_context()

        engine = create_engine(
            normalized,
            connect_args=connect_args,
            pool_pre_ping=True,
            pool_recycle=300,
        )
        _pg_engines[normalized] = engine
    return _pg_engines[normalized]



def translate_query_for_postgres(sql: str, params: tuple | list | dict | None) -> tuple[str, dict | None]:
    clean = sql.strip()

    # 1. Transaction markers
    if re.match(r"^\s*BEGIN\s+IMMEDIATE\s*$", clean, re.IGNORECASE):
        return "BEGIN", {}

    # 2. CURRENT_TIMESTAMP casting for TEXT columns in PostgreSQL
    clean = re.sub(
        r"\bupdated_at\s*=\s*CURRENT_TIMESTAMP\b",
        "updated_at=CURRENT_TIMESTAMP::text",
        clean,
        flags=re.IGNORECASE,
    )

    # 3. Case-insensitive LIKE matching (SQLite default is case-insensitive for ASCII)
    clean = re.sub(r"\b(?<!I)LIKE\b", "ILIKE", clean)

    # 4. Append RETURNING id for INSERT INTO serial tables
    match = re.match(r"^\s*INSERT\s+INTO\s+([a-zA-Z0-9_]+)", clean, re.IGNORECASE)
    if match:
        table = match.group(1).lower()
        if table in {
            "users", "conversations", "messages", "memories",
            "memory_versions", "temporary_context", "memory_events",
        } and "returning" not in clean.lower():
            clean += " RETURNING id"

    # 5. Convert ? to :p1, :p2...
    if not params:
        return clean, {}
    if isinstance(params, dict):
        return clean, params

    param_names = []

    def replacer(m):
        name = f"p{len(param_names) + 1}"
        param_names.append(name)
        return f":{name}"

    tokens = re.split(r"('(?:''|[^'])*')", clean)
    for i, token in enumerate(tokens):
        if i % 2 == 0:
            tokens[i] = re.sub(r"\?", replacer, token)
    translated_sql = "".join(tokens)
    param_dict = {name: val for name, val in zip(param_names, params)}
    return translated_sql, param_dict


class DbRow(collections.abc.Mapping):
    """Wrapper around SQLAlchemy Row providing sqlite3.Row compatible dict(row),
    row['col'], and row[0] indexing.
    """
    def __init__(self, row):
        self._row = row
        self._mapping = row._mapping

    def __getitem__(self, key):
        if isinstance(key, int):
            try:
                return self._row[key]
            except (TypeError, KeyError):
                return list(self._mapping.values())[key]
        return self._mapping[key]

    def __iter__(self):
        return iter(self._mapping)

    def __len__(self):
        return len(self._mapping)

    def keys(self):
        return self._mapping.keys()

    def values(self):
        return self._mapping.values()

    def items(self):
        return self._mapping.items()

    def get(self, key, default=None):
        return self._mapping.get(key, default)

    def __repr__(self):
        return f"DbRow({dict(self._mapping)})"


class CursorResultWrapper:
    """Cursor-like wrapper over SQLAlchemy CursorResult providing lastrowid,
    rowcount, fetchone(), fetchall(), and iteration.
    """
    def __init__(self, result, lastrowid=None):
        self._result = result
        self.lastrowid = lastrowid
        self.rowcount = result.rowcount if (result and hasattr(result, "rowcount")) else -1
        self._buffered_row = None

    def fetchone(self):
        if self._buffered_row is not None:
            row = self._buffered_row
            self._buffered_row = None
            return row
        if not self._result or not self._result.returns_rows:
            return None
        row = self._result.fetchone()
        return DbRow(row) if row is not None else None

    def fetchall(self):
        rows = []
        if self._buffered_row is not None:
            rows.append(self._buffered_row)
            self._buffered_row = None
        if self._result and self._result.returns_rows:
            rows.extend(DbRow(r) for r in self._result.fetchall())
        return rows

    def __iter__(self):
        if self._buffered_row is not None:
            yield self._buffered_row
            self._buffered_row = None
        if self._result and self._result.returns_rows:
            for r in self._result:
                yield DbRow(r)


class PostgresConnectionWrapper:
    """Database wrapper providing a synchronous sqlite3-compatible interface
    over SQLAlchemy 2.x and pg8000.
    """
    is_postgres = True
    dialect_name = "postgresql"

    def __init__(self, engine: Engine):
        self._engine = engine
        self._conn = None
        self._trans = None

    def _ensure_conn(self):
        if self._conn is None or self._conn.closed:
            self._conn = self._engine.connect()
            self._trans = self._conn.begin()

    def execute(self, sql: str, params=None):
        self._ensure_conn()
        translated_sql, param_dict = translate_query_for_postgres(sql, params)
        if translated_sql == "BEGIN":
            return CursorResultWrapper(None)

        stmt = text(translated_sql)
        res = self._conn.execute(stmt, param_dict or {})
        lastrowid = None
        buffered_row = None
        if "returning id" in translated_sql.lower() and res.returns_rows:
            try:
                row = res.fetchone()
                if row is not None:
                    lastrowid = int(row[0])
                    buffered_row = DbRow(row)
            except Exception:
                pass
        wrapper = CursorResultWrapper(res, lastrowid=lastrowid)
        if buffered_row is not None:
            wrapper._buffered_row = buffered_row
        return wrapper

    def executescript(self, script: str):
        self._ensure_conn()
        statements = [s.strip() for s in script.split(";") if s.strip()]
        for stmt in statements:
            self.execute(stmt)

    def commit(self):
        if self._conn and self._trans and self._trans.is_active:
            self._trans.commit()
            self._trans = None

    def rollback(self):
        if self._conn and self._trans and self._trans.is_active:
            self._trans.rollback()
            self._trans = None

    def close(self):
        try:
            if self._trans and self._trans.is_active:
                self._trans.rollback()
        finally:
            if self._conn and not self._conn.closed:
                self._conn.close()
            self._conn = None
            self._trans = None

    def __enter__(self):
        self._ensure_conn()
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
        finally:
            self.close()


class ClosingConnection(sqlite3.Connection):
    """Make `with connect()` close SQLite handles as well as commit/rollback."""
    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def connect(path: str | None = None):
    configured_url = os.getenv("DATABASE_URL", "").strip()
    target = path or (configured_url if configured_url else DATABASE_PATH)
    if is_postgres_target(target):
        engine = get_pg_engine(target)
        return PostgresConnectionWrapper(engine)

    db_path = path or DATABASE_PATH
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.is_postgres = False
    conn.dialect_name = "sqlite"
    return conn


def validate_existing_database(path: str | None = None) -> tuple[str, int]:
    """Validate a production database without creating or changing it."""
    configured_url = os.getenv("DATABASE_URL", "").strip()
    target = path or (configured_url if configured_url else DATABASE_PATH)

    if is_postgres_target(target):
        engine = get_pg_engine(target)
        try:
            with engine.connect() as conn:
                res = conn.execute(
                    text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
                )
                tables = {row[0] for row in res.fetchall()}
                required = {"users", "sessions", "conversations", "messages", "memories"}
                if not required.issubset(tables):
                    raise RuntimeError(
                        f"Production PostgreSQL database is missing required tables: {required - tables}. "
                        "Refusing to initialize an empty database; run migration script first or verify DATABASE_URL."
                    )
                user_count = conn.execute(text("SELECT COUNT(*) FROM users")).scalar() or 0
                return "postgresql", int(user_count)
        except Exception as exc:
            if isinstance(exc, RuntimeError):
                raise
            raise RuntimeError(
                f"Production PostgreSQL database validation failed: {exc}. "
                "Refusing to initialize an empty database."
            ) from exc

    db_path = path or DATABASE_PATH
    if db_path == ":memory:":
        raise RuntimeError("Production DATABASE_PATH must point to an existing SQLite file.")

    resolved = Path(db_path).resolve()
    if not resolved.is_file():
        raise RuntimeError(
            f"Production database file is missing at {resolved}. "
            "Refusing to initialize an empty database; restore the expected database "
            "or correct DATABASE_PATH."
        )

    try:
        uri = resolved.as_uri() + "?mode=ro"
        db = sqlite3.connect(uri, uri=True)
        try:
            integrity = db.execute("PRAGMA quick_check(1)").fetchone()
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        finally:
            db.close()
    except sqlite3.Error as exc:
        raise RuntimeError(
            f"Production database at {resolved} could not be read as SQLite. "
            "Refusing to initialize an empty database."
        ) from exc

    required = {"users", "sessions", "conversations", "messages"}
    if not integrity or integrity[0] != "ok" or not required.issubset(tables):
        raise RuntimeError(
            f"Production database at {resolved} failed integrity or schema validation. "
            "Refusing to initialize or modify it."
        )
    return str(resolved), resolved.stat().st_size


def init_postgres_db(conn_wrapper: PostgresConnectionWrapper) -> None:
    ddl = """
    CREATE TABLE IF NOT EXISTS users (
      id SERIAL PRIMARY KEY,
      username VARCHAR(64) UNIQUE NOT NULL,
      password_hash TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
      disabled_at TEXT
    );
    CREATE TABLE IF NOT EXISTS invitations (
      id TEXT PRIMARY KEY,
      token_hash TEXT NOT NULL,
      created_at TEXT NOT NULL,
      expires_at TEXT NOT NULL,
      redeemed_at TEXT,
      revoked_at TEXT
    );
    CREATE TABLE IF NOT EXISTS sessions (
      token_hash TEXT PRIMARY KEY,
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      expires_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS conversations (
      id SERIAL PRIMARY KEY,
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      title TEXT NOT NULL,
      summary TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text)
    );
    CREATE TABLE IF NOT EXISTS messages (
      id SERIAL PRIMARY KEY,
      conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
      role TEXT NOT NULL,
      content TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text)
    );
    CREATE TABLE IF NOT EXISTS memories (
      id SERIAL PRIMARY KEY,
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      content TEXT NOT NULL,
      category TEXT NOT NULL,
      priority INTEGER NOT NULL DEFAULT 2,
      source TEXT NOT NULL,
      reason TEXT NOT NULL,
      temporary INTEGER NOT NULL DEFAULT 0,
      deleted_at TEXT,
      created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
      updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
      source_message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL
    );
    CREATE INDEX IF NOT EXISTS memories_owner ON memories(user_id, deleted_at);
    CREATE INDEX IF NOT EXISTS memories_source_message ON memories(source_message_id);
    CREATE TABLE IF NOT EXISTS memory_versions (
      id SERIAL PRIMARY KEY,
      memory_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
      version INTEGER NOT NULL,
      content TEXT NOT NULL,
      category TEXT NOT NULL,
      priority INTEGER NOT NULL,
      reason TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
      UNIQUE(memory_id, version)
    );
    CREATE TABLE IF NOT EXISTS temporary_context (
      id SERIAL PRIMARY KEY,
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      content TEXT NOT NULL,
      expires_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS memory_events (
      id SERIAL PRIMARY KEY,
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      memory_id INTEGER NOT NULL,
      event_type TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text)
    );
    CREATE INDEX IF NOT EXISTS memory_events_owner_time ON memory_events(user_id, created_at);
    CREATE INDEX IF NOT EXISTS memories_fts_gin ON memories USING gin(to_tsvector('english', content || ' ' || category));
    """
    for stmt in ddl.split(";"):
        stmt = stmt.strip()
        if stmt:
            conn_wrapper.execute(stmt)


def init_db(path: str | None = None) -> None:
    with connect(path) as db:
        if getattr(db, "is_postgres", False):
            init_postgres_db(db)
            return

        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
          password_hash TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS invitations (
          id TEXT PRIMARY KEY, token_hash TEXT NOT NULL, created_at TEXT NOT NULL,
          expires_at TEXT NOT NULL, redeemed_at TEXT, revoked_at TEXT
        );
        CREATE TABLE IF NOT EXISTS sessions (
          token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          expires_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS memories (
          id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          content TEXT NOT NULL, category TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 2,
          source TEXT NOT NULL, reason TEXT NOT NULL, temporary INTEGER NOT NULL DEFAULT 0,
          deleted_at TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS memories_owner ON memories(user_id, deleted_at);
        CREATE TABLE IF NOT EXISTS memory_versions (
          id INTEGER PRIMARY KEY, memory_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
          version INTEGER NOT NULL, content TEXT NOT NULL, category TEXT NOT NULL,
          priority INTEGER NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          UNIQUE(memory_id, version)
        );
        CREATE TABLE IF NOT EXISTS conversations (
          id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS messages (
          id INTEGER PRIMARY KEY, conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
          role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS temporary_context (
          id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          content TEXT NOT NULL, expires_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS memory_events (
          id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          memory_id INTEGER NOT NULL, event_type TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS memory_events_owner_time ON memory_events(user_id, created_at);
        """)
        user_columns = {row[1] for row in db.execute("PRAGMA table_info(users)")}
        if "disabled_at" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN disabled_at TEXT")
        memory_columns = {row[1] for row in db.execute("PRAGMA table_info(memories)")}
        if "source_message_id" not in memory_columns:
            db.execute(
                "ALTER TABLE memories ADD COLUMN source_message_id INTEGER "
                "REFERENCES messages(id) ON DELETE SET NULL"
            )
        db.execute(
            "CREATE INDEX IF NOT EXISTS memories_source_message "
            "ON memories(source_message_id)"
        )
        try:
            db.executescript("""
            BEGIN IMMEDIATE;
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_search_fts USING fts5(
              content, category, owner_id UNINDEXED, priority UNINDEXED,
              tokenize='unicode61 remove_diacritics 2'
            );
            CREATE TRIGGER IF NOT EXISTS memories_fts_insert AFTER INSERT ON memories
            WHEN NEW.deleted_at IS NULL AND NEW.temporary=0 BEGIN
              INSERT INTO memory_search_fts(rowid,content,category,owner_id,priority)
              VALUES(NEW.id,NEW.content,NEW.category,NEW.user_id,NEW.priority);
            END;
            CREATE TRIGGER IF NOT EXISTS memories_fts_update
            AFTER UPDATE OF content,category,priority,temporary,deleted_at,user_id ON memories BEGIN
              DELETE FROM memory_search_fts WHERE rowid=OLD.id;
              INSERT INTO memory_search_fts(rowid,content,category,owner_id,priority)
              SELECT NEW.id,NEW.content,NEW.category,NEW.user_id,NEW.priority
              WHERE NEW.deleted_at IS NULL AND NEW.temporary=0;
            END;
            CREATE TRIGGER IF NOT EXISTS memories_fts_delete AFTER DELETE ON memories BEGIN
              DELETE FROM memory_search_fts WHERE rowid=OLD.id;
            END;
            INSERT INTO memory_search_fts(rowid,content,category,owner_id,priority)
            SELECT m.id,m.content,m.category,m.user_id,m.priority FROM memories m
            WHERE m.deleted_at IS NULL AND m.temporary=0
              AND NOT EXISTS (SELECT 1 FROM memory_search_fts f WHERE f.rowid=m.id);
            COMMIT;
            """)
        except sqlite3.OperationalError as exc:
            if "no such module: fts5" not in str(exc).lower():
                raise
