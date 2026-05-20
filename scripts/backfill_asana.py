#!/usr/bin/env python3
"""One-shot 180-day Asana backfill into the Knowledge Base.

Walks all active projects in the HJR Global workspace (gid 682743441507584), yields
Documents for project descriptions + tasks modified within the window, batches them
into the KB's upsert pipeline (which handles chunking + embedding).

Usage:
    cd C:\\Users\\Harri\\code\\cora
    uv run python scripts/backfill_asana.py
    uv run python scripts/backfill_asana.py --days 30 --dry-run
    uv run python scripts/backfill_asana.py --days 180 --batch-size 50

The connector applies the PHI guardrail (excludes projects with client/clinical/PHI
keywords in their names) and entity-classifies via project prefix.
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cora.connectors.asana_connector import backfill  # noqa: E402
from cora.knowledge_base import KnowledgeBase, KnowledgeBaseError  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("backfill_asana")

CORA_REPO_ROOT = Path(__file__).resolve().parents[1]
KB_DB_PATH = CORA_REPO_ROOT / "data" / "cora_kb.db"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=180, help="Look-back window in days (default 180)")
    parser.add_argument("--dry-run", action="store_true", help="Walk + count, don't ingest")
    parser.add_argument("--batch-size", type=int, default=50, help="Documents per KB upsert batch")
    parser.add_argument("--limit", type=int, default=0, help="Cap total Documents yielded (0 = no cap)")
    args = parser.parse_args()

    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    log.info("Asana backfill: looking back %d days (since %s UTC)", args.days, since.isoformat())

    if args.dry_run:
        log.info("DRY RUN — walking but not ingesting")
        count = 0
        by_entity: dict[str, int] = {}
        by_source_type: dict[str, int] = {"project_description": 0, "task": 0}
        t0 = time.time()
        for doc in backfill(since=since):
            count += 1
            by_entity[doc.entity] = by_entity.get(doc.entity, 0) + 1
            if (doc.metadata or {}).get("type") == "project_description":
                by_source_type["project_description"] += 1
            else:
                by_source_type["task"] += 1
            if args.limit and count >= args.limit:
                break
        elapsed = time.time() - t0
        log.info("Dry-run done in %.1fs — would ingest %d documents", elapsed, count)
        log.info("By entity: %s", by_entity)
        log.info("By source type: %s", by_source_type)
        return 0

    # Real ingest path
    kb = KnowledgeBase(KB_DB_PATH)
    total_docs = 0
    total_chunks = 0
    t0 = time.time()
    try:
        batch: list = []
        for doc in backfill(since=since):
            batch.append(doc)
            if len(batch) >= args.batch_size:
                chunks_written = kb.upsert_documents(batch)
                total_docs += len(batch)
                total_chunks += chunks_written
                log.info(
                    "Batch ingested: %d docs → %d chunks (running totals: %d docs / %d chunks)",
                    len(batch), chunks_written, total_docs, total_chunks,
                )
                batch = []
                if args.limit and total_docs >= args.limit:
                    break
        if batch:
            chunks_written = kb.upsert_documents(batch)
            total_docs += len(batch)
            total_chunks += chunks_written
            log.info("Final batch: %d docs → %d chunks", len(batch), chunks_written)
    except KnowledgeBaseError as exc:
        log.error("KB upsert failed: %s", exc)
        return 1
    finally:
        elapsed = time.time() - t0
        log.info(
            "Asana backfill complete in %.1fs — %d documents → %d chunks",
            elapsed, total_docs, total_chunks,
        )
        log.info("KB stats now: %s", kb.stats())
        kb.close()

    # Record sync watermark
    kb = KnowledgeBase(KB_DB_PATH)
    try:
        kb.set_sync_state("asana", int(time.time()))
        log.info("Updated sync_state.asana watermark to now")
    finally:
        kb.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
