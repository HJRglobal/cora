"""Unit tests for claude_client.generate_response()."""

from unittest.mock import MagicMock, patch

import anthropic
import pytest

import cora.claude_client as cl


def _mock_success(text="Hello from Claude"):
    response = MagicMock()
    response.content = [MagicMock(text=text)]
    return response


def _conn_error():
    return anthropic.APIConnectionError(request=MagicMock())


def _auth_error():
    resp = MagicMock()
    resp.status_code = 401
    resp.request = MagicMock()
    return anthropic.AuthenticationError("bad key", response=resp, body={})


def test_successful_response_returns_text():
    with patch.object(cl._client.messages, "create", return_value=_mock_success("test reply")):
        result = cl.generate_response("sys", "ctx", "hello")
    assert result == "test reply"


def test_persistent_failure_raises_ClaudeClientError():
    with patch.object(cl._client.messages, "create", side_effect=_conn_error()), \
         patch("cora.claude_client.time.sleep"):
        with pytest.raises(cl.ClaudeClientError):
            cl.generate_response("sys", "ctx", "hello")


def test_retries_on_transient_error():
    with patch.object(cl._client.messages, "create", side_effect=_conn_error()) as mock_create, \
         patch("cora.claude_client.time.sleep"):
        with pytest.raises(cl.ClaudeClientError):
            cl.generate_response("sys", "ctx", "hello")

    assert mock_create.call_count == 3


def test_no_retry_on_auth_error():
    with patch.object(cl._client.messages, "create", side_effect=_auth_error()) as mock_create, \
         patch("cora.claude_client.time.sleep") as mock_sleep:
        with pytest.raises(cl.ClaudeClientError):
            cl.generate_response("sys", "ctx", "hello")

    assert mock_create.call_count == 1
    assert mock_sleep.call_count == 0
