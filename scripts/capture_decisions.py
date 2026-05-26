"""Decision capture — scan Fireflies transcripts and Slack for decision signals.

Reads recent Fireflies content from the KB (already ingested nightly) and
optionally scans recent Slack messages in leadership channels. When it finds
sentences that look like decisions, it surfaces them to Harrison in
#hjrg-leadership as proposed decisions.md additions.

Harrison reacts ✅ to add to decisions.md (drafted, not auto-written),
or ❌ to discard.

Usage:
    python scripts/capture_decisions.py [--days N] [--dry-run] [--source fireflies|slack|both]

Design:
- Sources: KB chunks tagged source='fireflies' (already embedded nightly)
- Detection: lightweight regex patterns for decision language
- Output: Slack post to #hjrg-leadership listing proposed entries
- Auto-write: NOT implemented — Harrison confirms before any file write
- Deduplication: tracks surfaced decision fingerprints in data/surfaced-decisions.jsonl

Schedule: daily at 7am AZ alongside the morning brief.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

REPO_ROOT = Path(__file__).resolve().parent.parent
KB_DB_PATH = REPO_ROOT / "data" / "cora_kb.db"
SURFACED_PATH = REPO_ROOT / "data" / "surfaced-decisions.jsonl"
TARGET_CHANNEL = "hjrg-leadership"
DEFAULT_DAYS = 3
MAX_DECISIONS_PER_POST = 10  # cap to avoid overwhelming the channel

# ── Decision-language patterns ────────────────────────────────────────────────
# Ordered by confidence. Weights map to how confident we are it's a real decision.
_DECISION_PATTERNS: list[tuple[re.Pattern, int]] = [
    (re.compile(r"\bwe\s+(?:have\s+)?decided\s+to\b", re.I), 3),
    (re.compile(r"\b(?:decision|final\s+call|final\s+decision)[:\s]", re.I), 3),
    (re.compile(r"\bgoing\s+forward\s*[,:]", re.I), 2),
    (re.compile(r"\bwe\s+(?:are\s+)?going\s+(?:with|to\s+use)\b", re.I), 2),
    (re.compile(r"\blocked\s+(?:in|down)\b", re.I), 2),
    (re.compile(r"\bwe\s+(?:will|won'?t|are\s+not\s+going\s+to)\b", re.I), 2),
    (re.compile(r"\b(?:confirmed|approved|signed\s+off)\b", re.I), 2),
    (re.compile(r"\bno\s+longer\s+(?:going|doing|pursuing|using)\b", re.I), 2),
    (re.compile(r"\barchive[d]?\b", re.I), 1),
    (re.compile(r"\bcancell?ed?\b", re.I), 1),
    (re.compile(r"\b(?:effective|starting)\s+(?:immediately|today|now)\b", re.I), 2),
    (re.compile(r"\bpivot(?:ing|ed)?\s+(?:to|from|away)\b", re.I), 2),
    (re.compile(r"\bshipping\s+(?:today|this\s+week|now)\b", re.I), 2),
    (re.compile(r"\blocke[d]?\s+(?:in|down|as)\b", re.I), 2),
]

_MIN_SCORE = 2  # minimum total score for a sentence to be considered a decision


def score_sentence(text: str) -> int:
    """Return a confidence score for a sentence being a decision."""
    return sum(weight for pattern, weight in _DECISION_PATTERNS if pattern.search(text))


def extract_decision_sentences(text: str) -> list[str]:
    """Split text into sentences and return those that look like decisions."""
    # Simple sentence splitter — not perfect but good enough
    sentences = re.split(r"(?<=[.!?])\s+", text)
    results: list[str] = []
    for sent in sentences:
        sent = sent.strip()
        if len(sent) < 20 or len(sent) > 500:
            continue
        if score_sentence(sent) >= _MIN_SCORE:
            results.append(sent)
    return results


def _fingerprint(text: str) -> str:
    """Stable 12-char fingerprint for deduplication."""
    return hashlib.sha256(text.lower().strip().encode()).hexdigest()[:12]


def load_surfaced(path: Path) -> set[str]:
    """Load fingerprints of already-surfaced decisions."""
    if not path.exists():
        return set()
    seen: set[str] = set()
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
                fp = rec.get("fingerprint")
                if fp:
                    seen.add(fp)
            except json.JSONDecodeError:
                pass
    return seen


def save_surfaced(path: Path, fingerprints: list[str]) -> None:
    """Append new fingerprints to the surfaced log."""
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    with path.open("a", encoding="utf-8") as fh:
        for fp in fingerprints:
            fh.write(json.dumps({"fingerprint": fp, "surfaced_at": now}) + "\n")


def scan_kb_fireflies(days: int) -> list[dict]:
    """Scan recent Fireflies KB chunks for decision language.

    Returns list of dicts with keys: entity, title, sentence, source_ts.
    """
    if not KB_DB_PATH.exists():
        log.warning("KB not found at %s", KB_DB_PATH)
        return []

    cutoff = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())

    conn = sqlite3.connect(str(KB_DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")

    try:
        rows = conn.execute(
            """
            SELECT entity, title, content, date_created, deep_link
            FROM knowledge_chunks
            WHERE source = 'fireflies'
              AND (date_created IS NULL OR date_created >= ?)
            ORDER BY date_created DESC
            LIMIT 2000
            """,
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()

    found: list[dict] = []
    for entity, title, content, date_created, deep_link in rows:
        if not content:
            continue
        for sentence in extract_decision_sentences(content):
            found.append({
                "entity": entity or "FNDR",
                "title": title or "Fireflies transcript",
                "sentence": sentence,
                "date_created": date_created,
                "deep_link": deep_link or "",
                "fingerprint": _fingerprint(sentence),
            })

    return found


def deduplicate(candidates: list[dict], surfaced: set[str]) -> list[dict]:
    """Remove already-surfaced decisions and internal duplicates."""
    seen_fp: set[str] = set(surfaced)
    out: list[dict] = []
    for c in candidates:
        fp = c["fingerprint"]
        if fp not in seen_fp:
            seen_fp.add(fp)
            out.append(c)
    return out


def group_by_entity(candidates: list[dict]) -> dict[str, list[dict]]:
    from collections import defaultdict
    grouped: dict[str, list[dict]] = defaultdict(list)
    for c in candidates:
        grouped[c["entity"]].append(c)
    return grouped


def build_slack_message(candidates: list[dict], days: int) -> str:
    """Build Slack mrkdwn for the proposed decisions post."""
    if not candidates:
        return (
            f":white_check_mark: *Cora Decision Capture — last {days} days*\n"
            "No new decision signals detected in recent Fireflies transcripts."
        )

    capped = candidates[:MAX_DECISIONS_PER_POST]
    overflow = len(candidates) - len(capped)
    grouped = group_by_entity(capped)

    lines: list[str] = [
        f":memo: *Cora Decision Capture — last {days} days*",
        f"*{len(candidates)} proposed decision(s)* from Fireflies transcripts.",
        "React ✅ on individual entries to draft into `decisions.md`, ❌ to discard.\n",
    ]

    entity_order = ["FNDR", "F3E", "LEX", "OSN", "BDM", "HJRG"]
    shown: set[str] = set()
    for entity in entity_order + sorted(grouped.keys()):
        if entity in shown or entity not in grouped:
            continue
        shown.add(entity)
        ent_candidates = grouped[entity]
        lines.append(f"*{entity}*")
        for c in ent_candidates:
            title = c["title"][:50] if c["title"] else "transcript"
            deep = f" — <{c['deep_link']}|view>" if c.get("deep_link") else ""
            lines.append(f"  • _{title}{deep}_")
            lines.append(f"    > {c['sentence'][:200]}")
        lines.append("")

    if overflow > 0:
        lines.append(f"_…and {overflow} more not shown (increase MAX_DECISIONS_PER_POST or rerun)_")

    lines.append(
        "\n:bulb: To add to decisions.md: copy the sentence above, open "
        "`memory/decisions.md`, and paste under the relevant date."
    )
    return "\n".join(lines)


def find_channel_id(slack_client, channel_name: str) -> str | None:
    cursor = None
    while True:
        kwargs: dict = {"types": "public_channel,private_channel", "limit": 200}
        if cursor:
            kwargs["cursor"] = cursor
        resp = slack_client.conversations_list(**kwargs)
        for ch in resp.get("channels", []):
            if ch.get("name") == channel_name:
                return ch["id"]
        meta = resp.get("response_metadata", {})
        cursor = meta.get("next_cursor")
        if not cursor:
            break
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture decisions from Fireflies + Slack.")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    log.info("Scanning Fireflies KB chunks from last %d days...", args.days)
    candidates = scan_kb_fireflies(args.days)
    log.info("  Found %d candidate decision sentences", len(candidates))

    surfaced = load_surfaced(SURFACED_PATH)
    log.info("  %d already surfaced (will deduplicate)", len(surfaced))

    new_candidates = deduplicate(candidates, surfaced)
    log.info("  %d new candidates after deduplication", len(new_candidates))

    msg = build_slack_message(new_candidates, args.days)

    if args.dry_run:
        print(msg)
        return 0

    if not new_candidates:
        log.info("Nothing new to surface. Exiting.")
        return 0

    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        print("ERROR: SLACK_BOT_TOKEN not set", file=sys.stderr)
        return 1

    from slack_sdk import WebClient
    client = WebClient(token=token)

    channel_id = find_channel_id(client, TARGET_CHANNEL)
    if not channel_id:
        print(f"ERROR: Could not find #{TARGET_CHANNEL}", file=sys.stderr)
        return 1

    resp = client.chat_postMessage(
        channel=channel_id,
        text=msg,
        unfurl_links=False,
        unfurl_media=False,
    )
    log.info("Posted %d decisions to #%s ts=%s", len(new_candidates), TARGET_CHANNEL, resp.get("ts"))

    # Record fingerprints so we don't surface the same decisions tomorrow
    new_fps = [c["fingerprint"] for c in new_candidates[:MAX_DECISIONS_PER_POST]]
    save_surfaced(SURFACED_PATH, new_fps)
    log.info("Saved %d fingerprints to %s", len(new_fps), SURFACED_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
