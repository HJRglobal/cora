"""A5 D-051 remediation -- regression pins for every confirmed review defect.

Five adversarial lenses ran against the branch. Each test below corresponds to a
CONFIRMED finding, and each one FAILED before its fix. Grouped by the lens that
found it so a future reader can see which class of defect each guards.

The recurring shape worth remembering: none of these were caught by the feature
tests, because every one of them is about what happens on a path the happy-case
fixture never visits — a partially-read realm, an untrusted string, a hung mount,
a narrowed CLI run.
"""

from __future__ import annotations

import datetime
import json

import pytest

from cora import finance_close as fc
from cora import inventory_state as inv
from cora import qbo_bank_snapshot as qbs

MONDAY = datetime.date(2026, 8, 3)


# ═══════════════════════════════════════════════════════════════════════════
# COVERAGE-HONESTY LENS
# ═══════════════════════════════════════════════════════════════════════════

def _realm(net=100.0, *, status="ok", shell=False, complete=True,
           bank_unknown=0, cc_unknown=0, newest="2026-08-03",
           f_cov=5, f_exp=5):
    return {
        "status": status, "shell": shell, "error": None,
        "bank_total": net, "cc_total": 0.0, "cash_net_of_cards": net,
        "balances_complete": complete,
        "bank_unknown": bank_unknown, "cc_unknown": cc_unknown,
        "newest_bank_txn_date": newest,
        "freshness_types_covered": f_cov, "freshness_types_expected": f_exp,
    }


def _snap(**realms):
    return {"generated_at_utc": "2026-08-03T14:00:00+00:00",
            "basis": qbs.BALANCE_BASIS, "realms": realms}


def _founder_cut(pack_sections):
    """Run the REAL founder-cut renderer over a pack carrying these sections."""
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "_rfcp", Path(__file__).resolve().parents[1] / "scripts" / "run_finance_close_pack.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    pack = fc.ClosePack(generated_at="2026-08-03", sections=list(pack_sections))
    return module.build_founder_cut(pack)


class TestIncompleteBalancesAreVisible:
    """FINDING 1 (HIGH): a realm with unreadable account balances still counted as
    covered, so is_partial stayed False AND the withheld-total wording matched no
    founder-cut probe -- the section vanished from the cut entirely."""

    def _section(self):
        section, _ = fc.build_bank_section(
            ["F3E", "BDM"],
            fc.Sources(bank_snapshot=lambda: _snap(
                F3E=_realm(100.0, complete=False, bank_unknown=2),
                BDM=_realm(50.0))),
            today=MONDAY)
        return section

    def test_incomplete_realm_does_not_count_as_covered(self):
        section = self._section()
        assert section.covered == 1 and section.expected == 2
        assert section.is_partial is True

    def test_the_row_itself_names_the_gap(self):
        body = "\n".join(self._section().lines)
        assert "INCOMPLETE: 2 account(s) returned no balance" in body
        assert "understates the realm" in body

    def test_the_section_is_visible_in_the_founder_cut(self):
        cut = _founder_cut([self._section()])
        assert "QBO bank" in cut
        assert "every section had full coverage" not in cut

    def test_withheld_total_wording_matches_the_founder_cut_probe(self):
        body = "\n".join(self._section().lines)
        assert "unavailable —" in body


class TestUnknownAccountCountsAreRendered:
    """FINDING 2 (HIGH): bank_unknown / cc_unknown were computed, stored in the
    snapshot, and read by nothing -- so a total short by two accounts rendered
    unmarked."""

    def test_counts_reach_the_rendered_row(self):
        section, _ = fc.build_bank_section(
            ["F3E"],
            fc.Sources(bank_snapshot=lambda: _snap(
                F3E=_realm(50_000.0, complete=False, bank_unknown=2, cc_unknown=1))),
            today=MONDAY)
        assert "3 account(s) returned no balance" in "\n".join(section.lines)

    def test_complete_realm_carries_no_gap_note(self):
        section, _ = fc.build_bank_section(
            ["F3E"], fc.Sources(bank_snapshot=lambda: _snap(F3E=_realm())), today=MONDAY)
        assert "INCOMPLETE" not in "\n".join(section.lines)


class TestFreshnessCoverageIsDisclosed:
    """FINDING 7 (MED): a realm could be FLAGGED stale purely because the txn
    surfaces that would have disproved it were never read."""

    def test_partial_freshness_says_the_date_is_a_floor(self):
        section, _ = fc.build_bank_section(
            ["F3E"],
            fc.Sources(bank_snapshot=lambda: _snap(
                F3E=_realm(newest="2026-07-20", f_cov=2, f_exp=5))),
            today=MONDAY)
        body = "\n".join(section.lines)
        assert "only 2 of 5 txn surfaces read" in body
        assert "floor, not a confirmed latest" in body

    def test_full_freshness_coverage_adds_no_caveat(self):
        section, _ = fc.build_bank_section(
            ["F3E"], fc.Sources(bank_snapshot=lambda: _snap(F3E=_realm())), today=MONDAY)
        assert "txn surfaces read" not in "\n".join(section.lines)


