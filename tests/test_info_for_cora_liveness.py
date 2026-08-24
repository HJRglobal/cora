"""C7 (cq-77984df448c7): sweep liveness and sweep frozen-ness are two facts.

The seed said the #info-for-cora sweep had been "frozen 3 weeks per its own
repeating health warning" and asked to unfreeze it or retire the warning.
VERIFY-FIRST overturned it: the sweep is Ready, LastTaskResult 0, 0 missed runs,
ran at 06:05 on 2026-08-24, and advanced its watermark correctly on 8/22 to the
parent ts of Hannah Grant's 8/21 thread.

The warning was a false positive with a structural cause. `check_info_for_cora_
watermark` used the watermark FILE's mtime as its liveness proxy, on the stated
premise that "only a running sweep writes it". False: the watermark records the
newest PROCESSED MESSAGE and is written only when `high_water` is set, so a run
over a quiet channel completes, returns 0 and touches nothing. mtime tracked
CHANNEL TRAFFIC. Sweep at 06:05, health check at 08:45, threshold 48h -- exactly
two consecutive quiet days give 50.7h and fire. The live history is a sawtooth
keyed to channel posts (51/75/99/147/171/195/219/243h, silent 8/18-19, 51/75h,
silent 8/22-23, 51h on 8/24), reported as one 3-week freeze.

And it could NOT simply be retired: a genuine poison-pill freeze also returns 0
and writes nothing, so it was byte-identical to a quiet day. The condition the
check was built for had never been detectable at all.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SCRIPTS = str(Path(__file__).resolve().parents[1] / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import nightly_health_check as nhc  # noqa: E402
import run_info_for_cora_sweep as sweep  # noqa: E402

NOW = datetime(2026, 8, 24, 15, 45, tzinfo=timezone.utc)


@pytest.fixture()
def state(tmp_path, monkeypatch):
    d = tmp_path / "data" / "state"
    d.mkdir(parents=True)
    monkeypatch.setattr(nhc, "_REPO_ROOT", tmp_path)
    return d


def _runstate(d: Path, *, hours_ago: float, outcome: str):
    (d / "info-for-cora-runstate.json").write_text(json.dumps({
        "last_run_ts": (NOW - timedelta(hours=hours_ago)).isoformat(),
        "outcome": outcome, "detail": "",
    }), encoding="utf-8")


# ── the false positive is gone ──────────────────────────────────────────────

def test_two_quiet_days_no_longer_alarm(state):
    """THE regression. A watermark 58h old (the live value on 8/24) with the
    sweep having run 2h45m ago is a quiet channel, not a freeze."""
    (state / "info-for-cora-watermark.json").write_text(
        json.dumps({"last_ts": "1787352572.762379"}), encoding="utf-8")
    _runstate(state, hours_ago=2.75, outcome="complete")
    r = nhc.check_info_for_cora_watermark(now=NOW)
    assert r.status == "ok"
    assert "not a fault" in r.detail


def test_a_stale_watermark_alone_is_never_the_alarm(state):
    """Even a watermark three weeks old is fine while the sweep is running --
    that is what "quiet channel" looks like."""
    (state / "info-for-cora-watermark.json").write_text(
        json.dumps({"last_ts": "1000000000.0"}), encoding="utf-8")
    _runstate(state, hours_ago=1, outcome="complete")
    assert nhc.check_info_for_cora_watermark(now=NOW).status == "ok"


# ── the real conditions now fire ────────────────────────────────────────────

def test_a_task_that_stopped_firing_alarms(state):
    _runstate(state, hours_ago=72, outcome="complete")
    r = nhc.check_info_for_cora_watermark(now=NOW)
    assert r.status == "warn"
    assert "not firing" in r.detail
    assert "info-for-cora-sweep-" in r.detail, "must point at a log that exists"


def test_a_genuine_freeze_alarms_even_though_the_run_is_fresh(state):
    """The condition the old check was built for and could never see: the freeze
    path returns 0 and writes no watermark, exactly like a quiet day."""
    _runstate(state, hours_ago=2, outcome="frozen")
    r = nhc.check_info_for_cora_watermark(now=NOW)
    assert r.status == "warn"
    assert "FROZE" in r.detail
    assert "poison-pill" in r.detail


def test_a_held_lock_is_surfaced(state):
    _runstate(state, hours_ago=2, outcome="locked")
    r = nhc.check_info_for_cora_watermark(now=NOW)
    assert r.status == "warn"
    assert "lock" in r.detail.lower()


def test_an_unreadable_marker_warns_rather_than_claiming_health(state):
    (state / "info-for-cora-runstate.json").write_text("{not json", encoding="utf-8")
    r = nhc.check_info_for_cora_watermark(now=NOW)
    assert r.status == "warn"
    assert "unreadable" in r.detail


# ── honest degradation on a host that has not run the new sweep yet ─────────

def test_no_marker_and_no_watermark_is_a_bootstrap_not_a_fault(state):
    r = nhc.check_info_for_cora_watermark(now=NOW)
    assert r.status == "ok"
    assert "bootstraps" in r.detail


def test_no_marker_but_an_existing_watermark_does_not_alarm(state):
    """Between merge and the sweep's next fire there is no marker. Alarming on
    the absence of a file this build introduced would just replace one false
    positive with another."""
    (state / "info-for-cora-watermark.json").write_text(
        json.dumps({"last_ts": "1000000000.0"}), encoding="utf-8")
    r = nhc.check_info_for_cora_watermark(now=NOW)
    assert r.status == "ok"
    assert "NOT a liveness signal" in r.detail


# ── the sweep stamps every exit ─────────────────────────────────────────────

def test_the_marker_records_the_outcome(tmp_path, monkeypatch):
    monkeypatch.setattr(sweep, "_RUNSTATE_PATH", tmp_path / "rs.json")
    sweep._write_runstate("frozen", "unfetched tail")
    data = json.loads((tmp_path / "rs.json").read_text(encoding="utf-8"))
    assert data["outcome"] == "frozen"
    assert data["detail"] == "unfetched tail"
    datetime.fromisoformat(data["last_run_ts"])  # parses


def test_writing_the_marker_never_raises(tmp_path, monkeypatch):
    """Bookkeeping must not be able to fail a sweep."""
    monkeypatch.setattr(sweep, "_RUNSTATE_PATH", tmp_path)  # a DIRECTORY
    sweep._write_runstate("complete", "")


def test_every_terminal_outcome_the_health_check_knows_is_one_the_sweep_writes():
    """A vocabulary drift here means the health check silently never fires."""
    src = (Path(__file__).resolve().parents[1]
           / "scripts" / "run_info_for_cora_sweep.py").read_text(encoding="utf-8")
    for outcome in ("complete", "frozen", "locked", "since_days"):
        assert f'_write_runstate("{outcome}"' in src, outcome


def test_the_sweep_writes_a_log_file_the_warning_can_point_at():
    """logs/info-for-cora-sweep* did not exist in any of the 1,626 log files:
    the script was console-only and the task has no redirection, so a REAL
    freeze logged its warning into a void and check_logs_24h could never see
    its errors."""
    src = (Path(__file__).resolve().parents[1]
           / "scripts" / "run_info_for_cora_sweep.py").read_text(encoding="utf-8")
    assert "logging.FileHandler(" in src
    assert "info-for-cora-sweep-" in src
