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

import anthropic
from dotenv import load_dotenv

load_dotenv()

# Claude Haiku verification gate — keeps the regex heuristic as a cheap
# pre-filter and lets Haiku decide what is actually a business decision.
_HAIKU_MODEL = "claude-haiku-4-5"
_MAX_VERIFY = 50  # cap candidates sent to Haiku per run (bounds cost/tokens)

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
    # Strong — explicit decision language (weight 3, qualifies alone)
    (re.compile(r"\bwe\s+(?:have\s+)?decided\s+to\b", re.I), 3),
    (re.compile(r"\b(?:decision|final\s+call|final\s+decision)[:\s]", re.I), 3),
    # Medium — directional / commitment language (weight 2, qualifies alone)
    (re.compile(r"\bgoing\s+forward\s*[,:]", re.I), 2),
    (re.compile(r"\bwe\s+(?:are\s+)?going\s+(?:with|to\s+use)\b", re.I), 2),
    (re.compile(r"\blocked?\s+(?:in|down|as)\b", re.I), 2),
    (re.compile(r"\bno\s+longer\s+(?:going|doing|pursuing|using)\b", re.I), 2),
    (re.compile(r"\b(?:effective|starting)\s+(?:immediately|today|now)\b", re.I), 2),
    (re.compile(r"\bpivot(?:ing|ed)?\s+(?:to|from|away)\b", re.I), 2),
    (re.compile(r"\bshipping\s+(?:today|this\s+week|now)\b", re.I), 2),
    # Weak — common conversational words; need a second signal to qualify (weight 1)
    (re.compile(r"\bwe\s+(?:will|won'?t|are\s+not\s+going\s+to)\b", re.I), 1),
    (re.compile(r"\b(?:confirmed|approved|signed\s+off)\b", re.I), 1),
    (re.compile(r"\barchive[d]?\b", re.I), 1),
    (re.compile(r"\bcancell?ed?\b", re.I), 1),
]

_MIN_SCORE = 2  # minimum total score; a single weak (weight-1) signal can no longer pass
_MIN_WORDS = 5  # backchannel floor — kills "Verify confirmed.", "And so we will."

# Strip a leading "[Speaker Name] " transcript tag so it doesn't inflate length
# or pollute the dedup fingerprint.
_SPEAKER_PREFIX = re.compile(r"^\s*\[[^\]]{1,60}\]\s*")


def _strip_speaker(text: str) -> str:
    """Remove a leading "[Speaker]" transcript prefix, if present."""
    return _SPEAKER_PREFIX.sub("", text or "").strip()


def score_sentence(text: str) -> int:
    """Return a confidence score for a sentence being a decision."""
    return sum(weight for pattern, weight in _DECISION_PATTERNS if pattern.search(text))


def extract_decision_sentences(text: str) -> list[str]:
    """Split text into sentences and return those that look like decisions."""
    # Simple sentence splitter — not perfect but good enough
    sentences = re.split(r"(?<=[.!?])\s+", text)
    results: list[str] = []
    for sent in sentences:
        sent = _strip_speaker(sent.strip())
        if len(sent) < 20 or len(sent) > 500:
            continue
        if len(sent.split()) < _MIN_WORDS:
            continue
        if score_sentence(sent) >= _MIN_SCORE:
            results.append(sent)
    return results


def _fingerprint(text: str) -> str:
    """Stable 12-char fingerprint for deduplication.

    Normalizes first (strip speaker tag, lowercase, drop punctuation, collapse
    whitespace) so near-duplicates that differ only by a comma or speaker label
    collapse to the same fingerprint.
    """
    norm = _strip_speaker(text).lower()
    norm = re.sub(r"[^a-z0-9\s]", "", norm)
    norm = re.sub(r"\s+", " ", norm).strip()
    return hashlib.sha256(norm.encode()).hexdigest()[:12]


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


_VERIFY_PROMPT = """You are filtering candidate sentences pulled from meeting \
transcripts. Keep ONLY sentences that record an actual business DECISION, \
commitment, or resolved direction (e.g. "we decided to cancel X", "going \
forward we will use Y", "the launch is locked for June 15").

REJECT: backchannel and filler ("verify confirmed", "yep, sounds good"), \
questions, hypotheticals, vague musings, status chatter, and anything where \
no concrete choice was actually made.

For each item, return whether it is a real decision and, if so, a concise \
one-sentence restatement of the decision (no speaker names, no filler).

Return ONLY a JSON array, one object per input item, in the same order:
[{{"index": 0, "is_decision": true, "summary": "..."}}, ...]

Candidate sentences:
{items}"""


