#!/usr/bin/env python3
"""Daily incremental Fireflies sync — pulls transcripts since last sync.

Same shape as incremental_sync_asana.py but for Fireflies. Scheduled to fire at
3:30am AZ daily (30 min after Asana — small offset avoids both heavy embedding
jobs piling up on OpenAI API at once).
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

from cora.connectors.fireflies_connector import (  # noqa: E402
    FirefliesConnectorError,
    sync_delta,
)
from cora.knowledge_base import KnowledgeBase, KnowledgeBaseError  # noqa: E402

CORA_REPO_ROOT = Path(__file__).resolve().parents[1]
KB_DB_PATH = CORA_REPO_ROOT / "data" / "cora_kb.db"
LOG_DIR = CORA_REPO_ROOT / "logs"


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = LOG_DIR / f"kb-sync-fireflies-{today}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def _resolve_last_sync(state, since_days, fallback_days, now_ts: int) -> int:
    """Decide the 'since' timestamp for this run.

    --since-days overrides everything (forced deep backfill, ignores the watermark);
    else resume from the persisted watermark; else fall back N days.
    """
    if since_days:
        return now_ts - since_days * 86400
    if state is not None:
        return state[0]
    return now_ts - fallback_days * 86400


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fallback-days", type=int, default=2,
        help="Days to look back if no watermark exists (default 2)",
    )
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument(
        "--since-days", type=int, default=None,
        help="Force a deep backfill reaching back N days, ignoring the watermark "
             "(e.g. 2000 ~= all history the API key can access). Watermark still "
             "advances to now afterward, so nightly incremental resumes normally.",
    )
    args = parser.parse_args()

    _setup_logging()
    log = logging.getLogger("kb-sync-fireflies")
    log.info("=" * 60)
    log.info("Fireflies incremental sync starting")

    kb = KnowledgeBase(KB_DB_PATH)
    state = kb.get_sync_state("fireflies")
    now_ts = int(time.time())
    last_sync_ts = _resolve_last_sync(state, args.since_days, args.fallback_days, now_ts)

    if args.since_days:
        log.warning(
            "FORCED deep backfill: reaching back %d days (ignoring watermark), since=%s",
            args.since_days,
            datetime.fromtimestamp(last_sync_ts, tz=timezone.utc).isoformat(),
        )
    elif state is None:
        log.warning(
            "No watermark in sync_state.fireflies — falling back to last %d days",
            args.fallback_days,
        )
    else:
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
    except FirefliesConnectorError as exc:
        log.error("Fireflies sync failed: %s", exc)
        exit_code = 2
    except KnowledgeBaseError as exc:
        log.error("KB upsert failed: %s", exc)
        exit_code = 1
    finally:
        elapsed = time.time() - t0
        log.info(
            "Fireflies sync complete in %.1fs — %d transcripts → %d chunks (exit=%d)",
            elapsed, total_docs, total_chunks, exit_code,
        )

    if exit_code == 0:
        kb.set_sync_state("fireflies", sync_start, last_source_modified=sync_start)
        log.info("Watermark advanced to %s",
                 datetime.fromtimestamp(sync_start, tz=timezone.utc).isoformat())

    kb.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
