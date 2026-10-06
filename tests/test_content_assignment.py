"""Phase 2: master library, per-organisation assignment, organisation CRUD.

Section 3.4: assignment is the only access control, so the property under test
throughout is that one organisation's served content is never visible to
another, and that the sample organisation is the single deliberate exception.
"""

import pytest
from fastapi.testclient import TestClient

from chatbot.admin_store import AdminStore
from chatbot.api import create_app
from chatbot.content_library import (
    library_root,
    list_documents,
    seed_sample_pack,
    workspace_dir,
)
from chatbot.session_store import SessionStore
from chatbot.users import UserStore

KEY = "secret-admin-key"


def auth(key=KEY):
    return {"authorization": f"Bearer {key}"}


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


def upload_library(c, filename, subject="esg-reporting"):
    res = c.post(
        "/admin/library/upload",
        headers=auth(),
        data={"subject": subject, "jurisdiction": "HK", "effective_date": "2026-01-01",
              "version": "v1"},
        files={"files": (filename, b"%PDF-1.4 content", "application/pdf")},
    )
    assert res.status_code == 200, res.text
    return res.json()["saved_files"]


def make_org(c, org_id="acme", name="Acme Ltd"):
    res = c.post("/admin/organisations", headers=auth(), json={"id": org_id, "name": name})
    assert res.status_code == 200, res.text
    return res.json()


def seed_sample(c, filename="overview_of_esg.pdf", categories=("finance",)):
    """Give the free tier its launch pack the way startup does.

    The generic assignment endpoint now refuses `_sample` outright (it is the
    shared free tier, and paid content must not reach every free user), so the
    seeding path is the only supported one and these tests use it.
    """
    upload_library(c, filename)
    placed = seed_sample_pack(categories=list(categories))
    assert placed, "sample pack was not seeded"


def assign(c, org_id, filenames, categories):
    return c.post(
        "/admin/library/assign",
        headers=auth(),
        json={"organisation_id": org_id, "categories": categories, "filenames": filenames},
    )


class TestOrganisationCrud:
    def test_create_organisation(self, env):
        c, _ = env
        org = make_org(c)
        assert org["id"] == "acme"
        assert org["system_owned"] is False

    def test_reserved_ids_rejected(self, env):
        c, _ = env
        for reserved in ("_sample", "_default"):
            res = c.post("/admin/organisations", headers=auth(),
                         json={"id": reserved, "name": "Nope"})
            assert res.status_code == 422

    def test_duplicate_organisation_rejected(self, env):
        c, _ = env
        make_org(c)
        res = c.post("/admin/organisations", headers=auth(), json={"id": "acme", "name": "X"})
        assert res.status_code == 422

    def test_list_includes_system_orgs(self, env):
        c, _ = env
        make_org(c)
        orgs = {o["id"] for o in c.get("/admin/organisations", headers=auth()).json()["organisations"]}
        assert {"acme", "_sample", "_default"} <= orgs

    def test_list_reports_user_counts(self, env):
        c, _ = env
        make_org(c)
        users = c.app.state.user_store
        u = users.create_user("a@b.com", "password123", "A", "finance")
        users.set_user_organisation(u["id"], "acme")
        entry = next(
            o for o in c.get("/admin/organisations", headers=auth()).json()["organisations"]
            if o["id"] == "acme"
        )
        assert entry["user_count"] == 1

    def test_endpoints_require_admin_key(self, env):
        c, _ = env
        assert c.get("/admin/organisations").status_code == 401
        assert c.post("/admin/organisations", json={"id": "x"}).status_code == 401


