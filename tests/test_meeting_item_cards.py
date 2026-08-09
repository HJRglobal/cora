"""v2b S5: meeting_action_items as PER-ITEM confirm cards (cq-b5460ae7aca3).

A meeting preview lists several action items and the user usually wants some of
them, so one Confirm over the whole reply is the wrong shape. The preview now
stashes the asker's VERIFIED item list server-side and the reply is followed by
one Confirm/Skip card per item, valued "{stash_id}:{item_index}".

The properties under test, in rough order of how much they matter:

  * confirming item 0 creates item 0's task and NOTHING else, and leaves the
    other items confirmable -- the stash is claimed per index, not popped whole;
  * the write path re-runs every existing rail per tapped item (re-fetch,
    attendee, scope, LEX, and _create_selected's own content/dedup/budget rails);
  * an item tap can only ever address a meeting_item stash, and a meeting_item
    stash can only ever be reached through an item tap -- a bare Confirm would
    consume every unconfirmed sibling;
  * the typed confirmed=true idiom still works, now filtered against the
    verified list rather than the meeting's whole action-items blob.
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
from cora.tools import meeting_actions as ma  # noqa: E402
from cora.tools import tool_dispatch as td  # noqa: E402

USER = "U0MEET"
CHAN = "f3e-leadership"
TRANSCRIPT = "T123"
ITEMS = ["Send Josh the pallet quote", "Book the Hensley follow-up",
         "Update the retail forecast"]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "on")
    monkeypatch.delenv("CORA_EVAL_MODE", raising=False)
    cc.reset_cards_for_tests()
    with cc._INDEX_LOCK:
        cc._INDEX.clear()
    for kind in td._CLASSB_KINDS:
        td._CLASSB[kind]["store"].clear()
    yield
    for kind in td._CLASSB_KINDS:
        td._CLASSB[kind]["store"].clear()
    cc.reset_cards_for_tests()


def _stash(items=None, transcript_id=TRANSCRIPT, entity="F3E") -> str:
    items = ITEMS if items is None else items
    return td._classb_stash("meeting_item", USER, CHAN, {
        "items": list(items), "claimed": [False] * len(items),
        "transcript_id": transcript_id, "entity": entity, "is_dm": False,
    })


def _tap(sid, idx, who=USER, action="confirm", coords=None):
    with patch("cora.entity_router.route", return_value="F3E"):
        return td.resolve_and_claim_stash(sid, who, action, item_index=idx,
                                          card_coords=coords)


# ── the per-index claim ────────────────────────────────────────────────────


class TestPerItemClaim:
    def test_confirming_one_item_leaves_the_others_confirmable(self):
        sid = _stash()
        with patch.object(td, "_execute_claimed_meeting_item", return_value="Done: 1"):
            res = _tap(sid, 0)
        assert res["outcome"] == "executed"
        assert td.stash_is_live(sid), "the stash must survive for the remaining items"
        entry = td.peek_meeting_items(USER, CHAN)
        assert entry["claimed"] == [True, False, False]

    def test_each_item_executes_with_its_OWN_text(self):
        sid = _stash()
        seen = []
        with patch.object(td, "_execute_claimed_meeting_item",
                          side_effect=lambda p, u: seen.append(p["item"]) or "ok"):
            _tap(sid, 2)
            _tap(sid, 0)
        assert seen == [ITEMS[2], ITEMS[0]]

    def test_the_last_item_retires_the_whole_stash(self):
        sid = _stash(items=ITEMS[:2])
        with patch.object(td, "_execute_claimed_meeting_item", return_value="ok"):
            _tap(sid, 0)
            assert td.stash_is_live(sid)
            _tap(sid, 1)
        assert not td.stash_is_live(sid)
        assert td.peek_meeting_items(USER, CHAN) is None

    def test_a_second_tap_on_the_same_item_is_idempotent(self):
        sid = _stash()
        with patch.object(td, "_execute_claimed_meeting_item", return_value="ok") as ex:
            _tap(sid, 1)
            res = _tap(sid, 1)
        assert ex.call_count == 1
        assert res["outcome"] == "already_handled"

    def test_skip_dismisses_only_that_item(self):
        sid = _stash()
        with patch.object(td, "_execute_claimed_meeting_item") as ex:
            res = _tap(sid, 1, action="cancel")
        ex.assert_not_called()
        assert res["outcome"] == "cancelled"
        assert td.stash_is_live(sid)
        assert td.peek_meeting_items(USER, CHAN)["claimed"] == [False, True, False]

    def test_skipping_the_last_remaining_item_retires_the_stash(self):
        sid = _stash(items=[ITEMS[0]])
        res = _tap(sid, 0, action="cancel")
        assert res["outcome"] == "cancelled"
        assert not td.stash_is_live(sid)

    def test_an_out_of_range_index_creates_nothing(self):
        sid = _stash()
        with patch.object(td, "_execute_claimed_meeting_item") as ex:
            for bad in (99, -1):
                assert _tap(sid, bad)["outcome"] == "orphaned"
        ex.assert_not_called()
        assert td.peek_meeting_items(USER, CHAN)["claimed"] == [False, False, False]

    def test_a_non_requester_tap_creates_nothing_and_claims_nothing(self):
        sid = _stash()
        with patch.object(td, "_execute_claimed_meeting_item") as ex:
            res = _tap(sid, 0, who="U0INTRUDER")
        ex.assert_not_called()
        assert res["outcome"] == "unauthorized"
        assert td.peek_meeting_items(USER, CHAN)["claimed"] == [False, False, False]

    def test_an_expired_stash_creates_nothing_and_names_itself(self):
        sid = _stash()
        td.peek_meeting_items(USER, CHAN)["ts"] -= td._CLASSB_TTL_SECONDS + 5
        with patch.object(td, "_execute_claimed_meeting_item") as ex:
            res = _tap(sid, 0)
        ex.assert_not_called()
        assert res["outcome"] == "expired"
        assert res["label"] == "that meeting action item"

    def test_a_superseded_stash_creates_nothing(self):
        old = _stash()
        _stash(items=["A brand new meeting's item"])  # overwrites the slot
        with patch.object(td, "_execute_claimed_meeting_item") as ex:
            res = _tap(old, 0)
        ex.assert_not_called()
        assert res["outcome"] == "superseded"

    def test_eval_mode_refuses_an_item_tap(self, monkeypatch):
        sid = _stash()
        monkeypatch.setenv("CORA_EVAL_MODE", "1")
        with patch.object(td, "_execute_claimed_meeting_item") as ex:
            assert _tap(sid, 0)["outcome"] == "orphaned"
        ex.assert_not_called()

    def test_an_execute_crash_is_indeterminate_not_a_silent_success(self):
        sid = _stash()
        with patch.object(td, "_execute_claimed_meeting_item",
                          side_effect=RuntimeError("asana down")):
            res = _tap(sid, 0)
        assert res["outcome"] == "indeterminate"


# ── the two-way authority separation ───────────────────────────────────────


class TestActionIdSeparation:
    def test_an_item_index_cannot_address_another_kind(self):
        sid = td._classb_stash("gmail_draft", USER, CHAN, {"to": "a@b.com"})
        with patch("cora.entity_router.route", return_value="F3E"):
            res = td.resolve_and_claim_stash(sid, USER, "confirm", item_index=0)
        assert res["outcome"] == "orphaned"
        assert td.stash_is_live(sid), "the refused tap must not consume the stash"

    def test_a_BARE_confirm_cannot_consume_a_meeting_item_stash(self):
        """Without this the generic claim pops the whole entry, silently
        discarding every item the user had not yet decided on."""
        sid = _stash()
        with patch.object(td, "_execute_claimed_meeting_item") as ex, \
             patch("cora.entity_router.route", return_value="F3E"):
            res = td.resolve_and_claim_stash(sid, USER, "confirm")
        ex.assert_not_called()
        assert res["outcome"] == "orphaned"
        assert td.stash_is_live(sid)
        assert td.peek_meeting_items(USER, CHAN)["claimed"] == [False, False, False]

    def test_a_slot_index_cannot_address_a_meeting_item_stash(self):
        sid = _stash()
        with patch("cora.entity_router.route", return_value="F3E"):
            res = td.resolve_and_claim_stash(sid, USER, "confirm", slot_index=0)
        assert res["outcome"] == "orphaned"
        assert td.stash_is_live(sid)

    def test_the_item_action_ids_are_distinct_from_every_other_pair(self):
        ids = {cc.ACTION_CONFIRM, cc.ACTION_CANCEL, cc.ACTION_PICK,
               cc.ACTION_PICK_SLOT, cc.ACTION_CONFIRM_ITEM, cc.ACTION_CANCEL_ITEM}
        assert len(ids) == 6

    def test_both_item_action_ids_are_registered_with_bolt(self):
        src = (_REPO_ROOT / "src" / "cora" / "app.py").read_text(encoding="utf-8")
        assert "@app.action(confirm_cards.ACTION_CONFIRM_ITEM)" in src
        assert "@app.action(confirm_cards.ACTION_CANCEL_ITEM)" in src


# ── the write path re-runs every rail ──────────────────────────────────────


def _transcript(title="F3E retail sync", emails=("alex@f3energy.com",)):
    return {"id": TRANSCRIPT, "title": title,
            "participants": list(emails),
            "summary": {"action_items": "\n".join(ITEMS)}}


class TestWritePathRails:
    def _exec(self, payload=None, **patches):
        payload = payload or {"item": ITEMS[0], "item_index": 0, "last": False,
                              "transcript_id": TRANSCRIPT, "entity": "F3E",
                              "is_dm": False}
        defaults = {
            "_asker_emails": lambda u: {"alex@f3energy.com"},
            "_fetch_transcript_by_id": lambda t: _transcript(),
            "_asker_attended": lambda t, e, u: True,
            "_classify_meeting": lambda t: ("F3E", False),
            "_scope_ok": lambda me, ce, dm: (True, ""),
            "_lex_gate": lambda t, ti, me: (True, "", "F3E"),
        }
        defaults.update(patches)
        with patch.multiple(ma, **{k: MagicMock(side_effect=v) if callable(v) else v
                                   for k, v in defaults.items()}), \
             patch.object(ma, "_create_selected",
                          return_value="WRITE_CONFIRMED -- x\n\nDone.") as create:
            out = td._execute_claimed_meeting_item(payload, USER)
        return out, create

    def test_the_happy_path_creates_exactly_the_tapped_item(self):
        out, create = self._exec()
        create.assert_called_once()
        assert create.call_args.args[6] == [ITEMS[0]], \
            "only the tapped item may reach the create path"
        assert "Done." in out

    def test_a_non_attendee_creates_nothing(self):
        _out, create = self._exec(_asker_attended=lambda t, e, u: False)
        create.assert_not_called()

    def test_an_out_of_scope_channel_creates_nothing(self):
        _out, create = self._exec(_scope_ok=lambda me, ce, dm: (False, "Not here."))
        create.assert_not_called()

    def test_the_lex_gate_creates_nothing_when_it_refuses(self):
        _out, create = self._exec(_lex_gate=lambda t, ti, me: (False, "LEX no.", ""))
        create.assert_not_called()

    def test_a_missing_transcript_creates_nothing(self):
        _out, create = self._exec(_fetch_transcript_by_id=lambda t: None)
        create.assert_not_called()

    def test_an_unmapped_asker_creates_nothing(self):
        _out, create = self._exec(_asker_emails=lambda u: set())
        create.assert_not_called()

    def test_a_transcript_service_outage_creates_nothing(self):
        def _boom(_t):
            raise ma.FirefliesConnectorError("down")
        out, create = self._exec(_fetch_transcript_by_id=_boom)
        create.assert_not_called()
        assert "couldn't reach" in out

    def test_a_stash_with_no_transcript_id_creates_nothing(self):
        _out, create = self._exec(payload={"item": ITEMS[0], "item_index": 0,
                                           "last": False, "transcript_id": "",
                                           "entity": "F3E", "is_dm": False})
        create.assert_not_called()

    def test_the_scope_check_uses_the_STASHED_channel_context(self):
        payload = {"item": ITEMS[0], "item_index": 0, "last": False,
                   "transcript_id": TRANSCRIPT, "entity": "LEX-LLC", "is_dm": True}
        seen = {}
        self._exec(payload=payload,
                   _scope_ok=lambda me, ce, dm: seen.update(entity=ce, dm=dm) or (True, ""))
        assert seen == {"entity": "LEX-LLC", "dm": True}


# ── the preview stash + the typed path ─────────────────────────────────────


class TestPreviewStashAndTypedPath:
    def test_verified_item_texts_scrubs_and_caps(self):
        many = [{"task": f"item {i}"} for i in range(10)]
        out = ma.verified_item_texts(many, [], is_lex=False)
        assert len(out) == ma._MAX_SELECTED
        assert out[0] == "item 0"

    def test_verified_item_texts_drops_blanks(self):
        out = ma.verified_item_texts([{"task": "real"}, {"task": "   "}], [], False)
        assert out == ["real"]

    def test_unclear_items_are_offered_after_the_owned_ones(self):
        out = ma.verified_item_texts([{"task": "mine"}], [{"task": "unowned"}], False)
        assert out == ["mine", "unowned"]

    def test_the_typed_filter_keeps_a_verbatim_selection(self):
        assert ma._stashed_item_filter(ITEMS, [ITEMS[1]]) == [ITEMS[1]]

    def test_the_typed_filter_tolerates_relay_drift(self):
        assert ma._stashed_item_filter(ITEMS, ["  Send   Josh the pallet quote "])

    def test_the_typed_filter_drops_an_item_that_was_never_offered(self):
        """The tightening: today's rail matches against the meeting's WHOLE
        action-items blob, which also holds other attendees' lines."""
        assert ma._stashed_item_filter(ITEMS, ["Cancel the Perkins vendor contract"]) == []

    def test_the_typed_filter_is_a_no_op_with_no_stash(self):
        """No stash (expired, restarted, a script caller) must not start
        dropping selections -- the existing rails still run downstream."""
        assert ma._stashed_item_filter([], ["anything at all"]) == ["anything at all"]

    def test_the_preview_stashes_the_verified_list(self):
        captured = {}
        with patch.object(ma, "run_meeting_action_items",
                          side_effect=lambda u, e, i, **kw: (
                              captured.update(kw) or "preview")):
            td._tool_meeting_action_items(USER, "F3E", {"_channel_name": CHAN})
        assert callable(captured.get("stash_items"))
        assert callable(captured.get("stashed_items_for"))
        captured["stash_items"](ITEMS, {"transcript_id": TRANSCRIPT, "entity": "F3E"})
        entry = td.peek_meeting_items(USER, CHAN)
        assert entry["items"] == ITEMS
        assert entry["claimed"] == [False, False, False]
        assert entry["transcript_id"] == TRANSCRIPT

    def test_the_confirm_hook_only_answers_for_the_SAME_meeting(self):
        _stash()
        captured = {}
        with patch.object(ma, "run_meeting_action_items",
                          side_effect=lambda u, e, i, **kw: (
                              captured.update(kw) or "x")):
            td._tool_meeting_action_items(USER, "F3E", {"_channel_name": CHAN})
        assert captured["stashed_items_for"](TRANSCRIPT) == ITEMS
        assert captured["stashed_items_for"]("A_DIFFERENT_MEETING") is None


