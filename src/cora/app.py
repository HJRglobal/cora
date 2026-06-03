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
from . import active_thread_store
from . import channel_classifier
from . import user_access
from .config import config
from .context_loader import load_context
from .entity_router import route
from . import feedback_log
from . import help_responder
from . import knowledge_review
from . import intent_classifier as ic
from . import knowledge_gaps
from .knowledge_base import embeddings as kb_embeddings
from . import sibling_guard
from . import model_router
from .prompt_loader import load_prompt
from . import rate_limiter
from . import semantic_cache as sc
from . import slack_update_throttle
from . import team_learning
from . import user_feedback_tracker as uft
from .tools import user_identity
from .tools import osn_shift_handler

log = logging.getLogger(__name__)

app = App(token=config.slack_bot_token, signing_secret=config.slack_signing_secret)

_MENTION_RE = re.compile(r"^<@[A-Z0-9]+>\s*")
# ── Permanently blocked channel IDs ────────────────────────────────────────────
# Cora must NEVER post to these channels under any circumstance.
# C0B2NMLK7CK = #general-do-not-use (was #all-hjr-global) — workspace default
# general channel; Slack prevents archiving it, so we block it in code instead.
# Used by _is_blocked_channel() which gates every outbound chat_postMessage.
_BLOCKED_CHANNEL_IDS: frozenset[str] = frozenset({"C0B2NMLK7CK"})


def _is_blocked_channel(channel_id: str) -> bool:
    """Return True if this channel is permanently blocked from Cora posts."""
    return channel_id in _BLOCKED_CHANNEL_IDS


_FOUNDER_ID = "U0B2RM2JYJ1"  # Harrison — KB approvals and cross-entity access
_GAP_RE = re.compile(r"\n*\s*\[CORA_KNOWLEDGE_GAP:\s*(.+?)\]\s*$", re.DOTALL | re.IGNORECASE)

# Feature 3: Decision-language patterns for pin-on-approval logic
_DECISION_RE = re.compile(
    r"\b(decided|locked|going with|approved|we will|we won'?t|not going to|"
    r"confirmed|final decision|agreed to|moving forward with|shutting down|"
    r"launching|cancell?ing|pausing)\b",
    re.IGNORECASE,
)

# Feature 3 + 4: Entity → leadership channel ID, loaded lazily from entity-channels.yaml
_ENTITY_CHANNELS_CACHE: dict | None = None


def _is_decision_content(text: str) -> bool:
    """Return True if the text contains decision-language worthy of pinning."""
    return bool(_DECISION_RE.search(text))


def _load_entity_channels() -> dict:
    """Load entity-channels.yaml, cached in process memory."""
    global _ENTITY_CHANNELS_CACHE
    if _ENTITY_CHANNELS_CACHE is not None:
        return _ENTITY_CHANNELS_CACHE
    try:
        import yaml as _yaml
        path = Path(__file__).resolve().parents[2] / "data" / "maps" / "entity-channels.yaml"
        data = _yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        _ENTITY_CHANNELS_CACHE = data.get("entities", {})
    except Exception as exc:
        log.warning("_load_entity_channels: failed to load entity-channels.yaml: %s", exc)
        _ENTITY_CHANNELS_CACHE = {}
    return _ENTITY_CHANNELS_CACHE


def _entity_leadership_channel(entity: str) -> str | None:
    """Return the Slack channel ID for the entity's leadership channel, or None."""
    channels = _load_entity_channels()
    entry = channels.get(entity, {})
    return entry.get("leadership") or None


def _entity_finance_channel(entity: str) -> str | None:
    """Return the Slack channel ID for the entity's finance channel, or None."""
    channels = _load_entity_channels()
    entry = channels.get(entity, {})
    return entry.get("finance") or None

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


def _fetch_thread_history(
    client,
    channel_id: str,
    thread_root_ts: str,
    current_msg_ts: str,
    limit: int = 12,
) -> list[dict]:
    """Fetch prior messages in a Slack thread and convert to Claude message format.

    Returns a list of {"role": "user"|"assistant", "content": str} dicts suitable
    for prepending to the Claude messages array. The current message is excluded
    (it will be appended as the final user turn by generate_response).

    Errors are swallowed — thread context is best-effort; a cold-start response
    is always better than a crash.
    """
    try:
        resp = client.conversations_replies(
            channel=channel_id,
            ts=thread_root_ts,
            limit=limit,
        )
        raw_messages = resp.get("messages", [])
    except Exception as exc:
        log.warning(
            "thread_history: conversations_replies failed channel=%s thread_ts=%s: %s",
            channel_id, thread_root_ts, exc,
        )
        return []

    bot_id = _CORA_BOT_USER_ID  # may be None before first auth.test()
    history: list[dict] = []
    for msg in raw_messages:
        if msg.get("ts") == current_msg_ts:
            continue  # skip the current message — it's appended separately
        if msg.get("subtype"):
            continue  # skip channel joins, leaves, etc.
        text = msg.get("text", "").strip()
        if not text:
            continue
        # Strip @Cora mention prefix from user messages
        text = _MENTION_RE.sub("", text).strip()
        if not text:
            continue
        is_bot = bool(msg.get("bot_id")) or (bot_id and msg.get("user") == bot_id)
        role = "assistant" if is_bot else "user"
        history.append({"role": role, "content": text})

    # Anthropic requires alternating user/assistant turns. Merge consecutive
    # same-role messages (e.g. two user turns if Cora didn't respond to one).
    merged: list[dict] = []
    for turn in history:
        if merged and merged[-1]["role"] == turn["role"]:
            merged[-1]["content"] += "\n" + turn["content"]
        else:
            merged.append({"role": turn["role"], "content": turn["content"]})

    # Ensure history starts with a user turn (Claude API requirement)
    while merged and merged[0]["role"] == "assistant":
        merged.pop(0)

    log.info(
        "thread_history: fetched %d turns for channel=%s thread_ts=%s",
        len(merged), channel_id, thread_root_ts,
    )
    return merged


