"""Knowledge Gaps digest builder.

Reads logs/knowledge-gaps.jsonl, groups by entity, renders a markdown digest
with inline space for Harrison's review answers, and writes it to the Drive
folder where Harrison reviews it.

Usage (manual, Phase 1):
    uv run python scripts/generate_knowledge_gaps_digest.py
    uv run python scripts/generate_knowledge_gaps_digest.py --since 2026-05-18
    uv run python scripts/generate_knowledge_gaps_digest.py --output-dir "G:/My Drive/.../"

The Phase 2 version of this script runs on a daily Windows Task Scheduler
trigger at e.g. 5am AZ and surfaces a one-line hook in the daily brief.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG = REPO_ROOT / "logs" / "knowledge-gaps.jsonl"
DEFAULT_OUTPUT_DIR = Path(
    "G:/My Drive/HJR-Founder-OS/_shared/projects/cora/knowledge-gaps"
)

# Entity display ordering — FNDR last because it's the catch-all
ENTITY_ORDER = ["F3E", "LEX", "OSN", "BDM", "HJRG", "FNDR"]
ENTITY_LABELS = {
    "F3E": "F3 Energy",
    "LEX": "Lexington Services",
    "OSN": "One Stop Nutrition",
    "BDM": "Big D Media",
    "HJRG": "HJR Global",
    "FNDR": "Founder / cross-portfolio",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the knowledge-gaps digest.")
    parser.add_argument(
        "--log",
        type=Path,
        default=DEFAULT_LOG,
        help=f"Path to the knowledge-gaps.jsonl file (default: {DEFAULT_LOG})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory where the digest markdown will be written.",
    )
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help=(
            "ISO date (YYYY-MM-DD) — include gaps with ts >= this date. "
            "Default: last 24 hours."
        ),
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Include all gaps from all time (overrides --since).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the digest to stdout instead of writing to a file.",
    )
    return parser.parse_args()


def load_gaps(log_path: Path) -> list[dict]:
    """Read all gap records from the JSONL log. Skip malformed lines."""
    if not log_path.exists():
        return []
    gaps: list[dict] = []
    with log_path.open("r", encoding="utf-8") as fh:
        for line_num, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                gaps.append(json.loads(raw))
            except json.JSONDecodeError as exc:
                print(f"  WARNING: malformed JSON on line {line_num}: {exc}")
    return gaps


def filter_by_date(gaps: list[dict], since: datetime | None) -> list[dict]:
    if since is None:
        return gaps
    out = []
    for g in gaps:
        ts_str = g.get("ts")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str)
        except ValueError:
            continue
        if ts >= since:
            out.append(g)
    return out


def group_by_entity(gaps: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for g in gaps:
        entity = g.get("entity", "UNKNOWN")
        grouped[entity].append(g)
    return grouped


def render_digest(grouped: dict[str, list[dict]], since: datetime | None, total: int) -> str:
    today = date.today().isoformat()
    lines: list[str] = []
    lines.append(f"# Cora Knowledge Gaps Digest — {today}")
    lines.append("")
    if since is not None:
        lines.append(f"_Range: gaps captured since {since.isoformat()}_")
    else:
        lines.append("_Range: all gaps_")
    lines.append("")
    lines.append(f"**Total gaps in this window: {total}**")
    lines.append("")

    if total == 0:
        lines.append("No knowledge gaps captured in this window. Cora answered everything from her existing context.")
        lines.append("")
        return "\n".join(lines)

    lines.append("---")
    lines.append("")
    lines.append("## How to use this digest")
    lines.append("")
    lines.append("For each gap below, you have three options:")
    lines.append("")
    lines.append("1. **Trivial / one-off** — write `SKIP` in the **Your answer** block. The gap will be marked resolved without feeding back to Cora.")
    lines.append("2. **Real gap, here's the answer** — write the answer in the **Your answer** block. This text will be appended to the entity's known-answers file when the ingestion script runs (Phase 2).")
    lines.append("3. **Routing rule, not a fact** — write `ROUTE: ask [person/system]` in the **Your answer** block. Future asks of this type will be routed accordingly.")
    lines.append("")
    lines.append("Leave the **Your answer** block empty to defer the gap to tomorrow's digest.")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Render in canonical entity order, then any unknowns at the end
    seen_entities: set[str] = set()
    for entity in ENTITY_ORDER:
        if entity in grouped:
            seen_entities.add(entity)
            _render_entity_block(lines, entity, grouped[entity])
    for entity in sorted(grouped.keys()):
        if entity not in seen_entities:
            _render_entity_block(lines, entity, grouped[entity])

    return "\n".join(lines)


def _render_entity_block(lines: list[str], entity: str, gaps: list[dict]) -> None:
    label = ENTITY_LABELS.get(entity, entity)
    lines.append(f"## {label} ({entity}) — {len(gaps)} gap(s)")
    lines.append("")
    for idx, g in enumerate(gaps, start=1):
        ts = g.get("ts", "")
        # Truncate microseconds for display
        if "." in ts:
            ts = ts.split(".", 1)[0] + ts[ts.rfind("+"):] if "+" in ts else ts.split(".", 1)[0]
        channel = g.get("channel", "?")
        user = g.get("user", "?")
        question = g.get("question", "")
        gap = g.get("gap", "")
        latency = g.get("latency_ms", "?")
        response_chars = g.get("response_chars", "?")

        lines.append(f"### {entity}-{idx}: {gap[:80]}")
        lines.append("")
        lines.append(f"- **When:** {ts}")
        lines.append(f"- **Channel:** #{channel}")
        lines.append(f"- **Asked by:** {user}")
        lines.append(f"- **Latency:** {latency}ms · **Response sent:** {response_chars} chars")
        lines.append("")
        lines.append("**Question asked:**")
        lines.append("")
        lines.append(f"> {question}")
        lines.append("")
        lines.append("**Gap Cora flagged:**")
        lines.append("")
        lines.append(f"> {gap}")
        lines.append("")
        lines.append("**Your answer:**")
        lines.append("")
        lines.append("```")
        lines.append("(leave empty to defer · write SKIP to mark trivial · write the answer to feed back to Cora · write ROUTE: ask [person] for routing rule)")
        lines.append("```")
        lines.append("")
        lines.append("---")
        lines.append("")


def main() -> int:
    args = parse_args()

    # Parse --since
    since: datetime | None
    if args.all:
        since = None
    elif args.since:
        try:
            since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
        except ValueError as exc:
            print(f"ERROR: --since must be ISO date (YYYY-MM-DD): {exc}")
            return 1
    else:
        # Default: last 24 hours
        since = datetime.now(timezone.utc) - timedelta(hours=24)

    print(f"Reading gaps from: {args.log}")
    if not args.log.exists():
        print(f"  Log file does not exist yet. Nothing to digest.")
        return 0

    all_gaps = load_gaps(args.log)
    print(f"  Found {len(all_gaps)} total gaps in log")

    in_window = filter_by_date(all_gaps, since)
    print(f"  {len(in_window)} gaps in selected window")

    grouped = group_by_entity(in_window)
    digest = render_digest(grouped, since, len(in_window))

    if args.dry_run:
        print()
        print("=== DIGEST (dry-run, not written) ===")
        print()
        print(digest)
        return 0

    args.output_dir.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    out_path = args.output_dir / f"{today}-digest.md"
    out_path.write_text(digest, encoding="utf-8")
    print(f"Wrote digest to: {out_path}")
    print(f"  {len(in_window)} gap(s) across {len(grouped)} entit(y/ies)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
