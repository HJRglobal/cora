"""Tests for ads_client.py -- ad performance tool functions.

Pattern mirrors test_financial_client.py: mock polar_client.generate_report,
verify formatting + behavioral contract (UNKNOWN_RESPONSE, audit log, throttle).

Run (Windows): cd C:/Users/Harri/code/cora && .venv/Scripts/python.exe -m pytest tests/test_ads_client.py -v
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── Import under test ──────────────────────────────────────────────────────
from cora.tools import ads_client
from cora.connectors.polar_client import PolarConnectorError, PolarReport


# ────────────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def tmp_data_dirs(tmp_path, monkeypatch):
    """Redirect cache, log, and snapshot paths to tmp_path so tests don't
    write to the real repo and each test starts clean."""
    # Patch _repo_root() to return tmp_path
    monkeypatch.setattr(ads_client, "_repo_root", lambda: tmp_path)
    # Create expected subdirectories
    (tmp_path / "data" / "cache").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "snapshots" / "ads" / "manus-insights").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    yield tmp_path


@pytest.fixture()
def mock_polar_ok():
    """Return a PolarReport with reasonable test values."""
    report = PolarReport(
        query_id="test-qid",
        table_data=[
            {
                "total_marketing_spend": 2000.0,
                "blended_roas": 4.2,
                "acquisition_roas": 1.8,
                "poas": 3.1,
                "blended_cac": 42.0,
                "paid_roas": 3.9,
                "paid_cpa": 38.0,
                "custom_60638": 7500.0,
                "amazonads_campaign.raw.cost": 300.0,
                "acos": 22.5,
                "amazonsp_order_items.computed.net_sales_amazon": 1330.0,
            }
        ],
        total_data={
            "total_marketing_spend": 2000.0,
            "blended_roas": 4.2,
            "acquisition_roas": 1.8,
            "poas": 3.1,
            "blended_cac": 42.0,
            "paid_roas": 3.9,
            "paid_cpa": 38.0,
            "custom_60638": 7500.0,
            "amazonads_campaign.raw.cost": 300.0,
            "acos": 22.5,
            "amazonsp_order_items.computed.net_sales_amazon": 1330.0,
        },
        deep_link="https://app.polaranalytics.com/custom/create?test",
        date_from="2026-04-23",
        date_to="2026-05-22",
        metrics=[],
        dimensions=[],
    )
    return report


@pytest.fixture()
def mock_polar_channel_ok():
    """PolarReport with channel dimension rows."""
    rows = [
        {
            "custom_internal-default-channel-grouping": "Paid Social",
            "total_marketing_spend": 1500.0,
            "paid_roas": 4.1,
            "blended_roas": 4.3,
            "blended_cac": 38.0,
            "paid_cpa": 35.0,
            "custom_60638": 5500.0,
        },
        {
            "custom_internal-default-channel-grouping": "Paid Search",
            "total_marketing_spend": 500.0,
            "paid_roas": 3.2,
            "blended_roas": 3.4,
            "blended_cac": 55.0,
            "paid_cpa": 50.0,
            "custom_60638": 1500.0,
        },
    ]
    totals = {
        "total_marketing_spend": 2000.0,
        "blended_roas": 4.0,
        "paid_roas": 3.9,
    }
    return PolarReport(
        query_id="ch-qid",
        table_data=rows,
        total_data=totals,
        deep_link="",
        date_from="2026-04-23",
        date_to="2026-05-22",
        metrics=[],
        dimensions=["custom_internal-default-channel-grouping"],
    )


# ────────────────────────────────────────────────────────────────────────────
# Tool 1 — Performance summary
# ────────────────────────────────────────────────────────────────────────────

class TestGetPerformanceSummary:
    def test_happy_path_contains_key_metrics(self, mock_polar_ok):
        with patch("cora.tools.ads_client.generate_report", return_value=mock_polar_ok):
            result = ads_client.get_performance_summary_text(
                lookback_days=30, channel="F3E", user="U123"
            )
        assert "2,000" in result          # spend
        assert "4.20x" in result          # blended ROAS
        assert "$42" in result            # CAC
        assert "7,500" in result          # net revenue after ads

    def test_amazon_block_appears(self, mock_polar_ok):
        with patch("cora.tools.ads_client.generate_report", return_value=mock_polar_ok):
            result = ads_client.get_performance_summary_text(
                lookback_days=30, channel="F3E", user="U123"
            )
        assert "Amazon" in result
        assert "300" in result   # amz spend

    def test_source_opacity_no_platform_names(self, mock_polar_ok):
        """Output must not name underlying ad platforms."""
        with patch("cora.tools.ads_client.generate_report", return_value=mock_polar_ok):
            result = ads_client.get_performance_summary_text(
                lookback_days=30, channel="F3E", user="U123"
            )
        forbidden = ["Meta", "Facebook", "TikTok", "Google", "Polar", "Shopify", "Recharge"]
        for word in forbidden:
            assert word not in result, f"Platform name '{word}' leaked into output"

    def test_polar_error_returns_unknown_response(self):
        with patch(
            "cora.tools.ads_client.generate_report",
            side_effect=PolarConnectorError("API key missing"),
        ):
            with patch("cora.tools.ads_client.notify_gap", return_value=ads_client.UNKNOWN_RESPONSE):
                result = ads_client.get_performance_summary_text(
                    lookback_days=30, channel="F3E", user="U123"
                )
        assert result == ads_client.UNKNOWN_RESPONSE

    def test_audit_log_written_on_success(self, tmp_path, mock_polar_ok):
        with patch("cora.tools.ads_client.generate_report", return_value=mock_polar_ok):
            ads_client.get_performance_summary_text(
                lookback_days=30, channel="F3E", user="U456"
            )
        log_path = tmp_path / "logs" / "cora-ads-queries.jsonl"
        assert log_path.exists()
        records = [json.loads(line) for line in log_path.read_text().splitlines() if line]
        assert any(r["tool"] == "ads_get_performance_summary" and r["outcome"] == "ok"
                   for r in records)

    def test_targets_from_manus_snapshot(self, tmp_path, mock_polar_ok):
        """When Manus snapshot has targets, they're used in formatting."""
        snap_path = tmp_path / "data" / "snapshots" / "ads" / "manus-insights" / "latest.yaml"
        import yaml as _yaml
        snap_path.write_text(_yaml.dump({
            "refreshed": "2026-05-23",
            "reviewed_by": "Harrison",
            "targets": {
                "blended_roas_floor": 3.5,
                "nc_roas_target": 1.0,
                "cac_ceiling_usd": 50,
                "cm3_floor_pct": 15.0,
                "amazon_acos_target": 65.0,
            },
        }))
        with patch("cora.tools.ads_client.generate_report", return_value=mock_polar_ok):
            result = ads_client.get_performance_summary_text(
                lookback_days=30, channel="F3E", user="U789"
            )
        # ROAS 4.20 >= floor 3.5 → should show ✓
        assert "✓" in result


