"""Step 7.5 queue reconciliation for the 13WCF M3 bundle (worksheet v2).

Transitions ONLY the seed this branch actually closed. Dry-run by default;
`--apply` performs the write through
``code_queue.process_queue_action(ACTION_MARK_SHIPPED, cq_id, HARRISON_ID)`` --
never by hand-editing the jsonl or the backlog (loop step 7.5).

Without this the queue reads stale-positive and the Monday menu re-offers
shipped work (the 2026-07-31 incident: 14 D-095 seeds still PROPOSED after
merge).

    SHIPPED
      cq-ac5697bfcfd8  13WCF M3 worksheet v2 -- inputs cleared 8/18, fire the
                       build. Both cleared inputs are IN the merge: the LEX realm
                       is declared as the Lexington LLC company file (CF_LLC,
                       scope_attested, confirmed still Justin's), and carry-in
                       stays bank-sourced / Justin-entered with the QBO-substitute
                       PROPOSED (with its ~1-day feed-lag caveat) rather than
                       decided.

    LEFT QUEUED ON PURPOSE -- adjacent, not closed by this branch
      cq-f3bfa4e9ca5b  BILL Spend & Expense (Divvy) card-spend pull. M3 now names
                       it explicitly as one of the two ways to UNBLOCK the flip
                       gate: the sheet is Cash/CC and QBO's perimeter is bank-only,
                       and v1 cannot decompose the difference. Naming a seed as the
                       fix for a blocked metric is not shipping it.
      cq-2ff81156f53a  SA Drive-metadata 403. M3 keys no freshness signal on
                       `modifiedTime`, same as M1 and M2, so nothing here needed it.
      cq-6290cf5c1a4d  The two flagged BDM/HJRP tie-out discrepancies from the 8/7
                       Justin walkthrough. M3's parallel section will SURFACE
                       discrepancies of that shape once the entity map is confirmed,
                       but it investigates none of them.
      cq-d706457326e6  Midweek close-pack run logs (8/11, 8/14) -- cadence question,
                       untouched here.

Evidence of record:
  _shared/projects/cora/2026-08-18_fndr_13wcf-M3-CASCADE-REPORT.md
  _shared/projects/cora/_notes/2026-08-05_fndr_cora-code-prompt-13wcf-shadow-ledger.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO_ROOT / ".env", override=True)

from cora import code_queue  # noqa: E402

HARRISON_ID = "U0B2RM2JYJ1"

SHIPPED: dict[str, str] = {
    "cq-ac5697bfcfd8": "13WCF M3 worksheet v2 + forecast_assist supersession + cashflow_parallel",
}

LEFT_QUEUED: dict[str, str] = {
    "cq-f3bfa4e9ca5b": "BILL/Divvy card spend -- named as the flip-gate unblock, not built",
    "cq-2ff81156f53a": "SA Drive-metadata 403 -- M3 needs no modifiedTime signal",
    "cq-6290cf5c1a4d": "BDM/HJRP tie-out discrepancies -- M3 would surface the shape, investigates none",
    "cq-d706457326e6": "midweek close-pack cadence -- untouched",
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Perform the transitions. Omitted = report only.")
    args = ap.parse_args(argv)

    print(f"Step 7.5 -- 13WCF M3 bundle ({'APPLY' if args.apply else 'DRY-RUN'})\n")

    rc = 0
    for cq_id, label in SHIPPED.items():
        if not args.apply:
            print(f"  [dry-run] would mark SHIPPED  {cq_id}  {label}")
            continue
        try:
            result = code_queue.process_queue_action(
                code_queue.ACTION_MARK_SHIPPED, cq_id, HARRISON_ID)
            print(f"  SHIPPED  {cq_id}  {label}  -> {result}")
        except Exception as exc:  # noqa: BLE001 -- one bad id must not abort the rest
            print(f"  FAILED   {cq_id}  {label}  -> {type(exc).__name__}: {exc}")
            rc = 1

    print("\n  Left QUEUED on purpose (do NOT mark these shipped):")
    for cq_id, why in LEFT_QUEUED.items():
        print(f"    {cq_id}  {why}")

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
