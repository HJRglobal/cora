"""Cora health report -- repeatable snapshot of the metrics that gate scaling.

Implements Phase 0 of the 2026-06-08 scaling/memory game plan: the numbers that
turn every threshold in that doc into an alarm instead of an incident.

Reports six sections:
  1. KB corpus by entity + by source (+ FNDR co-scan share, sub_entity coverage)
  2. Static-context token size per entity (the uncached mass the caching split moves)
  3. Tool-definition block token size + tool count
  4. Recent real billing parsed from logs/cora-*.log "claude usage" lines
     (median input / cache_read / cache_create / output + cache_read/input ratio)
  5. State-store sizes (cora_kb.db, logs/ dir, every JSONL ledger)
  6. Scheduled-task next-run times + overlaps in the 03:00-09:00 AZ window

ASCII-only output (safe on a cp1252 host console). Offline + free by default
(token sizes via a char/4 heuristic); pass --count-tokens to use the Anthropic
count_tokens endpoint for a precise one-time baseline. --json dumps the full
snapshot for the weekly health-metric ritual (section 8 of the game plan).

    .venv\\Scripts\\python.exe scripts\\cora_health_report.py
    .venv\\Scripts\\python.exe scripts\\cora_health_report.py --json
    .venv\\Scripts\\python.exe scripts\\cora_health_report.py --count-tokens --log-days 7
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

try:
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env", override=True)
except Exception:  # noqa: BLE001 -- .env load is best-effort; offline mode still works
    pass

KB_DB_PATH = REPO_ROOT / "data" / "cora_kb.db"
LOGS_DIR = REPO_ROOT / "logs"

# Dashboard drift check (Asana Standard v1 Slice 6e): every pinned Cowork artifact
# must be registered in dashboard-access.yaml so a new dashboard surfaces as a
# Monday alarm instead of relying on human memory. Host/OneDrive-specific dir, so
# it is env-overridable and a missing dir is a no-op (never a hard error).
_ARTIFACTS_DIR = Path(
    os.environ.get(
        "COWORK_ARTIFACTS_DIR",
        str(Path.home() / "OneDrive" / "Documents" / "Claude" / "Artifacts"),
    )
)
_DASHBOARD_ACCESS_YAML = REPO_ROOT / "data" / "maps" / "dashboard-access.yaml"

# Entities whose static context we size. Keys of _ENTITY_PATHS plus FNDR
# (FNDR has no entity CLAUDE.md but _load_static_context still assembles the
# founder brief + known-answers + dynamic snapshots for it).
_USAGE_RE = re.compile(
    r"claude usage iter=(\d+) input=(\d+) cache_create=(\d+) "
    r"cache_read=(\d+) output=(\d+)"
    # Trailing fields added by cora.llm_usage (script-side instrumentation,
    # 2026-07-31 batch pilot slice 1). Optional so legacy bot lines from
    # claude_client._log_usage keep parsing; presence of caller= is the
    # bot-vs-script bucket discriminator in recent_billing().
    r"(?: model=(?P<model>\S+))?(?: caller=(?P<caller>\S+))?"
)

# USD per MTok (input, output) by model-id PREFIX, first match wins -- so dated
# snapshots (claude-haiku-4-5-20251001) and CORA_SONNET_MODEL variants
# (claude-sonnet-4-6 / claude-sonnet-5) resolve. Cache reads bill ~0.1x the
# input rate; cache writes ~1.25x (5-min TTL). ESTIMATE only -- ignores intro
# pricing and batch discounts (batch legs log via= and already spent 50% less).
_MODEL_RATES: tuple[tuple[str, tuple[float, float]], ...] = (
    ("claude-haiku-4-5", (1.0, 5.0)),
    ("claude-sonnet", (3.0, 15.0)),
    ("claude-opus", (5.0, 25.0)),
    ("claude-fable", (10.0, 50.0)),
)


def _rate_for(model: str) -> tuple[float, float] | None:
    for prefix, rates in _MODEL_RATES:
        if model.startswith(prefix):
            return rates
    return None

# KB retrieval latency (Slice 2-3): "KB retrieved N chunks (of M returned) for
# entity=<E> — best distance=<d> kb_ms=<ms>". Entity token stops at whitespace so
# codes like LEX-LLC parse whole.
_KB_MS_RE = re.compile(r"KB retrieved .*? entity=(\S+).*?kb_ms=(\d+(?:\.\d+)?)")


# --------------------------------------------------------------------------- #
# token counting
# --------------------------------------------------------------------------- #

def _make_token_counter(use_api: bool):
    """Return (counter_fn, method_label).

    counter_fn(text) -> int. Default is a char/4 heuristic (offline, free,
    deterministic). With --count-tokens we call the Anthropic count_tokens
    endpoint once per blob -- accurate, but needs an API key and network.
    """
    if not use_api:
        return (lambda text: len(text) // 4), "char/4 heuristic"

    try:
        import anthropic  # noqa: PLC0415
        from cora.claude_client import _MODEL  # noqa: PLC0415
        from cora.config import config  # noqa: PLC0415

        client = anthropic.Anthropic(api_key=config.anthropic_api_key)

        def _count(text: str) -> int:
            if not text:
                return 0
            resp = client.messages.count_tokens(
                model=_MODEL,
                messages=[{"role": "user", "content": text}],
            )
            return int(resp.input_tokens)

        return _count, f"anthropic count_tokens ({_MODEL})"
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: count_tokens unavailable ({exc}); falling back to char/4",
              file=sys.stderr)
        return (lambda text: len(text) // 4), "char/4 heuristic (api fallback)"


# --------------------------------------------------------------------------- #
# 1. KB corpus
# --------------------------------------------------------------------------- #

def kb_corpus() -> dict:
    if not KB_DB_PATH.exists():
        return {"available": False, "reason": f"no db at {KB_DB_PATH}"}
    conn = sqlite3.connect(str(KB_DB_PATH))
    try:
        total = conn.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0]
        by_entity = {
            (r[0] or "(null)"): r[1]
            for r in conn.execute(
                "SELECT entity, COUNT(*) c FROM knowledge_chunks "
                "GROUP BY entity ORDER BY c DESC"
            ).fetchall()
        }
        by_source = {
            (r[0] or "(null)"): r[1]
            for r in conn.execute(
                "SELECT source, COUNT(*) c FROM knowledge_chunks "
                "GROUP BY source ORDER BY c DESC"
            ).fetchall()
        }
        sub_entity_tagged = conn.execute(
            "SELECT COUNT(*) FROM knowledge_chunks WHERE sub_entity IS NOT NULL"
        ).fetchone()[0]
    finally:
        conn.close()

    fndr = by_entity.get("FNDR", 0)
    return {
        "available": True,
        "total_chunks": total,
        "by_entity": by_entity,
        "by_source": by_source,
        "fndr_chunks": fndr,
        "fndr_share_pct": round(100.0 * fndr / total, 1) if total else 0.0,
        "sub_entity_tagged": sub_entity_tagged,
    }


# --------------------------------------------------------------------------- #
# 2. static context token sizes
# --------------------------------------------------------------------------- #

def static_context_tokens(counter) -> dict:
    import cora.context_loader as cl  # noqa: PLC0415

    cl._cache.clear()  # ensure a clean read (don't trust a warm process cache)
    entities = list(cl._ENTITY_PATHS.keys()) + ["FNDR"]
    out: dict[str, dict] = {}
    for entity in entities:
        try:
            text = cl._load_static_context(entity)
            out[entity] = {"chars": len(text), "tokens": counter(text)}
        except Exception as exc:  # noqa: BLE001
            out[entity] = {"error": str(exc)}
    return out


# --------------------------------------------------------------------------- #
# 3. tool block size
# --------------------------------------------------------------------------- #

def tool_block_tokens(counter) -> dict:
    from cora.tools.tool_dispatch import TOOL_DEFINITIONS  # noqa: PLC0415

    serialized = json.dumps(list(TOOL_DEFINITIONS))
    return {
        "tool_count": len(TOOL_DEFINITIONS),
        "serialized_chars": len(serialized),
        "approx_tokens": counter(serialized),
    }


# --------------------------------------------------------------------------- #
# 4. recent billing from logs
# --------------------------------------------------------------------------- #

def _median(values: list[int]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return float(s[mid]) if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile (adequate for latency observability)."""
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1))))
    return float(s[idx])


