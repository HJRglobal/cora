"""Wrapper-level coverage for four dispatch tools that had ZERO tests (W3-06).

Grepping the whole corpus, these tool names appeared in no test file:
  * slack_send_dm         -- a direct-SEND write tool carrying the LEX/PHI block,
                             the confirmed=true staged-write gate, and
                             recipient-must-be-mapped resolution (all load-bearing,
                             none regression-pinned).
  * financial_get_pulse   -- own TIER gate at the wrapper (currently _is_finance_channel).
  * financial_get_close_pack -- same gate + a required `period`.
  * f3_create_sales_deck  -- pure delegate to sales_deck_client.

These are pure unit tests of the wrapper contract (gates + delegation), mocking
the downstream clients / Slack SDK. No restart, no live calls.

NOTE (W3-04): financial_get_pulse/close_pack gate on _is_finance_channel (channel
name endswith '-finance'), which DIVERGES from the _is_tier1_channel gate every
sibling financial tool uses. That divergence is a separate finding (W3-04, a
code fix, not this test-only slice). These tests deliberately pin the CURRENT
behavior — a TIER_1-but-not-'-finance' channel (e.g. '*-leadership') refuses. When
W3-04 aligns the gate, the '*-leadership refuses' expectations here update with it.
"""

from unittest.mock import MagicMock, patch

import slack_sdk

import cora.tools.tool_dispatch as td


# ─────────────────────────────────────────────────────────────────────────────
# slack_send_dm — the direct-send write tool
# ─────────────────────────────────────────────────────────────────────────────
class TestSlackSendDm:
    def test_lex_channel_blocked_before_any_send(self):
        with patch.object(slack_sdk, "WebClient") as WC:
            result = td._tool_slack_send_dm(
                "U_ASKER", "LEX-LLC",
                {"confirmed": True, "recipient_name": "Tommy", "message": "hi",
                 "_channel_id": "C_LEX"},
            )
        assert "blocked" in result.lower()
        WC.assert_not_called()  # no Slack client ever constructed for a LEX ask

    def test_unconfirmed_refuses_before_any_send(self):
        with patch.object(slack_sdk, "WebClient") as WC:
            result = td._tool_slack_send_dm(
                "U_ASKER", "F3E",
                {"recipient_name": "Tommy", "message": "hi"},  # no confirmed=true
            )
        assert "refused" in result.lower()
        WC.assert_not_called()

    def test_confirmed_string_true_does_not_satisfy_gate(self):
        # The gate is `confirmed is not True` — a truthy string must NOT pass it.
        with patch.object(slack_sdk, "WebClient") as WC:
            result = td._tool_slack_send_dm(
                "U_ASKER", "F3E",
                {"confirmed": "true", "recipient_name": "Tommy", "message": "hi"},
            )
        assert "refused" in result.lower()
        WC.assert_not_called()

    def test_unmapped_recipient_refuses(self):
        with patch.object(td, "resolve_name_to_slack_user_id",
                          return_value=(None, "no match")) as resolve, \
             patch.object(slack_sdk, "WebClient") as WC:
            result = td._tool_slack_send_dm(
                "U_ASKER", "F3E",
                {"confirmed": True, "recipient_name": "Nobody", "message": "hi"},
            )
        resolve.assert_called_once()
        assert "could not resolve" in result.lower()
        WC.assert_not_called()

    def test_mapped_and_confirmed_sends(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test-token")
        fake_client = MagicMock()
        fake_client.conversations_open.return_value = {"channel": {"id": "D_TOMMY"}}
        fake_client.chat_postMessage.return_value = {"ts": "1700.1"}
        with patch.object(td, "resolve_name_to_slack_user_id",
                          return_value=("U_TOMMY", None)), \
             patch.object(td, "_load_slack_asana_map",
                          return_value={"U_TOMMY": {"display_name": "Tommy"}}), \
             patch.object(slack_sdk, "WebClient", return_value=fake_client):
            result = td._tool_slack_send_dm(
                "U_ASKER", "F3E",
                {"confirmed": True, "recipient_name": "Tommy", "message": "ship it"},
            )
        fake_client.conversations_open.assert_called_once_with(users=["U_TOMMY"])
        fake_client.chat_postMessage.assert_called_once_with(channel="D_TOMMY", text="ship it")
        assert result.startswith("WRITE_CONFIRMED")
        assert "DM sent to Tommy" in result


# ─────────────────────────────────────────────────────────────────────────────
# financial_get_pulse / financial_get_close_pack — wrapper TIER gate
# ─────────────────────────────────────────────────────────────────────────────
class TestFinancialGetPulse:
    def test_non_finance_channel_refuses(self):
        # '*-leadership' is TIER_1 for other financial tools but not '-finance',
        # so the current _is_finance_channel gate refuses (pins W3-04 status quo).
        result = td._tool_financial_get_pulse(
            "U1", "F3E", {"_channel_name": "f3e-leadership"})
        assert result == td._FINANCE_CHANNEL_REQUIRED

    def test_finance_channel_passes_to_client(self):
        with patch.object(td.financial_client, "get_entity_pulse_text",
                          return_value="PULSE-OK") as pulse:
            result = td._tool_financial_get_pulse(
                "U1", "F3E", {"_channel_name": "f3e-finance"})
        assert result == "PULSE-OK"
        pulse.assert_called_once_with(entity="F3E", channel="f3e-finance", user="U1")


class TestFinancialGetClosePack:
    def test_non_finance_channel_refuses(self):
        result = td._tool_financial_get_close_pack(
            "U1", "F3E", {"_channel_name": "f3e-leadership", "period": "2026-04"})
        assert result == td._FINANCE_CHANNEL_REQUIRED

    def test_finance_channel_requires_period(self):
        with patch.object(td.financial_client, "get_close_pack_text") as cp:
            result = td._tool_financial_get_close_pack(
                "U1", "F3E", {"_channel_name": "f3e-finance"})
        assert "period is required" in result.lower()
        cp.assert_not_called()

    def test_finance_channel_with_period_passes_to_client(self):
        with patch.object(td.financial_client, "get_close_pack_text",
                          return_value="CLOSEPACK-OK") as cp:
            result = td._tool_financial_get_close_pack(
                "U1", "F3E", {"_channel_name": "f3e-finance", "period": "2026-04"})
        assert result == "CLOSEPACK-OK"
        cp.assert_called_once_with(
            entity="F3E", period="2026-04", doctype="pl",
            channel="f3e-finance", user="U1")


# ─────────────────────────────────────────────────────────────────────────────
# f3_create_sales_deck — pure delegate
# ─────────────────────────────────────────────────────────────────────────────
class TestF3CreateSalesDeck:
    def test_delegates_to_sales_deck_client(self):
        sentinel = "DECK-RESULT"
        with patch.object(td.sales_deck_client, "handle_f3_create_sales_deck",
                          return_value=sentinel) as handler:
            result = td._tool_f3_create_sales_deck("U1", "F3E", {"topic": "Q3"})
        assert result is sentinel
        handler.assert_called_once_with("U1", "F3E", {"topic": "Q3"})
