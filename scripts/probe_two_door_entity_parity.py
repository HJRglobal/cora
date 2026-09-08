#!/usr/bin/env python3
"""Read-only probe: do the two ingest doors agree on the entity of each
session-capture file? (ingest-integrity bundle I5, cq-12fd5d76fd04, 2026-09-08)

The harvester writes ``<entity>/_session-captures/YYYY-MM/<file>.md``. The
static_md door keys the entity on the FOLDER (deterministic). The drive_sweep
door had TWO writers for the same file id: ``sweep_founders_os`` (folder-keyed,
agrees) and the flat per-user sweep of Harrison's Drive (Haiku-keyed, so a file
under 08-Lexington-Services could be tagged FNDR or F3E). Both write
``source="drive_sweep"`` with the same ``source_id`` (the Drive file id), so the
stored entity was whichever writer ran LAST -- measured 2026-09-08: 64 of 411
capture files present through both doors disagreed (30 LEX-vs-FNDR, 20
LEX-vs-F3E, ...). The fix (drive_sweep.sweep_user skips files the founders_os
sweep owns) stops new disagreements; the re-file script deletes the existing
disagreeing Drive rows. This probe is the acceptance check.

    .venv\\Scripts\\python.exe scripts\\probe_two_door_entity_parity.py
    .venv\\Scripts\\python.exe scripts\\probe_two_door_entity_parity.py --sample 20

Exit 0 when every file agrees, 1 when any disagreement remains (so a hygiene
sweep can call it as a check). Prints filenames + entities only -- never content.
"""
from __future__ import annotations

import argparse
import collections
import re
import sqlite3
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HARVESTER_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_(?:code-session|cowork-session)_[0-9a-f]{8}\.md$")


def measure(conn: sqlite3.Connection) -> dict:
    static: dict[str, set[str]] = {}
    for sid, ent in conn.execute(
        "SELECT source_id, entity FROM knowledge_chunks WHERE source='static_md' "
        "AND source_id LIKE '%_session-captures%'"
    ):
        base = str(sid).replace("\\", "/").rsplit("/", 1)[-1]
        if _HARVESTER_NAME_RE.match(base):
            static.setdefault(base, set()).add(str(ent))
    drive: dict[str, set[str]] = {}
    for title, ent in conn.execute(
        "SELECT title, entity FROM knowledge_chunks WHERE source IN ('drive_sweep','drive_asset') "
        "AND (title LIKE '%_code-session_%' OR title LIKE '%_cowork-session_%')"
    ):
        if _HARVESTER_NAME_RE.match(str(title or "")):
            drive.setdefault(str(title), set()).add(str(ent))
    both = sorted(set(static) & set(drive))
    disagreements = [(b, sorted(static[b]), sorted(drive[b])) for b in both if static[b] != drive[b]]
    pairs = collections.Counter((tuple(s), tuple(d)) for _b, s, d in disagreements)
    return {
        "static_files": len(static), "drive_files": len(drive), "both": len(both),
        "disagreements": disagreements, "pairs": pairs,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(_REPO_ROOT / "data" / "cora_kb.db"))
    ap.add_argument("--sample", type=int, default=20, help="How many disagreements to list (default 20).")
    args = ap.parse_args(argv)
    conn = sqlite3.connect(f"file:{Path(args.db).as_posix()}?mode=ro", uri=True)
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        m = measure(conn)
    finally:
        conn.close()
    print(f"capture files: static_md={m['static_files']} drive={m['drive_files']} both={m['both']} "
          f"DISAGREEMENTS={len(m['disagreements'])}")
    for (s, d), n in m["pairs"].most_common():
        print(f"  static {'/'.join(s):10} vs drive {'/'.join(d):12}: {n}")
    for b, s, d in m["disagreements"][: args.sample]:
        print(f"    {b}  static={'/'.join(s)} drive={'/'.join(d)}")
    return 1 if m["disagreements"] else 0


if __name__ == "__main__":
    sys.exit(main())
