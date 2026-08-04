"""Tests for the weekly finance close-support pack core (src/cora/finance_close.py).

Coverage priorities, in order of how badly a regression would hurt:

1. **Honest stub, never a blank.** The acceptance criterion: a renamed
   Standing-ACTUALS row label must produce an unavailable SECTION, not a wall of
   blank figures (2026-06-04 doctrine).
2. **No silent partials.** A failed entity must still appear, and every section
   must state "N of M" coverage (the D-051 silent-partial-digest defect class).
3. **Deterministic computation.** Thresholds, deltas and period windows are pure
   functions of their inputs -- no model involvement anywhere in the facts block.
4. **Mapping drift.** Every provisioned QBO realm is either cross-check-mapped or
   explicitly excluded with a reason.
"""

from __future__ import annotations

import datetime
import json

import pytest

from cora import finance_close as fc


# ── fixtures / builders ──────────────────────────────────────────────────────

MONDAY = datetime.date(2026, 8, 3)


def _bs(bank: float | None) -> dict:
    """A QBO BalanceSheet payload shaped like the real nested tree."""
    inner = []
    if bank is not None:
        inner.append({
            "type": "Section",
            "Header": {"ColData": [{"value": "Bank Accounts"}]},
            "Summary": {"ColData": [
                {"value": "Total Bank Accounts"}, {"value": f"{bank:.2f}"},
            ]},
        })
    return {
        "Header": {"ReportName": "BalanceSheet"},
        "Rows": {"Row": [{
            "type": "Section",
            "Header": {"ColData": [{"value": "ASSETS"}]},
            "Rows": {"Row": [{
                "type": "Section",
                "Header": {"ColData": [{"value": "Current Assets"}]},
                "Rows": {"Row": inner},
            }]},
        }]},
    }


def _aging(total: float, oldest: float = 0.0, oldest_title: str = "91 and over") -> dict:
    return {
        "Columns": {"Column": [
            {"ColTitle": ""}, {"ColTitle": "Current"}, {"ColTitle": "1 - 30"},
            {"ColTitle": oldest_title}, {"ColTitle": "Total"},
        ]},
        "Rows": {"Row": [{
            "type": "Section",
            "Summary": {"ColData": [
                {"value": "TOTAL"}, {"value": "0.00"}, {"value": "0.00"},
                {"value": f"{oldest:.2f}"}, {"value": f"{total:.2f}"},
            ]},
        }]},
    }


def _pnl(revenue: float | None, expenses: float | None, basis: str = "Accrual") -> dict:
    rows = []
    if revenue is not None:
        rows.append({
            "type": "Section",
            "Header": {"ColData": [{"value": "Income"}]},
            "Summary": {"ColData": [{"value": "Total Income"}, {"value": f"{revenue:.2f}"}]},
        })
    if expenses is not None:
        rows.append({
            "type": "Section",
            "Header": {"ColData": [{"value": "Expenses"}]},
            "Summary": {"ColData": [{"value": "Total Expenses"}, {"value": f"{expenses:.2f}"}]},
        })
    return {"Header": {"ReportBasis": basis}, "Rows": {"Row": rows}}


def _sources(**over) -> fc.Sources:
    """Fully-stubbed Sources -- no default reaches the network."""
    base = dict(
        provisioned_entities=lambda: ["F3E", "OSNGW"],
        cash_closing=lambda e: {
            "closing": 100_000.0, "is_actual": True,
            "week_label": "Week of 7/27/2026", "stale": False, "age_days": 3,
        },
        balance_sheet=lambda e, a: _bs(100_500.0),
        ar_aging=lambda e: _aging(50_000.0, 5_000.0),
        ap_aging=lambda e: _aging(20_000.0, 1_000.0),
        profit_loss=lambda e, s, x: _pnl(200_000.0, 150_000.0),
        renewals=lambda: [],
        adherence_facts=lambda: None,
    )
    base.update(over)
    return fc.Sources(**base)


# ── extractors ───────────────────────────────────────────────────────────────

def test_extract_bank_balance_walks_nested_tree():
    assert fc.extract_bank_balance(_bs(12_345.67)) == pytest.approx(12_345.67)


