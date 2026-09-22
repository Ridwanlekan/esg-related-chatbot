import pytest
from fastapi.testclient import TestClient

from chatbot.api import create_app
from chatbot.security import RateLimiter
from chatbot.session_store import SessionStore
from chatbot.users import UserStore


class FakeBot:
    def ask(self, question, k=3, source=None, history=None):
        return f"answer to: {question}"

    def ask_stream(self, question, k=3, source=None, history=None):
        yield "chunk one "

    def retrieve(self, question, k=3, source=None):
        return []

    def read_and_embed_data(self):
        from types import SimpleNamespace

        return SimpleNamespace(
            documents_seen=3,
            documents_reindexed=1,
            chunks_upserted=42,
            stale_chunks_removed=0,
        )

    @property
    def store(self):
        from types import SimpleNamespace

        return SimpleNamespace(count=lambda: 350)


def _build(api_key=None, open_users=True, secret="test-secret", **kw):
    app = create_app(
        bot=FakeBot(),
        session_store=SessionStore(db_path=":memory:"),
        api_key=api_key,
        user_store=None if open_users else UserStore(db_path=":memory:", secret=secret),
        rate_limit=kw.pop("rate_limit", 0),
        rate_window=60,
        **kw,
    )
    return TestClient(app)


def _token(client):
    res = client.post(
        "/auth/signup",
        json={"email": "u@corp.com", "password": "password123", "name": "U", "category": "finance"},
    )
    assert res.status_code == 200
    return res.json()["token"]


class TestUserAuth:
    def test_user_endpoints_require_token(self):
        c = _build(open_users=False)
        assert c.post("/chat", json={"question": "hi"}).status_code == 401
        assert c.get("/sessions").status_code == 401

    def test_invalid_token_rejected(self):
        c = _build(open_users=False)
        h = {"authorization": "Bearer not-a-jwt"}
        assert c.post("/chat", json={"question": "hi"}, headers=h).status_code == 401

    def test_valid_token_accepted(self):
        c = _build(open_users=False)
        h = {"authorization": "Bearer " + _token(c)}
        res = c.post("/chat", json={"question": "moons?"}, headers=h)
        assert res.status_code == 200
        assert res.json()["answer"] == "answer to: moons?"

    def test_public_endpoints_need_no_token(self):
        c = _build(open_users=False)
        assert c.get("/health").status_code == 200
        assert c.get("/ui").status_code == 200

    def test_open_dev_mode_when_no_user_store(self):
        c = _build(open_users=True)
        assert c.post("/chat", json={"question": "moons?"}).status_code == 200


class TestAdminApiKey:
    def test_ingest_protected_with_key_set(self):
        c = _build(api_key="sekret")
        assert c.post("/ingest").status_code == 401
        assert c.get("/ingest/status").status_code == 401
        h = {"authorization": "Bearer sekret"}
        assert c.post("/ingest", headers=h).status_code == 200
        assert c.get("/ingest/status", headers=h).status_code == 200

    def test_ingest_open_when_no_key_configured(self):
        c = _build()
        assert c.post("/ingest").status_code == 200


class TestRateLimit:
    def test_blocks_after_cap(self):
        c = _build(rate_limit=2)
        for _ in range(2):
            assert c.post("/chat", json={"question": "hi"}).status_code == 200
        assert c.post("/chat", json={"question": "hi"}).status_code == 429

    def test_search_is_limited_too(self):
        c = _build(rate_limit=1)
        assert c.post("/search", json={"question": "jupiter"}).status_code == 200
        assert c.post("/search", json={"question": "jupiter"}).status_code == 429

    def test_ingest_not_rate_limited(self):
        c = _build(rate_limit=0)
        assert c.post("/ingest").status_code == 200


class TestCors:
    def test_origin_blocked_by_default(self):
        c = _build()
        res = c.get("/health", headers={"origin": "https://evil.example"})
        assert "access-control-allow-origin" not in res.headers

    def test_configured_origin_allowed(self):
        c = _build(cors_origins=["https://app.example"])
        res = c.get("/health", headers={"origin": "https://app.example"})
        assert res.headers["access-control-allow-origin"] == "https://app.example"


def test_rate_limiter_sliding_window():
    lim = RateLimiter(max_requests=2, window_seconds=60)
    assert lim.allow("a") and lim.allow("a")
    assert not lim.allow("a")
    lim.reset("a")
    assert lim.allow("a")


class EmptyBot:
    def ask(self, question, k=3, source=None, history=None):
        raise RuntimeError("call ingest() before retrieve()")

    def ask_stream(self, question, k=3, source=None, history=None):
        raise RuntimeError("call ingest() before retrieve()")
        yield

    def retrieve(self, question, k=3, source=None):
        raise RuntimeError("call ingest() before retrieve()")


class TestGracefulErrors:
    def _client(self):
        app = create_app(
            bot=EmptyBot(),
            session_store=SessionStore(db_path=":memory:"),
            user_store=None,
            rate_limit=0,
        )
        return TestClient(app)

    def test_chat_returns_503_without_crash(self):
        c = self._client()
        res = c.post("/chat", json={"question": "moons?"})
        assert res.status_code == 503
        assert "ingest" in res.json()["detail"]

    def test_stream_emits_error_event_without_crash(self):
        c = self._client()
        res = c.post("/chat/stream", json={"question": "moons?"})
        assert res.status_code == 200
        assert '"error"' in res.text
        assert "data: [DONE]" in res.text

    def test_search_returns_503_without_crash(self):
        c = self._client()
        res = c.post("/search", json={"question": "jupiter"})
        assert res.status_code == 503