"""Cross-source reconciliation engine — Component 3 of Ambient Awareness.

Scans the last 24h of KB content across all sources, detects four classes of gaps,
and returns ReconciliationGap objects that run_reconciliation.py queues for
Harrison's 👍/👎 via knowledge_review.propose_update().

Four detection passes:
  Pass 1 (missing Asana tasks):
      Action commitments / next-steps in Gmail/Slack KB chunks that have no
      matching open Asana task. Uses Claude Haiku to extract commitments, then
      fuzzy-matches against open tasks.

  Pass 2 (stale HubSpot deals):
      Deal / lead mentions in Slack/Fireflies KB chunks that name a company or
      deal by keyword, cross-referenced against HubSpot deals with no activity
      in the past 7 days.

  Pass 3 (uncaptured decisions):
      Decision-language patterns in Fireflies/Slack chunks
      ('we decided', 'going with', 'locked', 'confirmed') checked against
      KB chunks from the decisions.md static_md source. New ones get flagged.

  Pass 4 (stale open tasks):
      Completion language in Slack/Gmail chunks (leverages completion_detector.py
      vocabulary) cross-checked against open Asana tasks — same as the daily
      completion sweep but scoped to the 24h window and routed to the 👍/👎 flow
      rather than the #hjrg-leadership digest.

PHI guardrail: LEX chunks that mention client names, diagnoses, or care plan
language are excluded from ALL passes. Checked via _is_phi_content() before any
cross-entity reconciliation or gap-proposal.

Visibility CPA exclusion: Hayden Greber / Andrew Stubbs / Sarah Bertoglio /
Emily Stubbs never in proposed-update descriptions or payloads.

Harrison sole-authority doctrine (LOCKED 2026-05-21):
  This module NEVER writes to decisions.md, Asana, or HubSpot. It only
  proposes gaps — run_reconciliation.py queues them for Harrison's 👍/👎.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from cora.phi_guard import _PHI_PATTERNS as _PHI_RE

log = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

# Lookback window for KB chunk queries — 25h to account for late-night syncs.
DEFAULT_LOOKBACK_SECONDS = 25 * 3600

# Hard cap on chunks per KB query (2026-06-12). The window is keyed on CONTENT
# date, but a mis-dated source or any future surprise must never reproduce the
# 6/12 incident: the 18-month gmail backfill (ingested 6/11) made the
# ingestion-dated window scan 111,878 chunks for ~6 hours, starving the 7:00
# knowledge review and getting the 7:30 briefing task killed at its time limit.
# Normal nightly volume is low thousands; the cap only bites on floods.
MAX_CHUNKS_PER_QUERY = 4000

# Pass-4 budget knobs (2026-06-12): each completion-flagged sentence costs an
# OpenAI embed round-trip, so sentence volume — not chunk count — is the real
# wall-clock driver. After the embed budget is spent, matching falls back to
# fuzzy (cheap, lower recall) instead of stopping.
PASS4_MAX_SENTENCE_EMBEDS = 2000

# Extended lookback for Fireflies in Pass 4.  Fireflies syncs at 3:30am and
# meetings happen irregularly — 25h often misses yesterday's transcripts.
# 48h ensures 2 full days of meeting content are scanned for completion signals.
FIREFLIES_LOOKBACK_SECONDS = 48 * 3600

# HubSpot stale-deal threshold (seconds)
HUBSPOT_STALE_DAYS = 7
HUBSPOT_STALE_SECONDS = HUBSPOT_STALE_DAYS * 86400

# Confidence thresholds — only HIGH gaps are queued for Harrison review.
# MED was generating too many low-signal proposals that piled up unreviewed.
# Tuned 2026-06-03: raised from MED to HIGH to keep daily DM batch to 3-7 items.
CONFIDENCE_THRESHOLD = "HIGH"  # "HIGH" | "MED" — LOW always discarded

# Min fuzzy ratio for task/deal name matching
MIN_FUZZY_RATIO = 0.35

# Max gaps surfaced per pass. Reduced from 30 to 8 — keeps daily review batch
# small enough that Harrison can action everything within the 7am DM window.
MAX_GAPS_PER_PASS = 8

# Action/commitment language patterns for Pass 1.
_ACTION_RE = re.compile(
    r"\b(will\s+(?:send|follow|schedule|reach|create|set\s+up|handle|review|check"
    r"|update|call|email|make|get|look|coordinate|submit|prepare|arrange|book|draft)"
    r"|(?:action\s+item|next\s+step|follow[\s-]up|todo|to\s+do|i'll|i\s+will|we\s+will"
    r"|need\s+to|needs\s+to|should\s+(?:be\s+)?(?:done|completed|sent|updated)"
    r"|assigned\s+to|owner:|@\w+\s+(?:to|will)|going\s+to)\b)",
    re.IGNORECASE,
)

# Decision language patterns for Pass 3.
_DECISION_RE = re.compile(
    r"\b(we\s+decided|we\s+agreed|going\s+with|locked\s+(?:in|down)?|confirmed"
    r"|officially|decision\s+is|the\s+plan\s+is|we\s+(?:are|will)\s+(?:going|moving)"
    r"|final(?:ly|ized)?|concluded|selected|chosen|approved|signed\s+off)\b",
    re.IGNORECASE,
)

# Visibility CPA exclusion -- centralized in phi_guard
from cora.phi_guard import is_visibility_cpa_mention as _mentions_vis_cpa_fn


# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class ReconciliationGap:
    """One detected gap between what sources say and what's tracked."""

    gap_id: str                   # unique ID: "{pass_type}:{source_id}:{hash8}"
    gap_type: str                 # "missing_asana_task" | "stale_hubspot_deal" |
                                  # "uncaptured_decision" | "stale_open_task"
    description: str              # human-readable summary for Harrison's DM
    source_evidence: str          # excerpt from KB chunk that triggered detection
    source: str                   # "slack" | "gmail" | "fireflies" | etc.
    source_id: str                # KB chunk source_id
    entity: str                   # entity code
    confidence: str               # "HIGH" | "MED" | "LOW"
    proposed_action: str          # what Harrison would approve if he 👍s
    payload: dict[str, Any] = field(default_factory=dict)  # structured execution data
    deep_link: str = ""           # back-link to source chunk
    title: str = ""               # chunk title

    @property
    def is_actionable(self) -> bool:
        return self.confidence in ("HIGH", "MED")


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _kb_db_path() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "cora_kb.db"


