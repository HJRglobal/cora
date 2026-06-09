"""Phase 2 of game-plan section 10.6: drop the legacy float vec0 table.

The binary fast path (knowledge_vec_bin coarse scan + knowledge_vec_f32 exact
re-rank) fully replaced the original float vec0 index `knowledge_vec` on 6/07.
The store code stopped reading/writing knowledge_vec in commit fccf028, so the
table is now dead weight (~1.4 GB). This drops it and VACUUMs to return the
space to the OS.

SAFETY:
  - Refuses unless the binary index is ready (the fast path must be live).
  - Exits cleanly (0) if knowledge_vec is already gone.
  - VACUUM requires EXCLUSIVE db access -- run with Cora STOPPED.
  - Take a file backup of cora_kb.db BEFORE running (the caller does this).
  - --dry-run reports what it would do and changes nothing.

Run (Cora stopped):
    .venv\\Scripts\\python.exe scripts\\drop_legacy_float_vec.py --dry-run
    .venv\\Scripts\\python.exe scripts\\drop_legacy_float_vec.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from cora.knowledge_base import schema  # noqa: E402

KB_DB_PATH = REPO_ROOT / "data" / "cora_kb.db"


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
        (name,),
    ).fetchone() is not None


def _count(conn, table: str) -> int:
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except Exception:
        return -1


def main() -> int:
    ap = argparse.ArgumentParser(description="Drop the legacy knowledge_vec float table.")
    ap.add_argument("--db", type=Path, default=KB_DB_PATH)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"ERROR: KB db not found at {args.db}", file=sys.stderr)
        return 2

    size_before = args.db.stat().st_size
    conn = schema.connect(args.db)

    # Guard 1: the binary fast path must be live before we remove the fallback's
    # source table. (search() falls back to _search_float over f32 if not ready,
    # which is fine, but we still require a proven fast path before dropping.)
    import json
    ready = False
    try:
        row = conn.execute(
            "SELECT value_json FROM checkpoint_state WHERE key='kb_bin_index_ready'"
        ).fetchone()
        if row and row[0]:
            ready = bool(json.loads(row[0]).get("ready"))
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: could not read kb_bin_index_ready: {exc}", file=sys.stderr)
    if not ready:
        print("ABORT: binary index not marked ready (kb_bin_index_ready). "
              "Run the migration first; refusing to drop the fallback source.",
              file=sys.stderr)
        return 3

    # Guard 2: already gone?
    if not _table_exists(conn, "knowledge_vec"):
        print("knowledge_vec already dropped -- nothing to do.")
        return 0

    vec_n = _count(conn, "knowledge_vec")
    bin_n = _count(conn, "knowledge_vec_bin")
    f32_n = _count(conn, "knowledge_vec_f32")
    chunks_n = _count(conn, "knowledge_chunks")
    print(f"db size before     : {size_before/1e9:.2f} GB")
    print(f"knowledge_vec      : {vec_n:,}  <- drop target")
    print(f"knowledge_vec_bin  : {bin_n:,}  (keep)")
    print(f"knowledge_vec_f32  : {f32_n:,}  (keep)")
    print(f"knowledge_chunks   : {chunks_n:,}")

    # Guard 3: f32 must cover the live chunks (the fallback reads f32). If f32 is
    # materially short of knowledge_chunks, something is wrong -- do not proceed.
    if f32_n < chunks_n:
        print(f"ABORT: knowledge_vec_f32 ({f32_n:,}) has fewer rows than "
              f"knowledge_chunks ({chunks_n:,}) -- f32 coverage incomplete.",
              file=sys.stderr)
        return 4

    if args.dry_run:
        print("\n[dry-run] would: PRAGMA wal_checkpoint(TRUNCATE); "
              "DROP TABLE knowledge_vec; VACUUM;  (nothing changed)")
        return 0

    print("\nCheckpointing WAL...")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    print("Dropping knowledge_vec...")
    conn.execute("DROP TABLE knowledge_vec")
    conn.commit()
    print("VACUUM (rewrites the db file; may take a few minutes)...")
    conn.execute("VACUUM")
    conn.commit()
    conn.close()

    size_after = args.db.stat().st_size
    print(f"\ndb size after      : {size_after/1e9:.2f} GB")
    print(f"reclaimed          : {(size_before - size_after)/1e9:.2f} GB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
