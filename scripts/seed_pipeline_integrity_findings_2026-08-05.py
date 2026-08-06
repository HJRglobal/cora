"""Seed the two defects the pipeline-integrity bundle FOUND but deliberately did not
fix, so neither is carried only in a session note.

Both are seeded PROPOSED -- the queue's normal intake. Harrison approves or dismisses;
nothing here is a canon write (D-011).

1. no-lane gap dispositions (MEDIUM). Slice 5's diagnostic classified all 5 remaining
   "rotting" gaps. Three are LEX-origin (the 8/13-locked fork). The other two are DM
   personal-retrieval / QA-test noise, for which a durable known-answer would be WRONG
   -- they need a "not-a-knowledge-gap" disposition. That is a design decision about a
   new lane, not a <=20-line fix, so it is queued rather than guessed at. NOTE: S3's
   eligibility gate already gives the QA-noise one a disposition once it runs live;
   this item is about the personal-retrieval class that remains.

2. channel_synthesis source-opacity leak (HIGH). Found in passing: the test
   tests/test_channel_synthesis.py::TestD051Remediation::
   test_ecom_fold_source_opaque_when_healthy FAILS ON MAIN (verified against a pristine
   `main` worktree, not just this branch) -- the fulfillment-channel line renders
   "tiktok fbt 449 (mirror) | amazon fba 753" into the daily F3E ecom synthesis, which
   posts to Slack. Source-opacity is a standing doctrine for outward-facing output, so a
   red test on main plus a live leak is a real HIGH, not a test-fixture nit.

Usage (dry-run is the default):
    .venv\\Scripts\\python.exe scripts\\seed_pipeline_integrity_findings_2026-08-05.py
    .venv\\Scripts\\python.exe scripts\\seed_pipeline_integrity_findings_2026-08-05.py --apply
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import code_queue  # noqa: E402

SEEDS = [
    {
        "kind": "gap",
        "severity": "MEDIUM",
        "title": ("Knowledge gaps with no possible lane (DM personal-retrieval asks) "
                  "rot forever with no disposition"),
        "summary": (
            "Slice 5 diagnostic 2026-08-06: of 63 gaps older than 7d, 5 have no "
            "disposition. 3 are LEX-origin (the 8/13-locked mining/escalation fork). "
            "The other 2 are asks a durable known-answer can never serve: a DM "
            "personal-retrieval request (2026-07-09 HJRG, 'help me find harrison's "
            "receipt for $1,706.65', private_source=true) and QA-test noise "
            "(2026-07-12 'what's my test locker code?'). should_escalate correctly "
            "refuses to re-broadcast a DM question to a domain owner, and mining "
            "cannot produce a durable fact from either -- so they sit unrouted "
            "forever and read as pipeline rot in the flywheel monitor. NEEDED: a "
            "'not-a-knowledge-gap' disposition (auto-close with a reason, distinct "
            "from expired) so the metric denominator only counts gaps that COULD be "
            "routed. Slice 3's eligibility gate already dispositions the QA-noise "
            "class as state=ineligible once it runs live; the personal-retrieval "
            "class is what remains. Repro: "
            "scripts/diagnose_gap_routing_completeness.py."),
        "entity": "FNDR",
        "signal": "explicit",
        "subsystem_guess": "gap_autofill",
        "status": "PROPOSED",
    },
    {
        "kind": "bug",
        "severity": "HIGH",
        "title": ("Daily F3E ecom synthesis leaks platform source names "
                  "(TikTok/Amazon) -- source-opacity test RED on main"),
        "summary": (
            "Found in passing 2026-08-06 while gating the pipeline-integrity bundle: "
            "tests/test_channel_synthesis.py::TestD051Remediation::"
            "test_ecom_fold_source_opaque_when_healthy FAILS ON PRISTINE MAIN "
            "(verified in a separate `main` worktree, so it is not a branch artifact). "
            "channel_synthesis.gather_f3e_ecom renders a fulfillment-channel line "
            "reading 'tiktok fbt 449 (mirror) | amazon fba 753 (5 of 15 skus read) -- "
            "not yet swept: walmart wfs' into the daily synthesis, which POSTS TO "
            "SLACK. Source-opacity is standing doctrine for outward-facing output "
            "(the same rule B2 enforced for QBO), so this is a live leak plus a red "
            "test on main, not a fixture nit. Fix: either route the channel line "
            "through the same source-opaque wording the rest of the fold uses, or "
            "make the deliberate exception explicit and update the test to match -- "
            "do not simply relax the assertion."),
        "entity": "F3E",
        "signal": "explicit",
        "subsystem_guess": "channel_synthesis",
        "status": "PROPOSED",
    },
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="actually seed (default: dry run)")
    args = ap.parse_args()
    dry = not args.apply
    print(f"{'[DRY]' if dry else '[APPLY]'} seeding {len(SEEDS)} finding(s)\n")
    for seed in SEEDS:
        existing = code_queue.find_fingerprint(seed["signal"], seed["title"])
        if existing:
            print(f"  already seeded as {existing}: {seed['title'][:66]}...")
            continue
        if dry:
            print(f"  would seed [{seed['severity']}/{seed['entity']}] "
                  f"{seed['title'][:66]}...")
            continue
        cq_id = code_queue.seed_item(**seed)
        print(f"  {cq_id or 'REFUSED (PHI gate)'}: {seed['title'][:66]}...")
    print(f"\n{'Re-run with --apply to write.' if dry else 'done.'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
