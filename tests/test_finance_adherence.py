"""Tests for the A1-A3 finance SOP adherence checks.

THE ACCEPTANCE CRITERION THIS FILE OWNS
---------------------------------------
"A deliberately-renamed sheet/folder produces an honest missing/lane_retired fact,
never a silent blank" (the 2026-06-04 Standing-ACTUALS label-fragility doctrine,
applied to paths).

Two failure modes get equal weight:
  * a renamed/moved path must read as MISSING, not as a silent pass; and
  * a vanished G: mount must read as UNKNOWN, never as MISSING -- reporting an
    infrastructure blip as a missing month-end filing is a false alarm about
    someone else's work.
"""

from __future__ import annotations

import datetime
import json
import sys

import pytest

from cora import drive_io
from cora import finance_adherence as fa


TODAY = datetime.date(2026, 8, 4)


@pytest.fixture(autouse=True)
def _clear_breaker():
    """drive_io's circuit breaker is process-global; isolate every test."""
    drive_io.reset_state_for_tests()
    yield
    drive_io.reset_state_for_tests()


def _touch(path, *, days_old: int = 0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    if days_old:
        import os
        when = (datetime.datetime.combine(TODAY, datetime.time(12, 0))
                - datetime.timedelta(days=days_old)).timestamp()
        os.utime(path, (when, when))
    return path


# ── real-structure pins ──────────────────────────────────────────────────────

def test_cash_sheet_path_is_the_real_live_sheet():
    """SOP rev 4: the `_LIVE`-named set was never migrated -- do not encode it."""
    assert fa.CASH_SHEET_PATH.name == (
        "HJR-Lexco_ENTITIES_Weekly Cash Flow Requirements_Standing ACTUALS.gsheet"
    )
    assert fa.CASH_SHEET_PATH.parent.name == "live-sheets"
    assert "_LIVE" not in str(fa.CASH_SHEET_PATH)


def test_bank_entity_folders_match_the_real_structure():
    """Enumerated on the mount 2026-08-04. A rename must read MISSING, not vanish."""
    assert len(fa.BANK_ENTITY_FOLDERS) == 13
    for name in ("Big D Media", "HJR GS", "HJR Properties", "LBHS", "LLC", "LTS",
                 "Lex Corp", "Maryvale ASA", "OSN Core 4", "OSN GF", "OSN GMK",
                 "OSN GW", "OSN VVP"):
        assert name in fa.BANK_ENTITY_FOLDERS


def test_accounting_root_is_under_the_founder_os_mount():
    parts = fa.ACCOUNTING_ROOT.parts
    assert "HJR-Founder-OS" in parts and "01-HJR-Global" in parts
    assert fa.ACCOUNTING_ROOT.name == "accounting"


# ── A1: cash-sheet freshness ─────────────────────────────────────────────────

def test_cash_sheet_fresh(tmp_path):
    sheet = _touch(tmp_path / "live-sheets" / fa.CASH_SHEET_PATH.name, days_old=2)
    fact = fa.check_cash_sheet(today=TODAY, path=sheet)
    assert fact.status == fa.STATUS_OK
    assert "fresh" in fact.text and "2d ago" in fact.text
    assert fact.is_problem is False


def test_cash_sheet_stale(tmp_path):
    sheet = _touch(tmp_path / "live-sheets" / fa.CASH_SHEET_PATH.name, days_old=20)
    fact = fa.check_cash_sheet(today=TODAY, path=sheet)
    assert fact.status == fa.STATUS_STALE
    assert "STALE" in fact.text and "20d ago" in fact.text
    assert fact.is_problem is True


def test_cash_sheet_boundary_is_inclusive(tmp_path):
    sheet = _touch(tmp_path / "live-sheets" / fa.CASH_SHEET_PATH.name, days_old=7)
    assert fa.check_cash_sheet(today=TODAY, path=sheet).status == fa.STATUS_OK


def test_cash_sheet_renamed_is_missing_not_blank(tmp_path):
    """ACCEPTANCE: a renamed sheet yields an explicit MISSING fact naming the path."""
    _touch(tmp_path / "live-sheets" / "Standing ACTUALS RENAMED.gsheet")
    fact = fa.check_cash_sheet(
        today=TODAY, path=tmp_path / "live-sheets" / fa.CASH_SHEET_PATH.name,
    )
    assert fact.status == fa.STATUS_MISSING
    assert "MISSING" in fact.text
    # Names WHAT it looked for without echoing the filename: the facts block is
    # KB-ingested and gsheets_financials locks a source-opaque contract.
    assert "Standing-ACTUALS cash sheet" in fact.text
    assert "renamed" in fact.text
    assert fa.CASH_SHEET_PATH.name not in fact.text
    assert fact.text.strip()                        # never a blank


def test_cash_sheet_mount_gone_is_unknown_not_missing(tmp_path, monkeypatch):
    """A mount blip must never be reported as a missing/stale finance artifact."""
    def boom(*_a, **_kw):
        raise drive_io.DriveUnavailable("G: gone")

    monkeypatch.setattr(drive_io, "stat_info", boom)
    fact = fa.check_cash_sheet(today=TODAY, path=tmp_path / "x.gsheet")
    assert fact.status == fa.STATUS_UNKNOWN
    assert fact.is_problem is False                 # not a finance finding
    assert "mount was unreachable" in fact.text


def test_cash_sheet_other_oserror_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(drive_io, "stat_info",
                        lambda *a, **k: (_ for _ in ()).throw(PermissionError("denied")))
    fact = fa.check_cash_sheet(today=TODAY, path=tmp_path / "x.gsheet")
    assert fact.status == fa.STATUS_UNKNOWN


# ── A2: Clover lane retired ──────────────────────────────────────────────────

def test_clover_fact_is_static_retired():
    """SOP rev 4 struck the export -- never alarm, never count per-day misses."""
    fact = fa.clover_fact()
    assert fact.status == fa.STATUS_RETIRED
    assert "lane_retired" in fact.text
    assert "2026-06-06" in fact.text and "SOP rev 4" in fact.text
    assert fact.is_problem is False


def test_clover_fact_never_says_lane_absent():
    text = fa.clover_fact().text.lower()
    for forbidden in ("lane_absent", "missing", "overdue", "days missed"):
        assert forbidden not in text


def test_clover_fact_takes_no_arguments():
    """It reads nothing -- a filesystem check here would resurrect the nag."""
    import inspect
    assert list(inspect.signature(fa.clover_fact).parameters) == []


# ── A3a: monthly filing presence ─────────────────────────────────────────────

def test_target_filing_month_before_the_15th_looks_back():
    """Early-month runs must not nag about a folder nobody was to fill yet."""
    assert fa.target_filing_month(datetime.date(2026, 8, 4)) == datetime.date(2026, 7, 1)
    assert fa.target_filing_month(datetime.date(2026, 8, 15)) == datetime.date(2026, 7, 1)


def test_target_filing_month_after_the_15th_is_current():
    assert fa.target_filing_month(datetime.date(2026, 8, 16)) == datetime.date(2026, 8, 1)


def test_target_filing_month_crosses_year_boundary():
    assert fa.target_filing_month(datetime.date(2026, 1, 5)) == datetime.date(2025, 12, 1)


def test_monthly_filing_present(tmp_path):
    _touch(tmp_path / "2026-07" / "2026-06_bdm_pl.xlsx")
    fact = fa.check_monthly_filing(today=TODAY, root=tmp_path)
    assert fact.status == fa.STATUS_OK
    assert "filed" in fact.text and "2026-07" in fact.key


def test_monthly_filing_folder_absent_is_missing(tmp_path):
    fact = fa.check_monthly_filing(today=TODAY, root=tmp_path)
    assert fact.status == fa.STATUS_MISSING
    assert "MISSING" in fact.text and "2026-07" in fact.text


def test_monthly_filing_desktop_ini_only_is_missing(tmp_path):
    """The exact live state of monthly-reports/2026-07 on 2026-08-04.

    A folder-exists check alone would pass it, which is how a never-filed month
    reads as filed.
    """
    _touch(tmp_path / "2026-07" / "desktop.ini")
    fact = fa.check_monthly_filing(today=TODAY, root=tmp_path)
    assert fact.status == fa.STATUS_MISSING
    assert "no content" in fact.text
    assert "The folder being present is not the filing" in fact.text


def test_monthly_filing_ignores_all_os_metadata_names(tmp_path):
    for name in ("desktop.ini", "Thumbs.db", ".DS_Store"):
        _touch(tmp_path / "2026-07" / name)
    assert fa.check_monthly_filing(today=TODAY, root=tmp_path).status == fa.STATUS_MISSING


def test_monthly_filing_mount_gone_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(drive_io, "glob",
                        lambda *a, **k: (_ for _ in ()).throw(drive_io.DriveUnavailable("gone")))
    fact = fa.check_monthly_filing(today=TODAY, root=tmp_path)
    assert fact.status == fa.STATUS_UNKNOWN
    assert fact.is_problem is False


# ── A3b: bank statement freshness ────────────────────────────────────────────

def test_bank_statements_current(tmp_path):
    for name in ("LLC", "LBHS"):
        _touch(tmp_path / name / f"{name} Main July 2026.pdf", days_old=10)
    facts = fa.check_bank_statements(
        today=TODAY, root=tmp_path, folders=("LLC", "LBHS"),
    )
    assert len(facts) == 2
    assert all(f.status == fa.STATUS_OK for f in facts)
    assert "10d old" in facts[0].text


def test_bank_statements_stale(tmp_path):
    _touch(tmp_path / "LLC" / "LLC Main Feb 2026.pdf", days_old=120)
    facts = fa.check_bank_statements(today=TODAY, root=tmp_path, folders=("LLC",))
    assert facts[0].status == fa.STATUS_STALE
    assert "120d old" in facts[0].text


def test_bank_statements_uses_newest_file_not_oldest(tmp_path):
    _touch(tmp_path / "LLC" / "old.pdf", days_old=200)
    _touch(tmp_path / "LLC" / "new.pdf", days_old=5)
    facts = fa.check_bank_statements(today=TODAY, root=tmp_path, folders=("LLC",))
    assert facts[0].status == fa.STATUS_OK
    assert "5d old" in facts[0].text


def test_bank_statements_renamed_folder_is_missing(tmp_path):
    """ACCEPTANCE: a renamed entity folder reads MISSING, not silently absent."""
    _touch(tmp_path / "LLC RENAMED" / "stmt.pdf", days_old=3)
    facts = fa.check_bank_statements(today=TODAY, root=tmp_path, folders=("LLC",))
    assert facts[0].status == fa.STATUS_MISSING
    assert "no `LLC/` folder" in facts[0].text
    assert "renamed or moved" in facts[0].text


def test_bank_statements_empty_folder_is_missing_content(tmp_path):
    (tmp_path / "LLC").mkdir(parents=True)
    _touch(tmp_path / "LLC" / "desktop.ini")
    facts = fa.check_bank_statements(today=TODAY, root=tmp_path, folders=("LLC",))
    assert facts[0].status == fa.STATUS_MISSING
    assert "no content" in facts[0].text


def test_bank_statements_one_fact_per_folder_always(tmp_path):
    """No folder may silently vanish from the block."""
    _touch(tmp_path / "LLC" / "a.pdf", days_old=1)
    facts = fa.check_bank_statements(today=TODAY, root=tmp_path)
    assert len(facts) == len(fa.BANK_ENTITY_FOLDERS)
    keys = {f.key for f in facts}
    for name in fa.BANK_ENTITY_FOLDERS:
        assert f"bank_statements {name}" in keys


def test_bank_statements_mount_gone_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(drive_io, "glob",
                        lambda *a, **k: (_ for _ in ()).throw(drive_io.DriveUnavailable("gone")))
    facts = fa.check_bank_statements(today=TODAY, root=tmp_path, folders=("LLC",))
    assert facts[0].status == fa.STATUS_UNKNOWN
    assert facts[0].is_problem is False


def test_bank_statements_mount_dies_midway_is_unknown(tmp_path, monkeypatch):
    """A mount loss between glob and stat must not degrade to a false MISSING."""
    _touch(tmp_path / "LLC" / "a.pdf")
    monkeypatch.setattr(drive_io, "stat_info",
                        lambda *a, **k: (_ for _ in ()).throw(drive_io.DriveUnavailable("gone")))
    facts = fa.check_bank_statements(today=TODAY, root=tmp_path, folders=("LLC",))
    assert facts[0].status == fa.STATUS_UNKNOWN


def test_bank_statements_stat_cap_is_bounded(tmp_path):
    for i in range(fa.MAX_FILES_STATTED + 20):
        _touch(tmp_path / "LLC" / f"f{i}.pdf", days_old=3)
    facts = fa.check_bank_statements(today=TODAY, root=tmp_path, folders=("LLC",))
    assert facts[0].status == fa.STATUS_OK
    assert f"{fa.MAX_FILES_STATTED} file(s)" in facts[0].text


# ── report assembly ──────────────────────────────────────────────────────────

def _fixture_tree(tmp_path, *, filed=True, sheet_days=2, bank_days=10):
    _touch(tmp_path / "live-sheets" / fa.CASH_SHEET_PATH.name, days_old=sheet_days)
    if filed:
        _touch(tmp_path / "monthly-reports" / "2026-07" / "2026-06_bdm_pl.xlsx")
    for name in fa.BANK_ENTITY_FOLDERS:
        _touch(tmp_path / "bank-statements" / name / "s.pdf", days_old=bank_days)
    return tmp_path


def test_build_report_all_clear(tmp_path):
    report = fa.build_report(today=TODAY, accounting_root=_fixture_tree(tmp_path))
    assert report.generated_date == "2026-08-04"
    assert report.problems == []
    assert len(report.facts) == 3 + len(fa.BANK_ENTITY_FOLDERS)
    keys = [f.key for f in report.facts]
    assert keys[0] == "cash_sheet" and keys[1] == "clover"


def test_build_report_surfaces_problems(tmp_path):
    report = fa.build_report(
        today=TODAY,
        accounting_root=_fixture_tree(tmp_path, filed=False, sheet_days=30, bank_days=200),
    )
    statuses = {f.key: f.status for f in report.facts}
    assert statuses["cash_sheet"] == fa.STATUS_STALE
    assert statuses["monthly_filing 2026-07"] == fa.STATUS_MISSING
    assert statuses["clover"] == fa.STATUS_RETIRED
    assert len(report.problems) == 2 + len(fa.BANK_ENTITY_FOLDERS)


def test_build_report_never_returns_an_empty_block(tmp_path, monkeypatch):
    """Even total failure must yield facts -- an empty block reads as all-clear."""
    def boom(**_kw):
        raise RuntimeError("everything is broken")

    monkeypatch.setattr(fa, "check_cash_sheet", boom)
    monkeypatch.setattr(fa, "check_monthly_filing", boom)
    monkeypatch.setattr(fa, "check_bank_statements", boom)
    report = fa.build_report(today=TODAY, accounting_root=tmp_path)
    # cash_sheet stub + clover + monthly_filing stub + bank_statements stub
    assert len(report.facts) == 4
    assert [f.key for f in report.facts] == [
        "cash_sheet", "clover", "monthly_filing", "bank_statements",
    ]
    assert report.problems == []           # failures are unknown, not problems
    assert len(report.unknowns) == 3       # clover is retired, never unknown
    assert all(f.text.strip() for f in report.facts)


def test_build_report_check_exception_becomes_unknown_not_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(fa, "check_bank_statements",
                        lambda **_kw: (_ for _ in ()).throw(ValueError("bad")))
    report = fa.build_report(today=TODAY, accounting_root=_fixture_tree(tmp_path))
    bank = [f for f in report.facts if f.key == "bank_statements"]
    assert bank and bank[0].status == fa.STATUS_UNKNOWN
    assert "check failed" in bank[0].text


# ── serialization ────────────────────────────────────────────────────────────

# ── roll-up (found by the first live dry-run) ────────────────────────────────

def test_compact_facts_rolls_up_a_large_same_status_group(tmp_path):
    """13 stale folders within a 3-day spread is one cause, not 13 findings.

    Without the roll-up the two facts that DIFFER (cash sheet, monthly filing) are
    buried under 13 near-identical bank lines in the Slack-bound block.
    """
    report = fa.build_report(
        today=TODAY, accounting_root=_fixture_tree(tmp_path, bank_days=200),
    )
    compact = report.compact_facts()
    assert len(report.facts) == 16
    assert len(compact) == 4
    rolled = [line for line in compact if line.startswith("bank_statements (")]
    assert len(rolled) == 1
    assert "13 folders" in rolled[0]
    assert "STALE" in rolled[0]          # status token preserved for flag detection
    assert "200d" in rolled[0]
    for name in fa.BANK_ENTITY_FOLDERS:
        assert name in rolled[0]         # every folder still named


def test_compact_facts_shows_an_age_range_when_ages_differ(tmp_path):
    for i, name in enumerate(fa.BANK_ENTITY_FOLDERS):
        _touch(tmp_path / "bank-statements" / name / "s.pdf", days_old=60 + i)
    _touch(tmp_path / "live-sheets" / fa.CASH_SHEET_PATH.name, days_old=1)
    _touch(tmp_path / "monthly-reports" / "2026-07" / "r.xlsx")
    report = fa.build_report(today=TODAY, accounting_root=tmp_path)
    rolled = next(l for l in report.compact_facts() if l.startswith("bank_statements ("))
    assert "60-72d old" in rolled


def test_compact_facts_keeps_small_groups_individual(tmp_path):
    """Below the threshold, per-folder lines are more useful than a summary."""
    for name in ("LLC", "LBHS"):
        _touch(tmp_path / name / "s.pdf", days_old=200)
    facts = fa.check_bank_statements(today=TODAY, root=tmp_path, folders=("LLC", "LBHS"))
    report = fa.AdherenceReport(generated_date="2026-08-04", facts=facts)
    compact = report.compact_facts()
    assert len(compact) == 2
    assert not any(line.startswith("bank_statements (") for line in compact)


def test_compact_facts_splits_groups_by_status(tmp_path):
    """A mixed group must not collapse a stale folder in with the current ones."""
    for name in fa.BANK_ENTITY_FOLDERS[:4]:
        _touch(tmp_path / name / "s.pdf", days_old=200)
    for name in fa.BANK_ENTITY_FOLDERS[4:]:
        _touch(tmp_path / name / "s.pdf", days_old=3)
    facts = fa.check_bank_statements(today=TODAY, root=tmp_path)
    report = fa.AdherenceReport(generated_date="2026-08-04", facts=facts)
    rolled = [l for l in report.compact_facts() if l.startswith("bank_statements (")]
    assert len(rolled) == 2
    assert any("STALE" in l and "4 folders" in l for l in rolled)
    assert any("OK" in l and "9 folders" in l for l in rolled)


def test_compact_facts_preserves_non_group_facts_in_order(tmp_path):
    report = fa.build_report(today=TODAY, accounting_root=_fixture_tree(tmp_path))
    compact = report.compact_facts()
    assert compact[0].startswith("cash_sheet:")
    assert compact[1].startswith("clover:")
    assert compact[2].startswith("monthly_filing")


def test_markdown_keeps_every_folder_line_as_the_audit_record(tmp_path):
    """The roll-up is for the Slack-bound block only -- Drive keeps the full list."""
    report = fa.build_report(
        today=TODAY, accounting_root=_fixture_tree(tmp_path, bank_days=200),
    )
    md = report.to_markdown()
    for name in fa.BANK_ENTITY_FOLDERS:
        assert f"bank_statements {name}" in md


def test_to_json_carries_both_compact_and_full(tmp_path):
    report = fa.build_report(
        today=TODAY, accounting_root=_fixture_tree(tmp_path, bank_days=200),
    )
    payload = report.to_json()
    assert len(payload["facts"]) == 4            # what downstream renders
    assert len(payload["facts_full"]) == 16      # the complete record
    # Fixture files + filed month, so only the 13 stale bank folders are problems.
    assert payload["problem_count"] == 13
    assert payload["statuses"]["cash_sheet"] == fa.STATUS_OK


def test_rolled_up_stale_line_flags_in_the_close_pack(tmp_path):
    """The roll-up must not cost the downstream flag."""
    from cora import finance_close

    report = fa.build_report(
        today=TODAY, accounting_root=_fixture_tree(tmp_path, bank_days=200),
    )
    cash = finance_close.Section(key="cash", title="c", available=True, lines=["x"], flags=0)
    section = finance_close.build_close_prep_section(
        finance_close.Sources(adherence_facts=lambda: report.to_json()),
        cash_section=cash, today=TODAY,
    )
    body = "\n".join(section.lines)
    assert "bank_statements (13 folders)" in body
    assert ":triangular_flag_on_post:" in body


def test_to_json_shape_matches_what_the_close_pack_reads(tmp_path):
    """Contract test: finance_close reads generated_date + facts[] (list of str)."""
    from cora import finance_close

    report = fa.build_report(today=TODAY, accounting_root=_fixture_tree(tmp_path))
    payload = report.to_json()
    assert isinstance(payload["facts"], list)
    assert all(isinstance(line, str) for line in payload["facts"])
    assert payload["generated_date"] == "2026-08-04"

    path = tmp_path / "facts.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = finance_close.load_adherence_facts(path)
    cash = finance_close.Section(key="cash", title="c", available=True, lines=["x"])
    section = finance_close.build_close_prep_section(
        finance_close.Sources(adherence_facts=lambda: loaded),
        cash_section=cash, today=TODAY,
    )
    body = "\n".join(section.lines)
    assert "Adherence facts as of 2026-08-04" in body
    assert "lane_retired" in body


def test_close_pack_flags_the_missing_filing_line(tmp_path):
    """End-to-end: a MISSING adherence fact must flag in the close pack."""
    from cora import finance_close

    report = fa.build_report(
        today=TODAY, accounting_root=_fixture_tree(tmp_path, filed=False),
    )
    cash = finance_close.Section(key="cash", title="c", available=True, lines=["x"], flags=0)
    section = finance_close.build_close_prep_section(
        finance_close.Sources(adherence_facts=lambda: report.to_json()),
        cash_section=cash, today=TODAY,
    )
    assert section.flags >= 1
    assert "MISSING" in "\n".join(section.lines)


def test_clover_retired_line_never_flags_in_the_close_pack(tmp_path):
    """The retired lane must not become a weekly nag downstream either."""
    from cora import finance_close

    payload = {"generated_date": "2026-08-04", "facts": [fa.clover_fact().line()]}
    cash = finance_close.Section(key="cash", title="c", available=True, lines=["x"], flags=0)
    section = finance_close.build_close_prep_section(
        finance_close.Sources(adherence_facts=lambda: payload),
        cash_section=cash, today=TODAY,
    )
    assert section.flags == 0
    assert "lane_retired" in "\n".join(section.lines)


def test_to_markdown_lists_every_fact(tmp_path):
    report = fa.build_report(today=TODAY, accounting_root=_fixture_tree(tmp_path))
    md = report.to_markdown()
    assert md.startswith("# Finance SOP adherence facts")
    assert "Every line below IS a read" in md
    for fact in report.facts:
        assert fact.key in md


def test_to_markdown_marks_problems_and_unknowns(tmp_path):
    report = fa.build_report(
        today=TODAY, accounting_root=_fixture_tree(tmp_path, filed=False),
    )
    md = report.to_markdown()
    assert "[MISSING]" in md and "[RETIRED]" in md
    assert "need attention" in md


def test_summary_line_is_finance_safe(tmp_path):
    """The line goes to Slack -- it must carry no dollar figure."""
    from cora.channel_content_guard import _has_money_figure

    for filed in (True, False):
        report = fa.build_report(
            today=TODAY, accounting_root=_fixture_tree(tmp_path / str(filed), filed=filed),
        )
        line = report.summary_line()
        assert not _has_money_figure(line)
        assert "$" not in line
        assert line.strip()


def test_summary_line_says_all_clear_or_counts(tmp_path):
    ok = fa.build_report(today=TODAY, accounting_root=_fixture_tree(tmp_path / "a"))
    assert "all clear" in ok.summary_line()
    bad = fa.build_report(
        today=TODAY, accounting_root=_fixture_tree(tmp_path / "b", filed=False),
    )
    assert "need attention" in bad.summary_line()


def test_summary_line_handles_empty_report():
    assert "no checks ran" in fa.AdherenceReport(generated_date="2026-08-04").summary_line()


# ── persistence ──────────────────────────────────────────────────────────────

def test_write_facts_json_is_atomic_and_readable(tmp_path):
    report = fa.build_report(today=TODAY, accounting_root=_fixture_tree(tmp_path))
    out = tmp_path / "state" / "facts.json"
    assert fa.write_facts_json(report, path=out) == out
    assert json.loads(out.read_text(encoding="utf-8"))["generated_date"] == "2026-08-04"
    assert not list(out.parent.glob("*.tmp"))


def test_write_facts_markdown_returns_none_when_mount_gone(tmp_path, monkeypatch):
    """A Drive outage degrades the job; the local JSON stays authoritative."""
    monkeypatch.setattr(drive_io, "write_text_atomic",
                        lambda *a, **k: (_ for _ in ()).throw(drive_io.DriveUnavailable("gone")))
    report = fa.AdherenceReport(generated_date="2026-08-04", facts=[fa.clover_fact()])
    assert fa.write_facts_markdown(report, path=tmp_path / "f.md") is None


def test_write_facts_markdown_writes_in_place(tmp_path):
    report = fa.AdherenceReport(generated_date="2026-08-04", facts=[fa.clover_fact()])
    target = tmp_path / "finance-adherence-facts.md"
    assert fa.write_facts_markdown(report, path=target) == target
    first = target.read_text(encoding="utf-8")
    report.facts.append(Fact := fa.Fact(key="k", status=fa.STATUS_OK, text="t"))
    fa.write_facts_markdown(report, path=target)
    assert target.read_text(encoding="utf-8") != first
    assert len(list(tmp_path.glob("*.md"))) == 1     # in place, not accumulating


def test_facts_md_path_is_in_place_not_dated():
    """D-087: a recurring state file supersedes in place; dated files would accrue."""
    assert fa.FACTS_MD_PATH.name == "finance-adherence-facts.md"
    assert "2026" not in fa.FACTS_MD_PATH.name


def test_facts_json_path_is_what_finance_close_reads():
    from cora import finance_close
    assert fa.FACTS_JSON_PATH == finance_close.ADHERENCE_FACTS_PATH


# ── the script ───────────────────────────────────────────────────────────────

def _load_script():
    import importlib.util
    from pathlib import Path as _P

    path = _P(__file__).resolve().parent.parent / "scripts" / "run_finance_adherence_check.py"
    spec = importlib.util.spec_from_file_location("_run_finance_adherence", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["_run_finance_adherence"] = module
    spec.loader.exec_module(module)
    return module


def test_script_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    script = _load_script()
    report = fa.AdherenceReport(generated_date="2026-08-04", facts=[fa.clover_fact()])
    monkeypatch.setattr(fa, "build_report", lambda **_k: report)

    def fail(*_a, **_kw):
        raise AssertionError("dry run must not write")

    monkeypatch.setattr(fa, "write_facts_json", fail)
    monkeypatch.setattr(fa, "write_facts_markdown", fail)
    monkeypatch.setattr(sys, "argv", ["x", "--dry-run"])
    assert script.main() == 0
    out = capsys.readouterr().out
    assert "[DRY RUN] facts block" in out and "lane_retired" in out


def test_script_live_run_writes_both_artifacts(tmp_path, monkeypatch):
    script = _load_script()
    report = fa.AdherenceReport(generated_date="2026-08-04", facts=[fa.clover_fact()])
    wrote: list[str] = []
    monkeypatch.setattr(fa, "build_report", lambda **_k: report)
    monkeypatch.setattr(fa, "write_facts_json",
                        lambda r, **_k: (wrote.append("json"), tmp_path / "f.json")[1])
    monkeypatch.setattr(fa, "write_facts_markdown",
                        lambda r, **_k: (wrote.append("md"), tmp_path / "f.md")[1])
    monkeypatch.setattr(sys, "argv", ["x"])
    assert script.main() == 0
    assert wrote == ["json", "md"]


def test_script_exits_zero_when_drive_write_fails(monkeypatch, tmp_path):
    """A Drive blip must not read as a failed adherence check."""
    script = _load_script()
    report = fa.AdherenceReport(generated_date="2026-08-04", facts=[fa.clover_fact()])
    monkeypatch.setattr(fa, "build_report", lambda **_k: report)
    monkeypatch.setattr(fa, "write_facts_json", lambda r, **_k: tmp_path / "f.json")
    monkeypatch.setattr(fa, "write_facts_markdown", lambda r, **_k: None)
    monkeypatch.setattr(sys, "argv", ["x"])
    assert script.main() == 0


def test_script_posts_only_to_hjrg_finance(monkeypatch, tmp_path):
    script = _load_script()
    assert script.HJRG_FINANCE_CHANNEL == "C0B3V5SDNAG"
    assert script.HJRG_FINANCE_CHANNEL != "C0BAK65N4TA"   # archived #hjr-finance

    sent: list[tuple[str, str]] = []
    fake = type(sys)("slack_sdk")
    fake.WebClient = lambda token: type("C", (), {
        "chat_postMessage": lambda _s, channel, text, **_k: sent.append((channel, text)),
    })()
    monkeypatch.setitem(sys.modules, "slack_sdk", fake)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    assert script._post_summary("Finance SOP adherence - all clear.") is True
    assert sent[0][0] == script.HJRG_FINANCE_CHANNEL


def test_script_summary_post_without_token_is_soft(monkeypatch):
    script = _load_script()
    monkeypatch.setenv("SLACK_BOT_TOKEN", "")
    assert script._post_summary("x") is False


def test_script_makes_no_model_call():
    """Model-free by design -- so no llm_usage caller= tag applies to this job."""
    from pathlib import Path as _P

    text = (_P(__file__).resolve().parent.parent
            / "scripts" / "run_finance_adherence_check.py").read_text(encoding="utf-8")
    assert "anthropic" not in text.lower()
    assert "messages.create" not in text
    module = (_P(__file__).resolve().parent.parent
              / "src" / "cora" / "finance_adherence.py").read_text(encoding="utf-8")
    assert "messages.create" not in module
