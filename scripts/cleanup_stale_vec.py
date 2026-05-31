#!/usr/bin/env python3
"""Remove orphaned rows from knowledge_vec that have no matching knowledge_chunks entry.

sqlite-vec's vec0 virtual table does not enforce referential integrity, so a
crash or early exit during upsert_documents() can leave embedding rows in
knowledge_vec whose chunk_id no longer exists in knowledge_chunks.  These
orphans waste space and can be surfaced in knn searches, returning results with
no metadata.

Usage:
    python scripts/cleanup_stale_vec.py [--dry-run]

Exit codes:
    0  clean (no orphans found, or orphans deleted successfully)
    1  fatal error
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("cleanup-stale-vec")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report orphans but do not delete them",
    )
    args = parser.parse_args()

    if not KB_DB_PATH.exists():
        log.error("KB database not found: %s", KB_DB_PATH)
        return 1

    log.info("Opening KB database: %s", KB_DB_PATH)
    conn = schema.connect(KB_DB_PATH)

    try:
        # Find vec rowids whose chunk_id is not in knowledge_chunks.
        rows = conn.execute(
            """
            SELECT v.chunk_id
            FROM knowledge_vec v
            LEFT JOIN knowledge_chunks k ON k.chunk_id = v.chunk_id
            WHERE k.chunk_id IS NULL
            """
        ).fetchall()

        orphan_ids = [r[0] for r in rows]

        if not orphan_ids:
            log.info("No orphaned vec rows found — knowledge_vec is clean.")
            conn.close()
            return 0

        log.info("Found %d orphaned vec row(s).", len(orphan_ids))
        for oid in orphan_ids:
            log.info("  orphan chunk_id=%s", oid)

        if args.dry_run:
            log.info("Dry-run mode — no rows deleted.")
            conn.close()
            return 0

        placeholders = ",".join("?" * len(orphan_ids))
        cur = conn.execute(
            f"DELETE FROM knowledge_vec WHERE chunk_id IN ({placeholders})",
            orphan_ids,
        )
        conn.commit()
        log.info("Deleted %d orphaned vec row(s).", cur.rowcount)

    except Exception as exc:
        log.error("cleanup failed: %s", exc, exc_info=True)
        conn.close()
        return 1

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
