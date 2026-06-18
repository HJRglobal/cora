"""Asana REST client — single-PAT auth model.

Phase 2 #7 MVP scope:
- GET /tasks (filtered by assignee + workspace + incomplete) — read path
- POST /tasks — create_task added 2026-05-21 (first write capability;
  read-only doctrine deliberately reversed per Harrison decision after
  5/21 Lex Progress meeting verbal commitments)
- One workspace hard-coded (HJR Global, gid 682743441507584)
- PAT inherited from .env (ASANA_PAT)

Write doctrine (LOCKED 2026-05-21):
- All writes go through the staged-write pattern: Claude MUST show the
  user a draft preview and get explicit confirmation BEFORE calling the
  write tool. The `confirmed=true` parameter on create_task is the
  contract enforcement — it's set only after Claude observes the user's
  approval in conversation.
- Writes are logged at INFO with the requesting slack_user_id, the
  created task's GID, and a permalink.
- Workspace is fixed to HJR Global; cross-workspace writes are not
  supported in v1.

Phase 3+ paths (deferred):
- OAuth per-user (replaces single PAT — writes attributed to the asker
  not Harrison)
- search_tasks, update_task, complete_task
- Project resolution by name (today: optional project_gid passed in,
  no name-to-gid resolver)
"""

import logging
import os
import re
from datetime import date, datetime, timedelta
from typing import Any

import httpx

log = logging.getLogger(__name__)

_BASE = "https://app.asana.com/api/1.0"
_WORKSPACE_GID = "682743441507584"  # HJR Global workspace
_TIMEOUT = 10.0
_DEFAULT_MAX_TASKS = 25
# Asana rejects limit > 100 with a 400. max_tasks above this is served by
# paginating with the next_page offset token.
_API_MAX_LIMIT = 100


class AsanaClientError(Exception):
    """Raised when an Asana API call fails."""


def _pat() -> str:
    val = os.environ.get("ASANA_PAT", "")
    if not val:
        raise AsanaClientError("ASANA_PAT not set in environment — Asana tool-use disabled")
    return val


def get_user_tasks(user_gid: str, max_tasks: int = _DEFAULT_MAX_TASKS) -> list[dict[str, Any]]:
    """Fetch incomplete tasks assigned to a user.

    Paginates when max_tasks > 100 (Asana 400s on limit > 100 — the 5/31
    reconciliation "scale increase" to max_tasks=200 silently broke every
    fetch until 2026-06-11).

    Returns a list of task dicts. Empty list if no incomplete tasks.
    Raises AsanaClientError on auth / network / 5xx failure.
    """
    headers = {"Authorization": f"Bearer {_pat()}"}
    tasks: list[dict[str, Any]] = []
    offset: str | None = None

    while len(tasks) < max_tasks:
        params = {
            "assignee": user_gid,
            "workspace": _WORKSPACE_GID,
            "completed_since": "now",  # incomplete-only filter
            "limit": min(_API_MAX_LIMIT, max_tasks - len(tasks)),
            "opt_fields": ",".join([
                "name",
                "due_on",
                "due_at",
                "completed",
                "assignee.gid",   # needed for user_identity reverse lookup (@mention in digests)
                "assignee.name",  # human-readable fallback when reverse lookup misses
                "projects.name",
                "memberships.section.name",
                "memberships.project.name",
                "notes",
                "permalink_url",  # Slack deep link — user clicks to edit in Asana
            ]),
        }
        if offset:
            params["offset"] = offset

        try:
            with httpx.Client(timeout=_TIMEOUT) as c:
                r = c.get(f"{_BASE}/tasks", params=params, headers=headers)
        except httpx.RequestError as exc:
            raise AsanaClientError(f"Asana network error: {exc}") from exc

        if r.status_code == 401:
            raise AsanaClientError("Asana 401 — PAT invalid or revoked")
        if r.status_code == 403:
            raise AsanaClientError(f"Asana 403 — PAT lacks permission for user {user_gid}")
        if r.status_code >= 500:
            raise AsanaClientError(f"Asana {r.status_code} — upstream error: {r.text[:200]}")
        if r.status_code != 200:
            raise AsanaClientError(f"Asana {r.status_code}: {r.text[:200]}")

        body = r.json()
        tasks.extend(body.get("data", []) or [])
        offset = (body.get("next_page") or {}).get("offset")
        if not offset:
            break

    return tasks[:max_tasks]


