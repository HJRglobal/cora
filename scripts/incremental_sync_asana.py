#!/usr/bin/env python3
"""Daily incremental Asana sync — pulls tasks/projects modified since last sync.

Reads sync_state.asana watermark, calls asana_connector.sync_delta(), upserts
returned Documents. Idempotent via replace-on-conflict by source_id.

Scheduled run pattern (Windows Task Scheduler):
    cowork-cora-kb-sync-asana fires daily at 3:00am AZ
    Output redirected to logs/kb-sync-asana-YYYY-MM-DD.log

Exit codes:
    0 = success (sync ran cleanly, watermark advanced)
    1 = fatal error (no documents ingested, watermark unchanged)
    2 = partial — embeddings disabled or transient connector error
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

from cora.connectors.asana_connector import (  # noqa: E402
    AsanaConnectorError,
    sync_delta,
)
from cora.knowledge_base import KnowledgeBase, KnowledgeBaseError  # noqa: E402

CORA_REPO_ROOT = Path(__file__).resolve().parents[1]
KB_DB_PATH = CORA_REPO_ROOT / "data" / "cora_kb.db"
LOG_DIR = CORA_REPO_ROOT / "logs"


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = LOG_DIR / f"kb-sync-asana-{today}.log"
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
        help="Days to look back if no watermark exists (default 2 — slight overlap with backfill window)",
    )
    parser.add_argument("--batch-size", type=int, default=50)
    args = parser.parse_args()

    _setup_logging()
    log = logging.getLogger("kb-sync-asana")
    log.info("=" * 60)
    log.info("Asana incremental sync starting")

    kb = KnowledgeBase(KB_DB_PATH)
    state = kb.get_sync_state("asana")

    if state is None:
        # No prior watermark — fall back to last N days
        last_sync_ts = int(time.time()) - (args.fallback_days * 86400)
        log.warning(
            "No watermark in sync_state.asana — falling back to last %d days (since %s)",
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
    total_docs = 0
    total_chunks = 0
    t0 = time.time()
    exit_code = 0

    try:
        batch: list = []
        for doc in sync_delta(last_sync_ts):
            batch.append(doc)
            if len(batch) >= args.batch_size:
                total_chunks += kb.upsert_documents(batch)
                total_docs += len(batch)
                log.info("Batch upserted: %d docs (running: %d / %d chunks)",
                         len(batch), total_docs, total_chunks)
                batch = []
        if batch:
            total_chunks += kb.upsert_documents(batch)
            total_docs += len(batch)
            log.info("Final batch: %d docs", len(batch))
    except AsanaConnectorError as exc:
        log.error("Asana sync failed: %s", exc)
        exit_code = 2
    except KnowledgeBaseError as exc:
        log.error("KB upsert failed: %s", exc)
        exit_code = 1
    finally:
        elapsed = time.time() - t0
        log.info(
            "Asana sync complete in %.1fs — %d docs → %d chunks (exit=%d)",
            elapsed, total_docs, total_chunks, exit_code,
        )

    if exit_code == 0:
        # Advance watermark only on clean run
        kb.set_sync_state("asana", sync_start, last_source_modified=sync_start)
        log.info("Watermark advanced to %s", datetime.fromtimestamp(sync_start, tz=timezone.utc).isoformat())

    kb.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
