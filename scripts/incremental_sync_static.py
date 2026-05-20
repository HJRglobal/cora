#!/usr/bin/env python3
"""Daily incremental static MD sync — re-ingests Drive files modified since last sync.

Walks the Founder OS markdown tree (same paths as migrate_static_md.py), finds
files with mtime > sync_state.static_md.last_sync_at, upserts them. Idempotent —
replace-on-conflict by source_id means re-ingesting an unchanged file is a no-op.

Scheduled run: 4:00am AZ daily (60 min after Asana, 30 min after Fireflies).

Catches:
    - New CLAUDE.md / decisions.md / project-brief edits within last 24h
    - Brand-new files added to the Founder OS tree
    - Renamed files (will appear as new + old persists; manual cleanup if needed)
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

from cora.knowledge_base import KnowledgeBase, KnowledgeBaseError  # noqa: E402
from cora.knowledge_base.store import Document  # noqa: E402

CORA_REPO_ROOT = Path(__file__).resolve().parents[1]
KB_DB_PATH = CORA_REPO_ROOT / "data" / "cora_kb.db"
LOG_DIR = CORA_REPO_ROOT / "logs"

FOUNDER_OS_ROOT = Path(r"G:\My Drive\HJR-Founder-OS")

ENTITY_FOLDERS: dict[str, str] = {
    "01-HJR-Global": "HJRG",
    "02-F3-Energy": "F3E",
    "03-F3-Community": "F3C",
    "04-UFL": "UFL",
    "05-HJR-Productions": "HJRPROD",
    "06-HJR-Properties": "HJRP",
    "07-Big-D-Media": "BDM",
    "08-Lexington-Services": "LEX",
    "09-One-Stop-Nutrition": "OSN",
    "00-Founder": "FNDR",
}

PHI_BLACKLIST_SEGMENTS = {"consumers", "clients", "phi", "clinical", "ehr"}


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = LOG_DIR / f"kb-sync-static-{today}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def is_phi_path(path: Path) -> bool:
    parts_lower = {p.lower() for p in path.parts}
    return bool(parts_lower & PHI_BLACKLIST_SEGMENTS)


def classify_entity(path: Path) -> str:
    try:
        rel = path.relative_to(FOUNDER_OS_ROOT)
    except ValueError:
        return "FNDR"
    parts = rel.parts
    if not parts:
        return "FNDR"
    return ENTITY_FOLDERS.get(parts[0], "FNDR")


def file_to_document(path: Path) -> Document | None:
    if is_phi_path(path):
        return None
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    if not content.strip():
        return None

    stat = path.stat()
    entity = classify_entity(path)
    rel_path = (
        str(path.relative_to(FOUNDER_OS_ROOT))
        if path.is_relative_to(FOUNDER_OS_ROOT)
        else str(path)
    )

    return Document(
        source="static_md",
        source_id=rel_path,
        entity=entity,
        content=content,
        date_created=int(stat.st_ctime),
        date_modified=int(stat.st_mtime),
        author="",
        title=path.stem.replace("-", " ").replace("_", " ").title(),
        deep_link=f"computer://{path}",
        metadata={"path": rel_path, "size_bytes": stat.st_size},
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
    log = logging.getLogger("kb-sync-static")
    log.info("=" * 60)
    log.info("Static MD incremental sync starting")

    if not FOUNDER_OS_ROOT.exists():
        log.error("Founder OS root not found: %s", FOUNDER_OS_ROOT)
        return 1

    kb = KnowledgeBase(KB_DB_PATH)
    state = kb.get_sync_state("static_md")

    if state is None:
        last_sync_ts = int(time.time()) - (args.fallback_days * 86400)
        log.warning("No watermark — falling back to last %d days", args.fallback_days)
    else:
        last_sync_ts = state[0]
        log.info("Resuming from watermark: %s",
                 datetime.fromtimestamp(last_sync_ts, tz=timezone.utc).isoformat())

    sync_start = int(time.time())

    # Walk + filter to modified-since-watermark files
    modified_files: list[Path] = []
    for path in FOUNDER_OS_ROOT.rglob("*.md"):
        if not path.is_file():
            continue
        if is_phi_path(path):
            continue
        if any(part.startswith(".") for part in path.parts):
            continue
        if "_archive" in str(path).lower():
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime > last_sync_ts:
            modified_files.append(path)

    log.info("Discovered %d modified-since-watermark files (out of full tree walk)", len(modified_files))

    if not modified_files:
        log.info("No files modified — nothing to ingest")
        kb.set_sync_state("static_md", sync_start, last_source_modified=sync_start)
        kb.close()
        return 0

    # Build Documents
    docs: list[Document] = []
    for f in modified_files:
        d = file_to_document(f)
        if d:
            docs.append(d)

    if not docs:
        log.warning("No valid documents from %d modified files", len(modified_files))
        kb.set_sync_state("static_md", sync_start)
        kb.close()
        return 0

    total_docs = 0
    total_chunks = 0
    t0 = time.time()
    exit_code = 0

    try:
        for i in range(0, len(docs), args.batch_size):
            batch = docs[i : i + args.batch_size]
            total_chunks += kb.upsert_documents(batch)
            total_docs += len(batch)
            log.info("Batch ingested: %d docs (running: %d / %d chunks)",
                     len(batch), total_docs, total_chunks)
    except KnowledgeBaseError as exc:
        log.error("KB upsert failed: %s", exc)
        exit_code = 1
    finally:
        elapsed = time.time() - t0
        log.info(
            "Static MD sync complete in %.1fs — %d docs → %d chunks (exit=%d)",
            elapsed, total_docs, total_chunks, exit_code,
        )

    if exit_code == 0:
        kb.set_sync_state("static_md", sync_start, last_source_modified=sync_start)
        log.info("Watermark advanced")

    kb.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
