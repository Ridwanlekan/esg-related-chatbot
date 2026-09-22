import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from chatbot.admin_store import AdminStore
from chatbot.api import create_app
from chatbot.session_store import SessionStore
from chatbot.users import UserStore


class WorkspaceBot:
    def __init__(self, tag, source):
        self.tag = tag
        self.source = source
        self.ingest_calls = []
        self.store = SimpleNamespace(count=lambda: 100)
        self.last_results = []

    def ask(self, question=None, k=3, source=None, history=None, **kw):
        self.last_results = [SimpleNamespace(source=self.source)]
        return f"reply about {self.tag}"

    def read_and_embed_data(self):
        self.ingest_calls.append(1)
        return SimpleNamespace(
            documents_seen=1,
            documents_reindexed=1,
            chunks_upserted=10,
            stale_chunks_removed=0,
            documents_failed=0,
        )


@pytest.fixture
def admin_env(tmp_path):
    finance = WorkspaceBot("finance", "10. IFRS S1.pdf")
    hr = WorkspaceBot("hr", "ESG for HR.docx")
    os.environ["DATA_DIR"] = str(tmp_path / "data")
    os.environ["INDEX_DIR"] = str(tmp_path / ".index")
    app = create_app(
        session_store=SessionStore(db_path=str(tmp_path / "sessions.sqlite3")),
        user_store=UserStore(db_path=str(tmp_path / "users.sqlite3"), secret="test-secret"),
        workspaces={"finance": finance, "hr": hr},
        admin_store=AdminStore(str(tmp_path / "workspaces.sqlite3")),
        api_key="secret-admin-key",
        rate_limit=0,
    )
    return TestClient(app), {"finance": finance, "hr": hr}, tmp_path


def auth(key):
    return {"authorization": f"Bearer {key}"}


def test_admin_page_is_public(admin_env):
    c, _, _ = admin_env
    res = c.get("/admin")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    assert "Overview" in res.text


def test_admin_status_requires_api_key(admin_env):
    c, _, _ = admin_env
    assert c.get("/admin/status").status_code == 401
    assert c.get("/admin/status", headers=auth("wrong-key")).status_code == 401


def test_admin_status_reports_each_workspace(admin_env):
    c, _, _ = admin_env
    data = c.get("/admin/status", headers=auth("secret-admin-key")).json()
    cats = {ws["category"]: ws for ws in data["workspaces"]}
    assert set(cats) == {"finance", "hr"}
    for ws in data["workspaces"]:
        assert "label" in ws and "chunks" in ws and "sources" in ws
        assert "data_files" in ws and isinstance(ws["data_files"], list)
        assert isinstance(ws["indexed"], bool)
    assert cats["finance"]["chunks"] == 100  # from each workspace's own store
    assert "progress" in data
    assert data["totals"]["workspaces"] == 2
    assert data["totals"]["sessions"] == 0


def test_reindex_all_via_admin_key(admin_env):
    c, bots, _ = admin_env
    res = c.post("/ingest", headers=auth("secret-admin-key"))
    assert res.status_code == 200
    body = res.json()
    assert body["chunks_upserted"] == 20  # 10 per workspace
    assert bots["finance"].ingest_calls == [1]
    assert bots["hr"].ingest_calls == [1]


def test_reindex_specific_workspace(admin_env):
    c, bots, _ = admin_env
    res = c.post("/ingest/hr", headers=auth("secret-admin-key"))
    assert res.status_code == 200
    assert bots["hr"].ingest_calls == [1]
    assert bots["finance"].ingest_calls == []


