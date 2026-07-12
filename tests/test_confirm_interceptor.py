"""F-23 follow-up: the deterministic staged-write confirm interceptor.

The mega-smoke POST-MERGE log proved a haiku confirm turn can skip the destructive
tool entirely, fabricating "Confirmed -- task deleted" with ZERO tool_use (a false
success). try_confirm_pending_write executes the pending write IN CODE on a bare
affirmative -- the model is never consulted -- and cancels on a bare negative.
"""

import time
from unittest.mock import patch

import pytest

import cora.tools.tool_dispatch as td

HARRISON = "U0B2RM2JYJ1"        # founder -- gid path exempt from ownership check
TOMMY = "U0B3RU5Q55G"           # a mapped non-founder
_CH = "hjrg-leadership"


@pytest.fixture(autouse=True)
def _clear_stores():
    td._PENDING_ASANA_WRITES.clear()
    td._PENDING_SHOPIFY_WRITES.clear()
    yield
    td._PENDING_ASANA_WRITES.clear()
    td._PENDING_SHOPIFY_WRITES.clear()


# ── _confirm_intent classifier ──────────────────────────────────────────────
class TestConfirmIntent:
    @pytest.mark.parametrize("msg", [
        "yes", "Yes", "yes please", "yep", "yeah", "ok", "okay", "sure", "confirm",
        "confirmed", "do it", "go ahead", "yes, do it", "please confirm",
    ])
    def test_bare_affirmatives(self, msg):
        assert td._confirm_intent(msg, "delete") == "affirm"

    @pytest.mark.parametrize("msg", [
        "no", "nope", "cancel", "cancel it", "stop", "don't", "no thanks",
        "never mind", "no, don't",
    ])
    def test_bare_negatives(self, msg):
        assert td._confirm_intent(msg, "delete") == "negate"

    @pytest.mark.parametrize("msg", [
        "what's my cash position?", "yes but what about the invoice",
        "delete the OTHER task instead", "actually set it to 210",
        "who deleted that task", "is it done yet",
    ])
    def test_content_words_fall_through(self, msg):
        assert td._confirm_intent(msg, "delete") is None

    def test_mixed_go_and_stop_is_ambiguous(self):
        # "yes cancel it" reads both ways -> defer to the model (safe).
        assert td._confirm_intent("yes cancel it", "delete") is None

    def test_action_verb_matching_pending_is_affirm(self):
        assert td._confirm_intent("delete it", "delete") == "affirm"
        assert td._confirm_intent("complete it", "complete") == "affirm"

    def test_action_verb_conflicting_pending_is_ambiguous(self):
        # A *complete* is pending but the user says "delete the task" -> the
        # interceptor must NOT fire the complete; defer to the model.
        assert td._confirm_intent("delete the task", "complete") is None
        assert td._confirm_intent("complete the task", "delete") is None

    def test_action_verb_with_none_pending_is_affirm(self):
        # is_bare_affirmative path -- action verb counts as go when action is unknown.
        assert td._confirm_intent("delete it", None) == "affirm"

    def test_empty_and_overlong_fall_through(self):
        assert td._confirm_intent("", "delete") is None
        assert td._confirm_intent("   ", "delete") is None
        assert td._confirm_intent(" ".join(["yes"] * 11), "delete") is None

    def test_is_bare_affirmative(self):
        assert td.is_bare_affirmative("yes, delete it permanently") is True
        assert td.is_bare_affirmative("delete the SMOKE F23 CLEAN task") is False
        assert td.is_bare_affirmative("what's on my plate") is False


# ── _strip_write_sentinel ───────────────────────────────────────────────────
class TestStripSentinel:
    def test_strips_confirmed(self):
        raw = 'WRITE_CONFIRMED\n\nDeleted "X" from Asana.'
        assert td._strip_write_sentinel(raw) == 'Deleted "X" from Asana.'

    def test_strips_blocked_with_instructions(self):
        raw = "WRITE_BLOCKED -- post the lines after the blank ...\n\nNot deleted yet -- confirm."
        assert td._strip_write_sentinel(raw) == "Not deleted yet -- confirm."

    def test_passthrough_non_sentinel(self):
        assert td._strip_write_sentinel("plain text") == "plain text"


