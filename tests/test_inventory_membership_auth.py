"""G1 -- inventory-adjust authorization = HYBRID (Code #12; cq-f23d6885dd1c +
cq-28de84159c3f; C1/Q4 ruled 2026-09-01, membership ruled 2026-09-08).

VERIFY-FIRST (2026-09-09, roster files + live Slack read): Skylar Eastham
(U0B7BV5688Y) is a member of #f3-hq-inventory-adjustments (11 members via the bot
token) and appears in NEITHER user-permissions.yaml NOR org-roles.yaml, so the
pre-LLM entity gate read her as an unknown user (FNDR/HJRG only) and refused her
F3E inventory writes -- the live refusal AND the Hannah-vs-Skylar parity bug are one
defect: roster membership standing in for channel membership.

Contract under test:
  * IN the write channel, an inventory WRITE request from ANY poster passes the
    entity gate (entity_grant) -- membership is the post; topic blocks still run;
  * OUT of the channel (DM / other channel), the tool executes only for a LIVE
    member of the channel; a non-member is refused with the channel pointer; a
    failed lookup is refused (fail closed); no name list anywhere;
  * approval parity: identical requests from two authorized members resolve
    identically;
  * the membership cache is <= 5 minutes and never caches a failure;
  * the write-channel ids come from inventory-channel-config.yaml (C0B7X2E4VNG).
"""

from __future__ import annotations

import inspect
from unittest.mock import patch

import pytest

from cora import guard_input, inventory_membership as im, user_access
from cora.tools import tool_dispatch as td

# Captured at import, BEFORE any fixture runs: conftest's autouse default replaces
# im.member_ids with an everyone-is-a-member stub for the rest of the suite; this
# file owns the real contract and restores the real lookup per test.
_REAL_MEMBER_IDS = im.member_ids

HARRISON = "U0B2RM2JYJ1"
HANNAH = "U0B3AEQS0NB"
SKYLAR = "U0B7BV5688Y"
OUTSIDER = "U_NOT_IN_CHANNEL"
CHANNEL_ID = "C0B7X2E4VNG"
HQ = "f3-hq-inventory-adjustments"
TEMPLATE = "OFFICE INVENTORY UPDATE\nReason: handout at ASU football\nPURE: 12\n"
PROSE = "Removed 2 cases of Pure Original from the office for the pop-up"


class FakeSlack:
    def __init__(self, members, *, fail=False, pages=None):
        self.members = list(members)
        self.fail = fail
        self.pages = pages
        self.calls = 0

    def conversations_members(self, channel, limit=200, cursor=""):
        self.calls += 1
        if self.fail:
            raise RuntimeError("slack down")
        if self.pages:
            idx = int(cursor or 0)
            page = self.pages[idx]
            nxt = str(idx + 1) if idx + 1 < len(self.pages) else ""
            return {"members": page, "response_metadata": {"next_cursor": nxt}}
        return {"members": self.members, "response_metadata": {"next_cursor": ""}}


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.setattr(im, "member_ids", _REAL_MEMBER_IDS)  # undo the suite-wide stub
    im.reset_cache()
    guard_input._INV_CHANNEL_ID_CACHE.update({"at": 0.0, "value": None})
    yield
    im.reset_cache()


def test_suite_default_is_member_but_this_file_runs_the_real_lookup():
    """The conftest default (everyone is a member) exists so token-less suites never
    call Slack; this file must be running the REAL member_ids or every assertion
    below is vacuous."""
    assert im.member_ids is _REAL_MEMBER_IDS
    import tests.conftest as ct
    assert ct._EveryoneIsAMember().__contains__("anyone") is True


class TestConfig:
    def test_live_yaml_names_the_channel_id(self):
        assert guard_input.inventory_write_channel_ids() == {CHANNEL_ID}
        assert guard_input.is_inventory_write_channel(HQ)

    def test_write_intent_covers_both_forms(self):
        assert guard_input.is_inventory_write_intent(TEMPLATE)
        assert guard_input.is_inventory_write_intent(PROSE)
        assert not guard_input.is_inventory_write_intent("how many cases of Pure are at the office?")
        assert not guard_input.is_inventory_write_intent("what is the OSN revenue")


class TestMembershipLookup:
    def test_member_ids_paginates_and_caches(self):
        fake = FakeSlack([], pages=[[HANNAH, SKYLAR], [HARRISON]])
        ids = im.member_ids(CHANNEL_ID, client_factory=lambda: fake, now=1000.0)
        assert ids == frozenset({HANNAH, SKYLAR, HARRISON}) and fake.calls == 2
        again = im.member_ids(CHANNEL_ID, client_factory=lambda: fake, now=1000.0 + 299)
        assert again == ids and fake.calls == 2  # cache hit inside the TTL
        im.member_ids(CHANNEL_ID, client_factory=lambda: fake, now=1000.0 + 301)
        assert fake.calls == 4  # refreshed after 5 min

    def test_failure_returns_none_and_is_never_cached(self):
        fake = FakeSlack([], fail=True)
        assert im.member_ids(CHANNEL_ID, client_factory=lambda: fake) is None
        fake.fail = False
        fake.members = [SKYLAR]
        assert im.member_ids(CHANNEL_ID, client_factory=lambda: fake) == frozenset({SKYLAR})

    def test_no_client_is_a_failed_lookup(self, monkeypatch):
        monkeypatch.setattr(im, "_default_client", lambda: None)  # no token -> no client
        assert im.member_ids(CHANNEL_ID) is None

    def test_ttl_is_at_most_five_minutes(self):
        assert im.MEMBERSHIP_TTL_SECONDS <= 300


