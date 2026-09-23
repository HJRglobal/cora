"""S2 (cq-ab38a636e545): Jerry Reick's delegated-work eligibility.

PREMISE LARGELY OVERTURNED. The seed asks to "add Jerry to the DW roster mapping
with the standard $2/job + monthly envelope quota". There is no DW roster mapping
and no per-user quota to add him to, and he is already eligible.

  ELIGIBILITY IS ORG-ROLES-DERIVED, FULL STOP. `delegated_work.screen_request`
  screen #2 resolves the asker through `org_roles.get_role` and refuses only an
  unknown user or one flagged `external`. Grepping delegated_work.py,
  delegated_worker.py, run_delegated_work_runner.py and data/maps/*.yaml for any
  DW roster / seat / pilot map returns nothing. The CLAUDE.md note about a
  "delegated-work pilot member" is a YAML `notes:` string, not a gate.

  THE QUOTA IS GLOBAL ENV, NOT PER USER. user_daily_quota / org_daily_quota /
  job_usd_cap / monthly_usd_cap all read env with defaults (3 / 10 / $2 / $50),
  and mtd_spend is org-wide. "The standard quota" is not something you grant --
  it is what every non-founder already has.

  HE ALREADY HAS AN ORG-ROLES ENTRY (Staff Accountant, HJRG, manager Justin),
  pinned by test_org_roles and test_roster_drift.

So this file pins the state rather than changing it, and records the one thing
that IS worth a ruling.

WHAT ACTUALLY LIMITS HIM: screen #4 runs `user_access.check_access` against the
CHANNEL entity. Jerry is absent from user-permissions.yaml, and an unlisted user
is allowed FNDR and HJRG only. So DW works for him in a DM (his DM entity
resolves to his org-roles primary, HJRG) and in FNDR/HJRG channels, and refuses
elsewhere at the generic user_access layer before the DW lane is consulted.

⚠️ AND THE TRAP IF THAT IS EVER WIDENED: `check_access` SHORT-CIRCUITS topic
screening for unlisted users -- `blocked = blocked_topics(user_id); if not
blocked: return None`. Jerry today has NO topic blocks at all, and is bounded
only by entity. Adding him to user-permissions.yaml to reach LEX WITHOUT
`sensitive_topics_blocked: [phi, cap_table]` would hand him LEX-entity access
with the phi topic gate silently absent -- strictly worse than today. That is
Harrison's call and no entry is written here.
"""

from __future__ import annotations

import pytest

from cora import delegated_work, org_roles, user_access

JERRY = "U0B4L7886PJ"


def test_jerry_is_in_the_org_registry_and_is_not_external():
    role = org_roles.get_role(JERRY)
    assert role is not None, "DW screen #2 fails closed on an unknown asker"
    assert getattr(role, "external", False) is False
    assert (role.entity or "").upper() == "HJRG"


def test_no_dw_roster_or_per_user_quota_map_exists():
    """If one is ever added, this fails and whoever added it has to reconcile
    the seed's premise with reality rather than quietly leaving both."""
    import inspect
    src = inspect.getsource(delegated_work)
    for token in ("dw_roster", "DW_ROSTER", "delegated-work-roster",
                  "per_user_quota", "seat_map"):
        assert token not in src, f"a DW roster/seat concept appeared: {token}"


def test_the_quota_is_global_not_granted_per_person():
    """quota_remaining is user_daily_quota() minus that user's usage today --
    the same ceiling for every non-founder."""
    assert delegated_work.user_daily_quota() >= 1
    assert delegated_work.job_usd_cap() > 0
    assert delegated_work.monthly_usd_cap() > 0
    assert delegated_work.quota_remaining(JERRY) <= delegated_work.user_daily_quota()


# RULED 2026-09-19 (ask 9.14; Harrison commit f65b7fc): Jerry now HAS a
# user-permissions row -- FNDR/HJRG/F3E/OSN/HJRP, with phi/hr/legal/cap_table
# blocked. Re-pinned 2026-09-22 (Code #14); these two tests pinned the pre-ruling
# "unlisted -> FNDR+HJRG only" state. The ruling's draft row was HJRG-only with
# "entity lists are yours to trim"; the committed row is wider (it matches
# Tessa's). Flagged in the Code #14 cascade report for Harrison to confirm.
JERRY_ENTITIES = ("FNDR", "HJRG", "F3E", "OSN", "HJRP")


def test_jerry_reaches_his_ruled_entities_and_not_others():
    for entity in JERRY_ENTITIES:
        assert user_access.is_authorized(JERRY, entity) is True, entity
    for entity in ("LEX", "LEX-LLC", "LEX-LTS", "UFL", "BDM"):
        assert user_access.is_authorized(JERRY, entity) is False, entity


def test_his_row_carries_the_phi_block_and_never_reaches_lex():
    """The trap this file documented: an unlisted user has NO topic blocks, so
    check_access short-circuits topic screening. His ruled row now carries the
    blocks, and it still does not reach LEX; if it is ever widened to LEX, the phi
    block is what keeps that safe."""
    blocked = user_access.blocked_topics(JERRY)
    for topic in ("phi", "hr", "legal", "cap_table"):
        assert topic in blocked, topic
    assert user_access.is_authorized(JERRY, "LEX") is False, (
        "Jerry now reaches LEX -- confirm with Harrison; the phi block must stay")
