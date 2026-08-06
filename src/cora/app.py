"""Bolt app and event handlers."""

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from slack_bolt import App

from .claude_client import (
    ClaudeClientError,
    generate_response,
    generate_response_streaming,
    user_facing_message,
)
from . import active_thread_store
from . import channel_classifier
from . import channel_content_guard
from . import confirm_cards
from . import user_access
from . import lex_phi_access
from .config import config
from .context_loader import load_context_parts
from .entity_router import route
from . import feedback_log
from . import help_responder
from . import knowledge_review
from . import missed_message_catchup as missed_catchup
from .revops import cards as revops_cards
from . import intent_classifier as ic
from . import knowledge_gaps
from . import gap_detection
from . import gap_autofill
from . import code_queue
from . import delegated_work
from .knowledge_base import embeddings as kb_embeddings
from . import sibling_guard
from . import cross_entity_guard
from . import info_intake
from . import historical_access
from . import finance_receipts
from . import model_router
from . import org_roles
from . import phi_guard
from .prompt_loader import load_prompt
from . import rate_limiter
from .reply_formatter import format_reply
from . import semantic_cache as sc
from . import slack_update_throttle
from . import team_learning
from . import user_feedback_tracker as uft
from . import web_guard
from .tools import user_identity
from .tools import osn_shift_handler
from .tools import tool_dispatch as _tool_dispatch

log = logging.getLogger(__name__)

app = App(token=config.slack_bot_token, signing_secret=config.slack_signing_secret)


@app.middleware
def _dedup_event_deliveries(body, next, logger):  # noqa: A002 -- Bolt's contract names it `next`
    """Suppress duplicate Slack event DELIVERIES (cq-479b157f8c00): Socket Mode is
    at-least-once, and an ack lost on a flapping WebSocket makes Slack redeliver —
    each delivery previously ran the full Q&A pipeline (two contradictory replies
    14ms apart, live 7/27). Keyed STRICTLY on the events-API top-level event_id
    (commands / block-actions / shortcuts carry none and pass through; the
    app_mention+message dual-path for one message has distinct event_ids and is
    governed by W1-01, not this). Returning a 200 BoltResponse without next()
    halts dispatch AND acks the envelope so Slack stops retrying. Fail-open:
    any error here must never block dispatch."""
    try:
        from slack_bolt.response import BoltResponse

        from . import event_dedup

        event_id = body.get("event_id") if isinstance(body, dict) else None
        if event_id and event_dedup.is_duplicate(event_id):
            etype = ((body.get("event") or {}).get("type")
                     if isinstance(body, dict) else None)
            log.warning("event_dedup: duplicate delivery suppressed event_id=%s type=%s",
                        event_id, etype)
            return BoltResponse(status=200, body="duplicate event delivery ignored")
    except Exception:  # noqa: BLE001 -- dedup must never 500 an event
        log.warning("event_dedup: middleware error (failing open)", exc_info=True)
    return next()


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

# ── Channel-link validation ────────────────────────────────────────────────────
# The LLM occasionally invents Slack channel tokens (<#Cxxxx|name>) with
# fabricated IDs; Slack renders those as broken links (observed 2026-06-10 in a
# PHI-redirect reply). Verify each ID via conversations_info and degrade invalid
# ones to plain "#name" text. Applies to ALL replies including tool outputs —
# genuine tool-emitted channel IDs validate fine, so this is safe alongside the
# D-032 tool-output bypass.
_CHANNEL_LINK_RE = re.compile(r"<#(C[A-Z0-9]+)(?:\|([^>]*))?>")
_channel_link_cache: dict[str, bool] = {}  # channel_id -> exists

# Known-bad plain-text channel names the LLM produces by blending the #lex-
# prefix with sub-entity codes (taught by a since-fixed fndr.md line; stale
# copies persist in swept Slack history and KB chunks). The real families are
# #llc*, #lts*, #lbhs*, #lla*. Rewrite deterministically — "#lex-llc-leadership"
# also corrects to "#llc-leadership".
_LEX_CHANNEL_ALIAS_RE = re.compile(r"#lex-(llc|lts|lbhs|lla)\b")


def _fix_lex_channel_names(text: str) -> str:
    """Rewrite nonexistent #lex-<subentity> channel names to the real family."""
    if "#lex-" not in text:
        return text
    fixed, n = _LEX_CHANNEL_ALIAS_RE.subn(r"#\1", text)
    if n:
        log.warning("lex_channel_alias rewritten: %d occurrence(s)", n)
    return fixed


def _validate_channel_links(text: str, client) -> str:
    """Replace channel links whose IDs don't resolve with plain '#name' text."""
    if "<#" not in text:
        return text

    def _sub(m: re.Match) -> str:
        cid, label = m.group(1), m.group(2) or ""
        ok = _channel_link_cache.get(cid)
        if ok is None:
            try:
                resp = client.conversations_info(channel=cid)
                ok = bool(resp.get("ok"))
                _channel_link_cache[cid] = ok
            except Exception as exc:  # noqa: BLE001
                if "channel_not_found" in str(exc):
                    ok = False
                    _channel_link_cache[cid] = ok
                else:
                    # Transient API error — keep the token, don't cache a verdict.
                    log.warning("channel-link check failed for %s: %s", cid, exc)
                    return m.group(0)
        if ok:
            return m.group(0)
        log.warning("invalid_channel_link stripped: id=%s label=%s", cid, label)
        return f"#{label}" if label else "the relevant channel"

    return _CHANNEL_LINK_RE.sub(_sub, text)

# Resolved at first event via auth.test() - the bot's own user ID. Used to
# filter reaction_added events down to "user reacted to a Cora message" only.
# #info-for-cora intake channel (D1): user-fed facts here are routed into the
# Harrison-gated knowledge-review queue instead of being dropped. Patchable in
# tests via app_module.INFO_FOR_CORA_CHANNEL_ID.
#
# Sourced from info_intake so the event path, the @mention path, and the
# reconciling sweep cannot drift onto different channel ids (2026-08-06).
INFO_FOR_CORA_CHANNEL_ID = info_intake.CHANNEL_ID
_INFO_FOR_CORA_SKIP_SUBTYPES = frozenset({
    "message_changed", "message_deleted", "channel_join", "channel_leave",
    "channel_topic", "channel_purpose", "channel_name", "channel_archive",
    "channel_unarchive", "bot_message", "thread_broadcast",
})

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


# Channel name cache: avoids a Slack API call on every mention.
# Keyed by channel_id → (name, cached_at). TTL = 30 minutes.
# Channel names rarely change; stale cache for a renamed channel is acceptable.
_CHANNEL_NAME_CACHE: dict[str, tuple[str, float]] = {}
_CHANNEL_NAME_TTL = 1800  # 30 minutes


def _resolve_channel_name(client, channel_id: str) -> str:
    now = time.monotonic()
    cached = _CHANNEL_NAME_CACHE.get(channel_id)
    if cached is not None:
        name, cached_at = cached
        if now - cached_at < _CHANNEL_NAME_TTL:
            return name

    try:
        info = client.conversations_info(channel=channel_id)
        name = info["channel"]["name"]
        _CHANNEL_NAME_CACHE[channel_id] = (name, now)
        return name
    except Exception as exc:
        log.warning("Could not resolve channel name for %s: %s", channel_id, exc)
        # Cache the fallback too so we don't hammer Slack on a dead channel
        _CHANNEL_NAME_CACHE[channel_id] = (channel_id, now)
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


def _build_grant_context(
    grant: "historical_access.AccessDecision",
    query: str,
    user_id: str,
    channel_name: str,
    query_vec: "list[float] | None",
) -> str:
    """Fetch + format owner-authorized chunks for a Tier-2 / finance grant.

    Personal mode: owner-scoped search over the asker's (or, for an
    unrestricted asker, the named teammate's) mailboxes — full headers/links.
    Finance mode: financial_document-tagged chunks from any/scoped mailboxes,
    best-effort auto-filed into the Receipts & Invoices Inbox, every pull
    audit-logged. Both modes pass a defensive PHI filter.
    """
    from .context_loader import owned_kb_search

    # F-21: a "latest / most recent email" ask (personal mode) is answered
    # newest-first, not by pure vector similarity.
    recency = grant.mode != "finance" and historical_access.is_recency_query(query)
    try:
        results = owned_kb_search(
            query,
            grant.owner_emails,
            financial_only=(grant.mode == "finance"),
            k=12,
            query_vec=query_vec,
            recency_first=recency,
        )
    except Exception as exc:  # noqa: BLE001 — retrieval failure = empty, not crash
        log.error("historical_access: owned_kb_search failed user=%s: %s", user_id, exc)
        results = []

    results = historical_access.drop_phi(results)

    if grant.mode == "finance":
        filed_links: dict[str, str] = {}
        try:
            filed_links = finance_receipts.auto_file_results(results)
        except Exception as exc:  # noqa: BLE001
            log.warning("finance_receipts: on-demand auto-file failed: %s", exc)
        finance_receipts.audit(
            requester=user_id, query=query, owner_emails=grant.owner_emails,
            items=results, channel=channel_name,
        )
        return finance_receipts.format_finance_chunks(
            results, grant.target_label, filed_links,
        )

    historical_access.audit(
        requester=user_id, query=query, mode="personal",
        owner_emails=grant.owner_emails,
        items=[r.source_id for r in results], channel=channel_name,
    )
    label = "your" if grant.target_label == "your" else f"{grant.target_label}'s"
    return historical_access.format_owned_chunks(results, label, recency_first=recency)


# ── Asana task-action intent detector (F-23 Slice 2) ────────────────────────
# A clear delete/complete/create-task request must produce a TOOL-generated preview
# (+ server-side pending entry the confirm interceptor can later execute), never a
# haiku-fabricated one. When this fires, _dispatch_qa forces Sonnet AND forces the
# tool via tool_choice. Conservative: interrogatives are excluded, and every branch
# requires an explicit task referent (delete/create verbs are also used for events,
# drafts, reports, etc.). A miss is safe -- the interceptor + Slice 3 phantom guard
# still prevent a fabricated success on the follow-up confirm turn.
_ASANA_INTENT_INTERROGATIVE_RE = re.compile(
    r"^\s*(?:who|what|which|whose|whom|did|do|does|is|are|was|were|has|have|had|"
    r"can|could|would|should|when|where|why|how)\b|\?\s*$",
    re.IGNORECASE,
)
_ASANA_TASK_REF_RE = re.compile(r"\btasks?\b|\bto-?dos?\b", re.IGNORECASE)
# GOVERNANCE-REQUIRED (review MED #7): the verb must govern a task, not appear as an
# adjective ("the complete list of tasks") or an overloaded object ("remove Bob as a
# follower on the task"). "remove" is dropped entirely -- too overloaded (remove follower
# / from a list) and firing a PERMANENT delete on it is the dangerous case.
_ASANA_DELETE_INTENT_RE = re.compile(
    r"\b(?:delete|trash)\b[^.\n]{0,40}\b(?:tasks?|to-?dos?)\b"
    r"|\b(?:tasks?|to-?dos?)\b[^.\n]{0,25}\b(?:delete[d]?|trashed?)\b"
    r"|\bget rid of\b[^.\n]{0,40}\b(?:tasks?|to-?dos?)\b",
    re.IGNORECASE)
_ASANA_COMPLETE_INTENT_RE = re.compile(
    r"\bmark(?:ed|ing)?\b[^.\n]{0,30}\b(?:done|complete[d]?|finished|off)\b"
    r"|\b(?:complete|finish|close out|check off)\s+(?:the|my|this|that|our)\b[^.\n]{0,30}\b(?:tasks?|to-?dos?)\b"
    r"|\b(?:complete|finish|close out|check off)\s+(?:it|that|this)\b",
    re.IGNORECASE)
_ASANA_CREATE_INTENT_RE = re.compile(
    r"\b(?:create|make|set up|start)\b[^.\n]{0,24}\b(?:tasks?|to-?dos?)\b"
    r"|\b(?:new|another)\s+(?:asana\s+)?tasks?\b",
    re.IGNORECASE)
# PM-hub Phase 1 (2026-07-15): edit-tool intents. Each requires a task referent (the
# shared gate) EXCEPT subtask, which is self-referential ("subtask" has no standalone
# "task" word boundary) and carries its own verb, so it is checked before the gate.
_ASANA_SUBTASK_INTENT_RE = re.compile(
    r"\b(?:add|create|make|new|break|split|divide)\b[^.\n]{0,24}\bsub-?tasks?\b",
    re.IGNORECASE)
_ASANA_COMMENT_INTENT_RE = re.compile(
    r"\b(?:comment|leave a comment|add a comment|add a note|leave a note)\b"
    r"[^.\n]{0,24}\b(?:on|to|under)\b[^.\n]{0,24}\b(?:tasks?|to-?dos?)\b",
    re.IGNORECASE)
# D-051 (2026-07-15): the field-change verb must GOVERN the field noun (an optional
# article between them), not merely co-occur within 24 chars -- otherwise overloaded
# reads like "update me on the status of the deck task" / "any update on the deadline"
# were forced into the WRITE tool. Mirrors the delete/complete governance requirement.
_ASANA_UPDATE_INTENT_RE = re.compile(
    r"\bre-?assign\b"
    r"|\b(?:change|update|move|push|bump|extend|set|reset)\s+(?:the|its|this|that|my|our)?\s*"
    r"(?:due date|due-date|deadline|due on|priority|status)\b"
    r"|\brename\b[^.\n]{0,24}\b(?:tasks?|to-?dos?)\b",
    re.IGNORECASE)


# ── Explicit code-queue capture intent (cq-a1306f3835f8, 2026-08-05) ─────────
# THE BUG THIS FIXES: "@Cora queue a code session: <free text>" misrouted to an
# Asana task op (first seen 7/29 in the D-090 smokes, again in the 8/5 sweep).
# Root cause is intent PRECEDENCE, not tool selection: _asana_destructive_intent
# reads the DESCRIPTION text after the colon, and a bug report about tasks
# ("... marking a task done doesn't work") satisfies its task-referent gate plus a
# verb branch. It then forces that tool via tool_choice, which makes
# cora_queue_code_session literally UNREACHABLE for the turn. Six days as the
# longest-unresolved item in the 8/5 sweep, and it degrades the exact escape hatch
# people use to report bugs -- so the explicit phrase must WIN.
#
# Precision-biased, mirroring the Asana detector's house style:
#  - GOVERNANCE required: an explicit file-it verb must govern an explicit
#    CODE-QUEUE NOUN within 30 chars. A soft complaint ("it's broken", "Cora
#    should ...") is deliberately NOT here -- those are code_queue._PHRASE_RE
#    signals that ride the async Harrison-gated classifier, and force-filing a
#    card on every complaint would flood the queue.
#  - IMPERATIVE only: the verb list is present-tense/imperative. "I already queued
#    a code session" / "we logged this for the devs" describe a past action and must
#    not re-file. This is the precision lever that replaces an earlier positional
#    window (see the D-051 note below).
#  - DELIBERATION/NEGATION before the match disqualifies it ("I don't think we
#    should queue a code session", "instead of queueing one", "should we queue...").
#  - An ASANA/CALENDAR REFERENT before the match disqualifies it, so "create a task
#    to queue a code session" stays a task request.
#
# D-051 REVIEW REMEDIATION (2026-08-06, two HIGH findings on the first cut):
#  HIGH-1 -- a bare "for the devs?" object had no code/build noun in it, so
#    "add a subtask for the devs under the Pure launch task" / "add a comment for
#    the devs on the invoice task" / "add a calendar hold for the devs sync" all
#    fired. That SUPPRESSED the Asana force and forced the capture tool instead, so
#    the user's actual subtask/comment was never created -- a DISPLACED action, which
#    is worse than the dismissable-card cost the first version reasoned about.
#    "log this for the devs" is a real trigger from the tool's own description, so it
#    survives as a TIGHT alternative requiring log/file/queue + a demonstrative.
#  HIGH-2 -- a 60-char leading window was defeated by one ordinary clause of
#    preamble ("Following up from the leadership sync this morning, can you please
#    queue a code session: ..." starts at 67), and on a miss the ORIGINAL bug
#    returned because the Asana force won. Worse, `_MENTION_RE` strips only ONE
#    LEADING mention, so another person's `<@U...>` or a `<#C...|channel>` reference
#    burned the window. The window is REMOVED; the imperative-verb list plus the
#    before-match guards do the same job without a positional cliff.
_CODE_QUEUE_INTENT_RE = re.compile(
    # An explicit code-queue noun, governed by an imperative file-it verb.
    r"\b(?:queue|queueing|queuing|log|file|add|put|open)\b[^.\n]{0,30}?\b"
    r"(?:code[\s-]?session|code[\s-]?queue|build[\s-]?queue|dev(?:eloper)?[\s-]?queue)\b"
    # ...or the description's own "log this for the devs" phrasing, which needs a
    # demonstrative so it cannot absorb "add a comment for the devs on the X task".
    r"|\b(?:log|file|queue)\s+(?:this|that|it)\b[^.\n]{0,20}?\bfor the devs?\b",
    re.IGNORECASE,
)
# Anything in the text BEFORE the match that reframes it as hypothetical, negated,
# deliberative, or a rejected alternative.
# Bare "never" and "without" are deliberately ABSENT: they are stock bug prose ("the
# comment never posts", "it saves without asking"), and including them made
# "the invoice task comment never posts, queue a code session" fail (lens-6 second
# pass). Only request-negating forms belong here.
_CODE_QUEUE_NEGATION_RE = re.compile(
    r"\b(?:don'?t|do not|dont|shouldn'?t|should not|no need|not\s+(?:worth|going|gonna)|"
    r"instead of|rather than|never ?mind|why would|nothing to|"
    r"should\s+(?:we|i)|is it worth|do we need|would it help|do you think|"
    r"maybe|perhaps)\b",
    re.IGNORECASE,
)
# An Asana/calendar referent before the match, WITH the capture phrase subordinated
# by "to", means the sentence is about that object ("create a task TO queue a code
# session"), not about filing a build.
#
# SECOND D-051 PASS (lens 6, 2026-08-06): the first version of this guard was a bare
# noun scan over the preceding text, which REINTRODUCED cq-a1306f3835f8 for the
# clause-swapped ordering -- "marking a task done doesn't work -- queue a code session
# for it" (this branch's own repro sentence with its clauses swapped) disqualified on
# the word "task", and _asana_destructive_intent then forced asana_complete_task. The
# committed fixtures only carried the phrase-FIRST ordering, so the suite was green
# over a re-broken defect. Requiring the subordinating "to" keeps the narrow case it
# was for and cannot swallow a bug report that merely NAMES a task.
_CODE_QUEUE_OTHER_REFERENT_RE = re.compile(
    r"\b(?:sub-?tasks?|tasks?|to-?dos?|comments?|notes?|holds?|invites?|events?|"
    r"meetings?|reminders?|tickets?)\b",
    re.IGNORECASE,
)
_CODE_QUEUE_SUBORDINATED_RE = re.compile(r"\bto\s+$", re.IGNORECASE)


