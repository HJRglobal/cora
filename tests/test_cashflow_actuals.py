"""13WCF M2 / S2 -- the weekly QBO actuals store.

The pins that matter are the ones a green suite and a plausible figure both hide:

  * a PRELIMINARY window's blocks must never self-label usable for comparison;
  * a bank balance that covers only some accounts must render UNKNOWN, not a
    partial sum under a total's name;
  * an internal sweep between two of our own bank accounts must not read as
    income AND spend in the same week;
  * a realm we failed to read, or whose every transaction type came back empty,
    must render UNKNOWN -- never $0 of activity;
  * an unattributable realm (one QBO LEX realm, five Lex tabs) must not be
    collected at all, let alone attributed to a guessed tab;
  * the week-ending day comes from the sheet's own grid, never a hardcoded Friday.
"""

from __future__ import annotations

import datetime
import json

import pytest

from cora import cashflow_actuals as ca
from cora import cashflow_ledger as cl
from cora import cashflow_maps as cm


MONDAY = datetime.date(2026, 8, 10)      # a Monday
W1 = datetime.date(2026, 8, 7)           # the Friday that just closed
W2 = datetime.date(2026, 7, 31)


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    """Never touch the real ledger: it is the estate's most loss-critical store."""
    monkeypatch.setattr(cl, "STORE_DIR", tmp_path / "cashflow-ledger")
    monkeypatch.setattr(cl, "FORECAST_SNAPSHOT_DIR",
                        tmp_path / "cashflow-ledger" / "forecast-snapshots")
    monkeypatch.setattr(ca, "ACTUALS_DIR", tmp_path / "cashflow-ledger" / "actuals")
    monkeypatch.setenv("FOUNDER_OS_ROOT", str(tmp_path / "drive"))


def _bank_snapshot(weekday: str = "Friday", date_: str = "2026-08-10"):
    cl.FORECAST_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    (cl.FORECAST_SNAPSHOT_DIR / f"{date_}_forecast.json").write_text(
        json.dumps({"snapshot_date": date_, "week_ending_weekday": weekday,
                    "covered": 19, "expected": 19}), encoding="utf-8")


# ── week resolution ──────────────────────────────────────────────────────────

class TestResolveWindows:
    def test_derives_the_weekday_from_the_banked_snapshot(self):
        _bank_snapshot("Friday")
        weekday, w1, w2 = ca.resolve_windows(MONDAY)
        assert weekday == "Friday"
        assert (w1, w2) == (W1, W2)

    def test_honours_a_non_friday_workbook(self):
        """Fin-13: the week-ending day is a property of the sheet, not of code.
        If Justin re-cuts the grid to Sundays, the windows must follow."""
        _bank_snapshot("Sunday")
        weekday, w1, w2 = ca.resolve_windows(MONDAY)
        assert weekday == "Sunday"
        assert w1 == datetime.date(2026, 8, 9)

    def test_refuses_when_no_snapshot_has_ever_been_banked(self):
        """Assuming Friday is exactly the assumption D-126/Fin-13 forbid."""
        with pytest.raises(ca.ActualsError, match="never assumed"):
            ca.resolve_windows(MONDAY)

    def test_an_older_snapshot_still_answers(self):
        """S2 must not fail merely because S1 missed this Monday -- the weekday is
        a stable property of the workbook."""
        _bank_snapshot("Friday", date_="2026-07-06")
        assert ca.resolve_windows(MONDAY)[0] == "Friday"

    def test_week_ending_day_that_is_today_looks_back_a_week(self):
        _bank_snapshot("Monday")
        _weekday, w1, _w2 = ca.resolve_windows(MONDAY)
        assert w1 == datetime.date(2026, 8, 3)


# ── gross split / internal transfers ─────────────────────────────────────────

class TestSplitGross:
    def test_plain_rows(self):
        out = ca.split_gross([{"txn_id": "1", "amount": 100.0},
                              {"txn_id": "2", "amount": -40.0}])
        assert out["receipts"] == 100.0
        assert out["disbursements"] == -40.0
        assert out["internal_transfers_excluded"] == 0.0

    def test_internal_sweep_is_excluded_from_both_sides(self):
        """A $15,140.10 move between two of our own accounts is neither income
        nor spend. Left in, it inflates BOTH -- and net flow still looks right,
        which is what makes it easy to miss."""
        out = ca.split_gross([{"txn_id": "9", "amount": -15140.10},
                              {"txn_id": "9", "amount": 15140.10},
                              {"txn_id": "3", "amount": -20.20}])
        assert out["receipts"] == 0.0
        assert out["disbursements"] == -20.20
        assert out["internal_transfers_excluded"] == 15140.10

    def test_bank_to_card_payment_stays_a_real_disbursement(self):
        """Only one leg is inside the bank perimeter, so the group cannot cancel.
        This IS the cash event for carded spend."""
        out = ca.split_gross([{"txn_id": "1005", "amount": -5983.53}])
        assert out["disbursements"] == -5983.53
        assert out["internal_transfers_excluded"] == 0.0

    def test_net_flow_is_unaffected_by_the_exclusion(self):
        rows = [{"txn_id": "9", "amount": -500.0}, {"txn_id": "9", "amount": 500.0},
                {"txn_id": "3", "amount": -20.0}]
        out = ca.split_gross(rows)
        assert round(out["receipts"] + out["disbursements"], 2) == \
            round(sum(r["amount"] for r in rows), 2)

    def test_rows_without_a_txn_id_are_still_counted(self):
        out = ca.split_gross([{"txn_id": None, "amount": 12.0}])
        assert out["receipts"] == 12.0

    def test_journal_entry_internal_move_is_also_excluded(self):
        """Generalised past `Transfer` deliberately: a sweep booked as a JE has
        the same two-legs-cancelling shape and the same absence of cash effect."""
        out = ca.split_gross([{"txn_id": "je1", "amount": 3548.88},
                              {"txn_id": "je1", "amount": -3548.88}])
        assert (out["receipts"], out["disbursements"]) == (0.0, 0.0)
        assert out["internal_transfers_excluded"] == 3548.88


