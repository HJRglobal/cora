#!/usr/bin/env python3
"""One-shot cleanup: merge the two day-one duplicate pairs in the code-session queue
(v1.1 hardening, Slice 1f).

Field defect: the pre-hardening dedup missed two same-intent rephrasings, so the queue
holds two duplicate pairs (evidence: 2026-07-28 ledger + DM cards):

  * cq-3c265adf8fd1  ->  cq-5f48f328687b   (the RepRally order-check double-file)
  * cq-dad80c0011c9  ->  cq-06f4797db4f1   (the env-flag confirm-race duplicate)

Each pair is merged: the LOSER is marked SUPERSEDED (recording superseded_by) and the
WINNER's recurrence count is bumped, so the backlog regen reflects the merge. Both ids
must exist; a loser already SUPERSEDED is left alone (idempotent).

DRY-RUN BY DEFAULT -- pass --apply to write. Standalone script -- does NOT import the
bot process; no restart needed. Harrison runs --apply.

Usage:
    python scripts/cleanup_code_queue_dupes_2026-07-28.py            # dry-run
    python scripts/cleanup_code_queue_dupes_2026-07-28.py --apply    # write
"""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cora import code_queue  # noqa: E402

# (loser -> winner). The loser is superseded INTO the winner.
MERGES: list[tuple[str, str]] = [
    ("cq-3c265adf8fd1", "cq-5f48f328687b"),
    ("cq-dad80c0011c9", "cq-06f4797db4f1"),
]

# The build-REQUEST items whose work v1.1 delivers -> mark SHIPPED at cascade (prompt
# section 0). NOT the RepRally survivor cq-5f48.. (that is a genuine future build; only
# its duplicate is superseded above).
SHIPS: list[str] = [
    "cq-72b3e2ab670f",  # the dedup improvement (delivered by slice 1a)
    "cq-06f4797db4f1",  # the env-flag docstring fix (delivered by slice 1f)
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Actually write the merge events (default: dry-run print only).")
    args = ap.parse_args()

    merged, skipped = 0, 0
    for loser_id, winner_id in MERGES:
        loser = code_queue.get_item(loser_id)
        winner = code_queue.get_item(winner_id)
        if not loser or not winner:
            print(f"SKIP (missing id): loser={loser_id} exists={bool(loser)} "
                  f"winner={winner_id} exists={bool(winner)}")
            skipped += 1
            continue
        if loser.get("status") == "SUPERSEDED":
            print(f"SKIP (already superseded): {loser_id}")
            skipped += 1
            continue
        line = (f"{loser_id} [{loser.get('status')}] '{loser.get('title', '')[:60]}' "
                f"-> {winner_id} [{winner.get('status')}] '{winner.get('title', '')[:60]}'")
        if args.apply:
            ok = code_queue.supersede_item(loser_id, winner_id)
            print(f"{'MERGED' if ok else 'NOOP'}: {line}")
            merged += 1 if ok else 0
        else:
            print(f"WOULD MERGE: {line}")

    shipped = 0
    for cid in SHIPS:
        rec = code_queue.get_item(cid)
        if not rec:
            print(f"SKIP SHIP (missing id): {cid}")
            continue
        if rec.get("status") == "SHIPPED":
            print(f"SKIP SHIP (already shipped): {cid}")
            continue
        line = f"{cid} [{rec.get('status')}] '{rec.get('title', '')[:60]}'"
        if args.apply:
            code_queue.process_queue_action(
                code_queue.ACTION_MARK_SHIPPED, cid, code_queue.HARRISON_ID)
            print(f"SHIPPED: {line}")
            shipped += 1
        else:
            print(f"WOULD SHIP: {line}")

    print("")
    if args.apply:
        print(f"Done. Merged {merged}, shipped {shipped}, skipped {skipped}.")
        try:
            code_queue.render_backlog()
        except Exception as exc:  # noqa: BLE001
            print(f"(backlog render skipped: {exc})")
    else:
        print(f"Dry-run: would merge {len(MERGES) - skipped} pair(s) + ship {len(SHIPS)} item(s). "
              "Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
