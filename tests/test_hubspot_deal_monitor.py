"""Tests for run_hubspot_deal_monitor -- HubSpot Deal Stage Change Notifications.

Coverage:
  - DB setup (tables created, schema correct)
  - Owner name resolution from slack-to-hubspot.yaml
  - Slack notification format and routing
  - Stage change detection (new deal, same stage, changed stage)
  - snapshot_and_diff happy path
  - snapshot_and_diff empty pipelines
  - snapshot_and_diff HubSpot API failure
  - snapshot_and_diff dry_run (no Slack post)
  - Pipeline -> channel routing
  - TOOL_DEFINITIONS / _TOOL_FUNCTIONS not affected
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))


# ---------------------------------------------------------------------------
# Import the module under test
# ---------------------------------------------------------------------------

import scripts.run_hubspot_deal_monitor as monitor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_deal(
    deal_id: str,
    name: str,
    stage_id: str,
    pipeline_id: str = "2313722582",
    amount: str = "5000",
    owner_id: str = "162944825",
) -> dict[str, Any]:
    return {
        "id": deal_id,
        "properties": {
            "dealname": name,
            "dealstage": stage_id,
            "pipeline": pipeline_id,
            "amount": amount,
            "hubspot_owner_id": owner_id,
            "hs_lastmodifieddate": None,
            "deal_currency_code": "USD",
        },
    }


_STAGE_IDENTIFY = "3760235201"
_STAGE_QUALIFIED = "3760235204"
_STAGE_PROPOSAL = "3760204497"
_STAGE_WON = "3760235206"

_MOCK_STAGE_CACHE = {
    _STAGE_IDENTIFY: "Identify",
    _STAGE_QUALIFIED: "Qualified",
    _STAGE_PROPOSAL: "Proposal",
    _STAGE_WON: "Closed Won",
}

_MOCK_HUBSPOT_MAP = {
    "users": [
        {
            "slack_user_id": "U001",
            "hubspot_owner_id": 162944825,
            "hubspot_email": "tommy@f3energy.com",
            "display_name": "Tommy Anderson",
        },
        {
            "slack_user_id": "U002",
            "hubspot_owner_id": 160459333,
            "hubspot_email": "harrison@hjrglobal.com",
            "display_name": "Harrison Rogers",
        },
    ]
}


# ---------------------------------------------------------------------------
# DB setup tests
# ---------------------------------------------------------------------------

class TestDatabaseSetup:
    def test_get_db_creates_tables(self, tmp_path):
        """_get_db creates both required tables."""
        db_path = tmp_path / "test.db"
        with patch.object(monitor, "_DB_PATH", db_path):
            conn = monitor._get_db()
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "deal_snapshots" in tables
        assert "deal_last_stage" in tables
        conn.close()

    def test_deal_snapshots_schema(self, tmp_path):
        """deal_snapshots has correct columns."""
        db_path = tmp_path / "test.db"
        with patch.object(monitor, "_DB_PATH", db_path):
            conn = monitor._get_db()
        cols = {row[1] for row in conn.execute("PRAGMA table_info(deal_snapshots)").fetchall()}
        assert "deal_id" in cols
        assert "stage_id" in cols
        assert "snapshot_ts" in cols
        assert "pipeline_id" in cols
        conn.close()

    def test_deal_last_stage_schema(self, tmp_path):
        """deal_last_stage has correct columns."""
        db_path = tmp_path / "test.db"
        with patch.object(monitor, "_DB_PATH", db_path):
            conn = monitor._get_db()
        cols = {row[1] for row in conn.execute("PRAGMA table_info(deal_last_stage)").fetchall()}
        assert "deal_id" in cols
        assert "stage_id" in cols
        assert "stage_name" in cols
        assert "deal_name" in cols
        conn.close()

    def test_idempotent_table_creation(self, tmp_path):
        """Calling _get_db twice doesn't fail."""
        db_path = tmp_path / "test.db"
        with patch.object(monitor, "_DB_PATH", db_path):
            conn1 = monitor._get_db()
            conn1.close()
            conn2 = monitor._get_db()
            conn2.close()


