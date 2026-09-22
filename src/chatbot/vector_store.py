import os
import re
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np
import sqlite_vec


@dataclass(frozen=True)
class SearchResult:
    chunk_id: str
    source: str
    chunk_index: int
    content: str
    distance: float
    score: float | None = None

    @property
    def similarity(self):
        return 1.0 - self.distance


class VectorStore(ABC):
    @abstractmethod
    def insert_batch(
        self,
        ids,
        embeddings,
        sources,
        chunk_indexes,
        contents,
        doc_hashes,
    ):
        pass

    @abstractmethod
    def get_doc_hash(self, source):
        pass

    @abstractmethod
    def delete_source(self, source):
        pass

    @abstractmethod
    def delete_where_source_not_in(self, sources):
        pass

    @abstractmethod
    def search(self, embedding, k=3, source=None):
        pass

    @abstractmethod
    def search_hybrid(self, query_text, query_embedding, k=3, source=None, candidate_k=30, rrf_k=60):
        pass

    @abstractmethod
    def fetch_embeddings(self, chunk_ids):
        pass

    @abstractmethod
    def count(self):
        pass

    @abstractmethod
    def close(self):
        pass


def _normalize(vectors):
    vectors = np.asarray(vectors, dtype=np.float32)
    if vectors.ndim == 1:
        vectors = vectors.reshape(1, -1)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (vectors / norms).astype(np.float32)


def _tokenize_query(text):
    tokens = {t for t in re.split(r"[^A-Za-z0-9_]+", text.lower()) if t}
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens)


