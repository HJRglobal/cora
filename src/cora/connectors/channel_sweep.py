"""Org-wide public channel sweep for nightly synthesis.

Reads the last N hours of messages from every public channel Cora is a member of,
groups them by user, and uses Claude Haiku to extract per-user commitments,
decisions, and open questions. Output feeds the nightly reconciliation pass 6
and per-user daily briefings.

Design notes:
- Only reads channels Cora has joined (public only — private channels require invite).
- Skips bot messages, join/leave events, and channels with no human activity.
- Rate-limit aware: Slack Tier 3 = 50 req/min; 1.2s sleep between channel fetches.
- Each user's messages are batched across all channels before synthesis to give
  Haiku full cross-channel context per person.

Required scopes: channels:read, channels:history, channels:join (for bootstrap).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_CHANNEL_FETCH_SLEEP = 1.2   # seconds between channel fetches (Tier-3 rate limit)
_MAX_MSG_PER_CHANNEL = 500   # cap per channel per run
_SYNTHESIS_MODEL = "claude-haiku-4-5-20251001"

# Channels excluded from sweep (noise / irrelevant to synthesis)
_EXCLUDED_CHANNEL_NAMES = frozenset({
    "general", "random", "announcements", "cora-build",
})


@dataclass
class UserActivity:
    slack_user_id: str
    display_name: str = ""
    messages: list[dict] = field(default_factory=list)   # {channel, channel_name, ts, text}
    commitments: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    cross_entity_mentions: list[str] = field(default_factory=list)


@dataclass
class ChannelSweepResult:
    swept_at: str
    channels_swept: int
    users_active: int
    user_activity: dict[str, UserActivity]   # slack_user_id → UserActivity
    errors: list[str] = field(default_factory=list)


# ── Channel listing + history ───────────────────────────────────────────────────

def list_joined_channels(client) -> list[dict]:
    """Return all public channels Cora is currently a member of."""
    channels: list[dict] = []
    cursor = None
    while True:
        kwargs: dict = {"types": "public_channel", "limit": 200, "exclude_archived": True}
        if cursor:
            kwargs["cursor"] = cursor
        try:
            resp = client.conversations_list(**kwargs)
        except Exception as exc:
            log.warning("channel_sweep: conversations.list failed: %s", exc)
            break
        for ch in resp.get("channels", []):
            if ch.get("is_member"):
                channels.append({"id": ch["id"], "name": ch.get("name", "")})
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
        time.sleep(0.3)
    return channels


def fetch_channel_messages(client, channel_id: str, oldest_ts: str) -> list[dict]:
    """Fetch up to _MAX_MSG_PER_CHANNEL messages from a channel since oldest_ts."""
    messages: list[dict] = []
    cursor = None
    while len(messages) < _MAX_MSG_PER_CHANNEL:
        kwargs: dict = {
            "channel": channel_id,
            "oldest": oldest_ts,
            "limit": 200,
            "inclusive": False,
        }
        if cursor:
            kwargs["cursor"] = cursor
        try:
            resp = client.conversations_history(**kwargs)
        except Exception as exc:
            log.warning("channel_sweep: history failed for %s: %s", channel_id, exc)
            break
        for msg in resp.get("messages", []):
            # Skip bot messages, join/leave system events
            if msg.get("bot_id") or msg.get("subtype") in ("channel_join", "channel_leave", "channel_topic"):
                continue
            if not msg.get("user") or not msg.get("text"):
                continue
            messages.append(msg)
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not resp.get("has_more") or not cursor:
            break
        time.sleep(0.2)
    return messages


# ── Per-user synthesis ──────────────────────────────────────────────────────────

_SYNTHESIS_SYSTEM = """\
You are an organizational intelligence assistant. You receive a batch of Slack messages
from one person across multiple channels, all from the past 24 hours.

Extract the following in JSON format:
{
  "commitments": ["..."],       // Things they committed or said they will do (max 5)
  "decisions": ["..."],         // Decisions they announced or confirmed (max 5)
  "open_questions": ["..."],    // Unresolved questions they asked (max 5)
  "cross_entity_mentions": ["..."]  // References to other business entities/teams (max 5)
}

