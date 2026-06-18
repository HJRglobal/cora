#!/usr/bin/env python3
"""Slack Channel Health Monitor -- weekly report on dead/unmapped channels.

Fires weekly on Sunday at 04:00 UTC (9pm AZ Saturday).
Posts a digest to #hjrg-leadership.

Usage (Windows Task Scheduler):
    python scripts/run_channel_health_monitor.py [--dry-run]

Environment variables required:
    SLACK_BOT_TOKEN    For reading channel history and posting

"Unmapped" is decided by the canonical router (design/channel-routing.yaml via
cora.entity_router.is_mapped), NOT by data/maps/entity-channels.yaml. The old
entity-channels.yaml check held only ~22 leadership/finance IDs, so almost every
well-routed operational/sub channel read "unmapped" (~10x inflated). A channel is
unmapped now only when it matches just the trailing "*" catch-all route.

The report also self-stages archive candidates from the 2026-06-03 channel-sprawl
wave (TOM 0n): "-N" duplicate channels whose base channel exists, plus channels
the Cora bot created on/after that date that are now dead. Cora never archives --
Harrison executes every archive (gate G-F).
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

load_dotenv(_REPO_ROOT / ".env")

from cora import entity_router  # noqa: E402
from cora.connectors.slack_connector import list_joined_channels, SlackConnectorError  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("channel_health_monitor")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HJRG_LEADERSHIP_CHANNEL = "C0B3K67J10T"
DEAD_WINDOW_DAYS = 30
RATE_LIMIT_SLEEP = 0.3  # seconds between conversations_history calls
_PREVIEW_N = 15  # cap inline channel lists; the full list goes to a file (audit N9)

# Cora's own bot user id. Channels this user created on/after the 2026-06-03
# sprawl date (TOM 0n: "~97 public channels mass-created by Cora") are sprawl
# candidates; legitimate channels were created by Harrison / HJR Channel Bot.
CORA_BOT_USER_ID = "U0B44MDGC5R"
_AZ_TZ = timezone(timedelta(hours=-7))  # Arizona = UTC-7 year-round (no DST)
SPRAWL_CUTOFF = datetime(2026, 6, 3, tzinfo=_AZ_TZ)
SPRAWL_CUTOFF_EPOCH = SPRAWL_CUTOFF.timestamp()

# "-2".."-9" auto-dedup suffix on a channel whose base also exists. Single-digit
# 2-9 only, so building/store codes like #hjrp-1337 (multi-digit, not dash-prefixed
# on the final digit) are never matched. Linear regex (no nested quantifier).
_DUP_SUFFIX_RE = re.compile(r"^(?P<base>.+)-[2-9]$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _check_channel_activity(slack_client, channel_id: str, lookback_seconds: int) -> bool:
    """Return True if channel has at least 1 message in the lookback window."""
    try:
        oldest = time.time() - lookback_seconds
        resp = slack_client.conversations_history(
            channel=channel_id,
            limit=1,
            oldest=str(oldest),
        )
        messages = resp.get("messages", [])
        return len(messages) > 0
    except Exception as exc:
        log.warning("conversations_history failed for %s: %s", channel_id, exc)
        return True  # Assume active on error (don't flag as dead)


def _find_duplicate_channels(channels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return channels named '<base>-N' (N in 2-9) whose '<base>' is also joined.

    This is the 2026-06-03 sprawl '-2' duplicate pattern (TOM 0n: #osn-2,
    #f3-events-2, #retail-portfolio-2, ...). Name-only -- no API call.
    """
    names = {ch.get("name") for ch in channels if ch.get("name")}
    dups: list[dict[str, Any]] = []
    for ch in channels:
        name = ch.get("name") or ""
        m = _DUP_SUFFIX_RE.match(name)
        if m and m.group("base") in names:
            dups.append({"id": ch["id"], "name": name, "base": m.group("base")})
    return dups


