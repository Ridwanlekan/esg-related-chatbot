"""D18: free-account verification and cap.

Q22 keeps free self-registration open, which leaves the free tier's model spend
unbounded. The agreed control is email verification plus a cap on the number of
free accounts. These tests pin the behaviour that makes the control real: the
cap refuses signups without breaking paid or invited paths, the gate blocks
model time but not recovery, tokens are single-use, and existing accounts are
not locked out by the arrival of verification.
"""

import os

import pytest
from fastapi.testclient import TestClient

from chatbot.admin_store import AdminStore
from chatbot.api import create_app
from chatbot.session_store import SessionStore
from chatbot.users import UserStore

KEY = "secret-admin-key"


def admin_auth():
    return {"authorization": f"Bearer {KEY}"}


def bearer(token):
    return {"authorization": f"Bearer {token}"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("INDEX_DIR", str(tmp_path / "index"))
    monkeypatch.setenv("EMAIL_VERIFICATION_REQUIRED", "1")
    app = create_app(
        session_store=SessionStore(db_path=str(tmp_path / "s.sqlite3")),
        user_store=UserStore(db_path=str(tmp_path / "u.sqlite3"), secret="test-secret"),
        admin_store=AdminStore(str(tmp_path / "a.sqlite3")),
        api_key=KEY,
        rate_limit=0,
    )
    return TestClient(app), tmp_path


def signup(c, email="user@example.com", category="finance", invite=None):
    payload = {
        "email": email,
        "password": "password123",
        "name": "Test User",
        "category": category,
    }
    if invite:
        payload["invite"] = invite
    return c.post("/auth/signup", json=payload)


# ---- the store, in isolation from HTTP ------------------------------------


class TestVerificationTokens:
    def test_new_account_is_unverified(self):
        store = UserStore(db_path=":memory:", secret="s")
        user = store.create_user("a@example.com", "password123", "A", "finance")
        assert user["email_verified_at"] is None
        assert store.is_verified(user["id"]) is False

    def test_verified_flag_marks_account_at_creation(self):
        store = UserStore(db_path=":memory:", secret="s")
        user = store.create_user(
            "a@example.com", "password123", "A", "finance", verified=True
        )
        assert store.is_verified(user["id"]) is True

    def test_verify_email_confirms_and_consumes(self):
        store = UserStore(db_path=":memory:", secret="s")
        user = store.create_user("a@example.com", "password123", "A", "finance")
        token = store.issue_verification_token(user["id"])
        assert store.verify_email(token) == user["id"]
        assert store.is_verified(user["id"]) is True
        # A link works once: a leaked, already-used token is worthless.
        with pytest.raises(ValueError):
            store.verify_email(token)

    def test_only_a_digest_is_stored(self):
        store = UserStore(db_path=":memory:", secret="s")
        user = store.create_user("a@example.com", "password123", "A", "finance")
        token = store.issue_verification_token(user["id"])
        row = store.conn.execute(
            "SELECT verification_token_hash FROM users WHERE id = ?", (user["id"],)
        ).fetchone()
        assert row[0] and row[0] != token

    def test_reissuing_invalidates_the_previous_token(self):
        store = UserStore(db_path=":memory:", secret="s")
        user = store.create_user("a@example.com", "password123", "A", "finance")
        first = store.issue_verification_token(user["id"])
        store.issue_verification_token(user["id"])
        with pytest.raises(ValueError):
            store.verify_email(first)

    def test_session_token_is_not_a_verification_token(self):
        """Purpose separation, or any session token would verify an address."""
        store = UserStore(db_path=":memory:", secret="s")
        user = store.create_user("a@example.com", "password123", "A", "finance")
        session = store.token_for(user)
        with pytest.raises(ValueError):
            store.verify_email(session)

    def test_foreign_secret_is_rejected(self):
        store = UserStore(db_path=":memory:", secret="s" * 40)
        user = store.create_user("a@example.com", "password123", "A", "finance")
        token = store.issue_verification_token(user["id"])
        other = UserStore(db_path=":memory:", secret="t" * 40)
        with pytest.raises(ValueError):
            other.verify_email(token)

    def test_expired_token_is_rejected(self):
        store = UserStore(db_path=":memory:", secret="s")
        user = store.create_user("a@example.com", "password123", "A", "finance")
        token = store.issue_verification_token(user["id"], ttl_hours=-1)
        with pytest.raises(ValueError):
            store.verify_email(token)

    def test_garbage_token_is_rejected(self):
        store = UserStore(db_path=":memory:", secret="s")
        for bad in ("", "not-a-token", "a.b.c"):
            with pytest.raises(ValueError):
                store.verify_email(bad)

    def test_admin_can_override_verification(self):
        store = UserStore(db_path=":memory:", secret="s")
        user = store.create_user("a@example.com", "password123", "A", "finance")
        store.set_verified(user["id"])
        assert store.is_verified(user["id"]) is True
        store.set_verified(user["id"], verified=False)
        assert store.is_verified(user["id"]) is False

    def test_existing_rows_are_grandfathered_on_upgrade(self, tmp_path):
        """An older database opens with its accounts already confirmed."""
        import sqlite3

        path = str(tmp_path / "legacy.sqlite3")
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, "
            "name TEXT NOT NULL, category TEXT NOT NULL, password_hash BLOB NOT NULL, "
            "salt BLOB NOT NULL, created_at TEXT NOT NULL, last_login TEXT)"
        )
        conn.execute(
            "CREATE TABLE organisations (id TEXT PRIMARY KEY, name TEXT NOT NULL, "
            "system_owned INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO users VALUES ('u1', 'old@example.com', 'Old', 'finance', "
            "x'00', x'00', '2020-01-01T00:00:00+00:00', NULL)"
        )
        conn.commit()
        conn.close()

        store = UserStore(db_path=path, secret="s" * 40)
        assert store.is_verified("u1") is True
        # The backfill is a one-time upgrade step, so accounts created later
        # still start unverified.
        new_user = store.create_user("new@example.com", "password123", "N", "finance")
        assert new_user["email_verified_at"] is None

    def test_unverified_listing_and_counts(self):
        store = UserStore(db_path=":memory:", secret="s")
        a = store.create_user("a@example.com", "password123", "A", "finance")
        store.create_user(
            "b@example.com", "password123", "B", "finance", verified=True
        )
        c = store.create_user("c@example.com", "password123", "C", "finance")
        pending = store.list_unverified()
        assert {r["id"] for r in pending} == {a["id"], c["id"]}
        assert store.count_unverified() == 2

    def test_verification_status_reports_resend_time(self):
        store = UserStore(db_path=":memory:", secret="s")
        user = store.create_user("a@example.com", "password123", "A", "finance")
        store.issue_verification_token(user["id"])
        status = store.verification_status(user["id"])
        assert status["verified"] is False
        assert status["sent_at"]
        assert status["resend_available_at"] >= status["sent_at"]


