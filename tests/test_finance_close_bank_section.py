"""A5 S2 -- the close pack's "QBO bank & books freshness" section, plus the
OSN consolidated-row rider (cq-6fbb9d717512).

Two things here are load-bearing beyond ordinary rendering:

1. The section must NEVER reuse the snapshot's own portfolio total. The snapshot
   spans every provisioned realm including HR LLC, which is pack-excluded for
   SENSITIVITY -- so reusing that figure would put personal-expense cash into a
   multi-member finance channel.

2. The OSN cash cross-check row compared a CONSOLIDATED sheet row against a
   cash-less shell realm and flagged a phantom ~$37.6K every week.
"""

from __future__ import annotations

import datetime

import pytest

from cora import finance_close as fc

MONDAY = datetime.date(2026, 8, 3)


def _snapshot(**overrides):
    base = {
        "generated_at_utc": "2026-08-03T14:05:00+00:00",
        "basis": "QBO account register (Account API)",
        "covered": 3,
        "expected": 3,
        "realms": {
            "F3E": _realm(13051.47, -1300.54, 11750.93, "2026-08-02"),
            "BDM": _realm(11758.94, 0.0, 11758.94, "2026-08-01"),
            "OSN": _realm(0.0, 0.0, 0.0, "2026-07-07", shell=True),
        },
        "portfolio": {"bank_total": 99999.0, "cc_total": 0.0,
                      "cash_net_of_cards": 99999.0, "realms_included": ["F3E", "BDM"]},
        "portfolio_withheld_reason": None,
        "errors": {},
    }
    base.update(overrides)
    return base


def _realm(bank, cc, net, newest, *, status="ok", shell=False, complete=True, error=None):
    return {
        "status": status, "error": error, "shell": shell,
        "bank_total": bank, "cc_total": cc, "cash_net_of_cards": net,
        "balances_complete": complete,
        "newest_bank_txn_date": newest,
        "as_of_utc": "2026-08-03T14:05:00+00:00",
    }


def _sources(snapshot):
    return fc.Sources(bank_snapshot=lambda: snapshot)


def _now(hours_after=1):
    # The section computes age against real "now", so build stamps relative to it.
    import datetime as dt
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours_after)) \
        .replace(microsecond=0).isoformat()


class TestUnavailable:
    def test_missing_snapshot_is_an_honest_stub(self):
        section, snap = fc.build_bank_section(["F3E"], fc.Sources(bank_snapshot=lambda: None))
        assert section.available is False
        assert "has not run yet" in section.stub_reason
        assert snap == {}

    def test_realm_absent_from_snapshot_renders_unavailable_not_zero(self):
        section, _ = fc.build_bank_section(
            ["F3E", "HJRG"], _sources(_snapshot()), today=MONDAY)
        body = "\n".join(section.lines)
        assert "HJR Global: unavailable" in body
        assert section.covered == 1  # F3E only (OSN not in the entity list)

    def test_errored_realm_renders_unavailable_and_blocks_the_total(self):
        snap = _snapshot()
        snap["realms"]["BDM"] = _realm(None, None, None, None, status="error",
                                       error="HTTP 401 unauthorized")
        section, _ = fc.build_bank_section(["F3E", "BDM"], _sources(snap), today=MONDAY)
        body = "\n".join(section.lines)
        assert "Big D Media: unavailable" in body
        assert "Total withheld" in body
        assert "$0.00" not in body.split("Big D Media")[1].split("\n")[0]


class TestRendering:
    def test_renders_bank_cards_and_net(self):
        snap = _snapshot(generated_at_utc=_now(1))
        section, frag = fc.build_bank_section(["F3E"], _sources(snap), today=MONDAY)
        body = "\n".join(section.lines)
        assert "$13,051" in body
        assert "$11,751" in body
        assert frag["F3E"]["cash_net_of_cards"] == 11750.93

    def test_shell_realm_is_a_footnote_not_a_balance_row(self):
        snap = _snapshot(generated_at_utc=_now(1))
        section, frag = fc.build_bank_section(["OSN"], _sources(snap), today=MONDAY)
        body = "\n".join(section.lines)
        assert "cash-less holding shell" in body
        assert "OSN" not in frag, "a shell realm contributes no balance fragment"

    def test_method_difference_footer_is_present(self):
        section, _ = fc.build_bank_section(["F3E"], _sources(_snapshot()), today=MONDAY)
        body = "\n".join(section.lines)
        assert "ACCOUNT REGISTER" in body
        assert "BalanceSheet REPORT" in body
        assert "NOT a reconciliation break" in body

    def test_footer_names_no_numeric_tolerance(self):
        """The design proposed naming an expected tolerance; live data refuted it
        (opposite signs on the same account). Promising a tolerance would be a
        confidently-wrong claim."""
        assert "tolerance" not in fc._BANK_METHOD_FOOTER.lower()

    def test_coverage_is_structure_not_just_prose(self):
        section, _ = fc.build_bank_section(
            ["F3E", "BDM", "HJRG"], _sources(_snapshot()), today=MONDAY)
        assert section.expected == 3
        assert section.covered == 2
        assert section.is_partial is True


