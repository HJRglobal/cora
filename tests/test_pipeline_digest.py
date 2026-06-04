"""Tests for Feature #7: Weekly Pipeline Digest -> Tommy + Alex."""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
# Direct import (consistent with Tier 3 test pattern)
if str(_REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "scripts"))
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

import run_pipeline_digest as mod  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_deal(
    did="D001",
    name="TestCo Energy Deal",
    amount=5000.0,
    stage_id="qualify",
    days_old=5,
):
    ts = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()
    return {
        "id": did,
        "properties": {
            "dealname": name,
            "amount": str(amount),
            "dealstage": stage_id,
            "pipeline": "default",
            "createdate": ts,
            "hs_lastmodifieddate": ts,
        },
    }


def _make_slack():
    slack = MagicMock()
    slack.conversations_open.return_value = {"channel": {"id": "DM001"}}
    slack.chat_postMessage.return_value = {"ok": True}
    return slack


# ---------------------------------------------------------------------------
# build_tommy_message tests
# ---------------------------------------------------------------------------

class TestBuildTommyMessage:
    def test_contains_header(self):
        msg = mod.build_tommy_message([], "Pipeline: 0 deals")
        assert "F3E Pipeline" in msg
        assert "Monday" in msg

    def test_includes_pipeline_text(self):
        msg = mod.build_tommy_message([], "ACTIVE PIPELINE: 3 deals")
        assert "ACTIVE PIPELINE: 3 deals" in msg

    def test_aging_count_shown_when_deals_stall(self):
        deal = _make_deal(stage_id="identify", days_old=20)
        with patch.dict(mod._STAGE_NAME_CACHE, {"identify": "Identify"}):
            msg = mod.build_tommy_message([deal], "Pipeline text")
        assert "1 deal(s) need attention" in msg

    def test_no_aging_shows_check(self):
        deal = _make_deal(stage_id="identify", days_old=2)
        with patch.dict(mod._STAGE_NAME_CACHE, {"identify": "Identify"}):
            msg = mod.build_tommy_message([deal], "Pipeline text")
        assert "within stage thresholds" in msg

    def test_empty_deals_list(self):
        msg = mod.build_tommy_message([], "No deals")
        assert "No deals" in msg


# ---------------------------------------------------------------------------
# build_alex_message tests
# ---------------------------------------------------------------------------

class TestBuildAlexMessage:
    def test_empty_deals(self):
        msg = mod.build_alex_message([])
        assert "No active deals" in msg

    def test_contains_deal_count(self):
        deals = [_make_deal("D1"), _make_deal("D2")]
        with patch.dict(mod._STAGE_NAME_CACHE, {}):
            msg = mod.build_alex_message(deals)
        assert "2 active deal(s)" in msg

    def test_contains_total_value(self):
        deals = [_make_deal(amount=10000), _make_deal(amount=5000)]
        with patch.dict(mod._STAGE_NAME_CACHE, {}):
            msg = mod.build_alex_message(deals)
        assert "15,000" in msg

    def test_shows_stage_breakdown(self):
        deals = [_make_deal(stage_id="stage1"), _make_deal(stage_id="stage2")]
        with patch.dict(mod._STAGE_NAME_CACHE, {"stage1": "Identify", "stage2": "Outreach"}):
            msg = mod.build_alex_message(deals)
        assert "By stage" in msg

    def test_shows_top_deals(self):
        deals = [_make_deal(did="D1", name="Big Deal", amount=50000)]
        with patch.dict(mod._STAGE_NAME_CACHE, {}):
            msg = mod.build_alex_message(deals)
        assert "Big Deal" in msg

    def test_header_present(self):
        msg = mod.build_alex_message([_make_deal()])
        assert "UFL Sponsorship Pipeline" in msg


# ---------------------------------------------------------------------------
# _is_aging tests
# ---------------------------------------------------------------------------