def test_extract_bank_balance_none_when_section_absent():
    """No Bank Accounts section must yield None, never a substituted total.

    Falling back to total assets would fold AR and fixed assets into "cash" and
    make every cash delta wrong while looking authoritative.
    """
    assert fc.extract_bank_balance(_bs(None)) is None
    assert fc.extract_bank_balance({}) is None


def test_extract_bank_balance_handles_accounting_negative():
    report = _bs(0.0)
    inner = report["Rows"]["Row"][0]["Rows"]["Row"][0]["Rows"]["Row"][0]
    inner["Summary"]["ColData"][-1]["value"] = "(1,234.56)"
    assert fc.extract_bank_balance(report) == pytest.approx(-1234.56)


def test_extract_aging_reads_total_and_oldest_bucket():
    got = fc.extract_aging(_aging(9_000.0, 2_500.0, "91 and over"))
    assert got is not None
    assert got["total"] == pytest.approx(9_000.0)
    assert got["oldest_amount"] == pytest.approx(2_500.0)
    assert got["oldest_label"] == "91 and over"


def test_extract_aging_none_without_summary_row():
    assert fc.extract_aging({"Rows": {"Row": []}}) is None
    assert fc.extract_aging({}) is None


def test_extract_aging_oldest_label_positional_not_name_matched():
    """Bucket titles are read positionally so a relabelled bucket still works."""
    got = fc.extract_aging(_aging(100.0, 40.0, "Plus de 90 jours"))
    assert got["oldest_label"] == "Plus de 90 jours"


def test_extract_pnl_expenses():
    assert fc.extract_pnl_expenses(_pnl(10.0, 7_500.0)) == pytest.approx(7_500.0)
    assert fc.extract_pnl_expenses(_pnl(10.0, None)) is None


def test_extract_pnl_expenses_ignores_cogs_and_other():
    report = {"Rows": {"Row": [
        {"type": "Section",
         "Header": {"ColData": [{"value": "Cost of Goods Sold"}]},
         "Summary": {"ColData": [{"value": "Total COGS"}, {"value": "500.00"}]}},
        {"type": "Section",
         "Header": {"ColData": [{"value": "Other Expenses"}]},
         "Summary": {"ColData": [{"value": "Total Other"}, {"value": "99.00"}]}},
    ]}}
    assert fc.extract_pnl_expenses(report) is None


# ── thresholds ───────────────────────────────────────────────────────────────

def test_crosses_absolute_arm():
    assert fc._crosses(6_000.0, 1_000_000.0, fc.CASH_DELTA_ABS, fc.CASH_DELTA_PCT)
    assert not fc._crosses(100.0, 1_000_000.0, fc.CASH_DELTA_ABS, fc.CASH_DELTA_PCT)


def test_crosses_relative_arm_needs_a_floor():
    """A 100% swing on a tiny base must not flag."""
    assert not fc._crosses(12.0, 12.0, fc.CASH_DELTA_ABS, fc.CASH_DELTA_PCT)
    assert fc._crosses(1_000.0, 10_000.0, fc.CASH_DELTA_ABS, fc.CASH_DELTA_PCT)


def test_crosses_none_delta_never_flags():
    assert not fc._crosses(None, 1_000.0, fc.CASH_DELTA_ABS, fc.CASH_DELTA_PCT)


# ── formatting ───────────────────────────────────────────────────────────────

def test_fmt_money_never_blank():
    """An empty cell in a finance table reads as zero. Unknowns must be named."""
    assert fc.fmt_money(None) == "n/a"
    assert fc.fmt_money(0.0) == "$0"
    assert fc.fmt_money(-1500.0) == "-$1,500"
    assert fc.fmt_money(1_234_567.0) == "$1,234,567"


def test_fmt_delta_signs():
    assert fc.fmt_delta(500.0) == "+$500"
    assert fc.fmt_delta(-500.0) == "-$500"
    assert fc.fmt_delta(None) == "n/a"


def test_section_render_stub_and_empty_available():
    stub = fc.Section(key="k", title="T", available=False, stub_reason="dead source")
    assert any("section unavailable" in line for line in stub.render())
    empty = fc.Section(key="k", title="T", available=True, lines=[])
    assert any("no data rows returned" in line for line in empty.render())


