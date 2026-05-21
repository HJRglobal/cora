#!/usr/bin/env python3
"""Daily incremental Drive asset sync.

Walks the HJR-Founder-OS Drive via the Drive API, filters to files with
modifiedTime > sync_state.drive_assets.last_source_modified, re-ingests them
into the KB. Idempotent — replace-on-conflict by source_id (Drive file_id)
means re-ingesting an unchanged file would be a no-op anyway, but the API
filter saves an HTTP call per file.

Scheduled run: 4:30 AM AZ daily, after the static MD sync at 4:00 AM.

Catches:
- New files added to indexable Drive folders within last 24h
- Files renamed (filename changed but file_id stable - shows up as updated content)
- Files moved into indexable folders
- Files whose contents are edited (Drive bumps modifiedTime)
- Trashed files DO NOT auto-cleanup from KB; if a file is deleted, its KB
  entry persists until a future cleanup pass. Acceptable for v1.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cora.connectors.drive_connector import (  # noqa: E402
    DriveConnectorError,
    backfill,
)
from cora.knowledge_base import KnowledgeBase, KnowledgeBaseError  # noqa: E402

CORA_REPO_ROOT = Path(__file__).resolve().parents[1]
KB_DB_PATH = CORA_REPO_ROOT / "data" / "cora_kb.db"
LOG_DIR = CORA_REPO_ROOT / "logs"


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = LOG_DIR / f"kb-sync-drive-{today}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fallback-days", type=int, default=2,
        help="Days to look back if no watermark exists (default 2)",
    )
    parser.add_argument("--batch-size", type=int, default=50)
    args = parser.parse_args()

    _setup_logging()
    log = logging.getLogger("kb-sync-drive")
    log.info("=" * 60)
    log.info("Drive asset incremental sync starting")

    kb = KnowledgeBase(KB_DB_PATH)
    state = kb.get_sync_state("drive_assets")

    if state is None:
        last_sync_ts = int(time.time()) - (args.fallback_days * 86400)
        log.warning(
            "No watermark for drive_assets - falling back to last %d days",
            args.fallback_days,
        )
    else:
        last_sync_ts = state[0]
        log.info(
            "Resuming from watermark: %s",
            datetime.fromtimestamp(last_sync_ts, tz=timezone.utc).isoformat(),
        )

    sync_start = int(time.time())
    total_docs = 0
    total_chunks = 0
    batch: list = []
    exit_code = 0

    try:
        for doc in backfill(modified_after=last_sync_ts):
            batch.append(doc)
            if len(batch) >= args.batch_size:
                total_chunks += kb.upsert_documents(batch)
                total_docs += len(batch)
                log.info(
                    "Batch ingested: %d docs (running: %d / %d chunks)",
                    len(batch), total_docs, total_chunks,
                )
                batch.clear()

        if batch:
            total_chunks += kb.upsert_documents(batch)
            total_docs += len(batch)
            log.info(
                "Final batch ingested: %d docs (total: %d / %d chunks)",
                len(batch), total_docs, total_chunks,
            )
    except DriveConnectorError as exc:
        log.error("Drive walk failed: %s", exc)
        exit_code = 1
    except KnowledgeBaseError as exc:
        log.error("KB upsert failed: %s", exc)
        exit_code = 1
    finally:
        if exit_code == 0:
            kb.set_sync_state(
                "drive_assets", sync_start, last_source_modified=sync_start
            )
            log.info("Watermark advanced to %d", sync_start)
        kb.close()

    if total_docs == 0 and exit_code == 0:
        log.info("No drive files modified since watermark - nothing to ingest")
    log.info(
        "Drive incremental sync complete - %d docs / %d chunks (exit=%d)",
        total_docs, total_chunks, exit_code,
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
