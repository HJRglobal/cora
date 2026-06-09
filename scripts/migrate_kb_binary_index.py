"""Backfill the binary KB index (knowledge_vec_bin) + exact-float re-rank table
(knowledge_vec_f32) from the existing float vec0 table (knowledge_vec).

Why: the float brute-force KNN scans the whole ~1.4 GB float index on every
query (~31 s cold on the 224K-chunk DB). The fast path scans a 1/32-size binary
index (~43 MB) then re-ranks the top candidates against exact float vectors. This
script populates the two new tables from vectors already stored - NO re-embedding,
no OpenAI calls. Once complete it sets the `kb_bin_index_ready` checkpoint, which
flips store.search() onto the fast path.

+==============================================================================+
|  SAFETY - READ BEFORE RUNNING                                                  |
|  1. STOP CORA FIRST. The service holds an open connection; migrating under a   |
|     live service risks lock contention + a half-warmed shared instance.        |
|  2. BACK UP data/cora_kb.db FIRST (sqlite online-backup or a file copy while   |
|     stopped). This script is additive + idempotent, but back up anyway.        |
|  3. RUN ON THE HOST against the real DB. NEVER run against the live DB from a   |
|     Cowork sandbox over the FUSE/virtiofs mount.                               |
|  The companion host PS1 does stop -> backup -> migrate -> restart -> verify.   |
+==============================================================================+

Idempotent + resumable: a one-time sequential scan of the float table, skipping
chunk_ids already present in the binary index. A re-run (or resume after a crash)
re-scans and skips the completed prefix, and never double-inserts.

Usage (host, elevated, repo root, Cora stopped):
    .venv\\Scripts\\python.exe scripts\\migrate_kb_binary_index.py
    .venv\\Scripts\\python.exe scripts\\migrate_kb_binary_index.py --dry-run
    .venv\\Scripts\\python.exe scripts\\migrate_kb_binary_index.py --batch-size 4000
    .venv\\Scripts\\python.exe scripts\\migrate_kb_binary_index.py --force   # skip heartbeat guard
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from cora.knowledge_base import schema  # noqa: E402

KB_DB_PATH = REPO_ROOT / "data" / "cora_kb.db"
HEARTBEAT_PATH = REPO_ROOT / "data" / "health" / "heartbeat.txt"

_READY_CKPT = "kb_bin_index_ready"       # store.search reads this to enable fast path


def _set_ckpt(conn, key, data):
    conn.execute(
        """INSERT INTO checkpoint_state (key, value_json, updated_at)
           VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET
             value_json = excluded.value_json, updated_at = excluded.updated_at""",
        (key, json.dumps(data), int(time.time())),
    )
    conn.commit()


def _heartbeat_is_fresh(max_age_s: int = 180) -> bool:
    """True if Cora's heartbeat was touched recently -> service probably running."""
    try:
        age = time.time() - HEARTBEAT_PATH.stat().st_mtime
        return age < max_age_s
    except OSError:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill binary KB index + f32 re-rank table.")
    ap.add_argument("--db", type=Path, default=KB_DB_PATH)
    ap.add_argument("--batch-size", type=int, default=2000)
    ap.add_argument("--dry-run", action="store_true",
                    help="Report counts + work remaining; write nothing.")
    ap.add_argument("--force", action="store_true",
                    help="Skip the heartbeat (Cora-running) safety guard.")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"ERROR: KB db not found at {args.db}", file=sys.stderr)
        return 2

    if _heartbeat_is_fresh() and not args.force and not args.dry_run:
        print("ERROR: Cora heartbeat is fresh (<180s) - the service appears to be "
              "running. Stop Cora first, or pass --force if you know it's down.",
              file=sys.stderr)
        return 3

    conn = schema.connect(args.db)        # loads sqlite-vec (vec_quantize_binary)
    schema.init_schema(conn)              # ensure bin + f32 tables exist

    # The legacy float vec0 table this migration reads from was dropped 2026-06-08
    # once the binary fast path was proven. This one-time migration is therefore
    # complete and cannot re-run from knowledge_vec. To rebuild the binary index
    # in the future, source the float vectors from knowledge_vec_f32 instead.
    has_legacy = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name='knowledge_vec'"
    ).fetchone()
    if not has_legacy:
        print("knowledge_vec no longer exists (dropped 2026-06-08) -- migration "
              "already complete. Rebuild bin/f32 from knowledge_vec_f32 if ever needed.",
              file=sys.stderr)
        return 0

    total_vec = conn.execute("SELECT COUNT(*) FROM knowledge_vec").fetchone()[0]
    bin_have = conn.execute("SELECT COUNT(*) FROM knowledge_vec_bin").fetchone()[0]
    f32_have = conn.execute("SELECT COUNT(*) FROM knowledge_vec_f32").fetchone()[0]
    print(f"knowledge_vec rows : {total_vec:,}")
    print(f"knowledge_vec_bin  : {bin_have:,}")
    print(f"knowledge_vec_f32  : {f32_have:,}")

    # Resume set: chunk_ids already backfilled into the binary index. vec0 has
    # no usable rowid for pagination, so we stream the float table in one
    # sequential pass and skip ids that are already done. A resumed run simply
    # re-scans (sequential I/O is cheap) and skips the completed prefix.
    done = {r[0] for r in conn.execute("SELECT chunk_id FROM knowledge_vec_bin")}
    print(f"already backfilled : {len(done):,} (will skip)")

    if args.dry_run:
        remaining = total_vec - len(done)
        print(f"rows to migrate    : {remaining:,}")
        print("[dry-run] no changes written.")
        conn.close()
        return 0

    start = time.time()
    processed = 0

    # Separate read connection streams knowledge_vec; main `conn` does the writes.
    # Under WAL the reader holds a stable snapshot while the writer commits.
    read_conn = schema.connect(args.db)
    cursor = read_conn.execute("SELECT chunk_id, embedding FROM knowledge_vec")
    try:
        while True:
            rows = cursor.fetchmany(args.batch_size)
            if not rows:
                break
            new = [(cid, emb) for (cid, emb) in rows if cid not in done]
            if not new:
                continue
            ids = [c for c, _ in new]
            ph = ",".join("?" * len(ids))
            ent_map = dict(conn.execute(
                f"SELECT chunk_id, entity FROM knowledge_chunks WHERE chunk_id IN ({ph})",
                ids,
            ).fetchall())

            # idempotent: clear any prior rows for these ids, then insert
            conn.execute(f"DELETE FROM knowledge_vec_bin WHERE chunk_id IN ({ph})", ids)
            conn.execute(f"DELETE FROM knowledge_vec_f32 WHERE chunk_id IN ({ph})", ids)
            conn.executemany(
                "INSERT INTO knowledge_vec_f32 (chunk_id, embedding) VALUES (?, ?)",
                new,
            )
            conn.executemany(
                "INSERT INTO knowledge_vec_bin (chunk_id, entity, embedding) "
                "VALUES (?, ?, vec_quantize_binary(?))",
                [(c, ent_map.get(c, "FNDR"), e) for c, e in new],
            )
            conn.commit()
            done.update(ids)
            processed += len(new)
            elapsed = time.time() - start
            rate = processed / elapsed if elapsed else 0
            print(f"  ...{processed:,} migrated ({rate:,.0f}/s)")
    finally:
        read_conn.close()

    # Verify completeness against the source-of-truth float table, then arm the
    # fast path. Compare to knowledge_vec (not knowledge_chunks): a chunk with no
    # float vector is unsearchable on both paths, so it must not block readiness.
    total_vec = conn.execute("SELECT COUNT(*) FROM knowledge_vec").fetchone()[0]
    bin_have = conn.execute("SELECT COUNT(*) FROM knowledge_vec_bin").fetchone()[0]
    f32_have = conn.execute("SELECT COUNT(*) FROM knowledge_vec_f32").fetchone()[0]
    print(f"\nfinal: knowledge_vec={total_vec:,}  bin={bin_have:,}  f32={f32_have:,}")

    if bin_have == f32_have == total_vec and total_vec > 0:
        _set_ckpt(conn, _READY_CKPT, {"ready": True, "count": total_vec,
                                      "migrated_at": int(time.time())})
        print(f"OK - binary index ready ({total_vec:,} chunks). Fast path ARMED.")
        print("Restart Cora to pick up the readiness flag + new code.")
        rc = 0
    else:
        print("WARNING: counts do not match - readiness flag NOT set. Fast path "
              "stays OFF (float fallback). Re-run to complete; investigate if it "
              "persists (orphan chunks / interrupted run).", file=sys.stderr)
        rc = 1

    conn.close()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
