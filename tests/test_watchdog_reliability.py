"""Reliability rails for the auto-recovery layer (slice 1, cq-7915a8647cff + cq-0d163e5f9c22).

The 8/18 forensics reached three conclusions from log evidence -- "watchdog
restarts don't restart", "heartbeat.txt froze while the same instance's log
heartbeats ran", "blind windows with zero watchdog lines" -- and the first two
were artifacts of missing observability, not bugs:

  * `TimedRotatingFileHandler` pins the live log to the process START date and
    moves each completed day to `cora-<startdate>.log.<thatday>`, so a
    `cora-*.log` grep cannot see the startup line of any instance older than a
    day. All four historical restarts DID produce a fresh "Cora starting up".
  * The log line and the heartbeat.txt write are in the SAME loop iteration, and
    a write failure only ever produced one un-watched WARNING.

These tests pin the rails that make the next incident readable: provable
instance identity, an escalating write-failure alarm, a log scan that finds the
live log regardless of its pinned name, and a health check that can tell a
healthy watchdog from a watchdog that never ran.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import nightly_health_check as hc  # noqa: E402
from cora import instance_ledger  # noqa: E402

_WATCHDOG_PS1 = _REPO_ROOT / "deployment" / "cora-watchdog.ps1"
_RESTART_PS1 = _REPO_ROOT / "deployment" / "restart-cora.ps1"


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    """Redirect BOTH ledger paths at tmp; the real files are live-bot state."""
    monkeypatch.setattr(instance_ledger, "INSTANCE_FILE", tmp_path / "instance.json")
    monkeypatch.setattr(instance_ledger, "LEDGER_FILE", tmp_path / "cora-instances.jsonl")
    return tmp_path


# ── instance ledger: provable identity ───────────────────────────────────────

def test_record_start_writes_ledger_row_and_sentinel(ledger):
    row = instance_ledger.record_start(log_file="logs/cora-2026-08-17.log")
    assert row["event"] == "start"
    assert row["pid"] > 0

    starts = instance_ledger.read_starts()
    assert len(starts) == 1
    assert starts[0]["log_file"] == "logs/cora-2026-08-17.log"

    current = instance_ledger.read_current()
    assert current["pid"] == row["pid"]
    assert current["started_at"] == row["ts"]
    assert current["heartbeat_write_failures"] == 0


def test_restart_is_verifiable_by_a_new_pid_not_an_exit_code(ledger, monkeypatch):
    """The whole point: two start rows with different pids is what proves a restart."""
    monkeypatch.setattr(instance_ledger.os, "getpid", lambda: 1111)
    instance_ledger.record_start()
    monkeypatch.setattr(instance_ledger.os, "getpid", lambda: 2222)
    instance_ledger.record_start()

    starts = instance_ledger.read_starts()
    assert [s["pid"] for s in starts] == [1111, 2222]
    assert instance_ledger.read_current()["pid"] == 2222


def test_touch_preserves_started_at_and_reports_write_failures(ledger):
    started = instance_ledger.record_start(log_file="logs/x.log")["ts"]
    assert instance_ledger.touch(120, write_failures=3) is True
    current = instance_ledger.read_current()
    assert current["started_at"] == started      # not clobbered by a heartbeat tick
    assert current["log_file"] == "logs/x.log"
    assert current["uptime_s"] == 120
    assert current["heartbeat_write_failures"] == 3


def test_ledger_functions_never_raise_when_the_path_is_unwritable(tmp_path, monkeypatch):
    """Observability must never take the bot down -- every entry point swallows."""
    unwritable = tmp_path / "nope" / "deep"
    monkeypatch.setattr(instance_ledger, "INSTANCE_FILE", unwritable / "instance.json")
    monkeypatch.setattr(instance_ledger, "LEDGER_FILE", unwritable / "ledger.jsonl")

    def boom(*_a, **_k):
        raise OSError("read-only file system")

    monkeypatch.setattr(instance_ledger.Path, "mkdir", boom)
    assert instance_ledger.record_start()["event"] == "start"
    assert instance_ledger.record_stop("test")["event"] == "stop"
    assert instance_ledger.touch(5) is False
    assert instance_ledger.read_current() is None
    assert instance_ledger.read_starts() == []


def test_read_current_tolerates_a_truncated_or_non_dict_sentinel(ledger):
    instance_ledger.INSTANCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    instance_ledger.INSTANCE_FILE.write_text('{"pid": 1', encoding="utf-8")
    assert instance_ledger.read_current() is None
    instance_ledger.INSTANCE_FILE.write_text("[1, 2]", encoding="utf-8")
    assert instance_ledger.read_current() is None


def test_read_starts_skips_stop_rows_and_garbage(ledger):
    instance_ledger.record_start()
    instance_ledger.record_stop("clean")
    with instance_ledger.LEDGER_FILE.open("a", encoding="utf-8") as fh:
        fh.write("not json at all\n\n")
    starts = instance_ledger.read_starts()
    assert len(starts) == 1 and starts[0]["event"] == "start"


# ── heartbeat loop: the write-failure alarm ──────────────────────────────────

class _FakeStop:
    """Event stand-in that lets the heartbeat loop run exactly `n` iterations."""

    def __init__(self, n):
        self.n = n

    def wait(self, _timeout):
        self.n -= 1
        return self.n < 0

    def set(self):
        pass


def test_heartbeat_escalates_repeated_write_failures(tmp_path, monkeypatch, caplog):
    from cora import main as cora_main

    monkeypatch.setattr(cora_main, "_HEARTBEAT_FILE", tmp_path / "ro" / "heartbeat.txt")
    monkeypatch.setattr(instance_ledger, "INSTANCE_FILE", tmp_path / "instance.json")
    monkeypatch.setattr(instance_ledger, "LEDGER_FILE", tmp_path / "ledger.jsonl")

    def boom(*_a, **_k):
        raise OSError("disk gone")

    monkeypatch.setattr(Path, "write_text", boom)

    import logging
    log = logging.getLogger("test-heartbeat")
    with caplog.at_level(logging.WARNING, logger="test-heartbeat"):
        cora_main._heartbeat(_FakeStop(3), log)

    text = caplog.text
    # First failure stays a warning; the second onward carries the token the
    # nightly check treats as critical.
    assert "failed to write sentinel file" in text
    assert text.count("HEARTBEAT_FILE_WRITE_FAILING") == 2
    assert "3 consecutive failures" in text


def test_heartbeat_logs_recovery_and_resets_the_counter(tmp_path, monkeypatch, caplog):
    from cora import main as cora_main
    import logging

    hb = tmp_path / "heartbeat.txt"
    monkeypatch.setattr(cora_main, "_HEARTBEAT_FILE", hb)
    monkeypatch.setattr(instance_ledger, "INSTANCE_FILE", tmp_path / "instance.json")
    monkeypatch.setattr(instance_ledger, "LEDGER_FILE", tmp_path / "ledger.jsonl")

    real_write = Path.write_text
    calls = {"n": 0}

    def flaky(self, *a, **k):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise OSError("transient")
        return real_write(self, *a, **k)

    monkeypatch.setattr(Path, "write_text", flaky)
    log = logging.getLogger("test-heartbeat-recover")
    with caplog.at_level(logging.WARNING, logger="test-heartbeat-recover"):
        cora_main._heartbeat(_FakeStop(3), log)

    assert "RECOVERED after 2 consecutive failures" in caplog.text
    assert hb.exists()
    assert instance_ledger.read_current()["heartbeat_write_failures"] == 0


def test_heartbeat_line_carries_the_pid(tmp_path, monkeypatch, caplog):
    """Two stacked instances interleave into one log file; without a pid the
    forensics cannot separate them (the whole 8/18 dead end)."""
    from cora import main as cora_main
    import logging
    import os

    monkeypatch.setattr(cora_main, "_HEARTBEAT_FILE", tmp_path / "heartbeat.txt")
    monkeypatch.setattr(instance_ledger, "INSTANCE_FILE", tmp_path / "instance.json")
    monkeypatch.setattr(instance_ledger, "LEDGER_FILE", tmp_path / "ledger.jsonl")
    log = logging.getLogger("test-heartbeat-pid")
    with caplog.at_level(logging.INFO, logger="test-heartbeat-pid"):
        cora_main._heartbeat(_FakeStop(1), log)
    assert f"pid={os.getpid()}" in caplog.text


def test_heartbeat_file_stays_a_bare_iso_timestamp(tmp_path, monkeypatch):
    """Ten parsers (watchdog.ps1, health_endpoint, strategy_memo, the KB scripts)
    read this file with a plain ISO parse. New facts belong in instance.json."""
    from cora import main as cora_main
    import logging

    hb = tmp_path / "heartbeat.txt"
    monkeypatch.setattr(cora_main, "_HEARTBEAT_FILE", hb)
    monkeypatch.setattr(instance_ledger, "INSTANCE_FILE", tmp_path / "instance.json")
    monkeypatch.setattr(instance_ledger, "LEDGER_FILE", tmp_path / "ledger.jsonl")
    cora_main._heartbeat(_FakeStop(1), logging.getLogger("test-hb-format"))

    raw = hb.read_text(encoding="utf-8")
    assert raw.count("\n") == 1
    parsed = datetime.fromisoformat(raw.strip())
    assert parsed.tzinfo is not None


def test_setup_logging_returns_the_log_file_it_opened(tmp_path, monkeypatch):
    from cora import main as cora_main

    path = cora_main._setup_logging()
    assert isinstance(path, str) and path.endswith(".log")


# ── nightly health check ─────────────────────────────────────────────────────

def _write_watchdog_log(log_dir: Path, day: str, rows: list[dict]) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / f"watchdog-{day}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )


def test_watchdog_with_no_log_at_all_is_critical(tmp_path, monkeypatch):
    monkeypatch.setattr(hc, "_LOG_DIR", tmp_path)
    tmp_path.mkdir(exist_ok=True)
    res = hc.check_watchdog_liveness(now=datetime(2026, 8, 19, 12, tzinfo=timezone.utc))
    assert res.status == "critical"


def test_watchdog_tick_fresh_is_ok(tmp_path, monkeypatch):
    now = datetime(2026, 8, 19, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(hc, "_LOG_DIR", tmp_path)
    _write_watchdog_log(tmp_path, "2026-08-19", [
        {"ts": (now - timedelta(minutes=20)).isoformat(), "event": "tick",
         "age_min": 1.0, "healthy": True, "elevated": True},
    ])
    res = hc.check_watchdog_liveness(now=now)
    assert res.status == "ok"


def test_watchdog_silence_is_critical_not_ok(tmp_path, monkeypatch):
    """The exact 8/18 blind window: the task is Enabled, nothing is logged, and
    the old world had no way to distinguish that from healthy."""
    now = datetime(2026, 8, 19, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(hc, "_LOG_DIR", tmp_path)
    _write_watchdog_log(tmp_path, "2026-08-17", [
        {"ts": (now - timedelta(hours=29)).isoformat(), "event": "tick", "age_min": 1.0},
    ])
    res = hc.check_watchdog_liveness(now=now)
    assert res.status == "critical"
    assert "29" in res.detail and "schtasks /End" in res.detail


def test_watchdog_unverified_restart_is_a_warn(tmp_path, monkeypatch):
    now = datetime(2026, 8, 19, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(hc, "_LOG_DIR", tmp_path)
    _write_watchdog_log(tmp_path, "2026-08-19", [
        {"ts": (now - timedelta(hours=2)).isoformat(), "event": "restart_unverified",
         "restart_exit": 0, "action": "ESCALATE_ALERT"},
        {"ts": (now - timedelta(minutes=10)).isoformat(), "event": "tick", "age_min": 1.0},
    ])
    res = hc.check_watchdog_liveness(now=now)
    assert res.status == "warn"
    assert "restart_unverified" in res.detail


def test_watchdog_non_elevated_escalation_is_surfaced(tmp_path, monkeypatch):
    now = datetime(2026, 8, 19, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(hc, "_LOG_DIR", tmp_path)
    _write_watchdog_log(tmp_path, "2026-08-19", [
        {"ts": (now - timedelta(minutes=5)).isoformat(), "event": "tick",
         "age_min": 1.0, "elevated": False, "action": "ESCALATE_ALERT"},
    ])
    res = hc.check_watchdog_liveness(now=now)
    assert res.status == "warn"


def test_watchdog_log_with_bom_and_garbage_lines_still_parses(tmp_path, monkeypatch):
    """Add-Content -Encoding utf8 in PS 5.1 writes a BOM; the real files have one."""
    now = datetime(2026, 8, 19, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(hc, "_LOG_DIR", tmp_path)
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "watchdog-2026-08-19.jsonl").write_text(
        "﻿" + json.dumps({"ts": (now - timedelta(minutes=5)).isoformat(),
                               "event": "tick", "age_min": 1.0}) + "\n"
        "half a line{\n", encoding="utf-8")
    assert hc.check_watchdog_liveness(now=now).status == "ok"


def test_heartbeat_write_failure_token_is_a_critical_log_pattern():
    assert hc._CRITICAL_RE.search(
        "2026-08-19T00:00:00 ERROR [Heartbeat] __main__: "
        "HEARTBEAT_FILE_WRITE_FAILING: 4 consecutive failures writing x: disk gone")


def test_already_running_last_result_has_a_hint():
    assert 2147946720 in hc._LAST_RESULT_HINTS
    assert 2147946720 not in hc._BENIGN_LAST_RESULTS


def test_log_scan_finds_a_start_date_pinned_live_log(tmp_path, monkeypatch):
    """The regression that hid every critical pattern on a long-uptime instance:
    the live file is named for the START date, so a today/yesterday NAME filter
    excluded it. Recency must come from mtime."""
    monkeypatch.setattr(hc, "_LOG_DIR", tmp_path)
    tmp_path.mkdir(exist_ok=True)
    # Named three days ago (the instance started then) but written just now.
    stale_name = tmp_path / "cora-2026-08-16.log"
    stale_name.write_text(
        "2026-08-19T09:00:00 ERROR [Heartbeat] __main__: HEARTBEAT_FILE_WRITE_FAILING: "
        "9 consecutive failures writing x: disk gone\n", encoding="utf-8")
    results = hc.check_logs_24h()
    crit = [r for r in results if r.status == "critical"]
    assert crit, "a fresh-mtime, stale-named log must still be scanned"
    assert "HEARTBEAT_FILE_WRITE_FAILING" in crit[0].detail


def test_auto_restart_kill_filter_is_scoped_to_the_bot(monkeypatch):
    """The old filter matched any python.exe with 'cora' anywhere in its command
    line -- which includes scripts/run_mcp_server.py and every in-flight KB
    migration. An auto-restart must not shoot those down."""
    captured: list[list[str]] = []

    class _R:
        returncode = 0
        stdout = ""

    def fake_run(cmd, **_kw):
        captured.append(cmd)
        return _R()

    monkeypatch.setattr(hc.subprocess, "run", fake_run)
    monkeypatch.setattr(hc.time, "sleep", lambda _s: None)
    hc._restart_cora(dry_run=False)

    kill = [c for c in captured if any("Win32_Process" in str(p) for p in c)]
    assert kill, "expected a process-kill command"
    joined = " ".join(kill[0])
    assert "cora.main" in joined and "Scripts\\cora.exe" in joined
    assert "'*cora*'" not in joined


# ── the two PowerShell rails (source-pinned: they cannot be unit-tested) ─────

def test_watchdog_ps1_logs_a_tick_and_traps_its_own_errors():
    text = _WATCHDOG_PS1.read_text(encoding="utf-8")
    assert 'event = "tick"' in text
    assert 'event = "watchdog_error"' in text
    assert 'event = "restart_verified"' in text
    assert 'event = "restart_unverified"' in text
    assert 'event = "restart_blocked_not_elevated"' in text
    # A bounded verification wait -- a watchdog that hangs blocks every later
    # trigger under MultipleInstances=IgnoreNew.
    assert "$VerifyWaitSeconds" in text
    assert "-TotalCount 1" in text  # first line only: heartbeat.txt format is fixed


def test_restart_ps1_process_shape_counter_matches_the_kill_filter():
    """cq-0d163e5f9c22: the counter looked for processes the kill filter never
    matches, so it printed 0+0 and warned on every single restart."""
    text = _RESTART_PS1.read_text(encoding="utf-8")
    assert "cora.main" in text
    # Pin the PREDICATE, not a phrase: the old wording survives in the comment
    # that explains why it was wrong, so grepping the phrase would pass either way.
    assert "$pys.Count -ne 2 -or $launchers.Count -ne 1" not in text
    assert "cora-instances.jsonl" in text
    assert "$bot.Count -gt 3" in text


@pytest.mark.parametrize("ps1", [_WATCHDOG_PS1, _RESTART_PS1])
def test_deployment_ps1_files_are_ascii_only(ps1):
    """D-016: PowerShell 5.1 reads UTF-8 as Windows-1252."""
    raw = ps1.read_bytes()
    bad = [(i, b) for i, b in enumerate(raw) if b > 127]
    assert not bad, f"{ps1.name} has non-ASCII bytes at {bad[:5]}"
