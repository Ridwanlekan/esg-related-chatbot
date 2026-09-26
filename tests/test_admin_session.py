import base64
import os

import pytest
from fastapi.testclient import TestClient

from chatbot.admin_store import AdminStore
from chatbot.api import create_app
from chatbot.session_store import SessionStore
from chatbot.users import UserStore


def _gated_app(tmp_path, admin_store=None, basic=False, totp=None):
    os.environ.pop("ADMIN_BASIC_USER", None)
    os.environ.pop("ADMIN_BASIC_PASS", None)
    os.environ.pop("ADMIN_TOTP_SECRET", None)
    if basic:
        os.environ["ADMIN_BASIC_USER"] = "admin"
        os.environ["ADMIN_BASIC_PASS"] = "hunter2"
    if totp:
        os.environ["ADMIN_TOTP_SECRET"] = totp
    store = admin_store or AdminStore(str(tmp_path / "w.sqlite3"))
    app = create_app(
        session_store=SessionStore(db_path=str(tmp_path / "s.sqlite3")),
        user_store=UserStore(db_path=str(tmp_path / "u.sqlite3"), secret="test-secret"),
        workspaces={"finance": type("WS", (), {"store": type("S", (), {"count": lambda: 1})()})()},
        admin_store=store,
        api_key="secret-admin-key",
        rate_limit=0,
    )
    return TestClient(app), store


def auth(key="secret-admin-key"):
    return {"authorization": f"Bearer {key}"}


class TestPasswordSignIn:
    def test_login_disabled_without_basic_creds(self, tmp_path):
        c, _ = _gated_app(tmp_path)
        r = c.post(
            "/admin/login",
            json={"username": "admin", "password": "hunter2"},
        )
        assert r.status_code == 503

    def test_login_wrong_password(self, tmp_path):
        c, _ = _gated_app(tmp_path, basic=True)
        r = c.post(
            "/admin/login",
            json={"username": "admin", "password": "wrong"},
        )
        assert r.status_code == 401

    def test_login_ok_sets_cookie_and_unlocks_admin(self, tmp_path):
        c, store = _gated_app(tmp_path, basic=True)
        r = c.post(
            "/admin/login",
            json={"username": "admin", "password": "hunter2"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and body["actor"] == "admin"
        assert "esg_admin_session" in r.cookies

        # No Authorization header at all: the session cookie alone is enough.
        assert c.get("/admin/status").status_code == 200

        st = c.get("/admin/session").json()
        assert st["authenticated"] is True and st["actor"] == "admin"
        assert st["login"] is True

    def test_login_and_logout_roundtrip(self, tmp_path):
        c, store = _gated_app(tmp_path, basic=True)
        c.post("/admin/login", json={"username": "admin", "password": "hunter2"})
        assert c.get("/admin/status").status_code == 200
        lo = c.post("/admin/logout")
        assert lo.status_code == 200
        assert c.get("/admin/session").json()["authenticated"] is False
        assert c.get("/admin/status").status_code == 401

    def test_totp_required_at_login(self, tmp_path):
        secret = "JBSWY3DPEHPK3PXP"
        c, _ = _gated_app(tmp_path, basic=True, totp=secret)
        r = c.post(
            "/admin/login",
            json={"username": "admin", "password": "hunter2"},
        )
        assert r.status_code == 401

        from chatbot.admin_security import totp_code

        ok = c.post(
            "/admin/login",
            json={"username": "admin", "password": "hunter2", "totp": totp_code(secret)},
        )
        assert ok.status_code == 200
        # Session replaces the per-request TOTP on the legacy path.
        assert c.get("/admin/status").status_code == 200

    def test_login_logged_in_audit(self, tmp_path):
        c, _ = _gated_app(tmp_path, basic=True)
        c.post("/admin/login", json={"username": "admin", "password": "hunter2"})
        c.post("/admin/logout")
        basic = {"authorization": "Basic " + base64.b64encode(b"admin:hunter2").decode()}
        events = c.get("/admin/audit", headers=basic).json()["events"]
        assert any(e["event"] == "admin.login" for e in events)
        assert any(e["event"] == "admin.logout" for e in events)


class TestSessionStore:
    def test_create_get_delete(self, tmp_path):
        store = AdminStore(str(tmp_path / "w.sqlite3"))
        created = store.create_admin_session("admin", ttl_seconds=3600)
        info = store.get_admin_session(created["token"])
        assert info and info["actor"] == "admin"
        assert store.delete_admin_session(created["token"]) is True
        assert store.get_admin_session(created["token"]) is None

    def test_expired_session_rejected(self, tmp_path):
        store = AdminStore(str(tmp_path / "w.sqlite3"))
        created = store.create_admin_session("admin", ttl_seconds=-10)
        assert store.get_admin_session(created["token"]) is None

    def test_forged_token_rejected(self, tmp_path):
        store = AdminStore(str(tmp_path / "w.sqlite3"))
        store.create_admin_session("admin", ttl_seconds=3600)
        assert store.get_admin_session("surely-not-the-token") is None
        assert store.get_admin_session("") is None