"""Tests for connectors/channel_sweep.py (Phase 3.2 -- the critical untested module).

Covers channel listing (public-only + pagination + is_member filter), message
fetch (bot/system/empty skip + cap), the deny-list + exclusion filter in run_sweep,
the dry-run path (no Haiku), and synthesis fail-soft + ```json fence stripping.
All Slack/Anthropic calls are mocked -- no network. The public-only listing
behavior is asserted explicitly so a future consolidation can't silently widen it.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cora.connectors import channel_sweep as cs  # noqa: E402


def _no_sleep(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)


class TestListJoinedChannels:
    def test_returns_only_member_channels_and_paginates(self, monkeypatch):
        _no_sleep(monkeypatch)
        client = MagicMock()
        client.conversations_list.side_effect = [
            {"channels": [
                {"id": "C1", "name": "f3-sales", "is_member": True},
                {"id": "C2", "name": "not-joined", "is_member": False},
            ], "response_metadata": {"next_cursor": "PAGE2"}},
            {"channels": [
                {"id": "C3", "name": "osn-leadership", "is_member": True},
            ], "response_metadata": {"next_cursor": ""}},
        ]
        out = cs.list_joined_channels(client)
        assert {c["id"] for c in out} == {"C1", "C3"}  # C2 dropped (not member); both pages read
        assert client.conversations_list.call_count == 2

    def test_requests_public_channels_only(self, monkeypatch):
        # Locks the INTENTIONAL public-only scope (vs the KB sweep's all-types).
        _no_sleep(monkeypatch)
        client = MagicMock()
        client.conversations_list.return_value = {"channels": [], "response_metadata": {}}
        cs.list_joined_channels(client)
        kwargs = client.conversations_list.call_args.kwargs
        assert kwargs["types"] == "public_channel"
        assert kwargs["exclude_archived"] is True

    def test_api_error_returns_what_it_has(self, monkeypatch):
        _no_sleep(monkeypatch)
        client = MagicMock()
        client.conversations_list.side_effect = RuntimeError("boom")
        assert cs.list_joined_channels(client) == []


class TestFetchChannelMessages:
    def test_skips_bots_system_and_empty(self, monkeypatch):
        _no_sleep(monkeypatch)
        client = MagicMock()
        client.conversations_history.return_value = {
            "messages": [
                {"user": "U1", "text": "real message", "ts": "1"},
                {"bot_id": "B1", "text": "bot noise", "ts": "2"},
                {"subtype": "channel_join", "user": "U2", "text": "joined", "ts": "3"},
                {"user": "U3", "text": "", "ts": "4"},   # empty text
                {"text": "no user", "ts": "5"},          # no user
            ],
            "has_more": False, "response_metadata": {},
        }
        msgs = cs.fetch_channel_messages(client, "C1", "0")
        assert len(msgs) == 1 and msgs[0]["user"] == "U1"

    def test_caps_at_max_messages(self, monkeypatch):
        _no_sleep(monkeypatch)
        client = MagicMock()
        client.conversations_history.return_value = {
            "messages": [{"user": f"U{i}", "text": "x", "ts": str(i)} for i in range(200)],
            "has_more": True, "response_metadata": {"next_cursor": "MORE"},
        }
        msgs = cs.fetch_channel_messages(client, "C1", "0")
        assert len(msgs) >= cs._MAX_MESSAGES_PER_CHANNEL  # loop stops once the cap is reached

    def test_history_error_breaks_gracefully(self, monkeypatch):
        _no_sleep(monkeypatch)
        client = MagicMock()
        client.conversations_history.side_effect = RuntimeError("history down")
        assert cs.fetch_channel_messages(client, "C1", "0") == []


class TestRunSweep:
    def _client(self, channels, history):
        client = MagicMock()
        client.conversations_list.return_value = {"channels": channels, "response_metadata": {}}
        client.conversations_history.return_value = {
            "messages": history, "has_more": False, "response_metadata": {}}
        client.users_info.return_value = {"user": {"profile": {"display_name": "Tommy"}}}
        return client

    def test_dry_run_builds_activity_without_haiku(self, monkeypatch):
        _no_sleep(monkeypatch)
        monkeypatch.setattr(cs, "should_ingest", lambda *a, **k: True)
        client = self._client(
            [{"id": "C1", "name": "f3-sales", "is_member": True}],
            [{"user": "U1", "text": "shipping samples Friday", "ts": "1"}],
        )
        anthropic = MagicMock()
        res = cs.run_sweep(client, anthropic_client=anthropic, dry_run=True)
        assert res.users_active == 1
        assert res.user_activity["U1"].display_name == "Tommy"
        anthropic.messages.create.assert_not_called()

    def test_excludes_general_and_denied_channels(self, monkeypatch):
        _no_sleep(monkeypatch)
        monkeypatch.setattr(cs, "should_ingest", lambda name, cid, priv: name != "secret-room")
        client = self._client(
            [
                {"id": "C1", "name": "f3-sales", "is_member": True},
                {"id": "C2", "name": "general", "is_member": True},      # excluded by name
                {"id": "C3", "name": "secret-room", "is_member": True},  # denied by policy
            ],
            [{"user": "U1", "text": "hi", "ts": "1"}],
        )
        res = cs.run_sweep(client, anthropic_client=None, dry_run=True)
        assert res.channels_swept == 1  # only f3-sales survives


class TestSynthesizeUserActivity:
    def test_no_client_returns_empty(self):
        out = cs.synthesize_user_activity(None, "U1", "Tommy", [{"text": "x"}])
        assert out == {"commitments": [], "decisions": [], "open_questions": [],
                       "cross_entity_mentions": []}

    def test_strips_json_fences(self):
        client = MagicMock()
        resp = MagicMock()
        resp.content = [MagicMock(text=(
            '```json\n{"commitments": ["ship Friday"], "decisions": [], '
            '"open_questions": [], "cross_entity_mentions": []}\n```'))]
        client.messages.create.return_value = resp
        out = cs.synthesize_user_activity(
            client, "U1", "Tommy", [{"text": "x", "channel_name": "f3-sales"}])
        assert out["commitments"] == ["ship Friday"]

    def test_error_returns_empty(self):
        client = MagicMock()
        client.messages.create.side_effect = RuntimeError("api down")
        out = cs.synthesize_user_activity(client, "U1", "Tommy", [{"text": "x"}])
        assert out["commitments"] == []
