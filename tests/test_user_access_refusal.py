"""Regression tests for the access-gate refusal copy (Fix 2) + Alex's F3E
authorization (Fix 3), from the 2026-06-08 comms review.

Fix 2: check_access refusal must never emit an internal entity code.
Fix 3: the 6/1 #f3-events refusal of Alex was a data gap, now closed -- lock it.
"""

import pathlib
import sys

_REPO_ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora.user_access import check_access, is_authorized  # noqa: E402

_ENTITY_CODE_TOKENS = ["FNDR", "HJRG", "F3E", "OSN", "LEX", "UFL", "BDM", "HJRP", "HJRPROD", "F3C"]


class TestRefusalCopyNoEntityLeak:
    def test_refusal_emits_no_entity_code(self):
        # An unknown user asking about a non-FNDR/HJRG entity is refused.
        msg = check_access("U_UNKNOWN_USER_XYZ", "F3E", "how are sales going")
        assert msg is not None, "expected a refusal for unknown user on F3E"
        for token in _ENTITY_CODE_TOKENS:
            assert token not in msg, f"refusal copy leaked entity code: {token!r} in {msg!r}"

    def test_refusal_is_redirecting_and_channel_relative(self):
        msg = check_access("U_UNKNOWN_USER_XYZ", "OSN", "what's the revenue")
        assert msg is not None
        assert "channel" in msg.lower()

    def test_known_user_blocked_entity_no_leak(self):
        # Alex is F3E/UFL/HJRG; asking about OSN is refused -- copy must not leak "OSN"/"F3E".
        msg = check_access("U0B3VGWJTMJ", "OSN", "how is the store doing")
        assert msg is not None
        for token in _ENTITY_CODE_TOKENS:
            assert token not in msg


class TestAlexAuthorizationFix3:
    def test_alex_authorized_f3e(self):
        # The 6/1 #f3-events refusal must not recur: Alex IS authorized for F3E.
        assert is_authorized("U0B3VGWJTMJ", "F3E") is True

    def test_alex_authorized_ufl_and_hjrg(self):
        assert is_authorized("U0B3VGWJTMJ", "UFL") is True
        assert is_authorized("U0B3VGWJTMJ", "HJRG") is True

    def test_alex_not_authorized_osn(self):
        assert is_authorized("U0B3VGWJTMJ", "OSN") is False

    def test_alex_f3e_access_passes_check(self):
        # Full check_access path: a non-sensitive F3E question from Alex passes (None).
        assert check_access("U0B3VGWJTMJ", "F3E", "what events are coming up") is None


# ── Legal-deflection precision (Phase 1.6) ───────────────────────────────────

import pytest  # noqa: E402

from cora.user_access import _legal_is_blocked  # noqa: E402


class TestLegalDeflectionPrecision:
    @pytest.mark.parametrize("msg", [
        "are we exposed on the lawsuit with the vendor",
        "the attorney sent the litigation notice",
        "should we sue them for breach of contract",
        "the contract is in dispute and we may be liable",
        "this agreement has an indemnification clause we need",
        "is that conversation privileged",
    ])
    def test_genuine_legal_matter_blocked(self, msg):
        assert _legal_is_blocked(msg.lower()) is True

    @pytest.mark.parametrize("msg", [
        "what's our distribution agreement volume",
        "do we have liability insurance for the warehouse",
        "when does the vendor contract renew",
        "can you pull the latest signed agreement pdf",
        "let's discuss the contract issue on the call",   # 'issue' must NOT trip 'sue'
        "how do we structure the partnership",
    ])
    def test_ordinary_legal_adjacent_allowed(self, msg):
        assert _legal_is_blocked(msg.lower()) is False