def _code_queue_capture_intent(text: str) -> bool:
    """True for an EXPLICIT "file this to the code queue" command. See the module
    section above for why this must take precedence over the Asana task-op force,
    and for the two HIGH review findings that shaped the guards.

    Known residual (accepted, documented): the disqualifier scan looks only at text
    BEFORE the match, so a retraction that TRAILS it ("queue a code session for that
    -- actually never mind") still fires. Cost is one dismissable Harrison-gated card
    and a wasted turn, never a write. It is deliberately not guarded: the text after
    a capture phrase is arbitrary bug prose that legitimately contains "unless",
    "only if", "no" -- scanning it would break the primary use case to fix a
    cosmetic one.
    """
    t = (text or "").strip()
    if not t:
        return False
    m = _CODE_QUEUE_INTENT_RE.search(t)
    if not m:
        return False
    before = t[: m.start()]
    if _CODE_QUEUE_NEGATION_RE.search(before):
        return False
    # Both conditions, not either: a bug report legitimately NAMES a task, so the
    # referent alone must not disqualify (lens-6 HIGH). Only the subordinated shape
    # ("... a task TO queue a code session") is about the other object.
    if (_CODE_QUEUE_SUBORDINATED_RE.search(before)
            and _CODE_QUEUE_OTHER_REFERENT_RE.search(before)):
        return False
    return True


def _asana_destructive_intent(text: str) -> str | None:
    """Return the Asana WRITE tool to force (delete/complete/create/update/comment/
    subtask) for a clear imperative task action, else None. F-23 Slice 2 + PM-hub
    Phase 1. Conservative: interrogatives excluded, an explicit task referent required
    (except subtask, which is self-referential + verb-anchored), and destructive/create
    verbs must GOVERN the task (review MED #7). A miss is safe -- the confirm interceptor
    + phantom guard still prevent a fabricated success on the follow-up confirm turn."""
    t = (text or "").strip()
    if not t or _ASANA_INTENT_INTERROGATIVE_RE.search(t):
        return None
    # cq-a1306f3835f8: an explicit code-queue capture phrase OUTRANKS every task-op
    # branch below. The free text of a bug report legitimately contains task verbs
    # and a "task" referent; without this the report itself gets executed as a task
    # op. Checked here (not only at the call site) so no future caller of this
    # detector can reintroduce the hijack.
    if _code_queue_capture_intent(t):
        return None
    # Subtask first: "subtask" contains no standalone "task" boundary, so the generic
    # task-ref gate would drop it; the verb-anchored regex is its own referent.
    if _ASANA_SUBTASK_INTENT_RE.search(t):
        return "asana_add_subtask"
    if not _ASANA_TASK_REF_RE.search(t):
        return None  # remaining branches require an explicit task referent
    if _ASANA_DELETE_INTENT_RE.search(t):
        return "asana_delete_task"
    if _ASANA_COMPLETE_INTENT_RE.search(t):
        return "asana_complete_task"
    if _ASANA_CREATE_INTENT_RE.search(t):
        return "asana_create_task"
    if _ASANA_COMMENT_INTENT_RE.search(t):
        return "asana_add_comment"
    if _ASANA_UPDATE_INTENT_RE.search(t):
        return "asana_update_task"
    return None


# ── Remember/forget-note preview intent detector (S4, live-smoke 2026-08-02) ─
# A clear "remember X" / "forget that note" command must not run on Haiku,
# which live-fabricated preview-shaped TEXT with ZERO tool_use (no stash
# minted -- a buttonless, fake-looking confirm card exposed it, since
# remember/forget_note have no deterministic confirm-interceptor fallback the
# way Asana/Shopify do -- see tool_dispatch.try_confirm_pending_write's
# docstring). Unlike _asana_destructive_intent, this does NOT force the tool
# via tool_choice: cora_forget_note requires a note_id the model can only have
# after a prior cora_my_notes call, so forcing it blind could produce a
# nonsensical tool call. Instead this feeds the SAME sonnet-escalation
# OR-chain _asana_destructive_intent already forces Sonnet through below --
# a lower-risk intervention (Sonnet is more reliable at everything, not just
# one tool, so an over-broad match just means an unnecessary but harmless
# model upgrade, never a forced wrong tool call). Anchored to the START of
# the message (after the bot-mention strip) and excludes any "?" so a genuine
# question ("do you remember...") or a reminiscing aside never matches -- a
# miss just leaves the turn on today's model_router pick, same fallback
# safety as the Asana detector.
_REMEMBER_INTENT_RE = re.compile(
    r"^\s*(?:please\s+)?remember\b"
    r"|^\s*(?:please\s+)?note\s+that\b"
    r"|^\s*make\s+a\s+note\b",
    re.IGNORECASE,
)
_FORGET_NOTE_INTENT_RE = re.compile(
    r"^\s*(?:please\s+)?forget\b[^.\n]{0,40}\bnote\b"
    r"|^\s*(?:please\s+)?(?:delete|remove)\b[^.\n]{0,40}\bnote\b",
    re.IGNORECASE,
)


