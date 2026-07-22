#!/usr/bin/env python3
r"""Purge the NDA'd LBHS-COPA / Copa Health M&A-diligence MEETING transcripts from
the KB (2026-07-21 Harrison decision; BUILD 2 of the remaining-work kickoff).

These meetings (Fireflies transcripts) live OUTSIDE the copa-bhrf project folder the
D-086 pass handled, so they are keyed by MEETING TITLE, not path. This is a chunk
purge + (separately) a forward Fireflies-ingest exclusion (in
connectors/fireflies_connector via kb_exclusions.is_copa_meeting_title).

MATCH: kb_exclusions.is_copa_meeting_title -- the whole word "copa" (the acronym /
"Copa Health" / "Copa Model"), case-insensitive. NEVER a bare "copa" LIKE (would hit
Maricopa / copayment / copacker) and NEVER bare "Voyager" (Lexington's fleet Chrysler
Voyager minivans collide with it across the corpus). Titles are enumerated then
filtered in Python (precise); chunk_ids are resolved by exact title IN and purged via
the shared 3-table cascade (kb_archive.delete_chunks).

SAFETY: PHI/NDA-adjacent, so this stays HUMAN-REVIEWED even under the D-011 relaxation.
Dry-run is the DEFAULT: it PREVIEWS the matched titles for Harrison to review before
--apply. For --apply: STOP Cora + BACK UP cora_kb.db first (same posture as the D-086
purge); a small purge can skip VACUUM. Reversibility = re-ingest from Fireflies IF ever
wrong -- BUT note the forward exclusion will now block COPA re-ingest, so the manifest
(purged chunk_ids + titles) + this dry-run are the audit/undo record.

Usage (from repo root):
    .venv\Scripts\python.exe scripts\purge_copa_transcripts.py                 # DRY-RUN (default)
    .venv\Scripts\python.exe scripts\purge_copa_transcripts.py --apply         # PURGE (Cora stopped + db backed up)
    .venv\Scripts\python.exe scripts\purge_copa_transcripts.py --sources fireflies,drive_asset,drive_sweep --apply   # also the Drive copies

Exit codes: 0 ok, 1 fatal.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

from cora import kb_archive  # noqa: E402
from cora.kb_exclusions import is_copa_meeting_title  # noqa: E402

KB_DB_PATH = _REPO / "data" / "cora_kb.db"
LOG_DIR = _REPO / "logs"
_BATCH = 500
# Drive-copy sources the D-086 cascade flagged (same NDA'd content, FNDR-scoped,
# broadly retrievable). Out of the default fireflies scope -- surfaced for review.
_DRIVE_SOURCES = ("drive_asset", "drive_sweep")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("purge-copa-transcripts")


def select_copa_chunks(conn, sources: tuple[str, ...]) -> dict[tuple[str, str], list[str]]:
    """{(source, title): [chunk_ids]} for chunks whose TITLE matches the COPA meeting
    matcher, restricted to `sources`. Enumerates DISTINCT titles, filters in Python
    (precise -- never a bare-'copa' LIKE), then resolves chunk_ids by exact title IN."""
    if not sources:
        return {}
    ph = ",".join("?" * len(sources))
    titles = [r[0] for r in conn.execute(
        f"SELECT DISTINCT title FROM knowledge_chunks WHERE source IN ({ph}) AND title IS NOT NULL",
        list(sources),
    ).fetchall()]
    matched = sorted(t for t in titles if is_copa_meeting_title(t))
    out: dict[tuple[str, str], list[str]] = {}
    for t in matched:
        rows = conn.execute(
            f"SELECT chunk_id, source FROM knowledge_chunks WHERE source IN ({ph}) AND title=?",
            list(sources) + [t],
        ).fetchall()
        for cid, src in rows:
            out.setdefault((src, t), []).append(cid)
    return out


def _summarize(selected: dict[tuple[str, str], list[str]]) -> list[dict]:
    return [{"source": s, "title": t, "chunks": len(ids)}
            for (s, t), ids in sorted(selected.items())]


def main() -> int:
    ap = argparse.ArgumentParser(description="Purge NDA'd COPA meeting transcripts (dry-run default).")
    ap.add_argument("--apply", action="store_true", help="Execute the purge (default is a read-only dry-run).")
    ap.add_argument("--sources", default="fireflies",
                    help="Comma-separated KB sources to purge (default 'fireflies'). "
                         "Add drive_asset,drive_sweep to also purge the Drive copies.")
    ap.add_argument("--db", default=str(KB_DB_PATH))
    ap.add_argument("--report", metavar="PATH", help="Also write the manifest JSON here.")
    args = ap.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    db_path = Path(args.db)
    if not db_path.exists():
        log.error("KB database not found: %s", db_path)
        return 1
    sources = tuple(s.strip() for s in args.sources.split(",") if s.strip())
    mode = "APPLY" if args.apply else "DRY-RUN"

    ro = kb_archive.connect_ro(db_path)
    try:
        selected = select_copa_chunks(ro, sources)
        flag_sources = tuple(s for s in _DRIVE_SOURCES if s not in sources)
        flagged = select_copa_chunks(ro, flag_sources) if flag_sources else {}
    finally:
        ro.close()

    all_ids = sorted({cid for ids in selected.values() for cid in ids})
    matched_rows = _summarize(selected)
    flagged_rows = _summarize(flagged)

    log.info("=" * 72)
    log.info("COPA transcript purge  mode=%s  sources=%s  db=%s", mode, ",".join(sources), db_path)
    log.info("MATCHED (in scope) -- %d chunk(s) across %d title(s):", len(all_ids), len(matched_rows))
    for row in matched_rows:
        log.info("   [%s] %r -> %d chunks", row["source"], row["title"], row["chunks"])
    if flagged_rows:
        log.warning("FLAGGED -- same NDA'd COPA content in %s, NOT in the purge scope "
                    "(FNDR-scoped + retrievable). Re-run with --sources %s to include:",
                    ",".join(flag_sources), args.sources + "," + ",".join(flag_sources))
        for row in flagged_rows:
            log.warning("   [%s] %r -> %d chunks", row["source"], row["title"], row["chunks"])

    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    manifest = LOG_DIR / f"purge-copa-transcripts-manifest-{ts}.json"
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": mode,
        "sources": list(sources),
        "matched": [{**r, "chunk_ids": selected[(r["source"], r["title"])]} for r in matched_rows],
        "matched_chunks_total": len(all_ids),
        "flagged_out_of_scope": flagged_rows,
        "applied": False,
    }

    if not args.apply:
        payload_out = manifest
        manifest.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        log.info("DRY-RUN: %d chunk(s) would be purged. Manifest -> %s", len(all_ids), manifest)
        log.info("Review the matched titles above. To apply: STOP Cora + back up cora_kb.db, "
                 "then re-run with --apply.")
        if args.report:
            Path(args.report).write_text(json.dumps(payload, indent=1), encoding="utf-8")
        log.info("=" * 72)
        return 0

    if not all_ids:
        log.info("Nothing to purge. Exiting.")
        return 0

    log.warning("APPLY: purging %d chunk(s). Ensure Cora is STOPPED and cora_kb.db is BACKED UP.", len(all_ids))
    rw = kb_archive.connect_rw(db_path)
    try:
        totals = kb_archive.delete_chunks(rw, all_ids)
    finally:
        rw.close()
    payload["applied"] = True
    payload["deleted"] = totals
    manifest.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    if args.report:
        Path(args.report).write_text(json.dumps(payload, indent=1), encoding="utf-8")
    log.info("Deleted: %s", totals)
    log.info("Manifest -> %s", manifest)
    log.info("Reclaim disk (optional): .venv\\Scripts\\python.exe scripts\\reclaim_kb_space.py")
    log.info("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
