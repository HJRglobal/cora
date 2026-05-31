#!/usr/bin/env python3
"""Backfill sub_entity tags for LEX knowledge chunks that currently have sub_entity IS NULL.

Most NULL-sub_entity LEX chunks are genuinely cross-entity (payroll, training, general ops)
and should stay NULL -- they correctly appear in all LEX channels including the GM-level
#lex-* view. This script only tags chunks that have UNAMBIGUOUS sub-entity signals (unique
keywords that belong to exactly one sub-entity).

Usage:
    .venv\\Scripts\\python.exe scripts\\backfill_lex_sub_entity.py
    .venv\\Scripts\\python.exe scripts\\backfill_lex_sub_entity.py --dry-run
    .venv\\Scripts\\python.exe scripts\\backfill_lex_sub_entity.py --dry-run --verbose
"""

from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "cora_kb.db"

log = logging.getLogger("backfill_lex")

# ---------------------------------------------------------------------------
# Keyword patterns per sub-entity.
# Each entry is (sub_entity, [list of regex patterns]).
# A chunk is tagged only if it matches patterns for EXACTLY ONE sub-entity.
# If it matches patterns for 2+ sub-entities it stays NULL (ambiguous).
# ---------------------------------------------------------------------------
_SUB_ENTITY_PATTERNS: list[tuple[str, list[str]]] = [
    (
        "LEX-LLC",
        [
            r"\[LEX-LLC\]",
            r"\bLexington LLC\b",
            r"\bDay Program\b",
            r"\bSupported Living\b",
            r"\bHCBS\b",
            r"\bJeff Montgomery\b",
            r"\bAaron Ferrucci\b",
            r"\[LEX-LLC\]",
            r"Lexington.*LLC",
        ],
    ),
    (
        "LEX-LTS",
        [
            r"\[LEX-LTS\]",
            r"\bLexington Therapeutic\b",
            r"\bProvider Type 15\b",
            r"\bDDD Therapy Revalidation\b",
            r"\bJustin Gilmore\b",
        ],
    ),
    (
        "LEX-LBHS",
        [
            r"\bLBHS\b",
            r"\[LEX-LBHS\]",
            r"\bBehavioral Health Services\b",
            r"\bCOPA\b",
            r"\bBHRF\b",
            r"\bJared Harker\b",
        ],
    ),
    (
        "LEX-LLA",
        [
            r"\[LEX-LLA\]",
            r"\bLex Life Academy\b",
            r"\bSandy Patel\b",
        ],
    ),
]

# Compile all patterns once
_COMPILED: list[tuple[str, list[re.Pattern]]] = [
    (se, [re.compile(p, re.IGNORECASE) for p in pats])
    for se, pats in _SUB_ENTITY_PATTERNS
]


def _detect_sub_entity(title: str, content: str) -> str | None:
    """Return the sub_entity if UNAMBIGUOUS, else None."""
    text = (title or "") + " " + (content or "")
    matched: set[str] = set()
    for sub_entity, patterns in _COMPILED:
        for pat in patterns:
            if pat.search(text):
                matched.add(sub_entity)
                break  # one match per sub-entity is enough
    if len(matched) == 1:
        return matched.pop()
    return None  # 0 = general LEX; 2+ = ambiguous; both stay NULL


def run(dry_run: bool = False, verbose: bool = False) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute(
        "SELECT chunk_id, title, content FROM knowledge_chunks "
        "WHERE entity='LEX' AND sub_entity IS NULL"
    )
    rows = cur.fetchall()
    log.info("Scanning %d NULL-sub_entity LEX chunks ...", len(rows))

    updates: dict[str, list[str]] = {}  # sub_entity -> [chunk_ids]
    ambiguous = 0
    no_match = 0

    for row in rows:
        chunk_id = row["chunk_id"]
        title = row["title"] or ""
        content = row["content"] or ""
        se = _detect_sub_entity(title, content)
        if se is None:
            # Could be ambiguous (2+ matches) or just general LEX
            # We can't distinguish, but either way: stay NULL
            if verbose:
                # Check if it matched anything at all
                text = title + " " + content
                any_match = any(
                    any(pat.search(text) for pat in pats)
                    for _, pats in _COMPILED
                )
                if any_match:
                    ambiguous += 1
                else:
                    no_match += 1
            else:
                no_match += 1
        else:
            updates.setdefault(se, []).append(chunk_id)
            if verbose:
                log.debug("  -> %s | title=%s", se, title[:80])

    log.info("Results:")
    for se, ids in sorted(updates.items()):
        log.info("  %-12s  %d chunks would be tagged", se, len(ids))
    log.info("  %-12s  %d chunks (general LEX -- stay NULL)", "no match", no_match)
    if verbose:
        log.info("  %-12s  %d chunks (ambiguous -- stay NULL)", "ambiguous", ambiguous)
    log.info("  TOTAL updates: %d of %d chunks", sum(len(v) for v in updates.values()), len(rows))

    if dry_run:
        log.info("DRY RUN -- no changes written.")
        conn.close()
        return

    # Apply updates in batches per sub-entity
    for se, ids in updates.items():
        batch_size = 500
        for i in range(0, len(ids), batch_size):
            batch = ids[i : i + batch_size]
            placeholders = ",".join("?" * len(batch))
            cur.execute(
                f"UPDATE knowledge_chunks SET sub_entity=? WHERE chunk_id IN ({placeholders})",
                [se] + batch,
            )
        log.info("Tagged %d chunks as %s", len(ids), se)

    conn.commit()
    conn.close()
    log.info("Backfill complete.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report without writing")
    parser.add_argument("--verbose", action="store_true", help="Log each tagged chunk")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s %(message)s", stream=sys.stdout)

    if not DB_PATH.exists():
        log.error("KB database not found at %s", DB_PATH)
        sys.exit(1)

    run(dry_run=args.dry_run, verbose=args.verbose)


if __name__ == "__main__":
    main()
