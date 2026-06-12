"""Efficiency mining pass -- Org Synthesis Phase 3 ("process friction").

Weekly standalone scheduled script (NOT bot-process code -- importing this
module must never pull in app.py / tool_dispatch / claude_client). Mines the
swept KB corpus (slack / gmail / fireflies chunks, last LOOKBACK_DAYS) plus
Cora's own question logs for four classes of process-friction signal:

  1. repeated_question      -- semantically similar questions asked 3+ times,
                               either to Cora (knowledge-gaps.jsonl + the
                               semantic_cache table) or between humans in
                               swept Slack. Fix: known-answer entry / doc /
                               new tool.
  2. repeated_manual_steps  -- recurring manual rituals described in
                               conversation ("every week I have to export...").
                               Fix routed per D-029: rule-based mechanical ->
                               Make.com idea; language/context -> Cora tool.
  3. stale_handoff          -- a request/commitment between people with no
                               semantically-similar follow-up signal in the
                               7 days after it aged past HANDOFF_STALE_DAYS.
  4. cross_entity_duplication -- the same vendor/spend/process appearing in
                               2+ entities ("should this live at the holdco?"
                               lens from the founder brief).

Each surviving finding becomes ONE proposal in the existing 7am
knowledge-review DM queue via knowledge_review.propose_update with the new
update_type="efficiency". Harrison's thumbs-up routes it (via the
run_knowledge_review.py executor) into design/efficiency-backlog.md
(append-only); a thumbs-down simply resolves it. Either way the finding's
fingerprint is recorded in the ledger AT PROPOSAL TIME (D-030 ID-dedup
pattern), so the same finding is never re-proposed regardless of outcome.

Hard rules (locked):
  - Haiku drafting is FAIL-CLOSED: any API/parse error proposes nothing
    (gap_autofill pattern).
  - LEX / LEX-* chunks and PHI-flagged content are NEVER mined (stronger than
    reconciliation passes 1-4: LEX is excluded entirely at the SQL layer, and
    is_phi_risk() drops flagged content from ANY entity).
  - Visibility CPA threads are excluded.
  - org-roles is ADVISORY context for routing recommendations only -- it
    never expands access (D-044).
  - NOTHING auto-executes; every output is a proposal behind Harrison's
    thumbs-up (D-011).
  - Cap: MAX_PROPOSALS_PER_RUN proposals per run, highest-confidence first.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable

from .phi_guard import is_phi_risk, is_visibility_cpa_mention
from .reconciliation_engine import _cosine_sim, _extract_sentences

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

UPDATE_TYPE_EFFICIENCY = "efficiency"

SIGNAL_REPEATED_QUESTION = "repeated_question"
SIGNAL_MANUAL_STEPS = "repeated_manual_steps"
SIGNAL_STALE_HANDOFF = "stale_handoff"
SIGNAL_CROSS_ENTITY_DUP = "cross_entity_duplication"

_SIGNAL_LABELS = {
    SIGNAL_REPEATED_QUESTION: "Repeated question",
    SIGNAL_MANUAL_STEPS: "Repeated manual steps",
    SIGNAL_STALE_HANDOFF: "Stale handoff",
    SIGNAL_CROSS_ENTITY_DUP: "Cross-entity duplication",
}

LOOKBACK_DAYS = 14                  # mining window over the swept corpus
MAX_PROPOSALS_PER_RUN = 5           # hard cap on proposals per run
MAX_HAIKU_CANDIDATES = 12           # findings sent to Haiku per run (cost cap)
REPEAT_MIN_COUNT = 3                # cluster weight to count as "repeated"
MANUAL_MIN_COUNT = 2                # manual-ritual cluster size to surface
CLUSTER_SIM = 0.82                  # cosine sim for same-question clustering
FOLLOWUP_SIM = 0.72                 # cosine sim that counts as a follow-up
HANDOFF_STALE_DAYS = 7              # request must be at least this old
FUZZY_DEDUP_RATIO = 0.85            # ledger paraphrase-dedup threshold
_HAIKU_MODEL = "claude-haiku-4-5"

# Pool caps -- keep embedding spend bounded regardless of corpus size.
_MAX_QUESTION_POOL = 400
_MAX_HANDOFF_CANDIDATES = 60
_MAX_FOLLOWUP_POOL = 500
_MAX_VENDOR_POOL = 300

_MINED_SOURCES = ("slack", "gmail", "fireflies")

_CONF_RANK = {"HIGH": 0, "MED": 1, "LOW": 2}

# ---------------------------------------------------------------------------
# Signal regexes
# ---------------------------------------------------------------------------

# Recurrence cues for manual-ritual detection.
_RECURRENCE_RE = re.compile(
    r"\b(every\s+(?:single\s+)?(?:week|month|day|morning|monday|tuesday|wednesday"
    r"|thursday|friday|time)|each\s+(?:week|month|day|time)|weekly|monthly|daily"
    r"|once\s+a\s+(?:week|month|day)|as\s+usual|yet\s+again|all\s+over\s+again"
    r"|keeps?\s+(?:happening|coming\s+up)|always\s+(?:have|has|need|ends?\s+up))\b",
    re.IGNORECASE,
)

# Manual-action cues (the ritual itself).
_MANUAL_RE = re.compile(
    r"\b(manual(?:ly)?|copy(?:ing)?[\s-]*past(?:e|ing)?|export(?:ing|s)?"
    r"|re-?enter(?:ing)?|re-?typ(?:e|ing)|spreadsheet|by\s+hand|one\s+by\s+one"
    r"|upload(?:ing)?|download(?:ing)?|fill(?:ing)?\s+(?:in|out)"
    r"|update\s+(?:the|that|this|each)|type\s+(?:in|up|out)|paste\s+(?:in|into)"
    r"|(?:have|has|need|needs)\s+to\s+(?:go\s+)?(?:in(?:to)?\s+and\s+)?"
    r"(?:pull|send|enter|update|export|copy|upload|rebuild|redo|reconcile))\b",
    re.IGNORECASE,
)

# Handoff / request cues between people.
_HANDOFF_RE = re.compile(
    r"\b(can\s+you|could\s+you|please\s+(?:send|share|get|update|review|confirm"
    r"|sign|approve)|waiting\s+(?:on|for)|still\s+need(?:s)?\s+(?:the|your|a)"
    r"|i'?ll\s+(?:send|get|share)\s+(?:you|it|that|this)\s*(?:over|across)?"
    r"|will\s+(?:send|get\s+back|circle\s+back|follow\s+up)"
    r"|when\s+you\s+get\s+a\s+chance|any\s+update\s+on|haven'?t\s+(?:heard|received|seen))\b",
    re.IGNORECASE,
)

# Vendor / spend / process cues for cross-entity duplication.
_VENDOR_RE = re.compile(
    r"(\$\s?\d|invoice|subscription|renewal|vendor|contract|license|saas"
    r"|per\s+month|per\s+year|monthly\s+fee|annual\s+fee|signed\s+up\s+(?:for|with)"
    r"|paying\s+for|switch(?:ed|ing)?\s+to|onboard(?:ed|ing)?\s+with)",
    re.IGNORECASE,
)

_QUESTION_MIN_LEN = 20
_QUESTION_MAX_LEN = 240

# Email quoted-reply marker. A ">"-prefixed line is a COPY of an earlier
# message, not a fresh occurrence -- counting them inflates frequency
# (observed live 2026-06-11: a single email line counted 134x via re-quotes).
_QUOTE_LINE_RE = re.compile(r"^\s*>")


def _is_quoted(sentence: str) -> bool:
    return bool(_QUOTE_LINE_RE.match(sentence))


# ---------------------------------------------------------------------------
# Paths (env-overridable for tests)
# ---------------------------------------------------------------------------

def _kb_db_path() -> Path:
    return Path(os.environ.get("FRICTION_KB_DB_PATH")
                or _REPO_ROOT / "data" / "cora_kb.db")


def _gaps_log_path() -> Path:
    return Path(os.environ.get("KNOWLEDGE_GAPS_LOG_PATH")
                or _REPO_ROOT / "logs" / "knowledge-gaps.jsonl")


def _ledger_path() -> Path:
    return Path(os.environ.get("FRICTION_LEDGER_PATH")
                or _REPO_ROOT / "data" / "state" / "friction-fingerprints.jsonl")


def _backlog_path() -> Path:
    return Path(os.environ.get("EFFICIENCY_BACKLOG_PATH")
                or _REPO_ROOT / "design" / "efficiency-backlog.md")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class FrictionFinding:
    """One detected friction signal, pre-drafting."""

    signal_type: str
    entity: str                       # primary entity (FNDR for cross-entity)
    representative: str               # canonical text -- fingerprint basis
    count: int                        # observed occurrences in the window
    entities: list[str] = field(default_factory=list)
    evidence: list[dict[str, str]] = field(default_factory=list)  # max 3

    @property
    def frequency(self) -> str:
        return f"observed {self.count}x in the last {LOOKBACK_DAYS} days"

    @property
    def fingerprint(self) -> str:
        return compute_fingerprint(self.signal_type, self.representative)


def compute_fingerprint(signal_type: str, representative: str) -> str:
    norm = re.sub(r"[^a-z0-9]+", " ", (representative or "").lower()).strip()[:160]
    return f"{signal_type}:{hashlib.md5((signal_type + '|' + norm).encode()).hexdigest()[:12]}"


def _evidence_item(chunk: dict[str, Any], excerpt: str) -> dict[str, str]:
    return {
        "excerpt": (excerpt or "")[:300],
        "title": (chunk.get("title") or "")[:120],
        "source": chunk.get("source") or "",
    }


# ---------------------------------------------------------------------------
# Corpus loading -- LEX excluded at SQL layer; PHI + Visibility CPA dropped
# ---------------------------------------------------------------------------

def query_chunks(
    *,
    lookback_days: int = LOOKBACK_DAYS,
    sources: tuple[str, ...] = _MINED_SOURCES,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Swept-corpus chunks for the mining window.

    LEX / LEX-* entities are excluded in SQL (never even read); PHI-flagged
    content and Visibility CPA mentions are dropped for ALL entities.
    """
    db_path = db_path or _kb_db_path()
    if not db_path.exists():
        log.warning("friction_mining: KB DB not found at %s", db_path)
        return []
    cutoff = int(time.time() - lookback_days * 86400)
    placeholders = ",".join("?" * len(sources))
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            f"""
            SELECT source, source_id, entity, content, title, ingested_at
            FROM knowledge_chunks
            WHERE ingested_at >= ?
              AND source IN ({placeholders})
              AND entity NOT LIKE 'LEX%'
              AND (sub_entity IS NULL OR sub_entity NOT LIKE 'LEX%')
            ORDER BY ingested_at DESC
            """,
            [cutoff, *sources],
        ).fetchall()
    finally:
        conn.close()

    chunks: list[dict[str, Any]] = []
    for source, source_id, entity, content, title, ingested_at in rows:
        content = content or ""
        if is_phi_risk(content):
            continue
        if is_visibility_cpa_mention(content):
            continue
        chunks.append({
            "source": source or "",
            "source_id": source_id or "",
            "entity": (entity or "FNDR").upper(),
            "content": content,
            "title": title or "",
            "ingested_at": ingested_at or 0,
        })
    return chunks


