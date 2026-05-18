"""Daily log backup — copies operational data to Google Drive for disaster recovery.

Backs up:
- logs/knowledge-gaps.jsonl (CRITICAL — captured gaps that haven't been reviewed yet)
- Recent main log files (last 7 days)
- .resolved-gaps.jsonl (tracks which gaps have been ingested)

Destination: G:\My Drive\HJR-Founder-OS\_shared\projects\cora\backups\YYYY-MM-DD\

Usage (manual or scheduled):
    uv run python scripts/backup_logs.py
    uv run python scripts/backup_logs.py --dry-run
    uv run python scripts/backup_logs.py --keep-days 30

Runs daily at 4:30am via cowork-cora-backup Task Scheduler entry (fires before the 5am digest).
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = REPO_ROOT / "logs"
RESOLVED_FILE = REPO_ROOT / "design" / "known-answers" / ".resolved-gaps.jsonl"

DEFAULT_BACKUP_ROOT = Path(
    "G:/My Drive/HJR-Founder-OS/_shared/projects/cora/backups"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Back up Cora logs to Drive for DR.")
    parser.add_argument(
        "--backup-root",
        type=Path,
        default=DEFAULT_BACKUP_ROOT,
        help=f"Root directory for backups (default: {DEFAULT_BACKUP_ROOT})",
    )
    parser.add_argument(
        "--keep-days",
        type=int,
        default=30,
        help="Delete backup directories older than N days (default: 30). 0 = keep forever.",
    )
    parser.add_argument(
        "--include-main-logs-days",
        type=int,
        default=7,
        help="Back up main cora-*.log files from the last N days (default: 7).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be copied without actually doing it.",
    )
    return parser.parse_args()


def ensure_dir(path: Path, dry_run: bool) -> None:
    if dry_run:
        print(f"  [dry-run] mkdir: {path}")
        return
    path.mkdir(parents=True, exist_ok=True)


def copy_file(src: Path, dst: Path, dry_run: bool) -> bool:
    """Copy a single file. Returns True if copied, False if source missing."""
    if not src.exists():
        print(f"  SKIP (source missing): {src.name}")
        return False
    if dry_run:
        size = src.stat().st_size
        print(f"  [dry-run] copy: {src.name} ({size} bytes) -> {dst}")
        return True
    shutil.copy2(src, dst)
    print(f"  Copied: {src.name} ({src.stat().st_size} bytes)")
    return True


def backup_critical_files(dest_dir: Path, dry_run: bool) -> int:
    """Back up the critical files: knowledge-gaps.jsonl and .resolved-gaps.jsonl."""
    count = 0
    print("[1/3] Backing up critical knowledge-gap files...")

    # knowledge-gaps.jsonl — the captured-gaps queue
    kg_path = LOGS_DIR / "knowledge-gaps.jsonl"
    if copy_file(kg_path, dest_dir / "knowledge-gaps.jsonl", dry_run):
        count += 1

    # .resolved-gaps.jsonl — the ingestion-history tracker
    if copy_file(RESOLVED_FILE, dest_dir / "resolved-gaps.jsonl", dry_run):
        count += 1

    return count


def backup_recent_main_logs(dest_dir: Path, days: int, dry_run: bool) -> int:
    """Back up cora-YYYY-MM-DD.log files from the last `days` days."""
    print(f"[2/3] Backing up main logs (last {days} days)...")
    if not LOGS_DIR.exists():
        print(f"  Logs directory does not exist: {LOGS_DIR}")
        return 0

    cutoff = datetime.now() - timedelta(days=days)
    count = 0
    for log_file in sorted(LOGS_DIR.glob("cora-*.log")):
        try:
            # Parse date from filename: cora-YYYY-MM-DD.log
            date_str = log_file.stem.replace("cora-", "")
            file_date = datetime.fromisoformat(date_str)
        except ValueError:
            print(f"  SKIP (unparseable date): {log_file.name}")
            continue

        if file_date < cutoff:
            continue  # too old, skip

        if copy_file(log_file, dest_dir / log_file.name, dry_run):
            count += 1

    return count


def prune_old_backups(backup_root: Path, keep_days: int, dry_run: bool) -> int:
    """Delete backup directories older than `keep_days` days. Returns count pruned."""
    if keep_days <= 0:
        print("[3/3] Pruning disabled (keep_days=0).")
        return 0

    print(f"[3/3] Pruning backups older than {keep_days} days...")
    if not backup_root.exists():
        return 0

    cutoff_date = date.today() - timedelta(days=keep_days)
    count = 0
    for entry in sorted(backup_root.iterdir()):
        if not entry.is_dir():
            continue
        try:
            entry_date = date.fromisoformat(entry.name)
        except ValueError:
            continue  # not a YYYY-MM-DD directory, skip

        if entry_date < cutoff_date:
            if dry_run:
                print(f"  [dry-run] would prune: {entry.name}")
            else:
                shutil.rmtree(entry)
                print(f"  Pruned: {entry.name}")
            count += 1
    return count


def main() -> int:
    args = parse_args()

    today = date.today().isoformat()
    dest_dir = args.backup_root / today

    print()
    print("=" * 60)
    print(f"  Cora Log Backup -- {today}")
    print("=" * 60)
    print()
    print(f"Source repo:    {REPO_ROOT}")
    print(f"Backup target:  {dest_dir}")
    print(f"Dry run:        {args.dry_run}")
    print()

    if not args.backup_root.exists() and not args.dry_run:
        try:
            args.backup_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"ERROR: cannot create backup root {args.backup_root}: {exc}")
            print("Is Google Drive mounted at the expected path?")
            return 1

    ensure_dir(dest_dir, args.dry_run)

    critical_count = backup_critical_files(dest_dir, args.dry_run)
    main_log_count = backup_recent_main_logs(dest_dir, args.include_main_logs_days, args.dry_run)
    pruned_count = prune_old_backups(args.backup_root, args.keep_days, args.dry_run)

    print()
    print("=" * 60)
    print("  Summary")
    print("=" * 60)
    print(f"  Critical files backed up: {critical_count}")
    print(f"  Main log files backed up: {main_log_count}")
    print(f"  Old backups pruned:       {pruned_count}")
    print()

    if critical_count == 0:
        print("WARNING: no critical files were backed up. This is unusual for a healthy Cora deploy.")
        print("Check that logs/knowledge-gaps.jsonl exists (it's created when Cora first flags a gap).")

    return 0


if __name__ == "__main__":
    sys.exit(main())
