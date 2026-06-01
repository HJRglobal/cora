#!/usr/bin/env python3
"""Proactive gap surfacing — daily per-entity knowledge questions.

After the nightly KB sync (and reconciliation at 5:30am), this script runs
at 6:00am AZ and does something no other Cora system does: it proactively
reaches out to each entity's leadership channel with 1-2 targeted questions
based on what Cora noticed but doesn't have good context on.

This flips Cora from reactive (only answering questions) to active
(identifying her own blind spots and asking the team to fill them).

How it works:
  1. For each active entity with a leadership channel, fetch the last 24h of
     KB chunks (Slack + Fireflies + Gmail) for that entity.
  2. Feed those chunks to Claude Haiku with a prompt asking it to identify
     1-2 specific topics that appear active but where KB coverage looks thin.
  3. Haiku returns 1-2 natural-sounding questions for the team.
  4. Cora posts the questions in the entity leadership channel with brief
     framing ("I noticed X this week — can you help me understand Y better?").

PHI guardrail: LEX entity chunks are filtered through phi_guard before
any content reaches the Haiku call. LEX questions are generic operational
(never client-specific).
Visibility CPA exclusion: CPA names never appear in questions or evidence.

Scheduled as: cowork-cora-proactive-gaps  Daily 6:00am AZ
Run manually:  python scripts/run_proactive_gaps.py [--dry-run] [--entity F3E]
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

_KB_DB = _REPO_ROOT / "data" / "cora_kb.db"
_LOOKBACK_SECONDS = 25 * 3600  # 25h to catch late-night syncs
_MAX_CHUNKS_PER_ENTITY = 20    # cap context fed to Haiku
_MAX_CHUNK_CHARS = 300         # truncate each chunk to keep prompt bounded
_DEDUP_PATH = _REPO_ROOT / "data" / "proactive-gaps-sent.jsonl"

# Active entities: entity code → leadership channel name
_ENTITY_CHANNELS = {
    "F3E":   "f3e-leadership",
    "OSN":   "osn-leadership",
    "HJRG":  "hjrg-leadership",
    "BDM":   "bdm-leadership",
    "HJRP":  "hjrp-leadership",
    "LEX":   "lex-leadership",
}
# UFL is monitor-only (paused) — excluded.

_ENTITY_LABELS = {
    "F3E":  "F3 Energy",
    "OSN":  "One Stop Nutrition",
    "HJRG": "HJR Global",
    "BDM":  "Big D Media",
    "HJRP": "HJR Properties",
    "LEX":  "Lexington Services",
}

# Haiku prompt template
_HAIKU_PROMPT = """\
You are a knowledge assistant reviewing recent internal communications for {entity_label}.

Below are excerpts from Slack messages, meeting transcripts, and emails from the last 24 hours:

---
{chunks_text}
---

Your task: identify 1-2 specific topics, projects, or situations that appear to be actively \
discussed but where you likely need more context to answer questions accurately.

For each topic, write a single natural-sounding question you would ask the team in Slack — \
as if you're a helpful colleague who noticed something and wants to understand it better.

Rules:
- Be specific. Reference actual names, projects, or situations from the excerpts above.
- Do NOT ask about people's personal lives, health, or anything unrelated to work.
- Do NOT ask questions you could look up yourself (dates, basic facts).
- Do NOT include PHI, client names, diagnoses, or care-plan details (LEX only).
- Do NOT mention Visibility CPA team members by name.
- Return ONLY a JSON array of strings — each string is one question. Max 2 items.
- If there is nothing genuinely unclear or thin in the excerpts, return an empty array [].
- Example format: ["What's the current status of the BCB production run for Pure?", \
"Has the OSN DNA Sports AR been resolved or is it still open?"]

