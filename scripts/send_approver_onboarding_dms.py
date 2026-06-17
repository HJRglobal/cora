"""Send personalized knowledge-approver onboarding DMs to the 9 designated approvers.

Run once after the KQ channels and approval routing are deployed:

    uv run python scripts/send_approver_onboarding_dms.py

Each person gets a DM from Cora that covers:
  - Their authorized entities
  - Two ways to teach Cora (note/remember command + 📚 reaction)
  - Where to review and approve pending submissions (their KQ channels)
  - The ✅ / ❌ approval mechanic

Safe to re-run: the script will DM each person exactly once per run.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
if not SLACK_BOT_TOKEN:
    print("ERROR: SLACK_BOT_TOKEN not set in .env", file=sys.stderr)
    sys.exit(1)

from slack_sdk import WebClient  # noqa: E402

client = WebClient(token=SLACK_BOT_TOKEN)

# ── Approver roster ──────────────────────────────────────────────────────────
# slack_user_id, first_name, entities (display), kq_channels (channel names)

APPROVERS = [
    {
        "slack_user_id": "U0B3AEQS0NB",
        "first_name": "Hannah",
        "entities": ["HJR Global", "FNDR"],
        "kq_channels": ["cora-kq-fndr", "cora-kq-hjrg"],
    },
    {
        "slack_user_id": "U0B3AEJCYGP",
        "first_name": "Justin",
        "entities": ["HJR Global", "FNDR", "Lexington Services (all entities)"],
        "kq_channels": ["cora-kq-fndr", "cora-kq-lex", "cora-kq-lex-llc", "cora-kq-lex-lts", "cora-kq-lex-lbhs", "cora-kq-lex-lla"],
    },
    {
        "slack_user_id": "U0B3PS82G30",
        "first_name": "Shaun",
        "entities": ["Lexington Services", "Lexington LLC", "Lexington Therapies", "Lexington LBHS", "Lex Life Academy"],
        "kq_channels": ["cora-kq-lex", "cora-kq-lex-llc", "cora-kq-lex-lts", "cora-kq-lex-lbhs", "cora-kq-lex-lla"],
    },
    {
        "slack_user_id": "U0B3RU5Q55G",
        "first_name": "Tommy",
        "entities": ["F3 Energy"],
        "kq_channels": ["cora-kq-f3e"],
    },
    {
        "slack_user_id": "U0B3VGWJTMJ",
        "first_name": "Alex",
        "entities": ["F3 Energy"],
        "kq_channels": ["cora-kq-f3e"],
    },
    {
        "slack_user_id": "U0B4L78SZHN",
        "first_name": "Micah",
        "entities": ["One Stop Nutrition (all 4 stores)", "BDM"],
        "kq_channels": ["cora-kq-osn", "cora-kq-osngm", "cora-kq-osnvv", "cora-kq-osngf", "cora-kq-osngw", "cora-kq-bdm"],
    },
    {
        "slack_user_id": "U0B3PS7RFJA",
        "first_name": "Matt",
        "entities": ["One Stop Nutrition (all 4 stores)"],
        "kq_channels": ["cora-kq-osn", "cora-kq-osngm", "cora-kq-osnvv", "cora-kq-osngf", "cora-kq-osngw"],
    },
    {
        "slack_user_id": "U0B3NGR1Y85",
        "first_name": "Larry",
        "entities": ["BDM"],
        "kq_channels": ["cora-kq-bdm"],
    },
    {
        "slack_user_id": "U0B3RU65TFU",
        "first_name": "Demi",
        "entities": ["BDM"],
        "kq_channels": ["cora-kq-bdm"],
    },
]


def _build_message(approver: dict) -> str:
    first = approver["first_name"]
    entity_bullets = "\n".join(f"  • {e}" for e in approver["entities"])
    kq_mentions = "  ".join(f"#cora-kq-{ch.split('cora-kq-')[1]}" if "cora-kq-" in ch else f"#{ch}" for ch in approver["kq_channels"])
    # Build KQ channel mentions properly
    kq_display = "  ".join(f"#{ch}" for ch in approver["kq_channels"])

    return f"""\
Hey {first}! :wave: You've been added as a Cora knowledge approver.

*Your authorized entities:*
{entity_bullets}

*Two ways to teach Cora something:*
1️⃣  In any channel Cora is in, say: `@Cora note: <what you want her to know>`
     Example: `@Cora note: our LLC lease renews June 30, 2027`
     You can also use `@Cora remember:` — same thing.
2️⃣  React :books: to any message — Cora will bookmark it for review automatically.

*Reviewing submissions:*
Pending contributions from your entities appear in:
  {kq_display}
React ✅ to approve → it enters Cora's knowledge base.
React ❌ to decline → it's discarded.

That's it! Questions? Ask in #cora-build."""


def main() -> None:
    sent = 0
    failed = 0

    for approver in APPROVERS:
        uid = approver["slack_user_id"]
        first = approver["first_name"]
        msg = _build_message(approver)

        try:
            # Open DM channel
            dm_resp = client.conversations_open(users=[uid])
            dm_channel = dm_resp["channel"]["id"]

            from cora.slack_egress import sanitize_text  # noqa: PLC0415 -- B1: WebClient sender imports no cora module; route through the boundary
            client.chat_postMessage(
                channel=dm_channel,
                text=sanitize_text(msg),
                unfurl_links=False,
                unfurl_media=False,
            )
            print(f"  ✓  DM sent → {first} ({uid})")
            sent += 1
        except Exception as exc:
            print(f"  ✗  Failed → {first} ({uid}): {exc}")
            failed += 1

        time.sleep(0.3)  # stay well inside Slack rate limits

    print(f"\nDone: {sent} sent, {failed} failed.")


if __name__ == "__main__":
    main()
