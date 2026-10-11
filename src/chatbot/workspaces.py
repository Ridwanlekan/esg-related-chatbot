import os
import sqlite3

from chatbot.model_utils import BASE_DIR

DEFAULT_WORKSPACES = "finance,hr"

# Canonical store for cross-workspace shared documents. It lives under the data
# root but is never a workspace, so it must never get its own index.
SHARED_DIR_NAME = "_shared"

WORKSPACE_LABELS = {
    "finance": {
        "label": "ESG Finance",
        "emoji": "\U0001F4CA",
        "blurb": "IFRS S1/S2 disclosures, ESG financial reporting and climate-risk data",
    },
    "hr": {
        "label": "People & HR",
        "emoji": "\U0001F465",
        "blurb": "ESG people metrics, workplace culture, diversity and HR policies",
    },
}

# Workspaces added/overridden by the admin (loaded from AdminStore). Populated
# via reload_extra_workspaces(); rows here take precedence over WORKSPACE_LABELS.
_EXTRA = {}


def reload_extra_workspaces(db_path=None):
    """(Re)load admin-defined workspace rows into the in-memory registry.

    Passing None clears the registry (test isolation / admin reset).
    """
    global _EXTRA
    if not db_path or not os.path.exists(db_path):
        _EXTRA = {}
        return _EXTRA
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT category, label, blurb, emoji FROM workspaces"
        ).fetchall()
        _EXTRA = {
            r["category"]: {k: r[k] for k in ("label", "blurb", "emoji")}
            for r in rows
        }
    finally:
        conn.close()
    return _EXTRA


def extra_workspaces():
    return dict(_EXTRA)


def clear_extra_workspaces():
    """Drop the admin overrides registry (test isolation / admin reset)."""
    global _EXTRA
    _EXTRA = {}


def root_data_dir():
    return os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))


def index_dir():
    return os.environ.get("INDEX_DIR", os.path.join(BASE_DIR, ".index"))


def workspace_names():
    raw = os.environ.get("WORKSPACES", DEFAULT_WORKSPACES)
    base = [c.strip() for c in raw.split(",") if c.strip()]
    names = base + [c for c in _EXTRA if c not in base]
    # Single choke point: the shared-document store is never a workspace, so a
    # misconfigured WORKSPACES value cannot give it an index.
    return [c for c in names if is_workspace(c)]


def is_workspace(name):
    """True when `name` is a real workspace.

    Guards the shared-document store: a misconfigured WORKSPACES value must
    not turn data/_shared into a workspace with its own index, which would
    silently ingest every shared document into a bogus scope.
    """
    return bool(name) and name != SHARED_DIR_NAME


def base_workspace_names():
    """Categories configured via the WORKSPACES env variable (not admin-added)."""
    raw = os.environ.get("WORKSPACES", DEFAULT_WORKSPACES)
    return [c.strip() for c in raw.split(",") if c.strip() and is_workspace(c.strip())]


def default_workspace_config():
    return {
        name: {
            "data_dir": os.path.join(root_data_dir(), name),
            "store_path": os.path.join(index_dir(), f"vectors_{name}.sqlite3"),
        }
        for name in workspace_names()
    }


def workspace_meta(category):
    meta = dict(WORKSPACE_LABELS.get(category, {}))
    override = _EXTRA.get(category) or {}
    for key in ("label", "blurb", "emoji"):
        if override.get(key):
            meta[key] = override[key]
    return meta


def system_prompt_for(category):
    meta = workspace_meta(category)
    label = meta.get("label", category.title())
    return (
        f"You are the {label} assistant of an ESG platform. "
        f"Use ONLY the retrieved context to answer the user's question. "
        f"Cite the source file in square brackets when you use it. "
        f"Answer strictly within {label} scope. If the question is not about "
        f"{label} topics, or the retrieved context does not contain the answer, "
        "say you cannot help with that topic and never answer from general "
        "knowledge or make up information."
    )


def organisation_workspace_config(organisation_id, category):
    """Config for one organisation's workspace: its own data dir and its own index.

    Phase 2 made assignment the only access control (Section 3.4). Serving content
    per organisation is only real if the index is per organisation too: two
    organisations both holding a "finance" workspace would otherwise share one
    vector store, and one could retrieve the other's material. The imported
    module is local because content_library imports this one.
    """
    from chatbot import content_library

    return {
        "data_dir": str(content_library.workspace_dir(organisation_id, category)),
        "store_path": str(content_library.index_dir_for(organisation_id, category)),
    }


def make_workspace_bot(category, config=None):
    from chatbot.rag import RAGBot

    conf = config if config is not None else default_workspace_config()[category]
    return RAGBot(
        store_path=conf["store_path"],
        data_dir=conf["data_dir"],
        system_prompt=system_prompt_for(category),
    )