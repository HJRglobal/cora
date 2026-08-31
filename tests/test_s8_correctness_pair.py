"""S8 correctness pair: diarization attribution + Klaviyo throttling.

Both seeds were half-right about their own defect.
"""
from __future__ import annotations

import pytest

from src.cora.connectors import klaviyo_client as kc
from src.cora.reconciliation_engine import (
    _attribution_unreliable,
    _confidence_from_ratio,
    _confidence_from_sim,
    _neutralize_speaker,
    _source_weight,
)


class TestFlagIsReadable:
    """cq-ebe18d20a949 reads as "add a check". The flag was UNREACHABLE:
    _query_kb_chunks never selected `metadata`, so passes 1-4 could not see it.
    Step one was making the field reachable, not adding a conditional."""

    def test_query_selects_metadata(self):
        import io

        src = io.open("src/cora/reconciliation_engine.py", encoding="utf-8").read()
        assert "deep_link, title, ingested_at, metadata" in src, (
            "_query_kb_chunks must select metadata or the flag is unreadable"
        )

    @pytest.mark.parametrize("raw,expected", [
        (None, False),
        ("", False),
        ('{"attribution_unreliable": true}', True),
        ('{"attribution_unreliable": false}', False),
        ('{"other": 1}', False),
        ("{not valid json", False),      # must degrade, never raise
        ({"attribution_unreliable": True}, True),   # already-parsed dict
    ])
    def test_parse_is_fail_soft(self, raw, expected):
        assert _attribution_unreliable(raw) is expected


class TestWeightDemotionChangesPriorityNotSuppression:
    """The seed says "check the flag before weighting 0.90" but does not name the
    replacement. 0.75 (the slack tier) is chosen because diarization collapse
    falsifies only the ATTRIBUTION half of the fireflies premium -- the words
    were still said, and passes 2/3/4 detect on CONTENT, not speaker."""

    def test_flagged_fireflies_drops_to_the_slack_tier(self):
        assert _source_weight("fireflies") == 0.90
        assert _source_weight("fireflies", True) == 0.75
        assert _source_weight("slack", True) == 0.75      # unrelated sources unchanged
        assert _source_weight("gmail", True) == 0.70

    def test_pass3_still_proposes(self):
        """At 0.40 (static_md tier) pass3 would go LOW and be DISCARDED, silently
        deleting the decisions flagged transcripts contribute. At 0.75 it does not."""
        assert _confidence_from_ratio(0.5, "fireflies") == "MED"
        assert _confidence_from_ratio(0.5, "fireflies", True) == "MED"

    def test_pass4_marginal_match_demotes_but_survives(self):
        assert _confidence_from_sim(0.72, "fireflies") == "HIGH"
        assert _confidence_from_sim(0.72, "fireflies", True) == "MED"   # still proposed

    def test_pass4_strong_match_is_unaffected(self):
        assert _confidence_from_sim(0.85, "fireflies", True) == "HIGH"

    def test_nothing_is_suppressed_by_the_demotion(self):
        """The invariant that made 0.75 the right number: no confidence in the
        live band becomes LOW (LOW is the only value that is dropped)."""
        for sim in (0.70, 0.75, 0.80, 0.90):
            assert _confidence_from_sim(sim, "fireflies", True) != "LOW"


class TestSpeakerNeutralisation:
    """The downweight alone does not fix the reported harm -- the cards QUOTE the
    sentence verbatim, so a mis-attributed speaker still renders to Harrison as
    fact. This is the half that fixes it."""

    def test_leading_speaker_token_replaced_when_flagged(self):
        out = _neutralize_speaker("[Harrison] We decided to ship.", True)
        assert "Harrison" not in out
        assert "diarization collapsed" in out
        assert "We decided to ship." in out

    def test_untouched_when_not_flagged(self):
        text = "[Harrison] We decided to ship."
        assert _neutralize_speaker(text, True) != text
        assert _neutralize_speaker(text, False) == text

    def test_untouched_when_no_speaker_prefix(self):
        text = "We decided to ship."
        assert _neutralize_speaker(text, True) == text

    def test_empty_input(self):
        assert _neutralize_speaker("", True) == ""

    def test_regex_is_not_a_redos(self):
        """The repo has found seven ReDoS bugs. The pattern is a bounded negated
        class with a literal anchor -- no nested quantifier over a delimited
        string. Pathological input must return promptly."""
        import time

        start = time.time()
        _neutralize_speaker("[" + "a" * 20000, True)
        assert time.time() - start < 1.0


class TestKlaviyoThrottling:
    """cq-c2eb2979e793. The None-means-unknown contract was already honest, but
    an unknown caused by throttling is not the same fact as one caused by
    absence -- and only the first is worth re-running."""

    def test_retry_after_is_honoured_and_capped(self):
        class R:
            def __init__(self, h):
                self.headers = h

        assert kc._retry_after_seconds(R({"Retry-After": "5"})) == 5.0
        assert kc._retry_after_seconds(R({"Retry-After": "9999"})) == kc._RETRY_AFTER_CAP_SEC
        assert kc._retry_after_seconds(R({"Retry-After": "soon"})) is None
        assert kc._retry_after_seconds(R({})) is None

    def test_throttle_state_resets(self):
        kc._THROTTLE_STATE["reads"] = 4
        assert kc.throttled_reads() == 4
        kc.reset_throttle_state()
        assert kc.throttled_reads() == 0

    def test_report_says_throttled_when_it_was(self):
        from src.cora import klaviyo_audit as ka

        kc._THROTTLE_STATE["reads"] = 2
        try:
            audit = ka.build_audit(segments=None, account=None, seat_holders=[])
            assert audit["throttled_reads"] == 2
            assert "rate-limited by Klaviyo" in ka.format_report(audit)
        finally:
            kc.reset_throttle_state()

    def test_report_is_silent_when_clean(self):
        """A note that always fires is noise; this one must be conditional."""
        from src.cora import klaviyo_audit as ka

        kc.reset_throttle_state()
        audit = ka.build_audit(segments=None, account=None, seat_holders=[])
        assert "rate-limited by Klaviyo" not in ka.format_report(audit)

    def test_none_still_means_unknown_never_zero(self):
        """The pre-existing contract must survive the change."""
        from src.cora.klaviyo_audit import _fmt_count

        assert _fmt_count(None) == "unknown"
        assert _fmt_count(0) == "0"
        assert _fmt_count(1234) == "1,234"
