"""Ingest answered gaps from a knowledge-gaps digest into design/known-answers/.

Reads a YYYY-MM-DD-digest.md produced by generate_knowledge_gaps_digest.py,
parses Harrison's fill-in answers, and writes:
  - Factual answers to design/known-answers/{entity}.md under ## Known facts
  - Routing rules to design/known-answers/{entity}.md under ## Routing rules
  - Resolved records to design/known-answers/.resolved-gaps.jsonl

Usage:
    uv run python scripts/ingest_digest_answers.py
    uv run python scripts/ingest_digest_answers.py --digest path/to/digest.md
    uv run python scripts/ingest_digest_answers.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
KNOWN_ANSWERS_DIR = REPO_ROOT / "design" / "known-answers"
RESOLVED_PATH = KNOWN_ANSWERS_DIR / ".resolved-gaps.jsonl"
DEFAULT_OUTPUT_DIR = Path(
    "G:/My Drive/HJR-Founder-OS/_shared/projects/cora/knowledge-gaps"
)

# Canonical entity -> known-answers filename, shared with gap_autofill (the
# primary, automated flow) and context_loader so the maps can't drift (WS17-B
# item 7). This legacy manual-digest path is DEPRECATED — see the deprecation
# notice in main(); the gap_autofill loop is the supported owner of these files.
sys.path.insert(0, str(REPO_ROOT / "src"))
from cora.known_answers_map import ENTITY_FILES  # noqa: E402

ENTITY_FULL_NAMES: dict[str, str] = {
    "fndr": "Founder / Cross-portfolio",
    "f3e":  "F3 Energy",
    "lex":  "Lexington Services",
    "osn":  "One Stop Nutrition",
    "bdm":  "Big D Media",
}

ANSWER_PLACEHOLDER_PREFIX = "(leave empty to defer"

ENTITY_SECTION_RE = re.compile(
    r"^## .+ \(([A-Z]+)\) -- \d+ gap\(s\)",
    re.MULTILINE,
)
QUESTION_RE = re.compile(r"\*\*Question asked:\*\*\n\n> (.+?)(?=\n\n)", re.DOTALL)
GAP_DESC_RE = re.compile(r"\*\*Gap Cora flagged:\*\*\n\n> (.+?)(?=\n\n)", re.DOTALL)
ANSWER_RE = re.compile(r"\*\*Your answer:\*\*\n\n```\n(.*?)\n```", re.DOTALL)
ENTITY_OVERRIDE_RE = re.compile(r"^\[ENTITY:\s*([A-Z0-9]+)\]\s*", re.IGNORECASE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ingest digest answers into known-answers files.")
    parser.add_argument(
        "--digest",
        type=Path,
        default=None,
        help="Path to digest markdown file (default: most recent in Drive folder)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and print actions without writing anything.",
    )
    parser.add_argument(
        "--resolved-path",
        type=Path,
        default=RESOLVED_PATH,
        help="Path to .resolved-gaps.jsonl",
    )
    parser.add_argument(
        "--known-answers-dir",
        type=Path,
        default=KNOWN_ANSWERS_DIR,
        help="Directory containing the known-answers .md files.",
    )
    return parser.parse_args()


def find_latest_digest(output_dir: Path) -> Path | None:
    if not output_dir.exists():
        return None
    digests = sorted(output_dir.glob("*-digest.md"), reverse=True)
    return digests[0] if digests else None


def initialize_known_answers_file(path: Path, entity_label: str) -> None:
    """Create a blank known-answers file with standard section headers."""
    path.write_text(
        f"# {entity_label} -- Known Answers\n\n"
        "_Compiled from Cora knowledge-gap reviews. Read alongside CLAUDE.md and founder CLAUDE.md."
        " Append-only -- manually consolidate if it gets noisy._\n\n"
        "## Routing rules\n\n"
        "(empty - rules added as Harrison flags ROUTE: entries in digests)\n\n"
        "## Known facts\n\n"
        "(empty - facts added as Harrison fills answer blocks in digests)\n",
        encoding="utf-8",
    )


def load_and_parse_digest(digest_path: Path) -> list[dict]:
    """Parse a digest markdown into a list of gap entry dicts."""
    content = digest_path.read_text(encoding="utf-8")
    entries: list[dict] = []

    # Split document into entity sections
    section_matches = list(ENTITY_SECTION_RE.finditer(content))
    if not section_matches:
        return []

    section_ranges = []
    for i, m in enumerate(section_matches):
        end = section_matches[i + 1].start() if i + 1 < len(section_matches) else len(content)
        section_ranges.append((m.group(1), content[m.start():end]))

    for entity, section_text in section_ranges:
        # Split section by GAP_ID markers
        parts = re.split(r"<!-- GAP_ID: ", section_text)
        for part in parts[1:]:  # parts[0] is the section header
            try:
                gap_id_end = part.index(" -->")
            except ValueError:
                continue
            gap_id = part[:gap_id_end].strip()
            block = part[gap_id_end + 4:]

            question_m = QUESTION_RE.search(block)
            gap_m = GAP_DESC_RE.search(block)
            answer_m = ANSWER_RE.search(block)

            entries.append({
                "gap_id": gap_id,
                "entity": entity,
                "question": question_m.group(1).strip() if question_m else "",
                "gap_desc": gap_m.group(1).strip() if gap_m else "",
                "your_answer": answer_m.group(1).strip() if answer_m else "",
            })

    return entries


def determine_action(your_answer: str) -> tuple[str, str, str | None]:
    """Return (action, clean_answer, entity_override).

    action is one of: "defer", "skip", "route", "answer".
    entity_override is set when the answer starts with [ENTITY: X].
    """
    stripped = your_answer.strip()

    # Check for entity override prefix
    override_m = ENTITY_OVERRIDE_RE.match(stripped)
    entity_override: str | None = None
    if override_m:
        entity_override = override_m.group(1).upper()
        stripped = stripped[override_m.end():].strip()

    # Empty or placeholder
    if not stripped or stripped.startswith(ANSWER_PLACEHOLDER_PREFIX):
        return ("defer", "", entity_override)

    upper = stripped.upper()

    if upper.startswith("SKIP"):
        return ("skip", stripped, entity_override)

    if upper.startswith("ROUTE:"):
        return ("route", stripped[6:].strip(), entity_override)

    return ("answer", stripped, entity_override)


def append_to_section(file_path: Path, section_header: str, entry_lines: list[str]) -> None:
    """Append entry_lines under section_header, inserting before the next ## section."""
    content = file_path.read_text(encoding="utf-8")
    lines = content.rstrip("\n").split("\n")

    insert_at = len(lines)
    in_section = False
    for i, line in enumerate(lines):
        if line == section_header:
            in_section = True
            continue
        if in_section and line.startswith("## "):
            insert_at = i
            break

    lines = lines[:insert_at] + [""] + entry_lines + lines[insert_at:]
    file_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()

    print(
        "DEPRECATED (WS17-B): superseded by the automated gap_autofill loop, which "
        "owns design/known-answers/*.md. Entity map is now imported from "
        "cora.known_answers_map (no longer a local copy). See "
        "design/knowledge-pipeline.md.\n"
    )

    # Resolve digest path
    digest_path = args.digest
    if digest_path is None:
        digest_path = find_latest_digest(DEFAULT_OUTPUT_DIR)
    if digest_path is None or not digest_path.exists():
        print("ERROR: No digest file found. Pass --digest PATH or check the Drive folder.")
        return 1

    print(f"Reading digest: {digest_path}")
    entries = load_and_parse_digest(digest_path)
    print(f"  Found {len(entries)} gap entries")

    if not entries:
        print("  Nothing to ingest.")
        return 0

    # Ensure known-answers dir exists and initialize files
    ka_dir = args.known_answers_dir
    if not args.dry_run:
        ka_dir.mkdir(parents=True, exist_ok=True)
        for filename, label in ENTITY_FULL_NAMES.items():
            fpath = ka_dir / f"{filename}.md"
            if not fpath.exists():
                initialize_known_answers_file(fpath, label)
                print(f"  Initialized {fpath.name}")

    today = date.today().isoformat()
    n_answers = n_routes = n_skipped = n_deferred = 0
    resolved_records: list[dict] = []

    for entry in entries:
        action, clean_answer, entity_override = determine_action(entry["your_answer"])
        target_entity = entity_override if entity_override else entry["entity"]
        target_filename = ENTITY_FILES.get(target_entity, "fndr.md")
        target_file = ka_dir / target_filename
        gap_short = entry["gap_desc"][:80]

        if action == "defer":
            n_deferred += 1
            if args.dry_run:
                print(f"  [DEFER]  {entry['gap_id'][:30]}... -- will resurface in next digest")
            continue

        if action == "skip":
            n_skipped += 1
            if args.dry_run:
                print(f"  [SKIP]   {gap_short}")
            else:
                resolved_records.append({
                    "id": entry["gap_id"],
                    "action": "skip",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "target_entity": target_entity,
                    "captured_entity": entry["entity"],
                })
            continue

        if action == "route":
            n_routes += 1
            entry_lines = [f"**[{today}] {gap_short}:** {clean_answer}", ""]
            if args.dry_run:
                print(f"  [ROUTE]  {target_filename} << {entry_lines[0]}")
            else:
                append_to_section(target_file, "## Routing rules", entry_lines)
                resolved_records.append({
                    "id": entry["gap_id"],
                    "action": "route",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "target_entity": target_entity,
                    "captured_entity": entry["entity"],
                })
            continue

        if action == "answer":
            n_answers += 1
            entry_lines = [
                f"**[{today}] {gap_short}**",
                f"Q: {entry['question']}",
                f"A: {clean_answer}",
                "",
            ]
            if args.dry_run:
                print(f"  [ANSWER] {target_filename} << {entry_lines[0]}")
            else:
                append_to_section(target_file, "## Known facts", entry_lines)
                resolved_records.append({
                    "id": entry["gap_id"],
                    "action": "answer",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "target_entity": target_entity,
                    "captured_entity": entry["entity"],
                })
            continue

    # Append resolved records
    if not args.dry_run and resolved_records:
        args.resolved_path.parent.mkdir(parents=True, exist_ok=True)
        with args.resolved_path.open("a", encoding="utf-8") as fh:
            for rec in resolved_records:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"  Wrote {len(resolved_records)} resolved record(s) to {args.resolved_path.name}")

    print(
        f"Ingested {n_answers} answers, {n_routes} routing rules, "
        f"{n_skipped} skipped, {n_deferred} deferred."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
