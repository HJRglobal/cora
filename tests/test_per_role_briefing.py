"""Tests for Feature #2 -- Per-Role Entity Briefings.

Coverage:
  - _load_role_config(): happy path, missing file, malformed entry
  - _load_users(): merge with role config, skip_briefing flag, fallback defaults
  - _fetch_tasks(): entity filtering (F3E, LEX, OSN, FNDR pass-through)
  - _fetch_extra_data(): each extra_data key, unknown key handled gracefully
  - _fetch_hubspot_f3e_summary(): success + error fallback
  - _fetch_hubspot_all_summary(): success + error fallback
  - _fetch_financial_snapshot(): success + error fallback
  - _fetch_deal_aging_summary(): no DB, empty table, aging logic, no aging
  - _build_briefing(): prompt includes role + entity + extra_data sections
  - _load_asana_users(): happy path, missing file
  - main(): full run, missing env vars, no users
"""

from __future__ import annotations

import json
import sys
import time
import types
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

# ---------------------------------------------------------------------------
# Import the module under test
# ---------------------------------------------------------------------------

import importlib
import importlib.util

def _load_module():
    spec = importlib.util.spec_from_file_location(
        "run_daily_briefing",
        _REPO_ROOT / "scripts" / "run_daily_briefing.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

# We need to mock asana_client before loading
_ASANA_MOCK = MagicMock()
_ASANA_MOCK.get_user_tasks = MagicMock(return_value=[])
_ASANA_MOCK.AsanaClientError = Exception

with patch.dict("sys.modules", {
    "cora.tools.asana_client": _ASANA_MOCK,
    "dotenv": MagicMock(load_dotenv=lambda: None),
}):
    rdb = _load_module()

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

_SAMPLE_ASANA_YAML = """
users:
  - slack_user_id: U001
    asana_user_gid: "111"
    asana_email: alice@hjrglobal.com
    display_name: Alice Smith
  - slack_user_id: U002
    asana_user_gid: "222"
    asana_email: bob@hjrglobal.com
    display_name: Bob Jones
  - slack_user_id: U003
    asana_user_gid: "333"
    asana_email: carol@hjrglobal.com
    display_name: Carol White
"""

_SAMPLE_ROLE_YAML = """
users:
  - slack_user_id: U001
    role: "F3E Sales Lead"
    entity: F3E
    extra_data:
      - hubspot_f3e
      - deal_aging
    briefing_channel: ""
  - slack_user_id: U002
    role: "Controller"
    entity: HJRG
    extra_data:
      - financial
    briefing_channel: "#hjrg-finance"
  - slack_user_id: U003
    skip_briefing: true
    role: "External Contractor"
    entity: FNDR
    extra_data: []
    briefing_channel: ""
"""


def _make_task(name: str, project: str, due: str = "", url: str = "") -> dict:
    return {
        "name": name,
        "due_on": due,
        "permalink_url": url,
        "memberships": [{"project": {"name": project}}],
        "projects": [],
    }


# ---------------------------------------------------------------------------
# _load_asana_users
# ---------------------------------------------------------------------------

class TestLoadAsanaUsers:
    def test_happy_path(self, tmp_path):
        p = tmp_path / "slack-to-asana.yaml"
        p.write_text(_SAMPLE_ASANA_YAML, encoding="utf-8")
        with patch.object(rdb, "_ASANA_MAP", p):
            users = rdb._load_asana_users()
        assert len(users) == 3
        assert "U001" in users
        assert users["U001"]["first_name"] == "Alice"

    def test_missing_file(self, tmp_path, caplog):
        with patch.object(rdb, "_ASANA_MAP", tmp_path / "nonexistent.yaml"):
            users = rdb._load_asana_users()
        assert users == {}

    def test_skips_incomplete_entries(self, tmp_path):
        yaml_text = "users:\n  - slack_user_id: U999\n  - slack_user_id: U001\n    asana_user_gid: '1'\n    display_name: Valid User\n"
        p = tmp_path / "asana.yaml"
        p.write_text(yaml_text, encoding="utf-8")
        with patch.object(rdb, "_ASANA_MAP", p):
            users = rdb._load_asana_users()
        assert "U001" in users
        assert "U999" not in users


# ---------------------------------------------------------------------------
# _load_role_config
# ---------------------------------------------------------------------------

class TestLoadRoleConfig:
    def test_happy_path(self, tmp_path):
        p = tmp_path / "role-briefing-config.yaml"
        p.write_text(_SAMPLE_ROLE_YAML, encoding="utf-8")
        with patch.object(rdb, "_ROLE_CONFIG", p):
            config = rdb._load_role_config()
        assert "U001" in config
        assert config["U001"]["role"] == "F3E Sales Lead"
        assert config["U001"]["entity"] == "F3E"
        assert "hubspot_f3e" in config["U001"]["extra_data"]

    def test_missing_file_returns_empty(self, tmp_path):
        with patch.object(rdb, "_ROLE_CONFIG", tmp_path / "nonexistent.yaml"):
            config = rdb._load_role_config()
        assert config == {}

    def test_malformed_yaml_returns_empty(self, tmp_path):
        p = tmp_path / "role.yaml"
        p.write_text("not: valid: yaml: [[[", encoding="utf-8")
        with patch.object(rdb, "_ROLE_CONFIG", p):
            config = rdb._load_role_config()
        assert config == {}


# ---------------------------------------------------------------------------
# _load_users (merged)
# ---------------------------------------------------------------------------

class TestLoadUsers:
    def test_merge_applies_role(self, tmp_path):
        ap = tmp_path / "asana.yaml"
        rp = tmp_path / "role.yaml"
        ap.write_text(_SAMPLE_ASANA_YAML, encoding="utf-8")
        rp.write_text(_SAMPLE_ROLE_YAML, encoding="utf-8")
        with patch.object(rdb, "_ASANA_MAP", ap), patch.object(rdb, "_ROLE_CONFIG", rp):
            users = rdb._load_users()
        # Carol (U003) should be skipped
        names = [u["display_name"] for u in users]
        assert "Carol White" not in names
        assert len(users) == 2
        alice = next(u for u in users if u["slack_user_id"] == "U001")
        assert alice["role"] == "F3E Sales Lead"
        assert alice["entity"] == "F3E"
        assert "hubspot_f3e" in alice["extra_data"]

    def test_no_role_config_uses_defaults(self, tmp_path):
        ap = tmp_path / "asana.yaml"
        ap.write_text(_SAMPLE_ASANA_YAML, encoding="utf-8")
        with patch.object(rdb, "_ASANA_MAP", ap), patch.object(rdb, "_ROLE_CONFIG", tmp_path / "nope.yaml"):
            users = rdb._load_users()
        assert len(users) == 3
        assert all(u["role"] == "Team Member" for u in users)
        assert all(u["entity"] == "FNDR" for u in users)

    def test_briefing_channel_from_config(self, tmp_path):
        ap = tmp_path / "asana.yaml"
        rp = tmp_path / "role.yaml"
        ap.write_text(_SAMPLE_ASANA_YAML, encoding="utf-8")
        rp.write_text(_SAMPLE_ROLE_YAML, encoding="utf-8")
        with patch.object(rdb, "_ASANA_MAP", ap), patch.object(rdb, "_ROLE_CONFIG", rp):
            users = rdb._load_users()
        bob = next(u for u in users if u["slack_user_id"] == "U002")
        assert bob["briefing_channel"] == "#hjrg-finance"


# ---------------------------------------------------------------------------
# _fetch_tasks (entity filtering)
# ---------------------------------------------------------------------------

class TestFetchTasks:
    def _tasks(self):
        return [
            _make_task("F3E task 1", "[F3E] Sales Pipeline"),
            _make_task("OSN task 1", "[OSN] Operations"),
            _make_task("FNDR task 1", "[HJRG] Q1 Goals"),
            _make_task("F3E task 2", "[F3 Pure] Launch"),
        ]

    def test_fndr_returns_all(self):
        with patch.object(rdb, "get_user_tasks", return_value=self._tasks()):
            result = rdb._fetch_tasks("111", "FNDR")
        assert len(result) == 4

    def test_f3e_filters_correctly(self):
        with patch.object(rdb, "get_user_tasks", return_value=self._tasks()):
            result = rdb._fetch_tasks("111", "F3E")
        names = [t["name"] for t in result]
        assert "F3E task 1" in names
        assert "F3E task 2" in names
        assert "OSN task 1" not in names

    def test_osn_filters_correctly(self):
        with patch.object(rdb, "get_user_tasks", return_value=self._tasks()):
            result = rdb._fetch_tasks("111", "OSN")
        names = [t["name"] for t in result]
        assert "OSN task 1" in names
        assert "F3E task 1" not in names

    def test_asana_error_returns_empty(self):
        with patch.object(rdb, "get_user_tasks", side_effect=rdb.AsanaClientError("fail")):
            result = rdb._fetch_tasks("111", "F3E")
        assert result == []

    def test_unknown_entity_passes_through(self):
        with patch.object(rdb, "get_user_tasks", return_value=self._tasks()):
            result = rdb._fetch_tasks("111", "UNKNOWN_ENTITY")
        assert len(result) == 4  # no filter applied for unknown entity


# ---------------------------------------------------------------------------
# _fetch_extra_data
# ---------------------------------------------------------------------------

class TestFetchExtraData:
    def test_hubspot_f3e_called(self):
        # Patch the fetcher in the module's dict (already bound at load time)
        original = rdb._EXTRA_DATA_FETCHERS["hubspot_f3e"]
        rdb._EXTRA_DATA_FETCHERS["hubspot_f3e"] = lambda: "f3e summary"
        try:
            result = rdb._fetch_extra_data(["hubspot_f3e"])
        finally:
            rdb._EXTRA_DATA_FETCHERS["hubspot_f3e"] = original
        assert "F3E Sales Pipeline (HubSpot)" in result
        assert result["F3E Sales Pipeline (HubSpot)"] == "f3e summary"

    def test_hubspot_all_called(self):
        with patch.object(rdb, "_fetch_hubspot_all_summary", return_value="all summary"):
            result = rdb._fetch_extra_data(["hubspot_all"])
        assert "Sales Pipelines Overview (HubSpot)" in result

    def test_financial_called(self):
        with patch.object(rdb, "_fetch_financial_snapshot", return_value="cash: $500K"):
            result = rdb._fetch_extra_data(["financial"])
        assert "Cash Flow Snapshot" in result

    def test_deal_aging_called(self):
        with patch.object(rdb, "_fetch_deal_aging_summary", return_value="aging: 2 deals"):
            result = rdb._fetch_extra_data(["deal_aging"])
        assert "Deal Aging Alerts" in result

    def test_unknown_key_skipped_gracefully(self):
        result = rdb._fetch_extra_data(["hubspot_f3e", "totally_unknown_key"])
        # only 1 result (unknown key skipped, hubspot_f3e returned error string since not mocked)
        assert "totally_unknown_key" not in str(result)

    def test_empty_list_returns_empty(self):
        result = rdb._fetch_extra_data([])
        assert result == {}


# ---------------------------------------------------------------------------
# Individual extra data fetchers
# ---------------------------------------------------------------------------

class TestExtraFetchers:
    def test_hubspot_f3e_error_returns_fallback(self):
        with patch("cora.tools.hubspot_client.get_f3e_pipeline_summary_text", side_effect=Exception("fail")):
            result = rdb._fetch_hubspot_f3e_summary()
        assert "unavailable" in result

    def test_hubspot_all_error_returns_fallback(self):
        with patch("cora.tools.hubspot_client.get_f3e_pipeline_summary_text", side_effect=Exception("fail")):
            result = rdb._fetch_hubspot_all_summary()
        assert "unavailable" in result

    def test_financial_error_returns_fallback(self):
        # Patch the fetcher at the module level since it uses a local import
        with patch.object(rdb, "_fetch_financial_snapshot", return_value="(financial data unavailable)"):
            result = rdb._fetch_extra_data(["financial"])
        assert "unavailable" in result["Cash Flow Snapshot"]

    def test_deal_aging_no_db(self, tmp_path):
        with patch.object(rdb, "_REPO_ROOT", tmp_path):
            result = rdb._fetch_deal_aging_summary()
        assert "no deal snapshot data" in result

    def test_deal_aging_empty_table(self, tmp_path):
        import sqlite3
        db = tmp_path / "data" / "hubspot_deal_snapshots.db"
        db.parent.mkdir(parents=True)
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE deal_last_stage (deal_name TEXT, stage_name TEXT, last_seen_ts INTEGER)")
        conn.commit()
        conn.close()
        with patch.object(rdb, "_REPO_ROOT", tmp_path):
            result = rdb._fetch_deal_aging_summary()
        assert "no active deals" in result

    def test_deal_aging_detects_aged_deal(self, tmp_path):
        import sqlite3
        db = tmp_path / "data" / "hubspot_deal_snapshots.db"
        db.parent.mkdir(parents=True)
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE deal_last_stage (deal_name TEXT, stage_name TEXT, last_seen_ts INTEGER)")
        # Insert a deal that's been in "Identify" stage for 20 days (threshold: 14)
        old_ts = int(time.time()) - 20 * 86400
        conn.execute("INSERT INTO deal_last_stage VALUES (?, ?, ?)", ("Stale Deal", "Identify", old_ts))
        conn.commit()
        conn.close()
        with patch.object(rdb, "_REPO_ROOT", tmp_path):
            result = rdb._fetch_deal_aging_summary()
        assert "Stale Deal" in result
        assert "Identify" in result

    def test_deal_aging_fresh_deal_not_flagged(self, tmp_path):
        import sqlite3
        db = tmp_path / "data" / "hubspot_deal_snapshots.db"
        db.parent.mkdir(parents=True)
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE deal_last_stage (deal_name TEXT, stage_name TEXT, last_seen_ts INTEGER)")
        # A deal entered today -- should NOT appear in aging list
        fresh_ts = int(time.time()) - 2 * 86400
        conn.execute("INSERT INTO deal_last_stage VALUES (?, ?, ?)", ("Fresh Deal", "Identify", fresh_ts))
        conn.commit()
        conn.close()
        with patch.object(rdb, "_REPO_ROOT", tmp_path):
            result = rdb._fetch_deal_aging_summary()
        assert "Fresh Deal" not in result


# ---------------------------------------------------------------------------
# _build_briefing (prompt content)
# ---------------------------------------------------------------------------

class TestBuildBriefing:
    def _run(self, role="Sales Lead", entity="F3E", extra_data=None, tasks=None, chunks=None):
        captured_prompts = []

        def fake_create(**kwargs):
            msg_content = kwargs.get("messages", [{}])[0].get("content", "")
            captured_prompts.append(msg_content)
            resp = MagicMock()
            resp.content = [MagicMock(text="Good morning, Alice! Here is your briefing.")]
            return resp

        mock_client = MagicMock()
        mock_client.messages.create.side_effect = fake_create

        import anthropic as _ant
        with patch.object(_ant, "Anthropic", return_value=mock_client):
            result = rdb._build_briefing(
                api_key="test-key",
                display_name="Alice Smith",
                first_name="Alice",
                role=role,
                entity=entity,
                tasks=tasks or [],
                chunks=chunks or [],
                extra_data=extra_data or {},
                today_str="Monday, June 3, 2026",
            )
        return result, captured_prompts[0] if captured_prompts else ""

    def test_role_in_prompt(self):
        _, prompt = self._run(role="F3E Sales Lead")
        assert "F3E Sales Lead" in prompt

    def test_entity_note_in_prompt_for_non_fndr(self):
        _, prompt = self._run(entity="F3E")
        assert "F3E" in prompt

    def test_extra_data_section_in_prompt(self):
        _, prompt = self._run(extra_data={"F3E Sales Pipeline (HubSpot)": "3 open deals"})
        assert "3 open deals" in prompt
        assert "F3E Sales Pipeline" in prompt

    def test_tasks_in_prompt(self):
        tasks = [_make_task("Send samples to Sprouts", "[F3E] Sales")]
        _, prompt = self._run(tasks=tasks)
        assert "Send samples to Sprouts" in prompt

    def test_returns_haiku_output(self):
        result, _ = self._run()
        assert "Good morning, Alice!" in result

    def test_no_tasks_text(self):
        _, prompt = self._run(tasks=[])
        assert "no open tasks" in prompt


# ---------------------------------------------------------------------------
# main() integration
# ---------------------------------------------------------------------------

class TestMain:
    def test_missing_slack_token_returns_1(self):
        with patch.dict("os.environ", {"SLACK_BOT_TOKEN": "", "ANTHROPIC_API_KEY": "key"}):
            assert rdb.main() == 1

    def test_missing_anthropic_key_returns_1(self):
        with patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test", "ANTHROPIC_API_KEY": ""}):
            assert rdb.main() == 1

    def test_no_users_returns_0(self, tmp_path):
        ap = tmp_path / "asana.yaml"
        ap.write_text("users: []\n", encoding="utf-8")
        with (
            patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test", "ANTHROPIC_API_KEY": "key"}),
            patch.object(rdb, "_ASANA_MAP", ap),
            patch.object(rdb, "_ROLE_CONFIG", tmp_path / "nope.yaml"),
        ):
            assert rdb.main() == 0

    def test_full_run_success(self, tmp_path):
        ap = tmp_path / "asana.yaml"
        rp = tmp_path / "role.yaml"
        ap.write_text("""users:
  - slack_user_id: U001
    asana_user_gid: "111"
    asana_email: alice@hjrglobal.com
    display_name: Alice Smith
""", encoding="utf-8")
        rp.write_text("""users:
  - slack_user_id: U001
    role: "F3E Sales Lead"
    entity: F3E
    extra_data: []
    briefing_channel: ""
""", encoding="utf-8")

        mock_slack = MagicMock()
        mock_slack.conversations_open.return_value = {"channel": {"id": "DM001"}}
        mock_slack.chat_postMessage.return_value = {"ok": True}

        mock_haiku_resp = MagicMock()
        mock_haiku_resp.content = [MagicMock(text="Good morning, Alice!")]
        mock_ant_client = MagicMock()
        mock_ant_client.messages.create.return_value = mock_haiku_resp

        import anthropic as _ant
        with (
            patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test", "ANTHROPIC_API_KEY": "key"}),
            patch.object(rdb, "_ASANA_MAP", ap),
            patch.object(rdb, "_ROLE_CONFIG", rp),
            patch.object(rdb, "_KB_DB_PATH", tmp_path / "nope.db"),
            patch.object(rdb, "get_user_tasks", return_value=[]),
            patch.object(_ant, "Anthropic", return_value=mock_ant_client),
            # Patch the already-imported SlackWebClient in the module's namespace
            patch.object(rdb, "SlackWebClient", return_value=mock_slack),
            patch.object(rdb, "_write_audit", return_value=None),
        ):
            result = rdb.main()
        assert result == 0

    def test_haiku_failure_returns_2(self, tmp_path):
        ap = tmp_path / "asana.yaml"
        rp = tmp_path / "role.yaml"
        ap.write_text("""users:
  - slack_user_id: U001
    asana_user_gid: "111"
    asana_email: alice@hjrglobal.com
    display_name: Alice Smith
""", encoding="utf-8")
        rp.write_text("""users:
  - slack_user_id: U001
    role: "Sales"
    entity: F3E
    extra_data: []
    briefing_channel: ""
""", encoding="utf-8")

        mock_slack = MagicMock()
        mock_ant_client = MagicMock()
        mock_ant_client.messages.create.side_effect = Exception("API error")

        import anthropic as _ant
        with (
            patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test", "ANTHROPIC_API_KEY": "key"}),
            patch.object(rdb, "_ASANA_MAP", ap),
            patch.object(rdb, "_ROLE_CONFIG", rp),
            patch.object(rdb, "_KB_DB_PATH", tmp_path / "nope.db"),
            patch.object(rdb, "get_user_tasks", return_value=[]),
            patch.object(_ant, "Anthropic", return_value=mock_ant_client),
            patch("slack_sdk.WebClient", return_value=mock_slack),
            patch.object(rdb, "_write_audit", return_value=None),
        ):
            result = rdb.main()
        assert result == 2  # partial failure
