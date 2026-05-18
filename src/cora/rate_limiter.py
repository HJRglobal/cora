"""Sliding-window rate limiter for app_mention events."""

import time
from collections import deque
from threading import Lock

_USER_LIMIT = 10
_CHANNEL_LIMIT = 50
_WINDOW = 3600  # seconds


class RateLimiter:
    def __init__(self) -> None:
        self._user: dict[str, deque[float]] = {}
        self._channel: dict[str, deque[float]] = {}
        self._lock = Lock()

    @staticmethod
    def _evict(dq: deque[float], now: float) -> None:
        while dq and now - dq[0] > _WINDOW:
            dq.popleft()

    def check(self, user_id: str, channel_id: str) -> tuple[bool, str | None]:
        """Return (True, None) if allowed, (False, "user"|"channel") if capped.

        On allow, records the timestamp in both counters before returning.
        User cap is evaluated before channel cap.
        Thread-safe.
        """
        now = time.monotonic()
        with self._lock:
            user_dq = self._user.setdefault(user_id, deque())
            channel_dq = self._channel.setdefault(channel_id, deque())

            self._evict(user_dq, now)
            self._evict(channel_dq, now)

            if len(user_dq) >= _USER_LIMIT:
                return (False, "user")
            if len(channel_dq) >= _CHANNEL_LIMIT:
                return (False, "channel")

            user_dq.append(now)
            channel_dq.append(now)
            return (True, None)


_LIMITER = RateLimiter()


def check(user_id: str, channel_id: str) -> tuple[bool, str | None]:
    return _LIMITER.check(user_id, channel_id)
