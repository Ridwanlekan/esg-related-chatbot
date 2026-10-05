import os

import pytest

from chatbot.content_library import (
    SAMPLE_PACK,
    index_dir_for,
    is_sample,
    library_root,
    list_documents,
    materialise,
    organisation_dir,
    organisations_root,
    purge_organisation,
    remove,
    seed_sample_pack,
    workspace_dir,
)


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("INDEX_DIR", str(tmp_path / "index"))
    return tmp_path


def write_pdf(path, name="overview_of_esg.pdf", body=b"%PDF-1.4 fake"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


class TestPaths:
    def test_organisation_dir_under_organisations_root(self, lib):
        assert organisation_dir("acme").parent == organisations_root()

    def test_sample_org_directory_name(self, lib):
        assert organisation_dir("_sample").name == "_sample"
        assert is_sample("_sample") is True
        assert is_sample("acme") is False

    def test_workspace_dir_is_organisation_scoped(self, lib):
        assert workspace_dir("acme", "finance") == organisation_dir("acme") / "finance"

    def test_two_orgs_cannot_collide_on_same_workspace(self, lib):
        assert workspace_dir("acme", "finance") != workspace_dir("globex", "finance")

    def test_index_dir_is_per_organisation(self, lib):
        assert index_dir_for("acme", "finance") != index_dir_for("globex", "finance")

    def test_index_path_includes_workspace(self, lib):
        assert index_dir_for("acme", "finance").name == "vectors_finance.sqlite3"

    def test_library_root_is_not_under_organisations(self, lib):
        assert organisations_root() not in library_root().parents
        assert library_root() not in organisations_root().parents

    @pytest.mark.parametrize(
        "bad", ["..", ".", "", "  ", "../etc", "a/b", "a\\b", ".hidden", "/abs"]
    )
    def test_traversal_rejected_for_organisation(self, lib, bad):
        with pytest.raises(ValueError, match="Invalid organisation"):
            organisation_dir(bad)

    @pytest.mark.parametrize("bad", ["..", "_shared", "", "a/b"])
    def test_traversal_rejected_for_workspace(self, lib, bad):
        with pytest.raises(ValueError, match="Invalid workspace"):
            workspace_dir("acme", bad)

    def test_traversal_rejected_for_index(self, lib):
        with pytest.raises(ValueError):
            index_dir_for("acme", "../escape")


class TestMaterialise:
    def test_copies_document_into_workspace(self, lib):
        src = write_pdf(lib / "src" / "doc.pdf")
        dest = materialise("acme", "finance", src)
        assert dest.is_file()
        assert dest.parent == workspace_dir("acme", "finance")
        assert dest.read_bytes() == src.read_bytes()

    def test_uses_library_root_by_default(self, lib):
        src = write_pdf(library_root() / "doc.pdf")
        assert materialise("acme", "hr", src).name == "doc.pdf"

    def test_rename_on_materialise(self, lib):
        src = write_pdf(lib / "src" / "long-original-name.pdf")
        dest = materialise("acme", "finance", src, filename="short.pdf")
        assert dest.name == "short.pdf"

    def test_served_copy_is_independent_of_master(self, lib):
        """A real copy, so the master cannot be written through."""
        src = write_pdf(library_root() / "doc.pdf")
        dest = materialise("acme", "finance", src)
        src.write_bytes(b"replaced master")
        assert dest.read_bytes() == b"%PDF-1.4 fake"

    def test_repeat_materialise_overwrites_cleanly(self, lib):
        src = write_pdf(library_root() / "doc.pdf")
        first = materialise("acme", "finance", src)
        src.write_bytes(b"second version")
        materialise("acme", "finance", src)
        assert first.read_bytes() == b"second version"
        assert not list(first.parent.glob("*.partial"))

    def test_missing_source_rejected(self, lib):
        with pytest.raises(ValueError, match="not found"):
            materialise("acme", "finance", lib / "nope.pdf")

    @pytest.mark.parametrize("bad", ["../escape.pdf", "a/b.pdf", ".hidden.pdf"])
    def test_unsafe_filename_rejected(self, lib, bad):
        src = write_pdf(lib / "src" / "doc.pdf")
        with pytest.raises(ValueError, match="Invalid document name"):
            materialise("acme", "finance", src, filename=bad)

    def test_cannot_materialise_onto_itself(self, lib):
        src = write_pdf(library_root() / "doc.pdf")
        dest = materialise("acme", "finance", src)
        # Idempotent re-run, including the source already being the destination.
        assert materialise("acme", "finance", dest) == dest


class TestListingAndRemoval:
    def test_lists_materialised_documents(self, lib):
        src = write_pdf(library_root() / "doc.pdf")
        materialise("acme", "finance", src)
        assert list_documents("acme", "finance") == ["doc.pdf"]

    def test_lists_separate_workspaces_separately(self, lib):
        src = write_pdf(library_root() / "doc.pdf")
        materialise("acme", "finance", src)
        materialise("acme", "hr", src)
        assert list_documents("acme", "finance") == ["doc.pdf"]
        assert list_documents("acme", "hr") == ["doc.pdf"]

    def test_orgs_do_not_see_each_others_documents(self, lib):
        src = write_pdf(library_root() / "doc.pdf")
        materialise("acme", "finance", src)
        assert list_documents("globex", "finance") == []

    def test_listing_missing_workspace_is_empty(self, lib):
        assert list_documents("acme", "finance") == []

    def test_remove_document(self, lib):
        src = write_pdf(library_root() / "doc.pdf")
        materialise("acme", "finance", src)
        assert remove("acme", "finance", "doc.pdf") is True
        assert list_documents("acme", "finance") == []

    def test_remove_missing_document_is_false(self, lib):
        assert remove("acme", "finance", "nope.pdf") is False

    def test_remove_rejects_traversal(self, lib):
        with pytest.raises(ValueError, match="Invalid document name"):
            remove("acme", "finance", "../../etc/passwd")


class TestSamplePack:
    def test_seeds_launch_pack(self, lib):
        write_pdf(library_root() / "overview_of_esg.pdf")
        placed = seed_sample_pack(categories=("finance",))
        assert placed == [("finance", "overview_of_esg.pdf")]
        assert list_documents("_sample", "finance") == ["overview_of_esg.pdf"]

    def test_seeds_into_multiple_workspaces(self, lib):
        write_pdf(library_root() / "overview_of_esg.pdf")
        seed_sample_pack(categories=("finance", "hr"))
        assert list_documents("_sample", "finance") == ["overview_of_esg.pdf"]
        assert list_documents("_sample", "hr") == ["overview_of_esg.pdf"]

    def test_seeding_is_idempotent(self, lib):
        write_pdf(library_root() / "overview_of_esg.pdf")
        seed_sample_pack(categories=("finance",))
        seed_sample_pack(categories=("finance",))
        assert list_documents("_sample", "finance") == ["overview_of_esg.pdf"]

    def test_falls_back_to_data_root_document(self, lib):
        """The pre-organisation build kept the loose pack at the data root."""
        write_pdf(lib / "data" / "overview_of_esg.pdf")
        seed_sample_pack(categories=("finance",))
        assert list_documents("_sample", "finance") == ["overview_of_esg.pdf"]

    def test_customer_org_cannot_be_seeded(self, lib):
        write_pdf(library_root() / "overview_of_esg.pdf")
        with pytest.raises(ValueError, match="Only the system sample"):
            seed_sample_pack("acme")

    def test_missing_pack_document_is_skipped_not_fatal(self, lib):
        write_pdf(library_root() / "other.pdf")
        assert seed_sample_pack(categories=("finance",)) == []
        assert list_documents("_sample", "finance") == []

    def test_pack_contains_only_the_named_document(self, lib):
        assert SAMPLE_PACK == ("overview_of_esg.pdf",)


class TestPurge:
    def test_purge_removes_documents_and_index(self, lib):
        src = write_pdf(library_root() / "doc.pdf")
        materialise("acme", "finance", src)
        index_dir_for("acme", "finance").parent.mkdir(parents=True, exist_ok=True)
        index_dir_for("acme", "finance").write_text("index")

        assert purge_organisation("acme") is True
        assert not workspace_dir("acme", "finance").exists()
        assert not index_dir_for("acme", "finance").exists()

    def test_purge_leaves_other_orgs_untouched(self, lib):
        src = write_pdf(library_root() / "doc.pdf")
        materialise("acme", "finance", src)
        materialise("globex", "finance", src)
        purge_organisation("acme")
        assert list_documents("acme", "finance") == []
        assert list_documents("globex", "finance") == ["doc.pdf"]

    def test_purge_leaves_master_library_intact(self, lib):
        src = write_pdf(library_root() / "doc.pdf")
        materialise("acme", "finance", src)
        purge_organisation("acme")
        assert src.is_file()

    def test_purge_refuses_sample_org(self, lib):
        with pytest.raises(ValueError, match="cannot be purged"):
            purge_organisation("_sample")

    def test_purge_unknown_org_is_false(self, lib):
        assert purge_organisation("nope") is False

    def test_purge_rejects_traversal(self, lib):
        outside = lib / "outside"
        outside.mkdir()
        (outside / "keep.pdf").write_bytes(b"keep me")
        with pytest.raises(ValueError, match="Invalid organisation"):
            purge_organisation("../outside")
        assert (outside / "keep.pdf").is_file()