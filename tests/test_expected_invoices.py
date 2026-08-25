"""C13 (cq-015b3bc779e9): the expected-invoice check reports custody honestly.

The properties under test:

  1. MISSING IS NEVER SILENT AND NEVER INFERRED AS FINE. An unreadable list, an
     unreadable ledger, or an entry with no match patterns produces an explicit
     UNAVAILABLE/UNKNOWN -- never an empty all-clear. This is the blank-radar
     failure mode `finance-renewal-radar.yaml` warns about in its own header, and
     the same rule the Standing-ACTUALS label doctrine locked.
  2. A KNOWN CONFIGURATION GAP DOES NOT CRY WOLF. Google Ads invoices are
     verified undeliverable to any monitored mailbox, so the monthly line says so
     and does not count toward the flag total -- but clearing the flag in the YAML
     immediately restores the alarm.
  3. THE PERIOD IS THE LAST CLOSED MONTH. An absent invoice in the still-open
     current month is not yet news.
  4. IT READS CUSTODY, not mailbox arrival: a filing outside the period does not
     count for it.
"""

from __future__ import annotations

import datetime
import json

import pytest

from cora import expected_invoices as ei


def _write_list(tmp_path, entries):
    p = tmp_path / "expected.yaml"
    lines = ["expected:"]
    for e in entries:
        lines.append(f"  - name: {e['name']}")
        if e.get("entity"):
            lines.append(f"    entity: {e['entity']}")
        if e.get("match") is not None:
            lines.append("    match:")
            for m in e["match"]:
                lines.append(f"      - {m}")
        if e.get("known_undelivered"):
            lines.append("    known_undelivered: true")
        if e.get("note"):
            lines.append(f"    note: {e['note']}")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _write_ledger(tmp_path, rows):
    p = tmp_path / "ledger.jsonl"
    out = [json.dumps({"_schema": "cora email-filer content ledger (key=md5)"})]
    out += [json.dumps(r) for r in rows]
    p.write_text("\n".join(out) + "\n", encoding="utf-8")
    return p


_AZ = datetime.timezone(datetime.timedelta(hours=-7))


def _ts(y, m, d, h=0):
    """An Arizona wall-clock instant as a UTC epoch -- which is how the filer
    writes `filed_at` and how accounting thinks about the month."""
    return int(datetime.datetime(y, m, d, h, tzinfo=_AZ).timestamp())


# ── 3. the period ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("today,expect", [
    (datetime.date(2026, 8, 25), "2026-07"),
    (datetime.date(2026, 1, 3), "2025-12"),
    (datetime.date(2026, 3, 1), "2026-02"),
])
def test_the_period_is_the_last_closed_month(today, expect):
    assert ei.previous_period(today) == expect


def test_period_bounds_are_half_open_and_handle_december():
    start, end = ei.period_bounds("2026-12")
    assert start == _ts(2026, 12, 1)
    assert end == _ts(2027, 1, 1)


def test_the_period_is_bounded_in_arizona_not_utc():
    """THE month-end defect. A document filed 2026-07-31 at 21:00 AZ is
    2026-08-01 04:00 UTC, so UTC bounds pushed July's last-day invoice into
    August -- reporting July MISSING while the file sat in Drive. Wrong once a
    month, forever, in the dangerous direction."""
    start, end = ei.period_bounds("2026-07")
    late_july_az = _ts(2026, 7, 31, 21)
    assert start <= late_july_az < end, "a 21:00 AZ filing on the 31st is July"
    # ...and the first hours of August in Arizona are NOT July.
    assert _ts(2026, 8, 1, 1) >= end


def test_a_month_end_filing_is_attributed_to_the_arizona_month(tmp_path):
    lst = _write_list(tmp_path, [{"name": "Google Workspace invoice",
                                  "match": ["google-workspace"]}])
    ledger = _write_ledger(tmp_path, [
        {"drive_path": "01-HJR-Global/invoices/google-workspace-invoice.pdf",
         "filed_at": _ts(2026, 7, 31, 21)}])
    res = ei.assess("2026-07", expectations_path=lst, ledger_path=ledger)
    assert res["results"][0]["status"] == ei.STATUS_PRESENT


