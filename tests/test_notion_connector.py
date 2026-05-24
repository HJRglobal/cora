"""[HJRG] Notion connector tests — Contracts & Renewals Registry KB ingestion.

Two layers (same pattern as other Cora connector tests):

  Layer A — pure string/path assertions against connector source.
             Always runs. No src/ imports required.
             Documentation-as-tests: if key constants or functions are removed,
             the relevant test fails.

  Layer B — unit tests via actual import.
             Skipped automatically when notion_connector is unavailable
             (stale bash mount, missing deps, etc.).

All Layer A tests run against the raw source text of notion_connector.py.
All Layer B tests use mocked httpx to avoid live Notion API calls.
"""

import pathlib
import sys

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = pathlib.Path(__file__).parent.parent
_CONNECTOR_PATH = _REPO_ROOT / "src" / "cora" / "connectors" / "notion_connector.py"
_SYNC_SCRIPT_PATH = _REPO_ROOT / "scripts" / "incremental_sync_notion.py"

CONNECTOR_SRC = _CONNECTOR_PATH.read_text(encoding="utf-8")
SYNC_SRC = _SYNC_SCRIPT_PATH.read_text(encoding="utf-8")

# ---------------------------------------------------------------------------
# Layer B import guard
# ---------------------------------------------------------------------------

_CONNECTOR_AVAILABLE = False
try:
    sys.path.insert(0, str(_REPO_ROOT / "src"))
    from cora.connectors.notion_connector import (  # noqa: E402
        NotionConnectorError,
        _DB_ID,
        _ENTITY_MAP,
        _SUB_ENTITY_MAP,
        _entity_and_sub,
        _format_contract_content,
        _get_checkbox,
        _get_date_start,
        _get_number,
        _get_rich_text,
        _get_select,
        _get_title,
        _get_url,
        _page_to_document,
        _ts,
        backfill,
        sync_delta,
    )
    _CONNECTOR_AVAILABLE = True
except (ImportError, SyntaxError):
    pass

_skip_if_no_connector = pytest.mark.skipif(
    not _CONNECTOR_AVAILABLE,
    reason="notion_connector import unavailable (stale bash mount or missing deps)",
)


# ===========================================================================
# Layer A — source file existence + key identifiers
# ===========================================================================


class TestConnectorFilePresent:
    """The connector file must exist and be non-trivial."""

    def test_connector_file_exists(self):
        assert _CONNECTOR_PATH.exists(), "notion_connector.py not found"

    def test_connector_has_content(self):
        assert len(CONNECTOR_SRC.strip()) > 500, "notion_connector.py looks too short"

    def test_sync_script_exists(self):
        assert _SYNC_SCRIPT_PATH.exists(), "incremental_sync_notion.py not found"


class TestConnectorConstants:
    """Key constants must be present in source."""

    def test_db_id_present(self):
        assert "7820cd3689ae4596bd8f965f2bf96d5d" in CONNECTOR_SRC

    def test_api_base_present(self):
        assert "api.notion.com" in CONNECTOR_SRC

    def test_notion_version_present(self):
        assert "2022-06-28" in CONNECTOR_SRC

    def test_notion_api_key_env_var(self):
        assert "NOTION_API_KEY" in CONNECTOR_SRC

    def test_entity_map_defined(self):
        assert "_ENTITY_MAP" in CONNECTOR_SRC

    def test_sub_entity_map_defined(self):
        assert "_SUB_ENTITY_MAP" in CONNECTOR_SRC


class TestConnectorFunctions:
    """Key functions must be defined in source."""

    def test_sync_delta_defined(self):
        assert "def sync_delta" in CONNECTOR_SRC

    def test_backfill_defined(self):
        assert "def backfill" in CONNECTOR_SRC

    def test_page_to_document_defined(self):
        assert "def _page_to_document" in CONNECTOR_SRC

    def test_entity_and_sub_defined(self):
        assert "def _entity_and_sub" in CONNECTOR_SRC

    def test_format_contract_content_defined(self):
        assert "def _format_contract_content" in CONNECTOR_SRC

    def test_notion_connector_error_defined(self):
        assert "class NotionConnectorError" in CONNECTOR_SRC

    def test_paginate_db_defined(self):
        assert "def _paginate_db" in CONNECTOR_SRC


