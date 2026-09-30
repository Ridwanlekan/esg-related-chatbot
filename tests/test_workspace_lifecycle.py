"""Runtime workspace lifecycle: create -> upload -> delete, in config-based mode.

Every other admin test passes an explicit ``workspaces={...}`` registry, so
``app.state.workspace_config`` is ``{}`` and ``get_bot()`` always resolves from
the registry. Production runs config-based (bot=None, workspaces=None), where
the config map is built once at startup. These tests cover that path, which is
how a workspace created through the admin UI used to become unusable.
"""

import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from chatbot.admin_store import AdminStore
from chatbot.api import create_app
from chatbot.session_store import SessionStore
from chatbot.users import UserStore


class FakeBot:
    def __init__(self, category):
        self.category = category
        self.ingest_calls = 0
        self.store = SimpleNamespace(count=lambda: 0)

    def read_and_embed_data(self):
        self.ingest_calls += 1
        return SimpleNamespace(
            documents_seen=1,
            documents_reindexed=1,
            chunks_upserted=3,
            stale_chunks_removed=0,
            documents_failed=0,
        )

    def ask(self, question=None, k=3, source=None, history=None, **kw):
        return f"reply from {self.category}"


@pytest.fixture
def config_env(tmp_path, monkeypatch):
    """Config-based app: no injected bot, no injected registry."""
    os.environ["DATA_DIR"] = str(tmp_path / "data")
    os.environ["INDEX_DIR"] = str(tmp_path / ".index")
    os.environ["WORKSPACES"] = "finance"

    # make_workspace_bot would construct a real RAGBot (loading embedding
    # models); the point of these tests is routing, not embedding.
    monkeypatch.setattr("chatbot.api.make_workspace_bot",
                        lambda category, config=None: FakeBot(category))
    app = create_app(
        session_store=SessionStore(db_path=str(tmp_path / "s.sqlite3")),
        user_store=UserStore(db_path=str(tmp_path / "u.sqlite3"), secret="s"),
        workspaces=None,
        admin_store=AdminStore(str(tmp_path / "w.sqlite3")),
        api_key="k",
        rate_limit=0,
    )
    return TestClient(app), tmp_path


def auth():
    return {"authorization": "Bearer k"}


def create_ws(c, category, label="My Workspace"):
    res = c.post("/admin/workspaces", headers=auth(),
                 json={"category": category, "label": label})
    assert res.status_code == 200, res.text
    return res.json()


def upload(c, category, name="cv.pdf", content=b"%PDF-1.4 fake"):
    return c.post("/admin/upload", headers=auth(),
                  data={"category": category},
                  files=[("files", (name, content, "application/octet-stream"))])


def test_new_workspace_is_immediately_uploadable(config_env):
    """Regression: a workspace created at runtime must be resolvable by get_bot.

    Symptom: the file landed in data/<ws>/ but the request then failed with
    403 "Unknown workspace", because app.state.workspace_config was still the
    startup snapshot.
    """
    c, _ = config_env
    create_ws(c, "ridwan_cv")

    res = upload(c, "ridwan_cv")
    assert res.status_code == 200, res.text
    assert res.json()["saved_files"] == ["cv.pdf"]


def test_new_workspace_appears_in_workspace_list(config_env):
    c, _ = config_env
    create_ws(c, "ridwan_cv")

    listed = c.get("/workspaces").json()
    assert "ridwan_cv" in {w["category"] for w in listed}


def test_document_delete_in_new_workspace_succeeds(config_env):
    """delete_document unlinks the file and then calls get_bot(); if get_bot
    cannot resolve the runtime workspace the file is already destroyed."""
    c, tmp = config_env
    create_ws(c, "ridwan_cv")
    assert upload(c, "ridwan_cv").status_code == 200

    res = c.post("/admin/documents/delete", headers=auth(),
                 json={"category": "ridwan_cv", "filename": "cv.pdf"})
    assert res.status_code == 200, res.text
    assert not (tmp / "data" / "ridwan_cv" / "cv.pdf").exists()


def test_deleted_workspace_is_evicted_from_runtime_state(config_env):
    """A removed workspace must not stay resolvable, and its cached bot must
    not be handed out again."""
    c, _ = config_env
    create_ws(c, "ridwan_cv")
    assert upload(c, "ridwan_cv").status_code == 200

    res = c.delete("/admin/workspaces/ridwan_cv?purge=true", headers=auth())
    assert res.status_code == 200, res.text

    assert "ridwan_cv" not in {w["category"] for w in c.get("/workspaces").json()}
    assert upload(c, "ridwan_cv").status_code == 422


