"""Slice 3: scheduled G:-reading/writing jobs fail soft on a mount outage.

Each scheduled module (strategy_memo, drive_materializer, nudge_ledger,
session_capture) now routes its G: I/O through drive_io, so a transient unmount
becomes a bounded drive_io.DriveUnavailable rather than a hang. These assert that:
  * fail-OPEN readers (nudge_ledger) degrade to their safe default,
  * fail-SOFT writers (materializer flywheel mirror) skip rather than abort,
  * writers whose runner already catches exceptions RAISE DriveUnavailable cleanly
    (the runner's top-level try/except then exits 1 -- no hang).

Imports come from `cora` (not `src.cora`) so DriveUnavailable identity matches the code.
"""

from __future__ import annotations

import pytest

from cora import drive_io, drive_materializer, nudge_ledger, strategy_memo, session_capture


@pytest.fixture(autouse=True)
def _reset_breaker():
    drive_io.reset_state_for_tests()
    yield
    drive_io.reset_state_for_tests()


def _raise_unavailable(*_a, **_k):
    raise drive_io.DriveUnavailable("simulated G: outage")


# ── each scheduled module imports the wrapper ────────────────────────────────

@pytest.mark.parametrize("mod", [strategy_memo, drive_materializer, nudge_ledger, session_capture])
def test_scheduled_module_uses_drive_io(mod):
    assert getattr(mod, "drive_io", None) is drive_io


# ── strategy_memo (weekly) ───────────────────────────────────────────────────

def test_gather_stalled_decisions_failsoft_on_outage(monkeypatch):
    monkeypatch.setattr(strategy_memo.drive_io, "read_text", _raise_unavailable)
    out = strategy_memo.gather_stalled_decisions()
    assert out == {"ok": False, "decisions": []}


def test_write_memo_file_raises_driveunavailable_on_outage(monkeypatch):
    """run_memo already wraps write_memo_file in try/except so the DM still sends;
    here we just prove the write surfaces a catchable DriveUnavailable, not a hang."""
    monkeypatch.setattr(strategy_memo.drive_io, "write_text_atomic", _raise_unavailable)
    with pytest.raises(drive_io.DriveUnavailable):
        strategy_memo.write_memo_file("some memo document")


# ── drive_materializer (nightly) ─────────────────────────────────────────────

def test_write_swept_file_raises_driveunavailable_on_outage(monkeypatch):
    monkeypatch.setattr(drive_materializer.drive_io, "write_text_atomic", _raise_unavailable)
    with pytest.raises(drive_io.DriveUnavailable):
        drive_materializer._write_swept_file("F3E", "2026-07-16", "digest body")


def test_flywheel_mirror_failsoft_on_outage(monkeypatch, tmp_path):
    """The DR flywheel mirror must never abort the run: a gone mount => [] mirrored."""
    # Create one local source ledger so src.exists() is True and a write is attempted.
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "cora-reply-log.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(drive_materializer.drive_io, "write_bytes_atomic", _raise_unavailable)
    mirrored = drive_materializer.mirror_flywheel_ledgers(repo_root=tmp_path)
    assert mirrored == []


# ── nudge_ledger (daily) ─────────────────────────────────────────────────────

def test_recently_nudged_failopen_on_outage(monkeypatch):
    """Fail-open: an outage reads as 'not recently nudged' (never blocks a nudge)."""
    monkeypatch.setattr(nudge_ledger.drive_io, "exists", _raise_unavailable)
    assert nudge_ledger.recently_nudged("task-123") is False


def test_record_nudge_failopen_on_outage(monkeypatch):
    monkeypatch.setattr(nudge_ledger.drive_io, "exists", _raise_unavailable)
    monkeypatch.setattr(nudge_ledger.drive_io, "append_text", _raise_unavailable)
    assert nudge_ledger.record_nudge("task-123", task_name="x") is False


def test_safe_exists_failopen_on_outage(monkeypatch):
    monkeypatch.setattr(nudge_ledger.drive_io, "exists", _raise_unavailable)
    assert nudge_ledger._safe_exists(nudge_ledger._log_path()) is False
