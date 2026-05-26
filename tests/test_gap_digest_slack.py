"""Unit tests for scripts/post_gap_digest_slack.py."""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from post_gap_digest_slack import build_slack_blocks, load_gaps  # type: ignore  # noqa: E402


# ── load_gaps() ───────────────────────────────────────────────────────────────

def _write_gaps(path: Path, gaps: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for g in gaps:
            fh.write(json.dumps(g) + "\n")


def test_load_gaps_empty_file(tmp_path):
    log = tmp_path / "gaps.jsonl"
    log.write_text("")
    result = load_gaps(log, datetime.now(timezone.utc) - timedelta(days=7))
    assert result == []


def test_load_gaps_missing_file(tmp_path):
    result = load_gaps(tmp_path / "nonexistent.jsonl", datetime.now(timezone.utc))
    assert result == []


def test_load_gaps_within_window(tmp_path):
    log = tmp_path / "gaps.jsonl"
    now = datetime.now(timezone.utc)
    gaps = [
        {"ts": now.isoformat(), "entity": "F3E", "gap": "F3 launch date unknown", "question": "q"},
        {"ts": (now - timedelta(days=10)).isoformat(), "entity": "OSN", "gap": "old gap", "question": "q2"},
    ]
    _write_gaps(log, gaps)
    result = load_gaps(log, now - timedelta(days=7))
    assert len(result) == 1
    assert result[0]["entity"] == "F3E"


def test_load_gaps_malformed_lines_skipped(tmp_path):
    log = tmp_path / "gaps.jsonl"
    log.write_text('{"ts": "bad-date", "entity": "F3E", "gap": "g"}\n{"bad json\n')
    result = load_gaps(log, datetime.now(timezone.utc) - timedelta(days=7))
    assert result == []


# ── build_slack_blocks() ──────────────────────────────────────────────────────

def test_build_slack_blocks_no_gaps():
    since = datetime.now(timezone.utc) - timedelta(days=7)
    msg = build_slack_blocks([], since, 7)
    assert "No knowledge gaps" in msg
    assert "7" in msg


def test_build_slack_blocks_with_gaps():
    now = datetime.now(timezone.utc)
    gaps = [
        {"ts": now.isoformat(), "entity": "F3E", "gap": "F3 Pure launch date", "question": "when does Pure launch?"},
        {"ts": now.isoformat(), "entity": "OSN", "gap": "G Warner breakeven", "question": "what's breakeven?"},
        {"ts": now.isoformat(), "entity": "F3E", "gap": "F3 Pure tagline", "question": "what is the tagline?"},
    ]
    since = now - timedelta(days=7)
    msg = build_slack_blocks(gaps, since, 7)
    assert "F3E" in msg
    assert "OSN" in msg
    assert "3" in msg  # total count
    assert "gap" in msg.lower()


def test_build_slack_blocks_deduplicates_same_gap():
    now = datetime.now(timezone.utc)
    # Same gap text repeated 5 times
    gap_text = "F3 Pure launch date not known"
    gaps = [{"ts": now.isoformat(), "entity": "F3E", "gap": gap_text, "question": "q"}] * 5
    since = now - timedelta(days=7)
    msg = build_slack_blocks(gaps, since, 7)
    # Should show ×5 or similar dedup signal
    assert "×5" in msg or "5" in msg


def test_build_slack_blocks_entity_ordering():
    now = datetime.now(timezone.utc)
    gaps = [
        {"ts": now.isoformat(), "entity": "OSN", "gap": "osn gap", "question": "q"},
        {"ts": now.isoformat(), "entity": "F3E", "gap": "f3e gap", "question": "q"},
        {"ts": now.isoformat(), "entity": "HJRG", "gap": "hjrg gap", "question": "q"},
    ]
    since = now - timedelta(days=7)
    msg = build_slack_blocks(gaps, since, 7)
    # F3E should appear before OSN (per ENTITY_ORDER)
    assert msg.index("F3E") < msg.index("OSN")
