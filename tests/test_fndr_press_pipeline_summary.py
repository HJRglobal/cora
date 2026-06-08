"""[FNDR] fndr_press_pipeline_summary tool tests.

Mirrors the fndr_contracts_dashboard test pattern (two layers):

  Layer A — pure string assertions against notion_client.py + tool_dispatch.py
             + fndr.md. Always runs. Documentation-as-tests for the 4 wiring
             points + scope guard.

  Layer B — unit tests via actual import, using mocked Notion page dicts
             (no live API). Skipped automatically when imports are unavailable.
"""

import pathlib
import sys
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = pathlib.Path(__file__).parent.parent
_NOTION_CLIENT_PATH = _REPO_ROOT / "src" / "cora" / "tools" / "notion_client.py"
_DISPATCH_PATH = _REPO_ROOT / "src" / "cora" / "tools" / "tool_dispatch.py"
_FNDR_PROMPT_PATH = _REPO_ROOT / "design" / "system-prompts" / "fndr.md"

NOTION_CLIENT_SRC = _NOTION_CLIENT_PATH.read_text(encoding="utf-8")
DISPATCH_SRC = _DISPATCH_PATH.read_text(encoding="utf-8")
FNDR_PROMPT_SRC = _FNDR_PROMPT_PATH.read_text(encoding="utf-8")

# ---------------------------------------------------------------------------
# Layer B import guard
# ---------------------------------------------------------------------------

_CLIENT_AVAILABLE = False
try:
    sys.path.insert(0, str(_REPO_ROOT / "src"))
    from cora.tools.notion_client import (  # noqa: E402
        NotionClientError,
        _PRESS_DB_ID,
        _PRESS_TARGETS,
        _rich_text,
        _url,
        get_press_pipeline_summary_text,
    )
    _CLIENT_AVAILABLE = True
except (ImportError, SyntaxError):
    pass

_skip_if_no_client = pytest.mark.skipif(
    not _CLIENT_AVAILABLE,
    reason="notion_client import unavailable (stale bash mount or missing deps)",
)

_DISPATCH_AVAILABLE = False
try:
    from cora.tools.tool_dispatch import (  # noqa: E402
        _tool_fndr_press_pipeline_summary,
    )
    _DISPATCH_AVAILABLE = True
except (ImportError, SyntaxError):
    pass

_skip_if_no_dispatch = pytest.mark.skipif(
    not _DISPATCH_AVAILABLE,
    reason="tool_dispatch import unavailable (stale bash mount or missing deps)",
)


# ===========================================================================
# Layer A — source assertions (4 wiring points + scope guard + prompt)
# ===========================================================================


class TestPressClientPresent:
    def test_press_db_id_present(self):
        assert "b139a18460f447f0ab761ba0570bd4e2" in NOTION_CLIENT_SRC

    def test_function_defined(self):
        assert "def get_press_pipeline_summary_text" in NOTION_CLIENT_SRC

    def test_targets_present(self):
        # F3 Energy 3, Lexington 2
        assert '"F3E": 3' in NOTION_CLIENT_SRC
        assert '"Lexington": 2' in NOTION_CLIENT_SRC

    def test_status_values_present(self):
        for status in ("To pitch", "Pitched", "Responded", "Published"):
            assert status in NOTION_CLIENT_SRC

    def test_both_entity_handled(self):
        # "Both"-tagged features must count toward both entities
        assert '"Both"' in NOTION_CLIENT_SRC

    def test_published_progress_marker(self):
        assert "✅" in NOTION_CLIENT_SRC and "⏳" in NOTION_CLIENT_SRC

    def test_fallback_i_dont_have_that(self):
        assert "I don't have that right now" in NOTION_CLIENT_SRC

    def test_db_id_parameterized_query(self):
        # _query_db / _paginate must accept db_id so the contracts DB isn't hardcoded
        assert "db_id" in NOTION_CLIENT_SRC


class TestToolDispatchWiring:
    def test_notion_client_imported(self):
        assert "notion_client" in DISPATCH_SRC

    def test_handler_defined(self):
        assert "def _tool_fndr_press_pipeline_summary" in DISPATCH_SRC

    def test_catalog_entry_present(self):
        assert '"fndr_press_pipeline_summary"' in DISPATCH_SRC

    def test_dispatch_table_entry_present(self):
        exact = '"fndr_press_pipeline_summary": _tool_fndr_press_pipeline_summary'
        assert exact in DISPATCH_SRC or (
            "_tool_fndr_press_pipeline_summary" in DISPATCH_SRC
            and '"fndr_press_pipeline_summary"' in DISPATCH_SRC
        )

    def test_timeout_entry_present(self):
        assert '"fndr_press_pipeline_summary": 12' in DISPATCH_SRC

    def test_scope_guard_in_handler(self):
        # Handler must restrict to FNDR/HJRG
        start = DISPATCH_SRC.find("def _tool_fndr_press_pipeline_summary")
        section = DISPATCH_SRC[start: start + 900]
        assert "FNDR" in section and "HJRG" in section

    def test_catalog_description_mentions_press(self):
        assert "press" in DISPATCH_SRC.lower()


