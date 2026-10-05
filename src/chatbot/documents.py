"""Resolve a cited `source` string to a real file on disk.

Citations are this platform's trust surface: a user who can open the document an
answer came from can verify it, and a user who cannot has to take our word for
it. That makes this module a security boundary rather than a convenience.

The rule is deliberately narrow. A citation may only ever reach a file inside
the requesting workspace's own data directory, or the canonical shared store in
data/_shared. Nothing else on the filesystem is reachable, whatever the caller
sends, and the category is resolved against the live workspace registry so it
cannot be used as a traversal vector in its own right.
"""

import os
from pathlib import Path

from chatbot.workspaces import SHARED_DIR_NAME, is_workspace, root_data_dir

MAX_SOURCE_LENGTH = 512


class DocumentNotFound(Exception):
    """Raised for any citation that cannot be resolved to a readable file.

    One exception type for "unauthorised", "malformed" and "absent" on purpose:
    the endpoint maps all of them to 404 so the response never confirms that a
    document the caller is not entitled to exists.
    """


def _normalize(source):
    """Validate a citation string and return it as a clean relative path.

    Rejects rather than sanitises. A citation is produced by this service, so a
    value containing traversal or absolute-path syntax means something upstream
    is wrong; quietly rewriting it would hide that.
    """
    if not isinstance(source, str) or not source.strip():
        raise DocumentNotFound("empty document reference")
    if len(source) > MAX_SOURCE_LENGTH:
        raise DocumentNotFound("document reference too long")
    if "\x00" in source:
        raise DocumentNotFound("illegal character in document reference")
    name = source.replace("\\", "/").strip()
    if name.startswith("/") or os.path.isabs(name):
        raise DocumentNotFound("absolute document reference")
    segments = name.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        raise DocumentNotFound("illegal path segment in document reference")
    return "/".join(segments)


def _within(path, root):
    """True when `path` is `root` itself or lives under it."""
    try:
        return os.path.commonpath([str(path), str(root)]) == str(root)
    except ValueError:  # different drives, or a relative/absolute mix
        return False


def _allowed_roots(category):
    base = Path(os.path.realpath(root_data_dir()))
    return [base / category, base / SHARED_DIR_NAME]


def _candidate_paths(category, name):
    """Absolute paths a citation `name` may denote, most specific first.

    Ingest records sources relative to the data root, so a real citation reads
    "finance/policy.pdf" or "_shared/handbook.pdf" — that string is what lands in
    a search result, in a stored session and in a signed link. A reference already
    carrying one of those two prefixes is therefore resolved from the root.

    Anything else is read as relative to the caller's own workspace, which is the
    form older sessions and hand-built links contain. Both forms are confined to
    the same two roots afterwards, so accepting the root-relative one widens
    nothing: a citation naming another workspace still resolves inside the
    caller's own directory, where it does not exist.
    """
    base = Path(os.path.realpath(root_data_dir()))
    head, slash, _tail = name.partition("/")
    if slash and head in (category, SHARED_DIR_NAME):
        return [base / name]
    return [base / category / name, base / SHARED_DIR_NAME / name]


def resolve_document(category, source):
    """Return the absolute path of `source` as seen from `category`'s workspace.

    Raises DocumentNotFound if the category is not a workspace, the reference is
    malformed, the resolved path escapes both allowed roots, or nothing is there.
    """
    if not is_workspace(category):
        raise DocumentNotFound("unknown workspace")
    name = _normalize(source)
    for candidate in _candidate_paths(category, name):
        target = Path(os.path.realpath(candidate))
        if not any(_within(target, root) for root in _allowed_roots(category)):
            raise DocumentNotFound("document is outside the workspace")
        if target.is_file():
            return target
    raise DocumentNotFound("document does not exist")