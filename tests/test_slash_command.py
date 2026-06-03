"""Tests for Feature #16 -- /cora-ask slash command handler in app.py."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))


# ---------------------------------------------------------------------------
# Import the handler under test (without starting the Bolt app)
# ---------------------------------------------------------------------------

# We test handle_cora_ask by importing app module and calling it directly.
# Avoid actually starting the Slack app -- patch the config.
import os
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test-signing-secret")

from cora.app import handle_cora_ask, _is_blocked_channel  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_body(text="What is the F3 Energy pipeline?", channel_id="C0B4KRQT3LY", user_id="U0B2RM2JYJ1"):
    return {
        "text": text,
        "channel_id": channel_id,
        "user_id": user_id,
        "channel_name": "f3e-leadership",
    }


def _make_client():
    client = MagicMock()
    client.conversations_info.return_value = {
        "channel": {"name": "f3e-leadership"}
    }
    client.auth_test.return_value = {"user_id": "U_CORA_BOT"}
    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_ack_always_called():
    """ack() must be called immediately (required by Slack)."""
    ack = MagicMock()
    body = _make_body()
    client = _make_client()

    with patch("cora.app._dispatch_qa"), \
         patch("cora.app._is_blocked_channel", return_value=False), \
         patch("cora.app.rate_limiter.check", return_value=(True, None)), \
         patch("cora.app._resolve_channel_name", return_value="f3e-leadership"), \
         patch("cora.app._resolve_bot_user_id", return_value="U_CORA"), \
         patch("cora.app.route", return_value="F3E"):
        handle_cora_ask(ack=ack, body=body, client=client)

    ack.assert_called_once()


def test_empty_text_returns_ephemeral():
    """Empty text should send ephemeral usage hint and not call _dispatch_qa."""
    ack = MagicMock()
    body = _make_body(text="  ")
    client = _make_client()

    with patch("cora.app._dispatch_qa") as mock_dispatch:
        handle_cora_ask(ack=ack, body=body, client=client)

    mock_dispatch.assert_not_called()
    client.chat_postEphemeral.assert_called_once()
    call_kwargs = client.chat_postEphemeral.call_args.kwargs
    assert "Usage" in call_kwargs["text"]


def test_empty_text_ack_still_called():
    ack = MagicMock()
    body = _make_body(text="")
    client = _make_client()

    with patch("cora.app._dispatch_qa"):
        handle_cora_ask(ack=ack, body=body, client=client)

    ack.assert_called_once()


def test_blocked_channel_returns_early():
    """Blocked channel should not call _dispatch_qa or post anything."""
    ack = MagicMock()
    body = _make_body(channel_id="C0B2NMLK7CK")  # blocked channel
    client = _make_client()

    with patch("cora.app._dispatch_qa") as mock_dispatch, \
         patch("cora.app._resolve_channel_name", return_value="general-do-not-use"):
        handle_cora_ask(ack=ack, body=body, client=client)

    mock_dispatch.assert_not_called()
    client.chat_postMessage.assert_not_called()


def test_rate_limited_returns_ephemeral():
    """Rate-limited user should get ephemeral and _dispatch_qa should not be called."""
    ack = MagicMock()
    body = _make_body()
    client = _make_client()

    with patch("cora.app._dispatch_qa") as mock_dispatch, \
         patch("cora.app._is_blocked_channel", return_value=False), \
         patch("cora.app.rate_limiter.check", return_value=(False, "user")), \
         patch("cora.app._resolve_channel_name", return_value="f3e-leadership"), \
         patch("cora.app._resolve_bot_user_id", return_value="U_CORA"):
        handle_cora_ask(ack=ack, body=body, client=client)

    mock_dispatch.assert_not_called()
    client.chat_postEphemeral.assert_called_once()


def test_valid_question_calls_dispatch_qa():
    """Valid question should call _dispatch_qa with correct args."""
    ack = MagicMock()
    body = _make_body(text="What is the F3 Energy pipeline?")
    client = _make_client()

    with patch("cora.app._dispatch_qa") as mock_dispatch, \
         patch("cora.app._is_blocked_channel", return_value=False), \
         patch("cora.app.rate_limiter.check", return_value=(True, None)), \
         patch("cora.app._resolve_channel_name", return_value="f3e-leadership"), \
         patch("cora.app._resolve_bot_user_id", return_value="U_CORA"), \
         patch("cora.app.route", return_value="F3E"):
        handle_cora_ask(ack=ack, body=body, client=client)

    mock_dispatch.assert_called_once()
    call_kwargs = mock_dispatch.call_args.kwargs
    assert call_kwargs["user_message"] == "What is the F3 Energy pipeline?"
    assert call_kwargs["entity"] == "F3E"
    assert call_kwargs["channel_id"] == "C0B4KRQT3LY"
    assert call_kwargs["user_id"] == "U0B2RM2JYJ1"


def test_dispatch_qa_called_with_none_thread_ts():
    """Slash commands post to channel with no thread_ts."""
    ack = MagicMock()
    body = _make_body(text="Show me the OSN weekly metrics")
    client = _make_client()

    with patch("cora.app._dispatch_qa") as mock_dispatch, \
         patch("cora.app._is_blocked_channel", return_value=False), \
         patch("cora.app.rate_limiter.check", return_value=(True, None)), \
         patch("cora.app._resolve_channel_name", return_value="osn-leadership"), \
         patch("cora.app._resolve_bot_user_id", return_value="U_CORA"), \
         patch("cora.app.route", return_value="OSN"):
        handle_cora_ask(ack=ack, body=body, client=client)

    call_kwargs = mock_dispatch.call_args.kwargs
    assert call_kwargs["reply_thread_ts"] is None
    assert call_kwargs["root_thread_ts"] is None


def test_say_callable_strips_thread_ts():
    """The _say function passed to _dispatch_qa must strip thread_ts."""
    ack = MagicMock()
    body = _make_body(text="What is the latest?")
    client = _make_client()

    captured_say = [None]

    def capture_dispatch(**kwargs):
        captured_say[0] = kwargs["say"]

    with patch("cora.app._dispatch_qa", side_effect=capture_dispatch), \
         patch("cora.app._is_blocked_channel", return_value=False), \
         patch("cora.app.rate_limiter.check", return_value=(True, None)), \
         patch("cora.app._resolve_channel_name", return_value="f3e-leadership"), \
         patch("cora.app._resolve_bot_user_id", return_value="U_CORA"), \
         patch("cora.app.route", return_value="F3E"):
        handle_cora_ask(ack=ack, body=body, client=client)

    # Call the captured say function with thread_ts
    assert captured_say[0] is not None
    captured_say[0](text="test response", thread_ts="12345.000")

    # Verify chat_postMessage was called WITHOUT thread_ts
    client.chat_postMessage.assert_called_once()
    call_kwargs = client.chat_postMessage.call_args.kwargs
    assert "thread_ts" not in call_kwargs
    assert call_kwargs["text"] == "test response"


def test_is_blocked_channel_general():
    """The permanently blocked channel should return True."""
    assert _is_blocked_channel("C0B2NMLK7CK") is True


def test_is_blocked_channel_normal():
    """Normal channels should not be blocked."""
    assert _is_blocked_channel("C0B4KRQT3LY") is False


def test_dispatch_qa_prior_messages_empty():
    """Slash commands have no prior thread history."""
    ack = MagicMock()
    body = _make_body(text="What are the open deals?")
    client = _make_client()

    with patch("cora.app._dispatch_qa") as mock_dispatch, \
         patch("cora.app._is_blocked_channel", return_value=False), \
         patch("cora.app.rate_limiter.check", return_value=(True, None)), \
         patch("cora.app._resolve_channel_name", return_value="f3e-leadership"), \
         patch("cora.app._resolve_bot_user_id", return_value="U_CORA"), \
         patch("cora.app.route", return_value="F3E"):
        handle_cora_ask(ack=ack, body=body, client=client)

    call_kwargs = mock_dispatch.call_args.kwargs
    assert call_kwargs["prior_messages"] == []
