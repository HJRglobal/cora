"""C2 (cq-16014e463a66): the MECHANICAL BACKLOG number reaches a human surface.

Before this, the count existed ONLY as a `log.warning` inside the review run
(scripts/run_knowledge_review.py). It went 15 -> 54 across 2026-08-21..08-24
entirely inside a log file, while the weekly #cora-health digest printed a flat
"PENDING 250" -- the exact shape of an alarm that is computed, correct, and
invisible. The audit read that as "the alarm went dark post-flip"; it had never
been lit. The flip only changed the message's TEXT (it drops a "no surface is
enabled" suffix) and let the backlog grow fast.

Two properties carry the weight here:

  ONE PREDICATE. The health surfaces must count the SAME population the review
  run escalates. `review_lanes.past_review_deadline` is now the single
  definition and `_mechanical_past_deadline` a delegate; a copy would let the
  alarm and the run drift apart silently, which is worse than no alarm.

  HONEST ABSENCE. A missing metric must never read as zero-and-fine, and a
  missing baseline series must never render as "+0" -- collect() degrades to
  partial metrics by design, so every consumer is tested against the gap.
"""

from __future__ import annotations

import json
import sys
import unittest.mock as _m
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import cora.flywheel_metrics as fm
from cora import review_lanes

_SCRIPTS = str(Path(__file__).resolve().parents[1] / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)


NOW = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
OVERDUE = (NOW - timedelta(days=30)).isoformat()
NOT_YET = (NOW + timedelta(days=5)).isoformat()


def _collect(tmp_path, rows):
    path = tmp_path / "data" / "cora-proposed-memory-updates.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in rows:
            fh.write(json.dumps(rec) + "\n")
    return fm.collect(now=NOW, repo_root=tmp_path, update_baseline=False)


