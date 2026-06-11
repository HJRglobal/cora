"""Tests for the org-roles-driven daily briefing (Org Synthesis Phase 2, deliverable 2).

Coverage:
  - roster: registry-driven parity (every active registry user with a Slack ID
    gets a briefing built), externals + registry-only excluded, fail-closed
  - section composition: reuses the plate builders from tool_dispatch; LEX
    users never get a pipeline section; stalled decisions are Harrison-only;
    sections fail soft to stub lines
  - digest mode (DEFAULT): ONE DM to Harrison containing every user's
    would-be briefing; no per-user DMs (rollout doctrine 2026-06-11)
  - send mode: per-user DMs only with the explicit --send-users flag
  - retirement of role-briefing-config.yaml (old config path is gone)
  - digest chunking
  - sub-entity canonicalization in the SHARED plate task builder (LEX-LLC
    scopes to LEX, never unfiltered)

Doctrine: direct `sys.path + import mod` (NOT spec_from_file_location) so
patch.object(rdb, ...) intercepts module-global lookups.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from cora import org_roles  # noqa: E402
from cora.tools import tool_dispatch as td  # noqa: E402

import run_daily_briefing as rdb  # noqa: E402

# ---------------------------------------------------------------------------
# Registry fixture
# ---------------------------------------------------------------------------

_HARRISON = "U0B2RM2JYJ1"  # must match tool_dispatch._HARRISON_SLACK_ID

_SAMPLE_REGISTRY = """
users:
  - slack_id: U0B2RM2JYJ1
    name: Harrison Rogers
    role: Founder / CEO
    entity: FNDR
    responsibilities:
      - Portfolio strategy
  - slack_id: U101
    name: Tara Sales
    role: F3E Sales Lead
    entity: F3E
    responsibilities:
      - Retail pipeline
  - slack_id: U102
    name: Lana Lex
    role: LLC Office Manager
    entity: LEX-LLC
  - slack_id: UEXT
    name: Gene Guest
    role: Outside Consultant
    entity: F3E
    external: true
  - name: Reggie RegistryOnly
    role: Part-time EA
    entity: HJRG
