import hashlib

import numpy as np
import pytest

from chatbot import docling_loader
from chatbot.docling_loader import ConversionError, DoclingLoader, extension_of
from chatbot.ingest import INGEST_PROGRESS, ingest_documents
from chatbot.vector_store import SQLiteVecStore

DIM = 8


def _fake_embed(texts):
    return np.stack(
        [
            np.frombuffer(hashlib.sha256(t.encode()).digest()[:DIM], dtype=np.uint8).astype(
                np.float32
            )
            for t in texts
        ]
    )


def _make_store(tmp_path):
    return SQLiteVecStore(db_path=str(tmp_path / "vectors.sqlite3"), dim=DIM)


def _contents(store):
    return {
        row["source"]: row["content"]
        for row in store.conn.execute("SELECT source, content FROM chunks").fetchall()
    }


@pytest.fixture(autouse=True)
def _no_vet_writes_by_default(monkeypatch):
    # Keep generated .docling_vet/ out of the repo during tests; the vetting
    # tests below opt back in by setting DOCLING_VET_DIR themselves.
    monkeypatch.setenv("DOCLING_VET_DIR", "0")


class FakeLoader:
    """Stub DoclingLoader keyed by extension."""

    def __init__(self, by_ext, fail_ext=(), pages=None):
        self.by_ext = by_ext
        self.fail_ext = set(fail_ext)
        self.pages = pages or {}
        self.calls = []

    def supports(self, path):
        return extension_of(path) in self.by_ext

    def convert(self, path):
        self.calls.append(path)
        ext = extension_of(path)
        if ext in self.fail_ext:
            raise ConversionError(f"boom {ext}")
        return self.by_ext[ext]

    def convert_pages(self, path):
        if extension_of(path) in self.fail_ext:
            raise ConversionError(f"boom {extension_of(path)}")
        return self.pages.get(extension_of(path))


class PagedLoader(FakeLoader):
    """A loader that paginates, for sources that really have pages."""

    def __init__(self, by_ext, pages):
        super().__init__(by_ext, pages=pages)


class SinglePassLoader(FakeLoader):
    """A loader exposing the combined API ingest prefers, counting conversions."""

    def __init__(self, by_ext, pages):
        super().__init__(by_ext, pages=pages)
        self.conversions = 0

    def convert_both(self, path):
        self.conversions += 1
        return self.convert(path), self.pages.get(extension_of(path))


def test_extension_classification():
    assert extension_of("a/b/Report.PDF") == "pdf"
    assert extension_of("noext") == ""
    assert docling_loader.is_audio("talk.mp3") and not docling_loader.is_video("talk.mp3")
    assert docling_loader.is_video("clip.MP4") and docling_loader.is_media("clip.mp4")
    for ext in ("pdf", "docx", "adoc", "mp3", "mp4", "png", "csv", "xlsx", "eml", "vtt"):
        assert ext in docling_loader.DOCLING_EXTENSIONS
    assert {"md", "adoc", "csv"}.issubset(docling_loader.TEXT_FALLBACK_EXTENSIONS)


def test_loader_supports_expected_formats():
    loader = DoclingLoader()
    assert loader.supports("x.pdf") and loader.supports("a.adoc") and loader.supports("v.wav")
    assert not loader.supports("x.txt") and not loader.supports("x.unknown")


def test_ingest_routes_documents_through_docling(tmp_path):
    (tmp_path / "report.pdf").write_bytes(b"%PDF-1.4 not a real pdf")
    (tmp_path / "notes.txt").write_text("plain notes about carbon emissions. " * 40)

    loader = FakeLoader({"pdf": "CONVERTED PDF CONTENT about emissions\n\nsecond part"})
    store = _make_store(tmp_path)
    stats = ingest_documents(str(tmp_path), store, _fake_embed, loader=loader)

    assert stats.documents_reindexed == 2
    assert stats.documents_failed == 0
    assert len(loader.calls) == 1
    bodies = _contents(store)
    assert "CONVERTED PDF CONTENT" in bodies["report.pdf"]
    assert "plain notes" in bodies["notes.txt"]


