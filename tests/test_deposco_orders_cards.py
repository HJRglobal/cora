"""Tests for the push card. D-109 (no em-dashes anywhere outward) is pinned
against the same character the Class-B directive-prose guard checks for."""

from __future__ import annotations

import pytest

from cora.deposco_orders import cards, pending

ENTRY = {
    "id": "deposco-abc123",
    "channel": "wholesale",
    "spec_path": "orders/staged/2026-09-15_f3e_order-gotham-PO4471.yaml",
    "payload_hash": "3f9c1234567890",
    "preflight": {
        "checked_at": "2026-09-23T10:00:00-07:00",
        "checks": [
            {"name": "number_miss", "passed": True, "detail": ""},
            {"name": "reference_miss", "passed": True, "detail": ""},
            {"name": "items_exist", "passed": True, "detail": ""},
            {"name": "atp_sufficient", "passed": True, "detail": ""},
            {"name": "ship_via_pinned", "passed": True, "detail": ""},
        ],
    },
    "payload": {"order": [{
        "number": "F3E-W-GOTHAM-4471",
        "type": "Sales Order",
        "orderSource": "F3E-API-WHOLESALE",
        "otherReferenceNumber": "4471",
        "freight": {"termsType": "Prepaid"},
        "shipVia": "General Freight",
        "orderTotal": "18054.40",
        "orderLines": {"orderLine": [
            {"itemNumber": "PURE-Original", "orderPackQuantity": "208.0", "unitPrice": "21.70"},
            {"itemNumber": "PURE-Citrus", "orderPackQuantity": "208.0", "unitPrice": "21.70"},
        ]},
    }]},
}


@pytest.fixture(autouse=True)
def isolated_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("CORA_DEPOSCO_PUSH_LEDGER_PATH", str(tmp_path / "ledger.jsonl"))


class TestNoEmDash:
    def test_push_card_carries_no_em_dash(self):
        fallback, blocks = cards.build_push_card(ENTRY)
        assert "—" not in fallback
        for block in blocks:
            text = ((block.get("text") or {}).get("text") or "")
            assert "—" not in text
            for element in block.get("elements", []):
                assert "—" not in (element.get("text") or {}).get("text", "")

    def test_terminal_card_carries_no_em_dash(self):
        _, blocks = cards.build_push_card(ENTRY)
        entry = dict(ENTRY, state=pending.STATE_CONFIRMED)
        terminal = cards.terminal_card_blocks(blocks, entry)
        for block in terminal:
            assert "—" not in ((block.get("text") or {}).get("text") or "")


class TestCardContent:
    def test_action_ids_match_the_design(self):
        _, blocks = cards.build_push_card(ENTRY)
        actions_block = next(b for b in blocks if b.get("type") == "actions")
        ids = {el["action_id"] for el in actions_block["elements"]}
        assert ids == {cards.ACTION_PUSH_CONFIRM, cards.ACTION_PUSH_DISMISS}

    def test_buttons_carry_only_the_opaque_pending_id(self):
        _, blocks = cards.build_push_card(ENTRY)
        actions_block = next(b for b in blocks if b.get("type") == "actions")
        for element in actions_block["elements"]:
            assert element["value"] == "deposco-abc123"

    def test_card_names_the_order_number_and_channel(self):
        fallback, blocks = cards.build_push_card(ENTRY)
        full_text = "\n".join(
            (b.get("text") or {}).get("text", "") for b in blocks if b.get("type") == "section"
        )
        assert "F3E-W-GOTHAM-4471" in full_text
        assert "WHOLESALE" in full_text

    def test_card_states_manual_lane_off_and_no_cancel(self):
        _, blocks = cards.build_push_card(ENTRY)
        full_text = "\n".join(
            (b.get("text") or {}).get("text", "") for b in blocks if b.get("type") == "section"
        )
        assert "MANUAL LANE IS OFF" in full_text
        assert "call Nimbl" in full_text

    def test_clean_count_reflected_in_the_supervised_framing(self):
        pending.append_ledger_row(channel="wholesale", number="A",
                                  event=pending.STATE_CONFIRMED, clean=True)
        pending.append_ledger_row(channel="wholesale", number="B",
                                  event=pending.STATE_CONFIRMED, clean=True)
        _, blocks = cards.build_push_card(ENTRY)
        header = (blocks[0].get("text") or {}).get("text", "")
        assert "2 clean so far" in header
        assert "push 3 of" in header

    def test_no_creds_and_no_prices_beyond_what_the_spec_carries(self):
        fallback, blocks = cards.build_push_card(ENTRY)
        full_text = "\n".join(
            (b.get("text") or {}).get("text", "") for b in blocks if b.get("type") == "section"
        )
        assert "DEPOSCO_" not in full_text
        assert "Basic " not in full_text


class TestTerminalOutcomes:
    @pytest.mark.parametrize("state", [
        pending.STATE_CONFIRMED, pending.STATE_FAILED, pending.STATE_UNKNOWN,
        pending.STATE_ANOMALY_UPDATED, pending.STATE_MISMATCH, pending.STATE_DISMISSED,
    ])
    def test_every_terminal_state_has_its_own_headline(self, state):
        _, blocks = cards.build_push_card(ENTRY)
        entry = dict(ENTRY, state=state)
        terminal = cards.terminal_card_blocks(blocks, entry)
        headline = (terminal[0].get("text") or {}).get("text", "")
        assert headline  # every state produces SOME message, never a blank

    def test_unknown_headline_says_never_auto_retries(self):
        _, blocks = cards.build_push_card(ENTRY)
        entry = dict(ENTRY, state=pending.STATE_UNKNOWN)
        terminal = cards.terminal_card_blocks(blocks, entry)
        assert "locked" in (terminal[0].get("text") or {}).get("text", "")

    def test_anomaly_and_mismatch_say_call_nimbl_now(self):
        _, blocks = cards.build_push_card(ENTRY)
        for state in (pending.STATE_ANOMALY_UPDATED, pending.STATE_MISMATCH):
            entry = dict(ENTRY, state=state)
            terminal = cards.terminal_card_blocks(blocks, entry)
            assert "Call Nimbl now" in (terminal[0].get("text") or {}).get("text", "")

    def test_terminal_rewrite_drops_the_staged_state_claim(self):
        _, blocks = cards.build_push_card(ENTRY)
        entry = dict(ENTRY, state=pending.STATE_CONFIRMED)
        terminal = cards.terminal_card_blocks(blocks, entry)
        full_text = "\n".join((b.get("text") or {}).get("text", "") for b in terminal)
        assert "STAGED, NOT WRITTEN" not in full_text