# ---------------------------------------------------------------------------
# Owner name resolution tests
# ---------------------------------------------------------------------------

class TestOwnerNameResolution:
    def setup_method(self):
        monitor._owner_id_to_name = None

    def test_resolves_known_owner(self, tmp_path):
        """Resolves display name for known owner ID."""
        yaml_path = tmp_path / "slack-to-hubspot.yaml"
        yaml_path.write_text(yaml.dump(_MOCK_HUBSPOT_MAP), encoding="utf-8")
        with patch.object(monitor, "_HUBSPOT_MAP_PATH", yaml_path):
            name = monitor._get_owner_name("162944825")
        assert name == "Tommy Anderson"

    def test_returns_empty_for_unknown_owner(self, tmp_path):
        """Returns empty string for unmapped owner ID."""
        yaml_path = tmp_path / "slack-to-hubspot.yaml"
        yaml_path.write_text(yaml.dump(_MOCK_HUBSPOT_MAP), encoding="utf-8")
        with patch.object(monitor, "_HUBSPOT_MAP_PATH", yaml_path):
            name = monitor._get_owner_name("99999999")
        assert name == ""

    def test_returns_empty_for_none(self):
        """Returns empty string for None owner_id."""
        name = monitor._get_owner_name(None)
        assert name == ""

    def test_yaml_load_failure_returns_empty(self, tmp_path):
        """Corrupt yaml file returns empty map."""
        monitor._owner_id_to_name = None
        yaml_path = tmp_path / "slack-to-hubspot.yaml"
        yaml_path.write_text("not: valid: yaml: [", encoding="utf-8")
        with patch.object(monitor, "_HUBSPOT_MAP_PATH", yaml_path):
            result = monitor._load_owner_names()
        assert result == {}

    def test_caches_after_first_load(self, tmp_path):
        """Map is loaded only once (cached in module-level var)."""
        monitor._owner_id_to_name = None
        yaml_path = tmp_path / "slack-to-hubspot.yaml"
        yaml_path.write_text(yaml.dump(_MOCK_HUBSPOT_MAP), encoding="utf-8")
        with patch.object(monitor, "_HUBSPOT_MAP_PATH", yaml_path):
            r1 = monitor._load_owner_names()
            r2 = monitor._load_owner_names()
        assert r1 is r2  # same object == cached


# ---------------------------------------------------------------------------
# Slack notification format tests
# ---------------------------------------------------------------------------

