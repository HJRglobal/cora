r"""Daily backup — copies operational data to Google Drive for disaster recovery.

Backs up:
- data/cora_kb.db (CRITICAL — the entire knowledge base; uses SQLite online backup API)
- data/*.db feature databases (influencer, hubspot snapshots, etc.; online backup API)
- .env + the Google service-account JSON, bundled into a SINGLE ENCRYPTED blob
  (secrets-YYYY-MM-DD.enc). Requires CORA_BACKUP_PASSPHRASE in the environment;
  if unset, the secrets step is skipped (it never writes plaintext secrets).
- logs/knowledge-gaps.jsonl (CRITICAL — captured gaps that haven't been reviewed yet)
- Recent main log files (last 7 days)
- .resolved-gaps.jsonl (tracks which gaps have been ingested)

After writing, the run VERIFIES the KB backup actually landed at the destination and
returns a non-zero exit code if it did not (so a silent offsite failure is caught).

Destination: G:\My Drive\HJR-Founder-OS\_shared\projects\cora\backups\YYYY-MM-DD\

Restore the encrypted secrets with: python scripts/restore_secrets.py <secrets-*.enc>

Usage (manual or scheduled — use the repo venv, NOT uv, per D-005):
    .venv\\Scripts\\python.exe scripts/backup_logs.py
    .venv\\Scripts\\python.exe scripts/backup_logs.py --dry-run
    .venv\\Scripts\\python.exe scripts/backup_logs.py --keep-days 30

Runs daily at 1:00pm via the cowork-cora-backup Task Scheduler entry (off the
overnight kb-sync window so the online backup reads a quiescent KB).
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import shutil
import sys
import tarfile
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = REPO_ROOT / "logs"
RESOLVED_FILE = REPO_ROOT / "design" / "known-answers" / ".resolved-gaps.jsonl"
SNAPSHOTS_DIR = REPO_ROOT / "data" / "snapshots"
DATA_DIR = REPO_ROOT / "data"
KB_DB_PATH = REPO_ROOT / "data" / "cora_kb.db"
ENV_PATH = REPO_ROOT / ".env"

# Read .env so GOOGLE_SERVICE_ACCOUNT_JSON + CORA_BACKUP_PASSPHRASE are available.
load_dotenv(ENV_PATH, override=True)

# PBKDF2 iteration count for deriving the secrets-encryption key from the passphrase.
_KDF_ITERATIONS = 600_000

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


def backup_snapshots(dest_dir: Path, dry_run: bool) -> int:
    """Copy data/snapshots/ recursively to <dest_dir>/snapshots/. Skip if absent."""
    print("[4/7] Backing up snapshots...")
    if not SNAPSHOTS_DIR.exists():
        print(f"  SKIP: {SNAPSHOTS_DIR} does not exist yet.")
        return 0

    snap_dest = dest_dir / "snapshots"
    if dry_run:
        count = sum(1 for _ in SNAPSHOTS_DIR.rglob("*") if _.is_file())
        print(f"  [dry-run] would copy {count} snapshot file(s) to {snap_dest}")
        return count

    if snap_dest.exists():
        shutil.rmtree(snap_dest)
    shutil.copytree(SNAPSHOTS_DIR, snap_dest)
    count = sum(1 for _ in snap_dest.rglob("*") if _.is_file())
    print(f"  Snapshot files backed up: {count}")
    return count


def backup_kb_database(dest_dir: Path, dry_run: bool) -> bool:
    """Back up cora_kb.db using SQLite online backup API (safe while Cora is running)."""
    print("[1/7] Backing up knowledge base (cora_kb.db)...")
    if not KB_DB_PATH.exists():
        print(f"  SKIP: {KB_DB_PATH} does not exist yet.")
        return False
    dst = dest_dir / "cora_kb.db"
    size_mb = KB_DB_PATH.stat().st_size / (1024 * 1024)
    if dry_run:
        print(f"  [dry-run] would backup cora_kb.db ({size_mb:.1f} MB) -> {dst}")
        return True
    import sqlite3
    src_conn = sqlite3.connect(str(KB_DB_PATH))
    dst_conn = sqlite3.connect(str(dst))
    try:
        src_conn.backup(dst_conn)
        print(f"  KB backup complete: {dst} ({size_mb:.1f} MB)")
        return True
    except Exception as exc:
        print(f"  ERROR: KB backup failed: {exc}")
        return False
    finally:
        src_conn.close()
        dst_conn.close()


def backup_critical_files(dest_dir: Path, dry_run: bool) -> int:
    """Back up the critical files: knowledge-gaps.jsonl and .resolved-gaps.jsonl."""
    count = 0
    print("[2/7] Backing up critical knowledge-gap files...")

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
    print(f"[3/7] Backing up main logs (last {days} days)...")
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
        print("[7/7] Pruning disabled (keep_days=0).")
        return 0

    print(f"[7/7] Pruning backups older than {keep_days} days...")
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


def backup_feature_dbs(dest_dir: Path, dry_run: bool) -> int:
    """Online-backup the small SQLite feature DBs (every data/*.db except cora_kb.db)."""
    print("[5/7] Backing up feature databases...")
    if not DATA_DIR.exists():
        print(f"  SKIP: {DATA_DIR} does not exist.")
        return 0
    import sqlite3
    count = 0
    for db in sorted(DATA_DIR.glob("*.db")):
        if db.name == "cora_kb.db":
            continue  # handled separately by backup_kb_database
        dst = dest_dir / db.name
        if dry_run:
            print(f"  [dry-run] would back up {db.name}")
            count += 1
            continue
        try:
            src_conn = sqlite3.connect(str(db))
            dst_conn = sqlite3.connect(str(dst))
            try:
                src_conn.backup(dst_conn)
                count += 1
                print(f"  Backed up: {db.name}")
            finally:
                src_conn.close()
                dst_conn.close()
        except Exception as exc:
            print(f"  ERROR backing up {db.name}: {exc}")
    print(f"  Feature DBs backed up: {count}")
    return count


def _collect_secret_files() -> list[tuple[str, Path]]:
    """Return [(arcname, path)] for the secrets to back up, only those that exist."""
    items: list[tuple[str, Path]] = []
    if ENV_PATH.exists():
        items.append((".env", ENV_PATH))
    sa = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if sa:
        sa_path = Path(sa)
        if sa_path.exists():
            items.append((sa_path.name, sa_path))
    return items


def _encrypt_bytes(plaintext: bytes, passphrase: str) -> bytes:
    """Encrypt with Fernet using a PBKDF2-derived key. Returns salt(16) + token."""
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    salt = os.urandom(16)
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=_KDF_ITERATIONS
    )
    key = base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))
    token = Fernet(key).encrypt(plaintext)
    return salt + token


def backup_secrets(dest_dir: Path, dry_run: bool) -> str:
    """Bundle .env + the SA JSON into ONE encrypted blob. Never writes plaintext.

    Returns a status string: 'ok' / 'dry-run' / 'no-secrets' / 'no-passphrase' / 'no-crypto'.
    Skips (loudly) rather than failing if the passphrase is unset, so the rest of the
    backup still runs. Restore with scripts/restore_secrets.py.
    """
    print("[6/7] Backing up secrets (encrypted)...")
    secret_files = _collect_secret_files()
    if not secret_files:
        print("  SKIP: no secret files found (.env / SA JSON).")
        return "no-secrets"

    passphrase = os.environ.get("CORA_BACKUP_PASSPHRASE", "").strip()
    if not passphrase:
        print("  SKIP: CORA_BACKUP_PASSPHRASE not set -- refusing to write plaintext secrets.")
        print("        Set it (and store it in your password manager) to enable this step.")
        return "no-passphrase"

    names = ", ".join(arc for arc, _ in secret_files)
    if dry_run:
        print(f"  [dry-run] would encrypt {len(secret_files)} file(s) [{names}] -> secrets-{date.today().isoformat()}.enc")
        return "dry-run"

    # Bundle the secret files + a manifest (original absolute paths) into an in-memory tar.
    buf = io.BytesIO()
    manifest = {arc: str(path) for arc, path in secret_files}
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for arcname, path in secret_files:
            tar.add(str(path), arcname=arcname)
        man_bytes = json.dumps(manifest, indent=2).encode("utf-8")
        info = tarfile.TarInfo("MANIFEST.json")
        info.size = len(man_bytes)
        tar.addfile(info, io.BytesIO(man_bytes))

    try:
        blob = _encrypt_bytes(buf.getvalue(), passphrase)
    except ImportError:
        print("  SKIP: 'cryptography' not importable -- cannot encrypt secrets.")
        return "no-crypto"

    out = dest_dir / f"secrets-{date.today().isoformat()}.enc"
    out.write_bytes(blob)
    print(f"  Encrypted secrets written: {out.name} ({len(blob)} bytes, {len(secret_files)} files: {names})")
    return "ok"


def verify_offsite(dest_dir: Path, kb_ok: bool, dry_run: bool) -> bool:
    """Confirm the critical KB backup actually materialized at the destination.

    This is the loud-failure guard: a backup run that 'succeeds' but leaves no KB
    file at the offsite path is a silent DR failure. Returns False in that case so
    main() can exit non-zero.
    """
    print("Verifying offsite KB backup...")
    if dry_run:
        print("  [dry-run] skipping verification.")
        return True
    if not kb_ok:
        print("  FAIL: the KB backup step reported failure.")
        return False
    dst = dest_dir / "cora_kb.db"
    if not dst.exists() or dst.stat().st_size == 0:
        print(f"  FAIL: {dst} is missing or empty -- the offsite KB backup did not land.")
        return False
    src_mb = KB_DB_PATH.stat().st_size / (1024 * 1024) if KB_DB_PATH.exists() else 0
    dst_mb = dst.stat().st_size / (1024 * 1024)
    if src_mb and dst_mb < src_mb * 0.5:
        # Online backup compacts (drops WAL slack), so some shrink is normal; <50% is suspicious.
        print(f"  WARN: backup is {dst_mb:.0f} MB vs source {src_mb:.0f} MB (<50%) -- worth a manual look.")
    print(f"  OK: KB backup present at destination ({dst_mb:.0f} MB).")
    return True


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

    kb_ok = backup_kb_database(dest_dir, args.dry_run)
    critical_count = backup_critical_files(dest_dir, args.dry_run)
    main_log_count = backup_recent_main_logs(dest_dir, args.include_main_logs_days, args.dry_run)
    snapshot_count = backup_snapshots(dest_dir, args.dry_run)
    feature_db_count = backup_feature_dbs(dest_dir, args.dry_run)
    secrets_status = backup_secrets(dest_dir, args.dry_run)
    pruned_count = prune_old_backups(args.backup_root, args.keep_days, args.dry_run)
    offsite_ok = verify_offsite(dest_dir, kb_ok, args.dry_run)

    print()
    print("=" * 60)
    print("  Summary")
    print("=" * 60)
    print(f"  KB database backed up:    {'YES' if kb_ok else 'NO (see above)'}")
    print(f"  Feature DBs backed up:    {feature_db_count}")
    print(f"  Secrets (encrypted):      {secrets_status}")
    print(f"  Critical files backed up: {critical_count}")
    print(f"  Main log files backed up: {main_log_count}")
    print(f"  Snapshot files backed up: {snapshot_count}")
    print(f"  Old backups pruned:       {pruned_count}")
    print(f"  Offsite verify:           {'PASS' if offsite_ok else 'FAIL'}")
    print()

    if critical_count == 0:
        print("WARNING: no critical files were backed up. This is unusual for a healthy Cora deploy.")
        print("Check that logs/knowledge-gaps.jsonl exists (it's created when Cora first flags a gap).")

    if secrets_status == "no-passphrase":
        print("NOTE: secrets were NOT backed up (CORA_BACKUP_PASSPHRASE unset). Set it to close the DR gap.")

    # Loud failure: a run that didn't actually land the KB offsite is a DR failure.
    if not args.dry_run and not offsite_ok:
        print("ERROR: offsite KB verification FAILED -- this backup is not safe. Investigate.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
