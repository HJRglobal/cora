"""SQLite persistence for the OSN shift scheduling system.

Tables (in data/osn_shifts.db):
  osn_employees   — employee profiles: Slack user ID, name, tier, location prefs
  osn_availability — weekly availability submissions per employee
  osn_schedules   — generated (and approved/published) schedule records

Tier values: "high" | "mid" | "low"
Store codes: "GW" | "GM" | "GF" | "VVP"
Shift slots: "open" | "close" | "both"
Schedule status: "draft" | "pending_approval" | "approved" | "published"
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_DB_PATH = Path(__file__).parent.parent.parent.parent / "data" / "osn_shifts.db"

VALID_TIERS = ("high", "mid", "low")
VALID_STORES = ("GW", "GM", "GF", "VVP")
STORE_NAMES = {
    "GW": "Gilbert & Warner",
    "GM": "Gilbert & McKellips",
    "GF": "Greenfield & 60",
    "VVP": "Val Vista & Pecos",
}
VALID_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
VALID_SLOTS = ("open", "close", "both")
DAY_NAMES = {
    "mon": "Monday", "tue": "Tuesday", "wed": "Wednesday",
    "thu": "Thursday", "fri": "Friday", "sat": "Saturday", "sun": "Sunday",
}
SCHEDULE_STATUSES = ("draft", "pending_approval", "approved", "published")


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class Employee:
    slack_user_id: str
    name: str
    tier: str                          # "high" | "mid" | "low"
    preferred_locations: list[str]     # subset of VALID_STORES
    is_active: bool = True
    created_at: int = field(default_factory=lambda: int(time.time()))

    def display_locations(self) -> str:
        return ", ".join(
            f"{code} ({STORE_NAMES[code]})" for code in self.preferred_locations
            if code in STORE_NAMES
        )


@dataclass
class Availability:
    """One week's availability for one employee."""
    availability_id: str
    slack_user_id: str
    week_start: str                    # ISO date string: YYYY-MM-DD (always Monday)
    # days_availability: {"mon": {"slots": ["open","close"], "locations": ["GW","GM"]}, ...}
    days: dict[str, dict]
    submitted_at: int = field(default_factory=lambda: int(time.time()))
    updated_at: int = field(default_factory=lambda: int(time.time()))


@dataclass
class Schedule:
    schedule_id: str
    week_start: str                    # YYYY-MM-DD
    # shifts structure:
    # {"mon": {"GW": {"open": ["user1"], "close": ["user2","user3"]}, ...}, ...}
    shifts: dict
    status: str = "draft"
    created_at: int = field(default_factory=lambda: int(time.time()))
    approved_by: Optional[str] = None
    approved_at: Optional[int] = None
    notes: str = ""


# ── Connection + schema ───────────────────────────────────────────────────────

