import os
import sqlite3
from pathlib import Path

DATABASE_PATH = os.getenv("DATABASE_PATH", "data/memory.sqlite3")

class ClosingConnection(sqlite3.Connection):
    """Make `with connect()` close SQLite handles as well as commit/rollback."""
    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def connect(path: str | None = None) -> sqlite3.Connection:
    db_path = path or DATABASE_PATH
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: str | None = None) -> None:
    with connect(path) as db:
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
        -- Additive event log for deletion/recovery events. Existing memory and
        -- version rows are left untouched; earlier delete/recovery events cannot
        -- be reconstructed if they were never recorded.
        CREATE TABLE IF NOT EXISTS memory_events (
          id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          memory_id INTEGER NOT NULL, event_type TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS memory_events_owner_time ON memory_events(user_id, created_at);
        """)
        # Additive account suspension state for invitation-only demo operators.
        user_columns = {row[1] for row in db.execute("PRAGMA table_info(users)")}
        if "disabled_at" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN disabled_at TEXT")
        # Additive ownership-safe linkage lets message edits identify only the
        # automatic memories produced by the edited/pruned conversation branch.
        # Legacy memories stay unlinked and are never inferred or rewritten.
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
            # FTS5 is an additive local search index. It duplicates only active,
            # durable memory text and never modifies memory/version records.
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
            # Older SQLite builds can run the app with the lexical-only path.
            if "no such module: fts5" not in str(exc).lower():
                raise