@pytest.mark.parametrize("bad", ["2026", "not-a-period", "2026-13", "2026-00", ""])
def test_an_unusable_period_reports_unavailable_instead_of_a_traceback(tmp_path, bad):
    lst = _write_list(tmp_path, [{"name": "V", "match": ["v"]}])
    res = ei.assess(bad, expectations_path=lst,
                    ledger_path=_write_ledger(tmp_path, []))
    # An empty string falls back to the real previous period, which is fine; every
    # other malformed value must be reported, never raised.
    if bad:
        assert res["available"] is False
        assert "unusable period" in res["reason"]


def test_a_vendor_named_non_invoice_does_not_report_the_invoice_as_present(tmp_path):
    """The dangerous direction. A false PRESENT tells accounting a document is in
    hand when it is not, and unlike a false MISSING nobody goes looking."""
    lst = _write_list(tmp_path, [{"name": "Google Ads invoice",
                                  "match": ["google-ads"]}])
    ledger = _write_ledger(tmp_path, [
        {"drive_path": "02-F3-Energy/decks/google-ads-strategy-deck.pdf",
         "filed_at": _ts(2026, 7, 15)}])
    res = ei.assess("2026-07", expectations_path=lst, ledger_path=ledger)
    assert res["results"][0]["status"] == ei.STATUS_MISSING


def test_a_real_vendor_invoice_still_reports_present(tmp_path):
    """The tightening must not reject the thing it is meant to find -- and the
    live filer names these '...-monthly-invoice.pdf' under an 'invoices/' folder."""
    lst = _write_list(tmp_path, [{"name": "Google Ads invoice",
                                  "match": ["google-ads"]}])
    for path in ("01-HJR-Global/invoices/2026-07-02_hjrg_google-ads-monthly-invoice.pdf",
                 "01-HJR-Global/invoices/google-ads-2026-07.pdf",
                 "01-HJR-Global/receipts/google-ads-payment-receipt.pdf"):
        ledger = _write_ledger(tmp_path, [
            {"drive_path": path, "filed_at": _ts(2026, 7, 15)}])
        res = ei.assess("2026-07", expectations_path=lst, ledger_path=ledger)
        assert res["results"][0]["status"] == ei.STATUS_PRESENT, path


# ── 1. unavailability is explicit ────────────────────────────────────────────

def test_a_missing_expectation_list_reports_unavailable(tmp_path):
    res = ei.assess("2026-07", expectations_path=tmp_path / "nope.yaml",
                    ledger_path=_write_ledger(tmp_path, []))
    assert res["available"] is False
    assert "unreadable" in res["reason"]
    assert ei.flag_count(res) == 1, "an unavailable check must need a human"
    assert "Check unavailable" in ei.format_report(res)


def test_a_malformed_expectation_list_reports_unavailable(tmp_path):
    p = tmp_path / "expected.yaml"
    p.write_text("expected: [ this is not: valid: yaml", encoding="utf-8")
    res = ei.assess("2026-07", expectations_path=p,
                    ledger_path=_write_ledger(tmp_path, []))
    assert res["available"] is False


def test_an_empty_ledger_reports_unknown_not_missing(tmp_path):
    """The filer may simply never have run. Crying MISSING across the board off an
    empty ledger would train the reader to ignore the report."""
    lst = _write_list(tmp_path, [{"name": "Google Ads invoice", "match": ["google-ads"]}])
    res = ei.assess("2026-07", expectations_path=lst,
                    ledger_path=tmp_path / "absent.jsonl")
    assert [r["status"] for r in res["results"]] == [ei.STATUS_UNKNOWN]
    assert ei.flag_count(res) == 1