# ── try_confirm_pending_write ───────────────────────────────────────────────
class TestInterceptorAsana:
    def _stash_delete(self, gid="g1", label="SMOKE F23 CLEAN"):
        td._store_pending_asana_write(HARRISON, _CH, {
            "action": "delete", "gid": gid, "label": label, "ts": time.time(),
        })

    def test_affirmative_executes_delete_of_stashed_gid(self):
        self._stash_delete(gid="g1")
        with patch.object(td.asana_client, "delete_task", return_value=None) as mock:
            out = td.try_confirm_pending_write(
                slack_user_id=HARRISON, channel_name=_CH, entity="FNDR",
                message="yes, delete it permanently",
            )
        mock.assert_called_once_with("g1")            # the STASHED gid, executed in code
        assert out and "deleted" in out.lower()
        assert "WRITE_CONFIRMED" not in out           # sentinel stripped
        assert not td.has_pending_asana_write(HARRISON, _CH)  # consumed

    def test_affirmative_executes_complete(self):
        td._store_pending_asana_write(HARRISON, _CH, {
            "action": "complete", "gid": "g2", "label": "Send the deck", "ts": time.time(),
        })
        with patch.object(td.asana_client, "complete_task", return_value={}) as mock:
            out = td.try_confirm_pending_write(
                slack_user_id=HARRISON, channel_name=_CH, entity="FNDR", message="yes",
            )
        mock.assert_called_once_with("g2")
        assert "complete" in out.lower()

    def test_negative_cancels_without_executing(self):
        self._stash_delete()
        with patch.object(td.asana_client, "delete_task") as mock:
            out = td.try_confirm_pending_write(
                slack_user_id=HARRISON, channel_name=_CH, entity="FNDR", message="no, cancel it",
            )
        mock.assert_not_called()
        assert out == td._CONFIRM_CANCELLED_REPLY
        assert not td.has_pending_asana_write(HARRISON, _CH)  # popped

    def test_no_pending_returns_none(self):
        with patch.object(td.asana_client, "delete_task") as mock:
            out = td.try_confirm_pending_write(
                slack_user_id=HARRISON, channel_name=_CH, entity="FNDR", message="yes",
            )
        assert out is None
        mock.assert_not_called()

    def test_ambiguous_message_leaves_pending_intact(self):
        self._stash_delete()
        with patch.object(td.asana_client, "delete_task") as mock:
            out = td.try_confirm_pending_write(
                slack_user_id=HARRISON, channel_name=_CH, entity="FNDR",
                message="what's my cash position?",
            )
        assert out is None
        mock.assert_not_called()
        assert td.has_pending_asana_write(HARRISON, _CH)  # NOT consumed

    def test_ttl_expired_pending_returns_none(self):
        td._store_pending_asana_write(HARRISON, _CH, {
            "action": "delete", "gid": "g1", "label": "X",
            "ts": time.time() - (td._ASANA_PENDING_TTL_SECONDS + 5),
        })
        with patch.object(td.asana_client, "delete_task") as mock:
            out = td.try_confirm_pending_write(
                slack_user_id=HARRISON, channel_name=_CH, entity="FNDR", message="yes",
            )
        assert out is None
        mock.assert_not_called()

    def test_action_conflict_defers_to_model(self):
        # complete pending, but the user says "delete the task" -> must NOT complete.
        td._store_pending_asana_write(HARRISON, _CH, {
            "action": "complete", "gid": "g2", "label": "X", "ts": time.time(),
        })
        with patch.object(td.asana_client, "complete_task") as mc, \
             patch.object(td.asana_client, "delete_task") as md:
            out = td.try_confirm_pending_write(
                slack_user_id=HARRISON, channel_name=_CH, entity="FNDR",
                message="delete the task",
            )
        assert out is None
        mc.assert_not_called()
        md.assert_not_called()

    def test_no_user_id_returns_none(self):
        self._stash_delete()
        out = td.try_confirm_pending_write(
            slack_user_id="", channel_name=_CH, entity="FNDR", message="yes",
        )
        assert out is None


class TestInterceptorShopify:
    def test_affirmative_routes_to_shopify_tool_with_confirmed(self):
        td._store_pending_shopify_write(HARRISON, _CH, {
            "inventory_item_id": "i1", "location_id": "l1", "target_qty": 203,
            "preview_qty": 202, "variant_label": "Pure", "location_label": "Office",
            "ts": time.time(),
        })
        confirmed = ("WRITE_CONFIRMED -- post the line after the blank ...:\n\n"
                     "DTC inventory updated -- Pure at Office: 202 -> 203 units.")
        with patch.object(td, "_tool_f3e_shopify_set_inventory", return_value=confirmed) as mock:
            out = td.try_confirm_pending_write(
                slack_user_id=HARRISON, channel_name=_CH, entity="F3E", message="confirm",
            )
        mock.assert_called_once()
        # called with confirmed=True + the injected channel
        assert mock.call_args.args[2]["confirmed"] is True
        assert mock.call_args.args[2]["_channel_name"] == _CH
        assert out == "DTC inventory updated -- Pure at Office: 202 -> 203 units."

    def test_freshest_pending_wins_across_stores(self):
        # Both stores hold an entry; the interceptor acts on the fresher one (shopify).
        td._store_pending_asana_write(HARRISON, _CH, {
            "action": "delete", "gid": "g1", "label": "X", "ts": time.time() - 60,
        })
        td._store_pending_shopify_write(HARRISON, _CH, {
            "inventory_item_id": "i1", "location_id": "l1", "target_qty": 5,
            "preview_qty": 4, "variant_label": "V", "location_label": "Office",
            "ts": time.time(),
        })
        with patch.object(td, "_tool_f3e_shopify_set_inventory",
                          return_value="WRITE_CONFIRMED\n\nDTC inventory updated.") as ms, \
             patch.object(td.asana_client, "delete_task") as md:
            out = td.try_confirm_pending_write(
                slack_user_id=HARRISON, channel_name=_CH, entity="F3E", message="yes",
            )
        ms.assert_called_once()
        md.assert_not_called()
        assert out == "DTC inventory updated."
