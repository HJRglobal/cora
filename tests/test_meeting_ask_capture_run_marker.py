"""Code #14 R14-7a: 'Cora - Meeting Ask Capture' writes a run marker.

Before this slice the task had NO row in data/maps/scheduled-task-state.yaml and
its script never called run_marker.write -- so a lane firing every 15 minutes that
silently stopped would never have been noticed. A registry row alone would have
been WORSE (check_run_markers WARNs 'no run marker ever recorded' forever once the
`registered` window passes), so the row and the writer ship together.

Every test drives the REAL main() over tmp state: the Fireflies fetch and the Slack
client are the only things stubbed (no network), the run-marker ledger is the
conftest-redirected TASK_RUNS_LEDGER_PATH, and the script's watermark is pointed at
tmp so a live-shape run can never touch data/state/meeting-ask-watermark.json.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "run_meeting_ask_capture.py"
TASK = "Cora - Meeting Ask Capture"


def _load():
    spec = importlib.util.spec_from_file_location("_mac_run_marker", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _NoPostClient:
    """Constructed on a live run; the empty/stubbed windows below never post."""

    def __init__(self, token: str = "", **_kw):
        self.token = token


@pytest.fixture()
def mac(tmp_path, monkeypatch):
    m = _load()
    # env AFTER the module import (its load_dotenv(override=True) runs at import)
    monkeypatch.setenv("FIREFLIES_API_KEY", "ff-test-key")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test-token")
    monkeypatch.setattr(m, "_WATERMARK_PATH", tmp_path / "meeting-ask-watermark.json")
    monkeypatch.setattr(m.ffc, "_load_email_to_slack", lambda: {})
    monkeypatch.setattr(m, "_fetch_since", lambda since, n: [])
    monkeypatch.setattr(m, "_fetch_one", lambda tid: [])
    import slack_sdk
    monkeypatch.setattr(slack_sdk, "WebClient", _NoPostClient)
    return m


def _markers() -> list[dict]:
    from cora import run_marker
    return [r for r in run_marker.read_markers() if r.get("task") == TASK]


def test_ledger_is_the_conftest_redirect(tmp_path):
    """The writer's path resolves per call from TASK_RUNS_LEDGER_PATH, which the
    autouse conftest fixture points at tmp -- the suite can never append to the
    real logs/task-runs.jsonl."""
    from cora import run_marker
    assert str(run_marker.ledger_path()).startswith(str(tmp_path))


def test_live_run_writes_exactly_one_ok_marker(mac):
    assert mac.main([]) == 0
    rows = _markers()
    assert len(rows) == 1
    r = rows[0]
    assert r["ok"] is True and r["outputs"] == 0 and r["outcome"] == "nothing-new"
    assert r["script"] == "run_meeting_ask_capture.py"
    assert "transcripts=0" in r["detail"]
    assert mac.RUN_MARKER_TASK == TASK


def test_live_run_counts_asks_and_recaps_actually_carded(mac, monkeypatch):
    """outputs = ask cards + recap cards that were actually posted (never a constant)."""
    base = {"asks": 3, "carded": 2, "skipped_dup": 1, "no_recipient": 0,
            "overflow": 0, "phi_skipped": 0, "excluded": ""}
    monkeypatch.setattr(mac, "_fetch_since", lambda since, n: [{"id": "T-1", "title": "x"}])
    monkeypatch.setattr(mac, "process_transcript", lambda t, **kw: dict(base))
    monkeypatch.setattr(mac, "process_recap", lambda t, **kw: {"carded": 1})
    assert mac.main([]) == 0
    (r,) = _markers()
    assert r["ok"] is True and r["outputs"] == 3 and r["outcome"] == "carded"
    assert "carded=2" in r["detail"] and "recap_carded=1" in r["detail"]


def test_marker_detail_never_carries_a_meeting_title(mac, monkeypatch):
    """D-145: task-runs.jsonl is a shared ledger -- counts only, never a title."""
    title = "Quartz Lantern Weekly Sync"
    monkeypatch.setattr(mac, "_fetch_since", lambda since, n: [{"id": "T-2", "title": title}])
    monkeypatch.setattr(mac, "process_transcript",
                        lambda t, **kw: {"asks": 0, "carded": 0, "skipped_dup": 0,
                                         "no_recipient": 0, "overflow": 0,
                                         "phi_skipped": 0, "excluded": ""})
    monkeypatch.setattr(mac, "process_recap", lambda t, **kw: {"carded": 0})
    assert mac.main([]) == 0
    (r,) = _markers()
    assert title not in json.dumps(r)


def test_dry_run_writes_no_marker(mac):
    assert mac.main(["--dry-run"]) == 0
    assert _markers() == []


def test_transcript_id_probe_writes_no_marker(mac):
    assert mac.main(["--transcript-id", "T-probe"]) == 0
    assert _markers() == []


def test_fireflies_fetch_failure_writes_ok_false(mac, monkeypatch):
    def boom(since, n):
        raise RuntimeError("upstream 502 with a body we must not log")
    monkeypatch.setattr(mac, "_fetch_since", boom)
    assert mac.main([]) == 1
    (r,) = _markers()
    assert r["ok"] is False and r["outcome"] == "fireflies_error"
    assert "RuntimeError" in r["detail"] and "502" not in r["detail"]


def test_fireflies_fetch_failure_under_dry_run_writes_nothing(mac, monkeypatch):
    def boom(since, n):
        raise RuntimeError("down")
    monkeypatch.setattr(mac, "_fetch_since", boom)
    assert mac.main(["--dry-run"]) == 1
    assert _markers() == []


def test_missing_slack_token_writes_ok_false(mac, monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    assert mac.main([]) == 1
    (r,) = _markers()
    assert r["ok"] is False and r["outcome"] == "no_slack_token"


def test_missing_fireflies_key_writes_ok_false_live_only(mac, monkeypatch):
    monkeypatch.delenv("FIREFLIES_API_KEY", raising=False)
    assert mac.main(["--dry-run"]) == 1
    assert _markers() == []
    assert mac.main([]) == 1
    (r,) = _markers()
    assert r["ok"] is False and r["outcome"] == "no_fireflies_key"


# ── the registry row ─────────────────────────────────────────────────────────
def _registry():
    sys.path.insert(0, str(REPO / "scripts"))
    import nightly_health_check as nhc  # noqa: PLC0415
    return nhc._load_run_marker_registry()


def test_registry_row_loads_with_cadence_12_and_no_output_expectation():
    rows = [r for r in _registry() if r["name"] == TASK]
    assert len(rows) == 1
    row = rows[0]
    assert row["cadence_hours"] == 12
    assert row["expects_output"] is False
    assert row.get("registered"), "a row without `registered` alarms at once"


def test_registry_row_and_a_fresh_marker_evaluate_clean():
    """End-to-end through the health lane's evaluator: a marker written 11h ago
    (the longest legitimate overnight gap, 20:08 -> 07:08) is NOT an alarm; one
    25h old is a MISSED FIRE."""
    from cora import run_marker
    reg = [r for r in _registry() if r["name"] == TASK]
    now = datetime(2026, 9, 24, 14, 8, tzinfo=timezone.utc)
    fresh = {TASK: {"ts": (now - timedelta(hours=11)).isoformat(), "ok": True, "outputs": 0}}
    assert run_marker.evaluate(reg, fresh, now=now) == []
    stale = {TASK: {"ts": (now - timedelta(hours=25)).isoformat(), "ok": True, "outputs": 0}}
    out = run_marker.evaluate(reg, stale, now=now)
    assert out and "MISSED FIRE" in out[0][1]


def test_task_is_listed_as_expected_enabled():
    import yaml
    data = yaml.safe_load((REPO / "data" / "maps" / "scheduled-task-state.yaml")
                          .read_text(encoding="utf-8"))
    assert TASK in (data.get("enabled") or [])
    assert TASK not in (data.get("disabled") or [])
