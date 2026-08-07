"""Standing-loop STEP 7.5 for the #info-for-cora intake bundle: mark SHIPPED the
queue seed this branch closes.

Run at (or immediately after) the fast-forward merge of
`claude/info-for-cora-intake-2026-08-06`. Without it the queue reads stale-positive
and both the Monday menu and the next Code session re-offer work that is already
shipped (the 2026-07-31 incident: 14 D-095 seeds still PROPOSED post-merge).

Transitions via code_queue.process_queue_action(ACTION_MARK_SHIPPED, ...) only --
never a hand-edit of data/state/code-session-queue.jsonl or the generated backlog .md.

DELIBERATELY NOT SHIPPED here:
- cq-86c283d95a34 (F3E ecom source-opacity) is RED on main, pre-existing, and
  untouched by this work.
- No OTHER seed is claimed. The D-104 [QA] quarantine shipped here as the rider the
  bundle kickoff assigned it; it has no separate queue id, so nothing else to close.

HISTORY on cq-4e39d9f0f994 (now INCLUDED): this script briefly carried one item
because R9 stopped at its own gate -- the uncommitted org-roles.yaml held four
role/entity/manager changes beyond what the kickoff named, and the expected "Jerry
mapped" edit appeared absent. Cowork verified all four against founder canon on
2026-08-06 (Eric -> AP-Finance under Justin, Jen -> HCBS Director, Aaron -> Day
Programs Director per TOM 1yyyy; Alina -> HJRG HR/Payroll per TOM 1ssss + 1yyyy)
and established that Jerry's entry already existed at HEAD, so nothing was ever
pending for him. Harrison cleared R9 the same day and the roster landed with the
test-pin fix, so the seed is closed here.

VERIFY BEFORE APPLYING: this branch does NOT fully close the reported symptom on its
own. The @mention route and the reconciling sweep work today, but the original
event-driven path stays dark until the Slack app's Event Subscriptions gain
message.groups (see the cascade report's Harrison execution list). Marking the seed
SHIPPED is still correct -- the buildable half is complete and the remainder is a
Slack app-config action, not code -- but do not read SHIPPED as "the event path now
fires".

Usage (dry-run is the default):
    .venv\\Scripts\\python.exe scripts\\reconcile_code_queue_info_for_cora_intake_2026-08-06.py
    .venv\\Scripts\\python.exe scripts\\reconcile_code_queue_info_for_cora_intake_2026-08-06.py --apply
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import code_queue  # noqa: E402

# (cq_id, what closed it)
SHIPPED = [
    ("cq-f1236540b61e",
     "S1-S4 -- shared info_intake chokepoint + @mention route + kept message-event "
     "route + reconciling sweep, all on one idempotent infocora-{ts} id"),
    ("cq-4e39d9f0f994",
     "R9 -- Harrison-approved org-roles roster committed (Eric/Jen/Aaron/Alina role "
     "corrections + Brei Pebley) with the stale Alina manager test pin fixed in the "
     "same commit; both identity tests green"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="actually write (default: dry run)")
    args = ap.parse_args()
    dry = not args.apply
    print(f"{'[DRY]' if dry else '[APPLY]'} step 7.5 -- #info-for-cora intake bundle\n")

    rc = 0
    for cq_id, note in SHIPPED:
        rec = code_queue.get_item(cq_id)
        if not rec:
            print(f"  {cq_id}: MISSING -- skipped (investigate before merging)")
            rc = 1
            continue
        status = rec.get("status")
        if status == "SHIPPED":
            print(f"  {cq_id}: already SHIPPED")
            continue
        if dry:
            print(f"  {cq_id}: {status} -> would mark SHIPPED  ({note})")
            continue
        outcome, msg = code_queue.process_queue_action(
            code_queue.ACTION_MARK_SHIPPED, cq_id, code_queue.HARRISON_ID)
        print(f"  {cq_id}: {status} -> {outcome}: {msg}  ({note})")

    print(f"\n{'Re-run with --apply to write.' if dry else 'done.'}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
