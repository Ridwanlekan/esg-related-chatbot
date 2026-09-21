import numpy as np
import pytest

from chatbot.reranker import CrossEncoderReranker, mmr_select
from chatbot.vector_store import SQLiteVecStore, SearchResult


def _result(chunk_id):
    return SearchResult(
        chunk_id=chunk_id,
        source="doc.txt",
        chunk_index=0,
        content=chunk_id,
        distance=0.0,
        score=0.0,
    )


A = _result("A")
B = _result("B")
C = _result("C")


def test_mmr_lambda_one_is_relevance_only():
    embs = {"A": np.array([1.0, 0, 0]), "B": np.array([0, 1.0, 0]), "C": np.array([0.9, 0.43, 0])}
    out = mmr_select([A, B, C], np.array([1.0, 0, 0]), embs, 2, lambda_=1.0)
    assert [r.chunk_id for r in out] == ["A", "B"]


def test_mmr_picks_diverse_second_result():
    embs = {"A": np.array([1.0, 0, 0]), "B": np.array([0, 1.0, 0]), "C": np.array([0.9, 0.43, 0])}
    out = mmr_select([A, B, C], np.array([1.0, 0, 0]), embs, 2, lambda_=0.5)
    assert [r.chunk_id for r in out] == ["A", "B"]


def test_mmr_returns_all_when_k_exceeds():
    embs = {"A": np.array([1.0, 0, 0]), "B": np.array([0, 1.0, 0]), "C": np.array([0.9, 0.43, 0])}
    out = mmr_select([A, B, C], np.array([1.0, 0, 0]), embs, 5, lambda_=0.5)
    assert sorted(r.chunk_id for r in out) == ["A", "B", "C"]
    assert out[0].chunk_id == "A"


def test_mmr_falls_back_when_embedding_missing():
    embs = {"A": np.array([1.0, 0, 0]), "B": np.array([0, 1.0, 0])}
    out = mmr_select([A, B, C], np.array([1.0, 0, 0]), embs, 2, lambda_=0.5)
    assert [r.chunk_id for r in out] == ["A", "B"]


def test_mmr_edge_inputs():
    assert mmr_select([], np.array([1.0, 0, 0]), {}, 3) == []
    assert mmr_select([A, B, C], np.array([1.0, 0, 0]), {"A": np.array([1.0, 0, 0])}, 0) == []
    assert mmr_select([A, B], np.array([1.0, 0, 0]), {"A": np.array([1.0, 0, 0]), "B": np.array([0, 1.0, 0])}, 3) == [A, B]


def test_rerank_noop_without_loading_model(monkeypatch):
    def boom(_self):
        raise AssertionError("model must not load")

    monkeypatch.setattr(CrossEncoderReranker, "_ensure_model", boom)
    r = CrossEncoderReranker()
    assert r.rerank("q", [], 3) == []
    results = [A, B]
    assert r.rerank("q", results, 5) == results


def test_fetch_embeddings_roundtrip():
    store = SQLiteVecStore(db_path=":memory:", dim=3)
    ids = ["a", "b"]
    store.insert_batch(
        ids=ids,
        embeddings=np.array([[1.0, 0, 0], [0, 1.0, 0]], dtype=np.float32),
        sources=["s.txt", "s.txt"],
        chunk_indexes=[0, 1],
        contents=["alpha", "beta"],
        doc_hashes=["h", "h"],
    )
    fetched = store.fetch_embeddings(ids)
    assert set(fetched) == {"a", "b"}
    assert np.allclose(fetched["a"], [1.0, 0, 0])
    assert store.fetch_embeddings([]) == {} and store.fetch_embeddings(["nope"]) == {}