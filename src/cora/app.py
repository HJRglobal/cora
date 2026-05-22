"""Bolt app and event handlers."""

import logging
import re
import time

from slack_bolt import App

from .claude_client import ClaudeClientError, generate_response, user_facing_message
from . import channel_classifier
from .config import config
from .context_loader import load_context
from .entity_router import route
from . import feedback_log
from . import help_responder
from . import knowledge_gaps
from .prompt_loader import load_prompt
from . import rate_limiter

log = logging.getLogger(__name__)

app = App(token=config.slack_bot_token, signing_secret=config.slack_signing_secret)

_MENTION_RE = re.compile(r"^<@[A-Z0-9]+>\s*")
_GAP_RE = re.compile(r"\n*\s*\[CORA_KNOWLEDGE_GAP:\s*(.+?)\]\s*$", re.DOTALL | re.IGNORECASE)

# Resolved at first event via auth.test() - the bot's own user ID. Used to
# filter reaction_added events down to "user reacted to a Cora message" only.
_CORA_BOT_USER_ID: str | None = None


def _resolve_bot_user_id(client) -> str | None:
    """Lazy-resolve Cora's bot user ID via auth.test(). Cached after first call."""
    global _CORA_BOT_USER_ID
    if _CORA_BOT_USER_ID is not None:
        return _CORA_BOT_USER_ID
    try:
        resp = client.auth_test()
        _CORA_BOT_USER_ID = resp.get("user_id")
        log.info("Resolved Cora bot user_id=%s", _CORA_BOT_USER_ID)
    except Exception as exc:
        log.warning("Could not resolve bot user_id via auth.test(): %s", exc)
        _CORA_BOT_USER_ID = None
    return _CORA_BOT_USER_ID


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
    function = channel_classifier.classify_function(channel_name)
    tier = channel_classifier.tier_label(entity, function)

    log.info(
        "app_mention routed channel=#%s user=%s → entity=%s function=%s tier=%s",
        channel_name, user_id, entity, function, tier,
    )

    # Help-intent interception: if the user asked "what can you do" or similar,
    # short-circuit before the Claude call with a deterministic capability blurb.
    # Saves tokens, ensures consistent onboarding messaging across channels.
    if help_responder.is_help_intent(user_message):
        log.info("help-intent detected channel=#%s user=%s", channel_name, user_id)
        help_text = help_responder.build_message(entity, function, tier)
        say(text=help_text, thread_ts=thread_ts, unfurl_links=False, unfurl_media=False)
        return

    runtime_context = (
        f"## Runtime channel context\n\n"
        f"This channel (#{channel_name}) has these properties:\n"
        f"- Entity: {entity}\n"
        f"- Function: {function}\n"
        f"- Financial-access tier: {tier}\n\n"
        f"Apply the cross-entity and financial guardrails accordingly.\n\n"
        f"---\n\n"
    )

    t0 = time.monotonic()
    try:
        # Phase 3: pass user_message as query so context_loader can augment with
        # KB retrieval (top-K semantically-relevant chunks from cora_kb.db).
        # If KB isn't initialized or retrieval fails, falls back to static context.
        context = load_context(entity, query=user_message)
        prompt = load_prompt(entity)
        response_text = generate_response(
            prompt,
            runtime_context + context,
            user_message,
            slack_user_id=user_id or "",
            entity=entity,
        )
    except ClaudeClientError as exc:
        log.error("ClaudeClientError for entity=%s user=%s: %s", entity, user_id, exc)
        say(
            text=user_facing_message(exc),
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

    # unfurl_links/media False: suppresses Slack's auto-preview cards for every URL
    # in the response (was polluting calendar/asana/hubspot replies with "Google
    # Calendar — Easier Time Management..." stubs for every event link).
    say(
        text=response_text,
        thread_ts=thread_ts,
        unfurl_links=False,
        unfurl_media=False,
    )


# ────────────────────────────────────────────────────────────────────────────
# Reaction-based feedback capture
#
# When a user reacts to one of Cora's own messages, log the signal to
# logs/feedback.jsonl for downstream digesting. Only reactions on messages
# whose author is Cora (item_user == _CORA_BOT_USER_ID) get logged — other
# reactions in channels Cora is in are ignored.
#
# Requires Slack scope: reactions:read
# ────────────────────────────────────────────────────────────────────────────


def _handle_reaction(event: dict, client, event_type: str) -> None:
    """Shared logic for reaction_added and reaction_removed events."""
    item = event.get("item") or {}
    if item.get("type") != "message":
        return  # ignore reactions on files, channel boundaries, etc.

    item_user = event.get("item_user", "")
    bot_user_id = _resolve_bot_user_id(client)
    if not bot_user_id or item_user != bot_user_id:
        # Reaction on a non-Cora message - not our signal to capture
        return

    channel_id = item.get("channel", "")
    channel_name = _resolve_channel_name(client, channel_id) if channel_id else ""
    reactor = event.get("user", "")
    reaction = event.get("reaction", "")
    message_ts = item.get("ts", "")

    feedback_log.log_reaction(
        channel=channel_id,
        channel_name=channel_name,
        reactor=reactor,
        reaction=reaction,
        message_ts=message_ts,
        event_type=event_type,
    )


@app.event("reaction_added")
def handle_reaction_added(event: dict, client) -> None:
    _handle_reaction(event, client, "reaction_added")


@app.event("reaction_removed")
def handle_reaction_removed(event: dict, client) -> None:
    _handle_reaction(event, client, "reaction_removed")