class TestLegalDeflectionIntegration:
    # Larry Stone (BDM + F3E) stably has 'legal' blocked. NOT Tommy/Alex — their
    # 'legal' posture is in flux (the 2026-06-30 stopgap removed it pending the
    # tightened matcher), so a stable legal-blocked user makes this test robust.
    LEGAL_BLOCKED = "U0B3NGR1Y85"  # Larry Stone

    def test_ordinary_agreement_question_allowed(self):
        # The over-eager bug: this used to be refused. Now allowed.
        assert check_access(self.LEGAL_BLOCKED, "F3E", "what's our distribution agreement volume") is None

    def test_genuine_legal_matter_still_blocked(self):
        msg = check_access(self.LEGAL_BLOCKED, "F3E", "are we exposed on the lawsuit with the vendor")
        assert msg is not None and "legal" in msg.lower()


# ── Over-deflection fix (2026-06-30): commercial vs restricted finance ────────
# Alex/Tommy are sales roles whose daily work is money/contract-adjacent. Their
# legitimate COMMERCIAL questions (deal value, PO amount, wholesale price, margin
# on an order, invoice paid-status) must pass; genuine COMPANY finance (P&L,
# cash, payroll, cap table) must still deflect. See the review doc:
# _shared/projects/cora/2026-06-30_fndr_cora-over-deflection-review.md

from cora.user_access import _financials_is_blocked  # noqa: E402

ALEX = "U0B3VGWJTMJ"   # F3E/UFL/HJRG — has 'financials' + 'cap_table' blocked
TOMMY = "U0B3RU5Q55G"  # F3E — has 'financials' + 'cap_table' blocked


