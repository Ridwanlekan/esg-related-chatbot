#!/usr/bin/env python3
"""Bundle DATA_DIR and INDEX_DIR into one gzip tarball for shipping to a host.

The archive restores directly onto an App Service ``/home``: the data tree
becomes ``esg-data/`` and the index tree becomes ``esg-index/``, matching the
``DATA_DIR=/home/esg-data`` and ``INDEX_DIR=/home/esg-index`` app settings the
deployment uses. One bundle therefore carries the master library, every
organisation's served copies, the account store, the admin store and the chat
history, so a seeded deployment looks just like this machine.

SQLite files are captured with the sqlite3 backup API instead of a plain copy.
A database can have a write-ahead log that is not yet folded into the main file;
copying ``*.sqlite3`` alone would silently drop those rows, so a backup is taken
instead and the ``-wal``/``-shm`` sidecars are not shipped at all.

Typical use:

    ./venv/bin/python scripts/pack-state.py
    RG=esg-chatbot-test-rg ./scripts/azure-push-state.sh
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tarfile
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

DATA_MEMBER = "esg-data"
INDEX_MEMBER = "esg-index"

# Junk that sits beside real data locally but must never travel: the
# pre-organisation scratch index, docling vetting output, stray databases and
# Finder droppings.
DATA_SKIP_NAMES = {".DS_Store", ".index", ".docling_vet"}
DATA_SKIP_SUFFIXES = (".db", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm")
INDEX_SKIP_SUFFIXES = (".sqlite3-wal", ".sqlite3-shm")


def _iter_data(data_dir: Path):
    """Every shippable document under ``data_dir`` as (path, member name)."""
    for path in sorted(data_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(data_dir)
        if any(part in DATA_SKIP_NAMES for part in rel.parts):
            continue
        if path.suffix in DATA_SKIP_SUFFIXES:
            continue
        yield path, f"{DATA_MEMBER}/{rel.as_posix()}"


def _iter_index(index_dir: Path):
    """Every shippable file under ``index_dir`` as (path, member name)."""
    for path in sorted(index_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.name == ".DS_Store" or path.suffix in INDEX_SKIP_SUFFIXES:
            continue
        rel = path.relative_to(index_dir)
        yield path, f"{INDEX_MEMBER}/{rel.as_posix()}"


def _snapshot_sqlite(src: Path, dest: Path) -> None:
    """Write a consistent copy of one SQLite database to ``dest``."""
    source = sqlite3.connect(str(src))
    try:
        target = sqlite3.connect(str(dest))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def pack(data_dir: Path, index_dir: Path, out_path: Path) -> int:
    """Write the bundle; return how many databases were snapshotted."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sqlite_count = 0
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(out_path, "w:gz") as tar:
        tmp_dir = Path(tmp)
        for path, member in _iter_data(data_dir):
            tar.add(path, arcname=member)
        for path, member in _iter_index(index_dir):
            if path.suffix == ".sqlite3":
                snap = tmp_dir / f"{sqlite_count}.sqlite3"
                try:
                    _snapshot_sqlite(path, snap)
                except sqlite3.Error:
                    # Not a database we can read (empty or foreign): copy as-is.
                    tar.add(path, arcname=member)
                    continue
                tar.add(snap, arcname=member)
                sqlite_count += 1
            else:
                tar.add(path, arcname=member)
    return sqlite_count


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("DATA_DIR", str(REPO_ROOT / "data")),
        help="documents tree (default: DATA_DIR or ./data)",
    )
    parser.add_argument(
        "--index-dir",
        default=os.environ.get("INDEX_DIR", str(REPO_ROOT / ".index")),
        help="index/account/session tree (default: INDEX_DIR or ./.index)",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="bundle path (default: dist/esg-state-<UTC timestamp>.tar.gz)",
    )
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir).expanduser().resolve()
    index_dir = Path(args.index_dir).expanduser().resolve()
    if args.out:
        out_path = Path(args.out).expanduser().resolve()
    else:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        out_path = REPO_ROOT / "dist" / f"esg-state-{stamp}.tar.gz"

    if not data_dir.is_dir() and not index_dir.is_dir():
        print(
            f"Nothing to pack: no DATA_DIR ({data_dir}) and no INDEX_DIR "
            f"({index_dir}).",
            file=sys.stderr,
        )
        return 1

    sqlite_count = pack(data_dir, index_dir, out_path)
    size = out_path.stat().st_size
    print(f"Packed {out_path}")
    print(f"  data dir:         {data_dir}")
    print(f"  index dir:        {index_dir}")
    print(f"  sqlite snapshots: {sqlite_count}")
    print(f"  size:             {size / 1024 / 1024:.1f} MiB")
    print()
    print("Ship it with:")
    print(f"  RG=<resource-group> ./scripts/azure-push-state.sh --bundle {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