# ────────────────────────────────────────────────────────────────────────────
# Tool 2 — Channel breakdown
# ────────────────────────────────────────────────────────────────────────────

class TestGetChannelBreakdown:
    def test_happy_path_shows_channel_rows(self, mock_polar_channel_ok):
        with patch("cora.tools.ads_client.generate_report", return_value=mock_polar_channel_ok):
            result = ads_client.get_channel_breakdown_text(
                lookback_days=30, channel="F3E", user="U123"
            )
        assert "Paid Social" in result
        assert "Paid Search" in result
        assert "1,500" in result   # paid social spend
        assert "500" in result     # paid search spend

    def test_no_platform_names_in_channel_output(self, mock_polar_channel_ok):
        with patch("cora.tools.ads_client.generate_report", return_value=mock_polar_channel_ok):
            result = ads_client.get_channel_breakdown_text(
                lookback_days=30, channel="F3E", user="U123"
            )
        # Channel labels from custom dimension are fine ("Paid Social" etc)
        # but underlying platform names should not appear
        for word in ["Facebook", "Meta Ads", "Google Ads", "TikTok Ads"]:
            assert word not in result, f"Platform name '{word}' leaked"

    def test_empty_table_returns_no_data_message(self):
        empty_report = PolarReport(
            query_id="x", table_data=[], total_data={},
            deep_link="", date_from="2026-04-23", date_to="2026-05-22",
            metrics=[], dimensions=[],
        )
        with patch("cora.tools.ads_client.generate_report", return_value=empty_report):
            result = ads_client.get_channel_breakdown_text(
                lookback_days=30, channel="F3E", user="U123"
            )
        assert "No channel data" in result


