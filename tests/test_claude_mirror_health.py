"""S5 (2026-09-03, claude-workspace mirror): the health-lane wiring reads the
mirror's structured status.json and alarms on stale/missing-root/new-skill/
task-estate changes. Report, never absorb (D-214)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "src"))

import mirror_claude_workspace as mw  # noqa: E402
import nightly_health_check as nhc  # noqa: E402
import cora_health_report as chr  # noqa: E402


def _write_status(zk: Path, *, age_h: float = 1.0, **over):
    zk.mkdir(parents=True, exist_ok=True)
    at = (datetime.now(timezone.utc) - timedelta(hours=age_h)).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "at_utc": at, "at_az": "x",
        "roots_missing": [], "quarantined_count": 3, "unknown_skills": [],
        "warns": [], "unpinned": ["task-a"], "added": [], "removed": [],
        "model_changed": [], "counts": {},
    }
    payload.update(over)
    (zk / "mirror-status.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture()
def zk(tmp_path, monkeypatch):
    d = tmp_path / "zone-k"
    monkeypatch.setenv("CORA_MIRROR_ZONE_K_ROOT", str(d))
    from cora import drive_io
    drive_io.reset_state_for_tests()
    return d


# ── read_parity_status ────────────────────────────────────────────────────────
def test_read_parity_status_fresh(zk):
    _write_status(zk, age_h=2.0)
    st = mw.read_parity_status()
    assert st["available"] and not st["stale"] and st["age_hours"] < 3


def test_read_parity_status_stale(zk):
    _write_status(zk, age_h=48.0)
    st = mw.read_parity_status()
    assert st["available"] and st["stale"]


def test_read_parity_status_absent(zk):
    st = mw.read_parity_status()
    assert st["available"] is False and "error" in st


# ── nightly_health_check.check_claude_mirror ──────────────────────────────────
def test_nhc_ok_when_no_status(zk):
    r = nhc.check_claude_mirror()
    assert r.status == "ok" and "not yet run" in r.detail.lower()


def test_nhc_ok_when_fresh(zk):
    _write_status(zk, age_h=1.0)
    r = nhc.check_claude_mirror()
    assert r.status == "ok" and "fresh" in r.detail


def test_nhc_warns_on_stale(zk):
    _write_status(zk, age_h=48.0)
    r = nhc.check_claude_mirror()
    assert r.status == "warn" and "ago" in r.detail


def test_nhc_warns_on_missing_root(zk):
    _write_status(zk, roots_missing=["cowork_memory"])
    r = nhc.check_claude_mirror()
    assert r.status == "warn" and "cowork_memory" in r.detail


def test_nhc_warns_on_unknown_skill_and_task_delta(zk):
    _write_status(zk, unknown_skills=["brand-new"], added=["new-task"])
    r = nhc.check_claude_mirror()
    assert r.status == "warn"
    assert "brand-new" in r.detail and "new-task" in r.detail


# ── cora_health_report.claude_mirror_section + alarms ─────────────────────────
def test_report_section_and_alarm(zk):
    _write_status(zk, age_h=48.0, roots_missing=["skills"])
    section = chr.claude_mirror_section()
    assert section["available"] and section["stale"] and section["roots_missing"] == ["skills"]
    alarms = chr.threshold_alarms({"claude_mirror": section})
    assert any("claude mirror" in a for a in alarms)


def test_report_section_unavailable_no_alarm(zk):
    section = chr.claude_mirror_section()
    assert section["available"] is False
    alarms = chr.threshold_alarms({"claude_mirror": section})
    assert not any("claude mirror" in a for a in alarms)