class TestShellRealmsDoNotCreateAPermanentFalsePartial:
    """FINDING 6 (MED): OSN is shell-configured and NOT pack-excluded, so counting
    it in `expected` made forecast_assist report itself partial EVERY week --
    training the reader to ignore the one signal that marks a real gap."""

    # 13WCF M3: the sheet-dual series is no longer read. An injected SNAPSHOT
    # replaces it -- without one these fall through to the live on-disk store
    # and this unit test starts depending on whether this week's S1 job ran.
    SNAPSHOT = {
        "schema_version": 1, "snapshot_date": "2026-08-03",
        "week_ending_weekday": "Friday",
        "tabs": {"CF_SUMMARY": {
            "status": "ok", "post_refresh_suspect": False,
            "last_actual_week_ending": "2026-07-31",
            "forward_week_endings": ["2026-08-07"],
            "series": {"ending_cash": [
                {"week_ending": "2026-08-07", "forecast": 1767089.0,
                 "actual": None, "diff": None, "basis": "forecast"}]},
        }},
    }

    def test_forecast_assist_is_complete_on_a_clean_run(self):
        section, _ = fc.build_forecast_assist_section(
            ["F3E", "BDM", "OSN"],
            fc.Sources(cashflow_snapshot=lambda: self.SNAPSHOT,
                       cashflow_snapshot_dates=lambda: [],
                       cashflow_load_snapshot=lambda d: None,
                       bank_snapshot=lambda: _snap(
                           F3E=_realm(1.0), BDM=_realm(2.0),
                           OSN=_realm(0.0, shell=True))),
            today=MONDAY)
        assert section.covered == 2 and section.expected == 2
        assert section.is_partial is False

    def test_a_real_gap_still_shows_as_partial_and_is_named(self):
        section, _ = fc.build_forecast_assist_section(
            ["F3E", "BDM", "OSN"],
            fc.Sources(cashflow_snapshot=lambda: self.SNAPSHOT,
                       cashflow_snapshot_dates=lambda: [],
                       cashflow_load_snapshot=lambda d: None,
                       bank_snapshot=lambda: _snap(
                           F3E=_realm(1.0), BDM=_realm(0.0, status="error"),
                           OSN=_realm(0.0, shell=True))),
            today=MONDAY)
        assert section.is_partial is True
        body = "\n".join(section.lines)
        assert "Big D Media: unavailable —" in body

    def test_bank_section_also_drops_shells_from_the_denominator(self):
        section, _ = fc.build_bank_section(
            ["F3E", "OSN"],
            fc.Sources(bank_snapshot=lambda: _snap(
                F3E=_realm(1.0), OSN=_realm(0.0, shell=True))),
            today=MONDAY)
        assert section.covered == 1 and section.expected == 1
        assert section.is_partial is False


class TestFutureStampedSnapshot:
    """FINDING 8 (LOW): a future stamp failed the `> MAX_AGE` test and took the
    FRESH branch, rendering "~-128h ago" as current."""

    def test_future_stamp_is_unknown_age_not_fresh(self):
        future = (datetime.datetime.now(datetime.timezone.utc)
                  + datetime.timedelta(hours=48)).isoformat()
        section, _ = fc.build_bank_section(
            ["F3E"],
            fc.Sources(bank_snapshot=lambda: {
                "generated_at_utc": future, "realms": {"F3E": _realm()}}),
            today=MONDAY)
        first = section.lines[0]
        assert ":warning:" in first
        assert "FUTURE" in first
        assert "-" not in first.split("~")[-1][:4] if "~" in first else True


