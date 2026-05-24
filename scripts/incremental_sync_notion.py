#!/usr/bin/env python3
"""Daily incremental Notion sync — pulls Contracts & Renewals Registry entries
edited since the last sync watermark.

Reads sync_state.notion watermark, calls notion_connector.sync_delta(), upserts
returned Documents. Idempotent via replace-on-conflict by source_id.

Use --backfill to do a full re-index regardless of watermark (safe to run any time).

Scheduled run pattern (Windows Task Scheduler):
    cowork-cora-kb-sync-notion fires daily at 5:00am AZ
    (after drive sync at 4:30am)
    Output redirected to logs/kb-sync-notion-YYYY-MM-DD.log

Exit codes:
    0 = success (sync ran cleanly, watermark advanced)
    1 = fatal error (no documents ingested, watermark unchanged)
    2 = partial — connector error or transient Notion API issue
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cora.connectors.notion_connector import (  # noqa: E402
    NotionConnectorError,
    backfill,
    sync_delta,
)
from cora.knowledge_base import KnowledgeBase, KnowledgeBaseError  # noqa: E402

CORA_REPO_ROOT = Path(__file__).resolve().parents[1]
KB_DB_PATH = CORA_REPO_ROOT / "data" / "cora_kb.db"
LOG_DIR = CORA_REPO_ROOT / "logs"


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = LOG_DIR / f"kb-sync-notion-{today}.log"
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
    parser.add_argument(
        "--backfill", action="store_true",
        help="Full re-index of all Notion pages regardless of watermark",
    )
    args = parser.parse_args()

    _setup_logging()
    log = logging.getLogger("kb-sync-notion")
    log.info("=" * 60)
    log.info("Notion incremental sync starting (backfill=%s)", args.backfill)

    kb = KnowledgeBase(KB_DB_PATH)

    if args.backfill:
        log.info("--backfill requested: walking all Notion pages")
        doc_iterator = backfill()
        sync_start = int(time.time())
        last_sync_ts = None
    else:
        state = kb.get_sync_state("notion")
        if state is None:
            last_sync_ts = int(time.time()) - (args.fallback_days * 86400)
            log.warning(
                "No watermark in sync_state.notion — falling back to last %d days (since %s)",
                args.fallback_days,
                datetime.fromtimestamp(last_sync_ts, tz=timezone.utc).isoformat(),
            )
        else:
            last_sync_ts = state[0]
            log.info(
                "Resuming from watermark: last_sync_at=%s",
                datetime.fromtimestamp(last_sync_ts, tz=timezone.utc).isoformat(),
            )

        sync_start = int(time.time())
        doc_iterator = sync_delta(last_sync_ts)

    total_docs = 0
    total_chunks = 0
    t0 = time.time()
    exit_code = 0

    try:
        batch: list = []
        for doc in doc_iterator:
            batch.append(doc)
            if len(batch) >= args.batch_size:
                total_chunks += kb.upsert_documents(batch)
                total_docs += len(batch)
                log.info(
                    "Batch upserted: %d docs (running: %d / %d chunks)",
                    len(batch), total_docs, total_chunks,
                )
                batch = []
        if batch:
            total_chunks += kb.upsert_documents(batch)
            total_docs += len(batch)
            log.info("Final batch: %d docs", len(batch))
    except NotionConnectorError as exc:
        log.error("Notion sync failed: %s", exc)
        exit_code = 2
    except KnowledgeBaseError as exc:
        log.error("KB upsert failed: %s", exc)
        exit_code = 1
    finally:
        elapsed = time.time() - t0
        log.info(
            "Notion sync complete in %.1fs — %d docs → %d chunks (exit=%d)",
            elapsed, total_docs, total_chunks, exit_code,
        )

    if exit_code == 0:
        # Advance watermark only on clean run
        kb.set_sync_state("notion", sync_start, last_source_modified=sync_start)
        log.info(
            "Watermark advanced to %s",
            datetime.fromtimestamp(sync_start, tz=timezone.utc).isoformat(),
        )

    kb.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
