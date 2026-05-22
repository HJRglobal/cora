"""Slack chat.update throttling for streaming responses.

Two-layer rate limit:

  1. Per-stream interval gate: each individual stream must wait >= MIN_INTERVAL_S
     between consecutive update attempts. Avoids visual jitter from sub-second edits.

  2. Workspace-wide token bucket: across ALL concurrent streams, no more than
     WORKSPACE_BUDGET_PER_MINUTE updates per 60-second sliding window. Protects
     against Slack's chat.update Tier 3 rate limit (~50 RPM) when multiple users
     hit Cora concurrently.

Both layers run in the same call (`acquire`). If either layer says "wait", the
caller (the streaming callback) skips this update — the text keeps accumulating
in its local buffer and the next interval will catch up with the cumulative text.

Thread-safe: a single threading.Lock guards both the per-stream state map and
the workspace token bucket. Acquire is cheap (O(1) bucket operations).

This module is intentionally Slack-agnostic — it doesn't import slack_sdk or
make any API calls. It just answers "can I update right now?" Callers use the
answer to gate their actual chat_update calls.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Per-stream minimum interval between consecutive update attempts (seconds).
# 0.8s = ~75 updates per minute on a single stream, which feels "alive" without
# being jittery. Slack's edit indicator shows on every update but users adapt.
MIN_INTERVAL_S = 0.8

# Workspace-wide rolling-window budget. Slack's chat.update is Tier 3 (~50 RPM
# per workspace). 40 leaves headroom for the non-streaming chat.update calls
# (initial placeholder posts, final updates, error swaps).
WORKSPACE_BUDGET_PER_MINUTE = 40
WORKSPACE_WINDOW_S = 60.0


@dataclass
class _StreamState:
    last_attempt_at: float = 0.0
    last_successful_at: float = 0.0
    skipped_count: int = 0


class ChannelUpdateThrottle:
    """Two-layer throttle for Slack chat.update calls during streaming.

    Use via:
        throttle = ChannelUpdateThrottle()
        if throttle.acquire(stream_id):
            client.chat_update(...)  # actually update Slack
        else:
            pass  # skip this round; text keeps growing locally

    Each stream_id should be the Slack message ts (or any unique per-stream
    identifier). Stream state is kept per-id so two parallel streams in the
    same workspace don't share per-stream gates.

    Forced/final updates (e.g. the last chat.update at stream end) should
    use `force_acquire(stream_id)` to bypass the per-stream interval gate.
    The workspace budget still applies — but if the budget is exhausted,
    the caller can choose whether to still attempt (final updates SHOULD
    still try; logs/scope of the failure are acceptable).
    """

    def __init__(
        self,
        min_interval_s: float = MIN_INTERVAL_S,
        workspace_budget: int = WORKSPACE_BUDGET_PER_MINUTE,
        workspace_window_s: float = WORKSPACE_WINDOW_S,
    ):
        self._min_interval_s = min_interval_s
        self._workspace_budget = workspace_budget
        self._workspace_window_s = workspace_window_s
        self._lock = threading.Lock()
        self._streams: dict[str, _StreamState] = {}
        # Workspace-wide rolling window of recent update timestamps
        self._workspace_window: deque[float] = deque()

    def _evict_old(self, now: float) -> None:
        """Drop window entries older than the workspace window. Caller holds the lock."""
        cutoff = now - self._workspace_window_s
        while self._workspace_window and self._workspace_window[0] < cutoff:
            self._workspace_window.popleft()

    def _workspace_has_budget(self, now: float) -> bool:
        """True if a new update would fit in the workspace budget. Caller holds the lock."""
        self._evict_old(now)
        return len(self._workspace_window) < self._workspace_budget

    def acquire(self, stream_id: str) -> bool:
        """Try to acquire an update slot for `stream_id`.

        Returns True if both gates pass (per-stream interval + workspace budget).
        Returns False if either gate blocks. On True, the workspace budget is
        decremented (token consumed); on False, no state changes.
        """
        now = time.monotonic()
        with self._lock:
            state = self._streams.setdefault(stream_id, _StreamState())
            state.last_attempt_at = now

            if now - state.last_successful_at < self._min_interval_s:
                state.skipped_count += 1
                return False

            if not self._workspace_has_budget(now):
                state.skipped_count += 1
                log.debug(
                    "Workspace update budget exhausted (%d/%d in window) — skipping stream=%s",
                    len(self._workspace_window), self._workspace_budget, stream_id,
                )
                return False

            # Acquire
            state.last_successful_at = now
            self._workspace_window.append(now)
            return True

    def force_acquire(self, stream_id: str) -> bool:
        """Acquire bypassing the per-stream interval gate (for final updates).

        Workspace budget is still respected — final updates can fail if the
        workspace is throttled, but the caller usually still wants to try
        (and the chat.update API call itself will return an error we can log).
        On success, the per-stream and workspace state both update.
        """
        now = time.monotonic()
        with self._lock:
            state = self._streams.setdefault(stream_id, _StreamState())
            state.last_attempt_at = now
            if not self._workspace_has_budget(now):
                log.warning(
                    "Workspace budget exhausted on FORCE acquire — stream=%s "
                    "(final update may still be attempted by caller, but will be over-budget)",
                    stream_id,
                )
                # Still emit a token to mark the intent; let caller try the API
                # and handle whatever Slack returns.
                self._workspace_window.append(now)
                state.last_successful_at = now
                return False
            state.last_successful_at = now
            self._workspace_window.append(now)
            return True

    def release_stream(self, stream_id: str) -> dict[str, int]:
        """Remove per-stream state when the stream finishes. Returns small stats dict."""
        with self._lock:
            state = self._streams.pop(stream_id, None)
            if state is None:
                return {"skipped_count": 0}
            return {"skipped_count": state.skipped_count}

    def workspace_budget_remaining(self) -> int:
        """How many updates the workspace can still emit in the current window.

        Mostly useful for monitoring + tests.
        """
        now = time.monotonic()
        with self._lock:
            self._evict_old(now)
            return max(0, self._workspace_budget - len(self._workspace_window))


# Module-level singleton — Cora has exactly one workspace, so one throttle is fine.
default_throttle = ChannelUpdateThrottle()
