"""Legacy content migration and the citation fallback it replaces (D19).

A deployment that predates organisations has documents in the global tree. The
citation fallback keeps them reachable; these tests pin both the migration that
replaces the fallback and the fact that turning the fallback off with unmigrated
content present strands those documents rather than quietly serving something
else.
"""

import os

import pytest
from fastapi.testclient import TestClient

from chatbot.admin_store import AdminStore
from chatbot.api import create_app
from chatbot.content_library import (
    library_root,
    list_documents,
    scan_legacy_tree,
)
from chatbot.documents import DocumentNotFound, resolve_document
from chatbot.session_store import SessionStore
from chatbot.users import SAMPLE_ORGANISATION_ID, UserStore

KEY = "secret-admin-key"


def auth():
    return {"authorization": f"Bearer {KEY}"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("INDEX_DIR", str(tmp_path / "index"))
    app = create_app(
        session_store=SessionStore(db_path=str(tmp_path / "sessions.sqlite3")),
        user_store=UserStore(db_path=str(tmp_path / "users.sqlite3"), secret="test-secret"),
        admin_store=AdminStore(str(tmp_path / "admin.sqlite3")),
        api_key=KEY,
        rate_limit=0,
    )
    return TestClient(app), tmp_path


def make_org(c, org_id="acme", name="Acme Ltd"):
    res = c.post("/admin/organisations", headers=auth(),
                 json={"id": org_id, "name": name})
    assert res.status_code == 200, res.text
    return res.json()


def legacy_file(tmp_path, category, filename, body=b"%PDF-1.4 legacy"):
    folder = tmp_path / "data" / category
    folder.mkdir(parents=True, exist_ok=True)
    (folder / filename).write_bytes(body)
    return folder / filename


class TestScan:
    def test_scan_finds_documents_in_each_workspace(self, env):
        _, tmp_path = env
        legacy_file(tmp_path, "finance", "ifrs.pdf")
        legacy_file(tmp_path, "hr", "policy.pdf")
        found = {d["filename"] for d in scan_legacy_tree()}
        assert {"ifrs.pdf", "policy.pdf"} <= found

    def test_scan_reports_the_workspace_each_document_came_from(self, env):
        _, tmp_path = env
        legacy_file(tmp_path, "hr", "policy.pdf")
        doc = next(d for d in scan_legacy_tree() if d["filename"] == "policy.pdf")
        assert doc["category"] == "hr"

    def test_a_shared_document_is_reported_once(self, env):
        """Walking the workspace folders as well would report the same bytes once
        per hardlink, making the counts meaningless."""
        _, tmp_path = env
        shared = legacy_file(tmp_path, "_shared", "handbook.pdf")
        legacy_file(tmp_path, "finance", "handbook.pdf")
        legacy_file(tmp_path, "hr", "handbook.pdf")
        hits = [d for d in scan_legacy_tree() if d["filename"] == "handbook.pdf"]
        assert len(hits) == 1
        assert hits[0]["shared"] is True

    def test_scan_ignores_dotfiles(self, env):
        _, tmp_path = env
        legacy_file(tmp_path, "finance", ".DS_Store")
        assert scan_legacy_tree() == []

    def test_an_empty_deployment_scans_empty(self, env):
        _, _ = env
        assert scan_legacy_tree() == []

    def test_the_scan_endpoint_reports_it(self, env):
        c, tmp_path = env
        legacy_file(tmp_path, "finance", "ifrs.pdf")
        res = c.get("/admin/migrations/legacy-content", headers=auth())
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["count"] == 1
        assert body["by_workspace"]["finance"] == 1


class TestMigration:
    def test_migration_imports_into_the_library(self, env):
        c, tmp_path = env
        make_org(c, "acme")
        legacy_file(tmp_path, "finance", "ifrs.pdf")
        res = c.post("/admin/migrations/legacy-content", headers=auth(),
                     json={"organisation_id": "acme"})
        assert res.status_code == 200, res.text
        assert (library_root() / "ifrs.pdf").is_file()

    def test_migration_assigns_to_the_named_organisation(self, env):
        c, tmp_path = env
        make_org(c, "acme")
        legacy_file(tmp_path, "finance", "ifrs.pdf")
        c.post("/admin/migrations/legacy-content", headers=auth(),
               json={"organisation_id": "acme", "categories": ["finance"]})
        assert list_documents("acme", "finance") == ["ifrs.pdf"]

    def test_the_named_organisation_is_required(self, env):
        """The legacy tree belonged to the deployment, not to a customer, so
        there is no owner to infer."""
        c, tmp_path = env
        legacy_file(tmp_path, "finance", "ifrs.pdf")
        res = c.post("/admin/migrations/legacy-content", headers=auth(), json={})
        assert res.status_code == 422
        assert not (library_root() / "ifrs.pdf").exists()

    def test_the_free_tier_cannot_inherit_legacy_content(self, env):
        c, tmp_path = env
        legacy_file(tmp_path, "finance", "ifrs.pdf")
        res = c.post("/admin/migrations/legacy-content", headers=auth(),
                     json={"organisation_id": SAMPLE_ORGANISATION_ID})
        assert res.status_code == 409
        assert not (library_root() / "ifrs.pdf").exists()

    def test_unknown_organisation_is_refused(self, env):
        c, tmp_path = env
        legacy_file(tmp_path, "finance", "ifrs.pdf")
        res = c.post("/admin/migrations/legacy-content", headers=auth(),
                     json={"organisation_id": "nope"})
        assert res.status_code == 404

    def test_workspaces_can_be_narrowed(self, env):
        c, tmp_path = env
        make_org(c, "acme")
        legacy_file(tmp_path, "finance", "ifrs.pdf")
        legacy_file(tmp_path, "hr", "policy.pdf")
        c.post("/admin/migrations/legacy-content", headers=auth(),
               json={"organisation_id": "acme", "categories": ["finance"]})
        assert list_documents("acme", "finance") == ["ifrs.pdf"]
        assert list_documents("acme", "hr") == []

    def test_the_originals_are_copied_not_moved(self, env):
        """A wrong call here must be recoverable from the original tree."""
        c, tmp_path = env
        make_org(c, "acme")
        original = legacy_file(tmp_path, "finance", "ifrs.pdf")
        c.post("/admin/migrations/legacy-content", headers=auth(),
               json={"organisation_id": "acme"})
        assert original.is_file()

    def test_rerunning_does_not_duplicate_or_overwrite(self, env):
        c, tmp_path = env
        make_org(c, "acme")
        legacy_file(tmp_path, "finance", "ifrs.pdf")
        c.post("/admin/migrations/legacy-content", headers=auth(),
               json={"organisation_id": "acme"})
        (library_root() / "ifrs.pdf").write_bytes(b"%PDF curated edit")
        c.post("/admin/migrations/legacy-content", headers=auth(),
               json={"organisation_id": "acme"})
        assert (library_root() / "ifrs.pdf").read_bytes() == b"%PDF curated edit"

    def test_migration_is_audited(self, env):
        c, tmp_path = env
        make_org(c, "acme")
        legacy_file(tmp_path, "finance", "ifrs.pdf")
        c.post("/admin/migrations/legacy-content", headers=auth(),
               json={"organisation_id": "acme"})
        kinds = [e["event"] for e in c.app.state.admin_store.list_events()["events"]]
        assert "migration.legacy_content" in kinds

    def test_migrated_content_is_reachable_by_citation(self, env):
        """The point of the migration: what the fallback used to serve is now a
        served copy."""
        c, tmp_path = env
        make_org(c, "acme")
        legacy_file(tmp_path, "finance", "ifrs.pdf")
        c.post("/admin/migrations/legacy-content", headers=auth(),
               json={"organisation_id": "acme", "categories": ["finance"]})
        assert resolve_document("finance", "ifrs.pdf", "acme").read_bytes() \
            == b"%PDF-1.4 legacy"

    def test_another_organisation_gets_nothing(self, env):
        c, tmp_path = env
        make_org(c, "acme")
        make_org(c, "globex")
        legacy_file(tmp_path, "finance", "ifrs.pdf")
        c.post("/admin/migrations/legacy-content", headers=auth(),
               json={"organisation_id": "acme", "categories": ["finance"]})
        assert list_documents("globex", "finance") == []


class TestDestinations:
    def test_a_document_keeps_its_own_workspace(self, env):
        """Giving every legacy document to every workspace would hand a customer
        an HR policy because they once had an HR workspace."""
        c, tmp_path = env
        make_org(c, "acme")
        legacy_file(tmp_path, "hr", "policy.pdf")
        legacy_file(tmp_path, "finance", "ifrs.pdf")
        c.post("/admin/migrations/legacy-content", headers=auth(),
               json={"organisation_id": "acme"})
        assert list_documents("acme", "hr") == ["policy.pdf"]
        assert list_documents("acme", "finance") == ["ifrs.pdf"]

    def test_selecting_all_workspaces_does_not_cross_assign(self, env):
        c, tmp_path = env
        make_org(c, "acme")
        legacy_file(tmp_path, "hr", "policy.pdf")
        c.post("/admin/migrations/legacy-content", headers=auth(),
               json={"organisation_id": "acme",
                     "categories": ["finance", "hr", "governance"]})
        assert list_documents("acme", "finance") == []
        assert list_documents("acme", "hr") == ["policy.pdf"]

    def test_a_shared_document_goes_to_the_workspaces_it_was_linked_into(self, env):
        c, tmp_path = env
        make_org(c, "acme")
        legacy_file(tmp_path, "_shared", "handbook.pdf", b"%PDF shared")
        for cat in ("finance", "hr"):
            res = c.post("/admin/shared-documents/link", headers=auth(),
                         json={"filename": "handbook.pdf", "categories": [cat]})
            assert res.status_code == 200, res.text
        c.post("/admin/migrations/legacy-content", headers=auth(),
               json={"organisation_id": "acme"})
        assert list_documents("acme", "finance") == ["handbook.pdf"]
        assert list_documents("acme", "hr") == ["handbook.pdf"]
        assert list_documents("acme", "governance") == []

    def test_the_link_map_is_built_by_the_real_endpoint(self, env):
        """The migration reads the link map from the admin store, so the test
        must create it the way an operator would."""
        c, tmp_path = env
        legacy_file(tmp_path, "_shared", "handbook.pdf", b"%PDF shared")
        res = c.post("/admin/shared-documents/link", headers=auth(),
                     json={"filename": "handbook.pdf", "categories": ["finance"]})
        assert res.status_code == 200, res.text
        assert c.app.state.admin_store.shared_target_categories(
            "handbook.pdf"
        ) == ["finance"]

    def test_a_shared_document_with_no_links_is_skipped_with_a_reason(self, env):
        """_shared is not a workspace, so there is nowhere to put it without a
        link map saying which workspaces want it."""
        c, tmp_path = env
        make_org(c, "acme")
        legacy_file(tmp_path, "_shared", "handbook.pdf")
        res = c.post("/admin/migrations/legacy-content", headers=auth(),
                     json={"organisation_id": "acme"})
        assert res.status_code == 200, res.text
        assert res.json()["assigned"] == []
        assert any("handbook.pdf" == s["filename"] for s in res.json()["skipped"])


class TestOrphanedSharedDocuments:
    def test_a_shared_document_linked_outside_a_workspace_is_named(self, env):
        """Otherwise it stays unmigrated forever and the fallback can never be
        switched off, with nothing pointing at the file responsible."""
        c, tmp_path = env
        make_org(c, "acme")
        canonical = legacy_file(tmp_path, "_shared", "handbook.pdf", b"%PDF shared")
        strays = tmp_path / "data" / "ridwan_profile"
        strays.mkdir(parents=True)
        # A hardlink, which is how the shared store links into a workspace.
        os.link(canonical, strays / "handbook.pdf")
        res = c.get("/admin/migrations/legacy-content", headers=auth()).json()
        assert [o["filename"] for o in res["orphaned_shared"]] == ["handbook.pdf"]
        assert res["orphaned_shared"][0]["linked_in"] == ["ridwan_profile"]
        assert res["safe_to_disable_fallback"] is False

    def test_the_reason_names_what_to_do(self, env):
        c, tmp_path = env
        canonical = legacy_file(tmp_path, "_shared", "handbook.pdf", b"%PDF shared")
        strays = tmp_path / "data" / "ridwan_profile"
        strays.mkdir(parents=True)
        os.link(canonical, strays / "handbook.pdf")
        body = c.get("/admin/migrations/legacy-content", headers=auth()).json()
        assert "ridwan_profile" in body["orphaned_shared"][0]["reason"]

    def test_a_link_inside_a_real_workspace_is_not_orphaned(self, env):
        c, tmp_path = env
        canonical = legacy_file(tmp_path, "_shared", "handbook.pdf", b"%PDF shared")
        legacy_file(tmp_path, "finance", "placeholder.pdf")
        os.link(canonical, tmp_path / "data" / "finance" / "handbook.pdf")
        body = c.get("/admin/migrations/legacy-content", headers=auth()).json()
        assert body["orphaned_shared"] == []

    def test_a_plain_copy_elsewhere_is_not_orphaned(self, env):
        """Orphan detection is by inode: a separate file that happens to share a
        name is a different document, not a missing link."""
        c, tmp_path = env
        legacy_file(tmp_path, "_shared", "handbook.pdf", b"%PDF shared")
        legacy_file(tmp_path, "ridwan_profile", "handbook.pdf", b"%PDF different")
        body = c.get("/admin/migrations/legacy-content", headers=auth()).json()
        assert body["orphaned_shared"] == []

    def test_migrating_a_real_workspace_link_clears_the_orphan(self, env):
        c, tmp_path = env
        make_org(c, "acme")
        legacy_file(tmp_path, "_shared", "handbook.pdf", b"%PDF shared")
        res = c.post("/admin/shared-documents/link", headers=auth(),
                     json={"filename": "handbook.pdf", "categories": ["finance"]})
        assert res.status_code == 200, res.text
        c.post("/admin/migrations/legacy-content", headers=auth(),
               json={"organisation_id": "acme"})
        body = c.get("/admin/migrations/legacy-content", headers=auth()).json()
        assert body["safe_to_disable_fallback"] is True


class TestFallbackSwitch:
    def test_the_fallback_resolves_legacy_content_by_default(self, env):
        """Content uploaded before organisations existed must not be stranded."""
        _, tmp_path = env
        legacy_file(tmp_path, "finance", "legacy.pdf")
        assert resolve_document("finance", "legacy.pdf", "acme").read_bytes() \
            == b"%PDF-1.4 legacy"

    def test_disabling_it_keeps_the_served_copy_working(self, env, monkeypatch):
        monkeypatch.setenv("ALLOW_LEGACY_CONTENT", "0")
        c, tmp_path = env
        make_org(c, "acme")
        c.post("/admin/library/upload", headers=auth(),
               files={"files": ("acme.pdf", b"%PDF served", "application/pdf")})
        c.post("/admin/library/assign", headers=auth(),
               json={"organisation_id": "acme", "categories": ["finance"],
                     "filenames": ["acme.pdf"]})
        assert resolve_document("finance", "acme.pdf", "acme").read_bytes() \
            == b"%PDF served"

    def test_disabling_it_stops_reaching_the_legacy_tree(self, env, monkeypatch):
        monkeypatch.setenv("ALLOW_LEGACY_CONTENT", "0")
        _, tmp_path = env
        legacy_file(tmp_path, "finance", "legacy.pdf")
        with pytest.raises(DocumentNotFound):
            resolve_document("finance", "legacy.pdf", "acme")

    def test_the_scan_says_when_it_is_unsafe_to_disable(self, env):
        c, tmp_path = env
        legacy_file(tmp_path, "finance", "ifrs.pdf")
        body = c.get("/admin/migrations/legacy-content", headers=auth()).json()
        assert body["fallback_enabled"] is True
        assert body["safe_to_disable_fallback"] is False

    def test_the_scan_reports_an_empty_tree_as_safe(self, env):
        c, _ = env
        body = c.get("/admin/migrations/legacy-content", headers=auth()).json()
        assert body["safe_to_disable_fallback"] is True

    def test_migrating_everything_makes_it_safe(self, env):
        """Safety is about assignment, not emptiness: the migration copies rather
        than moves, so the originals stay on disk by design."""
        c, tmp_path = env
        make_org(c, "acme")
        legacy_file(tmp_path, "finance", "ifrs.pdf")
        c.post("/admin/migrations/legacy-content", headers=auth(),
               json={"organisation_id": "acme", "categories": ["finance"]})
        body = c.get("/admin/migrations/legacy-content", headers=auth()).json()
        assert body["safe_to_disable_fallback"] is True
        assert body["unassigned"] == []

    def test_a_partly_migrated_tree_is_not_safe(self, env):
        c, tmp_path = env
        make_org(c, "acme")
        legacy_file(tmp_path, "finance", "ifrs.pdf")
        legacy_file(tmp_path, "hr", "policy.pdf")
        c.post("/admin/migrations/legacy-content", headers=auth(),
               json={"organisation_id": "acme", "categories": ["finance"]})
        body = c.get("/admin/migrations/legacy-content", headers=auth()).json()
        assert body["safe_to_disable_fallback"] is False
        assert [d["filename"] for d in body["unassigned"]] == ["policy.pdf"]

    def test_unscoped_citation_is_unaffected_by_the_switch(self, env, monkeypatch):
        """Single-tenant deployments have no organisations, so their global tree
        is their content, not legacy content."""
        monkeypatch.setenv("ALLOW_LEGACY_CONTENT", "0")
        _, tmp_path = env
        legacy_file(tmp_path, "finance", "global.pdf")
        assert resolve_document("finance", "global.pdf").read_bytes() \
            == b"%PDF-1.4 legacy"