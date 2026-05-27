"""Lexington Services Cora tools.

lex_revalidation_status
    Reads Asana task 1215070649606664 (AZ DDD Therapy Revalidation) + its
    subtasks and stories. Returns days-remaining to 2026-06-30, open sub-task
    blockers, and last-comment age. Designed for the Sunday-evening
    #lex-leadership brief and any in-thread revalidation question.

lex_staff_pulse
    BLOCKED — depends on Sean/Jen Drive upload pipeline (Sean + Jen upload
    DDD staffing reports + driver safety CSVs to a shared Drive folder;
    Cora ingests for context). Returns a stub message until the pipeline is
    configured. Placeholder wiring keeps the tool in the catalog so it can
    be activated without a restart once the pipeline is ready.
"""

import logging
import os
from datetime import date, datetime, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)

_BASE = "https://app.asana.com/api/1.0"
_WORKSPACE_GID = "682743441507584"  # HJR Global workspace
_TIMEOUT = 10.0

# -----------------------------------------------------------------------
# Deadline constant
# -----------------------------------------------------------------------
_REVALIDATION_DEADLINE = date(2026, 6, 30)
_REVALIDATION_TASK_GID = "1215070649606664"


class LexClientError(Exception):
    """Raised when an Asana or data-access call fails."""


def _pat() -> str:
    val = os.environ.get("ASANA_PAT", "")
    if not val:
        raise LexClientError("ASANA_PAT not set in environment -- Asana tool-use disabled")
    return val


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_pat()}"}


# -----------------------------------------------------------------------
# Asana helpers
# -----------------------------------------------------------------------

def _get_task(task_gid: str) -> dict[str, Any]:
    """Fetch a single task by GID with relevant fields."""
    params = {
        "opt_fields": ",".join([
            "name",
            "completed",
            "due_on",
            "notes",
            "permalink_url",
            "modified_at",
            "assignee.name",
        ]),
    }
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.get(f"{_BASE}/tasks/{task_gid}", params=params, headers=_headers())
    except httpx.RequestError as exc:
        raise LexClientError(f"Asana network error: {exc}") from exc

    if r.status_code == 404:
        raise LexClientError(f"Asana task {task_gid} not found (404)")
    if r.status_code == 401:
        raise LexClientError("Asana 401 -- PAT invalid or revoked")
    if r.status_code >= 400:
        raise LexClientError(f"Asana {r.status_code}: {r.text[:200]}")
    return r.json().get("data", {})


def _get_subtasks(task_gid: str) -> list[dict[str, Any]]:
    """Fetch subtasks of a task."""
    params = {
        "opt_fields": "name,completed,due_on,assignee.name,permalink_url",
        "limit": 50,
    }
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.get(
                f"{_BASE}/tasks/{task_gid}/subtasks",
                params=params,
                headers=_headers(),
            )
    except httpx.RequestError as exc:
        raise LexClientError(f"Asana network error fetching subtasks: {exc}") from exc

    if r.status_code >= 400:
        # Subtask fetch failure is non-fatal -- return empty
        log.warning("lex_client: subtask fetch returned %d for task %s", r.status_code, task_gid)
        return []
    return r.json().get("data", []) or []


def _get_latest_story(task_gid: str) -> dict[str, Any] | None:
    """Return the most recent comment/story on the task, or None."""
    params = {
        "opt_fields": "created_at,created_by.name,text,type",
        "limit": 10,
    }
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.get(
                f"{_BASE}/tasks/{task_gid}/stories",
                params=params,
                headers=_headers(),
            )
    except httpx.RequestError as exc:
        log.warning("lex_client: stories fetch error for task %s: %s", task_gid, exc)
        return None

    if r.status_code >= 400:
        log.warning("lex_client: stories fetch %d for task %s", r.status_code, task_gid)
        return None

    stories = r.json().get("data", []) or []
    # Comments have type="comment"; filter for those
    comments = [s for s in stories if s.get("type") == "comment"]
    return comments[-1] if comments else None


# -----------------------------------------------------------------------
# Public interface
# -----------------------------------------------------------------------