"""


@pytest.fixture
def registry(tmp_path):
    p = tmp_path / "org-roles.yaml"
    p.write_text(_SAMPLE_REGISTRY, encoding="utf-8")
    with patch.object(org_roles, "_ROLES_PATH", p):
        org_roles.invalidate_cache()
        yield p
    org_roles.invalidate_cache()


@pytest.fixture
def empty_registry(tmp_path):
    p = tmp_path / "org-roles.yaml"
    p.write_text("users: []\n", encoding="utf-8")
    with patch.object(org_roles, "_ROLES_PATH", p):
        org_roles.invalidate_cache()
        yield p
    org_roles.invalidate_cache()


def _mock_slack():
    client = MagicMock()
    client.conversations_open.return_value = {"channel": {"id": "DM-CHAN"}}
    client.chat_postMessage.return_value = {"ok": True}
    return client


_ENV = {"SLACK_BOT_TOKEN": "xoxb-test", "ANTHROPIC_API_KEY": "key"}


# ---------------------------------------------------------------------------
# Roster (registry-driven config parity)
# ---------------------------------------------------------------------------

class TestRoster:
    def test_every_active_registry_user_with_slack_id_included(self, registry):
        roster, _ = rdb._load_briefing_roster()
        names = {r.name for r in roster}
        assert names == {"Harrison Rogers", "Tara Sales", "Lana Lex"}

    def test_external_excluded_from_delivery(self, registry):
        roster, excluded = rdb._load_briefing_roster()
        assert all(r.name != "Gene Guest" for r in roster)
        assert any("Gene Guest" in n and "external" in n for n in excluded)

    def test_registry_only_excluded_from_delivery(self, registry):
        roster, excluded = rdb._load_briefing_roster()
        assert all(r.name != "Reggie RegistryOnly" for r in roster)
        assert any("Reggie RegistryOnly" in n and "registry-only" in n for n in excluded)

    def test_empty_registry_yields_no_recipients(self, empty_registry):
        roster, excluded = rdb._load_briefing_roster()
        assert roster == []
        assert excluded == []

    def test_unknown_users_skipped_by_construction(self, registry):
        # The registry IS the roster -- there is no merge with any other map,
        # so a user absent from org-roles.yaml can never receive a briefing.
        roster, _ = rdb._load_briefing_roster()
        assert all(r.slack_id for r in roster)
        assert {r.slack_id for r in roster} <= {_HARRISON, "U101", "U102"}


# ---------------------------------------------------------------------------
# Section composition (shared plate builders)
# ---------------------------------------------------------------------------

class TestComposeSections:
    def test_role_header_and_lanes(self, registry):
        rec = org_roles.get_role("U101")
        with patch.object(rdb, "_plate_asana_section", return_value="T"), \
             patch.object(rdb, "_plate_calendar_section", return_value="C"), \
             patch.object(rdb, "_plate_hubspot_section", return_value=None):
            out = rdb._compose_sections(rec)
        assert "ROLE" in out
        assert "Tara Sales -- F3E Sales Lead (F3E)" in out
        assert "Lanes: Retail pipeline" in out
        assert "OPEN TASKS\nT" in out
        assert "CALENDAR\nC" in out

    def test_harrison_gets_stalled_decisions(self, registry):
        rec = org_roles.get_role(_HARRISON)
        with patch.object(rdb, "_plate_asana_section", return_value="T"), \
             patch.object(rdb, "_plate_calendar_section", return_value="C"), \
             patch.object(rdb, "_plate_hubspot_section", return_value=None), \
             patch.object(rdb, "_tool_fndr_open_decisions", return_value="DEC") as dec:
            out = rdb._compose_sections(rec)
        assert "STALLED DECISIONS\nDEC" in out
        dec.assert_called_once()

    def test_non_harrison_never_gets_stalled_decisions(self, registry):
        rec = org_roles.get_role("U101")
        with patch.object(rdb, "_plate_asana_section", return_value="T"), \
             patch.object(rdb, "_plate_calendar_section", return_value="C"), \
             patch.object(rdb, "_plate_hubspot_section", return_value=None), \
             patch.object(rdb, "_tool_fndr_open_decisions") as dec:
            out = rdb._compose_sections(rec)
        assert "STALLED DECISIONS" not in out
        dec.assert_not_called()

    def test_lex_user_never_gets_pipeline_section(self, registry):
        # Uses the REAL shared _plate_hubspot_section: LEX scope (incl.
        # sub-entities) returns None before any map/network access.
        rec = org_roles.get_role("U102")
        assert rec.entity == "LEX-LLC"
        with patch.object(rdb, "_plate_asana_section", return_value="T"), \
             patch.object(rdb, "_plate_calendar_section", return_value="C"):
            out = rdb._compose_sections(rec)
        assert "DEAL PIPELINE" not in out

    def test_pipeline_section_present_for_owner(self, registry):
        rec = org_roles.get_role("U101")
        with patch.object(rdb, "_plate_asana_section", return_value="T"), \
             patch.object(rdb, "_plate_calendar_section", return_value="C"), \
             patch.object(rdb, "_plate_hubspot_section", return_value="DEALS"):
            out = rdb._compose_sections(rec)
        assert "DEAL PIPELINE\nDEALS" in out

    def test_section_crash_degrades_to_stub(self, registry):
        # Real _safe_plate_section wraps the patched (crashing) builder.
        rec = org_roles.get_role("U101")
        with patch.object(rdb, "_plate_asana_section", side_effect=RuntimeError("boom")), \
             patch.object(rdb, "_plate_calendar_section", return_value="C"), \
             patch.object(rdb, "_plate_hubspot_section", return_value=None):
            out = rdb._compose_sections(rec)
        assert "(Open tasks section unavailable right now.)" in out

    def test_pipeline_crash_degrades_to_stub(self, registry):
        rec = org_roles.get_role("U101")
        with patch.object(rdb, "_plate_asana_section", return_value="T"), \
             patch.object(rdb, "_plate_calendar_section", return_value="C"), \
             patch.object(rdb, "_plate_hubspot_section", side_effect=RuntimeError("boom")):
            out = rdb._compose_sections(rec)
        assert "DEAL PIPELINE\n(Deal pipeline section unavailable right now.)" in out


# ---------------------------------------------------------------------------
# Digest mode (DEFAULT -- rollout doctrine)
# ---------------------------------------------------------------------------

class TestDigestMode:
    def _run(self, argv, registry_users=3):
        client = _mock_slack()
        with patch.dict("os.environ", _ENV), \
             patch.object(rdb, "SlackWebClient", return_value=client), \
             patch.object(rdb, "build_user_briefing",
                          side_effect=lambda rec, **k: f"BRIEF::{rec.name}"), \
             patch.object(rdb, "_write_audit", return_value=None):
            rc = rdb.main(argv)
        return rc, client

    def test_default_mode_is_digest_to_harrison_only(self, registry):
        rc, client = self._run([])
        assert rc == 0
        # ONE DM conversation opened -- Harrison's
        client.conversations_open.assert_called_once_with(users=[_HARRISON])
        sent = "\n".join(
            call.kwargs["text"] for call in client.chat_postMessage.call_args_list
        )
        assert "DAILY BRIEFING DIGEST" in sent
        for name in ("Harrison Rogers", "Tara Sales", "Lana Lex"):
            assert f"BRIEF::{name}" in sent

    def test_digest_only_flag_equivalent_to_default(self, registry):
        rc, client = self._run(["--digest-only"])
        assert rc == 0
        client.conversations_open.assert_called_once_with(users=[_HARRISON])

    def test_digest_header_carries_rollout_note_and_exclusions(self, registry):
        _, client = self._run([])
        first_msg = client.chat_postMessage.call_args_list[0].kwargs["text"]
        assert "per-user delivery is OFF" in first_msg
        assert "--send-users" in first_msg
        assert "Gene Guest" in first_msg          # external, named as excluded
        assert "Reggie RegistryOnly" in first_msg  # registry-only, named as excluded

    def test_no_briefing_dm_ever_reaches_external_or_other_users(self, registry):
        _, client = self._run([])
        opened = [c.kwargs["users"] for c in client.conversations_open.call_args_list]
        assert opened == [[_HARRISON]]

    def test_build_failure_lands_in_digest_and_returns_2(self, registry):
        client = _mock_slack()

        def _build(rec, **k):
            if rec.name == "Lana Lex":
                raise RuntimeError("asana down")
            return f"BRIEF::{rec.name}"

        with patch.dict("os.environ", _ENV), \
             patch.object(rdb, "SlackWebClient", return_value=client), \
             patch.object(rdb, "build_user_briefing", side_effect=_build), \
             patch.object(rdb, "_write_audit", return_value=None):
            rc = rdb.main([])
        assert rc == 2
        sent = "\n".join(
            call.kwargs["text"] for call in client.chat_postMessage.call_args_list
        )
        assert "(briefing could not be built" in sent
        assert "BRIEF::Tara Sales" in sent  # other users still present

    def test_empty_registry_returns_0_sends_nothing(self, empty_registry):
        rc, client = self._run([])
        assert rc == 0
        client.chat_postMessage.assert_not_called()


# ---------------------------------------------------------------------------
# Send mode (per-user delivery -- explicit flag only)
# ---------------------------------------------------------------------------

class TestSendMode:
    def test_send_users_dms_each_active_registry_user(self, registry):
        client = _mock_slack()
        with patch.dict("os.environ", _ENV), \
             patch.object(rdb, "SlackWebClient", return_value=client), \
             patch.object(rdb, "build_user_briefing",
                          side_effect=lambda rec, **k: f"BRIEF::{rec.name}"), \
             patch.object(rdb, "_write_audit", return_value=None), \
             patch.object(rdb.time, "sleep", return_value=None):
            rc = rdb.main(["--send-users"])
        assert rc == 0
        opened = sorted(c.kwargs["users"][0] for c in client.conversations_open.call_args_list)
        assert opened == sorted([_HARRISON, "U101", "U102"])
        assert client.chat_postMessage.call_count == 3

    def test_user_filter_limits_delivery(self, registry):
        client = _mock_slack()
        with patch.dict("os.environ", _ENV), \
             patch.object(rdb, "SlackWebClient", return_value=client), \
             patch.object(rdb, "build_user_briefing",
                          side_effect=lambda rec, **k: f"BRIEF::{rec.name}"), \
             patch.object(rdb, "_write_audit", return_value=None), \
             patch.object(rdb.time, "sleep", return_value=None):
            rc = rdb.main(["--send-users", "--user", "tara"])
        assert rc == 0
        client.conversations_open.assert_called_once_with(users=["U101"])

    def test_flags_mutually_exclusive(self, registry):
        with pytest.raises(SystemExit):
            rdb.main(["--digest-only", "--send-users"])


# ---------------------------------------------------------------------------
# Dry run + env guards
# ---------------------------------------------------------------------------

class TestDryRunAndEnv:
    def test_dry_run_sends_nothing(self, registry, capsys):
        with patch.dict("os.environ", {"SLACK_BOT_TOKEN": "", "ANTHROPIC_API_KEY": "key"}), \
             patch.object(rdb, "SlackWebClient") as slack_cls, \
             patch.object(rdb, "build_user_briefing",
                          side_effect=lambda rec, **k: f"BRIEF::{rec.name}"):
            rc = rdb.main(["--dry-run"])
        assert rc == 0
        slack_cls.assert_not_called()
        out = capsys.readouterr().out
        assert "BRIEF::Tara Sales" in out

    def test_missing_anthropic_key_returns_1(self, registry):
        with patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test", "ANTHROPIC_API_KEY": ""}):
            assert rdb.main([]) == 1

    def test_missing_slack_token_returns_1_when_sending(self, registry):
        with patch.dict("os.environ", {"SLACK_BOT_TOKEN": "", "ANTHROPIC_API_KEY": "key"}):
            assert rdb.main([]) == 1


# ---------------------------------------------------------------------------
# Retirement of role-briefing-config.yaml (D-044 item 5 consolidation point)
# ---------------------------------------------------------------------------

class TestOldConfigRetired:
    def test_module_has_no_role_config_path(self):
        assert not hasattr(rdb, "_ROLE_CONFIG")
        assert not hasattr(rdb, "_load_role_config")

    def test_source_never_references_old_config(self):
        src = Path(rdb.__file__).read_text(encoding="utf-8")
        assert "role-briefing-config" not in src

    def test_old_config_file_deleted_from_repo(self):
        assert not (_REPO_ROOT / "data" / "maps" / "role-briefing-config.yaml").exists()

    def test_roster_reads_org_roles_registry(self, registry):
        # The roster is served by org_roles (60s TTL live-reload) -- edit the
        # registry, invalidate, and the roster follows with no other config.
        roster, _ = rdb._load_briefing_roster()
        assert {r.slack_id for r in roster} == {_HARRISON, "U101", "U102"}


# ---------------------------------------------------------------------------
# Digest chunking
# ---------------------------------------------------------------------------

class TestDigestChunking:
    def test_single_message_when_small(self):
        msgs = rdb._chunk_digest("HEAD", ["a", "b"])
        assert len(msgs) == 1
        assert msgs[0].startswith("HEAD")
        assert "a" in msgs[0] and "b" in msgs[0]

    def test_splits_on_size_and_preserves_all_blocks(self):
        big = "x" * 3000
        blocks = [f"=== U{i} ===\n{big}" for i in range(4)]
        msgs = rdb._chunk_digest("HEAD", blocks)
        assert len(msgs) > 1
        joined = "\n".join(msgs)
        for i in range(4):
            assert f"=== U{i} ===" in joined
        # No message except a single-oversized-block one exceeds the cap by a block
        for m in msgs:
            assert len(m) <= rdb._DIGEST_CHUNK_CHARS + len(blocks[0])

    def test_order_preserved(self):
        big = "y" * 3400
        blocks = [f"B{i}|{big}" for i in range(3)]
        msgs = rdb._chunk_digest("HEAD", blocks)
        joined = "".join(msgs)
        assert joined.index("B0|") < joined.index("B1|") < joined.index("B2|")


# ---------------------------------------------------------------------------
# Sub-entity canonicalization in the SHARED plate task builder
# ---------------------------------------------------------------------------

class TestSharedBuilderSubEntityScope:
    def _tasks(self):
        return [
            {
                "name": "LEX task A",
                "permalink_url": "https://app.asana.com/1",
                "memberships": [{"project": {"name": "[LEX] Operations"}}],
                "projects": [],
            },
            {
                "name": "OSN task B",
                "permalink_url": "https://app.asana.com/2",
                "memberships": [{"project": {"name": "[OSN] Store Ops"}}],
                "projects": [],
            },
        ]

    def test_lex_llc_scopes_to_lex_not_unfiltered(self):
        with patch.object(td, "_load_slack_asana_map",
                          return_value={"U_X": {"asana_user_gid": "111"}}), \
             patch.object(td.asana_client, "get_user_tasks", return_value=self._tasks()):
            out = td._plate_asana_section("U_X", "LEX-LLC")
        assert "LEX task A" in out
        assert "OSN task B" not in out

    def test_osn_substore_scopes_to_osn(self):
        with patch.object(td, "_load_slack_asana_map",
                          return_value={"U_X": {"asana_user_gid": "111"}}), \
             patch.object(td.asana_client, "get_user_tasks", return_value=self._tasks()):
            out = td._plate_asana_section("U_X", "OSNGW")
        assert "OSN task B" in out
        assert "LEX task A" not in out
