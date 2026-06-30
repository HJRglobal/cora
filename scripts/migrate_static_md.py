#!/usr/bin/env python3
"""One-shot migration: ingest all static markdown context into the Knowledge Base.

What gets ingested:
  - Founder OS CLAUDE.md (G:\\My Drive\\HJR-Founder-OS\\CLAUDE.md) → entity=FNDR
  - Per-entity CLAUDE.md briefs (02-F3-Energy/, 08-Lexington-Services/, etc.) → entity=<code>
  - memory/decisions.md → entity=FNDR (every entry tagged FNDR; entity-specific decisions
    will be re-classified to their specific entity during Phase 3B refinement)
  - memory/projects.md, memory/people.md, memory/companies.md → entity=FNDR
  - design/known-answers/<entity>.md → entity=<code>
  - _shared/projects/<project>/CLAUDE.md → entity=FNDR (project briefs)
  - _shared/playbooks/*.md → entity=FNDR (cross-cutting playbooks)

PHI guardrail: explicitly skips any file under 08-Lexington-Services/{Consumers,Clients}/
or any path containing /phi/ or /clinical/.

Usage:
    cd C:\\Users\\Harri\\code\\cora
    uv run python scripts/migrate_static_md.py
    # Or with dry-run flag:
    uv run python scripts/migrate_static_md.py --dry-run
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Add src to path so we can import cora.* without installing
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cora.knowledge_base import KnowledgeBase, KnowledgeBaseError  # noqa: E402
from cora.knowledge_base.store import Document  # noqa: E402
from cora.kb_exclusions import is_cora_internal_path, is_swept_path  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("migrate_static_md")

# Founder OS root + KB path
FOUNDER_OS_ROOT = Path(r"G:\My Drive\HJR-Founder-OS")
CORA_REPO_ROOT = Path(__file__).resolve().parents[1]
KB_DB_PATH = CORA_REPO_ROOT / "data" / "cora_kb.db"

# Per-entity folder → entity code mapping
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

# PHI guardrail: any path containing these segments is skipped entirely
PHI_BLACKLIST_SEGMENTS = {"consumers", "clients", "phi", "clinical", "ehr"}


def is_phi_path(path: Path) -> bool:
    """Return True if any path segment matches the PHI blacklist (case-insensitive)."""
    parts_lower = {p.lower() for p in path.parts}
    return bool(parts_lower & PHI_BLACKLIST_SEGMENTS)


def classify_entity(path: Path) -> str:
    """Best-effort entity classification from the file path."""
    rel = path.relative_to(FOUNDER_OS_ROOT) if path.is_relative_to(FOUNDER_OS_ROOT) else path
    parts = rel.parts
    if not parts:
        return "FNDR"
    first = parts[0]
    if first in ENTITY_FOLDERS:
        return ENTITY_FOLDERS[first]
    return "FNDR"


def file_to_document(path: Path) -> Document | None:
    """Read a markdown file and convert to a Document. Returns None on read failure."""
    if is_phi_path(path):
        log.info("PHI guardrail: skipping %s", path)
        return None
    # Drive-materialization output: never re-ingest the nightly _brain/swept/ digests
    # (loop + bloat) — the full-rebuild path must apply the same guard as the incremental.
    if is_swept_path(path):
        return None
    # Cora's own build/audit/forensic docs are operational metadata, not org knowledge
    # (parity with incremental_sync_static + the kb-rebuild.md guard claim).
    if is_cora_internal_path(path):
        return None
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        log.warning("Failed to read %s: %s", path, exc)
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
        # Drive deep-link: best-effort. The actual Drive URL would require a Drive API
        # lookup; for now we surface the local path so the static-md citation shows
        # where the content lives. Future: enrich with Drive file_id via Drive connector.
        deep_link=f"computer://{path}",
        metadata={"path": rel_path, "size_bytes": stat.st_size},
    )


def discover_files() -> list[Path]:
    """Walk the Founder OS root and collect all *.md files (excluding PHI paths)."""
    found: list[Path] = []
    for path in FOUNDER_OS_ROOT.rglob("*.md"):
        if not path.is_file():
            continue
        if is_phi_path(path):
            log.info("PHI guardrail: skipping %s", path)
            continue
        # Drive-materialization output — never re-ingest (loop guard, both static walks).
        if is_swept_path(path):
            continue
        # Cora's own build/audit/forensic docs are not org knowledge.
        if is_cora_internal_path(path):
            continue
        # Skip obvious noise: .obsidian dot-dirs, archive folders, etc.
        if any(part.startswith(".") for part in path.parts):
            continue
        if "_archive" in str(path).lower():
            continue
        found.append(path)
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Discover files but don't ingest")
    parser.add_argument("--limit", type=int, default=0, help="Cap ingestion to N files (0 = no cap)")
    args = parser.parse_args()

    if not FOUNDER_OS_ROOT.exists():
        log.error("Founder OS root not found: %s", FOUNDER_OS_ROOT)
        return 1

    if not os.environ.get("OPENAI_API_KEY"):
        log.error("OPENAI_API_KEY not set in environment — cannot embed")
        return 1

    files = discover_files()
    log.info("Discovered %d markdown files under %s", len(files), FOUNDER_OS_ROOT)

    if args.limit:
        files = files[: args.limit]
        log.info("Limited to first %d files for this run", len(files))

    if args.dry_run:
        # Group by entity for visibility
        by_entity: dict[str, int] = {}
        for f in files:
            ent = classify_entity(f)
            by_entity[ent] = by_entity.get(ent, 0) + 1
        log.info("Dry-run: would ingest by entity = %s", by_entity)
        for f in files[:30]:
            log.info("  %s → %s", classify_entity(f), f.relative_to(FOUNDER_OS_ROOT))
        if len(files) > 30:
            log.info("  ... and %d more", len(files) - 30)
        return 0

    # Build Documents
    docs: list[Document] = []
    for f in files:
        d = file_to_document(f)
        if d:
            docs.append(d)
    log.info("Built %d Document objects (some files skipped due to read errors or PHI)", len(docs))

    if not docs:
        log.warning("No documents to ingest — exiting")
        return 0

    # Ingest in batches to avoid one giant embedding call
    BATCH = 50  # docs per upsert call
    kb = KnowledgeBase(KB_DB_PATH)
    total_chunks = 0
    t0 = time.time()
    try:
        for i in range(0, len(docs), BATCH):
            batch = docs[i : i + BATCH]
            chunks_written = kb.upsert_documents(batch)
            total_chunks += chunks_written
            log.info(
                "Batch %d-%d: %d docs → %d chunks (running total: %d)",
                i, i + len(batch), len(batch), chunks_written, total_chunks,
            )
    except KnowledgeBaseError as exc:
        log.error("KB upsert failed: %s", exc)
        return 1
    finally:
        elapsed = time.time() - t0
        stats = kb.stats()
        log.info("Ingestion complete in %.1fs", elapsed)
        log.info("KB stats: %s", stats)
        kb.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
