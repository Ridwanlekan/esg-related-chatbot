"""Password reset and session invalidation (D18-era account hygiene).

A product with email verification but no password recovery dead-ends a user
who forgets their password, and a password change that leaves old sessions
alive keeps out a locked-out account's holder. These tests pin the reset flow
(forgot -> email -> reset -> sign-in), the owner/admin reset of a member's
password, and the session generation that signs the other devices out.
"""

import pytest

from chatbot.users import MIN_PASSWORD_LENGTH, UserStore

KEY = "secret-admin-key"


def admin_auth():
    return {"authorization": f"Bearer {KEY}"}


def bearer(token):
    return {"authorization": f"Bearer {token}"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from chatbot.admin_store import AdminStore
    from chatbot.api import create_app
    from chatbot.session_store import SessionStore

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


def signup(c, email="reset@example.com"):
    return c.post("/auth/signup", json={
        "email": email, "password": "password123", "name": "Reset Test",
        "category": "finance",
    }).json()


def paid_member(c, email):
    c.post("/admin/organisations", headers=admin_auth(),
           json={"id": "acme", "name": "Acme"})
    created = c.post("/admin/users", headers=admin_auth(), json={
        "email": email, "password": "password123", "name": "Member",
        "category": "finance", "organisation_id": "acme",
    }).json()
    return created


# ---- the store, in isolation from HTTP ------------------------------------


class TestResetTokens:
    def test_token_is_single_use(self):
        store = UserStore(db_path=":memory:", secret="s")
        user = store.create_user("a@example.com", "pw12345678", "A", "finance")
        token = store.issue_password_reset_token(user["id"])
        assert store.reset_password(token, "newpw12345") == user["id"]
        # A link works once.
        with pytest.raises(ValueError):
            store.reset_password(token, "another12345")

    def test_only_a_digest_is_stored(self):
        store = UserStore(db_path=":memory:", secret="s")
        user = store.create_user("a@example.com", "pw12345678", "A", "finance")
        token = store.issue_password_reset_token(user["id"])
        row = store.conn.execute(
            "SELECT password_reset_token_hash FROM users WHERE id = ?",
            (user["id"],),
        ).fetchone()
        assert row[0] and row[0] != token

    def test_reissuing_invalidates_the_previous_token(self):
        store = UserStore(db_path=":memory:", secret="s")
        user = store.create_user("a@example.com", "pw12345678", "A", "finance")
        first = store.issue_password_reset_token(user["id"])
        store.issue_password_reset_token(user["id"])
        with pytest.raises(ValueError):
            store.reset_password(first, "another12345")

    def test_a_session_token_is_not_a_reset_token(self):
        store = UserStore(db_path=":memory:", secret="s")
        user = store.create_user("a@example.com", "pw12345678", "A", "finance")
        session = store.token_for(user)
        with pytest.raises(ValueError):
            store.reset_password(session, "another12345")

    def test_password_too_short_is_refused(self):
        store = UserStore(db_path=":memory:", secret="s")
        user = store.create_user("a@example.com", "pw12345678", "A", "finance")
        token = store.issue_password_reset_token(user["id"])
        with pytest.raises(ValueError):
            store.reset_password(token, "short")

    def test_expired_token_is_rejected(self):
        store = UserStore(db_path=":memory:", secret="s")
        user = store.create_user("a@example.com", "pw12345678", "A", "finance")
        token = store.issue_password_reset_token(user["id"], ttl_hours=-1)
        with pytest.raises(ValueError):
            store.reset_password(token, "another12345")

    def test_garbage_token_is_rejected(self):
        store = UserStore(db_path=":memory:", secret="s")
        for bad in ("", "not-a-token", "a.b.c"):
            with pytest.raises(ValueError):
                store.reset_password(bad, "another12345")


class TestSessionGeneration:
    def test_the_new_password_replaces_the_old(self):
        store = UserStore(db_path=":memory:", secret="s")
        user = store.create_user("a@example.com", "pw12345678", "A", "finance")
        token = store.issue_password_reset_token(user["id"])
        store.reset_password(token, "brandnewpw1")
        assert store.verify_password(user["id"], "brandnewpw1") is True
        assert store.verify_password(user["id"], "pw12345678") is False

    def test_change_password_needs_the_current_password(self):
        store = UserStore(db_path=":memory:", secret="s")
        user = store.create_user("a@example.com", "pw12345678", "A", "finance")
        with pytest.raises(ValueError):
            store.change_password(user["id"], "wrongpass1", "brandnewpw1")
        assert store.verify_password(user["id"], "pw12345678") is True

    def test_reset_bumps_the_session_version(self):
        store = UserStore(db_path=":memory:", secret="s")
        user = store.create_user("a@example.com", "pw12345678", "A", "finance")
        before = store.token_version(user["id"])
        token = store.issue_password_reset_token(user["id"])
        store.reset_password(token, "brandnewpw1")
        assert store.token_version(user["id"]) == before + 1

    def test_change_password_bumps_the_session_version(self):
        store = UserStore(db_path=":memory:", secret="s")
        user = store.create_user("a@example.com", "pw12345678", "A", "finance")
        before = store.token_version(user["id"])
        store.change_password(user["id"], "pw12345678", "brandnewpw1")
        assert store.token_version(user["id"]) == before + 1

    def test_admin_set_password_bumps_the_session_version(self):
        store = UserStore(db_path=":memory:", secret="s")
        user = store.create_user("a@example.com", "pw12345678", "A", "finance")
        before = store.token_version(user["id"])
        store.update_user(user["id"], password="brandnewpw1")
        assert store.token_version(user["id"]) == before + 1

    def test_new_tokens_carry_the_current_version(self):
        store = UserStore(db_path=":memory:", secret="s")
        user = store.create_user("a@example.com", "pw12345678", "A", "finance")
        first = store.token_for(user)
        store.change_password(user["id"], "pw12345678", "brandnewpw1")
        second = store.token_for(user)
        assert first != second
        payload = __import__("chatbot.users", fromlist=["verify_jwt"]).verify_jwt(
            "s", second
        )
        assert payload["v"] == store.token_version(user["id"])


# ---- the endpoints --------------------------------------------------------


class TestForgotPassword:
    def test_forgot_sends_mail_and_never_probes(self, env):
        c, _t = env
        body = signup(c)
        res = c.post("/auth/forgot-password", json={"email": "reset@example.com"}).json()
        assert res["sent"] is True
        assert "on its way" in res["message"].lower()
        # The outbox copy is what a no-SMTP deployment acts on.
        mail = c.app.state.admin_store.list_emails()
        assert any("/reset-password?token=" in m["body"] for m in mail)
        # Same response for an unknown address, so emails cannot be probed.
        unknown = c.post("/auth/forgot-password",
                         json={"email": "nobody@example.com"}).json()
        assert unknown["sent"] is True
        assert unknown["message"] == res["message"]
        assert "link" not in unknown
        assert body and body["token"]

    def test_forgot_requires_a_real_email_shape(self, env):
        c, _t = env
        assert c.post("/auth/forgot-password",
                      json={"email": "nope"}).status_code == 422


class TestResetPassword:
    def test_reset_signs_the_user_in_with_a_new_password(self, env):
        c, _t = env
        signup(c)
        link = c.post("/auth/forgot-password",
                      json={"email": "reset@example.com"}).json()["link"]
        token = link.split("token=")[-1]
        res = c.post("/auth/reset-password",
                     json={"token": token, "password": "brandnewpw1"})
        assert res.status_code == 200
        assert res.json()["token"]
        # The old password is dead, the new one works.
        assert c.post("/auth/login", json={
            "email": "reset@example.com", "password": "pw12345678",
        }).status_code == 401
        assert c.post("/auth/login", json={
            "email": "reset@example.com", "password": "brandnewpw1",
        }).status_code == 200

    def test_a_used_link_is_rejected(self, env):
        c, _t = env
        signup(c)
        link = c.post("/auth/forgot-password",
                      json={"email": "reset@example.com"}).json()["link"]
        token = link.split("token=")[-1]
        c.post("/auth/reset-password",
               json={"token": token, "password": "brandnewpw1"})
        assert c.post("/auth/reset-password",
                      json={"token": token, "password": "another12345"}).status_code == 422

    def test_a_weak_password_is_rejected(self, env):
        c, _t = env
        signup(c)
        link = c.post("/auth/forgot-password",
                      json={"email": "reset@example.com"}).json()["link"]
        token = link.split("token=")[-1]
        assert c.post("/auth/reset-password",
                      json={"token": token, "password": "short"}).status_code == 422

    def test_old_sessions_are_killed_by_the_reset(self, env):
        c, _t = env
        body = signup(c)
        old_token = body["token"]
        link = c.post("/auth/forgot-password",
                      json={"email": "reset@example.com"}).json()["link"]
        fresh = c.post("/auth/reset-password", json={
            "token": link.split("token=")[-1], "password": "brandnewpw1",
        }).json()
        # The session minted before the reset is refused from now on.
        assert c.get("/me", headers=bearer(old_token)).status_code == 401
        assert c.get("/me", headers=bearer(fresh["token"])).status_code == 200


class TestChangePassword:
    def test_change_password_return_a_fresh_session(self, env):
        c, _t = env
        body = signup(c)
        res = c.post("/me/password", headers=bearer(body["token"]), json={
            "current_password": "password123", "new_password": "brandnewpw1",
        })
        assert res.status_code == 200
        fresh = res.json()["token"]
        # Old token refused, new token works, new password logs in.
        assert c.get("/me", headers=bearer(body["token"])).status_code == 401
        assert c.get("/me", headers=bearer(fresh)).status_code == 200
        assert c.post("/auth/login", json={
            "email": "reset@example.com", "password": "brandnewpw1",
        }).status_code == 200

    def test_wrong_current_password_is_refused(self, env):
        c, _t = env
        body = signup(c)
        res = c.post("/me/password", headers=bearer(body["token"]), json={
            "current_password": "wrongpass1", "new_password": "brandnewpw1",
        })
        assert res.status_code == 422
        # Still signed in, because nothing changed.
        assert c.get("/me", headers=bearer(body["token"])).status_code == 200

    def test_unauthenticated_change_is_refused(self, env):
        c, _t = env
        assert c.post("/me/password", json={
            "current_password": "password123", "new_password": "brandnewpw1",
        }).status_code == 401


class TestOrgResetMemberPassword:
    def _owner_ctx(self, c):
        c.post("/admin/organisations", headers=admin_auth(),
               json={"id": "acme", "name": "Acme"})
        owner = paid_member(c, "owner@acme.com")
        c.post("/admin/organisations/role", headers=admin_auth(), json={
            "organisation_id": "acme", "user_id": owner["id"], "role": "owner",
        })
        login = c.post("/auth/login", json={
            "email": "owner@acme.com", "password": "password123",
        }).json()
        return owner, login["token"]

    def test_owner_can_reset_a_member_password(self, env):
        c, _t = env
        _owner, owner_token = self._owner_ctx(c)
        member = paid_member(c, "member@acme.com")
        before = c.post("/auth/login", json={
            "email": "member@acme.com", "password": "password123",
        }).json()
        assert c.get("/me", headers=bearer(before["token"])).status_code == 200
        res = c.post(f"/org/members/{member['id']}/reset-password",
                     headers=bearer(owner_token),
                     json={"password": "temppass123"})
        assert res.status_code == 200
        # The member's pre-reset session is dead and the new password works.
        assert c.get("/me", headers=bearer(before["token"])).status_code == 401
        assert c.post("/auth/login", json={
            "email": "member@acme.com", "password": "temppass123",
        }).status_code == 200

    def test_only_a_manager_can_reset(self, env):
        c, _t = env
        owner, owner_token = self._owner_ctx(c)
        member = paid_member(c, "member@acme.com")
        member_login = c.post("/auth/login", json={
            "email": "member@acme.com", "password": "password123",
        }).json()
        res = c.post(f"/org/members/{owner['id']}/reset-password",
                     headers=bearer(member_login["token"]),
                     json={"password": "temppass123"})
        assert res.status_code == 403

    def test_weak_reset_password_is_refused(self, env):
        c, _t = env
        _owner, owner_token = self._owner_ctx(c)
        member = paid_member(c, "member@acme.com")
        res = c.post(f"/org/members/{member['id']}/reset-password",
                     headers=bearer(owner_token), json={"password": "short"})
        assert res.status_code == 422

    def test_reset_is_scoped_to_the_caller_organisation(self, env):
        c, _t = env
        _owner, owner_token = self._owner_ctx(c)
        # A user raised into a different organisation is not a member of acme.
        c.post("/admin/organisations", headers=admin_auth(),
               json={"id": "elsewhere", "name": "Elsewhere"})
        other = c.post("/admin/users", headers=admin_auth(), json={
            "email": "other@elsewhere.com", "password": "password123",
            "name": "Other", "category": "finance", "organisation_id": "elsewhere",
        }).json()
        res = c.post(f"/org/members/{other['id']}/reset-password",
                     headers=bearer(owner_token),
                     json={"password": "temppass123"})
        assert res.status_code == 404


class TestSurfaces:
    def test_the_reset_page_is_served(self, env):
        c, _t = env
        res = c.get("/reset-password")
        assert res.status_code == 200
        assert "Reset your password" in res.text

    def test_the_reset_link_resolves(self, env):
        c, _t = env
        signup(c)
        link = c.post("/auth/forgot-password",
                      json={"email": "reset@example.com"}).json()["link"]
        path = link.split("?")[0].replace("http://testserver", "")
        assert c.get(path).status_code == 200

    def test_smtp_delivers_the_reset_email(self, env, monkeypatch):
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
                sent["body"] = message.get_body().get_content()

        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        monkeypatch.setattr("chatbot.email_sender.smtplib.SMTP", FakeSmtp)
        c, _t = env
        signup(c)
        res = c.post("/auth/forgot-password", json={"email": "reset@example.com"}).json()
        assert sent["host"] == "smtp.example.com"
        assert "/reset-password?token=" in sent["body"]
        # Nothing lands in the outbox when a real relay took the message.
        assert c.app.state.admin_store.count_emails() == 0
        # No link is returned to the browser once a relay is configured.
        assert "link" not in res

    def test_ui_offers_a_forgot_link(self):
        html = open("src/chatbot/static/ui.html").read()
        assert "forgot-link" in html
        assert "/auth/forgot-password" in html

    def test_min_password_length_is_still_eight(self):
        assert MIN_PASSWORD_LENGTH == 8