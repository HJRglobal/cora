"""D-051 remediation pins for the finance close-support bundle (2026-08-04).

Every test here corresponds to a defect a four-lens adversarial review found that
the original 165-test suite passed straight through. They are grouped by the
property that was violated, because that is what a future change is most likely to
break again.

The review found two independently-confirmed HIGHs of the same shape -- a figure
that was *plausible but wrong* -- and both are pinned first.
"""

from __future__ import annotations

import datetime
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

from cora import drive_io
from cora import finance_adherence as fa
from cora import finance_close as fc

MONDAY = datetime.date(2026, 8, 3)
_REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clear_breaker():
    drive_io.reset_state_for_tests()
    yield
    drive_io.reset_state_for_tests()


def _script():
    path = _REPO / "scripts" / "run_finance_close_pack.py"
    spec = importlib.util.spec_from_file_location("_rfcp_d051", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["_rfcp_d051"] = module
    spec.loader.exec_module(module)
    return module


def _bs(bank: float | None, cards: float | None = None) -> dict:
    rows = []
    if bank is not None:
        rows.append({
            "type": "Section",
            "Header": {"ColData": [{"value": "Bank Accounts"}]},
            "Summary": {"ColData": [
                {"value": "Total Bank Accounts"}, {"value": f"{bank:.2f}"},
            ]},
        })
    if cards is not None:
        rows.append({
            "type": "Section",
            "Header": {"ColData": [{"value": "Credit Cards"}]},
            "Summary": {"ColData": [
                {"value": "Total Credit Cards"}, {"value": f"{cards:.2f}"},
            ]},
        })
    return {"Rows": {"Row": rows}}


def _aging(total: float, oldest: float | None = None, title: str = "91 and over") -> dict:
    """A QBO AgedReceivables/AgedPayables payload (the sources feed extract_aging)."""
    return {
        "Columns": {"Column": [
            {"ColTitle": ""}, {"ColTitle": "Current"}, {"ColTitle": "1 - 30"},
            {"ColTitle": title}, {"ColTitle": "Total"},
        ]},
        "Rows": {"Row": [{"type": "Section", "Summary": {"ColData": [
            {"value": "TOTAL"}, {"value": "0.00"}, {"value": "0.00"},
            {"value": "" if oldest is None else f"{oldest:.2f}"},
            {"value": f"{total:.2f}"},
        ]}}]},
    }


def _sources(**over) -> fc.Sources:
    base = dict(
        provisioned_entities=lambda: ["F3E"],
        cash_closing=lambda e: {
            "closing": 100_000.0, "is_actual": True,
            "week_label": "Week of 7/27/2026", "stale": False, "age_days": 3,
        },
        balance_sheet=lambda e, a: _bs(100_000.0),
        ar_aging=lambda e: _aging(0.0),
        ap_aging=lambda e: _aging(0.0),
        profit_loss=lambda e, s, x: {},
        renewals=lambda: [],
        adherence_facts=lambda: None,
    )
    base.update(over)
    return fc.Sources(**base)


# ═════════════════════════════════════════════════════════════════════════════
# HIGH 1 -- the cash leg must be the sheet's ACTUAL, not its FORECAST
#
# gsheets_financials.closing_balance is FORECAST-first
# ("forecast if forecast is not None else actual"); the actual-first value lives in
# ending_cash_series/ending_cash_outlook. A prior D-051 review already found and
# fixed this exact divergence in scripts/write_cashflow_snapshot.py ("they
# disagreed mid-week"). Comparing the sheet's forecast to the books' actual reports
# the sheet's own forecast variance as a reconciliation break.
# ═════════════════════════════════════════════════════════════════════════════

def test_cash_leg_reads_the_actual_first_ending_cash_not_closing_balance():
    """Books matching the sheet's ACTUAL must yield a zero delta and no flag."""
    from cora.connectors import gsheets_financials as gs

    # Latest-actual week where FORECAST (100k) and ACTUAL (140k) DISAGREE.
    csv_text = (
        "Entity,7/27/2026,7/27/2026\n"
        ",FORECAST,ACTUAL\n"
        "F3 Energy,1000,1200\n"
        "Ending Cash/CC Book Balance,100000,140000\n"
    )
    summary = gs._parse_cashflow_csv(csv_text, "2026-07-28")
    # Precondition: the legacy field really is the forecast.
    assert summary.closing_balance == pytest.approx(100_000.0)
    anchor = summary.ending_cash_outlook(weeks=0)
    assert anchor and anchor[0]["ending_cash"] == pytest.approx(140_000.0)
    assert anchor[0]["is_actual"] is True

    section, snap = fc.build_cash_section(
        ["F3E"],
        _sources(
            cash_closing=lambda e: {
                "closing": anchor[0]["ending_cash"],
                "is_actual": anchor[0]["is_actual"],
                "week_label": summary.week_label, "stale": False, "age_days": 1,
            },
            balance_sheet=lambda e, a: _bs(140_000.0),   # books agree with the ACTUAL
        ),
        today=MONDAY,
    )
    assert section.flags == 0, "books matching the sheet's actual must not flag"
    assert snap["F3E"]["delta"] == pytest.approx(0.0)
    assert "(actual)" in "\n".join(section.lines)


def test_forecast_only_week_is_labelled_and_never_flagged():
    """A forecast-vs-books difference is not a reconciliation signal either way."""
    section, _ = fc.build_cash_section(
        ["F3E"],
        _sources(
            cash_closing=lambda e: {
                "closing": 100_000.0, "is_actual": False,
                "week_label": "Week of 7/27/2026", "stale": False, "age_days": 1,
            },
            balance_sheet=lambda e, a: _bs(500_000.0),   # huge difference
        ),
        today=MONDAY,
    )
    body = "\n".join(section.lines)
    assert "FORECAST" in body
    assert "not a reconciliation comparison" in body
    assert section.flags == 0
    assert "1 compared against a FORECAST week" in body


def test_get_cash_closing_default_uses_the_outlook_anchor(monkeypatch):
    """Pins the SEAM: the default source must not read closing_balance."""
    from cora.connectors import gsheets_financials as gs

    class _Summary:
        week_label = "Week of 7/27/2026"
        closing_balance = 100_000.0     # the forecast-first trap

        def ending_cash_outlook(self, weeks=0):
            return [{"week": "7/27/2026", "ending_cash": 140_000.0, "is_actual": True}]

        def is_stale(self):
            return False

        def data_age_days(self):
            return 1

    monkeypatch.setattr(gs, "get_cashflow", lambda **_kw: _Summary())
    monkeypatch.setattr(gs, "entity_to_tab", lambda e: "CF_F3")
    got = fc.Sources().get_cash_closing("F3E")
    assert got["closing"] == pytest.approx(140_000.0)
    assert got["is_actual"] is True


def test_get_cash_closing_falls_back_marked_not_actual(monkeypatch):
    """Empty outlook -> forecast-first fallback, explicitly marked not-actual."""
    from cora.connectors import gsheets_financials as gs

    class _Summary:
        week_label = "Week of 7/27/2026"
        closing_balance = 100_000.0

        def ending_cash_outlook(self, weeks=0):
            return []

        def is_stale(self):
            return False

        def data_age_days(self):
            return 1

    monkeypatch.setattr(gs, "get_cashflow", lambda **_kw: _Summary())
    monkeypatch.setattr(gs, "entity_to_tab", lambda e: "CF_F3")
    got = fc.Sources().get_cash_closing("F3E")
    assert got["closing"] == pytest.approx(100_000.0)
    assert got["is_actual"] is False


# ═════════════════════════════════════════════════════════════════════════════
# HIGH 2 -- the sheet row is "Cash/CC", so the books leg must net credit cards
# ═════════════════════════════════════════════════════════════════════════════

def test_credit_card_balance_is_extracted():
    assert fc.extract_credit_card_balance(_bs(100.0, 2_500.0)) == pytest.approx(2_500.0)
    assert fc.extract_credit_card_balance(_bs(100.0)) is None


def test_cash_leg_nets_credit_cards_out_of_the_books_figure():
    """The sheet row is 'Ending Cash/CC Book Balance' -- cash NET of cards.

    QBO reports cards in their own section, invisible to extract_bank_balance, so a
    Bank-Accounts-only comparison produced a delta equal to the card balance for
    every card-carrying entity -- a recurring false "unreconciled-looking" flag.
    """
    section, snap = fc.build_cash_section(
        ["F3E"],
        _sources(balance_sheet=lambda e, a: _bs(112_000.0, 12_000.0)),
        today=MONDAY,
    )
    assert snap["F3E"]["books_net"] == pytest.approx(100_000.0)
    assert snap["F3E"]["delta"] == pytest.approx(0.0)
    assert section.flags == 0
    assert "less cards $12,000" in "\n".join(section.lines)


def test_absent_credit_card_section_is_reported_not_assumed():
    section, _ = fc.build_cash_section(
        ["F3E"], _sources(balance_sheet=lambda e, a: _bs(100_000.0)), today=MONDAY,
    )
    assert "no credit-card section in the books" in "\n".join(section.lines)
    assert section.flags == 0


def test_zero_card_balance_adds_no_noise():
    section, _ = fc.build_cash_section(
        ["F3E"], _sources(balance_sheet=lambda e, a: _bs(100_000.0, 0.0)), today=MONDAY,
    )
    body = "\n".join(section.lines)
    assert "less cards" not in body and "no credit-card section" not in body


# ═════════════════════════════════════════════════════════════════════════════
# HIGH 3 -- partial coverage must never read as an all-clear
# (the D-051 silent-partial-digest class, reproduced inside the summary layer)
# ═════════════════════════════════════════════════════════════════════════════

def _nine_of_ten_unreadable() -> fc.ClosePack:
    """The most likely production failure: QBO realm tokens expiring."""
    entities = ["F3E", "OSNGW", "OSNGF", "OSNVV", "OSNGM", "OSN", "LEX", "HJRG", "HJRP", "BDM"]

    def bs(entity, as_of):
        if entity == "F3E":
            return _bs(100_400.0)
        raise RuntimeError("401 unauthorized")

    return fc.build_pack(
        _sources(provisioned_entities=lambda: entities, balance_sheet=bs),
        today=MONDAY, persist_snapshot=False,
    )


def test_cash_section_records_structured_coverage():
    section, _ = fc.build_cash_section(
        ["F3E", "OSNGW"],
        _sources(balance_sheet=lambda e, a: _bs(100_000.0) if e == "F3E" else _bs(None)),
        today=MONDAY,
    )
    assert section.covered == 1
    assert section.expected == 2
    assert section.is_partial is True


def test_close_prep_flags_partial_coverage_instead_of_saying_agree():
    pack = _nine_of_ten_unreadable()
    cash = next(s for s in pack.sections if s.key == "cash")
    prep = next(s for s in pack.sections if s.key == "close_prep")
    body = "\n".join(prep.lines)
    assert cash.is_partial is True
    assert "reconciliation status UNKNOWN for 9 of 10" in body
    assert prep.flags >= 1


def test_founder_cut_never_claims_all_clear_on_partial_coverage():
    """The founder cut is the ONLY cut Harrison reads."""
    script = _script()
    cut = script.build_founder_cut(_nine_of_ten_unreadable())
    assert "No item crossed a flag threshold this week, and every section had full coverage." not in cut
    assert "coverage was INCOMPLETE" in cut or "reconciliation status UNKNOWN" in cut
    assert "could not cover everything" in cut


def test_founder_cut_surfaces_unavailable_lines_and_coverage_footer():
    script = _script()
    cut = script.build_founder_cut(_nine_of_ten_unreadable())
    assert "unavailable —" in cut
    assert " of 10" in cut


def test_founder_cut_full_coverage_no_flags_says_so_plainly():
    script = _script()
    pack = fc.build_pack(_sources(), today=MONDAY, persist_snapshot=False)
    cut = script.build_founder_cut(pack)
    cash = next(s for s in pack.sections if s.key == "cash")
    assert cash.is_partial is False


def test_founder_cut_is_duck_typed_not_isinstance_gated():
    """An isinstance() check against one import path skips sections built under the
    other (`cora.*` vs `src.cora.*` are distinct module objects here), turning a type
    mismatch into a false clean bill of health."""
    script = _script()

    class ForeignSection:
        key = "cash"
        title = "Cash"
        available = True
        stub_reason = None
        lines = [":triangular_flag_on_post: F3 Energy delta +$40,000"]
        flags = 1
        is_partial = False

    class ForeignPack:
        generated_at = "2026-08-03"
        sections = [ForeignSection()]
        total_flags = 1

    cut = script.build_founder_cut(ForeignPack())
    assert "+$40,000" in cut
    assert "No item crossed a flag threshold" not in cut


# ═════════════════════════════════════════════════════════════════════════════
# HIGH 4 -- the adherence job must never claim clear about what it could not read
# ═════════════════════════════════════════════════════════════════════════════

def _unknown_report() -> fa.AdherenceReport:
    facts = [
        fa.Fact(key=f"bank_statements {n}", status=fa.STATUS_UNKNOWN,
                text="could not read — the Drive mount was unreachable.",
                group="bank_statements", label=n)
        for n in fa.BANK_ENTITY_FOLDERS
    ]
    return fa.AdherenceReport(generated_date="2026-08-03", facts=facts)


def test_summary_line_does_not_say_all_clear_when_nothing_was_readable():
    line = _unknown_report().summary_line()
    assert "all clear" not in line
    assert "no problems in what could be read" in line
    assert "could NOT be read" in line


def test_summary_line_still_says_all_clear_when_genuinely_clear():
    report = fa.AdherenceReport(generated_date="2026-08-03", facts=[
        fa.Fact(key="cash_sheet", status=fa.STATUS_OK, text="fresh"),
    ])
    assert "all clear" in report.summary_line()


def test_rollup_of_unknown_group_does_not_claim_files_are_absent():
    """"No files found" about 13 folders nobody could open is a false absence claim."""
    line = _unknown_report().compact_facts()[0]
    assert "could not be read" in line
    assert "no files found" not in line
    assert "UNKNOWN" in line


def test_rollup_of_missing_group_still_says_no_files_found():
    facts = [
        fa.Fact(key=f"bank_statements {n}", status=fa.STATUS_MISSING,
                text="MISSING (no content)", group="bank_statements", label=n)
        for n in fa.BANK_ENTITY_FOLDERS[:4]
    ]
    line = fa.AdherenceReport(generated_date="2026-08-03", facts=facts).compact_facts()[0]
    assert "no files found" in line


def test_close_prep_flags_when_adherence_checks_were_unreadable():
    """A G: outage at 08:15 must not render as checked-and-clear at 09:00."""
    payload = _unknown_report().to_json()
    cash = fc.Section(key="cash", title="c", available=True, lines=["x"], flags=0,
                      covered=1, expected=1)
    prep = fc.build_close_prep_section(
        _sources(adherence_facts=lambda: payload), cash_section=cash, today=MONDAY,
    )
    body = "\n".join(prep.lines)
    assert "could NOT be read" in body
    assert prep.flags >= 1


# ═════════════════════════════════════════════════════════════════════════════
# Structured severity -- prose matching under-flagged the rolled-up group
# ═════════════════════════════════════════════════════════════════════════════

def test_facts_status_travels_parallel_to_facts():
    report = fa.AdherenceReport(generated_date="2026-08-03", facts=[
        fa.Fact(key="cash_sheet", status=fa.STATUS_OK, text="fresh"),
        fa.Fact(key="clover", status=fa.STATUS_RETIRED, text="lane_retired"),
    ])
    payload = report.to_json()
    assert payload["facts_status"] == [fa.STATUS_OK, fa.STATUS_RETIRED]
    assert len(payload["facts_status"]) == len(payload["facts"])


def test_close_prep_flags_a_rolled_up_stale_group_via_status_not_prose():
    """The roll-up's synthetic key matches no per-folder status key, so key lookup
    alone silently under-flagged exactly the group the roll-up exists to surface."""
    facts = [
        fa.Fact(key=f"bank_statements {n}", status=fa.STATUS_STALE,
                text="STALE — newest statement is 200d old",
                group="bank_statements", label=n, age_days=200)
        for n in fa.BANK_ENTITY_FOLDERS
    ]
    payload = fa.AdherenceReport(generated_date="2026-08-03", facts=facts).to_json()
    cash = fc.Section(key="cash", title="c", available=True, lines=["x"],
                      covered=1, expected=1)
    prep = fc.build_close_prep_section(
        _sources(adherence_facts=lambda: payload), cash_section=cash, today=MONDAY,
    )
    body = "\n".join(prep.lines)
    assert "bank_statements (13 folders)" in body
    assert ":triangular_flag_on_post:" in body


def test_close_prep_does_not_flag_ok_or_retired_statuses():
    payload = {
        "generated_date": "2026-08-03",
        "facts": ["cash_sheet: fresh", "clover: lane_retired (2026-06-06)"],
        "facts_status": [fa.STATUS_OK, fa.STATUS_RETIRED],
        "unknown_count": 0,
    }
    cash = fc.Section(key="cash", title="c", available=True, lines=["x"],
                      covered=1, expected=1)
    prep = fc.build_close_prep_section(
        _sources(adherence_facts=lambda: payload), cash_section=cash, today=MONDAY,
    )
    assert prep.flags == 0


def test_close_prep_falls_back_to_token_matching_without_statuses():
    payload = {
        "generated_date": "2026-08-03",
        "facts": ["monthly_filing 2026-07: MISSING (no content)"],
    }
    cash = fc.Section(key="cash", title="c", available=True, lines=["x"],
                      covered=1, expected=1)
    prep = fc.build_close_prep_section(
        _sources(adherence_facts=lambda: payload), cash_section=cash, today=MONDAY,
    )
    assert prep.flags == 1


# ═════════════════════════════════════════════════════════════════════════════
# Adherence read failures: UNKNOWN, never MISSING
# ═════════════════════════════════════════════════════════════════════════════

def test_non_mount_oserror_on_listing_is_unknown_not_missing(tmp_path, monkeypatch):
    """A PermissionError once rendered a FILED month as MISSING (no content)."""
    monkeypatch.setattr(
        drive_io, "glob",
        lambda *a, **k: (_ for _ in ()).throw(PermissionError("denied")),
    )
    fact = fa.check_monthly_filing(today=datetime.date(2026, 8, 4), root=tmp_path)
    assert fact.status == fa.STATUS_UNKNOWN
    assert fact.is_problem is False
    facts = fa.check_bank_statements(
        today=datetime.date(2026, 8, 4), root=tmp_path, folders=("LLC",),
    )
    assert facts[0].status == fa.STATUS_UNKNOWN


def test_genuinely_absent_folder_is_still_missing(tmp_path):
    """The OSError fix must not turn a real absence into 'unknown'."""
    facts = fa.check_bank_statements(
        today=datetime.date(2026, 8, 4), root=tmp_path, folders=("LLC",),
    )
    assert facts[0].status == fa.STATUS_MISSING


# ═════════════════════════════════════════════════════════════════════════════
# The stat cap truncated by NAME order, reporting a current folder as STALE
# ═════════════════════════════════════════════════════════════════════════════

def test_stat_cap_keeps_the_newest_named_files(tmp_path):
    """Path.glob returns name order, so capping the raw list kept the OLDEST files.

    A folder of chronologically-named statements past the cap therefore reported a
    perfectly current set as STALE -- a false alarm that grows with maintenance.
    """
    folder = tmp_path / "LLC"
    folder.mkdir(parents=True)
    import os

    today = datetime.date(2026, 8, 4)
    for i in range(fa.MAX_FILES_STATTED + 11):
        year, month = 2019 + i // 12, (i % 12) + 1
        path = folder / f"LLC Main {year}-{month:02d}.pdf"
        path.write_text("x", encoding="utf-8")
        age = (fa.MAX_FILES_STATTED + 10 - i) * 30
        when = (datetime.datetime.combine(today, datetime.time(12, 0))
                - datetime.timedelta(days=age)).timestamp()
        os.utime(path, (when, when))

    facts = fa.check_bank_statements(today=today, root=tmp_path, folders=("LLC",))
    assert facts[0].status == fa.STATUS_OK, facts[0].text
    assert "only the" in facts[0].text and "newest-named" in facts[0].text


# ═════════════════════════════════════════════════════════════════════════════
# Threshold: base == 0 is the most extreme relative move, not an unflaggable one
# ═════════════════════════════════════════════════════════════════════════════

def test_crosses_flags_a_material_move_off_a_zero_base():
    assert fc._crosses(4_900.0, 0.0, fc.CASH_DELTA_ABS, fc.CASH_DELTA_PCT)
    assert fc._crosses(4_900.0, None, fc.CASH_DELTA_ABS, fc.CASH_DELTA_PCT)


def test_crosses_zero_base_still_respects_the_floor():
    assert not fc._crosses(100.0, 0.0, fc.CASH_DELTA_ABS, fc.CASH_DELTA_PCT)


def test_zero_base_and_epsilon_base_now_agree():
    """Two identically-rendered rows must not behave oppositely."""
    a = fc._crosses(4_900.0, 0.0, fc.CASH_DELTA_ABS, fc.CASH_DELTA_PCT)
    b = fc._crosses(4_900.0, 0.01, fc.CASH_DELTA_ABS, fc.CASH_DELTA_PCT)
    assert a == b is True


# ═════════════════════════════════════════════════════════════════════════════
# Aging extractor: clean books are not a broken read; bucket labels must align
# ═════════════════════════════════════════════════════════════════════════════

def test_blank_grand_total_with_structure_present_is_zero_not_none():
    """$0 AR and $0 AP are CLEAN BOOKS, not "unavailable — no aging totals"."""
    report = {
        "Columns": {"Column": [{"ColTitle": ""}, {"ColTitle": "Current"}, {"ColTitle": "Total"}]},
        "Rows": {"Row": [{"type": "Section", "Summary": {"ColData": [
            {"value": "TOTAL"}, {"value": ""}, {"value": ""},
        ]}}]},
    }
    got = fc.extract_aging(report)
    assert got is not None and got["total"] == pytest.approx(0.0)


def test_no_summary_row_at_all_is_still_none():
    assert fc.extract_aging({"Rows": {"Row": []}}) is None


def test_misaligned_columns_omit_the_bucket_callout_rather_than_mislabel():
    """A short summary row once labelled the NEWEST bucket ('Current') as the aged tail."""
    report = {
        "Columns": {"Column": [
            {"ColTitle": ""}, {"ColTitle": "Current"}, {"ColTitle": "31 - 60"},
            {"ColTitle": "91 and over"}, {"ColTitle": "Total"},
        ]},
        "Rows": {"Row": [{"type": "Section", "Summary": {"ColData": [
            {"value": "TOTAL"}, {"value": "500.00"}, {"value": "900.00"},
        ]}}]},
    }
    got = fc.extract_aging(report)
    assert got["total"] == pytest.approx(900.0)
    assert got["oldest_label"] == ""
    assert got["oldest_amount"] is None


# ═════════════════════════════════════════════════════════════════════════════
# Coverage counted per METRIC, and unmapped entities named
# ═════════════════════════════════════════════════════════════════════════════

def test_aging_coverage_is_per_metric_not_per_entity():
    """Every AP read failing must not render as full coverage."""
    entities = ["F3E", "OSNGW", "LEX"]
    section, _ = fc.build_aging_section(
        entities,
        _sources(
            ar_aging=lambda e: _aging(50_000.0),
            ap_aging=lambda e: (_ for _ in ()).throw(RuntimeError("AP endpoint down")),
        ),
        None, today=MONDAY,
    )
    body = "\n".join(section.lines)
    assert "AR read for 3 of 3" in body
    assert "AP read for 0 of 3" in body
    assert section.covered == 0 and section.expected == 3


def test_pnl_coverage_is_per_metric():
    def pnl(entity, start, end):
        return {"Rows": {"Row": [{
            "type": "Section",
            "Header": {"ColData": [{"value": "Income"}]},
            "Summary": {"ColData": [{"value": "Total Income"}, {"value": "1000.00"}]},
        }]}}

    section, _ = fc.build_pnl_section(["F3E", "LEX"], _sources(profit_loss=pnl), today=MONDAY)
    body = "\n".join(section.lines)
    assert "Revenue read for 2 of 2" in body
    assert "expenses read for 0 of 2" in body


def test_unmapped_entity_is_named_and_counted():
    """A newly provisioned realm must not vanish from the section AND its denominator."""
    section, _ = fc.build_cash_section(["F3E", "OSNXX"], _sources(), today=MONDAY)
    body = "\n".join(section.lines)
    assert "NOT cross-checked — no cash-sheet mapping" in body
    assert "Cross-checked 1 of 2" in body


def test_excluded_entity_is_not_reported_as_unmapped():
    section, _ = fc.build_cash_section(["F3E", "HRLLC"], _sources(), today=MONDAY)
    body = "\n".join(section.lines)
    assert "NOT cross-checked" not in body
    assert "Excluded" in body and "HR LLC" in body


# ═════════════════════════════════════════════════════════════════════════════
# HR LLC (personal) must be excluded from EVERY section, not just the cash check
# ═════════════════════════════════════════════════════════════════════════════

def test_hrllc_is_excluded_from_aging_and_pnl_too():
    """The exclusion reason is SENSITIVITY, so it cannot be scoped to one section."""
    seen: list[str] = []

    def ar(entity):
        seen.append(entity)
        return {"total": 7_400.0, "oldest_label": "91 and over", "oldest_amount": 7_400.0}

    pack = fc.build_pack(
        _sources(provisioned_entities=lambda: ["F3E", "HRLLC"], ar_aging=ar),
        today=MONDAY, persist_snapshot=False,
    )
    assert "HRLLC" not in seen, "HR LLC's AR was fetched despite the exclusion"
    rendered = pack.render()
    aging = next(s for s in pack.sections if s.key == "aging")
    assert "HR LLC" not in "\n".join(aging.lines)
    # The exclusion is STATED, not silent.
    assert "HR LLC" in rendered and "personal expense tracking" in rendered


# ═════════════════════════════════════════════════════════════════════════════
# Flag accounting: a restatement is not a distinct finding
# ═════════════════════════════════════════════════════════════════════════════

def test_close_prep_restatement_does_not_inflate_the_flag_total():
    cash = fc.Section(key="cash", title="c", available=True, lines=["x"], flags=3,
                      covered=2, expected=2)
    prep = fc.build_close_prep_section(_sources(), cash_section=cash, today=MONDAY)
    restatement = [ln for ln in prep.lines if "unreconciled-looking" in ln]
    assert restatement, "the restatement line must still be surfaced"
    # It renders, but adds nothing to the count -- 3 findings are not 4.
    assert prep.flags == 1, "only the missing-adherence-facts flag should count here"


# ═════════════════════════════════════════════════════════════════════════════
# Label-drift diagnosis must survive a minority of transient read failures
# ═════════════════════════════════════════════════════════════════════════════

def test_label_drift_diagnosis_survives_one_transient_failure():
    """The actionable "row labels renamed" message must not be replaced by a
    generic "unreadable" because one entity errored."""
    calls = {"n": 0}

    def cash(_entity):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient sheets 503")
        return {"closing": None, "is_actual": True,
                "week_label": "Week of 7/27/2026", "stale": False, "age_days": 1}

    section, _ = fc.build_cash_section(
        ["F3E", "OSNGW", "OSNGF", "OSNVV"], _sources(cash_closing=cash), today=MONDAY,
    )
    assert section.available is False
    assert "renamed" in (section.stub_reason or "")


def test_mostly_none_mixed_state_still_warns_about_drift():
    """A mixed state renders, so the drift hint belongs on the AVAILABLE path too."""
    def cash(entity):
        value = 100_000.0 if entity == "F3E" else None
        return {"closing": value, "is_actual": True,
                "week_label": "Week of 7/27/2026", "stale": False, "age_days": 1}

    section, _ = fc.build_cash_section(
        ["F3E", "OSNGW", "OSNGF", "OSNVV"], _sources(cash_closing=cash), today=MONDAY,
    )
    assert section.available is True
    assert "suspect row-label drift" in "\n".join(section.lines)


# ═════════════════════════════════════════════════════════════════════════════
# WoW baseline must be dated; a per-entity gap must not read as "no change"
# ═════════════════════════════════════════════════════════════════════════════

def test_aging_names_the_comparison_baseline_date():
    prior = {"_snapshot_date": "2026-07-27", "aging": {"F3E": {"ar": 1.0, "ap": 1.0}}}
    section, _ = fc.build_aging_section(["F3E"], _sources(), prior, today=MONDAY)
    assert "Compared against the 2026-07-27 snapshot." in "\n".join(section.lines)


def test_aging_warns_when_the_baseline_is_not_a_week_old():
    prior = {"_snapshot_date": "2026-06-29", "aging": {"F3E": {"ar": 1.0, "ap": 1.0}}}
    section, _ = fc.build_aging_section(["F3E"], _sources(), prior, today=MONDAY)
    body = "\n".join(section.lines)
    assert "not one week" in body and "35d ago" in body


def test_aging_marks_an_entity_missing_from_the_prior_snapshot():
    prior = {"_snapshot_date": "2026-07-27", "aging": {"OSNGW": {"ar": 1.0, "ap": 1.0}}}
    section, _ = fc.build_aging_section(
        ["F3E"],
        _sources(ar_aging=lambda e: _aging(50_000.0)),
        prior, today=MONDAY,
    )
    assert "(no prior)" in "\n".join(section.lines)


def test_aging_states_its_own_as_of_date():
    """Aging is as-of the run date while the cash section is as-of the sheet week."""
    section, _ = fc.build_aging_section(["F3E"], _sources(), None, today=MONDAY)
    assert "Aging is as of today (2026-08-03)." in "\n".join(section.lines)


def test_flagged_metric_is_named_on_the_line():
    prior = {"_snapshot_date": "2026-07-27", "aging": {"F3E": {"ar": 1_000.0, "ap": 20_000.0}}}
    section, _ = fc.build_aging_section(
        ["F3E"],
        _sources(
            ar_aging=lambda e: _aging(50_000.0),
            ap_aging=lambda e: _aging(20_100.0),
        ),
        prior, today=MONDAY,
    )
    body = "\n".join(section.lines)
    assert "[AR moved materially]" in body


# ═════════════════════════════════════════════════════════════════════════════
# Adherence-facts freshness on a WEEKLY cadence
# ═════════════════════════════════════════════════════════════════════════════

def test_adherence_max_age_is_tight_enough_to_catch_a_missed_week():
    assert fc.ADHERENCE_MAX_AGE_DAYS < 7


def test_week_old_adherence_facts_are_flagged_stale():
    payload = {"generated_date": "2026-07-27", "facts": ["cash_sheet: fresh"],
               "facts_status": [fa.STATUS_OK]}
    cash = fc.Section(key="cash", title="c", available=True, lines=["x"],
                      covered=1, expected=1)
    prep = fc.build_close_prep_section(
        _sources(adherence_facts=lambda: payload), cash_section=cash, today=MONDAY,
    )
    body = "\n".join(prep.lines)
    assert "STALE" in body and "did not run this morning" in body
    assert prep.flags >= 1


def test_undated_adherence_facts_get_an_explicit_qualifier():
    """Previously BOTH date branches were skipped -- no as-of line rendered at all."""
    for payload in (
        {"facts": ["cash_sheet: fresh"]},
        {"generated_date": "not-a-date", "facts": ["cash_sheet: fresh"]},
    ):
        cash = fc.Section(key="cash", title="c", available=True, lines=["x"],
                          covered=1, expected=1)
        prep = fc.build_close_prep_section(
            _sources(adherence_facts=lambda: payload), cash_section=cash, today=MONDAY,
        )
        body = "\n".join(prep.lines)
        assert "no readable generation date" in body
        assert prep.flags >= 1


def test_empty_adherence_facts_list_is_called_out():
    payload = {"generated_date": "2026-08-03", "facts": []}
    cash = fc.Section(key="cash", title="c", available=True, lines=["x"],
                      covered=1, expected=1)
    prep = fc.build_close_prep_section(
        _sources(adherence_facts=lambda: payload), cash_section=cash, today=MONDAY,
    )
    assert "nothing was actually checked" in "\n".join(prep.lines)


def test_adherence_lines_are_capped():
    payload = {
        "generated_date": "2026-08-03",
        "facts": [f"check {i}: ok" for i in range(200)],
        "facts_status": [fa.STATUS_OK] * 200,
    }
    cash = fc.Section(key="cash", title="c", available=True, lines=["x"],
                      covered=1, expected=1)
    prep = fc.build_close_prep_section(
        _sources(adherence_facts=lambda: payload), cash_section=cash, today=MONDAY,
    )
    assert "further adherence line(s) not shown" in "\n".join(prep.lines)
    assert len(prep.lines) < 60


# ═════════════════════════════════════════════════════════════════════════════
# Slack control-token injection from externally-authored strings
# ═════════════════════════════════════════════════════════════════════════════

def test_scrub_external_strips_slack_control_syntax():
    assert fc._scrub_external("<https://evil.example/pay|Pay now>") == (
        "https://evil.example/pay|Pay now"
    )
    assert "<" not in fc._scrub_external("<!channel> URGENT")
    assert fc._scrub_external("a\nb\nc") == "a b c"
    assert len(fc._scrub_external("x" * 500)) == 120


def test_renewal_names_cannot_inject_a_link_or_a_channel_ping():
    """sanitize_text PRESERVES <...> tokens by design, so a crafted YAML name would
    otherwise render a clickable payment link in a finance channel, signed by Cora."""
    from cora.slack_egress import sanitize_text

    items = [
        {"name": "<https://evil.example/pay|Pay now>", "next_due": "2026-08-05"},
        {"name": "<!channel> WIRE NOW", "entity": "<!here>", "next_due": "2026-08-06"},
    ]
    section = fc.build_renewal_section(_sources(renewals=lambda: items), today=MONDAY)
    body = sanitize_text("\n".join(section.lines))
    assert "<" not in body and ">" not in body
    assert "!channel" in body           # neutralized, not silently dropped


def test_renewal_newline_cannot_break_the_line_structure():
    items = [{"name": "Legit\nINJECTED SECOND LINE", "next_due": "2026-08-05"}]
    section = fc.build_renewal_section(_sources(renewals=lambda: items), today=MONDAY)
    hits = [ln for ln in section.lines if "INJECTED" in ln]
    assert len(hits) == 1 and hits[0].startswith("•")


def test_adherence_facts_lines_are_scrubbed_before_rendering():
    payload = {
        "generated_date": "2026-08-03",
        "facts": ["cash_sheet: <!channel> see <https://x|here>"],
        "facts_status": [fa.STATUS_OK],
    }
    cash = fc.Section(key="cash", title="c", available=True, lines=["x"],
                      covered=1, expected=1)
    prep = fc.build_close_prep_section(
        _sources(adherence_facts=lambda: payload), cash_section=cash, today=MONDAY,
    )
    body = "\n".join(prep.lines)
    assert "<" not in body and ">" not in body


def test_unconfirmed_renewal_entries_are_labelled():
    """A seeded placeholder date must not masquerade as a verified renewal."""
    items = [{"name": "Meta Verified", "next_due": "2026-08-05", "confirmed": False}]
    section = fc.build_renewal_section(_sources(renewals=lambda: items), today=MONDAY)
    body = "\n".join(section.lines)
    assert "UNCONFIRMED date/amount" in body
    assert "partial list, not full subscription coverage" in body


def test_shipped_renewal_seeds_are_marked_unconfirmed():
    """The committed seeds are unverified, so they must say so in the pack."""
    items = fc.load_renewals()
    assert items and all(i.get("confirmed") is False for i in items)


# ═════════════════════════════════════════════════════════════════════════════
# Narration: the no-invented-figure rule is CODE-enforced, not prompt-enforced
# ═════════════════════════════════════════════════════════════════════════════

def _fake_anthropic(reply: str):
    module = types.ModuleType("anthropic")

    class _Msg:
        content = [types.SimpleNamespace(text=reply)]
        usage = types.SimpleNamespace(
            input_tokens=10, output_tokens=5,
            cache_creation_input_tokens=0, cache_read_input_tokens=0,
        )

    class _Client:
        def __init__(self, **_kw):
            self.messages = self

        def create(self, **_kw):
            return _Msg()

    module.Anthropic = _Client
    return module


def _pack_with(line: str) -> fc.ClosePack:
    return fc.ClosePack(generated_at="2026-08-03", sections=[
        fc.Section(key="cash", title="Cash", lines=[line]),
    ])


def test_narration_with_an_invented_figure_is_dropped(monkeypatch):
    monkeypatch.setenv("FINANCE_CLOSE_NARRATE", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setitem(sys.modules, "anthropic",
                        _fake_anthropic("Portfolio cash totals $2,400,000 this week."))
    assert fc.narrate(_pack_with("• F3 Energy: delta +$40,000")) is None


def test_narration_restating_only_real_figures_survives(monkeypatch):
    monkeypatch.setenv("FINANCE_CLOSE_NARRATE", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setitem(sys.modules, "anthropic",
                        _fake_anthropic("Look at F3 Energy: delta +$40,000."))
    got = fc.narrate(_pack_with("• F3 Energy: delta +$40,000"))
    assert got == "Look at F3 Energy: delta +$40,000."


def test_narration_with_no_figures_at_all_survives(monkeypatch):
    monkeypatch.setenv("FINANCE_CLOSE_NARRATE", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setitem(sys.modules, "anthropic",
                        _fake_anthropic("Start with the cash section."))
    assert fc.narrate(_pack_with("• F3 Energy: delta +$40,000")) == (
        "Start with the cash section."
    )


def test_narration_is_labelled_as_a_restatement(monkeypatch, tmp_path, capsys):
    """It is prefixed ABOVE the "every figure is a direct source read" line, so it
    must not visually inherit that guarantee unlabelled."""
    script = _script()
    from cora import finance_close

    monkeypatch.setattr(finance_close, "build_pack", lambda **_k: _pack_with("• x"))
    monkeypatch.setattr(finance_close, "narrate", lambda _p: "Check cash first.")
    monkeypatch.setattr(script, "_DEDUP_PATH", tmp_path / "sent.json")
    monkeypatch.setattr(sys, "argv", ["x", "--dry-run"])
    script.main()
    out = capsys.readouterr().out
    assert "Summary (restatement of the facts below):" in out


# ═════════════════════════════════════════════════════════════════════════════
# Snapshot hygiene: an all-stubbed run must not become next week's baseline
# ═════════════════════════════════════════════════════════════════════════════

def test_all_stubbed_run_writes_no_snapshot(tmp_path):
    def boom(*_a, **_kw):
        raise RuntimeError("everything down")

    fc.build_pack(
        _sources(cash_closing=boom, ar_aging=boom, ap_aging=boom, profit_loss=boom),
        today=MONDAY, snapshot_dir=tmp_path,
    )
    assert list(tmp_path.glob("*.json")) == [], (
        "an empty snapshot would become next week's WoW baseline and mislabel the "
        "result 'First run'"
    )


def test_a_run_with_data_still_writes_a_snapshot(tmp_path):
    fc.build_pack(_sources(), today=MONDAY, snapshot_dir=tmp_path)
    assert (tmp_path / "2026-08-03.json").exists()


# ═════════════════════════════════════════════════════════════════════════════
# The pre-flight gate must not be the thing that breaks
# ═════════════════════════════════════════════════════════════════════════════

def test_pack_render_is_encodable_on_a_cp1252_console():
    """--dry-run died with UnicodeEncodeError on real data (any aged-tail line),
    and it is the ONLY gate before the first live post to a finance channel."""
    pack = fc.build_pack(
        _sources(
            ar_aging=lambda e: _aging(50_000.0, 8_017.92),
            ap_aging=lambda e: _aging(20_000.0, 1_000.0),
        ),
        today=MONDAY, persist_snapshot=False,
    )
    rendered = pack.render()
    assert "aged tail" in rendered
    rendered.encode("cp1252")     # must not raise


def test_scripts_harden_stdout_for_cp1252_consoles():
    for name in ("run_finance_close_pack.py", "run_finance_adherence_check.py"):
        text = (_REPO / "scripts" / name).read_text(encoding="utf-8")
        assert "reconfigure(encoding=\"utf-8\"" in text, name


# ═════════════════════════════════════════════════════════════════════════════
# Delivery safety
# ═════════════════════════════════════════════════════════════════════════════

def test_dm_target_must_be_a_single_user_id():
    """A comma would make conversations.open create an MPIM -- a wider audience."""
    script = _script()

    class _C:
        def conversations_open(self, users):
            raise AssertionError("must refuse before opening")

    for bad in ("U0B3AEJCYGP,U0B2RM2JYJ1", "C0B3V5SDNAG", "", "nonsense"):
        with pytest.raises(script.DeliveryTargetError):
            script.dm_user(_C(), bad, "figures")


def test_dm_target_accepts_the_real_id():
    script = _script()

    class _C:
        def __init__(self):
            self.sent = []

        def conversations_open(self, users):
            return {"channel": {"id": "D1"}}

        def chat_postMessage(self, channel, text, **_k):
            self.sent.append(channel)

    client = _C()
    assert script.dm_user(client, script.JUSTIN_SLACK_ID, "x") is True
    assert client.sent == ["D1"]


def test_scoped_run_does_not_consume_the_weekly_slot(monkeypatch, tmp_path):
    """--entities delivers a PARTIAL pack, so it must not suppress the real run."""
    script = _script()
    from cora import finance_close

    class _C:
        def __init__(self):
            self.posts = []

        def conversations_open(self, users):
            return {"channel": {"id": "D1"}}

        def chat_postMessage(self, channel, text, **_k):
            self.posts.append((channel, text))
            return {"ok": True}

    client = _C()
    monkeypatch.setattr(finance_close, "build_pack", lambda **_k: _pack_with("• x"))
    monkeypatch.setattr(finance_close, "narrate", lambda _p: None)
    monkeypatch.setattr(script, "_DEDUP_PATH", tmp_path / "sent.json")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setitem(sys.modules, "slack_sdk", types.ModuleType("slack_sdk"))
    sys.modules["slack_sdk"].WebClient = lambda token: client
    monkeypatch.setattr(sys, "argv", ["x", "--entities", "F3E"])

    assert script.main() == 0
    assert script._sent_targets(script._iso_week()) == set()
    assert any("SCOPED RUN" in t for _c, t in client.posts)


def test_build_failure_raises_a_loud_metadata_only_alert(monkeypatch, tmp_path):
    """A Monday crash was complete silence, and silence reads as "no problems"."""
    script = _script()
    from cora import finance_close
    from cora.channel_content_guard import _has_money_figure

    sent: list[tuple[str, str]] = []

    class _C:
        def chat_postMessage(self, channel, text, **_k):
            sent.append((channel, text))
            return {"ok": True}

    def boom(**_kw):
        raise RuntimeError("QBO token store corrupt")

    monkeypatch.setattr(finance_close, "build_pack", boom)
    monkeypatch.setattr(script, "_DEDUP_PATH", tmp_path / "sent.json")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setitem(sys.modules, "slack_sdk", types.ModuleType("slack_sdk"))
    sys.modules["slack_sdk"].WebClient = lambda token: _C()
    monkeypatch.setattr(sys, "argv", ["run_finance_close_pack.py"])

    assert script.main() == 1
    assert sent and "FAILED to build" in sent[0][1]
    assert sent[0][0] == "hjrg-leadership"
    assert not _has_money_figure(sent[0][1])
    assert "RuntimeError" in sent[0][1]


def test_build_failure_in_dry_run_posts_nothing(monkeypatch, tmp_path):
    script = _script()
    from cora import finance_close

    def boom(**_kw):
        raise RuntimeError("nope")

    called = {"alert": False}
    monkeypatch.setattr(finance_close, "build_pack", boom)
    monkeypatch.setattr(script, "_alert_build_failure",
                        lambda _n: called.update(alert=True))
    monkeypatch.setattr(script, "_DEDUP_PATH", tmp_path / "sent.json")
    monkeypatch.setattr(sys, "argv", ["x", "--dry-run"])
    assert script.main() == 1
    assert called["alert"] is False


# ═════════════════════════════════════════════════════════════════════════════
# Contract between the two modules stays intact after all of the above
# ═════════════════════════════════════════════════════════════════════════════

def test_live_adherence_payload_flows_into_the_close_pack(tmp_path):
    """End-to-end on the real payload shape, including the roll-up."""
    sheet = tmp_path / "live-sheets" / fa.CASH_SHEET_PATH.name
    sheet.parent.mkdir(parents=True)
    sheet.write_text("x", encoding="utf-8")
    for name in fa.BANK_ENTITY_FOLDERS:
        folder = tmp_path / "bank-statements" / name
        folder.mkdir(parents=True)
        (folder / "s.pdf").write_text("x", encoding="utf-8")

    report = fa.build_report(today=datetime.date(2026, 8, 4), accounting_root=tmp_path)
    payload = report.to_json()
    path = tmp_path / "facts.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = fc.load_adherence_facts(path)
    assert loaded is not None
    cash = fc.Section(key="cash", title="c", available=True, lines=["x"],
                      covered=1, expected=1)
    prep = fc.build_close_prep_section(
        _sources(adherence_facts=lambda: loaded), cash_section=cash,
        today=datetime.date(2026, 8, 4),
    )
    body = "\n".join(prep.lines)
    assert "monthly_filing 2026-07" in body     # genuinely missing in the fixture
    assert "lane_retired" in body
    assert prep.flags >= 1
