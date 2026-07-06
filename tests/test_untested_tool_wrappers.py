"""Wrapper-level coverage for four dispatch tools that had ZERO tests (W3-06).

Grepping the whole corpus, these tool names appeared in no test file:
  * slack_send_dm         -- a direct-SEND write tool carrying the LEX/PHI block,
                             the confirmed=true staged-write gate, and
                             recipient-must-be-mapped resolution (all load-bearing,
                             none regression-pinned).
  * financial_get_pulse   -- own TIER gate at the wrapper (now _is_tier1_channel, W3-04).
  * financial_get_close_pack -- same gate + a required `period`.
  * f3_create_sales_deck  -- pure delegate to sales_deck_client.

These are pure unit tests of the wrapper contract (gates + delegation), mocking
the downstream clients / Slack SDK. No restart, no live calls.

NOTE (W3-04, SHIPPED slice 05): financial_get_pulse/close_pack now gate on
_is_tier1_channel like every sibling financial tool (cashflow / QBO / osn_pulse),
replacing the divergent _is_finance_channel (-finance suffix only) gate. A TIER_1
channel that is not literally '*-finance' (e.g. '*-leadership') now PASSES; a
genuinely non-TIER_1 channel (e.g. '*-sales') refuses; a DM refuses (W2-02 — the
tool-level gate pins DMs to TIER_3 regardless of the asker's entity).
"""

import threading
import time
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
    def test_tier3_channel_refuses(self):
        # '*-sales' is a genuinely non-TIER_1 (sales/ops) channel -> refuse.
        result = td._tool_financial_get_pulse(
            "U1", "F3E", {"_channel_name": "f3e-sales"})
        assert result == td._FINANCE_CHANNEL_REQUIRED

    def test_leadership_channel_passes_after_w3_04(self):
        # W3-04: '*-leadership' is TIER_1 and now passes, matching every sibling
        # financial tool (was refused under the old _is_finance_channel gate).
        with patch.object(td.financial_client, "get_entity_pulse_text",
                          return_value="PULSE-OK") as pulse:
            result = td._tool_financial_get_pulse(
                "U1", "F3E", {"_channel_name": "f3e-leadership"})
        assert result == "PULSE-OK"
        pulse.assert_called_once_with(entity="F3E", channel="f3e-leadership", user="U1")

    def test_dm_refuses_even_for_hjrg_primary(self):
        # W2-02: a DM is TIER_3 at the tool gate regardless of the asker's entity.
        # HJRG would short-circuit is_tier_1 True, but channel_name=="dm" pins TIER_3.
        with patch.object(td.financial_client, "get_entity_pulse_text") as pulse:
            result = td._tool_financial_get_pulse(
                "U1", "HJRG", {"_channel_name": "dm"})
        assert result == td._FINANCE_CHANNEL_REQUIRED
        pulse.assert_not_called()

    def test_finance_channel_passes_to_client(self):
        with patch.object(td.financial_client, "get_entity_pulse_text",
                          return_value="PULSE-OK") as pulse:
            result = td._tool_financial_get_pulse(
                "U1", "F3E", {"_channel_name": "f3e-finance"})
        assert result == "PULSE-OK"
        pulse.assert_called_once_with(entity="F3E", channel="f3e-finance", user="U1")


