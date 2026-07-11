"""Unit tests for claude_client.generate_response()."""

from unittest.mock import MagicMock, patch

import anthropic
import pytest

import cora.claude_client as cl


def _mock_success(text="Hello from Claude"):
    response = MagicMock()
    block = MagicMock()
    block.type = "text"
    block.text = text
    response.content = [block]
    response.stop_reason = "end_turn"  # not "tool_use" — signals model is done
    return response


def _conn_error():
    return anthropic.APIConnectionError(request=MagicMock())


def _auth_error():
    resp = MagicMock()
    resp.status_code = 401
    resp.request = MagicMock()
    return anthropic.AuthenticationError("bad key", response=resp, body={})


def _mock_client(create_return=None, create_side_effect=None):
    """Build a mock Anthropic client whose messages.create is pre-wired."""
    client = MagicMock()
    if create_side_effect is not None:
        client.messages.create.side_effect = create_side_effect
    elif create_return is not None:
        client.messages.create.return_value = create_return
    return client


def test_successful_response_returns_text():
    mock = _mock_client(create_return=_mock_success("test reply"))
    with patch("cora.claude_client._get_client", return_value=mock):
        result = cl.generate_response("sys", "ctx", "hello")
    assert result == "test reply"


def test_persistent_failure_raises_ClaudeClientError():
    mock = _mock_client(create_side_effect=_conn_error())
    with patch("cora.claude_client._get_client", return_value=mock), \
         patch("cora.claude_client.time.sleep"):
        with pytest.raises(cl.ClaudeClientError):
            cl.generate_response("sys", "ctx", "hello")


def test_retries_on_transient_error():
    mock = _mock_client(create_side_effect=_conn_error())
    with patch("cora.claude_client._get_client", return_value=mock), \
         patch("cora.claude_client.time.sleep"):
        with pytest.raises(cl.ClaudeClientError):
            cl.generate_response("sys", "ctx", "hello")

    assert mock.messages.create.call_count == 3


def test_no_retry_on_auth_error():
    mock = _mock_client(create_side_effect=_auth_error())
    with patch("cora.claude_client._get_client", return_value=mock), \
         patch("cora.claude_client.time.sleep") as mock_sleep:
        with pytest.raises(cl.ClaudeClientError):
            cl.generate_response("sys", "ctx", "hello")

    assert mock.messages.create.call_count == 1
    assert mock_sleep.call_count == 0


# ── Shopify write-tool narration net (2026-07-10 HIGH-2) ─────────────────────

def _tool_use_response(tool_name, tool_id="tid1", tool_input=None):
    resp = MagicMock()
    resp.stop_reason = "tool_use"
    block = MagicMock()
    block.type = "tool_use"
    block.name = tool_name
    block.id = tool_id
    block.input = tool_input or {}
    resp.content = [block]
    return resp


class TestShopifyNarrationNet:
    def test_directed_text_extracts_confirmed_payload(self):
        raw = "WRITE_CONFIRMED -- post this:\n\nDTC inventory updated -- Pure: 202 -> 203 units."
        assert cl._shopify_directed_text(raw) == "DTC inventory updated -- Pure: 202 -> 203 units."

    def test_directed_text_extracts_blocked_payload(self):
        raw = "WRITE_BLOCKED -- show verbatim:\n\n⚠️ NOT WRITTEN -- no change.\nPure: 202 -> 203. Reply confirm."
        out = cl._shopify_directed_text(raw)
        assert out.startswith("⚠️ NOT WRITTEN")
        assert "WRITE_BLOCKED" not in out

    def test_directed_text_passthrough_without_sentinel(self):
        assert cl._shopify_directed_text("just text") == "just text"
        assert cl._shopify_directed_text("") == ""

    def test_last_shopify_result_matched_by_name(self):
        b1 = MagicMock(); b1.name = "asana_get_my_tasks"
        b2 = MagicMock(); b2.name = "f3e_shopify_set_inventory"
        results = [
            {"tool_use_id": "1", "content": "tasks..."},
            {"tool_use_id": "2", "content": "WRITE_BLOCKED -- s\n\nNOT WRITTEN"},
        ]
        assert cl._last_shopify_write_result([b1, b2], results) == "WRITE_BLOCKED -- s\n\nNOT WRITTEN"

    def test_last_shopify_result_empty_when_tool_absent(self):
        b1 = MagicMock(); b1.name = "asana_get_my_tasks"
        assert cl._last_shopify_write_result([b1], [{"tool_use_id": "1", "content": "x"}]) == ""

    def test_generate_response_overrides_false_success_narration(self):
        """The core HIGH-2 guarantee: if the write tool's last result is a non-write
        (WRITE_BLOCKED), the model's success-claim narration is REPLACED by the
        tool's NOT-WRITTEN text."""
        blocked = ("WRITE_BLOCKED -- show the user verbatim:\n\n"
                   "⚠️ NOT WRITTEN -- no inventory change was made.\n"
                   "Pure at the office: 202 -> 203 units. Reply \"confirm\" and I'll set it.")
        tu = _tool_use_response("f3e_shopify_set_inventory", tool_input={"confirmed": True})
        done = _mock_success("Done — 203 units set at the office.")  # FALSE narration
        with patch.object(cl, "_log_usage"), \
             patch.object(cl, "_create_with_retry", side_effect=[tu, done]), \
             patch.object(cl, "_dispatch_tools_parallel",
                          return_value=[{"type": "tool_result", "tool_use_id": "tid1", "content": blocked}]):
            out = cl.generate_response("sys", "ctx", "set pure to 203")
        assert "203 units set" not in out
        assert out.startswith("⚠️ NOT WRITTEN")

    def test_generate_response_posts_confirmed_line(self):
        confirmed = ("WRITE_CONFIRMED -- post the line after the blank:\n\n"
                     "DTC inventory updated -- Pure at the office: 202 -> 203 units.")
        tu = _tool_use_response("f3e_shopify_set_inventory", tool_input={"confirmed": True})
        done = _mock_success("ok")
        with patch.object(cl, "_log_usage"), \
             patch.object(cl, "_create_with_retry", side_effect=[tu, done]), \
             patch.object(cl, "_dispatch_tools_parallel",
                          return_value=[{"type": "tool_result", "tool_use_id": "tid1", "content": confirmed}]):
            out = cl.generate_response("sys", "ctx", "confirm")
        assert out == "DTC inventory updated -- Pure at the office: 202 -> 203 units."