class TestFinancialsMatcherSplit:
    """The matcher blocks only genuine COMPANY-level finance and lets deal-level
    commercial money talk pass. Covers the 2026-06-30 adversarial review:
    finance-leak-0 (natural-language company finance must block) and
    commercial-residue-0 (deal/account-qualified revenue must pass)."""

    @pytest.mark.parametrize("msg", [
        # CANON — always company-level (no scope word needed)
        "what is our company p&l",
        "show me the profit and loss statement",
        "how is our cash flow this week",
        "what's the cash position right now",
        "cash balance at month end",
        "net income this month",
        "net loss",
        "operating income",
        "ebitda year to date",
        "what is the ebit",
        "pull the balance sheet",
        "net worth",
        "what's our payroll cost this month",
        "show me payrolls for all stores",       # plural (regex-safety-0)
        "accounts receivable aging report",
        "accounts payable aging",
        "cap rate on the 1337 building",
        "debt service coverage",
        "what's the refinance status",
        "quickbooks balance",
        "our operating expenses",
        "what are total expenses",
        "company budget",
        "what is cogs",                          # company-leak-3
        "cost of goods sold",
        "what is the overhead",
        "how is cash looking",
        "how much cash do we have",
        "how much cash is left",
        "do we have enough cash",
        "are we cash positive",
        "what is our monthly burn",
        "how much are we losing",
        "the financials for q2",                 # bare 'financials', no deal
        "financial performance ytd",
        "profitability",
        # COMPANY-SCOPED natural language (finance-leak-0)
        "are we profitable",
        "what is our profit this quarter",
        "how much profit did f3e make",
        "company profit",
        "what is our gross margin",
        "how much revenue did we do",
        "what is our profitability",
        "how much debt do we have",
        "how are our finances",
        "how is the company doing financially",
        "did we lose money last quarter",
        "company revenue this quarter",
        "total revenue last month",
        # AGGREGATE / CATEGORY roll-ups (company-leak-0): a company roll-up that
        # names a category/aggregate must still block.
        "what's our profit this quarter across all accounts",
        "our revenue this year by product",
        "our margin overall per unit",
        "revenue company-wide by product",
        "what is our total revenue from products",
        "our product revenue",
        "our wholesale revenue",
        "revenue from all our accounts",
        "revenue from products",
        # money-verb idioms w/ company scope (company-leak-2)
        "how much are we bringing in",
        "are we making money",
        "what do we owe",
        "are we in the black",
        # plurals (fresh-correctness-0)
        "what are our margins this year",
        "our profits last quarter",
        "company debts",
    ])
    def test_restricted_company_finance_blocked(self, msg):
        assert _financials_is_blocked(msg.lower()) is True

    @pytest.mark.parametrize("msg", [
        "what's the mma lab deal value",
        "did the sprouts PO get paid",
        "what's the wholesale price on a case",
        "what's the margin on this order",
        "how much did the costco order cost",   # 'cost'->'Costco' must NOT trip
        "revenue from the mma lab deal",         # deal-level revenue is commercial
        # deal/account-qualified revenue (commercial-residue-0) must PASS
        "total revenue for the mma lab deal",
        "monthly revenue from the sprouts account",
        "net revenue on the sponsorship deal",
        "quarterly revenue from the whole foods account",
        "our revenue from the sprouts account",  # specific-deal overrides company-scope
        "the financials on this deal",           # deal-scoped 'financials' is commercial
        "pull the deal financials",
        "profit on this deal",                    # bare 'profit', deal-scoped
        # deal-qualified accounting terms are commercial (rd2 commercial-block-0)
        "gross profit on the order",
        "net profit on this deal",
        # PROPER-NOUN / rep / region / partnership commercial (rd3 overblock-0):
        # a sales owner names a customer/rep/region, not the generic word 'account'.
        "what's the revenue from whole foods this month",
        "how much revenue did sprouts bring us this quarter",
        "what's the margin on sprouts",
        "how profitable is the whole foods relationship",
        "what's tommy's revenue this month",
        "what's the revenue in arizona",
        "how much revenue did the mma lab partnership generate",
        "what's the revenue from the cejudo activation",
        "margin on the wholesale order",
        "what is the amount on the whole foods po",
        # event / booth / flavor sales questions (rd2 commercial-block-1)
        "how did we do on revenue for the booth",
        "our revenue at the trade show",
        "hows our margin looking on the new flavor",
        "our earnings from the booth this weekend",
        "the incoming shipment is late",          # 'income'->'incoming' must NOT trip
        "we made marginal gains this week",       # 'margin'->'marginal' must NOT trip
        "that codec is lossless",                  # 'loss'->'lossless' must NOT trip
        "this was a profitable deal",             # 'profitable' + deal-scope
        "we are losing steam this week",          # non-finance 'losing' must NOT trip
        "losing momentum on the campaign",
        "what did we spend on the trade show booth",
        "the invoice for the pallet order",
        "what's the budget for the launch event",  # not 'company budget'
        "deal size for the red hawk account",
        "how many orders shipped yesterday",
    ])
    def test_commercial_or_substring_not_blocked(self, msg):
        assert _financials_is_blocked(msg.lower()) is False


class TestBareTermResidualPasses:
    """v3 precision decision (2026-06-30 rd3 overblock-0): a BARE, unscoped money
    term defaults to COMMERCIAL and reaches the LLM (layers 2+3 — the prompt
    TIER_3 hard-stop and the tool-level TIER_1 gate — cover the rare company
    case). Blocking these by default structurally over-refused proper-noun
    commercial questions ("revenue from Whole Foods"), which is the exact bug this
    fix removes. Locked here so re-closing this residual is a conscious choice, not
    an accident."""

    @pytest.mark.parametrize("msg", [
        "what's the profit",
        "what's the revenue",
        "how much revenue",      # no "did we"
        "gross margin",
        "net margin",
        "operating margin",
        "what's the margin",
    ])
    def test_bare_scopeless_term_passes(self, msg):
        assert _financials_is_blocked(msg.lower()) is False