def get_revalidation_status() -> str:
    """Fetch AZ DDD Therapy Revalidation status from Asana and format for Slack.

    Returns a Slack mrkdwn string ready to be posted as-is.
    """
    try:
        task = _get_task(_REVALIDATION_TASK_GID)
    except LexClientError as exc:
        log.warning("lex_revalidation_status: task fetch failed: %s", exc)
        return "I don't have that right now."

    today = date.today()
    days_remaining = (_REVALIDATION_DEADLINE - today).days

    # Deadline marker
    if task.get("completed"):
        deadline_line = "REVALIDATION COMPLETE"
    elif days_remaining < 0:
        deadline_line = f"DEADLINE PASSED {abs(days_remaining)}d ago -- URGENT"
    elif days_remaining == 0:
        deadline_line = "DEADLINE IS TODAY -- URGENT"
    elif days_remaining <= 7:
        deadline_line = f"{days_remaining}d remaining to 6/30 -- CRITICAL"
    elif days_remaining <= 14:
        deadline_line = f"{days_remaining}d remaining to 6/30 -- HIGH"
    elif days_remaining <= 30:
        deadline_line = f"{days_remaining}d remaining to 6/30 -- WATCH"
    else:
        deadline_line = f"{days_remaining}d remaining to 6/30"

    # Emoji marker
    if days_remaining <= 7 and not task.get("completed"):
        marker = "CRITICAL"
    elif days_remaining <= 30 and not task.get("completed"):
        marker = "WARNING"
    else:
        marker = "OK" if task.get("completed") else "WATCH"

    lines = [
        f"*AZ DDD Therapy Revalidation* -- {deadline_line}",
        "",
    ]

    # Subtasks
    try:
        subtasks = _get_subtasks(_REVALIDATION_TASK_GID)
    except LexClientError:
        subtasks = []

    if subtasks:
        open_subs = [s for s in subtasks if not s.get("completed")]
        done_subs = [s for s in subtasks if s.get("completed")]

        if open_subs:
            lines.append(f"*Open blockers ({len(open_subs)}):*")
            for sub in open_subs[:8]:
                name = sub.get("name", "(unnamed)")
                due = sub.get("due_on") or "no due date"
                assignee = (sub.get("assignee") or {}).get("name") or "unassigned"
                link = sub.get("permalink_url") or ""
                if link:
                    lines.append(f"  - <{link}|{name}> -- {assignee}, due {due}")
                else:
                    lines.append(f"  - {name} -- {assignee}, due {due}")
            if len(open_subs) > 8:
                lines.append(f"  ... and {len(open_subs) - 8} more")
        else:
            lines.append("No open sub-task blockers.")

        lines.append(f"*Completed:* {len(done_subs)} of {len(subtasks)} sub-tasks done.")
    else:
        lines.append("No sub-tasks found on this task.")

    lines.append("")

    # Last comment age
    story = _get_latest_story(_REVALIDATION_TASK_GID)
    if story:
        raw_ts = story.get("created_at") or ""
        try:
            dt = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - dt).days
            author = (story.get("created_by") or {}).get("name") or "unknown"
            if age_days == 0:
                age_str = "today"
            elif age_days == 1:
                age_str = "yesterday"
            else:
                age_str = f"{age_days}d ago"
            lines.append(f"*Last comment:* {age_str} by {author}")
        except (ValueError, TypeError):
            lines.append("*Last comment:* (date parse error)")
    else:
        lines.append("*Last comment:* none on record")

    lines.append("")
    task_link = task.get("permalink_url") or ""
    if task_link:
        lines.append(f"<{task_link}|Open full task in Asana>")

    log.info(
        "lex_revalidation_status days_remaining=%d subtasks=%d open=%d",
        days_remaining,
        len(subtasks),
        len([s for s in subtasks if not s.get("completed")]) if subtasks else 0,
    )

    return "\n".join(lines)


def get_staff_pulse() -> str:
    """Return LEX staffing pulse from the Drive upload pipeline.

    BLOCKED -- depends on Sean/Jen DDD staffing + driver safety Drive upload
    pipeline. Until the pipeline folder is configured and the nightly sync is
    pointed at it, this tool returns a structured stub.

    Wire-up checklist (Harrison):
      1. Lock the Drive folder path for Sean/Jen uploads.
      2. Add STAFF_PULSE_DRIVE_FILE_ID to .env.
      3. Implement the actual read + parse in this function.
      4. Remove the BLOCKED stub below.
    """
    log.info("lex_staff_pulse called -- pipeline not yet configured (BLOCKED)")
    return (
        "Lex staffing pulse is not yet available. The staffing data pipeline (Sean + Jen "
        "Drive upload folder) has not been configured. Once the folder is set up and the "
        "nightly sync is pointed at it, this tool will return open positions, recent "
        "terminations, and training compliance counts. Ask Harrison to lock the Drive folder "
        "path to unblock this."
    )
