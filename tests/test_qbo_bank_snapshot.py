"""A5 S1 -- QBO account reads + the daily bank snapshot.

The load-bearing pins here are the ones a green suite would otherwise hide:
the CREDIT-CARD SIGN CONVENTION (a wrong sign ADDS card debt to cash), the
`Active = true` filter (inactive accounts silently inflate totals), the
freshness union's BillPayment leg (its absence flags bill-paying realms stale
forever), and the invariant that a `.json` under 01-HJR-Global/accounting/ is
ingested by NEITHER sweep path.

Every constant asserted below was verified against live QBO on 2026-08-04.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest

from cora import qbo_bank_snapshot as qbs
from cora.tools import qbo_client as qc


# ── query shape ──────────────────────────────────────────────────────────────

class TestAccountQueryShape:
    def test_active_filter_is_present(self):
        """Load-bearing, not hygiene: an unfiltered Account query returns closed
        accounts, and any residual balance on one silently inflates every total."""
        assert "Active = true" in qc._ACCOUNT_QUERY

    def test_uses_IN_not_chained_OR(self):
        """Verified live 2026-08-04: the chained-OR form is REJECTED with HTTP 400
        'Encountered <OR>'. QBO's query language has no OR operator."""
        assert "AccountType in ('Bank','Credit Card')" in qc._ACCOUNT_QUERY
        assert " or " not in qc._ACCOUNT_QUERY.lower().replace("orderby", "")

    def test_selects_the_fields_the_snapshot_renders(self):
        for field in ("Name", "AccountType", "AccountSubType", "CurrentBalance"):
            assert field in qc._ACCOUNT_QUERY

    def test_bank_side_txn_types_include_billpayment(self):
        """BillPayment is the standard pay-bills workflow. Omitting it would flag
        every bill-paying realm (HJRG/HJRP/LEX/all four OSN stores) stale forever."""
        assert set(qc._BANK_SIDE_TXN_TYPES) == {"Purchase", "Deposit", "Transfer", "BillPayment"}


class TestQueryAccounts:
    def test_parses_and_paginates(self, monkeypatch):
        page1 = {"Account": [
            {"Id": str(i), "Name": f"acct{i}", "AccountType": "Bank",
             "AccountSubType": "Checking", "CurrentBalance": 1.0}
            for i in range(qc._ACCOUNT_PAGE_SIZE)
        ]}
        page2 = {"Account": [
            {"Id": "999", "Name": "last", "AccountType": "Credit Card",
             "AccountSubType": "CreditCard", "CurrentBalance": -5.0}
        ]}
        calls: list[str] = []

        def fake_query(entity, query):
            calls.append(query)
            return page1 if "STARTPOSITION 1 " in query else page2

        monkeypatch.setattr(qc, "_query", fake_query)
        rows = qc.query_accounts("F3E")
        assert len(rows) == qc._ACCOUNT_PAGE_SIZE + 1
        assert len(calls) == 2, "a second page must be fetched, not silently dropped"
        assert rows[-1]["name"] == "last"

    def test_missing_balance_stays_none_not_zero(self, monkeypatch):
        """A None balance must never become 0.0 -- that reads as a real zero."""
        monkeypatch.setattr(qc, "_query", lambda e, q: {"Account": [
            {"Id": "1", "Name": "a", "AccountType": "Bank"},
        ]})
        assert qc.query_accounts("F3E")[0]["balance"] is None

    def test_string_balance_is_parsed(self, monkeypatch):
        monkeypatch.setattr(qc, "_query", lambda e, q: {"Account": [
            {"Id": "1", "Name": "a", "AccountType": "Bank", "CurrentBalance": "1,234.56"},
        ]})
        assert qc.query_accounts("F3E")[0]["balance"] == 1234.56


# ── THE SIGN GATE ────────────────────────────────────────────────────────────