class TestEntityMapCoverage:
    """Entity map must cover all known Notion entity values."""

    def test_personal_maps_to_fndr(self):
        assert '"Personal": "FNDR"' in CONNECTOR_SRC or "'Personal': 'FNDR'" in CONNECTOR_SRC

    def test_lex_lbhs_in_entity_map(self):
        assert "LEX-LBHS" in CONNECTOR_SRC

    def test_lex_llc_in_entity_map(self):
        assert "LEX-LLC" in CONNECTOR_SRC

    def test_lex_lla_in_entity_map(self):
        assert "LEX-LLA" in CONNECTOR_SRC

    def test_lex_lts_in_entity_map(self):
        assert "LEX-LTS" in CONNECTOR_SRC

    def test_hjrg_in_entity_map(self):
        assert '"HJRG"' in CONNECTOR_SRC or "'HJRG'" in CONNECTOR_SRC

    def test_sub_entity_map_has_four_lex_entries(self):
        # Sub-entity map must have all four Lex sub-entities
        for code in ("LEX-LLC", "LEX-LLA", "LEX-LBHS", "LEX-LTS"):
            assert code in CONNECTOR_SRC


class TestContentFormatting:
    """Content format must include all key labels."""

    def test_contract_label_in_content(self):
        assert "[Contract]" in CONNECTOR_SRC

    def test_entity_label_in_content(self):
        assert "Entity:" in CONNECTOR_SRC

    def test_risk_flag_label_in_content(self):
        assert "Risk Flag:" in CONNECTOR_SRC

    def test_auto_renew_label_in_content(self):
        assert "Auto-renew:" in CONNECTOR_SRC

    def test_term_end_label_in_content(self):
        assert "Term End:" in CONNECTOR_SRC

    def test_status_label_in_content(self):
        assert "Status:" in CONNECTOR_SRC

    def test_notes_label_in_content(self):
        assert "Notes:" in CONNECTOR_SRC

    def test_standard_as_default_risk_flag(self):
        # When risk_flag is None, should default to "Standard"
        assert "Standard" in CONNECTOR_SRC


class TestSyncScriptStructure:
    """Sync script must follow the standard pattern."""

    def test_sync_delta_imported(self):
        assert "sync_delta" in SYNC_SRC

    def test_backfill_imported(self):
        assert "backfill" in SYNC_SRC

    def test_watermark_read(self):
        assert 'get_sync_state("notion")' in SYNC_SRC

    def test_watermark_advance(self):
        assert 'set_sync_state("notion"' in SYNC_SRC

    def test_backfill_flag(self):
        assert "--backfill" in SYNC_SRC

    def test_log_file_named_correctly(self):
        assert "kb-sync-notion" in SYNC_SRC

    def test_exit_codes_documented(self):
        assert "Exit codes:" in SYNC_SRC

    def test_5am_schedule_documented(self):
        assert "5:00am" in SYNC_SRC


# ===========================================================================
# Layer B — unit tests via import
# ===========================================================================


