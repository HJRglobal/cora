#!/usr/bin/env python3
"""Nightly org-wide channel sweep — synthesizes per-user activity across all public channels.

What this does:
  1. Reads last 24h of messages from every public channel Cora is a member of
  2. Groups messages by user (skips bots/system events)
  3. Uses Claude Haiku to extract per-user: commitments, decisions, open questions,
     cross-entity mentions
  4. Writes data/channel-sweep/sweep-YYYY-MM-DD.json

The output is consumed by:
  - run_daily_briefing.py (per-user cross-channel context in morning briefing)
  - run_reconciliation.py (pass 6: org-wide commitment tracking)

Run:
    uv run python scripts/run_channel_sweep.py
    uv run python scripts/run_channel_sweep.py --dry-run
    uv run python scripts/run_channel_sweep.py --lookback-hours 48

Register as nightly task:
    .\\deployment\\setup-channel-sweep-task.ps1

Exit codes: 0 = success, 1 = error
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import asdict
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("channel_sweep")


def main() -> int:
    parser = argparse.ArgumentParser(description="Nightly org-wide channel sweep")
    parser.add_argument("--dry-run", action="store_true", help="Sweep channels but skip Haiku synthesis")
    parser.add_argument("--lookback-hours", type=float, default=24.0, help="Hours of history to sweep (default 24)")
    args = parser.parse_args()

    from slack_sdk import WebClient
    import anthropic as _anthropic

    slack_token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not slack_token:
        log.error("SLACK_BOT_TOKEN not set")
        return 1

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    anthropic_client = None
    if anthropic_key and not args.dry_run:
        try:
            anthropic_client = _anthropic.Anthropic(api_key=anthropic_key)
        except Exception as exc:
            log.warning("Could not build Anthropic client — synthesis disabled: %s", exc)
    elif args.dry_run:
        log.info("Dry run — Haiku synthesis disabled")
    else:
        log.warning("ANTHROPIC_API_KEY not set — synthesis disabled")

    client = WebClient(token=slack_token)

    from cora.connectors.channel_sweep import run_sweep

    log.info("=== Channel Sweep starting%s (%.0fh lookback) ===",
             " [DRY RUN]" if args.dry_run else "", args.lookback_hours)

    result = run_sweep(
        client=client,
        anthropic_client=anthropic_client,
        lookback_hours=args.lookback_hours,
        dry_run=args.dry_run,
    )

    log.info("=== Sweep complete ===")
    log.info("  Channels swept : %d", result.channels_swept)
    log.info("  Users active   : %d", result.users_active)
    if result.errors:
        log.warning("  Errors         : %d", len(result.errors))

    # Log per-user summary
    for uid, activity in result.user_activity.items():
        name = activity.display_name or uid
        total = len(activity.commitments) + len(activity.decisions) + len(activity.open_questions)
        if total:
            log.info(
                "  %-25s  commitments=%d  decisions=%d  questions=%d",
                name[:25], len(activity.commitments), len(activity.decisions), len(activity.open_questions),
            )

    # Write output JSON
    out_dir = _REPO_ROOT / "data" / "channel-sweep"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"sweep-{date.today().isoformat()}.json"

    output = {
        "swept_at": result.swept_at,
        "channels_swept": result.channels_swept,
        "users_active": result.users_active,
        "dry_run": args.dry_run,
        "users": {
            uid: {
                "display_name": a.display_name,
                "message_count": len(a.messages),
                "commitments": a.commitments,
                "decisions": a.decisions,
                "open_questions": a.open_questions,
                "cross_entity_mentions": a.cross_entity_mentions,
            }
            for uid, a in result.user_activity.items()
        },
        "errors": result.errors,
    }
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Output written to %s", out_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
