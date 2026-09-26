import secrets
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException

from .config import Settings


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class Store:
    def __init__(self, settings: Settings):
        self.settings = settings
        settings.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(settings.database_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA foreign_keys = ON;
            PRAGMA journal_mode = WAL;
            PRAGMA secure_delete = ON;
            PRAGMA busy_timeout = 5000;
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK (role IN ('wearer', 'operator')),
                text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                client_id TEXT NOT NULL,
                UNIQUE(session_id, role, client_id)
            );
            CREATE INDEX IF NOT EXISTS messages_session_seq ON messages(session_id, seq);
            CREATE TABLE IF NOT EXISTS stream_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                publisher_secret TEXT NOT NULL,
                reader_secret TEXT NOT NULL
            );
        """)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO stream_settings VALUES (1, ?, ?)",
                            (secrets.token_urlsafe(32), secrets.token_urlsafe(32)))

    def session(self, session_id: str):
        row = self.db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Conversation not found")
        return row

    def wearer(self, token_hash: str):
        return self.db.execute("SELECT id FROM sessions WHERE token_hash = ?", (token_hash,)).fetchone()

    def create_session(self, name: str, token_hash: str) -> str:
        with self.db:
            count = self.db.execute("SELECT count(*) FROM sessions").fetchone()[0]
            if count >= self.settings.max_sessions:
                raise HTTPException(409, "Conversation limit reached; delete a conversation first")
            session_id = str(uuid4())
            self.db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)",
                            (session_id, name, token_hash, timestamp()))
        return session_id

    def sessions(self):
        return [dict(row) for row in self.db.execute(
            "SELECT id, name, created_at FROM sessions ORDER BY rowid DESC")]

    def messages(self, session_id: str, last: int | None = None):
        self.session(session_id)
        sql = "SELECT id, session_id, role, text, created_at, client_id FROM messages WHERE session_id = ?"
        if last is not None:
            rows = self.db.execute(sql + " ORDER BY seq DESC LIMIT ?", (session_id, last)).fetchall()
            return [dict(row) for row in reversed(rows)]
        return [dict(row) for row in self.db.execute(sql + " ORDER BY seq ASC", (session_id,))]

    def message(self, session_id: str, role: str, text: str, client_id: str):
        with self.db:
            self.session(session_id)
            row = self.db.execute(
                "SELECT id, session_id, role, text, created_at, client_id FROM messages "
                "WHERE session_id = ? AND role = ? AND client_id = ?",
                (session_id, role, client_id),
            ).fetchone()
            if row:
                if row["text"] != text:
                    raise HTTPException(409, "client_id already used with different text")
                return dict(row), False
            count = self.db.execute("SELECT count(*) FROM messages WHERE session_id = ?",
                                    (session_id,)).fetchone()[0]
            if count >= self.settings.max_messages_per_session:
                raise HTTPException(409, "Message limit reached; start a new conversation")
            message = dict(id=str(uuid4()), session_id=session_id, role=role, text=text,
                           created_at=timestamp(), client_id=client_id)
            self.db.execute(
                "INSERT INTO messages (id, session_id, role, text, created_at, client_id) "
                "VALUES (:id, :session_id, :role, :text, :created_at, :client_id)", message)
        return message, True

    def delete(self, session_id: str):
        with self.db:
            self.session(session_id)
            self.db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