class TestCreditCardSignGate:
    """Verified live across all 11 realms, 2026-08-04.

    Query-API `Account.CurrentBalance` reports a card LIABILITY as NEGATIVE; the
    BalanceSheet report reports the same liability POSITIVE. OSNVV proved it
    cleanly -- identical magnitude, opposite sign: query -3,945.64 / report
    +3,945.64. So cash net of cards is bank + cc HERE.
    """

    def test_negative_card_balance_reduces_cash(self):
        accounts = [
            {"id": "1", "name": "checking", "type": "Bank", "balance": 13051.47},
            {"id": "2", "name": "card", "type": "Credit Card", "balance": -1300.54},
        ]
        s = qc.summarize_accounts(accounts)
        assert s["bank_total"] == 13051.47
        assert s["cc_total"] == -1300.54
        # The whole point: 13051.47 - 1300.54, NOT 13051.47 + 1300.54.
        assert s["cash_net_of_cards"] == 11750.93
        assert s["cash_net_of_cards"] < s["bank_total"]

    def test_osnvv_live_figures_reproduce(self):
        """Pins the exact live numbers the gate was verified against."""
        s = qc.summarize_accounts([
            {"id": "1", "name": "b", "type": "Bank", "balance": 16405.54},
            {"id": "2", "name": "c", "type": "Credit Card", "balance": -3945.64},
        ])
        assert s["cash_net_of_cards"] == 12459.90

    def test_wrong_sign_convention_would_be_caught(self):
        """Guard against a future 'fix' flipping the operator back: with the
        report-side convention (bank - cc) this card debt would ADD to cash."""
        s = qc.summarize_accounts([
            {"id": "1", "name": "b", "type": "Bank", "balance": 100.0},
            {"id": "2", "name": "c", "type": "Credit Card", "balance": -40.0},
        ])
        assert s["cash_net_of_cards"] == 60.0
        assert s["cash_net_of_cards"] != 140.0

    def test_zero_card_balance_is_a_no_op(self):
        s = qc.summarize_accounts([
            {"id": "1", "name": "b", "type": "Bank", "balance": 11758.94},
            {"id": "2", "name": "c", "type": "Credit Card", "balance": 0.0},
        ])
        assert s["cash_net_of_cards"] == 11758.94

    def test_unknown_balances_are_counted_and_flagged(self):
        s = qc.summarize_accounts([
            {"id": "1", "name": "b", "type": "Bank", "balance": 100.0},
            {"id": "2", "name": "b2", "type": "Bank", "balance": None},
            {"id": "3", "name": "c", "type": "Credit Card", "balance": None},
        ])
        assert s["bank_total"] == 100.0     # the unknown is skipped, not zeroed
        assert s["bank_unknown"] == 1
        assert s["cc_unknown"] == 1
        assert s["balances_complete"] is False


# ── freshness union ──────────────────────────────────────────────────────────

class TestFreshnessUnion:
    def _fake(self, per_type: dict, payments=None, errors=()):
        def fake_query(entity, query):
            for typ, date in per_type.items():
                if f"from {typ} " in query:
                    if typ in errors:
                        raise qc.QboClientError(f"{typ} boom")
                    return {typ: ([{"TxnDate": date}] if date else [])}
            if "from Payment " in query:
                return {"Payment": payments or []}
            return {}
        return fake_query

    def test_takes_the_newest_across_types(self, monkeypatch):
        monkeypatch.setattr(qc, "_query", self._fake({
            "Purchase": "2026-08-02", "Deposit": "2026-07-31",
            "Transfer": "2026-01-19", "BillPayment": "2026-08-04",
        }))
        out = qc.newest_bank_side_txn_date("HJRG")
        assert out["date"] == "2026-08-04"

    def test_billpayment_only_realm_is_not_reported_stale(self, monkeypatch):
        """The named regression: a realm whose only recent activity is bill
        payments must NOT read as months stale."""
        monkeypatch.setattr(qc, "_query", self._fake({
            "Purchase": "2026-05-01", "Deposit": "2026-05-01",
            "Transfer": None, "BillPayment": "2026-08-03",
        }))
        assert qc.newest_bank_side_txn_date("LEX")["date"] == "2026-08-03"

    def test_payment_counted_only_when_it_lands_in_a_bank_account(self, monkeypatch):
        """Verified live on F3E: payments deposit either to a Bank account (id 9)
        or to Undeposited Funds (id 215, an Other Current Asset). The latter has
        NOT touched the bank, so counting it would report false freshness."""
        monkeypatch.setattr(qc, "_query", self._fake(
            {"Purchase": "2026-07-01", "Deposit": "2026-07-01",
             "Transfer": None, "BillPayment": None},
            payments=[
                {"TxnDate": "2026-08-04", "DepositToAccountRef": {"value": "215"}},
                {"TxnDate": "2026-07-29", "DepositToAccountRef": {"value": "9"}},
            ],
        ))
        out = qc.newest_bank_side_txn_date("F3E", bank_account_ids={"9"})
        assert out["per_type"]["Payment"] == "2026-07-29"
        assert out["date"] == "2026-07-29"

    def test_undeposited_only_payments_contribute_nothing(self, monkeypatch):
        monkeypatch.setattr(qc, "_query", self._fake(
            {"Purchase": "2026-07-01", "Deposit": None,
             "Transfer": None, "BillPayment": None},
            payments=[{"TxnDate": "2026-08-04", "DepositToAccountRef": {"value": "215"}}],
        ))
        out = qc.newest_bank_side_txn_date("F3E", bank_account_ids={"9"})
        assert out["per_type"]["Payment"] is None
        assert out["date"] == "2026-07-01"

    def test_one_failing_type_degrades_coverage_not_the_answer(self, monkeypatch):
        monkeypatch.setattr(qc, "_query", self._fake({
            "Purchase": "2026-08-02", "Deposit": "2026-07-31",
            "Transfer": None, "BillPayment": None,
        }, errors=("Transfer",)))
        out = qc.newest_bank_side_txn_date("BDM")
        assert out["date"] == "2026-08-02"
        assert out["types_covered"] == out["types_expected"] - 1
        assert "Transfer" in out["errors"]

    def test_no_transactions_returns_none_not_a_date(self, monkeypatch):
        monkeypatch.setattr(qc, "_query", self._fake({
            "Purchase": None, "Deposit": None, "Transfer": None, "BillPayment": None,
        }))
        assert qc.newest_bank_side_txn_date("OSN")["date"] is None


