"""Org-scoped /admin/status, /admin/documents, /admin/upload, /admin/delete.

The document and status endpoints were category-global: they described the
pre-organisation tree, which for a multi-tenant deployment is nobody's content.
These tests pin that a scoped request reads and writes only inside one
organisation's served copies, and that unscoped behaviour is untouched so
single-tenant deployments keep working.
"""

import pytest
from fastapi.testclient import TestClient

from chatbot.admin_store import AdminStore
from chatbot.api import create_app
from chatbot.content_library import library_root, list_documents
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


def scoped_upload(c, org_id, category, filename, body=b"%PDF-1.4 scoped"):
    """Upload the way the console does for a customer organisation."""
    return c.post(
        "/admin/upload",
        headers=auth(),
        data={"category": category, "organisation_id": org_id},
        files={"files": (filename, body, "application/pdf")},
    )


def legacy_write(c, category, filename, body=b"%PDF-1.4 global"):
    """Write into the pre-organisation global tree."""
    return c.post(
        "/admin/upload",
        headers=auth(),
        data={"category": category},
        files={"files": (filename, body, "application/pdf")},
    )


class TestScopedUpload:
    def test_scoped_upload_lands_in_that_organisation(self, env):
        c, _ = env
        make_org(c, "acme")
        res = scoped_upload(c, "acme", "finance", "acme.pdf")
        assert res.status_code == 200, res.text
        assert list_documents("acme", "finance") == ["acme.pdf"]

    def test_scoped_upload_records_an_assignment(self, env):
        """A served copy with no assignment behind it is content nobody can revoke."""
        c, _ = env
        make_org(c, "acme")
        scoped_upload(c, "acme", "finance", "acme.pdf")
        rows = c.app.state.admin_store.list_content_assignments("acme")
        assert [r["filename"] for r in rows] == ["acme.pdf"]

    def test_scoped_upload_still_lands_in_the_master_library(self, env):
        """Upload is a convenience for the two-step flow, not a side door
        around it: the master copy is what makes reassignment to another
        customer possible later."""
        c, _ = env
        make_org(c, "acme")
        scoped_upload(c, "acme", "finance", "acme.pdf")
        assert (library_root() / "acme.pdf").is_file()

    def test_two_organisations_can_hold_the_same_filename(self, env):
        c, _ = env
        make_org(c, "acme")
        make_org(c, "globex")
        scoped_upload(c, "acme", "finance", "shared-name.pdf")
        scoped_upload(c, "globex", "finance", "shared-name.pdf")
        assert list_documents("acme", "finance") == ["shared-name.pdf"]
        assert list_documents("globex", "finance") == ["shared-name.pdf"]

    def test_scoped_upload_never_touches_the_global_tree(self, env):
        c, tmp_path = env
        make_org(c, "acme")
        scoped_upload(c, "acme", "finance", "acme.pdf")
        assert not (tmp_path / "data" / "finance" / "acme.pdf").exists()

    def test_unknown_organisation_is_rejected(self, env):
        c, _ = env
        res = scoped_upload(c, "nope", "finance", "x.pdf")
        assert res.status_code == 404

    def test_sample_organisation_refuses_paid_content(self, env):
        c, _ = env
        res = scoped_upload(c, SAMPLE_ORGANISATION_ID, "finance", "x.pdf")
        assert res.status_code == 409
        assert list_documents(SAMPLE_ORGANISATION_ID, "finance") == []

    def test_traversal_in_the_organisation_is_rejected(self, env):
        c, _ = env
        make_org(c, "acme")
        res = scoped_upload(c, "../escape", "finance", "x.pdf")
        assert res.status_code in (404, 422)


class TestScopedDocumentsListing:
    def test_listing_returns_only_that_organisations_documents(self, env):
        c, _ = env
        make_org(c, "acme")
        make_org(c, "globex")
        scoped_upload(c, "acme", "finance", "acme-only.pdf")
        scoped_upload(c, "globex", "finance", "globex-only.pdf")
        res = c.get("/admin/documents", headers=auth(),
                    params={"organisation_id": "acme"})
        assert res.status_code == 200, res.text
        names = [f["name"] for g in res.json()["documents"] for f in g["files"]]
        assert "acme-only.pdf" in names
        assert "globex-only.pdf" not in names

    def test_listing_hides_the_global_tree(self, env):
        c, _ = env
        make_org(c, "acme")
        legacy_write(c, "finance", "global.pdf")
        res = c.get("/admin/documents", headers=auth(),
                    params={"organisation_id": "acme"})
        names = [f["name"] for g in res.json()["documents"] for f in g["files"]]
        assert names == []

    def test_unscoped_listing_still_shows_the_global_tree(self, env):
        """Single-tenant deployments have no organisations; nothing changes."""
        c, _ = env
        legacy_write(c, "finance", "global.pdf")
        res = c.get("/admin/documents", headers=auth())
        names = [f["name"] for g in res.json()["documents"] for f in g["files"]]
        assert "global.pdf" in names

    def test_listing_echoes_the_scope_it_used(self, env):
        c, _ = env
        make_org(c, "acme")
        res = c.get("/admin/documents", headers=auth(),
                    params={"organisation_id": "acme"})
        assert res.json()["organisation_id"] == "acme"

    def test_unknown_organisation_is_not_a_silent_empty_list(self, env):
        c, _ = env
        res = c.get("/admin/documents", headers=auth(),
                    params={"organisation_id": "nope"})
        assert res.status_code == 404


