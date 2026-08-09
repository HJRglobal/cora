"""v2b S5: typed-confirm arbitration + Sonnet-force parity for the six Class-B
stash kinds.

The v2 S2 lesson, third time around the same loop. While the Class-B tools were
honor gates they minted nothing, so there was nothing for try_confirm_pending_write
to arbitrate. The moment a migration starts STASHING, that kind's confirm turn is
a bare affirmative sitting over a store the arbitration does not peek -- and the
freshest of the kinds it DOES peek fires instead. gmail_create_draft shipped in
that state on this branch; these tests pin the whole family, including the five
kinds whose producers land in later slices, so a kind can never again be
button-confirmable and invisible to freshest-first arbitration at the same time.

Also pins the D-164 deterministic typed CANCEL per kind (a cancel needs no
executor, only a claim) and the survivor enumeration that goes with it.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test-signing-secret")

import pytest  # noqa: E402

from cora import confirm_cards as cc  # noqa: E402
from cora.tools import tool_dispatch as td  # noqa: E402

USER_ID = "U0CLASSB"
CHANNEL = "f3e-leadership"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "on")
    monkeypatch.delenv("CORA_EVAL_MODE", raising=False)
    cc.reset_cards_for_tests()
    with cc._INDEX_LOCK:
        cc._INDEX.clear()
    _wipe()
    yield
    _wipe()
    cc.reset_cards_for_tests()


def _wipe() -> None:
    for store in (td._PENDING_ASANA_WRITES, td._PENDING_SHOPIFY_WRITES):
        store.clear()
    for kind in td._CLASSB_KINDS:
        td._CLASSB[kind]["store"].clear()


def _fresh_classb(kind: str, age: float = 0.0) -> str:
    """Mint a Class-B pending directly in its store. Deliberately store-level
    rather than through each tool: this file is about the ARBITRATION contract,
    which must hold for a kind whose producer has not been written yet."""
    sid = cc.mint_stash_id(kind, USER_ID, CHANNEL)
    td._CLASSB[kind]["put"](USER_ID, CHANNEL, {"ts": time.time() - age, "stash_id": sid})
    return sid


def _stale_shopify(age: float = 30.0) -> str:
    sid = cc.mint_stash_id("shopify", USER_ID, CHANNEL)
    td._store_pending_shopify_write(USER_ID, CHANNEL, {
        "sku": "SKU1", "delta": -5, "ts": time.time() - age, "stash_id": sid,
    })
    return sid


def _stale_asana_delete(age: float = 30.0) -> str:
    sid = cc.mint_stash_id("asana", USER_ID, CHANNEL)
    td._store_pending_asana_write(USER_ID, CHANNEL, {
        "action": "delete", "gid": "g1", "label": "Old task",
        "ts": time.time() - age, "stash_id": sid,
    })
    return sid


ALL_KINDS = list(td._CLASSB_KINDS)


class TestPeekSetParity:
    @pytest.mark.parametrize("kind", ALL_KINDS)
    def test_bare_yes_never_fires_a_staler_shopify_write(self, kind):
        stale = _stale_shopify()
        fresh = _fresh_classb(kind)

        with patch.object(td, "_run_confirm_execute") as ex:
            reply = td.try_confirm_pending_write(
                slack_user_id=USER_ID, channel_name=CHANNEL, entity="F3E",
                message="yes")

        ex.assert_not_called()
        assert reply is None, "must defer to the model, not answer deterministically"
        assert td.stash_is_live(stale), "the staler write must be left untouched"
        assert td.stash_is_live(fresh), "the fresh pending survives for the tool"

    @pytest.mark.parametrize("kind", ALL_KINDS)
    def test_bare_yes_never_fires_a_staler_asana_delete(self, kind):
        """The severe direction: an unrelated 'yes' permanently deleting a task.

        The stale delete is ABANDONED rather than kept -- that is Case 2's
        deliberate safety rule (a fresher write superseded it, so it must never
        be able to fire on a later stray affirmative), and it is what every
        other defer-kind already does. What must never happen is EXECUTION."""
        stale = _stale_asana_delete()
        fresh = _fresh_classb(kind)

        with patch.object(td, "_run_confirm_execute") as ex:
            reply = td.try_confirm_pending_write(
                slack_user_id=USER_ID, channel_name=CHANNEL, entity="F3E",
                message="yes")

        ex.assert_not_called()
        assert reply is None
        assert td.stash_is_live(fresh), "the fresh pending survives for the tool"
        assert not td.stash_is_live(stale), (
            "the superseded destructive delete must be disarmed, not left armed")

    def test_a_stale_classb_pending_does_not_block_a_fresher_shopify_confirm(self):
        """Parity in the other direction: an OLD Class-B pending must not stop a
        genuinely fresher Shopify confirm from executing deterministically."""
        _fresh_classb("gmail_draft", age=60.0)
        _stale_shopify(age=0.0)

        with patch.object(td, "_run_confirm_execute", return_value="Set.") as ex:
            reply = td.try_confirm_pending_write(
                slack_user_id=USER_ID, channel_name=CHANNEL, entity="F3E",
                message="yes")
        ex.assert_called_once()
        assert reply == "Set."

    def test_freshest_first_holds_among_the_classb_kinds(self):
        _fresh_classb("hubspot_note", age=30.0)
        newest = _fresh_classb("slack_dm", age=0.0)
        with patch.object(td, "_run_confirm_execute") as ex:
            assert td.try_confirm_pending_write(
                slack_user_id=USER_ID, channel_name=CHANNEL, entity="F3E",
                message="yes") is None
        ex.assert_not_called()
        assert td.stash_is_live(newest)

    def test_identical_timestamps_do_not_raise(self):
        """entries.sort() compares the third tuple slot on a ts tie -- a None
        action there would TypeError against another kind's string."""
        now = time.time()
        for kind in ("gmail_draft", "hubspot_stage"):
            sid = cc.mint_stash_id(kind, USER_ID, CHANNEL)
            td._CLASSB[kind]["put"](USER_ID, CHANNEL, {"ts": now, "stash_id": sid})
        sid = cc.mint_stash_id("asana", USER_ID, CHANNEL)
        td._store_pending_asana_write(USER_ID, CHANNEL, {
            "action": "create", "title": "T", "ts": now, "stash_id": sid})
        assert td.try_confirm_pending_write(
            slack_user_id=USER_ID, channel_name=CHANNEL, entity="F3E",
            message="yes") is None


