"""Slice 2-3: cora_health_report parses kb_ms lines -> warm p50/p95 per entity + alarm."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "cora_health_report", _REPO / "scripts" / "cora_health_report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


chr = _load()

# A real log line (em-dash + a hyphenated entity code).
_LINE = ("2026-07-28 09:00:00 INFO cora.context_loader: KB retrieved 5 chunks "
         "(of 12 returned) for entity={ent} — best distance=0.512 kb_ms={ms}\n")


def _write_log(tmp_path, samples: dict[str, list[int]]):
    """samples: {entity: [ms, ...]} -> one cora-*.log file."""
    lines = []
    for ent, mss in samples.items():
        for ms in mss:
            lines.append(_LINE.format(ent=ent, ms=ms))
    (tmp_path / "cora-2026-07-28.log").write_text("".join(lines), encoding="utf-8")


def test_regex_parses_hyphenated_entity():
    m = chr._KB_MS_RE.search(_LINE.format(ent="LEX-LLC", ms=1234))
    assert m and m.group(1) == "LEX-LLC" and m.group(2) == "1234"


def test_kb_latency_percentiles(tmp_path, monkeypatch):
    monkeypatch.setattr(chr, "LOGS_DIR", tmp_path)
    _write_log(tmp_path, {"F3E": [10, 20, 30, 40, 100], "LEX": [500, 600]})
    out = chr.kb_latency(log_days=7)
    assert out["samples"] == 7
    assert out["by_entity"]["F3E"]["n"] == 5
    assert out["by_entity"]["F3E"]["p50"] == 30          # nearest-rank median of 5
    assert out["by_entity"]["F3E"]["p95"] == 100
    assert out["by_entity"]["LEX"]["n"] == 2
    assert out["overall_p95"] >= 100


def test_kb_latency_no_samples(tmp_path, monkeypatch):
    monkeypatch.setattr(chr, "LOGS_DIR", tmp_path)
    (tmp_path / "cora-2026-07-28.log").write_text("no kb lines here\n", encoding="utf-8")
    out = chr.kb_latency(log_days=7)
    assert out == {"samples": 0, "overall_p50": 0, "overall_p95": 0, "by_entity": {}}


def test_alarm_fires_over_3s_with_enough_samples():
    report = {"kb_latency": {"by_entity": {"LEX": {"n": 10, "p50": 2000, "p95": 4200}}}}
    alarms = [a for a in chr.threshold_alarms(report) if "KB warm p95" in a]
    assert alarms and "LEX p95=4200ms" in alarms[0]


def test_alarm_suppressed_below_sample_floor():
    # p95 huge but only 3 samples -> no alarm (cold-outlier guard).
    report = {"kb_latency": {"by_entity": {"OSN": {"n": 3, "p50": 5000, "p95": 9000}}}}
    assert not [a for a in chr.threshold_alarms(report) if "KB warm p95" in a]


def test_alarm_clear_under_threshold():
    report = {"kb_latency": {"by_entity": {"F3E": {"n": 50, "p50": 900, "p95": 1800}}}}
    assert not [a for a in chr.threshold_alarms(report) if "KB warm p95" in a]