# ── period window ────────────────────────────────────────────────────────────

def test_last_completed_months_uses_full_calendar_months():
    cur, prior, cur_label, prior_label = fc.last_completed_months(datetime.date(2026, 8, 3))
    assert cur == ("2026-07-01", "2026-07-31")
    assert prior == ("2026-06-01", "2026-06-30")
    assert cur_label == "Jul 2026"
    assert prior_label == "Jun 2026"


def test_last_completed_months_crosses_year_boundary():
    cur, prior, _, _ = fc.last_completed_months(datetime.date(2026, 1, 9))
    assert cur == ("2025-12-01", "2025-12-31")
    assert prior == ("2025-11-01", "2025-11-30")


def test_last_completed_months_never_uses_a_partial_month():
    """A month-to-date leg would read as a revenue collapse in any early-month run."""
    for day in (1, 15, 28):
        cur, _, _, _ = fc.last_completed_months(datetime.date(2026, 3, day))
        assert cur == ("2026-02-01", "2026-02-28")


# ── mapping drift guard ──────────────────────────────────────────────────────

def test_every_provisioned_realm_is_mapped_or_explicitly_excluded():
    """Drift guard: a newly provisioned realm must be a deliberate decision.

    Silently dropping it would shrink the cross-check without anyone noticing.
    """
    from cora.connectors.qbo_oauth import list_provisioned_entities

    for entity in list_provisioned_entities():
        assert (
            entity in fc.QBO_TO_SHEET_ENTITY or entity in fc.PACK_EXCLUDED_ENTITIES
        ), f"QBO realm {entity} is neither cash-check-mapped nor explicitly excluded"


def test_mapping_targets_are_real_cashflow_tabs():
    """Every mapped sheet code must resolve to a real tab, not the CF_SUMMARY fallback.

    entity_to_tab() falls back to CF_SUMMARY for unknown codes, so a typo'd code
    would silently compare an OSN store against the portfolio roll-up.
    """
    from cora.connectors.gsheets_financials import ENTITY_TO_TAB

    for qbo_code, sheet_code in fc.QBO_TO_SHEET_ENTITY.items():
        assert sheet_code in ENTITY_TO_TAB, (
            f"{qbo_code} -> {sheet_code} is not a known cash-sheet entity code"
        )


def test_osngm_maps_to_mckellips():
    """Pins the by-elimination inference. If OSN adds a store, revisit this."""
    assert fc.QBO_TO_SHEET_ENTITY["OSNGM"] == "OSN-MK"


def test_hrllc_excluded_with_a_reason():
    assert "HRLLC" in fc.PACK_EXCLUDED_ENTITIES
    assert fc.PACK_EXCLUDED_ENTITIES["HRLLC"].strip()


# ── cash section: the label-drift acceptance criterion ───────────────────────

def test_cash_section_all_none_closing_becomes_stub_not_blanks():
    """ACCEPTANCE: a renamed sheet row must yield an unavailable SECTION.

    gsheets_financials returns None (not an error) when its row-label frozensets
    stop matching, which on 2026-06-04 rendered a portfolio-wide wall of '--'
    that read as zeros. The section must refuse to render instead.
    """
    section, snap = fc.build_cash_section(
        ["F3E", "OSNGW"],
        _sources(cash_closing=lambda e: {
            "closing": None, "is_actual": True,
            "week_label": "Week of 7/27/2026", "stale": False, "age_days": 3,
        }),
        today=MONDAY,
    )
    assert section.available is False
    assert "row labels may have been renamed" in (section.stub_reason or "")
    assert snap == {}
    rendered = "\n".join(section.render())
    assert "$" not in rendered  # no figure of any kind leaks into the stub


def test_cash_section_read_failure_for_all_is_a_distinct_stub():
    """A hard read failure is reported as an outage, not as label drift."""
    def boom(_e):
        raise RuntimeError("sheets api down")

    section, _ = fc.build_cash_section(["F3E"], _sources(cash_closing=boom), today=MONDAY)
    assert section.available is False
    assert "unreadable" in (section.stub_reason or "")
    assert "renamed" not in (section.stub_reason or "")


