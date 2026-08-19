"""Instance ledger -- provable identity for the always-on bot process.

WHY THIS EXISTS (2026-08-19): the 8/18 watchdog forensics concluded that four
"restart_exit: 0" recoveries had produced ZERO fresh "Cora starting up" lines and
therefore only stacked instances. Both halves were wrong, and both came from the
same blind spot: **nothing in the log or the sentinel file says WHICH PROCESS
wrote it.**

- The log format carries no pid, and every process started on the same calendar
  day appends to the same `cora-<date>.log`, so two instances interleave
  invisibly.
- `TimedRotatingFileHandler` pins the live file to the process's START date and
  moves each completed day to `cora-<startdate>.log.<thatday>`. So an instance
  started 8/17 writes 8/18's lines into `cora-2026-08-17.log`, and its own
  startup line ends up in `cora-2026-08-17.log.2026-08-17` -- which a
  `cora-*.log` glob does NOT match. That is exactly how a real restart reads as
  "no startup line anywhere".

This module supplies the missing evidence rather than trusting a log grep:

- `logs/cora-instances.jsonl` -- append-only start/stop ledger (pid, ISO ts, the
  log file actually in use, cwd). A restart is verified by a NEW start row with a
  DIFFERENT pid, not by an exit code.
- `data/health/instance.json` -- current-instance sentinel refreshed by the
  heartbeat loop (pid, started_at, last_heartbeat, uptime_s, consecutive
  heartbeat-file write failures).

`data/health/heartbeat.txt` is deliberately left byte-identical in format (a bare
ISO-8601 UTC timestamp). Ten independent parsers read it -- cora-watchdog.ps1,
health_endpoint, strategy_memo, nightly_health_check, four KB maintenance
scripts' heartbeat guards, restart-cora.ps1 and the runbook -- and several of
them (`datetime.fromisoformat`, `[datetimeoffset]::Parse`) would break on a
second line. New facts go in a NEW file.

Every function here is fail-soft by contract: observability must never take the
bot down. Nothing raises.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Module-level so tests can redirect both without touching the live files.
INSTANCE_FILE = _REPO_ROOT / "data" / "health" / "instance.json"
LEDGER_FILE = _REPO_ROOT / "logs" / "cora-instances.jsonl"

# Ledger rows are tiny; keep the whole history (one process start is ~200 bytes,
# and the monthly log-compaction job owns trimming under logs/).
_MAX_LEDGER_BYTES = 5_000_000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> bool:
    """Write JSON atomically. Returns True on success, False on any failure."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".instance-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
                fh.write("\n")
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            raise
        return True
    except Exception:
        return False


def _append_ledger(row: dict[str, Any]) -> bool:
    try:
        LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
        if LEDGER_FILE.exists() and LEDGER_FILE.stat().st_size > _MAX_LEDGER_BYTES:
            # Never let observability grow unbounded; the newest rows are the
            # ones any verification reads.
            return False
        with LEDGER_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        return True
    except Exception:
        return False


def record_start(log_file: str | None = None) -> dict[str, Any]:
    """Record this process as the live instance. Never raises."""
    row: dict[str, Any] = {
        "event": "start",
        "ts": _now_iso(),
        "pid": os.getpid(),
        "log_file": log_file or "",
        "cwd": "",
    }
    try:
        row["cwd"] = os.getcwd()
    except Exception:
        pass
    _append_ledger(row)
    _atomic_write_json(
        INSTANCE_FILE,
        {
            "pid": row["pid"],
            "started_at": row["ts"],
            "last_heartbeat": row["ts"],
            "uptime_s": 0,
            "log_file": row["log_file"],
            "heartbeat_write_failures": 0,
        },
    )
    return row


def record_stop(reason: str = "") -> dict[str, Any]:
    """Record a clean shutdown. Never raises. A missing stop row is normal (a
    killed process cannot write one) -- absence is NOT evidence of anything."""
    row = {
        "event": "stop",
        "ts": _now_iso(),
        "pid": os.getpid(),
        "reason": reason or "",
    }
    _append_ledger(row)
    return row


def touch(uptime_s: int, write_failures: int = 0, log_file: str | None = None) -> bool:
    """Refresh the current-instance sentinel from the heartbeat loop.

    `write_failures` is the count of CONSECUTIVE heartbeat.txt write failures --
    the number that turns "the file is stale" into "we know why". Never raises.
    """
    payload: dict[str, Any] = {
        "pid": os.getpid(),
        "last_heartbeat": _now_iso(),
        "uptime_s": int(uptime_s),
        "heartbeat_write_failures": int(write_failures),
    }
    prior = read_current()
    if prior:
        payload["started_at"] = prior.get("started_at", "")
        payload["log_file"] = prior.get("log_file", "")
    if log_file:
        payload["log_file"] = log_file
    return _atomic_write_json(INSTANCE_FILE, payload)


def read_current() -> dict[str, Any] | None:
    """Current-instance sentinel, or None if absent/unreadable/not a dict."""
    try:
        raw = INSTANCE_FILE.read_text(encoding="utf-8").strip()
        if not raw:
            return None
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def read_starts(limit: int = 20) -> list[dict[str, Any]]:
    """The most recent `limit` start rows, oldest-first. Empty on any failure."""
    if limit <= 0:
        return []
    try:
        lines = LEDGER_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    starts: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict) and row.get("event") == "start":
            starts.append(row)
    return starts[-limit:]
