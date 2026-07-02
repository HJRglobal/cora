# ── RETIRED 2026-07-02 (hygiene session, Harrison-approved) -- do not run ────
# Superseded by gap_autofill -> the daily knowledge-review DM: both read
# logs/knowledge-gaps.jsonl, and per-gap surfacing to Harrison already happens
# there, so this weekly #hjrg-leadership rollup is a duplicate surface.
# The host task "cowork-cora-gap-digest" is disabled by Harrison AFTER the
# merge (host step); expected-disabled is recorded in
# data/maps/scheduled-task-state.yaml so the nightly health check stays clean.
# Kept for history; no other module imports this script.
"""Post a weekly knowledge-gap digest to #hjrg-leadership.

RETIRED 2026-07-02 -- see the header comment above; do not run or re-register.

Reads logs/knowledge-gaps.jsonl, aggregates the top gaps from the past 7 days
(or a custom window), and posts a concise Slack-formatted summary to
#hjrg-leadership. The full Drive digest is written separately by
generate_knowledge_gaps_digest.py — this script produces the lightweight
Slack hook that drives action in the channel.

Usage:
    python scripts/post_gap_digest_slack.py [--days N] [--dry-run]

Scheduled as "HJRG -- Weekly gap digest" every Monday 8am AZ via Windows
Task Scheduler. Register with:

    schtasks /Create /TN "cowork-cora-gap-digest" /TR
      "C:\\Users\\Harri\\code\\cora\\.venv\\Scripts\\python.exe
       C:\\Users\\Harri\\code\\cora\\scripts\\post_gap_digest_slack.py"
    /SC WEEKLY /D MON /ST 08:00 /F

Prerequisites:
    SLACK_BOT_TOKEN must be set in .env (same token as Cora).
    Bot must be a member of #hjrg-leadership.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG = REPO_ROOT / "logs" / "knowledge-gaps.jsonl"
TARGET_CHANNEL = "hjrg-leadership"
DEFAULT_DAYS = 7
MAX_GAPS_PER_ENTITY = 5  # show top N per entity in the Slack post


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Post weekly gap digest to Slack.")
    p.add_argument("--days", type=int, default=DEFAULT_DAYS,
                   help=f"Look-back window in days (default: {DEFAULT_DAYS})")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the Slack message to stdout instead of posting.")
    p.add_argument("--log", type=Path, default=DEFAULT_LOG)
    return p.parse_args()


def load_gaps(log_path: Path, since: datetime) -> list[dict]:
    if not log_path.exists():
        return []
    gaps: list[dict] = []
    with log_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                ts_str = rec.get("ts", "")
                if not ts_str:
                    continue
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts >= since:
                    gaps.append(rec)
            except (json.JSONDecodeError, ValueError):
                pass
    return gaps


def build_slack_blocks(gaps: list[dict], since: datetime, days: int) -> str:
    """Build a Slack mrkdwn string for the gap digest."""
    if not gaps:
        return (
            f":white_check_mark: *Cora Knowledge Gap Digest — last {days} days*\n"
            "No knowledge gaps this week. Cora answered everything from context."
        )

    # Group by entity
    by_entity: dict[str, list[dict]] = defaultdict(list)
    for g in gaps:
        by_entity[g.get("entity", "UNKNOWN")].append(g)

    # Count gaps per entity for the header
    entity_counts = {e: len(gs) for e, gs in by_entity.items()}
    total = sum(entity_counts.values())

    lines: list[str] = []
    date_str = since.strftime("%b %d")
    today_str = datetime.now(timezone.utc).strftime("%b %d")
    lines.append(f":brain: *Cora Knowledge Gap Digest* ({date_str} – {today_str})")
    lines.append(f"*{total} gap(s)* across *{len(by_entity)} entit(y/ies)*\n")

    # Per entity: show top gaps by frequency of similar questions
    entity_order = ["F3E", "LEX", "OSN", "BDM", "HJRG", "FNDR"]
    shown: set[str] = set()
    for entity in entity_order + sorted(by_entity.keys()):
        if entity in shown or entity not in by_entity:
            continue
        shown.add(entity)
        ent_gaps = by_entity[entity]

        lines.append(f"*{entity}* — {len(ent_gaps)} gap(s)")

        # Deduplicate by gap description similarity (simple: exact gap text)
        gap_counter: Counter[str] = Counter(g.get("gap", "")[:120] for g in ent_gaps)
        top = gap_counter.most_common(MAX_GAPS_PER_ENTITY)

        for gap_text, count in top:
            if not gap_text:
                continue
            count_label = f" ×{count}" if count > 1 else ""
            lines.append(f"  • {gap_text}{count_label}")

        if len(ent_gaps) > MAX_GAPS_PER_ENTITY:
            lines.append(
                f"  _…and {len(ent_gaps) - MAX_GAPS_PER_ENTITY} more — "
                "see Drive digest for full list_"
            )
        lines.append("")

    lines.append(
        ":point_right: React to a Cora response with :thumbsdown: to flag the answer, "
        "or use `@Cora note: <content>` to contribute knowledge directly."
    )
    return "\n".join(lines)


def find_channel_id(slack_client, channel_name: str) -> str | None:
    """Find a channel ID by name."""
    cursor = None
    while True:
        kwargs: dict = {"types": "public_channel,private_channel", "limit": 200}
        if cursor:
            kwargs["cursor"] = cursor
        resp = slack_client.conversations_list(**kwargs)
        for ch in resp.get("channels", []):
            if ch.get("name") == channel_name:
                return ch["id"]
        meta = resp.get("response_metadata", {})
        cursor = meta.get("next_cursor")
        if not cursor:
            break
    return None


def main() -> int:
    args = parse_args()

    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    gaps = load_gaps(args.log, since)

    msg = build_slack_blocks(gaps, since, args.days)

    if args.dry_run:
        print(msg)
        return 0

    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        print("ERROR: SLACK_BOT_TOKEN not set", file=sys.stderr)
        return 1

    try:
        from slack_sdk import WebClient
        client = WebClient(token=token)
    except ImportError:
        # slack_bolt is installed; use its underlying WebClient
        from slack_bolt.app import App  # type: ignore
        from slack_sdk import WebClient
        client = WebClient(token=token)

    channel_id = find_channel_id(client, TARGET_CHANNEL)
    if not channel_id:
        print(f"ERROR: Could not find #{TARGET_CHANNEL}", file=sys.stderr)
        return 1

    from cora.slack_egress import sanitize_text  # noqa: PLC0415 -- B1: WebClient sender imports no cora module; route through the boundary
    resp = client.chat_postMessage(
        channel=channel_id,
        text=sanitize_text(msg),
        unfurl_links=False,
        unfurl_media=False,
    )
    print(f"Posted to #{TARGET_CHANNEL} ts={resp.get('ts')}")
    print(f"  {len(gaps)} gaps from last {args.days} days")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