# ---- the cap ---------------------------------------------------------------


class TestFreeAccountCap:
    def test_signup_refused_at_the_cap(self, env, monkeypatch):
        c, tmp_path = env
        monkeypatch.setenv("MAX_FREE_ACCOUNTS", "2")
        assert signup(c, "one@example.com").status_code == 200
        assert signup(c, "two@example.com").status_code == 200
        blocked = signup(c, "three@example.com")
        assert blocked.status_code == 503
        assert "full" in blocked.json()["detail"].lower()
        # The refused signup created nothing.
        store = UserStore(db_path=str(tmp_path / "u.sqlite3"), secret="test-secret")
        assert store.count_users_in_organisation("_sample") == 2

    def test_cap_is_counted_per_free_organisation(self, env, monkeypatch):
        c, tmp_path = env
        monkeypatch.setenv("MAX_FREE_ACCOUNTS", "1")
        signup(c, "one@example.com")
        # A paying organisation's seats are not metered against the free cap.
        c.post("/admin/organisations", headers=admin_auth(),
               json={"id": "acme", "name": "Acme"})
        made = c.post("/admin/users", headers=admin_auth(), json={
            "email": "paid@example.com", "password": "password123",
            "name": "Paid", "category": "finance", "organisation_id": "acme",
        })
        assert made.status_code == 200
        assert signup(c, "two@example.com").status_code == 503

    def test_invite_signup_is_not_capped(self, env, monkeypatch):
        c, _tmp = env
        monkeypatch.setenv("MAX_FREE_ACCOUNTS", "1")
        signup(c, "one@example.com")
        assert signup(c, "two@example.com").status_code == 503
        invite = c.post("/admin/invites", headers=admin_auth(),
                        json={"category": "finance"})
        code = invite.json()["token"]
        assert signup(c, "invited@example.com", invite=code).status_code == 200

    def test_cap_of_zero_closes_self_registration(self, env, monkeypatch):
        c, _tmp = env
        monkeypatch.setenv("MAX_FREE_ACCOUNTS", "0")
        assert signup(c, "one@example.com").status_code == 503
        # An invite still works, so an operator can always add someone.
        invite = c.post("/admin/invites", headers=admin_auth(),
                        json={"category": "finance"})
        code = invite.json()["token"]
        assert signup(c, "invited@example.com", invite=code).status_code == 200

    def test_malformed_cap_falls_back_to_the_default(self, env, monkeypatch):
        c, _tmp = env
        monkeypatch.setenv("MAX_FREE_ACCOUNTS", "not-a-number")
        assert signup(c, "one@example.com").status_code == 200

    def test_default_cap_is_five_hundred(self):
        from chatbot.users import DEFAULT_MAX_FREE_ACCOUNTS

        assert DEFAULT_MAX_FREE_ACCOUNTS == 500