def test_cash_section_partial_failure_still_lists_the_entity():
    """No silent partials: a per-entity failure must appear AND be counted."""
    def one_ok(entity):
        if entity == "F3E":
            return {"closing": 100_000.0, "is_actual": True,
                    "week_label": "Week of 7/27/2026", "stale": False, "age_days": 3}
        return {"closing": None, "is_actual": True,
                "week_label": "Week of 7/27/2026", "stale": False, "age_days": 3}

    section, _ = fc.build_cash_section(
        ["F3E", "OSNGW"], _sources(cash_closing=one_ok), today=MONDAY,
    )
    assert section.available is True
    body = "\n".join(section.lines)
    assert "OSN Warner: unavailable" in body
    assert "Cross-checked 1 of 2" in body


def test_cash_section_flags_material_delta():
    section, snap = fc.build_cash_section(
        ["F3E"],
        _sources(balance_sheet=lambda e, a: _bs(140_000.0)),  # +40k vs 100k sheet
        today=MONDAY,
    )
    assert section.flags == 1
    assert ":triangular_flag_on_post:" in "\n".join(section.lines)
    assert snap["F3E"]["delta"] == pytest.approx(40_000.0)


def test_cash_section_does_not_flag_immaterial_delta():
    section, _ = fc.build_cash_section(
        ["F3E"], _sources(balance_sheet=lambda e, a: _bs(100_400.0)), today=MONDAY,
    )
    assert section.flags == 0


def test_cash_section_surfaces_stale_sheet():
    section, _ = fc.build_cash_section(
        ["F3E"],
        _sources(cash_closing=lambda e: {
            "closing": 100_000.0, "is_actual": True,
            "week_label": "Week of 6/1/2026", "stale": True, "age_days": 63,
        }),
        today=MONDAY,
    )
    body = "\n".join(section.lines)
    assert "BEHIND" in body and "63d" in body


def test_cash_section_uses_the_sheet_week_as_balance_sheet_as_of():
    """Both legs must describe the same moment or every delta looks like a break."""
    seen: list[str] = []

    def bs(entity, as_of):
        seen.append(as_of)
        return _bs(100_000.0)

    fc.build_cash_section(["F3E"], _sources(balance_sheet=bs), today=MONDAY)
    assert seen == ["2026-07-27"]


def test_cash_section_labels_unparsed_week_date():
    section, _ = fc.build_cash_section(
        ["F3E"],
        _sources(cash_closing=lambda e: {
            "closing": 100_000.0, "is_actual": True,
            "week_label": "no date here", "stale": False, "age_days": None,
        }),
        today=MONDAY,
    )
    assert "week date unparsed" in "\n".join(section.lines)


def test_cash_section_reports_excluded_entities():
    section, _ = fc.build_cash_section(["F3E", "HRLLC"], _sources(), today=MONDAY)
    body = "\n".join(section.lines)
    assert "Excluded" in body and "HR LLC" in body


def test_cash_section_stub_when_nothing_mappable():
    section, _ = fc.build_cash_section(["HRLLC"], _sources(), today=MONDAY)
    assert section.available is False
    assert "no provisioned entity maps" in (section.stub_reason or "")


# ── aging section ────────────────────────────────────────────────────────────

def test_aging_section_first_run_says_no_deltas_yet():
    section, snap = fc.build_aging_section(["F3E"], _sources(), None)
    assert "First run" in "\n".join(section.lines)
    assert snap["F3E"]["ar"] == pytest.approx(50_000.0)


def test_aging_section_computes_wow_delta_and_flags():
    prior = {"aging": {"F3E": {"ar": 20_000.0, "ap": 20_000.0}}}
    section, _ = fc.build_aging_section(["F3E"], _sources(), prior)
    body = "\n".join(section.lines)
    assert "+$30,000" in body
    assert section.flags == 1


def test_aging_section_surfaces_oldest_bucket():
    section, _ = fc.build_aging_section(["F3E"], _sources(), None)
    assert "aged tail" in "\n".join(section.lines)
    assert "91 and over" in "\n".join(section.lines)