# ── the card layer ─────────────────────────────────────────────────────────


class TestItemCards:
    def test_one_card_carries_its_own_index(self):
        blocks = cc.build_item_confirm_blocks("*1.* Do the thing", "sid1", 0)
        els = blocks[1]["elements"]
        assert [e["action_id"] for e in els] == [cc.ACTION_CONFIRM_ITEM,
                                                 cc.ACTION_CANCEL_ITEM]
        assert [e["value"] for e in els] == ["sid1:0", "sid1:0"]

    def test_the_block_id_disambiguates_items_of_one_stash(self):
        a = cc.build_item_confirm_blocks("a", "sid1", 0)[1]["block_id"]
        b = cc.build_item_confirm_blocks("b", "sid1", 1)[1]["block_id"]
        assert a != b

    def test_pop_card_at_takes_only_its_own_coordinate(self):
        cc.claim_card_attach("sid1")
        cc.register_card("sid1", "C1", "1.1", "one")
        cc.register_card("sid1", "C1", "2.2", "two")
        assert cc.pop_card_at("sid1", "C1", "1.1") is True
        assert [ts for _c, ts, _p in cc.pop_cards("sid1")] == ["2.2"]

    def test_pop_card_at_is_false_for_an_unknown_coordinate(self):
        cc.claim_card_attach("sid1")
        cc.register_card("sid1", "C1", "1.1", "one")
        assert cc.pop_card_at("sid1", "C1", "9.9") is False
        assert cc.pop_card_at("", "C1", "1.1") is False

    def test_a_tap_hands_over_only_its_own_card(self):
        sid = _stash()
        cc.claim_card_attach(sid)
        cc.register_card(sid, "C1", "1.1", "item 1")
        cc.register_card(sid, "C1", "2.2", "item 2")
        with patch.object(td, "_execute_claimed_meeting_item", return_value="ok"):
            _tap(sid, 0, coords=("C1", "1.1"))
        remaining = [ts for _c, ts, _p in cc.pop_cards(sid)]
        assert remaining == ["2.2"], "sibling item cards must stay sweepable"

    def test_the_item_cap_matches_what_the_write_path_will_create(self):
        assert cc.MAX_ITEM_CARDS == ma._MAX_SELECTED


