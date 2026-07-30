"""Append-only knowledge gap log."""

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

_DEFAULT_LOG_PATH = Path(__file__).parent.parent.parent / "logs" / "knowledge-gaps.jsonl"
_LOCK = Lock()

log = logging.getLogger(__name__)


def _log_path() -> Path:
    """Same env override the gap_autofill READER honors (KNOWLEDGE_GAPS_LOG_PATH)
    so writer and reader can never point at different files."""
    return Path(os.environ.get("KNOWLEDGE_GAPS_LOG_PATH") or _DEFAULT_LOG_PATH)


# ─────────────────────────────────────────────────────────────────────────────
# Fork 3a (Wave-1 flywheel-conversion calibration, 2026-07-30): capability-ask
# routing at the shared log_gap chokepoint
# ─────────────────────────────────────────────────────────────────────────────
# A capability ask ("can you access X", "do you have access to X", "are you able to
# X", "I (just) shared/saved/gave you X") is asking whether CORA can DO something --
# a missing TOOL/connector -- not a missing FACT. Logging it as a knowledge gap sends
# it toward "teach Cora this fact" (known-answer drafting, owner escalation), which is
# the wrong lane; it belongs in the code-session queue as a feature candidate.
#
# Deterministic (D-034 pattern, no model call) and precision-biased: it requires an
# explicit SECOND-PERSON ("you") capability verb, so a genuine world/fact question
# ("will RSP be affected by the new HNT assessments?") never matches -- fail-closed,
# ambiguous text falls through to normal gap logging (status quo).
_CAPABILITY_VERBS = (
    r"access|reach|connect(?:\s+to)?|pull\s+from|see|get\s+into|read|open|"
    r"check|look\s+at|view"
)
_CAPABILITY_ASK_RE = re.compile(
    r"\b(?:can|could|do|does|are|is)\s+you\s+(?:" + _CAPABILITY_VERBS + r")\b"
    r"|\bdo\s+you\s+have\s+access\s+to\b"
    r"|\bare\s+you\s+able\s+to\b"
    r"|\bi\s+(?:just\s+)?(?:shared|saved|gave)\s+(?:you|it)\b",
    re.IGNORECASE,
)


def is_capability_ask(text: str) -> bool:
    """True for a second-person capability ask directed at Cora, False for a genuine
    world/fact question (or anything ambiguous -- fail-closed to False so it stays a
    normal knowledge gap). See module section above."""
    if not text:
        return False
    return bool(_CAPABILITY_ASK_RE.search(text))


def _route_capability_ask(*, entity: str, channel: str, user: str | None, question: str) -> bool:
    """Forward a capability ask to the code-queue as a feature candidate. Returns
    True if the code-queue accepted the route (caller must NOT also write the
    knowledge-gap record); False if the code-queue is off (caller falls back to
    normal gap logging -- a capability ask must never simply vanish). Fail-soft:
    any error here also falls back to normal logging."""
    try:
        from . import code_queue
        if code_queue.code_queue_level() == "off":
            return False
        code_queue.capture_capability_ask(question, entity, channel, user)
        return True
    except Exception:  # noqa: BLE001 -- log_gap must never raise into the Q&A hot path
        log.warning("knowledge_gaps: capability-ask route to code_queue failed "
                   "(non-fatal, falling back to normal gap logging)", exc_info=True)
        return False


def log_gap(
    entity: str,
    channel: str,
    user: str,
    question: str,
    response_chars: int,
    gap: str,
    latency_ms: int,
    detector: str = "llm_sentinel",
    private_source: bool = False,
    best_distance: float | None = None,
    chunks_returned: int | None = None,
) -> None:
    # Fork 3a (earliest-intercept, shared sink): a capability ask is rerouted to the
    # code-queue as a feature candidate instead of ever landing in this log -- this
    # single check covers all three log_gap callers (gap_detection.maybe_log_gap,
    # app.py's sentinel path, and code_queue._route_to_flywheel) regardless of which
    # one invoked it. Falls through to normal logging if the code-queue is off/erroring
    # (never silently drops a signal) or if the text isn't capability-shaped.
    if is_capability_ask(question) and _route_capability_ask(
            entity=entity, channel=channel, user=user, question=question):
        return
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "entity": entity,
        "channel": channel,
        "user": user,
        "question": question,
        "response_chars": response_chars,
        "gap": gap,
        "latency_ms": latency_ms,
        # WS-1: which mechanism flagged this gap. "llm_sentinel" = the model
        # emitted [CORA_KNOWLEDGE_GAP: ...]; "kb_miss"/"unknown_response" =
        # the deterministic detectors in gap_detection.py. Pre-WS-1 records
        # lack the field and are treated as llm_sentinel.
        "detector": detector,
    }
    if private_source:
        # DM-originated: gap_autofill must never quote this question to a
        # domain owner (mining stays allowed -- output is Harrison-gated).
        record["private_source"] = True
    # WS-1 kb_miss calibration (D-066 follow-up): the closest returned chunk's
    # distance + the raw returned count when a KB search ran. Recorded only when
    # present (KB search ran) so pre-existing and non-KB records stay clean.
    # kb_miss is currently unreachable (0 relevant hits never happens at ~560K
    # chunks); a week of these best_distance values lets kb_miss be recalibrated
    # to a distance FLOOR with Harrison. Neither field is PHI-bearing.
    if best_distance is not None:
        record["best_distance"] = best_distance
    if chunks_returned is not None:
        record["chunks_returned"] = chunks_returned
    path = _log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    log.info("knowledge gap flagged entity=%s detector=%s gap_chars=%d",
             entity, detector, len(gap))