def _dispatch_qa(
    *,
    channel_id: str,
    channel_name: str,
    user_id: str | None,
    user_message: str,
    reply_thread_ts: str,
    entity: str,
    client,
    say,
    prior_messages: list[dict] | None = None,
    root_thread_ts: str | None = None,
) -> None:
    """Core Q&A pipeline — intent → cache → KB → Claude → post response.

    Shared between handle_mention (for @-mention triggers) and the thread
    follow-up path in handle_message_event (for replies in active threads
    without a fresh @-mention). After a successful response the thread is
    registered in active_thread_store so subsequent replies stay in context.

    Args:
        channel_id:       Slack channel ID.
        channel_name:     Resolved channel name (without #).
        user_id:          Slack user ID of the person who sent the message.
        user_message:     Cleaned message text (no @Cora prefix).
        reply_thread_ts:  The thread_ts to reply into.
        entity:           Resolved entity code (e.g. "F3E", "OSN").
        client:           Slack WebClient from Bolt.
        say:              Callable that posts to the current channel (Bolt's
                          say() or a lambda wrapping chat_postMessage).
        prior_messages:   List of prior {role, content} dicts for thread context.
        root_thread_ts:   Thread root to register in active_thread_store after
                          responding. Defaults to reply_thread_ts if None.
    """
    if prior_messages is None:
        prior_messages = []
    register_ts = root_thread_ts or reply_thread_ts

    function = channel_classifier.classify_function(channel_name)
    tier = channel_classifier.tier_label(entity, function)

    # Resolve who is asking — ALWAYS inject caller identity so Claude never
    # confuses one team member for another (e.g. Hannah for Harrison).
    caller_name = user_identity.display_name(user_id or "") if user_id else "Unknown"
    caller_record = user_identity.get_user(user_id or "") if user_id else None
    caller_role_hint = ""
    if caller_record and caller_record.asana_email:
        caller_role_hint = f" ({caller_record.asana_email})"

    # Founder (Harrison) gets cross-entity access from any channel. His questions
    # about UFL, LEX, OSN etc. from an F3E channel should not be blocked by entity scope.
    is_founder = (user_id == _FOUNDER_ID)
    founder_note = (
        "\n**Cross-entity access ENABLED:** This user is the portfolio founder. "
        "Answer questions about any HJR Global entity regardless of this channel's "
        "entity scope. Do not redirect to other channels based on entity scoping.\n"
    ) if is_founder else ""

    runtime_context = (
        f"## Runtime channel context\n\n"
        f"This channel (#{channel_name}) has these properties:\n"
        f"- Entity: {entity}\n"
        f"- Function: {function}\n"
        f"- Financial-access tier: {tier}\n\n"
        f"**The person asking this question is: {caller_name}{caller_role_hint}** "
        f"(Slack ID: {user_id or 'unknown'}).\n"
        f"Address them by their first name if relevant. Do NOT assume the asker is "
        f"Harrison Rogers unless their Slack ID is U0B2RM2JYJ1.\n"
        f"{founder_note}\n"
        f"Apply the cross-entity and financial guardrails accordingly.\n\n"
        f"---\n\n"
    )

    t0 = time.monotonic()

    # ── Intent classification + semantic cache ─────────────────────────────
    intent = ic.classify(user_message, entity)
    hints  = ic.routing_hints(intent)

    log.info(
        "intent_classify channel=#%s user=%s intent=%s skip_kb=%s bypass_cache=%s",
        channel_name, user_id, intent, hints.skip_kb, hints.bypass_cache,
    )

    question_embedding: list[float] | None = None
    if not hints.bypass_cache:
        try:
            question_embedding = kb_embeddings.embed_query(user_message)
            cached_response = sc.get_cache().lookup(entity, question_embedding)
            if cached_response:
                latency_ms = int((time.monotonic() - t0) * 1000)
                log.info(
                    "semantic_cache served channel=#%s user=%s entity=%s latency_ms=%d",
                    channel_name, user_id, entity, latency_ms,
                )
                say(
                    text=cached_response,
                    thread_ts=reply_thread_ts,
                    unfurl_links=False,
                    unfurl_media=False,
                )
                active_thread_store.register(channel_id, register_ts)
                return
        except Exception as exc:
            log.warning("semantic_cache lookup error for entity=%s: %s", entity, exc)

    # ── Context + prompt loading ───────────────────────────────────────────
    context = load_context(
        entity,
        query=user_message,
        skip_kb=hints.skip_kb,
        kb_k=hints.kb_k_override,
    )
    prompt = load_prompt(entity)
    chosen_model = model_router.choose_model(user_message)
    log.info(
        "model_routing channel=#%s user=%s model=%s msg_chars=%d",
        channel_name, user_id, model_router.short_label(chosen_model), len(user_message),
    )

    # ── Streaming: post placeholder, then update it as Claude streams ──────
    placeholder_ts: str | None = None
    placeholder_channel: str = channel_id
    try:
        placeholder_resp = say(
            text=":thought_balloon: thinking…",
            thread_ts=reply_thread_ts,
            unfurl_links=False,
            unfurl_media=False,
        )
        placeholder_ts = placeholder_resp.get("ts")
        placeholder_channel = placeholder_resp.get("channel") or channel_id
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "Placeholder post failed for channel=%s user=%s: %s — falling back to non-streaming",
            channel_id, user_id, exc,
        )

    if placeholder_ts is None:
        # ── Fallback: non-streaming path ──
        try:
            response_text = generate_response(
                prompt,
                runtime_context + context,
                user_message,
                slack_user_id=user_id or "",
                entity=entity,
                model=chosen_model,
                prior_messages=prior_messages,
                channel_name=channel_name,
            )
        except ClaudeClientError as exc:
            log.error("ClaudeClientError for entity=%s user=%s: %s", entity, user_id, exc)
            say(text=user_facing_message(exc), thread_ts=reply_thread_ts)
            return

        latency_ms = int((time.monotonic() - t0) * 1000)
        response_text = _extract_and_log_gap(
            response_text, entity, channel_name, user_id, user_message, latency_ms,
        )
        _try_cache_store(entity, user_message, question_embedding, response_text, hints)
        log.info(
            "responded (non-streaming) entity=%s channel=#%s user=%s latency_ms=%d response_chars=%d",
            entity, channel_name, user_id, latency_ms, len(response_text),
        )
        say(
            text=response_text,
            thread_ts=reply_thread_ts,
            unfurl_links=False,
            unfurl_media=False,
        )
        active_thread_store.register(channel_id, register_ts)
        return

    # ── Streaming path ──
    stream_id = placeholder_ts
    throttle = slack_update_throttle.default_throttle

    def update_callback(cumulative_text: str) -> None:
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
        except Exception as upd_exc:  # noqa: BLE001
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
            prior_messages=prior_messages,
            channel_name=channel_name,
        )
    except ClaudeClientError as exc:
        log.error("ClaudeClientError (streaming) for entity=%s user=%s: %s", entity, user_id, exc)
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
            say(text=error_msg, thread_ts=reply_thread_ts)
        throttle.release_stream(stream_id)
        return

    latency_ms = int((time.monotonic() - t0) * 1000)
    response_text = _extract_and_log_gap(
        response_text, entity, channel_name, user_id, user_message, latency_ms,
    )
    _try_cache_store(entity, user_message, question_embedding, response_text, hints)

    skipped = throttle.release_stream(stream_id).get("skipped_count", 0)
    log.info(
        "responded (streaming) entity=%s channel=#%s user=%s latency_ms=%d response_chars=%d updates_skipped=%d",
        entity, channel_name, user_id, latency_ms, len(response_text), skipped,
    )

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
            thread_ts=reply_thread_ts,
            unfurl_links=False,
            unfurl_media=False,
        )

    # Register AFTER the response is confirmed posted so only successful
    # interactions activate the thread follow-up window.
    active_thread_store.register(channel_id, register_ts)


