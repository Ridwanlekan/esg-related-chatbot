import numpy as np
import pytest

from chatbot.vector_store import SQLiteVecStore

DIM = 8


@pytest.fixture
def store(tmp_path):
    s = SQLiteVecStore(db_path=str(tmp_path / "vectors.sqlite3"), dim=DIM)
    yield s
    s.close()


def _emb(axis):
    v = np.zeros(DIM, dtype=np.float32)
    v[axis % DIM] = 1.0
    return v


def _sample():
    ids = [f"chunk-{i}" for i in range(5)]
    embeddings = [_emb(i) for i in range(5)]
    sources = ["a.txt", "a.txt", "a.txt", "b.txt", "b.txt"]
    indexes = [0, 1, 2, 0, 1]
    contents = [f"content {i}" for i in range(5)]
    hashes = ["hash-a"] * 3 + ["hash-b"] * 2
    return ids, embeddings, sources, indexes, contents, hashes


def test_insert_and_count(store):
    store.insert_batch(*_sample())
    assert store.count() == 5


def test_reinsert_same_ids_is_idempotent(store):
    store.insert_batch(*_sample())
    store.insert_batch(*_sample())
    assert store.count() == 5


def test_search_ranks_by_similarity(store):
    ids, embeddings, sources, indexes, contents, hashes = _sample()
    store.insert_batch(ids, embeddings, sources, indexes, contents, hashes)
    results = store.search(_emb(3), k=3)
    assert results[0].chunk_id == "chunk-3"
    assert [r.similarity for r in results] == sorted(
        (r.similarity for r in results), reverse=True
    )
    assert len(results) == 3


def test_search_source_filter(store):
    ids, embeddings, sources, indexes, contents, hashes = _sample()
    store.insert_batch(ids, embeddings, sources, indexes, contents, hashes)
    results = store.search(_emb(0), k=5, source="a.txt")
    assert results
    assert all(r.source == "a.txt" for r in results)


def test_doc_hash_roundtrip(store):
    ids, embeddings, sources, indexes, contents, hashes = _sample()
    store.insert_batch(ids, embeddings, sources, indexes, contents, hashes)
    assert store.get_doc_hash("a.txt") == "hash-a"
    assert store.get_doc_hash("missing.txt") is None


def test_delete_source(store):
    store.insert_batch(*_sample())
    store.delete_source("a.txt")
    assert store.count() == 2
    assert store.get_doc_hash("a.txt") is None


def test_delete_where_source_not_in(store):
    store.insert_batch(*_sample())
    pruned = store.delete_where_source_not_in(["a.txt"])
    assert pruned == 2
    assert store.count() == 3
    assert all(r.source == "a.txt" for r in store.search(_emb(0), k=5))


def test_dim_mismatch_rejected(store):
    bad = np.zeros((1, DIM + 4), dtype=np.float32)
    with pytest.raises(ValueError, match="reindex"):
        store.insert_batch(["x"], bad, ["a.txt"], [0], ["c"], ["h"])


def _hybrid_docs(store):
    store.insert_batch(
        ["d1", "d2"],
        [_emb(0), _emb(1)],
        ["crispr.txt", "jupiter.txt"],
        [0, 0],
        [
            "CRISPR gene editing shows enormous promise.",
            "Jupiter has many moons orbiting far away.",
        ],
        ["h-crispr", "h-jupiter"],
    )


def test_hybrid_finds_lexical_match_not_dense(store):
    _hybrid_docs(store)
    query_emb = _emb(1)
    results = store.search_hybrid("CRISPR promise", query_emb, k=2, candidate_k=5)
    assert results[0].chunk_id == "d1"
    assert results[0].score > results[1].score


def test_hybrid_falls_back_to_dense_when_no_lexical_match(store):
    _hybrid_docs(store)
    query_emb = _emb(0)
    results = store.search_hybrid("oxycontin zzz qqq", query_emb, k=2, candidate_k=5)
    assert results[0].chunk_id == "d1"