class TestPartialChannelTotalsAreDisclosed:
    """FINDING 3 (HIGH): a channel where SOME skus read printed a bare total, so a
    SKU that vanished from the feed looked identical to one holding zero."""

    SKU_MAP = {
        "channels": {"amazon_fba": "Amazon FBA", "dtc_3pl": "DTC 3PL"},
        "skus": {"A": {"display_name": "A"}, "B": {"display_name": "B"},
                 "C": {"display_name": "C"}},
    }

    def test_partial_channel_says_how_many_skus_it_read(self):
        loads = {
            "shopify": inv.SourceLoad("shopify", None, "missing"),
            "channels": inv.SourceLoad("channels", {"channels": {"amazon_fba": {
                "status": "ok", "skus": {"A": 800, "B": 400}}}}, "ok"),
            "manual": inv.SourceLoad("manual", None, "missing"),
        }
        merged = inv.merge(self.SKU_MAP, loads)
        line = inv.render_channel_summary(merged, self.SKU_MAP)
        assert "Amazon FBA 1,200" in line
        assert "2 of 3 SKUs read" in line

    def test_a_fully_read_channel_carries_no_partial_note(self):
        loads = {
            "shopify": inv.SourceLoad("shopify", None, "missing"),
            "channels": inv.SourceLoad("channels", {"channels": {"amazon_fba": {
                "status": "ok", "skus": {"A": 1, "B": 2, "C": 3}}}}, "ok"),
            "manual": inv.SourceLoad("manual", None, "missing"),
        }
        line = inv.render_channel_summary(inv.merge(self.SKU_MAP, loads), self.SKU_MAP)
        assert "SKUs read" not in line


# ═══════════════════════════════════════════════════════════════════════════
# EGRESS / PHI LENS
# ═══════════════════════════════════════════════════════════════════════════

_SKU_MAP = {
    "channels": {"amazon_fba": "Amazon FBA", "manual": "Manual count",
                 "dtc_3pl": "DTC 3PL"},
    "skus": {"PURE-Original": {"display_name": "F3 PURE Original", "line": "Pure"}},
}


def _loads(shopify=None, channels=None, manual=None):
    return {
        "shopify": inv.SourceLoad("shopify", shopify, "ok" if shopify else "missing"),
        "channels": inv.SourceLoad("channels", channels, "ok" if channels else "missing"),
        "manual": inv.SourceLoad("manual", manual, "ok" if manual else "missing"),
    }


class TestUntrustedStoreTextCannotCarryLinksOrSourceNames:
    """FINDING 2/3 (HIGH, egress lens): scrub() stripped Slack control chars but
    NOT urls or vendor tokens. The tool is a VERBATIM_TABLE_TOOL, so format_reply
    is bypassed, and egress redacts bare URLs only for an allowlist of hosts -- so
    an arbitrary URL typed into the Airtable location field reached an F3E channel
    as a live clickable link signed by Cora."""

    @pytest.mark.parametrize("hostile", [
        "Pay now https://evil.example/pay",
        "see www.evil.example/x",
        "shopify token expired",
        "check Seller Central",
        "Seller Center outage",
        "polar dashboard down",
    ])
    def test_scrub_neutralises_links_and_source_names(self, hostile):
        out = inv.scrub(hostile)
        assert "http" not in out and "www." not in out
        for banned in ("shopify", "seller central", "seller center", "polar"):
            assert banned not in out.lower()

    def test_manual_count_location_is_neutralised_end_to_end(self):
        manual = {"counts": [{"sku": "PURE-Original", "count": 12,
                              "location": "Pay now https://evil.example/pay"}]}
        body = "\n".join(inv.render_rows(inv.merge(_SKU_MAP, _loads(manual=manual)), _SKU_MAP))
        assert "evil.example" not in body
        assert "http" not in body
        # The note survives as neutralized text, so the count stays attributable
        # to whoever entered it. (The render re-scrubs, which also strips the
        # "[link]" brackets -- the invariant is that no URL reaches Slack.)
        assert "Manual count 12 (Pay now link)" in body

    def test_block_status_is_neutralised_end_to_end(self):
        sweep = {"channels": {"amazon_fba": {
            "status": "shopify token expired see https://evil.example/x", "skus": {}}}}
        body = "\n".join(inv.render_rows(inv.merge(_SKU_MAP, _loads(channels=sweep)), _SKU_MAP))
        assert "evil.example" not in body
        assert "shopify" not in body.lower()

    def test_unmapped_sku_key_is_neutralised(self):
        sweep = {"channels": {"amazon_fba": {
            "status": "ok", "skus": {"EVIL https://evil.example/x": 3}}}}
        body = "\n".join(inv.render_rows(inv.merge(_SKU_MAP, _loads(channels=sweep)), _SKU_MAP))
        assert "evil.example" not in body

    def test_as_of_stamp_is_neutralised(self):
        manual = {"as_of_utc": "https://evil.example/as-of",
                  "counts": [{"sku": "PURE-Original", "count": 1}]}
        merged = inv.merge(_SKU_MAP, _loads(manual=manual))
        assert "evil.example" not in "\n".join(inv.render_rows(merged, _SKU_MAP))

    def test_a_labelled_slack_link_cannot_survive(self):
        """`<url|label>` is the sanctioned citation form egress PROTECTS, so it
        must be destroyed here rather than downstream."""
        out = inv.scrub("<https://evil.example|Reset your password>")
        assert "evil.example" not in out
        assert "<" not in out and ">" not in out


