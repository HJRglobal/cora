"""Completion detector — cross-reference KB signals against open Asana tasks.

Scans recently-ingested KB chunks (Fireflies, Slack, email, HubSpot) for
language that indicates a task or project was completed, then fuzzy-matches
those signals against open Asana tasks to surface actionable recommendations.

Mode B (scheduled sweep) is the primary delivery path:
  run_completion_sweep.py calls detect_candidates() daily and posts a digest
  to #hjrg-leadership with clickable Asana deep links.

Confidence levels:
  HIGH  >= 0.80 — Fireflies transcript with explicit completion verb + noun
                  that substring-matches an open task (≥0.60 fuzzy ratio)
  MED   >= 0.60 — Slack/email/HubSpot signal with fuzzy task match
  LOW   < 0.60  — broad keyword hit; NOT surfaced in sweep (noise risk)

Dedup: same (task_gid, source_id) pair is skipped if already recommended
within DEDUP_WINDOW_HOURS.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ── Tunable constants ──────────────────────────────────────────────────────

# Only surface candidates at or above this confidence in the sweep digest.
SWEEP_CONFIDENCE_THRESHOLD = 0.60

# Lookback window for KB chunk queries (seconds). Default 25h (buffer for
# late-ingested Fireflies transcripts from the previous evening).
DEFAULT_LOOKBACK_SECONDS = 25 * 3600

# Minimum fuzzy ratio between signal noun-phrase and task name to consider a
# match at all. Below this, skip the candidate.
MIN_FUZZY_RATIO = 0.35

# Fuzzy ratio that, combined with a high-confidence source, reaches HIGH tier.
HIGH_FUZZY_RATIO = 0.60

# Don't re-recommend the same (task_gid, source_id) within this window.
DEDUP_WINDOW_HOURS = 48

# Maximum candidates returned per sweep (cap noise on low-threshold runs).
MAX_CANDIDATES = 30

# ── Completion signal vocabulary ───────────────────────────────────────────

# Verbs that strongly imply completion. Applied as whole-word regex.
_COMPLETION_VERBS = (
    r"complet(?:ed?|ion)",
    r"finish(?:ed|es)?",
    r"\bdone\b",
    r"shipped?",
    r"launch(?:ed)?",
    r"sign(?:ed)?",
    r"closed?",
    r"resolved?",
    r"paid",
    r"receiv(?:ed)?",
    r"confirm(?:ed)?",
    r"approved?",
    r"submitted?",
    r"delivered?",
    r"executed?",
    r"wrapped?\s+up",
    r"sent\s+(?:over|out|the)",
    r"went\s+live",
    r"went\s+(?:through|out)",
    r"is\s+live",
    r"are\s+live",
    r"got\s+(?:it\s+)?done",
    r"taken\s+care\s+of",
    r"all\s+set",
    r"good\s+to\s+go",
    r"no\s+longer\s+needed",
    r"cancell?ed",
)

_COMPLETION_RE = re.compile(
    r"(?:" + r"|".join(_COMPLETION_VERBS) + r")",
    re.IGNORECASE,
)

# Source confidence weights — Fireflies transcripts are the most reliable
# because they capture spoken decisions directly.
_SOURCE_WEIGHTS: dict[str, float] = {
    "fireflies": 0.90,
    "slack":     0.75,
    "gmail":     0.70,
    "hubspot":   0.65,
    "asana":     0.60,
    "static_md": 0.40,
}

# ── Data structures ────────────────────────────────────────────────────────

@dataclass
class CompletionSignal:
    """One completion-indicating snippet extracted from a KB chunk."""
    source: str           # "fireflies" | "slack" | "gmail" | "hubspot" | "asana"
    source_id: str        # native ID from source system (for dedup)
    entity: str           # Cora entity code ("F3E", "OSN", etc.)
    signal_text: str      # the sentence / excerpt that triggered detection
    source_weight: float  # base confidence from source type (0–1)
    source_ts: float      # ingested_at unix timestamp of the KB chunk
    deep_link: str        # clickable link back to source (Slack mrkdwn or URL)
    title: str            # chunk title (meeting name, email subject, etc.)


@dataclass
class CompletionCandidate:
    """A (signal, Asana task) pairing that warrants a human review."""
    signal: CompletionSignal
    task_gid: str
    task_name: str
    task_url: str          # Asana permalink (for deep link)
    assignee_name: str     # "" if unassigned
    project_name: str      # first project name, "" if none
    fuzzy_ratio: float     # SequenceMatcher ratio (signal ↔ task name)
    confidence: float      # final blended score

    @property
    def is_high_confidence(self) -> bool:
        return self.confidence >= 0.80

    @property
    def is_mid_confidence(self) -> bool:
        return 0.60 <= self.confidence < 0.80

    def slack_line(self) -> str:
        """Format one candidate as a Slack mrkdwn bullet."""
        task_link = f"<{self.task_url}|{self.task_name}>" if self.task_url else self.task_name
        source_back = f" (<{self.signal.deep_link}|source>)" if self.signal.deep_link else ""
        conf_tag = "🟢" if self.is_high_confidence else "🟡"
        assignee_tag = f" · {self.assignee_name}" if self.assignee_name else ""
        project_tag = f" · _{self.project_name}_" if self.project_name else ""
        excerpt = self.signal.signal_text[:120].replace("\n", " ")
        return (
            f"{conf_tag} {task_link}{assignee_tag}{project_tag}\n"
            f'   > "{excerpt}"\n'
            f"   via {self.signal.source} · {self.signal.title}{source_back}"
        )


# ── DB helpers ─────────────────────────────────────────────────────────────

def _kb_db_path() -> Path:
    """Resolve the canonical KB sqlite path."""
    return Path(__file__).resolve().parents[3] / "data" / "cora_kb.db"


def _dedup_db_path() -> Path:
    p = Path(__file__).resolve().parents[3] / "data" / "cache" / "completion-dedup.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _ensure_dedup_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS completion_dedup (
            task_gid    TEXT NOT NULL,
            source_id   TEXT NOT NULL,
            recommended_at INTEGER NOT NULL,
            PRIMARY KEY (task_gid, source_id)
        )
        """
    )
    conn.commit()