def get_project_tasks(project_gid: str, max_tasks: int = _API_MAX_LIMIT) -> list[dict[str, Any]]:
    """Fetch incomplete tasks in a project (GET /projects/{gid}/tasks).

    Read-only. `completed_since=now` returns only incomplete tasks. Same opt_fields
    + pagination contract as get_user_tasks. Used by the F3E daily ecom+ops brief to
    surface open Run-2 production tasks regardless of assignee.

    Returns a list of task dicts (empty if none). Raises AsanaClientError on auth /
    network / 5xx failure.
    """
    headers = {"Authorization": f"Bearer {_pat()}"}
    tasks: list[dict[str, Any]] = []
    offset: str | None = None

    while len(tasks) < max_tasks:
        params: dict[str, Any] = {
            "completed_since": "now",  # incomplete-only filter
            "limit": min(_API_MAX_LIMIT, max_tasks - len(tasks)),
            "opt_fields": ",".join([
                "name",
                "due_on",
                "due_at",
                "completed",
                "assignee.name",
                "permalink_url",
            ]),
        }
        if offset:
            params["offset"] = offset

        try:
            with httpx.Client(timeout=_TIMEOUT) as c:
                r = c.get(f"{_BASE}/projects/{project_gid}/tasks", params=params, headers=headers)
        except httpx.RequestError as exc:
            raise AsanaClientError(f"Asana network error: {exc}") from exc

        if r.status_code == 401:
            raise AsanaClientError("Asana 401 — PAT invalid or revoked")
        if r.status_code == 403:
            raise AsanaClientError(f"Asana 403 — PAT lacks permission for project {project_gid}")
        if r.status_code >= 500:
            raise AsanaClientError(f"Asana {r.status_code} — upstream error: {r.text[:200]}")
        if r.status_code != 200:
            raise AsanaClientError(f"Asana {r.status_code}: {r.text[:200]}")

        body = r.json()
        tasks.extend(body.get("data", []) or [])
        offset = (body.get("next_page") or {}).get("offset")
        if not offset:
            break

    return tasks[:max_tasks]


def create_task(
    *,
    name: str,
    assignee_gid: str | None = None,
    project_gid: str | None = None,
    notes: str | None = None,
    due_on: str | None = None,
) -> dict[str, Any]:
    """Create a task in the HJR Global Asana workspace.

    Required: `name` (the task title). All other fields optional.

    Returns the created task dict (including gid + permalink_url).
    Raises AsanaClientError on auth / network / 4xx / 5xx failure.

    Asana API note: Tasks must belong to either a workspace or a project.
    We always set the workspace; project is optional. If no project is
    given, the task lands in the assignee's "My Tasks" within the
    workspace.
    """
    if not name or not name.strip():
        raise AsanaClientError("create_task requires a non-empty `name`")

    data: dict[str, Any] = {
        "name": name.strip(),
        "workspace": _WORKSPACE_GID,
    }
    if assignee_gid:
        data["assignee"] = str(assignee_gid)
    if project_gid:
        # Asana wants projects as an array even for one project
        data["projects"] = [str(project_gid)]
    if notes:
        data["notes"] = notes
    if due_on:
        # Light validation: must be YYYY-MM-DD shape
        if len(due_on) != 10 or due_on[4] != "-" or due_on[7] != "-":
            raise AsanaClientError(
                f"create_task: due_on must be YYYY-MM-DD format, got {due_on!r}"
            )
        data["due_on"] = due_on

    headers = {
        "Authorization": f"Bearer {_pat()}",
        "Content-Type": "application/json",
    }
    opt_fields = ",".join([
        "name",
        "assignee.name",
        "due_on",
        "projects.name",
        "notes",
        "permalink_url",
    ])

    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.post(
                f"{_BASE}/tasks",
                params={"opt_fields": opt_fields},
                json={"data": data},
                headers=headers,
            )
    except httpx.RequestError as exc:
        raise AsanaClientError(f"Asana network error: {exc}") from exc

    if r.status_code == 401:
        raise AsanaClientError("Asana 401 — PAT invalid or revoked")
    if r.status_code == 403:
        raise AsanaClientError(
            f"Asana 403 — PAT lacks permission to create tasks (or "
            f"to assign to the requested user)"
        )
    if r.status_code == 400:
        # Bad request — likely an invalid GID. Surface Asana's error message.
        raise AsanaClientError(
            f"Asana 400 — bad request: {r.text[:400]}"
        )
    if r.status_code >= 500:
        raise AsanaClientError(f"Asana {r.status_code} — upstream error: {r.text[:200]}")
    if r.status_code not in (200, 201):
        raise AsanaClientError(f"Asana {r.status_code}: {r.text[:200]}")

    return r.json().get("data") or {}