# ────────────────────────────────────────────────────────────────────────────
# Tool 3 — Sub-brand performance
# ────────────────────────────────────────────────────────────────────────────

class TestGetSubbrandPerformance:
    def test_happy_path_shows_brand_rows(self):
        rows = [
            {"custom_5621": "F3 Energy", "total_marketing_spend": 1200.0, "blended_roas": 4.5, "blended_cac": 38.0},
            {"custom_5621": "F3 Pure", "total_marketing_spend": 600.0, "blended_roas": 3.8, "blended_cac": 44.0},
            {"custom_5621": "F3 Mood", "total_marketing_spend": 200.0, "blended_roas": 2.9, "blended_cac": 60.0},
        ]
        report = PolarReport(
            query_id="sb-qid",
            table_data=rows,
            total_data={"total_marketing_spend": 2000.0, "blended_roas": 4.1},
            deep_link="",
            date_from="2026-04-23", date_to="2026-05-22",
            metrics=[], dimensions=["custom_5621"],
        )
        with patch("cora.tools.ads_client.generate_report", return_value=report):
            result = ads_client.get_subbrand_performance_text(
                lookback_days=30, channel="F3E", user="U123"
            )
        assert "F3 Energy" in result
        assert "F3 Pure" in result
        assert "F3 Mood" in result


# ────────────────────────────────────────────────────────────────────────────
# Tool 4 — Pixel attribution
# ────────────────────────────────────────────────────────────────────────────

class TestGetPixelAttribution:
    def test_happy_path_shows_pixel_and_gap(self):
        report = PolarReport(
            query_id="px-qid",
            table_data=[{
                "pixel_roas": 3.8, "pixel_paid_roas": 3.5,
                "pixel_cac": 44.0, "pixel_paid_cac": 41.0,
                "pixel_paid_cost_per_order": 38.0,
                "paid_roas": 5.2,   # platform over-reports vs pixel 3.5
                "total_marketing_spend": 2000.0,
            }],
            total_data={
                "pixel_roas": 3.8, "pixel_paid_roas": 3.5,
                "pixel_cac": 44.0, "pixel_paid_cac": 41.0,
                "pixel_paid_cost_per_order": 38.0,
                "paid_roas": 5.2,
                "total_marketing_spend": 2000.0,
            },
            deep_link="",
            date_from="2026-04-23", date_to="2026-05-22",
            metrics=[], dimensions=[],
        )
        with patch("cora.tools.ads_client.generate_report", return_value=report):
            result = ads_client.get_pixel_attribution_text(
                lookback_days=30, channel="F3E", user="U123"
            )
        assert "3.80x" in result       # pixel ROAS blended
        assert "3.50x" in result       # pixel ROAS paid
        assert "5.20x" in result       # platform reported
        assert "Attribution gap" in result
        assert "+1.70x" in result      # delta: 5.2 - 3.5 = 1.7

    def test_no_platform_names(self):
        report = PolarReport(
            query_id="px-qid2",
            table_data=[{"pixel_roas": 3.0, "paid_roas": 4.0, "total_marketing_spend": 1000.0}],
            total_data={"pixel_roas": 3.0, "paid_roas": 4.0, "total_marketing_spend": 1000.0},
            deep_link="", date_from="2026-04-23", date_to="2026-05-22",
            metrics=[], dimensions=[],
        )
        with patch("cora.tools.ads_client.generate_report", return_value=report):
            result = ads_client.get_pixel_attribution_text(
                lookback_days=30, channel="F3E", user="U123"
            )
        # "Pixel" as a measurement label is acceptable ("Pixel ROAS").
        # What must not appear are specific platform/tool brand names.
        for word in ["Meta", "Facebook", "Google", "TikTok", "Polar Pixel", "polaranalytics"]:
            assert word not in result, f"Platform/tool name '{word}' leaked"