# ---- the gate --------------------------------------------------------------


class TestUnverifiedGate:
    def test_signup_reports_that_verification_is_pending(self, env):
        c, _tmp = env
        body = signup(c).json()
        assert body["verification"]["verified"] is False
        assert body["verification"]["sent"] is True

    def test_unverified_user_cannot_chat(self, env):
        c, _tmp = env
        token = signup(c).json()["token"]
        res = c.post("/chat", headers=bearer(token),
                     json={"question": "What is ESG?", "category": "finance"})
        assert res.status_code == 403
        assert "confirm" in res.json()["detail"].lower()

    def test_unverified_user_cannot_search_or_stream(self, env):
        c, _tmp = env
        token = signup(c).json()["token"]
        assert c.post("/search", headers=bearer(token),
                      json={"question": "ESG", "category": "finance"}).status_code == 403
        assert c.post("/chat/stream", headers=bearer(token),
                      json={"question": "ESG", "category": "finance"}).status_code == 403

    def test_unverified_user_cannot_mint_document_links(self, env):
        c, _tmp = env
        token = signup(c).json()["token"]
        res = c.post("/documents/link", headers=bearer(token),
                     json={"source": "overview_of_esg.pdf#page=1"})
        assert res.status_code == 403

    def test_unverified_user_can_still_reach_me_and_resend(self, env):
        c, _tmp = env
        token = signup(c).json()["token"]
        me = c.get("/me", headers=bearer(token))
        assert me.status_code == 200
        assert me.json()["verification"]["verified"] is False
        # The recovery path must work, or the gate is a dead end.
        again = c.post("/auth/resend-verification", headers=bearer(token))
        assert again.status_code == 200

    def test_me_reports_verified_after_confirmation(self, env):
        c, _tmp = env
        signup_body = signup(c).json()
        link = signup_body["verification"]["link"]
        token = signup_body["token"]
        c.post("/auth/verify-email", json={"token": link.split("token=")[-1]})
        me = c.get("/me", headers=bearer(token)).json()
        assert me["user"]["email_verified"] is True
        assert me["verification"] is None

    def test_confirmed_user_can_chat(self, env):
        c, _tmp = env
        body = signup(c).json()
        c.post("/auth/verify-email",
               json={"token": body["verification"]["link"].split("token=")[-1]})
        res = c.post("/chat", headers=bearer(body["token"]),
                     json={"question": "Hi", "category": "finance"})
        assert res.status_code == 200

    def test_paid_accounts_are_never_gated(self, env):
        """A paying customer must not be locked out by a free-tier control."""
        c, _tmp = env
        c.post("/admin/organisations", headers=admin_auth(),
               json={"id": "acme", "name": "Acme"})
        created = c.post("/admin/users", headers=admin_auth(), json={
            "email": "paid@example.com", "password": "password123",
            "name": "Paid", "category": "finance", "organisation_id": "acme",
        }).json()
        member = dict(created)
        member["token"] = c.post("/auth/login", json={
            "email": "paid@example.com", "password": "password123",
        }).json()["token"]
        store = c.app.state.user_store
        assert store.get(member["id"])["email_verified_at"] is not None
        res = c.post("/chat", headers=bearer(member["token"]),
                     json={"question": "Hi", "category": "finance"})
        assert res.status_code == 200
        assert store.verification_status(member["id"])["verified"] is True

    def test_gate_can_be_disabled_for_a_deployment_without_mail(self, env, monkeypatch):
        c, _tmp = env
        monkeypatch.setenv("EMAIL_VERIFICATION_REQUIRED", "0")
        token = signup(c).json()["token"]
        assert c.post("/chat", headers=bearer(token),
                      json={"question": "Hi", "category": "finance"}).status_code == 200

    def test_admin_verification_override_unblocks_a_user(self, env):
        c, tmp_path = env
        token = signup(c).json()["token"]
        user_id = c.get("/me", headers=bearer(token)).json()["user"]["id"]
        c.post(f"/admin/users/{user_id}/verify", headers=admin_auth(),
               json={"verified": True})
        assert c.post("/chat", headers=bearer(token),
                      json={"question": "Hi", "category": "finance"}).status_code == 200


