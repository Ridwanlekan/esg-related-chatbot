from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from chatbot.api import create_app
from chatbot.session_store import SessionStore
from chatbot.users import UserStore, sign_jwt
from chatbot.vector_store import SearchResult


class WorkspaceBot:
    def __init__(self, tag, source):
        self.tag = tag
        self.source = source
        self.ask_calls = []
        self.ingest_calls = []
        self.last_results = [
            SearchResult(chunk_id=f"c-{tag}", source=source, chunk_index=0,
                         content=f"content from {tag}", distance=0.3, score=0.9)
        ]
        self.store = SimpleNamespace(count=lambda: 100)

    def ask(self, question, k=3, source=None, history=None, usage_sink=None):
        self.ask_calls.append(question)
        return f"{self.tag}: answer to '{question}'"

    def ask_stream(self, question, k=3, source=None, history=None, usage_sink=None):
        self.ask_calls.append(question)
        yield f"{self.tag}: "

    def retrieve(self, question, k=3, source=None):
        return self.last_results

    def read_and_embed_data(self):
        self.ingest_calls.append(1)
        return SimpleNamespace(
            documents_seen=1, documents_reindexed=1, chunks_upserted=1,
            stale_chunks_removed=0, documents_failed=0,
        )


@pytest.fixture
def env(tmp_path):
    finance = WorkspaceBot("finance", "10. IFRS S1.pdf")
    hr = WorkspaceBot("hr", "ESG for HR.docx")
    app = create_app(
        session_store=SessionStore(db_path=str(tmp_path / "sessions.sqlite3")),
        user_store=UserStore(db_path=str(tmp_path / "users.sqlite3"), secret="test-secret"),
        workspaces={"finance": finance, "hr": hr},
        rate_limit=0,
    )
    return TestClient(app), {"finance": finance, "hr": hr}


def signup(client, email, category, name="Test User", password="password123"):
    """A confirmed account, so these tests are not all about the D18 gate."""
    res = client.post(
        "/auth/signup",
        json={"email": email, "password": password, "name": name, "category": category},
    )
    assert res.status_code == 200, res.text
    client.app.state.user_store.set_verified(res.json()["user"]["id"])
    return res.json()["token"]


