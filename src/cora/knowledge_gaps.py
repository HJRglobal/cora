"""Append-only knowledge gap log."""

import json
import logging
import os
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
) -> None:
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
    path = _log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    log.info("knowledge gap flagged entity=%s detector=%s gap_chars=%d",
             entity, detector, len(gap))
