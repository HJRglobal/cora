"""Standing-loop STEP 7.5 for the pipeline-integrity bundle: mark SHIPPED the queue
seeds this branch closes.

Run at (or immediately after) the fast-forward merge of
`claude/pipeline-integrity-2026-08-05`. Without it the queue reads stale-positive and
both the Monday menu and the next Code session re-offer work that is already shipped
(the 2026-07-31 incident: 14 D-095 seeds still PROPOSED post-merge).

Transitions via code_queue.process_queue_action(ACTION_MARK_SHIPPED, ...) only -- never
a hand-edit of data/state/code-session-queue.jsonl or the generated backlog .md.

DELIBERATELY NOT SHIPPED here:
- cq-f1236540b61e (P1 #info-for-cora intake) -- ships with ITS OWN session, per the
  bundle kickoff. Slice 0 only recorded its missing `staged` event.
- cq-742ce9691bb5 / cq-86c283d95a34 -- seeded BY this session as found-but-not-fixed;
  they stay PROPOSED for Harrison to triage.
- cq-483109dfea11 -- the lexicon ask-on-ambiguity bypass. A DIFFERENT mechanism from
  9-RED-2 (model-side phrase rewriting, not a literal suffix the matcher rejects);
  stays PROPOSED.
- cq-4e39d9f0f994 -- the Alina org-roles test pin. Its failure is caused by a
  concurrent session's uncommitted data/maps/org-roles.yaml edit, not by this branch.

Usage (dry-run is the default):
    .venv\\Scripts\\python.exe scripts\\reconcile_code_queue_shipped_pipeline_integrity_2026-08-05.py
    .venv\\Scripts\\python.exe scripts\\reconcile_code_queue_shipped_pipeline_integrity_2026-08-05.py --apply
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import code_queue  # noqa: E402

# (cq_id, which slice closed it)
SHIPPED = [
    ("cq-861ca3630d31", "S1 -- pack-qualifier parity in F3E inventory resolution"),
    ("cq-a1306f3835f8", "S2 -- explicit code-queue phrase outranks the Asana task-op force"),
    ("cq-5c6ff15610bd", "S3 -- mine-eligibility gate + the D-128 disputed-exchange rule"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="actually write (default: dry run)")
    args = ap.parse_args()
    dry = not args.apply
    print(f"{'[DRY]' if dry else '[APPLY]'} step 7.5 -- pipeline-integrity bundle\n")

    rc = 0
    for cq_id, slice_note in SHIPPED:
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
            print(f"  {cq_id}: {status} -> would mark SHIPPED  ({slice_note})")
            continue
        outcome, msg = code_queue.process_queue_action(
            code_queue.ACTION_MARK_SHIPPED, cq_id, code_queue.HARRISON_ID)
        print(f"  {cq_id}: {status} -> {outcome}: {msg}  ({slice_note})")

    print(f"\n{'Re-run with --apply to write.' if dry else 'done.'}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