def test_an_entry_with_no_match_patterns_is_named_not_skipped(tmp_path):
    lst = _write_list(tmp_path, [{"name": "Mystery vendor", "match": []}])
    ledger = _write_ledger(tmp_path, [
        {"drive_path": "x/y.pdf", "filed_at": _ts(2026, 7, 2)}])
    res = ei.assess("2026-07", expectations_path=lst, ledger_path=ledger)
    assert res["results"][0]["status"] == ei.STATUS_UNKNOWN
    assert "Mystery vendor" in ei.format_report(res)


def test_a_ledger_with_junk_lines_still_reads_the_good_rows(tmp_path):
    p = tmp_path / "ledger.jsonl"
    p.write_text("\n".join([
        "{not json",
        "",
        json.dumps({"drive_path": "01-HJR-Global/invoices/google-workspace.pdf",
                    "filed_at": _ts(2026, 7, 2)}),
    ]) + "\n", encoding="utf-8")
    lst = _write_list(tmp_path, [{"name": "Google Workspace invoice",
                                  "match": ["google-workspace"]}])
    res = ei.assess("2026-07", expectations_path=lst, ledger_path=p)
    assert res["results"][0]["status"] == ei.STATUS_PRESENT


# ── 4. custody, inside the period ────────────────────────────────────────────

def test_a_filing_inside_the_period_is_present(tmp_path):
    lst = _write_list(tmp_path, [{"name": "Google Workspace invoice",
                                  "match": ["google-workspace"]}])
    ledger = _write_ledger(tmp_path, [
        {"drive_path": "01-HJR-Global/invoices/2026-07-02_hjrg_google-workspace-monthly-invoice.pdf",
         "filed_at": _ts(2026, 7, 2)}])
    res = ei.assess("2026-07", expectations_path=lst, ledger_path=ledger)
    assert res["results"][0]["status"] == ei.STATUS_PRESENT
    assert ei.flag_count(res) == 0


def test_a_filing_outside_the_period_does_not_count(tmp_path):
    """Last month's invoice is not this month's invoice."""
    lst = _write_list(tmp_path, [{"name": "Google Workspace invoice",
                                  "match": ["google-workspace"]}])
    ledger = _write_ledger(tmp_path, [
        {"drive_path": "01-HJR-Global/invoices/google-workspace.pdf",
         "filed_at": _ts(2026, 6, 2)}])
    res = ei.assess("2026-07", expectations_path=lst, ledger_path=ledger)
    assert res["results"][0]["status"] == ei.STATUS_MISSING


def test_a_row_with_no_filed_at_is_not_counted_as_present(tmp_path):
    lst = _write_list(tmp_path, [{"name": "Google Workspace invoice",
                                  "match": ["google-workspace"]}])
    ledger = _write_ledger(tmp_path, [
        {"drive_path": "01-HJR-Global/invoices/google-workspace.pdf"}])
    res = ei.assess("2026-07", expectations_path=lst, ledger_path=ledger)
    assert res["results"][0]["status"] == ei.STATUS_MISSING


def test_match_patterns_are_substrings_not_regexes(tmp_path):
    """They come from a human-maintained YAML file: a stray regex metacharacter
    must not raise, and must not silently match everything."""
    lst = _write_list(tmp_path, [{"name": "Odd vendor", "match": ["a(b"]}])
    ledger = _write_ledger(tmp_path, [
        {"drive_path": "invoices/a(b-2026.pdf", "filed_at": _ts(2026, 7, 2)},
    ])
    res = ei.assess("2026-07", expectations_path=lst, ledger_path=ledger)
    assert res["results"][0]["status"] == ei.STATUS_PRESENT


# ── 2. the known configuration gap ───────────────────────────────────────────

