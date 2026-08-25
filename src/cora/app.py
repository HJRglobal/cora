"""Bolt app and event handlers."""

import json
import logging
import os
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
from . import briefing_enrollment
from .connectors import hubspot_email_sync
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
from . import knowledge_check
from . import code_queue
from . import delegated_work
from .knowledge_base import embeddings as kb_embeddings
from . import review_lanes
from . import sibling_guard
from . import cross_entity_guard
from . import info_intake
from . import qa_scaffolding
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


# The trailing [,:] is load-bearing, not tidiness (D-051 lens-5 HIGH,
# 2026-08-20). Slack's autocomplete inserts a mention pill and people then
# type their own comma: "@Cora, remember the vendor is Apex". Without it this
# strip left ", remember ...", and EVERY start-anchored staged-write detector
# in this module then failed exactly as it did for the unstripped DM token --
# measured: `_staged_write_force_tool` returns None and
# `_remember_or_forget_intent` returns False for ", remember the vendor is
# Apex Appliance", and both fire once the comma is gone. The DM fix below
# would otherwise have left the identical defect live in every CHANNEL, which
# is the half its own comment wrongly claimed parity with.
_MENTION_RE = re.compile(r"^<@[A-Z0-9]+>\s*[,:]?\s*")
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


# Explicit "hand this to the background worker" command. Deliberately narrow:
# a delegation VERB aimed at Cora, or a named job archetype.
_DELEGATE_INTENT_RE = re.compile(
    r"(?:"
    r"\bdelegate\s+(?:a|this|that|the)?\s*(?:job|task|work|brief|research)\b"
    r"|\bdelegate\s*:"
    # "background" is MANDATORY: "run a job costing analysis" is not a request
    # to run a background job (D-051 HIGH-1).
    r"|\brun\s+(?:a|this)\s+background\s+job\b"
    r"|\b(?:queue|kick\s+off|start|spin\s+up)\s+(?:a|the)\s+"
    r"(?:research\s+brief|doc(?:ument)?\s+draft|background\s+job)\b"
    # NOTE the bare "research brief on ..." alternative was REMOVED. It matched
    # every message that merely DISCUSSED a brief -- "where's the research brief
    # on Sprouts?", "what did the research brief on F3 retail conclude?" -- and
    # because cora_delegate_work is a contract-write tool its string is posted
    # VERBATIM over the model, so the user's actual question was destroyed and
    # replaced by "Unknown job archetype ''". The queue/kick-off/start/spin-up
    # branch above already covers the genuine imperative.
    r")",
    re.IGNORECASE,
)
# An interrogative is a question ABOUT delegation, not a delegation. Mirrors the
# bail _asana_destructive_intent has had since F-23 Slice 2 -- its absence here
# is what let "how do I delegate a task in Asana" force a job preview.
_DELEGATE_INTERROGATIVE_RE = re.compile(
    r"^\s*(?:@?\w+[,:]?\s+)?(?:"
    # Informational openers -- a question ABOUT delegation.
    r"(?:how|what|where|which|who|whose|when|why|do|does|did|is|are|was|were"
    r"|have|has|any)\b"
    # Modals are only interrogative when NOT aimed at Cora: "can you run a
    # background job" is a polite IMPERATIVE and must still force, while
    # "can I delegate a task in Asana" is a question.
    r"|(?:can|could|should|would|will)\s+(?!you\b|u\b)"
    r")",
    re.IGNORECASE,
)
# "delegate that to Shaun" is about a HUMAN, not the worker. Anchored to the text
# AFTER the delegation verb. The object list is deliberately NEGATIVE-scoped:
# "delegate a job TO YOU / to Cora / to the background worker" are the most
# explicit statements of the intent this detector exists to catch, and an
# any-capitalised-word rule sent all three back to the path that produced the
# live defect (D-051 MED-1).
_DELEGATE_TO_HUMAN_RE = re.compile(
    r"^\s*(?:it|this|that)?\s*to\s+"
    r"(?!(?:you|yourself|cora|the\s+(?:background\s+)?worker|the\s+bot)\b)"
    r"[A-Z@]",
    re.IGNORECASE,
)


def _delegate_work_intent(text: str) -> bool:
    """True for an EXPLICIT delegated-work command, so tool_choice can force
    cora_delegate_work.

    Why forcing is safe here, on the same reasoning that made forcing
    cora_queue_code_session safe: `action=request` FILES NOTHING -- it runs the
    intake screens, then returns a preview and stashes server-side. A false
    positive costs one dismissable preview, never a data write. The tool is in
    _GLOBAL_CORE_TOOLS (exposed in every entity + DMs), so tool_choice can never
    name an unexposed tool.

    Why it is needed (cq-d30815ee6993, live 2026-08-07): a LEX-TS ask reading
    "delegate a job: research brief on <person>'s AHCCCS eligibility renewal
    status" produced a job PREVIEW with no cora_delegate_work call anywhere in
    the log -- so the intake screen never ran at all. The screen was correct;
    it was simply never reached. A model that narrates a preview instead of
    calling the tool bypasses every deterministic guard behind it, which is
    exactly the surface a forced tool closes.
    """
    t = (text or "").strip()
    if not t or _DELEGATE_INTERROGATIVE_RE.match(t):
        return False
    m = _DELEGATE_INTENT_RE.search(t)
    if not m:
        return False
    if _DELEGATE_TO_HUMAN_RE.match(t[m.end():]):
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
# Optional leading vocative (v2 S3). A DM carries no <@Uxxx> token for
# _MENTION_RE to strip, so people simply type "Cora, remember ..." -- which is
# precisely the live cq-67490abe2d86 phrasing the bare ^remember anchor missed.
# Kept anchored: "remember" mid-sentence still does not count as a command.
#
# D-051 lens-3 HIGH: the first cut wrote `@?cora\s*[,:]?\s+` -- two overlapping
# whitespace quantifiers with an optional element between them, which is
# quadratic on a FAILING match. Measured on this machine: "cora" + 40,000 spaces
# + "x" took 43 SECONDS per regex, and _remember_or_forget_intent runs two of
# them. CPython's re does not release the GIL, so that burn blocks the whole bot
# process -- heartbeat, Socket Mode acks, every other user's turn -- and this
# predicate is evaluated eagerly on EVERY DM, upstream of the rate limiter. The
# bounded {0,2}/{1,4} repeats remove the overlap; a vocative separated from the
# verb by more than a few spaces is not a real message.
_VOCATIVE = r"(?:(?:hey|hi|ok|okay)\s+)?@?cora\s{0,2}[,:]?\s{1,4}"
_REMEMBER_INTENT_RE = re.compile(
    rf"^\s*(?:{_VOCATIVE})?(?:please\s+)?remember\b"
    rf"|^\s*(?:{_VOCATIVE})?(?:please\s+)?note\s+that\b"
    rf"|^\s*(?:{_VOCATIVE})?make\s+a\s+note\b",
    re.IGNORECASE,
)
_FORGET_NOTE_INTENT_RE = re.compile(
    rf"^\s*(?:{_VOCATIVE})?(?:please\s+)?forget\b[^.\n]{{0,40}}\bnote\b"
    rf"|^\s*(?:{_VOCATIVE})?(?:please\s+)?(?:delete|remove)\b[^.\n]{{0,40}}\bnote\b",
    re.IGNORECASE,
)


def _remember_or_forget_intent(text: str) -> bool:
    """True for a clear imperative 'remember'/'forget note' command -- used for
    the Sonnet escalation, which covers BOTH verbs (see the module comment
    above for why only 'remember' is safe to FORCE)."""
    t = (text or "").strip()
    if not t or "?" in t:
        return False
    return bool(_REMEMBER_INTENT_RE.search(t) or _FORGET_NOTE_INTENT_RE.search(t))


# ── Phantom-preview force: staged-write intents (S6 rider, cq-904f849bc59a) ──
#
# THE DEFECT. The 8/9 acceptance battery proved the model narrates a
# preview-shaped reply with ZERO tool_use across cora_remember, slack_send_dm
# (twice, including an explicit tool-name ask on haiku) and gmail_create_draft.
# No tool call means no server-side stash, so there is nothing for a confirm --
# typed or tapped -- to execute, and every deterministic guard behind the tool
# is bypassed. Escalating to Sonnet was the S4 mitigation and it is NOT enough:
# the battery caught Sonnet doing it too. The fix is the same one the 8/7 DW
# intake used for cq-d30815ee6993 -- force the tool via tool_choice on the first
# model turn, so the preview is produced BY the tool or not at all.
#
# WHY FORCING THESE FOUR IS SAFE, on the reasoning that made forcing
# cora_queue_code_session and cora_delegate_work safe:
#   * all four are staged writes -- an unconfirmed first call FILES NOTHING, it
#     validates, stashes server-side and returns a preview. A false positive
#     costs one dismissable preview, never a data write;
#   * all four are in _GLOBAL_CORE_TOOLS, exposed in every entity and in DMs, so
#     tool_choice can never name an unexposed tool (a forced name that is not in
#     the turn's tool list is silently dropped by _apply_forced_tool anyway);
#   * cora_forget_note is deliberately NOT forced -- it needs a note_id the model
#     can only have after a prior cora_my_notes call, so forcing it blind would
#     produce a nonsensical call. It keeps the Sonnet escalation only.
#
# THE REAL COST IS DISPLACEMENT, NOT NOISE (D-158): the DW force's first cut
# stole an Asana DELETE, and a stolen turn is worse than a dismissable card --
# the user's actual request never happens. So every branch here is
# START-ANCHORED after the mention strip, excludes interrogatives outright, and
# requires an explicit object. The measured safe-set lives in
# tests/test_phantom_preview_force.py: every candidate string is run through the
# REAL function against must-force / must-not-force expectations, including the
# existing detectors' own positives, so a new branch that steals an Asana,
# code-queue or delegate turn fails the suite (D-169).
#
# All bounded-quantifier by construction and timed on a 40k input in that same
# file -- three self-inflicted ReDoS in this arc were all found by review, not
# tests (D-165).

