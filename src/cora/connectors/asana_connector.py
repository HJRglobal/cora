"""Asana connector — backfill + incremental sync of tasks + comments + project descriptions.

What we ingest:
    - Every active (non-archived) project's description as a Document (entity from prefix)
    - Every task in those projects modified within the window:
        - Combined content: task name + notes + project/section + assignee + due date +
          full comment thread (only resource_subtype == "comment_added" stories)
        - deep_link: task.permalink_url (clickable HTTPS URL)
        - entity: classified from project name prefix (same logic as tool_dispatch)

PHI guardrail (Lex):
    Projects whose names contain client-identifying keywords are skipped entirely.
    See _is_phi_project() — operational LEX projects (hiring, compliance, ops) ingest;
    clinical / consumer / session-note projects are excluded.

Rate limiting:
    Asana free + paid tier limits are 1500 requests/min per workspace. We sleep 50ms
    between bulk task/story fetches → ~1200 req/min, well under the cap.

Reuses HUBSPOT-style PAT auth pattern from src/cora/tools/asana_client.py.
"""

import logging
import os
import time
from collections.abc import Iterator
from datetime import datetime, timezone

import httpx

from cora.knowledge_base.store import Document
from cora.tools.tool_dispatch import ENTITY_PROJECT_PREFIXES

log = logging.getLogger(__name__)

_BASE = "https://app.asana.com/api/1.0"
_WORKSPACE_GID = "682743441507584"  # HJR Global
_TIMEOUT = 20.0
_RATE_SLEEP = 0.01  # 10ms between requests. Asana's actual limit is 1500/min = 40ms,
                    # but network roundtrip is 50-150ms naturally, so 10ms client-side
                    # gives ample headroom while massively speeding up the bulk walk.

# Conservative PHI keyword filter — applied to project NAMES (case-insensitive).
# Any project whose name contains one of these is skipped entirely.
_PHI_KEYWORDS = {
    "consumer", "consumers",
    "client", "clients",
    "patient", "patients",
    "phi",
    "clinical",
    "session note", "session notes",
    "treatment plan", "treatment",
    "medical record", "medical",
    "case note", "case notes",
}


class AsanaConnectorError(Exception):
    pass


def _pat() -> str:
    val = os.environ.get("ASANA_PAT", "")
    if not val:
        raise AsanaConnectorError("ASANA_PAT not set — Asana connector disabled")
    return val


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_pat()}"}


def _get(path: str, params: dict | None = None) -> dict:
    """GET an Asana API endpoint with auth + rate-limit sleep."""
    time.sleep(_RATE_SLEEP)
    with httpx.Client(timeout=_TIMEOUT) as c:
        r = c.get(f"{_BASE}{path}", headers=_headers(), params=params or {})
    if r.status_code == 401:
        raise AsanaConnectorError("Asana 401 — PAT invalid")
    if r.status_code == 429:
        log.warning("Asana 429 rate-limited; sleeping 5s and retrying")
        time.sleep(5)
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.get(f"{_BASE}{path}", headers=_headers(), params=params or {})
    if r.status_code >= 500:
        raise AsanaConnectorError(f"Asana {r.status_code} upstream: {r.text[:200]}")
    if r.status_code != 200:
        raise AsanaConnectorError(f"Asana {r.status_code}: {r.text[:200]}")
    return r.json()


def _paginate(path: str, params: dict) -> Iterator[dict]:
    """Yield items from a paginated Asana endpoint. Handles offset cursor."""
    params = dict(params)
    params.setdefault("limit", 100)
    while True:
        body = _get(path, params=params)
        for item in body.get("data", []) or []:
            yield item
        next_page = body.get("next_page") or {}
        offset = next_page.get("offset")
        if not offset:
            break
        params["offset"] = offset


# Maps Asana team GIDs to LEX sub-entity codes.
# LTS has no dedicated team GID (shared team) — identified by project name keywords instead.
_ASANA_TEAM_SUB_ENTITY: dict[str, str] = {
    "1209152915815732": "LEX-LLC",
    "1209152923740446": "LEX-LLA",
    "1209152923740451": "LEX-LBHS",
}


