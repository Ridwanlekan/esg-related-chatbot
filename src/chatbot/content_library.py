"""Master library and per-organisation served copies (Section 3.4).

Two stores, one rule:

- The **master library** holds the curated originals. It is organised by ESG
  subject and legal regime for the curator's benefit, and no customer endpoint
  ever resolves a path into it.
- Each **served copy** is materialised per organisation at
  ``DATA_DIR/organisations/<organisation>/<workspace>/``, so two organisations
  both holding a "Finance" workspace cannot collide and an incorrect assignment
  fails by leaving an organisation *missing* content rather than serving another
  party's material.

Assignment is the only access control. Nothing filters content by jurisdiction or
any other attribute; the Administrator chooses, explicitly, what each organisation
receives.

The system sample organisation (``_sample``) is the one place where unrelated
parties deliberately share a served copy, which is what makes the free tier work.
"""

import errno
import logging
import os
import shutil
from pathlib import Path

from chatbot.users import SAMPLE_ORGANISATION_ID
from chatbot.workspaces import SHARED_DIR_NAME, root_data_dir

logger = logging.getLogger("esg.content_library")

# Directory name for the per-organisation served copies. Separate from
# SHARED_DIR_NAME: that one is org-internal content shared across a single
# organisation's workspaces, this one is the parent of all organisations.
ORGANISATIONS_DIR_NAME = "organisations"

# The launch pack. Q2 scopes the free tier to a single general document, which
# is enough for a visitor to judge the product without tailoring content to any
# one party (Section 3.5).
SAMPLE_PACK = ("overview_of_esg.pdf",)


def library_root() -> Path:
    """The curated master store. Platform Administrators only."""
    return Path(root_data_dir()) / "library"


def organisations_root() -> Path:
    """Parent of every organisation's served copy."""
    return Path(root_data_dir()) / ORGANISATIONS_DIR_NAME


def _safe_component(raw):
    """Reduce untrusted input to a single safe path component.

    Returns "" when the input could escape its directory. Used for every
    organisation and workspace name that reaches the filesystem, because a
    traversal here would escape the per-organisation isolation this whole design
    exists to provide.
    """
    name = (raw or "").strip()
    if not name or name in (".", "..") or name.startswith("."):
        return ""
    if "/" in name or "\\" in name or os.sep in name:
        return ""
    return name if name == os.path.basename(name) else ""


def organisation_dir(organisation_id):
    """The served-copy root for one organisation."""
    safe = _safe_component(organisation_id)
    if not safe:
        raise ValueError("Invalid organisation.")
    return organisations_root() / safe


def workspace_dir(organisation_id, category):
    """Where one workspace's documents and index live for one organisation."""
    safe_org = _safe_component(organisation_id)
    safe_ws = _safe_component(category)
    if not safe_org:
        raise ValueError("Invalid organisation.")
    if not safe_ws:
        raise ValueError("Invalid workspace.")
    if safe_ws == SHARED_DIR_NAME:
        raise ValueError("Invalid workspace.")
    return organisation_dir(safe_org) / safe_ws


def index_dir_for(organisation_id, category):
    """Index path for an organisation's workspace.

    Distinct per organisation on purpose: the isolation guarantee is physical,
    so one organisation's index cannot be read through another's.
    """
    safe_org = _safe_component(organisation_id)
    safe_ws = _safe_component(category)
    if not safe_org or not safe_ws:
        raise ValueError("Invalid organisation or workspace.")
    root = os.environ.get("INDEX_DIR", os.path.join(root_data_dir(), ".index"))
    return Path(root) / ORGANISATIONS_DIR_NAME / safe_org / f"vectors_{safe_ws}.sqlite3"


def is_sample(organisation_id):
    return organisation_id == SAMPLE_ORGANISATION_ID


def materialise(organisation_id, category, source_path, filename=None):
    """Copy one document into an organisation's served copy.

    A real copy rather than a hardlink: a served copy must not be able to write
    through to the master, and an organisation's material must stay stable even
    if the library file is replaced. Returns the destination path.
    """
    src = Path(source_path)
    if not src.is_file():
        raise ValueError(f"Source document not found: {src.name}")
    name = _safe_component(filename or src.name)
    if not name:
        raise ValueError("Invalid document name.")
    dest_dir = workspace_dir(organisation_id, category)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    if src.resolve() == dest.resolve():
        return dest
    tmp = dest.with_name(dest.name + ".partial")
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dest)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    return dest


def remove(organisation_id, category, filename):
    """Delete one document from a served copy. Returns True when a file went."""
    name = _safe_component(filename)
    if not name:
        raise ValueError("Invalid document name.")
    target = workspace_dir(organisation_id, category) / name
    if not target.exists():
        return False
    target.unlink()
    return True


def list_documents(organisation_id, category):
    """Documents currently served to an organisation's workspace."""
    folder = workspace_dir(organisation_id, category)
    if not folder.is_dir():
        return []
    return sorted(p.name for p in folder.iterdir() if p.is_file() and not p.name.startswith("."))


def seed_sample_pack(organisation_id=SAMPLE_ORGANISATION_ID, categories=("finance",), source_dir=None):
    """Assign the launch pack to the sample organisation.

    Idempotent, so it can run on every start without duplicating content. Only
    the sample organisation may be seeded this way: a customer organisation gets
    content through an explicit assignment, never as a side effect of startup.
    """
    if not is_sample(organisation_id):
        raise ValueError(
            "Only the system sample organisation may be seeded automatically."
        )
    source = Path(source_dir) if source_dir else library_root()
    placed = []
    for name in SAMPLE_PACK:
        candidate = source / name
        if not candidate.is_file():
            # Also accept a document already sitting at the data root, which is
            # where the pre-organisation build kept the loose pack.
            alt = Path(root_data_dir()) / name
            candidate = alt if alt.is_file() else candidate
        if not candidate.is_file():
            logger.warning("Sample pack document not found: %s", name)
            continue
        for category in categories:
            materialise(organisation_id, category, candidate, filename=name)
            placed.append((category, name))
    return placed


def purge_organisation(organisation_id):
    """Delete every served copy and index belonging to an organisation (Q12).

    Q12 asks for immediate deletion, so this does not retain a backup: the
    master library is the only copy of the originals and is untouched. Refuses
    system-owned organisations, since the sample pack is a shared asset.
    """
    if is_sample(organisation_id):
        raise ValueError("The system sample organisation cannot be purged.")
    root = organisation_dir(organisation_id)
    index_root = Path(
        os.environ.get("INDEX_DIR", os.path.join(root_data_dir(), ".index"))
    ) / ORGANISATIONS_DIR_NAME / _safe_component(organisation_id)
    removed = False
    for target in (root, index_root):
        if not target.exists():
            continue
        try:
            shutil.rmtree(target)
            removed = True
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise
    return removed