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
# Token families share one signing scheme, so they must be distinguishable by
# their claims. `document_download` lives in chatbot.download_links; it is named
# there because only that module mints one.
SESSION_PURPOSE = "session"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# The organisation every self-registered free user sits in. Owned by the system
# rather than by a customer, and shared by all free accounts, so it is the one
# place where content is deliberately pooled across unrelated parties. The id
# doubles as the data directory name (data/organisations/_sample/), which is why
# it is underscore-prefixed like SHARED_DIR_NAME.
SAMPLE_ORGANISATION_ID = "_sample"
SAMPLE_ORGANISATION_NAME = "Sample"

# Where accounts land when no organisation is specified and the caller is not the
# free self-service path. Also the backfill target for rows written before
# organisations existed (Phase 3 migration).
DEFAULT_ORGANISATION_ID = "_default"
DEFAULT_ORGANISATION_NAME = "Default"

# Membership role within one workspace. This is deliberately NOT the Section 3.3
# role set: org-level roles (owner/admin) belong to the organisation, and the
# only two that attach to a single workspace are these. Q10 asks whether the
# workspace admin role is wanted at all, so keeping the vocabulary minimal
# avoids baking in an answer the client has not given.
DEFAULT_MEMBERSHIP_ROLE = "member"
MEMBERSHIP_ROLES = frozenset({"member"})

# Org-level roles from Section 3.3. Workspace Admin was removed (Q10), and Owner
# is singular per organisation (Q9), enforced by set_org_role rather than by the
# schema, because a partial unique index cannot be used portably here.
ORG_ROLE_OWNER = "owner"
ORG_ROLE_ADMIN = "admin"
ORG_ROLES = frozenset({ORG_ROLE_OWNER, ORG_ROLE_ADMIN})

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
    keys = row.keys()
    return {
        "id": row["id"],
        "email": row["email"],
        "name": row["name"],
        "category": row["category"],
        "organisation_id": row["organisation_id"] if "organisation_id" in keys else None,
        "created_at": row["created_at"],
        "last_login": row["last_login"] if "last_login" in keys else None,
    }


