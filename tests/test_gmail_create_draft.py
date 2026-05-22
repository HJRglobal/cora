"""Unit tests for the gmail_create_draft tool — staged-write confirmation gate +
recipient resolution + MIME encoding behavior. Real Gmail API calls are stubbed.
"""

from unittest.mock import patch

import pytest

import cora.tools.gmail_client as gc
import cora.tools.tool_dispatch as td

HARRISON_SLACK = "U0B2RM2JYJ1"


# ---- Recipient normalization (pure logic, no API calls) ----


def test_normalize_recipients_accepts_string():
    assert gc._normalize_recipients("alice@example.com") == ["alice@example.com"]


def test_normalize_recipients_accepts_comma_separated_string():
    result = gc._normalize_recipients("alice@example.com, bob@example.com")
    assert result == ["alice@example.com", "bob@example.com"]


def test_normalize_recipients_accepts_list():
    result = gc._normalize_recipients(["alice@example.com", "bob@example.com"])
    assert result == ["alice@example.com", "bob@example.com"]


def test_normalize_recipients_handles_named_addresses():
    result = gc._normalize_recipients("Alice <alice@example.com>, Bob <bob@example.com>")
    assert result == ["alice@example.com", "bob@example.com"]


def test_normalize_recipients_empty():
    assert gc._normalize_recipients(None) == []
    assert gc._normalize_recipients("") == []
    assert gc._normalize_recipients([]) == []


def test_normalize_recipients_rejects_non_email():
    with pytest.raises(gc.GmailClientError, match="doesn't look like an email"):
        gc._normalize_recipients("notanemail")


# ---- MIME construction ----


def test_build_mime_message_includes_required_headers():
    import base64
    import email as email_lib

    raw = gc._build_mime_message(
        to=["alice@example.com"],
        subject="Hello",
        body="World",
        sender="harrison@hjrglobal.com",
    )
    mime_bytes = base64.urlsafe_b64decode(raw)
    msg = email_lib.message_from_bytes(mime_bytes)
    assert msg["To"] == "alice@example.com"
    assert msg["From"] == "harrison@hjrglobal.com"
    assert msg["Subject"] == "Hello"
    # get_payload(decode=True) handles any Content-Transfer-Encoding
    assert msg.get_payload(decode=True).decode("utf-8") == "World"


def test_build_mime_message_includes_cc_when_provided():
    raw = gc._build_mime_message(
        to=["alice@example.com"],
        subject="Hi",
        body="msg",
        cc=["cc1@example.com"],
        sender="me@example.com",
    )
    import base64
    decoded = base64.urlsafe_b64decode(raw).decode("utf-8")
    assert "Cc: cc1@example.com" in decoded


# ---- Tool dispatch — confirmation gate ----


def test_create_draft_refuses_without_confirmed():
    result = td._tool_gmail_create_draft(
        slack_user_id=HARRISON_SLACK,
        entity="FNDR",
        _input={
            "to": "alice@example.com",
            "subject": "Hi",
            "body": "Test",
        },
    )
    assert "refused" in result.lower()
    assert "confirmed" in result.lower()
    assert "preview" in result.lower()


def test_create_draft_refuses_with_confirmed_false():
    result = td._tool_gmail_create_draft(
        slack_user_id=HARRISON_SLACK,
        entity="FNDR",
        _input={
            "to": "alice@example.com",
            "subject": "Hi",
            "body": "Test",
            "confirmed": False,
        },
    )
    assert "refused" in result.lower()


def test_create_draft_refuses_missing_to():
    result = td._tool_gmail_create_draft(
        slack_user_id=HARRISON_SLACK,
        entity="FNDR",
        _input={
            "subject": "Hi",
            "body": "Test",
            "confirmed": True,
        },
    )
    assert "to" in result.lower()
    assert "required" in result.lower() or "missing" in result.lower()


def test_create_draft_refuses_missing_subject():
    result = td._tool_gmail_create_draft(
        slack_user_id=HARRISON_SLACK,
        entity="FNDR",
        _input={
            "to": "alice@example.com",
            "body": "Test",
            "confirmed": True,
        },
    )
    assert "subject" in result.lower()


def test_create_draft_refuses_missing_body():
    result = td._tool_gmail_create_draft(
        slack_user_id=HARRISON_SLACK,
        entity="FNDR",
        _input={
            "to": "alice@example.com",
            "subject": "Hi",
            "confirmed": True,
        },
    )
    assert "body" in result.lower()


def test_create_draft_happy_path_calls_gmail_client():
    fake_draft = {
        "id": "draft_abc123",
        "message": {"id": "msg_xyz789"},
    }
    with patch.object(gc, "create_draft", return_value=fake_draft) as mock:
        result = td._tool_gmail_create_draft(
            slack_user_id=HARRISON_SLACK,
            entity="FNDR",
            _input={
                "to": "shaun@lexingtonservices.com",
                "subject": "Quick question",
                "body": "Hey Shaun — quick context.\n\n— Harrison",
                "confirmed": True,
            },
        )

    mock.assert_called_once()
    call_kwargs = mock.call_args.kwargs
    # Sender resolved from Harrison's row in slack-to-asana.yaml
    assert call_kwargs["sender_email"] == "harrison@hjrglobal.com"
    assert call_kwargs["to"] == "shaun@lexingtonservices.com"
    assert call_kwargs["subject"] == "Quick question"
    assert "Shaun" in call_kwargs["body"]
    # Output for the LLM should surface the draft
    assert "CREATED" in result
    assert "draft_abc123" in result
    assert "Drafts" in result  # link to Gmail Drafts folder


def test_create_draft_unknown_asker_refuses_gracefully():
    with patch.object(gc, "create_draft") as mock:
        result = td._tool_gmail_create_draft(
            slack_user_id="U_NOT_IN_MAP",
            entity="FNDR",
            _input={
                "to": "alice@example.com",
                "subject": "Hi",
                "body": "Test",
                "confirmed": True,
            },
        )
    mock.assert_not_called()
    assert "not in the slack-to-asana" in result.lower() or "not in the slack-to-asana" in result.lower()


def test_create_draft_with_cc():
    fake_draft = {"id": "draft_cc_test"}
    with patch.object(gc, "create_draft", return_value=fake_draft) as mock:
        result = td._tool_gmail_create_draft(
            slack_user_id=HARRISON_SLACK,
            entity="FNDR",
            _input={
                "to": "alice@example.com",
                "cc": ["bob@example.com", "carol@example.com"],
                "subject": "CC test",
                "body": "body",
                "confirmed": True,
            },
        )
    call_kwargs = mock.call_args.kwargs
    assert call_kwargs["cc"] == ["bob@example.com", "carol@example.com"]
    # Output mentions cc
    assert "bob@example.com" in result.lower() or "carol@example.com" in result.lower()
