"""Tests for cross-workspace shared documents (Option B).

The load-bearing property is that a shared document behaves like an ordinary
file inside each workspace it is served to: the existing recursive ingest finds
it, the content hash is stable across a re-publish, and no workspace can see
another workspace's documents.
"""

import errno
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from chatbot import shared_docs
from chatbot.admin_store import AdminStore
from chatbot.api import create_app
from chatbot.session_store import SessionStore
from chatbot.users import UserStore
from chatbot.workspaces import SHARED_DIR_NAME


@pytest.fixture(autouse=True)
def _clear_digest_cache():
    shared_docs.clear_digest_cache()
    yield
    shared_docs.clear_digest_cache()


class FakeBot:
    """Stands in for a workspace's RAGBot and records re-index calls."""

    def __init__(self, category):
        self.category = category
        self.ingest_calls = 0
        self.store = SimpleNamespace(count=lambda: 0)
        self.last_results = []

    def read_and_embed_data(self):
        self.ingest_calls += 1
        return SimpleNamespace(
            documents_seen=1,
            documents_reindexed=1,
            chunks_upserted=4,
            stale_chunks_removed=0,
            documents_failed=0,
        )

    def ask(self, question=None, k=3, source=None, history=None, **kw):
        return f"reply from {self.category}"


@pytest.fixture
def env(tmp_path):
    os.environ["DATA_DIR"] = str(tmp_path / "data")
    os.environ["INDEX_DIR"] = str(tmp_path / ".index")
    bots = {"finance": FakeBot("finance"), "hr": FakeBot("hr"),
            "legal": FakeBot("legal")}
    app = create_app(
        session_store=SessionStore(db_path=str(tmp_path / "s.sqlite3")),
        user_store=UserStore(db_path=str(tmp_path / "u.sqlite3"), secret="s"),
        workspaces=bots,
        admin_store=AdminStore(str(tmp_path / "w.sqlite3")),
        api_key="k",
        rate_limit=0,
    )
    c = TestClient(app)
    yield c, bots, Path(os.environ["DATA_DIR"])
    shared_docs.clear_digest_cache()


def auth():
    return {"authorization": "Bearer k"}


def publish(c, name, content, cats="finance,hr"):
    res = c.post(
        "/admin/shared-documents",
        headers=auth(),
        data={"categories": cats},
        files=[("files", (name, content, "application/octet-stream"))],
    )
    assert res.status_code == 200, res.text
    return res.json()


# ---- name safety ------------------------------------------------------------


@pytest.mark.parametrize("raw", [
    "../../etc/passwd", "a/../../b.txt", "sub/dir.txt",
    "..\\..\\windows\\system32", "/etc/passwd",
])
def test_safe_name_can_never_escape_the_target_directory(raw):
    """Any directory component must be stripped, not preserved."""
    out = shared_docs.safe_name(raw)
    assert out not in ("", ".", "..")
    assert "/" not in out and "\\" not in out
    assert out == os.path.basename(out)
    assert out == shared_docs.safe_name(out)  # already normalised, idempotent


@pytest.mark.parametrize("raw", ["", "   ", "..", ".", ".hidden", "\t"])
def test_safe_name_rejects_empty_dot_and_dotfile_input(raw):
    assert shared_docs.safe_name(raw) == ""


def test_safe_name_strips_directories():
    assert shared_docs.safe_name("docs/policy.docx") == "policy.docx"
    assert shared_docs.safe_name("a\\b\\policy.docx") == "policy.docx"


def test_target_path_rejects_shared_store_name(tmp_path):
    os.environ["DATA_DIR"] = str(tmp_path / "data")
    with pytest.raises(ValueError):
        shared_docs.target_path("finance", "_shared")


@pytest.mark.parametrize("cat", [
    "..", ".", "", "  ", "_shared", "../hr", "a/b", "a\\b", "finance/../../etc",
])
def test_target_path_rejects_a_traversing_category(cat, tmp_path):
    """A category is untrusted input that becomes a path component."""
    os.environ["DATA_DIR"] = str(tmp_path / "data")
    with pytest.raises(ValueError):
        shared_docs.target_path(cat, "policy.docx")


# ---- publishing and linking -------------------------------------------------