class TestDeleteConfirmation:
    def test_delete_requires_matching_confirmation(self, env):
        c, _ = env
        make_org(c)
        upload_library(c, "a.pdf")
        assign(c, "acme", ["a.pdf"], ["finance"])
        res = c.post("/admin/organisations/delete", headers=auth(),
                     json={"organisation_id": "acme", "confirm": "wrong"})
        assert res.status_code == 400
        assert list_documents("acme", "finance") == ["a.pdf"]

    def test_missing_confirmation_field_rejected(self, env):
        c, _ = env
        make_org(c)
        res = c.post("/admin/organisations/delete", headers=auth(),
                     json={"organisation_id": "acme"})
        assert res.status_code == 422

    def test_delete_with_confirmation_removes_content_and_members(self, env):
        c, _ = env
        make_org(c)
        upload_library(c, "a.pdf")
        assign(c, "acme", ["a.pdf"], ["finance"])
        users = c.app.state.user_store
        u = users.create_user("a@b.com", "password123", "A", "finance")
        users.set_user_organisation(u["id"], "acme")

        res = c.post("/admin/organisations/delete", headers=auth(),
                     json={"organisation_id": "acme", "confirm": "acme"})
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["members_deleted"] == 1
        assert list_documents("acme", "finance") == []
        assert users.get_organisation("acme") is None
        assert users.get(u["id"]) is None

    def test_delete_leaves_master_library_intact(self, env):
        c, _ = env
        make_org(c)
        upload_library(c, "a.pdf")
        assign(c, "acme", ["a.pdf"], ["finance"])
        c.post("/admin/organisations/delete", headers=auth(),
               json={"organisation_id": "acme", "confirm": "acme"})
        assert (library_root() / "a.pdf").is_file()

    def test_delete_unknown_organisation(self, env):
        c, _ = env
        res = c.post("/admin/organisations/delete", headers=auth(),
                     json={"organisation_id": "nope", "confirm": "nope"})
        assert res.status_code == 404

    def test_cannot_delete_sample_organisation(self, env):
        c, _ = env
        res = c.post("/admin/organisations/delete", headers=auth(),
                     json={"organisation_id": "_sample", "confirm": "_sample"})
        assert res.status_code == 403
        assert c.app.state.user_store.get_organisation("_sample") is not None

    def test_sample_pack_survives_a_customer_deletion(self, env):
        c, _ = env
        seed_sample(c)
        make_org(c)
        c.post("/admin/organisations/delete", headers=auth(),
               json={"organisation_id": "acme", "confirm": "acme"})
        assert list_documents("_sample", "finance") == ["overview_of_esg.pdf"]


class TestLibraryUpload:
    def test_upload_registers_with_metadata(self, env):
        c, _ = env
        upload_library(c, "overview_of_esg.pdf")
        doc = c.get("/admin/library", headers=auth()).json()["documents"][0]
        assert doc["filename"] == "overview_of_esg.pdf"
        assert doc["subject"] == "esg-reporting"
        assert doc["jurisdiction"] == "HK"
        assert doc["version"] == "v1"
        assert doc["present"] is True

    def test_upload_writes_into_library_root(self, env):
        c, _ = env
        upload_library(c, "doc.pdf")
        assert (library_root() / "doc.pdf").read_bytes() == b"%PDF-1.4 content"

    def test_upload_alone_serves_nobody(self, env):
        c, _ = env
        upload_library(c, "doc.pdf")
        for org in ("_sample", "_default", "acme"):
            assert list_documents(org, "finance") == []

    def test_reupload_updates_metadata(self, env):
        c, _ = env
        upload_library(c, "doc.pdf", subject="labour")
        c.post("/admin/library/upload", headers=auth(), data={"subject": "climate"},
               files={"files": ("doc.pdf", b"%PDF-1.4 v2", "application/pdf")})
        doc = c.get("/admin/library", headers=auth()).json()["documents"][0]
        assert doc["subject"] == "climate"
        assert (library_root() / "doc.pdf").read_bytes() == b"%PDF-1.4 v2"

    def test_list_filters_by_subject(self, env):
        c, _ = env
        upload_library(c, "a.pdf", subject="labour")
        upload_library(c, "b.pdf", subject="climate")
        doc = c.get("/admin/library?subject=labour", headers=auth()).json()
        assert [d["filename"] for d in doc["documents"]] == ["a.pdf"]

    def test_upload_requires_admin_key(self, env):
        c, _ = env
        res = c.post("/admin/library/upload",
                     files={"files": ("a.pdf", b"x", "application/pdf")})
        assert res.status_code == 401


