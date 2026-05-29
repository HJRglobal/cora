#!/usr/bin/env python3
"""Daily Gmail full-thread sweep for KB ingestion (multi-user DWD).

Nightly at 2:30am AZ. For each enabled account in monitored-email-accounts.yaml:
  1. Fetch thread IDs updated since per-user watermark.
  2. For each thread: call get_full_thread_text() to get all message bodies.
  3. Chunk each message body (<=600 tokens / ~2400 chars).
  4. Derive entity from email labels + subject keyword patterns.
  5. Tag with user's Asana GID so reconciliation can match commitments to tasks.
  6. Ingest to KB with source=gmail, entity=derived.
  7. Advance watermark on clean run.

Pairs with run_attachment_filer.py (no overlap — filer handles Drive uploads,
this script handles KB text ingestion).

Exit codes:
    0 = success
    1 = fatal error
    2 = partial — some accounts skipped (missing scope, disabled, etc.)
"""

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import yaml  # noqa: E402

from cora.connectors.gmail_reader import (  # noqa: E402
    GmailReaderError,
    get_full_thread_text,
    list_threads_since,
)
from cora.knowledge_base import KnowledgeBase, KnowledgeBaseError  # noqa: E402
from cora.knowledge_base.store import Document  # noqa: E402

CORA_REPO_ROOT   = Path(__file__).resolve().parents[1]
KB_DB_PATH       = CORA_REPO_ROOT / "data" / "cora_kb.db"
LOG_DIR          = CORA_REPO_ROOT / "logs"
WATERMARKS_PATH  = CORA_REPO_ROOT / "data" / "cache" / "gmail-thread-watermarks.json"
ACCOUNTS_PATH    = CORA_REPO_ROOT / "data" / "maps" / "monitored-email-accounts.yaml"

# Chunk size in characters (~600 tokens at ~4 chars/token)
MAX_CHUNK_CHARS  = 2400
# Maximum threads to process per account per run (safety cap)
MAX_THREADS_PER_ACCOUNT = 500

# Entity keyword patterns for subject/label-based entity detection
_ENTITY_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bf3|pure|mood|energy drink|f3e\b", re.I), "F3E"),
    (re.compile(r"\bosn|one stop nutrition|gilbert|warner|mckelips|greenfield|val vista\b", re.I), "OSN"),
    (re.compile(r"\blexington|lex\s|llc\s|lbhs|lla\s|lts\s|ddd|ahcccs|hcbs\b", re.I), "LEX"),
    (re.compile(r"\bufl|united fight league|mma league\b", re.I), "UFL"),
    (re.compile(r"\bbig d media|bdm\b|larry stone\b", re.I), "BDM"),
    (re.compile(r"\bhjr properties|hjrp|rogers ranch|cinema lanes|lci realty\b", re.I), "HJRP"),
    (re.compile(r"\bhjr productions|podcast|falling forward|clouthub\b", re.I), "HJRPROD"),
    (re.compile(r"\bhjr global|hjrg|holdco|visibility cpa\b", re.I), "FNDR"),
]


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = LOG_DIR / f"kb-sync-gmail-{today}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def _load_accounts() -> list[dict[str, Any]]:
    if not ACCOUNTS_PATH.exists():
        return []
    with ACCOUNTS_PATH.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    accounts = data.get("accounts", [])
    # Filter to thread_sweep-enabled accounts
    return [a for a in accounts if a.get("enabled", False) and a.get("thread_sweep", True)]