# ---- endpoints -------------------------------------------------------------


class TestVerificationEndpoints:
    def test_verify_endpoint_rejects_a_session_token(self, env):
        c, _tmp = env
        token = signup(c).json()["token"]
        assert c.post("/auth/verify-email", json={"token": token}).status_code == 422

    def test_verify_endpoint_rejects_an_unknown_token(self, env):
        c, _tmp = env
        assert c.post("/auth/verify-email",
                      json={"token": "x" * 40}).status_code == 422

    def test_verify_endpoint_reports_the_address(self, env):
        c, _tmp = env
        body = signup(c).json()
        res = c.post("/auth/verify-email",
                     json={"token": body["verification"]["link"].split("token=")[-1]})
        assert res.status_code == 200
        assert res.json()["verified"] is True
        assert res.json()["email"] == "user@example.com"

    def test_resend_requires_authentication(self, env):
        c, _tmp = env
        assert c.post("/auth/resend-verification").status_code == 401

    def test_resend_is_rate_limited_per_user(self, env):
        c, _tmp = env
        token = signup(c).json()["token"]
        # Signup already sent the first message, so a resend inside the
        # cooldown is refused: re-minting would invalidate the link the user
        # may already have open in another tab.
        res = c.post("/auth/resend-verification", headers=bearer(token)).json()
        assert res["sent"] is False
        assert res["verified"] is False

    def test_resend_on_a_verified_account_is_a_no_op(self, env):
        c, _tmp = env
        body = signup(c).json()
        c.post("/auth/verify-email",
               json={"token": body["verification"]["link"].split("token=")[-1]})
        res = c.post("/auth/resend-verification", headers=bearer(body["token"])).json()
        assert res["verified"] is True
        assert res["sent"] is False

    def test_login_reports_pending_verification(self, env):
        c, _tmp = env
        signup(c)
        body = c.post("/auth/login", json={
            "email": "user@example.com", "password": "password123",
        }).json()
        assert body["verification"]["verified"] is False


# ---- the outbox ------------------------------------------------------------


class TestOutbox:
    def test_signup_mail_is_recorded_when_smtp_is_absent(self, env):
        c, _tmp = env
        signup(c)
        store = c.app.state.admin_store
        mail = store.list_emails()
        assert len(mail) == 1
        assert mail[0]["to_address"] == "user@example.com"
        assert "/verify-email?token=" in mail[0]["body"]
        assert store.count_emails(include_consumed=False) == 1

    def test_signup_returns_the_link_when_mail_cannot_be_sent(self, env):
        """A deployment with no relay must still be able to onboard a user."""
        c, _tmp = env
        body = signup(c).json()
        assert body["verification"]["link"].startswith("http")
        assert "token=" in body["verification"]["link"]

    def test_smtp_is_used_when_configured(self, env, monkeypatch):
        c, _tmp = env
        sent = {}

        class FakeSmtp:
            def __init__(self, host, port, timeout=None):
                sent["host"] = host

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def starttls(self):
                sent["tls"] = True

            def login(self, user, password):
                sent["login"] = user

            def send_message(self, message):
                sent["to"] = message["To"]

        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        monkeypatch.setattr("chatbot.email_sender.smtplib.SMTP", FakeSmtp)
        signup(c)
        assert sent["host"] == "smtp.example.com"
        assert sent["tls"] is True
        # Nothing lands in the outbox when a real relay took the message.
        assert c.app.state.admin_store.count_emails() == 0

    def test_a_failed_send_falls_back_to_the_outbox(self, env, monkeypatch):
        c, _tmp = env

        class BrokenSmtp:
            def __init__(self, host, port, timeout=None):
                pass

            def __enter__(self):
                raise OSError("connection refused")

            def __exit__(self, *exc):
                return False

        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        monkeypatch.setattr("chatbot.email_sender.smtplib.SMTP", BrokenSmtp)
        body = signup(c).json()
        assert body["verification"]["sent"] is False
        # The account still exists and the link is recoverable.
        assert c.app.state.admin_store.count_emails() == 1
        assert body["verification"]["link"]

    def test_consuming_a_message_clears_the_pending_list(self, env):
        c, _tmp = env
        body = signup(c).json()
        mail = c.app.state.admin_store.list_emails()[0]
        assert c.post(f"/admin/emails/{mail['id']}/consume",
                      headers=admin_auth()).status_code == 200
        assert c.app.state.admin_store.count_emails(include_consumed=False) == 0

    def test_following_the_link_consumes_the_message(self, env):
        c, _tmp = env
        body = signup(c).json()
        assert c.app.state.admin_store.count_emails(include_consumed=False) == 1
        c.post("/auth/verify-email",
               json={"token": body["verification"]["link"].split("token=")[-1]})
        assert c.app.state.admin_store.count_emails(include_consumed=False) == 0


