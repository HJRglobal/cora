"""Session snapshot layer (SLIM) — the durable-file half of the 2026-07-30
session-comms decision (options map: `_shared/projects/cora/2026-07-30_fndr_
cora-session-comms-options-map.md`; Harrison chose SLIM the same night).

The cora-tools MCP plugin is the LIVE query lane; this module writes the same
D-092 read-surface data to small JSON files so that sessions WITHOUT the plugin
(claude.ai web/mobile via the Drive connector, mount-less sandboxes, plain file
tools) can still read Cora's state:

    data/session-bus/snapshots/            <- repo lane (gitignored)
        index.json                          catalog + cadence promises + stamps
        status.json                         cora_health payload + writer_alive
        code-queue.json                     generated backlog view (LEX-redacted)
        flywheel.json                       flywheel_metrics.collect() output
        known-answers-index.json            entity -> file mtime/size (no contents)
    G:\\My Drive\\HJR-Founder-OS\\_brain\\_bus\\snapshots\\   <- Drive mirror

The `session-bus` parent directory is deliberate: the deferred request/response
mailbox (spec in the kickoff's git/Drive history) slots in later without path
churn. Nothing here reads inbound files — this layer only ever WRITES.

Design invariants:

1.  REUSE, NEVER RE-RENDER RAW. Every payload is built from the exact code path
    the D-092 MCP surface already serves: ``mcp_server`` health helpers,
    ``code_queue.render_backlog_text()`` (LEX-redacted at the read layer by
    construction — load_items/_lex_safe_view), ``flywheel_metrics.collect()``
    (aggregate counters only; ``update_baseline`` stays False so this is a pure
    read), and the ``known_answers_map`` exposure rule (LEX sub-entities
    excluded, matching the MCP tool). No file carries anything the MCP surface
    would not serve.
2.  OWN DAEMON THREAD, NOT THE HEARTBEAT THREAD. The writer rides the same
    fail-soft daemon *pattern* as the heartbeat/drive-monitor loops, but runs on
    its own process-lifetime thread: a slow render (the scheduled-task
    PowerShell query is bounded at 45s; a G: mirror write can block for the
    drive_io timeout) must never delay the liveness sentinel that the watchdog
    and dead-man's ping key off. Worst case, a slow tick makes snapshots late —
    never the bot unhealthy-looking.
3.  FAIL-SOFT PER FILE PER TICK. A render/write error logs, skips that file,
    and leaves the previous file (and its ``updated_at``) in place — readers
    judge freshness from the stamps, never assume. One poisoned render can
    never kill the loop (belt: per-file try/except; suspenders: the loop wraps
    the whole tick).
4.  ATOMIC WRITES ONLY. Local files go temp + ``os.replace`` (never edited in
    place — virtiofs stale-read doctrine); the mirror goes through
    ``drive_io.write_text_atomic`` (bounded, breaker-guarded, D-083). A mirror
    outage never affects the repo lane; the file is retried on a later tick
    because ``_last_mirrored`` only advances on success.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from cora import drive_io

log = logging.getLogger("cora.session_snapshots")

_REPO_ROOT = Path(__file__).resolve().parents[2]

_DEFAULT_SNAPSHOT_DIR = _REPO_ROOT / "data" / "session-bus" / "snapshots"
_DEFAULT_MIRROR_DIR = Path("G:/My Drive/HJR-Founder-OS/_brain/_bus/snapshots")

_INDEX_NAME = "index.json"

# Heartbeat-staleness threshold mirrored from mcp_server.health() so the two
# surfaces can never disagree on what "alive" means.
_ALIVE_THRESHOLD_SECS = 300

# The scheduled-task query shells out to PowerShell (bounded 45s in
# mcp_server._read_task_last_results) — far too heavy to run every 60s tick.
# status.json still refreshes heartbeat/uptime every tick; task results ride a
# TTL cache and refresh at most this often.
_TASK_RESULTS_TTL_SECS = 300


def _snapshot_dir() -> Path:
    return Path(os.environ.get("CORA_SNAPSHOT_DIR") or _DEFAULT_SNAPSHOT_DIR)


def _mirror_dir() -> Path:
    return Path(os.environ.get("CORA_SNAPSHOT_MIRROR_DIR") or _DEFAULT_MIRROR_DIR)


def _interval_secs() -> int:
    try:
        return max(10, int(os.environ.get("CORA_SNAPSHOT_INTERVAL_SECS") or "60"))
    except ValueError:
        return 60


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# Renders (each returns the payload WITHOUT updated_at; the writer stamps it)
# ─────────────────────────────────────────────────────────────────────────────
_task_results_cache: tuple[float, dict[str, Any]] | None = None


def _cached_task_results() -> dict[str, Any]:
    """Scheduled-task states via the MCP health helper, TTL-cached (see the
    _TASK_RESULTS_TTL_SECS note). Shape: {task_name: {state, last_result}}."""
    global _task_results_cache
    mono = time.monotonic()
    if _task_results_cache is not None and mono - _task_results_cache[0] < _TASK_RESULTS_TTL_SECS:
        return _task_results_cache[1]
    from cora import mcp_server

    raw = mcp_server._read_task_last_results()
    shaped = {
        name: {"state": state, "last_result": code}
        for name, (state, code) in sorted(raw.items())
    }
    _task_results_cache = (mono, shaped)
    return shaped


def _render_status() -> dict[str, Any]:
    """The cora_health payload (same helpers the MCP tool composes from) plus a
    writer_alive stamp. status.json's own updated_at going stale IS the signal
    that this writer (and therefore the bot process) stopped."""
    from cora import health_endpoint, mcp_server

    out: dict[str, Any] = {
        "writer_alive": True,
        "writer_interval_seconds": _interval_secs(),
    }
    age: float | None = None
    try:
        age = health_endpoint.heartbeat_age_seconds()
    except Exception as exc:  # noqa: BLE001
        out["heartbeat_error"] = str(exc)
    out["heartbeat_age_seconds"] = None if age is None else round(age, 1)
    out["alive"] = age is not None and age <= _ALIVE_THRESHOLD_SECS
    out["uptime_seconds"] = mcp_server._read_uptime_from_log()
    out["task_results"] = _cached_task_results()
    return out


def _render_code_queue() -> dict[str, Any]:
    """The generated code-session backlog view. LEX title/summary/fix_sketch
    redaction is applied at code_queue's read layer (load_items/_lex_safe_view)
    AND re-checked inside render_backlog_text — this snapshot inherits both by
    construction (pinned by test)."""
    from cora import code_queue, mcp_server

    return {
        "provenance": mcp_server._CQ_PROVENANCE,
        "backlog": code_queue.render_backlog_text(),
    }


def _render_flywheel() -> dict[str, Any]:
    """flywheel_metrics.collect() — aggregate counters/rates only. update_baseline
    stays at its False default: the pending-growth baseline history belongs to the
    scheduled monitor; this snapshot must be a pure read."""
    from cora import flywheel_metrics

    return {"metrics": flywheel_metrics.collect()}


def _render_known_answers_index() -> dict[str, Any]:
    """entity -> known-answers file mtime/size — an INDEX only, never contents.
    LEX sub-entities are excluded, matching the MCP known_answers exposure rule
    (their answers surface only at the LEX GM level)."""
    from cora import context_loader as cl
    from cora.known_answers_map import ENTITY_FILES

    entities = sorted(k for k in ENTITY_FILES if not k.startswith("LEX-"))
    file_stats: dict[str, dict[str, Any]] = {}
    for fname in sorted({ENTITY_FILES[e] for e in entities}):
        path = cl._KNOWN_ANSWERS_DIR / fname
        try:
            info = drive_io.stat_info(path, timeout=5.0, retry_seconds=0)
        except Exception:  # noqa: BLE001 — DriveUnavailable/timeout: unknown, not absent
            file_stats[fname] = {"exists": None, "error": "stat unavailable"}
            continue
        if info is None:
            file_stats[fname] = {"exists": False}
        else:
            mtime, size = info
            try:
                modified = datetime.fromtimestamp(mtime, timezone.utc).isoformat()
            except (OSError, ValueError, OverflowError):
                modified = None
            file_stats[fname] = {"exists": True, "modified": modified, "size_bytes": size}
    return {
        "note": ("Index only -- known-answers file contents are never snapshotted. "
                 "LEX sub-entities excluded (answers surface at the LEX GM level only)."),
        "entities": {e: {"file": ENTITY_FILES[e], **file_stats[ENTITY_FILES[e]]}
                     for e in entities},
    }


# Catalog of everything this writer maintains. Cadences are minimum refresh
# intervals, evaluated on the ~60s tick; 0 = every tick. index.json is not
# listed — it is rewritten at the end of every tick to reflect fresh stamps.
_SPECS: list[dict[str, Any]] = [
    {
        "name": "status.json",
        "description": ("Cora liveness snapshot (heartbeat age, uptime, scheduled-task "
                        "results) + writer_alive stamp"),
        "cadence": 0,
        "render": _render_status,
    },
    {
        "name": "code-queue.json",
        "description": ("Generated code-session backlog view (Harrison-gated queue; "
                        "LEX items redacted at the read layer)"),
        "cadence": 300,
        "render": _render_code_queue,
    },
    {
        "name": "flywheel.json",
        "description": "flywheel_metrics.collect() output (aggregate counters/rates)",
        "cadence": 3600,
        "render": _render_flywheel,
    },
    {
        "name": "known-answers-index.json",
        "description": ("entity -> known-answers file mtime/size index (no contents; "
                        "LEX sub-entities excluded)"),
        "cadence": 300,
        "render": _render_known_answers_index,
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# Writer state (single writer thread; tests call tick() directly)
# ─────────────────────────────────────────────────────────────────────────────
_last_success: dict[str, float] = {}   # name -> monotonic of last successful write
_last_stamp: dict[str, str] = {}       # name -> updated_at written to the file
_last_mirrored: dict[str, str] = {}    # name -> updated_at last pushed to the mirror
_mirror_healthy: bool | None = None    # None until the first mirror attempt


def reset_state_for_tests() -> None:
    global _task_results_cache, _mirror_healthy
    _last_success.clear()
    _last_stamp.clear()
    _last_mirrored.clear()
    _task_results_cache = None
    _mirror_healthy = None


def _bootstrap_stamp(name: str) -> str | None:
    """After a restart the in-memory stamps are empty but files persist on disk;
    read a file's own updated_at (best-effort) so index.json stays honest about
    what is actually there instead of reporting null."""
    try:
        data = json.loads((_snapshot_dir() / name).read_text(encoding="utf-8"))
        stamp = data.get("updated_at")
        return stamp if isinstance(stamp, str) and stamp else None
    except Exception:  # noqa: BLE001 — absent/corrupt file: no stamp to report
        return None


def _stamp_for_index(name: str) -> str | None:
    stamp = _last_stamp.get(name)
    if stamp is None:
        stamp = _bootstrap_stamp(name)
        if stamp is not None:
            _last_stamp[name] = stamp
    return stamp


def _render_index() -> dict[str, Any]:
    interval = _interval_secs()
    files = {}
    for spec in _SPECS:
        files[spec["name"]] = {
            "description": spec["description"],
            "cadence_seconds": spec["cadence"] or interval,
            "updated_at": _stamp_for_index(spec["name"]),
        }
    return {
        "writer": "src/cora/session_snapshots.py (Cora bot process, SessionSnapshots daemon)",
        "interval_seconds": interval,
        "note": ("Freshness contract: judge each file by its updated_at against its "
                 "cadence_seconds. A stale stamp means the writer (or that render) "
                 "is down -- stamps are never faked."),
        "files": files,
    }


def _write_local(name: str, payload: dict[str, Any]) -> None:
    """Temp + os.replace in the same directory — never an in-place edit."""
    directory = _snapshot_dir()
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / (name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, directory / name)


def _log_mirror_transition(ok: bool, detail: str = "") -> None:
    global _mirror_healthy
    if _mirror_healthy is ok:
        return
    if ok:
        log.info("session-snapshots: G: mirror active (%s)", _mirror_dir())
    else:
        log.warning("session-snapshots: G: mirror unavailable (%s) -- repo lane "
                    "unaffected, mirror retries next tick", detail or "unknown")
    _mirror_healthy = ok


def _mirror_pass() -> None:
    """Push every file whose updated_at advanced since its last successful mirror
    write. Mirrors the exact local bytes (one serialization, one truth). Entirely
    fail-soft: a Drive blip skips this pass and retries later."""
    names = [spec["name"] for spec in _SPECS] + [_INDEX_NAME]
    to_push = [n for n in names
               if _last_stamp.get(n) and _last_stamp.get(n) != _last_mirrored.get(n)]
    if not to_push:
        return
    src_dir = _snapshot_dir()
    target_dir = _mirror_dir()
    for name in to_push:
        stamp = _last_stamp[name]
        try:
            text = (src_dir / name).read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("session-snapshots: mirror read-back of %s failed: %s", name, exc)
            continue
        try:
            drive_io.write_text_atomic(target_dir / name, text, retry_seconds=0)
        except drive_io.DriveUnavailable as exc:
            _log_mirror_transition(False, str(exc))
            return  # mount is gone; the rest of this pass would fail identically
        except Exception as exc:  # noqa: BLE001
            _log_mirror_transition(False, f"{name}: {exc}")
            continue
        _last_mirrored[name] = stamp
        _log_mirror_transition(True)


def tick(*, force: bool = False) -> dict[str, str]:
    """One snapshot pass: render every due file (fail-soft per file), rewrite
    index.json, then mirror what changed. Returns {filename: written|fresh|failed}
    for observability/tests."""
    results: dict[str, str] = {}
    mono = time.monotonic()
    for spec in _SPECS:
        name = spec["name"]
        last = _last_success.get(name)
        if not force and last is not None and (mono - last) < spec["cadence"]:
            results[name] = "fresh"
            continue
        try:
            payload = {"updated_at": _utc_now_iso(), **spec["render"]()}
            _write_local(name, payload)
        except Exception:  # noqa: BLE001 — one bad render never kills the tick
            log.warning("session-snapshots: %s render/write failed -- previous "
                        "file (and stamp) left in place", name, exc_info=True)
            results[name] = "failed"
            continue
        _last_success[name] = mono
        _last_stamp[name] = payload["updated_at"]
        results[name] = "written"

    try:
        payload = {"updated_at": _utc_now_iso(), **_render_index()}
        _write_local(_INDEX_NAME, payload)
        _last_stamp[_INDEX_NAME] = payload["updated_at"]
        results[_INDEX_NAME] = "written"
    except Exception:  # noqa: BLE001
        log.warning("session-snapshots: index.json write failed", exc_info=True)
        results[_INDEX_NAME] = "failed"

    _mirror_pass()
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Daemon loop (process-lifetime; started once from main())
# ─────────────────────────────────────────────────────────────────────────────
def _snapshot_loop(stop: threading.Event, loop_log: logging.Logger | None = None,
                   *, interval: float | None = None) -> None:
    _log = loop_log or log
    step = interval if interval is not None else _interval_secs()
    while True:
        try:
            tick()
        except Exception:  # noqa: BLE001 — suspenders; tick() is already fail-soft
            _log.warning("session-snapshots: tick failed (ignored)", exc_info=True)
        if stop.wait(step):
            return


def start_snapshot_writer(loop_log: logging.Logger | None = None) -> threading.Thread:
    """Start the process-lifetime snapshot daemon. The stop Event is never set in
    production (the thread dies with the process); it exists for tests."""
    thread = threading.Thread(
        target=_snapshot_loop,
        args=(threading.Event(), loop_log),
        name="SessionSnapshots",
        daemon=True,
    )
    thread.start()
    return thread
