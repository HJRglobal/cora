"""Sub-entity KB migration script.

Updates the `sub_entity` column on existing knowledge_chunks rows that have
entity='LEX' and sub_entity IS NULL, by applying keyword detection rules to
the chunk content.

Why this matters:
  The KB search filter for LEX sub-entity channels (LEX-LLC, LEX-LTS, etc.)
  only returns chunks explicitly tagged with the matching sub_entity value.
  Chunks with sub_entity=NULL are treated as GM-level (excluded from sub-entity
  channel results by the strict sibling guard). This means older Fireflies/Asana
  content that was ingested before sub-entity tagging was added returns no results
  in LLC/LTS/LBHS/LLA channels — hurting retrieval quality.

  This script retroactively tags those chunks so the RAG pipeline can serve them.

Detection rules:
  Keywords are scored per sub-entity. The sub-entity with the highest score wins.
  If score is 0 (no keywords matched), the chunk stays NULL (genuinely GM-level).
  If two sub-entities tie, the chunk stays NULL (ambiguous — safer to leave it).

Run from repo root:
    python scripts/migrate_sub_entity_tags.py [--dry-run] [--verbose]

Safe to re-run: already-tagged chunks (sub_entity IS NOT NULL) are skipped.
"""

import argparse
import re
import sqlite3
import sys
from pathlib import Path

# ── Keyword detection rules ───────────────────────────────────────────────────
# Maps sub_entity code → list of (pattern, weight) tuples.
# Weight 2 = strong signal (proper name, acronym), Weight 1 = moderate signal.

SUB_ENTITY_RULES: dict[str, list[tuple[str, int]]] = {
    "LEX-LLC": [
        (r"\bLexington\s+LLC\b",              2),
        (r"\bShaun\s+Hawkins\b",              2),
        (r"\bShaun@lexington\b",              2),
        (r"\bHCBS\b",                         2),
        (r"\bDDD\b",                          1),
        (r"\bAHCCCS\b",                       1),
        (r"\bJen\s+Mortensen\b",              2),
        (r"\bAaron\s+Ferrucci\b",             2),
        (r"\bJeff\s+Montgomery\b",            1),
        (r"\bSpokeChoice\b",                  2),
        (r"\bDTA\b",                          1),
        (r"\bLLC\s+operations?\b",            1),
        (r"\bgroup\s+home[s]?\b",             1),
        (r"\bstaffing\s+schedule[s]?\b",      1),
        (r"\bsupported\s+living\b",           1),
    ],
    "LEX-LTS": [
        (r"\bLexington\s+Therapies\b",        2),
        (r"\bLTS\b",                          2),
        (r"\bJustin\s+Gilmore\b",             2),
        (r"\bjustin\.gilmore@",               2),
        (r"\bJG[\s,]+LLC\b",                  2),
        (r"\bNew\s+Age\s+Cash\s+Flow\b",      2),
        (r"\bProvider\s+Type\s+15\b",         2),
        (r"\btherapy\s+revalidation\b",       2),
        (r"\bDDD\s+[Tt]herapy\b",             2),
        (r"\bAZ\s+DDD\s+[Tt]herapy\b",       2),
        (r"\bspeech\s+therap[y|ist]\b",       1),
        (r"\boccupational\s+therap[y|ist]\b", 1),
        (r"\bphysical\s+therap[y|ist]\b",     1),
        (r"\bABA\s+therap[y|ist]\b",          1),
        (r"\btherapeutic\s+services?\b",      1),
    ],
    "LEX-LBHS": [
        (r"\bLBHS\b",                         2),
        (r"\bLexington\s+Behavioral\b",       2),
        (r"\bJared\s+Harker\b",               2),
        (r"\bHMLA\b",                         2),
        (r"\bApplied\s+Behavior\s+Analysis\b",2),
        (r"\bABA\b",                          1),
        (r"\bCOPA\b",                         2),
        (r"\bBHRF\b",                         2),
        (r"\bUnitedHealthcare.*LBHS\b",       2),
        (r"\bbehavior\s+support\s+plan[s]?\b",1),
        (r"\bbehavior\s+intervention\b",      1),
        (r"\bintervention\s+plan[s]?\b",      1),
        (r"\b42\s+CFR\s+Part\s+2\b",         2),
    ],
    "LEX-LLA": [
        (r"\bLex\s+Life\s+Academy\b",         2),
        (r"\bLLA\b",                          2),
        (r"\bSandy\s+Patel\b",                2),
        (r"\bSBP\s+Inc\b",                    2),
        (r"\bBryan\s+Patel\b",                2),
        (r"\bMaryvale\b",                     1),
        (r"\bAchieve\s*[-–]\s*Maryvale\b",   2),
        (r"\bLLA\s+Show\s+Low\b",             2),
        (r"\bEllsworth\b",                    1),
        (r"\btuition\s+cycle[s]?\b",          2),
        (r"\bschool\s+program[s]?\b",         1),
        (r"\bday\s+program[s]?\b",            1),
        (r"\bcommunity[\s\-]integration\b",   1),
        (r"\bIEP[s]?\b",                      1),
    ],
}

