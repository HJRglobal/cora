"""13WCF M2 / S2 -- the QBO layer under the weekly bank-cash actuals.

Every pin here is a defect this build actually hit against live QBO on
2026-08-05, or the mechanism that catches it. A green suite hid all of them:

  * `select * from CreditCardPayment` returns rows under `CreditCardPaymentTxn`,
    so a name-keyed lookup reads six real bank->card payments as ZERO activity
    (-$11,950.34 of OSNVV outflow in one week).
  * The monthly intercompany triple posts a `Purchase` whose HEADER is a
    credit-card clearing account and whose LINE hits a bank account. A
    header-only perimeter misses the cash leg ($16,471.82 LEX / $3,079.72 F3E /
    $2,810.13 HJRG in one week).
  * GL section header ids are not trustworthy -- filtering to a CHILD account
    returns a wrapper section stamped with the REQUESTED id whose total repeats
    the child's, so summing section totals double-counts.

The GL-vs-recompute residual was $0.00 across 36 realm-weeks once both were
fixed; these tests keep it that way without needing the network.
"""

from __future__ import annotations

import pytest

from cora.tools import qbo_client as qc


# ── QueryResponse key extraction ─────────────────────────────────────────────

class TestQueryRows:
    def test_normal_entity_key(self):
        page = {"Purchase": [{"Id": "1"}], "maxResults": 1}
        rows, unexpected = qc.query_rows(page, "Purchase")
        assert [r["Id"] for r in rows] == ["1"]
        assert unexpected is None

    def test_credit_card_payment_alias_is_honoured(self):
        """THE $11,950.34 BUG. The entity is queried as CreditCardPayment but QBO
        answers under CreditCardPaymentTxn; page.get(entity) is None, which reads
        as zero activity with no error."""
        page = {"CreditCardPaymentTxn": [{"Id": "1005", "Amount": 5983.53}],
                "totalCount": 1}
        rows, unexpected = qc.query_rows(page, "CreditCardPayment")
        assert len(rows) == 1
        assert unexpected is None

    def test_genuinely_empty_response_is_not_an_error(self):
        """A one-week window legitimately holds no Transfers. Empty must stay
        empty -- treating it as suspicious would fire on the normal cadence."""
        rows, unexpected = qc.query_rows({}, "Transfer")
        assert rows == []
        assert unexpected is None

    def test_meta_only_response_is_not_an_error(self):
        rows, unexpected = qc.query_rows({"maxResults": 0, "startPosition": 1}, "Deposit")
        assert (rows, unexpected) == ([], None)

    def test_unrecognised_key_reports_rather_than_reading_as_zero(self):
        """The generalisation of the CreditCardPayment trap: if QBO renames a key
        again, the window must render UNKNOWN, not $0 of activity."""
        page = {"DepositTxnV2": [{"Id": "9"}], "totalCount": 1}
        rows, unexpected = qc.query_rows(page, "Deposit")
        assert rows == []
        assert unexpected == "DepositTxnV2"

    def test_single_object_response_is_wrapped(self):
        rows, _ = qc.query_rows({"Purchase": {"Id": "1"}}, "Purchase")
        assert len(rows) == 1


# ── posting table ────────────────────────────────────────────────────────────

