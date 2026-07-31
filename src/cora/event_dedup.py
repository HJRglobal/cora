"""Slack event-delivery idempotency (cq-479b157f8c00).

Slack Socket Mode is at-least-once: during the 2026-07-27 SSLEOFError WebSocket
flap, acks sent on dying TLS sessions never reached Slack, which redelivered the
same event envelopes — and Cora ran the FULL Q&A pipeline per delivery (two
placeholders 14ms apart; one Haiku run answered "can't access inventory" while
its twin called the tool and posted figures). Bolt Python has no built-in dedup,
and the socket-mode adapter DROPS the envelope's retry metadata, so a retry is
indistinguishable from a fresh event — the only reliable key is the events-API
top-level ``event_id``, globally unique per event and identical only on
redelivery.

Deliberately NOT keyed on channel+ts: the app_mention and message events for
the same Slack message carry DIFFERENT event_ids by design, and the dual-path
handling ("@Cora yes" confirms via the message path while handle_mention also
fires; the W1-01 guard governs that class) must keep working.

In-memory only: dedup does not span stacked bot processes — the single-verified-
instance restart doctrine (#5) remains the operating assumption there.
"""

from __future__ import annotations

import threading
import time

_TTL_SECONDS = 3600  # Slack retries span seconds to ~5 min; 1h is comfortable.
_SEEN: dict[str, float] = {}
_LOCK = threading.Lock()


def is_duplicate(event_id: str, ttl_secs: float = _TTL_SECONDS) -> bool:
    """Atomic check-and-insert: False the first time an event_id is seen (and it
    is recorded), True on a repeat within the TTL. Fail-open by contract: callers
    wrap this so an unexpected error never blocks event dispatch."""
    if not event_id:
        return False
    now = time.monotonic()
    with _LOCK:
        # Opportunistic prune so the dict never grows unboundedly.
        if len(_SEEN) > 4096:
            cutoff = now - ttl_secs
            for k in [k for k, ts in _SEEN.items() if ts < cutoff]:
                del _SEEN[k]
        ts = _SEEN.get(event_id)
        if ts is not None and (now - ts) < ttl_secs:
            return True
        _SEEN[event_id] = now
    return False


def reset_for_tests() -> None:
    with _LOCK:
        _SEEN.clear()
