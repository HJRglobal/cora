#!/usr/bin/env python3
"""Due-Date DM Escalation -- DM assignees for tasks due today/tomorrow + stalled P0 decisions.

Pass 1: For each team member with an Asana GID, fetch their open tasks and DM them
        for any task with due_on = today or tomorrow (within 24h from now in AZ time).

Pass 2: Read memory/decisions-pending.md and DM Harrison for any P0 decisions open >7 days.

Throttle:
  - Tasks: 48h per task GID (won't re-alert within 2 days)
  - Decisions: 7 days per decision text hash (decisions move slower)

Usage (Windows Task Scheduler):
    python scripts/run_due_date_escalation.py [--dry-run]

Environment variables required:
    ASANA_PAT           Asana personal access token
    SLACK_BOT_TOKEN     For sending DMs
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

load_dotenv(_REPO_ROOT / ".env")

import yaml  # noqa: E402

from cora.tools.asana_client import get_user_tasks, AsanaClientError  # noqa: E402

LOG_DIR = _REPO_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s -- %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            LOG_DIR / f"cora-{datetime.now().strftime('%Y-%m-%d')}.log",
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger("due_date_escalation")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_THROTTLE_PATH = _REPO_ROOT / "data" / "state" / "due_date_escalation_throttle.json"
_ASANA_MAP_PATH = _REPO_ROOT / "data" / "maps" / "slack-to-asana.yaml"
# decisions-pending.md lives on the G: Founder-OS mount, NOT in the repo (the
# repo has no memory/ dir). Pass 2 was a silent no-op because this pointed at the
# repo. Read it via drive_io (bounded, fail-soft). Env-overridable for tests.
_DECISIONS_PENDING_PATH = Path(
    os.environ.get("FNDR_DECISIONS_PENDING_PATH")
    or r"G:\My Drive\HJR-Founder-OS\memory\decisions-pending.md")
_HARRISON_SLACK_ID = "U0B2RM2JYJ1"
_FALLBACK_CHANNEL = "C0B3K67J10T"  # #hjrg-leadership

_TASK_THROTTLE_SECONDS = 48 * 3600     # 48 hours
_DECISION_THROTTLE_SECONDS = 7 * 86400  # 7 days
_DECISION_STALE_DAYS = 7


def _az_now() -> datetime:
    """Current time in America/Phoenix (UTC-7, no DST)."""
    return datetime.now(timezone.utc).astimezone(
        timezone(timedelta(hours=-7))
    )


def _load_throttle() -> dict:
    if _THROTTLE_PATH.exists():
        try:
            return json.loads(_THROTTLE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_throttle(throttle: dict) -> None:
    _THROTTLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _THROTTLE_PATH.write_text(json.dumps(throttle, indent=2), encoding="utf-8")


def _is_throttled(throttle: dict, key: str, window_seconds: int) -> bool:
    ts = throttle.get(key)
    if ts is None:
        return False
    return (time.time() - ts) < window_seconds


def _load_asana_map() -> list[dict]:
    """Return list of user dicts from slack-to-asana.yaml."""
    try:
        raw = yaml.safe_load(_ASANA_MAP_PATH.read_text(encoding="utf-8")) or {}
        return raw.get("users") or []
    except Exception as exc:
        log.warning("Failed to load asana map: %s", exc)
        return []


def _open_dm(slack_client, user_id: str) -> str | None:
    """Open a DM channel with user_id and return channel ID."""
    try:
        resp = slack_client.conversations_open(users=[user_id])
        return resp["channel"]["id"]
    except Exception as exc:
        log.warning("Failed to open DM with %s: %s", user_id, exc)
        return None


def _send_dm(slack_client, user_id: str, text: str, dry_run: bool) -> bool:
    if dry_run:
        log.info("[DRY-RUN] DM to %s: %s", user_id, text[:120])
        return True
    dm_ch = _open_dm(slack_client, user_id)
    if not dm_ch:
        return False
    try:
        slack_client.chat_postMessage(channel=dm_ch, text=text)
        return True
    except Exception as exc:
        log.warning("Failed to send DM to %s: %s", user_id, exc)
        return False


# ---------------------------------------------------------------------------
# Pass 1 -- Due-soon task DMs
# ---------------------------------------------------------------------------

def _is_due_soon(due_on_str: str | None, now_az: datetime) -> bool:
    """True if task is due today or tomorrow (within 24h)."""
    if not due_on_str:
        return False
    try:
        due = datetime.strptime(due_on_str, "%Y-%m-%d").date()
    except ValueError:
        return False
    today = now_az.date()
    tomorrow = today + timedelta(days=1)
    return due in (today, tomorrow)


def run_pass1_due_tasks(
    slack_client,
    users: list[dict],
    throttle: dict,
    now_az: datetime,
    dry_run: bool,
) -> dict[str, int]:
    stats = {"alerted": 0, "throttled": 0, "errors": 0}

    for user in users:
        gid = str(user.get("asana_user_gid") or "")
        slack_id = user.get("slack_user_id") or ""
        name = user.get("display_name") or slack_id

        if not gid or not slack_id:
            continue

        try:
            tasks = get_user_tasks(gid)
        except AsanaClientError as exc:
            log.warning("Asana error for user %s: %s", name, exc)
            stats["errors"] += 1
            continue

        for task in tasks:
            due_on = task.get("due_on")
            if not _is_due_soon(due_on, now_az):
                continue

            task_gid = task.get("gid", "")
            task_name = task.get("name", "Untitled task")
            url = task.get("permalink_url", "")
            throttle_key = f"task:{task_gid}"

            if _is_throttled(throttle, throttle_key, _TASK_THROTTLE_SECONDS):
                stats["throttled"] += 1
                log.debug("Throttled task %s for %s", task_gid, name)
                continue

            link = f"<{url}|{task_name}>" if url else task_name
            text = (
                f":alarm_clock: *Task due soon* -- {link}\n"
                f"Due: {due_on}. Want me to help move it forward?"
            )

            if _send_dm(slack_client, slack_id, text, dry_run):
                throttle[throttle_key] = time.time()
                stats["alerted"] += 1
                log.info("Alerted %s for task %s (due %s)", name, task_gid, due_on)

    return stats


# ---------------------------------------------------------------------------
# Pass 2 -- P0 stalled decisions
# ---------------------------------------------------------------------------

def _parse_pending_decisions(path: Path) -> list[dict[str, Any]]:
    """Parse the Founder-OS pending-decisions queue -> stalled P0/P1 entries.

    Reads via drive_io (bounded, fail-soft on a G: mount blip -- this is a
    scheduled job that must never hang). Format is '### topic' blocks with
    '**Severity**: P0/P1' + '**Last touched**: YYYY-MM-DD'; skips the '[Topic]'
    template skeleton and the '## Recently resolved' section. Ports the tested
    parser from strategy_memo.gather_stalled_decisions so it matches the real
    file (the old list-marker+P0 parser false-matched the template/rubric lines
    and read the date off the wrong line). age_days is computed from Last touched
    (None when absent -> not escalated)."""
    from cora import drive_io

    try:
        content = drive_io.read_text(path, timeout=5.0, retry_seconds=2.0)
    except drive_io.DriveUnavailable:
        log.warning("decisions-pending.md unavailable (G: mount reconnecting) -- "
                    "skipping pass 2")
        return []
    except FileNotFoundError:
        log.info("decisions-pending.md not found at %s, skipping pass 2", path)
        return []
    except Exception as exc:  # noqa: BLE001 -- a read error must not crash the job
        log.warning("Failed to read decisions-pending.md: %s", exc)
        return []

    today = _az_now().date()
    resolved = re.search(r"^## Recently resolved\b", content, re.MULTILINE)
    parseable = content[: resolved.start()] if resolved else content

    decisions: list[dict[str, Any]] = []
    for block in re.split(r"\n(?=### )", parseable):
        if not block.startswith("### "):
            continue
        topic = block.split("\n", 1)[0][4:].strip()
        if topic == "[Topic]":
            continue  # the "How to use" template skeleton, not a real entry
        # Match an annotated real value ("P0 (decision Monday)") but NOT the
        # template alternatives line "P0 / P1 / P2 / P3" -- the (?!\s*/) guard.
        sev = re.search(r"\*\*Severity\*\*:\s*(P\d)\b(?!\s*/)", block)
        if not sev or sev.group(1) not in ("P0", "P1"):
            continue
        age_days: int | None = None
        touched = re.search(r"\*\*Last touched\*\*:\s*[^\n]*?(\d{4}-\d{2}-\d{2})", block)
        if touched:
            try:
                age_days = (today - datetime.strptime(
                    touched.group(1), "%Y-%m-%d").date()).days
            except ValueError:
                pass
        decisions.append({
            "topic": topic[:300],
            "severity": sev.group(1),
            "age_days": age_days,
        })
    return decisions


def run_pass2_stalled_decisions(
    slack_client,
    throttle: dict,
    dry_run: bool,
) -> dict[str, int]:
    stats = {"alerted": 0, "throttled": 0}

    decisions = _parse_pending_decisions(_DECISIONS_PENDING_PATH)
    if not decisions:
        return stats

    now_ts = time.time()

    for decision in decisions:
        age_days = decision.get("age_days")
        # Undated (age_days None) -> can't tell it's stale, don't escalate.
        if age_days is None or age_days < _DECISION_STALE_DAYS:
            continue

        sev = decision["severity"]
        topic = decision["topic"]
        text_hash = hashlib.md5(f"{sev}:{topic}".encode()).hexdigest()
        throttle_key = f"decision:{text_hash}"

        if _is_throttled(throttle, throttle_key, _DECISION_THROTTLE_SECONDS):
            stats["throttled"] += 1
            continue

        msg = (
            f":rotating_light: *Stalled {sev} decision (>{age_days}d open)*\n"
            f"{topic[:300]}\n\n"
            f"This has been open for {age_days}+ days."
        )

        if _send_dm(slack_client, _HARRISON_SLACK_ID, msg, dry_run):
            throttle[throttle_key] = now_ts
            stats["alerted"] += 1
            log.info("Alerted Harrison on stalled %s decision age=%dd", sev, age_days)

    return stats


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(dry_run: bool = False) -> dict:
    from slack_sdk import WebClient as SlackWebClient

    bot_token = os.getenv("SLACK_BOT_TOKEN")
    if not bot_token:
        log.error("SLACK_BOT_TOKEN not set")
        return {"error": "SLACK_BOT_TOKEN not set"}

    slack = SlackWebClient(token=bot_token)
    users = _load_asana_map()
    throttle = _load_throttle()
    now_az = _az_now()

    log.info("Starting due-date escalation: %d users, dry_run=%s", len(users), dry_run)

    p1_stats = run_pass1_due_tasks(slack, users, throttle, now_az, dry_run)
    log.info("Pass 1 done: %s", p1_stats)

    p2_stats = run_pass2_stalled_decisions(slack, throttle, dry_run)
    log.info("Pass 2 done: %s", p2_stats)

    if not dry_run:
        _save_throttle(throttle)

    result = {
        "tasks_alerted": p1_stats["alerted"],
        "tasks_throttled": p1_stats["throttled"],
        "tasks_errors": p1_stats["errors"],
        "decisions_alerted": p2_stats["alerted"],
        "decisions_throttled": p2_stats["throttled"],
    }
    log.info("Due-date escalation complete: %s", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cora due-date escalation script")
    parser.add_argument("--dry-run", action="store_true", help="Log actions without sending")
    args = parser.parse_args()
    result = main(dry_run=args.dry_run)
    sys.exit(0)
