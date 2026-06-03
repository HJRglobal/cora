"""Tests for Feature #9: Inventory + Reorder Alerts (OSN + F3E)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest


# ---------------------------------------------------------------------------
# Import module under test
# ---------------------------------------------------------------------------

def _import_module():
    import importlib.util, sys
    spec = importlib.util.spec_from_file_location(
        "run_inventory_alerts",
        Path(__file__).resolve().parents[1] / "scripts" / "run_inventory_alerts.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_inventory_alerts"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _import_module()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_slack():
    slack = MagicMock()
    slack.chat_postMessage.return_value = {"ok": True}
    return slack


def _make_inv_item(name="Protein", qty=5, low_stock=True):
    from cora.connectors.clover_client import InventoryItem
    return InventoryItem(name=name, sku="SKU1", qty_on_hand=qty, low_stock=low_stock, price_usd=29.99)


def _make_store_summary(store_code="GW", store_name="Gilbert & Warner", low_stock_items=None):
    from cora.connectors.clover_client import StoreInventorySummary
    return StoreInventorySummary(
        store_code=store_code,
        store_name=store_name,
        total_items=50,
        low_stock_items=low_stock_items or [],
    )


def _thresholds():
    return {
        "osn": [
            {"item": "Protein", "alert_below": 20},
            {"item": "Pre-Workout", "alert_below": 15},
        ],
        "f3e": [
            {"sku": "F3 Energy", "alert_below": 200},
        ],
    }


# ---------------------------------------------------------------------------
# _is_flagged_line tests
# ---------------------------------------------------------------------------

class TestIsFlaggedLine:
    def test_critical_emoji_flagged(self):
        assert mod._is_flagged_line("🚨 F3 Energy: 30 cases") is True

    def test_warning_emoji_flagged(self):
        assert mod._is_flagged_line("⚠️ F3 Pure: 90 cases") is True

    def test_ok_line_not_flagged(self):
        assert mod._is_flagged_line("✅ F3 Mood: 350 cases") is False

    def test_empty_line_not_flagged(self):
        assert mod._is_flagged_line("") is False

    def test_normal_text_not_flagged(self):
        assert mod._is_flagged_line("F3 Energy: 500 cases available") is False


# ---------------------------------------------------------------------------
# _is_throttled tests
# ---------------------------------------------------------------------------

class TestIsThrottled:
    def test_new_key_not_throttled(self):
        assert mod._is_throttled({}, "f3e:sku1") is False

    def test_recent_key_throttled(self):
        throttle = {"f3e:sku1": time.time() - 3600}
        assert mod._is_throttled(throttle, "f3e:sku1") is True

    def test_old_key_not_throttled(self):
        throttle = {"f3e:sku1": time.time() - (8 * 86400)}
        assert mod._is_throttled(throttle, "f3e:sku1") is False

    def test_seven_day_boundary(self):
        # 6 days ago -- still throttled
        throttle = {"key": time.time() - (6 * 86400)}
        assert mod._is_throttled(throttle, "key") is True


# ---------------------------------------------------------------------------
# run_f3e_pass tests
# ---------------------------------------------------------------------------

class TestRunF3EPass:
    def test_flagged_lines_posted(self):
        slack = _make_slack()
        pulse_text = "✅ F3 Mood: 500 cases\n🚨 F3 Energy: 25 cases critical\n⚠️ F3 Pure: 90 cases"
        with patch("cora.tools.inventory_client.get_f3e_inventory_pulse_text", return_value=pulse_text), \
             patch("cora.tools.inventory_client.UNKNOWN_RESPONSE", "UNKNOWN"):
            stats = mod.run_f3e_pass(slack, {}, dry_run=False)
        assert stats["posted"] > 0
        slack.chat_postMessage.assert_called_once()

    def test_no_flagged_lines_no_post(self):
        slack = _make_slack()
        pulse_text = "✅ F3 Energy: 500 cases\n✅ F3 Pure: 300 cases"
        with patch("cora.tools.inventory_client.get_f3e_inventory_pulse_text", return_value=pulse_text), \
             patch("cora.tools.inventory_client.UNKNOWN_RESPONSE", "UNKNOWN"):
            stats = mod.run_f3e_pass(slack, {}, dry_run=False)
        assert stats["posted"] == 0
        slack.chat_postMessage.assert_not_called()

    def test_unknown_response_no_post(self):
        slack = _make_slack()
        with patch("cora.tools.inventory_client.get_f3e_inventory_pulse_text", return_value="UNKNOWN"), \
             patch("cora.tools.inventory_client.UNKNOWN_RESPONSE", "UNKNOWN"):
            stats = mod.run_f3e_pass(slack, {}, dry_run=False)
        assert stats["posted"] == 0

    def test_throttled_item_not_reposted(self):
        slack = _make_slack()
        pulse_text = "🚨 F3 Energy: 25 cases"
        key = f"f3e:{pulse_text.strip()[:80]}"
        throttle = {key: time.time() - 3600}
        with patch("cora.tools.inventory_client.get_f3e_inventory_pulse_text", return_value=pulse_text), \
             patch("cora.tools.inventory_client.UNKNOWN_RESPONSE", "UNKNOWN"):
            stats = mod.run_f3e_pass(slack, throttle, dry_run=False)
        assert stats["throttled"] == 1
        slack.chat_postMessage.assert_not_called()

    def test_dry_run_no_post(self):
        slack = _make_slack()
        pulse_text = "🚨 F3 Energy: 25 cases"
        with patch("cora.tools.inventory_client.get_f3e_inventory_pulse_text", return_value=pulse_text), \
             patch("cora.tools.inventory_client.UNKNOWN_RESPONSE", "UNKNOWN"):
            stats = mod.run_f3e_pass(slack, {}, dry_run=True)
        assert stats["posted"] == 1
        slack.chat_postMessage.assert_not_called()

    def test_inventory_error_captured(self):
        slack = _make_slack()
        with patch("cora.tools.inventory_client.get_f3e_inventory_pulse_text",
                   side_effect=Exception("Drive error")):
            stats = mod.run_f3e_pass(slack, {}, dry_run=False)
        assert stats["error"] is not None

    def test_message_contains_warning_text(self):
        slack = _make_slack()
        pulse_text = "🚨 F3 Pure: 10 cases -- reorder now"
        with patch("cora.tools.inventory_client.get_f3e_inventory_pulse_text", return_value=pulse_text), \
             patch("cora.tools.inventory_client.UNKNOWN_RESPONSE", "UNKNOWN"):
            mod.run_f3e_pass(slack, {}, dry_run=False)
        text = slack.chat_postMessage.call_args.kwargs["text"]
        assert "F3E Inventory Alert" in text
        assert "F3 Pure" in text


# ---------------------------------------------------------------------------
# run_osn_pass tests
# ---------------------------------------------------------------------------

class TestRunOSNPass:
    def test_low_stock_item_triggers_alert(self):
        slack = _make_slack()
        low_item = _make_inv_item(name="Protein Shake", qty=5)
        summary = _make_store_summary(low_stock_items=[low_item])
        thresholds = {"osn": [{"item": "Protein", "alert_below": 20}]}

        with patch("cora.connectors.clover_client.get_all_stores_inventory", return_value=[summary]):
            stats = mod.run_osn_pass(slack, {}, thresholds, dry_run=False)

        assert stats["posted"] == 1
        slack.chat_postMessage.assert_called_once()

    def test_no_low_stock_no_alert(self):
        slack = _make_slack()
        summary = _make_store_summary(low_stock_items=[])
        thresholds = {"osn": [{"item": "Protein", "alert_below": 20}]}

        with patch("cora.connectors.clover_client.get_all_stores_inventory", return_value=[summary]):
            stats = mod.run_osn_pass(slack, {}, thresholds, dry_run=False)

        assert stats["posted"] == 0

    def test_item_not_in_thresholds_not_alerted(self):
        slack = _make_slack()
        low_item = _make_inv_item(name="Creatine Monohydrate", qty=2)
        summary = _make_store_summary(low_stock_items=[low_item])
        thresholds = {"osn": [{"item": "Protein", "alert_below": 20}]}

        with patch("cora.connectors.clover_client.get_all_stores_inventory", return_value=[summary]):
            stats = mod.run_osn_pass(slack, {}, thresholds, dry_run=False)

        assert stats["posted"] == 0

    def test_throttled_item_skipped(self):
        slack = _make_slack()
        low_item = _make_inv_item(name="Protein Powder", qty=3)
        summary = _make_store_summary(low_stock_items=[low_item])
        thresholds = {"osn": [{"item": "Protein", "alert_below": 20}]}
        throttle = {"osn:protein powder": time.time() - 3600}

        with patch("cora.connectors.clover_client.get_all_stores_inventory", return_value=[summary]):
            stats = mod.run_osn_pass(slack, throttle, thresholds, dry_run=False)

        assert stats["throttled"] == 1
        slack.chat_postMessage.assert_not_called()

    def test_dry_run_no_post(self):
        slack = _make_slack()
        low_item = _make_inv_item(name="Protein Mix", qty=2)
        summary = _make_store_summary(low_stock_items=[low_item])
        thresholds = {"osn": [{"item": "Protein", "alert_below": 20}]}

        with patch("cora.connectors.clover_client.get_all_stores_inventory", return_value=[summary]):
            stats = mod.run_osn_pass(slack, {}, thresholds, dry_run=True)

        assert stats["posted"] == 1
        slack.chat_postMessage.assert_not_called()

    def test_clover_error_captured(self):
        slack = _make_slack()
        thresholds = {"osn": [{"item": "Protein", "alert_below": 20}]}
        with patch("cora.connectors.clover_client.get_all_stores_inventory",
                   side_effect=Exception("API error")):
            stats = mod.run_osn_pass(slack, {}, thresholds, dry_run=False)
        assert stats["error"] is not None

    def test_multi_store_same_item(self):
        slack = _make_slack()
        item1 = _make_inv_item(name="Protein 5lb", qty=2)
        item2 = _make_inv_item(name="Protein 5lb", qty=3)
        summaries = [
            _make_store_summary("GW", "G & Warner", low_stock_items=[item1]),
            _make_store_summary("GM", "G & McKellips", low_stock_items=[item2]),
        ]
        thresholds = {"osn": [{"item": "Protein", "alert_below": 20}]}

        with patch("cora.connectors.clover_client.get_all_stores_inventory", return_value=summaries):
            stats = mod.run_osn_pass(slack, {}, thresholds, dry_run=False)

        text = slack.chat_postMessage.call_args.kwargs["text"]
        assert "G & Warner" in text or "G & McKellips" in text

    def test_no_thresholds_configured(self):
        slack = _make_slack()
        thresholds = {"osn": []}
        with patch("cora.connectors.clover_client.get_all_stores_inventory", return_value=[]):
            stats = mod.run_osn_pass(slack, {}, thresholds, dry_run=False)
        assert stats["posted"] == 0

    def test_message_contains_item_name(self):
        slack = _make_slack()
        low_item = _make_inv_item(name="Protein Shake", qty=1)
        summary = _make_store_summary(low_stock_items=[low_item])
        thresholds = {"osn": [{"item": "Protein", "alert_below": 20}]}

        with patch("cora.connectors.clover_client.get_all_stores_inventory", return_value=[summary]):
            mod.run_osn_pass(slack, {}, thresholds, dry_run=False)

        text = slack.chat_postMessage.call_args.kwargs["text"]
        assert "Protein Shake" in text
        assert "OSN Inventory Alert" in text


# ---------------------------------------------------------------------------
# main() integration tests
# ---------------------------------------------------------------------------

class TestMain:
    def test_returns_dict_with_expected_keys(self):
        slack = _make_slack()
        with patch("slack_sdk.WebClient", return_value=slack), \
             patch("cora.tools.inventory_client.get_f3e_inventory_pulse_text", return_value="✅ all good"), \
             patch("cora.tools.inventory_client.UNKNOWN_RESPONSE", "UNKNOWN"), \
             patch("cora.connectors.clover_client.get_all_stores_inventory", return_value=[]):
            result = mod.main(dry_run=True)
        assert "f3e_posted" in result
        assert "osn_posted" in result

    def test_missing_slack_token_returns_error(self):
        import os
        token = os.environ.pop("SLACK_BOT_TOKEN", None)
        try:
            result = mod.main(dry_run=False)
            assert "error" in result
        finally:
            if token:
                os.environ["SLACK_BOT_TOKEN"] = token