def kb_latency(log_days: int) -> dict:
    """Warm KB-search latency per entity, parsed from the cora-*.log kb_ms lines.

    Feeds the section-5 warm-p95 < 3s threshold and makes the Slice 2-1 partition-key
    win measurable. No API/DB access -- pure log parse.
    """
    logs = sorted(LOGS_DIR.glob("cora-2*.log"))[-log_days:] if LOGS_DIR.exists() else []
    by_entity: dict[str, list[float]] = {}
    for log_path in logs:
        try:
            with log_path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if "kb_ms=" not in line:
                        continue
                    m = _KB_MS_RE.search(line)
                    if m:
                        by_entity.setdefault(m.group(1), []).append(float(m.group(2)))
        except OSError:
            continue
    all_ms: list[float] = []
    per_entity: dict[str, dict] = {}
    for ent, vals in by_entity.items():
        all_ms.extend(vals)
        per_entity[ent] = {
            "n": len(vals),
            "p50": round(_percentile(vals, 50)),
            "p95": round(_percentile(vals, 95)),
        }
    return {
        "samples": len(all_ms),
        "overall_p50": round(_percentile(all_ms, 50)) if all_ms else 0,
        "overall_p95": round(_percentile(all_ms, 95)) if all_ms else 0,
        "by_entity": per_entity,
    }