def complete_task(task_gid: str) -> dict[str, Any]:
    """Mark an Asana task as complete.

    Returns the updated task dict. Raises AsanaClientError on failure.
    """
    if not task_gid or not task_gid.strip():
        raise AsanaClientError("complete_task requires a non-empty task_gid")

    headers = {
        "Authorization": f"Bearer {_pat()}",
        "Content-Type": "application/json",
    }
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.put(
                f"{_BASE}/tasks/{task_gid}",
                json={"data": {"completed": True}},
                headers=headers,
            )
    except httpx.RequestError as exc:
        raise AsanaClientError(f"Asana network error: {exc}") from exc

    if r.status_code == 401:
        raise AsanaClientError("Asana 401 — PAT invalid or revoked")
    if r.status_code == 403:
        raise AsanaClientError(f"Asana 403 — cannot complete task {task_gid}")
    if r.status_code == 404:
        raise AsanaClientError(f"Asana 404 — task {task_gid} not found")
    if r.status_code >= 500:
        raise AsanaClientError(f"Asana {r.status_code} — upstream error: {r.text[:200]}")
    if r.status_code not in (200, 201):
        raise AsanaClientError(f"Asana {r.status_code}: {r.text[:200]}")

    return r.json().get("data") or {}


def create_task_comment(task_gid: str, text: str) -> dict[str, Any]:
    """Post a comment (story) on an Asana task.

    Returns the story dict. Raises AsanaClientError on failure.
    """
    if not task_gid or not task_gid.strip():
        raise AsanaClientError("create_task_comment requires a non-empty task_gid")
    if not text or not text.strip():
        raise AsanaClientError("create_task_comment requires non-empty text")

    headers = {
        "Authorization": f"Bearer {_pat()}",
        "Content-Type": "application/json",
    }
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.post(
                f"{_BASE}/tasks/{task_gid}/stories",
                json={"data": {"text": text}},
                headers=headers,
            )
    except httpx.RequestError as exc:
        raise AsanaClientError(f"Asana network error: {exc}") from exc

    if r.status_code == 401:
        raise AsanaClientError("Asana 401 — PAT invalid or revoked")
    if r.status_code == 403:
        raise AsanaClientError(f"Asana 403 — cannot comment on task {task_gid}")
    if r.status_code == 404:
        raise AsanaClientError(f"Asana 404 — task {task_gid} not found")
    if r.status_code >= 500:
        raise AsanaClientError(f"Asana {r.status_code} — upstream error: {r.text[:200]}")
    if r.status_code not in (200, 201):
        raise AsanaClientError(f"Asana {r.status_code}: {r.text[:200]}")

    return r.json().get("data") or {}


def get_task_completion(task_gid: str) -> dict[str, Any]:
    """Fetch a task's live completion state: {"completed": bool, "completed_at": str|None}.

    Used by the closed-task nudge guard (nudge_ledger.closed_task_guard) to
    re-check completion at fire time -- the candidate list can be stale by the
    time a nudge actually posts, and other comment sources don't filter on
    completion at all (2026-06-11 Hannah report: daily nudges on a task closed
    a year prior).

    Raises AsanaClientError on any failure -- the caller decides fail direction.
    """
    if not task_gid or not task_gid.strip():
        raise AsanaClientError("get_task_completion requires a non-empty task_gid")

    headers = {"Authorization": f"Bearer {_pat()}"}
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.get(
                f"{_BASE}/tasks/{task_gid}",
                params={"opt_fields": "completed,completed_at"},
                headers=headers,
            )
    except httpx.RequestError as exc:
        raise AsanaClientError(f"Asana network error: {exc}") from exc

    if r.status_code == 404:
        raise AsanaClientError(f"Asana 404 — task {task_gid} not found")
    if r.status_code != 200:
        raise AsanaClientError(f"Asana {r.status_code}: {r.text[:200]}")

    data = r.json().get("data") or {}
    return {
        "completed": bool(data.get("completed")),
        "completed_at": data.get("completed_at"),
    }


