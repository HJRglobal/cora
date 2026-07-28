"""Asana Standard v1 Slice 6e: the weekly health report warns on any pinned
Cowork artifact missing from dashboard-access.yaml (register-or-purge)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]


def _load_health_module():
    spec = importlib.util.spec_from_file_location(
        "cora_health_report", _REPO / "scripts" / "cora_health_report.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


chr = _load_health_module()


def _drift_alarms(report: dict) -> list[str]:
    return [a for a in chr.threshold_alarms(report) if "dashboard drift" in a]


def test_drift_alarm_fires_on_unregistered():
    alarms = _drift_alarms({"dashboard_drift": {"available": True, "unregistered": ["new-secret-dash"]}})
    assert alarms and "new-secret-dash" in alarms[0]
    assert "1 pinned artifact" in alarms[0]


def test_no_drift_alarm_when_clean():
    assert _drift_alarms({"dashboard_drift": {"available": True, "unregistered": []}}) == []


def test_no_drift_alarm_when_section_unavailable():
    assert _drift_alarms({"dashboard_drift": {"available": False, "reason": "no dir"}}) == []
    assert _drift_alarms({}) == []


def test_drift_section_unions_all_buckets(tmp_path, monkeypatch):
    """A registered id from ANY bucket (covered_by_existing / utility / retired)
    must NOT be flagged; an unregistered artifact MUST be. Guards against a
    partial union false-positiving on legit artifacts."""
    # Two known-registered ids (one from covered_by_existing, one from utility,
    # one from retired) + one truly-unregistered id.
    for name in ("f3-ecom", "session-launcher", "f3-pure-tiktok-cockpit", "totally-unregistered-xyz"):
        d = tmp_path / name
        d.mkdir()
        (d / "index.html").write_text("<h1>x</h1>", encoding="utf-8")
    monkeypatch.setattr(chr, "_ARTIFACTS_DIR", tmp_path)
    out = chr.dashboard_drift_section()
    assert out["available"] is True
    assert out["unregistered"] == ["totally-unregistered-xyz"]


def test_drift_section_no_dir_is_no_op(tmp_path, monkeypatch):
    monkeypatch.setattr(chr, "_ARTIFACTS_DIR", tmp_path / "does-not-exist")
    out = chr.dashboard_drift_section()
    assert out["available"] is False


# NOTE: no test asserts zero drift against the REAL host OneDrive artifacts dir --
# that would couple the pytest gate to mutable host state outside the repo (a later
# unregistered artifact would fail the suite for reasons unrelated to any branch).
# Live drift is the weekly cora_health_report --slack alarm's job; the hermetic
# test_drift_section_unions_all_buckets above pins the mechanism.
