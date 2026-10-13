"""Global security headers and admin-console CSRF (app hardening)."""

import os

import pytest
from fastapi.testclient import TestClient

from chatbot.admin_store import AdminStore
from chatbot.api import create_app
from chatbot.session_store import SessionStore
from chatbot.users import UserStore


def _app(tmp_path, basic=True):
    os.environ.pop("ADMIN_BASIC_USER", None)
    os.environ.pop("ADMIN_BASIC_PASS", None)
    os.environ.pop("ADMIN_TOTP_SECRET", None)
    if basic:
        os.environ["ADMIN_BASIC_USER"] = "admin"
        os.environ["ADMIN_BASIC_PASS"] = "hunter2"
    app = create_app(
        session_store=SessionStore(db_path=str(tmp_path / "s.sqlite3")),
        user_store=UserStore(db_path=str(tmp_path / "u.sqlite3"), secret="test-secret"),
        workspaces={"finance": type("WS", (), {"store": type("S", (), {"count": lambda: 1})()})()},
        admin_store=AdminStore(str(tmp_path / "w.sqlite3")),
        api_key="secret-admin-key",
        rate_limit=0,
    )
    return TestClient(app)


class TestSecurityHeaders:
    def test_headers_present_on_every_response(self, tmp_path):
        c = _app(tmp_path)
        r = c.get("/health")
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert r.headers["X-Frame-Options"] == "DENY"
        assert r.headers["Referrer-Policy"] == "no-referrer"
        assert "X-Request-ID" in r.headers
        csp = r.headers["Content-Security-Policy"]
        assert "frame-ancestors 'none'" in csp
        assert "object-src 'none'" in csp

    def test_csp_keeps_inline_for_the_consoles(self, tmp_path):
        c = _app(tmp_path)
        csp = c.get("/ui").headers["Content-Security-Policy"]
        assert "'unsafe-inline'" in csp
        assert "default-src 'self'" in csp

    def test_hsts_only_when_https(self, tmp_path):
        c = _app(tmp_path)
        assert "Strict-Transport-Security" not in c.get("/health").headers
        r = c.get("/health", headers={"x-forwarded-proto": "https"})
        assert r.headers["Strict-Transport-Security"].startswith("max-age=31536000")


class TestAdminCsrf:
    def _login(self, c):
        r = c.post(
            "/admin/login", json={"username": "admin", "password": "hunter2"}
        )
        assert r.status_code == 200
        return r

    def test_login_sets_csrf_cookie_and_returns_token(self, tmp_path):
        c = _app(tmp_path)
        r = self._login(c)
        assert "esg_admin_csrf" in r.cookies
        assert r.json().get("csrf")

    def test_session_write_without_csrf_is_forbidden(self, tmp_path):
        c = _app(tmp_path)
        self._login(c)
        r = c.post("/admin/organisations", json={"id": "csrf1", "name": "CSRF One"})
        assert r.status_code == 403

    def test_session_write_with_wrong_csrf_is_forbidden(self, tmp_path):
        c = _app(tmp_path)
        self._login(c)
        r = c.post(
            "/admin/organisations",
            json={"id": "csrf2", "name": "CSRF Two"},
            headers={"x-csrf-token": "not-the-token"},
        )
        assert r.status_code == 403

    def test_session_write_with_csrf_succeeds(self, tmp_path):
        c = _app(tmp_path)
        token = self._login(c).json()["csrf"]
        r = c.post(
            "/admin/organisations",
            json={"id": "csrf3", "name": "CSRF Three"},
            headers={"x-csrf-token": token},
        )
        assert r.status_code == 200, r.text

    def test_api_key_write_needs_no_csrf(self, tmp_path):
        # No Basic creds configured, so the API key is the admin path and the
        # CSRF requirement (cookie-only) does not apply.
        c = _app(tmp_path, basic=False)
        r = c.post(
            "/admin/organisations",
            json={"id": "key1", "name": "Key One"},
            headers={"authorization": "Bearer secret-admin-key"},
        )
        assert r.status_code == 200, r.text

    def test_logout_clears_csrf_cookie(self, tmp_path):
        c = _app(tmp_path)
        self._login(c)
        c.post("/admin/logout")
        assert not c.cookies.get("esg_admin_csrf")