class TestAssignment:
    def test_assignment_materialises_and_records(self, env):
        c, _ = env
        make_org(c)
        upload_library(c, "overview_of_esg.pdf")
        res = assign(c, "acme", ["overview_of_esg.pdf"], ["finance"])
        assert res.status_code == 200, res.text
        assert list_documents("acme", "finance") == ["overview_of_esg.pdf"]
        assert res.json()["assigned"][0]["category"] == "finance"

    def test_assignment_across_several_workspaces(self, env):
        c, _ = env
        make_org(c)
        upload_library(c, "a.pdf")
        assign(c, "acme", ["a.pdf"], ["finance", "hr"])
        assert list_documents("acme", "finance") == ["a.pdf"]
        assert list_documents("acme", "hr") == ["a.pdf"]

    def test_paid_assignment_to_the_sample_organisation_is_refused(self, env):
        """`_sample` is the shared free tier.

        Assigning to it would serve the document to every free account in
        deployment, which is the paid/paying boundary and not something a
        platform admin should be able to do by accident.
        """
        c, _ = env
        upload_library(c, "acme-contract.pdf")
        res = assign(c, "_sample", ["acme-contract.pdf"], ["finance"])
        assert res.status_code == 409
        assert list_documents("_sample", "finance") == []

    def test_sample_organisation_still_serves_the_launch_pack(self, env):
        c, _ = env
        seed_sample(c)
        assert list_documents("_sample", "finance") == ["overview_of_esg.pdf"]

    def test_one_organisation_never_sees_another(self, env):
        c, _ = env
        make_org(c, "acme")
        make_org(c, "globex")
        upload_library(c, "acme-only.pdf")
        assign(c, "acme", ["acme-only.pdf"], ["finance"])
        assert list_documents("acme", "finance") == ["acme-only.pdf"]
        assert list_documents("globex", "finance") == []

    def test_assignment_is_idempotent(self, env):
        c, _ = env
        make_org(c)
        upload_library(c, "a.pdf")
        assign(c, "acme", ["a.pdf"], ["finance"])
        assign(c, "acme", ["a.pdf"], ["finance"])
        assert list_documents("acme", "finance") == ["a.pdf"]
        rows = c.app.state.admin_store.list_content_assignments("acme")
        assert len(rows) == 1

    def test_unknown_library_document_rejected(self, env):
        c, _ = env
        make_org(c)
        res = assign(c, "acme", ["never-uploaded.pdf"], ["finance"])
        assert res.status_code == 422
        assert "Not in the library" in res.json()["detail"]

    def test_unknown_organisation_rejected(self, env):
        c, _ = env
        upload_library(c, "a.pdf")
        res = assign(c, "nope", ["a.pdf"], ["finance"])
        assert res.status_code == 404

    def test_unknown_workspace_rejected(self, env):
        c, _ = env
        make_org(c)
        upload_library(c, "a.pdf")
        res = assign(c, "acme", ["a.pdf"], ["not-a-workspace"])
        assert res.status_code == 422

    def test_partial_assignment_reports_skipped(self, env):
        c, _ = env
        make_org(c)
        upload_library(c, "real.pdf")
        res = assign(c, "acme", ["real.pdf", "missing.pdf"], ["finance"])
        assert res.status_code == 200
        body = res.json()
        assert [a["filename"] for a in body["assigned"]] == ["real.pdf"]
        assert [s["filename"] for s in body["skipped"]] == ["missing.pdf"]

    def test_traversal_filename_cannot_escape(self, env):
        c, _ = env
        make_org(c)
        res = assign(c, "acme", ["../../escape.pdf"], ["finance"])
        assert res.status_code == 422
        assert list_documents("acme", "finance") == []

    def test_assignment_requires_admin_key(self, env):
        c, _ = env
        upload_library(c, "a.pdf")
        res = c.post("/admin/library/assign",
                     json={"organisation_id": "acme", "categories": ["finance"],
                           "filenames": ["a.pdf"]})
        assert res.status_code == 401