@pytest.fixture(autouse=True)
def _no_stray_gap_log(monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_GAPS_LOG_PATH", raising=False)


def _slack_report(**flywheel):
    base = {
        "token_method": "approx",
        "kb_corpus": {"available": False},
        "state": {},
        "billing": {},
        "scheduled_tasks": {"available": False},
        "static_context": {},
        "tool_block": {},
        "alarms": [],
        "flywheel": dict({"available": True}, **flywheel),
    }
    return base


# ── counting ────────────────────────────────────────────────────────────────

def test_counts_only_pending_mechanical_rows(tmp_path):
    m = _collect(tmp_path, [
        # mechanical + PENDING + past deadline -> counted in BOTH gauges
        {"update_id": "m1", "update_type": "task_close", "state": "PENDING",
         "proposed_at": OVERDUE, "expires_at": OVERDUE},
        {"update_id": "m2", "update_type": "hubspot_note", "state": "PENDING",
         "proposed_at": OVERDUE, "expires_at": OVERDUE},
        # mechanical + PENDING but INSIDE its deadline -> pending only
        {"update_id": "m3", "update_type": "asana_task", "state": "PENDING",
         "proposed_at": OVERDUE, "expires_at": NOT_YET},
        # mechanical but already RESOLVED -> neither
        {"update_id": "m4", "update_type": "task_close", "state": "DISMISSED",
         "proposed_at": OVERDUE, "expires_at": OVERDUE, "resolved_at": OVERDUE},
        # judgment and decision rows are other lanes -> neither gauge
        {"update_id": "k1", "update_type": "known_answer", "state": "PENDING",
         "proposed_at": OVERDUE, "expires_at": OVERDUE},
        {"update_id": "d1", "update_type": "decision_capture", "state": "PENDING",
         "proposed_at": OVERDUE, "expires_at": OVERDUE},
    ])
    assert m["mechanical_pending"] == 3
    assert m["mechanical_overdue"] == 2
    # ...and the broad gauge still sees every lane
    assert m["pending_total"] == 5


def test_a_row_with_no_expires_at_falls_back_to_the_shared_window(tmp_path):
    """Pre-TTL-at-creation rows carry no expires_at. They must age out on the
    same 14-day window the expiry pass used for them, not read as never-due."""
    old = (NOW - timedelta(days=review_lanes.OPERATIONAL_UNROUTED_EXPIRY_DAYS + 1))
    young = (NOW - timedelta(days=review_lanes.OPERATIONAL_UNROUTED_EXPIRY_DAYS - 1))
    m = _collect(tmp_path, [
        {"update_id": "old", "update_type": "task_close", "state": "PENDING",
         "proposed_at": old.isoformat()},
        {"update_id": "young", "update_type": "task_close", "state": "PENDING",
         "proposed_at": young.isoformat()},
    ])
    assert m["mechanical_pending"] == 2
    assert m["mechanical_overdue"] == 1


def test_a_malformed_row_degrades_one_row_not_the_metric(tmp_path):
    m = _collect(tmp_path, [
        {"update_id": "bad", "update_type": "task_close", "state": "PENDING",
         "proposed_at": "not-a-date"},
        {"update_id": "good", "update_type": "task_close", "state": "PENDING",
         "proposed_at": OVERDUE, "expires_at": OVERDUE},
    ])
    assert m["mechanical_pending"] == 2      # both still classify
    assert m["mechanical_overdue"] == 1      # the unparseable one fails SAFE


# ── one predicate, three consumers ──────────────────────────────────────────

def test_the_count_uses_the_SAME_predicate_the_review_run_escalates_on():
    import run_knowledge_review as rkr
    row = {"update_id": "x", "update_type": "task_close", "state": "PENDING",
           "proposed_at": OVERDUE,
           "expires_at": (NOW - timedelta(days=1)).isoformat()}
    assert rkr._mechanical_past_deadline(row, NOW) is True
    assert review_lanes.past_review_deadline(row, NOW) is True
    # the delegate really delegates: patching the shared definition must change
    # the script's answer, or the two have forked again
    with _m.patch.object(review_lanes, "past_review_deadline", return_value=False):
        assert rkr._mechanical_past_deadline(row, NOW) is False
    assert (rkr._OPERATIONAL_UNROUTED_EXPIRY_DAYS
            == review_lanes.OPERATIONAL_UNROUTED_EXPIRY_DAYS)


# ── the alarm ───────────────────────────────────────────────────────────────

def test_alarm_fires_above_threshold_and_names_the_pool():
    alarms = fm.evaluate({"mechanical_overdue": fm.WARN_MECHANICAL_OVERDUE + 1,
                          "mechanical_pending": 189})
    msgs = [msg for _sev, msg in alarms if "MECHANICAL BACKLOG" in msg]
    assert len(msgs) == 1
    assert "189" in msgs[0], "the alarm must state the pool, not only the overdue count"


@pytest.mark.parametrize("n", [0, 1, fm.WARN_MECHANICAL_OVERDUE])
def test_alarm_silent_at_or_below_threshold(n):
    alarms = fm.evaluate({"mechanical_overdue": n, "mechanical_pending": 100})
    assert not [msg for _sev, msg in alarms if "MECHANICAL BACKLOG" in msg]


@pytest.mark.parametrize("metrics", [{}, {"mechanical_overdue": None},
                                     {"mechanical_overdue": "54"}])
def test_absent_or_unusable_metric_never_alarms_and_never_raises(metrics):
    """collect() degrades to partial metrics on a ledger error. A missing key
    must not read as zero-and-fine, and must not take evaluate() down."""
    assert not [msg for _sev, msg in fm.evaluate(metrics)
                if "MECHANICAL BACKLOG" in msg]


def test_the_alarm_reaches_the_health_report_alarm_block():
    """The point of raising it in evaluate() rather than adding a Slack call to
    the review script: one alarm, both surfaces, single-sourced thresholds."""
    import cora_health_report as chr_mod
    report = {
        "kb_corpus": {"available": False},
        "state": {"jsonl_ledgers": {}, "logs_dir_bytes": 0},
        "scheduled_tasks": {"available": False},
        "flywheel": {"available": True,
                     "alarm_lines": ["MECHANICAL BACKLOG: 54 of 189 pending "
                                     "mechanical past their review deadline"]},
    }
    alarms = chr_mod.threshold_alarms(report)
    assert any("MECHANICAL BACKLOG" in a for a in alarms)


# ── the digest lines ────────────────────────────────────────────────────────

def test_format_lines_carries_the_mechanical_line():
    lines = fm.format_lines({"available": True, "mechanical_pending": 189,
                             "mechanical_overdue": 54})
    assert any("mechanical lane: 189 pending, 54 past their review deadline" in ln
               for ln in lines)


def test_slack_digest_carries_the_WoW_trend_and_the_mechanical_line():
    """The delta was already computed by collect() and rendered by
    format_lines() -- but format_lines feeds only stdout, and the hand-built
    Slack line dropped it. A flat "PENDING 251" reads identically on the week it
    rose +75 and on a quiet one."""
    import cora_health_report as chr_mod
    msg = chr_mod.format_slack(_slack_report(
        knowledge_dms_7d=2, gaps_last_entry_age_days=3.0,
        gap_autofill_proposed_7d=1, shadow_records=4, shadow_days=2,
        pending_total=251, pending_growth_7d=75,
        mechanical_pending=189, mechanical_overdue=54))
    assert "PENDING 251" in msg
    assert "+75 vs 7d ago" in msg
    assert "*Mechanical lane:* 189 pending | 54 past review deadline" in msg


def test_a_shrinking_pool_renders_its_sign():
    import cora_health_report as chr_mod
    msg = chr_mod.format_slack(_slack_report(pending_total=100,
                                             pending_growth_7d=-12))
    assert "-12 vs 7d ago" in msg


def test_slack_digest_omits_the_trend_when_there_is_no_history():
    """A fresh install has no baseline series and collect() sets the growth to
    None. Rendering "+0" there would be a lie."""
    import cora_health_report as chr_mod
    msg = chr_mod.format_slack(_slack_report(pending_total=12,
                                             pending_growth_7d=None))
    assert "PENDING 12" in msg
    assert "vs 7d ago" not in msg
    assert "*Mechanical lane:*" not in msg