# ── categorisation ───────────────────────────────────────────────────────────

def _category_map(**accounts):
    data = {
        "categories": {"operating_disbursements": ["Rent", "Utilities"],
                       "receipts": ["Services"]},
        "expense_categories": ["Rent", "Utilities"],
        "realms": {"F3E": {"accounts": accounts}},
    }
    return data


def _load_category_map(tmp_path, data) -> cm.CategoryMap:
    path = tmp_path / "cat.yaml"
    import yaml
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return cm.load_category_map(path)


class TestClassifyRows:
    def test_confirmed_mapping_places_the_row(self, tmp_path):
        cmap = _load_category_map(tmp_path, _category_map(**{
            "109": {"account_type": "Expense", "category": "Rent", "confirmed": True}}))
        out = ca.classify_rows(
            [{"split_account_id": "109", "amount": -1200.0}], "F3E", cmap)
        assert out["categories"] == {"Rent": -1200.0}
        assert out["uncategorized"]["rows"] == 0

    def test_unconfirmed_mapping_is_not_guessed_onto_a_row(self, tmp_path):
        """D-118: an unconfirmed mapping resolves to nothing on purpose. The
        amount is REPORTED as unplaced, never silently dropped."""
        cmap = _load_category_map(tmp_path, _category_map(**{
            "109": {"account_type": "Expense", "category": "Rent", "confirmed": False}}))
        out = ca.classify_rows(
            [{"split_account_id": "109", "amount": -1200.0}], "F3E", cmap)
        assert out["categories"] == {}
        assert out["uncategorized"]["amount"] == -1200.0
        assert out["uncategorized"]["reasons"] == {"mapping_unconfirmed": 1}

    def test_multi_line_split_is_reported_as_such(self, tmp_path):
        cmap = _load_category_map(tmp_path, _category_map())
        out = ca.classify_rows([{"split_account_id": None, "amount": -267.04}],
                               "F3E", cmap)
        assert out["uncategorized"]["reasons"] == {"multi_line_split": 1}

    def test_account_absent_from_the_map(self, tmp_path):
        cmap = _load_category_map(tmp_path, _category_map())
        out = ca.classify_rows([{"split_account_id": "999", "amount": -5.0}],
                               "F3E", cmap)
        assert out["uncategorized"]["reasons"] == {"account_not_in_map": 1}

    def test_amounts_accumulate_per_category(self, tmp_path):
        cmap = _load_category_map(tmp_path, _category_map(**{
            "109": {"account_type": "Expense", "category": "Rent", "confirmed": True}}))
        out = ca.classify_rows([{"split_account_id": "109", "amount": -100.0},
                                {"split_account_id": "109", "amount": -50.0}],
                               "F3E", cmap)
        assert out["categories"] == {"Rent": -150.0}


# ── per-realm build ──────────────────────────────────────────────────────────

def _entity_map(**pairs) -> cm.EntityMap:
    return cm.EntityMap(
        pairs={k: cm.RealmPairing(realm=k, **v) for k, v in pairs.items()},
        excluded_realms=cm.HARD_EXCLUDED_REALMS,
        manual_entry_tabs=["CF_UFL"],
        derived_tabs=["CF_SUMMARY"],
    )


def _gl(rows, opening=1000.0, identity=None, accounts_with_opening=1):
    return {"rows": rows, "row_count": len(rows), "opening_balance": opening,
            "accounts_with_opening": accounts_with_opening,
            "opening_conflict": False,
            "identity": identity or {"checked": 1, "worst_residual": 0.0, "failed": []},
            "duplicate_row_keys": 0, "sections": {}}


def _check(net, **kw):
    base = {"net": net, "per_type": {"Purchase": net}, "counts": {"Purchase": 1},
            "types_expected": 9, "internal_transfers": 0.0, "credit_rows": 0,
            "empty_types": [], "unexpected_keys": {}, "errors": {},
            "capped_types": []}
    base.update(kw)
    return base