def test_aging_section_stub_when_no_entity_returns_a_total():
    section, _ = fc.build_aging_section(
        ["F3E", "OSNGW"],
        _sources(ar_aging=lambda e: {}, ap_aging=lambda e: {}),
        None,
    )
    assert section.available is False
    assert "no aging report returned" in (section.stub_reason or "")


def test_aging_section_partial_failure_lists_entity_and_coverage():
    def ar(entity):
        if entity == "F3E":
            return _aging(1_000.0)
        raise RuntimeError("realm 401")

    section, _ = fc.build_aging_section(
        ["F3E", "OSNGW"], _sources(ar_aging=ar, ap_aging=lambda e: {}), None,
    )
    body = "\n".join(section.lines)
    assert "OSN Warner: unavailable" in body
    assert "1 of 2" in body


# ── P&L section ──────────────────────────────────────────────────────────────

def test_pnl_section_flags_material_swing():
    def pnl(entity, start, end):
        return _pnl(200_000.0 if start.startswith("2026-07") else 100_000.0, 150_000.0)

    section, snap = fc.build_pnl_section(["F3E"], _sources(profit_loss=pnl), today=MONDAY)
    body = "\n".join(section.lines)
    assert "+$100,000 MoM" in body
    assert section.flags == 1
    assert snap["F3E"]["revenue"] == pytest.approx(200_000.0)


def test_pnl_section_labels_basis():
    section, _ = fc.build_pnl_section(
        ["F3E"],
        _sources(profit_loss=lambda e, s, x: _pnl(10_000.0, 5_000.0, basis="Cash")),
        today=MONDAY,
    )
    assert "[Cash basis]" in "\n".join(section.lines)


def test_pnl_section_basis_change_suppresses_the_flag():
    """A basis switch between months makes the swing a report artifact, not news."""
    def pnl(entity, start, end):
        if start.startswith("2026-07"):
            return _pnl(500_000.0, 10_000.0, basis="Accrual")
        return _pnl(100_000.0, 10_000.0, basis="Cash")

    section, _ = fc.build_pnl_section(["F3E"], _sources(profit_loss=pnl), today=MONDAY)
    body = "\n".join(section.lines)
    assert "basis changed between months" in body
    assert section.flags == 0


def test_pnl_section_does_not_pin_accounting_method():
    """Pinning Accrual portfolio-wide would misstate the genuinely cash-basis books."""
    calls: list[tuple] = []

    def pnl(entity, start, end):
        calls.append((entity, start, end))
        return _pnl(1.0, 1.0)

    fc.build_pnl_section(["F3E"], _sources(profit_loss=pnl), today=MONDAY)
    # The Sources seam takes exactly (entity, start, end) -- no basis parameter
    # is threaded, so no caller can silently pin one.
    assert all(len(c) == 3 for c in calls)


def test_pnl_section_stub_when_no_totals_anywhere():
    section, _ = fc.build_pnl_section(
        ["F3E"], _sources(profit_loss=lambda e, s, x: _pnl(None, None)), today=MONDAY,
    )
    assert section.available is False
    assert "no P&L returned" in (section.stub_reason or "")


def test_pnl_section_handles_missing_prior_month():
    def pnl(entity, start, end):
        return _pnl(50_000.0, 10_000.0) if start.startswith("2026-07") else _pnl(None, None)

    section, _ = fc.build_pnl_section(["F3E"], _sources(profit_loss=pnl), today=MONDAY)
    assert "no prior month" in "\n".join(section.lines)
    assert section.flags == 0


# ── snapshots ────────────────────────────────────────────────────────────────

def test_write_and_load_prior_snapshot(tmp_path):
    fc.write_snapshot({"aging": {"F3E": {"ar": 1.0}}},
                      today=datetime.date(2026, 7, 27), snapshot_dir=tmp_path)
    got = fc.load_prior_snapshot(today=MONDAY, snapshot_dir=tmp_path)
    assert got is not None
    assert got["aging"]["F3E"]["ar"] == pytest.approx(1.0)
    assert got["_snapshot_date"] == "2026-07-27"


