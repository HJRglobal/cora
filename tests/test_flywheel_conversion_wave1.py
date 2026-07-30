"""Tests for the 2026-07-30 Wave-1 flywheel-conversion-calibration kickoff:
Fork 3a (capability-ask routing at the shared log_gap chokepoint) + Fork 3b
(code-queue PHI parity-raise + LEX title/summary/fix_sketch redaction).

Covers: is_capability_ask true/false positives (incl. the RepRally + how-to-guide
asks from the live pool, and the RSP/HNT world-question negative); routing through
all 3 log_gap callers (knowledge_gaps.log_gap direct, gap_detection.maybe_log_gap,
code_queue._route_to_flywheel); fail-closed fallback when code_queue is off;
is_any_phi parity at code_queue's checkpoints; LEX redaction at capture, render,
and the MCP surface (which reuses render_backlog_text).
"""

from __future__ import annotations

import json

import pytest

from cora import code_queue as cq
from cora import gap_detection
from cora import knowledge_gaps as kg
from cora import phi_guard


@pytest.fixture
def isolate_backlog_writes(tmp_path, monkeypatch):
    """Belt-and-suspenders isolation for any test that drives cq._capture() to a
    successful (non-dropped) new-item persist: _capture unconditionally calls
    _render_backlog_safe() -> render_backlog() -> drive_io.write_text_atomic(
    backlog_path(), ...) afterward. Without this, a test that only redirects the
    event ledger (not FOUNDER_OS_ROOT / drive_io) writes to the REAL live
    G:\\...\\code-session-backlog.md -- exactly the 2026-07-30 incident (item
    cq-96fdd1850605, a LEX-redacted TEST title). code_queue.render_backlog()'s
    own leak guard (_backlog_write_would_leak) is defense-in-depth for THIS
    exact mismatch; tests still isolate properly rather than relying on it."""
    monkeypatch.setenv("FOUNDER_OS_ROOT", str(tmp_path / "founder-os"))

    def _plain_write(path, text, **kw):
        from pathlib import Path as _P
        _P(path).parent.mkdir(parents=True, exist_ok=True)
        _P(path).write_text(text, encoding="utf-8")

    monkeypatch.setattr(cq.drive_io, "write_text_atomic", _plain_write)


# ─────────────────────────────────────────────────────────────────────────────
# is_capability_ask -- deterministic classifier
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", [
    "can you check our RepRally wholesale listings? *Sent using* <@U0B3V5RHT3P>",
    'do you have access to the Lexington "how to guide" that Jen Mortensen sent my email?',
    'I just save this "how to guide" to my Google Drive. Can you access the contents of the document now?',
    'I just shared the Lexington "how to guide" with your cora@hjrglobal.com. Do you have access to its content now?',
    "are you able to connect to our inventory system?",
    "can you reach the shared drive folder?",
])
def test_is_capability_ask_true_positives(text):
    assert kg.is_capability_ask(text) is True


@pytest.mark.parametrize("text", [
    "will RSP be affected by the new HNT assessments?",
    "what are the monthly deposit amounts of each OSN store?",
    "who supplies our office coffee?",
    "what's my test locker code?",
    "pull from QBO and just use total cash expenses (no depreciation expenses) from 2022 through 2026",
    "domain is Lexington. LLC is the DDD services provider/contract and RSP, HAH, and ATC "
    "are all DDD services that Lexington (LLC) provides. What are you missing?",
])
def test_is_capability_ask_false_on_world_questions(text):
    assert kg.is_capability_ask(text) is False


def test_is_capability_ask_empty_and_none():
    assert kg.is_capability_ask("") is False
    assert kg.is_capability_ask(None) is False


# ─────────────────────────────────────────────────────────────────────────────
# knowledge_gaps.log_gap -- the shared chokepoint
# ─────────────────────────────────────────────────────────────────────────────
def test_log_gap_routes_capability_ask_to_code_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_GAPS_LOG_PATH", str(tmp_path / "gaps.jsonl"))
    monkeypatch.setenv("CORA_CODE_QUEUE", "log")
    monkeypatch.setattr(cq, "_EVENT_LEDGER", tmp_path / "cq-events.jsonl")
    monkeypatch.setattr(cq, "_FINGERPRINT_LEDGER", tmp_path / "cq-fp.jsonl")
    monkeypatch.setattr(cq, "_SYNC", True)

    kg.log_gap(
        entity="FNDR", channel="cora-build", user="U1",
        question="can you check our RepRally wholesale listings?",
        response_chars=0, gap="capability ask", latency_ms=0,
        detector="unknown_response",
    )

    gaps_path = tmp_path / "gaps.jsonl"
    assert not gaps_path.exists() or gaps_path.read_text(encoding="utf-8").strip() == ""

    events = [json.loads(l) for l in
              (tmp_path / "cq-events.jsonl").read_text(encoding="utf-8").splitlines()]
    captured = [e for e in events if e.get("event") == "captured"]
    assert len(captured) == 1
    assert captured[0]["kind"] == "feature"
    assert captured[0]["signal"] == "capability"
    assert "RepRally" in captured[0]["title"]


