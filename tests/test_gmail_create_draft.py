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


# ---- Tool dispatch: STAGED WRITE (v2b S5) ----
#
# gmail_create_draft was an honor gate: it refused unless the model asserted
# confirmed=true, then drafted whatever THAT call carried. A model that dropped
# or altered a field on the second call silently drafted something other than
# what the user approved. It is now a real staged write -- the unconfirmed call
# validates and STASHES, and the confirmed call (or a button tap) executes the
# STASH.

CHAN = {"_channel_name": "hjrg-leadership"}


def _clear():
    td._CLASSB["gmail_draft"]["store"].clear()


def test_unconfirmed_call_previews_and_stashes_instead_of_refusing():
    _clear()
    with patch.object(gc, "create_draft") as mock:
        result = td._tool_gmail_create_draft(
            slack_user_id=HARRISON_SLACK, entity="FNDR",
            _input={**CHAN, "to": "alice@example.com", "subject": "Hi", "body": "Test"},
        )
    mock.assert_not_called()
    assert "WRITE_BLOCKED" in result
    assert "NOT DRAFTED yet" in result
    assert "alice@example.com" in result and "Hi" in result and "Test" in result
    pending = td._CLASSB["gmail_draft"]["peek"](HARRISON_SLACK, "hjrg-leadership")
    assert pending is not None and pending["stash_id"]
    _clear()


def test_the_preview_carries_an_honest_confirm_instruction():
    _clear()
    result = td._tool_gmail_create_draft(
        slack_user_id=HARRISON_SLACK, entity="FNDR",
        _input={**CHAN, "to": "a@b.com", "subject": "S", "body": "B"},
    )
    # The USER-facing half is everything after the contract preamble; the
    # preamble itself legitimately says "reply to confirm" because it is
    # MODEL-facing instruction about calling the tool again (S4 kept it).
    user_facing = result.split("\n\n", 1)[1]
    assert "@mention me with" in user_facing or "tap *Confirm* below" in user_facing
    assert "reply to confirm" not in user_facing.lower()
    _clear()


def test_confirm_executes_the_STASH_not_the_confirm_turn_args():
    """The whole point of the migration: a confirm turn that carries DIFFERENT
    values must not be able to retarget the draft."""
    _clear()
    td._tool_gmail_create_draft(
        slack_user_id=HARRISON_SLACK, entity="FNDR",
        _input={**CHAN, "to": "shaun@lexingtonservices.com",
                "subject": "Quick question", "body": "Hey Shaun."},
    )
    fake_draft = {"id": "draft_abc123", "message": {"id": "msg_xyz789"}}
    with patch.object(gc, "create_draft", return_value=fake_draft) as mock:
        result = td._tool_gmail_create_draft(
            slack_user_id=HARRISON_SLACK, entity="FNDR",
            _input={**CHAN, "confirmed": True,
                    "to": "attacker@evil.com", "subject": "CHANGED", "body": "CHANGED"},
        )
    kw = mock.call_args.kwargs
    assert kw["sender_email"] == "harrison@hjrglobal.com"
    assert kw["to"] == "shaun@lexingtonservices.com", "confirm-turn args retargeted the draft"
    assert kw["subject"] == "Quick question"
    assert "Shaun" in kw["body"]
    assert "draft_abc123" in result
    _clear()


def test_a_confirm_with_no_pending_is_honest_and_writes_nothing():
    _clear()
    with patch.object(gc, "create_draft") as mock:
        result = td._tool_gmail_create_draft(
            slack_user_id=HARRISON_SLACK, entity="FNDR",
            _input={**CHAN, "confirmed": True, "to": "a@b.com",
                    "subject": "S", "body": "B"},
        )
    mock.assert_not_called()
    assert "NOT DONE" in result and "expired" in result.lower()


