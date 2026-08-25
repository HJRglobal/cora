"""Step 7.5 queue reconciliation for the 2026-08-24 approvals + counters bundle.

Transitions ONLY the seeds this branch actually closed. Dry-run by default;
``--apply`` performs every write through ``code_queue.process_queue_action`` and
``code_queue.supersede_item`` -- never by hand-editing the jsonl or the backlog
(loop step 7.5).

Without this the queue reads stale-positive and the Monday menu re-offers shipped
work (the 2026-07-31 incident: 14 D-095 seeds still PROPOSED after merge). The
INVERSE failure is quieter and just as bad -- marking a seed SHIPPED because some
of it landed -- so the LEFT_OPEN block below is as load-bearing as the SHIPPED
one, and two of this bundle's slices are deliberately in it.

    SHIPPED
      cq-551fada9dee8  S1  email citation contract + the ordering bug + defect E
      cq-ab38a636e545  S2  Jerry DW eligibility -- premise overturned, state pinned
      cq-da2d6772f0ec  C1  the wholesale eval + the >=2-week auto-seed rule
      cq-16014e463a66  C2  MECHANICAL BACKLOG alarm + the PENDING WoW line
      cq-a46ebe458d92  C3  the auto-learned zero now says why + counter relabelled
      cq-e33ce0545e85  C4  honest executed-state cards (+ the C16 scanner audit)
      cq-0d40bb50bdb1  C6  decision fingerprint dedup
      cq-77984df448c7  C7  sweep liveness vs frozen-ness, separately observable
      cq-c3454e25f7cf  C8  in-thread answer to a stalled-decision alert
      cq-dacabcc2e47e  C9  intake entity from channel tokens + the LEX screen
      cq-affac22a9723  C11 the weekly participation DM
      cq-ee0a88a2185c  C12 seed-guard precision
      cq-5414c154b213  C15 stage-by-id + both leaks
      cq-89fdad5f0f86      the empty-provenance line (was STAGED)

    DISMISSED
      cq-a43ef0ccb644  the junk meta-item ("Retrieve pending code-queue item
                       cq-f52c6b691127 for staging") that a typed stage request
                       minted on 8/24 15:38. C15(b) closes the mechanism; this
                       removes the artifact. Its tombstoned kickoff file is
                       deleted by --apply AFTER the ledger transition lands, so a
                       staged prompt_path never points at a live file.

    LEFT OPEN ON PURPOSE -- read this list before assuming the bundle is complete
      cq-7bac8008b140  C5. The corrections counter still reads 0 and CANNOT be
                       fixed in this repo: its only writer sits on the channel
                       `message` event, which this Slack app does not receive
                       (Event Subscriptions, configured separately from OAuth
                       scopes and invisible to the token -- D-138..145). What
                       shipped is the honest reporting, the latent
                       mention-strip fix behind it, and the stuck-task detection.
                       The counter itself unblocks only via the Slack app config.
      cq-b0e5bc37c41b  C10. HALF shipped: the non-answer veto at the write path.
                       The supersede/expiry half -- point-in-time quantitative
                       facts like the stale 7/13 cash figure -- is NOT built.
      cq-f52c6b691127  S3. Split to session #7 (per the kickoff's own
                       "if the bundle overruns" clause). Stays APPROVED.
      cq-0337b00c0966  NOT superseded. It is absorbed by cq-f52c6b691127, and
                       that item has not shipped -- superseding it now would
                       retire the only remaining record of the per-meeting sweep.
                       Supersede it at #7's step 7.5, not here.
      cq-015b3bc779e9  C13 Google Ads invoice lane -- split to #7.
      cq-118f8bbf842e  C14 Klaviyo billing audit -- split to #7.
      cq-288edaba659d  Only the code-queue instance of the WRITE_CONFIRMED
                       directive leak is fixed. The seed names three Class-B
                       kinds; the other two are unaudited.

    APPROVED (status flip only, no build)
      cq-015b3bc779e9, cq-118f8bbf842e -- Harrison's 2026-08-24 blanket ruling
      ("All build items approved. Fold into Code #6.") never reached the ledger
      for the two slices that were split out. Flipped so the Monday menu shows
      them as ruled work awaiting a session, not as undecided proposals.
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

# The tombstoned kickoff the junk item auto-staged. Deleted only after the
# ledger dismissal lands, so nothing ever points at a live file.
TOMBSTONE = Path(
    r"G:\My Drive\HJR-Founder-OS\_shared\projects\cora\_notes"
    r"\2026-08-24_fndr_cora-code-prompt-retrieve-pending-code-queue-item-cq-f52c6b691127-8bbc09.md"
)

SHIPPED: dict[str, str] = {
    "cq-551fada9dee8": "S1 -- email citation contract, the internalDate ordering bug, and the blank Latest: line",
    "cq-ab38a636e545": "S2 -- premise overturned: Jerry was already DW-eligible; state pinned, no permissions row written",
    "cq-da2d6772f0ec": "C1 -- eval re-anchored on $25.15 (canon moved, retrieval never broke) + the >=2-week auto-seed",
    "cq-16014e463a66": "C2 -- MECHANICAL BACKLOG alarm raised in evaluate(); PENDING WoW restored to the Slack line",
    "cq-a46ebe458d92": "C3 -- dead LANE not dead read; the zero now explains itself and the counter names its actor",
    "cq-e33ce0545e85": "C4/C16 -- terminal cards drop their affordance, the emoji path retires the card, store labels shared",
    "cq-0d40bb50bdb1": "C6 -- deterministic pass-5 ids + same-fact fingerprint ledger (the six CBS filings)",
    "cq-77984df448c7": "C7 -- sweep was never frozen; liveness and frozen-ness are now separately observable",
    "cq-c3454e25f7cf": "C8 -- alerts carry an identity, a threaded answer stages a confirm, and never re-asks",
    "cq-dacabcc2e47e": "C9 -- intake resolves the entity from channel tokens; is_lex_content consumes the same union",
    "cq-affac22a9723": "C11 -- the aggregation already existed; the weekly DM to Hannah and its task now exist too",
    "cq-ee0a88a2185c": "C12 -- request-shaped PHI union; the two 8/24 false refusals pass, 16 true positives pinned",
    "cq-5414c154b213": "C15 -- stage-by-id verb, the WRITE_CONFIRMED leak, and the empty provenance line",
    "cq-89fdad5f0f86": "the empty-provenance line, fixed on the card AND the kickoff renderer",
}

DISMISS: dict[str, str] = {
    "cq-a43ef0ccb644": "junk meta-item minted by a typed stage request; the mechanism is closed by cq-5414c154b213",
}

APPROVE: dict[str, str] = {
    "cq-015b3bc779e9": "C13 -- ruled 2026-08-24, split to session #7",
    "cq-118f8bbf842e": "C14 -- ruled 2026-08-24, split to session #7",
}

LEFT_OPEN: dict[str, str] = {
    "cq-7bac8008b140": "C5 -- the counter's only writer is on an event this Slack app does not receive; unblocks in the app config, not here",
    "cq-b0e5bc37c41b": "C10 -- the non-answer veto shipped; supersede/expiry for point-in-time figures did NOT",
    "cq-f52c6b691127": "S3 -- split to #7; stays APPROVED",
    "cq-0337b00c0966": "absorbed by cq-f52c6b691127, which has not shipped -- supersede at #7, not now",
    "cq-015b3bc779e9": "C13 -- approved above, built at #7",
    "cq-118f8bbf842e": "C14 -- approved above, built at #7",
    "cq-288edaba659d": "only the code-queue instance is fixed; the other two Class-B kinds are unaudited",
}


def _act(action: str, cq_id: str, label: str, apply: bool, verb: str) -> int:
    if not apply:
        print(f"  [dry-run] would {verb:<10} {cq_id}  {label}")
        return 0
    try:
        result = code_queue.process_queue_action(action, cq_id, HARRISON_ID)
        print(f"  {verb.upper():<10} {cq_id}  {label}  -> {result}")
        return 0
    except Exception as exc:  # noqa: BLE001 -- one bad id must not abort the rest
        print(f"  FAILED     {cq_id}  {label}  -> {type(exc).__name__}: {exc}")
        return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Perform the transitions. Omitted = report only.")
    args = ap.parse_args(argv)

    print(f"Step 7.5 -- approvals + counters bundle "
          f"({'APPLY' if args.apply else 'DRY-RUN'})\n")

    rc = 0
    for cq_id, label in APPROVE.items():
        rc |= _act(code_queue.ACTION_APPROVE, cq_id, label, args.apply, "approve")
    for cq_id, label in SHIPPED.items():
        rc |= _act(code_queue.ACTION_MARK_SHIPPED, cq_id, label, args.apply, "ship")
    for cq_id, label in DISMISS.items():
        rc |= _act(code_queue.ACTION_DISMISS, cq_id, label, args.apply, "dismiss")

    # AFTER the dismissal, never before: a staged prompt_path must not point at a
    # live file that a session could paste.
    if DISMISS and TOMBSTONE.exists():
        if not args.apply:
            print(f"\n  [dry-run] would DELETE the tombstoned kickoff:\n    {TOMBSTONE}")
        else:
            try:
                TOMBSTONE.unlink()
                print(f"\n  DELETED the tombstoned kickoff:\n    {TOMBSTONE}")
            except Exception as exc:  # noqa: BLE001
                print(f"\n  FAILED to delete {TOMBSTONE}: {exc}")
                rc = 1
    elif DISMISS:
        print(f"\n  Tombstone already absent: {TOMBSTONE.name}")

    print("\n  Left OPEN on purpose (do NOT mark these shipped):")
    for cq_id, why in LEFT_OPEN.items():
        print(f"    {cq_id}  {why}")

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