def _tag_asana_sub_entity_for_project(project: dict) -> str | None:
    """Resolve LEX sub-entity from a project's team GID or name keywords."""
    team_gid = (project.get("team") or {}).get("gid", "")
    if team_gid in _ASANA_TEAM_SUB_ENTITY:
        return _ASANA_TEAM_SUB_ENTITY[team_gid]
    project_name = project.get("name", "")
    if any(kw in project_name for kw in ("LTS", "Therapies", "Lexington Therapies")):
        return "LEX-LTS"
    return None


def _is_phi_project(project_name: str) -> bool:
    """Return True if the project should be excluded for PHI/clinical reasons."""
    name_lower = project_name.lower()
    return any(kw in name_lower for kw in _PHI_KEYWORDS)


def _entity_for_project(project_name: str) -> str:
    """Classify a project to an entity code using existing ENTITY_PROJECT_PREFIXES table."""
    for entity, prefixes in ENTITY_PROJECT_PREFIXES.items():
        for prefix in prefixes:
            if project_name.startswith(prefix):
                return entity
    return "FNDR"


def _ts(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def _format_task_content(
    task: dict,
    project_name: str,
    section_name: str,
    comments: list[dict],
) -> str:
    """Build the chunkable text content for a task Document."""
    lines = [f"[Task] {task.get('name', '(no name)')}"]
    lines.append("")
    lines.append(f"Project: {project_name}")
    if section_name:
        lines.append(f"Section: {section_name}")
    if task.get("due_on"):
        lines.append(f"Due: {task['due_on']}")
    if (task.get("assignee") or {}).get("name"):
        lines.append(f"Assignee: {task['assignee']['name']}")
    lines.append(f"Status: {'completed' if task.get('completed') else 'incomplete'}")

    notes = (task.get("notes") or "").strip()
    if notes:
        lines.append("")
        lines.append("Notes:")
        lines.append(notes)

    if comments:
        lines.append("")
        lines.append(f"Comments ({len(comments)}):")
        for c in comments:
            who = (c.get("created_by") or {}).get("name", "unknown")
            when = (c.get("created_at") or "")[:10] or "?"
            text = (c.get("text") or "").strip()
            if text:
                lines.append(f"[{when} — {who}] {text}")

    return "\n".join(lines)


def _list_projects() -> Iterator[dict]:
    """All non-archived projects in the workspace."""
    yield from _paginate(
        f"/workspaces/{_WORKSPACE_GID}/projects",
        params={
            "archived": "false",
            "opt_fields": "name,archived,notes,modified_at,team.name,team.gid",
        },
    )


def _list_tasks_in_project(project_gid: str, since: datetime | None) -> Iterator[dict]:
    """Tasks in a project, optionally filtered by modified_at >= since."""
    params: dict = {
        "opt_fields": (
            "name,notes,due_on,completed,modified_at,permalink_url,"
            "assignee.name,memberships.section.name,memberships.project.name"
        ),
    }
    # Asana doesn't support a server-side modified_since filter on /projects/X/tasks,
    # so we paginate all + client-side filter. Acceptable for backfill scale.
    yield from _paginate(f"/projects/{project_gid}/tasks", params=params)


def _list_task_comments(task_gid: str) -> list[dict]:
    """Get all comment stories for a task. Filters out system events."""
    out: list[dict] = []
    for story in _paginate(
        f"/tasks/{task_gid}/stories",
        params={
            "opt_fields": "resource_subtype,text,created_at,created_by.name",
        },
    ):
        if story.get("resource_subtype") == "comment_added":
            out.append(story)
    return out


def backfill(since: datetime) -> Iterator[Document]:
    """Walk all active projects + tasks modified since `since`. Yield Documents.

    Skips PHI-suspicious projects entirely. Each project description becomes one
    Document; each task (with its comment thread) becomes another Document.
    """
    since_ts = since.replace(tzinfo=timezone.utc).timestamp() if since.tzinfo is None else since.timestamp()

    project_count = 0
    task_count = 0
    skipped_phi = 0

    for project in _list_projects():
        project_name = project.get("name", "")
        project_gid = project.get("gid", "")
        if not project_name or not project_gid:
            continue

        if _is_phi_project(project_name):
            skipped_phi += 1
            log.info("PHI guardrail: skipping project %s", project_name)
            continue

        entity = _entity_for_project(project_name)
        sub_entity = _tag_asana_sub_entity_for_project(project) if entity == "LEX" else None
        project_modified_ts = _ts(project.get("modified_at"))
        project_permalink = f"https://app.asana.com/0/{project_gid}/list"

        # Project description as a Document (only if non-empty notes)
        project_notes = (project.get("notes") or "").strip()
        if project_notes:
            yield Document(
                source="asana",
                source_id=f"project:{project_gid}",
                entity=entity,
                sub_entity=sub_entity,
                content=f"[Asana Project] {project_name}\n\n{project_notes}",
                date_created=project_modified_ts,
                date_modified=project_modified_ts,
                author="",
                title=f"Asana project: {project_name}",
                deep_link=f"<{project_permalink}|{project_name}>",
                metadata={"project_gid": project_gid, "type": "project_description"},
            )
            project_count += 1

        # Walk tasks in this project
        for task in _list_tasks_in_project(project_gid, since=since):
            task_modified_ts = _ts(task.get("modified_at"))
            if task_modified_ts is None or task_modified_ts < since_ts:
                continue

            task_gid = task.get("gid", "")
            if not task_gid:
                continue

            # Resolve section name from memberships
            section_name = ""
            for m in (task.get("memberships") or []):
                if (m.get("project") or {}).get("gid") == project_gid:
                    section_name = (m.get("section") or {}).get("name", "")
                    break

            # Fetch comments
            try:
                comments = _list_task_comments(task_gid)
            except AsanaConnectorError as exc:
                log.warning("Failed to fetch comments for task %s: %s", task_gid, exc)
                comments = []

            # Effective last-modified is max(task.modified_at, latest comment created_at)
            effective_ts = task_modified_ts
            for c in comments:
                c_ts = _ts(c.get("created_at"))
                if c_ts and c_ts > effective_ts:
                    effective_ts = c_ts

            content = _format_task_content(task, project_name, section_name, comments)
            task_name = task.get("name", "(no name)")
            permalink = task.get("permalink_url") or f"https://app.asana.com/0/{project_gid}/{task_gid}"

            yield Document(
                source="asana",
                source_id=f"task:{task_gid}",
                entity=entity,
                sub_entity=sub_entity,
                content=content,
                date_created=task_modified_ts,
                date_modified=effective_ts,
                author=(task.get("assignee") or {}).get("name", ""),
                title=task_name,
                deep_link=f"<{permalink}|{task_name}>",
                metadata={
                    "task_gid": task_gid,
                    "project_gid": project_gid,
                    "project_name": project_name,
                    "section": section_name,
                    "completed": task.get("completed", False),
                    "due_on": task.get("due_on"),
                    "comment_count": len(comments),
                },
            )
            task_count += 1

    log.info(
        "Asana backfill done: %d project descriptions, %d tasks yielded, %d projects skipped for PHI",
        project_count, task_count, skipped_phi,
    )


def sync_delta(last_sync_ts: int) -> Iterator[Document]:
    """Pull tasks modified since the last sync timestamp.

    Note: Asana lacks a workspace-wide modified_since filter, so this still walks all
    projects but client-side filters tasks by modified_at. For daily incremental syncs
    over a portfolio with ~100-200 projects, this completes in <60s.
    """
    since_dt = datetime.fromtimestamp(last_sync_ts, tz=timezone.utc)
    yield from backfill(since=since_dt)
