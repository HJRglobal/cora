#!/usr/bin/env python3
"""Deterministic egress-boundary smoke (Phase 2.1).

Posts ONE test message to a Slack channel through a real (egress-patched)
slack_sdk.WebClient so you can eyeball that the boundary:
  - PRESERVES a code-fenced / fixed-width table (alignment intact)
  - PRESERVES intentional signal emoji + an em-dash
  - PRESERVES a sanctioned <url|label> link
  - REDACTS a bare Drive URL and a gid/long-id

Usage (defaults to the #cora-build channel):
    .venv\\Scripts\\python.exe scripts\\smoke_egress.py
    .venv\\Scripts\\python.exe scripts\\smoke_egress.py --channel C0XXXXXXX
"""

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Importing cora installs the egress sanitizer (cora/__init__.py).
import cora  # noqa: F401,E402
from slack_sdk import WebClient  # noqa: E402

_DEFAULT_CHANNEL = "C0B4B0URRQS"  # #cora-build

_MESSAGE = (
    ":test_tube: *Egress boundary smoke* (Phase 2.1) -- this should render with the "
    "table aligned, the red dot + em-dash intact, the sanctioned link clickable, and "
    "the bare Drive URL + gid GONE.\n"
    "```\n"
    "Entity        Ending Cash\n"
    "F3 Energy           $1,680\n"
    "UFL                 $2,244\n"
    "```\n"
    ":red_circle: Status -- steady. Sanctioned link: <https://example.com|the doc>. "
    "Bare leak (should vanish): https://drive.google.com/file/d/SHOULD_BE_REDACTED  "
    "gid 1215472268404903 (should vanish)."
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", default=_DEFAULT_CHANNEL,
                        help="Slack channel ID to post to (default: #cora-build)")
    args = parser.parse_args()

    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        print("SLACK_BOT_TOKEN not set -- cannot post.")
        return 1

    resp = WebClient(token=token).chat_postMessage(
        channel=args.channel, text=_MESSAGE, unfurl_links=False, unfurl_media=False,
    )
    print(f"Posted to {args.channel} (ts={resp.get('ts')}). Open Slack and verify:")
    print("  - the 3-row table is column-aligned inside a code block")
    print("  - the red dot + em-dash are present")
    print("  - the <example.com|the doc> link is intact")
    print("  - the drive.google.com URL and the gid are GONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
