"""Slice 4 (pipeline-integrity bundle, 2026-08-05) -- structural enforcement of the
P0/P1-gets-a-kickoff-at-approval rule.

Design (TOM 1fff) says a P0/P1 item gets a full kickoff prompt the moment it is
approved. Verify-first found THREE reasons that rule was unenforceable, and each has
its own class of test below:

 1. The rule lived only inside process_queue_action(ACTION_APPROVE), so an item
    seeded straight to status="APPROVED" never triggered it. That is exactly what
    happened to cq-f1236540b61e (P1, #info-for-cora intake) -- its `captured` event
    carries "status": "APPROVED" and no `approved` event exists at all, so the
    generator never ran and it read approved-and-unstaged for a week.
 2. The severity test was a bare `in ("P0","P1")` string check while every seed_item
    caller passes HIGH/MEDIUM/LOW -- so a HIGH item, P1 in every meaningful sense,
    was invisible to the rule.
 3. A generation FAILURE was silent: a falsy generate_kickoff_prompt left the plain
    "Queued (APPROVED)" message, so a P1 looked fully handled with no prompt.

The nightly monitor (check_priority_kickoffs) is the net for any path nobody
anticipated -- including seed_item, which stays deliberately non-generating.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import code_queue as cq

from test_code_queue import qenv  # noqa: F401  -- shared isolation fixture


HARRISON = "U0B2RM2JYJ1"


def _seed(severity="P1", status="PROPOSED", title="thing", **kw):
    return cq.seed_item(kind="bug", severity=severity, title=title, summary="s",
                        entity="FNDR", signal="explicit", status=status, **kw)


def _fake_generator(monkeypatch, path="/notes/prompt.md", meta=None):
    def _gen(items, slug=None, meta_out=None):
        if meta_out is not None and meta:
            meta_out.update(meta)
        return path
    monkeypatch.setattr(cq, "generate_kickoff_prompt", _gen)


# ── ensure_kickoff_staged: the single shared implementation ───────────────────

class TestEnsureKickoffStaged:
    def test_stages_and_records_the_event(self, qenv, monkeypatch):  # noqa: F811
        _fake_generator(monkeypatch)
        cid = _seed()
        outcome, detail = cq.ensure_kickoff_staged(cid)
        assert outcome == "staged" and detail == "/notes/prompt.md"
        rec = cq.get_item(cid)
        assert rec["status"] == "STAGED" and rec["prompt_path"] == "/notes/prompt.md"

    def test_already_staged_is_a_noop(self, qenv, monkeypatch):  # noqa: F811
        _fake_generator(monkeypatch)
        cid = _seed()
        cq.ensure_kickoff_staged(cid)
        calls = {"n": 0}

        def _gen(items, slug=None, meta_out=None):
            calls["n"] += 1
            return "/notes/second.md"
        monkeypatch.setattr(cq, "generate_kickoff_prompt", _gen)
        outcome, detail = cq.ensure_kickoff_staged(cid)
        assert outcome == "noop" and detail == "/notes/prompt.md"
        assert calls["n"] == 0  # no second Sonnet call

    def test_generator_returning_nothing_is_an_error_not_silence(self, qenv, monkeypatch):  # noqa: F811
        monkeypatch.setattr(cq, "generate_kickoff_prompt",
                            lambda items, slug=None, meta_out=None: "")
        cid = _seed()
        outcome, detail = cq.ensure_kickoff_staged(cid)
        assert outcome == "error" and "no file" in detail
        assert cq.get_item(cid)["status"] == "PROPOSED"  # no phantom staged event

    def test_generator_crash_is_an_error_not_a_raise(self, qenv, monkeypatch):  # noqa: F811
        def _boom(items, slug=None, meta_out=None):
            raise RuntimeError("sonnet down")
        monkeypatch.setattr(cq, "generate_kickoff_prompt", _boom)
        cid = _seed()
        outcome, detail = cq.ensure_kickoff_staged(cid)
        assert outcome == "error" and "crashed" in detail

    def test_reservation_race_is_an_error_not_silence(self, qenv, monkeypatch):  # noqa: F811
        """Losing the TOCTOU reservation must be REPORTED. Previously the approve
        branch skipped generation entirely and still said "Queued (APPROVED)"."""
        _fake_generator(monkeypatch)
        cid = _seed()
        assert cq._begin_staging([cid]) is True  # simulate an in-flight winner
        try:
            outcome, detail = cq.ensure_kickoff_staged(cid)
        finally:
            cq._end_staging([cid])
        assert outcome == "error" and "in flight" in detail

    def test_missing_item(self, qenv):  # noqa: F811
        assert cq.ensure_kickoff_staged("cq-nope")[0] == "error"

    def test_mis_homed_meta_is_preserved(self, qenv, monkeypatch):  # noqa: F811
        _fake_generator(monkeypatch, meta={"mis_homed": True})
        cid = _seed()
        cq.ensure_kickoff_staged(cid)
        events = [e for e in cq._read_jsonl(cq._EVENT_LEDGER)
                  if e.get("event") == "staged" and e.get("id") == cid]
        assert events and events[-1].get("mis_homed") is True


# ── defect 2: the HIGH vocabulary was invisible to the rule ──────────────────

class TestSeverityVocabulary:
    @pytest.mark.parametrize("severity", ["P0", "P1", "HIGH", "high", "CRITICAL"])
    def test_priority_class_stages_on_approve(self, qenv, monkeypatch, severity):  # noqa: F811
        _fake_generator(monkeypatch)
        cid = _seed(severity=severity)
        outcome, msg = cq.process_queue_action(cq.ACTION_APPROVE, cid, HARRISON)
        assert outcome == "approved"
        assert "prompt staged" in msg
        assert cq.get_item(cid)["status"] == "STAGED"

    @pytest.mark.parametrize("severity", ["P2", "P3", "MEDIUM", "LOW"])
    def test_non_priority_does_not_stage_on_approve(self, qenv, monkeypatch, severity):  # noqa: F811
        """Unchanged behavior for the bulk of the queue: P2/P3 wait for the Monday
        menu. Auto-staging everything would burn Sonnet on the whole backlog."""
        _fake_generator(monkeypatch)
        cid = _seed(severity=severity)
        outcome, msg = cq.process_queue_action(cq.ACTION_APPROVE, cid, HARRISON)
        assert outcome == "approved" and "prompt staged" not in msg
        assert cq.get_item(cid)["status"] == "APPROVED"

    def test_unknown_severity_does_not_stage(self, qenv, monkeypatch):  # noqa: F811
        """Fail-SAFE direction: an unrecognized vocabulary must not trigger a
        Sonnet call + Drive write."""
        _fake_generator(monkeypatch)
        cid = _seed(severity="SEV1")
        cq.process_queue_action(cq.ACTION_APPROVE, cid, HARRISON)
        assert cq.get_item(cid)["status"] == "APPROVED"


# ── defect 3: generation failure must be LOUD ────────────────────────────────

class TestLoudFailure:
    def test_approve_reports_generation_failure(self, qenv, monkeypatch):  # noqa: F811
        monkeypatch.setattr(cq, "generate_kickoff_prompt",
                            lambda items, slug=None, meta_out=None: None)
        cid = _seed(severity="P1")
        outcome, msg = cq.process_queue_action(cq.ACTION_APPROVE, cid, HARRISON)
        # The approve STANDS (its ledger event is already written) but the message
        # must not imply the kickoff exists.
        assert outcome == "approved"
        assert "did NOT generate" in msg and "retry" in msg.lower()
        assert cq.get_item(cid)["status"] == "APPROVED"

    def test_stage_action_shares_the_same_implementation(self, qenv, monkeypatch):  # noqa: F811
        _fake_generator(monkeypatch, path="/notes/staged.md")
        cid = _seed(severity="P3")
        outcome, msg = cq.process_queue_action(cq.ACTION_STAGE, cid, HARRISON)
        assert outcome == "staged" and "/notes/staged.md" in msg
        outcome2, msg2 = cq.process_queue_action(cq.ACTION_STAGE, cid, HARRISON)
        assert outcome2 == "noop" and "Already staged" in msg2

    def test_stage_action_reports_failure(self, qenv, monkeypatch):  # noqa: F811
        monkeypatch.setattr(cq, "generate_kickoff_prompt",
                            lambda items, slug=None, meta_out=None: "")
        cid = _seed(severity="P3")
        outcome, msg = cq.process_queue_action(cq.ACTION_STAGE, cid, HARRISON)
        assert outcome == "error" and "nothing staged" in msg

    def test_stage_action_inflight_is_a_noop_message(self, qenv, monkeypatch):  # noqa: F811
        _fake_generator(monkeypatch)
        cid = _seed(severity="P3")
        assert cq._begin_staging([cid]) is True
        try:
            outcome, msg = cq.process_queue_action(cq.ACTION_STAGE, cid, HARRISON)
        finally:
            cq._end_staging([cid])
        assert outcome == "noop" and "Already staging" in msg


# ── defect 1: the seed-at-APPROVED path ──────────────────────────────────────

class TestSeedAtApproved:
    def test_seed_at_approved_priority_warns_loudly(self, qenv, caplog):  # noqa: F811
        """The cq-f1236540b61e shape. seed_item stays non-generating by design (it
        is documented "no DM, no classifier"), but it can no longer be SILENT."""
        import logging
        with caplog.at_level(logging.WARNING, logger="cora.code_queue"):
            cid = _seed(severity="P1", status="APPROVED", title="intake gap")
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any(cid in w and "NO kickoff prompt" in w for w in warnings), warnings

    def test_seed_at_approved_non_priority_is_quiet(self, qenv, caplog):  # noqa: F811
        import logging
        with caplog.at_level(logging.WARNING, logger="cora.code_queue"):
            _seed(severity="P3", status="APPROVED", title="minor thing")
        assert not [r for r in caplog.records
                    if r.levelno >= logging.WARNING and "kickoff" in r.getMessage()]

    def test_seed_with_stage_now_generates(self, qenv, monkeypatch):  # noqa: F811
        _fake_generator(monkeypatch, path="/notes/seeded.md")
        cid = _seed(severity="HIGH", status="APPROVED", title="stage me",
                    stage_now=True)
        rec = cq.get_item(cid)
        assert rec["status"] == "STAGED" and rec["prompt_path"] == "/notes/seeded.md"

    def test_seed_stage_now_default_is_off(self, qenv, monkeypatch):  # noqa: F811
        """Default must not fire a Sonnet call from a data-migration helper."""
        calls = {"n": 0}

        def _gen(items, slug=None, meta_out=None):
            calls["n"] += 1
            return "/notes/x.md"
        monkeypatch.setattr(cq, "generate_kickoff_prompt", _gen)
        _seed(severity="P1", status="APPROVED", title="quiet seed")
        assert calls["n"] == 0


# ── the nightly monitor: the net for any unanticipated path ──────────────────

class TestPriorityKickoffMonitor:
    def _age(self, cid, hours):
        """Push the item's approval clock back by appending a dated event."""
        old = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        cq._append_event({"event": "approved", "ts": old, "id": cid})

    def test_flags_an_aged_approved_priority_item(self, qenv):  # noqa: F811
        cid = _seed(severity="P1", status="PROPOSED", title="dropped P1")
        self._age(cid, 48)
        offenders = cq.priority_items_missing_kickoff()
        ids = [o["id"] for o in offenders]
        assert cid in ids
        row = next(o for o in offenders if o["id"] == cid)
        assert row["severity"] == "P1" and row["age_hours"] >= 47

    def test_flags_a_high_severity_item(self, qenv):  # noqa: F811
        cid = _seed(severity="HIGH", status="PROPOSED", title="dropped HIGH")
        self._age(cid, 30)
        assert cid in [o["id"] for o in cq.priority_items_missing_kickoff()]

    def test_grace_window_protects_a_fresh_approve(self, qenv):  # noqa: F811
        cid = _seed(severity="P1", status="PROPOSED")
        cq._append_event({"event": "approved", "ts": cq._now_iso(), "id": cid})
        assert cid not in [o["id"] for o in cq.priority_items_missing_kickoff()]

    def test_age_is_measured_from_approval_not_capture(self, qenv):  # noqa: F811
        """An item captured weeks ago and approved TODAY must not flag -- the
        dropped-kickoff clock starts at approval."""
        cid = _seed(severity="P1", status="PROPOSED")
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        cq._append_event({"event": "captured", "ts": old, "id": cid})  # no-op on fold
        cq._append_event({"event": "approved", "ts": cq._now_iso(), "id": cid})
        rec = cq.get_item(cid)
        assert rec.get("approved_at")
        assert cid not in [o["id"] for o in cq.priority_items_missing_kickoff()]

    def test_staged_item_is_not_flagged(self, qenv, monkeypatch):  # noqa: F811
        _fake_generator(monkeypatch)
        cid = _seed(severity="P1", status="PROPOSED")
        self._age(cid, 72)
        cq.ensure_kickoff_staged(cid)
        assert cid not in [o["id"] for o in cq.priority_items_missing_kickoff()]

    def test_non_priority_and_terminal_states_not_flagged(self, qenv):  # noqa: F811
        low = _seed(severity="P3", status="PROPOSED", title="low")
        self._age(low, 72)
        shipped = _seed(severity="P1", status="PROPOSED", title="shipped one")
        self._age(shipped, 72)
        cq._append_event({"event": "shipped", "ts": cq._now_iso(), "id": shipped})
        ids = [o["id"] for o in cq.priority_items_missing_kickoff()]
        assert low not in ids and shipped not in ids

    def test_scan_never_raises(self, qenv, monkeypatch):  # noqa: F811
        monkeypatch.setattr(cq, "load_items",
                            lambda: (_ for _ in ()).throw(RuntimeError("ledger gone")))
        assert cq.priority_items_missing_kickoff() == []

    def test_newest_offender_ordering_is_oldest_first(self, qenv):  # noqa: F811
        a = _seed(severity="P1", status="PROPOSED", title="older")
        b = _seed(severity="P1", status="PROPOSED", title="newer")
        self._age(a, 100)
        self._age(b, 30)
        offenders = cq.priority_items_missing_kickoff()
        ids = [o["id"] for o in offenders if o["id"] in (a, b)]
        assert ids == [a, b]  # most-aged first, so the worst offender reads first


