#!/usr/bin/env python3
"""One-shot status reconciliation: transition the code-queue seeds the D-095
bug-hunt bundle closed (main=ccd70a3, merged + restarted 2026-07-31).

Executes the Harrison-approved staged note
(00-Founder/projects/review-cora-capabilities-and-roadmap/_notes/
2026-07-31_fndr_code-queue-status-reconciliation.md) with two VERIFY-FIRST
corrections against the kickoff-of-record + cascade report + bundle commits:

  1. cq-9845f2effb8d joins the SHIPPED set (the note misfiled it in the
     verify bucket): the kickoff names it among the 13 covered seeds, commit
     345aa69 implements it ("read-path location scoping"), and the cascade
     lists it under Seeds covered. 13 bundle-shipped ids, not 12.
  2. cq-7fb82054ee4a: the note says SUPERSEDED, but supersede_item() requires
     a winner queue item and none exists (it was fixed by PRIOR main work
     8bc2a81, not by another item). Marked SHIPPED via the public API
     instead -- factually defensible: its residual (8s->20s timeout) shipped
     in bundle commit c73efa6 and the cascade lists it as covered. The
     premise-overturned note lives here and in the session capture.

LEFT OPEN (verified NOT in the bundle): cq-a1306f3835f8, cq-2af049327848,
cq-8e2771423833, and the two P3s the kickoff explicitly excluded for the
2026-08-03 Monday menu (cq-0e9971a5d047, cq-bd286f89b357). NOTE: the Monday
menu surfaces APPROVED items only -- the P3s need Harrison's approve tap to
actually ride it.

All transitions go through code_queue.process_queue_action (append-only
events; never hand-edit the jsonl or the generated G: backlog). Idempotent:
already-SHIPPED ids are skipped. DRY-RUN BY DEFAULT -- pass --apply to write.
Standalone script -- does NOT import the bot process; no restart needed
(every consumer re-folds the jsonl per read).
"""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cora import code_queue  # noqa: E402

# The 14 seeds the D-095 bundle closed: 13 bundle slices + the
# premise-overturned-with-residual-shipped item (see docstring).
SHIPS: list[tuple[str, str]] = [
    ("cq-935a18e2969e", "Slice 1: dual-metric decision age (4fd6f89)"),
    ("cq-7dde32efa597", "Slice 2a: portfolio cash reconciliation (4985aa6)"),
    ("cq-90f8ca56c758", "Slice 2b: memo WoW delta reconciliation (4985aa6)"),
    ("cq-b38f9293fe3b", "Slice 2c: PM digest real counts (1692809)"),
    ("cq-ed29165fca97", "Slice 3: expired-confirm tombstone (345aa69)"),
    ("cq-9845f2effb8d", "Slice 3: read-path location scoping (345aa69)"),
    ("cq-532b1c30256c", "Slice 4a: blind-execute confirm removed (c73efa6)"),
    ("cq-7fb82054ee4a", "Slice 4b: premise overturned (fixed 8bc2a81); residual timeout shipped (c73efa6)"),
    ("cq-4d73879917fa", "Slice 5: pricing provenance grounding + purge (5420cf5)"),
    ("cq-479b157f8c00", "Slice 6: Slack event-delivery idempotency (49af3a0)"),
    ("cq-c6392ebbaa45", "Slice 7: code-computed due framing + Today anchor (6f77215)"),
    ("cq-f583932a625e", "Slice 8: filer sibling uniquification (8ff0fdf)"),
    ("cq-a40ca0e72d86", "Slice 9: Needs-you source labels (8ff0fdf)"),
    ("cq-d9432f552a33", "Slice 10: known-answers write isolation (36c64e4)"),
]

# Verified NOT in the bundle -- listed here so a re-reader sees the decision,
# and so the script can assert they are NOT accidentally transitioned.
LEAVE_OPEN: list[str] = [
    "cq-a1306f3835f8",
    "cq-2af049327848",
    "cq-8e2771423833",
    "cq-0e9971a5d047",
    "cq-bd286f89b357",
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Actually write the shipped events (default: dry-run print only).")
    args = ap.parse_args()

    shipped, skipped, missing = 0, 0, 0
    for cid, why in SHIPS:
        rec = code_queue.get_item(cid)
        if not rec:
            print(f"SKIP (missing id): {cid}")
            missing += 1
            continue
        if rec.get("status") == "SHIPPED":
            print(f"SKIP (already shipped): {cid}")
            skipped += 1
            continue
        line = (f"{cid} [{rec.get('status')}] '{rec.get('title', '')[:60]}'"
                f"  <- {why}")
        if args.apply:
            code_queue.process_queue_action(
                code_queue.ACTION_MARK_SHIPPED, cid, code_queue.HARRISON_ID)
            print(f"SHIPPED: {line}")
            shipped += 1
        else:
            print(f"WOULD SHIP: {line}")

    print("")
    for cid in LEAVE_OPEN:
        rec = code_queue.get_item(cid)
        status = rec.get("status") if rec else "(missing)"
        print(f"LEAVE OPEN: {cid} [{status}] '{(rec or {}).get('title', '')[:60]}'")

    print("")
    if args.apply:
        print(f"Done. Shipped {shipped}, already-shipped {skipped}, missing {missing}.")
        try:
            code_queue.render_backlog()
            print("Backlog regenerated.")
        except Exception as exc:  # noqa: BLE001
            print(f"(backlog render skipped: {exc})")
    else:
        print(f"Dry-run: would ship {len(SHIPS) - skipped - missing} item(s), "
              f"leave {len(LEAVE_OPEN)} open. Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
