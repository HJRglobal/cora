"""Deterministic knowledge-gap detection at the _dispatch_qa chokepoint (WS-1).

The LLM-sentinel path ([CORA_KNOWLEDGE_GAP: ...]) is behaviorally unreliable
as the ONLY intake for the knowledge flywheel: 44 gaps ever logged, fading as
the KB grew to ~560K chunks (retrieval nearly always returns *something*, so
the model rarely "feels" a gap). Prompt-only INSTRUMENTATION is insufficient
for a load-bearing signal -- the same lesson D-034 locked for enforcement.

Two deterministic detectors run on every LLM-generated Q&A response, in code:

  kb_miss          -- KB retrieval ran and returned 0 chunks under the live
                      distance gate (no personal-note hit, no cross-entity
                      fallback, no tool involvement) for a substantive question.
  unknown_response -- the whole reply is the locked UNKNOWN_RESPONSE phrase or
                      a clear short "I don't have that" shape.

The existing sentinel path is KEPT as belt-and-braces; its records now carry
detector="llm_sentinel".

Noise / leak controls (all deterministic, all fail-toward-NOT-logging; the
gap log later flows to Haiku drafting in gap_autofill, owner-escalation DMs,
and eval seeds -- review lens R1):

  - Deterministic guard refusals (user_access / sibling_guard /
    cross_entity_guard / finance-tier / historical_access) structurally CANNOT
    reach this hook: every guard returns before _dispatch_qa calls the LLM.
    What CAN reach it are LLM-generated deflections enforced by the prompts;
    those are vetoed by _DEFLECTION_RES and never logged as gaps.
  - LEX* entities never enter deterministic detection (fail-closed): a LEX
    question's text may embed client identifiers. The sentinel path's existing
    behavior is unchanged; gap_autofill already never escalates LEX gaps.
  - PHI-flagged question text is never logged (is_phi_risk on the question).
  - Smalltalk / non-substantive messages are skipped.
  - Per-(entity, normalized question) dedup with a 7-day window; a global
    daily cap (default 15) with an overflow counter, persisted in
    data/state/gap_detection_state.json.
  - One detection per thread root (in-memory, pruned) so a multi-turn thread
    probing the same missing fact logs once.
  - DM-originated detections carry private_source=True; gap_autofill never
    escalates a DM-originated gap to a domain owner (mining stays allowed --
    its output is Harrison-gated, D-011).
  - Eval-context calls (CORA_EVAL_MODE=1) never log.

The detectors store ONLY what the sentinel path already stores (entity,
channel name, user id, question text, response length, generic gap
description, latency) plus the detector tag and the private_source flag --
never response text, never KB chunk content.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock

from . import knowledge_gaps
from .phi_guard import is_phi_risk

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Daily cap on deterministically-detected gaps (sentinel records don't count).
_DEFAULT_DAILY_CAP = 15
# Dedup window for a repeated (entity, normalized question).
_DEDUP_WINDOW_DAYS = 7
# One detection per thread root; prune in-memory entries older than this.
_THREAD_TTL_SECONDS = 48 * 3600

_STATE_LOCK = Lock()
_THREAD_LOCK = Lock()
_THREAD_LOGGED: dict[str, float] = {}


def _state_path() -> Path:
    return Path(os.environ.get("GAP_DETECTION_STATE_PATH")
                or _REPO_ROOT / "data" / "state" / "gap_detection_state.json")


def _daily_cap() -> int:
    try:
        return int(os.environ.get("CORA_GAP_DETECT_DAILY_CAP", _DEFAULT_DAILY_CAP))
    except ValueError:
        return _DEFAULT_DAILY_CAP


# ---------------------------------------------------------------------------
# Question-side filters
# ---------------------------------------------------------------------------

_MENTION_RE = re.compile(r"<@[A-Z0-9]+>")
_SMALLTALK_RE = re.compile(
    r"^(hi|hiya|hello|hey|yo|sup|thanks?|thank you|thx|ty|ok(ay)?|k|cool|nice|"
    r"great|perfect|awesome|good (morning|afternoon|evening|night)|gm|"
    r"got it|sounds good|will do|no problem|np|lol|haha+|yes|no|yep|nope|"
    r"sure|done|test(ing)?|ping)[\s!.,;:)?]*$",
    re.IGNORECASE,
)


def normalize_question(question: str) -> str:
    """Lowercased, mention-stripped, punctuation-collapsed form for dedup keys."""
    q = _MENTION_RE.sub("", question or "")
    q = q.lower()
    q = re.sub(r"[^a-z0-9]+", " ", q)
    return " ".join(q.split())


def is_substantive(question: str) -> bool:
    """True for questions worth gap-logging; smalltalk/acks/one-worders are not."""
    q = _MENTION_RE.sub("", question or "").strip()
    if len(q) < 12:
        return False
    if _SMALLTALK_RE.match(q):
        return False
    words = [w for w in re.split(r"\s+", q) if w]
    if len(words) < 3:
        return False
    if not re.search(r"[a-zA-Z]", q):
        return False
    return True


# ---------------------------------------------------------------------------
# Response-side shapes
# ---------------------------------------------------------------------------

# Locked finance unknown-answer phrase. Duplicated from
# cora.tools.financial_client.UNKNOWN_RESPONSE (importing that module here
# would pull the whole finance connector stack into the hot Q&A path); a pin
# test asserts the two never drift.
UNKNOWN_RESPONSE_TEXT = (
    "I don't have that right now. I will notify the finance department "
    "immediately to obtain the information and provide the correct and "
    "updated answer when you ask again."
)

# A genuine unknown/no-data reply is one short statement, per the prompt
# contract ("respond with this exact text and nothing else").
_UNKNOWN_MAX_CHARS = 350
_UNKNOWN_RES = [
    re.compile(r"^i don'?t have (that|this|any|it)\b", re.IGNORECASE),
    re.compile(r"^i don'?t have (visibility|information|data|details|a record|records)\b",
               re.IGNORECASE),
    re.compile(r"^i (couldn'?t|could not|can'?t|cannot) find (any|that|a|the)\b",
               re.IGNORECASE),
    re.compile(r"^i have no (record|information|data|details)\b", re.IGNORECASE),
]

# LLM-generated deflection shapes (prompt-enforced refusals + tool-gate relays
# the LLM passes through). A deflection is a refusal WORKING AS DESIGNED, not
# a knowledge gap -- and its question text must never enter the gap log.
# Inventory sourced from _UNIVERSAL_RULES (prompt_loader.py), the per-entity
# prompts, user_access redirect texts (which the prompts mirror), the
# tool-level TIER_1 gate strings (tool_dispatch.py), and the tech-stack
# guardrail. Matching is anywhere-in-reply but only for short replies (a real
# deflection is one or two sentences).
_DEFLECTION_MAX_CHARS = 400
_DEFLECTION_RES = [
    re.compile(p, re.IGNORECASE) for p in (
        r"that'?s company financials",
        r"ask (me )?in (an? )?#\S+",
        r"ask in the channel for the team",
        r"that'?s a legal matter",
        r"reach emily stubbs",
        r"that'?s hr\b",
        r"hr matters go to",
        r"bring it to hannah grant",
        r"client-specific health info stays in the ehr",
        r"ask the clinical lead",
        r"ownership details need harrison",
        r"that needs harrison",
        r"all media goes through harrison",
        r"i don'?t speculate",
        r"i'?m not able to discuss that",
        r"financial details are only available in this entity'?s dedicated "
        r"finance channel",
        r"quickbooks financial data is available in tier_?1 channels only",
        r"outside what i can help with in this channel",
        r"outside your access scope",
        r"i'?m scoped to [^.\n]{1,80} here",
        r"cannot be discussed here",
        r"can'?t discuss company financials here",
        r"this channel is for morning briefs only",
        r"go(es)? in a finance channel",
    )
]


def _normalize_reply(text: str) -> str:
    """Fold typographic quotes so shape regexes match either form."""
    return (text or "").replace("’", "'").replace("‘", "'").strip()


def is_deflection(response_text: str) -> bool:
    reply = _normalize_reply(response_text)
    if not reply or len(reply) > _DEFLECTION_MAX_CHARS:
        return False
    return any(rx.search(reply) for rx in _DEFLECTION_RES)


def is_unknown_response(response_text: str) -> bool:
    reply = _normalize_reply(response_text)
    if not reply or len(reply) > _UNKNOWN_MAX_CHARS:
        return False
    if _normalize_reply(UNKNOWN_RESPONSE_TEXT) in reply:
        return True
    return any(rx.match(reply) for rx in _UNKNOWN_RES)


# ---------------------------------------------------------------------------
# Dedup + daily cap state
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(path)


def _dedup_key(entity: str, question: str) -> str:
    normalized = normalize_question(question)
    raw = f"{(entity or 'FNDR').upper()}|{normalized}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


def register_detection(entity: str, question: str) -> bool:
    """Atomically apply the 7d dedup window + the global daily cap.

    Returns True when the caller may log this gap. On a cap hit the overflow
    counter increments (surfaced by the flywheel telemetry) and the dedup key
    is still recorded so a capped question doesn't re-contend tomorrow.
    """
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    key = _dedup_key(entity, question)
    with _STATE_LOCK:
        state = _load_state()
        if state.get("day") != today:
            state["day"] = today
            state["count"] = 0
            state["overflow"] = 0
        recent = state.get("recent") or {}
        # Prune entries past the dedup window.
        cutoff = now - timedelta(days=_DEDUP_WINDOW_DAYS)
        pruned = {}
        for k, iso in recent.items():
            try:
                seen = datetime.fromisoformat(iso)
                if seen.tzinfo is None:
                    seen = seen.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if seen >= cutoff:
                pruned[k] = iso
        recent = pruned
        if key in recent:
            state["recent"] = recent
            _save_state(state)
            return False
        recent[key] = now.isoformat()
        state["recent"] = recent
        if int(state.get("count") or 0) >= _daily_cap():
            state["overflow"] = int(state.get("overflow") or 0) + 1
            _save_state(state)
            log.info(
                "gap_detection: daily cap (%d) reached -- overflow=%d today",
                _daily_cap(), state["overflow"],
            )
            return False
        state["count"] = int(state.get("count") or 0) + 1
        _save_state(state)
        return True


def _thread_already_logged(thread_key: str) -> bool:
    if not thread_key:
        return False
    now = time.time()
    with _THREAD_LOCK:
        for k in [k for k, ts in _THREAD_LOGGED.items()
                  if now - ts > _THREAD_TTL_SECONDS]:
            _THREAD_LOGGED.pop(k, None)
        return thread_key in _THREAD_LOGGED


def _mark_thread_logged(thread_key: str) -> None:
    if not thread_key:
        return
    with _THREAD_LOCK:
        _THREAD_LOGGED[thread_key] = time.time()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def maybe_log_gap(
    *,
    entity: str,
    channel: str,
    user: str | None,
    question: str,
    response_text: str,
    latency_ms: int,
    kb_meta: dict | None = None,
    gen_meta: dict | None = None,
    is_dm: bool = False,
    thread_key: str = "",
) -> str | None:
    """Run the deterministic detectors; log at most one gap. Never raises.

    Returns the detector name when a gap was logged, else None.
    """
    try:
        return _maybe_log_gap_inner(
            entity=entity, channel=channel, user=user, question=question,
            response_text=response_text, latency_ms=latency_ms,
            kb_meta=kb_meta or {}, gen_meta=gen_meta or {},
            is_dm=is_dm, thread_key=thread_key,
        )
    except Exception:  # noqa: BLE001 -- instrumentation must never break Q&A
        log.warning("gap_detection: detector error (non-fatal)", exc_info=True)
        return None


def _maybe_log_gap_inner(
    *,
    entity: str,
    channel: str,
    user: str | None,
    question: str,
    response_text: str,
    latency_ms: int,
    kb_meta: dict,
    gen_meta: dict,
    is_dm: bool,
    thread_key: str,
) -> str | None:
    # Eval harness traffic never logs gaps (WS-3 isolation, lens R3).
    if os.environ.get("CORA_EVAL_MODE") == "1":
        return None

    # LEX fail-closed (lens R1): a LEX question's text may embed client
    # identifiers, and this log flows to Haiku drafting + owner DMs + eval
    # seeds. The custodian-scoped sentinel path is unchanged.
    ent = (entity or "FNDR").strip().upper()
    if ent.startswith("LEX"):
        log.debug("gap_detection: LEX entity -- deterministic detection skipped")
        return None

    if not is_substantive(question):
        return None

    # Belt-and-braces: PHI-flagged question text never enters the gap log.
    if is_phi_risk(question):
        log.debug("gap_detection: question flagged PHI -- skipped")
        return None

    # A deflection is a guard working as designed, not a gap -- and its
    # question is likely a blocked topic; drop entirely.
    if is_deflection(response_text):
        return None

    detector: str | None = None
    gap_desc = ""
    if is_unknown_response(response_text):
        detector = "unknown_response"
        gap_desc = "Reply was an unknown/no-data response"
    elif (
        kb_meta.get("kb_search_ran")
        and int(kb_meta.get("kb_relevant_hits") or 0) == 0
        and not kb_meta.get("kb_notes_hit")
        and not kb_meta.get("cross_entity_fallback")
        and not gen_meta.get("used_tools")
    ):
        # Retrieval genuinely missed and no tool supplied the answer.
        detector = "kb_miss"
        gap_desc = ("KB retrieval returned no relevant content "
                    "(0 chunks passed the distance gate)")
    if detector is None:
        return None

    if _thread_already_logged(thread_key):
        return None
    if not register_detection(ent, question):
        return None

    knowledge_gaps.log_gap(
        entity=ent,
        channel=channel,
        user=user,
        question=question,
        response_chars=len(response_text or ""),
        gap=gap_desc,
        latency_ms=latency_ms,
        detector=detector,
        private_source=is_dm,
    )
    _mark_thread_logged(thread_key)
    log.info("gap_detection: gap logged detector=%s entity=%s channel=#%s",
             detector, ent, channel)
    return detector
