"""Tests for OSN shift scheduling — algorithm, DB, and handler parsing."""

from __future__ import annotations

import json
import uuid
from datetime import date
from unittest.mock import MagicMock, patch

import pytest


# ── DB / model tests ──────────────────────────────────────────────────────────

def test_employee_display_locations():
    from cora.tools.osn_shift_db import Employee
    emp = Employee(
        slack_user_id="U001",
        name="Alice",
        tier="high",
        preferred_locations=["GW", "GF"],
    )
    display = emp.display_locations()
    assert "Gilbert & Warner" in display
    assert "Greenfield & 60" in display


def test_upsert_and_get_employee(tmp_path, monkeypatch):
    import cora.tools.osn_shift_db as db
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "test.db")
    db.init_db()

    emp = db.Employee(
        slack_user_id="U123",
        name="Bob Smith",
        tier="mid",
        preferred_locations=["GM", "VVP"],
    )
    db.upsert_employee(emp)
    fetched = db.get_employee("U123")
    assert fetched is not None
    assert fetched.name == "Bob Smith"
    assert fetched.tier == "mid"
    assert "GM" in fetched.preferred_locations


def test_upsert_employee_update(tmp_path, monkeypatch):
    import cora.tools.osn_shift_db as db
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "test.db")
    db.init_db()

    emp = db.Employee("U999", "Carol", "low", ["GW"])
    db.upsert_employee(emp)
    emp2 = db.Employee("U999", "Carol Updated", "mid", ["GW", "GM"])
    db.upsert_employee(emp2)

    fetched = db.get_employee("U999")
    assert fetched.name == "Carol Updated"
    assert fetched.tier == "mid"
    assert "GM" in fetched.preferred_locations


def test_availability_upsert_and_get(tmp_path, monkeypatch):
    import cora.tools.osn_shift_db as db
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "test.db")
    db.init_db()

    avail = db.Availability(
        availability_id=str(uuid.uuid4()),
        slack_user_id="U123",
        week_start="2026-06-01",
        days={
            "mon": {"slots": ["open"], "locations": ["GW"]},
            "wed": {"slots": ["open", "close"], "locations": ["GW", "GM"]},
        },
    )
    db.upsert_availability(avail)
    fetched = db.get_availability("U123", "2026-06-01")
    assert fetched is not None
    assert "mon" in fetched.days
    assert "open" in fetched.days["mon"]["slots"]


def test_dm_state_roundtrip(tmp_path, monkeypatch):
    import cora.tools.osn_shift_db as db
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "test.db")
    db.init_db()

    db.set_dm_state("U555", {"step": "asking_days", "week_start": "2026-06-01"})
    state = db.get_dm_state("U555")
    assert state["step"] == "asking_days"

    db.clear_dm_state("U555")
    assert db.get_dm_state("U555") == {}


# ── Scheduling algorithm tests ────────────────────────────────────────────────

def test_schedule_no_two_lows(tmp_path, monkeypatch):
    import cora.tools.osn_shift_db as db
    import cora.tools.osn_shift_scheduler as sched

    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "test.db")
    db.init_db()

    # Add two LOW employees and one HIGH
    db.upsert_employee(db.Employee("low1", "Low One", "low", ["GW"]))
    db.upsert_employee(db.Employee("low2", "Low Two", "low", ["GW"]))
    db.upsert_employee(db.Employee("high1", "High One", "high", ["GW"]))

    week = "2026-06-01"
    for uid in ("low1", "low2", "high1"):
        db.upsert_availability(db.Availability(
            availability_id=str(uuid.uuid4()),
            slack_user_id=uid,
            week_start=week,
            days={"mon": {"slots": ["open"], "locations": ["GW"]}},
        ))

    schedule, warnings = sched.generate_schedule(week)
    assigned = schedule.shifts.get("mon", {}).get("GW", {}).get("open", [])

    # Should NOT be [low1, low2] — must include the high-tier employee
    if len(assigned) == 2:
        tiers = {db.get_employee(uid).tier for uid in assigned}
        assert "low" not in tiers or "high" in tiers or "mid" in tiers, \
            "Two LOW employees were scheduled together"


def test_schedule_only_lows_leaves_shift_empty(tmp_path, monkeypatch):
    import cora.tools.osn_shift_db as db
    import cora.tools.osn_shift_scheduler as sched

    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "test.db")
    db.init_db()

    db.upsert_employee(db.Employee("low1", "Low A", "low", ["GW"]))
    db.upsert_employee(db.Employee("low2", "Low B", "low", ["GW"]))

    week = "2026-06-01"
    for uid in ("low1", "low2"):
        db.upsert_availability(db.Availability(
            availability_id=str(uuid.uuid4()),
            slack_user_id=uid,
            week_start=week,
            days={"mon": {"slots": ["open"], "locations": ["GW"]}},
        ))

    schedule, warnings = sched.generate_schedule(week)
    assigned = schedule.shifts.get("mon", {}).get("GW", {}).get("open", [])
    assert assigned == [], "Should not assign two LOW employees"
    assert any("LOW" in w for w in warnings), "Should warn about LOW-only shift"


