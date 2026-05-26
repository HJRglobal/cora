"""User feedback / sentiment tracker — per-user signal attribution.

Writes a single unified log at logs/cora-user-feedback.jsonl.
Each entry captures one negative or corrective signal from a Slack user,
enriched with their display name via user_identity so downstream analysis
can group by person, channel, and entity without chasing Slack IDs.

Signal types:
  correction       — user replied to a Cora thread with correction language
                     (detected by team_learning.is_correction)
  knowledge_gap    — Cora flagged a gap in her own response
  thumbsdown       — user reacted with a negative emoji to a Cora message

Schema (one JSON line per event):
    {
        "ts":              "2026-05-24T07:42:11+00:00",  (ISO 8601 UTC)
        "slack_user_id":   "U0B2RM2JYJ1",
        "display_name":    "Harrison Rogers",
        "channel":         "C0B4B0URRQS",
        "channel_name":    "f3e-leadership",
        "entity":          "F3E",
        "signal_type":     "correction",   (see types above)
        "message_excerpt": "That's not right — the quantity was 75K cans not..."
    }

Callers:
  - app.py handle_message_event: log_signal("correction", ...)
  - app.py _strip_gap_marker / knowledge-gap path: log_signal("knowledge_gap", ...)
  - app.py handle_reaction_event (negative sentiment): log_signal("thumbsdown", ...)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

log = logging.getLogger(__name__)

_LOG_PATH = Path(__file__).parent.parent.parent / "logs" / "cora-user-feedback.jsonl"
_LOCK = Lock()

# Truncate message_excerpt at this length to keep log size manageable.
_EXCERPT_MAX = 300


def log_signal(
    signal_type: str,
    slack_user_id: str,
    channel: str,
    channel_name: str,
    entity: str,
    message_excerpt: str = "",
) -> None:
    """Append one user-signal event to logs/cora-user-feedback.jsonl.

    Enriches the Slack user ID with a human display name via user_identity.
    If the identity cache isn't loaded or the user isn't mapped, falls back
    to the raw Slack user ID as the display name (never crashes).

    Args:
        signal_type:     One of "correction", "knowledge_gap", "thumbsdown".
        slack_user_id:   Slack user ID of the person who triggered the signal.
        channel:         Slack channel ID (e.g. "C0B4B0URRQS").
        channel_name:    Human-readable channel name (e.g. "f3e-leadership").
        entity:          Cora entity code for this channel ("F3E", "OSN", etc.)
        message_excerpt: The text excerpt that triggered the signal (first 300 chars).
    """
    # Resolve display name without crashing if identity layer isn't warmed up.
    try:
        from cora.tools import user_identity as _uid
        display = _uid.display_name(slack_user_id)  # returns slack_user_id as fallback
    except Exception:
        display = slack_user_id

    excerpt = (message_excerpt or "").strip()[:_EXCERPT_MAX]

    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "slack_user_id": slack_user_id,
        "display_name": display,
        "channel": channel,
        "channel_name": channel_name,
        "entity": entity,
        "signal_type": signal_type,
        "message_excerpt": excerpt,
    }

    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        with _LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    log.info(
        "user_feedback: %s user=%s (%s) channel=#%s entity=%s",
        signal_type, slack_user_id, display, channel_name, entity,
    )


def log_correction(
    slack_user_id: str,
    channel: str,
    channel_name: str,
    entity: str,
    correction_text: str,
) -> None:
    """Convenience wrapper for correction-capture events."""
    log_signal(
        signal_type="correction",
        slack_user_id=slack_user_id,
        channel=channel,
        channel_name=channel_name,
        entity=entity,
        message_excerpt=correction_text,
    )


def log_knowledge_gap(
    slack_user_id: str,
    channel: str,
    channel_name: str,
    entity: str,
    question: str,
    gap_description: str,
) -> None:
    """Convenience wrapper for knowledge-gap events.

    Joins the original question + gap description into the excerpt so
    downstream analysis has full context on what was asked and what was missing.
    """
    excerpt = f"Q: {question[:150]} | GAP: {gap_description[:150]}"
    log_signal(
        signal_type="knowledge_gap",
        slack_user_id=slack_user_id,
        channel=channel,
        channel_name=channel_name,
        entity=entity,
        message_excerpt=excerpt,
    )


def log_thumbsdown(
    slack_user_id: str,
    channel: str,
    channel_name: str,
    entity: str,
    message_ts: str = "",
) -> None:
    """Convenience wrapper for thumbs-down reaction events."""
    log_signal(
        signal_type="thumbsdown",
        slack_user_id=slack_user_id,
        channel=channel,
        channel_name=channel_name,
        entity=entity,
        message_excerpt=f"reaction on message_ts={message_ts}" if message_ts else "",
    )
