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
from typing import Iterator

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cora.knowledge_base import KnowledgeBase, KnowledgeBaseError  # noqa: E402
from cora.knowledge_base.store import Document  # noqa: E402
from cora.kb_exclusions import (  # noqa: E402
    is_copa_bhrf_path,
    is_cora_internal_path,
    is_swept_path,
)

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

# Exact-filename companions to the ``*.md`` walk (2026-09-03, knowledge-parity
# audit gap G5). ``bootstrap.txt`` is the per-project Cowork bootstrap note (28
# in the tree on 2026-09-03) and was never walked because the sync is ``.md``-only.
# It is the ONLY non-``.md`` name walked -- ``*.txt`` in general stays out (an
# ``env.txt`` / ``notes.txt`` never enters). Every candidate passes the SAME
# exclusion chain as the ``.md`` files (``is_static_excluded``) and the same
# ``classify_entity``, so a ``bootstrap.txt`` under ``_shared/projects/cora/``
# or ``copa-bhrf/`` is excluded exactly like its ``.md`` siblings.
# migrate_static_md.py (the full rebuild) imports these so the two walks cannot
# drift.
STATIC_EXACT_FILENAMES: tuple[str, ...] = ("bootstrap.txt",)


def iter_static_candidates(root: Path) -> Iterator[Path]:
    """Every file the static walk considers: ``*.md`` plus the exact-filename
    companions. Filtering (``is_static_excluded``) is the caller's job so the
    incremental and full-rebuild walks share ONE candidate generator."""
    yield from root.rglob("*.md")
    for name in STATIC_EXACT_FILENAMES:
        yield from root.rglob(name)


def is_static_excluded(path: Path) -> bool:
    """The single exclusion chain for the static walk -- PHI path segments,
    ``_brain/swept`` materialization output, Cora's own workspace (D-057), the
    copa-bhrf NDA folder, dot-dirs, and ``_archive`` trees. Returns True when the
    path must NOT be ingested. Shared by ``main`` and ``file_to_document``'s
    callers so a new candidate class (bootstrap.txt) inherits every rule."""
    if is_phi_path(path):
        return True
    if is_swept_path(path):
        return True
    if is_cora_internal_path(path):
        return True
    if is_copa_bhrf_path(str(path)):
        return True
    if any(part.startswith(".") for part in path.parts):
        return True
    if "_archive" in str(path).lower():
        return True
    return False

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


def is_delegated_work_path(path: Path) -> bool:
    """True when the file lives under an entity's `_delegated-work/` tree
    (delegated-work artifacts -- AI-authored, tagged bot_authored at ingest)."""
    return any(p.lower() == "_delegated-work" for p in path.parts)


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


def _static_title(path: Path) -> str:
    """Human title for a static doc. A ``bootstrap.txt`` is named after the
    project folder it bootstraps ("Pure Launch Bootstrap"), never the bare stem
    -- 28 chunks all titled "Bootstrap" would be indistinguishable in retrieval."""
    stem = path.stem.replace("-", " ").replace("_", " ").title()
    if path.name.lower() in STATIC_EXACT_FILENAMES:
        parent = path.parent.name.replace("-", " ").replace("_", " ").title()
        return f"{parent} {stem}".strip()
    return stem


def file_to_document(path: Path) -> Document | None:
    if is_phi_path(path):
        return None
    if is_swept_path(path):
        return None
    # Cora's own build/audit/forensic docs are operational metadata, not org
    # knowledge — keep them out of the KB (they fabricate "diagnostics" via RAG).
    if is_cora_internal_path(path):
        return None
    # copa-bhrf: LEX NDA'd M&A-diligence folder, purged from the KB 2026-07-21.
    # The canonical copy stays on Drive IN PLACE (outside _archive), so it would
    # otherwise re-ingest on the next static sweep (decision §2c). Belt for the
    # store Step-0 chokepoint (which also blocks it).
    if is_copa_bhrf_path(str(path)):
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

    metadata: dict = {"path": rel_path, "size_bytes": stat.st_size}
    if path.name.lower() in STATIC_EXACT_FILENAMES:
        metadata["kind"] = "bootstrap"
    # Delegated-work artifacts (2026-08-01, D-096 lesson): anything under an
    # entity's _delegated-work/ tree is AI-authored output. Tag it
    # bot_authored so the existing machinery applies for free -- the "not
    # canon" retrieval label on every chunk (context_loader) and exclusion
    # from gap/friction/reconciliation mining. Without this, an AI draft
    # containing decision-shaped language could fuzzy-suppress a real
    # uncaptured decision and delegated outputs would re-enter retrieval
    # indistinguishable from canon (the self-poisoning class D-096 closed
    # for Cora's own Slack replies).
    if is_delegated_work_path(path):
        metadata["bot_authored"] = True
        # D-051 F1 (2026-08-06, LEX lane): the artifact carries no sub_entity,
        # so store.upsert_documents Step 0 would RE-DERIVE one from its own
        # CONTENT. For a LEX research brief that content is model-written from
        # untrusted fetched pages -- i.e. third-party text would choose which
        # Lexington sub-entity channel it becomes visible in. lex_gm_level opts
        # the doc out of that detection, pinning it GM-level (sub_entity NULL)
        # where a human decides its scope. Applied to every entity's artifacts,
        # not just LEX: the same argument holds anywhere auto-detection reads
        # AI-authored prose, and it keeps one rule instead of a LEX branch.
        metadata["lex_gm_level"] = True

    return Document(
        source="static_md",
        source_id=rel_path,
        entity=entity,
        content=content,
        date_created=int(stat.st_ctime),
        date_modified=int(stat.st_mtime),
        author="",
        title=_static_title(path),
        deep_link=f"computer://{path}",
        metadata=metadata,
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
    for path in iter_static_candidates(FOUNDER_OS_ROOT):
        if not path.is_file():
            continue
        # Cora's own build/audit/forensic docs are NOT org knowledge — never ingest
        # (counted separately for the log line; is_static_excluded re-checks it).
        if is_cora_internal_path(path):
            skipped_cora_internal += 1
            continue
        # PHI segments, _brain/swept, copa-bhrf, dot-dirs, _archive -- one chain
        # shared with the bootstrap.txt candidates and the full rebuild.
        if is_static_excluded(path):
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
