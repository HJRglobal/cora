#!/usr/bin/env python3
"""One-shot Drive asset backfill.

Walks the HJR-Founder-OS Drive root via the Drive API, indexes file metadata
(filename, path, mtime, owner, MIME type, webViewLink) into the KB as
source='drive_asset'.

Indexable MIME types are limited to deliverables (Google Docs/Sheets/Slides,
docx/xlsx/pptx, pdf, common image types). PHI / archive / hidden paths are
skipped per the blacklist in drive_connector.py.

This is METADATA-only indexing. Content extraction from file bodies is Phase 5+.

Usage:
    cd C:\\Users\\Harri\\code\\cora
    uv run python scripts/backfill_drive_assets.py
    uv run python scripts/backfill_drive_assets.py --dry-run
    uv run python scripts/backfill_drive_assets.py --limit 100

Prerequisites:
- GOOGLE_SERVICE_ACCOUNT_JSON in .env (same cred file calendar uses)
- DWD scope `https://www.googleapis.com/auth/drive.readonly` added to the
  service account in Google Workspace admin
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cora.connectors.drive_connector import (  # noqa: E402
    DriveConnectorError,
    backfill,
)
from cora.knowledge_base import KnowledgeBase, KnowledgeBaseError  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("backfill_drive_assets")

CORA_REPO_ROOT = Path(__file__).resolve().parents[1]
KB_DB_PATH = CORA_REPO_ROOT / "data" / "cora_kb.db"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Walk Drive but don't write to KB")
    parser.add_argument("--limit", type=int, default=0,
                        help="Cap ingestion to N files (0 = no cap)")
    parser.add_argument("--batch-size", type=int, default=50,
                        help="Documents per KB upsert batch (default 50)")
    args = parser.parse_args()

    try:
        docs_iter = backfill()
    except DriveConnectorError as exc:
        log.error("Drive connector init failed: %s", exc)
        return 1

    sync_start = int(time.time())

    if args.dry_run:
        by_entity: dict[str, int] = {}
        sample: list[str] = []
        count = 0
        for doc in docs_iter:
            by_entity[doc.entity] = by_entity.get(doc.entity, 0) + 1
            if len(sample) < 30:
                sample.append(f"{doc.entity:8s} | {doc.title} | {doc.metadata.get('mime_type', '')}")
            count += 1
            if args.limit and count >= args.limit:
                break
        log.info("Dry-run summary - %d files would be indexed by entity:", count)
        for ent, n in sorted(by_entity.items()):
            log.info("  %-8s %d", ent, n)
        log.info("Sample (first 30):")
        for s in sample:
            log.info("  %s", s)
        return 0

    kb = KnowledgeBase(KB_DB_PATH)
    total_docs = 0
    total_chunks = 0
    batch: list = []
    exit_code = 0

    try:
        for doc in docs_iter:
            batch.append(doc)
            if len(batch) >= args.batch_size:
                total_chunks += kb.upsert_documents(batch)
                total_docs += len(batch)
                log.info(
                    "Batch ingested: %d docs (running: %d docs / %d chunks)",
                    len(batch), total_docs, total_chunks,
                )
                batch.clear()
            if args.limit and total_docs + len(batch) >= args.limit:
                log.info("Limit reached (%d) - stopping", args.limit)
                break

        # Flush final partial batch
        if batch:
            total_chunks += kb.upsert_documents(batch)
            total_docs += len(batch)
            log.info(
                "Final batch ingested: %d docs (total: %d docs / %d chunks)",
                len(batch), total_docs, total_chunks,
            )
    except KnowledgeBaseError as exc:
        log.error("KB upsert failed: %s", exc)
        exit_code = 1
    except DriveConnectorError as exc:
        log.error("Drive walk failed mid-backfill: %s", exc)
        exit_code = 1
    finally:
        if exit_code == 0:
            kb.set_sync_state(
                "drive_assets", sync_start, last_source_modified=sync_start
            )
            log.info("drive_assets watermark set to %d", sync_start)
        kb.close()

    log.info(
        "Drive asset backfill complete - %d docs indexed / %d chunks (exit=%d)",
        total_docs, total_chunks, exit_code,
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