def test_ingest_converts_a_paginated_document_only_once(tmp_path):
    """Both the text and the page split come from one conversion.

    Asking the converter separately for each half parses and OCRs the document
    twice, which is the single most expensive thing ingestion does.
    """
    (tmp_path / "report.pdf").write_bytes(b"%PDF-1.4 not a real pdf")
    body = "emissions reporting clause for page one. " * 20

    loader = SinglePassLoader({"pdf": body}, {"pdf": [(1, body), (2, "water risk on page two. " * 20)]})
    store = _make_store(tmp_path)
    stats = ingest_documents(str(tmp_path), store, _fake_embed, loader=loader)

    assert stats.documents_reindexed == 1
    assert loader.conversions == 1
    assert len(loader.calls) == 1
    assert {page for _, start, end in _pages(store, "report.pdf") for page in (start, end)} == {1, 2}


def test_ingest_keeps_a_document_indexed_when_only_the_page_split_fails(tmp_path):
    """Page numbers are an enrichment, never a reason to lose the document."""
    (tmp_path / "report.pdf").write_bytes(b"%PDF-1.4 not a real pdf")
    body = "emissions reporting clause for page one. " * 20

    class BrokenSplit(FakeLoader):
        """No combined API, and page splitting blows up after a good convert."""

        def convert_pages(self, path):
            raise RuntimeError("page split exploded")

    store = _make_store(tmp_path)
    stats = ingest_documents(str(tmp_path), store, _fake_embed, loader=BrokenSplit({"pdf": body}))

    assert stats.documents_reindexed == 1
    assert stats.documents_failed == 0
    assert "emissions reporting clause" in _contents(store)["report.pdf"]
    assert {page for _, start, end in _pages(store, "report.pdf") for page in (start, end)} == {None}


def test_ingest_skips_failed_conversion(tmp_path):
    (tmp_path / "bad.pdf").write_bytes(b"%PDF-1.4 broken")
    (tmp_path / "good.txt").write_text("good content about water. " * 40)

    loader = FakeLoader({"pdf": "unused"}, fail_ext={"pdf"})
    store = _make_store(tmp_path)
    stats = ingest_documents(str(tmp_path), store, _fake_embed, loader=loader)

    assert stats.documents_failed == 1
    assert stats.documents_reindexed == 1
    assert store.get_doc_hash("bad.pdf") is None
    assert "good content" in _contents(store)["good.txt"]


def test_markdown_falls_back_to_plain_text_when_docling_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(docling_loader, "is_available", lambda: False)
    (tmp_path / "notes.md").write_text("plain markdown about forests. " * 40)

    store = _make_store(tmp_path)
    stats = ingest_documents(str(tmp_path), store, _fake_embed)
    assert stats.documents_reindexed == 1
    assert "plain markdown about forests" in _contents(store)["notes.md"]