class TestSensitivityExclusion:
    def test_pack_excluded_realm_is_never_rendered(self):
        snap = _snapshot()
        snap["realms"]["HRLLC"] = _realm(2211.11, 0.0, 2211.11, "2026-08-03")
        section, frag = fc.build_bank_section(
            ["F3E", "HRLLC"], _sources(snap), today=MONDAY)
        body = "\n".join(section.lines)
        assert "HR LLC" not in body
        assert "2,211.11" not in body
        assert "HRLLC" not in frag
        assert section.expected == 1, "an excluded realm must not inflate the denominator"

    def test_snapshot_portfolio_total_is_not_reused(self):
        """The snapshot's own portfolio spans pack-excluded realms. Reusing it
        would leak HR LLC's cash into a multi-member finance channel."""
        snap = _snapshot()
        snap["portfolio"]["cash_net_of_cards"] = 99999.0
        section, _ = fc.build_bank_section(["F3E", "BDM"], _sources(snap), today=MONDAY)
        body = "\n".join(section.lines)
        assert "99,999" not in body
        # It computes its own total over exactly the rendered rows.
        assert "$23,510" in body  # 11,750.93 + 11,758.94 = 23,509.87


class TestFreshnessFlags:
    def test_stale_txn_flags(self, monkeypatch):
        monkeypatch.setenv("FINANCE_BANK_TXN_STALE_DAYS", "14")
        snap = _snapshot()
        snap["realms"]["F3E"]["newest_bank_txn_date"] = "2026-06-01"
        section, _ = fc.build_bank_section(["F3E"], _sources(snap), today=MONDAY)
        assert section.flags == 1
        assert ":triangular_flag_on_post:" in "\n".join(section.lines)

    def test_recent_txn_does_not_flag(self, monkeypatch):
        monkeypatch.setenv("FINANCE_BANK_TXN_STALE_DAYS", "14")
        section, _ = fc.build_bank_section(["F3E"], _sources(_snapshot()), today=MONDAY)
        assert section.flags == 0

    def test_threshold_is_env_tunable(self, monkeypatch):
        snap = _snapshot()
        snap["realms"]["F3E"]["newest_bank_txn_date"] = "2026-07-25"  # 9d before MONDAY
        monkeypatch.setenv("FINANCE_BANK_TXN_STALE_DAYS", "5")
        section, _ = fc.build_bank_section(["F3E"], _sources(snap), today=MONDAY)
        assert section.flags == 1

    def test_unknown_txn_date_does_not_flag_but_says_unknown(self):
        snap = _snapshot()
        snap["realms"]["F3E"]["newest_bank_txn_date"] = None
        section, _ = fc.build_bank_section(["F3E"], _sources(snap), today=MONDAY)
        assert section.flags == 0
        assert "UNKNOWN" in "\n".join(section.lines)


class TestSnapshotStaleness:
    def test_old_snapshot_is_labelled_stale(self):
        section, _ = fc.build_bank_section(
            ["F3E"], _sources(_snapshot(generated_at_utc="2026-07-01T00:00:00+00:00")),
            today=MONDAY)
        assert ":warning:" in section.lines[0]
        assert "not today" in section.lines[0]

    def test_fresh_snapshot_is_not_labelled_stale(self):
        section, _ = fc.build_bank_section(
            ["F3E"], _sources(_snapshot(generated_at_utc=_now(2))), today=MONDAY)
        assert ":warning:" not in section.lines[0]

    def test_missing_timestamp_is_unknown_age_not_current(self):
        snap = _snapshot()
        snap.pop("generated_at_utc")
        section, _ = fc.build_bank_section(["F3E"], _sources(snap), today=MONDAY)
        assert "UNKNOWN age" in section.lines[0]


# ── the rider: cq-6fbb9d717512 ───────────────────────────────────────────────

def _bs(bank, cards=None):
    rows = [{"type": "Section",
             "Header": {"ColData": [{"value": "Bank Accounts"}]},
             "Summary": {"ColData": [{"value": "Total Bank Accounts"},
                                     {"value": f"{bank:.2f}"}]}}]
    if cards is not None:
        rows.append({"type": "Section",
                     "Header": {"ColData": [{"value": "Credit Cards"}]},
                     "Summary": {"ColData": [{"value": "Total Credit Cards"},
                                             {"value": f"{cards:.2f}"}]}})
    return {"Rows": {"Row": rows}}


