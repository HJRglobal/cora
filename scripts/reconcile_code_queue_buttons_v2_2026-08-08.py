#!/usr/bin/env python3
"""Step-7.5 queue reconciliation for the Slack confirm-buttons v2 bundle
(branch claude/slack-buttons-v2-coverage).

Run AFTER Harrison FF-merges the branch. Idempotent; dry-run by default;
--apply to write.

Closed by this bundle (8):
  * cq-db3b28dcdd42 -- S1: the dropped concurrent turn. ROOT CAUSE was the
    confirm interceptor abandoning a sibling turn's in-flight destructive Asana
    pending; fixed by scoping the arbitration with turn_started_at.
  * cq-fee6c9764950 -- S1: duplicate live cards + a typed cancel leaving buttons
    live. Fixed by the rendered-card registry (one-shot attach claim) and the
    stash_is_live sweep that closes a card terminated by ANY route.
  * cq-67490abe2d86 -- S3: a "remember that X" DM swallowed by the gap-ask
    capture. Fixed by gating allow_toplevel on the staged-write intent, plus the
    vocative-prefix repair without which the fix would not have matched the live
    phrasing.
  * cq-8063c3cee70f -- S3/S4: DW typed confirm ignored in a channel thread.
    CODE SIDE COMPLETE (honest preview copy via _confirm_how, with the button as
    the in-thread affordance). The remaining half is STRUCTURAL and not fixable
    from this repo: channel `message` events are not subscribed, so a bare
    in-thread channel reply never reaches the app. That is the Slack Event
    Subscriptions change (message.channels / message.groups + reinstall),
    already an open Harrison-side decision from the info-for-cora build.
  * cq-483109dfea11 -- S7: lexicon ask-on-ambiguity bypassed by the model's
    pre-resolver phrase rewriting. Fixed by judging ambiguity on the verbatim
    user text, with two new hard eval gates.
  * cq-8e2771423833 -- S8: a gid-only task-complete ask previewed the raw gid.
  * cq-2778868827ab -- S8: PREMISE OVERTURNED. The DW preview's quota and cost
    lines are already unconditional on every surface (verified live). Nothing to
    restore; pinned by regression tests so it stays true.
  * cq-2c5d864691fb -- S8: PREMISE OVERTURNED. delegated_level() already reads
    os.environ per call, so the TRIAL MODE label was never a process snapshot
    (verified live by flipping the variable between calls). The real residual --
    a .env FILE edit not reaching a running bot -- is the documented restart
    requirement for the kill switch, not a mislabel. Pinned with an AST drift
    guard against a future module-level snapshot.

DELIBERATELY LEFT OPEN (1):
  * cq-b5460ae7aca3 -- meeting_action_items per-item confirm cards. Part of
    slice S5 (the 7-tool Class-B stash-parity batch), which was NOT built in
    this session. Split to a follow-on branch per the scope's cut line; a
    partial slice is never shipped, so this seed stays PROPOSED.

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
    ("cq-db3b28dcdd42", "S1: concurrent-turn stash loss (interceptor scoped by turn_started_at)"),
    ("cq-fee6c9764950", "S1: one-stash-one-card + every terminal route closes it"),
    ("cq-67490abe2d86", "S3: a remember-command DM is no longer a gap answer"),
    ("cq-8063c3cee70f", "S3/S4: honest confirm copy + buttons (structural half is a Harrison decision)"),
    ("cq-483109dfea11", "S7: ambiguity judged on the verbatim user text"),
    ("cq-8e2771423833", "S8: gid-only ask previews the resolved task name"),
    ("cq-2778868827ab", "S8: premise overturned -- quota/cost already unconditional"),
    ("cq-2c5d864691fb", "S8: premise overturned -- level already read at call time"),
]

# Named so a reader of this file can see what was deliberately NOT closed.
LEFT_OPEN: list[tuple[str, str]] = [
    ("cq-b5460ae7aca3", "S5 meeting_action_items per-item cards -- deferred to the follow-on branch"),
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

    print("\nLeft OPEN on purpose (slice not built -- never ship a partial slice):")
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
