"""Tests for Feature #9: Inventory + Reorder Alerts (F3E).

The OSN/Clover pass was removed 2026-06-17 (Phase 3 item C) -- per-SKU store
inventory has no QBO equivalent, so only the F3E pass remains.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch


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
# main() integration tests
# ---------------------------------------------------------------------------

class TestMain:
    def test_returns_dict_with_expected_keys(self):
        slack = _make_slack()
        with patch("slack_sdk.WebClient", return_value=slack), \
             patch("cora.tools.inventory_client.get_f3e_inventory_pulse_text", return_value="✅ all good"), \
             patch("cora.tools.inventory_client.UNKNOWN_RESPONSE", "UNKNOWN"):
            result = mod.main(dry_run=True)
        assert "f3e_posted" in result
        # OSN pass removed -- no OSN keys should appear
        assert "osn_posted" not in result

    def test_missing_slack_token_returns_error(self):
        import os
        token = os.environ.pop("SLACK_BOT_TOKEN", None)
        try:
            result = mod.main(dry_run=False)
            assert "error" in result
        finally:
            if token:
                os.environ["SLACK_BOT_TOKEN"] = token
