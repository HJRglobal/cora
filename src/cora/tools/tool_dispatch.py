"""Tool dispatch layer for Claude tool-use loop.

Holds the tool catalog (name → JSONSchema + python callable). The catalog is
deliberately small — one tool today, expand as more land.

The dispatcher resolves a Claude `tool_use` block to a tool_result string.
Failures are caught and rendered as error tool_results so the model can react
gracefully instead of crashing the conversation.

Cross-entity scope rules: when the asking channel maps to a specific entity
(F3E, LEX, OSN, BDM), the tool filters tasks down to projects tagged for
that entity. FNDR channels (founder-level + catch-all) see everything.
"""

import logging
from pathlib import Path
from typing import Any, Callable

import yaml

from . import asana_client

log = logging.getLogger(__name__)

# Path to slack→asana mapping. Resolves relative to repo root (parent of src/).
_REPO_ROOT = Path(__file__).resolve().parents[3]
_MAP_PATH = _REPO_ROOT / "data" / "maps" / "slack-to-asana.yaml"


# --- Entity scope filter ---
#
# Asana projects in the HJR Global workspace are named with an entity prefix
# (per _shared/playbooks/naming-conventions.md). Tasks belong to one or more
# projects; we check each project's name against the entity's prefix list.
#
# A task counts as "belonging to entity X" if ANY of its projects starts with
# a prefix in ENTITY_PROJECT_PREFIXES[X]. Cross-project tasks may appear in
# multiple entity scopes (deliberately over-inclusive — better than dropping
# legitimate work).
ENTITY_PROJECT_PREFIXES: dict[str, list[str]] = {
    "F3E": ["[F3E]", "[F3 ", "[F3-", "[F3C]"],         # F3 Energy + F3 Community
    "LEX": ["[LEX]", "[LEX-"],                          # LEX-LLC, LEX-LLA, LEX-LBHS
    "OSN": ["[OSN]"],
    "BDM": ["[BDM]"],
    "UFL": ["[UFL]"],
    "HJRP": ["[HJRP]", "[HJRP-"],                       # HJRP, Cinema Lanes, LCI
    "HJRPROD": ["[HJRPROD]", "[POD]", "[FF]", "[HJR-PB]", "[CHK]", "[CHB]"],
    "HJRG": ["[HJRG]"],
    "FNDR": [],  # FNDR = no filter, return all
}


def _filter_tasks_by_entity(
    tasks: list[dict[str, Any]], entity: str
) -> list[dict[str, Any]]:
    """Filter tasks to those tagged for the given entity. Returns input unchanged for FNDR."""
    if entity == "FNDR" or entity not in ENTITY_PROJECT_PREFIXES:
        return tasks
    prefixes = ENTITY_PROJECT_PREFIXES[entity]
    if not prefixes:
        return tasks

    out = []
    for t in tasks:
        # Collect every project name attached to this task (memberships + flat projects)
        memberships = t.get("memberships") or []
        proj_names = [(m.get("project") or {}).get("name", "") for m in memberships]
        proj_names += [p.get("name", "") for p in (t.get("projects") or [])]
        if any(
            pname.startswith(prefix) for pname in proj_names for prefix in prefixes
        ):
            out.append(t)
    return out


def _load_slack_asana_map() -> dict[str, dict[str, Any]]:
    """Load the mapping, returning a dict keyed by slack_user_id."""
    if not _MAP_PATH.exists():
        log.warning("slack-to-asana.yaml not found at %s", _MAP_PATH)
        return {}
    with open(_MAP_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    users = data.get("users") or []
    return {u["slack_user_id"]: u for u in users if u.get("slack_user_id")}


# --- Tool implementations (bound to the requesting slack user + entity scope via dispatch) ---


def _tool_get_my_tasks(slack_user_id: str, entity: str, _input: dict) -> str:
    """Resolve user → Asana gid → fetch tasks → entity-filter → format."""
    mapping = _load_slack_asana_map()
    user = mapping.get(slack_user_id)
    if not user:
        return (
            f"Asana lookup failed: Slack user {slack_user_id} is not mapped to an Asana "
            f"account yet. Harrison can add a row to data/maps/slack-to-asana.yaml. "
            f"Reply explaining this and offer a non-Asana answer if possible."
        )

    # Coerce to str — YAML parses bare-number gids as int, which breaks the substring check.
    asana_gid = str(user.get("asana_user_gid", "") or "")
    if not asana_gid or "REPLACE" in asana_gid:
        return (
            f"Asana lookup failed: user {user.get('display_name', slack_user_id)} has "
            f"a placeholder asana_user_gid in the mapping. Tell the user Harrison needs "
            f"to finish populating data/maps/slack-to-asana.yaml."
        )

    try:
        all_tasks = asana_client.get_user_tasks(asana_gid)
    except asana_client.AsanaClientError as exc:
        log.warning("Asana tool error for slack_user=%s gid=%s: %s", slack_user_id, asana_gid, exc)
        return f"Asana error: {exc}. Tell the user there's a temporary issue reaching Asana."

    # Apply entity scope filter (no-op for FNDR)
    filtered = _filter_tasks_by_entity(all_tasks, entity)
    total = len(all_tasks)
    shown = len(filtered)

    log.info(
        "asana_get_my_tasks user=%s entity=%s total=%d shown_after_filter=%d",
        slack_user_id, entity, total, shown,
    )

    return asana_client.format_tasks_for_llm(
        filtered,
        entity_scope=entity if entity != "FNDR" else None,
        total_before_filter=total,
    )


# --- Catalog: tool definitions exposed to Claude ---


TOOL_DEFINITIONS = [
    {
        "name": "asana_get_my_tasks",
        "description": (
            "Fetch the incomplete Asana tasks assigned to the user who @-mentioned Cora. "
            "Use this when the user asks about their workload, priorities, or what they "
            "should work on — phrases like 'what's on my plate', 'what should I work on', "
            "'show me my tasks', 'what's due this week'. Returns up to 25 tasks with name, "
            "due date, project, and notes preview. Do not call for questions about another "
            "person's tasks — only the asking user's. The tool automatically scopes the "
            "result to the channel's entity (e.g. in #osn-leadership only OSN-tagged tasks "
            "are returned). FNDR channels (founder + catch-all) see all entities."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


# Name → callable. The callable takes (slack_user_id, entity, input_dict) and returns a string.
_TOOL_FUNCTIONS: dict[str, Callable[[str, str, dict], str]] = {
    "asana_get_my_tasks": _tool_get_my_tasks,
}


def dispatch(
    tool_name: str,
    tool_input: dict[str, Any],
    slack_user_id: str,
    entity: str = "FNDR",
) -> str:
    """Run a tool by name. Always returns a string for tool_result content.

    entity is the routed entity code for the channel the @mention came from
    (F3E, LEX, OSN, BDM, FNDR, etc.) — tools may use this to scope results.
    """
    fn = _TOOL_FUNCTIONS.get(tool_name)
    if not fn:
        log.warning("Unknown tool name requested by model: %s", tool_name)
        return f"Unknown tool: {tool_name}. Available tools: {list(_TOOL_FUNCTIONS)}"
    try:
        return fn(slack_user_id, entity, tool_input or {})
    except Exception as exc:
        log.exception("Tool %s raised unexpected error", tool_name)
        return f"Tool {tool_name} crashed: {exc}. Apologize to the user and continue."