class TestSkuFilterEcho:
    """FINDING 1/4 (HIGH): the no-match message echoed user text verbatim, and
    egress deliberately PRESERVES `<...>` -- so any channel member could make Cora
    post an @channel ping or an attacker-labelled hyperlink."""

    HARRISON = "U0B2RM2JYJ1"

    def _call(self, sku_filter):
        from cora.tools import tool_dispatch as td
        return td._tool_f3e_channel_inventory(
            self.HARRISON, "F3E",
            {"_channel_name": "f3e-leadership", "sku_filter": sku_filter})

    @pytest.mark.parametrize("hostile", [
        "<!channel>", "<!here>", "<@U0B2RM2JYJ1>",
        "<https://evil.example|Reset your password>", "https://evil.example/x",
    ])
    def test_hostile_filters_cannot_round_trip(self, hostile):
        out = self._call(hostile)
        assert "<" not in out and ">" not in out
        assert "evil.example" not in out

    @pytest.mark.parametrize("vendor", ["Shopify", "Seller Central", "Polar"])
    def test_source_names_are_not_echoed_back(self, vendor):
        assert vendor.lower() not in self._call(vendor).lower()

    def test_an_ordinary_miss_still_reads_helpfully(self):
        out = self._call("Gatorade")
        assert "Gatorade" in out
        assert "don't have a SKU or product line" in out


class TestMirrorOmitsAccountDetail:
    """FINDING 1 (HIGH, both lenses): the Drive mirror lands in a folder Justin and
    Hayden work in, and carried every realm's ACCOUNT NAMES -- including LEX's."""

    def test_mirror_payload_drops_the_accounts_array(self):
        import importlib.util
        from pathlib import Path
        spec = importlib.util.spec_from_file_location(
            "_rqbs", Path(__file__).resolve().parents[1] / "scripts" / "run_qbo_bank_snapshot.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        snapshot = {"realms": {"LEX": {
            "status": "ok", "cash_net_of_cards": 1.0,
            "accounts": [{"name": "Due from Jane Smith", "balance": 1.0}]}}}
        payload = module._mirror_payload(snapshot)
        assert "accounts" not in payload["realms"]["LEX"]
        assert "Jane Smith" not in json.dumps(payload)
        assert payload["realms"]["LEX"]["cash_net_of_cards"] == 1.0
        # The local copy is untouched.
        assert "accounts" in snapshot["realms"]["LEX"]


class TestExcludedRealmsAreNeverSwept:
    """FINDING 1 (HIGH): the snapshot enumerated all 11 realms and mirrored HR LLC
    -- Harrison's PERSONAL books -- into the shared accounting folder, and folded
    it into the portfolio total."""

    def test_shipped_config_excludes_hrllc(self):
        assert "HRLLC" in qbs.excluded_realms()

    def test_unreadable_config_still_excludes_it(self, tmp_path):
        bad = tmp_path / "cfg.yaml"
        bad.write_text("broken: [\n", encoding="utf-8")
        assert "HRLLC" in qbs.excluded_realms(qbs.load_config(bad))

    def test_config_without_the_key_still_excludes_it(self):
        assert "HRLLC" in qbs.excluded_realms({"portfolio_total": {}, "realms": {}})


class TestNameOpaqueRealmMatching:
    """FINDING 6 (LOW): an exact-string gate is the wrong shape for a guard whose
    premise is that a human cannot spot a bare person name in an account title."""

    @pytest.mark.parametrize("realm", ["LEX", "lex", "LEX-LLC", "LEXLLC", "LEX-LTS", "Lex"])
    def test_lex_variants_are_all_opaque(self, realm):
        assert fc.is_name_opaque_realm(realm) is True

    @pytest.mark.parametrize("realm", ["F3E", "BDM", "HJRG", "OSNGW", "", "HRLLC"])
    def test_non_lex_realms_are_not(self, realm):
        assert fc.is_name_opaque_realm(realm) is False

    def test_a_lex_subentity_realm_gets_placeholders(self):
        bs = {"Rows": {"Row": [{"type": "Data", "ColData": [
            {"value": "Due from Jane Smith", "id": "9"}, {"value": "1500.00"}]}]}}
        section, _ = fc.build_intercompany_section(
            ["LEX-LLC"], fc.Sources(balance_sheet=lambda e, a: bs), today=MONDAY)
        body = "\n".join(section.lines)
        assert "Jane Smith" not in body
        assert "candidate account #1" in body