Be specific and include context. If a category has nothing, return an empty list.
Return ONLY the JSON object with no markdown fences or commentary.
"""


def synthesize_user_activity(
    anthropic_client,
    user_id: str,
    display_name: str,
    messages: list[dict],
) -> dict:
    """Call Haiku to extract commitments/decisions/questions from a user's messages."""
    if not anthropic_client or not messages:
        return {"commitments": [], "decisions": [], "open_questions": [], "cross_entity_mentions": []}

    # Build a compact transcript
    lines: list[str] = []
    for m in messages[:60]:  # cap at 60 messages per user
        ch = m.get("channel_name", "?")
        text = (m.get("text") or "").replace("\n", " ")[:300]
        lines.append(f"[#{ch}] {text}")
    transcript = "\n".join(lines)

    user_msg = f"User: {display_name} ({user_id})\n\nMessages:\n{transcript}"

    try:
        resp = anthropic_client.messages.create(
            model=_SYNTHESIS_MODEL,
            max_tokens=600,
            system=_SYNTHESIS_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = (resp.content[0].text or "").strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1]
            raw = raw.rsplit("```", 1)[0].strip()
        return json.loads(raw)
    except Exception as exc:
        log.warning("channel_sweep: synthesis failed for %s: %s", user_id, exc)
        return {"commitments": [], "decisions": [], "open_questions": [], "cross_entity_mentions": []}


# ── User display name resolution ───────────────────────────────────────────────

def _resolve_display_names(client, user_ids: list[str]) -> dict[str, str]:
    """Batch-resolve Slack user IDs to display names."""
    names: dict[str, str] = {}
    for uid in user_ids:
        try:
            resp = client.users_info(user=uid)
            profile = (resp.get("user") or {}).get("profile", {})
            names[uid] = profile.get("display_name") or profile.get("real_name") or uid
        except Exception:
            names[uid] = uid
        time.sleep(0.15)
    return names


# ── Main sweep entry point ──────────────────────────────────────────────────────

def run_sweep(
    client,
    anthropic_client=None,
    lookback_hours: float = 24,
    dry_run: bool = False,
) -> ChannelSweepResult:
    """Sweep all joined public channels and synthesize per-user activity."""
    from datetime import datetime, timezone

    oldest_ts = str(time.time() - lookback_hours * 3600)
    swept_at = datetime.now(timezone.utc).isoformat()
    errors: list[str] = []

    # 1. List all joined channels
    channels = list_joined_channels(client)
    active_channels = [
        ch for ch in channels
        if ch["name"] not in _EXCLUDED_CHANNEL_NAMES
    ]
    log.info("channel_sweep: %d joined channels (%d after exclusions)", len(channels), len(active_channels))

    # 2. Collect messages per user across all channels
    user_messages: dict[str, list[dict]] = {}  # user_id → messages with channel_name attached

    for ch in active_channels:
        log.info("channel_sweep: reading #%s", ch["name"])
        msgs = fetch_channel_messages(client, ch["id"], oldest_ts)
        log.info("  %d messages", len(msgs))
        for msg in msgs:
            uid = msg.get("user", "")
            if not uid:
                continue
            msg_with_channel = dict(msg, channel_name=ch["name"])
            user_messages.setdefault(uid, []).append(msg_with_channel)
        time.sleep(_CHANNEL_FETCH_SLEEP)

    # 3. Resolve display names for active users
    active_user_ids = list(user_messages.keys())
    log.info("channel_sweep: %d users active across channels", len(active_user_ids))
    display_names = _resolve_display_names(client, active_user_ids)

    # 4. Synthesize per-user activity
    result_map: dict[str, UserActivity] = {}
    for uid, msgs in user_messages.items():
        name = display_names.get(uid, uid)
        log.info("channel_sweep: synthesizing %s (%d messages)", name, len(msgs))

        if dry_run:
            activity = UserActivity(slack_user_id=uid, display_name=name, messages=msgs)
        else:
            synthesis = synthesize_user_activity(anthropic_client, uid, name, msgs)
            activity = UserActivity(
                slack_user_id=uid,
                display_name=name,
                messages=msgs,
                commitments=synthesis.get("commitments") or [],
                decisions=synthesis.get("decisions") or [],
                open_questions=synthesis.get("open_questions") or [],
                cross_entity_mentions=synthesis.get("cross_entity_mentions") or [],
            )
        result_map[uid] = activity

    return ChannelSweepResult(
        swept_at=swept_at,
        channels_swept=len(active_channels),
        users_active=len(result_map),
        user_activity=result_map,
        errors=errors,
    )
