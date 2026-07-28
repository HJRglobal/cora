"""Build the partitioned binary KB index knowledge_vec_bin_v2 (Slice 2-1, 2026-07-28).

The coarse hamming scan filters candidates by entity. Today `entity` is a vec0 METADATA
column (knowledge_vec_bin); the scan touches the whole ~680K-row index every query. This
migration builds a side-by-side v2 table with `entity` as a vec0 PARTITION KEY, so the
`entity IN (...)` filter PRUNES to the named partitions (probed on 0.1.9: pruning works,
and `IN` returns a per-partition-k SUPERSET of the metadata path's candidates -> recall is
>= the legacy path, and the exact f32 re-rank still yields the true top-k).

Populates v2 from the exact float vectors in knowledge_vec_f32 (NO re-embedding;
vec_quantize_binary of the same blob reproduces the identical binary vector) + the
canonical entity from knowledge_chunks. Run scripts/backfill_entity_normalization.py FIRST
so v2 is born with canonical entity partitions.

+==============================================================================+
|  CUTOVER (the RENAME "--swap" in the plan is NOT used -- vec0 shadow tables    |
|  cannot be ALTER TABLE RENAME'd: probed 2026-07-28 -> "no such table          |
|  ..._rowids". Activation is a CHECKPOINT FLIP instead; the legacy table is     |
|  retained as a live fallback and dropped only by --drop-legacy.)               |
|                                                                                |
|   1. (Cora UP) backfill_entity_normalization.py --apply                        |
|   2. (Cora UP) this script (default): create + populate v2. The live bot       |
|      dual-writes new chunks to v2 as it exists -> no drift.                     |
|   3. this script --swap: verify v2==f32 counts, then ARM kb_bin_partition_ready|
|   4. restart Cora: the fresh instance's coarse scan now reads v2 (partitioned).|
|   5. (after N stable days, Cora STOPPED) this script --drop-legacy: DROP the   |
|      old knowledge_vec_bin.                                                     |
|  Rollback: --unarm (clears the checkpoint) + restart -> coarse scan falls back |
|  to the legacy table. Rebuild v2 anytime from f32 (no re-embedding).           |
+==============================================================================+

Idempotent + resumable: a sequential scan of f32, skipping chunk_ids already in v2.

Usage (host, repo root):
    .venv\\Scripts\\python.exe scripts\\migrate_kb_partition_key.py --dry-run
    .venv\\Scripts\\python.exe scripts\\migrate_kb_partition_key.py            # populate
    .venv\\Scripts\\python.exe scripts\\migrate_kb_partition_key.py --swap       # arm
    .venv\\Scripts\\python.exe scripts\\migrate_kb_partition_key.py --drop-legacy
    .venv\\Scripts\\python.exe scripts\\migrate_kb_partition_key.py --unarm      # rollback
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

_READY_CKPT = "kb_bin_partition_ready"
_V2 = "knowledge_vec_bin_v2"
_LEGACY = "knowledge_vec_bin"


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
    try:
        return (time.time() - HEARTBEAT_PATH.stat().st_mtime) < max_age_s
    except OSError:
        return False


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = ?", (name,)
    ).fetchone() is not None


def _create_v2(conn) -> None:
    conn.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS {_V2} USING vec0(
            chunk_id TEXT PRIMARY KEY,
            entity TEXT PARTITION KEY,
            embedding bit[{schema.EMBEDDING_DIM}]
        )
        """
    )
    conn.commit()


def _counts(conn) -> tuple[int, int]:
    f32 = conn.execute("SELECT COUNT(*) FROM knowledge_vec_f32").fetchone()[0]
    v2 = conn.execute(f"SELECT COUNT(*) FROM {_V2}").fetchone()[0] if _table_exists(conn, _V2) else 0
    return f32, v2