class TestScopedDelete:
    def test_delete_removes_the_served_copy_and_the_assignment(self, env):
        """Unlinking the file alone would leave the assignment behind, so the
        console would keep offering to grant a document that is no longer there."""
        c, _ = env
        make_org(c, "acme")
        scoped_upload(c, "acme", "finance", "acme.pdf")
        res = c.post("/admin/documents/delete", headers=auth(),
                     json={"organisation_id": "acme", "category": "finance",
                           "filename": "acme.pdf"})
        assert res.status_code == 200, res.text
        assert list_documents("acme", "finance") == []
        assert c.app.state.admin_store.list_content_assignments("acme") == []

    def test_delete_leaves_the_master_library_intact(self, env):
        c, _ = env
        make_org(c, "acme")
        scoped_upload(c, "acme", "finance", "acme.pdf")
        c.post("/admin/documents/delete", headers=auth(),
               json={"organisation_id": "acme", "category": "finance",
                     "filename": "acme.pdf"})
        assert (library_root() / "acme.pdf").is_file()

    def test_scoped_delete_cannot_remove_another_organisations_copy(self, env):
        c, _ = env
        make_org(c, "acme")
        make_org(c, "globex")
        scoped_upload(c, "globex", "finance", "globex-only.pdf")
        res = c.post("/admin/documents/delete", headers=auth(),
                     json={"organisation_id": "acme", "category": "finance",
                           "filename": "globex-only.pdf"})
        assert res.status_code == 404
        assert list_documents("globex", "finance") == ["globex-only.pdf"]

    def test_scoped_delete_is_audited(self, env):
        c, _ = env
        make_org(c, "acme")
        scoped_upload(c, "acme", "finance", "acme.pdf")
        c.post("/admin/documents/delete", headers=auth(),
               json={"organisation_id": "acme", "category": "finance",
                     "filename": "acme.pdf"})
        kinds = [e["event"] for e in c.app.state.admin_store.list_events()["events"]]
        assert "content.unassign" in kinds

    def test_traversal_in_the_filename_cannot_escape(self, env):
        c, tmp_path = env
        make_org(c, "acme")
        keep = tmp_path / "data" / "keepme.pdf"
        keep.parent.mkdir(parents=True, exist_ok=True)
        keep.write_bytes(b"keep")
        res = c.post("/admin/documents/delete", headers=auth(),
                     json={"organisation_id": "acme", "category": "finance",
                           "filename": "../keepme.pdf"})
        assert res.status_code == 404
        assert keep.is_file()

    def test_unscoped_delete_still_works_on_the_global_tree(self, env):
        c, _ = env
        legacy_write(c, "finance", "global.pdf")
        res = c.post("/admin/documents/delete", headers=auth(),
                     json={"category": "finance", "filename": "global.pdf"})
        assert res.status_code == 200, res.text


class TestScopedStatus:
    def test_status_reports_only_that_organisations_files(self, env):
        c, _ = env
        make_org(c, "acme")
        make_org(c, "globex")
        scoped_upload(c, "acme", "finance", "acme-only.pdf")
        scoped_upload(c, "globex", "finance", "globex-only.pdf")
        res = c.get("/admin/status", headers=auth(),
                    params={"organisation_id": "acme"})
        assert res.status_code == 200, res.text
        finance = next(w for w in res.json()["workspaces"] if w["category"] == "finance")
        assert finance["data_files"] == ["acme-only.pdf"]

    def test_status_counts_only_that_organisations_users(self, env):
        c, _ = env
        make_org(c, "acme")
        c.post("/auth/signup", json={"email": "free@visitor.com",
                                     "password": "password123", "name": "V",
                                     "category": "finance"})
        c.post("/admin/users", headers=auth(),
               json={"email": "buyer@acme.com", "password": "password123",
                     "name": "Buyer", "category": "finance",
                     "organisation_id": "acme"})
        res = c.get("/admin/status", headers=auth(),
                    params={"organisation_id": "acme"})
        assert res.json()["totals"]["users"] == 1

    def test_status_echoes_the_scope(self, env):
        c, _ = env
        make_org(c, "acme")
        res = c.get("/admin/status", headers=auth(),
                    params={"organisation_id": "acme"})
        assert res.json()["organisation_id"] == "acme"

    def test_unknown_organisation_is_rejected(self, env):
        c, _ = env
        res = c.get("/admin/status", headers=auth(),
                    params={"organisation_id": "nope"})
        assert res.status_code == 404

    def test_unscoped_status_still_reports_the_global_tree(self, env):
        c, _ = env
        legacy_write(c, "finance", "global.pdf")
        res = c.get("/admin/status", headers=auth())
        finance = next(w for w in res.json()["workspaces"] if w["category"] == "finance")
        assert finance["data_files"] == ["global.pdf"]

    def test_an_organisation_with_nothing_assigned_reports_nothing(self, env):
        """The failure this fixes: reporting the global tree as if it were the
        customer's, so an empty organisation looked fully indexed."""
        c, _ = env
        make_org(c, "acme")
        legacy_write(c, "finance", "global.pdf")
        res = c.get("/admin/status", headers=auth(),
                    params={"organisation_id": "acme"})
        finance = next(w for w in res.json()["workspaces"] if w["category"] == "finance")
        assert finance["data_files"] == []
        assert finance["indexed"] is False