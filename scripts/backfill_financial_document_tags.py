"""One-time backfill: tag existing gmail/drive_sweep KB chunks as
financial_document=true where the deterministic classifier fires.

Ingest-time tagging (store.upsert_documents Step 0b) covers everything from
2026-06-10 forward, including the 18-month gmail backfill. This script
catches up chunks ingested BEFORE that shipped. Idempotent and resumable —
already-tagged chunks are skipped, and re-running after new ingests only
touches untagged rows. No re-embedding; metadata-only UPDATE.

Usage (host, from C:\\Users\\Harri\\code\\cora):
    .venv\\Scripts\\python.exe scripts\\backfill_financial_document_tags.py [--dry-run]

Safe to run alongside the live bot (schema.connect sets busy_timeout, D-039),
but prefer a quiet window for the multi-minute scan.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from cora.finance_doc_classifier import is_financial_document  # noqa: E402
from cora.knowledge_base import schema  # noqa: E402

DB_PATH = REPO_ROOT / "data" / "cora_kb.db"
BATCH = 2000

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill-financial-tags")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not DB_PATH.exists():
        log.error("KB not found at %s", DB_PATH)
        return 1

    conn = schema.connect(DB_PATH)
    cur = conn.cursor()

    scanned = tagged = already = 0
    last_rowid = 0
    t0 = time.time()

    while True:
        rows = cur.execute(
            """
            SELECT rowid, chunk_id, title, content, author, metadata
            FROM knowledge_chunks
            WHERE source IN ('gmail', 'drive_sweep') AND rowid > ?
            ORDER BY rowid
            LIMIT ?
            """,
            (last_rowid, BATCH),
        ).fetchall()
        if not rows:
            break

        updates: list[tuple[str, str]] = []
        for rowid, chunk_id, title, content, author, metadata_raw in rows:
            last_rowid = rowid
            scanned += 1
            try:
                meta = json.loads(metadata_raw) if metadata_raw else {}
            except (ValueError, TypeError):
                meta = {}
            if meta.get("financial_document"):
                already += 1
                continue
            if is_financial_document(title or "", content or "", author or ""):
                meta["financial_document"] = True
                updates.append((json.dumps(meta), chunk_id))

        if updates and not args.dry_run:
            cur.executemany(
                "UPDATE knowledge_chunks SET metadata = ? WHERE chunk_id = ?",
                updates,
            )
            conn.commit()
        tagged += len(updates)
        if scanned % 20000 < BATCH:
            log.info("scanned=%d tagged=%d already=%d (%.0fs)",
                     scanned, tagged, already, time.time() - t0)

    conn.close()
    log.info(
        "DONE%s: scanned=%d newly_tagged=%d already_tagged=%d in %.0fs",
        " (dry-run)" if args.dry_run else "", scanned, tagged, already,
        time.time() - t0,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
