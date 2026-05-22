"""Tests for the Claude API error classifier in claude_client.user_facing_message().

Verifies each specific anthropic exception type/status maps to a specific user
message, and that unknown/missing underlying exceptions fall back gracefully.
"""

from unittest.mock import MagicMock

import anthropic
import httpx
import pytest

from cora.claude_client import ClaudeClientError, user_facing_message


def _api_status_error(status_code: int) -> anthropic.APIStatusError:
    """Build a minimal APIStatusError with the given status_code.

    APIStatusError requires `message`, `response`, and `body` — pass minimal stubs.
    """
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.headers = {}
    response.request = MagicMock(spec=httpx.Request)
    err = anthropic.APIStatusError(
        message=f"HTTP {status_code}",
        response=response,
        body=None,
    )
    # APIStatusError sets self.status_code from response, but mock may not — ensure it.
    err.status_code = status_code
    return err


def _wrap_as_client_error(underlying: Exception) -> ClaudeClientError:
    """Mirror the raise-from pattern claude_client uses internally."""
    try:
        raise underlying
    except Exception as exc:
        try:
            raise ClaudeClientError(f"Claude API error: {exc}") from exc
        except ClaudeClientError as wrapped:
            return wrapped


def test_529_overloaded_returns_retry_message():
    exc = _wrap_as_client_error(_api_status_error(529))
    msg = user_facing_message(exc)
    assert "529" in msg
    assert "overloaded" in msg.lower()
    assert "retry" in msg.lower() or "try" in msg.lower()


def test_401_returns_key_failure_message():
    exc = _wrap_as_client_error(_api_status_error(401))
    msg = user_facing_message(exc)
    assert "401" in msg
    assert "key" in msg.lower()
    assert "harrison" in msg.lower()


def test_403_returns_key_failure_message():
    exc = _wrap_as_client_error(_api_status_error(403))
    msg = user_facing_message(exc)
    assert "403" in msg
    assert "key" in msg.lower()


def test_429_returns_rate_limit_message():
    exc = _wrap_as_client_error(_api_status_error(429))
    msg = user_facing_message(exc)
    assert "429" in msg
    assert "rate limit" in msg.lower()
    assert "30" in msg  # "wait about 30 seconds"


def test_400_returns_bad_request_message():
    exc = _wrap_as_client_error(_api_status_error(400))
    msg = user_facing_message(exc)
    assert "400" in msg
    assert "bug" in msg.lower() or "didn't accept" in msg.lower()


def test_500_returns_upstream_message():
    exc = _wrap_as_client_error(_api_status_error(500))
    msg = user_facing_message(exc)
    assert "500" in msg
    assert "upstream" in msg.lower()


def test_502_returns_upstream_message():
    exc = _wrap_as_client_error(_api_status_error(502))
    msg = user_facing_message(exc)
    assert "502" in msg


def test_unknown_status_returns_generic_with_status():
    exc = _wrap_as_client_error(_api_status_error(418))  # "I'm a teapot"
    msg = user_facing_message(exc)
    assert "418" in msg


def test_timeout_returns_timeout_message():
    timeout_exc = anthropic.APITimeoutError(request=MagicMock(spec=httpx.Request))
    exc = _wrap_as_client_error(timeout_exc)
    msg = user_facing_message(exc)
    assert "too long" in msg.lower() or "timeout" in msg.lower()


def test_connection_error_returns_network_message():
    conn_exc = anthropic.APIConnectionError(request=MagicMock(spec=httpx.Request))
    exc = _wrap_as_client_error(conn_exc)
    msg = user_facing_message(exc)
    assert "network" in msg.lower() or "internet" in msg.lower()


def test_no_underlying_exception_falls_back_to_generic():
    """A ClaudeClientError raised without `from exc` (no __cause__) should
    return the generic fallback message."""
    exc = ClaudeClientError("synthetic — no cause attached")
    msg = user_facing_message(exc)
    # Generic fallback should look like the old message
    assert "trouble reaching Claude" in msg


def test_underlying_non_anthropic_exception_falls_back_to_generic():
    """If an unexpected non-anthropic exception got wrapped, fall back to generic."""
    exc = _wrap_as_client_error(ValueError("something weird"))
    msg = user_facing_message(exc)
    assert "trouble reaching Claude" in msg
