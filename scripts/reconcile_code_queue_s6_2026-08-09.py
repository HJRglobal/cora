#!/usr/bin/env python3
"""Step-7.5 queue reconciliation for the S6 migrations bundle
(branch claude/slack-buttons-s6-migrations).

Run AFTER Harrison FF-merges the branch. Idempotent; dry-run by default;
--apply to write.

Closed by this branch (3):
  * cq-904f849bc59a -- cora_remember phantom preview (HIGH). The mandatory S6
    rider: _staged_write_force_tool forces the staged-write tool via tool_choice
    on the first model turn for remember / send-DM / draft-email intents, so the
    preview is produced BY the tool (and its server-side stash) or not at all.
    Safe because an unconfirmed first call on all of them FILES NOTHING, and all
    are in _GLOBAL_CORE_TOOLS so tool_choice can never name an unexposed tool.
    Measured safe-set of 34 strings guards the D-158 displacement class.
  * cq-8866d3f7ac3b -- lexicon teach misroutes to cora_remember. Teach intents
    now route to cora_lexicon_add, LANE-GATED on lexicon_level()=="full" (read
    per-call). Shipping the mechanism, not a live behavior change: production is
    CORA_LEXICON=resolve today, where the tool answers every call with "isn't
    enabled yet", so forcing it would replace a useful reply with a dead end.
    Activates the day Harrison flips the flag; pinned both ways. The F-23
    no-stash refusal the kickoff asked to pin is pinned for all four tools.
  * cq-b8a4d7b9dd4a -- Cancel taps logged nothing (LOW). _handle_confirm_tap now
    writes one symmetric, payload-free INFO line for EVERY tap outcome, with the
    kind resolved before the claim consumes the stash.

DELIBERATELY LEFT OPEN (the kickoff's "optional riders if trivially cheap"; all
three were assessed and none is trivially cheap):
  * cq-288edaba659d -- model-facing directive prose on 3 Class-B card kinds. The
    fix is a clean user-facing half per executor, touching formatters shared with
    other callers. The v2 bundle's own lesson is that six of ten defects came
    from its late fixes, so this stays a deliberate separate slice.
  * cq-5ef75b623c10 -- slack_send_dm into _CONTRACT_WRITE_TOOLS. The rider
    partially addresses the motivating symptom (the preview turn now runs forced
    + on Sonnet, so the Haiku-fabrication shape is closed), but membership itself
    converts every turn of this tool to verbatim posting and wants its own tests.
  * Meeting per-item cards rendered 6 of 9 items on the 8/9 battery. VERIFIED as
    BY DESIGN and typed-reachable -- the kickoff asked to check exactly this:
      - confirm_cards.MAX_ITEM_CARDS == 6 caps how many CARDS post, matching
        meeting_actions._MAX_SELECTED == 6, which slices `selected[:_MAX_SELECTED]`
        in _create_selected as a per-CALL creation cap (timeout safety).
      - Items 7+ ARE still creatable. The server-side verified list is
        deliberately NOT capped at _MAX_SELECTED (an S5 D-051 fix: capping it made
        items 7+ un-creatable by ANY route and told the user "none of those match
        the action items I showed you" about items it had itself displayed). A
        typed confirm naming items 7-9 filters against that uncapped list and
        creates them, since 3 <= the per-call cap.
    So: no defect, no fix. Raising the card cap would mean raising _MAX_SELECTED,
    which is a product decision about the meeting-capture time budget, not a bug.

Standalone script -- does NOT import the bot process; no restart needed.
"""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_REPO_ROOT / ".env")

sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import code_queue  # noqa: E402

SHIPS: list[tuple[str, str]] = [
    ("cq-904f849bc59a", "phantom preview closed by the tool_choice staged-write force"),
    ("cq-8866d3f7ac3b", "lexicon teach routes to cora_lexicon_add (lane-gated); F-23 refusal pinned"),
    ("cq-b8a4d7b9dd4a", "symmetric confirm_card TAP log line on every tap outcome"),
]

LEFT_OPEN: list[tuple[str, str]] = [
    ("cq-288edaba659d", "model-facing card prose -- shared formatters, own slice"),
    ("cq-5ef75b623c10", "slack_send_dm -> _CONTRACT_WRITE_TOOLS -- wants its own tests"),
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

    print("\nLeft OPEN on purpose:")
    for cid, why in LEFT_OPEN:
        rec = code_queue.get_item(cid)
        status = rec.get("status") if rec else "missing"
        print(f"  {cid} [{status}] <- {why}")

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
