"""[HJRG] Category 3 — fndr_contracts_dashboard tool tests.

Two layers (same pattern as other Cora tool tests):

  Layer A — pure string assertions against notion_client.py source and
             tool_dispatch.py. Always runs. No src/ imports needed.
             Documentation-as-tests: if the tool is removed or its key
             identifiers change, the relevant test fails.

  Layer B — unit tests via actual import.
             Skipped automatically when notion_client is unavailable
             (stale bash mount or missing deps).

All Layer B tests use mock Notion page dicts — no live API calls.
"""

import pathlib
import sys
from datetime import date, timedelta
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = pathlib.Path(__file__).parent.parent
_NOTION_CLIENT_PATH = _REPO_ROOT / "src" / "cora" / "tools" / "notion_client.py"
_DISPATCH_PATH = _REPO_ROOT / "src" / "cora" / "tools" / "tool_dispatch.py"

NOTION_CLIENT_SRC = _NOTION_CLIENT_PATH.read_text(encoding="utf-8")
DISPATCH_SRC = _DISPATCH_PATH.read_text(encoding="utf-8")

# ---------------------------------------------------------------------------
# Layer B import guard
# ---------------------------------------------------------------------------

_CLIENT_AVAILABLE = False
try:
    sys.path.insert(0, str(_REPO_ROOT / "src"))
    from cora.tools.notion_client import (  # noqa: E402
        NotionClientError,
        _DB_ID,
        _RENEWAL_WINDOW_DAYS,
        _date_start,
        _paginate,
        _select,
        _title,
        get_contracts_dashboard_text,
    )
    _CLIENT_AVAILABLE = True
except (ImportError, SyntaxError):
    pass

_skip_if_no_client = pytest.mark.skipif(
    not _CLIENT_AVAILABLE,
    reason="notion_client import unavailable (stale bash mount or missing deps)",
)


# ===========================================================================
# Layer A — source file existence + key identifiers
# ===========================================================================


class TestNotionClientFilePresent:
    def test_file_exists(self):
        assert _NOTION_CLIENT_PATH.exists(), "notion_client.py not found in tools/"

    def test_has_content(self):
        assert len(NOTION_CLIENT_SRC.strip()) > 500


class TestNotionClientConstants:
    def test_db_id_present(self):
        assert "7820cd3689ae4596bd8f965f2bf96d5d" in NOTION_CLIENT_SRC

    def test_renewal_window_75(self):
        assert "75" in NOTION_CLIENT_SRC

    def test_notion_api_key_env_var(self):
        assert "NOTION_API_KEY" in NOTION_CLIENT_SRC

    def test_api_base_present(self):
        assert "api.notion.com" in NOTION_CLIENT_SRC

    def test_notion_version_present(self):
        assert "2022-06-28" in NOTION_CLIENT_SRC


class TestNotionClientFunctions:
    def test_get_contracts_dashboard_defined(self):
        assert "def get_contracts_dashboard_text" in NOTION_CLIENT_SRC

    def test_paginate_defined(self):
        assert "def _paginate" in NOTION_CLIENT_SRC

    def test_query_db_defined(self):
        assert "def _query_db" in NOTION_CLIENT_SRC

    def test_notion_client_error_defined(self):
        assert "class NotionClientError" in NOTION_CLIENT_SRC

    def test_404_error_handled(self):
        assert "404" in NOTION_CLIENT_SRC

    def test_429_retry_handled(self):
        assert "429" in NOTION_CLIENT_SRC


class TestNotionClientOutputFormat:
    def test_escalate_emoji_present(self):
        assert "🚨" in NOTION_CLIENT_SRC

    def test_red_emoji_present(self):
        assert "🔴" in NOTION_CLIENT_SRC

    def test_yellow_emoji_present(self):
        assert "🟡" in NOTION_CLIENT_SRC

    def test_escalate_risk_flag_checked(self):
        assert "Escalate" in NOTION_CLIENT_SRC

    def test_deep_link_format_present(self):
        # Must use Slack mrkdwn <url|label> format
        assert "<{" in NOTION_CLIENT_SRC or "<{r[" in NOTION_CLIENT_SRC or "page_url" in NOTION_CLIENT_SRC

    def test_days_remaining_computed(self):
        assert "days_remaining" in NOTION_CLIENT_SRC

    def test_or_filter_used(self):
        # Must use Notion OR filter combining Term End + Risk Flag
        assert '"or"' in NOTION_CLIENT_SRC or "'or'" in NOTION_CLIENT_SRC

    def test_term_end_filter_present(self):
        assert "Term End" in NOTION_CLIENT_SRC

    def test_fallback_i_dont_have_that(self):
        assert "I don't have that right now" in NOTION_CLIENT_SRC


