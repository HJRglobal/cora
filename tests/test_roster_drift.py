"""Roster-drift guard (audit N4 / Phase 1.1).

org-roles.yaml is the canonical Slack-ID-keyed registry. Every user that appears
in an operational map (slack-to-asana, user-permissions, lex-phi-custodians) must
also exist in org-roles, so identity / role resolution never hits an unknown
user. This test fails loudly if a map drifts out of org-roles -- the lightweight
cross-validation alternative to merging the maps (Phase 1.1).
"""

from __future__ import annotations

from pathlib import Path

import yaml

_MAPS = Path(__file__).resolve().parents[1] / "data" / "maps"


def _load(name: str) -> dict:
    return yaml.safe_load((_MAPS / name).read_text(encoding="utf-8")) or {}


def _org_roles_ids() -> set[str]:
    return {u["slack_id"] for u in _load("org-roles.yaml").get("users", []) if u.get("slack_id")}


def _slack_to_asana_ids() -> set[str]:
    return {u["slack_user_id"] for u in _load("slack-to-asana.yaml").get("users", []) if u.get("slack_user_id")}


def _user_permissions_ids() -> set[str]:
    return set((_load("user-permissions.yaml").get("users") or {}).keys())


def _custodian_ids() -> set[str]:
    return {c["slack_id"] for c in _load("lex-phi-custodians.yaml").get("custodians", []) if c.get("slack_id")}


def test_org_roles_registry_is_populated():
    assert len(_org_roles_ids()) >= 18  # sanity: the canonical registry has the roster


def test_slack_to_asana_subset_of_org_roles():
    missing = _slack_to_asana_ids() - _org_roles_ids()
    assert not missing, f"slack-to-asana users missing from org-roles.yaml: {sorted(missing)}"


def test_user_permissions_subset_of_org_roles():
    missing = _user_permissions_ids() - _org_roles_ids()
    assert not missing, f"user-permissions users missing from org-roles.yaml: {sorted(missing)}"


def test_phi_custodians_subset_of_org_roles():
    missing = _custodian_ids() - _org_roles_ids()
    assert not missing, f"PHI custodians missing from org-roles.yaml: {sorted(missing)}"


def test_backfilled_users_present_in_slack_to_asana():
    # audit N4: Aaron / Sara / Jerry were in org-roles but not slack-to-asana, so
    # briefings + calendar rendered them "unknown". They must now resolve.
    ids = _slack_to_asana_ids()
    for sid in ("U0B3PS32A22", "U0B9JS3JW07", "U0B4L7886PJ"):
        assert sid in ids, f"{sid} not backfilled into slack-to-asana.yaml"
