"""Knowledge-gap autofill -- mine Slack conversations, escalate to teammates.

Cora logs a knowledge gap (logs/knowledge-gaps.jsonl) every time she answers
with a [CORA_KNOWLEDGE_GAP: ...] sentinel. Historically those gaps were only
resolved through the manual Drive digest flow (1 of 41 ever resolved). This
module closes the loop automatically, in two stages:

  Stage 1 -- MINE: for each open gap, semantic-search the KB restricted to
  swept Slack conversation chunks (source="slack", entity-scoped, PHI-guarded)
  and let Haiku draft a candidate answer with citations. Confident drafts are
  proposed through the existing knowledge-review flow -- Harrison gets the
  standard 7am DM and reacts with thumbs-up/down (D-011 preserved: nothing is
  written without his approval).

  Stage 2 -- ASK: gaps that stay unanswerable for ESCALATE_AFTER_HOURS are
  escalated once to the entity's domain owner (data/maps/gap-domain-owners.yaml)
  via a Slack DM asking the question. Their reply is captured by app.py's DM
  handler, routed back here, and proposed through the same Harrison gate.

On Harrison's approval, run_knowledge_review.py's executor appends the answer
to design/known-answers/{entity}.md (loaded into Cora's per-entity context)
and records the gap as resolved in design/known-answers/.resolved-gaps.jsonl
-- the same files the manual digest flow uses, so the two flows can't fight.

Guardrails:
  - PHI: gaps or evidence flagged by phi_guard are never mined or escalated.
  - LEX: escalation DMs are skipped for LEX* gaps unless the
    CORA_GAP_ESCALATION_LEX lane is on (default off; Harrison 2026-08-06,
    superseding the 1uuu Fork-2 "LEX escalation stays OFF" lock). Even then the
    ONLY recipients are roster entries flagged `gap_escalation: true` for LEX
    (leadership), the PHI union is re-screened at the DM render site, and LEX
    stays excluded from MINING at the SQL layer -- only the ASK lane opens.
  - Visibility CPA: never an escalation target (IDs map is internal-only).
  - Throttle: one escalation DM per gap, ever. Max MAX_ASKS_PER_RUN per run.
  - Fail-closed drafting: an API/parse failure proposes nothing.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from .phi_guard import is_any_phi, is_phi_risk, is_lex_billing_status_phi, is_clinical_phi

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]

UPDATE_TYPE_KNOWN_ANSWER = "known_answer"

# Stage-1 tuning
MAX_DISTANCE = 1.30          # KB cosine-distance ceiling (Phase 3 tuned value)
MIN_EVIDENCE_CHUNKS = 2      # require at least this many usable chunks to draft
SEARCH_K = 24                # over-fetch before source filtering
EVIDENCE_K = 8               # max chunks passed to Haiku
_HAIKU_MODEL = "claude-haiku-4-5"

# Stage-2 tuning
ESCALATE_AFTER_HOURS = 72    # gap must be at least this old before a DM ask
ASK_TTL_HOURS = 96           # pending ask expires after this (no re-ask)
MAX_ASKS_PER_RUN = 3

# WS-1: gaps open longer than this with no resolution auto-close as expired,
# so the 6am run's Haiku spend stays pointed at live gaps instead of re-mining
# a stale set forever.
GAP_TTL_DAYS = 30

# DM keywords that belong to the OSN shift scheduler -- a top-level DM reply
# matching these is never treated as a gap answer (threaded replies always win).
_SHIFT_KEYWORDS = (
    "my schedule", "my shifts", "when do i work",
    "help", "what can you do", "commands", "cancel", "quit", "stop",
)


def _allowed_sources() -> frozenset[str]:
    raw = os.environ.get("GAP_AUTOFILL_SOURCES", "slack")
    return frozenset(s.strip().lower() for s in raw.split(",") if s.strip())


# ---------------------------------------------------------------------------
# Paths (env-overridable for tests)
# ---------------------------------------------------------------------------

def _gaps_log_path() -> Path:
    return Path(os.environ.get("KNOWLEDGE_GAPS_LOG_PATH")
                or _REPO_ROOT / "logs" / "knowledge-gaps.jsonl")


def _resolved_path() -> Path:
    return Path(os.environ.get("RESOLVED_GAPS_PATH")
                or _REPO_ROOT / "design" / "known-answers" / ".resolved-gaps.jsonl")


def _state_path() -> Path:
    return Path(os.environ.get("GAP_AUTOFILL_STATE_PATH")
                or _REPO_ROOT / "data" / "state" / "gap_autofill_state.json")


def _pending_asks_path() -> Path:
    return Path(os.environ.get("GAP_ASK_PENDING_PATH")
                or _REPO_ROOT / "data" / "state" / "gap_ask_pending.json")


def _owners_map_path() -> Path:
    return Path(os.environ.get("GAP_DOMAIN_OWNERS_PATH")
                or _REPO_ROOT / "data" / "maps" / "gap-domain-owners.yaml")


def _known_answers_dir() -> Path:
    return Path(os.environ.get("KNOWN_ANSWERS_DIR")
                or _REPO_ROOT / "design" / "known-answers")


_STATE_LOCK = Lock()
_ASKS_LOCK = Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _atomic_write_text(path: Path, text: str) -> None:
    """Crash-safe text write (temp-file + os.replace), same pattern as _write_json.

    Drive-materialization (2026-06-29): with KNOWN_ANSWERS_DIR pointed at the Drive
    _brain/known-answers/ store that Tag reads live, a partial/interrupted write would
    leave Tag (and Cora) reading a half-written known-answers file. temp+rename makes
    the swap atomic on the same filesystem (the .tmp sits in the same dir as the target).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def load_state() -> dict[str, Any]:
    """Per-gap autofill state, keyed by gap ts. States: proposed | asked | exhausted."""
    return _read_json(_state_path(), {})


def save_state(state: dict[str, Any]) -> None:
    with _STATE_LOCK:
        _write_json(_state_path(), state)


def load_pending_asks() -> dict[str, Any]:
    """Pending teammate asks, keyed by ask_id."""
    return _read_json(_pending_asks_path(), {})


def save_pending_asks(asks: dict[str, Any]) -> None:
    with _ASKS_LOCK:
        _write_json(_pending_asks_path(), asks)


# ---------------------------------------------------------------------------
# Gap loading
# ---------------------------------------------------------------------------

def _load_resolved_ids() -> set[str]:
    ids: set[str] = set()
    path = _resolved_path()
    if not path.exists():
        return ids
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            gap_id = rec.get("id")
            if gap_id:
                ids.add(gap_id)
    except Exception as exc:
        log.warning("gap_autofill: could not read resolved gaps: %s", exc)
    return ids


def load_open_gaps() -> list[dict[str, Any]]:
    """All logged gaps that are neither resolved nor already handled by autofill.

    A gap stays "open" while its autofill state is absent. States 'proposed',
    'asked', and 'exhausted' all remove it from this list -- re-proposing the
    same gap would spam Harrison's review queue.
    """
    path = _gaps_log_path()
    if not path.exists():
        return []
    resolved = _load_resolved_ids()
    state = load_state()
    gaps: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = rec.get("ts", "")
        if not ts or ts in resolved or ts in state:
            continue
        if not rec.get("gap") or not rec.get("question"):
            continue
        gaps.append(rec)
    return gaps


