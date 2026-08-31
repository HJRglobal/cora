"""WS-2 flywheel telemetry (cora.flywheel_metrics + both health-surface wirings)."""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import cora.flywheel_metrics as fm

_SCRIPTS = str(Path(__file__).resolve().parents[1] / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)


NOW = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


def _slack_ts(dt: datetime) -> str:
    return f"{dt.timestamp():.6f}"


def _write_jsonl(path: Path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """A synthetic repo root with ledger/gaps/state/shadow fixtures."""
    monkeypatch.delenv("KNOWLEDGE_GAPS_LOG_PATH", raising=False)
    monkeypatch.delenv("GAP_DETECTION_STATE_PATH", raising=False)
    monkeypatch.delenv("GAP_AUTOFILL_STATE_PATH", raising=False)
    monkeypatch.delenv("CORA_GRADUATED_SHADOW_DIR", raising=False)

    recent = NOW - timedelta(days=2)
    old = NOW - timedelta(days=20)
    ledger = [
        # knowledge item DM'd 2d ago -> counts
        {"update_id": "gapfill-1", "update_type": "known_answer",
         "state": "PENDING", "proposed_at": recent.isoformat(),
         "dm_message_ts": _slack_ts(recent)},
        # info-for-cora generic DM'd 2d ago -> counts as knowledge
        {"update_id": "note-1", "update_type": "generic",
         "payload": {"source": "info-for-cora"},
         "state": "PENDING", "proposed_at": recent.isoformat(),
         "dm_message_ts": _slack_ts(recent)},
        # drive generic (no source) DM'd 2d ago -> NOT knowledge
        {"update_id": "drive_fact:aa", "update_type": "generic",
         "payload": {"fact_type": "person"},
         "state": "PENDING", "proposed_at": recent.isoformat(),
         "dm_message_ts": _slack_ts(recent)},
        # knowledge item DM'd 20d ago -> outside window
        {"update_id": "gapfill-2", "update_type": "known_answer",
         "state": "DISMISSED", "proposed_at": old.isoformat(),
         "resolved_at": old.isoformat(),
         "dm_message_ts": _slack_ts(old)},
        # operational, routed within window
        {"update_id": "drive_fact:bb", "update_type": "asana_task",
         "state": "DISMISSED", "proposed_at": recent.isoformat(),
         "resolved_at": recent.isoformat(),
         "resolved_reason": "routed_to_owner:U123",
         "dm_message_ts": ""},
        # operational, expired_unrouted within window
        {"update_id": "drive_fact:cc", "update_type": "hubspot_note",
         "state": "DISMISSED", "proposed_at": old.isoformat(),
         "resolved_at": recent.isoformat(),
         "resolved_reason": "expired_unrouted",
         "dm_message_ts": ""},
        # plain PENDING operational
        {"update_id": "drive_fact:dd", "update_type": "decision_capture",
         "state": "PENDING", "proposed_at": old.isoformat(),
         "dm_message_ts": ""},
    ]
    _write_jsonl(tmp_path / "data" / "cora-proposed-memory-updates.jsonl", ledger)
    # Archived knowledge DM within the window must still count (rotation moves
    # resolved rows out of the live file after 3 days).
    _write_jsonl(tmp_path / "data" / "cora-proposed-memory-updates.archive.jsonl", [
        {"update_id": "gapfill-3", "update_type": "efficiency",
         "state": "DISMISSED", "proposed_at": recent.isoformat(),
         "resolved_at": recent.isoformat(),
         "dm_message_ts": _slack_ts(recent)},
    ])
    _write_jsonl(tmp_path / "logs" / "knowledge-gaps.jsonl", [
        {"ts": (NOW - timedelta(days=16)).isoformat(), "entity": "FNDR",
         "question": "old", "gap": "g"},
        {"ts": (NOW - timedelta(days=10)).isoformat(), "entity": "F3E",
         "question": "newest", "gap": "g", "detector": "kb_miss"},
    ])
    (tmp_path / "data" / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "state" / "gap_autofill_state.json").write_text(json.dumps({
        "t1": {"state": "proposed", "at": recent.isoformat()},
        "t2": {"state": "proposed", "at": old.isoformat()},
        "t3": {"state": "asked", "at": recent.isoformat()},
    }), encoding="utf-8")
    _write_jsonl(tmp_path / "logs" / "graduated-trust-shadow-2026-06-30.jsonl",
                 [{"kind": "shadow_decision"}, {"kind": "shadow_reaction"}])
    _write_jsonl(tmp_path / "logs" / "graduated-trust-shadow-2026-07-01.jsonl",
                 [{"kind": "shadow_decision"}])
    return tmp_path


class TestCollect:
    def test_knowledge_dms_7d_counts_knowledge_incl_archive(self, repo):
        m = fm.collect(now=NOW, repo_root=repo)
        # gapfill-1 + note-1 (live) + gapfill-3 (archive); drive generic and
        # the 20d-old one excluded.
        assert m["knowledge_dms_7d"] == 3

    def test_pending_counts_live_only(self, repo):
        m = fm.collect(now=NOW, repo_root=repo)
        assert m["pending_total"] == 4

    def test_producer_vs_drain(self, repo):
        m = fm.collect(now=NOW, repo_root=repo)
        assert m["proposed_7d"] == 5
        assert m["resolved_7d"] == 3
        assert m["routed_to_owner_7d"] == 1
        assert m["expired_unrouted_7d"] == 1

    def test_gap_log_age_and_detectors(self, repo):
        m = fm.collect(now=NOW, repo_root=repo)
        assert m["gaps_total"] == 2
        assert m["gaps_last_entry_age_days"] == pytest.approx(10.0, abs=0.1)
        assert m["gaps_by_detector"] == {"llm_sentinel": 1, "kb_miss": 1}

    def test_gap_autofill_proposed_7d(self, repo):
        m = fm.collect(now=NOW, repo_root=repo)
        assert m["gap_autofill_proposed_7d"] == 1

    def test_shadow_records_and_days(self, repo):
        m = fm.collect(now=NOW, repo_root=repo)
        assert m["shadow_records"] == 3
        assert m["shadow_days"] == 2

    def test_baseline_written_only_when_asked(self, repo):
        baseline = repo / "data" / "health-flywheel-baseline.json"
        fm.collect(now=NOW, repo_root=repo, update_baseline=False)
        assert not baseline.exists()
        fm.collect(now=NOW, repo_root=repo, update_baseline=True)
        hist = json.loads(baseline.read_text(encoding="utf-8"))["history"]
        assert hist == {"2026-07-01": 4}

    def test_growth_from_baseline_history(self, repo):
        baseline = repo / "data" / "health-flywheel-baseline.json"
        baseline.parent.mkdir(parents=True, exist_ok=True)
        week_ago = (NOW - timedelta(days=6)).strftime("%Y-%m-%d")
        baseline.write_text(json.dumps({"history": {week_ago: 1}}),
                            encoding="utf-8")
        m = fm.collect(now=NOW, repo_root=repo)
        assert m["pending_growth_7d"] == 3  # 4 pending now vs 1 a week ago

    def test_non_dict_ledger_line_does_not_kill_gauges(self, repo):
        # Adversarial review LOW: a valid-JSON non-dict line ('null', '[]')
        # must be skipped, not abort the whole ledger scan.
        ledger = repo / "data" / "cora-proposed-memory-updates.jsonl"
        with ledger.open("a", encoding="utf-8") as fh:
            fh.write("null\n[]\n\"stray\"\n")
        m = fm.collect(now=NOW, repo_root=repo)
        assert "ledger_error" not in m
        assert m["knowledge_dms_7d"] == 3

    def test_rotation_crash_duplicate_not_double_counted(self, repo):
        # Adversarial review LOW: the same row in live AND archive (rotation
        # crash window) must count once.
        recent = NOW - timedelta(days=2)
        dup = {"update_id": "gapfill-1", "update_type": "known_answer",
               "state": "DISMISSED", "proposed_at": recent.isoformat(),
               "resolved_at": recent.isoformat(),
               "dm_message_ts": _slack_ts(recent)}
        with (repo / "data" / "cora-proposed-memory-updates.archive.jsonl").open(
                "a", encoding="utf-8") as fh:
            fh.write(json.dumps(dup) + "\n")
        m = fm.collect(now=NOW, repo_root=repo)
        assert m["knowledge_dms_7d"] == 3  # unchanged despite the dup row

    def test_missing_everything_degrades(self, tmp_path, monkeypatch):
        monkeypatch.delenv("KNOWLEDGE_GAPS_LOG_PATH", raising=False)
        monkeypatch.delenv("GAP_DETECTION_STATE_PATH", raising=False)
        monkeypatch.delenv("GAP_AUTOFILL_STATE_PATH", raising=False)
        monkeypatch.delenv("CORA_GRADUATED_SHADOW_DIR", raising=False)
        m = fm.collect(now=NOW, repo_root=tmp_path)
        assert m["available"] is True
        assert m["pending_total"] == 0
        assert m["knowledge_dms_7d"] == 0
        assert m["gaps_last_entry_age_days"] is None
        assert m["shadow_records"] == 0


class TestEvaluate:
    def test_starvation_warns(self):
        alarms = fm.evaluate({"knowledge_dms_7d": 0,
                              "gaps_last_entry_age_days": 25.0,  # > the 21d threshold
                              "pending_total": 100})
        msgs = [m for _s, m in alarms]
        assert any("0 knowledge items" in m for m in msgs)
        assert any("no new knowledge-gaps" in m and "25d" in m for m in msgs)

    def test_normal_gap_lull_does_not_warn(self):
        """Slice 6 recalibration: a routine sporadic-intake lull (16d, between the old
        7d and the new 21d threshold) must NOT fire the stale-gap alarm."""
        alarms = fm.evaluate({"knowledge_dms_7d": 5,
                              "gaps_last_entry_age_days": 16.0,
                              "pending_total": 100})
        assert not any("knowledge-gaps" in m for _s, m in alarms)

    def test_healthy_is_quiet(self):
        alarms = fm.evaluate({"knowledge_dms_7d": 5,
                              "gaps_last_entry_age_days": 1.0,
                              "pending_total": 500,
                              "pending_growth_7d": 20})
        assert alarms == []

    def test_size_and_growth_warn(self):
        alarms = fm.evaluate({"knowledge_dms_7d": 5,
                              "gaps_last_entry_age_days": 1.0,
                              "pending_total": fm.WARN_PENDING_SIZE + 1,
                              "pending_growth_7d": fm.WARN_PENDING_GROWTH_7D + 1})
        assert len(alarms) == 2

    def test_never_critical(self):
        alarms = fm.evaluate({"knowledge_dms_7d": 0,
                              "gaps_last_entry_age_days": 99.0,
                              "pending_total": 99_999,
                              "pending_growth_7d": 99_999})
        assert all(sev == "warn" for sev, _m in alarms)

    def test_empty_gap_log_warns(self):
        alarms = fm.evaluate({"knowledge_dms_7d": 5,
                              "gaps_last_entry_age_days": None,
                              "pending_total": 10})
        assert any("no entries" in m for _s, m in alarms)


class TestDriftPins:
    def test_knowledge_types_pinned_to_run_knowledge_review(self):
        import run_knowledge_review as rkr
        assert fm._KNOWLEDGE_TYPES == rkr._KNOWLEDGE_TYPES

    def test_is_knowledge_item_parity(self):
        import run_knowledge_review as rkr
        cases = [
            {"update_type": "known_answer"},
            {"update_type": "efficiency"},
            {"update_type": "generic", "payload": {"source": "info-for-cora"}},
            {"update_type": "generic", "payload": {"fact_type": "person"}},
            {"update_type": "asana_task"},
            {"update_type": "hubspot_note", "payload": None},
        ]
        for case in cases:
            assert fm.is_knowledge_item(case) == rkr._is_knowledge_item(case), case


class TestHealthSurfaceWiring:
    def test_nightly_check_flywheel_warn_only(self, repo, monkeypatch):
        import nightly_health_check as nhc
        monkeypatch.setattr(fm, "_REPO_ROOT", repo)
        results = nhc.check_flywheel()
        assert results
        assert all(r.status in ("ok", "warn") for r in results)
        # The fixture repo has knowledge DMs, so no starvation warn; the
        # summary line always renders.
        assert any(r.name == "Flywheel throughput" for r in results)

    def test_nightly_check_flywheel_fail_soft(self, monkeypatch):
        import nightly_health_check as nhc
        monkeypatch.setattr(fm, "collect",
                            lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
        results = nhc.check_flywheel()
        assert len(results) == 1
        assert results[0].status == "warn"

    def test_report_section_fail_soft(self, monkeypatch):
        import cora_health_report as chr_mod
        monkeypatch.setattr(fm, "collect",
                            lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
        section = chr_mod.flywheel_metrics_section()
        assert section["available"] is False

    def test_report_alarms_include_flywheel(self, monkeypatch):
        import cora_health_report as chr_mod
        report = {
            "kb_corpus": {"available": False},
            "state": {"jsonl_ledgers": {}, "logs_dir_bytes": 0},
            "scheduled_tasks": {"available": False},
            "flywheel": {"available": True,
                         "alarm_lines": ["0 knowledge items DM'd to Harrison in 7d"]},
        }
        alarms = chr_mod.threshold_alarms(report)
        assert any(a.startswith("FLYWHEEL:") for a in alarms)

    def test_format_slack_renders_flywheel_line(self):
        import cora_health_report as chr_mod
        report = {
            "token_method": "approx",
            "kb_corpus": {"available": False},
            "state": {},
            "billing": {},
            "scheduled_tasks": {"available": False},
            "static_context": {},
            "tool_block": {},
            "alarms": [],
            "flywheel": {"available": True, "knowledge_dms_7d": 2,
                         "gaps_last_entry_age_days": 3.0,
                         "gap_autofill_proposed_7d": 1,
                         "shadow_records": 4, "shadow_days": 2,
                         "pending_total": 123},
        }
        msg = chr_mod.format_slack(report)
        assert "*Flywheel:*" in msg
        assert "knowledge DMs 7d 2" in msg


# ─────────────────────────────────────────────────────────────────────────────
# Fork 5a (Wave-1 flywheel-conversion calibration, 2026-07-30): conversion-of-
# eligible rate, per lane; eligible-signal inflow; routing-completeness; T0
# baseline read-through
# ─────────────────────────────────────────────────────────────────────────────
class TestWave1ConversionMetrics:
    def test_eligible_signal_inflow_excludes_lex_and_capability(self, tmp_path, monkeypatch):
        monkeypatch.delenv("KNOWLEDGE_GAPS_LOG_PATH", raising=False)
        recent = NOW - timedelta(days=2)
        _write_jsonl(tmp_path / "logs" / "knowledge-gaps.jsonl", [
            {"ts": recent.isoformat(), "entity": "F3E",
             "question": "who is the Sprouts buyer?", "gap": "g"},
            {"ts": recent.isoformat(), "entity": "LEX",
             "question": "will RSP be affected by HNT?", "gap": "g"},
            {"ts": recent.isoformat(), "entity": "FNDR",
             "question": "can you check our RepRally wholesale listings?", "gap": "g"},
        ])
        m = fm.collect(now=NOW, repo_root=tmp_path)
        assert m["eligible_signal_inflow_7d"] == 1

    def test_conversions_by_lane_splits_mining_vs_escalation_and_efficiency(
            self, tmp_path, monkeypatch):
        recent = NOW - timedelta(days=2)
        _write_jsonl(tmp_path / "data" / "cora-proposed-memory-updates.jsonl", [
            {"update_id": "gapfill-mined", "update_type": "known_answer",
             "state": "APPROVED", "resolved_at": recent.isoformat(),
             "payload": {"answer_source": "slack_kb"}},
            {"update_id": "gapfill-asked", "update_type": "known_answer",
             "state": "APPROVED", "resolved_at": recent.isoformat(),
             "payload": {"answer_source": "teammate_dm"}},
            {"update_id": "friction-1", "update_type": "efficiency",
             "state": "APPROVED", "resolved_at": recent.isoformat()},
            # Not APPROVED -> not a conversion.
            {"update_id": "gapfill-pending", "update_type": "known_answer",
             "state": "PENDING", "proposed_at": recent.isoformat()},
        ])
        m = fm.collect(now=NOW, repo_root=tmp_path)
        assert m["conversions_by_lane_7d"] == {
            "known_answer_mined": 1, "known_answer_escalation_asker": 1,
            "friction_efficiency": 1, "decision_staged": 0,
            "lexicon_mined": 0, "lexicon_taught": 0,
        }

    def test_lexicon_conversions_split_taught_vs_mined(self, tmp_path, monkeypatch):
        recent = NOW - timedelta(days=2)
        _write_jsonl(tmp_path / "data" / "cora-proposed-memory-updates.jsonl", [
            {"update_id": "lexicon-a", "update_type": "lexicon",
             "state": "APPROVED", "resolved_at": recent.isoformat(),
             "payload": {"lane": "taught"}},
            {"update_id": "lexicon-b", "update_type": "lexicon",
             "state": "APPROVED", "resolved_at": recent.isoformat(),
             "payload": {"lane": "mined"}},
            {"update_id": "lexicon-c", "update_type": "lexicon",
             "state": "APPROVED", "resolved_at": recent.isoformat(),
             "payload": {"lane": "resolver"}},   # lane A -> counts as mined
            {"update_id": "lexicon-old", "update_type": "lexicon",
             "state": "APPROVED",
             "resolved_at": (NOW - timedelta(days=20)).isoformat(),
             "payload": {"lane": "taught"}},     # outside window
        ])
        m = fm.collect(now=NOW, repo_root=tmp_path)
        conv = m["conversions_by_lane_7d"]
        assert conv["lexicon_taught"] == 1
        assert conv["lexicon_mined"] == 2

    def test_code_queue_routings_tracked_separately_not_in_knowledge_numerator(
            self, tmp_path):
        recent = NOW - timedelta(days=2)
        old = NOW - timedelta(days=20)
        _write_jsonl(tmp_path / "data" / "state" / "code-session-queue.jsonl", [
            {"event": "captured", "id": "cq-1", "ts": recent.isoformat(),
             "signal": "capability", "entity": "FNDR"},
            {"event": "captured", "id": "cq-2", "ts": recent.isoformat(),
             "signal": "capability", "entity": "LEX"},
            {"event": "captured", "id": "cq-3", "ts": old.isoformat(),
             "signal": "capability", "entity": "FNDR"},  # outside window
            {"event": "captured", "id": "cq-4", "ts": recent.isoformat(),
             "signal": "tool_error", "entity": "F3E"},  # not capability
        ])
        m = fm.collect(now=NOW, repo_root=tmp_path)
        assert m["code_queue_capability_routed_7d"] == 2
        # Never folded into the knowledge-conversion numerator.
        assert "code_queue_capability_routed_7d" not in m["conversions_by_lane_7d"]

    def test_gap_routing_completeness_routed_vs_rotting(self, tmp_path, monkeypatch):
        monkeypatch.delenv("KNOWLEDGE_GAPS_LOG_PATH", raising=False)
        # The conftest autouse write-isolation (cq-d9432f552a33) points
        # RESOLVED_GAPS_PATH at a tmp file; this test builds its OWN repo-root
        # tree and needs the repo-relative fallback to resolve within it.
        monkeypatch.delenv("RESOLVED_GAPS_PATH", raising=False)
        # Same reason (session #11 S2): the autouse fixture now also redirects
        # GAP_AUTOFILL_STATE_PATH, and this test writes its own copy under
        # tmp_path/data/state and relies on the repo-relative fallback.
        monkeypatch.delenv("GAP_AUTOFILL_STATE_PATH", raising=False)
        old1 = NOW - timedelta(days=10)
        old2 = NOW - timedelta(days=12)
        old3 = NOW - timedelta(days=15)
        recent = NOW - timedelta(days=1)  # within 7d -- excluded from the >7d count
        _write_jsonl(tmp_path / "logs" / "knowledge-gaps.jsonl", [
            {"ts": old1.isoformat(), "entity": "F3E", "question": "q1", "gap": "g"},
            {"ts": old2.isoformat(), "entity": "OSN", "question": "q2", "gap": "g"},
            {"ts": old3.isoformat(), "entity": "FNDR", "question": "q3", "gap": "g"},
            {"ts": recent.isoformat(), "entity": "F3E", "question": "q4", "gap": "g"},
        ])
        # old1 resolved; old2 has an autofill state entry; old3 has neither (rotting).
        _write_jsonl(tmp_path / "design" / "known-answers" / ".resolved-gaps.jsonl", [
            {"id": old1.isoformat(), "action": "answer"},
        ])
        (tmp_path / "data" / "state").mkdir(parents=True, exist_ok=True)
        (tmp_path / "data" / "state" / "gap_autofill_state.json").write_text(
            json.dumps({old2.isoformat(): {"state": "proposed"}}), encoding="utf-8")

        m = fm.collect(now=NOW, repo_root=tmp_path)
        routing = m["gap_routing_completeness_7d"]
        assert routing["total_over_7d"] == 3
        assert routing["routed"] == 2
        assert routing["rotting"] == 1

    def test_t0_baseline_passthrough(self, tmp_path):
        baseline = {
            "computed_at": "2026-07-30T12:00:00+00:00",
            "eligible_open_count": 0,
            "disposition_counts": {"walled-permanent": 6, "capability": 5, "expire": 2},
        }
        (tmp_path / "data" / "state").mkdir(parents=True, exist_ok=True)
        (tmp_path / "data" / "state" / "flywheel-t0-baseline.json").write_text(
            json.dumps(baseline), encoding="utf-8")
        m = fm.collect(now=NOW, repo_root=tmp_path)
        assert m["t0_baseline"] == baseline

    def test_t0_baseline_none_when_absent(self, tmp_path):
        m = fm.collect(now=NOW, repo_root=tmp_path)
        assert m["t0_baseline"] is None

    def test_format_lines_surfaces_wave1_fields(self):
        lines = fm.format_lines({
            "available": True, "gaps_by_detector": {}, "gaps_last_entry_age_days": None,
            "eligible_signal_inflow_7d": 2,
            "conversions_by_lane_7d": {
                "known_answer_mined": 1, "known_answer_escalation_asker": 0,
                "friction_efficiency": 0, "decision_staged": 0,
            },
            "code_queue_capability_routed_7d": 3,
            "gap_routing_completeness_7d": {
                "total_over_7d": 5, "routed": 4, "rotting": 1,
            },
            "t0_baseline": {"computed_at": "2026-07-30", "eligible_open_count": 0,
                           "disposition_counts": {}},
        })
        blob = "\n".join(lines)
        assert "eligible-signal inflow, 7d: 2" in blob
        assert "known_answer_mined=1" in blob
        assert "code-queue capability routings, 7d: 3" in blob
        assert "NOT a knowledge conversion" in blob
        assert "4/5 routed, 1 rotting" in blob
        assert "T0 baseline" in blob
