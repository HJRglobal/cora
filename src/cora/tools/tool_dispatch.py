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

from . import asana_client, calendar_client, gmail_client, hubspot_client, qbo_client
from ..connectors import qbo_oauth

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
]


# Name → callable. The callable takes (slack_user_id, entity, input_dict) and returns a string.
_TOOL_FUNCTIONS: dict[str, Callable[[str, str, dict], str]] = {
    "asana_get_my_tasks": _tool_get_my_tasks,
    "asana_get_user_tasks": _tool_get_user_tasks,
    "asana_create_task": _tool_asana_create_task,
    "gmail_create_draft": _tool_gmail_create_draft,
    "hubspot_get_my_deals": _tool_get_my_deals,
    "calendar_get_my_events": _tool_get_my_events,
    "calendar_create_event": _tool_calendar_create_event,
    "qbo_get_profit_loss": _tool_qbo_get_profit_loss,
    "qbo_get_balance_sheet": _tool_qbo_get_balance_sheet,
    "qbo_get_ar_aging": _tool_qbo_get_ar_aging,
    "qbo_get_ap_aging": _tool_qbo_get_ap_aging,
    "qbo_get_recent_transactions": _tool_qbo_get_recent_transactions,
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
