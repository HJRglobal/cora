"""Reaction-based feedback capture.

When a user reacts to a Cora message in Slack with a thumbs-up, thumbs-down,
or similar evaluative emoji, this module logs the signal to
`logs/feedback.jsonl` for downstream analysis.

Sentiment classification is conservative — we capture every reaction on a
Cora message but tag it positive / negative / neutral so the daily digest
can prioritize the negative signal (where Cora's answer was off).

Schema (one JSON line per reaction):
    {
        "ts":          "2026-05-21T13:42:11+00:00",
        "channel":     "C0B4B0URRQS",
        "channel_name": "cora-build",       (best-effort, may be channel_id if lookup fails)
        "reactor":     "U01234567",
        "reaction":    "thumbsdown",
        "sentiment":   "negative",
        "message_ts":  "1747832123.123456",
        "event_type":  "reaction_added"     (or "reaction_removed" — see below)
    }

We log BOTH reaction_added AND reaction_removed events to surface "the user
took back their thumbs-down" patterns. The aggregator can fold these later.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

_LOG_PATH = Path(__file__).parent.parent.parent / "logs" / "feedback.jsonl"
_LOCK = Lock()

log = logging.getLogger(__name__)


# Sentiment classification - conservative buckets.
_POSITIVE_REACTIONS = {
    "+1",
    "thumbsup",
    "heavy_check_mark",
    "white_check_mark",
    "100",
    "fire",
    "heart",
    "raised_hands",
    "muscle",
    "ok_hand",
    "tada",
    "clap",
    "rocket",
    "star",
    "star2",
}

_NEGATIVE_REACTIONS = {
    "-1",
    "thumbsdown",
    "x",
    "no_entry",
    "no_entry_sign",
    "warning",
    "confused",
    "frowning",
    "white_frowning_face",
    "rage",
    "weary",
    "skull",
}


def classify_sentiment(reaction: str) -> str:
    """Return 'positive', 'negative', or 'neutral' for a Slack reaction emoji name."""
    if not reaction:
        return "neutral"
    # Slack strips skin-tone modifiers like '::skin-tone-2'. Just normalize.
    base = reaction.split("::", 1)[0].lower()
    if base in _POSITIVE_REACTIONS:
        return "positive"
    if base in _NEGATIVE_REACTIONS:
        return "negative"
    return "neutral"


def log_reaction(
    channel: str,
    channel_name: str,
    reactor: str,
    reaction: str,
    message_ts: str,
    event_type: str = "reaction_added",
) -> None:
    """Append one reaction event to logs/feedback.jsonl. Thread-safe."""
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "channel": channel,
        "channel_name": channel_name,
        "reactor": reactor,
        "reaction": reaction,
        "sentiment": classify_sentiment(reaction),
        "message_ts": message_ts,
        "event_type": event_type,
    }
    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        with _LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    log.info(
        "feedback logged channel=#%s reactor=%s reaction=%s sentiment=%s",
        channel_name, reactor, reaction, record["sentiment"],
    )
