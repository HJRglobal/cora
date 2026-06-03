"""Tests for scripts/run_deal_aging_alerts.py.

Coverage:
  - STAGE_THRESHOLDS lookup for known + unknown stages
  - _is_throttled: suppresses re-alert within 3 days, allows after 3+ days
  - _get_slack_id_for_owner: correct mapping, missing mapping
  - _build_alert_text: format, all fields present
  - _parse_hs_date: ISO-8601, epoch-ms, None, empty string
  - _deal_age_days: uses hs_lastmodifieddate, fallback to createdate, missing both
  - run_aging_alerts: dry_run mode, empty pipeline, alert sent, throttled,
    fallback channel, no token, pipeline fetch error
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

# ---------------------------------------------------------------------------
# Import target module
# ---------------------------------------------------------------------------

import importlib
import types


def _import_module():
    """Import run_deal_aging_alerts with dotenv + external side-effects mocked."""
    # Stub dotenv so load_dotenv() is a no-op during import
    dotenv_mod = types.ModuleType("dotenv")
    dotenv_mod.load_dotenv = lambda *a, **kw: None
    sys.modules.setdefault("dotenv", dotenv_mod)

    spec = importlib.util.spec_from_file_location(
        "run_deal_aging_alerts",
        _REPO_ROOT / "scripts" / "run_deal_aging_alerts.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _import_module()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_deal(
    deal_id: str,
    name: str,
    stage_id: str,
    stage_name: str,
    last_modified: str | None = None,
    create_date: str | None = None,
    amount: str = "5000",
    owner_id: str = "160459333",
) -> dict[str, Any]:
    return {
        "id": deal_id,
        "properties": {
            "dealname": name,
            "dealstage": stage_id,
            "amount": amount,
            "hubspot_owner_id": owner_id,
            "hs_lastmodifieddate": last_modified,
            "createdate": create_date,
        },
    }


_NOW = time.time()
_30_DAYS_AGO = _NOW - (30 * 86400)
_5_DAYS_AGO = _NOW - (5 * 86400)
_3_DAYS_AGO_MINUS_1 = _NOW - (3 * 86400 - 60)  # just under 3d -- throttled
_3_DAYS_AGO_PLUS_1 = _NOW - (3 * 86400 + 60)   # just over 3d -- NOT throttled

# Convert timestamps to ISO strings for deal properties
def _ts_to_iso(ts: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# ---------------------------------------------------------------------------
# STAGE_THRESHOLDS tests
# ---------------------------------------------------------------------------

class TestStageThresholds:
    def test_identify_threshold(self):
        assert _mod.STAGE_THRESHOLDS["Identify"] == 14

    def test_outreach_threshold(self):
        assert _mod.STAGE_THRESHOLDS["Outreach"] == 10

    def test_sample_sent_threshold(self):
        assert _mod.STAGE_THRESHOLDS["Sample Sent"] == 7

    def test_qualified_threshold(self):
        assert _mod.STAGE_THRESHOLDS["Qualified"] == 21

    def test_proposal_threshold(self):
        assert _mod.STAGE_THRESHOLDS["Proposal"] == 14

    def test_negotiation_threshold(self):
        assert _mod.STAGE_THRESHOLDS["Negotiation"] == 7

    def test_default_threshold_for_unknown_stage(self):
        assert _mod._get_threshold("Some Unknown Stage") == _mod.DEFAULT_THRESHOLD

    def test_default_threshold_is_21(self):
        assert _mod.DEFAULT_THRESHOLD == 21

    def test_get_threshold_known_stage(self):
        assert _mod._get_threshold("Outreach") == 10

    def test_get_threshold_empty_string(self):
        assert _mod._get_threshold("") == _mod.DEFAULT_THRESHOLD


# ---------------------------------------------------------------------------
# Throttle tests
# ---------------------------------------------------------------------------

class TestThrottle:
    def test_not_throttled_when_no_entry(self):
        assert _mod._is_throttled("deal_1", {}, _NOW) is False

    def test_throttled_within_3_days(self):
        state = {"deal_1": _3_DAYS_AGO_MINUS_1}
        assert _mod._is_throttled("deal_1", state, _NOW) is True

    def test_not_throttled_after_3_days(self):
        state = {"deal_1": _3_DAYS_AGO_PLUS_1}
        assert _mod._is_throttled("deal_1", state, _NOW) is False

    def test_not_throttled_for_different_deal(self):
        state = {"deal_1": _3_DAYS_AGO_MINUS_1}
        assert _mod._is_throttled("deal_2", state, _NOW) is False

    def test_throttle_window_is_259200_seconds(self):
        assert _mod._THROTTLE_SECONDS == 259200

    def test_exactly_at_throttle_boundary_is_throttled(self):
        state = {"deal_1": _NOW - _mod._THROTTLE_SECONDS + 1}
        assert _mod._is_throttled("deal_1", state, _NOW) is True


# ---------------------------------------------------------------------------
# _parse_hs_date tests
# ---------------------------------------------------------------------------

class TestParseHsDate:
    def test_none_returns_none(self):
        assert _mod._parse_hs_date(None) is None

    def test_empty_string_returns_none(self):
        assert _mod._parse_hs_date("") is None

    def test_iso_8601_string(self):
        result = _mod._parse_hs_date("2026-01-01T00:00:00.000Z")
        assert result is not None
        assert result > 0

    def test_epoch_ms_string(self):
        ts_ms = int(_NOW * 1000)
        result = _mod._parse_hs_date(str(ts_ms))
        assert result is not None
        assert abs(result - _NOW) < 2.0

    def test_unparseable_string_returns_none(self):
        assert _mod._parse_hs_date("not-a-date") is None


# ---------------------------------------------------------------------------
# _deal_age_days tests
# ---------------------------------------------------------------------------

class TestDealAgeDays:
    def test_uses_last_modified_date(self):
        props = {"hs_lastmodifieddate": _ts_to_iso(_30_DAYS_AGO), "createdate": _ts_to_iso(_5_DAYS_AGO)}
        age = _mod._deal_age_days(props, _NOW)
        assert 29 < age < 31

    def test_falls_back_to_createdate(self):
        props = {"hs_lastmodifieddate": None, "createdate": _ts_to_iso(_5_DAYS_AGO)}
        age = _mod._deal_age_days(props, _NOW)
        assert 4.9 < age < 5.1

    def test_returns_zero_when_no_dates(self):
        props = {}
        assert _mod._deal_age_days(props, _NOW) == 0.0


# ---------------------------------------------------------------------------
# Owner -> Slack ID resolution tests
# ---------------------------------------------------------------------------

class TestOwnerToSlack:
    def test_known_owner_returns_slack_id(self):
        with patch.object(_mod, "_load_owner_to_slack",
                          return_value={"160459333": "U0B2RM2JYJ1", "162944825": "U0B3RU5Q55G"}):
            assert _mod._get_slack_id_for_owner("160459333") == "U0B2RM2JYJ1"

    def test_unknown_owner_returns_none(self):
        with patch.object(_mod, "_load_owner_to_slack", return_value={"160459333": "U0B2RM2JYJ1"}):
            assert _mod._get_slack_id_for_owner("999999999") is None

    def test_none_owner_returns_none(self):
        assert _mod._get_slack_id_for_owner(None) is None


# ---------------------------------------------------------------------------
# _build_alert_text tests
# ---------------------------------------------------------------------------

class TestBuildAlertText:
    def test_contains_deal_name(self):
        text = _mod._build_alert_text("Acme Corp", "12345", "Proposal", 20.0, 14, "5000", "F3E Retail")
        assert "Acme Corp" in text

    def test_contains_stage_name(self):
        text = _mod._build_alert_text("Deal A", "12345", "Negotiation", 10.0, 7, "2000", "F3E Retail")
        assert "Negotiation" in text

    def test_contains_age_days(self):
        text = _mod._build_alert_text("Deal A", "12345", "Outreach", 15.0, 10, "1000", "F3E Retail")
        assert "15d" in text

    def test_contains_threshold(self):
        text = _mod._build_alert_text("Deal A", "12345", "Outreach", 15.0, 10, "1000", "F3E Retail")
        assert "10d" in text

    def test_contains_amount(self):
        text = _mod._build_alert_text("Deal A", "12345", "Outreach", 15.0, 10, "5000", "F3E Retail")
        assert "$5,000" in text

    def test_contains_pipeline_name(self):
        text = _mod._build_alert_text("Deal A", "12345", "Outreach", 15.0, 10, "1000", "F3E Retail")
        assert "F3E Retail" in text

    def test_contains_hubspot_url(self):
        text = _mod._build_alert_text("Deal A", "12345", "Outreach", 15.0, 10, "1000", "F3E Retail")
        assert "12345" in text
        assert "hubspot" in text.lower()

    def test_none_amount_shows_na(self):
        text = _mod._build_alert_text("Deal A", "12345", "Outreach", 15.0, 10, None, "F3E Retail")
        assert "N/A" in text


# ---------------------------------------------------------------------------
# run_aging_alerts tests
# ---------------------------------------------------------------------------

def _patch_caches():
    """Pre-populate the shared hubspot_client cache dicts so tests don't need live API calls."""
    from cora.tools import hubspot_client as hc
    hc._STAGE_NAME_CACHE.update({"stage_outreach": "Outreach", "stage_closed_won": "Closed Won"})
    hc._PIPELINE_NAME_CACHE.update({"2313722582": "F3E Retail", "default": "Default Pipeline"})


