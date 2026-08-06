"""Slice 0 -- one-shot code-queue reconciliation for the pipeline-integrity bundle.

Harrison approved every transition below in the 2026-08-05 FNDR Fable
flywheel/pipeline strategy session (fork of record: "Queue actions -- approve all
four"; capture note
``00-Founder/_session-captures/2026-08/2026-08-05_fndr_flywheel-pipeline-strategy.md``).

Every write goes through a PUBLIC code_queue API (seed_item /
process_queue_action / set_severity / append_evidence) so the append-only event
ledger stays the single source of truth -- never a hand-edit of
``data/state/code-session-queue.jsonl`` or the generated backlog .md.

Idempotency: every step is a genuine no-op on re-run. Step 3b relies on
``append_evidence``'s note dedup, which was ADDED during the D-051 review of this
bundle -- before that, a second ``--apply`` re-appended the same notes until the
fold's 10-entry cap silently ate them. Verified 2026-08-06 against the live rows.

Known cosmetic residual: ``_scrub_evidence`` truncates a note to 200 chars, and the
first ``--apply`` (pre-review) stored both notes below cut mid-word, losing the tails.
``append_evidence`` now REPORTS truncation instead of falsely succeeding; the notes
are left as-is on purpose so the dedup above keeps matching, and their full text lives
in the S0 commit message and the session capture note.

What it does:
  1. SEED the 9-RED-2 true gap (single-item SKU alias rejects a literal "12-pack"
     suffix the batch path tolerates) as a NEW item, then PROPOSED -> APPROVED.
  2. cq-a1306f3835f8 ("queue a code session:" misroute): PROPOSED -> APPROVED,
     severity MEDIUM -> HIGH.

NOTE on the HIGH items and kickoff prompts: this script's ``--apply`` ran while the
tree was at S0, before S4 taught the approve path that HIGH is P1-class, so
cq-861ca3630d31 and cq-a1306f3835f8 are APPROVED-with-no-kickoff. That is CORRECT
here and deliberately not repaired: this branch IS their build, and step 7.5 marks
them SHIPPED at merge. If the merge slips past
``code_queue.PRIORITY_KICKOFF_GRACE_HOURS``, the new nightly monitor will name them --
expected, not a regression.
  3. cq-5c6ff15610bd (gap-autofill known_answer eligibility): PROPOSED ->
     APPROVED, plus two dated real-world evidence notes (NOT a new item, and NOT
     a `recurrence` -- see code_queue.append_evidence for why count must not move).
  4. cq-f1236540b61e (P1 #info-for-cora intake): record the missing `staged`
     event pointing at the kickoff file a Fable session hand-staged on 8/3, so the
     ledger stops reading it as approved-but-unstaged.

Usage (dry-run is the default):
    .venv\\Scripts\\python.exe scripts\\reconcile_code_queue_pipeline_integrity_2026-08-05.py
    .venv\\Scripts\\python.exe scripts\\reconcile_code_queue_pipeline_integrity_2026-08-05.py --apply
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import code_queue  # noqa: E402

HARRISON_ID = code_queue.HARRISON_ID

# ── 1. the 9-RED-2 true gap ───────────────────────────────────────────────────
NEW_ITEM = {
    "kind": "bug",
    "severity": "HIGH",
    "title": ("single-item SKU alias resolution rejects literal '12-pack' suffix; "
              "batch path succeeds"),
    "summary": (
        "Weekly Slack-output clarity audit 2026-08-01, reproduced 2x post-restart: "
        "f3e_shopify_set_inventory single-item mode refuses a product phrase carrying "
        "a literal pack-size suffix (\"couldn't find 'F3 Pure Variety Pack 12-pack'\" / "
        "\"couldn't find 'Pure Strawberry Lemonade 12-pack'\") while the identical "
        "phrase resolves on the items[] batch path. Blocks single-item office "
        "inventory writes daily. DISTINCT MECHANISM from cq-483109dfea11 (that one is "
        "model-side phrase rewriting bypassing the lexicon ask-on-ambiguity; this is a "
        "literal suffix the matcher cannot tolerate). Evidence: sweep findings doc "
        "2026-08-05_fndr_cora-request-sweep-and-pipeline-review.md item 9-RED-2."),
    "entity": "F3E",
    "signal": "explicit",
    "subsystem_guess": "f3e_shopify_set_inventory",
    "status": "PROPOSED",
}

# ── 2 + 3. approve-in-place ───────────────────────────────────────────────────
APPROVALS = [
    ("cq-a1306f3835f8", "HIGH"),
    ("cq-5c6ff15610bd", None),
]

# ── 3b. dated evidence for cq-5c6ff15610bd ────────────────────────────────────
EVIDENCE = [
    ("cq-5c6ff15610bd",
     "2026-08-05: the 8/3 fighter-compliance exchange converted to an approved "
     "known_answer in _brain/known-answers/f3e.md, encoding ONE SIDE of a "
     "tracking-sheet disagreement Cora herself had flagged as unresolved -- the "
     "D-128 class (an exchange Cora flagged uncertain is DECISION material, not "
     "FACT material)."),
    ("cq-5c6ff15610bd",
     "2026-08-05: an internal classifier string (\"capability/knowledge ask routed "
     "from code-queue classifier\") converted into a known answer in f3e.md around "
     "lines 61-63 -- a capability ask becoming a durable fact, the exact class this "
     "item names."),
]

# ── 4. the missing staged event ───────────────────────────────────────────────
STAGED = (
    "cq-f1236540b61e",
    str(Path("G:/My Drive/HJR-Founder-OS/_shared/projects/cora/_notes/"
             "2026-08-10_fndr_cora-code-prompt-info-for-cora-intake.md")),
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="actually write (default is a dry run)")
    args = ap.parse_args()
    dry = not args.apply
    tag = "[DRY]" if dry else "[APPLY]"

    print(f"{tag} Slice 0 -- code-queue reconciliation (pipeline-integrity bundle)\n")

    # 1. seed + approve the new 9-RED-2 item -----------------------------------
    existing = code_queue.find_fingerprint("explicit", NEW_ITEM["title"])
    if existing:
        print(f"  1. 9-RED-2 already seeded as {existing}")
        new_id: str | None = existing
    elif dry:
        print(f"  1. would SEED 9-RED-2 (HIGH, F3E): {NEW_ITEM['title'][:70]}...")
        new_id = None
    else:
        new_id = code_queue.seed_item(**NEW_ITEM)
        if not new_id:
            print("  1. FAILED -- seed_item refused (PHI gate?)")
            return 1
        print(f"  1. SEEDED {new_id}")

    if new_id:
        if dry:
            rec = code_queue.get_item(new_id) or {}
            print(f"     would APPROVE {new_id} (status now {rec.get('status')})")
        else:
            outcome, msg = code_queue.process_queue_action(
                code_queue.ACTION_APPROVE, new_id, HARRISON_ID)
            print(f"     approve -> {outcome}: {msg}")

    # 2 + 3. approvals (+ optional re-rate) ------------------------------------
    for i, (cq_id, severity) in enumerate(APPROVALS, start=2):
        rec = code_queue.get_item(cq_id)
        if not rec:
            print(f"  {i}. {cq_id}: MISSING -- skipped")
            continue
        print(f"  {i}. {cq_id} (status {rec.get('status')}, severity {rec.get('severity')})")
        if severity:
            if dry:
                print(f"     would set severity -> {severity}")
            else:
                outcome, msg = code_queue.set_severity(cq_id, HARRISON_ID, severity)
                print(f"     set_severity -> {outcome}: {msg}")
        if dry:
            print("     would APPROVE")
        else:
            outcome, msg = code_queue.process_queue_action(
                code_queue.ACTION_APPROVE, cq_id, HARRISON_ID)
            print(f"     approve -> {outcome}: {msg}")

    # 3b. evidence -------------------------------------------------------------
    for cq_id, note in EVIDENCE:
        if dry:
            print(f"  3b. would attach evidence to {cq_id}: {note[:70]}...")
            continue
        outcome, msg = code_queue.append_evidence(cq_id, HARRISON_ID, note)
        print(f"  3b. append_evidence {cq_id} -> {outcome}: {msg}")

    # 4. the missing staged event ---------------------------------------------
    cq_id, path = STAGED
    rec = code_queue.get_item(cq_id)
    if not rec:
        print(f"  4. {cq_id}: MISSING -- skipped")
    elif rec.get("status") == "STAGED" and rec.get("prompt_path"):
        print(f"  4. {cq_id} already STAGED at {rec['prompt_path']}")
    elif dry:
        print(f"  4. would record staged event for {cq_id} -> {path}")
        print(f"     kickoff file exists on disk: {Path(path).exists()}")
    else:
        if not Path(path).exists():
            print(f"  4. REFUSED -- kickoff file not found: {path}")
            return 1
        outcome, msg = code_queue.record_staged(cq_id, path, HARRISON_ID)
        print(f"  4. record_staged {cq_id} -> {outcome}: {msg}")

    print(f"\n{tag} done." + ("  Re-run with --apply to write." if dry else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
