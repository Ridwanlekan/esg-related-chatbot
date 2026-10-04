import glob
import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass

from chatbot import docling_loader, model_utils
from chatbot.docling_loader import DOCLING_EXTENSIONS, ConversionError, clean_docling_text

logger = logging.getLogger("esg.ingest")

DEFAULT_CHUNK_SIZE = 500
DEFAULT_OVERLAP = 50

# Every file type Docling can parse, plus plain-text files.
DEFAULT_PATTERNS = tuple(
    f"*.{ext}" for ext in sorted(DOCLING_EXTENSIONS | {"txt", "log"})
)

VET_DIR_NAME = ".docling_vet"

# Bumped whenever the chunking contract changes in a way an existing index cannot
# satisfy — adding page attribution to chunks was the first. It is mixed into the
# stored hash so every document is re-extracted exactly once after the change and
# then goes back to skipping on an unchanged hash. Without it, a document indexed
# before page tracking would match its own hash forever and never gain pages.
INGEST_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class IngestStats:
    documents_seen: int
    documents_reindexed: int
    chunks_upserted: int
    stale_chunks_removed: int
    documents_failed: int = 0


@dataclass(frozen=True)
class ExtractedText:
    text: str
    from_docling: bool = False
    # [(page_no, text)] when the source is paginated, else empty. Used to give
    # every chunk an exact page, so a citation can say which page to open.
    pages: tuple = ()


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


def _doc_hash(raw):
    """Content hash of a file, stamped with the indexer version that read it.

    Compared against the stored value and never parsed, so the suffix costs
    nothing and buys a single guaranteed reindex whenever INGEST_SCHEMA_VERSION
    moves. Without it, documents already in the index would report themselves
    unchanged and keep the chunk layout of whatever version first saw them.
    """
    return f"{hashlib.sha256(raw).hexdigest()}:v{INGEST_SCHEMA_VERSION}"


