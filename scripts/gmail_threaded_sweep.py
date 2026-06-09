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


def _order_accounts(
    accounts: list[dict[str, Any]],
    watermarks: dict[str, int],
    fallback_ts: int,
) -> list[dict[str, Any]]:
    """Return accounts sorted stale-first (oldest current watermark first).

    Accounts that have never been swept (no watermark) use fallback_ts, which is
    older than any persisted watermark from a prior run, so never-swept accounts
    naturally sort to the front. This guarantees that even if a run is cut short
    (Task Scheduler time limit), the most-neglected mailboxes are processed first
    and every account is reached over successive runs.
    """
    return sorted(
        accounts,
        key=lambda a: watermarks.get(a.get("email", ""), fallback_ts),
    )


def _next_watermark(
    thread_count: int,
    max_threads: int,
    newest_processed_ts: int,
    sync_start: int,
) -> int:
    """Decide the watermark to persist for an account after a sweep pass.

    - If the account returned FEWER threads than the cap, we drained everything
      since its old watermark, so advance to sync_start (fully caught up).
    - If the cap was hit, there is almost certainly older backlog we did NOT
      reach this pass. Advancing to sync_start would silently skip it forever.
      Instead advance only to the newest message we actually processed, so the
      next run re-queries and continues draining the backlog (idempotent upsert
      absorbs the small overlap). If somehow nothing parsed, leave the watermark
      untouched by returning 0 (caller treats 0 as "no change").
    """
    if thread_count < max_threads:
        return sync_start
    if newest_processed_ts > 0:
        return newest_processed_ts
    return 0


def _upsert_with_retry(kb, docs, log, attempts: int = 5, base_delay: float = 2.0) -> int:
    """Upsert a batch, retrying on transient 'database is locked' errors.

    The KB connection now sets busy_timeout=30s, but a long backfill running while
    the live Cora service is writing can still occasionally exceed it. Rather than
    crash the whole run (and lose the in-progress account), back off and retry.
    Re-raises any non-lock error, and the lock error after the final attempt.
    """
    import sqlite3
    for attempt in range(1, attempts + 1):
        try:
            return kb.upsert_documents(docs)
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == attempts:
                raise
            delay = base_delay * attempt
            log.warning(
                "KB locked on upsert (attempt %d/%d) -- retrying in %.0fs",
                attempt, attempts, delay,
            )
            time.sleep(delay)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fallback-days", type=int, default=2,
        help="Days to look back if no watermark exists for an account (default 2)",
    )
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument(
        "--max-threads", type=int, default=MAX_THREADS_PER_ACCOUNT,
        help=f"Max threads per account per run (default {MAX_THREADS_PER_ACCOUNT}). "
             "Raise for a one-time deep historical backfill.",
    )
    parser.add_argument(
        "--accounts", type=str, default="",
        help="Comma-separated list of account emails to sweep (default: all enabled). "
             "Use for targeted backfill of specific mailboxes.",
    )
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
    max_threads = max(1, args.max_threads)

    # Optional --accounts filter for targeted backfill
    account_filter = {e.strip().lower() for e in args.accounts.split(",") if e.strip()}
    if account_filter:
        accounts = [a for a in accounts if a.get("email", "").lower() in account_filter]
        log.info("Account filter active: %d of requested %d accounts matched",
                 len(accounts), len(account_filter))

    # Stale-first: process the most-neglected mailboxes first so that even if this
    # run is cut short by the Task Scheduler time limit, every account is reached
    # over successive runs (no account is permanently starved behind the wall).
    accounts = _order_accounts(accounts, watermarks, fallback_ts)

    total_accounts = 0
    total_threads = 0
    total_docs = 0
    total_chunks = 0
    skipped_accounts = 0
    capped_accounts = 0
    exit_code = 0

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
                user_email, since_ts=watermark_ts, max_results=max_threads
            )
        except GmailReaderError as exc:
            log.warning("Skipping %s: %s", user_email, exc)
            skipped_accounts += 1
            exit_code = 2
            continue

        log.info("Found %d threads since watermark for %s", len(thread_ids), user_email)
        docs_batch: list[Document] = []
        newest_processed_ts = 0  # newest message ts we actually examined this account

        for thread_id in thread_ids:
            try:
                messages = get_full_thread_text(user_email, thread_id)
            except GmailReaderError as exc:
                log.warning("Skipping thread %s for %s: %s", thread_id, user_email, exc)
                continue

            if not messages:
                continue

            # Track coverage boundary across ALL examined threads (incl. PHI-skipped),
            # so the cap-aware watermark reflects everything we actually looked at.
            thread_newest = max((m.get("date_ts", 0) for m in messages), default=0)
            if thread_newest > newest_processed_ts:
                newest_processed_ts = thread_newest

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
                chunk_count = _upsert_with_retry(kb, docs_batch, log)
                total_chunks += chunk_count
                total_docs += len(docs_batch)
                log.info(
                    "Batch upserted: %d docs / %d chunks (account=%s)",
                    len(docs_batch), chunk_count, user_email,
                )
                docs_batch = []

        if docs_batch:
            chunk_count = _upsert_with_retry(kb, docs_batch, log)
            total_chunks += chunk_count
            total_docs += len(docs_batch)
            log.info(
                "Final batch for %s: %d docs / %d chunks",
                user_email, len(docs_batch), chunk_count,
            )

        # Cap-aware, incremental watermark persistence. We persist AFTER EACH
        # account so a mid-run kill (Task Scheduler time limit) still advances
        # every completed account -- the next run resumes with the remainder.
        next_wm = _next_watermark(
            thread_count=len(thread_ids),
            max_threads=max_threads,
            newest_processed_ts=newest_processed_ts,
            sync_start=sync_start,
        )
        if next_wm > 0:
            watermarks[user_email] = next_wm
            if len(thread_ids) >= max_threads:
                capped_accounts += 1
                log.warning(
                    "%s hit the %d-thread cap -- watermark advanced only to %d "
                    "(newest processed); older backlog remains. Run a deeper "
                    "backfill with --max-threads / --fallback-days to fully drain.",
                    user_email, max_threads, next_wm,
                )
        _save_watermarks(watermarks)
        total_accounts += 1

    log.info(
        "Gmail sweep complete -- %d accounts, %d threads, %d docs -> %d chunks "
        "(%d skipped, %d capped, exit=%d)",
        total_accounts, total_threads, total_docs, total_chunks,
        skipped_accounts, capped_accounts, exit_code,
    )

    kb.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