def _load_watermarks() -> dict[str, int]:
    if not WATERMARKS_PATH.exists():
        return {}
    try:
        with WATERMARKS_PATH.open(encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save_watermarks(wm: dict[str, int]) -> None:
    WATERMARKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = WATERMARKS_PATH.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(wm, fh, indent=2)
    tmp.replace(WATERMARKS_PATH)


def _derive_entity(
    subject: str,
    sender: str,
    recipients: str,
    label_ids: list[str],
    account_entity_default: str = "FNDR",
) -> str:
    """Derive entity from email metadata using keyword matching."""
    combined = f"{subject} {sender} {recipients}"
    for pattern, entity in _ENTITY_PATTERNS:
        if pattern.search(combined):
            return entity
    return account_entity_default


def _is_phi_risk(subject: str, label_ids: list[str]) -> bool:
    """Conservative check: does this thread touch LEX client PHI patterns?

    PHI guardrail: we never ingest identifiable client health/care records.
    If a thread looks like it contains PHI (client name + diagnosis/care notes),
    skip it entirely.
    """
    phi_patterns = re.compile(
        r"(service note|care plan|incident report|prior auth|iep|arc|support plan"
        r"|clinical note|assessment|discharge|intake form|medication|\bmom\b|\bdad\b)",
        re.I,
    )
    return bool(phi_patterns.search(subject))


def _chunk_text(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split text into chunks of at most max_chars, preferring paragraph boundaries."""
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks = []
    paragraphs = text.split("\n\n")
    current = []
    current_len = 0

    for para in paragraphs:
        if current_len + len(para) > max_chars and current:
            chunks.append("\n\n".join(current))
            current = []
            current_len = 0
        current.append(para)
        current_len += len(para)

    if current:
        chunks.append("\n\n".join(current))

    return chunks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fallback-days", type=int, default=2,
        help="Days to look back if no watermark exists for an account (default 2)",
    )
    parser.add_argument("--batch-size", type=int, default=50)
    args = parser.parse_args()

    _setup_logging()
    log = logging.getLogger("kb-sync-gmail")
    log.info("=" * 60)
    log.info("Gmail threaded sweep starting")

    accounts = _load_accounts()
    if not accounts:
        log.info("No enabled thread_sweep accounts found in %s", ACCOUNTS_PATH)
        return 0

    watermarks = _load_watermarks()
    kb = KnowledgeBase(KB_DB_PATH)

    sync_start = int(time.time())
    fallback_ts = sync_start - (args.fallback_days * 86400)

    total_accounts = 0
    total_threads = 0
    total_docs = 0
    total_chunks = 0
    skipped_accounts = 0
    exit_code = 0
    new_watermarks: dict[str, int] = {}

    for account in accounts:
        user_email = account.get("email", "")
        display_name = account.get("name", user_email)
        entity_default = account.get("entity_default", "FNDR")
        asana_gid = account.get("asana_gid", "")

        if not user_email:
            continue

        watermark_ts = watermarks.get(user_email, fallback_ts)
        log.info(
            "Sweeping %s (%s, entity_default=%s, since=%d)",
            user_email, display_name, entity_default, watermark_ts,
        )

        try:
            thread_ids = list_threads_since(
                user_email, since_ts=watermark_ts, max_results=MAX_THREADS_PER_ACCOUNT
            )
        except GmailReaderError as exc:
            log.warning("Skipping %s: %s", user_email, exc)
            skipped_accounts += 1
            exit_code = 2
            continue

        log.info("Found %d threads since watermark for %s", len(thread_ids), user_email)
        docs_batch: list[Document] = []

        for thread_id in thread_ids:
            try:
                messages = get_full_thread_text(user_email, thread_id)
            except GmailReaderError as exc:
                log.warning("Skipping thread %s for %s: %s", thread_id, user_email, exc)
                continue

            if not messages:
                continue

            # PHI check on first message subject
            first_subject = messages[0].get("subject", "")
            first_labels = messages[0].get("label_ids", [])
            if _is_phi_risk(first_subject, first_labels):
                log.debug(
                    "PHI risk detected in thread %s subject=%r — skipping",
                    thread_id, first_subject[:80],
                )
                continue

            # Process each message as a Document
            for msg in messages:
                subject = msg.get("subject", "(no subject)")
                sender  = msg.get("sender", "")
                recipients = msg.get("recipients", "")
                date_ts = msg.get("date_ts", 0)
                body_text = msg.get("body_text", "").strip()
                att_names = msg.get("attachment_names", [])

                if not body_text and not att_names:
                    # Empty message (probably calendar invite or delivery notification)
                    continue

                entity = _derive_entity(subject, sender, recipients, msg.get("label_ids", []), entity_default)

                # PHI guardrail: skip LEX chunks that look like client records
                if entity == "LEX" and _is_phi_risk(subject, msg.get("label_ids", [])):
                    continue

                # Build content text
                header = f"From: {sender}\nTo: {recipients}\nSubject: {subject}\nDate: {datetime.fromtimestamp(date_ts, tz=timezone.utc).strftime('%Y-%m-%d') if date_ts else 'unknown'}"
                if att_names:
                    header += f"\nAttachments: {', '.join(att_names)}"
                full_text = header + "\n\n" + body_text

                chunks = _chunk_text(full_text)
                source_id = f"gmail:{user_email}:{msg['message_id']}"

                for i, chunk in enumerate(chunks):
                    doc = Document(
                        source="gmail",
                        source_id=f"{source_id}:chunk{i}" if len(chunks) > 1 else source_id,
                        entity=entity,
                        content=chunk,
                        date_created=date_ts,
                        date_modified=date_ts,
                        author=sender,
                        title=subject[:200],
                        deep_link="",  # No direct Gmail deep link via SA
                        metadata={
                            "user_email": user_email,
                            "asana_gid": asana_gid,
                            "thread_id": thread_id,
                            "message_id": msg["message_id"],
                            "sender": sender,
                        },
                    )
                    docs_batch.append(doc)

            total_threads += 1

            if len(docs_batch) >= args.batch_size:
                chunk_count = kb.upsert_documents(docs_batch)
                total_chunks += chunk_count
                total_docs += len(docs_batch)
                log.info(
                    "Batch upserted: %d docs / %d chunks (account=%s)",
                    len(docs_batch), chunk_count, user_email,
                )
                docs_batch = []

        if docs_batch:
            chunk_count = kb.upsert_documents(docs_batch)
            total_chunks += chunk_count
            total_docs += len(docs_batch)
            log.info(
                "Final batch for %s: %d docs / %d chunks",
                user_email, len(docs_batch), chunk_count,
            )

        new_watermarks[user_email] = sync_start
        total_accounts += 1

    merged = {**watermarks, **new_watermarks}
    _save_watermarks(merged)
    log.info(
        "Gmail sweep complete — %d accounts, %d threads, %d docs -> %d chunks (exit=%d)",
        total_accounts, total_threads, total_docs, total_chunks, exit_code,
    )

    kb.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
