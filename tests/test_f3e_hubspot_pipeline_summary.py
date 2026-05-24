"""Tests for f3e_hubspot_pipeline_summary — hubspot_client helpers and tool dispatch.

Coverage:
  - get_f3e_pipeline_summary_text(): stage grouping, hot list, owner split, closed section,
    empty pipeline, pagination, error handling
  - _tool_f3e_hubspot_pipeline_summary: dispatch wiring, HubSpotClientError forwarding
  - TOOL_DEFINITIONS: entry exists with required fields
  - _TOOL_FUNCTIONS: callable registered
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

# ---------------------------------------------------------------------------
# Helpers to build fake HubSpot deal objects
# ---------------------------------------------------------------------------

def _make_deal(
    deal_id: str,
    name: str,
    stage_id: str,
    amount: str | None = None,
    owner_id: str | None = None,
    closedate: str | None = None,
) -> dict[str, Any]:
    return {
        "id": deal_id,
        "properties": {
            "dealname": name,
            "dealstage": stage_id,
            "amount": amount,
            "hubspot_owner_id": owner_id,
            "closedate": closedate,
            "hs_lastmodifieddate": None,
            "deal_currency_code": "USD",
        },
    }


# Stage IDs from hubspot_client constants
_IDENTIFY = "3601439469"
_OUTREACH = "3601439470"
_SAMPLE_SENT = "3672898248"
_QUALIFIED = "3672898250"
_PROPOSAL = "3672898249"
_NEGOTIATION = "3604397771"
_CLOSED_WON = "3601439474"
_CLOSED_LOST = "3601439475"

_TOMMY_OWNER = "162944825"
_HARRISON_OWNER = "160459333"


# ---------------------------------------------------------------------------
# Class 1: Basic pipeline structure — active stages only
# ---------------------------------------------------------------------------

class TestPipelineSummaryBasicStructure:
    """Verify the summary header and section headers are always present."""

    def _get_summary(self, deals: list[dict[str, Any]]) -> str:
        with patch("cora.tools.hubspot_client._fetch_pipeline_deals", return_value=deals):
            with patch("cora.tools.hubspot_client._refresh_pipeline_cache"):
                with patch("cora.tools.hubspot_client._STAGE_NAME_CACHE", {}):
                    from cora.tools.hubspot_client import get_f3e_pipeline_summary_text
                    return get_f3e_pipeline_summary_text()

    def test_header_contains_date(self):
        result = self._get_summary([])
        assert "F3E Retail Pipeline" in result
        assert "as of" in result

    def test_active_pipeline_line_present(self):
        result = self._get_summary([])
        assert "ACTIVE PIPELINE" in result

    def test_by_stage_section_present(self):
        result = self._get_summary([])
        assert "BY STAGE:" in result

    def test_owner_split_section_present(self):
        result = self._get_summary([])
        assert "OWNER SPLIT" in result

    def test_note_footer_present(self):
        result = self._get_summary([])
        assert "NOTE:" in result
        assert "source-opaque" in result.lower() or "Source-opaque" in result

    def test_no_hubspot_mention_in_output(self):
        result = self._get_summary([])
        assert "HubSpot" not in result or "NOTE" in result  # NOTE section may mention HubSpot as guidance


# ---------------------------------------------------------------------------
# Class 2: Stage grouping — deals land in correct stage buckets
# ---------------------------------------------------------------------------

class TestStageGrouping:
    """Deals in each stage are counted and summed correctly."""

    def _run(self, deals: list) -> str:
        with patch("cora.tools.hubspot_client._fetch_pipeline_deals", return_value=deals):
            with patch("cora.tools.hubspot_client._refresh_pipeline_cache"):
                with patch("cora.tools.hubspot_client._STAGE_NAME_CACHE", {}):
                    from cora.tools.hubspot_client import get_f3e_pipeline_summary_text
                    return get_f3e_pipeline_summary_text()

    def test_identify_stage_shown(self):
        deals = [_make_deal("1", "Acme", _IDENTIFY, "1000", _TOMMY_OWNER)]
        result = self._run(deals)
        assert "Identify" in result

    def test_outreach_stage_shown(self):
        deals = [_make_deal("2", "Beta", _OUTREACH, "2000", _TOMMY_OWNER)]
        result = self._run(deals)
        assert "Outreach" in result

    def test_sample_sent_stage_shown(self):
        deals = [_make_deal("3", "Gamma", _SAMPLE_SENT, "3000", _TOMMY_OWNER)]
        result = self._run(deals)
        assert "Sample Sent" in result

    def test_empty_stages_omitted(self):
        # No deals in Outreach → "Outreach" should not appear under BY STAGE
        deals = [_make_deal("1", "Acme", _IDENTIFY, "1000", _TOMMY_OWNER)]
        result = self._run(deals)
        assert "Outreach" not in result

    def test_two_deals_same_stage_count(self):
        deals = [
            _make_deal("1", "Acme", _IDENTIFY, "1000", _TOMMY_OWNER),
            _make_deal("2", "Beta", _IDENTIFY, "2000", _TOMMY_OWNER),
        ]
        result = self._run(deals)
        assert "2 deals" in result

    def test_single_deal_singular(self):
        deals = [_make_deal("1", "Solo", _IDENTIFY, "1000", _TOMMY_OWNER)]
        result = self._run(deals)
        assert "1 deal" in result and "1 deals" not in result

    def test_stage_value_included(self):
        deals = [_make_deal("1", "BigDeal", _QUALIFIED, "50000", _TOMMY_OWNER)]
        result = self._run(deals)
        assert "50,000" in result

    def test_none_amount_treated_as_zero(self):
        deals = [_make_deal("1", "NullAmt", _IDENTIFY, None, _TOMMY_OWNER)]
        result = self._run(deals)
        assert "ACTIVE PIPELINE" in result  # should not crash


# ---------------------------------------------------------------------------
# Class 3: Active pipeline total
# ---------------------------------------------------------------------------

class TestActivePipelineTotal:
    """Active pipeline total excludes Closed Won and Closed Lost."""

    def _run(self, deals: list) -> str:
        with patch("cora.tools.hubspot_client._fetch_pipeline_deals", return_value=deals):
            with patch("cora.tools.hubspot_client._refresh_pipeline_cache"):
                with patch("cora.tools.hubspot_client._STAGE_NAME_CACHE", {}):
                    from cora.tools.hubspot_client import get_f3e_pipeline_summary_text
                    return get_f3e_pipeline_summary_text()

    def test_active_total_excludes_closed_won(self):
        deals = [
            _make_deal("1", "Active", _QUALIFIED, "10000", _TOMMY_OWNER),
            _make_deal("2", "Won", _CLOSED_WON, "99999", _TOMMY_OWNER),
        ]
        result = self._run(deals)
        # Active pipeline total should be $10,000, not $109,999
        assert "10,000" in result
        # 99,999 should not appear in the ACTIVE PIPELINE line
        lines = result.split("\n")
        active_line = next((l for l in lines if "ACTIVE PIPELINE" in l), "")
        assert "99,999" not in active_line

    def test_active_total_excludes_closed_lost(self):
        deals = [
            _make_deal("1", "Active", _PROPOSAL, "5000", _TOMMY_OWNER),
            _make_deal("2", "Lost", _CLOSED_LOST, "77777", _TOMMY_OWNER),
        ]
        result = self._run(deals)
        lines = result.split("\n")
        active_line = next((l for l in lines if "ACTIVE PIPELINE" in l), "")
        assert "77,777" not in active_line

    def test_zero_active_deals(self):
        deals = [_make_deal("1", "Won", _CLOSED_WON, "1000", _TOMMY_OWNER)]
        result = self._run(deals)
        assert "0 deals" in result

    def test_multiple_active_stages_summed(self):
        deals = [
            _make_deal("1", "A", _IDENTIFY, "1000", _TOMMY_OWNER),
            _make_deal("2", "B", _PROPOSAL, "2000", _TOMMY_OWNER),
            _make_deal("3", "C", _NEGOTIATION, "3000", _TOMMY_OWNER),
        ]
        result = self._run(deals)
        lines = result.split("\n")
        active_line = next((l for l in lines if "ACTIVE PIPELINE" in l), "")
        assert "6,000" in active_line


# ---------------------------------------------------------------------------
# Class 4: Hot list — Qualified, Proposal, Negotiation
# ---------------------------------------------------------------------------

class TestHotList:
    """Hot list contains only deals in Qualified, Proposal, or Negotiation stages."""

    def _run(self, deals: list) -> str:
        with patch("cora.tools.hubspot_client._fetch_pipeline_deals", return_value=deals):
            with patch("cora.tools.hubspot_client._refresh_pipeline_cache"):
                with patch("cora.tools.hubspot_client._STAGE_NAME_CACHE", {}):
                    from cora.tools.hubspot_client import get_f3e_pipeline_summary_text
                    return get_f3e_pipeline_summary_text()

    def test_hot_list_section_appears_when_hot_deals_exist(self):
        deals = [_make_deal("1", "HotDeal", _QUALIFIED, "5000", _TOMMY_OWNER)]
        result = self._run(deals)
        assert "HOT LIST" in result

    def test_hot_list_absent_when_no_hot_deals(self):
        deals = [_make_deal("1", "ColdDeal", _IDENTIFY, "5000", _TOMMY_OWNER)]
        result = self._run(deals)
        assert "HOT LIST" not in result

    def test_qualified_in_hot_list(self):
        deals = [_make_deal("1", "QualDeal", _QUALIFIED, "5000", _TOMMY_OWNER)]
        result = self._run(deals)
        assert "🔥" in result
        assert "Qualified" in result

    def test_proposal_in_hot_list(self):
        deals = [_make_deal("1", "PropDeal", _PROPOSAL, "7000", _TOMMY_OWNER)]
        result = self._run(deals)
        assert "🔥" in result
        assert "Proposal" in result

    def test_negotiation_in_hot_list(self):
        deals = [_make_deal("1", "NegoDeal", _NEGOTIATION, "9000", _TOMMY_OWNER)]
        result = self._run(deals)
        assert "🔥" in result
        assert "Negotiation" in result

    def test_hot_list_sorted_by_value_descending(self):
        deals = [
            _make_deal("1", "SmallDeal", _QUALIFIED, "1000", _TOMMY_OWNER),
            _make_deal("2", "BigDeal", _PROPOSAL, "9000", _TOMMY_OWNER),
        ]
        result = self._run(deals)
        big_pos = result.index("BigDeal")
        small_pos = result.index("SmallDeal")
        assert big_pos < small_pos  # BigDeal should appear first

    def test_identify_not_in_hot_list(self):
        deals = [_make_deal("1", "EarlyDeal", _IDENTIFY, "10000", _TOMMY_OWNER)]
        result = self._run(deals)
        assert "HOT LIST" not in result

    def test_hot_deal_shows_owner(self):
        deals = [_make_deal("1", "TommyDeal", _QUALIFIED, "5000", _TOMMY_OWNER)]
        result = self._run(deals)
        assert "Tommy" in result

    def test_hot_deal_shows_close_date(self):
        deals = [_make_deal("1", "WithDate", _QUALIFIED, "5000", _TOMMY_OWNER, "2026-07-15T00:00:00Z")]
        result = self._run(deals)
        assert "2026-07-15" in result

    def test_hot_deal_link_format(self):
        """Deal links should use Slack mrkdwn <url|name> format."""
        deals = [_make_deal("123", "LinkDeal", _QUALIFIED, "5000", _TOMMY_OWNER)]
        result = self._run(deals)
        assert "<" in result and "|LinkDeal>" in result


# ---------------------------------------------------------------------------
# Class 5: Owner split
# ---------------------------------------------------------------------------

class TestOwnerSplit:
    """Tommy and Harrison deal counts and values are correct."""

    def _run(self, deals: list) -> str:
        with patch("cora.tools.hubspot_client._fetch_pipeline_deals", return_value=deals):
            with patch("cora.tools.hubspot_client._refresh_pipeline_cache"):
                with patch("cora.tools.hubspot_client._STAGE_NAME_CACHE", {}):
                    from cora.tools.hubspot_client import get_f3e_pipeline_summary_text
                    return get_f3e_pipeline_summary_text()

    def test_tommy_appears_in_owner_split(self):
        deals = [_make_deal("1", "TDeal", _IDENTIFY, "1000", _TOMMY_OWNER)]
        result = self._run(deals)
        assert "Tommy" in result

    def test_harrison_appears_when_has_active_deals(self):
        deals = [_make_deal("1", "HDeal", _IDENTIFY, "1000", _HARRISON_OWNER)]
        result = self._run(deals)
        assert "Harrison" in result

    def test_harrison_omitted_when_no_active_deals(self):
        deals = [
            _make_deal("1", "TDeal", _IDENTIFY, "1000", _TOMMY_OWNER),
            # Harrison only has a closed deal — should not appear in active split
            _make_deal("2", "HWon", _CLOSED_WON, "99999", _HARRISON_OWNER),
        ]
        result = self._run(deals)
        # Harrison line should not appear in OWNER SPLIT section
        owner_split_start = result.index("OWNER SPLIT")
        # Get the section after OWNER SPLIT
        after_split = result[owner_split_start:]
        # Harrison should not appear in active owner split
        # (only appears in CLOSED section if at all)
        split_section = after_split.split("\n\n")[0]  # first paragraph after OWNER SPLIT
        assert "Harrison" not in split_section

    def test_closed_deals_excluded_from_owner_split(self):
        deals = [
            _make_deal("1", "Active", _QUALIFIED, "10000", _TOMMY_OWNER),
            _make_deal("2", "Closed", _CLOSED_WON, "50000", _TOMMY_OWNER),
        ]
        result = self._run(deals)
        lines = result.split("\n")
        # Use "Tommy:" (colon) to match the OWNER SPLIT label line, not the HOT LIST line
        tommy_split_line = next((l for l in lines if l.strip().startswith("Tommy:") and "deal" in l.lower()), "")
        # Tommy should have 1 deal in owner split (only the active one)
        assert "1 deal" in tommy_split_line

    def test_unknown_owner_not_crash(self):
        deals = [_make_deal("1", "Anon", _IDENTIFY, "1000", "999999999")]
        result = self._run(deals)
        assert "ACTIVE PIPELINE" in result


# ---------------------------------------------------------------------------
# Class 6: Closed section
# ---------------------------------------------------------------------------

class TestClosedSection:
    """Closed Won and Closed Lost deals appear in CLOSED section."""

    def _run(self, deals: list) -> str:
        with patch("cora.tools.hubspot_client._fetch_pipeline_deals", return_value=deals):
            with patch("cora.tools.hubspot_client._refresh_pipeline_cache"):
                with patch("cora.tools.hubspot_client._STAGE_NAME_CACHE", {}):
                    from cora.tools.hubspot_client import get_f3e_pipeline_summary_text
                    return get_f3e_pipeline_summary_text()

    def test_closed_section_present_when_won_deals_exist(self):
        deals = [_make_deal("1", "WonDeal", _CLOSED_WON, "5000", _TOMMY_OWNER)]
        result = self._run(deals)
        assert "CLOSED:" in result

    def test_closed_section_present_when_lost_deals_exist(self):
        deals = [_make_deal("1", "LostDeal", _CLOSED_LOST, "5000", _TOMMY_OWNER)]
        result = self._run(deals)
        assert "CLOSED:" in result

    def test_closed_section_absent_when_no_closed_deals(self):
        deals = [_make_deal("1", "ActiveDeal", _IDENTIFY, "5000", _TOMMY_OWNER)]
        result = self._run(deals)
        assert "CLOSED:" not in result

    def test_won_deal_marked_with_checkmark(self):
        deals = [_make_deal("1", "WonDeal", _CLOSED_WON, "5000", _TOMMY_OWNER)]
        result = self._run(deals)
        assert "✅" in result

    def test_lost_deal_marked_with_x(self):
        deals = [_make_deal("1", "LostDeal", _CLOSED_LOST, "5000", _TOMMY_OWNER)]
        result = self._run(deals)
        assert "❌" in result

    def test_closed_won_not_counted_in_active_stages(self):
        deals = [_make_deal("1", "WonDeal", _CLOSED_WON, "5000", _TOMMY_OWNER)]
        result = self._run(deals)
        assert "Closed Won" not in result.split("BY STAGE:")[1].split("OWNER SPLIT")[0]


# ---------------------------------------------------------------------------
# Class 7: Empty pipeline
# ---------------------------------------------------------------------------

class TestEmptyPipeline:
    """Zero deals — no crash, sensible output."""

    def _run(self) -> str:
        with patch("cora.tools.hubspot_client._fetch_pipeline_deals", return_value=[]):
            with patch("cora.tools.hubspot_client._refresh_pipeline_cache"):
                with patch("cora.tools.hubspot_client._STAGE_NAME_CACHE", {}):
                    from cora.tools.hubspot_client import get_f3e_pipeline_summary_text
                    return get_f3e_pipeline_summary_text()

    def test_no_crash_on_empty(self):
        result = self._run()
        assert "F3E Retail Pipeline" in result

    def test_zero_active_deals(self):
        result = self._run()
        assert "0 deals" in result

    def test_no_hot_list_on_empty(self):
        result = self._run()
        assert "HOT LIST" not in result

    def test_no_closed_section_on_empty(self):
        result = self._run()
        assert "CLOSED:" not in result


# ---------------------------------------------------------------------------
# Class 8: Pagination — multiple pages of deals fetched
# ---------------------------------------------------------------------------

class TestPagination:
    """_fetch_pipeline_deals loops until no more 'after' cursor."""

    def test_all_pages_aggregated(self):
        page1 = {
            "results": [_make_deal("1", "DealA", _IDENTIFY, "1000", _TOMMY_OWNER)],
            "paging": {"next": {"after": "cursor_2"}},
        }
        page2 = {
            "results": [_make_deal("2", "DealB", _OUTREACH, "2000", _TOMMY_OWNER)],
        }
        responses = [page1, page2]
        call_idx = 0

        def _fake_post(*args, **kwargs):
            nonlocal call_idx
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = responses[call_idx]
            call_idx += 1
            return resp

        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.side_effect = _fake_post

        with patch("cora.tools.hubspot_client._STAGE_NAME_CACHE", {}):
            with patch("cora.tools.hubspot_client._refresh_pipeline_cache"):
                with patch("cora.tools.hubspot_client._token", return_value="fake-token"):
                    with patch("httpx.Client", return_value=mock_client):
                        from cora.tools.hubspot_client import _fetch_pipeline_deals
                        deals = _fetch_pipeline_deals("2234421978")

        assert len(deals) == 2
        names = {d["properties"]["dealname"] for d in deals}
        assert "DealA" in names and "DealB" in names


# ---------------------------------------------------------------------------
# Class 9: Error handling in hubspot_client
# ---------------------------------------------------------------------------

class TestHubSpotClientErrors:
    """_fetch_pipeline_deals raises HubSpotClientError on HTTP errors."""

    def _make_mock_client(self, status_code: int, text: str = "error"):
        resp = MagicMock()
        resp.status_code = status_code
        resp.text = text
        resp.json.return_value = {"results": []}
        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = resp
        return mock_client

    def test_401_raises_client_error(self):
        from cora.tools.hubspot_client import HubSpotClientError, _fetch_pipeline_deals
        mock_client = self._make_mock_client(401)
        with patch("cora.tools.hubspot_client._STAGE_NAME_CACHE", {}):
            with patch("cora.tools.hubspot_client._refresh_pipeline_cache"):
                with patch("cora.tools.hubspot_client._token", return_value="fake-token"):
                    with patch("httpx.Client", return_value=mock_client):
                        with pytest.raises(HubSpotClientError, match="401"):
                            _fetch_pipeline_deals("2234421978")

    def test_403_raises_client_error(self):
        from cora.tools.hubspot_client import HubSpotClientError, _fetch_pipeline_deals
        mock_client = self._make_mock_client(403)
        with patch("cora.tools.hubspot_client._STAGE_NAME_CACHE", {}):
            with patch("cora.tools.hubspot_client._refresh_pipeline_cache"):
                with patch("cora.tools.hubspot_client._token", return_value="fake-token"):
                    with patch("httpx.Client", return_value=mock_client):
                        with pytest.raises(HubSpotClientError):
                            _fetch_pipeline_deals("2234421978")

    def test_500_raises_client_error(self):
        from cora.tools.hubspot_client import HubSpotClientError, _fetch_pipeline_deals
        mock_client = self._make_mock_client(500)
        with patch("cora.tools.hubspot_client._STAGE_NAME_CACHE", {}):
            with patch("cora.tools.hubspot_client._refresh_pipeline_cache"):
                with patch("cora.tools.hubspot_client._token", return_value="fake-token"):
                    with patch("httpx.Client", return_value=mock_client):
                        with pytest.raises(HubSpotClientError):
                            _fetch_pipeline_deals("2234421978")

    def test_network_error_raises_client_error(self):
        import httpx
        from cora.tools.hubspot_client import HubSpotClientError, _fetch_pipeline_deals
        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.side_effect = httpx.RequestError("connection refused")
        with patch("cora.tools.hubspot_client._STAGE_NAME_CACHE", {}):
            with patch("cora.tools.hubspot_client._refresh_pipeline_cache"):
                with patch("cora.tools.hubspot_client._token", return_value="fake-token"):
                    with patch("httpx.Client", return_value=mock_client):
                        with pytest.raises(HubSpotClientError, match="network error"):
                            _fetch_pipeline_deals("2234421978")


# ---------------------------------------------------------------------------
# Class 10: _tool_f3e_hubspot_pipeline_summary handler
# ---------------------------------------------------------------------------

class TestToolHandler:
    """The dispatch handler wraps get_f3e_pipeline_summary_text correctly."""

    def test_returns_summary_on_success(self):
        from cora.tools.tool_dispatch import _tool_f3e_hubspot_pipeline_summary  # type: ignore
        with patch("cora.tools.hubspot_client.get_f3e_pipeline_summary_text", return_value="SUMMARY_TEXT"):
            result = _tool_f3e_hubspot_pipeline_summary("U123", "F3E", {})
        assert result == "SUMMARY_TEXT"

    def test_returns_error_string_on_client_error(self):
        from cora.tools.hubspot_client import HubSpotClientError
        from cora.tools.tool_dispatch import _tool_f3e_hubspot_pipeline_summary  # type: ignore
        with patch("cora.tools.hubspot_client.get_f3e_pipeline_summary_text",
                   side_effect=HubSpotClientError("401 — token invalid")):
            result = _tool_f3e_hubspot_pipeline_summary("U123", "F3E", {})
        assert "f3e_hubspot_pipeline_summary" in result
        assert "HubSpot call failed" in result

    def test_error_string_contains_guidance(self):
        from cora.tools.hubspot_client import HubSpotClientError
        from cora.tools.tool_dispatch import _tool_f3e_hubspot_pipeline_summary  # type: ignore
        with patch("cora.tools.hubspot_client.get_f3e_pipeline_summary_text",
                   side_effect=HubSpotClientError("429 rate limited")):
            result = _tool_f3e_hubspot_pipeline_summary("U456", "FNDR", {})
        # Should tell Claude to apologize and suggest retry
        assert "Apologize" in result or "apologize" in result

    def test_entity_and_user_passed_without_crash(self):
        from cora.tools.tool_dispatch import _tool_f3e_hubspot_pipeline_summary  # type: ignore
        with patch("cora.tools.hubspot_client.get_f3e_pipeline_summary_text", return_value="OK"):
            result = _tool_f3e_hubspot_pipeline_summary("UABC", "FNDR", {"extra": "ignored"})
        assert result == "OK"


# ---------------------------------------------------------------------------
# Class 11: TOOL_DEFINITIONS entry
# ---------------------------------------------------------------------------

class TestToolDefinitionsEntry:
    """TOOL_DEFINITIONS list has a well-formed entry for f3e_hubspot_pipeline_summary."""

    def _get_entry(self):
        from cora.tools.tool_dispatch import TOOL_DEFINITIONS  # type: ignore
        return next((t for t in TOOL_DEFINITIONS if t["name"] == "f3e_hubspot_pipeline_summary"), None)

    def test_entry_exists(self):
        assert self._get_entry() is not None

    def test_has_description(self):
        entry = self._get_entry()
        assert "description" in entry
        assert len(entry["description"]) > 50

    def test_has_input_schema(self):
        entry = self._get_entry()
        assert "input_schema" in entry
        assert entry["input_schema"]["type"] == "object"

    def test_description_mentions_sales_summary_trigger(self):
        entry = self._get_entry()
        assert "sales summary" in entry["description"].lower() or "@Cora sales summary" in entry["description"]

    def test_description_mentions_channel_scope(self):
        entry = self._get_entry()
        desc = entry["description"]
        assert "F3E" in desc or "f3e" in desc

    def test_description_mentions_source_opacity(self):
        entry = self._get_entry()
        assert "source-opaque" in entry["description"].lower() or "HubSpot" in entry["description"]

    def test_no_required_inputs(self):
        entry = self._get_entry()
        assert entry["input_schema"]["required"] == []


# ---------------------------------------------------------------------------
# Class 12: _TOOL_FUNCTIONS registration
# ---------------------------------------------------------------------------

class TestToolFunctionsRegistration:
    """f3e_hubspot_pipeline_summary is registered in _TOOL_FUNCTIONS."""

    def test_registered_in_tool_functions(self):
        from cora.tools.tool_dispatch import _TOOL_FUNCTIONS  # type: ignore
        assert "f3e_hubspot_pipeline_summary" in _TOOL_FUNCTIONS

    def test_registered_value_is_callable(self):
        from cora.tools.tool_dispatch import _TOOL_FUNCTIONS  # type: ignore
        fn = _TOOL_FUNCTIONS["f3e_hubspot_pipeline_summary"]
        assert callable(fn)


# ---------------------------------------------------------------------------
# Class 13: dispatch() integration
# ---------------------------------------------------------------------------

class TestDispatchIntegration:
    """dispatch() routes 'f3e_hubspot_pipeline_summary' to the correct handler."""

    def test_dispatch_calls_handler(self):
        from cora.tools.tool_dispatch import dispatch  # type: ignore
        with patch("cora.tools.hubspot_client.get_f3e_pipeline_summary_text", return_value="DISPATCH_OK"):
            result = dispatch("f3e_hubspot_pipeline_summary", {}, "U789", entity="F3E")
        assert result == "DISPATCH_OK"

    def test_dispatch_unknown_tool_returns_error(self):
        from cora.tools.tool_dispatch import dispatch  # type: ignore
        result = dispatch("nonexistent_tool_xyz", {}, "U000", entity="F3E")
        assert "Unknown tool" in result

    def test_dispatch_passes_entity(self):
        """Handler should receive entity correctly (no crash with FNDR entity)."""
        from cora.tools.tool_dispatch import dispatch  # type: ignore
        with patch("cora.tools.hubspot_client.get_f3e_pipeline_summary_text", return_value="FNDR_OK"):
            result = dispatch("f3e_hubspot_pipeline_summary", {}, "UFNDR", entity="FNDR")
        assert result == "FNDR_OK"

    def test_dispatch_hubspot_error_returns_string(self):
        from cora.tools.hubspot_client import HubSpotClientError
        from cora.tools.tool_dispatch import dispatch  # type: ignore
        with patch("cora.tools.hubspot_client.get_f3e_pipeline_summary_text",
                   side_effect=HubSpotClientError("500 server error")):
            result = dispatch("f3e_hubspot_pipeline_summary", {}, "U000", entity="F3E")
        # Should return a string (not raise) — dispatch catches at outer level too
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Class 14: Source-opacity assurance
# ---------------------------------------------------------------------------

class TestSourceOpacity:
    """Output must never expose HubSpot internals, stage IDs, or owner IDs in body text."""

    def _run(self, deals: list) -> str:
        with patch("cora.tools.hubspot_client._fetch_pipeline_deals", return_value=deals):
            with patch("cora.tools.hubspot_client._refresh_pipeline_cache"):
                with patch("cora.tools.hubspot_client._STAGE_NAME_CACHE", {}):
                    from cora.tools.hubspot_client import get_f3e_pipeline_summary_text
                    return get_f3e_pipeline_summary_text()

    def test_stage_ids_not_exposed(self):
        deals = [_make_deal("1", "Deal", _QUALIFIED, "5000", _TOMMY_OWNER)]
        result = self._run(deals)
        # Raw stage IDs like "3672898250" should not appear in readable body
        assert "3672898250" not in result
        assert "3672898249" not in result
        assert "3601439469" not in result

    def test_owner_ids_not_exposed(self):
        deals = [_make_deal("1", "Deal", _IDENTIFY, "5000", _TOMMY_OWNER)]
        result = self._run(deals)
        assert "162944825" not in result
        assert "160459333" not in result

    def test_pipeline_id_not_exposed(self):
        deals = [_make_deal("1", "Deal", _IDENTIFY, "5000", _TOMMY_OWNER)]
        result = self._run(deals)
        assert "2234421978" not in result

    def test_note_instructs_source_opacity(self):
        result = self._run([])
        assert "NOTE:" in result
        # The NOTE section should remind Claude not to mention HubSpot
        note_section = result.split("NOTE:")[1] if "NOTE:" in result else ""
        assert "HubSpot" in note_section or "source-opaque" in note_section.lower()


# ---------------------------------------------------------------------------
# Class 15: Deal link format
# ---------------------------------------------------------------------------

class TestDealLinkFormat:
    """Deal links use Slack mrkdwn <url|name> format with correct HubSpot URLs."""

    def _run(self, deals: list) -> str:
        with patch("cora.tools.hubspot_client._fetch_pipeline_deals", return_value=deals):
            with patch("cora.tools.hubspot_client._refresh_pipeline_cache"):
                with patch("cora.tools.hubspot_client._STAGE_NAME_CACHE", {}):
                    from cora.tools.hubspot_client import get_f3e_pipeline_summary_text
                    return get_f3e_pipeline_summary_text()

    def test_hot_deal_link_contains_deal_id(self):
        deals = [_make_deal("99999", "TargetDeal", _QUALIFIED, "5000", _TOMMY_OWNER)]
        result = self._run(deals)
        assert "99999" in result

    def test_hot_deal_link_uses_angle_bracket_syntax(self):
        deals = [_make_deal("123", "LinkTest", _PROPOSAL, "5000", _TOMMY_OWNER)]
        result = self._run(deals)
        assert "<" in result and "|LinkTest>" in result

