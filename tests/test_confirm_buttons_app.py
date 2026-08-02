"""app.py Slack action-handler wiring for the interactive Confirm/Cancel/Pick
buttons (design 2026-08-02). A green tool_dispatch-level suite doesn't prove
the @app.action wrappers route the click, check the flag, and edit the
Slack card in place -- these drive the handlers with a fake Slack client,
mirroring the test_catchup_one_tap_handler.py convention.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from cora import app as capp
from cora import confirm_cards as cc
from cora.tools import tool_dispatch as td

HARRISON = "U0B2RM2JYJ1"
ATTACKER = "U0BATTACKER1"
_CH = "cora-build"
_CHANNEL_ID = "C1"


class _FakeClient:
    def __init__(self):
        self.updated: list[dict] = []
        self.ephemeral: list[dict] = []
        self.posted: list[dict] = []

    def chat_update(self, **kw):
        self.updated.append(kw)
        return {"ok": True}

    def chat_postEphemeral(self, **kw):
        self.ephemeral.append(kw)
        return {"ok": True}

    def chat_postMessage(self, **kw):
        self.posted.append(kw)
        return {"ok": True, "ts": "999.9"}


@pytest.fixture(autouse=True)
def _clear_stores(monkeypatch):
    monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "on")
    td._PENDING_ASANA_WRITES.clear()
    td._PENDING_SHOPIFY_WRITES.clear()
    td._PENDING_ASK_STASH.clear()
    with cc._INDEX_LOCK:
        cc._INDEX.clear()
    with cc._ASK_INDEX_LOCK:
        cc._ASK_INDEX.clear()
    yield
    td._PENDING_ASANA_WRITES.clear()
    td._PENDING_SHOPIFY_WRITES.clear()
    td._PENDING_ASK_STASH.clear()


def _stash_asana_delete(user=HARRISON, channel=_CH, gid="g1", label="Test task"):
    sid = cc.mint_stash_id("asana", user, channel)
    td._store_pending_asana_write(user, channel, {
        "action": "delete", "gid": gid, "label": label, "ts": time.time(), "stash_id": sid,
    })
    return sid


def _confirm_body(user_id, stash_id, action_id):
    return {
        "user": {"id": user_id},
        "channel": {"id": _CHANNEL_ID},
        "message": {"ts": "1780000000.0001", "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": "Not deleted yet -- reply to confirm..."}},
            {"type": "actions", "elements": [{"type": "button", "action_id": action_id, "value": stash_id}]},
        ]},
        "actions": [{"action_id": action_id, "value": stash_id}],
    }


class TestConfirmTap:
    def test_confirm_executes_and_edits_card_in_place(self):
        sid = _stash_asana_delete()
        fake = _FakeClient()
        with patch.object(td.asana_client, "delete_task", return_value=None) as mock:
            capp._handle_confirm_tap(_confirm_body(HARRISON, sid, cc.ACTION_CONFIRM),
                                     fake, action="confirm")
        mock.assert_called_once_with("g1")
        assert len(fake.updated) == 1
        assert fake.updated[0]["channel"] == _CHANNEL_ID
        # Buttons dropped on the terminal card.
        assert all(b.get("type") != "actions" for b in fake.updated[0]["blocks"])
        assert "deleted" in fake.updated[0]["text"].lower()
        assert not fake.ephemeral

    def test_cancel_pops_without_executing_and_edits_card(self):
        sid = _stash_asana_delete()
        fake = _FakeClient()
        with patch.object(td.asana_client, "delete_task") as mock:
            capp._handle_confirm_tap(_confirm_body(HARRISON, sid, cc.ACTION_CANCEL),
                                     fake, action="cancel")
        mock.assert_not_called()
        assert "Cancelled" in fake.updated[0]["text"]

    def test_cross_user_tap_gets_ephemeral_refusal_card_untouched(self):
        sid = _stash_asana_delete(user=HARRISON)
        fake = _FakeClient()
        with patch.object(td.asana_client, "delete_task") as mock:
            capp._handle_confirm_tap(_confirm_body(ATTACKER, sid, cc.ACTION_CONFIRM),
                                     fake, action="confirm")
        mock.assert_not_called()
        assert not fake.updated                    # card NOT mutated
        assert len(fake.ephemeral) == 1
        assert fake.ephemeral[0]["user"] == ATTACKER
        assert HARRISON in fake.ephemeral[0]["text"]  # names the real owner

    def test_orphaned_tap_gets_ephemeral_only(self):
        fake = _FakeClient()
        capp._handle_confirm_tap(_confirm_body(HARRISON, "f" * 16, cc.ACTION_CONFIRM),
                                 fake, action="confirm")
        assert not fake.updated
        assert len(fake.ephemeral) == 1

    def test_double_tap_second_reads_already_handled(self):
        sid = _stash_asana_delete()
        fake = _FakeClient()
        with patch.object(td.asana_client, "delete_task", return_value=None):
            capp._handle_confirm_tap(_confirm_body(HARRISON, sid, cc.ACTION_CONFIRM),
                                     fake, action="confirm")
            capp._handle_confirm_tap(_confirm_body(HARRISON, sid, cc.ACTION_CONFIRM),
                                     fake, action="confirm")
        assert len(fake.updated) == 2
        assert "Already handled" in fake.updated[1]["text"]

    def test_flag_off_never_mutates_card_ephemeral_nudge_only(self, monkeypatch):
        monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "off")
        sid = _stash_asana_delete()
        fake = _FakeClient()
        with patch.object(td.asana_client, "delete_task") as mock:
            capp._handle_confirm_tap(_confirm_body(HARRISON, sid, cc.ACTION_CONFIRM),
                                     fake, action="confirm")
        mock.assert_not_called()
        assert not fake.updated
        assert len(fake.ephemeral) == 1
        # The stash is UNTOUCHED -- the typed path must still work after this.
        assert td._peek_pending_asana(HARRISON, _CH) is not None

    def test_drift_repreview_closes_old_card_and_posts_fresh_confirm_card(self):
        # D-051 review fix: a confirm tap whose execute re-stashed a fresh
        # preview (Shopify drift) must close the OLD card honestly AND post a
        # NEW Confirm/Cancel card -- never leave a live pending un-carded.
        sid = cc.mint_stash_id("shopify", HARRISON, _CH)
        td._store_pending_shopify_write(HARRISON, _CH, {
            "inventory_item_id": "i1", "location_id": "l1", "target_qty": 10,
            "preview_qty": 8, "delta": 2, "unit": "units", "variant_label": "Pure",
            "location_label": "Office", "resolved_from": "", "lex": None,
            "ts": time.time(), "stash_id": sid,
        })
        fake = _FakeClient()
        with patch.object(td.shopify_client, "get_inventory_level", return_value=99), \
             patch.object(td.shopify_client, "set_inventory_level") as mock_set, \
             patch.object(td, "_load_shopify_write_config", return_value=({"office"}, {})):
            capp._handle_confirm_tap(_confirm_body(HARRISON, sid, cc.ACTION_CONFIRM),
                                     fake, action="confirm")
        mock_set.assert_not_called()
        # Old card closed (terminal, no buttons).
        assert len(fake.updated) == 1
        assert all(b.get("type") != "actions" for b in fake.updated[0]["blocks"])
        # A FRESH Confirm/Cancel card posted for the re-preview.
        assert len(fake.posted) == 1
        posted_blocks = fake.posted[0]["blocks"]
        actions_block = next(b for b in posted_blocks if b["type"] == "actions")
        action_ids = {el["action_id"] for el in actions_block["elements"]}
        assert action_ids == {cc.ACTION_CONFIRM, cc.ACTION_CANCEL}
        new_stash_id = actions_block["elements"][0]["value"]
        assert new_stash_id != sid
        assert td._peek_pending_shopify(HARRISON, _CH)["stash_id"] == new_stash_id

    def test_indeterminate_shows_check_instruction(self):
        sid = _stash_asana_delete()
        fake = _FakeClient()
        with patch.object(td, "_execute_claimed_stash", side_effect=RuntimeError("boom")):
            capp._handle_confirm_tap(_confirm_body(HARRISON, sid, cc.ACTION_CONFIRM),
                                     fake, action="confirm")
        assert "can't confirm either way" in fake.updated[0]["text"].lower() \
            or "cannot confirm" in fake.updated[0]["text"].lower() \
            or "can not confirm" in fake.updated[0]["text"].lower()

    def test_missing_value_is_a_noop(self):
        fake = _FakeClient()
        body = _confirm_body(HARRISON, "", cc.ACTION_CONFIRM)
        capp._handle_confirm_tap(body, fake, action="confirm")
        assert not fake.updated and not fake.ephemeral and not fake.posted

    def test_action_ids_registered(self):
        assert callable(capp.handle_confirm_write)
        assert callable(capp.handle_cancel_write)
        assert callable(capp.handle_pick_candidate)


class TestPickTap:
    def _stash_variant_ask(self, user=HARRISON, channel=_CH):
        aid = cc.mint_ask_id(user, channel)
        td._store_pending_ask(user, channel, {
            "ask_id": aid, "ask_kind": "variant", "loc_id": "l1", "loc_name": "Office",
            "unit": "units", "quantity": 5, "delta": None, "product_query": "",
            "candidates": [
                ("0", "Pure Original", {"product_title": "Pure Original", "variant_title": "",
                                       "sku": "SKU1", "variant_id": 1, "inventory_item_id": "i1"}),
                ("1", "Pure Variety", {"product_title": "Pure Variety", "variant_title": "",
                                      "sku": "SKU2", "variant_id": 2, "inventory_item_id": "i2"}),
            ],
            "ts": time.time(),
        })
        return aid

    def _pick_body(self, user_id, value):
        return {
            "user": {"id": user_id},
            "channel": {"id": _CHANNEL_ID},
            "message": {"ts": "1780000000.0002", "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": "Which one?"}},
                {"type": "actions", "elements": [{"type": "button", "action_id": cc.ACTION_PICK, "value": value}]},
            ]},
            "actions": [{"action_id": cc.ACTION_PICK, "value": value}],
        }

    def test_pick_closes_picker_and_posts_fresh_confirm_card(self):
        aid = self._stash_variant_ask()
        fake = _FakeClient()
        with patch.object(td.shopify_client, "get_inventory_level", return_value=5), \
             patch.object(td, "_load_shopify_write_config", return_value=({"office"}, {})):
            capp._handle_pick_tap(self._pick_body(HARRISON, f"{aid}:0"), fake)
        # Picker card closed (terminal, no actions block).
        assert len(fake.updated) == 1
        assert all(b.get("type") != "actions" for b in fake.updated[0]["blocks"])
        assert fake.updated[0]["text"] == "Picked."
        # A FRESH Confirm/Cancel card posted for the resulting preview.
        assert len(fake.posted) == 1
        posted_blocks = fake.posted[0]["blocks"]
        actions_block = next(b for b in posted_blocks if b["type"] == "actions")
        action_ids = {el["action_id"] for el in actions_block["elements"]}
        assert action_ids == {cc.ACTION_CONFIRM, cc.ACTION_CANCEL}
        # The bound button value is a stash_id, resolvable back to a fresh
        # Shopify pending for THIS user/channel -- never the picked label.
        new_stash_id = actions_block["elements"][0]["value"]
        pending = td._peek_pending_shopify(HARRISON, _CH)
        assert pending is not None and pending["stash_id"] == new_stash_id

    def test_pick_flag_off_ephemeral_nudge_only(self, monkeypatch):
        monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "off")
        aid = self._stash_variant_ask()
        fake = _FakeClient()
        capp._handle_pick_tap(self._pick_body(HARRISON, f"{aid}:0"), fake)
        assert not fake.updated and not fake.posted
        assert len(fake.ephemeral) == 1

    def test_pick_never_round_trips_through_model(self):
        # The candidate's bound VALUE (a VariantMatch field dict) is looked up
        # server-side entirely from the stash -- the tap body carries only
        # "{ask_id}:{candidate_key}", never a product name/SKU string.
        aid = self._stash_variant_ask()
        body = self._pick_body(HARRISON, f"{aid}:1")
        raw_value = body["actions"][0]["value"]
        assert "Pure" not in raw_value and "SKU" not in raw_value

    def test_cross_user_pick_refused(self):
        aid = self._stash_variant_ask(user=HARRISON)
        fake = _FakeClient()
        outcome, owner, _sid = td.resolve_shopify_ask_pick(aid, ATTACKER, "0")
        assert outcome == "unauthorized"
        assert owner == HARRISON

    def test_invalid_candidate_key_refused(self):
        aid = self._stash_variant_ask()
        outcome, _msg, sid = td.resolve_shopify_ask_pick(aid, HARRISON, "99")
        assert outcome == "invalid_candidate"
        assert sid is None

    def test_eval_mode_never_resolves_a_pick(self, monkeypatch):
        aid = self._stash_variant_ask()
        monkeypatch.setenv("CORA_EVAL_MODE", "1")
        outcome, _msg, sid = td.resolve_shopify_ask_pick(aid, HARRISON, "0")
        assert outcome == "orphaned"
        assert sid is None
        # Untouched -- eval mode must not even claim the ask.
        assert td._peek_pending_ask(HARRISON, _CH) is not None
