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
import hashlib
import json
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
from cora.kb_exclusions import is_cora_internal_path, is_swept_path  # noqa: E402

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

# F-09: the mtime watermark alone MISSES a content change that does not advance
# mtime past the watermark -- notably a REDACTION (content shrinks) synced by Drive
# File Stream, which left stale figure chunks in the KB after the 7/11 redaction.
# A per-source_id content sha256 sidecar makes the sync CONTENT-change-driven: a
# file re-ingests when its content hash differs from the last ingested hash, even
# if mtime didn't move. The store is shrink-safe (upsert purges prior chunks), so
# this only fixes the TRIGGER, not the dedup.
_HASH_STORE_PATH = CORA_REPO_ROOT / "data" / "state" / "static-md-content-hashes.json"


def _rel_key(path: Path) -> str:
    """The source_id key -- identical to file_to_document's rel_path."""
    return (
        str(path.relative_to(FOUNDER_OS_ROOT))
        if path.is_relative_to(FOUNDER_OS_ROOT)
        else str(path)
    )


def _sha256_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _load_hash_store() -> dict[str, str]:
    try:
        with open(_HASH_STORE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_hash_store(store: dict[str, str]) -> None:
    try:
        _HASH_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _HASH_STORE_PATH.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(store, f)
        tmp.replace(_HASH_STORE_PATH)
    except OSError as exc:
        logging.getLogger("kb-sync-static").warning(
            "could not persist static-md content-hash store: %s", exc
        )


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


# is_swept_path now lives in cora.kb_exclusions (shared with migrate_static_md so a
# third static walk can never drift). Imported above.


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
    if is_swept_path(path):
        return None
    # Cora's own build/audit/forensic docs are operational metadata, not org
    # knowledge — keep them out of the KB (they fabricate "diagnostics" via RAG).
    if is_cora_internal_path(path):
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

    # Walk + filter to changed files. A file re-ingests when its mtime is past the
    # watermark OR its content hash differs from the last ingested hash (F-09: a
    # redaction can change content without advancing mtime past the watermark).
    hash_store = _load_hash_store()
    pending_hashes: dict[str, str] = {}
    modified_files: list[Path] = []
    skipped_cora_internal = 0
    hash_triggered = 0
    for path in FOUNDER_OS_ROOT.rglob("*.md"):
        if not path.is_file():
            continue
        if is_phi_path(path):
            continue
        # Drive-materialization output — never re-ingest (loop guard).
        if is_swept_path(path):
            continue
        # Cora's own build/audit/forensic docs are NOT org knowledge — never ingest.
        if is_cora_internal_path(path):
            skipped_cora_internal += 1
            continue
        if any(part.startswith(".") for part in path.parts):
            continue
        if "_archive" in str(path).lower():
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        key = _rel_key(path)
        digest = _sha256_file(path)
        mtime_changed = mtime > last_sync_ts
        content_changed = digest is not None and hash_store.get(key) != digest
        if mtime_changed or content_changed:
            modified_files.append(path)
            if digest is not None:
                pending_hashes[key] = digest
            if content_changed and not mtime_changed:
                hash_triggered += 1

    if skipped_cora_internal:
        log.info("Excluded %d Cora build/audit docs from ingest (cora-internal)", skipped_cora_internal)
    log.info(
        "Discovered %d changed files (out of full tree walk); %d via content-hash "
        "only (mtime unchanged -- e.g. a redaction)",
        len(modified_files), hash_triggered,
    )

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
        # Record hashes so content-changed-but-empty files don't re-select forever.
        if pending_hashes:
            hash_store.update(pending_hashes)
            _save_hash_store(hash_store)
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
        if pending_hashes:
            hash_store.update(pending_hashes)
            _save_hash_store(hash_store)
            log.info("Content-hash store updated for %d files", len(pending_hashes))

    kb.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
