#!/usr/bin/env python3
"""Reset a deployment to a clean testing state.

A full UI test wants a deployment that is empty apart from the sample launch
pack, so a tester can sign up, create an organisation, and assign content from
the master library without inheriting earlier test data.

Kept:
  * the sample launch pack in the master library (``data/library``); every
    other master document is removed unless ``--keep-library`` is given;
  * system organisations (``_sample`` and ``_default``) and the ``_sample``
    launch pack: its served copies and its per-organisation indexes.

Removed:
  * every customer organisation, its members, roles, served copies, per
    organisation indexes and content assignments;
  * the global (pre-organisation) workspace tree ``data/<workspace>`` and
    ``data/_shared``, plus the global workspace indexes;
  * admin-defined / custom workspaces and shared-document registrations;
  * master library documents other than the sample pack (see ``--keep-library``
    and ``--purge-library``).

Safe and rerunnable: it prints the plan and changes nothing unless ``--yes`` is
given, and running it twice is a no-op the second time. Paths honour ``DATA_DIR``
and ``INDEX_DIR``, so it also works against a mounted deployment volume.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from chatbot import content_library  # noqa: E402
from chatbot.admin_store import AdminStore  # noqa: E402
from chatbot.users import (  # noqa: E402
    DEFAULT_ORGANISATION_ID,
    SAMPLE_ORGANISATION_ID,
    UserStore,
)

# Directories under DATA_DIR that are never a workspace.
NON_WORKSPACE_DIRS = {"organisations", "library", "demo"}


def index_dir() -> Path:
    return Path(os.environ.get("INDEX_DIR", str(REPO_ROOT / ".index")))


def do(actions, desc, dry_run, fn):
    actions.append(desc)
    if not dry_run:
        fn()


def reset(args) -> int:
    dry_run = not args.yes
    data_root = Path(os.environ.get("DATA_DIR", str(REPO_ROOT / "data")))
    idx = index_dir()
    users_db = idx / "users.sqlite3"
    admin_db = idx / "workspaces.sqlite3"
    actions: list[str] = []

    if not users_db.exists():
        print(f"error: user store not found: {users_db}", file=sys.stderr)
        return 2

    user_store = UserStore(str(users_db))
    admin_store = AdminStore(str(admin_db)) if admin_db.exists() else None

    # The active index root, plus the DATA_DIR/.index fallback some deployments
    # (and older runs) leave behind. Both may hold per-organisation indexes.
    index_roots = [idx]
    nested = data_root / ".index"
    if nested != idx:
        index_roots.append(nested)

    print(f"DATA_DIR   {data_root}")
    print(f"INDEX_DIR  {idx}")
    if len(index_roots) > 1:
        print(f"           {nested}")
    print()

    # 1. Customer organisations: identity, served copies, assignments.
    orgs = user_store.list_organisations()
    keep_orgs = {SAMPLE_ORGANISATION_ID, DEFAULT_ORGANISATION_ID}
    keep_orgs |= {o["id"] for o in orgs if o.get("system_owned")}
    for org in orgs:
        org_id = org["id"]
        if org_id in keep_orgs:
            continue
        if admin_store is not None:
            for row in admin_store.list_content_assignments(org_id):
                do(
                    actions,
                    f"unassign {row['filename']} from {org_id}/{row['category']}",
                    dry_run,
                    lambda c=row["category"], f=row["filename"], o=org_id: (
                        admin_store.remove_content_assignment(o, c, f)
                    ),
                )
        do(
            actions,
            f"purge served copies and indexes for {org_id}",
            dry_run,
            lambda o=org_id: content_library.purge_organisation(o),
        )
        for root in index_roots:
            org_index = root / "organisations" / org_id
            do(
                actions,
                f"remove index directory {org_index}",
                dry_run,
                lambda p=org_index: shutil.rmtree(p, ignore_errors=True),
            )
        do(
            actions,
            f"delete organisation {org_id} (members, roles, memberships)",
            dry_run,
            lambda o=org_id: user_store.delete_organisation(o),
        )

    # 2. Global (pre-organisation) workspace tree and shared root.
    if data_root.is_dir():
        for path in sorted(data_root.iterdir()):
            if (
                path.is_dir()
                and not path.name.startswith(".")
                and path.name not in NON_WORKSPACE_DIRS
            ):
                do(
                    actions,
                    f"remove global workspace directory {path}",
                    dry_run,
                    lambda p=path: shutil.rmtree(p, ignore_errors=True),
                )

    # 3. Global workspace indexes (vectors_<category>.sqlite3, bare vectors).
    for root in index_roots:
        if not root.is_dir():
            continue
        for path in sorted(root.glob("vectors*.sqlite3")):
            for suffix in ("", "-wal", "-shm"):
                target = Path(str(path) + suffix)
                do(
                    actions,
                    f"remove global index {target}",
                    dry_run,
                    lambda p=target: p.unlink(missing_ok=True),
                )

    # 4. Admin-defined / custom workspaces and shared-document registrations.
    if admin_store is not None:
        for row in admin_store.list_workspaces():
            do(
                actions,
                f"remove custom workspace {row['category']}",
                dry_run,
                lambda c=row["category"]: admin_store.delete_workspace(c),
            )
        for doc in admin_store.list_shared_docs():
            do(
                actions,
                f"remove shared document {doc['filename']}",
                dry_run,
                lambda f=doc["filename"]: admin_store.remove_shared_doc(f),
            )

    # 5. Prune the master library to the sample pack.
    if not args.keep_library:
        keep = set() if args.purge_library else set(content_library.SAMPLE_PACK)
        lib_root = content_library.library_root()
        if lib_root.is_dir():
            for path in sorted(lib_root.iterdir()):
                if path.is_file() and path.name not in keep:
                    do(
                        actions,
                        f"remove master library file {path.name}",
                        dry_run,
                        lambda p=path: p.unlink(missing_ok=True),
                    )
        if admin_store is not None:
            for doc in admin_store.list_library_docs():
                if doc["filename"] not in keep:
                    do(
                        actions,
                        f"deregister master document {doc['filename']}",
                        dry_run,
                        lambda f=doc["filename"]: admin_store.remove_library_doc(f),
                    )

    # 6. Optional: free-tier accounts and sessions.
    if args.purge_free_accounts:
        rows = user_store.conn.execute(
            "SELECT id FROM users WHERE organisation_id = ?",
            (SAMPLE_ORGANISATION_ID,),
        ).fetchall()
        for (user_id,) in rows:
            do(
                actions,
                f"delete free account user {user_id}",
                dry_run,
                lambda u=user_id: user_store.delete_user(u),
            )

    if args.purge_sessions:
        for suffix in ("", "-wal", "-shm"):
            target = Path(str(idx / "sessions.sqlite3") + suffix)
            do(
                actions,
                f"remove sessions store {target.name}",
                dry_run,
                lambda p=target: p.unlink(missing_ok=True),
            )

    print(f"{'PLAN' if dry_run else 'APPLIED'} ({len(actions)} change(s)):")
    for line in actions:
        print(f"  - {line}")
    if not actions:
        print("  (nothing to do; already clean)")
    print()
    if dry_run:
        print("Dry run: nothing changed. Re-run with --yes to apply.")
    else:
        print("Done. Kept data/library and the _sample launch pack.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--yes",
        action="store_true",
        help="apply the changes (without this, only prints the plan)",
    )
    parser.add_argument(
        "--purge-free-accounts",
        action="store_true",
        help="also delete the free-tier accounts in _sample (kept by default)",
    )
    parser.add_argument(
        "--keep-library",
        action="store_true",
        help="keep every master library document (default keeps only the sample pack)",
    )
    parser.add_argument(
        "--purge-library",
        action="store_true",
        help="remove every master library document, including the sample pack",
    )
    parser.add_argument(
        "--purge-sessions",
        action="store_true",
        help="also delete the stored chat sessions",
    )
    return reset(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
