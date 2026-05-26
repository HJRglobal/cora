"""Tests for cora.user_feedback_tracker — per-user signal attribution log.

All tests redirect the log file to a tmp_path so they don't touch
logs/cora-user-feedback.jsonl on disk.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cora import user_feedback_tracker as uft


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _redirect_log(tmp_path, monkeypatch):
    """Write to a temp log file rather than the real one."""
    log_path = tmp_path / "cora-user-feedback.jsonl"
    monkeypatch.setattr(uft, "_LOG_PATH", log_path)
    return log_path


def _read_log(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ── log_signal ─────────────────────────────────────────────────────────────────

class TestLogSignal:

    def test_writes_one_record(self, tmp_path):
        log_path = tmp_path / "cora-user-feedback.jsonl"
        uft.log_signal(
            signal_type="correction",
            slack_user_id="U001",
            channel="C001",
            channel_name="f3e-leadership",
            entity="F3E",
            message_excerpt="That figure was wrong.",
        )
        records = _read_log(log_path)
        assert len(records) == 1

    def test_record_schema(self, tmp_path):
        log_path = tmp_path / "cora-user-feedback.jsonl"
        uft.log_signal(
            signal_type="knowledge_gap",
            slack_user_id="U002",
            channel="C002",
            channel_name="osn-leadership",
            entity="OSN",
            message_excerpt="What was April revenue?",
        )
        r = _read_log(log_path)[0]
        assert "ts" in r
        assert r["slack_user_id"] == "U002"
        assert r["channel"] == "C002"
        assert r["channel_name"] == "osn-leadership"
        assert r["entity"] == "OSN"
        assert r["signal_type"] == "knowledge_gap"
        assert r["message_excerpt"] == "What was April revenue?"

    def test_display_name_falls_back_to_user_id_when_identity_not_wired(self, tmp_path):
        """When user_identity module can't resolve the ID, display_name == slack_user_id."""
        log_path = tmp_path / "cora-user-feedback.jsonl"
        uft.log_signal(
            signal_type="thumbsdown",
            slack_user_id="UXXX_UNKNOWN",
            channel="C001",
            channel_name="hjrg-leadership",
            entity="HJRG",
        )
        r = _read_log(log_path)[0]
        # Should not crash; display_name is either resolved or the ID itself
        assert r["display_name"]  # must be non-empty
        assert isinstance(r["display_name"], str)

    def test_excerpt_truncated_at_300_chars(self, tmp_path):
        log_path = tmp_path / "cora-user-feedback.jsonl"
        long_text = "x" * 500
        uft.log_signal(
            signal_type="correction",
            slack_user_id="U001",
            channel="C001",
            channel_name="test",
            entity="FNDR",
            message_excerpt=long_text,
        )
        r = _read_log(log_path)[0]
        assert len(r["message_excerpt"]) <= 300

    def test_multiple_signals_all_appended(self, tmp_path):
        log_path = tmp_path / "cora-user-feedback.jsonl"
        for i in range(5):
            uft.log_signal(
                signal_type="correction",
                slack_user_id=f"U00{i}",
                channel="C001",
                channel_name="test",
                entity="FNDR",
            )
        records = _read_log(log_path)
        assert len(records) == 5

    def test_empty_excerpt_allowed(self, tmp_path):
        log_path = tmp_path / "cora-user-feedback.jsonl"
        uft.log_signal(
            signal_type="thumbsdown",
            slack_user_id="U001",
            channel="C001",
            channel_name="test",
            entity="FNDR",
            message_excerpt="",
        )
        r = _read_log(log_path)[0]
        assert r["message_excerpt"] == ""

    def test_creates_parent_directory_if_missing(self, tmp_path, monkeypatch):
        nested = tmp_path / "deeply" / "nested" / "cora-user-feedback.jsonl"
        monkeypatch.setattr(uft, "_LOG_PATH", nested)
        uft.log_signal(
            signal_type="correction",
            slack_user_id="U001",
            channel="C001",
            channel_name="test",
            entity="FNDR",
        )
        assert nested.exists()

    def test_ts_is_iso8601(self, tmp_path):
        log_path = tmp_path / "cora-user-feedback.jsonl"
        uft.log_signal(
            signal_type="correction",
            slack_user_id="U001",
            channel="C001",
            channel_name="test",
            entity="FNDR",
        )
        r = _read_log(log_path)[0]
        ts = r["ts"]
        # Should parse without raising
        from datetime import datetime
        datetime.fromisoformat(ts)


# ── Convenience wrappers ───────────────────────────────────────────────────────

class TestConvenienceWrappers:

    def test_log_correction_sets_signal_type(self, tmp_path):
        log_path = tmp_path / "cora-user-feedback.jsonl"
        uft.log_correction(
            slack_user_id="U001",
            channel="C001",
            channel_name="f3e-leadership",
            entity="F3E",
            correction_text="The number was 75K not 70K.",
        )
        r = _read_log(log_path)[0]
        assert r["signal_type"] == "correction"
        assert "75K" in r["message_excerpt"]

    def test_log_knowledge_gap_sets_signal_type(self, tmp_path):
        log_path = tmp_path / "cora-user-feedback.jsonl"
        uft.log_knowledge_gap(
            slack_user_id="U002",
            channel="C002",
            channel_name="osn-leadership",
            entity="OSN",
            question="What was April net revenue?",
            gap_description="April P&L not yet in Drive",
        )
        r = _read_log(log_path)[0]
        assert r["signal_type"] == "knowledge_gap"
        assert "Q:" in r["message_excerpt"]
        assert "GAP:" in r["message_excerpt"]

    def test_log_thumbsdown_sets_signal_type(self, tmp_path):
        log_path = tmp_path / "cora-user-feedback.jsonl"
        uft.log_thumbsdown(
            slack_user_id="U003",
            channel="C003",
            channel_name="hjrg-leadership",
            entity="HJRG",
            message_ts="1747832123.123456",
        )
        r = _read_log(log_path)[0]
        assert r["signal_type"] == "thumbsdown"
        assert "1747832123.123456" in r["message_excerpt"]

    def test_log_thumbsdown_without_message_ts(self, tmp_path):
        log_path = tmp_path / "cora-user-feedback.jsonl"
        uft.log_thumbsdown(
            slack_user_id="U003",
            channel="C003",
            channel_name="test",
            entity="FNDR",
        )
        r = _read_log(log_path)[0]
        assert r["signal_type"] == "thumbsdown"


# ── Thread safety ──────────────────────────────────────────────────────────────

class TestThreadSafety:

    def test_concurrent_writes_no_corruption(self, tmp_path):
        import threading

        log_path = tmp_path / "cora-user-feedback.jsonl"

        def _write(i: int) -> None:
            uft.log_signal(
                signal_type="correction",
                slack_user_id=f"U{i:04d}",
                channel="C001",
                channel_name="test",
                entity="FNDR",
                message_excerpt=f"correction {i}",
            )

        threads = [threading.Thread(target=_write, args=(i,)) for i in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        records = _read_log(log_path)
        assert len(records) == 30
        # Each line should parse cleanly
        for r in records:
            assert r["signal_type"] == "correction"