def gap_age_hours(gap: dict[str, Any]) -> float:
    try:
        dt = datetime.fromisoformat(str(gap.get("ts", "")).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except Exception:
        return 0.0
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0


def expire_stale_gaps(dry_run: bool = False, ttl_days: int = GAP_TTL_DAYS) -> int:
    """Close open gaps older than ttl_days as 'expired' (WS-1 gap TTL).

    Writes an 'expired' record to the shared resolved ledger -- the same file
    the digest flow and apply_known_answer use -- so an expired gap leaves
    load_open_gaps() for every consumer at once. Idempotent (an already-
    resolved gap is not open). Returns the number of gaps expired.
    """
    stale = [g for g in load_open_gaps()
             if gap_age_hours(g) > ttl_days * 24]
    if not stale or dry_run:
        return len(stale)
    resolved_path = _resolved_path()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    with resolved_path.open("a", encoding="utf-8") as fh:
        for gap in stale:
            fh.write(json.dumps({
                "id": gap.get("ts", ""),
                "action": "expired",
                "timestamp": _now_iso(),
                "target_entity": gap.get("entity", "FNDR"),
                "captured_entity": gap.get("entity", "FNDR"),
                "source": "gap_ttl",
            }, ensure_ascii=False) + "\n")
    log.info("gap_autofill: expired %d gap(s) older than %dd", len(stale), ttl_days)
    return len(stale)


# ---------------------------------------------------------------------------
# Stage 1 -- mine swept Slack conversations
# ---------------------------------------------------------------------------

def _entity_scope(entity: str) -> tuple[str, str | None]:
    """Map a gap's logged entity to (kb_entity, sub_entity)."""
    entity = (entity or "FNDR").strip().upper()
    if entity.startswith("LEX-"):
        return "LEX", entity
    return entity, None


def search_slack_evidence(kb: Any, gap: dict[str, Any]) -> list[Any]:
    """Entity-scoped KB search filtered to Slack-conversation chunks.

    Returns up to EVIDENCE_K SearchResult objects with distance <= MAX_DISTANCE,
    source in the allowed set, and no PHI-flagged content.
    """
    query = f"{gap.get('question', '')}\n{gap.get('gap', '')}".strip()
    kb_entity, sub_entity = _entity_scope(gap.get("entity", "FNDR"))
    try:
        results = kb.search(query=query, entity=kb_entity, k=SEARCH_K,
                            sub_entity=sub_entity)
    except Exception as exc:
        log.warning("gap_autofill: KB search failed for gap %s: %s",
                    gap.get("ts", "?"), exc)
        return []
    allowed = _allowed_sources()
    out = []
    for r in results:
        if getattr(r, "source", "") not in allowed:
            continue
        if getattr(r, "distance", 99.0) > MAX_DISTANCE:
            continue
        # cq-8d16969e85fb: pure bot/automation chunks (incl. Cora's own swept
        # replies) are never evidence — the self-poisoning class. Mixed chunks
        # (human ask + Cora reply) stay: the human question is real evidence.
        meta = getattr(r, "metadata", None)
        if isinstance(meta, dict) and meta.get("bot_authored"):
            continue
        content = getattr(r, "content", "") or ""
        # PHI PARITY-RAISE (D-051 lens-2 HIGH, 2026-08-06): this filter was
        # single-predicate `is_phi_risk`, which provably misses clinical and named
        # admin-PHI text that the 3-predicate union catches ("He is nonverbal and on
        # risperidone", "Bob Smith's billing authorization is pending and his service
        # units ran out"). That evidence goes into a Haiku prompt, so the union is the
        # right bar here -- the same raise code_queue took on 2026-07-30. Fail-closed:
        # a screen error drops the chunk rather than passing it.
        try:
            if is_any_phi(content):
                continue
        except Exception:  # noqa: BLE001 -- fail closed, never widen the surface
            log.warning("gap_autofill: evidence PHI screen errored -- dropping chunk",
                        exc_info=True)
            continue
        out.append(r)
        if len(out) >= EVIDENCE_K:
            break
    return out


_DRAFT_PROMPT = """\
You are filling a knowledge gap for Cora, an internal company assistant.

A user asked a question Cora could not answer. Below are excerpts from real
Slack conversations between team members that may contain the answer.

QUESTION ASKED:
{question}

GAP CORA FLAGGED:
{gap}

SLACK CONVERSATION EXCERPTS:
{evidence}

Decide whether the excerpts contain enough information to answer the gap
factually. Do NOT guess or extrapolate beyond what the excerpts state.

Respond with ONLY a JSON object (no markdown fences, no prose):
{{"answerable": true/false,
  "answer": "1-3 sentence factual answer, empty string if not answerable",
  "confidence": "HIGH"/"MED"/"LOW",
  "citation": "which excerpt(s) support the answer, e.g. 'excerpt 2 (#osn-leadership, 2026-06-01)'"}}

Rules:
- answerable=true only if the answer is directly supported by the excerpts.
- HIGH = stated explicitly; MED = strongly implied; LOW = weakly implied.
- Never include client names, diagnoses, or other PHI in the answer.
"""


def _format_evidence(chunks: list[Any]) -> str:
    parts = []
    for i, c in enumerate(chunks, 1):
        title = getattr(c, "title", "") or "(untitled)"
        ts = getattr(c, "date_modified", None)
        date_str = ""
        if ts:
            try:
                date_str = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
            except Exception:
                date_str = ""
        content = (getattr(c, "content", "") or "")[:1200]
        parts.append(f"--- excerpt {i} [{title} {date_str}] ---\n{content}")
    return "\n\n".join(parts)


# ── MINE answer-quality / durability gate (GL-11/12, 2026-07-09) ─────────────
# The lifetime auto-approved MINE writes were low-value and are why the
# approve-rate is 0: vague deflections ("F3 ad-spend ROI lives in Polar, ping
# Larry"; "find the PDF Harrison presented"), in-progress statuses ("Harrison is
# working with freelancers ... by end of week"), and point-in-time snapshots
# ("cash crunched this week"; "what's on my plate") frozen as durable canon.
# Bias to PRECISION: a low-quality proposal that Harrison must -1 (or, worse,
# approves into a misleading canonical fact) is worse than proposing nothing, so
# reject these BEFORE a MINE proposal is queued. An occasional false reject is
# recoverable (digest flow / re-ask); a bad durable write is not.

# Answer punts to a person/doc/tool instead of stating the fact.
# The (?-i:...) scopes the capital-initial classes to CASE-SENSITIVE even though
# the pattern is IGNORECASE overall (D-051: a plain [A-Z@#] under re.IGNORECASE
# matches lowercase too, so "the contact is Jane", "the email is help@x",
# "message retention is 90d" were all wrongly rejected). The verb words stay
# case-insensitive (match "Contact"/"Email" at a sentence start), but the token
# AFTER them must be a genuinely capitalized proper name/handle -- that is what
# distinguishes a deflection ("contact Larry", "email @x") from the same word
# used as a NOUN in a real fact ("the contact is ...", "email marketing runs...").
_VAGUE_DEFLECTION_RE = re.compile(
    r"\b(?:ping|reach out to|reach|contact|check with|talk to|follow up with|"
    r"loop in|email|dm|message)\s+(?-i:[A-Z@#])"     # "ping Larry", "email @x", "check with #y"
    r"|\bask\s+(?:(?-i:[A-Z])|@|#|the\s+\w+\s+(?:team|owner|lead))"  # "ask Larry" / "ask the finance team"
    r"|\b(?:find|see|refer to|look\s+(?:at|in|for))\s+(?:the|our|that|this|it\s+in)\b"
    r"|\b(?:lives|is|are|can be found|located|sits)\s+in\s+(?:polar|hubspot|asana|"
    r"drive|quickbooks|qbo|slack|notion)\b"
    r"|\bin\s+the\s+(?:\w+\s+)?(?:sheet|deck|doc|document|drive|pdf|file|spreadsheet|"
    r"folder|tracker)\b"                              # "in the sheet", "in the shared spreadsheet"
    r"|\bthe\s+(?:pdf|deck|doc|document|file|spreadsheet|sheet)\s+(?:harrison|he|she|"
    r"they|we|larry|justin|hannah|tommy|shaun|matt)\b",
    re.IGNORECASE,
)

# Answer describes work-in-progress rather than a settled fact.
_IN_PROGRESS_RE = re.compile(
    r"\b(?:working on|in progress|being\s+(?:worked|built|finalized|decided|determined)|"
    r"by\s+(?:the\s+)?end of|by eod|by\s+(?:mon|tues|wednes|thurs|fri|satur|sun)day|"
    r"coming soon|not yet\s+(?:decided|determined|confirmed|finalized|set|available)|"
    r"still\s+(?:being|working|figuring)|to be\s+(?:determined|decided|confirmed)|"
    r"\btbd\b|\btbc\b)",
    re.IGNORECASE,
)

# Answer is time-relative -> rots as canon (a durable fact is not "this week").
_SNAPSHOT_RE = re.compile(
    r"\b(?:this\s+(?:week|month|quarter)|last\s+(?:week|month|quarter)|today|yesterday|"
    r"as of\s+(?:now|today|this|yesterday)|currently|right now|at the moment|"
    r"so far this|this\s+(?:morning|afternoon)|(?:this|current)\s+pay\s?period)\b",
    re.IGNORECASE,
)

_MIN_DURABLE_ANSWER_CHARS = 12


def answer_quality_ok(answer: str) -> tuple[bool, str]:
    """Reject vague-deflection / in-progress / point-in-time-snapshot drafts
    before a MINE proposal is queued (GL-11/12). Returns (ok, reason).

    Applied ONLY on the MINE (mined-draft) path -- NOT at the Harrison-approved
    write, so a click-to-approve always results in a write (no confusing
    "approved but nothing saved"). Bias to precision.
    """
    text = (answer or "").strip()
    if len(text) < _MIN_DURABLE_ANSWER_CHARS:
        return False, "answer too short to be a durable fact"
    if _VAGUE_DEFLECTION_RE.search(text):
        return False, "answer punts to a person/doc/tool instead of stating the fact"
    if _IN_PROGRESS_RE.search(text):
        return False, "answer describes in-progress work, not a settled fact"
    if _SNAPSHOT_RE.search(text):
        return False, "answer is a point-in-time snapshot, not durable canon"
    return True, ""


# ── Fork 1 (Wave-1 flywheel-conversion calibration, 2026-07-30): rejection-log
# aggregation ──────────────────────────────────────────────────────────────────
# Decision: keep answer_quality_ok STRICT (it rejected exactly 1 gap -- it is not
# the bottleneck; the binding constraint is intake, not conversion). The per-
# rejection signal already exists (draft_answer logs "gap_autofill: draft
# rejected (quality) for gap <id>: <reason>" at INFO on every rejection); the
# missing piece is AGGREGATION so the 2-week review can read it as a summary
# instead of grepping logs by hand.
#
# PHI-safe by construction: the <reason> logged is always one of
# answer_quality_ok's four fixed, canned strings (never raw mined-answer or raw
# gap text), and <id> is a bare gap ts (no content). This aggregator re-parses
# those log lines from gap-autofill-{date}.log (this module's own dated log
# family, written by scripts/run_gap_autofill.py's logging setup) and tallies by
# a short REASON CODE -- an unrecognized/garbled reason (a future edit to the
# canned strings, or log corruption) buckets to "other" rather than surfacing
# the raw matched text, so this can never become a PHI leak path even if the
# reason strings ever change.
_REJECTION_LOG_RE = re.compile(
    r"gap_autofill: draft rejected \(quality\) for gap (\S+): (.+)$"
)

# reason string (from answer_quality_ok) -> (reason_code, time_decaying).
# time_decaying=True marks the "dated-snapshot-with-expiry" class (in-progress /
# point-in-time-snapshot rejections) the 2-week review may want to reopen;
# time_decaying=False marks structural rejections (too-short / vague-deflection)
# unrelated to that question.
_REASON_CODES: dict[str, tuple[str, bool]] = {
    "answer too short to be a durable fact": ("too_short", False),
    "answer punts to a person/doc/tool instead of stating the fact": ("vague_deflection", False),
    "answer describes in-progress work, not a settled fact": ("in_progress", True),
    "answer is a point-in-time snapshot, not durable canon": ("snapshot", True),
    # Slice 3 (2026-08-05): the mine-eligibility reasons share this log line + code
    # table so a REJECTED conversion is aggregated and visible in the review digest
    # rather than bucketing to "other". A rejection that vanishes is the failure mode
    # routing-completeness already suffers from. Registered here, not just emitted --
    # forgetting this table is how a new reason becomes invisible.
    # (Values are the MINE_INELIGIBLE_* constants; kept literal because this table is
    # read by a log-line parser, so it must match the strings on disk historically.)
    "gap is a capability ask, not a missing fact": ("capability_ask", False),
    "gap was already routed to the code-session queue": ("already_routed", False),
    "gap references a retired process or connector": ("retired_process", False),
    "gap is connector/QA scaffolding, not organizational knowledge": ("qa_scaffolding", False),
    "gap asks for point-in-time state, not durable canon": ("ephemeral_question", True),
    "exchange is unresolved/disputed -- decision material, not fact material":
        ("disputed_d128", True),
    "eligibility screen errored (fail-closed)": ("screen_error", False),
}


def _rejection_log_files(days: int, repo_root: Path | None = None) -> list[Path]:
    """D-051 adversarial review LOW: scripts/run_gap_autofill.py's logging setup
    names its dated log file from LOCAL `datetime.now()`, not UTC -- using
    `datetime.now(timezone.utc).date()` here silently shifted the window by a
    day in the evening AZ hours (UTC date is already "tomorrow"), dropping the
    oldest day's file from the scan. Match the writer's local-date basis."""
    root = Path(repo_root) if repo_root else _REPO_ROOT
    log_dir = root / "logs"
    today = datetime.now().date()
    out = []
    for i in range(days):
        d = today - timedelta(days=i)
        p = log_dir / f"gap-autofill-{d.isoformat()}.log"
        if p.exists():
            out.append(p)
    return out


def aggregate_quality_rejections(days: int = 14, repo_root: Path | None = None) -> dict[str, Any]:
    """PHI-safe review-time aggregation of the existing draft-rejection log lines.

    Returns a dict: window_days, total_rejections, unique_gaps, by_reason (dict
    of reason_code -> count), time_decaying / non_time_decaying counts. Never
    raises -- a missing/unreadable log file just contributes nothing (fail-soft,
    matching every other flywheel-metrics gauge)."""
    by_reason: dict[str, int] = {}
    time_decaying = 0
    non_time_decaying = 0
    total = 0
    gap_ids: set[str] = set()
    for path in _rejection_log_files(days, repo_root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            match = _REJECTION_LOG_RE.search(line)
            if not match:
                continue
            gap_id, reason = match.group(1), match.group(2).strip()
            code, decaying = _REASON_CODES.get(reason, ("other", False))
            by_reason[code] = by_reason.get(code, 0) + 1
            if decaying:
                time_decaying += 1
            else:
                non_time_decaying += 1
            total += 1
            gap_ids.add(gap_id)
    return {
        "window_days": days,
        "total_rejections": total,
        "unique_gaps": len(gap_ids),
        "by_reason": by_reason,
        "time_decaying": time_decaying,
        "non_time_decaying": non_time_decaying,
    }


# ── MINE eligibility gate (Slice 3: cq-5c6ff15610bd + D-128, 2026-08-05) ──────
# answer_quality_ok above screens the DRAFTED ANSWER for durability. This gate is
# upstream of it and screens the EXCHANGE ITSELF -- whether this gap is the kind of
# thing a durable "known fact" can ever be, regardless of how well Haiku words it.
# It runs BEFORE the Haiku call, so an ineligible exchange never reaches an LLM at
# all (cheaper, and one less egress surface for mined Slack text).
#
# The four classes cq-5c6ff15610bd names, all deterministic (D-034 pattern):
#   capability ask      -- "can you access X" belongs in the code-session queue as a
#                          missing TOOL, not in known-answers as a missing FACT.
#   already routed      -- a detector=="code_queue_route" gap was ALREADY dispositioned
#                          into the code queue by the classifier; mining it is double
#                          handling, and it is how the internal classifier string
#                          "capability/knowledge ask routed from code-queue classifier"
#                          became a durable F3E "fact" on 2026-08-05.
#   retired process     -- canon that has been explicitly retired cannot become fresh
#                          canon (Clover D-027, role-briefing-config, the WS17-C silent
#                          auto-approve, Make 4768887).
#   connector/QA        -- a [QA]-prefixed smoke test (D-104) or a Cowork-connector
#                          footer is test scaffolding, not organizational knowledge.
#   ephemeral snapshot  -- "what's on my plate" / "cash this week" is a QUESTION about
#                          point-in-time state; freezing any answer to it is wrong.
#
# Plus the D-128 hard rule (cascaded 2026-08-05): an exchange Cora HERSELF flagged as
# unresolved/uncertain, or that encodes a live disagreement between two systems of
# record, is DECISION material, not FACT material. It gets routed to the decisions
# lane instead -- provenance-stamped -- and never staged as a known_answer.

MINE_INELIGIBLE_CAPABILITY_ASK = "gap is a capability ask, not a missing fact"
MINE_INELIGIBLE_ALREADY_ROUTED = "gap was already routed to the code-session queue"
MINE_INELIGIBLE_RETIRED = "gap references a retired process or connector"
MINE_INELIGIBLE_QA = "gap is connector/QA scaffolding, not organizational knowledge"
MINE_INELIGIBLE_EPHEMERAL = "gap asks for point-in-time state, not durable canon"
MINE_INELIGIBLE_DISPUTED = (
    "exchange is unresolved/disputed -- decision material, not fact material")
MINE_INELIGIBLE_SCREEN_ERROR = "eligibility screen errored (fail-closed)"

# A gap whose detector already carries its own disposition elsewhere.
_ALREADY_ROUTED_DETECTORS = frozenset({"code_queue_route"})

# Which ineligibility reasons justify a PERMANENT disposition in
# gap_autofill_state.json. This matters because there is NO reset path: load_open_gaps
# excludes any gap ts present in state, forever (D-051 lens-5 HIGH/LOW).
#
# So only STRUCTURAL reasons retire a gap -- already_routed (the classifier filed it
# in the code queue, so it genuinely has a disposition) and disputed (only when a
# decision card was actually created). The HEURISTIC classes (retired/QA/ephemeral/
# capability) skip mining and escalation for the run but stay OPEN, so a regex false
# positive costs one skipped night instead of permanently burying an answerable gap,
# and expire_stale_gaps still gives them the normal audited 30-day close.
PERMANENT_INELIGIBLE_REASONS = frozenset({
    MINE_INELIGIBLE_ALREADY_ROUTED,
})

_RETIRED_PROCESS_RE = re.compile(
    r"\bclover\b"                                   # D-027, retired from OSN permanently
    r"|\brole-?briefing-?config\b"                  # retired at Phase-2 d2
    r"|\bfighter_?compliance\b"                     # backend tracker retired 2026-08-03
    r"|\bmake\.?com scenario 4768887\b|\bscenario 4768887\b"   # D-045a, deactivated
    r"|\bsilent auto-?approve\b",                   # D-060, retired
    re.IGNORECASE,
)

# NOTE: the literal [QA] leg lives in qa_scaffolding.contains_qa_marker, NOT here
# (R8, 2026-08-06 -- the one-definition promise). This regex carries only the OTHER
# scaffolding signals and the caller ORs the two. A second private copy of the
# marker pattern is exactly how the D-104 convention came to be honoured on one
# surface and silently absent from the rest.
_QA_SCAFFOLDING_RE = re.compile(
    # The Cowork connector footer, which is literally "*Sent using* <@U...>". Bare
    # "sent using" over-fired on ordinary prose (D-051 lens-5 LOW: "Which invoices
    # were sent using the new template?" is a real durable question); require the
    # mention token, mirroring tool_dispatch._CONFIRM_SENT_USING_RE.
    r"sent using\s*\*?\s*<@"
    r"|\bsmoke ?test\b|\btest message\b|\bignore this\b|\bthis is a test\b"
    r"|\btest locker code\b",
    re.IGNORECASE,
)


def _is_qa_scaffolding(text: str) -> bool:
    """The [QA] marker (shared definition) OR the other scaffolding signals."""
    from . import qa_scaffolding
    return bool(qa_scaffolding.contains_qa_marker(text)
                or _QA_SCAFFOLDING_RE.search(text or ""))

# Point-in-time QUESTIONS (the mirror of _SNAPSHOT_RE, which screens ANSWERS).
# A TIME-RELATIVE marker is required. D-051 lens-5 LOW: the first cut also matched
# "(how much|what's our) ... (cash|balance|runway)" with no time qualifier at all,
# so "What's our cash allocation policy?" -- a durable fact -- read as ephemeral.
_EPHEMERAL_QUESTION_RE = re.compile(
    r"\bwhat'?s on (?:my|his|her|their) plate\b"
    r"|\b(?:this|last) (?:week|month|quarter)'?s? (?:numbers|revenue|sales|total|cash)\b"
    r"|\bright now\b|\bat the moment\b|\bas of (?:today|now)\b|\bso far (?:today|this)\b"
    r"|\b(?:how much|what'?s our|what is our)\b[^?\n]{0,40}"
    r"\b(?:cash|balance|runway)\b[^?\n]{0,30}\b(?:today|now|this week|currently)\b"
    r"|\bwhat'?s (?:my|our) (?:uptime|status)\b",
    re.IGNORECASE,
)

# D-128 half 1 -- Cora's OWN first-person uncertainty inside the mined exchange.
_CORA_UNCERTAINTY_RE = re.compile(
    r"\bi can'?t tell\b|\bi cannot tell\b|\bi can'?t verify\b|\bi cannot verify\b"
    r"|\bi (?:shouldn'?t|should not)\s+(?:keep\s+)?(?:assert|claim|say|state|present)"
    r"|\bi'?m not (?:sure|certain|confident)\b|\bi am not (?:sure|certain|confident)\b"
    r"|\bworth harrison'?s attention\b|\bflag(?:ging|ged)?\s+(?:this\s+)?for harrison\b"
    r"|\bneeds? (?:harrison'?s?|your)\s+(?:call|decision|confirmation|review)\b"
    r"|\blacks? (?:direct )?(?:live )?(?:connector )?access to verify\b"
    r"|\bcan'?t reconcile\b|\bcannot reconcile\b|\bi don'?t know which\b",
    re.IGNORECASE,
)

# D-128 half 2 -- a live disagreement between two systems of record.
_SOURCE_DISAGREEMENT_RE = re.compile(
    r"\bdiscrepanc(?:y|ies)\b|\bconflict(?:s|ing)?\s+with\b|\bcontradict"
    r"|\bdoes ?n'?t match\b|\bdo ?n'?t match\b|\bout of sync\b|\bsync issue\b"
    r"|\bthat is false\b|\bthat'?s false\b|\bthat'?s (?:not|in)correct\b"
    r"|\btwo different (?:sources?|numbers?|answers?|sheets?|systems?)\b"
    r"|\bwhich (?:one )?is (?:correct|right|authoritative|the source of record)\b"
    r"|\bsource of record\b[^.\n]{0,60}\b(?:but|while|whereas|however)\b"
    r"|\bunresolved\b|\bstill disputed\b",
    re.IGNORECASE,
)


def log_mine_rejection(gap_ts: Any, reason: str) -> None:
    """Emit the CANONICAL rejection line that aggregate_quality_rejections parses.

    D-051 lens-2 MEDIUM (2026-08-06): registering the new reasons in _REASON_CODES was
    not enough to make them visible. _REJECTION_LOG_RE matches only the
    "draft rejected (quality)" line, which is emitted inside draft_answer -- and the
    driver `continue`s on an ineligible gap BEFORE draft_answer is ever called, so in
    PRODUCTION all seven new codes would have aggregated to zero forever. Both callers
    now go through this one function, so the log shape and the parser cannot drift.
    """
    log.info("gap_autofill: draft rejected (quality) for gap %s: %s",
             gap_ts if gap_ts not in (None, "") else "?", reason)


def _evidence_text(evidence: list[Any] | None) -> str:
    """Concatenated evidence content, for the D-128 screens. Bounded so a large
    evidence set can't turn a regex sweep into a hot loop."""
    parts = []
    for c in (evidence or [])[:EVIDENCE_K]:
        parts.append(str(getattr(c, "content", "") or "")[:1200])
    return "\n".join(parts)


def _cora_attributed_text(evidence: list[Any] | None) -> str:
    """Evidence text from chunks that actually CONTAIN a Cora reply.

    D-051 lens-5 HIGH (2026-08-06): the first cut screened the ENTIRE evidence set,
    which is a KB VECTOR SEARCH result -- topically similar chunks, not "the
    exchange" -- with regexes that carry no speaker attribution. Measured: 9 of 9
    innocuous single human lines tripped it ("[Justin] I'm not sure who owns that
    account yet", "[Tommy] Needs your review before I send it", "[Matt] The Shopify
    count is out of sync"). Stock operational Slack. The double wrong outcome was an
    ANSWERABLE gap declared disputed -- skipped for mining AND proposed as a
    never-expiring decision card.

    The ingest already tags the attribution this needs: `has_cora_reply` per chunk
    (cq-8d16969e85fb). Untagged chunks (pre-tagging rows) are NOT screened -- the
    safe direction here is toward not-disputed, because a missed dispute still lands
    in front of Harrison as a gated known_answer proposal, while a false dispute
    both suppresses a real answer and adds noise to a lane that is already backlogged.
    """
    parts = []
    for c in (evidence or [])[:EVIDENCE_K]:
        meta = getattr(c, "metadata", None)
        if not (isinstance(meta, dict) and meta.get("has_cora_reply")):
            continue
        parts.append(str(getattr(c, "content", "") or "")[:1200])
    return "\n".join(parts)


def is_disputed_exchange(gap: dict[str, Any], evidence: list[Any] | None = None) -> bool:
    """D-128: True when Cora herself flagged this exchange uncertain, or it encodes
    a live disagreement between two systems of record.

    Screens the gap's own question/gap text (that IS the exchange -- the user's
    message plus Cora's own sentinel output) and ONLY the Cora-ATTRIBUTED evidence
    chunks. See _cora_attributed_text for why unattributed evidence is excluded.
    """
    own = f"{gap.get('question', '')}\n{gap.get('gap', '')}"
    text = f"{own}\n{_cora_attributed_text(evidence)}"
    return bool(_CORA_UNCERTAINTY_RE.search(text)
                or _SOURCE_DISAGREEMENT_RE.search(text))


def mine_eligibility(gap: dict[str, Any],
                     evidence: list[Any] | None = None) -> tuple[bool, str]:
    """(eligible, reason) for staging a known_answer from this gap.

    FAIL-CLOSED: any error in screening returns ineligible. The reason string is one
    of the fixed MINE_INELIGIBLE_* constants -- never raw gap text -- so it is safe to
    log and aggregate (the same PHI-safety property answer_quality_ok's reasons have).
    """
    try:
        question = str(gap.get("question", "") or "")
        gap_text = str(gap.get("gap", "") or "")
        detector = str(gap.get("detector", "") or "").strip().lower()
        combined = f"{question}\n{gap_text}"

        # ORDER MATTERS (D-051 lens-2 MEDIUM, 2026-08-06). The first cut evaluated
        # D-128 LAST, so a disputed exchange that also matched a cheaper class was
        # silently DROPPED instead of routed -- losing exactly D-128's motivating
        # class, a disagreement about a NUMBER ("what's our cash balance right now?"
        # + "[Cora] I can't reconcile the two sheets" matched _EPHEMERAL_QUESTION_RE
        # first). D-128 is a HARD rule, so it now outranks every class EXCEPT
        # already_routed -- that one has a disposition BY CONSTRUCTION (the classifier
        # already filed it in the code queue), so routing it again would double-handle.
        if detector in _ALREADY_ROUTED_DETECTORS:
            return False, MINE_INELIGIBLE_ALREADY_ROUTED
        if is_disputed_exchange(gap, evidence):
            return False, MINE_INELIGIBLE_DISPUTED
        try:
            from .knowledge_gaps import is_capability_ask
            if is_capability_ask(question):
                return False, MINE_INELIGIBLE_CAPABILITY_ASK
        except Exception:  # noqa: BLE001 -- fail closed on a screen import/None error
            # Log with the traceback (D-051 lens-5 HIGH): the first cut returned the
            # code silently, so a systemic screen failure left only a generic reason
            # and no way to diagnose it.
            log.warning("gap_autofill: capability-ask screen errored -- fail-closed",
                        exc_info=True)
            return False, MINE_INELIGIBLE_SCREEN_ERROR
        if _is_qa_scaffolding(combined):
            return False, MINE_INELIGIBLE_QA
        if _RETIRED_PROCESS_RE.search(combined):
            return False, MINE_INELIGIBLE_RETIRED
        if _EPHEMERAL_QUESTION_RE.search(question):
            return False, MINE_INELIGIBLE_EPHEMERAL
        return True, ""
    except Exception:  # noqa: BLE001 -- a bad draft is worse than no draft
        log.warning("gap_autofill: mine eligibility screen errored -- fail-closed",
                    exc_info=True)
        return False, MINE_INELIGIBLE_SCREEN_ERROR


# Cap the decisions lane so a corpus-wide sweep can never flood Harrison's
# never-expiring decision cards.
#
# 1, not 3 (D-051 lens-5 MEDIUM, measured 2026-08-06). The decision pool is ALREADY
# 65 PENDING with 55 never DM'd and 46 HIGH-and-never-DM'd, oldest 2026-07-16, while
# the drain sends at most _MAX_DECISION_DMS_PER_RUN=5 Mon-Fri. This task runs DAILY,
# so a cap of 3 could add 21 never-expiring cards a week against a net drain of ~4 --
# and because these route at confidence MED they sort BEHIND every HIGH item, making
# D-128 a write-only sink: durable (the letter of the rule) but not reaching Harrison,
# while the gap is marked handled. One per run keeps the lane honest until that
# backlog is triaged (scripts/triage_decision_backlog.py exists for exactly this).
MAX_DECISION_ROUTES_PER_RUN = 1


def decision_route_block_reason(gap: dict[str, Any]) -> str:
    """"" when this gap may be routed to the decisions lane, else a short reason.

    Split out from route_disputed_to_decision_lane so the DRY-RUN report runs the
    SAME screens the real path does. The first live dry-run reported "would route to
    the decisions lane" for a LEX gap the real path refuses outright -- a dry run
    that over-promises is worse than no dry run, since it IS the rollout gate.

    Screens mirror code_queue._route_to_flywheel: LEX entities skipped outright, any
    other entity's text through the 3-predicate PHI union, fail-closed on error.
    decision_inbox.screen_decision re-screens at the drain and again at the durable
    write, so these are the first of three belts, not the only one.
    """
    gap_ts = str(gap.get("ts", "") or "")
    if not gap_ts:
        return "no gap ts"
    entity = str(gap.get("entity", "FNDR") or "FNDR").strip().upper()
    if entity.startswith("LEX"):
        return "LEX entity (fail-closed)"
    text = f"{gap.get('question', '')} {gap.get('gap', '')}"
    try:
        if is_any_phi(text):
            return "PHI-flagged"
    except Exception:  # noqa: BLE001 -- fail closed
        log.warning("gap_autofill: D-128 PHI screen errored for gap %s", gap_ts,
                    exc_info=True)
        return "PHI screen errored (fail-closed)"
    return ""


def route_disputed_to_decision_lane(gap: dict[str, Any],
                                    evidence: list[Any] | None = None) -> str | None:
    """D-128: propose the disputed exchange as a decision_capture candidate.

    Returns the update_id on success, None when skipped. Provenance-stamped so the
    card says WHY it is here rather than reading as a mined fact.

    A skip is always LOGGED. A rejected conversion that simply vanished is the exact
    failure mode routing-completeness already suffers from.

    `evidence` is accepted but deliberately NOT persisted (see source_evidence below);
    it is kept in the signature because the caller has it and a future belt may want
    to screen it rather than drop it.

    KNOWN RESIDUAL (lens-6 LOW): once a route succeeds the driver records a permanent
    disposition, so if Harrison DISMISSES the resulting card the gap is gone with no
    answer and no escalation. That is the card being treated as the disposition, which
    is the intended design -- named here so it is a known choice.
    """
    gap_ts = str(gap.get("ts", "") or "")
    entity = str(gap.get("entity", "FNDR") or "FNDR").strip().upper()
    blocked = decision_route_block_reason(gap)
    if blocked:
        log.info("gap_autofill: D-128 route skipped for gap %s: %s",
                 gap_ts or "(no ts)", blocked)
        return None
    question = str(gap.get("question", "") or "")
    gap_text = str(gap.get("gap", "") or "")

    decision_text = (
        f"UNRESOLVED (routed by gap-autofill under D-128, not a mined fact): "
        f"{gap_text or question}\n"
        f"Asked: {question[:400]}\n"
        f"Cora flagged this exchange as uncertain, or it encodes a disagreement "
        f"between two systems of record. Deciding which source is authoritative is "
        f"a call, not a fact -- so no known_answer was staged."
    )
    update_id = f"gap-dispute-{gap_ts}"
    candidate = {
        "update_id": update_id,
        "description": f"[{entity}] Unresolved: {(gap_text or question)[:160]}",
        "payload": {
            "entity": entity,
            "decision_text": decision_text,
            "source": "gap_autofill_d128",
            "gap_ts": gap_ts,
        },
        # D-051 lens-2 HIGH (2026-08-06): source_evidence is deliberately EMPTY.
        # The first cut sent _evidence_text(evidence)[:800] -- up to 800 chars of
        # OTHER PEOPLE'S raw Slack messages -- into
        # data/cora-proposed-memory-updates.jsonl, which drive_materializer mirrors
        # to G:\...\_brain\_flywheel\ as a VERBATIM byte copy with no PHI wall (the
        # wall is on the distillation path only). That was a brand-new durable +
        # Drive-egress surface for raw mined text: before this diff the only
        # persisted evidence was Haiku's short citation string. Screening 800 chars
        # of arbitrary conversation is the wrong shape of fix -- the card does not
        # need it. The question + gap + provenance already say what is disputed and
        # why, and this also removes the DM-origin re-broadcast concern that
        # should_escalate exists to prevent.
        "source_evidence": "",
    }
    # Screen with the SAME function the drain and the durable inbox write use, at
    # PROPOSE time, so propose-time screening is not strictly weaker than the two
    # downstream belts (lens-2 MEDIUM: entity-prefix-only screening let a non-LEX-
    # entity gap naming Lexington/LBHS/LTS reach the ledger, where the drain could
    # only DISMISS it -- the row itself persists and rides into the archive).
    try:
        from .decision_inbox import screen_decision
        excluded, why = screen_decision(candidate)
    except Exception:  # noqa: BLE001 -- fail closed
        log.warning("gap_autofill: D-128 decision screen errored for gap %s -- not routing",
                    gap_ts, exc_info=True)
        return None
    if excluded:
        log.info("gap_autofill: D-128 route dropped for gap %s (decision screen: %s)",
                 gap_ts, why)
        return None
    try:
        from .knowledge_review import UPDATE_TYPE_DECISION, propose_update
        proposed = propose_update(
            update_id=update_id,
            update_type=UPDATE_TYPE_DECISION,
            description=candidate["description"],
            payload=candidate["payload"],
            source_evidence="",
            confidence="MED",
        )
    except Exception:  # noqa: BLE001 -- never break the run over a routing failure
        log.warning("gap_autofill: D-128 route failed for gap %s", gap_ts, exc_info=True)
        return None
    if not proposed:
        log.info("gap_autofill: D-128 route already proposed for gap %s (idempotent)", gap_ts)
    else:
        log.info("gap_autofill: D-128 routed gap %s to the decisions lane (%s)",
                 gap_ts, update_id)
    return update_id


def draft_answer(gap: dict[str, Any], evidence: list[Any]) -> dict[str, Any] | None:
    """Haiku drafts an answer from evidence. Fail-CLOSED: any error -> None."""
    if len(evidence) < MIN_EVIDENCE_CHUNKS:
        return None
    # Slice 3 belt at the chokepoint: the driver also calls mine_eligibility (so it
    # can route D-128 exchanges and record a disposition), but screening HERE means
    # no caller of draft_answer can stage an ineligible known_answer, and an
    # ineligible exchange never reaches the Haiku call below.
    eligible, why = mine_eligibility(gap, evidence)
    if not eligible:
        log_mine_rejection(gap.get("ts", "?"), why)
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        log.warning("gap_autofill: ANTHROPIC_API_KEY not set -- skipping draft")
        return None
    prompt = _DRAFT_PROMPT.format(
        question=gap.get("question", "")[:800],
        gap=gap.get("gap", "")[:600],
        evidence=_format_evidence(evidence),
    )
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=_HAIKU_MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        from .llm_usage import log_usage
        log_usage(response, caller="gap_autofill", model=_HAIKU_MODEL)
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = "\n".join(l for l in raw.split("\n") if not l.startswith("```")).strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            return None
        verdict = json.loads(raw[start:end + 1])
    except Exception as exc:  # noqa: BLE001 -- fail-closed by design
        log.warning("gap_autofill: Haiku draft failed for gap %s: %s",
                    gap.get("ts", "?"), exc)
        return None
    if not isinstance(verdict, dict) or not verdict.get("answerable"):
        return None
    answer = str(verdict.get("answer") or "").strip()
    # 3-predicate union (D-051, 2026-08-06): same missing admin-PHI class as
    # record_ask_answer -- a mined LEX billing/authorization answer carries no
    # clinical keyword of its own.
    if not answer or is_any_phi(answer):
        return None
    # GL-11/12 durability gate: don't propose a vague deflection / in-progress
    # status / point-in-time snapshot as canon (bias to precision).
    ok, reason = answer_quality_ok(answer)
    if not ok:
        log.info("gap_autofill: draft rejected (quality) for gap %s: %s",
                 gap.get("ts", "?"), reason)
        return None
    confidence = str(verdict.get("confidence") or "MED").upper()
    if confidence not in ("HIGH", "MED", "LOW"):
        confidence = "MED"
    return {
        "answer": answer,
        "confidence": confidence,
        "citation": str(verdict.get("citation") or "")[:300],
    }


def propose_known_answer(
    gap: dict[str, Any],
    answer: str,
    *,
    confidence: str,
    answer_source: str,
    citation: str = "",
    answered_by: str = "",
) -> str:
    """Record a known_answer proposal in the Harrison-gated review queue.

    Returns the update_id. The 7am knowledge-review run DMs Harrison; on his
    thumbs-up the executor writes design/known-answers/{entity}.md and marks
    the gap resolved.
    """
    from .knowledge_review import propose_update

    update_id = f"gapfill-{uuid.uuid4().hex[:12]}"
    src_label = {"slack_kb": "mined from Slack conversations",
                 "teammate_dm": f"answered by <@{answered_by}> via DM"}.get(
        answer_source, answer_source)
    description = (
        f"Knowledge gap fill ({gap.get('entity', 'FNDR')}) -- {src_label}\n"
        f"Q: {gap.get('question', '')[:200]}\n"
        f"Gap: {gap.get('gap', '')[:200]}\n"
        f"Proposed answer: {answer[:400]}"
    )
    propose_update(
        update_id=update_id,
        update_type=UPDATE_TYPE_KNOWN_ANSWER,
        description=description,
        payload={
            "gap_ts": gap.get("ts", ""),
            "entity": gap.get("entity", "FNDR"),
            "question": gap.get("question", ""),
            "gap": gap.get("gap", ""),
            "answer": answer,
            "answer_source": answer_source,
            "answered_by": answered_by,
            "citation": citation,
        },
        source_evidence=citation,
        confidence=confidence,
    )
    return update_id


# ---------------------------------------------------------------------------
# Stage 2 -- escalate to the entity's domain owner via DM
# ---------------------------------------------------------------------------

def lex_escalation_enabled() -> bool:
    """CORA_GAP_ESCALATION_LEX: may a LEX gap ask a LEX leader?

    Default OFF (unset/unrecognized -> off). SCRIPT-SIDE: the only consumer is
    scripts/run_gap_autofill.py, a fresh process per fire, so flipping this
    takes effect at the next run with NO restart -- unlike the web and
    delegated-work lanes, whose intake lives in the always-on bot.
    """
    return (os.environ.get("CORA_GAP_ESCALATION_LEX", "") or "").strip().lower() in (
        "on", "1", "true", "yes",
    )


def _lex_escalation_recipients(entity: str) -> list[str]:
    """LEX leaders flagged `gap_escalation: true` in the org registry.

    Roster-driven on purpose: Harrison changes WHO is asked by editing
    org-roles.yaml (60s TTL, no restart), not by a code change. Returns [] on
    any error -- an unresolvable roster means no DM, never a fallback to the
    owners map's `default` (which is Harrison, and would silently convert a
    scoped LEX ask into a founder ask).
    """
    try:
        from . import org_roles
        # Resolve against the LEX PARENT, not the exact sub-entity: LEX
        # leadership covers all four sub-entities, and Shaun/Jen are registered
        # under LEX-LLC with entities:[LEX]. Querying the raw sub-entity would
        # find nobody for an LEX-LTS or LEX-LBHS gap and silently drop the ask.
        # The roster flag alone is NOT sufficient (D-051, 2026-08-06). The
        # docstring and .env.example both advertise roster editing as the
        # low-friction change path -- 60s TTL, no restart -- so a one-line YAML
        # edit with no code review would otherwise move who receives verbatim
        # LEX question text. Custodianship is checked independently against
        # lex_phi_access, the fail-closed single source of truth. Today both
        # flagged users happen to be custodians; nothing enforced that.
        from . import lex_phi_access
        out: list[str] = []
        for r in org_roles.roles_for_entity("LEX"):
            if not (getattr(r, "gap_escalation", False) and r.slack_id):
                continue
            if not lex_phi_access.phi_allowed(r.slack_id, "LEX", is_dm=True):
                log.warning("gap_autofill: %s is flagged gap_escalation but is NOT "
                            "a LEX PHI custodian -- skipping", r.slack_id)
                continue
            out.append(r.slack_id)
        return out
    except Exception:  # noqa: BLE001 -- fail closed: no recipients, no DM
        log.warning("gap_autofill: LEX recipient resolution failed", exc_info=True)
        return []


def resolve_owner(entity: str) -> str | None:
    """Slack user ID of the domain owner for an entity, or None.

    LEX resolves through the ROSTER (gap_escalation flag), never the owners
    map: the map carries a `default: Harrison` fallback, and LEX-LTS has no
    entry at all, so a map lookup would silently route a Lexington gap to the
    founder instead of the scoped leadership pair.
    """
    entity = (entity or "").strip().upper()
    if entity.startswith("LEX"):
        recipients = _lex_escalation_recipients(entity)
        return recipients[0] if recipients else None
    path = _owners_map_path()
    if not path.exists():
        return None
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        log.warning("gap_autofill: could not read owners map: %s", exc)
        return None
    owners = data.get("owners") or {}
    return owners.get(entity) or data.get("default") or None


def _has_client_name(text: str) -> bool:
    """Bare client name in care context -- the residual the 3-predicate union
    misses ("what's the respite policy for participant Marcus"). Fail-CLOSED:
    any error reads as 'a name is present'. One branch must not screen its two
    lanes differently -- the web lane got this detector, so the DM lane does
    too (D-051, 2026-08-06)."""
    try:
        from . import phi_guard
        from .web_guard import _lex_staff_names
        return phi_guard.has_care_context_person_name(text, set(_lex_staff_names()))
    except Exception:  # noqa: BLE001 -- fail closed
        log.warning("gap_autofill: client-name screen errored -- treating as PHI",
                    exc_info=True)
        return True


def should_escalate(gap: dict[str, Any]) -> bool:
    """Eligibility for a teammate DM ask. LEX + PHI gaps never escalate.

    WS-1: DM-originated gaps never escalate either -- escalation quotes the
    question text to a domain owner, and a question asked in a private DM must
    not be re-broadcast to a third party. Mining stays allowed (its output is
    Harrison-gated, D-011).

    WS-1 review (R1b): company-finance gaps never escalate either. The
    unknown_response detector now reliably logs finance-tool misses (the
    locked UNKNOWN_RESPONSE) from TIER_1 channels, and an escalation DM would
    quote that restricted-finance question to a domain owner who may be
    financials-blocked (D-064). The D-064 canon matcher decides; the finance
    unknown already has its own #hjrg-finance notification loop, and mining
    stays allowed (Harrison-gated).
    """
    entity = (gap.get("entity") or "").strip().upper()
    if entity.startswith("LEX") and not lex_escalation_enabled():
        return False
    if gap.get("private_source") or (gap.get("channel") or "").strip().lower() == "dm":
        return False
    # kb_miss gaps are mining-only telemetry, never an owner ask: the detector
    # is retrieval-side and can fire on a question Cora answered correctly
    # from static context -- DMing an owner to supply an answer Cora already
    # gave is pure noise (adversarial review MEDIUM). unknown_response and
    # llm_sentinel gaps reflect an actual failed answer and stay eligible.
    if gap.get("detector") == "kb_miss":
        return False
    text = f"{gap.get('question', '')} {gap.get('gap', '')}"
    # Same 3-predicate PHI union as the write gates (adversarial review HIGH:
    # is_phi_risk alone misses bare clinical terms + named-person admin-PHI,
    # and escalation quotes this text verbatim to a possibly-non-custodian).
    if is_phi_risk(text) or is_clinical_phi(text) or is_lex_billing_status_phi(text):
        return False
    # LEX-ONLY. Checked here as well as at the render site so an ineligible gap
    # never burns its one-ask-ever throttle. Scoped to LEX because the detector
    # is recall-biased for a PHI boundary and over-blocks ordinary commercial
    # prose: it read "did SJ Food Brokers pay invoice 8562 yet?" as a client
    # name ("invoice" is a PHI cue, "SJ Food Brokers" is Title-case) and killed
    # a legitimate F3E escalation. Caught by test_commercial_money_gap_still_
    # escalates -- non-LEX behaviour must stay byte-identical.
    if entity.startswith("LEX") and _has_client_name(text):
        return False
    try:
        from .user_access import _financials_is_blocked
        if _financials_is_blocked(text.lower()):
            return False
    except Exception:  # noqa: BLE001 -- fail CLOSED on a guard error
        log.warning("gap_autofill: finance screen errored -- not escalating",
                    exc_info=True)
        return False
    return gap_age_hours(gap) >= ESCALATE_AFTER_HOURS


def escalate_gap(gap: dict[str, Any], slack_client: Any) -> dict[str, Any] | None:
    """DM the entity domain owner asking the gap question. One ask per gap, ever.

    Returns the pending-ask record on success, None on failure/skip.
    """
    # INDEPENDENT PHI belt at the render site. should_escalate already screens,
    # but this function composes the DM text and is reachable from any caller
    # (and from a future one), so the last thing before a client's words leave
    # for a third party re-runs the union rather than trusting an earlier gate.
    _screen_blob = f"{gap.get('question', '')} {gap.get('gap', '')}"
    try:
        _blob_entity = (gap.get("entity") or "").strip().upper()
        if (is_phi_risk(_screen_blob) or is_clinical_phi(_screen_blob)
                or is_lex_billing_status_phi(_screen_blob)
                or (_blob_entity.startswith("LEX")
                    and _has_client_name(_screen_blob))):
            log.info("gap_autofill: escalation blocked at render (PHI) entity=%s",
                     gap.get("entity", "?"))
            return None
    except Exception:  # noqa: BLE001 -- fail CLOSED
        log.warning("gap_autofill: render-site PHI screen errored -- not escalating",
                    exc_info=True)
        return None
    entity = (gap.get("entity") or "").strip().upper()
    if entity.startswith("LEX") and not lex_escalation_enabled():
        log.info("gap_autofill: LEX escalation lane is off -- skip")
        return None
    owner = resolve_owner(gap.get("entity", ""))
    if not owner:
        log.info("gap_autofill: no domain owner for entity %s -- skip escalation",
                 gap.get("entity", "?"))
        return None
    # The decline sentence must match what actually renders: with buttons off
    # nothing is attached, so naming a button would point at something that is
    # not there (and the kill switch reverts this surface byte-identically).
    try:
        from . import confirm_cards as _cc
        _buttons_on = _cc.confirm_buttons_enabled()
    except Exception:  # noqa: BLE001 -- flag unavailable: behave as pre-branch
        _buttons_on = False
    _decline_hint = ("tap a button below or just say so" if _buttons_on
                     else "just say so")
    text = (
        ":wave: Hi -- I'm trying to fill a knowledge gap and you're the best "
        f"person to ask for *{gap.get('entity', 'the portfolio')}*.\n\n"
        f"Someone asked in #{gap.get('channel', '?')}:\n"
        f"> {gap.get('question', '')[:400]}\n\n"
        f"What I couldn't answer: _{gap.get('gap', '')[:300]}_\n\n"
        "If you know the answer, *reply to this message* (a thread reply is "
        f"best) and I'll route it for approval. If it's not your area, {_decline_hint}."
    )
    # Minted BEFORE the post so the buttons can carry it (S6 migration 2).
    ask_id = f"gapask-{uuid.uuid4().hex[:12]}"
    try:
        blocks = build_ask_blocks(text, ask_id) if _buttons_on else None
    except Exception:  # noqa: BLE001 -- a card problem must never block the ask
        blocks = None
    try:
        open_resp = slack_client.conversations_open(users=[owner])
        dm_channel = open_resp["channel"]["id"]
        post_kwargs = {"channel": dm_channel, "text": text,
                       "unfurl_links": False, "unfurl_media": False}
        if blocks:
            post_kwargs["blocks"] = blocks
        post = slack_client.chat_postMessage(**post_kwargs)
    except Exception as exc:
        log.warning("gap_autofill: escalation DM to %s failed: %s", owner, exc)
        return None
    ask = {
        "ask_id": ask_id,
        "gap_ts": gap.get("ts", ""),
        "entity": gap.get("entity", "FNDR"),
        "question": gap.get("question", ""),
        "gap": gap.get("gap", ""),
        "target_user_id": owner,
        "dm_channel_id": dm_channel,
        "ask_message_ts": post.get("ts", ""),
        "asked_at": _now_iso(),
        "state": "PENDING",
    }
    asks = load_pending_asks()
    asks[ask["ask_id"]] = ask
    save_pending_asks(asks)
    log.info("gap_autofill: escalated gap %s to %s (ask %s)",
             gap.get("ts", "?"), owner, ask["ask_id"])
    return ask


def _ask_expired(ask: dict[str, Any]) -> bool:
    try:
        dt = datetime.fromisoformat(str(ask.get("asked_at", "")).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except Exception:
        return True
    return (datetime.now(timezone.utc) - dt).total_seconds() > ASK_TTL_HOURS * 3600


def match_pending_ask(
    user_id: str,
    thread_ts: str | None,
    *,
    allow_toplevel: bool = True,
) -> dict[str, Any] | None:
    """Find the pending ask this DM reply answers, if any.

    Threaded replies match on thread_ts == ask_message_ts (always wins).
    Top-level replies match only when allow_toplevel is True, the user has
    exactly one live pending ask, and the ask hasn't expired.
    """
    asks = load_pending_asks()
    live = [a for a in asks.values()
            if a.get("state") == "PENDING"
            and a.get("target_user_id") == user_id
            and not _ask_expired(a)]
    if not live:
        return None
    if thread_ts:
        for a in live:
            if a.get("ask_message_ts") == thread_ts:
                return a
        return None
    if allow_toplevel and len(live) == 1:
        return live[0]
    return None


def is_shift_keyword(text: str) -> bool:
    """True if a DM looks like an OSN shift-scheduler command, not a gap answer."""
    t = (text or "").lower().strip()
    return any(kw in t for kw in _SHIFT_KEYWORDS) and len(t.split()) <= 6


# Leading wh-interrogatives are unconditional question openers -- a declarative
# gap ANSWER essentially never starts with one.
_QUESTION_WH_RE = re.compile(
    r"^\s*(what|whats|where|when|who|whom|whose|why|how|which)\b",
    re.IGNORECASE,
)
# An auxiliary verb opens a question only when a question SUBJECT follows it
# ("do WE have...", "is THE launch...", "can YOU pull..."). It deliberately does
# NOT fire on auxiliary-led DECLARATIVE answers -- "Has to go through Hannah",
# "Should be the Tucson location", "Can be found in the shared drive", "Will
# check with Justin" -- nor on a proper-noun subject that collides with an
# auxiliary ("Will Rogers handles that", "May Chen owns it"). Those are real gap
# answers and must still be captured (D-051 review of W-DMQ). Auxiliary-led
# questions WITHOUT a following subject pronoun ("can Harrison approve this")
# are still caught when they end with '?'.
_QUESTION_AUX_RE = re.compile(
    r"^\s*(is|are|am|was|were|do|does|did|can|could|should|would|will|"
    r"has|have|had|may|might|shall)\s+"
    r"(i|we|you|he|she|it|they|the|there|that|this|these|those|"
    r"your|our|my|his|her|their|any|anyone|someone|somebody)\b",
    re.IGNORECASE,
)


def looks_like_question(text: str) -> bool:
    """True when a DM reads as a FRESH question, not an answer to a gap ask.

    W-DMQ: match_pending_ask's top-level branch greedily captures the next
    non-shift DM as the answer to a lone outstanding ask. That swallowed an
    unrelated question a teammate happened to DM while one ask was live (e.g.
    "what's our cash position across the entities?"), proposing it to Harrison
    as a bogus known-answer. Gap answers are declarative facts, so a trailing
    '?', a leading wh-interrogative, or an auxiliary-verb-plus-question-subject
    opener is a strong "this is a new question" signal -> route those to Q&A
    instead of capturing them.

    The auxiliary arm requires a following question subject (do WE / is THE /
    can YOU) so an auxiliary-LED declarative answer is not misread as a question
    and lost -- see the D-051 review notes on _QUESTION_AUX_RE above.

    Deliberately applied ONLY to the ambiguous top-level path (see the caller in
    app.handle_message_event): a reply typed in the ask's OWN thread is
    unambiguous intent to answer and always matches, question-shaped or not.
    Declines ("no idea", "not my area", "don't know") are declarative, so they
    still match here and are handled downstream by record_ask_answer.

    ACCEPTED RESIDUALS (a lexical classifier over free prose cannot perfectly
    separate a fresh question from an answer -- both directions are recoverable
    here, so we keep the rule simple rather than chase them):
      * false-positive: an ANSWER that ends in a confirmation tag ("... , right?")
        or leads with "aux + the/that" reads as a question and is routed to Q&A
        instead of captured -- the ask stays PENDING (re-escalated / digest picks
        it up; a threaded reply always captures).
      * false-negative: an aux-led question about a NAMED person ("did Justin
        send it") or an imperative ("pull up the Q1 P&L") without a trailing '?'
        is captured as a gap answer -> a Harrison-gated bogus proposal he -1's at
        the 7am review (annoyance, never a write).
    The robust disambiguation (drop top-level auto-capture; require a threaded
    reply to the ask) is a product/UX change, not this cleanup -- left as a
    follow-up option for Harrison.
    """
    t = (text or "").strip()
    if not t:
        return False
    if t.endswith("?"):
        return True
    return bool(_QUESTION_WH_RE.match(t) or _QUESTION_AUX_RE.match(t))


_DECLINE_RE = re.compile(
    r"^\s*(no idea|not my area|don'?t know|dunno|no clue|not sure|ask (someone|harrison))\b",
    re.IGNORECASE,
)

DECLINE_ACK = "No problem -- thanks for letting me know. I'll find another route."


def _mark_declined(stored: dict[str, Any], *, via: str) -> None:
    """The DECLINED state transition, shared by the typed reply and the button.

    Extracted so the two paths cannot drift: the S6 buttons must resolve a
    pending ask EXACTLY as the decline-phrase regex does (kickoff wording), and
    the only way to guarantee that over time is for both to run this code.
    `via` is additive audit metadata; nothing branches on it."""
    stored["state"] = "DECLINED"
    stored["replied_at"] = _now_iso()
    stored["declined_via"] = via


# ── Decline buttons on the escalation DM (S6 migration 2, 2026-08-09) ────────
# DECLINE ONLY, deliberately. The typed reply REMAINS the answer mechanism --
# a gap answer is free prose and no button can carry it. What buttons remove is
# the friction on the one response that IS enumerable: "this isn't mine". There
# is no answer-via-button and there must never be one.
ACTION_DECLINE_NOT_MINE = "cora_gap_decline_not_mine"
ACTION_DECLINE_UNKNOWN = "cora_gap_decline_unknown"

# Slack section-text limit is 3000; the ask body is bounded by construction
# (question capped at 400, gap at 300) so one section always suffices.
_ASK_SECTION_CHARS = 2900


def build_ask_blocks(body_text: str, ask_id: str) -> list[dict]:
    """(blocks) for one gap-escalation DM: the ask + two decline buttons.

    Values are the ask_id, which is already an opaque minted handle
    (`gapask-<hex12>`) and never a payload. Body is sanitize_text-wrapped at
    construction -- Block Kit bodies bypass the class-level WebClient egress
    patch, which only covers `text=` (D-168)."""
    text = body_text or ""
    try:
        from .slack_egress import sanitize_text
        text = sanitize_text(text)
    except Exception:  # noqa: BLE001 -- sanitizer is a belt, never a blocker
        pass
    from . import confirm_cards as _cc
    return [
        *_cc.chunk_mrkdwn_sections(text),
        {"type": "actions",
         "block_id": f"cora_gap_ask_actions_{ask_id}"[:255],
         "elements": [
             {"type": "button", "action_id": ACTION_DECLINE_NOT_MINE,
              "text": {"type": "plain_text", "text": "Not my area"},
              "value": ask_id},
             {"type": "button", "action_id": ACTION_DECLINE_UNKNOWN,
              "text": {"type": "plain_text", "text": "I don't know"},
              "value": ask_id},
         ]},
    ]


def process_decline_tap(ask_id: str, actor_id: str, *, reason: str = "not_mine"
                        ) -> tuple[str, str]:
    """Apply a decline-button tap. Returns (outcome, message).

    Outcomes: declined | not_authorized | orphaned | already_handled | expired.

    ADDRESSEE-ONLY authorization: only the person the ask was sent to may
    decline it. The ask lives in a 1:1 DM so in practice nobody else can tap --
    but a decline sends the gap back to the digest flow, and letting a third
    party trigger that from a forged payload is a denial-of-answer.

    The whole read-modify-write runs under _ASKS_LOCK so two fast taps cannot
    both see PENDING (`save_pending_asks` takes the same non-reentrant lock, so
    the underlying _write_json is used directly here)."""
    if not ask_id or not actor_id:
        return "orphaned", "I don't have a record of that question anymore."

    with _ASKS_LOCK:
        asks = _read_json(_pending_asks_path(), {})
        stored = asks.get(ask_id)
        if not stored:
            return "orphaned", "I don't have a record of that question anymore."
        if stored.get("target_user_id") != actor_id:
            return "not_authorized", "Only the person I asked can answer this one."
        if stored.get("state") != "PENDING":
            return "already_handled", "Thanks -- that one's already resolved."
        if _ask_expired(stored):
            # Honest tombstone (D-095): the ask aged out, so nothing changes.
            return "expired", ("That question expired before you got to it -- "
                               "nothing to do.")
        _mark_declined(stored, via=f"button:{reason}")
        asks[ask_id] = stored
        _write_json(_pending_asks_path(), asks)

    log.info("gap_autofill: ask %s DECLINED via button (%s) by %s",
             ask_id, reason, actor_id)
    return "declined", DECLINE_ACK


def record_ask_answer(ask: dict[str, Any], reply_text: str) -> str:
    """Capture a teammate's DM reply to a gap ask. Returns the ack message.

    Declines mark the ask DECLINED (gap stays open for the digest flow).
    Answers are proposed through the Harrison gate.
    """
    reply_text = (reply_text or "").strip()
    asks = load_pending_asks()
    stored = asks.get(ask.get("ask_id", ""), ask)

    if _DECLINE_RE.match(reply_text):
        # Locked read-modify-write (D-051 lens-2): the button path locks, and
        # this whole-dict write would otherwise revert a concurrent button
        # decline -- after that tap's card had already been edited and its
        # buttons dropped. Same lock, so the two paths serialise.
        with _ASKS_LOCK:
            fresh = _read_json(_pending_asks_path(), {})
            target = fresh.get(stored.get("ask_id", "")) or stored
            _mark_declined(target, via="reply")
            fresh[target["ask_id"]] = target
            _write_json(_pending_asks_path(), fresh)
        return DECLINE_ACK

    # 3-predicate union (D-051, 2026-08-06). is_lex_billing_status_phi was
    # MISSING here -- the D-050 admin class that exists precisely for LEX.
    # Before the CORA_GAP_ESCALATION_LEX lane no LEX reply could reach this
    # function; opening it makes exactly that inbound path live, and both
    # roster-flagged recipients are PHI custodians whose ordinary vocabulary
    # IS this class. Without it the reply persists verbatim to
    # gap_ask_pending.json and the review payload, the replier is told it was
    # routed for approval, and the durable write then silently refuses at the
    # full-union gate below.
    if is_any_phi(reply_text):
        stored["state"] = "REJECTED_PHI"
        stored["replied_at"] = _now_iso()
        asks[stored["ask_id"]] = stored
        save_pending_asks(asks)
        return ("Thanks -- but that answer looks like it contains protected "
                "health information, so I can't store it. If there's a "
                "PHI-free version, reply with that instead.")

    gap = {
        "ts": stored.get("gap_ts", ""),
        "entity": stored.get("entity", "FNDR"),
        "question": stored.get("question", ""),
        "gap": stored.get("gap", ""),
    }
    update_id = propose_known_answer(
        gap,
        reply_text[:1500],
        confidence="HIGH",
        answer_source="teammate_dm",
        answered_by=stored.get("target_user_id", ""),
        citation=f"DM reply from <@{stored.get('target_user_id', '')}>",
    )
    stored["state"] = "ANSWERED"
    stored["replied_at"] = _now_iso()
    stored["update_id"] = update_id
    stored["reply_text"] = reply_text[:1500]
    asks[stored["ask_id"]] = stored
    save_pending_asks(asks)

    # Mark the gap 'asked-then-answered' in autofill state so it isn't re-processed.
    state = load_state()
    if stored.get("gap_ts"):
        state[stored["gap_ts"]] = {
            "state": "proposed", "via": "teammate_dm",
            "update_id": update_id, "at": _now_iso(),
        }
        save_state(state)
    return ("Got it -- thanks! I've routed your answer to Harrison for "
            "approval. Once he confirms, I'll remember it.")


# ---------------------------------------------------------------------------
# Executor -- apply a Harrison-approved known_answer (called by
# run_knowledge_review.py after a thumbs-up; D-011 gate already passed)
# ---------------------------------------------------------------------------

# Canonical entity -> known-answers filename. Shared with context_loader (read
# side) and scripts/ingest_digest_answers.py (legacy write side) via
# known_answers_map so the three can never drift (WS17-B item 6/7).
from .known_answers_map import ENTITY_FILES as _ENTITY_FILES  # noqa: E402


def _append_to_section(file_path: Path, section_header: str, entry_lines: list[str]) -> None:
    """Append entry_lines under section_header, before the next ## section.

    Same insertion semantics as scripts/ingest_digest_answers.append_to_section
    so both flows produce identically-shaped known-answers files.
    """
    if not file_path.exists():
        _atomic_write_text(
            file_path,
            f"# Known Answers\n\n## Routing rules\n\n{section_header}\n",
        )
    content = file_path.read_text(encoding="utf-8")
    lines = content.rstrip("\n").split("\n")
    insert_at = len(lines)
    in_section = False
    for i, line in enumerate(lines):
        if line == section_header:
            in_section = True
            continue
        if in_section and line.startswith("## "):
            insert_at = i
            break
    lines = lines[:insert_at] + [""] + entry_lines + lines[insert_at:]
    _atomic_write_text(file_path, "\n".join(lines) + "\n")


def apply_known_answer(payload: dict[str, Any]) -> tuple[bool, str]:
    """Write an approved answer to known-answers + mark the gap resolved.

    Returns (ok, summary_message). Never raises.
    """
    try:
        entity = (payload.get("entity") or "FNDR").strip().upper()
        question = (payload.get("question") or "").strip()
        answer = (payload.get("answer") or "").strip()
        gap_desc = (payload.get("gap") or "").strip()
        gap_ts = payload.get("gap_ts") or ""
        if not answer:
            return False, "known_answer payload has no answer text -- skipped"

        # Fail-closed PHI re-check at the IRREVERSIBLE write, entity-agnostic --
        # mirrors apply_contributed_note (D-059: a durable knowledge write needs the
        # 3-predicate re-check). The clinical class is caught upstream in draft_answer,
        # but a mined LEX administrative-billing answer (named person + authorization,
        # no clinical keyword) would otherwise reach this durable write unscreened.
        # Both known-answers writers now apply the same three predicates at the write.
        _blob = f"{question}\n{answer}"
        if is_phi_risk(_blob) or is_clinical_phi(_blob) or is_lex_billing_status_phi(_blob):
            log.info("gap_autofill: known_answer refused (PHI) -- not persisted")
            return False, "answer looks like PHI -- not persisted"

        target_file = _known_answers_dir() / _ENTITY_FILES.get(entity, "fndr.md")

        # Idempotency (B6): the knowledge-review executor (on Harrison's 👍) runs this
        # BEFORE it marks the proposed update APPROVED, so a SIGKILL between the
        # two leaves the update PENDING and it re-runs next pass. apply always
        # appends, so without a guard a crash-recovery re-run duplicates the fact
        # block + the resolved line. Two guards close both crash windows:
        #   (1) gap already in the resolved ledger -> fully applied last run, no-op
        #       (covers a crash between _execute_approved_update and resolve_update).
        #   (2) otherwise skip the .md append if this exact Q/A block is already
        #       present (covers a crash between the append below and the
        #       resolved-ledger write, plus blank gap_ts which has no ledger key).
        if gap_ts and gap_ts in _load_resolved_ids():
            log.info("gap_autofill: gap %s already resolved -- skipping duplicate apply",
                     gap_ts)
            return True, "gap already resolved -- skipped duplicate write"

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        source_note = {"slack_kb": "mined from Slack",
                       "teammate_dm": "teammate DM"}.get(
            payload.get("answer_source", ""), payload.get("answer_source", ""))
        entry_lines = [
            f"**[{today}] {gap_desc[:80]}** _(gap autofill -- {source_note})_",
            f"Q: {question}",
            f"A: {answer}",
            "",
        ]
        existing = target_file.read_text(encoding="utf-8") if target_file.exists() else ""
        # Anchor the dedup to a real fact block (Q-line at line start, A-line ending
        # a line) so a bare "Q:..\nA:.." embedded inside ANOTHER entry's answer body
        # can't false-positive-skip a distinct new gap (adversarial review LOW).
        block_re = re.compile(
            r"^Q: " + re.escape(question) + r"\nA: " + re.escape(answer) + r"$",
            re.MULTILINE,
        )
        if existing and block_re.search(existing):
            log.info("gap_autofill: identical Q/A already in %s -- skipping append",
                     target_file.name)
        else:
            _append_to_section(target_file, "## Known facts", entry_lines)

        if gap_ts:
            resolved_path = _resolved_path()
            resolved_path.parent.mkdir(parents=True, exist_ok=True)
            with resolved_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "id": gap_ts,
                    "action": "answer",
                    "timestamp": _now_iso(),
                    "target_entity": entity,
                    "captured_entity": entity,
                    "source": "gap_autofill",
                    "answer_source": payload.get("answer_source", ""),
                }, ensure_ascii=False) + "\n")

        log.info("gap_autofill: applied known answer for gap %s -> %s",
                 gap_ts or "?", target_file.name)
        return True, (f"answer written to design/known-answers/{target_file.name} "
                      f"(entity {entity}); gap marked resolved")
    except Exception as exc:  # noqa: BLE001 -- executor must not crash the run
        log.error("gap_autofill: apply_known_answer failed: %s", exc, exc_info=True)
        return False, f"apply failed: {exc}"


