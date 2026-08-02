#!/usr/bin/env python3
"""Step-7.5 queue reconciliation for the web-gate/notes fix
(branch claude/web-gate-notes-fix-2026-08-01).

Run AFTER Harrison FF-merges the branch + restarts (app.py/context_loader/
web_guard are bot-loaded): transitions the one seed this branch closes.
Idempotent; dry-run by default; --apply to write.

  * cq-49a7835f081c -- web tools never attached for Harrison: the
    personal-note overlay set unstripped_personal on nearly every ask and the
    app.py gate skipped web_guard.evaluate() silently. Fixed via the web-clean
    context load (notes/Tier-1-unstripped/cross-entity-fallback excluded from
    explicit-web-intent turns by construction) + gate_skipped:<reason>
    ledger/log observability.

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
    ("cq-49a7835f081c", "web-clean context load + gate_skipped observability"),
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
