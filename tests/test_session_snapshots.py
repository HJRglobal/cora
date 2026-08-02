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


def _monotonic_stamps(monkeypatch):
    """Windows' datetime.now granularity (~15ms) can hand two back-to-back forced
    ticks IDENTICAL updated_at stamps — impossible at the 60s production cadence,
    but it breaks stamp-ordering/change-gating assertions in tests. Substitute a
    strictly increasing fake."""
    counter = {"n": 0}

    def fake():
        counter["n"] += 1
        return f"2026-07-31T00:00:{counter['n']:02d}.000000+00:00"

    monkeypatch.setattr(ss, "_utc_now_iso", fake)


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
    _monotonic_stamps(monkeypatch)
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
    _monotonic_stamps(monkeypatch)
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
    ss._mirror_cooldown_until = 0.0                # outage cooldown elapsed
    ss._mirror_pass()                              # e.g. a later tick's pass
    assert sorted(pushed) == ["a.json", "index.json"]
    assert set(ss._last_mirrored) == {"a.json", "index.json"}


def test_mirror_outage_starts_cooldown(monkeypatch):
    """D-051 loop-kill fix: after a DriveUnavailable, mirror attempts stand down
    for the cooldown window (in a hang-mode outage each attempt leaks an
    abandoned drive-io worker; a 60s cadence would defeat the 30s breaker)."""
    _fake_specs(monkeypatch, [_spec("a.json", lambda: {"v": 1})])
    attempts = {"n": 0}

    def gone(path, text, **kwargs):
        attempts["n"] += 1
        raise drive_io.DriveUnavailable("hang-mode")

    monkeypatch.setattr(ss.drive_io, "write_text_atomic", gone)
    ss.tick(force=True)
    assert attempts["n"] == 1                      # aborted after the first file
    assert ss._mirror_cooldown_until > 0
    ss.tick(force=True)                            # next tick: inside cooldown
    assert attempts["n"] == 1                      # no new attempt, no new leak

    ss._mirror_cooldown_until = 0.0                # cooldown elapsed
    ss.tick(force=True)
    assert attempts["n"] == 2                      # attempts resume


def test_mirror_withholds_index_while_a_data_file_push_fails(monkeypatch):
    """D-051 clobber fix: a per-file (non-outage) mirror failure must withhold
    the mirror index — otherwise the index claims freshness for a file that
    never landed (e.g. a sync-locked read-only target failing every tick)."""
    _fake_specs(monkeypatch, [
        _spec("stuck.json", lambda: {"v": 1}, cadence=99999),
        _spec("fine.json", lambda: {"v": 2}, cadence=99999),
    ])
    state = {"stuck_fails": True}
    pushed: list[str] = []
    real_write = drive_io.write_text_atomic

    def selective(path, text, **kwargs):
        name = Path(path).name
        if name == "stuck.json" and state["stuck_fails"]:
            raise PermissionError(5, "target sync-locked")
        pushed.append(name)
        return real_write(path, text, **kwargs)

    monkeypatch.setattr(ss.drive_io, "write_text_atomic", selective)
    ss.tick(force=True)
    assert "fine.json" in pushed                   # per-file failure: pass continues
    assert "index.json" not in pushed              # ...but the index is withheld
    assert "stuck.json" not in ss._last_mirrored

    state["stuck_fails"] = False
    ss._mirror_pass()                              # file recovers -> self-heals
    assert "stuck.json" in pushed
    assert "index.json" in pushed
    assert ss._last_mirrored["index.json"] == ss._last_stamp["index.json"]


def test_uptime_reads_newest_live_log_across_midnight(tmp_path):
    """D-051 fix pin: the live log keeps the process-START-date basename across
    midnight rollovers — uptime must come from the newest-mtime dated log, and a
    heartbeat-less same-day decoy file must fall through to the real one."""
    import os as _os

    import cora.mcp_server as ms

    live = tmp_path / "cora-2026-07-30.log"        # start-date basename, still live
    live.write_text(
        "2026-07-31 00:18:00 INFO cora heartbeat alive uptime_s=93780\n",
        encoding="utf-8")
    decoy = tmp_path / "cora-2026-07-31.log"       # empty same-day file, no heartbeat
    decoy.write_text("", encoding="utf-8")
    # Make the decoy NEWEST by mtime — the fallback must still find the live log.
    _os.utime(live, (1_000_000_000, 1_000_000_000))
    assert ms._read_uptime_from_log(log_dir=tmp_path) == 93780

    # And when the live file IS the newest (the normal shape), it wins directly.
    _os.utime(decoy, (999_999_999, 999_999_999))
    assert ms._read_uptime_from_log(log_dir=tmp_path) == 93780


