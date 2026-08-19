"""13WCF M2 / S4 -- category-map discovery + the D-124 opacity gate.

Discovery proposes; Justin confirms. So the pins here are about what discovery
must never propose, and about the one file in this build that is allowed to carry
LEX account names (a LOCAL-ONLY confirm artifact, never the shared map).

The two defects the live 2026-08-06 run caught, both now regression-pinned:
  * `Rents Receivable` proposed for the sheet's *Rent* expense row on the strength
    of the word "rent" -- $180,742.98 of collected income filed as rent expense;
  * bank accounts proposed as spend CATEGORIES, when a bank counterpart is an
    internal move that split_gross already excludes from receipts/disbursements.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

from cora import cashflow_actuals as ca
from cora import cashflow_maps as cm
from cora.tools import qbo_client as qc

_REPO_ROOT = Path(__file__).resolve().parents[1]

VALID_ROWS = {"Rent", "Utilities", "Payroll and Prof Fees", "Direct Hard Costs",
              "Leasehold Improvements", "Large Equipment/Furniture acquisitions",
              "Interest and Principal", "Services", "Interest Income",
              "Contributions/Draws", "Advertising and Marketing", "Other Costs"}
EXPENSE_ROWS = {"Rent", "Utilities", "Payroll and Prof Fees", "Direct Hard Costs",
                "Leasehold Improvements", "Large Equipment/Furniture acquisitions",
                "Interest and Principal", "Advertising and Marketing", "Other Costs"}


# ── D-124: the opacity gate is PREFIX-based, the exclusion list is not ───────

class TestOpacityGate:
    def test_lex_is_opaque(self):
        assert cm.realm_names_are_opaque("LEX") is True

    def test_a_future_lex_sub_realm_is_also_opaque(self):
        """D-124 corollary: an exact-match gate would let `LEX-LLC` free-render
        the very names the gate exists to hide, and would fail SILENTLY."""
        for realm in ("LEX-LLC", "LEX2", "lex-lbhs"):
            assert cm.realm_names_are_opaque(realm) is True, realm

    def test_other_realms_are_not_opaque(self):
        for realm in ("F3E", "HJRG", "OSNGW", "BDM"):
            assert cm.realm_names_are_opaque(realm) is False, realm

    def test_exclusion_list_is_deliberately_exact_not_prefix(self):
        """The asymmetry is load-bearing: prefix-excluding "OSN" would sweep away
        OSNGF/OSNGM/OSNGW/OSNVV, the four operating store realms this extractor
        exists to read. Widening an opacity gate is fail-safe; widening an
        exclusion list deletes real coverage."""
        assert "OSN" in cm.HARD_EXCLUDED_REALMS
        for store in ("OSNGF", "OSNGM", "OSNGW", "OSNVV"):
            assert store not in cm.HARD_EXCLUDED_REALMS, store

    def test_display_name_honours_the_prefix_gate(self):
        mapping = cm.AccountMapping(account_id="530", realm="LEX-LLC",
                                    account_type="Bank",
                                    qbo_account="Trad LLC Main 5490")
        assert "Trad LLC Main" not in mapping.display_name()

    def test_display_name_passes_a_non_opaque_realm_through(self):
        mapping = cm.AccountMapping(account_id="9", realm="F3E",
                                    account_type="Bank",
                                    qbo_account="Tradition F3 8950")
        assert mapping.display_name() == "Tradition F3 8950"


# ── the `filters` gate (D-051 HIGH) ─────────────────────────────────────────

class TestFiltersDoNotConferResolvability:
    """The mechanism advertised as the containment gate was INERT, and using it
    OPENED the realm instead of narrowing it.

    `resolvable` short-circuited on `filters` BEFORE the tab check while no
    consumer ever read `filters` -- so the moment Justin followed the YAML's own
    instruction and supplied account filters, the extractor would have read the
    ENTIRE LEX realm (all five Lex entities) and published it under `tab: None`.
    """

    def _lex(self, **kw):
        base = dict(realm="LEX", tab=None,
                    candidate_tabs=["CF_LLC", "CF_LBHS", "CF_LTS", "CF_LLA_MV",
                                    "CF_LEXCORP"])
        base.update(kw)
        return cm.RealmPairing(**base)

    def test_filters_alone_does_not_resolve(self):
        pairing = self._lex(filters={"accounts": ["530", "531"]})
        assert pairing.resolvable is False

    def test_filters_plus_a_tab_still_does_not_resolve(self):
        """Because nothing APPLIES the filters, a tab plus filters would publish
        the whole realm under that one tab -- the exact mis-attribution the gate
        exists to prevent."""
        pairing = self._lex(tab="CF_LLC", scope_attested=True,
                            filters={"accounts": ["530"]})
        assert pairing.resolvable is False

    def test_the_refusal_says_why_and_what_to_do_instead(self):
        reason = self._lex(filters={"accounts": ["530"]}).refusal_reason
        assert "NOTHING APPLIES" in reason
        assert "scope_attested" in reason
        assert "tab_splits" in reason

    def test_scope_attested_remains_the_supported_path(self):
        assert self._lex(tab="CF_LLC", scope_attested=True).resolvable is True

    def test_a_confirmed_pairing_with_filters_fails_at_LOAD(self, tmp_path):
        """Loudly, not silently: a confirmed-but-unresolvable pair already raises,
        so a well-meaning edit stops the whole job instead of quietly widening
        collection scope."""
        data = {
            "excluded_realms": ["HRLLC", "OSN"],
            "pairs": {"LEX": {"tab": "CF_LLC", "confirmed": True,
                              "scope_attested": True,
                              "filters": {"accounts": ["530"]}}},
        }
        path = tmp_path / "entity.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        with pytest.raises(cm.MapError, match="confirmed but does not resolve"):
            cm.load_entity_map(path)

    def test_the_shipped_map_no_longer_advertises_filters_as_available(self):
        """The YAML told Justin to populate a field that would have opened the
        realm. The instruction has to change with the code."""
        body = cm.ENTITY_MAP_PATH.read_text(encoding="utf-8")
        assert "NOT IMPLEMENTED" in body
        assert "tab_splits" in body

    def test_the_live_lex_row_resolves_only_via_scope_attestation(self):
        """SUPERSEDED 2026-08-18: the LEX realm is the Lexington LLC company
        file (CF_LLC), attested by Harrison. What this class actually guards is
        that `filters` never confers resolvability -- so assert the attestation
        is what carries it, and that adding filters still refuses."""
        pairing = cm.load_entity_map().pairing("LEX")
        assert pairing is not None
        assert pairing.resolvable is True
        assert pairing.scope_attested is True
        assert pairing.filters == {}
        pairing.filters = {"class_ids": ["7"]}
        assert pairing.resolvable is False


# ── category suggestion ──────────────────────────────────────────────────────

def _suggest(name, type_="Expense", inflow=0.0, outflow=100.0):
    return ca.suggest_category(
        {"name": name, "fqn": name, "type": type_}, VALID_ROWS,
        expense_rows=EXPENSE_ROWS, inflow=inflow, outflow=outflow)


class TestSuggestCategory:
    def test_plain_name_match(self):
        assert _suggest("Facilities:Rent") == ("Rent", "name-match-high")

    def test_leasehold_beats_rent(self):
        """Priority order matters: the specific case must sit above the general
        one that would otherwise swallow it."""
        assert _suggest("Leasehold Improvements")[0] == "Leasehold Improvements"

    def test_interest_income_beats_interest(self):
        assert _suggest("Interest Income", inflow=100.0, outflow=0.0)[0] == \
            "Interest Income"

    def test_direction_conflict_suppresses_a_backwards_match(self):
        """THE $180,742.98 CASE. `Rents Receivable` only ever appears opposite
        DEPOSITS -- it is rent COLLECTED. Filing it on the Rent expense row would
        be a large, confident, wrong number."""
        category, confidence = _suggest("Rents Receivable", type_="Accounts Receivable",
                                        inflow=180742.98, outflow=0.0)
        assert category is None
        assert confidence == "direction-conflict"

    def test_outflow_account_is_not_offered_a_receipts_row(self):
        category, confidence = _suggest("Management Fees", inflow=0.0, outflow=9593.47)
        assert (category, confidence) == (None, "direction-conflict")

    def test_bidirectional_row_is_exempt_from_the_veto(self):
        """`Contributions/Draws` says both directions in its own name, and the
        sheet keeps them on one row. A veto that fires on a legitimate case is the
        same failure class it was built to prevent."""
        assert _suggest("Paid-in-Capital", type_="Equity",
                        inflow=0.0, outflow=72000.0)[0] == "Contributions/Draws"
        assert _suggest("Paid-in-Capital", type_="Equity",
                        inflow=92150.0, outflow=0.0)[0] == "Contributions/Draws"

    def test_accrual_liability_counterpart_still_maps(self):
        """Deliberately NOT classification-based: accrual bookkeeping puts a
        LIABILITY opposite most real outflows (a bill payment clears A/P, payroll
        clears Accrued Payroll), so an 'expense rows must be Expense-classified'
        rule would reject the commonest correct case."""
        assert _suggest("Accrued Payroll", type_="Other Current Liability",
                        outflow=61627.89)[0] == "Payroll and Prof Fees"

    def test_mixed_direction_keeps_the_name_signal(self):
        assert _suggest("Facilities:Rent", inflow=50.0, outflow=50.0)[0] == "Rent"

    def test_type_fallback_when_no_keyword_matches(self):
        assert _suggest("Widget Revenue", type_="Income",
                        inflow=100.0, outflow=0.0) == ("Services", "type-match-medium")

    def test_nothing_is_proposed_for_an_unrecognised_account(self):
        assert _suggest("Undeposited Funds", type_="Other Current Asset",
                        inflow=50.0, outflow=50.0) == (None, "unmapped")

    def test_rent_does_not_match_inside_current(self):
        """D-051 MED, verified against the live chart of accounts: substring
        matching proposed the sheet's *Rent* row for "Other Current
        Liabilities:Sales Tax Payable" and "Current Portion of Long Term Debt",
        because "rent" sits inside "cur-rent". Sales-tax remittance and debt
        principal are among the commonest counterparts of a bank outflow in the OSN
        store realms, so these were high-confidence proposals on real money."""
        assert _suggest("Other Current Liabilities:Sales Tax Payable",
                        type_="Other Current Liability", outflow=42000.0)[0] != "Rent"

    def test_debt_needles_outrank_rent(self):
        assert _suggest("Current Portion of Long Term Debt",
                        type_="Long Term Liability",
                        outflow=90000.0)[0] == "Interest and Principal"

    def test_draw_does_not_match_inside_drawer(self):
        """A retail store's daily cash sweep was proposed as owner contributions
        -- and `Contributions/Draws` is exempt from the direction veto, so nothing
        downstream would have caught it."""
        assert _suggest("Cash Drawer", type_="Other Current Asset",
                        inflow=250000.0, outflow=0.0)[0] != "Contributions/Draws"

    def test_real_rent_and_draws_still_match(self):
        """The boundary fix must not cost the true positives."""
        assert _suggest("Facilities:Rent", outflow=74492.24)[0] == "Rent"
        assert _suggest("Equipment Rental", outflow=12000.0)[0] is not None
        assert _suggest("2. Other Equity Activity:Paid-in-Capital", type_="Equity",
                        inflow=92150.0)[0] == "Contributions/Draws"
        assert _suggest("Owner Draw", type_="Equity",
                        outflow=5000.0)[0] == "Contributions/Draws"

    def test_a_row_the_sheet_does_not_carry_is_never_proposed(self):
        """The sheet's row labels are the contract -- inventing one would put
        money on a row nobody can reconcile against."""
        category, _ = ca.suggest_category(
            {"name": "Facilities:Rent", "fqn": "Facilities:Rent", "type": "Expense"},
            valid_rows={"Services"}, expense_rows=set(), outflow=100.0)
        assert category is None


# ── candidate discovery ──────────────────────────────────────────────────────

ACCOUNTS = [
    {"id": "9", "name": "Tradition F3 8950", "fqn": "Cash:Tradition F3 8950",
     "type": "Bank"},
    {"id": "52", "name": "Chase Card", "fqn": "Chase Card", "type": "Credit Card"},
    {"id": "153", "name": "Rent", "fqn": "Facilities:Rent", "type": "Expense"},
    {"id": "160", "name": "Accrued Payroll", "fqn": "Accrued Payroll",
     "type": "Other Current Liability"},
]


def _discover(rows, realm="F3E"):
    return ca.discover_category_candidates(
        realm, rows, ACCOUNTS, VALID_ROWS, expense_rows=EXPENSE_ROWS)


class TestDiscoverCategoryCandidates:
    def test_proposes_counterpart_accounts_busiest_first(self):
        candidates, _ = _discover([
            {"split_account_id": "153", "amount": -100.0},
            {"split_account_id": "160", "amount": -5000.0},
        ])
        assert list(candidates) == ["160", "153"]
        assert candidates["153"]["category"] == "Rent"

    def test_bank_counterpart_is_the_perimeter_not_a_category(self):
        """A bank-to-bank counterpart is an internal move split_gross already keeps
        out of receipts and disbursements; giving it a spend category would file a
        sweep between two of our own accounts as an expense."""
        candidates, skipped = _discover([{"split_account_id": "9", "amount": -500.0}])
        assert candidates == {}
        assert skipped["perimeter_counterpart"] == 1

    def test_card_counterpart_is_also_perimeter(self):
        candidates, skipped = _discover([{"split_account_id": "52", "amount": -500.0}])
        assert candidates == {}
        assert skipped["perimeter_counterpart"] == 1

    def test_multi_line_split_is_counted_not_proposed(self):
        candidates, skipped = _discover([{"split_account_id": None, "amount": -50.0}])
        assert candidates == {}
        assert skipped["multi_line_split"] == 1

    def test_direction_is_recorded_for_the_reviewer(self):
        candidates, _ = _discover([{"split_account_id": "153", "amount": -100.0},
                                   {"split_account_id": "153", "amount": 25.0}])
        entry = candidates["153"]
        assert entry["observed_outflow"] == 100.0
        assert entry["observed_inflow"] == 25.0
        assert entry["observed_rows"] == 2
        assert entry["observed_abs_amount"] == 125.0

    def test_everything_lands_unconfirmed(self):
        """A guess Justin rubber-stamps is worse than a blank."""
        candidates, _ = _discover([{"split_account_id": "153", "amount": -100.0}])
        assert candidates["153"]["confirmed"] is False

    def test_lex_names_never_enter_the_candidate_entry(self):
        """D-124: opaque placeholder, and NO qbo_account field at all."""
        accounts = [{"id": "413", "name": "Program Rent", "fqn": "Program Rent",
                     "type": "Expense"}]
        candidates, _ = ca.discover_category_candidates(
            "LEX", [{"split_account_id": "413", "amount": -734096.79}],
            accounts, VALID_ROWS, expense_rows=EXPENSE_ROWS)
        entry = candidates["413"]
        assert entry["placeholder"] == "LEX acct 413"
        assert "qbo_account" not in entry
        assert "Program Rent" not in repr(entry)

    def test_a_future_lex_sub_realm_is_opaque_too(self):
        accounts = [{"id": "1", "name": "Secret", "fqn": "Secret", "type": "Expense"}]
        candidates, _ = ca.discover_category_candidates(
            "LEX-LBHS", [{"split_account_id": "1", "amount": -1.0}],
            accounts, VALID_ROWS, expense_rows=EXPENSE_ROWS)
        assert "Secret" not in repr(candidates)

    def test_non_lex_realm_carries_the_readable_name(self):
        candidates, _ = _discover([{"split_account_id": "153", "amount": -100.0}])
        assert candidates["153"]["qbo_account"] == "Facilities:Rent"

    def test_unknown_counterpart_account_still_proposed_as_unmapped(self):
        """An account absent from the chart read must not vanish -- it is real
        money the reviewer needs to see."""
        candidates, _ = _discover([{"split_account_id": "777", "amount": -9.0}])
        assert candidates["777"]["confidence"] == "unmapped"


# ── merge ────────────────────────────────────────────────────────────────────

class TestMergeCategoryCandidates:
    def test_adds_new_rows(self):
        merged, counts = ca.merge_category_candidates(
            {"realms": {}}, {"F3E": {"153": {"category": "Rent", "confirmed": False}}})
        assert merged["realms"]["F3E"]["accounts"]["153"]["category"] == "Rent"
        assert counts["added"] == 1

    def test_never_overwrites_a_confirmed_row(self):
        """Discovery re-runs weekly. A confirm a later run could silently revert
        is not a confirm."""
        existing = {"realms": {"F3E": {"accounts": {"153": {
            "category": "Other Costs", "confirmed": True,
            "confirmed_by": "justin"}}}}}
        merged, counts = ca.merge_category_candidates(
            existing, {"F3E": {"153": {"category": "Rent", "confirmed": False}}})
        row = merged["realms"]["F3E"]["accounts"]["153"]
        assert row["category"] == "Other Costs"
        assert row["confirmed_by"] == "justin"
        assert counts["kept_confirmed"] == 1
        assert counts["added"] == 0

    def test_refreshes_an_unconfirmed_row(self):
        existing = {"realms": {"F3E": {"accounts": {"153": {
            "category": None, "confirmed": False, "observed_rows": 1}}}}}
        merged, counts = ca.merge_category_candidates(
            existing, {"F3E": {"153": {"category": "Rent", "confirmed": False,
                                       "observed_rows": 9}}})
        assert merged["realms"]["F3E"]["accounts"]["153"]["observed_rows"] == 9
        assert counts["refreshed"] == 1

    def test_other_realms_are_left_alone(self):
        existing = {"realms": {"BDM": {"accounts": {"1": {"confirmed": True}}}}}
        merged, _ = ca.merge_category_candidates(
            existing, {"F3E": {"153": {"category": "Rent"}}})
        assert merged["realms"]["BDM"]["accounts"]["1"]["confirmed"] is True

    def test_top_level_keys_survive(self):
        """The categories/expense_categories blocks are the contract; a merge that
        dropped them would make the file fail to load."""
        existing = {"categories": {"receipts": ["Services"]},
                    "expense_categories": ["Rent"], "realms": {}}
        merged, _ = ca.merge_category_candidates(
            existing, {"F3E": {"153": {"category": "Rent"}}})
        assert merged["categories"] == {"receipts": ["Services"]}
        assert merged["expense_categories"] == ["Rent"]

    def test_merged_body_still_loads_through_the_validating_loader(self, tmp_path):
        existing = {
            "excluded_realms": ["HRLLC", "OSN"],
            "categories": {"operating_disbursements": ["Rent"]},
            "expense_categories": ["Rent"], "realms": {}}
        merged, _ = ca.merge_category_candidates(existing, {"F3E": {"153": {
            "account_type": "Expense", "category": "Rent", "confirmed": False}}})
        path = tmp_path / "cat.yaml"
        path.write_text(yaml.safe_dump(merged), encoding="utf-8")
        loaded = cm.load_category_map(path)
        assert loaded.mapping("F3E", "153") is not None

    def test_a_cc_liability_mapped_to_an_expense_still_fails_the_loader(self, tmp_path):
        """The cash-perimeter assertion must survive a discovery write: mapping a
        card account to an expense row would double-count every carded dollar."""
        existing = {"excluded_realms": ["HRLLC", "OSN"],
                    "categories": {"operating_disbursements": ["Rent"]},
                    "expense_categories": ["Rent"], "realms": {}}
        merged, _ = ca.merge_category_candidates(existing, {"F3E": {"52": {
            "account_type": "Credit Card", "category": "Rent", "confirmed": False}}})
        path = tmp_path / "cat.yaml"
        path.write_text(yaml.safe_dump(merged), encoding="utf-8")
        with pytest.raises(cm.MapError, match="double-count"):
            cm.load_category_map(path)


# ── the full chart of accounts read ──────────────────────────────────────────

class TestQueryAllAccounts:
    def test_paginates(self, monkeypatch):
        pages = [
            {"Account": [{"Id": str(i), "Name": f"a{i}", "AccountType": "Expense"}
                         for i in range(qc._ACCOUNT_PAGE_SIZE)]},
            {"Account": [{"Id": "last", "Name": "z", "AccountType": "Expense"}]},
        ]
        calls: list[str] = []

        def fake_query(entity, query):
            calls.append(query)
            return pages[len(calls) - 1]
        monkeypatch.setattr(qc, "_query", fake_query)
        out = qc.query_all_accounts("F3E")
        assert len(out) == qc._ACCOUNT_PAGE_SIZE + 1
        assert "STARTPOSITION 201" in calls[1]

    def test_includes_non_perimeter_accounts(self):
        """`query_accounts` narrows to the cash perimeter; discovery needs the
        OTHER side of a transaction, so this one reads everything active."""
        assert "AccountType in" not in qc._ALL_ACCOUNT_QUERY
        assert "Active = true" in qc._ALL_ACCOUNT_QUERY

    def test_unexpected_response_key_refuses(self, monkeypatch):
        """An empty chart of accounts would make every counterpart look unmapped
        and silently gut the whole discovery pass."""
        monkeypatch.setattr(qc, "_query",
                            lambda e, q: {"AccountV2": [{"Id": "1"}], "totalCount": 1})
        with pytest.raises(qc.QboClientError, match="unrecognised key"):
            qc.query_all_accounts("F3E")


# ── the LEX confirm artifact ─────────────────────────────────────────────────

def _load_script():
    spec = importlib.util.spec_from_file_location(
        "run_cashflow_actuals_disc",
        _REPO_ROOT / "scripts" / "run_cashflow_actuals.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestLexConfirmArtifact:
    def test_it_is_written_outside_the_mirrored_tree(self, tmp_path, monkeypatch):
        """This is the ONE file in the build that carries LEX account names. It
        must live where nothing mirrors or ingests it."""
        script = _load_script()
        monkeypatch.setattr(script, "_LEX_CONFIRM_DIR", tmp_path)
        import datetime
        path = script._write_lex_confirm_artifact(
            "LEX",
            [{"id": "530", "name": "Trad LLC Main 5490",
              "fqn": "Trad LLC Main 5490", "type": "Bank"},
             {"id": "413", "name": "Program Rent", "fqn": "Program Rent",
              "type": "Expense"}],
            {"413": {"placeholder": "LEX acct 413", "account_type": "Expense",
                     "category": "Rent", "observed_rows": 3,
                     "observed_abs_amount": 734096.79}},
            datetime.date(2026, 8, 6))
        body = path.read_text(encoding="utf-8")
        assert "LOCAL ONLY" in body
        # Its whole purpose: the real names, for Harrison's DM.
        assert "Program Rent" in body
        assert "LEX acct 413" in body
        # And the bank accounts, which are the mechanism for the 1:N split.
        assert "Trad LLC Main 5490" in body
        assert "scope_attested" in body

    def test_the_confirm_dir_is_the_local_log_tree(self):
        script = _load_script()
        assert script._LEX_CONFIRM_DIR.name == "logs"

    def test_table_cells_are_sanitised_and_pipe_escaped(self):
        """The one surface in the build that renders these names at all is exactly
        where the D-123 sanitizer must not be skipped: a pipe or control character
        in an account name silently mangles the table Harrison confirms from."""
        script = _load_script()
        assert script._cell("Trust | 9021\x00") == r"Trust \| 9021"
        assert script._cell(None) == ""

    def test_the_artifact_is_only_written_under_apply(self, tmp_path, monkeypatch):
        """`--discover` is documented as "prints by default; needs --apply to
        write". That has to be true of THIS file above all, since it is the only
        one carrying LEX account names in plaintext."""
        script = _load_script()
        monkeypatch.setattr(script, "_LEX_CONFIRM_DIR", tmp_path)
        source = (_REPO_ROOT / "scripts" / "run_cashflow_actuals.py").read_text(
            encoding="utf-8")
        # The write site sits inside an --apply branch.
        idx = source.index("_write_lex_confirm_artifact(\n                    realm")
        assert "if args.apply:" in source[idx - 200:idx]

    def test_discovery_refuses_a_realm_with_no_entity_map_pairing(self):
        """The window path refuses an unmapped realm BEFORE its first API call.
        Discovery must match, or it becomes the weaker of the two collection
        boundaries -- a personal entity provisioned under a code the exclusion
        list does not name would have its whole chart of accounts read and its
        names written into a git-tracked file."""
        source = (_REPO_ROOT / "scripts" / "run_cashflow_actuals.py").read_text(
            encoding="utf-8")
        guard = source.index("if pairing is None:")
        first_read = source.index("qc.query_all_accounts(realm)")
        assert guard < first_read