def test_docling_disabled_uses_plain_text(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCLING_ENABLED", "0")
    (tmp_path / "notes.adoc").write_text("asciidoc body about wind turbines. " * 40)

    store = _make_store(tmp_path)
    stats = ingest_documents(str(tmp_path), store, _fake_embed)
    assert stats.documents_reindexed == 1
    assert "asciidoc body about wind turbines" in _contents(store)["notes.adoc"]


def test_binary_without_docling_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(docling_loader, "is_available", lambda: False)
    (tmp_path / "scan.pdf").write_bytes(b"%PDF-1.4 binary")

    store = _make_store(tmp_path)
    stats = ingest_documents(str(tmp_path), store, _fake_embed)
    assert stats.documents_failed == 1
    assert stats.documents_reindexed == 0
    assert store.count() == 0


def test_clean_docling_text_normalizes_whitespace():
    raw = "# Title   \n\n\n\nBody line   \nspaced   \n\n\n   \n\nTail\n\n\n"
    cleaned = docling_loader.clean_docling_text(raw)
    assert cleaned == "# Title\n\nBody line\nspaced\n\nTail\n"
    assert cleaned.endswith("\n") and not cleaned.startswith("\n")
    assert docling_loader.clean_docling_text("") == ""


def test_vet_outputs_saved_and_excluded(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCLING_VET_DIR", str(tmp_path / "vet"))

    (tmp_path / "report.pdf").write_bytes(b"%PDF-1.4 fake")
    (tmp_path / "notes.txt").write_text("plain notes about carbon. " * 40)

    raw_body = "Messy   # Title   \n\n\nBody line   \n\nmore\n"
    loader = FakeLoader({"pdf": raw_body})
    store = _make_store(tmp_path)
    stats = ingest_documents(str(tmp_path), store, _fake_embed, loader=loader)

    # Only the cleaned text is persisted (exactly what gets chunked).
    vet_file = tmp_path / "vet" / "report.md"
    assert vet_file.read_text() == "Messy   # Title\n\nBody line\n\nmore\n"

    assert stats.documents_reindexed == 2
    assert stats.documents_failed == 0
    assert "plain notes" in _contents(store)["notes.txt"]
    assert all(
        ".docling_vet" not in row["source"]
        for row in store.conn.execute("SELECT source AS source FROM chunks").fetchall()
    )

    # Second run: vet .md files must not be picked up as ingestible documents.
    stats2 = ingest_documents(str(tmp_path), store, _fake_embed, loader=loader)
    assert stats2.documents_reindexed == 0


def test_vet_disabled_does_not_write(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCLING_VET_DIR", "0")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "report.pdf").write_bytes(b"%PDF-1.4 fake")
    store = _make_store(tmp_path)
    ingest_documents(str(tmp_path), store, _fake_embed, loader=FakeLoader({"pdf": "text\n"}))
    assert not (tmp_path / ".docling_vet").exists()
    # "0" must disable vetting entirely, not be treated as a path.
    assert not (tmp_path / "0").exists()


def test_ingest_tracks_progress_in_snapshot(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"%PDF-1.4 fake")
    (tmp_path / "b.txt").write_text("plain carbon notes. " * 40)
    store = _make_store(tmp_path)
    stats = ingest_documents(
        str(tmp_path), store, _fake_embed, loader=FakeLoader({"pdf": "x\n"})
    )
    snap = INGEST_PROGRESS.snapshot()
    assert snap["running"] is False
    assert snap["total_files"] == 2
    assert snap["processed_files"] == 2
    assert snap["documents_reindexed"] == stats.documents_reindexed
    assert snap["documents_failed"] == stats.documents_failed
    assert snap["chunks_upserted"] == stats.chunks_upserted
    assert snap["elapsed_seconds"] is not None


def test_real_docling_converts_asciidoc(tmp_path):
    pytest.importorskip("docling")
    path = tmp_path / "guide.adoc"
    path.write_text("= ESG Guide\n\nCarbon *matters* a lot.\n\n== Scope\n\nEmissions data.\n")

    text = DoclingLoader().convert(str(path))
    assert "ESG Guide" in text and "Carbon" in text and "Emissions data" in text


def _pages(store, source):
    return [
        (row["chunk_index"], row["page_start"], row["page_end"])
        for row in store.conn.execute(
            "SELECT chunk_index, page_start, page_end FROM chunks "
            "WHERE source = ? ORDER BY chunk_index",
            (source,),
        )
    ]


def test_ingest_records_the_page_of_every_chunk(tmp_path):
    (tmp_path / "report.pdf").write_bytes(b"%PDF-1.4 fake")
    loader = PagedLoader(
        {"pdf": "full body"},
        pages={"pdf": [(1, "page one emissions"), (2, "page two governance")]},
    )
    store = _make_store(tmp_path)
    ingest_documents(str(tmp_path), store, _fake_embed, loader=loader)

    pages = _pages(store, "report.pdf")
    assert pages, "expected chunks"
    assert all(start == end for _, start, end in pages)
    assert sorted({start for _, start, end in pages}) == [1, 2]
    assert pages == sorted(pages), "chunk_index must stay in page order"


def _all_bodies(store, source):
    return "\n".join(
        row["content"]
        for row in store.conn.execute(
            "SELECT content FROM chunks WHERE source = ? ORDER BY chunk_index", (source,)
        )
    )


def test_a_paginated_document_loses_no_text(tmp_path):
    (tmp_path / "report.pdf").write_bytes(b"%PDF-1.4 fake")
    loader = PagedLoader(
        {"pdf": "unused"},
        pages={"pdf": [(1, "alpha one"), (2, "beta two"), (7, "gamma seven")]},
    )
    store = _make_store(tmp_path)
    ingest_documents(str(tmp_path), store, _fake_embed, loader=loader)

    bodies = _all_bodies(store, "report.pdf")
    assert "alpha one" in bodies and "beta two" in bodies and "gamma seven" in bodies


def test_unpaginated_sources_have_no_page_numbers(tmp_path):
    """Plain text has no pages, and inventing one would be a false citation."""
    (tmp_path / "notes.txt").write_text("carbon notes. " * 40)
    loader = FakeLoader({})
    store = _make_store(tmp_path)
    ingest_documents(str(tmp_path), store, _fake_embed, loader=loader)

    assert _pages(store, "notes.txt")
    assert all(start is None for _, start, _ in _pages(store, "notes.txt"))


def test_pages_are_optional_for_a_source_that_cannot_be_paginated(tmp_path):
    """A converter without per-page support still indexes the document."""
    (tmp_path / "report.pdf").write_bytes(b"%PDF-1.4 fake")
    (tmp_path / "notes.txt").write_text("plain body text. " * 40)
    loader = FakeLoader({"pdf": "CONVERTED BODY"})
    store = _make_store(tmp_path)
    stats = ingest_documents(str(tmp_path), store, _fake_embed, loader=loader)

    assert stats.documents_reindexed == 2
    assert all(start is None for _, start, _ in _pages(store, "report.pdf"))


def test_page_extraction_failure_does_not_stop_ingestion(tmp_path):
    (tmp_path / "report.pdf").write_bytes(b"%PDF-1.4 fake")
    loader = FakeLoader({"pdf": "BODY"}, fail_ext={"pdf"})
    store = _make_store(tmp_path)
    # convert() raises, so the document falls back to plain text and is skipped;
    # what matters is that the failure is contained rather than raised.
    stats = ingest_documents(str(tmp_path), store, _fake_embed, loader=loader)
    assert stats.documents_failed == 1


def test_bumping_the_ingest_version_forces_one_reindex(tmp_path, monkeypatch):
    """Documents indexed before page tracking must be revisited exactly once."""
    from chatbot import ingest as ingest_module

    (tmp_path / "report.pdf").write_bytes(b"%PDF-1.4 fake")
    loader = PagedLoader({"pdf": "full body"}, pages={"pdf": [(1, "page one")]})
    store = _make_store(tmp_path)

    ingest_documents(str(tmp_path), store, _fake_embed, loader=loader)
    assert _pages(store, "report.pdf")
    assert ingest_documents(str(tmp_path), store, _fake_embed, loader=loader).documents_reindexed == 0

    monkeypatch.setattr(ingest_module, "INGEST_SCHEMA_VERSION", 99)
    again = ingest_documents(str(tmp_path), store, _fake_embed, loader=loader)
    assert again.documents_reindexed == 1
    assert ingest_documents(str(tmp_path), store, _fake_embed, loader=loader).documents_reindexed == 0


class TestConvertPages:
    """Page attribution from Docling's provenance, without losing text."""

    class _Item:
        def __init__(self, text=None, prov=(), markdown=None):
            self.text = text
            self.prov = list(prov)
            self._markdown = markdown

        def export_to_markdown(self):
            return self._markdown or ""

    class _Prov:
        def __init__(self, page_no, charspan=None):
            self.page_no = page_no
            self.charspan = charspan

    class _Document:
        def __init__(self, items, pages):
            self._items = items
            self.pages = pages

        def iterate_items(self):
            return [(item, 0) for item in self._items]

        def export_to_markdown(self):
            return "\n\n".join(i.text or i.export_to_markdown() for i in self._items)

    def _loader_for(self, document):
        loader = DoclingLoader.__new__(DoclingLoader)
        loader._convert_document = lambda path: document
        return loader

    def test_an_item_spanning_two_pages_is_split_between_them(self):
        """The case Docling's own page filter loses, and the reason we slice.

        One text block printed across a page break carries two provenance
        entries with charspans. Filtering by page drops the block instead of
        slicing it, which silently deletes half the document.
        """
        item = self._Item(
            text="HEADER ON ONE TAIL ON TWO",
            prov=[self._Prov(1, (0, 14)), self._Prov(2, (14, 25))],
        )
        pages = self._loader_for(self._Document([item], {1: None, 2: None})).convert_pages("x.pdf")
        assert pages == [(1, "HEADER ON ONE\n"), (2, "TAIL ON TWO\n")]

    def test_items_are_ordered_by_page(self):
        items = [
            self._Item(text="second page text", prov=[self._Prov(3, None)]),
            self._Item(text="first page text", prov=[self._Prov(1, None)]),
        ]
        pages = self._loader_for(self._Document(items, {1: None, 3: None})).convert_pages("x.pdf")
        assert [no for no, _ in pages] == [1, 3]

    def test_a_table_is_attributed_to_the_page_it_starts_on(self):
        table = self._Item(prov=[self._Prov(2, None), self._Prov(3, None)], markdown="| a | b |")
        pages = self._loader_for(self._Document([table], {2: None, 3: None})).convert_pages("x.pdf")
        assert pages == [(2, "| a | b |\n")]

    def test_empty_pages_are_dropped_rather_than_cited(self):
        items = [
            self._Item(text="real text", prov=[self._Prov(1, None)]),
            self._Item(text="   ", prov=[self._Prov(2, None)]),
        ]
        pages = self._loader_for(self._Document(items, {1: None, 2: None})).convert_pages("x.pdf")
        assert [no for no, _ in pages] == [1]

    def test_a_document_with_no_pages_reports_none(self):
        loader = self._loader_for(self._Document([self._Item(text="x", prov=[])], {}))
        assert loader.convert_pages("x.html") is None

    def test_convert_both_returns_text_and_pages_from_one_conversion(self):
        """Ingest needs both halves; parsing and OCRing twice is pure waste."""
        document = self._Document(
            [self._Item(text="page one", prov=[self._Prov(1, None)])],
            {1: None, 2: None},
        )
        loader = self._loader_for(document)
        calls = []
        loader._convert_document = lambda path: (calls.append(path), document)[1]

        markdown, pages = loader.convert_both("x.pdf")

        assert calls == ["x.pdf"]
        assert "page one" in markdown
        assert pages == [(1, "page one\n")]

    def test_convert_both_keeps_the_text_when_the_page_split_fails(self):
        loader = self._loader_for(
            self._Document([self._Item(text="x", prov=[self._Prov(1, None)])], {1: None})
        )

        def boom(document):
            raise ValueError("split failed")

        loader._pages_from = staticmethod(boom)
        markdown, pages = loader.convert_both("x.pdf")

        assert "x" in markdown
        assert pages is None
