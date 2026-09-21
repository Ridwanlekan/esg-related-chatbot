import os
import threading

import numpy as np
from sentence_transformers import CrossEncoder

from chatbot import model_utils

RERANKER_DEFAULT = "cross-encoder/ms-marco-MiniLM-L-6-v2"


def reranker_enabled():
    return os.environ.get("RERANKER_ENABLED", "1") != "0"


def mmr_lambda_value():
    return float(os.environ.get("MMR_LAMBDA", "0.5"))


def mmr_select(items, query_embedding, embeddings, k, lambda_=0.5):
    """Maximal Marginal Relevance over already-scored candidates.

    items: list of SearchResult, pre-ranked by relevance.
    embeddings: {chunk_id: normalized ndarray} for the candidates.
    lambda_: 1.0 = pure relevance, 0.0 = pure diversity (default 0.5).

    Greedy: repeatedly pick the candidate maximizing
        lambda_ * sim(query, i) - (1 - lambda_) * max_j sim(i, selected_j).
    If any candidate lacks an embedding (or lambda_ is 1.0), falls back to
    the first k items in original order.
    """
    if not items or k <= 0:
        return list(items[:k])
    if lambda_ >= 1.0:
        return list(items[:k])
    keyed = [(item, embeddings[item.chunk_id]) for item in items if item.chunk_id in embeddings]
    if not keyed or len(keyed) < len(items):
        return list(items[:k])

    k = min(k, len(items))
    query_vec = np.asarray(query_embedding, dtype=np.float32)
    vecs = np.stack([v for _, v in keyed]).astype(np.float32)
    sim_q = vecs @ query_vec
    sim_cross = vecs @ vecs.T
    selected = np.zeros(len(keyed), dtype=bool)
    order = []
    for _ in range(k):
        if selected.any():
            max_cross = sim_cross[:, selected].max(axis=1)
            score = lambda_ * sim_q - (1.0 - lambda_) * max_cross
        else:
            score = sim_q
        j = int(np.argmax(np.where(selected, -np.inf, score)))
        selected[j] = True
        order.append(keyed[j][0])
    return order


class CrossEncoderReranker:
    """Local cross-encoder (query, chunk) scorer. Lazy-loaded so retrieval
    pipelines that pass fewer results than top_k never touch the model."""

    def __init__(self, model_id=RERANKER_DEFAULT, model_path=None):
        self.model_id = model_id
        self.model_path = model_path
        self._model = None
        self._lock = threading.Lock()

    def _ensure_model(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    path = self.model_path or model_utils.env_or_local_model(
                        self.model_id, prefix="cross-encoder"
                    )
                    self._model = CrossEncoder(path)
        return self._model

    def rerank(self, query, results, top_k):
        if not results or top_k >= len(results):
            return list(results)
        model = self._ensure_model()
        pairs = [(query[:512], r.content[:512]) for r in results]
        scores = model.predict(pairs, show_progress_bar=False, batch_size=64)
        return [r for r, _ in sorted(zip(results, scores), key=lambda p: -p[1])[:top_k]]