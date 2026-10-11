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
from datetime import datetime, timedelta, timezone

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

# --- D18: free-account verification and cap ---
# Q22 keeps free self-registration open, so the cost is unbounded unless we
# bound it. The agreed control is email verification (which removes the
# throwaway-address abuse case) plus a cap on how many free accounts may exist
# at once. The cap is generous because the direct cost of a fully-used free
# account is small; it exists to stop the tail, not to ration a scarce resource.
DEFAULT_MAX_FREE_ACCOUNTS = 500
VERIFICATION_TOKEN_TTL_HOURS = 48
# A resend must not become a way to mail-bomb an address, and re-minting the
# token invalidates the previous link, so the window is enforced per user.
VERIFICATION_RESEND_COOLDOWN_SECONDS = 60

# Password-reset links are more sensitive than verification links: they grant
# full control of an account rather than confirming an address, so they are
# short-lived. Two hours is enough for mail to arrive and a person to act, and
# short enough that a leaked inbox copy is unlikely to still be live.
PASSWORD_RESET_TOKEN_TTL_HOURS = 2
PASSWORD_RESET_PURPOSE = "password_reset"


def _token_hash(token):
    """Digest of a bearer token, for storing instead of the token itself."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _shift_iso(iso, seconds):
    """iso + a cooldown, for telling the frontend when a resend is allowed."""
    try:
        return (
            datetime.fromisoformat(iso) + timedelta(seconds=seconds)
        ).isoformat()
    except (TypeError, ValueError):
        return None


def max_free_accounts():
    """The free-account cap. 0 means closed, not unlimited.

    A cap that could be switched off by setting it to zero would make the
    worst possible configuration the easiest one to write, so zero is read as
    "stop self-registration" and anything negative is clamped to it. The
    default is generous; the value exists to bound the tail, not to ration a
    scarce resource.
    """
    raw = os.environ.get("MAX_FREE_ACCOUNTS", str(DEFAULT_MAX_FREE_ACCOUNTS))
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_MAX_FREE_ACCOUNTS


def verification_required():
    """Whether an unverified address may open the sample workspace.

    Defaults to on. `EMAIL_VERIFICATION_REQUIRED=0` exists so a deployment that
    has not finished SMTP can run the gate closed rather than not at all.
    """
    return os.environ.get("EMAIL_VERIFICATION_REQUIRED", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


# How many questions a free-tier account (the shared sample workspace) may ask
# before it has to choose a plan. This is the per-account twin of
# MAX_FREE_ACCOUNTS: that bounds how many throwaway accounts can exist, this
# bounds what any one of them can cost in model time. Ownerless by design, so
# the count is per user rather than per organisation.
DEFAULT_FREE_TIER_QUESTION_CAP = 50


def max_free_questions():
    """Questions a free-tier account gets before it is asked to upgrade.

    Read as "no limit" when unset, non-numeric, zero or negative: unlike the
    account cap, a free tier with no question ceiling is the sensible default
    for a deployment that has not decided a number yet, and an operator turns
    the gate off by setting it to 0 rather than by deleting the variable.
    """
    raw = os.environ.get("FREE_TIER_QUESTION_CAP", str(DEFAULT_FREE_TIER_QUESTION_CAP))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_FREE_TIER_QUESTION_CAP
    return value if value > 0 else 0

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
        "email_verified_at": row["email_verified_at"]
        if "email_verified_at" in keys
        else None,
        "account_status": row["account_status"]
        if "account_status" in keys
        else "active",
    }


def _with_verified_flag(user):
    """Expose verification as a boolean for the API layer.

    Absent on rows predating the column, hence None rather than False: None
    means "unknown, do not gate", False means "known unconfirmed, gate it".
    """
    if not user:
        return user
    user = dict(user)
    user["email_verified"] = (
        bool(user["email_verified_at"])
        if user.get("email_verified_at") is not None
        else None
    )
    return user


def _row_to_organisation(row):
    return {
        "id": row["id"],
        "name": row["name"],
        # Older rows predate the column and default to 0 = customer-owned, which
        # is the safe reading: a real customer must never inherit the guard.
        "system_owned": bool(row["system_owned"]) if "system_owned" in row.keys() else False,
        "created_at": row["created_at"],
        # Billing fields (lazy ALTER below). Absent for rows that predate the
        # columns, so each stays conditional rather than defaulting a made-up
        # state.
        "stripe_customer_id": row["stripe_customer_id"] if "stripe_customer_id" in row.keys() else None,
        "stripe_subscription_id": row["stripe_subscription_id"] if "stripe_subscription_id" in row.keys() else None,
        "plan": row["plan"] if "plan" in row.keys() else None,
        "billing_status": row["billing_status"] if "billing_status" in row.keys() else None,
        "current_period_end": row["current_period_end"] if "current_period_end" in row.keys() else None,
        "current_period_start": row["current_period_start"] if "current_period_start" in row.keys() else None,
        # Extra seats already reported to Stripe for `current_period_end`, so a
        # mid-period join sends the difference rather than the whole count again.
        "seats_reported": row["seats_reported"] if "seats_reported" in row.keys() else 0,
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
        if "email_verified_at" not in cols:
            # Added for D18. Existing accounts are grandfathered as verified at
            # their creation time: they already have access, and requiring
            # verification now would lock out accounts that were fine
            # yesterday. Only accounts created *after* this column exists are
            # left unverified, which is why the backfill runs in this branch
            # rather than on every start.
            self.conn.execute("ALTER TABLE users ADD COLUMN email_verified_at TEXT")
            self.conn.execute(
                "UPDATE users SET email_verified_at = COALESCE(created_at, ?) "
                "WHERE email_verified_at IS NULL",
                (_now(),),
            )
        if "verification_token_hash" not in cols:
            self.conn.execute("ALTER TABLE users ADD COLUMN verification_token_hash TEXT")
        if "verification_sent_at" not in cols:
            self.conn.execute("ALTER TABLE users ADD COLUMN verification_sent_at TEXT")
        if "password_reset_token_hash" not in cols:
            self.conn.execute("ALTER TABLE users ADD COLUMN password_reset_token_hash TEXT")
        if "password_reset_sent_at" not in cols:
            self.conn.execute("ALTER TABLE users ADD COLUMN password_reset_sent_at TEXT")
        if "account_status" not in cols:
            self.conn.execute(
                "ALTER TABLE users ADD COLUMN account_status TEXT NOT NULL DEFAULT 'active'"
            )
        # Session tokens are stateless, so the only way to invalidate the ones
        # already out there when a password changes is a value the token and the
        # row both carry. Tokens minted before this column existed have no
        # version claim and read as version 0, so grandfathering is automatic.
        if "token_version" not in cols:
            self.conn.execute(
                "ALTER TABLE users ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0"
            )
        org_cols = {
            r[1] for r in self.conn.execute("PRAGMA table_info(organisations)").fetchall()
        }
        # Billing columns, lazily added so a database written by an older build
        # opens untouched. `billing_status` deliberately has no default: an
        # organisation that was never billed should not quietly read as "active".
        for col in (
            "stripe_customer_id",
            "stripe_subscription_id",
            "plan",
            "billing_status",
            "current_period_end",
            "current_period_start",
        ):
            if col not in org_cols:
                self.conn.execute(
                    f"ALTER TABLE organisations ADD COLUMN {col} TEXT"
                )
        # Seats are metered per billing period, so what has already been
        # reported has to survive a restart; without it a second sync in the
        # same period would bill the same seats twice. Defaults to 0, which
        # reads as "nothing reported yet" - the safe direction, since the next
        # period's opening snapshot sets it straight either way.
        if "seats_reported" not in org_cols:
            self.conn.execute(
                "ALTER TABLE organisations ADD COLUMN seats_reported INTEGER "
                "NOT NULL DEFAULT 0"
            )
        # Webhook idempotency: Stripe may retry delivery and our own handler may
        # be invoked more than once, so each event id is recorded after it has
        # been applied and skip the second pass rather than double-apply.
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS billing_events ("
            "event_id TEXT PRIMARY KEY, "
            "type TEXT NOT NULL, "
            "organisation_id TEXT, "
            "at TEXT NOT NULL, "
            "detail TEXT)"
        )
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

    def get_organisation_by_stripe_customer(self, customer_id):
        """Resolve an organisation from a Stripe customer id.

        Webhook events identify the customer, so this is how an event reaches
        the organisation it belongs to when the event carries no metadata.
        """
        row = self.conn.execute(
            "SELECT * FROM organisations WHERE stripe_customer_id = ?",
            (customer_id,),
        ).fetchone()
        return _row_to_organisation(row) if row else None

    def set_organisation_billing(self, organisation_id, **fields):
        """Update billing fields on an organisation (customer, plan, status).

        Fields are whitelisted so a caller can never slip a column past schema
        changes. Returns the updated organisation or None if it does not exist.
        """
        allowed = {
            "stripe_customer_id",
            "stripe_subscription_id",
            "plan",
            "billing_status",
            "current_period_end",
            "current_period_start",
            "seats_reported",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return self.get_organisation(organisation_id)
        assignments = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values())
        cur = self.conn.execute(
            f"UPDATE organisations SET {assignments} WHERE id = ?",
            (*values, organisation_id),
        )
        self.conn.commit()
        if cur.rowcount == 0:
            return None
        return self.get_organisation(organisation_id)

    def has_billing_event(self, event_id):
        return self.conn.execute(
            "SELECT 1 FROM billing_events WHERE event_id = ?", (event_id,)
        ).fetchone() is not None

    def record_billing_event(self, event_id, event_type, organisation_id, detail):
        """Record a processed webhook event id so a retry is not re-applied."""
        self.conn.execute(
            "INSERT OR IGNORE INTO billing_events (event_id, type, "
            "organisation_id, at, detail) VALUES (?, ?, ?, ?, ?)",
            (event_id, event_type, organisation_id, _now(), (detail or "")[:2000]),
        )
        self.conn.commit()

    def list_organisations(self):
        rows = self.conn.execute(
            "SELECT * FROM organisations ORDER BY id"
        ).fetchall()
        return [_row_to_organisation(r) for r in rows]

    def organisation_workspace_categories(self, organisation_id):
        """Distinct workspaces the organisation has provisioned.

        The base fee is charged per workspace, so this is the quantity this
        organisation is billed for: one fee line per workspace its people can
        reach. Distinct memberships are the source because a user may hold
        several workspaces (Q7), and seats are counted per workspace (Q8).
        """
        rows = self.conn.execute(
            "SELECT DISTINCT m.category FROM workspace_memberships m "
            "JOIN users u ON u.id = m.user_id "
            "WHERE u.organisation_id = ? ORDER BY m.category",
            (organisation_id,),
        ).fetchall()
        return [r["category"] for r in rows]

    def organisation_workspace_seats(self, organisation_id):
        """Seats held per workspace inside one organisation.

        The seat allowance is counted per workspace (Q8) while the people
        belong to the organisation, so the two are joined here: only this
        organisation's users count towards a workspace's seats, and a workspace
        nobody in the organisation has joined is not provisioned at all (which
        is what organisation_workspace_categories returns).
        """
        rows = self.conn.execute(
            "SELECT m.category AS category, COUNT(*) AS seats "
            "FROM workspace_memberships m "
            "JOIN users u ON u.id = m.user_id "
            "WHERE u.organisation_id = ? "
            "GROUP BY m.category ORDER BY m.category",
            (organisation_id,),
        ).fetchall()
        return [dict(r) for r in rows]

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

    def ensure_initial_owner(self, organisation_id, user_id):
        """Make this account the organisation's owner when it has none yet.

        The first account a customer organisation gets is the one running it,
        however it arrived - created by an Administrator, or the first to
        redeem an invite. Q9 keeps owner singular, so this grants nothing once
        an owner exists: later accounts are plain members until that owner
        promotes them. Returns True when a role was granted.
        """
        org = self.get_organisation(organisation_id)
        if org is None or org.get("system_owned") or not user_id:
            return False
        if self.get(user_id) is None or self.org_role(user_id) is not None:
            return False
        for row in self.org_roles_for(organisation_id):
            if row["role"] == ORG_ROLE_OWNER:
                return False
        self.set_org_role(organisation_id, user_id, ORG_ROLE_OWNER)
        return True

    def backfill_org_roles(self):
        """Grant the owner each customer organisation was never given.

        Accounts created before roles were recorded landed in their
        organisation with no row in org_roles at all, so the owner badge, the
        role checks and Q9's single-owner rule all read an empty table. The
        earliest account of each organisation is the one that was running it.
        System organisations - the shared free tier and the legacy default -
        are platform-owned and are left alone. Idempotent; returns the number
        of owners granted.
        """
        granted = 0
        rows = self.conn.execute(
            "SELECT id FROM organisations WHERE system_owned = 0"
        ).fetchall()
        for row in rows:
            org_id = row["id"]
            if self.ensure_initial_owner(
                org_id,
                self._earliest_member(org_id),
            ):
                granted += 1
        return granted

    def _earliest_member(self, organisation_id):
        """The account that joined an organisation first, or None."""
        row = self.conn.execute(
            "SELECT id FROM users WHERE organisation_id = ? "
            "ORDER BY created_at, id LIMIT 1",
            (organisation_id,),
        ).fetchone()
        return row["id"] if row else None

    def revoke_org_role(self, organisation_id, user_id):
        """Remove an org-level role. Refuses to leave an organisation ownerless.

        Q9 makes ownership singular, and an organisation with no owner cannot be
        administered by anyone, so the last owner's role is not removable. The
        caller must transfer ownership first, which is a single store operation
        and never leaves the organisation with two owners or none.
        """
        if self.org_role(user_id) == ORG_ROLE_OWNER:
            remaining = [
                r for r in self.org_roles_for(organisation_id)
                if r["role"] == ORG_ROLE_OWNER and r["user_id"] != user_id
            ]
            if not remaining:
                raise ValueError(
                    "An organisation must keep exactly one owner. Transfer "
                    "ownership before revoking this role."
                )
        cur = self.conn.execute(
            "DELETE FROM org_roles WHERE organisation_id = ? AND user_id = ?",
            (organisation_id, user_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

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

    def create_user(self, email, password, name, category, organisation_id=None,
                    verified=False):
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
        verified_at = _now() if verified else None
        try:
            self.conn.execute(
                "INSERT INTO users (id, email, name, category, organisation_id, "
                "password_hash, salt, created_at, email_verified_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, email, name, category, org_id, digest, salt, _now(), verified_at),
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

    def reachable_workspaces(self, user_id, extra_categories=None):
        """Workspaces a user may reach, primary-first order preserved.

        A member reaches exactly the workspaces they hold. An organisation owner
        or admin additionally reaches every workspace in their organisation:
        they can already provision, grant and revoke workspaces, so leaving them
        unable to enter one they administer was a gap between authority and
        access. `extra_categories` carries workspaces the store cannot see on its
        own (those holding assigned content but no member seat); the caller adds
        them only for a manager.
        """
        cats = self.workspace_categories(user_id)
        seen = set(cats)
        user = self.get(user_id)
        if user and self.org_role(user_id) in (ORG_ROLE_OWNER, ORG_ROLE_ADMIN):
            org_id = user.get("organisation_id")
            if org_id:
                for cat in self.organisation_workspace_categories(org_id):
                    if cat not in seen:
                        seen.add(cat)
                        cats.append(cat)
        for cat in (extra_categories or ()):
            cat = (cat or "").strip().lower()
            if cat and cat not in seen:
                seen.add(cat)
                cats.append(cat)
        return cats

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

    def set_primary_workspace(self, user_id, category, extra_categories=None):
        """Make a reachable workspace the user's active one.

        Reachability, not mere membership, is the test: an organisation owner or
        admin may switch to any workspace in their organisation, including one
        they do not hold a seat in. Switching must not create a membership, or
        every workspace an owner visits would be billed as an extra seat.
        """
        cat = (category or "").strip().lower()
        if cat not in self.reachable_workspaces(user_id, extra_categories):
            raise ValueError("Workspace is not one this user may reach.")
        self.conn.execute(
            "UPDATE users SET category = ? WHERE id = ?", (cat, user_id)
        )
        self.conn.commit()
        return self.get(user_id)

    def get(self, user_id):
        row = self.conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return _with_verified_flag(_row_to_user(row)) if row else None

    def get_by_email(self, email):
        row = self.conn.execute(
            "SELECT * FROM users WHERE email = ?", ((email or "").strip().lower(),)
        ).fetchone()
        return _with_verified_flag(_row_to_user(row)) if row else None

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

    def verify_password(self, user_id, password):
        """Check a password against one account, without signing anyone in."""
        row = self.conn.execute(
            "SELECT salt, password_hash FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not row:
            return False
        _, digest = _hash_password(password or "", salt=bytes(row["salt"]))
        return hmac.compare_digest(digest, bytes(row["password_hash"]))

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

    # ---- email verification (D18) -----------------------------------------

    def is_verified(self, user_id):
        row = self.conn.execute(
            "SELECT email_verified_at FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return bool(row and row[0])

    def issue_verification_token(self, user_id, ttl_hours=None):
        """Mint a verification token and remember only its hash.

        The plaintext is returned once, for the email body. Storing a digest
        rather than the token means a copy of the database cannot be used to
        verify anyone's address; a lost link is recoverable by asking for a
        fresh one, which overwrites this row anyway.
        """
        hours = ttl_hours or int(
            os.environ.get("VERIFICATION_TOKEN_TTL_HOURS", VERIFICATION_TOKEN_TTL_HOURS)
        )
        token = sign_jwt(
            self.secret,
            {
                "sub": user_id,
                "purpose": "email_verification",
                # sign_jwt stamps whole seconds, so two tokens minted in the
                # same second would be byte-identical. That matters here:
                # reissuing is meant to invalidate the previous link, and
                # without a nonce it would silently return the same one.
                "jti": secrets.token_urlsafe(9),
            },
            ttl_seconds=hours * 3600,
        )
        self.conn.execute(
            "UPDATE users SET verification_token_hash = ?, verification_sent_at = ? "
            "WHERE id = ?",
            (_token_hash(token), _now(), user_id),
        )
        self.conn.commit()
        return token

    def verify_email(self, token):
        """Consume a verification token.

        Returns the user id, or raises ValueError with a message the caller can
        surface. Expiry and signature are checked before the token hash is
        compared, and the hash is cleared on success so a link works once.
        """
        payload = verify_jwt(self.secret, token or "")
        if not payload or payload.get("purpose") != "email_verification":
            raise ValueError("That verification link is not valid.")
        user_id = payload.get("sub")
        row = self.conn.execute(
            "SELECT verification_token_hash, email_verified_at FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            raise ValueError("That verification link is not valid.")
        if row["email_verified_at"]:
            raise ValueError("That address is already verified.")
        if not row["verification_token_hash"]:
            raise ValueError("That verification link has been replaced. Request a new one.")
        if not hmac.compare_digest(row["verification_token_hash"], _token_hash(token)):
            raise ValueError("That verification link is not valid.")
        self.conn.execute(
            "UPDATE users SET email_verified_at = ?, verification_token_hash = NULL "
            "WHERE id = ?",
            (_now(), user_id),
        )
        self.conn.commit()
        return user_id

    def verification_status(self, user_id):
        """What the frontend needs to explain a blocked account."""
        row = self.conn.execute(
            "SELECT email_verified_at, verification_sent_at FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            return None
        verified_at, sent_at = row[0], row[1]
        resend_after = None
        if sent_at:
            resend_after = _shift_iso(sent_at, VERIFICATION_RESEND_COOLDOWN_SECONDS)
        return {
            "verified": bool(verified_at),
            "verified_at": verified_at,
            "sent_at": sent_at,
            # Surfaced rather than enforced in the UI, so a user who waits past
            # the cooldown is not told to email support.
            "resend_available_at": resend_after,
        }

    def set_verified(self, user_id, verified=True):
        """Administrative override, for a user who cannot receive mail."""
        self.conn.execute(
            "UPDATE users SET email_verified_at = ?, verification_token_hash = NULL "
            "WHERE id = ?",
            (_now() if verified else None, user_id),
        )
        self.conn.commit()
        return self.get(user_id)

    # ---- password reset ----------------------------------------------------

    def token_version(self, user_id):
        row = self.conn.execute(
            "SELECT token_version FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def _bump_token_version(self, user_id):
        """Invalidate every session token already issued for this account.

        Called whenever a password is set, because a password change is the
        moment a person expects the sessions they do not control to stop
        working. The row's version moves and every old token's claim no longer
        matches, so `require_user` turns them away on their next request.
        """
        self.conn.execute(
            "UPDATE users SET token_version = token_version + 1 WHERE id = ?",
            (user_id,),
        )

    def issue_password_reset_token(self, user_id, ttl_hours=None):
        """Mint a single-use reset token and remember only its hash.

        Same shape as a verification token - signed, purpose-tagged, stored as
        a digest - so a copied database cannot be used to reset anyone's
        password. Reissuing overwrites the previous hash, so only the newest
        link works.
        """
        user = self.get(user_id)
        if user is None:
            return None
        hours = ttl_hours or int(
            os.environ.get("PASSWORD_RESET_TOKEN_TTL_HOURS", PASSWORD_RESET_TOKEN_TTL_HOURS)
        )
        token = sign_jwt(
            self.secret,
            {
                "sub": user_id,
                "purpose": PASSWORD_RESET_PURPOSE,
                "jti": secrets.token_urlsafe(9),
            },
            ttl_seconds=hours * 3600,
        )
        self.conn.execute(
            "UPDATE users SET password_reset_token_hash = ?, password_reset_sent_at = ? "
            "WHERE id = ?",
            (_token_hash(token), _now(), user_id),
        )
        self.conn.commit()
        return token

    def reset_password(self, token, new_password):
        """Consume a reset token and set a new password.

        Returns the user id. Raises ValueError with a message for the caller.
        The old token is cleared on success so the link works once, and the
        session version is bumped so every previous sign-in is signed out.
        """
        if len(new_password or "") < MIN_PASSWORD_LENGTH:
            raise ValueError(
                f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
            )
        payload = verify_jwt(self.secret, token or "")
        if not payload or payload.get("purpose") != PASSWORD_RESET_PURPOSE:
            raise ValueError("That password reset link is not valid.")
        user_id = payload.get("sub")
        row = self.conn.execute(
            "SELECT password_reset_token_hash FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row is None or not row["password_reset_token_hash"]:
            raise ValueError("That password reset link has been replaced. Request a new one.")
        if not hmac.compare_digest(row["password_reset_token_hash"], _token_hash(token)):
            raise ValueError("That password reset link is not valid.")
        salt, digest = _hash_password(new_password)
        self.conn.execute(
            "UPDATE users SET salt = ?, password_hash = ?, password_reset_token_hash = NULL "
            "WHERE id = ?",
            (salt, digest, user_id),
        )
        self._bump_token_version(user_id)
        self.conn.commit()
        return user_id

    def change_password(self, user_id, current_password, new_password):
        """Self-service change: verify the old password, set and re-hash.

        Bumping the session version signs out every device, including the one
        making the change, so the caller is handed a fresh token afterwards.
        """
        if not self.verify_password(user_id, current_password):
            raise ValueError("Your current password is not correct.")
        if len(new_password or "") < MIN_PASSWORD_LENGTH:
            raise ValueError(
                f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
            )
        salt, digest = _hash_password(new_password)
        self.conn.execute(
            "UPDATE users SET salt = ?, password_hash = ?, password_reset_token_hash = NULL "
            "WHERE id = ?",
            (salt, digest, user_id),
        )
        self._bump_token_version(user_id)
        self.conn.commit()
        return self.get(user_id)

    def list_unverified(self, organisation_id=None, limit=100, offset=0):
        """Unverified accounts for the admin console, oldest first."""
        sql = (
            "SELECT id, email, name, organisation_id, created_at, verification_sent_at "
            "FROM users WHERE email_verified_at IS NULL"
        )
        params = []
        if organisation_id:
            sql += " AND organisation_id = ?"
            params.append(organisation_id)
        sql += " ORDER BY created_at ASC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def count_unverified(self, organisation_id=None):
        sql = "SELECT COUNT(*) FROM users WHERE email_verified_at IS NULL"
        params = []
        if organisation_id:
            sql += " AND organisation_id = ?"
            params.append(organisation_id)
        return self.conn.execute(sql, params).fetchone()[0]

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

    def update_user(self, user_id, name=None, category=None, password=None,
                    account_status=None):
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
            self._bump_token_version(user_id)
        else:
            self.conn.execute(
                "UPDATE users SET name=?, category=? WHERE id=?",
                (new_name, new_cat, user_id),
            )
        if account_status is not None:
            status = (account_status or "").strip().lower()
            if status not in ("active", "suspended"):
                raise ValueError("account_status must be 'active' or 'suspended'")
            self.conn.execute(
                "UPDATE users SET account_status=? WHERE id=?",
                (status, user_id),
            )
            if status == "suspended":
                self._bump_token_version(user_id)
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
                # Session generation. Bumped whenever the password changes, so
                # a stolen or shared token stops working the moment the owner
                # resets. Absent on tokens minted before this existed, which
                # read as 0 - the same value an untouched account has.
                "v": self.token_version(user["id"]),
                # Stamped explicitly so the download endpoint can refuse a session
                # token. Tokens issued before this claim existed have no purpose
                # and are still accepted as sessions.
                "purpose": SESSION_PURPOSE,
            },
            ttl_seconds=ttl,
        )

    def close(self):
        self.conn.close()