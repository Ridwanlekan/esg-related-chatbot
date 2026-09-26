import hashlib
import os
import re
import secrets
import sqlite3
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone

CATEGORY_RE = re.compile(r"^[a-z][a-z0-9_-]{1,29}$")

DEFAULT_PRICE_INPUT_PER_M = 0.40
DEFAULT_PRICE_OUTPUT_PER_M = 1.60
DEFAULT_USAGE_TOP_USERS = 20


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
        self._lock = threading.Lock()
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
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS usage ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "at TEXT NOT NULL, "
            "kind TEXT NOT NULL, "
            "model TEXT, "
            "prompt_tokens INTEGER NOT NULL DEFAULT 0, "
            "completion_tokens INTEGER NOT NULL DEFAULT 0, "
            "duration_s REAL, "
            "category TEXT, "
            "user_id TEXT, "
            "session_id TEXT, "
            "request_id TEXT)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS settings ("
            "key TEXT PRIMARY KEY, "
            "value TEXT)"
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_at ON audit(at)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_at ON usage(at)")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_usage_category ON usage(category)"
        )
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

    # ---- token usage & cost ------------------------------------------------

    def record_usage(
        self,
        *,
        kind,
        model=None,
        prompt_tokens=0,
        completion_tokens=0,
        duration_s=None,
        category=None,
        user_id=None,
        session_id=None,
        request_id=None,
    ):
        """Append one LLM-call usage row. Best-effort, never raises."""
        try:
            with self._lock:
                self.conn.execute(
                    "INSERT INTO usage (at, kind, model, prompt_tokens, "
                    "completion_tokens, duration_s, category, user_id, "
                    "session_id, request_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        _now(),
                        kind,
                        model,
                        int(prompt_tokens or 0),
                        int(completion_tokens or 0),
                        duration_s,
                        category,
                        user_id,
                        session_id,
                        request_id,
                    ),
                )
                self.conn.commit()
        except sqlite3.Error:
            pass

    def get_rates(self):
        rows = self.conn.execute("SELECT key, value FROM settings").fetchall()
        settings = {r["key"]: r["value"] for r in rows}
        try:
            price_in = float(settings.get("price_input_per_m", ""))
            if price_in < 0:
                raise ValueError
        except (TypeError, ValueError):
            price_in = DEFAULT_PRICE_INPUT_PER_M
        try:
            price_out = float(settings.get("price_output_per_m", ""))
            if price_out < 0:
                raise ValueError
        except (TypeError, ValueError):
            price_out = DEFAULT_PRICE_OUTPUT_PER_M
        return {
            "price_input_per_m": price_in,
            "price_output_per_m": price_out,
        }

    def set_rates(self, price_input_per_m, price_output_per_m):
        with self._lock:
            self.conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("price_input_per_m", str(float(price_input_per_m))),
            )
            self.conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("price_output_per_m", str(float(price_output_per_m))),
            )
            self.conn.commit()
        return self.get_rates()

    @staticmethod
    def _usage_cost(rates, prompt_tokens, completion_tokens):
        return (
            prompt_tokens / 1_000_000 * rates["price_input_per_m"]
            + completion_tokens / 1_000_000 * rates["price_output_per_m"]
        )

    def _usage_rows(self, range_days=None, category=None):
        where, params = [], []
        if range_days:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=range_days)).isoformat()
            where.append("at >= ?")
            params.append(cutoff)
        if category:
            where.append("category = ?")
            params.append(category)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        return self.conn.execute(
            f"SELECT * FROM usage{clause} ORDER BY id", params
        ).fetchall()

    def usage_stats(self, range_days=None, category=None, top_users=DEFAULT_USAGE_TOP_USERS):
        """Aggregate token usage and estimate cost. All aggregation in Python to
        stay robust against ISO-8601/offset variants in the stored timestamps."""
        rows = self._usage_rows(range_days=range_days, category=category)
        rates = self.get_rates()

        totals = {"calls": len(rows), "prompt_tokens": 0, "completion_tokens": 0,
                  "duration_s": 0.0}
        by_kind = defaultdict(lambda: {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
        by_category = defaultdict(lambda: {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
        by_user = defaultdict(lambda: {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
        by_bucket = defaultdict(lambda: {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
        hourly = range_days is not None and range_days <= 2

        for r in rows:
            pt, ct = r["prompt_tokens"] or 0, r["completion_tokens"] or 0
            totals["prompt_tokens"] += pt
            totals["completion_tokens"] += ct
            totals["duration_s"] += r["duration_s"] or 0.0
            by_kind[r["kind"]]["calls"] += 1
            by_kind[r["kind"]]["prompt_tokens"] += pt
            by_kind[r["kind"]]["completion_tokens"] += ct
            cat = r["category"] or "(unknown)"
            by_category[cat]["calls"] += 1
            by_category[cat]["prompt_tokens"] += pt
            by_category[cat]["completion_tokens"] += ct
            uid = r["user_id"] or "(anonymous)"
            by_user[uid]["calls"] += 1
            by_user[uid]["prompt_tokens"] += pt
            by_user[uid]["completion_tokens"] += ct
            try:
                dt = datetime.fromisoformat(r["at"])
            except (TypeError, ValueError):
                dt = None
            if dt is not None:
                bucket = dt.strftime("%Y-%m-%d %H:00") if hourly else dt.strftime("%Y-%m-%d")
                by_bucket[bucket]["calls"] += 1
                by_bucket[bucket]["prompt_tokens"] += pt
                by_bucket[bucket]["completion_tokens"] += ct

        totals["input_cost"] = self._usage_cost(rates, totals["prompt_tokens"], 0)
        totals["output_cost"] = self._usage_cost(rates, 0, totals["completion_tokens"])
        totals["cost"] = totals["input_cost"] + totals["output_cost"]
        totals["avg_tokens_per_call"] = (
            round((totals["prompt_tokens"] + totals["completion_tokens"]) / totals["calls"]) 
            if totals["calls"] else 0
        )
        totals["avg_cost_per_call"] = (
            totals["cost"] / totals["calls"] if totals["calls"] else 0.0
        )

        def deco(agg):
            return [{"key": k, **v, "cost": self._usage_cost(rates, v["prompt_tokens"], v["completion_tokens"])}
                    for k, v in agg.items()]

        return {
            "rates": rates,
            "totals": totals,
            "by_kind": sorted(deco(by_kind), key=lambda d: -d["cost"]),
            "by_category": sorted(deco(by_category), key=lambda d: -d["cost"]),
            # Exclude the anonymous bucket from top users; per-user spend is
            # the pitch, guesses are not.
            "by_user": sorted(
                (d for d in deco(by_user) if d["key"] != "(anonymous)"),
                key=lambda d: -d["cost"],
            )[:top_users],
            "series": [
                {"bucket": k, **v, "cost": self._usage_cost(rates, v["prompt_tokens"], v["completion_tokens"])}
                for k, v in sorted(by_bucket.items())
            ],
        }

    def usage_export_rows(self, range_days=None, category=None):
        rows = self._usage_rows(range_days=range_days, category=category)
        return [
            {
                "at": r["at"],
                "kind": r["kind"],
                "model": r["model"],
                "prompt_tokens": r["prompt_tokens"],
                "completion_tokens": r["completion_tokens"],
                "duration_s": r["duration_s"],
                "category": r["category"],
                "user_id": r["user_id"],
                "session_id": r["session_id"],
                "request_id": r["request_id"],
            }
            for r in rows
        ]

    def close(self):
        self.conn.close()