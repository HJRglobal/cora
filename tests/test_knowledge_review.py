"""Tests for src/cora/knowledge_review.py — Component 0: Harrison 👍/👎 approval flow.

Layer A: string/logic assertions that run without any cora dependencies.
Layer B: import-guarded unit tests with mocks (skipped on stale CIFS mount).

Coverage:
  - classify_reaction(): APPROVED / DISMISSED / COMMENT_REQUESTED / OTHER
  - propose_update(): file write, schema validation
  - log_reply_reaction(): file write, schema validation
  - resolve_update(): state machine transitions
  - get_pending_updates(): filters to PENDING state
  - correlate_reactions_to_updates(): reaction -> update matching
  - format_pending_dm(): DM text formatting
  - send_dm_to_harrison(): import path + interface shape
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── Layer A: string/logic (no cora imports needed) ────────────────────────────

class TestClassifyReaction:
    """Layer A — pure-string reaction classification."""

    def test_thumbsup_approved(self):
        # Import inline — tolerate stale CIFS
        try:
            from src.cora.knowledge_review import classify_reaction
        except ImportError:
            pytest.skip("cora imports unavailable on this mount")
        assert classify_reaction("+1") == "APPROVED"

    def test_thumbsdown_dismissed(self):
        try:
            from src.cora.knowledge_review import classify_reaction
        except ImportError:
            pytest.skip("cora imports unavailable")
        assert classify_reaction("-1") == "DISMISSED"

    def test_x_dismissed(self):
        try:
            from src.cora.knowledge_review import classify_reaction
        except ImportError:
            pytest.skip("cora imports unavailable")
        assert classify_reaction("x") == "DISMISSED"

    def test_speech_balloon_comment(self):
        try:
            from src.cora.knowledge_review import classify_reaction
        except ImportError:
            pytest.skip("cora imports unavailable")
        assert classify_reaction("speech_balloon") == "COMMENT_REQUESTED"

    def test_thumbsup_variant_approved(self):
        try:
            from src.cora.knowledge_review import classify_reaction
        except ImportError:
            pytest.skip("cora imports unavailable")
        assert classify_reaction("thumbsup") == "APPROVED"

    def test_white_check_mark_approved(self):
        try:
            from src.cora.knowledge_review import classify_reaction
        except ImportError:
            pytest.skip("cora imports unavailable")
        assert classify_reaction("white_check_mark") == "APPROVED"

    def test_fire_other(self):
        try:
            from src.cora.knowledge_review import classify_reaction
        except ImportError:
            pytest.skip("cora imports unavailable")
        # fire is positive feedback but NOT an approval action for knowledge-review
        assert classify_reaction("fire") == "OTHER"

    def test_empty_reaction_other(self):
        try:
            from src.cora.knowledge_review import classify_reaction
        except ImportError:
            pytest.skip("cora imports unavailable")
        assert classify_reaction("") == "OTHER"

    def test_skin_tone_modifier_stripped(self):
        try:
            from src.cora.knowledge_review import classify_reaction
        except ImportError:
            pytest.skip("cora imports unavailable")
        # Slack sends "+1::skin-tone-2" — should still be APPROVED
        assert classify_reaction("+1::skin-tone-2") == "APPROVED"

    def test_unknown_reaction_other(self):
        try:
            from src.cora.knowledge_review import classify_reaction
        except ImportError:
            pytest.skip("cora imports unavailable")
        assert classify_reaction("party_parrot") == "OTHER"


class TestFormatPendingDm:
    """Layer A — DM text formatting assertions."""

    def _get_format_fn(self):
        try:
            from src.cora.knowledge_review import format_pending_dm
            return format_pending_dm
        except ImportError:
            pytest.skip("cora imports unavailable")

    def _make_update(self, uid=None, utype="asana_task", desc="Test action", evidence="From Slack", conf="HIGH"):
        return {
            "update_id": uid or str(uuid.uuid4()),
            "update_type": utype,
            "description": desc,
            "source_evidence": evidence,
            "confidence": conf,
            "state": "PENDING",
        }

    def test_empty_list_returns_empty_string(self):
        fmt = self._get_format_fn()
        assert fmt([]) == ""

    def test_single_update_contains_description(self):
        fmt = self._get_format_fn()
        update = self._make_update(desc="Create task for BCB deadline")
        result = fmt([update])
        assert "Create task for BCB deadline" in result

    def test_contains_high_confidence_emoji(self):
        fmt = self._get_format_fn()
        update = self._make_update(conf="HIGH")
        result = fmt([update])
        assert "HIGH" in result

    def test_contains_med_confidence_marker(self):
        fmt = self._get_format_fn()
        update = self._make_update(conf="MED")
        result = fmt([update])
        assert "MED" in result

    def test_multiple_updates_numbered(self):
        fmt = self._get_format_fn()
        updates = [self._make_update(desc=f"Action {i}") for i in range(3)]
        result = fmt(updates)
        assert "1." in result
        assert "2." in result
        assert "3." in result

    def test_contains_approval_instructions(self):
        fmt = self._get_format_fn()
        update = self._make_update()
        result = fmt([update])
        # Should contain some mention of thumbs or approval
        assert "👍" in result or "approve" in result.lower()

    def test_contains_dismiss_instructions(self):
        fmt = self._get_format_fn()
        update = self._make_update()
        result = fmt([update])
        assert "👎" in result or "dismiss" in result.lower()

    def test_asana_task_type_label(self):
        fmt = self._get_format_fn()
        update = self._make_update(utype="asana_task")
        result = fmt([update])
        assert "Asana task" in result

    def test_decision_capture_type_label(self):
        fmt = self._get_format_fn()
        update = self._make_update(utype="decision_capture")
        result = fmt([update])
        assert "Decision" in result

    def test_source_evidence_truncated(self):
        fmt = self._get_format_fn()
        long_evidence = "x" * 300
        update = self._make_update(evidence=long_evidence)
        result = fmt([update])
        # Evidence should be present but truncated to ~200 chars
        assert "x" * 200 in result
        assert len(result) < 5000  # Sanity: not unboundedly long


# ── Layer B: import-guarded unit tests with mocks ─────────────────────────────

try:
    from src.cora import knowledge_review as kr
    _IMPORT_OK = True
except Exception:
    _IMPORT_OK = False


@pytest.mark.skipif(not _IMPORT_OK, reason="cora imports unavailable on this mount")
class TestProposeUpdateIO:
    """Layer B — file I/O for propose_update()."""

    def test_propose_creates_file(self, tmp_path):
        with patch.object(kr, "_PROPOSED_UPDATES_PATH", tmp_path / "updates.jsonl"):
            kr.propose_update(
                update_id="test-001",
                update_type=kr.UPDATE_TYPE_ASANA_TASK,
                description="Create BCB deadline task",
                payload={"project_gid": "123", "name": "BCB deadline"},
                source_evidence="Slack says BCB on May 27",
                confidence="HIGH",
            )
            assert (tmp_path / "updates.jsonl").exists()

    def test_propose_valid_json(self, tmp_path):
        with patch.object(kr, "_PROPOSED_UPDATES_PATH", tmp_path / "updates.jsonl"):
            kr.propose_update(
                update_id="test-002",
                update_type=kr.UPDATE_TYPE_DECISION,
                description="Pure launch locked 6/15",
                payload={"decision_text": "Pure launch is 6/15"},
            )
            lines = (tmp_path / "updates.jsonl").read_text(encoding="utf-8").splitlines()
            assert len(lines) == 1
            entry = json.loads(lines[0])
            assert entry["update_id"] == "test-002"
            assert entry["state"] == "PENDING"
            assert entry["update_type"] == kr.UPDATE_TYPE_DECISION

    def test_propose_state_is_pending(self, tmp_path):
        with patch.object(kr, "_PROPOSED_UPDATES_PATH", tmp_path / "updates.jsonl"):
            kr.propose_update(
                update_id="test-003",
                update_type=kr.UPDATE_TYPE_GENERIC,
                description="Generic action",
                payload={},
            )
            lines = (tmp_path / "updates.jsonl").read_text(encoding="utf-8").splitlines()
            entry = json.loads(lines[0])
            assert entry["state"] == "PENDING"
            assert entry["resolved_at"] is None

    def test_propose_multiple_appends(self, tmp_path):
        with patch.object(kr, "_PROPOSED_UPDATES_PATH", tmp_path / "updates.jsonl"):
            for i in range(3):
                kr.propose_update(
                    update_id=f"test-multi-{i}",
                    update_type=kr.UPDATE_TYPE_GENERIC,
                    description=f"Action {i}",
                    payload={},
                )
            lines = (tmp_path / "updates.jsonl").read_text(encoding="utf-8").splitlines()
            assert len(lines) == 3


@pytest.mark.skipif(not _IMPORT_OK, reason="cora imports unavailable on this mount")
class TestLogReplyReaction:
    """Layer B — file I/O for log_reply_reaction()."""

    def test_log_creates_file(self, tmp_path):
        with patch.object(kr, "_REPLY_LOG_PATH", tmp_path / "reply-log.jsonl"):
            kr.log_reply_reaction(
                reactor_id=kr.HARRISON_SLACK_USER_ID,
                reaction="+1",
                message_ts="1717000000.123456",
                channel_id="D0ABCDEF123",
                channel_name="directmessage",
            )
            assert (tmp_path / "reply-log.jsonl").exists()

    def test_log_entry_schema(self, tmp_path):
        with patch.object(kr, "_REPLY_LOG_PATH", tmp_path / "reply-log.jsonl"):
            kr.log_reply_reaction(
                reactor_id=kr.HARRISON_SLACK_USER_ID,
                reaction="-1",
                message_ts="1717000001.000000",
                channel_id="D0XYZ",
                channel_name="directmessage",
                event_type="reaction_added",
            )
            lines = (tmp_path / "reply-log.jsonl").read_text(encoding="utf-8").splitlines()
            entry = json.loads(lines[0])
            assert entry["reactor_id"] == kr.HARRISON_SLACK_USER_ID
            assert entry["action"] == "DISMISSED"
            assert entry["message_ts"] == "1717000001.000000"

    def test_log_approved_action_field(self, tmp_path):
        with patch.object(kr, "_REPLY_LOG_PATH", tmp_path / "reply-log.jsonl"):
            kr.log_reply_reaction(
                reactor_id=kr.HARRISON_SLACK_USER_ID,
                reaction="thumbsup",
                message_ts="1717000002.000000",
                channel_id="D0XYZ",
                channel_name="directmessage",
            )
            entry = json.loads((tmp_path / "reply-log.jsonl").read_text(encoding="utf-8").strip())
            assert entry["action"] == "APPROVED"


@pytest.mark.skipif(not _IMPORT_OK, reason="cora imports unavailable on this mount")
class TestResolveUpdate:
    """Layer B — state machine transitions in resolve_update()."""

    def _write_pending(self, path: Path, uid: str) -> None:
        entry = {
            "update_id": uid,
            "update_type": kr.UPDATE_TYPE_GENERIC,
            "description": "test",
            "payload": {},
            "state": "PENDING",
            "proposed_at": "2026-05-27T07:00:00+00:00",
            "resolved_at": None,
            "dm_message_ts": "1717001000.000000",
            "dm_channel_id": "D0ABC",
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    def test_resolve_to_approved(self, tmp_path):
        path = tmp_path / "updates.jsonl"
        self._write_pending(path, "uid-approve-1")
        with patch.object(kr, "_PROPOSED_UPDATES_PATH", path):
            result = kr.resolve_update("uid-approve-1", "APPROVED")
        assert result is True
        entry = json.loads(path.read_text(encoding="utf-8").strip())
        assert entry["state"] == "APPROVED"
        assert entry["resolved_at"] is not None

    def test_resolve_to_dismissed(self, tmp_path):
        path = tmp_path / "updates.jsonl"
        self._write_pending(path, "uid-dismiss-1")
        with patch.object(kr, "_PROPOSED_UPDATES_PATH", path):
            result = kr.resolve_update("uid-dismiss-1", "DISMISSED")
        assert result is True
        entry = json.loads(path.read_text(encoding="utf-8").strip())
        assert entry["state"] == "DISMISSED"

    def test_resolve_unknown_id_returns_false(self, tmp_path):
        path = tmp_path / "updates.jsonl"
        self._write_pending(path, "uid-real")
        with patch.object(kr, "_PROPOSED_UPDATES_PATH", path):
            result = kr.resolve_update("uid-nonexistent", "APPROVED")
        assert result is False

    def test_resolve_already_resolved_returns_false(self, tmp_path):
        path = tmp_path / "updates.jsonl"
        self._write_pending(path, "uid-already")
        with patch.object(kr, "_PROPOSED_UPDATES_PATH", path):
            kr.resolve_update("uid-already", "APPROVED")
            # Second call on same ID should return False (already resolved)
            result = kr.resolve_update("uid-already", "DISMISSED")
        assert result is False

    def test_resolve_missing_file_returns_false(self, tmp_path):
        nonexistent = tmp_path / "does-not-exist.jsonl"
        with patch.object(kr, "_PROPOSED_UPDATES_PATH", nonexistent):
            result = kr.resolve_update("any-id", "APPROVED")
        assert result is False


@pytest.mark.skipif(not _IMPORT_OK, reason="cora imports unavailable on this mount")
class TestGetPendingUpdates:
    """Layer B — pending filter in get_pending_updates()."""

    def _write_entry(self, path: Path, uid: str, state: str) -> None:
        entry = {
            "update_id": uid,
            "update_type": kr.UPDATE_TYPE_GENERIC,
            "description": uid,
            "payload": {},
            "state": state,
            "proposed_at": "2026-05-27T07:00:00+00:00",
            "resolved_at": None if state == "PENDING" else "2026-05-27T08:00:00+00:00",
            "dm_message_ts": "",
            "dm_channel_id": "",
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    def test_returns_only_pending(self, tmp_path):
        path = tmp_path / "updates.jsonl"
        self._write_entry(path, "p1", "PENDING")
        self._write_entry(path, "a1", "APPROVED")
        self._write_entry(path, "p2", "PENDING")
        self._write_entry(path, "d1", "DISMISSED")
        with patch.object(kr, "_PROPOSED_UPDATES_PATH", path):
            pending = kr.get_pending_updates()
        assert len(pending) == 2
        assert all(u["state"] == "PENDING" for u in pending)

    def test_empty_file_returns_empty(self, tmp_path):
        path = tmp_path / "updates.jsonl"
        path.touch()
        with patch.object(kr, "_PROPOSED_UPDATES_PATH", path):
            pending = kr.get_pending_updates()
        assert pending == []

    def test_missing_file_returns_empty(self, tmp_path):
        path = tmp_path / "does-not-exist.jsonl"
        with patch.object(kr, "_PROPOSED_UPDATES_PATH", path):
            pending = kr.get_pending_updates()
        assert pending == []


@pytest.mark.skipif(not _IMPORT_OK, reason="cora imports unavailable on this mount")
class TestCorrelateReactions:
    """Layer B — correlate_reactions_to_updates() state machine."""

    def _write_update(self, path: Path, uid: str, dm_ts: str) -> None:
        entry = {
            "update_id": uid,
            "update_type": kr.UPDATE_TYPE_ASANA_TASK,
            "description": f"Action for {uid}",
            "payload": {},
            "state": "PENDING",
            "proposed_at": "2026-05-27T07:00:00+00:00",
            "resolved_at": None,
            "dm_message_ts": dm_ts,
            "dm_channel_id": "D0ABC123",
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    def _write_reaction(self, path: Path, reactor: str, reaction: str, msg_ts: str) -> None:
        entry = {
            "ts": "2026-05-27T07:30:00+00:00",
            "reactor_id": reactor,
            "reaction": reaction,
            "action": kr.classify_reaction(reaction),
            "message_ts": msg_ts,
            "channel_id": "D0ABC123",
            "channel_name": "directmessage",
            "event_type": "reaction_added",
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    def test_matched_approved_pair(self, tmp_path):
        upd_path = tmp_path / "updates.jsonl"
        rep_path = tmp_path / "reply-log.jsonl"
        dm_ts = "1717100000.000000"
        self._write_update(upd_path, "uid-match", dm_ts)
        self._write_reaction(rep_path, kr.HARRISON_SLACK_USER_ID, "+1", dm_ts)
        with (
            patch.object(kr, "_PROPOSED_UPDATES_PATH", upd_path),
            patch.object(kr, "_REPLY_LOG_PATH", rep_path),
        ):
            pairs = kr.correlate_reactions_to_updates()
        assert len(pairs) == 1
        update, reaction = pairs[0]
        assert update["update_id"] == "uid-match"
        assert reaction["action"] == "APPROVED"

    def test_non_harrison_reaction_ignored(self, tmp_path):
        upd_path = tmp_path / "updates.jsonl"
        rep_path = tmp_path / "reply-log.jsonl"
        dm_ts = "1717200000.000000"
        self._write_update(upd_path, "uid-ignore", dm_ts)
        self._write_reaction(rep_path, "U0SOMEONE_ELSE", "+1", dm_ts)
        with (
            patch.object(kr, "_PROPOSED_UPDATES_PATH", upd_path),
            patch.object(kr, "_REPLY_LOG_PATH", rep_path),
        ):
            pairs = kr.correlate_reactions_to_updates()
        assert len(pairs) == 0

    def test_no_matching_ts_returns_empty(self, tmp_path):
        upd_path = tmp_path / "updates.jsonl"
        rep_path = tmp_path / "reply-log.jsonl"
        self._write_update(upd_path, "uid-no-match", "1717300000.000000")
        self._write_reaction(rep_path, kr.HARRISON_SLACK_USER_ID, "+1", "1717999999.000000")
        with (
            patch.object(kr, "_PROPOSED_UPDATES_PATH", upd_path),
            patch.object(kr, "_REPLY_LOG_PATH", rep_path),
        ):
            pairs = kr.correlate_reactions_to_updates()
        assert len(pairs) == 0

    def test_dismissed_reaction_included(self, tmp_path):
        upd_path = tmp_path / "updates.jsonl"
        rep_path = tmp_path / "reply-log.jsonl"
        dm_ts = "1717400000.000000"
        self._write_update(upd_path, "uid-dismiss", dm_ts)
        self._write_reaction(rep_path, kr.HARRISON_SLACK_USER_ID, "-1", dm_ts)
        with (
            patch.object(kr, "_PROPOSED_UPDATES_PATH", upd_path),
            patch.object(kr, "_REPLY_LOG_PATH", rep_path),
        ):
            pairs = kr.correlate_reactions_to_updates()
        assert len(pairs) == 1
        _, reaction = pairs[0]
        assert reaction["action"] == "DISMISSED"

    def test_already_resolved_update_not_returned(self, tmp_path):
        upd_path = tmp_path / "updates.jsonl"
        rep_path = tmp_path / "reply-log.jsonl"
        dm_ts = "1717500000.000000"
        # Write as already APPROVED
        entry = {
            "update_id": "uid-resolved",
            "update_type": kr.UPDATE_TYPE_GENERIC,
            "description": "already done",
            "payload": {},
            "state": "APPROVED",
            "proposed_at": "2026-05-27T06:00:00+00:00",
            "resolved_at": "2026-05-27T07:00:00+00:00",
            "dm_message_ts": dm_ts,
            "dm_channel_id": "D0ABC",
        }
        with upd_path.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        self._write_reaction(rep_path, kr.HARRISON_SLACK_USER_ID, "+1", dm_ts)
        with (
            patch.object(kr, "_PROPOSED_UPDATES_PATH", upd_path),
            patch.object(kr, "_REPLY_LOG_PATH", rep_path),
        ):
            pairs = kr.correlate_reactions_to_updates()
        assert len(pairs) == 0


@pytest.mark.skipif(not _IMPORT_OK, reason="cora imports unavailable on this mount")
class TestSendDmInterface:
    """Layer B — send_dm_to_harrison() interface / smoke test."""

    def test_missing_token_returns_none(self):
        result = kr.send_dm_to_harrison("Test message", slack_bot_token="")
        assert result is None

    def test_returns_none_on_slack_error(self):
        """Mocked SDK call raises an exception — function returns None gracefully."""
        mock_client = MagicMock()
        mock_client.conversations_open.side_effect = Exception("Slack API down")
        with patch("src.cora.knowledge_review.SlackWebClient", return_value=mock_client, create=True):
            try:
                from slack_sdk import WebClient  # Verify slack_sdk is available
                result = kr.send_dm_to_harrison("Hello", slack_bot_token="xoxb-fake-token")
                assert result is None
            except ImportError:
                pytest.skip("slack_sdk not installed in this environment")

    def test_returns_ts_on_success(self):
        """Mocked successful DM send — returns message_ts string via _client_factory injection."""
        mock_client = MagicMock()
        mock_client.conversations_open.return_value = {"channel": {"id": "D0DUMMY"}}
        mock_client.chat_postMessage.return_value = {"ts": "1717600000.000000", "ok": True}
        result = kr.send_dm_to_harrison(
            "Hello Harrison",
            slack_bot_token="xoxb-fake",
            _client_factory=lambda: mock_client,
        )
        assert result == "1717600000.000000"


@pytest.mark.skipif(not _IMPORT_OK, reason="cora imports unavailable on this mount")
class TestProposeUpdateIdempotency:
    """WS17-B item 2: propose_update is idempotent on update_id, so a re-run /
    backfill that re-derives an existing id can't re-flood the ledger."""

    def _count(self, path):
        return len([l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()])

    def test_duplicate_id_not_appended(self, tmp_path):
        path = tmp_path / "updates.jsonl"
        with patch.object(kr, "_PROPOSED_UPDATES_PATH", path):
            kr._SEEN_IDS_CACHE = None
            first = kr.propose_update(update_id="dup-1", update_type="known_answer",
                                      description="d", payload={})
            second = kr.propose_update(update_id="dup-1", update_type="known_answer",
                                       description="d again", payload={})
            assert first is True
            assert second is False
            assert self._count(path) == 1  # only the first append landed

    def test_distinct_ids_each_append(self, tmp_path):
        path = tmp_path / "updates.jsonl"
        with patch.object(kr, "_PROPOSED_UPDATES_PATH", path):
            kr._SEEN_IDS_CACHE = None
            assert kr.propose_update(update_id="a", update_type="generic",
                                     description="d", payload={}) is True
            assert kr.propose_update(update_id="b", update_type="generic",
                                     description="d", payload={}) is True
            assert self._count(path) == 2

    def test_dedup_sees_external_append(self, tmp_path):
        """An id written by another process (cache rebuilt on mtime change) is deduped."""
        path = tmp_path / "updates.jsonl"
        with patch.object(kr, "_PROPOSED_UPDATES_PATH", path):
            kr._SEEN_IDS_CACHE = None
            # Simulate a concurrent producer process appending directly.
            path.write_text(json.dumps({"update_id": "ext-1", "state": "PENDING"}) + "\n",
                            encoding="utf-8")
            assert kr.propose_update(update_id="ext-1", update_type="generic",
                                     description="d", payload={}) is False
            assert kr.propose_update(update_id="new-1", update_type="generic",
                                     description="d", payload={}) is True
            assert self._count(path) == 2
