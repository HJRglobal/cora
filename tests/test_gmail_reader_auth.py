"""Auth-failure conversion in gmail_reader.

An alias-only address (no impersonatable mailbox, e.g.
harrison@unitedfightleague.com) fails DWD token refresh with
google.auth.exceptions.RefreshError ("invalid_grant: Invalid email or User
ID") — NOT an HttpError. Uncaught, that killed the entire 2026-06-10
18-month backfill (and the nightly sweep) at the first such account.

These tests pin the conversion: every sweep-facing gmail_reader entry point
must surface auth failures as GmailReaderError so per-account loops skip the
mailbox and continue.
"""

from unittest.mock import MagicMock

import pytest
from google.auth.exceptions import RefreshError

from cora.connectors import gmail_reader


def _failing_service():
    """A fake Gmail service whose every .execute() raises RefreshError."""
    service = MagicMock()
    for chain in (
        service.users.return_value.threads.return_value.list,
        service.users.return_value.threads.return_value.get,
        service.users.return_value.messages.return_value.list,
        service.users.return_value.messages.return_value.get,
    ):
        chain.return_value.execute.side_effect = RefreshError(
            "invalid_grant: Invalid email or User ID",
            {"error": "invalid_grant"},
        )
    return service


@pytest.fixture(autouse=True)
def patch_service(monkeypatch):
    monkeypatch.setattr(gmail_reader, "_build_service", lambda email: _failing_service())


def test_list_threads_since_converts_refresh_error():
    with pytest.raises(gmail_reader.GmailReaderError, match="auth failed"):
        gmail_reader.list_threads_since("alias@unitedfightleague.com", 0)


def test_get_full_thread_text_converts_refresh_error():
    with pytest.raises(gmail_reader.GmailReaderError, match="auth failed"):
        gmail_reader.get_full_thread_text("alias@unitedfightleague.com", "t1")


def test_list_messages_with_attachments_converts_refresh_error():
    with pytest.raises(gmail_reader.GmailReaderError, match="auth failed"):
        gmail_reader.list_messages_with_attachments("alias@unitedfightleague.com", 0)


def test_get_message_converts_refresh_error():
    with pytest.raises(gmail_reader.GmailReaderError, match="auth failed"):
        gmail_reader.get_message("alias@unitedfightleague.com", "m1")
