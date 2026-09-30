"""Cross-workspace shared documents (Option B: one file, many names).

A shared document is stored once under ``DATA_DIR/_shared/`` and linked into
each subscribing workspace folder as a plain file name. The existing recursive
ingest glob then picks it up like any other document, so retrieval, reranking
and citation work unchanged and each workspace keeps its own physical index.

Link semantics
--------------
``os.link`` (hardlink) is preferred: the link and the canonical file are the
same inode, so replacing the canonical content is immediately visible in every
workspace and cannot drift. A symlink is the fallback when hardlinks are
unavailable (different filesystem, or a platform that forbids them). Both
behaviours are one file name to the ingest pipeline, so the fallback is
transparent to retrieval.

Deliberately not supported: copying the file into each workspace. That is the
"duplicate upload" option this design exists to avoid, since copies drift.
"""

import errno
import hashlib
import logging
import os
from pathlib import Path

from chatbot.workspaces import SHARED_DIR_NAME, root_data_dir

logger = logging.getLogger("esg.shared_docs")

HARDLINK = "hardlink"
SYMLINK = "symlink"

# A link target that exists but resolves somewhere else, or a stray file
# sharing a name with a registered target, is reported as these states so the
# admin console can surface drift rather than silently serving stale content.
LINK_OK = "ok"
LINK_MISSING = "missing"      # registered, but the canonical file is gone
LINK_BROKEN = "broken"        # registered, but the link is gone from the folder
LINK_DIVERGED = "diverged"    # registered, but the folder no longer matches it

# (path, size, mtime_ns) -> sha256, so health checks stay cheap on repeat calls.
_DIGEST_CACHE = {}


def clear_digest_cache():
    _DIGEST_CACHE.clear()


def shared_root() -> Path:
    """The canonical store. Lives under DATA_DIR so it is never a workspace."""
    return Path(root_data_dir()) / SHARED_DIR_NAME


def safe_name(raw):
    """Reduce an uploaded/entered name to a bare file name.

    Rejects anything that could escape the target directory. Returns "" when
    the input is unusable.
    """
    name = os.path.basename((raw or "").replace("\\", "/").strip())
    if name in ("", ".", "..") or name.startswith("."):
        return ""
    return name


def canonical_path(filename):
    name = safe_name(filename)
    if not name:
        raise ValueError("Invalid document name.")
    return shared_root() / name


def _is_valid_category(category):
    """Reject anything that is not a bare workspace category.

    Callers already validate against workspace_names(), but this is the function
    that builds a filesystem path from untrusted input, so it refuses to
    construct one from a category containing a separator or '..'.
    """
    cat = (category or "").strip()
    if not cat or cat in (".", "..") or cat == SHARED_DIR_NAME:
        return False
    if "/" in cat or "\\" in cat or os.sep in cat:
        return False
    return cat == os.path.basename(cat)


def target_path(category, link_name):
    """Where a shared document's name lives inside a workspace folder."""
    if not _is_valid_category(category):
        raise ValueError("Invalid workspace.")
    name = safe_name(link_name)
    if not name:
        raise ValueError("Invalid link name.")
    if name == SHARED_DIR_NAME:
        raise ValueError("A shared document cannot be named after the shared store.")
    return Path(root_data_dir()) / category / name


def same_file(a, b):
    """True when both paths resolve to the same underlying file.

    Handles hardlinks (same inode) and symlinks (same resolved path).
    """
    pa, pb = Path(a), Path(b)
    if not (pa.exists() and pb.exists()):
        return False
    try:
        if pa.samefile(pb):
            return True
    except OSError:
        pass
    try:
        return os.path.realpath(pa) == os.path.realpath(pb)
    except OSError:
        return False


def link_status(canonical, target):
    """Classify how a target currently relates to its canonical document."""
    target = Path(target)
    if not target.exists() and not target.is_symlink():
        return LINK_BROKEN
    if not Path(canonical).exists():
        return LINK_MISSING
    return LINK_OK if same_file(canonical, target) else LINK_DIVERGED


