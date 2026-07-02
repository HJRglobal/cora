"""kb_miss calibration instrumentation (D-066 follow-up).

Pins the ORIGIN of the calibration data: context_loader._try_kb_retrieve must
populate kb_meta with the closest returned chunk's distance and the raw returned
count, BOTH regardless of the _KB_MAX_DISTANCE (1.30) gate. This is the data
that later lets kb_miss be recalibrated to a distance FLOOR (kb_miss currently
requires 0 relevant hits, empirically unreachable at ~560K chunks). The
consumption side (gap record + decision log) is pinned in test_gap_detection.py.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

_SRC = str(Path(__file__).resolve().parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import cora.context_loader as cl  # noqa: E402
from cora.knowledge_base.store import SearchResult  # noqa: E402


def _result(distance: float, chunk_id: str = "c") -> SearchResult:
    return SearchResult(
        chunk_id=chunk_id,
        source="asana",
        source_id="s",
        entity="F3E",
        title="t",
        content="benign operational content",
        deep_link="",
        date_modified=None,
        distance=distance,
    )


def _wire_kb(monkeypatch, results):
    monkeypatch.setattr(cl, "_KB_DB_PATH", Path(__file__).resolve().parent)  # dir .exists()
    fake_kb = SimpleNamespace(search=lambda *a, **k: list(results))
    monkeypatch.setattr(cl, "get_shared_kb", lambda: fake_kb)


def test_best_distance_is_min_across_returned_chunks(monkeypatch):
    # Three chunks all UNDER the 1.30 gate; best_distance = the closest.
    _wire_kb(monkeypatch, [_result(1.20, "a"), _result(1.057, "b"), _result(1.11, "c")])
    meta: dict = {}
    cl._try_kb_retrieve("F3E", "how is the launch going", kb_meta=meta)
    assert meta["kb_search_ran"] is True
    assert meta["kb_relevant_hits"] == 3
    assert meta["kb_chunks_returned"] == 3
    assert meta["kb_best_distance"] == 1.057  # min, not results[0]


def test_fields_set_even_when_zero_pass_the_gate(monkeypatch):
    # The exact kb_miss scenario: chunks returned but NONE under 1.30. The
    # calibration fields must still be populated (0 relevant hits, but the
    # closest chunk's distance is the number kb_miss will eventually gate on).
    monkeypatch.setattr(cl, "_try_cross_entity_fallback", lambda *a, **k: None)
    _wire_kb(monkeypatch, [_result(1.45, "a"), _result(1.50, "b")])
    meta: dict = {}
    cl._try_kb_retrieve("F3E", "official policy on office plant watering", kb_meta=meta)
    assert meta["kb_search_ran"] is True
    assert meta["kb_relevant_hits"] == 0
    assert meta["kb_chunks_returned"] == 2
    assert meta["kb_best_distance"] == 1.45


def test_best_distance_none_when_no_chunks_returned(monkeypatch):
    monkeypatch.setattr(cl, "_try_cross_entity_fallback", lambda *a, **k: None)
    _wire_kb(monkeypatch, [])
    meta: dict = {}
    cl._try_kb_retrieve("F3E", "anything at all here", kb_meta=meta)
    assert meta["kb_search_ran"] is True
    assert meta["kb_chunks_returned"] == 0
    assert meta["kb_best_distance"] is None


def test_distance_is_rounded_to_4dp(monkeypatch):
    _wire_kb(monkeypatch, [_result(1.0571234, "a")])
    meta: dict = {}
    cl._try_kb_retrieve("F3E", "some substantive question", kb_meta=meta)
    assert meta["kb_best_distance"] == 1.0571