def _is_deduped(task_gid: str, source_id: str) -> bool:
    """Return True if this (task, source) pair was already recommended recently."""
    cutoff = time.time() - DEDUP_WINDOW_HOURS * 3600
    conn = sqlite3.connect(str(_dedup_db_path()))
    try:
        _ensure_dedup_table(conn)
        row = conn.execute(
            "SELECT recommended_at FROM completion_dedup WHERE task_gid=? AND source_id=?",
            (task_gid, source_id),
        ).fetchone()
        if not row:
            return False
        return row[0] >= cutoff
    finally:
        conn.close()


def _mark_deduped(task_gid: str, source_id: str) -> None:
    conn = sqlite3.connect(str(_dedup_db_path()))
    try:
        _ensure_dedup_table(conn)
        conn.execute(
            """
            INSERT INTO completion_dedup (task_gid, source_id, recommended_at)
            VALUES (?, ?, ?)
            ON CONFLICT(task_gid, source_id) DO UPDATE SET recommended_at=excluded.recommended_at
            """,
            (task_gid, source_id, int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()


# ── Signal extraction ──────────────────────────────────────────────────────

def extract_signals_from_db(
    *,
    lookback_seconds: float = DEFAULT_LOOKBACK_SECONDS,
    entities: list[str] | None = None,
    db_path: Path | None = None,
) -> list[CompletionSignal]:
    """Query KB chunks ingested within the lookback window that contain
    completion language. Returns one CompletionSignal per matching chunk.

    entities: if provided, restrict to those entity codes. None = all entities.
    """
    cutoff_ts = int(time.time() - lookback_seconds)
    db_path = db_path or _kb_db_path()

    if not db_path.exists():
        log.warning("KB DB not found at %s — returning empty signals", db_path)
        return []

    conn = sqlite3.connect(str(db_path))
    try:
        # Exclude static_md (playbooks/CLAUDE.md) — too much noise, low signal.
        params: list = [cutoff_ts]
        entity_clause = ""
        if entities:
            placeholders = ",".join("?" * len(entities))
            entity_clause = f"AND entity IN ({placeholders})"
            params.extend(entities)

        rows = conn.execute(
            f"""
            SELECT source, source_id, entity, content, deep_link, title,
                   ingested_at
            FROM knowledge_chunks
            WHERE ingested_at >= ?
              AND source != 'static_md'
              {entity_clause}
            ORDER BY ingested_at DESC
            """,
            params,
        ).fetchall()
    finally:
        conn.close()

    signals: list[CompletionSignal] = []
    for source, source_id, entity, content, deep_link, title, ingested_at in rows:
        # Sentence-level scan: split on ". " / "\n" and test each sentence
        # so we can return a tighter excerpt than the full chunk.
        sentences = re.split(r"(?<=[.!?])\s+|\n+", content or "")
        for sentence in sentences:
            if not _COMPLETION_RE.search(sentence):
                continue
            weight = _SOURCE_WEIGHTS.get(source or "static_md", 0.50)
            signals.append(
                CompletionSignal(
                    source=source or "",
                    source_id=source_id or "",
                    entity=entity or "FNDR",
                    signal_text=sentence.strip(),
                    source_weight=weight,
                    source_ts=float(ingested_at or 0),
                    deep_link=deep_link or "",
                    title=title or "",
                )
            )
    log.info(
        "extract_signals_from_db: %d chunks scanned → %d signals (lookback=%ds)",
        len(rows), len(signals), int(lookback_seconds),
    )
    return signals


def extract_signals_from_text(
    text: str,
    *,
    source: str = "slack",
    source_id: str = "",
    entity: str = "FNDR",
    deep_link: str = "",
    title: str = "",
) -> list[CompletionSignal]:
    """Extract signals directly from an arbitrary text blob (e.g. a real-time
    Slack message). Used for potential future inline (Mode A) support."""
    weight = _SOURCE_WEIGHTS.get(source, 0.50)
    signals: list[CompletionSignal] = []
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text or "")
    for sentence in sentences:
        if not _COMPLETION_RE.search(sentence):
            continue
        signals.append(
            CompletionSignal(
                source=source,
                source_id=source_id,
                entity=entity,
                signal_text=sentence.strip(),
                source_weight=weight,
                source_ts=time.time(),
                deep_link=deep_link,
                title=title,
            )
        )
    return signals


# ── Fuzzy task matching ────────────────────────────────────────────────────

def _fuzzy_ratio(a: str, b: str) -> float:
    """SequenceMatcher ratio between two strings (lowercased)."""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _best_task_match(
    signal: CompletionSignal,
    open_tasks: list[dict],
) -> tuple[dict, float] | None:
    """Return the (task_dict, fuzzy_ratio) pair with the highest ratio above
    MIN_FUZZY_RATIO, or None if no task clears the threshold."""
    text = signal.signal_text
    best_task: dict | None = None
    best_ratio = MIN_FUZZY_RATIO  # anything below this is ignored

    for task in open_tasks:
        name = task.get("name") or ""
        if not name:
            continue
        ratio = _fuzzy_ratio(text, name)
        if ratio > best_ratio:
            best_ratio = ratio
            best_task = task

    if best_task is None:
        return None
    return best_task, best_ratio


def compute_confidence(source_weight: float, fuzzy_ratio: float) -> float:
    """Blend source reliability and name-match quality into a single score.

    Formula: weight * 0.55 + fuzzy_ratio * 0.45
    Fireflies (0.90) + HIGH_FUZZY (0.60) → 0.90*0.55 + 0.60*0.45 = 0.765 (MID+)
    Fireflies (0.90) + perfect (1.00)    → 0.90*0.55 + 1.00*0.45 = 0.945 (HIGH)
    Slack (0.75)     + HIGH_FUZZY (0.60) → 0.75*0.55 + 0.60*0.45 = 0.6825 (MID)
    """
    return round(source_weight * 0.55 + fuzzy_ratio * 0.45, 4)


# ── Top-level orchestration ────────────────────────────────────────────────

def match_signals_to_tasks(
    signals: list[CompletionSignal],
    open_tasks: list[dict],
    *,
    min_confidence: float = SWEEP_CONFIDENCE_THRESHOLD,
    apply_dedup: bool = True,
) -> list[CompletionCandidate]:
    """Cross-reference signals against open Asana tasks.

    Returns CompletionCandidate list sorted by confidence desc, capped at
    MAX_CANDIDATES. Dedup is applied when apply_dedup=True (always in prod;
    disable in tests).
    """
    candidates: list[CompletionCandidate] = []

    for signal in signals:
        match = _best_task_match(signal, open_tasks)
        if match is None:
            continue
        task, ratio = match
        conf = compute_confidence(signal.source_weight, ratio)
        if conf < min_confidence:
            continue

        task_gid = task.get("gid") or ""
        if apply_dedup and _is_deduped(task_gid, signal.source_id):
            log.debug("dedup skip: task_gid=%s source_id=%s", task_gid, signal.source_id)
            continue

        # Extract task metadata
        permalink = task.get("permalink_url") or ""
        assignee = (task.get("assignee") or {}).get("name") or ""
        projects = task.get("projects") or task.get("memberships") or []
        project_name = ""
        if projects:
            first = projects[0]
            project_name = (first.get("project") or first).get("name") or ""

        candidates.append(
            CompletionCandidate(
                signal=signal,
                task_gid=task_gid,
                task_name=task.get("name") or "",
                task_url=permalink,
                assignee_name=assignee,
                project_name=project_name,
                fuzzy_ratio=round(ratio, 4),
                confidence=conf,
            )
        )

    # Sort by confidence desc, deduplicate by task_gid (keep highest-conf signal)
    candidates.sort(key=lambda c: c.confidence, reverse=True)
    seen_tasks: set[str] = set()
    deduped: list[CompletionCandidate] = []
    for c in candidates:
        if c.task_gid not in seen_tasks:
            seen_tasks.add(c.task_gid)
            deduped.append(c)

    return deduped[:MAX_CANDIDATES]


def detect_candidates(
    open_tasks: list[dict],
    *,
    lookback_seconds: float = DEFAULT_LOOKBACK_SECONDS,
    entities: list[str] | None = None,
    min_confidence: float = SWEEP_CONFIDENCE_THRESHOLD,
    apply_dedup: bool = True,
    db_path: Path | None = None,
) -> list[CompletionCandidate]:
    """Full pipeline: extract signals → match → filter → return candidates.

    open_tasks: pre-fetched list of incomplete Asana task dicts (caller
                handles the API call so this module stays testable without
                live Asana access).
    """
    signals = extract_signals_from_db(
        lookback_seconds=lookback_seconds,
        entities=entities,
        db_path=db_path,
    )
    return match_signals_to_tasks(
        signals,
        open_tasks,
        min_confidence=min_confidence,
        apply_dedup=apply_dedup,
    )


def mark_candidates_sent(candidates: list[CompletionCandidate]) -> None:
    """Record that these candidates were surfaced so dedup fires on retry."""
    for c in candidates:
        _mark_deduped(c.task_gid, c.signal.source_id)


# ── Slack formatting ───────────────────────────────────────────────────────

def format_sweep_digest(
    candidates: list[CompletionCandidate],
    *,
    lookback_hours: float = DEFAULT_LOOKBACK_SECONDS / 3600,
) -> str:
    """Build the full Slack mrkdwn digest message for the daily sweep post."""
    if not candidates:
        return (
            f"✅ *Completion sweep — last {int(lookback_hours)}h*\n"
            "No completion candidates found. All open tasks look genuinely open."
        )

    high = [c for c in candidates if c.is_high_confidence]
    mid  = [c for c in candidates if c.is_mid_confidence]

    lines = [
        f"🧹 *Completion candidates — last {int(lookback_hours)}h* "
        f"({len(candidates)} found · tap to open in Asana · mark done if confirmed)",
        "",
    ]

    if high:
        lines.append("*🟢 High confidence*")
        for c in high:
            lines.append(c.slack_line())
            lines.append("")

    if mid:
        lines.append("*🟡 Medium confidence — verify before closing*")
        for c in mid:
            lines.append(c.slack_line())
            lines.append("")

    lines.append(
        "_These are recommendations only — Cora does not auto-complete tasks. "
        "Open the link and mark done if confirmed._"
    )
    return "\n".join(lines).strip()
