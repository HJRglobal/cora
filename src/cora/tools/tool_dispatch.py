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

from . import asana_client, calendar_client, hubspot_client

log = logging.getLogger(__name__)

# Path to slack→tool mappings. Resolves relative to repo root (parent of src/).
_REPO_ROOT = Path(__file__).resolve().parents[3]
_MAP_PATH = _REPO_ROOT / "data" / "maps" / "slack-to-asana.yaml"
_HUBSPOT_MAP_PATH = _REPO_ROOT / "data" / "maps" / "slack-to-hubspot.yaml"

# HubSpot pipeline → entity routing. Used to scope hubspot_get_my_deals by channel.
HUBSPOT_PIPELINE_BY_ENTITY: dict[str, str] = {
    "F3E": hubspot_client.PIPELINE_F3E_RETAIL,
    "UFL": hubspot_client.PIPELINE_UFL_SPONSORSHIPS,
    # OSN / LEX / BDM / HJRG don't currently have HubSpot pipelines; return all-pipeline
    # results in those channels (or empty if no deals match).
}


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
    """Load the Asana mapping, returning a dict keyed by slack_user_id."""
    if not _MAP_PATH.exists():
        log.warning("slack-to-asana.yaml not found at %s", _MAP_PATH)
        return {}
    with open(_MAP_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    users = data.get("users") or []
    return {u["slack_user_id"]: u for u in users if u.get("slack_user_id")}


def _load_slack_hubspot_map() -> dict[str, dict[str, Any]]:
    """Load the HubSpot mapping, returning a dict keyed by slack_user_id."""
    if not _HUBSPOT_MAP_PATH.exists():
        log.warning("slack-to-hubspot.yaml not found at %s", _HUBSPOT_MAP_PATH)
        return {}
    with open(_HUBSPOT_MAP_PATH, encoding="utf-8") as f:
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


def _tool_get_my_events(slack_user_id: str, entity: str, _input: dict) -> str:
    """Resolve user → Google email (from slack-to-asana mapping) → fetch calendar events → format.

    Reuses asana_email field from slack-to-asana.yaml as the Google identity, since most
    HJR team members share their @hjrglobal.com email across both. Domain-wide Delegation
    impersonates that email to read their primary calendar.
    """
    mapping = _load_slack_asana_map()
    user = mapping.get(slack_user_id)
    if not user:
        return (
            f"Calendar lookup failed: Slack user {slack_user_id} is not mapped to a Google "
            f"identity yet. Harrison can add a row to data/maps/slack-to-asana.yaml (the same "
            f"file Cora uses for Asana — the asana_email field doubles as the Google identity)."
        )

    user_email = (user.get("asana_email") or "").strip()
    if not user_email:
        return (
            f"Calendar lookup failed: user {user.get('display_name', slack_user_id)} has "
            f"no asana_email (Google identity) in the mapping."
        )

    # Accept tool input for 'when' (today, tomorrow, this_week, next_week, YYYY-MM-DD).
    # Default to today if not provided.
    when = (_input or {}).get("when") or "today"

    try:
        events, window_label = calendar_client.get_user_events(user_email, when=when)
    except calendar_client.CalendarClientError as exc:
        log.warning(
            "Calendar tool error for slack_user=%s email=%s: %s",
            slack_user_id, user_email, exc,
        )
        return (
            f"Calendar error: {exc}. Tell the user there's a temporary issue reaching Google "
            f"Calendar — they may want to check Google Calendar directly."
        )

    log.info(
        "calendar_get_my_events user=%s email=%s when=%s events=%d",
        slack_user_id, user_email, when, len(events),
    )

    return calendar_client.format_events_for_llm(events, window_label)


def _tool_get_my_deals(slack_user_id: str, entity: str, _input: dict) -> str:
    """Resolve user → HubSpot owner_id → fetch deals → channel-scope by pipeline → format."""
    mapping = _load_slack_hubspot_map()
    user = mapping.get(slack_user_id)
    if not user:
        return (
            f"HubSpot lookup failed: Slack user {slack_user_id} is not mapped to a HubSpot "
            f"owner yet. Harrison can run scripts/build_hubspot_user_map.py and paste the "
            f"results into data/maps/slack-to-hubspot.yaml. Reply explaining this and offer a "
            f"non-HubSpot answer if possible."
        )

    # Coerce to str — YAML parses bare-number ids as int
    owner_id = str(user.get("hubspot_owner_id", "") or "")
    if not owner_id or "REPLACE" in owner_id:
        return (
            f"HubSpot lookup failed: user {user.get('display_name', slack_user_id)} has "
            f"a placeholder hubspot_owner_id in the mapping. Tell the user Harrison needs "
            f"to finish populating data/maps/slack-to-hubspot.yaml."
        )

    # Channel-scope by pipeline. F3E channels → F3E Retail pipeline only. UFL → UFL Sponsors
    # (paused, will likely return zero). Other entities (OSN/LEX/BDM/HJRG/FNDR) → no pipeline
    # filter, all deals owned by the user.
    pipeline_id = HUBSPOT_PIPELINE_BY_ENTITY.get(entity)
    pipeline_filter_applied = pipeline_id is not None

    try:
        deals = hubspot_client.get_owner_deals(owner_id, pipeline_id=pipeline_id)
    except hubspot_client.HubSpotClientError as exc:
        log.warning(
            "HubSpot tool error for slack_user=%s owner=%s: %s",
            slack_user_id, owner_id, exc,
        )
        return (
            f"HubSpot error: {exc}. Tell the user there's a temporary issue reaching HubSpot."
        )

    log.info(
        "hubspot_get_my_deals user=%s entity=%s pipeline=%s deals=%d",
        slack_user_id, entity, pipeline_id or "(all)", len(deals),
    )

    return hubspot_client.format_deals_for_llm(
        deals,
        entity_scope=entity if entity != "FNDR" else None,
        pipeline_filter_applied=pipeline_filter_applied,
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
            "due date, project, and notes preview. Each task name is wrapped in a Slack "
            "hyperlink (`<url|task name>`) — preserve these verbatim in your reply so the "
            "user can click through to edit in Asana. Do not call for questions about another "
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
    {
        "name": "calendar_get_my_events",
        "description": (
            "Fetch calendar events from the user's Google Calendar primary calendar. "
            "Use this when the user asks about their schedule, meetings, calendar, or availability "
            "— phrases like 'what's on my calendar today', 'what meetings do I have this week', "
            "'am I free Friday', 'what's my schedule tomorrow'. Returns up to 25 events with "
            "title, time, duration, attendees, and location. Each event title is wrapped in a "
            "Slack hyperlink (`<url|event title>`) — preserve these verbatim in your reply so "
            "the user can click through to open in Google Calendar. The 'when' parameter accepts: "
            "'today' (default), 'tomorrow', 'this_week', 'next_week', or a specific date as "
            "'YYYY-MM-DD'. Do not call for questions about another person's calendar — only the "
            "asking user's."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "when": {
                    "type": "string",
                    "description": "Time window for events. Accepts: 'today', 'tomorrow', 'this_week', 'next_week', or a specific date as 'YYYY-MM-DD'. Defaults to 'today' if omitted.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "hubspot_get_my_deals",
        "description": (
            "Fetch the open HubSpot deals owned by the user who @-mentioned Cora. "
            "Use this when the user asks about their pipeline, deals, sales activity, "
            "or specific accounts — phrases like 'what's in my pipeline', 'show me my "
            "deals', 'how's the sales pipeline looking', 'what deals do I have'. Returns "
            "up to 25 open deals (closed-won and closed-lost excluded) with name, amount, "
            "stage, pipeline, close date, and F3E custom-field meta. Each deal name is "
            "wrapped in a Slack hyperlink (`<url|deal name>`) — preserve these verbatim in "
            "your reply so the user can click through to edit in HubSpot. In F3E channels "
            "the tool scopes to the F3E Retail pipeline only; in UFL channels it scopes to "
            "the (paused) UFL Sponsorships pipeline; other channels return all pipelines. "
            "Do not call for questions about another person's deals — only the asking user's."
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
    "hubspot_get_my_deals": _tool_get_my_deals,
    "calendar_get_my_events": _tool_get_my_events,
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