class TestCommercialQuestionsPass:
    """R6: Alex/Tommy commercial questions are NOT blocked (in their own entity)."""

    @pytest.mark.parametrize("user", [ALEX, TOMMY])
    @pytest.mark.parametrize("msg", [
        "what's the mma lab deal value",
        "did the sprouts PO get paid",
        "what's our wholesale price on a case of energy",
        "what's the margin on this order",
        "how much did the last order cost",
        "what's the invoice status for the pallet",
    ])
    def test_commercial_question_passes_tier3(self, user, msg):
        # TIER_3 (their sales channel) — commercial questions must reach the LLM.
        assert check_access(user, "F3E", msg, tier="TIER_3") is None


class TestCompanyFinanceStillBlocked:
    """R6 invariant: genuine company finance + cap table still gated for sales roles."""

    @pytest.mark.parametrize("user", [ALEX, TOMMY])
    @pytest.mark.parametrize("msg", [
        "what's our company p&l",
        "what's the cash position",
        "what's our payroll this month",
        "net income for f3e ytd",
    ])
    def test_company_finance_blocked_in_tier3(self, user, msg):
        block = check_access(user, "F3E", msg, tier="TIER_3")
        assert block is not None
        assert "financ" in block.lower()

    @pytest.mark.parametrize("user", [ALEX, TOMMY])
    @pytest.mark.parametrize("msg", [
        "what's the cap table look like",
        "how much equity does each owner have",
    ])
    def test_cap_table_blocked_regardless_of_tier(self, user, msg):
        # cap_table is Harrison-only in EVERY channel — tier-blind.
        assert check_access(user, "F3E", msg, tier="TIER_3") is not None
        assert check_access(user, "F3E", msg, tier="TIER_1") is not None


class TestFinancialsTierAware:
    """R3: the financials block is suppressed in a TIER_1 channel; cap_table is not."""

    def test_company_finance_not_preempted_in_tier1(self):
        # #f3e-leadership is TIER_1 — the deterministic financials block must not
        # pre-empt (the tool-level TIER_1 gate + prompt still govern the data).
        assert check_access(ALEX, "F3E", "what's our company p&l", tier="TIER_1") is None
        assert check_access(TOMMY, "F3E", "what's the cash position", tier="TIER_1") is None

    def test_company_finance_blocked_when_tier_absent(self):
        # Fail-safe: no tier passed => treated as non-TIER_1 => still blocked.
        assert check_access(ALEX, "F3E", "what's our company p&l") is not None
        assert check_access(ALEX, "F3E", "what's our company p&l", tier=None) is not None

    def test_cap_table_still_blocked_in_tier1(self):
        # R3 only relaxes 'financials' — cap_table stays gated in TIER_1.
        assert check_access(ALEX, "F3E", "what's the cap table", tier="TIER_1") is not None


class TestLegalNarrowingIntegration:
    """R1: routine commercial contract talk (terminate/penalty) no longer reads
    as a legal matter for a legal-blocked user; genuine legal still deflects."""

    LEGAL_BLOCKED = "U0B3NGR1Y85"  # Larry Stone

    @pytest.mark.parametrize("msg", [
        "what's the early-termination penalty on the mma lab sponsorship contract",
        "the vendor contract default clause",
        "there's an moq violation in the agreement",
        "when does the distribution agreement renew",
    ])
    def test_commercial_contract_talk_allowed(self, msg):
        assert check_access(self.LEGAL_BLOCKED, "F3E", msg) is None

    @pytest.mark.parametrize("msg", [
        "are we exposed on the lawsuit with the vendor",
        "the attorney sent a litigation notice",
        "should we sue them for breach of contract",
    ])
    def test_genuine_legal_still_blocked(self, msg):
        assert check_access(self.LEGAL_BLOCKED, "F3E", msg) is not None


class TestHarrisonBypassUnaffected:
    HARRISON = "U0B2RM2JYJ1"

    def test_harrison_never_blocked_on_finance(self):
        assert check_access(self.HARRISON, "F3E", "what's our company p&l", tier="TIER_3") is None
        assert check_access(self.HARRISON, "OSN", "cash position for all stores") is None
