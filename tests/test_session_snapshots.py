"""Tests for the session snapshot layer (SLIM session-comms, 2026-07-30).

Pins the load-bearing invariants:
  - atomic temp+replace writes, no .tmp leftovers
  - fail-soft per file: a failing render keeps the previous file AND its stamp
    (stale stamps are honest; never faked), and the tick continues
  - mirror fail-soft: a G: outage never affects the repo lane; retried later;
    only files whose updated_at advanced are re-mirrored
  - LEX redaction flows through code-queue.json (read-layer redaction pin)
  - known-answers index excludes LEX sub-entities and never carries contents
  - index.json catalog completeness + restart stamp bootstrap
  - flywheel snapshot is a pure read (update_baseline never set)
  - the daemon loop survives a poisoned tick
  - repo hygiene pins: data/session-bus/ gitignored; the plugin's .mcp.json
    is NOT swallowed by the root ignore (anchored /.mcp.json)
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from cora import code_queue as cq
from cora import drive_io
from cora import session_snapshots as ss

_REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def snap_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CORA_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    monkeypatch.setenv("CORA_SNAPSHOT_MIRROR_DIR", str(tmp_path / "mirror"))
    ss.reset_state_for_tests()
    drive_io.reset_state_for_tests()
    yield
    ss.reset_state_for_tests()
    drive_io.reset_state_for_tests()


def _fake_specs(monkeypatch, specs):
    monkeypatch.setattr(ss, "_SPECS", specs)


def _spec(name, render, cadence=0, description="test spec"):
    return {"name": name, "description": description, "cadence": cadence, "render": render}


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ─────────────────────────────────────────────────────────────────────────────
# Tick mechanics: writes, atomicity, cadence
# ─────────────────────────────────────────────────────────────────────────────
def test_tick_writes_files_and_index_atomically(tmp_path, monkeypatch):
    _fake_specs(monkeypatch, [
        _spec("a.json", lambda: {"v": 1}),
        _spec("b.json", lambda: {"v": 2}),
    ])
    results = ss.tick(force=True)
    d = ss._snapshot_dir()
    assert results == {"a.json": "written", "b.json": "written", "index.json": "written"}
    for name, v in (("a.json", 1), ("b.json", 2)):
        data = _read_json(d / name)
        assert data["v"] == v
        assert data["updated_at"]  # every file carries its stamp
    # No temp artifacts left behind (temp + os.replace, never in-place).
    assert list(d.glob("*.tmp")) == []
    idx = _read_json(d / "index.json")
    assert set(idx["files"]) == {"a.json", "b.json"}
    assert idx["files"]["a.json"]["updated_at"] == _read_json(d / "a.json")["updated_at"]


def test_atomic_overwrite_of_existing_file(monkeypatch):
    _fake_specs(monkeypatch, [_spec("a.json", lambda: {"v": 1})])
    ss.tick(force=True)
    first = _read_json(ss._snapshot_dir() / "a.json")
    ss.tick(force=True)
    second = _read_json(ss._snapshot_dir() / "a.json")
    assert second["updated_at"] >= first["updated_at"]
    assert list(ss._snapshot_dir().glob("*.tmp")) == []


def test_cadence_skips_until_due(monkeypatch):
    calls = {"n": 0}

    def render():
        calls["n"] += 1
        return {"n": calls["n"]}

    _fake_specs(monkeypatch, [
        _spec("hot.json", render, cadence=0),
        _spec("cold.json", render, cadence=99999),
    ])
    r1 = ss.tick()
    r2 = ss.tick()
    assert r1["hot.json"] == "written" and r1["cold.json"] == "written"
    assert r2["hot.json"] == "written"
    assert r2["cold.json"] == "fresh"  # cadence not elapsed -> untouched


# ─────────────────────────────────────────────────────────────────────────────
# Fail-soft: a failing render keeps the stale file + stamp, tick continues
# ─────────────────────────────────────────────────────────────────────────────
def test_failing_render_keeps_stale_file_and_stamp(monkeypatch):
    state = {"fail": False}

    def flaky():
        if state["fail"]:
            raise RuntimeError("poisoned render")
        return {"ok": True}

    _fake_specs(monkeypatch, [
        _spec("good.json", lambda: {"ok": True}),
        _spec("flaky.json", flaky),
    ])
    ss.tick(force=True)
    d = ss._snapshot_dir()
    stale = _read_json(d / "flaky.json")

    state["fail"] = True
    results = ss.tick(force=True)
    assert results["flaky.json"] == "failed"
    assert results["good.json"] == "written"      # tick continued past the failure
    assert results["index.json"] == "written"
    # The previous file AND its stamp survive untouched — honest staleness.
    assert _read_json(d / "flaky.json") == stale
    idx = _read_json(d / "index.json")
    assert idx["files"]["flaky.json"]["updated_at"] == stale["updated_at"]
    assert idx["files"]["good.json"]["updated_at"] > stale["updated_at"]


def test_failed_render_retries_next_tick(monkeypatch):
    state = {"fail": True}

    def flaky():
        if state["fail"]:
            raise RuntimeError("down")
        return {"ok": True}

    _fake_specs(monkeypatch, [_spec("flaky.json", flaky, cadence=99999)])
    assert ss.tick()["flaky.json"] == "failed"
    # A failure never advances _last_success, so the next tick retries even
    # though the cadence has not elapsed.
    state["fail"] = False
    assert ss.tick()["flaky.json"] == "written"


def test_loop_survives_poisoned_tick(monkeypatch):
    monkeypatch.setattr(ss, "tick", lambda **k: (_ for _ in ()).throw(RuntimeError("boom")))
    stop = threading.Event()
    stop.set()  # run exactly one iteration
    ss._snapshot_loop(stop, interval=0)  # must return, not raise


# ─────────────────────────────────────────────────────────────────────────────
# Restart honesty: stamps bootstrap from disk, never fabricated
# ─────────────────────────────────────────────────────────────────────────────
def test_index_bootstraps_stamp_from_disk_after_restart(monkeypatch):
    _fake_specs(monkeypatch, [_spec("a.json", lambda: {"v": 1})])
    ss.tick(force=True)
    stamp = _read_json(ss._snapshot_dir() / "a.json")["updated_at"]

    ss.reset_state_for_tests()  # simulate a process restart
    idx = ss._render_index()
    assert idx["files"]["a.json"]["updated_at"] == stamp


def test_index_reports_null_stamp_for_never_written_file(monkeypatch):
    _fake_specs(monkeypatch, [_spec("never.json", lambda: {})])
    idx = ss._render_index()
    assert idx["files"]["never.json"]["updated_at"] is None


# ─────────────────────────────────────────────────────────────────────────────
# Mirror: change-gated, fail-soft, retried
# ─────────────────────────────────────────────────────────────────────────────
def test_mirror_pushes_exact_bytes_and_skips_unchanged(monkeypatch):
    _fake_specs(monkeypatch, [
        _spec("hot.json", lambda: {"v": 1}, cadence=0),
        _spec("cold.json", lambda: {"v": 2}, cadence=99999),
    ])
    pushed: list[str] = []
    real_write = drive_io.write_text_atomic

    def spy(path, text, **kwargs):
        pushed.append(Path(path).name)
        return real_write(path, text, **kwargs)

    monkeypatch.setattr(ss.drive_io, "write_text_atomic", spy)

    ss.tick(force=True)
    assert sorted(pushed) == ["cold.json", "hot.json", "index.json"]
    # Mirror carries the exact local bytes (one serialization, one truth).
    for name in ("hot.json", "cold.json", "index.json"):
        local = (ss._snapshot_dir() / name).read_text(encoding="utf-8")
        mirrored = (ss._mirror_dir() / name).read_text(encoding="utf-8")
        assert mirrored == local

    pushed.clear()
    ss.tick()
    # Only files whose updated_at advanced are re-pushed: hot (cadence 0) and
    # index (rewritten every tick); cold stays fresh -> not re-mirrored.
    assert sorted(pushed) == ["hot.json", "index.json"]


def test_mirror_unavailable_never_affects_repo_lane(monkeypatch):
    _fake_specs(monkeypatch, [_spec("a.json", lambda: {"v": 1})])

    def gone(path, text, **kwargs):
        raise drive_io.DriveUnavailable("G: mount gone")

    monkeypatch.setattr(ss.drive_io, "write_text_atomic", gone)
    results = ss.tick(force=True)
    assert results["a.json"] == "written"          # repo lane unaffected
    assert (ss._snapshot_dir() / "a.json").exists()
    assert not (ss._mirror_dir() / "a.json").exists()
    assert ss._last_mirrored == {}                 # nothing falsely marked mirrored


def test_mirror_retries_after_outage(monkeypatch):
    _fake_specs(monkeypatch, [_spec("a.json", lambda: {"v": 1}, cadence=99999)])
    state = {"fail": True}
    pushed: list[str] = []

    def flaky_write(path, text, **kwargs):
        if state["fail"]:
            raise drive_io.DriveUnavailable("blip")
        pushed.append(Path(path).name)

    monkeypatch.setattr(ss.drive_io, "write_text_atomic", flaky_write)
    ss.tick(force=True)                            # mirror pass fails (outage)
    assert ss._last_mirrored == {}

    state["fail"] = False
    ss._mirror_pass()                              # e.g. the next tick's pass
    assert sorted(pushed) == ["a.json", "index.json"]
    assert set(ss._last_mirrored) == {"a.json", "index.json"}


# ─────────────────────────────────────────────────────────────────────────────
# Content posture: LEX redaction, known-answers exclusion, flywheel pure read
# ─────────────────────────────────────────────────────────────────────────────
def test_code_queue_snapshot_redacts_lex_items():
    """Read-layer redaction pin: even a legacy raw-LEX row in the event ledger
    (pre-parity-raise) must never surface raw title/summary/fix_sketch in the
    snapshot — code-queue.json inherits load_items/_lex_safe_view + the
    render-side re-check by construction."""
    row = {
        "event": "captured", "id": "cq-lextest0001",
        "ts": "2026-07-30T00:00:00+00:00", "status": "PROPOSED",
        "entity": "LEX-LLC", "kind": "bug", "severity": "HIGH",
        "title": "Marcus Alvarez billing authorization lookup",
        "summary": "raw sensitive summary", "fix_sketch": "raw fix detail",
    }
    cq._EVENT_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    cq._EVENT_LEDGER.write_text(json.dumps(row) + "\n", encoding="utf-8")

    payload = ss._render_code_queue()
    assert "Marcus" not in payload["backlog"]
    assert "raw sensitive summary" not in payload["backlog"]
    assert cq._LEX_REDACTED_TITLE in payload["backlog"]
    assert payload["provenance"]  # injection framing rides along


def test_known_answers_index_excludes_lex_subentities_and_contents(tmp_path, monkeypatch):
    from cora import context_loader as cl

    ka_dir = tmp_path / "ka"
    ka_dir.mkdir()
    (ka_dir / "f3e.md").write_text("SECRET-KNOWN-ANSWER-CONTENT", encoding="utf-8")
    monkeypatch.setattr(cl, "_KNOWN_ANSWERS_DIR", ka_dir)

    payload = ss._render_known_answers_index()
    entities = payload["entities"]
    assert not any(e.startswith("LEX-") for e in entities)  # sub-entities excluded
    assert "LEX" in entities                                 # GM level stays exposed
    assert entities["F3E"]["exists"] is True
    assert entities["F3E"]["size_bytes"] == len("SECRET-KNOWN-ANSWER-CONTENT")
    assert entities["F3E"]["modified"]
    assert entities["OSN"]["exists"] is False                # absent file, honest
    # An INDEX only: file contents must never appear anywhere in the payload.
    assert "SECRET-KNOWN-ANSWER-CONTENT" not in json.dumps(payload)


def test_known_answers_index_drive_unavailable_is_unknown_not_absent(monkeypatch):
    def gone(path, **kwargs):
        raise drive_io.DriveUnavailable("mount gone")

    monkeypatch.setattr(ss.drive_io, "stat_info", gone)
    payload = ss._render_known_answers_index()
    assert all(v["exists"] is None for v in payload["entities"].values())


def test_flywheel_snapshot_is_a_pure_read(monkeypatch):
    import cora.flywheel_metrics as fm

    calls: list[tuple] = []

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return {"available": True, "pending_total": 3}

    monkeypatch.setattr(fm, "collect", spy)
    payload = ss._render_flywheel()
    assert payload["metrics"]["pending_total"] == 3
    # update_baseline must stay at its False default — the baseline history
    # belongs to the scheduled monitor, never to a snapshot read.
    assert calls == [((), {})]


def test_status_snapshot_shape_and_alive_threshold(monkeypatch):
    import cora.health_endpoint as he
    import cora.mcp_server as ms

    monkeypatch.setattr(he, "heartbeat_age_seconds", lambda: 12.0)
    monkeypatch.setattr(ms, "_read_uptime_from_log", lambda: 345)
    monkeypatch.setattr(ms, "_read_task_last_results",
                        lambda: {"Cora - Foo": ("Ready", 0)})
    out = ss._render_status()
    assert out["writer_alive"] is True
    assert out["alive"] is True
    assert out["heartbeat_age_seconds"] == 12.0
    assert out["uptime_seconds"] == 345
    assert out["task_results"] == {"Cora - Foo": {"state": "Ready", "last_result": 0}}

    ss.reset_state_for_tests()
    monkeypatch.setattr(he, "heartbeat_age_seconds", lambda: 9999.0)
    assert ss._render_status()["alive"] is False


def test_task_results_are_ttl_cached(monkeypatch):
    import cora.mcp_server as ms

    calls = {"n": 0}

    def counted():
        calls["n"] += 1
        return {}

    monkeypatch.setattr(ms, "_read_task_last_results", counted)
    ss._cached_task_results()
    ss._cached_task_results()
    assert calls["n"] == 1  # the bounded-45s PowerShell query never runs per tick


# ─────────────────────────────────────────────────────────────────────────────
# Catalog + integration + repo hygiene pins
# ─────────────────────────────────────────────────────────────────────────────
def test_spec_catalog_is_complete_and_described():
    names = {s["name"] for s in ss._SPECS}
    assert names == {"status.json", "code-queue.json", "flywheel.json",
                     "known-answers-index.json"}
    for spec in ss._SPECS:
        assert spec["description"]
        assert isinstance(spec["cadence"], int)
        assert callable(spec["render"])
    idx = ss._render_index()
    assert set(idx["files"]) == names
    assert all(f["cadence_seconds"] > 0 for f in idx["files"].values())


def test_full_tick_with_real_specs(tmp_path, monkeypatch):
    """End-to-end over the REAL spec table with the heavy externals stubbed:
    all five files land, valid JSON, stamped, no temp leftovers."""
    import cora.context_loader as cl
    import cora.flywheel_metrics as fm
    import cora.health_endpoint as he
    import cora.mcp_server as ms

    monkeypatch.setattr(he, "heartbeat_age_seconds", lambda: 5.0)
    monkeypatch.setattr(ms, "_read_uptime_from_log", lambda: 60)
    monkeypatch.setattr(ms, "_read_task_last_results", lambda: {})
    monkeypatch.setattr(fm, "collect", lambda: {"available": True})
    ka_dir = tmp_path / "ka"
    ka_dir.mkdir()
    monkeypatch.setattr(cl, "_KNOWN_ANSWERS_DIR", ka_dir)
    cq._EVENT_LEDGER.parent.mkdir(parents=True, exist_ok=True)

    results = ss.tick(force=True)
    assert set(results) == {"status.json", "code-queue.json", "flywheel.json",
                            "known-answers-index.json", "index.json"}
    assert all(v == "written" for v in results.values())
    d = ss._snapshot_dir()
    for name in results:
        data = _read_json(d / name)
        assert data["updated_at"]
    assert list(d.glob("*.tmp")) == []
    # Mirror carried everything (local tmp mirror dir).
    for name in results:
        assert (ss._mirror_dir() / name).exists()


def test_session_bus_dir_is_gitignored():
    text = (_REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "data/session-bus/" in text


def test_root_mcp_json_ignore_is_anchored():
    """The root .mcp.json (machine-specific) is ignored ANCHORED — an unanchored
    `.mcp.json` pattern would silently swallow the committed plugin source at
    deployment/cora-tools-plugin/.mcp.json (the bug this pin guards against)."""
    lines = (_REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "/.mcp.json" in lines
    assert ".mcp.json" not in lines  # the unanchored form must never come back