def test_schedule_respects_location_preference(tmp_path, monkeypatch):
    import cora.tools.osn_shift_db as db
    import cora.tools.osn_shift_scheduler as sched

    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "test.db")
    db.init_db()

    # Employee only works GW
    db.upsert_employee(db.Employee("mid1", "Mid One", "mid", ["GW"]))

    week = "2026-06-01"
    db.upsert_availability(db.Availability(
        availability_id=str(uuid.uuid4()),
        slack_user_id="mid1",
        week_start=week,
        days={"mon": {"slots": ["open"], "locations": ["GW"]}},
    ))

    schedule, _ = sched.generate_schedule(week)
    # Should NOT appear at GM
    gm_open = schedule.shifts.get("mon", {}).get("GM", {}).get("open", [])
    assert "mid1" not in gm_open


def test_next_monday():
    from cora.tools.osn_shift_scheduler import next_monday
    from datetime import date
    # Wednesday → next Monday
    result = next_monday(date(2026, 5, 27))  # Wednesday
    assert result == "2026-06-01"


def test_current_week_monday():
    from cora.tools.osn_shift_scheduler import current_week_monday
    from datetime import date
    result = current_week_monday(date(2026, 5, 27))  # Wednesday
    assert result == "2026-05-25"


# ── Handler parsing tests ─────────────────────────────────────────────────────

def test_parse_days_abbreviations():
    from cora.tools.osn_shift_handler import _parse_days
    result = _parse_days("mon, wed, fri, sat")
    assert result == ["mon", "wed", "fri", "sat"]


def test_parse_days_full_names():
    from cora.tools.osn_shift_handler import _parse_days
    result = _parse_days("Monday and Thursday")
    assert "mon" in result
    assert "thu" in result


def test_parse_slots_open():
    from cora.tools.osn_shift_handler import _parse_slots
    assert _parse_slots("open") == ["open"]
    assert _parse_slots("opening shift") == ["open"]


def test_parse_slots_both():
    from cora.tools.osn_shift_handler import _parse_slots
    result = _parse_slots("both")
    assert set(result) == {"open", "close"}


def test_parse_locations_codes():
    from cora.tools.osn_shift_handler import _parse_locations
    result = _parse_locations("GW and VVP")
    assert "GW" in result
    assert "VVP" in result


def test_parse_locations_all():
    from cora.tools.osn_shift_handler import _parse_locations
    result = _parse_locations("all locations")
    assert set(result) == {"GW", "GM", "GF", "VVP"}


def test_is_confirmation():
    from cora.tools.osn_shift_handler import _is_confirmation
    assert _is_confirmation("yes, looks good")
    assert _is_confirmation("confirm")
    assert not _is_confirmation("no, change it")


def test_is_negative():
    from cora.tools.osn_shift_handler import _is_negative
    assert _is_negative("none")
    assert _is_negative("I'm unavailable this week")
    assert not _is_negative("monday and tuesday")


# ── Format tests ──────────────────────────────────────────────────────────────

def test_format_schedule_slack_empty(tmp_path, monkeypatch):
    import cora.tools.osn_shift_db as db
    import cora.tools.osn_shift_scheduler as sched_mod
    from cora.tools.osn_shift_db import Schedule

    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "test.db")
    db.init_db()

    s = Schedule(
        schedule_id="test-id",
        week_start="2026-06-01",
        shifts={},
    )
    text = sched_mod.format_schedule_slack(s, {})
    assert "2026-06-01" in text
    assert "No shifts" in text


def test_format_employee_schedule_no_shifts(tmp_path, monkeypatch):
    import cora.tools.osn_shift_db as db
    import cora.tools.osn_shift_scheduler as sched_mod
    from cora.tools.osn_shift_db import Schedule, Employee

    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "test.db")
    db.init_db()

    emp = Employee("U001", "Dana", "high", ["GW"])
    employees = {"U001": emp}

    s = Schedule(
        schedule_id="test-id-2",
        week_start="2026-06-01",
        shifts={},
    )
    text = sched_mod.format_employee_schedule_slack(s, "U001", employees)
    assert "Dana" in text
    assert "no shifts" in text.lower()
