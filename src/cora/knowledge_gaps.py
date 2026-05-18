"""Append-only knowledge gap log."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

_LOG_PATH = Path(__file__).parent.parent.parent / "logs" / "knowledge-gaps.jsonl"
_LOCK = Lock()

log = logging.getLogger(__name__)


def log_gap(
    entity: str,
    channel: str,
    user: str,
    question: str,
    response_chars: int,
    gap: str,
    latency_ms: int,
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
    }
    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        with _LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    log.info("knowledge gap flagged entity=%s gap_chars=%d", entity, len(gap))