class TestAppTapWiring:
    def _body(self, value):
        return {"user": {"id": USER}, "channel": {"id": "C0T"},
                "message": {"ts": "1.1", "blocks": [
                    {"type": "section", "text": {"type": "mrkdwn", "text": "*1.* x"}},
                    {"type": "actions", "elements": []},
                ]},
                "actions": [{"value": value}]}

    def test_a_confirm_item_tap_creates_and_closes_its_card(self):
        sid = _stash()
        client = MagicMock()
        with patch.object(td, "_execute_claimed_meeting_item", return_value="Created."), \
             patch("cora.entity_router.route", return_value="F3E"):
            app_mod.handle_confirm_item(MagicMock(), self._body(f"{sid}:1"), client)
        assert "Created." in client.chat_update.call_args.kwargs["text"]

    def test_a_skip_tap_says_it_skipped_ONE_item(self):
        sid = _stash()
        client = MagicMock()
        with patch("cora.entity_router.route", return_value="F3E"):
            app_mod.handle_cancel_item(MagicMock(), self._body(f"{sid}:0"), client)
        text = client.chat_update.call_args.kwargs["text"]
        assert "Skipped" in text
        assert "nothing was changed" not in text.lower(), \
            "the sibling items are still staged; that copy would be misleading"

    def test_a_malformed_value_is_ignored_entirely(self):
        sid = _stash()
        client = MagicMock()
        with patch.object(td, "_execute_claimed_meeting_item") as ex:
            app_mod.handle_confirm_item(MagicMock(), self._body(sid), client)
            app_mod.handle_confirm_item(MagicMock(), self._body(f"{sid}:x"), client)
        ex.assert_not_called()
        client.chat_update.assert_not_called()
        assert td.peek_meeting_items(USER, CHAN)["claimed"] == [False, False, False]

    def test_flag_off_never_creates(self, monkeypatch):
        monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "off")
        sid = _stash()
        client = MagicMock()
        with patch.object(td, "_execute_claimed_meeting_item") as ex:
            app_mod.handle_confirm_item(MagicMock(), self._body(f"{sid}:0"), client)
        ex.assert_not_called()
        client.chat_update.assert_not_called()
        assert td.stash_is_live(sid)