def recent_billing(log_days: int) -> dict:
    """Parse "claude usage" lines into a BOT bucket and a SCRIPT bucket.

    Bot bucket (existing semantics): lines WITHOUT caller= in logs/cora-2*.log
    -- claude_client._log_usage output, median-summarized. Script bucket
    (2026-07-31 slice 1): lines WITH caller= from EVERY dated log file in the
    same date window (scripts log to per-family files like
    session-capture-YYYY-MM-DD.log; the synthesis runners share cora-*.log),
    summed per caller with a $-estimate from _MODEL_RATES.
    """
    cora_logs = sorted(LOGS_DIR.glob("cora-2*.log"))[-log_days:] if LOGS_DIR.exists() else []
    # Window = the selected cora logs' stem dates UNION the last N calendar
    # days (the bot bucket keeps its historical last-N-files semantics; the
    # calendar anchor keeps the SCRIPT bucket honest even when cora-*.log
    # naming is sparse).
    today = _dt.date.today()
    window_dates = {p.stem[-10:] for p in cora_logs} | {
        (today - _dt.timedelta(days=i)).isoformat() for i in range(log_days)}
    # file -> is_cora_log; ONLY the selected cora_logs feed the bot bucket.
    all_logs: dict[Path, bool] = dict.fromkeys(cora_logs, True)
    if LOGS_DIR.exists():
        for p in LOGS_DIR.glob("*.log"):
            if p in all_logs:
                continue
            # Admit by filename date OR by mtime date: the always-on bot's
            # TimedRotatingFileHandler keeps writing its process-START-date
            # basename across midnight rollovers (D-051 2026-08-01 finding 3),
            # so bot-resident caller= lines live in a file whose NAME date can
            # be weeks old while its mtime is today.
            try:
                mtime_date = _dt.date.fromtimestamp(p.stat().st_mtime).isoformat()
            except OSError:
                mtime_date = ""
            if p.stem[-10:] in window_dates or mtime_date in window_dates:
                all_logs[p] = False

    inputs: list[int] = []
    cache_reads: list[int] = []
    cache_creates: list[int] = []
    outputs: list[int] = []
    scripts: dict[str, dict] = {}
    script_est_incomplete = False
    for log_path, is_cora in all_logs.items():
        try:
            with log_path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if "claude usage" not in line:
                        continue
                    m = _USAGE_RE.search(line)
                    if not m:
                        continue
                    caller = m.group("caller")
                    if caller:
                        # Bound whole-file scans to the window: a long-lived
                        # (mtime-admitted) file can hold weeks of caller=
                        # lines; skip any line whose leading timestamp date is
                        # outside the window. Undated line formats pass (their
                        # files are dated per day and window-gated already).
                        prefix = line[:10]
                        if prefix[:4].isdigit() and prefix not in window_dates:
                            continue
                        model = m.group("model") or "-"
                        row = scripts.setdefault(caller, {
                            "calls": 0, "input": 0, "cache_create": 0,
                            "cache_read": 0, "output": 0, "est_usd": 0.0,
                        })
                        row["calls"] += 1
                        row["input"] += int(m.group(2))
                        row["cache_create"] += int(m.group(3))
                        row["cache_read"] += int(m.group(4))
                        row["output"] += int(m.group(5))
                        rates = _rate_for(model)
                        if rates is None:
                            script_est_incomplete = True
                        else:
                            in_rate, out_rate = rates
                            row["est_usd"] += (
                                int(m.group(2)) * in_rate
                                + int(m.group(3)) * in_rate * 1.25
                                + int(m.group(4)) * in_rate * 0.1
                                + int(m.group(5)) * out_rate
                            ) / 1_000_000
                    elif is_cora:
                        inputs.append(int(m.group(2)))
                        cache_creates.append(int(m.group(3)))
                        cache_reads.append(int(m.group(4)))
                        outputs.append(int(m.group(5)))
        except OSError:
            continue

    for row in scripts.values():
        row["est_usd"] = round(row["est_usd"], 4)
    med_input = _median(inputs)
    med_cache_read = _median(cache_reads)
    ratio = round(med_cache_read / med_input, 3) if med_input else 0.0
    return {
        "logs_parsed": [p.name for p in cora_logs],
        "usage_lines": len(inputs),
        "median_input": med_input,
        "median_cache_read": med_cache_read,
        "median_cache_create": _median(cache_creates),
        "median_output": _median(outputs),
        "cache_read_over_input": ratio,
        "script_usage": dict(sorted(scripts.items(),
                                    key=lambda kv: -kv[1]["est_usd"])),
        "script_lines": sum(r["calls"] for r in scripts.values()),
        "script_est_usd": round(sum(r["est_usd"] for r in scripts.values()), 4),
        "script_est_incomplete": script_est_incomplete,
        "script_files_parsed": len(all_logs) - len(cora_logs),
    }


