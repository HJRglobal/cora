"""Tests for drive_io -- the resilient G:-mount I/O wrapper (2026-07-16).

Covers the three contract guarantees and the happy-path invariant:
  * happy path is a pass-through (same value as raw pathlib),
  * a genuine file-level error while the mount is UP is preserved (NOT reclassified),
  * a mount-gone condition (WinError 21/53/67 OR a hung call OR any OSError with the
    anchor unreachable) raises DriveUnavailable after a BOUNDED wait -- never hangs,
  * the circuit breaker fast-fails during a sustained outage.

The mount anchor is monkeypatched in every reachability-dependent test so results are
deterministic regardless of whether the test host actually has G: mounted.
"""

from __future__ import annotations

import threading
import time

import pytest

# Import from `cora` (not `src.cora`) so this test exercises the SAME module object
# the production code imports (`from cora import drive_io`); the two import paths are
# distinct module trees with distinct DriveUnavailable classes.
from cora import drive_io


@pytest.fixture(autouse=True)
def _reset_breaker():
    """The circuit breaker is process-global; reset around every test."""
    drive_io.reset_state_for_tests()
    yield
    drive_io.reset_state_for_tests()


@pytest.fixture
def mount_up(tmp_path, monkeypatch):
    """Point the mount anchor at a real existing dir => mount reads as reachable."""
    monkeypatch.setattr(drive_io, "MOUNT_ANCHOR", tmp_path)
    return tmp_path


@pytest.fixture
def mount_gone(tmp_path, monkeypatch):
    """Point the mount anchor at a non-existent path => mount reads as unreachable."""
    monkeypatch.setattr(drive_io, "MOUNT_ANCHOR", tmp_path / "no-such-drive-root")


# ── happy path: pass-through ─────────────────────────────────────────────────

def test_read_text_happy_path(mount_up):
    f = mount_up / "hello.md"
    f.write_text("hi there", encoding="utf-8")
    assert drive_io.read_text(f) == "hi there"


def test_read_bytes_happy_path(mount_up):
    f = mount_up / "blob.bin"
    f.write_bytes(b"\x00\x01\x02")
    assert drive_io.read_bytes(f) == b"\x00\x01\x02"


def test_exists_true_and_false_happy_path(mount_up):
    f = mount_up / "present.md"
    f.write_text("x", encoding="utf-8")
    assert drive_io.exists(f) is True
    assert drive_io.exists(mount_up / "absent.md") is False


def test_stat_mtime_happy_path(mount_up):
    f = mount_up / "dated.md"
    f.write_text("x", encoding="utf-8")
    mt = drive_io.stat_mtime(f, retry_seconds=0)
    assert isinstance(mt, float)
    assert mt == pytest.approx(f.stat().st_mtime, abs=1.0)


def test_stat_mtime_missing_returns_none_when_mount_up(mount_up):
    # Genuine absence with the mount up -> None, NOT DriveUnavailable.
    assert drive_io.stat_mtime(mount_up / "nope.md", retry_seconds=0) is None


def test_glob_happy_path(mount_up):
    (mount_up / "a.yaml").write_text("1", encoding="utf-8")
    (mount_up / "b.yaml").write_text("2", encoding="utf-8")
    (mount_up / "c.txt").write_text("3", encoding="utf-8")
    names = sorted(p.name for p in drive_io.glob(mount_up, "*.yaml"))
    assert names == ["a.yaml", "b.yaml"]


def test_write_text_atomic_happy_path(mount_up):
    dest = mount_up / "sub" / "out.md"
    drive_io.write_text_atomic(dest, "written")
    assert dest.read_text(encoding="utf-8") == "written"
    # No temp leftover.
    assert not (mount_up / "sub" / "out.md.drivetmp").exists()


def test_write_bytes_atomic_happy_path(mount_up):
    dest = mount_up / "out.bin"
    drive_io.write_bytes_atomic(dest, b"\xde\xad")
    assert dest.read_bytes() == b"\xde\xad"


# ── genuine file-level error while mount is UP is preserved ──────────────────

def test_read_text_missing_file_raises_filenotfound_not_driveunavailable(mount_up):
    with pytest.raises(FileNotFoundError):
        drive_io.read_text(mount_up / "missing.md", retry_seconds=0)


def test_non_mount_oserror_is_reraised_when_mount_up(mount_up, monkeypatch):
    """A non-mount OSError (e.g. PermissionError, winerror 5) with the mount up is
    re-raised as-is, never reclassified as DriveUnavailable."""
    def _boom():
        raise PermissionError(13, "Access is denied")

    with pytest.raises(PermissionError):
        drive_io._guarded(_boom, what="test", timeout=1.0, retry_seconds=0)


