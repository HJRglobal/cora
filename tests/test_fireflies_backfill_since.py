"""Tests for the Fireflies deep-backfill override (--since-days)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).parents[1] / "scripts"


def _load():
    try:
        sys.path.insert(0, str(_SCRIPTS))
        import incremental_sync_fireflies as m
        return m
    except Exception:
        pytest.skip("incremental_sync_fireflies not importable")


class TestResolveLastSync:
    NOW = 1_000_000

    def test_since_days_overrides_watermark(self):
        m = _load()
        # recent watermark present, but --since-days forces a deep reach
        assert m._resolve_last_sync((self.NOW - 100,), 30, 2, self.NOW) == self.NOW - 30 * 86400

    def test_watermark_used_when_no_override(self):
        m = _load()
        assert m._resolve_last_sync((555,), None, 2, self.NOW) == 555

    def test_fallback_when_no_state_and_no_override(self):
        m = _load()
        assert m._resolve_last_sync(None, None, 2, self.NOW) == self.NOW - 2 * 86400

    def test_since_days_zero_is_no_override(self):
        m = _load()
        assert m._resolve_last_sync((555,), 0, 2, self.NOW) == 555

    def test_deep_2000_days(self):
        m = _load()
        assert m._resolve_last_sync((self.NOW - 5,), 2000, 2, self.NOW) == self.NOW - 2000 * 86400
