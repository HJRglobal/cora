"""Step 7.5 -- reconcile the code-queue seeds closed by the round-3 clarity branch.

Loop step 7.5 exists because a merged branch that leaves its seeds PROPOSED reads
STALE-POSITIVE: sessions and the Monday menu re-offer shipped work (the 2026-07-31
incident: 14 D-095 seeds still PROPOSED post-merge). Never hand-edit the jsonl or
the backlog -- go through code_queue.process_queue_action so the ledger fold and
the backlog render stay consistent.

Deliberately uses ACTION_MARK_SHIPPED and NOT ACTION_APPROVE: approve on a
P0/P1-severity row calls ensure_kickoff_staged, which fires the Sonnet kickoff
generator and writes a staged prompt file. Generating a kickoff for work this
branch already delivered would burn a model call and leave a misleading staged
prompt behind. Harrison's approval is recorded in the session prompt; the
shipped transition is what the queue actually needs.

TWO of these five are marked shipped as PREMISE OVERTURNED, not as code fixes --
see the reason strings below and tests/test_slack_readback_semantics.py.

Dry-run by default. Run with --apply to write.

    .venv\\Scripts\\python.exe scripts\\reconcile_code_queue_clarity_round3_2026-08-17.py
    .venv\\Scripts\\python.exe scripts\\reconcile_code_queue_clarity_round3_2026-08-17.py --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import code_queue  # noqa: E402

# (cq_id, why it is being closed)
ITEMS: tuple[tuple[str, str], ...] = (
    ("cq-d0cda6edd0c3",
     "FIXED. Two independent root causes, both closed: (a) user_access matched "
     "hr/phi/cap_table by naive substring, so 'cam-PTO-ntozona' in a Reason line "
     "refused an F3 PURE write as an HR matter; (b) scope guards routed on the "
     "free-text Reason itself, so 'Reason: 4 OSN Stores' redirected all-F3E-SKU "
     "writes to #osn-leadership 5x. Commits cc4968b + 2d74627."),
    ("cq-233ca1a22976",
     "FIXED. guard_outbound already computed the specific trip class; it was "
     "discarded at both boundaries, so the requester got no remedy and 9 live "
     "failures were un-triageable. Now a job-aware diagnostic (what tripped, "
     "which channel, the remedy) + guard_class/archetype/entity/channel on the "
     "ledger row + quota disclosure. Ticket text was stale per the 8/2 GO "
     "decision: the class spans research_brief AND spreadsheet_build. "
     "Commit 4d0be3b."),
    ("cq-082174dc05fd",
     "PREMISE OVERTURNED -- not a defect. ':left_right_arrow:' is an artifact of "
     "reading Slack back through its API, which normalizes emoji-presentation "
     "Unicode to shortcodes; the client renders the arrow correctly. Proof: "
     "Cora's own persisted 2026-08-17 synthesis holds the real U+2194, and the "
     "em dash in the same line survives read-back as \\u2014. Measured residual "
     "115/613,182 KB chunks (0.019%), accepted. Invariant pinned in "
     "tests/test_slack_readback_semantics.py so this stops being re-flagged."),
    ("cq-fc4e595b8a60",
     "PREMISE OVERTURNED -- not a defect. Same read-back artifact class: Slack "
     "HTML-escapes & < > in transport and renders them correctly. Proof from the "
     "same 8/17 window: a WORKING Drive URL in #cora-filing comes back with "
     "'&amp;ouid=', so read-back escaping is cosmetic. The real risk is "
     "DOUBLE-escaping, which the new tests pin against."),
    ("cq-7f347ea3e17a",
     "SHIPPED Cowork-side 2026-08-17 (glob broadened, model pin intact) -- "
     "verified by Harrison. Cowork-side scheduled-task fix, no repo change; "
     "explicitly out of scope for this Code session per the kickoff."),
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write the transitions (default: dry-run)")
    args = ap.parse_args()

    exit_code = 0
    for cq_id, why in ITEMS:
        rec = code_queue.get_item(cq_id)
        if not rec:
            print(f"MISSING  {cq_id} -- no such queue item; SKIPPED")
            exit_code = 1
            continue
        status = str(rec.get("status", "PROPOSED"))
        title = str(rec.get("title", ""))[:64]
        if status == "SHIPPED":
            print(f"noop     {cq_id} already SHIPPED -- {title}")
            continue
        if not args.apply:
            print(f"WOULD    {cq_id} {status} -> SHIPPED -- {title}")
            print(f"         reason: {why}")
            continue
        outcome, msg = code_queue.process_queue_action(
            code_queue.ACTION_MARK_SHIPPED, cq_id, code_queue.HARRISON_ID)
        # process_queue_action returns Slack copy with emoji; a cp1252 console
        # raises UnicodeEncodeError on it AFTER the ledger write has landed,
        # which looks like a failed transition that actually succeeded.
        safe = msg.encode("ascii", "replace").decode("ascii").strip()
        print(f"{outcome:8s} {cq_id} {status} -> SHIPPED -- {safe}")
        if outcome not in ("shipped", "noop"):
            exit_code = 1

    if not args.apply:
        print("\nDRY RUN -- nothing written. Re-run with --apply.")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
