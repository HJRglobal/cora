"""One-shot script: DM every knowledge contributor their capabilities + tutorial.

Run once after #cora-kq-* channels are created and contributors.yaml is finalized:

    uv run python scripts/notify_contributors.py

Reads data/maps/knowledge-contributors.yaml and sends a personalized Slack DM
to every approver/contributor except Harrison (who already knows the system).
Uses SLACK_BOT_TOKEN from .env.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

load_dotenv()

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CONTRIBUTORS_YAML = _REPO_ROOT / "data" / "maps" / "knowledge-contributors.yaml"

# Harrison's Slack ID — skip him (he built the system)
_HARRISON_ID = "U0B2RM2JYJ1"

# Entity code → friendly display name
_ENTITY_LABELS: dict[str, str] = {
    "FNDR":     "Founder / FNDR",
    "HJRG":     "HJR Global",
    "F3E":      "F3 Energy",
    "LEX":      "Lexington Services (all)",
    "LEX-LLC":  "Lexington LLC",
    "LEX-LTS":  "Lexington Therapies",
    "LEX-LBHS": "Lexington LBHS",
    "LEX-LLA":  "Lexington LLA",
    "OSN":      "One Stop Nutrition",
    "OSNGM":    "OSN — Gilbert & McKellips",
    "OSNVV":    "OSN — Val Vista & Pecos",
    "OSNGF":    "OSN — Greenfield & 60",
    "OSNGW":    "OSN — Gilbert & Warner",
    "BDM":      "Big D Media",
}


def _entity_label(code: str) -> str:
    return _ENTITY_LABELS.get(code, code)


def _queue_channel(entity: str) -> str:
    return f"cora-kq-{entity.lower()}"


def _build_message(name: str, entities: list[str], tier: str) -> str:
    first = name.split()[0]

    # Build entity bullet list
    entity_lines = "\n".join(f"  • {_entity_label(e)}" for e in entities)

    # Build queue channel list (deduplicated, sorted)
    queue_channels = sorted({_queue_channel(e) for e in entities})
    queue_lines = "  " + "  ".join(f"`#{ch}`" for ch in queue_channels)

    contribute_section = (
        "*Two ways to teach Cora something:*\n"
        "1️⃣  In any channel for your entity, type:\n"
        "    `@Cora remember: [what Cora should know]`\n"
        "    _Example: `@Cora remember: our LLC lease renews June 30, 2027`_\n\n"
        "2️⃣  React 📚 to *any message* in one of your channels — Cora will bookmark it automatically.\n"
    )

    if tier == "approver":
        review_section = (
            "*Reviewing submissions:*\n"
            f"You'll see pending contributions from your team in your queue channel(s):\n"
            f"{queue_lines}\n\n"
            "React ✅ on a queued card to approve it → it enters Cora's knowledge base immediately.\n"
            "React ❌ to decline → nothing is added."
        )
    else:
        review_section = (
            "*Your submissions:*\n"
            "Everything you submit goes to an approver for review before entering Cora's knowledge base. "
            "You'll see a confirmation in the channel when you submit."
        )

    return (
        f"Hey {first}! 👋 You've been added as a Cora *knowledge {tier}*.\n\n"
        f"*Your authorized entities:*\n{entity_lines}\n\n"
        f"{contribute_section}\n"
        f"{review_section}\n\n"
        "That's it — the more your team teaches Cora, the smarter she gets for your entity. "
        "Questions? Ask Harrison or just `@Cora` anything."
    )


def main() -> None:
    token = os.getenv("SLACK_BOT_TOKEN")
    if not token:
        print("ERROR: SLACK_BOT_TOKEN not set in .env", file=sys.stderr)
        sys.exit(1)

    data = yaml.safe_load(_CONTRIBUTORS_YAML.read_text())
    contributors: dict[str, dict] = data.get("contributors", {})

    client = WebClient(token=token)
    sent = 0
    skipped = 0

    for uid, entry in contributors.items():
        if uid == _HARRISON_ID:
            skipped += 1
            continue

        name = entry.get("name", uid)
        tier = entry.get("tier", "contributor")
        entities: list[str] = entry.get("entities", [])

        if not entities:
            print(f"  SKIP {name} — no entities listed")
            skipped += 1
            continue

        message = _build_message(name, entities, tier)

        try:
            client.chat_postMessage(
                channel=uid,   # DM to user ID directly
                text=message,
                unfurl_links=False,
                unfurl_media=False,
            )
            print(f"  ✅  Sent to {name} ({uid}) — {len(entities)} entities")
            sent += 1
        except SlackApiError as exc:
            print(f"  ❌  Failed for {name} ({uid}): {exc.response['error']}")

        time.sleep(0.5)  # stay well under Slack rate limit (1 req/sec Tier 1)

    print(f"\nDone — {sent} sent, {skipped} skipped (Harrison + no-entity entries).")


if __name__ == "__main__":
    main()
