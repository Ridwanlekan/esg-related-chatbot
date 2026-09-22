import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone

from chatbot.workspaces import workspace_names

logger = logging.getLogger("esg.users")

PBKDF2_ITERATIONS = 200_000
MIN_PASSWORD_LENGTH = 8
DEFAULT_TOKEN_TTL_SECONDS = 7 * 24 * 3600
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_EPHEMERAL_WARNED = False


def _now():
    return datetime.now(timezone.utc).isoformat()


def _hash_password(password, salt=None):
    salt = salt if salt is not None else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return salt, digest


def _b64(data) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64d(text) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _auth_secret(provided=None):
    secret = provided or os.environ.get("AUTH_SECRET")
    if secret:
        return secret
    global _EPHEMERAL_WARNED
    if not _EPHEMERAL_WARNED:
        _EPHEMERAL_WARNED = True
        logger.warning(
            "AUTH_SECRET not set - tokens are signed with an ephemeral secret "
            "(users will be logged out on restart). Set AUTH_SECRET in production."
        )
    return secrets.token_hex(32)


def sign_jwt(secret, payload, ttl_seconds=DEFAULT_TOKEN_TTL_SECONDS):
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = {
        **payload,
        "iat": int(time.time()),
        "exp": int(time.time()) + ttl_seconds,
    }
    encoded = _b64(json.dumps(body, separators=(",", ":")).encode())
    digest = hmac.new(
        secret.encode("utf-8"), f"{header}.{encoded}".encode("utf-8"), hashlib.sha256
    ).digest()
    return f"{header}.{encoded}.{_b64(digest)}"


def verify_jwt(secret, token):
    if not token or token.count(".") != 2:
        return None
    try:
        header, encoded, sig = token.split(".")
        expected = hmac.new(
            secret.encode("utf-8"), f"{header}.{encoded}".encode("utf-8"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(expected, _b64d(sig)):
            return None
        body = json.loads(_b64d(encoded))
        if int(body.get("exp", 0)) < time.time():
            return None
        return body
    except Exception:
        return None


def _row_to_user(row):
    return {
        "id": row["id"],
        "email": row["email"],
        "name": row["name"],
        "category": row["category"],
        "created_at": row["created_at"],
        "last_login": row["last_login"] if "last_login" in row.keys() else None,
    }


class UserStore:
    def __init__(self, db_path, secret=None):
        self.db_path = db_path
        self.secret = _auth_secret(provided=secret)
        if db_path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS users ("
            "id TEXT PRIMARY KEY, "
            "email TEXT NOT NULL UNIQUE, "
            "name TEXT NOT NULL, "
            "category TEXT NOT NULL, "
            "password_hash BLOB NOT NULL, "
            "salt BLOB NOT NULL, "
            "created_at TEXT NOT NULL, "
            "last_login TEXT)"
        )
        self.conn.commit()
        self._ensure_schema()

    def _ensure_schema(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(users)").fetchall()}
        if "last_login" not in cols:
            self.conn.execute("ALTER TABLE users ADD COLUMN last_login TEXT")
            self.conn.commit()

    def categories(self):
        return workspace_names()

    def create_user(self, email, password, name, category):
        email = (email or "").strip().lower()
        name = (name or "").strip()
        category = (category or "").strip().lower()
        if not EMAIL_RE.match(email):
            raise ValueError("Enter a valid email address.")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(
                f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
            )
        if category not in self.categories():
            raise ValueError(
                f"Category must be one of: {', '.join(self.categories())}."
            )
        salt, digest = _hash_password(password)
        user_id = secrets.token_hex(16)
        try:
            self.conn.execute(
                "INSERT INTO users (id, email, name, category, password_hash, salt, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, email, name, category, digest, salt, _now()),
            )
            self.conn.commit()
        except sqlite3.IntegrityError:
            raise ValueError("An account with that email already exists.")
        return self.get(user_id)

    def get(self, user_id):
        row = self.conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return _row_to_user(row) if row else None

    def get_by_email(self, email):
        row = self.conn.execute(
            "SELECT * FROM users WHERE email = ?", ((email or "").strip().lower(),)
        ).fetchone()
        return _row_to_user(row) if row else None

    def verify(self, email, password):
        row = self.conn.execute(
            "SELECT * FROM users WHERE email = ?", ((email or "").strip().lower(),)
        ).fetchone()
        if not row:
            return None
        stored_salt = bytes(row["salt"])
        _, digest = _hash_password(password, salt=stored_salt)
        if not hmac.compare_digest(digest, bytes(row["password_hash"])):
            return None
        return _row_to_user(row)

    def list_users(self, limit=100, offset=0, category=None):
        where, params = "", []
        if category:
            where, params = "WHERE category = ?", [category]
        rows = self.conn.execute(
            f"SELECT id, email, name, category, created_at, last_login FROM users {where} "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        return [_row_to_user(r) for r in rows]

    def mark_login(self, user_id):
        self.conn.execute(
            "UPDATE users SET last_login = ? WHERE id = ?", (_now(), user_id)
        )
        self.conn.commit()

    def count_users(self):
        return self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def count_for_category(self, category):
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM users WHERE category = ?", (category,)
        )
        return cur.fetchone()[0]

    def update_user(self, user_id, name=None, category=None, password=None):
        existing = self.get(user_id)
        if existing is None:
            return None
        new_name = (name or "").strip() if name is not None else existing["name"]
        new_cat = (category or "").strip().lower() if category is not None else existing["category"]
        if new_cat not in self.categories():
            raise ValueError(
                f"Category must be one of: {', '.join(self.categories())}."
            )
        if password is not None and len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(
                f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
            )
        if password:
            salt, digest = _hash_password(password)
            self.conn.execute(
                "UPDATE users SET salt=?, password_hash=?, name=?, category=? "
                "WHERE id=?",
                (salt, digest, new_name, new_cat, user_id),
            )
        else:
            self.conn.execute(
                "UPDATE users SET name=?, category=? WHERE id=?",
                (new_name, new_cat, user_id),
            )
        self.conn.commit()
        return self.get(user_id)

    def delete_user(self, user_id):
        cur = self.conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def token_for(self, user, ttl_seconds=None):
        ttl = ttl_seconds or int(os.environ.get("TOKEN_TTL_SECONDS", DEFAULT_TOKEN_TTL_SECONDS))
        return sign_jwt(
            self.secret,
            {
                "sub": user["id"],
                "email": user["email"],
                "name": user["name"],
                "category": user["category"],
            },
            ttl_seconds=ttl,
        )

    def close(self):
        self.conn.close()