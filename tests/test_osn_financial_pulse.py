"""Unit tests for the osn_financial_pulse tool.

Covers:
  - get_osn_pulse_text() in financial_client.py
  - _tool_osn_financial_pulse() handler in tool_dispatch.py

All tests patch get_cashflow() so no real Google Sheets calls are made.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from cora.connectors.gsheets_financials import (
    CashflowSummary,
    EntityRow,
    GsheetsConnectorError,
)
from cora.tools.financial_client import (
    UNKNOWN_RESPONSE,
    get_osn_pulse_text,
)


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────

def _make_summary(
    *,
    week_label: str = "Week of 5/19/2026",
    as_of_date: str = "2026-05-21",
    entities: list[EntityRow] | None = None,
    portfolio_forecast: float | None = None,
    portfolio_actual: float | None = None,
    portfolio_diff: float | None = None,
    opening_balance: float | None = None,
    closing_balance: float | None = None,
) -> CashflowSummary:
    return CashflowSummary(
        week_label=week_label,
        as_of_date=as_of_date,
        entities=entities or [],
        portfolio_forecast=portfolio_forecast,
        portfolio_actual=portfolio_actual,
        portfolio_diff=portfolio_diff,
        opening_balance=opening_balance,
        closing_balance=closing_balance,
    )


def _make_store_row(
    entity_code: str,
    *,
    label: str = "",
    forecast: float | None = 50_000.0,
    actual: float | None = 47_000.0,
    diff: float | None = -3_000.0,
) -> EntityRow:
    return EntityRow(
        label=label or entity_code,
        entity_code=entity_code,
        forecast=forecast,
        actual=actual,
        diff=diff,
    )


_GC_PATH = "cora.tools.financial_client.get_cashflow"
_AUDIT_PATH = "cora.tools.financial_client._audit"


# ────────────────────────────────────────────────────────────────────────────
# Happy-path: standard 4-store week
# ────────────────────────────────────────────────────────────────────────────

class TestGetOsnPulseTextHappyPath:
    """get_osn_pulse_text returns a well-formed Slack message on success."""

    def _four_store_summary(self) -> CashflowSummary:
        return _make_summary(
            entities=[
                _make_store_row("OSN-GW", label="OSN Warner",    forecast=55_000, actual=52_000, diff=-3_000),
                _make_store_row("OSN-GF", label="OSN Greenfield", forecast=40_000, actual=38_000, diff=-2_000),
                _make_store_row("OSN-VV", label="OSN Val Vista",  forecast=35_000, actual=36_000, diff=1_000),
                _make_store_row("OSN-MK", label="OSN McKellips",  forecast=30_000, actual=29_000, diff=-1_000),
            ],
            portfolio_forecast=160_000,
            portfolio_actual=155_000,
            portfolio_diff=-5_000,
        )

    def test_header_present(self):
        with patch(_GC_PATH, return_value=self._four_store_summary()), \
             patch(_AUDIT_PATH):
            result = get_osn_pulse_text()
        assert "OSN Financial Pulse" in result
        assert "Week of 5/19/2026" in result

    def test_as_of_date_present(self):
        with patch(_GC_PATH, return_value=self._four_store_summary()), \
             patch(_AUDIT_PATH):
            result = get_osn_pulse_text()
        assert "2026-05-21" in result

    def test_store_breakdown_heading(self):
        with patch(_GC_PATH, return_value=self._four_store_summary()), \
             patch(_AUDIT_PATH):
            result = get_osn_pulse_text()
        assert "Store breakdown" in result

    def test_all_four_store_labels_present(self):
        with patch(_GC_PATH, return_value=self._four_store_summary()), \
             patch(_AUDIT_PATH):
            result = get_osn_pulse_text()
        for label in ["Gilbert & Warner", "Greenfield & 60", "Val Vista & Pecos", "Gilbert & McKellips"]:
            assert label in result, f"Expected label '{label}' in output"

    def test_financial_values_formatted(self):
        with patch(_GC_PATH, return_value=self._four_store_summary()), \
             patch(_AUDIT_PATH):
            result = get_osn_pulse_text()
        # Spot-check one store: OSN-GW actual $52,000
        assert "$52,000" in result

    def test_portfolio_total_section(self):
        with patch(_GC_PATH, return_value=self._four_store_summary()), \
             patch(_AUDIT_PATH):
            result = get_osn_pulse_text()
        assert "OSN Total" in result
        assert "$160,000" in result   # forecast
        assert "$155,000" in result   # actual

    def test_negative_diff_shows_rotating_light(self):
        """Stores with diff < 0 get a :rotating_light: warning."""
        with patch(_GC_PATH, return_value=self._four_store_summary()), \
             patch(_AUDIT_PATH):
            result = get_osn_pulse_text()
        assert ":rotating_light:" in result

    def test_positive_diff_no_rotating_light_for_that_store(self):
        """Val Vista is positive; no :rotating_light: should appear on its line."""
        summary = self._four_store_summary()
        # Replace all stores with just the positive one
        summary.entities = [_make_store_row("OSN-VV", forecast=35_000, actual=36_000, diff=1_000)]
        summary.portfolio_diff = 1_000
        with patch(_GC_PATH, return_value=summary), \
             patch(_AUDIT_PATH):
            result = get_osn_pulse_text()
        assert ":rotating_light:" not in result

    def test_source_opaque_no_sheet_name(self):
        """Must not expose 'OSN Consolidated' or any sheet reference."""
        with patch(_GC_PATH, return_value=self._four_store_summary()), \
             patch(_AUDIT_PATH):
            result = get_osn_pulse_text()
        assert "Consolidated" not in result
        assert "CF_" not in result
        assert "gsheet" not in result.lower()

    def test_audit_called_on_success(self):
        with patch(_GC_PATH, return_value=self._four_store_summary()), \
             patch(_AUDIT_PATH) as mock_audit:
            get_osn_pulse_text(channel="osn-leadership", user="U12345")
        mock_audit.assert_called_once()
        call_kwargs = mock_audit.call_args.kwargs
        assert call_kwargs["result_type"] == "success"
        assert call_kwargs["channel"] == "osn-leadership"
        assert call_kwargs["user"] == "U12345"


# ────────────────────────────────────────────────────────────────────────────
# Store code label mapping
# ────────────────────────────────────────────────────────────────────────────

class TestStoreCodeMapping:
    """Each known entity_code maps to the correct human-readable store name."""

    @pytest.mark.parametrize("entity_code, expected_label", [
        ("OSN-GW",  "Gilbert & Warner"),
        ("OSN-WR",  "Gilbert & Warner"),
        ("OSN-MK",  "Gilbert & McKellips"),
        ("OSN-GM",  "Gilbert & McKellips"),
        ("OSN-GF",  "Greenfield & 60"),
        ("OSN-VV",  "Val Vista & Pecos"),
        ("OSN-VVP", "Val Vista & Pecos"),
    ])
    def test_known_code_maps_to_label(self, entity_code, expected_label):
        summary = _make_summary(
            entities=[_make_store_row(entity_code, label="raw label")]
        )
        with patch(_GC_PATH, return_value=summary), \
             patch(_AUDIT_PATH):
            result = get_osn_pulse_text()
        assert expected_label in result

    def test_unknown_code_falls_back_to_row_label(self):
        """An entity_code not in the label dict falls back to EntityRow.label."""
        summary = _make_summary(
            entities=[_make_store_row("OSN-XX", label="OSN Mystery Store")]
        )
        with patch(_GC_PATH, return_value=summary), \
             patch(_AUDIT_PATH):
            result = get_osn_pulse_text()
        assert "OSN Mystery Store" in result

    def test_code_lookup_is_case_insensitive(self):
        """entity_code is .upper()'d before lookup, so lower/mixed-case rows work."""
        summary = _make_summary(
            entities=[_make_store_row("osn-gw", label="raw")]
        )
        with patch(_GC_PATH, return_value=summary), \
             patch(_AUDIT_PATH):
            result = get_osn_pulse_text()
        assert "Gilbert & Warner" in result