def test_existing_workspace_still_works(config_env):
    """Guards against the sync dropping or corrupting startup workspaces."""
    c, _ = config_env
    res = upload(c, "finance")
    assert res.status_code == 200, res.text


# ---- deletion actually removes the data on disk ------------------------------


def test_delete_workspace_refuses_to_drop_documents_silently(config_env):
    """Regression: deleting a workspace used to leave data/<ws>/ orphaned."""
    c, tmp = config_env
    create_ws(c, "ridwan_cv")
    assert upload(c, "ridwan_cv").status_code == 200

    res = c.delete("/admin/workspaces/ridwan_cv", headers=auth())
    assert res.status_code == 409, res.text
    # Nothing destroyed, and the workspace is still registered.
    assert (tmp / "data" / "ridwan_cv" / "cv.pdf").exists()
    assert "ridwan_cv" in {w["category"] for w in c.get("/workspaces").json()}


def test_delete_workspace_with_purge_removes_folder_and_index(config_env):
    c, tmp = config_env
    create_ws(c, "ridwan_cv")
    assert upload(c, "ridwan_cv").status_code == 200
    index = tmp / ".index" / "vectors_ridwan_cv.sqlite3"
    index.write_bytes(b"stale index")

    res = c.delete("/admin/workspaces/ridwan_cv?purge=true", headers=auth())
    assert res.status_code == 200, res.text
    assert not (tmp / "data" / "ridwan_cv").exists(), "data folder survived purge"
    assert not index.exists(), "vector index survived purge"
    assert "ridwan_cv" not in {w["category"] for w in c.get("/workspaces").json()}


def test_purge_removes_shared_links_without_touching_the_shared_store(config_env):
    """A workspace's hardlinks must go, but the canonical _shared copy and its
    registry rows must survive so another workspace keeps serving the document."""
    c, tmp = config_env
    create_ws(c, "ridwan_cv")
    pub = c.post("/admin/shared-documents", headers=auth(), data={"categories": "ridwan_cv"},
                 files=[("files", ("policy.pdf", b"%PDF shared", "application/octet-stream"))])
    assert pub.status_code == 200, pub.text

    res = c.delete("/admin/workspaces/ridwan_cv?purge=true", headers=auth())
    assert res.status_code == 200, res.text

    data = tmp / "data"
    assert not (data / "ridwan_cv" / "policy.pdf").exists(), "stale hardlink survived"
    assert (data / "_shared" / "policy.pdf").exists(), "canonical copy was deleted"
    listed = c.get("/admin/shared-documents", headers=auth()).json()
    assert listed["documents"][0]["filename"] == "policy.pdf"
    assert listed["documents"][0]["targets"] == []


def test_delete_built_in_workspace_still_rejected(config_env):
    c, _ = config_env
    assert c.delete("/admin/workspaces/finance?purge=true", headers=auth()).status_code == 403


def test_delete_empty_workspace_needs_no_purge(config_env):
    """An empty workspace has nothing to lose, so it deletes without a second step."""
    c, tmp = config_env
    create_ws(c, "ridwan_cv")
    os.makedirs(tmp / "data" / "ridwan_cv", exist_ok=True)

    res = c.delete("/admin/workspaces/ridwan_cv", headers=auth())
    assert res.status_code == 200, res.text
    assert not (tmp / "data" / "ridwan_cv").exists()


def test_listed_document_count_matches_the_delete_guard(config_env):
    """The admin prompt states the count from data_file_count, so it has to be
    counted exactly like the guard that decides 409-vs-purge."""
    c, tmp = config_env
    create_ws(c, "ridwan_cv")
    assert upload(c, "ridwan_cv").status_code == 200
    # Nested, so a non-recursive count would under-report.
    nested = tmp / "data" / "ridwan_cv" / "2026" / "q1"
    nested.mkdir(parents=True)
    (nested / "report.pdf").write_bytes(b"%PDF nested")

    row = next(w for w in c.get("/admin/workspaces", headers=auth()).json()["workspaces"]
               if w["category"] == "ridwan_cv")
    assert row["data_file_count"] == 2, "listed count disagrees with the guard"
    assert c.delete("/admin/workspaces/ridwan_cv", headers=auth()).status_code == 409