def apply_contributed_note(payload: dict[str, Any]) -> tuple[bool, str]:
    """Write a Harrison-approved knowledge contribution to known-answers.

    WS17-B item 5 + WS17-C fold. A free-form fact (no Q/A, no gap_ts) from
    #info-for-cora OR a folded team note/bookmark/correction (all proposed with
    payload source 'info-for-cora'); on Harrison's 👍 it persists to the entity's
    known-answers file (the same runtime-loaded store gap fills use) instead of
    only posting a Slack suggestion. payload['kind']/['channel'] drive the
    provenance label. Never raises (executor safety).
    """
    try:
        entity = (payload.get("entity") or "FNDR").strip().upper()
        # Blanket LEX refusal at the WRITE (Harrison mandate 2026-08-06). The
        # info_intake skip alone does NOT close known-answers/lex.md: this executor
        # is reachable from any producer that proposes a source="info-for-cora"
        # generic, and the ingest-side screen only guards the #info-for-cora routes.
        #
        # NAMED CONSEQUENCE: this also blocks the LEX-channel team-note confirm-fold,
        # which proposes with the same source and entity = the channel. That fold has
        # never fired (channel message events do not reach the app -- two orphaned
        # paraphrases, 6/06 and 6/30), so nothing live is lost today; it would matter
        # only if the dark message event is ever lit up. Flagged in the cascade report
        # so it is a decision rather than an accident.
        if entity.startswith("LEX"):
            log.info("gap_autofill: contributed note refused (LEX entity) -- not persisted")
            return False, "LEX contributions are not captured through this path"
        # Normalize to a single line so the dedup search below is reliable and the
        # stored fact is a clean one-liner (adversarial review LOW: a multi-line
        # contribution otherwise defeated the line-anchored dedup regex).
        text = re.sub(r"\s+", " ", (payload.get("text") or payload.get("note") or "")).strip()
        if not text:
            return False, "info-for-cora payload has no text -- skipped"
        # R5a belt (D-123 class): this file is ALWAYS-INJECTED context and the
        # egress boundary deliberately preserves `<...>`, so a live `<!channel>`
        # broadcast or a labelled attacker link must never be written here.
        # info_intake already scrubs before proposing; this covers every OTHER
        # producer that reaches this executor (the team-note fold, backfills).
        from .info_intake import scrub_contribution
        text = scrub_contribution(text)
        # Fail-closed PHI re-check at the IRREVERSIBLE write (adversarial review
        # MEDIUM). This is a durable write to an always-loaded known-answers file;
        # the #info-for-cora intake admin-PHI gate is LEX-ASKER-scoped, so a non-LEX
        # asker pasting a named LEX client's billing/auth status would slip through.
        # Apply the base PHI check, the clinical diagnosis/medication check
        # (is_clinical_phi -- WS17-B pre-merge fix; closes the autism/ADHD/nonverbal/
        # risperidone class is_phi_risk misses), AND the LEX admin augmentation
        # UNCONDITIONALLY here (entity-agnostic) -- a missed legit fact is a far
        # cheaper error than persisting PHI into a durable knowledge surface.
        if is_phi_risk(text) or is_lex_billing_status_phi(text) or is_clinical_phi(text):
            log.info("gap_autofill: contributed note refused (PHI) -- not persisted")
            return False, "contribution looks like PHI -- not persisted"
        author = (payload.get("author_name") or "").strip()
        # Source-aware provenance (WS17-C): a folded team note/bookmark/correction
        # records the channel it came from; a #info-for-cora post records that.
        kind = (payload.get("kind") or "").strip().lower()
        channel = (payload.get("channel") or "").strip()
        if kind in ("note", "correction", "bookmark") and channel and channel != "info-for-cora":
            src_label = {"bookmark": "Bookmark", "correction": "Correction"}.get(kind, "Team note")
            where = f" from #{channel}"
        else:
            src_label = "Team note"
            where = " via #info-for-cora"
        by = f" by {author}" if author else ""

        target_file = _known_answers_dir() / _ENTITY_FILES.get(entity, "fndr.md")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        entry_lines = [
            f"**[{today}] {src_label}{where}{by}**",
            text,
            "",
        ]
        existing = target_file.read_text(encoding="utf-8") if target_file.exists() else ""
        # Dedup on the exact fact text on its own line so the same contribution
        # approved twice isn't written twice.
        line_re = re.compile(r"^" + re.escape(text) + r"$", re.MULTILINE)
        if existing and line_re.search(existing):
            log.info("gap_autofill: contributed note already in %s -- skipping",
                     target_file.name)
        else:
            _append_to_section(target_file, "## Known facts", entry_lines)

        log.info("gap_autofill: applied #info-for-cora note -> %s (entity %s)",
                 target_file.name, entity)
        return True, (f"contribution written to design/known-answers/"
                      f"{target_file.name} (entity {entity})")
    except Exception as exc:  # noqa: BLE001 -- executor must not crash the run
        log.error("gap_autofill: apply_contributed_note failed: %s", exc, exc_info=True)
        return False, f"apply failed: {exc}"