# ── snapshot assembly ────────────────────────────────────────────────────────

def _stub_sources(balances: dict[str, tuple[float, float]], newest="2026-08-04",
                  fail: set[str] = frozenset()):
    def query_accounts(entity):
        if entity in fail:
            raise qc.QboClientError("realm down")
        bank, cc = balances[entity]
        return [
            {"id": "1", "name": "b", "type": "Bank", "balance": bank},
            {"id": "2", "name": "c", "type": "Credit Card", "balance": cc},
        ]

    def freshness(entity, bank_ids):
        return {"date": newest, "per_type": {}, "types_covered": 5,
                "types_expected": 5, "errors": {}}

    return query_accounts, qc.summarize_accounts, freshness


_CFG_ON = {"portfolio_total": {"enabled": True, "roll_up_verified": True},
           "realms": {"OSN": {"shell": True}}}


class TestSnapshotAssembly:
    def test_happy_path_covers_everything(self):
        qa, sm, fr = _stub_sources({"F3E": (100.0, -10.0), "BDM": (50.0, 0.0)})
        snap = qbs.build_snapshot(["F3E", "BDM"], query_accounts=qa, summarize=sm,
                                  freshness=fr, config=_CFG_ON)
        assert snap["covered"] == 2 and snap["expected"] == 2
        assert snap["portfolio"]["cash_net_of_cards"] == 140.0
        assert snap["basis"] == qbs.BALANCE_BASIS

    def test_failed_realm_renders_unknown_never_zero(self):
        qa, sm, fr = _stub_sources({"F3E": (100.0, 0.0), "BDM": (0.0, 0.0)}, fail={"BDM"})
        snap = qbs.build_snapshot(["F3E", "BDM"], query_accounts=qa, summarize=sm,
                                  freshness=fr, config=_CFG_ON)
        assert snap["realms"]["BDM"]["status"] == "error"
        assert snap["realms"]["BDM"]["bank_total"] is None      # NOT 0.0
        assert snap["realms"]["BDM"]["cash_net_of_cards"] is None
        assert snap["covered"] == 1 and snap["expected"] == 2

    def test_one_dead_realm_does_not_blank_the_others(self):
        qa, sm, fr = _stub_sources({"F3E": (100.0, 0.0), "BDM": (0.0, 0.0)}, fail={"BDM"})
        snap = qbs.build_snapshot(["F3E", "BDM"], query_accounts=qa, summarize=sm,
                                  freshness=fr, config=_CFG_ON)
        assert snap["realms"]["F3E"]["bank_total"] == 100.0