# ────────────────────────────────────────────────────────────────────────────
# Edge cases: missing optional fields
# ────────────────────────────────────────────────────────────────────────────

class TestMissingOptionalFields:

    def test_store_with_none_actual_omits_actual_line(self):
        summary = _make_summary(
            entities=[_make_store_row("OSN-GW", actual=None, diff=None)]
        )
        with patch(_GC_PATH, return_value=summary), \
             patch(_AUDIT_PATH):
            result = get_osn_pulse_text()
        # Should still render; just no "actual" in the store line
        assert "Gilbert & Warner" in result
        # No crash
        assert UNKNOWN_RESPONSE not in result

    def test_store_with_none_forecast_omits_forecast_line(self):
        summary = _make_summary(
            entities=[_make_store_row("OSN-GF", forecast=None)]
        )
        with patch(_GC_PATH, return_value=summary), \
             patch(_AUDIT_PATH):
            result = get_osn_pulse_text()
        assert "Greenfield & 60" in result
        assert UNKNOWN_RESPONSE not in result

    def test_no_portfolio_totals_no_total_section(self):
        """If portfolio_forecast/actual/diff are all None, the OSN Total block is omitted."""
        summary = _make_summary(
            entities=[_make_store_row("OSN-GW")],
            portfolio_forecast=None,
            portfolio_actual=None,
            portfolio_diff=None,
        )
        with patch(_GC_PATH, return_value=summary), \
             patch(_AUDIT_PATH):
            result = get_osn_pulse_text()
        assert "OSN Total" not in result


