"""The shared D-171 timing helper (tests/_timing.py) measures, it never loosens.

D-051 integration-tests-6: single-run timing bounds flaked under concurrent host
load, so the branch's linear-cost pins read the best of 3 through one helper. These
tests pin that the helper times every call for real and returns their minimum, with
no averaging or scaling, so a converted test keeps its bound's full strength.
"""
from __future__ import annotations

import time

import pytest

import _timing
from _timing import best_of_3


class _Clock:
    """A fake perf_counter: each call returns the next tick."""

    def __init__(self, ticks):
        self.ticks = list(ticks)

    def __call__(self):
        return self.ticks.pop(0)


def test_returns_the_minimum_of_three_real_measurements(monkeypatch):
    # three runs costing 0.30s, 0.10s and 0.20s -> the helper reports 0.10s
    monkeypatch.setattr(_timing.time, "perf_counter",
                        _Clock([0.0, 0.30, 1.0, 1.10, 2.0, 2.20]))
    assert best_of_3(lambda: None) == pytest.approx(0.10)


def test_calls_fn_exactly_three_times_with_its_arguments():
    calls = []
    best_of_3(lambda *a, **k: calls.append((a, k)), 1, "x", key="v")
    assert calls == [((1, "x"), {"key": "v"})] * 3


def test_a_function_slower_than_the_bound_on_every_run_still_fails_the_bound():
    """The load-noise fix must not hide a real cost: when EVERY run is slow, the
    minimum is slow too."""
    assert best_of_3(time.sleep, 0.02) >= 0.02


def test_an_exception_propagates_instead_of_being_timed_away():
    def boom():
        raise ValueError("real failure")

    with pytest.raises(ValueError):
        best_of_3(boom)