class TestFinancialGetClosePack:
    def test_tier3_channel_refuses(self):
        result = td._tool_financial_get_close_pack(
            "U1", "F3E", {"_channel_name": "f3e-sales", "period": "2026-04"})
        assert result == td._FINANCE_CHANNEL_REQUIRED

    def test_leadership_channel_passes_after_w3_04(self):
        with patch.object(td.financial_client, "get_close_pack_text",
                          return_value="CLOSEPACK-OK") as cp:
            result = td._tool_financial_get_close_pack(
                "U1", "F3E", {"_channel_name": "f3e-leadership", "period": "2026-04"})
        assert result == "CLOSEPACK-OK"

    def test_dm_refuses_even_for_hjrg_primary(self):
        # W2-02: DM pinned TIER_3 -- refuse before any period check or client call.
        with patch.object(td.financial_client, "get_close_pack_text") as cp:
            result = td._tool_financial_get_close_pack(
                "U1", "HJRG", {"_channel_name": "dm", "period": "2026-04"})
        assert result == td._FINANCE_CHANNEL_REQUIRED
        cp.assert_not_called()

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
# W3-01 — a hung tool must be abandoned at its timeout, not block the dispatch
# ─────────────────────────────────────────────────────────────────────────────
class TestDispatchTimeoutBounded:
    def test_hung_tool_returns_at_timeout_not_at_completion(self, monkeypatch):
        release = threading.Event()      # never set until teardown
        entered = threading.Event()

        def _hang(_uid, _entity, _inp):
            entered.set()
            release.wait(timeout=30)     # would block ~30s if not bounded
            return "SLOW-RESULT"

        # Register the hung tool with a short (1s) timeout tier.
        monkeypatch.setitem(td._TOOL_FUNCTIONS, "hang_tool", _hang)
        monkeypatch.setitem(td._TOOL_TIMEOUTS, "hang_tool", 1)
        try:
            t0 = time.monotonic()
            result = td.dispatch("hang_tool", {}, "U1", entity="FNDR")
            elapsed = time.monotonic() - t0
            assert entered.wait(timeout=2), "tool never started"
            # Bounded by the ~1s timeout, NOT the 30s sleep -> the __exit__(wait=True)
            # block is gone. Generous ceiling to stay non-flaky on a loaded CI box.
            assert elapsed < 8, f"dispatch blocked on the hung tool ({elapsed:.1f}s)"
            assert result == "Tool timed out — please try again."
        finally:
            release.set()                # let the orphaned worker exit cleanly


# ─────────────────────────────────────────────────────────────────────────────
# _is_tier1_channel — the shared finance-tool tier gate (W2-02 DM pin + W3-04)
# ─────────────────────────────────────────────────────────────────────────────
class TestIsTier1ChannelGate:
    def test_dm_is_tier3_even_for_hjrg(self):
        # W2-02 core: a DM must never be TIER_1, even though is_tier_1 short-circuits
        # True for the HJRG entity. channel_name=="dm" pins TIER_3.
        assert td._is_tier1_channel("HJRG", "dm") is False

    def test_dm_is_tier3_for_every_entity(self):
        for ent in ("HJRG", "FNDR", "F3E", "OSN", "LEX"):
            assert td._is_tier1_channel(ent, "dm") is False

    def test_empty_channel_is_tier3(self):
        assert td._is_tier1_channel("HJRG", "") is False

    def test_hjrg_real_channel_still_tier1(self):
        # Regression guard: the DM pin must NOT break HJRG's any-channel TIER_1.
        assert td._is_tier1_channel("HJRG", "hjrg-general") is True

    def test_leadership_channel_tier1(self):
        assert td._is_tier1_channel("F3E", "f3e-leadership") is True

    def test_finance_channel_tier1(self):
        assert td._is_tier1_channel("OSN", "osn-finance") is True

    def test_sales_channel_tier3(self):
        assert td._is_tier1_channel("F3E", "f3e-sales") is False


class TestQboToolDmRefusal:
    def test_qbo_pnl_refuses_in_dm_for_hjrg(self):
        # W2-02: the QBO P&L tool must refuse in a DM even for an HJRG-primary asker,
        # before resolving any entity or hitting QBO.
        with patch.object(td.qbo_client, "get_profit_loss") as pnl:
            result = td._tool_qbo_get_profit_loss(
                "U1", "HJRG", {"_channel_name": "dm"})
        assert result == td._QBO_TIER1_REQUIRED
        pnl.assert_not_called()


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
