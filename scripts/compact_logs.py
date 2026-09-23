"""Log + ledger compaction (game-plan section 10.5).

The unbounded grower is dated log files: ~23 task families each drop one
`<name>-YYYY-MM-DD.log` per day, forever. This gzips dated logs older than the
retention window into logs/archive/ and deletes the originals, then purges
archives past a long horizon. JSONL ledgers are tiny today, so the ledger trim
is SIZE-GATED -- it only touches a *.jsonl once it exceeds --ledger-min-mb, and
even then keeps the last --ledger-days by the "ts" field plus any line it can't
date (fail-safe: never drop an undatable line, never break a throttle lookback).

No SQLite VACUUM here: the live bot holds several state DBs open (VACUUM needs
exclusive access) and cora_kb.db is too heavy for a routine job -- big reclaims
are done manually (see scripts/reclaim_kb_space.py).

Safe to run anytime; archival reads only logs older than the window, which no
live process is still writing. --dry-run reports and changes nothing.

    .venv\\Scripts\\python.exe scripts\\compact_logs.py --dry-run
    .venv\\Scripts\\python.exe scripts\\compact_logs.py
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import shutil
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = REPO_ROOT / "logs"
ARCHIVE_DIR = LOGS_DIR / "archive"
DATA_DIR = REPO_ROOT / "data"

# Matches the date suffix on dated log files: e.g. cora-2026-06-09.log
_DATED_LOG_RE = re.compile(r"-(\d{4})-(\d{2})-(\d{2})\.log$")


def _fmt_mb(n: int) -> str:
    return f"{n / 1024 / 1024:.1f}MB"


def rotate_logs(log_days: int, archive_days: int, dry_run: bool) -> dict:
    """Gzip dated logs older than log_days into logs/archive/; purge archives
    older than archive_days. Dates from the filename (reliable), not mtime."""
    now = time.time()
    cutoff = now - log_days * 86400
    archived = 0
    freed = 0
    if not dry_run:
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    for p in sorted(LOGS_DIR.glob("*.log")):
        m = _DATED_LOG_RE.search(p.name)
        if not m:
            continue
        try:
            d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                         tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
        if d >= cutoff:
            continue  # within retention window -- keep
        size = p.stat().st_size
        if dry_run:
            archived += 1
            freed += size
            continue
        dest = ARCHIVE_DIR / (p.name + ".gz")
        with p.open("rb") as fi, gzip.open(dest, "wb") as fo:
            shutil.copyfileobj(fi, fo)
        p.unlink()
        archived += 1
        freed += size

    purged = 0
    if ARCHIVE_DIR.exists():
        purge_cutoff = now - archive_days * 86400
        for p in ARCHIVE_DIR.glob("*.gz"):
            if p.stat().st_mtime < purge_cutoff:
                if not dry_run:
                    p.unlink()
                purged += 1

    return {"archived": archived, "freed_bytes": freed, "purged": purged}


def _parse_ts(obj: dict) -> float | None:
    ts = obj.get("ts")
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts).timestamp()
        except ValueError:
            return None
    return None


def trim_ledgers(ledger_days: int, min_mb: float, dry_run: bool) -> list[dict]:
    """Trim *.jsonl ledgers over min_mb to the last ledger_days (by 'ts').

    Size-gated so it is a no-op while ledgers are small (avoids racing the live
    appenders for no benefit). Keeps any line that lacks a parseable 'ts' so a
    throttle/dedup lookback is never broken. ledger_days defaults far beyond any
    throttle window (max ~14d), so only genuinely-stale lines are dropped.
    """
    cutoff = time.time() - ledger_days * 86400
    out: list[dict] = []
    for base in (LOGS_DIR, DATA_DIR):
        if not base.exists():
            continue
        for p in sorted(base.glob("*.jsonl")):
            size = p.stat().st_size
            if size < min_mb * 1024 * 1024:
                continue  # too small to bother -- skip (no race risk)
            kept: list[str] = []
            dropped: list[str] = []
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except Exception:  # noqa: BLE001 -- malformed line: keep it
                    kept.append(line)
                    continue
                t = _parse_ts(obj) if isinstance(obj, dict) else None
                if t is None or t >= cutoff:
                    kept.append(line)
                else:
                    dropped.append(line)
            if not dropped:
                continue
            rec = {"file": str(p.relative_to(REPO_ROOT)), "dropped": len(dropped),
                   "kept": len(kept)}
            out.append(rec)
            if dry_run:
                continue
            ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            gz = ARCHIVE_DIR / f"{p.stem}-pruned-{int(time.time())}.jsonl.gz"
            with gzip.open(gz, "wb") as fo:
                fo.write(("\n".join(dropped) + "\n").encode("utf-8"))
            tmp = p.with_suffix(".jsonl.tmp")
            tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
            tmp.replace(p)  # atomic
    return out


def _health_report_dir() -> Path:
    """Same resolution as nightly_health_check._health_report_dir (env var read
    PER CALL, default <repo>/reports/health) -- the conftest autouse fixture
    redirects it for every test."""
    raw = (os.environ.get("CORA_HEALTH_REPORT_DIR") or "").strip()
    return Path(raw) if raw else REPO_ROOT / "reports" / "health"


def prune_reports(days: int, dry_run: bool, *, today=None,
                  report_dir: Path | None = None) -> dict:
    """Delete health-check run artifacts (reports/health/YYYY-MM-DD.{json,md})
    older than `days`, keyed on the FILENAME date (Code #14 S2). A file whose stem
    is not a plain date is never touched. --dry-run counts and deletes nothing."""
    d = report_dir if report_dir is not None else _health_report_dir()
    today = today or date.today()
    cutoff = today - timedelta(days=days)
    pruned = 0
    freed = 0
    if not d.exists():
        return {"pruned": 0, "freed_bytes": 0, "dir": str(d)}
    for p in sorted(d.iterdir()):
        if not p.is_file() or p.suffix not in (".json", ".md"):
            continue
        try:
            file_day = datetime.strptime(p.stem, "%Y-%m-%d").date()
        except ValueError:
            continue  # not a dated artifact -- never ours to delete
        if file_day >= cutoff:
            continue
        size = p.stat().st_size
        if not dry_run:
            p.unlink()
        pruned += 1
        freed += size
    return {"pruned": pruned, "freed_bytes": freed, "dir": str(d)}


def main() -> int:
    ap = argparse.ArgumentParser(description="Cora log + ledger compaction.")
    ap.add_argument("--log-days", type=int, default=30,
                    help="Archive dated logs older than this many days.")
    ap.add_argument("--archive-days", type=int, default=365,
                    help="Purge archived .gz older than this many days.")
    ap.add_argument("--ledger-days", type=int, default=90,
                    help="Keep this many days of JSONL ledger lines.")
    ap.add_argument("--ledger-min-mb", type=float, default=5.0,
                    help="Only trim a .jsonl ledger once it exceeds this size.")
    ap.add_argument("--report-days", type=int, default=90,
                    help="Delete health-check run artifacts (reports/health) older "
                         "than this many days, by filename date.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not LOGS_DIR.exists():
        print(f"ERROR: logs dir not found at {LOGS_DIR}", file=sys.stderr)
        return 2

    tag = "[dry-run] " if args.dry_run else ""
    logs = rotate_logs(args.log_days, args.archive_days, args.dry_run)
    print(f"{tag}logs: archived {logs['archived']} dated logs "
          f"({_fmt_mb(logs['freed_bytes'])}), purged {logs['purged']} old archives "
          f"(retention {args.log_days}d, archive horizon {args.archive_days}d)")

    ledgers = trim_ledgers(args.ledger_days, args.ledger_min_mb, args.dry_run)
    if ledgers:
        for r in ledgers:
            print(f"{tag}ledger {r['file']}: dropped {r['dropped']} lines "
                  f">{args.ledger_days}d, kept {r['kept']}")
    else:
        print(f"{tag}ledgers: none over {args.ledger_min_mb}MB -- nothing to trim")

    reports = prune_reports(args.report_days, args.dry_run)
    print(f"{tag}health reports: pruned {reports['pruned']} artifact file(s) "
          f"({_fmt_mb(reports['freed_bytes'])}) older than {args.report_days}d "
          f"in {reports['dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
