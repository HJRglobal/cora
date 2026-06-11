"""Tests for org_roles -- the Phase 1 Org Synthesis role registry.

Layer A: source/wiring string assertions (app.py is not importable without
         slack deps, so wiring is asserted against source text -- the
         repo-standard pattern).
Layer B: unit tests against org_roles itself (yaml + stdlib only).
"""

from __future__ import annotations

import textwrap
import time
from pathlib import Path

import pytest
import yaml

from src.cora import org_roles

_REPO_ROOT = Path(__file__).resolve().parents[1]
_REGISTRY = _REPO_ROOT / "data" / "maps" / "org-roles.yaml"
_APP_SRC = (_REPO_ROOT / "src" / "cora" / "app.py").read_text(encoding="utf-8")

HARRISON = "U0B2RM2JYJ1"


@pytest.fixture(autouse=True)
def _fresh_registry():
    """Each test starts from the real registry path with a cold cache."""
    org_roles._ROLES_PATH = _REGISTRY
    org_roles.invalidate_cache()
    yield
    org_roles._ROLES_PATH = _REGISTRY
    org_roles.invalidate_cache()


def _write_registry(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "org-roles.yaml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    org_roles._ROLES_PATH = p
    org_roles.invalidate_cache()
    return p


# ── Registry file integrity ────────────────────────────────────────────────


class TestRegistryFile:
    def test_registry_exists_and_parses(self):
        data = yaml.safe_load(_REGISTRY.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        assert isinstance(data.get("users"), list)
        assert len(data["users"]) >= 15

    def test_every_entry_has_required_fields(self):
        data = yaml.safe_load(_REGISTRY.read_text(encoding="utf-8"))
        for entry in data["users"]:
            # slack_id optional (registry-only people, e.g. Tessa) but when
            # present it must be a real Slack ID.
            sid = entry.get("slack_id", "")
            assert sid == "" or sid.startswith("U"), entry
            assert entry.get("name"), entry
            assert entry.get("role"), entry
            assert entry.get("entity"), entry

    def test_slack_ids_unique(self):
        data = yaml.safe_load(_REGISTRY.read_text(encoding="utf-8"))
        ids = [e["slack_id"] for e in data["users"] if e.get("slack_id")]
        assert len(ids) == len(set(ids))

    def test_managers_reference_known_ids_or_blank(self):
        data = yaml.safe_load(_REGISTRY.read_text(encoding="utf-8"))
        ids = {e["slack_id"] for e in data["users"] if e.get("slack_id")}
        for entry in data["users"]:
            mgr = entry.get("manager", "")
            assert mgr == "" or mgr in ids, f"{entry['name']} manager {mgr} unknown"


class TestRosterCompleteness:
    """Drift guards: the registry must cover every identity-mapped human."""

    def test_covers_every_slack_to_asana_user(self):
        mapped = yaml.safe_load(
            (_REPO_ROOT / "data" / "maps" / "slack-to-asana.yaml").read_text(encoding="utf-8")
        )
        registry_ids = {r.slack_id for r in org_roles.all_roles()}
        for entry in mapped["users"]:
            sid = entry["slack_user_id"]
            assert sid in registry_ids, f"{entry.get('display_name')} ({sid}) missing from org-roles.yaml"

    def test_covers_phi_custodians(self):
        custodians = yaml.safe_load(
            (_REPO_ROOT / "data" / "maps" / "lex-phi-custodians.yaml").read_text(encoding="utf-8")
        )
        registry_ids = {r.slack_id for r in org_roles.all_roles()}
        for c in custodians["custodians"]:
            assert c["slack_id"] in registry_ids, f"custodian {c['name']} missing"

    def test_covers_finance_allowlist(self):
        allow = yaml.safe_load(
            (_REPO_ROOT / "data" / "maps" / "finance-receipt-allowlist.yaml").read_text(encoding="utf-8")
        )
        registry_ids = {r.slack_id for r in org_roles.all_roles()}
        for sid in allow["users"]:
            assert sid in registry_ids, f"finance-allowlisted {sid} missing"


# ── Lookup behavior ────────────────────────────────────────────────────────


class TestGetRole:
    def test_known_user(self):
        rec = org_roles.get_role(HARRISON)
        assert rec is not None
        assert rec.name == "Harrison Rogers"
        assert rec.entity == "FNDR"
        assert "Founder" in rec.role

    def test_unknown_user_fail_closed(self):
        assert org_roles.get_role("U0NOTAREALID") is None

    def test_empty_id_fail_closed(self):
        assert org_roles.get_role("") is None

    def test_all_entities_dedup_primary_first(self):
        rec = org_roles.get_role(HARRISON)
        ents = rec.all_entities
        assert ents[0] == "FNDR"
        assert len(ents) == len(set(ents))

    def test_roles_for_entity_lex(self):
        names = {r.name for r in org_roles.roles_for_entity("LEX")}
        assert {"Shaun Hawkins", "Jennifer Mortensen", "Jeff Montgomery"} <= names

    def test_roles_for_entity_case_insensitive(self):
        assert org_roles.roles_for_entity("lex") == org_roles.roles_for_entity("LEX")


class TestRegistryOnlyPeople:
    """People without Slack IDs (e.g. Tessa) ride the roster, never injection."""

    def test_tessa_in_all_roles(self):
        names = {r.name for r in org_roles.all_roles()}
        assert "Tessa Miller" in names

    def test_tessa_in_entity_rosters(self):
        for ent in ("HJRG", "OSN", "HJRP"):
            names = {r.name for r in org_roles.roles_for_entity(ent)}
            assert "Tessa Miller" in names, ent

    def test_registry_only_never_injects(self, tmp_path):
        _write_registry(
            tmp_path,
            """
            users:
              - name: No Slack Person
                role: Registry Only
                entity: HJRG
            """,
        )
        assert len(org_roles.all_roles()) == 1
        # No slack identity -> no get_role hit, no block.
        assert org_roles.get_role("") is None
        assert org_roles.format_role_context("") == ""

    def test_jerry_title_confirmed(self):
        rec = org_roles.get_role("U0B4L7886PJ")
        assert rec is not None
        assert rec.role == "Staff Accountant"
        assert rec.manager == "U0B3AEJCYGP"  # Justin


# ── Role block formatting ──────────────────────────────────────────────────


class TestFormatRoleContext:
    def test_unknown_user_empty_block(self):
        assert org_roles.format_role_context("U0NOTAREALID") == ""
        assert org_roles.format_role_context("") == ""

    def test_known_user_block_contents(self):
        block = org_roles.format_role_context("U0B3PS7RFJA")  # Matt Petrovich
        assert "OSN Operations Manager" in block
        assert "Their lanes:" in block
        assert "inventory reconciliation" in block

    def test_block_always_carries_no_expansion_rule(self):
        for sid in (HARRISON, "U0B3PS7RFJA", "U0B3NGR1Y85"):
            block = org_roles.format_role_context(sid)
            assert "does NOT expand" in block
            assert "guardrail" in block

    def test_external_consultant_caution(self):
        block = org_roles.format_role_context("U0B6LQNSR25")  # Jason Dorfman
        assert "EXTERNAL" in block
        assert "internal-only" in block

    def test_daniel_executor_removed_note(self):
        block = org_roles.format_role_context("U0B3PS63F1C")
        assert "never propose Daniel" in block

    def test_block_is_terse(self):
        # Uncached per-request token cost: keep every block under ~1K chars.
        for rec in org_roles.all_roles():
            if not rec.slack_id:
                continue
            block = org_roles.format_role_context(rec.slack_id)
            assert len(block) < 1000, f"{rec.name} block too long ({len(block)})"


# ── Loader robustness ──────────────────────────────────────────────────────


class TestLoaderRobustness:
    def test_malformed_entries_skipped(self, tmp_path):
        _write_registry(
            tmp_path,
            """
            users:
              - slack_id: U0GOOD
                name: Good Person
                role: Tester
                entity: F3E
              - slack_id: ""
                name: ""
                role: Ghost
                entity: F3E
              - just a string
              - slack_id: U0NOROLE
                name: No Role
                entity: F3E
            """,
        )
        assert org_roles.get_role("U0GOOD") is not None
        assert org_roles.get_role("U0NOROLE") is None
        assert len(org_roles.all_roles()) == 1

    def test_missing_file_yields_empty(self, tmp_path):
        org_roles._ROLES_PATH = tmp_path / "nope.yaml"
        org_roles.invalidate_cache()
        assert org_roles.get_role(HARRISON) is None
        assert org_roles.format_role_context(HARRISON) == ""

    def test_empty_file_yields_empty(self, tmp_path):
        _write_registry(tmp_path, "")
        assert org_roles.all_roles() == []

    def test_ttl_reload_picks_up_edit(self, tmp_path, monkeypatch):
        p = _write_registry(
            tmp_path,
            """
            users:
              - slack_id: U0EDIT
                name: Before Edit
                role: Old Role
                entity: F3E
            """,
        )
        assert org_roles.get_role("U0EDIT").role == "Old Role"
        p.write_text(
            textwrap.dedent(
                """
                users:
                  - slack_id: U0EDIT
                    name: After Edit
                    role: New Role
                    entity: F3E
                """
            ),
            encoding="utf-8",
        )
        # Within TTL: still old.
        assert org_roles.get_role("U0EDIT").role == "Old Role"
        # Past TTL: reloaded.
        real_monotonic = time.monotonic
        monkeypatch.setattr(
            org_roles.time, "monotonic", lambda: real_monotonic() + org_roles._TTL_SECONDS + 1
        )
        assert org_roles.get_role("U0EDIT").role == "New Role"

    def test_parse_error_keeps_last_good_registry(self, tmp_path, monkeypatch):
        p = _write_registry(
            tmp_path,
            """
            users:
              - slack_id: U0KEEP
                name: Keep Me
                role: Survivor
                entity: F3E
            """,
        )
        assert org_roles.get_role("U0KEEP") is not None
        p.write_text("users: [:::not yaml:::", encoding="utf-8")
        real_monotonic = time.monotonic
        monkeypatch.setattr(
            org_roles.time, "monotonic", lambda: real_monotonic() + org_roles._TTL_SECONDS + 1
        )
        # Broken file: last good registry survives.
        assert org_roles.get_role("U0KEEP") is not None


# ── Layer A: app.py wiring ─────────────────────────────────────────────────


class TestAppWiring:
    def test_org_roles_imported(self):
        assert "from . import org_roles" in _APP_SRC

    def test_role_block_built_from_caller(self):
        assert "org_roles.format_role_context(user_id or \"\")" in _APP_SRC

    def test_role_block_injected_into_runtime_context(self):
        assert "caller_role_block" in _APP_SRC
        # Injected conditionally so unknown users add zero tokens.
        assert "if caller_role_block else" in _APP_SRC

    def test_injection_sits_inside_runtime_context(self):
        runtime_idx = _APP_SRC.index("runtime_context = (")
        inject_idx = _APP_SRC.index("if caller_role_block else")
        synthesis_idx = _APP_SRC.index("TIER1_SYNTHESIS_RULE")
        assert runtime_idx < inject_idx < synthesis_idx