# Pre-compile patterns
_COMPILED_RULES: dict[str, list[tuple[re.Pattern, int]]] = {
    sub_entity: [(re.compile(pat, re.IGNORECASE), weight) for pat, weight in rules]
    for sub_entity, rules in SUB_ENTITY_RULES.items()
}


def score_chunk(content: str) -> dict[str, int]:
    """Return a score per sub-entity for the given chunk content."""
    scores: dict[str, int] = {}
    for sub_entity, rules in _COMPILED_RULES.items():
        total = sum(weight for pattern, weight in rules if pattern.search(content))
        if total > 0:
            scores[sub_entity] = total
    return scores


def detect_sub_entity(content: str) -> str | None:
    """Return the best-matching sub-entity code, or None if ambiguous/no match."""
    scores = score_chunk(content)
    if not scores:
        return None

    max_score = max(scores.values())
    winners = [k for k, v in scores.items() if v == max_score]

    if len(winners) == 1:
        return winners[0]

    # Tie — ambiguous, leave as GM-level (None)
    return None


def run_migration(db_path: Path, dry_run: bool = False, verbose: bool = False) -> None:
    """Main migration logic."""
    if not db_path.exists():
        print(f"ERROR: KB database not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")

    # Fetch all LEX chunks with NULL sub_entity
    rows = conn.execute(
        """
        SELECT chunk_id, content, title
        FROM knowledge_chunks
        WHERE entity = 'LEX'
          AND sub_entity IS NULL
        """
    ).fetchall()

    print(f"Found {len(rows)} LEX chunks with NULL sub_entity")

    stats: dict[str, int] = {"no_match": 0, "ambiguous": 0}
    updates: list[tuple[str, str]] = []  # (sub_entity, chunk_id)

    for chunk_id, content, title in rows:
        detected = detect_sub_entity(content or "")

        if detected is None:
            scores = score_chunk(content or "")
            if not scores:
                stats["no_match"] = stats.get("no_match", 0) + 1
                if verbose:
                    print(f"  NO_MATCH  [{chunk_id[:8]}] {(title or '')[:60]}")
            else:
                stats["ambiguous"] = stats.get("ambiguous", 0) + 1
                if verbose:
                    print(f"  AMBIGUOUS [{chunk_id[:8]}] {(title or '')[:60]} scores={scores}")
        else:
            stats[detected] = stats.get(detected, 0) + 1
            updates.append((detected, chunk_id))
            if verbose:
                scores = score_chunk(content or "")
                print(f"  {detected:<12} [{chunk_id[:8]}] {(title or '')[:60]} score={scores.get(detected, 0)}")

    print(f"\nDetection summary:")
    for key in sorted(stats):
        print(f"  {key:<20} {stats[key]}")
    print(f"\nTotal to update: {len(updates)}")

    if dry_run:
        print("\nDRY RUN — no changes written.")
        conn.close()
        return

    if not updates:
        print("Nothing to update.")
        conn.close()
        return

    print(f"\nWriting {len(updates)} updates...")
    conn.executemany(
        "UPDATE knowledge_chunks SET sub_entity = ? WHERE chunk_id = ?",
        updates,
    )
    conn.commit()
    print("Done. Run `incremental_sync_static.py` to re-embed if needed.")
    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate sub_entity tags on LEX KB chunks")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(__file__).parent.parent / "data" / "cora_kb.db",
        help="Path to cora_kb.db (default: data/cora_kb.db)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be updated without writing",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print per-chunk detection results",
    )
    args = parser.parse_args()
    run_migration(args.db, dry_run=args.dry_run, verbose=args.verbose)


if __name__ == "__main__":
    main()
