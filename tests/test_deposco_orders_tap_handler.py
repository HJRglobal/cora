"""The app.py Slack wrapper for the Deposco push card.

Driven with realistic `block_actions` payloads -- the wrapper is the half the
module tests cannot reach: whether the card is EDITED, whether the reply is
ephemeral, and whether the buttons survive are all decided here. Mirrors
`tests/test_f3e_blog_tap_handler.py` exactly.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from cora import app as app_mod
from cora.connectors import deposco_push as dpush
from cora.deposco_orders import cards as dc_cards
from cora.deposco_orders import handler as dc_handler
from cora.deposco_orders import payload as payload_mod
from cora.deposco_orders import pending, preflight
from cora.deposco_orders import spec as spec_mod

OTHER_USER = "U0B3AEJCYGP"

SPEC = spec_mod.OrderSpec(
    channel="wholesale", buyer_or_fc_code="GOTHAM", reference="4471",
    authored_by="Harrison",
    lines=[spec_mod.OrderSpecLine(sku="PURE-Original", qty=208, unit_price="21.70")],
)
PASSED_PREFLIGHT = preflight.PreflightResult(
    passed=True, checked_at="2026-09-23T10:00:00-07:00",
    checks=[preflight.PreflightCheck("number_miss", True)],
)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CORA_DEPOSCO_PENDING_DIR", str(tmp_path / "pending"))
    monkeypatch.setenv("CORA_DEPOSCO_PUSH_LEDGER_PATH", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("CORA_DEPOSCO_DEMOTION_STATE_PATH", str(tmp_path / "demotion.json"))
    monkeypatch.delenv("CORA_DEPOSCO_STANDING_CHANNELS", raising=False)
    monkeypatch.delenv("CORA_DEPOSCO_PUSH_CHANNELS", raising=False)
    monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "on")
    monkeypatch.delenv("CORA_EVAL_MODE", raising=False)
    yield


def _stage():
    payload = payload_mod.build_payload(SPEC)
    number = payload_mod.build_order_number(SPEC)
    return pending.stage_entry(
        channel="wholesale", number=number, payload=payload, env="prod",
        preflight_result=PASSED_PREFLIGHT, authored_by="Harrison",
        spec_raw={
            "channel": "wholesale", "buyer_or_fc_code": "GOTHAM", "reference": "4471",
            "authored_by": "Harrison", "freight_terms": "Prepaid",
            "lines": [{"sku": "PURE-Original", "qty": 208, "unit_price": "21.70"}],
        },
    )


def _body(pending_id, *, action_id, user=None):
    _, blocks = dc_cards.build_push_card(pending.get_entry(pending_id))
    return {
        "user": {"id": user or dc_handler.HARRISON_ID},
        "channel": {"id": "D0HARRISON"},
        "message": {"ts": "1756200000.123456", "blocks": blocks},
        "actions": [{"action_id": action_id, "value": pending_id}],
    }


def _kinds(blocks):
    return [b.get("type") for b in blocks]


class FakeReadClient:
    def __init__(self):
        self._calls = 0

    def find_order_detail(self, order_type, number):
        self._calls += 1
        if self._calls == 1:
            return None  # not found at the live re-preflight check
        from cora.connectors import deposco_client as dc
        return dc.OrderHeaderRecord(
            number=number, customer_order_number="4471", ship_to_postal_code="11101",
            lines=[dc.OrderHeaderLine(item_number="PURE-Original", order_pack_quantity=208,
                                      unit_price="21.70")],
        )

    def search_orders(self, order_type, **kw):
        from cora.connectors import deposco_client as dc
        return dc.DeposcoResponse("prod", "/search/Order", 200, "<orders/>")

    def item_exists(self, item_number):
        return True

    def get_enterprise_availability(self, item_numbers=None, **kw):
        from cora.connectors import deposco_client as dc
        rows = [dc.EnterpriseInventoryRow(item_number="PURE-Original", measures={"atpQty": 500})]
        return dc.AvailabilityResult(env="prod", rows=rows)


@pytest.fixture(autouse=True)
def patch_reference_check(monkeypatch):
    monkeypatch.setattr(dc_handler.preflight, "_reference_exists", lambda c, r: False)


def _patch_confirmed_push(monkeypatch):
    monkeypatch.setattr(dc_handler.dc, "DeposcoClient", lambda **kw: FakeReadClient())
    push_client = MagicMock()
    push_client.push_order.return_value = dpush.PushOutcome(env="prod", status=207,
                                                             text="201 Created")
    monkeypatch.setattr(dc_handler.dpush, "DeposcoPushClient", lambda **kw: push_client)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_both_actions_are_actually_registered_not_orphaned_decorators():
    names = {getattr(l.ack_function, "__name__", "") for l in app_mod.app._listeners}
    assert "handle_deposco_push_confirm" in names
    assert "handle_deposco_push_dismiss" in names


# ---------------------------------------------------------------------------
# Unauthorized / race outcomes: ephemeral only, card untouched
# ---------------------------------------------------------------------------


def test_a_non_harrison_tap_never_edits_the_shared_card():
    entry = _stage()
    client = MagicMock()
    app_mod._handle_deposco_push_tap(
        _body(entry["id"], action_id=dc_cards.ACTION_PUSH_CONFIRM, user=OTHER_USER),
        client, action="push",
    )
    assert not client.chat_update.called
    client.chat_postEphemeral.assert_called_once()
    assert pending.get_entry(entry["id"])["state"] == pending.STATE_STAGED


def test_an_orphaned_tap_is_ephemeral_only():
    client = MagicMock()
    body = {"user": {"id": dc_handler.HARRISON_ID}, "channel": {"id": "D1"},
            "message": {"ts": "1.1", "blocks": []},
            "actions": [{"action_id": dc_cards.ACTION_PUSH_CONFIRM, "value": "deposco-gone"}]}
    app_mod._handle_deposco_push_tap(body, client, action="push")
    assert not client.chat_update.called
    assert client.chat_postEphemeral.called


# ---------------------------------------------------------------------------
# Terminal outcomes: card is closed, buttons dropped
# ---------------------------------------------------------------------------


def test_a_confirmed_push_closes_the_card_and_drops_the_buttons(monkeypatch):
    entry = _stage()
    _patch_confirmed_push(monkeypatch)
    client = MagicMock()
    app_mod._handle_deposco_push_tap(
        _body(entry["id"], action_id=dc_cards.ACTION_PUSH_CONFIRM), client, action="push",
    )
    kw = client.chat_update.call_args[1]
    kinds = _kinds(kw["blocks"])
    assert "actions" not in kinds, "a confirmed push must not stay tappable"
    body = "\n".join(b["text"]["text"] for b in kw["blocks"] if b.get("type") == "section")
    assert "CONFIRMED" in body
    assert "STAGED, NOT WRITTEN" not in body
    assert pending.get_entry(entry["id"])["state"] == pending.STATE_CONFIRMED


def test_a_dismiss_closes_the_card_and_drops_the_buttons():
    entry = _stage()
    client = MagicMock()
    app_mod._handle_deposco_push_tap(
        _body(entry["id"], action_id=dc_cards.ACTION_PUSH_DISMISS), client, action="dismiss",
    )
    kinds = _kinds(client.chat_update.call_args[1]["blocks"])
    assert "actions" not in kinds
    assert pending.get_entry(entry["id"])["state"] == pending.STATE_DISMISSED


def test_a_failed_push_keeps_the_card_closed_not_retryable_by_tap(monkeypatch):
    """Unlike the blog lane, a FAILED push here is terminal for THIS pending
    entry -- correction happens by editing the spec and re-staging, never by
    re-tapping the same card (design SS4.1)."""
    entry = _stage()
    monkeypatch.setattr(dc_handler.dc, "DeposcoClient", lambda **kw: FakeReadClient())
    push_client = MagicMock()
    push_client.push_order.return_value = dpush.PushOutcome(env="prod", status=400,
                                                             text="missingItems")
    monkeypatch.setattr(dc_handler.dpush, "DeposcoPushClient", lambda **kw: push_client)
    client = MagicMock()
    app_mod._handle_deposco_push_tap(
        _body(entry["id"], action_id=dc_cards.ACTION_PUSH_CONFIRM), client, action="push",
    )
    kinds = _kinds(client.chat_update.call_args[1]["blocks"])
    assert "actions" not in kinds
    assert pending.get_entry(entry["id"])["state"] == pending.STATE_FAILED


# ---------------------------------------------------------------------------
# Kill switches
# ---------------------------------------------------------------------------


def test_buttons_off_does_not_mutate_the_card_and_never_calls_process_push_tap(monkeypatch):
    monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "off")
    entry = _stage()
    client = MagicMock()
    monkeypatch.setattr(
        dc_handler, "process_push_tap",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    app_mod._handle_deposco_push_tap(
        _body(entry["id"], action_id=dc_cards.ACTION_PUSH_CONFIRM), client, action="push",
    )
    assert not client.chat_update.called
    assert pending.get_entry(entry["id"])["state"] == pending.STATE_STAGED


def test_eval_mode_is_inert(monkeypatch):
    monkeypatch.setenv("CORA_EVAL_MODE", "1")
    entry = _stage()
    client = MagicMock()
    app_mod._handle_deposco_push_tap(
        _body(entry["id"], action_id=dc_cards.ACTION_PUSH_CONFIRM), client, action="push",
    )
    assert not client.chat_update.called and not client.chat_postEphemeral.called
    assert pending.get_entry(entry["id"])["state"] == pending.STATE_STAGED


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


def test_a_slack_api_failure_does_not_crash_the_handler(monkeypatch):
    entry = _stage()
    _patch_confirmed_push(monkeypatch)
    client = MagicMock()
    client.chat_update.side_effect = RuntimeError("slack 500")
    app_mod._handle_deposco_push_tap(
        _body(entry["id"], action_id=dc_cards.ACTION_PUSH_CONFIRM), client, action="push",
    )
    # The push still stands -- the card edit failing must not undo it.
    assert pending.get_entry(entry["id"])["state"] == pending.STATE_CONFIRMED


def test_a_malformed_payload_does_not_crash_the_handler():
    client = MagicMock()
    for body in ({}, {"actions": []}, {"actions": [{}], "user": {}},
                 {"user": {"id": dc_handler.HARRISON_ID}, "actions": [{"value": None}]}):
        app_mod._handle_deposco_push_tap(body, client, action="push")
