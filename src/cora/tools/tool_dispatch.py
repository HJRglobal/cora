"""Tool dispatch layer for Claude tool-use loop.

Holds the tool catalog (name → JSONSchema + python callable). The catalog is
deliberately small — one tool today, expand as more land.

The dispatcher resolves a Claude `tool_use` block to a tool_result string.
Failures are caught and rendered as error tool_results so the model can react
gracefully instead of crashing the conversation.
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


def _load_slack_asana_map() -> dict[str, dict[str, str]]:
    """Load the mapping, returning a dict keyed by slack_user_id."""
    if not _MAP_PATH.exists():
        log.warning("slack-to-asana.yaml not found at %s", _MAP_PATH)
        return {}
    with open(_MAP_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    users = data.get("users") or []
    return {u["slack_user_id"]: u for u in users if u.get("slack_user_id")}


# --- Tool implementations (bound to the requesting slack user via dispatch) ---


def _tool_get_my_tasks(slack_user_id: str, _input: dict) -> str:
    """Resolve the requesting Slack user → Asana gid → fetch tasks → format."""
    mapping = _load_slack_asana_map()
    user = mapping.get(slack_user_id)
    if not user:
        return (
            f"Asana lookup failed: Slack user {slack_user_id} is not mapped to an Asana "
            f"account yet. Harrison can add a row to data/maps/slack-to-asana.yaml. "
            f"Reply explaining this and offer a non-Asana answer if possible."
        )

    asana_gid = user.get("asana_user_gid", "")
    if not asana_gid or "REPLACE" in asana_gid:
        return (
            f"Asana lookup failed: user {user.get('display_name', slack_user_id)} has "
            f"a placeholder asana_user_gid in the mapping. Tell the user Harrison needs "
            f"to finish populating data/maps/slack-to-asana.yaml."
        )

    try:
        tasks = asana_client.get_user_tasks(asana_gid)
    except asana_client.AsanaClientError as exc:
        log.warning("Asana tool error for slack_user=%s gid=%s: %s", slack_user_id, asana_gid, exc)
        return f"Asana error: {exc}. Tell the user there's a temporary issue reaching Asana."

    return asana_client.format_tasks_for_llm(tasks)


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
            "person's tasks — only the asking user's. Cross-entity scope rules still apply: "
            "if the channel's entity is not FNDR, only mention tasks relevant to that entity."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


# Name → callable. The callable takes (slack_user_id, input_dict) and returns a string.
_TOOL_FUNCTIONS: dict[str, Callable[[str, dict], str]] = {
    "asana_get_my_tasks": _tool_get_my_tasks,
}


def dispatch(tool_name: str, tool_input: dict[str, Any], slack_user_id: str) -> str:
    """Run a tool by name. Always returns a string for tool_result content."""
    fn = _TOOL_FUNCTIONS.get(tool_name)
    if not fn:
        log.warning("Unknown tool name requested by model: %s", tool_name)
        return f"Unknown tool: {tool_name}. Available tools: {list(_TOOL_FUNCTIONS)}"
    try:
        return fn(slack_user_id, tool_input or {})
    except Exception as exc:
        log.exception("Tool %s raised unexpected error", tool_name)
        return f"Tool {tool_name} crashed: {exc}. Apologize to the user and continue."
