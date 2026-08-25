"""Step 7.5 queue reconciliation for the 2026-08-25 Class-B directive-prose bundle.

Transitions ONLY the seed this branch actually closed. Dry-run by default;
``--apply`` performs every write through ``code_queue.process_queue_action`` --
never by hand-editing the jsonl or the backlog (loop step 7.5).

Without this the queue reads stale-positive and the Monday menu re-offers shipped
work (the 2026-07-31 incident: 14 D-095 seeds still PROPOSED after merge). The
INVERSE failure is quieter and just as bad -- marking a seed SHIPPED because part
of it landed -- so the LEFT_OPEN block below is as load-bearing as SHIPPED.

    SHIPPED
      cq-288edaba659d  Confirm cards render model-facing directive prose.
                       The seed's title says "3 Class-B kinds"; MEASURED against
                       the authoritative roster (_CLASSB_KINDS = EIGHT kinds),
                       SIX kinds leaked across THIRTEEN outcome paths, because
                       every kind's FAILURE return is sentinel-free by
                       construction and the seed counted only success paths. The
                       title is corrected in the cascade report rather than
                       silently shipped as written. All 24 outcome paths across
                       all 8 kinds now measure clean; a non-Class-B leak in
                       _execute_claimed_code_queue was found by the static
                       executor scan and fixed in the same pass.

    LEFT OPEN ON PURPOSE -- read this before assuming the class is closed
      cq-5ef75b623c10  "slack_send_dm approval surface is model-authored (not in
                       _CONTRACT_WRITE_TOOLS)". ADJACENT, NOT CLOSED. That seed
                       is about the PREVIEW surface and about code-enforcing the
                       preview via _CONTRACT_WRITE_TOOLS membership; this bundle
                       deliberately did not touch that set (doing so converts
                       every turn of the tool to verbatim posting and wants its
                       own tests). What this bundle DOES change for it: every
                       Class-B success payload now emits a well-formed contract,
                       so the "membership is close to a one-line change"
                       precondition it records now holds for more kinds than it
                       did. Its own claim that "the confirm card is built from
                       the model's reply rather than the tool's own text" was
                       re-verified true this session for the PREVIEW card.
      cq-767298fa78b3  Connector footer riding into slack_send_dm's stashed
                       message payload. Different defect (the S6 strip list), on
                       the payload rather than the outcome text. Untouched.
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
    "cq-288edaba659d": (
        "Class-B directive prose -- shared-seam belt (_strip_model_directives) + "
        "13 leaking outcome paths across 6 of the 8 roster kinds rewritten to a "
        "well-formed contract (success) or clean human text (failure); "
        "roster-driven runtime invariant + static AST invariant over all 17 "
        "executors; seed's '3 kinds' corrected to 6"
    ),
}

LEFT_OPEN: dict[str, str] = {
    "cq-5ef75b623c10": (
        "slack_send_dm _CONTRACT_WRITE_TOOLS membership / preview surface -- "
        "ADJACENT, deliberately untouched. Needs its own tests."
    ),
    "cq-767298fa78b3": (
        "Connector footer in slack_send_dm's stashed payload -- different "
        "defect (S6 strip list), on the payload not the outcome text."
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

    print(f"Step 7.5 -- Class-B directive-prose bundle "
          f"({'APPLY' if args.apply else 'DRY-RUN'})\n")

    rc = 0
    for cq_id, label in SHIPPED.items():
        rc |= _act(code_queue.ACTION_MARK_SHIPPED, cq_id, label, args.apply, "ship")

    print("\n  Left OPEN on purpose (do NOT mark these shipped):")
    for cq_id, why in LEFT_OPEN.items():
        print(f"    {cq_id}  {why}")

    return rc


if __name__ == "__main__":
    sys.exit(main())
