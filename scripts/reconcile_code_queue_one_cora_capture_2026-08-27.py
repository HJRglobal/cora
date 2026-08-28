"""Step 7.5 queue reconciliation for the 2026-08-27 One Cora capture lane.

Transitions ONLY the seed this branch actually closed. Dry-run by default;
``--apply`` performs every write through ``code_queue.process_queue_action`` --
never by hand-editing the jsonl or the backlog (loop step 7.5).

Without this the queue reads stale-positive and the Monday menu re-offers shipped
work (the 2026-07-31 incident: 14 D-095 seeds still PROPOSED after merge). The
INVERSE failure is quieter and just as bad -- marking a seed SHIPPED because part
of it landed -- so the LEFT_OPEN block below is as load-bearing as SHIPPED.

    SHIPPED
      cq-ffcf6e4ffe7c  One Cora meeting-capture lane. All four slices landed:
                       S1 DWD ensure lane (DARK behind CORA_ONECORA_ENSURE),
                       S2 daily capture auditor (LIVE), S3 fred_joined canonical
                       dedup with a measured content floor, S4 API-key migration
                       runbook. The seed is shipped even though S1 is dark: the
                       kickoff explicitly required it to ship dark until cora@'s
                       Fireflies seat is active, and "do not enable S1 live
                       in-session" was a build instruction, not deferred scope.

    ABSORBED -- see the note, this is NOT a supersede
      cq-6fef5505fb3e  "fred_joined:true is canonical when a dup pair exists."
                       The plan of record says this build ABSORBS it, and S3
                       implements it. It is deliberately NOT superseded here:
                       supersede_item is for a duplicate seed that a WINNER seed
                       replaces, and merging it into cq-ffcf6e4ffe7c would bump
                       that seed's recurrence count as though the same request had
                       arrived twice. If Harrison wants it closed, MARK IT SHIPPED
                       -- it is implemented -- rather than merged. Left to him
                       because the plan calls it "absorbed" without saying which,
                       and guessing wrong is a silent queue lie in either
                       direction.

    LEFT OPEN ON PURPOSE -- read this before assuming the bundle is complete
      cq-ebe18d20a949  Adjacent: the reconciliation_engine fireflies 0.90
                       weighting. The kickoff said "do not fix there unless
                       trivial -- note instead". It is not trivial and it was not
                       touched. VERIFIED this session and worth recording: 0.90 is
                       a per-source credibility CONSTANT in three confidence
                       formulas, NOT a dedup interaction -- an upstream dedup does
                       not change the weight or any threshold, it changes how many
                       chunks reach the passes. The real coupling is that _gap_id
                       embeds source_id, so duplicate transcripts produce distinct
                       gap ids for the same sentence.
      cq-8d16f1a557e5  The cora@ intake-mailbox decision. The account now exists,
                       which unblocks it, but nothing in this branch addresses the
                       mailbox half.
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
    "cq-ffcf6e4ffe7c": (
        "One Cora meeting-capture lane -- S1 DWD ensure lane (dark behind "
        "CORA_ONECORA_ENSURE + --apply), S2 daily capture auditor (live, 07:22 AZ), "
        "S3 fred_joined canonical dedup with a measured content floor, S4 key- "
        "migration runbook. Suite 13,392; D-051 28 confirmed findings remediated"
    ),
}

LEFT_OPEN: dict[str, str] = {
    "cq-6fef5505fb3e": (
        "fred_joined canonicality -- IMPLEMENTED by S3, but left for Harrison to "
        "close as SHIPPED rather than merged: supersede would bump the winner's "
        "recurrence count as if the request had arrived twice."
    ),
    "cq-ebe18d20a949": (
        "reconciliation_engine fireflies 0.90 weighting -- not touched, per the "
        "kickoff. Verified NOT a dedup interaction (it is a per-source confidence "
        "constant); the real coupling is _gap_id embedding source_id."
    ),
    "cq-8d16f1a557e5": (
        "cora@ intake-mailbox decision -- unblocked by the account now existing, "
        "but the mailbox half is untouched by this branch."
    ),
}


def _act(action: str, cq_id: str, label: str, apply: bool, verb: str) -> int:
    if not apply:
        print(f"  [dry-run] would {verb:<9} {cq_id}  {label}")
        return 0
    try:
        result = code_queue.process_queue_action(action, cq_id, HARRISON_ID)
        print(f"  {verb.upper():<9} {cq_id}  {label}  -> {result}")
        return 0
    except Exception as exc:  # noqa: BLE001 -- one bad id must not abort the rest
        print(f"  FAILED    {cq_id}  {label}  -> {type(exc).__name__}: {exc}")
        return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Perform the transitions. Omitted = report only.")
    args = ap.parse_args(argv)

    print(f"Step 7.5 -- One Cora capture lane "
          f"({'APPLY' if args.apply else 'DRY-RUN'})\n")

    rc = 0
    for cq_id, label in SHIPPED.items():
        rc |= _act(code_queue.ACTION_MARK_SHIPPED, cq_id, label, args.apply, "ship")

    print("\n  Left OPEN on purpose (do NOT mark these shipped without reading why):")
    for cq_id, why in LEFT_OPEN.items():
        print(f"    {cq_id}  {why}")

    return rc


if __name__ == "__main__":
    sys.exit(main())