class TestPortfolioWithholding:
    def test_withheld_when_a_realm_errored(self):
        qa, sm, fr = _stub_sources({"F3E": (100.0, 0.0), "BDM": (0.0, 0.0)}, fail={"BDM"})
        snap = qbs.build_snapshot(["F3E", "BDM"], query_accounts=qa, summarize=sm,
                                  freshness=fr, config=_CFG_ON)
        assert snap["portfolio"] is None
        assert "BDM" in snap["portfolio_withheld_reason"]

    def test_withheld_when_config_disables_it(self):
        qa, sm, fr = _stub_sources({"F3E": (100.0, 0.0)})
        snap = qbs.build_snapshot(["F3E"], query_accounts=qa, summarize=sm, freshness=fr,
                                  config={"portfolio_total": {"enabled": False}, "realms": {}})
        assert snap["portfolio"] is None

    def test_withheld_when_roll_up_not_verified(self):
        qa, sm, fr = _stub_sources({"F3E": (100.0, 0.0)})
        snap = qbs.build_snapshot(
            ["F3E"], query_accounts=qa, summarize=sm, freshness=fr,
            config={"portfolio_total": {"enabled": True, "roll_up_verified": False}, "realms": {}})
        assert snap["portfolio"] is None
        assert "double-count" in snap["portfolio_withheld_reason"]

    def test_shell_realm_is_excluded_from_the_sum(self):
        qa, sm, fr = _stub_sources({"F3E": (100.0, 0.0), "OSN": (0.0, 0.0)})
        snap = qbs.build_snapshot(["F3E", "OSN"], query_accounts=qa, summarize=sm,
                                  freshness=fr, config=_CFG_ON)
        assert snap["portfolio"]["realms_included"] == ["F3E"]
        assert snap["portfolio"]["shell_realms_excluded"] == ["OSN"]
        assert snap["realms"]["OSN"]["shell"] is True

    def test_shell_realm_carrying_money_withholds_the_total(self):
        """The automatic safety belt: OSN is excluded from the sum BECAUSE it is
        empty. If it ever carries cash, the no-double-count premise is dead and
        the total must withhold rather than quietly drop real money."""
        qa, sm, fr = _stub_sources({"F3E": (100.0, 0.0), "OSN": (5000.0, 0.0)})
        snap = qbs.build_snapshot(["F3E", "OSN"], query_accounts=qa, summarize=sm,
                                  freshness=fr, config=_CFG_ON)
        assert snap["portfolio"] is None
        assert "shell" in snap["portfolio_withheld_reason"]

    def test_withheld_when_a_balance_is_unknown(self):
        def qa(entity):
            return [{"id": "1", "name": "b", "type": "Bank", "balance": None}]
        snap = qbs.build_snapshot(["F3E"], query_accounts=qa, summarize=qc.summarize_accounts,
                                  freshness=lambda e, i: {"date": None, "per_type": {},
                                                          "types_covered": 5, "types_expected": 5,
                                                          "errors": {}},
                                  config=_CFG_ON)
        assert snap["portfolio"] is None
        assert "incomplete" in snap["portfolio_withheld_reason"]


class TestConfigFailClosed:
    def test_missing_config_withholds_the_total(self, tmp_path):
        cfg = qbs.load_config(tmp_path / "nope.yaml")
        assert cfg["portfolio_total"]["enabled"] is False

    def test_corrupt_config_withholds_the_total(self, tmp_path):
        bad = tmp_path / "cfg.yaml"
        bad.write_text("just: [a string\n", encoding="utf-8")
        assert qbs.load_config(bad)["portfolio_total"]["enabled"] is False

    def test_shipped_config_marks_osn_a_shell_and_enables_the_total(self):
        cfg = qbs.load_config()
        assert cfg["portfolio_total"]["enabled"] is True
        assert cfg["portfolio_total"]["roll_up_verified"] is True
        assert cfg["realms"]["OSN"]["shell"] is True


# ── staleness helpers ────────────────────────────────────────────────────────

