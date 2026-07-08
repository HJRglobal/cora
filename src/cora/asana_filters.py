"""Shared Asana task filters (WS12).

Asana auto-generates "system" reminder tasks (e.g. goal-update reminders) that
are assigned to users but are NOT real work. They polluted reconciliation,
the morning brief, and the plate tool because the skip filter lived only in the
hygiene-nudge script (commit 06417e7). This module is the single source of truth,
imported by every consumer (and applied at the get_user_tasks source) so a
goal-reminder can never surface as a real task anywhere.

Keep the term set TIGHT — a substring match against real task names risks false
positives, so only add phrasings Asana itself emits for system reminders.
"""

from __future__ import annotations

# Case-insensitive substring terms identifying Asana-generated system tasks.
# "it's time to update your goal" substring-matches the singular AND plural
# ("...your goals") reminder titles Asana emits. Apostrophes are normalized (see
# below), so this stores the ASCII form only.
SYSTEM_NOISE_SKIP_TERMS: frozenset[str] = frozenset({
    "it's time to update your goal",
})


def is_system_noise_task(task_name: str | None) -> bool:
    """True if a task name matches an Asana system-reminder pattern (not real work)."""
    if not task_name:
        return False
    # Normalize the curly/right-single-quote (U+2019) to ASCII — Asana renders many
    # auto-generated titles with the typographic apostrophe ("It's time..."), which
    # would otherwise slip past the ASCII-apostrophe term and fail OPEN (D-051).
    lower = task_name.lower().replace("’", "'")
    return any(term in lower for term in SYSTEM_NOISE_SKIP_TERMS)


# ---------------------------------------------------------------------------
# Entity-scoped project-prefix filtering (daily channel synthesis, 2026-07-07)
# ---------------------------------------------------------------------------
# Asana projects are named with an "[ENTITY]" / "[ENTITY-SUB]" prefix. This is a
# PURE (stdlib-only) helper so a standalone script can scope tasks to one entity
# WITHOUT importing tool_dispatch.ENTITY_PROJECT_PREFIXES (D-047: no bot-process
# imports). Values are the canonical prefixes copied from tool_dispatch, with two
# deliberate corrections for the daily-synthesis use case:
#   1. F3C is a SEPARATE entity here (its own #f3c-leadership channel), so its
#      prefixes are split OUT of F3E — tool_dispatch folds "[F3C]" under F3E for
#      tool exposure (inclusive by design), which would bleed F3C tasks into the
#      F3E synthesis. Neither entity's prefix set matches the other's projects.
#   2. LEX uses the UNION of every LEX sub-prefix (incl. "[LLC") so no LEX task
#      can leak into an itemized non-LEX post -- err over-inclusive for the
#      PHI-critical entity (matches strategy_memo._is_lex_task intent + D10).
# Prefixes are lowercase; matching lowercases the project name (case-insensitive).
ENTITY_PROJECT_PREFIXES: dict[str, tuple[str, ...]] = {
    "F3E": ("[f3e]", "[f3-e", "[f3 energy", "[f3 pure", "[f3 mood",
            "[f3pure", "[f3mood"),
    "F3C": ("[f3c]", "[f3 community", "[f3-c"),
    "LEX": ("[lex]", "[lex-", "[lts", "[lbhs", "[lla", "[llc"),
    "OSN": ("[osn]",),
    "BDM": ("[bdm]",),
    "UFL": ("[ufl]",),
    "HJRP": ("[hjrp]", "[hjrp-"),
    "HJRPROD": ("[hjrprod]", "[pod]", "[ff]", "[hjr-pb]", "[chk]", "[chb]"),
    "HJRG": ("[hjrg]",),
}


def _task_project_names(task: dict) -> list[str]:
    """Every project name attached to a task, across BOTH the flat `projects` and
    the `memberships[].project` opt-fields (get_user_tasks populates both; the two
    can diverge, so read both to avoid missing a scoping signal)."""
    names: list[str] = []
    for proj in task.get("projects") or []:
        name = (proj or {}).get("name")
        if name:
            names.append(str(name))
    for mem in task.get("memberships") or []:
        proj = (mem or {}).get("project") or {}
        name = proj.get("name")
        if name:
            names.append(str(name))
    return names


def task_belongs_to_entity(task: dict, entity: str) -> bool:
    """True if ANY of a task's project names carries *entity*'s "[PREFIX]".

    Case-insensitive; reads both `projects` and `memberships[].project` names.
    Unknown entity -> False (fail-closed). A task in a foreign entity's project
    (e.g. a "[F3C]" task tested against "F3E") returns False -- the prefix sets
    do not overlap across entities.
    """
    prefixes = ENTITY_PROJECT_PREFIXES.get((entity or "").upper())
    if not prefixes:
        return False
    for name in _task_project_names(task):
        low = name.lower()
        if any(low.startswith(p) for p in prefixes):
            return True
    return False