def _build(pairing, gl, check, cmap, freshness_date="2026-08-07",
           window_kind=ca.WINDOW_FINALIZED):
    return ca.build_realm(
        "F3E", pairing, week_start=W1 - datetime.timedelta(days=6), week_ending=W1,
        window_kind=window_kind, category_map=cmap,
        query_accounts=lambda r: [{"id": "9", "type": "Bank"},
                                  {"id": "52", "type": "Credit Card"}],
        ledger_rows=lambda *a: gl,
        recompute=lambda *a: check,
        freshness=lambda *a: {"date": freshness_date},
    )


class TestBuildRealm:
    def _cmap(self, tmp_path):
        return _load_category_map(tmp_path, _category_map())

    def test_happy_path(self, tmp_path):
        rows = [{"txn_id": "1", "amount": -100.0, "split_account_id": None}]
        block = _build(cm.RealmPairing(realm="F3E", tab="CF_F3", confirmed=True),
                       _gl(rows), _check(-100.0), self._cmap(tmp_path))
        assert block["status"] == "ok"
        assert block["net_flow"] == -100.0
        assert block["closing_bank_balance"] == 900.0
        assert block["tie_out"]["status"] == "ok"
        assert block["usable_for_comparison"] is True
        assert block["posted_through"] == "2026-08-07"

    def test_a_preliminary_window_is_never_usable_for_comparison(self, tmp_path):
        """D-051 MED-HIGH. The payload's notes say "never use a preliminary window
        for accuracy math", but a consumer reads the FIELD, not the notes -- and it
        used to say True even on a confirmed, tied-out preliminary block. That is
        the Fin-1 failure the module claims to have designed out, re-entering
        through a field name."""
        rows = [{"txn_id": "1", "amount": -100.0, "split_account_id": None}]
        pairing = cm.RealmPairing(realm="F3E", tab="CF_F3", confirmed=True)
        prelim = _build(pairing, _gl(rows), _check(-100.0), self._cmap(tmp_path),
                        window_kind=ca.WINDOW_PRELIMINARY)
        final = _build(pairing, _gl(rows), _check(-100.0), self._cmap(tmp_path),
                       window_kind=ca.WINDOW_FINALIZED)
        assert prelim["usable_for_comparison"] is False
        assert final["usable_for_comparison"] is True
        assert prelim["net_flow"] == -100.0          # still banked, still stamped

    def test_balances_are_withheld_when_the_ledger_omitted_accounts(self, tmp_path):
        """D-051 HIGH, live-verified: QBO's General Ledger renders only accounts
        with activity -- LEX returned ONE section for TWELVE bank accounts -- so the
        opening it reports is a partial sum. Published as `opening_bank_balance`
        that is a wrong number, and `closing = opening + net` inherits it while the
        tie-out stays clean, because the FLOW is right and nothing checks the
        BALANCE."""
        rows = [{"txn_id": "1", "amount": -100.0, "split_account_id": None}]
        block = ca.build_realm(
            "LEX2", cm.RealmPairing(realm="LEX2", tab="CF_X", confirmed=True),
            week_start=W2, week_ending=W1, window_kind=ca.WINDOW_FINALIZED,
            category_map=self._cmap(tmp_path),
            # 3 bank accounts in the perimeter, 1 rendered by the report.
            query_accounts=lambda r: [{"id": str(i), "type": "Bank"} for i in (1, 2, 3)],
            ledger_rows=lambda *a: _gl(rows, opening=160749.40,
                                       accounts_with_opening=1),
            recompute=lambda *a: _check(-100.0),
            freshness=lambda *a: {"date": "2026-08-07"})
        assert block["opening_bank_balance"] is None
        assert block["closing_bank_balance"] is None
        assert block["balances"]["complete"] is False
        assert block["balances"]["accounts_with_opening"] == 1
        assert block["balances"]["bank_accounts"] == 3
        # The partial figure survives under a name that says what it is.
        assert block["balances"]["opening_rendered_accounts_only"] == 160749.40
        # And the flow, which IS complete, is untouched.
        assert block["net_flow"] == -100.0

    def test_complete_balances_are_published(self, tmp_path):
        rows = [{"txn_id": "1", "amount": -100.0, "split_account_id": None}]
        block = _build(cm.RealmPairing(realm="F3E", tab="CF_F3", confirmed=True),
                       _gl(rows, opening=1000.0, accounts_with_opening=1),
                       _check(-100.0), self._cmap(tmp_path))
        assert block["opening_bank_balance"] == 1000.0
        assert block["closing_bank_balance"] == 900.0
        assert block["balances"]["complete"] is True

    def test_a_same_label_section_collapse_withholds_balances(self, tmp_path):
        rows = [{"txn_id": "1", "amount": -100.0, "split_account_id": None}]
        gl = _gl(rows)
        gl["opening_conflict"] = True
        block = _build(cm.RealmPairing(realm="F3E", tab="CF_F3", confirmed=True),
                       gl, _check(-100.0), self._cmap(tmp_path))
        assert block["opening_bank_balance"] is None
        assert "collapse" in block["balances"]["reason"]

    def test_unconfirmed_pair_is_not_usable_for_comparison(self, tmp_path):
        block = _build(cm.RealmPairing(realm="F3E", tab="CF_F3", confirmed=False),
                       _gl([]), _check(0.0), self._cmap(tmp_path))
        assert block["map_confirmed"] is False
        assert block["usable_for_comparison"] is False

    def test_unresolvable_realm_is_not_collected_at_all(self, tmp_path):
        """The LEX case. Attributing one QBO realm's activity to whichever of five
        Lex tabs we guessed would put LBHS/LTS/LLA money on LLC. Refuse -- and do
        not even read it (D-124: exclude at collection)."""
        pairing = cm.RealmPairing(realm="LEX", tab=None,
                                  candidate_tabs=["CF_LLC", "CF_LBHS"])
        called: list[str] = []
        block = ca.build_realm(
            "LEX", pairing, week_start=W2, week_ending=W1,
            window_kind=ca.WINDOW_FINALIZED, category_map=self._cmap(tmp_path),
            query_accounts=lambda r: called.append(r) or [],
            ledger_rows=lambda *a: called.append("gl") or _gl([]),
            recompute=lambda *a: _check(0.0), freshness=lambda *a: {"date": None},
        )
        assert block["status"] == "refused"
        assert block["reason_code"] == "realm_scope_undeclared"
        assert called == []          # nothing was read
        assert "net_flow" not in block

    def test_realm_missing_from_the_entity_map_refuses(self, tmp_path):
        block = _build(None, _gl([]), _check(0.0), self._cmap(tmp_path))
        assert block["reason_code"] == "realm_not_in_entity_map"

    def test_tie_out_failure_banks_but_blocks_comparison(self, tmp_path):
        """Two independent computations disagree: one is wrong and we do not know
        which, so the figure is not published as comparable."""
        rows = [{"txn_id": "1", "amount": -100.0, "split_account_id": None}]
        block = _build(cm.RealmPairing(realm="F3E", tab="CF_F3", confirmed=True),
                       _gl(rows), _check(-88.0), self._cmap(tmp_path))
        assert block["tie_out"]["status"] == "failed"
        assert block["tie_out"]["residual"] == -12.0
        assert block["usable_for_comparison"] is False
        assert block["net_flow"] == -100.0      # still banked

    def test_recompute_error_makes_the_tie_out_unavailable(self, tmp_path):
        block = _build(cm.RealmPairing(realm="F3E", tab="CF_F3", confirmed=True),
                       _gl([]), _check(None, errors={"Deposit": "HTTP 500 boom"}),
                       self._cmap(tmp_path))
        assert block["tie_out"]["status"] == "unavailable"
        assert block["usable_for_comparison"] is False

    def test_every_type_empty_renders_unknown_not_zero(self, tmp_path):
        """cq-db2fd53aa608. QBO was observed serving empty responses for every
        transaction type while Account queries kept working; $0 of activity there
        is a wrong number, not a missing one."""
        block = _build(cm.RealmPairing(realm="F3E", tab="CF_F3", confirmed=True),
                       _gl([], opening=1000.0),
                       _check(0.0, empty_types=["a"] * 9, per_type={}, counts={}),
                       self._cmap(tmp_path))
        assert block["status"] == "unknown"
        assert block["reason_code"] == "all_transaction_types_empty"
        assert block["net_flow"] is None
        assert block["usable_for_comparison"] is False

    def test_one_empty_type_is_ordinary(self, tmp_path):
        """Most realms book no Transfers most weeks -- a signal that fires on the
        normal cadence is not a signal (D-127a)."""
        rows = [{"txn_id": "1", "amount": -100.0, "split_account_id": None}]
        block = _build(cm.RealmPairing(realm="F3E", tab="CF_F3", confirmed=True),
                       _gl(rows), _check(-100.0, empty_types=["Transfer"]),
                       self._cmap(tmp_path))
        assert block["status"] == "ok"

    def test_unexpected_query_key_blocks_comparison(self, tmp_path):
        rows = [{"txn_id": "1", "amount": -100.0, "split_account_id": None}]
        block = _build(cm.RealmPairing(realm="F3E", tab="CF_F3", confirmed=True),
                       _gl(rows), _check(-100.0,
                                         unexpected_keys={"Deposit": "DepositV2"}),
                       self._cmap(tmp_path))
        assert block["usable_for_comparison"] is False

    def test_page_cap_blocks_comparison(self, tmp_path):
        rows = [{"txn_id": "1", "amount": -100.0, "split_account_id": None}]
        block = _build(cm.RealmPairing(realm="F3E", tab="CF_F3", confirmed=True),
                       _gl(rows), _check(-100.0, capped_types=["Purchase"]),
                       self._cmap(tmp_path))
        assert block["usable_for_comparison"] is False

    def test_broken_register_identity_blocks_comparison(self, tmp_path):
        rows = [{"txn_id": "1", "amount": -100.0, "split_account_id": None}]
        block = _build(cm.RealmPairing(realm="F3E", tab="CF_F3", confirmed=True),
                       _gl(rows, identity={"checked": 1, "worst_residual": 42.0,
                                           "failed": ["acct"]}),
                       _check(-100.0), self._cmap(tmp_path))
        assert block["usable_for_comparison"] is False

    def test_read_failure_renders_unknown_never_zero(self, tmp_path):
        block = ca.build_realm(
            "F3E", cm.RealmPairing(realm="F3E", tab="CF_F3", confirmed=True),
            week_start=W2, week_ending=W1, window_kind=ca.WINDOW_FINALIZED,
            category_map=self._cmap(tmp_path),
            query_accounts=lambda r: (_ for _ in ()).throw(RuntimeError("HTTP 503")),
            ledger_rows=lambda *a: _gl([]), recompute=lambda *a: _check(0.0),
            freshness=lambda *a: {"date": None})
        assert block["status"] == "error"
        assert block["net_flow"] is None
        assert block["reason_code"] == "api_server_error"

    def test_error_reason_never_carries_raw_exception_text(self, tmp_path):
        """D-127g: raw failure text embeds the request URI and therefore the realm
        id, and this payload is mirrored into a shared accounting folder."""
        secret = "https://quickbooks.api.intuit.com/v3/company/9130354../query"
        block = ca.build_realm(
            "F3E", cm.RealmPairing(realm="F3E", tab="CF_F3", confirmed=True),
            week_start=W2, week_ending=W1, window_kind=ca.WINDOW_FINALIZED,
            category_map=self._cmap(tmp_path),
            query_accounts=lambda r: (_ for _ in ()).throw(RuntimeError(secret)),
            ledger_rows=lambda *a: _gl([]), recompute=lambda *a: _check(0.0),
            freshness=lambda *a: {"date": None})
        assert secret not in json.dumps(block)

    def test_no_bank_accounts_is_unknown(self, tmp_path):
        block = ca.build_realm(
            "F3E", cm.RealmPairing(realm="F3E", tab="CF_F3", confirmed=True),
            week_start=W2, week_ending=W1, window_kind=ca.WINDOW_FINALIZED,
            category_map=self._cmap(tmp_path),
            query_accounts=lambda r: [{"id": "52", "type": "Credit Card"}],
            ledger_rows=lambda *a: _gl([]), recompute=lambda *a: _check(0.0),
            freshness=lambda *a: {"date": None})
        assert block["reason_code"] == "no_bank_accounts"

    def test_freshness_failure_does_not_discard_the_figures(self, tmp_path):
        rows = [{"txn_id": "1", "amount": -100.0, "split_account_id": None}]
        block = ca.build_realm(
            "F3E", cm.RealmPairing(realm="F3E", tab="CF_F3", confirmed=True),
            week_start=W2, week_ending=W1, window_kind=ca.WINDOW_FINALIZED,
            category_map=self._cmap(tmp_path),
            query_accounts=lambda r: [{"id": "9", "type": "Bank"}],
            ledger_rows=lambda *a: _gl(rows), recompute=lambda *a: _check(-100.0),
            freshness=lambda *a: (_ for _ in ()).throw(RuntimeError("nope")))
        assert block["status"] == "ok"
        assert block["net_flow"] == -100.0
        assert block["posted_through"] is None

    def test_unknown_opening_leaves_closing_unknown(self, tmp_path):
        rows = [{"txn_id": "1", "amount": -100.0, "split_account_id": None}]
        block = _build(cm.RealmPairing(realm="F3E", tab="CF_F3", confirmed=True),
                       _gl(rows, opening=None), _check(-100.0), self._cmap(tmp_path))
        assert block["opening_bank_balance"] is None
        assert block["closing_bank_balance"] is None