def _remember_or_forget_intent(text: str) -> bool:
    """True for a clear imperative 'remember'/'forget note' command -- see
    the module comment above for why this only escalates the MODEL, never
    forces a specific tool."""
    t = (text or "").strip()
    if not t or "?" in t:
        return False
    return bool(_REMEMBER_INTENT_RE.search(t) or _FORGET_NOTE_INTENT_RE.search(t))


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

    # ── Per-user historical email/Drive access gate (pre-LLM, D-034) ────────
    # Deterministic, runs before the semantic cache and before any Claude
    # call. Finance gate first (it only acts inside #founder-finance), then the
    # personal Tier-2 gate. "respond" decisions are COMPLETE replies (DM
    # redirect / refusal); "grant" switches the pipeline to owner-authorized
    # retrieval below. See historical_access.py / finance_receipts.py.
    is_dm = str(channel_id).startswith("D")
    access_decision = finance_receipts.check_request(
        user_id or "", channel_id, user_message,
    )
    if access_decision.action == "pass":
        access_decision = historical_access.check_tier2(
            user_id or "", is_dm, user_message,
        )
    if access_decision.action == "respond":
        log.info(
            "historical_access: deterministic response channel=#%s user=%s",
            channel_name, user_id,
        )
        say(text=access_decision.message, thread_ts=reply_thread_ts,
            unfurl_links=False, unfurl_media=False)
        return
    retrieval_grant = access_decision if access_decision.action == "grant" else None
    asker_emails = historical_access.owned_emails(user_id or "")
    asker_unrestricted = historical_access.is_unrestricted(user_id or "")

    function = channel_classifier.classify_function(channel_name)
    tier = channel_classifier.tier_label(entity, function)

    # Resolve who is asking — ALWAYS inject caller identity so Claude never
    # confuses one team member for another (e.g. Hannah for Harrison).
    caller_name = user_identity.display_name(user_id or "") if user_id else "Unknown"
    caller_record = user_identity.get_user(user_id or "") if user_id else None
    caller_role_hint = ""
    if caller_record and caller_record.asana_email:
        caller_role_hint = f" ({caller_record.asana_email})"

    # Role-aware context (org_roles, Phase 1 of Org Synthesis): a terse block
    # describing the asker's role/entity/lanes so answers are tailored to their
    # position. ADVISORY ONLY -- unknown users get "" (fail-closed to neutral)
    # and the block itself states it never expands entity access. All hard
    # guards (user_access / sibling / cross_entity / phi / historical_access)
    # run regardless.
    caller_role_block = org_roles.format_role_context(user_id or "")

    # Founder (Harrison) gets cross-entity access from any channel. His questions
    # about UFL, LEX, OSN etc. from an F3E channel should not be blocked by entity scope.
    is_founder = (user_id == _FOUNDER_ID)
    founder_note = (
        "\n**Cross-entity access ENABLED:** This user is the portfolio founder. "
        "Answer questions about any HJR Global entity regardless of this channel's "
        "entity scope. Do not redirect to other channels based on entity scoping.\n"
    ) if is_founder else ""

    # cq-c6392ebbaa45: anchor the model's "now" — with no date line the model
    # free-handed day arithmetic ("44 days overdue" for a 29-day gap) and even
    # resolved relative phrases against a stale internal date (the S4 create-path
    # incident). One factual line; rides the uncached runtime block.
    az_today = datetime.now(timezone(timedelta(hours=-7))).strftime("%Y-%m-%d")
    runtime_context = (
        f"## Runtime channel context\n\n"
        f"Today's date: {az_today} (America/Phoenix).\n"
        f"This channel (#{channel_name}) has these properties:\n"
        f"- Entity: {entity}\n"
        f"- Function: {function}\n"
        f"- Financial-access tier: {tier}\n\n"
        f"**The person asking this question is: {caller_name}{caller_role_hint}** "
        f"(Slack ID: {user_id or 'unknown'}).\n"
        f"Address them by their first name if relevant. Do NOT assume the asker is "
        f"Harrison Rogers unless their Slack ID is U0B2RM2JYJ1.\n"
        + (f"\n{caller_role_block}\n" if caller_role_block else "")
        + f"{founder_note}\n"
        f"Apply the cross-entity and financial guardrails accordingly.\n\n"
        f"{historical_access.TIER1_SYNTHESIS_RULE}\n\n"
        f"---\n\n"
    )

    # ── Outbound channel-scope content guard (F-08 family) ──────────────────
    # Twin of the retrieval-side PHI scrub: evaluate the COMPOSED answer against
    # THIS channel and refuse a confidential content class the channel doesn't
    # permit (personal insurance / capital program / travel points / cross-entity
    # CRM / company financials outside TIER_1). Keyed on CHANNEL, not asker, so it
    # fires even for the founder. Applied at EVERY post site (cache-hit,
    # non-streaming, streaming final, and each streaming frame) because the
    # semantic cache is entity-keyed, not tier-keyed -- a TIER_1-generated answer
    # can be cache-served into a TIER_3 channel of the same entity. The original
    # (unguarded) answer is what gets CACHED; guarding happens at serve time.
    # Skipped on the Tier-2 owner-mail GRANT path (1:1 DM, owner-scoped, already
    # access-controlled + PHI-dropped).
    def _guard_content(txt: str) -> str:
        if retrieval_grant is not None or not txt:
            return txt
        guarded, tripped = channel_content_guard.guard_outbound(
            txt, entity=entity, tier=tier, channel_name=channel_name,
            user_id=user_id or "", is_dm=is_dm,
        )
        return guarded

    # ── Confirm-button turn snapshot (S1/S2, design 2026-08-02) ─────────────
    # Captured BEFORE the confirm interceptor (which can itself mint a fresh
    # re-preview, e.g. Shopify drift) and before the model's tool loop.
    # Compared against a second snapshot at each reply site via
    # _confirm_card_for_reply() to detect "a fresh preview (or ambiguity ask)
    # was minted THIS turn" -- the trigger for attaching a Confirm/Cancel (or
    # picker) button card. Marker-free by design: works whether a kind's
    # preview text is code-enforced verbatim or model-mediated.
    # begin_turn() (v1.1 S1 fix, live-smoke 2026-08-02) tags every stash this
    # turn's own tool call mints with a fresh turn id, BEFORE anything in this
    # turn could mint one -- freshest_changed_stash() uses it to bind a reply's
    # card to a stash THIS turn minted, never a concurrent turn's, even when
    # their snapshot/diff windows overlap (same (user, channel), overlapping
    # @mentions).
    confirm_cards.begin_turn()
    _confirm_before_snapshot = (
        _tool_dispatch.snapshot_stash_ids(user_id, channel_name) if user_id else {}
    )

    def _confirm_card_for_reply(text: str) -> list[dict] | None:
        if not user_id or not text or not confirm_cards.confirm_buttons_enabled():
            return None
        changed = _tool_dispatch.freshest_changed_stash(
            _confirm_before_snapshot, user_id, channel_name)
        if changed is None:
            return None
        kind, cid = changed
        if kind == "ask":
            ask_entry = _tool_dispatch.get_pending_ask(user_id, channel_name)
            if not ask_entry or ask_entry.get("ask_id") != cid:
                return None
            candidates = [(key, label) for key, label, _value in ask_entry.get("candidates", [])]
            if not candidates:
                return None
            return confirm_cards.build_picker_blocks(text, cid, candidates)
        return confirm_cards.build_confirm_blocks(text, cid)

    # ── Deterministic staged-write confirm interceptor (F-23, 2026-07-12) ──────
    # A fresh pending Asana/Shopify write for this (user, channel) + a clear bare
    # affirmative executes the write IN CODE via the tool's own confirm executor
    # and posts the tool's own outcome text -- the model is never consulted, so a
    # haiku that skips the tool (fabricating a phantom "deleted" success) can no
    # longer lose the write. A clear negative cancels. Anything else returns None
    # and falls through to the model with the pending intact (the Sonnet
    # write-escalation below still covers the ambiguous case). Runs downstream of
    # the DM gap-ask + OSN-scheduler routing in handle_message_event, so a "yes"
    # meant for those never reaches here.
    # Typed-SEND fallback for revops Tier-1 cards (design 2026-08-01): an exact
    # `SEND <stash_id>` from the approver routes to the SAME gate as the ✅ tap
    # (kill switch, stash claim, guard re-run, recipient subset all inside).
    # Deliberately narrow: uppercase SEND + a 16-hex stash id, nothing else --
    # ordinary sentences can never match, and non-approvers get the gate refusal.
    # DM-ONLY: the receipt names counterparty addresses, which must never land
    # in a shared channel just because the approver typed there (D-051 lens 7).
    if user_id and channel_name == "dm":
        m_send = re.match(r"^\s*SEND\s+([0-9a-f]{16})\s*$", user_message or "")
        if m_send:
            outcome, send_msg = revops_cards.process_send_action(
                m_send.group(1), user_id, action="send"
            )
            log.info("revops typed-SEND outcome=%s user=%s", outcome, user_id)
            say(text=_guard_content(send_msg), thread_ts=reply_thread_ts,
                unfurl_links=False, unfurl_media=False)
            active_thread_store.register(channel_id, register_ts)
            return

    if user_id:
        confirm_reply = _tool_dispatch.try_confirm_pending_write(
            slack_user_id=user_id, channel_name=channel_name, entity=entity,
            message=user_message,
        )
        if confirm_reply is not None:
            log.info(
                "confirm_interceptor served channel=#%s user=%s", channel_name, user_id,
            )
            guarded_reply = _guard_content(confirm_reply)
            say(text=guarded_reply, blocks=_confirm_card_for_reply(guarded_reply),
                thread_ts=reply_thread_ts, unfurl_links=False, unfurl_media=False)
            active_thread_store.register(channel_id, register_ts)
            return

    t0 = time.monotonic()

    # ── Intent classification + semantic cache ─────────────────────────────
    intent = ic.classify(user_message, entity)
    hints  = ic.routing_hints(intent)

    log.info(
        "intent_classify channel=#%s user=%s intent=%s skip_kb=%s bypass_cache=%s",
        channel_name, user_id, intent, hints.skip_kb, hints.bypass_cache,
    )

    question_embedding: list[float] | None = None
    # Explicit live-web intent must never be served a (≤30-min) stale KB-only
    # cached answer -- bypass the cache read for it (pure-regex, no KB/ledger
    # dependency). The time-sensitive fallback still uses the cache: it only
    # attaches on a KB miss, so a cache hit means the KB DID have the answer.
    web_intent = web_guard.is_web_intent(user_message)
    # Grant-path responses contain owner-private mail/file content — they must
    # never be served from (or stored into) the shared semantic cache, where a
    # different user's similar question would replay them.
    if not hints.bypass_cache and retrieval_grant is None and not web_intent:
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
                    text=_guard_content(cached_response),
                    thread_ts=reply_thread_ts,
                    unfurl_links=False,
                    unfurl_media=False,
                )
                active_thread_store.register(channel_id, register_ts)
                return
        except Exception as exc:
            log.warning("semantic_cache lookup error for entity=%s: %s", entity, exc)

    # ── Context + prompt loading ───────────────────────────────────────────
    # Pass question_embedding (already computed for semantic cache) so
    # context_loader → store.search() can skip its own embed_query() call.
    # If bypass_cache=True the embedding was never computed; passing None
    # is safe -- store.search() falls back to computing it internally.
    # Split static portfolio context (cacheable) from per-query KB chunks
    # (volatile). static_text becomes a cached system block; kb_text rides in the
    # uncached block alongside runtime_context. See claude_client._build_cached_system.
    kb_meta: dict = {}
    # PHI cache-leak guard: a custodian's LEX answer is NOT PHI-scrubbed (they are
    # authorized for full PHI), so it must NEVER enter the shared, user-agnostic
    # semantic cache -- a cache hit replays the stored text to whoever asks a
    # similar question next, bypassing the retrieval-path scrub entirely. Defaulted
    # here so it is in scope for cache_storable regardless of which branch runs.
    phi_custodian = False
    if retrieval_grant is not None:
        # Tier-2 grant: owner-authorized retrieval REPLACES normal KB
        # retrieval, and the static portfolio context is withheld — explicit
        # mailbox retrieval doesn't need it, and a DM asker may not be
        # entity-authorized for the founder brief it contains.
        # W2-04: this grant is owner-scoped + PHI-dropped (see _build_grant_context
        # and historical_access.py L31-34); the DM-retrieval entry above dispatches
        # here without re-running the guard trio, which is safe by that scoping.
        static_text = ""
        kb_text = _build_grant_context(
            retrieval_grant, user_message, user_id or "", channel_name,
            question_embedding,
        )
    else:
        # PHI scrub gate (F-2 / 2.3): custodians in LEX scope get full PHI; every
        # other asker has retrieved LEX chunk text PHI-scrubbed in context_loader.
        # Fail-closed (non-custodian -> False -> scrub) via lex_phi_access.
        phi_custodian = (
            lex_phi_access.phi_allowed(user_id, entity, is_dm=is_dm) if user_id else False
        )
        # Web-clean load (cq-49a7835f081c): on an explicit-web-intent turn that
        # WILL actually attach web tools, build the context WITHOUT unstripped
        # personal content (no note overlay, Tier-1 stripped posture, no
        # asker-scoped cross-entity fallback) so the D-051 personal-context
        # exclusion in the gate below is satisfied by construction instead of
        # silently swallowing the web ask. The pre-flight evaluate() is
        # kb_meta-independent on the explicit leg (web_intent=True short-circuits
        # the fallback leg), so a disabled/LEX/screened/capped/unsupported-model
        # turn — and every custodian turn — keeps its FULL context and simply
        # never attaches (D-051 review: degrading context on a turn that cannot
        # attach is pure loss). Residual: a daily-cap race between this
        # pre-flight and the authoritative post-load evaluate can yield one
        # degraded KB-only reply — accepted (ms window, fail-safe direction).
        web_clean = (
            web_intent
            and not phi_custodian
            and not web_guard.is_lex_scope(entity)
            and web_guard.evaluate(
                user_message, entity, kb_meta=None,
                skip_kb=hints.skip_kb, model=model_router.MODEL_SONNET,
            ).attach
        )
        static_text, kb_text = load_context_parts(
            entity,
            query=user_message,
            skip_kb=hints.skip_kb,
            kb_k=hints.kb_k_override,
            query_vec=question_embedding,
            asker_emails=asker_emails,
            asker_unrestricted=asker_unrestricted,
            kb_meta=kb_meta,
            # Personal-note overlay (Phase 5): owner-filtered at the SQL layer;
            # any response using a note sets kb_meta["unstripped_personal"] so
            # the cache_storable check below keeps it out of the shared cache.
            asker_slack_id=user_id or "",
            asker_is_dm=is_dm,
            phi_custodian=phi_custodian,
            web_clean=web_clean,
        )
    # A response built on UNSTRIPPED personal chunks (owner's own mail, or an
    # unrestricted asker) must not enter the shared semantic cache. Nor may a
    # custodian's un-scrubbed LEX answer (PHI cache-leak guard above).
    cache_storable = (
        retrieval_grant is None
        and not kb_meta.get("unstripped_personal")
        and not phi_custodian
    )
    prompt = load_prompt(entity)
    chosen_model = model_router.choose_model(user_message)
    # F-23 Slice 2: a clear delete/complete/create-task request forces that tool (via
    # tool_choice) on the first model turn, so a TOOL preview + server-side pending
    # entry is produced instead of a haiku-fabricated one (the delete-intent turn ran
    # on haiku live and fabricated the preview -- no tool_use, no pending).
    # Precedence (cq-a1306f3835f8): an explicit "queue a code session" command wins
    # over the Asana task-op force, whose regexes legitimately match the bug report's
    # own free text. Forcing the capture tool is safe where forcing cora_forget_note
    # would not be -- cora_queue_code_session needs only a `request` string the model
    # always has, it is in _GLOBAL_CORE_TOOLS (exposed in every entity + DMs, so
    # tool_choice can never name an unexposed tool), and its first call files NOTHING:
    # it returns a preview and stashes server-side, so even a false positive costs one
    # dismissable Harrison-gated card, never a data write.
    force_tool = None
    if user_id:
        if _code_queue_capture_intent(user_message):
            force_tool = "cora_queue_code_session"
            log.info("code_queue capture intent -> forcing tool channel=#%s user=%s",
                     channel_name, user_id)
        else:
            force_tool = _asana_destructive_intent(user_message)
    # F-23 Slice 3: a bare affirmative broadens the phantom-write guard so a fabricated
    # "Confirmed -- task deleted" (with no write sentinel) is corrected. Gated on NO
    # pending write existing (review HIGH #3/#4, MED #5): if a pending exists and a bare
    # affirmative still reached the model, it is a CALENDAR confirm (Asana/Shopify would
    # have fired the interceptor) -- a real calendar write is coming, so broadening would
    # clobber its legitimate success narration. With no pending, a bare affirmative that
    # reached the model has nothing legitimate to confirm -> broaden is safe. (claude_client
    # further gates broaden on "no tool ran this turn".)
    assume_confirm = (
        bool(user_id)
        and _tool_dispatch.is_bare_affirmative(user_message)
        and not (
            _tool_dispatch.has_pending_asana_write(user_id, channel_name)
            or _tool_dispatch.has_pending_shopify_write(user_id, channel_name)
            or _tool_dispatch.has_pending_calendar_write(user_id, channel_name)
            or _tool_dispatch.has_pending_delegated_write(user_id, channel_name)
        )
    )
    # Staged-WRITE escalation (2026-07-10 hotfix): a pending DTC inventory/calendar/
    # Asana confirm for this (user, channel) means the next turn is very likely the
    # "yes" -- undetectable from message content -- so force Sonnet. A write flow is
    # not a Haiku job (both live confirm turns ran on Haiku). Also force Sonnet when a
    # destructive/create tool is being forced (Slice 2) -- forced tool use + a write
    # is a Sonnet job.
    # S4 (live-smoke 2026-08-02): a clear "remember"/"forget note" PREVIEW-turn
    # command also forces Sonnet -- Haiku live-fabricated preview-shaped text
    # with zero tool_use on this exact phrasing (no stash minted, buttonless
    # card). has_pending_remember/has_pending_forget_note close the matching
    # CONFIRM-turn gap: remember/forget have no deterministic confirm
    # interceptor (unlike Asana/Shopify), so their bare-"yes" follow-up
    # previously could not join this escalation at all.
    remember_intent = _remember_or_forget_intent(user_message) if user_id else False
    if force_tool or remember_intent or (user_id and (
        _tool_dispatch.has_pending_shopify_write(user_id, channel_name)
        or _tool_dispatch.has_pending_calendar_write(user_id, channel_name)
        or _tool_dispatch.has_pending_asana_write(user_id, channel_name)
        or _tool_dispatch.has_pending_delegated_write(user_id, channel_name)
        or _tool_dispatch.has_pending_remember(user_id, channel_name)
        or _tool_dispatch.has_pending_forget_note(user_id, channel_name)
        # D-051 lens-1 MEDIUM (2026-08-06): the code-queue capture confirm was the
        # one staged-write confirm turn running unescalated. It DEFERS to the model
        # by design, and the S4 precedent is that Haiku fabricates preview-shaped
        # text with zero tool_use on exactly this shape. Slice 2's forced capture
        # makes this turn common, so it joins the chain.
        or _tool_dispatch.has_pending_code_queue(user_id, channel_name)
    )):
        chosen_model = model_router.MODEL_SONNET
    log.info(
        "model_routing channel=#%s user=%s model=%s msg_chars=%d",
        channel_name, user_id, model_router.short_label(chosen_model), len(user_message),
    )

    # A live-web-intent query is never cache-STORED either (the read was already
    # bypassed above): a KB-only degraded answer to a web ask must not shadow the
    # web path for the next asker.
    if web_intent:
        cache_storable = False
    # ── Web tools gate (2026-07-31): the server-side web_search/web_fetch tools
    # attach only when web_guard says so — explicit web intent, or a time-sensitive
    # question whose KB retrieval missed; NEVER in LEX scope; the user query is
    # egress-screened fail-closed; a daily search cap bounds spend. A block is a
    # soft degradation (KB-only behavior), never a user-facing refusal.
    #
    # DETERMINISTIC EXCLUSIONS (never carry web tools):
    #  - forced-tool / bare-affirmative confirm / Tier-2 retrieval-grant turns;
    #  - phi_custodian or unstripped_personal context: unscrubbed LEX/personal
    #    content is already in this turn's context (custodian DM, cross-entity
    #    fallback, personal-notes overlay), and could ride into a model-composed
    #    search query — mirror the cache_storable exclusion (D-051 remediation).
    #    The web-clean load above makes unstripped_personal unreachable on an
    #    explicit-intent turn (cq-49a7835f081c); the check stays as a fail-closed
    #    belt for the time-sensitive fallback path and any future setter.
    web_on = False
    web_gate_skip: str | None = None
    if force_tool is not None:
        web_gate_skip = "forced_tool"
    elif assume_confirm:
        web_gate_skip = "assume_confirm"
    elif retrieval_grant is not None:
        web_gate_skip = "retrieval_grant"
    elif phi_custodian:
        web_gate_skip = "phi_custodian"
    elif kb_meta.get("unstripped_personal"):
        web_gate_skip = "unstripped_personal"
    # evaluate() runs even on excluded turns (it is read-only and fail-closed):
    # a withheld web ask must ALWAYS leave ledger/log evidence — the original
    # cq-49a7835f081c failure was three explicit web asks degrading silently.
    # Web attach forces Sonnet, so screen the model that will actually run
    # (soft-degrades to KB-only if it can't accept the 20260209 tool types).
    web_decision = web_guard.evaluate(
        user_message, entity, kb_meta=kb_meta,
        skip_kb=hints.skip_kb, model=model_router.MODEL_SONNET,
    )
    if web_gate_skip is None:
        web_on = web_decision.attach
        web_guard.record_decision(
            web_decision, entity=entity, channel_name=channel_name, user_id=user_id or "",
        )
        if web_on:
            # Multi-source live synthesis is not a Haiku job, and the model must
            # support the 20260209 tool revisions — force Sonnet.
            chosen_model = model_router.MODEL_SONNET
            runtime_context = runtime_context + "\n\n" + web_guard.WEB_MODE_CONTEXT
            # Live-web answers are time-anchored — never enter the shared cache.
            cache_storable = False
            log.info(
                "web_tools ATTACHED channel=#%s user=%s model=%s reason=%s",
                channel_name, user_id, model_router.short_label(chosen_model),
                web_decision.reason,
            )
        elif web_decision.reason not in ("no_intent", "disabled"):
            log.info(
                "web_tools withheld channel=#%s user=%s reason=%s",
                channel_name, user_id, web_decision.reason,
            )
    elif web_decision.attach or web_decision.reason not in ("no_intent", "disabled"):
        # The gate never ran, but web_guard would have acted on this turn
        # (attach, or a live block reason). Record the skip so a deterministic
        # exclusion can never again swallow a web ask invisibly.
        web_guard.record_decision(
            web_guard.WebDecision(False, f"gate_skipped:{web_gate_skip}"),
            entity=entity, channel_name=channel_name, user_id=user_id or "",
        )
        log.info(
            "web_tools gate_skipped channel=#%s user=%s skip=%s would=%s",
            channel_name, user_id, web_gate_skip, web_decision.reason,
        )

    # ── Streaming: post placeholder, then update it as Claude streams ──────
    # A web turn spends its first seconds in the server-side search/fetch phase
    # emitting no text deltas, so a web-specific placeholder tells the user what
    # the pause is (the throttled stream updates take over once text arrives).
    placeholder_text = ":mag: searching the web…" if web_on else ":thought_balloon: thinking…"
    placeholder_ts: str | None = None
    placeholder_channel: str = channel_id
    try:
        placeholder_resp = say(
            text=placeholder_text,
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

    # D-032 reply-formatter signal: generate_response* sets gen_meta["used_tools"]
    # so tool-bearing replies bypass format_reply (tool outputs present as-is).
    gen_meta: dict = {}

    if placeholder_ts is None:
        # ── Fallback: non-streaming path ──
        try:
            response_text = generate_response(
                prompt,
                runtime_context + kb_text,
                user_message,
                slack_user_id=user_id or "",
                entity=entity,
                model=chosen_model,
                prior_messages=prior_messages,
                channel_name=channel_name,
                cached_context=static_text,
                cross_entity_tools=is_founder,
                meta=gen_meta,
                force_tool=force_tool,
                assume_confirm=assume_confirm,
                web_tools=web_on,
                channel_id=channel_id,
                thread_ts=reply_thread_ts,
            )
        except ClaudeClientError as exc:
            log.error("ClaudeClientError for entity=%s user=%s: %s", entity, user_id, exc)
            # Ledger any searches billed before the failure (retries re-bill), so
            # the daily cap does not undercount on error days (D-051 remediation).
            if web_on:
                web_guard.record_usage(
                    gen_meta.get("web_search_requests", 0),
                    gen_meta.get("web_fetch_requests", 0),
                    entity=entity, channel_name=channel_name,
                )
            say(text=user_facing_message(exc), thread_ts=reply_thread_ts)
            return

        latency_ms = int((time.monotonic() - t0) * 1000)
        if not web_on:
            # Skip gap logging on web turns: a web answer is not a KB gap, and an
            # UNKNOWN sentinel echoed from attacker-controlled web text must never
            # feed gap_autofill -> known-answers canon (D-051 remediation).
            response_text = _extract_and_log_gap(
                response_text, entity, channel_name, user_id, user_message, latency_ms,
                kb_meta=kb_meta, gen_meta=gen_meta, is_dm=is_dm,
                # No thread root (e.g. /cora-ask) -> no thread key; "C123:None"
                # would collapse every slash-command ask in a channel into one
                # 48h dedup bucket (adversarial review LOW).
                thread_key=f"{channel_id}:{register_ts}" if register_ts else "",
                thread_context=bool(prior_messages),
            )
        # Code-session queue S2/S4 (fail-soft, off-thread, reply-inert): a build-signal
        # phrase in the ask or a capability deflection in the reply becomes a candidate.
        code_queue.capture_message_signal(
            user_message, entity, channel_id, channel_name, user_id or "",
            response_text=response_text,
        )
        if web_on:
            # Daily-cap accounting + deterministic provenance: the Sources line is
            # composed from the API's own citations as sanctioned <url|label> tokens,
            # which format_reply Pass 1 and the egress boundary preserve end-to-end.
            web_guard.record_usage(
                gen_meta.get("web_search_requests", 0),
                gen_meta.get("web_fetch_requests", 0),
                entity=entity, channel_name=channel_name,
            )
            sources_line = web_guard.format_sources_line(gen_meta.get("web_citations"))
            if sources_line:
                response_text = response_text.rstrip() + "\n\n" + sources_line
        # D-032 / Phase 2.1: conversational replies pass through the deterministic
        # voice formatter; only genuine verbatim-table tools bypass it. The old
        # bool(used_tools) heuristic bypassed EVERY tool-using reply (so a prose
        # answer that merely looked something up went out unsanitized) -- now
        # gated on used_verbatim_tool (set by claude_client from VERBATIM_TABLE_TOOLS).
        # Applied before the cache store so cached replays are already-formatted.
        is_structured_table = bool(gen_meta.get("used_verbatim_tool"))
        response_text = format_reply(response_text, is_tool_output=is_structured_table)
        response_text = _fix_lex_channel_names(response_text)
        response_text = _validate_channel_links(response_text, client)
        # Verbatim tables are time-sensitive (financial figures), so never cache them.
        # Cache the ORIGINAL (entity-keyed, channel-agnostic); guard at serve time.
        if cache_storable and not is_structured_table:
            _try_cache_store(entity, user_message, question_embedding, response_text, hints)
        response_text = _guard_content(response_text)
        log.info(
            "responded (non-streaming) entity=%s channel=#%s user=%s latency_ms=%d response_chars=%d",
            entity, channel_name, user_id, latency_ms, len(response_text),
        )
        say(
            text=response_text,
            blocks=_confirm_card_for_reply(response_text),
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
                # Guard every mid-stream frame so a confidential class streaming in
                # is masked the instant it appears (not just on the final update).
                text=_guard_content(cumulative_text),
            )
        except Exception as upd_exc:  # noqa: BLE001
            log.warning(
                "chat_update mid-stream failed for ts=%s: %s — stream continues",
                placeholder_ts, upd_exc,
            )

    try:
        response_text = generate_response_streaming(
            prompt,
            runtime_context + kb_text,
            user_message,
            update_callback=update_callback,
            slack_user_id=user_id or "",
            entity=entity,
            model=chosen_model,
            prior_messages=prior_messages,
            channel_name=channel_name,
            cached_context=static_text,
            cross_entity_tools=is_founder,
            meta=gen_meta,
            force_tool=force_tool,
            assume_confirm=assume_confirm,
            web_tools=web_on,
            channel_id=channel_id,
            thread_ts=reply_thread_ts,
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
        # Ledger any searches billed before the streaming failure (D-051 remediation).
        if web_on:
            web_guard.record_usage(
                gen_meta.get("web_search_requests", 0),
                gen_meta.get("web_fetch_requests", 0),
                entity=entity, channel_name=channel_name,
            )
        throttle.release_stream(stream_id)
        return

    latency_ms = int((time.monotonic() - t0) * 1000)
    if not web_on:
        # Skip gap logging on web turns (see non-streaming path): a web answer is
        # not a KB gap, and an echoed UNKNOWN sentinel must not feed canon.
        response_text = _extract_and_log_gap(
            response_text, entity, channel_name, user_id, user_message, latency_ms,
            kb_meta=kb_meta, gen_meta=gen_meta, is_dm=is_dm,
            thread_key=f"{channel_id}:{register_ts}" if register_ts else "",
            thread_context=bool(prior_messages),
        )
    # Code-session queue S2/S4 (fail-soft, off-thread, reply-inert): a build-signal
    # phrase in the ask or a capability deflection in the reply becomes a candidate.
    code_queue.capture_message_signal(
        user_message, entity, channel_id, channel_name, user_id or "",
        response_text=response_text,
    )
    if web_on:
        # Daily-cap accounting + deterministic provenance (see non-streaming path).
        web_guard.record_usage(
            gen_meta.get("web_search_requests", 0),
            gen_meta.get("web_fetch_requests", 0),
            entity=entity, channel_name=channel_name,
        )
        sources_line = web_guard.format_sources_line(gen_meta.get("web_citations"))
        if sources_line:
            response_text = response_text.rstrip() + "\n\n" + sources_line
    # D-032 / Phase 2.1: conversational replies pass through the deterministic
    # voice formatter; only genuine verbatim-table tools bypass it (used_verbatim_tool,
    # not the old too-broad bool(used_tools)). Applied before the cache store so
    # cached replays are already-formatted.
    is_structured_table = bool(gen_meta.get("used_verbatim_tool"))
    response_text = format_reply(response_text, is_tool_output=is_structured_table)
    response_text = _fix_lex_channel_names(response_text)
    response_text = _validate_channel_links(response_text, client)
    # Verbatim tables are never cached (time-sensitive financial figures).
    # Cache the ORIGINAL (entity-keyed, channel-agnostic); guard at serve time.
    if cache_storable and not is_structured_table:
        _try_cache_store(entity, user_message, question_embedding, response_text, hints)
    response_text = _guard_content(response_text)

    skipped = throttle.release_stream(stream_id).get("skipped_count", 0)
    log.info(
        "responded (streaming) entity=%s channel=#%s user=%s latency_ms=%d response_chars=%d updates_skipped=%d",
        entity, channel_name, user_id, latency_ms, len(response_text), skipped,
    )

    throttle.force_acquire(stream_id + "-final")
    confirm_blocks = _confirm_card_for_reply(response_text)
    try:
        client.chat_update(
            channel=placeholder_channel,
            ts=placeholder_ts,
            text=response_text,
            blocks=confirm_blocks,
        )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "Final chat_update failed for ts=%s: %s — sending fresh reply as fallback",
            placeholder_ts, exc,
        )
        say(
            text=response_text,
            blocks=confirm_blocks,
            thread_ts=reply_thread_ts,
            unfurl_links=False,
            unfurl_media=False,
        )

    # Register AFTER the response is confirmed posted so only successful
    # interactions activate the thread follow-up window.
    active_thread_store.register(channel_id, register_ts)


@app.command("/cora-ask")
def handle_cora_ask(ack, body, client) -> None:
    """Handle /cora-ask slash command -- answer a question in-channel without @-mention.

    Usage: /cora-ask [your question]

    Note: Register /cora-ask in the Slack app manifest under Slash Commands.
    For Socket Mode apps, no URL is needed -- just enable the command in the app config.
    """
    ack()  # Must ack within 3 seconds per Slack requirements

    channel_id = body.get("channel_id", "")
    user_id    = body.get("user_id", "")
    text       = (body.get("text") or "").strip()

    if not text:
        client.chat_postEphemeral(
            channel=channel_id,
            user=user_id,
            text=":information_source: Usage: `/cora-ask [your question]`",
        )
        return

    # Hard block: never respond in permanently blocked channels
    if _is_blocked_channel(channel_id):
        log.warning("handle_cora_ask: blocked channel %s -- ignoring", channel_id)
        return

    # Rate limiting (reuse same limiter as @-mentions)
    allowed, cap_type = rate_limiter.check(user_id, channel_id)
    if not allowed:
        client.chat_postEphemeral(
            channel=channel_id,
            user=user_id,
            text="You've hit the rate limit. Try again in a moment.",
        )
        return

    channel_name = _resolve_channel_name(client, channel_id)

    # Lazy-resolve bot user ID (needed for thread history role assignment)
    _resolve_bot_user_id(client)

    from .entity_router import is_silent_channel
    if is_silent_channel(channel_name):
        log.info("silent channel #%s -- ignoring /cora-ask from %s", channel_name, user_id)
        return

    entity = route(channel_name)

    log.info(
        "cora_ask slash channel=#%s user=%s entity=%s question=%.80s",
        channel_name, user_id, entity, text,
    )

    # Access guards -- parity with handle_mention (pre-LLM, fail-closed). /cora-ask
    # previously dispatched with NO guards, leaving the best-effort content scrub as
    # the only PHI defense on this path. Refusals post ephemerally (asker-only).
    is_dm = str(channel_id).startswith("D")
    if user_id:
        phi_custodian = lex_phi_access.phi_allowed(user_id, entity, is_dm=is_dm)
        tier = channel_classifier.tier_label(
            entity, channel_classifier.classify_function(channel_name)
        )
        access_block = user_access.check_access(
            user_id, entity, text, phi_custodian=phi_custodian, tier=tier
        )
        if access_block:
            log.info(
                "cora_ask: user_access blocked user=%s entity=%s reason=%s",
                user_id, entity, access_block[:80],
            )
            client.chat_postEphemeral(channel=channel_id, user=user_id, text=access_block)
            return
    sibling_redirect = sibling_guard.check_redirect(entity, text)
    if sibling_redirect:
        client.chat_postEphemeral(channel=channel_id, user=user_id, text=sibling_redirect)
        return
    cross_redirect = cross_entity_guard.check_cross_entity(text, entity)
    if cross_redirect:
        client.chat_postEphemeral(channel=channel_id, user=user_id, text=cross_redirect)
        return

    # Build a say-equivalent that posts to the channel (not in a thread)
    def _say(**kwargs) -> dict:
        kwargs.pop("thread_ts", None)
        return client.chat_postMessage(channel=channel_id, **kwargs)

    _dispatch_qa(
        channel_id=channel_id,
        channel_name=channel_name,
        user_id=user_id,
        user_message=text,
        reply_thread_ts=None,
        entity=entity,
        client=client,
        say=_say,
        prior_messages=[],
        root_thread_ts=None,
    )


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

    # ── #info-for-cora intake (route 1 of 3) ──────────────────────────────────
    # This is the ONLY intake route proven to fire in this channel today: channel
    # `message` events do not reach the app (see info_intake's module docstring),
    # so the D1 handler below has never run. Placed BEFORE parse_note on purpose --
    # _handle_note's paraphrase/confirm loop needs a message event to capture the
    # user's "yes", so in THIS channel it dead-ends (6/06 and 6/30 paraphrases,
    # neither ever confirmed). parse_note is still used to unwrap an explicit
    # "note: <fact>" prefix so that phrasing keeps working.
    #
    # A QUESTION is not a contribution: every one of Hannah's 5/28-6/17 posts here
    # was a question, so questions fall through to the normal Q&A path unchanged
    # and only statements are queued.
    if channel_id == INFO_FOR_CORA_CHANNEL_ID:
        contribution = team_learning.parse_note(user_message) or user_message
        author_name = user_id or ""
        try:
            rec = org_roles.get_role(user_id or "")
            if rec and rec.name:
                author_name = rec.name
        except Exception as exc:  # noqa: BLE001 -- naming must not break intake
            log.warning("info-for-cora: org_roles lookup failed: %s", exc)
        result = info_intake.ingest(
            text=contribution, author_id=user_id or "", author_name=author_name,
            ts=event.get("ts", ""), route="mention",
            channel_id=channel_id, channel_name=channel_name,
        )
        if result.outcome != info_intake.NOT_A_CONTRIBUTION:
            if result.ack:
                say(text=result.ack, thread_ts=thread_ts)
            elif result.outcome == info_intake.ERROR:
                say(text="Sorry -- I couldn't log that just now. Nothing was saved; "
                         "please re-post it and I'll try again.",
                    thread_ts=thread_ts)
            log.info("info-for-cora: mention intake outcome=%s user=%s",
                     result.outcome, user_id)
            return

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

    # Channel financial tier (leadership/finance/founder/build => TIER_1). Used by
    # the user_access financials block (permitted in TIER_1) and the help block.
    function = channel_classifier.classify_function(channel_name)
    tier = channel_classifier.tier_label(entity, function)

    # ── User access check — entity + sensitive topic authorization ────────────
    if user_id:
        # LEX PHI custodian gate (fail-closed). Grants the `phi` topic ONLY to an
        # allowlisted custodian asking inside LEX scope (LEX/LEX-* channel, or DM).
        # Channel IDs starting with "D" are DMs. Never relaxes anything else; the
        # sibling + cross-entity guards below still run.
        phi_custodian = lex_phi_access.phi_allowed(
            user_id, entity, is_dm=str(channel_id).startswith("D")
        )
        access_block = user_access.check_access(
            user_id, entity, user_message, phi_custodian=phi_custodian, tier=tier
        )
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
        help_text = help_responder.build_message(entity, function, tier)
        say(text=help_text, thread_ts=thread_ts, unfurl_links=False, unfurl_media=False)
        return

    # Sibling-entity redirect interception (LEX sub-entity channels)
    sibling_redirect = sibling_guard.check_redirect(entity, user_message)
    if sibling_redirect:
        log.info("sibling-entity redirect fired channel=#%s entity=%s", channel_name, entity)
        say(text=sibling_redirect, thread_ts=thread_ts, unfurl_links=False, unfurl_media=False)
        return

    # Cross-entity redirect interception (deterministic, pre-LLM). Fires before
    # any tool/Claude call so cross-entity data can never be surfaced.
    cross_redirect = cross_entity_guard.check_cross_entity(user_message, entity)
    if cross_redirect:
        log.info("cross-entity redirect fired channel=#%s entity=%s", channel_name, entity)
        say(text=cross_redirect, thread_ts=thread_ts, unfurl_links=False, unfurl_media=False)
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
    *,
    kb_meta: dict | None = None,
    gen_meta: dict | None = None,
    is_dm: bool = False,
    thread_key: str = "",
    thread_context: bool = False,
) -> str:
    """Pull the [CORA_KNOWLEDGE_GAP: ...] sentinel out of the response (if
    present), log the gap, and return the cleaned response text.

    WS-1: when NO sentinel is present, the deterministic detectors in
    gap_detection run instead (kb_miss / unknown_response) -- the sentinel is
    behaviorally unreliable as the only intake (44 gaps ever), so detection is
    now code-level (the instrumentation twin of D-034). Deterministic guard
    refusals never reach this helper (every guard returns before _dispatch_qa
    calls the LLM), and gap_detection vetoes LLM-generated deflections, LEX
    entities, PHI, smalltalk, dedups 7d, and caps per day. Fail-soft: a
    detector error never affects the response.
    """
    match = _GAP_RE.search(response_text)
    if not match:
        gap_detection.maybe_log_gap(
            entity=entity,
            channel=channel_name,
            user=user_id,
            question=user_message,
            response_text=response_text,
            latency_ms=latency_ms,
            kb_meta=kb_meta,
            gen_meta=gen_meta,
            is_dm=is_dm,
            thread_key=thread_key,
            thread_context=thread_context,
        )
        return response_text
    gap_desc = match.group(1).strip()
    cleaned = _GAP_RE.sub("", response_text).rstrip()
    _km = kb_meta or {}
    knowledge_gaps.log_gap(
        entity=entity,
        channel=channel_name,
        user=user_id,
        question=user_message,
        response_chars=len(cleaned),
        gap=gap_desc,
        latency_ms=latency_ms,
        detector="llm_sentinel",
        private_source=is_dm,
        # kb_miss calibration (D-066 follow-up): same best-distance/count fields
        # the detector path records, when retrieval ran on this sentinel reply.
        best_distance=_km.get("kb_best_distance"),
        chunks_returned=_km.get("kb_chunks_returned"),
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

    # PHI never enters the knowledge pipeline. screen_contribution covers scope/
    # injection/length but NOT PHI, and the raw note is about to be sent to Haiku
    # for paraphrasing -- so refuse it here first. Mirrors _handle_info_for_cora;
    # the write-time re-check in apply_contributed_note is the entity-agnostic backstop.
    try:
        # is_clinical_phi catches the diagnosis/medication class is_phi_risk misses
        # (WS17-B fix) -- important here because the raw note is about to hit Haiku.
        # is_lex_billing_status_phi UNCONDITIONAL (entity-agnostic): the raw note is
        # about to hit Haiku via paraphrase_note, and a non-LEX-tagged note can carry
        # named-client LEX billing PHI (independent-review catch, WS17-C). Mirror the
        # write gate + the coras_read egress screen.
        note_is_phi = (phi_guard.is_phi_risk(content) or phi_guard.is_clinical_phi(content)
                       or phi_guard.is_lex_billing_status_phi(content))
    except Exception as exc:  # noqa: BLE001 -- fail safe: drop rather than risk PHI
        log.warning("team_learning: phi check failed (dropping): %s", exc)
        note_is_phi = True
    if note_is_phi:
        say(
            text=("Thanks, but that reads like client / PHI information -- I can't capture "
                  "that here. Client data belongs in the EHR, not in Cora's memory."),
            thread_ts=original_ts, unfurl_links=False, unfurl_media=False,
        )
        log.info("team_learning: PHI-flagged note refused user=%s entity=%s", user_id, entity)
        return

    paraphrase = team_learning.paraphrase_note(content, entity)
    preview_resp = say(
        text=(
            f"{paraphrase}\n\n"
            "Does that capture it? Reply *yes* to log it for Harrison's review, "
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


# ── Plain-DM Q&A (fixed 2026-06-11) ──────────────────────────────────────────
# Scheduler phrases that keep a DM on the OSN shift handler when the user is
# NOT mid-flow. Employees are nudged with the exact phrase "submit
# availability"; everything else in an idle DM is a question for Cora.
_SHIFT_DM_TRIGGERS = (
    "submit availability", "my availability", "availability",
    "my schedule", "my shifts", "when do i work",
)


def _dm_is_shift_message(user_id: str, text: str) -> bool:
    """True when a plain DM belongs to the OSN shift scheduler.

    Mid-flow users (DM state step != idle) stay with the scheduler
    unconditionally so multi-step availability submission is never hijacked by
    the Q&A pipeline. Idle users route there only on an explicit scheduler
    phrase.
    """
    try:
        if osn_shift_handler.get_dm_state(user_id).get("step", "idle") != "idle":
            return True
    except Exception:  # noqa: BLE001 — scheduler state must never break DMs
        log.warning("dm_routing: shift-state lookup failed for user=%s", user_id)
    t = (text or "").lower()
    return any(kw in t for kw in _SHIFT_DM_TRIGGERS)


def _fetch_dm_history(client, channel_id: str, current_msg_ts: str, limit: int = 10) -> list[dict]:
    """Prior messages of a DM conversation in Claude format (oldest first).

    DMs have no reliable thread structure — people type in the main composer —
    so conversation context (e.g. the 'yes' confirming a staged write) comes
    from the channel history itself. Best-effort: errors return [].
    """
    try:
        resp = client.conversations_history(channel=channel_id, limit=limit)
        raw_messages = resp.get("messages", [])
    except Exception as exc:  # noqa: BLE001
        log.warning("dm_history: conversations_history failed channel=%s: %s", channel_id, exc)
        return []

    bot_id = _CORA_BOT_USER_ID
    history: list[dict] = []
    for msg in reversed(raw_messages):  # API returns newest first
        if msg.get("ts") == current_msg_ts:
            continue  # the current message is appended as the final user turn
        if msg.get("subtype"):
            continue
        text = _MENTION_RE.sub("", msg.get("text", "")).strip()
        if not text:
            continue
        is_bot = bool(msg.get("bot_id")) or (bot_id and msg.get("user") == bot_id)
        history.append({"role": "assistant" if is_bot else "user", "content": text})

    # Anthropic requires alternating turns starting with user — same merge
    # rules as _fetch_thread_history.
    merged: list[dict] = []
    for turn in history:
        if merged and merged[-1]["role"] == turn["role"]:
            merged[-1]["content"] += "\n" + turn["content"]
        else:
            merged.append({"role": turn["role"], "content": turn["content"]})
    while merged and merged[0]["role"] == "assistant":
        merged.pop(0)
    return merged


def _handle_dm_qa(event: dict, client, user_id: str, text: str) -> None:
    """Run the full Q&A pipeline for a plain DM.

    Mirrors handle_mention's guard sequence (rate limit, user_access incl. the
    PHI custodian relaxation, help intent, sibling + cross-entity guards) —
    none of them are skipped just because the surface is a DM.

    Entity scope: the asker's primary entity from org-roles. The registry is
    ADVISORY (D-044) and is used here only to pick WHICH entity context to
    load — user_access.check_access still enforces authorization against that
    entity. Unknown users fall back to FNDR, which is exactly the catch-all
    channel posture (is_authorized allows unknown users FNDR/HJRG only, and
    every topic block still applies). Harrison is FNDR as everywhere else.
    """
    dm_channel = event.get("channel", user_id)
    current_ts = event.get("ts", "")
    # If the user is typing inside a thread — including Slack's AI-assistant
    # "Chat" pane (Agents & AI Apps mode), where every conversation is an
    # assistant thread on the im channel — replies MUST land in that thread.
    # A top-level reply renders only in the classic conversation view (the
    # "History" tab) and the pane the user is typing in looks unanswered.
    dm_thread_ts = event.get("thread_ts") or None

    allowed, _cap = rate_limiter.check(user_id, dm_channel)
    if not allowed:
        try:
            client.chat_postMessage(
                channel=dm_channel,
                text="You've hit the rate limit. Try again in a bit.",
            )
        except Exception:  # noqa: BLE001
            pass
        return

    _resolve_bot_user_id(client)

    role = org_roles.get_role(user_id)
    # org_roles.RoleRecord's field is `entity` (NOT `primary_entity`). Reading the
    # wrong attribute silently resolved EVERY DM to "FNDR" (the unknown-user
    # fallback), which (a) REFUSED DM Q&A for every teammate whose allowed_entities
    # excludes FNDR (user_access.check_access blocked them) and (b) blocked the
    # LEX-scope PHI relaxation for custodians (their DM never carried LEX scope).
    # Read the real field so a DM loads the asker's org-roles entity — an ADVISORY
    # pick of WHICH context to load (D-044); user_access still enforces authorization.
    # Unknown/unmapped users -> "" -> "FNDR" (the catch-all posture, unchanged).
    entity = (getattr(role, "entity", "") or "").strip() or "FNDR"
    # A portfolio-wide user (allowed_entities: all — e.g. cross-entity finance/HR)
    # works across every entity, so scoping their DM to a single home entity would
    # let cross_entity_guard redirect cross-entity questions they're authorized to
    # ask. Resolve them to the HJRG aggregator (pass-through in cross_entity_guard,
    # is_authorized True), matching the existing HJRG-primary allowed=all user. Only
    # allowed=all users qualify, so this never broadens a narrow-scope user.
    if user_access.has_unrestricted_entity_access(user_id):
        entity = "HJRG"
    if user_id == _FOUNDER_ID:
        entity = "FNDR"

    def _say(**kwargs) -> dict:
        # Follow the user's surface: threaded ask -> threaded reply (the
        # AI-assistant Chat pane case); top-level ask -> main conversation.
        if dm_thread_ts:
            kwargs["thread_ts"] = dm_thread_ts
        else:
            kwargs.pop("thread_ts", None)
        return client.chat_postMessage(channel=dm_channel, **kwargs)

    # DM financial tier: a DM is NOT a leadership/finance channel, so it is TIER_3
    # for the financials-block purpose — structurally, not via entity. Deriving it
    # from the asker's org-roles entity would make an HJRG-primary user's DM read
    # TIER_1 (is_tier_1 short-circuits True for HJRG), which would silently suppress
    # the company-financials deflection for a financials-blocked user if the roster
    # ever changed. Pin TIER_3 so the guarantee is roster-independent. Harrison
    # (root) is exempt from every topic block regardless of tier.
    function = channel_classifier.classify_function("dm")
    tier = "TIER_3"

    # PHI custodian relaxation: DMs count as LEX scope for allowlisted
    # custodians (lex_phi_access doctrine); everyone else unchanged.
    phi_custodian = lex_phi_access.phi_allowed(user_id, entity, is_dm=True)
    access_block = user_access.check_access(
        user_id, entity, text, phi_custodian=phi_custodian, tier=tier
    )
    if access_block:
        log.info(
            "dm_qa: user_access blocked user=%s entity=%s reason=%s",
            user_id, entity, access_block[:80],
        )
        _say(text=access_block, unfurl_links=False, unfurl_media=False)
        return

    if help_responder.is_help_intent(text):
        _say(text=help_responder.build_message(entity, function, tier),
             unfurl_links=False, unfurl_media=False)
        return

    sibling_redirect = sibling_guard.check_redirect(entity, text)
    if sibling_redirect:
        _say(text=sibling_redirect, unfurl_links=False, unfurl_media=False)
        return

    cross_redirect = cross_entity_guard.check_cross_entity(text, entity)
    if cross_redirect:
        _say(text=cross_redirect, unfurl_links=False, unfurl_media=False)
        return

    log.info(
        "dm_qa routed user=%s entity=%s thread=%s text=%.80s",
        user_id, entity, bool(dm_thread_ts), text,
    )

    # Conversation context: thread replies (assistant pane) read the thread;
    # top-level DMs read the recent channel history.
    if dm_thread_ts:
        prior_messages = _fetch_thread_history(client, dm_channel, dm_thread_ts, current_ts)
    else:
        prior_messages = _fetch_dm_history(client, dm_channel, current_ts)

    _dispatch_qa(
        channel_id=dm_channel,
        channel_name="dm",
        user_id=user_id,
        user_message=text,
        reply_thread_ts=dm_thread_ts,  # _say enforces the same surface either way
        entity=entity,
        client=client,
        say=_say,
        prior_messages=prior_messages,
        root_thread_ts=dm_thread_ts or current_ts,
    )


# Message event handler — correction capture + active-thread follow-up routing.
# Bolt requires an explicit event listener for "message" events.
def _handle_info_for_cora(event: dict, client) -> None:
    """Intake for #info-for-cora, route 2 of 3 -- the ORIGINAL D1 message-event path.

    KEPT DELIBERATELY EVEN THOUGH IT CURRENTLY NEVER FIRES. Channel `message`
    events do not reach this app (evidence in info_intake's module docstring), so
    this handler has produced exactly zero items since it shipped 2026-06-13. The
    moment the Slack app's Event Subscriptions gain message.groups it starts
    contributing with NO further code change -- and because it derives the same
    infocora-{ts} id as the @mention and sweep routes, a message delivered by two
    routes is queued once and acked once.

    The bot/subtype guards stay: Cora's OWN replies carry bot_id (verified on the
    wire 2026-08-06), so re-ingesting them would reopen the KB self-poisoning class
    (cq-8d16969e85fb). Cowork-connector posts carry NO bot_id and pass through as
    ordinary user messages -- which is correct, they ARE human contributions.
    """
    if event.get("bot_id") or event.get("subtype") in _INFO_FOR_CORA_SKIP_SUBTYPES:
        return
    user_id = event.get("user", "")
    text = (event.get("text") or "").strip()
    ts = event.get("ts", "")
    if not user_id or not text or user_id == _CORA_BOT_USER_ID:
        return

    channel = event.get("channel", "") or INFO_FOR_CORA_CHANNEL_ID
    reply_ts = event.get("thread_ts") or ts

    author_name = user_id
    try:
        rec = org_roles.get_role(user_id)
        if rec and rec.name:
            author_name = rec.name
    except Exception as exc:  # noqa: BLE001
        log.warning("info-for-cora: org_roles lookup failed: %s", exc)

    # Strip a LEADING @Cora token (this path also sees @mentioned messages) and
    # unwrap an explicit "note: <fact>" prefix, matching the @mention route.
    body = _MENTION_RE.sub("", text).strip() or text
    body = team_learning.parse_note(body) or body

    result = info_intake.ingest(
        text=body, author_id=user_id, author_name=author_name, ts=ts,
        route="message_event", channel_id=channel,
    )
    if result.outcome == info_intake.NOT_A_CONTRIBUTION:
        return
    if result.ack:
        try:
            client.chat_postMessage(
                channel=channel, text=result.ack, thread_ts=reply_ts,
                unfurl_links=False, unfurl_media=False,
            )
        except Exception as exc:  # noqa: BLE001 -- ack failure must not break intake
            log.warning("info-for-cora: ack post failed: %s", exc)
    log.info("info-for-cora: message-event intake outcome=%s user=%s entity=%s",
             result.outcome, user_id, result.entity)


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
    # ── DM path — gap-ask reply capture, then OSN shift scheduler ───────────
    channel_type = event.get("channel_type", "")
    if channel_type == "im":
        user_id = event.get("user", "")
        text = event.get("text", "").strip()
        if user_id and text and not event.get("bot_id"):
            # Gap autofill Stage 2: if this user has a pending knowledge-gap
            # ask, treat the reply as the answer. Threaded replies to the ask
            # message always match. A top-level DM matches only when it is NOT
            # an OSN shift-scheduler command AND does NOT read as a fresh
            # question (W-DMQ): the lone-ask top-level match is greedy, so an
            # unrelated question a teammate DMs while one ask is live (e.g.
            # "what's our cash position?") would otherwise be swallowed and
            # proposed to Harrison as a bogus known-answer. A clearly
            # interrogative top-level DM falls through to the normal Q&A path
            # instead; a genuine answer typed in the ask's OWN thread still
            # always matches (looks_like_question only gates the top-level path).
            try:
                ask = gap_autofill.match_pending_ask(
                    user_id,
                    event.get("thread_ts"),
                    allow_toplevel=(
                        not gap_autofill.is_shift_keyword(text)
                        and not gap_autofill.looks_like_question(text)
                    ),
                )
            except Exception as exc:  # noqa: BLE001 — capture must never break DMs
                log.warning("gap_autofill: match_pending_ask failed: %s", exc)
                ask = None
            if ask:
                log.info("gap_autofill: DM reply captured user=%s ask=%s",
                         user_id, ask.get("ask_id", "?"))
                try:
                    ack = gap_autofill.record_ask_answer(ask, text)
                except Exception as exc:  # noqa: BLE001
                    log.error("gap_autofill: record_ask_answer failed: %s", exc)
                    ack = ("Sorry — something went wrong recording that. "
                           "I'll re-ask if it's still needed.")
                try:
                    client.chat_postMessage(
                        channel=event.get("channel", user_id),
                        text=ack,
                        thread_ts=event.get("thread_ts"),
                        unfurl_links=False,
                        unfurl_media=False,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("gap_autofill: ack post failed: %s", exc)
                return
            # ── Tier-2 historical retrieval (DM-only by design) ─────────────
            # Explicit "pull up / show me my emails" requests in a plain DM
            # route into the Q&A pipeline; the historical-access gate at the
            # top of _dispatch_qa issues the owner-scoped grant (or refuses,
            # fail-closed). The grant path withholds the static portfolio
            # context, so this adds no entity exposure for non-FNDR users.
            #
            # W2-04 — guard-trio exemption (documented, deliberate): unlike
            # _handle_dm_qa below, this branch dispatches WITHOUT re-running
            # user_access.check_access / sibling_guard / cross_entity_guard.
            # That is safe by construction, NOT by omission:
            #   * check_tier2 is FAIL-CLOSED — an unmapped identity gets no grant.
            #   * the grant is scoped to the asker's OWN mailbox (owned_kb_search),
            #     which they may always see (Harrison directive; the topic-block
            #     exemption is documented at historical_access.py L31-34), so a
            #     cross-entity / sibling leak is structurally impossible here.
            #   * _build_grant_context applies historical_access.drop_phi before
            #     the content ever reaches the model.
            # Do NOT "restore" the guard trio here without re-reading that
            # contract: the trio's job (entity/topic scoping) is already
            # subsumed by the owner-scope + fail-closed grant.
            if historical_access.detect_retrieval_intent(text):
                dm_channel = event.get("channel", user_id)
                allowed, _cap = rate_limiter.check(user_id, dm_channel)
                if not allowed:
                    client.chat_postMessage(
                        channel=dm_channel,
                        text="You've hit the rate limit. Try again in a bit.",
                    )
                    return
                log.info(
                    "historical_access: DM retrieval intent user=%s text=%.80s",
                    user_id, text,
                )
                _dispatch_qa(
                    channel_id=dm_channel,
                    channel_name="dm",
                    user_id=user_id,
                    user_message=text,
                    reply_thread_ts=event.get("thread_ts") or event.get("ts"),
                    entity="FNDR",
                    client=client,
                    say=lambda **kw: client.chat_postMessage(channel=dm_channel, **kw),
                    prior_messages=[],
                    root_thread_ts=None,
                )
                return
            # ── Plain-DM routing: shift scheduler vs Q&A (fixed 2026-06-11) ──
            # Slack does NOT deliver app_mention events for IMs, so this branch
            # is the ONLY DM entry point. Before this fix every non-retrieval
            # DM fell through to the OSN shift scheduler greeting and DM Q&A
            # (incl. the Phase 5 personal-notes write path) was unreachable.
            # The scheduler keeps (a) users mid availability flow and (b)
            # explicit scheduler phrases; everything else is a Q&A question.
            if _dm_is_shift_message(user_id, text):
                log.info("osn_shift_handler: DM from user=%s text=%r", user_id, text[:80])
                osn_shift_handler.handle_dm(text=text, slack_user_id=user_id, client=client)
                return
            _handle_dm_qa(event, client, user_id, text)
        return

    # Hard block: never respond in permanently blocked channels
    if _is_blocked_channel(event.get("channel", "")):
        return

    # #info-for-cora intake (D1): users post facts here; route them into the
    # Harrison-gated knowledge-review queue (top-level AND thread replies), then
    # stop -- this channel is intake-only, not a Q&A surface.
    if event.get("channel", "") == INFO_FOR_CORA_CHANNEL_ID:
        _handle_info_for_cora(event, client)
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
            # Author confirmed -- fold the contribution into the ONE Harrison-gated
            # knowledge queue (WS17-C). No #cora-kq approval card / per-entity
            # approver anymore: propose a GENERIC update (source=info-for-cora) so on
            # Harrison's 👍 it writes to known-answers/{entity}.md via
            # apply_contributed_note -- the same path #info-for-cora uses.
            team_learning.clear_pending_confirm(channel_id, thread_ts)

            # Prefer the author-confirmed paraphrase (it incorporates any inline
            # corrections); fall back to the raw note if paraphrasing failed.
            text_to_store = (pending.get("paraphrase") or pending["raw_content"]).strip()
            entity = pending["entity"]  # route(channel) -- specific tag, not an org_roles re-derive
            author_name = pending["author"]
            try:
                rec = org_roles.get_role(user_id)
                if rec and rec.name:
                    author_name = rec.name
            except Exception as exc:  # noqa: BLE001
                log.warning("team_note: org_roles lookup failed: %s", exc)

            # PHI never enters the knowledge pipeline. Screen the FINAL text being
            # proposed (catches PHI introduced via an inline correction). Mirrors
            # _handle_info_for_cora; apply_contributed_note re-checks at the write.
            phi_hit = False
            try:
                phi_hit = (phi_guard.is_phi_risk(text_to_store)
                           or phi_guard.is_clinical_phi(text_to_store))
                if not phi_hit and entity.upper().startswith("LEX"):
                    phi_hit = phi_guard.is_lex_billing_status_phi(text_to_store)
            except Exception as exc:  # noqa: BLE001 -- fail safe: drop
                log.warning("team_note: phi check failed (dropping): %s", exc)
                phi_hit = True

            logged = False  # True only once the contribution is actually queued
            if phi_hit:
                confirmed_text = (
                    "That reads like client / PHI information, so I can't add it to "
                    "Cora's memory. Client data belongs in the EHR."
                )
                log.info("team_learning: PHI-flagged contribution dropped at confirm user=%s", user_id)
            else:
                update_id = f"teamnote-{thread_ts}"
                try:
                    already = any(
                        u.get("update_id") == update_id
                        for u in knowledge_review.load_proposed_updates()
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("team_note: dedup check failed (continuing): %s", exc)
                    already = False
                if already:
                    logged = True  # an earlier delivery of this 'yes' already queued it
                else:
                    try:
                        knowledge_review.propose_update(
                            update_id=update_id,
                            update_type=knowledge_review.UPDATE_TYPE_GENERIC,
                            description=f"Team note from {author_name} ({entity}): {text_to_store[:240]}",
                            payload={
                                "text": text_to_store,
                                "author_id": user_id,
                                "author_name": author_name,
                                "entity": entity,
                                "channel": pending["channel_name"],
                                "source": "info-for-cora",
                                "kind": pending["kind"],
                                "message_ts": thread_ts,
                            },
                            source_evidence=pending["raw_content"],
                            confidence="MED",
                        )
                        logged = True
                    except Exception as exc:  # noqa: BLE001 -- confirm must not break the bot
                        log.warning("team_note: propose_update failed: %s", exc)
                if logged:
                    confirmed_text = (
                        f"{pending['paraphrase']}\n\n"
                        "✅ Logged for Harrison's review. It won't become shared org "
                        "knowledge until he approves it."
                    )
                else:
                    # pending state is already cleared above, so the note can't be
                    # recovered automatically -- tell the truth, never fake a ✅.
                    confirmed_text = (
                        "Sorry, I couldn't log that just now -- please resend the note "
                        "so it isn't lost."
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
                    say(text=confirmed_text, thread_ts=thread_ts,
                        unfurl_links=False, unfurl_media=False)
            else:
                say(text=confirmed_text, thread_ts=thread_ts,
                    unfurl_links=False, unfurl_media=False)
            # Feature 2: React ✅ ONLY when the note was actually queued (not on a
            # PHI refusal or a failed propose).
            if logged:
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
            # Author is correcting. Screen the correction text for PHI BEFORE it
            # reaches Haiku (paraphrase_note embeds it in the prompt) and before it
            # can launder PHI past the confirm gate. Mirrors _handle_note.
            corr_entity = pending["entity"]
            corr_phi = False
            try:
                # is_lex_billing_status_phi UNCONDITIONAL: the correction is about to hit
                # Haiku via paraphrase_note; a non-LEX-tagged correction can carry LEX
                # billing PHI (independent-review catch, WS17-C).
                corr_phi = (phi_guard.is_phi_risk(text) or phi_guard.is_clinical_phi(text)
                            or phi_guard.is_lex_billing_status_phi(text))
            except Exception as exc:  # noqa: BLE001 -- fail safe: drop the correction
                log.warning("team_note: correction phi check failed (dropping): %s", exc)
                corr_phi = True
            if corr_phi:
                say(
                    text=("That correction reads like client / PHI information, so I "
                          "can't apply it. Client data belongs in the EHR -- the "
                          "previous version is unchanged."),
                    thread_ts=thread_ts, unfurl_links=False, unfurl_media=False,
                )
                log.info("team_learning: PHI-flagged correction dropped user=%s", user_id)
                return
            # Re-paraphrase incorporating the (PHI-screened) correction.
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
                            "Does that capture it? Reply *yes* to log it for Harrison's review, "
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

    # ── W1-01: skip Path 2 when this reply @mentions Cora ────────────────────
    # An @mention posted as a reply INSIDE an active thread is delivered by
    # Slack as BOTH an app_mention event (-> handle_mention, a full answer) AND
    # this message event. Without this guard Path 2 dispatches a SECOND full
    # answer on the same message (doubled LLM call + duplicate reply, and on
    # mention-polluted text since Path 2 never strips the leading <@Uxxx>).
    # handle_mention already owns any message that mentions Cora, so bail here.
    # Scope: only Cora's OWN bot id triggers the skip -- an in-thread follow-up
    # that merely mentions a teammate (<@Usomeone>) is a legitimate Path-2
    # question and must still route through. Fail OPEN when the bot id can't be
    # resolved (never drop a real follow-up). This is deliberately placed AFTER
    # Path 0 (note-confirm) and Path 1 (correction), which handle_mention does
    # NOT own -- a "@Cora yes"/correction reply must still reach those paths.
    _cora_bot_id = _resolve_bot_user_id(client)
    if _cora_bot_id and f"<@{_cora_bot_id}>" in text:
        log.info(
            "thread_followup: skip Path 2 -- reply @mentions Cora "
            "(handle_mention owns it) channel=%s user=%s",
            channel_id, user_id,
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

    # User access check pre-LLM (mirrors handle_mention + /cora-ask; same params,
    # same ordering: check_access -> sibling -> cross). Closes the 6/18 gap: an
    # in-thread follow-up previously skipped check_access, so the entity-auth /
    # finance-topic (D-064) / PHI blocks enforced at the @mention did not hold
    # in-thread. user_id is non-empty here (guarded at the top of the handler);
    # Path 2 is channel threads, so is_dm is computed for parity only.
    is_dm = str(channel_id).startswith("D")
    phi_custodian = lex_phi_access.phi_allowed(user_id, entity, is_dm=is_dm)
    tier = channel_classifier.tier_label(
        entity, channel_classifier.classify_function(channel_name)
    )
    access_block = user_access.check_access(
        user_id, entity, text, phi_custodian=phi_custodian, tier=tier
    )
    if access_block:
        # By design this also blocks a staged-write CONFIRM reply that echoes a
        # blocked-topic phrase from the preview ("yes, the DDD revalidation one")
        # -- exempting confirmation-shaped text would be a smuggling hole. A bare
        # "yes"/"confirm" passes and completes the staged write.
        log.info(
            "thread_followup: user_access blocked user=%s entity=%s reason=%s",
            user_id, entity, access_block[:80],
        )
        client.chat_postMessage(
            channel=channel_id, thread_ts=thread_ts, text=access_block,
            unfurl_links=False, unfurl_media=False,
        )
        return

    # Apply sibling-entity guard pre-LLM (mirrors handle_mention Path 1).
    sibling_redirect = sibling_guard.check_redirect(entity, text)
    if sibling_redirect:
        client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text=sibling_redirect)
        return

    # Cross-entity guard pre-LLM (mirrors handle_mention Path 1).
    cross_redirect = cross_entity_guard.check_cross_entity(text, entity)
    if cross_redirect:
        client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text=cross_redirect)
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

    # PHI never enters the knowledge pipeline (screen_contribution has no PHI check).
    try:
        bm_is_phi = phi_guard.is_phi_risk(content) or phi_guard.is_clinical_phi(content)
        if not bm_is_phi and entity.upper().startswith("LEX"):
            bm_is_phi = phi_guard.is_lex_billing_status_phi(content)
    except Exception as exc:  # noqa: BLE001 -- fail safe: drop rather than risk PHI
        log.warning("bookmark_reaction: phi check failed (dropping): %s", exc)
        bm_is_phi = True
    if bm_is_phi:
        try:
            client.chat_postEphemeral(
                channel=channel_id, user=reactor,
                text=("📚 That reads like client / PHI information -- I can't capture it. "
                      "Client data belongs in the EHR, not in Cora's memory."),
            )
        except Exception as exc:
            log.warning("bookmark_reaction: ephemeral phi-fail: %s", exc)
        log.info("bookmark_reaction: PHI-flagged content dropped reactor=%s entity=%s", reactor, entity)
        return

    # Fold into the ONE Harrison-gated knowledge queue (WS17-C). A bookmark has no
    # paraphrase step, so capture the raw message text. On Harrison's 👍 it writes
    # to known-answers/{entity}.md via apply_contributed_note -- the same path
    # #info-for-cora uses. No #cora-kq card / per-entity approver anymore.
    author_name = reactor
    try:
        rec = org_roles.get_role(reactor)
        if rec and rec.name:
            author_name = rec.name
    except Exception as exc:  # noqa: BLE001
        log.warning("bookmark_reaction: org_roles lookup failed: %s", exc)

    update_id = f"bookmark-{message_ts}"
    try:
        already = any(u.get("update_id") == update_id
                      for u in knowledge_review.load_proposed_updates())
    except Exception as exc:  # noqa: BLE001
        log.warning("bookmark_reaction: dedup check failed (continuing): %s", exc)
        already = False
    if not already:
        try:
            knowledge_review.propose_update(
                update_id=update_id,
                update_type=knowledge_review.UPDATE_TYPE_GENERIC,
                description=f"Bookmarked by {author_name} ({entity}): {content[:240]}",
                payload={
                    "text": content,
                    "author_id": reactor,
                    "author_name": author_name,
                    "entity": entity,
                    "channel": channel_name,
                    "source": "info-for-cora",
                    "kind": "bookmark",
                    "message_ts": message_ts,
                },
                source_evidence=content,
                confidence="MED",
            )
        except Exception as exc:  # noqa: BLE001 -- must not break the bot
            log.warning("bookmark_reaction: propose_update failed: %s", exc)
            return

    try:
        client.chat_postEphemeral(
            channel=channel_id, user=reactor,
            text=(f"📚 Logged for Harrison's review. It won't become shared *{entity}* "
                  "knowledge until he approves it."),
        )
    except Exception as exc:
        log.warning("bookmark_reaction: ephemeral confirm failed: %s", exc)


def _handle_react_to_task(
    *,
    client,
    reactor: str,
    channel_id: str,
    channel_name: str,
    message_ts: str,
) -> None:
    """Create an Asana task from a clipboard-reacted message and DM the reactor."""
    import yaml as _yaml
    from pathlib import Path as _Path
    from cora.tools.asana_client import create_task, AsanaClientError

    try:
        hist = client.conversations_history(
            channel=channel_id, latest=message_ts, limit=1, inclusive=True
        )
        msgs = hist.get("messages") or []
        if not msgs:
            log.info("react_to_task: no messages found ts=%s channel=%s", message_ts, channel_id)
            return
        msg_text = msgs[0].get("text", "").strip()
        if not msg_text:
            log.info("react_to_task: empty message ts=%s channel=%s -- skipping", message_ts, channel_id)
            return

        # Truncate task name to 250 chars (Asana limit)
        task_name = msg_text[:250]

        # Resolve reactor's Asana GID from slack-to-asana.yaml
        _repo_root = _Path(__file__).resolve().parents[2]
        asana_map_path = _repo_root / "data" / "maps" / "slack-to-asana.yaml"
        asana_map: dict = {}
        try:
            raw = _yaml.safe_load(asana_map_path.read_text(encoding="utf-8")) or {}
            for u in raw.get("users") or []:
                sid = u.get("slack_user_id")
                if sid:
                    asana_map[sid] = u
        except Exception as exc:
            log.warning("react_to_task: failed to load asana map: %s", exc)

        assignee_gid: str | None = None
        reactor_name = "you"
        if reactor in asana_map:
            gid_val = asana_map[reactor].get("asana_user_gid")
            assignee_gid = str(gid_val) if gid_val else None
            dn = (asana_map[reactor].get("display_name") or "").strip()
            reactor_name = dn.split()[0] if dn else "you"

        task = create_task(
            name=task_name,
            assignee_gid=assignee_gid or None,
            notes=f"Created from Slack message in #{channel_name} via clipboard reaction.",
        )
        task_url = task.get("permalink_url", "")
        task_gid = task.get("gid", "")

        # DM the reactor
        try:
            dm = client.conversations_open(users=[reactor])
            dm_channel = dm["channel"]["id"]
            reply = f":clipboard: Task created for {reactor_name}!"
            if task_url:
                reply += f"\n{task_url}"
            client.chat_postMessage(channel=dm_channel, text=reply)
        except Exception as exc:
            log.warning("react_to_task: DM failed for reactor=%s: %s", reactor, exc)

        log.info(
            "react_to_task: task=%s created for reactor=%s channel=#%s",
            task_gid, reactor, channel_name,
        )
    except AsanaClientError as exc:
        log.warning("react_to_task: Asana error: %s", exc)
    except Exception as exc:
        log.warning("react_to_task handler failed: %s", exc)


def _handle_reaction(event: dict, client, event_type: str) -> None:
    """Shared logic for reaction_added and reaction_removed events."""
    item = event.get("item") or {}
    if item.get("type") != "message":
        return  # ignore reactions on files, channel boundaries, etc.

    channel_id = item.get("channel", "")
    reactor = event.get("user", "")
    reaction = event.get("reaction", "")
    message_ts = item.get("ts", "")

    # ── 📋 react-to-task: ANY message -> Asana task + DM reactor ───────────────
    if event_type == "reaction_added" and reaction == "clipboard" and reactor and channel_id:
        channel_name = _resolve_channel_name(client, channel_id)
        _handle_react_to_task(
            client=client,
            reactor=reactor,
            channel_id=channel_id,
            channel_name=channel_name,
            message_ts=message_ts,
        )
        return

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
    # 👍 to attach the thread or 👎 to skip. Runs AFTER the item_user==bot gate
    # above: the pending-reaction DM is Cora-authored (item_user == bot), so it
    # passes that gate cleanly, and get_pending_reaction is keyed on Cora's own
    # DM ts -- so the post-gate position is functionally correct.
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

    # NOTE (WS17-B item 10): a second 📚-bookmark handler used to live here calling
    # _handle_note(kind="bookmark"). It was DEAD — the books branch above
    # (_handle_bookmark_reaction) returns first, so this never ran. Removed to keep
    # one bookmark path. _handle_bookmark_reaction is the live one.

    # W1-02: a redundant re-fetch of item_user/bot_user_id + an identical
    # `if not bot_user_id or item_user != bot_user_id: return` used to sit here.
    # It was dead -- the gate above already guarantees item_user == bot_user_id
    # (it returns otherwise) and neither value can change in between. Removed.
    # Every handler below runs only on Cora-authored messages via that one gate.

    # ── OSN shift scheduler: ✅ on a schedule message approves + publishes it ──
    if event_type == "reaction_added" and reaction == "white_check_mark":
        sched_reply = osn_shift_handler.handle_schedule_approval_reaction(
            reaction=reaction, message_ts=message_ts, reactor_user_id=reactor, client=client
        )
        if sched_reply:
            client.chat_postMessage(channel=channel_id, text=sched_reply)

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
        # Code-session queue S3 (fail-soft, off-thread): >= threshold thumbs-downs
        # on similar replies within the window promotes to a bug candidate.
        code_queue.capture_thumbsdown(channel_id, message_ts, entity, reactor, client=client)


@app.event("reaction_added")
def handle_reaction_added(event: dict, client) -> None:
    _handle_reaction(event, client, "reaction_added")


@app.event("reaction_removed")
def handle_reaction_removed(event: dict, client) -> None:
    _handle_reaction(event, client, "reaction_removed")


# ── One-tap knowledge-review approve/dismiss (2026-07-09 write-path) ──────────
# Block Kit buttons on the knowledge-review DM. Harrison taps Approve and the
# item is written + resolved IMMEDIATELY (keeping the Harrison-only human gate;
# D-011 intact -- friction-removal, NOT auto-approve). The emoji 👍/👎 path is
# unchanged as the belt-and-braces (processed at the next scheduled run), so
# nothing regresses if Slack interactivity is disabled. All correctness
# (Harrison gate, idempotency, apply-first-then-resolve) lives in
# knowledge_review.process_one_tap_action; this wrapper is only Slack I/O.

def _handle_knowledge_one_tap(body: dict, client, *, approve: bool,
                              processor=None) -> None:
    try:
        actions = body.get("actions") or []
        update_id = (actions[0].get("value") if actions else "") or ""
        actor_id = (body.get("user") or {}).get("id", "")
        channel_id = (body.get("channel") or {}).get("id", "")
        message_ts = (body.get("message") or {}).get("ts", "")

        # Same wrapper serves the knowledge cards and the Fork-4 decision cards;
        # only the correctness processor differs (both share the contract:
        # Harrison gate, _ONE_TAP_LOCK, apply-first-then-resolve).
        proc = processor or knowledge_review.process_one_tap_action
        outcome, msg = proc(update_id, actor_id, approve=approve)

        # Audit trail. event_type="block_action" (NOT reaction_added) so the
        # scheduled correlate_reactions_to_updates never re-processes this item.
        try:
            knowledge_review.log_reply_reaction(
                reactor_id=actor_id,
                reaction=("button_approve" if approve else "button_dismiss"),
                message_ts=message_ts,
                channel_id=channel_id,
                channel_name="dm",
                event_type="block_action",
            )
        except Exception:  # noqa: BLE001 -- audit is best-effort
            pass

        if outcome == "not_authorized":
            # A non-Harrison actor: refuse without rewriting Harrison's DM.
            try:
                client.chat_postEphemeral(channel=channel_id, user=actor_id, text=msg)
            except Exception:  # noqa: BLE001
                pass
            return

        # Update the DM in place: keep the item's original text, append the
        # outcome, and drop the buttons so it can't be re-tapped.
        if channel_id and message_ts:
            orig = (body.get("message") or {}).get("blocks") or []
            section_blocks = [b for b in orig if b.get("type") == "section"]
            new_blocks = section_blocks + [
                {"type": "context", "elements": [{"type": "mrkdwn", "text": msg}]}
            ]
            if not section_blocks:
                new_blocks = [
                    {"type": "section", "text": {"type": "mrkdwn", "text": msg}}
                ]
            try:
                client.chat_update(
                    channel=channel_id, ts=message_ts, text=msg, blocks=new_blocks,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("knowledge one-tap: chat_update failed: %s", exc)
    except Exception:  # noqa: BLE001 -- a handler error must never crash the bot
        log.warning("knowledge one-tap handler error (non-fatal)", exc_info=True)


@app.action(knowledge_review.ACTION_APPROVE)
def handle_knowledge_approve(ack, body, client) -> None:
    ack()
    _handle_knowledge_one_tap(body, client, approve=True)


@app.action(knowledge_review.ACTION_DISMISS)
def handle_knowledge_dismiss(ack, body, client) -> None:
    ack()
    _handle_knowledge_one_tap(body, client, approve=False)


# ── Decision cards (Fork 4, 2026-08-01): Accept -> NON-canon decisions inbox ──
# Dedicated action ids so a decision tap can never reach the knowledge applier
# (and via it the autowrite path). Correctness lives in
# knowledge_review.process_decision_tap + decision_inbox.apply_decision_accept;
# these wrappers are only Slack I/O.

@app.action(knowledge_review.ACTION_DECISION_ACCEPT)
def handle_decision_accept(ack, body, client) -> None:
    ack()
    _handle_knowledge_one_tap(
        body, client, approve=True,
        processor=knowledge_review.process_decision_tap)


@app.action(knowledge_review.ACTION_DECISION_DISMISS)
def handle_decision_dismiss(ack, body, client) -> None:
    ack()
    _handle_knowledge_one_tap(
        body, client, approve=False,
        processor=knowledge_review.process_decision_tap)


# ── One-tap auto-write REVERT (§7B, 2026-07-21) ──────────────────────────────
# The weekly auto-write digest carries a Revert button per item. Harrison taps it
# and knowledge_review.process_autowrite_revert removes the auto-written block +
# marks the audit reverted (Harrison-only; D-011-relaxed reversibility). All
# correctness lives in knowledge_review; this wrapper is only Slack I/O.

def _handle_autowrite_revert(body, client) -> None:
    try:
        actions = body.get("actions") or []
        update_id = (actions[0].get("value") if actions else "") or ""
        actor_id = (body.get("user") or {}).get("id", "")
        channel_id = (body.get("channel") or {}).get("id", "")
        message_ts = (body.get("message") or {}).get("ts", "")

        outcome, msg = knowledge_review.process_autowrite_revert(update_id, actor_id)

        if outcome == "not_authorized":
            try:
                client.chat_postEphemeral(channel=channel_id, user=actor_id, text=msg)
            except Exception:  # noqa: BLE001
                pass
            return
        # Post the outcome as a threaded reply so the digest stays intact and the
        # other items keep their Revert buttons.
        if channel_id and message_ts:
            try:
                client.chat_postMessage(channel=channel_id, thread_ts=message_ts, text=msg)
            except Exception as exc:  # noqa: BLE001
                log.warning("autowrite revert: reply failed: %s", exc)
    except Exception:  # noqa: BLE001 -- a handler error must never crash the bot
        log.warning("autowrite revert handler error (non-fatal)", exc_info=True)


@app.action(knowledge_review.ACTION_AUTOWRITE_REVERT)
def handle_autowrite_revert(ack, body, client) -> None:
    ack()
    _handle_autowrite_revert(body, client)


# ── Missed-Message Catch-Up one-tap (Send / Skip / Edit) ─────────────────────────
# Mirrors the knowledge one-tap contract: ack() immediately, then delegate; ALL
# correctness (Harrison gate, idempotency, apply-then-record, re-guard-at-post)
# lives in missed_message_catchup.process_catchup_action. The emoji fallback does
# NOT apply here (there is no scheduled correlator for catch-up), so these buttons
# require Slack Interactivity to be ON; a card with no working buttons is harmless
# (it just cannot be actioned) -- nothing auto-posts either way.

def _handle_catchup_one_tap(body: dict, client, *, action: str) -> None:
    try:
        actions = body.get("actions") or []
        cid = (actions[0].get("value") if actions else "") or ""
        actor_id = (body.get("user") or {}).get("id", "")
        channel_id = (body.get("channel") or {}).get("id", "")
        message_ts = (body.get("message") or {}).get("ts", "")

        outcome, msg = missed_catchup.process_catchup_action(
            cid, actor_id, client, action=action,
        )

        if outcome == "not_authorized":
            try:
                client.chat_postEphemeral(channel=channel_id, user=actor_id, text=msg)
            except Exception:  # noqa: BLE001
                pass
            return

        # Update the DM card in place: keep the item text, append the outcome, drop
        # the buttons so it can't be re-tapped.
        if channel_id and message_ts:
            orig = (body.get("message") or {}).get("blocks") or []
            section_blocks = [b for b in orig if b.get("type") == "section"]
            new_blocks = section_blocks + [
                {"type": "context", "elements": [{"type": "mrkdwn", "text": msg}]}
            ]
            if not section_blocks:
                new_blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": msg}}]
            try:
                client.chat_update(channel=channel_id, ts=message_ts, text=msg, blocks=new_blocks)
            except Exception as exc:  # noqa: BLE001
                log.warning("catchup one-tap: chat_update failed: %s", exc)
    except Exception:  # noqa: BLE001 -- a handler error must never crash the bot
        log.warning("catchup one-tap handler error (non-fatal)", exc_info=True)


@app.action(missed_catchup.ACTION_SEND)
def handle_catchup_send(ack, body, client) -> None:
    ack()
    _handle_catchup_one_tap(body, client, action="send")


@app.action(missed_catchup.ACTION_SKIP)
def handle_catchup_skip(ack, body, client) -> None:
    ack()
    _handle_catchup_one_tap(body, client, action="skip")


@app.action(missed_catchup.ACTION_EDIT)
def handle_catchup_edit(ack, body, client) -> None:
    """Open a modal prefilled with the draft so Harrison can edit before sending."""
    ack()
    try:
        actions = body.get("actions") or []
        cid = (actions[0].get("value") if actions else "") or ""
        actor_id = (body.get("user") or {}).get("id", "")
        channel_id = (body.get("channel") or {}).get("id", "")
        message_ts = (body.get("message") or {}).get("ts", "")
        trigger_id = body.get("trigger_id", "")

        if actor_id != missed_catchup.HARRISON_ID:
            try:
                client.chat_postEphemeral(
                    channel=channel_id, user=actor_id,
                    text="Only Harrison can edit catch-up replies.",
                )
            except Exception:  # noqa: BLE001
                pass
            return

        row = missed_catchup.latest_disposition(cid)
        if not row or row.get("disposition") != "pending":
            try:
                client.chat_postEphemeral(
                    channel=channel_id, user=actor_id,
                    text="That catch-up item is no longer editable.",
                )
            except Exception:  # noqa: BLE001
                pass
            return

        view = missed_catchup.edit_modal_view(
            cid, channel_id, message_ts, row.get("draft_text", ""),
        )
        client.views_open(trigger_id=trigger_id, view=view)
    except Exception:  # noqa: BLE001
        log.warning("catchup edit-modal open failed (non-fatal)", exc_info=True)


@app.view(missed_catchup.VIEW_EDIT_SUBMIT)
def handle_catchup_edit_submit(ack, body, client, view) -> None:
    ack()
    try:
        meta = json.loads(view.get("private_metadata") or "{}")
        cid = meta.get("catchup_id", "")
        dm_channel = meta.get("dm_channel", "")
        dm_ts = meta.get("dm_ts", "")
        actor_id = (body.get("user") or {}).get("id", "")
        state = (view.get("state") or {}).get("values") or {}
        edited = (
            (state.get("catchup_edit_block") or {})
            .get("catchup_edit_input", {})
            .get("value", "")
        ) or ""

        outcome, msg = missed_catchup.process_catchup_action(
            cid, actor_id, client, action="send", edited_text=edited,
        )

        # Rewrite the original DM card to reflect the outcome (best-effort).
        if dm_channel and dm_ts and outcome != "not_authorized":
            try:
                client.chat_update(
                    channel=dm_channel, ts=dm_ts, text=msg,
                    blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": msg}}],
                )
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        log.warning("catchup edit-submit handler error (non-fatal)", exc_info=True)


# ── Revenue-ops Tier-1 send cards (R2, D-081 pattern; design 2026-08-01) ────────
# ack() first, delegate; ALL correctness (approver gate, kill switch, byte-exact
# stash, guard re-run, recipient subset, single-shot claim) lives in
# revops.cards.process_send_action -> revops.sender.send_stashed. Ships DARK:
# with CORA_SEND_LIVE=off (the default) an approved tap refuses in the gate.

def _handle_revops_send_tap(body: dict, client, *, action: str) -> None:
    try:
        actions = body.get("actions") or []
        sid = (actions[0].get("value") if actions else "") or ""
        actor_id = (body.get("user") or {}).get("id", "")
        channel_id = (body.get("channel") or {}).get("id", "")
        message_ts = (body.get("message") or {}).get("ts", "")

        outcome, msg = revops_cards.process_send_action(sid, actor_id, action=action)

        if outcome == "not_authorized":
            try:
                client.chat_postEphemeral(channel=channel_id, user=actor_id, text=msg)
            except Exception:  # noqa: BLE001
                pass
            return

        # Terminal outcomes drop the buttons. RETRYABLE refusals leave the
        # stash staged, so the card must stay tappable AND keep its typed-SEND
        # context line -- otherwise a transient Gmail blip strands an approved
        # nudge until the 48h expiry (D-051 lens 1/2).
        keep_buttons = outcome in (
            "env_off", "thread_verify_failed", "tier_denied", "mailbox_denied", "error",
        )
        if channel_id and message_ts:
            orig = (body.get("message") or {}).get("blocks") or []
            kept = [
                b for b in orig
                if b.get("type") == "section"
                or (keep_buttons and b.get("type") in ("actions", "context"))
            ]
            new_blocks = kept + [
                {"type": "context", "elements": [{"type": "mrkdwn", "text": msg}]}
            ]
            if not kept:
                new_blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": msg}}]
            try:
                client.chat_update(channel=channel_id, ts=message_ts, text=msg, blocks=new_blocks)
            except Exception as exc:  # noqa: BLE001
                log.warning("revops send-card chat_update failed: %s", exc)
    except Exception:  # noqa: BLE001 -- a handler error must never crash the bot
        log.warning("revops send-card handler error (non-fatal)", exc_info=True)


@app.action(revops_cards.ACTION_SEND)
def handle_revops_send(ack, body, client) -> None:
    ack()
    _handle_revops_send_tap(body, client, action="send")


@app.action(revops_cards.ACTION_SKIP)
def handle_revops_skip(ack, body, client) -> None:
    ack()
    _handle_revops_send_tap(body, client, action="skip")


@app.action(revops_cards.ACTION_CLOSE)
def handle_revops_close(ack, body, client) -> None:
    ack()
    _handle_revops_send_tap(body, client, action="close")


@app.action(revops_cards.ACTION_EDIT)
def handle_revops_edit(ack, body, client) -> None:
    """Open a modal prefilled with the staged body; submit restages a NEW card."""
    ack()
    try:
        actions = body.get("actions") or []
        sid = (actions[0].get("value") if actions else "") or ""
        actor_id = (body.get("user") or {}).get("id", "")
        channel_id = (body.get("channel") or {}).get("id", "")
        message_ts = (body.get("message") or {}).get("ts", "")
        trigger_id = body.get("trigger_id", "")

        from .revops import ledger as revops_ledger, stash as revops_stash

        conn = revops_ledger.connect()
        try:
            row = revops_stash.get_stash(conn, sid)
        finally:
            conn.close()
        if (
            row is None
            or row["status"] != "staged"
            or not revops_cards.send_trust.is_approver(row["playbook_id"], actor_id)
        ):
            try:
                client.chat_postEphemeral(
                    channel=channel_id, user=actor_id,
                    text="That send card is no longer editable.",
                )
            except Exception:  # noqa: BLE001
                pass
            return
        view = revops_cards.edit_modal_view(sid, channel_id, message_ts, row["body_text"] or "")
        client.views_open(trigger_id=trigger_id, view=view)
    except Exception:  # noqa: BLE001
        log.warning("revops edit-modal open failed (non-fatal)", exc_info=True)


@app.view(revops_cards.VIEW_EDIT_SUBMIT)
def handle_revops_edit_submit(ack, body, client, view) -> None:
    ack()
    try:
        meta = json.loads(view.get("private_metadata") or "{}")
        sid = meta.get("stash_id", "")
        dm_channel = meta.get("dm_channel", "")
        dm_ts = meta.get("dm_ts", "")
        actor_id = (body.get("user") or {}).get("id", "")
        state = (view.get("state") or {}).get("values") or {}
        edited = (
            (state.get("revops_edit_block") or {})
            .get("revops_edit_input", {})
            .get("value", "")
        ) or ""

        outcome, msg, new_sid = revops_cards.restage_with_edit(sid, actor_id, edited)

        if outcome == "restaged" and new_sid:
            from .revops import email_egress_guard as revops_guard
            from .revops import ledger as revops_ledger, stash as revops_stash

            conn = revops_ledger.connect()
            try:
                new_row = revops_stash.get_stash(conn, new_sid)
                thread_row = (
                    revops_ledger.get_thread(conn, new_row["thread_key"]) if new_row else None
                )
                if new_row is not None:
                    guard = revops_guard.check_email(
                        new_row["body_text"] or "",
                        workstream=thread_row["workstream"] if thread_row else None,
                        entity=thread_row["entity"] if thread_row else None,
                    )
                    fallback, blocks = revops_cards.build_send_card(new_row, thread_row, guard)
                    resp = client.chat_postMessage(
                        channel=dm_channel or actor_id, text=fallback[:2900],
                        blocks=blocks, unfurl_links=False,
                    )
                    revops_stash.set_card_ref(
                        conn, new_sid, channel=resp["channel"], ts=resp["ts"]
                    )
            finally:
                conn.close()

        if dm_channel and dm_ts and outcome != "not_authorized":
            try:
                client.chat_update(
                    channel=dm_channel, ts=dm_ts, text=msg,
                    blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": msg}}],
                )
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        log.warning("revops edit-submit handler error (non-fatal)", exc_info=True)


# ── Code-session queue one-tap (Queue / Edit / Dismiss / Later / Stage / etc.) ───
# Mirrors the knowledge/catchup one-tap contract: ack() first, delegate; ALL
# correctness (Harrison gate, idempotency, apply-then-record) lives in
# code_queue.process_queue_action / stage_bundle / apply_edit. A single-item card
# is its OWN message -> chat_update drops its actions block; the Monday menu is ONE
# message with MANY actions blocks -> a threaded reply keeps the others tappable.

def _cq_ack_in_message(client, body: dict, msg: str) -> None:
    channel_id = (body.get("channel") or {}).get("id", "")
    message_ts = (body.get("message") or {}).get("ts", "")
    if not (channel_id and message_ts):
        return
    blocks = (body.get("message") or {}).get("blocks") or []
    actions_blocks = [b for b in blocks if b.get("type") == "actions"]
    try:
        if len(actions_blocks) > 1:
            client.chat_postMessage(channel=channel_id, thread_ts=message_ts, text=msg,
                                    unfurl_links=False, unfurl_media=False)
        else:
            section_blocks = [b for b in blocks if b.get("type") == "section"]
            new_blocks = section_blocks + [
                {"type": "context", "elements": [{"type": "mrkdwn", "text": msg}]}]
            if not section_blocks:
                new_blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": msg}}]
            client.chat_update(channel=channel_id, ts=message_ts, text=msg, blocks=new_blocks)
    except Exception as exc:  # noqa: BLE001
        log.warning("code-queue ack update failed: %s", exc)


def _handle_code_queue_button(body: dict, client, action_id: str) -> None:
    try:
        actions = body.get("actions") or []
        value = (actions[0].get("value") if actions else "") or ""
        actor_id = (body.get("user") or {}).get("id", "")
        channel_id = (body.get("channel") or {}).get("id", "")
        if action_id == code_queue.ACTION_STAGE and value.startswith("bundle:"):
            outcome, msg = code_queue.stage_bundle(value, actor_id)
        else:
            outcome, msg = code_queue.process_queue_action(action_id, value, actor_id)
        if outcome == "not_authorized":
            try:
                client.chat_postEphemeral(channel=channel_id, user=actor_id, text=msg)
            except Exception:  # noqa: BLE001
                pass
            return
        _cq_ack_in_message(client, body, msg)
    except Exception:  # noqa: BLE001 -- a handler error must never crash the bot
        log.warning("code-queue button handler error (non-fatal)", exc_info=True)


# ── Delegated work HELD-card one-tap (Release / Dismiss) ────────────────────
# Same contract as the code-queue buttons: ack() first, delegate; ALL
# correctness (Harrison gate, idempotency) lives in delegated_work.
# process_job_action. This wrapper is Slack I/O only.

def _handle_delegated_work_button(body: dict, client, action_id: str) -> None:
    try:
        actions = body.get("actions") or []
        value = (actions[0].get("value") if actions else "") or ""
        actor_id = (body.get("user") or {}).get("id", "")
        channel_id = (body.get("channel") or {}).get("id", "")
        outcome, msg = delegated_work.process_job_action(action_id, value, actor_id)
        if outcome == "not_authorized":
            try:
                client.chat_postEphemeral(channel=channel_id, user=actor_id, text=msg)
            except Exception:  # noqa: BLE001
                pass
            return
        _cq_ack_in_message(client, body, msg)
    except Exception:  # noqa: BLE001 -- a handler error must never crash the bot
        log.warning("delegated-work button handler error (non-fatal)", exc_info=True)


@app.action(delegated_work.ACTION_RELEASE)
def handle_dw_release(ack, body, client) -> None:
    ack()
    _handle_delegated_work_button(body, client, delegated_work.ACTION_RELEASE)


@app.action(delegated_work.ACTION_DISMISS)
def handle_dw_dismiss(ack, body, client) -> None:
    ack()
    _handle_delegated_work_button(body, client, delegated_work.ACTION_DISMISS)


@app.action(code_queue.ACTION_APPROVE)
def handle_cq_approve(ack, body, client) -> None:
    ack()
    _handle_code_queue_button(body, client, code_queue.ACTION_APPROVE)


@app.action(code_queue.ACTION_DISMISS)
def handle_cq_dismiss(ack, body, client) -> None:
    ack()
    _handle_code_queue_button(body, client, code_queue.ACTION_DISMISS)


@app.action(code_queue.ACTION_LATER)
def handle_cq_later(ack, body, client) -> None:
    ack()
    _handle_code_queue_button(body, client, code_queue.ACTION_LATER)


@app.action(code_queue.ACTION_STAGE)
def handle_cq_stage(ack, body, client) -> None:
    ack()
    _handle_code_queue_button(body, client, code_queue.ACTION_STAGE)


@app.action(code_queue.ACTION_MARK_SHIPPED)
def handle_cq_shipped(ack, body, client) -> None:
    ack()
    _handle_code_queue_button(body, client, code_queue.ACTION_MARK_SHIPPED)


@app.action(code_queue.ACTION_KEEP)
def handle_cq_keep(ack, body, client) -> None:
    ack()
    _handle_code_queue_button(body, client, code_queue.ACTION_KEEP)


@app.action(code_queue.ACTION_EDIT)
def handle_cq_edit(ack, body, client) -> None:
    """Open a modal prefilled with the item's title + summary."""
    ack()
    try:
        actions = body.get("actions") or []
        cq_id = (actions[0].get("value") if actions else "") or ""
        actor_id = (body.get("user") or {}).get("id", "")
        channel_id = (body.get("channel") or {}).get("id", "")
        message_ts = (body.get("message") or {}).get("ts", "")
        trigger_id = body.get("trigger_id", "")
        if actor_id != code_queue.HARRISON_ID:
            try:
                client.chat_postEphemeral(channel=channel_id, user=actor_id,
                                          text="Only Harrison can edit queue items.")
            except Exception:  # noqa: BLE001
                pass
            return
        view = code_queue.edit_modal_view(cq_id, channel_id, message_ts)
        client.views_open(trigger_id=trigger_id, view=view)
    except Exception:  # noqa: BLE001
        log.warning("code-queue edit-modal open failed (non-fatal)", exc_info=True)


@app.view(code_queue.VIEW_EDIT_SUBMIT)
def handle_cq_edit_submit(ack, body, client, view) -> None:
    ack()
    try:
        meta = json.loads(view.get("private_metadata") or "{}")
        cq_id = meta.get("cq_id", "")
        dm_channel = meta.get("dm_channel", "")
        dm_ts = meta.get("dm_ts", "")
        actor_id = (body.get("user") or {}).get("id", "")
        state = (view.get("state") or {}).get("values") or {}
        title = ((state.get("cq_title") or {}).get("v", {}) or {}).get("value", "") or ""
        summary = ((state.get("cq_summary") or {}).get("v", {}) or {}).get("value", "") or ""
        outcome, _msg = code_queue.apply_edit(cq_id, actor_id, title, summary)
        if dm_channel and dm_ts and outcome == "edited":
            rec = code_queue.get_item(cq_id)
            if rec:
                text, blocks = code_queue.build_item_card(rec)
                try:
                    client.chat_update(channel=dm_channel, ts=dm_ts, text=text, blocks=blocks)
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        log.warning("code-queue edit-submit handler error (non-fatal)", exc_info=True)


def _edit_card_terminal(client, channel_id: str, message_ts: str, orig_blocks: list[dict],
                        outcome_text: str) -> None:
    """Edit a confirm/picker card to a terminal state: keep the original
    preview/question section(s), append the outcome, drop the actions block
    (buttons gone -- revops/code-queue precedent).

    Callers MUST be the unique claim winner for this message (S2 fix,
    live-smoke 2026-08-02 + D-051 re-review): a same-card race-LOSER outcome
    (already_handled for Confirm/Cancel, superseded for the picker) must
    never call this -- it should go ephemeral-only instead, since there is no
    way to order two independent chat_update calls against each other, and
    the loser's edit is never necessary (the winner always edits the card
    with the real outcome)."""
    section_blocks = [b for b in orig_blocks if b.get("type") == "section"]
    new_blocks = section_blocks + confirm_cards.terminal_blocks(outcome_text)
    try:
        client.chat_update(channel=channel_id, ts=message_ts, text=outcome_text, blocks=new_blocks)
    except Exception:  # noqa: BLE001
        log.warning("confirm-button card edit failed (non-fatal) channel=%s ts=%s",
                    channel_id, message_ts, exc_info=True)


def _post_followup_confirm_card(client, channel_id: str, text: str, stash_id: str) -> None:
    """Post a NEW Confirm/Cancel card for a freshly-minted stash_id. Shared by
    the picker's pick -> preview hand-off AND a confirm-tap whose own execute
    declined to write and re-stashed a fresh preview instead (D-051 review:
    both cases need the SAME "a fresh stash appeared, give it a card" step)."""
    blocks = confirm_cards.build_confirm_blocks(text, stash_id)
    try:
        client.chat_postMessage(channel=channel_id, text=text, blocks=blocks)
    except Exception:  # noqa: BLE001
        log.warning("confirm-button follow-up card post failed (non-fatal) channel=%s",
                    channel_id, exc_info=True)


def _handle_confirm_tap(body: dict, client, *, action: str) -> None:
    """Shared Confirm/Cancel tap handler for all 9 stash kinds (S2, design
    2026-08-02). Requester-only, atomic claim, honest terminal states for
    every lifecycle case (superseded/expired/orphaned/indeterminate/already-
    handled) -- see tool_dispatch.resolve_and_claim_stash for the security
    invariants."""
    tapping_user = (body.get("user") or {}).get("id", "")
    channel_id = (body.get("channel") or {}).get("id", "")
    message_ts = (body.get("message") or {}).get("ts", "")
    orig_blocks = (body.get("message") or {}).get("blocks") or []
    actions = body.get("actions") or [{}]
    stash_id = str(actions[0].get("value") or "")

    if not confirm_cards.confirm_buttons_enabled():
        # Kill switch: a stale card tapped after a flag flip to off. Never
        # mutate the card (its stash may still be live for the typed path) --
        # just redirect the tapper to the typed path.
        if tapping_user and channel_id:
            try:
                client.chat_postEphemeral(
                    channel=channel_id, user=tapping_user,
                    text="Buttons are turned off right now -- reply with a typed confirm instead.")
            except Exception:  # noqa: BLE001
                pass
        return

    if not stash_id or not tapping_user or not channel_id or not message_ts:
        return

    # D-051 review (defense-in-depth): a re-preview minted below (e.g. Shopify
    # drift) is attached via its EXPLICIT returned stash_id, never through
    # freshest_changed_stash's turn-ownership diff, so this doesn't change
    # behavior today -- but Bolt's socket-mode transport reuses worker
    # threads across unrelated events, and confirm_cards._TURN_ID is a plain
    # per-thread contextvar (not scoped via Context.run() here), so without
    # this a reused thread could carry a stale turn_id from whatever
    # _dispatch_qa turn last ran on it. Establishing a fresh scope here makes
    # the invariant "a mint's turn_id reflects ITS OWN triggering event" true
    # by construction rather than true-by-coincidence.
    confirm_cards.begin_turn()
    result = _tool_dispatch.resolve_and_claim_stash(stash_id, tapping_user, action)
    outcome = result.get("outcome")

    if outcome == "unauthorized":
        owner = result.get("owner", "")
        owner_disp = f"<@{owner}>" if owner else "the requester"
        try:
            client.chat_postEphemeral(channel=channel_id, user=tapping_user,
                                      text=f"Only {owner_disp} can act on this.")
        except Exception:  # noqa: BLE001
            pass
        return

    if outcome == "orphaned":
        try:
            client.chat_postEphemeral(
                channel=channel_id, user=tapping_user,
                text=("I don't have a record of that request anymore (maybe I "
                      "restarted) -- ask again if you still want this."))
        except Exception:  # noqa: BLE001
            pass
        return

    if outcome == "already_handled":
        # S2 fix (REDESIGNED, D-051 review 2026-08-02): a racing SECOND tap on
        # the same card always resolves to already_handled -- but the winner's
        # own edit can be arbitrarily slower than this one (it does real work
        # in _execute_claimed_stash first; this path is a fast lock-miss with
        # no I/O). A "claim a slot, whoever writes first wins" scheme was tried
        # and REJECTED: it only blocks the clobber in ONE arrival order (an
        # informative claim already recorded before this one checks) -- if
        # already_handled's OWN chat_update fires first (the common case,
        # since it is faster) and the winner's slower edit arrives after, BOTH
        # calls still fire and the outcome depends on whichever chat_update
        # Slack applies last, which is exactly the original clobber. The only
        # actually race-free fix is for already_handled to NEVER touch the
        # shared card at all -- there is always a legitimate winner (that's
        # the definition of already_handled) who will edit it with the real
        # outcome, so nothing is lost by leaving the card alone here.
        try:
            client.chat_postEphemeral(channel=channel_id, user=tapping_user,
                                      text="Already handled -- no action needed.")
        except Exception:  # noqa: BLE001
            pass
        return

    if outcome == "re_previewed":
        # The claim succeeded but execute itself declined to write and
        # re-stashed a FRESH preview instead (e.g. Shopify live-inventory
        # drift/floor-guard detected between preview and confirm) -- close
        # THIS card honestly, then post a NEW Confirm/Cancel card for the
        # fresh stash so the user isn't left with a dead-looking card and an
        # un-actionable pending underneath it (D-051 review finding). This
        # outcome is only ever reached by the unique claim winner (see
        # resolve_and_claim_stash), so it can never race another edit of the
        # SAME message_ts.
        text = result.get("message") or "The count moved -- here's an updated preview."
        _edit_card_terminal(client, channel_id, message_ts, orig_blocks, text)
        fresh_stash_id = result.get("stash_id")
        if fresh_stash_id:
            _post_followup_confirm_card(client, channel_id, text, fresh_stash_id)
        return

    if outcome == "superseded":
        text = "This preview was replaced by a newer one -- check your latest message."
    elif outcome == "expired":
        label = result.get("label", "that request")
        text = (f"That {label} expired before you confirmed. Nothing was "
                f"changed -- tell me again and I'll re-preview it.")
    elif outcome == "cancelled":
        text = "Cancelled -- nothing was changed."
    elif outcome == "indeterminate":
        text = ("Something may have gone through, but I hit an error right "
                "after -- I can't confirm either way. Check before retrying; "
                "this card is closed and will not retry.")
    elif outcome == "executed":
        text = result.get("message") or "Done."
    else:
        text = "Something went wrong -- nothing was changed."

    # Every outcome reaching here (superseded/expired/cancelled/indeterminate/
    # executed/unrecognized) is the UNIQUE claim winner's own result -- at most
    # one caller ever reaches "claimed" for a given stash_id (the lock inside
    # _claim_stash_by_id), so none of these can race another edit of the SAME
    # message_ts. already_handled is handled above and never reaches here.
    _edit_card_terminal(client, channel_id, message_ts, orig_blocks, text)


@app.action(confirm_cards.ACTION_CONFIRM)
def handle_confirm_write(ack, body, client) -> None:
    ack()
    _handle_confirm_tap(body, client, action="confirm")


@app.action(confirm_cards.ACTION_CANCEL)
def handle_cancel_write(ack, body, client) -> None:
    ack()
    _handle_confirm_tap(body, client, action="cancel")


def _handle_pick_tap(body: dict, client) -> None:
    """Ambiguity-picker tap (S4): bind the chosen candidate server-side and
    post the resulting preview as a fresh Confirm/Cancel card -- the term
    never round-trips through the model."""
    tapping_user = (body.get("user") or {}).get("id", "")
    channel_id = (body.get("channel") or {}).get("id", "")
    message_ts = (body.get("message") or {}).get("ts", "")
    orig_blocks = (body.get("message") or {}).get("blocks") or []
    actions = body.get("actions") or [{}]
    raw_value = str(actions[0].get("value") or "")

    if not confirm_cards.confirm_buttons_enabled():
        if tapping_user and channel_id:
            try:
                client.chat_postEphemeral(
                    channel=channel_id, user=tapping_user,
                    text="Buttons are turned off right now -- reply with the option name instead.")
            except Exception:  # noqa: BLE001
                pass
        return

    if ":" not in raw_value or not tapping_user or not channel_id or not message_ts:
        return
    ask_id, candidate_key = raw_value.split(":", 1)

    # D-051 review (defense-in-depth): see the matching comment in
    # _handle_confirm_tap -- the fresh preview this may mint is attached via
    # its EXPLICIT stash_id, never the turn-ownership diff, so this is
    # belt-and-suspenders against thread-reuse carrying a stale turn_id.
    confirm_cards.begin_turn()
    outcome, message, stash_id = _tool_dispatch.resolve_shopify_ask_pick(
        ask_id, tapping_user, candidate_key)

    if outcome == "unauthorized":
        owner_disp = f"<@{message}>" if message else "the requester"
        try:
            client.chat_postEphemeral(channel=channel_id, user=tapping_user,
                                      text=f"Only {owner_disp} can act on this.")
        except Exception:  # noqa: BLE001
            pass
        return
    if outcome == "orphaned":
        try:
            client.chat_postEphemeral(
                channel=channel_id, user=tapping_user,
                text="I don't have a record of that question anymore -- ask again.")
        except Exception:  # noqa: BLE001
            pass
        return
    if outcome == "superseded":
        # D-051 re-review finding: resolve_shopify_ask_pick's atomic claim
        # (_take_pending_ask) means "superseded" can be a SAME-card race loser
        # (a second tap on the exact ask_id the winner just claimed), not only
        # a genuinely-different newer ask replacing a stale one -- same
        # fast-loser/slow-winner shape as _handle_confirm_tap's
        # already_handled, so it must never edit the shared card either
        # (ephemeral-only; the winner's own edit, if any, is authoritative).
        try:
            client.chat_postEphemeral(
                channel=channel_id, user=tapping_user,
                text="This question was replaced by a newer one -- check your latest message.")
        except Exception:  # noqa: BLE001
            pass
        return
    if outcome == "invalid_candidate":
        # Only reachable AFTER this caller's OWN atomic claim already
        # succeeded (unique per ask_id) -- cannot race another edit of the
        # SAME message_ts, so a direct card edit is safe here.
        _edit_card_terminal(client, channel_id, message_ts, orig_blocks,
                            "That option didn't resolve cleanly -- restate the item and I'll ask again.")
        return

    # outcome == "preview": close the picker card, then post the fresh
    # preview as ITS OWN Confirm/Cancel card (pick -> preview -> confirm). A
    # refusal from the resolution tail (e.g. "not stocked here anymore") has
    # no stash_id -- nothing to confirm -- so it posts as plain text instead.
    _edit_card_terminal(client, channel_id, message_ts, orig_blocks, "Picked.")
    clean_text = message or ""
    if stash_id:
        _post_followup_confirm_card(client, channel_id, clean_text, stash_id)
    else:
        try:
            client.chat_postMessage(channel=channel_id, text=clean_text)
        except Exception:  # noqa: BLE001
            log.warning("picker follow-up text post failed (non-fatal) channel=%s",
                        channel_id, exc_info=True)


@app.action(confirm_cards.ACTION_PICK)
def handle_pick_candidate(ack, body, client) -> None:
    ack()
    _handle_pick_tap(body, client)


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