def test_the_stash_is_consumed_exactly_once():
    _clear()
    td._tool_gmail_create_draft(
        slack_user_id=HARRISON_SLACK, entity="FNDR",
        _input={**CHAN, "to": "a@b.com", "subject": "S", "body": "B"},
    )
    with patch.object(gc, "create_draft", return_value={"id": "d1"}):
        first = td._tool_gmail_create_draft(
            slack_user_id=HARRISON_SLACK, entity="FNDR",
            _input={**CHAN, "confirmed": True})
    with patch.object(gc, "create_draft") as mock2:
        second = td._tool_gmail_create_draft(
            slack_user_id=HARRISON_SLACK, entity="FNDR",
            _input={**CHAN, "confirmed": True})
    assert "d1" in first
    mock2.assert_not_called()
    assert "NOT DONE" in second


def test_a_button_tap_executes_the_same_stash():
    _clear()
    td._tool_gmail_create_draft(
        slack_user_id=HARRISON_SLACK, entity="FNDR",
        _input={**CHAN, "to": "a@b.com", "subject": "S", "body": "B"},
    )
    sid = td._CLASSB["gmail_draft"]["peek"](HARRISON_SLACK, "hjrg-leadership")["stash_id"]
    with patch.object(gc, "create_draft", return_value={"id": "tapped"}) as mock,          patch("cora.entity_router.route", return_value="FNDR"):
        result = td.resolve_and_claim_stash(sid, HARRISON_SLACK, "confirm")
    mock.assert_called_once()
    assert result["outcome"] == "executed"
    assert "tapped" in result["message"]


def test_a_non_requester_tap_is_refused_and_consumes_nothing():
    _clear()
    td._tool_gmail_create_draft(
        slack_user_id=HARRISON_SLACK, entity="FNDR",
        _input={**CHAN, "to": "a@b.com", "subject": "S", "body": "B"},
    )
    sid = td._CLASSB["gmail_draft"]["peek"](HARRISON_SLACK, "hjrg-leadership")["stash_id"]
    with patch.object(gc, "create_draft") as mock:
        result = td.resolve_and_claim_stash(sid, "U0SOMEONE_ELSE", "confirm")
    mock.assert_not_called()
    assert result["outcome"] == "unauthorized"
    assert td.stash_is_live(sid)
    _clear()


def test_missing_fields_still_refuse_at_PREVIEW_time_and_stash_nothing():
    """Validation moved ahead of the stash: a bad request never becomes a
    confirmable pending."""
    _clear()
    for bad, word in (
        ({"subject": "Hi", "body": "T"}, "to"),
        ({"to": "a@b.com", "body": "T"}, "subject"),
        ({"to": "a@b.com", "subject": "Hi"}, "body"),
    ):
        result = td._tool_gmail_create_draft(
            slack_user_id=HARRISON_SLACK, entity="FNDR", _input={**CHAN, **bad})
        assert word in result.lower()
        assert td._CLASSB["gmail_draft"]["peek"](HARRISON_SLACK, "hjrg-leadership") is None


def test_unknown_asker_refuses_at_preview_and_stashes_nothing():
    _clear()
    with patch.object(gc, "create_draft") as mock:
        result = td._tool_gmail_create_draft(
            slack_user_id="U_NOT_IN_MAP", entity="FNDR",
            _input={**CHAN, "to": "alice@example.com", "subject": "Hi", "body": "Test"},
        )
    mock.assert_not_called()
    assert "not in the slack-to-asana" in result.lower()
    assert td._CLASSB["gmail_draft"]["peek"]("U_NOT_IN_MAP", "hjrg-leadership") is None


def test_cc_survives_the_stash_round_trip():
    _clear()
    td._tool_gmail_create_draft(
        slack_user_id=HARRISON_SLACK, entity="FNDR",
        _input={**CHAN, "to": "alice@example.com",
                "cc": ["bob@example.com", "carol@example.com"],
                "subject": "CC test", "body": "body"},
    )
    with patch.object(gc, "create_draft", return_value={"id": "draft_cc_test"}) as mock:
        result = td._tool_gmail_create_draft(
            slack_user_id=HARRISON_SLACK, entity="FNDR",
            _input={**CHAN, "confirmed": True})
    assert mock.call_args.kwargs["cc"] == ["bob@example.com", "carol@example.com"]
    assert "bob@example.com" in result.lower() or "carol@example.com" in result.lower()
    _clear()