# --------------------------------------------------------------------------- #
# 5. state-store sizes
# --------------------------------------------------------------------------- #

def _dir_bytes(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def state_sizes() -> dict:
    dbs = {}
    data_dir = REPO_ROOT / "data"
    if data_dir.exists():
        for p in sorted(data_dir.glob("*.db")):
            dbs[p.name] = p.stat().st_size

    jsonl = {}
    for base in (LOGS_DIR, data_dir):
        if not base.exists():
            continue
        for p in sorted(base.rglob("*.jsonl")):
            try:
                jsonl[str(p.relative_to(REPO_ROOT))] = p.stat().st_size
            except OSError:
                pass

    return {
        "cora_kb_db_bytes": (KB_DB_PATH.stat().st_size if KB_DB_PATH.exists() else 0),
        "logs_dir_bytes": _dir_bytes(LOGS_DIR),
        "state_dbs": dbs,
        "jsonl_ledgers": jsonl,
    }


# --------------------------------------------------------------------------- #
# 6. scheduled tasks
# --------------------------------------------------------------------------- #

_TIME_RE = re.compile(r"(\d{1,2}):(\d{2}):\d{2}\s*(AM|PM)", re.IGNORECASE)


def _hour_of(next_run: str) -> int | None:
    """Best-effort: extract the 24h hour from a schtasks 'Next Run Time' string."""
    m = _TIME_RE.search(next_run)
    if not m:
        return None
    hour = int(m.group(1)) % 12
    if m.group(3).upper() == "PM":
        hour += 12
    return hour


def _clock_str(next_run: str) -> str | None:
    """Extract the clock time-of-day (e.g. '4:00 AM') ignoring the date.

    Used to detect same-clock-time collisions across tasks whose next-run dates
    differ (a daily 4am task vs a weekly 4am task collide on the clock).
    """
    m = _TIME_RE.search(next_run)
    if not m:
        return None
    return f"{int(m.group(1))}:{m.group(2)} {m.group(3).upper()}"


def scheduled_tasks() -> dict:
    try:
        proc = subprocess.run(
            ["schtasks", "/query", "/fo", "LIST", "/v"],
            capture_output=True, text=True, timeout=60,
        )
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": str(exc)}

    tasks: list[dict] = []
    cur: dict[str, str] = {}
    for raw in proc.stdout.splitlines():
        line = raw.strip()
        if not line:
            if cur.get("name"):
                tasks.append(cur)
            cur = {}
            continue
        if line.startswith("TaskName:"):
            if cur.get("name"):
                tasks.append(cur)
            cur = {"name": line.split(":", 1)[1].strip()}
        elif line.startswith("Next Run Time:"):
            cur["next_run"] = line.split(":", 1)[1].strip()
        elif line.startswith("Scheduled Task State:"):
            cur["state"] = line.split(":", 1)[1].strip()
    if cur.get("name"):
        tasks.append(cur)

    cora = [
        t for t in tasks
        if "cora" in t.get("name", "").lower()
    ]
    early_window = []  # next run in 03:00-09:00 AZ (the heavy KB/hygiene window)
    for t in cora:
        hour = _hour_of(t.get("next_run", ""))
        if hour is not None and 3 <= hour < 9:
            early_window.append({"name": t["name"], "next_run": t.get("next_run", "")})

    # Same-clock-time collisions in the early window (true simultaneity).
    peaks = Counter(
        _clock_str(t["next_run"]) for t in early_window if _clock_str(t["next_run"])
    )
    max_concurrent = max(peaks.values()) if peaks else 0
    peak_times = sorted(ct for ct, c in peaks.items() if c >= 2)

    return {
        "available": True,
        "cora_task_count": len(cora),
        "tasks": [
            {"name": t["name"], "next_run": t.get("next_run", ""),
             "state": t.get("state", "")}
            for t in cora
        ],
        "early_window_0300_0900": early_window,
        "early_window_overlap": len(early_window) > 1,
        "max_concurrent_in_window": max_concurrent,
        "concurrent_peak_times": peak_times,
    }


def flywheel_metrics_section() -> dict:
    """Knowledge-flywheel throughput (WS-2) -- computed by cora.flywheel_metrics
    so this report and the nightly health check share ONE set of numbers and
    thresholds. Read-only here (update_baseline=False; the nightly check owns
    the daily pending-size baseline write)."""
    try:
        from cora import flywheel_metrics as fm
        metrics = fm.collect(update_baseline=False)
        metrics["alarm_lines"] = [msg for _sev, msg in fm.evaluate(metrics)]
        metrics["display_lines"] = fm.format_lines(metrics)
        return metrics
    except Exception as exc:  # noqa: BLE001 -- fail-soft convention (see kb_corpus)
        return {"available": False, "reason": str(exc)}


def dashboard_drift_section() -> dict:
    """Diff the on-disk Cowork artifact ids against dashboard-access.yaml so an
    unregistered dashboard shows up as a Monday alarm (Slice 6e).

    Unions ALL registry buckets (dashboards / covered_by_existing / utility /
    retired) -- omitting any would false-positive on legitimately-registered
    artifacts. Fail-soft: a missing artifacts dir (a machine without the OneDrive
    mount) returns available=False, no alarm."""
    try:
        import yaml  # PyYAML is a repo dependency
        if not _ARTIFACTS_DIR.exists():
            return {"available": False, "reason": f"no artifacts dir at {_ARTIFACTS_DIR}"}
        on_disk = {p.parent.name for p in _ARTIFACTS_DIR.glob("*/index.html")}
        with _DASHBOARD_ACCESS_YAML.open("r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        registered = (
            set(cfg.get("dashboards", {}) or {})
            | set(cfg.get("covered_by_existing", {}) or {})
            | set(cfg.get("utility", []) or [])
            | set(cfg.get("retired", []) or [])
        )
        return {
            "available": True,
            "on_disk_count": len(on_disk),
            "registered_count": len(registered),
            "unregistered": sorted(on_disk - registered),
        }
    except Exception as exc:  # noqa: BLE001 -- fail-soft convention (see kb_corpus)
        return {"available": False, "reason": str(exc)}


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #

def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024.0
    return f"{n:.1f}GB"


def threshold_alarms(report: dict) -> list[str]:
    """Return human-readable alarms for any section-5 threshold crossed.

    Empty list = all clear. These are the lines the weekly Slack post leads with
    so the metrics watch themselves instead of waiting for an incident.
    """
    alarms: list[str] = []
    kb = report.get("kb_corpus", {})
    if kb.get("available"):
        if kb.get("fndr_share_pct", 0) > 60:
            alarms.append(
                f"FNDR co-scan share {kb['fndr_share_pct']}% > 60% -- add an "
                f"FNDR retention/archive tier (every query co-scans FNDR)."
            )
        if kb.get("total_chunks", 0) > 750_000:
            alarms.append(
                f"KB {kb['total_chunks']:,} chunks > 750K -- evaluate graduating "
                f"off sqlite-vec (LanceDB/Qdrant)."
            )
    st = report.get("state", {})
    ledger_bytes = sum(st.get("jsonl_ledgers", {}).values()) + st.get("logs_dir_bytes", 0)
    if ledger_bytes > 300 * 1024 * 1024:
        alarms.append(
            f"logs/ + JSONL ledgers {_fmt_bytes(ledger_bytes)} > 300MB -- run the "
            f"compaction/rotation job."
        )
    # KB warm latency (Slice 2-3): any entity with enough samples whose warm p95 exceeds
    # the game-plan section-5 3s budget. Min-sample floor so a couple of cold outliers
    # don't cry wolf.
    kl = report.get("kb_latency", {})
    slow = {e: v for e, v in kl.get("by_entity", {}).items()
            if v.get("n", 0) >= 5 and v.get("p95", 0) > 3000}
    if slow:
        parts = ", ".join(f"{e} p95={int(v['p95'])}ms"
                          for e, v in sorted(slow.items(), key=lambda kv: -kv[1]["p95"]))
        alarms.append(
            f"KB warm p95 > 3s: {parts} -- investigate the coarse-scan / partition index."
        )
    sch = report.get("scheduled_tasks", {})
    if sch.get("available") and sch.get("max_concurrent_in_window", 0) > 2:
        times = ", ".join(sch.get("concurrent_peak_times", [])) or "?"
        alarms.append(
            f"up to {sch['max_concurrent_in_window']} tasks share a clock time in "
            f"the 03:00-09:00 window; collisions at {times} -- stagger them."
        )
    # Dashboard drift (Slice 6e): a pinned Cowork artifact with no dashboard-access
    # registry entry -- register it (as a data dashboard, covered_by_existing, or
    # utility) or purge the artifact.
    dd = report.get("dashboard_drift", {})
    if dd.get("available") and dd.get("unregistered"):
        ids = ", ".join(dd["unregistered"])
        alarms.append(
            f"dashboard drift: {len(dd['unregistered'])} pinned artifact(s) not in "
            f"dashboard-access.yaml -- {ids} (register or purge)."
        )
    # Flywheel alarms come pre-evaluated by cora.flywheel_metrics (WS-2) so the
    # thresholds are single-sourced with the nightly health check.
    fw = report.get("flywheel", {})
    if fw.get("available"):
        alarms.extend(f"FLYWHEEL: {msg}" for msg in fw.get("alarm_lines", []))
    return alarms


def _fmt_tok(info: dict) -> str:
    return f"~{info['tokens']:,}" if "tokens" in info else "ERR"


def format_slack(report: dict) -> str:
    """Compact Slack mrkdwn digest of the weekly health metrics."""
    kb = report.get("kb_corpus", {})
    st = report.get("state", {})
    b = report.get("billing", {})
    sch = report.get("scheduled_tasks", {})
    alarms = report.get("alarms", [])

    lines = ["*Cora weekly health* (Phase-0 scaling metrics)"]
    if alarms:
        lines.append(":rotating_light: *Alarms:*")
        lines.extend(f"  - {a}" for a in alarms)
    else:
        lines.append(":white_check_mark: all section-5 thresholds clear")

    if kb.get("available"):
        top = max(kb["by_entity"].items(), key=lambda kv: kv[1]) if kb.get("by_entity") else ("?", 0)
        lines.append(
            f"*KB:* {kb['total_chunks']:,} chunks | FNDR co-scan "
            f"{kb['fndr_share_pct']}% | largest {top[0]} ({top[1]:,})"
        )
    sc = report.get("static_context", {})
    big = {e: sc[e] for e in ("F3E", "OSN", "LEX", "FNDR") if e in sc and "tokens" in sc[e]}
    if big:
        lines.append(
            "*Static ctx/entity:* "
            + " | ".join(f"{e} {_fmt_tok(sc[e])} tok" for e in big)
            + f" | tools {report.get('tool_block', {}).get('tool_count', '?')}/"
            + f"~{report.get('tool_block', {}).get('approx_tokens', 0):,}"
        )
    lines.append(
        f"*Billing* ({b.get('usage_lines', 0)} lines): median input "
        f"{b.get('median_input', 0):,.0f} | cache_read/input {b.get('cache_read_over_input', 0)}"
    )
    if b.get("script_lines"):
        top_scripts = list(b.get("script_usage", {}).items())[:4]
        script_str = " | ".join(
            f"{name} ${row['est_usd']:.2f}/{row['calls']}c" for name, row in top_scripts)
        lines.append(
            f"*Script LLM spend* ({b['script_lines']} calls, "
            f"{len(b.get('logs_parsed', []))}d window): ~${b.get('script_est_usd', 0):.2f}"
            f"{' (partial est)' if b.get('script_est_incomplete') else ''} | {script_str}"
        )
    kl = report.get("kb_latency", {})
    if kl.get("samples", 0):
        top = sorted(kl.get("by_entity", {}).items(),
                     key=lambda kv: -kv[1].get("p95", 0))[:3]
        ent_str = " | ".join(f"{e} p50/{int(v['p50'])} p95/{int(v['p95'])}ms" for e, v in top)
        lines.append(
            f"*KB latency* ({kl['samples']} q): overall p50 {int(kl['overall_p50'])} / "
            f"p95 {int(kl['overall_p95'])}ms | {ent_str}"
        )
    lines.append(
        f"*Disk:* cora_kb.db {_fmt_bytes(st.get('cora_kb_db_bytes', 0))} | "
        f"logs/ {_fmt_bytes(st.get('logs_dir_bytes', 0))}"
    )
    if sch.get("available"):
        lines.append(
            f"*Tasks:* {sch['cora_task_count']} cora | "
            f"{len(sch.get('early_window_0300_0900', []))} in 03:00-09:00 | "
            f"peak {sch.get('max_concurrent_in_window', 0)} at one clock time"
        )
    fw = report.get("flywheel", {})
    if fw.get("available"):
        lines.append(
            f"*Flywheel:* knowledge DMs 7d {fw.get('knowledge_dms_7d', '?')} | "
            f"gaps newest "
            + (f"{fw['gaps_last_entry_age_days']:.0f}d"
               if fw.get("gaps_last_entry_age_days") is not None else "n/a")
            + f" | mined 7d {fw.get('gap_autofill_proposed_7d', '?')} | "
            f"shadow {fw.get('shadow_records', 0)}rec/{fw.get('shadow_days', 0)}d | "
            f"PENDING {fw.get('pending_total', '?')}"
        )
    lines.append(f"_token method: {report.get('token_method')}_")
    return "\n".join(lines)


def post_slack(message: str, channel: str) -> bool:
    """Post the digest to Slack via chat.postMessage. Returns success."""
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        print("SLACK_BOT_TOKEN not set -- not posting.", file=sys.stderr)
        return False
    try:
        import httpx  # noqa: PLC0415
        from cora.slack_egress import sanitize_text  # noqa: PLC0415 -- B1: raw POST bypasses the WebClient patch
        resp = httpx.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            json={"channel": channel, "text": sanitize_text(message),
                  "unfurl_links": False, "unfurl_media": False},
            timeout=15,
        )
        ok = bool(resp.json().get("ok"))
        if not ok:
            print(f"Slack post failed: {resp.text}", file=sys.stderr)
        return ok
    except Exception as exc:  # noqa: BLE001
        print(f"Slack post error: {exc}", file=sys.stderr)
        return False