def load_gap_questions(*, lookback_days: int = LOOKBACK_DAYS) -> list[dict[str, str]]:
    """Recent questions Cora could not answer (knowledge-gaps.jsonl)."""
    path = _gaps_log_path()
    if not path.exists():
        return []
    cutoff = datetime.now(timezone.utc).timestamp() - lookback_days * 86400
    out: list[dict[str, str]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        entity = str(rec.get("entity") or "FNDR").upper()
        if entity.startswith("LEX"):
            continue
        question = str(rec.get("question") or "").strip()
        if not question or is_phi_risk(question) or is_visibility_cpa_mention(question):
            continue
        try:
            ts = datetime.fromisoformat(str(rec.get("ts", "")).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts.timestamp() < cutoff:
                continue
        except Exception:
            continue
        out.append({"text": question, "entity": entity, "origin": "cora_gap"})
    return out


def load_cache_questions(
    *,
    lookback_days: int = LOOKBACK_DAYS,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Recent questions asked to Cora, from the semantic_cache table.

    hit_count rides along as extra weight (a cache hit means the same
    question was asked again within the TTL).
    """
    db_path = db_path or _kb_db_path()
    if not db_path.exists():
        return []
    cutoff = int(time.time() - lookback_days * 86400)
    conn = sqlite3.connect(str(db_path))
    try:
        try:
            rows = conn.execute(
                """
                SELECT entity, question, hit_count
                FROM semantic_cache
                WHERE created_at >= ? AND entity NOT LIKE 'LEX%'
                """,
                [cutoff],
            ).fetchall()
        except sqlite3.OperationalError:
            return []   # table absent (fresh DB) -- not an error
    finally:
        conn.close()
    out: list[dict[str, Any]] = []
    for entity, question, hit_count in rows:
        question = (question or "").strip()
        if not question or is_phi_risk(question) or is_visibility_cpa_mention(question):
            continue
        out.append({
            "text": question,
            "entity": (entity or "FNDR").upper(),
            "origin": "cora_cache",
            "weight": 1 + int(hit_count or 0),
        })
    return out


# ---------------------------------------------------------------------------
# Embedding + clustering helpers
# ---------------------------------------------------------------------------

def _default_embed(texts: list[str]) -> list[list[float]]:
    """Lazy import so tests can run without the openai dependency."""
    from cora.knowledge_base.embeddings import embed_texts
    return embed_texts(texts)


def _safe_embed(texts: list[str], embed_fn: Callable | None) -> list[list[float]]:
    """Embed texts; [] on ANY failure (detectors then yield nothing -- the
    overall run stays fail-closed: no embeddings, no findings, no proposals)."""
    if not texts:
        return []
    fn = embed_fn or _default_embed
    try:
        vecs = fn(texts)
    except Exception as exc:  # noqa: BLE001 -- fail-soft per detector
        log.warning("friction_mining: embedding failed (%s) -- detector yields nothing", exc)
        return []
    if not vecs or len(vecs) != len(texts):
        return []
    return vecs


def greedy_cluster(vecs: list[list[float]], sim_threshold: float = CLUSTER_SIM) -> list[list[int]]:
    """Greedy single-pass clustering: each item joins the first cluster whose
    seed it matches at >= sim_threshold, else starts a new cluster.
    Deterministic for a given input order."""
    clusters: list[list[int]] = []
    seeds: list[list[float]] = []
    for i, v in enumerate(vecs):
        placed = False
        for c_idx, seed in enumerate(seeds):
            if _cosine_sim(v, seed) >= sim_threshold:
                clusters[c_idx].append(i)
                placed = True
                break
        if not placed:
            clusters.append([i])
            seeds.append(v)
    return clusters


def _extract_question_sentences(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Question-shaped sentences from swept human conversation."""
    out: list[dict[str, Any]] = []
    for chunk in chunks:
        for sentence in _extract_sentences(chunk["content"]):
            if _is_quoted(sentence):
                continue
            if "?" not in sentence:
                continue
            if not (_QUESTION_MIN_LEN <= len(sentence) <= _QUESTION_MAX_LEN):
                continue
            out.append({
                "text": sentence,
                "entity": chunk["entity"],
                "origin": "slack_human" if chunk["source"] == "slack" else chunk["source"],
                "chunk": chunk,
            })
            if len(out) >= _MAX_QUESTION_POOL:
                return out
    return out


# ---------------------------------------------------------------------------
# Detector 1: repeated questions
# ---------------------------------------------------------------------------

def detect_repeated_questions(
    chunks: list[dict[str, Any]],
    gap_questions: list[dict[str, Any]],
    cache_questions: list[dict[str, Any]],
    *,
    embed_fn: Callable | None = None,
) -> list[FrictionFinding]:
    """Semantically similar questions asked REPEAT_MIN_COUNT+ times."""
    pool: list[dict[str, Any]] = []
    pool.extend(gap_questions)
    pool.extend(cache_questions)
    pool.extend(_extract_question_sentences(chunks))
    if not pool:
        return []
    pool = pool[:_MAX_QUESTION_POOL]
    vecs = _safe_embed([p["text"] for p in pool], embed_fn)
    if not vecs:
        return []

    findings: list[FrictionFinding] = []
    for cluster in greedy_cluster(vecs):
        members = [pool[i] for i in cluster]
        weight = sum(int(m.get("weight", 1)) for m in members)
        if weight < REPEAT_MIN_COUNT:
            continue
        rep = min((m["text"] for m in members), key=len)
        entities = sorted({m["entity"] for m in members})
        evidence: list[dict[str, str]] = []
        for m in members[:3]:
            chunk = m.get("chunk") or {"title": f"({m['origin']})", "source": m["origin"]}
            evidence.append(_evidence_item(chunk, m["text"]))
        findings.append(FrictionFinding(
            signal_type=SIGNAL_REPEATED_QUESTION,
            entity=entities[0] if len(entities) == 1 else "FNDR",
            entities=entities,
            representative=rep,
            count=weight,
            evidence=evidence,
        ))
    return findings


# ---------------------------------------------------------------------------
# Detector 2: repeated manual steps
# ---------------------------------------------------------------------------

def detect_manual_steps(
    chunks: list[dict[str, Any]],
    *,
    embed_fn: Callable | None = None,
) -> list[FrictionFinding]:
    """Recurring manual rituals described in conversation.

    A sentence is a candidate when it carries BOTH a recurrence cue and a
    manual-action cue. A single sentence with an explicit recurrence phrase
    counts on its own; otherwise MANUAL_MIN_COUNT similar sentences must
    cluster together.
    """
    candidates: list[dict[str, Any]] = []
    for chunk in chunks:
        for sentence in _extract_sentences(chunk["content"]):
            if _is_quoted(sentence) or len(sentence) < 25 or len(sentence) > 400:
                continue
            if _RECURRENCE_RE.search(sentence) and _MANUAL_RE.search(sentence):
                candidates.append({"text": sentence, "entity": chunk["entity"], "chunk": chunk})
                if len(candidates) >= _MAX_QUESTION_POOL:
                    break
        if len(candidates) >= _MAX_QUESTION_POOL:
            break
    if not candidates:
        return []
    vecs = _safe_embed([c["text"] for c in candidates], embed_fn)
    if not vecs:
        return []

    findings: list[FrictionFinding] = []
    for cluster in greedy_cluster(vecs):
        members = [candidates[i] for i in cluster]
        # Explicit "every week/month/..." phrasing is strong enough alone.
        explicit = any(
            re.search(r"\bevery\s+(?:single\s+)?(?:week|month|day|morning|time)\b",
                      m["text"], re.IGNORECASE)
            for m in members
        )
        if len(members) < MANUAL_MIN_COUNT and not explicit:
            continue
        rep = min((m["text"] for m in members), key=len)
        entities = sorted({m["entity"] for m in members})
        findings.append(FrictionFinding(
            signal_type=SIGNAL_MANUAL_STEPS,
            entity=entities[0] if len(entities) == 1 else "FNDR",
            entities=entities,
            representative=rep,
            count=len(members),
            evidence=[_evidence_item(m["chunk"], m["text"]) for m in members[:3]],
        ))
    return findings


# ---------------------------------------------------------------------------
# Detector 3: stale handoffs
# ---------------------------------------------------------------------------

def detect_stale_handoffs(
    chunks: list[dict[str, Any]],
    *,
    embed_fn: Callable | None = None,
    now: float | None = None,
) -> list[FrictionFinding]:
    """Requests/commitments aged HANDOFF_STALE_DAYS+ with no semantically
    similar follow-up content ingested afterwards."""
    now = now or time.time()
    stale_cutoff = now - HANDOFF_STALE_DAYS * 86400

    old_candidates: list[dict[str, Any]] = []
    followup_pool: list[dict[str, Any]] = []
    for chunk in chunks:
        is_old = chunk["ingested_at"] < stale_cutoff
        for sentence in _extract_sentences(chunk["content"]):
            if _is_quoted(sentence) or len(sentence) < 25 or len(sentence) > 400:
                continue
            if is_old:
                if _HANDOFF_RE.search(sentence) and len(old_candidates) < _MAX_HANDOFF_CANDIDATES:
                    old_candidates.append({"text": sentence, "entity": chunk["entity"], "chunk": chunk})
            elif len(followup_pool) < _MAX_FOLLOWUP_POOL:
                followup_pool.append({"text": sentence})
    if not old_candidates:
        return []

    all_texts = [c["text"] for c in old_candidates] + [f["text"] for f in followup_pool]
    vecs = _safe_embed(all_texts, embed_fn)
    if not vecs:
        return []
    old_vecs = vecs[:len(old_candidates)]
    follow_vecs = vecs[len(old_candidates):]

    findings: list[FrictionFinding] = []
    for cand, vec in zip(old_candidates, old_vecs):
        followed_up = any(_cosine_sim(vec, fv) >= FOLLOWUP_SIM for fv in follow_vecs)
        if followed_up:
            continue
        findings.append(FrictionFinding(
            signal_type=SIGNAL_STALE_HANDOFF,
            entity=cand["entity"],
            entities=[cand["entity"]],
            representative=cand["text"],
            count=1,
            evidence=[_evidence_item(cand["chunk"], cand["text"])],
        ))
    return findings


# ---------------------------------------------------------------------------
# Detector 4: cross-entity duplication
# ---------------------------------------------------------------------------

def detect_cross_entity_duplication(
    chunks: list[dict[str, Any]],
    *,
    embed_fn: Callable | None = None,
) -> list[FrictionFinding]:
    """The same vendor/spend/process discussed in 2+ entities."""
    candidates: list[dict[str, Any]] = []
    for chunk in chunks:
        entity = chunk["entity"]
        if entity in ("FNDR", "HJRG"):
            continue   # aggregators discuss everything -- not a duplication signal
        for sentence in _extract_sentences(chunk["content"]):
            if _is_quoted(sentence) or len(sentence) < 25 or len(sentence) > 400:
                continue
            if _VENDOR_RE.search(sentence):
                candidates.append({"text": sentence, "entity": entity, "chunk": chunk})
                if len(candidates) >= _MAX_VENDOR_POOL:
                    break
        if len(candidates) >= _MAX_VENDOR_POOL:
            break
    if not candidates:
        return []
    vecs = _safe_embed([c["text"] for c in candidates], embed_fn)
    if not vecs:
        return []

    findings: list[FrictionFinding] = []
    for cluster in greedy_cluster(vecs):
        members = [candidates[i] for i in cluster]
        entities = sorted({m["entity"] for m in members})
        if len(entities) < 2:
            continue
        rep = min((m["text"] for m in members), key=len)
        # one evidence excerpt per entity, up to 3
        evidence: list[dict[str, str]] = []
        seen_entities: set[str] = set()
        for m in members:
            if m["entity"] in seen_entities:
                continue
            seen_entities.add(m["entity"])
            evidence.append(_evidence_item(m["chunk"], m["text"]))
            if len(evidence) >= 3:
                break
        findings.append(FrictionFinding(
            signal_type=SIGNAL_CROSS_ENTITY_DUP,
            entity="FNDR",
            entities=entities,
            representative=rep,
            count=len(members),
            evidence=evidence,
        ))
    return findings


# ---------------------------------------------------------------------------
# Fingerprint ledger (D-030 ID-dedup pattern; recorded at proposal time)
# ---------------------------------------------------------------------------

def load_ledger() -> list[dict[str, Any]]:
    path = _ledger_path()
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def is_already_proposed(finding: FrictionFinding, ledger: list[dict[str, Any]]) -> bool:
    """Exact fingerprint OR same-signal paraphrase (fuzzy >= FUZZY_DEDUP_RATIO)."""
    fp = finding.fingerprint
    rep = (finding.representative or "").lower()
    for entry in ledger:
        if entry.get("fingerprint") == fp:
            return True
        if entry.get("signal_type") == finding.signal_type:
            prior = str(entry.get("representative") or "").lower()
            if prior and SequenceMatcher(None, rep, prior).ratio() >= FUZZY_DEDUP_RATIO:
                return True
    return False


def record_proposal(finding: FrictionFinding, update_id: str) -> None:
    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "fingerprint": finding.fingerprint,
            "signal_type": finding.signal_type,
            "representative": finding.representative[:300],
            "entity": finding.entity,
            "update_id": update_id,
            "proposed_at": _now_iso(),
        }, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Haiku drafting (FAIL-CLOSED) with org-roles advisory routing context
# ---------------------------------------------------------------------------

def _routing_context(entities: list[str]) -> str:
    """Advisory people context from org-roles (D-044: never an access layer)."""
    try:
        from .org_roles import roles_for_entity
        lines: list[str] = []
        for entity in entities[:3]:
            for rec in roles_for_entity(entity)[:3]:
                lines.append(f"- {rec.name} ({rec.role}, {rec.entity})")
        return "\n".join(lines) or "(none mapped)"
    except Exception:  # noqa: BLE001 -- advisory only, never blocks
        return "(unavailable)"


_DRAFT_PROMPT = """\
You review process-friction signals detected across a small business portfolio
and decide whether each is worth proposing to the founder as an efficiency
improvement.

SIGNAL TYPE: {signal_label}
ENTITY/ENTITIES: {entities}
OBSERVED PATTERN: {representative}
FREQUENCY: {frequency}

EVIDENCE EXCERPTS:
{evidence}

PEOPLE CONTEXT (advisory only -- for routing the recommendation, never for
expanding anyone's access):
{routing}

ROUTING DOCTRINE: if the fix is rule-based mechanical automation with no
language generation, recommend a Make.com scenario. If it requires language
understanding or generation, recommend a Cora tool/feature. Repeated questions
are usually best fixed with a known-answer entry or a short doc. Processes or
vendors duplicated across entities should be considered for consolidation at
the holdco (HJR Global).

Respond with ONLY a JSON object (no markdown fences, no prose):
{{"worth_proposing": true/false,
  "title": "imperative summary, max 90 chars",
  "recommendation": "2-4 sentence concrete recommendation grounded in the evidence",
  "route": "known_answer"/"doc"/"cora_tool"/"make_com"/"process_change"/"holdco_consolidation",
  "confidence": "HIGH"/"MED"/"LOW"}}

Rules:
- worth_proposing=false for noise, one-offs, jokes, or anything sensitive
  (client health information, legal disputes, individual compensation).
- Do not invent facts beyond the evidence.
- Never include client names, diagnoses, or other PHI.
"""

_VALID_ROUTES = {"known_answer", "doc", "cora_tool", "make_com",
                 "process_change", "holdco_consolidation"}


def _format_finding_evidence(finding: FrictionFinding) -> str:
    parts = []
    for i, ev in enumerate(finding.evidence[:3], 1):
        label = ev.get("title") or ev.get("source") or "excerpt"
        parts.append(f"--- excerpt {i} [{label}] ---\n{ev.get('excerpt', '')}")
    return "\n\n".join(parts) or "(representative sentence only)"


def draft_proposal(finding: FrictionFinding) -> dict[str, Any] | None:
    """Haiku turns a finding into a recommendation. Fail-CLOSED: None on any
    API/parse error or a not-worth-proposing verdict."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        log.warning("friction_mining: ANTHROPIC_API_KEY not set -- skipping draft")
        return None
    prompt = _DRAFT_PROMPT.format(
        signal_label=_SIGNAL_LABELS.get(finding.signal_type, finding.signal_type),
        entities=", ".join(finding.entities or [finding.entity]),
        representative=finding.representative[:400],
        frequency=finding.frequency,
        evidence=_format_finding_evidence(finding),
        routing=_routing_context(finding.entities or [finding.entity]),
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
        log.warning("friction_mining: Haiku draft failed for %s: %s",
                    finding.fingerprint, exc)
        return None
    if not isinstance(verdict, dict) or not verdict.get("worth_proposing"):
        return None
    title = str(verdict.get("title") or "").strip()[:120]
    recommendation = str(verdict.get("recommendation") or "").strip()
    if not title or not recommendation or is_phi_risk(title + " " + recommendation):
        return None
    route = str(verdict.get("route") or "").strip().lower()
    if route not in _VALID_ROUTES:
        route = "process_change"
    confidence = str(verdict.get("confidence") or "MED").upper()
    if confidence not in ("HIGH", "MED", "LOW"):
        confidence = "MED"
    return {
        "title": title,
        "recommendation": recommendation[:800],
        "route": route,
        "confidence": confidence,
    }


# ---------------------------------------------------------------------------
# Proposal into the Harrison-gated 7am knowledge-review queue (D-011)
# ---------------------------------------------------------------------------

def propose_efficiency(finding: FrictionFinding, draft: dict[str, Any]) -> str:
    """Queue one efficiency proposal for Harrison's thumbs-up. Returns update_id."""
    from .knowledge_review import propose_update

    update_id = f"friction-{uuid.uuid4().hex[:12]}"
    signal_label = _SIGNAL_LABELS.get(finding.signal_type, finding.signal_type)
    description = (
        f"Efficiency finding ({finding.entity}) -- {signal_label}\n"
        f"{draft['title']}\n"
        f"Recommendation: {draft['recommendation'][:300]}\n"
        f"Frequency: {finding.frequency} | Route: {draft['route']}"
    )
    first_excerpt = finding.evidence[0]["excerpt"] if finding.evidence else finding.representative
    propose_update(
        update_id=update_id,
        update_type=UPDATE_TYPE_EFFICIENCY,
        description=description,
        payload={
            "signal_type": finding.signal_type,
            "entity": finding.entity,
            "entities": finding.entities,
            "title": draft["title"],
            "recommendation": draft["recommendation"],
            "route": draft["route"],
            "frequency": finding.frequency,
            "representative": finding.representative[:300],
            "evidence": finding.evidence[:3],
            "fingerprint": finding.fingerprint,
        },
        source_evidence=first_excerpt[:500],
        confidence=draft["confidence"],
    )
    return update_id


# ---------------------------------------------------------------------------
# Executor -- apply a Harrison-approved efficiency proposal (called by
# run_knowledge_review.py after a thumbs-up; D-011 gate already passed)
# ---------------------------------------------------------------------------

def apply_efficiency(payload: dict[str, Any]) -> tuple[bool, str]:
    """Append an approved finding to design/efficiency-backlog.md (append-only).

    Returns (ok, summary). Never raises.
    """
    try:
        title = (payload.get("title") or "").strip() or "(untitled finding)"
        path = _backlog_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(
                "# Efficiency Backlog\n\n"
                "_Harrison-approved findings from the weekly friction-mining pass "
                "(Org Synthesis Phase 3). Append-only; newest last._\n",
                encoding="utf-8",
            )
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        evidence_bits = "; ".join(
            (ev.get("excerpt") or "")[:120] for ev in (payload.get("evidence") or [])[:2]
        )
        entry = (
            f"\n## [{today}] {title}\n\n"
            f"- Signal: {payload.get('signal_type', '?')} | "
            f"Entity: {payload.get('entity', '?')} | {payload.get('frequency', '')}\n"
            f"- Route: {payload.get('route', '?')}\n"
            f"- Recommendation: {payload.get('recommendation', '')}\n"
            f"- Evidence: {evidence_bits}\n"
        )
        with path.open("a", encoding="utf-8") as fh:
            fh.write(entry)
        log.info("friction_mining: backlog entry appended -- %s", title)
        return True, f"appended to {path.name}"
    except Exception as exc:  # noqa: BLE001 -- executor must not crash the run
        log.error("friction_mining: apply_efficiency failed: %s", exc, exc_info=True)
        return False, f"apply failed: {exc}"


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_mining(
    *,
    lookback_days: int = LOOKBACK_DAYS,
    max_proposals: int = MAX_PROPOSALS_PER_RUN,
    signals: set[str] | None = None,
    dry_run: bool = False,
    db_path: Path | None = None,
    embed_fn: Callable | None = None,
    draft_fn: Callable | None = None,
    propose_fn: Callable | None = None,
) -> dict[str, Any]:
    """One full mining run. Returns a summary dict.

    dry_run: detect + dedup + draft, but write NOTHING (no proposals, no
    ledger rows) -- the rollout-gate review mode.
    """
    draft_fn = draft_fn or draft_proposal
    propose_fn = propose_fn or propose_efficiency
    signals = signals or {SIGNAL_REPEATED_QUESTION, SIGNAL_MANUAL_STEPS,
                          SIGNAL_STALE_HANDOFF, SIGNAL_CROSS_ENTITY_DUP}

    chunks = query_chunks(lookback_days=lookback_days, db_path=db_path)
    log.info("friction_mining: %d corpus chunks in window (%dd)", len(chunks), lookback_days)

    findings: list[FrictionFinding] = []
    if SIGNAL_REPEATED_QUESTION in signals:
        findings.extend(detect_repeated_questions(
            chunks,
            load_gap_questions(lookback_days=lookback_days),
            load_cache_questions(lookback_days=lookback_days, db_path=db_path),
            embed_fn=embed_fn,
        ))
    if SIGNAL_MANUAL_STEPS in signals:
        findings.extend(detect_manual_steps(chunks, embed_fn=embed_fn))
    if SIGNAL_STALE_HANDOFF in signals:
        findings.extend(detect_stale_handoffs(chunks, embed_fn=embed_fn))
    if SIGNAL_CROSS_ENTITY_DUP in signals:
        findings.extend(detect_cross_entity_duplication(chunks, embed_fn=embed_fn))
    log.info("friction_mining: %d raw findings", len(findings))

    # Dedup: against the ledger AND within this run.
    ledger = load_ledger()
    seen_fps: set[str] = set()
    fresh: list[FrictionFinding] = []
    for f in findings:
        if f.fingerprint in seen_fps or is_already_proposed(f, ledger):
            continue
        seen_fps.add(f.fingerprint)
        fresh.append(f)
    log.info("friction_mining: %d findings after dedup", len(fresh))

    # Cost cap on Haiku: strongest (most-observed) candidates first.
    fresh.sort(key=lambda f: -f.count)
    candidates = fresh[:MAX_HAIKU_CANDIDATES]

    drafted: list[tuple[FrictionFinding, dict[str, Any]]] = []
    for f in candidates:
        draft = draft_fn(f)
        if draft:
            drafted.append((f, draft))
    log.info("friction_mining: %d drafts survived Haiku", len(drafted))

    # Highest confidence first, then most-observed; hard cap.
    drafted.sort(key=lambda fd: (_CONF_RANK.get(fd[1]["confidence"], 3), -fd[0].count))
    to_propose = drafted[:max_proposals]

    proposed: list[dict[str, Any]] = []
    for f, draft in to_propose:
        item = {
            "signal_type": f.signal_type,
            "entity": f.entity,
            "entities": f.entities,
            "title": draft["title"],
            "recommendation": draft["recommendation"],
            "route": draft["route"],
            "confidence": draft["confidence"],
            "frequency": f.frequency,
            "representative": f.representative[:200],
            "evidence": f.evidence,
        }
        if not dry_run:
            update_id = propose_fn(f, draft)
            record_proposal(f, update_id)
            item["update_id"] = update_id
        proposed.append(item)

    return {
        "dry_run": dry_run,
        "chunks": len(chunks),
        "raw_findings": len(findings),
        "after_dedup": len(fresh),
        "drafted": len(drafted),
        "proposed": proposed,
    }
