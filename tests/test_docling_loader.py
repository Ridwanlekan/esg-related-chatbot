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

    def __init__(self, by_ext, fail_ext=()):
        self.by_ext = by_ext
        self.fail_ext = set(fail_ext)
        self.calls = []

    def supports(self, path):
        return extension_of(path) in self.by_ext

    def convert(self, path):
        self.calls.append(path)
        ext = extension_of(path)
        if ext in self.fail_ext:
            raise ConversionError(f"boom {ext}")
        return self.by_ext[ext]


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