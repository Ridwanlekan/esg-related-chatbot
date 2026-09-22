from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from chatbot.api import create_app
from chatbot.session_store import SessionStore
from chatbot.users import UserStore
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

    def ask(self, question, k=3, source=None, history=None):
        self.ask_calls.append(question)
        return f"{self.tag}: answer to '{question}'"

    def ask_stream(self, question, k=3, source=None, history=None):
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
    res = client.post(
        "/auth/signup",
        json={"email": email, "password": password, "name": name, "category": category},
    )
    assert res.status_code == 200, res.text
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
        assert r.json()["sources"] == ["10. IFRS S1.pdf"]

        r = c.post("/chat", json={"question": "diversity metric?"}, headers=auth(hr_token))
        assert r.json()["answer"] == "hr: answer to 'diversity metric?'"
        assert r.json()["sources"] == ["ESG for HR.docx"]

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