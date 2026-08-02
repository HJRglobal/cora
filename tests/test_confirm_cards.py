"""Unit tests for src.cora.confirm_cards -- the shared stash-id mint/index +
Block Kit rendering layer for Slack interactive Confirm/Cancel + picker
buttons (design 2026-08-02)."""

import os
import time
from unittest.mock import patch

import pytest

from cora import confirm_cards as cc


@pytest.fixture(autouse=True)
def _clear_indexes():
    with cc._INDEX_LOCK:
        cc._INDEX.clear()
    with cc._ASK_INDEX_LOCK:
        cc._ASK_INDEX.clear()
    yield
    with cc._INDEX_LOCK:
        cc._INDEX.clear()
    with cc._ASK_INDEX_LOCK:
        cc._ASK_INDEX.clear()


class TestConfirmButtonsFlag:
    def test_default_off(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CORA_CONFIRM_BUTTONS", None)
            assert cc.confirm_buttons_enabled() is False

    def test_on(self):
        with patch.dict(os.environ, {"CORA_CONFIRM_BUTTONS": "on"}):
            assert cc.confirm_buttons_enabled() is True

    def test_case_insensitive(self):
        with patch.dict(os.environ, {"CORA_CONFIRM_BUTTONS": "ON"}):
            assert cc.confirm_buttons_enabled() is True

    @pytest.mark.parametrize("val", ["off", "true", "1", "yes", "", "garbage"])
    def test_anything_else_is_off(self, val):
        with patch.dict(os.environ, {"CORA_CONFIRM_BUTTONS": val}):
            assert cc.confirm_buttons_enabled() is False


class TestMintStashId:
    def test_mints_16_hex_chars(self):
        sid = cc.mint_stash_id("asana", "U1", "c1")
        assert len(sid) == 16
        int(sid, 16)  # valid hex

    def test_two_mints_are_unique(self):
        a = cc.mint_stash_id("asana", "U1", "c1")
        b = cc.mint_stash_id("asana", "U1", "c1")
        assert a != b

    def test_index_lookup_returns_kind_user_channel(self):
        sid = cc.mint_stash_id("shopify", "U1", "c1")
        entry = cc.index_lookup(sid)
        # turn_id is None outside any begin_turn() scope (S1 fix) -- see
        # TestTurnProvenance below for the turn-tagging behavior itself.
        assert entry == {"kind": "shopify", "user": "U1", "channel": "c1", "ts": entry["ts"],
                         "turn_id": None}

    def test_index_lookup_unknown_id_is_none(self):
        assert cc.index_lookup("0" * 16) is None

    def test_index_release_removes_entry(self):
        sid = cc.mint_stash_id("asana", "U1", "c1")
        cc.index_release(sid)
        assert cc.index_lookup(sid) is None

    def test_index_release_unknown_id_is_a_noop(self):
        cc.index_release("nonexistent")  # must not raise

    def test_index_lookup_returns_a_copy_not_the_live_dict(self):
        # Mutating the returned dict must never corrupt the index.
        sid = cc.mint_stash_id("asana", "U1", "c1")
        entry = cc.index_lookup(sid)
        entry["user"] = "ATTACKER"
        assert cc.index_lookup(sid)["user"] == "U1"

    def test_stale_entries_pruned_on_next_mint(self):
        sid_old = cc.mint_stash_id("asana", "U1", "c1")
        with cc._INDEX_LOCK:
            cc._INDEX[sid_old]["ts"] = time.time() - cc.INDEX_GRACE_SECONDS - 1
        cc.mint_stash_id("asana", "U2", "c2")  # triggers the opportunistic prune
        assert cc.index_lookup(sid_old) is None


class TestAskIndex:
    def test_mint_and_lookup(self):
        aid = cc.mint_ask_id("U1", "c1")
        entry = cc.ask_index_lookup(aid)
        assert entry["user"] == "U1" and entry["channel"] == "c1"

    def test_unique_ids(self):
        a = cc.mint_ask_id("U1", "c1")
        b = cc.mint_ask_id("U1", "c1")
        assert a != b

    def test_release_removes(self):
        aid = cc.mint_ask_id("U1", "c1")
        cc.ask_index_release(aid)
        assert cc.ask_index_lookup(aid) is None

    def test_ask_index_is_separate_namespace_from_confirm_index(self):
        # An ask_id and a stash_id are drawn from the same token space
        # (secrets.token_hex(8)) but live in DIFFERENT dicts -- a stash_id
        # must never resolve via ask_index_lookup and vice versa.
        sid = cc.mint_stash_id("asana", "U1", "c1")
        assert cc.ask_index_lookup(sid) is None
        aid = cc.mint_ask_id("U1", "c1")
        assert cc.index_lookup(aid) is None


class TestBuildConfirmBlocks:
    def test_shape(self):
        blocks = cc.build_confirm_blocks("Preview text", "abc123")
        assert blocks[0]["type"] == "section"
        assert blocks[0]["text"]["text"] == "Preview text"
        actions = blocks[1]
        assert actions["type"] == "actions"
        assert len(actions["elements"]) == 2

    def test_button_values_are_stash_id_only(self):
        # Security invariant #1: button value carries ONLY the opaque
        # stash_id -- never a payload, never preview text.
        blocks = cc.build_confirm_blocks("Preview text with secrets", "deadbeef01234567")
        for el in blocks[1]["elements"]:
            assert el["value"] == "deadbeef01234567"
            assert "secrets" not in el["value"]

    def test_confirm_and_cancel_action_ids_present(self):
        blocks = cc.build_confirm_blocks("x", "abc")
        action_ids = {el["action_id"] for el in blocks[1]["elements"]}
        assert action_ids == {cc.ACTION_CONFIRM, cc.ACTION_CANCEL}


class TestBuildPickerBlocks:
    def test_shape_and_values(self):
        blocks = cc.build_picker_blocks(
            "Which one?", "askid123",
            [("0", "Pure Original"), ("1", "Pure Variety")],
        )
        actions = blocks[1]
        assert len(actions["elements"]) == 2
        values = {el["value"] for el in actions["elements"]}
        assert values == {"askid123:0", "askid123:1"}

    def test_all_buttons_are_pick_action(self):
        blocks = cc.build_picker_blocks("Which?", "a1", [("0", "X")])
        assert all(el["action_id"] == cc.ACTION_PICK for el in blocks[1]["elements"])

    def test_long_label_truncated(self):
        long_label = "X" * 200
        blocks = cc.build_picker_blocks("Which?", "a1", [("0", long_label)])
        text = blocks[1]["elements"][0]["text"]["text"]
        assert len(text) <= cc._BTN_LABEL_MAX


class TestTerminalBlocks:
    def test_no_actions_block(self):
        blocks = cc.terminal_blocks("Cancelled -- nothing was changed.")
        assert all(b.get("type") != "actions" for b in blocks)
        assert blocks[0]["text"]["text"] == "Cancelled -- nothing was changed."


class TestTurnProvenance:
    """S1 fix (cq-883878e81274): mint_stash_id/mint_ask_id tag every entry
    with the currently-active turn (begin_turn/current_turn_id)."""

    def test_no_active_turn_stamps_none(self):
        sid = cc.mint_stash_id("asana", "U1", "c1")
        assert cc.index_lookup(sid)["turn_id"] is None

    def test_begin_turn_stamps_the_fresh_id(self):
        cc.begin_turn()
        tid = cc.current_turn_id()
        assert tid is not None
        sid = cc.mint_stash_id("asana", "U1", "c1")
        assert cc.index_lookup(sid)["turn_id"] == tid

    def test_begin_turn_mints_a_fresh_id_each_call(self):
        cc.begin_turn()
        first = cc.current_turn_id()
        cc.begin_turn()
        second = cc.current_turn_id()
        assert first != second

    def test_ask_id_also_stamped(self):
        cc.begin_turn()
        tid = cc.current_turn_id()
        aid = cc.mint_ask_id("U1", "c1")
        assert cc.ask_index_lookup(aid)["turn_id"] == tid
