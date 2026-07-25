#!/usr/bin/env python3
"""D1 interactivity smoke test (Rider D, 2026-07-24).

Sends TWO Block Kit cards to Harrison's DM so a live button tap can be observed
end-to-end:

  1. A knowledge one-tap card  (✅ Approve & save / 👎 Dismiss  -> ACTION_APPROVE
     / ACTION_DISMISS, handled by app._handle_knowledge_one_tap).
  2. An auto-write digest item (↩️ Revert -> ACTION_AUTOWRITE_REVERT, handled by
     app._handle_autowrite_revert).

Both carry a SENTINEL update_id ("smoke-*") that matches nothing in the ledger,
so a tap is guaranteed SIDE-EFFECT-FREE: process_one_tap_action returns
"not_found" and process_autowrite_revert returns "not_found" -- no write, no
resolve. A successful tap still proves the full round-trip: Slack -> Socket Mode
-> @app.action handler -> in-message acknowledgement (chat_update / threaded
reply). Zero button taps have ever appeared in the live bot log, so this is the
first proof the interactivity path works at all.

The card text tells Harrison it is a safe test.

Usage (from the repo root, host venv):
    .venv\\Scripts\\python.exe scripts\\smoke_interactivity_cards.py            # DRY-RUN: print, no send
    .venv\\Scripts\\python.exe scripts\\smoke_interactivity_cards.py --send      # actually DM the two cards

Requires SLACK_BOT_TOKEN in the environment / .env. Prints each card's
message_ts on send so the tap can be correlated in the live log.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# The Windows console defaults to cp1252, which cannot encode the emoji in the
# card bodies; reconfigure stdout so the dry-run / status prints never crash.
try:  # pragma: no cover -- console I/O only
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Importing cora installs the class-level Slack egress patch in-process
# (test_no_raw_slack_post guard); the card bodies below are fixed literals with
# no user/PHI content, so egress is a formality here.
from cora.knowledge_review import (  # noqa: E402
    HARRISON_SLACK_USER_ID,
    build_autowrite_digest_blocks,
    build_single_item_blocks,
    _build_slack_client,
)

_TEST_BANNER = (
    "🧪 *TEST CARD — safe to tap.* Tapping just proves Cora's button "
    "interactivity works; it matches no real item, so nothing is saved, changed, "
    "or deleted. — Rider D D1 smoke test."
)


def _knowledge_card(stamp: str) -> tuple[str, list[dict]]:
    update = {
        "update_id": f"smoke-kr-{stamp}",
        "update_type": "known_answer",
        "confidence": "LOW",
        "description": f"{_TEST_BANNER}\n\n(This is the knowledge one-tap card.)",
        "source_evidence": "",
    }
    return build_single_item_blocks(update)


def _digest_card(stamp: str) -> tuple[str, list[dict]]:
    record = {
        "update_id": f"smoke-aw-{stamp}",
        "update_type": "known_answer",
        "tier": 0,
        "entity": "TEST",
        "summary": f"{_TEST_BANNER} (This is the auto-write digest Revert card.)",
    }
    return build_autowrite_digest_blocks([record])


def main() -> int:
    ap = argparse.ArgumentParser(description="Rider D D1 interactivity smoke test.")
    ap.add_argument("--send", action="store_true",
                    help="Actually DM the two cards to Harrison (default: dry-run print only).")
    args = ap.parse_args()

    stamp = str(int(time.time()))
    kr_text, kr_blocks = _knowledge_card(stamp)
    aw_text, aw_blocks = _digest_card(stamp)

    if not args.send:
        print("=== DRY RUN (no send). Pass --send to DM the two cards. ===\n")
        print("[1] knowledge one-tap card blocks:")
        print(json.dumps(kr_blocks, ensure_ascii=False, indent=2))
        print("\n[2] auto-write digest card blocks:")
        print(json.dumps(aw_blocks, ensure_ascii=False, indent=2))
        return 0

    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        print("ERROR: SLACK_BOT_TOKEN not set — cannot send.", file=sys.stderr)
        return 2

    client = _build_slack_client(token)
    dm = client.conversations_open(users=[HARRISON_SLACK_USER_ID])["channel"]["id"]

    r1 = client.chat_postMessage(channel=dm, text=kr_text, blocks=kr_blocks,
                                 unfurl_links=False, unfurl_media=False)
    print(f"[1] knowledge one-tap card sent: ts={r1.get('ts')} channel={dm}")

    r2 = client.chat_postMessage(channel=dm, text=aw_text, blocks=aw_blocks,
                                 unfurl_links=False, unfurl_media=False)
    print(f"[2] auto-write digest card sent: ts={r2.get('ts')} channel={dm}")

    print("\nTap ✅/👎 on card 1 and ↩️ Revert on card 2. Then check the live bot "
          "log for the handler + in-message ack (both sentinels return 'not_found', "
          "which is the expected safe outcome).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