# Reflexive/broadcast objects: "message me the numbers" is a request TO Cora,
# not a DM to a teammate, and "dm everyone" is not a single-recipient send.
_DM_NOT_A_RECIPIENT = (
    r"(?!(?i:me|us|myself|everyone|everybody|all|here|them|him|her|you|"
    r"the\s+team|the\s+channel)\b)"
)
# D-051 lens-5 HIGH (2026-08-09): the first cut tested the object with a bare
# `\S`, so "Slack is down for me right now", "Slack channel health monitor keeps
# firing", "slack messages are not syncing to the KB" and "DM notifications are
# broken" ALL forced slack_send_dm -- the D-158 stolen-turn class, reintroduced
# by the very branch whose header comment claims to have closed it, and invisible
# because the safe-set carried no noun-"Slack"/noun-"DM" case. A forced tool
# REPLACES the answer, so the usual "a false positive costs one dismissable
# preview" argument does not apply here.
#
# The object must now LOOK LIKE A PERSON: a Slack mention, an @handle, or a
# Capitalised name. That requires case sensitivity, so this pattern carries NO
# re.IGNORECASE -- the verb casings are enumerated instead, and the stopword
# lookahead uses an inline (?i:...) group.
# The \b belongs only on the bare-name forms: a mention ends in '>', which is a
# non-word char, so a trailing \b after it never matches before a space (the
# first cut silently missed every "DM <@U...> ..." -- the exact live phrasing).
_DM_RECIPIENT = (
    r"(?:<@[A-Z0-9]+>|(?:@[A-Za-z][\w.\-]{0,30}|[A-Z][a-z]{1,20})\b)"
)
_VOCATIVE_CS = r"(?:(?:[Hh]ey|[Hh]i|[Oo]k|[Oo]kay)\s+)?@?[Cc]ora\s{0,2}[,:]?\s{1,4}"
_SLACK_DM_INTENT_RE = re.compile(
    rf"^\s*(?:{_VOCATIVE_CS})?(?:[Pp]lease\s+)?(?:[Dd][Mm]|[Ss]lack)\s+"
    rf"{_DM_NOT_A_RECIPIENT}{_DM_RECIPIENT}"
    rf"|^\s*(?:{_VOCATIVE_CS})?(?:[Pp]lease\s+)?[Ss]end\s+(?:an?\s+)?"
    rf"(?:[Dd][Mm]|[Ss]lack\s+message|message)\s+to\s+"
    rf"{_DM_NOT_A_RECIPIENT}{_DM_RECIPIENT}",
)
# The email NOUN is mandatory, AND it must be an email TO someone. D-051 lens-3
# MED-4: without a recipient this took copy-writing turns -- "write an email
# signature block for Justin", "prepare an email summary of the board deck",
# "compose an email subject line for the campaign" -- where the model then has no
# `to` and Cora answers a copy request with "who is the recipient?". A drafting
# request that names no recipient is not yet a draft request.
_GMAIL_DRAFT_INTENT_RE = re.compile(
    rf"^\s*(?:{_VOCATIVE})?(?:please\s+)?(?:draft|compose|write|prepare)\s+"
    r"(?:me\s+)?(?:an?|the|a\s+quick)\s+(?:email|e-mail)\s+"
    r"(?:back\s+)?to\b"
    rf"|^\s*(?:{_VOCATIVE})?(?:please\s+)?(?:draft|compose)\s+(?:an?|the)\s+"
    r"(?:reply|response)\s+to\b[^.\n]{0,40}\b(?:email|e-mail|thread)\b",
    re.IGNORECASE,
)
# Teaching a shared term, as opposed to saving a personal note. Anchored like
# the rest: "create a task to add the SKU to the lexicon" is a TASK request and
# must keep reaching asana_create_task, which an unanchored branch would steal.
#
# D-051 lens-3 MED-2: anchoring alone was NOT enough. "add a comment to the
# glossary task" / "add a note to the vocabulary task" start with `add`, so they
# matched and stole a correct asana_add_comment force -- the very displacement
# the comment above claimed to have closed. The glossary noun must be the DIRECT
# object, so an intervening object noun (task/comment/note/doc/deck/...) between
# the verb and "to the lexicon" disqualifies the match. Latent today
# (CORA_LEXICON=resolve gates the force off) but it would arm on the flag flip.
_LEXICON_OBJECT_BLOCKER = (
    r"(?:tasks?|to-?dos?|comments?|notes?|subtasks?|docs?|documents?|"
    r"decks?|sections?|tabs?|folders?|sheets?|pages?|channels?|threads?)"
)
_LEXICON_TEACH_INTENT_RE = re.compile(
    rf"^\s*(?:{_VOCATIVE})?(?:please\s+)?(?:add|save|record|teach)\b"
    rf"(?:(?!\b{_LEXICON_OBJECT_BLOCKER}\b)[^.\n]){{0,40}}"
    r"\bto\s+(?:the\s+|our\s+|your\s+)?"
    r"(?:lexicon|glossary|vocabulary|dictionary)\b"
    # ...and the glossary word must not itself be modifying a DOCUMENT: "add
    # the definitions to the glossary doc in Drive" / "add these to the
    # glossary section of the deck" are file edits, not lexicon teaches.
    r"(?!\s+(?:docs?|documents?|sections?|tabs?|pages?|sheets?|files?|"
    r"folders?|decks?|channels?)\b)"
    rf"|^\s*(?:{_VOCATIVE})?(?:please\s+)?(?:the\s+)?"
    r"(?:term|word|acronym|abbreviation)\s+"
    # "the term sheet means we are past LOI" is a business statement, not a
    # definition -- the head noun must not itself be a document (lens-3 MED-2).
    r"(?!sheets?\b)[^.\n]{1,60}?\bmeans\b",
    re.IGNORECASE,
)
# A trailing request clause turns a "remember"/"note that" opener into a
# discourse marker rather than a command (D-051 lens-3 MED-3): "note that the
# numbers exclude OSNVV, give me the WoW delta" is a data question, and forcing
# cora_remember makes the actual ask unreachable. The remember regex was authored
# for MODEL escalation, where an over-broad match was harmless; promoting it to a
# forced tool changes that calculus, so it needs its own precision guard.
# D-165, caught by measurement on the very next run: the first cut used
# `\s+--+` / `\s+—`, and this predicate is an UNANCHORED search over a message
# up to Slack's 40k limit -- `\s+` re-consuming a long whitespace run at every
# start position took 4.2s (vs 0.007s before). A real clause separator is never
# more than a few spaces, so every whitespace run here is bounded. Fifth regex
# of this shape in the arc; the lesson keeps being "measure, then believe".
_REMEMBER_TRAILING_REQUEST_RE = re.compile(
    r"(?:[,;]|\s{1,4}--+|\s{1,4}—)\s{0,4}(?:and\s{1,4})?"
    r"(?:give|show|send|pull|find|get|tell|list|check|"
    r"what|who|when|where|how|why|which)\b",
    re.IGNORECASE,
)


# A polite modal aimed at CORA is an imperative, not a question -- "can you dm
# Tommy the Q3 numbers?" is the most natural phrasing of the very intent these
# detectors exist to catch, and a blanket "?" bail left the headline fix dark for
# it (D-051 lens-3 LOW-6). Same distinction _DELEGATE_INTERROGATIVE_RE already
# draws: modal + "you" is a request, modal + anything else is a question. The
# prefix is STRIPPED so the start-anchored patterns still see the imperative.
_POLITE_MODAL_RE = re.compile(
    r"^\s*(?:hey\s+|hi\s+)?(?:@?cora[,:]?\s+)?"
    r"(?:can|could|would|will)\s+(?:you|u)\s+(?:please\s+)?",
    re.IGNORECASE,
)


def _imperative_body(text: str) -> str | None:
    """The command text to match, or None when the message is a real question.

    Strips a leading "can you ..." politeness wrapper; anything else carrying a
    "?" is treated as a question and never forces a tool."""
    t = (text or "").strip()
    if not t:
        return None
    m = _POLITE_MODAL_RE.match(t)
    if m:
        t = t[m.end():].strip().rstrip("?").strip()
        return t or None
    if "?" in t:
        return None
    return t


def _slack_dm_intent(text: str) -> bool:
    t = _imperative_body(text)
    return bool(t and _SLACK_DM_INTENT_RE.search(t))


def _gmail_draft_intent(text: str) -> bool:
    t = _imperative_body(text)
    return bool(t and _GMAIL_DRAFT_INTENT_RE.search(t))


def _lexicon_teach_intent(text: str) -> bool:
    t = _imperative_body(text)
    return bool(t and _LEXICON_TEACH_INTENT_RE.search(t))


def _remember_intent(text: str) -> bool:
    """The FORCEABLE half of _remember_or_forget_intent (remember only)."""
    t = _imperative_body(text)
    if not t or _REMEMBER_TRAILING_REQUEST_RE.search(t):
        return False
    return bool(_REMEMBER_INTENT_RE.search(t))


