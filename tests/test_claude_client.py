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
