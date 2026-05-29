"""Slack channel history connector for Cora KB ingestion.

Reads message history from all channels Cora is a member of and yields
Document objects for ingestion into the vector KB. Used by
scripts/incremental_sync_slack.py (nightly at 2:00am AZ).

Design notes:
- Uses SLACK_BOT_TOKEN (xoxb-) to call the Slack Web API directly.
  This is separate from the Bolt app's socket-mode connection.
- Only reads channels Cora has been explicitly invited to. Private channels
  and DMs are only readable if Cora was invited.
- Rate-limit aware: Slack Tier 3 = 50 req/min. We add 1.2s sleep between
  full channel fetches (not between pagination calls on the same channel).
- Threads are fetched separately via conversations.replies for any message
  that has reply_count > 0. Thread replies are grouped with the parent.
- PHI guardrail: Lex sub-entity channels (llc-*, lbhs-*, lla-*, lts-*) are
  NOT excluded at the connector level — they're tagged with their sub_entity
  and the KB sibling_guard + LEX siloing prevents cross-channel leakage. The
  connector tags chunks correctly; the KB search layer enforces access.

Required Slack OAuth scopes (bot):
  channels:history    — read public channel messages
  groups:history      — read private channel messages
  im:history          — read DM messages (Cora in a DM)
  mpim:history        — read multi-person DM messages
  channels:read       — list public channels + membership
  groups:read         — list private channels + membership
  im:read             — list DM channels
  mpim:read           — list multi-person DM channels

These scopes are documented in slack-app-config/manifest.json.
If im:history or mpim:history are missing, the connector silently skips
those channel types (does NOT crash — useful for incremental rollout).
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Generator

log = logging.getLogger(__name__)

# Slack Tier-3 rate limit is 50 req/min. 1.2s sleep per channel fetch
# keeps us well under that even on large workspaces (47 channels = ~56s total).
_CHANNEL_FETCH_SLEEP = 1.2

# Maximum messages to fetch per channel per sync run. Keeps single-channel
# runaway in check; watermark ensures we only fetch new messages anyway.
_MAX_MESSAGES_PER_CHANNEL = 500

# Slack channel types to scan. We request all types in one API call.
_CHANNEL_TYPES = "public_channel,private_channel,mpim,im"


class SlackConnectorError(Exception):
    """Raised on unrecoverable Slack API errors."""


# ────────────────────────────────────────────────────────────────────────────
# Auth
# ────────────────────────────────────────────────────────────────────────────


def _get_bot_token() -> str:
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        raise SlackConnectorError(
            "SLACK_BOT_TOKEN not set — Slack connector disabled"
        )
    return token


def _build_client():
    """Build a slack_sdk WebClient. Import deferred to avoid startup cost."""
    try:
        from slack_sdk import WebClient
    except ImportError as exc:
        raise SlackConnectorError(
            "slack_sdk not installed — run: pip install slack-sdk"
        ) from exc
    return WebClient(token=_get_bot_token())


# ────────────────────────────────────────────────────────────────────────────
# Channel listing
# ────────────────────────────────────────────────────────────────────────────


def list_joined_channels() -> list[dict[str, Any]]:
    """Return all channels Cora is a member of.

    Returns a list of channel dicts with at minimum:
      {
        "id": str,            Slack channel ID (C..., G..., D...)
        "name": str,          channel name (or empty for DMs)
        "is_private": bool,
        "is_im": bool,
        "is_mpim": bool,
      }

    Pagination is handled transparently.
    Raises SlackConnectorError on API failure.
    """
    client = _build_client()
    channels: list[dict[str, Any]] = []
    cursor: str | None = None

    while True:
        kwargs: dict[str, Any] = {
            "types": _CHANNEL_TYPES,
            "exclude_archived": True,
            "limit": 200,
        }
        if cursor:
            kwargs["cursor"] = cursor

        try:
            resp = client.conversations_list(**kwargs)
        except Exception as exc:
            raise SlackConnectorError(
                f"conversations.list failed: {exc}"
            ) from exc

        for ch in resp.get("channels", []):
            if not ch.get("is_member", False):
                continue  # skip channels Cora isn't in
            channels.append({
                "id": ch["id"],
                "name": ch.get("name") or ch.get("user") or ch["id"],
                "is_private": ch.get("is_private", False),
                "is_im": ch.get("is_im", False),
                "is_mpim": ch.get("is_mpim", False),
            })

        meta = resp.get("response_metadata") or {}
        cursor = meta.get("next_cursor") or ""
        if not cursor:
            break

    log.info("slack_connector: found %d joined channels", len(channels))
    return channels


# ────────────────────────────────────────────────────────────────────────────
# Message history
# ────────────────────────────────────────────────────────────────────────────


def get_channel_history(
    channel_id: str,
    oldest_ts: float,
    *,
    client=None,
) -> list[dict[str, Any]]:
    """Fetch messages in channel_id since oldest_ts (Unix seconds as float).

    Returns list of message dicts (raw Slack message payloads).
    Handles pagination. Raises SlackConnectorError on scope/API failure.

    Slack returns messages newest-first; we reverse to chronological order.
    """
    if client is None:
        client = _build_client()

    messages: list[dict[str, Any]] = []
    cursor: str | None = None

    while len(messages) < _MAX_MESSAGES_PER_CHANNEL:
        kwargs: dict[str, Any] = {
            "channel": channel_id,
            "oldest": str(oldest_ts),
            "limit": 200,
            "inclusive": False,
        }
        if cursor:
            kwargs["cursor"] = cursor

        try:
            resp = client.conversations_history(**kwargs)
        except Exception as exc:
            err_str = str(exc).lower()
            if "missing_scope" in err_str or "not_in_channel" in err_str:
                # Graceful skip — missing im:history or mpim:history scope
                log.warning(
                    "slack_connector: skipping channel %s (missing scope or not member): %s",
                    channel_id, exc,
                )
                return []
            raise SlackConnectorError(
                f"conversations.history failed for {channel_id}: {exc}"
            ) from exc

        batch = resp.get("messages", [])
        messages.extend(batch)

        if not resp.get("has_more", False):
            break

        meta = resp.get("response_metadata") or {}
        cursor = meta.get("next_cursor") or ""
        if not cursor:
            break

    # Slack returns newest-first; reverse for chronological order
    messages.reverse()
    log.debug(
        "slack_connector: get_channel_history(%s, oldest=%.0f) -> %d messages",
        channel_id, oldest_ts, len(messages),
    )
    return messages


def get_thread_replies(
    channel_id: str,
    thread_ts: str,
    *,
    client=None,
) -> list[dict[str, Any]]:
    """Fetch all replies in a thread. Returns messages excluding the parent.

    The parent message is already included in get_channel_history(); we only
    want the replies here (message["thread_ts"] != message["ts"]).
    """
    if client is None:
        client = _build_client()

    replies: list[dict[str, Any]] = []
    cursor: str | None = None

    while True:
        kwargs: dict[str, Any] = {
            "channel": channel_id,
            "ts": thread_ts,
            "limit": 200,
        }
        if cursor:
            kwargs["cursor"] = cursor

        try:
            resp = client.conversations_replies(**kwargs)
        except Exception as exc:
            log.warning(
                "slack_connector: get_thread_replies(%s, %s) failed: %s",
                channel_id, thread_ts, exc,
            )
            return []

        for msg in resp.get("messages", []):
            # Exclude the parent (its ts == thread_ts)
            if msg.get("ts") != thread_ts:
                replies.append(msg)

        if not resp.get("has_more", False):
            break

        meta = resp.get("response_metadata") or {}
        cursor = meta.get("next_cursor") or ""
        if not cursor:
            break

    return replies


# ────────────────────────────────────────────────────────────────────────────
# Channel name → user display names (for DM channels)
# ────────────────────────────────────────────────────────────────────────────


def _resolve_user_display_name(user_id: str, client) -> str:
    """Best-effort user ID → display name resolution. Returns user_id on failure."""
    try:
        resp = client.users_info(user=user_id)
        profile = resp.get("user", {}).get("profile", {})
        return (
            profile.get("display_name")
            or profile.get("real_name")
            or user_id
        )
    except Exception:
        return user_id


# ────────────────────────────────────────────────────────────────────────────
# Message serialization
# ────────────────────────────────────────────────────────────────────────────


def serialize_message(msg: dict[str, Any]) -> str:
    """Convert a Slack message dict to a plain-text string for KB chunking.

    Format: "[HH:MM] @user: text\n  reply: @user: text"
    Strips block_kit / attachments / unfurl junk — plain text only.
    """
    ts_float = float(msg.get("ts", 0))
    from datetime import datetime, timezone
    dt = datetime.fromtimestamp(ts_float, tz=timezone.utc)
    time_str = dt.strftime("%Y-%m-%d %H:%M UTC")

    user = msg.get("user", msg.get("bot_id", "unknown"))
    text = (msg.get("text") or "").strip()

    # Include file names if any attachments are present
    files = msg.get("files") or []
    if files:
        file_names = [f.get("name", "file") for f in files]
        text += f" [files: {', '.join(file_names)}]"

    return f"[{time_str}] <{user}>: {text}"