def find_recent_duplicate_task(name: str, within_days: int = 7) -> str | None:
    """Return the GID of an existing OPEN task with the same name created recently.

    Creation-time dedup guard used by Meeting Action Capture: before creating an
    auto-captured action item, check whether the bot already created an identical
    open task in the last `within_days` days. Prevents duplicate creation when a
    prior run crashed mid-way (after creating tasks, before persisting the
    watermark) and the same meeting is reprocessed.

    Matching is case-insensitive on the full (stripped) task name. Only OPEN
    (incomplete) tasks count -- a closed dup shouldn't block a fresh re-raise.

    Fail-open: on any API/network error, returns None (allows creation). We'd
    rather risk a rare duplicate than silently suppress a legitimate task.
    """
    if not name or not name.strip():
        return None

    target = name.strip().lower()
    headers = {"Authorization": f"Bearer {_pat()}"}

    # Phase 1: typeahead to find candidate tasks by name (cheap, name-only).
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.get(
                f"{_BASE}/workspaces/{_WORKSPACE_GID}/typeahead",
                params={
                    "resource_type": "task",
                    "query": name.strip()[:100],
                    "count": 20,
                    "opt_fields": "name",
                },
                headers=headers,
            )
        if r.status_code != 200:
            log.warning("dedup typeahead %s: %s", r.status_code, r.text[:120])
            return None
        candidates = r.json().get("data", []) or []
    except httpx.RequestError as exc:
        log.warning("dedup typeahead network error: %s", exc)
        return None

    # Keep only exact (case-insensitive) name matches.
    matches = [
        c.get("gid")
        for c in candidates
        if (c.get("name") or "").strip().lower() == target and c.get("gid")
    ][:5]
    if not matches:
        return None

    # Phase 2: confirm each match is open AND created within the window.
    cutoff = _utcnow() - within_days * 86400
    for gid in matches:
        try:
            with httpx.Client(timeout=_TIMEOUT) as c:
                r = c.get(
                    f"{_BASE}/tasks/{gid}",
                    params={"opt_fields": "completed,created_at,name"},
                    headers=headers,
                )
            if r.status_code != 200:
                continue
            t = r.json().get("data") or {}
            if t.get("completed"):
                continue
            created_ts = _parse_iso8601(t.get("created_at"))
            if created_ts is not None and created_ts >= cutoff:
                log.info("dedup hit: existing open task gid=%s name=%r", gid, name)
                return gid
        except httpx.RequestError as exc:
            log.warning("dedup task fetch network error for %s: %s", gid, exc)
            continue

    return None


def set_task_custom_fields(task_gid: str, custom_fields: dict[str, str]) -> bool:
    """Best-effort set of enum/text custom fields on a task.

    `custom_fields` maps custom_field_gid -> value (an enum option GID for enum
    fields, or a plain string for text fields).

    Returns True on success, False on any failure. NEVER raises -- custom-field
    failures (e.g. the field isn't attached to the task's project) must not
    abort the surrounding flow. The task itself is already created; tagging is
    a best-effort enrichment.
    """
    if not task_gid or not custom_fields:
        return False

    headers = {
        "Authorization": f"Bearer {_pat()}",
        "Content-Type": "application/json",
    }
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.put(
                f"{_BASE}/tasks/{task_gid}",
                json={"data": {"custom_fields": custom_fields}},
                headers=headers,
            )
        if r.status_code in (200, 201):
            return True
        log.warning(
            "set_task_custom_fields %s for task %s: %s",
            r.status_code, task_gid, r.text[:200],
        )
        return False
    except httpx.RequestError as exc:
        log.warning("set_task_custom_fields network error for %s: %s", task_gid, exc)
        return False


def list_project_custom_field_gids(project_gid: str) -> set[str]:
    """Return the set of custom-field GIDs currently attached to a project.

    Used to make custom-field attachment idempotent (skip fields already on the
    project). Raises AsanaClientError on failure.
    """
    headers = {"Authorization": f"Bearer {_pat()}"}
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.get(
                f"{_BASE}/projects/{project_gid}",
                params={"opt_fields": "custom_field_settings.custom_field.gid"},
                headers=headers,
            )
    except httpx.RequestError as exc:
        raise AsanaClientError(f"Asana network error: {exc}") from exc
    if r.status_code != 200:
        raise AsanaClientError(f"Asana {r.status_code}: {r.text[:200]}")
    settings = (r.json().get("data") or {}).get("custom_field_settings") or []
    return {
        str((s.get("custom_field") or {}).get("gid", ""))
        for s in settings
        if (s.get("custom_field") or {}).get("gid")
    }


