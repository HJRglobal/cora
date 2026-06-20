#!/usr/bin/env python3
"""Purge already-ingested Cora build/audit/forensic docs from the KB (WS1).

Pairs with the ingest-time exclusion (src/cora/kb_exclusions.py, wired into
incremental_sync_static.py): the exclusion stops FUTURE ingestion of Cora's own
build/audit/forensic/code-prompt docs under ``_shared/projects/cora/``; this
removes what is already in the KB. Those chunks are why a "diagnose yourself"
query could RAG-narrate Cora's own audit notes (a fabricated "diagnostic").

Two scopes:
  STATIC_MD  -- chunks with source='static_md' whose source_id is cora-internal
                (folder ``_shared/projects/cora/`` or a cora-build filename),
                decided by the SAME predicate the ingest path uses.
  NOTES      -- (opt-in, --include-notes) user_note chunks whose content matches
                a suspicious-fabrication pattern (default: minute press /
                diagnostic / self-diagnos). Reported for Harrison to eyeball;
                deleted only with --apply --include-notes.

Usage (--dry-run is read-only + safe anytime, even with Cora live; STOP Cora
before --apply because the delete contends with live writes):
    .venv\\Scripts\\python.exe scripts\\purge_cora_internal_kb.py                 # dry-run report
    .venv\\Scripts\\python.exe scripts\\purge_cora_internal_kb.py --apply         # delete static_md cora docs
    .venv\\Scripts\\python.exe scripts\\purge_cora_internal_kb.py --apply --include-notes
    .venv\\Scripts\\python.exe scripts\\purge_cora_internal_kb.py --db <path>      # target a specific DB
After --apply, reclaim disk: .venv\\Scripts\\python.exe scripts\\reclaim_kb_space.py

Exit codes: 0 ok, 1 fatal.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

from cora.kb_exclusions import (  # noqa: E402
    is_cora_internal_source_id,
    is_cora_internal_title,
)
from cora.knowledge_base import schema  # noqa: E402

KB_DB_PATH = _REPO / "data" / "cora_kb.db"
_BATCH = 500

# drive_sweep + drive_asset copy Founder-OS Drive files into the KB under a
# Drive-FILE-ID source_id with the filename in `title` -- the path-based
# source_id rule can't see them, so we match the stored title here. (This is the
# dominant leak vector that the static_md-only purge missed; see WS1-DRIVE.)
_DRIVE_COPY_SOURCES = ("drive_sweep", "drive_asset")

# Suspicious-fabrication phrases for the opt-in user_note sweep. These are the
# shapes of a fabricated self-"diagnostic" note (e.g. the Minute Press miss).
_DEFAULT_NOTE_PATTERN = r"minute press|self-?diagnos|diagnostic finding|finding-code|fabricat"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("purge-cora-internal-kb")


def target_static_md(conn) -> tuple[list[str], list[str]]:
    """Read-only. Return (chunk_ids, sample source_ids) for cora-internal static_md."""
    rows = conn.execute(
        "SELECT chunk_id, source_id FROM knowledge_chunks WHERE source='static_md'"
    ).fetchall()
    ids: list[str] = []
    sources: set[str] = set()
    for chunk_id, source_id in rows:
        if is_cora_internal_source_id(str(source_id or "")):
            ids.append(chunk_id)
            sources.add(str(source_id or ""))
    return ids, sorted(sources)


def target_drive_doc_copies(conn, *, broad: bool = False) -> tuple[list[str], list[str]]:
    """Read-only. Cora build/audit docs ingested as Drive copies (drive_sweep/asset).

    Matches on the stored `title` (the filename) OR the source_id, since the Drive
    copy's source_id is a file id with no path. `broad=True` widens to Cora's full
    ops/build doc set. Returns (chunk_ids, sample 'title' filenames).
    """
    ph = ",".join("?" * len(_DRIVE_COPY_SOURCES))
    rows = conn.execute(
        f"SELECT chunk_id, source_id, title FROM knowledge_chunks WHERE source IN ({ph})",
        _DRIVE_COPY_SOURCES,
    ).fetchall()
    ids: list[str] = []
    names: set[str] = set()
    for chunk_id, source_id, title in rows:
        if is_cora_internal_title(str(title or ""), broad=broad) or is_cora_internal_source_id(
            str(source_id or "")
        ):
            ids.append(chunk_id)
            names.add(str(title or source_id or ""))
    return ids, sorted(names)


def target_notes(conn, pattern: str) -> list[tuple[str, str, str]]:
    """Read-only. Return [(chunk_id, source_id, content_excerpt)] for matching notes."""
    rx = re.compile(pattern, re.IGNORECASE)
    rows = conn.execute(
        "SELECT chunk_id, source_id, content FROM knowledge_chunks WHERE source='user_note'"
    ).fetchall()
    hits: list[tuple[str, str, str]] = []
    for chunk_id, source_id, content in rows:
        text = str(content or "")
        if rx.search(text):
            excerpt = " ".join(text.split())[:160]
            hits.append((chunk_id, str(source_id or ""), excerpt))
    return hits


def delete_chunks(conn, chunk_ids) -> dict:
    """Batched delete from all 3 tables. Returns rows deleted per table."""
    totals = {"knowledge_vec_bin": 0, "knowledge_vec_f32": 0, "knowledge_chunks": 0}
    ids = list(chunk_ids)
    for i in range(0, len(ids), _BATCH):
        batch = ids[i : i + _BATCH]
        ph = ",".join("?" * len(batch))
        for tbl in ("knowledge_vec_bin", "knowledge_vec_f32", "knowledge_chunks"):
            cur = conn.execute(f"DELETE FROM {tbl} WHERE chunk_id IN ({ph})", batch)
            totals[tbl] += cur.rowcount
    conn.commit()
    return totals


def main() -> int:
    ap = argparse.ArgumentParser(description="Purge Cora build/audit docs from the KB (WS1).")
    ap.add_argument("--apply", action="store_true", help="Delete (default is a read-only dry-run).")
    ap.add_argument("--dry-run", action="store_true", help="Report only (this is the default).")
    ap.add_argument("--db", default=str(KB_DB_PATH), help="Path to the KB sqlite DB.")
    ap.add_argument("--include-notes", action="store_true",
                    help="Also sweep+delete suspicious user_note chunks (opt-in).")
    ap.add_argument("--note-pattern", default=_DEFAULT_NOTE_PATTERN,
                    help="Regex (case-insensitive) for the suspicious-note sweep.")
    ap.add_argument("--scope", choices=("targeted", "broad"), default="targeted",
                    help="Drive-copy breadth. targeted (default): build/audit/forensic/"
                         "log artifacts. broad: also reviews/proposals/plans/specs/"
                         "code-session docs (still cora- + keyword; legit docs spared).")
    args = ap.parse_args()
    apply_changes = args.apply and not args.dry_run
    broad = args.scope == "broad"

    db_path = Path(args.db)
    if not db_path.exists():
        log.error("KB database not found: %s", db_path)
        return 1

    conn = schema.connect(db_path)
    try:
        static_ids, static_sources = target_static_md(conn)
        log.info("=== Purge scope (Cora build/audit docs) [drive-copy scope=%s] ===", args.scope)
        log.info("  STATIC_MD cora-internal chunks: %d  (across %d files)",
                 len(static_ids), len(static_sources))
        for sid in static_sources[:40]:
            log.info("      %s", sid)
        if len(static_sources) > 40:
            log.info("      ... +%d more files", len(static_sources) - 40)

        drive_ids, drive_names = target_drive_doc_copies(conn, broad=broad)
        log.info("  DRIVE-COPY (drive_sweep/drive_asset) cora-internal chunks: %d  (across %d files)",
                 len(drive_ids), len(drive_names))
        for nm in drive_names[:40]:
            log.info("      %s", nm)
        if len(drive_names) > 40:
            log.info("      ... +%d more files (see full manifest below)", len(drive_names) - 40)

        # Full auditable manifest: the inline log samples at 40, so write EVERY
        # selected filename to disk -- a broad --apply must be reviewable in full
        # before it irreversibly deletes anything.
        try:
            manifest = _REPO / "logs" / f"purge-cora-internal-{args.scope}.txt"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            with manifest.open("w", encoding="utf-8") as fh:
                fh.write(f"# Cora-internal purge manifest  scope={args.scope}\n")
                fh.write(f"# static_md files ({len(static_sources)}):\n")
                for s in static_sources:
                    fh.write(f"  {s}\n")
                fh.write(f"# drive-copy files ({len(drive_names)}):\n")
                for n in drive_names:
                    fh.write(f"  {n}\n")
            log.info("  Full file manifest written -> %s", manifest)
        except Exception as exc:  # noqa: BLE001
            log.warning("  could not write manifest: %s", exc)

        note_hits: list[tuple[str, str, str]] = []
        if args.include_notes:
            note_hits = target_notes(conn, args.note_pattern)
            log.info("  USER_NOTE suspicious matches (pattern=%r): %d", args.note_pattern, len(note_hits))
            for chunk_id, source_id, excerpt in note_hits:
                log.info("      [%s] %s", chunk_id, excerpt)
        else:
            preview = target_notes(conn, args.note_pattern)
            log.info("  USER_NOTE suspicious matches (report-only; pass --include-notes to delete): %d",
                     len(preview))
            for chunk_id, source_id, excerpt in preview:
                log.info("      [%s] %s", chunk_id, excerpt)

        to_delete = list(static_ids) + list(drive_ids) + [h[0] for h in note_hits]
        log.info("  TOTAL chunks that --apply would delete: %d "
                 "(static_md %d + drive-copy %d%s)",
                 len(to_delete), len(static_ids), len(drive_ids),
                 f" + notes {len(note_hits)}" if args.include_notes else "")

        if not apply_changes:
            log.info("Dry-run -- nothing deleted. Re-run with --apply (Cora STOPPED), "
                     "then reclaim_kb_space.py.")
            return 0

        if not to_delete:
            log.info("Nothing to delete.")
            return 0

        log.info("Deleting %d chunks from knowledge_chunks + both vec tables...", len(to_delete))
        totals = delete_chunks(conn, to_delete)
        log.info("Deleted: %s", totals)
        log.info("Reclaim disk with: .venv\\Scripts\\python.exe scripts\\reclaim_kb_space.py")
    except Exception as exc:  # noqa: BLE001
        log.error("purge failed: %s", exc, exc_info=True)
        conn.close()
        return 1
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