def main() -> int:
    ap = argparse.ArgumentParser(description="Build/arm the partitioned KB binary index.")
    ap.add_argument("--db", type=Path, default=KB_DB_PATH)
    ap.add_argument("--batch-size", type=int, default=2000)
    ap.add_argument("--dry-run", action="store_true", help="Report only; write nothing.")
    ap.add_argument("--force", action="store_true", help="Skip the heartbeat safety guard.")
    ap.add_argument("--swap", action="store_true",
                    help="Verify v2==f32 counts then ARM kb_bin_partition_ready (activation).")
    ap.add_argument("--drop-legacy", action="store_true",
                    help="DROP the legacy knowledge_vec_bin (only when armed; Cora STOPPED).")
    ap.add_argument("--unarm", action="store_true",
                    help="Clear kb_bin_partition_ready (rollback to the legacy table).")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"ERROR: KB db not found at {args.db}", file=sys.stderr)
        return 2

    conn = schema.connect(args.db)     # loads sqlite-vec (vec_quantize_binary)

    # ── rollback ────────────────────────────────────────────────────────────────
    if args.unarm:
        conn.execute("DELETE FROM checkpoint_state WHERE key = ?", (_READY_CKPT,))
        conn.commit()
        print("UNARMED: kb_bin_partition_ready cleared. Restart Cora -> coarse scan falls "
              "back to the legacy knowledge_vec_bin.")
        conn.close()
        return 0

    # ── drop legacy ──────────────────────────────────────────────────────────────
    if args.drop_legacy:
        cp = conn.execute("SELECT value_json FROM checkpoint_state WHERE key = ?",
                          (_READY_CKPT,)).fetchone()
        armed = bool(cp and json.loads(cp[0]).get("ready"))
        if not armed:
            print("REFUSING: partition index is not armed (kb_bin_partition_ready unset). "
                  "Run --swap first.", file=sys.stderr)
            conn.close()
            return 3
        if _heartbeat_is_fresh() and not args.force:
            print("ERROR: Cora heartbeat is fresh -- stop Cora before --drop-legacy (the "
                  "live connection may hold the table). Pass --force to override.",
                  file=sys.stderr)
            conn.close()
            return 3
        if not _table_exists(conn, _LEGACY):
            print("legacy knowledge_vec_bin already absent -- nothing to drop.")
            conn.close()
            return 0
        conn.execute(f"DROP TABLE {_LEGACY}")
        conn.commit()
        print("DROPPED legacy knowledge_vec_bin. Reclaim disk with VACUUM if desired.")
        conn.close()
        return 0

    # ── swap / arm ────────────────────────────────────────────────────────────────
    if args.swap:
        f32, v2 = _counts(conn)
        print(f"knowledge_vec_f32: {f32:,}   {_V2}: {v2:,}")
        if not _table_exists(conn, _V2):
            print("REFUSING: v2 does not exist -- run the populate step first.", file=sys.stderr)
            conn.close()
            return 3
        if v2 != f32 or f32 == 0:
            print(f"REFUSING to arm: v2 ({v2:,}) != f32 ({f32:,}). Re-run the populate step "
                  "to catch any drift, then --swap again.", file=sys.stderr)
            conn.close()
            return 1
        if args.dry_run:
            print("[dry-run] counts match; would ARM kb_bin_partition_ready.")
            conn.close()
            return 0
        _set_ckpt(conn, _READY_CKPT, {"ready": True, "count": v2, "armed_at": int(time.time())})
        print(f"ARMED kb_bin_partition_ready ({v2:,} chunks). Restart Cora -> coarse scan "
              "reads the partitioned v2.")
        conn.close()
        return 0

    # ── populate (default) ─────────────────────────────────────────────────────────
    if _heartbeat_is_fresh() and not args.force and not args.dry_run:
        print("NOTE: Cora heartbeat is fresh. Populating v2 is SAFE with Cora up (writes a "
              "new table the bot doesn't yet read; the bot dual-writes v2 too). Proceeding. "
              "(--swap verifies counts before arming; re-run populate if it reports drift.)")

    _create_v2(conn)
    f32_total = conn.execute("SELECT COUNT(*) FROM knowledge_vec_f32").fetchone()[0]
    done = {r[0] for r in conn.execute(f"SELECT chunk_id FROM {_V2}")}
    print(f"knowledge_vec_f32: {f32_total:,}   already in v2: {len(done):,} (will skip)")

    if args.dry_run:
        print(f"rows to migrate: {f32_total - len(done):,}")
        print("[dry-run] no changes written.")
        conn.close()
        return 0

    start, processed = time.time(), 0
    read_conn = schema.connect(args.db)
    cursor = read_conn.execute("SELECT chunk_id, embedding FROM knowledge_vec_f32")
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
            conn.execute(f"DELETE FROM {_V2} WHERE chunk_id IN ({ph})", ids)
            conn.executemany(
                f"INSERT INTO {_V2} (chunk_id, entity, embedding) "
                "VALUES (?, ?, vec_quantize_binary(?))",
                [(c, ent_map.get(c, "FNDR"), e) for c, e in new],
            )
            conn.commit()
            done.update(ids)
            processed += len(new)
            elapsed = time.time() - start
            print(f"  ...{processed:,} migrated ({processed / elapsed if elapsed else 0:,.0f}/s)")
    finally:
        read_conn.close()

    f32, v2 = _counts(conn)
    print(f"\nfinal: f32={f32:,}  {_V2}={v2:,}")
    if v2 == f32 and f32 > 0:
        print("OK -- v2 fully populated. Next: --swap to arm, then restart Cora.")
        rc = 0
    else:
        print("WARNING: v2 != f32 -- re-run to complete before --swap.", file=sys.stderr)
        rc = 1
    conn.close()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