# ── window build ─────────────────────────────────────────────────────────────

def _build_window(realms, kind=ca.WINDOW_PRELIMINARY, emap=None, tmp_path=None,
                  net=-100.0, **kw):
    import yaml
    path = tmp_path / "cat.yaml"
    path.write_text(yaml.safe_dump(_category_map()), encoding="utf-8")
    rows = [{"txn_id": "1", "amount": net, "split_account_id": None}]
    return ca.build_window(
        realms, window_kind=kind, week_ending=W1, weekday_name="Friday",
        entity_map=emap or _entity_map(F3E={"tab": "CF_F3", "confirmed": True}),
        category_map=cm.load_category_map(path),
        query_accounts=lambda r: [{"id": "9", "type": "Bank"}],
        ledger_rows=lambda *a: _gl(rows), recompute=lambda *a: _check(net),
        freshness=lambda *a: {"date": "2026-08-07"}, today=MONDAY, **kw)


class TestBuildWindow:
    def test_coverage_is_structure_not_prose(self, tmp_path):
        payload = _build_window(["F3E"], tmp_path=tmp_path)
        assert (payload["covered"], payload["expected"]) == (1, 1)
        assert payload["week_ending"] == W1.isoformat()
        assert payload["week_start"] == "2026-08-01"

    def test_excluded_realms_are_never_collected(self, tmp_path):
        """HR LLC is personal books; the OSN realm is a cash-less shell. Both are
        dropped at the sweep, not filtered downstream."""
        payload = _build_window(["F3E", "HRLLC", "OSN"], tmp_path=tmp_path)
        assert set(payload["realms"]) == {"F3E"}
        assert payload["expected"] == 1
        assert "HRLLC" in payload["excluded_realms"]

    def test_finalized_window_declares_what_it_supersedes(self, tmp_path):
        payload = _build_window(["F3E"], kind=ca.WINDOW_FINALIZED, tmp_path=tmp_path)
        assert payload["supersedes"] == f"{W1.isoformat()}_prelim-actuals.json"

    def test_preliminary_window_supersedes_nothing(self, tmp_path):
        assert _build_window(["F3E"], tmp_path=tmp_path)["supersedes"] is None

    def test_preliminary_notes_warn_about_the_missing_weekend(self, tmp_path):
        notes = " ".join(_build_window(["F3E"], tmp_path=tmp_path)["notes"])
        assert "Friday-Sunday" in notes
        assert "bank portal" in notes

    def test_finalized_notes_say_comparison_binds_here(self, tmp_path):
        notes = " ".join(_build_window(["F3E"], kind=ca.WINDOW_FINALIZED,
                                       tmp_path=tmp_path)["notes"])
        assert "SUPERSEDES" in notes

    def test_zero_readable_realms_refuses_to_write(self, tmp_path):
        """D-127c: a dated FILE is not evidence of a banked week. An all-failed
        run must not produce something a monitor calls green."""
        import yaml
        path = tmp_path / "cat.yaml"
        path.write_text(yaml.safe_dump(_category_map()), encoding="utf-8")
        with pytest.raises(ca.ActualsError, match="refusing to write an empty"):
            ca.build_window(
                ["F3E"], window_kind=ca.WINDOW_PRELIMINARY, week_ending=W1,
                weekday_name="Friday",
                entity_map=_entity_map(F3E={"tab": "CF_F3", "confirmed": True}),
                category_map=cm.load_category_map(path),
                query_accounts=lambda r: (_ for _ in ()).throw(RuntimeError("dead")),
                ledger_rows=lambda *a: _gl([]), recompute=lambda *a: _check(0.0),
                freshness=lambda *a: {"date": None}, today=MONDAY)

    def test_partial_sweep_is_recorded(self, tmp_path):
        payload = _build_window(["F3E"], tmp_path=tmp_path,
                                full_scope=["F3E", "BDM"])
        assert payload["partial_sweep"] is True
        assert payload["expected"] == 2

    def test_an_unresolvable_realm_leaves_the_coverage_denominator(self, tmp_path):
        """D-122's corollary. LEX cannot be read until Justin declares its split,
        so counting it in `expected` would make every healthy week report itself
        partial -- training the reader to ignore the one signal that marks a real
        gap. It is named instead."""
        emap = _entity_map(F3E={"tab": "CF_F3", "confirmed": True},
                           LEX={"tab": None, "candidate_tabs": ["CF_LLC", "CF_LBHS"]})
        payload = _build_window(["F3E", "LEX"], emap=emap, tmp_path=tmp_path)
        assert payload["expected"] == 1
        assert payload["covered"] == 1
        assert payload["awaiting_map_confirmation"] == ["LEX"]

    def test_a_realm_absent_from_the_map_stays_in_the_denominator(self, tmp_path):
        """Different failure from the one above: a provisioned realm nobody has
        mapped may be carrying real money, so it must keep reading as a gap rather
        than quietly leaving the count."""
        emap = _entity_map(F3E={"tab": "CF_F3", "confirmed": True})
        payload = _build_window(["F3E", "BDM"], emap=emap, tmp_path=tmp_path)
        assert payload["expected"] == 2
        assert payload["covered"] == 1
        assert payload["awaiting_map_confirmation"] == []
        assert payload["realms"]["BDM"]["reason_code"] == "realm_not_in_entity_map"

    def test_manual_entry_and_derived_tabs_ride_the_payload(self, tmp_path):
        """D-117: a tab with no QBO source is not a failure, and a derived
        roll-up has nothing to map. Both must be distinguishable from a gap."""
        payload = _build_window(["F3E"], tmp_path=tmp_path)
        assert payload["manual_entry_tabs"] == ["CF_UFL"]
        assert payload["derived_tabs"] == ["CF_SUMMARY"]

    def test_unknown_window_kind_refuses(self, tmp_path):
        with pytest.raises(ca.ActualsError, match="unknown window kind"):
            _build_window(["F3E"], kind="sort-of-final", tmp_path=tmp_path)

    def test_basis_and_perimeter_are_always_stated(self, tmp_path):
        payload = _build_window(["F3E"], tmp_path=tmp_path)
        assert "bank-cash only" in payload["basis"]
        assert "not a cash event" in payload["cash_perimeter"]


