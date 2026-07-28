"""One-time backfill: normalize stray entity codes in the live KB (Slice 2-2).

211 chunks on 2026-07-28 carry a sub-entity / franchise code as their top-level
``entity`` (LEX-LLC 120, OSNG{F,M,W}/OSNVV 15 each, HJRP-LCI 14, LEX-LLA 9, HJRP-1337 4,
HJRP-1555 3, F3 1). Retrieval filters on the canonical parent codes, so these are DARK
to their parent/aggregate views (and, for LEX-*, dark even to their own sub-entity
channel, which searches under the "LEX" parent). This script rewrites those rows to the
canonical entity via ``entity_normalize.normalize_entity`` -- LEX-* moves into
sub_entity (preserving the strict LEX scoping); OSN franchise / HJRP property codes
collapse onto the parent; F3 -> F3E.

Two stores are updated per chunk:
  * knowledge_chunks.entity (+ sub_entity)     -- the authoritative re-rank filter
  * knowledge_vec_bin (entity metadata column) -- the coarse pre-filter; rebuilt by
    DELETE + re-INSERT from the exact float vector in knowledge_vec_f32 (NO re-embedding;
    vec_quantize_binary of the same f32 blob reproduces the identical binary vector).

Run BEFORE scripts/migrate_kb_partition_key.py so the partitioned index (v2) is born
with canonical entity partitions.

WAL-safe to run with Cora UP (the cutover sequences it before the stop): ~211 tiny
writes; the live reader keeps its snapshot and sees the corrected rows on its next read.
Idempotent: a re-run finds 0 strays. DRY-RUN BY DEFAULT -- pass --apply to write.

Usage (host, repo root):
    .venv\\Scripts\\python.exe scripts\\backfill_entity_normalization.py            # dry-run
    .venv\\Scripts\\python.exe scripts\\backfill_entity_normalization.py --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from cora.knowledge_base import schema  # noqa: E402
from cora.knowledge_base.entity_normalize import CANONICAL, normalize_entity  # noqa: E402

KB_DB_PATH = REPO_ROOT / "data" / "cora_kb.db"


# user_note is EXCLUDED everywhere: notes scope on the raw channel entity verbatim via
# search_user_notes (LEX-LLC is a first-class note scope, not sub_entity), so folding
# them onto the parent would break note containment. Mirrors the upsert Step-0e skip.
_USER_NOTE = "user_note"


def _stray_entities(conn) -> list[tuple[str, int]]:
    """(entity, count) for every non-canonical entity code (user_note excluded)."""
    rows = conn.execute(
        "SELECT entity, COUNT(*) FROM knowledge_chunks WHERE source != ? "
        "GROUP BY entity ORDER BY 2 DESC",
        (_USER_NOTE,),
    ).fetchall()
    return [(e, n) for e, n in rows if (e or "").strip().upper() not in CANONICAL]


_BIN_TABLES = ("knowledge_vec_bin", "knowledge_vec_bin_v2")


def _existing_bin_tables(conn) -> list[str]:
    """Every binary coarse-index table that exists. Rebuilding ALL of them (not just the
    legacy one) makes the backfill ORDER-INDEPENDENT: if the partition migration was run
    BEFORE this backfill (operator error), v2's stale franchise/sub-entity partitions get
    corrected here too, so those chunks aren't dark on the armed v2 coarse scan."""
    return [t for t in _BIN_TABLES
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (t,)).fetchone()]


def main() -> int:
    ap = argparse.ArgumentParser(description="Normalize stray KB entity codes.")
    ap.add_argument("--db", type=Path, default=KB_DB_PATH)
    ap.add_argument("--batch-size", type=int, default=500)
    ap.add_argument("--apply", action="store_true",
                    help="Actually write (default: dry-run report only).")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"ERROR: KB db not found at {args.db}", file=sys.stderr)
        return 2

    conn = schema.connect(args.db)   # loads sqlite-vec (vec_quantize_binary)
    bin_tables = _existing_bin_tables(conn)

    strays = _stray_entities(conn)
    if not strays:
        print("No stray entity codes -- nothing to normalize (already canonical).")
        conn.close()
        return 0

    print("Stray entity codes found:")
    plan: dict[str, tuple[str, str]] = {}   # stray_entity -> (new_entity, note)
    total = 0
    for ent, n in strays:
        new_ent, _ = normalize_entity(ent, None)
        total += n
        if new_ent == ent:
            print(f"  {ent:14} {n:>6}  -> (unrecognized: LEFT AS-IS, fail-open)")
        else:
            print(f"  {ent:14} {n:>6}  -> {new_ent}")
            plan[ent] = (new_ent, "")
    print(f"Total stray chunks: {total:,}  ({len(plan)} code(s) will be remapped)")

    if not args.apply:
        print("\n[dry-run] no changes written. Re-run with --apply.")
        conn.close()
        return 0

    changed = 0
    bin_rebuilt = 0
    for stray_ent, (new_ent, _) in plan.items():
        cur = conn.execute(
            "SELECT chunk_id, sub_entity FROM knowledge_chunks WHERE entity = ? AND source != ?",
            (stray_ent, _USER_NOTE),
        )
        rows = cur.fetchall()
        for i in range(0, len(rows), args.batch_size):
            batch = rows[i:i + args.batch_size]
            for chunk_id, sub_entity in batch:
                new_e, new_sub = normalize_entity(stray_ent, sub_entity)
                conn.execute(
                    "UPDATE knowledge_chunks SET entity = ?, sub_entity = ? WHERE chunk_id = ?",
                    (new_e, new_sub, chunk_id),
                )
                changed += 1
                if bin_tables:
                    frow = conn.execute(
                        "SELECT embedding FROM knowledge_vec_f32 WHERE chunk_id = ?",
                        (chunk_id,),
                    ).fetchone()
                    for bt in bin_tables:
                        conn.execute(f"DELETE FROM {bt} WHERE chunk_id = ?", (chunk_id,))
                        if frow is not None:
                            conn.execute(
                                f"INSERT INTO {bt} (chunk_id, entity, embedding) "
                                "VALUES (?, ?, vec_quantize_binary(?))",
                                (chunk_id, new_e, frow[0]),
                            )
                            bin_rebuilt += 1
            conn.commit()
            print(f"  {stray_ent} -> {new_ent}: {min(i + len(batch), len(rows))}/{len(rows)}")

    # Verify: no strays remain.
    remaining = _stray_entities(conn)
    print(f"\nUpdated {changed:,} chunk(s); rebuilt {bin_rebuilt:,} binary-index row(s).")
    if remaining:
        print("WARNING: stray codes remain (unrecognized, fail-open): "
              + ", ".join(f"{e}({n})" for e, n in remaining), file=sys.stderr)
    else:
        print("OK -- 0 stray entity codes remain.")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
