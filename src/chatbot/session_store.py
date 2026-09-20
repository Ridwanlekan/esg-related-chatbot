import os
import sqlite3
import uuid
from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc).isoformat()


class SessionStore:
    def __init__(self, db_path, retention=None):
        self.db_path = db_path
        self.retention = int(retention if retention is not None else os.environ.get("MAX_RETAINED_SESSIONS", "500"))
        if db_path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            "id TEXT PRIMARY KEY, "
            "created_at TEXT NOT NULL)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS messages ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "session_id TEXT NOT NULL, "
            "role TEXT NOT NULL, "
            "content TEXT NOT NULL, "
            "created_at TEXT NOT NULL)"
        )
        self.conn.commit()

    def create(self, session_id):
        self.conn.execute(
            "INSERT OR IGNORE INTO sessions (id, created_at) VALUES (?, ?)",
            (session_id, _now()),
        )
        self.conn.commit()
        if self.retention and self.count() > self.retention:
            self.prune(self.retention)

    def append(self, session_id, role, content):
        self.create(session_id)
        self.conn.execute(
            "INSERT INTO messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (session_id, role, content, _now()),
        )
        self.conn.commit()

    def history(self, session_id, limit=10):
        rows = self.conn.execute(
            "SELECT role, content FROM messages "
            "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [
            {"role": row["role"], "content": row["content"]}
            for row in reversed(rows)
        ]

    def messages(self, session_id):
        rows = self.conn.execute(
            "SELECT role, content FROM messages "
            "WHERE session_id = ? ORDER BY id ASC",
            (session_id,),
        ).fetchall()
        return [{"role": row["role"], "content": row["content"]} for row in rows]

    def list_sessions(self, limit=20, offset=0):
        total = self.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        rows = self.conn.execute(
            "SELECT s.id, s.created_at, "
            "COUNT(m.id) AS message_count, "
            "(SELECT content FROM messages m2 WHERE m2.session_id = s.id "
            " ORDER BY m2.id DESC LIMIT 1) AS last_message "
            "FROM sessions s LEFT JOIN messages m ON m.session_id = s.id "
            "GROUP BY s.id ORDER BY MAX(m.id) DESC, s.created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return {
            "total": total,
            "sessions": [
                {
                    "session_id": row["id"],
                    "created_at": row["created_at"],
                    "message_count": row["message_count"],
                    "last_message": row["last_message"],
                }
                for row in rows
            ],
        }

    def delete(self, session_id):
        self.conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        self.conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        self.conn.commit()

    def count(self):
        return self.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

    def prune(self, keep=500):
        """Delete the oldest sessions beyond the retention cap. Returns the number removed."""
        removed = 0
        rows = self.conn.execute(
            "SELECT id FROM sessions "
            "ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?",
            (keep,),
        ).fetchall()
        for row in rows:
            self.delete(row["id"])
            removed += 1
        return removed

    def new_id(self):
        return uuid.uuid4().hex

    def close(self):
        self.conn.close()