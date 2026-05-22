"""Bolt app and event handlers."""

import logging
import re
import time

from slack_bolt import App

from .claude_client import (
    ClaudeClientError,
    generate_response,
    generate_response_streaming,
    user_facing_message,
)
from . import channel_classifier
from .config import config
from .context_loader import load_context
from .entity_router import route
from . import feedback_log
from . import help_responder
from . import knowledge_gaps
from . import model_router
from .prompt_loader import load_prompt
from . import rate_limiter
from . import slack_update_throttle

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

    # Phase 3: pass user_message as query so context_loader can augment with
    # KB retrieval (top-K semantically-relevant chunks from cora_kb.db).
    # If KB isn't initialized or retrieval fails, falls back to static context.
    context = load_context(entity, query=user_message)
    prompt = load_prompt(entity)

    # Phase 6: route simple lookups to Haiku for ~3-5x speed-up; keep Sonnet for
    # reasoning-heavy / analytical / drafting requests. Heuristic-only — see
    # model_router.choose_model() for the rules.
    chosen_model = model_router.choose_model(user_message)
    log.info(
        "model_routing channel=#%s user=%s model=%s msg_chars=%d",
        channel_name, user_id, model_router.short_label(chosen_model), len(user_message),
    )

    # Phase 5: streaming. Post a placeholder so the user sees instant activity,
    # then progressively edit it as text streams from Claude. If the placeholder
    # post itself fails, fall back to the original non-streaming path so the user
    # still gets *some* reply.
    placeholder_ts: str | None = None
    placeholder_channel: str = channel_id
    try:
        placeholder_resp = say(
            text=":thought_balloon: thinking…",
            thread_ts=thread_ts,
            unfurl_links=False,
            unfurl_media=False,
        )
        placeholder_ts = placeholder_resp.get("ts")
        # Bolt's say() returns the chat.postMessage response shape; channel may
        # be echoed back or absent (Slack omits it if same as request). Default
        # to channel_id we already have.
        placeholder_channel = placeholder_resp.get("channel") or channel_id
    except Exception as exc:  # noqa: BLE001 — Slack errors are diverse; we just want to fall back
        log.warning(
            "Placeholder post failed for channel=%s user=%s: %s — falling back to non-streaming",
            channel_id, user_id, exc,
        )

    if placeholder_ts is None:
        # ── Fallback: non-streaming path (placeholder unavailable) ──
        try:
            response_text = generate_response(
                prompt,
                runtime_context + context,
                user_message,
                slack_user_id=user_id or "",
                entity=entity,
                model=chosen_model,
            )
        except ClaudeClientError as exc:
            log.error("ClaudeClientError for entity=%s user=%s: %s", entity, user_id, exc)
            say(text=user_facing_message(exc), thread_ts=thread_ts)
            return

        latency_ms = int((time.monotonic() - t0) * 1000)
        response_text = _extract_and_log_gap(
            response_text, entity, channel_name, user_id, user_message, latency_ms,
        )
        log.info(
            "responded (fallback non-streaming) entity=%s channel=#%s user=%s latency_ms=%d response_chars=%d",
            entity, channel_name, user_id, latency_ms, len(response_text),
        )
        say(
            text=response_text,
            thread_ts=thread_ts,
            unfurl_links=False,
            unfurl_media=False,
        )
        return

    # ── Streaming path ──
    stream_id = placeholder_ts  # ts is unique per stream
    throttle = slack_update_throttle.default_throttle

    def update_callback(cumulative_text: str) -> None:
        """Push the partial response to Slack via chat_update, gated by throttle.

        Called by generate_response_streaming on every text delta. Cheap when the
        throttle blocks (just a clock check + lock). Failure to update is logged
        but does NOT raise — the next batch will carry the cumulative text.
        """
        if not cumulative_text:
            return
        if not throttle.acquire(stream_id):
            return
        try:
            client.chat_update(
                channel=placeholder_channel,
                ts=placeholder_ts,
                text=cumulative_text,
            )
        except Exception as upd_exc:  # noqa: BLE001 — Slack errors are diverse
            log.warning(
                "chat_update mid-stream failed for ts=%s: %s — stream continues",
                placeholder_ts, upd_exc,
            )

    try:
        response_text = generate_response_streaming(
            prompt,
            runtime_context + context,
            user_message,
            update_callback=update_callback,
            slack_user_id=user_id or "",
            entity=entity,
            model=chosen_model,
        )
    except ClaudeClientError as exc:
        log.error("ClaudeClientError (streaming) for entity=%s user=%s: %s", entity, user_id, exc)
        # Replace placeholder with the classified error message
        error_msg = user_facing_message(exc)
        try:
            client.chat_update(
                channel=placeholder_channel,
                ts=placeholder_ts,
                text=error_msg,
            )
        except Exception as upd_exc:  # noqa: BLE001
            log.error(
                "Final error chat_update failed for ts=%s: %s — sending fresh reply",
                placeholder_ts, upd_exc,
            )
            say(text=error_msg, thread_ts=thread_ts)
        throttle.release_stream(stream_id)
        return

    latency_ms = int((time.monotonic() - t0) * 1000)
    response_text = _extract_and_log_gap(
        response_text, entity, channel_name, user_id, user_message, latency_ms,
    )

    skipped = throttle.release_stream(stream_id).get("skipped_count", 0)
    log.info(
        "responded (streaming) entity=%s channel=#%s user=%s latency_ms=%d response_chars=%d updates_skipped=%d",
        entity, channel_name, user_id, latency_ms, len(response_text), skipped,
    )

    # Final chat_update with the definitive text. force_acquire bypasses the
    # per-stream interval gate (the previous update was probably < 0.8s ago).
    # If the workspace budget is exhausted we still try — Slack will tell us if
    # it actually rejects the call.
    throttle.force_acquire(stream_id + "-final")
    try:
        client.chat_update(
            channel=placeholder_channel,
            ts=placeholder_ts,
            text=response_text,
        )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "Final chat_update failed for ts=%s: %s — sending fresh reply as fallback",
            placeholder_ts, exc,
        )
        say(
            text=response_text,
            thread_ts=thread_ts,
            unfurl_links=False,
            unfurl_media=False,
        )


def _extract_and_log_gap(
    response_text: str,
    entity: str,
    channel_name: str,
    user_id: str | None,
    user_message: str,
    latency_ms: int,
) -> str:
    """Pull the [CORA_KNOWLEDGE_GAP: ...] sentinel out of the response (if present),
    log the gap, and return the cleaned response text."""
    match = _GAP_RE.search(response_text)
    if not match:
        return response_text
    gap_desc = match.group(1).strip()
    cleaned = _GAP_RE.sub("", response_text).rstrip()
    knowledge_gaps.log_gap(
        entity=entity,
        channel=channel_name,
        user=user_id,
        question=user_message,
        response_chars=len(cleaned),
        gap=gap_desc,
        latency_ms=latency_ms,
    )
    return cleaned


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