class TestFndrPromptUpdated:
    def test_prompt_mentions_tool(self):
        assert "fndr_press_pipeline_summary" in FNDR_PROMPT_SRC

    def test_prompt_has_press_section(self):
        assert "Press pipeline" in FNDR_PROMPT_SRC


# ===========================================================================
# Layer B — behavioral tests via import (mocked Notion payload)
# ===========================================================================


def _make_page(
    reporter: str = "Jane Doe",
    outlet: str = "BevNET",
    angle: str | None = "A - leaving Monster",
    status: str = "To pitch",
    entity: str | None = "F3E",
    date_pitched: str | None = None,
    coverage_link: str | None = None,
    page_url: str = "https://www.notion.so/pressrow",
) -> dict:
    """Build a minimal mock Notion page dict for a press-pipeline row."""
    props: dict = {
        "Reporter": {"title": [{"plain_text": reporter}]},
        "Outlet": {"rich_text": [{"plain_text": outlet}]} if outlet else {"rich_text": []},
        "Angle": {"select": {"name": angle}} if angle else {"select": None},
        "Status": {"select": {"name": status}} if status else {"select": None},
        "Entity": {"select": {"name": entity}} if entity else {"select": None},
        "Date Pitched": {"date": {"start": date_pitched}} if date_pitched else {"date": None},
        "Coverage Link": {"url": coverage_link},
    }
    return {"id": "test-id", "url": page_url, "properties": props}


class TestHelpers:
    @_skip_if_no_client
    def test_rich_text_extracts(self):
        props = {"Outlet": {"rich_text": [{"plain_text": "Sports Illustrated"}]}}
        assert _rich_text(props, "Outlet") == "Sports Illustrated"

    @_skip_if_no_client
    def test_rich_text_empty(self):
        assert _rich_text({}, "Outlet") == ""

    @_skip_if_no_client
    def test_url_extracts(self):
        props = {"Coverage Link": {"url": "https://si.com/feature"}}
        assert _url(props, "Coverage Link") == "https://si.com/feature"

    @_skip_if_no_client
    def test_url_none(self):
        assert _url({"Coverage Link": {"url": None}}, "Coverage Link") is None

    @_skip_if_no_client
    def test_press_db_id_constant(self):
        assert _PRESS_DB_ID == "b139a18460f447f0ab761ba0570bd4e2"

    @_skip_if_no_client
    def test_targets_constant(self):
        assert _PRESS_TARGETS == {"F3E": 3, "Lexington": 2}


