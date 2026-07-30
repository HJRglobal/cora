"""Tests for Fork 3c (Wave-1 flywheel-conversion calibration): the one-time purge
of raw LEX capability-ask text from logs/knowledge-gaps.jsonl.

Covers: dry-run touches nothing; --apply redacts only the 3 exact-matched target
rows (ts + entity=LEX + detector=code_queue_route), leaves every other row byte-
identical, marks the targets resolved (idempotently), and a second --apply run is
a no-op.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPT_PATH = _REPO / "scripts" / "purge_lex_capability_gap_rows_2026-07-30.py"

_spec = importlib.util.spec_from_file_location(
    "purge_lex_capability_gap_rows_wave1", _SCRIPT_PATH)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


_ROWS = [
    {"ts": "2026-07-28T20:35:19.157162+00:00", "entity": "LEX", "channel": "lex-hcbs",
     "user": "U1", "question": "will RSP be affected by the new HNT assessments?",
     "response_chars": 0, "gap": "capability/knowledge ask routed from code-queue classifier",
     "latency_ms": 0, "detector": "code_queue_route"},
    {"ts": "2026-07-30T04:58:49.176210+00:00", "entity": "LEX", "channel": "shaun-leadership",
     "user": "U2", "question": "do you have access to the Lexington how-to guide?",
     "response_chars": 0, "gap": "capability/knowledge ask routed from code-queue classifier",
     "latency_ms": 0, "detector": "code_queue_route"},
    {"ts": "2026-07-30T05:01:15.827097+00:00", "entity": "LEX", "channel": "shaun-leadership",
     "user": "U2", "question": "I just saved this how-to guide. Can you access it now?",
     "response_chars": 0, "gap": "capability/knowledge ask routed from code-queue classifier",
     "latency_ms": 0, "detector": "code_queue_route"},
    {"ts": "2026-07-30T05:24:01.404247+00:00", "entity": "LEX", "channel": "shaun-leadership",
     "user": "U2", "question": "I just shared the how-to guide with cora. Access now?",
     "response_chars": 0, "gap": "capability/knowledge ask routed from code-queue classifier",
     "latency_ms": 0, "detector": "code_queue_route"},
    {"ts": "2026-07-29T23:28:40.131221+00:00", "entity": "OSN", "channel": "osn-finance",
     "user": "U3", "question": "what are the monthly deposit amounts of each OSN store?",
     "response_chars": 498, "gap": "Reply was an unknown/no-data response",
     "latency_ms": 6672, "detector": "unknown_response"},
]


@pytest.fixture
def gaps_env(tmp_path, monkeypatch):
    gaps_path = tmp_path / "knowledge-gaps.jsonl"
    gaps_path.write_text(
        "\n".join(json.dumps(r) for r in _ROWS) + "\n", encoding="utf-8")
    resolved_path = tmp_path / "resolved-gaps.jsonl"
    monkeypatch.setenv("KNOWLEDGE_GAPS_LOG_PATH", str(gaps_path))
    monkeypatch.setenv("RESOLVED_GAPS_PATH", str(resolved_path))
    return {"gaps": gaps_path, "resolved": resolved_path}


def test_plan_selects_exact_3_target_rows(gaps_env):
    p = m.plan()
    assert len(p["targets"]) == 3
    target_ts = {t["ts"] for t in p["targets"]}
    assert target_ts == m._TARGET_TS
    # The LEX RSP/HNT world-question and the OSN row must NOT be selected --
    # only entity=LEX + detector=code_queue_route + an exact target ts.
    assert "2026-07-28T20:35:19.157162+00:00" not in target_ts
    assert "2026-07-29T23:28:40.131221+00:00" not in target_ts


def test_dry_run_touches_nothing(gaps_env):
    before = gaps_env["gaps"].read_text(encoding="utf-8")
    p = m.plan()
    assert len(p["targets"]) == 3
    after = gaps_env["gaps"].read_text(encoding="utf-8")
    assert before == after
    assert not gaps_env["resolved"].exists()


def test_apply_redacts_only_targets_and_marks_resolved(gaps_env):
    p = m.plan()
    n_redacted = m.apply_redaction(p)
    n_resolved = m.mark_resolved(p)
    assert n_redacted == 3
    assert n_resolved == 3

    lines = [json.loads(l) for l in
             gaps_env["gaps"].read_text(encoding="utf-8").splitlines()]
    by_ts = {r["ts"]: r for r in lines}
    for ts in m._TARGET_TS:
        assert by_ts[ts]["question"] == m._REDACTED_QUESTION
        # Every other field is untouched.
        assert by_ts[ts]["detector"] == "code_queue_route"
        assert by_ts[ts]["entity"] == "LEX"
    # Non-target rows are byte-identical.
    assert by_ts["2026-07-28T20:35:19.157162+00:00"]["question"] == \
        "will RSP be affected by the new HNT assessments?"
    assert by_ts["2026-07-29T23:28:40.131221+00:00"]["question"] == \
        "what are the monthly deposit amounts of each OSN store?"

    resolved_lines = [json.loads(l) for l in
                      gaps_env["resolved"].read_text(encoding="utf-8").splitlines()]
    resolved_ids = {r["id"] for r in resolved_lines}
    assert resolved_ids == m._TARGET_TS
    assert all(r["action"] == "capability_routed" for r in resolved_lines)


def test_apply_is_idempotent(gaps_env):
    p1 = m.plan()
    m.apply_redaction(p1)
    m.mark_resolved(p1)

    p2 = m.plan()
    assert p2["targets"] == []
    assert set(p2["already_redacted"]) == m._TARGET_TS
    n_redacted_2 = m.apply_redaction(p2)
    n_resolved_2 = m.mark_resolved(p2)
    assert n_redacted_2 == 0
    assert n_resolved_2 == 0

    resolved_lines = [json.loads(l) for l in
                      gaps_env["resolved"].read_text(encoding="utf-8").splitlines()]
    assert len(resolved_lines) == 3  # no duplicate resolved entries


# ─────────────────────────────────────────────────────────────────────────────
# D-051 adversarial-review remediation (2026-07-30, 2 review lenses independently
# flagged this): apply_redaction() must re-read the file FRESH immediately
# before writing, not rewrite from a stale plan() snapshot -- a stale snapshot
# would silently drop any gap the live bot process appended in between (the
# in-process knowledge_gaps._LOCK provides zero protection against this
# separate script process).
# ─────────────────────────────────────────────────────────────────────────────
def test_apply_redaction_preserves_a_row_appended_after_plan(gaps_env):
    """Simulates the live bot appending a new gap between plan() and the write."""
    p = m.plan()
    assert len(p["targets"]) == 3

    # The bot appends a fresh gap AFTER plan() was computed but BEFORE the
    # redaction write runs.
    concurrent_row = {
        "ts": "2026-07-30T06:00:00.000000+00:00", "entity": "F3E",
        "channel": "f3e-leadership", "user": "U9",
        "question": "who is the Sprouts buyer?", "response_chars": 200,
        "gap": "Sprouts buyer specifics", "latency_ms": 900,
        "detector": "unknown_response",
    }
    with gaps_env["gaps"].open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(concurrent_row) + "\n")

    n_redacted = m.apply_redaction(p)
    assert n_redacted == 3

    lines = [json.loads(l) for l in
             gaps_env["gaps"].read_text(encoding="utf-8").splitlines()]
    # The concurrently-appended row must survive, untouched.
    concurrent = [r for r in lines if r["ts"] == concurrent_row["ts"]]
    assert len(concurrent) == 1
    assert concurrent[0]["question"] == "who is the Sprouts buyer?"
    # And the 3 targets are still correctly redacted.
    by_ts = {r["ts"]: r for r in lines}
    for ts in m._TARGET_TS:
        assert by_ts[ts]["question"] == m._REDACTED_QUESTION
