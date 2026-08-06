"""Slice 5 (pipeline-integrity bundle, 2026-08-05) -- gap_routing_completeness_7d
counted an already-dispositioned gap class as "rotting".

The flywheel monitor read 52/57 routed with 5 rotting, degraded from 2 since 7/31 and
the worst since the metric shipped. Verify-first classified all of them (the count had
grown to 6 by 8/6). One class was a METRIC DEFECT, not a pipeline defect:

A gap written by code_queue._route_to_flywheel carries detector=="code_queue_route".
The classifier had ALREADY dispositioned it into the code-session queue, and its record
lives in the code-queue ledger -- which is neither .resolved-gaps.jsonl nor
gap_autofill_state.json, the only two places the metric looked. So the one class that
was demonstrably handled read as silently rotting: a false negative in the exact gauge
that exists to make rotting visible.

The other five are named in the completion report and left alone on purpose: 3 are
LEX-origin (LEX gaps can never escalate through the PHI wall, so they have no
disposition path -- the 8/13-locked fork) and 2 are DM personal-retrieval / QA-test
noise that need a "not-a-knowledge-gap" lane, which is a design decision, so it is
seeded as a queue item rather than guessed at.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import flywheel_metrics as fm


OLD = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
FRESH = datetime.now(timezone.utc).isoformat()


@pytest.fixture
def gapenv(tmp_path, monkeypatch):
    """Isolate every path the routing-completeness metric reads.

    _paths honors env overrides for the three files this metric uses (the suite-wide
    conftest sets them), so repo_root alone would NOT isolate them -- point the env
    vars at tmp too or the test reads live production data.
    """
    logs = tmp_path / "logs"
    state = tmp_path / "data" / "state"
    ka = tmp_path / "design" / "known-answers"
    for d in (logs, state, ka):
        d.mkdir(parents=True, exist_ok=True)
    (ka / ".resolved-gaps.jsonl").write_text("", encoding="utf-8")
    (state / "gap_autofill_state.json").write_text("{}", encoding="utf-8")
    (logs / "knowledge-gaps.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setenv("KNOWLEDGE_GAPS_LOG_PATH", str(logs / "knowledge-gaps.jsonl"))
    monkeypatch.setenv("GAP_AUTOFILL_STATE_PATH", str(state / "gap_autofill_state.json"))
    monkeypatch.setenv("RESOLVED_GAPS_PATH", str(ka / ".resolved-gaps.jsonl"))
    return tmp_path


def _write_gaps(root: Path, rows: list[dict]) -> None:
    p = root / "logs" / "knowledge-gaps.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _completeness(root: Path) -> dict:
    metrics = fm.collect(repo_root=root)
    return metrics["gap_routing_completeness_7d"]


# ── the detector predicate ───────────────────────────────────────────────────

def test_has_own_disposition_keys_on_the_detector():
    assert fm._has_own_disposition({"detector": "code_queue_route"}) is True
    assert fm._has_own_disposition({"detector": "CODE_QUEUE_ROUTE"}) is True
    assert fm._has_own_disposition({"detector": "unknown_response"}) is False
    assert fm._has_own_disposition({"detector": "llm_sentinel"}) is False
    assert fm._has_own_disposition({"detector": "kb_miss"}) is False
    assert fm._has_own_disposition({}) is False


def test_predicate_does_not_key_on_the_gap_message():
    """Keyed on the DETECTOR -- a structural fact of how the row was written -- not
    on the internal string "capability/knowledge ask routed from code-queue
    classifier". Messages get reworded; the detector field does not."""
    assert fm._has_own_disposition({
        "detector": "unknown_response",
        "gap": "capability/knowledge ask routed from code-queue classifier",
    }) is False


# ── the metric ───────────────────────────────────────────────────────────────

def test_code_queue_route_row_counts_as_routed(gapenv):
    _write_gaps(gapenv, [
        {"ts": OLD, "entity": "LEX", "detector": "code_queue_route",
         "question": "will RSP be affected by the new HNT assessments?",
         "gap": "capability/knowledge ask routed from code-queue classifier"},
    ])
    c = _completeness(gapenv)
    assert c == {"total_over_7d": 1, "routed": 1, "rotting": 0}