def test_load_prior_snapshot_ignores_same_day(tmp_path):
    """A same-day re-run must not diff against the file it is about to overwrite."""
    fc.write_snapshot({"aging": {}}, today=MONDAY, snapshot_dir=tmp_path)
    assert fc.load_prior_snapshot(today=MONDAY, snapshot_dir=tmp_path) is None


def test_load_prior_snapshot_none_when_empty(tmp_path):
    assert fc.load_prior_snapshot(today=MONDAY, snapshot_dir=tmp_path) is None


def test_load_prior_snapshot_skips_corrupt_file(tmp_path):
    (tmp_path / "2026-07-27.json").write_text("{not json", encoding="utf-8")
    fc.write_snapshot({"ok": True}, today=datetime.date(2026, 7, 20), snapshot_dir=tmp_path)
    got = fc.load_prior_snapshot(today=MONDAY, snapshot_dir=tmp_path)
    assert got is not None and got.get("ok") is True


def test_write_snapshot_prunes(tmp_path):
    for day in range(1, 8):
        fc.write_snapshot({"n": day}, today=datetime.date(2026, 7, day),
                          snapshot_dir=tmp_path, keep=3)
    assert len(list(tmp_path.glob("*.json"))) == 3


# ── renewal radar ────────────────────────────────────────────────────────────

def test_renewal_section_stub_when_map_missing():
    section = fc.build_renewal_section(_sources(renewals=lambda: None), today=MONDAY)
    assert section.available is False
    assert "missing or unreadable" in (section.stub_reason or "")


def test_renewal_section_stub_when_map_empty():
    """An empty radar must not render as a reassuring 'nothing due'."""
    section = fc.build_renewal_section(_sources(renewals=lambda: []), today=MONDAY)
    assert section.available is False
    assert "no entries" in (section.stub_reason or "")


def test_renewal_section_flags_past_due_and_imminent():
    items = [
        {"name": "Meta Verified", "entity": "F3E", "amount": 15, "next_due": "2026-07-20"},
        {"name": "Judge.me", "entity": "F3E", "amount": 99, "next_due": "2026-08-06"},
        {"name": "Far off", "next_due": "2026-12-01"},
    ]
    section = fc.build_renewal_section(_sources(renewals=lambda: items), today=MONDAY)
    body = "\n".join(section.lines)
    assert "PAST DUE 14d" in body
    assert "due in 3d" in body
    assert "Far off" not in body      # beyond the 45-day horizon
    assert section.flags == 2
    assert "Radar covers 3 tracked item(s); 2 within 45d." in body


def test_renewal_section_names_undated_entries_instead_of_dropping_them():
    items = [{"name": "Mystery sub"}, {"name": "Bad date", "next_due": "not-a-date"}]
    section = fc.build_renewal_section(_sources(renewals=lambda: items), today=MONDAY)
    body = "\n".join(section.lines)
    assert "no parseable next_due" in body
    assert "Mystery sub" in body and "Bad date" in body


def test_renewal_section_tolerates_non_numeric_amount():
    items = [{"name": "Weird", "amount": "fifteen dollars", "next_due": "2026-08-10"}]
    section = fc.build_renewal_section(_sources(renewals=lambda: items), today=MONDAY)
    assert "Weird" in "\n".join(section.lines)


def test_shipped_renewal_map_parses():
    """The committed map must load -- an unreadable map silently stubs the section."""
    items = fc.load_renewals()
    assert items is not None and len(items) >= 1
    assert all("name" in i for i in items)


# ── close-prep ───────────────────────────────────────────────────────────────

def test_close_prep_notes_absent_adherence_facts():
    cash = fc.Section(key="cash", title="c", available=True, lines=["x"])
    section = fc.build_close_prep_section(_sources(), cash_section=cash, today=MONDAY)
    assert "Adherence facts unavailable" in "\n".join(section.lines)


def test_close_prep_restates_cash_flags_as_unreconciled_signal():
    cash = fc.Section(key="cash", title="c", available=True, lines=["x"], flags=2)
    section = fc.build_close_prep_section(_sources(), cash_section=cash, today=MONDAY)
    body = "\n".join(section.lines)
    assert "2 entity(ies) show a cash delta over threshold" in body
    assert "unreconciled-looking" in body


