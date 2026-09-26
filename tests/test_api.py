from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from chatbot.api import create_app
from chatbot.session_store import SessionStore
from chatbot.vector_store import SearchResult


class FakeBot:
    def __init__(self):
        self.last_results = [
            SearchResult(
                chunk_id="c1",
                source="jupiter.txt",
                chunk_index=0,
                content="Jupiter has many moons.",
                distance=0.4,
                score=0.08,
            )
        ]
        self.store = SimpleNamespace(count=lambda: 350)
        self.ask_calls = []

    def retrieve(self, question, k=3, source=None):
        return self.last_results

    def ask(self, question, k=3, source=None, history=None, usage_sink=None):
        self.ask_calls.append({"history": history})
        return f"answer to: {question}"

    def ask_stream(self, question, k=3, source=None, history=None, usage_sink=None):
        yield "chunk one "
        yield "chunk two"

    def read_and_embed_data(self):
        return SimpleNamespace(
            documents_seen=3,
            documents_reindexed=1,
            chunks_upserted=42,
            stale_chunks_removed=0,
        )


@pytest.fixture
def client(tmp_path):
    bot = FakeBot()
    store = SessionStore(db_path=str(tmp_path / "sessions.sqlite3"))
    app = create_app(bot=bot, session_store=store, user_store=None)
    yield TestClient(app), bot, store


def test_health(client):
    c, _, _ = client
    body = c.get("/health").json()
    assert body["status"] == "ok"
    assert "finance" in body["workspaces"]


def test_chat_creates_session_and_returns_answer(client):
    c, _, store = client
    res = c.post("/chat", json={"question": "moons?"})
    body = res.json()
    assert res.status_code == 200
    assert body["answer"] == "answer to: moons?"
    assert body["sources"] == ["jupiter.txt"]
    assert store.history(body["session_id"]) == [
        {"role": "user", "content": "moons?"},
        {"role": "assistant", "content": "answer to: moons?"},
    ]


def test_chat_reuses_session_history(client):
    c, bot, store = client
    sid = store.new_id()
    store.create(sid, user_id="dev")
    store.append(sid, "user", "Moons?")
    store.append(sid, "assistant", "There are many.")
    res = c.post("/chat", json={"session_id": sid, "question": "name one"})
    history_sent = bot.ask_calls[-1]["history"]
    assert history_sent[-2:] == [
        {"role": "user", "content": "Moons?"},
        {"role": "assistant", "content": "There are many."},
    ]
    assert res.json()["session_id"] == sid


def test_chat_validates_question(client):
    c, _, _ = client
    res = c.post("/chat", json={"question": ""})
    assert res.status_code == 422


def test_greeting_answered_without_rag(client):
    c, bot, _ = client
    res = c.post("/chat", json={"question": "good morning"})
    body = res.json()
    assert res.status_code == 200
    assert body["sources"] == []
    assert any(g in body["answer"] for g in ("Good morning", "Good afternoon", "Good evening"))
    assert bot.ask_calls == []  # small talk never reaches the RAG bot


def test_regular_question_reaches_rag(client):
    c, bot, _ = client
    c.post("/chat", json={"question": "what are the moons?"})
    assert len(bot.ask_calls) == 1


def test_stream_greeting_emits_delta_and_done(client):
    c, _, store = client
    res = c.post("/chat/stream", json={"question": "hi"})
    assert res.status_code == 200
    assert "Good morning" in res.text or "Good afternoon" in res.text or "Good evening" in res.text
    assert "data: [DONE]" in res.text
    body = store.history(res.headers["X-Session-ID"])
    assert body[0] == {"role": "user", "content": "hi"}
    assert body[-1]["role"] == "assistant"


def test_stream_returns_sse_deltas(client):
    c, _, store = client
    res = c.post("/chat/stream", json={"question": "moons?"})
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    body = res.text
    assert "chunk one" in body and "chunk two" in body
    assert "data: [DONE]" in body


def test_stream_persists_messages(client):
    c, _, store = client
    sid = store.new_id()
    store.create(sid, user_id="dev")
    c.post("/chat/stream", json={"session_id": sid, "question": "moons?"})
    history = store.history(sid)
    assert history[0] == {"role": "user", "content": "moons?"}
    assert history[-1] == {"role": "assistant", "content": "chunk one chunk two"}


def test_stream_returns_session_header(client):
    c, _, _ = client
    res = c.post("/chat/stream", json={"question": "moons?"})
    assert res.headers["X-Session-ID"]


def test_stream_emits_sources_event(client):
    c, _, _ = client
    res = c.post("/chat/stream", json={"question": "moons?"})
    assert '"sources"' in res.text
    assert "jupiter.txt" in res.text


def test_list_and_load_sessions(client):
    c, _, store = client
    sid = store.new_id()
    store.create(sid, user_id="dev")
    store.append(sid, "user", "hello")
    store.append(sid, "assistant", "hi there")
    listing = c.get("/sessions").json()
    assert listing["total"] >= 1
    assert listing["sessions"][0]["session_id"] == sid
    assert listing["sessions"][0]["message_count"] == 2
    assert listing["sessions"][0]["last_message"] == "hi there"
    loaded = c.get(f"/sessions/{sid}").json()
    assert loaded["messages"] == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]


def test_list_sessions_paginates(client):
    c, _, store = client
    for i in range(5):
        sid = store.new_id()
        store.create(sid, user_id="dev")
        store.append(sid, "user", str(i))
    full = c.get("/sessions?limit=100").json()["total"]
    page = c.get("/sessions?limit=2&offset=1").json()
    assert page["total"] == full
    assert len(page["sessions"]) == 2
    assert len(c.get("/sessions?limit=0").json()["sessions"]) == 1
    assert len(c.get("/sessions?limit=9999").json()["sessions"]) <= 100


def test_session_retention_cap(tmp_path):
    store = SessionStore(db_path=str(tmp_path / "s.sqlite3"), retention=3)
    for i in range(6):
        store.append("s" + str(i), "user", str(i))
    rows = store.list_sessions(limit=100)
    assert rows["total"] <= 3


def test_ui_page_served(client):
    c, _, _ = client
    res = c.get("/ui")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    assert "ESG Workspace Chatbot" in res.text
    assert "/chat/stream" in res.text
    assert "Create account" in res.text


def test_search_returns_items(client):
    c, _, _ = client
    res = c.post("/search", json={"question": "moons?"})
    body = res.json()
    assert res.status_code == 200
    assert body["results"][0]["source"] == "jupiter.txt"
    assert body["results"][0]["similarity"] == 0.6
    assert body["results"][0]["score"] == 0.08


def test_ingest_reports_stats(client):
    c, _, _ = client
    body = c.post("/ingest").json()
    assert body == {
        "documents_seen": 3,
        "documents_reindexed": 1,
        "chunks_upserted": 42,
        "stale_chunks_removed": 0,
        "documents_failed": 0,
        "duration_seconds": body["duration_seconds"],
    }
    assert body["duration_seconds"] >= 0


def test_ingest_status_reports_progress(client):
    c, _, _ = client
    res = c.get("/ingest/status")
    assert res.status_code == 200
    body = res.json()
    assert set(body) >= {"running", "total_files", "processed_files", "current_file"}
    assert body["running"] is False


def test_delete_session(client):
    c, _, store = client
    sid = store.new_id()
    store.create(sid, user_id="dev")
    store.append(sid, "user", "hi")
    c.delete(f"/sessions/{sid}")
    assert store.history(sid) == []