def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    conn = _connect()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS osn_employees (
            slack_user_id       TEXT PRIMARY KEY,
            name                TEXT NOT NULL,
            tier                TEXT NOT NULL,
            preferred_locations TEXT NOT NULL DEFAULT '[]',
            is_active           INTEGER NOT NULL DEFAULT 1,
            created_at          INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS osn_availability (
            availability_id TEXT PRIMARY KEY,
            slack_user_id   TEXT NOT NULL,
            week_start      TEXT NOT NULL,
            days_json       TEXT NOT NULL DEFAULT '{}',
            submitted_at    INTEGER NOT NULL,
            updated_at      INTEGER NOT NULL,
            UNIQUE(slack_user_id, week_start)
        );

        CREATE INDEX IF NOT EXISTS idx_avail_week ON osn_availability(week_start);
        CREATE INDEX IF NOT EXISTS idx_avail_user ON osn_availability(slack_user_id);

        CREATE TABLE IF NOT EXISTS osn_schedules (
            schedule_id TEXT PRIMARY KEY,
            week_start  TEXT NOT NULL,
            shifts_json TEXT NOT NULL DEFAULT '{}',
            status      TEXT NOT NULL DEFAULT 'draft',
            created_at  INTEGER NOT NULL,
            approved_by TEXT,
            approved_at INTEGER,
            notes       TEXT NOT NULL DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_sched_week ON osn_schedules(week_start);

        CREATE TABLE IF NOT EXISTS osn_dm_state (
            slack_user_id TEXT PRIMARY KEY,
            state_json    TEXT NOT NULL DEFAULT '{}',
            updated_at    INTEGER NOT NULL
        );
    """)
    conn.commit()
    conn.close()


# ── Employee CRUD ─────────────────────────────────────────────────────────────

def upsert_employee(emp: Employee) -> None:
    conn = _connect()
    conn.execute(
        """
        INSERT INTO osn_employees (slack_user_id, name, tier, preferred_locations, is_active, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(slack_user_id) DO UPDATE SET
            name = excluded.name,
            tier = excluded.tier,
            preferred_locations = excluded.preferred_locations,
            is_active = excluded.is_active
        """,
        (
            emp.slack_user_id,
            emp.name,
            emp.tier,
            json.dumps(emp.preferred_locations),
            int(emp.is_active),
            emp.created_at,
        ),
    )
    conn.commit()
    conn.close()


def get_employee(slack_user_id: str) -> Optional[Employee]:
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM osn_employees WHERE slack_user_id = ?", (slack_user_id,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    return _row_to_employee(row)


def get_all_active_employees() -> list[Employee]:
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM osn_employees WHERE is_active = 1 ORDER BY name"
    ).fetchall()
    conn.close()
    return [_row_to_employee(r) for r in rows]


def _row_to_employee(row: sqlite3.Row) -> Employee:
    return Employee(
        slack_user_id=row["slack_user_id"],
        name=row["name"],
        tier=row["tier"],
        preferred_locations=json.loads(row["preferred_locations"]),
        is_active=bool(row["is_active"]),
        created_at=row["created_at"],
    )


# ── Availability CRUD ─────────────────────────────────────────────────────────

def upsert_availability(avail: Availability) -> None:
    now = int(time.time())
    conn = _connect()
    conn.execute(
        """
        INSERT INTO osn_availability
            (availability_id, slack_user_id, week_start, days_json, submitted_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(slack_user_id, week_start) DO UPDATE SET
            days_json  = excluded.days_json,
            updated_at = excluded.updated_at
        """,
        (
            avail.availability_id,
            avail.slack_user_id,
            avail.week_start,
            json.dumps(avail.days),
            avail.submitted_at,
            now,
        ),
    )
    conn.commit()
    conn.close()


def get_availability(slack_user_id: str, week_start: str) -> Optional[Availability]:
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM osn_availability WHERE slack_user_id = ? AND week_start = ?",
        (slack_user_id, week_start),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return _row_to_avail(row)


def get_week_availability(week_start: str) -> list[Availability]:
    """All availability submissions for a given week."""
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM osn_availability WHERE week_start = ?", (week_start,)
    ).fetchall()
    conn.close()
    return [_row_to_avail(r) for r in rows]


def _row_to_avail(row: sqlite3.Row) -> Availability:
    return Availability(
        availability_id=row["availability_id"],
        slack_user_id=row["slack_user_id"],
        week_start=row["week_start"],
        days=json.loads(row["days_json"]),
        submitted_at=row["submitted_at"],
        updated_at=row["updated_at"],
    )


# ── Schedule CRUD ─────────────────────────────────────────────────────────────

def save_schedule(sched: Schedule) -> None:
    conn = _connect()
    conn.execute(
        """
        INSERT INTO osn_schedules
            (schedule_id, week_start, shifts_json, status, created_at, approved_by, approved_at, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(schedule_id) DO UPDATE SET
            shifts_json = excluded.shifts_json,
            status      = excluded.status,
            approved_by = excluded.approved_by,
            approved_at = excluded.approved_at,
            notes       = excluded.notes
        """,
        (
            sched.schedule_id,
            sched.week_start,
            json.dumps(sched.shifts),
            sched.status,
            sched.created_at,
            sched.approved_by,
            sched.approved_at,
            sched.notes,
        ),
    )
    conn.commit()
    conn.close()


def get_schedule(schedule_id: str) -> Optional[Schedule]:
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM osn_schedules WHERE schedule_id = ?", (schedule_id,)
    ).fetchone()
    conn.close()
    return _row_to_schedule(row) if row else None


def get_latest_schedule_for_week(week_start: str) -> Optional[Schedule]:
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM osn_schedules WHERE week_start = ? ORDER BY created_at DESC LIMIT 1",
        (week_start,),
    ).fetchone()
    conn.close()
    return _row_to_schedule(row) if row else None


def approve_schedule(schedule_id: str, approved_by: str) -> bool:
    conn = _connect()
    result = conn.execute(
        "UPDATE osn_schedules SET status = 'approved', approved_by = ?, approved_at = ? WHERE schedule_id = ?",
        (approved_by, int(time.time()), schedule_id),
    )
    conn.commit()
    conn.close()
    return result.rowcount > 0


def publish_schedule(schedule_id: str) -> bool:
    conn = _connect()
    result = conn.execute(
        "UPDATE osn_schedules SET status = 'published' WHERE schedule_id = ?",
        (schedule_id,),
    )
    conn.commit()
    conn.close()
    return result.rowcount > 0


def _row_to_schedule(row: sqlite3.Row) -> Schedule:
    return Schedule(
        schedule_id=row["schedule_id"],
        week_start=row["week_start"],
        shifts=json.loads(row["shifts_json"]),
        status=row["status"],
        created_at=row["created_at"],
        approved_by=row["approved_by"],
        approved_at=row["approved_at"],
        notes=row["notes"] or "",
    )


# ── DM conversation state ─────────────────────────────────────────────────────
# Used by the DM handler to track where each employee is in the multi-step
# availability submission flow.

def get_dm_state(slack_user_id: str) -> dict:
    conn = _connect()
    row = conn.execute(
        "SELECT state_json FROM osn_dm_state WHERE slack_user_id = ?", (slack_user_id,)
    ).fetchone()
    conn.close()
    return json.loads(row["state_json"]) if row else {}


def set_dm_state(slack_user_id: str, state: dict) -> None:
    conn = _connect()
    conn.execute(
        """
        INSERT INTO osn_dm_state (slack_user_id, state_json, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(slack_user_id) DO UPDATE SET
            state_json = excluded.state_json,
            updated_at = excluded.updated_at
        """,
        (slack_user_id, json.dumps(state), int(time.time())),
    )
    conn.commit()
    conn.close()


def clear_dm_state(slack_user_id: str) -> None:
    conn = _connect()
    conn.execute("DELETE FROM osn_dm_state WHERE slack_user_id = ?", (slack_user_id,))
    conn.commit()
    conn.close()


# ── Helpers ───────────────────────────────────────────────────────────────────

def new_id() -> str:
    return str(uuid.uuid4())


# Initialise schema on import (idempotent)
try:
    init_db()
except Exception as _e:
    log.warning("osn_shift_db: init_db failed: %s", _e)
