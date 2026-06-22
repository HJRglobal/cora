"""Tests for the paraphrase-confirm loop in team_learning.py."""
import time
from unittest.mock import MagicMock, patch

import pytest

import cora.team_learning as tl


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Point team_learning at a fresh temp DB for each test."""
    db_path = tmp_path / "test_kb.db"
    monkeypatch.setattr(tl, "_KB_DB_PATH", db_path)
    yield


# ---------------------------------------------------------------------------
# store / get / clear
# ---------------------------------------------------------------------------

class TestPendingConfirmCRUD:
    def test_store_and_get(self):
        tl.store_pending_confirm(
            channel_id="C001", thread_ts="1.0",
            entity="F3E", channel_name="f3e-leadership",
            author="U001", kind="note",
            raw_content="Sales hit $50K.", paraphrase="F3E sales hit $50K.",
        )
        result = tl.get_pending_confirm("C001", "1.0")
        assert result is not None
        assert result["entity"] == "F3E"
        assert result["author"] == "U001"
        assert result["paraphrase"] == "F3E sales hit $50K."
        assert result["raw_content"] == "Sales hit $50K."

    def test_get_missing_returns_none(self):
        assert tl.get_pending_confirm("Cnone", "0.0") is None

    def test_clear_removes_record(self):
        tl.store_pending_confirm("C002", "2.0", "OSN", "osn", "U002", "note", "x", "y")
        tl.clear_pending_confirm("C002", "2.0")
        assert tl.get_pending_confirm("C002", "2.0") is None

    def test_replace_on_second_store(self):
        tl.store_pending_confirm("C003", "3.0", "F3E", "c", "U003", "note", "old", "old para")
        tl.store_pending_confirm("C003", "3.0", "F3E", "c", "U003", "note", "old", "new para")
        r = tl.get_pending_confirm("C003", "3.0")
        assert r["paraphrase"] == "new para"

    def test_different_threads_isolated(self):
        tl.store_pending_confirm("C004", "4.0", "F3E", "c", "U", "note", "a", "a")
        tl.store_pending_confirm("C004", "4.1", "OSN", "c", "U", "note", "b", "b")
        r1 = tl.get_pending_confirm("C004", "4.0")
        r2 = tl.get_pending_confirm("C004", "4.1")
        assert r1["entity"] == "F3E"
        assert r2["entity"] == "OSN"

    def test_expired_record_returns_none(self, monkeypatch):
        tl.store_pending_confirm("C005", "5.0", "F3E", "c", "U", "note", "x", "y")
        # Simulate time far in the future using a fixed timestamp
        monkeypatch.setattr(tl.time, "time", lambda: 9_999_999_999.0)
        assert tl.get_pending_confirm("C005", "5.0") is None

    def test_clear_idempotent(self):
        # Clearing a non-existent record should not raise
        tl.clear_pending_confirm("Cx", "9.9")


# ---------------------------------------------------------------------------
# is_confirmation
# ---------------------------------------------------------------------------

class TestIsConfirmation:
    def test_yes(self):
        for text in ("yes", "Yes", "YES", "yep", "yup", "y"):
            assert tl.is_confirmation(text), f"failed: {text!r}"

    def test_ok(self):
        for text in ("ok", "okay", "k"):
            assert tl.is_confirmation(text), f"failed: {text!r}"

    def test_longer_phrases(self):
        assert tl.is_confirmation("looks good")
        assert tl.is_confirmation("sounds good")
        assert tl.is_confirmation("lgtm")
        assert tl.is_confirmation("approved")

    def test_trailing_punctuation(self):
        assert tl.is_confirmation("yes!")
        assert tl.is_confirmation("ok.")

    def test_not_confirmation(self):
        for text in ("actually", "no", "wait", "correction: it was $60K"):
            assert not tl.is_confirmation(text), f"should be False: {text!r}"


# ---------------------------------------------------------------------------
# paraphrase_note (mocked Haiku)
# ---------------------------------------------------------------------------

def test_paraphrase_note_calls_haiku():
    mock_client = MagicMock()
    mock_client.messages.create.return_value = MagicMock(
        content=[MagicMock(text="F3E closed a deal with Acme.")],
    )
    with patch("anthropic.Anthropic", return_value=mock_client):
        result = tl.paraphrase_note("we closed acme deal", "F3E")
    assert result == "F3E closed a deal with Acme."


def test_paraphrase_note_incorporates_correction():
    mock_client = MagicMock()
    mock_client.messages.create.return_value = MagicMock(
        content=[MagicMock(text="F3E closed the Acme deal for $50K.")],
    )
    with patch("anthropic.Anthropic", return_value=mock_client):
        result = tl.paraphrase_note("acme deal", "F3E", correction="it was $50K")
    assert "$50K" in result


def test_paraphrase_note_falls_back_on_api_error():
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = Exception("API down")
    with patch("anthropic.Anthropic", return_value=mock_client):
        result = tl.paraphrase_note("raw content here", "F3E")
    assert result == "raw content here"