class TestConfirmedPairNameWithheldForOpaqueRealms:
    """FINDING 7a (LOW): the confirmed-pair branch rendered its YAML label
    free-form for any realm, relying on hand-authoring discipline."""

    def test_pair_touching_lex_withholds_its_label(self, monkeypatch):
        bs = {
            "LEX": {"Rows": {"Row": [{"type": "Data", "ColData": [
                {"value": "Due from Jane Smith", "id": "1"}, {"value": "100.00"}]}]}},
            "HJRG": {"Rows": {"Row": [{"type": "Data", "ColData": [
                {"value": "Due to Lexington", "id": "2"}, {"value": "-100.00"}]}]}},
        }
        monkeypatch.setattr(fc, "load_intercompany_map", lambda *a, **k: {"pairs": [{
            "name": "Jane Smith settlement", "confirmed": True, "opposite_signs": True,
            "left": {"entity": "LEX", "account_id": "1"},
            "right": {"entity": "HJRG", "account_id": "2"}}]})
        section, _ = fc.build_intercompany_section(
            ["LEX", "HJRG"], fc.Sources(balance_sheet=lambda e, a: bs[e]), today=MONDAY)
        body = "\n".join(section.lines)
        assert "Jane Smith" not in body
        assert "name withheld" in body


class TestCashSectionHonoursThePackExclusion:
    """FINDING 7b (LOW): build_pack's comment claims the exclusion is applied once
    and governs every section -- but the cash section got the UNFILTERED list.
    HR LLC was kept out only by the accident of lacking a sheet mapping."""

    def test_an_excluded_entity_is_never_cross_checked(self, monkeypatch):
        monkeypatch.setitem(fc.QBO_TO_SHEET_ENTITY, "HRLLC", "HRLLC")
        section, snap = fc.build_cash_section(
            ["F3E", "HRLLC"],
            fc.Sources(
                cash_closing=lambda e: {
                    "closing": 999_999.0 if e == "HRLLC" else 1.0,
                    "is_actual": True, "week_label": "Week of 7-31",
                    "stale": False, "age_days": 3},
                balance_sheet=lambda e, a: {"Rows": {"Row": [{
                    "type": "Section",
                    "Header": {"ColData": [{"value": "Bank Accounts"}]},
                    "Summary": {"ColData": [{"value": "Total"}, {"value": "1.00"}]}}]}}),
            today=MONDAY)
        assert "HRLLC" not in snap
        assert "999,999" not in "\n".join(section.lines)
        assert "HR LLC" not in "\n".join(
            ln for ln in section.lines if not ln.startswith("_"))


class TestFinanceWorksheetKbExclusionCoversBothSweeps:
    """FINDING 5 (MED): the segment predicate covers static_md (whose source_id IS
    the path) but drive_sweep stores a bare file id and NO path, and
    sweep_founders_os walks 01-HJR-Global."""

    def test_title_predicate_catches_the_drive_sweep_door(self):
        from cora.kb_exclusions import is_finance_worksheet_title
        assert is_finance_worksheet_title("2026-08-05_fndr_forecast-assist.md")
        assert is_finance_worksheet_title("FORECAST-ASSIST-2026-08-05.md")

    def test_title_predicate_is_narrow(self):
        from cora.kb_exclusions import is_finance_worksheet_title
        assert not is_finance_worksheet_title("2026-08-05_fndr_close-pack.md")
        assert not is_finance_worksheet_title("")

    def test_both_predicates_are_wired_at_the_store_chokepoint(self):
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / "src" / "cora"
                  / "knowledge_base" / "store.py").read_text(encoding="utf-8")
        assert "is_finance_worksheet_path(doc.source_id)" in source
        assert "is_finance_worksheet_title(doc.title)" in source


# ═══════════════════════════════════════════════════════════════════════════
# INTEGRATION-REALISM LENS
# ═══════════════════════════════════════════════════════════════════════════

class TestFreshnessFailureDoesNotDiscardBalances:
    """FINDING 2 (MED): QboAuthError is a SIBLING of QboClientError, not a
    subclass, so an auth failure (including a token-lock timeout) escaped the
    per-type fail-soft and marked the whole realm errored -- throwing away
    balances that had already been read successfully."""

    def test_auth_error_in_freshness_keeps_the_balances(self):
        from cora.connectors.qbo_oauth import QboAuthError

        def boom(entity, ids):
            raise QboAuthError("token lock timeout")

        block = qbs.build_realm(
            "F3E",
            query_accounts=lambda e: [
                {"id": "1", "name": "b", "type": "Bank", "balance": 100.0}],
            summarize=lambda a: {
                "bank_count": 1, "cc_count": 0, "bank_total": 100.0, "cc_total": 0.0,
                "cash_net_of_cards": 100.0, "bank_unknown": 0, "cc_unknown": 0,
                "balances_complete": True},
            freshness=boom)
        assert block["status"] == "ok"
        assert block["bank_total"] == 100.0
        assert block["newest_bank_txn_date"] is None
        assert "freshness" in block["freshness_errors"]

    def test_account_read_failure_still_errors_the_realm(self):
        block = qbs.build_realm(
            "F3E",
            query_accounts=lambda e: (_ for _ in ()).throw(RuntimeError("down")),
            summarize=lambda a: {}, freshness=lambda e, i: {})
        assert block["status"] == "error"
        assert block["bank_total"] is None

    def test_freshness_catches_non_qbo_exceptions_per_type(self, monkeypatch):
        from cora.tools import qbo_client as qc
        from cora.connectors.qbo_oauth import QboAuthError

        def q(entity, query):
            if "Purchase" in query:
                raise QboAuthError("lock timeout")
            return {}

        monkeypatch.setattr(qc, "_query", q)
        out = qc.newest_bank_side_txn_date("F3E")
        assert "Purchase" in out["errors"]
        assert out["types_covered"] == out["types_expected"] - 1


