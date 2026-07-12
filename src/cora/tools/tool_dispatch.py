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

import concurrent.futures
import logging
import os
import re
import time
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, Callable

import yaml

from . import ads_client, asana_client, brand_voice_client, calendar_client, completion_detector, fighter_tracker_client, financial_client, generate_image, gmail_client, hjrp_client, hubspot_client, influencer_client, inventory_client, lex_client, notion_client, qbo_client, sales_deck_client
from .. import dashboard_access, org_roles
from ..connectors import airtable_client, dashboard_drive_reader, gmail_reader, photoroom_client, qbo_oauth, shopify_client
from ..channel_classifier import classify_function as _classify_channel_function, is_tier_1 as _channel_is_tier1

log = logging.getLogger(__name__)

# Path to slack→tool mappings. Resolves relative to repo root (parent of src/).
_REPO_ROOT = Path(__file__).resolve().parents[3]
_MAP_PATH = _REPO_ROOT / "data" / "maps" / "slack-to-asana.yaml"
_HUBSPOT_MAP_PATH = _REPO_ROOT / "data" / "maps" / "slack-to-hubspot.yaml"
_ALIASES_PATH = _REPO_ROOT / "data" / "maps" / "user-aliases.yaml"
_HIERARCHY_PATH = _REPO_ROOT / "data" / "maps" / "supervisor-hierarchy.yaml"
# Refuse-by-default allowlist of Shopify locations that MAY receive a manual
# inventory write from Slack (f3e_shopify_set_inventory). Read fresh per write
# (live-reload). See the file header for the location-by-location rationale.
_SHOPIFY_WRITE_LOC_PATH = _REPO_ROOT / "data" / "maps" / "shopify-inventory-write-locations.yaml"
# Per-write audit trail for DTC inventory sets.
_SHOPIFY_WRITE_AUDIT_PATH = _REPO_ROOT / "logs" / "shopify-inventory-writes.jsonl"

# HubSpot pipeline → entity routing. Used to scope hubspot_get_my_deals by channel.
HUBSPOT_PIPELINE_BY_ENTITY: dict[str, str] = {
    "F3E": hubspot_client.PIPELINE_F3E_RETAIL,
    "UFL": hubspot_client.PIPELINE_UFL_OSN_BDM,
    # OSN and BDM share the UFL/OSN/BDM combined pipeline on Starter tier.
    "OSN": hubspot_client.PIPELINE_UFL_OSN_BDM,
    "BDM": hubspot_client.PIPELINE_UFL_OSN_BDM,
    # LEX / HJRG don't have HubSpot pipelines; return all-pipeline results.
}


# --- Verbatim-table tools (Phase 2.1 inline-formatter opt-out) ---
#
# These tools return pre-formatted financial/data tables or dashboards that the
# inline conversational voice pass (format_reply: markdown-table flatten, dash/
# list strip, char-cap) would mangle. When a reply incorporates one of these,
# claude_client sets meta["used_verbatim_tool"] and app.py SKIPS the inline
# format_reply for that reply (is_tool_output=True) and keeps it out of the cache.
# NOTE: the egress boundary still applies the universal SAFETY layer (mojibake +
# bare-URL/GID/16+digit-ID redaction) to every send, including these -- it does
# NOT voice-flatten, so tables survive, but a tool must not emit a bare 16+ digit
# id unwrapped (it would be redacted). Tables are source-opaque by construction.
#
# This REPLACES the old too-broad `is_tool_output=bool(used_tools)` heuristic,
# which made EVERY tool-using reply bypass the formatter (so a prose answer that
# merely looked something up went out unsanitized). Lookup/confirmation tools
# (asana_get_my_tasks, get_my_events, *_create_*, cora_my_notes, ...) are NOT
# here: their output is synthesized into prose that should still be sanitized.
VERBATIM_TABLE_TOOLS: frozenset[str] = frozenset({
    # Financial / QBO
    "financial_get_cashflow",
    "financial_get_pulse",
    "financial_get_close_pack",
    "osn_financial_pulse",
    "qbo_get_profit_loss",
    "qbo_get_balance_sheet",
    "qbo_get_ar_aging",
    "qbo_get_ap_aging",
    "qbo_get_recent_transactions",
    # Sales / pipeline / decision dashboards
    "f3e_hubspot_pipeline_summary",
    "fndr_open_decisions",
    "fndr_press_pipeline_summary",
    "fndr_contracts_dashboard",
    # Inventory
    "f3e_inventory_pulse",
    "f3e_inventory_by_location",
    "f3e_shopify_inventory",
    "f3e_shopify_sales_pulse",
    # Ads / Polar
    "ads_get_performance_summary",
    "ads_get_channel_breakdown",
    "ads_get_subbrand_performance",
    "ads_get_pixel_attribution",
    "ads_get_cm_waterfall",
    # Composite plate (role + tasks + calendar + pipeline, with sanctioned links)
    "whats_on_my_plate",
    # Dashboard read layer -- personal/confidential dashboards MUST never cache
    # (D-043 class); the two CRM readers are time-sensitive structured dashboards.
    "personal_oneamerica_portfolio",
    "personal_capital_program_state",
    "f3e_creator_crm",
    "fndr_content_pipeline",
})


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


# --- Name → user resolution (third-party lookups) ---