def test_log_gap_world_question_still_logs_normally(tmp_path, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_GAPS_LOG_PATH", str(tmp_path / "gaps.jsonl"))
    monkeypatch.setenv("CORA_CODE_QUEUE", "log")
    monkeypatch.setattr(cq, "_EVENT_LEDGER", tmp_path / "cq-events.jsonl")
    monkeypatch.setattr(cq, "_SYNC", True)

    kg.log_gap(
        entity="LEX", channel="lex-hcbs", user="U2",
        question="will RSP be affected by the new HNT assessments?",
        response_chars=1000, gap="DDD service definitions", latency_ms=100,
        detector="llm_sentinel",
    )

    gaps_path = tmp_path / "gaps.jsonl"
    lines = gaps_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["question"].startswith("will RSP be affected")
    assert not (tmp_path / "cq-events.jsonl").exists()


def test_log_gap_capability_ask_falls_back_when_code_queue_off(tmp_path, monkeypatch):
    """A capability ask must never simply vanish: if the code-queue is off, it
    falls back to normal gap logging rather than being silently dropped."""
    monkeypatch.setenv("KNOWLEDGE_GAPS_LOG_PATH", str(tmp_path / "gaps.jsonl"))
    monkeypatch.setenv("CORA_CODE_QUEUE", "off")

    kg.log_gap(
        entity="FNDR", channel="cora-build", user="U1",
        question="can you check our RepRally wholesale listings?",
        response_chars=0, gap="capability ask", latency_ms=0,
    )

    lines = (tmp_path / "gaps.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert "RepRally" in json.loads(lines[0])["question"]


# ─────────────────────────────────────────────────────────────────────────────
# All 3 log_gap call sites
# ─────────────────────────────────────────────────────────────────────────────
def test_gap_detection_routes_capability_ask(tmp_path, monkeypatch):
    """Site 2: gap_detection.maybe_log_gap (unknown_response detector)."""
    monkeypatch.setenv("KNOWLEDGE_GAPS_LOG_PATH", str(tmp_path / "gaps.jsonl"))
    monkeypatch.setenv("GAP_DETECTION_STATE_PATH", str(tmp_path / "gd-state.json"))
    monkeypatch.setenv("CORA_CODE_QUEUE", "log")
    monkeypatch.setattr(cq, "_EVENT_LEDGER", tmp_path / "cq-events.jsonl")
    monkeypatch.setattr(cq, "_SYNC", True)

    detector = gap_detection.maybe_log_gap(
        entity="F3E", channel="f3e-leadership", user="U3",
        question="are you able to connect to our inventory system?",
        response_text=gap_detection.UNKNOWN_RESPONSE_TEXT,
        latency_ms=500,
    )
    assert detector == "unknown_response"
    gaps_path = tmp_path / "gaps.jsonl"
    assert not gaps_path.exists() or gaps_path.read_text(encoding="utf-8").strip() == ""
    events = [json.loads(l) for l in
              (tmp_path / "cq-events.jsonl").read_text(encoding="utf-8").splitlines()]
    captured = [e for e in events if e.get("event") == "captured"]
    assert len(captured) == 1
    assert captured[0]["signal"] == "capability"


def test_route_to_flywheel_lex_entity_skipped(tmp_path, monkeypatch):
    """Site 3: code_queue._route_to_flywheel -- LEX must be skipped outright,
    never a side-door into the gap log around gap_detection's own LEX-skip."""
    monkeypatch.setenv("KNOWLEDGE_GAPS_LOG_PATH", str(tmp_path / "gaps.jsonl"))
    cq._route_to_flywheel("some LEX question", "LEX", "lex-hcbs", "U4")
    gaps_path = tmp_path / "gaps.jsonl"
    assert not gaps_path.exists() or gaps_path.read_text(encoding="utf-8").strip() == ""


def test_route_to_flywheel_phi_dropped(tmp_path, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_GAPS_LOG_PATH", str(tmp_path / "gaps.jsonl"))
    cq._route_to_flywheel("what is the patient's diagnosis code icd-10 M54.5",
                          "F3E", "f3e-leadership", "U5")
    gaps_path = tmp_path / "gaps.jsonl"
    assert not gaps_path.exists() or gaps_path.read_text(encoding="utf-8").strip() == ""


def test_route_to_flywheel_normal_question_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_GAPS_LOG_PATH", str(tmp_path / "gaps.jsonl"))
    monkeypatch.setenv("CORA_CODE_QUEUE", "off")
    cq._route_to_flywheel("what are the monthly deposit amounts of each OSN store?",
                          "OSN", "osn-finance", "U6")
    lines = (tmp_path / "gaps.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["detector"] == "code_queue_route"


# ─────────────────────────────────────────────────────────────────────────────
# Fork 3b -- PHI parity-raise (is_any_phi at every code_queue checkpoint)
# ─────────────────────────────────────────────────────────────────────────────
# Trips is_lex_billing_status_phi but NOT is_phi_risk (no clinical keyword) --
# exactly the class the parity-raise closes.
_LEX_BILLING_TEXT = "can you access Marcus's service authorization status?"


def test_is_any_phi_catches_billing_status_without_clinical_keyword():
    assert phi_guard.is_phi_risk(_LEX_BILLING_TEXT) is False
    assert phi_guard.is_any_phi(_LEX_BILLING_TEXT) is True


def test_capture_drops_billing_status_phi_via_parity_raise(
        tmp_path, monkeypatch, isolate_backlog_writes):
    monkeypatch.setattr(cq, "_EVENT_LEDGER", tmp_path / "cq-events.jsonl")
    monkeypatch.setattr(cq, "_FINGERPRINT_LEDGER", tmp_path / "cq-fp.jsonl")
    rec = {
        "kind": "feature", "severity": "P3", "title": _LEX_BILLING_TEXT[:120],
        "summary": _LEX_BILLING_TEXT[:200], "entity": "LEX", "signal": "capability",
        "representative": _LEX_BILLING_TEXT,
        "evidence": [{"channel_id": "", "ts": "", "note": ""}],
        "reporter": "U7",
    }
    result = cq._capture(rec)
    assert result is None
    assert not (tmp_path / "cq-events.jsonl").exists()


# ─────────────────────────────────────────────────────────────────────────────
# Fork 3b -- LEX title/summary/fix_sketch redaction at capture + render + MCP
# ─────────────────────────────────────────────────────────────────────────────
def test_capture_redacts_lex_title_summary_fix_sketch(
        tmp_path, monkeypatch, isolate_backlog_writes):
    monkeypatch.setattr(cq, "_EVENT_LEDGER", tmp_path / "cq-events.jsonl")
    monkeypatch.setattr(cq, "_FINGERPRINT_LEDGER", tmp_path / "cq-fp.jsonl")
    rec = {
        "kind": "feature", "severity": "P3",
        "title": 'do you have access to the Lexington "how to guide"?',
        "summary": 'do you have access to the Lexington "how to guide"?',
        "fix_sketch": "build a Google Drive connector for LEX how-to guides",
        "entity": "LEX", "signal": "capability",
        "representative": 'do you have access to the Lexington "how to guide"?',
        "evidence": [{"channel_id": "", "ts": "", "note": ""}],
        "reporter": "U8",
    }
    cq_id = cq._capture(rec)
    assert cq_id is not None
    item = cq.get_item(cq_id)
    assert item["title"] == cq._LEX_REDACTED_TITLE
    assert item["summary"] == ""
    assert item["fix_sketch"] == ""
    assert "how to guide" not in json.dumps(item)


def test_render_backlog_egress_recheck_redacts_legacy_raw_lex_row(tmp_path, monkeypatch):
    """Defense-in-depth: even a record that somehow persisted RAW LEX title text
    (a legacy row from before this fix, or a future write-path regression) must
    never surface it through render_backlog_text (and therefore the MCP surface,
    which calls render_backlog_text directly)."""
    monkeypatch.setattr(cq, "_EVENT_LEDGER", tmp_path / "cq-events.jsonl")
    legacy_event = {
        "event": "captured", "id": "cq-legacy001", "ts": "2026-07-01T00:00:00+00:00",
        "status": "PROPOSED", "kind": "feature", "severity": "P3",
        "title": "raw LEX client text that should never render",
        "summary": "raw LEX client text that should never render",
        "entity": "LEX", "signal": "capability", "count": 1,
    }
    (tmp_path / "cq-events.jsonl").write_text(
        json.dumps(legacy_event) + "\n", encoding="utf-8")
    text = cq.render_backlog_text()
    assert "raw LEX client text" not in text
    assert cq._LEX_REDACTED_TITLE in text


def test_seed_item_redacts_lex_and_uses_any_phi(tmp_path, monkeypatch):
    monkeypatch.setattr(cq, "_EVENT_LEDGER", tmp_path / "cq-events.jsonl")
    monkeypatch.setattr(cq, "_FINGERPRINT_LEDGER", tmp_path / "cq-fp.jsonl")
    cq_id = cq.seed_item(
        kind="feature", severity="P3", title="LEX how-to guide access ask",
        summary="LEX how-to guide access ask", entity="LEX",
        signal="capability", status="PROPOSED",
    )
    assert cq_id is not None
    item = cq.get_item(cq_id)
    assert item["title"] == cq._LEX_REDACTED_TITLE
    assert item["summary"] == ""

    # PHI parity: a seed tripping is_lex_billing_status_phi only must be refused.
    refused = cq.seed_item(
        kind="feature", severity="P3", title=_LEX_BILLING_TEXT,
        summary=_LEX_BILLING_TEXT, entity="LEX", signal="capability", status="PROPOSED",
    )
    assert refused is None
