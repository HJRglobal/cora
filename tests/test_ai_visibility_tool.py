"""Wiring + exposure tests for the f3e_ai_visibility tool + report reader."""

from __future__ import annotations

import pytest

from cora.tools import tool_dispatch as td
from cora.ai_visibility import report as rpt
from cora.ai_visibility import store as st
from cora.ai_visibility.scorer import BrandScore


@pytest.fixture(autouse=True)
def _no_eval_mode(monkeypatch):
    monkeypatch.delenv("CORA_EVAL_MODE", raising=False)


# --- registration / wiring ---
def test_tool_in_definitions_and_functions():
    names = {t["name"] for t in td.TOOL_DEFINITIONS}
    assert "f3e_ai_visibility" in names
    assert "f3e_ai_visibility" in td._TOOL_FUNCTIONS


def test_tool_definition_shape():
    entry = next(t for t in td.TOOL_DEFINITIONS if t["name"] == "f3e_ai_visibility")
    assert entry["input_schema"]["properties"] == {}
    assert "AI visibility" in entry["description"] or "AI-visibility" in entry["description"]


def test_timeout_is_fast_tier():
    assert td._TOOL_TIMEOUTS.get("f3e_ai_visibility") == 8


# --- exposure ---
def test_exposed_to_f3e():
    names = {t["name"] for t in td.tools_for_entity("F3E")}
    assert "f3e_ai_visibility" in names


def test_exposed_to_fndr_and_hjrg():
    for ent in ("FNDR", "HJRG"):
        names = {t["name"] for t in td.tools_for_entity(ent)}
        assert "f3e_ai_visibility" in names, ent


def test_founder_from_any_channel_sees_it():
    names = {t["name"] for t in td.tools_for_entity("OSN", cross_entity=True)}
    assert "f3e_ai_visibility" in names


def test_not_exposed_to_osn_or_lex():
    for ent in ("OSN", "LEX", "UFL", "HJRP"):
        names = {t["name"] for t in td.tools_for_entity(ent)}
        assert "f3e_ai_visibility" not in names, ent


# --- runtime entity guard ---
def test_function_refuses_outside_f3e():
    fn = td._TOOL_FUNCTIONS["f3e_ai_visibility"]
    out = fn("U1", "OSN", {})
    assert "scoped to F3 Energy" in out


def test_function_allows_f3e_and_fndr(tmp_path):
    st.set_db_path(tmp_path / "av.db")  # empty DB -> _NO_SCAN, proves it reached report
    try:
        for ent in ("F3E", "FNDR", "HJRG"):
            out = td._TOOL_FUNCTIONS["f3e_ai_visibility"]("U1", ent, {})
            assert "No AI visibility scan has completed yet" in out, ent
    finally:
        st.set_db_path(None)


# --- report reader ---
def test_report_no_scan(tmp_path):
    st.set_db_path(tmp_path / "av.db")
    try:
        assert "No AI visibility scan has completed yet" in rpt.get_tool_summary()
    finally:
        st.set_db_path(None)


def test_report_summary_after_scan(tmp_path):
    st.set_db_path(tmp_path / "av.db")
    try:
        sid = st.create_scan(basket_version=1, models=["perplexity_sonar"], runs_per_prompt=1,
                             brands=["energy"])
        st.save_score(sid, BrandScore(brand="energy", composite=42.0, composite_direct_only=42.0,
                                      presence=50, share_of_voice=30, position=33, sentiment=60,
                                      unaided_presence=44.0))
        st.finish_scan(sid, status="completed", total_calls=5, total_cost_usd=0.1,
                       aio_included=False)
        summary = rpt.get_tool_summary()
        assert "F3 Energy: 42/100" in summary
        assert "first run - no baseline" in summary
        assert "unaided presence 44%" in summary
        assert "Google AI Overviews coverage was not available" in summary
    finally:
        st.set_db_path(None)


# --- F3E system prompt has the mandatory tool-call line ---
def test_f3e_prompt_has_ai_visibility_section():
    from pathlib import Path
    text = Path("design/system-prompts/f3e.md").read_text(encoding="utf-8")
    assert "## AI visibility (mandatory tool call)" in text
    assert "f3e_ai_visibility" in text
