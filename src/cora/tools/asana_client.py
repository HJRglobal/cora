"""Asana REST client — read-only, single-PAT auth model.

Phase 2 #7 MVP scope:
- One endpoint: GET /tasks (filtered by assignee + workspace + incomplete)
- One workspace hard-coded (HJR Global, gid 682743441507584)
- PAT inherited from .env (ASANA_PAT)
- No write methods (deliberate — write-back is a different risk class)

Phase 3+ paths (deferred):
- OAuth per-user (replaces single PAT)
- Project filtering by entity (cross-entity scope rules)
- Tool: search_tasks, get_task, create_task
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