# ── persistence ──────────────────────────────────────────────────────────────

class TestWriteWindow:
    def test_round_trip(self, tmp_path):
        payload = _build_window(["F3E"], tmp_path=tmp_path)
        path = ca.write_window(payload, today=MONDAY)
        assert path.name == f"{W1.isoformat()}_prelim-actuals.json"
        assert ca.load_window(W1, ca.WINDOW_PRELIMINARY)["covered"] == 1

    def test_preliminary_alongside_a_finalized_week_is_allowed(self, tmp_path):
        """The refusal that used to live here protected NOTHING: prelim and final
        are separate files and every accuracy consumer reads load_finalized, so a
        D-051 reviewer showed the matured figure surviving intact. Its only real
        effect was to fail routine backfills and train the operator to add
        --overwrite reflexively -- the flag that disables the guards that matter."""
        ca.write_window(_build_window(["F3E"], kind=ca.WINDOW_FINALIZED,
                                      net=-250.0, tmp_path=tmp_path), today=MONDAY)
        ca.write_window(_build_window(["F3E"], net=-100.0, tmp_path=tmp_path),
                        today=MONDAY)
        assert ca.load_finalized(W1)["realms"]["F3E"]["net_flow"] == -250.0

    def test_re_pulling_the_same_kind_is_allowed(self, tmp_path):
        """Not destructive: the whole design rests on QBO being re-readable,
        which is what makes the finalized re-pull possible at all."""
        ca.write_window(_build_window(["F3E"], tmp_path=tmp_path), today=MONDAY)
        ca.write_window(_build_window(["F3E"], net=-200.0, tmp_path=tmp_path),
                        today=MONDAY)
        assert ca.load_window(W1, ca.WINDOW_PRELIMINARY)["realms"]["F3E"]["net_flow"] == -200.0

    def test_partial_sweep_refuses_to_overwrite_a_full_file(self, tmp_path):
        ca.write_window(_build_window(["F3E"], tmp_path=tmp_path), today=MONDAY)
        partial = _build_window(["F3E"], tmp_path=tmp_path,
                                full_scope=["F3E", "BDM"])
        with pytest.raises(ca.ActualsError, match="PARTIAL sweep"):
            ca.write_window(partial, today=MONDAY)

    def test_a_coverage_regression_refuses(self, tmp_path):
        """D-051 MED. A full sweep where 7 of 8 realms error is NOT a partial
        sweep, and covered==1 clears the zero floor -- so a QBO outage could
        replace a complete matured week with one reading UNKNOWN almost
        everywhere. The data is re-pullable; the RECORD of what was there is not."""
        emap = _entity_map(F3E={"tab": "CF_F3", "confirmed": True},
                           BDM={"tab": "CF_BigDM", "confirmed": True})
        ca.write_window(_build_window(["F3E", "BDM"], emap=emap, tmp_path=tmp_path),
                        today=MONDAY)
        thin = _build_window(["F3E", "BDM"], emap=emap, tmp_path=tmp_path)
        thin["covered"] = 1
        with pytest.raises(ca.ActualsError, match="better record with a worse"):
            ca.write_window(thin, today=MONDAY)

    def test_equal_or_better_coverage_is_allowed(self, tmp_path):
        ca.write_window(_build_window(["F3E"], tmp_path=tmp_path), today=MONDAY)
        assert ca.write_window(_build_window(["F3E"], net=-5.0, tmp_path=tmp_path),
                               today=MONDAY).exists()

    def test_future_dated_window_refuses(self, tmp_path):
        """One stray future file blinds a max()-based missed-run check for
        months (D-127c)."""
        payload = _build_window(["F3E"], tmp_path=tmp_path)
        payload["week_ending"] = "2027-01-01"
        with pytest.raises(ca.ActualsError, match="future-dated"):
            ca.write_window(payload, today=MONDAY)

    def test_tmp_file_is_process_unique(self, tmp_path):
        payload = _build_window(["F3E"], tmp_path=tmp_path)
        ca.write_window(payload, today=MONDAY)
        assert not list(ca.ACTUALS_DIR.glob("*.tmp"))