def test_non_oserror_is_reraised(mount_gone):
    """A non-OSError (decode error) is never treated as mount-gone, even if the
    anchor happens to be unreachable."""
    def _boom():
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")

    with pytest.raises(UnicodeDecodeError):
        drive_io._guarded(_boom, what="test", timeout=1.0, retry_seconds=0.05)


# ── mount gone -> DriveUnavailable ───────────────────────────────────────────

@pytest.mark.parametrize("winerror", [21, 53, 67])
def test_known_winerror_raises_driveunavailable_without_anchor_probe(
    winerror, mount_up, monkeypatch
):
    """A device/network WinError is mount-gone even when the anchor is reachable
    (the anchor probe is short-circuited for these unambiguous codes)."""
    monkeypatch.setattr(drive_io, "BACKOFF_SECONDS", 0.01)

    def _boom():
        err = OSError("device not ready")
        err.winerror = winerror
        raise err

    with pytest.raises(drive_io.DriveUnavailable):
        drive_io._guarded(_boom, what="test", timeout=1.0, retry_seconds=0.05)


def test_oserror_with_anchor_unreachable_is_driveunavailable(mount_gone, monkeypatch):
    monkeypatch.setattr(drive_io, "BACKOFF_SECONDS", 0.01)

    def _boom():
        raise FileNotFoundError(2, "No such file")

    with pytest.raises(drive_io.DriveUnavailable):
        drive_io._guarded(_boom, what="test", timeout=1.0, retry_seconds=0.05)


# ── the hang guarantee (invariant #1) ────────────────────────────────────────

def test_hung_op_returns_control_within_timeout(mount_gone, monkeypatch):
    """A hung op must NOT block the caller past ~timeout. This is the load-bearing
    guarantee: the incident froze because a G: read blocked the interpreter."""
    monkeypatch.setattr(drive_io, "BACKOFF_SECONDS", 0.01)
    release = threading.Event()

    def _hang():
        release.wait(30)  # simulate a blocked kernel read (releases the GIL)
        return "late"

    start = time.monotonic()
    try:
        with pytest.raises(drive_io.DriveUnavailable):
            # timeout 0.2s, retry window 0.1s -> one attempt, then give up
            drive_io._guarded(_hang, what="hang", timeout=0.2, retry_seconds=0.0)
        elapsed = time.monotonic() - start
        # Bounded: one 0.2s attempt + classification probe, well under the 30s hang.
        assert elapsed < 5.0, f"caller was blocked {elapsed:.1f}s -- not bounded"
    finally:
        release.set()  # let the abandoned worker exit


def test_hung_op_does_not_freeze_other_threads(mount_gone, monkeypatch):
    """While one thread is stuck in a hung G: read, an independent thread (proxy for
    the heartbeat) keeps making progress -- the interpreter is not frozen.

    NOTE: this demonstrates the GIL-RELEASING blocking case (Event.wait releases the
    GIL, as a real kernel FS read does), which is the case the wrapper actually
    recovers. The pathological GIL-NON-releasing hang is acknowledged as
    in-process-unrecoverable (see the drive_io module docstring) and is covered by the
    external cora-watchdog -- it is not what this test proves."""
    monkeypatch.setattr(drive_io, "BACKOFF_SECONDS", 0.01)
    release = threading.Event()
    beats = []
    beat_stop = threading.Event()

    def _hang():
        release.wait(30)
        return "late"

    def _heartbeat():
        while not beat_stop.wait(0.02):
            beats.append(time.monotonic())

    hb = threading.Thread(target=_heartbeat, daemon=True)
    hb.start()
    try:
        with pytest.raises(drive_io.DriveUnavailable):
            drive_io._guarded(_hang, what="hang", timeout=0.3, retry_seconds=0.0)
        # The heartbeat kept ticking throughout the hung read.
        assert len(beats) >= 3, f"heartbeat proxy starved: {len(beats)} beats"
    finally:
        beat_stop.set()
        release.set()


# ── circuit breaker ──────────────────────────────────────────────────────────