class TestPostingTable:
    def test_every_header_entry_is_a_path_sign_pair(self):
        """One unambiguous shape. The first cut encoded Transfer's two opposite
        ends differently from the single-ended types, and the shape probe that
        told them apart was itself ambiguous -- it crashed on a real query."""
        for txn_type, spec in qc._FLOW_POSTINGS.items():
            for entry in spec["header"]:
                assert isinstance(entry, tuple) and len(entry) == 2, txn_type
                path, sign = entry
                assert isinstance(path, str) and sign in (-1, +1), txn_type

    def test_transfer_ends_carry_opposite_signs(self):
        signs = dict(qc._FLOW_POSTINGS["Transfer"]["header"])
        assert signs["FromAccountRef"] == -1
        assert signs["ToAccountRef"] == +1

    def test_purchase_lines_oppose_its_header(self):
        """Double entry: the payer is credited, the expense line debited. This is
        what makes the intercompany triple's line-level bank leg come out with
        the sign QBO's own register shows."""
        spec = qc._FLOW_POSTINGS["Purchase"]
        assert dict(spec["header"])["AccountRef"] == -1
        assert spec["lines"] == +1

    def test_only_purchase_flips_on_the_credit_flag(self):
        flipping = [t for t, s in qc._FLOW_POSTINGS.items() if s["credit_flips"]]
        assert flipping == ["Purchase"]

    def test_credit_card_payment_is_in_the_table(self):
        """A bank->card payment IS the cash event for carded spend; card
        purchases are excluded by construction, so losing this type loses the
        outflow entirely rather than merely mis-dating it."""
        assert qc._CC_PAYMENT_ENTITY in qc._FLOW_POSTINGS

    def test_journal_entry_uses_line_posting_types(self):
        """JEs carry no header account -- a row-level scan sees nothing, yet one
        LEX week moved $79,544.95 through a bank account this way."""
        spec = qc._FLOW_POSTINGS["JournalEntry"]
        assert spec["header"] == ()
        assert spec["lines"] == "posting_type"

    def test_accrual_invoicing_types_are_absent(self):
        """Bills and Invoices are accrual events, not cash ones. Including them
        would double-count every dollar that later clears the bank."""
        for accrual in ("Bill", "Invoice", "VendorCredit", "CreditMemo"):
            assert accrual not in qc._FLOW_POSTINGS


# ── line-level account extraction ────────────────────────────────────────────

class TestLineAccountIds:
    def test_expense_line_detail(self):
        line = {"Amount": 16471.82, "AccountBasedExpenseLineDetail": {
            "AccountRef": {"value": "530", "name": "Trad LLC Main 5490"}}}
        assert qc._line_account_ids(line) == [("530", None)]

    def test_journal_line_carries_posting_type(self):
        line = {"Amount": 79544.95, "JournalEntryLineDetail": {
            "PostingType": "Credit", "AccountRef": {"value": "530"}}}
        assert qc._line_account_ids(line) == [("530", "Credit")]

    def test_item_account_ref_is_picked_up(self):
        line = {"Amount": 36.99, "SalesItemLineDetail": {
            "ItemAccountRef": {"value": "226"}}}
        assert qc._line_account_ids(line) == [("226", None)]

    def test_unknown_detail_shape_still_contributes(self):
        """Scanning any *Detail block rather than enumerating known ones: a shape
        QBO adds later contributes its account instead of vanishing."""
        line = {"Amount": 1.0, "SomeFutureLineDetail": {"AccountRef": {"value": "77"}}}
        assert qc._line_account_ids(line) == [("77", None)]

    def test_non_detail_keys_are_ignored(self):
        line = {"Amount": 1.0, "LinkedTxn": [{"TxnId": "2716", "TxnType": "Bill"}]}
        assert qc._line_account_ids(line) == []


# ── the recompute ────────────────────────────────────────────────────────────

def _flow(monkeypatch, pages: dict[str, dict], bank: set[str]):
    def fake_query(entity, query):
        for txn_type in qc._FLOW_POSTINGS:
            if f"from {txn_type} " in query:
                return pages.get(txn_type, {})
        return {}
    monkeypatch.setattr(qc, "_query", fake_query)
    return qc.bank_side_flow("F3E", bank, "2026-07-25", "2026-07-31")