def test_close_prep_says_agreed_when_no_cash_flags():
    cash = fc.Section(key="cash", title="c", available=True, lines=["x"], flags=0)
    section = fc.build_close_prep_section(_sources(), cash_section=cash, today=MONDAY)
    assert "agree within threshold" in "\n".join(section.lines)


def test_close_prep_reports_unavailable_cash_check():
    cash = fc.Section(key="cash", title="c", available=False, stub_reason="dead")
    section = fc.build_close_prep_section(_sources(), cash_section=cash, today=MONDAY)
    assert "reconciliation status unknown" in "\n".join(section.lines)


def test_close_prep_consumes_adherence_facts_and_flags_problems():
    facts = {
        "generated_date": "2026-08-03",
        "facts": [
            "cash_sheet: fresh (modified 1d ago)",
            "monthly_filing 2026-07: MISSING (no content)",
            "clover: lane_retired (2026-06-06 decision; SOP rev 4)",
        ],
    }
    cash = fc.Section(key="cash", title="c", available=True, lines=["x"], flags=0)
    section = fc.build_close_prep_section(
        _sources(adherence_facts=lambda: facts), cash_section=cash, today=MONDAY,
    )
    body = "\n".join(section.lines)
    assert "Adherence facts as of 2026-08-03" in body
    assert "monthly_filing 2026-07: MISSING" in body
    assert "lane_retired" in body
    assert section.flags == 1  # only the MISSING line flags


def test_close_prep_marks_stale_adherence_facts():
    facts = {"generated_date": "2026-06-01", "facts": ["cash_sheet: fresh"]}
    cash = fc.Section(key="cash", title="c", available=True, lines=["x"], flags=0)
    section = fc.build_close_prep_section(
        _sources(adherence_facts=lambda: facts), cash_section=cash, today=MONDAY,
    )
    assert "STALE" in "\n".join(section.lines)


def test_load_adherence_facts_missing_and_corrupt(tmp_path):
    assert fc.load_adherence_facts(tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{oops", encoding="utf-8")
    assert fc.load_adherence_facts(bad) is None
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"facts": ["a"]}), encoding="utf-8")
    assert fc.load_adherence_facts(good) == {"facts": ["a"]}


# ── whole-pack assembly ──────────────────────────────────────────────────────

def test_build_pack_all_sections_present(tmp_path):
    pack = fc.build_pack(_sources(), today=MONDAY, snapshot_dir=tmp_path)
    assert [s.key for s in pack.sections] == [
        "cash", "aging", "pnl", "close_prep", "renewals",
    ]
    rendered = pack.render()
    assert "Weekly Finance Close-Support Pack" in rendered
    assert "deterministic" in rendered


def test_build_pack_every_section_is_real_data_or_an_honest_stub(tmp_path):
    """ACCEPTANCE: no section may be silently empty."""
    pack = fc.build_pack(_sources(), today=MONDAY, snapshot_dir=tmp_path)
    for section in pack.sections:
        if section.available:
            assert section.lines, f"{section.key} is available but has no lines"
        else:
            assert section.stub_reason, f"{section.key} is a stub with no reason"


def test_build_pack_section_exception_becomes_a_stub(tmp_path):
    def boom(_e):
        raise ValueError("kaboom")

    pack = fc.build_pack(
        _sources(ar_aging=boom, ap_aging=boom), today=MONDAY, snapshot_dir=tmp_path,
    )
    aging = next(s for s in pack.sections if s.key == "aging")
    assert aging.available is False


def test_build_pack_writes_snapshot(tmp_path):
    fc.build_pack(_sources(), today=MONDAY, snapshot_dir=tmp_path)
    snap = json.loads((tmp_path / "2026-08-03.json").read_text(encoding="utf-8"))
    assert "cash" in snap and "aging" in snap and "pnl" in snap


def test_build_pack_can_skip_snapshot(tmp_path):
    fc.build_pack(_sources(), today=MONDAY, snapshot_dir=tmp_path, persist_snapshot=False)
    assert list(tmp_path.glob("*.json")) == []