class SQLiteVecStore(VectorStore):
    def __init__(self, db_path, dim=384):
        self.db_path = db_path
        self.dim = dim
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.enable_load_extension(True)
        sqlite_vec.load(self.conn)
        self._create_schema()

    def _create_schema(self):
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS chunks ("
            "id TEXT PRIMARY KEY, "
            "source TEXT NOT NULL, "
            "chunk_index INTEGER NOT NULL, "
            "content TEXT NOT NULL, "
            "doc_hash TEXT NOT NULL)"
        )
        self.conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks "
            f"USING vec0(embedding float[{self.dim}], source text)"
        )
        self.conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts "
            "USING fts5(content, source UNINDEXED)"
        )
        self._backfill_fts_if_needed()
        self.conn.commit()

    def _backfill_fts_if_needed(self):
        fts_count = self.conn.execute("SELECT COUNT(*) AS c FROM chunks_fts").fetchone()["c"]
        chunk_count = self.conn.execute("SELECT COUNT(*) AS c FROM chunks").fetchone()["c"]
        if fts_count == 0 and chunk_count:
            self.conn.execute(
                "INSERT INTO chunks_fts (rowid, content, source) "
                "SELECT rowid, content, source FROM chunks"
            )

    def insert_batch(self, ids, embeddings, sources, chunk_indexes, contents, doc_hashes):
        embeddings = _normalize(embeddings)
        if embeddings.shape[1] != self.dim:
            raise ValueError(
                f"embedding dim {embeddings.shape[1]} != store dim {self.dim}; "
                "reindex with the matching embedding model"
            )
        try:
            for chunk_id, source, idx, content, doc_hash, vec in zip(
                ids, sources, chunk_indexes, contents, doc_hashes, embeddings
            ):
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO chunks (id, source, chunk_index, content, doc_hash) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (chunk_id, source, idx, content, doc_hash),
                )
                if cur.rowcount == 0:
                    continue
                rowid = self.conn.execute(
                    "SELECT rowid FROM chunks WHERE id = ?", (chunk_id,)
                ).fetchone()["rowid"]
                self.conn.execute(
                    "INSERT INTO vec_chunks (rowid, embedding, source) VALUES (?, ?, ?)",
                    (rowid, vec.tobytes(), source),
                )
                self.conn.execute(
                    "INSERT INTO chunks_fts (rowid, content, source) VALUES (?, ?, ?)",
                    (rowid, content, source),
                )
        except sqlite3.IntegrityError:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

    def get_doc_hash(self, source):
        row = self.conn.execute(
            "SELECT doc_hash FROM chunks WHERE source = ? LIMIT 1", (source,)
        ).fetchone()
        return row["doc_hash"] if row else None

    def delete_source(self, source):
        rowids = [
            r["rowid"]
            for r in self.conn.execute(
                "SELECT rowid FROM chunks WHERE source = ?", (source,)
            ).fetchall()
        ]
        if rowids:
            self._delete_rowids(rowids)

    def delete_where_source_not_in(self, sources):
        if not sources:
            count = self.count()
            self.conn.execute("DELETE FROM vec_chunks")
            self.conn.execute("DELETE FROM chunks_fts")
            self.conn.execute("DELETE FROM chunks")
            self.conn.commit()
            return count
        rowids = [
            r["rowid"]
            for r in self.conn.execute(
                f"SELECT rowid FROM chunks WHERE source NOT IN ({', '.join('?' for _ in sources)})",
                list(sources),
            ).fetchall()
        ]
        count = len(rowids)
        if rowids:
            self._delete_rowids(rowids)
        return count

    def _delete_rowids(self, rowids):
        for rowid in rowids:
            self.conn.execute("DELETE FROM vec_chunks WHERE rowid = ?", (rowid,))
            self.conn.execute("DELETE FROM chunks_fts WHERE rowid = ?", (rowid,))
        self.conn.execute(
            f"DELETE FROM chunks WHERE rowid IN ({', '.join('?' for _ in rowids)})", rowids
        )
        self.conn.commit()

    def _fetch(self, rowids):
        meta = self.conn.execute(
            f"SELECT rowid, id, source, chunk_index, content FROM chunks "
            f"WHERE rowid IN ({', '.join('?' for _ in rowids)})",
            rowids,
        ).fetchall()
        return {row["rowid"]: row for row in meta}

    def search(self, embedding, k=3, source=None):
        q = _normalize(np.asarray(embedding, dtype=np.float32))[0]
        if source is None:
            rows = self.conn.execute(
                "SELECT rowid, distance FROM vec_chunks "
                "WHERE embedding MATCH ? AND k = ?",
                (q.tobytes(), k),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT rowid, distance FROM vec_chunks "
                "WHERE source = ? AND embedding MATCH ? AND k = ?",
                (source, q.tobytes(), k),
            ).fetchall()
        if not rows:
            return []
        meta = self._fetch([rowid for rowid, _ in rows])
        results = []
        for rowid, dist in rows:
            row = meta.get(rowid)
            if row is None:
                continue
            results.append(
                SearchResult(
                    chunk_id=row["id"],
                    source=row["source"],
                    chunk_index=row["chunk_index"],
                    content=row["content"],
                    distance=dist,
                )
            )
        results.sort(key=lambda r: r.distance)
        return results

    def _search_lexical(self, query_text, k=30, source=None):
        match = _tokenize_query(query_text)
        if not match:
            return []
        if source is None:
            rows = self.conn.execute(
                "SELECT rowid, bm25(chunks_fts) AS bm25 FROM chunks_fts "
                "WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts) LIMIT ?",
                (match, k),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT rowid, bm25(chunks_fts) AS bm25 FROM chunks_fts "
                "WHERE source = ? AND chunks_fts MATCH ? "
                "ORDER BY bm25(chunks_fts) LIMIT ?",
                (source, match, k),
            ).fetchall()
        if not rows:
            return []
        meta = self._fetch([rowid for rowid, _ in rows])
        results = []
        for rowid, raw in rows:
            row = meta.get(rowid)
            if row is None:
                continue
            results.append(
                SearchResult(
                    chunk_id=row["id"],
                    source=row["source"],
                    chunk_index=row["chunk_index"],
                    content=row["content"],
                    distance=0.0,
                    score=-raw,
                )
            )
        results.sort(key=lambda r: r.score, reverse=True)
        return results

    def search_hybrid(self, query_text, query_embedding, k=3, source=None, candidate_k=30, rrf_k=60):
        dense_hits = self.search(query_embedding, k=candidate_k, source=source)
        lexical_hits = self._search_lexical(query_text, k=candidate_k, source=source)

        fused = {}
        for hits in (dense_hits, lexical_hits):
            for rank, hit in enumerate(hits):
                entry = fused.setdefault(
                    hit.chunk_id,
                    {"score": 0.0, "result": hit},
                )
                entry["score"] += 1.0 / (rrf_k + rank + 1)

        ranked = sorted(
            fused.values(),
            key=lambda entry: entry["score"],
            reverse=True,
        )[:k]
        return [
            SearchResult(
                chunk_id=entry["result"].chunk_id,
                source=entry["result"].source,
                chunk_index=entry["result"].chunk_index,
                content=entry["result"].content,
                distance=entry["result"].distance,
                score=entry["score"],
            )
            for entry in ranked
        ]

    def fetch_embeddings(self, chunk_ids):
        if not chunk_ids:
            return {}
        placeholders = ", ".join("?" for _ in chunk_ids)
        rows = self.conn.execute(
            f"SELECT c.id, v.embedding FROM chunks c "
            f"JOIN vec_chunks v ON v.rowid = c.rowid "
            f"WHERE c.id IN ({placeholders})",
            list(chunk_ids),
        ).fetchall()
        return {
            row["id"]: np.frombuffer(row["embedding"], dtype=np.float32)
            for row in rows
        }

    def count(self):
        return self.conn.execute("SELECT COUNT(*) AS c FROM chunks").fetchone()["c"]

    def close(self):
        self.conn.close()