class TestAuthFlow:
    def test_signup_login_me(self, env):
        c, _ = env
        token = signup(c, "ada@bank.com", "finance")
        assert c.get("/me", headers=auth(token)).json()["user"]["email"] == "ada@bank.com"

        login = c.post("/auth/login", json={"email": "ada@bank.com", "password": "password123"})
        assert login.status_code == 200
        assert login.json()["user"]["category"] == "finance"

        bad = c.post("/auth/login", json={"email": "ada@bank.com", "password": "wrong"})
        assert bad.status_code == 401

    def test_me_includes_workspace_meta(self, env):
        c, _ = env
        token = signup(c, "bob@corp.com", "hr")
        me = c.get("/me", headers=auth(token)).json()
        assert me["workspace"]["category"] == "hr"
        assert me["workspace"]["label"] == "People & HR"
        assert "hr" in me["workspace"]["categories"]

    def test_signup_lands_in_sample_organisation(self, env):
        c, _ = env
        token = signup(c, "free@visitor.com", "finance")
        assert c.get("/me", headers=auth(token)).json()["user"]["organisation_id"] == "_sample"

    def test_organisation_id_in_signup_response(self, env):
        c, _ = env
        res = c.post(
            "/auth/signup",
            json={"email": "n@visitor.com", "password": "password123",
                  "name": "N", "category": "hr"},
        )
        assert res.json()["user"]["organisation_id"] == "_sample"

    def test_single_workspace_user_gets_no_switcher(self, env):
        """Q7: exactly one workspace means the frontend shows no switcher."""
        c, _ = env
        token = signup(c, "solo@corp.com", "finance")
        me = c.get("/me", headers=auth(token)).json()
        assert [w["category"] for w in me["workspaces"]] == ["finance"]
        assert me["workspaces"][0]["is_primary"] is True

    def test_multi_workspace_user_sees_all_workspaces(self, env):
        c, _ = env
        token = signup(c, "multi@corp.com", "finance")
        store = c.app.state.user_store
        user = store.get_by_email("multi@corp.com")
        store.add_workspace(user["id"], "hr")

        me = c.get("/me", headers=auth(token)).json()
        assert [w["category"] for w in me["workspaces"]] == ["finance", "hr"]
        assert [w["is_primary"] for w in me["workspaces"]] == [True, False]
        assert me["user"]["workspaces"] == ["finance", "hr"]

    def test_membership_change_visible_without_relogin(self, env):
        """A grant takes effect on the next /me rather than at token expiry."""
        c, _ = env
        token = signup(c, "late@corp.com", "finance")
        store = c.app.state.user_store
        user = store.get_by_email("late@corp.com")
        assert c.get("/me", headers=auth(token)).json()["user"]["workspaces"] == ["finance"]

        store.add_workspace(user["id"], "hr")
        assert c.get("/me", headers=auth(token)).json()["user"]["workspaces"] == ["finance", "hr"]

    def test_revoked_workspace_disappears_from_switcher(self, env):
        c, _ = env
        token = signup(c, "revoke@corp.com", "finance")
        store = c.app.state.user_store
        user = store.get_by_email("revoke@corp.com")
        store.add_workspace(user["id"], "hr")
        assert len(c.get("/me", headers=auth(token)).json()["user"]["workspaces"]) == 2

        store.remove_workspace(user["id"], "finance")
        assert c.get("/me", headers=auth(token)).json()["user"]["workspaces"] == ["hr"]

    def test_membership_does_not_leak_across_users(self, env):
        c, _ = env
        mine = signup(c, "me@corp.com", "finance")
        theirs = signup(c, "them@corp.com", "hr")
        store = c.app.state.user_store
        store.add_workspace(store.get_by_email("me@corp.com")["id"], "hr")

        assert c.get("/me", headers=auth(theirs)).json()["user"]["workspaces"] == ["hr"]
        assert c.get("/me", headers=auth(mine)).json()["user"]["workspaces"] == ["finance", "hr"]

    def test_membership_in_unconfigured_workspace_hidden(self, env):
        """A grant to a workspace with no registry entry must not surface."""
        c, _ = env
        token = signup(c, "stale@corp.com", "finance")
        store = c.app.state.user_store
        user = store.get_by_email("stale@corp.com")
        # Bypasses add_workspace's validation, mimicking a workspace that was
        # deleted after the grant was made.
        store.conn.execute(
            "INSERT OR IGNORE INTO workspace_memberships "
            "(user_id, category, role, created_at) VALUES (?, 'legal', 'member', 'now')",
            (user["id"],),
        )
        store.conn.commit()

        me = c.get("/me", headers=auth(token)).json()
        assert [w["category"] for w in me["workspaces"]] == ["finance"]

    def test_legacy_token_without_workspaces_claim_still_resolves(self, env):
        """Tokens minted before the membership table fall back to `category`."""
        c, _ = env
        signup(c, "old@corp.com", "hr")
        store = c.app.state.user_store
        secret = store.secret
        user_id = store.get_by_email("old@corp.com")["id"]
        legacy = sign_jwt(
            secret,
            {"sub": user_id, "email": "old@corp.com",
             "name": "Old", "category": "hr", "purpose": "session"},
        )
        me = c.get("/me", headers={"authorization": f"Bearer {legacy}"}).json()
        assert [w["category"] for w in me["workspaces"]] == ["hr"]

    def test_missing_or_invalid_token_rejected(self, env):
        c, _ = env
        assert c.post("/chat", json={"question": "hi"}).status_code == 401
        assert c.get("/sessions").status_code == 401
        assert c.post("/chat", json={"question": "hi"},
                      headers={"authorization": "Bearer not-a-jwt"}).status_code == 401

    def test_public_endpoints_open(self, env):
        c, _ = env
        assert c.get("/health").status_code == 200
        assert c.get("/ui").status_code == 200
        assert c.post("/auth/login", json={"email": "x@y.com", "password": "12345678"}).status_code == 401


