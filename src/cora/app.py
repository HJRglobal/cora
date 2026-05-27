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


@app.event("app_mention")
def handle_mention(event: dict, say: callable, client) -> None:
    channel_id = event.get("channel", "")
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
    entity = route(channel_name)
    user_message = _MENTION_RE.sub("", raw_text).strip()
    function = channel_classifier.classify_function(channel_name)
    tier = channel_classifier.tier_label(entity, function)

    # ── Write-back interception: @Cora note: <content> ────────────────────────
    # Handled before Q&A pipeline — no KB retrieval, no Claude call.
    note_content = team_learning.parse_note(user_message)
    if note_content:
        _handle_note(
            client=client, say=say,
            entity=entity, channel_id=channel_id, channel_name=channel_name,
            user_id=user_id or "", content=note_content, original_ts=thread_ts or "",
        )
        return

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

    # Sibling-entity redirect interception: for LEX sub-entity channels, if the
    # message asks about a sibling entity (e.g. LLA question in #llc channel),
    # return the one-sentence redirect deterministically — no LLM call needed.
    # This bypasses the LLM's helpfulness bias overriding format instructions.
    sibling_redirect = sibling_guard.check_redirect(entity, user_message)
    if sibling_redirect:
        log.info(
            "sibling-entity redirect fired channel=#%s entity=%s",
            channel_name, entity,
        )
        say(
            text=sibling_redirect,
            thread_ts=thread_ts,
            unfurl_links=False,
            unfurl_media=False,
        )
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

    # ── Intent classification + semantic cache ─────────────────────────────
    # 1. Classify the question to get routing hints (no LLM call — regex only).
    # 2. Embed the question once — reused for both cache lookup and KB search.
    # 3. Check the semantic cache before touching the KB or Claude.
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
                    thread_ts=thread_ts,
                    unfurl_links=False,
                    unfurl_media=False,
                )
                return
        except Exception as exc:
            # Cache miss due to error — not fatal; proceed normally
            log.warning("semantic_cache lookup error for entity=%s: %s", entity, exc)

    # ── Context loading (KB-augmented or static-only) ─────────────────────
    # Phase 3: pass user_message as query so context_loader can augment with
    # KB retrieval (top-K semantically-relevant chunks from cora_kb.db).
    # Intent routing: FINANCIAL skips KB; SIMPLE/TASK_LOOKUP reduce k.
    context = load_context(
        entity,
        query=user_message,
        skip_kb=hints.skip_kb,
        kb_k=hints.kb_k_override,
    )
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
                prior_messages=prior_messages,
            )
        except ClaudeClientError as exc:
            log.error("ClaudeClientError for entity=%s user=%s: %s", entity, user_id, exc)
            say(text=user_facing_message(exc), thread_ts=thread_ts)
            return

        latency_ms = int((time.monotonic() - t0) * 1000)
        response_text = _extract_and_log_gap(
            response_text, entity, channel_name, user_id, user_message, latency_ms,
        )
        _try_cache_store(entity, user_message, question_embedding, response_text, hints)
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
            prior_messages=prior_messages,
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
    _try_cache_store(entity, user_message, question_embedding, response_text, hints)

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
    """Store a pending write-back/correction and post an approval card to #hjrg-leadership."""
    cid = team_learning.store_contribution(
        kind=kind,
        entity=entity,
        channel_id=channel_id,
        channel_name=channel_name,
        author=user_id,
        content=content,
        original_ts=original_ts,
    )

    # Acknowledge in the source channel
    ack = "✅ Got it — pending Harrison's approval." if kind == "note" else "🔄 Correction noted — pending Harrison's approval."
    say(text=ack, thread_ts=original_ts, unfurl_links=False, unfurl_media=False)

    # Post approval card to #hjrg-leadership
    card_text = team_learning.build_approval_card(
        kind=kind,
        entity=entity,
        channel_name=channel_name,
        author=user_id,
        content=content,
        contribution_id=cid,
    )
    try:
        # Look up #hjrg-leadership channel ID dynamically
        search_resp = client.conversations_list(types="public_channel,private_channel", limit=200)
        approval_ch_id = channel_id  # fallback to source channel
        for ch in search_resp.get("channels", []):
            if ch.get("name") == team_learning.APPROVAL_CHANNEL:
                approval_ch_id = ch["id"]
                break

        post_resp = client.chat_postMessage(
            channel=approval_ch_id,
            text=card_text,
            unfurl_links=False,
            unfurl_media=False,
        )
        approval_ts = post_resp.get("ts", "")
        team_learning.set_approval_msg(cid, approval_ts, approval_ch_id)
        log.info(
            "team_learning: posted approval card cid=%s kind=%s ts=%s",
            cid[:8], kind, approval_ts,
        )
    except Exception as exc:
        log.error("team_learning: failed to post approval card cid=%s: %s", cid[:8], exc)


# Correction capture: message event in a thread where Cora replied last.
# Bolt requires an explicit event listener for "message" events.
@app.event("message")
def handle_message_event(event: dict, client) -> None:
    """Capture correction-intent thread replies directed at Cora's output."""
    # Only interested in thread replies (has thread_ts != ts)
    thread_ts = event.get("thread_ts")
    msg_ts = event.get("ts")
    if not thread_ts or thread_ts == msg_ts:
        return  # top-level message, not a reply

    # Skip bot messages
    if event.get("bot_id") or event.get("subtype") == "bot_message":
        return

    text = event.get("text", "").strip()
    if not team_learning.is_correction(text):
        return

    channel_id = event.get("channel", "")
    user_id = event.get("user", "")
    if not channel_id or not user_id:
        return

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
            )
            # Fall through to also log the reaction as normal feedback

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
) -> None:
    """Process a ✅ or ❌ on a pending contribution approval card."""
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


@app.event("reaction_added")
def handle_reaction_added(event: dict, client) -> None:
    _handle_reaction(event, client, "reaction_added")


@app.event("reaction_removed")
def handle_reaction_removed(event: dict, client) -> None:
    _handle_reaction(event, client, "reaction_removed")
