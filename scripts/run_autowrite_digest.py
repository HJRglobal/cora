#!/usr/bin/env python3
r"""Weekly "Cora auto-learned this week" digest (§7B oversight-after-the-fact).

Since the graduated-trust flip (CORA_AUTOWRITE_LIVE) lets Cora auto-write Tier-0/1
knowledge without a per-item Harrison gate, THIS is the oversight surface:
a weekly DM to Harrison of every auto-write, with a one-tap Revert per item, plus
week-over-week counts so drift is visible. Reversibility + audit replace the gate.

For the first ~4 weeks after the flip, this digest IS the validation (the shadow
produced zero Tier-0/1 track record, so the flip rests on the conservative tier
scoping + reversibility, not a shadow verdict -- watch these counts).

Runs weekly (Mon). Reads logs/cora-autowrite-audit.jsonl (written by
knowledge_review.apply_autowrite). DMs Harrison ONLY. Fail-soft.

Usage:
    .venv\Scripts\python.exe scripts\run_autowrite_digest.py            # DM if any activity
    .venv\Scripts\python.exe scripts\run_autowrite_digest.py --dry-run  # print, no DM
    .venv\Scripts\python.exe scripts\run_autowrite_digest.py --force    # DM even if quiet
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import os  # noqa: E402

from cora import knowledge_review as kr  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("autowrite-digest")

_DAY = 86400.0


def _ts(rec: dict) -> float:
    try:
        return datetime.fromisoformat(str(rec.get("ts", ""))).timestamp()
    except ValueError:
        return 0.0


def _is_autowrite(rec: dict) -> bool:
    return str(rec.get("decision_reason", "")).startswith("auto_")


def build_digest(now_ts: float, days: int = 7) -> tuple[dict, list[dict]]:
    """Return (stats, this_week_autowrites). this_week = non-reverted auto-writes
    in the last `days`."""
    records = kr.read_autowrite_audit()
    since_1 = now_ts - days * _DAY
    since_2 = now_ts - 2 * days * _DAY
    # index reverts by update_id so we can flag/skip already-reverted items
    reverted_ids = {r.get("update_id") for r in records
                    if r.get("decision_reason") == "revert"}
    this_week, prev_week, reverts_this_week = [], 0, 0
    for r in records:
        t = _ts(r)
        if r.get("decision_reason") == "revert":
            if t >= since_1:
                reverts_this_week += 1
            continue
        if not _is_autowrite(r):
            continue
        if t >= since_1:
            if r.get("update_id") not in reverted_ids:
                this_week.append(r)
        elif since_2 <= t < since_1:
            prev_week += 1
    stats = {
        "this_week": len(this_week),
        "prev_week": prev_week,
        "reverts_this_week": reverts_this_week,
        "level": kr.autowrite_level(),
    }
    return stats, this_week


def deliver(stats: dict, items: list[dict]) -> bool:
    from slack_sdk import WebClient

    from cora import slack_egress
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        log.error("autowrite-digest: SLACK_BOT_TOKEN not set -- cannot DM")
        return False
    fallback, blocks = kr.build_autowrite_digest_blocks(items)
    summary = (f"{fallback}\n_This week {stats['this_week']} · last week "
               f"{stats['prev_week']} · reverts this week {stats['reverts_this_week']} · "
               f"level={stats['level']}_")
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": summary}}] + blocks[1:]
    try:
        safe_fallback = slack_egress.sanitize_text(summary)
    except Exception:  # noqa: BLE001
        safe_fallback = summary
    try:
        client = WebClient(token=token)
        resp = client.conversations_open(users=[kr.HARRISON_SLACK_USER_ID])
        client.chat_postMessage(channel=resp["channel"]["id"], text=safe_fallback[:3000], blocks=blocks)
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("autowrite-digest: DM failed: %s", exc)
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Weekly Cora auto-write digest (DM to Harrison).")
    ap.add_argument("--dry-run", action="store_true", help="Print, do not DM.")
    ap.add_argument("--force", action="store_true", help="DM even if there was no activity.")
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()

    now_ts = datetime.now(timezone.utc).timestamp()
    stats, items = build_digest(now_ts, days=args.days)
    log.info("autowrite-digest: this_week=%d prev_week=%d reverts=%d level=%s",
             stats["this_week"], stats["prev_week"], stats["reverts_this_week"], stats["level"])

    if not items and stats["reverts_this_week"] == 0 and not args.force:
        log.info("No auto-write activity this week -- no DM (use --force to send anyway).")
        return 0
    if args.dry_run:
        for it in items:
            log.info("[DRY RUN] %s tier=%s %s", it.get("update_type"), it.get("tier"),
                     str(it.get("summary", ""))[:120])
        return 0
    ok = deliver(stats, items)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