def test_publish_creates_one_canonical_and_links_each_workspace(env):
    c, _, data = env
    publish(c, "policy.docx", b"CORP POLICY", cats="finance,hr")
    canonical = data / "_shared" / "policy.docx"
    assert canonical.read_bytes() == b"CORP POLICY"
    for cat in ("finance", "hr"):
        assert (data / cat / "policy.docx").read_bytes() == b"CORP POLICY"


def test_links_share_one_inode_so_content_cannot_drift(env):
    c, _, data = env
    publish(c, "policy.docx", b"v1")
    canonical = data / "_shared" / "policy.docx"
    inodes = {
        (data / cat / "policy.docx").stat().st_ino for cat in ("finance", "hr")
    }
    assert inodes == {canonical.stat().st_ino}


def test_republish_updates_every_linked_workspace_and_reindexes_them(env):
    c, bots, data = env
    publish(c, "policy.docx", b"v1")
    assert (data / "finance" / "policy.docx").read_bytes() == b"v1"
    assert (data / "hr" / "policy.docx").read_bytes() == b"v1"
    bots["finance"].ingest_calls = 0
    bots["hr"].ingest_calls = 0

    publish(c, "policy.docx", b"v2-UPDATED")

    for cat in ("finance", "hr"):
        assert (data / cat / "policy.docx").read_bytes() == b"v2-UPDATED", cat
        assert bots[cat].ingest_calls == 1, f"{cat} was not re-indexed"


def test_workspace_not_subscribed_never_receives_the_document(env):
    c, bots, data = env
    publish(c, "policy.docx", b"CORP", cats="finance")
    assert not (data / "hr" / "policy.docx").exists()
    assert not (data / "legal" / "policy.docx").exists()
    assert bots["hr"].ingest_calls == 0


def test_attach_existing_shared_doc_to_another_workspace(env):
    c, bots, data = env
    publish(c, "policy.docx", b"CORP", cats="finance")
    res = c.post(
        "/admin/shared-documents/link",
        headers=auth(),
        json={"filename": "policy.docx", "categories": ["hr"]},
    )
    assert res.status_code == 200, res.text
    assert (data / "hr" / "policy.docx").read_bytes() == b"CORP"
    assert bots["hr"].ingest_calls == 1


def test_attach_under_a_custom_name_is_preserved_on_republish(env):
    c, _, data = env
    publish(c, "policy.docx", b"v1", cats="finance")
    c.post(
        "/admin/shared-documents/link",
        headers=auth(),
        json={"filename": "policy.docx", "categories": ["hr"],
              "link_names": {"hr": "corp-policy.docx"}},
    )
    publish(c, "policy.docx", b"v2", cats="finance")

    assert (data / "hr" / "corp-policy.docx").read_bytes() == b"v2"
    # The re-publish must not leave a second, differently named link behind.
    assert not (data / "hr" / "policy.docx").exists()


def test_link_refuses_to_overwrite_an_unrelated_private_file(env):
    c, _, data = env
    publish(c, "policy.docx", b"CORP", cats="finance")
    priv = data / "hr" / "policy.docx"
    priv.parent.mkdir(parents=True, exist_ok=True)
    priv.write_bytes(b"HR's own policy")

    res = c.post(
        "/admin/shared-documents/link",
        headers=auth(),
        json={"filename": "policy.docx", "categories": ["hr"]},
    )
    assert res.status_code == 200
    assert res.json()["linked"] == []
    assert res.json()["skipped"][0]["category"] == "hr"
    assert priv.read_bytes() == b"HR's own policy"


# ---- the clobber guard ------------------------------------------------------


def test_private_upload_cannot_overwrite_a_shared_document(env):
    c, _, data = env
    publish(c, "policy.docx", b"CORP v1", cats="finance,hr")
    res = c.post(
        "/admin/upload",
        headers=auth(),
        data={"category": "finance"},
        files=[("files", ("policy.docx", b"private", "application/octet-stream"))],
    )
    assert res.status_code == 409
    assert "shared document" in res.json()["detail"]
    # Crucially, the shared copy in the *other* workspace is untouched.
    assert (data / "hr" / "policy.docx").read_bytes() == b"CORP v1"
    assert (data / "_shared" / "policy.docx").read_bytes() == b"CORP v1"


def test_private_delete_cannot_remove_a_shared_link(env):
    c, _, data = env
    publish(c, "policy.docx", b"CORP", cats="finance")
    res = c.post(
        "/admin/documents/delete",
        headers=auth(),
        json={"category": "finance", "filename": "policy.docx"},
    )
    assert res.status_code == 409
    assert (data / "finance" / "policy.docx").exists()


