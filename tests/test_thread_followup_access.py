"""Path 2 (active-thread follow-up) user_access parity — hygiene Session 2 (D-068).

Before this fix, handle_message_event Path 2 ran rate-limit + sibling +
cross-entity guards but skipped user_access.check_access, so the
entity-authorization, finance-topic (D-064), and PHI blocks enforced at the
initial @mention (handle_mention) and on /cora-ask did NOT hold for in-thread
follow-ups. The fix mirrors those paths exactly: same params
(phi_custodian via lex_phi_access.phi_allowed, tier via channel_classifier),
same ordering (check_access -> sibling -> cross), refusal posted in-thread.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import cora.app as app_module


TOMMY = "U0B3RU5Q55G"  # real roster: F3E sales role, 'financials' blocked
ALEX = "U0B3VGWJTMJ"   # real roster: F3E/UFL/HJRG, 'financials' blocked


def _thread_event(user=TOMMY, text="what about the pricing?", channel="C123CHAN",
                  ts="200.2", thread_ts="200.1"):
    """A channel thread-reply event that reaches Path 2 (not DM, not top-level)."""
    return {"user": user, "text": text, "channel": channel, "ts": ts,
            "thread_ts": thread_ts, "channel_type": "channel"}


@pytest.fixture
def path2_mocks():
    """Drive handle_message_event into Path 2 with every guard defaulted to pass."""
    with patch.object(app_module.team_learning, "get_pending_confirm", return_value=None), \
         patch.object(app_module.team_learning, "is_correction", return_value=False), \
         patch.object(app_module.active_thread_store, "is_active", return_value=True), \
         patch.object(app_module.active_thread_store, "touch"), \
         patch.object(app_module.rate_limiter, "check", return_value=(True, None)), \
         patch.object(app_module, "_resolve_channel_name", return_value="f3e-sales"), \
         patch.object(app_module, "route", return_value="F3E") as route, \
         patch.object(app_module.lex_phi_access, "phi_allowed", return_value=False) as phi, \
         patch.object(app_module.user_access, "check_access", return_value=None) as access, \
         patch.object(app_module.sibling_guard, "check_redirect", return_value=None) as sibling, \
         patch.object(app_module.cross_entity_guard, "check_cross_entity", return_value=None) as cross, \
         patch.object(app_module, "_fetch_thread_history", return_value=[]), \
         patch.object(app_module, "_dispatch_qa") as dispatch:
        yield SimpleNamespace(route=route, phi=phi, access=access,
                              sibling=sibling, cross=cross, dispatch=dispatch)


class TestFollowupAccessCheck:
    def test_blocked_followup_refused_in_thread(self, path2_mocks):
        path2_mocks.access.return_value = "You're not authorized for that topic."
        client = MagicMock()
        app_module.handle_message_event(_thread_event(), client)
        client.chat_postMessage.assert_called_once_with(
            channel="C123CHAN", thread_ts="200.1",
            text="You're not authorized for that topic.",
        )
        path2_mocks.dispatch.assert_not_called()
        # check_access fires BEFORE sibling/cross (mention + /cora-ask parity),
        # so a blocked question never reaches the other guards.
        path2_mocks.sibling.assert_not_called()
        path2_mocks.cross.assert_not_called()

    def test_authorized_followup_still_passes(self, path2_mocks):
        client = MagicMock()
        app_module.handle_message_event(_thread_event(), client)
        path2_mocks.access.assert_called_once()
        path2_mocks.dispatch.assert_called_once()
        client.chat_postMessage.assert_not_called()

    def test_check_access_params_mirror_mention_path(self, path2_mocks):
        client = MagicMock()
        app_module.handle_message_event(_thread_event(text="whats the plan"), client)
        # phi_custodian derived exactly as handle_mention does (channel -> not DM).
        path2_mocks.phi.assert_called_once_with(TOMMY, "F3E", is_dm=False)
        # tier computed via the real channel_classifier: f3e-sales -> TIER_3.
        path2_mocks.access.assert_called_once_with(
            TOMMY, "F3E", "whats the plan", phi_custodian=False, tier="TIER_3",
        )

    def test_phi_custodian_flag_passes_through(self, path2_mocks):
        path2_mocks.phi.return_value = True
        client = MagicMock()
        app_module.handle_message_event(_thread_event(), client)
        assert path2_mocks.access.call_args.kwargs["phi_custodian"] is True


class TestFollowupD064Integration:
    """REAL user_access.check_access on Path 2: a company-finance follow-up from
    a financials-blocked sales role is deflected in-thread (previously it ran
    straight to the LLM); a commercial deal-scoped follow-up still passes."""

    @pytest.fixture
    def path2_real_access(self):
        with patch.object(app_module.team_learning, "get_pending_confirm", return_value=None), \
             patch.object(app_module.team_learning, "is_correction", return_value=False), \
             patch.object(app_module.active_thread_store, "is_active", return_value=True), \
             patch.object(app_module.active_thread_store, "touch"), \
             patch.object(app_module.rate_limiter, "check", return_value=(True, None)), \
             patch.object(app_module, "_resolve_channel_name", return_value="f3e-sales"), \
             patch.object(app_module, "_fetch_thread_history", return_value=[]), \
             patch.object(app_module, "_dispatch_qa") as dispatch:
            yield dispatch

    def test_company_finance_followup_deflected(self, path2_real_access):
        client = MagicMock()
        app_module.handle_message_event(
            _thread_event(user=TOMMY, text="what's our p&l this quarter?"), client)
        path2_real_access.assert_not_called()
        client.chat_postMessage.assert_called_once()
        kwargs = client.chat_postMessage.call_args.kwargs
        assert kwargs["channel"] == "C123CHAN"
        assert kwargs["thread_ts"] == "200.1"
        assert kwargs["text"]                     # standard refusal posted in-thread
        assert "F3E" not in kwargs["text"]        # no internal entity-code leak

    def test_commercial_followup_passes_real_access(self, path2_real_access):
        client = MagicMock()
        app_module.handle_message_event(
            _thread_event(user=TOMMY, text="what's the price on the Reliant order?"),
            client)
        client.chat_postMessage.assert_not_called()
        path2_real_access.assert_called_once()

    def test_unknown_user_fails_closed(self, path2_real_access):
        client = MagicMock()
        app_module.handle_message_event(
            _thread_event(user="U_UNKNOWN_USER_XYZ", text="how are sales going?"),
            client)
        path2_real_access.assert_not_called()
        client.chat_postMessage.assert_called_once()
        assert client.chat_postMessage.call_args.kwargs["text"]