class TestRegistration:
    def test_the_kind_is_registered_everywhere_it_has_to_be(self):
        assert "meeting_item" in td._stash_kind_specs()
        assert "meeting_item" in td._defer_to_model_kinds()
        assert "meeting_item" in td._PENDING_KIND_LABELS

    def test_a_typed_cancel_dismisses_the_whole_meeting_stash(self):
        """A cancel is a cancel: it pops the entry, so every item goes."""
        sid = _stash()
        reply = td.try_confirm_pending_write(
            slack_user_id=USER, channel_name=CHAN, entity="F3E",
            message="no, cancel that")
        assert reply is not None and "cancelled" in reply.lower()
        assert not td.stash_is_live(sid)

    def test_the_sweep_leaves_a_partly_claimed_stash_alone(self):
        """stash_is_live is the sweep's skip condition, and it must stay True
        while any item is unclaimed -- otherwise the sibling cards get closed
        under the user the moment they tap the first one."""
        sid = _stash()
        with patch.object(td, "_execute_claimed_meeting_item", return_value="ok"):
            _tap(sid, 0)
        assert td.stash_is_live(sid) is True

    def test_a_fully_claimed_stash_reads_as_handled_not_expired(self):
        sid = _stash(items=[ITEMS[0]])
        with patch.object(td, "_execute_claimed_meeting_item", return_value="ok"):
            _tap(sid, 0)
        assert td.stash_is_live(sid) is False
        assert td.stash_expired_not_consumed(sid) is False, \
            "a consumed stash must not get the expiry copy"

    def test_a_lapsed_stash_reads_as_expired(self):
        sid = _stash()
        td.peek_meeting_items(USER, CHAN)["ts"] -= td._CLASSB_TTL_SECONDS + 5
        assert td.stash_is_live(sid) is False
        assert td.stash_expired_not_consumed(sid) is True, \
            "a lapsed meeting stash must get the 'nothing was created' copy"