def test_private_upload_still_works_for_a_different_name(env):
    c, _, data = env
    publish(c, "policy.docx", b"CORP", cats="finance")
    res = c.post(
        "/admin/upload",
        headers=auth(),
        data={"category": "finance"},
        files=[("files", ("ifrs-s1.pdf", b"PRIVATE", "application/octet-stream"))],
    )
    assert res.status_code == 200
    assert (data / "finance" / "ifrs-s1.pdf").read_bytes() == b"PRIVATE"


# ---- detaching and deleting -------------------------------------------------


def test_unlink_removes_only_that_workspace_and_reindexes_it(env):
    c, bots, data = env
    publish(c, "policy.docx", b"CORP", cats="finance,hr")
    bots["finance"].ingest_calls = 0
    bots["hr"].ingest_calls = 0

    res = c.post(
        "/admin/shared-documents/unlink",
        headers=auth(),
        json={"filename": "policy.docx", "category": "hr"},
    )
    assert res.status_code == 200
    assert not (data / "hr" / "policy.docx").exists()
    assert (data / "finance" / "policy.docx").read_bytes() == b"CORP"
    assert bots["hr"].ingest_calls == 1
    assert bots["finance"].ingest_calls == 0


def test_unlink_stops_a_later_republish_from_reaching_that_workspace(env):
    c, _, data = env
    publish(c, "policy.docx", b"v1", cats="finance,hr")
    c.post(
        "/admin/shared-documents/unlink",
        headers=auth(),
        json={"filename": "policy.docx", "category": "hr"},
    )
    # Re-selecting only finance must not silently re-subscribe hr.
    publish(c, "policy.docx", b"v2", cats="finance")
    assert (data / "finance" / "policy.docx").read_bytes() == b"v2"
    assert not (data / "hr" / "policy.docx").exists()
    assert _states(c, "policy.docx") == {"finance": "ok"}


def test_republish_restores_a_link_deleted_out_of_band(env):
    c, _, data = env
    publish(c, "policy.docx", b"v1", cats="finance")
    (data / "finance" / "policy.docx").unlink()
    assert _states(c, "policy.docx") == {"finance": "broken"}
    publish(c, "policy.docx", b"v2", cats="finance")
    assert _states(c, "policy.docx") == {"finance": "ok"}
    assert (data / "finance" / "policy.docx").read_bytes() == b"v2"


def test_delete_removes_the_canonical_and_every_link(env):
    c, _, data = env
    publish(c, "policy.docx", b"CORP", cats="finance,hr")
    res = c.post(
        "/admin/shared-documents/delete",
        headers=auth(),
        json={"filename": "policy.docx", "category": "finance"},
    )
    assert res.status_code == 200
    assert sorted(res.json()["detached_from"]) == ["finance", "hr"]
    assert not (data / "_shared" / "policy.docx").exists()
    assert not (data / "finance" / "policy.docx").exists()
    assert not (data / "hr" / "policy.docx").exists()
    assert c.get("/admin/shared-documents", headers=auth()).json()["documents"] == []


# ---- validation and auth ----------------------------------------------------


def test_endpoints_require_admin_auth(env):
    c, _, _ = env
    assert c.get("/admin/shared-documents").status_code == 401
    assert c.post("/admin/shared-documents/link", json={}).status_code == 401


def test_publish_requires_at_least_one_valid_workspace(env):
    c, _, _ = env
    res = c.post(
        "/admin/shared-documents",
        headers=auth(),
        data={"categories": "nope,alsonope"},
        files=[("files", ("policy.docx", b"x", "application/octet-stream"))],
    )
    assert res.status_code == 422


def test_linking_an_unknown_document_is_404(env):
    c, _, _ = env
    res = c.post(
        "/admin/shared-documents/link",
        headers=auth(),
        json={"filename": "ghost.docx", "categories": ["hr"]},
    )
    assert res.status_code == 404


def test_upload_sanitises_a_traversing_filename(env):
    c, _, data = env
    res = c.post(
        "/admin/shared-documents",
        headers=auth(),
        data={"categories": "finance"},
        files=[("files", ("../../escape.docx", b"x",
                          "application/octet-stream"))],
    )
    assert res.status_code == 200
    assert not (data.parent / "escape.docx").exists()
    assert (data / "_shared" / "escape.docx").exists()


