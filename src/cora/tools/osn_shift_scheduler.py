"""OSN shift scheduling algorithm.

Inputs:
  - Employee profiles (tier: high/mid/low, preferred locations)
  - Availability submissions for a given week

Output:
  A Schedule with assigned employees per day/location/slot.

Pairing constraint:
  - A LOW-tier employee must never be the sole employee at a shift, AND
    two LOW-tier employees must never be paired together at the same shift.
  - Valid pairs: HIGH+HIGH, HIGH+MID, HIGH+LOW, MID+MID, MID+LOW
  - Invalid pairs: LOW+LOW (or LOW alone when >1 employee needed)

Coverage target:
  - Each shift slot (open/close) at each location targets 2 employees.
    If only 1 is available and they're not LOW, 1 is acceptable.
    Unfilledable slots are noted in the schedule warnings.

The scheduler is deterministic given the same inputs (sorted by user ID as
tiebreaker) so repeat calls produce the same result.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from .osn_shift_db import (
    Availability,
    Employee,
    Schedule,
    VALID_DAYS,
    VALID_SLOTS,
    VALID_STORES,
    DAY_NAMES,
    STORE_NAMES,
    get_all_active_employees,
    get_week_availability,
    get_employee,
    save_schedule,
)

log = logging.getLogger(__name__)

_TIER_RANK = {"high": 3, "mid": 2, "low": 1}


# ── Public entry point ────────────────────────────────────────────────────────

def generate_schedule(week_start: str) -> tuple[Schedule, list[str]]:
    """Generate a draft schedule for the given week (YYYY-MM-DD, must be Monday).

    Returns (schedule, warnings) where warnings is a list of human-readable
    strings about unresolvable constraints or unfilled shifts.
    """
    employees = {e.slack_user_id: e for e in get_all_active_employees()}
    availability_list = get_week_availability(week_start)
    avail_map = {a.slack_user_id: a for a in availability_list}

    shifts: dict = {}  # {day: {store: {slot: [user_ids]}}}
    warnings: list[str] = []

    for day in VALID_DAYS:
        shifts[day] = {}
        for store in VALID_STORES:
            shifts[day][store] = {}
            for slot in ("open", "close"):
                # Collect candidates: available this day+slot+store
                candidates = _candidates(employees, avail_map, day, slot, store)
                assigned, warn = _assign_shift(candidates, employees, day, store, slot)
                shifts[day][store][slot] = assigned
                warnings.extend(warn)

    sched = Schedule(
        schedule_id=str(uuid.uuid4()),
        week_start=week_start,
        shifts=shifts,
        status="draft",
        created_at=int(time.time()),
    )
    save_schedule(sched)
    log.info(
        "shift_scheduler: generated schedule %s for week=%s warnings=%d",
        sched.schedule_id[:8], week_start, len(warnings),
    )
    return sched, warnings


# ── Candidate selection ───────────────────────────────────────────────────────

def _candidates(
    employees: dict[str, Employee],
    avail_map: dict[str, Availability],
    day: str,
    slot: str,
    store: str,
) -> list[Employee]:
    """Return employees available for a specific day/slot/store."""
    result = []
    for uid, emp in employees.items():
        if store not in emp.preferred_locations:
            continue
        avail = avail_map.get(uid)
        if not avail:
            continue
        day_data = avail.days.get(day, {})
        if not day_data:
            continue
        avail_slots = day_data.get("slots", [])
        avail_locs = day_data.get("locations", [])
        if slot not in avail_slots and "both" not in avail_slots:
            continue
        if store not in avail_locs:
            continue
        result.append(emp)

    # Deterministic order: tier (desc) then name, then user_id as tiebreaker
    result.sort(key=lambda e: (-_TIER_RANK.get(e.tier, 0), e.name, e.slack_user_id))
    return result


# ── Assignment logic ──────────────────────────────────────────────────────────

def _assign_shift(
    candidates: list[Employee],
    employees: dict[str, Employee],
    day: str,
    store: str,
    slot: str,
) -> tuple[list[str], list[str]]:
    """Pick the best ≤2 employees for this shift, honouring tier constraints.

    Returns (assigned_user_ids, warnings).
    """
    warnings: list[str] = []
    label = f"{DAY_NAMES[day]} {slot} @ {STORE_NAMES.get(store, store)}"

    if not candidates:
        return [], []  # No one available — not a warning, just empty

    highs = [e for e in candidates if e.tier == "high"]
    mids  = [e for e in candidates if e.tier == "mid"]
    lows  = [e for e in candidates if e.tier == "low"]

    # Try to fill two slots with the best valid pair
    pair = _best_pair(highs, mids, lows)
    if pair:
        return [e.slack_user_id for e in pair], warnings

    # Only one person available
    single = candidates[0]
    if single.tier == "low":
        warnings.append(
            f"{label}: only LOW-tier employee available ({single.name}) — shift left unstaffed."
        )
        return [], warnings

    # One non-low employee is acceptable coverage
    warnings.append(f"{label}: only 1 employee available ({single.name}).")
    return [single.slack_user_id], warnings


def _best_pair(
    highs: list[Employee],
    mids: list[Employee],
    lows: list[Employee],
) -> Optional[list[Employee]]:
    """Return the best 2-person combination, or None if < 2 non-LOW+LOW options."""
    all_non_low = highs + mids
    if len(all_non_low) >= 2:
        return all_non_low[:2]
    if len(all_non_low) == 1 and lows:
        return [all_non_low[0], lows[0]]
    # Only lows available, or total < 2
    return None


# ── Formatting ────────────────────────────────────────────────────────────────

def format_schedule_slack(sched: Schedule, employees: Optional[dict] = None) -> str:
    """Return a Slack-formatted string summarising the schedule.

    employees: optional {user_id: Employee} map; loaded from DB if None.
    """
    if employees is None:
        employees = {e.slack_user_id: e for e in get_all_active_employees()}

    lines = [f"*Schedule for week of {sched.week_start}*  (status: `{sched.status}`)\n"]

    has_any = False
    for day in VALID_DAYS:
        day_data = sched.shifts.get(day, {})
        day_lines = []
        for store in VALID_STORES:
            store_data = day_data.get(store, {})
            for slot in ("open", "close"):
                assigned = store_data.get(slot, [])
                if not assigned:
                    continue
                names = []
                for uid in assigned:
                    emp = employees.get(uid)
                    names.append(emp.name if emp else uid)
                day_lines.append(
                    f"  • {STORE_NAMES.get(store, store)} *{slot.capitalize()}*: {', '.join(names)}"
                )
        if day_lines:
            has_any = True
            lines.append(f"*{DAY_NAMES[day]}*")
            lines.extend(day_lines)
            lines.append("")

    if not has_any:
        lines.append("_No shifts assigned — not enough availability submitted yet._")

    return "\n".join(lines)


def format_employee_schedule_slack(
    sched: Schedule,
    slack_user_id: str,
    employees: Optional[dict] = None,
) -> str:
    """Return a personalised schedule message for one employee."""
    if employees is None:
        employees = {e.slack_user_id: e for e in get_all_active_employees()}

    emp = employees.get(slack_user_id)
    name = emp.name if emp else "there"

    lines = [f"Hi {name}! Here's your schedule for the week of *{sched.week_start}*:\n"]
    found = False

    for day in VALID_DAYS:
        day_data = sched.shifts.get(day, {})
        for store in VALID_STORES:
            store_data = day_data.get(store, {})
            for slot in ("open", "close"):
                assigned = store_data.get(slot, [])
                if slack_user_id in assigned:
                    found = True
                    # List co-workers
                    co = [
                        (employees[uid].name if uid in employees else uid)
                        for uid in assigned
                        if uid != slack_user_id
                    ]
                    co_str = f" (with {', '.join(co)})" if co else " (solo)"
                    lines.append(
                        f"• *{DAY_NAMES[day]}* — {STORE_NAMES.get(store, store)}, "
                        f"{slot.capitalize()} shift{co_str}"
                    )

    if not found:
        lines.append("_You have no shifts assigned this week._")

    return "\n".join(lines)


def next_monday(from_date: Optional[date] = None) -> str:
    """Return the ISO date string of the next Monday from today (or from_date)."""
    d = from_date or date.today()
    days_ahead = 7 - d.weekday()  # Monday is 0
    if days_ahead == 7:
        days_ahead = 7  # still go to *next* Monday, not today
    return (d + timedelta(days=days_ahead)).isoformat()


def current_week_monday(from_date: Optional[date] = None) -> str:
    """Return the ISO date string of the Monday of the current week."""
    d = from_date or date.today()
    return (d - timedelta(days=d.weekday())).isoformat()