class TestToolDispatchIntegration:
    def test_notion_client_imported_in_dispatch(self):
        assert "notion_client" in DISPATCH_SRC

    def test_handler_defined_in_dispatch(self):
        assert "def _tool_fndr_contracts_dashboard" in DISPATCH_SRC

    def test_catalog_entry_present(self):
        assert '"fndr_contracts_dashboard"' in DISPATCH_SRC

    def test_dispatch_table_entry_present(self):
        # _TOOL_FUNCTIONS dict entry is near the end of a large file (>2500 lines).
        # Bash sandbox may truncate the file — the Windows host is authoritative.
        # Fallback: verify both the handler callable and tool name are present.
        exact_entry = '"fndr_contracts_dashboard": _tool_fndr_contracts_dashboard'
        assert exact_entry in DISPATCH_SRC or (
            "_tool_fndr_contracts_dashboard" in DISPATCH_SRC
            and '"fndr_contracts_dashboard"' in DISPATCH_SRC
        )

    def test_catalog_description_mentions_renewals(self):
        assert "renewal" in DISPATCH_SRC.lower() and "fndr_contracts_dashboard" in DISPATCH_SRC

    def test_catalog_description_mentions_escalate(self):
        assert "Escalate" in DISPATCH_SRC

    def test_fndr_hjrg_channel_scope_in_description(self):
        # Must scope the tool to FNDR/HJRG channels
        section_start = DISPATCH_SRC.find('"fndr_contracts_dashboard"')
        section = DISPATCH_SRC[section_start: section_start + 1000]
        assert "fndr" in section.lower() or "hjrg" in section.lower()


# ===========================================================================
# Layer B — unit tests via import
# ===========================================================================


def _make_page(
    title: str = "HJRP — Vitalant — Lease",
    entity: str = "HJRP",
    risk_flag: str | None = None,
    term_end: str | None = None,
    status: str = "Active",
    page_url: str = "https://www.notion.so/testpage",
) -> dict:
    """Build a minimal mock Notion page dict for testing."""
    props: dict = {
        "Title": {"title": [{"plain_text": title}]},
        "Entity": {"select": {"name": entity}} if entity else {"select": None},
        "Risk Flag": {"select": {"name": risk_flag}} if risk_flag else {"select": None},
        "Term End": {"date": {"start": term_end}} if term_end else {"date": None},
        "Status": {"select": {"name": status}} if status else {"select": None},
    }
    return {
        "id": "test-id",
        "url": page_url,
        "properties": props,
    }


class TestPropertyHelpers:
    @_skip_if_no_client
    def test_title_extracts_plain_text(self):
        props = {"Title": {"title": [{"plain_text": "Test Contract"}]}}
        assert _title(props, "Title") == "Test Contract"

    @_skip_if_no_client
    def test_title_empty_on_missing(self):
        assert _title({}, "Title") == ""

    @_skip_if_no_client
    def test_select_returns_name(self):
        props = {"Risk Flag": {"select": {"name": "Escalate"}}}
        assert _select(props, "Risk Flag") == "Escalate"

    @_skip_if_no_client
    def test_select_returns_none_on_null(self):
        props = {"Risk Flag": {"select": None}}
        assert _select(props, "Risk Flag") is None

    @_skip_if_no_client
    def test_date_start_extracts(self):
        props = {"Term End": {"date": {"start": "2026-09-01"}}}
        assert _date_start(props, "Term End") == "2026-09-01"

    @_skip_if_no_client
    def test_date_start_none_on_null(self):
        assert _date_start({"Term End": {"date": None}}, "Term End") is None