def test_build_pack_no_provisioned_entities_stubs_everything(tmp_path):
    pack = fc.build_pack(
        _sources(provisioned_entities=lambda: []), today=MONDAY, snapshot_dir=tmp_path,
    )
    for key in ("cash", "aging", "pnl"):
        section = next(s for s in pack.sections if s.key == key)
        assert section.available is False
    assert len(pack.sections) == 5


def test_build_pack_provisioned_listing_failure_is_survivable(tmp_path):
    def boom():
        raise RuntimeError("token store gone")

    pack = fc.build_pack(
        _sources(provisioned_entities=boom), today=MONDAY, snapshot_dir=tmp_path,
    )
    assert len(pack.sections) == 5
    assert all(not s.available for s in pack.sections[:3])


def test_pack_footer_counts_flags_and_names_unavailable_sections(tmp_path):
    pack = fc.build_pack(
        _sources(
            balance_sheet=lambda e, a: _bs(200_000.0),   # big delta -> flags
            renewals=lambda: None,                        # -> stub
        ),
        today=MONDAY, snapshot_dir=tmp_path,
    )
    rendered = pack.render()
    assert "item(s) flagged" in rendered
    assert "renewals" in pack.unavailable_sections
    assert "section(s) unavailable" in rendered


# ── narration gate ───────────────────────────────────────────────────────────

def test_narration_disabled_by_default(monkeypatch):
    monkeypatch.delenv("FINANCE_CLOSE_NARRATE", raising=False)
    assert fc.narration_enabled() is False
    pack = fc.ClosePack(generated_at="2026-08-03")
    assert fc.narrate(pack) is None


def test_narration_gate_accepts_truthy_values(monkeypatch):
    for value in ("1", "true", "YES", "on"):
        monkeypatch.setenv("FINANCE_CLOSE_NARRATE", value)
        assert fc.narration_enabled() is True
    monkeypatch.setenv("FINANCE_CLOSE_NARRATE", "0")
    assert fc.narration_enabled() is False


def test_narration_returns_none_without_api_key(monkeypatch):
    monkeypatch.setenv("FINANCE_CLOSE_NARRATE", "true")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert fc.narrate(fc.ClosePack(generated_at="2026-08-03")) is None


def test_narration_failure_is_never_load_bearing(monkeypatch):
    """An API error must return None so the caller still posts the facts block."""
    monkeypatch.setenv("FINANCE_CLOSE_NARRATE", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    import sys
    import types

    fake = types.ModuleType("anthropic")

    class _Client:
        def __init__(self, **_kw):
            self.messages = self

        def create(self, **_kw):
            raise RuntimeError("api down")

    fake.Anthropic = _Client
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    assert fc.narrate(fc.ClosePack(generated_at="2026-08-03")) is None


def test_narration_is_given_only_the_computed_facts(monkeypatch):
    """The model must never see a raw report -- it cannot invent a figure."""
    monkeypatch.setenv("FINANCE_CLOSE_NARRATE", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    import sys
    import types

    captured: dict = {}
    fake = types.ModuleType("anthropic")

    class _Msg:
        content = [types.SimpleNamespace(text="Look at F3 Energy first.")]
        usage = types.SimpleNamespace(
            input_tokens=10, output_tokens=5,
            cache_creation_input_tokens=0, cache_read_input_tokens=0,
        )

    class _Client:
        def __init__(self, **_kw):
            self.messages = self

        def create(self, **kw):
            captured.update(kw)
            return _Msg()

    fake.Anthropic = _Client
    monkeypatch.setitem(sys.modules, "anthropic", fake)

    pack = fc.ClosePack(generated_at="2026-08-03", sections=[
        fc.Section(key="cash", title="Cash", lines=["• F3 Energy: delta +$40,000"]),
    ])
    got = fc.narrate(pack)
    assert got == "Look at F3 Energy first."
    prompt = captured["messages"][0]["content"]
    assert "+$40,000" in prompt
    assert "never compute" in prompt
    # Adaptive thinking must stay disabled (D-091): max_tokens caps thinking+output.
    assert captured["thinking"] == {"type": "disabled"}