@app.event("app_mention")
def handle_mention(event: dict, say: callable, client) -> None:
    if event.get("bot_id"):
        return

    channel_id = event.get("channel", "")

    # Hard block: never respond in permanently blocked channels
    if _is_blocked_channel(channel_id):
        log.warning("handle_mention: blocked channel %s — ignoring", channel_id)
        return

    user_id = event.get("user")
    thread_ts = event.get("ts")          # ts of THIS message (used for reply threading)
    event_thread_ts = event.get("thread_ts")  # root ts if this is inside a thread
    raw_text = event.get("text", "")

    # Lazy-resolve bot user ID (needed for thread history role assignment)
    _resolve_bot_user_id(client)

    # If this @mention is inside an existing thread, fetch prior messages so
    # Claude has conversation context (e.g. "go ahead" after a dry-run reply).
    prior_messages: list[dict] = []
    if event_thread_ts and event_thread_ts != thread_ts:
        prior_messages = _fetch_thread_history(
            client, channel_id, event_thread_ts, thread_ts
        )

    allowed, cap_type = rate_limiter.check(user_id, channel_id)
    if not allowed:
        log.warning("rate_limited user=%s channel=%s cap=%s", user_id, channel_id, cap_type)
        if cap_type == "user":
            say(text="You've hit the per-user mention cap (10/hour). I'll be back shortly.", thread_ts=thread_ts)
        else:
            say(text="This channel has hit the mention cap (50/hour). Try again in a bit.", thread_ts=thread_ts)
        return

    channel_name = _resolve_channel_name(client, channel_id)

    # ── Silent channel check — automated feed channels, Cora does not respond ─
    from .entity_router import is_silent_channel
    if is_silent_channel(channel_name):
        log.info("silent channel #%s — ignoring @mention from %s", channel_name, user_id)
        return

    entity = route(channel_name)
    user_message = _MENTION_RE.sub("", raw_text).strip()

    # ── Write-back interception: @Cora note: <content> ────────────────────────
    note_content = team_learning.parse_note(user_message)
    if note_content:
        _handle_note(
            client=client, say=say,
            entity=entity, channel_id=channel_id, channel_name=channel_name,
            user_id=user_id or "", content=note_content, original_ts=thread_ts or "",
        )
        return

    log.info(
        "app_mention routed channel=#%s user=%s → entity=%s",
        channel_name, user_id, entity,
    )

    # ── User access check — entity + sensitive topic authorization ────────────
    if user_id:
        access_block = user_access.check_access(user_id, entity, user_message)
        if access_block:
            log.info(
                "user_access: blocked user=%s entity=%s reason=%s",
                user_id, entity, access_block[:80],
            )
            say(text=access_block, thread_ts=thread_ts,
                unfurl_links=False, unfurl_media=False)
            return

    # Help-intent interception
    if help_responder.is_help_intent(user_message):
        log.info("help-intent detected channel=#%s user=%s", channel_name, user_id)
        function = channel_classifier.classify_function(channel_name)
        tier = channel_classifier.tier_label(entity, function)
        help_text = help_responder.build_message(entity, function, tier)
        say(text=help_text, thread_ts=thread_ts, unfurl_links=False, unfurl_media=False)
        return

    # Sibling-entity redirect interception (LEX sub-entity channels)
    sibling_redirect = sibling_guard.check_redirect(entity, user_message)
    if sibling_redirect:
        log.info("sibling-entity redirect fired channel=#%s entity=%s", channel_name, entity)
        say(text=sibling_redirect, thread_ts=thread_ts, unfurl_links=False, unfurl_media=False)
        return

    # Root thread ts: if @mention is inside an existing thread use that root,
    # otherwise this message IS the root.
    root_thread_ts = event_thread_ts or thread_ts

    _dispatch_qa(
        channel_id=channel_id,
        channel_name=channel_name,
        user_id=user_id,
        user_message=user_message,
        reply_thread_ts=thread_ts,
        entity=entity,
        client=client,
        say=say,
        prior_messages=prior_messages,
        root_thread_ts=root_thread_ts,
    )