class TestFreshnessCountsOnlyBankSideActivity:
    """FINDING 1 (HIGH, finance lens): `Purchase` was counted unfiltered, but a
    QBO Purchase with PaymentType=CreditCard never touches a bank account.
    Verified live 2026-08-05: LEX's newest Purchase sat on `Divvy Card Main`, and
    40 of its newest 60 Purchases were card-side. On a card-heavy realm that made
    the date wrong AND meant the staleness flag could never fire -- daily card
    spend kept it fresh forever while the bank feed sat dead."""

    def _rows(self, monkeypatch, rows_by_type):
        from cora.tools import qbo_client as qc

        def fake_query(entity, query):
            for typ, rows in rows_by_type.items():
                if f"from {typ} " in query:
                    return {typ: rows}
            return {}

        monkeypatch.setattr(qc, "_query", fake_query)
        return qc

    def test_card_side_purchase_does_not_count_as_bank_activity(self, monkeypatch):
        qc = self._rows(monkeypatch, {"Purchase": [
            {"TxnDate": "2026-08-04", "AccountRef": {"value": "99"}},   # a card
            {"TxnDate": "2026-07-31", "AccountRef": {"value": "9"}},    # the bank
        ]})
        out = qc.newest_bank_side_txn_date("LEX", bank_account_ids={"9"})
        assert out["per_type"]["Purchase"] == "2026-07-31"
        assert out["date"] == "2026-07-31"

    def test_an_all_card_realm_reports_unknown_not_a_false_fresh_date(self, monkeypatch):
        qc = self._rows(monkeypatch, {"Purchase": [
            {"TxnDate": "2026-08-04", "AccountRef": {"value": "99"}},
        ]})
        out = qc.newest_bank_side_txn_date("LEX", bank_account_ids={"9"})
        assert out["per_type"]["Purchase"] is None

    def test_transfer_matches_either_side(self, monkeypatch):
        qc = self._rows(monkeypatch, {"Transfer": [
            {"TxnDate": "2026-08-02", "FromAccountRef": {"value": "99"},
             "ToAccountRef": {"value": "9"}},
        ]})
        out = qc.newest_bank_side_txn_date("LEX", bank_account_ids={"9"})
        assert out["per_type"]["Transfer"] == "2026-08-02"

    def test_billpayment_nested_bank_account_ref_matches(self, monkeypatch):
        qc = self._rows(monkeypatch, {"BillPayment": [
            {"TxnDate": "2026-08-03",
             "CheckPayment": {"BankAccountRef": {"value": "9"}}},
        ]})
        out = qc.newest_bank_side_txn_date("LEX", bank_account_ids={"9"})
        assert out["per_type"]["BillPayment"] == "2026-08-03"

    def test_billpayment_on_a_card_does_not_count(self, monkeypatch):
        qc = self._rows(monkeypatch, {"BillPayment": [
            {"TxnDate": "2026-08-03",
             "CreditCardPayment": {"CCAccountRef": {"value": "99"}}},
        ]})
        out = qc.newest_bank_side_txn_date("LEX", bank_account_ids={"9"})
        assert out["per_type"]["BillPayment"] is None

    def test_rows_exposing_no_account_ref_fall_back_and_say_so(self, monkeypatch):
        """FAIL-SAFE: losing the signal entirely would be worse than an imprecise
        one, so an unexpected schema falls back to the unfiltered date and reports
        the type as unfiltered so the consumer can label it."""
        qc = self._rows(monkeypatch, {"Purchase": [{"TxnDate": "2026-08-04"}]})
        out = qc.newest_bank_side_txn_date("LEX", bank_account_ids={"9"})
        assert out["per_type"]["Purchase"] == "2026-08-04"
        assert "Purchase" in out["unfiltered_types"]

    def test_future_dated_rows_never_win_the_max(self, monkeypatch):
        """FINDING 4 (MED): clamping a future age to 0 turned a detectable anomaly
        into the strongest possible 'current' signal -- a postdated deposit dated
        five months out suppressed the staleness flag for the whole period."""
        qc = self._rows(monkeypatch, {"Purchase": [
            {"TxnDate": "2026-12-31", "AccountRef": {"value": "9"}},
            {"TxnDate": "2026-06-01", "AccountRef": {"value": "9"}},
        ]})
        out = qc.newest_bank_side_txn_date("LEX", bank_account_ids={"9"})
        assert out["per_type"]["Purchase"] == "2026-06-01"

    def test_every_type_empty_is_reported_as_suspicious_not_clean(self, monkeypatch):
        """QBO was observed returning an EMPTY QueryResponse (not an error) for
        every transaction type while Account queries kept working. Reporting that
        as full coverage would be a silent blind spot."""
        qc = self._rows(monkeypatch, {})
        out = qc.newest_bank_side_txn_date("LEX", bank_account_ids={"9"})
        assert out["date"] is None
        assert out["types_covered"] == 0
        assert "_all_types_empty" in out["errors"]