class TestBankSideFlow:
    def test_purchase_on_a_bank_account_is_an_outflow(self, monkeypatch):
        out = _flow(monkeypatch, {"Purchase": {"Purchase": [
            {"Id": "1", "TotalAmt": 100.0, "AccountRef": {"value": "9"}}]}}, {"9"})
        assert out["net"] == -100.0

    def test_card_purchase_is_not_a_cash_event(self, monkeypatch):
        """The perimeter: buying on a company card moves no bank balance. The
        bank->card PAYMENT is the cash event."""
        out = _flow(monkeypatch, {"Purchase": {"Purchase": [
            {"Id": "1", "TotalAmt": 500.0, "AccountRef": {"value": "52"}}]}}, {"9"})
        assert out["net"] == 0.0
        assert out["counts"] == {}

    def test_credit_purchase_reverses_direction(self, monkeypatch):
        out = _flow(monkeypatch, {"Purchase": {"Purchase": [
            {"Id": "1", "TotalAmt": 100.0, "Credit": True,
             "AccountRef": {"value": "9"}}]}}, {"9"})
        assert out["net"] == 100.0
        assert out["credit_rows"] == 1

    def test_line_level_bank_leg_is_counted(self, monkeypatch):
        """THE INTERCOMPANY TRIPLE. Header sits on a card clearing account, the
        LINE hits the bank, `Credit: true` -- QBO's register shows -16,471.82.
        A header-only perimeter reports $0 for this week's biggest move."""
        out = _flow(monkeypatch, {"Purchase": {"Purchase": [{
            "Id": "49044", "TotalAmt": 16471.82, "Credit": True,
            "AccountRef": {"value": "361"},
            "Line": [{"Amount": 16471.82, "AccountBasedExpenseLineDetail": {
                "AccountRef": {"value": "530"}}}],
        }]}}, {"530"})
        assert out["net"] == -16471.82

    def test_internal_bank_transfer_nets_to_zero_and_is_recorded(self, monkeypatch):
        out = _flow(monkeypatch, {"Transfer": {"Transfer": [
            {"Id": "1", "Amount": 1527.02,
             "FromAccountRef": {"value": "346"}, "ToAccountRef": {"value": "31"}}]}},
            {"346", "31"})
        assert out["net"] == 0.0
        assert out["internal_transfers"] == 1527.02

    def test_transfer_to_a_card_is_a_real_outflow(self, monkeypatch):
        """bank -> credit card: the cash event for carded spend. Not internal."""
        out = _flow(monkeypatch, {"Transfer": {"Transfer": [
            {"Id": "1", "Amount": 15140.10,
             "FromAccountRef": {"value": "346"}, "ToAccountRef": {"value": "342"}}]}},
            {"346"})
        assert out["net"] == -15140.10
        assert out["internal_transfers"] == 0.0

    def test_journal_entry_debit_and_credit(self, monkeypatch):
        out = _flow(monkeypatch, {"JournalEntry": {"JournalEntry": [{
            "Id": "1", "Line": [
                {"Amount": 100.0, "JournalEntryLineDetail": {
                    "PostingType": "Debit", "AccountRef": {"value": "9"}}},
                {"Amount": 40.0, "JournalEntryLineDetail": {
                    "PostingType": "Credit", "AccountRef": {"value": "9"}}},
                {"Amount": 60.0, "JournalEntryLineDetail": {
                    "PostingType": "Debit", "AccountRef": {"value": "95"}}},
            ]}]}}, {"9"})
        assert out["net"] == 60.0

    def test_credit_card_payment_alias_reaches_the_sum(self, monkeypatch):
        out = _flow(monkeypatch, {qc._CC_PAYMENT_ENTITY: {"CreditCardPaymentTxn": [
            {"Id": "1005", "Amount": 5983.53, "BankAccountRef": {"value": "8"},
             "CreditCardAccountRef": {"value": "1150040038"}}]}}, {"8"})
        assert out["net"] == -5983.53

    def test_an_error_withholds_the_net(self, monkeypatch):
        """A partial sum is not a net flow -- UNKNOWN is never a number."""
        def fake_query(entity, query):
            if "from Deposit " in query:
                raise qc.QboClientError("boom")
            return {}
        monkeypatch.setattr(qc, "_query", fake_query)
        out = qc.bank_side_flow("F3E", {"9"}, "2026-07-25", "2026-07-31")
        assert out["net"] is None
        assert "Deposit" in out["errors"]

    def test_unexpected_key_is_reported_and_excluded(self, monkeypatch):
        out = _flow(monkeypatch, {"Deposit": {"DepositV2": [{"Id": "1"}], "totalCount": 1}}, {"9"})
        assert out["unexpected_keys"] == {"Deposit": "DepositV2"}

    def test_page_cap_is_reported(self, monkeypatch):
        rows = [{"Id": str(i), "TotalAmt": 1.0, "AccountRef": {"value": "9"}}
                for i in range(qc._FLOW_PAGE_SIZE)]
        out = _flow(monkeypatch, {"Purchase": {"Purchase": rows}}, {"9"})
        assert out["capped_types"] == ["Purchase"]

    def test_all_types_empty_is_visible_to_the_caller(self, monkeypatch):
        out = _flow(monkeypatch, {}, {"9"})
        assert set(out["empty_types"]) == set(qc._FLOW_POSTINGS)


# ── the General Ledger parse ─────────────────────────────────────────────────