def _find_sprawl_channels(channels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return channels the Cora bot created on/after the 2026-06-03 sprawl date.

    Precise discriminator: creator == Cora bot AND created >= cutoff. Legitimate
    channels were created by Harrison / HJR Channel Bot, or by Cora before the
    sprawl (e.g. #cora-kq-* on 2026-05-30). Needs `created`+`creator` from
    list_joined_channels (None when Slack omits them -> skipped).
    """
    out: list[dict[str, Any]] = []
    for ch in channels:
        creator = ch.get("creator")
        created = ch.get("created")
        if (
            creator == CORA_BOT_USER_ID
            and isinstance(created, (int, float))
            and not isinstance(created, bool)
            and created >= SPRAWL_CUTOFF_EPOCH
        ):
            out.append({"id": ch["id"], "name": ch.get("name", ch["id"]), "created": int(created)})
    return out


def _build_archive_candidates(
    duplicates: list[dict[str, Any]],
    sprawl: list[dict[str, Any]],
    dead_ids: set[str],
) -> list[dict[str, Any]]:
    """High-confidence archive candidates for the self-staging report (gate G-F).

    Conservative on purpose -- this feeds an automated weekly post, so it must not
    cry wolf:
      - every "-N" duplicate (structurally redundant with an existing channel),
        annotated dead/active so Harrison sees whether the dup is the live one;
      - every sprawl channel that is ALSO dead (created in the 2026-06-03 wave and
        since abandoned). Sprawl channels that are still active are EXCLUDED -- the
        archive doc warns some were adopted for real work.
    Deduped by id (a dup that's also dead-sprawl appears once, as a duplicate).
    """
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for d in duplicates:
        status = f"dead {DEAD_WINDOW_DAYS}d" if d["id"] in dead_ids else "active"
        candidates.append({
            "id": d["id"],
            "name": d["name"],
            "reason": f"duplicate of #{d['base']} ({status})",
        })
        seen.add(d["id"])
    for s in sprawl:
        if s["id"] in seen or s["id"] not in dead_ids:
            continue
        candidates.append({
            "id": s["id"],
            "name": s["name"],
            "reason": f"Cora-created 2026-06-03 sprawl, dead {DEAD_WINDOW_DAYS}d",
        })
        seen.add(s["id"])
    return candidates


def build_report(
    checked: int,
    dead_channels: list[dict[str, Any]],
    unmapped_channels: list[dict[str, Any]],
    archive_candidates: list[dict[str, Any]] | None = None,
    full_report_path: str | None = None,
) -> str:
    """Build the Slack message for the health report.

    Inline lists are capped at _PREVIEW_N (audit N9: the raw dump of dead +
    unmapped channel IDs was a 400+ line wall). The complete list is written to a
    file and referenced here instead of dumped.
    """
    archive_candidates = archive_candidates or []
    today = date.today().isoformat()
    lines = [f":health: *Channel Health Report -- {today}*", ""]

    def _section(items, header_active, header_empty, suffix):
        if not items:
            lines.append(header_empty)
            lines.append("")
            return
        lines.append(header_active)
        for ch in items[:_PREVIEW_N]:
            lines.append(f"  - #{ch['name']} ({ch['id']}) -- {suffix}")
        extra = len(items) - _PREVIEW_N
        if extra > 0:
            tail = f"  ...and {extra} more"
            if full_report_path:
                tail += f" (full list: {full_report_path})"
            lines.append(tail)
        lines.append("")

    # Archive candidates lead the report -- this is the actionable, self-staged
    # output (each item carries its own reason, so it doesn't use _section).
    if archive_candidates:
        lines.append(
            f":wastebasket: *Archive candidates (review before archiving) -- "
            f"{len(archive_candidates)} total:*"
        )
        for ch in archive_candidates[:_PREVIEW_N]:
            lines.append(f"  - #{ch['name']} ({ch['id']}) -- {ch['reason']}")
        extra = len(archive_candidates) - _PREVIEW_N
        if extra > 0:
            tail = f"  ...and {extra} more"
            if full_report_path:
                tail += f" (full list: {full_report_path})"
            lines.append(tail)
        lines.append("")
    else:
        lines.append(":wastebasket: *Archive candidates:* none")
        lines.append("")

    _section(
        dead_channels,
        f":zzz: *Dead channels (0 messages in {DEAD_WINDOW_DAYS}d) -- {len(dead_channels)} total:*",
        f":zzz: *Dead channels:* none -- all channels active in {DEAD_WINDOW_DAYS}d",
        "consider archiving",
    )
    _section(
        unmapped_channels,
        f":question: *Unmapped channels (no route in channel-routing.yaml) -- {len(unmapped_channels)} total:*",
        ":question: *Unmapped channels:* none -- every joined channel has an entity route",
        "add a route to design/channel-routing.yaml",
    )

    healthy = checked - len(dead_channels)
    lines.append(
        f":white_check_mark: {healthy} channels healthy | "
        f"{len(dead_channels)} dead | "
        f"{len(unmapped_channels)} unmapped | "
        f"{len(archive_candidates)} archive candidates"
    )

    return "\n".join(lines)


def _write_full_list(
    dead_channels: list[dict[str, Any]],
    unmapped_channels: list[dict[str, Any]],
    duplicates: list[dict[str, Any]],
    sprawl: list[dict[str, Any]],
    archive_candidates: list[dict[str, Any]],
) -> Path:
    """Write the complete signal lists to a dated file so the Slack post can stay a
    summary (audit N9). Returns the file path."""
    out_dir = _REPO_ROOT / "logs"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"channel-health-{date.today().isoformat()}.md"
    out = [f"# Channel Health Report -- {date.today().isoformat()}", ""]

    out.append(f"## Archive candidates (review before archiving) -- {len(archive_candidates)}")
    out.extend(f"- #{ch['name']} ({ch['id']}) -- {ch['reason']}" for ch in archive_candidates)
    out.append("")
    out.append(f"## Dead channels (0 messages in {DEAD_WINDOW_DAYS}d) -- {len(dead_channels)}")
    out.extend(f"- #{ch['name']} ({ch['id']})" for ch in dead_channels)
    out.append("")
    out.append(f"## Unmapped channels (no route in channel-routing.yaml) -- {len(unmapped_channels)}")
    out.extend(f"- #{ch['name']} ({ch['id']})" for ch in unmapped_channels)
    out.append("")
    out.append(f"## '-N' duplicate channels (base also exists) -- {len(duplicates)}")
    out.extend(f"- #{ch['name']} ({ch['id']}) -- duplicate of #{ch['base']}" for ch in duplicates)
    out.append("")
    out.append(
        f"## Cora-created since 2026-06-03 (sprawl; may be adopted -- confirm before archiving) "
        f"-- {len(sprawl)}"
    )
    out.extend(f"- #{ch['name']} ({ch['id']})" for ch in sprawl)
    out.append("")
    path.write_text("\n".join(out), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(dry_run: bool = False) -> dict[str, int]:
    from slack_sdk import WebClient

    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not bot_token:
        log.error("SLACK_BOT_TOKEN not set")
        return {"channels_checked": 0, "dead": 0, "missing": 0}

    slack_client = WebClient(token=bot_token)

    try:
        all_channels = list_joined_channels()
    except SlackConnectorError as exc:
        log.error("Failed to list channels: %s", exc)
        return {"channels_checked": 0, "dead": 0, "missing": 0}

    # Filter to non-DM, non-MPIM channels only
    channels = [
        ch for ch in all_channels
        if not ch.get("is_im") and not ch.get("is_mpim")
    ]

    dead_channels: list[dict[str, Any]] = []
    unmapped_channels: list[dict[str, Any]] = []
    lookback_sec = DEAD_WINDOW_DAYS * 86400

    for ch in channels:
        ch_id   = ch["id"]
        ch_name = ch.get("name", ch_id)

        # Dead check
        time.sleep(RATE_LIMIT_SLEEP)
        active = _check_channel_activity(slack_client, ch_id, lookback_sec)
        if not active:
            log.info("Dead channel: #%s (%s)", ch_name, ch_id)
            dead_channels.append({"id": ch_id, "name": ch_name})

        # Unmapped = matches ONLY the catch-all (no dedicated entity route).
        # Uses the canonical router, NOT entity-channels.yaml.
        if not entity_router.is_mapped(ch_name):
            log.debug("Unmapped channel: #%s (%s)", ch_name, ch_id)
            unmapped_channels.append({"id": ch_id, "name": ch_name})

    duplicates = _find_duplicate_channels(channels)
    sprawl = _find_sprawl_channels(channels)
    dead_ids = {d["id"] for d in dead_channels}
    archive_candidates = _build_archive_candidates(duplicates, sprawl, dead_ids)

    channels_checked = len(channels)
    full_path = _write_full_list(
        dead_channels, unmapped_channels, duplicates, sprawl, archive_candidates
    )
    report = build_report(
        channels_checked,
        dead_channels,
        unmapped_channels,
        archive_candidates=archive_candidates,
        full_report_path=f"logs/{full_path.name}",
    )

    if dry_run:
        log.info("[DRY RUN] Would post to %s:\n%s", HJRG_LEADERSHIP_CHANNEL, report)
    else:
        try:
            slack_client.chat_postMessage(
                channel=HJRG_LEADERSHIP_CHANNEL,
                text=report,
            )
            log.info(
                "Channel health report posted: %d checked, %d dead, %d unmapped, "
                "%d archive candidates",
                channels_checked, len(dead_channels), len(unmapped_channels),
                len(archive_candidates),
            )
        except Exception as exc:
            log.error("Failed to post report: %s", exc)

    return {
        "channels_checked": channels_checked,
        "dead": len(dead_channels),
        "missing": len(unmapped_channels),  # "missing" kept for backward-compat; == unmapped
        "duplicates": len(duplicates),
        "sprawl": len(sprawl),
        "archive_candidates": len(archive_candidates),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Slack Channel Health Monitor")
    parser.add_argument("--dry-run", action="store_true", help="Log without posting")
    args = parser.parse_args()

    result = run(dry_run=args.dry_run)
    log.info("channel_health_monitor result: %s", result)