class TestConfirmedPairLookupSurvivesARename:
    """FINDING 7 (LOW): _pair_balance searched only the PATTERN-MATCHED candidate
    set, so a renamed account left `found` and its stable id could never be
    located -- defeating the documented reason account_id is the key."""

    def test_a_renamed_account_is_still_found_by_id(self, monkeypatch):
        bs = {
            "HJRG": {"Rows": {"Row": [{"type": "Data", "ColData": [
                {"value": "Intercompany Clearing", "id": "1"}, {"value": "100.00"}]}]}},
            # Renamed so it no longer matches any intercompany pattern.
            "F3E": {"Rows": {"Row": [{"type": "Data", "ColData": [
                {"value": "2002 IC Clearing", "id": "2"}, {"value": "-100.00"}]}]}},
        }
        monkeypatch.setattr(fc, "load_intercompany_map", lambda *a, **k: {"pairs": [{
            "name": "HJRG <-> F3E", "confirmed": True, "opposite_signs": True,
            "left": {"entity": "HJRG", "account_id": "1"},
            "right": {"entity": "F3E", "account_id": "2"}}]})
        section, _ = fc.build_intercompany_section(
            ["HJRG", "F3E"], fc.Sources(balance_sheet=lambda e, a: bs[e]), today=MONDAY)
        body = "\n".join(section.lines)
        assert "in balance" in body
        assert "unavailable —" not in body


class TestPartialSweepCannotSelfCertify:
    """FINDING 4 (MED): `expected` used the REQUESTED set, so an `--entities F3E`
    run wrote a one-realm file carrying covered == expected and a portfolio
    block, overwriting the daily snapshot with no marker that it was partial."""

    def _sources(self):
        return {
            "query_accounts": lambda e: [
                {"id": "1", "name": "b", "type": "Bank", "balance": 10.0}],
            "summarize": lambda a: {
                "bank_count": 1, "cc_count": 0, "bank_total": 10.0, "cc_total": 0.0,
                "cash_net_of_cards": 10.0, "bank_unknown": 0, "cc_unknown": 0,
                "balances_complete": True},
            "freshness": lambda e, i: {"date": "2026-08-03", "per_type": {},
                                       "types_covered": 5, "types_expected": 5,
                                       "errors": {}},
        }

    CFG = {"portfolio_total": {"enabled": True, "roll_up_verified": True}, "realms": {}}

    def test_narrowed_sweep_records_the_real_denominator(self):
        snap = qbs.build_snapshot(["F3E"], config=self.CFG,
                                  full_scope=["F3E", "BDM", "HJRG"], **self._sources())
        assert snap["expected"] == 3
        assert snap["partial_sweep"] is True

    def test_narrowed_sweep_withholds_the_portfolio_total(self):
        snap = qbs.build_snapshot(["F3E"], config=self.CFG,
                                  full_scope=["F3E", "BDM"], **self._sources())
        assert snap["portfolio"] is None
        assert "partial sweep" in snap["portfolio_withheld_reason"]

    def test_a_full_sweep_is_not_marked_partial(self):
        snap = qbs.build_snapshot(["F3E", "BDM"], config=self.CFG,
                                  full_scope=["F3E", "BDM"], **self._sources())
        assert snap["partial_sweep"] is False
        assert snap["portfolio"] is not None