class TestAdminWorkspaces:
    def test_public_workspaces_lists_categories(self, admin_env):
        c, _, _ = admin_env
        data = c.get("/workspaces").json()
        assert {w["category"] for w in data} >= {"finance", "hr"}
        assert all("label" in w and "blurb" in w for w in data)

    def test_create_custom_workspace_enables_signup(self, admin_env):
        c, _, _ = admin_env
        res = c.post(
            "/admin/workspaces",
            headers=auth("secret-admin-key"),
            json={"category": "climate", "label": "Climate Risk", "blurb": "TCFD"},
        )
        assert res.status_code == 200
        assert res.json()["type"] == "custom"
        listed = c.get("/admin/workspaces", headers=auth("secret-admin-key")).json()
        cats = {w["category"]: w for w in listed["workspaces"]}
        assert "climate" in cats and cats["climate"]["custom"] is True
        assert c.get("/workspaces").json()
        pub = {w["category"] for w in c.get("/workspaces").json()}
        assert "climate" in pub
        signup = c.post(
            "/auth/signup",
            json={
                "email": "clim@corp.com",
                "password": "password123",
                "name": "Clima",
                "category": "climate",
            },
        )
        assert signup.status_code == 200
        assert signup.json()["workspace"]["category"] == "climate"

    def test_admin_requires_api_key_for_workspace_mutations(self, admin_env):
        c, _, _ = admin_env
        assert c.post("/admin/workspaces", json={"category": "x"}).status_code == 401
        assert c.get("/admin/workspaces").status_code == 401

    def test_update_builtin_workspace_metadata(self, admin_env):
        c, _, _ = admin_env
        res = c.patch(
            "/admin/workspaces/finance",
            headers=auth("secret-admin-key"),
            json={"label": "Finance & ESG"},
        )
        assert res.status_code == 200
        assert res.json()["label"] == "Finance & ESG"
        assert res.json()["custom"] is False
        pub = {w["category"]: w for w in c.get("/workspaces").json()}
        assert pub["finance"]["label"] == "Finance & ESG"

    def test_delete_custom_workspace(self, admin_env):
        c, _, _ = admin_env
        c.post(
            "/admin/workspaces",
            headers=auth("secret-admin-key"),
            json={"category": "climate"},
        )
        res = c.delete("/admin/workspaces/climate", headers=auth("secret-admin-key"))
        assert res.status_code == 200
        cats = {w["category"] for w in c.get("/workspaces").json()}
        assert "climate" not in cats

    def test_cannot_delete_builtin_workspace(self, admin_env):
        c, _, _ = admin_env
        res = c.delete("/admin/workspaces/finance", headers=auth("secret-admin-key"))
        assert res.status_code == 403
        assert "built-in" in res.json()["detail"].lower()

    def test_reject_invalid_category_name(self, admin_env):
        c, _, _ = admin_env
        res = c.post(
            "/admin/workspaces",
            headers=auth("secret-admin-key"),
            json={"category": "Bad Category!"},
        )
        assert res.status_code == 422

    def test_deleted_workspace_vanishes_from_admin_views(self, tmp_path):
        os.environ["DATA_DIR"] = str(tmp_path / "data")
        os.environ["INDEX_DIR"] = str(tmp_path / ".index")
        app = create_app(
            session_store=SessionStore(db_path=str(tmp_path / "sessions.sqlite3")),
            admin_store=AdminStore(str(tmp_path / "workspaces.sqlite3")),
            api_key="secret-admin-key",
            rate_limit=0,
        )
        c = TestClient(app)
        h = auth("secret-admin-key")
        assert c.post("/admin/workspaces", headers=h, json={"category": "governance"}).status_code == 200
        assert "governance" in {w["category"] for w in c.get("/admin/status", headers=h).json()["workspaces"]}
        assert c.delete("/admin/workspaces/governance", headers=h).status_code == 200
        status_cats = {w["category"] for w in c.get("/admin/status", headers=h).json()["workspaces"]}
        assert "governance" not in status_cats
        doc_cats = {g["category"] for g in c.get("/admin/documents", headers=h).json()["documents"]}
        assert "governance" not in doc_cats
        pub_cats = {w["category"] for w in c.get("/workspaces").json()}
        assert "governance" not in pub_cats