Return ONLY the JSON array. No preamble.
"""


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(
            open(sys.stdout.fileno(), "w", encoding="utf-8", errors="replace", closefd=False)
        )],
    )


def _load_sent_today() -> set[str]:
    """Load question fingerprints already sent today (dedup)."""
    if not _DEDUP_PATH.exists():
        return set()
    today = datetime.now().strftime("%Y-%m-%d")
    sent: set[str] = set()
    with _DEDUP_PATH.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                import json
                rec = json.loads(line)
                if rec.get("date") == today:
                    sent.add(rec.get("fingerprint", ""))
            except Exception:
                pass
    return sent


def _record_sent(entity: str, question: str) -> None:
    import json
    import hashlib
    today = datetime.now().strftime("%Y-%m-%d")
    fp = hashlib.sha1(f"{today}:{entity}:{question}".encode()).hexdigest()[:16]
    _DEDUP_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _DEDUP_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"date": today, "entity": entity, "fingerprint": fp}) + "\n")


def _fingerprint(entity: str, question: str) -> str:
    import hashlib
    today = datetime.now().strftime("%Y-%m-%d")
    return hashlib.sha1(f"{today}:{entity}:{question}".encode()).hexdigest()[:16]


def _fetch_recent_chunks(entity: str, lookback_seconds: float) -> list[str]:
    """Return recent content excerpts for this entity from KB."""
    if not _KB_DB.exists():
        return []
    cutoff = int(time.time() - lookback_seconds)
    conn = sqlite3.connect(str(_KB_DB))
    try:
        rows = conn.execute(
            """
            SELECT content FROM knowledge_chunks
            WHERE entity = ? AND source IN ('slack','fireflies','gmail')
              AND ingested_at >= ?
            ORDER BY ingested_at DESC
            LIMIT ?
            """,
            (entity, cutoff, _MAX_CHUNKS_PER_ENTITY),
        ).fetchall()
    finally:
        conn.close()

    excerpts = []
    for (content,) in rows:
        if content:
            excerpts.append(content[:_MAX_CHUNK_CHARS].replace("\n", " ").strip())
    return excerpts


def _phi_filter_chunks(chunks: list[str]) -> list[str]:
    """Strip chunks with PHI language for LEX entity."""
    try:
        from cora.phi_guard import _PHI_PATTERNS as _PHI_RE
        return [c for c in chunks if not _PHI_RE.search(c)]
    except Exception:
        return chunks


def _visibility_filter_chunks(chunks: list[str]) -> list[str]:
    """Remove chunks mentioning Visibility CPA."""
    try:
        from cora.phi_guard import is_visibility_cpa_mention
        return [c for c in chunks if not is_visibility_cpa_mention(c)]
    except Exception:
        return chunks


def _ask_haiku(entity: str, chunks: list[str], api_key: str) -> list[str]:
    """Call Claude Haiku to generate proactive questions. Returns list of question strings."""
    if not chunks:
        return []
    try:
        import anthropic
        import json

        chunks_text = "\n\n".join(f"- {c}" for c in chunks)
        prompt = _HAIKU_PROMPT.format(
            entity_label=_ENTITY_LABELS.get(entity, entity),
            chunks_text=chunks_text,
        )
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = (msg.content[0].text or "").strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        questions = json.loads(raw)
        if isinstance(questions, list):
            return [str(q).strip() for q in questions if isinstance(q, str) and q.strip()]
        return []
    except Exception as exc:
        logging.getLogger("proactive-gaps").warning(
            "Haiku call failed for %s: %s", entity, exc
        )
        return []


def _post_questions(
    entity: str,
    channel: str,
    questions: list[str],
    token: str,
    sent_fps: set[str],
    dry_run: bool,
) -> int:
    """Post unseen questions to the entity channel. Returns count posted."""
    log = logging.getLogger("proactive-gaps")
    posted = 0
    for q in questions:
        fp = _fingerprint(entity, q)
        if fp in sent_fps:
            log.info("skip duplicate question for %s: %s", entity, q[:60])
            continue

        message = f":thinking_face: *Cora is curious:* {q}"

        if dry_run:
            print(f"[DRY RUN] #{channel}: {message}")
            posted += 1
            continue

        try:
            from slack_sdk import WebClient
            WebClient(token=token).chat_postMessage(
                channel=channel,
                text=message,
                unfurl_links=False,
                unfurl_media=False,
            )
            _record_sent(entity, q)
            sent_fps.add(fp)
            log.info("posted proactive question to #%s: %s", channel, q[:80])
            posted += 1
        except Exception as exc:
            log.warning("failed to post to #%s: %s", channel, exc)
        time.sleep(0.3)
    return posted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--entity", type=str, default=None,
                        help="Run for one entity only (e.g. F3E)")
    parser.add_argument("--lookback-hours", type=float, default=25.0)
    args = parser.parse_args()

    _setup_logging()
    log = logging.getLogger("proactive-gaps")
    log.info("=" * 60)
    log.info("Proactive gaps run starting (dry_run=%s)", args.dry_run)

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        log.error("ANTHROPIC_API_KEY not set — cannot run proactive gaps")
        return 1

    slack_token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not slack_token and not args.dry_run:
        log.error("SLACK_BOT_TOKEN not set")
        return 1

    sent_fps = _load_sent_today()
    lookback = args.lookback_hours * 3600

    entities = {args.entity: _ENTITY_CHANNELS[args.entity]} \
        if args.entity and args.entity in _ENTITY_CHANNELS \
        else _ENTITY_CHANNELS

    total_posted = 0
    for entity, channel in entities.items():
        log.info("Processing entity=%s channel=#%s", entity, channel)

        chunks = _fetch_recent_chunks(entity, lookback)
        if not chunks:
            log.info("No recent chunks for %s — skipping", entity)
            continue

        # Apply guardrails
        if entity.startswith("LEX"):
            chunks = _phi_filter_chunks(chunks)
        chunks = _visibility_filter_chunks(chunks)

        if not chunks:
            log.info("All chunks filtered for %s — skipping", entity)
            continue

        log.info("Asking Haiku for questions on %d chunks (entity=%s)", len(chunks), entity)
        questions = _ask_haiku(entity, chunks, api_key)
        log.info("Haiku returned %d question(s) for %s", len(questions), entity)

        if not questions:
            log.info("No questions generated for %s", entity)
            continue

        n = _post_questions(entity, channel, questions, slack_token, sent_fps, args.dry_run)
        total_posted += n
        time.sleep(1.0)  # brief pause between entities

    log.info("Proactive gaps run complete — %d question(s) posted", total_posted)
    return 0


if __name__ == "__main__":
    sys.exit(main())