class TestMountReadsUseAnInteractiveBudget:
    """FINDING 2 (HIGH, tool lens): load_source inherited drive_io's SCHEDULED-job
    defaults (10s timeout, 90s retry). One call measured 98s against a hung mount;
    the tool makes three, so the 12s tool timeout fired first and the user got a
    generic timeout instead of the honest all-UNKNOWN render -- and the long read
    tripped drive_io's PROCESS-WIDE breaker, hurting every other user."""

    def test_reads_pass_a_short_timeout_and_no_retry(self):
        seen: list[dict] = []

        class Reader:
            def exists(self, path, **kw):
                seen.append(kw)
                return False

            def read_text(self, path, **kw):  # pragma: no cover - not reached
                seen.append(kw)
                return "{}"

        inv.load_source("shopify", reader=Reader())
        assert seen and seen[0]["timeout"] <= 5.0
        assert seen[0]["retry_seconds"] == 0.0

    def test_budget_is_well_inside_the_tool_timeout(self):
        from cora.tools import tool_dispatch as td
        worst_case = len(inv.STORE_FILES) * 2 * inv._MOUNT_TIMEOUT_SEC
        assert worst_case < td._TOOL_TIMEOUTS["f3e_channel_inventory"]

    def test_a_reader_without_kwargs_still_works(self):
        """Belt: a simpler test double must not break on the kwargs."""
        class Old:
            def exists(self, path):
                return False
        assert inv.load_source("manual", reader=Old()).status == "missing"


class TestUnparseableIsDistinctFromUnknown:
    """FINDING 4 (MED, tool lens): the render carried a `caveat != ""` conjunct, so
    UNPARSEABLE collapsed into UNKNOWN on the four channels without caveats --
    including both marketplace lanes, where nobody can check by hand."""

    @pytest.mark.parametrize("channel", ["office", "dtc_3pl", "amazon_fba", "walmart_wfs"])
    def test_garbage_renders_unparseable_on_every_channel(self, channel):
        count = inv.ChannelCount(channel, None, None, "", unparseable=True)
        assert inv.render_units(count) == "UNPARSEABLE"

    def test_absent_still_renders_unknown(self):
        assert inv.render_units(inv.ChannelCount("amazon_fba", None, None, "")) == "UNKNOWN"

    def test_the_two_signals_differ_end_to_end(self):
        sweep = {"channels": {"amazon_fba": {
            "status": "ok", "skus": {"PURE-Original": "not-a-number"}}}}
        body = "\n".join(inv.render_rows(inv.merge(_SKU_MAP, _loads(channels=sweep)), _SKU_MAP))
        assert "Amazon FBA UNPARSEABLE" in body


class TestSynthesisAlwaysEmitsTheLine:
    """FINDING 3 (MED): the synthesis appended nothing when the merge produced no
    rows, so the section vanished rather than saying it had nothing -- and absence
    of a line reads as 'nothing to flag'."""

    def test_empty_merge_still_emits_a_line(self, monkeypatch):
        from cora import channel_synthesis as cs

        class _Empty:
            rows: list = []

        monkeypatch.setattr(inv, "merge", lambda *a, **k: _Empty())
        monkeypatch.setattr(cs, "_az_today", lambda: MONDAY, raising=False)
        out = cs.gather_f3e_ecom(today=MONDAY)
        assert any("Cross-channel inventory" in ln for ln in out["lines"])


class TestDashboardIndexListsTheNewReader:
    """FINDING 6 (LOW): the tool was reachable but invisible to the index, so
    nobody could discover it."""

    def test_it_is_in_the_index(self):
        from cora.tools import tool_dispatch as td
        assert td._DASH_CHANNEL_INVENTORY in td._DASH_INDEX

    def test_it_shows_up_in_an_f3e_channel(self):
        from cora.tools import tool_dispatch as td
        out = td._tool_cowork_dashboards_index(
            "U0B2RM2JYJ1", "F3E", {"_channel_name": "f3e-leadership"})
        assert "across every sales channel" in out


class TestPromptDisambiguatesAllFourInventoryTools:
    """FINDING 5 (MED): the new routing block contrasted ONE sibling while four
    exist, and two pre-existing routes now overlapped -- a live regression risk
    while the cross-channel store is still empty."""

    def test_every_inventory_tool_is_named(self):
        from pathlib import Path
        prompt = (Path(__file__).resolve().parents[1] / "design" / "system-prompts"
                  / "f3e.md").read_text(encoding="utf-8")
        block = prompt.split("## Cross-channel inventory")[1].split("##")[0]
        for tool in ("f3e_channel_inventory", "f3e_inventory_by_location",
                     "f3e_inventory_pulse", "f3e_shopify_inventory"):
            assert tool in block, f"{tool} missing from the routing block"

    def test_it_warns_against_substituting_a_number_for_unknown(self):
        from pathlib import Path
        prompt = (Path(__file__).resolve().parents[1] / "design" / "system-prompts"
                  / "f3e.md").read_text(encoding="utf-8")
        assert "do NOT silently substitute" in prompt
        assert "do NOT call it zero" in prompt