def _load_user_aliases() -> dict[str, Any]:
    """Load the user-aliases.yaml, returning the raw config dict.

    Returns an empty dict (with empty aliases / rules) if the file is missing —
    callers fall back to display_name lookup only.
    """
    if not _ALIASES_PATH.exists():
        log.warning("user-aliases.yaml not found at %s — name lookup will only match display_name exactly", _ALIASES_PATH)
        return {"aliases": {}, "disambiguation_rules": []}
    with open(_ALIASES_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return {
        "aliases": data.get("aliases") or {},
        "disambiguation_rules": data.get("disambiguation_rules") or [],
    }


def resolve_name_to_slack_user_id(name: str, channel_entity: str | None = None) -> tuple[str | None, str | None]:
    """Resolve a free-text name ("Sean", "Shaun Hawkins", "Tommy Anderson") to a slack_user_id.

    Returns: (slack_user_id, canonical_display_name) tuple.
    If no match, returns (None, None).
    If ambiguous (an alias collides across multiple canonical names) and the
    disambiguation rule covers the channel_entity, returns the routed user.
    Otherwise returns (None, ambiguity_message_for_llm).

    Lookup order:
      1. Exact match (case-insensitive) on display_name in slack-to-asana.yaml.
      2. Alias match in user-aliases.yaml → resolve to canonical display_name → lookup.
      3. Substring match on display_name (e.g. "Shaun" matches "Shaun Hawkins").
         Only used when neither 1 nor 2 hit, to handle aliases we haven't added yet.
      4. No match — return (None, None).
    """
    import re

    if not name or not name.strip():
        return None, None

    raw = name.strip()
    slack_asana_map = _load_slack_asana_map()

    # Slack mention syntax (<@U123>, <@U123|label>) or a bare Slack ID -> resolve
    # directly. An unknown Slack ID returns no-match (ask), never a guess (N4).
    m = re.match(r"^<@([UW][A-Z0-9]+)(?:\|[^>]*)?>$", raw)
    slack_id_candidate = m.group(1) if m else (raw if re.fullmatch(r"[UW][A-Z0-9]{6,}", raw) else None)
    if slack_id_candidate:
        user = slack_asana_map.get(slack_id_candidate)
        if user:
            return user["slack_user_id"], user.get("display_name")
        return None, None

    # Strip a leading "@" so "@Tommy" matches "Tommy Anderson" (audit N4).
    needle = raw.lstrip("@").strip().lower()
    if not needle:
        return None, None
    aliases_config = _load_user_aliases()

    # Build a display_name → user record lookup
    by_display: dict[str, dict[str, Any]] = {}
    for user in slack_asana_map.values():
        display = (user.get("display_name") or "").strip()
        if display:
            by_display[display.lower()] = user

    # 1. Exact match on display_name
    if needle in by_display:
        user = by_display[needle]
        return user["slack_user_id"], user.get("display_name")

    # 2. Alias match — find which canonical name(s) this alias maps to
    aliases_map: dict[str, list[str]] = aliases_config.get("aliases", {})
    canonical_matches: list[str] = []
    for canonical, variants in aliases_map.items():
        # Include the canonical itself as an implicit alias
        all_variants = [canonical] + list(variants)
        if any(v.strip().lower() == needle for v in all_variants):
            canonical_matches.append(canonical)

    if len(canonical_matches) == 1:
        canonical = canonical_matches[0]
        user = by_display.get(canonical.lower())
        if user:
            return user["slack_user_id"], user.get("display_name")
        log.warning(
            "Alias %r resolved to canonical %r but no user with that display_name in slack-to-asana.yaml",
            name, canonical,
        )
        return None, None

    if len(canonical_matches) > 1:
        # Try disambiguation rules
        rules = aliases_config.get("disambiguation_rules", [])
        for rule in rules:
            if rule.get("alias", "").strip().lower() == needle:
                routing = rule.get("channel_entity_routing") or {}
                target = routing.get(channel_entity) if channel_entity else None
                target = target or routing.get("default")
                if target:
                    user = by_display.get(target.lower())
                    if user:
                        return user["slack_user_id"], user.get("display_name")
        # No rule covered it
        log.info("Ambiguous name %r matches multiple canonical users: %s", name, canonical_matches)
        return None, f"Multiple users match '{name}': {canonical_matches}. Tell the user which one they meant."

    # 3. Word-anchored prefix match on display_name (fallback for un-aliased
    #    nicknames). Anchored to a word boundary + min length 3 so a short needle
    #    can't mis-resolve to the wrong person (the B3 lesson: no unanchored
    #    substring). Multiple distinct matches -> ask rather than guess.
    if len(needle) >= 3:
        seen_ids: set[str] = set()
        word_hits: list[dict[str, Any]] = []
        for key, u in by_display.items():
            words = key.split()
            if needle in words or any(w.startswith(needle) for w in words):
                sid = u["slack_user_id"]
                if sid not in seen_ids:
                    seen_ids.add(sid)
                    word_hits.append(u)
        if len(word_hits) == 1:
            user = word_hits[0]
            return user["slack_user_id"], user.get("display_name")
        if len(word_hits) > 1:
            names = [u.get("display_name", "?") for u in word_hits]
            log.info("Word-anchored lookup for %r ambiguous, matches: %s", name, names)
            return None, f"Multiple users match '{name}': {names}. Tell the user which one they meant."

    # 4. No match
    return None, None


# --- Supervisor hierarchy / authorization ---


def _load_supervisor_hierarchy() -> dict[str, Any]:
    """Load supervisor-hierarchy.yaml. Returns {founder_slack_id, reports_to_map}.

    reports_to_map: dict mapping report_slack_id -> supervisor_slack_id.
    """
    if not _HIERARCHY_PATH.exists():
        log.warning(
            "supervisor-hierarchy.yaml not found at %s — third-party Asana lookups will be "
            "restricted to founder only (Harrison hardcoded if found in slack-to-asana map)",
            _HIERARCHY_PATH,
        )
        return {"founder_slack_id": None, "reports_to_map": {}}
    with open(_HIERARCHY_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    rows = data.get("reports_to") or []
    reports_to_map: dict[str, str] = {}
    for row in rows:
        report = (row.get("report") or "").strip()
        supervisor = (row.get("supervisor") or "").strip()
        if report and supervisor:
            if report in reports_to_map:
                log.warning(
                    "supervisor-hierarchy.yaml: %s listed as report under multiple supervisors "
                    "(keeping first: %s, ignoring: %s)",
                    report, reports_to_map[report], supervisor,
                )
                continue
            reports_to_map[report] = supervisor
    return {
        "founder_slack_id": (data.get("founder_slack_id") or "").strip() or None,
        "reports_to_map": reports_to_map,
    }


def _get_supervisor_chain(target_slack_id: str) -> list[str]:
    """Walk up the supervisor chain from `target_slack_id`.

    Returns the list of slack_user_ids that are (direct or transitive) supervisors
    of the target. Founder is the last entry if the chain reaches the top.
    Returns empty list if the target has no supervisor record.

    Cycle-safe: stops if a slack_user_id repeats.
    """
    hierarchy = _load_supervisor_hierarchy()
    reports_to_map = hierarchy["reports_to_map"]

    chain: list[str] = []
    seen: set[str] = {target_slack_id}
    current = target_slack_id
    while True:
        supervisor = reports_to_map.get(current)
        if not supervisor:
            break
        if supervisor in seen:
            log.warning("supervisor-hierarchy.yaml: cycle detected at %s — truncating chain", supervisor)
            break
        chain.append(supervisor)
        seen.add(supervisor)
        current = supervisor
    return chain


def is_authorized_to_query_user(
    asker_slack_id: str, target_slack_id: str
) -> tuple[bool, str | None]:
    """Check whether `asker` is allowed to query `target`'s Asana tasks.

    Rules (per Harrison 2026-05-21):
      1. Self-query -> True. (Path normally uses asana_get_my_tasks, but if called
         here we don't refuse.)
      2. Founder -> True. (Universal override.)
      3. Asker is in target's supervisor chain -> True. (Direct or transitive.)
      4. Else -> False, with a refusal message the LLM should surface.
    """
    if asker_slack_id == target_slack_id:
        return True, None

    hierarchy = _load_supervisor_hierarchy()
    founder = hierarchy.get("founder_slack_id")
    if founder and asker_slack_id == founder:
        return True, None

    chain = _get_supervisor_chain(target_slack_id)
    if asker_slack_id in chain:
        return True, None

    return False, (
        "Not authorized to look up that teammate's tasks. Per HJR's access doctrine, "
        "only direct or transitive supervisors of a person can query their Asana tasks. "
        "Tell the user this is a privacy / hierarchy rule — they can ask the person directly, "
        "or escalate to a shared supervisor (ultimately Harrison) if the information is needed."
    )


# --- Tool implementations (bound to the requesting slack user + entity scope via dispatch) ---

# F-03: cap the standalone asana_get_my_tasks render so a long verbatim
# reproduction can't overrun the reply's output-token budget and truncate
# mid-line (25 rich link-lines did, live 2026-07-11). Matches _PLATE_MAX_ITEMS;
# the tool still renders richer per-task detail than the plate composite.
_MY_TASKS_MAX_ITEMS = 10


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
        max_items=_MY_TASKS_MAX_ITEMS,
    )


def _tool_get_user_tasks(slack_user_id: str, entity: str, _input: dict) -> str:
    """Look up another teammate's Asana tasks by name.

    Distinct from _tool_get_my_tasks: takes a `user_name` parameter and resolves
    it to a slack_user_id via data/maps/user-aliases.yaml. Then runs the same
    Asana query path as the first-person tool.

    Use cases (per 2026-05-21 doctrine clarification): Harrison can ask Cora
    "what is Sean's latest tasks?" in a #fndr or #hjrg-* channel and Cora
    returns Shaun Hawkins's tasks. In entity-scoped channels, the same entity
    filter applies (tasks filtered to the channel's entity prefix).
    """
    name = (_input or {}).get("user_name", "").strip()
    if not name:
        return (
            "asana_get_user_tasks called without a user_name parameter. "
            "Tell the user which teammate you want to look up."
        )

    resolved_slack_id, info = resolve_name_to_slack_user_id(name, channel_entity=entity)
    if not resolved_slack_id:
        if info:  # ambiguity message
            return info
        return (
            f"No teammate found matching '{name}'. Tell the user the name didn't match "
            f"anyone in the Slack-to-Asana map. Suggest using their full name or a "
            f"common nickname (Sean, Shaun, Tommy, etc.). "
            f"If they're a new hire, Harrison can add them to data/maps/slack-to-asana.yaml + user-aliases.yaml."
        )

    # Peer-visibility allowed (Harrison 2026-05-21 follow-up): anyone in the
    # slack-to-asana map can query any other mapped teammate's tasks. Rationale:
    # if peer A depends on peer B to ship a deliverable, A can transparently
    # check status — coordination benefit > privacy cost at HJR's scale.
    # The supervisor-hierarchy artifact + is_authorized_to_query_user function
    # stay in the codebase (dormant) in case a future feature wants the org
    # chart for non-gating purposes (escalation routing, etc.).

    mapping = _load_slack_asana_map()
    user = mapping.get(resolved_slack_id)
    if not user:
        # This shouldn't happen — resolver pulls from the same map — but guard anyway.
        return (
            f"Found '{info}' but their Asana mapping is incomplete. "
            f"Tell the user there's a configuration issue."
        )

    asana_gid = str(user.get("asana_user_gid", "") or "")
    if not asana_gid or "REPLACE" in asana_gid:
        return (
            f"Found '{info}' but their asana_user_gid is a placeholder. "
            f"Tell the user Harrison needs to finish populating data/maps/slack-to-asana.yaml."
        )

    try:
        all_tasks = asana_client.get_user_tasks(asana_gid)
    except asana_client.AsanaClientError as exc:
        log.warning("Asana third-party lookup error name=%r resolved=%s gid=%s: %s", name, info, asana_gid, exc)
        return f"Asana error: {exc}. Tell the user there's a temporary issue reaching Asana."

    # Apply entity scope filter (same as first-person tool)
    filtered = _filter_tasks_by_entity(all_tasks, entity)
    total = len(all_tasks)
    shown = len(filtered)

    log.info(
        "asana_get_user_tasks asker=%s target=%r resolved=%s entity=%s total=%d shown=%d",
        slack_user_id, name, info, entity, total, shown,
    )

    formatted = asana_client.format_tasks_for_llm(
        filtered,
        entity_scope=entity if entity != "FNDR" else None,
        total_before_filter=total,
    )
    # Prepend the resolved-name context so Claude's reply attributes the tasks correctly
    return f"[Looking up tasks for: {info}]\n\n{formatted}"


_AGGREGATOR_CREATE_ENTITIES = frozenset({"FNDR", "HJRG"})


def _norm_task_key(name: str) -> str:
    """Normalize a task name for exact-name dedup (collapse ws, cap 160, lower)."""
    s = " ".join((name or "").split())
    return s[:160].rstrip().lower()


def _find_open_dup(project_gid: str, title: str) -> str | None:
    """Permalink/gid of an existing OPEN task with the same normalized name in the
    project, else None. Fail-OPEN -- a dedup read must never block a legit create.
    (Exact-name only -- a reworded duplicate from LLM rephrasing still slips; this
    is the floor, not a complete dedup.)"""
    if not project_gid:
        return None
    try:
        key = _norm_task_key(title)
        for t in asana_client.get_project_tasks(project_gid, max_tasks=500):
            if _norm_task_key(t.get("name") or "") == key:
                return t.get("permalink_url") or t.get("gid") or "an existing task"
    except Exception as exc:  # noqa: BLE001 -- dedup is best-effort
        log.warning("asana_create_task: dedup scan failed for %s: %s", project_gid, exc)
    return None


def _plan_asana_create(
    entity: str,
    title: str,
    notes: str | None,
    project_gid: str | None,
    assignee_gid: str | None,
) -> dict:
    """Decide the final project + (for a Lexington channel) PHI-scrub the task,
    with NO silent cross-entity re-routing. Returns title/notes/project_gid plus
    human-readable `notices` to surface at the confirm gate. No network calls.

    INVARIANT (WS3): never silently file a task into another entity's project, and
    never silently scrub a deliberately cross-entity task -- every adjustment is
    recorded in `notices` and shown in the preview before the user confirms.
    """
    from cora.tools import project_resolver as pr

    ent_upper = (entity or "FNDR").upper()
    is_aggregator = ent_upper in _AGGREGATOR_CREATE_ENTITIES
    is_lex = ent_upper == "LEX" or ent_upper.startswith("LEX-")
    notices: list[str] = []

    # 1. Drop an explicit project_gid that belongs to a DIFFERENT entity family.
    if project_gid and not is_aggregator and not pr.belongs_to_entity(project_gid, ent_upper):
        owners = ", ".join(sorted(pr.project_owner_entities(project_gid))) or "another entity"
        notices.append(
            f"the project you named belongs to {owners}, not {ent_upper}; I routed this "
            f"to a {ent_upper} project instead"
        )
        project_gid = None

    # 2. Smart routing when no (valid) project was supplied.
    if not project_gid:
        try:
            resolved = pr.resolve_project(
                entity=entity, task_text=title + (" " + (notes or "")), assignee_gid=assignee_gid
            )
            if resolved and not pr.is_blocked_project(resolved):
                project_gid = resolved
        except Exception as exc:  # noqa: BLE001
            log.warning("asana_create_task: project_resolver failed (%s)", exc)

    # 3. No silent My-Tasks orphan for an entity-scoped channel. If the entity has
    #    no configured project at all (e.g. BDM), SURFACE the orphan -- never silent.
    if not project_gid and not is_aggregator:
        ca = pr.entity_catch_all(entity)
        if ca and not pr.is_blocked_project(ca):
            project_gid = ca
        else:
            notices.append(
                f"no {ent_upper} project is configured, so this lands in the assignee's "
                f"My Tasks -- move it in Asana if it needs a project"
            )

    # 4. Lexington channel: PHI-scrub (minimum-necessary) + keep it in LEX scope.
    lex_scrub_error = False
    if is_lex:
        try:
            from .. import phi_guard
            staff = {r.name for r in org_roles.all_roles() if getattr(r, "name", "")}
            new_title = phi_guard.scrub_lex_phi(title, allowed_names=staff)
            new_notes = phi_guard.scrub_lex_phi(notes, allowed_names=staff) if notes else notes
            if new_title != title or (notes and new_notes != notes):
                notices.append("PHI-scrubbed the task for Lexington (minimum-necessary)")
            title, notes = new_title, new_notes
        except Exception as exc:  # noqa: BLE001 -- fail CLOSED on PHI
            log.warning("asana_create_task: LEX PHI scrub failed (%s)", exc)
            lex_scrub_error = True
        if not lex_scrub_error and project_gid:
            owners = pr.project_owner_entities(project_gid)
            is_confirmed_lex = bool(owners) and any(
                str(o).upper().startswith("LEX") for o in owners
            )
            if not is_confirmed_lex:
                # Fail CLOSED: a project we cannot POSITIVELY confirm is LEX-owned
                # (including an unmapped/ad-hoc GID, owners==empty) is dropped to the
                # LEX catch-all -- a Lexington task must never land outside LEX scope.
                ca = pr.entity_catch_all(entity) or pr.entity_catch_all("LEX")
                project_gid = ca if (ca and not pr.is_blocked_project(ca)) else None
                notices.append(
                    "routed to a Lexington project (an unverified project was dropped "
                    "to protect client confidentiality)"
                )

    return {
        "title": title,
        "notes": notes,
        "project_gid": project_gid,
        "notices": notices,
        "lex_scrub_error": lex_scrub_error,
    }


def _tool_asana_create_task(slack_user_id: str, entity: str, _input: dict) -> str:
    """Create a new Asana task in the HJR Global workspace.

    First Cora write tool. Reverses 2026-05-18 read-only doctrine per Harrison
    2026-05-21 decision after Lex Progress meeting verbal commitments.

    Safety pattern (LOCKED):
    - Tool description tells Claude to ALWAYS show the user a draft preview
      first and get explicit 'yes/approve/create it' before calling this tool
      with confirmed=true.
    - Tool refuses to fire if confirmed != true — defense in depth against
      Claude skipping the preview step.
    - Default assignee = the @-mentioning user. Cross-assignments require
      assignee_name to be set explicitly.
    - All creates audit-logged with asker slack_user_id, created task gid +
      permalink.
    """
    input_data = _input or {}
    title = (input_data.get("title") or "").strip()
    if not title:
        return (
            "asana_create_task called without a `title`. Tell the user what the "
            "task should be named — Cora won't create unnamed tasks."
        )

    # Resolve assignee
    assignee_name = (input_data.get("assignee_name") or "").strip()
    if assignee_name:
        resolved_slack_id, info = resolve_name_to_slack_user_id(
            assignee_name, channel_entity=entity
        )
        if not resolved_slack_id:
            if info:
                return info
            return (
                f"asana_create_task: assignee '{assignee_name}' didn't match anyone "
                f"in the Slack-to-Asana map. Either use a full name / common alias, "
                f"or omit assignee_name to default to the asking user."
            )
        target_user = _load_slack_asana_map().get(resolved_slack_id)
        if not target_user:
            return (
                f"asana_create_task: resolved '{assignee_name}' to a user but "
                f"their Asana mapping is incomplete. Tell the user there's a "
                f"configuration issue."
            )
        assignee_gid = str(target_user.get("asana_user_gid", "") or "")
        assignee_display = target_user.get("display_name", assignee_name)
    else:
        # Default: assign to the asking user
        asker = _load_slack_asana_map().get(slack_user_id)
        if not asker:
            return (
                f"asana_create_task: asker {slack_user_id} is not in the Slack-to-Asana "
                f"map, so I can't default the assignee to you. Either ask Harrison to add "
                f"your row to data/maps/slack-to-asana.yaml, or specify assignee_name "
                f"explicitly."
            )
        assignee_gid = str(asker.get("asana_user_gid", "") or "")
        assignee_display = asker.get("display_name", "(self)")

    if not assignee_gid or "REPLACE" in assignee_gid:
        return (
            f"asana_create_task: assignee_user_gid is missing or a placeholder for "
            f"{assignee_display}. Tell the user Harrison needs to finish populating "
            f"data/maps/slack-to-asana.yaml."
        )

    # Optional fields
    notes = input_data.get("notes") or None
    due_on = (input_data.get("due_on") or "").strip() or None
    explicit_project = (input_data.get("project_gid") or "").strip() or None
    force_duplicate = input_data.get("force_duplicate", False) is True

    # Plan routing + (LEX) PHI scrub -- no silent cross-entity re-route, no orphan.
    plan = _plan_asana_create(entity, title, notes, explicit_project, assignee_gid)
    if plan["lex_scrub_error"]:
        return (
            "asana_create_task: I couldn't safely prepare this Lexington task for "
            "confidentiality. Tell the user to create it directly in Asana."
        )
    f_title, f_notes, f_project = plan["title"], plan["notes"], plan["project_gid"]

    # The confirmation gate (defense in depth) -- surface routing/scrub in the preview.
    confirmed = input_data.get("confirmed", False)
    if confirmed is not True:
        preview = [
            "asana_create_task refused: `confirmed` must be set to true ONLY after you "
            "have shown the user this preview block AND received their explicit approval "
            "('yes' / 'approve' / 'create it'):",
            f"- Task: {f_title}",
            f"- Assignee: {assignee_display}",
            f"- Due: {due_on or '(none)'}",
        ]
        if plan["notices"]:
            preview.append("- Note: " + "; ".join(plan["notices"]))
        preview.append("If they approve, call again with confirmed=true.")
        return "\n".join(preview)

    # Dedup gate: surface a likely duplicate instead of silently creating one.
    if f_project and not force_duplicate:
        dup = _find_open_dup(f_project, f_title)
        if dup:
            return (
                f"asana_create_task: an OPEN task named {f_title!r} already exists in that "
                f"project ({dup}). Tell the user it already exists. If it is truly a "
                f"separate task, call again with force_duplicate=true."
            )

    try:
        created = asana_client.create_task(
            name=f_title,
            assignee_gid=assignee_gid,
            project_gid=f_project,
            notes=f_notes,
            due_on=due_on,
        )
    except asana_client.AsanaClientError as exc:
        log.warning(
            "asana_create_task FAILED asker=%s title=%r assignee=%s exc=%s",
            slack_user_id, f_title, assignee_gid, exc,
        )
        return (
            f"Asana create_task error: {exc}. Tell the user the task wasn't created. "
            f"If the error mentions an invalid project or assignee, suggest they check "
            f"the details and try again."
        )

    log.info(
        "asana_create_task CREATED asker=%s title=%r assignee=%s task_gid=%s project=%s notices=%s",
        slack_user_id, f_title, assignee_display, created.get("gid", ""), f_project, plan["notices"],
    )

    # Optional followers so a task meant for >1 person surfaces for all of them
    # (Asana allows ONE assignee but many followers).
    added_followers: list[str] = []
    follower_names = input_data.get("follower_names") or []
    if follower_names and created.get("gid"):
        fgids: list[str] = []
        for nm in follower_names:
            rid, _info = resolve_name_to_slack_user_id(str(nm), channel_entity=entity)
            if not rid:
                continue
            fu = _load_slack_asana_map().get(rid) or {}
            g = str(fu.get("asana_user_gid", "") or "")
            if g and "REPLACE" not in g and g != assignee_gid:
                fgids.append(g)
                added_followers.append(fu.get("display_name", str(nm)))
        if fgids:
            try:
                asana_client.add_task_followers(str(created["gid"]), fgids)
            except asana_client.AsanaClientError as exc:
                log.warning("asana_create_task: add followers failed: %s", exc)
                added_followers = []

    out = asana_client.format_created_task_for_llm(created)
    extras = list(plan["notices"])
    if added_followers:
        extras.append("following: " + ", ".join(added_followers))
    if extras:
        out += "\n(" + "; ".join(extras) + ")"
    return out


_FOUNDER_SLACK_ID = "U0B2RM2JYJ1"  # Harrison -- portfolio-wide task authority


def _lex_safe_label(label: str, entity: str) -> str:
    """PHI-scrub a task label before echoing it into a LEX channel (a manually-created
    task name can carry a client name). Non-LEX labels pass through unchanged."""
    if not (entity or "").upper().startswith("LEX"):
        return label
    try:
        from .. import phi_guard
        staff = {r.name for r in org_roles.all_roles() if getattr(r, "name", "")}
        return phi_guard.scrub_lex_phi(label, allowed_names=staff)
    except Exception:  # noqa: BLE001 -- never let a scrub error echo raw PHI
        return "your Lexington task"


def _resolve_asker_task(slack_user_id: str, task_gid: str, task_name: str, entity: str = ""):
    """Resolve a task to act on. Returns (gid, label, error).

    OWNERSHIP (WS5 invariant): complete/delete act ONLY on the asker's OWN open tasks,
    on BOTH the gid and name paths. A raw gid is NOT trusted blindly -- a teammate's
    gid is visible via asana_get_user_tasks, and the shared workspace PAT would
    otherwise let anyone complete/delete anyone's task through the confirm gate. The
    portfolio founder + FNDR/HJRG channels are exempt (cross-entity authority by design).
    """
    unrestricted = slack_user_id == _FOUNDER_SLACK_ID or (entity or "").upper() in ("FNDR", "HJRG")
    asker = _load_slack_asana_map().get(slack_user_id)
    agid = str((asker or {}).get("asana_user_gid", "") or "")
    task_gid = (task_gid or "").strip()

    if task_gid:
        if unrestricted:
            return task_gid, (task_name or task_gid).strip(), None
        if not agid:
            return None, None, (
                "I can't verify that task is yours -- your Slack->Asana mapping is "
                "missing. Ask Harrison to add your row."
            )
        try:
            tasks = asana_client.get_user_tasks(agid)
        except asana_client.AsanaClientError as exc:
            return None, None, f"Couldn't verify that task is yours ({exc})."
        owned = next(
            (t for t in tasks if not t.get("completed") and str(t.get("gid")) == task_gid),
            None,
        )
        if not owned:
            return None, None, (
                "That task isn't one of your open tasks -- I can only complete or "
                "delete tasks assigned to you."
            )
        return task_gid, (owned.get("name") or task_name or task_gid), None

    # Name path -- inherently scoped to the asker's own open tasks.
    if not agid:
        return None, None, (
            "I can't look up your tasks -- your Slack->Asana mapping is missing. "
            "Give me the task's id, or ask Harrison to add your row."
        )
    if not task_name:
        return None, None, "Tell me which task (a name or an id)."
    try:
        tasks = asana_client.get_user_tasks(agid)
    except asana_client.AsanaClientError as exc:
        return None, None, f"Couldn't read your tasks ({exc})."
    key = _norm_task_key(task_name)
    opent = [t for t in tasks if not t.get("completed")]
    matches = [t for t in opent if _norm_task_key(t.get("name") or "") == key]
    if not matches:
        matches = [t for t in opent if key and key in _norm_task_key(t.get("name") or "")]
    if not matches:
        return None, None, f"No open task of yours matches {task_name!r}."
    if len(matches) > 1:
        names = "; ".join((t.get("name") or "?") for t in matches[:6])
        return None, None, (
            f"Several open tasks of yours match {task_name!r}: {names}. "
            f"Be more specific or give the task id."
        )
    t = matches[0]
    return str(t.get("gid") or ""), (t.get("name") or task_name), None


def _tool_asana_complete_task(slack_user_id: str, entity: str, _input: dict) -> str:
    """Mark one of the asker's tasks complete (staged-write, confirmed gate)."""
    input_data = _input or {}
    gid, label, err = _resolve_asker_task(
        slack_user_id,
        (input_data.get("task_gid") or "").strip(),
        (input_data.get("task_name") or "").strip(),
        entity,
    )
    if err:
        return err
    label = _lex_safe_label(label, entity)
    if input_data.get("confirmed", False) is not True:
        return (
            f"asana_complete_task refused: show the user the preview "
            f"'Mark complete: {label}' and get explicit approval, then call again "
            f"with confirmed=true."
        )
    try:
        asana_client.complete_task(gid)
    except asana_client.AsanaClientError as exc:
        return f"Couldn't complete that task ({exc}). Tell the user it was NOT marked done."
    log.info("asana_complete_task actor=%s gid=%s", slack_user_id, gid)
    return f'WRITE_CONFIRMED -- post exactly: Done -- marked "{label}" complete in Asana.'


def _tool_asana_delete_task(slack_user_id: str, entity: str, _input: dict) -> str:
    """PERMANENTLY delete one of the asker's tasks (staged-write, confirmed gate)."""
    input_data = _input or {}
    gid, label, err = _resolve_asker_task(
        slack_user_id,
        (input_data.get("task_gid") or "").strip(),
        (input_data.get("task_name") or "").strip(),
        entity,
    )
    if err:
        return err
    label = _lex_safe_label(label, entity)
    if input_data.get("confirmed", False) is not True:
        return (
            f"asana_delete_task refused: deleting a task is PERMANENT (completing is "
            f"usually better). Show the user 'Permanently delete: {label}' and get "
            f"explicit approval, then call again with confirmed=true."
        )
    try:
        asana_client.delete_task(gid)
    except asana_client.AsanaClientError as exc:
        return f"Couldn't delete that task ({exc}). Tell the user it was NOT deleted."
    log.info("asana_delete_task actor=%s gid=%s label=%r", slack_user_id, gid, label)
    return f'WRITE_CONFIRMED -- post exactly: Deleted "{label}" from Asana.'


def _tool_gmail_create_draft(slack_user_id: str, entity: str, _input: dict) -> str:
    """Create a Gmail draft in the asker's own Drafts folder.

    Cora's second write tool. Same staged-write doctrine as asana_create_task:
    refuses to fire without confirmed=true; the tool description tells Claude
    to show a preview block first and get explicit user approval.

    The draft is impersonated AS the asker via Domain-wide Delegation, so it
    lands in the asker's personal Gmail Drafts — not a shared mailbox.
    The user must open Gmail and send the draft themselves; Cora never sends.
    """
    input_data = _input or {}

    # Confirmation gate (same pattern as asana_create_task)
    confirmed = input_data.get("confirmed", False)
    if confirmed is not True:
        return (
            "gmail_create_draft refused: `confirmed` must be set to true ONLY "
            "after you have shown the user a preview block (to, cc, subject, body) "
            "AND received their explicit approval in their next message "
            "('yes', 'draft it', 'create it', or similar). If you have NOT done "
            "that yet, format a clear preview NOW and ask the user to confirm "
            "before drafting."
        )

    to = input_data.get("to")
    subject = (input_data.get("subject") or "").strip()
    body = input_data.get("body") or ""
    cc = input_data.get("cc")
    bcc = input_data.get("bcc")

    if not to:
        return "gmail_create_draft: missing required field `to`. Ask the user who the recipient(s) should be."
    if not subject:
        return "gmail_create_draft: missing required field `subject`. Ask the user for a subject line."
    if not body.strip():
        return "gmail_create_draft: missing required field `body`. Ask the user what the email should say."

    # Resolve sender = the asking user
    asker = _load_slack_asana_map().get(slack_user_id)
    if not asker:
        return (
            f"gmail_create_draft: asker {slack_user_id} is not in the Slack-to-Asana "
            f"map, so I can't impersonate them for Gmail. Either ask Harrison to add "
            f"a row to data/maps/slack-to-asana.yaml (the asana_email field doubles "
            f"as the Google identity)."
        )

    sender_email = (asker.get("asana_email") or "").strip()
    if not sender_email:
        return (
            f"gmail_create_draft: user {asker.get('display_name', slack_user_id)} has "
            f"no asana_email in the user map. Tell the user there's a configuration issue."
        )

    try:
        draft = gmail_client.create_draft(
            sender_email=sender_email,
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            bcc=bcc,
        )
    except gmail_client.GmailClientError as exc:
        log.warning(
            "gmail_create_draft FAILED asker=%s sender=%s subject=%r exc=%s",
            slack_user_id, sender_email, subject, exc,
        )
        return (
            f"Gmail draft error: {exc}. Tell the user the draft wasn't created. "
            f"If the error mentions a bad recipient or auth, suggest they check the "
            f"details and try again."
        )

    # Normalize recipient lists for the response/log
    to_list = gmail_client._normalize_recipients(to)
    cc_list = gmail_client._normalize_recipients(cc) if cc else None

    log.info(
        "gmail_create_draft CREATED asker=%s sender=%s draft_id=%s subject=%r recipient_count=%d cc_count=%d",
        slack_user_id,
        sender_email,
        draft.get("id", ""),
        subject,
        len(to_list),
        len(cc_list) if cc_list else 0,
    )

    return gmail_client.format_created_draft_for_llm(
        draft,
        sender_email=sender_email,
        to=to_list,
        subject=subject,
        cc=cc_list,
    )


def _tool_gmail_inbox(slack_user_id: str, entity: str, _input: dict) -> str:
    """Return the asking user's recent Gmail inbox messages (unread + starred by default).

    Resolves Slack user → asana_email (Google identity) → DWD Gmail access.
    Returns up to 10 messages with sender, subject, date, and snippet.
    Read-only — no confirmation gate needed.
    """
    mapping = _load_slack_asana_map()
    user = mapping.get(slack_user_id)
    if not user:
        return (
            f"Gmail lookup failed: Slack user {slack_user_id} is not mapped to a Google "
            f"identity yet. Harrison can add a row to data/maps/slack-to-asana.yaml "
            f"(the asana_email field doubles as the Google identity)."
        )

    user_email = (user.get("asana_email") or "").strip()
    if not user_email:
        return (
            f"Gmail lookup failed: user {user.get('display_name', slack_user_id)} has "
            f"no asana_email (Google identity) in the mapping."
        )

    inp = _input or {}
    query = (inp.get("query") or "is:unread OR is:starred").strip()
    max_results = max(1, min(int(inp.get("max_results") or 10), 20))

    try:
        messages = gmail_reader.get_inbox_summary(
            user_email, query=query, max_results=max_results
        )
    except gmail_reader.GmailReaderError as exc:
        log.warning(
            "gmail_inbox tool error user=%s email=%s: %s", slack_user_id, user_email, exc
        )
        return (
            f"Gmail error: {exc}. Tell the user there's a temporary issue reading their inbox. "
            f"If the error mentions DWD or 403, Harrison may need to verify the Google Workspace "
            f"Domain-wide Delegation settings."
        )

    log.info(
        "gmail_inbox user=%s email=%s query=%r messages=%d",
        slack_user_id, user_email, query, len(messages),
    )

    if not messages:
        return f"No messages found matching '{query}' in your inbox right now."

    import datetime
    lines = [f"*Inbox for {user.get('display_name', user_email)}* — {len(messages)} message(s):"]
    lines.append("")
    for msg in messages:
        date_str = ""
        if msg.get("date_ts"):
            dt = datetime.datetime.fromtimestamp(msg["date_ts"])
            # %-m / %-I removes leading zeros on Linux; %#m / %#I does the same on Windows
            import sys as _sys
            if _sys.platform == "win32":
                date_str = dt.strftime("%#m/%#d %#I:%M %p")
            else:
                date_str = dt.strftime("%-m/%-d %-I:%M %p")
        sender = msg.get("from", "Unknown")
        # Strip angle-bracket email from "Name <email>" for cleaner display
        import re as _re
        name_only = _re.sub(r"\s*<[^>]+>", "", sender).strip() or sender
        subject = msg.get("subject") or "(no subject)"
        snippet = (msg.get("snippet") or "").strip()
        if len(snippet) > 120:
            snippet = snippet[:117] + "…"
        labels = msg.get("labels") or []
        flags = ""
        if "UNREAD" in labels:
            flags += " 🔵"
        if "STARRED" in labels:
            flags += " ⭐"
        line = f"• *{subject}*{flags}\n  From: {name_only}  ·  {date_str}"
        if snippet:
            line += f"\n  _{snippet}_"
        lines.append(line)

    return "\n".join(lines)


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


# ── Calendar staged-write server-side pending store (F-05, 2026-07-12) ───────
# Same server-side confirm pattern as the Shopify DTC write (below, ~2588): a
# calendar write PREVIEWS on the first call, stashing the RESOLVED fields keyed by
# (user, channel), and only EXECUTES on a later confirm turn from the STASHED
# fields -- never from model-echoed fields on the confirm turn. Closes the
# honor-system gate (F-05: a first-call confirmed=true booked an event + SENT
# Google invites with NO preview). Shared by create + delete; each tool checks the
# stashed `action` and re-previews on mismatch/no-pending (never a blind write).
_CALENDAR_PENDING_LOCK = Lock()
_CALENDAR_PENDING_TTL_SECONDS = 600  # 10 min
_PENDING_CALENDAR_WRITES: dict[tuple[str, str], dict] = {}


def _calendar_pending_key(slack_user: str, channel: str) -> tuple[str, str]:
    return (slack_user or "", (channel or "").strip().lower())


def _store_pending_calendar_write(slack_user: str, channel: str, entry: dict) -> None:
    with _CALENDAR_PENDING_LOCK:
        _PENDING_CALENDAR_WRITES[_calendar_pending_key(slack_user, channel)] = entry


def _take_pending_calendar_write(slack_user: str, channel: str) -> dict | None:
    """Pop-and-return the caller's pending calendar write if present AND fresh."""
    key = _calendar_pending_key(slack_user, channel)
    with _CALENDAR_PENDING_LOCK:
        entry = _PENDING_CALENDAR_WRITES.pop(key, None)
    if not entry:
        return None
    if (time.time() - float(entry.get("ts", 0))) > _CALENDAR_PENDING_TTL_SECONDS:
        return None
    return entry


def has_pending_calendar_write(slack_user: str, channel: str) -> bool:
    """Read-only freshness probe -- app.py forces Sonnet on the confirm turn (a
    bare 'yes'/'confirm' is undetectable from message content)."""
    key = _calendar_pending_key(slack_user, channel)
    with _CALENDAR_PENDING_LOCK:
        entry = _PENDING_CALENDAR_WRITES.get(key)
    return bool(entry) and (time.time() - float(entry.get("ts", 0))) <= _CALENDAR_PENDING_TTL_SECONDS


def _calendar_resolve_email(slack_user_id: str) -> tuple[str | None, str | None]:
    """Resolve the caller's Google identity. Returns (user_email, error_str)."""
    asker = _load_slack_asana_map().get(slack_user_id)
    if not asker:
        return None, (
            f"Slack user {slack_user_id} is not mapped to a Google identity. Harrison "
            f"can add a row to data/maps/slack-to-asana.yaml (asana_email doubles as the "
            f"Google identity)."
        )
    user_email = (asker.get("asana_email") or "").strip()
    if not user_email:
        return None, (
            f"User {asker.get('display_name', slack_user_id)} has no asana_email in the "
            f"user map. Tell the user there's a configuration issue."
        )
    return user_email, None


def _tool_calendar_create_event(slack_user_id: str, entity: str, _input: dict) -> str:
    """Create a Calendar event in the asker's own primary calendar (staged write).

    F-05: server-side pending store, NOT the honor-system model `confirmed` flag.
    The FIRST call previews + stashes the resolved fields; only a later confirm turn
    (which pops the stashed entry) actually books. A first-call confirmed=true now
    re-previews instead of booking, so an event/invite can never be sent unconfirmed.
    The event is created via DWD impersonation AS the asker (lands in their own
    calendar); attendees get Google invites (sendUpdates='all').
    """
    input_data = _input or {}
    channel = str(input_data.get("_channel_name") or "")
    confirmed = input_data.get("confirmed") is True

    # ── Phase 2: confirm turn -- execute ONLY the caller's own stashed create ──
    if confirmed:
        pending = _take_pending_calendar_write(slack_user_id, channel)
        if pending and pending.get("action") == "create":
            try:
                event = calendar_client.create_event(
                    user_email=pending["user_email"],
                    summary=pending["summary"],
                    start=pending["start"],
                    end=pending["end"],
                    attendees=pending.get("attendees"),
                    description=pending.get("description"),
                    location=pending.get("location"),
                    time_zone=pending.get("time_zone") or calendar_client._DEFAULT_TZ,
                )
            except calendar_client.CalendarClientError as exc:
                log.warning("calendar_create_event BOOK FAILED asker=%s exc=%s", slack_user_id, exc)
                return (
                    f"Calendar event error: {exc}. Tell the user the event wasn't created. "
                    f"If the error mentions a missing DWD scope, Harrison needs to update "
                    f"Domain-wide Delegation in admin.google.com."
                )
            log.info(
                "calendar_create_event CREATED asker=%s email=%s event_id=%s attendee_count=%d",
                slack_user_id, pending["user_email"], event.get("id", ""),
                len(pending.get("attendees") or []),
            )
            return calendar_client.format_created_event_for_llm(
                event, user_email=pending["user_email"]
            )
        # No fresh pending create (first-call-confirmed, stale, or restart-cleared)
        # -> fall through to Phase 1 and RE-PREVIEW. Never book blind.

    # ── Phase 1: validate + resolve + stash + preview ─────────────────────────
    summary = (input_data.get("summary") or "").strip()
    start = (input_data.get("start") or "").strip()
    end = (input_data.get("end") or "").strip()
    attendees = input_data.get("attendees")  # list[str] or None
    description = (input_data.get("description") or "").strip() or None
    location = (input_data.get("location") or "").strip() or None
    time_zone = (input_data.get("time_zone") or calendar_client._DEFAULT_TZ).strip()

    if not summary:
        return "calendar_create_event: missing required field `summary`. Ask the user for an event title."
    if not start:
        return "calendar_create_event: missing required field `start`. Ask the user for a start date/time."
    if not end:
        return "calendar_create_event: missing required field `end`. Ask the user for an end date/time."

    user_email, err = _calendar_resolve_email(slack_user_id)
    if err:
        return f"calendar_create_event: {err}"

    # Normalize attendees
    attendee_list: list[str] | None = None
    if attendees:
        if isinstance(attendees, str):
            attendee_list = [a.strip() for a in attendees.split(",") if a.strip()]
        elif isinstance(attendees, list):
            attendee_list = [str(a).strip() for a in attendees if str(a).strip()]

    _store_pending_calendar_write(slack_user_id, channel, {
        "action": "create",
        "user_email": user_email,
        "summary": summary,
        "start": start,
        "end": end,
        "attendees": attendee_list,
        "description": description,
        "location": location,
        "time_zone": time_zone,
        "ts": time.time(),
    })
    attendee_note = (
        f" Google will send invites to {len(attendee_list)} attendee(s) on confirm."
        if attendee_list else ""
    )
    return (
        "NOT CREATED yet -- this is a preview. Show the user this event and ask them to "
        "confirm; NOTHING is on the calendar until they say yes.\n"
        f"- Title: {summary}\n"
        f"- Start: {start}\n"
        f"- End: {end}\n"
        + (f"- Location: {location}\n" if location else "")
        + (f"- Attendees: {', '.join(attendee_list)}\n" if attendee_list else "")
        + f"\nReply to confirm and I'll book it (a Google Meet link is added "
        f"automatically).{attendee_note}"
    )


def _tool_calendar_delete_event(slack_user_id: str, entity: str, _input: dict) -> str:
    """Cancel/delete a Calendar event in the asker's own primary calendar (F-06).

    Staged write on the SAME server-side pending store as create: PREVIEW resolves
    the target event (by event_id, or by matching a title/keywords within a window)
    and stashes its id; a later confirm turn pops the stash and deletes ONLY that
    resolved id. Never deletes on the first call and never from a model-echoed id on
    the confirm turn. Attendees are notified (sendUpdates='all')."""
    input_data = _input or {}
    channel = str(input_data.get("_channel_name") or "")
    confirmed = input_data.get("confirmed") is True

    user_email, err = _calendar_resolve_email(slack_user_id)
    if err:
        return f"calendar_delete_event: {err}"

    # ── Phase 2: confirm turn -- delete ONLY the caller's own stashed target ──
    if confirmed:
        pending = _take_pending_calendar_write(slack_user_id, channel)
        if pending and pending.get("action") == "delete":
            try:
                calendar_client.delete_event(
                    user_email=pending["user_email"], event_id=pending["event_id"]
                )
            except calendar_client.CalendarClientError as exc:
                log.warning("calendar_delete_event FAILED asker=%s exc=%s", slack_user_id, exc)
                return (
                    f"Calendar delete error: {exc}. Tell the user the event was NOT "
                    f"cancelled."
                )
            log.info(
                "calendar_delete_event DELETED asker=%s email=%s event_id=%s",
                slack_user_id, pending["user_email"], pending["event_id"],
            )
            return (
                f"Cancelled '{pending['summary']}'. Google notified any attendees. Tell "
                f"the user it's off their calendar."
            )
        # No fresh pending delete -> fall through to Phase 1 and RE-PREVIEW.

    # ── Phase 1: resolve the target + stash + preview ─────────────────────────
    event_id = (input_data.get("event_id") or "").strip()
    query = (input_data.get("query") or "").strip()
    when = (input_data.get("when") or "this_week").strip()

    if event_id:
        summary = query or "(event)"
        _store_pending_calendar_write(slack_user_id, channel, {
            "action": "delete", "event_id": event_id, "summary": summary,
            "user_email": user_email, "ts": time.time(),
        })
        return (
            f"NOT CANCELLED yet -- reply to confirm and I'll cancel '{summary}'. "
            f"Attendees will be notified. Nothing changes until you confirm."
        )

    if not query:
        return (
            "calendar_delete_event: tell me which event to cancel -- a title or keywords "
            "(and a date if it's not this week). I won't guess."
        )
    try:
        events, label = calendar_client.get_user_events(user_email, when=when, max_events=50)
    except calendar_client.CalendarClientError as exc:
        return f"I couldn't pull your calendar to find that event: {exc}"
    matches = [e for e in events if query.lower() in (e.get("summary") or "").lower()]
    if not matches:
        return (
            f"I couldn't find an event matching '{query}' in {label}. Tell me the date "
            f"(e.g. 'on 2026-07-13') and I'll look again -- I won't cancel anything I "
            f"can't find."
        )
    if len(matches) > 1:
        listing = "; ".join((e.get("summary") or "(no title)") for e in matches[:8])
        return (
            f"'{query}' matches {len(matches)} events in {label}: {listing}. Which one? "
            f"(give the title and its date)"
        )
    ev = matches[0]
    ev_summary = ev.get("summary") or "(event)"
    _store_pending_calendar_write(slack_user_id, channel, {
        "action": "delete", "event_id": ev.get("id", ""), "summary": ev_summary,
        "user_email": user_email, "ts": time.time(),
    })
    return (
        f"NOT CANCELLED yet -- reply to confirm and I'll cancel '{ev_summary}' ({label}). "
        f"Attendees will be notified. Nothing changes until you confirm."
    )


def _tool_influencer_add_handle(slack_user_id: str, entity: str, _input: dict) -> str:
    """Register an athlete's social media handle in the influencer tracker.

    Write tool — confirmed=True gate. Once registered, the automated Instagram
    scanner can match detected mentions/tags directly to the athlete's name
    and deliverable record without Alex having to identify them manually.

    Alex uses this when onboarding a new sponsored athlete:
        '@Cora add handle for Luis Pena — instagram @luispena_ufc'
    """
    input_data = _input or {}

    confirmed = input_data.get("confirmed", False)
    if confirmed is not True:
        return (
            "influencer_add_handle refused: `confirmed` must be set to true ONLY "
            "after you have shown the user a preview (athlete name, platform, handle) "
            "AND received their explicit approval. Show the preview first, then re-call "
            "with confirmed=true."
        )

    athlete_name = (input_data.get("athlete_name") or "").strip()
    platform = (input_data.get("platform") or "").strip().lower()
    handle = (input_data.get("handle") or "").strip()
    row_entity = (input_data.get("entity") or entity or "F3E").strip().upper()

    if not athlete_name:
        return "influencer_add_handle: `athlete_name` is required."
    if not platform:
        return "influencer_add_handle: `platform` is required (instagram or tiktok)."
    if not handle:
        return "influencer_add_handle: `handle` is required (the athlete's account handle, with or without @)."

    try:
        row = influencer_client.register_handle(
            athlete_name=athlete_name,
            platform=platform,
            handle=handle,
            entity=row_entity,
            added_by=slack_user_id,
        )
    except influencer_client.InfluencerClientError as exc:
        log.warning(
            "influencer_add_handle FAILED actor=%s athlete=%r: %s",
            slack_user_id, athlete_name, exc,
        )
        return f"Influencer tracker error: {exc}. Tell the user the handle wasn't registered."

    clean_handle = row["handle"]
    log.info(
        "influencer_add_handle REGISTERED actor=%s athlete=%r platform=%s handle=%s entity=%s",
        slack_user_id, athlete_name, platform, clean_handle, row_entity,
    )
    return (
        f"Handle REGISTERED. Surface this to the user:\n"
        f"- *{athlete_name}* → {platform.capitalize()} @{clean_handle} [{row_entity}]\n"
        f"The Instagram scanner will now automatically match this athlete when they tag "
        f"the F3 brand accounts. Any new detections will appear in <#{_NOTIFY_CHANNEL_HINT}>."
    )


_NOTIFY_CHANNEL_HINT = "f3-sales"  # display hint only; actual channel set via INFLUENCER_SCAN_NOTIFY_CHANNEL env var


def _tool_influencer_get_status(slack_user_id: str, entity: str, _input: dict) -> str:
    """Return an influencer deliverable status or compliance report.

    Read-only. Surfaces overdue + pending deliverables (or a per-athlete compliance
    breakdown) for Alex / Harrison to act on. Entity-scoped by channel — in F3E
    channels only F3E deliverables surface; FNDR sees all entities.
    """
    input_data = _input or {}
    report_type = (input_data.get("report_type") or "status").strip().lower()
    athlete = (input_data.get("athlete") or "").strip() or None

    # Resolve entity scope: tool param overrides channel entity; FNDR = no filter
    entity_filter = entity if entity != "FNDR" else None

    log.info(
        "influencer_get_status actor=%s report_type=%s athlete=%r entity=%s",
        slack_user_id, report_type, athlete, entity_filter or "ALL",
    )

    try:
        if report_type == "compliance":
            rows = influencer_client.get_compliance_report(
                entity=entity_filter,
                athlete=athlete,
            )
            return influencer_client.format_compliance_report_for_llm(
                rows, entity_scope=entity_filter
            )
        else:
            # status or overdue — both read the open list; overdue just changes the label
            rows = influencer_client.get_deliverables(
                entity=entity_filter,
                athlete=athlete,
                include_complete=False,
                include_waived=False,
            )
            if report_type == "overdue":
                rows = [r for r in rows if r["display_status"] == "overdue"]
                label = "Overdue Influencer Deliverables"
            else:
                label = "Influencer Deliverables"
            return influencer_client.format_status_report_for_llm(
                rows,
                entity_scope=entity_filter,
                report_label=label,
            )
    except influencer_client.InfluencerClientError as exc:
        log.warning("influencer_get_status error actor=%s: %s", slack_user_id, exc)
        return f"Influencer tracker error: {exc}. Tell the user there was a problem reading the deliverable data."


def _tool_influencer_log_deliverable(slack_user_id: str, entity: str, _input: dict) -> str:
    """Add, complete, or waive an influencer deliverable.

    Write tool — same staged-write doctrine as asana_create_task / gmail_create_draft.
    Refuses to fire without confirmed=True. Claude must show a preview block first.

    action=add:      Register a new promised deliverable.
    action=complete: Mark an existing deliverable as done.
    action=waive:    Mark an existing deliverable as excused / cancelled.
    """
    input_data = _input or {}

    # Confirmation gate
    confirmed = input_data.get("confirmed", False)
    if confirmed is not True:
        return (
            "influencer_log_deliverable refused: `confirmed` must be set to true ONLY "
            "after you have shown the user a preview block AND received their explicit "
            "approval ('yes', 'log it', 'mark it done', 'waive it', or similar). "
            "If you have NOT done that yet, format a clear preview NOW and ask the user "
            "to confirm before writing to the tracker."
        )

    action = (input_data.get("action") or "add").strip().lower()
    if action not in ("add", "complete", "waive"):
        return (
            f"influencer_log_deliverable: unknown action {action!r}. "
            f"Valid actions are: add, complete, waive."
        )

    # Resolve actor display name for audit log
    asker_map = _load_slack_asana_map()
    actor_display = (asker_map.get(slack_user_id) or {}).get("display_name", slack_user_id)

    try:
        if action == "add":
            athlete_name = (input_data.get("athlete_name") or "").strip()
            platform = (input_data.get("platform") or "").strip()
            deliverable_type = (input_data.get("deliverable_type") or "").strip()
            due_date = (input_data.get("due_date") or "").strip() or None
            notes = input_data.get("notes") or None
            hubspot_deal_id = (input_data.get("hubspot_deal_id") or "").strip() or None
            # Entity: prefer tool-provided override, fall back to channel entity
            row_entity = (input_data.get("entity") or entity or "F3E").strip().upper()

            if not athlete_name:
                return "influencer_log_deliverable: `athlete_name` is required for action=add."
            if not platform:
                return "influencer_log_deliverable: `platform` is required for action=add (e.g. instagram, tiktok)."
            if not deliverable_type:
                return "influencer_log_deliverable: `deliverable_type` is required for action=add (e.g. post, story, reel)."

            row = influencer_client.add_deliverable(
                athlete_name=athlete_name,
                platform=platform,
                deliverable_type=deliverable_type,
                due_date=due_date,
                notes=notes,
                hubspot_deal_id=hubspot_deal_id,
                entity=row_entity,
                created_by=slack_user_id,
            )
            row["display_status"] = "pending"
            log.info(
                "influencer_log_deliverable ADD actor=%s id=%d athlete=%r",
                actor_display, row["id"], row["athlete_name"],
            )
            return influencer_client.format_logged_deliverable_for_llm(row, action="add")

        else:  # complete or waive
            deliverable_id_raw = input_data.get("deliverable_id")
            athlete_name_raw = (input_data.get("athlete_name") or "").strip()

            if not deliverable_id_raw and athlete_name_raw:
                # Name-based lookup: Alex typed "complete deliverable Mario Bautista story"
                dtype = (input_data.get("deliverable_type") or "").strip() or None
                month = (input_data.get("campaign_month") or "").strip() or None
                resolved = influencer_client.resolve_pending_deliverable(
                    athlete_name=athlete_name_raw,
                    deliverable_type=dtype,
                    campaign_month=month,
                )
                if not resolved:
                    desc = athlete_name_raw
                    if dtype:
                        desc += f" ({dtype})"
                    return (
                        f"No pending deliverable found for {desc}. "
                        f"Either it's already complete, the name doesn't match, "
                        f"or try specifying the type (story/post/reel)."
                    )
                deliverable_id = resolved["id"]
                log.info(
                    "influencer_log_deliverable: resolved %r %r -> id=%d",
                    athlete_name_raw, dtype, deliverable_id,
                )
            elif deliverable_id_raw:
                try:
                    deliverable_id = int(deliverable_id_raw)
                except (TypeError, ValueError):
                    return (
                        f"influencer_log_deliverable: `deliverable_id` must be a number. "
                        f"Got {deliverable_id_raw!r}."
                    )
            else:
                return (
                    f"influencer_log_deliverable: provide either `deliverable_id` (numeric) "
                    f"or `athlete_name` (e.g. 'Mario Bautista') to identify the deliverable."
                )

            if action == "complete":
                completion_link = (input_data.get("completion_link") or "").strip() or None
                notes = input_data.get("notes") or None
                row = influencer_client.mark_complete(
                    deliverable_id=deliverable_id,
                    completion_link=completion_link,
                    notes=notes,
                    actor=actor_display,
                )
                row["display_status"] = "complete"
                log.info(
                    "influencer_log_deliverable COMPLETE actor=%s id=%d athlete=%r",
                    actor_display, deliverable_id, row["athlete_name"],
                )
                return influencer_client.format_logged_deliverable_for_llm(row, action="complete")

            else:  # waive
                notes = input_data.get("notes") or None
                row = influencer_client.mark_waived(
                    deliverable_id=deliverable_id,
                    notes=notes,
                    actor=actor_display,
                )
                row["display_status"] = "waived"
                log.info(
                    "influencer_log_deliverable WAIVE actor=%s id=%d athlete=%r",
                    actor_display, deliverable_id, row["athlete_name"],
                )
                return influencer_client.format_logged_deliverable_for_llm(row, action="waive")

    except influencer_client.InfluencerClientError as exc:
        log.warning(
            "influencer_log_deliverable FAILED actor=%s action=%s: %s",
            actor_display, action, exc,
        )
        return (
            f"Influencer tracker error: {exc}. Tell the user the action wasn't completed "
            f"and suggest they check the deliverable ID or input values."
        )


def _tool_fighter_compliance(slack_user_id: str, entity: str, _input: dict) -> str:
    """Read the F3 Fighter Influencer Tracker Google Sheet and return compliance status."""
    input_data    = _input or {}
    month_tab     = (input_data.get("month_tab") or "").strip() or None
    show_complete = bool(input_data.get("show_complete", False))

    log.info(
        "fighter_compliance actor=%s month=%s show_complete=%s",
        slack_user_id, month_tab or "current", show_complete,
    )
    try:
        return fighter_tracker_client.format_compliance_for_slack(
            month_tab=month_tab,
            show_complete=show_complete,
        )
    except fighter_tracker_client.FighterTrackerError as exc:
        return f"Could not read fighter tracker: {exc}"


def _tool_influencer_list_handles(slack_user_id: str, entity: str, _input: dict) -> str:
    """Return all registered athlete handles, optionally filtered by platform.

    Read-only roster view. Alex uses this to see who's already in the tracker
    before registering a new athlete, or to confirm handle spellings before
    filing a deliverable.
    """
    input_data = _input or {}
    platform = (input_data.get("platform") or "").strip().lower() or None
    # Entity scope: in entity channels filter to that entity; FNDR sees all
    entity_filter = entity if entity != "FNDR" else None

    try:
        rows = influencer_client.list_handles(entity=entity_filter, platform=platform)
    except influencer_client.InfluencerClientError as exc:
        log.warning("influencer_list_handles error actor=%s: %s", slack_user_id, exc)
        return f"Influencer tracker error: {exc}. Tell the user there was a problem reading the handle registry."

    if not rows:
        scope_note = f" for {entity_filter}" if entity_filter else ""
        plat_note = f" on {platform}" if platform else ""
        return (
            f"No athlete handles are registered{scope_note}{plat_note} yet. "
            f"Use `influencer_add_handle` to register an athlete's Instagram or TikTok."
        )

    # Group by athlete for compact display
    by_athlete: dict[str, list[str]] = {}
    for r in rows:
        name = r["athlete_name"]
        tag = f"{r['platform'].capitalize()} @{r['handle']}"
        if r.get("entity"):
            tag += f" [{r['entity']}]"
        by_athlete.setdefault(name, []).append(tag)

    lines = [f"*Registered athlete handles ({len(rows)} total):*"]
    for name, handles in sorted(by_athlete.items()):
        lines.append(f"• *{name}*: {' | '.join(handles)}")

    if entity_filter:
        lines.append(f"_(scoped to {entity_filter} — use #fndr to see all entities)_")

    log.info(
        "influencer_list_handles actor=%s entity=%s platform=%s rows=%d",
        slack_user_id, entity_filter or "ALL", platform or "all", len(rows),
    )
    return "\n".join(lines)


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


# --- QBO tool helpers ---

# Entities that COULD have QBO data eventually. The tool checks list_provisioned_entities()
# at call time, so this is just for the "please specify entity" disambiguation prompt
# when the model calls a QBO tool from FNDR without naming an entity.
_KNOWN_QBO_CAPABLE_ENTITIES = (
    "HJRG", "F3E", "F3C", "BDM", "LEX", "OSN", "HJRP", "HJRPROD", "UFL",
    "HJRP-1337", "HJRP-1555",  # HJRP sub-properties — use HJRP token + QBO class filter
)


def _resolve_qbo_entity(channel_entity: str, override: str | None) -> tuple[str | None, str | None]:
    """Resolve which QBO entity a tool call should run against.

    Returns (target_entity, error_message). If target_entity is None, the caller
    should return error_message to the model as the tool_result.

    Rules:
      - If override is given and provisioned -> use it.
      - If override is given but NOT provisioned -> error with hint.
      - If channel_entity is FNDR/HJRG/etc. and no override -> ask the model to specify.
      - If channel_entity is a real entity (F3E, OSN, etc.) -> use it (and check provisioning).
    """
    try:
        provisioned = set(qbo_oauth.list_provisioned_entities())
    except Exception as exc:
        log.warning("Could not read QBO provisioned entities: %s", exc)
        return None, (
            "QBO tokens cannot be loaded right now (tokens file unreadable or missing). "
            "Tell the user QBO is temporarily unavailable."
        )

    if not provisioned:
        return None, (
            "No QBO entities are provisioned yet. Tell the user Harrison needs to run "
            "`uv run python scripts/qbo_oauth_flow.py --entity <CODE>` for each company first."
        )

    if override:
        norm = override.strip().upper()
        if norm in provisioned:
            return norm, None
        return None, (
            f"QBO entity {norm!r} is not provisioned. Available: {sorted(provisioned)}. "
            f"Ask the user which one they want, or pick the closest match if obvious."
        )

    # No override - use channel entity if it's a concrete entity with QBO tokens
    if channel_entity in provisioned:
        return channel_entity, None

    # FNDR / HJRG-as-fndr-fallback / unprovisioned channel entity -> ask user
    return None, (
        f"This channel is scoped to '{channel_entity}', which doesn't have its own QBO data. "
        f"Ask the user which entity's QBO data to pull. Provisioned entities: "
        f"{sorted(provisioned)}."
    )


def _tool_qbo_get_profit_loss(slack_user_id: str, entity: str, _input: dict) -> str:
    """Fetch Profit and Loss for an entity over a period. Returns a source-opaque Slack-mrkdwn summary (no QBO link)."""
    channel_name = (_input or {}).get("_channel_name", "")
    if not _is_tier1_channel(entity, channel_name):
        return _QBO_TIER1_REQUIRED
    override = (_input or {}).get("entity") if _is_founder_entity(entity) else None
    target, err = _resolve_qbo_entity(entity, override)
    if err:
        return err
    period = (_input or {}).get("period")
    start_date, end_date = qbo_client.parse_period(period)
    # For HJRP sub-properties, pass the sub-entity code so qbo_client
    # auto-resolves the correct QBO class filter (1337 or 1555 building).
    qbo_entity = target if target not in ("HJRP-1337", "HJRP-1555") else target
    # WS6: pass the per-entity basis override when configured (else company
    # default). format_pnl_for_llm labels whatever basis QBO actually rendered.
    basis = qbo_client.entity_pnl_basis(qbo_entity)
    try:
        report = qbo_client.get_profit_loss(
            qbo_entity, start_date, end_date, accounting_method=basis
        )
    except qbo_client.QboClientError as exc:
        log.warning("QBO P&L tool error entity=%s: %s", target, exc)
        return _qbo_error_message(target, exc)
    log.info("qbo_get_profit_loss entity=%s period=%s..%s", target, start_date, end_date)
    return qbo_client.format_pnl_for_llm(report, target, start_date, end_date)


def _tool_qbo_get_balance_sheet(slack_user_id: str, entity: str, _input: dict) -> str:
    """Fetch Balance Sheet snapshot for an entity as-of a date (defaults to today)."""
    channel_name = (_input or {}).get("_channel_name", "")
    if not _is_tier1_channel(entity, channel_name):
        return _QBO_TIER1_REQUIRED
    override = (_input or {}).get("entity") if _is_founder_entity(entity) else None
    target, err = _resolve_qbo_entity(entity, override)
    if err:
        return err
    as_of = (_input or {}).get("as_of_date")
    try:
        report = qbo_client.get_balance_sheet(target, as_of)
    except qbo_client.QboClientError as exc:
        log.warning("QBO Balance Sheet tool error entity=%s: %s", target, exc)
        return _qbo_error_message(target, exc)
    log.info("qbo_get_balance_sheet entity=%s as_of=%s", target, as_of or "today")
    return qbo_client.format_balance_sheet_for_llm(report, target, as_of or "today")


def _tool_qbo_get_ar_aging(slack_user_id: str, entity: str, _input: dict) -> str:
    """Fetch AR aging summary for an entity."""
    channel_name = (_input or {}).get("_channel_name", "")
    if not _is_tier1_channel(entity, channel_name):
        return _QBO_TIER1_REQUIRED
    override = (_input or {}).get("entity") if _is_founder_entity(entity) else None
    target, err = _resolve_qbo_entity(entity, override)
    if err:
        return err
    try:
        report = qbo_client.get_ar_aging(target)
    except qbo_client.QboClientError as exc:
        log.warning("QBO AR Aging tool error entity=%s: %s", target, exc)
        return _qbo_error_message(target, exc)
    log.info("qbo_get_ar_aging entity=%s", target)
    return qbo_client.format_ar_aging_for_llm(report, target)


def _tool_qbo_get_ap_aging(slack_user_id: str, entity: str, _input: dict) -> str:
    """Fetch AP aging summary for an entity."""
    channel_name = (_input or {}).get("_channel_name", "")
    if not _is_tier1_channel(entity, channel_name):
        return _QBO_TIER1_REQUIRED
    override = (_input or {}).get("entity") if _is_founder_entity(entity) else None
    target, err = _resolve_qbo_entity(entity, override)
    if err:
        return err
    try:
        report = qbo_client.get_ap_aging(target)
    except qbo_client.QboClientError as exc:
        log.warning("QBO AP Aging tool error entity=%s: %s", target, exc)
        return _qbo_error_message(target, exc)
    log.info("qbo_get_ap_aging entity=%s", target)
    return qbo_client.format_ap_aging_for_llm(report, target)


def _tool_qbo_get_recent_transactions(slack_user_id: str, entity: str, _input: dict) -> str:
    """Fetch a digest of recent Invoice / Bill / Payment activity for an entity."""
    channel_name = (_input or {}).get("_channel_name", "")
    if not _is_tier1_channel(entity, channel_name):
        return _QBO_TIER1_REQUIRED
    override = (_input or {}).get("entity") if _is_founder_entity(entity) else None
    target, err = _resolve_qbo_entity(entity, override)
    if err:
        return err
    try:
        days = int((_input or {}).get("days") or 30)
    except (TypeError, ValueError):
        days = 30
    days = max(1, min(days, 180))  # clamp to sane range
    try:
        payload = qbo_client.get_recent_transactions(target, days=days)
    except qbo_client.QboClientError as exc:
        log.warning("QBO Recent Transactions tool error entity=%s: %s", target, exc)
        return _qbo_error_message(target, exc)
    log.info("qbo_get_recent_transactions entity=%s days=%d", target, days)
    return qbo_client.format_recent_transactions_for_llm(payload, target, days)


# --- Finance channel enforcement ---


def _is_hr_channel(channel_name: str) -> bool:
    """Return True only if channel_name ends with '-hr' (e.g. lex-hr)."""
    return bool(channel_name) and channel_name.lower().endswith("-hr")


def _is_founder_entity(entity: str) -> bool:
    """Return True for founder-level entities that can access cross-entity data."""
    return entity.upper() in ("FNDR", "HJRG")


_FINANCE_CHANNEL_REQUIRED = (
    "Financial details are only available in this entity's dedicated finance channel."
)

_QBO_TIER1_REQUIRED = (
    "QuickBooks financial data is available in TIER_1 channels only "
    "(finance, leadership, founder, or build channels)."
)

_HR_CHANNEL_REQUIRED = (
    "HR and staff information is only available in this entity's dedicated HR channel."
)


def _qbo_error_message(entity: str, exc: Exception) -> str:
    """Return a clean, user-facing error string for a QBO failure.

    Distinguishes auth failures (not connected / needs re-auth) from transient
    API errors so Claude doesn't hallucinate remediation steps.
    """
    exc_str = str(exc).lower()
    if "auth error" in exc_str or "invalid_grant" in exc_str or "refresh failed" in exc_str:
        return (
            f"QuickBooks for {entity} isn't connected or needs re-authorization. "
            f"I don't have that data right now."
        )
    return f"QuickBooks is temporarily unavailable for {entity}. I don't have that data right now."


def _is_tier1_channel(entity: str, channel_name: str) -> bool:
    """Return True if the channel + entity combination grants TIER_1 access.

    A DM is structurally TIER_3 (W2-02). In a DM the ``entity`` passed to the
    finance tools is the asker's org-roles PRIMARY, and channel_classifier.is_tier_1
    short-circuits True for any HJRG-primary asker -- so an HJRG-primary user could
    pull live finance DATA in a DM even though user_access.check_access already pins
    DMs to TIER_3 (see app._handle_dm_qa). Refusing DMs here makes the tool-level
    finance gate roster-independent and consistent with that pinned tier. The DM
    signal at the tool layer is channel_name=="dm" -- claude_client does NOT thread
    the "D..."-prefixed channel_id into dispatch, so _channel_id is empty on the
    Q&A tool path; an empty/unknown channel_name is already non-TIER_1 below.
    """
    if not channel_name:
        return False
    if channel_name.strip().lower() == "dm":
        return False
    func = _classify_channel_function(channel_name)
    return _channel_is_tier1(entity, func)


# --- Financial / cashflow tools ---


def _tool_financial_get_cashflow(slack_user_id: str, entity: str, _input: dict) -> str:
    """Fetch current-week cash flow from the Standing ACTUALS sheet."""
    inp = _input or {}
    channel_name = inp.get("_channel_name", "")
    if not _is_tier1_channel(entity, channel_name):
        return _FINANCE_CHANNEL_REQUIRED
    # Cross-entity gate: FNDR/HJRG can override entity_filter; all others are
    # locked to their own entity — prevents e.g. #osn-finance querying LEX data.
    if _is_founder_entity(entity):
        entity_filter = inp.get("entity_filter") or entity or "FNDR"
    else:
        entity_filter = entity
    question = inp.get("question") or ""
    result = financial_client.get_cashflow_text(
        entity_filter=entity_filter,
        channel=channel_name,
        user=slack_user_id,
        question=question,
    )
    log.info(
        "financial_get_cashflow entity=%s entity_filter=%s result_len=%d",
        entity,
        entity_filter,
        len(result),
    )
    # Feature 6: upload long reports as Slack files
    channel_id = inp.get("_channel_id", "")
    if (
        len(result) > financial_client.FILE_UPLOAD_THRESHOLD
        and result != financial_client.UNKNOWN_RESPONSE
        and channel_id
    ):
        import os as _os
        from slack_sdk import WebClient as _SlackWebClient
        _bot_token = _os.environ.get("SLACK_BOT_TOKEN", "")
        if _bot_token:
            _sc = _SlackWebClient(token=_bot_token)
            title = f"Cash Flow Report — {entity_filter}"
            thread_ts = inp.get("_thread_ts")
            uploaded = financial_client.upload_report_as_file(
                slack_client=_sc,
                channel_id=channel_id,
                title=title,
                content=result,
                thread_ts=thread_ts,
            )
            if uploaded:
                return "📎 Full cash flow report uploaded above."
    return result


def _tool_osn_financial_pulse(slack_user_id: str, entity: str, _input: dict) -> str:
    """Fetch OSN store-by-store financial snapshot from the OSN Consolidated cashflow tab."""
    channel_name = (_input or {}).get("_channel_name", "")
    if not _is_tier1_channel(entity, channel_name):
        return _FINANCE_CHANNEL_REQUIRED
    log.info("osn_financial_pulse user=%s entity=%s", slack_user_id, entity)
    return financial_client.get_osn_pulse_text(
        channel=channel_name,
        user=slack_user_id,
    )


def _tool_financial_get_pulse(slack_user_id: str, entity: str, _input: dict) -> str:
    """Read the weekly financial pulse .md file for the entity from Drive."""
    channel_name = (_input or {}).get("_channel_name", "")
    # W3-04: gate on the same _is_tier1_channel as every sibling financial tool
    # (cashflow / QBO / osn_pulse). The pulse .md summarizes finance data those
    # tools already serve in any TIER_1 channel, so the old _is_finance_channel
    # (-finance suffix only) gate was a narrower-than-D-064 divergence; aligning
    # is a within-firewall tightening (no TIER_3 channel gains access).
    if not _is_tier1_channel(entity, channel_name):
        return _FINANCE_CHANNEL_REQUIRED
    log.info("financial_get_pulse user=%s entity=%s", slack_user_id, entity)
    return financial_client.get_entity_pulse_text(
        entity=entity,
        channel=channel_name,
        user=slack_user_id,
    )


def _tool_financial_get_close_pack(slack_user_id: str, entity: str, _input: dict) -> str:
    """Read a monthly close pack xlsx (P&L, balance sheet, cash flow, AR, or AP) from Drive."""
    channel_name = (_input or {}).get("_channel_name", "")
    # W3-04: align to the sibling _is_tier1_channel gate (see financial_get_pulse).
    if not _is_tier1_channel(entity, channel_name):
        return _FINANCE_CHANNEL_REQUIRED
    inp = _input or {}
    period = (inp.get("period") or "").strip()
    if not period:
        return "Period is required (format: YYYY-MM, e.g. '2026-04')."
    doctype = (inp.get("doctype") or "pl").strip().lower()
    log.info(
        "financial_get_close_pack user=%s entity=%s period=%s doctype=%s",
        slack_user_id, entity, period, doctype,
    )
    return financial_client.get_close_pack_text(
        entity=entity,
        period=period,
        doctype=doctype,
        channel=channel_name,
        user=slack_user_id,
    )


# --- OSN tools (Clover removed -- OSN now uses QBO as financial source) ---


# --- FNDR-specific tools ---


def _tool_fndr_open_decisions(slack_user_id: str, entity: str, _input: dict) -> str:
    """Return stalled decisions from memory/decisions-pending.md, entity-filtered.

    When called from a FNDR/HJRG channel (or entity is empty), returns ALL P0/P1 items
    across the full portfolio.  When called from an entity-specific channel (e.g. OSN,
    F3E, LEX-LLC) returns only items whose Entity tag matches that entity — plus any
    FNDR-tagged items (those are portfolio-level and visible everywhere).

    Reads the entire file — covers the main ## Active section AND the Gmail Deep Dive
    open-questions sections.  Skips the ## Recently resolved section.
    """
    import re
    from datetime import date, datetime

    _DRIVE_ROOT = Path("G:/My Drive/HJR-Founder-OS")
    decisions_path = _DRIVE_ROOT / "memory" / "decisions-pending.md"

    try:
        content = decisions_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.warning("fndr_open_decisions: decisions-pending.md not found at %s", decisions_path)
        return "I don't have that right now."
    except Exception as exc:
        log.warning("fndr_open_decisions: error reading decisions-pending.md: %s", exc)
        return "I don't have that right now."

    today = date.today()

    # Determine whether this is a portfolio-wide (FNDR/HJRG) or entity-scoped call.
    # Lex sub-entity channels (LEX-LLC, LEX-LLA, etc.) narrow to that sub-entity.
    calling_entity = (entity or "FNDR").upper().strip()
    portfolio_wide = calling_entity in ("FNDR", "HJRG", "")

    # Normalize caller entity for matching (e.g. "OSN", "F3E", "LEX-LLC")
    def _entity_matches(entry_entity_raw: str) -> bool:
        """True if the entry's Entity field covers the calling channel's entity."""
        if portfolio_wide:
            return True
        # Parse comma-separated entity codes from the entry
        entry_entities = [e.strip().upper() for e in entry_entity_raw.split(",")]
        # Direct match OR portfolio-level items (FNDR visible everywhere)
        if "FNDR" in entry_entities:
            return True
        if calling_entity in entry_entities:
            return True
        # Lex parent channel (#lex-*) sees all sub-entities
        if calling_entity == "LEX":
            return any(e.startswith("LEX") for e in entry_entities)
        return False

    # Strip the ## Recently resolved section before parsing
    resolved_match = re.search(r"^## Recently resolved\b", content, re.MULTILINE)
    parseable = content[: resolved_match.start()] if resolved_match else content

    # Parse every ### block in the file (covers Active + Gmail Deep Dive sections)
    entries: list[dict] = []
    topic_blocks = re.split(r"\n(?=### )", parseable)

    for block in topic_blocks:
        if not block.startswith("### "):
            continue

        topic = block.split("\n", 1)[0][4:].strip()  # strip "### " prefix
        if topic == "[Topic]":
            continue  # the "How to use" template skeleton, not a real entry

        # Entity tag (required for filtering; absent = treat as FNDR/visible everywhere)
        entity_match = re.search(r"\*\*Entity\*\*:\s*([^\n]+)", block)
        entry_entity_raw = entity_match.group(1).strip() if entity_match else "FNDR"
        if not _entity_matches(entry_entity_raw):
            continue

        # The template's "P0 / P1 / P2 / P3" alternatives line must not match;
        # annotated real values ("P0 (decision Monday)") must.
        sev_match = re.search(r"\*\*Severity\*\*:\s*(P\d)\b(?!\s*/)", block)
        if not sev_match:
            continue
        severity = sev_match.group(1)
        # Portfolio-wide: P0+P1 only. Entity-scoped: P0+P1+P2 for the full picture.
        if portfolio_wide and severity not in ("P0", "P1"):
            continue
        if not portfolio_wide and severity not in ("P0", "P1", "P2"):
            continue

        # Parse Last touched — handles "2026-05-23", "2026-05-12 (note)", "~2026-04"
        touched_match = re.search(r"\*\*Last touched\*\*:\s*([^\n]+)", block)
        age_days: int | None = None
        if touched_match:
            raw = touched_match.group(1).strip()
            date_match = re.search(r"(\d{4}-\d{2}-\d{2})", raw)
            month_match = re.search(r"~?(\d{4}-\d{2})$", raw.strip())
            if date_match:
                try:
                    touched = datetime.strptime(date_match.group(1), "%Y-%m-%d").date()
                    age_days = (today - touched).days
                except ValueError:
                    pass
            elif month_match:
                try:
                    touched = datetime.strptime(month_match.group(1) + "-01", "%Y-%m-%d").date()
                    age_days = (today - touched).days
                except ValueError:
                    pass

        owner_match = re.search(r"\*\*Owner of next nudge\*\*:\s*([^\n]+)", block)
        owner = owner_match.group(1).strip() if owner_match else "unassigned"

        entries.append(
            {
                "topic": topic,
                "severity": severity,
                "age_days": age_days,
                "owner": owner,
                "entity": entry_entity_raw,
            }
        )

    if not entries:
        if portfolio_wide:
            return "No P0 or P1 decisions are currently pending."
        return f"No open decisions found for {calling_entity}."

    p0 = sorted(
        [e for e in entries if e["severity"] == "P0"],
        key=lambda x: x["age_days"] or 0,
        reverse=True,
    )
    p1 = sorted(
        [e for e in entries if e["severity"] == "P1"],
        key=lambda x: x["age_days"] or 0,
        reverse=True,
    )
    p2 = sorted(
        [e for e in entries if e["severity"] == "P2"],
        key=lambda x: x["age_days"] or 0,
        reverse=True,
    )

    def _fmt(e: dict) -> str:
        age = e["age_days"]
        if age is None:
            age_str = "age unknown"
        elif age == 0:
            age_str = "touched today"
        elif age == 1:
            age_str = "1d stale"
        else:
            age_str = f"{age}d stale"
        # 🚨 = P0 >14d, 🔴 = P0 <=14d, 🟡 = P1, ⚪ = P2
        if e["severity"] == "P0" and (age or 0) > 14:
            marker = "🚨"
        elif e["severity"] == "P0":
            marker = "🔴"
        elif e["severity"] == "P1":
            marker = "🟡"
        else:
            marker = "⚪"
        return f"{marker} *{e['topic']}* ({age_str}) — {e['owner']}"

    scope_label = "portfolio" if portfolio_wide else calling_entity
    header_parts = []
    if p0:
        header_parts.append(f"{len(p0)} P0")
    if p1:
        header_parts.append(f"{len(p1)} P1")
    if p2 and not portfolio_wide:
        header_parts.append(f"{len(p2)} P2")
    header = f"*Open decisions ({scope_label}) — {', '.join(header_parts) or 'none'}:*"

    lines = [header, ""]
    for e in p0:
        lines.append(_fmt(e))
    if p0 and (p1 or p2):
        lines.append("")
    for e in p1:
        lines.append(_fmt(e))
    if p1 and p2 and not portfolio_wide:
        lines.append("")
    for e in p2:
        lines.append(_fmt(e))

    log.info(
        "fndr_open_decisions user=%s entity=%s scope=%s p0=%d p1=%d p2=%d",
        slack_user_id, entity, scope_label, len(p0), len(p1), len(p2),
    )
    return "\n".join(lines)


def _tool_fndr_completion_candidates(slack_user_id: str, entity: str, _input: dict) -> str:
    """Scan KB for recent completion signals and match against open Asana tasks.

    Pulls KB chunks from the last 25 hours that contain completion language
    (completed, shipped, signed, paid, launched, etc.), fuzzy-matches each
    signal against the caller's open Asana tasks, and returns a formatted
    digest with clickable Asana deep links.

    Read-only — never marks tasks complete. Intended for FNDR/HJRG channels.
    """
    log.info("fndr_completion_candidates user=%s entity=%s", slack_user_id, entity)

    # 1. Fetch open tasks for the requesting user (same pattern as get_my_tasks)
    user_map = _load_slack_asana_map()

    user_entry = user_map.get(slack_user_id)
    asana_gid = str(user_entry.get("asana_user_gid", "") or "") if user_entry else ""

    open_tasks: list[dict] = []
    if asana_gid and "REPLACE" not in asana_gid:
        try:
            open_tasks = asana_client.get_user_tasks(asana_gid, max_tasks=100)
        except asana_client.AsanaClientError as exc:
            log.warning("fndr_completion_candidates: Asana error: %s", exc)
            return "I don't have that right now — couldn't reach Asana."
    else:
        # FNDR sweep: pull tasks for all known users and pool them
        all_gids: set[str] = set()
        for v in user_map.values():
            gid = str(v.get("asana_user_gid", "") or "") if isinstance(v, dict) else ""
            if gid and "REPLACE" not in gid:
                all_gids.add(gid)
        for gid in list(all_gids)[:10]:  # cap at 10 users to avoid rate limits
            try:
                open_tasks.extend(asana_client.get_user_tasks(gid, max_tasks=50))
            except asana_client.AsanaClientError:
                pass

    if not open_tasks:
        return "No open Asana tasks found — can't run completion matching."

    # 2. Scope entity list for KB query
    entity_list: list[str] | None = None
    if entity and entity != "FNDR":
        entity_list = [entity, "FNDR"]

    # 3. Collect live email signals for the requesting user (fails silently)
    email_signals: list = []
    if user_entry:
        user_email = (user_entry.get("asana_email") or "").strip()
        if user_email:
            email_signals = completion_detector.collect_email_signals(
                user_email,
                lookback_seconds=completion_detector.INTERACTIVE_LOOKBACK_SECONDS,
                entity=entity or "FNDR",
            )

    # 4. Run detection — use shorter interactive lookback (4h vs 25h sweep) so
    # the fuzzy-match loop finishes in <5s even on a large KB.
    candidates = completion_detector.detect_candidates(
        open_tasks,
        lookback_seconds=completion_detector.INTERACTIVE_LOOKBACK_SECONDS,
        entities=entity_list,
        apply_dedup=True,
        extra_signals=email_signals or None,
    )

    # 5. Record dedup timestamps so the same candidates don't resurface immediately
    completion_detector.mark_candidates_sent(candidates)

    log.info(
        "fndr_completion_candidates user=%s entity=%s tasks=%d email_signals=%d candidates=%d",
        slack_user_id, entity, len(open_tasks), len(email_signals), len(candidates),
    )
    return completion_detector.format_sweep_digest(candidates)


def _tool_f3e_brand_voice_check(slack_user_id: str, entity: str, _input: dict) -> str:
    """Check draft copy against F3 brand-guidelines V1 voice spec for the specified sub-brand.

    Read-only, no external calls. Returns a structured findings report (CRITICAL / WARNING / INFO)
    plus the brand's locked voice-pillar summary so Claude can synthesize a helpful reply.

    Checks:
      - Health/nutrition claims (universal — all three brands)
      - Cross-entity UFL pause (universal — F3-UFL crossover blocked)
      - Sleep positioning (Mood ONLY — CRITICAL anti-pattern)
      - Sibling-brand drift (Energy-lane or Mood-lane language in the wrong brand's copy)
      - Anti-positioning (competitor brand names in Energy copy, etc.)
    """
    input_data = _input or {}
    brand = (input_data.get("brand") or "").strip().lower()
    copy = (input_data.get("copy") or "").strip()

    if not brand:
        return (
            "f3e_brand_voice_check called without `brand`. "
            "Ask the user which F3 sub-brand the copy is for: pure, mood, or energy."
        )
    if not copy:
        return (
            "f3e_brand_voice_check called without `copy`. "
            "Ask the user to paste the draft copy they want checked."
        )
    if brand not in brand_voice_client.VALID_BRANDS:
        return (
            f"f3e_brand_voice_check: unknown brand {brand!r}. "
            f"Must be one of: {', '.join(brand_voice_client.VALID_BRANDS)}. "
            "Ask the user which F3 sub-brand the copy is for."
        )

    log.info(
        "f3e_brand_voice_check brand=%s copy_len=%d user=%s entity=%s",
        brand, len(copy), slack_user_id, entity,
    )

    result = brand_voice_client.check_copy(brand, copy)
    return brand_voice_client.format_result_for_llm(result)


# --- Ad performance tool handlers ---


def _tool_ads_get_performance_summary(slack_user_id: str, entity: str, _input: dict) -> str:
    """Fetch blended ad performance summary (spend, ROAS, CAC, POAS, Amazon)."""
    lookback_days = int((_input or {}).get("lookback_days") or 30)
    return ads_client.get_performance_summary_text(
        lookback_days=lookback_days,
        channel=entity,
        user=slack_user_id,
    )


def _tool_ads_get_channel_breakdown(slack_user_id: str, entity: str, _input: dict) -> str:
    """Fetch per-channel ad performance breakdown."""
    lookback_days = int((_input or {}).get("lookback_days") or 30)
    return ads_client.get_channel_breakdown_text(
        lookback_days=lookback_days,
        channel=entity,
        user=slack_user_id,
    )


def _tool_ads_get_subbrand_performance(slack_user_id: str, entity: str, _input: dict) -> str:
    """Fetch per-sub-brand (Pure / Mood / Energy) ad performance."""
    lookback_days = int((_input or {}).get("lookback_days") or 30)
    return ads_client.get_subbrand_performance_text(
        lookback_days=lookback_days,
        channel=entity,
        user=slack_user_id,
    )


def _tool_ads_get_pixel_attribution(slack_user_id: str, entity: str, _input: dict) -> str:
    """Fetch first-party pixel attribution vs platform-reported ROAS."""
    lookback_days = int((_input or {}).get("lookback_days") or 30)
    return ads_client.get_pixel_attribution_text(
        lookback_days=lookback_days,
        channel=entity,
        user=slack_user_id,
    )


def _tool_ads_get_cm_waterfall(slack_user_id: str, entity: str, _input: dict) -> str:
    """Fetch CM1 → CM4 contribution margin waterfall."""
    lookback_days = int((_input or {}).get("lookback_days") or 30)
    return ads_client.get_cm_waterfall_text(
        lookback_days=lookback_days,
        channel=entity,
        user=slack_user_id,
    )


# --- F3E Shopify DTC tools ---


def _tool_f3e_shopify_sales_pulse(slack_user_id: str, entity: str, _input: dict) -> str:
    """Return F3E DTC sales summary from Shopify for a period."""
    period = (_input.get("period") or "today").lower()
    if period not in shopify_client.VALID_PERIODS:
        period = "today"
    try:
        summary = shopify_client.get_sales_pulse(period)
    except shopify_client.ShopifyConfigError as exc:
        log.warning("f3e_shopify_sales_pulse config error: %s", exc)
        return "I don't have that right now."
    except shopify_client.ShopifyConnectorError as exc:
        log.warning("f3e_shopify_sales_pulse connector error user=%s: %s", slack_user_id, exc)
        return "I don't have that right now."
    log.info(
        "f3e_shopify_sales_pulse user=%s entity=%s period=%s orders=%d net=%.2f",
        slack_user_id, entity, period, summary.order_count, summary.net_revenue_usd,
    )
    return shopify_client.format_sales_for_llm(summary)


def _tool_f3e_shopify_inventory(slack_user_id: str, entity: str, _input: dict) -> str:
    """Return F3E inventory levels from Shopify with low-stock flags."""
    low_stock_only = _input.get("low_stock_only", True)
    threshold = int(_input.get("threshold") or shopify_client.LOW_STOCK_THRESHOLD)
    try:
        variants = shopify_client.get_inventory_status(threshold)
    except shopify_client.ShopifyConfigError as exc:
        log.warning("f3e_shopify_inventory config error: %s", exc)
        return "I don't have that right now."
    except shopify_client.ShopifyConnectorError as exc:
        log.warning("f3e_shopify_inventory connector error user=%s: %s", slack_user_id, exc)
        return "I don't have that right now."
    log.info(
        "f3e_shopify_inventory user=%s entity=%s variants=%d low_stock_only=%s",
        slack_user_id, entity, len(variants), low_stock_only,
    )
    return shopify_client.format_inventory_for_llm(variants, bool(low_stock_only))


# --- F3E warehouse inventory pulse ---


def _tool_f3e_inventory_pulse(slack_user_id: str, entity: str, _input: dict) -> str:
    """Return F3E warehouse/3PL stock levels from the Cotton weekly batch report."""
    log.info("f3e_inventory_pulse user=%s entity=%s", slack_user_id, entity)
    return inventory_client.get_f3e_inventory_pulse_text()


# --- F3E location-aware inventory ---


def _tool_f3e_inventory_by_location(slack_user_id: str, entity: str, _input: dict) -> str:
    """Return F3E inventory for a named location.

    Routes by location:
      nimbl            -> live Shopify inventory_levels (real-time Nimbl sync)
      unis/warehouse   -> weekly Excel batch report (Cotton 3PL snapshot)
      office/117       -> weekly Excel batch report (117 office snapshot)

    Optional brand filter narrows to Pure / Mood / Energy.
    """
    location = (_input.get("location") or "").strip().lower()
    brand = (_input.get("brand") or "").strip() or None

    if not location:
        return (
            "f3e_inventory_by_location called without `location`. "
            "Ask the user which location to check: 'Nimbl', 'UNIS' (warehouse), or 'office'."
        )

    # Nimbl = live Shopify inventory_levels
    if location == "nimbl":
        try:
            skus = shopify_client.get_inventory_by_location(location, brand)
        except shopify_client.ShopifyConfigError as exc:
            log.warning("f3e_inventory_by_location nimbl config error: %s", exc)
            return "I don't have that right now."
        except shopify_client.ShopifyConnectorError as exc:
            log.warning(
                "f3e_inventory_by_location nimbl connector error user=%s: %s",
                slack_user_id, exc,
            )
            return "I don't have that right now."
        log.info(
            "f3e_inventory_by_location user=%s location=nimbl brand=%s skus=%d (live Shopify)",
            slack_user_id, brand or "ALL", len(skus),
        )
        return shopify_client.format_location_inventory_for_llm(skus, location, brand)

    # UNIS / warehouse / cotton, office / 117 -> weekly Excel snapshot
    log.info(
        "f3e_inventory_by_location user=%s location=%s brand=%s (weekly Excel)",
        slack_user_id, location, brand or "ALL",
    )
    return inventory_client.get_f3e_location_inventory_text(location, brand)


# --- F3E DTC inventory WRITE (staged) ---


def _load_shopify_write_config() -> tuple[frozenset[str], dict[str, str]]:
    """Parse the write-locations map. Returns:
        (allowed_names_lc, alias_lc -> canonical_name_lc)

    `allowed_names_lc` is the refuse-by-default allowlist (which Shopify location
    NAMES may be written, lowercased). `alias_lc` maps spoken names ("office") to
    a canonical allowlisted location name so a user need not type the exact
    Shopify label. Entries may be a bare string (name only) or a dict with
    `name` + optional `aliases`. Read fresh from disk (live-reload). FAIL-CLOSED:
    a missing or unparseable file returns (empty, empty) -> every write refused."""
    try:
        if not _SHOPIFY_WRITE_LOC_PATH.exists():
            log.warning("shopify write-locations map not found at %s -- refusing all writes",
                        _SHOPIFY_WRITE_LOC_PATH)
            return frozenset(), {}
        with open(_SHOPIFY_WRITE_LOC_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        entries = data.get("allowed_write_locations") or []
        allowed: set[str] = set()
        aliases: dict[str, str] = {}
        for entry in entries:
            if isinstance(entry, str):
                nm = entry.strip().lower()
                if nm:
                    allowed.add(nm)
            elif isinstance(entry, dict):
                nm = str(entry.get("name") or "").strip().lower()
                if not nm:
                    continue
                allowed.add(nm)
                for alias in (entry.get("aliases") or []):
                    al = str(alias).strip().lower()
                    if al:
                        aliases[al] = nm
        return frozenset(allowed), aliases
    except Exception as exc:  # noqa: BLE001 -- fail closed on any read/parse error
        log.warning("shopify write-locations map load failed (%s) -- refusing all writes", exc)
        return frozenset(), {}


def _audit_shopify_write(
    *, slack_user: str, channel: str, variant: str, location: str, old, new: int,
) -> None:
    """Append one line to logs/shopify-inventory-writes.jsonl. Audit failure must
    never break the reply (mirrors historical_access.audit)."""
    import json as _json
    import time as _time
    try:
        _SHOPIFY_WRITE_AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": int(_time.time()),
            "slack_user": slack_user,
            "channel": channel,
            "variant": variant,
            "location": location,
            "old": old,
            "new": new,
        }
        with _SHOPIFY_WRITE_AUDIT_PATH.open("a", encoding="utf-8") as fh:
            fh.write(_json.dumps(entry) + "\n")
    except Exception as exc:  # noqa: BLE001 -- audit failure must not break the write reply
        log.error("shopify inventory write audit failed: %s", exc)


def _resolve_shopify_location(query: str) -> tuple[int | None, str, list[str]]:
    """Resolve a spoken/typed location name to (location_id, canonical_name, all_names).

    Resolution order:
      1. a configured ALIAS ("office" -> "1337 S Gilbert Rd") -> that live location;
      2. exact (case-insensitive) live-location name;
      3. unique partial substring of a live-location name.
    Returns (None, "", all_names) when zero or multiple candidates match -- the
    caller asks the user rather than guessing. all_names is the full active-location
    list (for a helpful 'known locations' message). NOTE: an alias only resolves the
    NAME; the caller still enforces the allowlist, so an alias never bypasses it."""
    locations = shopify_client.get_active_locations()  # [{'id','name'}]
    all_names = [str(l["name"]) for l in locations]
    needle = (query or "").strip().lower()
    if not needle:
        return None, "", all_names
    _, aliases = _load_shopify_write_config()
    target = aliases.get(needle)
    if target:
        for l in locations:
            if str(l["name"]).strip().lower() == target:
                return int(l["id"]), str(l["name"]), all_names
        # Alias points at a name that is not a live active location -> no match.
        return None, "", all_names
    exact = [l for l in locations if str(l["name"]).strip().lower() == needle]
    if len(exact) == 1:
        return int(exact[0]["id"]), str(exact[0]["name"]), all_names
    partial = [l for l in locations if needle in str(l["name"]).strip().lower()]
    if len(partial) == 1:
        return int(partial[0]["id"]), str(partial[0]["name"]), all_names
    return None, "", all_names


# ── Pending-confirmation store for the DTC inventory write (HIGH-1, 2026-07-10) ──
# The write's identity binding is the TOOL's server-resolved ids, NOT an LLM echo.
# The first design (D-051) required the model to echo the exact preview labels into
# expected_item/expected_location; the model NORMALIZES the variant label (it emits
# 'F3 Pure Original (12 Pack)' -- the f3e_shopify_inventory formatter style it sees
# in context -- instead of the Shopify title 'F3 PURE Original Energy Drink - 12
# Pack'), so every confirm re-previewed forever and the write path was DEAD (proven
# live 2026-07-10, office count 202 never moved). Phase 1 stashes the resolved write
# here keyed on (slack_user, channel); Phase 2 confirmed=true executes the caller's
# OWN pending entry after a FRESH live-qty re-check. Single process, in-memory,
# TTL-bounded, single slot per key (a new preview overwrites); a restart clearing it
# is fine (the user just re-previews). Thread guard mirrors the _ONE_TAP_LOCK idiom.
_SHOPIFY_PENDING_LOCK = Lock()
_SHOPIFY_PENDING_TTL_SECONDS = 600  # 10 min
_PENDING_SHOPIFY_WRITES: dict[tuple[str, str], dict] = {}

_NOT_WRITTEN = "⚠️ NOT WRITTEN -- no inventory change was made."


def _shopify_pending_key(slack_user: str, channel: str) -> tuple[str, str]:
    return (slack_user or "", (channel or "").strip().lower())


def _store_pending_shopify_write(slack_user: str, channel: str, entry: dict) -> None:
    with _SHOPIFY_PENDING_LOCK:
        _PENDING_SHOPIFY_WRITES[_shopify_pending_key(slack_user, channel)] = entry


def _take_pending_shopify_write(slack_user: str, channel: str) -> dict | None:
    """Pop-and-return the caller's pending entry if present AND fresh, else None.
    Popping under the lock CLAIMS it so a concurrent duplicate confirm cannot
    double-execute (the absolute set is idempotent anyway; this keeps it clean)."""
    key = _shopify_pending_key(slack_user, channel)
    with _SHOPIFY_PENDING_LOCK:
        entry = _PENDING_SHOPIFY_WRITES.pop(key, None)
    if not entry:
        return None
    if (time.time() - float(entry.get("ts", 0))) > _SHOPIFY_PENDING_TTL_SECONDS:
        return None
    return entry


def has_pending_shopify_write(slack_user: str, channel: str) -> bool:
    """True if a fresh pending DTC inventory confirm exists for (user, channel).
    Read-only (does NOT claim). app.py model routing calls this to force Sonnet on
    the confirm turn -- a bare 'yes' is undetectable from message content."""
    key = _shopify_pending_key(slack_user, channel)
    with _SHOPIFY_PENDING_LOCK:
        entry = _PENDING_SHOPIFY_WRITES.get(key)
    return bool(entry) and (time.time() - float(entry.get("ts", 0))) <= _SHOPIFY_PENDING_TTL_SECONDS


def _normalize_inv_label(s: str) -> str:
    """Loose normalization for the OPTIONAL belt-and-suspenders expected_* check
    (never a gate): lowercase, keep only alphanumerics."""
    return "".join(c for c in (s or "").lower() if c.isalnum())


def _shopify_write_blocked(user_text: str) -> str:
    """Wrap a NON-write, user-facing message in the WRITE_BLOCKED contract. The
    claude_client narration net posts `user_text` verbatim (the part after the blank
    line) and OVERRIDES any success-claim the model might stream -- the tool owns the
    outcome text so a mis-narrating model can never imply a write that did not happen
    (HIGH-2, 2026-07-10; the 21:43 false 'units set' on a re-preview)."""
    return (
        "WRITE_BLOCKED -- post the lines after the blank as your reply verbatim; "
        "NOTHING was written, do NOT claim any inventory change. If it is a preview, "
        "the user must reply to confirm and you then call this tool again with "
        "confirmed=true (the same product / location / quantity).\n\n"
        f"{user_text}"
    )


def _shopify_preview_text(
    *, variant_label: str, location_name: str, current: int, quantity: int,
    moved_from: int | None = None,
) -> str:
    """Source-opaque NOT-WRITTEN preview line the net posts to the user."""
    moved = (f" The count moved since I checked (now {current}, was {moved_from})."
             if moved_from is not None else "")
    return (
        f"{_NOT_WRITTEN}{moved}\n"
        f"{variant_label} at {location_name}: {current} -> {quantity} units. "
        f"Reply \"confirm\" and I'll set it."
    )


def _shopify_resolve(slack_user_id: str, input_data: dict):
    """Validate + resolve the write target (shared by Phase 1 and the Phase-2
    re-preview). Returns (blocked_str, None) on any stop/ask/refuse, or
    (None, data) with data=dict(match, loc_id, loc_name, current, quantity)."""
    quantity_raw = input_data.get("quantity")
    if quantity_raw is None or str(quantity_raw).strip() == "":
        return _shopify_write_blocked(f"{_NOT_WRITTEN}\nWhat number should I set the count to?"), None
    try:
        quantity = int(quantity_raw)
    except (TypeError, ValueError):
        return _shopify_write_blocked(f"{_NOT_WRITTEN}\nGive me a whole number to set the count to."), None
    if quantity < 0:
        return _shopify_write_blocked(f"{_NOT_WRITTEN}\nThe count can't be negative -- give me zero or more."), None

    product_query = (input_data.get("product") or "").strip()
    location_query = (input_data.get("location") or "").strip()
    if not product_query:
        return _shopify_write_blocked(f"{_NOT_WRITTEN}\nWhich product or variant should I set? (a name or SKU)"), None
    if not location_query:
        return _shopify_write_blocked(f"{_NOT_WRITTEN}\nWhich location should I set it at? (I won't assume one.)"), None

    try:
        loc_id, loc_name, all_loc_names = _resolve_shopify_location(location_query)
    except (shopify_client.ShopifyConfigError, shopify_client.ShopifyConnectorError) as exc:
        log.warning("f3e_shopify_set_inventory location resolve error user=%s: %s", slack_user_id, exc)
        return _shopify_write_blocked(f"{_NOT_WRITTEN}\nI can't reach inventory right now -- try again in a moment."), None
    if loc_id is None:
        return _shopify_write_blocked(
            f"{_NOT_WRITTEN}\nI couldn't pin down a single location matching '{location_query}'. "
            f"Known locations: {', '.join(all_loc_names)}."), None

    allowed, _ = _load_shopify_write_config()
    if loc_name.strip().lower() not in allowed:
        allowed_display = ", ".join(sorted(n.title() for n in allowed)) or "none configured"
        return _shopify_write_blocked(
            f"{_NOT_WRITTEN}\nI can't set inventory at {loc_name} from here -- that count is kept "
            f"in sync automatically (a fulfillment partner owns it), so a manual change would be "
            f"overwritten. Manual updates go to: {allowed_display}. Changing {loc_name} is a call "
            f"for Harrison."), None

    try:
        matches = shopify_client.resolve_variants(product_query)
    except (shopify_client.ShopifyConfigError, shopify_client.ShopifyConnectorError) as exc:
        log.warning("f3e_shopify_set_inventory variant resolve error user=%s: %s", slack_user_id, exc)
        return _shopify_write_blocked(f"{_NOT_WRITTEN}\nI can't reach inventory right now -- try again in a moment."), None
    if not matches:
        return _shopify_write_blocked(
            f"{_NOT_WRITTEN}\nI couldn't find a product matching '{product_query}'. "
            f"Restate the name or give the SKU -- I won't guess."), None
    if len(matches) > 1:
        listing = "; ".join(f"{m.label}" + (f" [SKU {m.sku}]" if m.sku else "") for m in matches[:12])
        return _shopify_write_blocked(
            f"{_NOT_WRITTEN}\n'{product_query}' matches {len(matches)} variants: {listing}. Which one?"), None
    match = matches[0]

    try:
        current = shopify_client.get_inventory_level(match.inventory_item_id, loc_id)
    except (shopify_client.ShopifyConfigError, shopify_client.ShopifyConnectorError) as exc:
        log.warning("f3e_shopify_set_inventory level read error user=%s: %s", slack_user_id, exc)
        return _shopify_write_blocked(f"{_NOT_WRITTEN}\nI can't read the current count right now -- try again in a moment."), None
    if current is None:
        return _shopify_write_blocked(
            f"{_NOT_WRITTEN}\n{match.label} isn't stocked at {loc_name} yet, so I can't set a count "
            f"there -- the item has to be connected to that location first (a call for Harrison)."), None

    # Optional belt-and-suspenders (NEVER a gate, HIGH-1): if the model echoed an
    # expected_item, log a soft normalized-mismatch but PROCEED -- the pending store's
    # server-resolved ids are the identity binding, not the LLM echo.
    exp_item = str(input_data.get("expected_item") or "").strip()
    if exp_item and _normalize_inv_label(exp_item) != _normalize_inv_label(match.label):
        log.info("f3e_shopify_set_inventory expected_item soft-mismatch (ignored) user=%s exp=%r got=%r",
                 slack_user_id, exp_item, match.label)

    return None, {"match": match, "loc_id": loc_id, "loc_name": loc_name,
                  "current": current, "quantity": quantity}


def _store_and_preview_shopify(slack_user_id: str, channel: str, data: dict,
                               *, moved_from: int | None = None) -> str:
    """Stash the resolved write as the caller's pending confirm + return the
    NOT-WRITTEN preview (WRITE_BLOCKED)."""
    match = data["match"]
    _store_pending_shopify_write(slack_user_id, channel, {
        "inventory_item_id": match.inventory_item_id,
        "location_id": data["loc_id"],
        "target_qty": data["quantity"],
        "preview_qty": data["current"],
        "variant_label": match.label,
        "location_label": data["loc_name"],
        "ts": time.time(),
    })
    log.info("f3e_shopify_set_inventory PREVIEW user=%s item=%s loc=%s cur=%s -> %s",
             slack_user_id, match.inventory_item_id, data["loc_id"], data["current"], data["quantity"])
    return _shopify_write_blocked(_shopify_preview_text(
        variant_label=match.label, location_name=data["loc_name"],
        current=data["current"], quantity=data["quantity"], moved_from=moved_from))


def _shopify_execute_pending(slack_user_id: str, channel: str, pending: dict) -> str:
    """Phase 2 executor: FRESH live-qty re-check, then WRITE the caller's pending
    entry (identity = the tool's server-resolved ids). Re-previews on drift; never
    a blind write."""
    item_id = pending["inventory_item_id"]
    loc_id = pending["location_id"]
    target = int(pending["target_qty"])
    preview_qty = int(pending["preview_qty"])
    variant_label = pending["variant_label"]
    loc_name = pending["location_label"]

    # Defense-in-depth: re-check the allowlist (Harrison could have removed the
    # location from the YAML since the preview -- live-reload).
    allowed, _ = _load_shopify_write_config()
    if loc_name.strip().lower() not in allowed:
        return _shopify_write_blocked(
            f"{_NOT_WRITTEN}\nI can't set inventory at {loc_name} -- that location is kept in sync "
            f"automatically. Changing it is a call for Harrison.")

    try:
        live = shopify_client.get_inventory_level(item_id, loc_id)
    except (shopify_client.ShopifyConfigError, shopify_client.ShopifyConnectorError) as exc:
        log.warning("f3e_shopify_set_inventory confirm level read error user=%s: %s", slack_user_id, exc)
        return _shopify_write_blocked(f"{_NOT_WRITTEN}\nI couldn't read the current count just now -- try again in a moment.")
    if live is None:
        return _shopify_write_blocked(
            f"{_NOT_WRITTEN}\n{variant_label} isn't stocked at {loc_name} anymore, so I can't set a count there.")
    if live != preview_qty:
        # The live number moved between preview and confirm -> re-preview (re-store
        # a fresh pending so the user can confirm the updated number), NO write.
        _store_pending_shopify_write(slack_user_id, channel, {**pending, "preview_qty": live, "ts": time.time()})
        log.info("f3e_shopify_set_inventory CONCURRENCY re-preview user=%s item=%s preview=%s live=%s",
                 slack_user_id, item_id, preview_qty, live)
        return _shopify_write_blocked(_shopify_preview_text(
            variant_label=variant_label, location_name=loc_name,
            current=live, quantity=target, moved_from=preview_qty))

    try:
        new_available = shopify_client.set_inventory_level(item_id, loc_id, target)
    except (shopify_client.ShopifyConfigError, shopify_client.ShopifyConnectorError) as exc:
        log.warning("f3e_shopify_set_inventory WRITE FAILED user=%s item=%s loc=%s: %s",
                    slack_user_id, item_id, loc_id, exc)
        return _shopify_write_blocked(
            f"{_NOT_WRITTEN}\nThat DTC inventory update didn't go through -- try again shortly.")

    _audit_shopify_write(slack_user=slack_user_id, channel=channel,
                         variant=variant_label, location=loc_name, old=live, new=new_available)
    log.info("f3e_shopify_set_inventory WROTE user=%s item=%s loc=%s %s -> %s",
             slack_user_id, item_id, loc_id, live, new_available)
    return (
        f"WRITE_CONFIRMED -- post the line after the blank as your entire response "
        f"(no preamble, no meta-commentary, do not name the store or platform):\n\n"
        f"DTC inventory updated -- {variant_label} at {loc_name}: {live} -> {new_available} units."
    )


def _repreview_pending_new_target(slack_user_id: str, channel: str, pending: dict, new_qty: int) -> str:
    """The user changed the target on the confirm turn ('yes, but make it 210').
    Re-preview the NEW target against the SAME server-resolved item/location from the
    pending entry -- do NOT discard those ids and re-resolve from free text (which
    dead-ends when the model omits product/location, review #2/#7). Re-stores a fresh
    pending so the follow-up confirm works. NO write."""
    item_id = pending["inventory_item_id"]
    loc_id = pending["location_id"]
    variant_label = pending["variant_label"]
    loc_name = pending["location_label"]
    try:
        live = shopify_client.get_inventory_level(item_id, loc_id)
    except (shopify_client.ShopifyConfigError, shopify_client.ShopifyConnectorError) as exc:
        log.warning("f3e_shopify_set_inventory new-target level read error user=%s: %s", slack_user_id, exc)
        return _shopify_write_blocked(f"{_NOT_WRITTEN}\nI can't read the current count right now -- try again in a moment.")
    if live is None:
        return _shopify_write_blocked(
            f"{_NOT_WRITTEN}\n{variant_label} isn't stocked at {loc_name} anymore, so I can't set a count there.")
    _store_pending_shopify_write(slack_user_id, channel, {
        "inventory_item_id": item_id, "location_id": loc_id, "target_qty": new_qty,
        "preview_qty": live, "variant_label": variant_label, "location_label": loc_name,
        "ts": time.time(),
    })
    log.info("f3e_shopify_set_inventory RE-PREVIEW(new target) user=%s item=%s loc=%s cur=%s -> %s",
             slack_user_id, item_id, loc_id, live, new_qty)
    return _shopify_write_blocked(_shopify_preview_text(
        variant_label=variant_label, location_name=loc_name, current=live, quantity=new_qty))


def _tool_f3e_shopify_set_inventory(slack_user_id: str, entity: str, _input: dict) -> str:
    """Crash-safe wrapper (review #1): a WRITE tool must fail SOURCE-OPAQUE and say
    NOT WRITTEN, never surface a raw crash string. Any unexpected exception becomes a
    WRITE_BLOCKED NOT-WRITTEN reply (which the narration net then posts cleanly)."""
    try:
        return _shopify_set_inventory_impl(slack_user_id, entity, _input)
    except Exception as exc:  # noqa: BLE001 -- fail closed + source-opaque, never a raw crash
        log.exception("f3e_shopify_set_inventory crashed user=%s: %s", slack_user_id, exc)
        return _shopify_write_blocked(
            f"{_NOT_WRITTEN}\nSomething went wrong on my end -- the count was not changed. "
            f"Try again in a moment.")


def _shopify_set_inventory_impl(slack_user_id: str, entity: str, _input: dict) -> str:
    """Set F3E DTC inventory for one variant at one location. Staged-write tool.

    Phase 1 (confirmed != true): resolve the product/variant + location, refuse
    externally-synced locations via the refuse-by-default allowlist, read the CURRENT
    quantity, STASH the resolved write as the caller's pending confirm (keyed on
    slack_user + channel, TTL 10 min), and return a NOT-WRITTEN preview.
    Phase 2 (confirmed=true): execute the caller's OWN pending entry -- identity is
    the tool's server-resolved ids, NOT an LLM echo (HIGH-1) -- after a FRESH live-qty
    re-check (drift -> re-preview; match -> WRITE). A changed target re-previews
    against the same resolved ids; no fresh pending falls back to a fresh resolve.
    Never a blind write.

    Every non-write return is WRITE_BLOCKED-wrapped and leads with a NOT-WRITTEN
    marker; the claude_client narration net posts the tool's own outcome text so a
    mis-narrating model can never claim a phantom write (HIGH-2).

    Scope: F3E channels + Harrison/FNDR/HJRG cross-entity. Source-opaque output.
    """
    input_data = _input or {}
    ent = (entity or "").upper()
    channel = str(input_data.get("_channel_name") or "")

    # --- Scope guard (defense-in-depth; tools_for_entity already gates exposure) ---
    is_founder = slack_user_id == _HARRISON_SLACK_ID or ent in ("FNDR", "HJRG")
    if not (ent == "F3E" or is_founder):
        return _shopify_write_blocked(
            f"{_NOT_WRITTEN}\nDTC inventory updates are only available from F3E channels."
        )

    confirmed = input_data.get("confirmed", False) is True

    # --- Phase 2: confirmed -> execute the caller's pending write ------------------
    if confirmed:
        pending = _take_pending_shopify_write(slack_user_id, channel)
        if pending is not None:
            # Did the model pass a DIFFERENT target than was previewed?
            new_qty = None
            q_raw = input_data.get("quantity")
            if q_raw is not None and str(q_raw).strip() != "":
                try:
                    new_qty = int(q_raw)
                except (TypeError, ValueError):
                    new_qty = None
            if new_qty is None or new_qty == int(pending["target_qty"]):
                return _shopify_execute_pending(slack_user_id, channel, pending)
            if new_qty < 0:
                # Keep the (unchanged) pending alive; just ask for a valid number.
                _store_pending_shopify_write(slack_user_id, channel, {**pending, "ts": time.time()})
                return _shopify_write_blocked(
                    f"{_NOT_WRITTEN}\nThe count can't be negative -- give me zero or more.")
            # User changed the number -> re-preview the NEW target against the SAME
            # resolved item/location (reuse the pending's ids; never dead-end).
            return _repreview_pending_new_target(slack_user_id, channel, pending, new_qty)
        # No fresh pending (expired / never previewed / a machine restart cleared it)
        # -> resolve fresh and re-preview.
        blocked, data = _shopify_resolve(slack_user_id, input_data)
        if blocked:
            return blocked
        return _store_and_preview_shopify(slack_user_id, channel, data)

    # --- Phase 1: resolve + preview ------------------------------------------------
    blocked, data = _shopify_resolve(slack_user_id, input_data)
    if blocked:
        return blocked
    return _store_and_preview_shopify(slack_user_id, channel, data)


# --- Calendar meeting scheduling ---


def _tool_calendar_schedule_meeting(slack_user_id: str, entity: str, _input: dict) -> str:
    """Find the next available slot for all participants and propose or book a meeting.

    Two-phase staged-write:
    Phase 1 (confirmed=False): resolves participants, calls freebusy, finds the
    next common open slot in the next 7 working days (Mon-Fri 9am-5pm AZ), and
    returns a preview block for the user to confirm.
    Phase 2 (confirmed=True): creates the Google Calendar event using the
    proposed_start and proposed_end passed by Claude from Phase 1, sends invites.

    Participant names are resolved via the same alias system as other tools.
    The requester is always included automatically.
    """
    input_data       = _input or {}
    confirmed        = input_data.get("confirmed", False)
    duration_minutes = max(15, int(input_data.get("duration_minutes") or 30))
    title            = (input_data.get("title") or "").strip() or "Meeting"

    # --- Resolve requester ---
    user_map  = _load_slack_asana_map()
    requester = user_map.get(slack_user_id)
    if not requester:
        return (
            f"calendar_schedule_meeting: requesting user {slack_user_id} is not in "
            f"the user map. Harrison can add them to data/maps/slack-to-asana.yaml "
            f"(the asana_email field doubles as the Google Calendar identity)."
        )
    requester_email = (requester.get("asana_email") or "").strip()
    requester_name  = (requester.get("display_name") or slack_user_id).strip()
    if not requester_email:
        return (
            f"calendar_schedule_meeting: {requester_name} has no asana_email in "
            f"the user map -- cannot resolve their Google Calendar identity."
        )

    # --- Resolve named participants ---
    participant_raw = input_data.get("participants") or []
    if isinstance(participant_raw, str):
        participant_raw = [p.strip() for p in participant_raw.split(",") if p.strip()]

    # resolved = [(display_name, google_email), ...], requester always first
    resolved: list[tuple[str, str]] = [(requester_name, requester_email)]
    unresolved: list[str] = []

    for name_or_id in participant_raw:
        name_or_id = str(name_or_id).strip()
        if not name_or_id:
            continue
        # Skip if the user named themselves
        if name_or_id == slack_user_id or name_or_id.lower() == requester_name.lower():
            continue
        sid, _ = resolve_name_to_slack_user_id(name_or_id, entity)
        if sid and sid in user_map:
            u       = user_map[sid]
            email   = (u.get("asana_email") or "").strip()
            display = (u.get("display_name") or name_or_id).strip()
            if email:
                resolved.append((display, email))
            else:
                unresolved.append(name_or_id)
        else:
            unresolved.append(name_or_id)

    if unresolved:
        known = sorted(
            u.get("display_name", "") for u in user_map.values() if u.get("display_name")
        )
        return (
            f"calendar_schedule_meeting: could not find these participants in the team "
            f"roster: {', '.join(unresolved)}. "
            f"Known team members: {', '.join(known)}. "
            f"Ask the user to clarify who they meant, or confirm the person is listed in "
            f"data/maps/slack-to-asana.yaml."
        )

    if len(resolved) < 2:
        return (
            "calendar_schedule_meeting: at least 2 participants are needed. "
            "The requester is included automatically -- ask the user to name at least "
            "one other person."
        )

    names  = [n for n, _ in resolved]
    emails = [e for _, e in resolved]

    # -- Phase 2: Book the confirmed slot -------------------------------------
    if confirmed is True:
        proposed_start = (input_data.get("proposed_start") or "").strip()
        proposed_end   = (input_data.get("proposed_end") or "").strip()
        if not proposed_start or not proposed_end:
            return (
                "calendar_schedule_meeting: confirmed=true but proposed_start or "
                "proposed_end is missing. Re-run with confirmed=false first to find a "
                "slot, then pass the exact start/end strings from that proposal."
            )
        try:
            event = calendar_client.create_event(
                user_email=requester_email,
                summary=title,
                start=proposed_start,
                end=proposed_end,
                attendees=emails,
                description=f"Scheduled by Cora on behalf of {requester_name}.",
                time_zone=calendar_client._DEFAULT_TZ,
            )
        except calendar_client.CalendarClientError as exc:
            log.warning(
                "calendar_schedule_meeting BOOK FAILED requester=%s title=%r exc=%s",
                slack_user_id, title, exc,
            )
            return (
                f"Meeting booking failed: {exc}. "
                f"Tell the user the event was not created. "
                f"If the error mentions a missing DWD scope, Harrison needs to update "
                f"Domain-wide Delegation in admin.google.com to include "
                f"https://www.googleapis.com/auth/calendar.events."
            )
        log.info(
            "calendar_schedule_meeting BOOKED requester=%s event_id=%s title=%r "
            "attendee_count=%d start=%s",
            slack_user_id, event.get("id", ""), title, len(emails), proposed_start,
        )
        return calendar_client.format_created_event_for_llm(event, user_email=requester_email)

    # -- Phase 1: Find up to 3 available slots --------------------------------
    try:
        slots = calendar_client.find_meeting_slots(
            requester_email=requester_email,
            calendar_emails=emails,
            duration_minutes=duration_minutes,
            n=3,
            search_days=14,
        )
    except calendar_client.CalendarClientError as exc:
        log.warning(
            "calendar_schedule_meeting FREEBUSY FAILED requester=%s exc=%s",
            slack_user_id, exc,
        )
        return (
            f"Could not check calendar availability: {exc}. "
            f"If the error mentions a missing DWD scope, Harrison needs to ensure "
            f"https://www.googleapis.com/auth/calendar.events is in Domain-wide "
            f"Delegation for the Cora service account (admin.google.com)."
        )

    log.info(
        "calendar_schedule_meeting SLOTS FOUND requester=%s participants=%s slots=%d dur=%dmin",
        slack_user_id, emails, len(slots), duration_minutes,
    )
    return calendar_client.format_slot_proposals_for_llm(slots, names, title=title)


def _tool_f3e_hubspot_pipeline_summary(slack_user_id: str, entity: str, _input: dict) -> str:
    """Fetch the F3E HubSpot pipeline summary and return formatted text for Claude."""
    try:
        return hubspot_client.get_f3e_pipeline_summary_text()
    except hubspot_client.HubSpotClientError as exc:
        log.warning("f3e_hubspot_pipeline_summary error user=%s: %s", slack_user_id, exc)
        return (
            f"f3e_hubspot_pipeline_summary: HubSpot call failed -- {exc}. "
            "Apologize to the user and suggest they try again in a moment."
        )


def _tool_f3e_ai_visibility(slack_user_id: str, entity: str, _input: dict) -> str:
    """Return the latest F3 AI-visibility scores + WoW deltas + top competitor gaps.

    Read-only. F3E + FNDR/HJRG only. PHI guard OFF (F3 marketing/visibility data,
    no health data). No external writes.
    """
    entity_upper = (entity or "").upper()
    if not (entity_upper.startswith("F3E") or entity_upper in ("FNDR", "HJRG")):
        return "AI visibility metrics are scoped to F3 Energy channels."
    log.info("f3e_ai_visibility user=%s entity=%s", slack_user_id, entity)
    try:
        from ..ai_visibility import report  # noqa: PLC0415 -- lazy read-only import
        return report.get_tool_summary()
    except Exception as exc:  # noqa: BLE001
        log.warning("f3e_ai_visibility error user=%s: %s", slack_user_id, exc)
        return ("f3e_ai_visibility: I don't have the AI visibility scores right now -- "
                f"{exc}. Apologize and suggest trying again shortly.")


def _tool_fndr_contracts_dashboard(slack_user_id: str, entity: str, _input: dict) -> str:
    """Fetch the FNDR/HJRG contracts and renewals dashboard from Notion."""
    try:
        return notion_client.get_contracts_dashboard_text()
    except notion_client.NotionClientError as exc:
        log.warning("fndr_contracts_dashboard error user=%s: %s", slack_user_id, exc)
        return (
            f"fndr_contracts_dashboard: I don't have that right now -- {exc}. "
            "Apologize to the user and suggest they check back shortly."
        )


def _tool_fndr_press_pipeline_summary(slack_user_id: str, entity: str, _input: dict) -> str:
    """Fetch the FNDR/HJRG press-acquisition pipeline summary from Notion.

    Scope: founder-level only. Refuses in entity channels (press strategy is a
    portfolio concern, mirroring the cross-entity firewall doctrine).
    """
    entity_upper = (entity or "FNDR").upper().strip()
    if entity_upper not in ("FNDR", "HJRG"):
        return (
            "The press pipeline is a founder-level view -- ask me about it in a "
            "founder or HJR Global channel."
        )
    try:
        return notion_client.get_press_pipeline_summary_text()
    except notion_client.NotionClientError as exc:
        log.warning("fndr_press_pipeline_summary error user=%s: %s", slack_user_id, exc)
        return (
            f"fndr_press_pipeline_summary: I don't have that right now -- {exc}. "
            "Apologize to the user and suggest they check back shortly."
        )


# ---------------------------------------------------------------------------
# PhotoRoom image generation tool handlers
# ---------------------------------------------------------------------------


def _tool_f3_generate_image(slack_user_id: str, entity: str, _input: dict) -> str:
    """Delegate to generate_image.handle_f3_generate_image."""
    return generate_image.handle_f3_generate_image(slack_user_id, entity, _input)


def _tool_f3_batch_image_run(slack_user_id: str, entity: str, _input: dict) -> str:
    """Delegate to generate_image.handle_f3_batch_image_run."""
    return generate_image.handle_f3_batch_image_run(slack_user_id, entity, _input)


def _tool_f3_create_image(slack_user_id: str, entity: str, _input: dict) -> str:
    """Delegate to generate_image.handle_f3_create_image."""
    return generate_image.handle_f3_create_image(slack_user_id, entity, _input)


def _tool_f3_create_sales_deck(slack_user_id: str, entity: str, _input: dict) -> str:
    """Delegate to sales_deck_client.handle_f3_create_sales_deck."""
    return sales_deck_client.handle_f3_create_sales_deck(slack_user_id, entity, _input)


# --- LEX-specific tool handlers ---


def _tool_lex_revalidation_status(slack_user_id: str, entity: str, _input: dict) -> str:
    """Return AZ DDD Therapy Revalidation status from Asana.

    Reads task 1215070649606664 (root task + subtasks + latest comment).
    Returns days-remaining to 2026-06-30, open sub-task blockers, last-comment
    age, and a deep link to the Asana task.

    Scoped to LEX / LEX-* entities and FNDR/HJRG. Tool description instructs
    Claude to ALWAYS call this tool for revalidation questions rather than
    answering from KB memory.
    """
    log.info("lex_revalidation_status user=%s entity=%s", slack_user_id, entity)
    return lex_client.get_revalidation_status()


def _tool_hjrp_lease_status(slack_user_id: str, entity: str, _input: dict) -> str:
    """Return the HJRP lease register: renewal countdowns, clusters, vacancies, brokers.

    Lease economics (monthly rent, rent-at-risk) are financial -> gated to HJRP
    (or founder) entities AND TIER_1 channels (#hjrp-finance / #hjrp-leadership).
    Mirrors the financial-tool gating pattern; the system prompt also restricts it.
    """
    entity_upper = (entity or "").upper()
    if not (entity_upper.startswith("HJRP") or entity_upper in ("FNDR", "HJRG")):
        return "Lease details are scoped to HJR Properties channels."
    channel_name = (_input or {}).get("_channel_name", "")
    if not _is_tier1_channel(entity, channel_name):
        return _FINANCE_CHANNEL_REQUIRED
    log.info("hjrp_lease_status user=%s entity=%s", slack_user_id, entity)
    return hjrp_client.get_lease_status()


def _tool_hubspot_update_deal_stage(slack_user_id: str, entity: str, _input: dict) -> str:
    """Update a HubSpot deal's pipeline stage. Staged-write tool.

    First call (confirmed=False or missing): fetches deal name + current stage,
    returns a human-readable preview string asking for explicit confirmation.
    Second call (confirmed=True): executes the stage update via PATCH and returns
    a WRITE_CONFIRMED response.

    Scope: FNDR, F3E, OSN, BDM only. Blocked from LEX channels.
    """
    import os
    from slack_sdk import WebClient as _SlackWebClient
    from slack_sdk.errors import SlackApiError as _SlackApiError

    input_data = _input or {}

    # Scope guard
    if entity and entity.upper().startswith("LEX"):
        return (
            "hubspot_update_deal_stage blocked: HubSpot write tools are not available "
            "from Lex channels. Use a non-Lex channel or contact Harrison."
        )

    deal_id = (input_data.get("deal_id") or "").strip()
    stage_id = (input_data.get("stage_id") or "").strip()
    confirmed = input_data.get("confirmed", False)

    if not deal_id:
        return "hubspot_update_deal_stage: missing `deal_id`. Ask the user for the HubSpot deal ID."
    if not stage_id:
        return "hubspot_update_deal_stage: missing `stage_id`. Ask the user for the target stage ID."

    # Fetch current deal info for preview (needed for both confirmed and unconfirmed)
    try:
        deal_props = hubspot_client.get_deal(deal_id)
    except hubspot_client.HubSpotClientError as exc:
        return f"hubspot_update_deal_stage: could not fetch deal {deal_id}: {exc}"

    deal_name = deal_props.get("dealname") or "(unnamed)"
    current_stage_id = deal_props.get("dealstage") or ""
    # Ensure stage cache is warm
    if not hubspot_client._STAGE_NAME_CACHE:
        try:
            hubspot_client._refresh_pipeline_cache()
        except hubspot_client.HubSpotClientError:
            pass
    current_stage_name = hubspot_client._STAGE_NAME_CACHE.get(current_stage_id, current_stage_id)
    new_stage_name = hubspot_client._STAGE_NAME_CACHE.get(stage_id, stage_id)

    if confirmed is not True:
        return (
            f"WRITE_PREVIEW Update deal '{deal_name}' stage from "
            f"'{current_stage_name}' to '{new_stage_name}'? "
            f"Respond with confirmed=True to proceed."
        )

    # Execute the update
    try:
        hubspot_client.update_deal_stage(deal_id, stage_id)
    except hubspot_client.HubSpotClientError as exc:
        log.warning(
            "hubspot_update_deal_stage FAILED asker=%s deal_id=%s stage_id=%s exc=%s",
            slack_user_id, deal_id, stage_id, exc,
        )
        return (
            f"HubSpot update failed: {exc}. Tell the user the stage was not changed "
            "and suggest they update it directly in HubSpot."
        )

    deal_url = hubspot_client._deal_url(deal_id)
    log.info(
        "hubspot_update_deal_stage UPDATED asker=%s deal_id=%s deal_name=%r "
        "old_stage=%r new_stage=%r",
        slack_user_id, deal_id, deal_name, current_stage_name, new_stage_name,
    )

    return (
        f"WRITE_CONFIRMED -- post the following lines as your entire response "
        f"(no preamble, no meta-commentary, just these lines):\n\n"
        f"Updated <{deal_url}|{deal_name}> stage: {current_stage_name} -> *{new_stage_name}*."
    )


def _tool_hubspot_add_note(slack_user_id: str, entity: str, _input: dict) -> str:
    """Add a note to a HubSpot deal. Staged-write tool.

    First call (confirmed=False or missing): returns a preview showing the deal
    name and note body, asking for confirmation.
    Second call (confirmed=True): calls hubspot_client.create_note() and returns
    a WRITE_CONFIRMED response.

    Scope: FNDR, F3E, OSN, BDM, HJRG channels. Blocked from LEX channels.
    """
    input_data = _input or {}

    # Scope guard
    if entity and entity.upper().startswith("LEX"):
        return (
            "hubspot_add_note blocked: HubSpot write tools are not available "
            "from Lex channels. Use a non-Lex channel or contact Harrison."
        )

    deal_id = (input_data.get("deal_id") or "").strip()
    note_body = (input_data.get("note_body") or "").strip()
    confirmed = input_data.get("confirmed", False)

    if not deal_id:
        return "hubspot_add_note: missing `deal_id`. Ask the user which deal to note."
    if not note_body:
        return "hubspot_add_note: missing `note_body`. Ask the user what the note should say."

    # Fetch deal name for preview
    try:
        deal_props = hubspot_client.get_deal(deal_id)
    except hubspot_client.HubSpotClientError as exc:
        return f"hubspot_add_note: could not fetch deal {deal_id}: {exc}"

    deal_name = deal_props.get("dealname") or "(unnamed)"

    if confirmed is not True:
        preview_body = note_body[:300] + ("..." if len(note_body) > 300 else "")
        return (
            f"WRITE_PREVIEW Add note to '{deal_name}':\n\n"
            f"{preview_body}\n\n"
            f"Respond with confirmed=True to add this note."
        )

    # Execute note creation
    try:
        note_id = hubspot_client.create_note(body=note_body, deal_id=deal_id)
    except hubspot_client.HubSpotClientError as exc:
        log.warning(
            "hubspot_add_note FAILED asker=%s deal_id=%s exc=%s",
            slack_user_id, deal_id, exc,
        )
        return (
            f"HubSpot note creation failed: {exc}. Tell the user the note was not saved "
            "and suggest they add it directly in HubSpot."
        )

    deal_url = hubspot_client._deal_url(deal_id)
    log.info(
        "hubspot_add_note CREATED asker=%s deal_id=%s deal_name=%r note_id=%s chars=%d",
        slack_user_id, deal_id, deal_name, note_id, len(note_body),
    )

    return (
        f"WRITE_CONFIRMED -- post the following lines as your entire response "
        f"(no preamble, no meta-commentary, just these lines):\n\n"
        f"Note added to <{deal_url}|{deal_name}>."
    )


def _tool_slack_send_dm(slack_user_id: str, entity: str, _input: dict) -> str:
    """Send a Slack DM to a named teammate on behalf of Cora.

    Fourth write tool. Same staged-write doctrine as the other write tools:
    refuses to fire without confirmed=true; Claude must show a preview and
    get explicit user approval before calling with confirmed=true.

    Guardrails:
    - LEX channels are blocked (PHI risk -- no DMs triggered from Lex context).
    - Only sends to users present in slack-to-asana.yaml (mapped teammates).
    - Cora signs the message as itself -- no impersonation.
    - No PHI, no financial data, no cross-entity information in DMs.
    """
    import os
    from slack_sdk import WebClient as _SlackWebClient
    from slack_sdk.errors import SlackApiError as _SlackApiError

    input_data = _input or {}

    # PHI / LEX guardrail
    if entity and entity.upper().startswith("LEX"):
        return (
            "slack_send_dm blocked: DMs cannot be triggered from Lex channels "
            "due to PHI guardrails. If this is non-PHI coordination, ask Harrison "
            "to send the message from a non-Lex channel."
        )

    # Confirmation gate
    confirmed = input_data.get("confirmed", False)
    if confirmed is not True:
        return (
            "slack_send_dm refused: `confirmed` must be set to true ONLY "
            "after you have shown the user a preview (recipient + full message text) "
            "AND received their explicit approval ('yes', 'send it', 'go ahead', or "
            "similar). Format a clear preview NOW if you have not done that yet."
        )

    recipient_name = (input_data.get("recipient_name") or "").strip()
    message = (input_data.get("message") or "").strip()

    if not recipient_name:
        return "slack_send_dm: missing `recipient_name`. Ask the user who should receive the DM."
    if not message:
        return "slack_send_dm: missing `message`. Ask the user what the DM should say."

    # Resolve recipient to Slack user ID
    resolved_id, info = resolve_name_to_slack_user_id(recipient_name, channel_entity=entity)
    if not resolved_id:
        return (
            f"slack_send_dm: could not resolve '{recipient_name}' to a Slack user. "
            f"{info or 'Check the name and try again.'}"
        )

    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        return "slack_send_dm: SLACK_BOT_TOKEN not configured. Tell Harrison."

    try:
        client = _SlackWebClient(token=token)
        # Open (or reuse) a DM channel with the recipient
        open_resp = client.conversations_open(users=[resolved_id])
        dm_channel = open_resp["channel"]["id"]
        send_resp = client.chat_postMessage(channel=dm_channel, text=message)
    except _SlackApiError as exc:
        log.warning(
            "slack_send_dm FAILED asker=%s recipient=%s (%s) exc=%s",
            slack_user_id, recipient_name, resolved_id, exc,
        )
        return (
            f"Slack DM error: {exc.response.get('error', str(exc))}. "
            f"Tell the user the message wasn't sent and suggest they send it manually."
        )

    ts = send_resp.get("ts", "")
    log.info(
        "slack_send_dm SENT asker=%s recipient=%s (%s) ts=%s chars=%d",
        slack_user_id, recipient_name, resolved_id, ts, len(message),
    )

    recipient_map = _load_slack_asana_map().get(resolved_id, {})
    display_name = recipient_map.get("display_name", recipient_name)

    return (
        f"WRITE_CONFIRMED -- post the following lines as your entire response "
        f"(no preamble, no meta-commentary, just these lines):\n\n"
        f"DM sent to {display_name}."
    )


# --- Personal notes (Org Synthesis Phase 5, deliverable 1) ---
#
# Any teammate can teach Cora a personal note ("Cora, remember X"). Notes are
# blast-radius-1: stored in the KB under source="user_note" + owner metadata,
# retrievable ONLY by their owner (SQL-layer exclusion in store.search /
# store.search_user_notes — D-034 pattern, never prompts). D-011 untouched:
# a personal note is the user's own data, not canonical memory; org-wide
# promotion (share_requested) ships in deliverable 2 behind Harrison's gate.


def _notes_kb():
    """Shared KnowledgeBase instance + lock from context_loader (lazy import)."""
    from cora import context_loader
    return context_loader.get_shared_kb(), context_loader._SHARED_KB_LOCK


def _tool_cora_remember(slack_user_id: str, entity: str, _input: dict) -> str:
    """Save a personal note for the asking user (staged-write, confirmed gate).

    PHI save matrix (deterministic, user_notes.resolve_save_scope): PHI-flagged
    text saves only for a LEX PHI custodian in LEX scope or DM (forced into
    LEX scope); everyone else gets the standard PHI refusal. Save-time conflict
    check probes the canonical KB and appends a heads-up — never blocks.
    """
    from cora import user_notes

    input_data = _input or {}
    note_text = str(input_data.get("note_text", "") or "").strip()
    if not note_text:
        return "cora_remember: note_text is required and cannot be empty."
    is_dm = str(input_data.get("_channel_id", "") or "").startswith("D")

    # PHI / scope gate runs BEFORE the staged-write confirm gate. A save that
    # PHI policy will refuse (e.g. a non-custodian saving a named individual's
    # billing/authorization in a LEX channel) is rejected on the FIRST tool
    # call -- never staged as a "Saving to YOUR notes..." preview, never
    # confirmed. Deterministic, code-layer (D-034); the preview/confirm round
    # trip below is reached only for an allowed save.
    decision = user_notes.resolve_save_scope(note_text, entity, slack_user_id, is_dm)
    if not decision.allowed:
        log.info(
            "cora_remember PHI-REFUSED owner=%s entity=%s is_dm=%s",
            slack_user_id, entity, is_dm,
        )
        return decision.reason

    confirmed = input_data.get("confirmed", False)
    if confirmed is not True:
        return (
            "cora_remember refused: `confirmed` must be set to true ONLY after "
            "you have shown the user a preview of the note — format it as "
            "\"Saving to YOUR notes (only you can retrieve this): <note text>\" — "
            "and received explicit approval. Show the preview, wait for their "
            "yes, then call again with confirmed=true."
        )

    share_requested = bool(input_data.get("share_requested", False))
    kb, kb_lock = _notes_kb()
    if kb is None:
        return (
            "Notes storage is unavailable right now — tell the user the note "
            "was NOT saved and to try again shortly."
        )

    owner_email = str(
        (_load_slack_asana_map().get(slack_user_id) or {}).get("asana_email", "") or ""
    ).strip()

    with kb_lock:
        conflict = user_notes.conflict_excerpt(kb, note_text, decision.entity)
        note_id = user_notes.save_note(
            kb,
            note_text=note_text,
            owner_slack=slack_user_id,
            owner_email=owner_email,
            entity=decision.entity,
            sub_entity=decision.sub_entity,
            share_requested=share_requested,
            channel_name=str(input_data.get("_channel_name", "") or ""),
        )

    lines = [
        "WRITE_CONFIRMED -- post the following as your entire response "
        "(no preamble, no meta-commentary):",
        "",
        "Saved to your notes. Only you can retrieve it -- ask me about it any time.",
    ]
    if share_requested:
        lines.append(
            "You asked to share it org-wide: org-wide sharing goes through "
            "Harrison's review, which isn't wired up yet -- for now the note "
            "is saved privately and flagged for that review."
        )
    if conflict:
        lines.append(
            f"Heads up -- this may conflict with existing org knowledge: {conflict}"
        )
    log.info(
        "cora_remember SAVED owner=%s id=%s entity=%s conflict=%s",
        slack_user_id, note_id, decision.entity, bool(conflict),
    )
    return "\n".join(lines)


def _tool_cora_my_notes(slack_user_id: str, entity: str, _input: dict) -> str:
    """List the asking user's own personal notes (owner-only, read-only)."""
    kb, kb_lock = _notes_kb()
    if kb is None:
        return "Notes storage is unavailable right now -- please try again shortly."
    with kb_lock:
        notes = kb.list_user_notes(slack_user_id)
    if not notes:
        return (
            "You have no saved personal notes. Tell the user they can save one "
            "any time with 'Cora, remember ...'."
        )
    lines = [f"Your saved personal notes ({len(notes)}):", ""]
    for i, n in enumerate(notes, 1):
        date_str = ""
        if n.get("date_created"):
            try:
                import datetime as _dt
                date_str = _dt.date.fromtimestamp(n["date_created"]).isoformat()
            except (OSError, ValueError, OverflowError):
                pass
        text = (n.get("content") or "").strip().replace("\n", " ")
        if len(text) > 160:
            text = text[:160] + "..."
        short_id = str(n.get("note_id", "")).rsplit(":", 1)[-1]
        lines.append(f"{i}. [{short_id}] {date_str} -- {text}")
    lines.append("")
    lines.append(
        "(These are the asker's OWN private notes -- present them only to the "
        "asker. To delete one, they can say 'forget that note' and you confirm "
        "which, then call cora_forget_note with its id.)"
    )
    return "\n".join(lines)


def _tool_cora_forget_note(slack_user_id: str, entity: str, _input: dict) -> str:
    """Delete one of the asking user's own notes (staged-write, owner-only)."""
    input_data = _input or {}
    confirmed = input_data.get("confirmed", False)
    if confirmed is not True:
        return (
            "cora_forget_note refused: `confirmed` must be set to true ONLY "
            "after you have shown the user WHICH note will be deleted (use "
            "cora_my_notes to find it) and received explicit approval."
        )
    note_id = str(input_data.get("note_id", "") or "").strip()
    if not note_id:
        return "cora_forget_note: note_id is required (find it via cora_my_notes)."

    kb, kb_lock = _notes_kb()
    if kb is None:
        return "Notes storage is unavailable right now -- the note was NOT deleted."

    # Accept either the full id ("note:U123:abcdef1234") or the short token
    # shown by cora_my_notes — resolved against the ASKER'S OWN notes only.
    if not note_id.startswith("note:"):
        with kb_lock:
            notes = kb.list_user_notes(slack_user_id)
        matches = [n["note_id"] for n in notes if n["note_id"].rsplit(":", 1)[-1] == note_id]
        if not matches:
            return "No note of yours matches that id -- nothing was deleted."
        note_id = matches[0]

    with kb_lock:
        deleted = kb.delete_user_note(note_id, owner_slack=slack_user_id)
    if deleted == 0:
        # Missing and not-yours are deliberately indistinguishable (no
        # existence leak) — the SQL owner check handled both.
        return "No note of yours matches that id -- nothing was deleted."
    log.info("cora_forget_note DELETED owner=%s id=%s chunks=%d", slack_user_id, note_id, deleted)
    return (
        "WRITE_CONFIRMED -- post the following as your entire response "
        "(no preamble, no meta-commentary):\n\n"
        "Note deleted."
    )


def _tool_meeting_action_items(slack_user_id: str, entity: str, _input: dict) -> str:
    """PULL flow: summarize a meeting + the asker's action items, then (on a
    confirmed second call) create the selected ones as Asana tasks assigned to
    the asker. Replaces the retired auto-create push. Thin wrapper -- all logic
    (attendee gate, channel/DM scope, D-052 LEX rails, staged-write create) lives
    in cora.tools.meeting_actions. Lazy import avoids any import-order surprise."""
    from cora.tools import meeting_actions  # noqa: PLC0415
    return meeting_actions.run_meeting_action_items(slack_user_id, entity, _input or {})


# --- Catalog: tool definitions exposed to Claude ---


# --- What's on my plate (Org Synthesis Phase 2, deliverable 1) ---
#
# Role-scoped composite "my plate" view: role + lanes (org-roles registry),
# open Asana tasks, today/tomorrow calendar, and HubSpot deals for users who
# own a pipeline. Pull-not-push: only fires when the user asks. ADVISORY data
# only -- org_roles never expands access (D-044); all deterministic guards
# (user_access, sibling, cross_entity, phi, historical_access) already ran
# before the tool loop, and each section reuses the same per-user mapping +
# entity-scope filters as the standalone tools.

_HARRISON_SLACK_ID = "U0B2RM2JYJ1"

# Cap per plate section: long composites (25 tasks + 23 deals seen live
# 2026-06-11) push the narration into max-token truncation, ending the Slack
# reply in a malformed half-link. asana_get_my_tasks caps separately at
# _MY_TASKS_MAX_ITEMS (F-03); the deal/other sections here remain full.
_PLATE_MAX_ITEMS = 10


def _safe_plate_section(label: str, builder: Callable[..., Any], *args: Any) -> str:
    """Fail-soft wrapper for plate sections: a section may degrade to a stub
    line, but it must NEVER raise or return None into the composite (the
    2026-06-11 live crash was a helper returning None into a str concat)."""
    try:
        out = builder(*args)
    except Exception:
        log.exception("whats_on_my_plate: %s section crashed", label)
        return f"({label} section unavailable right now.)"
    if out is None or not str(out).strip():
        log.warning("whats_on_my_plate: %s section returned empty/None", label)
        return f"({label} section returned no data.)"
    return str(out)


def _plate_asana_section(
    target_id: str, entity: str, drop_stale_days: int | None = None
) -> str:
    """Open Asana tasks for the target, entity-scoped. Fail-soft string.

    drop_stale_days: when set, drop tasks overdue by more than this many days
    (abandoned backlog) before scoping/capping -- the daily brief opts in
    (N7 / Harrison #1) so stale 2025 goal tasks stop surfacing every morning;
    the on-demand plate tool leaves it None (keeps everything).
    """
    # Sub-entities canonicalize to their parent for the task filter --
    # ENTITY_PROJECT_PREFIXES has no LEX-LLC/OSNGF/... keys, so a raw
    # sub-entity fell through _filter_tasks_by_entity UNFILTERED. A
    # sub-entity scope must never be wider than its parent's.
    entity = _SUBENTITY_PARENT.get(entity, entity)
    mapping = _load_slack_asana_map()
    user = mapping.get(target_id)
    asana_gid = str((user or {}).get("asana_user_gid", "") or "")
    if not user or not asana_gid or "REPLACE" in asana_gid:
        return "(No Asana mapping for this user -- task list unavailable.)"
    try:
        all_tasks = asana_client.get_user_tasks(asana_gid)
    except asana_client.AsanaClientError as exc:
        log.warning("whats_on_my_plate asana error user=%s: %s", target_id, exc)
        return "(Temporary issue reaching Asana -- task list unavailable right now.)"
    if drop_stale_days is not None:
        all_tasks = asana_client.drop_stale_tasks(all_tasks, max_overdue_days=drop_stale_days)
    filtered = _filter_tasks_by_entity(all_tasks, entity)
    # Due-dated tasks first so the 10-item cap keeps the most urgent work
    # (2026-06-11 exit-gate nit: a long no-due-date list crowded out dated tasks).
    shown = asana_client.sort_tasks_due_first(filtered)[:_PLATE_MAX_ITEMS]
    text = asana_client.format_tasks_for_llm(
        shown,
        entity_scope=entity if entity != "FNDR" else None,
        total_before_filter=len(all_tasks),
    )
    # Belt-and-braces: never let a formatter regression (e.g. the truncated
    # format_tasks_for_llm shipped 6/03-6/11) propagate None into the plate.
    if not isinstance(text, str) or not text.strip():
        log.warning("whats_on_my_plate: task formatter returned %r for user=%s", type(text).__name__, target_id)
        return f"({len(filtered)} open task(s) found, but the task list could not be rendered.)"
    if len(filtered) > _PLATE_MAX_ITEMS:
        text += (
            f"\n(Plate view shows the first {_PLATE_MAX_ITEMS} of {len(filtered)} open tasks -- "
            f"say 'show me my tasks' for the full task view.)"
        )
    return text


def _plate_calendar_section(target_id: str) -> str:
    """Today + tomorrow calendar for the target. Fail-soft string."""
    mapping = _load_slack_asana_map()
    user_email = ((mapping.get(target_id) or {}).get("asana_email") or "").strip()
    if not user_email:
        return "(No Google identity mapped -- calendar unavailable.)"
    parts: list[str] = []
    for when in ("today", "tomorrow"):
        try:
            events, window_label = calendar_client.get_user_events(user_email, when=when)
        except calendar_client.CalendarClientError as exc:
            log.warning("whats_on_my_plate calendar error user=%s when=%s: %s", target_id, when, exc)
            parts.append(f"(Temporary issue reaching the calendar for {when}.)")
            continue
        parts.append(calendar_client.format_events_for_llm(events, window_label))
    return "\n".join(parts)


def _plate_hubspot_section(target_id: str, entity: str) -> str | None:
    """Open deals for users who own a HubSpot pipeline. None = omit section.

    LEX scope (incl. sub-entities) never gets a HubSpot section -- HubSpot is
    blocked for LEX per the Tier-1 doctrine, matching tools_for_entity.
    """
    if _SUBENTITY_PARENT.get(entity, entity) == "LEX":
        return None
    user = _load_slack_hubspot_map().get(target_id)
    owner_id = str((user or {}).get("hubspot_owner_id", "") or "")
    if not user or not owner_id or "REPLACE" in owner_id:
        return None  # not a deal owner -- silently omit, not an error
    pipeline_id = HUBSPOT_PIPELINE_BY_ENTITY.get(entity)
    try:
        deals = hubspot_client.get_owner_deals(owner_id, pipeline_id=pipeline_id)
    except hubspot_client.HubSpotClientError as exc:
        log.warning("whats_on_my_plate hubspot error user=%s: %s", target_id, exc)
        return "(Temporary issue reaching the deal pipeline.)"
    shown = deals[:_PLATE_MAX_ITEMS]
    text = hubspot_client.format_deals_for_llm(
        shown,
        entity_scope=entity if entity != "FNDR" else None,
        pipeline_filter_applied=pipeline_id is not None,
    )
    if isinstance(text, str) and len(deals) > _PLATE_MAX_ITEMS:
        text += (
            f"\n(Plate view shows the first {_PLATE_MAX_ITEMS} of {len(deals)} deals -- "
            f"say 'show me my deals' for the full list.)"
        )
    return text


def _tool_whats_on_my_plate(slack_user_id: str, entity: str, _input: dict) -> str:
    """Role-scoped composite plate view for the asking user.

    Optional `person` parameter is HARRISON-ONLY: everyone else gets their own
    plate exclusively (asana_get_user_tasks remains the peer-visible path for
    a teammate's raw task list). Unknown users (no org-roles entry) get a
    graceful no-data response (fail-closed, D-044). External consultants get
    role scope only -- no internal task/CRM/calendar pulls.
    """
    person = str((_input or {}).get("person") or "").strip()
    target_id = slack_user_id
    if person:
        if slack_user_id != _HARRISON_SLACK_ID:
            return (
                "Refused: whats_on_my_plate only shows the asking user their OWN plate; "
                "viewing another teammate's plate is Harrison-only. Politely explain this, "
                "and offer asana_get_user_tasks if they just need that teammate's open "
                "Asana tasks (peer-visible by doctrine)."
            )
        resolved, info = resolve_name_to_slack_user_id(person, channel_entity=entity)
        if not resolved:
            return info or (
                f"No teammate found matching '{person}'. Tell Harrison the name didn't "
                f"match anyone in the user map."
            )
        target_id = resolved

    rec = org_roles.get_role(target_id)
    if rec is None:
        if target_id == slack_user_id:
            return (
                "I don't have a role mapping for this user yet -- Harrison can add them to "
                "the org role registry (data/maps/org-roles.yaml). No plate data is shown "
                "without a registry entry. Relay this politely and suggest they ping Harrison."
            )
        return (
            "That person has no entry in the org role registry yet, so there is no plate "
            "view for them. Harrison can add them to data/maps/org-roles.yaml."
        )

    log.info(
        "whats_on_my_plate asker=%s target=%s entity=%s external=%s",
        slack_user_id, target_id, entity, rec.external,
    )

    # Labeled like the other sections + reinforced in the closing instruction:
    # live smoke 2026-06-11 showed models presenting the role line for Harrison
    # but dropping it 2/2 for other askers when it was an unlabeled preamble.
    header = [f"YOUR ROLE\n{rec.name} -- {rec.role} ({rec.entity})"]
    if rec.responsibilities:
        header.append("Lanes: " + "; ".join(rec.responsibilities))

    if rec.external:
        return "\n".join(header) + (
            "\n\nThis user is an EXTERNAL consultant/guest: internal task, CRM, and "
            "calendar systems are not part of their plate view. Their plate is their "
            "engagement scope above. Do not surface internal-only context (financials, "
            "cap tables, internal personnel matters) beyond that scope."
        )

    sections: list[str] = ["\n".join(header)]
    sections.append("OPEN TASKS\n" + _safe_plate_section("Open tasks", _plate_asana_section, target_id, entity))
    sections.append("CALENDAR\n" + _safe_plate_section("Calendar", _plate_calendar_section, target_id))
    try:
        deals = _plate_hubspot_section(target_id, entity)
    except Exception:
        log.exception("whats_on_my_plate: deal pipeline section crashed")
        deals = "(Deal pipeline section unavailable right now.)"
    if deals is not None:
        sections.append("DEAL PIPELINE\n" + str(deals))
    if target_id == _HARRISON_SLACK_ID:
        sections.append(
            "STALLED DECISIONS\n"
            + _safe_plate_section("Stalled decisions", _tool_fndr_open_decisions, target_id, entity, {})
        )

    sections.append(
        "(REPLY FORMAT -- follow exactly: START your reply by stating the user's role and "
        "lanes from the YOUR ROLE section. EVERY asker gets their role line, not only "
        "Harrison. Then present each remaining section in order, preserving any <url|name> "
        "links verbatim. This is the user's own role-scoped picture; entity scoping for "
        "this channel has already been applied. Do not add financial figures from other "
        "sources.)"
    )
    return "\n\n".join(sections)


def _tool_cora_self_check(slack_user_id: str, entity: str, _input: dict) -> str:
    """Report Cora's REAL operational state (read-only) from LIVE signals.

    Heartbeat freshness, KB chunk count, and source sync watermarks — NEVER the
    knowledge base (which must not narrate Cora's own build/audit docs as a
    "diagnostic"). Detailed by-source counts + watermarks are founder-level
    (FNDR/HJRG) only; other channels get heartbeat + total chunks.
    """
    import time as _time

    from .. import health_endpoint

    detail = entity in ("FNDR", "HJRG")
    lines: list[str] = ["Cora self-check (live operational state):"]

    # 1. Heartbeat — the real liveness signal.
    try:
        age = health_endpoint.heartbeat_age_seconds()
        if age is None:
            lines.append("- Heartbeat: MISSING — starting up or wedged.")
        elif age <= health_endpoint.FRESH_SECS:
            lines.append(f"- Heartbeat: fresh ({int(age)}s ago).")
        else:
            lines.append(
                f"- Heartbeat: STALE ({int(age)}s ago; fresh threshold {health_endpoint.FRESH_SECS}s)."
            )
    except Exception:  # noqa: BLE001
        lines.append("- Heartbeat: unavailable.")

    # 2. KB size + sync watermarks (real DB reads via the shared instance).
    try:
        kb, kb_lock = _notes_kb()
        if kb is None:
            lines.append("- Knowledge base: unavailable.")
        else:
            with kb_lock:
                st = kb.stats()
                watermarks = {
                    s: kb.get_sync_state(s)
                    for s in ("static_md", "slack", "asana", "fireflies", "drive", "gmail")
                }
            total = int(st.get("total_chunks", 0) or 0)
            lines.append(f"- Knowledge base: {total:,} chunks.")
            if detail:
                by_src = st.get("by_source", {}) or {}
                top = ", ".join(f"{k} {int(v):,}" for k, v in list(by_src.items())[:6])
                if top:
                    lines.append(f"  - by source: {top}")
                now = _time.time()
                fresh_bits = []
                for src, ws in watermarks.items():
                    if ws and ws[0]:
                        hours = (now - float(ws[0])) / 3600.0
                        fresh_bits.append(f"{src} {hours:.0f}h ago")
                if fresh_bits:
                    lines.append("  - last sync: " + ", ".join(fresh_bits))
    except Exception as exc:  # noqa: BLE001
        log.warning("cora_self_check KB read failed: %s", exc)
        lines.append("- Knowledge base: read error.")

    if not detail:
        lines.append("(Detailed counts + sync state are founder-level — ask in a founder channel.)")

    lines.append("")
    lines.append(
        "NOTE for Cora: relay ONLY these live signals as your status. Do NOT add "
        "anything from the knowledge base about your own status, build, audits, or "
        "'diagnostics' — those documents are not operational truth."
    )
    log.info("cora_self_check actor=%s entity=%s detail=%s", slack_user_id, entity, detail)
    return "\n".join(lines)


def _tool_cora_person_dossier(slack_user_id: str, entity: str, _input: dict) -> str:
    """On-demand per-person involvement dossier (North Star pillar 4).

    FOUNDER-OR-SELF gate (deterministic, pre-pull): Harrison may check in on any
    teammate; everyone else may profile ONLY themselves -- a peer-surveillance
    request is refused with NO target leak (the name is never resolved). Then a
    multi-source composite (email / meetings / tasks / deals / calendar) is pulled
    fail-soft, PHI-walled for LEX staff, synthesized (Sonnet), and written back to
    the person's `_brain/people/{slug}.md` dossier (decision 10.2 = ON). The dossier
    is peer-walled -- readable by Harrison + that person only, never posted to a
    channel and never about a peer. Lazy import avoids any import-order surprise."""
    from cora.tools import person_dossier  # noqa: PLC0415

    inp = _input or {}
    person_arg = str(inp.get("person") or "").strip()
    try:
        days = int(inp.get("days") or 14)
    except (TypeError, ValueError):
        days = 14
    days = max(1, min(days, 30))

    founder = _load_supervisor_hierarchy().get("founder_slack_id") or _HARRISON_SLACK_ID
    # Access gate FIRST so a peer-surveillance request is refused with no target leak
    # BEFORE the surface check (a peer must never learn the surface rule reveals a target).
    target, refusal = person_dossier.resolve_access(slack_user_id, person_arg, founder)
    if refusal:
        log.info("cora_person_dossier refused asker=%s had_person=%s", slack_user_id, bool(person_arg))
        return refusal
    if target is None:
        return "I couldn't resolve that person -- ask Harrison to check the people map."

    # PEER-WALL (deterministic, D-034): a dossier is readable by Harrison + that person
    # only and must NEVER be rendered into a shared channel. The QA loop threads
    # _channel_name ("dm" for IMs); _channel_id (starts "D") is the belt. In any non-DM
    # surface, refuse-and-redirect to a DM -- do NOT pull/build (no involvement content
    # is produced where a peer could see it). Applies to BOTH self and founder check-ins.
    is_dm = (
        str(inp.get("_channel_name", "") or "").strip().lower() == "dm"
        or str(inp.get("_channel_id", "") or "").startswith("D")
    )
    if not is_dm:
        log.info("cora_person_dossier redirected-to-DM asker=%s target=%s", slack_user_id, target.slug)
        return (
            "Involvement summaries are private -- readable by Harrison and that person "
            "only, never posted in a shared channel. DM me directly and I'll pull it."
        )

    log.info("cora_person_dossier asker=%s target=%s days=%d", slack_user_id, target.slug, days)
    try:
        result = person_dossier.build_dossier(target, days=days)
    except Exception as exc:  # noqa: BLE001 -- never crash the dispatch
        log.exception("cora_person_dossier build crashed for %s", target.slug)
        return f"I hit a snag pulling that involvement summary ({exc}). Tell the user to try again shortly."
    return result.reply


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard read layer (2026-07-11)
# ─────────────────────────────────────────────────────────────────────────────
# Read-only tools that surface the live state of Cowork dashboards from their
# backing stores. EVERY tool calls dashboard_access.check_dashboard_access FIRST
# (deterministic, pre-answer, fail-closed, no existence leak) and is SOURCE-OPAQUE
# by construction: replies say "your insurance portfolio" / "the capital program
# tracker" / "the creator CRM" / "the content pipeline" -- never Airtable / Drive /
# Notion / OneAmerica / the carrier / a portal URL. The egress boundary does NOT
# redact named systems, so opacity is enforced here, not downstream. All four are
# VERBATIM_TABLE_TOOLS (never cached; the two personal ones are the D-043 class).

_DASH_ONEAMERICA = "oneamerica-whole-life-portfolio"
_DASH_CAPITAL = "f3-capital-program"
_DASH_CREATOR = "f3-creator-sponsorship-command-center"
_DASH_CONTENT = "f3-content-pipeline"


# Source-opacity scrub for pass-through free-text field values. These tools are
# VERBATIM_TABLE_TOOLS, so format_reply is bypassed and only the egress boundary
# runs downstream -- and egress does NOT strip named systems or non-allowlisted
# vendor URLs (airtable.com / instagram.com / a portal link pasted into a "Next
# step" note). So a URL or platform token pasted into an Airtable/OneAmerica field
# would leak verbatim. Neutralize both here before interpolation (D-051 2026-07-11).
_DASH_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_DASH_VENDOR_RE = re.compile(
    r"\b(airtable|quickbooks|shopify|oneamerica|one\s?america|notion)\b", re.IGNORECASE
)


def _dash_scrub(text: str) -> str:
    """Strip URLs (all hosts) + explicit platform tokens from tool output so a
    value pasted into a backing-store field can't leak the source/mechanics."""
    if not isinstance(text, str) or not text:
        return text
    text = _DASH_URL_RE.sub("[link]", text)
    text = _DASH_VENDOR_RE.sub("[source]", text)
    return text


def _dash_f(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _dash_money(v: Any) -> str:
    return f"${_dash_f(v):,.0f}"


def _dash_millions(v: Any) -> str:
    return f"${_dash_f(v) / 1e6:,.2f}M"


def _dash_parse_date(s: Any) -> date | None:
    if not s or not isinstance(s, str):
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _dash_count_single(records: list[dict], field_name: str) -> Counter:
    """Count a single-select / formula string field across records."""
    c: Counter = Counter()
    for r in records:
        v = r.get(field_name)
        if isinstance(v, str) and v.strip():
            c[v.strip()] += 1
        elif isinstance(v, dict) and v.get("name"):  # defensive: nested {name} shape
            c[str(v["name"])] += 1
    return c


def _dash_count_multi(records: list[dict], field_name: str) -> Counter:
    """Count a multi-select field (list of names) across records."""
    c: Counter = Counter()
    for r in records:
        v = r.get(field_name)
        if isinstance(v, list):
            for item in v:
                if isinstance(item, str) and item.strip():
                    c[item.strip()] += 1
                elif isinstance(item, dict) and item.get("name"):
                    c[str(item["name"])] += 1
        elif isinstance(v, str) and v.strip():
            c[v.strip()] += 1
    return c


def _dash_fmt_counts(counter: Counter) -> str:
    return ", ".join(f"{k} {v}" for k, v in counter.most_common())


# --- OneAmerica whole-life portfolio (Harrison DM only) ---

def _format_oneamerica(data: dict, *, detail: bool = False, today: date | None = None) -> str:
    today = today or date.today()
    _m = data.get("meta")
    meta = _m if isinstance(_m, dict) else {}
    policies = [p for p in (data.get("policies") or []) if isinstance(p, dict)]
    as_of = meta.get("values_as_of") or "recently"
    n = len(policies)

    total_db = sum(_dash_f(p.get("total_db")) for p in policies)
    total_cv = sum(_dash_f(p.get("guar_cv")) + _dash_f(p.get("pua_cv")) for p in policies)
    total_loan = sum(_dash_f(p.get("loan_balance")) for p in policies)
    total_avail = sum(_dash_f(p.get("avail_loan")) for p in policies)
    annual_prem = sum(_dash_f(p.get("premium")) for p in policies)
    loans_count = sum(1 for p in policies if _dash_f(p.get("loan_balance")) > 0)

    high_borrow = 0
    for p in policies:
        lb = _dash_f(p.get("loan_balance"))
        av = p.get("avail_loan")
        if av is None:
            pct = 100.0 if lb > 0 else 0.0
        else:
            denom = lb + _dash_f(av)
            pct = (lb / denom * 100.0) if denom > 0 else 0.0
        if pct > 85.0:
            high_borrow += 1

    overdue, upcoming, flagged = [], [], []
    for p in policies:
        insured = p.get("insured", "a policy")
        ptd = _dash_parse_date(p.get("paid_to_date"))
        if p.get("flags"):
            flagged.append((insured, str(p.get("flags"))))
        if ptd and ptd < today:
            overdue.append((insured, p.get("paid_to_date")))
        elif ptd and today <= ptd <= today + timedelta(days=30):
            upcoming.append((insured, p.get("paid_to_date"), _dash_f(p.get("premium"))))

    lines = [f"Your whole-life portfolio (values as of {as_of}) -- {n} policies:"]
    lines.append(f"- Total death benefit: {_dash_millions(total_db)}")
    lines.append(f"- Total cash value: {_dash_millions(total_cv)}")
    loan_line = f"- Policy loans outstanding: {_dash_millions(total_loan)} across {loans_count} policies"
    if high_borrow:
        loan_line += f" ({high_borrow} at >85% borrowed)"
    lines.append(loan_line)
    lines.append(f"- Available to borrow: {_dash_money(total_avail)}")
    lines.append(f"- Total annual premium: {_dash_money(annual_prem)}")

    if overdue:
        lines.append("")
        lines.append("Premium paid-to date in the PAST (verify status):")
        for insured, d in sorted(overdue, key=lambda x: str(x[1])):
            lines.append(f"  - {insured}: paid to {d}")
    if upcoming:
        lines.append("")
        lines.append("Premiums due in the next 30 days:")
        for insured, d, prem in sorted(upcoming, key=lambda x: str(x[1])):
            lines.append(f"  - {insured}: {d} ({_dash_money(prem)})")
    if flagged:
        lines.append("")
        lines.append("Flags:")
        for insured, fl in flagged:
            lines.append(f"  - {insured}: {fl}")
    if detail:
        lines.append("")
        lines.append("Per policy (insured / death benefit / cash value / loan / paid-to):")
        for p in policies:
            cv = _dash_f(p.get("guar_cv")) + _dash_f(p.get("pua_cv"))
            lines.append(
                f"  - {p.get('insured', '?')} ({p.get('product', '')}): "
                f"{_dash_money(p.get('total_db'))} DB, {_dash_money(cv)} CV, "
                f"{_dash_money(p.get('loan_balance'))} loan, paid to {p.get('paid_to_date', '?')}"
            )
    return _dash_scrub("\n".join(lines))


def _tool_personal_oneamerica_portfolio(slack_user_id: str, entity: str, _input: dict) -> str:
    inp = _input or {}
    refusal = dashboard_access.check_dashboard_access(
        _DASH_ONEAMERICA, slack_user_id, inp.get("_channel_name", "")
    )
    if refusal:
        return refusal
    store = dashboard_access.store_for(_DASH_ONEAMERICA)
    file_id = (store.get("files") or {}).get("policies_current", "")
    data = dashboard_drive_reader.read_json_by_id(file_id)
    if not isinstance(data, dict) or not isinstance(data.get("policies"), list):
        return "I couldn't pull your insurance portfolio just now -- try again in a moment."
    log.info("personal_oneamerica_portfolio user=%s", slack_user_id)
    return _format_oneamerica(data, detail=bool(inp.get("detail")))


# --- F3 capital program tracker (Harrison DM only, HIGHLY CONFIDENTIAL) ---

def _dash_render_state(v: Any) -> str:
    """Compact render of an unknown-shape edit-state value (dict/list/scalar)."""
    if isinstance(v, dict):
        return "; ".join(f"{k}: {_dash_render_state(val)}" for k, val in list(v.items())[:12])
    if isinstance(v, list):
        if all(not isinstance(x, (dict, list)) for x in v):
            return ", ".join(str(x) for x in v[:12])
        return f"{len(v)} items"
    return str(v)


def _format_capital_program(data: dict) -> str:
    _m = data.get("meta")
    meta = _m if isinstance(_m, dict) else {}
    _l = data.get("locked")
    locked = _l if isinstance(_l, dict) else {}
    synced = meta.get("synced_at") or "unknown"

    lines = ["*Capital program -- locked terms:*"]
    if locked.get("raise_usd"):
        lines.append(
            f"- Raise: {_dash_money(locked.get('raise_usd'))} at "
            f"{_dash_money(locked.get('post_money_valuation_usd'))} post-money"
        )
    if locked.get("price_per_share_usd"):
        lines.append(f"- Price per share: ${_dash_f(locked.get('price_per_share_usd')):.4f}")
    if locked.get("founder_conversion_usd"):
        lines.append(f"- Founder conversion: {_dash_money(locked.get('founder_conversion_usd'))}")
    if locked.get("ambassador_pool_usd"):
        lines.append(
            f"- Ambassador pool: {_dash_money(locked.get('ambassador_pool_usd'))} "
            f"({_dash_f(locked.get('ambassador_pool_pct')):g}%)"
        )
    if locked.get("operator_seat_usd"):
        lines.append(
            f"- Operator seat: {_dash_money(locked.get('operator_seat_usd'))} "
            f"({_dash_f(locked.get('operator_seat_pct')):g}%)"
        )
    if locked.get("recap"):
        lines.append(f"- Recap: {locked.get('recap')}")
    _carta = locked.get("carta")
    carta = _carta if isinstance(_carta, dict) else {}
    if carta:
        lines.append(
            f"- Cap table: {int(_dash_f(carta.get('fully_diluted'))):,} fully diluted; "
            f"Harrison {int(_dash_f(carta.get('harrison_shares'))):,} "
            f"({_dash_f(carta.get('harrison_pct')):g}%)"
        )

    edit_keys = ("calc", "roster", "phases", "legal", "open_items", "tracker")
    has_edit = any(data.get(k) for k in edit_keys) or data.get("candidates")
    if not has_edit:
        lines.append("")
        lines.append(
            f"The tracker hasn't been synced from the dashboard yet (last bridge write: "
            f"{synced}), so I only have the locked terms above -- open the tracker and press "
            f"Sync to Cora for the live candidate pipeline and status."
        )
        if data.get("note"):
            lines.append(f"Note: {data.get('note')}")
        return _dash_scrub("\n".join(lines))

    lines.append("")
    lines.append(f"*Live state (synced {synced}):*")
    candidates = data.get("candidates") or []
    if candidates:
        confirmed = [
            c for c in candidates
            if isinstance(c, dict) and str(c.get("status", "")).lower() in ("confirmed", "committed", "closed")
        ]
        cand_line = f"- Candidates: {len(candidates)} in pipeline"
        if confirmed:
            names = ", ".join(str(c.get("name", "?")) for c in confirmed[:12])
            cand_line += f"; {len(confirmed)} confirmed ({names})"
        lines.append(cand_line)
    for k in ("calc", "phases", "legal", "tracker", "open_items"):
        v = data.get(k)
        if v:
            lines.append(f"- {k.replace('_', ' ').title()}: {_dash_render_state(v)}")
    if data.get("note"):
        lines.append(f"- Note: {data.get('note')}")
    return _dash_scrub("\n".join(lines))


def _tool_personal_capital_program_state(slack_user_id: str, entity: str, _input: dict) -> str:
    inp = _input or {}
    refusal = dashboard_access.check_dashboard_access(
        _DASH_CAPITAL, slack_user_id, inp.get("_channel_name", "")
    )
    if refusal:
        return refusal
    store = dashboard_access.store_for(_DASH_CAPITAL)
    data = dashboard_drive_reader.newest_json_by_title(
        store.get("folder", ""), store.get("title", "")
    )
    if not isinstance(data, dict) or not data:
        return "I couldn't pull the capital program tracker just now -- try again in a moment."
    log.info("personal_capital_program_state user=%s", slack_user_id)
    return _format_capital_program(data)


# --- F3 creator & ambassador CRM (F3E creator/leadership + founder + DM) ---

def _format_creator_crm(roster: list[dict], activity: list[dict], *, today: date | None = None) -> str:
    today = today or date.today()
    lines = [f"*Creator CRM* -- {len(roster)} people in the roster."]
    stage = _dash_count_single(roster, "Stage")
    tier = _dash_count_single(roster, "Tier")
    prog = _dash_count_multi(roster, "Program")
    if stage:
        lines.append("By stage: " + _dash_fmt_counts(stage))
    if tier:
        lines.append("By tier: " + _dash_fmt_counts(tier))
    if prog:
        lines.append("By program: " + _dash_fmt_counts(prog))

    due = []
    for a in activity:
        fu = _dash_parse_date(a.get("Follow-up date"))
        if fu and fu <= today:
            due.append((a.get("Entry", "(activity)"), a.get("Follow-up date")))
    if due:
        lines.append("")
        lines.append(f"Follow-ups due ({len(due)}):")
        for entry, d in sorted(due, key=lambda x: str(x[1]))[:10]:
            lines.append(f"  - {entry} (due {d})")

    top = [r for r in sorted(roster, key=lambda r: _dash_f(r.get("GMV")), reverse=True) if _dash_f(r.get("GMV")) > 0][:5]
    if top:
        lines.append("")
        lines.append("Top creators by sales driven:")
        for r in top:
            lines.append(f"  - {r.get('Name', '?')}: {_dash_money(r.get('GMV'))}")

    recent = sorted([a for a in activity if a.get("Date")], key=lambda a: str(a.get("Date")), reverse=True)[:5]
    if recent:
        lines.append("")
        lines.append("Recent activity:")
        for a in recent:
            typ = f" [{a.get('Type')}]" if a.get("Type") else ""
            lines.append(f"  - {a.get('Date')}: {a.get('Entry', '(entry)')}{typ}")
    return _dash_scrub("\n".join(lines))


def _creator_person_lookup(roster: list[dict], person: str) -> str:
    pl = person.strip().lower()
    matches = [r for r in roster if pl in str(r.get("Name", "")).lower()]
    if not matches:
        return f'I don\'t have a creator named "{person}" in the roster.'
    if len(matches) > 6:
        return f'"{person}" matches {len(matches)} creators -- can you be more specific?'
    lines = []
    for r in matches:
        header = f"*{r.get('Name', '?')}*"
        if r.get("Handle"):
            header += f" ({r.get('Handle')})"
        lines.append(header)
        detail = []
        for label, key in (("Program", "Program"), ("Stage", "Stage"), ("Tier", "Tier"),
                            ("GMV", "GMV"), ("Last touch", "Last touch"), ("Next step", "Next step")):
            v = r.get(key)
            if not v:
                continue
            if key == "GMV":
                v = _dash_money(v)
            elif isinstance(v, list):
                v = ", ".join(str(x) for x in v)
            detail.append(f"{label}: {v}")
        if detail:
            lines.append("  " + " | ".join(detail))
    return _dash_scrub("\n".join(lines))


def _tool_f3e_creator_crm(slack_user_id: str, entity: str, _input: dict) -> str:
    inp = _input or {}
    refusal = dashboard_access.check_dashboard_access(
        _DASH_CREATOR, slack_user_id, inp.get("_channel_name", "")
    )
    if refusal:
        return refusal
    store = dashboard_access.store_for(_DASH_CREATOR)
    base = store.get("base", "")
    tables = store.get("tables") or {}
    roster = airtable_client.list_records(
        base, tables.get("roster", ""),
        fields=["Name", "Program", "Stage", "Tier", "GMV", "Handle", "Last touch", "Next step", "Owner", "Brand"],
    )
    if not roster.available:
        return "The creator CRM isn't connected yet, so I can't pull that right now."
    activity = airtable_client.list_records(
        base, tables.get("activity", ""),
        fields=["Entry", "Date", "Type", "Follow-up date"],
    )
    activity_records = activity.records if activity.available else []
    person = (inp.get("person") or "").strip()
    log.info("f3e_creator_crm user=%s person=%r", slack_user_id, person)
    if person:
        return _creator_person_lookup(roster.records, person)
    return _format_creator_crm(roster.records, activity_records)


# --- Founder content & freelancer pipeline (founder channels + DM) ---

def _format_content_pipeline(
    deliverables: list[dict], calendar: list[dict], campaigns: list[dict],
    budget: list[dict], events: list[dict], *, today: date | None = None,
) -> str:
    today = today or date.today()
    lines = ["*Content & freelancer pipeline:*"]

    flags = _dash_count_single(deliverables, "Action flag")
    if flags:
        order = ["Overdue", "Due this week", "Unassigned", "In production", "On track", "Done"]
        ordered = sorted(
            flags.items(),
            key=lambda kv: (order.index(kv[0]) if kv[0] in order else 99, -kv[1]),
        )
        lines.append("Deliverables: " + ", ".join(f"{k} {v}" for k, v in ordered))
        prio = sorted(
            [d for d in deliverables if str(d.get("Action flag", "")) in ("Overdue", "Due this week", "Unassigned")],
            key=lambda d: order.index(str(d.get("Action flag", ""))) if str(d.get("Action flag", "")) in order else 99,
        )
        for d in prio[:10]:
            due = d.get("Due date")
            lines.append(
                f"  - [{d.get('Action flag')}] {d.get('Deliverable', '?')}"
                + (f" (due {due})" if due else "")
            )

    week = [
        c for c in calendar
        if (dd := _dash_parse_date(c.get("Slot date"))) and today <= dd <= today + timedelta(days=7)
    ]
    if week:
        lines.append("")
        lines.append(f"Calendar slots this week ({len(week)}):")
        for c in sorted(week, key=lambda c: str(c.get("Slot date"))):
            lines.append(f"  - {c.get('Slot date')}: {c.get('Slot', '?')}")

    camp = _dash_count_single(campaigns, "Status")
    if camp:
        lines.append("")
        lines.append("Campaigns: " + _dash_fmt_counts(camp))

    buckets: dict[str, list[float]] = {}
    for b in budget:
        bk = b.get("Bucket") or "Other"
        pair = buckets.setdefault(str(bk), [0.0, 0.0])
        pair[0] += _dash_f(b.get("Planned $"))
        pair[1] += _dash_f(b.get("Actual $"))
    if buckets:
        lines.append("")
        lines.append("Budget (actual of planned):")
        for bk, (planned, actual) in buckets.items():
            lines.append(f"  - {bk}: {_dash_money(actual)} of {_dash_money(planned)}")

    ev = _dash_count_single(events, "Status")
    if ev:
        lines.append("")
        lines.append("Events pipeline: " + _dash_fmt_counts(ev))

    if len(lines) == 1:
        lines.append("Nothing in the pipeline right now.")
    return _dash_scrub("\n".join(lines))


def _tool_fndr_content_pipeline(slack_user_id: str, entity: str, _input: dict) -> str:
    inp = _input or {}
    refusal = dashboard_access.check_dashboard_access(
        _DASH_CONTENT, slack_user_id, inp.get("_channel_name", "")
    )
    if refusal:
        return refusal
    store = dashboard_access.store_for(_DASH_CONTENT)
    base = store.get("base", "")
    tables = store.get("tables") or {}
    deliverables = airtable_client.list_records(
        base, tables.get("deliverables", ""),
        fields=["Deliverable", "Action flag", "Due date", "Status", "Assigned freelancer"],
    )
    if not deliverables.available:
        return "The content pipeline isn't connected yet, so I can't pull that right now."

    def _rows(table_key: str, fields: list[str]) -> list[dict]:
        res = airtable_client.list_records(base, tables.get(table_key, ""), fields=fields)
        return res.records if res.available else []

    calendar = _rows("calendar", ["Slot", "Slot date", "Status", "Platform"])
    campaigns = _rows("campaigns", ["Campaign", "Status", "Launch month"])
    budget = _rows("budget", ["Line", "Bucket", "Planned $", "Actual $", "Period"])
    events = _rows("events", ["Event", "Status", "Date", "Fit score"])
    log.info("fndr_content_pipeline user=%s", slack_user_id)
    return _format_content_pipeline(deliverables.records, calendar, campaigns, budget, events)


TOOL_DEFINITIONS = [
    {
        "name": "asana_get_my_tasks",
        "description": (
            "Fetch the incomplete Asana tasks assigned to the user who @-mentioned Cora. "
            "Use this when the user asks specifically about their Asana tasks — phrases "
            "like 'show me my tasks', 'what's due this week', 'my open Asana items'. For "
            "the broader whole-plate picture ('what's on my plate', 'what do I have going "
            "on', overall workload/day), use whats_on_my_plate instead — it includes tasks "
            "plus role, calendar, and pipeline. Returns up to 25 tasks with name, "
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
        "name": "asana_get_user_tasks",
        "description": (
            "Fetch the incomplete Asana tasks assigned to ANOTHER teammate (not the "
            "asking user). Use this when the user asks about a specific named teammate's "
            "workload — phrases like 'what is Sean's latest tasks', 'show me Tommy's open work', "
            "'what's Hannah working on', 'how busy is Larry'. Accepts common first-name aliases "
            "and misspellings (Sean → Shaun Hawkins, Tommy → Tommy Anderson, etc.) via "
            "data/maps/user-aliases.yaml. Returns up to 25 incomplete tasks formatted "
            "identically to asana_get_my_tasks. Channel entity scope still applies — in "
            "#osn-leadership, only OSN-tagged tasks will be returned; FNDR channels see "
            "all entities. For the asking user's own tasks, use asana_get_my_tasks instead. "
            "Peer-visible by design (per HJR 2026-05-21 doctrine): any mapped teammate can "
            "check any other mapped teammate's task status — coordination benefit outweighs "
            "the privacy cost at HJR's current team size."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "user_name": {
                    "type": "string",
                    "description": "Name of the teammate to look up. Accepts first name, full name, or common nickname (e.g. 'Sean', 'Shaun', 'Shaun Hawkins', 'Tommy', 'Tommy Anderson').",
                },
            },
            "required": ["user_name"],
        },
    },
    {
        "name": "asana_create_task",
        "description": (
            "Create a new Asana task in the HJR Global workspace. This is Cora's FIRST write "
            "tool — use with care.\n"
            "\n"
            "REQUIRED PATTERN (staged-write — never skip):\n"
            "1. When the user asks to create a task, DRAFT it as a preview block in your "
            "   reply. Show: title, assignee (default: the asker), due date if mentioned, "
            "   notes if mentioned, project if mentioned. DO NOT call this tool on the first "
            "   turn — just show the preview and ask the user to confirm.\n"
            "2. Wait for the user's next message. If they say 'yes', 'approve', 'create it', "
            "   'go ahead', or similar explicit affirmation, call this tool with confirmed=true.\n"
            "3. If the user wants changes, re-show the preview with the changes and ask again. "
            "   Do not call this tool until they explicitly approve.\n"
            "4. If they reject ('no', 'cancel', 'don't'), don't call this tool at all.\n"
            "\n"
            "Use this tool when the user asks Cora to create, add, or queue a task — phrases like "
            "'create a task to...', 'add a task for Sean to...', 'remind me to...', 'set up a task '"
            "to do X', 'queue a task for Hannah'. The default assignee is the @-mentioning user. "
            "Cross-assignment is allowed (any teammate in slack-to-asana.yaml; aliases supported). "
            "The tool returns a clickable Slack link to the created task — preserve the <url|name> "
            "syntax verbatim in your reply.\n"
            "\n"
            "If you call without confirmed=true, the tool will refuse and remind you to confirm "
            "first. That's the safety net."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Task title / name. Required. Should be action-oriented (start with a verb).",
                },
                "assignee_name": {
                    "type": "string",
                    "description": "Optional. Name of the teammate to assign the task to (first name, full name, or common alias — 'Sean' / 'Shaun' / 'Shaun Hawkins' all resolve). If omitted, the task is assigned to the @-mentioning user.",
                },
                "follower_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional. Extra teammates to add as FOLLOWERS (Asana has one assignee but many followers) so a task meant for two people surfaces for both. Names/aliases, same resolution as assignee_name.",
                },
                "notes": {
                    "type": "string",
                    "description": "Optional. Task description / context. Becomes the Asana task notes field.",
                },
                "due_on": {
                    "type": "string",
                    "description": "Optional. Due date in YYYY-MM-DD format.",
                },
                "project_gid": {
                    "type": "string",
                    "description": "Optional. Asana project GID. Usually OMIT it — the tool auto-routes to the right project for this channel's entity (and never into another entity's project). A project belonging to a different entity is dropped and re-routed (surfaced in the preview).",
                },
                "force_duplicate": {
                    "type": "boolean",
                    "description": "Optional. The tool refuses to create a task whose name already matches an OPEN task in the target project, and tells you so. Only set true if the user confirms it is genuinely a separate task after seeing that warning.",
                },
                "confirmed": {
                    "type": "boolean",
                    "description": "Required. Set to true ONLY after you have shown the user the tool's preview (it includes the resolved project + any Lexington PHI-scrub note) and received explicit approval. If false or omitted, the tool refuses and returns the preview for you to show.",
                },
            },
            "required": ["title", "confirmed"],
        },
    },
    {
        "name": "asana_complete_task",
        "description": (
            "Mark one of the ASKING USER's open Asana tasks COMPLETE. Use when they say "
            "'mark X done', 'complete the X task', 'close out X', 'I finished X'. "
            "REQUIRED PATTERN (staged-write): on the first turn, call with task_name (or "
            "task_gid if you have it) WITHOUT confirmed -- the tool resolves the task and "
            "returns a refusal telling you which task it matched; show the user that "
            "preview ('Mark complete: <task>') and get explicit approval, THEN call again "
            "with confirmed=true. The tool only matches the asker's OWN open tasks; if it "
            "reports zero or multiple matches, relay that and ask them to clarify."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_name": {"type": "string", "description": "Name (or close fragment) of the asker's open task to complete."},
                "task_gid": {"type": "string", "description": "Optional Asana task gid (skips name matching, but the task is still verified to be one of YOUR open tasks unless you are the founder/an aggregator channel)."},
                "confirmed": {"type": "boolean", "description": "Set true ONLY after showing the matched task and getting explicit approval."},
            },
            "required": [],
        },
    },
    {
        "name": "asana_delete_task",
        "description": (
            "PERMANENTLY DELETE one of the asking user's Asana tasks. Deletion is "
            "IRREVERSIBLE -- prefer asana_complete_task unless the user explicitly wants it "
            "gone. Same staged-write pattern: first call with task_name (no confirmed) to "
            "resolve + preview ('Permanently delete: <task>'), get explicit approval, THEN "
            "call with confirmed=true. Only matches the asker's OWN open tasks."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_name": {"type": "string", "description": "Name (or close fragment) of the asker's open task to delete."},
                "task_gid": {"type": "string", "description": "Optional Asana task gid (skips name matching, but the task is still verified to be one of YOUR open tasks unless you are the founder/an aggregator channel)."},
                "confirmed": {"type": "boolean", "description": "Set true ONLY after showing the matched task and getting explicit approval to PERMANENTLY delete it."},
            },
            "required": [],
        },
    },
    {
        "name": "gmail_create_draft",
        "description": (
            "Create a Gmail draft in the asking user's own Gmail Drafts folder. Cora "
            "does NOT send — the user opens Gmail and sends manually. This is a "
            "human-in-the-loop write tool.\n"
            "\n"
            "REQUIRED PATTERN (staged-write — never skip):\n"
            "1. When the user asks to draft an email, FORMAT a preview block in your "
            "   reply showing: to, cc/bcc if any, subject, body. DO NOT call this tool "
            "   on the first turn — just show the preview and ask the user to confirm.\n"
            "2. Wait for the user's next message. If they say 'yes', 'draft it', 'create "
            "   it', 'looks good', 'go ahead', or similar explicit affirmation, call this "
            "   tool with confirmed=true.\n"
            "3. If they want edits, re-show the preview with changes and ask again. "
            "   Do not call this tool until they explicitly approve.\n"
            "4. If they reject ('no', 'cancel'), don't call this tool at all.\n"
            "\n"
            "Use this tool when the user asks Cora to draft, write, compose, or queue "
            "an email — phrases like 'draft an email to X', 'write a reply to Y', "
            "'compose a note to the team', 'queue an email about Z'. The draft lands "
            "in the asker's own Gmail Drafts (impersonated via service account + DWD); "
            "they open Gmail and click Send when ready. Cora returns a clickable link "
            "to the Drafts folder — preserve the <url|name> syntax verbatim.\n"
            "\n"
            "Recipients: 'to' is required (string or list of email addresses). 'cc' and "
            "'bcc' are optional. Subject + body are required. Plain text body only "
            "(no HTML in v1). If the user mentions a name instead of an email and you "
            "don't know the email, ask them for the address — don't guess.\n"
            "\n"
            "If you call without confirmed=true, the tool refuses and reminds you to "
            "confirm first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {
                    "type": ["string", "array"],
                    "description": "Recipient email address(es). String (comma-separated) or array.",
                    "items": {"type": "string"},
                },
                "subject": {
                    "type": "string",
                    "description": "Subject line. Required, non-empty.",
                },
                "body": {
                    "type": "string",
                    "description": "Plain-text email body. Required, non-empty. Sign with the asker's name unless they say otherwise.",
                },
                "cc": {
                    "type": ["string", "array"],
                    "description": "Optional Cc recipient(s).",
                    "items": {"type": "string"},
                },
                "bcc": {
                    "type": ["string", "array"],
                    "description": "Optional Bcc recipient(s).",
                    "items": {"type": "string"},
                },
                "confirmed": {
                    "type": "boolean",
                    "description": "Required. Set to true ONLY after you have shown the user a preview and received explicit approval. If false or omitted, the tool refuses.",
                },
            },
            "required": ["to", "subject", "body", "confirmed"],
        },
    },
    {
        "name": "gmail_inbox",
        "description": (
            "Fetch the asking user's recent Gmail inbox messages — unread and starred by default. "
            "Use this when the user asks about their email, inbox, unread messages, or recent mail "
            "— phrases like 'check my email', 'what's in my inbox', 'any unread emails', "
            "'what emails do I have', 'show me my recent emails', 'any important emails'. "
            "Returns up to 10 messages with sender name, subject, date, and a short snippet. "
            "Uses the user's asana_email as their Google identity (same as Calendar). "
            "Read-only — no confirmation needed. Do not call for another person's inbox, "
            "only the asking user's. The optional `query` parameter accepts any Gmail search "
            "string (e.g. 'from:harrison', 'subject:invoice', 'is:unread label:important'). "
            "Default query: 'is:unread OR is:starred'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Gmail search string to filter messages. "
                        "Default: 'is:unread OR is:starred'. "
                        "Examples: 'is:unread', 'from:harrison@hjrglobal.com', "
                        "'subject:invoice after:2026/05/01', 'is:unread label:important'."
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max messages to return (1-20). Defaults to 10.",
                },
            },
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
        "name": "calendar_create_event",
        "description": (
            "Create a new event in the user's Google Calendar primary calendar. "
            "Use this when the user asks to schedule, book, add, or create a meeting or event — "
            "phrases like 'schedule a meeting with X', 'add a call to my calendar', "
            "'book time for Y', 'create a calendar event for Z'. "
            "STAGED WRITE: the FIRST call returns a NOT-CREATED preview and does not book "
            "anything; show that preview to the user and get their explicit yes; then call "
            "AGAIN with confirmed=true to book. (A first-call confirmed=true just re-previews "
            "-- the tool books from a server-side stash, never from the confirm-turn fields, "
            "so an event/invite can never be sent unconfirmed.) "
            "The event is created in the asking user's own primary Google Calendar. "
            "If attendees are provided, Google sends them invitations automatically. "
            "The tool returns a clickable link to the created event — preserve the "
            "`<url|name>` Slack hyperlink syntax verbatim in your reply. "
            "Default time zone is America/Phoenix. "
            "Do not call this tool for questions about reading or viewing a calendar — "
            "use calendar_get_my_events for that."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Event title / name. Required.",
                },
                "start": {
                    "type": "string",
                    "description": (
                        "Start date and time in ISO 8601 format. Examples: "
                        "'2026-05-25T14:00' (naive, treated as America/Phoenix), "
                        "'2026-05-25T14:00:00-07:00' (explicit offset). Required."
                    ),
                },
                "end": {
                    "type": "string",
                    "description": (
                        "End date and time. Same format as start. Must be after start. Required."
                    ),
                },
                "attendees": {
                    "type": ["array", "string"],
                    "description": (
                        "Optional list (or comma-separated string) of attendee email addresses. "
                        "Google will send each attendee an invitation."
                    ),
                    "items": {"type": "string"},
                },
                "description": {
                    "type": "string",
                    "description": "Optional free-text event body / notes.",
                },
                "location": {
                    "type": "string",
                    "description": "Optional location — address, room name, or video link.",
                },
                "time_zone": {
                    "type": "string",
                    "description": (
                        "IANA time zone name for the event. Defaults to 'America/Phoenix'. "
                        "Use this if the user specifies a different city or time zone."
                    ),
                },
                "confirmed": {
                    "type": "boolean",
                    "description": (
                        "Set to true on the CONFIRM turn, after the user has seen the "
                        "NOT-CREATED preview and said yes. The first call (confirmed false/"
                        "omitted) previews + stashes; the tool books from that stash on the "
                        "confirm turn, so a first-call confirmed=true only re-previews."
                    ),
                },
            },
            "required": ["summary", "start", "end", "confirmed"],
        },
    },
    {
        "name": "calendar_delete_event",
        "description": (
            "Cancel / delete an event from the user's own Google Calendar. Use when the "
            "user asks to cancel, delete, remove, or call off a meeting or hold they have "
            "-- 'cancel the SMOKE TEST hold', 'delete my 2pm', 'remove the Friday sync'. "
            "STAGED WRITE (same as create): the FIRST call returns a NOT-CANCELLED preview "
            "identifying the event; show it and get the user's yes; then call AGAIN with "
            "confirmed=true to cancel. The tool resolves the event server-side (by event_id "
            "from a prior create/list, or by matching `query` within `when`) and deletes only "
            "that resolved event on confirm -- it never deletes on the first call and never "
            "from a confirm-turn-supplied id. Attendees are notified. If it can't pin down a "
            "single event it asks which one -- relay that; do NOT guess."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "The event title or keywords to cancel (e.g. 'SMOKE TEST HOLD', "
                        "'marketing sync'). Matched case-insensitively against events in the "
                        "`when` window. Omit only if you pass event_id."
                    ),
                },
                "when": {
                    "type": "string",
                    "description": (
                        "Search window for resolving `query`: 'today', 'tomorrow', "
                        "'this_week' (default, next 7 days), 'next_week', or 'YYYY-MM-DD'. "
                        "Pass the event's date when the user gives one."
                    ),
                },
                "event_id": {
                    "type": "string",
                    "description": (
                        "Optional direct event id from a prior calendar_create_event result "
                        "or event list, if you have it -- skips the query resolve."
                    ),
                },
                "confirmed": {
                    "type": "boolean",
                    "description": (
                        "Set to true on the CONFIRM turn, after the user has seen the "
                        "NOT-CANCELLED preview and said yes. First call previews; the tool "
                        "deletes the stashed event on the confirm turn."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "calendar_schedule_meeting",
        "description": (
            "Find up to 3 available times when ALL participants are free, present them "
            "as numbered options for the user to choose from, then book the chosen slot "
            "in Google Calendar. A Google Meet link is ALWAYS included automatically. "
            "Use this when anyone says things like 'schedule a meeting for Larry and me', "
            "'find a time for Harrison and Hannah', 'when can Tommy and I meet', "
            "'set up a call with Alex', 'find a time that works for all of us', or similar. "
            "\n"
            "TWO-PHASE FLOW:\n"
            "Phase 1 (confirmed=false) — call with participant names; the tool queries "
            "everyone's Google Calendar freebusy over the next 14 days and returns up to "
            "3 numbered options (1/2/3). Present these options clearly and ask the user "
            "to reply with their choice. Do NOT book yet.\n"
            "Phase 2 (confirmed=true) — once the user picks an option, call again with "
            "confirmed=true plus the exact proposed_start and proposed_end ISO strings "
            "from the chosen option. The tool creates the event, attaches a Google Meet "
            "link, and sends calendar invitations to all participants. "
            "The requester is always auto-included — pass only OTHER participants. "
            "Working hours: Mon-Fri 9 AM to 5 PM America/Phoenix. "
            "Default duration: 30 minutes. Search window: next 14 days."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "participants": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Names (or Slack display names) of people to include, "
                        "NOT including the requester (they are auto-added). "
                        "Example: ['Larry', 'Hannah']. Required."
                    ),
                },
                "duration_minutes": {
                    "type": "integer",
                    "description": (
                        "Meeting length in minutes. Defaults to 30. "
                        "Minimum 15. Example: 60 for a one-hour meeting."
                    ),
                },
                "title": {
                    "type": "string",
                    "description": (
                        "Optional event title / meeting name. "
                        "Defaults to 'Meeting' if omitted."
                    ),
                },
                "confirmed": {
                    "type": "boolean",
                    "description": (
                        "Phase gate. Set false (or omit) for Phase 1 (find + propose). "
                        "Set true for Phase 2 (book) -- only after user confirms. "
                        "NEVER set true on the first call."
                    ),
                },
                "proposed_start": {
                    "type": "string",
                    "description": (
                        "Phase 2 only. The exact proposed_start ISO string returned by "
                        "Phase 1. Example: '2026-06-02T09:00:00-07:00'."
                    ),
                },
                "proposed_end": {
                    "type": "string",
                    "description": (
                        "Phase 2 only. The exact proposed_end ISO string returned by "
                        "Phase 1. Example: '2026-06-02T09:30:00-07:00'."
                    ),
                },
            },
            "required": ["participants"],
        },
    },
    {
        "name": "fighter_compliance",
        "description": (
            "Read the MMA Lab x F3 Fighters Tracker Google Sheet and show MMA Lab "
            "sponsorship compliance. Use when Alex or Harrison asks: 'show fighter compliance', "
            "'who hasn't posted yet', 'what's the MMA Lab status for June', 'which fighters "
            "still owe us posts', 'how much do we owe MMA Lab', 'show fighter tracker', "
            "'who's missing their stories this month'. "
            "Tabs are per month (June 2026, July 2026, etc.). Make.com writes dates when "
            "fighters post. Shows who's done, who's missing deliverables, and amount owed "
            "to MMA Lab ($125 per fighter who completes all 3, max $6,250+/month). "
            "Read-only, no confirmation needed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "month_tab": {
                    "type": "string",
                    "description": "Month tab to read, e.g. 'June 2026', 'July 2026'. Defaults to current month if omitted.",
                },
                "show_complete": {
                    "type": "boolean",
                    "description": "If true, also lists fighters who completed all 3 deliverables. Default false (shows only incomplete).",
                },
            },
            "required": [],
        },
    },
    {
        "name": "influencer_list_handles",
        "description": (
            "List all sponsored athletes registered in Cora's influencer tracker — their "
            "names, platforms, and handles. Use this when Alex or Harrison wants to see "
            "the current athlete roster before registering someone new, confirm a handle "
            "spelling, or just audit who's being tracked — phrases like 'who are our "
            "registered athletes', 'show me the athlete handles', 'what athletes do we "
            "have in the tracker', 'is [athlete] registered already', 'list our influencers'. "
            "Read-only — no confirmation needed. Channel entity scope applies (in F3E "
            "channels only F3E athletes appear; FNDR channels see all entities). Optionally "
            "filter to a single platform with the `platform` parameter."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "platform": {
                    "type": "string",
                    "description": "Optional. Filter to a specific platform: 'instagram' or 'tiktok'. Omit to see all platforms.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "influencer_add_handle",
        "description": (
            "Register a sponsored athlete's social media handle in Cora's influencer tracker. "
            "Once registered, the automated Instagram scanner can automatically match that "
            "athlete's posts to their deliverables without Alex having to identify them. "
            "Use this when onboarding a new sponsored athlete or adding a platform handle for "
            "an existing athlete — phrases like 'add handle for [athlete]', 'register [athlete]'s "
            "Instagram', '[athlete] is @handle on TikTok', 'add [name] to the influencer tracker'.\n"
            "\n"
            "REQUIRED PATTERN (staged-write — never skip):\n"
            "1. Show a preview: Athlete name, platform, handle. Ask the user to confirm.\n"
            "2. Wait for explicit approval ('yes', 'register it', 'add it').\n"
            "3. Then call with confirmed=true.\n"
            "\n"
            "Platform values: 'instagram', 'tiktok'. "
            "Handle: the athlete's account username, with or without the @ symbol."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "athlete_name": {
                    "type": "string",
                    "description": "Full name of the sponsored athlete as it appears in their contract / deliverable records.",
                },
                "platform": {
                    "type": "string",
                    "description": "Social platform: 'instagram' or 'tiktok'.",
                },
                "handle": {
                    "type": "string",
                    "description": "The athlete's account handle on that platform (with or without @). Example: '@luispena_ufc' or 'luispena_ufc'.",
                },
                "entity": {
                    "type": "string",
                    "description": "Optional entity code for the sponsoring brand (default: F3E). Use UFL for fight-league athletes.",
                },
                "confirmed": {
                    "type": "boolean",
                    "description": "Required. Set to true ONLY after preview and explicit user approval.",
                },
            },
            "required": ["athlete_name", "platform", "handle", "confirmed"],
        },
    },
    {
        "name": "influencer_get_status",
        "description": (
            "Fetch the current status of influencer / sponsored-athlete deliverables tracked "
            "in Cora's influencer tracker. Use this when Alex, Harrison, or any team member "
            "asks about influencer compliance, pending posts, overdue deliverables, or wants "
            "a compliance report — phrases like 'what influencer deliverables are pending', "
            "'who's overdue on their posts', 'show me the influencer tracker', 'compliance "
            "report for our athletes', 'has [athlete] posted yet', 'what's outstanding for "
            "[name]'. Returns a Slack-formatted list or per-athlete compliance breakdown. "
            "Channel entity scope applies — in F3E channels only F3E-tagged deliverables "
            "appear; FNDR channels see all entities. Does NOT require confirmation — read-only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "report_type": {
                    "type": "string",
                    "description": (
                        "Type of report to generate. "
                        "'status' (default) — all open (pending + overdue) deliverables. "
                        "'overdue' — only past-due deliverables. "
                        "'compliance' — per-athlete compliance percentage table (complete / total owed)."
                    ),
                },
                "athlete": {
                    "type": "string",
                    "description": "Optional. Filter to a specific athlete by name (partial match). If omitted, returns all athletes.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "influencer_log_deliverable",
        "description": (
            "Add, complete, or waive an influencer deliverable in Cora's sponsored-athlete "
            "tracker. This is a WRITE tool — same staged-write pattern as other Cora write tools.\n"
            "\n"
            "REQUIRED PATTERN (staged-write — never skip):\n"
            "1. When the user asks to log, add, complete, or waive a deliverable, show a "
            "   PREVIEW BLOCK in your reply first. For add: show athlete, platform, type, due date. "
            "   For complete: show the deliverable ID, athlete, and optional link. "
            "   For waive: show the deliverable ID, athlete, and reason. "
            "   DO NOT call this tool on the first turn.\n"
            "2. Wait for the user's explicit approval ('yes', 'log it', 'mark it done', "
            "   'looks good', etc.). Then call with confirmed=true.\n"
            "3. If they want changes, re-show the preview and wait again.\n"
            "\n"
            "Use this tool when:\n"
            "- action=add: 'add a deliverable for [athlete]', 'log that [athlete] owes us a post', "
            "  'track a sponsored reel from [name] due [date]'.\n"
            "- action=complete: '[athlete] posted their story', 'mark #5 as done', "
            "  'log that [athlete] delivered their reel — here's the link'.\n"
            "- action=waive: 'waive [athlete]'s post this month', 'cancel the deliverable for [name]', "
            "  'mark #7 as excused — they had an injury'.\n"
            "\n"
            "For action=complete or action=waive: use EITHER deliverable_id (#N from status reports) "
            "OR athlete_name + deliverable_type (e.g. athlete_name='Mario Bautista', deliverable_type='story'). "
            "Name-based lookup finds the oldest pending deliverable matching that fighter and type — "
            "ideal when Alex replies to a Make.com Instagram notification like 'complete Mario Bautista story'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Required. One of: 'add' (create new deliverable), 'complete' (mark as done), 'waive' (mark as excused/cancelled).",
                },
                "athlete_name": {
                    "type": "string",
                    "description": (
                        "Name of the sponsored athlete / influencer. "
                        "Required for action=add. "
                        "For action=complete or action=waive: provide this INSTEAD OF deliverable_id "
                        "when Alex types naturally (e.g. 'complete Mario Bautista story'). "
                        "Partial match is supported — 'Mario' finds 'Mario Bautista'."
                    ),
                },
                "platform": {
                    "type": "string",
                    "description": "Social platform. Required for action=add. Examples: instagram, tiktok, youtube, twitter, podcast.",
                },
                "deliverable_type": {
                    "type": "string",
                    "description": "Type of deliverable. Required for action=add. Examples: post, story, reel, video, tweet, shoutout, podcast_mention.",
                },
                "due_date": {
                    "type": "string",
                    "description": "Optional. Due date in YYYY-MM-DD format. Used for action=add.",
                },
                "deliverable_id": {
                    "type": "integer",
                    "description": (
                        "ID of an existing deliverable (shown as #N in status reports). "
                        "For action=complete and action=waive: use this OR athlete_name — not both required. "
                        "If athlete_name is provided, deliverable_id can be omitted and Cora will resolve automatically."
                    ),
                },
                "campaign_month": {
                    "type": "string",
                    "description": "Optional. YYYY-MM format (e.g. 2026-06). Narrows name-based lookup to a specific month when a fighter has deliverables across multiple months.",
                },
                "completion_link": {
                    "type": "string",
                    "description": "Optional. URL to the completed post. Used for action=complete.",
                },
                "notes": {
                    "type": "string",
                    "description": "Optional. Free-text context (reason for waive, post caption, deal notes, etc.).",
                },
                "hubspot_deal_id": {
                    "type": "string",
                    "description": "Optional. HubSpot deal ID to link this deliverable to the source sponsorship deal. Used for action=add.",
                },
                "entity": {
                    "type": "string",
                    "description": "Optional. Entity code for the deal (e.g. F3E, UFL). Defaults to channel entity. Used for action=add.",
                },
                "confirmed": {
                    "type": "boolean",
                    "description": "Required. Set to true ONLY after you have shown the user a preview and received explicit approval. If false or omitted, the tool refuses.",
                },
            },
            "required": ["action", "confirmed"],
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
    {
        "name": "qbo_get_profit_loss",
        "description": (
            "Fetch a QuickBooks Online Profit & Loss summary for a portfolio entity over "
            "a date range. THIS IS THE PRIMARY TOOL FOR ALL REVENUE, P&L, AND INCOME QUESTIONS. "
            "Use this when a user asks about revenue, income, expenses, profitability, margin, "
            "or P&L performance for any time period -- phrases like: "
            "'Q1 revenue', 'Q1 LLC revenue', 'what was revenue in Q1', 'Q1 P&L', "
            "'how much did we make last month', 'what is revenue YTD', 'profit this month', "
            "'show me the P&L', 'how did we do in January', 'quarterly results'. "
            "For quarterly questions: Q1 = '2026-01-01 to 2026-03-31', Q2 = '2026-04-01 to 2026-06-30', "
            "Q3 = '2026-07-01 to 2026-09-30', Q4 = '2026-10-01 to 2026-12-31'. "
            "Returns top-line section totals (Income, COGS, Net Income). Present the numbers "
            "directly; do NOT add a QuickBooks/QBO link or name the source system. The tool defaults to the channel's entity, "
            "but the `entity` parameter can override (use it in FNDR/HJRG channels where "
            "the user names a specific entity). The `period` parameter controls the date "
            "range -- defaults to last_30_days. CALL THIS BEFORE financial_get_close_pack "
            "for any revenue or P&L question. Refuse and don't call this tool in TIER_3 "
            "channels per the financial guardrail."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity": {
                    "type": "string",
                    "description": "Optional entity code override (e.g. HJRG, F3E, OSN, LEX, BDM). If omitted, uses the channel's entity. Required when the channel is FNDR/HJRG and the user names a specific business.",
                },
                "period": {
                    "type": "string",
                    "description": "Time period for the P&L. Accepts: 'this_month', 'last_month', 'ytd', 'last_year', 'last_30_days' (default), 'last_90_days', or an explicit range like '2026-01-01 to 2026-03-31'.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "qbo_get_balance_sheet",
        "description": (
            "Fetch a QuickBooks Online Balance Sheet snapshot for a portfolio entity as of "
            "a specific date (defaults to today). Use this when a user in a TIER_1 channel "
            "asks about assets, liabilities, equity, cash position, balance sheet, or "
            "financial position. Returns top-level section totals (Total Assets, Total "
            "Liabilities, Equity). Present the numbers directly; do NOT add a QuickBooks/QBO "
            "link or name the source system. The "
            "`entity` parameter overrides the channel's entity. Refuse in TIER_3 channels."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity": {
                    "type": "string",
                    "description": "Optional entity code override. If omitted, uses the channel's entity.",
                },
                "as_of_date": {
                    "type": "string",
                    "description": "ISO date (YYYY-MM-DD) for the snapshot. Defaults to today if omitted.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "qbo_get_ar_aging",
        "description": (
            "Fetch a QuickBooks Online Accounts Receivable Aging summary for a portfolio "
            "entity. Use this when a user in a TIER_1 channel asks about open invoices, "
            "money owed to the business, customer collections, or AR aging buckets — "
            "phrases like 'who owes us money', 'what's outstanding on receivables', 'AR "
            "aging report'. Returns aging buckets (current, 1-30, 31-60, 61-90, 91+). "
            "Present the numbers directly; do NOT add a QuickBooks/QBO link or name the "
            "source system. The `entity` parameter overrides the channel's "
            "entity. Refuse in TIER_3 channels."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity": {
                    "type": "string",
                    "description": "Optional entity code override. If omitted, uses the channel's entity.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "qbo_get_ap_aging",
        "description": (
            "Fetch a QuickBooks Online Accounts Payable Aging summary for a portfolio "
            "entity. Use this when a user in a TIER_1 channel asks about unpaid vendor "
            "bills, money we owe, payables aging buckets, or upcoming vendor payments — "
            "phrases like 'what do we owe', 'AP aging', 'vendor balances outstanding'. "
            "Returns aging buckets (current, 1-30, 31-60, 61-90, 91+). Present the numbers "
            "directly; do NOT add a QuickBooks/QBO link or name the source system. The "
            "`entity` parameter overrides the channel's entity. "
            "Refuse in TIER_3 channels."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity": {
                    "type": "string",
                    "description": "Optional entity code override. If omitted, uses the channel's entity.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "qbo_get_recent_transactions",
        "description": (
            "Fetch a QuickBooks Online recent activity digest for a portfolio entity — "
            "counts of recently updated Invoices, Bills, and Payments over a configurable "
            "lookback window (defaults to 30 days). Use this when a user in a TIER_1 "
            "channel asks about recent QBO activity, what's been entered recently, or "
            "wants a high-level pulse on the books — phrases like 'what's been happening "
            "in QBO', 'any new invoices this week', 'recent activity'. Returns counts per "
            "type. Present the counts directly; do NOT add a QuickBooks/QBO link or name "
            "the source system. The `entity` parameter "
            "overrides the channel's entity. Refuse in TIER_3 channels."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity": {
                    "type": "string",
                    "description": "Optional entity code override. If omitted, uses the channel's entity.",
                },
                "days": {
                    "type": "integer",
                    "description": "Lookback window in days (1-180). Defaults to 30.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "financial_get_cashflow",
        "description": (
            "Fetch the current week's cash flow data from the portfolio's Standing ACTUALS "
            "sheet. Use this when a user in a TIER_1 or FNDR channel asks about cash "
            "position, weekly cash flow, how much cash we have, entity net cash, opening "
            "or closing balances, or portfolio-wide cash status — phrases like 'what's our "
            "cash this week', 'how is OSN doing on cash', 'portfolio cash position', 'what "
            "does the cash flow look like'. "
            "Returns the most recent week with actual data for all 18 portfolio entities "
            "plus portfolio totals and opening/closing balances. "
            "The `entity_filter` parameter scopes output to a single entity or entity group "
            "(e.g. 'OSN', 'LEX', 'LEX-LBHS'). Omit for portfolio-wide view. "
            "IMPORTANT: if this tool returns the UNKNOWN_RESPONSE string (starts with "
            "'I don\\'t have that right now'), immediately call financial_notify_gap. "
            "NEVER call this tool in TIER_3 (sales/ops) channels — financial guardrail applies."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_filter": {
                    "type": "string",
                    "description": (
                        "Optional entity code to scope output. Examples: 'OSN' (all OSN "
                        "stores), 'LEX' (all Lex entities), 'OSN-GW' (Gilbert & Warner), "
                        "'F3E', 'BDM', 'UFL'. Omit for full portfolio view."
                    ),
                },
            },
            "required": [],
        },
    },
    # ── FNDR-specific tools (founder / HJRG channels only) ──
    {
        "name": "fndr_completion_candidates",
        "description": (
            "Scan recent KB activity (Fireflies transcripts, Slack, email, HubSpot) for "
            "signals that a task or project was completed, then cross-reference those signals "
            "against open Asana tasks and return a digest of completion candidates with "
            "clickable Asana links. "
            "Use this when someone asks: 'what tasks should be closed?', 'any tasks that look "
            "done?', 'run a completion sweep', 'what can we mark complete?', 'hygiene sweep', "
            "'what's been finished that's still showing as open?', 'check for stale open tasks', "
            "'what did we complete this week?', 'anything we forgot to close out?'. "
            "Returns 🟢 High and 🟡 Medium confidence candidates with the triggering excerpt "
            "and a deep link to open the task in Asana. Never auto-completes — the human "
            "clicks the link and marks done. "
            "Read-only. Preferred in FNDR or HJRG channels for cross-entity sweep; "
            "also works in entity channels (OSN, F3E, LEX, etc.) for entity-scoped results."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "fndr_open_decisions",
        "description": (
            "Return stalled decisions from the pending decisions queue, entity-scoped to "
            "the calling channel. In FNDR/HJRG channels returns all portfolio P0+P1 items. "
            "In entity-specific channels (OSN, F3E, LEX-LLC, LEX-LLA, HJRP, UFL, etc.) "
            "returns only that entity's open decisions — P0, P1, and P2 — so operators "
            "see exactly what's blocking their entity this week. "
            "Use this when someone asks about open or stalled decisions, what decisions "
            "are pending, what's blocking progress, what needs to be decided — phrases like "
            "'what decisions are pending', 'what's stalled', 'show me the decision queue', "
            "'what P0s do I have', 'what do I need to decide', 'what's blocking us', "
            "'what's been waiting on me', 'what decisions are open for OSN/F3E/Lex'. "
            "Returns: 🚨 P0 stale >14d (decide this week), 🔴 P0 <=14d, 🟡 P1, ⚪ P2. "
            "Read-only — no confirmation needed. Call in any channel."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    # ── F3E Shopify DTC tools ──
    {
        "name": "f3e_shopify_sales_pulse",
        "description": (
            "Fetch F3 Energy DTC sales data from the Shopify store for a time period. "
            "Use this when a user asks about DTC orders, online revenue, Shopify sales, "
            "e-commerce performance, or top-selling products -- phrases like 'how are DTC "
            "sales today', 'what did we do online yesterday', 'Shopify revenue this week', "
            "'how many orders today', 'what's our AOV', 'what products are selling', "
            "'how are online sales', 'DTC numbers'. "
            "Returns order count, gross revenue, discounts, refunds, net revenue, AOV, "
            "and top 5 products by revenue. Output is source-opaque -- never mention "
            "Shopify, platform names, or store URLs in your reply. "
            "Only call in F3E or FNDR channels. Apply TIER_1 guardrail for revenue figures."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {
                    "type": "string",
                    "enum": ["today", "yesterday", "7d", "30d"],
                    "description": "Time window for sales data. Defaults to 'today'.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "f3e_shopify_inventory",
        "description": (
            "Fetch F3 Energy product inventory levels from the Shopify store. "
            "Use this when a user asks about DTC inventory, stock levels, what's in stock, "
            "low stock SKUs, or how many units we have -- phrases like 'what's our inventory', "
            "'what SKUs are low', 'how much Pure do we have', 'stock check', 'inventory status', "
            "'what's running low', 'are we out of anything'. "
            "Returns variant-level inventory with low-stock flags (default threshold: 10 units). "
            "Defaults to low-stock-only view; set low_stock_only=false for full inventory. "
            "Note: Nimbl 3PL syncs to Shopify in real time -- this is the canonical inventory. "
            "Output is source-opaque. Only call in F3E or FNDR channels."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "low_stock_only": {
                    "type": "boolean",
                    "description": "If true (default), return only SKUs at or below the threshold. Set false for full inventory.",
                },
                "threshold": {
                    "type": "integer",
                    "description": "Units-remaining threshold for 'low stock' flag. Defaults to 10.",
                },
            },
            "required": [],
        },
    },
    # ── F3E DTC inventory WRITE (staged) ──
    {
        "name": "f3e_shopify_set_inventory",
        "description": (
            "STAGED-WRITE tool. Set F3 Energy DTC inventory to an absolute number "
            "for ONE product/variant at ONE location. Use when a user asks to set, "
            "update, correct, or adjust the on-hand count -- phrases like 'set Pure "
            "Original at the office to 240', 'update Mood 12-pack stock to 50', "
            "'change the office count for Energy to 0'. This is a WRITE: two calls. "
            "First call with confirmed=false (or omitted) resolves the product + "
            "location, reads the CURRENT count, and returns a preview for the user "
            "to approve. Then, after the user says yes, call again with "
            "confirmed=true -- I REMEMBER the exact item, location, and target from "
            "the preview, so you do NOT need to re-echo anything; just confirmed=true "
            "(re-passing the same product/location/quantity is fine but not required, "
            "and if the user changed the number, pass the new quantity and I'll "
            "re-preview). I re-check the live count before writing. "
            "Relay refusals plainly (do not argue): a synced location (a fulfillment "
            "partner's) can't be set manually -- only the office; an ambiguous product "
            "or location means ask which one; an un-stocked item can't be set. "
            "Source-opaque: never name the store, platform, or a URL -- say 'DTC "
            "inventory'/'online'. F3E channels only (or Harrison cross-entity)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "product": {
                    "type": "string",
                    "description": (
                        "Product/variant name or SKU to set, e.g. 'Pure Original 12-pack' "
                        "or a SKU. If it matches more than one variant the tool asks you "
                        "to disambiguate; pass the fuller name the user gives."
                    ),
                },
                "location": {
                    "type": "string",
                    "description": (
                        "Location name to set at, e.g. 'office'. Ask the user which "
                        "location -- do NOT assume one."
                    ),
                },
                "quantity": {
                    "type": "integer",
                    "description": "The absolute on-hand count to set (0 or more).",
                },
                "confirmed": {
                    "type": "boolean",
                    "description": (
                        "false/omitted = preview only. true = execute the previewed "
                        "change, ONLY after the user approved it. On true I use the "
                        "item/location/target I resolved during the preview and re-check "
                        "the live count first -- no echo needed."
                    ),
                },
                "expected_current": {
                    "type": "integer",
                    "description": (
                        "Optional / legacy. Ignored for the confirm decision -- the tool "
                        "re-reads the live count itself. Safe to omit."
                    ),
                },
                "expected_item": {
                    "type": "string",
                    "description": (
                        "Optional / legacy. Ignored for the confirm decision -- the tool "
                        "binds identity to its own resolved ids, not this echo. Safe to omit."
                    ),
                },
                "expected_location": {
                    "type": "string",
                    "description": (
                        "Optional / legacy. Ignored for the confirm decision. Safe to omit."
                    ),
                },
            },
            "required": ["product", "location", "quantity", "confirmed"],
        },
    },
    # ── F3E warehouse inventory (batch report, not live DTC) ──
    {
        "name": "f3e_inventory_pulse",
        "description": (
            "Use this for WAREHOUSE STOCK LEVELS from the weekly batch report (Cotton 3PL "
            "warehouse, Nimbl lot totals, 117 office). Do NOT use for live DTC inventory — "
            "use f3e_shopify_inventory for that. Use when user explicitly asks about warehouse "
            "stock, weekly inventory report, Cotton 3PL levels, or total cans across all "
            "locations — phrases like 'what does the inventory report say', 'how many cans "
            "do we have total', 'what's in the warehouse', 'Cotton 3PL stock', 'Nimbl inventory', "
            "'how many cases do we have across all locations'. "
            "Returns case counts by SKU across UNIS warehouse, Nimbl 3PL lot, and 117 office, "
            "with 🚨 Critical (≤50 cases), ⚠️ Low (≤200 cases), ✅ Healthy flags. "
            "Read-only. Only call in F3E or FNDR channels (#f3e-ops, #f3e-leadership, #fndr-*)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    # ── F3E location-specific inventory ──
    {
        "name": "f3e_inventory_by_location",
        "description": (
            "Return F3E inventory for a specific named location. "
            "Use when a user asks about stock at a particular location -- phrases like "
            "'how much Pure do we have at Nimbl', 'how many Mood cases at UNIS', "
            "'what's the Energy inventory at the warehouse', 'office stock for Pure', "
            "'Nimbl inventory for Mood', 'how many cases at Cotton', "
            "'what's in the UNIS warehouse for Energy', 'live Nimbl stock'.\n"
            "\n"
            "Location routing:\n"
            "  - 'nimbl'  -> LIVE Shopify inventory (real-time; Nimbl syncs with Shopify).\n"
            "  - 'unis', 'warehouse', or 'cotton' -> weekly Excel batch report (snapshot).\n"
            "  - 'office' or '117' -> weekly Excel batch report (snapshot).\n"
            "\n"
            "The optional `brand` parameter filters to one F3 sub-brand. "
            "If the user says 'Pure inventory at Nimbl', set brand=pure and location=nimbl. "
            "If they say 'Nimbl inventory' with no brand, omit brand to return all SKUs. "
            "Read-only -- no confirmation needed. "
            "Call in any #f3e-*, #f3-*, or FNDR channel."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": (
                        "Which location to query. Required. Case-insensitive. "
                        "Valid values: 'nimbl' (live Shopify data), "
                        "'unis' / 'warehouse' / 'cotton' (weekly Excel snapshot), "
                        "'office' / '117' (weekly Excel snapshot)."
                    ),
                },
                "brand": {
                    "type": "string",
                    "description": (
                        "Optional. Filter to one F3 sub-brand. "
                        "Values: 'Pure', 'Mood', or 'Energy' (case-insensitive). "
                        "Omit to return all brands at that location."
                    ),
                },
            },
            "required": ["location"],
        },
    },
    # ── F3 brand voice tools (F3E only — social channels + any F3E/FNDR channel) ──
    {
        "name": "f3e_brand_voice_check",
        "description": (
            "Check a draft piece of copy (caption, email, web copy, ad creative) against "
            "F3 Energy brand-guidelines V1 voice spec for a specified sub-brand (Pure, Mood, "
            "or Energy). Use this when a user asks whether copy is on-brand, requests a brand "
            "check, or shares draft content for review — phrases like '@Cora is this copy "
            "on-brand?', '@Cora does this caption fit Pure's voice?', '@Cora check this for "
            "Mood', 'is this content on-brand for Energy?', '@Cora brand check:', 'does this "
            "fit Lauren's voice?', 'is this Marcus-level tone?', 'check this against our brand "
            "guidelines', 'review this copy for F3 Pure'. "
            "Returns a structured analysis: CRITICAL issues (health claims, sleep positioning "
            "for Mood, competitor brand names, UFL crossover, anti-positioning violations), "
            "WARNINGS (sibling-brand drift — Energy-lane language in Pure copy, etc.), and a "
            "verdict. Also returns the brand's locked voice-pillar summary so you can give "
            "informed, specific guidance. "
            "Read-only — no confirmation needed. "
            "Call in #f3-pure-social, #f3-mood-social, #f3-energy-social, any #f3e-* or #f3-* "
            "channel, or any FNDR channel when copy is explicitly for an F3 brand. "
            "After presenting findings, always offer to help revise if issues are found."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "brand": {
                    "type": "string",
                    "description": (
                        "The F3 sub-brand this copy is for. Required. "
                        "Must be one of: 'pure', 'mood', or 'energy' (case-insensitive). "
                        "If the user is in #f3-pure-social, default to 'pure'; "
                        "#f3-mood-social → 'mood'; #f3-energy-social → 'energy'. "
                        "If ambiguous, ask the user which brand before calling."
                    ),
                },
                "copy": {
                    "type": "string",
                    "description": (
                        "The draft copy to check — caption, email body, web copy, ad creative, "
                        "or any text intended for an F3 brand surface. "
                        "Include the FULL text (no truncation) so the check is complete. "
                        "If the user pastes copy in their message, extract it verbatim."
                    ),
                },
            },
            "required": ["brand", "copy"],
        },
    },
    {
        "name": "fndr_contracts_dashboard",
        "description": (
            "Fetch the FNDR/HJRG Contracts and Renewals Registry from Notion. "
            "Use this when a user asks about contract status, upcoming renewals, "
            "lease expirations, or risk flags -- phrases like 'what contracts are "
            "expiring', 'show me renewals', 'any Escalate-flagged contracts', "
            "'contracts dashboard', 'what leases are up'. "
            "Returns contracts sorted by days remaining with risk flags and Escalate "
            "notices for items requiring Harrison's attention. "
            "Call in any FNDR or HJRG channel. No inputs required."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "fndr_press_pipeline_summary",
        "description": (
            "Fetch the FNDR/HJRG press-acquisition pipeline (Media Contacts) summary. "
            "Use this when a user asks about press / media outreach status, the press "
            "pipeline, journalist outreach, published features, or Wikipedia-AfC press "
            "progress -- phrases like 'press pipeline summary', 'press pipeline', "
            "'how's the press strategy going', 'media contacts', 'what have we pitched', "
            "'who's published us', 'press coverage status'. "
            "Returns total contacts + status breakdown, per-entity Published-feature "
            "progress vs the Wikipedia AfC threshold (F3 Energy 3, Lexington 2), the "
            "active pitched/responded reporters, and the to-pitch list with deep links. "
            "Call in any FNDR or HJRG channel only. No inputs required."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "f3e_hubspot_pipeline_summary",
        "description": (
            "Fetch a source-opaque sales summary of the F3E HubSpot pipeline. "
            "Use this when a user asks for a sales summary, pipeline overview, or deal status "
            "in F3E channels -- phrases like '@Cora sales summary', 'what's in the pipeline', "
            "'show me our deals', 'pipeline update', 'how are sales looking'. "
            "Returns stage breakdown, hot deals, and total pipeline value. "
            "Call in any F3E or FNDR channel. No inputs required."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "f3e_ai_visibility",
        "description": (
            "Fetch F3's AI-visibility scores -- how often ChatGPT / Perplexity / "
            "Gemini / Claude (and Google AI Overviews) recommend F3 Energy, Pure, and "
            "Mood when people ask buyer-style questions. Read-only. Use this for "
            "'@Cora what's our AI visibility score', 'are we showing up in AI search', "
            "'AI visibility', 'do the AI engines recommend us', 'where do competitors "
            "beat us in AI answers'. Returns each brand's 0-100 score, week-over-week "
            "delta, unaided presence, share-of-voice, and the top prompts where a "
            "competitor is named but F3 isn't. Present the tool output as-is; do NOT "
            "answer from KB memory. Call in any F3E or FNDR channel. No inputs required."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    # ── OSN financial tools (OSN and FNDR channels) ──
    {
        "name": "osn_financial_pulse",
        "description": (
            "Fetch a store-by-store financial snapshot for all four OSN locations from the "
            "13-week rolling cash-flow sheet. Use this when a user asks about OSN's overall "
            "financial health, cash position, how stores compare, which store is performing "
            "best or worst, weekly cash flow, or breakeven trajectory -- phrases like 'how is "
            "OSN doing financially', 'give me the OSN financial pulse', 'how are the stores "
            "tracking vs forecast', 'which OSN location is negative', 'OSN cash position', "
            "'how is G Warner vs forecast', 'store-by-store P&L', 'what's OSN's financial "
            "picture this week'. "
            "Returns actual vs forecast vs diff for each store (Gilbert & Warner, "
            "Gilbert & McKellips, Greenfield & 60, Val Vista & Pecos) plus the OSN "
            "portfolio total. Negative-variance stores are flagged with a warning indicator. "
            "Data is sourced from the 13-week Standing ACTUALS sheet maintained by Hayden / "
            "Justin. Output is source-opaque -- never mention sheet names, file IDs, or Drive. "
            "If data is unavailable, returns the standard UNKNOWN_RESPONSE and Cora notifies "
            "the finance channel. "
            "MANDATORY TOOL CALL for any OSN financial health question -- do NOT answer from "
            "KB memory or prior context. Always call this tool to get current data. "
            "Only call in OSN or FNDR channels."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    # ── Drive-based financial pulse + monthly close pack ──
    {
        "name": "financial_get_pulse",
        "description": (
            "Read the weekly financial pulse summary for an entity from the live-sheets "
            "Drive folder. Use this when a user in a *-finance channel asks about current "
            "financial health, weekly performance, or wants a high-level financial snapshot "
            "-- phrases like 'give me the financial pulse', 'how are we doing financially', "
            "'weekly financial summary', 'what's the latest on financials'. "
            "Supported entities: OSN (all stores), F3E, LEX (and all sub-entities). "
            "Returns the .md pulse file content as-is. Data is maintained by Hayden/Justin "
            "and updated weekly. Output is source-opaque -- never mention file names or Drive. "
            "FINANCE CHANNEL ONLY: returns an access-denied message in any non-finance channel. "
            "If data is unavailable, returns the standard UNKNOWN_RESPONSE."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "financial_get_close_pack",
        "description": (
            "Read a monthly close pack REPORT FILE (P&L, Balance Sheet, Cash Flow, AR aging, "
            "or AP aging) for an entity from the Drive monthly-reports folder. "
            "USE THIS ONLY AS A FALLBACK when qbo_get_profit_loss returns no data -- "
            "this tool reads archived Excel report files that Hayden/Justin file in Drive "
            "each month. It does NOT query live QuickBooks data. "
            "Use for: 'show me the filed April close pack', 'get the March report from Drive', "
            "'what did Hayden file for February', 'the monthly report for March', "
            "'AR aging report for February', 'what do we owe (AP) per the close pack'. "
            "DO NOT use for: 'Q1 revenue', quarterly results, or any live accounting question "
            "-- use qbo_get_profit_loss instead. "
            "Reports cover all portfolio entities. Files are named {YYYY-MM}_{entity}_{type}.xlsx. "
            "FINANCE CHANNEL ONLY. If the report file is not found in Drive, returns UNKNOWN_RESPONSE. "
            "When the user says 'last month', resolve to the prior calendar month. Default doctype pl."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {
                    "type": "string",
                    "description": (
                        "Data period in YYYY-MM format (e.g. '2026-04' for April 2026). "
                        "Required. Resolve relative references: 'last month', 'March', etc."
                    ),
                },
                "doctype": {
                    "type": "string",
                    "enum": ["pl", "bs", "cf", "ar", "ap"],
                    "description": (
                        "Report type: pl=Profit & Loss, bs=Balance Sheet, cf=Cash Flow, "
                        "ar=Accounts Receivable Aging, ap=Accounts Payable Aging. "
                        "Default: pl."
                    ),
                },
            },
            "required": ["period"],
        },
    },
    # ── Ad performance tools (F3E only — scoped by entity check in f3e.md prompt) ──
    {
        "name": "ads_get_performance_summary",
        "description": (
            "Fetch F3 Energy's blended ad performance summary across all paid channels. "
            "Use this when a user asks about overall ad spend, ROAS, CAC, POAS, or ad "
            "efficiency — phrases like 'how are our ads doing', 'what's our ROAS', "
            "'what did we spend on ads', 'how's our CAC looking', 'are ads profitable', "
            "'what's our ad performance this month', 'how's paid performing'. "
            "Returns total spend, blended ROAS, new-customer ROAS, POAS, blended CAC, "
            "paid CPO, net revenue after ads, and Amazon ad metrics. "
            "All output is source-opaque — no platform names, no account references. "
            "Defaults to last 30 days; user can request a different window. "
            "Only call in F3E or FNDR channels. Do NOT call for OSN, LEX, BDM, or UFL."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "lookback_days": {
                    "type": "integer",
                    "description": (
                        "Number of days to look back from yesterday (default 30). "
                        "Use 7 for 'this week', 30 for 'this month', 90 for 'last quarter'. "
                        "Max 365."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "ads_get_channel_breakdown",
        "description": (
            "Fetch F3 Energy ad performance broken down by marketing channel. "
            "Use this when a user asks which channels are working, channel-level ROAS or "
            "spend allocation — phrases like 'which channels are performing best', "
            "'how is Meta vs Google', 'where should we shift budget', 'channel breakdown', "
            "'what's our spend by channel', 'paid social vs paid search performance'. "
            "Returns spend, ROAS, and CAC per channel group. "
            "Output is source-opaque — channel names come from the custom dimension "
            "configured in Polar (set by Harrison in Settings → Custom Dimensions → "
            "Default channel grouping). Never name the underlying platforms directly. "
            "NOTE: Amazon channels will appear only after Harrison adds an 'Amazon' rule "
            "to the Polar custom channel grouping (pre-build gate). "
            "Only call in F3E or FNDR channels."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "lookback_days": {
                    "type": "integer",
                    "description": "Number of days to look back from yesterday (default 30).",
                },
            },
            "required": [],
        },
    },
    {
        "name": "ads_get_subbrand_performance",
        "description": (
            "Fetch F3 Energy ad performance split by sub-brand: F3 Pure, F3 Mood, and "
            "F3 Energy (core). Use this when a user asks about brand-level performance, "
            "which product line is driving results, or wants to compare brands — phrases "
            "like 'how is F3 Pure doing on ads', 'which brand has the best ROAS', "
            "'Pure vs Energy ad performance', 'sub-brand breakdown', 'brand-level spend'. "
            "Returns spend, blended ROAS, CAC, net revenue after ads, and subscription "
            "share per brand. Output is source-opaque. "
            "Only call in F3E or FNDR channels."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "lookback_days": {
                    "type": "integer",
                    "description": "Number of days to look back from yesterday (default 30).",
                },
            },
            "required": [],
        },
    },
    {
        "name": "ads_get_pixel_attribution",
        "description": (
            "Fetch first-party pixel attribution data vs platform-reported ROAS for F3 "
            "Energy. Use this when a user asks about attribution accuracy, platform "
            "over-reporting, true ROAS, or pixel vs platform discrepancies — phrases like "
            "'what does the pixel say', 'is Meta over-reporting', 'true ROAS vs reported', "
            "'first-party attribution', 'how accurate are the platform numbers', "
            "'pixel CAC'. "
            "Returns pixel ROAS (blended + paid), pixel CAC, pixel CPO, platform-reported "
            "ROAS, and the attribution gap delta. Output is source-opaque. "
            "Only call in F3E or FNDR channels."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "lookback_days": {
                    "type": "integer",
                    "description": "Number of days to look back from yesterday (default 30).",
                },
            },
            "required": [],
        },
    },
    {
        "name": "ads_get_cm_waterfall",
        "description": (
            "Fetch the F3 Energy contribution margin waterfall (CM1 through CM4). "
            "Use this when a user asks about profitability after marketing spend, "
            "contribution margin, CM3, whether marketing is eating into margin — phrases "
            "like 'what's our CM3', 'contribution margin after ads', 'how much margin "
            "are we left with after marketing', 'CM waterfall', 'are we hitting our "
            "margin targets', 'profitability after ad spend'. "
            "Returns CM1 (after COGS), CM2 (after variable opex), CM3 (after marketing "
            "— primary health metric), and CM4 (after fixed opex) as both $ and %. "
            "CM3 is compared against the target floor set in the Manus snapshot. "
            "Output is source-opaque. Only call in F3E or FNDR channels. "
            "This is a financial-adjacent question — apply TIER_3 guardrail in non-"
            "leadership channels: refuse and redirect to #f3e-finance or #f3e-leadership."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "lookback_days": {
                    "type": "integer",
                    "description": "Number of days to look back from yesterday (default 30).",
                },
            },
            "required": [],
        },
    },
    # ---------------------------------------------------------------------------
    # PhotoRoom image generation tools (Session 2 wiring — stubs registered here,
    # handlers in tools/generate_image.py once that file is written)
    # ---------------------------------------------------------------------------
    {
        "name": "f3_generate_image",
        "description": (
            "Generate an F3 brand image via PhotoRoom AI Backgrounds and wire it to "
            "Shopify. Accepts a spec JSON (per the image spec schema) or a Drive file "
            "ID pointing to a spec. Returns Shopify File GID + cost summary. "
            "Supports dry_run=true to estimate cost without consuming an API credit. "
            "Scope: F3E channels only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "spec": {
                    "type": "object",
                    "description": "Image spec JSON per the PhotoRoom spec schema.",
                },
                "spec_drive_file_id": {
                    "type": "string",
                    "description": "Alternative: Google Drive file ID of a spec JSON file.",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": (
                        "If true, validates the spec and estimates cost without calling "
                        "PhotoRoom. Returns 'Would generate 1 image. Cost: $0.10.'"
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "f3_batch_image_run",
        "description": (
            "Run a batch of F3 image generation specs from a Google Drive folder. "
            "Processes all JSON spec files in the folder in series, respecting rate "
            "limits. Posts a batch summary to Slack when done. Hard cap: 50 images "
            "per batch. Scope: FNDR or F3E channels only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "spec_folder_drive_id": {
                    "type": "string",
                    "description": "Google Drive folder ID containing JSON spec files.",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "If true, validates all specs and estimates total cost only.",
                },
            },
            "required": ["spec_folder_drive_id"],
        },
    },
    {
        "name": "f3_create_image",
        "description": (
            "Generate an F3 brand image from a plain-English creative brief. "
            "Claude writes a PhotoRoom-quality background prompt from the F3 brand "
            "guidelines, PhotoRoom renders the scene behind the product can, and the "
            "finished PNG is uploaded to the team's Drive review folder. Cora posts "
            "the Drive link in Slack so Harrison and BDM can review before publishing. "
            "Use when someone says 'generate an image of...', 'create a photo of...', "
            "'make a lifestyle shot of...', or describes a desired scene. "
            "Scope: F3E or FNDR channels only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "brand": {
                    "type": "string",
                    "enum": ["pure", "mood", "energy"],
                    "description": "F3 sub-brand for this image.",
                },
                "brief": {
                    "type": "string",
                    "description": (
                        "Plain-English description of the desired scene. "
                        "Examples: 'person holding a can next to a pool', "
                        "'woman on a morning walk through a suburban neighborhood', "
                        "'outdoor farmers market, golden hour'. "
                        "Min 10 characters."
                    ),
                },
                "output_size": {
                    "type": "string",
                    "enum": ["1920x900", "1080x1080", "1200x628", "1920x1080"],
                    "description": "Output dimensions. Default: 1920x900 (hero banner).",
                },
                "main_image_url": {
                    "type": "string",
                    "description": (
                        "Optional: override the default product can image URL. "
                        "Use a Shopify CDN URL for a specific SKU."
                    ),
                },
                "dry_run": {
                    "type": "boolean",
                    "description": (
                        "If true, shows the Claude-generated background prompt "
                        "without calling PhotoRoom. Useful for prompt review."
                    ),
                },
            },
            "required": ["brand", "brief"],
        },
    },
    {
        "name": "f3_create_sales_deck",
        "description": (
            "Generate a customized F3 Energy distributor sales deck. "
            "Claude writes the slide content from F3 brand guidelines and program data, "
            "then fires a Make automation that fills a Canva brand template, exports a PDF, "
            "uploads it to Google Drive, and DMs the requester the link. "
            "Use when someone says 'create a sales deck', 'make a pitch deck', "
            "'I need a deck for a distributor meeting', 'build a presentation for [distributor]', "
            "or similar. The requester will receive a Slack DM with the Drive link in ~2 minutes. "
            "Scope: F3E or FNDR channels only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "distributor_name": {
                    "type": "string",
                    "description": (
                        "Name of the distributor or company being presented to. "
                        "Examples: 'Hensley', 'KeHE Distributors', 'UNFI', 'Sysco'. "
                        "Used in slide titles and personalized copy."
                    ),
                },
                "programs": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["pure", "mood", "energy"]},
                    "description": (
                        "F3 sub-brands to include in the deck. "
                        "Defaults to all three (pure, mood, energy) if not specified. "
                        "Pass a subset to build a focused single-brand deck."
                    ),
                },
                "notes": {
                    "type": "string",
                    "description": (
                        "Optional context about the distributor or meeting. "
                        "Examples: 'Texas-only distributor focused on health food', "
                        "'they already carry a competitor', 'meeting is next Tuesday at their HQ'. "
                        "Claude uses this to personalize the content."
                    ),
                },
                "distributor_logo_url": {
                    "type": "string",
                    "description": (
                        "Optional: direct URL to the distributor's logo (PNG or JPG). "
                        "If provided, Canva embeds it on the cover slide. "
                        "If omitted, the cover shows the distributor name as text."
                    ),
                },
            },
            "required": ["distributor_name"],
        },
    },
    # --- LEX tools ---
    {
        "name": "lex_revalidation_status",
        "description": (
            "Return the live AZ DDD Therapy Revalidation status from Asana. "
            "ALWAYS call this tool when any user asks about revalidation status, "
            "days remaining to the deadline, open blockers, sub-task progress, "
            "or whether the revalidation is on track. Do NOT answer from KB memory -- "
            "the tool fetches live Asana data and returns days-remaining to 2026-06-30, "
            "open sub-task blockers with assignees and due dates, and the age of the "
            "last comment. Present its output as-is without truncating or summarizing.\n"
            "\n"
            "Trigger phrases: 'revalidation', 'DDD revalidation', 'AHCCCS revalidation', "
            "'Provider Type 15', 'June 30 deadline', '6/30 deadline', 'revalidation status', "
            "'what's happening with the revalidation', 'are we on track for June 30'.\n"
            "\n"
            "Scope: LEX / LEX-* channels and FNDR/HJRG. Always surface in the "
            "Sunday-evening #lex-leadership brief."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "hjrp_lease_status",
        "description": (
            "Return the HJR Properties lease register for both buildings — North "
            "Hampton (1337 S Gilbert) and South Hampton (1555 S Gilbert). For each "
            "tenant it returns the lease-end date, days-to-expiry, status, plus the "
            "upcoming renewal cluster(s) with monthly rent at risk, upcoming "
            "vacancies, and broker contacts. ALWAYS call this tool when a user asks "
            "about HJRP leases, lease renewals, when a tenant's lease expires, which "
            "leases are coming up, the October 2026 cluster, rent at risk, upcoming "
            "vacancies, or relist status. Do NOT answer from KB memory — present the "
            "tool output as-is without truncating or summarizing.\n"
            "\n"
            "Trigger phrases: 'lease status', 'lease renewals', 'which leases expire', "
            "'when does <tenant>'s lease end', 'October 2026 cluster', 'rent at risk', "
            "'upcoming vacancies', 'what's expiring', 'lease register', 'renewal timeline'.\n"
            "\n"
            "Scope: HJRP / HJRP-* channels and FNDR/HJRG. Lease economics are financial "
            "— TIER_1 channels only (#hjrp-finance, #hjrp-leadership). In TIER_3 HJRP "
            "channels, do NOT call this tool; the financial guardrail applies."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    # --- HubSpot two-way write tools ---
    {
        "name": "hubspot_update_deal_stage",
        "description": (
            "Update a HubSpot deal's pipeline stage. STAGED-WRITE TOOL -- show a preview "
            "and receive explicit approval (confirmed=true) before mutating.\n\n"
            "Trigger phrases: 'move deal to', 'update deal stage', 'advance deal', "
            "'change stage for', 'mark deal as'.\n\n"
            "Scope: FNDR, F3E, OSN, BDM channels only. Not available in LEX channels."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "deal_id": {
                    "type": "string",
                    "description": "HubSpot deal ID (numeric string).",
                },
                "stage_id": {
                    "type": "string",
                    "description": "Target stage ID (from pipeline stage list).",
                },
                "confirmed": {
                    "type": "boolean",
                    "description": "Must be true after user approves the preview.",
                },
            },
            "required": ["deal_id", "stage_id", "confirmed"],
        },
    },
    {
        "name": "hubspot_add_note",
        "description": (
            "Add a note to a HubSpot deal. STAGED-WRITE TOOL -- show a preview and receive "
            "explicit approval (confirmed=true) before writing.\n\n"
            "Trigger phrases: 'add note to deal', 'log note', 'note on deal', 'update deal notes'.\n\n"
            "Scope: FNDR, F3E, OSN, BDM, HJRG channels. Not available in LEX channels."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "deal_id": {
                    "type": "string",
                    "description": "HubSpot deal ID (numeric string).",
                },
                "note_body": {
                    "type": "string",
                    "description": "Full text of the note to add.",
                },
                "confirmed": {
                    "type": "boolean",
                    "description": "Must be true after user approves the preview.",
                },
            },
            "required": ["deal_id", "note_body", "confirmed"],
        },
    },
    # --- Cross-entity write tools ---
    {
        "name": "slack_send_dm",
        "description": (
            "Send a Slack DM to a named teammate on behalf of Cora. "
            "STAGED-WRITE TOOL -- you MUST show a preview (recipient name + full message text) "
            "and receive the user's explicit approval before calling with confirmed=true.\n"
            "\n"
            "Guardrails:\n"
            "- LEX channels are BLOCKED (PHI risk). Do not attempt from any LEX context.\n"
            "- Recipient must be a mapped teammate (in slack-to-asana.yaml).\n"
            "- Message must be non-PHI, non-financial, non-cross-entity.\n"
            "\n"
            "Trigger phrases: 'message Larry', 'DM Sean', 'send Tommy a note', "
            "'let Hannah know', 'ping Shaun', 'message the team'.\n"
            "\n"
            "Scope: FNDR, F3E, OSN, BDM, HJRG channels only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "recipient_name": {
                    "type": "string",
                    "description": "Name of the teammate to DM. Accepts first name, full name, or alias (e.g. 'Larry', 'Larry Jackson', 'Sean', 'Shaun Hawkins').",
                },
                "message": {
                    "type": "string",
                    "description": "Full text of the DM to send. Write the complete message -- no placeholders.",
                },
                "confirmed": {
                    "type": "boolean",
                    "description": "Must be true. Only set after showing user a preview (recipient + message) and receiving explicit approval.",
                },
            },
            "required": ["recipient_name", "message", "confirmed"],
        },
    },
    {
        "name": "whats_on_my_plate",
        "description": (
            "Role-scoped composite picture of the asking user's current work. Use this "
            "whenever the user asks for their overall plate, workload, day, or focus — "
            "phrases like 'what's on my plate', 'what do I have going on', 'what should I "
            "be focused on today', 'catch me up on my work', 'how does my day look'. "
            "Returns, in one call: their role and lanes (from the org role registry), "
            "their open Asana tasks (entity-scoped to this channel), today + tomorrow "
            "calendar, and their open deals if they own a sales pipeline. Present the "
            "returned sections in order and preserve any `<url|name>` Slack links "
            "verbatim. This tool shows the asker their OWN plate only — for another "
            "teammate's plate it refuses unless the asker is Harrison (the optional "
            "`person` parameter is Harrison-only; for just a teammate's open Asana tasks, "
            "anyone can use asana_get_user_tasks instead). Users without an org-role "
            "registry entry get a graceful no-data response."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "person": {
                    "type": "string",
                    "description": (
                        "HARRISON-ONLY. Name of a teammate whose plate Harrison wants to "
                        "see (first name, full name, or alias). Omit for the asking "
                        "user's own plate — which is the only valid use for everyone else."
                    ),
                },
            },
            "required": [],
        },
    },
    # --- Personal notes (Org Synthesis Phase 5) ---
    {
        "name": "cora_remember",
        "description": (
            "Save a PERSONAL NOTE for the asking user. Use this whenever the user "
            "teaches Cora something or asks her to remember a fact — phrases like "
            "'Cora, remember ...', 'note that ...', 'keep track of ...', 'this is the "
            "<X> we use for ...', 'save this for later'. Notes are PRIVATE: only the "
            "person who saved a note can ever retrieve it, and Cora automatically "
            "surfaces a user's own notes when they ask a related question later.\n"
            "\n"
            "REQUIRED PATTERN (staged-write — never skip):\n"
            "1. On the first ask, show a preview: \"Saving to YOUR notes (only you can "
            "   retrieve this): <note text>\" and ask them to confirm. DO NOT call the "
            "   tool yet.\n"
            "2. On their explicit yes, call with confirmed=true.\n"
            "3. If they want changes, re-show the preview. If they cancel, don't call.\n"
            "\n"
            "If the user signals they want it shared org-wide ('make sure everyone can "
            "find it', 'the team should know this'), still save it — set "
            "share_requested=true — and tell them org-wide sharing goes through "
            "Harrison's review. NEVER refuse to accept knowledge: the right response is "
            "\"I'll save that to your notes; org-wide sharing needs Harrison's review.\"\n"
            "\n"
            "The result may include a heads-up that the note conflicts with existing "
            "org knowledge — relay it to the user verbatim; the note is still saved.\n"
            "\n"
            "PHI: in a Lexington channel, do NOT promise to save or show a preview for "
            "a note about a specific individual's health, billing, authorization, "
            "eligibility, or client status — call this tool and let the gate decide. It "
            "refuses unless the saver is an authorized LEX custodian, and relaying its "
            "refusal verbatim is the correct response."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "note_text": {
                    "type": "string",
                    "description": "The note to save, as a self-contained statement (it will be retrieved out of this conversation's context later).",
                },
                "share_requested": {
                    "type": "boolean",
                    "description": "Set true ONLY when the user explicitly asked for the note to be shared org-wide / findable by everyone.",
                },
                "confirmed": {
                    "type": "boolean",
                    "description": "Must be true. Only set after showing the user the save preview and receiving explicit approval.",
                },
            },
            "required": ["note_text", "confirmed"],
        },
    },
    {
        "name": "cora_my_notes",
        "description": (
            "List the asking user's own saved personal notes with dates and ids. Use "
            "when the user asks 'show my notes', 'what notes do I have', 'what have I "
            "asked you to remember', or before deleting a note (to find its id). "
            "Owner-only: this never shows anyone else's notes, and another user's "
            "notes can never appear here."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "cora_forget_note",
        "description": (
            "Delete ONE of the asking user's own personal notes. Use when the user "
            "says 'forget that note', 'delete my note about X', 'remove that'. "
            "REQUIRED PATTERN (staged-write): first call cora_my_notes to find the "
            "note, show the user WHICH note will be deleted, and only after their "
            "explicit yes call this with confirmed=true and the note's id. Owner-only: "
            "a user can only delete their own notes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "note_id": {
                    "type": "string",
                    "description": "The note id from cora_my_notes (the short token in [brackets], or the full note:... id).",
                },
                "confirmed": {
                    "type": "boolean",
                    "description": "Must be true. Only set after showing the user which note will be deleted and receiving explicit approval.",
                },
            },
            "required": ["note_id", "confirmed"],
        },
    },
    {
        "name": "meeting_action_items",
        "description": (
            "Pull a meeting's summary + the action items meant for the ASKING USER, "
            "then (only after they confirm) create the chosen ones as Asana tasks "
            "assigned to them. Use this when someone asks 'what were my action items "
            "from <meeting>?', 'summarize the <meeting> and what I need to do', "
            "'recap my to-dos from yesterday's call', etc. Cora does NOT auto-create "
            "tasks from meetings -- this tool is how a meeting attendee turns their "
            "own items into tasks, on request.\n"
            "\n"
            "TWO-STEP PROTOCOL (never skip):\n"
            "1. PREVIEW (read-only): call WITHOUT confirmed, passing meeting_query "
            "(the title/keywords/date the user gave). The result is a summary + the "
            "user's numbered action items + a hidden transcript_id and instructions. "
            "Show the user the summary and items, then ASK which they want created. "
            "Do NOT call again until they answer.\n"
            "   - If the result is a numbered PICK-LIST (the hint matched several "
            "meetings, or the user gave no hint), show the titles+dates and ask which "
            "they mean, then call again with transcript_id set to that meeting's id "
            "from the [id:...] tag (still WITHOUT confirmed -- that just loads it).\n"
            "2. CREATE (staged write): once the user picks items, call again with "
            "confirmed=true, transcript_id (from step 1), and selected_items set to "
            "the EXACT task texts they chose. The tool creates them assigned to the "
            "user and returns a confirmation to relay verbatim. If they want none, "
            "create nothing.\n"
            "\n"
            "The tool enforces its own safety: it only works for meetings the asker "
            "ATTENDED, only surfaces a meeting where it belongs (Lexington meetings "
            "only in Lexington channels; an entity's meeting in that entity's / a "
            "founder channel / a DM), and PHI-scrubs Lexington content. If it refuses, "
            "relay the refusal -- don't try to work around it.\n"
            "\n"
            "RELAY THE TOOL'S RESULT -- IT IS THE ANSWER. This tool is the ONLY "
            "source of which meetings the user attended and what was assigned to them. "
            "When it returns a pick-list, a refusal, or a 'couldn't find a meeting...' "
            "message, relay THAT (and ask the user to pick if it's a list). Do NOT "
            "consult the calendar, your knowledge base, or memory for this question, "
            "and NEVER invent or guess a meeting date, attendee, or 'your last "
            "meeting was on ...' -- if the tool didn't return it, you don't know it. "
            "On a pick-list follow-up, call again carrying BOTH the title and the "
            "user's date/position (e.g. meeting_query='Lexington Progress June 18' or "
            "'Lexington Progress, the first one'), or the [id:...] from the list."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "meeting_query": {
                    "type": "string",
                    "description": "The user's hint for which meeting -- a title, keywords, and/or a date or pick-list selection. Accepts a title ('F3 marketing sync'), a date ('June 18', '6/18', 'today', 'yesterday', 'the 18th'), a position ('the first one', 'last one'), or a combination ('Lexington Progress June 18'). When the user is choosing from a pick-list, carry their original title forward together with the date/position. Omit to get a list of the user's recent meetings to choose from.",
                },
                "transcript_id": {
                    "type": "string",
                    "description": "The meeting's id from a prior PREVIEW result or pick-list ([id:...]). REQUIRED on the confirmed create call. Never shown to the user.",
                },
                "confirmed": {
                    "type": "boolean",
                    "description": "Set true ONLY on the create call, after the user has seen the items and chosen which to create. Requires transcript_id + selected_items.",
                },
                "selected_items": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "On the confirmed create call: the exact task texts the user chose, copied verbatim from the PREVIEW list.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "cora_self_check",
        "description": (
            "Report Cora's REAL operational status from LIVE signals: heartbeat "
            "freshness, knowledge-base size, and last sync times. Call this for "
            "ANY question about Cora's own state -- 'are you working?', 'what's "
            "your status?', 'is the KB up to date?', 'diagnose yourself', 'is "
            "everything healthy?'. CRITICAL: this tool is the ONLY source of "
            "Cora's status. Do NOT answer such questions from the knowledge base "
            "or memory -- the KB must never be used to narrate Cora's own build, "
            "audit, or 'diagnostic' notes as fact. Relay the tool's output."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "cora_person_dossier",
        "description": (
            "Pull a teammate's recent WORK involvement -- a founder check-in or a "
            "self check-in. Use for phrases like 'what has <person> been working on "
            "(lately/this week)?', 'check in on <person>', '<person>'s recent "
            "involvement', or self: 'what have I been working on?', 'what have I been "
            "involved in lately?'. Returns a synthesized 'Recent involvements' summary "
            "from the person's email, meetings, tasks, deals, and calendar (last 14 "
            "days by default). "
            "ACCESS (enforced in code, do not second-guess): ONLY Harrison may pass a "
            "`person`; everyone else gets THEIR OWN involvement only -- a request to "
            "profile a teammate from a non-founder is refused. Omit `person` for a "
            "self check-in. Work-involvement only; never personal life; LEX staff are "
            "PHI-walled. The result is private to Harrison + that person -- never repost "
            "it to a channel."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "person": {
                    "type": "string",
                    "description": (
                        "The teammate to check in on, by name (Harrison only). Omit "
                        "entirely for a self check-in (the asker's own involvement)."
                    ),
                },
                "days": {
                    "type": "integer",
                    "description": "Lookback window in days (default 14, max 30).",
                },
            },
            "required": [],
        },
    },
    {
        "name": "personal_oneamerica_portfolio",
        "description": (
            "Harrison's personal whole-life insurance portfolio: total death benefit, "
            "total cash value, outstanding policy loans (and how many are near their "
            "borrowing limit), per-policy flags (e.g. a premium paid-to date in the "
            "past), and premiums due in the next 30 days. Use when Harrison asks about "
            "his whole-life policies, insurance portfolio, cash value, policy loans, or "
            "premium schedule. Private -- only reachable in Harrison's DM (it refuses "
            "everywhere else). Pass detail=true for a per-policy breakdown."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "detail": {
                    "type": "boolean",
                    "description": "Include a per-policy breakdown (default false: summary only).",
                },
            },
            "required": [],
        },
    },
    {
        "name": "personal_capital_program_state",
        "description": (
            "The F3 capital program / raise tracker: locked deal terms (raise amount, "
            "post-money valuation, price per share, ambassador/operator pools, recap, cap "
            "table) plus -- once synced from the dashboard -- calc outputs, candidate "
            "pipeline, and phase/legal/tracker status. Use when Harrison asks about the "
            "capital program, the raise, the equity program, investor/candidate pipeline, "
            "or deal terms. HIGHLY CONFIDENTIAL -- only reachable in Harrison's DM (it "
            "refuses everywhere else)."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "f3e_creator_crm",
        "description": (
            "F3's creator & ambassador CRM: roster counts by program / stage / tier, "
            "follow-ups due, top creators by sales driven (GMV), and recent activity. Use "
            "when someone asks about the creator roster, ambassadors, influencers, the "
            "sponsorship pipeline, or a specific creator's status. Pass person=<name> to "
            "look up one creator. Available in F3 creator/leadership channels, founder "
            "channels, and Harrison's DM (it refuses elsewhere)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "person": {
                    "type": "string",
                    "description": "Optional: a creator's name to look up their single record.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "fndr_content_pipeline",
        "description": (
            "The founder content & freelancer pipeline: deliverables by priority (overdue "
            "/ due this week / unassigned), this week's content calendar slots, campaign "
            "statuses, budget planned-vs-actual by bucket, and the events-sponsorship "
            "pipeline by stage. Use when Harrison asks what's overdue or due in content, "
            "the content pipeline, freelancer deliverables, the marketing calendar, "
            "campaign or content-budget status, or the events pipeline. Founder-only "
            "(refuses outside founder channels + Harrison's DM)."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# Per-entity tool exposure
# ─────────────────────────────────────────────────────────────────────────────
# Which TOOL_DEFINITIONS are offered to Claude for a given channel entity. This
# is a PERFORMANCE + tool-selection optimization, NOT a security boundary: the
# cross-entity guard (cross_entity_guard.py, pre-LLM) and each tool's own runtime
# guardrails (financial tier gate, LEX PHI/HubSpot block, staged-write confirm)
# remain the security layer. So this map errs INCLUSIVE — a tool missing here only
# means the model isn't offered it; an over-broad entry is harmless (just tokens).
#
# Sending only the channel-entity's tools (a) shrinks the per-call tool-schema
# block (~14.5K tok for all 48) — the biggest savings land on the lean entities
# (F3C/HJRPROD ship ~11 core tools, not 48), and (b) narrows the model's
# tool-selection space so it mis-picks less and burns fewer wasted iterations.
#
# Aggregators (FNDR, HJRG) and the founder from ANY channel get the full set —
# they ask cross-entity questions by design.

# Tools every channel gets: task/calendar/comms/cashflow + the portfolio
# decisions queue (referenced by the OSN/LEX/HJRP prompts, not FNDR-only).
_GLOBAL_CORE_TOOLS: frozenset[str] = frozenset({
    "whats_on_my_plate",
    "meeting_action_items",
    "cora_remember",
    "cora_my_notes",
    "cora_forget_note",
    "asana_get_my_tasks",
    "asana_get_user_tasks",
    "asana_create_task",
    "asana_complete_task",
    "asana_delete_task",
    "gmail_create_draft",
    "gmail_inbox",
    "calendar_get_my_events",
    "calendar_create_event",
    "calendar_delete_event",
    "calendar_schedule_meeting",
    "slack_send_dm",
    "financial_get_cashflow",
    "fndr_open_decisions",
    "cora_self_check",
    "cora_person_dossier",
})

# QBO + Drive close-pack financial depth — only entities that have QBO
# provisioned (BDM/F3E/HJRG/HJRP/LEX/OSN). UFL/F3C/HJRPROD are not provisioned;
# they keep financial_get_cashflow (in core) for the weekly forecast.
_FINANCIAL_TOOLS: frozenset[str] = frozenset({
    "qbo_get_profit_loss",
    "qbo_get_balance_sheet",
    "qbo_get_ar_aging",
    "qbo_get_ap_aging",
    "qbo_get_recent_transactions",
    "financial_get_pulse",
    "financial_get_close_pack",
})

# HubSpot deal tools — sales entities only. LEX is intentionally excluded
# (HubSpot blocked for LEX per the Tier-1 doctrine).
_HUBSPOT_TOOLS: frozenset[str] = frozenset({
    "hubspot_get_my_deals",
    "hubspot_update_deal_stage",
    "hubspot_add_note",
})

# F3 brand image / deck generation — F3E + BDM (the in-house media agency).
_F3_IMAGE_TOOLS: frozenset[str] = frozenset({
    "f3_generate_image",
    "f3_batch_image_run",
    "f3_create_image",
    "f3_create_sales_deck",
})

# Extra tools per entity, BEYOND _GLOBAL_CORE_TOOLS.
_ENTITY_TOOLS: dict[str, frozenset[str]] = {
    "F3E": _FINANCIAL_TOOLS | _HUBSPOT_TOOLS | _F3_IMAGE_TOOLS | frozenset({
        "f3e_creator_crm",
        "f3e_shopify_sales_pulse",
        "f3e_shopify_inventory",
        "f3e_shopify_set_inventory",
        "f3e_inventory_pulse",
        "f3e_inventory_by_location",
        "f3e_brand_voice_check",
        "f3e_hubspot_pipeline_summary",
        "f3e_ai_visibility",
        "ads_get_performance_summary",
        "ads_get_channel_breakdown",
        "ads_get_subbrand_performance",
        "ads_get_pixel_attribution",
        "ads_get_cm_waterfall",
        "fighter_compliance",
        "influencer_list_handles",
        "influencer_add_handle",
        "influencer_get_status",
        "influencer_log_deliverable",
    }),
    "OSN": _FINANCIAL_TOOLS | _HUBSPOT_TOOLS | frozenset({"osn_financial_pulse"}),
    "LEX": _FINANCIAL_TOOLS | frozenset({"lex_revalidation_status"}),
    "HJRP": _FINANCIAL_TOOLS | frozenset({"hjrp_lease_status"}),
    "BDM": _FINANCIAL_TOOLS | _HUBSPOT_TOOLS | _F3_IMAGE_TOOLS,
    "UFL": _HUBSPOT_TOOLS,  # sponsor pipeline; no QBO provisioned
    "F3C": frozenset(),       # nonprofit — core only
    "HJRPROD": frozenset(),   # personal-brand umbrella — core only
}

# Sub-entity channels resolve to their parent's tool set.
_SUBENTITY_PARENT: dict[str, str] = {
    "OSNGF": "OSN", "OSNGM": "OSN", "OSNGW": "OSN", "OSNVV": "OSN",
    "LEX-LLC": "LEX", "LEX-LTS": "LEX", "LEX-LBHS": "LEX", "LEX-LLA": "LEX",
    "HJRP-1337": "HJRP", "HJRP-1555": "HJRP", "HJRP-RR": "HJRP",
    "HJRP-CL": "HJRP", "HJRP-LCI": "HJRP",
}

# Aggregators see every tool — they ask cross-entity questions by design.
_FULL_ACCESS_ENTITIES: frozenset[str] = frozenset({"FNDR", "HJRG"})


def tools_for_entity(entity: str, cross_entity: bool = False) -> list[dict]:
    """Return the TOOL_DEFINITIONS subset to offer Claude for this channel.

    Order is preserved from TOOL_DEFINITIONS so the cached tools block has a
    stable per-entity cache key.

    cross_entity=True (the founder asking from any channel) or an aggregator
    entity (FNDR/HJRG) gets the full set. An unknown entity falls back to the
    global core only — safe, never a crash.
    """
    # WS-3 eval isolation (lens R3): the offline eval harness
    # (scripts/run_kb_evals.py) must be side-effect-free -- no tool can be
    # OFFERED to Claude in eval mode, so no staged write / connector call can
    # ever execute from eval traffic. Read at call time; the bot process never
    # sets this env var. dispatch() carries the belt-and-braces refusal.
    if os.environ.get("CORA_EVAL_MODE") == "1":
        return []
    if cross_entity or entity in _FULL_ACCESS_ENTITIES:
        return list(TOOL_DEFINITIONS)
    canon = _SUBENTITY_PARENT.get(entity, entity)
    allowed = _GLOBAL_CORE_TOOLS | _ENTITY_TOOLS.get(canon, frozenset())
    return [t for t in TOOL_DEFINITIONS if t["name"] in allowed]


# Name -> callable. The callable takes (slack_user_id, entity, input_dict) and returns a string.
_TOOL_FUNCTIONS: dict[str, Callable[[str, str, dict], str]] = {
    "asana_get_my_tasks": _tool_get_my_tasks,
    "asana_get_user_tasks": _tool_get_user_tasks,
    "asana_create_task": _tool_asana_create_task,
    "asana_complete_task": _tool_asana_complete_task,
    "asana_delete_task": _tool_asana_delete_task,
    "gmail_create_draft": _tool_gmail_create_draft,
    "gmail_inbox": _tool_gmail_inbox,
    "calendar_get_my_events": _tool_get_my_events,
    "calendar_create_event": _tool_calendar_create_event,
    "calendar_delete_event": _tool_calendar_delete_event,
    "calendar_schedule_meeting": _tool_calendar_schedule_meeting,
    "fighter_compliance": _tool_fighter_compliance,
    "influencer_list_handles": _tool_influencer_list_handles,
    "influencer_add_handle": _tool_influencer_add_handle,
    "influencer_get_status": _tool_influencer_get_status,
    "influencer_log_deliverable": _tool_influencer_log_deliverable,
    "hubspot_get_my_deals": _tool_get_my_deals,
    "qbo_get_profit_loss": _tool_qbo_get_profit_loss,
    "qbo_get_balance_sheet": _tool_qbo_get_balance_sheet,
    "qbo_get_ar_aging": _tool_qbo_get_ar_aging,
    "qbo_get_ap_aging": _tool_qbo_get_ap_aging,
    "qbo_get_recent_transactions": _tool_qbo_get_recent_transactions,
    "financial_get_cashflow": _tool_financial_get_cashflow,
    "financial_get_pulse": _tool_financial_get_pulse,
    "financial_get_close_pack": _tool_financial_get_close_pack,
    "fndr_completion_candidates": _tool_fndr_completion_candidates,
    "fndr_open_decisions": _tool_fndr_open_decisions,
    "f3e_shopify_sales_pulse": _tool_f3e_shopify_sales_pulse,
    "f3e_shopify_inventory": _tool_f3e_shopify_inventory,
    "f3e_shopify_set_inventory": _tool_f3e_shopify_set_inventory,
    "f3e_inventory_pulse": _tool_f3e_inventory_pulse,
    "f3e_inventory_by_location": _tool_f3e_inventory_by_location,
    "f3e_brand_voice_check": _tool_f3e_brand_voice_check,
    "f3e_hubspot_pipeline_summary": _tool_f3e_hubspot_pipeline_summary,
    "f3e_ai_visibility": _tool_f3e_ai_visibility,
    "fndr_contracts_dashboard": _tool_fndr_contracts_dashboard,
    "fndr_press_pipeline_summary": _tool_fndr_press_pipeline_summary,
    "osn_financial_pulse": _tool_osn_financial_pulse,
    "ads_get_performance_summary": _tool_ads_get_performance_summary,
    "ads_get_channel_breakdown": _tool_ads_get_channel_breakdown,
    "ads_get_subbrand_performance": _tool_ads_get_subbrand_performance,
    "ads_get_pixel_attribution": _tool_ads_get_pixel_attribution,
    "ads_get_cm_waterfall": _tool_ads_get_cm_waterfall,
    # PhotoRoom image generation + sales deck
    "f3_generate_image": _tool_f3_generate_image,
    "f3_batch_image_run": _tool_f3_batch_image_run,
    "f3_create_image": _tool_f3_create_image,
    "f3_create_sales_deck": _tool_f3_create_sales_deck,
    # LEX tools
    "lex_revalidation_status": _tool_lex_revalidation_status,
    # HJRP tools
    "hjrp_lease_status": _tool_hjrp_lease_status,
    # HubSpot two-way write tools
    "hubspot_update_deal_stage": _tool_hubspot_update_deal_stage,
    "hubspot_add_note": _tool_hubspot_add_note,
    # Cross-entity write tools
    "slack_send_dm": _tool_slack_send_dm,
    # Org Synthesis Phase 2: role-scoped composite plate view
    "whats_on_my_plate": _tool_whats_on_my_plate,
    # Org Synthesis Phase 5: personal notes (owner-only, blast-radius-1)
    "cora_remember": _tool_cora_remember,
    "cora_my_notes": _tool_cora_my_notes,
    "cora_forget_note": _tool_cora_forget_note,
    # Read-only operational self-status (heartbeat + KB size + sync watermarks)
    "cora_self_check": _tool_cora_self_check,
    # Per-person involvement dossier (founder-or-self; North Star pillar 4)
    "cora_person_dossier": _tool_cora_person_dossier,
    # Meeting action items -- PULL flow (replaces the retired auto-create push)
    "meeting_action_items": _tool_meeting_action_items,
    # Dashboard read layer (2026-07-11): read-only, each gates on dashboard_access.
    "personal_oneamerica_portfolio": _tool_personal_oneamerica_portfolio,
    "personal_capital_program_state": _tool_personal_capital_program_state,
    "f3e_creator_crm": _tool_f3e_creator_crm,
    "fndr_content_pipeline": _tool_fndr_content_pipeline,
}


# Per-tool timeout overrides (seconds). THIS DICT IS THE SOURCE OF TRUTH for tool
# timeouts — any doctrine text (decisions.md D-014, repo CLAUDE.md) is a pointer here.
# Six tiers in use (superseding the older 8/15/25 three-tier note): 8s fast (local
# DB / single quick call) · 12s normal (single external API) · 15s default (finance
# / QBO reads) · 20s heavy (uploads, DM, drafts) · 25s heaviest (image/deck/meeting
# parse / multi-source composite) · plus per-tool overrides (e.g. cora_person_dossier
# = 60s for its Sonnet-dominated tail). Default when unlisted = _DEFAULT_TOOL_TIMEOUT (15s).
_TOOL_TIMEOUTS: dict[str, int] = {
    # Fast — local DB or single quick API call
    "asana_get_my_tasks": 8,
    "asana_get_user_tasks": 8,
    "fndr_open_decisions": 8,
    "fndr_completion_candidates": 8,
    "hjrp_lease_status": 8,
    "osn_financial_pulse": 8,
    "hubspot_get_my_deals": 8,
    "calendar_get_my_events": 8,
    "influencer_list_handles": 8,
    "influencer_get_status": 8,
    "f3e_ai_visibility": 8,
    # Normal — single external API call
    "asana_create_task": 12,
    "asana_complete_task": 12,
    "asana_delete_task": 12,
    "f3e_hubspot_pipeline_summary": 12,
    "fndr_contracts_dashboard": 12,
    "fndr_press_pipeline_summary": 12,
    "lex_revalidation_status": 12,
    "f3e_shopify_sales_pulse": 12,
    "f3e_shopify_inventory": 12,
    "f3e_inventory_pulse": 12,
    "f3e_inventory_by_location": 12,
    "financial_get_cashflow": 15,
    "financial_get_pulse": 15,
    "financial_get_close_pack": 15,
    "qbo_get_profit_loss": 15,
    "qbo_get_balance_sheet": 15,
    "qbo_get_ar_aging": 15,
    "qbo_get_ap_aging": 15,
    "qbo_get_recent_transactions": 15,
    # Heavy — multi-step, slow uploads, or long-running APIs
    "gmail_create_draft": 20,
    "calendar_create_event": 20,
    "calendar_delete_event": 20,
    # DTC inventory write (D-028 / D-051): the confirmed phase makes up to four
    # SEQUENTIAL Shopify calls, each with its own 15s per-request budget --
    # get_active_locations + resolve_variants (paginated products.json) +
    # get_inventory_level + set_inventory_level. The dispatch timeout MUST exceed
    # that worst-case sum, or a slow write is abandoned mid-flight and reported as
    # "didn't go through" AFTER the POST actually landed (the f3_generate_image
    # lesson). 15*4 + a pagination page = ~75s. Self-healing on retry (the
    # optimistic-concurrency guard re-previews), but the false-failure message is
    # the defect this closes.
    "f3e_shopify_set_inventory": 75,
    "calendar_schedule_meeting": 25,
    # Image generation (W3-02): the dispatch timeout MUST exceed the tool's
    # internal httpx budgets, else a real generation is abandoned mid-flight and
    # the user gets a spurious "Tool timed out" (W3-01 made this timeout a true
    # wall-clock bound). photoroom_client budgets are additive within one spec:
    # main-image download 30s (:177) + optional reference-image download 30s +
    # generation POST 60s (:254) -> a single spec runs up to ~90s in the common
    # path (fast downloads + the 60s generation ceiling).
    "f3_generate_image": 90,    # PhotoRoom download(s) + 60s generation ceiling
    "f3_create_image": 100,     # + Haiku brief->spec (spec_generator) before PhotoRoom
    "f3_batch_image_run": 180,  # N specs in SERIES; a large batch exceeds this and
                                # finishes in the background (W3-01 abandons the worker
                                # on timeout, but images still land in Drive) -- the
                                # true fix for large batches is async fire-and-report
                                # (tracked, out of this slice's scope).
    "f3_create_sales_deck": 25,
    "influencer_log_deliverable": 20,
    "hubspot_update_deal_stage": 20,
    "hubspot_add_note": 20,
    "slack_send_dm": 12,
    "whats_on_my_plate": 25,  # multi-source composite (Asana + Calendar x2 + HubSpot)
    "meeting_action_items": 25,  # Fireflies window fetch + Haiku parse (preview); bounded creates (confirm)
    "cora_person_dossier": 60,  # concurrent pulls (~7s) + internal Sonnet synth (the variable long pole, ~25-31s). Live smokes: 38s sequential (timed out at 25s) -> 38s with the 45s budget (worked but thin) -> 60s gives tail-latency headroom over the Sonnet-dominated cost. A timeout is recoverable anyway (orphaned worker still writes the dossier; retry succeeds).
    # Personal notes: remember = embed + conflict probe + upsert (default 15s
    # tier is right); list/delete are local SQL.
    "cora_remember": 15,
    "cora_my_notes": 8,
    "cora_forget_note": 8,
    "cora_self_check": 8,
    # Dashboard read layer: Drive/Airtable network reads.
    "personal_oneamerica_portfolio": 20,   # Drive JSON download
    "personal_capital_program_state": 15,  # folder list + newest JSON download
    "f3e_creator_crm": 15,                 # 2 Airtable list calls
    "fndr_content_pipeline": 20,           # 5 sequential Airtable list calls
}
_DEFAULT_TOOL_TIMEOUT = 15


def dispatch(
    tool_name: str,
    tool_input: dict[str, Any],
    slack_user_id: str,
    entity: str = "FNDR",
    channel_name: str = "",
    channel_id: str = "",
    thread_ts: str | None = None,
) -> str:
    """Run a tool by name. Always returns a string for tool_result content.

    entity is the routed entity code for the channel the @mention came from
    (F3E, LEX, OSN, BDM, FNDR, etc.) -- tools may use this to scope results.

    channel_name is injected into tool_input as '_channel_name' so financial
    tools can enforce the finance-channel access rule without changing signatures.

    channel_id and thread_ts are injected for Feature 6 file uploads and any
    future tools that need to post directly to Slack channels.
    """
    # WS-3 eval isolation, belt-and-braces (lens R3): tools_for_entity already
    # offers NO tools in eval mode, so this only fires if a code path bypasses
    # the offer gate. Never executes a tool from eval traffic.
    if os.environ.get("CORA_EVAL_MODE") == "1":
        log.warning("dispatch refused in eval mode: %s", tool_name)
        return "Tool use is disabled in eval mode."
    fn = _TOOL_FUNCTIONS.get(tool_name)
    if not fn:
        log.warning("Unknown tool name requested by model: %s", tool_name)
        return f"Unknown tool: {tool_name}. Available tools: {list(_TOOL_FUNCTIONS)}"
    injected = dict(tool_input or {})
    injected["_channel_name"] = channel_name
    if channel_id:
        injected["_channel_id"] = channel_id
    if thread_ts:
        injected["_thread_ts"] = thread_ts
    try:
        timeout = _TOOL_TIMEOUTS.get(tool_name, _DEFAULT_TOOL_TIMEOUT)
        # W3-01: do NOT use the ThreadPoolExecutor context manager -- its __exit__
        # runs shutdown(wait=True), which BLOCKS until the worker finishes, so a
        # hung tool defeats its own future.result(timeout=...) and the dispatch
        # wall-clock becomes unbounded. Manage the executor manually and shut it
        # down non-blocking in a finally: wait=False abandons a still-running
        # worker (Python cannot force-kill a thread), cancel_futures cancels any
        # not-yet-started work. The success path is unchanged -- the worker is
        # already done, so there is nothing to wait for. Tradeoff: a genuinely
        # hung tool leaks one worker thread until it returns on its own, which is
        # far better than blocking every request and is cleared on the next
        # supervisor restart.
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(fn, slack_user_id, entity, injected)
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            log.warning("Tool %s timed out after %ds for user=%s entity=%s", tool_name, timeout, slack_user_id, entity)
            return "Tool timed out — please try again."
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
    except Exception as exc:
        log.exception("Tool %s raised unexpected error", tool_name)
        return f"Tool {tool_name} crashed: {exc}. Apologize to the user and continue."
