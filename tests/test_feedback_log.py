"""Unit tests for feedback_log.classify_sentiment and log_reaction."""

import json
from pathlib import Path

import pytest

import cora.feedback_log as fl_module
from cora.feedback_log import classify_sentiment, log_reaction


# ── classify_sentiment ────────────────────────────────────────────────────────

class TestClassifySentiment:
    # Positive reactions
    @pytest.mark.parametrize("reaction", [
        "+1", "thumbsup", "heavy_check_mark", "white_check_mark",
        "100", "fire", "heart", "raised_hands", "muscle",
        "ok_hand", "tada", "clap", "rocket", "star", "star2",
    ])
    def test_positive_reactions(self, reaction):
        assert classify_sentiment(reaction) == "positive"

    # Negative reactions
    @pytest.mark.parametrize("reaction", [
        "-1", "thumbsdown", "x", "no_entry", "no_entry_sign",
        "warning", "confused", "frowning", "white_frowning_face",
        "rage", "weary", "skull",
    ])
    def test_negative_reactions(self, reaction):
        assert classify_sentiment(reaction) == "negative"

    # Neutral (unlisted / informational) reactions
    @pytest.mark.parametrize("reaction", [
        "wave", "eyes", "thinking_face", "pencil", "memo", "bulb", "question",
    ])
    def test_neutral_reactions(self, reaction):
        assert classify_sentiment(reaction) == "neutral"

    def test_empty_string_is_neutral(self):
        assert classify_sentiment("") == "neutral"

    def test_skin_tone_suffix_is_stripped_positive(self):
        # Slack appends ::skin-tone-N to base emoji names
        assert classify_sentiment("thumbsup::skin-tone-2") == "positive"
        assert classify_sentiment("+1::skin-tone-5") == "positive"

    def test_skin_tone_suffix_is_stripped_negative(self):
        assert classify_sentiment("thumbsdown::skin-tone-3") == "negative"
        assert classify_sentiment("-1::skin-tone-6") == "negative"

    def test_skin_tone_suffix_stripped_neutral(self):
        # Even an unknown emoji with a suffix should still classify as neutral
        assert classify_sentiment("wave::skin-tone-1") == "neutral"

    def test_uppercase_input_normalised(self):
        # Slack always sends lowercase, but the implementation lower()s anyway
        assert classify_sentiment("THUMBSUP") == "positive"
        assert classify_sentiment("THUMBSDOWN") == "negative"

    def test_mixed_case_normalised(self):
        assert classify_sentiment("FireWorKs") == "neutral"  # not in either set


# ── log_reaction ──────────────────────────────────────────────────────────────

class TestLogReaction:
    @pytest.fixture(autouse=True)
    def redirect_log_path(self, tmp_path, monkeypatch):
        log_file = tmp_path / "feedback.jsonl"
        monkeypatch.setattr(fl_module, "_LOG_PATH", log_file)
        self.log_file = log_file

    def _read_records(self) -> list[dict]:
        return [json.loads(line) for line in self.log_file.read_text().splitlines()]

    def test_creates_jsonl_record(self):
        log_reaction(
            channel="C_CHAN_01",
            channel_name="f3e-leadership",
            reactor="U_USER_01",
            reaction="thumbsup",
            message_ts="1747832123.456789",
        )
        records = self._read_records()
        assert len(records) == 1
        r = records[0]
        assert r["channel"] == "C_CHAN_01"
        assert r["channel_name"] == "f3e-leadership"
        assert r["reactor"] == "U_USER_01"
        assert r["reaction"] == "thumbsup"
        assert r["sentiment"] == "positive"
        assert r["message_ts"] == "1747832123.456789"
        assert r["event_type"] == "reaction_added"
        assert "ts" in r  # ISO timestamp present

    def test_default_event_type_is_reaction_added(self):
        log_reaction("C1", "ch", "U1", "fire", "111.222")
        r = self._read_records()[0]
        assert r["event_type"] == "reaction_added"

    def test_reaction_removed_event_type_stored(self):
        log_reaction("C1", "ch", "U1", "-1", "111.222", event_type="reaction_removed")
        r = self._read_records()[0]
        assert r["event_type"] == "reaction_removed"

    def test_sentiment_classified_in_record(self):
        log_reaction("C1", "ch", "U1", "thumbsdown", "ts1")
        r = self._read_records()[0]
        assert r["sentiment"] == "negative"

    def test_neutral_emoji_stored_as_neutral(self):
        log_reaction("C1", "ch", "U1", "wave", "ts1")
        r = self._read_records()[0]
        assert r["sentiment"] == "neutral"

    def test_multiple_reactions_appended_in_order(self):
        for i, emoji in enumerate(["thumbsup", "thumbsdown", "fire"]):
            log_reaction("C1", "ch", "U1", emoji, f"ts{i}")
        records = self._read_records()
        assert len(records) == 3
        assert records[0]["reaction"] == "thumbsup"
        assert records[1]["reaction"] == "thumbsdown"
        assert records[2]["reaction"] == "fire"

    def test_creates_parent_directory_if_missing(self, tmp_path, monkeypatch):
        nested = tmp_path / "deep" / "nested" / "feedback.jsonl"
        monkeypatch.setattr(fl_module, "_LOG_PATH", nested)
        log_reaction("C1", "ch", "U1", "+1", "ts1")
        assert nested.exists()

    def test_record_is_valid_json(self):
        log_reaction("C1", "ch", "U1", "tada", "ts1")
        line = self.log_file.read_text().strip()
        # Should parse without error
        record = json.loads(line)
        assert isinstance(record, dict)

    def test_ts_field_is_iso_format(self):
        log_reaction("C1", "ch", "U1", "+1", "ts1")
        r = self._read_records()[0]
        # ISO 8601 timestamps contain 'T' separator
        assert "T" in r["ts"]

    def test_thread_safe_concurrent_writes(self):
        import threading
        errors = []

        def write_reaction(i):
            try:
                log_reaction("C1", "ch", f"U{i}", "+1", f"ts{i}")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=write_reaction, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        records = self._read_records()
        assert len(records) == 10
