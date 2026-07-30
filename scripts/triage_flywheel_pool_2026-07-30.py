#!/usr/bin/env python3
"""STEP 0 -- one-time flywheel-conversion pool triage + T0 baseline.

Handoff: `_notes/2026-07-30_fndr_cora-code-prompt-flywheel-conversion-wave1.md`
STEP 0. The live open gap pool (logs/knowledge-gaps.jsonl, minus resolved/state
per gap_autofill.load_open_gaps) is untriaged, so Fork 5's conversion-of-eligible
metric has no labeled denominator to compare against at the 2-week review. This
is a ONE-TIME hand triage of the pool AS OF 2026-07-30 (verified live: exactly 13
open gaps, ts-for-ts matching the handoff's table) -- not an ongoing classifier.
Any gap that arrives AFTER this snapshot and isn't in _KNOWN_DISPOSITIONS below is
classified by a best-effort fallback heuristic (LEX entity -> walled-permanent;
knowledge_gaps.is_capability_ask -> capability; else -> eligible) and flagged
UNLISTED so a human can sanity-check the fallback call.

Writes the T0 baseline snapshot to data/state/flywheel-t0-baseline.json:
eligible-open count, per-disposition counts, and the per-lane conversion counts
(all ~0 at T0 by design -- Wave 1 is measurement-integrity, not a throughput
win) so the 2-week review has a clean before/after comparison.

Dry-run by default (prints the triage table + computed baseline, writes nothing).
Pass --write to persist the baseline snapshot.

ORDERING (D-051 adversarial review finding): run this script with --write
BEFORE running scripts/purge_lex_capability_gap_rows_2026-07-30.py --apply.
The purge script's mark_resolved effect removes 3 of these 13 gaps from
gap_autofill.load_open_gaps(), so running it first would make this script
snapshot 10 open gaps instead of 13 -- understating the T0 baseline the
2-week review compares against.

Usage:
    python scripts/triage_flywheel_pool_2026-07-30.py            # dry-run
    python scripts/triage_flywheel_pool_2026-07-30.py --write     # persist baseline
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import gap_autofill, knowledge_gaps  # noqa: E402

# Hand-triaged AS OF 2026-07-30 -- see the handoff doc's STEP 0 table. Keyed by the
# gap's exact `ts`. Do NOT extend this table for future gaps; the fallback
# heuristic below handles anything not captured here.
_KNOWN_DISPOSITIONS: dict[str, str] = {
    "2026-07-03T05:53:08.435531+00:00": "walled-permanent",   # FNDR finance (QBO pull)
    "2026-07-09T00:08:43.552523+00:00": "capability",         # HJRG receipt-find ask
    "2026-07-12T00:15:14.947226+00:00": "expire",             # FNDR DM-test
    "2026-07-28T14:11:54.037008+00:00": "capability",         # FNDR RepRally ask
    "2026-07-28T15:46:38.407831+00:00": "walled-permanent",   # LEX DDD service types
    "2026-07-28T15:52:01.337625+00:00": "expire",             # LEX meta/junk
    "2026-07-28T20:35:19.157162+00:00": "walled-permanent",   # LEX RSP/HNT world-question
    "2026-07-29T19:07:45.301614+00:00": "walled-permanent",   # LEX-LLC HR separation
    "2026-07-29T23:28:40.131221+00:00": "walled-permanent",   # OSN monthly deposits
    "2026-07-29T23:28:41.678718+00:00": "walled-permanent",   # OSN monthly deposits (dup)
    "2026-07-30T04:58:49.176210+00:00": "capability",         # LEX how-to-guide ask 1
    "2026-07-30T05:01:15.827097+00:00": "capability",         # LEX how-to-guide ask 2
    "2026-07-30T05:24:01.404247+00:00": "capability",         # LEX how-to-guide ask 3
}

_BASELINE_PATH = _REPO_ROOT / "data" / "state" / "flywheel-t0-baseline.json"


def _fallback_disposition(gap: dict) -> str:
    entity = (gap.get("entity") or "FNDR").strip().upper()
    if entity.startswith("LEX"):
        return "walled-permanent"
    if knowledge_gaps.is_capability_ask(gap.get("question") or ""):
        return "capability"
    return "eligible"


def triage() -> dict:
    gaps = gap_autofill.load_open_gaps()
    rows = []
    counts: dict[str, int] = {}
    for g in gaps:
        ts = g.get("ts", "")
        known = ts in _KNOWN_DISPOSITIONS
        disposition = _KNOWN_DISPOSITIONS.get(ts) or _fallback_disposition(g)
        counts[disposition] = counts.get(disposition, 0) + 1
        rows.append({
            "ts": ts, "entity": g.get("entity", "?"),
            "disposition": disposition, "unlisted": not known,
        })
    eligible = counts.get("eligible", 0)
    baseline = {
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "eligible_open_count": eligible,
        "total_open_count": len(gaps),
        "disposition_counts": counts,
        # T0 conversion counts -- all ~0 by design (Wave 1 is measurement-integrity,
        # not a throughput win; see the handoff doc section 0 framing). The 2-week
        # review compares flywheel_metrics.collect()'s live per-lane counts against
        # THESE zeros, not against this script re-run.
        "conversions_by_lane_t0": {
            "known_answer_mined": 0, "known_answer_escalation_asker": 0,
            "friction_efficiency": 0, "decision_staged": 0,
        },
        "code_queue_capability_routed_t0": 0,
    }
    return {"rows": rows, "baseline": baseline}


def main() -> int:
    ap = argparse.ArgumentParser(description="STEP 0 flywheel pool triage + T0 baseline.")
    ap.add_argument("--write", action="store_true",
                    help="Persist the T0 baseline (default: dry-run print only).")
    args = ap.parse_args()

    result = triage()
    print(f"{'ts':32} {'entity':10} {'disposition':18} unlisted")
    for r in result["rows"]:
        print(f"{r['ts']:32} {r['entity']:10} {r['disposition']:18} "
              f"{'YES -- review' if r['unlisted'] else ''}")
    print()
    print("Disposition counts:", result["baseline"]["disposition_counts"])
    print("Eligible-open count (T0 denominator):", result["baseline"]["eligible_open_count"])

    if args.write:
        _BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _BASELINE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(result["baseline"], indent=2), encoding="utf-8")
        tmp.replace(_BASELINE_PATH)
        print(f"\nWrote T0 baseline -> {_BASELINE_PATH}")
    else:
        print("\nDry-run: re-run with --write to persist the T0 baseline.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
