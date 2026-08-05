#!/usr/bin/env python3
"""Step-7.5 queue reconciliation for the A5 QBO-bank + inventory bundle
(branch claude/a5-qbo-bank-inventory).

Run AFTER Harrison FF-merges the branch. Without this the queue reads
stale-positive and the Monday menu re-offers work that already shipped
(the 2026-07-31 incident: 14 seeds still PROPOSED post-merge).

Idempotent; dry-run by default; --apply to write. Never hand-edit the jsonl or
the backlog -- both are regenerated from the event log by process_queue_action.

  * cq-a2066f11c4f1 -- fndr_intercompany_check (STAGED, sparse auto-draft).
    ABSORBED by the A5 design and built as the close pack's `intercompany`
    section: a real Data-row account extractor (the staged draft would have used
    the Section-summary readers, which return zero candidates on every realm),
    discovery-first with an empty confirmed-pair map, LEX names rendered as
    opaque placeholders, and per-pair sign conventions recorded rather than
    inferred.

  * cq-6fbb9d717512 -- close pack's OSN consolidated cash row false-flags weekly.
    FIXED by the S2 rider: the sheet's "OSN" row is the tab "OSN Consolidated"
    ($37,605 = the four store tabs summed) while the QBO realm OSN is a cash-less
    shell ($0), so the check compared a consolidation against an empty shell. The
    books leg is now re-based to the sum of the member realms; live, the row
    reports a real -$8,026 consolidation gap instead of a phantom -$37,605.

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
    ("cq-a2066f11c4f1", "S3: intercompany discovery/check absorbed into the close pack"),
    ("cq-6fbb9d717512", "S2 rider: OSN consolidated cash row re-based to the store realms"),
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
