"""Step 7.5 queue reconciliation for the 8/18 remodel-fixes bundle.

Transitions ONLY the seeds this branch actually closed. Dry-run by default;
`--apply` performs the writes through
``code_queue.process_queue_action(ACTION_MARK_SHIPPED, cq_id, HARRISON_ID)`` --
never by hand-editing the jsonl or the backlog (loop step 7.5).

WHY THREE OF THE SIX SEEDS ARE DELIBERATELY LEFT OPEN
----------------------------------------------------
Without this, the queue reads stale-positive and the Monday menu re-offers
shipped work (the 2026-07-31 incident: 14 D-095 seeds still PROPOSED after
merge). The inverse failure is just as bad and less visible: marking a seed
SHIPPED because *some* of it landed. Two of the items below shipped only their
investigative half, and one did not ship at all.

    SHIPPED
      cq-96adf03bcda3  QBO monthly-report populator          (slice 1, built)
      cq-e1d091eb6007  DW delivery integrity                 (slice 5, built)
      cq-b3a705ff10c9  Tessa registry-only -> active         (slice 6, built)

    LEFT OPEN ON PURPOSE
      cq-232fe6a541ff  decisions-lane delivery gap  -- the AUDIT is done and
                       written up, and it OVERTURNED the seed's own premise: all
                       five decisions are P2, and every Cora surfacing lane
                       hard-filters to P0/P1, so the "delivery verification for
                       aging P-decisions" this seed asks for would have stayed
                       green through every one of them. The mechanism that
                       actually catches the class (gate-date escalation at any
                       severity) is specified but NOT built.
      cq-44645e3f79a3  Klaviyo lane pivot -- diagnosis done and recorded (there
                       is no Cora-side 6-draft pipeline: no Klaviyo module, no
                       credential, no campaign-creating archetype; it could not
                       stall silently because nothing owned it). The template /
                       structured-prompt lane is NOT built and is blocked on a
                       Klaviyo API credential Harrison must provision.
      cq-f330d402e5cd  Justin decision-UX bundle -- NOT started.

Evidence of record:
  00-Founder/projects/review-org-remodel-alignment/
      2026-08-18_fndr_ops-finance-session-review-and-corrections.md
      2026-08-18_fndr_decisions-lane-delivery-audit.md
      2026-08-18_fndr_remodel-fixes-bundle-handoff.md
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
    "cq-96adf03bcda3": "QBO monthly-report populator (slice 1)",
    "cq-e1d091eb6007": "DW delivery integrity -- honest destination + post-write verify (slice 5)",
    "cq-b3a705ff10c9": "org-roles Tessa registry-only -> active + paired pins (slice 6)",
}

LEFT_OPEN: dict[str, str] = {
    "cq-232fe6a541ff": "audit done + premise overturned (all five are P2; lanes filter P0/P1) -- mechanism not built",
    "cq-44645e3f79a3": "diagnosis done -- template lane blocked on a Klaviyo credential",
    "cq-f330d402e5cd": "not started",
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Perform the transitions. Omitted = report only.")
    args = ap.parse_args(argv)

    print(f"Step 7.5 -- remodel-fixes bundle ({'APPLY' if args.apply else 'DRY-RUN'})\n")

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

    print("\n  Left OPEN on purpose (do NOT mark these shipped):")
    for cq_id, why in LEFT_OPEN.items():
        print(f"    {cq_id}  {why}")

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
