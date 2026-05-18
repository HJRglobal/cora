"""Bolt app and event handlers."""

import logging
import re
import time

from slack_bolt import App

from .claude_client import ClaudeClientError, generate_response
from .config import config
from .context_loader import load_context
from .entity_router import route
from . import knowledge_gaps
from .prompt_loader import load_prompt
from . import rate_limiter

log = logging.getLogger(__name__)

app = App(token=config.slack_bot_token, signing_secret=config.slack_signing_secret)

_MENTION_RE = re.compile(r"^<@[A-Z0-9]+>\s*")
_GAP_RE = re.compile(r"\n*\s*\[CORA_KNOWLEDGE_GAP:\s*(.+?)\]\s*$", re.DOTALL | re.IGNORECASE)


def _resolve_channel_name(client, channel_id: str) -> str:
    try:
        info = client.conversations_info(channel=channel_id)
        return info["channel"]["name"]
    except Exception as exc:
        log.warning("Could not resolve channel name for %s: %s", channel_id, exc)
        return channel_id


@app.event("app_mention")
def handle_mention(event: dict, say: callable, client) -> None:
    channel_id = event.get("channel", "")
    user_id = event.get("user")
    thread_ts = event.get("ts")
    raw_text = event.get("text", "")

    allowed, cap_type = rate_limiter.check(user_id, channel_id)
    if not allowed:
        log.warning("rate_limited user=%s channel=%s cap=%s", user_id, channel_id, cap_type)
        if cap_type == "user":
            say(text="You've hit the per-user mention cap (10/hour). I'll be back shortly.", thread_ts=thread_ts)
        else:
            say(text="This channel has hit the mention cap (50/hour). Try again in a bit.", thread_ts=thread_ts)
        return

    channel_name = _resolve_channel_name(client, channel_id)
    entity = route(channel_name)
    user_message = _MENTION_RE.sub("", raw_text).strip()

    log.info("app_mention routed channel=#%s user=%s → entity=%s", channel_name, user_id, entity)

    t0 = time.monotonic()
    try:
        context = load_context(entity)
        prompt = load_prompt(entity)
        response_text = generate_response(prompt, context, user_message)
    except ClaudeClientError as exc:
        log.error("ClaudeClientError for entity=%s user=%s: %s", entity, user_id, exc)
        say(
            text="I'm having trouble reaching Claude right now — try again in a moment.",
            thread_ts=thread_ts,
        )
        return

    latency_ms = int((time.monotonic() - t0) * 1000)

    match = _GAP_RE.search(response_text)
    if match:
        gap_desc = match.group(1).strip()
        response_text = _GAP_RE.sub("", response_text).rstrip()
        knowledge_gaps.log_gap(
            entity=entity,
            channel=channel_name,
            user=user_id,
            question=user_message,
            response_chars=len(response_text),
            gap=gap_desc,
            latency_ms=latency_ms,
        )

    log.info(
        "responded entity=%s channel=#%s user=%s latency_ms=%d response_chars=%d",
        entity,
        channel_name,
        user_id,
        latency_ms,
        len(response_text),
    )

    say(text=response_text, thread_ts=thread_ts)
