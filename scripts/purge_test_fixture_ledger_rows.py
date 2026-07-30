"""Purge test-fixture rows that leaked into live ledgers (Slice 5, 2026-07-29 audit).

Root cause (now fixed in tests/conftest.py): the two rollout flags were flipped
ON in .env (CORA_AUTOWRITE_LIVE=all, CORA_CODE_QUEUE=live) and the test process
inherited them, so tests that drove the autowrite / code-queue write paths appended
fixture rows to the REAL ledgers instead of a tmp path. This script removes those
fixture rows. It is DRY-RUN by default; Harrison runs `--apply`.

Design (conservative -- "when in doubt, keep + flag"):
  * Each target file has an explicit, documented fixture predicate. A row is
    removed ONLY when it matches a strong, unambiguous fixture signature.
  * A weak/ambiguous match is KEPT and printed as a WARNING for human review.
  * --apply writes a timestamped .bak backup, then atomically rewrites the file
    without the fixture rows. Real rows are preserved byte-for-byte.

Concurrency note: code-session-queue.jsonl is written by the always-on bot. Run
`--apply` when the bot is quiet (or simply re-run -- the purge is idempotent and a
row appended mid-run is caught on the next pass).

Usage:
    python scripts/purge_test_fixture_ledger_rows.py            # dry-run (default)
    python scripts/purge_test_fixture_ledger_rows.py --apply    # remove + backup
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

# The hang_tool fixture: a made-up tool name used by the code-queue tests. It does
# not exist anywhere in the real tool catalog, so any live-ledger row referencing
# it is a leaked fixture.
_HANG_TOOL = "hang_tool"
_HANG_FINGERPRINT = "2d13e7591b068881a205407bbc21280f107e2e14"


def _is_autowrite_fixture(row: dict) -> tuple[bool, str]:
    """cora-autowrite-audit.jsonl: the ka-x / U-TOMMY fixture record."""
    if row.get("update_id") == "ka-x":
        return True, "update_id == 'ka-x' (test fixture id)"
    if row.get("contributor") == "U-TOMMY":
        return True, "contributor == 'U-TOMMY' (test fixture user)"
    return False, ""


def _is_code_queue_fixture(row: dict) -> tuple[bool, str]:
    """code-session-queue.jsonl: rows tied to the hang_tool fixture item."""
    for field in ("title", "summary", "subsystem_guess", "representative"):
        if _HANG_TOOL in str(row.get(field) or "").lower():
            return True, f"{field} references '{_HANG_TOOL}' (test fixture)"
    if row.get("fingerprint") == _HANG_FINGERPRINT:
        return True, f"fingerprint == hang_tool fixture fingerprint"
    return False, ""


def _is_signals_fixture(row: dict) -> tuple[bool, str]:
    """code-queue-signals.jsonl: hang_tool tool_failure signals."""
    if str(row.get("key") or "").lower() == _HANG_TOOL:
        return True, f"signal key == '{_HANG_TOOL}' (test fixture)"
    return False, ""


def _is_fingerprints_fixture(row: dict) -> tuple[bool, str]:
    """code-queue-fingerprints.jsonl: the hang_tool fingerprint ONLY.

    Row 0 (asana_create_task phantom-failure) is a REAL queued item and must be
    preserved -- key on the exact hang_tool fingerprint / representative, never on
    the generic 'tool_error' signal (which real items also carry)."""
    if row.get("fingerprint") == _HANG_FINGERPRINT:
        return True, "fingerprint == hang_tool fixture fingerprint"
    if str(row.get("representative") or "").lower() == _HANG_TOOL:
        return True, f"representative == '{_HANG_TOOL}' (test fixture)"
    return False, ""


# (repo-relative path, predicate) -- add here if more contamination is found.
_TARGETS = [
    ("logs/cora-autowrite-audit.jsonl", _is_autowrite_fixture),
    ("data/state/code-session-queue.jsonl", _is_code_queue_fixture),
    ("data/state/code-queue-signals.jsonl", _is_signals_fixture),
    ("data/state/code-queue-fingerprints.jsonl", _is_fingerprints_fixture),
]


def _process(rel_path: str, predicate, apply: bool) -> tuple[int, int]:
    """Return (kept, removed). Dry-run prints; --apply rewrites with a backup."""
    path = _REPO_ROOT / rel_path
    if not path.exists():
        print(f"  {rel_path}: (absent -- skipped)")
        return 0, 0

    keep_lines: list[str] = []
    removed: list[dict] = []
    unparseable = 0
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError:
                # Never drop a line we cannot parse -- keep + flag (conservative).
                unparseable += 1
                keep_lines.append(stripped)
                continue
            if not isinstance(row, dict):
                keep_lines.append(stripped)
                continue
            is_fixture, why = predicate(row)
            if is_fixture:
                removed.append((row, why))
            else:
                keep_lines.append(stripped)

    print(f"  {rel_path}: keep={len(keep_lines)} remove={len(removed)}"
          + (f" unparseable_kept={unparseable}" if unparseable else ""))
    for row, why in removed:
        preview = json.dumps(row, ensure_ascii=False)
        if len(preview) > 140:
            preview = preview[:140] + "..."
        print(f"     REMOVE [{why}]: {preview}")

    if apply and removed:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        backup = path.with_suffix(path.suffix + f".bak-{stamp}")
        backup.write_bytes(path.read_bytes())
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for ln in keep_lines:
                fh.write(ln + "\n")
        tmp.replace(path)
        print(f"     APPLIED -> backup at {backup.name}, {len(removed)} row(s) removed")

    return len(keep_lines), len(removed)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Actually remove the fixture rows (writes a .bak backup). "
                         "Default is a read-only dry run.")
    args = ap.parse_args()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"Purge test-fixture ledger rows [{mode}]\n")
    total_removed = 0
    for rel_path, predicate in _TARGETS:
        _, removed = _process(rel_path, predicate, args.apply)
        total_removed += removed

    print()
    if args.apply:
        print(f"Done. {total_removed} fixture row(s) removed across "
              f"{len(_TARGETS)} ledger(s).")
    else:
        print(f"Dry run: {total_removed} fixture row(s) would be removed. "
              f"Re-run with --apply to remove them (a .bak backup is written first).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