class TestStaleness:
    def test_age_hours(self):
        now = datetime.datetime(2026, 8, 5, 12, 0, tzinfo=datetime.timezone.utc)
        snap = {"generated_at_utc": "2026-08-05T06:00:00+00:00"}
        assert qbs.snapshot_age_hours(snap, now) == pytest.approx(6.0)

    def test_missing_stamp_is_unknown_not_fresh(self):
        assert qbs.snapshot_age_hours({}) is None
        assert qbs.snapshot_age_hours({"generated_at_utc": "not-a-date"}) is None

    def test_naive_stamp_is_treated_as_utc(self):
        now = datetime.datetime(2026, 8, 5, 12, 0, tzinfo=datetime.timezone.utc)
        assert qbs.snapshot_age_hours({"generated_at_utc": "2026-08-05T11:00:00"}, now) \
            == pytest.approx(1.0)

    def test_txn_age(self):
        assert qbs.txn_age_days("2026-08-01", datetime.date(2026, 8, 5)) == 4

    def test_future_dated_txn_clamps_to_zero(self):
        """QBO legitimately carries future-dated transactions -- F3E held a Deposit
        dated 2026-08-05 when the snapshot ran on 08-04. A negative age would
        render as nonsense."""
        assert qbs.txn_age_days("2026-08-05", datetime.date(2026, 8, 4)) == 0

    def test_absent_or_bad_date_is_unknown(self):
        assert qbs.txn_age_days(None) is None
        assert qbs.txn_age_days("whenever") is None

    def test_stale_threshold_env_tunable(self, monkeypatch):
        monkeypatch.setenv("FINANCE_BANK_TXN_STALE_DAYS", "30")
        assert qbs.stale_txn_days() == 30
        monkeypatch.setenv("FINANCE_BANK_TXN_STALE_DAYS", "garbage")
        assert qbs.stale_txn_days() == qbs.DEFAULT_STALE_TXN_DAYS


class TestPersistence:
    def test_write_then_load_round_trip(self, tmp_path):
        path = tmp_path / "snap.json"
        qbs.write_snapshot({"generated_at_utc": "x", "realms": {}}, path)
        assert qbs.load_snapshot(path)["generated_at_utc"] == "x"

    def test_write_is_atomic_no_tmp_left_behind(self, tmp_path):
        path = tmp_path / "snap.json"
        qbs.write_snapshot({"a": 1}, path)
        assert not list(tmp_path.glob("*.tmp"))

    def test_missing_file_loads_as_none_not_empty_dict(self, tmp_path):
        """None must be distinguishable from 'a snapshot with no realms'."""
        assert qbs.load_snapshot(tmp_path / "absent.json") is None

    def test_corrupt_file_loads_as_none(self, tmp_path):
        path = tmp_path / "snap.json"
        path.write_text("{not json", encoding="utf-8")
        assert qbs.load_snapshot(path) is None


# ── KB containment (design 7 / D-051 finding 12) ─────────────────────────────

class TestSnapshotIsNotKbIngested:
    """Pins the invariant that a `.json` under 01-HJR-Global/accounting/ is
    ingested by NEITHER sweep path. Today that holds by MIME/extension accident;
    these tests make it a checked invariant so a future allowlist widening
    cannot silently start ingesting live finance snapshots."""

    def test_mirror_lands_under_the_accounting_folder(self):
        parts = qbs.MIRROR_RELPATH.parts
        assert parts[0] == "01-HJR-Global"
        assert parts[1] == "accounting"
        assert qbs.MIRROR_RELPATH.suffix == ".json"

    def test_static_sweep_enumerates_only_markdown(self):
        source = (Path(__file__).resolve().parents[1]
                  / "scripts" / "incremental_sync_static.py").read_text(encoding="utf-8")
        assert 'rglob("*.md")' in source, (
            "static sync must enumerate ONLY *.md -- widening this would sweep the "
            "QBO bank snapshot json into the KB as HJRG chunks"
        )
        for pattern in ('rglob("*")', 'rglob("*.json")', 'glob("*.json")'):
            assert pattern not in source

    def test_drive_sweep_extracts_nothing_from_application_json(self, monkeypatch):
        from cora.connectors import drive_sweep

        class _Media:
            def get_media(self, fileId):  # noqa: N803 - google api kwarg
                return object()

        class _Service:
            def files(self):
                return _Media()

        monkeypatch.setattr(drive_sweep, "_retry_execute",
                            lambda req: b'{"bank_total": 12345.67}')
        text = drive_sweep._download_and_extract(
            _Service(), {"id": "f1", "mimeType": "application/json"})
        assert text == "", (
            "application/json must extract to empty -- a live finance snapshot "
            "must never become KB chunks"
        )

    def test_json_is_not_in_the_text_mime_allowlist(self):
        from cora.connectors import drive_sweep
        assert "application/json" not in drive_sweep._TEXT_MIME_TYPES
        assert not "application/json".startswith("text/")