class TestPressPipelineSummary:
    @_skip_if_no_client
    def test_empty_when_no_pages(self):
        with patch("cora.tools.notion_client._paginate", return_value=[]):
            result = get_press_pipeline_summary_text()
        assert "empty" in result.lower()

    @_skip_if_no_client
    def test_header_total_count(self):
        pages = [_make_page(reporter="A"), _make_page(reporter="B")]
        with patch("cora.tools.notion_client._paginate", return_value=pages):
            result = get_press_pipeline_summary_text()
        assert "2 contacts" in result

    @_skip_if_no_client
    def test_status_breakdown_in_header(self):
        pages = [
            _make_page(reporter="A", status="To pitch"),
            _make_page(reporter="B", status="Pitched"),
        ]
        with patch("cora.tools.notion_client._paginate", return_value=pages):
            result = get_press_pipeline_summary_text()
        assert "To pitch 1" in result
        assert "Pitched 1" in result

    @_skip_if_no_client
    def test_published_progress_zero(self):
        pages = [_make_page(status="To pitch", entity="F3E")]
        with patch("cora.tools.notion_client._paginate", return_value=pages):
            result = get_press_pipeline_summary_text()
        assert "0/3 published" in result
        assert "0/2 published" in result

    @_skip_if_no_client
    def test_published_f3e_counted(self):
        pages = [
            _make_page(reporter="Pub1", status="Published", entity="F3E"),
            _make_page(reporter="Pub2", status="Published", entity="F3E"),
        ]
        with patch("cora.tools.notion_client._paginate", return_value=pages):
            result = get_press_pipeline_summary_text()
        assert "2/3 published" in result

    @_skip_if_no_client
    def test_both_entity_counts_toward_both(self):
        pages = [_make_page(reporter="Dual", status="Published", entity="Both")]
        with patch("cora.tools.notion_client._paginate", return_value=pages):
            result = get_press_pipeline_summary_text()
        # Counts toward F3E (1/3) AND Lexington (1/2)
        assert "1/3 published" in result
        assert "1/2 published" in result

    @_skip_if_no_client
    def test_target_met_shows_check(self):
        pages = [
            _make_page(reporter=f"R{i}", status="Published", entity="Lexington")
            for i in range(2)
        ]
        with patch("cora.tools.notion_client._paginate", return_value=pages):
            result = get_press_pipeline_summary_text()
        assert "✅" in result
        assert "2/2 published" in result

    @_skip_if_no_client
    def test_active_lists_pitched_and_responded(self):
        pages = [
            _make_page(reporter="PitchedGuy", status="Pitched", date_pitched="2026-06-01"),
            _make_page(reporter="RespondedGal", status="Responded", date_pitched="2026-06-03"),
        ]
        with patch("cora.tools.notion_client._paginate", return_value=pages):
            result = get_press_pipeline_summary_text()
        assert "PitchedGuy" in result
        assert "RespondedGal" in result
        assert "Active (2)" in result

    @_skip_if_no_client
    def test_active_oldest_pitched_first(self):
        pages = [
            _make_page(reporter="Newer", status="Pitched", date_pitched="2026-06-10"),
            _make_page(reporter="Older", status="Pitched", date_pitched="2026-06-01"),
        ]
        with patch("cora.tools.notion_client._paginate", return_value=pages):
            result = get_press_pipeline_summary_text()
        assert result.index("Older") < result.index("Newer")

    @_skip_if_no_client
    def test_to_pitch_section(self):
        pages = [_make_page(reporter="ToPitchPerson", status="To pitch")]
        with patch("cora.tools.notion_client._paginate", return_value=pages):
            result = get_press_pipeline_summary_text()
        assert "To pitch (1)" in result
        assert "ToPitchPerson" in result

    @_skip_if_no_client
    def test_published_coverage_deep_link(self):
        pages = [
            _make_page(
                reporter="Pub",
                status="Published",
                entity="F3E",
                coverage_link="https://si.com/the-feature",
            )
        ]
        with patch("cora.tools.notion_client._paginate", return_value=pages):
            result = get_press_pipeline_summary_text()
        assert "https://si.com/the-feature" in result

    @_skip_if_no_client
    def test_row_deep_link_in_active(self):
        pages = [
            _make_page(
                reporter="X",
                status="Pitched",
                page_url="https://www.notion.so/uniquepressrow",
            )
        ]
        with patch("cora.tools.notion_client._paginate", return_value=pages):
            result = get_press_pipeline_summary_text()
        assert "https://www.notion.so/uniquepressrow" in result

    @_skip_if_no_client
    def test_notion_error_returns_fallback(self):
        with patch(
            "cora.tools.notion_client._paginate",
            side_effect=NotionClientError("403 no access"),
        ):
            result = get_press_pipeline_summary_text()
        assert "I don't have that right now" in result

    @_skip_if_no_client
    def test_untitled_rows_skipped(self):
        # A page with no Reporter title should be skipped, not crash
        blank = {"id": "x", "url": "", "properties": {"Reporter": {"title": []}}}
        good = _make_page(reporter="RealOne", status="To pitch")
        with patch("cora.tools.notion_client._paginate", return_value=[blank, good]):
            result = get_press_pipeline_summary_text()
        assert "1 contacts" in result
        assert "RealOne" in result


class TestScopeGuard:
    @_skip_if_no_dispatch
    def test_refuses_in_entity_channel(self):
        result = _tool_fndr_press_pipeline_summary("U123", "OSN", {})
        assert "founder" in result.lower()
        assert "ask me" in result.lower()

    @_skip_if_no_dispatch
    def test_allows_fndr_channel(self):
        with patch(
            "cora.tools.notion_client.get_press_pipeline_summary_text",
            return_value="SENTINEL_OUTPUT",
        ):
            result = _tool_fndr_press_pipeline_summary("U123", "FNDR", {})
        assert result == "SENTINEL_OUTPUT"

    @_skip_if_no_dispatch
    def test_allows_hjrg_channel(self):
        with patch(
            "cora.tools.notion_client.get_press_pipeline_summary_text",
            return_value="SENTINEL_OUTPUT",
        ):
            result = _tool_fndr_press_pipeline_summary("U123", "HJRG", {})
        assert result == "SENTINEL_OUTPUT"
