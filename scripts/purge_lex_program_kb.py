#!/usr/bin/env python3
"""Purge already-ingested LEX program/client/DDD/clinical meeting chunks (WS2).

Pairs with the ingest-time hard-exclude (fireflies_connector.classify_lex_meeting,
wired into backfill): the exclusion stops FUTURE ingestion of Lexington program /
client-facing / DDD / clinical / LBHS meeting transcripts; this removes what is
already in the KB. Root case: a Lexington probation "1st Budget Class" (organizer
@hjrglobal.com, *.maricopa.gov clients) was classified HJRG and ingested,
exposing criminal-justice client PII outside LEX -- the Phase 1.4 sub_entity
purge (LEX-LBHS/LTS) missed it because it was tagged GM-LEX/HJRG, not a sub-entity.

A stored chunk has no attendee/organizer data, so we match on what IS stored:
the meeting TITLE (program / DDD / clinical patterns, sourced from the SAME
detector config), the specific transcript_id, and the LEX-LBHS sub_entity tag.
Dry-run REPORTS every matched title so Harrison can eyeball before --apply.

Usage (--dry-run is read-only + safe anytime; STOP Cora before --apply):
    .venv\\Scripts\\python.exe scripts\\purge_lex_program_kb.py                       # dry-run
    .venv\\Scripts\\python.exe scripts\\purge_lex_program_kb.py --apply               # delete (Cora stopped)
    .venv\\Scripts\\python.exe scripts\\purge_lex_program_kb.py --db <path>
    .venv\\Scripts\\python.exe scripts\\purge_lex_program_kb.py --transcript-id <id>  # add a known id
After --apply, reclaim disk: .venv\\Scripts\\python.exe scripts\\reclaim_kb_space.py

Exit codes: 0 ok, 1 fatal.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

from cora.connectors.fireflies_connector import (  # noqa: E402
    _PHI_TITLE_KEYWORDS,
    _load_lex_detect_cfg,
)
from cora.knowledge_base import schema  # noqa: E402

KB_DB_PATH = _REPO / "data" / "cora_kb.db"
# The known live gap (Lexington probation budget class).
_BUDGET_CLASS_ID = "01KVBWJTMYD9VXGPFZV0CQB5GV"
_BATCH = 500

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("purge-lex-program-kb")


def target(conn, transcript_ids: set[str]):
    """Read-only. Return (chunk_ids, [(title, reason)] samples)."""
    cfg = _load_lex_detect_cfg()
    program = [p.lower() for p in cfg["program_titles"]]
    ddd = [p.lower() for p in cfg["ddd_titles"]]
    clinical = [p.lower() for p in _PHI_TITLE_KEYWORDS]

    rows = conn.execute(
        "SELECT chunk_id, source_id, title, sub_entity FROM knowledge_chunks WHERE source='fireflies'"
    ).fetchall()
    ids: set[str] = set()
    samples: dict[str, str] = {}  # title -> reason (dedup display)
    for chunk_id, source_id, title, sub_entity in rows:
        tl = (title or "").lower()
        reason = None
        if str(source_id or "") in transcript_ids:
            reason = "transcript-id"
        elif str(sub_entity or "") == "LEX-LBHS":
            reason = "LBHS/Part-2"
        elif any(p in tl for p in program):
            reason = "program-title"
        elif any(p in tl for p in ddd):
            reason = "ddd-title"
        elif any(p in tl for p in clinical):
            reason = "clinical-title"
        if reason:
            ids.add(chunk_id)
            samples.setdefault(title or "(untitled)", reason)
    return ids, samples


def delete_chunks(conn, chunk_ids) -> dict:
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
    ap = argparse.ArgumentParser(description="Purge LEX program/client meeting chunks (WS2).")
    ap.add_argument("--apply", action="store_true", help="Delete (default is a read-only dry-run).")
    ap.add_argument("--dry-run", action="store_true", help="Report only (default).")
    ap.add_argument("--db", default=str(KB_DB_PATH), help="Path to the KB sqlite DB.")
    ap.add_argument("--transcript-id", action="append", default=[],
                    help="Additional transcript id(s) to purge (repeatable).")
    args = ap.parse_args()
    apply_changes = args.apply and not args.dry_run

    db_path = Path(args.db)
    if not db_path.exists():
        log.error("KB database not found: %s", db_path)
        return 1

    transcript_ids = {_BUDGET_CLASS_ID, *[t.strip() for t in args.transcript_id if t.strip()]}

    conn = schema.connect(db_path)
    try:
        ids, samples = target(conn, transcript_ids)
        log.info("=== Purge scope (LEX program/client/DDD/clinical meeting chunks) ===")
        log.info("  Known transcript ids targeted: %s", ", ".join(sorted(transcript_ids)))
        log.info("  Matched meeting titles (title -> reason):")
        for title, reason in sorted(samples.items()):
            log.info("      [%s] %s", reason, title[:120])
        log.info("  TOTAL chunks that --apply would delete: %d (across %d distinct titles)",
                 len(ids), len(samples))

        if not apply_changes:
            log.info("Dry-run -- nothing deleted. Re-run with --apply (Cora STOPPED), "
                     "then reclaim_kb_space.py.")
            return 0
        if not ids:
            log.info("Nothing to delete.")
            return 0

        log.info("Deleting %d chunks from knowledge_chunks + both vec tables...", len(ids))
        totals = delete_chunks(conn, ids)
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
