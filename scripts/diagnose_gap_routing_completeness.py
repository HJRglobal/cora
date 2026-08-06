"""READ-ONLY diagnostic: which gaps is gap_routing_completeness_7d calling rotting?

The flywheel monitor reports a COUNT ("52/57 routed, 5 rotting"). Harrison needs the
WHICH: a count that degrades tells you something broke but not what, and the 2026-08-05
sweep had to reconstruct the list by hand. This prints every unrouted gap with the
fields needed to classify it, plus a per-class tally.

Classification the 2026-08-06 run produced (6 rotting, up from 5 on 7/31):
  metric artifact  -- detector=="code_queue_route" rows, already dispositioned into
                      the code-session queue by the classifier. FIXED in this slice
                      (flywheel_metrics._has_own_disposition).
  LEX-origin       -- LEX gaps can never escalate (PHI wall) and mining rarely lands,
                      so they have no disposition path at all. That is the 8/13-locked
                      LEX-origin mining/escalation fork -- deliberately NOT fixed here.
  no-lane          -- DM personal-retrieval asks and QA/test noise. A durable
                      known-answer would be WRONG for these; they need a
                      "not-a-knowledge-gap" disposition, which is a design decision,
                      so it is seeded as a queue item instead of guessed at.

Usage:
    .venv\\Scripts\\python.exe scripts\\diagnose_gap_routing_completeness.py [--days 7]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import flywheel_metrics as fm  # noqa: E402


def _classify(rec: dict) -> str:
    detector = str(rec.get("detector", "") or "").strip().lower()
    entity = str(rec.get("entity", "") or "").strip().upper()
    if detector in fm._SELF_DISPOSITIONED_DETECTORS:
        return "metric-artifact (already routed to the code queue)"
    if entity.startswith("LEX"):
        return "LEX-origin (no disposition path -- 8/13-locked fork)"
    if rec.get("private_source") or str(rec.get("channel", "")).lower() == "dm":
        return "no-lane (DM personal-retrieval / test noise)"
    return "UNEXPLAINED -- investigate"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=7,
                   help="age threshold in days (default 7, matching the metric)")
    args = ap.parse_args()

    p = fm._paths(None)
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    resolved_ids = fm._resolved_ids_from(p["resolved_gaps"])
    state_ids = set(fm._json_object(p["gap_autofill_state"]).keys())

    total = routed = 0
    rotting: list[dict] = []
    for rec in fm._iter_jsonl(p["gaps_log"]):
        ts = fm._parse_iso(rec.get("ts") or "")
        if not ts or ts >= cutoff:
            continue
        total += 1
        gid = rec.get("ts", "")
        if gid in resolved_ids or gid in state_ids or fm._has_own_disposition(rec):
            routed += 1
        else:
            rotting.append(rec)

    print(f"gap routing-completeness (>{args.days}d old): {routed}/{total} routed, "
          f"{len(rotting)} rotting")
    print(f"  resolved-ledger ids: {len(resolved_ids)} | autofill-state ids: {len(state_ids)}")
    print()

    tally: dict[str, int] = {}
    for rec in rotting:
        klass = _classify(rec)
        tally[klass] = tally.get(klass, 0) + 1
        print(f"- {rec.get('ts')}  [{rec.get('entity')}/{rec.get('channel')}]  "
              f"detector={rec.get('detector')}")
        print(f"    class: {klass}")
        print(f"    q: {str(rec.get('question', ''))[:150]}")
        print(f"    gap: {str(rec.get('gap', ''))[:150]}")
    if not rotting:
        print("  (none)")
    print()
    print("By class:")
    for klass, n in sorted(tally.items(), key=lambda kv: -kv[1]):
        print(f"  {n}  {klass}")
    unexplained = tally.get("UNEXPLAINED -- investigate", 0)
    if unexplained:
        print(f"\n{unexplained} row(s) fit no known class -- worth a look.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