def create_link(canonical, target):
    """Point `target` at `canonical` as a hardlink, falling back to a symlink.

    Any existing entry at `target` is removed first, but only after confirming
    it is safe to do so: the caller is responsible for having checked that
    `target` is not an unrelated file the operator cares about (see
    `conflicting_target`).
    """
    canonical = Path(canonical)
    target = Path(target)
    if not canonical.is_file():
        raise ValueError("Shared document not found.")

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        target.unlink()

    try:
        os.link(canonical, target)
        return HARDLINK
    except OSError as exc:
        if exc.errno not in (
            errno.EXDEV,
            errno.EPERM,
            errno.EMLINK,
            errno.ENOSYS,
            errno.EOPNOTSUPP,
            errno.EACCES,
        ):
            raise
        logger.info(
            "Hardlink unavailable for %s (%s); falling back to a symlink.",
            target,
            exc.strerror or exc,
        )

    os.symlink(os.path.abspath(canonical), target)
    return SYMLINK


def conflicting_target(canonical, target):
    """Return True when `target` holds an unrelated file that must not be lost.

    Guards two cases: a private document already sitting in a workspace under
    the same name, and a name already claimed by a different shared document.
    """
    target = Path(target)
    if not target.exists() and not target.is_symlink():
        return False
    return not same_file(canonical, target)


def replace_canonical(canonical, new_bytes):
    """Write new content to the canonical path, preserving existing links.

    Writing through the existing inode keeps hardlinked targets in sync. When
    the canonical was created as a symlink target by another system we still
    write in place for the same reason.
    """
    canonical = Path(canonical)
    canonical.parent.mkdir(parents=True, exist_ok=True)
    with open(canonical, "wb") as fh:
        fh.write(new_bytes)
    return canonical.stat().st_size


def file_digest(path):
    """SHA-256 of a file's bytes, or None when it cannot be read.

    Cached on (size, mtime_ns) so repeated admin-page loads do not re-hash
    every shared document. Content is the authoritative drift signal: an inode
    comparison alone can pass when the filesystem recycles an inode number
    after a link is removed and an unrelated file takes its place.
    """
    try:
        st = os.stat(path)
    except OSError:
        _DIGEST_CACHE.pop((str(path), None, None), None)
        return None
    key = (str(path), st.st_size, st.st_mtime_ns)
    cached = _DIGEST_CACHE.get(key)
    if cached is not None:
        return cached
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(block)
    except OSError:
        return None
    digest = h.hexdigest()
    if len(_DIGEST_CACHE) > 512:
        _DIGEST_CACHE.clear()
    _DIGEST_CACHE[key] = digest
    return digest


def verify_links(rows, canonical_lookup):
    """Attach a health state to each target row.

    `rows` are registry target dicts; `canonical_lookup(filename)` returns the
    canonical Path or None. Returns a new list with `state` and `state_detail`
    keys added, leaving the input untouched.
    """
    out = []
    for row in rows:
        canonical = canonical_lookup(row.get("filename"))
        entry = dict(row)
        if canonical is None or not Path(canonical).exists():
            entry["state"] = LINK_MISSING
            entry["state_detail"] = "The shared document is missing."
            out.append(entry)
            continue
        try:
            target = target_path(row["category"], row["link_name"])
        except ValueError as exc:
            entry["state"] = LINK_DIVERGED
            entry["state_detail"] = str(exc)
            out.append(entry)
            continue

        if not target.exists() and not target.is_symlink():
            entry["state"] = LINK_BROKEN
            entry["state_detail"] = "The link is missing from the workspace folder."
        elif not same_file(canonical, target):
            entry["state"] = LINK_DIVERGED
            entry["state_detail"] = (
                "The file in the workspace no longer points at the shared document."
            )
        elif file_digest(target) != file_digest(canonical):
            entry["state"] = LINK_DIVERGED
            entry["state_detail"] = (
                "The copy in the workspace has different content from the "
                "shared document."
            )
        else:
            entry["state"] = LINK_OK
            entry["state_detail"] = "Linked."
        out.append(entry)
    return out