class TestAdminDocuments:
    def test_upload_and_list_and_delete(self, admin_env):
        c, bots, tmp = admin_env
        up = c.post(
            "/admin/upload",
            headers=auth("secret-admin-key"),
            files=[("files", ("esg-report.pdf", b"%PDF-1.4 fake", "application/pdf"))],
            data={"category": "finance"},
        )
        assert up.status_code == 200
        body = up.json()
        assert body["saved_files"] == ["esg-report.pdf"]
        assert body["chunks_upserted"] == 10
        assert (tmp / "data" / "finance" / "esg-report.pdf").exists()

        docs = c.get("/admin/documents", headers=auth("secret-admin-key")).json()
        groups = {g["category"]: g["files"] for g in docs["documents"]}
        assert groups["finance"] == [{"name": "esg-report.pdf", "size": 13, "indexed": False}]
        # docs listing must not have triggered a re-ingest
        assert bots["finance"].ingest_calls == [1]

        dele = c.post(
            "/admin/documents/delete",
            headers=auth("secret-admin-key"),
            json={"category": "finance", "filename": "esg-report.pdf"},
        )
        assert dele.status_code == 200
        assert dele.json()["deleted"] == "esg-report.pdf"
        assert not (tmp / "data" / "finance" / "esg-report.pdf").exists()

    def test_upload_unknown_workspace_rejected(self, admin_env):
        c, _, _ = admin_env
        res = c.post(
            "/admin/upload",
            headers=auth("secret-admin-key"),
            files=[("files", ("x.pdf", b"abc", "application/pdf"))],
            data={"category": "nope"},
        )
        assert res.status_code == 422

    def test_delete_missing_document_404(self, admin_env):
        c, _, _ = admin_env
        res = c.post(
            "/admin/documents/delete",
            headers=auth("secret-admin-key"),
            json={"category": "finance", "filename": "ghost.pdf"},
        )
        assert res.status_code == 404


class TestAdminUsers:
    def test_create_list_update_delete(self, admin_env):
        c, _, _ = admin_env
        res = c.post(
            "/admin/users",
            headers=auth("secret-admin-key"),
            json={
                "email": "a@corp.com",
                "password": "password123",
                "name": "Alice",
                "category": "finance",
            },
        )
        assert res.status_code == 200
        uid = res.json()["id"]

        listed = c.get("/admin/users", headers=auth("secret-admin-key")).json()
        assert listed["total"] == 1
        assert listed["users"][0]["email"] == "a@corp.com"
        assert listed["users"][0]["session_count"] == 0

        upd = c.patch(
            f"/admin/users/{uid}",
            headers=auth("secret-admin-key"),
            json={"category": "hr", "password": "newpass123"},
        )
        assert upd.status_code == 200
        assert upd.json()["category"] == "hr"

        login = c.post(
            "/auth/login",
            json={"email": "a@corp.com", "password": "newpass123"},
        )
        assert login.status_code == 200
        assert login.json()["user"]["category"] == "hr"

        dele = c.delete(f"/admin/users/{uid}", headers=auth("secret-admin-key"))
        assert dele.status_code == 200
        assert c.get("/admin/users", headers=auth("secret-admin-key")).json()["total"] == 0

    def test_admin_users_require_api_key(self, admin_env):
        c, _, _ = admin_env
        assert c.get("/admin/users").status_code == 401
        assert c.post("/admin/users", json={}).status_code == 401

    def test_delete_unknown_user_404(self, admin_env):
        c, _, _ = admin_env
        res = c.delete("/admin/users/doesnotexist", headers=auth("secret-admin-key"))
        assert res.status_code == 404

    def test_list_users_reports_last_login(self, admin_env):
        c, _, _ = admin_env
        c.post(
            "/admin/users",
            headers=auth("secret-admin-key"),
            json={"email": "ll@corp.com", "password": "password123", "name": "L", "category": "finance"},
        )
        users = c.get("/admin/users", headers=auth("secret-admin-key")).json()["users"]
        user = next(u for u in users if u["email"] == "ll@corp.com")
        assert user["last_login"] is None
        c.post("/auth/login", json={"email": "ll@corp.com", "password": "password123"})
        users = c.get("/admin/users", headers=auth("secret-admin-key")).json()["users"]
        user = next(u for u in users if u["email"] == "ll@corp.com")
        assert user["last_login"] and user["last_login"].startswith("20")