def _gl_report(rows_tree, col_types=None):
    types = col_types or ["tx_date", "txn_type", "doc_num", "is_adj", "name",
                          "memo", "split_acc", "subt_nat_amount", "rbal_nat_amount"]
    return {"Columns": {"Column": [{"ColType": t, "ColTitle": t} for t in types]},
            "Rows": rows_tree}


def _row(date, txn_type, txn_id, split_id, amount, balance):
    return {"ColData": [
        {"value": date}, {"value": txn_type, "id": txn_id}, {"value": ""},
        {"value": "No"}, {"value": "Someone"}, {"value": "a memo"},
        {"value": "-Split-" if split_id is None else "5263 Fees",
         **({"id": split_id} if split_id else {})},
        {"value": f"{amount:.2f}"}, {"value": f"{balance:.2f}"}]}


def _beginning(balance):
    return {"ColData": [{"value": "Beginning Balance"}] + [{"value": ""}] * 7
                       + [{"value": f"{balance:.2f}"}]}


class TestGeneralLedgerBankRows:
    def _call(self, monkeypatch, report):
        monkeypatch.setattr(qc, "_request", lambda *a, **k: report)
        return qc.general_ledger_bank_rows("F3E", ["9"], "2026-07-25", "2026-07-31")

    def test_signed_rows_opening_and_identity(self, monkeypatch):
        report = _gl_report({"Row": [{
            "Header": {"ColData": [{"value": "1011 Tradition F3 8950", "id": "9"}]},
            "Rows": {"Row": [_beginning(200869.15),
                             _row("2026-07-22", "Expense", "2647", "109", -1.32, 200867.83),
                             _row("2026-07-22", "Deposit", "2646", None, 44.00, 200911.83)]},
            "Summary": {"ColData": [{"value": "Total"}, {"value": "42.68"}]}}]})
        out = self._call(monkeypatch, report)
        assert out["row_count"] == 2
        assert out["opening_balance"] == 200869.15
        assert [r["amount"] for r in out["rows"]] == [-1.32, 44.00]
        assert out["identity"]["checked"] == 1
        assert out["identity"]["failed"] == []

    def test_identity_breaks_when_a_row_is_lost(self, monkeypatch):
        """Prove the guard: the balance column no longer reconciles to
        opening + rows, so the window cannot pass as clean."""
        report = _gl_report({"Row": [{
            "Header": {"ColData": [{"value": "acct", "id": "9"}]},
            "Rows": {"Row": [_beginning(100.0),
                             _row("2026-07-22", "Expense", "1", "9", -10.0, 50.0)]}}]})
        out = self._call(monkeypatch, report)
        assert out["identity"]["failed"] == ["acct"]

    def test_txn_id_and_split_id_are_captured(self, monkeypatch):
        report = _gl_report({"Row": [{
            "Header": {"ColData": [{"value": "acct", "id": "9"}]},
            "Rows": {"Row": [_row("2026-07-22", "Expense", "2647", "109", -1.32, 1.0)]}}]})
        row = self._call(monkeypatch, report)["rows"][0]
        assert row["txn_id"] == "2647"
        assert row["split_account_id"] == "109"

    def test_multi_line_split_has_no_account_id(self, monkeypatch):
        """`-Split-` means several counterparts; there is no single account to
        categorise onto, so it must stay uncategorised rather than be guessed."""
        report = _gl_report({"Row": [{
            "Header": {"ColData": [{"value": "acct", "id": "9"}]},
            "Rows": {"Row": [_row("2026-07-24", "Journal Entry", "2735", None, -267.04, 1.0)]}}]})
        assert self._call(monkeypatch, report)["rows"][0]["split_account_id"] is None

    def test_human_typed_text_never_enters_the_payload(self, monkeypatch):
        """D-124, at COLLECTION. Memo/Name carry people's names -- and on a LEX
        realm potentially client names -- and this payload is mirrored into a
        shared accounting folder."""
        report = _gl_report({"Row": [{
            "Header": {"ColData": [{"value": "acct", "id": "9"}]},
            "Rows": {"Row": [_row("2026-07-22", "Expense", "1", "9", -1.0, 1.0)]}}]})
        out = self._call(monkeypatch, report)
        serialised = repr(out["rows"])
        assert "a memo" not in serialised
        assert "Someone" not in serialised
        assert set(out["rows"][0]) == {
            "date", "txn_type", "txn_id", "split_account_id", "amount", "section"}

    def test_total_rows_are_not_summed_as_data(self, monkeypatch):
        report = _gl_report({"Row": [{
            "Header": {"ColData": [{"value": "acct", "id": "9"}]},
            "Rows": {"Row": [
                _beginning(0.0),
                _row("2026-07-22", "Expense", "1", "9", -10.0, -10.0),
                {"ColData": [{"value": "Total for acct"}] + [{"value": ""}] * 6
                            + [{"value": "-10.00"}, {"value": ""}]}]}}]})
        out = self._call(monkeypatch, report)
        assert out["row_count"] == 1

    def test_nested_wrapper_section_does_not_double_count(self, monkeypatch):
        """Verified live: filtering to a CHILD account returns an outer wrapper
        section stamped with the REQUESTED id whose total repeats the child's.
        Summing section TOTALS gave -375,697.44 for a -187,848.72 account; rows
        appear exactly once, so rows are what get summed."""
        inner = {"Header": {"ColData": [{"value": "1011 Tradition F3 8950"}]},
                 "Rows": {"Row": [_beginning(100.0),
                                  _row("2026-07-22", "Expense", "1", "9", -50.0, 50.0)]},
                 "Summary": {"ColData": [{"value": "Total"}, {"value": "-50.00"}]}}
        report = _gl_report({"Row": [{
            "Header": {"ColData": [{"value": "Cash and cash equivalents", "id": "9"}]},
            "Rows": {"Row": [inner]},
            "Summary": {"ColData": [{"value": "Total"}, {"value": "-50.00"}]}}]})
        out = self._call(monkeypatch, report)
        assert out["row_count"] == 1
        assert round(sum(r["amount"] for r in out["rows"]), 2) == -50.0

    def test_repeated_opening_is_flagged_not_summed_twice(self, monkeypatch):
        report = _gl_report({"Row": [{
            "Header": {"ColData": [{"value": "acct", "id": "9"}]},
            "Rows": {"Row": [_beginning(100.0), _beginning(100.0)]}}]})
        out = self._call(monkeypatch, report)
        assert out["opening_balance"] == 100.0
        assert out["sections"]["acct"]["opening_conflict"] is True

    def test_duplicate_row_keys_are_reported_not_dropped(self, monkeypatch):
        """Two genuinely identical transactions in one day are real -- an F3E week
        held such a pair and still tied out to $0.00. Report, never gate."""
        report = _gl_report({"Row": [{
            "Header": {"ColData": [{"value": "acct", "id": "9"}]},
            "Rows": {"Row": [_row("2026-07-27", "Expense", "1", "9", -270.0, 1.0),
                             _row("2026-07-27", "Expense", "1", "9", -270.0, 1.0)]}}]})
        out = self._call(monkeypatch, report)
        assert out["row_count"] == 2
        assert out["duplicate_row_keys"] == 1

    def test_unrecognisable_columns_refuse(self, monkeypatch):
        """Guessing column positions on a finance figure is not an option."""
        report = _gl_report({"Row": []}, col_types=["something", "else"])
        with pytest.raises(qc.QboClientError, match="expected"):
            self._call(monkeypatch, report)

    def test_accounting_method_is_pinned(self, monkeypatch):
        """LEX renders Cash-basis by default while the other ten render Accrual
        (D-120); an unpinned basis makes weeks incomparable."""
        captured: dict = {}

        def fake_request(entity, path, params=None):
            captured.update(params or {})
            return _gl_report({"Row": []})
        monkeypatch.setattr(qc, "_request", fake_request)
        qc.general_ledger_bank_rows("LEX", ["530", "531"], "2026-07-25", "2026-07-31")
        assert captured["accounting_method"] == "Accrual"
        assert captured["account"] == "530,531"

    def test_no_beginning_balance_leaves_opening_unknown(self, monkeypatch):
        """UNKNOWN is never zero: a missing opening must not read as $0 cash."""
        report = _gl_report({"Row": [{
            "Header": {"ColData": [{"value": "acct", "id": "9"}]},
            "Rows": {"Row": [_row("2026-07-22", "Expense", "1", "9", -1.0, 1.0)]}}]})
        assert self._call(monkeypatch, report)["opening_balance"] is None
