"""Tests for STEP 0 (Wave-1 flywheel-conversion calibration): the one-time pool
triage + T0 baseline script.

D-051 adversarial review (test-coverage lens): _fallback_disposition is real
branching classification logic (LEX check -> capability-ask check -> eligible)
that shipped with zero test coverage. Covers the 3 branches plus the
known-vs-unlisted distinction and the baseline write shape.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPT_PATH = _REPO / "scripts" / "triage_flywheel_pool_2026-07-30.py"

_spec = importlib.util.spec_from_file_location(
    "triage_flywheel_pool_wave1", _SCRIPT_PATH)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


class TestFallbackDisposition:
    def test_lex_entity_is_walled_permanent(self):
        gap = {"entity": "LEX-LLC", "question": "what is the DDD contract term?"}
        assert m._fallback_disposition(gap) == "walled-permanent"

    def test_capability_ask_is_capability(self):
        gap = {"entity": "FNDR", "question": "can you connect to our RepRally system?"}
        assert m._fallback_disposition(gap) == "capability"

    def test_plain_question_is_eligible(self):
        gap = {"entity": "F3E", "question": "who is the Sprouts buyer?"}
        assert m._fallback_disposition(gap) == "eligible"

    def test_missing_entity_defaults_fndr_not_lex(self):
        gap = {"question": "who supplies our office coffee?"}
        assert m._fallback_disposition(gap) == "eligible"


class TestTriage:
    def test_known_ts_uses_hardcoded_disposition_not_fallback(self, monkeypatch):
        known_ts = next(iter(m._KNOWN_DISPOSITIONS))
        monkeypatch.setattr(m.gap_autofill, "load_open_gaps", lambda: [
            {"ts": known_ts, "entity": "FNDR", "question": "anything at all"},
        ])
        result = m.triage()
        assert len(result["rows"]) == 1
        assert result["rows"][0]["disposition"] == m._KNOWN_DISPOSITIONS[known_ts]
        assert result["rows"][0]["unlisted"] is False

    def test_unlisted_gap_uses_fallback_and_is_flagged(self, monkeypatch):
        monkeypatch.setattr(m.gap_autofill, "load_open_gaps", lambda: [
            {"ts": "2026-08-15T00:00:00+00:00", "entity": "F3E",
             "question": "who is the Sprouts buyer?"},
        ])
        result = m.triage()
        assert len(result["rows"]) == 1
        row = result["rows"][0]
        assert row["disposition"] == "eligible"
        assert row["unlisted"] is True

    def test_baseline_counts_and_eligible_denominator(self, monkeypatch):
        monkeypatch.setattr(m.gap_autofill, "load_open_gaps", lambda: [
            {"ts": "2026-08-15T00:00:00+00:00", "entity": "LEX",
             "question": "walled question"},
            {"ts": "2026-08-15T00:00:01+00:00", "entity": "FNDR",
             "question": "can you check our system?"},
            {"ts": "2026-08-15T00:00:02+00:00", "entity": "F3E",
             "question": "who is the Sprouts buyer?"},
        ])
        baseline = m.triage()["baseline"]
        assert baseline["total_open_count"] == 3
        assert baseline["eligible_open_count"] == 1
        assert baseline["disposition_counts"] == {
            "walled-permanent": 1, "capability": 1, "eligible": 1,
        }
        assert baseline["conversions_by_lane_t0"] == {
            "known_answer_mined": 0, "known_answer_escalation_asker": 0,
            "friction_efficiency": 0, "decision_staged": 0,
        }
        assert baseline["code_queue_capability_routed_t0"] == 0

    def test_write_persists_baseline_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr(m.gap_autofill, "load_open_gaps", lambda: [
            {"ts": "2026-08-15T00:00:00+00:00", "entity": "F3E",
             "question": "who is the Sprouts buyer?"},
        ])
        baseline_path = tmp_path / "flywheel-t0-baseline.json"
        monkeypatch.setattr(m, "_BASELINE_PATH", baseline_path)
        import sys
        old_argv = sys.argv
        sys.argv = ["triage_flywheel_pool_2026-07-30.py", "--write"]
        try:
            m.main()
        finally:
            sys.argv = old_argv
        assert baseline_path.exists()
        written = json.loads(baseline_path.read_text(encoding="utf-8"))
        assert written["eligible_open_count"] == 1