# ────────────────────────────────────────────────────────────────────────────
# Tool 5 — CM waterfall
# ────────────────────────────────────────────────────────────────────────────

class TestGetCmWaterfall:
    def test_happy_path_shows_all_cm_levels(self):
        report = PolarReport(
            query_id="cm-qid",
            table_data=[{
                "contribution_margin_1": 18000.0,
                "contribution_margin_1_ratio": 60.0,
                "contribution_margin_2": 12000.0,
                "contribution_margin_2_ratio": 40.0,
                "contribution_margin_3": 6000.0,
                "contribution_margin_3_ratio": 20.0,
                "contribution_margin_4": 3000.0,
                "contribution_margin_4_ratio": 10.0,
            }],
            total_data={
                "contribution_margin_1": 18000.0,
                "contribution_margin_1_ratio": 60.0,
                "contribution_margin_2": 12000.0,
                "contribution_margin_2_ratio": 40.0,
                "contribution_margin_3": 6000.0,
                "contribution_margin_3_ratio": 20.0,
                "contribution_margin_4": 3000.0,
                "contribution_margin_4_ratio": 10.0,
            },
            deep_link="",
            date_from="2026-04-23", date_to="2026-05-22",
            metrics=[], dimensions=[],
        )
        with patch("cora.tools.ads_client.generate_report", return_value=report):
            result = ads_client.get_cm_waterfall_text(
                lookback_days=30, channel="F3E", user="U123"
            )
        assert "CM1" in result
        assert "CM2" in result
        assert "CM3" in result
        assert "CM4" in result
        assert "18,000" in result
        assert "20.0%" in result   # CM3 %

    def test_cm3_target_indicator_from_snapshot(self, tmp_path):
        import yaml as _yaml
        snap_path = tmp_path / "data" / "snapshots" / "ads" / "manus-insights" / "latest.yaml"
        snap_path.write_text(_yaml.dump({
            "targets": {"cm3_floor_pct": 15.0},
        }))
        report = PolarReport(
            query_id="cm-qid2",
            table_data=[{
                "contribution_margin_3": 5000.0,
                "contribution_margin_3_ratio": 20.0,  # 20 >= 15 → ✓
            }],
            total_data={
                "contribution_margin_3": 5000.0,
                "contribution_margin_3_ratio": 20.0,
            },
            deep_link="", date_from="2026-04-23", date_to="2026-05-22",
            metrics=[], dimensions=[],
        )
        with patch("cora.tools.ads_client.generate_report", return_value=report):
            result = ads_client.get_cm_waterfall_text(
                lookback_days=30, channel="F3E", user="U123"
            )
        assert "✓" in result   # CM3 above floor


# ────────────────────────────────────────────────────────────────────────────
# Throttle + gap notification
# ────────────────────────────────────────────────────────────────────────────