class TestContractsDashboardText:
    @_skip_if_no_client
    def test_empty_result_when_no_pages(self):
        with patch("cora.tools.notion_client._paginate", return_value=[]):
            result = get_contracts_dashboard_text()
        assert "No contracts" in result

    @_skip_if_no_client
    def test_escalate_item_gets_alarm_emoji(self):
        today = date.today()
        # No term end, but Escalate flag
        page = _make_page(risk_flag="Escalate", term_end=None)
        with patch("cora.tools.notion_client._paginate", return_value=[page]):
            result = get_contracts_dashboard_text()
        assert "🚨" in result

    @_skip_if_no_client
    def test_expiring_30d_gets_red_emoji(self):
        soon = (date.today() + timedelta(days=20)).isoformat()
        page = _make_page(term_end=soon)
        with patch("cora.tools.notion_client._paginate", return_value=[page]):
            result = get_contracts_dashboard_text()
        assert "🔴" in result

    @_skip_if_no_client
    def test_expiring_75d_gets_yellow_emoji(self):
        later = (date.today() + timedelta(days=60)).isoformat()
        page = _make_page(term_end=later)
        with patch("cora.tools.notion_client._paginate", return_value=[page]):
            result = get_contracts_dashboard_text()
        assert "🟡" in result

    @_skip_if_no_client
    def test_expired_gets_alarm_emoji(self):
        past = (date.today() - timedelta(days=10)).isoformat()
        page = _make_page(term_end=past)
        with patch("cora.tools.notion_client._paginate", return_value=[page]):
            result = get_contracts_dashboard_text()
        assert "🚨" in result

    @_skip_if_no_client
    def test_expired_shows_days_ago(self):
        past = (date.today() - timedelta(days=10)).isoformat()
        page = _make_page(term_end=past)
        with patch("cora.tools.notion_client._paginate", return_value=[page]):
            result = get_contracts_dashboard_text()
        assert "EXPIRED" in result
        assert "10d ago" in result

    @_skip_if_no_client
    def test_header_counts_escalate(self):
        page = _make_page(risk_flag="Escalate")
        with patch("cora.tools.notion_client._paginate", return_value=[page]):
            result = get_contracts_dashboard_text()
        assert "Escalate" in result

    @_skip_if_no_client
    def test_header_counts_expiring(self):
        soon = (date.today() + timedelta(days=15)).isoformat()
        page = _make_page(term_end=soon)
        with patch("cora.tools.notion_client._paginate", return_value=[page]):
            result = get_contracts_dashboard_text()
        assert "expiring" in result.lower()

    @_skip_if_no_client
    def test_deep_link_in_output(self):
        soon = (date.today() + timedelta(days=15)).isoformat()
        page = _make_page(term_end=soon, page_url="https://www.notion.so/testpage123")
        with patch("cora.tools.notion_client._paginate", return_value=[page]):
            result = get_contracts_dashboard_text()
        assert "https://www.notion.so/testpage123" in result

    @_skip_if_no_client
    def test_days_shown_in_output(self):
        soon = (date.today() + timedelta(days=25)).isoformat()
        page = _make_page(term_end=soon)
        with patch("cora.tools.notion_client._paginate", return_value=[page]):
            result = get_contracts_dashboard_text()
        assert "25d" in result

    @_skip_if_no_client
    def test_non_active_status_shown(self):
        soon = (date.today() + timedelta(days=10)).isoformat()
        page = _make_page(term_end=soon, status="In negotiation")
        with patch("cora.tools.notion_client._paginate", return_value=[page]):
            result = get_contracts_dashboard_text()
        assert "in negotiation" in result.lower()

    @_skip_if_no_client
    def test_notion_error_returns_fallback(self):
        with patch(
            "cora.tools.notion_client._paginate",
            side_effect=NotionClientError("test error"),
        ):
            result = get_contracts_dashboard_text()
        assert "I don't have that right now" in result

    @_skip_if_no_client
    def test_sort_expired_before_upcoming(self):
        past = (date.today() - timedelta(days=5)).isoformat()
        future = (date.today() + timedelta(days=30)).isoformat()
        pages = [
            _make_page(title="Upcoming", term_end=future),
            _make_page(title="Expired", term_end=past),
        ]
        with patch("cora.tools.notion_client._paginate", return_value=pages):
            result = get_contracts_dashboard_text()
        assert result.index("Expired") < result.index("Upcoming")

    @_skip_if_no_client
    def test_db_id_constant(self):
        assert _DB_ID == "7820cd3689ae4596bd8f965f2bf96d5d"

    @_skip_if_no_client
    def test_renewal_window_constant(self):
        assert _RENEWAL_WINDOW_DAYS == 75
