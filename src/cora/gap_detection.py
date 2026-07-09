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
  unknown_response -- the reply OPENS with the locked UNKNOWN_RESPONSE phrase
                      or a prefix-anchored "I don't have that / I couldn't
                      find / I have no record" shape (length-INDEPENDENT: Cora's
                      answer-first house style, the 2026-06-30 format standard,
                      pads a genuine miss reply to 550-700 chars), OR a short
                      reply that merely contains the locked finance phrase.

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
from .phi_guard import is_phi_risk, is_clinical_phi, is_lex_billing_status_phi

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Daily cap on deterministically-detected gaps (sentinel records don't count).
_DEFAULT_DAILY_CAP = 15
# Dedup window for a repeated (entity, normalized question).
_DEDUP_WINDOW_DAYS = 7
# One detection per thread root; prune in-memory entries older than this.
_THREAD_TTL_SECONDS = 48 * 3600

# ── kb_miss SHADOW calibration floor (D-066 follow-up; 2026-07-09 slice) ──────
# PROVISIONAL, NOT LOCKED. The real kb_miss detector is UNCHANGED (still fires
# only when kb_relevant_hits == 0, empirically unreachable at ~570K chunks). To
# escape the chicken-and-egg -- we can't pick a real floor without a distance
# distribution, and we have no distribution because kb_miss never fires -- this
# module now (a) logs best_distance for EVERY answerable retrieval to a decision
# stream, and (b) computes a SHADOW kb_miss verdict against this provisional
# floor, LOG-ONLY. The shadow verdict NEVER DMs, routes, writes, or feeds
# knowledge_gaps.log_gap; it only annotates the decision-log line. Harrison
# calibrates the REAL floor ~1 week out from the collected distribution -- do
# NOT wire this number into any gating path. Env override for experimentation.
_DEFAULT_SHADOW_KB_MISS_FLOOR = 1.10


def _shadow_kb_miss_floor() -> float:
    try:
        return float(os.environ.get("CORA_KB_MISS_SHADOW_FLOOR",
                                    _DEFAULT_SHADOW_KB_MISS_FLOOR))
    except (TypeError, ValueError):
        return _DEFAULT_SHADOW_KB_MISS_FLOOR


_STATE_LOCK = Lock()
_THREAD_LOCK = Lock()
_DECISION_LOG_LOCK = Lock()
_THREAD_LOGGED: dict[str, float] = {}


def _state_path() -> Path:
    return Path(os.environ.get("GAP_DETECTION_STATE_PATH")
                or _REPO_ROOT / "data" / "state" / "gap_detection_state.json")


def _decision_log_path() -> Path:
    """Per-query retrieval-decision stream (env-overridable for tests). Distinct
    from the gap log: it records the retrieval OUTCOME (best_distance, counts,
    shadow verdict) for EVERY answerable query -- including the ones that got a
    good answer -- so the answerable distance distribution exists for the ~1-week
    kb_miss floor calibration. Numeric + entity/channel only; never any question
    or response text (PHI-safe by construction)."""
    return Path(os.environ.get("KB_DECISION_LOG_PATH")
                or _REPO_ROOT / "logs" / "kb-retrieval-decisions.jsonl")


def _daily_cap() -> int:
    try:
        return int(os.environ.get("CORA_GAP_DETECT_DAILY_CAP", _DEFAULT_DAILY_CAP))
    except ValueError:
        return _DEFAULT_DAILY_CAP


def _log_retrieval_decision(
    *, entity: str, channel: str, kb_meta: dict, gen_meta: dict,
    thread_context: bool,
) -> bool:
    """Append one retrieval-decision record; compute the SHADOW kb_miss verdict.

    Fires for every answerable query where a KB search ran (kb_search_ran).
    Returns the shadow_kb_miss verdict (for logging by the caller). NEVER raises
    -- a decision-log I/O error must not affect gap logging or the Q&A reply.
    Shadow verdict is LOG-ONLY: it does not gate, DM, route, or write.
    """
    try:
        if not kb_meta.get("kb_search_ran"):
            return False
        best_distance = kb_meta.get("kb_best_distance")
        chunks_returned = kb_meta.get("kb_chunks_returned")
        floor = _shadow_kb_miss_floor()
        # Shadow kb_miss: the closest chunk was weaker than the provisional floor
        # AND no other answer source was in play (mirrors the real kb_miss
        # "no other source" guards, but keyed on distance instead of the
        # unreachable relevant_hits==0). LOG-ONLY.
        shadow_kb_miss = bool(
            best_distance is not None
            and best_distance > floor
            and not kb_meta.get("kb_notes_hit")
            and not kb_meta.get("cross_entity_fallback")
            and not gen_meta.get("used_tools")
            and not thread_context
        )
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "entity": entity,
            "channel": channel,
            "best_distance": best_distance,
            "chunks_returned": chunks_returned,
            "relevant_hits": kb_meta.get("kb_relevant_hits"),
            "notes_hit": bool(kb_meta.get("kb_notes_hit")),
            "cross_entity_fallback": bool(kb_meta.get("cross_entity_fallback")),
            "used_tools": bool(gen_meta.get("used_tools")),
            "thread_context": bool(thread_context),
            "shadow_kb_miss": shadow_kb_miss,
            "shadow_floor": floor,
        }
        path = _decision_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with _DECISION_LOG_LOCK:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return shadow_kb_miss
    except Exception:  # noqa: BLE001 -- instrumentation must never break Q&A
        log.warning("gap_detection: decision-log error (non-fatal)", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Question-side filters
# ---------------------------------------------------------------------------

_MENTION_RE = re.compile(r"<@[A-Z0-9]+>")
_SMALLTALK_RE = re.compile(
    r"^(hi|hiya|hello|hey|yo|sup|thanks?|thank you|thx|ty|ok(ay)?|k|cool|nice|"
    r"great|perfect|awesome|good (morning|afternoon|evening|night)( (team|all|everyone))?|gm|"
    r"got it|sounds good|will do|no problem|np|lol|haha+( that'?s \w+)?|yes|no|yep|nope|"
    r"sure|done|test(ing)?|ping|"
    r"can you help( me)?( real quick| out)?|what do you think|"
    r"thanks so much( for the help)?|appreciate (it|you))[\s!.,;:)?]*$",
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

# Short-reply guard for the ANYWHERE-in-reply containment path ONLY (a long
# reply may quote/embed the locked phrase mid-text rather than assert it). The
# prefix-anchored openers below run length-INDEPENDENTLY: an answer-first miss
# reply that OPENS with the shape but runs 550-700 chars (Cora's 2026-06-30
# format standard) was being silently skipped by a blanket length gate here --
# the exact 2026-07-02 #cora-build smoke miss (D-066 follow-up).
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

# Two classes, because the veto must behave differently for a reply that OPENS
# with a genuine-unknown shape vs a pure refusal:
#
#   POINTER  -- ambiguous "go ask over there" phrases. Cora's answer-first house
#               style (2026-06-30 format standard) appends a channel pointer to
#               GENUINE unknown replies as helpfulness -- "I don't have that.
#               ...or ask in #hjrg." So a pointer must NOT veto a reply that
#               OPENS unknown-shaped (2026-07-02 ROUND-2 addendum: the 351-char
#               plant-watering miss was wrongly vetoed by exactly this trailing
#               pointer). A pointer STILL vetoes a NON-unknown reply (a pure
#               redirect that leads with it) via is_deflection() below.
#   REASON   -- unambiguous refusal reasons. A genuine "I don't have that"
#               opener never carries one of these as helpfulness, so they ALWAYS
#               veto -- even when the reply is unknown-shaped and even past the
#               400-char cap.
_DEFLECTION_POINTER_RES = [
    re.compile(p, re.IGNORECASE) for p in (
        r"ask (me )?in (an? )?#\S+",
    )
]
_DEFLECTION_REASON_RES = [
    re.compile(p, re.IGNORECASE) for p in (
        r"that'?s company financials",
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
# Full set preserves the original anywhere-in-reply veto for is_deflection(),
# which is applied to NON-unknown replies (pure redirects) -- both classes veto
# a short pure refusal exactly as before.
_DEFLECTION_RES = _DEFLECTION_POINTER_RES + _DEFLECTION_REASON_RES


def _normalize_reply(text: str) -> str:
    """Fold typographic quotes AND strip emphasis markers so shape regexes match
    either form. Detection runs on RAW pre-format_reply LLM output, and the
    prompt contract sanctions interior *bold* -- "That's *company financials*"
    must still match the deflection veto (adversarial review MEDIUM: bold
    markers split every verbatim-anchored pattern)."""
    t = (text or "").replace("’", "'").replace("‘", "'")
    t = t.replace("*", "").replace("_", "").replace("`", "")
    return t.strip()


def is_deflection(response_text: str) -> bool:
    reply = _normalize_reply(response_text)
    if not reply or len(reply) > _DEFLECTION_MAX_CHARS:
        return False
    return any(rx.search(reply) for rx in _DEFLECTION_RES)


def _opens_unknown(reply_norm: str) -> bool:
    """True when an already-normalized reply BEGINS with an unknown/no-data shape
    (the locked finance phrase or a prefix-anchored _UNKNOWN_RES opener). Shared
    by is_unknown_response and the deflection-veto scoping in _maybe_log_gap_inner
    so both agree on what counts as a genuine-miss opener. The regexes are all
    START-anchored, so this fires only on the reply's OWN opening verdict, never
    on a mid-text quote."""
    if not reply_norm:
        return False
    locked = _normalize_reply(UNKNOWN_RESPONSE_TEXT)
    return reply_norm.startswith(locked) or any(
        rx.match(reply_norm) for rx in _UNKNOWN_RES)


def is_unknown_response(response_text: str) -> bool:
    reply = _normalize_reply(response_text)
    if not reply:
        return False
    locked = _normalize_reply(UNKNOWN_RESPONSE_TEXT)
    # (1) Length-INDEPENDENT: the reply OPENS with an unknown/no-data shape.
    # A genuine miss reply that opens "I don't have that ..." then adds
    # house-style pointers (550-700 chars) MUST detect; the old blanket length
    # gate hid it (D-066 follow-up smoke).
    if _opens_unknown(reply):
        # A padded refusal that OPENS unknown-shaped but ALSO carries an
        # unambiguous refusal REASON is a guard working as designed, not a gap.
        # Re-assert the REASON veto here length-INDEPENDENTLY so a >400-char
        # deflection can't slip past it now that the unknown length gate is gone
        # (adversarial review MED). A trailing channel POINTER is NOT re-checked
        # here: on an unknown opener it is house-style helpfulness, not a
        # refusal (2026-07-02 ROUND-2 addendum -- the 351-char plant-watering
        # miss). Safe: no REASON phrase legitimately BEGINS an unknown-shaped
        # genuine miss, so a real miss opener is never suppressed.
        if any(rx.search(reply) for rx in _DEFLECTION_REASON_RES):
            return False
        return True
    # (2) ANYWHERE-in-reply containment KEEPS the short-reply guard: in a long
    # reply the locked phrase may be quoted/embedded ("...she said 'I don't
    # have that right now...' but the PO confirms it"), which is NOT the reply's
    # own verdict, so a length cap prevents that false positive.
    if len(reply) <= _UNKNOWN_MAX_CHARS and locked in reply:
        return True
    return False


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
        state["recent"] = recent
        # Cap check BEFORE recording the dedup key (adversarial review MEDIUM:
        # recording it on a cap hit silently suppressed a capped question for
        # the whole 7-day window instead of letting it log tomorrow).
        if int(state.get("count") or 0) >= _daily_cap():
            state["overflow"] = int(state.get("overflow") or 0) + 1
            _save_state(state)
            log.info(
                "gap_detection: daily cap (%d) reached -- overflow=%d today",
                _daily_cap(), state["overflow"],
            )
            return False
        recent[key] = now.isoformat()
        state["recent"] = recent
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
    thread_context: bool = False,
) -> str | None:
    """Run the deterministic detectors; log at most one gap. Never raises.

    thread_context=True means prior thread messages were in the LLM context --
    kb_miss is skipped there (the answer source is the thread itself, invisible
    to kb_relevant_hits; adversarial review MEDIUM).

    Returns the detector name when a gap was logged, else None.
    """
    try:
        return _maybe_log_gap_inner(
            entity=entity, channel=channel, user=user, question=question,
            response_text=response_text, latency_ms=latency_ms,
            kb_meta=kb_meta or {}, gen_meta=gen_meta or {},
            is_dm=is_dm, thread_key=thread_key, thread_context=thread_context,
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
    thread_context: bool = False,
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
    # ALL THREE predicates, entity-agnostic (adversarial review HIGH:
    # is_phi_risk alone misses bare clinical terms -- autism/risperidone/ADHD
    # -- and named-person billing/authorization admin-PHI; that is exactly why
    # is_clinical_phi and is_lex_billing_status_phi exist, and this log flows
    # to Haiku + owner DMs + eval seeds. A missed legit gap is a far cheaper
    # error than PHI in an egress-bound file -- same posture as
    # apply_contributed_note's 3-predicate write gate).
    if (is_phi_risk(question) or is_clinical_phi(question)
            or is_lex_billing_status_phi(question)):
        log.debug("gap_detection: question flagged PHI -- skipped")
        return None

    # A deflection is a guard working as designed, not a gap -- and its
    # question is likely a blocked topic; drop entirely. EXCEPTION: when the
    # reply OPENS with a genuine-unknown shape, a bare trailing channel POINTER
    # ("I don't have that. ...or ask in #hjrg.") is Cora's house-style
    # helpfulness, NOT a refusal -- so for an unknown opener veto ONLY on an
    # unambiguous refusal REASON (length-independent, mirroring the re-veto
    # inside is_unknown_response). 2026-07-02 ROUND-2 addendum: the old
    # anywhere-in-reply is_deflection() gate ate the 351-char plant-watering
    # miss because it carried a trailing "ask in #hjrg" pointer.
    reply_norm = _normalize_reply(response_text)
    if _opens_unknown(reply_norm):
        if any(rx.search(reply_norm) for rx in _DEFLECTION_REASON_RES):
            return None
    elif is_deflection(response_text):
        return None

    # Per-query retrieval-decision telemetry (2026-07-09 kb_miss calibration
    # slice): record best_distance + the SHADOW kb_miss verdict for every
    # answerable query where a KB search ran. LOG-ONLY -- the shadow verdict
    # never gates, DMs, routes, or writes; it exists solely to build the
    # answerable distance distribution Harrison calibrates the real floor from
    # (~1 week out). Placed AFTER the deflection veto so refusals don't pollute
    # the distribution, and BEFORE the dedup/cap/thread gates so EVERY answerable
    # query is measured (not just the <=15/day that survive the gap cap).
    # Fail-soft: _log_retrieval_decision never raises.
    shadow_miss = _log_retrieval_decision(
        entity=ent, channel=channel, kb_meta=kb_meta, gen_meta=gen_meta,
        thread_context=thread_context,
    )
    if shadow_miss:
        log.info("gap_detection: SHADOW kb_miss (log-only, NOT a gap) entity=%s "
                 "channel=#%s best_distance=%s floor=%.2f", ent, channel,
                 kb_meta.get("kb_best_distance"), _shadow_kb_miss_floor())

    detector: str | None = None
    gap_desc = ""
    if is_unknown_response(response_text) and (
            not gen_meta.get("used_tools")
            or _normalize_reply(response_text).startswith(
                _normalize_reply(UNKNOWN_RESPONSE_TEXT))):
        # A tool-path reply counts as an unknown-gap ONLY when it is the LOCKED
        # finance UNKNOWN phrase (the finance connector's genuine data-gap
        # signal -- test_unknown_wins_even_with_tools pins this). A tool's own
        # "no meeting found" / "no signals for {person}" relay is a tool RESULT,
        # not a knowledge gap -- and a dossier relay carries a person name into
        # this egress-bound log. Mirrors kb_miss's used_tools exclusion below
        # (adversarial review MED: meeting_actions/person_dossier refusals).
        detector = "unknown_response"
        gap_desc = "Reply was an unknown/no-data response"
    elif (
        kb_meta.get("kb_search_ran")
        and int(kb_meta.get("kb_relevant_hits") or 0) == 0
        and not kb_meta.get("kb_notes_hit")
        and not kb_meta.get("cross_entity_fallback")
        and not gen_meta.get("used_tools")
        and not thread_context
    ):
        # Retrieval genuinely missed and no other answer source (tool, note,
        # fallback, prior thread messages) was in play.
        detector = "kb_miss"
        gap_desc = ("KB retrieval returned no relevant content "
                    "(0 chunks passed the distance gate)")
    if detector is None:
        return None

    if _thread_already_logged(thread_key):
        return None
    if not register_detection(ent, question):
        return None

    # kb_miss calibration data (D-066 follow-up): the closest returned chunk's
    # distance + raw count when a KB search ran (set by context_loader). Present
    # for kb_miss AND for unknown_response replies that also ran retrieval; None
    # when no search ran (tool-only path). Instrumentation only -- no gating.
    best_distance = kb_meta.get("kb_best_distance")
    chunks_returned = kb_meta.get("kb_chunks_returned")

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
        best_distance=best_distance,
        chunks_returned=chunks_returned,
    )
    _mark_thread_logged(thread_key)
    log.info("gap_detection: gap logged detector=%s entity=%s channel=#%s "
             "best_distance=%s chunks_returned=%s",
             detector, ent, channel, best_distance, chunks_returned)
    return detector
