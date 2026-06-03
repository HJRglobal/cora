"""Tests for Feature #8: React-to-Task (clipboard emoji -> Asana task + DM)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch, call
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client(
    history_messages=None,
    history_ok=True,
    dm_channel_id="DM123",
):
    """Return a mock Slack client configured for common happy-path behaviour."""
    client = MagicMock()

    msgs = history_messages if history_messages is not None else [{"text": "Ship the feature"}]
    if history_ok:
        client.conversations_history.return_value = {"messages": msgs}
    else:
        client.conversations_history.side_effect = Exception("Slack error")

    client.conversations_open.return_value = {"channel": {"id": dm_channel_id}}
    client.chat_postMessage.return_value = {"ok": True}
    return client


def _make_task(url="https://app.asana.com/0/1/task123", gid="task123"):
    return {"permalink_url": url, "gid": gid, "name": "Ship the feature"}


# ---------------------------------------------------------------------------
# Import the function under test
# ---------------------------------------------------------------------------

def _import_handler():
    from src.cora.app import _handle_react_to_task
    return _handle_react_to_task


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------

class TestReactToTaskHappyPath:
    def test_creates_task_for_known_reactor(self):
        client = _make_client()
        with patch("src.cora.app._handle_react_to_task.__wrapped__" if hasattr(_import_handler(), "__wrapped__") else "cora.tools.asana_client.create_task", return_value=_make_task()) as mock_create:
            # Direct invocation using the module-level patch
            pass

    def test_basic_happy_path(self):
        """Reactor with Asana mapping creates task and gets DM'd."""
        handler = _import_handler()
        client = _make_client()

        with patch("cora.tools.asana_client.create_task", return_value=_make_task()) as mock_create:
            handler(
                client=client,
                reactor="U0B2RM2JYJ1",  # Harrison
                channel_id="C0B4KRQT3LY",
                channel_name="f3e-leadership",
                message_ts="1234567890.123456",
            )

        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args.kwargs
        assert "Ship the feature" in call_kwargs["name"]
        client.conversations_open.assert_called_once_with(users=["U0B2RM2JYJ1"])
        client.chat_postMessage.assert_called_once()
        dm_text = client.chat_postMessage.call_args.kwargs["text"]
        assert ":clipboard:" in dm_text
        assert "https://app.asana.com" in dm_text

    def test_task_name_from_message_text(self):
        """Task name is derived from the message text."""
        handler = _import_handler()
        client = _make_client(history_messages=[{"text": "Call Dennis about Pure production"}])

        with patch("cora.tools.asana_client.create_task", return_value=_make_task()) as mock_create:
            handler(
                client=client,
                reactor="U0B3RU5Q55G",  # Tommy
                channel_id="C0B4KRQT3LY",
                channel_name="f3e-leadership",
                message_ts="1234567890.000001",
            )

        assert mock_create.call_args.kwargs["name"] == "Call Dennis about Pure production"

    def test_task_notes_contain_channel_name(self):
        """Task notes mention the originating channel."""
        handler = _import_handler()
        client = _make_client()

        with patch("cora.tools.asana_client.create_task", return_value=_make_task()) as mock_create:
            handler(
                client=client,
                reactor="U0B2RM2JYJ1",
                channel_id="C0B4KRQT3LY",
                channel_name="f3e-leadership",
                message_ts="1234567890.000002",
            )

        notes = mock_create.call_args.kwargs.get("notes", "")
        assert "f3e-leadership" in notes
        assert "clipboard" in notes

    def test_dm_contains_task_url(self):
        """DM text contains the Asana permalink."""
        handler = _import_handler()
        client = _make_client()

        with patch("cora.tools.asana_client.create_task", return_value=_make_task(url="https://app.asana.com/0/1/xyz999")):
            handler(
                client=client,
                reactor="U0B2RM2JYJ1",
                channel_id="C0B4KRQT3LY",
                channel_name="f3-sales",
                message_ts="1234567890.000003",
            )

        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "https://app.asana.com/0/1/xyz999" in text

    def test_assignee_gid_set_for_known_user(self):
        """Assignee GID is passed for users in slack-to-asana.yaml."""
        handler = _import_handler()
        client = _make_client()

        # Harrison's GID from yaml is 1204525779609669
        with patch("cora.tools.asana_client.create_task", return_value=_make_task()) as mock_create:
            handler(
                client=client,
                reactor="U0B2RM2JYJ1",
                channel_id="C0B4KRQT3LY",
                channel_name="f3e-leadership",
                message_ts="1234567890.000004",
            )

        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs.get("assignee_gid") is not None


