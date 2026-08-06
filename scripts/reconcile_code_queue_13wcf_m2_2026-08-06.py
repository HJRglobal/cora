#!/usr/bin/env python3
"""Step-7.5 queue reconciliation for the 13WCF M2 QBO-actuals bundle
(branch claude/13wcf-m2-qbo-actuals).

Run AFTER Harrison FF-merges the branch: transitions the one seed the bundle
closes. Idempotent; dry-run by default; --apply to write.

  * cq-db2fd53aa608 -- "QBO empty-QueryResponse mode: transaction-type queries
    return empty bodies (not errors) while Account queries work."

    Closed by M2 in two places, and the seed turned out to UNDERSTATE the
    problem. The empty-body mode is real, and a realm whose every transaction
    type comes back empty with no error and no register line now renders UNKNOWN
    rather than $0 of activity. But the same investigation found a sharper
    version: `select * from CreditCardPayment` answers under the key
    `CreditCardPaymentTxn`, so the response was not empty at all -- a name-keyed
    lookup simply missed it and read as zero activity, silently dropping six
    bank-to-card payments worth $11,950.34 in one OSNVV week. `query_rows()`
    honours that alias and reports any OTHER unrecognised key so the window
    renders UNKNOWN instead of a wrong zero.

NOT transitioned by this script (checked, deliberately left alone):
  * cq-2ff81156f53a (SA Drive-metadata 403) -- M2 needs no modifiedTime signal,
    same as M1. Stays queued.
  * cq-f3bfa4e9ca5b (BILL/Divvy card-spend) -- adjacent, not in M2's scope.
  * cq-d55b8b9cfddf (QBO customer/invoice tools) -- adjacent, untouched.
  * cq-4e39d9f0f994 (stale test_person_identity pin vs the uncommitted
    org-roles.yaml edit) -- one of the two pre-existing suite failures this
    branch reports but did not cause. Already seeded and APPROVED; it belongs to
    whoever owns that roster edit.

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
    ("cq-db2fd53aa608",
     "M2/S1: key-agnostic QueryResponse extraction + all-types-empty -> UNKNOWN"),
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