class TestOsnConsolidatedRider:
    """Live 2026-08-04: sheet "OSN" is the tab "OSN Consolidated", closing $37,605
    = Warner 4,722 + McKellips 6,936 + Greenfield 4,365 + Val Vista 21,581. The
    QBO realm OSN is a $0 shell. The old comparison flagged that whole gap weekly.
    """

    ENTITIES = ["OSN", "OSNGF", "OSNGM", "OSNGW", "OSNVV"]

    def _sheet(self, sheet_entity):
        values = {"OSN": 37605.0, "OSN-GF": 4365.0, "OSN-MK": 6936.0,
                  "OSN-GW": 4722.0, "OSN-VV": 21581.0}
        return {"closing": values[sheet_entity], "is_actual": True,
                "week_label": "Week of 7-31", "stale": False, "age_days": 3}

    def _books(self, entity, as_of):
        # Realm OSN is the cash-less shell; the stores carry the money.
        return _bs({"OSN": 0.0, "OSNGF": 4365.0, "OSNGM": 6936.0,
                    "OSNGW": 4722.0, "OSNVV": 21581.0}[entity])

    def _src(self, **kw):
        return fc.Sources(cash_closing=self._sheet, balance_sheet=self._books, **kw)

    def test_consolidated_row_no_longer_false_flags(self):
        section, snap = fc.build_cash_section(self.ENTITIES, self._src(), today=MONDAY)
        body = "\n".join(section.lines)
        osn_line = next(ln for ln in section.lines if ln.startswith("• ") and "One Stop Nutrition:" in ln)
        assert ":triangular_flag_on_post:" not in osn_line, (
            f"OSN consolidated row still false-flags: {osn_line}"
        )
        assert section.flags == 0

    def test_books_leg_is_the_sum_of_member_realms(self):
        section, snap = fc.build_cash_section(self.ENTITIES, self._src(), today=MONDAY)
        assert snap["OSN"]["books_net"] == 37604.0   # the four stores summed
        assert snap["OSN"]["delta"] == pytest.approx(-1.0)

    def test_row_says_it_is_consolidated_and_the_realm_holds_no_cash(self):
        section, _ = fc.build_cash_section(self.ENTITIES, self._src(), today=MONDAY)
        osn_line = next(ln for ln in section.lines if "One Stop Nutrition:" in ln)
        assert "consolidated row" in osn_line
        assert "holds no cash" in osn_line
        for store in ("OSN Greenfield", "OSN McKellips", "OSN Warner", "OSN Val Vista"):
            assert store in osn_line

    def test_a_real_consolidation_break_still_flags(self):
        """The fix must not blind the check -- if the stores stop tying to the
        consolidated row, that is a genuine signal."""
        def books(entity, as_of):
            return _bs({"OSN": 0.0, "OSNGF": 4365.0, "OSNGM": 6936.0,
                        "OSNGW": 4722.0, "OSNVV": 1000.0}[entity])
        section, _ = fc.build_cash_section(
            self.ENTITIES, fc.Sources(cash_closing=self._sheet, balance_sheet=books),
            today=MONDAY)
        osn_line = next(ln for ln in section.lines if "One Stop Nutrition:" in ln)
        assert ":triangular_flag_on_post:" in osn_line

    def test_partial_members_render_unknown_never_a_partial_sum(self):
        """A partial sum against a FULL consolidated sheet row would manufacture
        exactly the false flag this rider removes."""
        def books(entity, as_of):
            if entity == "OSNVV":
                raise RuntimeError("realm down")
            return self._books(entity, as_of)
        section, snap = fc.build_cash_section(
            self.ENTITIES, fc.Sources(cash_closing=self._sheet, balance_sheet=books),
            today=MONDAY)
        osn_line = next(ln for ln in section.lines if "One Stop Nutrition:" in ln)
        assert "unavailable" in osn_line
        assert "consolidated member" in osn_line
        assert "OSN" not in snap

    def test_member_realms_still_checked_individually(self):
        section, snap = fc.build_cash_section(self.ENTITIES, self._src(), today=MONDAY)
        for store in ("OSNGF", "OSNGM", "OSNGW", "OSNVV"):
            assert store in snap, f"{store} lost its own cross-check row"

    def test_rollup_map_matches_the_sheet_tab_mapping(self):
        """Pins the membership: the sheet's OSN Consolidated tab is exactly these
        four stores. A new OSN store must be added here too, or the roll-up check
        silently compares against an incomplete sum."""
        assert fc.SHEET_ROLLUPS["OSN"] == ("OSNGF", "OSNGM", "OSNGW", "OSNVV")
        for member in fc.SHEET_ROLLUPS["OSN"]:
            assert member in fc.QBO_TO_SHEET_ENTITY

    def test_non_rollup_entities_are_untouched(self):
        section, snap = fc.build_cash_section(
            ["F3E"],
            fc.Sources(cash_closing=lambda e: {"closing": 100.0, "is_actual": True,
                                               "week_label": "Week of 7-31",
                                               "stale": False, "age_days": 3},
                       balance_sheet=lambda e, a: _bs(100.0)),
            today=MONDAY)
        assert snap["F3E"]["books_net"] == 100.0
        assert "consolidated row" not in "\n".join(section.lines)