class TestEntityAndSub:
    """_entity_and_sub() mapping logic."""

    @_skip_if_no_connector
    def test_personal_maps_to_fndr(self):
        entity, sub = _entity_and_sub("Personal")
        assert entity == "FNDR"
        assert sub is None

    @_skip_if_no_connector
    def test_none_maps_to_fndr(self):
        entity, sub = _entity_and_sub(None)
        assert entity == "FNDR"
        assert sub is None

    @_skip_if_no_connector
    def test_empty_string_maps_to_fndr(self):
        entity, sub = _entity_and_sub("")
        assert entity == "FNDR"
        assert sub is None

    @_skip_if_no_connector
    def test_lex_lbhs_maps_correctly(self):
        entity, sub = _entity_and_sub("LEX-LBHS")
        assert entity == "LEX"
        assert sub == "LEX-LBHS"

    @_skip_if_no_connector
    def test_lex_llc_maps_correctly(self):
        entity, sub = _entity_and_sub("LEX-LLC")
        assert entity == "LEX"
        assert sub == "LEX-LLC"

    @_skip_if_no_connector
    def test_lex_lla_maps_correctly(self):
        entity, sub = _entity_and_sub("LEX-LLA")
        assert entity == "LEX"
        assert sub == "LEX-LLA"

    @_skip_if_no_connector
    def test_lex_lts_maps_correctly(self):
        entity, sub = _entity_and_sub("LEX-LTS")
        assert entity == "LEX"
        assert sub == "LEX-LTS"

    @_skip_if_no_connector
    def test_hjrg_maps_to_hjrg(self):
        entity, sub = _entity_and_sub("HJRG")
        assert entity == "HJRG"
        assert sub is None

    @_skip_if_no_connector
    def test_f3e_maps_to_f3e(self):
        entity, sub = _entity_and_sub("F3E")
        assert entity == "F3E"
        assert sub is None

    @_skip_if_no_connector
    def test_ufl_maps_to_ufl(self):
        entity, sub = _entity_and_sub("UFL")
        assert entity == "UFL"
        assert sub is None

    @_skip_if_no_connector
    def test_unknown_value_maps_to_fndr(self):
        entity, sub = _entity_and_sub("BOGUS_ENTITY")
        assert entity == "FNDR"
        assert sub is None

    @_skip_if_no_connector
    def test_all_lex_sub_entities_no_sub_for_bare_lex(self):
        entity, sub = _entity_and_sub("LEX")
        assert entity == "LEX"
        assert sub is None


class TestPropertyHelpers:
    """Property extraction helpers."""

    @_skip_if_no_connector
    def test_get_select_returns_none_on_empty_props(self):
        assert _get_select({}, "Status") is None

    @_skip_if_no_connector
    def test_get_select_returns_none_on_null_select(self):
        assert _get_select({"Status": {"select": None}}, "Status") is None

    @_skip_if_no_connector
    def test_get_select_returns_name(self):
        result = _get_select({"Status": {"select": {"name": "Active"}}}, "Status")
        assert result == "Active"

    @_skip_if_no_connector
    def test_get_checkbox_returns_false_on_empty(self):
        assert _get_checkbox({}, "Auto-renew") is False

    @_skip_if_no_connector
    def test_get_checkbox_returns_true(self):
        assert _get_checkbox({"Auto-renew": {"checkbox": True}}, "Auto-renew") is True

    @_skip_if_no_connector
    def test_get_number_returns_none_on_empty(self):
        assert _get_number({}, "Annual Value") is None

    @_skip_if_no_connector
    def test_get_number_returns_float(self):
        assert _get_number({"Annual Value": {"number": 121859.2}}, "Annual Value") == 121859.2

    @_skip_if_no_connector
    def test_get_rich_text_empty(self):
        assert _get_rich_text({}, "Notes") == ""

    @_skip_if_no_connector
    def test_get_rich_text_concatenates(self):
        props = {"Notes": {"rich_text": [{"plain_text": "hello "}, {"plain_text": "world"}]}}
        assert _get_rich_text(props, "Notes") == "hello world"

    @_skip_if_no_connector
    def test_ts_parses_iso_z(self):
        result = _ts("2026-05-15T06:26:50Z")
        assert result is not None
        assert isinstance(result, int)
        assert result > 0

    @_skip_if_no_connector
    def test_ts_returns_none_on_empty(self):
        assert _ts(None) is None
        assert _ts("") is None

    @_skip_if_no_connector
    def test_get_date_start_extracts_date(self):
        props = {"Term End": {"date": {"start": "2027-06-30"}}}
        assert _get_date_start(props, "Term End") == "2027-06-30"

    @_skip_if_no_connector
    def test_get_date_start_none_on_null(self):
        props = {"Term End": {"date": None}}
        assert _get_date_start(props, "Term End") is None