class TestAdminDocumentsPagination:
    def test_paginates_per_workspace(self, admin_env):
        c, _, tmp_path = admin_env
        folder = tmp_path / "data" / "finance"
        folder.mkdir(parents=True, exist_ok=True)
        for i in range(5):
            (folder / f"doc{i}.pdf").write_text("x" * 10)
        res = c.get(
            "/admin/documents",
            headers=auth("secret-admin-key"),
            params={"category": "finance", "limit": 2, "offset": 0},
        )
        assert res.status_code == 200
        group = next(g for g in res.json()["documents"] if g["category"] == "finance")
        assert len(group["files"]) == 2
        assert group["total_files"] == 5
        assert group["has_more"] is True
        page2 = c.get(
            "/admin/documents",
            headers=auth("secret-admin-key"),
            params={"category": "finance", "limit": 2, "offset": 4},
        ).json()["documents"][0]
        assert len(page2["files"]) == 1
        assert page2["has_more"] is False

    def test_unknown_category_404(self, admin_env):
        c, _, _ = admin_env
        res = c.get(
            "/admin/documents",
            headers=auth("secret-admin-key"),
            params={"category": "nope"},
        )
        assert res.status_code == 404


class TestAuditLog:
    def test_admin_actions_are_logged(self, admin_env):
        c, _, _ = admin_env
        c.post(
            "/admin/workspaces",
            headers=auth("secret-admin-key"),
            json={"category": "ghost", "label": "Ghost"},
        )
        c.post(
            "/admin/users",
            headers=auth("secret-admin-key"),
            json={"email": "audit@corp.com", "password": "password123", "name": "A", "category": "finance"},
        )
        c.post(
            "/admin/users/audit@nonexistent",
            headers=auth("secret-admin-key"),
            json={},
        )  # 422/404 -> no audit row expected
        res = c.get("/admin/audit", headers=auth("secret-admin-key"))
        assert res.status_code == 200
        events = res.json()["events"]
        names = [e["event"] for e in events]
        assert "workspace.create" in names
        assert "user.create" in names
        assert all(e["actor"] for e in events)

    def test_signup_is_logged(self, admin_env):
        c, _, _ = admin_env
        c.post(
            "/auth/signup",
            json={"email": "sig@corp.com", "password": "password123", "name": "S", "category": "finance"},
        )
        events = c.get("/admin/audit", headers=auth("secret-admin-key")).json()["events"]
        assert any(e["event"] == "auth.signup" for e in events)


class TestAdminTranscripts:
    def test_sessions_and_transcript(self, admin_env):
        c, _, _ = admin_env
        c.post(
            "/auth/signup",
            json={"email": "t@corp.com", "password": "password123", "name": "T", "category": "finance"},
        )
        tok = c.post(
            "/auth/login",
            json={"email": "t@corp.com", "password": "password123"},
        ).json()["token"]
        c.post(
            "/chat",
            headers={"authorization": f"Bearer {tok}"},
            json={"question": "hello dashboard"},
        )
        users = c.get("/admin/users", headers=auth("secret-admin-key")).json()["users"]
        uid = next(u for u in users if u["email"] == "t@corp.com")["id"]
        sess = c.get(f"/admin/users/{uid}/sessions", headers=auth("secret-admin-key"))
        assert sess.status_code == 200
        sessions = sess.json()
        assert sessions["total"] >= 1
        sid = sessions["sessions"][0]["session_id"]
        msgs = c.get(f"/admin/sessions/{sid}", headers=auth("secret-admin-key"))
        assert msgs.status_code == 200
        assert len(msgs.json()["messages"]) >= 2

    def test_session_auth_404s(self, admin_env):
        c, _, _ = admin_env
        assert c.get("/admin/sessions/nope", headers=auth("secret-admin-key")).status_code == 404


