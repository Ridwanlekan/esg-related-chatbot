"""The free tier must work on sign-up with no administrator action (Q2, 3.5).

The sample pack is seeded at app startup, so these tests pin the behaviour a
visitor actually depends on rather than the seeding helper in isolation.
"""

import pytest
from fastapi.testclient import TestClient

from chatbot import content_library
from chatbot.admin_store import AdminStore
from chatbot.api import create_app
from chatbot.content_library import library_root, list_documents, seed_sample_pack
from chatbot.session_store import SessionStore
from chatbot.users import SAMPLE_ORGANISATION_ID, UserStore


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Point the app at a scratch data root before anything reads it."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("INDEX_DIR", str(tmp_path / "index"))
    return tmp_path


@pytest.fixture
def pack_in_library(data_dir):
    """The launch pack, present before create_app seeds it.

    Ordered after data_dir because library_root() resolves through DATA_DIR, so
    writing the document before the root is redirected would put it in the real
    repository data directory.
    """
    from chatbot.content_library import library_root

    root = library_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "overview_of_esg.pdf").write_bytes(b"%PDF-1.4 orientation")
    return root


def build_app(tmp_path, suffix=""):
    return create_app(
        session_store=SessionStore(db_path=str(tmp_path / f"sessions{suffix}.sqlite3")),
        user_store=UserStore(db_path=str(tmp_path / f"users{suffix}.sqlite3"), secret="test-secret"),
        admin_store=AdminStore(str(tmp_path / f"admin{suffix}.sqlite3")),
        api_key="secret-admin-key",
        rate_limit=0,
    )


@pytest.fixture
def env(data_dir, pack_in_library):
    return TestClient(build_app(data_dir)), data_dir


@pytest.fixture
def env_without_pack(data_dir):
    return TestClient(build_app(data_dir)), data_dir


def test_sample_pack_seeded_on_startup(env):
    c, _ = env
    assert list_documents(SAMPLE_ORGANISATION_ID, "finance") == ["overview_of_esg.pdf"]
    assert list_documents(SAMPLE_ORGANISATION_ID, "hr") == ["overview_of_esg.pdf"]


def test_free_user_can_ask_immediately_after_signup(env):
    c, _ = env
    res = c.post("/auth/signup", json={
        "email": "visitor@x.com", "password": "password123",
        "name": "V", "category": "finance",
    })
    assert res.status_code == 200
    me = c.get("/me", headers={"authorization": f"Bearer {res.json()['token']}"}).json()
    assert me["user"]["organisation_id"] == SAMPLE_ORGANISATION_ID
    assert me["user"]["workspaces"] == ["finance"]


def test_startup_does_not_seed_customer_organisations(env):
    c, _ = env
    assert list_documents("acme", "finance") == []
    assert list_documents("_default", "finance") == []


def test_seeding_is_idempotent_across_restarts(env, data_dir):
    c, _ = env
    build_app(data_dir, suffix="-restart")  # as a restart would
    assert list_documents(SAMPLE_ORGANISATION_ID, "finance") == ["overview_of_esg.pdf"]
    assert list_documents(SAMPLE_ORGANISATION_ID, "hr") == ["overview_of_esg.pdf"]


def test_startup_survives_missing_pack(env_without_pack):
    """A deployment without the launch pack must still boot, not 500."""
    c, _ = env_without_pack
    assert c.get("/health").status_code == 200
    assert list_documents(SAMPLE_ORGANISATION_ID, "finance") == []


def test_seeding_never_touches_another_organisations_content(env):
    """The seeding pass must not reach past the sample organisation."""
    c, _ = env
    content_library.materialise(
        "acme", "finance", library_root() / "overview_of_esg.pdf",
        filename="acme-private.pdf",
    )
    seed_sample_pack(categories=("finance",))
    assert list_documents("acme", "finance") == ["acme-private.pdf"]