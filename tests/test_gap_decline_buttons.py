"""Gap-escalation decline buttons (S6 migration 2).

DECLINE-ONLY buttons on the domain-owner ask DM. The typed reply REMAINS the
answer mechanism, so the load-bearing pins here are (a) no answer-via-button
exists, and (b) the button resolves the ask EXACTLY as the decline-phrase regex
does -- guaranteed structurally by the shared `_mark_declined`, and asserted
here as observable parity.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from cora import app as capp
from cora import gap_autofill as ga

OWNER = "U_OWNER"
STRANGER = "U_STRANGER"
_DM = "D_OWNER"
_TS = "1780000000.0002"


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
        return {"ok": True, "ts": _TS}

    def conversations_open(self, **kw):
        return {"channel": {"id": _DM}}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "on")
    monkeypatch.delenv("CORA_EVAL_MODE", raising=False)
    monkeypatch.setenv("GAP_ASK_PENDING_PATH", str(tmp_path / "gap_ask_pending.json"))
    yield


def _seed_ask(ask_id="gapask-abc123def456", state="PENDING", target=OWNER,
              asked_at=None):
    # RELATIVE, not a literal date. `_ask_expired` measures asked_at against the
    # real clock (ASK_TTL_HOURS), so the old hardcoded 2026-08-09 aged past the
    # TTL and from ~8/10 onward six tests failed with outcome "expired" -- they
    # were pinning the TTL, not the decline path. The deliberately-expired case
    # passes its own literal (feedback_test_clock_collision).
    if asked_at is None:
        asked_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    asks = {ask_id: {
        "ask_id": ask_id, "gap_ts": "g1", "entity": "F3E",
        "question": "What is the wholesale case price?",
        "gap": "no pricing record", "target_user_id": target,
        "dm_channel_id": _DM, "ask_message_ts": _TS,
        "asked_at": asked_at, "state": state,
    }}
    ga.save_pending_asks(asks)
    return ask_id


def _tap_body(user_id, ask_id, action_id):
    return {
        "user": {"id": user_id},
        "channel": {"id": _DM},
        "message": {"ts": _TS, "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": "Hi -- knowledge gap"}},
            {"type": "actions", "elements": [
                {"type": "button", "action_id": action_id, "value": ask_id}]},
        ]},
        "actions": [{"action_id": action_id, "value": ask_id}],
    }


class TestDeclineCore:
    def test_addressee_decline_marks_declined(self):
        aid = _seed_ask()
        outcome, msg = ga.process_decline_tap(aid, OWNER, reason="not_mine")
        assert outcome == "declined"
        assert msg == ga.DECLINE_ACK
        stored = ga.load_pending_asks()[aid]
        assert stored["state"] == "DECLINED"
        assert stored["declined_via"] == "button:not_mine"
        assert stored["replied_at"]

    def test_button_and_typed_decline_reach_identical_state(self):
        """Parity is the whole requirement: the button must resolve the ask
        exactly as the decline-phrase regex does."""
        # One shared asked_at: the two seeds are compared field-by-field, so
        # letting each stamp its own "now" would differ by microseconds.
        recent = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        a1 = _seed_ask(ask_id="gapask-button", asked_at=recent)
        ga.process_decline_tap(a1, OWNER, reason="unknown")
        btn = ga.load_pending_asks()[a1]

        a2 = _seed_ask(ask_id="gapask-typed", asked_at=recent)
        typed_ack = ga.record_ask_answer(ga.load_pending_asks()[a2], "not my area")
        typed = ga.load_pending_asks()[a2]

        assert btn["state"] == typed["state"] == "DECLINED"
        assert typed_ack == ga.DECLINE_ACK
        # Identical apart from the audit-only provenance marker and ids.
        ignore = {"ask_id", "declined_via", "replied_at"}
        assert {k: v for k, v in btn.items() if k not in ignore} == \
               {k: v for k, v in typed.items() if k not in ignore}

    def test_stranger_refused_and_ask_stays_pending(self):
        aid = _seed_ask()
        outcome, _ = ga.process_decline_tap(aid, STRANGER)
        assert outcome == "not_authorized"
        assert ga.load_pending_asks()[aid]["state"] == "PENDING"

    def test_second_tap_already_handled(self):
        aid = _seed_ask()
        ga.process_decline_tap(aid, OWNER)
        outcome, _ = ga.process_decline_tap(aid, OWNER)
        assert outcome == "already_handled"

    def test_unknown_ask_id_is_orphaned(self):
        _seed_ask()
        outcome, _ = ga.process_decline_tap("gapask-forged", OWNER)
        assert outcome == "orphaned"

    def test_expired_ask_is_an_honest_tombstone_and_mutates_nothing(self):
        aid = _seed_ask(asked_at="2020-01-01T00:00:00+00:00")
        outcome, msg = ga.process_decline_tap(aid, OWNER)
        assert outcome == "expired"
        assert "expired" in msg.lower()
        assert ga.load_pending_asks()[aid]["state"] == "PENDING"

    def test_typed_answer_path_still_proposes_after_buttons_exist(self):
        """Fallback parity: adding buttons must not touch the answer route."""
        aid = _seed_ask()
        # propose_update is imported inside the function from knowledge_review.
        with patch("cora.knowledge_review.propose_update", return_value=None) as prop:
            ack = ga.record_ask_answer(ga.load_pending_asks()[aid],
                                       "It is 24 dollars a case.")
        prop.assert_called_once()
        assert "approval" in ack.lower() or "harrison" in ack.lower()


class TestAskBlocks:
    def test_only_decline_buttons_exist_no_answer_button(self):
        blocks = ga.build_ask_blocks("ask body", "gapask-1")
        actions = [b for b in blocks if b["type"] == "actions"][0]
        ids = {e["action_id"] for e in actions["elements"]}
        assert ids == {ga.ACTION_DECLINE_NOT_MINE, ga.ACTION_DECLINE_UNKNOWN}
        labels = {e["text"]["text"] for e in actions["elements"]}
        assert labels == {"Not my area", "I don't know"}

    def test_values_are_the_ask_id_only(self):
        blocks = ga.build_ask_blocks("ask body", "gapask-1")
        actions = [b for b in blocks if b["type"] == "actions"][0]
        assert {e["value"] for e in actions["elements"]} == {"gapask-1"}

    def test_body_is_sanitized_at_construction(self):
        with patch("cora.slack_egress.sanitize_text", return_value="SCRUBBED") as m:
            blocks = ga.build_ask_blocks("<!channel> raw", "gapask-1")
        m.assert_called_once()
        assert blocks[0]["text"]["text"] == "SCRUBBED"


class TestEscalationAttachesButtons:
    def _gap(self):
        return {"ts": "g1", "entity": "F3E", "channel": "f3e-sales",
                "question": "What is the wholesale case price?",
                "gap": "no pricing record"}

    def test_escalation_dm_carries_decline_buttons_when_enabled(self):
        fake = _FakeClient()
        with patch.object(ga, "resolve_owner", return_value=OWNER):
            ask = ga.escalate_gap(self._gap(), fake)
        assert ask is not None
        assert len(fake.posted) == 1
        blocks = fake.posted[0].get("blocks")
        assert blocks, "escalation DM should carry the decline buttons"
        actions = [b for b in blocks if b["type"] == "actions"][0]
        assert {e["value"] for e in actions["elements"]} == {ask["ask_id"]}
        # text= survives as the notification fallback.
        assert "knowledge gap" in fake.posted[0]["text"]

    def test_flag_off_posts_the_original_text_only_message(self, monkeypatch):
        monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "off")
        fake = _FakeClient()
        with patch.object(ga, "resolve_owner", return_value=OWNER):
            ask = ga.escalate_gap(self._gap(), fake)
        assert ask is not None
        assert "blocks" not in fake.posted[0]

    def test_ask_id_on_the_card_matches_the_stored_ask(self):
        fake = _FakeClient()
        with patch.object(ga, "resolve_owner", return_value=OWNER):
            ask = ga.escalate_gap(self._gap(), fake)
        stored = ga.load_pending_asks()
        assert ask["ask_id"] in stored
        actions = [b for b in fake.posted[0]["blocks"] if b["type"] == "actions"][0]
        assert actions["elements"][0]["value"] in stored


class TestHandlerEntryPoint:
    """D-167: drive the @app.action wrapper the way production does."""

    def test_not_my_area_tap_declines_and_drops_buttons(self):
        aid = _seed_ask()
        fake = _FakeClient()
        capp._handle_gap_decline_tap(
            _tap_body(OWNER, aid, ga.ACTION_DECLINE_NOT_MINE), fake, reason="not_mine")
        assert ga.load_pending_asks()[aid]["state"] == "DECLINED"
        assert len(fake.updated) == 1
        assert all(b.get("type") != "actions" for b in fake.updated[0]["blocks"])

    def test_dont_know_tap_records_its_own_reason(self):
        aid = _seed_ask()
        fake = _FakeClient()
        capp._handle_gap_decline_tap(
            _tap_body(OWNER, aid, ga.ACTION_DECLINE_UNKNOWN), fake, reason="unknown")
        assert ga.load_pending_asks()[aid]["declined_via"] == "button:unknown"

    def test_stranger_tap_ephemeral_only_card_untouched(self):
        aid = _seed_ask()
        fake = _FakeClient()
        capp._handle_gap_decline_tap(
            _tap_body(STRANGER, aid, ga.ACTION_DECLINE_NOT_MINE), fake, reason="not_mine")
        assert ga.load_pending_asks()[aid]["state"] == "PENDING"
        assert not fake.updated
        assert len(fake.ephemeral) == 1

    def test_second_tap_does_not_edit_the_shared_card(self):
        aid = _seed_ask()
        fake = _FakeClient()
        body = _tap_body(OWNER, aid, ga.ACTION_DECLINE_NOT_MINE)
        capp._handle_gap_decline_tap(body, fake, reason="not_mine")
        capp._handle_gap_decline_tap(body, fake, reason="not_mine")
        assert len(fake.updated) == 1
        assert len(fake.ephemeral) == 1

    def test_eval_mode_mutates_nothing(self, monkeypatch):
        monkeypatch.setenv("CORA_EVAL_MODE", "1")
        aid = _seed_ask()
        fake = _FakeClient()
        capp._handle_gap_decline_tap(
            _tap_body(OWNER, aid, ga.ACTION_DECLINE_NOT_MINE), fake, reason="not_mine")
        assert ga.load_pending_asks()[aid]["state"] == "PENDING"
        assert not fake.updated and not fake.ephemeral

    def test_buttons_off_names_the_typed_fallback(self, monkeypatch):
        monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "off")
        aid = _seed_ask()
        fake = _FakeClient()
        capp._handle_gap_decline_tap(
            _tap_body(OWNER, aid, ga.ACTION_DECLINE_NOT_MINE), fake, reason="not_mine")
        assert ga.load_pending_asks()[aid]["state"] == "PENDING"
        assert "not my area" in fake.ephemeral[0]["text"].lower()

    def test_handler_never_raises_on_malformed_body(self):
        capp._handle_gap_decline_tap({}, _FakeClient(), reason="not_mine")

    def test_action_ids_are_unique_to_this_surface(self):
        from cora import briefing_enrollment as be
        from cora import confirm_cards as cc
        ids = {ga.ACTION_DECLINE_NOT_MINE, ga.ACTION_DECLINE_UNKNOWN}
        others = {cc.ACTION_CONFIRM, cc.ACTION_CANCEL, cc.ACTION_PICK,
                  cc.ACTION_CONFIRM_ITEM, cc.ACTION_CANCEL_ITEM,
                  be.ACTION_ENABLE, be.ACTION_SKIP}
        assert not (ids & others)
