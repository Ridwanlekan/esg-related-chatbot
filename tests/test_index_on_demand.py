"""A copied document has to become an askable one, with nobody reindexing it.

Seeding materialises the free tier's pack at startup, and the admin endpoints
reindex when a document is assigned - but no path indexes a workspace for the
visitor who arrives first on a fresh deployment. That gap answered a question
with an engineering message. These tests pin the fix: the bot indexes itself
on first use, and a workspace that genuinely holds nothing says so in words
someone can act on.
"""

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from chatbot import content_library
from chatbot.admin_store import AdminStore
from chatbot.api import create_app
from chatbot.rag import EMBEDDING_DIM, RAGBot
from chatbot.session_store import SessionStore
from chatbot.users import SAMPLE_ORGANISATION_ID, UserStore

DIM = EMBEDDING_DIM


def _embed(texts):
    """Deterministic unit-ish vectors: no model, no network, stable per text."""
    out = np.zeros((len(texts), DIM), dtype=np.float32)
    for i, text in enumerate(texts):
        seed = hashlib.sha256(text.encode()).digest()
        rng = np.frombuffer(seed * (DIM // len(seed) + 1), dtype=np.uint8)[:DIM]
        out[i] = rng.astype(np.float32) / 255.0
    return out


@pytest.fixture
def rag(monkeypatch):
    """RAGBot with the embedding model and the LLM replaced by stubs.

    The model is loaded in __init__, so every construction in a test would
    otherwise pay for it; and the answer half of ask() needs a provider that
    tests never configure.
    """
    monkeypatch.setattr("chatbot.rag.SentenceTransformer", lambda *a, **k: object())
    monkeypatch.setattr(
        "chatbot.rag.AutoTokenizer",
        SimpleNamespace(from_pretrained=lambda *a, **k: object()),
    )
    monkeypatch.setattr(RAGBot, "_embed", lambda self, texts: _embed(list(texts)))
    monkeypatch.setattr(
        RAGBot,
        "_complete",
        lambda self, kind, messages, **kw: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="stubbed answer"))]
        ),
    )
    # Off by default in tests: both would load a second model to rank three
    # chunks that have already been retrieved.
    monkeypatch.setenv("RERANKER_ENABLED", "0")
    monkeypatch.setenv("MMR_LAMBDA", "0")
    return monkeypatch


def _bot(tmp_path, rag, *, data_dir=None):
    folder = data_dir or (tmp_path / "docs")
    folder.mkdir(parents=True, exist_ok=True)
    return RAGBot(
        store_path=str(tmp_path / "vectors.sqlite3"),
        data_dir=str(folder),
        system_prompt="Answer from the documents.",
    )


def test_the_first_question_indexes_the_workspace(tmp_path, rag):
    """Retrieve on an empty store ingests first instead of refusing."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "notes.txt").write_text(
        "Carbon emissions targets are reported annually under ISSB S2. "
        "Scope 1 covers direct emissions from owned sources.",
        encoding="utf-8",
    )
    bot = RAGBot(
        store_path=str(tmp_path / "vectors.sqlite3"),
        data_dir=str(docs),
        system_prompt="Answer from the documents.",
    )
    assert bot.store.count() == 0

    results = bot.retrieve("carbon emissions targets")

    assert results, "an indexed workspace must answer its own question"
    assert bot.store.count() > 0
    assert results[0].source == "notes.txt"


def test_a_second_question_does_not_reingest(tmp_path, rag):
    """The empty store is the only trigger: a warm index costs nothing."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "notes.txt").write_text("Governance failures start at the board.", encoding="utf-8")
    bot = RAGBot(
        store_path=str(tmp_path / "vectors.sqlite3"),
        data_dir=str(docs),
        system_prompt="Answer from the documents.",
    )
    bot.retrieve("governance")
    indexed = bot.store.count()

    def explode(*a, **k):
        raise AssertionError("a warm store must not re-ingest")

    bot.ingest = explode
    assert bot.retrieve("board")