class TestWorkspaceRouting:
    def test_chat_routes_to_own_workspace(self, env):
        c, bots = env
        fin_token = signup(c, "fin@corp.com", "finance")
        hr_token = signup(c, "hr@corp.com", "hr")

        r = c.post("/chat", json={"question": "climate risk?"}, headers=auth(fin_token))
        assert r.json()["answer"] == "finance: answer to 'climate risk?'"
        fin = r.json()["sources"][0]
        assert fin["source"] == "10. IFRS S1.pdf"
        # The link carries a download capability so a native new-tab navigation,
        # which sends no Authorization header, can still open the document.
        assert fin["url"].startswith("/documents/download?source=10.%20IFRS%20S1.pdf&token=")

        r = c.post("/chat", json={"question": "diversity metric?"}, headers=auth(hr_token))
        assert r.json()["answer"] == "hr: answer to 'diversity metric?'"
        assert r.json()["sources"][0]["source"] == "ESG for HR.docx"

        assert bots["finance"].ask_calls == ["climate risk?"]
        assert bots["hr"].ask_calls == ["diversity metric?"]

    def test_greetings_use_workspace_persona(self, env):
        c, _ = env
        fin_token = signup(c, "fin@corp.com", "finance")
        hr_token = signup(c, "hr@corp.com", "hr")
        fin = c.post("/chat", json={"question": "hello"}, headers=auth(fin_token)).json()
        hr = c.post("/chat", json={"question": "hello"}, headers=auth(hr_token)).json()
        assert "ESG Finance assistant" in fin["answer"]
        assert "People & HR assistant" in hr["answer"]
        assert fin["sources"] == [] and hr["sources"] == []

    def test_session_isolation_between_users(self, env):
        c, _ = env
        fin_token = signup(c, "fin@corp.com", "finance")
        hr_token = signup(c, "hr@corp.com", "hr")
        sid = c.post("/chat", json={"question": "funding"}, headers=auth(fin_token)).json()["session_id"]

        assert c.get(f"/sessions/{sid}", headers=auth(hr_token)).status_code == 404
        assert c.get(f"/sessions/{sid}", headers=auth(fin_token)).status_code == 200
        assert c.post("/chat", json={"session_id": sid, "question": "intrusion?"},
                      headers=auth(hr_token)).status_code == 404
        assert c.delete(f"/sessions/{sid}", headers=auth(hr_token)).status_code == 404
        assert c.delete(f"/sessions/{sid}", headers=auth(fin_token)).status_code == 200

    def test_session_lists_are_per_user(self, env):
        c, _ = env
        fin_token = signup(c, "fin@corp.com", "finance")
        hr_token = signup(c, "hr@corp.com", "hr")
        c.post("/chat", json={"question": "one"}, headers=auth(fin_token))
        c.post("/chat", json={"question": "two"}, headers=auth(fin_token))
        c.post("/chat", json={"question": "other"}, headers=auth(hr_token))
        fin = c.get("/sessions", headers=auth(fin_token)).json()
        hr = c.get("/sessions", headers=auth(hr_token)).json()
        assert fin["total"] == 2
        assert hr["total"] == 1

    def test_legacy_unowned_sessions_invisible(self, env):
        c, _ = env
        token = signup(c, "fin@corp.com", "finance")
        # A session created before user-scoping has no owner (NULL).
        store = c.app.state.session_store
        legacy = store.new_id()
        store.create(legacy)  # no user_id
        assert store.owner_of(legacy) is None
        listing = c.get("/sessions", headers=auth(token)).json()
        assert listing["total"] == 0
        assert listing["sessions"] == []
        assert c.get(f"/sessions/{legacy}", headers=auth(token)).status_code == 404
        assert c.post("/chat", json={"session_id": legacy, "question": "hi"},
                      headers=auth(token)).status_code == 404
        assert c.delete(f"/sessions/{legacy}", headers=auth(token)).status_code == 404

    def test_search_scoped_to_workspace(self, env):
        c, bots = env
        fin_token = signup(c, "fin@corp.com", "finance")
        res = c.post("/search", json={"question": "targets", "k": 3}, headers=auth(fin_token))
        assert res.json()["results"][0]["source"] == "10. IFRS S1.pdf"

    def test_ingest_targets_specific_workspace(self, env):
        c, bots = env
        c.post("/ingest/finance")
        c.post("/ingest")
        assert bots["finance"].ingest_calls == [1, 1]
        assert bots["hr"].ingest_calls == [1]


def auth(token):
    return {"authorization": f"Bearer {token}"}