# ---- admin visibility ------------------------------------------------------


class TestAdminVisibility:
    def test_admin_sees_pending_verifications(self, env):
        c, _tmp = env
        signup(c, "one@example.com")
        signup(c, "two@example.com")
        res = c.get("/admin/verifications", headers=admin_auth()).json()
        assert res["count"] == 2
        assert {r["email"] for r in res["pending"]} == {
            "one@example.com", "two@example.com",
        }
        assert res["free_accounts"]["limit"] == 500

    def test_verification_listing_is_admin_only(self, env):
        c, _tmp = env
        assert c.get("/admin/verifications").status_code == 401

    def test_outbox_listing_is_admin_only(self, env):
        c, _tmp = env
        assert c.get("/admin/emails").status_code == 401
        assert c.get("/admin/emails", headers=admin_auth()).status_code == 200

    def test_free_account_counter_is_reported(self, env):
        c, _tmp = env
        signup(c)
        res = c.get("/admin/verifications", headers=admin_auth()).json()
        assert res["free_accounts"]["used"] == 1
        assert res["free_accounts"]["remaining"] == 499

# ---- the surfaces -----------------------------------------------------------


class TestSurfaces:
    def test_link_points_at_a_served_page(self, env):
        """The emailed link must resolve, or verification is unreachable."""
        c, _tmp = env
        link = signup(c).json()["verification"]["link"]
        path = link.split("?")[0].replace("http://testserver", "")
        assert c.get(path).status_code == 200
        assert "Confirm your email" in c.get(path).text

    def test_ui_offers_a_resend_control(self):
        html = open("src/chatbot/static/ui.html").read()
        assert 'id="verify-resend"' in html
        assert 'id="verify-notice"' in html
        # The composer must be blocked while unconfirmed, not just decorated.
        assert "$(\"input\").disabled = true" in html

    def test_admin_console_exposes_the_queue_and_outbox(self):
        html = open("src/chatbot/static/admin.html").read()
        assert 'id="verify-list"' in html
        assert 'id="outbox-list"' in html
        assert 'id="free-account-meter"' in html
        assert "/admin/verifications" in html
        assert "/admin/emails" in html

    def test_smtp_variables_are_documented(self):
        readme = open("README.md").read()
        for name in ("SMTP_HOST", "MAX_FREE_ACCOUNTS", "EMAIL_VERIFICATION_REQUIRED"):
            assert name in readme, name


class TestDeletedAccount:
    def test_deleted_account_token_is_rejected(self, env):
        """Deleting an account must kill its token, not just the row.

        The token proves who signed in, not that the account still exists.
        Before the lookup was added, a deleted account kept every permission
        until the token expired and /me crashed on the missing row.
        """
        c, _tmp = env
        body = signup(c).json()
        token = body["token"]
        user_id = body["user"]["id"]
        assert c.get("/me", headers=bearer(token)).status_code == 200
        assert c.delete(f"/admin/users/{user_id}", headers=admin_auth()).status_code == 200
        assert c.get("/me", headers=bearer(token)).status_code == 401
        assert c.post("/chat", headers=bearer(token),
                      json={"question": "hi"}).status_code == 401
        assert c.get("/sessions", headers=bearer(token)).status_code == 401
        # An unauthenticated caller is treated the same way, so the response
        # does not reveal whether the account once existed.
        assert c.get("/me", headers=bearer(token)).json()["detail"] == (
            c.get("/me").json()["detail"]
        )