# ────────────────────────────────────────────────────────────────────────────
# Empty store rows
# ────────────────────────────────────────────────────────────────────────────

class TestEmptyStoreRows:

    def test_no_store_rows_shows_fallback_message(self):
        """When osn_entities() returns [], show a helpful message instead of crashing."""
        summary = _make_summary(entities=[])   # no OSN-* rows
        with patch(_GC_PATH, return_value=summary), \
             patch(_AUDIT_PATH):
            result = get_osn_pulse_text()
        assert "No store-level rows" in result
        # Should still have the header
        assert "OSN Financial Pulse" in result

    def test_no_store_rows_directs_to_hayden_or_justin(self):
        summary = _make_summary(entities=[])
        with patch(_GC_PATH, return_value=summary), \
             patch(_AUDIT_PATH):
            result = get_osn_pulse_text()
        assert "Hayden" in result or "Justin" in result


# ────────────────────────────────────────────────────────────────────────────
# Non-OSN entities are filtered out
# ────────────────────────────────────────────────────────────────────────────

class TestNonOsnEntitiesFiltered:

    def test_non_osn_rows_not_shown(self):
        """Rows like LEX-LLC or HJRG in the summary should not appear in OSN pulse output."""
        summary = _make_summary(
            entities=[
                EntityRow(label="LEX LLC", entity_code="LEX-LLC", forecast=10_000, actual=9_000, diff=-1_000),
                _make_store_row("OSN-GW", actual=50_000),
            ]
        )
        with patch(_GC_PATH, return_value=summary), \
             patch(_AUDIT_PATH):
            result = get_osn_pulse_text()
        assert "LEX" not in result
        assert "Gilbert & Warner" in result


# ────────────────────────────────────────────────────────────────────────────
# Error handling
# ────────────────────────────────────────────────────────────────────────────

class TestErrorHandling:

    def test_gsheets_connector_error_returns_unknown_response(self):
        with patch(_GC_PATH, side_effect=GsheetsConnectorError("sheet unavailable")), \
             patch(_AUDIT_PATH):
            result = get_osn_pulse_text()
        assert result == UNKNOWN_RESPONSE

    def test_gsheets_connector_error_audits_connector_error(self):
        with patch(_GC_PATH, side_effect=GsheetsConnectorError("oops")), \
             patch(_AUDIT_PATH) as mock_audit:
            get_osn_pulse_text(channel="osn-leadership", user="U99")
        call_kwargs = mock_audit.call_args.kwargs
        assert call_kwargs["result_type"] == "connector_error"

    def test_unexpected_exception_returns_unknown_response(self):
        with patch(_GC_PATH, side_effect=RuntimeError("boom")), \
             patch(_AUDIT_PATH):
            result = get_osn_pulse_text()
        assert result == UNKNOWN_RESPONSE

    def test_unexpected_exception_audits_unexpected_error(self):
        with patch(_GC_PATH, side_effect=ValueError("unexpected")), \
             patch(_AUDIT_PATH) as mock_audit:
            get_osn_pulse_text(channel="c", user="u")
        call_kwargs = mock_audit.call_args.kwargs
        assert call_kwargs["result_type"] == "unexpected_error"

    def test_unknown_response_verbatim(self):
        """UNKNOWN_RESPONSE must be returned exactly — no modifications."""
        with patch(_GC_PATH, side_effect=GsheetsConnectorError("x")), \
             patch(_AUDIT_PATH):
            result = get_osn_pulse_text()
        assert result == (
            "I don't have that right now. I will notify the finance department "
            "immediately to obtain the information and provide the correct and "
            "updated answer when you ask again."
        )