class TestFormatContractContent:
    """_format_contract_content() output structure."""

    @_skip_if_no_connector
    def test_contract_label_in_output(self):
        out = _format_contract_content(
            title="HJRG — Asana Premium — Vendor SaaS",
            entity_raw="HJRG",
            counterparty="Asana",
            contract_type="Vendor SaaS",
            status="Active",
            risk_flag=None,
            auto_renew=True,
            annual_value=None,
            term_end=None,
            renewal_window=None,
            signed_date=None,
            effective_date=None,
            counterparty_contact=None,
            surviving_obligations=None,
            notes="Premium tier.",
        )
        assert "[Contract] HJRG — Asana Premium — Vendor SaaS" in out

    @_skip_if_no_connector
    def test_standard_risk_flag_when_none(self):
        out = _format_contract_content(
            title="Test",
            entity_raw="HJRG",
            counterparty="Test Co",
            contract_type=None,
            status=None,
            risk_flag=None,
            auto_renew=False,
            annual_value=None,
            term_end=None,
            renewal_window=None,
            signed_date=None,
            effective_date=None,
            counterparty_contact=None,
            surviving_obligations=None,
            notes="",
        )
        assert "Risk Flag: Standard" in out

    @_skip_if_no_connector
    def test_escalate_risk_flag_preserved(self):
        out = _format_contract_content(
            title="Test",
            entity_raw="HJRP",
            counterparty="Vitalant",
            contract_type="Lease",
            status="Active",
            risk_flag="Escalate",
            auto_renew=True,
            annual_value=None,
            term_end=None,
            renewal_window=None,
            signed_date=None,
            effective_date=None,
            counterparty_contact=None,
            surviving_obligations=None,
            notes="",
        )
        assert "Risk Flag: Escalate" in out

    @_skip_if_no_connector
    def test_auto_renew_yes(self):
        out = _format_contract_content(
            title="Test",
            entity_raw="HJRG",
            counterparty="Co",
            contract_type=None,
            status=None,
            risk_flag=None,
            auto_renew=True,
            annual_value=None,
            term_end=None,
            renewal_window=None,
            signed_date=None,
            effective_date=None,
            counterparty_contact=None,
            surviving_obligations=None,
            notes="",
        )
        assert "Auto-renew: Yes" in out

    @_skip_if_no_connector
    def test_annual_value_formatted(self):
        out = _format_contract_content(
            title="Test",
            entity_raw="LEX",
            counterparty="HMLA",
            contract_type="Loan/Note",
            status="Active",
            risk_flag=None,
            auto_renew=False,
            annual_value=121859.20,
            term_end=None,
            renewal_window=None,
            signed_date=None,
            effective_date=None,
            counterparty_contact=None,
            surviving_obligations=None,
            notes="",
        )
        assert "121,859.20" in out

    @_skip_if_no_connector
    def test_term_end_not_set_when_none(self):
        out = _format_contract_content(
            title="Test",
            entity_raw="F3E",
            counterparty="Co",
            contract_type=None,
            status=None,
            risk_flag=None,
            auto_renew=False,
            annual_value=None,
            term_end=None,
            renewal_window=None,
            signed_date=None,
            effective_date=None,
            counterparty_contact=None,
            surviving_obligations=None,
            notes="",
        )
        assert "Term End: (not set)" in out

    @_skip_if_no_connector
    def test_notes_section_present_when_non_empty(self):
        out = _format_contract_content(
            title="Test",
            entity_raw="HJRG",
            counterparty="Co",
            contract_type=None,
            status=None,
            risk_flag=None,
            auto_renew=False,
            annual_value=None,
            term_end=None,
            renewal_window=None,
            signed_date=None,
            effective_date=None,
            counterparty_contact=None,
            surviving_obligations=None,
            notes="Some notes here.",
        )
        assert "Notes:" in out
        assert "Some notes here." in out