def _try_cache_store(
    entity: str,
    question: str,
    question_embedding: "list[float] | None",
    response: str,
    hints: "ic.RoutingHints",
) -> None:
    """Store response in semantic cache if routing allows it. Never raises."""
    if hints.bypass_cache or question_embedding is None or hints.cache_ttl <= 0:
        return
    try:
        sc.get_cache().store(
            entity=entity,
            question=question,
            question_embedding=question_embedding,
            response=response,
            ttl_seconds=hints.cache_ttl,
        )
    except Exception as exc:
        log.warning("semantic_cache store failed for entity=%s: %s", entity, exc)


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
    # Per-user feedback attribution — enriches gap event with display name.
    # channel_id not available in this helper scope; best-effort with channel_name.
    uft.log_knowledge_gap(
        slack_user_id=user_id or "",
        channel=channel_name,   # may be name rather than ID here; tolerated
        channel_name=channel_name,
        entity=entity,
        question=user_message,
        gap_description=gap_desc,
    )
    return cleaned


# ────────────────────────────────────────────────────────────────────────────
# Team learning helpers — write-back, corrections, approval processing
# ────────────────────────────────────────────────────────────────────────────


def _resolve_queue_channel_id(client, entity: str, fallback_channel_id: str) -> str:
    """Find the Slack channel ID for the per-entity queue, falling back to #hjrg-leadership."""
    target_names = [
        team_learning.get_queue_channel(entity),  # e.g. cora-kq-osngm
        team_learning.APPROVAL_CHANNEL,           # hjrg-leadership
    ]
    try:
        resp = client.conversations_list(types="public_channel,private_channel", limit=200)
        channels = {ch["name"]: ch["id"] for ch in resp.get("channels", [])}
        for name in target_names:
            if name in channels:
                return channels[name]
    except Exception as exc:
        log.warning("_resolve_queue_channel_id: conversations_list failed: %s", exc)
    return fallback_channel_id


def _queue_contribution(
    *,
    client,
    entity: str,
    channel_id: str,
    channel_name: str,
    user_id: str,
    content: str,
    original_ts: str,
    kind: str = "note",
) -> None:
    """Store a confirmed contribution and post its approval card to the queue channel.

    Called by the Path-0 confirmation loop once the author has said 'yes'.
    Also used internally when bypassing the paraphrase step is appropriate.
    """
    cid = team_learning.store_contribution(
        kind=kind,
        entity=entity,
        channel_id=channel_id,
        channel_name=channel_name,
        author=user_id,
        content=content,
        original_ts=original_ts,
    )

    queue_name = team_learning.get_queue_channel(entity)
    card_text = team_learning.build_approval_card(
        kind=kind,
        entity=entity,
        channel_name=channel_name,
        author=user_id,
        content=content,
        contribution_id=cid,
    )
    try:
        approval_ch_id = _resolve_queue_channel_id(client, entity, channel_id)
        post_resp = client.chat_postMessage(
            channel=approval_ch_id,
            text=card_text,
            unfurl_links=False,
            unfurl_media=False,
        )
        approval_ts = post_resp.get("ts", "")
        team_learning.set_approval_msg(cid, approval_ts, approval_ch_id)
        log.info(
            "team_learning: posted approval card cid=%s kind=%s queue=#%s ts=%s",
            cid[:8], kind, queue_name, approval_ts,
        )
    except Exception as exc:
        log.error("team_learning: failed to post approval card cid=%s: %s", cid[:8], exc)


def _handle_note(
    *,
    client,
    say,
    entity: str,
    channel_id: str,
    channel_name: str,
    user_id: str,
    content: str,
    original_ts: str,
    kind: str = "note",
) -> None:
    """Paraphrase a contribution and ask the author to confirm before queuing for approval."""
    if not team_learning.is_authorized_contributor(user_id, entity):
        say(
            text=(
                f"Sorry, you're not registered as a knowledge contributor for *{entity}*. "
                "Contact Harrison to get access."
            ),
            thread_ts=original_ts,
            unfurl_links=False,
            unfurl_media=False,
        )
        log.info("team_learning: unauthorized note attempt user=%s entity=%s", user_id, entity)
        return

    ok, reason = team_learning.screen_contribution(content)
    if not ok:
        say(text=reason, thread_ts=original_ts, unfurl_links=False, unfurl_media=False)
        log.info("team_learning: scope rejection user=%s entity=%s", user_id, entity)
        return

    paraphrase = team_learning.paraphrase_note(content, entity)
    preview_resp = say(
        text=(
            f"{paraphrase}\n\n"
            "Does that capture it? Reply *yes* to queue for approval, "
            "or correct anything above."
        ),
        thread_ts=original_ts,
        unfurl_links=False,
        unfurl_media=False,
    )
    preview_msg_ts = None
    if isinstance(preview_resp, dict):
        preview_msg_ts = preview_resp.get("ts")
    team_learning.store_pending_confirm(
        channel_id=channel_id,
        thread_ts=original_ts,
        entity=entity,
        channel_name=channel_name,
        author=user_id,
        kind=kind,
        raw_content=content,
        paraphrase=paraphrase,
        preview_msg_ts=preview_msg_ts,
    )
    log.info(
        "team_learning: paraphrase posted channel=#%s user=%s kind=%s preview_ts=%s",
        channel_name, user_id, kind, preview_msg_ts,
    )


