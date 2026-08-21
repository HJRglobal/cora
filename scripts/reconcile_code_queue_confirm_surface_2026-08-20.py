"""Step 7.5 queue reconciliation for the 8/20 confirm-surface + queue-split bundle.

Transitions ONLY the seeds this branch actually closed. Dry-run by default;
``--apply`` performs the writes through
``code_queue.process_queue_action(ACTION_MARK_SHIPPED, cq_id, HARRISON_ID)`` and
``code_queue.supersede_item(...)`` -- never by hand-editing the jsonl or the
backlog (loop step 7.5).

Without this the queue reads stale-positive and the Monday menu re-offers
shipped work (the 2026-07-31 incident: 14 D-095 seeds still PROPOSED after
merge). The inverse failure is quieter and just as bad -- marking a seed SHIPPED
because some of it landed -- so the LEFT_OPEN block below is as load-bearing as
the SHIPPED one.

    SHIPPED
      cq-236fd0310eb8  founder-DM confirm surface (slice 1)
      cq-6b014816819c  knowledge-review queue split (slice 2)
      cq-12bd309c93a8  purged-NDA hard-block ordering (slice 3)

    SUPERSEDED
      cq-25db72b0a5cb  the withheld original of the NDA seed -- restated in full
                       by cq-12bd309c93a8, which is the one that shipped.

    LEFT OPEN ON PURPOSE
      cq-7fa883cb2220  LEX maintenance-Airtable read lane. The slice was GATED on
                       Shaun sharing the maintenance-requests base to
                       harrison@hjrglobal.com. Checked live against the PAT on
                       2026-08-20: eight bases are visible and none is a
                       maintenance base. "Lexington Matching (MVP)"
                       (appXMvvHHp1dpG73u) was ruled out by probing its tables --
                       Providers / Provider Availability / Members / Service
                       Requests / Match Review, i.e. the caregiver-matching MVP.
                       Per D-205 no schema was guessed and nothing was built.
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
    "cq-236fd0310eb8": "founder-DM confirm surface -- mention strip + deterministic remember/forget confirm (slice 1)",
    "cq-6b014816819c": "knowledge-review queue split -- mechanical lane + escalation instead of silent expiry (slice 2)",
    "cq-12bd309c93a8": "purged-NDA hard block now fires across the LEX family and stops naming a channel (slice 3)",
}

SUPERSEDED: dict[str, tuple[str, str]] = {
    "cq-25db72b0a5cb": ("cq-12bd309c93a8", "withheld original of the NDA guard seed"),
}

LEFT_OPEN: dict[str, str] = {
    "cq-7fa883cb2220": "gate not met -- no maintenance base is visible to the Airtable PAT (checked live 8/20); nothing built against a guessed schema",
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Perform the transitions. Omitted = report only.")
    args = ap.parse_args(argv)

    print(f"Step 7.5 -- confirm-surface + queue-split bundle "
          f"({'APPLY' if args.apply else 'DRY-RUN'})\n")

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

    for loser, (winner, why) in SUPERSEDED.items():
        if not args.apply:
            print(f"  [dry-run] would SUPERSEDE     {loser} by {winner}  ({why})")
            continue
        try:
            ok = code_queue.supersede_item(loser, winner)
            print(f"  SUPERSEDED {loser} by {winner}  ({why})  -> {ok}")
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED     {loser}  -> {type(exc).__name__}: {exc}")
            rc = 1

    print("\n  Left OPEN on purpose (do NOT mark these shipped):")
    for cq_id, why in LEFT_OPEN.items():
        print(f"    {cq_id}  {why}")

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
