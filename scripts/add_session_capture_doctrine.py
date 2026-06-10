#!/usr/bin/env python3
"""Append the session-capture doctrine block to each entity-level CLAUDE.md.

Idempotent + append-only: skips any file that already carries the marker. The
founder master brief (HJR-Founder-OS\\CLAUDE.md) and the Cora project brief are
edited separately; this covers the nine entity folders (01-09).

Usage:
    .venv\\Scripts\\python.exe scripts\\add_session_capture_doctrine.py [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

FOUNDER_OS_ROOT = Path(r"G:\My Drive\HJR-Founder-OS")
MARKER = "<!-- session-capture-doctrine -->"

ENTITY_FOLDERS = {
    "01-HJR-Global": "HJRG",
    "02-F3-Energy": "F3E",
    "03-F3-Community": "F3C",
    "04-UFL": "UFL",
    "05-HJR-Productions": "HJRPROD",
    "06-HJR-Properties": "HJRP",
    "07-Big-D-Media": "BDM",
    "08-Lexington-Services": "LEX",
    "09-One-Stop-Nutrition": "OSN",
}

_BASE = (
    "End every session by writing a dated capture note to "
    "`_session-captures/YYYY-MM/` in this entity's folder — a short distilled "
    "summary (Decisions / Facts learned / Action items / Open questions). The "
    "nightly `static_md` sync ingests it into Cora's KB; the "
    "`cowork-cora-session-capture` task is only a backstop. Canonical promotion "
    "into this brief / `memory/` stays behind Harrison's 👍 review."
)

_LEX_EXTRA = (
    " LEX sessions are captured **in full** into the LEX-scoped store; client "
    "PHI is access-gated by the custodian allowlist (`lex-phi-custodians.yaml`) "
    "and never surfaces outside LEX scope."
)


def _block(entity: str) -> str:
    body = _BASE + (_LEX_EXTRA if entity == "LEX" else "")
    return f"\n\n{MARKER}\n## Session capture\n\n{body}\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    changed, skipped, missing = 0, 0, 0
    for folder, entity in ENTITY_FOLDERS.items():
        path = FOUNDER_OS_ROOT / folder / "CLAUDE.md"
        if not path.exists():
            print(f"MISSING  {path}")
            missing += 1
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if MARKER in text:
            print(f"SKIP     {folder} (already has doctrine)")
            skipped += 1
            continue
        if args.dry_run:
            print(f"[DRY] would append doctrine to {folder} (entity={entity})")
            changed += 1
            continue
        path.write_text(text.rstrip() + _block(entity), encoding="utf-8")
        print(f"APPEND   {folder} (entity={entity})")
        changed += 1

    print(f"\nchanged={changed} skipped={skipped} missing={missing}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
