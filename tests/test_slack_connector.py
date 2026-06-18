"""Tests for src/cora/connectors/slack_connector.py — Component 1.

Layer A: string/logic assertions (no imports needed beyond stdlib).
Layer B: import-guarded unit tests with mocked Slack SDK.

Coverage:
  - _resolve_entity(): channel routing against yaml routes
  - serialize_message(): message text serialization
  - _chunk_thread(): chunking logic (size limits, channel prefix)
  - list_joined_channels(): pagination + member filter
  - get_channel_history(): oldest_ts filtering + pagination
  - get_thread_replies(): parent exclusion
  - SlackConnectorError raised on missing scope / bad token
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── Layer A: pure logic (no cora imports needed) ──────────────────────────────

class TestResolveEntity:
    """Layer A — entity routing from channel names."""

    def _load(self):
        try:
            from scripts.incremental_sync_slack import _resolve_entity, _load_routing
        except ImportError:
            try:
                import sys
                sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
                from incremental_sync_slack import _resolve_entity, _load_routing
            except ImportError:
                pytest.skip("incremental_sync_slack not importable")
        return _resolve_entity

    def _routes(self):
        return [
            {"pattern": "f3e",        "entity": "F3E"},
            {"pattern": "f3e-*",      "entity": "F3E"},
            {"pattern": "f3-*",       "entity": "F3E"},
            {"pattern": "llc",        "entity": "LEX-LLC"},
            {"pattern": "llc-*",      "entity": "LEX-LLC"},
            {"pattern": "lex-*",      "entity": "LEX"},
            {"pattern": "osn",        "entity": "OSN"},
            {"pattern": "osn-*",      "entity": "OSN"},
            {"pattern": "hjrg-*",     "entity": "FNDR"},
            {"pattern": "fndr*",      "entity": "FNDR"},
            {"pattern": "*",          "entity": "FNDR"},
        ]

    def test_f3e_bare(self):
        fn = self._load()
        assert fn("f3e", self._routes()) == "F3E"

    def test_f3e_dash_prefix(self):
        fn = self._load()
        assert fn("f3e-leadership", self._routes()) == "F3E"

    def test_f3_pure_matches_f3_star(self):
        fn = self._load()
        assert fn("f3-pure", self._routes()) == "F3E"

    def test_llc_bare(self):
        fn = self._load()
        assert fn("llc", self._routes()) == "LEX-LLC"

    def test_llc_prefix(self):
        fn = self._load()
        assert fn("llc-leadership", self._routes()) == "LEX-LLC"

    def test_lex_prefix(self):
        fn = self._load()
        assert fn("lex-hcbs", self._routes()) == "LEX"

    def test_osn_bare(self):
        fn = self._load()
        assert fn("osn", self._routes()) == "OSN"

    def test_hjrg_routes_to_fndr(self):
        fn = self._load()
        assert fn("hjrg-leadership", self._routes()) == "FNDR"

    def test_unknown_channel_defaults_fndr(self):
        fn = self._load()
        assert fn("random-channel", self._routes()) == "FNDR"

    def test_direct_message_defaults_fndr(self):
        fn = self._load()
        # DM channels have IDs like D0ABC — name is user_id or 'directmessage'
        assert fn("directmessage", self._routes()) == "FNDR"


class TestSerializeMessage:
    """Layer A — message serialization to text."""

    def _load(self):
        try:
            from src.cora.connectors.slack_connector import serialize_message
            return serialize_message
        except ImportError:
            pytest.skip("slack_connector not importable")

    def test_basic_message(self):
        fn = self._load()
        msg = {"ts": "1717000000.000000", "user": "U0ABC", "text": "Hello team"}
        result = fn(msg)
        assert "Hello team" in result
        assert "U0ABC" in result

    def test_bot_message_uses_bot_id(self):
        fn = self._load()
        msg = {"ts": "1717000001.000000", "bot_id": "B0BOT", "text": "Cora response"}
        result = fn(msg)
        assert "B0BOT" in result or "Cora response" in result

    def test_includes_timestamp(self):
        fn = self._load()
        msg = {"ts": "1717000000.000000", "user": "U0ABC", "text": "test"}
        result = fn(msg)
        assert "2024" in result or "UTC" in result

    def test_file_attachment_mentioned(self):
        fn = self._load()
        msg = {
            "ts": "1717000002.000000",
            "user": "U0ABC",
            "text": "See attached",
            "files": [{"name": "report.pdf"}],
        }
        result = fn(msg)
        assert "report.pdf" in result

    def test_empty_text_no_crash(self):
        fn = self._load()
        msg = {"ts": "1717000003.000000", "user": "U0ABC", "text": ""}
        result = fn(msg)
        assert isinstance(result, str)


class TestChunkThread:
    """Layer A — thread chunking."""

    def _load(self):
        try:
            from scripts.incremental_sync_slack import _chunk_thread
        except ImportError:
            try:
                import sys
                sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
                from incremental_sync_slack import _chunk_thread
            except ImportError:
                pytest.skip("incremental_sync_slack not importable")
        return _chunk_thread

    def _msg(self, ts, user, text):
        return {"ts": str(ts), "user": user, "text": text}

    def test_single_message_one_chunk(self):
        fn = self._load()
        parent = self._msg("1717000000.000000", "U0A", "Hello")
        chunks = fn(parent, [], "f3e-leadership")
        assert len(chunks) == 1

    def test_chunk_contains_channel_name(self):
        fn = self._load()
        parent = self._msg("1717000000.000000", "U0A", "Hello")
        chunks = fn(parent, [], "f3e-leadership")
        assert "#f3e-leadership" in chunks[0]

    def test_long_content_splits_into_multiple_chunks(self):
        fn = self._load()
        # Create a very long message that exceeds MAX_CHUNK_CHARS
        parent = self._msg("1717000000.000000", "U0A", "x" * 1500)
        replies = [self._msg(f"171700000{i}.000001", f"U0{i}", "y" * 600) for i in range(1, 4)]
        chunks = fn(parent, replies, "test-channel")
        # Should split into multiple chunks
        assert len(chunks) >= 2

    def test_thread_replies_included(self):
        fn = self._load()
        parent = self._msg("1717000000.000000", "U0A", "Question here")
        replies = [
            self._msg("1717000001.000001", "U0B", "Reply from B"),
            self._msg("1717000002.000002", "U0C", "Reply from C"),
        ]
        chunks = fn(parent, replies, "osn-leadership")
        combined = " ".join(chunks)
        assert "Reply from B" in combined
        assert "Reply from C" in combined


# ── Layer B: import-guarded unit tests with mocks ─────────────────────────────

try:
    from src.cora.connectors import slack_connector as sc
    _IMPORT_OK = True
except Exception:
    _IMPORT_OK = False


@pytest.mark.skipif(not _IMPORT_OK, reason="slack_connector not importable")
class TestListJoinedChannels:
    """Layer B — list_joined_channels() with mocked Slack SDK."""

    def _mock_response(self, channels, next_cursor=""):
        resp = MagicMock()
        resp.get = lambda key, default=None: {
            "channels": channels,
            "response_metadata": {"next_cursor": next_cursor},
        }.get(key, default)
        return resp

    def test_returns_member_channels_only(self):
        channels = [
            {"id": "C0A", "name": "f3e-leadership", "is_member": True,
             "is_private": False, "is_im": False, "is_mpim": False},
            {"id": "C0B", "name": "not-a-member", "is_member": False,
             "is_private": False, "is_im": False, "is_mpim": False},
        ]
        mock_client = MagicMock()
        mock_client.conversations_list.return_value = self._mock_response(channels)
        with patch.object(sc, "_build_client", return_value=mock_client):
            result = sc.list_joined_channels()
        assert len(result) == 1
        assert result[0]["id"] == "C0A"

    def test_includes_created_and_creator(self):
        channels = [
            {"id": "C0A", "name": "f3e-leadership", "is_member": True,
             "is_private": False, "is_im": False, "is_mpim": False,
             "created": 1748000000, "creator": "U0B44MDGC5R"},
        ]
        mock_client = MagicMock()
        mock_client.conversations_list.return_value = self._mock_response(channels)
        with patch.object(sc, "_build_client", return_value=mock_client):
            result = sc.list_joined_channels()
        assert result[0]["created"] == 1748000000
        assert result[0]["creator"] == "U0B44MDGC5R"

    def test_created_creator_default_to_none_when_absent(self):
        channels = [
            {"id": "C0A", "name": "old-channel", "is_member": True,
             "is_private": False, "is_im": False, "is_mpim": False},
        ]
        mock_client = MagicMock()
        mock_client.conversations_list.return_value = self._mock_response(channels)
        with patch.object(sc, "_build_client", return_value=mock_client):
            result = sc.list_joined_channels()
        assert result[0]["created"] is None
        assert result[0]["creator"] is None

    def test_pagination_followed(self):
        page1 = [{"id": "C01", "name": "ch1", "is_member": True,
                  "is_private": False, "is_im": False, "is_mpim": False}]
        page2 = [{"id": "C02", "name": "ch2", "is_member": True,
                  "is_private": False, "is_im": False, "is_mpim": False}]
        resp1 = MagicMock()
        resp1.get = lambda k, d=None: {
            "channels": page1,
            "response_metadata": {"next_cursor": "cursor-abc"},
        }.get(k, d)
        resp2 = MagicMock()
        resp2.get = lambda k, d=None: {
            "channels": page2,
            "response_metadata": {"next_cursor": ""},
        }.get(k, d)
        mock_client = MagicMock()
        mock_client.conversations_list.side_effect = [resp1, resp2]
        with patch.object(sc, "_build_client", return_value=mock_client):
            result = sc.list_joined_channels()
        assert len(result) == 2

    def test_raises_on_api_error(self):
        mock_client = MagicMock()
        mock_client.conversations_list.side_effect = Exception("Slack API down")
        with patch.object(sc, "_build_client", return_value=mock_client):
            with pytest.raises(sc.SlackConnectorError):
                sc.list_joined_channels()

    def test_missing_token_raises(self):
        with patch.dict("os.environ", {"SLACK_BOT_TOKEN": ""}):
            with pytest.raises(sc.SlackConnectorError):
                sc._get_bot_token()


@pytest.mark.skipif(not _IMPORT_OK, reason="slack_connector not importable")
class TestGetChannelHistory:
    """Layer B — get_channel_history() with mocked Slack SDK."""

    def _mock_resp(self, messages, has_more=False, next_cursor=""):
        resp = MagicMock()
        resp.get = lambda k, d=None: {
            "messages": messages,
            "has_more": has_more,
            "response_metadata": {"next_cursor": next_cursor},
        }.get(k, d)
        return resp

    def test_returns_messages_since_ts(self):
        msgs = [
            {"ts": "1717001000.000001", "user": "U0A", "text": "msg1"},
            {"ts": "1717001001.000002", "user": "U0B", "text": "msg2"},
        ]
        mock_client = MagicMock()
        mock_client.conversations_history.return_value = self._mock_resp(msgs)
        result = sc.get_channel_history("C0TEST", oldest_ts=1717000000.0, client=mock_client)
        assert len(result) == 2

    def test_returns_chronological_order(self):
        # Slack returns newest-first; we reverse
        msgs = [
            {"ts": "1717001002.000003", "user": "U0A", "text": "newer"},
            {"ts": "1717001001.000002", "user": "U0A", "text": "older"},
        ]
        mock_client = MagicMock()
        mock_client.conversations_history.return_value = self._mock_resp(msgs)
        result = sc.get_channel_history("C0TEST", oldest_ts=1717000000.0, client=mock_client)
        # After reversal, older message should come first
        assert result[0]["text"] == "older"
        assert result[1]["text"] == "newer"

    def test_missing_scope_returns_empty(self):
        mock_client = MagicMock()
        mock_client.conversations_history.side_effect = Exception("missing_scope error")
        result = sc.get_channel_history("C0TEST", oldest_ts=0.0, client=mock_client)
        assert result == []

    def test_empty_channel_returns_empty_list(self):
        mock_client = MagicMock()
        mock_client.conversations_history.return_value = self._mock_resp([])
        result = sc.get_channel_history("C0EMPTY", oldest_ts=0.0, client=mock_client)
        assert result == []


@pytest.mark.skipif(not _IMPORT_OK, reason="slack_connector not importable")
class TestGetThreadReplies:
    """Layer B — get_thread_replies() excluding parent message."""

    def test_parent_excluded_from_results(self):
        parent_ts = "1717002000.000000"
        msgs = [
            {"ts": parent_ts, "user": "U0A", "text": "Parent message"},
            {"ts": "1717002001.000001", "thread_ts": parent_ts, "user": "U0B", "text": "Reply 1"},
            {"ts": "1717002002.000002", "thread_ts": parent_ts, "user": "U0C", "text": "Reply 2"},
        ]
        mock_client = MagicMock()
        resp = MagicMock()
        resp.get = lambda k, d=None: {
            "messages": msgs,
            "has_more": False,
        }.get(k, d)
        mock_client.conversations_replies.return_value = resp
        result = sc.get_thread_replies("C0TEST", parent_ts, client=mock_client)
        # Parent should be excluded
        assert all(m["ts"] != parent_ts for m in result)
        assert len(result) == 2

    def test_api_error_returns_empty_list(self):
        mock_client = MagicMock()
        mock_client.conversations_replies.side_effect = Exception("channel_not_found")
        result = sc.get_thread_replies("C0TEST", "1717000000.000000", client=mock_client)
        assert result == []
