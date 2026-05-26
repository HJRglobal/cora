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

from . import ads_client, asana_client, brand_voice_client, calendar_client, financial_client, gmail_client, hubspot_client, influencer_client, inventory_client, qbo_client
from ..connectors import clover_client, qbo_oauth, shopify_client

log = logging.getLogger(__name__)

# Path to slack→tool mappings. Resolves relative to repo root (parent of src/).
_REPO_ROOT = Path(__file__).resolve().parents[3]
_MAP_PATH = _REPO_ROOT / "data" / "maps" / "slack-to-asana.yaml"
_HUBSPOT_MAP_PATH = _REPO_ROOT / "data" / "maps" / "slack-to-hubspot.yaml"
_ALIASES_PATH = _REPO_ROOT / "data" / "maps" / "user-aliases.yaml"
_HIERARCHY_PATH = _REPO_ROOT / "data" / "maps" / "supervisor-hierarchy.yaml"

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
    if not name or not name.strip():
        return None, None

    needle = name.strip().lower()
    slack_asana_map = _load_slack_asana_map()
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

    # 3. Substring match on display_name (fallback for un-aliased nicknames)
    substring_hits = [u for key, u in by_display.items() if needle in key]
    if len(substring_hits) == 1:
        user = substring_hits[0]
        return user["slack_user_id"], user.get("display_name")
    if len(substring_hits) > 1:
        names = [u.get("display_name", "?") for u in substring_hits]
        log.info("Substring lookup for %r ambiguous, matches: %s", name, names)
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

    # The confirmation gate
    confirmed = input_data.get("confirmed", False)
    if confirmed is not True:
        return (
            "asana_create_task refused: `confirmed` must be set to true ONLY "
            "after you have shown the user a preview block (title, assignee, "
            "project, due date, notes) AND received their explicit approval "
            "in their next message ('yes', 'approve', 'create it', or similar). "
            "If you have NOT done that yet, do it now: format a clear preview "
            "and ask the user to confirm. If you HAVE shown a preview and the "
            "user approved, call this tool again with confirmed=true."
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
    project_gid = (input_data.get("project_gid") or "").strip() or None
    notes = input_data.get("notes") or None
    due_on = (input_data.get("due_on") or "").strip() or None

    try:
        created = asana_client.create_task(
            name=title,
            assignee_gid=assignee_gid,
            project_gid=project_gid,
            notes=notes,
            due_on=due_on,
        )
    except asana_client.AsanaClientError as exc:
        log.warning(
            "asana_create_task FAILED asker=%s title=%r assignee=%s exc=%s",
            slack_user_id, title, assignee_gid, exc,
        )
        return (
            f"Asana create_task error: {exc}. Tell the user the task wasn't created. "
            f"If the error mentions an invalid project or assignee, suggest they check "
            f"the details and try again."
        )

    log.info(
        "asana_create_task CREATED asker=%s title=%r assignee=%s task_gid=%s permalink=%s",
        slack_user_id,
        title,
        assignee_display,
        created.get("gid", ""),
        created.get("permalink_url", ""),
    )

    return asana_client.format_created_task_for_llm(created)


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


def _tool_calendar_create_event(slack_user_id: str, entity: str, _input: dict) -> str:
    """Create a Calendar event in the asker's own primary calendar.

    Cora's third write tool. Same staged-write doctrine as asana_create_task
    and gmail_create_draft: refuses without confirmed=True; tool description
    instructs Claude to show a preview block and get explicit user approval first.

    The event is created via DWD impersonation AS the asker, so it lands in
    their own Google Calendar. Attendees receive Google invitations automatically
    when sendUpdates='all' (the default).
    """
    input_data = _input or {}

    # Confirmation gate — same pattern as the other write tools
    confirmed = input_data.get("confirmed", False)
    if confirmed is not True:
        return (
            "calendar_create_event refused: `confirmed` must be set to true ONLY "
            "after you have shown the user a clear preview block (title, start, end, "
            "attendees, description, location) AND received their explicit approval "
            "('yes', 'create it', 'add it', 'looks good', or similar). "
            "If you have NOT done that yet, format a preview NOW and wait for confirmation."
        )

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

    # Resolve caller's Google identity from the Slack→Asana map
    asker = _load_slack_asana_map().get(slack_user_id)
    if not asker:
        return (
            f"calendar_create_event: Slack user {slack_user_id} is not mapped to a Google "
            f"identity. Harrison can add a row to data/maps/slack-to-asana.yaml (the "
            f"asana_email field doubles as the Google identity)."
        )
    user_email = (asker.get("asana_email") or "").strip()
    if not user_email:
        return (
            f"calendar_create_event: user {asker.get('display_name', slack_user_id)} has "
            f"no asana_email in the user map. Tell the user there's a configuration issue."
        )

    # Normalize attendees
    attendee_list: list[str] | None = None
    if attendees:
        if isinstance(attendees, str):
            attendee_list = [a.strip() for a in attendees.split(",") if a.strip()]
        elif isinstance(attendees, list):
            attendee_list = [str(a).strip() for a in attendees if str(a).strip()]

    try:
        event = calendar_client.create_event(
            user_email=user_email,
            summary=summary,
            start=start,
            end=end,
            attendees=attendee_list,
            description=description,
            location=location,
            time_zone=time_zone,
        )
    except calendar_client.CalendarClientError as exc:
        log.warning(
            "calendar_create_event FAILED asker=%s email=%s summary=%r exc=%s",
            slack_user_id, user_email, summary, exc,
        )
        return (
            f"Calendar event error: {exc}. Tell the user the event wasn't created. "
            f"If the error mentions a missing DWD scope, Harrison needs to update "
            f"Domain-wide Delegation in admin.google.com."
        )

    log.info(
        "calendar_create_event CREATED asker=%s email=%s event_id=%s summary=%r "
        "start=%s attendee_count=%d",
        slack_user_id,
        user_email,
        event.get("id", ""),
        summary,
        start,
        len(attendee_list) if attendee_list else 0,
    )

    return calendar_client.format_created_event_for_llm(event, user_email=user_email)


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
            if not deliverable_id_raw:
                return (
                    f"influencer_log_deliverable: `deliverable_id` is required for action={action}. "
                    f"Ask the user for the deliverable ID (shown in status reports as #N)."
                )
            try:
                deliverable_id = int(deliverable_id_raw)
            except (TypeError, ValueError):
                return (
                    f"influencer_log_deliverable: `deliverable_id` must be a number. "
                    f"Got {deliverable_id_raw!r}."
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
_KNOWN_QBO_CAPABLE_ENTITIES = ("HJRG", "F3E", "F3C", "BDM", "LEX", "OSN", "HJRP", "HJRPROD", "UFL")


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
    """Fetch Profit and Loss for an entity over a period. Returns a Slack-mrkdwn summary + QBO link."""
    target, err = _resolve_qbo_entity(entity, (_input or {}).get("entity"))
    if err:
        return err
    period = (_input or {}).get("period")
    start_date, end_date = qbo_client.parse_period(period)
    try:
        report = qbo_client.get_profit_loss(target, start_date, end_date)
    except qbo_client.QboClientError as exc:
        log.warning("QBO P&L tool error entity=%s: %s", target, exc)
        return f"QBO error fetching P&L for {target}: {exc}. Tell the user there's a temporary QBO issue."
    log.info("qbo_get_profit_loss entity=%s period=%s..%s", target, start_date, end_date)
    return qbo_client.format_pnl_for_llm(report, target, start_date, end_date)


def _tool_qbo_get_balance_sheet(slack_user_id: str, entity: str, _input: dict) -> str:
    """Fetch Balance Sheet snapshot for an entity as-of a date (defaults to today)."""
    target, err = _resolve_qbo_entity(entity, (_input or {}).get("entity"))
    if err:
        return err
    as_of = (_input or {}).get("as_of_date")
    try:
        report = qbo_client.get_balance_sheet(target, as_of)
    except qbo_client.QboClientError as exc:
        log.warning("QBO Balance Sheet tool error entity=%s: %s", target, exc)
        return f"QBO error fetching Balance Sheet for {target}: {exc}. Tell the user there's a temporary QBO issue."
    log.info("qbo_get_balance_sheet entity=%s as_of=%s", target, as_of or "today")
    return qbo_client.format_balance_sheet_for_llm(report, target, as_of or "today")


def _tool_qbo_get_ar_aging(slack_user_id: str, entity: str, _input: dict) -> str:
    """Fetch AR aging summary for an entity."""
    target, err = _resolve_qbo_entity(entity, (_input or {}).get("entity"))
    if err:
        return err
    try:
        report = qbo_client.get_ar_aging(target)
    except qbo_client.QboClientError as exc:
        log.warning("QBO AR Aging tool error entity=%s: %s", target, exc)
        return f"QBO error fetching AR Aging for {target}: {exc}. Tell the user there's a temporary QBO issue."
    log.info("qbo_get_ar_aging entity=%s", target)
    return qbo_client.format_ar_aging_for_llm(report, target)


def _tool_qbo_get_ap_aging(slack_user_id: str, entity: str, _input: dict) -> str:
    """Fetch AP aging summary for an entity."""
    target, err = _resolve_qbo_entity(entity, (_input or {}).get("entity"))
    if err:
        return err
    try:
        report = qbo_client.get_ap_aging(target)
    except qbo_client.QboClientError as exc:
        log.warning("QBO AP Aging tool error entity=%s: %s", target, exc)
        return f"QBO error fetching AP Aging for {target}: {exc}. Tell the user there's a temporary QBO issue."
    log.info("qbo_get_ap_aging entity=%s", target)
    return qbo_client.format_ap_aging_for_llm(report, target)


def _tool_qbo_get_recent_transactions(slack_user_id: str, entity: str, _input: dict) -> str:
    """Fetch a digest of recent Invoice / Bill / Payment activity for an entity."""
    target, err = _resolve_qbo_entity(entity, (_input or {}).get("entity"))
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
        return f"QBO error fetching recent transactions for {target}: {exc}. Tell the user there's a temporary QBO issue."
    log.info("qbo_get_recent_transactions entity=%s days=%d", target, days)
    return qbo_client.format_recent_transactions_for_llm(payload, target, days)


# --- Financial / cashflow tools ---


def _tool_financial_get_cashflow(slack_user_id: str, entity: str, _input: dict) -> str:
    """Fetch current-week cash flow from the Standing ACTUALS sheet."""
    inp = _input or {}
    entity_filter = inp.get("entity_filter") or entity or "FNDR"
    question = inp.get("question") or ""
    # entity_to_tab() inside financial_client selects the correct tab for this entity.
    # For OSN, if the question mentions distributions/partners it switches to Core4 tab.
    result = financial_client.get_cashflow_text(
        entity_filter=entity_filter,
        channel=entity,
        user=slack_user_id,
        question=question,
    )
    log.info(
        "financial_get_cashflow entity=%s entity_filter=%s result_len=%d",
        entity,
        entity_filter,
        len(result),
    )
    return result


def _tool_financial_notify_gap(slack_user_id: str, entity: str, _input: dict) -> str:
    """Post a finance data gap alert to #hjrg-finance and return the fixed response."""
    topic = (_input or {}).get("topic") or "unspecified financial question"
    return financial_client.notify_gap(
        topic=topic,
        channel=entity,
        user=slack_user_id,
    )


def _tool_osn_financial_pulse(slack_user_id: str, entity: str, _input: dict) -> str:
    """Fetch OSN store-by-store financial snapshot from the OSN Consolidated cashflow tab."""
    log.info("osn_financial_pulse user=%s entity=%s", slack_user_id, entity)
    return financial_client.get_osn_pulse_text(
        channel=entity,
        user=slack_user_id,
    )


# --- OSN Clover tools ---


def _tool_osn_sales_pulse(slack_user_id: str, entity: str, _input: dict) -> str:
    """Return OSN sales summary from Clover POS for one or all stores."""
    store = (_input.get("store") or "all").upper()
    period = _input.get("period") or "today"
    if period not in clover_client.VALID_PERIODS:
        period = "today"
    try:
        if store == "ALL":
            summaries = clover_client.get_all_stores_sales_pulse(period)
        elif store in clover_client.VALID_STORES:
            summaries = [clover_client.get_sales_pulse(store, period)]
        else:
            return (
                f"Unknown store code {store!r}. "
                f"Valid options: {', '.join(clover_client.VALID_STORES)} or 'all'."
            )
    except clover_client.CloverConfigError as exc:
        log.warning("osn_sales_pulse config error: %s", exc)
        return "I don't have that right now."
    except clover_client.CloverConnectorError as exc:
        log.warning("osn_sales_pulse connector error user=%s: %s", slack_user_id, exc)
        return "I don't have that right now."
    if not summaries:
        return "No sales data returned for that period."
    log.info("osn_sales_pulse user=%s entity=%s store=%s period=%s stores=%d",
             slack_user_id, entity, store, period, len(summaries))
    return clover_client.format_sales_for_llm(summaries, period)


def _tool_osn_inventory_status(slack_user_id: str, entity: str, _input: dict) -> str:
    """Return OSN inventory levels from Clover POS with low-stock flags."""
    store = (_input.get("store") or "all").upper()
    low_stock_only = _input.get("low_stock_only", True)
    threshold = int(_input.get("threshold") or clover_client.DEFAULT_LOW_STOCK_THRESHOLD)
    try:
        if store == "ALL":
            summaries = clover_client.get_all_stores_inventory(threshold)
        elif store in clover_client.VALID_STORES:
            summaries = [clover_client.get_inventory(store, threshold)]
        else:
            return (
                f"Unknown store code {store!r}. "
                f"Valid options: {', '.join(clover_client.VALID_STORES)} or 'all'."
            )
    except clover_client.CloverConfigError as exc:
        log.warning("osn_inventory_status config error: %s", exc)
        return "I don't have that right now."
    except clover_client.CloverConnectorError as exc:
        log.warning("osn_inventory_status connector error user=%s: %s", slack_user_id, exc)
        return "I don't have that right now."
    if not summaries:
        return "No inventory data returned."
    log.info("osn_inventory_status user=%s entity=%s store=%s low_stock_only=%s",
             slack_user_id, entity, store, low_stock_only)
    return clover_client.format_inventory_for_llm(summaries, bool(low_stock_only))


def _tool_osn_customer_trends(slack_user_id: str, entity: str, _input: dict) -> str:
    """Return OSN customer trend data from Clover POS with MoM delta."""
    store = (_input.get("store") or "all").upper()
    period = _input.get("period") or "30d"
    if period not in clover_client.VALID_PERIODS:
        period = "30d"
    try:
        if store == "ALL":
            summaries = clover_client.get_all_stores_customer_trends(period)
        elif store in clover_client.VALID_STORES:
            summaries = [clover_client.get_customer_trends(store, period)]
        else:
            return (
                f"Unknown store code {store!r}. "
                f"Valid options: {', '.join(clover_client.VALID_STORES)} or 'all'."
            )
    except clover_client.CloverConfigError as exc:
        log.warning("osn_customer_trends config error: %s", exc)
        return "I don't have that right now."
    except clover_client.CloverConnectorError as exc:
        log.warning("osn_customer_trends connector error user=%s: %s", slack_user_id, exc)
        return "I don't have that right now."
    if not summaries:
        return "No customer data returned for that period."
    log.info("osn_customer_trends user=%s entity=%s store=%s period=%s",
             slack_user_id, entity, store, period)
    return clover_client.format_customer_trends_for_llm(summaries)


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

        # Entity tag (required for filtering; absent = treat as FNDR/visible everywhere)
        entity_match = re.search(r"\*\*Entity\*\*:\s*([^\n]+)", block)
        entry_entity_raw = entity_match.group(1).strip() if entity_match else "FNDR"
        if not _entity_matches(entry_entity_raw):
            continue

        sev_match = re.search(r"\*\*Severity\*\*:\s*(P\d)", block)
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


# --- F3 brand voice check ---


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
                    "description": "Optional. Asana project GID. If omitted, task lands in the assignee's My Tasks. Cora's v1 doesn't resolve project names automatically — if the user wants a specific project, they can move the task in Asana after creation.",
                },
                "confirmed": {
                    "type": "boolean",
                    "description": "Required. Set to true ONLY after you have shown the user a preview and received explicit approval. If false or omitted, the tool refuses.",
                },
            },
            "required": ["title", "confirmed"],
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
            "IMPORTANT: You MUST show the user a clear preview block (title, start time, "
            "end time, attendees, description, location) and receive their EXPLICIT approval "
            "before setting confirmed=true. Never set confirmed=true on the first call — "
            "always preview first. "
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
                        "Required. Set to true ONLY after you have shown the user a preview "
                        "block AND received their explicit approval. If false or omitted, "
                        "the tool refuses and instructs you to show a preview first."
                    ),
                },
            },
            "required": ["summary", "start", "end", "confirmed"],
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
            "For action=complete or action=waive, you need the deliverable_id (shown as #N in "
            "status reports). If the user doesn't know the ID, call influencer_get_status first "
            "to find it, then confirm the right one with the user before completing/waiving."
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
                    "description": "Full name of the sponsored athlete / influencer. Required for action=add.",
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
                    "description": "ID of an existing deliverable. Required for action=complete and action=waive. Shown as #N in status reports.",
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
            "a date range. Use this when a user in a TIER_1 channel (any #*-finance, "
            "#*-leadership, #hjrg-*, or #fndr-* channel) asks about revenue, expenses, "
            "profitability, margin, or P&L performance — phrases like 'what's our P&L', "
            "'how much did we make last month', 'what's revenue YTD', 'profit this month'. "
            "Returns top-line section totals (Income, COGS, Net Income) plus a clickable "
            "QBO deep link to the full report. The tool defaults to the channel's entity, "
            "but the `entity` parameter can override (use it in FNDR/HJRG channels where "
            "the user names a specific entity). The `period` parameter controls the date "
            "range — defaults to last_30_days. Refuse and don't call this tool in TIER_3 "
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
            "Liabilities, Equity) plus a clickable QBO deep link to the full report. The "
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
            "aging report'. Returns aging buckets (current, 1-30, 31-60, 61-90, 91+) plus "
            "a clickable QBO deep link. The `entity` parameter overrides the channel's "
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
            "Returns aging buckets (current, 1-30, 31-60, 61-90, 91+) plus a clickable "
            "QBO deep link. The `entity` parameter overrides the channel's entity. "
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
            "type plus a clickable QBO transactions deep link. The `entity` parameter "
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
    {
        "name": "financial_notify_gap",
        "description": (
            "Post a finance data gap alert to the #hjrg-finance channel and return the "
            "standard unknown-answer response string. Call this tool when: "
            "(1) financial_get_cashflow returned the UNKNOWN_RESPONSE string, OR "
            "(2) the user asked a financial question that financial_get_cashflow cannot "
            "answer (e.g. a specific month's P&L, QBO balance sheet, or a question about "
            "data that isn't in the Standing ACTUALS sheet). "
            "The notification is throttled to one per topic per 24 hours — call it freely, "
            "the tool handles deduplication. Always return the tool's output verbatim to "
            "the user — do not rephrase or soften it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": (
                        "Short description of what the user asked for that could not be "
                        "answered. Example: 'OSN April P&L', 'QBO balance sheet for F3E', "
                        "'weekly cash flow (connector error)'. Used in the Slack alert."
                    ),
                },
            },
            "required": ["topic"],
        },
    },
    # ── FNDR-specific tools (founder / HJRG channels only) ──
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
    # ── OSN Clover POS tools (OSN channels only) ──
    {
        "name": "osn_sales_pulse",
        "description": (
            "Fetch real-time sales data for one or all OSN store locations. "
            "Use when a user asks about sales, revenue, transactions, average ticket, "
            "or refunds — phrases like 'how are sales today', 'what did we do yesterday', "
            "'show me this week's numbers', 'how much did GW bring in', 'total revenue', "
            "'transactions today', 'average sale'. "
            "Data is sourced from the point-of-sale system (clover-daily refresh cadence, "
            "cached 5 minutes). Output is source-opaque — never mention the POS platform "
            "or merchant IDs. "
            "Only call in OSN or FNDR channels. "
            "Requires TIER_1 (leadership-channel) enforcement — refuse in non-leadership "
            "channels and redirect to #osn-leadership."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "store": {
                    "type": "string",
                    "enum": ["all", "GW", "GM", "GF", "VVP"],
                    "description": (
                        "Store code: GW = Gilbert & Warner, GM = Gilbert & McKellips, "
                        "GF = Greenfield & 60, VVP = Val Vista & Pecos. "
                        "Defaults to 'all' (portfolio summary)."
                    ),
                },
                "period": {
                    "type": "string",
                    "enum": ["today", "yesterday", "7d", "30d"],
                    "description": "Time window. Defaults to 'today'.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "osn_inventory_status",
        "description": (
            "Fetch current inventory levels for one or all OSN store locations. "
            "Use when a user asks about stock, inventory, what's running low, out-of-stock "
            "items — phrases like 'what's low on stock', 'inventory status', 'what do we "
            "need to reorder', 'stock levels at GW', 'what's out', 'inventory check'. "
            "Can filter to low-stock items only (at or below threshold). "
            "Output is source-opaque. Only call in OSN or FNDR channels. "
            "Apply TIER_1 guardrail — leadership channels only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "store": {
                    "type": "string",
                    "enum": ["all", "GW", "GM", "GF", "VVP"],
                    "description": "Store code. Defaults to 'all'.",
                },
                "low_stock_only": {
                    "type": "boolean",
                    "description": (
                        "If true, return only items at or below the low-stock threshold. "
                        "Defaults to false (return all items)."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "osn_customer_trends",
        "description": (
            "Fetch customer count trends for one or all OSN store locations, comparing "
            "the current period to the equivalent prior period. "
            "Use when a user asks about customer traffic, foot traffic, new vs returning "
            "customers, customer growth — phrases like 'how many customers today', "
            "'customer count this week', 'are we getting more customers', 'foot traffic', "
            "'new customers this month', 'customer trends at VVP'. "
            "Returns current period count, prior period count, and delta. "
            "Output is source-opaque. Only call in OSN or FNDR channels. "
            "Apply TIER_1 guardrail — leadership channels only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "store": {
                    "type": "string",
                    "enum": ["all", "GW", "GM", "GF", "VVP"],
                    "description": "Store code. Defaults to 'all'.",
                },
                "period": {
                    "type": "string",
                    "enum": ["today", "yesterday", "7d", "30d"],
                    "description": "Time window for trend comparison. Defaults to 'today'.",
                },
            },
            "required": [],
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
]


# Name → callable. The callable takes (slack_user_id, entity, input_dict) and returns a string.
_TOOL_FUNCTIONS: dict[str, Callable[[str, str, dict], str]] = {
    "asana_get_my_tasks": _tool_get_my_tasks,
    "asana_get_user_tasks": _tool_get_user_tasks,
    "asana_create_task": _tool_asana_create_task,
    "gmail_create_draft": _tool_gmail_create_draft,
    "calendar_get_my_events": _tool_get_my_events,
    "calendar_create_event": _tool_calendar_create_event,
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
    "financial_notify_gap": _tool_financial_notify_gap,
    "fndr_open_decisions": _tool_fndr_open_decisions,
    "f3e_shopify_sales_pulse": _tool_f3e_shopify_sales_pulse,
    "f3e_shopify_inventory": _tool_f3e_shopify_inventory,
    "f3e_inventory_pulse": _tool_f3e_inventory_pulse,
    "f3e_brand_voice_check": _tool_f3e_brand_voice_check,
    "osn_financial_pulse": _tool_osn_financial_pulse,
    "osn_sales_pulse": _tool_osn_sales_pulse,
    "osn_inventory_status": _tool_osn_inventory_status,
    "osn_customer_trends": _tool_osn_customer_trends,
    "ads_get_performance_summary": _tool_ads_get_performance_summary,
    "ads_get_channel_breakdown": _tool_ads_get_channel_breakdown,
    "ads_get_subbrand_performance": _tool_ads_get_subbrand_performance,
    "ads_get_pixel_attribution": _tool_ads_get_pixel_attribution,
    "ads_get_cm_waterfall": _tool_ads_get_cm_waterfall,
}


def dispatch(
    tool_name: str,
    tool_input: dict[str, Any],
    slack_user_id: str,
    entity: str = "FNDR",
) -> str:
    """Run a tool by name. Always returns a string for tool_result content.

    entity is the routed entity code for the channel the @mention came from
    (F3E, LEX, OSN, BDM, FNDR, etc.) -- tools may use this to scope results.
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
