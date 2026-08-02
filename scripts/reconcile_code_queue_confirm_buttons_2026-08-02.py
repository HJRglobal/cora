#!/usr/bin/env python3
"""Step-7.5 queue reconciliation for the Slack confirm-buttons build
(branch claude/slack-confirm-buttons).

Run AFTER Harrison FF-merges the branch: transitions the one seed this
branch's MANDATORY S5 slice closes.

  * cq-2af049327848 -- staged Asana DELETE/COMPLETE confirm silently no-ops
    on a merely-unrecognized (but real) confirm phrase. Root-caused +
    fixed + regression-pinned in try_confirm_pending_write's Case 1.

Two OPTIONAL riders named in the build brief were NOT addressed by this
branch (out of scope for this ship -- left PROPOSED, not reconciled here):
  * cq-a1306f3835f8 -- "@Cora queue a code session:" phrase misroute.
  * cq-1a38136e450c -- catch-up cards render raw Slack user IDs.

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
    ("cq-2af049327848", "S5: fixed the destructive-Asana confirm no-op "
                        "(try_confirm_pending_write Case 1)"),
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