def _staged_write_force_tool(text: str) -> str | None:
    """The staged-write tool to force for this message, or None.

    One function so precedence is testable in isolation and the call site stays
    a single branch. Ordered most-specific first: a lexicon teach and an email
    draft both look like "save this" to a looser matcher.
    """
    if _lexicon_teach_intent(text):
        # Only when the teach lane is actually live. With CORA_LEXICON below
        # "full" the tool answers every call with "isn't enabled yet", so
        # forcing it would replace a useful reply with a dead end. Read
        # per-call, so this activates the day the flag flips (cq-8866d3f7ac3b).
        try:
            from . import lexicon as _lex
            if _lex.lexicon_level() == "full":
                return "cora_lexicon_add"
        except Exception:  # noqa: BLE001 -- flag unavailable: fall through
            pass
    if _gmail_draft_intent(text):
        return "gmail_create_draft"
    if _slack_dm_intent(text):
        return "slack_send_dm"
    if _remember_intent(text):
        return "cora_remember"
    return None


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
    # FIRST statement on purpose (D-051 lens-1 MEDIUM). This is the reference
    # point for "did a sibling turn mint this pending after my turn began", so
    # every millisecond between the user's message arriving and this line is a
    # window in which a concurrent turn's pending is misread as this turn's own
    # -- the cq-db3b28dcdd42 shape. Derived here it still trails the pre-dispatch
    # Slack calls (_fetch_thread_history, _resolve_channel_name) by up to a few
    # hundred ms; closing that fully means threading the triggering event ts
    # through all five call sites, which is a follow-up, not a hotfix.
    # Wall clock (not monotonic): every pending store stamps time.time().
    _turn_started_at = time.time()
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
    # cq-24cc6ac4bbc8's pending-state line is appended AFTER the confirm
    # interceptor runs (D-051 lens-1 MEDIUM) -- probing it here would snapshot
    # state the interceptor is about to mutate. Declared now so it is in scope
    # for cache_storable regardless of which branch runs.
    pending_note = ""
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
    # Set when guard_outbound REPLACED a reply with a refusal, so the card layer
    # can tell "this text is the answer" from "this text is a refusal standing in
    # for an answer the channel may not see" (v2b S5 D-051). Attaching Confirm
    # buttons to a refusal asks the user to approve a payload they were just told
    # they cannot be shown.
    _guard_tripped: dict[str, str] = {}

    def _guard_content(txt: str) -> str:
        if retrieval_grant is not None or not txt:
            return txt
        guarded, tripped = channel_content_guard.guard_outbound(
            txt, entity=entity, tier=tier, channel_name=channel_name,
            user_id=user_id or "", is_dm=is_dm,
        )
        if tripped:
            _guard_tripped["class"] = tripped
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

    # v2 S1: the stash_id this turn's reply actually got carded with (if any),
    # so the post site can register WHERE the card landed once the post
    # succeeds. A one-element holder rather than a nonlocal because the reply
    # sites live in sibling scopes.
    _carded: dict[str, str] = {}

    def _confirm_card_for_reply(text: str) -> list[dict] | None:
        if not user_id or not text or not confirm_cards.confirm_buttons_enabled():
            return None
        if _guard_tripped:
            # The reply IS a refusal. Buttons under it would ask the user to
            # approve a write whose preview the channel guard just withheld --
            # and for gmail/hubspot the preview embeds the whole body, which is
            # exactly the kind of content that trips. The stash stays live for
            # the typed path and expires honestly on its own.
            log.info("confirm_card suppressed (reply refused by content guard class=%s)",
                     _guard_tripped.get("class"))
            return None
        changed = _tool_dispatch.freshest_changed_stash(
            _confirm_before_snapshot, user_id, channel_name)
        if changed is None:
            return None
        kind, cid = changed
        # v2 S1 (cq-fee6c9764950): one stash gets at most ONE live card. The
        # claim is atomic and one-shot, so a second attach attempt for the same
        # stash -- from a retry, a concurrent turn that raced onto the same id,
        # or a future reply site -- degrades to a plain text reply instead of
        # posting a duplicate apparently-actionable copy. The typed confirm
        # path still works for that reply, so nothing is lost.
        # D-051 lens-2 LOW: validate BEFORE claiming. The claim is one-shot and
        # deliberately survives pop_cards, so claiming and then bailing on an
        # invalid ask would permanently bar that id from ever getting a card.
        candidates: list[tuple[str, str]] = []
        item_texts: list[str] = []
        if kind == "ask":
            ask_entry = _tool_dispatch.get_pending_ask(user_id, channel_name)
            if not ask_entry or ask_entry.get("ask_id") != cid:
                return None
            candidates = [(key, label) for key, label, _value in ask_entry.get("candidates", [])]
            if not candidates:
                return None
        if kind == "meeting_item":
            # Validated HERE, alongside the ask, for the reason the comment above
            # gives: the claim below is one-shot and survives pop_cards, so
            # claiming and then bailing would bar this stash from ever being
            # carded. The first cut validated AFTER the claim and re-claimed --
            # which always failed, because the claim had already been taken by
            # the line above, so no item card was ever posted at all (D-051).
            entry = _tool_dispatch.peek_meeting_items(user_id, channel_name) or {}
            if entry.get("stash_id") == cid:
                item_texts = entry.get("items") or []
            if not item_texts:
                return None
        if not confirm_cards.claim_card_attach(cid):
            log.info("confirm_card attach REFUSED (already carded) stash=%s kind=%s", cid, kind)
            return None
        if kind == "ask":
            _carded["id"] = cid
            _carded["text"] = text
            log.info("confirm_card attached kind=ask stash=%s channel=#%s user=%s",
                     cid, channel_name, user_id)
            return confirm_cards.build_picker_blocks(text, cid, candidates)
        if kind == "meeting_item":
            # v2b S5 (cq-b5460ae7aca3): a meeting preview lists SEVERAL action
            # items, and the user wants some of them, so one Confirm over the
            # whole reply is the wrong shape. The reply itself stays buttonless
            # and one Confirm/Skip card per item follows it (posted by
            # _register_posted_card once the reply has landed). The claim above
            # covers all of them: one stash, one set of cards.
            _carded["item_stash"] = cid
            _carded["items"] = item_texts
            log.info("confirm_card meeting items=%d stash=%s channel=#%s user=%s",
                     len(item_texts), cid, channel_name, user_id)
            return None
        if kind == "schedule_meeting":
            # One button per OFFERED slot instead of a single Confirm (v2 S2):
            # v1's Confirm always booked slots[0] while the preview text offered
            # up to 3. Falls back to the plain Confirm/Cancel pair if the stash
            # carries no labels (a proposal minted before this change).
            entry = _tool_dispatch._peek_pending_schedule_meeting(user_id, channel_name) or {}
            labels = entry.get("slot_labels") or []
            if entry.get("stash_id") == cid and labels:
                _carded["id"] = cid
                _carded["text"] = text
                log.info("confirm_card attached kind=schedule_meeting slots=%d stash=%s "
                         "channel=#%s user=%s", len(labels), cid, channel_name, user_id)
                return confirm_cards.build_slot_picker_blocks(text, cid, labels)
        _carded["id"] = cid
        _carded["text"] = text
        log.info("confirm_card attached kind=%s stash=%s channel=#%s user=%s",
                 kind, cid, channel_name, user_id)
        return confirm_cards.build_confirm_blocks(text, cid)

    def _register_posted_card(channel, message_ts) -> None:
        """Record where this turn's card landed, so a later terminal state can
        take its buttons down (v2 S1). No-op when nothing was carded.

        Both args come straight off a Slack API response, so they are validated
        as real strings here rather than trusted: a test double (or a Slack
        response shape change) must never register a junk coordinate that a
        later sweep would then try to chat_update."""
        cid = _carded.get("id")
        if cid and isinstance(message_ts, str) and message_ts:
            ch = channel if isinstance(channel, str) and channel else channel_id
            confirm_cards.register_card(cid, ch, message_ts, _carded.get("text", ""))
        # v2b S5: meeting per-item cards ride AFTER the reply, one message each.
        # Folded in here rather than added at each reply site so all three sites
        # get it from the single place that already runs post-reply.
        _post_meeting_item_cards()

    def _post_meeting_item_cards() -> None:
        """One Confirm/Skip card per action item, each its own message.

        Separate messages, not one message with N button rows: two taps on
        different items of a shared message would each be a legitimate claim
        winner with a DIFFERENT correct outcome, and their two chat_update calls
        cannot be ordered after the fact -- the later-applied edit would clobber
        the other item's result. One message per item makes that impossible.

        Best-effort throughout: a card that fails to post simply is not there,
        and the typed 'create the first and third' path still works."""
        sid = _carded.pop("item_stash", None)
        items = _carded.pop("items", None) or []
        if not sid or not items:
            return
        # This is a NEW Slack-write surface reached from _dispatch_qa, and unlike
        # every other reply site it posts its own messages rather than going
        # through `say`. Both of the gates those sites get by construction have to
        # be stated here (D-051):
        #
        # EVAL_MODE -- missed_message_catchup drives _dispatch_qa with
        # CORA_EVAL_MODE=1 and a capture client that overrides ONLY chat_update,
        # so a raw chat_postMessage would reach real Slack. Unreachable today
        # (eval mode disables the tool, so no stash is minted and turn-ownership
        # would not match anyway) -- gated so it is safe by construction rather
        # than by accident, exactly as _close_stale_confirm_cards was.
        if os.environ.get("CORA_EVAL_MODE") == "1":
            return
        if not confirm_cards.confirm_buttons_enabled():
            return
        for idx, item in enumerate(items[:confirm_cards.MAX_ITEM_CARDS]):
            # format_reply -- every other card's text is the model's reply, which
            # has already been through it. This text comes from the STASH, so it
            # would otherwise skip the source-opacity lints (bare doc URLs, GIDs,
            # sheet identifiers, Drive paths). That matters more here than
            # anywhere else: slack_egress's class-level sanitizer only rewrites
            # the `text=` kwarg, and Slack renders `blocks`, so the string the
            # channel actually SEES has no egress backstop at all.
            item = format_reply(item)
            text = f"*{idx + 1}.* {item}"
            # channel_content_guard -- same story. guard_outbound is
            # all-or-nothing (it replaces the whole answer with a refusal), so an
            # unguarded card would publish, in its own message, the exact content
            # the guard had just refused to put in this channel. Per item, so one
            # tripping item is dropped rather than the whole set.
            if not item.strip() or _guard_content(text) != text:
                log.warning("meeting item card WITHHELD by outbound guards idx=%d channel=#%s",
                            idx, channel_name)
                continue
            try:
                resp = client.chat_postMessage(
                    channel=channel_id, thread_ts=reply_thread_ts, text=text,
                    blocks=confirm_cards.build_item_confirm_blocks(text, sid, idx),
                    unfurl_links=False, unfurl_media=False,
                )
                confirm_cards.register_card(sid, channel_id, (resp or {}).get("ts", ""), text)
            except Exception:  # noqa: BLE001 -- a card is a nicety, never the answer
                log.warning("meeting item card post failed idx=%d (non-fatal)", idx,
                            exc_info=True)

    def _post_reply_card_sweep() -> None:
        """Close any rendered card whose stash went terminal -- including THIS
        turn's own typed/model confirm, cancel, or supersede (v2 S1). Runs after
        the reply is posted so a card minted THIS turn (still live) is untouched
        while a card the turn just consumed comes down immediately."""
        _close_stale_confirm_cards(client)

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
            message=user_message, turn_started_at=_turn_started_at,
        )
        if confirm_reply is not None:
            log.info(
                "confirm_interceptor served channel=#%s user=%s", channel_name, user_id,
            )
            guarded_reply = _guard_content(confirm_reply)
            _resp = say(text=guarded_reply, blocks=_confirm_card_for_reply(guarded_reply),
                        thread_ts=reply_thread_ts, unfurl_links=False, unfurl_media=False)
            _register_posted_card((_resp or {}).get("channel", ""), (_resp or {}).get("ts", ""))
            _post_reply_card_sweep()
            active_thread_store.register(channel_id, register_ts)
            return

    # ── Pending-state visibility for the model (cq-24cc6ac4bbc8) ────────────
    # Probed HERE, after the interceptor, for two reasons found by the D-051
    # lens-1 pass:
    #   * the interceptor MUTATES this state while still returning None (it
    #     abandons a stale destructive Asana pending on a superseding write and
    #     on a question), so a probe taken earlier told the model a delete was
    #     staged that had just been abandoned;
    #   * it takes turn_started_at, so a pending minted by a CONCURRENT turn is
    #     excluded here exactly as it is from the arbitration. Without that, the
    #     note re-exposed a sibling turn's pending on the model path with an
    #     imperative to confirm it -- and the per-kind claims are keyed on
    #     (user, channel) with no turn check, so the model could have executed a
    #     write for a preview the user had not yet been shown. That is
    #     cq-db3b28dcdd42 reintroduced on the model path.
    if user_id:
        try:
            pending_note = _tool_dispatch.describe_live_pendings(
                user_id, channel_name, turn_started_at=_turn_started_at)
        except Exception:  # noqa: BLE001 -- enrichment must never break a reply
            log.warning("pending-state context probe failed (non-fatal)", exc_info=True)
            pending_note = ""
        if pending_note:
            runtime_context = runtime_context + pending_note + "\n\n"

        # Cora's memory of its OWN outstanding knowledge-check question
        # (cq-6fbaf37b1ee7). Reaching here means the message did not match as an
        # answer -- a competing intent won, or the person is asking something
        # else -- and the 8/14 complaint was that Cora then told the person it
        # had never asked. DM-ONLY: the ask is delivered by DM, and the question
        # carries its own entity scope (a LEX question has no business appearing
        # in an F3E channel's context just because the same person mentioned
        # Cora there). Fail-soft to "".
        if channel_name == "dm":
            try:
                kc_note = knowledge_check.recall_ask_note(user_id)
            except Exception:  # noqa: BLE001 -- enrichment must never break a reply
                log.warning("knowledge-check recall probe failed (non-fatal)",
                            exc_info=True)
                kc_note = ""
            if kc_note:
                runtime_context = runtime_context + kc_note + "\n\n"

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
    # Hoisted so the web gate below can read it on EVERY branch (the Tier-2
    # grant path never computes one). R1 makes the gate depend on it.
    web_clean = False
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
        # LEX lane (2026-08-06): evaluate() is the SINGLE authority on whether
        # this turn can attach, so there is deliberately no separate LEX clause
        # here. With CORA_WEB_TOOLS_LEX off, evaluate returns "lex_scope"
        # (attach=False) and web_clean is False exactly as before -- byte-
        # identical.
        #
        # What this actually buys (corrected by the D-051 F3 finding -- an
        # earlier version of this comment overstated it): web_clean does NOT
        # strip ordinary LEX chunk text. It demotes the asker to the fail-closed
        # STRANGER posture -- no note overlay, Tier-1 strips every personal
        # chunk, no asker-scoped cross-entity fallback -- and all three of those
        # surfaces set unstripped_personal, which the belt below already
        # converts into a withhold. So this is not the barrier against a leak;
        # the belt is. Without it, a LEX web ask from anyone carrying a personal
        # note would gate_skip and SILENTLY DEGRADE, i.e. exactly the
        # cq-49a7835f081c never-attaches bug re-created for LEX. It makes the
        # lane usable; the belt keeps it safe.
        #
        # Scope note: this covers the EXPLICIT-intent leg only (web_intent). The
        # time-sensitive fallback leg can still attach in LEX without the clean
        # load -- fail-safe, because every surface it would have removed sets
        # unstripped_personal and is withheld by the belt.
        #
        # Honest limitation (D-051 2026-08-07 -- an earlier phrasing of this
        # claimed context degrades ONLY when tools really attach, which is not
        # true for one branch): web_clean is decided HERE, but force_tool and
        # the gate are computed AFTER the load. A turn carrying explicit web
        # intent AND a forced-tool intent (a destructive Asana confirm, a
        # code-queue op) takes the stranger-posture load and is then withheld
        # as gate_skipped:forced_tool -- degraded context, no web, pure loss.
        # Pre-existing for ordinary askers; R1 extends it to custodians on their
        # own LEX surface. Narrow and fail-safe (it loses context, never leaks
        # it); the real fix is to move the gate-skip computation above the load,
        # which is a larger reshuffle than this rider should carry.
        #
        # R1 (Harrison ruling 2026-08-07): `not phi_custodian` is GONE from this
        # condition. A custodian used to be excluded here and then withheld
        # outright at the gate below, which made the whole LEX web lane inert --
        # all five people who can ask a LEX web question are custodians. The fix
        # is the same shape that closed Harrison's own web blackout
        # (cq-49a7835f081c): exclude the CONTENT from the turn, not the turn
        # from the capability. A custodian whose turn will actually attach now
        # takes the stranger-posture load for that turn only. Identity and
        # authorization are untouched -- `phi_custodian` below still holds the
        # true value for access checks and the cache guard; only what the model
        # can see while composing outbound queries is demoted. load_context_parts
        # forces phi_custodian=False internally under web_clean so the LEX scrub
        # actually runs (the flag is a separate parameter from the three
        # asker-scoped locals web_clean already nulls).
        web_clean = (
            web_intent
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
        # A turn carrying pending-state context can produce a reply that names
        # THIS person's staged writes ("you have an inventory change staged").
        # The semantic cache is entity-keyed, not user-keyed, so storing that
        # would serve one person's staged-write state to the next asker in the
        # same entity. Same exclusion shape as unstripped_personal (D-043).
        and not pending_note
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
        elif _delegate_work_intent(user_message):
            # Ordered ABOVE the Asana force: "delegate a job: ..." is an
            # explicit worker hand-off, not a task op. Forcing it is what makes
            # the delegated-work intake screens actually RUN -- a narrated
            # preview with no tool call bypasses every guard behind them
            # (cq-d30815ee6993).
            force_tool = "cora_delegate_work"
            log.info("delegate-work intent -> forcing tool channel=#%s user=%s",
                     channel_name, user_id)
        else:
            # S6 rider (cq-904f849bc59a): the Class-B staged-write intents.
            # Ordered ABOVE the Asana force and BELOW code-queue/delegate. All
            # four branches are start-anchored, so a task request that merely
            # MENTIONS one of them ("create a task to draft an email to Bob")
            # still reaches asana_create_task -- the displacement class D-158
            # was opened by an unanchored match.
            force_tool = _staged_write_force_tool(user_message)
            if force_tool:
                log.info("staged-write intent -> forcing %s channel=#%s user=%s",
                         force_tool, channel_name, user_id)
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
            # v2b S5: the Class-B kinds mint pendings this gate was blind to,
            # and its whole premise is "a pending means a real write is coming,
            # so broadening would clobber its legitimate success narration".
            # Without this, a bare "yes" that sends a DM had its truthful
            # "Done -- DM sent to Tommy" replaced with "I didn't actually change
            # anything in Asana" -- inviting a re-ask that sends it twice.
            or _tool_dispatch.has_pending_classb(user_id, channel_name)
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
        # v2 S2: schedule_meeting was the last staged-write kind still absent
        # from this chain. Its confirm turn is the same bare-affirmative-answers-
        # a-staged-preview shape as every other kind here, and it now also
        # participates in the typed-confirm arbitration (which DEFERS to the
        # model), so the model has to reliably reach the tool.
        or _tool_dispatch.has_pending_schedule_meeting(user_id, channel_name)
        # v2b S5: all six Class-B kinds behind ONE call (gmail draft, HubSpot
        # stage/note, Slack DM, influencer handle/deliverable, meeting items).
        # They defer in the arbitration exactly like the kinds above, so the
        # model is the only route to their tool -- and a seventh Class-B kind
        # is covered here the moment it is registered, with no edit to this line.
        or _tool_dispatch.has_pending_classb(user_id, channel_name)
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
    # A FORCED staged-write turn produces a preview bound to a server-side stash
    # that exists only for this (user, channel). Caching it means a later,
    # similar ask is served the stored preview text verbatim -- with no stash, no
    # buttons, and, entity-keyed as the cache is, potentially another user's
    # recipient and message body. That replayed buttonless preview IS the phantom
    # state this rider exists to eliminate (D-051 lens-3 MED-5).
    if force_tool is not None:
        cache_storable = False
    # ── Web tools gate (2026-07-31): the server-side web_search/web_fetch tools
    # attach only when web_guard says so — explicit web intent, or a time-sensitive
    # question whose KB retrieval missed; in LEX scope only when the
    # CORA_WEB_TOOLS_LEX lane is on (default off) and then through a stricter
    # client-name screen; the user query is egress-screened fail-closed; a daily
    # search cap bounds spend. A block is a soft degradation (KB-only behavior),
    # never a user-facing refusal.
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
    #    R1 (2026-08-07): the custodian exclusion is now `and not web_clean`.
    #    The exclusion exists because unscrubbed content is in context -- when
    #    the web-clean load ran, it is NOT: the three asker-scoped locals are
    #    nulled and phi_custodian is forced False for that load, so the LEX
    #    scrub runs and the custodian sees exactly what a stranger sees. The
    #    premise of the withhold is gone, so the withhold goes. A custodian turn
    #    that did NOT take the clean load (time-sensitive fallback, or any
    #    future caller) still withholds, fail-closed.
    web_on = False
    web_gate_skip: str | None = None
    if force_tool is not None:
        web_gate_skip = "forced_tool"
    elif assume_confirm:
        web_gate_skip = "assume_confirm"
    elif retrieval_grant is not None:
        web_gate_skip = "retrieval_grant"
    elif phi_custodian and not web_clean:
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
            # D-051 F4/F2 (2026-08-06): prior thread/DM turns are an UNGOVERNED
            # free-text surface -- _fetch_thread_history/_fetch_dm_history do
            # structural transforms only, no PHI scrub, and web_clean never
            # reaches them (it governs the KB load, not the conversation). The
            # model composes its search strings from the whole window, so in
            # LEX scope those turns can carry client-identifying prose straight
            # into an outbound query. This turn's LEX chunks ARE deterministically
            # scrubbed; the human turns above them are not -- that asymmetry is
            # the exposure. Drop history for LEX web turns only: thread context
            # is the least valuable input to a live-web lookup, and screening
            # free-text Slack prose would over-block and re-create the
            # never-attaches bug (cq-49a7835f081c). Non-LEX is untouched;
            # cq-505a37b1c4b7 still owns the general case.
            # R1 follow-up (D-051, 2026-08-07) -- HIGH, caught pre-push. This
            # drop and the custodian withhold R1 relaxed must cover the SAME
            # set, or relaxing one silently un-covers the other. They were keyed
            # on different predicates and are NOT co-extensive:
            # lex_phi_access.phi_allowed also returns True for the founder in a
            # DM (a fixed-identity carve-out), and _handle_dm_qa pins his DM
            # entity to FNDR -- so `phi_custodian=True` with
            # `is_lex_scope("FNDR")=False`. Before R1 that turn was withheld
            # outright; after R1 it attached with prior DM turns intact, and a
            # custodian's DM note overlay is unscrubbed AND forced into the LEX
            # store (user_notes.resolve_save_scope). A client name quoted one
            # turn earlier could ride into a model-composed search query on the
            # highest-volume DM surface in the system. `or phi_custodian`
            # restores co-extension.
            if (web_guard.is_lex_scope(entity) or phi_custodian) and prior_messages:
                log.info("web_tools: dropping %d prior turn(s) (lex_scope=%s "
                         "custodian=%s)", len(prior_messages),
                         web_guard.is_lex_scope(entity), phi_custodian)
                prior_messages = []
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
        _resp = say(
            text=response_text,
            blocks=_confirm_card_for_reply(response_text),
            thread_ts=reply_thread_ts,
            unfurl_links=False,
            unfurl_media=False,
        )
        _register_posted_card((_resp or {}).get("channel", ""), (_resp or {}).get("ts", ""))
        _post_reply_card_sweep()
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
        _register_posted_card(placeholder_channel, placeholder_ts)
    except Exception as exc:  # noqa: BLE001
        log.error(
            "Final chat_update failed for ts=%s: %s — sending fresh reply as fallback",
            placeholder_ts, exc,
        )
        # D-051 lens-1 LOW: post the fallback WITHOUT buttons. chat_update can
        # be applied server-side and still raise client-side (a read timeout or
        # reset after Slack processed it), in which case the card already
        # landed; re-posting `confirm_blocks` here would put a SECOND live card
        # under a claim that was already consumed, breaking one-stash-one-card
        # and leaving the first copy unregistered and unclosable. The text still
        # reaches the user, and the typed confirm path still works.
        say(
            text=response_text,
            thread_ts=reply_thread_ts,
            unfurl_links=False,
            unfurl_media=False,
        )
    _post_reply_card_sweep()

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
    # channel_name threaded here too: without it the live 8/19 13:14 defect
    # still reproduced via /cora-ask in the inventory channel, and the
    # surface-parity test could not catch the divergence because it patches
    # the guard with a return_value (D-051 lens-3 F6).
    cross_redirect = cross_entity_guard.check_cross_entity(
        text, entity, channel_name=channel_name)
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
    # channel_name is threaded so the dedicated office-inventory write channels
    # can carry the prose-write exemption (cq-1b6554a58fae). Every other channel
    # is unaffected: an omitted channel_name means the guard behaves exactly as
    # before, which is why the remaining call sites are safe left as they are.
    cross_redirect = cross_entity_guard.check_cross_entity(
        user_message, entity, channel_name=channel_name)
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
    # D-104 [QA] quarantine (2026-08-06). The note path is a fourth intake surface
    # and the only one that spends a Haiku call BEFORE anything is queued, so an
    # unscreened "@Cora note: [QA] ..." both costs a model call and stages a
    # confirm. Screened here, at the single entry both callers share.
    if qa_scaffolding.is_qa_message(content):
        log.info("team_learning: [QA] smoke note -- not captured user=%s", user_id)
        say(text="[QA] noted -- treated as test traffic, not captured as knowledge.",
            thread_ts=original_ts, unfurl_links=False)
        return
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


# ── DM bot-mention strip (cq-236fd0310eb8, live 2026-08-20) ──────────────────
# Slack delivers no app_mention event for IMs, so the DM branch of
# handle_message_event is the only DM entry point -- and unlike handle_mention
# (which runs `_MENTION_RE.sub("", raw_text)` before anything reads the text) it
# passed `event["text"]` through VERBATIM. A DM that opens with "@Cora ..."
# therefore reached every downstream detector still carrying its literal
# `<@U0B44MDGC5R> ` token.
#
# Every staged-write intent detector in this module is START-ANCHORED after the
# mention strip -- that anchoring is the documented defence against the D-158
# stolen-turn class -- so the token blinded ALL of them at once. Measured on the
# live 8/20 16:05 message: `_staged_write_force_tool` returned None and
# `_remember_or_forget_intent` returned False with the token, and
# "cora_remember"/True the moment it was removed. The consequence was the whole
# reported incident: no force, no Sonnet escalation, Haiku narrated a
# preview-shaped reply with ZERO tool_use, so no stash was ever minted -- hence
# no confirm card could attach, and the three typed "Confirm" turns that
# followed had nothing pending to intercept and fell to the model, which called
# cora_remember(confirmed=True) against an empty store over and over.
#
# Scoped deliberately to CORA'S OWN id, not the generic `_MENTION_RE`: in a DM
# a leading mention of a THIRD party is content ("<@U123> approved it"), and
# rewriting text that guards and forced-tool detectors read is a smuggling
# channel unless it is kept narrow. That IS an asymmetry with the channel path,
# and a deliberate one -- an app_mention event guarantees the mention is Cora's,
# a DM does not.
#
# The first version of this comment claimed blanket "parity ... so this
# introduces no asymmetry", and used it as a reason not to look further. D-051
# lens-5 caught what that hid: `_MENTION_RE` did not strip a trailing comma, so
# "@Cora, remember ..." was still broken in every channel by the very defect
# this function documents. That is now fixed at `_MENTION_RE` itself. The two
# strips agree on the leading anchor and on the comma; they differ only on WHOSE
# mention they remove, and on a mention-only message (the channel path yields "",
# this one keeps the original text so the DM guard's truthiness test is stable).
def _strip_dm_bot_mention(text: str, client) -> str:
    """Strip a leading `<@CoraBotId>` (plus a trailing comma/colon) from DM text.

    Returns the text unchanged when the bot id cannot be resolved -- failing
    open here only restores today's behaviour, never a wrong strip."""
    # Cheapest possible short-circuit FIRST (D-051 lens-1). _resolve_bot_user_id
    # caches None on failure and therefore RETRIES on the next call, so without
    # this every DM -- including a shift-scheduler turn that makes no other Slack
    # call, and upstream of the rate limiter -- would block on a failing
    # auth.test while Slack is degraded. A DM with no leading mention cannot
    # possibly need the bot id.
    if not text or not text.startswith("<@"):
        return text
    bot_id = _resolve_bot_user_id(client)
    # isinstance, not truthiness: bot_id comes straight off a Slack API
    # response and is cached process-wide, so a shape change (or a test double)
    # that yields a non-string would otherwise raise inside re.escape and take
    # the whole DM path down -- for every DM, for the life of the process.
    if not isinstance(bot_id, str) or not bot_id:
        return text
    stripped = re.sub(rf"^<@{re.escape(bot_id)}>\s*[,:]?\s*", "", text).strip()
    return stripped or text


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


def _kc_post(client, channel: str, thread_ts: str | None, text: str,
             blocks: list | None = None) -> str:
    """Post one knowledge-check DM message. Returns its ts ("" on failure)."""
    try:
        kwargs = {"channel": channel, "text": text, "unfurl_links": False,
                  "unfurl_media": False}
        if blocks:
            kwargs["blocks"] = blocks
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        return (client.chat_postMessage(**kwargs) or {}).get("ts", "") or ""
    except Exception as exc:  # noqa: BLE001
        log.warning("knowledge_check: post failed: %s", exc)
        return ""


def _handle_knowledge_check_reply(event: dict, client, user_id: str, text: str,
                                  cycle: dict) -> bool:
    """Route a DM reply into the knowledge-check flow.

    Returns True when this branch OWNS the message (caller must return), False
    when it declined it so the DM falls through to the normal routing. Declining
    matters: an outcome of not_live/not_authorized/empty means this was never an
    answer, and swallowing it would leave a real question unanswered.
    """
    try:
        outcome, payload, post_card = knowledge_check.handle_dm_reply(
            cycle.get("cycle_id", ""), user_id, text)
    except Exception:  # noqa: BLE001 -- capture must never break DMs
        log.warning("knowledge_check: handle_dm_reply failed", exc_info=True)
        return False

    if outcome in ("not_live", "not_authorized", "empty"):
        return False

    channel = event.get("channel", user_id)
    thread_ts = event.get("thread_ts")
    log.info("knowledge_check: DM reply user=%s cycle=%s outcome=%s",
             user_id, cycle.get("cycle_id"), outcome)

    if post_card:
        cid = cycle.get("cycle_id", "")
        # A cycle asked on an EARLIER day gets its date printed on the card, so a
        # late answer is bound to a named question rather than confirmed blind
        # (cq-6fbaf37b1ee7).
        asked_on = (str(cycle.get("date") or "")
                    if knowledge_check.is_late_ask(cycle) else "")
        body = knowledge_check.confirm_text(
            payload, cycle.get("question", ""), asked_on)
        blocks = (knowledge_check.build_confirm_blocks(
            payload, cid, cycle.get("question", ""), asked_on)
            if confirm_cards.confirm_buttons_enabled() else None)
        # With buttons off the same text still ships and the TYPED path
        # completes the loop -- the flow never depends on a tap.
        #
        # "save" / "discard", NOT "yes" / "no" (D-051 lens-2 HIGH). The F-23
        # deterministic interceptor executes a pending staged write on a bare
        # affirmative BEFORE the model runs, and its vocabulary contains "yes"
        # and "ok" while its STOP list contains "no" and "skip". Since a live
        # staged write outranks this branch, telling the user to reply "yes"
        # would instruct them straight into firing an unrelated staged write
        # (potentially a destructive Asana delete), and "no" would cancel it.
        # Neither "save" nor "discard" appears in either list, so the copy can
        # never steer a user into the collision.
        if not blocks:
            body += "\n\n_Reply *save* to save it, or *discard* to skip._"
        card_ts = _kc_post(client, channel, thread_ts, body, blocks)
        # Record where the card landed so a reply typed in the CARD's thread
        # matches too. Without it, match_live_cycle only ever recognised the ASK
        # message's ts, so a confirm typed in the confirm card's own thread fell
        # through to Q&A and the answer expired unwritten.
        if card_ts:
            knowledge_check.register_card_ts(cid, card_ts)
        return True

    _kc_post(client, channel, thread_ts, payload or "Got it.")
    return True


def _handle_kc_tap(body: dict, client, action: str) -> None:
    """Shared receiver for the four knowledge-check buttons.

    Mirrors the gap-decline handler's terminal-edit rule exactly: an outcome the
    tapper does not own (not_authorized / already_handled / orphaned) replies
    EPHEMERALLY and never touches the shared card, because two independent
    chat_update round-trips cannot be ordered after the fact and the race loser
    would clobber the winner's real result.
    """
    try:
        actions = body.get("actions") or []
        raw_value = (actions[0].get("value") if actions else "") or ""
        cycle_id, fingerprint = knowledge_check.split_tap_value(raw_value)
        actor_id = (body.get("user") or {}).get("id", "")
        channel_id = (body.get("channel") or {}).get("id", "")
        message_ts = (body.get("message") or {}).get("ts", "")

        if os.environ.get("CORA_EVAL_MODE") == "1":
            return
        if not knowledge_check.enabled():
            return

        if action == "confirm":
            outcome, msg = knowledge_check.process_confirm_tap(
                cycle_id, actor_id, fingerprint)
        elif action == "edit":
            outcome, msg = knowledge_check.process_edit_tap(
                cycle_id, actor_id, fingerprint)
        elif action == "skip_answer":
            outcome, msg = knowledge_check.process_skip_answer_tap(
                cycle_id, actor_id, fingerprint)
        else:
            outcome, msg = knowledge_check.process_skip_today_tap(cycle_id, actor_id)

        if outcome in ("not_authorized", "orphaned", "already_handled", "not_live",
                       "superseded"):
            try:
                client.chat_postEphemeral(channel=channel_id, user=actor_id, text=msg)
            except Exception:  # noqa: BLE001
                pass
            return

        if outcome == "editing":
            # The card stays live: they are about to send a reworded answer, and
            # dropping the buttons now would strand it if they change their mind.
            try:
                client.chat_postEphemeral(channel=channel_id, user=actor_id, text=msg)
            except Exception:  # noqa: BLE001
                pass
            return

        # promoted | held | skipped | refused: the unique winner closes its card.
        if channel_id and message_ts:
            orig = (body.get("message") or {}).get("blocks") or []
            sections = [b for b in orig if b.get("type") == "section"]
            new_blocks = (sections or [{"type": "section",
                                        "text": {"type": "mrkdwn", "text": msg}}]) + [
                {"type": "context", "elements": [{"type": "mrkdwn", "text": msg}]}]
            try:
                client.chat_update(channel=channel_id, ts=message_ts,
                                   text=msg, blocks=new_blocks)
            except Exception as exc:  # noqa: BLE001
                log.warning("knowledge_check: chat_update failed: %s", exc)
    except Exception:  # noqa: BLE001 -- a handler error must never crash the bot
        log.warning("knowledge_check tap handler error (non-fatal)", exc_info=True)


@app.action(knowledge_check.ACTION_CONFIRM_ANSWER)
def handle_kc_confirm(ack, body, client) -> None:
    ack()
    _handle_kc_tap(body, client, "confirm")


@app.action(knowledge_check.ACTION_EDIT_ANSWER)
def handle_kc_edit(ack, body, client) -> None:
    ack()
    _handle_kc_tap(body, client, "edit")


@app.action(knowledge_check.ACTION_SKIP_ANSWER)
def handle_kc_skip_answer(ack, body, client) -> None:
    ack()
    _handle_kc_tap(body, client, "skip_answer")


@app.action(knowledge_check.ACTION_SKIP_TODAY)
def handle_kc_skip_today(ack, body, client) -> None:
    ack()
    _handle_kc_tap(body, client, "skip_today")


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
            # Parity with handle_mention, at the ONE place DM text is derived,
            # so every downstream consumer in this branch (gap-ask capture, the
            # daily knowledge check, the shift-scheduler test, retrieval intent
            # and _handle_dm_qa) sees the same body a channel ask would produce.
            # Inside the guard, not above it: a bot/empty DM is dropped without
            # spending an auth.test on it, and the strip can only ever shorten
            # a non-empty string (it falls back to the original when the whole
            # message was the mention), so the guard's own truthiness test is
            # unaffected by running after it.
            text = _strip_dm_bot_mention(text, client)
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
            # v2 S3 (cq-67490abe2d86, live 8/3): a clear "remember that X" /
            # "forget that note" DM is a STAGED-WRITE COMMAND, not an answer to
            # a pending gap ask -- but it is also not interrogative, so
            # looks_like_question let the greedy top-level match swallow it.
            # Live symptom: "Cora, remember the cobalt falcon ..." was filed as
            # a bogus known-answer proposal and nothing was ever saved to the
            # user's notes. Same shape and same remedy as the two predicates
            # beside it: a top-level DM that is plainly something else does not
            # count as an answer. The THREADED path is untouched -- a genuine
            # answer typed in the ask's own thread ignores allow_toplevel and
            # still always matches, even if it happens to contain "remember".
            # D-051 lens-2 MEDIUM: a live staged write outranks the gap ask
            # outright. S4's new preview copy tells DM users to reply "confirm",
            # and "confirm" is neither a shift keyword nor a question nor a
            # remember command -- so it was eligible for top-level capture, and
            # a user with a pending gap ask who did exactly what the preview
            # said would have their write silently NOT fire while the literal
            # word "confirm" was filed as the gap's answer. Same failure family
            # as cq-67490abe2d86, newly reachable because of S4. A
            # pending-stash test is tighter than any bare-affirmative regex: it
            # covers "yes", "2", a slot number and every future phrasing.
            _has_staged_write = bool(
                any(_tool_dispatch.snapshot_stash_ids(user_id, "dm").values()))

            # ── Daily knowledge check capture ────────────────────────────────
            # Same intent-collision family as the gap ask above, and it shares
            # that branch's whole guard set: a shift command, a fresh question, a
            # remember/forget command and a live staged write all outrank a
            # top-level capture, because each of those is a DIFFERENT thing the
            # person plainly meant to do.
            #
            # The two capture systems are MUTUALLY EXCLUSIVE on the ambiguous
            # top-level path: if a gap ask is also outstanding, neither claims
            # the DM and it falls through to Q&A. Letting both compete would mean
            # one person's single reply silently answering whichever question the
            # code happened to check first. A THREADED reply is unambiguous and
            # always matches its own ask, in either system.
            #
            # Safety of the capture itself: nothing is written here. A capture
            # stages an answer and shows it back on a card, so a mis-capture
            # costs one Skip tap -- never a wrong fact in an always-injected file.
            # _dm_is_shift_message, NOT gap_autofill.is_shift_keyword (D-051
            # lens-2 HIGH). The two vocabularies differ: is_shift_keyword misses
            # "submit availability" -- the exact phrase OSN employees are nudged
            # with -- and, critically, it has no mid-flow check, so a scheduler
            # user part-way through submitting availability would have their
            # "Mon Wed Fri 6a-2p" captured as a knowledge-check answer while the
            # scheduler stayed stuck. _dm_is_shift_message is the predicate the
            # router itself trusts 100 lines below; using anything weaker here
            # makes this branch laxer than the one that actually owns the intent.
            _kc_live = knowledge_check.enabled() and knowledge_check.has_live_cycle(user_id)
            # TWO predicates, not one (D-051 lens-3 F4). `_kc_live` is the RECALL
            # window and decides whether KC may try to match at all; `_kc_today` is
            # the same-day window and is what gap_autofill's mutual exclusion needs
            # -- the two capture systems only genuinely compete for the same reply
            # while both asks are live TODAY. Passing the wide predicate to
            # gap_autofill silently suppressed a gap-ask answer for up to four
            # days: all weekend, every weekend.
            _kc_today = _kc_live and knowledge_check.has_cycle_asked_today(user_id)
            _gap_ask_live = gap_autofill.has_live_ask(user_id)
            _generic_intent_ok = (
                not _dm_is_shift_message(user_id, text)
                and not gap_autofill.looks_like_question(text)
                and not _remember_or_forget_intent(text)
                and not _has_staged_write
            )
            if _kc_live:
                try:
                    kc_cycle = knowledge_check.match_live_cycle(
                        user_id, event.get("thread_ts"),
                        allow_toplevel=_generic_intent_ok and not _gap_ask_live,
                    )
                except Exception:  # noqa: BLE001 -- capture must never break DMs
                    log.warning("knowledge_check: match_live_cycle failed", exc_info=True)
                    kc_cycle = None
                if kc_cycle and _handle_knowledge_check_reply(
                        event, client, user_id, text, kc_cycle):
                    return

            try:
                ask = gap_autofill.match_pending_ask(
                    user_id,
                    event.get("thread_ts"),
                    allow_toplevel=_generic_intent_ok and not _kc_today,
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
    # C5: every pattern in team_learning._CORRECTION_PATTERNS is ^-anchored, and
    # the W1-01 "@mentions Cora" skip sits AFTER this path deliberately, so a
    # correction addressed to Cora reaches here as "<@U...> actually that's
    # wrong" and every anchored pattern misses it. Same unstripped-mention-blinds-
    # a-start-anchored-detector class as the 8/20 confirm-surface finding, and
    # the mention route already applies exactly this strip. Latent today (channel
    # `message` events do not reach this app at all -- see below), which is
    # precisely why it has to be fixed now: it would go live silently the moment
    # the Event Subscription lands.
    _correction_text = _MENTION_RE.sub("", text).strip() or text
    if team_learning.is_correction(_correction_text):
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
            correction_text=_correction_text,
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

    # Cross-entity guard pre-LLM (mirrors handle_mention Path 1) -- including the
    # channel_name, so a thread FOLLOW-UP in an inventory channel ("make it 3
    # instead") gets the same treatment as the mention that opened the thread.
    cross_redirect = cross_entity_guard.check_cross_entity(
        text, entity, channel_name=channel_name)
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
    # cq-6b014816819c: also capture a listed MECHANICAL-lane approver's reaction,
    # so the split-out mechanical surface can be acted on at all. Logging is not
    # approving -- correlate_reactions_to_updates re-checks authorization per
    # ITEM, and review_lanes.can_approve lets a non-Harrison actor act only on a
    # non-LEX mechanical row.
    #
    # is_review_approver is itself gated on the surface flag (D-051 lens-2 LOW):
    # without that, adding a name to the YAML started logging that person's
    # reactions on EVERY Cora message anywhere, flag or no flag -- so the two
    # halves of the documented "deliberate two-part flip" were not independent
    # in code, and one of them had a privacy side effect nobody asked for.
    if review_lanes.is_review_approver(reactor):
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
            # C4: the buttons were already dropped here, but the card's own
            # footer -- "👍 Approve · 👎 Dismiss  (or tap a button below)" -- was
            # carried through verbatim, so a tapped card kept telling Harrison to
            # use an emoji that is now a no-op and a button that no longer
            # exists. He acted on exactly that: 11 of his 19 reactions on
            # 2026-08-24 landed on cards he had already executed that morning.
            # terminal_card_blocks does the section-keep + affordance-strip +
            # actions-drop in one place, shared with the emoji path.
            new_blocks = knowledge_review.terminal_card_blocks(orig, msg)
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


# ── Daily-briefing enrollment one-tap (S6 migration 1, 2026-08-09) ───────────
# Enable/Skip on the "WOULD-BE BRIEFING" review DMs. Before this, enrolling a
# teammate meant reacting :+1: and WAITING for the next 7:30am fire to resolve
# it -- the button applies the same verdict immediately.
#
# The reaction path is untouched and permanent (locked pattern rule: buttons are
# ADDITIVE). Idempotency between the two comes from the shared pending-review
# list: a tap CONSUMES the entry, and the script's reaction resolver only acts on
# entries still in that list, so tapping and then reacting cannot double-apply.
#
# All correctness (Harrison-only gate, atomic read-modify-write, consumption)
# lives in briefing_enrollment.process_enrollment_tap; this wrapper is Slack I/O.

# A tap CONSUMES the pending-review entry, and the script's reaction resolver
# only acts on entries still in that list -- so after a tap, any reaction on that
# message is a permanent no-op. The retained body still said "Reacting :+1: /
# :-1: still works too", which is exactly the promise Harrison would reach for to
# correct a mis-tap (D-051 lens-5 MED). Rewrite that sentence on the terminal
# edit so the card never advertises an affordance it has just disabled.
_REACTION_AFFORDANCE_RE = re.compile(
    r"\s*Reacting :\+1: / :-1: still works too \(picked up at the next run\)\.",
)


def _strip_reaction_affordance(block: dict) -> dict:
    try:
        txt = ((block.get("text") or {}).get("text") or "")
        if not txt or "Reacting :+1:" not in txt:
            return block
        cleaned = _REACTION_AFFORDANCE_RE.sub(
            " (Reactions no longer apply to this card.)", txt)
        return {**block, "text": {**block["text"], "text": cleaned}}
    except Exception:  # noqa: BLE001 -- cosmetic; never break the edit
        return block


def _handle_briefing_enrollment_tap(body: dict, client, *, enable: bool) -> None:
    try:
        actions = body.get("actions") or []
        review_id = (actions[0].get("value") if actions else "") or ""
        actor_id = (body.get("user") or {}).get("id", "")
        channel_id = (body.get("channel") or {}).get("id", "")
        message_ts = (body.get("message") or {}).get("ts", "")

        # Read-only harness (missed-message catch-up) must never mutate
        # enrollment -- same gate every other write surface carries.
        if os.environ.get("CORA_EVAL_MODE") == "1":
            return

        if not confirm_cards.confirm_buttons_enabled():
            # Kill-switch parity: with buttons off these cards are never posted,
            # but a card rendered BEFORE a flip-and-restart can still be tapped.
            # Refuse and name the fallback that still works.
            if channel_id and actor_id:
                try:
                    client.chat_postEphemeral(
                        channel=channel_id, user=actor_id,
                        text=("Buttons are turned off right now -- react :+1: "
                              "or :-1: on the message instead."))
                except Exception:  # noqa: BLE001
                    pass
            return

        outcome, msg = briefing_enrollment.process_enrollment_tap(
            review_id, actor_id, enable=enable)

        if outcome in ("not_authorized", "orphaned", "already_handled",
                       "write_failed"):
            # None of these may edit the shared card. not_authorized must not
            # rewrite Harrison's DM on a stranger's tap; already_handled is the
            # fast RACE LOSER of two taps on one card, and the winner's own edit
            # is the authoritative one (the D-051 terminal-edit race rule).
            # write_failed must leave the buttons LIVE so the tap can be retried.
            try:
                client.chat_postEphemeral(channel=channel_id, user=actor_id, text=msg)
            except Exception:  # noqa: BLE001
                pass
            return

        # Unique winner: keep the briefing body, drop the buttons, append the
        # outcome. Section blocks carry the body across several chunks, so they
        # are all preserved (dropping to a single block would delete most of the
        # briefing Harrison just reviewed).
        if channel_id and message_ts:
            orig = (body.get("message") or {}).get("blocks") or []
            section_blocks = [_strip_reaction_affordance(b)
                              for b in orig if b.get("type") == "section"]
            new_blocks = section_blocks + [
                {"type": "context", "elements": [{"type": "mrkdwn", "text": msg}]}
            ]
            if not section_blocks:
                new_blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": msg}}]
            try:
                client.chat_update(channel=channel_id, ts=message_ts,
                                   text=msg, blocks=new_blocks)
            except Exception as exc:  # noqa: BLE001
                log.warning("briefing enrollment: chat_update failed: %s", exc)
    except Exception:  # noqa: BLE001 -- a handler error must never crash the bot
        log.warning("briefing enrollment handler error (non-fatal)", exc_info=True)


@app.action(briefing_enrollment.ACTION_ENABLE)
def handle_briefing_enable(ack, body, client) -> None:
    ack()
    _handle_briefing_enrollment_tap(body, client, enable=True)


@app.action(briefing_enrollment.ACTION_SKIP)
def handle_briefing_skip(ack, body, client) -> None:
    ack()
    _handle_briefing_enrollment_tap(body, client, enable=False)


def _handle_gap_decline_tap(body: dict, client, *, reason: str) -> None:
    try:
        actions = body.get("actions") or []
        ask_id = (actions[0].get("value") if actions else "") or ""
        actor_id = (body.get("user") or {}).get("id", "")
        channel_id = (body.get("channel") or {}).get("id", "")
        message_ts = (body.get("message") or {}).get("ts", "")

        if os.environ.get("CORA_EVAL_MODE") == "1":
            return

        if not confirm_cards.confirm_buttons_enabled():
            if channel_id and actor_id:
                try:
                    client.chat_postEphemeral(
                        channel=channel_id, user=actor_id,
                        text=("Buttons are turned off right now -- just reply "
                              "\"not my area\" and I'll pick it up."))
                except Exception:  # noqa: BLE001
                    pass
            return

        outcome, msg = gap_autofill.process_decline_tap(
            ask_id, actor_id, reason=reason)

        if outcome in ("not_authorized", "orphaned", "already_handled"):
            # Never edit the shared card: not_authorized is a stranger's tap,
            # already_handled is the fast race loser of two taps (the winner
            # owns the outcome text -- D-051 terminal-edit rule).
            try:
                client.chat_postEphemeral(channel=channel_id, user=actor_id, text=msg)
            except Exception:  # noqa: BLE001
                pass
            return

        # declined | expired: the unique winner closes its own card.
        if channel_id and message_ts:
            orig = (body.get("message") or {}).get("blocks") or []
            section_blocks = [b for b in orig if b.get("type") == "section"]
            new_blocks = section_blocks + [
                {"type": "context", "elements": [{"type": "mrkdwn", "text": msg}]}
            ]
            if not section_blocks:
                new_blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": msg}}]
            try:
                client.chat_update(channel=channel_id, ts=message_ts,
                                   text=msg, blocks=new_blocks)
            except Exception as exc:  # noqa: BLE001
                log.warning("gap decline: chat_update failed: %s", exc)
    except Exception:  # noqa: BLE001 -- a handler error must never crash the bot
        log.warning("gap decline handler error (non-fatal)", exc_info=True)


@app.action(gap_autofill.ACTION_DECLINE_NOT_MINE)
def handle_gap_decline_not_mine(ack, body, client) -> None:
    ack()
    _handle_gap_decline_tap(body, client, reason="not_mine")


@app.action(gap_autofill.ACTION_DECLINE_UNKNOWN)
def handle_gap_decline_unknown(ack, body, client) -> None:
    ack()
    _handle_gap_decline_tap(body, client, reason="unknown")


# ── HubSpot email-sync ambiguous match (S6 migration 3, 2026-08-09) ──────────
# Attach/Skip on the ambiguous-match DM. The 👍/👎 reaction handler above is
# untouched and stays the permanent fallback (locked pattern rule).
#
# Authorization is REQUESTER-SCOPED (the mailbox owner the DM went to), checked
# in hubspot_email_sync.process_match_tap against the real action-payload user.

def _handle_hubspot_match_tap(body: dict, client, *, attach: bool) -> None:
    try:
        actions = body.get("actions") or []
        pending_id = (actions[0].get("value") if actions else "") or ""
        actor_id = (body.get("user") or {}).get("id", "")
        channel_id = (body.get("channel") or {}).get("id", "")
        message_ts = (body.get("message") or {}).get("ts", "")

        if os.environ.get("CORA_EVAL_MODE") == "1":
            return

        if not confirm_cards.confirm_buttons_enabled():
            if channel_id and actor_id:
                try:
                    client.chat_postEphemeral(
                        channel=channel_id, user=actor_id,
                        text=("Buttons are turned off right now -- react "
                              ":+1: to attach or :-1: to skip instead."))
                except Exception:  # noqa: BLE001
                    pass
            return

        from .connectors import hubspot_email_sync as _hes
        outcome, msg = _hes.process_match_tap(pending_id, actor_id, attach=attach)

        if outcome in ("not_authorized", "orphaned"):
            try:
                client.chat_postEphemeral(channel=channel_id, user=actor_id, text=msg)
            except Exception:  # noqa: BLE001
                pass
            return

        if channel_id and message_ts:
            orig = (body.get("message") or {}).get("blocks") or []
            section_blocks = [b for b in orig if b.get("type") == "section"]
            new_blocks = section_blocks + [
                {"type": "context", "elements": [{"type": "mrkdwn", "text": msg}]}
            ]
            if not section_blocks:
                new_blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": msg}}]
            try:
                client.chat_update(channel=channel_id, ts=message_ts,
                                   text=msg, blocks=new_blocks)
            except Exception as exc:  # noqa: BLE001
                log.warning("hubspot match: chat_update failed: %s", exc)
        log.info("email_sync: button %s on pending=%s by %s",
                 "ATTACH" if attach else "SKIP", pending_id, actor_id)
    except Exception:  # noqa: BLE001 -- a handler error must never crash the bot
        log.warning("hubspot match handler error (non-fatal)", exc_info=True)


@app.action(hubspot_email_sync.ACTION_ATTACH)
def handle_hubspot_match_attach(ack, body, client) -> None:
    ack()
    _handle_hubspot_match_tap(body, client, attach=True)


@app.action(hubspot_email_sync.ACTION_SKIP)
def handle_hubspot_match_skip(ack, body, client) -> None:
    ack()
    _handle_hubspot_match_tap(body, client, attach=False)


# ── OSN shift-schedule approve button (S6 migration 4, 2026-08-09) ───────────
# The ✅ reaction handler above is untouched and stays the permanent fallback.
#
# AUTHORITY-SCOPED, not requester-scoped: osn_shift_handler._is_admin, the same
# gate the reaction path uses, applied to the real action-payload user.
#
# The button APPROVES ONLY -- it does not publish. See the note on
# osn_shift_handler.ACTION_APPROVE: publishing DMs every active employee and is
# a separate admin command the reaction path has never performed either.

def _handle_osn_approve_tap(body: dict, client) -> None:
    try:
        actions = body.get("actions") or []
        schedule_id = (actions[0].get("value") if actions else "") or ""
        actor_id = (body.get("user") or {}).get("id", "")
        channel_id = (body.get("channel") or {}).get("id", "")
        message_ts = (body.get("message") or {}).get("ts", "")

        if os.environ.get("CORA_EVAL_MODE") == "1":
            return

        if not confirm_cards.confirm_buttons_enabled():
            if channel_id and actor_id:
                try:
                    client.chat_postEphemeral(
                        channel=channel_id, user=actor_id,
                        text=("Buttons are turned off right now -- react "
                              ":white_check_mark: on the schedule instead."))
                except Exception:  # noqa: BLE001
                    pass
            return

        outcome, msg = osn_shift_handler.process_schedule_approval_tap(
            schedule_id, actor_id)

        if outcome in ("not_authorized", "orphaned", "already_handled"):
            try:
                client.chat_postEphemeral(channel=channel_id, user=actor_id, text=msg)
            except Exception:  # noqa: BLE001
                pass
            return

        if channel_id and message_ts:
            orig = (body.get("message") or {}).get("blocks") or []
            section_blocks = [b for b in orig if b.get("type") == "section"]
            new_blocks = section_blocks + [
                {"type": "context", "elements": [{"type": "mrkdwn", "text": msg}]}
            ]
            if not section_blocks:
                new_blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": msg}}]
            try:
                client.chat_update(channel=channel_id, ts=message_ts,
                                   text=msg, blocks=new_blocks)
            except Exception as exc:  # noqa: BLE001
                log.warning("osn approve: chat_update failed: %s", exc)
    except Exception:  # noqa: BLE001 -- a handler error must never crash the bot
        log.warning("osn approve handler error (non-fatal)", exc_info=True)


@app.action(osn_shift_handler.ACTION_APPROVE)
def handle_osn_schedule_approve(ack, body, client) -> None:
    ack()
    _handle_osn_approve_tap(body, client)


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


def _card_preview_text(orig_blocks: list[dict]) -> str:
    """The card's original preview text, recovered from its rendered blocks --
    used to re-register a card whose terminal edit failed, so a later sweep can
    retry with the same preview visible."""
    for b in orig_blocks or []:
        if b.get("type") == "section":
            return ((b.get("text") or {}).get("text") or "")
    return ""


def _edit_card_terminal(client, channel_id: str, message_ts: str, orig_blocks: list[dict],
                        outcome_text: str) -> bool:
    """Edit a confirm/picker card to a terminal state: keep the original
    preview/question section(s), append the outcome, drop the actions block
    (buttons gone -- revops/code-queue precedent).

    Callers MUST be the unique claim winner for this message (S2 fix,
    live-smoke 2026-08-02 + D-051 re-review): a same-card race-LOSER outcome
    (already_handled for Confirm/Cancel, superseded for the picker) must
    never call this -- it should go ephemeral-only instead, since there is no
    way to order two independent chat_update calls against each other, and
    the loser's edit is never necessary (the winner always edits the card
    with the real outcome).

    Returns True when the edit landed. D-051 lens-1 MEDIUM: the callers pop the
    card's ONLY coordinate out of the registry before calling this, and a
    swallowed failure here (a chat.update 429 -- Tier 3, ~50 RPM, and card edits
    are not throttled -- or any transient error) then left a live-buttoned card
    over a dead stash that NOTHING could ever close again, which is the exact
    symptom S1 exists to remove. Callers re-register the coordinate on False so
    the next sweep retries."""
    section_blocks = [b for b in orig_blocks if b.get("type") == "section"]
    new_blocks = section_blocks + confirm_cards.terminal_blocks(outcome_text)
    try:
        client.chat_update(channel=channel_id, ts=message_ts, text=outcome_text, blocks=new_blocks)
        return True
    except Exception:  # noqa: BLE001
        log.warning("confirm-button card edit failed (non-fatal) channel=%s ts=%s",
                    channel_id, message_ts, exc_info=True)
        return False


def _post_followup_confirm_card(client, channel_id: str, text: str, stash_id: str) -> None:
    """Post a NEW Confirm/Cancel card for a freshly-minted stash_id. Shared by
    the picker's pick -> preview hand-off AND a confirm-tap whose own execute
    declined to write and re-stashed a fresh preview instead (D-051 review:
    both cases need the SAME "a fresh stash appeared, give it a card" step)."""
    # v2 S1: same one-card-per-stash claim the reply sites use. A refused claim
    # means something already carded this stash; post the text without buttons
    # rather than a second live copy.
    carded = confirm_cards.claim_card_attach(stash_id)
    blocks = confirm_cards.build_confirm_blocks(text, stash_id) if carded else None
    try:
        resp = client.chat_postMessage(channel=channel_id, text=text, blocks=blocks)
        if carded:
            confirm_cards.register_card(stash_id, channel_id, (resp or {}).get("ts", ""), text)
    except Exception:  # noqa: BLE001
        log.warning("confirm-button follow-up card post failed (non-fatal) channel=%s",
                    channel_id, exc_info=True)


# Neutral, honest terminal line for a card closed by the sweep rather than by a
# tap. The sweep only knows THAT the stash left its store, never by which route
# (typed confirm, typed cancel, supersede, expiry all look identical from here),
# so the copy must not claim an outcome it cannot see. The reply that actually
# handled it is already in the conversation directly above.
_CARD_CLOSED_BY_SWEEP = (
    "_Handled in the conversation -- these buttons are closed._"
)

# D-051 lens-2 MEDIUM: expiry is the one terminal route with NO reply above the
# card, so the generic line would assert an outcome that never happened -- and
# on a destructive stash (delete a task, cancel an event, forget a note) the
# user reads "handled" as "it went through". Separate, explicitly negative copy.
_CARD_CLOSED_BY_EXPIRY = (
    "_This expired before you confirmed. Nothing was changed -- "
    "ask me again and I'll re-preview it._"
)


def _close_stale_confirm_cards(client) -> None:
    """Take the buttons down on every rendered card whose stash is no longer
    live (v2 S1, cq-fee6c9764950).

    v1 could only close a card from a tap on that same card, so a user who
    TYPED "confirm"/"cancel" -- or whose next request superseded the pending --
    was left looking at live-looking Confirm/Cancel buttons over a stash that no
    longer existed. Called after every reply post and after every tap, so a
    stash consumed by ANY route has its card closed within the same turn.

    Best-effort throughout: a failed edit just leaves that card visually stale,
    and tapping it still resolves honestly through resolve_and_claim_stash."""
    # D-051 lens-2 LOW: the sweep is a NEW Slack-write surface reached from
    # _dispatch_qa, which missed_message_catchup drives with CORA_EVAL_MODE=1.
    # It was safe only by accident (separate process, overridden client); make
    # it safe by construction, matching every other write path's own gate.
    if os.environ.get("CORA_EVAL_MODE") == "1":
        return
    if not confirm_cards.confirm_buttons_enabled():
        return
    try:
        open_ids = confirm_cards.open_card_stash_ids()
    except Exception:  # noqa: BLE001
        return
    for sid in open_ids:
        try:
            if _tool_dispatch.stash_is_live(sid):
                continue
            expired = _tool_dispatch.stash_expired_not_consumed(sid)
            outcome = _CARD_CLOSED_BY_EXPIRY if expired else _CARD_CLOSED_BY_SWEEP
            # pop_cards hands each coordinate over exactly once, so two racing
            # sweeps can never both chat_update the same message.
            for card_channel, card_ts, preview in confirm_cards.pop_cards(sid):
                preview_blocks = (
                    [{"type": "section", "text": {"type": "mrkdwn", "text": preview}}]
                    if preview else []
                )
                if _edit_card_terminal(client, card_channel, card_ts, preview_blocks, outcome):
                    log.info("confirm_card closed by sweep stash=%s channel=%s reason=%s",
                             sid, card_channel, "expired" if expired else "handled")
                else:
                    # Put the coordinate back so a later sweep retries -- without
                    # this, a transient chat_update failure orphans a live-buttoned
                    # card permanently (D-051 lens-1 MEDIUM).
                    confirm_cards.register_card(sid, card_channel, card_ts, preview)
        except Exception:  # noqa: BLE001 -- card hygiene must never break a reply
            log.warning("confirm-card sweep error stash=%s (non-fatal)", sid, exc_info=True)


def _handle_confirm_tap(body: dict, client, *, action: str,
                        stash_id_override: str | None = None,
                        slot_index: int | None = None,
                        item_index: int | None = None) -> None:
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
    # A slot tap's raw value is "{stash_id}:{slot_index}", already split by the
    # slot handler -- everything downstream sees a bare stash_id, exactly as a
    # plain Confirm tap does (v2 S2).
    stash_id = stash_id_override if stash_id_override is not None else str(actions[0].get("value") or "")

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
    # Resolve the kind BEFORE the claim: the claim consumes the stash, and the
    # log line below wants to name what was acted on (cq-b8a4d7b9dd4a).
    _idx = confirm_cards.index_lookup(stash_id) or {}
    result = _tool_dispatch.resolve_and_claim_stash(
        stash_id, tapping_user, action, slot_index=slot_index,
        item_index=item_index,
        # v2b S5: an item tap hands over only ITS OWN card coordinate at claim
        # time, so its siblings stay registered and sweepable.
        card_coords=(channel_id, message_ts) if item_index is not None else None)
    outcome = result.get("outcome")

    # cq-b8a4d7b9dd4a: a tapped Cancel used to write NOTHING to the log, while a
    # typed cancel logs "confirm_interceptor CANCEL" and a typed confirm logs
    # EXECUTE -- so a card that vanished could not be attributed to a person or
    # even distinguished from an expiry. One symmetric line for EVERY tap
    # outcome, on the button path, at INFO. Deliberately payload-free: the kind
    # and the opaque stash id, never the previewed content.
    log.info("confirm_card TAP action=%s outcome=%s kind=%s stash=%s user=%s%s",
             action, outcome, _idx.get("kind", "?"), stash_id, tapping_user,
             f" item={item_index}" if item_index is not None else
             (f" slot={slot_index}" if slot_index is not None else ""))

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
        # Coordinates were popped inside resolve_and_claim_stash, at claim
        # time, so no concurrent sweep can be mid-flight against this card.
        _edit_card_terminal(client, channel_id, message_ts, orig_blocks, text)
        fresh_stash_id = result.get("stash_id")
        if fresh_stash_id:
            _post_followup_confirm_card(client, channel_id, text, fresh_stash_id)
        _close_stale_confirm_cards(client)
        return

    if outcome == "superseded":
        text = "This preview was replaced by a newer one -- check your latest message."
    elif outcome == "expired":
        label = result.get("label", "that request")
        text = (f"That {label} expired before you confirmed. Nothing was "
                f"changed -- tell me again and I'll re-preview it.")
    elif outcome == "cancelled":
        # v2b S5: an item Skip dismisses ONE item, and the sibling cards next to
        # it are still live -- "nothing was changed" would read as "I cancelled
        # the whole thing" when the others are still awaiting a tap.
        text = ("Skipped -- no task created for this one." if item_index is not None
                else "Cancelled -- nothing was changed.")
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
    #
    # Discard this stash's registered coordinates FIRST (v2 S1): the tap writes
    # the real outcome ("Done: ..."), and the generic sweep below must never
    # come along afterwards and overwrite it with its vaguer "handled in the
    # conversation" line.
    # This tap owns the outcome text. Its coordinates left the registry at
    # CLAIM time (resolve_and_claim_stash), before execute ran, so the
    # process-global sweep could never race this edit.
    if not _edit_card_terminal(client, channel_id, message_ts, orig_blocks, text):
        # Re-register THIS card so a later sweep retries, rather than
        # orphaning live buttons over a stash that is already gone. The
        # coordinates are the tap's own, straight off the action payload.
        confirm_cards.register_card(stash_id, channel_id, message_ts,
                                    _card_preview_text(orig_blocks))
    _close_stale_confirm_cards(client)


@app.action(confirm_cards.ACTION_CONFIRM)
def handle_confirm_write(ack, body, client) -> None:
    ack()
    _handle_confirm_tap(body, client, action="confirm")


@app.action(confirm_cards.ACTION_CANCEL)
def handle_cancel_write(ack, body, client) -> None:
    ack()
    _handle_confirm_tap(body, client, action="cancel")


@app.action(confirm_cards.ACTION_PICK_SLOT)
def handle_pick_slot(ack, body, client) -> None:
    """Meeting-slot tap (v2 S2): "{stash_id}:{slot_index}". Split here, then run
    the SAME claim/authorize/execute path a plain Confirm takes -- the slot index
    is bounds-checked against the stash's own offered list server-side, so a
    forged or stale index can only ever address a slot this stash really
    offered."""
    ack()
    actions = body.get("actions") or [{}]
    raw = str(actions[0].get("value") or "")
    sid, _, idx_raw = raw.partition(":")
    if not sid or not idx_raw.isdigit():
        return
    _handle_confirm_tap(body, client, action="confirm",
                        stash_id_override=sid, slot_index=int(idx_raw))


def _handle_item_tap(body: dict, client, *, action: str) -> None:
    """Meeting per-item tap (v2b S5): "{stash_id}:{item_index}". Split here, then
    run the SAME claim/authorize path a plain Confirm takes -- the index is
    bounds-checked against the stash's OWN verified list server-side, so a forged
    or stale index can only ever address an item this stash really offered."""
    actions = body.get("actions") or [{}]
    raw = str(actions[0].get("value") or "")
    sid, _, idx_raw = raw.partition(":")
    if not sid or not idx_raw.isdigit():
        return
    _handle_confirm_tap(body, client, action=action,
                        stash_id_override=sid, item_index=int(idx_raw))


@app.action(confirm_cards.ACTION_CONFIRM_ITEM)
def handle_confirm_item(ack, body, client) -> None:
    ack()
    _handle_item_tap(body, client, action="confirm")


@app.action(confirm_cards.ACTION_CANCEL_ITEM)
def handle_cancel_item(ack, body, client) -> None:
    ack()
    _handle_item_tap(body, client, action="cancel")


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
        confirm_cards.pop_cards(ask_id)  # this tap owns the outcome text (v2 S1)
        _edit_card_terminal(client, channel_id, message_ts, orig_blocks,
                            "That option didn't resolve cleanly -- restate the item and I'll ask again.")
        _close_stale_confirm_cards(client)
        return

    # outcome == "preview": close the picker card, then post the fresh
    # preview as ITS OWN Confirm/Cancel card (pick -> preview -> confirm). A
    # refusal from the resolution tail (e.g. "not stocked here anymore") has
    # no stash_id -- nothing to confirm -- so it posts as plain text instead.
    confirm_cards.pop_cards(ask_id)  # this tap owns the outcome text (v2 S1)
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
    _close_stale_confirm_cards(client)


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
