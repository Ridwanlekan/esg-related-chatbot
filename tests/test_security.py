from fastapi.testclient import TestClient

from chatbot.api import create_app
from chatbot.security import RateLimiter
from chatbot.session_store import SessionStore


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


def _build(api_key, **kw):
    app = create_app(
        bot=FakeBot(),
        session_store=SessionStore(db_path=":memory:"),
        api_key=api_key,
        rate_limit=kw.pop("rate_limit", 0),
        rate_window=60,
        **kw,
    )
    return TestClient(app)


class TestAuth:
    def test_protected_endpoint_rejects_without_key(self):
        c = _build("sekret")
        assert c.post("/chat", json={"question": "hi"}).status_code == 401
        assert c.get("/sessions").status_code == 401

    def test_protected_endpoint_rejects_wrong_key(self):
        c = _build("sekret")
        h = {"authorization": "Bearer wrong"}
        assert c.post("/chat", json={"question": "hi"}, headers=h).status_code == 401

    def test_protected_endpoint_accepts_valid_key(self):
        c = _build("sekret")
        h = {"authorization": "Bearer sekret"}
        res = c.post("/chat", json={"question": "hi"}, headers=h)
        assert res.status_code == 200
        assert res.json()["answer"] == "answer to: hi"

    def test_public_endpoints_need_no_key(self):
        c = _build("sekret")
        assert c.get("/health").status_code == 200
        assert c.get("/ui").status_code == 200

    def test_no_key_configured_means_open(self):
        c = _build(None)
        assert c.post("/chat", json={"question": "hi"}).status_code == 200


class TestRateLimit:
    def test_blocks_after_cap(self):
        c = _build(None, rate_limit=2)
        for _ in range(2):
            assert c.post("/chat", json={"question": "hi"}).status_code == 200
        assert c.post("/chat", json={"question": "hi"}).status_code == 429

    def test_search_is_limited_too(self):
        c = _build(None, rate_limit=1)
        assert c.post("/search", json={"question": "jupiter"}).status_code == 200
        assert c.post("/search", json={"question": "jupiter"}).status_code == 429

    def test_ingest_not_rate_limited(self):
        c = _build(None, rate_limit=0)
        assert c.post("/ingest").status_code == 200


class TestCors:
    def test_origin_blocked_by_default(self):
        c = _build(None)
        res = c.get("/health", headers={"origin": "https://evil.example"})
        assert "access-control-allow-origin" not in res.headers

    def test_configured_origin_allowed(self):
        c = _build(None, cors_origins=["https://app.example"])
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
            rate_limit=0,
        )
        return TestClient(app)

    def test_chat_returns_503_without_crash(self):
        c = self._client()
        res = c.post("/chat", json={"question": "hi"})
        assert res.status_code == 503
        assert "ingest" in res.json()["detail"]

    def test_stream_emits_error_event_without_crash(self):
        c = self._client()
        res = c.post("/chat/stream", json={"question": "hi"})
        assert res.status_code == 200
        assert '"error"' in res.text
        assert "data: [DONE]" in res.text

    def test_search_returns_503_without_crash(self):
        c = self._client()
        res = c.post("/search", json={"question": "jupiter"})
        assert res.status_code == 503