def _decode(raw):
    for encoding in ("utf-8", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _docling_extract(path, loader):
    """Convert via Docling, returning `(markdown, pages)`.

    `pages` is `[(page_no, text), ...]` for a paginated document, else empty.
    Returns `(None, ())` when the format isn't handled.

    A loader passed explicitly bypasses the global availability check so
    tests (and callers) can inject a stub. Raises ConversionError if the
    conversion itself fails, but never for a failure to split pages: page
    numbers are an enrichment, and not having them must not stop a document
    from being indexed.
    """
    if loader is None:
        if not (docling_loader.enabled() and docling_loader.is_available()):
            return None, ()
        loader = docling_loader.get_loader()
    if not loader.supports(path):
        return None, ()

    pages = ()
    text = None
    convert_both = getattr(loader, "convert_both", None)
    try:
        if convert_both is not None:
            text, raw_pages = convert_both(path)
        else:
            text = loader.convert(path)
            convert_pages = getattr(loader, "convert_pages", None)
            raw_pages = convert_pages(path) if convert_pages else None
    except ConversionError:
        # A real conversion failure, not a missing enrichment: let the caller
        # decide whether to fall back to plain-text decoding.
        raise
    except Exception as exc:
        logger.debug("Per-page extraction unavailable for %s: %s", path, exc)
        raw_pages = None
    if raw_pages:
        pages = tuple(
            (int(no), txt) for no, txt in raw_pages if txt and txt.strip()
        )
    return text, pages


def _extract_document(path, loader=None):
    """Best-effort extraction. Returns an ExtractedText (already cleaned for
    chunking) or None if the file should be skipped.

    Order: Docling (documents, images, audio, video) → plain-text decode for
    text-like formats. Binary formats that fail conversion are skipped.
    """
    try:
        text, pages = _docling_extract(path, loader)
    except ConversionError as exc:
        logger.warning("Docling conversion failed for %s: %s", path, exc)
        text, pages = None, ()
    if text is not None:
        return ExtractedText(
            text=clean_docling_text(text), from_docling=True, pages=pages
        )

    ext = docling_loader.extension_of(path)
    if ext in docling_loader.TEXT_FALLBACK_EXTENSIONS or ext in ("txt", "log", ""):
        text = _decode(_read_bytes(path)).strip()
        if text:
            return ExtractedText(text=text)
    return None


def chunk_with_pages(extracted, chunk_size=DEFAULT_CHUNK_SIZE, overlap=DEFAULT_OVERLAP):
    """Split a document into (chunk, page_start, page_end) triples.

    Paginated documents are chunked page by page. That costs a little context at
    each page break, where a paragraph could otherwise straddle two pages, and
    buys an unambiguous answer to "which page is this quote on?" — the property
    the whole citation feature rests on. Trying to recover pages after the fact
    by matching offsets against the concatenated text would have to guess at
    where Docling's Markdown serialiser inserted separators, and a wrong page
    number on a compliance citation is worse than none.

    Unpaginated sources keep the original whole-document behaviour, so plain text
    and HTML retrieval is untouched.
    """
    if not extracted.pages:
        return [(chunk, None, None) for chunk in recursive_split(extracted.text, chunk_size, overlap)]
    triples = []
    for page_no, page_text in extracted.pages:
        for chunk in recursive_split(page_text, chunk_size, overlap):
            triples.append((chunk, page_no, page_no))
    return triples


def _vet_base(data_dir):
    # Generated artifacts live at the repo root (like .index/), never inside
    # the source data directory. Override with DOCLING_VET_DIR.
    value = os.environ.get("DOCLING_VET_DIR")
    if value is None:
        return os.path.join(model_utils.BASE_DIR, VET_DIR_NAME)
    if value.strip().lower() in ("0", "false", "off", "no", "none", ""):
        return None
    return value


def _vet_filename(source):
    root, _ = os.path.splitext(source)
    return root if root else source


def _write_vet(base, source, content):
    out = os.path.join(base, _vet_filename(source) + ".md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(content)


class IngestProgress:
    """Thread-safe in-memory snapshot of the most recent ingestion run.

    A single global instance is used so callers (API status endpoint, UI
    poller, CLIs) can observe whatever ingestion is currently running.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.reset()

    def reset(self):
        with self._lock:
            self.running = False
            self.started_at = None
            self.total_files = 0
            self.processed_files = 0
            self.current_file = None
            self.documents_reindexed = 0
            self.documents_failed = 0
            self.chunks_upserted = 0
            self.finished_at = None
            self.error = None

    def start(self, total_files):
        with self._lock:
            self.reset()
            self.running = True
            self.started_at = time.time()
            self.total_files = total_files

    def begin_file(self, source):
        with self._lock:
            self.current_file = source

    def end_file(self, reindexed=False, failed=False, chunks=0):
        with self._lock:
            self.processed_files += 1
            self.documents_reindexed += 1 if reindexed else 0
            self.documents_failed += 1 if failed else 0
            self.chunks_upserted += chunks

    def finish(self, error=None):
        with self._lock:
            self.running = False
            self.finished_at = time.time()
            self.current_file = None
            self.error = error

    def snapshot(self):
        with self._lock:
            elapsed = None
            if self.started_at is not None:
                elapsed = round((self.finished_at or time.time()) - self.started_at, 1)
            return {
                "running": self.running,
                "started_at": self.started_at,
                "elapsed_seconds": elapsed,
                "total_files": self.total_files,
                "processed_files": self.processed_files,
                "current_file": self.current_file,
                "documents_reindexed": self.documents_reindexed,
                "documents_failed": self.documents_failed,
                "chunks_upserted": self.chunks_upserted,
                "error": self.error,
            }


INGEST_PROGRESS = IngestProgress()


def ingest_documents(
    data_dir,
    store,
    embed_fn,
    patterns=DEFAULT_PATTERNS,
    batch_size=64,
    loader=None,
):
    files = []
    for pattern in patterns:
        files.extend(
            glob.glob(os.path.join(data_dir, "**", pattern), recursive=True)
        )
    vet_base = _vet_base(data_dir)
    files = sorted(
        {
            os.path.abspath(f)
            for f in files
            if vet_base is None
            or (
                not f.startswith(vet_base + os.sep)
                and not f.startswith(os.path.abspath(vet_base) + os.sep)
            )
        }
    )
    rel_sources = [os.path.relpath(path, data_dir) for path in files]

    pending_ids = []
    pending_sources = []
    pending_indexes = []
    pending_contents = []
    pending_hashes = []
    pending_page_starts = []
    pending_page_ends = []

    def flush():
        nonlocal pending_ids, pending_sources, pending_indexes, pending_contents
        nonlocal pending_hashes, pending_page_starts, pending_page_ends
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
            page_starts=pending_page_starts,
            page_ends=pending_page_ends,
        )
        n = len(pending_ids)
        pending_ids = []
        pending_sources = []
        pending_indexes = []
        pending_contents = []
        pending_hashes = []
        pending_page_starts = []
        pending_page_ends = []
        return n

    chunks_upserted = 0
    documents_failed = 0
    documents_reindexed = 0
    progress = INGEST_PROGRESS
    progress.start(len(files))
    started = time.time()
    logger.info(
        "Ingestion started: %d file(s) under %s (chunk_size=%d, overlap=%d)",
        len(files),
        data_dir,
        DEFAULT_CHUNK_SIZE,
        DEFAULT_OVERLAP,
    )
    try:
        for i, (path, source) in enumerate(zip(files, rel_sources), 1):
            progress.begin_file(source)
            raw = _read_bytes(path)
            doc_hash = _doc_hash(raw)
            if store.get_doc_hash(source) == doc_hash:
                logger.debug("  [%d/%d] unchanged, skipping %s", i, len(files), source)
                progress.end_file()
                continue
            logger.info("[%d/%d] %s", i, len(files), source)
            if store.get_doc_hash(source) is not None:
                logger.debug("  -> changed since last ingest")
                store.delete_source(source)
            extracted = _extract_document(path, loader=loader)
            if extracted is None:
                logger.warning("  -> no text extracted; skipping %s", source)
                documents_failed += 1
                progress.end_file(failed=True)
                continue
            if extracted.from_docling and vet_base is not None:
                _write_vet(vet_base, source, extracted.text)
            chunked = chunk_with_pages(extracted)
            logger.info(
                "  -> %d chunk(s)%s",
                len(chunked),
                f" across {len(extracted.pages)} page(s)" if extracted.pages else "",
            )
            for index, (chunk, page_start, page_end) in enumerate(chunked):
                chunk_id = hashlib.sha256(
                    f"{source}::{index}::{doc_hash}".encode()
                ).hexdigest()
                pending_ids.append(chunk_id)
                pending_sources.append(source)
                pending_indexes.append(index)
                pending_contents.append(chunk)
                pending_hashes.append(doc_hash)
                pending_page_starts.append(page_start)
                pending_page_ends.append(page_end)
            documents_reindexed += 1
            progress.end_file(reindexed=True, chunks=len(chunked))
            if len(pending_ids) >= batch_size:
                chunks_upserted += flush()

        chunks_upserted += flush()
    except Exception as exc:
        progress.finish(error=str(exc))
        raise
    stale_removed = store.delete_where_source_not_in(rel_sources)
    progress.finish()
    logger.info(
        "Ingestion done: %d reindexed, %d failed, %d chunks upserted, "
        "%d stale removed (%.1fs)",
        documents_reindexed,
        documents_failed,
        chunks_upserted,
        stale_removed,
        time.time() - started,
    )

    return IngestStats(
        documents_seen=len(files),
        documents_reindexed=documents_reindexed,
        chunks_upserted=chunks_upserted,
        stale_chunks_removed=stale_removed,
        documents_failed=documents_failed,
    )