"""Unit tests for app.py internal helper functions.

Tests the three pure-ish helpers that can be exercised without a running
Bolt app or real Slack events:

  _extract_and_log_gap  — strips the CORA_KNOWLEDGE_GAP sentinel from
                          Claude's response and logs the gap event.
  _try_cache_store      — conditionally writes to the semantic cache,
                          swallowing all exceptions.
  _resolve_bot_user_id  — lazy-resolves and caches Cora's own bot user ID.
"""

from unittest.mock import MagicMock, call, patch

import pytest

# Importing cora.app also creates the Bolt App singleton (using the dummy
# tokens set by conftest.py), which is acceptable for unit tests.
from cora.app import _extract_and_log_gap, _resolve_bot_user_id, _try_cache_store
import cora.app as app_module
from cora.intent_classifier import Intent, RoutingHints


# ── Helpers ───────────────────────────────────────────────────────────────────

def _hints(bypass_cache: bool = False, cache_ttl: int = 1800) -> RoutingHints:
    return RoutingHints(
        intent=Intent.COMPLEX,
        skip_kb=False,
        kb_k_override=None,
        bypass_cache=bypass_cache,
        cache_ttl=cache_ttl,
    )


_EMBEDDING = [0.1] * 1536


# ── _extract_and_log_gap ──────────────────────────────────────────────────────

class TestExtractAndLogGap:
    def test_no_sentinel_returns_text_unchanged(self):
        text = "Normal Cora response with no gap marker."
        result = _extract_and_log_gap(text, "F3E", "f3e-leadership", "U1", "q?", 500)
        assert result == text

    def test_sentinel_stripped_from_response(self):
        text = "Here is my answer.\n\n[CORA_KNOWLEDGE_GAP: F3E Sprouts buyer info]"
        with patch("cora.app.knowledge_gaps"), patch("cora.app.uft"):
            result = _extract_and_log_gap(text, "F3E", "f3e-ch", "U1", "q?", 600)
        assert result == "Here is my answer."
        assert "[CORA_KNOWLEDGE_GAP" not in result

    def test_sentinel_case_insensitive(self):
        text = "Answer text.\n\n[cora_knowledge_gap: gap description]"
        with patch("cora.app.knowledge_gaps"), patch("cora.app.uft"):
            result = _extract_and_log_gap(text, "OSN", "osn-ch", "U2", "q?", 300)
        assert "cora_knowledge_gap" not in result.lower()

    def test_gap_description_extracted_correctly(self):
        gap_desc = "Missing F3E Sprouts buyer contact"
        text = f"Some answer.\n\n[CORA_KNOWLEDGE_GAP: {gap_desc}]"
        with patch("cora.app.knowledge_gaps") as mock_kg, patch("cora.app.uft"):
            _extract_and_log_gap(text, "F3E", "f3e-leadership", "U3", "who?", 1200)
        mock_kg.log_gap.assert_called_once()
        kwargs = mock_kg.log_gap.call_args[1]
        assert kwargs["gap"] == gap_desc

    def test_gap_log_receives_correct_entity_and_latency(self):
        text = "Answer.\n\n[CORA_KNOWLEDGE_GAP: some gap]"
        with patch("cora.app.knowledge_gaps") as mock_kg, patch("cora.app.uft"):
            _extract_and_log_gap(text, "OSN", "osn-finance", "U4", "p&l?", 2500)
        kwargs = mock_kg.log_gap.call_args[1]
        assert kwargs["entity"] == "OSN"
        assert kwargs["latency_ms"] == 2500
        assert kwargs["channel"] == "osn-finance"

    def test_uft_log_called_with_gap_info(self):
        gap_desc = "Q4 revenue data unavailable"
        text = f"Partial answer.\n\n[CORA_KNOWLEDGE_GAP: {gap_desc}]"
        with patch("cora.app.knowledge_gaps"), patch("cora.app.uft") as mock_uft:
            _extract_and_log_gap(text, "F3E", "f3e-ch", "U5", "q4 rev?", 800)
        mock_uft.log_knowledge_gap.assert_called_once()
        kwargs = mock_uft.log_knowledge_gap.call_args[1]
        assert kwargs["gap_description"] == gap_desc
        assert kwargs["entity"] == "F3E"

    def test_user_id_none_handled_without_error(self):
        text = "Answer.\n\n[CORA_KNOWLEDGE_GAP: some gap]"
        with patch("cora.app.knowledge_gaps"), patch("cora.app.uft"):
            # user_id=None should not raise — uft receives empty string
            result = _extract_and_log_gap(text, "OSN", "osn-ch", None, "q?", 300)
        assert "CORA_KNOWLEDGE_GAP" not in result

    def test_no_log_calls_when_no_sentinel(self):
        text = "Clean response with no sentinel."
        with patch("cora.app.knowledge_gaps") as mock_kg, patch("cora.app.uft") as mock_uft:
            _extract_and_log_gap(text, "F3E", "f3e-ch", "U1", "q?", 400)
        mock_kg.log_gap.assert_not_called()
        mock_uft.log_knowledge_gap.assert_not_called()

    def test_trailing_whitespace_stripped_from_cleaned_response(self):
        text = "Answer here.   \n\n[CORA_KNOWLEDGE_GAP: gap]"
        with patch("cora.app.knowledge_gaps"), patch("cora.app.uft"):
            result = _extract_and_log_gap(text, "F3E", "ch", "U1", "q?", 200)
        assert not result.endswith(" ")
        assert not result.endswith("\n")