def test_hybrid_respects_source_filter(store):
    _hybrid_docs(store)
    results = store.search_hybrid("CRISPR", _emb(0), k=5, source="jupiter.txt", candidate_k=5)
    assert all(r.source == "jupiter.txt" for r in results)


def test_lexical_rows_synced_on_delete(store):
    _hybrid_docs(store)
    store.delete_source("crispr.txt")
    results = store.search_hybrid("CRISPR editing", _emb(0), k=5, candidate_k=5)
    assert all(r.source != "crispr.txt" for r in results)


def test_lexical_rows_pruned_by_source(store):
    _hybrid_docs(store)
    store.delete_where_source_not_in(["jupiter.txt"])
    results = store.search_hybrid("CRISPR", _emb(0), k=5, candidate_k=5)
    assert not any(r.source == "crispr.txt" for r in results)


def test_fts_backfilled_for_legacy_database(tmp_path):
    import sqlite3

    db_path = str(tmp_path / "legacy.sqlite3")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE chunks (id TEXT PRIMARY KEY, source TEXT NOT NULL, "
        "chunk_index INTEGER NOT NULL, content TEXT NOT NULL, doc_hash TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO chunks VALUES ('c1', 'a.txt', 0, 'alpha beta', 'h')"
    )
    conn.commit()
    conn.close()
    conn = None

    import sqlite_vec

    compiled = sqlite3.connect(db_path)
    compiled.enable_load_extension(True)
    sqlite_vec.load(compiled)
    compiled.close()

    s = SQLiteVecStore(db_path=db_path, dim=DIM)
    hits = s._search_lexical("alpha", k=5)
    assert hits and hits[0].content == "alpha beta"
    s.close()


def _paged(store):
    store.insert_batch(
        ["p1", "p2", "p3"],
        [_emb(0), _emb(1), _emb(2)],
        ["report.pdf"] * 3,
        [0, 1, 2],
        ["emissions on page one", "governance on page two", "safety on page nine"],
        ["h"] * 3,
        page_starts=[1, 2, 9],
        page_ends=[1, 2, 9],
    )


def test_pages_survive_dense_search(store):
    _paged(store)
    results = store.search(_emb(1), k=1)
    assert (results[0].page_start, results[0].page_end) == (2, 2)
    assert results[0].has_pages


def test_pages_survive_lexical_search(store):
    _paged(store)
    results = store._search_lexical("governance", k=1)
    assert results[0].page_start == 2


def test_pages_survive_hybrid_search(store):
    """Hybrid rebuilds its results, so page metadata is easy to drop there."""
    _paged(store)
    results = store.search_hybrid("governance", _emb(1), k=1)
    assert results[0].page_start == 2
    assert results[0].chunk_id == "p2"


def test_pages_are_absent_for_unpagulated_sources(store):
    store.insert_batch(*_sample())
    assert all(r.page_start is None for r in store.search(_emb(0), k=5))
    assert all(not r.has_pages for r in store.search_hybrid("content", _emb(0), k=5))


def test_legacy_index_gains_page_columns_without_losing_rows(tmp_path):
    """An index built before page tracking opens as-is and reads NULL pages."""
    import sqlite3

    import sqlite_vec

    db_path = str(tmp_path / "legacy.sqlite3")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE chunks (id TEXT PRIMARY KEY, source TEXT NOT NULL, "
        "chunk_index INTEGER NOT NULL, content TEXT NOT NULL, doc_hash TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO chunks VALUES ('c1', 'a.txt', 0, 'alpha beta', 'h')")
    conn.commit()
    conn.close()

    compiled = sqlite3.connect(db_path)
    compiled.enable_load_extension(True)
    sqlite_vec.load(compiled)
    compiled.close()

    s = SQLiteVecStore(db_path=db_path, dim=DIM)
    cols = {r[1] for r in s.conn.execute("PRAGMA table_info(chunks)").fetchall()}
    assert {"page_start", "page_end"} <= cols
    assert s.count() == 1
    hits = s._search_lexical("alpha", k=5)
    assert hits[0].page_start is None
    s.close()