#!/usr/bin/env python3
"""One-shot 180-day Fireflies backfill into the Knowledge Base.

Walks all Fireflies transcripts within the window, yields Documents (one per
transcript, combining summary + action items + full transcript text), batches
into the KB's upsert pipeline.

Usage:
    cd C:\\Users\\Harri\\code\\cora
    uv run python scripts/backfill_fireflies.py
    uv run python scripts/backfill_fireflies.py --days 30 --dry-run
    uv run python scripts/backfill_fireflies.py --days 180 --batch-size 20

Entity classification by title keyword. PHI guardrail skips clinical LEX meetings.
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

from cora.connectors.fireflies_connector import backfill  # noqa: E402
from cora.knowledge_base import KnowledgeBase, KnowledgeBaseError  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("backfill_fireflies")

CORA_REPO_ROOT = Path(__file__).resolve().parents[1]
KB_DB_PATH = CORA_REPO_ROOT / "data" / "cora_kb.db"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=180, help="Look-back window in days (default 180)")
    parser.add_argument("--dry-run", action="store_true", help="Walk + count, don't ingest")
    parser.add_argument("--batch-size", type=int, default=20, help="Docs per KB upsert batch")
    parser.add_argument("--limit", type=int, default=0, help="Cap total transcripts (0 = no cap)")
    args = parser.parse_args()

    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    log.info("Fireflies backfill: looking back %d days (since %s UTC)", args.days, since.isoformat())

    if args.dry_run:
        log.info("DRY RUN — walking but not ingesting")
        count = 0
        by_entity: dict[str, int] = {}
        t0 = time.time()
        for doc in backfill(since=since):
            count += 1
            by_entity[doc.entity] = by_entity.get(doc.entity, 0) + 1
            if count <= 10:
                log.info("  example: [%s] %s", doc.entity, doc.title)
            if args.limit and count >= args.limit:
                break
        log.info("Dry-run done in %.1fs — would ingest %d transcripts", time.time() - t0, count)
        log.info("By entity: %s", by_entity)
        return 0

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
                    "Batch ingested: %d docs → %d chunks (totals: %d / %d)",
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
            "Fireflies backfill complete in %.1fs — %d transcripts → %d chunks",
            elapsed, total_docs, total_chunks,
        )
        log.info("KB stats now: %s", kb.stats())
        kb.close()

    # Watermark
    kb = KnowledgeBase(KB_DB_PATH)
    try:
        kb.set_sync_state("fireflies", int(time.time()))
        log.info("Updated sync_state.fireflies watermark to now")
    finally:
        kb.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
