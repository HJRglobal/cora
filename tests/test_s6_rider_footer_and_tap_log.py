"""S6 rider, parts B and C.

B -- the Cowork connector footer leaking INTO composed payloads. The 8/9 battery
     caught a hubspot note_body carrying "*Sent using* <@U...>" verbatim into
     the CRM: the connector appends it to the relayed Slack message, the model
     copies the message into the tool arg, and it gets FILED.

C -- cq-b8a4d7b9dd4a: a tapped Cancel wrote no log line at all, while the typed
     paths log CANCEL and EXECUTE, so a card that vanished could not be
     attributed to a person or told apart from an expiry.
"""

from __future__ import annotations

import logging
import time
from unittest.mock import patch

import pytest

from cora import app as capp
from cora import confirm_cards as cc
from cora.tools import tool_dispatch as td

HARRISON = "U0B2RM2JYJ1"
_CH = "cora-build"
_CHANNEL_ID = "C1"
_FOOTER = "\n\n*Sent using* <@U0B2RM2JYJ1>"


class _FakeClient:
    def __init__(self):
        self.updated: list[dict] = []
        self.ephemeral: list[dict] = []

    def chat_update(self, **kw):
        self.updated.append(kw)
        return {"ok": True}

    def chat_postEphemeral(self, **kw):
        self.ephemeral.append(kw)
        return {"ok": True}

    def chat_postMessage(self, **kw):
        return {"ok": True, "ts": "999.9"}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "on")
    monkeypatch.delenv("CORA_EVAL_MODE", raising=False)
    td._PENDING_ASANA_WRITES.clear()
    with cc._INDEX_LOCK:
        cc._INDEX.clear()
    cc.reset_cards_for_tests()
    yield
    td._PENDING_ASANA_WRITES.clear()


class TestConnectorFooterStrip:
    def test_strips_the_trailing_footer(self):
        assert td.strip_connector_footer(
            "Please send the revised quote." + _FOOTER
        ) == "Please send the revised quote."

    def test_leaves_legitimate_mid_message_wording_alone(self):
        """End-anchored on purpose: an unanchored strip would eat this."""
        text = "the invoices sent using the old template are wrong"
        assert td.strip_connector_footer(text) == text

    def test_leaves_a_body_that_merely_contains_a_mention(self):
        text = "Ask <@U123> to confirm the pallet count"
        assert td.strip_connector_footer(text) == text

    def test_empty_and_none_safe(self):
        assert td.strip_connector_footer("") == ""
        assert td.strip_connector_footer(None) is None

    @pytest.mark.parametrize("field", ["message", "body", "note_body"])
    def test_classb_stash_strips_every_composed_body_field(self, field):
        entry = {field: "Revised volumes attached." + _FOOTER, "deal_id": "D1"}
        td._classb_stash("hubspot_note", HARRISON, _CH, entry)
        stored = td._CLASSB["hubspot_note"]["peek"](HARRISON, _CH)
        assert stored[field] == "Revised volumes attached."

    def test_classb_stash_never_touches_identifier_fields(self):
        """The strip must not reach a field whose exact value is load-bearing."""
        entry = {"note_body": "hi" + _FOOTER, "deal_id": "D_sent using_1",
                 "deal_name": "Acme sent using X"}
        td._classb_stash("hubspot_note", HARRISON, _CH, entry)
        stored = td._CLASSB["hubspot_note"]["peek"](HARRISON, _CH)
        assert stored["deal_id"] == "D_sent using_1"
        assert stored["deal_name"] == "Acme sent using X"

    def test_slack_dm_message_is_stripped_at_the_chokepoint(self):
        entry = {"recipient_id": "U9", "message": "pallet shipped" + _FOOTER}
        td._classb_stash("slack_dm", HARRISON, _CH, entry)
        stored = td._CLASSB["slack_dm"]["peek"](HARRISON, _CH)
        assert stored["message"] == "pallet shipped"
        assert "Sent using" not in stored["message"]

    def test_non_string_field_is_ignored_not_crashed(self):
        td._classb_stash("gmail_draft", HARRISON, _CH, {"body": None, "to": ["a@b.c"]})
        stored = td._CLASSB["gmail_draft"]["peek"](HARRISON, _CH)
        assert stored["body"] is None


