#!/usr/bin/env python3
"""Weekly feedback health report — reads cora-user-feedback.jsonl and posts
entity-level signal patterns to #hjrg-leadership.

Shows:
  - Which entities generated the most corrections (Cora's weakest KB areas)
  - Top repeated knowledge gaps per entity (what to teach her next)
  - Thumbsdown frequency per entity
  - Top correctors by name (they know things Cora doesn't)

Scheduled as: cowork-cora-feedback-health  Every Monday 8:30am AZ
Run manually:  python scripts/run_feedback_health_report.py [--days N] [--dry-run]
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

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

_FEEDBACK_LOG = _REPO_ROOT / "logs" / "cora-user-feedback.jsonl"
_GAPS_LOG      = _REPO_ROOT / "logs" / "knowledge-gaps.jsonl"
_NOTIFY_CH     = "hjrg-leadership"
_DEFAULT_DAYS  = 7

_ENTITY_LABELS = {
    "F3E": "F3 Energy",
    "LEX": "Lexington",
    "OSN": "One Stop Nutrition",
    "BDM": "Big D Media",
    "HJRG": "HJR Global",
    "FNDR": "Founder",
    "UFL": "UFL",
    "HJRP": "HJR Properties",
}


def _label(entity: str) -> str:
    return _ENTITY_LABELS.get(entity, entity)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Post weekly Cora feedback health report.")
    p.add_argument("--days", type=int, default=_DEFAULT_DAYS)
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def _load_jsonl(path: Path, since: datetime) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open(encoding="utf-8") as fh:
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
                    out.append(rec)
            except (json.JSONDecodeError, ValueError):
                pass
    return out


def _build_report(feedback: list[dict], gaps: list[dict], days: int) -> str:
    if not feedback and not gaps:
        return (
            f":white_check_mark: *Cora Feedback Health — last {days} days*\n"
            "No corrections, thumbsdowns, or knowledge gaps recorded this week."
        )

    # ── Corrections by entity ──────────────────────────────────────────────────
    corrections = [f for f in feedback if f.get("signal_type") == "correction"]
    thumbsdowns  = [f for f in feedback if f.get("signal_type") == "thumbsdown"]

    corrections_by_entity: Counter = Counter(f.get("entity", "?") for f in corrections)
    thumbsdown_by_entity: Counter  = Counter(f.get("entity", "?") for f in thumbsdowns)

    # Top correctors by display name
    corrector_counts: Counter = Counter(
        f.get("display_name") or f.get("slack_user_id", "?") for f in corrections
    )

    # ── Knowledge gaps by entity ───────────────────────────────────────────────
    gaps_by_entity: dict[str, list[str]] = defaultdict(list)
    for g in gaps:
        entity = g.get("entity", "?")
        gap_desc = g.get("gap", "")
        if gap_desc:
            gaps_by_entity[entity].append(gap_desc)

    # Top repeated gap topics (simple: most common leading 6 words)
    def _topic_key(gap_text: str) -> str:
        return " ".join(gap_text.lower().split()[:6])

    top_gaps_by_entity: dict[str, list[tuple[str, int]]] = {}
    for entity, gap_list in gaps_by_entity.items():
        counter: Counter = Counter(_topic_key(g) for g in gap_list)
        top_gaps_by_entity[entity] = counter.most_common(3)

    # ── Build Slack message ───────────────────────────────────────────────────
    lines = [f":bar_chart: *Cora Feedback Health — last {days} days*\n"]

    total_corrections = len(corrections)
    total_thumbsdowns = len(thumbsdowns)
    total_gaps = len(gaps)
    lines.append(
        f"*Summary:* {total_corrections} correction(s) · "
        f"{total_thumbsdowns} :thumbsdown: · "
        f"{total_gaps} knowledge gap(s)\n"
    )

    # Corrections by entity
    if corrections_by_entity:
        lines.append("*Corrections by entity* (where Cora is most often wrong):")
        for entity, count in corrections_by_entity.most_common(5):
            lines.append(f"  • {_label(entity)}: {count}")
        lines.append("")

    # Top correctors
    if corrector_counts:
        top = corrector_counts.most_common(3)
        lines.append("*Top correctors* (they know things Cora doesn't — consider registering them as contributors):")
        for name, count in top:
            lines.append(f"  • {name}: {count} correction(s)")
        lines.append("")

    # Thumbsdowns by entity
    if thumbsdown_by_entity:
        lines.append("*Thumbsdowns by entity:*")
        for entity, count in thumbsdown_by_entity.most_common(5):
            lines.append(f"  • {_label(entity)}: {count}")
        lines.append("")

    # Top gap topics per entity
    if top_gaps_by_entity:
        lines.append("*Top knowledge gaps by entity* (teach Cora these first):")
        for entity in sorted(top_gaps_by_entity.keys()):
            topics = top_gaps_by_entity[entity]
            total_entity_gaps = len(gaps_by_entity[entity])
            lines.append(f"  *{_label(entity)}* ({total_entity_gaps} gap(s)):")
            for topic, count in topics:
                suffix = f" ×{count}" if count > 1 else ""
                lines.append(f"    - _{topic}…_{suffix}")
        lines.append("")

    lines.append(
        "_To teach Cora: `@Cora remember: [fact]` in any entity channel, "
        "or react :books: to any message._"
    )

    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    since = datetime.now(timezone.utc) - timedelta(days=args.days)

    feedback = _load_jsonl(_FEEDBACK_LOG, since)
    gaps     = _load_jsonl(_GAPS_LOG, since)

    report = _build_report(feedback, gaps, args.days)

    if args.dry_run:
        print(report)
        return 0

    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        print("ERROR: SLACK_BOT_TOKEN not set", file=sys.stderr)
        return 1

    try:
        from slack_sdk import WebClient
        from cora.slack_egress import sanitize_text  # noqa: PLC0415 -- B1: WebClient sender imports no cora module; route through the boundary
        WebClient(token=token).chat_postMessage(
            channel=_NOTIFY_CH,
            text=sanitize_text(report),
            unfurl_links=False,
            unfurl_media=False,
        )
        print(f"Posted feedback health report to #{_NOTIFY_CH} ({len(feedback)} signals, {len(gaps)} gaps)")
        return 0
    except Exception as exc:
        print(f"ERROR posting to Slack: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