def test_breaker_fast_fails_after_outage(mount_gone, monkeypatch):
    """Once a mount-gone failure trips the breaker, the next call fast-fails WITHOUT
    invoking the op -- bounding worker-thread spawns during a sustained outage."""
    monkeypatch.setattr(drive_io, "BACKOFF_SECONDS", 0.01)

    def _boom():
        raise FileNotFoundError(2, "gone")

    with pytest.raises(drive_io.DriveUnavailable):
        drive_io._guarded(_boom, what="first", timeout=1.0, retry_seconds=0.05)

    # Breaker is now open. A second op must not even run.
    calls = {"n": 0}

    def _tracked():
        calls["n"] += 1
        return "ok"

    start = time.monotonic()
    with pytest.raises(drive_io.DriveUnavailable):
        drive_io._guarded(_tracked, what="second", timeout=1.0, retry_seconds=5.0)
    elapsed = time.monotonic() - start
    assert calls["n"] == 0, "op ran despite open breaker"
    assert elapsed < 1.0, "breaker-open path was not a fast fail"


def test_breaker_resets_on_success(mount_up):
    """A successful read ACTIVELY resets the breaker (not merely lets it time out).
    Set the window to a PAST monotonic value (expired, but the stored value is
    non-zero); a successful read must ZERO it via the success-path _breaker_reset().
    Teeth: deleting that reset would leave the stale negative value, not 0.0."""
    f = mount_up / "ok.md"
    f.write_text("ok", encoding="utf-8")
    drive_io._breaker_open_until = time.monotonic() - 1.0  # expired but non-zero
    assert not drive_io._breaker_is_open()
    assert drive_io.read_text(f, retry_seconds=0) == "ok"
    assert drive_io._breaker_open_until == 0.0


def test_breaker_trip_honors_hold_seconds(monkeypatch):
    """_breaker_trip holds the breaker open for max(BREAKER_SECONDS, hold_seconds), so
    a long-retry-window (scheduled) call keeps it open at least that long (D-051 Q5)."""
    monkeypatch.setattr(drive_io, "BREAKER_SECONDS", 5.0)
    drive_io._breaker_trip(hold_seconds=50.0)
    assert drive_io._breaker_open_until - time.monotonic() > 5.0  # held for the larger hold
    drive_io.reset_state_for_tests()
    drive_io._breaker_trip(hold_seconds=1.0)
    remaining = drive_io._breaker_open_until - time.monotonic()
    assert 3.5 < remaining <= 5.0  # held for BREAKER_SECONDS (the larger of the two)


def test_retry_then_recover(monkeypatch):
    """An op that fails mount-gone once then succeeds within the window returns the
    value and leaves the breaker CLOSED -- the 'ride over a ~30s remount' guarantee
    (D-051 GAP1). Neither always-succeed nor always-fail exercises this loop."""
    monkeypatch.setattr(drive_io, "BACKOFF_SECONDS", 0.01)
    calls = {"n": 0}

    def _op():
        calls["n"] += 1
        if calls["n"] == 1:
            err = OSError("device not ready")
            err.winerror = 21  # short-circuits classification -> mount-gone -> retry
            raise err
        return "recovered"

    val = drive_io._guarded(_op, what="test", timeout=1.0, retry_seconds=1.0)
    assert val == "recovered"
    assert calls["n"] == 2                       # failed once, then succeeded
    assert not drive_io._breaker_is_open()        # success reset the breaker


def test_guarded_retry_zero_is_single_attempt(mount_gone, monkeypatch):
    """retry_seconds=0 makes exactly ONE attempt (no retry). This is the invariant
    append_text relies on to guarantee no double-append (D-051 GAP3)."""
    monkeypatch.setattr(drive_io, "BACKOFF_SECONDS", 0.01)
    calls = {"n": 0}

    def _op():
        calls["n"] += 1
        raise FileNotFoundError(2, "gone")

    with pytest.raises(drive_io.DriveUnavailable):
        drive_io._guarded(_op, what="append", timeout=1.0, retry_seconds=0)
    assert calls["n"] == 1


def test_append_text_happy_path(mount_up):
    """append_text APPENDS (does not overwrite) and creates parents (D-051 GAP3)."""
    dest = mount_up / "sub" / "ledger.jsonl"
    drive_io.append_text(dest, '{"a": 1}\n')
    drive_io.append_text(dest, '{"b": 2}\n')
    assert dest.read_text(encoding="utf-8") == '{"a": 1}\n{"b": 2}\n'


# ── is_mount_available ───────────────────────────────────────────────────────

def test_is_mount_available_true(mount_up):
    assert drive_io.is_mount_available() is True


def test_is_mount_available_false(mount_gone):
    assert drive_io.is_mount_available() is False