def _stash_asana_delete(user=HARRISON, channel=_CH):
    sid = cc.mint_stash_id("asana", user, channel)
    td._store_pending_asana_write(user, channel, {
        "action": "delete", "gid": "g1", "label": "Test task",
        "ts": time.time(), "stash_id": sid,
    })
    return sid


def _tap_body(user_id, stash_id, action_id):
    return {
        "user": {"id": user_id},
        "channel": {"id": _CHANNEL_ID},
        "message": {"ts": "1780000000.0005", "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": "Not deleted yet"}},
            {"type": "actions", "elements": [
                {"type": "button", "action_id": action_id, "value": stash_id}]},
        ]},
        "actions": [{"action_id": action_id, "value": stash_id}],
    }


class TestTapLogging:
    def test_cancel_tap_writes_an_attributable_log_line(self, caplog):
        sid = _stash_asana_delete()
        with caplog.at_level(logging.INFO, logger="cora.app"):
            capp._handle_confirm_tap(
                _tap_body(HARRISON, sid, cc.ACTION_CANCEL), _FakeClient(),
                action="cancel")
        line = next((r.getMessage() for r in caplog.records
                     if "confirm_card TAP" in r.getMessage()), None)
        assert line is not None, "a tapped Cancel must be attributable"
        assert "action=cancel" in line
        assert "outcome=cancelled" in line
        assert "kind=asana" in line
        assert HARRISON in line
        assert sid in line

    def test_confirm_tap_logs_the_same_shape(self, caplog):
        sid = _stash_asana_delete()
        with caplog.at_level(logging.INFO, logger="cora.app"), \
             patch.object(td.asana_client, "delete_task", return_value=None):
            capp._handle_confirm_tap(
                _tap_body(HARRISON, sid, cc.ACTION_CONFIRM), _FakeClient(),
                action="confirm")
        line = next((r.getMessage() for r in caplog.records
                     if "confirm_card TAP" in r.getMessage()), None)
        assert line is not None
        assert "action=confirm" in line and "outcome=executed" in line

    def test_the_log_line_carries_no_preview_payload(self, caplog):
        """Payload-free by construction: the kind and the opaque id only."""
        sid = _stash_asana_delete()
        with caplog.at_level(logging.INFO, logger="cora.app"):
            capp._handle_confirm_tap(
                _tap_body(HARRISON, sid, cc.ACTION_CANCEL), _FakeClient(),
                action="cancel")
        line = next(r.getMessage() for r in caplog.records
                    if "confirm_card TAP" in r.getMessage())
        assert "Test task" not in line
        assert "Not deleted yet" not in line

    def test_unauthorized_tap_is_also_logged(self, caplog):
        sid = _stash_asana_delete(user=HARRISON)
        with caplog.at_level(logging.INFO, logger="cora.app"):
            capp._handle_confirm_tap(
                _tap_body("U_ATTACKER", sid, cc.ACTION_CONFIRM), _FakeClient(),
                action="confirm")
        line = next((r.getMessage() for r in caplog.records
                     if "confirm_card TAP" in r.getMessage()), None)
        assert line is not None
        assert "outcome=unauthorized" in line

    def test_item_index_appears_when_present(self, caplog):
        sid = cc.mint_stash_id("meeting_item", HARRISON, _CH)
        td._classb_stash("meeting_item", HARRISON, _CH, {
            "items": ["do a thing"], "claimed": [False], "transcript_id": "t1",
            "entity": "OSN", "is_dm": False,
        })
        live = td._CLASSB["meeting_item"]["peek"](HARRISON, _CH)
        with caplog.at_level(logging.INFO, logger="cora.app"):
            capp._handle_confirm_tap(
                _tap_body(HARRISON, live["stash_id"], cc.ACTION_CANCEL_ITEM),
                _FakeClient(), action="cancel", item_index=0)
        line = next((r.getMessage() for r in caplog.records
                     if "confirm_card TAP" in r.getMessage()), None)
        assert line is not None and "item=0" in line