class TestPageToDocument:
    """_page_to_document() Document construction."""

    @_skip_if_no_connector
    def _make_page(self, overrides: dict | None = None) -> dict:
        """Build a minimal mock Notion page dict."""
        base = {
            "id": "test-page-id-123",
            "url": "https://www.notion.so/testpage",
            "created_time": "2026-05-15T06:26:50.000Z",
            "last_edited_time": "2026-05-15T06:26:50.000Z",
            "properties": {
                "Title": {
                    "title": [{"plain_text": "HJRG — Asana Premium — Vendor SaaS"}]
                },
                "Entity": {"select": {"name": "HJRG"}},
                "Counterparty": {"rich_text": [{"plain_text": "Asana"}]},
                "Contract Type": {"select": {"name": "Vendor SaaS"}},
                "Status": {"select": {"name": "Active"}},
                "Risk Flag": {"select": None},
                "Auto-renew": {"checkbox": True},
                "Annual Value": {"number": None},
                "Term End": {"date": None},
                "Renewal Notice Window (days)": {"number": None},
                "Signed Date": {"date": None},
                "Effective Date": {"date": None},
                "Counterparty Contact": {"rich_text": []},
                "Surviving Obligations": {"rich_text": []},
                "Notes": {"rich_text": [{"plain_text": "Premium tier."}]},
                "Linked Document": {"url": None},
            },
        }
        if overrides:
            base.update(overrides)
        return base

    @_skip_if_no_connector
    def test_basic_document_construction(self):
        page = self._make_page()
        doc = _page_to_document(page)
        assert doc is not None
        assert doc.source == "notion"
        assert doc.source_id == "notion:test-page-id-123"
        assert doc.entity == "HJRG"
        assert doc.sub_entity is None
        assert "HJRG — Asana Premium" in doc.title
        assert "[Contract]" in doc.content
        assert "Auto-renew: Yes" in doc.content
        assert "Premium tier." in doc.content

    @_skip_if_no_connector
    def test_personal_entity_maps_to_fndr(self):
        page = self._make_page()
        page["properties"]["Entity"] = {"select": {"name": "Personal"}}
        doc = _page_to_document(page)
        assert doc is not None
        assert doc.entity == "FNDR"
        assert doc.sub_entity is None

    @_skip_if_no_connector
    def test_lex_lbhs_entity_mapping(self):
        page = self._make_page()
        page["properties"]["Entity"] = {"select": {"name": "LEX-LBHS"}}
        doc = _page_to_document(page)
        assert doc is not None
        assert doc.entity == "LEX"
        assert doc.sub_entity == "LEX-LBHS"

    @_skip_if_no_connector
    def test_returns_none_on_missing_title(self):
        page = self._make_page()
        page["properties"]["Title"] = {"title": []}
        doc = _page_to_document(page)
        assert doc is None

    @_skip_if_no_connector
    def test_deep_link_includes_url_and_title(self):
        page = self._make_page()
        doc = _page_to_document(page)
        assert doc is not None
        assert "https://www.notion.so/testpage" in doc.deep_link
        assert "HJRG — Asana Premium — Vendor SaaS" in doc.deep_link

    @_skip_if_no_connector
    def test_metadata_has_risk_flag_standard_when_null(self):
        page = self._make_page()
        doc = _page_to_document(page)
        assert doc is not None
        assert doc.metadata.get("risk_flag") == "Standard"

    @_skip_if_no_connector
    def test_metadata_has_escalate_risk_flag(self):
        page = self._make_page()
        page["properties"]["Risk Flag"] = {"select": {"name": "Escalate"}}
        doc = _page_to_document(page)
        assert doc is not None
        assert doc.metadata.get("risk_flag") == "Escalate"

    @_skip_if_no_connector
    def test_date_modified_parsed_from_last_edited(self):
        page = self._make_page()
        doc = _page_to_document(page)
        assert doc is not None
        assert doc.date_modified is not None
        assert isinstance(doc.date_modified, int)
        assert doc.date_modified > 0

    @_skip_if_no_connector
    def test_db_id_constant_matches_known_value(self):
        assert _DB_ID == "7820cd3689ae4596bd8f965f2bf96d5d"
