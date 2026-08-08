"""v2 S2: typed-confirm arbitration + Sonnet-force parity for the three stash
kinds added last, and per-slot meeting buttons.

remember / forget_note / schedule_meeting minted pendings that
try_confirm_pending_write never peeked, so a bare "yes" answering ONE OF THEIR
previews fell through to the freshest of the six kinds it DID peek and fired a
staler Asana/Shopify write instead. Same class as the 8/6 code-queue fix, left
open for three more kinds.

Also pins: has_pending_schedule_meeting joins the Sonnet-force chain, and the
schedule_meeting card renders one button per OFFERED slot (v1's single Confirm
always booked slots[0] while the typed path allowed any of the up-to-3 options).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test-signing-secret")

import pytest  # noqa: E402

import cora.app as app_mod  # noqa: E402
from cora import confirm_cards as cc  # noqa: E402
from cora.tools import tool_dispatch as td  # noqa: E402

USER_ID = "U0ARB"
CHANNEL = "f3e-leadership"

_SLOTS = [
    ("2026-08-11T09:00:00-07:00", "2026-08-11T09:30:00-07:00"),
    ("2026-08-11T14:00:00-07:00", "2026-08-11T14:30:00-07:00"),
    ("2026-08-12T10:00:00-07:00", "2026-08-12T10:30:00-07:00"),
]
_LABELS = ["Monday, August 11, 9:00 AM -- 9:30 AM AZ",
           "Monday, August 11, 2:00 PM -- 2:30 PM AZ",
           "Tuesday, August 12, 10:00 AM -- 10:30 AM AZ"]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "on")
    monkeypatch.delenv("CORA_EVAL_MODE", raising=False)
    cc.reset_cards_for_tests()
    with cc._INDEX_LOCK:
        cc._INDEX.clear()
    for store in (td._PENDING_ASANA_WRITES, td._PENDING_SHOPIFY_WRITES,
                  td._PENDING_REMEMBER, td._PENDING_FORGET_NOTE,
                  td._PENDING_SCHEDULE_MEETING):
        store.clear()
    yield
    for store in (td._PENDING_ASANA_WRITES, td._PENDING_SHOPIFY_WRITES,
                  td._PENDING_REMEMBER, td._PENDING_FORGET_NOTE,
                  td._PENDING_SCHEDULE_MEETING):
        store.clear()
    cc.reset_cards_for_tests()


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


def _fresh_remember() -> str:
    sid = cc.mint_stash_id("remember", USER_ID, CHANNEL)
    td._store_pending_remember(USER_ID, CHANNEL, {
        "content": "the cobalt falcon is the staging box",
        "scope": "FNDR", "ts": time.time(), "stash_id": sid,
    })
    return sid


def _fresh_forget() -> str:
    sid = cc.mint_stash_id("forget_note", USER_ID, CHANNEL)
    td._store_pending_forget_note(USER_ID, CHANNEL, {
        "note_id": 7, "preview": "old note", "ts": time.time(), "stash_id": sid,
    })
    return sid


def _fresh_schedule_meeting(slots=None, labels=None) -> str:
    sid = cc.mint_stash_id("schedule_meeting", USER_ID, CHANNEL)
    td._store_pending_schedule_meeting(USER_ID, CHANNEL, {
        "requester_email": "h@hjrglobal.com", "requester_name": "Harrison",
        "title": "Sync", "names": ["Harrison", "Tommy"],
        "emails": ["h@hjrglobal.com", "t@f3energy.com"],
        "slots": _SLOTS if slots is None else slots,
        "slot_labels": _LABELS if labels is None else labels,
        "ts": time.time(), "stash_id": sid,
    })
    return sid


# ── the arbitration gap ────────────────────────────────────────────────────


class TestPeekSetParity:
    """A bare affirmative answering one of the three newest kinds' previews must
    DEFER to the model, never fire a staler write from another kind."""

    @pytest.mark.parametrize("mint_fresh", [
        _fresh_remember, _fresh_forget, _fresh_schedule_meeting,
    ])
    def test_bare_yes_never_fires_a_staler_shopify_write(self, mint_fresh):
        stale = _stale_shopify()
        fresh = mint_fresh()

        with patch.object(td, "_run_confirm_execute") as ex:
            reply = td.try_confirm_pending_write(
                slack_user_id=USER_ID, channel_name=CHANNEL, entity="F3E",
                message="yes",
            )

        ex.assert_not_called()
        assert reply is None, "must defer to the model, not answer deterministically"
        assert td.stash_is_live(stale), "the staler write must be left untouched"
        assert td.stash_is_live(fresh), "the fresh pending survives for the tool"

    @pytest.mark.parametrize("mint_fresh", [
        _fresh_remember, _fresh_forget, _fresh_schedule_meeting,
    ])
    def test_bare_yes_never_fires_a_staler_asana_delete(self, mint_fresh):
        _stale_asana_delete()
        fresh = mint_fresh()

        with patch.object(td, "_run_confirm_execute") as ex:
            reply = td.try_confirm_pending_write(
                slack_user_id=USER_ID, channel_name=CHANNEL, entity="F3E",
                message="yes",
            )

        ex.assert_not_called()
        assert reply is None
        assert td.stash_is_live(fresh)

    def test_freshest_first_still_holds_between_the_new_kinds(self):
        """The newest of the three wins the arbitration (and defers), rather
        than whichever happens to be peeked first."""
        _fresh_remember()
        time.sleep(0.01)
        newest = _fresh_schedule_meeting()

        with patch.object(td, "_run_confirm_execute") as ex:
            assert td.try_confirm_pending_write(
                slack_user_id=USER_ID, channel_name=CHANNEL, entity="F3E",
                message="yes") is None
        ex.assert_not_called()
        assert td.stash_is_live(newest)

    def test_a_stale_new_kind_does_not_block_a_fresher_shopify_confirm(self):
        """Parity in the other direction: an OLD remember pending must not stop
        a genuinely fresher Shopify confirm from executing deterministically."""
        _fresh_remember()
        td._PENDING_REMEMBER[td._remember_pending_key(USER_ID, CHANNEL)]["ts"] = time.time() - 60
        _stale_shopify(age=0.0)

        with patch.object(td, "_run_confirm_execute", return_value="Set.") as ex:
            reply = td.try_confirm_pending_write(
                slack_user_id=USER_ID, channel_name=CHANNEL, entity="F3E",
                message="yes")
        ex.assert_called_once()
        assert reply == "Set."


class TestSonnetForceParity:
    def test_has_pending_schedule_meeting_true_when_fresh(self):
        _fresh_schedule_meeting()
        assert td.has_pending_schedule_meeting(USER_ID, CHANNEL) is True

    def test_has_pending_schedule_meeting_false_when_absent(self):
        assert td.has_pending_schedule_meeting(USER_ID, CHANNEL) is False

    def test_has_pending_schedule_meeting_false_when_expired(self):
        _fresh_schedule_meeting()
        key = td._schedule_meeting_pending_key(USER_ID, CHANNEL)
        td._PENDING_SCHEDULE_MEETING[key]["ts"] = (
            time.time() - td._SCHEDULE_MEETING_PENDING_TTL_SECONDS - 5)
        assert td.has_pending_schedule_meeting(USER_ID, CHANNEL) is False

    def test_it_is_wired_into_the_escalation_chain(self):
        src = (_REPO_ROOT / "src" / "cora" / "app.py").read_text(encoding="utf-8")
        assert "has_pending_schedule_meeting(user_id, channel_name)" in src, \
            "schedule_meeting must join the Sonnet-force OR-chain"

    def test_every_stash_kind_is_in_the_escalation_chain(self):
        """Drift guard: a NEW staged-write kind must join the chain too."""
        src = (_REPO_ROOT / "src" / "cora" / "app.py").read_text(encoding="utf-8")
        # calendar/asana/shopify/delegated use has_pending_*_write naming.
        for probe in ("has_pending_shopify_write", "has_pending_calendar_write",
                      "has_pending_asana_write", "has_pending_delegated_write",
                      "has_pending_remember", "has_pending_forget_note",
                      "has_pending_code_queue", "has_pending_schedule_meeting"):
            assert probe in src, f"{probe} missing from the Sonnet-force chain"


# ── per-slot meeting buttons ───────────────────────────────────────────────


class TestSlotPickerCard:
    def test_one_button_per_offered_slot_plus_cancel(self):
        blocks = cc.build_slot_picker_blocks("Pick a time", "sid1", _LABELS)
        elements = blocks[1]["elements"]
        assert len(elements) == 4  # 3 slots + Cancel
        assert [e["action_id"] for e in elements[:3]] == [cc.ACTION_PICK_SLOT] * 3
        assert [e["value"] for e in elements[:3]] == ["sid1:0", "sid1:1", "sid1:2"]
        assert elements[3]["action_id"] == cc.ACTION_CANCEL
        assert elements[3]["value"] == "sid1"

    def test_slot_buttons_are_capped_at_three(self):
        blocks = cc.build_slot_picker_blocks("Pick", "sid1", _LABELS + ["a 4th"])
        assert len([e for e in blocks[1]["elements"]
                    if e["action_id"] == cc.ACTION_PICK_SLOT]) == 3

    def test_long_labels_are_truncated_to_slacks_limit(self):
        blocks = cc.build_slot_picker_blocks("Pick", "sid1", ["x" * 200])
        label = blocks[1]["elements"][0]["text"]["text"]
        assert len(label) <= 75 and label.endswith("...")

    def test_a_single_slot_still_renders_a_picker(self):
        blocks = cc.build_slot_picker_blocks("Pick", "sid1", [_LABELS[0]])
        assert len(blocks[1]["elements"]) == 2  # 1 slot + Cancel


class TestSlotTapBooksTheChosenSlot:
    def _tap(self, client, value: str, stash_id: str):
        body = {
            "user": {"id": USER_ID}, "channel": {"id": "C0T"},
            "message": {"ts": "1.1", "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": "Pick a time"}},
                {"type": "actions", "elements": []},
            ]},
            "actions": [{"value": value}],
        }
        ack = MagicMock()
        with patch("cora.entity_router.route", return_value="F3E"):
            app_mod.handle_pick_slot(ack, body, client)
        ack.assert_called_once()

    def test_tapping_slot_2_books_slot_2(self):
        sid = _fresh_schedule_meeting()
        client = MagicMock()
        with patch.object(td, "_execute_claimed_schedule_meeting",
                          return_value="Booked.") as book:
            self._tap(client, f"{sid}:1", sid)
        book.assert_called_once()
        args = book.call_args.args
        assert args[2] == _SLOTS[1][0] and args[3] == _SLOTS[1][1], \
            "the tapped slot, not slots[0], must be booked"

    def test_tapping_slot_1_books_slot_1(self):
        sid = _fresh_schedule_meeting()
        with patch.object(td, "_execute_claimed_schedule_meeting",
                          return_value="Booked.") as book:
            self._tap(MagicMock(), f"{sid}:0", sid)
        assert book.call_args.args[2] == _SLOTS[0][0]

    def test_out_of_range_index_books_nothing(self):
        sid = _fresh_schedule_meeting()
        client = MagicMock()
        with patch.object(td, "_execute_claimed_schedule_meeting") as book:
            self._tap(client, f"{sid}:9", sid)
        book.assert_not_called()
        assert "no longer one of the times" in client.chat_update.call_args.kwargs["text"]

    def test_malformed_value_is_ignored_entirely(self):
        sid = _fresh_schedule_meeting()
        client = MagicMock()
        with patch.object(td, "_execute_claimed_schedule_meeting") as book:
            self._tap(client, sid, sid)          # no ":idx"
            self._tap(client, f"{sid}:abc", sid)  # non-numeric
        book.assert_not_called()
        client.chat_update.assert_not_called()
        assert td.stash_is_live(sid), "a malformed tap must not consume the stash"

    def test_cancel_button_on_a_slot_card_cancels(self):
        sid = _fresh_schedule_meeting()
        client = MagicMock()
        body = {
            "user": {"id": USER_ID}, "channel": {"id": "C0T"},
            "message": {"ts": "1.1", "blocks": []},
            "actions": [{"value": sid}],
        }
        app_mod._handle_confirm_tap(body, client, action="cancel")
        assert not td.stash_is_live(sid)
        assert "Cancelled" in client.chat_update.call_args.kwargs["text"]

    def test_a_non_requester_cannot_tap_a_slot(self):
        sid = _fresh_schedule_meeting()
        client = MagicMock()
        body = {
            "user": {"id": "U0SOMEONE_ELSE"}, "channel": {"id": "C0T"},
            "message": {"ts": "1.1", "blocks": []},
            "actions": [{"value": f"{sid}:1"}],
        }
        with patch.object(td, "_execute_claimed_schedule_meeting") as book:
            app_mod.handle_pick_slot(MagicMock(), body, client)
        book.assert_not_called()
        client.chat_postEphemeral.assert_called_once()
        assert td.stash_is_live(sid), "an unauthorized tap must not consume the stash"

    def test_typed_path_slot_choice_is_unchanged(self):
        """The typed confirm still books any offered slot by exact iso strings."""
        _fresh_schedule_meeting()
        with patch.object(td, "_execute_claimed_schedule_meeting",
                          return_value="Booked.") as book:
            td._tool_calendar_schedule_meeting(USER_ID, "F3E", {
                "_channel_name": CHANNEL, "confirmed": True,
                "proposed_start": _SLOTS[2][0], "proposed_end": _SLOTS[2][1],
            })
        assert book.call_args.args[2] == _SLOTS[2][0]


class TestSlotCardWiring:
    def test_slot_action_id_is_registered_with_bolt(self):
        src = (_REPO_ROOT / "src" / "cora" / "app.py").read_text(encoding="utf-8")
        assert "@app.action(confirm_cards.ACTION_PICK_SLOT)" in src

    def test_slot_and_candidate_pickers_use_distinct_action_ids(self):
        """A slot pick CONFIRMS a write; a candidate pick answers a question.
        Sharing one action id would let one resolve through the other's store."""
        assert cc.ACTION_PICK_SLOT != cc.ACTION_PICK

    def test_eval_mode_refuses_a_slot_tap(self, monkeypatch):
        monkeypatch.setenv("CORA_EVAL_MODE", "1")
        sid = _fresh_schedule_meeting()
        client = MagicMock()
        body = {
            "user": {"id": USER_ID}, "channel": {"id": "C0T"},
            "message": {"ts": "1.1", "blocks": []},
            "actions": [{"value": f"{sid}:1"}],
        }
        with patch.object(td, "_execute_claimed_schedule_meeting") as book:
            app_mod.handle_pick_slot(MagicMock(), body, client)
        book.assert_not_called()

    def test_flag_off_slot_tap_never_writes(self, monkeypatch):
        monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "off")
        sid = _fresh_schedule_meeting()
        client = MagicMock()
        body = {
            "user": {"id": USER_ID}, "channel": {"id": "C0T"},
            "message": {"ts": "1.1", "blocks": []},
            "actions": [{"value": f"{sid}:1"}],
        }
        with patch.object(td, "_execute_claimed_schedule_meeting") as book:
            app_mod.handle_pick_slot(MagicMock(), body, client)
        book.assert_not_called()
        client.chat_update.assert_not_called()
        assert td.stash_is_live(sid)