def add_project_custom_field_setting(
    project_gid: str, custom_field_gid: str, *, is_important: bool = True
) -> dict[str, Any]:
    """Attach an EXISTING custom field to a project (POST addCustomFieldSetting).

    Passes the exact custom_field GID, so it can NEVER create a duplicate field
    (the failure mode a UI/Chrome-Agent attach risks). Idempotency is the
    caller's job (see list_project_custom_field_gids). Raises AsanaClientError
    on failure; returns the created custom_field_setting dict.
    """
    headers = {"Authorization": f"Bearer {_pat()}", "Content-Type": "application/json"}
    body = {"data": {"custom_field": str(custom_field_gid), "is_important": is_important}}
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.post(
                f"{_BASE}/projects/{project_gid}/addCustomFieldSetting",
                json=body,
                headers=headers,
            )
    except httpx.RequestError as exc:
        raise AsanaClientError(f"Asana network error: {exc}") from exc
    if r.status_code not in (200, 201):
        raise AsanaClientError(
            f"Asana addCustomFieldSetting {r.status_code} for project {project_gid} "
            f"field {custom_field_gid}: {r.text[:200]}"
        )
    return r.json().get("data") or {}


def _utcnow() -> int:
    """Current UTC epoch seconds (wrapper for test patchability)."""
    import time as _time
    return int(_time.time())


def _parse_iso8601(value: str | None) -> int | None:
    """Parse an ISO8601 timestamp (Asana created_at) to UTC epoch seconds."""
    if not value:
        return None
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def format_created_task_for_llm(task: dict[str, Any]) -> str:
    """Render a freshly-created task as a Slack-mrkdwn confirmation line."""
    name = task.get("name", "(no name)")
    permalink = task.get("permalink_url") or ""
    assignee = (task.get("assignee") or {}).get("name") or "(unassigned)"
    due = task.get("due_on") or "(no due date)"
    projects = ", ".join(p.get("name", "") for p in (task.get("projects") or [])) or "(no project — in assignee's My Tasks)"

    if permalink:
        name_link = f"<{permalink}|{name}>"
    else:
        name_link = name

    # Return ready-to-post Slack text. Claude MUST echo this verbatim as its
    # reply — never produce an empty response after a successful write.
    return (
        f"WRITE_CONFIRMED — post the following lines as your entire response "
        f"(no preamble, no meta-commentary, just these lines):\n\n"
        f"Task created: {name_link}\n"
        f"Assignee: {assignee} · Due: {due} · Project: {projects}"
    )