class TestLoadPreference:
    def test_load_actuals_prefers_finalized(self, tmp_path):
        ca.write_window(_build_window(["F3E"], net=-100.0, tmp_path=tmp_path),
                        today=MONDAY)
        ca.write_window(_build_window(["F3E"], kind=ca.WINDOW_FINALIZED,
                                      net=-250.0, tmp_path=tmp_path), today=MONDAY)
        assert ca.load_actuals(W1)["realms"]["F3E"]["net_flow"] == -250.0

    def test_load_finalized_ignores_a_preliminary(self, tmp_path):
        """Accuracy math binds to matured weeks only -- it must not silently fall
        back to a window QBO was incomplete for."""
        ca.write_window(_build_window(["F3E"], tmp_path=tmp_path), today=MONDAY)
        assert ca.load_finalized(W1) is None

    def test_list_finalized_weeks(self, tmp_path):
        ca.write_window(_build_window(["F3E"], kind=ca.WINDOW_FINALIZED,
                                      tmp_path=tmp_path), today=MONDAY)
        assert ca.list_finalized_weeks() == [W1]

    def test_window_coverage_reads_the_payload_not_the_filename(self, tmp_path):
        ca.write_window(_build_window(["F3E"], tmp_path=tmp_path), today=MONDAY)
        assert ca.window_coverage(W1, ca.WINDOW_PRELIMINARY) == (1, 1)

    def test_window_coverage_of_a_corrupt_file_is_unknown(self, tmp_path):
        ca.ACTUALS_DIR.mkdir(parents=True, exist_ok=True)
        ca.actuals_path(W1, ca.WINDOW_FINALIZED).write_text("{{{", encoding="utf-8")
        assert ca.window_coverage(W1, ca.WINDOW_FINALIZED) is None

    def test_unparseable_filename_is_ignored_not_fatal(self, tmp_path):
        ca.ACTUALS_DIR.mkdir(parents=True, exist_ok=True)
        (ca.ACTUALS_DIR / "notadate_final-actuals.json").write_text("{}", encoding="utf-8")
        assert ca.list_finalized_weeks() == []


