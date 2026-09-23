"""Shared D-171 timing helper for the linear-cost (ReDoS) pins.

A single wall-clock sample flakes when other suites share the host, which is the
normal state during a D-051 review or a slice gate. Host load only ever ADDS time,
so the MINIMUM of a few runs is the cleanest estimate of what the code itself costs.
A bound on that minimum keeps its full strength while the load noise drops out
(D-051 integration-tests-6). Callers keep their bounds exactly as they were.

Not a test module (no ``test_`` prefix, so pytest never collects it). Import it as
``from _timing import best_of_3``: under the suite's default ("prepend") import mode
``tests/`` is on sys.path, the same way sibling modules such as
``test_code_queue`` are imported today. Pinned by tests/test_timing_helper.py.
"""
from __future__ import annotations

import time
from typing import Any, Callable

RUNS = 3


def best_of_3(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> float:
    """Seconds taken by the fastest of three calls of ``fn(*args, **kwargs)``.

    Every call is timed in full; nothing is averaged, scaled or discounted. The
    result is never less than the fastest real run.
    """
    best = float("inf")
    for _ in range(RUNS):
        t0 = time.perf_counter()
        fn(*args, **kwargs)
        best = min(best, time.perf_counter() - t0)
    return best