def sort_tasks_due_first(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Due-dated tasks first (ascending), then no-due-date tasks in input order.

    2026-06-11 exit-gate nit (Shaun's plate): a long no-due-date list rendered
    ahead of dated work, so any narration cutoff cost the urgent tail. Stable
    sort: ties keep the API's order.
    """
    def _key(t: dict[str, Any]) -> tuple[int, str]:
        due = t.get("due_on") or t.get("due_at") or ""
        return (0, str(due)) if due else (1, "")

    return sorted(tasks, key=_key)


# A task whose due date is more than this many days in the past is treated as
# abandoned backlog. The morning brief surfaced long-dead goal-tracking tasks
# ("Sales & Revenue Goals due 2025-02-04", "HJR Podcast due 2025-01-31") every
# single day (N7 / Harrison #1). A task explicitly flagged P0 is kept no matter
# how overdue -- a genuinely critical item must still surface.
_DEFAULT_STALE_OVERDUE_DAYS = 90
_P0_RE = re.compile(r"\bP0\b", re.IGNORECASE)


def _parse_due_date(raw: str) -> date | None:
    """Parse an Asana due_on ('YYYY-MM-DD') or due_at (ISO 8601) into a date.

    Returns None for empty/unparseable input -- the caller KEEPS such tasks
    (we never drop a task we cannot confidently date).
    """
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except (ValueError, TypeError):
        return None


def _task_is_high_priority(task: dict[str, Any]) -> bool:
    """True if the task name or notes carries an explicit, word-bounded P0 marker."""
    text = f"{task.get('name') or ''} {task.get('notes') or ''}"
    return bool(_P0_RE.search(text))


def drop_stale_tasks(
    tasks: list[dict[str, Any]],
    *,
    max_overdue_days: int = _DEFAULT_STALE_OVERDUE_DAYS,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Drop tasks whose due date is more than ``max_overdue_days`` in the past.

    KEPT (never dropped): tasks with no due date, future or recently-overdue
    tasks, tasks whose due date cannot be parsed, and any task explicitly
    flagged P0. This removes long-abandoned goal-tracking artifacts that
    cluttered the morning brief every day (N7 / Harrison #1) without touching
    live work. Opt-in per caller -- the on-demand plate tool keeps everything;
    the daily brief opts in.
    """
    if today is None:
        today = datetime.now().date()
    cutoff = today - timedelta(days=max_overdue_days)
    kept: list[dict[str, Any]] = []
    for t in tasks:
        due = _parse_due_date(t.get("due_on") or t.get("due_at") or "")
        if due is None or due >= cutoff or _task_is_high_priority(t):
            kept.append(t)
    return kept


def format_tasks_for_llm(
    tasks: list[dict[str, Any]],
    entity_scope: str | None = None,
    total_before_filter: int | None = None,
) -> str:
    """Render task list as a string suitable for a tool_result content block.

    Format: one line per task. Optimized for Claude to skim + prioritize, not for pretty-print.

    entity_scope: if set (e.g. "OSN"), prepends/appends a scope note so Claude knows the
    list has been filtered to a specific entity. total_before_filter is the count before
    the entity filter ran — used to render "showing N of M" framing when filtering reduced
    the count.
    """
    # Empty-list case
    if not tasks:
        if entity_scope and total_before_filter and total_before_filter > 0:
            # 2026-06-11 exit-gate nit: never point a non-founder at #fndr-*
            # channels they cannot access -- state the fact, skip the advice.
            return (
                f"No incomplete {entity_scope}-tagged tasks assigned. "
                f"User has {total_before_filter} task(s) across other entities, "
                f"outside this channel's scope."
            )
        return "No incomplete tasks assigned in Asana."

    tasks = sort_tasks_due_first(tasks)

    # Header — vary by whether scope filter is active
    if entity_scope and total_before_filter and total_before_filter > len(tasks):
        header = (
            f"Found {len(tasks)} incomplete Asana task(s) tagged for {entity_scope} "
            f"(filtered from {total_before_filter} total assigned tasks):"
        )
    elif entity_scope:
        header = f"Found {len(tasks)} incomplete Asana task(s) tagged for {entity_scope}:"
    else:
        header = f"Found {len(tasks)} incomplete Asana task(s):"

    lines = [header]
    lines.append(
        "(Task names below are Slack-formatted hyperlinks — preserve the `<url|name>` "
        "syntax verbatim in your reply so the user can click through to edit in Asana.)"
    )
    if len(tasks) > 10:
        lines.append(
            "(Long list: reproduce each task line verbatim and add no extra "
            "commentary, so the full list fits in the reply.)"
        )
    for t in tasks:
        name = t.get("name", "(no name)")
        due = t.get("due_on") or t.get("due_at") or "no due date"
        permalink = t.get("permalink_url") or ""

        # Resolve project / section from memberships (richer than top-level projects)
        memberships = t.get("memberships") or []
        proj_section = []
        for m in memberships:
            proj = (m.get("project") or {}).get("name") or ""
            section = (m.get("section") or {}).get("name") or ""
            if proj:
                proj_section.append(f"{proj}" + (f" / {section}" if section else ""))
        if not proj_section:
            # fallback to flat projects list
            proj_section = [p.get("name", "") for p in (t.get("projects") or [])]
        project_str = " | ".join(p for p in proj_section if p) or "no project"

        # Truncate notes to a preview
        notes = (t.get("notes") or "").replace("\n", " ").strip()
        notes_preview = f" — {notes[:120]}..." if len(notes) > 120 else (f" — {notes}" if notes else "")

        # Wrap task name in Slack mrkdwn hyperlink (renders as clickable in Slack)
        if permalink:
            name_with_link = f"<{permalink}|{name}>"
        else:
            name_with_link = name

        lines.append(f"- [{due}] {name_with_link} ({project_str}){notes_preview}")

    # Footer for scoped results — helps the LLM mention the scope to the user
    if entity_scope and total_before_filter and total_before_filter > len(tasks):
        lines.append("")
        lines.append(
            f"[Scope: showing {entity_scope}-tagged tasks only. "
            f"{total_before_filter - len(tasks)} other task(s) exist outside "
            f"this channel's scope.]"
        )

    return "\n".join(lines)
    