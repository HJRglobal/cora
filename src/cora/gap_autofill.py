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
  - LEX: escalation DMs are skipped entirely for LEX* gaps.
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
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from .phi_guard import is_phi_risk, is_lex_billing_status_phi, is_clinical_phi

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
        content = getattr(r, "content", "") or ""
        if is_phi_risk(content):
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


def draft_answer(gap: dict[str, Any], evidence: list[Any]) -> dict[str, Any] | None:
    """Haiku drafts an answer from evidence. Fail-CLOSED: any error -> None."""
    if len(evidence) < MIN_EVIDENCE_CHUNKS:
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
    if not answer or is_phi_risk(answer) or is_clinical_phi(answer):
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

def resolve_owner(entity: str) -> str | None:
    """Slack user ID of the domain owner for an entity, or None."""
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
    entity = (entity or "").strip().upper()
    return owners.get(entity) or data.get("default") or None


def should_escalate(gap: dict[str, Any]) -> bool:
    """Eligibility for a teammate DM ask. LEX + PHI gaps never escalate."""
    entity = (gap.get("entity") or "").strip().upper()
    if entity.startswith("LEX"):
        return False
    text = f"{gap.get('question', '')} {gap.get('gap', '')}"
    if is_phi_risk(text):
        return False
    return gap_age_hours(gap) >= ESCALATE_AFTER_HOURS


def escalate_gap(gap: dict[str, Any], slack_client: Any) -> dict[str, Any] | None:
    """DM the entity domain owner asking the gap question. One ask per gap, ever.

    Returns the pending-ask record on success, None on failure/skip.
    """
    owner = resolve_owner(gap.get("entity", ""))
    if not owner:
        log.info("gap_autofill: no domain owner for entity %s -- skip escalation",
                 gap.get("entity", "?"))
        return None
    text = (
        ":wave: Hi -- I'm trying to fill a knowledge gap and you're the best "
        f"person to ask for *{gap.get('entity', 'the portfolio')}*.\n\n"
        f"Someone asked in #{gap.get('channel', '?')}:\n"
        f"> {gap.get('question', '')[:400]}\n\n"
        f"What I couldn't answer: _{gap.get('gap', '')[:300]}_\n\n"
        "If you know the answer, *reply to this message* (a thread reply is "
        "best) and I'll route it for approval. If it's not your area, just say so."
    )
    try:
        open_resp = slack_client.conversations_open(users=[owner])
        dm_channel = open_resp["channel"]["id"]
        post = slack_client.chat_postMessage(
            channel=dm_channel, text=text, unfurl_links=False, unfurl_media=False,
        )
    except Exception as exc:
        log.warning("gap_autofill: escalation DM to %s failed: %s", owner, exc)
        return None
    ask = {
        "ask_id": f"gapask-{uuid.uuid4().hex[:12]}",
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


_DECLINE_RE = re.compile(
    r"^\s*(no idea|not my area|don'?t know|dunno|no clue|not sure|ask (someone|harrison))\b",
    re.IGNORECASE,
)


def record_ask_answer(ask: dict[str, Any], reply_text: str) -> str:
    """Capture a teammate's DM reply to a gap ask. Returns the ack message.

    Declines mark the ask DECLINED (gap stays open for the digest flow).
    Answers are proposed through the Harrison gate.
    """
    reply_text = (reply_text or "").strip()
    asks = load_pending_asks()
    stored = asks.get(ask.get("ask_id", ""), ask)

    if _DECLINE_RE.match(reply_text):
        stored["state"] = "DECLINED"
        stored["replied_at"] = _now_iso()
        asks[stored["ask_id"]] = stored
        save_pending_asks(asks)
        return "No problem -- thanks for letting me know. I'll find another route."

    if is_phi_risk(reply_text) or is_clinical_phi(reply_text):
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
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(
            f"# Known Answers\n\n## Routing rules\n\n{section_header}\n",
            encoding="utf-8",
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
    file_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


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

        target_file = _known_answers_dir() / _ENTITY_FILES.get(entity, "fndr.md")

        # Idempotency (B6): the knowledge-review auto-approve path executes this
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
        # Normalize to a single line so the dedup search below is reliable and the
        # stored fact is a clean one-liner (adversarial review LOW: a multi-line
        # contribution otherwise defeated the line-anchored dedup regex).
        text = re.sub(r"\s+", " ", (payload.get("text") or payload.get("note") or "")).strip()
        if not text:
            return False, "info-for-cora payload has no text -- skipped"
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