# ────────────────────────────────────────────────────────────────────────────
# Currency formatting
# ────────────────────────────────────────────────────────────────────────────

class TestCurrencyFormatting:

    def test_positive_value_formatted_with_dollar_comma(self):
        summary = _make_summary(
            entities=[_make_store_row("OSN-GW", actual=52_000, forecast=55_000, diff=-3_000)],
            portfolio_actual=52_000,
        )
        with patch(_GC_PATH, return_value=summary), \
             patch(_AUDIT_PATH):
            result = get_osn_pulse_text()
        assert "$52,000" in result

    def test_negative_value_formatted_with_minus(self):
        summary = _make_summary(
            entities=[_make_store_row("OSN-GW", actual=-5_000, forecast=50_000, diff=-55_000)],
        )
        with patch(_GC_PATH, return_value=summary), \
             patch(_AUDIT_PATH):
            result = get_osn_pulse_text()
        assert "-$5,000" in result

    def test_diff_positive_shows_plus(self):
        """Positive diff (under budget = good) shows a + prefix."""
        summary = _make_summary(
            entities=[_make_store_row("OSN-VV", actual=51_000, forecast=50_000, diff=1_000)],
        )
        with patch(_GC_PATH, return_value=summary), \
             patch(_AUDIT_PATH):
            result = get_osn_pulse_text()
        assert "+$1,000" in result


# ────────────────────────────────────────────────────────────────────────────
# Tool dispatch handler
# ────────────────────────────────────────────────────────────────────────────

class TestToolDispatchHandler:
    """Verify that _tool_osn_financial_pulse is wired into _TOOL_FUNCTIONS."""

    def test_osn_financial_pulse_in_tool_functions(self):
        from cora.tools.tool_dispatch import _TOOL_FUNCTIONS
        assert "osn_financial_pulse" in _TOOL_FUNCTIONS

    def test_handler_callable(self):
        from cora.tools.tool_dispatch import _TOOL_FUNCTIONS
        handler = _TOOL_FUNCTIONS["osn_financial_pulse"]
        assert callable(handler)

    def test_dispatch_routes_to_get_osn_pulse_text(self):
        """Calling the handler returns what get_osn_pulse_text returns."""
        from cora.tools.tool_dispatch import _TOOL_FUNCTIONS

        summary = _make_summary(
            entities=[_make_store_row("OSN-GW")],
            portfolio_actual=52_000,
        )
        with patch(_GC_PATH, return_value=summary), \
             patch(_AUDIT_PATH):
            result = _TOOL_FUNCTIONS["osn_financial_pulse"](
                slack_user_id="U123",
                entity="OSN",
                _input={"_channel_name": "osn-finance"},
            )
        assert "OSN Financial Pulse" in result

    def test_osn_financial_pulse_in_tool_definitions(self):
        """The tool must also be in TOOL_DEFINITIONS so Claude can invoke it."""
        from cora.tools.tool_dispatch import TOOL_DEFINITIONS
        names = {t["name"] for t in TOOL_DEFINITIONS}
        assert "osn_financial_pulse" in names

    def test_tool_definition_has_required_keys(self):
        from cora.tools.tool_dispatch import TOOL_DEFINITIONS
        td = next(t for t in TOOL_DEFINITIONS if t["name"] == "osn_financial_pulse")
        assert "description" in td
        assert "input_schema" in td
        assert td["input_schema"]["type"] == "object"

    def test_tool_definition_description_mentions_osn(self):
        from cora.tools.tool_dispatch import TOOL_DEFINITIONS
        td = next(t for t in TOOL_DEFINITIONS if t["name"] == "osn_financial_pulse")
        assert "OSN" in td["description"]

    def test_tool_definition_description_mentions_mandatory(self):
        """Must have the MANDATORY TOOL CALL directive to override LLM KB bypass."""
        from cora.tools.tool_dispatch import TOOL_DEFINITIONS
        td = next(t for t in TOOL_DEFINITIONS if t["name"] == "osn_financial_pulse")
        assert "MANDATORY" in td["description"]
