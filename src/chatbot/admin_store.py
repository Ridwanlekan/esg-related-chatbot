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
# Voice ledger rates. Defaults mirror the provider pricing the voice feature
# falls back to (Whisper API $/audio-minute, gpt-4o-mini-tts $/1M characters) —
# verify current provider pricing before relying on the absolute numbers, and
# note local faster-whisper STT has no per-request marginal cost at all.
DEFAULT_PRICE_STT_PER_MIN = 0.006
DEFAULT_PRICE_TTS_PER_M = 0.60
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
            "actor TEXT, "
            "organisation_id TEXT)"
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
            "units REAL, "
            "category TEXT, "
            "user_id TEXT, "
            "session_id TEXT, "
            "request_id TEXT, "
            "organisation_id TEXT)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS settings ("
            "key TEXT PRIMARY KEY, "
            "value TEXT)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS admin_sessions ("
            "token_hash TEXT PRIMARY KEY, "
            "actor TEXT NOT NULL, "
            "created_at TEXT NOT NULL, "
            "expires_at TEXT NOT NULL)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS shared_docs ("
            "filename TEXT PRIMARY KEY, "
            "size INTEGER NOT NULL DEFAULT 0, "
            "created_at TEXT NOT NULL, "
            "actor TEXT)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS shared_doc_targets ("
            "filename TEXT NOT NULL, "
            "category TEXT NOT NULL, "
            "link_name TEXT NOT NULL, "
            "link_kind TEXT, "
            "created_at TEXT NOT NULL, "
            "PRIMARY KEY (filename, category))"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS library_docs ("
            "filename TEXT PRIMARY KEY, "
            "subject TEXT, "
            "jurisdiction TEXT, "
            "effective_date TEXT, "
            "version TEXT, "
            "size INTEGER NOT NULL DEFAULT 0, "
            "created_at TEXT NOT NULL, "
            "actor TEXT)"
        )
        # Section 3.4: assignment is the only access control, and each act is
        # recorded with who made it and when, so the audit log can answer which
        # content an organisation was given. Scoped per organisation rather than
        # per workspace so an org's full content set is one query.
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS content_assignments ("
            "organisation_id TEXT NOT NULL, "
            "category TEXT NOT NULL, "
            "filename TEXT NOT NULL, "
            "assigned_at TEXT NOT NULL, "
            "actor TEXT, "
            "PRIMARY KEY (organisation_id, category, filename))"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_content_assignments_org "
            "ON content_assignments(organisation_id)"
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_at ON audit(at)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_at ON usage(at)")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_usage_category ON usage(category)"
        )
        # Outgoing mail (D18). Present so a deployment without an SMTP relay can
        # still complete verification by hand instead of blocking signups.
        # `consumed_at` is set when someone acts on the message, which is the
        # event that matters for onboarding, not when it is read.
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS email_outbox ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "to_address TEXT NOT NULL, "
            "subject TEXT NOT NULL, "
            "body TEXT NOT NULL, "
            "html_body TEXT, "
            "created_at TEXT NOT NULL, "
            "consumed_at TEXT)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_email_outbox_created "
            "ON email_outbox(created_at)"
        )
        self.conn.commit()
        self._migrate_usage_units()
        self._migrate_usage_organisation()
        self._migrate_invites_organisation()

    # ---- outbox (D18) ------------------------------------------------------

    def record_email(self, to_address, subject, body, html_body=None):
        cursor = self.conn.execute(
            "INSERT INTO email_outbox (to_address, subject, body, html_body, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (to_address, subject, body, html_body, _now()),
        )
        self.conn.commit()
        return cursor.lastrowid

    def list_emails(self, limit=50, offset=0, include_consumed=True):
        """Newest first, so the message a new signup awaits sits at the top."""
        sql = "SELECT * FROM email_outbox"
        if not include_consumed:
            sql += " WHERE consumed_at IS NULL"
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        return [dict(r) for r in self.conn.execute(sql, (limit, offset)).fetchall()]

    def count_emails(self, include_consumed=False):
        sql = "SELECT COUNT(*) FROM email_outbox"
        if not include_consumed:
            sql += " WHERE consumed_at IS NULL"
        return self.conn.execute(sql).fetchone()[0]

    def consume_email(self, email_id):
        """Mark a message dealt with, so it drops off the pending list."""
        self.conn.execute(
            "UPDATE email_outbox SET consumed_at = ? "
            "WHERE id = ? AND consumed_at IS NULL",
            (_now(), email_id),
        )
        self.conn.commit()
        return self.conn.execute(
            "SELECT * FROM email_outbox WHERE id = ?", (email_id,)
        ).fetchone()

    # ---- master library and content assignment (Section 3.4) ---------------

    def add_library_doc(
        self, filename, size=0, subject=None, jurisdiction=None,
        effective_date=None, version=None, actor="",
    ):
        """Register a curated master document. Idempotent on filename."""
        self.conn.execute(
            "INSERT INTO library_docs (filename, subject, jurisdiction, "
            "effective_date, version, size, created_at, actor) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(filename) DO UPDATE SET subject=excluded.subject, "
            "jurisdiction=excluded.jurisdiction, "
            "effective_date=excluded.effective_date, version=excluded.version, "
            "size=excluded.size, actor=excluded.actor",
            (filename, subject, jurisdiction, effective_date, version, size,
             _now(), actor),
        )
        self.conn.commit()
        return self.get_library_doc(filename)

    def get_library_doc(self, filename):
        row = self.conn.execute(
            "SELECT * FROM library_docs WHERE filename = ?", (filename,)
        ).fetchone()
        return dict(row) if row else None

    def list_library_docs(self, subject=None):
        """The curated master library, for the admin console to assign from."""
        if subject:
            rows = self.conn.execute(
                "SELECT * FROM library_docs WHERE subject = ? ORDER BY filename",
                (subject,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM library_docs ORDER BY filename"
            ).fetchall()
        return [dict(r) for r in rows]

    def remove_library_doc(self, filename):
        cur = self.conn.execute(
            "DELETE FROM library_docs WHERE filename = ?", (filename,)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def add_content_assignment(self, organisation_id, category, filename, actor=""):
        """Record that an organisation was given a document (idempotent)."""
        self.conn.execute(
            "INSERT OR IGNORE INTO content_assignments "
            "(organisation_id, category, filename, assigned_at, actor) "
            "VALUES (?, ?, ?, ?, ?)",
            (organisation_id, category, filename, _now(), actor),
        )
        self.conn.commit()

    def remove_content_assignment(self, organisation_id, category, filename):
        cur = self.conn.execute(
            "DELETE FROM content_assignments "
            "WHERE organisation_id = ? AND category = ? AND filename = ?",
            (organisation_id, category, filename),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def list_content_assignments(self, organisation_id=None, category=None):
        if organisation_id and category:
            rows = self.conn.execute(
                "SELECT * FROM content_assignments "
                "WHERE organisation_id = ? AND category = ? ORDER BY filename",
                (organisation_id, category),
            ).fetchall()
        elif organisation_id:
            rows = self.conn.execute(
                "SELECT * FROM content_assignments "
                "WHERE organisation_id = ? ORDER BY category, filename",
                (organisation_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM content_assignments ORDER BY organisation_id, "
                "category, filename"
            ).fetchall()
        return [dict(r) for r in rows]

    def assigned_filenames(self, organisation_id, category):
        rows = self.conn.execute(
            "SELECT filename FROM content_assignments "
            "WHERE organisation_id = ? AND category = ?",
            (organisation_id, category),
        ).fetchall()
        return [r["filename"] for r in rows]

    def _migrate_usage_units(self):
        """Add the generic non-token unit count (voice seconds/characters).

        Lazy ALTER, so a usage table written by an older build opens untouched
        and simply reads NULL (no units) for its rows.
        """
        cols = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(usage)").fetchall()
        }
        if "units" not in cols:
            self.conn.execute("ALTER TABLE usage ADD COLUMN units REAL")
            self.conn.commit()

    def _migrate_usage_organisation(self):
        """Attribute usage rows to an organisation, which is what billing counts.

        Questions are billed to the organisation rather than the person who
        asked, and the question pool is per organisation per period, so a row
        without an owner cannot be counted at all. Rows written before this
        column existed simply read NULL and are excluded from the pool, which
        under-counts rather than over-counts a customer.
        """
        cols = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(usage)").fetchall()
        }
        if "organisation_id" not in cols:
            self.conn.execute("ALTER TABLE usage ADD COLUMN organisation_id TEXT")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_usage_org_period "
            "ON usage(organisation_id, kind, at)"
        )
        self.conn.commit()

    def _migrate_invites_organisation(self):
        """Add the organisation an invite belongs to.

        Lazy ALTER, so a database opened by an older build keeps working: its
        invites simply have no organisation and redeem into the free tier,
        which is what those tokens always did.
        """
        cols = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(invites)").fetchall()
        }
        if "organisation_id" not in cols:
            self.conn.execute("ALTER TABLE invites ADD COLUMN organisation_id TEXT")
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

    # ---- cross-workspace shared documents -----------------------------------

    def add_shared_doc(self, filename, size=0, actor=""):
        """Register a canonical shared document (idempotent on filename)."""
        self.conn.execute(
            "INSERT INTO shared_docs (filename, size, created_at, actor) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(filename) DO UPDATE SET size=excluded.size",
            (filename, int(size or 0), _now(), actor or ""),
        )
        self.conn.commit()

    def remove_shared_doc(self, filename):
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM shared_docs WHERE filename = ?", (filename,)
            )
            self.conn.execute(
                "DELETE FROM shared_doc_targets WHERE filename = ?", (filename,)
            )
            self.conn.commit()
        return cur.rowcount > 0

    def get_shared_doc(self, filename):
        row = self.conn.execute(
            "SELECT * FROM shared_docs WHERE filename = ?", (filename,)
        ).fetchone()
        return dict(row) if row else None

    def list_shared_docs(self):
        """Every shared document with its current target workspaces."""
        docs = [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM shared_docs ORDER BY filename"
            ).fetchall()
        ]
        targets = [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM shared_doc_targets ORDER BY filename, category"
            ).fetchall()
        ]
        by_doc = defaultdict(list)
        for t in targets:
            by_doc[t["filename"]].append(t)
        for d in docs:
            d["targets"] = by_doc.get(d["filename"], [])
        return docs

    def add_shared_target(self, filename, category, link_name, link_kind=None):
        cat = validate_category(category)
        self.conn.execute(
            "INSERT INTO shared_doc_targets (filename, category, link_name, "
            "link_kind, created_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(filename, category) DO UPDATE SET "
            "link_name=excluded.link_name, link_kind=excluded.link_kind",
            (filename, cat, link_name, link_kind, _now()),
        )
        self.conn.commit()

    def remove_shared_target(self, filename, category):
        cur = self.conn.execute(
            "DELETE FROM shared_doc_targets WHERE filename = ? AND category = ?",
            (filename, category),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def get_shared_target(self, filename, category):
        row = self.conn.execute(
            "SELECT * FROM shared_doc_targets WHERE filename = ? AND category = ?",
            (filename, category),
        ).fetchone()
        return dict(row) if row else None

    def shared_target_categories(self, filename):
        return [
            r[0]
            for r in self.conn.execute(
                "SELECT category FROM shared_doc_targets WHERE filename = ? "
                "ORDER BY category",
                (filename,),
            ).fetchall()
        ]

    def all_shared_targets(self):
        """Flattened target rows, used to validate private uploads/deletes."""
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM shared_doc_targets"
            ).fetchall()
        ]

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

    def create_invite(self, category, label=None, max_uses=1, ttl_hours=168,
                      actor="", organisation_id=None):
        cat = validate_category(category)
        token = secrets.token_urlsafe(24)
        now = datetime.now(timezone.utc)
        expires = now + timedelta(hours=int(ttl_hours))
        org = (organisation_id or "").strip() or None
        self.conn.execute(
            "INSERT INTO invites (token, category, label, max_uses, uses, "
            "created_at, expires_at, actor, organisation_id) "
            "VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)",
            (self._hash_token(token), cat, (label or "").strip() or None,
             int(max(1, max_uses)), now.isoformat(), expires.isoformat(),
             actor or "", org),
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
            "organisation_id": org,
        }

    def _invite_row(self, token_hash):
        return self.conn.execute(
            "SELECT category, label, max_uses, uses, expires_at, organisation_id "
            "FROM invites WHERE token = ?",
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
            "organisation_id": row["organisation_id"],
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
        """Consume one use of an invite and return its category/org binding."""
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
        return {"category": row["category"], "label": row["label"],
                "organisation_id": row["organisation_id"]}

    def list_invites(self):
        now = datetime.now(timezone.utc)
        rows = self.conn.execute(
            "SELECT category, label, max_uses, uses, created_at, expires_at, "
            "actor, organisation_id "
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
        units=None,
        category=None,
        user_id=None,
        session_id=None,
        request_id=None,
        organisation_id=None,
    ):
        """Append one metered usage row. Best-effort, never raises.

        `units` is the generic non-token meter: seconds of audio for kind
        "stt", characters for kind "tts". Token-based kinds leave it None.
        `organisation_id` is what makes the row billable: the question pool
        belongs to the organisation, not to the person who asked.
        """
        try:
            with self._lock:
                self.conn.execute(
                    "INSERT INTO usage (at, kind, model, prompt_tokens, "
                    "completion_tokens, duration_s, units, category, user_id, "
                    "session_id, request_id, organisation_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        _now(),
                        kind,
                        model,
                        int(prompt_tokens or 0),
                        int(completion_tokens or 0),
                        duration_s,
                        units,
                        category,
                        user_id,
                        session_id,
                        request_id,
                        organisation_id,
                    ),
                )
                self.conn.commit()
        except sqlite3.Error:
            pass

    def questions_in_period(self, organisation_id, since=None):
        """Questions an organisation has asked since `since` (ISO-8601).

        Counted from rows written as kind "question" - one per request that
        reached the model - rather than from the LLM rows underneath it, which
        are several per question (a rewrite and an answer both log usage).
        Voice and search rows are therefore never counted either: speech is
        bundled (Q18) and a search generates no answer.
        """
        if not organisation_id:
            return 0
        sql = (
            "SELECT COUNT(*) FROM usage "
            "WHERE organisation_id = ? AND kind = 'question'"
        )
        params = [organisation_id]
        if since:
            sql += " AND at >= ?"
            params.append(since)
        try:
            with self._lock:
                row = self.conn.execute(sql, params).fetchone()
            return int(row[0] or 0)
        except sqlite3.Error:
            return 0

    @staticmethod
    def _nonneg_float(value, default):
        try:
            parsed = float(value)
            if parsed < 0:
                raise ValueError
            return parsed
        except (TypeError, ValueError):
            return default

    def get_setting(self, key, default=None):
        row = self.conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row and row[0] is not None else default

    def set_setting(self, key, value):
        with self._lock:
            self.conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )
            self.conn.commit()
        return self.get_setting(key)

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
            # Voice rates are env-driven (not admin-editable): they track the
            # provider's published price rather than a negotiated token rate.
            "price_stt_per_min": self._nonneg_float(
                os.environ.get("VOICE_STT_PRICE_PER_MIN"), DEFAULT_PRICE_STT_PER_MIN
            ),
            "price_tts_per_m": self._nonneg_float(
                os.environ.get("VOICE_TTS_PRICE_PER_M"), DEFAULT_PRICE_TTS_PER_M
            ),
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
    def _usage_cost(rates, *, kind=None, prompt_tokens=0, completion_tokens=0,
                    duration_s=None, units=None):
        """Cost of one usage row.

        Token kinds (rewrite/answer/stream/...) price per 1M tokens; the voice
        kinds meter non-token units — "stt" per minute of audio (duration_s),
        "tts" per 1M characters (units). Both are linear in their units, so
        per-row costs sum to the same aggregate cost as the pre-voice formula
        computed from totals.

        Keyword-only: a positional call would silently misprice a token row by
        landing in `kind`.
        """
        if kind == "stt":
            return (float(duration_s) if duration_s else 0.0) / 60.0 * rates["price_stt_per_min"]
        if kind == "tts":
            return (float(units) if units else 0.0) / 1_000_000 * rates["price_tts_per_m"]
        return (
            (prompt_tokens or 0) / 1_000_000 * rates["price_input_per_m"]
            + (completion_tokens or 0) / 1_000_000 * rates["price_output_per_m"]
        )

    def _usage_rows(self, range_days=None, category=None):
        where, params = [], []
        # Billing's question rows share this table but are not LLM calls:
        # including them would inflate the call count on the cost page and
        # count the same request twice, once as tokens and once as a question.
        where.append("kind <> 'question'")
        if range_days:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=range_days)).isoformat()
            where.append("at >= ?")
            params.append(cutoff)
        if category:
            where.append("category = ?")
            params.append(category)
        clause = " WHERE " + " AND ".join(where)
        return self.conn.execute(
            f"SELECT * FROM usage{clause} ORDER BY id", params
        ).fetchall()

    def usage_stats(self, range_days=None, category=None, top_users=DEFAULT_USAGE_TOP_USERS):
        """Aggregate usage and estimate cost. All aggregation in Python to
        stay robust against ISO-8601/offset variants in the stored timestamps.

        Cost is accumulated per row because a bucket (workspace, user, day)
        can mix token kinds with voice kinds, whose rates are not comparable:
        a bucket's cost is the sum of its rows' costs, each computed by kind.
        """
        rows = self._usage_rows(range_days=range_days, category=category)
        rates = self.get_rates()

        totals = {"calls": len(rows), "prompt_tokens": 0, "completion_tokens": 0,
                  "duration_s": 0.0, "units": 0.0,
                  "input_cost": 0.0, "output_cost": 0.0, "voice_cost": 0.0, "cost": 0.0,
                  "voice_seconds": 0.0, "voice_chars": 0.0}
        by_kind = defaultdict(lambda: {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                                       "duration_s": 0.0, "units": 0.0, "cost": 0.0})
        by_category = defaultdict(lambda: {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                                           "duration_s": 0.0, "units": 0.0, "cost": 0.0})
        by_user = defaultdict(lambda: {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                                       "duration_s": 0.0, "units": 0.0, "cost": 0.0})
        by_bucket = defaultdict(lambda: {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                                         "duration_s": 0.0, "units": 0.0, "cost": 0.0})
        hourly = range_days is not None and range_days <= 2

        def add(agg, pt, ct, dur, units, cost):
            agg["calls"] += 1
            agg["prompt_tokens"] += pt
            agg["completion_tokens"] += ct
            agg["duration_s"] += dur
            agg["units"] += units
            agg["cost"] += cost

        for r in rows:
            pt, ct = r["prompt_tokens"] or 0, r["completion_tokens"] or 0
            dur = r["duration_s"] or 0.0
            units = r["units"] or 0.0
            kind = r["kind"]
            cost = self._usage_cost(
                rates, kind=kind, prompt_tokens=pt, completion_tokens=ct,
                duration_s=dur, units=units,
            )
            totals["prompt_tokens"] += pt
            totals["completion_tokens"] += ct
            totals["duration_s"] += dur
            totals["units"] += units
            totals["cost"] += cost
            if kind == "stt":
                totals["voice_cost"] += cost
                totals["voice_seconds"] += dur
            elif kind == "tts":
                totals["voice_cost"] += cost
                totals["voice_chars"] += units
            else:
                totals["input_cost"] += pt / 1_000_000 * rates["price_input_per_m"]
                totals["output_cost"] += ct / 1_000_000 * rates["price_output_per_m"]
            add(by_kind[kind], pt, ct, dur, units, cost)
            cat = r["category"] or "(unknown)"
            add(by_category[cat], pt, ct, dur, units, cost)
            uid = r["user_id"] or "(anonymous)"
            add(by_user[uid], pt, ct, dur, units, cost)
            try:
                dt = datetime.fromisoformat(r["at"])
            except (TypeError, ValueError):
                dt = None
            if dt is not None:
                bucket = dt.strftime("%Y-%m-%d %H:00") if hourly else dt.strftime("%Y-%m-%d")
                add(by_bucket[bucket], pt, ct, dur, units, cost)

        totals["avg_tokens_per_call"] = (
            round((totals["prompt_tokens"] + totals["completion_tokens"]) / totals["calls"])
            if totals["calls"] else 0
        )
        totals["avg_cost_per_call"] = (
            totals["cost"] / totals["calls"] if totals["calls"] else 0.0
        )

        def deco(agg):
            return [{"key": k, **v} for k, v in agg.items()]

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
                {"bucket": k, **v}
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
                "units": r["units"],
                "category": r["category"],
                "user_id": r["user_id"],
                "session_id": r["session_id"],
                "request_id": r["request_id"],
            }
            for r in rows
        ]

    # ---- admin console sessions -------------------------------------------

    @staticmethod
    def _hash_session_token(token):
        return hashlib.sha256((token or "").encode("utf-8")).hexdigest()

    def create_admin_session(self, actor, ttl_seconds=43200):
        """Create a server-side admin session and return its raw token once.

        Only the SHA-256 hash is stored, so the token is unforgeable and a
        DB leak does not expose live credentials.
        """
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=int(ttl_seconds))
        with self._lock:
            self.conn.execute(
                "INSERT INTO admin_sessions (token_hash, actor, created_at, "
                "expires_at) VALUES (?, ?, ?, ?)",
                (self._hash_session_token(token), actor,
                 now.isoformat(), expires.isoformat()),
            )
            self.conn.commit()
        return {
            "token": token,
            "actor": actor,
            "created_at": now.isoformat(),
            "expires_at": expires.isoformat(),
        }

    def get_admin_session(self, token):
        """Return the session actor/expiry, or None when missing/expired."""
        if not token:
            return None
        token_hash = self._hash_session_token(token)
        with self._lock:
            row = self.conn.execute(
                "SELECT actor, created_at, expires_at FROM admin_sessions "
                "WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            try:
                expires = datetime.fromisoformat(row["expires_at"])
            except (TypeError, ValueError):
                expires = datetime.min.replace(tzinfo=timezone.utc)
            if expires <= datetime.now(timezone.utc):
                self.conn.execute(
                    "DELETE FROM admin_sessions WHERE token_hash = ?",
                    (token_hash,),
                )
                self.conn.commit()
                return None
            return {
                "actor": row["actor"],
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
            }

    def delete_admin_session(self, token):
        if not token:
            return False
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM admin_sessions WHERE token_hash = ?",
                (self._hash_session_token(token),),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def close(self):
        self.conn.close()