def test_a_genuinely_undispositioned_row_still_rots(gapenv):
    """The fix must not make the gauge blind -- that would be worse than the false
    negative it corrects."""
    _write_gaps(gapenv, [
        {"ts": OLD, "entity": "FNDR", "detector": "unknown_response",
         "question": "what's the Anaheim address?", "gap": "not in KB"},
    ])
    c = _completeness(gapenv)
    assert c == {"total_over_7d": 1, "routed": 0, "rotting": 1}


def test_mixed_set_tallies_correctly(gapenv):
    _write_gaps(gapenv, [
        {"ts": OLD, "detector": "code_queue_route", "entity": "F3E"},
        {"ts": OLD, "detector": "unknown_response", "entity": "FNDR"},
        {"ts": OLD, "detector": "llm_sentinel", "entity": "LEX"},
        {"ts": FRESH, "detector": "unknown_response", "entity": "FNDR"},  # inside 7d
    ])
    c = _completeness(gapenv)
    assert c["total_over_7d"] == 3
    assert c["routed"] == 1
    assert c["rotting"] == 2


def test_resolved_and_state_ids_still_count(gapenv):
    """The two pre-existing disposition sources must keep working."""
    _write_gaps(gapenv, [
        {"ts": OLD, "detector": "unknown_response", "entity": "FNDR"},
        {"ts": "2026-01-01T00:00:00+00:00", "detector": "unknown_response",
         "entity": "FNDR"},
    ])
    (gapenv / "design" / "known-answers" / ".resolved-gaps.jsonl").write_text(
        json.dumps({"id": OLD}) + "\n", encoding="utf-8")
    (gapenv / "data" / "state" / "gap_autofill_state.json").write_text(
        json.dumps({"2026-01-01T00:00:00+00:00": {"state": "proposed"}}),
        encoding="utf-8")
    c = _completeness(gapenv)
    assert c == {"total_over_7d": 2, "routed": 2, "rotting": 0}


def test_slice3_ineligible_state_gives_a_disposition(gapenv):
    """Slice 3 records state="ineligible" for a gap that can never become a fact
    (QA noise, capability ask, ephemeral). Those keys land in
    gap_autofill_state.json, so the metric picks them up for free -- which is why
    one of the two no-lane rows resolves itself once S3 runs live."""
    _write_gaps(gapenv, [
        {"ts": OLD, "entity": "FNDR", "channel": "dm",
         "detector": "unknown_response", "question": "what's my test locker code?"},
    ])
    (gapenv / "data" / "state" / "gap_autofill_state.json").write_text(
        json.dumps({OLD: {"state": "ineligible", "reason": "qa"}}), encoding="utf-8")
    assert _completeness(gapenv)["rotting"] == 0


def test_metric_is_fail_soft(gapenv):
    """A missing gaps log must not raise and must not invent offenders. Asserted as
    ONE outcome, not "either" (lens-6 LOW: `is None or == 0` could not fail)."""
    (gapenv / "logs" / "knowledge-gaps.jsonl").unlink()
    c = _completeness(gapenv)
    assert c == {"total_over_7d": 0, "routed": 0, "rotting": 0}


# ── the diagnostic script ────────────────────────────────────────────────────

def test_diagnostic_classifies_every_known_class():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "diag_routing", _REPO_ROOT / "scripts" / "diagnose_gap_routing_completeness.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "metric-artifact" in mod._classify({"detector": "code_queue_route"})
    assert "LEX-origin" in mod._classify(
        {"detector": "llm_sentinel", "entity": "LEX-LLC"})
    assert "no-lane" in mod._classify(
        {"detector": "unknown_response", "entity": "FNDR", "channel": "dm"})
    assert "no-lane" in mod._classify(
        {"detector": "unknown_response", "entity": "HJRG", "private_source": True})
    # An unrecognized shape must SAY so rather than being silently bucketed --
    # the monitor DMs a count, so a new class has to be visible as new.
    assert "UNEXPLAINED" in mod._classify(
        {"detector": "unknown_response", "entity": "F3E", "channel": "f3e-leadership"})