class TestRunAgingAlerts:
    def setup_method(self):
        _patch_caches()

    def _make_stale_deal(self, deal_id: str = "D1", stage_name: str = "Outreach") -> dict:
        return _make_deal(
            deal_id=deal_id,
            name="Test Deal",
            stage_id="stage_outreach",
            stage_name=stage_name,
            last_modified=_ts_to_iso(_30_DAYS_AGO),
        )

    @patch.object(_mod, "get_deals_by_pipeline")
    @patch.object(_mod, "_refresh_pipeline_cache")
    @patch.object(_mod, "_send_slack_dm", return_value=True)
    @patch.object(_mod, "_get_slack_id_for_owner", return_value="U_OWNER")
    @patch.object(_mod, "_load_throttle", return_value={})
    @patch.object(_mod, "_save_throttle")
    def test_alert_sent_for_stale_deal(self, mock_save, mock_load_throttle,
                                       mock_owner, mock_dm, mock_refresh, mock_get_deals):
        mock_get_deals.return_value = [self._make_stale_deal()]
        result = _mod.run_aging_alerts(dry_run=False)
        assert result["alerts_sent"] >= 1

    @patch.object(_mod, "get_deals_by_pipeline")
    @patch.object(_mod, "_refresh_pipeline_cache")
    @patch.object(_mod, "_send_slack_dm", return_value=True)
    @patch.object(_mod, "_get_slack_id_for_owner", return_value="U_OWNER")
    @patch.object(_mod, "_load_throttle", return_value={})
    @patch.object(_mod, "_save_throttle")
    def test_dry_run_does_not_save_throttle(self, mock_save, mock_load_throttle,
                                             mock_owner, mock_dm, mock_refresh, mock_get_deals):
        mock_get_deals.return_value = [self._make_stale_deal()]
        _mod.run_aging_alerts(dry_run=True)
        mock_save.assert_not_called()

    @patch.object(_mod, "get_deals_by_pipeline")
    @patch.object(_mod, "_refresh_pipeline_cache")
    @patch.object(_mod, "_send_slack_dm", return_value=True)
    @patch.object(_mod, "_get_slack_id_for_owner", return_value="U_OWNER")
    @patch.object(_mod, "_load_throttle")
    @patch.object(_mod, "_save_throttle")
    def test_throttled_deal_not_re_alerted(self, mock_save, mock_load_throttle,
                                            mock_owner, mock_dm, mock_refresh, mock_get_deals):
        mock_load_throttle.return_value = {"D1": _3_DAYS_AGO_MINUS_1}
        mock_get_deals.return_value = [self._make_stale_deal("D1")]
        result = _mod.run_aging_alerts(dry_run=False)
        assert result["throttled"] >= 1
        mock_dm.assert_not_called()

    @patch.object(_mod, "get_deals_by_pipeline")
    @patch.object(_mod, "_refresh_pipeline_cache")
    def test_empty_pipeline_returns_zero_checked(self, mock_refresh, mock_get_deals):
        mock_get_deals.return_value = []
        result = _mod.run_aging_alerts(dry_run=True)
        assert result["deals_checked"] == 0
        assert result["alerts_sent"] == 0

    @patch.object(_mod, "get_deals_by_pipeline")
    @patch.object(_mod, "_refresh_pipeline_cache")
    @patch.object(_mod, "_send_fallback_channel", return_value=True)
    @patch.object(_mod, "_get_slack_id_for_owner", return_value=None)
    @patch.object(_mod, "_load_throttle", return_value={})
    @patch.object(_mod, "_save_throttle")
    def test_fallback_channel_when_no_slack_id(self, mock_save, mock_load_throttle,
                                                mock_owner, mock_fallback, mock_refresh, mock_get_deals):
        mock_get_deals.return_value = [self._make_stale_deal()]
        result = _mod.run_aging_alerts(dry_run=False)
        assert result["alerts_sent"] >= 1
        mock_fallback.assert_called()

    @patch.object(_mod, "_refresh_pipeline_cache")
    @patch.object(_mod, "get_deals_by_pipeline")
    def test_pipeline_fetch_error_does_not_crash(self, mock_get_deals, mock_refresh):
        from cora.tools.hubspot_client import HubSpotClientError
        mock_get_deals.side_effect = HubSpotClientError("network error")
        result = _mod.run_aging_alerts(dry_run=True)
        assert isinstance(result, dict)