def _query_kb_chunks(
    *,
    sources: list[str] | None = None,
    entities: list[str] | None = None,
    lookback_seconds: float = DEFAULT_LOOKBACK_SECONDS,
    db_path: Path | None = None,
    exclude_phi_lex: bool = True,
    max_chunks: int = MAX_CHUNKS_PER_QUERY,
) -> list[dict[str, Any]]:
    """Fetch KB chunks whose CONTENT date falls within the lookback window.

    Windowing is keyed on COALESCE(date_modified, date_created, ingested_at) —
    the message/content date with an ingestion-date fallback for undated rows.
    Keying on ingested_at alone made every historical backfill look like "the
    last 25 hours" (the 2026-06-12 incident: a 6h pass-4 run over the 18-month
    gmail backfill). max_chunks is a hard backstop on top of that — newest
    content first, excess dropped with a warning.

    Returns list of dicts with keys:
      source, source_id, entity, sub_entity, content, deep_link, title, ingested_at
    """
    db_path = db_path or _kb_db_path()
    if not db_path.exists():
        log.warning("reconciliation: KB DB not found at %s", db_path)
        return []

    cutoff_ts = int(time.time() - lookback_seconds)
    content_date = "COALESCE(date_modified, date_created, ingested_at)"
    conn = sqlite3.connect(str(db_path))
    try:
        params: list[Any] = [cutoff_ts]
        clauses: list[str] = [f"{content_date} >= ?"]

        if sources:
            placeholders = ",".join("?" * len(sources))
            clauses.append(f"source IN ({placeholders})")
            params.extend(sources)

        if entities:
            placeholders = ",".join("?" * len(entities))
            clauses.append(f"entity IN ({placeholders})")
            params.extend(entities)

        where = " AND ".join(clauses)
        params.append(int(max_chunks))
        rows = conn.execute(
            f"""
            SELECT source, source_id, entity, sub_entity, content,
                   deep_link, title, ingested_at
            FROM knowledge_chunks
            WHERE {where}
            ORDER BY {content_date} DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    finally:
        conn.close()

    if len(rows) >= max_chunks:
        log.warning(
            "reconciliation: chunk query hit the %d-chunk cap (sources=%s) -- "
            "oldest in-window content dropped; if this repeats outside a "
            "backfill day, raise MAX_CHUNKS_PER_QUERY",
            max_chunks, sources,
        )

    chunks = []
    for row in rows:
        source, source_id, entity, sub_entity, content, deep_link, title, ingested_at = row
        # PHI guardrail: skip LEX chunks with PHI language entirely
        if exclude_phi_lex and (entity or "").startswith("LEX"):
            if _is_phi_content(content or ""):
                continue
        # Visibility CPA exclusion: skip chunks that name CPA team
        if _mentions_vis_cpa(content or ""):
            continue
        chunks.append({
            "source": source or "",
            "source_id": source_id or "",
            "entity": entity or "FNDR",
            "sub_entity": sub_entity or "",
            "content": content or "",
            "deep_link": deep_link or "",
            "title": title or "",
            "ingested_at": ingested_at or 0,
        })

    log.debug(
        "reconciliation: _query_kb_chunks sources=%s entities=%s -> %d chunks",
        sources, entities, len(chunks),
    )
    return chunks


# ── PHI + CPA guards ───────────────────────────────────────────────────────────

def _is_phi_content(text: str) -> bool:
    """True if text appears to contain LEX client PHI patterns."""
    return bool(_PHI_RE.search(text))


def _mentions_vis_cpa(text: str) -> bool:
    """True if text mentions Visibility CPA team members (by name)."""
    return _mentions_vis_cpa_fn(text)


# ── Utility ────────────────────────────────────────────────────────────────────

def _fuzzy_ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _gap_id(gap_type: str, source_id: str, description: str) -> str:
    """Generate a stable gap ID from its key fields."""
    import hashlib
    h = hashlib.md5((gap_type + source_id + description).encode()).hexdigest()[:8]
    safe_sid = re.sub(r"[^a-zA-Z0-9_-]", "_", source_id)[:40]
    return f"{gap_type}:{safe_sid}:{h}"


def _extract_sentences(text: str) -> list[str]:
    """Split text into sentences for fine-grained pattern scanning."""
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def _confidence_from_ratio(fuzzy_ratio: float, source: str) -> str:
    """Map fuzzy ratio + source type to HIGH/MED/LOW."""
    source_weight = {
        "fireflies": 0.90, "slack": 0.75, "gmail": 0.70,
        "hubspot": 0.65, "asana": 0.60, "static_md": 0.40,
    }.get(source, 0.55)
    score = source_weight * 0.55 + fuzzy_ratio * 0.45
    if score >= 0.75:
        return "HIGH"
    if score >= 0.55:
        return "MED"
    return "LOW"


# ── Fix 1+2+3 helpers: semantic task matching for Pass 4 ──────────────────────

# Entity/assignee prefix pattern — strips "[F3E] Tommy — " style prefixes from
# Asana task names before matching so the prefix doesn't dilute similarity scores.
# Build dash character class without literal Unicode in source (avoids CIFS encoding issues).
# Covers: em dash (U+2014), en dash (U+2013), regular hyphen (U+002D), double hyphen (--)
_DASH_CHARS = chr(0x2014) + chr(0x2013) + "-"
_TASK_PREFIX_RE = re.compile(
    r"^\s*"
    r"(?:\[[A-Z0-9_-]+\]\s*)?"             # optional [ENTITY] bracket, e.g. [F3E]
    r"(?:[A-Za-z]+(?:\s+[A-Za-z]+)?"       # optional "Name" (1-2 words)
    + r"\s*(?:--|[" + _DASH_CHARS + r"])\s*)?"  # followed by -- or any dash
    + r"\s*",
)

# Minimum cosine similarity to consider a match (0–1 scale).
# 0.72 ≈ "same topic / related concept"; avoids superficial keyword matches.
_MIN_COSINE_SIM = 0.72


def _normalize_task_name(name: str) -> str:
    """Strip entity/assignee prefixes so semantic matching focuses on task content.

    Examples:
      "[F3E] Tommy — ADF sampling kit delivery"  → "ADF sampling kit delivery"
      "[OSN] Matt — Reconciliation Pilot Phase 1" → "Reconciliation Pilot Phase 1"
      "Follow up with Harrison"                   → "Follow up with Harrison"
    """
    return _TASK_PREFIX_RE.sub("", name).strip()


def _cosine_sim(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two embedding vectors.

    text-embedding-3-small outputs unit vectors, so this equals the dot product.
    Computed explicitly to avoid numpy dependency.
    """
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if not mag_a or not mag_b:
        return 0.0
    return dot / (mag_a * mag_b)


def _embed_task_names(task_entries: list[dict]) -> list[list[float]]:
    """Batch-embed normalised task names. Returns [] on any embedding failure.

    Uses text-embedding-3-small via the existing cora.knowledge_base.embeddings
    module.  Cost: ~10 tokens/task × 200 tasks = 2000 tokens ≈ $0.00004/run.
    Falls back to empty list if OPENAI_API_KEY is missing or API call fails.
    """
    try:
        from cora.knowledge_base.embeddings import embed_texts, EmbeddingError
    except ImportError:
        return []

    names = [_normalize_task_name(t.get("name", "")) for t in task_entries]
    if not names:
        return []
    try:
        return embed_texts(names)
    except EmbeddingError as exc:
        log.warning("pass4 semantic: embed_task_names failed (%s) — falling back to fuzzy", exc)
        return []


def _embed_sentence(sentence: str) -> list[float] | None:
    """Embed a single sentence. Returns None on failure."""
    try:
        from cora.knowledge_base.embeddings import embed_query, EmbeddingError
        return embed_query(sentence)
    except Exception as exc:
        log.debug("pass4 semantic: embed_sentence failed: %s", exc)
        return None


def _semantic_best_match(
    sentence: str,
    sentence_emb: list[float],
    task_entries: list[dict],
    task_embs: list[list[float]],
) -> tuple[float, dict]:
    """Return (cosine_sim, best_task_entry) for the highest-similarity task.

    Returns (0.0, {}) if no match exceeds _MIN_COSINE_SIM.
    """
    best_sim = _MIN_COSINE_SIM
    best_task: dict = {}
    for task_entry, task_emb in zip(task_entries, task_embs):
        sim = _cosine_sim(sentence_emb, task_emb)
        if sim > best_sim:
            best_sim = sim
            best_task = task_entry
    return best_sim, best_task


def _confidence_from_sim(cosine_sim: float, source: str) -> str:
    """Map cosine similarity + source to HIGH/MED/LOW confidence.

    Semantic similarity (0.72+) is weighted 60%; source credibility 40%.
    Thresholds calibrated so a strong semantic match from Fireflies = HIGH,
    a weak match from static_md = LOW.
    """
    source_weight = {
        "fireflies": 0.90, "slack": 0.75, "gmail": 0.70,
        "hubspot": 0.65, "asana": 0.60, "static_md": 0.40,
    }.get(source, 0.55)
    score = source_weight * 0.40 + cosine_sim * 0.60
    if score >= 0.78:
        return "HIGH"
    if score >= 0.62:
        return "MED"
    return "LOW"


# ── Pass 1: Missing Asana tasks ────────────────────────────────────────────────

def _extract_action_sentences(chunk: dict[str, Any]) -> list[str]:
    """Return sentences from a chunk that contain action/commitment language."""
    hits = []
    for sentence in _extract_sentences(chunk["content"]):
        if len(sentence) < 15:
            continue
        if _ACTION_RE.search(sentence):
            hits.append(sentence)
    return hits


def pass1_missing_asana_tasks(
    open_tasks: list[dict[str, Any]],
    *,
    lookback_seconds: float = DEFAULT_LOOKBACK_SECONDS,
    db_path: Path | None = None,
) -> list[ReconciliationGap]:
    """Scan Slack/Gmail chunks for action commitments not matched to open tasks.

    Strategy:
    1. Query KB for Slack + Gmail chunks from last 24h.
    2. Extract sentences containing action/commitment language.
    3. Fuzzy-match each sentence against open Asana task names.
    4. Flag sentences where NO open task matches above MIN_FUZZY_RATIO
       (the commitment exists but has no Asana task to track it).
    """
    chunks = _query_kb_chunks(
        sources=["slack", "gmail"],
        lookback_seconds=lookback_seconds,
        db_path=db_path,
    )

    open_task_names = [
        {"gid": t.get("gid", ""), "name": t.get("name", ""), "task": t}
        for t in open_tasks
        if t.get("name")
    ]

    gaps: list[ReconciliationGap] = []

    for chunk in chunks:
        action_sentences = _extract_action_sentences(chunk)
        for sentence in action_sentences[:5]:  # cap per chunk to avoid explosion
            # Check if any open task already covers this commitment
            best_ratio = 0.0
            for task_entry in open_task_names:
                ratio = _fuzzy_ratio(sentence, task_entry["name"])
                if ratio > best_ratio:
                    best_ratio = ratio

            # Only flag if no task matches the commitment
            if best_ratio >= MIN_FUZZY_RATIO:
                continue  # already tracked

            confidence = _confidence_from_ratio(0.0, chunk["source"])
            # Without a task match, base confidence purely on source weight
            src_weight = {
                "fireflies": 0.90, "slack": 0.75, "gmail": 0.70,
            }.get(chunk["source"], 0.55)
            if src_weight >= 0.75:
                confidence = "MED"
            else:
                confidence = "LOW"

            if confidence == "LOW":
                continue

            description = (
                f"Action commitment in {chunk['source']} ({chunk['entity']}) "
                f"with no matching Asana task: \"{sentence[:120]}\""
            )
            gap = ReconciliationGap(
                gap_id=_gap_id("missing_asana_task", chunk["source_id"], sentence[:80]),
                gap_type="missing_asana_task",
                description=description,
                source_evidence=sentence,
                source=chunk["source"],
                source_id=chunk["source_id"],
                entity=chunk["entity"],
                confidence=confidence,
                proposed_action=(
                    f"Create Asana task: \"{sentence[:150]}\" "
                    f"(entity: {chunk['entity']})"
                ),
                payload={
                    "suggested_task_name": sentence[:150],
                    "entity": chunk["entity"],
                    "source": chunk["source"],
                    "source_id": chunk["source_id"],
                    "chunk_title": chunk["title"],
                },
                deep_link=chunk["deep_link"],
                title=chunk["title"],
            )
            gaps.append(gap)
            if len(gaps) >= MAX_GAPS_PER_PASS:
                break
        if len(gaps) >= MAX_GAPS_PER_PASS:
            break

    log.info("reconciliation pass1: %d missing-task gaps found", len(gaps))
    return gaps


# ── Pass 2: Stale HubSpot deals ────────────────────────────────────────────────

# Company/deal keywords for F3E and UFL pipelines
_F3E_DEAL_RE = re.compile(
    r"\b(american\s+discount|sprouts|whole\s+foods|sj\s+food|sunset\s+distributing"
    r"|reliant|ufc\s+gym|red\s+hawk|mma\s+lab|berry\s+divine|cruse|big\s+savings"
    r"|ike\s+|deal|retailer|distributor|buyer|account)\b",
    re.IGNORECASE,
)
_UFL_DEAL_RE = re.compile(
    r"\b(sponsor|sponsorship|unbeaten|mas\s+comm|caa|visit\s+mesa|d[\s-]?backs"
    r"|suns|partnership|naming\s+rights)\b",
    re.IGNORECASE,
)


def pass2_stale_hubspot_deals(
    active_deals: list[dict[str, Any]],
    *,
    lookback_seconds: float = DEFAULT_LOOKBACK_SECONDS,
    db_path: Path | None = None,
) -> list[ReconciliationGap]:
    """Find deals mentioned in Slack/Fireflies chunks where HubSpot has no recent activity.

    Strategy:
    1. Query KB for Slack + Fireflies chunks from last 24h.
    2. Find chunks that mention deal-related keywords.
    3. Fuzzy-match mention against active deal names from HubSpot.
    4. Flag deals where last_activity is older than HUBSPOT_STALE_DAYS
       but recent Slack/Fireflies content mentions the deal.
    """
    chunks = _query_kb_chunks(
        sources=["slack", "fireflies"],
        lookback_seconds=lookback_seconds,
        db_path=db_path,
    )

    # Build {deal_name_lower: deal_dict} for fuzzy matching
    deal_index = {d.get("name", "").lower(): d for d in active_deals if d.get("name")}

    gaps: list[ReconciliationGap] = []
    seen_deals: set[str] = set()  # avoid duplicate gaps for same deal

    stale_cutoff = time.time() - HUBSPOT_STALE_SECONDS

    for chunk in chunks:
        # Quick keyword check before fuzzy matching
        if not (_F3E_DEAL_RE.search(chunk["content"])
                or _UFL_DEAL_RE.search(chunk["content"])):
            continue

        for sentence in _extract_sentences(chunk["content"]):
            if not (_F3E_DEAL_RE.search(sentence) or _UFL_DEAL_RE.search(sentence)):
                continue

            # Find best-matching deal
            best_deal_name = ""
            best_ratio = MIN_FUZZY_RATIO
            best_deal: dict[str, Any] = {}
            for deal_name_lower, deal in deal_index.items():
                ratio = _fuzzy_ratio(sentence, deal_name_lower)
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_deal_name = deal_name_lower
                    best_deal = deal

            if not best_deal:
                continue

            deal_id = str(best_deal.get("id") or best_deal.get("gid") or "")
            if deal_id in seen_deals:
                continue

            # Check HubSpot last activity
            last_activity = best_deal.get("last_activity_ts", 0)
            if last_activity and float(last_activity) > stale_cutoff:
                continue  # recently updated — not stale

            seen_deals.add(deal_id)
            confidence = _confidence_from_ratio(best_ratio, chunk["source"])
            if confidence == "LOW":
                continue

            deal_display = best_deal.get("name", best_deal_name)
            description = (
                f"Deal \"{deal_display}\" mentioned in {chunk['source']} "
                f"({chunk['entity']}) but no HubSpot activity in {HUBSPOT_STALE_DAYS}d"
            )
            deal_url = best_deal.get("deep_link", "")

            gaps.append(ReconciliationGap(
                gap_id=_gap_id("stale_hubspot_deal", chunk["source_id"], deal_display),
                gap_type="stale_hubspot_deal",
                description=description,
                source_evidence=sentence[:400],
                source=chunk["source"],
                source_id=chunk["source_id"],
                entity=chunk["entity"],
                confidence=confidence,
                proposed_action=(
                    f"Add HubSpot note to deal \"{deal_display}\": "
                    f"mentioned in {chunk['source']} — needs pipeline update"
                ),
                payload={
                    "deal_name": deal_display,
                    "deal_id": deal_id,
                    "deal_url": deal_url,
                    "days_stale": HUBSPOT_STALE_DAYS,
                    "source": chunk["source"],
                    "source_id": chunk["source_id"],
                    "chunk_title": chunk["title"],
                },
                deep_link=chunk["deep_link"],
                title=chunk["title"],
            ))

            if len(gaps) >= MAX_GAPS_PER_PASS:
                break
        if len(gaps) >= MAX_GAPS_PER_PASS:
            break

    log.info("reconciliation pass2: %d stale-HubSpot gaps found", len(gaps))
    return gaps


# ── Pass 3: Uncaptured decisions ───────────────────────────────────────────────

def pass3_uncaptured_decisions(
    *,
    lookback_seconds: float = DEFAULT_LOOKBACK_SECONDS,
    db_path: Path | None = None,
) -> list[ReconciliationGap]:
    """Find decision-language in Fireflies/Slack that doesn't appear in decisions.md KB chunks.

    Strategy:
    1. Query KB for Fireflies + Slack chunks from last 24h.
    2. Extract sentences with decision language.
    3. For each decision sentence, check if a similar phrase already exists in
       static_md KB chunks from decisions.md (fuzzy match).
    4. Flag sentences that appear to be new decisions not yet captured.
    """
    # Query recent Fireflies + Slack chunks (decision sources)
    live_chunks = _query_kb_chunks(
        sources=["fireflies", "slack"],
        lookback_seconds=lookback_seconds,
        db_path=db_path,
    )

    # Query decisions.md content from static_md (expand lookback to get full file)
    decisions_chunks = _query_kb_chunks(
        sources=["static_md"],
        lookback_seconds=365 * 86400,  # full year for decisions.md
        db_path=db_path,
    )
    # Only keep chunks that look like they're from decisions.md
    decisions_texts = [
        c["content"] for c in decisions_chunks
        if "decisions" in (c["title"] or "").lower()
        or "decided" in (c["content"] or "").lower()
    ]
    decisions_full_text = " ".join(decisions_texts).lower()

    gaps: list[ReconciliationGap] = []

    for chunk in live_chunks:
        for sentence in _extract_sentences(chunk["content"]):
            if len(sentence) < 20:
                continue
            if not _DECISION_RE.search(sentence):
                continue

            # Check if this decision is already in decisions.md
            # Use a simple substring check on the key noun phrase
            # Extract nouns: words > 4 chars, not common filler
            words = re.findall(r"\b[a-zA-Z]{4,}\b", sentence.lower())
            _filler = {
                "that", "this", "with", "will", "have", "been", "from",
                "they", "them", "their", "were", "which", "when", "what",
                "decided", "going", "confirmed", "locked", "agreed",
            }
            key_words = [w for w in words if w not in _filler][:5]

            if not key_words:
                continue

            # If 3+ key words from sentence appear together in decisions.md, skip
            matches_in_decisions = sum(1 for w in key_words if w in decisions_full_text)
            if matches_in_decisions >= 3:
                continue  # likely already captured

            confidence = _confidence_from_ratio(0.5, chunk["source"])
            if confidence == "LOW":
                continue

            description = (
                f"Possible uncaptured decision in {chunk['source']} "
                f"({chunk['entity']}): \"{sentence[:150]}\""
            )
            gaps.append(ReconciliationGap(
                gap_id=_gap_id("uncaptured_decision", chunk["source_id"], sentence[:80]),
                gap_type="uncaptured_decision",
                description=description,
                source_evidence=sentence[:400],
                source=chunk["source"],
                source_id=chunk["source_id"],
                entity=chunk["entity"],
                confidence=confidence,
                proposed_action=(
                    f"Append to decisions.md: \"{sentence[:200]}\" "
                    f"(entity: {chunk['entity']}, source: {chunk['source']}, "
                    f"date: {chunk['title']})"
                ),
                payload={
                    "decision_text": sentence[:500],
                    "entity": chunk["entity"],
                    "source": chunk["source"],
                    "source_id": chunk["source_id"],
                    "chunk_title": chunk["title"],
                },
                deep_link=chunk["deep_link"],
                title=chunk["title"],
            ))

            if len(gaps) >= MAX_GAPS_PER_PASS:
                break
        if len(gaps) >= MAX_GAPS_PER_PASS:
            break

    log.info("reconciliation pass3: %d uncaptured-decision gaps found", len(gaps))
    return gaps


# ── Pass 4: Stale open tasks ───────────────────────────────────────────────────

def pass4_stale_open_tasks(
    open_tasks: list[dict[str, Any]],
    *,
    lookback_seconds: float = DEFAULT_LOOKBACK_SECONDS,
    db_path: Path | None = None,
    deadline_monotonic: float | None = None,
) -> list[ReconciliationGap]:
    """Find completion language in Slack/Gmail/Fireflies KB chunks matched against open tasks.

    Three improvements over the original implementation:
      1. Fireflies added as a source (meeting transcripts are the highest-signal source
         for completion language — "we closed X", "Alex confirmed delivery").
      2. Semantic matching replaces fuzzy string matching (SequenceMatcher).  Task names
         and completion sentences are embedded with text-embedding-3-small; cosine
         similarity >= 0.72 triggers a match.  Falls back to fuzzy if OPENAI_API_KEY
         is absent.
      3. Task name prefixes ([F3E], "Tommy — ") are stripped before matching so the
         entity/assignee decorators don't dilute similarity scores.

    Delivery path: 👍/👎 DM to Harrison (not #hjrg-leadership digest).
    """
    # -- Completion vocabulary ------------------------------------------------
    try:
        from cora.tools.completion_detector import _COMPLETION_RE, _is_deduped
        _have_completion_detector = True
    except ImportError:
        log.warning("reconciliation pass4: completion_detector not importable, using fallback")
        _COMPLETION_RE = re.compile(
            r"\b(complet|finish|done|shipped|launched|signed|closed|resolved"
            r"|paid|confirmed|approved|submitted|delivered)\b",
            re.IGNORECASE,
        )
        _have_completion_detector = False

    # -- Fix 1: include Fireflies, Bug 3: use extended lookback for Fireflies --
    # Slack/Gmail use the standard 25h window.  Fireflies syncs at 3:30am and
    # meetings happen irregularly — using 48h ensures yesterday's transcripts
    # are always included even when the reconciliation run is same-day.
    chunks_short = _query_kb_chunks(
        sources=["slack", "gmail"],
        lookback_seconds=lookback_seconds,
        db_path=db_path,
    )
    chunks_ff = _query_kb_chunks(
        sources=["fireflies"],
        lookback_seconds=FIREFLIES_LOOKBACK_SECONDS,
        db_path=db_path,
    )
    # Merge, dedup by source_id (Fireflies wins over shorter-window duplicates)
    _seen_chunk_ids: set[str] = set()
    chunks: list[dict] = []
    for c in chunks_short + chunks_ff:
        sid = c["source_id"]
        if sid not in _seen_chunk_ids:
            _seen_chunk_ids.add(sid)
            chunks.append(c)
    log.info(
        "pass4 sources: %d slack/gmail chunks + %d fireflies chunks = %d total",
        len(chunks_short), len(chunks_ff), len(chunks),
    )

    open_task_names = [
        {
            "gid": t.get("gid", ""),
            "name": t.get("name", ""),
            "permalink_url": t.get("permalink_url", ""),
            "assignee": t.get("assignee") or {},
        }
        for t in open_tasks
        if t.get("name")
    ]

    if not open_task_names:
        log.info("reconciliation pass4: no open tasks to match against")
        return []

    # -- Fix 2+3: embed normalised task names once upfront ---------------------
    task_embs = _embed_task_names(open_task_names)
    use_semantic = bool(task_embs and len(task_embs) == len(open_task_names))
    if use_semantic:
        log.info(
            "reconciliation pass4: semantic matching active — %d task embeddings ready",
            len(task_embs),
        )
    else:
        log.info("reconciliation pass4: falling back to fuzzy matching (semantic unavailable)")

    # Bug 5 fix: best-score-wins per task instead of first-match-wins.
    # Tracks the best match found for each task GID so a stronger signal from
    # Fireflies can supersede a weaker earlier match from Slack.
    best_per_task: dict[str, tuple[float, bool, dict, dict]] = {}
    # task_gid -> (score, used_semantic, chunk, best_task_entry)

    # Wall-clock + embed budgets (2026-06-12): sentence embeds are an OpenAI
    # round-trip each, so a chunk flood turns this loop into hours. Past the
    # embed budget we fall back to fuzzy matching; past the deadline we stop
    # scanning and keep what we found.
    embeds_used = 0
    deadline_hit = False

    for chunk_idx, chunk in enumerate(chunks):
        if deadline_monotonic is not None and time.monotonic() > deadline_monotonic:
            deadline_hit = True
            log.warning(
                "reconciliation pass4: wall-clock deadline hit at chunk %d/%d -- "
                "keeping %d matches found so far",
                chunk_idx, len(chunks), len(best_per_task),
            )
            break
        for sentence in _extract_sentences(chunk["content"]):
            if len(sentence) < 15:
                continue
            if not _COMPLETION_RE.search(sentence):
                continue

            best_task: dict[str, Any] = {}
            match_score: float = 0.0
            used_semantic = False

            if use_semantic and embeds_used < PASS4_MAX_SENTENCE_EMBEDS:
                embeds_used += 1
                if embeds_used == PASS4_MAX_SENTENCE_EMBEDS:
                    log.warning(
                        "reconciliation pass4: sentence-embed budget (%d) spent -- "
                        "remaining sentences match via fuzzy fallback",
                        PASS4_MAX_SENTENCE_EMBEDS,
                    )
                sent_emb = _embed_sentence(sentence)
                if sent_emb:
                    best_sim, best_task = _semantic_best_match(
                        sentence, sent_emb, open_task_names, task_embs
                    )
                    if best_task:
                        match_score = best_sim
                        used_semantic = True

            if not best_task:
                best_ratio = MIN_FUZZY_RATIO
                for task_entry in open_task_names:
                    clean_name = _normalize_task_name(task_entry["name"])
                    ratio = _fuzzy_ratio(sentence, clean_name)
                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_task = task_entry
                if best_task:
                    match_score = best_ratio

            if not best_task:
                continue

            task_gid = best_task.get("gid", "")
            if not task_gid:
                continue

            # Skip if already in completion_detector dedup cache
            if _have_completion_detector and _is_deduped(task_gid, chunk["source_id"]):
                continue

            # Confidence: use semantic scoring when semantic match was used
            if used_semantic:
                confidence = _confidence_from_sim(match_score, chunk["source"])
            else:
                confidence = _confidence_from_ratio(match_score, chunk["source"])

            if confidence == "LOW":
                continue

            # Best-score-wins: keep the highest-confidence match per task
            prev = best_per_task.get(task_gid)
            if prev is None or match_score > prev[0]:
                best_per_task[task_gid] = (match_score, used_semantic, chunk, best_task)

    # Build gap list from best-per-task matches, sorted by score descending
    gaps: list[ReconciliationGap] = []
    for task_gid, (match_score, used_semantic, chunk, best_task) in sorted(
        best_per_task.items(), key=lambda x: x[1][0], reverse=True
    ):
        if len(gaps) >= MAX_GAPS_PER_PASS:
            break

        task_name = best_task.get("name", "")
        task_url = best_task.get("permalink_url", "")
        assignee = best_task.get("assignee") or {}
        assignee_name = assignee.get("name", "")

        match_method = "semantic" if used_semantic else "fuzzy"
        match_label = (
            f"sim={match_score:.3f}" if used_semantic else f"ratio={match_score:.3f}"
        )

        description = (
            f"Possible task completion: \"{task_name}\" "
            f"-- {chunk['source']} says: \"{chunk['content'][:120]}\""
        )
        if assignee_name:
            description += f" (assigned to {assignee_name})"

        gaps.append(ReconciliationGap(
            gap_id=_gap_id("stale_open_task", chunk["source_id"], task_gid),
            gap_type="stale_open_task",
            description=description,
            source_evidence=chunk["content"][:400],
            source=chunk["source"],
            source_id=chunk["source_id"],
            entity=chunk["entity"],
            confidence=(
                _confidence_from_sim(match_score, chunk["source"])
                if used_semantic
                else _confidence_from_ratio(match_score, chunk["source"])
            ),
            proposed_action=(
                f"Close Asana task \"{task_name}\" "
                f"({task_url}) -- completion language found in {chunk['source']} "
                f"[{match_method} {match_label}]"
            ),
            payload={
                "task_gid": task_gid,
                "task_name": task_name,
                "task_url": task_url,
                "assignee_name": assignee_name,
                "source": chunk["source"],
                "source_id": chunk["source_id"],
                "match_method": match_method,
                "match_score": round(match_score, 4),
            },
            deep_link=chunk["deep_link"],
            title=chunk["title"],
        ))

    log.info(
        "reconciliation pass4: %d stale-task gaps found (%s matching, %d sources, "
        "%d sentence embeds%s)",
        len(gaps),
        "semantic" if use_semantic else "fuzzy",
        len(chunks),
        embeds_used,
        ", DEADLINE HIT" if deadline_hit else "",
    )
    return gaps


# ── Pass 5: Drive synthesis ────────────────────────────────────────────────────

# Max characters of Drive chunk content fed to Haiku per entity group
_PASS5_MAX_CHARS = 4000
# Max Drive files per Haiku call (keeps prompt bounded)
_PASS5_MAX_FILES = 10

# Entity codes that represent real Drive-accessible entities (excludes LEX — PHI)
_DRIVE_ENTITY_CODES = {"FNDR", "HJRG", "F3E", "OSN", "BDM", "HJRP", "HJRPROD", "UFL"}

_DRIVE_SYNTHESIS_PROMPT = """You are a cross-reference analyst for a portfolio of businesses.

Below is a sample of recently-ingested Drive documents grouped by entity.
Also provided are the entity's open Asana tasks (if any) and active HubSpot deals (if any).

Your job: identify gaps — documents or decisions in Drive that suggest:
  (a) A missing Asana task (action/commitment with no matching open task), or
  (b) A decision that isn't yet captured in any task, or
  (c) A completed milestone that would close or update an open task.

Rules:
- Return ONLY a JSON object with three keys: "missing_tasks", "decisions", "completed_tasks".
- Each key maps to a list of objects. If none found, return an empty list for that key.
- missing_tasks items: {"subject": str, "source_filename": str, "entity": str, "confidence": "HIGH"|"MED"}
- decisions items: {"summary": str, "source_filename": str, "entity": str, "confidence": "HIGH"|"MED"}
- completed_tasks items: {"task_name_hint": str, "source_filename": str, "entity": str, "confidence": "HIGH"|"MED"}
- Exclude anything involving PHI, client health data, diagnoses, or care plans.
- Exclude Visibility CPA team members (Hayden Greber, Andrew Stubbs, Sarah Bertoglio, Emily Stubbs).
- Max 3 items per key. Prefer HIGH-confidence, obvious gaps over speculative ones.
- If nothing actionable is found, return {"missing_tasks": [], "decisions": [], "completed_tasks": []}.

Return ONLY the JSON object. No preamble. No explanation.
"""


def pass5_drive_insights(
    open_tasks: list[dict[str, Any]],
    active_deals: list[dict[str, Any]],
    anthropic_client: Any,
    *,
    lookback_seconds: float = DEFAULT_LOOKBACK_SECONDS,
    db_path: Path | None = None,
) -> list[ReconciliationGap]:
    """Pass 5: Cross-reference recently-ingested Drive chunks against open tasks + deals.

    Groups Drive KB chunks by entity, feeds each group to Claude Haiku with the
    entity's open tasks and active deals as context, and returns gaps. LEX is
    excluded entirely (PHI).
    """
    gaps: list[ReconciliationGap] = []
    db = db_path or _kb_db_path()
    cutoff_ts = int(time.time()) - int(lookback_seconds)

    # Query recently-ingested drive_sweep chunks, excluding LEX
    try:
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        # Content-date window (same rule as _query_kb_chunks): a backfill's
        # ingestion burst must not read as "recent Drive activity".
        rows = conn.execute(
            """
            SELECT source_id, entity, sub_entity, content, metadata
            FROM knowledge_chunks
            WHERE source = 'drive_sweep'
              AND COALESCE(date_modified, date_created, ingested_at) >= ?
            ORDER BY COALESCE(date_modified, date_created, ingested_at) DESC
            LIMIT 200
            """,
            (cutoff_ts,),
        ).fetchall()
        conn.close()
    except Exception as exc:
        log.error("pass5: DB query failed: %s", exc)
        return gaps

    if not rows:
        log.info("pass5: no recent drive_sweep chunks found in lookback window")
        return gaps

    # Group by entity, skip LEX (PHI) and unknown entities
    by_entity: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        entity = row["entity"] or "FNDR"
        if entity.startswith("LEX"):
            continue  # PHI guardrail — always skip
        if entity not in _DRIVE_ENTITY_CODES:
            continue
        by_entity.setdefault(entity, []).append(row)

    log.info("pass5: %d entities with recent drive chunks", len(by_entity))

    # Build entity → task/deal lookup strings for context injection
    task_by_entity: dict[str, str] = {}
    for task in open_tasks:
        name = (task.get("name") or "").strip()
        if not name:
            continue
        # Entity tag is the first bracketed prefix, e.g. "[F3E]"
        m = re.match(r"\[([A-Z0-9\-]+)\]", name)
        if m:
            ent = m.group(1)
            task_by_entity.setdefault(ent, "")
            task_by_entity[ent] += f"- {name}\n"

    deal_by_entity: dict[str, str] = {}
    for deal in active_deals:
        name = (deal.get("name") or deal.get("dealname") or "").strip()
        if not name:
            continue
        # Map deals roughly — F3E retail pipeline to F3E
        deal_by_entity.setdefault("F3E", "")
        deal_by_entity["F3E"] += f"- {name}\n"

    for entity, chunk_rows in by_entity.items():
        if len(gaps) >= MAX_GAPS_PER_PASS:
            break

        # Build a bounded document summary block for this entity
        doc_blocks: list[str] = []
        total_chars = 0
        for row in chunk_rows[:_PASS5_MAX_FILES]:
            content = (row["content"] or "").strip()
            if not content:
                continue
            # PHI check on content (belt-and-suspenders)
            if _PHI_RE.search(content[:500]):
                continue
            # Skip if Visibility CPA names appear
            if _mentions_vis_cpa(content):
                continue

            try:
                meta = json.loads(row["metadata"] or "{}")
            except (ValueError, TypeError):
                meta = {}
            filename = meta.get("filename") or row["source_id"] or "unknown"
            snippet = content[:400]
            block = f"[{filename}]\n{snippet}\n"
            if total_chars + len(block) > _PASS5_MAX_CHARS:
                break
            doc_blocks.append(block)
            total_chars += len(block)

        if not doc_blocks:
            continue

        docs_text = "\n---\n".join(doc_blocks)
        tasks_text = task_by_entity.get(entity, "(none)")
        deals_text = deal_by_entity.get(entity, "(none)")

        user_msg = (
            f"ENTITY: {entity}\n\n"
            f"OPEN ASANA TASKS:\n{tasks_text}\n\n"
            f"ACTIVE HUBSPOT DEALS:\n{deals_text}\n\n"
            f"RECENT DRIVE DOCUMENTS:\n{docs_text}"
        )

        try:
            resp = anthropic_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=800,
                system=_DRIVE_SYNTHESIS_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
            raw = (resp.content[0].text or "").strip()
        except Exception as exc:
            log.error("pass5: Haiku call failed for entity %s: %s", entity, exc)
            continue

        # Strip markdown fences that Haiku sometimes wraps around JSON
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1]
            raw = raw.rsplit("```", 1)[0].strip()

        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            log.warning("pass5: Haiku returned non-JSON for entity %s: %.120s", entity, raw)
            continue

        # Convert parsed items to ReconciliationGap objects
        for item in (parsed.get("missing_tasks") or [])[:3]:
            subj = (item.get("subject") or "").strip()
            src_file = item.get("source_filename", "")
            conf = item.get("confidence", "MED")
            if not subj or conf not in ("HIGH", "MED"):
                continue
            gap_id = f"pass5:drive:{hash(subj + entity) & 0xFFFFFFFF:08x}"
            gaps.append(ReconciliationGap(
                gap_id=gap_id,
                gap_type="missing_asana_task",
                description=f"[{entity}] Drive doc suggests missing task: {subj}",
                source_evidence=f"Source: {src_file}",
                source="drive_sweep",
                source_id=src_file,
                entity=entity,
                confidence=conf,
                proposed_action=f"Create Asana task: [{entity}] {subj}",
                title=subj,
            ))

        for item in (parsed.get("decisions") or [])[:3]:
            summary = (item.get("summary") or "").strip()
            src_file = item.get("source_filename", "")
            conf = item.get("confidence", "MED")
            if not summary or conf not in ("HIGH", "MED"):
                continue
            gap_id = f"pass5:decision:{hash(summary + entity) & 0xFFFFFFFF:08x}"
            gaps.append(ReconciliationGap(
                gap_id=gap_id,
                gap_type="uncaptured_decision",
                description=f"[{entity}] Uncaptured decision in Drive: {summary}",
                source_evidence=f"Source: {src_file}",
                source="drive_sweep",
                source_id=src_file,
                entity=entity,
                confidence=conf,
                proposed_action=f"Capture decision in decisions.md: {summary}",
                title=summary,
            ))

        for item in (parsed.get("completed_tasks") or [])[:3]:
            hint = (item.get("task_name_hint") or "").strip()
            src_file = item.get("source_filename", "")
            conf = item.get("confidence", "MED")
            if not hint or conf not in ("HIGH", "MED"):
                continue
            gap_id = f"pass5:complete:{hash(hint + entity) & 0xFFFFFFFF:08x}"
            gaps.append(ReconciliationGap(
                gap_id=gap_id,
                gap_type="stale_open_task",
                description=f"[{entity}] Drive doc suggests task may be done: {hint}",
                source_evidence=f"Source: {src_file}",
                source="drive_sweep",
                source_id=src_file,
                entity=entity,
                confidence=conf,
                proposed_action=f"Review and close Asana task matching: {hint}",
                title=hint,
            ))

        if len(gaps) >= MAX_GAPS_PER_PASS:
            break

    log.info("pass5 (drive insights): %d gaps found", len(gaps))
    return gaps


