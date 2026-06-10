"""Restore secrets from an encrypted backup blob produced by backup_logs.py.

Decrypts a secrets-YYYY-MM-DD.enc file (created by backup_logs.backup_secrets)
back into the original .env and Google service-account JSON, using the same
passphrase the backup was made with.

The passphrase is read from CORA_BACKUP_PASSPHRASE, or prompted if unset. This is
the key you stored in your password manager -- it is NOT in the backup itself.

Usage:
    .venv\\Scripts\\python.exe scripts/restore_secrets.py <secrets-*.enc>
    .venv\\Scripts\\python.exe scripts/restore_secrets.py <blob> --dest C:\\restore\\here --dry-run

By default each file is restored to its ORIGINAL absolute path (from the manifest
baked into the blob). --dest writes everything into one directory instead (safer
for inspection before overwriting live secrets). Existing files are NOT overwritten
unless --force is given.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import io
import json
import sys
import tarfile
from pathlib import Path

_KDF_ITERATIONS = 600_000  # must match backup_logs._KDF_ITERATIONS


def _decrypt_bytes(blob: bytes, passphrase: str) -> bytes:
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    salt, token = blob[:16], blob[16:]
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=_KDF_ITERATIONS
    )
    key = base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))
    return Fernet(key).decrypt(token)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Restore secrets from an encrypted backup blob.")
    p.add_argument("blob", type=Path, help="Path to the secrets-*.enc file.")
    p.add_argument("--dest", type=Path, default=None,
                   help="Restore all files into this directory instead of their original paths.")
    p.add_argument("--force", action="store_true", help="Overwrite existing files.")
    p.add_argument("--dry-run", action="store_true", help="Show what would be restored, write nothing.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if not args.blob.exists():
        print(f"ERROR: blob not found: {args.blob}")
        return 1

    passphrase = (
        __import__("os").environ.get("CORA_BACKUP_PASSPHRASE", "").strip()
        or getpass.getpass("CORA_BACKUP_PASSPHRASE: ").strip()
    )
    if not passphrase:
        print("ERROR: no passphrase provided.")
        return 1

    try:
        plaintext = _decrypt_bytes(args.blob.read_bytes(), passphrase)
    except Exception as exc:
        print(f"ERROR: decryption failed (wrong passphrase or corrupt blob): {exc}")
        return 1

    tar = tarfile.open(fileobj=io.BytesIO(plaintext), mode="r")
    manifest = {}
    try:
        m = tar.extractfile("MANIFEST.json")
        if m is not None:
            manifest = json.loads(m.read().decode("utf-8"))
    except KeyError:
        pass  # older blob without a manifest

    restored = 0
    for member in tar.getmembers():
        if member.name == "MANIFEST.json" or not member.isfile():
            continue
        if args.dest:
            target = args.dest / member.name
        else:
            target = Path(manifest.get(member.name, member.name))
        print(f"  {member.name} -> {target}")
        if args.dry_run:
            continue
        if target.exists() and not args.force:
            print(f"    SKIP: exists (use --force to overwrite): {target}")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        data = tar.extractfile(member)
        if data is None:
            continue
        target.write_bytes(data.read())
        restored += 1

    tar.close()
    print(f"\nRestored {restored} file(s).{' (dry-run)' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
