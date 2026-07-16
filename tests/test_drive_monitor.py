"""Slice 4: the G: reachability breadcrumb (main._drive_reachability_monitor).

Observability only -- it logs available<->unavailable transitions and NEVER gates the
bot or raises. Uses the `cora` import tree to match the code.
"""

from __future__ import annotations

import logging
import threading

from cora import main


def test_log_transition_state_machine():
    log = logging.getLogger("drive-monitor-test")
    # startup states
    assert main._log_drive_transition(None, True, log) is True
    assert main._log_drive_transition(None, False, log) is False
    # flips
    assert main._log_drive_transition(True, False, log) is False   # LOST
    assert main._log_drive_transition(False, True, log) is True    # RECOVERED
    # steady state (no flip)
    assert main._log_drive_transition(True, True, log) is True
    assert main._log_drive_transition(False, False, log) is False


def test_startup_available_logs_info(caplog):
    log = logging.getLogger("drive-monitor-test")
    with caplog.at_level(logging.INFO, logger="drive-monitor-test"):
        main._log_drive_transition(None, True, log)
    assert any("AVAILABLE at startup" in r.message for r in caplog.records)


def test_lost_transition_logs_warning(caplog):
    log = logging.getLogger("drive-monitor-test")
    with caplog.at_level(logging.WARNING, logger="drive-monitor-test"):
        main._log_drive_transition(True, False, log)
    assert any("LOST" in r.message for r in caplog.records)


def test_monitor_runs_one_iteration_then_exits_on_stop():
    """One probe, one transition log, then a clean exit when the stop event is set."""
    stop = threading.Event()
    stop.set()  # so stop.wait() returns True immediately after the first iteration
    calls = {"n": 0}

    def _probe():
        calls["n"] += 1
        return True

    main._drive_reachability_monitor(
        logging.getLogger("drive-monitor-test"), stop, probe=_probe, interval=0
    )
    assert calls["n"] == 1


def test_monitor_swallows_probe_error():
    """A probe that raises must not crash the breadcrumb (observability, never gates)."""
    stop = threading.Event()
    stop.set()

    def _probe():
        raise RuntimeError("simulated probe failure")

    # No exception should escape.
    main._drive_reachability_monitor(
        logging.getLogger("drive-monitor-test"), stop, probe=_probe, interval=0
    )


def test_monitor_does_not_gate_and_is_daemon_startable():
    """A real (bounded) probe run in a daemon thread completes and exits on stop --
    proving the monitor never blocks process startup/shutdown."""
    stop = threading.Event()
    t = threading.Thread(
        target=main._drive_reachability_monitor,
        args=(logging.getLogger("drive-monitor-test"), stop),
        kwargs={"interval": 0.05},
        daemon=True,
    )
    t.start()
    stop.set()
    t.join(timeout=15)
    assert not t.is_alive(), "drive monitor did not exit promptly on stop"
