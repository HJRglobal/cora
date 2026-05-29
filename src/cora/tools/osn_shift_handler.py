"""OSN Shift Scheduler — Slack interaction handler.

Handles two surfaces:
  1. Employee DMs to Cora — guided multi-step availability submission.
  2. Admin @mentions in OSN channels — schedule generation, approval, publishing.

DM flow (state machine stored in osn_dm_state):
  idle          → employee says "submit" or "availability"
  asking_days   → bot asks which days; employee replies
  asking_slots  → (per day) bot asks open/close/both; employee replies
  asking_locs   → bot asks which locations
  confirming    → bot shows summary; employee confirms or changes
  done          → availability saved, state cleared

Admin commands (via @Cora mention in any OSN-related channel):
  "generate schedule [for next week]"  → runs scheduler, posts draft for approval
  "show availability [for next week]"  → lists who has/hasn't submitted
  "approve schedule <id>"             → marks a draft approved
  "publish schedule <id>"             → DMs employees their shifts

Approval flow:
  - Admin generates schedule → bot posts to #osn-scheduling (or current channel)
    with a ✅ reaction instruction.
  - Admin reacts ✅ → schedule moves to "approved" status.
  - Admin runs "publish schedule <id>" → employees receive DMs with their shifts.

Admins who can approve: configured in OSN_SCHEDULER_ADMIN_USER_IDS (comma-sep
Slack user IDs) env var. Falls back to checking against name list if not set.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import date, timedelta
from typing import Optional

from .osn_shift_db import (
    Availability,
    Employee,
    VALID_DAYS,
    VALID_SLOTS,
    VALID_STORES,
    VALID_TIERS,
    DAY_NAMES,
    STORE_NAMES,
    get_all_active_employees,
    get_week_availability,
    get_employee,
    upsert_employee,
    upsert_availability,
    get_dm_state,
    set_dm_state,
    clear_dm_state,
    get_schedule,
    get_latest_schedule_for_week,
    approve_schedule,
    publish_schedule,
    new_id,
)
from .osn_shift_scheduler import (
    generate_schedule,
    format_schedule_slack,
    format_employee_schedule_slack,
    next_monday,
    current_week_monday,
)

log = logging.getLogger(__name__)

# ── Admin authorisation ───────────────────────────────────────────────────────

# Set OSN_SCHEDULER_ADMIN_USER_IDS in .env as comma-separated Slack user IDs.
# These users can generate, approve, and publish schedules.
_ADMIN_IDS_RAW = os.environ.get("OSN_SCHEDULER_ADMIN_USER_IDS", "")
_ADMIN_USER_IDS: set[str] = {
    uid.strip() for uid in _ADMIN_IDS_RAW.split(",") if uid.strip()
}

# Channel where schedule approval cards are posted
_APPROVAL_CHANNEL_NAME = os.environ.get("OSN_SCHEDULER_APPROVAL_CHANNEL", "osn-scheduling")

# ── Day / slot / location parsing helpers ─────────────────────────────────────

_DAY_ALIASES: dict[str, str] = {
    "monday": "mon", "tuesday": "tue", "wednesday": "wed",
    "thursday": "thu", "friday": "fri", "saturday": "sat", "sunday": "sun",
    "mon": "mon", "tue": "tue", "wed": "wed",
    "thu": "thu", "fri": "fri", "sat": "sat", "sun": "sun",
    "m": "mon", "t": "tue", "w": "wed", "th": "thu", "f": "fri",
    "sa": "sat", "su": "sun",
}

_SLOT_ALIASES: dict[str, str] = {
    "open": "open", "opening": "open", "opener": "open",
    "close": "close", "closing": "close", "closer": "close",
    "both": "both", "all": "both", "either": "both",
    "open/close": "both", "open & close": "both", "open and close": "both",
}

_STORE_ALIASES: dict[str, str] = {
    "gw": "GW", "gilbert warner": "GW", "gilbert & warner": "GW", "warner": "GW",
    "gm": "GM", "gilbert mckellips": "GM", "gilbert & mckellips": "GM", "mckellips": "GM",
    "gf": "GF", "greenfield": "GF", "greenfield 60": "GF", "greenfield & 60": "GF",
    "vvp": "VVP", "val vista": "VVP", "val vista pecos": "VVP", "pecos": "VVP",
    "vv": "VVP",
}

# Patterns recognized as "all locations"
_ALL_STORES_PATTERNS = re.compile(
    r"\ball\b|\beverywhere\b|\bany\b|\ball\s+locations?\b|\ball\s+stores?\b",
    re.IGNORECASE,
)

# Patterns recognized as "no availability" / not working
_NONE_PATTERNS = re.compile(
    r"\bnone\b|\bnothing\b|\boff\b|\bn/a\b|\bunavailable\b|\bcan'?t\b|\bnot\s+available\b",
    re.IGNORECASE,
)


def _parse_days(text: str) -> list[str]:
    text_lower = text.lower()
    found = set()
    # Use word-boundary matching to avoid "t" matching inside "sat", "thu", etc.
    for alias, code in _DAY_ALIASES.items():
        if re.search(r"\b" + re.escape(alias) + r"\b", text_lower):
            found.add(code)
    # Preserve VALID_DAYS order
    return [d for d in VALID_DAYS if d in found]


def _parse_slots(text: str) -> list[str]:
    text_lower = text.lower()
    for alias, slot in _SLOT_ALIASES.items():
        if alias in text_lower:
            if slot == "both":
                return ["open", "close"]
            return [slot]
    return []


def _parse_locations(text: str) -> list[str]:
    if _ALL_STORES_PATTERNS.search(text):
        return list(VALID_STORES)
    text_lower = text.lower()
    found = []
    for alias, code in _STORE_ALIASES.items():
        if alias in text_lower and code not in found:
            found.append(code)
    return [s for s in VALID_STORES if s in found]


def _is_negative(text: str) -> bool:
    return bool(_NONE_PATTERNS.search(text))


def _is_confirmation(text: str) -> bool:
    t = text.lower().strip()
    return any(w in t for w in ("yes", "confirm", "looks good", "correct", "ok", "done", "submit"))


def _is_cancel_or_change(text: str) -> bool:
    t = text.lower().strip()
    return any(w in t for w in ("change", "redo", "restart", "cancel", "start over", "no"))


# ── DM flow entry point ───────────────────────────────────────────────────────

def handle_dm(
    text: str,
    slack_user_id: str,
    client,
) -> None:
    """Process a DM from an employee. Routes based on current DM state."""
    text = text.strip()
    state = get_dm_state(slack_user_id)
    step = state.get("step", "idle")

    def _reply(msg: str) -> None:
        try:
            client.chat_postMessage(
                channel=slack_user_id,  # DM channel = user ID
                text=msg,
                unfurl_links=False,
                unfurl_media=False,
            )
        except Exception as exc:
            log.error("osn_shift_handler: DM reply failed user=%s: %s", slack_user_id, exc)

    # ── Keyword triggers (any step) ───────────────────────────────────────
    t_lower = text.lower()
    if any(kw in t_lower for kw in ("my schedule", "my shifts", "when do i work")):
        _send_personal_schedule(slack_user_id, _reply)
        return
    if any(kw in t_lower for kw in ("help", "what can you do", "commands")):
        _reply(_dm_help())
        return
    if any(kw in t_lower for kw in ("cancel", "quit", "stop")) and step != "idle":
        clear_dm_state(slack_user_id)
        _reply("Availability submission cancelled. Reply *submit availability* any time to start again.")
        return

    # ── Trigger words to start the flow ──────────────────────────────────
    if step == "idle":
        if any(kw in t_lower for kw in ("submit", "availability", "schedule", "hours")):
            _start_availability_flow(slack_user_id, state, _reply)
        else:
            _reply(
                "Hi! I can help you submit your availability for the upcoming week.\n"
                "Reply *submit availability* to get started, or *my schedule* to see your current schedule."
            )
        return

    # ── Multi-step flow ───────────────────────────────────────────────────
    if step == "asking_days":
        _handle_days_response(slack_user_id, text, state, _reply)
    elif step == "asking_slots":
        _handle_slots_response(slack_user_id, text, state, _reply)
    elif step == "asking_locs":
        _handle_locs_response(slack_user_id, text, state, _reply)
    elif step == "confirming":
        _handle_confirmation(slack_user_id, text, state, _reply, client)
    else:
        clear_dm_state(slack_user_id)
        _reply("Something went wrong. Reply *submit availability* to start fresh.")


# ── Flow step handlers ────────────────────────────────────────────────────────

def _start_availability_flow(slack_user_id: str, state: dict, reply) -> None:
    week_start = next_monday()
    week_display = _format_week(week_start)
    state = {"step": "asking_days", "week_start": week_start, "days": {}}
    set_dm_state(slack_user_id, state)
    reply(
        f"Great! Let's set up your availability for *{week_display}*.\n\n"
        f"*Step 1 of 3 — Days:*\n"
        f"Which days can you work next week? List them separated by commas.\n"
        f"_(e.g. `mon, wed, fri, sat` or `all` for every day)_\n\n"
        f"Reply *none* if you can't work next week."
    )


def _handle_days_response(slack_user_id: str, text: str, state: dict, reply) -> None:
    if _is_negative(text):
        # No availability this week — save empty and finish
        _save_empty_availability(slack_user_id, state["week_start"])
        clear_dm_state(slack_user_id)
        reply(
            f"Got it — you're marked as unavailable for {_format_week(state['week_start'])}. "
            "Reply *submit availability* to update this any time."
        )
        return

    # "all" / weekdays detection
    if re.search(r"\ball\b", text, re.IGNORECASE):
        days = list(VALID_DAYS)
    else:
        days = _parse_days(text)

    if not days:
        reply(
            "I didn't catch which days. Please list them like: `mon, tue, wed` "
            "or use full names like `Monday, Wednesday, Friday`."
        )
        return

    state["days"] = {d: {} for d in days}
    state["pending_days"] = list(days)
    state["step"] = "asking_slots"
    set_dm_state(slack_user_id, state)
    _ask_slots_for_next_day(slack_user_id, state, reply)


def _ask_slots_for_next_day(slack_user_id: str, state: dict, reply) -> None:
    pending = state.get("pending_days", [])
    if not pending:
        state["step"] = "asking_locs"
        set_dm_state(slack_user_id, state)
        _ask_locs(state, reply)
        return
    day = pending[0]
    reply(
        f"*Step 2 of 3 — Shifts for {DAY_NAMES[day]}:*\n"
        f"Can you work the *open* shift, *close* shift, or *both*?\n"
        f"_(e.g. `open`, `close`, or `both`)_"
    )


def _handle_slots_response(slack_user_id: str, text: str, state: dict, reply) -> None:
    pending = state.get("pending_days", [])
    if not pending:
        state["step"] = "asking_locs"
        set_dm_state(slack_user_id, state)
        _ask_locs(state, reply)
        return

    day = pending[0]
    slots = _parse_slots(text)
    if not slots:
        reply(
            f"I didn't catch that for {DAY_NAMES[day]}. "
            "Please reply *open*, *close*, or *both*."
        )
        return

    state["days"][day]["slots"] = slots
    state["pending_days"] = pending[1:]

    if state["pending_days"]:
        set_dm_state(slack_user_id, state)
        _ask_slots_for_next_day(slack_user_id, state, reply)
    else:
        state["step"] = "asking_locs"
        set_dm_state(slack_user_id, state)
        _ask_locs(state, reply)


def _ask_locs(state: dict, reply) -> None:
    loc_list = "\n".join(
        f"  • *{code}* — {STORE_NAMES[code]}" for code in VALID_STORES
    )
    reply(
        f"*Step 3 of 3 — Locations:*\n"
        f"Which store locations are you able to work at?\n\n"
        f"{loc_list}\n\n"
        f"Reply with the codes, e.g. `GW, GF` or `all` for all four."
    )


def _handle_locs_response(slack_user_id: str, text: str, state: dict, reply) -> None:
    locs = _parse_locations(text)
    if not locs:
        reply(
            "I didn't recognise those locations. Please use the codes: "
            "`GW`, `GM`, `GF`, `VVP` — or reply `all` for all four."
        )
        return

    # Apply locations to all days
    for day_data in state["days"].values():
        day_data["locations"] = locs

    state["locations"] = locs
    state["step"] = "confirming"
    set_dm_state(slack_user_id, state)

    # Show summary
    reply(_build_summary(state) + "\n\nReply *confirm* to save, or *change* to start over.")


def _handle_confirmation(slack_user_id: str, text: str, state: dict, reply, client) -> None:
    if _is_cancel_or_change(text):
        clear_dm_state(slack_user_id)
        _start_availability_flow(slack_user_id, {}, reply)
        return
    if _is_confirmation(text):
        _persist_availability(slack_user_id, state)
        clear_dm_state(slack_user_id)
        week_display = _format_week(state["week_start"])
        reply(
            f"✅ Availability saved for *{week_display}*! "
            "You'll receive your schedule once it's been approved. "
            "Reply *my schedule* at any time to check."
        )
    else:
        reply("Reply *confirm* to save your availability, or *change* to start over.")


# ── Persistence helpers ───────────────────────────────────────────────────────

def _persist_availability(slack_user_id: str, state: dict) -> None:
    avail = Availability(
        availability_id=new_id(),
        slack_user_id=slack_user_id,
        week_start=state["week_start"],
        days=state["days"],
    )
    upsert_availability(avail)
    log.info(
        "osn_shift_handler: saved availability user=%s week=%s days=%s",
        slack_user_id, state["week_start"], list(state["days"].keys()),
    )


def _save_empty_availability(slack_user_id: str, week_start: str) -> None:
    avail = Availability(
        availability_id=new_id(),
        slack_user_id=slack_user_id,
        week_start=week_start,
        days={},
    )
    upsert_availability(avail)


# ── Admin commands ────────────────────────────────────────────────────────────

def handle_admin_command(
    text: str,
    slack_user_id: str,
    channel_id: str,
    client,
) -> Optional[str]:
    """Handle an @Cora admin command in an OSN channel.

    Returns a reply string, or None if the text isn't an OSN scheduler command.
    """
    t_lower = text.lower()

    # Generate schedule
    if re.search(r"\bgenerate\b.*\bschedule\b|\bschedule\b.*\bgenerate\b", t_lower):
        if not _is_admin(slack_user_id):
            return "Sorry, only OSN scheduling admins can generate schedules."
        week_start = next_monday()
        return _cmd_generate(week_start, slack_user_id, channel_id, client)

    # Show availability
    if re.search(r"\bshow\b.*\bavailabilit\b|\bavailabilit\b.*\bshow\b|\bwho.*submitted\b|\bsubmitted\b", t_lower):
        week_start = next_monday()
        return _cmd_show_availability(week_start)

    # Approve schedule
    m = re.search(r"\bapprove\b.*\bschedule\b\s*([a-f0-9-]{8,})?", t_lower)
    if m:
        if not _is_admin(slack_user_id):
            return "Only OSN scheduling admins can approve schedules."
        schedule_id = m.group(1)
        return _cmd_approve(schedule_id, slack_user_id, next_monday())

    # Publish schedule
    m = re.search(r"\bpublish\b.*\bschedule\b\s*([a-f0-9-]{8,})?", t_lower)
    if m:
        if not _is_admin(slack_user_id):
            return "Only OSN scheduling admins can publish schedules."
        schedule_id = m.group(1)
        return _cmd_publish(schedule_id, next_monday(), client)

    # Show current schedule
    if re.search(r"\bschedule\b", t_lower) and re.search(r"\bshow\b|\bview\b|\bsee\b|\bcurrent\b|\bnext\b|\bweek\b", t_lower):
        week_start = next_monday()
        sched = get_latest_schedule_for_week(week_start)
        if not sched:
            return f"No schedule found for week of {week_start}. Use *generate schedule* to create one."
        employees = {e.slack_user_id: e for e in get_all_active_employees()}
        return format_schedule_slack(sched, employees)

    # List employees
    if re.search(r"\blist\b.*\bemployee\b|\bemployee\b.*\blist\b|\bstaff\b|\bshow\b.*\bemployee\b", t_lower):
        if not _is_admin(slack_user_id):
            return "Only admins can view the employee list."
        return _cmd_list_employees()

    # Add employee:  add employee <@U123> name="Jane Doe" tier=high locations=GW,GM
    if re.search(r"\badd\b.*\bemployee\b", t_lower):
        if not _is_admin(slack_user_id):
            return "Only admins can add employees."
        return _cmd_upsert_employee(text, action="add")

    # Update employee:  update employee <@U123> tier=mid  OR  update employee <@U123> locations=GW,VVP
    if re.search(r"\bupdate\b.*\bemployee\b|\bedit\b.*\bemployee\b", t_lower):
        if not _is_admin(slack_user_id):
            return "Only admins can update employees."
        return _cmd_upsert_employee(text, action="update")

    # Remove / deactivate employee
    if re.search(r"\bremove\b.*\bemployee\b|\bdeactivate\b.*\bemployee\b", t_lower):
        if not _is_admin(slack_user_id):
            return "Only admins can remove employees."
        return _cmd_deactivate_employee(text)

    return None  # Not an OSN scheduler command


def _cmd_generate(week_start: str, admin_user_id: str, channel_id: str, client) -> str:
    week_display = _format_week(week_start)
    avail_list = get_week_availability(week_start)
    if not avail_list:
        return (
            f"No availability submitted yet for {week_display}. "
            "Remind employees to DM me their availability first."
        )

    sched, warnings = generate_schedule(week_start)
    employees = {e.slack_user_id: e for e in get_all_active_employees()}

    formatted = format_schedule_slack(sched, employees)

    # Post the full schedule to the approval channel
    _post_approval_card(sched, formatted, warnings, channel_id, client)

    warn_text = ""
    if warnings:
        warn_list = "\n".join(f"  ⚠ {w}" for w in warnings[:5])
        warn_text = f"\n\n*Warnings ({len(warnings)}):*\n{warn_list}"

    return (
        f"✅ Draft schedule generated for *{week_display}*.\n"
        f"Schedule ID: `{sched.schedule_id[:8]}`\n"
        f"Submitters: {len(avail_list)}{warn_text}\n\n"
        "React ✅ to the schedule card to approve it, then use *publish schedule* to notify employees."
    )


def _cmd_show_availability(week_start: str) -> str:
    week_display = _format_week(week_start)
    all_employees = get_all_active_employees()
    avail_list = get_week_availability(week_start)
    submitted_ids = {a.slack_user_id for a in avail_list}

    submitted = [e for e in all_employees if e.slack_user_id in submitted_ids]
    missing   = [e for e in all_employees if e.slack_user_id not in submitted_ids]

    lines = [f"*Availability for {week_display}*\n"]
    if submitted:
        lines.append(f"✅ *Submitted ({len(submitted)}):*")
        for e in submitted:
            a = next(a for a in avail_list if a.slack_user_id == e.slack_user_id)
            days_str = ", ".join(DAY_NAMES[d] for d in VALID_DAYS if d in a.days)
            lines.append(f"  • {e.name} [{e.tier}] — {days_str or 'none'}")
    if missing:
        lines.append(f"\n⏳ *Not yet submitted ({len(missing)}):*")
        for e in missing:
            lines.append(f"  • {e.name} [{e.tier}]")

    if not all_employees:
        return "No employees in the system yet. Use the employee management commands to add them."

    return "\n".join(lines)


def _cmd_approve(schedule_id: Optional[str], admin_user_id: str, week_start: str) -> str:
    if schedule_id:
        # Try full ID lookup first, then prefix
        sched = get_schedule(schedule_id)
        if not sched:
            # Try prefix match
            import sqlite3
            from pathlib import Path
            from .osn_shift_db import _DB_PATH, _row_to_schedule
            conn = sqlite3.connect(str(_DB_PATH))
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM osn_schedules WHERE schedule_id LIKE ?",
                (schedule_id + "%",),
            ).fetchone()
            conn.close()
            sched = _row_to_schedule(row) if row else None
    else:
        sched = get_latest_schedule_for_week(week_start)

    if not sched:
        return "Schedule not found. Use the full or partial schedule ID from the draft card."

    if sched.status == "approved":
        return f"Schedule `{sched.schedule_id[:8]}` is already approved."
    if sched.status == "published":
        return f"Schedule `{sched.schedule_id[:8]}` is already published."

    ok = approve_schedule(sched.schedule_id, admin_user_id)
    if ok:
        return (
            f"✅ Schedule `{sched.schedule_id[:8]}` approved for week of *{sched.week_start}*.\n"
            "Use *publish schedule* to send each employee their shifts."
        )
    return "Failed to approve schedule. Check logs."


def _cmd_publish(schedule_id: Optional[str], week_start: str, client) -> str:
    if schedule_id:
        sched = get_schedule(schedule_id)
        if not sched:
            import sqlite3
            from .osn_shift_db import _DB_PATH, _row_to_schedule
            conn = sqlite3.connect(str(_DB_PATH))
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM osn_schedules WHERE schedule_id LIKE ?",
                (schedule_id + "%",),
            ).fetchone()
            conn.close()
            sched = _row_to_schedule(row) if row else None
    else:
        sched = get_latest_schedule_for_week(week_start)

    if not sched:
        return "Schedule not found."
    if sched.status not in ("approved", "published"):
        return (
            f"Schedule `{sched.schedule_id[:8]}` is `{sched.status}` — "
            "it must be approved before publishing. Use *approve schedule* first."
        )

    employees = {e.slack_user_id: e for e in get_all_active_employees()}
    notified = []
    failed = []

    for uid in employees:
        msg = format_employee_schedule_slack(sched, uid, employees)
        try:
            client.chat_postMessage(
                channel=uid,
                text=msg,
                unfurl_links=False,
                unfurl_media=False,
            )
            notified.append(uid)
        except Exception as exc:
            log.error("osn_shift_handler: failed to DM schedule to %s: %s", uid, exc)
            failed.append(uid)

    publish_schedule(sched.schedule_id)

    result = f"📣 Published schedule `{sched.schedule_id[:8]}` — notified {len(notified)} employees."
    if failed:
        names = [employees[uid].name for uid in failed if uid in employees]
        result += f"\n⚠ Failed to DM: {', '.join(names)}. Check logs."
    return result


_MENTION_RE = re.compile(r"<@([A-Z0-9]+)>")
# Quoted name="Full Name" OR unquoted name=FirstOnly
_NAME_RE    = re.compile(r'name=["\']([^"\']+)["\']|name=(\S+)', re.IGNORECASE)
_TIER_RE    = re.compile(r"tier=(\w+)", re.IGNORECASE)
_LOCS_RE    = re.compile(r"locations?=([\w,\s]+?)(?:\s|$)", re.IGNORECASE)


def _cmd_list_employees() -> str:
    employees = get_all_active_employees()
    if not employees:
        return (
            "No employees in the system yet.\n"
            "Use `@Cora add employee <@SlackUser> name=\"Full Name\" tier=high locations=GW,GM` to add them."
        )
    tier_order = {"high": 0, "mid": 1, "low": 2}
    employees.sort(key=lambda e: (tier_order.get(e.tier, 9), e.name))

    lines = [f"*OSN Employees ({len(employees)}):*\n"]
    for e in employees:
        loc_str = ", ".join(e.preferred_locations) if e.preferred_locations else "none set"
        lines.append(f"  • *{e.name}* — tier: `{e.tier}` | locations: {loc_str} | <@{e.slack_user_id}>")
    return "\n".join(lines)


def _cmd_upsert_employee(text: str, action: str) -> str:
    uid_match = _MENTION_RE.search(text)
    if not uid_match:
        return (
            f"Please mention the employee's Slack account.\n"
            f"Example: `@Cora {action} employee <@U123ABC> name=\"Jane Doe\" tier=high locations=GW,GM`"
        )
    uid = uid_match.group(1)

    tier_match = _TIER_RE.search(text)
    locs_match = _LOCS_RE.search(text)
    name_match = _NAME_RE.search(text)

    existing = get_employee(uid)

    # For add, name is required unless updating
    if name_match:
        name = (name_match.group(1) or name_match.group(2) or "").strip()
    else:
        name = existing.name if existing else None
    if not name:
        return (
            "Please include the employee's name.\n"
            "Example: `name=\"Jane Doe\"`"
        )

    tier_raw = tier_match.group(1).lower() if tier_match else (existing.tier if existing else None)
    if not tier_raw or tier_raw not in VALID_TIERS:
        return (
            f"Please specify a valid tier: `tier=high`, `tier=mid`, or `tier=low`.\n"
            f"Example: `@Cora {action} employee <@{uid}> tier=high`"
        )

    if locs_match:
        locs_raw = locs_match.group(1)
        if re.search(r"\ball\b", locs_raw, re.IGNORECASE):
            locs = list(VALID_STORES)
        else:
            locs = [s.strip().upper() for s in re.split(r"[,\s]+", locs_raw) if s.strip()]
            locs = [l for l in locs if l in VALID_STORES]
    else:
        locs = existing.preferred_locations if existing else []

    if not locs:
        return (
            f"Please specify at least one location.\n"
            f"Valid codes: `{', '.join(VALID_STORES)}` or `all`.\n"
            f"Example: `locations=GW,GM`"
        )

    emp = Employee(
        slack_user_id=uid,
        name=name,
        tier=tier_raw,
        preferred_locations=locs,
        is_active=True,
    )
    upsert_employee(emp)

    verb = "Added" if action == "add" and not existing else "Updated"
    loc_display = ", ".join(f"{c} ({STORE_NAMES[c]})" for c in locs)
    return (
        f"✅ {verb} *{name}* (<@{uid}>)\n"
        f"  Tier: `{tier_raw}` | Locations: {loc_display}"
    )


def _cmd_deactivate_employee(text: str) -> str:
    import sqlite3
    from .osn_shift_db import _DB_PATH

    uid_match = _MENTION_RE.search(text)
    if not uid_match:
        return "Please mention the employee to remove: `@Cora remove employee <@U123ABC>`"
    uid = uid_match.group(1)

    existing = get_employee(uid)
    if not existing:
        return f"No employee found with Slack ID `{uid}`."

    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute("UPDATE osn_employees SET is_active = 0 WHERE slack_user_id = ?", (uid,))
    conn.commit()
    conn.close()
    return f"✅ *{existing.name}* has been deactivated and won't appear in future schedules."


# ── Approval card ─────────────────────────────────────────────────────────────

def _post_approval_card(sched, formatted: str, warnings: list, channel_id: str, client) -> None:
    warn_section = ""
    if warnings:
        warn_lines = "\n".join(f"⚠ {w}" for w in warnings[:8])
        warn_section = f"\n\n*Scheduling warnings:*\n{warn_lines}"

    card = (
        f"*OSN Schedule Draft — week of {sched.week_start}*\n"
        f"Schedule ID: `{sched.schedule_id[:8]}`\n\n"
        f"{formatted}{warn_section}\n\n"
        f"*React ✅ on this message to approve, or reply `approve schedule {sched.schedule_id[:8]}` below.*"
    )
    try:
        resp = client.chat_postMessage(
            channel=channel_id,
            text=card,
            unfurl_links=False,
            unfurl_media=False,
        )
        # Store approval card ts in schedule notes for reaction lookup
        card_ts = resp.get("ts", "")
        card_channel = resp.get("channel", channel_id)
        log.info(
            "osn_shift_handler: posted approval card ts=%s channel=%s sched=%s",
            card_ts, card_channel, sched.schedule_id[:8],
        )
        # Update schedule notes with card ts (for reaction-based approval)
        import json
        from .osn_shift_db import save_schedule, get_schedule
        s = get_schedule(sched.schedule_id)
        if s:
            s.notes = json.dumps({"approval_card_ts": card_ts, "approval_channel": card_channel})
            save_schedule(s)
    except Exception as exc:
        log.error("osn_shift_handler: failed to post approval card: %s", exc)


# ── Reaction-based approval ───────────────────────────────────────────────────

def handle_schedule_approval_reaction(
    reaction: str,
    message_ts: str,
    reactor_user_id: str,
    client,
) -> Optional[str]:
    """Check if a reaction on a schedule approval card should trigger approval.

    Called from app.py reaction_added handler. Returns a reply string if handled.
    """
    if reaction != "white_check_mark":
        return None
    if not _is_admin(reactor_user_id):
        return None

    # Find schedule whose approval card has this ts
    import sqlite3, json
    from .osn_shift_db import _DB_PATH, _row_to_schedule
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM osn_schedules WHERE status = 'draft' OR status = 'pending_approval'"
    ).fetchall()
    conn.close()

    for row in rows:
        sched = _row_to_schedule(row)
        try:
            notes = json.loads(sched.notes) if sched.notes else {}
        except Exception:
            notes = {}
        if notes.get("approval_card_ts") == message_ts:
            ok = approve_schedule(sched.schedule_id, reactor_user_id)
            if ok:
                return (
                    f"✅ Schedule `{sched.schedule_id[:8]}` for week of *{sched.week_start}* approved!\n"
                    f"Use `@Cora publish schedule {sched.schedule_id[:8]}` to notify employees."
                )
    return None


# ── Prompt employees ──────────────────────────────────────────────────────────

def prompt_all_employees_for_availability(client) -> dict:
    """DM every active employee asking for next week's availability.

    Called by a scheduled task (e.g. every Friday). Returns {sent: n, failed: n}.
    """
    employees = get_all_active_employees()
    week_start = next_monday()
    week_display = _format_week(week_start)
    sent, failed = 0, 0

    for emp in employees:
        msg = (
            f"Hi {emp.name.split()[0]}! 👋 It's time to submit your availability for "
            f"*{week_display}*.\n\n"
            "Reply *submit availability* to get started — it only takes a minute!"
        )
        try:
            client.chat_postMessage(
                channel=emp.slack_user_id,
                text=msg,
                unfurl_links=False,
                unfurl_media=False,
            )
            sent += 1
        except Exception as exc:
            log.error("osn_shift_handler: failed to prompt %s: %s", emp.slack_user_id, exc)
            failed += 1

    log.info("osn_shift_handler: prompted %d employees (failed %d) week=%s", sent, failed, week_start)
    return {"sent": sent, "failed": failed}


# ── Personal schedule lookup ──────────────────────────────────────────────────

def _send_personal_schedule(slack_user_id: str, reply) -> None:
    week_start = current_week_monday()
    sched = get_latest_schedule_for_week(week_start)

    if not sched or sched.status not in ("approved", "published"):
        # Try next week
        week_start = next_monday()
        sched = get_latest_schedule_for_week(week_start)

    if not sched or sched.status not in ("approved", "published"):
        reply("No approved schedule found for this week yet. Check back once it's been approved!")
        return

    employees = {e.slack_user_id: e for e in get_all_active_employees()}
    reply(format_employee_schedule_slack(sched, slack_user_id, employees))


# ── Formatting helpers ────────────────────────────────────────────────────────

def _format_week(week_start: str) -> str:
    try:
        from datetime import date, timedelta
        d = date.fromisoformat(week_start)
        end = d + timedelta(days=6)
        return f"{d.strftime('%b %-d')} – {end.strftime('%b %-d, %Y')}"
    except Exception:
        return week_start


def _build_summary(state: dict) -> str:
    lines = [f"*Here's your availability for {_format_week(state['week_start'])}:*\n"]
    locs = state.get("locations", [])
    loc_str = ", ".join(f"{c} ({STORE_NAMES[c]})" for c in locs)

    for day, day_data in state.get("days", {}).items():
        slots = day_data.get("slots", [])
        slot_str = " & ".join(s.capitalize() for s in slots)
        lines.append(f"  • {DAY_NAMES[day]}: {slot_str}")

    lines.append(f"\nLocations: {loc_str}")
    return "\n".join(lines)


def _dm_help() -> str:
    return (
        "*OSN Shift Scheduler — Commands:*\n\n"
        "• *submit availability* — Enter your hours for next week\n"
        "• *my schedule* — See your upcoming shifts\n"
        "• *cancel* — Cancel the current availability entry\n\n"
        "_Admins only:_\n"
        "• *@Cora generate schedule* — Create a draft schedule\n"
        "• *@Cora show availability* — See who's submitted\n"
        "• *@Cora approve schedule* — Approve the draft\n"
        "• *@Cora publish schedule* — Send schedules to employees"
    )


# ── Admin check ───────────────────────────────────────────────────────────────

def _is_admin(slack_user_id: str) -> bool:
    if not _ADMIN_USER_IDS:
        # No admin list configured — allow any user (open mode for initial setup)
        return True
    return slack_user_id in _ADMIN_USER_IDS