def test_snapshot_writer_warns_when_dir_off_repo_volume(monkeypatch, caplog):
    """D-051 fix pin: pointing CORA_SNAPSHOT_DIR at a non-repo volume (e.g. G:)
    routes unbounded raw I/O at a mount that can hang — warn loudly at startup."""
    import logging as _logging

    monkeypatch.setenv("CORA_SNAPSHOT_DIR", "Q:/somewhere/snapshots")
    monkeypatch.setattr(ss, "_snapshot_loop", lambda *a, **k: None)  # don't run
    with caplog.at_level(_logging.WARNING, logger="cora.session_snapshots"):
        t = ss.start_snapshot_writer()
        t.join(timeout=5)
    assert any("not on the repo volume" in r.message for r in caplog.records)


# ─────────────────────────────────────────────────────────────────────────────
# Content posture: LEX redaction, known-answers exclusion, flywheel pure read
# ─────────────────────────────────────────────────────────────────────────────
def test_code_queue_snapshot_redacts_lex_items():
    """Read-layer redaction pin: even a legacy raw-LEX row in the event ledger
    (pre-parity-raise) must never surface raw title/summary/fix_sketch — nor a
    staged prompt_path whose FILENAME embeds the raw-title slug (D-051
    2026-07-31 finding) — in the snapshot. code-queue.json inherits
    load_items/_lex_safe_view + the render-side re-check by construction."""
    row = {
        "event": "captured", "id": "cq-lextest0001",
        "ts": "2026-07-30T00:00:00+00:00", "status": "PROPOSED",
        "entity": "LEX-LLC", "kind": "bug", "severity": "HIGH",
        "title": "Marcus Alvarez billing authorization lookup",
        "summary": "raw sensitive summary", "fix_sketch": "raw fix detail",
    }
    staged = {
        "event": "staged", "id": "cq-lextest0001",
        "ts": "2026-07-30T01:00:00+00:00",
        "prompt_path": "_notes/2026-07-30_fndr_cora-code-prompt-"
                       "marcus-alvarez-billing-authorization-lookup-abc123.md",
    }
    cq._EVENT_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    cq._EVENT_LEDGER.write_text(
        json.dumps(row) + "\n" + json.dumps(staged) + "\n", encoding="utf-8")

    payload = ss._render_code_queue()
    assert "Marcus" not in payload["backlog"]
    assert "marcus-alvarez" not in payload["backlog"]  # the slug channel
    assert "raw sensitive summary" not in payload["backlog"]
    assert cq._LEX_REDACTED_TITLE in payload["backlog"]
    assert payload["provenance"]  # injection framing rides along


def test_lex_safe_view_redacts_prompt_path_but_keeps_it_truthy():
    """The prompt_path replacement must stay TRUTHY: process_queue_action's
    staging idempotency keys on it — a blanked path would re-stage (and
    double-generate) an already-staged LEX item."""
    it = {"id": "cq-x", "entity": "LEX", "status": "STAGED",
          "title": "raw", "summary": "raw", "fix_sketch": "raw",
          "prompt_path": "_notes/2026-07-28_cora-code-prompt-client-name-slug.md"}
    safe = cq._lex_safe_view(it)
    assert safe["prompt_path"] == cq._LEX_REDACTED_PROMPT_PATH
    assert safe["prompt_path"]  # truthy — idempotency preserved
    assert "client-name-slug" not in json.dumps(safe)
    # Non-LEX untouched; LEX item without a path gains nothing.
    assert cq._lex_safe_view({"entity": "F3E", "prompt_path": "p.md"})["prompt_path"] == "p.md"
    assert "prompt_path" not in cq._lex_safe_view({"entity": "LEX", "title": "t"})


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
                     "known-answers-index.json", "revops-ledger.json"}
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
    monkeypatch.setenv("CORA_REVOPS_DB", str(tmp_path / "revops_ledger.db"))
    cq._EVENT_LEDGER.parent.mkdir(parents=True, exist_ok=True)

    results = ss.tick(force=True)
    assert set(results) == {"status.json", "code-queue.json", "flywheel.json",
                            "known-answers-index.json", "revops-ledger.json",
                            "index.json"}
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
