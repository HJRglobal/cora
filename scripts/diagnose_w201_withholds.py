#!/usr/bin/env python3
r"""Read-only diagnostic: which non-LEX chunks the W2-01 PHI backstop withholds.

cq-69ffc4b44bf6 reported two FNDR-tagged chunks tripping the withhold on every
retrieval and asked for a retag-or-purge. Run this before acting on that class --
BOTH halves of the premise turned out to be wrong on 2026-08-19, and the numbers
are the argument:

  1. THE TWO NAMED CHUNKS ARE NOT MIS-TAGGED. 012af994 is "Harrison's Fireflies
     Tracker" (a drive_sweep of Harrison's own action-items doc) and 1c2e0700 is
     2026-08-18_fndr_RESUME-PROMPT-feature-priorities-post-approval-tail.md -- the
     kickoff note for this very session. Both are correctly FNDR. They trip
     because they DISCUSS LEX operations ("community outings", "submit a
     maintenance request", "LEX read lanes"), not because they carry client PHI.
     Retagging them into LEX would file founder documents as clinical records;
     purging them would delete legitimate founder knowledge.
  2. THE POPULATION IS NOT TWO. It is ~2,384 of 370,193 non-LEX chunks (0.64%),
     and a large share are genuinely LEX-shaped content sitting in a non-LEX
     partition -- "Client Attendance Logs (Autism Academy)", "kuska autism
     services lexington aba utah" -- i.e. the guard doing exactly its job.

So the residual is a RECALL cost on founder content that talks about LEX, not a
tagging defect. Narrowing a PHI backstop is a decision with its own session:
D-051's standing lesson on this class is to measure what a precision fix STOPS
catching, across the whole corpus, in every consumer -- which is what this script
exists to make cheap.

READ-ONLY. Opens the KB with mode=ro and writes nothing.

Usage:
    .venv\Scripts\python.exe scripts\diagnose_w201_withholds.py
    .venv\Scripts\python.exe scripts\diagnose_w201_withholds.py --entity FNDR --show 40
    .venv\Scripts\python.exe scripts\diagnose_w201_withholds.py --chunk 012af994-...
"""

from __future__ import annotations

import argparse
import collections
import sqlite3
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import org_roles, phi_guard  # noqa: E402

KB_DB_PATH = _REPO_ROOT / "data" / "cora_kb.db"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(KB_DB_PATH))
    ap.add_argument("--entity", help="limit to one entity (e.g. FNDR)")
    ap.add_argument("--chunk", action="append", default=None,
                    help="explain a specific chunk_id (repeatable)")
    ap.add_argument("--show", type=int, default=15, help="rows to list (default 15)")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"KB DB not found: {db}")
        return 1
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.execute("PRAGMA busy_timeout=30000")

    # The live backstop compares against the staff roster, so the diagnostic must
    # use the same allowed_names or its verdicts are not the live verdicts.
    try:
        staff = {r.name for r in org_roles.all_roles() if getattr(r, "name", "")}
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: roster unavailable ({exc}) -- verdicts will over-trip")
        staff = set()

    if args.chunk:
        for cid in args.chunk:
            row = conn.execute(
                "SELECT entity, sub_entity, source, title, content FROM knowledge_chunks "
                "WHERE chunk_id=?", (cid,)).fetchone()
            print("=" * 20, cid)
            if not row:
                print("  NOT FOUND")
                continue
            entity, sub, source, title, content = row
            trips = phi_guard.non_lex_phi_backstop_trips_live(
                content or "", allowed_names=staff)
            print(f"  entity={entity} sub_entity={sub} source={source}")
            print(f"  title={title}")
            print(f"  W2-01 withholds: {trips}")
            print(f"  content[:400]={(content or '')[:400]!r}")
        return 0

    sql = ("SELECT chunk_id, entity, source, title, content FROM knowledge_chunks "
           "WHERE entity NOT LIKE 'LEX%'")
    params: tuple = ()
    if args.entity:
        sql += " AND entity = ?"
        params = (args.entity,)
    rows = conn.execute(sql, params).fetchall()

    withheld: list[tuple[str, str, str, str]] = []
    errors = 0
    for cid, entity, source, title, content in rows:
        try:
            if phi_guard.non_lex_phi_backstop_trips_live(
                    content or "", allowed_names=staff):
                withheld.append((cid, entity, source, title or ""))
        except Exception:  # noqa: BLE001 -- the live path withholds on error too
            errors += 1
            withheld.append((cid, entity, source, f"(predicate error) {title or ''}"))

    total = len(rows)
    pct = (100.0 * len(withheld) / total) if total else 0.0
    print(f"non-LEX chunks scanned: {total}")
    print(f"withheld by W2-01:      {len(withheld)} ({pct:.2f}%)"
          + (f", {errors} via predicate error (fail-closed)" if errors else ""))
    print("by entity: " + ", ".join(
        f"{k} {v}" for k, v in collections.Counter(e for _, e, _, _ in withheld).most_common()))
    print("by source: " + ", ".join(
        f"{k} {v}" for k, v in collections.Counter(s for _, _, s, _ in withheld).most_common()))
    print("\nsample:")
    for cid, entity, source, title in withheld[:args.show]:
        print(f"  {entity:8s} {source:12s} {cid[:8]} {title[:70]}")
    print("\nREAD-ONLY -- nothing was written. A retag or purge on this class needs "
          "a decision, not a sweep: see this file's header.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