def test_a_workspace_with_no_documents_says_so(tmp_path, rag):
    """Not the call that would have fixed it - a reason the reader can use."""
    bot = _bot(tmp_path, rag)
    with pytest.raises(RuntimeError) as exc:
        bot.retrieve("anything at all")

    message = str(exc.value)
    assert "no documents" in message
    assert "ingest()" not in message  # never hand an operator a code path


def test_a_stored_answer_is_still_produced_end_to_end(tmp_path, rag):
    """ask() must reach the model, which only works once retrieval did."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "notes.txt").write_text("Biodiversity net gain is measured per site.", encoding="utf-8")
    bot = RAGBot(
        store_path=str(tmp_path / "vectors.sqlite3"),
        data_dir=str(docs),
        system_prompt="Answer from the documents.",
    )
    assert bot.ask("how is biodiversity measured?") == "stubbed answer"
    assert bot.last_results and bot.last_results[0].source == "notes.txt"


# ---- The same gap, seen through the API -----------------------------------

@pytest.fixture
def data_env(tmp_path, rag, monkeypatch):
    """A real app wired to real organisation workspaces, stubbed model.

    bot=None is the point: org bots are then built through get_org_bot, which
    is the path a visitor takes.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("INDEX_DIR", str(tmp_path / ".index"))
    app = create_app(
        bot=None,
        session_store=SessionStore(db_path=str(tmp_path / "sessions.sqlite3")),
        user_store=UserStore(db_path=str(tmp_path / "users.sqlite3"), secret="s"),
        admin_store=AdminStore(str(tmp_path / "admin.sqlite3")),
        api_key="secret-admin-key",
        rate_limit=0,
    )
    return TestClient(app)


def _free_token(client, category="finance"):
    res = client.post("/auth/signup", json={
        "email": f"visitor-{category}@x.com", "password": "password123",
        "name": "Visitor", "category": category,
    })
    assert res.status_code == 200, res.text
    client.app.state.user_store.set_verified(res.json()["user"]["id"])
    return res.json()["token"]


def _seed_workspace_text(organisation_id, category, body, filename="notes.txt"):
    """Materialise a plain-text document into a workspace's served copy.

    Staged outside every workspace so nothing but the copy is ever indexed.
    """
    staging = Path(os.environ["DATA_DIR"]) / "staged"
    staging.mkdir(parents=True, exist_ok=True)
    source = staging / filename
    source.write_text(body, encoding="utf-8")
    content_library.materialise(organisation_id, category, source, filename=filename)


def test_a_free_visitor_can_ask_before_anything_was_indexed(data_env):
    """The free tier's promise: sign up, ask, get an answer (Q2, 3.5)."""
    c = data_env
    _seed_workspace_text(
        SAMPLE_ORGANISATION_ID, "finance",
        "The five capitals are financial, manufactured, human, social and "
        "natural. ESG covers environmental, social and governance factors.",
    )
    token = _free_token(c, "finance")
    headers = {"authorization": f"Bearer {token}"}

    res = c.post("/chat", json={"question": "what are the five capitals?"}, headers=headers)

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["answer"] == "stubbed answer"
    assert [s["source"] for s in body["sources"]] == ["notes.txt"]


def test_a_paid_organisation_with_nothing_assigned_explains_itself(data_env):
    """Before an assignment, a customer gets a reason rather than a stack."""
    c = data_env
    store = c.app.state.user_store
    store.create_organisation("acme", "Acme Ltd")
    user = store.create_user(
        "owner@acme.com", "password123", "Owner", "finance",
        organisation_id="acme", verified=True,
    )
    headers = {"authorization": f"Bearer {store.token_for(user)}"}

    res = c.post("/chat", json={"question": "what are our targets?"}, headers=headers)

    assert res.status_code == 503, res.text
    assert "no documents" in res.json()["detail"]
    assert "ingest" not in res.json()["detail"]