class TestIsAging:
    def test_identify_stage_14d_threshold(self):
        deal = _make_deal(stage_id="identify", days_old=20)
        with patch.dict(mod._STAGE_NAME_CACHE, {"identify": "Identify"}):
            assert mod._is_aging(deal) is True

    def test_identify_stage_not_aging(self):
        deal = _make_deal(stage_id="identify", days_old=5)
        with patch.dict(mod._STAGE_NAME_CACHE, {"identify": "Identify"}):
            assert mod._is_aging(deal) is False

    def test_unknown_stage_uses_default_threshold(self):
        deal = _make_deal(stage_id="custom123", days_old=25)
        with patch.dict(mod._STAGE_NAME_CACHE, {}):
            assert mod._is_aging(deal) is True


# ---------------------------------------------------------------------------
# run() integration tests
# -- All hubspot patches use patch.object(mod, ...) because run_pipeline_digest
#    uses direct from-import; source-level patches do not intercept.
# ---------------------------------------------------------------------------

class TestRun:
    def test_sends_to_tommy_and_alex(self):
        slack = _make_slack()
        f3e_deals = [_make_deal()]
        default_deals = [_make_deal(name="UFL MMA Sponsorship")]

        with patch.object(mod, "get_deals_by_pipeline") as mock_deals, \
             patch.object(mod, "get_f3e_pipeline_summary_text", return_value="Pipeline OK"), \
             patch.object(mod, "_refresh_pipeline_cache"), \
             patch("slack_sdk.WebClient", return_value=slack), \
             patch.dict(mod._STAGE_NAME_CACHE, {}):
            mock_deals.side_effect = [f3e_deals, default_deals]
            result = mod.run(dry_run=False)

        assert result["tommy"] is True
        assert result["alex"] is True

    def test_hubspot_error_sends_fallback_and_continues(self):
        slack = _make_slack()
        from cora.tools.hubspot_client import HubSpotClientError

        with patch.object(mod, "get_deals_by_pipeline",
                   side_effect=HubSpotClientError("401")), \
             patch.object(mod, "get_f3e_pipeline_summary_text",
                   side_effect=HubSpotClientError("401")), \
             patch.object(mod, "_refresh_pipeline_cache"), \
             patch("slack_sdk.WebClient", return_value=slack), \
             patch.dict(mod._STAGE_NAME_CACHE, {}):
            result = mod.run(dry_run=False)

        assert len(result["errors"]) > 0
        assert slack.chat_postMessage.call_count >= 1

    def test_dry_run_no_dm_sent(self):
        slack = _make_slack()
        with patch.object(mod, "get_deals_by_pipeline", return_value=[]), \
             patch.object(mod, "get_f3e_pipeline_summary_text", return_value="OK"), \
             patch.object(mod, "_refresh_pipeline_cache"), \
             patch("slack_sdk.WebClient", return_value=slack), \
             patch.dict(mod._STAGE_NAME_CACHE, {}):
            mod.run(dry_run=True)

        slack.chat_postMessage.assert_not_called()

    def test_missing_slack_token_returns_error(self):
        import os
        original = os.environ.pop("SLACK_BOT_TOKEN", None)
        try:
            result = mod.run(dry_run=False)
            assert "error" in result
        finally:
            if original:
                os.environ["SLACK_BOT_TOKEN"] = original

    def test_ufl_keyword_filter_works(self):
        slack = _make_slack()
        deals = [
            _make_deal(did="D1", name="UFL Fight Night Sponsor"),
            _make_deal(did="D2", name="Regular OSN Deal"),
        ]
        with patch.object(mod, "get_deals_by_pipeline") as mock_deals, \
             patch.object(mod, "get_f3e_pipeline_summary_text", return_value="OK"), \
             patch.object(mod, "_refresh_pipeline_cache"), \
             patch("slack_sdk.WebClient", return_value=slack), \
             patch.dict(mod._STAGE_NAME_CACHE, {}):
            mock_deals.side_effect = [[], deals]
            mod.run(dry_run=False)

        all_texts = [
            c.kwargs.get("text") or (c.args[0] if c.args else "")
            for c in slack.chat_postMessage.call_args_list
        ]
        assert any("UFL Sponsorship" in (t or "") for t in all_texts), (
            f"Expected 'UFL Sponsorship' in one of the posted messages. Got: {all_texts}"
        )