class TestAllows:
    def test_member_not_member_lookup_failed(self):
        fake = FakeSlack([HANNAH, SKYLAR, HARRISON])
        assert im.allows(SKYLAR, client_factory=lambda: fake) == (True, "member")
        assert im.allows(HANNAH, client_factory=lambda: fake) == (True, "member")
        assert im.allows(OUTSIDER, client_factory=lambda: fake) == (False, "not_member")
        im.reset_cache()  # a cached SUCCESS is honoured within the TTL; test the cold failure
        assert im.allows(HARRISON, client_factory=lambda: FakeSlack([], fail=True)) == (False, "lookup_failed")
        assert im.allows("", client_factory=lambda: fake) == (False, "no_user")

    def test_cached_success_is_honoured_within_ttl_even_if_slack_then_fails(self):
        good = FakeSlack([SKYLAR])
        assert im.allows(SKYLAR, client_factory=lambda: good) == (True, "member")
        assert im.allows(SKYLAR, client_factory=lambda: FakeSlack([], fail=True)) == (True, "member")

    def test_no_configured_channel_refuses(self, monkeypatch):
        monkeypatch.setattr(guard_input, "inventory_write_channel_ids", lambda: set())
        assert im.allows(SKYLAR, client_factory=lambda: FakeSlack([SKYLAR])) == (False, "no_channel_configured")

    def test_refusal_text_is_honest_and_source_opaque(self):
        for why in ("not_member", "lookup_failed", "no_channel_configured"):
            t = im.refusal_text(why)
            assert "#f3-hq-inventory-adjustments" in t
            assert "shopify" not in t.lower()
        assert "couldn't verify" in im.refusal_text("lookup_failed")
        assert "members" in im.refusal_text("not_member")

    def test_no_name_list_anywhere(self):
        src = inspect.getsource(im)
        assert "U0B3AEQS0NB" not in src and "U0B3VGWJTMJ" not in src
        # the fixture id appears only in the module docstring's incident record
        assert src.count(SKYLAR) == 1 and SKYLAR in (im.__doc__ or "")


class TestInChannelGate:
    def test_non_roster_member_passes_entity_gate_with_the_grant(self):
        assert user_access.is_authorized(SKYLAR, "F3E") is False  # the pre-fix refusal
        assert user_access.check_access(SKYLAR, "F3E", TEMPLATE) is not None
        assert user_access.check_access(SKYLAR, "F3E", TEMPLATE, entity_grant=True) is None

    def test_grant_never_skips_topic_blocks(self, monkeypatch):
        monkeypatch.setattr(user_access, "blocked_topics", lambda uid: ["hr"])
        blocked = user_access.check_access(SKYLAR, "F3E", "Removed 2 cases; also what is Alex's salary",
                                           entity_grant=True)
        assert blocked is not None and "HR" in blocked

    def test_parity_hannah_and_skylar_identical(self):
        assert user_access.check_access(HANNAH, "F3E", TEMPLATE, entity_grant=True) == \
            user_access.check_access(SKYLAR, "F3E", TEMPLATE, entity_grant=True) is None


class TestToolOutOfChannel:
    def _run(self, user, channel, members=None, fail=False):
        fake = FakeSlack(members or [], fail=fail)
        with patch.object(im, "_default_client", lambda: fake), \
             patch.object(td, "_shopify_resolve", lambda *a, **k: (f"{td._NOT_WRITTEN}\nRESOLVED-OK", None)):
            return td._shopify_set_inventory_impl(user, "F3E", {"_channel_name": channel, "product": "pure original 12", "quantity": 5})

    def test_dm_from_a_member_executes(self):
        out = self._run(SKYLAR, "dm", members=[HANNAH, SKYLAR])
        assert "RESOLVED-OK" in out

    def test_dm_from_a_non_member_is_refused_with_the_pointer(self):
        out = self._run(OUTSIDER, "dm", members=[HANNAH, SKYLAR])
        assert "NOT WRITTEN" in out and "#f3-hq-inventory-adjustments" in out and "RESOLVED-OK" not in out

    def test_other_f3e_channel_uses_membership_too(self):
        out = self._run("U0B3RU5Q55G", "f3e-sales", members=[HANNAH, SKYLAR])  # Tommy, not a channel member
        assert "NOT WRITTEN" in out and "members" in out

    def test_lookup_failure_refuses_everyone_including_the_founder(self):
        out = self._run(HARRISON, "dm", fail=True)
        assert "NOT WRITTEN" in out and "couldn't verify" in out

    def test_in_channel_never_looks_membership_up(self):
        with patch.object(im, "allows", side_effect=AssertionError("must not be called in-channel")), \
             patch.object(td, "_shopify_resolve", lambda *a, **k: (f"{td._NOT_WRITTEN}\nRESOLVED-OK", None)):
            out = td._shopify_set_inventory_impl(SKYLAR, "F3E", {"_channel_name": HQ, "product": "pure original 12", "quantity": 5})
        assert "RESOLVED-OK" in out

    def test_parity_identical_out_of_channel_requests(self):
        a = self._run(HANNAH, "dm", members=[HANNAH, SKYLAR])
        b = self._run(SKYLAR, "dm", members=[HANNAH, SKYLAR])
        assert a == b and "RESOLVED-OK" in a


class TestAppWiring:
    def test_channel_paths_pass_the_grant_and_the_dm_path_does_not(self):
        import cora.app as app_module
        src = inspect.getsource(app_module)
        # three channel-path call sites carry the grant; the DM path (_handle_dm_qa) does not
        assert src.count("entity_grant=_inv_grant") == 3
        dm = inspect.getsource(app_module._handle_dm_qa)
        assert "entity_grant" not in dm
        # the grant is channel AND write-intent, never one alone
        assert src.count("guard_input.is_inventory_write_channel(channel_name) and") == 3
        assert src.count("guard_input.is_inventory_write_intent(") == 3