# ── rendering safety ─────────────────────────────────────────────────────────

class TestAnnotateAdvisory:
    """chain_check must not blame a back-date for a MISSING week."""

    def _payload(self, week, opening=1000.0):
        return {"window_kind": ca.WINDOW_FINALIZED, "week_ending": week,
                "realms": {"F3E": {"status": "ok",
                                   "opening_bank_balance": opening}}}

    def test_adjacent_weeks_are_compared(self):
        prior = {"week_ending": "2026-07-31",
                 "realms": {"F3E": {"closing_bank_balance": 900.0}}}
        out = ca.annotate_advisory(self._payload("2026-08-07"),
                                   prior_finalized=prior)
        chain = out["realms"]["F3E"]["chain_check"]
        assert chain["status"] == "checked"
        assert chain["residual"] == 100.0

    def test_a_gap_is_reported_as_a_gap_not_a_back_date(self):
        """Both review lenses found this independently: chaining to the newest
        EARLIER week rather than the ADJACENT one turns a missing week into a
        six-figure "activity booked into a finalised week" alarm, when the residual
        is simply the absent week's own net flow."""
        prior = {"week_ending": "2026-07-24",
                 "realms": {"F3E": {"closing_bank_balance": 900.0}}}
        out = ca.annotate_advisory(self._payload("2026-08-07"),
                                   prior_finalized=prior)
        chain = out["realms"]["F3E"]["chain_check"]
        assert chain["status"] == "not_adjacent"
        assert chain["gap_weeks"] == 2
        assert "residual" not in chain
        assert "--week" in chain["note"]

    def test_withheld_balances_render_unavailable_not_checked(self):
        prior = {"week_ending": "2026-07-31",
                 "realms": {"F3E": {"closing_bank_balance": None}}}
        out = ca.annotate_advisory(self._payload("2026-08-07", opening=None),
                                   prior_finalized=prior)
        assert out["realms"]["F3E"]["chain_check"]["status"] == "unavailable"

    def test_a_preliminary_window_gets_no_advisory_blocks(self):
        payload = self._payload("2026-08-07")
        payload["window_kind"] = ca.WINDOW_PRELIMINARY
        out = ca.annotate_advisory(payload, prior_finalized={
            "week_ending": "2026-07-31",
            "realms": {"F3E": {"closing_bank_balance": 900.0}}})
        assert "chain_check" not in out["realms"]["F3E"]

    def test_register_reference_is_only_comparable_at_the_window_end(self):
        snapshot = {"basis": "QBO account register (Account API)",
                    "realms": {"F3E": {"status": "ok", "bank_total": 1234.0,
                                       "as_of_utc": "2026-08-20T07:05:00+00:00"}}}
        out = ca.annotate_advisory(self._payload("2026-08-07"),
                                   register_snapshot=snapshot)
        ref = out["realms"]["F3E"]["register_reference"]
        assert ref["comparable"] is False
        assert "DIFFERENT measure" in ref["note"]