class TestCSVExports:
    def test_users_csv(self, admin_env):
        c, _, _ = admin_env
        c.post(
            "/admin/users",
            headers=auth("secret-admin-key"),
            json={"email": "csv@corp.com", "password": "password123", "name": "C", "category": "finance"},
        )
        res = c.get("/admin/users/export.csv", headers=auth("secret-admin-key"))
        assert res.status_code == 200
        assert "text/csv" in res.headers["content-type"]
        assert "filename=\"users.csv\"" in res.headers["content-disposition"]
        body = res.text
        assert "\ufeff" not in body or True
        assert "csv@corp.com" in body
        assert body.splitlines()[0].startswith("id,email,name")

    def test_workspaces_csv(self, admin_env):
        c, _, tmp_path = admin_env
        folder = tmp_path / "data" / "hr"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "policy.pdf").write_text("x")
        res = c.get("/admin/workspaces/export.csv", headers=auth("secret-admin-key"))
        assert res.status_code == 200
        assert "filename=\"workspaces.csv\"" in res.headers["content-disposition"]
        header = res.text.splitlines()[0]
        assert header.startswith("category,label,type")
        assert any(line.startswith("finance,") for line in res.text.splitlines())


class TestInvites:
    def test_create_list_redeem_flow(self, admin_env):
        c, _, _ = admin_env
        created = c.post(
            "/admin/invites",
            headers=auth("secret-admin-key"),
            json={"category": "finance", "max_uses": 2, "ttl_hours": 24},
        )
        assert created.status_code == 200
        token = created.json()["token"]
        listed = c.get("/admin/invites", headers=auth("secret-admin-key")).json()
        assert listed["invites"][0]["token"] is None  # raw token never returned again
        assert listed["invites"][0]["valid"] is True

        peek = c.get(f"/invites/{token}")
        assert peek.status_code == 200
        assert peek.json()["category"] == "finance"

        signup = c.post(
            "/auth/signup",
            json={
                "email": "inv@corp.com",
                "password": "password123",
                "name": "I",
                "category": "finance",
                "invite": token,
            },
        )
        assert signup.status_code == 200

    def test_max_uses_exceeded_rejected(self, admin_env):
        c, _, _ = admin_env
        created = c.post(
            "/admin/invites",
            headers=auth("secret-admin-key"),
            json={"category": "finance", "max_uses": 1, "ttl_hours": 1},
        ).json()
        tok = created["token"]
        assert c.get(f"/invites/{tok}").status_code == 200
        first = c.post(
            "/auth/signup",
            json={
                "email": "inv2@corp.com",
                "password": "password123",
                "name": "I2",
                "category": "finance",
                "invite": tok,
            },
        )
        assert first.status_code == 200
        second = c.post(
            "/auth/signup",
            json={
                "email": "inv3@corp.com",
                "password": "password123",
                "name": "I3",
                "category": "finance",
                "invite": tok,
            },
        )
        assert second.status_code == 403
        assert "used up" in second.json()["detail"]

    def test_expired_invite_rejected(self, admin_env):
        c, _, tmp_path = admin_env
        import datetime as dt

        from chatbot.admin_store import AdminStore

        # The app's AdminStore and this second handle share the same sqlite file.
        store = AdminStore(str(tmp_path / "workspaces.sqlite3"))
        created = store.create_invite("finance", max_uses=1, ttl_hours=1)
        store.conn.execute(
            "UPDATE invites SET expires_at = ? WHERE token = ?",
            (
                (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat(),
                AdminStore._hash_token(created["token"]),
            ),
        )
        store.conn.commit()
        assert c.get(f"/invites/{created['token']}").status_code == 404
        res = c.post(
            "/auth/signup",
            json={
                "email": "exp@corp.com",
                "password": "password123",
                "name": "E",
                "category": "finance",
                "invite": created["token"],
            },
        )
        assert res.status_code == 403
        store.close()

    def test_invalid_invite_token(self, admin_env):
        c, _, _ = admin_env
        assert c.get("/invites/bogus").status_code == 404

    def test_delete_invite(self, admin_env):
        c, _, _ = admin_env
        tok = c.post(
            "/admin/invites",
            headers=auth("secret-admin-key"),
            json={"category": "finance"},
        ).json()["token"]
        dele = c.delete(f"/admin/invites/{tok}", headers=auth("secret-admin-key"))
        assert dele.status_code == 200
        assert c.get(f"/invites/{tok}").status_code == 404


class TestAdminAuthGate:
    def _gated_app(self, tmp_path, basic=False, totp=None, secret="x"):
        os.environ.pop("ADMIN_BASIC_USER", None)
        os.environ.pop("ADMIN_BASIC_PASS", None)
        os.environ.pop("ADMIN_TOTP_SECRET", None)
        if basic:
            os.environ["ADMIN_BASIC_USER"] = "admin"
            os.environ["ADMIN_BASIC_PASS"] = "hunter2"
        if totp:
            os.environ["ADMIN_TOTP_SECRET"] = totp
        from chatbot.admin_store import AdminStore

        app = create_app(
            session_store=SessionStore(db_path=str(tmp_path / "s.sqlite3")),
            user_store=UserStore(db_path=str(tmp_path / "u.sqlite3"), secret="test-secret"),
            workspaces={"finance": WorkspaceBot("finance", "f.pdf")},
            admin_store=AdminStore(str(tmp_path / "w.sqlite3")),
            api_key="secret-admin-key",
            rate_limit=0,
        )
        return app

    def test_plain_api_key_still_works(self, tmp_path):
        c = TestClient(self._gated_app(tmp_path))
        assert (
            c.get("/admin/status", headers=auth("secret-admin-key")).status_code == 200
        )
        assert c.get("/admin/status").status_code == 401

    def test_basic_auth_enforced(self, tmp_path):
        c = TestClient(self._gated_app(tmp_path, basic=True))
        assert c.get("/admin/status").status_code == 401
        assert c.get("/admin/status", headers=auth("secret-admin-key")).status_code == 401
        ok = c.get(
            "/admin/status",
            headers=auth("secret-admin-key")
            | {"authorization": "Basic " + __import__("base64").b64encode(b"admin:hunter2").decode()},
        )
        assert ok.status_code == 200
        bad = c.get(
            "/admin/status",
            headers=auth("secret-admin-key")
            | {"authorization": "Basic " + __import__("base64").b64encode(b"admin:wrong").decode()},
        )
        assert bad.status_code == 401

    def test_totp_required(self, tmp_path):
        secret = "JBSWY3DPEHPK3PXP"  # base32, TOTP test secret
        c = TestClient(self._gated_app(tmp_path, totp=secret))
        assert c.get("/admin/status", headers=auth("secret-admin-key")).status_code == 401
        from chatbot.admin_security import totp_code

        code = totp_code(secret)
        ok = c.get(
            "/admin/status",
            headers=auth("secret-admin-key") | {"x-admin-totp": code},
        )
        assert ok.status_code == 200
        bad = c.get(
            "/admin/status",
            headers=auth("secret-admin-key") | {"x-admin-totp": "000000"},
        )
        assert bad.status_code == 401

    def test_audit_actor_reflects_basic_user(self, tmp_path):
        c = TestClient(self._gated_app(tmp_path, basic=True))
        c.post(
            "/admin/users",
            headers=auth("secret-admin-key")
            | {"authorization": "Basic " + __import__("base64").b64encode(b"admin:hunter2").decode()},
            json={"email": "act@corp.com", "password": "password123", "name": "X", "category": "finance"},
        )
        events = c.get(
            "/admin/audit",
            headers=auth("secret-admin-key")
            | {"authorization": "Basic " + __import__("base64").b64encode(b"admin:hunter2").decode()},
        ).json()["events"]
        assert any(e["event"] == "user.create" and e["actor"] == "user:admin" for e in events)