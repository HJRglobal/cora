"""Unit tests for rate_limiter."""

import cora.rate_limiter as rl_module
from cora.rate_limiter import RateLimiter


def test_within_limits_allows():
    rl = RateLimiter()
    for _ in range(9):
        assert rl.check("u1", "c1") == (True, None)


def test_user_cap_hits_at_10():
    rl = RateLimiter()
    for _ in range(10):
        assert rl.check("u1", "c1") == (True, None)
    assert rl.check("u1", "c1") == (False, "user")


def test_channel_cap_hits_at_50():
    rl = RateLimiter()
    for i in range(50):
        assert rl.check(f"user_{i}", "shared_channel") == (True, None)
    assert rl.check("user_50", "shared_channel") == (False, "channel")


def test_window_expiry(monkeypatch):
    rl = RateLimiter()
    current_time = [0.0]
    monkeypatch.setattr(rl_module.time, "monotonic", lambda: current_time[0])

    for _ in range(10):
        rl.check("u1", "c1")

    assert rl.check("u1", "c1") == (False, "user")

    current_time[0] = 3601.0

    assert rl.check("u1", "c1") == (True, None)


def test_user_cap_checked_before_channel_cap():
    rl = RateLimiter()

    # Exhaust user cap for u_main across throwaway channels (don't pollute main_channel)
    for i in range(10):
        rl.check("u_main", f"other_{i}")

    # Exhaust channel cap on main_channel using distinct users
    for i in range(50):
        rl.check(f"u_{i}", "main_channel")

    # Both caps would fire — user must be reported first
    assert rl.check("u_main", "main_channel") == (False, "user")