def test_a_known_undelivered_vendor_does_not_count_toward_the_flags(tmp_path):
    lst = _write_list(tmp_path, [
        {"name": "Google Ads invoice", "entity": "F3E", "match": ["google-ads"],
         "known_undelivered": True, "note": "Billing contact is outside the org."}])
    ledger = _write_ledger(tmp_path, [
        {"drive_path": "x/other.pdf", "filed_at": _ts(2026, 7, 2)}])
    res = ei.assess("2026-07", expectations_path=lst, ledger_path=ledger)
    assert res["results"][0]["status"] == ei.STATUS_MISSING
    assert ei.flag_count(res) == 0, "a tracked config gap must not alarm monthly"
    report = ei.format_report(res)
    assert "not expected to be" in report
    assert "rotating_light" not in report


def test_clearing_the_known_flag_restores_the_alarm(tmp_path):
    lst = _write_list(tmp_path, [
        {"name": "Google Ads invoice", "entity": "F3E", "match": ["google-ads"]}])
    ledger = _write_ledger(tmp_path, [
        {"drive_path": "x/other.pdf", "filed_at": _ts(2026, 7, 2)}])
    res = ei.assess("2026-07", expectations_path=lst, ledger_path=ledger)
    assert ei.flag_count(res) == 1
    assert "rotating_light" in ei.format_report(res)


# ── presentation ─────────────────────────────────────────────────────────────

def test_a_long_note_is_cut_on_a_word_boundary_not_mid_word():
    """Mid-word truncation has shipped on three separate surfaces in this repo and
    each time read as corruption rather than brevity."""
    note = ("Verified 2026-08-25: never filed, and no Google Ads billing email has "
            "reached a monitored mailbox in one hundred and twenty days because the "
            "account was granted by invitation from an address outside the "
            "organisation entirely")
    out = ei._first_sentence(note)
    assert len(out) <= ei._NOTE_CHARS + 4
    assert not out.rstrip(" .").endswith(("organi", "outsid", "becaus"))
    assert out.endswith("...") or out.endswith(".")


def test_a_note_shorter_than_the_cap_is_untouched():
    assert ei._first_sentence("Billing contact is outside the org.") == \
        "Billing contact is outside the org."


def test_an_absent_note_still_renders_something():
    assert ei._first_sentence(None) == "delivery not configured"
    assert ei._first_sentence("") == "delivery not configured"


def test_an_all_clear_says_so_explicitly(tmp_path):
    lst = _write_list(tmp_path, [{"name": "Google Workspace invoice",
                                  "match": ["google-workspace"]}])
    ledger = _write_ledger(tmp_path, [
        {"drive_path": "invoices/google-workspace.pdf", "filed_at": _ts(2026, 7, 2)}])
    res = ei.assess("2026-07", expectations_path=lst, ledger_path=ledger)
    assert "Everything expected for this period is filed." in ei.format_report(res)


def test_the_report_says_it_measures_custody_not_arrival(tmp_path):
    lst = _write_list(tmp_path, [{"name": "V", "match": ["v"]}])
    res = ei.assess("2026-07", expectations_path=lst,
                    ledger_path=_write_ledger(tmp_path, [
                        {"drive_path": "v.pdf", "filed_at": _ts(2026, 7, 2)}]))
    assert "CUSTODY" in ei.format_report(res)


# ── the shipped list is real ──────────────────────────────────────────────────

def test_the_shipped_expectation_list_parses_and_has_match_patterns():
    """A shipped entry with no `match` would report UNKNOWN forever."""
    items = ei.load_expectations()
    assert items, "the shipped list must parse"
    for item in items:
        assert item.get("name")
        assert item.get("match"), f"{item.get('name')} has no match patterns"


def test_the_shipped_list_flags_google_ads_as_a_known_gap():
    """Measured 2026-08-25: never filed, and no billing email in 120 days. If
    someone clears this flag without fixing delivery, the monthly report starts
    alarming -- which is the correct behaviour, and this test documents why the
    flag is set."""
    items = {i["name"]: i for i in (ei.load_expectations() or [])}
    ads = items.get("Google Ads invoice")
    assert ads is not None
    assert ads.get("known_undelivered") is True
    assert "352-797-6311" in str(ads.get("note") or ""), \
        "the note must carry the account id so the fix is actionable"
