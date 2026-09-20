import glob
import hashlib
import os
from dataclasses import dataclass

DEFAULT_CHUNK_SIZE = 500
DEFAULT_OVERLAP = 50


@dataclass(frozen=True)
class IngestStats:
    documents_seen: int
    documents_reindexed: int
    chunks_upserted: int
    stale_chunks_removed: int


def recursive_split(text, chunk_size=DEFAULT_CHUNK_SIZE, overlap=DEFAULT_OVERLAP):
    text = text.replace("\r\n", "\n")
    separators = ["\n\n", "\n"]

    granular = None
    for sep in separators:
        if sep in text:
            granular = [seg for seg in text.split(sep) if seg.strip()]
            break
    if granular is None:
        granular = [text]

    chunks = []
    current = ""
    for seg in granular:
        seg = seg.strip()
        if not seg:
            continue
        if current and len(current) + len(seg) + 2 > chunk_size:
            chunks.append(current)
            current = seg
        elif not current:
            current = seg
        else:
            current = f"{current}\n{seg}"

        while len(current) > chunk_size:
            chunks.append(current[:chunk_size])
            current = current[chunk_size - overlap :]

    if current.strip():
        chunks.append(current)
    return [c.strip() for c in chunks if c.strip()]


def _read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def _decode(raw):
    for encoding in ("utf-8", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def ingest_documents(
    data_dir,
    store,
    embed_fn,
    patterns=("*.txt",),
    batch_size=64,
):
    files = []
    for pattern in patterns:
        files.extend(
            glob.glob(os.path.join(data_dir, "**", pattern), recursive=True)
        )
    files = sorted({os.path.abspath(f) for f in files})
    rel_sources = [os.path.relpath(path, data_dir) for path in files]

    pending_ids = []
    pending_sources = []
    pending_indexes = []
    pending_contents = []
    pending_hashes = []

    documents_reindexed = 0

    def flush():
        nonlocal pending_ids, pending_sources, pending_indexes, pending_contents, pending_hashes
        if not pending_ids:
            return 0
        embeddings = embed_fn(pending_contents)
        store.insert_batch(
            pending_ids,
            embeddings,
            pending_sources,
            pending_indexes,
            pending_contents,
            pending_hashes,
        )
        n = len(pending_ids)
        pending_ids = []
        pending_sources = []
        pending_indexes = []
        pending_contents = []
        pending_hashes = []
        return n

    chunks_upserted = 0
    for path, source in zip(files, rel_sources):
        raw = _read_bytes(path)
        doc_hash = hashlib.sha256(raw).hexdigest()
        if store.get_doc_hash(source) == doc_hash:
            continue
        if store.get_doc_hash(source) is not None:
            store.delete_source(source)
        text = _decode(raw)
        chunks = recursive_split(text)
        for index, chunk in enumerate(chunks):
            chunk_id = hashlib.sha256(
                f"{source}::{index}::{doc_hash}".encode()
            ).hexdigest()
            pending_ids.append(chunk_id)
            pending_sources.append(source)
            pending_indexes.append(index)
            pending_contents.append(chunk)
            pending_hashes.append(doc_hash)
        documents_reindexed += 1
        if len(pending_ids) >= batch_size:
            chunks_upserted += flush()

    chunks_upserted += flush()
    stale_removed = store.delete_where_source_not_in(rel_sources)

    return IngestStats(
        documents_seen=len(files),
        documents_reindexed=documents_reindexed,
        chunks_upserted=chunks_upserted,
        stale_chunks_removed=stale_removed,
    )