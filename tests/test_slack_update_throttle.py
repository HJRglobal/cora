"""Unit tests for ChannelUpdateThrottle — per-stream interval + workspace bucket.

Uses monkeypatching of time.monotonic to advance time deterministically.
"""

import threading
import time
from unittest.mock import patch

import cora.slack_update_throttle as st


def _make_throttle(min_interval=0.5, budget=10, window=60.0):
    return st.ChannelUpdateThrottle(
        min_interval_s=min_interval,
        workspace_budget=budget,
        workspace_window_s=window,
    )


# ---- Per-stream interval gate ----


def test_first_acquire_succeeds():
    t = _make_throttle()
    assert t.acquire("stream-1") is True


def test_immediate_second_acquire_blocked_by_interval():
    t = _make_throttle(min_interval=1.0)
    fake_time = [100.0]

    def now():
        return fake_time[0]

    with patch("cora.slack_update_throttle.time.monotonic", side_effect=now):
        assert t.acquire("stream-1") is True
        # 0.5s later — under the 1.0s interval
        fake_time[0] = 100.5
        assert t.acquire("stream-1") is False


def test_acquire_succeeds_after_interval_elapses():
    t = _make_throttle(min_interval=1.0)
    fake_time = [100.0]

    def now():
        return fake_time[0]

    with patch("cora.slack_update_throttle.time.monotonic", side_effect=now):
        assert t.acquire("stream-1") is True
        fake_time[0] = 101.0  # exactly at interval
        assert t.acquire("stream-1") is True


def test_per_stream_state_is_independent():
    t = _make_throttle(min_interval=1.0)
    fake_time = [100.0]

    def now():
        return fake_time[0]

    with patch("cora.slack_update_throttle.time.monotonic", side_effect=now):
        assert t.acquire("stream-1") is True
        # stream-2 has its own interval gate — should succeed even though stream-1 just fired
        assert t.acquire("stream-2") is True


# ---- Workspace budget ----


def test_workspace_budget_blocks_when_exhausted():
    t = _make_throttle(min_interval=0.0, budget=3)

    fake_time = [100.0]

    def now():
        return fake_time[0]

    with patch("cora.slack_update_throttle.time.monotonic", side_effect=now):
        # Burn the budget with 3 different streams (no per-stream gate at interval=0)
        assert t.acquire("s1") is True
        assert t.acquire("s2") is True
        assert t.acquire("s3") is True
        # 4th should be blocked by workspace budget
        assert t.acquire("s4") is False


def test_workspace_budget_recovers_after_window():
    t = _make_throttle(min_interval=0.0, budget=2, window=60.0)
    fake_time = [100.0]

    def now():
        return fake_time[0]

    with patch("cora.slack_update_throttle.time.monotonic", side_effect=now):
        assert t.acquire("s1") is True
        assert t.acquire("s2") is True
        assert t.acquire("s3") is False  # budget exhausted

        # Advance past the window
        fake_time[0] = 161.0
        assert t.acquire("s4") is True  # old entries evicted


def test_workspace_budget_remaining_reports_correctly():
    t = _make_throttle(min_interval=0.0, budget=5)
    fake_time = [100.0]

    def now():
        return fake_time[0]

    with patch("cora.slack_update_throttle.time.monotonic", side_effect=now):
        assert t.workspace_budget_remaining() == 5
        t.acquire("s1")
        assert t.workspace_budget_remaining() == 4
        t.acquire("s2")
        t.acquire("s3")
        assert t.workspace_budget_remaining() == 2


# ---- force_acquire ----


def test_force_acquire_bypasses_interval_gate():
    t = _make_throttle(min_interval=5.0)
    fake_time = [100.0]

    def now():
        return fake_time[0]

    with patch("cora.slack_update_throttle.time.monotonic", side_effect=now):
        assert t.acquire("s1") is True
        # Normal acquire would be blocked by the interval
        fake_time[0] = 100.1
        assert t.acquire("s1") is False
        # force_acquire should succeed
        assert t.force_acquire("s1") is True


def test_force_acquire_returns_false_when_over_budget_but_still_acquires():
    """Final updates over-budget return False to signal the situation but still
    consume a token + record success. Caller decides whether to retry."""
    t = _make_throttle(min_interval=0.0, budget=1)
    fake_time = [100.0]

    def now():
        return fake_time[0]

    with patch("cora.slack_update_throttle.time.monotonic", side_effect=now):
        assert t.acquire("s1") is True
        # Force on s2 — over budget, returns False but state advances
        assert t.force_acquire("s2") is False
        # Verify the next force from s2 would still try (advances state)
        assert t.workspace_budget_remaining() == 0


# ---- release_stream ----


def test_release_stream_returns_skipped_count():
    t = _make_throttle(min_interval=1.0)
    fake_time = [100.0]

    def now():
        return fake_time[0]

    with patch("cora.slack_update_throttle.time.monotonic", side_effect=now):
        assert t.acquire("s1") is True
        fake_time[0] = 100.1
        # 5 skipped attempts
        for _ in range(5):
            assert t.acquire("s1") is False

        stats = t.release_stream("s1")
        assert stats["skipped_count"] == 5


def test_release_unknown_stream_does_not_crash():
    t = _make_throttle()
    stats = t.release_stream("never-existed")
    assert stats["skipped_count"] == 0


# ---- Thread safety smoke test ----


def test_concurrent_acquires_respect_budget():
    """Hammer the throttle with many threads, verify total acquires never exceed budget."""
    t = _make_throttle(min_interval=0.0, budget=20)
    successful = []
    lock = threading.Lock()

    def worker(i):
        for _ in range(10):
            if t.acquire(f"s-{i}"):
                with lock:
                    successful.append(i)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    # Total acquires must be <= budget. Some streams may have acquired multiple
    # times (different stream ids have independent per-stream gates and
    # interval=0 means no per-stream delay), but the workspace budget caps the total.
    assert len(successful) <= 20
