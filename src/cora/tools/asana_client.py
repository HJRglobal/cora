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


def format_tasks_for_llm(tasks: list[dict[str, Any]]) -> str:
    """Render task list as a string suitable for a tool_result content block.

    Format: one line per task. Optimized for Claude to skim + prioritize, not for pretty-print.
    """
    if not tasks:
        return "No incomplete tasks assigned in Asana."

    lines = [f"Found {len(tasks)} incomplete Asana task(s):"]
    for t in tasks:
        name = t.get("name", "(no name)")
        due = t.get("due_on") or t.get("due_at") or "no due date"

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

        lines.append(f"- [{due}] {name} ({project_str}){notes_preview}")

    return "\n".join(lines)
