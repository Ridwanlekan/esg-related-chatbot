import os
import sqlite3

import numpy as np
import sqlite_vec

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_DB = os.path.join(PROJECT_ROOT, ".index", "vectors.sqlite3")


def _open_conn(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    return conn


def show_summary(conn):
    print("=== INDEX SUMMARY ===")
    total = conn.execute("SELECT COUNT(*) AS c FROM chunks").fetchone()["c"]
    print(f"total chunks: {total}")
    row = conn.execute(
        "SELECT rowid, id, source, chunk_index, content, doc_hash FROM chunks LIMIT 1"
    ).fetchone()
    if row:
        print("example row fields:", list(row.keys()))
    print()
    print("=== CHUNKS PER SOURCE ===")
    for r in conn.execute(
        "SELECT source, COUNT(*) AS n FROM chunks GROUP BY source ORDER BY n DESC"
    ).fetchall():
        print(f"  {r['source']}: {r['n']}")
    print()


def show_chunks(conn, source=None, limit=50):
    where = "WHERE source = ?" if source else ""
    params = (source,) if source else ()
    sql = (
        f"SELECT id, source, chunk_index, doc_hash, substr(content, 1, 120) AS preview "
        f"FROM chunks {where} ORDER BY source, chunk_index LIMIT ?"
    )
    print(f"=== CHUNK ROWS (source={source or 'all'}, limit={limit}) ===")
    rows = conn.execute(sql, params + (limit,)).fetchall()
    if not rows:
        print("  (none)")
        return
    print(f"  {'id':<24} {'source':<18} {'idx':>3}  content preview")
    for r in rows:
        print(f"  {r['id'][:22]:<24} {r['source']:<18} {r['chunk_index']:>3}  {r['preview']!r}")
    print()


def show_full_chunk(conn, chunk_id=None, source=None, chunk_index=0):
    if chunk_id:
        rows = conn.execute(
            "SELECT id, source, chunk_index, doc_hash, content FROM chunks WHERE id = ?",
            (chunk_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, source, chunk_index, doc_hash, content FROM chunks "
            "WHERE source = ? AND chunk_index = ?",
            (source, chunk_index),
        ).fetchall()
    for r in rows:
        print(f"=== FULL CHUNK [{r['source']} #{r['chunk_index']}] ===")
        print(f"id: {r['id']}")
        print(f"doc_hash: {r['doc_hash']}")
        print(f"content:\n{r['content']}")


def show_search(db_path, query, k=3):
    from chatbot.rag import RAGBot

    bot = RAGBot(store_path=db_path)
    results = bot.store.search(bot._embed([query])[0], k=k)
    print(f"=== TOP {k} RESULTS FOR QUERY: {query!r} ===")
    if not results:
        print("  (no results)")
        return
    for rank, r in enumerate(results, 1):
        print(f"\n[{rank}] similarity={r.similarity:.4f} | distance={r.distance:.4f}")
        print(f"    source: {r.source} (chunk #{r.chunk_index})")
        print(f"    id: {r.chunk_id}")
        print(f"    {r.content[:200]}...")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Inspect the RAG SQLite vector index")
    parser.add_argument("--db", default=DEFAULT_DB, help="path to the index db (default: default project index)")
    parser.add_argument("--source", help="filter rows to one source file")
    parser.add_argument("--limit", type=int, default=50, help="max chunk rows to print")
    parser.add_argument("--full", metavar="CHUNK_ID", help="print a full chunk by id")
    parser.add_argument("--search", metavar="QUERY", help="semantic search (loads the embedding model)")
    parser.add_argument("--k", type=int, default=3, help="top-k for --search")
    args = parser.parse_args()

    if args.search:
        show_search(args.db, args.search, k=args.k)
        return

    conn = _open_conn(args.db)
    show_summary(conn)
    if args.full:
        show_full_chunk(conn, chunk_id=args.full)
    else:
        show_chunks(conn, source=args.source, limit=args.limit)
    conn.close()


if __name__ == "__main__":
    main()