class TestThrottleAndGapNotification:
    def test_throttle_prevents_duplicate_notifications(self, tmp_path):
        topic = "test ads topic"
        assert not ads_client.is_throttled(topic)

        with patch("cora.tools.ads_client.SlackWebClient") as mock_slack:
            mock_client = MagicMock()
            mock_slack.return_value = mock_client
            with patch.dict(os.environ, {"SLACK_BOT_TOKEN": "xoxb-test"}):
                ads_client.notify_gap(topic, "F3E", "U123")
                assert mock_client.chat_postMessage.call_count == 1
                # Second call — should be throttled
                ads_client.notify_gap(topic, "F3E", "U123")
                assert mock_client.chat_postMessage.call_count == 1  # still 1

        assert ads_client.is_throttled(topic)

    def test_gap_notification_returns_unknown_response(self, tmp_path):
        with patch("cora.tools.ads_client.SlackWebClient") as mock_slack:
            mock_client = MagicMock()
            mock_slack.return_value = mock_client
            with patch.dict(os.environ, {"SLACK_BOT_TOKEN": "xoxb-test"}):
                result = ads_client.notify_gap("some topic", "F3E", "U123")
        assert result == ads_client.UNKNOWN_RESPONSE

    def test_unknown_response_text_is_exact(self):
        """UNKNOWN_RESPONSE must match exact locked contract."""
        assert ads_client.UNKNOWN_RESPONSE.startswith("I don't have that right now.")
        assert "marketing team" in ads_client.UNKNOWN_RESPONSE


# ────────────────────────────────────────────────────────────────────────────
# Manus snapshot
# ────────────────────────────────────────────────────────────────────────────

class TestManusSnapshot:
    def test_missing_snapshot_returns_empty_dict(self, tmp_path):
        # tmp_data_dirs fixture redirects _repo_root, snapshot doesn't exist
        result = ads_client._load_manus_snapshot()
        assert result == {}

    def test_valid_snapshot_loads_correctly(self, tmp_path):
        import yaml as _yaml
        snap = {
            "refreshed": "2026-05-23",
            "reviewed_by": "Harrison",
            "targets": {"blended_roas_floor": 3.5, "cac_ceiling_usd": 50},
            "active_campaigns": [{"name": "Test Campaign"}],
            "strategy_notes": "Some notes here.",
        }
        snap_path = tmp_path / "data" / "snapshots" / "ads" / "manus-insights" / "latest.yaml"
        snap_path.write_text(_yaml.dump(snap))
        result = ads_client._load_manus_snapshot()
        assert result["targets"]["blended_roas_floor"] == 3.5
        assert result["active_campaigns"][0]["name"] == "Test Campaign"

    def test_malformed_yaml_returns_empty_dict(self, tmp_path):
        snap_path = tmp_path / "data" / "snapshots" / "ads" / "manus-insights" / "latest.yaml"
        snap_path.write_text(": invalid: yaml: {{{ broken")
        result = ads_client._load_manus_snapshot()
        assert isinstance(result, dict)


# ────────────────────────────────────────────────────────────────────────────
# Formatters (unit tests)
# ────────────────────────────────────────────────────────────────────────────

class TestFormatters:
    def test_fmt_currency_basic(self):
        assert ads_client._fmt_currency(2092.27) == "$2,092"
        assert ads_client._fmt_currency(None) == "n/a"
        assert ads_client._fmt_currency(0) == "$0"

    def test_fmt_x_basic(self):
        assert ads_client._fmt_x(4.20) == "4.20x"
        assert ads_client._fmt_x(None) == "n/a"

    def test_fmt_pct_basic(self):
        assert ads_client._fmt_pct(20.0) == "20.0%"
        assert ads_client._fmt_pct(None) == "n/a"

    def test_fmt_delta_above_target(self):
        result = ads_client._fmt_delta(4.2, ads_client._fmt_x, 3.5, higher_is_better=True)
        assert "✓" in result

    def test_fmt_delta_below_target(self):
        result = ads_client._fmt_delta(2.8, ads_client._fmt_x, 3.5, higher_is_better=True)
        assert "↓" in result

    def test_fmt_delta_cac_below_ceiling(self):
        # CAC $42 < ceiling $50 → good → ✓
        result = ads_client._fmt_delta(42.0, ads_client._fmt_currency, 50, higher_is_better=False)
        assert "✓" in result


    def test_fmt_delta_cac_above_ceiling(self):
        # CAC $65 > ceiling $50 -- bad -- shows up arrow
        result = ads_client._fmt_delta(65.0, ads_client._fmt_currency, 50, higher_is_better=False)
        assert chr(8593) in result  # ↑
