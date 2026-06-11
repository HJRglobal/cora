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
from typing import Any

import httpx

log = logging.getLogger(__name__)

_BASE = "https://app.asana.com/api/1.0"
_WORKSPACE_GID = "682743441507584"  # HJR Global workspace
_TIMEOUT = 10.0
_DEFAULT_MAX_TASKS = 25


class AsanaClientError(Exception):
    """Raised when an Asana API call fails."""


def _pat() -> str:
    val = os.environ.get("ASANA_PAT", "")
    if not val:
        raise AsanaClientError("ASANA_PAT not set in environment — Asana tool-use disabled")
    return val


def get_user_tasks(user_gid: str, max_tasks: int = _DEFAULT_MAX_TASKS) -> list[dict[str, Any]]:
    """Fetch incomplete tasks assigned to a user.

    Returns a list of task dicts. Empty list if no incomplete tasks.
    Raises AsanaClientError on auth / network / 5xx failure.
    """
    params = {
        "assignee": user_gid,
        "workspace": _WORKSPACE_GID,
        "completed_since": "now",  # incomplete-only filter
        "limit": max_tasks,
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
    headers = {"Authorization": f"Bearer {_pat()}"}
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

    return r.json().get("data", []) or []


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
            return (
                f"No incomplete {entity_scope}-tagged tasks assigned. "
                f"User has {total_before_filter} task(s) across other entities — "
                f"they can ask in a #fndr-* channel to see the full list."
            )
        return "No incomplete tasks assigned in Asana."

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
            f"{total_before_filter - len(tasks)} other tasks exist across other entities — "
            f"user can ask in a #fndr-* channel to see them all.]"
        )

    return "\n".join(lines)
    