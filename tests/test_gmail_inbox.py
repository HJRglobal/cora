"""Unit tests for per-user Gmail inbox reading and email completion signals.

Covers:
  - gmail_reader.get_inbox_summary()
  - gmail_reader.get_sent_signals()
  - completion_detector.collect_email_signals()
  - tool_dispatch._tool_gmail_inbox via td.dispatch()

All Gmail API calls are mocked — no real network access.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

import cora.connectors.gmail_reader as gr
import cora.tools.completion_detector as cd
import cora.tools.tool_dispatch as td

# ── Constants ──────────────────────────────────────────────────────────────

HARRISON_SLACK = "U0B2RM2JYJ1"
HARRISON_EMAIL = "harrison@hjrglobal.com"

_NOW_TS = 1748000000  # fixed epoch for predictable date formatting

# A minimal fake Gmail message response (metadata format)
def _fake_msg(
    msg_id="msg001",
    thread_id="thread001",
    subject="Test Subject",
    from_addr="Sender Name <sender@example.com>",
    to_addr="harrison@hjrglobal.com",
    date_str="Fri, 23 May 2025 10:00:00 -0700",
    snippet="This is a short preview of the email body.",
    label_ids=None,
) -> dict:
    if label_ids is None:
        label_ids = ["INBOX", "UNREAD"]
    return {
        "id": msg_id,
        "threadId": thread_id,
        "snippet": snippet,
        "labelIds": label_ids,
        "payload": {
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From",    "value": from_addr},
                {"name": "To",      "value": to_addr},
                {"name": "Date",    "value": date_str},
            ],
        },
    }


def _fake_sent_msg(
    msg_id="sent001",
    subject="Re: Invoice follow-up",
    snippet="Hi, just wanted to confirm the invoice was sent.",
    date_str="Fri, 23 May 2025 09:00:00 -0700",
) -> dict:
    return {
        "id": msg_id,
        "threadId": "thread999",
        "snippet": snippet,
        "labelIds": ["SENT"],
        "payload": {
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "Date",    "value": date_str},
            ],
        },
    }


# ── get_inbox_summary ──────────────────────────────────────────────────────


def test_get_inbox_summary_returns_messages():
    svc = MagicMock()
    msg1 = _fake_msg(msg_id="m1", subject="Hello")
    msg2 = _fake_msg(msg_id="m2", subject="Invoice", label_ids=["INBOX", "STARRED"])

    svc.users().messages().list().execute.return_value = {
        "messages": [{"id": "m1"}, {"id": "m2"}]
    }
    svc.users().messages().get().execute.side_effect = [msg1, msg2]

    with patch.object(gr, "_build_service", return_value=svc):
        result = gr.get_inbox_summary(HARRISON_EMAIL)

    assert len(result) == 2
    assert result[0]["subject"] == "Hello"
    assert result[0]["from"] == "Sender Name <sender@example.com>"
    assert "UNREAD" in result[0]["labels"]
    assert result[1]["subject"] == "Invoice"
    assert "STARRED" in result[1]["labels"]


def test_get_inbox_summary_caps_at_20():
    """max_results should be capped at 20 regardless of input."""
    svc = MagicMock()
    svc.users().messages().list().execute.return_value = {"messages": []}

    with patch.object(gr, "_build_service", return_value=svc):
        gr.get_inbox_summary(HARRISON_EMAIL, max_results=100)

    list_call_kwargs = svc.users().messages().list.call_args
    assert list_call_kwargs.kwargs["maxResults"] == 20


def test_get_inbox_summary_empty_inbox():
    svc = MagicMock()
    svc.users().messages().list().execute.return_value = {"messages": []}

    with patch.object(gr, "_build_service", return_value=svc):
        result = gr.get_inbox_summary(HARRISON_EMAIL)

    assert result == []


def test_get_inbox_summary_403_raises():
    from googleapiclient.errors import HttpError

    svc = MagicMock()
    resp = MagicMock()
    resp.status = 403
    svc.users().messages().list().execute.side_effect = HttpError(resp=resp, content=b"forbidden")

    with patch.object(gr, "_build_service", return_value=svc):
        with pytest.raises(gr.GmailReaderError, match="403"):
            gr.get_inbox_summary(HARRISON_EMAIL)


def test_get_inbox_summary_date_parsing():
    svc = MagicMock()
    msg = _fake_msg(date_str="Fri, 23 May 2025 10:00:00 -0700")
    svc.users().messages().list().execute.return_value = {"messages": [{"id": "m1"}]}
    svc.users().messages().get().execute.return_value = msg

    with patch.object(gr, "_build_service", return_value=svc):
        result = gr.get_inbox_summary(HARRISON_EMAIL)

    assert len(result) == 1
    assert isinstance(result[0]["date_ts"], int)
    assert result[0]["date_ts"] > 0


# ── get_sent_signals ───────────────────────────────────────────────────────


def test_get_sent_signals_returns_sent_mail():
    svc = MagicMock()
    sent1 = _fake_sent_msg(msg_id="s1", subject="Invoice sent")
    sent2 = _fake_sent_msg(msg_id="s2", subject="Project completed")

    svc.users().messages().list().execute.return_value = {
        "messages": [{"id": "s1"}, {"id": "s2"}]
    }
    svc.users().messages().get().execute.side_effect = [sent1, sent2]

    with patch.object(gr, "_build_service", return_value=svc):
        result = gr.get_sent_signals(HARRISON_EMAIL, since_ts=_NOW_TS - 3600)

    assert len(result) == 2
    assert result[0]["subject"] == "Invoice sent"
    assert result[1]["subject"] == "Project completed"
    assert all("message_id" in r and "snippet" in r and "date_ts" in r for r in result)


def test_get_sent_signals_empty_when_no_sent():
    svc = MagicMock()
    svc.users().messages().list().execute.return_value = {}

    with patch.object(gr, "_build_service", return_value=svc):
        result = gr.get_sent_signals(HARRISON_EMAIL, since_ts=_NOW_TS)

    assert result == []


def test_get_sent_signals_fails_silently_on_error():
    from googleapiclient.errors import HttpError

    svc = MagicMock()
    resp = MagicMock()
    resp.status = 500
    svc.users().messages().list().execute.side_effect = HttpError(resp=resp, content=b"error")

    with patch.object(gr, "_build_service", return_value=svc):
        result = gr.get_sent_signals(HARRISON_EMAIL, since_ts=_NOW_TS)

    # Should return empty list without raising
    assert result == []


def test_get_sent_signals_uses_sent_query():
    svc = MagicMock()
    svc.users().messages().list().execute.return_value = {"messages": []}

    with patch.object(gr, "_build_service", return_value=svc):
        gr.get_sent_signals(HARRISON_EMAIL, since_ts=12345)

    list_call_kwargs = svc.users().messages().list.call_args
    query = list_call_kwargs.kwargs["q"]
    assert "in:sent" in query
    assert "12345" in query


# ── collect_email_signals ──────────────────────────────────────────────────


def test_collect_email_signals_returns_signals():
    fake_sent = [
        {"message_id": "s1", "subject": "Contract signed", "snippet": "We got the signature.", "date_ts": _NOW_TS},
        {"message_id": "s2", "subject": "Invoice paid",    "snippet": "Payment confirmed.",    "date_ts": _NOW_TS},
    ]
    with patch("cora.connectors.gmail_reader.get_sent_signals", return_value=fake_sent):
        signals = cd.collect_email_signals(HARRISON_EMAIL, lookback_seconds=3600)

    # Both subjects contain completion verbs → should produce signals
    assert len(signals) > 0
    assert all(s.source == "gmail" for s in signals)


def test_collect_email_signals_fails_silently_on_exception():
    with patch("cora.connectors.gmail_reader.get_sent_signals", side_effect=RuntimeError("network error")):
        signals = cd.collect_email_signals(HARRISON_EMAIL, lookback_seconds=3600)

    assert signals == []


def test_collect_email_signals_skips_empty_subjects():
    fake_sent = [
        {"message_id": "s1", "subject": "", "snippet": "", "date_ts": _NOW_TS},
    ]
    with patch("cora.connectors.gmail_reader.get_sent_signals", return_value=fake_sent):
        signals = cd.collect_email_signals(HARRISON_EMAIL, lookback_seconds=3600)

    # Empty text → no signals extracted
    assert signals == []


def test_collect_email_signals_source_weight_is_gmail():
    from cora.tools.completion_detector import _SOURCE_WEIGHTS
    # Verify gmail weight constant is defined and reasonable
    assert "gmail" in _SOURCE_WEIGHTS
    weight = _SOURCE_WEIGHTS["gmail"]
    assert 0.5 <= weight <= 1.0


# ── _tool_gmail_inbox via dispatch ─────────────────────────────────────────


def _patch_slack_map(monkeypatch):
    """Patch the user map to include Harrison with a real email."""
    monkeypatch.setattr(
        td,
        "_load_slack_asana_map",
        lambda: {
            HARRISON_SLACK: {
                "slack_user_id": HARRISON_SLACK,
                "display_name": "Harrison",
                "asana_email": HARRISON_EMAIL,
                "asana_user_gid": "111",
            }
        },
    )


def test_tool_gmail_inbox_returns_messages(monkeypatch):
    _patch_slack_map(monkeypatch)

    fake_messages = [
        {
            "message_id": "m1",
            "thread_id": "t1",
            "from": "Alex <alex@hjrglobal.com>",
            "to": HARRISON_EMAIL,
            "subject": "F3 campaign update",
            "date_ts": _NOW_TS,
            "snippet": "The campaign is live now.",
            "labels": ["INBOX", "UNREAD"],
        }
    ]

    with patch.object(gr, "get_inbox_summary", return_value=fake_messages):
        result = td.dispatch(
            "gmail_inbox",
            {},
            slack_user_id=HARRISON_SLACK,
            entity="FNDR",
        )

    assert "F3 campaign update" in result
    assert "🔵" in result  # UNREAD flag
    assert "The campaign is live" in result


def test_tool_gmail_inbox_starred_flag(monkeypatch):
    _patch_slack_map(monkeypatch)

    fake_messages = [
        {
            "message_id": "m2",
            "thread_id": "t2",
            "from": "Vendor <vendor@example.com>",
            "to": HARRISON_EMAIL,
            "subject": "Invoice #1042",
            "date_ts": _NOW_TS,
            "snippet": "Please review and approve.",
            "labels": ["INBOX", "STARRED"],
        }
    ]

    with patch.object(gr, "get_inbox_summary", return_value=fake_messages):
        result = td.dispatch("gmail_inbox", {}, slack_user_id=HARRISON_SLACK, entity="FNDR")

    assert "Invoice #1042" in result
    assert "⭐" in result  # STARRED flag


def test_tool_gmail_inbox_empty_result(monkeypatch):
    _patch_slack_map(monkeypatch)

    with patch.object(gr, "get_inbox_summary", return_value=[]):
        result = td.dispatch("gmail_inbox", {}, slack_user_id=HARRISON_SLACK, entity="FNDR")

    assert "No messages" in result


def test_tool_gmail_inbox_unmapped_user():
    result = td.dispatch(
        "gmail_inbox",
        {},
        slack_user_id="U_NOT_IN_MAP",
        entity="FNDR",
    )
    assert "not mapped" in result.lower() or "lookup failed" in result.lower()


def test_tool_gmail_inbox_no_email(monkeypatch):
    monkeypatch.setattr(
        td,
        "_load_slack_asana_map",
        lambda: {
            HARRISON_SLACK: {
                "slack_user_id": HARRISON_SLACK,
                "display_name": "Harrison",
                "asana_email": "",  # empty email
                "asana_user_gid": "111",
            }
        },
    )
    result = td.dispatch("gmail_inbox", {}, slack_user_id=HARRISON_SLACK, entity="FNDR")
    assert "no asana_email" in result.lower() or "lookup failed" in result.lower()


def test_tool_gmail_inbox_gmail_error(monkeypatch):
    _patch_slack_map(monkeypatch)

    with patch.object(gr, "get_inbox_summary", side_effect=gr.GmailReaderError("403 permission denied")):
        result = td.dispatch("gmail_inbox", {}, slack_user_id=HARRISON_SLACK, entity="FNDR")

    assert "Gmail error" in result or "403" in result


def test_tool_gmail_inbox_custom_query(monkeypatch):
    _patch_slack_map(monkeypatch)

    captured_query = {}

    def fake_inbox(user_email, query="is:unread OR is:starred", max_results=10):
        captured_query["q"] = query
        return []

    with patch.object(gr, "get_inbox_summary", side_effect=fake_inbox):
        td.dispatch(
            "gmail_inbox",
            {"query": "from:alex@hjrglobal.com"},
            slack_user_id=HARRISON_SLACK,
            entity="FNDR",
        )

    assert captured_query["q"] == "from:alex@hjrglobal.com"


def test_tool_gmail_inbox_registered_in_tool_functions():
    assert "gmail_inbox" in td._TOOL_FUNCTIONS


def test_tool_gmail_inbox_registered_in_tool_definitions():
    names = [t["name"] for t in td.TOOL_DEFINITIONS]
    assert "gmail_inbox" in names
