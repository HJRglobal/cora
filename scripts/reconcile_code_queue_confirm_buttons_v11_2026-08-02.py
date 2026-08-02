#!/usr/bin/env python3
"""Step-7.5 queue reconciliation for the Slack confirm-buttons v1.1 live-smoke
fix bundle (branch claude/slack-buttons-v11-fixes).

Run AFTER Harrison FF-merges the branch: transitions the 4 seeds this
branch's S1-S4 slices close.

  * cq-883878e81274 -- S1 (HIGH): concurrent-turn stash cross-binding. Fixed
    with a contextvars-based turn id (confirm_cards.begin_turn/_TURN_ID),
    stamped at mint time, checked by freshest_changed_stash. D-051 re-review
    caught the first attempt incomplete (tool_dispatch.dispatch()'s own
    internal executor also needed the fix) before this could ship.
  * cq-056a3a4de2f7 -- S2 (MED): terminal-edit race. Rebuilt so an
    already_handled/superseded (same-card race loser) outcome never edits
    the shared card -- ephemeral-only, unconditionally. D-051 re-review
    caught the first "claim a slot" registry design as incomplete (it only
    blocked the clobber in one arrival order) before this could ship.
  * cq-4c9306652bb5 -- S3 (MED): Cowork connector footer defeated the
    deterministic confirm-intent classifier. Fixed with an end-anchored
    strip mirroring code_queue.py's existing pattern.
  * cq-08166dcf283d -- S4 (MED): a haiku-routed "remember"/"forget note"
    turn could emit a phantom (zero-tool_use) preview. Fixed with a new
    intent detector forcing Sonnet, plus has_pending_remember/
    has_pending_forget_note closing the matching confirm-turn gap.

Two out-of-scope, PRE-EXISTING v1 bugs found during the D-051 review were
NOT addressed by this branch -- flagged as separate follow-up sessions
(spawn_task chips shown to Harrison), left PROPOSED, not reconciled here:
  * resolve_and_claim_stash's re_previewed detection is an unowned peek
    against the shared per-kind slot (HIGH, can bind a phantom card to an
    unrelated task under a concurrent same-kind write).
  * _stash_expired_label reads the wrong dict field for Asana create/
    subtask (LOW-MEDIUM, degrades to a generic "that task" label).
cq-483109dfea11 (bulk/single product-row canonicalization) was also named as
a possible rider and was NOT attempted -- its own seed, unaffected here.

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
    ("cq-883878e81274", "S1: turn-scoped stash binding (freshest_changed_stash "
                        "+ dispatch() propagation fix)"),
    ("cq-056a3a4de2f7", "S2: terminal-edit race -- already_handled/superseded "
                        "never edit the shared card"),
    ("cq-4c9306652bb5", "S3: strip the Cowork connector footer before "
                        "confirm-intent classification"),
    ("cq-08166dcf283d", "S4: force Sonnet on a clear remember/forget-note "
                        "preview or confirm turn"),
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