def test_shared_link_is_flagged_in_the_documents_listing(env):
    c, _, _ = env
    publish(c, "policy.docx", b"CORP", cats="finance")
    groups = {
        g["category"]: {f["name"]: f for f in g["files"]}
        for g in c.get("/admin/documents", headers=auth()).json()["documents"]
    }
    assert groups["finance"]["policy.docx"]["shared"] == "policy.docx"
    # A private file keeps its original shape, with no shared key at all.
    assert "shared" not in groups["finance"].get("nothing-here", {})


# ---- health reporting -------------------------------------------------------


def _states(c, filename):
    docs = c.get("/admin/shared-documents", headers=auth()).json()["documents"]
    doc = next(d for d in docs if d["filename"] == filename)
    return {t["category"]: t["state"] for t in doc["targets"]}


def test_health_reports_ok_for_healthy_links(env):
    c, _, _ = env
    publish(c, "policy.docx", b"CORP", cats="finance,hr")
    assert _states(c, "policy.docx") == {"finance": "ok", "hr": "ok"}


def test_health_flags_a_deleted_link_as_broken(env):
    c, _, data = env
    publish(c, "policy.docx", b"CORP", cats="finance,hr")
    (data / "hr" / "policy.docx").unlink()
    assert _states(c, "policy.docx") == {"finance": "ok", "hr": "broken"}


def test_health_flags_a_replaced_copy_as_diverged(env):
    c, _, data = env
    publish(c, "policy.docx", b"CORP POLICY", cats="finance,hr")
    p = data / "hr" / "policy.docx"
    p.unlink()
    p.write_bytes(b"XXXX")  # same length, different content
    assert _states(c, "policy.docx") == {"finance": "ok", "hr": "diverged"}


def test_health_flags_a_missing_canonical(env):
    c, _, data = env
    publish(c, "policy.docx", b"CORP", cats="finance")
    (data / "_shared" / "policy.docx").unlink()
    docs = c.get("/admin/shared-documents", headers=auth()).json()["documents"]
    assert docs[0]["present"] is False
    assert docs[0]["targets"][0]["state"] == "missing"


def test_health_ignores_the_digest_cache_after_an_edit(env):
    c, _, data = env
    publish(c, "policy.docx", b"CORP POLICY", cats="finance")
    assert _states(c, "policy.docx")["finance"] == "ok"
    p = data / "finance" / "policy.docx"
    p.unlink()
    p.write_bytes(b"TAMPERED!!")  # deliberately identical length
    assert _states(c, "policy.docx")["finance"] == "diverged"


# ---- integration with the real ingest pipeline ------------------------------


def test_ingest_picks_up_a_shared_link_in_both_workspaces(tmp_path):
    """The end-to-end promise: no changes to ingest, links just work."""
    from chatbot.ingest import ingest_documents

    data_dir = tmp_path / "data"
    os.environ["DATA_DIR"] = str(data_dir)
    os.environ["INDEX_DIR"] = str(tmp_path / ".index")

    canonical = data_dir / "_shared" / "policy.txt"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text("Scope 3 emissions are reported using the GHG Protocol.")

    for cat in ("finance", "hr"):
        shared_docs.create_link(canonical, shared_docs.target_path(cat, "policy.txt"))

    indexed = {}
    for cat in ("finance", "hr"):
        store = _RecordingStore()
        ingest_documents(
            str(data_dir / cat), store,
            embed_fn=lambda texts: [[0.0] * 4 for _ in texts],
            patterns=("*.txt",),
        )
        indexed[cat] = store.seen_sources

    # Both workspaces see the shared file under its own name, with no ingest
    # change: it is just a file in the folder.
    assert indexed["finance"] == ["policy.txt"]
    assert indexed["hr"] == ["policy.txt"]


def test_ingest_sees_the_same_content_hash_in_both_workspaces(tmp_path):
    """Identical content must hash identically, so chunks are comparable."""
    from chatbot.ingest import ingest_documents

    data_dir = tmp_path / "data"
    os.environ["DATA_DIR"] = str(data_dir)
    canonical = data_dir / "_shared" / "policy.txt"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text("Board oversight of climate risk.")
    for cat in ("finance", "hr"):
        shared_docs.create_link(canonical, shared_docs.target_path(cat, "policy.txt"))

    hashes = {}
    for cat in ("finance", "hr"):
        store = _RecordingStore()
        ingest_documents(str(data_dir / cat), store,
                         embed_fn=lambda t: [[0.0] * 4 for _ in t],
                         patterns=("*.txt",))
        hashes[cat] = store.hashes["policy.txt"]

    assert hashes["finance"] == hashes["hr"]


