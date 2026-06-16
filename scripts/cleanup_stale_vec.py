#!/usr/bin/env python3
"""Remove orphaned vector rows -- embeddings whose knowledge_chunks row is gone.

sqlite-vec's vec0 virtual tables (knowledge_vec_bin, knowledge_vec_f32) do not
enforce referential integrity, so a crash mid-upsert, the binary-index migration,
or a source item that is deleted-and-never-reingested can leave embedding rows
whose chunk_id no longer exists in knowledge_chunks. Orphans waste space and can
surface in searches with no metadata. (Audit F-15: ~38,642 such rows.)

Scope note: the upsert path keeps both vec tables in sync on RE-INGEST
(store.upsert_documents deletes bin+f32+chunks for the re-ingested source_id). A
source item deleted at the source and never re-ingested is NOT pruned by upsert
alone -- this sweep reclaims those. A true source-delete prune (a connector
diffing current vs prior source items) is a separate follow-up; this periodic
sweep is the mitigation.

Usage (--dry-run is safe anytime; stop Cora before --apply):
    python scripts/cleanup_stale_vec.py            # dry-run: report orphan counts
    python scripts/cleanup_stale_vec.py --apply    # delete orphans from both tables

After --apply, reclaim disk with VACUUM INTO (D-035, scripts/reclaim_kb_space.py)
-- a plain in-place VACUUM in WAL mode reports success but does not shrink the file.

Exit codes: 0 ok, 1 fatal error.
"""

import argparse
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cora.knowledge_base import schema  # noqa: E402

CORA_REPO_ROOT = Path(__file__).resolve().parents[1]
KB_DB_PATH = CORA_REPO_ROOT / "data" / "cora_kb.db"

_VEC_TABLES = ("knowledge_vec_bin", "knowledge_vec_f32")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("cleanup-stale-vec")


def find_orphans(conn, vec_table: str) -> list[str]:
    """Return chunk_ids in vec_table that have no matching knowledge_chunks row."""
    rows = conn.execute(
        f"""
        SELECT v.chunk_id
        FROM {vec_table} v
        LEFT JOIN knowledge_chunks k ON k.chunk_id = v.chunk_id
        WHERE k.chunk_id IS NULL
        """
    ).fetchall()
    return [r[0] for r in rows]


def delete_orphans(conn, vec_table: str, chunk_ids: list[str], batch_size: int = 500) -> int:
    """Delete the given chunk_ids from vec_table, in batches. Returns rows deleted.

    Batched to stay under SQLite's bound-variable limit -- the live KB had 38,642
    orphans per table, and a single IN(...) with that many params raises
    "too many SQL variables". vec0 DELETE supports an IN-list of bound params (see
    store.upsert_documents), just not tens of thousands at once.
    """
    total = 0
    for i in range(0, len(chunk_ids), batch_size):
        batch = chunk_ids[i:i + batch_size]
        placeholders = ",".join("?" * len(batch))
        cur = conn.execute(
            f"DELETE FROM {vec_table} WHERE chunk_id IN ({placeholders})",
            batch,
        )
        total += cur.rowcount
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description="Sweep orphaned KB vector rows.")
    parser.add_argument("--apply", action="store_true",
                        help="Delete orphans (default is a dry-run report).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report only (this is the default; kept for compatibility).")
    args = parser.parse_args()
    apply_changes = args.apply and not args.dry_run

    if not KB_DB_PATH.exists():
        log.error("KB database not found: %s", KB_DB_PATH)
        return 1

    log.info("Opening KB database: %s", KB_DB_PATH)
    conn = schema.connect(KB_DB_PATH)
    total = 0
    try:
        for vec_table in _VEC_TABLES:
            try:
                orphans = find_orphans(conn, vec_table)
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not scan %s: %s", vec_table, exc)
                continue
            total += len(orphans)
            log.info("%s: %d orphan(s)", vec_table, len(orphans))
            if orphans and apply_changes:
                deleted = delete_orphans(conn, vec_table, orphans)
                conn.commit()
                log.info("%s: deleted %d orphan(s)", vec_table, deleted)

        if total == 0:
            log.info("No orphaned vec rows found -- KB vec tables are clean.")
        elif not apply_changes:
            log.info("Dry-run: %d total orphan(s) across %d table(s). Re-run with "
                     "--apply (Cora stopped) to delete, then VACUUM INTO.",
                     total, len(_VEC_TABLES))
        else:
            log.info("Deleted orphans. Reclaim disk with VACUUM INTO (reclaim_kb_space.py).")
    except Exception as exc:  # noqa: BLE001
        log.error("cleanup failed: %s", exc, exc_info=True)
        conn.close()
        return 1
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
