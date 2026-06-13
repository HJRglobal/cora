"""Tests for run_knowledge_review._auto_dismiss_stale_pending (D1 fix, 2026-06-13).

A PENDING entry is auto-dismissed ONLY once it has been DM'd to Harrison
(dm_message_ts set) and left unreacted past 48h. A never-DM'd entry must survive
(Step 2 DMs it this run) so an #info-for-cora note posted right before a >48h gap
(Friday evening -> Monday 7am review) is not silently dropped before he sees it.
"""

from datetime import datetime, timedelta, timezone

import scripts.run_knowledge_review as rkr


def _now():
    return datetime.now(timezone.utc)


def _entry(dm_ts="", age_hours=100, state="PENDING"):
    return {
        "update_id": "x",
        "state": state,
        "dm_message_ts": dm_ts,
        "proposed_at": (_now() - timedelta(hours=age_hours)).isoformat(),
        "resolved_at": None,
    }


def test_dmd_and_stale_is_dismissed():
    now = _now()
    e = _entry(dm_ts="1700.1", age_hours=100)
    assert rkr._auto_dismiss_stale_pending([e], now - timedelta(hours=48), now) == 1
    assert e["state"] == "DISMISSED" and e["resolved_at"]


def test_never_dmd_is_not_dismissed():
    now = _now()
    e = _entry(dm_ts="", age_hours=100)  # never shown to Harrison
    assert rkr._auto_dismiss_stale_pending([e], now - timedelta(hours=48), now) == 0
    assert e["state"] == "PENDING"


def test_dmd_but_recent_is_not_dismissed():
    now = _now()
    e = _entry(dm_ts="1700.1", age_hours=10)
    assert rkr._auto_dismiss_stale_pending([e], now - timedelta(hours=48), now) == 0
    assert e["state"] == "PENDING"


def test_non_pending_untouched():
    now = _now()
    e = _entry(dm_ts="1700.1", age_hours=100, state="APPROVED")
    assert rkr._auto_dismiss_stale_pending([e], now - timedelta(hours=48), now) == 0
    assert e["state"] == "APPROVED"


def test_bad_proposed_at_ignored():
    now = _now()
    e = {"state": "PENDING", "dm_message_ts": "1.1", "proposed_at": "not-a-date", "resolved_at": None}
    assert rkr._auto_dismiss_stale_pending([e], now - timedelta(hours=48), now) == 0
    assert e["state"] == "PENDING"