class TestPostStageChange:
    def test_posts_correct_channel(self):
        """Posts notification to specified channel."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp
        monitor._owner_id_to_name = {}

        with (
            patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test"}),
            patch("httpx.Client", return_value=mock_client),
        ):
            monitor._post_stage_change(
                channel="#f3-leadership",
                deal_id="123",
                deal_name="Test Deal",
                old_stage="Identify",
                new_stage="Qualified",
                amount="10000",
                owner_id=None,
            )

        call_kwargs = mock_client.post.call_args
        assert call_kwargs[1]["json"]["channel"] == "#f3-leadership"
        text = call_kwargs[1]["json"]["text"]
        assert "Test Deal" in text
        assert "Identify" in text
        assert "Qualified" in text

    def test_amount_formatted(self):
        """Amount is formatted as $X,XXX."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp
        monitor._owner_id_to_name = {}

        with (
            patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test"}),
            patch("httpx.Client", return_value=mock_client),
        ):
            monitor._post_stage_change(
                "#f3-leadership", "D1", "Deal", "A", "B", "10000", None
            )

        text = mock_client.post.call_args[1]["json"]["text"]
        assert "$10,000" in text

    def test_dry_run_skips_http(self):
        """dry_run=True does not make HTTP call."""
        monitor._owner_id_to_name = {}
        with (
            patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test"}),
            patch("httpx.Client") as mock_http,
        ):
            monitor._post_stage_change(
                "#f3-leadership", "D1", "Deal", "A", "B", "5000", None, dry_run=True
            )
        mock_http.assert_not_called()

    def test_missing_token_skips_post(self):
        """Missing SLACK_BOT_TOKEN skips HTTP call."""
        monitor._owner_id_to_name = {}
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("httpx.Client") as mock_http,
        ):
            import os
            os.environ.pop("SLACK_BOT_TOKEN", None)
            monitor._post_stage_change(
                "#f3-leadership", "D1", "Deal", "A", "B", "5000", None
            )
        mock_http.assert_not_called()

    def test_includes_owner_name(self):
        """Notification includes owner name when resolved."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp
        monitor._owner_id_to_name = {"162944825": "Tommy Anderson"}

        with (
            patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test"}),
            patch("httpx.Client", return_value=mock_client),
        ):
            monitor._post_stage_change(
                "#f3-leadership", "D1", "Deal A", "Identify", "Qualified", "5000", "162944825"
            )

        text = mock_client.post.call_args[1]["json"]["text"]
        assert "Tommy Anderson" in text


# ---------------------------------------------------------------------------
# Pipeline routing tests
# ---------------------------------------------------------------------------

class TestPipelineRouting:
    def test_f3e_pipeline_routes_to_f3_leadership(self):
        assert monitor._PIPELINE_CHANNEL["2313722582"] == "#f3-leadership"

    def test_default_pipeline_routes_to_hjrg_leadership(self):
        assert monitor._PIPELINE_CHANNEL["default"] == "#hjrg-leadership"


# ---------------------------------------------------------------------------
# snapshot_and_diff core logic tests
# ---------------------------------------------------------------------------

class TestSnapshotAndDiff:
    def setup_method(self):
        monitor._owner_id_to_name = {}

    def _patch_db(self, tmp_path):
        db_path = tmp_path / "test.db"
        return patch.object(monitor, "_DB_PATH", db_path)

    def test_new_deal_no_notification(self, tmp_path):
        """Brand-new deal (no previous record) is inserted without notification."""
        deals = [_make_deal("D1", "New Deal", _STAGE_IDENTIFY)]

        with (
            self._patch_db(tmp_path),
            patch("scripts.run_hubspot_deal_monitor._STAGE_NAME_CACHE", _MOCK_STAGE_CACHE),
            patch("scripts.run_hubspot_deal_monitor._refresh_pipeline_cache"),
            patch("scripts.run_hubspot_deal_monitor.get_deals_by_pipeline", return_value=deals),
            patch("scripts.run_hubspot_deal_monitor._post_stage_change") as mock_notify,
        ):
            result = monitor.snapshot_and_diff(dry_run=False)

        mock_notify.assert_not_called()
        assert result["deals_checked"] == 2  # same deals returned for both pipelines
        assert result["stage_changes"] == 0

    def test_same_stage_no_notification(self, tmp_path):
        """Deal with unchanged stage does not trigger notification."""
        deals = [_make_deal("D1", "Existing Deal", _STAGE_QUALIFIED)]

        # Pre-populate last_stage with same stage
        db_path = tmp_path / "test.db"
        with patch.object(monitor, "_DB_PATH", db_path):
            conn = monitor._get_db()
            conn.execute(
                "INSERT INTO deal_last_stage (deal_id, stage_id, stage_name, deal_name, pipeline_id, amount, owner_id, last_seen_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("D1", _STAGE_QUALIFIED, "Qualified", "Existing Deal", "2313722582", "5000", "", int(time.time())),
            )
            conn.commit()
            conn.close()

        with (
            patch.object(monitor, "_DB_PATH", db_path),
            patch("scripts.run_hubspot_deal_monitor._STAGE_NAME_CACHE", _MOCK_STAGE_CACHE),
            patch("scripts.run_hubspot_deal_monitor._refresh_pipeline_cache"),
            patch("scripts.run_hubspot_deal_monitor.get_deals_by_pipeline", return_value=deals),
            patch("scripts.run_hubspot_deal_monitor._post_stage_change") as mock_notify,
        ):
            result = monitor.snapshot_and_diff(dry_run=False)

        mock_notify.assert_not_called()
        assert result["stage_changes"] == 0

    def test_changed_stage_triggers_notification(self, tmp_path):
        """Deal with changed stage triggers Slack notification."""
        deals = [_make_deal("D1", "Moving Deal", _STAGE_QUALIFIED)]

        # Pre-populate with different (old) stage
        db_path = tmp_path / "test.db"
        with patch.object(monitor, "_DB_PATH", db_path):
            conn = monitor._get_db()
            conn.execute(
                "INSERT INTO deal_last_stage (deal_id, stage_id, stage_name, deal_name, pipeline_id, amount, owner_id, last_seen_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("D1", _STAGE_IDENTIFY, "Identify", "Moving Deal", "2313722582", "5000", "", int(time.time())),
            )
            conn.commit()
            conn.close()

        with (
            patch.object(monitor, "_DB_PATH", db_path),
            patch("scripts.run_hubspot_deal_monitor._STAGE_NAME_CACHE", _MOCK_STAGE_CACHE),
            patch("scripts.run_hubspot_deal_monitor._refresh_pipeline_cache"),
            patch("scripts.run_hubspot_deal_monitor.get_deals_by_pipeline", return_value=deals),
            patch("scripts.run_hubspot_deal_monitor._post_stage_change") as mock_notify,
        ):
            result = monitor.snapshot_and_diff(dry_run=False)

        assert mock_notify.called
        call_args = mock_notify.call_args
        assert call_args[1]["old_stage"] == "Identify"
        assert call_args[1]["new_stage"] == "Qualified"
        assert result["stage_changes"] >= 1

    def test_hubspot_api_failure_continues(self, tmp_path):
        """HubSpot API failure for one pipeline doesn't crash the whole run."""
        from cora.tools.hubspot_client import HubSpotClientError

        db_path = tmp_path / "test.db"
        with (
            patch.object(monitor, "_DB_PATH", db_path),
            patch("scripts.run_hubspot_deal_monitor._STAGE_NAME_CACHE", _MOCK_STAGE_CACHE),
            patch("scripts.run_hubspot_deal_monitor._refresh_pipeline_cache"),
            patch(
                "scripts.run_hubspot_deal_monitor.get_deals_by_pipeline",
                side_effect=HubSpotClientError("API error"),
            ),
        ):
            result = monitor.snapshot_and_diff(dry_run=False)

        assert result["deals_checked"] == 0
        assert result["stage_changes"] == 0

    def test_empty_pipeline_no_crash(self, tmp_path):
        """Empty pipeline returns zero counts without error."""
        with (
            self._patch_db(tmp_path),
            patch("scripts.run_hubspot_deal_monitor._STAGE_NAME_CACHE", _MOCK_STAGE_CACHE),
            patch("scripts.run_hubspot_deal_monitor._refresh_pipeline_cache"),
            patch("scripts.run_hubspot_deal_monitor.get_deals_by_pipeline", return_value=[]),
        ):
            result = monitor.snapshot_and_diff(dry_run=False)

        assert result["deals_checked"] == 0
        assert result["stage_changes"] == 0
        assert result["notifications_sent"] == 0

    def test_dry_run_detects_changes_no_slack(self, tmp_path):
        """dry_run=True detects stage changes but counts them without actually posting."""
        deals = [_make_deal("D1", "Deal", _STAGE_QUALIFIED)]
        db_path = tmp_path / "test.db"
        with patch.object(monitor, "_DB_PATH", db_path):
            conn = monitor._get_db()
            conn.execute(
                "INSERT INTO deal_last_stage (deal_id, stage_id, stage_name, deal_name, pipeline_id, amount, owner_id, last_seen_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("D1", _STAGE_IDENTIFY, "Identify", "Deal", "2313722582", "5000", "", int(time.time())),
            )
            conn.commit()
            conn.close()

        with (
            patch.object(monitor, "_DB_PATH", db_path),
            patch("scripts.run_hubspot_deal_monitor._STAGE_NAME_CACHE", _MOCK_STAGE_CACHE),
            patch("scripts.run_hubspot_deal_monitor._refresh_pipeline_cache"),
            patch("scripts.run_hubspot_deal_monitor.get_deals_by_pipeline", return_value=deals),
            patch("httpx.Client") as mock_http,
        ):
            result = monitor.snapshot_and_diff(dry_run=True)

        mock_http.assert_not_called()
        assert result["stage_changes"] >= 1

    def test_snapshot_stored_in_db(self, tmp_path):
        """Snapshot rows are inserted into deal_snapshots table."""
        deals = [_make_deal("D1", "Deal", _STAGE_IDENTIFY)]
        db_path = tmp_path / "test.db"

        with (
            patch.object(monitor, "_DB_PATH", db_path),
            patch("scripts.run_hubspot_deal_monitor._STAGE_NAME_CACHE", _MOCK_STAGE_CACHE),
            patch("scripts.run_hubspot_deal_monitor._refresh_pipeline_cache"),
            patch("scripts.run_hubspot_deal_monitor.get_deals_by_pipeline", return_value=deals),
            patch("scripts.run_hubspot_deal_monitor._post_stage_change"),
        ):
            monitor.snapshot_and_diff(dry_run=False)

        with patch.object(monitor, "_DB_PATH", db_path):
            conn = monitor._get_db()
            rows = conn.execute("SELECT * FROM deal_snapshots WHERE deal_id = 'D1'").fetchall()
            conn.close()
        assert len(rows) >= 1

    def test_last_stage_upserted(self, tmp_path):
        """deal_last_stage is updated after each run."""
        deals = [_make_deal("D1", "Deal", _STAGE_QUALIFIED)]
        db_path = tmp_path / "test.db"

        with (
            patch.object(monitor, "_DB_PATH", db_path),
            patch("scripts.run_hubspot_deal_monitor._STAGE_NAME_CACHE", _MOCK_STAGE_CACHE),
            patch("scripts.run_hubspot_deal_monitor._refresh_pipeline_cache"),
            patch("scripts.run_hubspot_deal_monitor.get_deals_by_pipeline", return_value=deals),
            patch("scripts.run_hubspot_deal_monitor._post_stage_change"),
        ):
            monitor.snapshot_and_diff(dry_run=False)

        with patch.object(monitor, "_DB_PATH", db_path):
            conn = monitor._get_db()
            row = conn.execute(
                "SELECT stage_id FROM deal_last_stage WHERE deal_id = 'D1'"
            ).fetchone()
            conn.close()
        assert row is not None
        assert row["stage_id"] == _STAGE_QUALIFIED

    def test_pipeline_cache_refreshed_when_empty(self, tmp_path):
        """_refresh_pipeline_cache is called when _STAGE_NAME_CACHE is empty."""
        with (
            self._patch_db(tmp_path),
            patch("scripts.run_hubspot_deal_monitor._STAGE_NAME_CACHE", {}),
            patch("scripts.run_hubspot_deal_monitor._refresh_pipeline_cache") as mock_refresh,
            patch("scripts.run_hubspot_deal_monitor.get_deals_by_pipeline", return_value=[]),
        ):
            monitor.snapshot_and_diff(dry_run=False)

        mock_refresh.assert_called_once()


# ---------------------------------------------------------------------------
# tool_dispatch not affected
# ---------------------------------------------------------------------------

class TestToolDispatchUnaffected:
    def test_tool_definitions_importable(self):
        """TOOL_DEFINITIONS can still be imported (not affected by new script)."""
        from cora.tools.tool_dispatch import TOOL_DEFINITIONS
        assert isinstance(TOOL_DEFINITIONS, list)

    def test_tool_functions_importable(self):
        """_TOOL_FUNCTIONS can still be imported."""
        from cora.tools.tool_dispatch import _TOOL_FUNCTIONS
        assert isinstance(_TOOL_FUNCTIONS, dict)