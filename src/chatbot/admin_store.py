import hashlib
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

CATEGORY_RE = re.compile(r"^[a-z][a-z0-9_-]{1,29}$")


def _now():
    return datetime.now(timezone.utc).isoformat()


def validate_category(category):
    """Return the normalized category or raise ValueError."""
    cat = (category or "").strip().lower()
    if not CATEGORY_RE.match(cat):
        raise ValueError(
            "Category must be 2-30 chars, start with a letter, and use only "
            "a-z, 0-9, '-' and '_'."
        )
    return cat


class AdminStore:
    """Persistent admin registry (workspace definitions) in SQLite.

    Base workspace categories (finance, hr, ...) come from the WORKSPACES env
    variable and the built-in labels; this store adds *extra* workspaces and
    lets an admin override the label/blurb/emoji of any category. Rows here
    take precedence over the static defaults (see `workspaces.reload_extra`).
    """

    def __init__(self, db_path):
        self.db_path = db_path
        if db_path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS workspaces ("
            "category TEXT PRIMARY KEY, "
            "label TEXT NOT NULL, "
            "blurb TEXT, "
            "emoji TEXT, "
            "created_at TEXT NOT NULL)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS audit ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "at TEXT NOT NULL, "
            "actor TEXT, "
            "event TEXT NOT NULL, "
            "object_type TEXT, "
            "object_id TEXT, "
            "detail TEXT)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS invites ("
            "token TEXT PRIMARY KEY, "
            "category TEXT NOT NULL, "
            "label TEXT, "
            "max_uses INTEGER NOT NULL, "
            "uses INTEGER NOT NULL DEFAULT 0, "
            "created_at TEXT NOT NULL, "
            "expires_at TEXT NOT NULL, "
            "actor TEXT)"
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_at ON audit(at)")
        self.conn.commit()

    def list_workspaces(self):
        rows = self.conn.execute(
            "SELECT * FROM workspaces ORDER BY category"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_workspace(self, category):
        row = self.conn.execute(
            "SELECT * FROM workspaces WHERE category = ?", (category,)
        ).fetchone()
        return dict(row) if row else None

    def upsert_workspace(self, category, label=None, blurb=None, emoji=None):
        cat = validate_category(category)
        label = (label or "").strip() or cat.title()
        existing = self.get_workspace(cat)
        blurb = (blurb or "").strip() or (existing or {}).get("blurb") or label
        emoji = (emoji or "").strip() or (existing or {}).get("emoji") or "\U0001F4D6"
        self.conn.execute(
            "INSERT INTO workspaces (category, label, blurb, emoji, created_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(category) DO UPDATE SET label=excluded.label, "
            "blurb=excluded.blurb, emoji=excluded.emoji",
            (cat, label, blurb, emoji, _now()),
        )
        self.conn.commit()
        return self.get_workspace(cat)

    def delete_workspace(self, category):
        cur = self.conn.execute(
            "DELETE FROM workspaces WHERE category = ?", (category,)
        )
        self.conn.commit()
        return cur.rowcount > 0

    # ---- audit log ---------------------------------------------------------

    def log_event(self, actor, event, object_type=None, object_id=None, detail=""):
        self.conn.execute(
            "INSERT INTO audit (at, actor, event, object_type, object_id, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), actor, event,
             object_type, object_id, detail or ""),
        )
        self.conn.commit()

    def list_events(self, limit=100, offset=0):
        total = self.conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
        rows = self.conn.execute(
            "SELECT at, actor, event, object_type, object_id, detail FROM audit "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return {"total": total, "events": [dict(r) for r in rows]}

    # ---- invites -----------------------------------------------------------

    @staticmethod
    def _hash_token(token):
        return hashlib.sha256((token or "").encode("utf-8")).hexdigest()

    def create_invite(self, category, label=None, max_uses=1, ttl_hours=168, actor=""):
        cat = validate_category(category)
        token = secrets.token_urlsafe(24)
        now = datetime.now(timezone.utc)
        expires = now + timedelta(hours=int(ttl_hours))
        self.conn.execute(
            "INSERT INTO invites (token, category, label, max_uses, uses, "
            "created_at, expires_at, actor) VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
            (self._hash_token(token), cat, (label or "").strip() or None,
             int(max(1, max_uses)), now.isoformat(), expires.isoformat(), actor or ""),
        )
        self.conn.commit()
        return {
            "token": token,
            "category": cat,
            "label": (label or "").strip() or None,
            "max_uses": int(max(1, max_uses)),
            "uses": 0,
            "created_at": now.isoformat(),
            "expires_at": expires.isoformat(),
        }

    def _invite_row(self, token_hash):
        return self.conn.execute(
            "SELECT category, label, max_uses, uses, expires_at FROM invites "
            "WHERE token = ?",
            (token_hash,),
        ).fetchone()

    def peek_invite(self, token):
        """Validate an invite without consuming it. Returns row or raises ValueError."""
        row = self._invite_row(self._hash_token(token))
        if row is None:
            raise ValueError("That invitation link is not valid.")
        usable, reason = self._invite_status(row)
        if not usable:
            raise ValueError(reason)
        return {
            "category": row["category"],
            "label": row["label"],
            "expires_at": row["expires_at"],
        }

    @staticmethod
    def _invite_status(row):
        if row["uses"] >= row["max_uses"]:
            return False, "That invitation link has already been used up."
        try:
            expires = datetime.fromisoformat(row["expires_at"])
        except (TypeError, ValueError):
            return True, None
        if expires <= datetime.now(timezone.utc):
            return False, "That invitation link has expired."
        return True, None

    def redeem_invite(self, token):
        """Consume one use of an invite and return its category/label."""
        token_hash = self._hash_token(token)
        row = self._invite_row(token_hash)
        if row is None:
            raise ValueError("That invitation link is not valid.")
        usable, reason = self._invite_status(row)
        if not usable:
            raise ValueError(reason)
        self.conn.execute(
            "UPDATE invites SET uses = uses + 1 WHERE token = ?", (token_hash,)
        )
        self.conn.commit()
        return {"category": row["category"], "label": row["label"]}

    def list_invites(self):
        now = datetime.now(timezone.utc)
        rows = self.conn.execute(
            "SELECT category, label, max_uses, uses, created_at, expires_at, actor "
            "FROM invites ORDER BY created_at DESC"
        ).fetchall()
        invites = []
        for r in rows:
            usable, reason = self._invite_status(r)
            invites.append({**dict(r), "valid": usable, "reason": reason, "token": None})
        return invites

    def delete_invite(self, token):
        cur = self.conn.execute(
            "DELETE FROM invites WHERE token = ?", (self._hash_token(token),)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def close(self):
        self.conn.close()