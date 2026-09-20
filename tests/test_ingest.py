import hashlib

import numpy as np

from chatbot.ingest import recursive_split, ingest_documents
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


def _write(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(body)
    return path


def _make_store(tmp_path):
    return SQLiteVecStore(db_path=str(tmp_path / "vectors.sqlite3"), dim=DIM)


def test_recursive_split_respects_size():
    text = "Ada Lovelace wrote the first algorithm. " * 40
    chunks = recursive_split(text, chunk_size=200, overlap=30)
    assert len(chunks) > 1
    assert all(len(c) <= 200 for c in chunks)


def test_recursive_split_keeps_content():
    text = "one\ntwo\nthree"
    chunks = recursive_split(text, chunk_size=100, overlap=10)
    assert all(any(p in c for c in chunks) for p in ("one", "two", "three"))


def test_ingest_and_idempotent_rerun(tmp_path):
    _write(tmp_path, "a.txt", "Final answer dot product alpha theta.\n" * 100)
    _write(tmp_path, "b.txt", "Jupiter has many moons orbiting far away.\n" * 100)

    store = _make_store(tmp_path)
    first = ingest_documents(str(tmp_path), store, _fake_embed)
    assert first.documents_reindexed == 2
    assert first.chunks_upserted == store.count()
    assert first.stale_chunks_removed == 0

    snapshot = store.count()
    second = ingest_documents(str(tmp_path), store, _fake_embed)
    assert second.documents_reindexed == 0
    assert second.chunks_upserted == 0
    assert store.count() == snapshot


def test_edited_file_reindexed(tmp_path):
    doc = _write(tmp_path, "a.txt", "version one content here. " * 50)
    store = _make_store(tmp_path)
    ingest_documents(str(tmp_path), store, _fake_embed)
    first_hash = store.get_doc_hash("a.txt")

    doc.write_text("version two totally different body keeps hash separate. " * 50)
    stats = ingest_documents(str(tmp_path), store, _fake_embed)
    assert stats.documents_reindexed == 1
    assert store.get_doc_hash("a.txt") != first_hash


def test_deleted_file_pruned_from_store(tmp_path):
    _write(tmp_path, "keep.txt", "content that remains relevant alpha. " * 50)
    doc = _write(tmp_path, "remove-me.txt", "content to be removed omega. " * 50)
    store = _make_store(tmp_path)
    ingest_documents(str(tmp_path), store, _fake_embed)
    assert store.get_doc_hash("remove-me.txt") is not None
    before = store.count()

    doc.unlink()
    stats = ingest_documents(str(tmp_path), store, _fake_embed)
    assert stats.stale_chunks_removed > 0
    assert store.get_doc_hash("remove-me.txt") is None
    assert store.count() == before - stats.stale_chunks_removed
    results = store.search(_fake_embed(["omega content"])[0], k=10)
    assert all(r.source != "remove-me.txt" for r in results)