# Message event handler — correction capture + active-thread follow-up routing.
# Bolt requires an explicit event listener for "message" events.
@app.event("message")
def handle_message_event(event: dict, client) -> None:
    """Thread reply handler: correction capture and active-thread follow-up routing.

    Two paths:
      1. Correction path — if the reply matches a correction pattern, queue it
         for Harrison's approval (existing behaviour, unchanged).
      2. Active-thread path — if the reply is in a thread where Cora previously
         responded (within TTL_SECONDS), treat it as a follow-up question and
         run the full Q&A pipeline without requiring a fresh @mention.
    """
    # ── DM path — route to OSN shift scheduler ───────────────────────────────
    channel_type = event.get("channel_type", "")
    if channel_type == "im":
        user_id = event.get("user", "")
        text = event.get("text", "").strip()
        if user_id and text and not event.get("bot_id"):
            log.info("osn_shift_handler: DM from user=%s text=%r", user_id, text[:80])
            osn_shift_handler.handle_dm(text=text, slack_user_id=user_id, client=client)
        return

    # Hard block: never respond in permanently blocked channels
    if _is_blocked_channel(event.get("channel", "")):
        return

    # Only interested in thread replies (has thread_ts != ts)
    thread_ts = event.get("thread_ts")
    msg_ts = event.get("ts")
    if not thread_ts or thread_ts == msg_ts:
        return  # top-level message, not a reply

    # Skip bot messages
    if event.get("bot_id") or event.get("subtype") == "bot_message":
        return

    channel_id = event.get("channel", "")
    user_id = event.get("user", "")
    if not channel_id or not user_id:
        return

    text = event.get("text", "").strip()
    if not text:
        return

    # ── Path 0: Pending note confirmation loop ────────────────────────────────
    # If this thread is waiting for the author to confirm Cora's paraphrase,
    # handle the reply here before anything else.
    pending = team_learning.get_pending_confirm(channel_id, thread_ts)
    if pending and pending["author"] == user_id:
        channel_name = _resolve_channel_name(client, channel_id)
        say = lambda **kw: client.chat_postMessage(channel=channel_id, **kw)

        if team_learning.is_confirmation(text):
            # Author confirmed — queue the contribution and clear state
            team_learning.clear_pending_confirm(channel_id, thread_ts)
            target_ch = team_learning.kq_channel_for_entity(pending["entity"])
            _queue_contribution(
                client=client,
                entity=pending["entity"],
                channel_id=channel_id,
                channel_name=pending["channel_name"],
                user_id=user_id,
                content=pending["raw_content"],
                original_ts=thread_ts,
                kind=pending["kind"],
            )
            confirmed_text = (
                f"{pending['paraphrase']}\n\n"
                f"✅ Confirmed — queued for approval in #{target_ch}."
            )
            # Feature 7: Update preview message in-place instead of posting a new reply
            preview_ts = pending.get("preview_msg_ts")
            if preview_ts:
                try:
                    client.chat_update(
                        channel=channel_id,
                        ts=preview_ts,
                        text=confirmed_text,
                        unfurl_links=False,
                        unfurl_media=False,
                    )
                except Exception as exc:
                    log.warning("staged_write_update: chat.update failed ts=%s: %s", preview_ts, exc)
                    # Fallback: post as new reply
                    say(
                        text=f"✅ Logged and queued for approval in #{target_ch}.",
                        thread_ts=thread_ts,
                        unfurl_links=False,
                        unfurl_media=False,
                    )
            else:
                say(
                    text=f"✅ Logged and queued for approval in #{target_ch}.",
                    thread_ts=thread_ts,
                    unfurl_links=False,
                    unfurl_media=False,
                )
            # Feature 2: React ✅ to the user's confirming message
            try:
                client.reactions_add(
                    channel=channel_id,
                    name="white_check_mark",
                    timestamp=event["ts"],
                )
            except Exception as exc:
                err_str = str(exc)
                if "already_reacted" not in err_str:
                    log.warning("react_to_confirm: reactions.add failed: %s", exc)
            log.info(
                "team_learning: confirmed channel=#%s user=%s kind=%s",
                channel_name, user_id, pending["kind"],
            )
        else:
            # Author is correcting — re-paraphrase incorporating the correction
            updated = team_learning.paraphrase_note(
                pending["raw_content"], pending["entity"], correction=text
            )
            # Feature 7: update the existing preview message in-place
            preview_ts = pending.get("preview_msg_ts")
            if preview_ts:
                try:
                    client.chat_update(
                        channel=channel_id,
                        ts=preview_ts,
                        text=(
                            f"{updated}\n\n"
                            "Does that capture it? Reply *yes* to queue for approval, "
                            "or correct anything above."
                        ),
                        unfurl_links=False,
                        unfurl_media=False,
                    )
                except Exception as exc:
                    log.warning("staged_write_update: chat.update (correction) failed: %s", exc)
                    preview_ts = None  # Fall through to say() below
            if not preview_ts:
                say(
                    text=updated,
                    thread_ts=thread_ts,
                    unfurl_links=False,
                    unfurl_media=False,
                )
            # Update stored paraphrase so next correction builds on this one; preserve preview_ts
            team_learning.store_pending_confirm(
                channel_id=channel_id,
                thread_ts=thread_ts,
                entity=pending["entity"],
                channel_name=pending["channel_name"],
                author=user_id,
                kind=pending["kind"],
                raw_content=pending["raw_content"],
                paraphrase=updated,
                preview_msg_ts=pending.get("preview_msg_ts"),
            )
            active_thread_store.touch(channel_id, thread_ts)
            log.info(
                "team_learning: re-paraphrased channel=#%s user=%s",
                channel_name, user_id,
            )
        return

    # ── Path 1: Correction capture ────────────────────────────────────────────
    if team_learning.is_correction(text):
        channel_name = _resolve_channel_name(client, channel_id)
        entity = route(channel_name)
        log.info(
            "team_learning: correction detected channel=#%s user=%s",
            channel_name, user_id,
        )
        # Attribute the correction to this person for per-user feedback tracking.
        uft.log_correction(
            slack_user_id=user_id,
            channel=channel_id,
            channel_name=channel_name,
            entity=entity,
            correction_text=text,
        )
        _handle_note(
            client=client,
            say=lambda **kw: client.chat_postMessage(channel=channel_id, **kw),
            entity=entity,
            channel_id=channel_id,
            channel_name=channel_name,
            user_id=user_id,
            content=text,
            original_ts=thread_ts,
            kind="correction",
        )
        return

    # ── Path 2: Active-thread follow-up (no @mention required) ───────────────
    # Only trigger if Cora is known to be active in this thread (within TTL).
    if not active_thread_store.is_active(channel_id, thread_ts):
        return

    allowed, cap_type = rate_limiter.check(user_id, channel_id)
    if not allowed:
        log.warning("rate_limited (path2) user=%s channel=%s cap=%s", user_id, channel_id, cap_type)
        if cap_type == "user":
            client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text="You've hit the per-user mention cap (10/hour). I'll be back shortly.")
        else:
            client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text="This channel has hit the mention cap (50/hour). Try again in a bit.")
        return

    channel_name = _resolve_channel_name(client, channel_id)
    entity = route(channel_name)

    # Apply sibling-entity guard pre-LLM (mirrors handle_mention Path 1).
    sibling_redirect = sibling_guard.check_redirect(entity, text)
    if sibling_redirect:
        client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text=sibling_redirect)
        return

    active_thread_store.touch(channel_id, thread_ts)
    prior_messages = _fetch_thread_history(client, channel_id, thread_ts, msg_ts)

    log.info(
        "thread_followup: active thread channel=#%s user=%s thread_ts=%s",
        channel_name, user_id, thread_ts,
    )

    _dispatch_qa(
        channel_id=channel_id,
        channel_name=channel_name,
        user_id=user_id,
        user_message=text,
        reply_thread_ts=thread_ts,
        entity=entity,
        client=client,
        say=lambda **kw: client.chat_postMessage(channel=channel_id, **kw),
        prior_messages=prior_messages,
        root_thread_ts=thread_ts,
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


def _handle_bookmark_reaction(
    *, client, reactor: str, channel_id: str, channel_name: str, message_ts: str
) -> None:
    """Stage a 📚-bookmarked message as a knowledge contribution for the entity queue."""
    entity = route(channel_name) if channel_name else "FNDR"

    if not team_learning.is_authorized_contributor(reactor, entity):
        try:
            client.chat_postEphemeral(
                channel=channel_id,
                user=reactor,
                text=(
                    f"📚 You're not authorized to contribute knowledge for *{entity}*. "
                    "Contact Harrison to get access."
                ),
            )
        except Exception as exc:
            log.warning("bookmark_reaction: ephemeral auth-fail: %s", exc)
        return

    # Fetch the bookmarked message text via reactions.get (works for thread replies too)
    content = ""
    try:
        resp = client.reactions_get(channel=channel_id, timestamp=message_ts)
        msg_obj = resp.get("message") or {}
        content = msg_obj.get("text", "").strip()
    except Exception as exc:
        log.warning("bookmark_reaction: failed to fetch message ts=%s: %s", message_ts, exc)

    if not content:
        log.info("bookmark_reaction: empty content ts=%s channel=%s — skipping", message_ts, channel_id)
        return

    ok, reason = team_learning.screen_contribution(content)
    if not ok:
        try:
            client.chat_postEphemeral(channel=channel_id, user=reactor, text=reason)
        except Exception as exc:
            log.warning("bookmark_reaction: ephemeral scope-fail: %s", exc)
        log.info("bookmark_reaction: scope rejection reactor=%s entity=%s", reactor, entity)
        return

    cid = team_learning.store_contribution(
        kind="note",
        entity=entity,
        channel_id=channel_id,
        channel_name=channel_name,
        author=reactor,
        content=content,
        original_ts=message_ts,
    )

    queue_name = team_learning.get_queue_channel(entity)
    card_text = team_learning.build_approval_card(
        kind="note",
        entity=entity,
        channel_name=channel_name,
        author=reactor,
        content=content,
        contribution_id=cid,
    )
    try:
        approval_ch_id = _resolve_queue_channel_id(client, entity, channel_id)
        post_resp = client.chat_postMessage(
            channel=approval_ch_id,
            text=card_text,
            unfurl_links=False,
            unfurl_media=False,
        )
        approval_ts = post_resp.get("ts", "")
        team_learning.set_approval_msg(cid, approval_ts, approval_ch_id)
        log.info(
            "bookmark_reaction: staged cid=%s entity=%s queue=#%s", cid[:8], entity, queue_name
        )
    except Exception as exc:
        log.error("bookmark_reaction: failed to post approval card cid=%s: %s", cid[:8], exc)
        return

    try:
        client.chat_postEphemeral(
            channel=channel_id,
            user=reactor,
            text=f"📚 Queued for *{entity}* review in `#{queue_name}`. Thanks!",
        )
    except Exception as exc:
        log.warning("bookmark_reaction: ephemeral confirm failed: %s", exc)


def _handle_reaction(event: dict, client, event_type: str) -> None:
    """Shared logic for reaction_added and reaction_removed events."""
    item = event.get("item") or {}
    if item.get("type") != "message":
        return  # ignore reactions on files, channel boundaries, etc.

    channel_id = item.get("channel", "")
    reactor = event.get("user", "")
    reaction = event.get("reaction", "")
    message_ts = item.get("ts", "")

    # ── 📚 bookmark: runs on ANY message, not just Cora's ────────────────────
    if event_type == "reaction_added" and reaction == "books" and channel_id and reactor:
        channel_name = _resolve_channel_name(client, channel_id)
        _handle_bookmark_reaction(
            client=client,
            reactor=reactor,
            channel_id=channel_id,
            channel_name=channel_name,
            message_ts=message_ts,
        )
        return

    item_user = event.get("item_user", "")
    bot_user_id = _resolve_bot_user_id(client)
    if not bot_user_id or item_user != bot_user_id:
        # Reaction on a non-Cora message - not our signal to capture
        return

    channel_name = _resolve_channel_name(client, channel_id) if channel_id else ""

    # ── HubSpot email sync: 👍/👎 on an ambiguous-match DM ──────────────────────
    # When Cora DMs about an ambiguous email→HubSpot match, Harrison reacts
    # 👍 to attach the thread or 👎 to skip. Runs before the bot_user_id gate
    # because DM messages from Cora are item_user=bot but we want to catch this
    # early for both DM channels (channel_type "im") and regular channels.
    if event_type == "reaction_added" and reaction in ("+1", "thumbsup", "-1", "thumbsdown"):
        try:
            from cora.connectors.hubspot_email_sync import (
                get_pending_reaction,
                resolve_pending_reaction,
            )
            pending = get_pending_reaction(message_ts)
            if pending:
                approved = reaction in ("+1", "thumbsup")
                resolve_pending_reaction(message_ts, approved=approved)
                ack = (
                    ":white_check_mark: Got it — email thread attached to HubSpot."
                    if approved
                    else ":x: Skipped — thread won't be attached."
                )
                try:
                    client.chat_postMessage(
                        channel=channel_id,
                        text=ack,
                        thread_ts=message_ts,
                        unfurl_links=False,
                        unfurl_media=False,
                    )
                except Exception:
                    pass  # DM thread reply is best-effort
                log.info(
                    "email_sync: reaction %s on pending DM ts=%s approved=%s",
                    reaction, message_ts, approved,
                )
        except Exception as exc:
            log.warning("email_sync reaction handler failed: %s", exc)

    # ── OSN shift scheduler: ✅ on a schedule message approves + publishes it ──
    if event_type == "reaction_added" and reaction == "white_check_mark":
        sched_reply = osn_shift_handler.handle_schedule_approval_reaction(
            message_ts=message_ts, channel_id=channel_id, client=client
        )
        if sched_reply:
            client.chat_postMessage(channel=channel_id, text=sched_reply)

    # ── 📚 bookmark: works on ANY message, not just Cora's ───────────────────
    # When a team member reacts 📚 to any message, capture its text as a
    # pending KB contribution and route it to the entity's KQ channel for approval.
    if event_type == "reaction_added" and reaction == "books" and reactor and channel_id:
        try:
            hist = client.conversations_history(
                channel=channel_id, latest=message_ts, limit=1, inclusive=True
            )
            msgs = (hist.get("messages") or [])
            msg_text = msgs[0].get("text", "").strip() if msgs else ""
            if msg_text:
                entity = route(channel_name) if channel_name else "FNDR"
                _handle_note(
                    client=client,
                    say=lambda **kw: None,  # no ack — reaction is silent
                    entity=entity,
                    channel_id=channel_id,
                    channel_name=channel_name,
                    user_id=reactor,
                    content=msg_text,
                    original_ts=message_ts,
                    kind="bookmark",
                )
                log.info(
                    "bookmark reaction cid=queued channel=#%s reactor=%s",
                    channel_name, reactor,
                )
        except Exception as exc:
            log.warning("bookmark reaction handler failed: %s", exc)
        # bookmarks are on any message — don't gate the rest on bot_user_id

    item_user = event.get("item_user", "")
    bot_user_id = _resolve_bot_user_id(client)
    if not bot_user_id or item_user != bot_user_id:
        # Remaining handlers only apply to reactions on Cora's own messages
        return

    # ── OSN shift scheduler: ✅ on a schedule message approves + publishes it ──
    if event_type == "reaction_added" and reaction == "white_check_mark":
        sched_reply = osn_shift_handler.handle_schedule_approval_reaction(
            message_ts=message_ts, channel_id=channel_id, client=client
        )
        if sched_reply:
            client.chat_postMessage(channel=channel_id, text=sched_reply)

    # ── Team learning: approval/decline of pending contributions ──────────────
    # Only process ✅ / ❌ on reaction_added (not removal). Look up by the ts
    # of the approval card Cora posted.
    if event_type == "reaction_added" and reaction in ("white_check_mark", "x"):
        contribution = team_learning.lookup_by_approval_ts(message_ts)
        if contribution:
            _process_contribution_reaction(
                client=client,
                contribution=contribution,
                reaction=reaction,
                approval_channel_id=channel_id,
                approval_msg_ts=message_ts,
                reactor_id=reactor,
            )
            # Fall through to also log the reaction as normal feedback

    # ── Knowledge-review: capture Harrison 👍/👎/💬 on proposed-update DMs ──
    # Only log when Harrison (sole-authority reactor) reacts with an actionable
    # emoji AND the update corresponds to a DM channel (starts with "D").
    # We capture ALL reaction_added AND reaction_removed events — the
    # correlate_reactions_to_updates() function uses the first APPROVED/DISMISSED
    # on a given message_ts, so order is stable.
    if reactor == knowledge_review.HARRISON_SLACK_USER_ID:
        action = knowledge_review.classify_reaction(reaction)
        if action in ("APPROVED", "DISMISSED", "COMMENT_REQUESTED"):
            knowledge_review.log_reply_reaction(
                reactor_id=reactor,
                reaction=reaction,
                message_ts=message_ts,
                channel_id=channel_id,
                channel_name=channel_name,
                event_type=event_type,
            )

    feedback_log.log_reaction(
        channel=channel_id,
        channel_name=channel_name,
        reactor=reactor,
        reaction=reaction,
        message_ts=message_ts,
        event_type=event_type,
    )

    # Per-user feedback attribution — only track negative reactions to Cora messages.
    if (
        event_type == "reaction_added"
        and feedback_log.classify_sentiment(reaction) == "negative"
        and reactor
    ):
        entity = route(channel_name) if channel_name else "FNDR"
        uft.log_thumbsdown(
            slack_user_id=reactor,
            channel=channel_id,
            channel_name=channel_name,
            entity=entity,
            message_ts=message_ts,
        )


def _process_contribution_reaction(
    *,
    client,
    contribution: dict,
    reaction: str,
    approval_channel_id: str,
    approval_msg_ts: str,
    reactor_id: str | None = None,
) -> None:
    """Process a ✅ or ❌ on a pending contribution approval card."""
    entity = contribution.get("entity", "")
    # Harrison (founder) can approve any entity. Registered entity approvers can
    # approve only their authorized entities. Anyone else is ignored.
    if reactor_id != _FOUNDER_ID and not team_learning.is_approver(reactor_id, entity):
        log.warning(
            "team_learning: KB reaction from unauthorized reactor %s for entity %s — ignored",
            reactor_id, entity,
        )
        return

    import datetime as _dt
    cid = contribution["contribution_id"]
    if reaction == "white_check_mark":
        # Approve: ingest to KB
        success = team_learning.ingest_contribution(contribution)
        team_learning.resolve_contribution(cid, "approved")
        if success:
            reply = f"✅ Contribution `[{cid[:8]}]` approved and added to Cora's knowledge base."
        else:
            reply = (
                f"⚠️ Approved `[{cid[:8]}]` but KB ingest failed — "
                "check logs. Contribution marked approved but not in KB."
            )
        log.info("team_learning: contribution %s approved ingest_ok=%s", cid[:8], success)

        # Feature 2: React ✅ to the approval card to signal it was processed
        try:
            client.reactions_add(
                channel=approval_channel_id,
                name="white_check_mark",
                timestamp=approval_msg_ts,
            )
        except Exception as _react_exc:
            if "already_reacted" not in str(_react_exc):
                log.warning("react_to_approve: reactions.add failed: %s", _react_exc)

        # Post audit record to Harrison's private KB log channel
        kind = contribution.get("kind", "note")
        kind_label = "Team Note" if kind == "note" else ("Bookmark" if kind == "bookmark" else "Correction")
        approver_mention = f"<@{reactor_id}>" if reactor_id else "_(unknown)_"
        author_mention = f"<@{contribution['author']}>"
        approved_at = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        content_preview = contribution.get("content", "")[:600]
        audit_text = (
            f"📋 *KB Approval* `[{cid[:8]}]`\n"
            f"*Approved by:* {approver_mention}  |  "
            f"*Entity:* {contribution['entity']}  |  "
            f"*Kind:* {kind_label}\n"
            f"*Submitted by:* {author_mention}  |  "
            f"*Source:* #{contribution['channel_name']}  |  "
            f"*At:* {approved_at}\n"
            f"```\n{content_preview}\n```"
        )
        try:
            client.chat_postMessage(
                channel=team_learning.KB_AUDIT_CHANNEL,
                text=audit_text,
                unfurl_links=False,
                unfurl_media=False,
            )
        except Exception as exc:
            log.warning("team_learning: failed to post KB audit record cid=%s: %s", cid[:8], exc)
    else:
        # Decline
        team_learning.resolve_contribution(cid, "declined")
        reply = f"❌ Contribution `[{cid[:8]}]` declined. Nothing added to KB."
        log.info("team_learning: contribution %s declined", cid[:8])

    try:
        client.chat_postMessage(
            channel=approval_channel_id,
            thread_ts=approval_msg_ts,
            text=reply,
            unfurl_links=False,
            unfurl_media=False,
        )
    except Exception as exc:
        log.warning("team_learning: failed to post resolution reply: %s", exc)

    # Feature 3: Pin approved decisions to the entity's leadership channel
    if reaction == "white_check_mark" and _is_decision_content(contribution.get("content", "")):
        leadership_ch = _entity_leadership_channel(contribution.get("entity", ""))
        original_ts = contribution.get("original_ts")
        if leadership_ch and original_ts:
            try:
                client.pins_add(channel=leadership_ch, timestamp=original_ts)
                log.info(
                    "pin_decision: pinned cid=%s to #%s ts=%s",
                    contribution.get("contribution_id", "?")[:8], leadership_ch, original_ts,
                )
            except Exception as exc:
                err_str = str(exc)
                if "already_pinned" not in err_str and "no_pin" not in err_str:
                    log.warning("pin_decision: pins.add failed: %s", exc)


@app.event("reaction_added")
def handle_reaction_added(event: dict, client) -> None:
    _handle_reaction(event, client, "reaction_added")


@app.event("reaction_removed")
def handle_reaction_removed(event: dict, client) -> None:
    _handle_reaction(event, client, "reaction_removed")


@app.event("channel_created")
def handle_channel_created(event: dict, client) -> None:
    """Auto-join every new public channel so the nightly sweep has full coverage."""
    ch = event.get("channel") or {}
    ch_id = ch.get("id", "")
    ch_name = ch.get("name", "")
    if not ch_id:
        return
    try:
        client.conversations_join(channel=ch_id)
        log.info("auto-joined new channel #%s (%s)", ch_name, ch_id)
    except Exception as exc:
        log.warning("failed to auto-join #%s: %s", ch_name, exc)