def render(report: dict) -> None:
    print("=" * 72)
    print("CORA HEALTH REPORT (Phase 0 baseline)")
    print(f"token method: {report['token_method']}")
    print("=" * 72)

    alarms = report.get("alarms", [])
    print("\n[0] THRESHOLD ALARMS")
    if alarms:
        for a in alarms:
            print(f"  !! {a}")
    else:
        print("  all section-5 thresholds clear")

    # 1. KB corpus
    kb = report["kb_corpus"]
    print("\n[1] KB CORPUS")
    if not kb.get("available"):
        print(f"  unavailable: {kb.get('reason')}")
    else:
        print(f"  total chunks: {kb['total_chunks']:,}")
        print(f"  FNDR co-scan share: {kb['fndr_chunks']:,} "
              f"({kb['fndr_share_pct']}%)  [threshold to act: >60%]")
        print(f"  sub_entity-tagged: {kb['sub_entity_tagged']:,}")
        print("  by entity:")
        for ent, c in kb["by_entity"].items():
            print(f"    {ent:<10} {c:>9,}")
        print("  by source:")
        for src, c in kb["by_source"].items():
            print(f"    {src:<14} {c:>9,}")

    # 2. static context
    print("\n[2] STATIC-CONTEXT TOKENS (uncached mass moved by the caching split)")
    for ent, info in report["static_context"].items():
        if "error" in info:
            print(f"    {ent:<10} ERROR: {info['error']}")
        else:
            print(f"    {ent:<10} ~{info['tokens']:>7,} tok  ({info['chars']:,} chars)")

    # 3. tools
    tb = report["tool_block"]
    print("\n[3] TOOL-DEFINITION BLOCK")
    print(f"    tools: {tb['tool_count']}  | ~{tb['approx_tokens']:,} tok "
          f"({tb['serialized_chars']:,} chars serialized)")

    # 4. billing
    b = report["billing"]
    print(f"\n[4] RECENT BILLING (last {len(b['logs_parsed'])} log files, "
          f"{b['usage_lines']:,} usage lines)")
    print(f"    median input:        {b['median_input']:,.0f}")
    print(f"    median cache_read:   {b['median_cache_read']:,.0f}")
    print(f"    median cache_create: {b['median_cache_create']:,.0f}")
    print(f"    median output:       {b['median_output']:,.0f}")
    print(f"    cache_read / input:  {b['cache_read_over_input']}  "
          f"<-- BASELINE; the caching split should raise this")
    if b.get("script_lines"):
        print(f"\n    SCRIPT-SIDE callers (caller= lines, "
              f"{b.get('script_files_parsed', 0)} extra log files, "
              f"~${b.get('script_est_usd', 0):.2f} est over the window"
              f"{', partial' if b.get('script_est_incomplete') else ''}):")
        for name, row in b.get("script_usage", {}).items():
            print(f"      {name:<28} {row['calls']:>4} calls  "
                  f"in {row['input']:>9,}  out {row['output']:>8,}  "
                  f"~${row['est_usd']:.3f}")
    else:
        print("    (no script-side caller= usage lines in window yet)")

    # 4b. KB latency (Slice 2-3)
    kl = report.get("kb_latency", {})
    print(f"\n[4b] KB WARM LATENCY ({kl.get('samples', 0):,} queries)  "
          f"[threshold to alarm: p95 > 3000ms]")
    if not kl.get("samples"):
        print("    no kb_ms samples in the parsed logs")
    else:
        print(f"    overall: p50 {int(kl['overall_p50'])}ms  p95 {int(kl['overall_p95'])}ms")
        for ent, v in sorted(kl.get("by_entity", {}).items(), key=lambda kv: -kv[1]["p95"]):
            print(f"      {ent:<10} n={v['n']:>4}  p50 {int(v['p50']):>5}ms  p95 {int(v['p95']):>5}ms")

    # 5. state
    s = report["state"]
    print("\n[5] STATE-STORE SIZES")
    print(f"    cora_kb.db: {_fmt_bytes(s['cora_kb_db_bytes'])}")
    print(f"    logs/ dir:  {_fmt_bytes(s['logs_dir_bytes'])}")
    print("    state DBs:")
    for name, sz in s["state_dbs"].items():
        print(f"      {name:<28} {_fmt_bytes(sz)}")
    print("    JSONL ledgers:")
    for name, sz in s["jsonl_ledgers"].items():
        print(f"      {name:<48} {_fmt_bytes(sz)}")

    # 6. scheduled tasks
    st = report["scheduled_tasks"]
    print("\n[6] SCHEDULED TASKS")
    if not st.get("available"):
        print(f"    unavailable: {st.get('reason')}")
    else:
        print(f"    cora tasks: {st['cora_task_count']}")
        if st["early_window_overlap"]:
            print(f"    !! OVERLAP in 03:00-09:00 window "
                  f"({len(st['early_window_0300_0900'])} tasks):")
        else:
            print(f"    03:00-09:00 window tasks: "
                  f"{len(st['early_window_0300_0900'])}")
        for t in st["early_window_0300_0900"]:
            print(f"      {t['name']:<40} {t['next_run']}")

    # 7. flywheel
    fw = report.get("flywheel", {})
    print("\n[7] FLYWHEEL (knowledge-loop throughput)")
    if not fw.get("available"):
        print(f"    unavailable: {fw.get('reason')}")
    else:
        for line in fw.get("display_lines", []):
            print(f"    {line}")

    print("\n" + "=" * 72)