# ---------------------------------------------------------------------------
# Edge-case tests
# ---------------------------------------------------------------------------

class TestReactToTaskEdgeCases:
    def test_empty_message_skipped(self):
        """Empty message text does not call create_task."""
        handler = _import_handler()
        client = _make_client(history_messages=[{"text": ""}])

        with patch("cora.tools.asana_client.create_task") as mock_create:
            handler(
                client=client,
                reactor="U0B2RM2JYJ1",
                channel_id="C0B4KRQT3LY",
                channel_name="f3e-leadership",
                message_ts="1234567890.000005",
            )

        mock_create.assert_not_called()
        client.chat_postMessage.assert_not_called()

    def test_whitespace_only_message_skipped(self):
        """Message with only whitespace does not create a task."""
        handler = _import_handler()
        client = _make_client(history_messages=[{"text": "   \n   "}])

        with patch("cora.tools.asana_client.create_task") as mock_create:
            handler(
                client=client,
                reactor="U0B2RM2JYJ1",
                channel_id="C0B4KRQT3LY",
                channel_name="f3e-leadership",
                message_ts="1234567890.000006",
            )

        mock_create.assert_not_called()

    def test_no_messages_returned_skipped(self):
        """Empty messages list does not create a task."""
        handler = _import_handler()
        client = _make_client(history_messages=[])

        with patch("cora.tools.asana_client.create_task") as mock_create:
            handler(
                client=client,
                reactor="U0B2RM2JYJ1",
                channel_id="C0B4KRQT3LY",
                channel_name="f3e-leadership",
                message_ts="1234567890.000007",
            )

        mock_create.assert_not_called()

    def test_missing_messages_key_skipped(self):
        """Missing 'messages' key in history response is handled."""
        handler = _import_handler()
        client = MagicMock()
        client.conversations_history.return_value = {}  # no 'messages' key

        with patch("cora.tools.asana_client.create_task") as mock_create:
            handler(
                client=client,
                reactor="U0B2RM2JYJ1",
                channel_id="C0B4KRQT3LY",
                channel_name="f3e-leadership",
                message_ts="1234567890.000008",
            )

        mock_create.assert_not_called()

    def test_unknown_reactor_still_creates_task(self):
        """Unknown Slack user creates task without an assignee_gid."""
        handler = _import_handler()
        client = _make_client()

        with patch("cora.tools.asana_client.create_task", return_value=_make_task()) as mock_create:
            handler(
                client=client,
                reactor="UUNKNOWN999",
                channel_id="C0B4KRQT3LY",
                channel_name="f3e-leadership",
                message_ts="1234567890.000009",
            )

        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args.kwargs
        # assignee_gid should be None for unknown user
        assert call_kwargs.get("assignee_gid") is None

    def test_unknown_reactor_dm_still_sent(self):
        """DM is still sent even when reactor is not in yaml."""
        handler = _import_handler()
        client = _make_client()

        with patch("cora.tools.asana_client.create_task", return_value=_make_task()):
            handler(
                client=client,
                reactor="UUNKNOWN999",
                channel_id="C0B4KRQT3LY",
                channel_name="f3e-leadership",
                message_ts="1234567890.000010",
            )

        client.conversations_open.assert_called_once_with(users=["UUNKNOWN999"])
        client.chat_postMessage.assert_called_once()

    def test_dm_failure_does_not_raise(self):
        """DM failure is caught and logged; no exception propagates."""
        handler = _import_handler()
        client = _make_client()
        client.conversations_open.side_effect = Exception("DM failed")

        with patch("cora.tools.asana_client.create_task", return_value=_make_task()):
            # Should not raise
            handler(
                client=client,
                reactor="U0B2RM2JYJ1",
                channel_id="C0B4KRQT3LY",
                channel_name="f3e-leadership",
                message_ts="1234567890.000011",
            )

    def test_asana_error_does_not_raise(self):
        """AsanaClientError is caught; function returns without raising."""
        from cora.tools.asana_client import AsanaClientError
        handler = _import_handler()
        client = _make_client()

        with patch("cora.tools.asana_client.create_task", side_effect=AsanaClientError("401")):
            # Should not raise
            handler(
                client=client,
                reactor="U0B2RM2JYJ1",
                channel_id="C0B4KRQT3LY",
                channel_name="f3e-leadership",
                message_ts="1234567890.000012",
            )

        client.chat_postMessage.assert_not_called()

    def test_history_api_failure_does_not_raise(self):
        """Slack API failure in conversations_history is caught silently."""
        handler = _import_handler()
        client = MagicMock()
        client.conversations_history.side_effect = Exception("Slack 500")

        with patch("cora.tools.asana_client.create_task") as mock_create:
            handler(
                client=client,
                reactor="U0B2RM2JYJ1",
                channel_id="C0B4KRQT3LY",
                channel_name="f3e-leadership",
                message_ts="1234567890.000013",
            )

        mock_create.assert_not_called()

    def test_task_name_truncated_at_250_chars(self):
        """Task name is truncated to 250 characters."""
        handler = _import_handler()
        long_text = "A" * 300
        client = _make_client(history_messages=[{"text": long_text}])

        with patch("cora.tools.asana_client.create_task", return_value=_make_task()) as mock_create:
            handler(
                client=client,
                reactor="U0B2RM2JYJ1",
                channel_id="C0B4KRQT3LY",
                channel_name="f3e-leadership",
                message_ts="1234567890.000014",
            )

        name = mock_create.call_args.kwargs["name"]
        assert len(name) == 250

    def test_task_name_short_text_not_truncated(self):
        """Short task name is not modified."""
        handler = _import_handler()
        client = _make_client(history_messages=[{"text": "Quick task"}])

        with patch("cora.tools.asana_client.create_task", return_value=_make_task()) as mock_create:
            handler(
                client=client,
                reactor="U0B2RM2JYJ1",
                channel_id="C0B4KRQT3LY",
                channel_name="f3e-leadership",
                message_ts="1234567890.000015",
            )

        name = mock_create.call_args.kwargs["name"]
        assert name == "Quick task"

    def test_no_task_url_dm_still_sent(self):
        """DM is sent even when Asana task has no permalink_url."""
        handler = _import_handler()
        client = _make_client()

        with patch("cora.tools.asana_client.create_task", return_value={"gid": "123"}):
            handler(
                client=client,
                reactor="U0B2RM2JYJ1",
                channel_id="C0B4KRQT3LY",
                channel_name="f3e-leadership",
                message_ts="1234567890.000016",
            )

        client.chat_postMessage.assert_called_once()
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert ":clipboard:" in text

    def test_generic_exception_does_not_raise(self):
        """Unexpected exception inside handler is caught and logged."""
        handler = _import_handler()
        client = MagicMock()
        client.conversations_history.return_value = {"messages": [{"text": "test"}]}
        client.conversations_open.return_value = {"channel": {"id": "DM1"}}

        with patch("cora.tools.asana_client.create_task", side_effect=RuntimeError("unexpected")):
            handler(
                client=client,
                reactor="U0B2RM2JYJ1",
                channel_id="C0B4KRQT3LY",
                channel_name="f3e-leadership",
                message_ts="1234567890.000017",
            )

    def test_first_name_used_in_dm(self):
        """DM greeting uses the reactor's first name when known."""
        handler = _import_handler()
        client = _make_client()

        with patch("cora.tools.asana_client.create_task", return_value=_make_task()):
            handler(
                client=client,
                reactor="U0B2RM2JYJ1",  # Harrison Rogers
                channel_id="C0B4KRQT3LY",
                channel_name="f3e-leadership",
                message_ts="1234567890.000018",
            )

        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "Harrison" in text

    def test_unknown_user_dm_says_you(self):
        """DM for unknown reactor says 'you' instead of a name."""
        handler = _import_handler()
        client = _make_client()

        with patch("cora.tools.asana_client.create_task", return_value=_make_task()):
            handler(
                client=client,
                reactor="UXXX_NOBODY",
                channel_id="C0B4KRQT3LY",
                channel_name="f3e-leadership",
                message_ts="1234567890.000019",
            )

        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "you" in text.lower()

    def test_conversations_open_uses_reactor_id(self):
        """DM is opened with exactly the reactor's Slack user ID."""
        handler = _import_handler()
        client = _make_client()

        with patch("cora.tools.asana_client.create_task", return_value=_make_task()):
            handler(
                client=client,
                reactor="U0B3RU5Q55G",  # Tommy
                channel_id="C0B4KRQT3LY",
                channel_name="f3e-leadership",
                message_ts="1234567890.000020",
            )

        client.conversations_open.assert_called_once_with(users=["U0B3RU5Q55G"])
