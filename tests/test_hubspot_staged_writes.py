"""v2b S5: hubspot_update_deal_stage + hubspot_add_note as real staged writes.

Both were honor gates -- they refused unless the model asserted confirmed=true,
then wrote whatever THAT call carried. A model that altered deal_id on the
second call silently moved a DIFFERENT deal than the one previewed, and a
re-worded note_body filed text the user never approved. Now the unconfirmed
call validates + STASHES, and the confirmed call (or a button tap) executes the
STASH.

The tests that matter most are the retarget ones: a confirm turn carrying
different values must not be able to change what happens.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test-signing-secret")

import pytest  # noqa: E402

from cora import confirm_cards as cc  # noqa: E402
from cora.tools import hubspot_client as hs  # noqa: E402
from cora.tools import tool_dispatch as td  # noqa: E402

USER = "U0B2RM2JYJ1"
CHAN = "f3e-leadership"
IN = {"_channel_name": CHAN}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "on")
    monkeypatch.delenv("CORA_EVAL_MODE", raising=False)
    cc.reset_cards_for_tests()
    with cc._INDEX_LOCK:
        cc._INDEX.clear()
    for kind in td._CLASSB_KINDS:
        td._CLASSB[kind]["store"].clear()
    monkeypatch.setattr(hs, "_STAGE_NAME_CACHE",
                        {"s_new": "Proposal", "s_old": "Discovery"}, raising=False)
    yield
    for kind in td._CLASSB_KINDS:
        td._CLASSB[kind]["store"].clear()
    cc.reset_cards_for_tests()


def _deal(name="Wham Foods Retail", stage="s_old"):
    return {"dealname": name, "dealstage": stage}


def _peek(kind):
    return td._CLASSB[kind]["peek"](USER, CHAN)


# ── hubspot_update_deal_stage ──────────────────────────────────────────────


class TestDealStagePreview:
    def test_unconfirmed_call_previews_and_stashes_instead_of_writing(self):
        with patch.object(hs, "get_deal", return_value=_deal()), \
             patch.object(hs, "update_deal_stage") as write:
            out = td._tool_hubspot_update_deal_stage(USER, "F3E", {
                **IN, "deal_id": "D1", "stage_id": "s_new"})
        write.assert_not_called()
        assert "NOT CHANGED yet" in out
        assert "Wham Foods Retail" in out and "Discovery -> Proposal" in out
        assert _peek("hubspot_stage")["stash_id"]

    def test_the_preview_carries_an_honest_confirm_instruction(self):
        with patch.object(hs, "get_deal", return_value=_deal()):
            out = td._tool_hubspot_update_deal_stage(USER, "F3E", {
                **IN, "deal_id": "D1", "stage_id": "s_new"})
        user_facing = out.split("\n\n", 1)[1]
        assert "@mention me with" in user_facing or "tap *Confirm* below" in user_facing
        assert "confirmed=true" not in user_facing.lower()

    def test_missing_fields_refuse_at_preview_and_stash_nothing(self):
        for bad, word in (({"stage_id": "s_new"}, "deal_id"),
                          ({"deal_id": "D1"}, "stage_id")):
            out = td._tool_hubspot_update_deal_stage(USER, "F3E", {**IN, **bad})
            assert word in out
            assert _peek("hubspot_stage") is None

    def test_an_unfetchable_deal_stashes_nothing(self):
        with patch.object(hs, "get_deal", side_effect=hs.HubSpotClientError("404")):
            out = td._tool_hubspot_update_deal_stage(USER, "F3E", {
                **IN, "deal_id": "D1", "stage_id": "s_new"})
        assert "could not fetch" in out
        assert _peek("hubspot_stage") is None


class TestDealStageConfirm:
    def test_confirm_executes_the_STASH_not_the_confirm_turn_args(self):
        with patch.object(hs, "get_deal", return_value=_deal()):
            td._tool_hubspot_update_deal_stage(USER, "F3E", {
                **IN, "deal_id": "D1", "stage_id": "s_new"})
        with patch.object(hs, "update_deal_stage") as write, \
             patch.object(hs, "_deal_url", return_value="https://hs/D1"):
            out = td._tool_hubspot_update_deal_stage(USER, "F3E", {
                **IN, "confirmed": True, "deal_id": "D_OTHER", "stage_id": "s_closedwon"})
        write.assert_called_once_with("D1", "s_new")
        assert "WRITE_CONFIRMED" in out and "Wham Foods Retail" in out

    def test_a_confirm_with_no_pending_is_honest_and_writes_nothing(self):
        with patch.object(hs, "update_deal_stage") as write:
            out = td._tool_hubspot_update_deal_stage(USER, "F3E", {
                **IN, "confirmed": True, "deal_id": "D1", "stage_id": "s_new"})
        write.assert_not_called()
        assert "NOT DONE" in out and "expired" in out.lower()

    def test_the_stash_is_consumed_exactly_once(self):
        with patch.object(hs, "get_deal", return_value=_deal()):
            td._tool_hubspot_update_deal_stage(USER, "F3E", {
                **IN, "deal_id": "D1", "stage_id": "s_new"})
        with patch.object(hs, "update_deal_stage") as w1, \
             patch.object(hs, "_deal_url", return_value="u"):
            td._tool_hubspot_update_deal_stage(USER, "F3E", {**IN, "confirmed": True})
        with patch.object(hs, "update_deal_stage") as w2:
            second = td._tool_hubspot_update_deal_stage(USER, "F3E", {**IN, "confirmed": True})
        w1.assert_called_once()
        w2.assert_not_called()
        assert "NOT DONE" in second

    def test_an_api_failure_does_not_claim_success(self):
        with patch.object(hs, "get_deal", return_value=_deal()):
            td._tool_hubspot_update_deal_stage(USER, "F3E", {
                **IN, "deal_id": "D1", "stage_id": "s_new"})
        with patch.object(hs, "update_deal_stage",
                          side_effect=hs.HubSpotClientError("boom")):
            out = td._tool_hubspot_update_deal_stage(USER, "F3E", {**IN, "confirmed": True})
        assert "WRITE_CONFIRMED" not in out
        assert "not changed" in out.lower()


class TestDealStageButtonTap:
    def test_a_tap_executes_the_same_stash(self):
        with patch.object(hs, "get_deal", return_value=_deal()):
            td._tool_hubspot_update_deal_stage(USER, "F3E", {
                **IN, "deal_id": "D1", "stage_id": "s_new"})
        sid = _peek("hubspot_stage")["stash_id"]
        with patch.object(hs, "update_deal_stage") as write, \
             patch.object(hs, "_deal_url", return_value="u"), \
             patch("cora.entity_router.route", return_value="F3E"):
            res = td.resolve_and_claim_stash(sid, USER, "confirm")
        write.assert_called_once_with("D1", "s_new")
        assert res["outcome"] == "executed"
        assert "WRITE_CONFIRMED" not in res["message"], "sentinel must be stripped"

    def test_a_non_requester_tap_is_refused_and_consumes_nothing(self):
        with patch.object(hs, "get_deal", return_value=_deal()):
            td._tool_hubspot_update_deal_stage(USER, "F3E", {
                **IN, "deal_id": "D1", "stage_id": "s_new"})
        sid = _peek("hubspot_stage")["stash_id"]
        with patch.object(hs, "update_deal_stage") as write:
            res = td.resolve_and_claim_stash(sid, "U0INTRUDER", "confirm")
        write.assert_not_called()
        assert res["outcome"] == "unauthorized"
        assert td.stash_is_live(sid)

    def test_a_cancel_tap_writes_nothing_and_kills_the_stash(self):
        with patch.object(hs, "get_deal", return_value=_deal()):
            td._tool_hubspot_update_deal_stage(USER, "F3E", {
                **IN, "deal_id": "D1", "stage_id": "s_new"})
        sid = _peek("hubspot_stage")["stash_id"]
        with patch.object(hs, "update_deal_stage") as write:
            res = td.resolve_and_claim_stash(sid, USER, "cancel")
        write.assert_not_called()
        assert res["outcome"] == "cancelled"
        assert not td.stash_is_live(sid)

    def test_an_expired_stash_tap_is_an_honest_tombstone(self):
        with patch.object(hs, "get_deal", return_value=_deal()):
            td._tool_hubspot_update_deal_stage(USER, "F3E", {
                **IN, "deal_id": "D1", "stage_id": "s_new"})
        entry = _peek("hubspot_stage")
        sid = entry["stash_id"]
        entry["ts"] -= td._CLASSB_TTL_SECONDS + 5
        with patch.object(hs, "update_deal_stage") as write:
            res = td.resolve_and_claim_stash(sid, USER, "confirm")
        write.assert_not_called()
        assert res["outcome"] == "expired"
        assert res["label"] == "that deal-stage change", \
            "an expired tombstone must name WHICH staged thing lapsed"

    def test_eval_mode_refuses_a_tap(self, monkeypatch):
        with patch.object(hs, "get_deal", return_value=_deal()):
            td._tool_hubspot_update_deal_stage(USER, "F3E", {
                **IN, "deal_id": "D1", "stage_id": "s_new"})
        sid = _peek("hubspot_stage")["stash_id"]
        monkeypatch.setenv("CORA_EVAL_MODE", "1")
        with patch.object(hs, "update_deal_stage") as write:
            res = td.resolve_and_claim_stash(sid, USER, "confirm")
        write.assert_not_called()
        assert res["outcome"] == "orphaned"


class TestDealStageLexScope:
    def test_lex_is_blocked_at_preview_and_stashes_nothing(self):
        for ent in ("LEX", "LEX-LLC", "lex-lbhs"):
            out = td._tool_hubspot_update_deal_stage(USER, ent, {
                **IN, "deal_id": "D1", "stage_id": "s_new"})
            assert "blocked" in out
            assert _peek("hubspot_stage") is None

    def test_the_executor_re_checks_the_stashed_entity(self):
        """Defense in depth: a tap re-derives entity from the channel, so this
        cannot differ today. It must stay refused if that ever changes."""
        with patch.object(hs, "update_deal_stage") as write:
            out = td._execute_claimed_hubspot_stage({
                "deal_id": "D1", "stage_id": "s_new", "deal_name": "X",
                "current_stage_name": "a", "new_stage_name": "b", "entity": "LEX-LLC",
            }, USER)
        write.assert_not_called()
        assert "Nothing was changed" in out


# ── hubspot_add_note ───────────────────────────────────────────────────────


class TestAddNote:
    def test_unconfirmed_call_previews_and_stashes_instead_of_writing(self):
        with patch.object(hs, "get_deal", return_value=_deal()), \
             patch.object(hs, "create_note") as write:
            out = td._tool_hubspot_add_note(USER, "F3E", {
                **IN, "deal_id": "D1", "note_body": "Talked to Josh; retail pilot in Q4."})
        write.assert_not_called()
        assert "NOT ADDED yet" in out and "retail pilot in Q4" in out
        assert _peek("hubspot_note")["stash_id"]

    def test_confirm_files_the_STASHED_body_not_the_confirm_turn_body(self):
        with patch.object(hs, "get_deal", return_value=_deal()):
            td._tool_hubspot_add_note(USER, "F3E", {
                **IN, "deal_id": "D1", "note_body": "APPROVED TEXT"})
        with patch.object(hs, "create_note", return_value="n1") as write, \
             patch.object(hs, "_deal_url", return_value="u"):
            td._tool_hubspot_add_note(USER, "F3E", {
                **IN, "confirmed": True, "deal_id": "D_OTHER",
                "note_body": "SOMETHING THE USER NEVER SAW"})
        write.assert_called_once_with(body="APPROVED TEXT", deal_id="D1")

    def test_a_confirm_with_no_pending_is_honest_and_writes_nothing(self):
        with patch.object(hs, "create_note") as write:
            out = td._tool_hubspot_add_note(USER, "F3E", {
                **IN, "confirmed": True, "deal_id": "D1", "note_body": "x"})
        write.assert_not_called()
        assert "NOT DONE" in out

    def test_a_long_note_says_the_preview_was_shortened(self):
        body = "y" * 900
        with patch.object(hs, "get_deal", return_value=_deal()):
            out = td._tool_hubspot_add_note(USER, "F3E", {
                **IN, "deal_id": "D1", "note_body": body})
        assert "Preview shortened" in out and "900-character" in out
        assert _peek("hubspot_note")["note_body"] == body, \
            "the FULL body must be stashed, not the truncated render"

    def test_a_short_note_is_not_labelled_shortened(self):
        with patch.object(hs, "get_deal", return_value=_deal()):
            out = td._tool_hubspot_add_note(USER, "F3E", {
                **IN, "deal_id": "D1", "note_body": "short"})
        assert "Preview shortened" not in out

    def test_missing_fields_refuse_at_preview_and_stash_nothing(self):
        for bad, word in (({"note_body": "x"}, "deal_id"),
                          ({"deal_id": "D1"}, "note_body")):
            out = td._tool_hubspot_add_note(USER, "F3E", {**IN, **bad})
            assert word in out
            assert _peek("hubspot_note") is None

    def test_a_tap_executes_the_same_stash(self):
        with patch.object(hs, "get_deal", return_value=_deal()):
            td._tool_hubspot_add_note(USER, "F3E", {
                **IN, "deal_id": "D1", "note_body": "note text"})
        sid = _peek("hubspot_note")["stash_id"]
        with patch.object(hs, "create_note", return_value="n1") as write, \
             patch.object(hs, "_deal_url", return_value="u"), \
             patch("cora.entity_router.route", return_value="F3E"):
            res = td.resolve_and_claim_stash(sid, USER, "confirm")
        write.assert_called_once_with(body="note text", deal_id="D1")
        assert res["outcome"] == "executed"

    def test_lex_is_blocked_at_preview_and_in_the_executor(self):
        out = td._tool_hubspot_add_note(USER, "LEX-LLC", {
            **IN, "deal_id": "D1", "note_body": "x"})
        assert "blocked" in out
        assert _peek("hubspot_note") is None
        with patch.object(hs, "create_note") as write:
            out2 = td._execute_claimed_hubspot_note(
                {"deal_id": "D1", "note_body": "x", "deal_name": "X", "entity": "LEX"}, USER)
        write.assert_not_called()
        assert "Nothing was written" in out2


class TestBothKindsShareTheOneClaimPath:
    def test_the_two_kinds_do_not_collide_in_the_store(self):
        with patch.object(hs, "get_deal", return_value=_deal()):
            td._tool_hubspot_update_deal_stage(USER, "F3E", {
                **IN, "deal_id": "D1", "stage_id": "s_new"})
            td._tool_hubspot_add_note(USER, "F3E", {
                **IN, "deal_id": "D2", "note_body": "n"})
        assert _peek("hubspot_stage")["deal_id"] == "D1"
        assert _peek("hubspot_note")["deal_id"] == "D2"

    def test_confirming_the_note_leaves_the_stage_change_staged(self):
        with patch.object(hs, "get_deal", return_value=_deal()):
            td._tool_hubspot_update_deal_stage(USER, "F3E", {
                **IN, "deal_id": "D1", "stage_id": "s_new"})
            td._tool_hubspot_add_note(USER, "F3E", {
                **IN, "deal_id": "D2", "note_body": "n"})
        stage_sid = _peek("hubspot_stage")["stash_id"]
        with patch.object(hs, "create_note", return_value="n1"), \
             patch.object(hs, "_deal_url", return_value="u"), \
             patch.object(hs, "update_deal_stage") as stage_write:
            td._tool_hubspot_add_note(USER, "F3E", {**IN, "confirmed": True})
        stage_write.assert_not_called()
        assert td.stash_is_live(stage_sid)

    @pytest.mark.parametrize("kind", ["hubspot_stage", "hubspot_note"])
    def test_the_kind_is_registered_everywhere_it_has_to_be(self, kind):
        assert kind in td._stash_kind_specs()
        assert kind in td._defer_to_model_kinds()
        assert kind in td._PENDING_KIND_LABELS