def build_report(log_days: int, use_api: bool) -> dict:
    counter, method = _make_token_counter(use_api)
    report = {
        "token_method": method,
        "kb_corpus": kb_corpus(),
        "static_context": static_context_tokens(counter),
        "tool_block": tool_block_tokens(counter),
        "billing": recent_billing(log_days),
        "kb_latency": kb_latency(log_days),
        "state": state_sizes(),
        "scheduled_tasks": scheduled_tasks(),
        "flywheel": flywheel_metrics_section(),
        "dashboard_drift": dashboard_drift_section(),
    }
    report["alarms"] = threshold_alarms(report)
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="Cora Phase 0 health report.")
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    ap.add_argument("--count-tokens", action="store_true",
                    help="Use Anthropic count_tokens (accurate; needs API key).")
    ap.add_argument("--log-days", type=int, default=3,
                    help="How many recent cora-*.log files to parse for billing.")
    ap.add_argument("--slack", action="store_true",
                    help="Post the compact digest to Slack (weekly task uses this).")
    ap.add_argument("--channel", default="",
                    help="Slack channel (default HEALTH_REPORT_CHANNEL env or hjrg-leadership).")
    args = ap.parse_args()

    report = build_report(args.log_days, args.count_tokens)

    if args.slack:
        channel = args.channel or os.environ.get("HEALTH_REPORT_CHANNEL", "hjrg-leadership")
        msg = format_slack(report)
        ok = post_slack(msg, channel)
        print(msg)
        print(f"\n[slack] posted to #{channel}: {ok}")
        return 0 if ok else 1

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        render(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
