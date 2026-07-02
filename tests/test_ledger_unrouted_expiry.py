"""WS-4 ledger boundedness: _auto_expire_unrouted_operational.

An OPERATIONAL item still PENDING and never DM'd/routed after 14 days expires
as DISMISSED / resolved_reason="expired_unrouted". Knowledge items keep the
D-051 never-expire-unseen guarantee; unknown update_types are left alone.
"""

from datetime import datetime, timedelta, timezone

import scripts.run_knowledge_review as rkr


def _now():
    return datetime.now(timezone.utc)


def _entry(update_type="asana_task", dm_ts="", age_days=20, state="PENDING",
           payload=None):
    return {
        "update_id": f"x-{update_type}-{age_days}",
        "update_type": update_type,
        "payload": payload,
        "state": state,
        "dm_message_ts": dm_ts,
        "proposed_at": (_now() - timedelta(days=age_days)).isoformat(),
        "resolved_at": None,
    }


def _run(entries, days=None):
    now = _now()
    cutoff = now - timedelta(days=days or rkr._OPERATIONAL_UNROUTED_EXPIRY_DAYS)
    return rkr._auto_expire_unrouted_operational(entries, cutoff, now)


def test_old_unrouted_operational_expires():
    e = _entry("asana_task", age_days=20)
    assert _run([e]) == 1
    assert e["state"] == "DISMISSED"
    assert e["resolved_reason"] == "expired_unrouted"
    assert e["resolved_at"]


def test_every_operational_type_covered():
    entries = [_entry(t, age_days=20) for t in sorted(rkr._OPERATIONAL_TYPES)]
    assert _run(entries) == len(rkr._OPERATIONAL_TYPES)


def test_recent_operational_survives():
    e = _entry("hubspot_note", age_days=3)
    assert _run([e]) == 0
    assert e["state"] == "PENDING"


def test_knowledge_items_are_exempt():
    # D-051: a never-DM'd knowledge item must never be auto-dismissed on age.
    entries = [
        _entry("known_answer", age_days=60),
        _entry("efficiency", age_days=60),
        _entry("generic", age_days=60, payload={"source": "info-for-cora"}),
    ]
    assert _run(entries) == 0
    assert all(e["state"] == "PENDING" for e in entries)


def test_drive_generic_is_operational_and_expires():
    # A drive_extractor person fact is generic WITHOUT the info-for-cora
    # source marker -- it is operational and expires.
    e = _entry("generic", age_days=20, payload={"fact_type": "person"})
    assert _run([e]) == 1
    assert e["resolved_reason"] == "expired_unrouted"


def test_already_dmd_item_left_for_the_other_expiry():
    # dm_message_ts set = it reached a human; the SEEN-expiry path
    # (_auto_dismiss_stale_pending) owns that lifecycle, not this one.
    e = _entry("asana_task", dm_ts="1700.1", age_days=60)
    assert _run([e]) == 0
    assert e["state"] == "PENDING"


def test_unknown_update_type_left_alone():
    e = _entry("mystery_type", age_days=60)
    assert _run([e]) == 0
    assert e["state"] == "PENDING"


def test_non_pending_untouched():
    e = _entry("asana_task", age_days=60, state="DISMISSED")
    assert _run([e]) == 0


def test_malformed_proposed_at_survives():
    e = _entry("asana_task", age_days=60)
    e["proposed_at"] = "not-a-date"
    assert _run([e]) == 0
    assert e["state"] == "PENDING"