# ── _try_cache_store ──────────────────────────────────────────────────────────

class TestTryCacheStore:
    def test_store_called_when_all_conditions_met(self):
        with patch("cora.app.sc") as mock_sc:
            _try_cache_store("F3E", "What is the P&L?", _EMBEDDING, "The P&L is...", _hints())
        mock_sc.get_cache.return_value.store.assert_called_once()

    def test_store_skipped_when_bypass_cache_true(self):
        with patch("cora.app.sc") as mock_sc:
            _try_cache_store("F3E", "q?", _EMBEDDING, "resp", _hints(bypass_cache=True))
        mock_sc.get_cache.assert_not_called()

    def test_store_skipped_when_embedding_is_none(self):
        with patch("cora.app.sc") as mock_sc:
            _try_cache_store("F3E", "q?", None, "resp", _hints())
        mock_sc.get_cache.assert_not_called()

    def test_store_skipped_when_cache_ttl_is_zero(self):
        with patch("cora.app.sc") as mock_sc:
            _try_cache_store("F3E", "q?", _EMBEDDING, "resp", _hints(cache_ttl=0))
        mock_sc.get_cache.assert_not_called()

    def test_store_skipped_when_cache_ttl_is_negative(self):
        with patch("cora.app.sc") as mock_sc:
            _try_cache_store("F3E", "q?", _EMBEDDING, "resp", _hints(cache_ttl=-1))
        mock_sc.get_cache.assert_not_called()

    def test_store_passes_correct_kwargs(self):
        with patch("cora.app.sc") as mock_sc:
            _try_cache_store("OSN", "revenue?", _EMBEDDING, "OSN answer", _hints(cache_ttl=900))
        store_call = mock_sc.get_cache.return_value.store
        store_call.assert_called_once()
        kwargs = store_call.call_args[1]
        assert kwargs["entity"] == "OSN"
        assert kwargs["question"] == "revenue?"
        assert kwargs["question_embedding"] == _EMBEDDING
        assert kwargs["response"] == "OSN answer"
        assert kwargs["ttl_seconds"] == 900

    def test_cache_exception_does_not_propagate(self):
        with patch("cora.app.sc") as mock_sc:
            mock_sc.get_cache.side_effect = RuntimeError("cache exploded")
            # Must silently swallow the error
            _try_cache_store("F3E", "q?", _EMBEDDING, "resp", _hints())

    def test_store_exception_does_not_propagate(self):
        with patch("cora.app.sc") as mock_sc:
            mock_sc.get_cache.return_value.store.side_effect = Exception("db locked")
            _try_cache_store("F3E", "q?", _EMBEDDING, "resp", _hints())


# ── _resolve_bot_user_id ──────────────────────────────────────────────────────

class TestResolveBotUserId:
    def setup_method(self):
        # Reset the module-level cached value before each test
        app_module._CORA_BOT_USER_ID = None

    def teardown_method(self):
        app_module._CORA_BOT_USER_ID = None

    def test_resolves_user_id_from_auth_test(self):
        mock_client = MagicMock()
        mock_client.auth_test.return_value = {"user_id": "U_BOT_123"}
        result = _resolve_bot_user_id(mock_client)
        assert result == "U_BOT_123"

    def test_result_is_cached_after_first_call(self):
        mock_client = MagicMock()
        mock_client.auth_test.return_value = {"user_id": "U_BOT_456"}
        _resolve_bot_user_id(mock_client)
        _resolve_bot_user_id(mock_client)
        # auth_test should only be called once despite two invocations
        mock_client.auth_test.assert_called_once()

    def test_returns_none_when_auth_test_raises(self):
        mock_client = MagicMock()
        mock_client.auth_test.side_effect = Exception("Slack API error")
        result = _resolve_bot_user_id(mock_client)
        assert result is None

    def test_cached_value_returned_on_second_call(self):
        mock_client = MagicMock()
        mock_client.auth_test.return_value = {"user_id": "U_CACHED"}
        first = _resolve_bot_user_id(mock_client)
        # Poison the client to ensure cache is used, not re-fetched
        mock_client.auth_test.side_effect = Exception("should not be called")
        second = _resolve_bot_user_id(mock_client)
        assert first == second == "U_CACHED"
