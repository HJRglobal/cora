"""Tests for plain-DM Q&A routing (fix shipped 2026-06-11).

Background: Slack does not deliver app_mention events for IMs, so the
message-event DM branch is the ONLY DM entry point. Before the fix, every
non-retrieval DM fell through to the OSN shift scheduler greeting — plain
Q&A (incl. the Phase 5 personal-notes write path) was unreachable in DMs.

The contract under test:
  - shift scheduler keeps mid-flow users and explicit scheduler phrases
  - everything else runs the guarded Q&A pipeline (_handle_dm_qa)
  - _handle_dm_qa mirrors handle_mention's guards: rate limit, user_access
    (incl. PHI custodian), help intent, sibling + cross-entity
  - entity = asker's org-roles primary entity (advisory pick, not access);
    unknown users fall back to FNDR; Harrison is FNDR
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import cora.app as app_module


HARRISON = "U0B2RM2JYJ1"
TOMMY = "U_TOMMY_TEST"


# ── _dm_is_shift_message ──────────────────────────────────────────────────────

class TestShiftMessageDetection:
    def test_mid_flow_state_always_routes_to_scheduler(self):
        with patch.object(app_module.osn_shift_handler, "get_dm_state",
                          return_value={"step": "collecting_days"}):
            assert app_module._dm_is_shift_message(TOMMY, "remember the vendor is Apex") is True

    def test_idle_plus_scheduler_phrase_routes_to_scheduler(self):
        with patch.object(app_module.osn_shift_handler, "get_dm_state",
                          return_value={"step": "idle"}):
            assert app_module._dm_is_shift_message(TOMMY, "submit availability") is True
            assert app_module._dm_is_shift_message(TOMMY, "when do I work this week?") is True

    def test_idle_plus_question_routes_to_qa(self):
        with patch.object(app_module.osn_shift_handler, "get_dm_state",
                          return_value={"step": "idle"}):
            assert app_module._dm_is_shift_message(
                TOMMY, "Cora, remember the Tucson stove vendor is Apex Appliance"
            ) is False
            assert app_module._dm_is_shift_message(TOMMY, "what's on my plate") is False

    def test_state_lookup_failure_falls_back_to_keywords(self):
        with patch.object(app_module.osn_shift_handler, "get_dm_state",
                          side_effect=RuntimeError("db gone")):
            assert app_module._dm_is_shift_message(TOMMY, "remember X") is False
            assert app_module._dm_is_shift_message(TOMMY, "my shifts") is True


# ── _fetch_dm_history ─────────────────────────────────────────────────────────

class TestFetchDmHistory:
    def test_orders_merges_and_excludes_current(self):
        client = MagicMock()
        # Slack returns newest first.
        client.conversations_history.return_value = {
            "messages": [
                {"ts": "4", "text": "current question", "user": TOMMY},
                {"ts": "3", "text": "Saved to your notes.", "bot_id": "B1"},
                {"ts": "2", "text": "yes", "user": TOMMY},
                {"ts": "1", "text": "remember the vendor is Apex", "user": TOMMY},
            ]
        }
        history = app_module._fetch_dm_history(client, "D123", current_msg_ts="4")
        assert history == [
            {"role": "user", "content": "remember the vendor is Apex\nyes"},
            {"role": "assistant", "content": "Saved to your notes."},
        ]

    def test_leading_assistant_turns_dropped(self):
        client = MagicMock()
        client.conversations_history.return_value = {
            "messages": [
                {"ts": "2", "text": "current", "user": TOMMY},
                {"ts": "1", "text": "Hi! I can help.", "bot_id": "B1"},
            ]
        }
        assert app_module._fetch_dm_history(client, "D123", current_msg_ts="2") == []

    def test_api_error_returns_empty(self):
        client = MagicMock()
        client.conversations_history.side_effect = RuntimeError("boom")
        assert app_module._fetch_dm_history(client, "D123", current_msg_ts="1") == []


# ── _handle_dm_qa guard sequence ──────────────────────────────────────────────

def _event(user=TOMMY, text="remember the vendor is Apex", channel="D123", ts="100.1"):
    return {"user": user, "text": text, "channel": channel, "ts": ts, "channel_type": "im"}


@pytest.fixture
def qa_mocks():
    """Patch every guard + dispatch around _handle_dm_qa, defaulted to 'pass'."""
    with patch.object(app_module.rate_limiter, "check", return_value=(True, None)) as rate, \
         patch.object(app_module, "_resolve_bot_user_id"), \
         patch.object(app_module.org_roles, "get_role", return_value=None) as get_role, \
         patch.object(app_module.lex_phi_access, "phi_allowed", return_value=False), \
         patch.object(app_module.user_access, "check_access", return_value=None) as access, \
         patch.object(app_module.help_responder, "is_help_intent", return_value=False), \
         patch.object(app_module.sibling_guard, "check_redirect", return_value=None), \
         patch.object(app_module.cross_entity_guard, "check_cross_entity", return_value=None), \
         patch.object(app_module, "_fetch_dm_history", return_value=[]), \
         patch.object(app_module, "_dispatch_qa") as dispatch:
        yield SimpleNamespace(rate=rate, get_role=get_role, access=access, dispatch=dispatch)


class TestHandleDmQa:
    def test_dispatches_with_org_roles_primary_entity(self, qa_mocks):
        qa_mocks.get_role.return_value = SimpleNamespace(primary_entity="F3E")
        app_module._handle_dm_qa(_event(), MagicMock(), TOMMY, "remember the vendor is Apex")
        assert qa_mocks.dispatch.call_count == 1
        kwargs = qa_mocks.dispatch.call_args.kwargs
        assert kwargs["entity"] == "F3E"
        assert kwargs["channel_name"] == "dm"
        assert kwargs["reply_thread_ts"] is None

    def test_unknown_user_falls_back_to_fndr(self, qa_mocks):
        qa_mocks.get_role.return_value = None
        app_module._handle_dm_qa(_event(user="U_STRANGER"), MagicMock(), "U_STRANGER", "hello")
        assert qa_mocks.dispatch.call_args.kwargs["entity"] == "FNDR"

    def test_harrison_is_fndr_regardless_of_registry(self, qa_mocks):
        qa_mocks.get_role.return_value = SimpleNamespace(primary_entity="HJRG")
        app_module._handle_dm_qa(_event(user=HARRISON), MagicMock(), HARRISON, "remember X")
        assert qa_mocks.dispatch.call_args.kwargs["entity"] == "FNDR"

    def test_access_block_stops_dispatch(self, qa_mocks):
        qa_mocks.access.return_value = "That's outside what I can help with."
        client = MagicMock()
        app_module._handle_dm_qa(_event(), client, TOMMY, "show me the cap table")
        qa_mocks.dispatch.assert_not_called()
        assert client.chat_postMessage.call_count == 1

    def test_rate_limit_stops_dispatch(self, qa_mocks):
        qa_mocks.rate.return_value = (False, "user")
        client = MagicMock()
        app_module._handle_dm_qa(_event(), client, TOMMY, "remember X")
        qa_mocks.dispatch.assert_not_called()

    def test_user_access_always_consulted(self, qa_mocks):
        app_module._handle_dm_qa(_event(), MagicMock(), TOMMY, "remember X")
        assert qa_mocks.access.call_count == 1


# ── handle_message_event DM branch routing ────────────────────────────────────

class TestDmBranchRouting:
    @pytest.fixture
    def branch_mocks(self):
        with patch.object(app_module.gap_autofill, "match_pending_ask", return_value=None), \
             patch.object(app_module.historical_access, "detect_retrieval_intent",
                          return_value=False), \
             patch.object(app_module.osn_shift_handler, "handle_dm") as shift, \
             patch.object(app_module, "_handle_dm_qa") as qa:
            yield SimpleNamespace(shift=shift, qa=qa)

    def test_question_dm_routes_to_qa(self, branch_mocks):
        with patch.object(app_module, "_dm_is_shift_message", return_value=False):
            app_module.handle_message_event(
                _event(text="Cora, remember the Tucson stove vendor is Apex Appliance"),
                MagicMock(),
            )
        branch_mocks.qa.assert_called_once()
        branch_mocks.shift.assert_not_called()

    def test_scheduler_dm_routes_to_shift_handler(self, branch_mocks):
        with patch.object(app_module, "_dm_is_shift_message", return_value=True):
            app_module.handle_message_event(_event(text="submit availability"), MagicMock())
        branch_mocks.shift.assert_called_once()
        branch_mocks.qa.assert_not_called()

    def test_bot_dms_ignored(self, branch_mocks):
        event = _event()
        event["bot_id"] = "B1"
        app_module.handle_message_event(event, MagicMock())
        branch_mocks.shift.assert_not_called()
        branch_mocks.qa.assert_not_called()

    def test_retrieval_intent_still_takes_tier2_path(self, branch_mocks):
        # "pull up my emails" keeps its dedicated Tier-2 grant path.
        with patch.object(app_module.historical_access, "detect_retrieval_intent",
                          return_value=True), \
             patch.object(app_module.rate_limiter, "check", return_value=(True, None)), \
             patch.object(app_module, "_dispatch_qa") as dispatch:
            app_module.handle_message_event(_event(text="pull up my emails"), MagicMock())
        dispatch.assert_called_once()
        branch_mocks.qa.assert_not_called()
        branch_mocks.shift.assert_not_called()