# ── the health-check surface ──────────────────────────────────────────────────

class TestHealthCheckLine:
    def _load(self):
        if str(_REPO_ROOT / "scripts") not in sys.path:
            sys.path.insert(0, str(_REPO_ROOT / "scripts"))
        import nightly_health_check as mod
        return mod

    def test_ok_when_no_offenders(self, monkeypatch):
        mod = self._load()
        monkeypatch.setattr(cq, "priority_items_missing_kickoff", lambda: [])
        r = mod.check_priority_kickoffs()
        assert r.status == "ok" and "No APPROVED" in r.detail

    def test_warns_and_names_the_items(self, monkeypatch):
        mod = self._load()
        monkeypatch.setattr(cq, "priority_items_missing_kickoff", lambda: [
            {"id": "cq-aaa", "severity": "P1", "entity": "FNDR",
             "title": "intake gap", "age_hours": 170.0},
        ])
        r = mod.check_priority_kickoffs()
        assert r.status == "warn"
        assert "cq-aaa" in r.detail and "P1" in r.detail
        # Actionable, not just a count -- Harrison needs the WHICH.
        assert "Stage" in r.detail

    def test_truncates_a_long_list(self, monkeypatch):
        mod = self._load()
        monkeypatch.setattr(cq, "priority_items_missing_kickoff", lambda: [
            {"id": f"cq-{i}", "severity": "P1", "entity": "FNDR",
             "title": "t", "age_hours": 100.0} for i in range(9)
        ])
        r = mod.check_priority_kickoffs()
        assert r.status == "warn" and "+4 more" in r.detail

    def test_broken_scan_warns_never_raises(self, monkeypatch):
        mod = self._load()
        monkeypatch.setattr(cq, "priority_items_missing_kickoff",
                            lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        r = mod.check_priority_kickoffs()
        assert r.status == "warn" and "Could not scan" in r.detail

    def test_check_is_wired_into_the_run(self):
        src = (_REPO_ROOT / "scripts" / "nightly_health_check.py").read_text(
            encoding="utf-8")
        assert "all_results.append(check_priority_kickoffs())" in src
