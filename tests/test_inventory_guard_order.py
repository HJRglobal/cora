"""Code #13 adjacency (D-301, ruled 2026-09-10) -- two guard-order fixes around the
Code #12 G1 HYBRID inventory authorization (commit 5e5822c), which stays intact.

(a) cq-da5abb36df07 -- user_access.check_access: the DEFAULT sensitive-topic block
    set (financials / hr / legal / phi / cap_table) applies to EVERY unlisted user
    EVERYWHERE (any channel, any DM), not only under the in-channel inventory grant.
    Code #12 applied it only when `entity_grant` was True; the `entity_grant`
    conjunct is gone. Observable only where the entity gate passes an unlisted user
    (FNDR/HJRG surfaces and DMs -- a DM resolves an unknown asker to FNDR at
    TIER_3), and `financials` stays tier-aware (TIER_1 permits finance talk by
    design, so the fixture uses TIER_3/None or a tier-blind topic). The grant path
    is unchanged: an unlisted member's in-channel template still executes, with
    the topic blocks ON.

(b) cq-eebf2408f252 -- tool_dispatch._shopify_set_inventory_impl: the out-of-channel
    MEMBERSHIP check now runs BEFORE the F3E entity-scope guard, so a NON-MEMBER's
    DM ask is refused with the MEMBERSHIP pointer ("post it in #<channel>"), never
    the entity-scope text -- whatever entity the DM resolved to. Ruling folded in:
    membership does NOT widen the entity wall (a live member whose surface entity
    is not F3E/FNDR/HJRG still meets the scope guard; D-051 lens C). In-channel
    still never looks membership up.

Conventions copied from tests/test_inventory_membership_auth.py: the real
`member_ids` is captured at import (the suite-wide conftest default makes everyone
a member) and restored per test; Slack is driven by a FakeSlack through
`_default_client`; `_shopify_resolve` is a sentinel so authority is tested apart
from resolution. All identities and text are synthetic (D-145).
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch

import pytest

from cora import guard_input, inventory_membership as im, user_access
from cora.tools import tool_dispatch as td

# Captured at import, BEFORE any fixture runs (see the module docstring).
_REAL_MEMBER_IDS = im.member_ids

HARRISON = td._HARRISON_SLACK_ID
UNLISTED = "U_UNLISTED_NOBODY_777"     # in NEITHER roster file (premise-checked below)
MEMBER_A = "U_MEMBER_ALPHA_001"        # synthetic live channel members
MEMBER_B = "U_MEMBER_BRAVO_002"
OUTSIDER = "U_OUTSIDER_CHARLIE_003"    # never in the fake channel roster
HQ = "f3-hq-inventory-adjustments"
POINTER = f"#{HQ}"
ENTITY_TEXT = "only available from F3E channels"
TEMPLATE = "OFFICE INVENTORY UPDATE\nReason: sampling table at the weekend market\nPURE: 12\n"

FIN_Q = "what's our company p&l this quarter"
HR_Q = "what is the warehouse lead's salary"
CAP_Q = "who is on the cap table and at what split"
PHI_Q = "which care plans were updated this week"
LEGAL_Q = "did the attorney send the litigation update"
PLAIN_Q = "how is the portfolio doing?"

R_FIN = "Company financials (P&L, cash, payroll) go in a finance channel or to Harrison."
R_HR = "HR matters go to Hannah Grant or Harrison."
R_CAP = "Ownership details need Harrison."
R_PHI = "Client-specific health info stays in the EHR. Ask the clinical lead."
R_LEGAL = "That's a legal matter. Reach Emily Stubbs."


class FakeSlack:
    def __init__(self, members, *, fail=False):
        self.members = list(members)
        self.fail = fail
        self.calls = 0

    def conversations_members(self, channel, limit=200, cursor=""):
        self.calls += 1
        if self.fail:
            raise RuntimeError("slack down")
        return {"members": self.members, "response_metadata": {"next_cursor": ""}}


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.setattr(im, "member_ids", _REAL_MEMBER_IDS)  # undo the suite-wide stub
    im.reset_cache()
    guard_input._INV_CHANNEL_ID_CACHE.update({"at": 0.0, "value": None})
    yield
    im.reset_cache()


def _resolve_sentinel(*_a, **_k):
    return f"{td._NOT_WRITTEN}\nRESOLVED-OK", None


def _run(user, entity, channel, members=(), fail=False):
    """Drive the tool impl directly with a fake channel roster (out-of-channel)."""
    fake = FakeSlack(members, fail=fail)
    with patch.object(im, "_default_client", lambda: fake), \
         patch.object(td, "_shopify_resolve", _resolve_sentinel):
        return td._shopify_set_inventory_impl(
            user, entity, {"_channel_name": channel, "product": "pure original 12", "quantity": 5})


def test_premises_this_file_relies_on():
    """Prevents every assertion below going vacuous: this file must run the REAL
    membership lookup (not the conftest everyone-is-a-member default), the
    synthetic ids must be absent from the live roster, and 'dm' must not read as
    the write channel."""
    assert im.member_ids is _REAL_MEMBER_IDS
    for uid in (UNLISTED, MEMBER_A, MEMBER_B, OUTSIDER):
        assert user_access._load_permissions().get(uid) is None, uid
    assert not guard_input.is_inventory_write_channel("dm")
    assert guard_input.is_inventory_write_channel(HQ)


# ── (b) guard ORDER in the inventory tool ─────────────────────────────────────

class TestGuardOrderPin:
    def test_membership_check_precedes_scope_guard_in_source(self):
        """Prevents the order regressing silently: if the scope guard is moved back
        above the membership check, a non-member's DM refusal would again carry
        the entity text instead of the channel pointer."""
        src = inspect.getsource(td._shopify_set_inventory_impl)
        assert src.count("inventory_membership.allows(") == 1
        assert src.count(ENTITY_TEXT) == 1
        assert src.index("inventory_membership.allows(") < src.index(ENTITY_TEXT)

    def test_membership_check_precedes_the_confirm_phase(self):
        """Prevents a reorder that pushes membership below the confirm/preview
        split -- both phases must stay gated (Code #12 G1 contract)."""
        src = inspect.getsource(td._shopify_set_inventory_impl)
        assert src.index("inventory_membership.allows(") < src.index("_confirmed_flag(input_data)")


class TestNonMemberGetsThePointer:
    @pytest.mark.parametrize("entity", ["OSN", "LEX-LLC", "BDM", "UFL", "HJRP"])
    def test_non_member_dm_with_non_f3e_entity_gets_pointer_not_entity_text(self, entity):
        """Prevents cq-eebf2408f252: a non-member whose DM resolved to a non-F3E
        entity used to be refused with 'only available from F3E channels' -- an
        unactionable message; the pointer names the channel to post in."""
        out = _run(OUTSIDER, entity, "dm", members=[MEMBER_A, MEMBER_B])
        assert out.startswith("WRITE_BLOCKED")
        assert "NOT WRITTEN" in out and POINTER in out and "members" in out
        assert ENTITY_TEXT not in out and "RESOLVED-OK" not in out

    def test_non_member_dm_with_f3e_entity_gets_pointer(self):
        """Prevents a regression of the Code #12 G1 pin on the F3E-entity path
        (the reorder must not change the already-correct case)."""
        out = _run(OUTSIDER, "F3E", "dm", members=[MEMBER_A, MEMBER_B])
        assert "NOT WRITTEN" in out and POINTER in out and "RESOLVED-OK" not in out

    def test_non_member_from_another_channel_gets_pointer(self):
        """Prevents the reorder being DM-only: any non-write channel is
        out-of-channel and gets the same pointer."""
        out = _run(OUTSIDER, "OSN", "osn-leadership", members=[MEMBER_A])
        assert POINTER in out and ENTITY_TEXT not in out

    @pytest.mark.parametrize("entity", ["OSN", "F3E"])
    def test_lookup_failure_says_could_not_verify_for_any_entity(self, entity):
        """Prevents a failed lookup on a non-F3E surface being narrated as an
        entity-scope refusal: 'could not verify' is the honest, fail-closed copy."""
        out = _run(OUTSIDER, entity, "dm", fail=True)
        assert "NOT WRITTEN" in out and "couldn't verify" in out
        assert ENTITY_TEXT not in out and "RESOLVED-OK" not in out

    def test_non_member_founder_dm_gets_pointer_no_side_door(self):
        """Prevents the reorder opening a founder side-door: Harrison's FNDR entity
        passes the scope guard, but out-of-channel he is a member like anyone
        else -- not in the roster means the pointer."""
        out = _run(HARRISON, "FNDR", "dm", members=[MEMBER_A])
        assert POINTER in out and "RESOLVED-OK" not in out


class TestMembershipDoesNotWidenTheEntityWall:
    def test_member_with_non_f3e_entity_still_meets_scope_guard(self):
        """Prevents membership short-circuiting the entity guard: a live channel
        member whose DM resolved to OSN is still refused on scope (a channel
        roster is not an entity grant, D-051 lens C)."""
        out = _run(MEMBER_A, "OSN", "dm", members=[MEMBER_A, MEMBER_B])
        assert "NOT WRITTEN" in out and ENTITY_TEXT in out and "RESOLVED-OK" not in out

    def test_member_with_f3e_entity_executes(self):
        """Prevents the reorder breaking the happy path (Code #12 G1 pin)."""
        out = _run(MEMBER_A, "F3E", "dm", members=[MEMBER_A, MEMBER_B])
        assert "RESOLVED-OK" in out

    def test_member_founder_cross_entity_executes(self):
        """Prevents the reorder breaking the founder cross-entity path: Harrison,
        a member, asking from an OSN surface passes both guards."""
        out = _run(HARRISON, "OSN", "dm", members=[HARRISON, MEMBER_A])
        assert "RESOLVED-OK" in out and ENTITY_TEXT not in out

    def test_parity_two_members_identical(self):
        """Prevents identity-dependent outcomes for equally-authorized members."""
        a = _run(MEMBER_A, "F3E", "dm", members=[MEMBER_A, MEMBER_B])
        im.reset_cache()
        b = _run(MEMBER_B, "F3E", "dm", members=[MEMBER_A, MEMBER_B])
        assert a == b and "RESOLVED-OK" in a


class TestInChannelUnchanged:
    def test_in_channel_never_looks_membership_up_and_scope_guard_still_runs(self):
        """Prevents the reorder adding a Slack lookup to the in-channel path (the
        post itself is the membership proof) while keeping the scope guard for a
        mis-routed entity."""
        with patch.object(im, "allows", side_effect=AssertionError("must not be called in-channel")), \
             patch.object(td, "_shopify_resolve", _resolve_sentinel):
            ok = td._shopify_set_inventory_impl(
                UNLISTED, "F3E", {"_channel_name": HQ, "product": "pure original 12", "quantity": 5})
            wrong_entity = td._shopify_set_inventory_impl(
                UNLISTED, "OSN", {"_channel_name": HQ, "product": "pure original 12", "quantity": 5})
        assert "RESOLVED-OK" in ok
        assert ENTITY_TEXT in wrong_entity and "RESOLVED-OK" not in wrong_entity


# ── fixture 2: an unlisted member's in-channel template executes, blocks ON ───

class TestUnlistedMemberInChannelGrantUnchanged:
    def test_template_passes_gate_and_tool_executes(self):
        """Prevents (a) breaking the grant path: an unlisted channel member's
        in-channel template still passes the pre-LLM gate under the grant and
        the tool still executes without a membership lookup."""
        assert user_access.blocked_topics(UNLISTED) == []   # roster list is empty ...
        assert user_access.check_access(UNLISTED, "F3E", TEMPLATE, entity_grant=True) is None
        with patch.object(im, "allows", side_effect=AssertionError("must not be called in-channel")), \
             patch.object(td, "_shopify_resolve", _resolve_sentinel):
            out = td._shopify_set_inventory_impl(
                UNLISTED, "F3E", {"_channel_name": HQ, "product": "pure original 12", "quantity": 5})
        assert "RESOLVED-OK" in out

    def test_topic_blocks_are_on_under_the_grant(self):
        """Prevents the grant reading as 'no topic is sensitive': the default
        blocks run on the raw message for an unlisted member under the grant."""
        assert user_access.check_access(UNLISTED, "F3E", TEMPLATE + "\nAlso, " + HR_Q,
                                        entity_grant=True) == R_HR
        assert user_access.check_access(UNLISTED, "F3E", TEMPLATE + "\nAlso, " + FIN_Q,
                                        entity_grant=True) == R_FIN
        assert user_access.check_access(UNLISTED, "F3E", TEMPLATE + "\nAlso, " + CAP_Q,
                                        entity_grant=True) == R_CAP

    def test_no_grant_means_the_entity_gate_still_refuses(self):
        """Prevents (a) being mistaken for an entity widening: without the grant an
        unlisted user is still refused for F3E at the entity gate."""
        assert user_access.is_authorized(UNLISTED, "F3E") is False
        refusal = user_access.check_access(UNLISTED, "F3E", TEMPLATE)
        assert refusal is not None and "F3E" not in refusal


# ── (a) + fixture 3: the default blocks apply to unlisted users EVERYWHERE ────

class TestUnlistedDefaultBlocksEverywhere:
    @pytest.mark.parametrize("tier", ["TIER_3", None])
    def test_unlisted_fndr_company_finance_blocked(self, tier):
        """Prevents cq-da5abb36df07: an unlisted user in a non-inventory (FNDR-
        routed, non-TIER_1) channel asking company finance passed with NO topic
        screen because their roster block list was empty."""
        assert user_access.check_access(UNLISTED, "FNDR", FIN_Q, tier=tier) == R_FIN

    @pytest.mark.parametrize("entity", ["FNDR", "HJRG"])
    @pytest.mark.parametrize("text,redirect", [
        (HR_Q, R_HR), (CAP_Q, R_CAP), (PHI_Q, R_PHI), (LEGAL_Q, R_LEGAL),
    ])
    def test_unlisted_tier_blind_topics_blocked_on_aggregator_surfaces(self, entity, text, redirect):
        """Prevents the default being finance-only: hr / cap_table / phi / legal
        block an unlisted user on every aggregator surface regardless of tier."""
        assert user_access.check_access(UNLISTED, entity, text, tier="TIER_1") == redirect
        assert user_access.check_access(UNLISTED, entity, text, tier="TIER_3") == redirect

    def test_unlisted_tier1_finance_still_passes_by_design(self):
        """Prevents (a) silently changing the tier rule: `financials` stays
        tier-aware, so an unlisted user's finance question in a TIER_1 surface
        (every HJRG channel; leadership/finance/founder/build) is not pre-empted.
        cap_table stays blocked there (tier-blind)."""
        assert user_access.check_access(UNLISTED, "HJRG", FIN_Q, tier="TIER_1") is None
        assert user_access.check_access(UNLISTED, "HJRG", CAP_Q, tier="TIER_1") == R_CAP

    def test_unlisted_non_sensitive_question_still_passes(self):
        """Prevents the default over-refusing ordinary questions: the FNDR
        catch-all posture for unlisted users survives for non-sensitive text."""
        assert user_access.check_access(UNLISTED, "FNDR", PLAIN_Q, tier="TIER_3") is None
        assert user_access.check_access(UNLISTED, "FNDR", "how are sales going?", tier="TIER_3") is None

    def test_harrison_is_exempt_even_if_the_roster_is_unreadable(self, monkeypatch):
        """Prevents the default ever applying to Harrison: with an unreadable roster
        (empty map) everyone else is unlisted and screened; Harrison is not."""
        monkeypatch.setattr(user_access, "_load_permissions", lambda: {})
        assert user_access.check_access(HARRISON, "FNDR", FIN_Q) is None
        assert user_access.check_access(UNLISTED, "FNDR", FIN_Q) == R_FIN

    def test_listed_user_with_explicit_empty_list_keeps_it(self, monkeypatch):
        """Prevents the default clobbering a LISTED user's roster: an explicit
        `sensitive_topics_blocked: []` on a listed user means no topic blocks --
        the default keys on roster ABSENCE, not on an empty block list."""
        roster = {"U_LISTED_DELTA_004": {"allowed_entities": ["F3E"], "sensitive_topics_blocked": []}}
        monkeypatch.setattr(user_access, "_load_permissions", lambda: roster)
        assert user_access.check_access("U_LISTED_DELTA_004", "F3E", FIN_Q) is None
        assert user_access.check_access(UNLISTED, "FNDR", FIN_Q) == R_FIN

    def test_default_lives_in_check_access_not_blocked_topics(self):
        """Prevents a relocation of the default into blocked_topics(): the Code
        #12 pin `blocked_topics(unlisted) == []` documents that the roster list is
        the roster list; the default is applied at the gate."""
        assert user_access.blocked_topics(UNLISTED) == []
        assert set(user_access._UNLISTED_DEFAULT_BLOCKED_TOPICS) == {
            "financials", "hr", "legal", "phi", "cap_table"}
        assert user_access._GRANT_DEFAULT_BLOCKED_TOPICS is user_access._UNLISTED_DEFAULT_BLOCKED_TOPICS

    def test_no_new_check_access_kwarg(self):
        """Prevents a signature drift the surface-parity tests pin by exact kwargs
        (tests/test_surface_guard_parity.py, tests/test_thread_followup_access.py)."""
        params = list(inspect.signature(user_access.check_access).parameters)
        assert params == ["user_id", "entity", "user_message", "phi_custodian", "tier", "entity_grant"]


class TestDmSurfaceEndToEnd:
    """The DM handler passes no grant and pins TIER_3, so (a) changes its behaviour
    with zero code edits there; drive the REAL guard chain to prove it."""

    def test_unlisted_dm_company_finance_deflected_pre_llm(self):
        """Prevents the DM surface leaking company finance to an unlisted asker:
        before (a) an unknown DM user resolved to FNDR and reached _dispatch_qa
        with a P&L question."""
        from tests.test_surface_guard_parity import UNKNOWN, _invoke, _refusal_text, surface_ctx
        with surface_ctx("_handle_dm_qa", "real") as ctx:
            client = MagicMock()
            _invoke("_handle_dm_qa", client, FIN_Q + "?", UNKNOWN)
            ctx.dispatch.assert_not_called()
            text = _refusal_text("_handle_dm_qa", client, None)
            assert text and R_FIN in text

    def test_unlisted_dm_plain_question_still_dispatches(self):
        """Prevents (a) collapsing the by-design DM asymmetry for unlisted users:
        a non-sensitive question still reaches _dispatch_qa."""
        from tests.test_surface_guard_parity import UNKNOWN, _invoke, surface_ctx
        with surface_ctx("_handle_dm_qa", "real") as ctx:
            client = MagicMock()
            _invoke("_handle_dm_qa", client, PLAIN_Q, UNKNOWN)
            ctx.dispatch.assert_called_once()