class TestSafeLabel:
    def test_strips_control_characters(self):
        assert ca.safe_label("CF_LLC\x00\x1f") == "CF_LLC"

    def test_bounds_length(self):
        assert len(ca.safe_label("x" * 500)) == 80

    def test_none_becomes_empty(self):
        assert ca.safe_label(None) == ""


class TestReasonCode:
    @pytest.mark.parametrize("text,code", [
        ("QBO auth error for entity=F3E", "auth_error"),
        ("QBO API error: HTTP 400 - bad", "api_client_error"),
        ("QBO API error: HTTP 503", "api_server_error"),
        ("did not carry the expected columns", "report_shape_changed"),
        ("HTTP error reaching QBO", "network_error"),
        ("something new", "unknown"),
    ])
    def test_codes(self, text, code):
        assert ca.reason_code(text) == code


class TestMirrorPaths:
    def test_mirror_lands_under_the_accounting_tree(self, tmp_path):
        path = ca.mirror_actuals_path(W1, ca.WINDOW_FINALIZED)
        assert path.parts[-4:] == ("accounting", "cashflow-ledger", "actuals",
                                   f"{W1.isoformat()}_final-actuals.json")

    def test_same_ignoring_stamps(self):
        a = json.dumps({"generated_at_utc": "t1", "run_date": "d1", "x": 1})
        b = json.dumps({"generated_at_utc": "t2", "run_date": "d2", "x": 1})
        c = json.dumps({"generated_at_utc": "t1", "run_date": "d1", "x": 2})
        assert ca.same_ignoring_stamps(a, b) is True
        assert ca.same_ignoring_stamps(a, c) is False