# ── Top-level orchestration ────────────────────────────────────────────────────

def reconcile(
    open_tasks: list[dict[str, Any]],
    active_deals: list[dict[str, Any]],
    *,
    lookback_seconds: float = DEFAULT_LOOKBACK_SECONDS,
    db_path: Path | None = None,
    passes: list[int] | None = None,
    anthropic_client: Any | None = None,
    deadline_monotonic: float | None = None,
) -> list[ReconciliationGap]:
    """Run all reconciliation passes and return actionable gaps.

    Parameters
    ----------
    open_tasks:
        Pre-fetched incomplete Asana task dicts (from asana_client.get_user_tasks
        or a broader search). Must include gid, name, permalink_url, assignee.
    active_deals:
        Pre-fetched HubSpot deal dicts. Must include name, id/gid,
        last_activity_ts (Unix seconds), and optionally deep_link.
    lookback_seconds:
        How far back to scan KB chunks. Default 25h.
    db_path:
        Override KB DB path (for testing).
    passes:
        Which passes to run (1-5). Default: all five. Useful for targeted
        runs or testing.
    anthropic_client:
        Anthropic client instance for Pass 5 (Drive synthesis). Required if
        5 is in passes; Pass 5 is silently skipped if None.

    Returns
    -------
    List of ReconciliationGap objects with confidence HIGH or MED, sorted by
    confidence desc. LOW confidence gaps are filtered out.
    """
    passes = passes or [1, 2, 3, 4, 5]
    all_gaps: list[ReconciliationGap] = []

    kwargs = {"lookback_seconds": lookback_seconds, "db_path": db_path}

    def _past_deadline(pass_no: int) -> bool:
        if deadline_monotonic is not None and time.monotonic() > deadline_monotonic:
            log.warning(
                "reconciliation: wall-clock deadline reached -- skipping pass %d "
                "and any remaining passes", pass_no,
            )
            return True
        return False

    if 1 in passes and not _past_deadline(1):
        try:
            all_gaps.extend(pass1_missing_asana_tasks(open_tasks, **kwargs))
        except Exception as exc:
            log.error("reconciliation pass1 failed: %s", exc, exc_info=True)

    if 2 in passes and not _past_deadline(2):
        try:
            all_gaps.extend(pass2_stale_hubspot_deals(active_deals, **kwargs))
        except Exception as exc:
            log.error("reconciliation pass2 failed: %s", exc, exc_info=True)

    if 3 in passes and not _past_deadline(3):
        try:
            all_gaps.extend(pass3_uncaptured_decisions(**kwargs))
        except Exception as exc:
            log.error("reconciliation pass3 failed: %s", exc, exc_info=True)

    if 4 in passes and not _past_deadline(4):
        try:
            all_gaps.extend(pass4_stale_open_tasks(
                open_tasks, deadline_monotonic=deadline_monotonic, **kwargs
            ))
        except Exception as exc:
            log.error("reconciliation pass4 failed: %s", exc, exc_info=True)

    if 5 in passes and anthropic_client is not None:
        if not _past_deadline(5):
            try:
                all_gaps.extend(
                    pass5_drive_insights(
                        open_tasks, active_deals, anthropic_client, **kwargs
                    )
                )
            except Exception as exc:
                log.error("reconciliation pass5 failed: %s", exc, exc_info=True)
    elif 5 in passes and anthropic_client is None:
        log.info("reconciliation pass5 skipped: no anthropic_client provided")

    # Filter to actionable only (HIGH + MED), sort by confidence desc
    actionable = [g for g in all_gaps if g.is_actionable]
    actionable.sort(key=lambda g: (0 if g.confidence == "HIGH" else 1))

    log.info(
        "reconciliation: total=%d actionable=%d (high=%d, med=%d)",
        len(all_gaps),
        len(actionable),
        sum(1 for g in actionable if g.confidence == "HIGH"),
        sum(1 for g in actionable if g.confidence == "MED"),
    )
    return actionable
