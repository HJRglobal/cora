#!/usr/bin/env python3
"""Step-7.5 queue reconciliation for the integrity-rails bundle
(branch claude/integrity-rails-2026-08-30, Cora Code session #11).

Run AFTER Harrison FF-merges the branch. Idempotent; dry-run by default;
--apply to write. Never hand-edit the jsonl.

Without this the queue reads stale-positive and the Monday menu re-offers
shipped work (the 2026-07-31 incident: 14 seeds still PROPOSED post-merge).

TWO SEEDS ARE DELIBERATELY *NOT* MARKED SHIPPED -- see NOT_SHIPPED below. A
step-7.5 script that marks a seed shipped when the work was escalated rather
than done is exactly the stale-positive it exists to prevent, pointing the
other way.
"""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cora import code_queue  # noqa: E402

SHIPS: list[tuple[str, str]] = [
    ("cq-f87099a5dec5", "S1: post-seam sentinel scrub at the egress boundary "
                        "(APPROVED)"),
    ("cq-b75ff2802764", "S1: _NON_SENTINEL_WRITE_TOOLS 4->14, derived from the "
                        "registration point + the live phantom-guard defect fixed "
                        "in app.py's assume_confirm gate (APPROVED)"),
    ("cq-12270999d138", "S1: duplicate of cq-f87099a5dec5 -- absorbed"),
    ("cq-eba0861fc043", "S2: 20 conftest redirects + enumeration rail; purge "
                        "script STAGED for Harrison (HIGH)"),
    ("cq-b0e5bc37c41b", "S3: two-tier staleness on all three read views + the "
                        "contributed-note quality floor"),
    ("cq-a251dee3f5cf", "S4: run-marker contract -- fired-no-output and "
                        "missed-fire now both alarm"),
    ("cq-f7ec95e2d313", "S6: health-check reporting fixes (health-ping class)"),
    ("cq-b2dee156caee", "S6: \\bFATAL\\b vs 'non-fatal', ERROR-volume line count, "
                        "fabricated recurrence claim"),
    ("cq-85b35413b020", "S7: preflight FP fix -- R2 proper-noun + R1 negation "
                        "scope (HIGH)"),
    ("cq-ebe18d20a949", "S8: attribution_unreliable made readable, honoured in "
                        "weight AND in the quoted evidence"),
    ("cq-c2eb2979e793", "S8: Klaviyo 429 backoff + honest throttled rendering"),
    ("cq-38faa8bd62a1", "S9: 'That that ... expired' fixed at the consumer"),
    ("cq-bd286f89b357", "S9: uptime parser no longer concatenates the pid "
                        "(APPROVED)"),
]

# Seeds this session touched but did NOT close. Printed, never transitioned.
NOT_SHIPPED: list[tuple[str, str]] = [
    ("cq-db780fcf7889",
     "S7 DATA half NOT BUILT -- structurally out of scope. No body-edit function "
     "exists (articleUpdate appears once, hard-wired to isPublished), process_tap "
     "short-circuits already_live on any published article, and cards can only be "
     "minted from list_unpublished(). Building it needs a NEW external write "
     "surface, which this session's own scope forbids. Stays PROPOSED."),
    ("cq-d54408d334d7",
     "S7 DATA half NOT BUILT -- same reason. The seed's own subsystem_guess reads "
     "'content ops (Cowork session, Shopify articleUpdate) - not a Cora code "
     "change'. Stays PROPOSED."),
    ("cq-bbdc206a097c",
     "S9 item 3 DIAGNOSED, NOT FIXED (APPROVED seed). The finding is that this "
     "seed is ITSELF an artifact: all four recent 'thumbsdown' signals are :-1: "
     "taps on cards whose own text reads ':+1: do it - :-1: dismiss'. The counter "
     "measures a CONTROL, not dissatisfaction. A fix needs a dm_message_ts "
     "discriminator so genuine thumbs-down is not suppressed; Harrison should "
     "decide whether to build it or DISMISS this seed as self-generated."),
    ("cq-c1abf007effd", "S9 item 1 (Gmail failure payload) NOT BUILT -- stretch, "
                        "ran out of session. Stays PROPOSED."),
    ("cq-a296aa8e0a2e", "S9 item 4 (repo-docs-sync + DR manifest) NOT BUILT -- "
                        "stretch, ran out of session. Stays PROPOSED."),
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
        line = f"{cid} [{rec.get('status')}] '{rec.get('title', '')[:56]}'  <- {why}"
        if args.apply:
            code_queue.process_queue_action(
                code_queue.ACTION_MARK_SHIPPED, cid, code_queue.HARRISON_ID)
            print(f"SHIPPED: {line}")
            shipped += 1
        else:
            print(f"WOULD SHIP: {line}")

    print("\n-- deliberately NOT transitioned --")
    for cid, why in NOT_SHIPPED:
        rec = code_queue.get_item(cid)
        status = rec.get("status") if rec else "MISSING"
        print(f"LEFT OPEN [{status}]: {cid}\n    {why}")

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