def verify_decisions_with_haiku(candidates: list[dict]) -> list[dict]:
    """Precision gate: keep only candidates Haiku confirms are real decisions.

    Returns a subset of ``candidates`` (each with ``sentence`` replaced by
    Haiku's cleaned summary and the original kept under ``raw_sentence``), plus
    sets ``_haiku_evaluated=True`` on every input it actually scored.

    Fail-open: on missing key / API error / parse failure, returns the input
    unchanged (degraded = the tightened heuristic list, never silent total loss).
    """
    if not candidates:
        return []

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        log.warning("ANTHROPIC_API_KEY not set — skipping Haiku verification (fail-open)")
        return candidates

    batch = candidates[:_MAX_VERIFY]
    items_text = "\n".join(f"{i}. {c['sentence']}" for i, c in enumerate(batch))

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=_HAIKU_MODEL,
            max_tokens=2048,
            messages=[{"role": "user", "content": _VERIFY_PROMPT.format(items=items_text)}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = "\n".join(
                line for line in raw.split("\n") if not line.startswith("```")
            ).strip()
        # Haiku sometimes wraps the array in prose ("Here are the results: [...]
        # Let me know..."). Extract just the JSON array to avoid "Extra data".
        start, end = raw.find("["), raw.rfind("]")
        if start != -1 and end > start:
            raw = raw[start:end + 1]
        verdicts = json.loads(raw)
        if not isinstance(verdicts, list):
            log.warning("Haiku returned non-list verdicts — fail-open")
            return candidates
    except json.JSONDecodeError as exc:
        log.warning("Haiku verdict JSON parse failed (%s) — fail-open", exc)
        return candidates
    except Exception as exc:  # noqa: BLE001 — any API error -> degrade gracefully
        log.error("Haiku verification error (%s) — fail-open", exc)
        return candidates

    kept: list[dict] = []
    for v in verdicts:
        if not isinstance(v, dict):
            continue
        idx = v.get("index")
        if not isinstance(idx, int) or idx < 0 or idx >= len(batch):
            continue
        batch[idx]["_haiku_evaluated"] = True
        if not v.get("is_decision"):
            continue
        cand = batch[idx]
        summary = str(v.get("summary") or "").strip()
        if summary:
            cand["raw_sentence"] = cand["sentence"]
            cand["sentence"] = summary
        kept.append(cand)

    log.info("Haiku verification: %d/%d candidates confirmed as decisions",
             len(kept), len(batch))
    return kept


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

    # The Slack message contains emoji (✅ etc.); the default Windows console is
    # cp1252 and would crash on print(). Make stdout UTF-8 tolerant for --dry-run.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

    log.info("Scanning Fireflies KB chunks from last %d days...", args.days)
    candidates = scan_kb_fireflies(args.days)
    log.info("  Found %d candidate decision sentences", len(candidates))

    surfaced = load_surfaced(SURFACED_PATH)
    log.info("  %d already surfaced (will deduplicate)", len(surfaced))

    new_candidates = deduplicate(candidates, surfaced)
    log.info("  %d new candidates after deduplication", len(new_candidates))

    verified = verify_decisions_with_haiku(new_candidates)
    log.info("  %d candidate(s) confirmed by Haiku verification", len(verified))

    # Fingerprints Haiku evaluated but rejected — record so they never re-incur cost.
    verified_ids = {id(c) for c in verified}
    rejected_fps = [
        c["fingerprint"] for c in new_candidates
        if c.get("_haiku_evaluated") and id(c) not in verified_ids
    ]

    msg = build_slack_message(verified, args.days)

    if args.dry_run:
        print(msg)
        return 0

    if not verified:
        log.info("Nothing new to surface. Exiting.")
        if rejected_fps:
            save_surfaced(SURFACED_PATH, rejected_fps)
            log.info("Recorded %d Haiku-rejected fingerprint(s)", len(rejected_fps))
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
    log.info("Posted %d decisions to #%s ts=%s", len(verified), TARGET_CHANNEL, resp.get("ts"))

    # Record fingerprints so we don't resurface: shown (verified) + Haiku-rejected.
    shown_fps = [c["fingerprint"] for c in verified[:MAX_DECISIONS_PER_POST]]
    all_fps = shown_fps + rejected_fps
    save_surfaced(SURFACED_PATH, all_fps)
    log.info("Saved %d fingerprints (%d shown + %d rejected) to %s",
             len(all_fps), len(shown_fps), len(rejected_fps), SURFACED_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
