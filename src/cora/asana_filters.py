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