class TestUnassign:
    def test_unassign_removes_file_and_record(self, env):
        c, _ = env
        make_org(c)
        upload_library(c, "a.pdf")
        assign(c, "acme", ["a.pdf"], ["finance"])
        res = c.post("/admin/library/unassign", headers=auth(),
                     json={"organisation_id": "acme", "categories": ["finance"],
                           "filenames": ["a.pdf"]})
        assert res.status_code == 200, res.text
        assert res.json()["removed"] == [{"category": "finance", "filename": "a.pdf"}]
        assert list_documents("acme", "finance") == []
        assert c.app.state.admin_store.list_content_assignments("acme") == []

    def test_unassign_one_workspace_leaves_the_other(self, env):
        c, _ = env
        make_org(c)
        upload_library(c, "a.pdf")
        assign(c, "acme", ["a.pdf"], ["finance", "hr"])
        c.post("/admin/library/unassign", headers=auth(),
               json={"organisation_id": "acme", "categories": ["finance"],
                     "filenames": ["a.pdf"]})
        assert list_documents("acme", "finance") == []
        assert list_documents("acme", "hr") == ["a.pdf"]

    def test_unassign_unknown_is_not_an_error(self, env):
        c, _ = env
        make_org(c)
        res = c.post("/admin/library/unassign", headers=auth(),
                     json={"organisation_id": "acme", "categories": ["finance"],
                           "filenames": ["nope.pdf"]})
        assert res.status_code == 200
        assert res.json()["removed"] == []

    def test_unassign_does_not_touch_master(self, env):
        c, _ = env
        make_org(c)
        upload_library(c, "a.pdf")
        assign(c, "acme", ["a.pdf"], ["finance"])
        c.post("/admin/library/unassign", headers=auth(),
               json={"organisation_id": "acme", "categories": ["finance"],
                     "filenames": ["a.pdf"]})
        assert (library_root() / "a.pdf").is_file()


class TestOrganisationContentView:
    def test_reports_assignments_and_library(self, env):
        c, _ = env
        make_org(c)
        upload_library(c, "assigned.pdf", subject="labour")
        upload_library(c, "unassigned.pdf", subject="climate")
        assign(c, "acme", ["assigned.pdf"], ["finance"])

        body = c.get("/admin/organisations/acme/content", headers=auth()).json()
        assert [a["filename"] for a in body["assignments"]["finance"]] == ["assigned.pdf"]
        assert body["assignments"]["finance"][0]["present"] is True
        assert {d["filename"] for d in body["library"]} == {"assigned.pdf", "unassigned.pdf"}
        assert set(body["workspaces"]) >= {"finance", "hr"}

    def test_detects_deleted_served_copy(self, env):
        """A served copy removed out of band must not look intact."""
        c, _ = env
        make_org(c)
        upload_library(c, "a.pdf")
        assign(c, "acme", ["a.pdf"], ["finance"])
        workspace_dir("acme", "finance").joinpath("a.pdf").unlink()

        body = c.get("/admin/organisations/acme/content", headers=auth()).json()
        assert body["assignments"]["finance"][0]["present"] is False

    def test_library_view_lists_assigned_organisations(self, env):
        c, _ = env
        make_org(c)
        upload_library(c, "a.pdf")
        assign(c, "acme", ["a.pdf"], ["finance"])
        doc = c.get("/admin/library", headers=auth()).json()["documents"][0]
        assert doc["assigned_to"] == ["acme"]

    def test_unknown_organisation_content_is_404(self, env):
        c, _ = env
        assert c.get("/admin/organisations/nope/content", headers=auth()).status_code == 404

    def test_requires_admin_key(self, env):
        c, _ = env
        make_org(c)
        assert c.get("/admin/organisations/acme/content").status_code == 401


class TestAuditTrail:
    def test_assignment_is_audited(self, env):
        """D12: the audit log must show which content an organisation was given."""
        c, _ = env
        make_org(c)
        upload_library(c, "a.pdf")
        assign(c, "acme", ["a.pdf"], ["finance"])
        events = c.app.state.admin_store.list_events()
        kinds = [e["event"] for e in events["events"]]
        assert "content.assign" in kinds

    def test_unassignment_is_audited(self, env):
        c, _ = env
        make_org(c)
        upload_library(c, "a.pdf")
        assign(c, "acme", ["a.pdf"], ["finance"])
        c.post("/admin/library/unassign", headers=auth(),
               json={"organisation_id": "acme", "categories": ["finance"],
                     "filenames": ["a.pdf"]})
        kinds = [e["event"] for e in c.app.state.admin_store.list_events()["events"]]
        assert "content.unassign" in kinds

    def test_organisation_lifecycle_is_audited(self, env):
        c, _ = env
        make_org(c)
        c.post("/admin/organisations/delete", headers=auth(),
               json={"organisation_id": "acme", "confirm": "acme"})
        kinds = [e["event"] for e in c.app.state.admin_store.list_events()["events"]]
        assert "org.create" in kinds
        assert "org.delete" in kinds

    def test_library_upload_is_audited(self, env):
        c, _ = env
        upload_library(c, "a.pdf")
        kinds = [e["event"] for e in c.app.state.admin_store.list_events()["events"]]
        assert "library.upload" in kinds