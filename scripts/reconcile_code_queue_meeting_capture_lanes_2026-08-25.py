"""Step 7.5 queue reconciliation for the 2026-08-25 meeting-capture + lanes bundle.

Transitions ONLY the seeds this branch actually closed. Dry-run by default;
``--apply`` performs every write through ``code_queue.process_queue_action`` /
``code_queue.supersede_item`` -- never by hand-editing the jsonl or the backlog
(loop step 7.5).

Without this the queue reads stale-positive and the Monday menu re-offers shipped
work (the 2026-07-31 incident: 14 D-095 seeds still PROPOSED after merge). The
INVERSE failure is quieter and just as bad -- marking a seed SHIPPED because part
of it landed -- so the LEFT_OPEN block below is as load-bearing as SHIPPED, and
C10b is deliberately in it.

    SHIPPED
      cq-f52c6b691127  S3  per-meeting trigger + PROPOSE-ONLY meeting-ask cards
      cq-015b3bc779e9  C13 Google billing sender in the finance classifier +
                           the expected-invoice custody check
      cq-118f8bbf842e  C14 read-only Klaviyo billing/seat audit

    SUPERSEDED
      cq-0337b00c0966  "Per-meeting-triggered Fireflies summary sweep". Absorbed
                       by cq-f52c6b691127, which HAS now shipped -- so the
                       precondition #6 recorded for deferring this ("superseding
                       it now would retire a seed whose replacement does not
                       exist") is satisfied. Merged INTO cq-f52c6b691127, which
                       the kickoff named explicitly. Ordered AFTER the ship above
                       so the winner is already terminal when the merge lands.

    LEFT OPEN ON PURPOSE -- read this before assuming the bundle is complete
      cq-b0e5bc37c41b  C10. Still HALF shipped, unchanged by this session. #6
                       shipped the non-answer veto; C10b (supersede/expiry for
                       point-in-time quantitative facts) was SKIPPED HERE BY
                       INSTRUCTION, because it needs Harrison's TTL number and
                       the kickoff is explicit: "If the number is not on record
                       at session start, ask ONCE in the report and SKIP the
                       slice -- do not guess a TTL (the whole point is that it is
                       his number)." It is not on record: the #6 cascade report
                       lists it under "Decisions waiting on you" and no answer
                       appears in decisions.md, the founder TOM, or any capture
                       through 2026-08-25. The live targets are still there -- the
                       7/13 portfolio-cash snapshot in fndr.md and the superseded
                       $36.99 note in f3e.md.
      cq-288edaba659d  The other two Class-B kinds are still unaudited. This was
                       OPTIONAL in the kickoff ("Harrison rules at fire -- say
                       which in the firing message") and the firing message named
                       no ruling, so it was not fired. NOTE for whoever picks it
                       up: #6's own fix was for a DIFFERENT defect that merely
                       cited this id (three `_execute_claimed_code_queue` branches
                       whose WRITE_CONFIRMED sentinel was separated from the user
                       text by ": " instead of a blank line, making the strip fail
                       open). The population this seed actually names is three
                       kinds that emit NO sentinel at all and open with "Surface
                       this to the user:".
      cq-7bac8008b140  C5. Unchanged and still unfixable in this repo -- its only
                       writer sits on a channel `message` event this Slack app
                       does not receive.
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
    "cq-f52c6b691127": (
        "S3 -- per-meeting trigger + propose-only meeting-ask cards; detector "
        "measured on the live corpus (15 distinct asks / 19,385 sentences), "
        "attribution-unreliable routes to the meeting owner, C4 footer registered"
    ),
    "cq-015b3bc779e9": (
        "C13 -- payments-noreply@google.com scored in the finance classifier "
        "(+68 chunks, 0 new false positives across 2,880) + the monthly "
        "expected-invoice custody check. The Ads invoice itself needs a Google "
        "Ads billing-contact change, which is not code"
    ),
    "cq-118f8bbf842e": (
        "C14 -- read-only-by-construction Klaviyo client + derived charge basis "
        "(4,464 email-marketable, definition quoted) + definition-driven "
        "candidates + klaviyo_seat roster flag; ships dark, no credential"
    ),
}

SUPERSEDE: dict[str, tuple[str, str]] = {
    # loser -> (winner, why)
    "cq-0337b00c0966": (
        "cq-f52c6b691127",
        "absorbed by S3, which has now shipped -- the condition #6 set for "
        "deferring this supersede is satisfied",
    ),
}

LEFT_OPEN: dict[str, str] = {
    "cq-b0e5bc37c41b": (
        "C10 -- C10b SKIPPED BY INSTRUCTION: Harrison's TTL number is not on "
        "record, and the kickoff forbids guessing it. Asked once in the report."
    ),
    "cq-288edaba659d": (
        "OPTIONAL and not fired (no ruling in the firing message). #6 fixed a "
        "different defect that cited this id; the three no-sentinel kinds remain."
    ),
    "cq-7bac8008b140": (
        "C5 -- unchanged; unblocks in the Slack app config, not in this repo."
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

    print(f"Step 7.5 -- meeting-capture + lanes bundle "
          f"({'APPLY' if args.apply else 'DRY-RUN'})\n")

    rc = 0
    for cq_id, label in SHIPPED.items():
        rc |= _act(code_queue.ACTION_MARK_SHIPPED, cq_id, label, args.apply, "ship")

    # AFTER the ships: supersede_item refuses if the loser is already SUPERSEDED
    # and bumps the WINNER's recurrence count, so the winner should already be in
    # its final state when the merge is written.
    for loser, (winner, why) in SUPERSEDE.items():
        if not args.apply:
            print(f"  [dry-run] would supersede {loser} -> {winner}  ({why})")
            continue
        try:
            ok = code_queue.supersede_item(loser, winner)
            print(f"  SUPERSEDE {loser} -> {winner}  ({why})  -> {ok}")
            if not ok:
                # False means "no merge written" -- already superseded, a missing
                # id, or self-merge. Not fatal, but it must never read as done.
                print(f"    NOTE: supersede_item returned False for {loser}; "
                      f"check its current status before assuming it is merged.")
                rc = 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED    {loser} -> {winner}  -> {type(exc).__name__}: {exc}")
            rc = 1

    print("\n  Left OPEN on purpose (do NOT mark these shipped):")
    for cq_id, why in LEFT_OPEN.items():
        print(f"    {cq_id}  {why}")

    return rc


if __name__ == "__main__":
    sys.exit(main())
