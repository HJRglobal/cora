#!/usr/bin/env python3
"""Retro-tag existing slack KB chunks with bot-authorship metadata (cq-8d16969e85fb).

Forward tagging ships in scripts/incremental_sync_slack.py (metadata
bot_authored / has_cora_reply computed from the raw Slack message dicts at
ingest). This one-shot backfill classifies the ~4.4K ALREADY-INGESTED slack
chunks by parsing their serialized speaker lines ("[ts] <U0B44MDGC5R>: ...")
so miners exclude and retrieval labels the historical corpus too.

Tag-don't-drop (staged preference): metadata-only UPDATE on knowledge_chunks —
entity/sub_entity/content untouched, no re-embedding, no vec-table work
(metadata is not embedded; D-046a in-place re-tag precedent).

DRY-RUN BY DEFAULT. --apply performs the UPDATEs (Harrison-gated: KB write).
Idempotent — rows already carrying the computed flags are skipped.

Standalone script — does NOT import the bot process; no restart needed.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

KB_DB_PATH = _REPO_ROOT / "data" / "cora_kb.db"

# Cora's bot user id (the live workspace constant; matches
# completion_detector._BOT_AUTHOR_EXACT and the ingest-side fallback).
CORA_USER_ID = "U0B44MDGC5R"

# Speaker header as serialized by slack_connector.serialize_message:
#   "[2026-05-28 05:08 UTC] <U0B44MDGC5R>: text"
# In-text mentions render as <@U...> (with @) and are NOT matched.
_SPEAKER_RE = re.compile(r"<([UWB][A-Z0-9]{5,}|unknown)>:")

_BATCH = 500


def classify_chunk(content: str, cora_uid: str = CORA_USER_ID) -> dict[str, bool]:
    """Authorship flags for a serialized slack chunk, from its speaker lines.

    Mirrors incremental_sync_slack._bot_flags semantics:
      bot_authored  — every parsed speaker is an app (Cora or a B-prefixed
                      bot_id). "unknown" speakers count as NOT bot (conservative).
      has_cora_reply — at least one speaker is Cora.

    Chunks with no parseable speaker header (older raw-text continuation
    chunks) return {} — left untagged rather than guessed.
    """
    speakers = _SPEAKER_RE.findall(content or "")
    if not speakers:
        return {}
    flags: dict[str, bool] = {}
    if all(s == cora_uid or s.startswith("B") for s in speakers):
        flags["bot_authored"] = True
    if any(s == cora_uid for s in speakers):
        flags["has_cora_reply"] = True
    return flags


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Perform the metadata UPDATEs (default: dry-run report only)")
    parser.add_argument("--db", default=str(KB_DB_PATH))
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"KB DB not found: {db_path}")
        return 1

    mode = "rw" if args.apply else "ro"
    conn = sqlite3.connect(f"file:{db_path}?mode={mode}", uri=True)
    conn.execute("PRAGMA busy_timeout=30000")

    rows = conn.execute(
        "SELECT chunk_id, content, metadata FROM knowledge_chunks WHERE source='slack'"
    ).fetchall()

    updates: list[tuple[str, str]] = []
    stats = {"total": len(rows), "bot_only": 0, "mixed_cora": 0,
             "untaggable": 0, "already_tagged": 0, "human_only": 0}

    for chunk_id, content, metadata_json in rows:
        flags = classify_chunk(content or "")
        if not flags:
            if _SPEAKER_RE.findall(content or ""):
                stats["human_only"] += 1
            else:
                stats["untaggable"] += 1
            continue
        try:
            meta = json.loads(metadata_json) if metadata_json else {}
            if not isinstance(meta, dict):
                meta = {}
        except (json.JSONDecodeError, TypeError):
            meta = {}
        if all(meta.get(k) == v for k, v in flags.items()):
            stats["already_tagged"] += 1
            continue
        if flags.get("bot_authored"):
            stats["bot_only"] += 1
        else:
            stats["mixed_cora"] += 1
        meta.update(flags)
        updates.append((json.dumps(meta), chunk_id))

    print(f"slack chunks scanned:   {stats['total']}")
    print(f"  bot-only (to tag):    {stats['bot_only']}")
    print(f"  mixed w/ Cora reply:  {stats['mixed_cora']}")
    print(f"  human-only:           {stats['human_only']}")
    print(f"  untaggable (no hdrs): {stats['untaggable']}")
    print(f"  already tagged:       {stats['already_tagged']}")
    print(f"  rows to UPDATE:       {len(updates)}")

    if not args.apply:
        print("\nDRY RUN — no writes. Re-run with --apply to tag.")
        conn.close()
        return 0

    for i in range(0, len(updates), _BATCH):
        batch = updates[i:i + _BATCH]
        conn.executemany(
            "UPDATE knowledge_chunks SET metadata=? WHERE chunk_id=?", batch)
        conn.commit()
    conn.close()
    print(f"\nAPPLIED: {len(updates)} rows tagged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