def _row_to_organisation(row):
    return {
        "id": row["id"],
        "name": row["name"],
        # Older rows predate the column and default to 0 = customer-owned, which
        # is the safe reading: a real customer must never inherit the guard.
        "system_owned": bool(row["system_owned"]) if "system_owned" in row.keys() else False,
        "created_at": row["created_at"],
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
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS organisations ("
            "id TEXT PRIMARY KEY, "
            "name TEXT NOT NULL, "
            "system_owned INTEGER NOT NULL DEFAULT 0, "
            "created_at TEXT NOT NULL)"
        )
        # Workspace access is many-to-many (Q7: a user may hold several
        # workspaces inside their organisation), so it needs its own table
        # rather than a column on users. The unique pair makes a repeated grant
        # idempotent, and ON DELETE CASCADE is what keeps a deleted user or
        # workspace from leaving a membership that grants access to nothing.
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS workspace_memberships ("
            "user_id TEXT NOT NULL, "
            "category TEXT NOT NULL, "
            "role TEXT NOT NULL DEFAULT 'member', "
            "created_at TEXT NOT NULL, "
            "PRIMARY KEY (user_id, category), "
            "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE)"
        )
        # Org-level roles, scoped to the organisation rather than a workspace.
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS org_roles ("
            "organisation_id TEXT NOT NULL, "
            "user_id TEXT NOT NULL, "
            "role TEXT NOT NULL, "
            "created_at TEXT NOT NULL, "
            "PRIMARY KEY (organisation_id, user_id))"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_org_roles_user ON org_roles(user_id)"
        )
        self.conn.commit()
        self._ensure_schema()

    def _ensure_schema(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(users)").fetchall()}
        if "last_login" not in cols:
            self.conn.execute("ALTER TABLE users ADD COLUMN last_login TEXT")
        if "organisation_id" not in cols:
            # Lazy ALTER so a users table written by an older build opens
            # untouched, then backfill so no row is left organisation-less.
            self.conn.execute("ALTER TABLE users ADD COLUMN organisation_id TEXT")
        self.conn.commit()
        self._ensure_system_organisations()
        self._ensure_memberships()

    def _ensure_memberships(self):
        """Give every user a membership row for the workspace they already had.

        `users.category` predates the membership table and is still the primary
        workspace, so it is the authoritative backfill source. OR IGNORE keeps
        this idempotent across restarts and leaves real grants untouched.
        """
        self.conn.execute(
            "INSERT OR IGNORE INTO workspace_memberships (user_id, category, role, created_at) "
            "SELECT id, category, 'member', COALESCE(created_at, ?) FROM users",
            (_now(),),
        )
        self.conn.commit()

    def _ensure_system_organisations(self):
        """Create the two system-owned organisations if absent.

        `_sample` is the shared free-tier organisation and `_default` is the
        backfill target for pre-organisation accounts. Both are system-owned, so
        the merge guard in `merge_guard` covers them by construction rather than
        by name matching.
        """
        for org_id, name in (
            (SAMPLE_ORGANISATION_ID, SAMPLE_ORGANISATION_NAME),
            (DEFAULT_ORGANISATION_ID, DEFAULT_ORGANISATION_NAME),
        ):
            self.conn.execute(
                "INSERT OR IGNORE INTO organisations (id, name, system_owned, created_at) "
                "VALUES (?, ?, 1, ?)",
                (org_id, name, _now()),
            )
        self.conn.commit()
        self.conn.execute(
            "UPDATE users SET organisation_id = ? WHERE organisation_id IS NULL",
            (DEFAULT_ORGANISATION_ID,),
        )
        self.conn.commit()

    # ---- organisations -----------------------------------------------------

    def get_organisation(self, organisation_id):
        row = self.conn.execute(
            "SELECT * FROM organisations WHERE id = ?", (organisation_id,)
        ).fetchone()
        return _row_to_organisation(row) if row else None

    def list_organisations(self):
        rows = self.conn.execute(
            "SELECT * FROM organisations ORDER BY id"
        ).fetchall()
        return [_row_to_organisation(r) for r in rows]

    def create_organisation(self, organisation_id, name):
        """Create a customer organisation. Refuses the reserved system ids.

        No owner argument: an organisation is created empty, and ownership is
        granted afterwards via set_org_role once a user has actually been placed
        in it. Passing an owner here could only be a user who is not yet a member,
        which set_org_role rejects.
        """
        org_id = (organisation_id or "").strip()
        label = (name or "").strip() or org_id.title()
        if not org_id:
            raise ValueError("An organisation id is required.")
        if org_id in (SAMPLE_ORGANISATION_ID, DEFAULT_ORGANISATION_ID):
            raise ValueError(f"'{org_id}' is reserved for system use.")
        try:
            self.conn.execute(
                "INSERT INTO organisations (id, name, system_owned, created_at) "
                "VALUES (?, ?, 0, ?)",
                (org_id, label, _now()),
            )
            self.conn.commit()
        except sqlite3.IntegrityError:
            raise ValueError("An organisation with that id already exists.")
        return self.get_organisation(org_id)

    # ---- organisation roles (Section 3.3) ----------------------------------
    #
    # Org-level roles live here rather than on workspace_memberships, because
    # their scope is the whole organisation. Workspace Admin is gone (Q10), so
    # this table plus memberships covers the entire customer role model.

    def org_role(self, user_id):
        row = self.conn.execute(
            "SELECT role FROM org_roles WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row["role"] if row else None

    def org_roles_for(self, organisation_id):
        rows = self.conn.execute(
            "SELECT o.user_id, o.role, o.created_at, u.email, u.name "
            "FROM org_roles o JOIN users u ON u.id = o.user_id "
            "WHERE o.organisation_id = ? ORDER BY o.role DESC, u.email",
            (organisation_id,),
        ).fetchall()
        return [
            {
                "user_id": r["user_id"],
                "email": r["email"],
                "name": r["name"],
                "role": r["role"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def set_org_role(self, organisation_id, user_id, role):
        """Grant an org-level role. Owner is singular (Q9), so a second grant
        is refused rather than silently creating two owners."""
        role = (role or "").strip().lower()
        if role not in ORG_ROLES:
            raise ValueError(f"Role must be one of: {', '.join(sorted(ORG_ROLES))}.")
        if self.get_organisation(organisation_id) is None:
            raise ValueError(f"Unknown organisation: {organisation_id}")
        user = self.get(user_id)
        if user is None:
            raise ValueError("Unknown user.")
        if user["organisation_id"] != organisation_id:
            raise ValueError("User does not belong to that organisation.")
        if role == ORG_ROLE_OWNER and self.org_role(user_id) != ORG_ROLE_OWNER:
            current = self.conn.execute(
                "SELECT user_id FROM org_roles WHERE organisation_id = ? AND role = ?",
                (organisation_id, ORG_ROLE_OWNER),
            ).fetchone()
            if current is not None:
                raise ValueError(
                    f"'{organisation_id}' already has an owner. Transfer ownership "
                    "instead of granting a second one."
                )
        self.conn.execute(
            "INSERT INTO org_roles (organisation_id, user_id, role, created_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(organisation_id, user_id) DO UPDATE SET role=excluded.role",
            (organisation_id, user_id, role, _now()),
        )
        self.conn.commit()
        return role

    def transfer_ownership(self, organisation_id, from_user_id, to_user_id):
        """Move the single owner seat (Q9). One call, so there is never a moment
        with two owners or none."""
        if self.org_role(from_user_id) != ORG_ROLE_OWNER:
            raise ValueError("That user is not the owner of this organisation.")
        if self.get(to_user_id) is None:
            raise ValueError("Unknown user.")
        if self.get(to_user_id)["organisation_id"] != organisation_id:
            raise ValueError("User does not belong to that organisation.")
        self.conn.execute(
            "UPDATE org_roles SET role = ? WHERE organisation_id = ? AND user_id = ?",
            (ORG_ROLE_ADMIN, organisation_id, from_user_id),
        )
        self.conn.execute(
            "INSERT INTO org_roles (organisation_id, user_id, role, created_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(organisation_id, user_id) DO UPDATE SET role=excluded.role",
            (organisation_id, to_user_id, ORG_ROLE_OWNER, _now()),
        )
        self.conn.commit()
        return self.org_roles_for(organisation_id)

    def can_manage_workspaces(self, user_id):
        """Q11: organisation admins and platform administrators may create and
        remove workspaces. Platform administrators are identified by the caller
        passing platform_admin=True, since they are not in this table."""
        return self.org_role(user_id) in (ORG_ROLE_OWNER, ORG_ROLE_ADMIN)

    def delete_organisation(self, organisation_id):
        """Delete an organisation and its memberships and roles (Q12).

        The caller is responsible for the confirmation step and for removing the
        organisation's materialised content; this only clears the identity rows.
        """
        org = self.get_organisation(organisation_id)
        if org is None:
            return False
        if org["system_owned"]:
            raise ValueError(f"'{organisation_id}' is system-owned and cannot be deleted.")
        self.conn.execute("DELETE FROM org_roles WHERE organisation_id = ?", (organisation_id,))
        self.conn.execute(
            "DELETE FROM workspace_memberships WHERE user_id IN "
            "(SELECT id FROM users WHERE organisation_id = ?)",
            (organisation_id,),
        )
        self.conn.execute("DELETE FROM users WHERE organisation_id = ?", (organisation_id,))
        self.conn.execute("DELETE FROM organisations WHERE id = ?", (organisation_id,))
        self.conn.commit()
        return True

    def merge_guard(self, target_organisation_id):
        """Raise unless `target_organisation_id` may receive customer data.

        Section 3.5 of the proposal: a system-owned organisation is the one place
        where a user could be served content assigned to a different party, so it
        is the one place a merge must be rejected. Enforced here rather than in a
        caller so no future merge path can forget the check.
        """
        org = self.get_organisation(target_organisation_id)
        if org is None:
            raise ValueError(f"Unknown organisation: {target_organisation_id}")
        if org["system_owned"]:
            raise ValueError(
                f"'{target_organisation_id}' is system-owned and cannot receive "
                "customer organisations or paying accounts."
            )
        return org

    def set_user_organisation(self, user_id, organisation_id):
        """Move a user into another organisation, honouring the merge guard.

        Clears their org-level role on the way out. Content is organisation-scoped,
        so a role left behind would let a former owner keep administering the
        organisation they just left, and org_roles_for() would keep listing them
        among its members. Workspace memberships survive the move: they name
        workspaces, not organisations, and are re-pointed by the caller if the
        workspaces the new organisation uses differ.
        """
        self.merge_guard(organisation_id)
        cur = self.conn.execute(
            "UPDATE users SET organisation_id = ? WHERE id = ?",
            (organisation_id, user_id),
        )
        if cur.rowcount:
            self.conn.execute(
                "DELETE FROM org_roles WHERE user_id = ? AND organisation_id != ?",
                (user_id, organisation_id),
            )
        self.conn.commit()
        return cur.rowcount > 0

    def categories(self):
        return workspace_names()

    def create_user(self, email, password, name, category, organisation_id=None):
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
        # Unspecified means the free self-service path (Section 3.5): everyone
        # lands in the shared sample organisation with the curated pack.
        org_id = (organisation_id or "").strip() or SAMPLE_ORGANISATION_ID
        if self.get_organisation(org_id) is None:
            raise ValueError(f"Unknown organisation: {org_id}")
        salt, digest = _hash_password(password)
        user_id = secrets.token_hex(16)
        try:
            self.conn.execute(
                "INSERT INTO users (id, email, name, category, organisation_id, "
                "password_hash, salt, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, email, name, category, org_id, digest, salt, _now()),
            )
            self.conn.commit()
        except sqlite3.IntegrityError:
            raise ValueError("An account with that email already exists.")
        # The signup category is the user's first workspace, so it gets a
        # membership row in the same transaction. Without this a brand-new user
        # would have no workspaces until something backfilled them.
        self.conn.execute(
            "INSERT OR IGNORE INTO workspace_memberships "
            "(user_id, category, role, created_at) VALUES (?, ?, 'member', ?)",
            (user_id, category, _now()),
        )
        self.conn.commit()
        return self.get(user_id)

    # ---- workspace memberships ---------------------------------------------

    def memberships(self, user_id):
        """Workspaces this user may reach, primary workspace first."""
        rows = self.conn.execute(
            "SELECT m.category, m.role, m.created_at, "
            "(u.category = m.category) AS is_primary "
            "FROM workspace_memberships m JOIN users u ON u.id = m.user_id "
            "WHERE m.user_id = ? "
            "ORDER BY is_primary DESC, m.category ASC",
            (user_id,),
        ).fetchall()
        return [
            {
                "category": r["category"],
                "role": r["role"],
                "created_at": r["created_at"],
                "is_primary": bool(r["is_primary"]),
            }
            for r in rows
        ]

    def workspace_categories(self, user_id):
        return [m["category"] for m in self.memberships(user_id)]

    def has_workspace(self, user_id, category):
        return self.conn.execute(
            "SELECT 1 FROM workspace_memberships WHERE user_id = ? AND category = ?",
            (user_id, category),
        ).fetchone() is not None

    def add_workspace(self, user_id, category, role=DEFAULT_MEMBERSHIP_ROLE):
        """Grant a user a workspace. Idempotent on (user_id, category).

        `role` is retained in the signature but has one legal value since Q10
        removed Workspace Admin. It stays as a parameter so a future role does
        not require changing every caller.
        """
        cat = (category or "").strip().lower()
        if cat not in self.categories():
            raise ValueError(f"Category must be one of: {', '.join(self.categories())}.")
        if self.get(user_id) is None:
            raise ValueError("Unknown user.")
        role = (role or "").strip().lower() or DEFAULT_MEMBERSHIP_ROLE
        if role not in MEMBERSHIP_ROLES:
            raise ValueError(f"Role must be one of: {', '.join(sorted(MEMBERSHIP_ROLES))}.")
        self.conn.execute(
            "INSERT OR IGNORE INTO workspace_memberships "
            "(user_id, category, role, created_at) VALUES (?, ?, ?, ?)",
            (user_id, cat, role, _now()),
        )
        self.conn.commit()
        return self.memberships(user_id)

    def remove_workspace(self, user_id, category):
        """Revoke a workspace, refusing to strand the user with none.

        A user with zero workspaces has no reachable content, so the removal is
        rejected rather than silently leaving a broken account. Reassignment
        (changing the primary workspace first) is the supported way out.
        """
        cat = (category or "").strip().lower()
        remaining = [c for c in self.workspace_categories(user_id) if c != cat]
        if not remaining:
            raise ValueError(
                "Cannot remove the only workspace. Reassign the user first."
            )
        cur = self.conn.execute(
            "DELETE FROM workspace_memberships WHERE user_id = ? AND category = ?",
            (user_id, cat),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def set_primary_workspace(self, user_id, category):
        """Make an existing workspace primary. Membership is required first."""
        cat = (category or "").strip().lower()
        if not self.has_workspace(user_id, cat):
            raise ValueError(f"User has no membership in workspace '{cat}'.")
        self.conn.execute("UPDATE users SET category = ? WHERE id = ?", (cat, user_id))
        self.conn.commit()
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
            f"SELECT id, email, name, category, organisation_id, created_at, "
            f"last_login FROM users {where} "
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

    def count_users_in_organisation(self, organisation_id):
        return self.conn.execute(
            "SELECT COUNT(*) FROM users WHERE organisation_id = ?",
            (organisation_id,),
        ).fetchone()[0]

    def count_for_category(self, category):
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM users WHERE category = ?", (category,)
        )
        return cur.fetchone()[0]

    def count_for_organisation(self, organisation_id):
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM users WHERE organisation_id = ?", (organisation_id,)
        )
        return cur.fetchone()[0]

    def list_users_in_organisation(self, organisation_id, limit=100, offset=0):
        rows = self.conn.execute(
            "SELECT * FROM users WHERE organisation_id = ? "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (organisation_id, limit, offset),
        ).fetchall()
        return [_row_to_user(r) for r in rows]

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
        # Changing the category changes the primary workspace, so a membership
        # must exist for it. Without this the user would point at a workspace
        # they cannot reach, and the switcher would list it as primary.
        if category is not None and new_cat != existing["category"]:
            if not self.has_workspace(user_id, new_cat):
                self.conn.execute(
                    "INSERT OR IGNORE INTO workspace_memberships "
                    "(user_id, category, role, created_at) VALUES (?, ?, 'member', ?)",
                    (user_id, new_cat, _now()),
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
        # Explicit child delete rather than relying on ON DELETE CASCADE, which
        # SQLite ignores unless foreign_keys is turned on for the connection.
        self.conn.execute(
            "DELETE FROM workspace_memberships WHERE user_id = ?", (user_id,)
        )
        self.conn.execute("DELETE FROM org_roles WHERE user_id = ?", (user_id,))
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
                # Optional: tokens minted before organisations existed carry no
                # organisation claim, and remain valid until they expire.
                "organisation_id": user.get("organisation_id"),
                # May be several (Q7). The frontend shows a switcher when the
                # list has more than one entry and nothing when it has exactly
                # one, so an empty or absent list is a valid state to render.
                # Read from the store rather than the passed row, because a row
                # from get() carries no memberships.
                "workspaces": self.workspace_categories(user["id"]),
                # Stamped explicitly so the download endpoint can refuse a session
                # token. Tokens issued before this claim existed have no purpose
                # and are still accepted as sessions.
                "purpose": SESSION_PURPOSE,
            },
            ttl_seconds=ttl,
        )

    def close(self):
        self.conn.close()