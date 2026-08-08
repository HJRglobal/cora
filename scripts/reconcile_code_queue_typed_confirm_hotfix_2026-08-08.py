#!/usr/bin/env python3
"""Step-7.5 queue reconciliation for the typed-confirm liveness HOTFIX
(branch claude/hotfix-typed-confirm-liveness).

Run AFTER Harrison FF-merges the branch. Idempotent; dry-run by default;
--apply to write.

Closed by this hotfix (2):
  * cq-be2289dfdc21 -- S1: the typed confirm/cancel path. TWO root causes, and
    NEITHER was the liveness/expiry evaluation the seed suspected: (a) the [QA]
    smoke marker is a content word, and _confirm_intent disqualifies a bare
    confirm on any content word, so every [QA]-tagged smoke of this path could
    never have passed; (b) blanket deferral of the six executor-less kinds
    swallowed typed CANCELs, which need no executor at all. stash_is_live() and
    turn_started_at were verified CORRECT at 1s / 45s / 7min and are now pinned.
  * cq-24cc6ac4bbc8 -- S2: a deferred turn now carries factual pending-state
    context, so the model can no longer assert "nothing is staged" over armed
    previews.

DELIBERATELY LEFT OPEN (1):
  * cq-68f2f3d8ef7f -- S3, calendar_create_event resolving relative datetimes at
    stash time. The kickoff scoped it "rider if cheap". It is not cheap: it
    changes what the calendar preview shows and when unparseable dates are
    refused, which is a behavioural change to a staged-write path and wants its
    own tests and review rather than a ride on a hotfix. Stays PROPOSED.

Standalone script -- does NOT import the bot process; no restart needed.
"""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cora import code_queue  # noqa: E402

SHIPS: list[tuple[str, str]] = [
    ("cq-be2289dfdc21", "S1: [QA] marker blinded the interceptor + cancels were deferred away"),
    ("cq-24cc6ac4bbc8", "S2: deferred turns carry pending-state context"),
]

LEFT_OPEN: list[tuple[str, str]] = [
    ("cq-68f2f3d8ef7f", "S3 calendar relative-datetime resolution -- not a cheap rider"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Actually write the shipped events (default: dry-run).")
    args = ap.parse_args()

    shipped = 0
    for cid, why in SHIPS:
        rec = code_queue.get_item(cid)
        if not rec:
            print(f"SKIP (missing id): {cid}")
            continue
        if rec.get("status") == "SHIPPED":
            print(f"SKIP (already shipped): {cid}")
            continue
        line = f"{cid} [{rec.get('status')}] '{rec.get('title', '')[:60]}'  <- {why}"
        if args.apply:
            code_queue.process_queue_action(
                code_queue.ACTION_MARK_SHIPPED, cid, code_queue.HARRISON_ID)
            print(f"SHIPPED: {line}")
            shipped += 1
        else:
            print(f"WOULD SHIP: {line}")

    print("\nLeft OPEN on purpose:")
    for cid, why in LEFT_OPEN:
        rec = code_queue.get_item(cid)
        status = rec.get("status") if rec else "missing"
        print(f"  {cid} [{status}] <- {why}")

    if args.apply:
        print(f"\nDone. Shipped {shipped}.")
        try:
            code_queue.render_backlog()
            print("Backlog regenerated.")
        except Exception as exc:  # noqa: BLE001
            print(f"(backlog render skipped: {exc})")
    else:
        print("\nDry-run. Re-run with --apply AFTER the branch is merged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