class TestDeterministicTypedCancel:
    """D-164: a typed cancel needs no executor, only a claim, so the interceptor
    handles it for defer-kinds too. Pinned per kind so the factory registration
    cannot silently stop covering one."""

    @pytest.mark.parametrize("kind", ALL_KINDS)
    def test_typed_cancel_pops_the_classb_pending(self, kind):
        sid = _fresh_classb(kind)
        reply = td.try_confirm_pending_write(
            slack_user_id=USER_ID, channel_name=CHANNEL, entity="F3E",
            message="no, cancel that")
        assert reply is not None, "a typed cancel must not silently fall through"
        assert "cancelled" in reply.lower()
        assert not td.stash_is_live(sid)

    @pytest.mark.parametrize("kind", ALL_KINDS)
    def test_the_cancel_reply_names_the_survivors(self, kind):
        """A cancel pops only the FRESHEST pending; the others stay armed and
        confirmable, so the reply has to say so."""
        _stale_shopify()
        sid = _fresh_classb(kind)
        reply = td.try_confirm_pending_write(
            slack_user_id=USER_ID, channel_name=CHANNEL, entity="F3E",
            message="cancel")
        assert not td.stash_is_live(sid)
        assert "Still staged" in reply and "inventory change" in reply

    @pytest.mark.parametrize("kind", ALL_KINDS)
    def test_every_classb_kind_has_a_human_label(self, kind):
        assert kind in td._PENDING_KIND_LABELS, (
            f"{kind} would be named by its raw kind token in user-facing copy")


class TestSonnetForceParity:
    @pytest.mark.parametrize("kind", ALL_KINDS)
    def test_has_pending_classb_true_for_each_kind(self, kind):
        _fresh_classb(kind)
        assert td.has_pending_classb(USER_ID, CHANNEL) is True

    def test_has_pending_classb_false_when_absent(self):
        assert td.has_pending_classb(USER_ID, CHANNEL) is False

    @pytest.mark.parametrize("kind", ALL_KINDS)
    def test_has_pending_classb_false_when_expired(self, kind):
        _fresh_classb(kind, age=td._CLASSB_TTL_SECONDS + 5)
        assert td.has_pending_classb(USER_ID, CHANNEL) is False

    def test_it_is_wired_into_the_escalation_chain(self):
        src = (_REPO_ROOT / "src" / "cora" / "app.py").read_text(encoding="utf-8")
        assert "has_pending_classb(user_id, channel_name)" in src, \
            "the Class-B kinds must join the Sonnet-force OR-chain"


class TestDeferRegistrationIsComplete:
    """Revert-proofing (D-154): the three properties that make a registered kind
    safe are separate pieces of code, and shipping one without the others is the
    exact defect this file exists for."""

    @pytest.mark.parametrize("kind", ALL_KINDS)
    def test_registered_kinds_defer_to_the_model(self, kind):
        assert kind in td._defer_to_model_kinds()

    @pytest.mark.parametrize("kind", ALL_KINDS)
    def test_registered_kinds_are_in_the_shared_spec_table(self, kind):
        """_stash_kind_specs is what gives a kind stash_is_live, the card sweep,
        the deferred-cancel claim and the button dispatcher."""
        assert kind in td._stash_kind_specs()

    def test_the_defer_set_covers_every_kind_without_an_executor(self):
        """Any kind in the spec table that try_confirm_pending_write cannot
        execute must defer. asana / shopify / delegated are the three it CAN."""
        executable = {"asana", "shopify", "delegated"}
        for kind in td._stash_kind_specs():
            if kind in executable:
                continue
            assert kind in td._defer_to_model_kinds(), (
                f"{kind} has no deterministic executor and does not defer -- a "
                f"bare 'yes' answering it can fire another kind's staler write")