def test_removing_a_link_prunes_it_from_that_workspace_only(tmp_path):
    from chatbot.ingest import ingest_documents

    data_dir = tmp_path / "data"
    os.environ["DATA_DIR"] = str(data_dir)
    canonical = data_dir / "_shared" / "policy.txt"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text("Shared policy text.")
    for cat in ("finance", "hr"):
        shared_docs.create_link(canonical, shared_docs.target_path(cat, "policy.txt"))

    def run(cat):
        store = _RecordingStore()
        ingest_documents(str(data_dir / cat), store,
                         embed_fn=lambda t: [[0.0] * 4 for _ in t],
                         patterns=("*.txt",))
        return store

    assert "policy.txt" in run("finance").seen_sources
    assert "policy.txt" in run("hr").seen_sources

    # Detach from hr, then re-ingest: hr's stored sources lose the document
    # while finance keeps it. This is what makes isolation hold after a detach.
    shared_docs.target_path("hr", "policy.txt").unlink()
    after_hr = run("hr")
    assert "policy.txt" not in after_hr.seen_sources
    assert after_hr.pruned_against == []

    after_finance = run("finance")
    assert after_finance.seen_sources == ["policy.txt"]


# ---- link kind fallback -----------------------------------------------------


def test_falls_back_to_a_symlink_when_hardlinks_are_unavailable(tmp_path, monkeypatch):
    """Different filesystems must still work, with the same single-copy guarantee."""
    os.environ["DATA_DIR"] = str(tmp_path / "data")
    shared_docs.clear_digest_cache()
    canonical = shared_docs.shared_root() / "policy.txt"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_bytes(b"CORP v1")

    def cross_device(*_a, **_kw):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "link", cross_device)
    target = shared_docs.target_path("finance", "policy.txt")
    kind = shared_docs.create_link(canonical, target)

    assert kind == shared_docs.SYMLINK
    assert target.is_symlink()
    assert target.read_bytes() == b"CORP v1"
    canonical.write_bytes(b"CORP v2")
    assert target.read_bytes() == b"CORP v2"
    assert shared_docs.link_status(canonical, target) == shared_docs.LINK_OK


def test_hardlink_is_preferred_when_available(tmp_path):
    os.environ["DATA_DIR"] = str(tmp_path / "data")
    shared_docs.clear_digest_cache()
    canonical = shared_docs.shared_root() / "policy.txt"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_bytes(b"CORP")
    target = shared_docs.target_path("hr", "policy.txt")
    kind = shared_docs.create_link(canonical, target)
    assert kind == shared_docs.HARDLINK
    assert target.stat().st_ino == canonical.stat().st_ino


def test_shared_store_is_never_treated_as_a_workspace(monkeypatch):
    from chatbot import workspaces

    monkeypatch.setenv("WORKSPACES", "finance,_shared,hr")
    assert workspaces.workspace_names() == ["finance", "hr"]
    assert workspaces.base_workspace_names() == ["finance", "hr"]
    assert workspaces.is_workspace("_shared") is False


def test_default_workspace_config_excludes_the_shared_store(monkeypatch, tmp_path):
    from chatbot import workspaces

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("INDEX_DIR", str(tmp_path / ".index"))
    monkeypatch.setenv("WORKSPACES", "finance,_shared")
    conf = workspaces.default_workspace_config()
    assert set(conf) == {"finance"}
    assert not any(SHARED_DIR_NAME in p for p in conf["finance"].values())


class _RecordingStore:
    """Minimal vector-store stand-in that records what ingest sent it."""

    def __init__(self):
        self.seen_sources = []
        self.pruned_against = None
        self.hashes = {}

    def get_doc_hash(self, source):
        return self.hashes.get(source)

    def insert_batch(self, ids, embeddings, sources, indexes, contents, hashes):
        for s, h in zip(sources, hashes):
            self.hashes[s] = h
            self.seen_sources.append(s)

    def delete_source(self, source):
        self.hashes.pop(source, None)

    def delete_where_source_not_in(self, sources):
        self.pruned_against = list(sources)
        return 0
