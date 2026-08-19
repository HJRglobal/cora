#!/usr/bin/env python3
"""Retro-tag already-ingested fireflies chunks whose diarization collapsed (cq-e63feff3a0bf).

Forward detection ships in the connector (fireflies_diarization.assess at ingest).
This is the catch-up pass over the corpus already in the KB -- including the
77-minute meeting that started the whole thing.

IT NEEDS NO FIREFLIES CALL. The ingest formatter writes speaker labels into the
stored content as `[Speaker] text` lines, so the same judgement runs offline over
exactly the text retrieval will hand the model. No API, no re-embedding.

Grouping: by BARE source_id (the transcript id) across all of a meeting's chunks,
because the collapse is a property of the MEETING, not of one chunk -- a chunk
that happens to hold a single person's monologue is not evidence of anything
(chunk-family doctrine, 2026-08-01).

Party count comes from the stored `attendee_emails` / `participants` metadata,
NEVER from the speaker labels under test. A meeting whose metadata shows fewer
than 2 known parties is left alone: single-speaker is the correct reading.

Tag-don't-drop: metadata-only UPDATE on knowledge_chunks. entity / sub_entity /
content untouched, no vec-table work (metadata is not embedded; the D-046a
in-place re-tag precedent).

DRY-RUN BY DEFAULT. --apply performs the UPDATEs (Harrison-gated: KB write).
Idempotent -- rows already carrying the flag are skipped.

Standalone script -- does NOT import the bot process; no restart needed.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora.connectors import fireflies_diarization as fd  # noqa: E402

KB_DB_PATH = _REPO_ROOT / "data" / "cora_kb.db"
_BATCH = 500


def _meta(raw: str | None) -> dict:
    try:
        parsed = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parties(metas: list[dict]) -> int:
    """Distinct known attendees across a meeting's chunks (metadata, not labels)."""
    seen: set[str] = set()
    for meta in metas:
        participants = meta.get("participants") or []
        if isinstance(participants, str):
            participants = [participants]
        for value in list(meta.get("attendee_emails") or []) + list(participants):
            text = str(value or "").strip().lower()
            # The notetaker bot rides in both lists; counting it makes a genuine
            # solo recording look multi-party and false-flags it. Same predicate
            # the canary uses, so the two cannot disagree (D-051 lens-2).
            if text and fd._is_human_address(text):
                seen.add(text)
    return len(seen)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Perform the metadata UPDATEs (default: dry-run report only)")
    parser.add_argument("--db", default=str(KB_DB_PATH))
    parser.add_argument("--show", type=int, default=15,
                        help="How many flagged meetings to list (default 15)")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"KB DB not found: {db_path}")
        return 1

    conn = sqlite3.connect(f"file:{db_path}?mode={'rw' if args.apply else 'ro'}", uri=True)
    conn.execute("PRAGMA busy_timeout=30000")

    rows = conn.execute(
        "SELECT chunk_id, source_id, title, entity, content, metadata "
        "FROM knowledge_chunks WHERE source='fireflies'"
    ).fetchall()

    # Group every chunk of one transcript together. Legacy rows may carry a
    # ':chunkN' suffix on source_id; strip it so both schemes fold to the meeting.
    meetings: dict[str, list[tuple]] = defaultdict(list)
    for row in rows:
        source_id = str(row[1] or "")
        meetings[source_id.split(":chunk")[0] or source_id].append(row)

    updates: list[tuple[str, str]] = []
    flagged: list[tuple[str, str, object]] = []
    ledger_rows: list[tuple[str, str, str, object]] = []
    stats = {"chunks": len(rows), "meetings": len(meetings), "collapsed": 0,
             "healthy": 0, "too_short": 0, "single_party": 0, "already": 0}

    for source_id, chunk_rows in meetings.items():
        metas = [_meta(r[5]) for r in chunk_rows]
        content = "\n".join(str(r[4] or "") for r in chunk_rows)
        health = fd.assess_rendered(content, _parties(metas))
        if not health.collapsed:
            if health.sentences < fd.MIN_SENTENCES:
                stats["too_short"] += 1
            elif health.expected_parties < 2:
                stats["single_party"] += 1
            else:
                stats["healthy"] += 1
            continue
        stats["collapsed"] += 1
        title = str(chunk_rows[0][2] or source_id)
        entity = str(chunk_rows[0][3] or "")
        flagged.append((title, health.reason, len(chunk_rows)))
        ledger_rows.append((source_id, title, entity, health))
        for row, meta in zip(chunk_rows, metas):
            if meta.get("attribution_unreliable") is True:
                stats["already"] += 1
                continue
            meta.update(health.as_metadata())
            updates.append((json.dumps(meta), row[0]))

    print(f"fireflies chunks scanned: {stats['chunks']} across {stats['meetings']} meetings")
    print(f"  collapsed (to flag):    {stats['collapsed']} meetings")
    print(f"  healthy:                {stats['healthy']}")
    print(f"  below sentence floor:   {stats['too_short']} (not evidence either way)")
    print(f"  <2 known parties:       {stats['single_party']} (single-speaker expected)")
    print(f"  chunks already flagged: {stats['already']}")
    print(f"  chunk rows to UPDATE:   {len(updates)}")

    if flagged:
        print("\nFlagged meetings:")
        for title, reason, n_chunks in flagged[:args.show]:
            print(f"  - {title[:90]} ({n_chunks} chunks) -- {reason}")
        if len(flagged) > args.show:
            print(f"  ...and {len(flagged) - args.show} more")

    if not args.apply:
        print("\nDRY RUN -- no writes. Re-run with --apply to tag.")
        conn.close()
        return 0

    for i in range(0, len(updates), _BATCH):
        conn.executemany(
            "UPDATE knowledge_chunks SET metadata=? WHERE chunk_id=?",
            updates[i:i + _BATCH])
        conn.commit()
    conn.close()
    # The weekly coverage digest reads the FLAG LEDGER, not chunk metadata, so a
    # metadata-only sweep would tag these 126 meetings and the digest would never
    # mention one of them -- while the commit implied the digest was the surface
    # for this class (D-051 lens-5). Write both.
    recorded = 0
    for source_id, title, entity, health in ledger_rows:
        try:
            from cora.connectors.fireflies_connector import _record_diarization_flag
            _record_diarization_flag(source_id, title, entity, None, health)
            recorded += 1
        except Exception as exc:  # noqa: BLE001 -- the tagging is the primary write
            print(f"  ledger row failed for {source_id}: {exc}")
    print(f"\nAPPLIED: {len(updates)} chunk rows flagged attribution-unreliable; "
          f"{recorded} flag(s) recorded for the weekly coverage digest.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
