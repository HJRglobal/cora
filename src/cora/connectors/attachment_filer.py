"""Email attachment auto-filer — watches Gmail inboxes, classifies attachments with
Claude, and stores them in the canonically correct HJR-Founder-OS Drive folder.

Flow (per monitored account):
  1. List emails with attachments since the per-account watermark.
  2. Skip any message already in the message ledger (no re-classify on re-scan).
  3. Call Claude (haiku) to classify each attachment: entity, subfolder, canonical
     filename, or skip if the attachment isn't worth archiving.
  4. For each "file" decision: download bytes → compute md5 → if that content was
     already filed (content ledger, folder-agnostic) skip, else ensure Drive
     folder → upload (Drive also dedups by name + md5) → KB index → append the
     content row to disk immediately.
  5. Mark the message processed in the message ledger.
  6. Advance + persist the per-account watermark IMMEDIATELY (per account, not at
     end-of-run) so a Task-Scheduler kill never discards progress.
  7. Post a Slack summary to EMAIL_FILING_NOTIFY_CHANNEL if anything was filed.

Idempotency is JSONL-ledger-based + crash-safe (data/state/filer-*.jsonl) and
content-aware (md5) so the same document arriving via multiple emails / under
different LLM-chosen names / into different folders is filed exactly once. No
Gmail labels are written (the org moved to invisible JSON dedup in commit
396d8e4; see filer_ledger.py for the full root-cause).

Org-wide: the monitored account list lives in
  data/maps/monitored-email-accounts.yaml
and can include any @hjrglobal.com user for whom the service account has
gmail.modify DWD scope. All attachments are filed into the shared
HJR-Founder-OS Drive (accessed as Harrison via the Drive SA grant).

Canonical filename format:
  YYYY-MM-DD_{entity-code}_{description}.{ext}
  e.g. 2026-05-27_f3e_q2-distribution-agreement.pdf

Drive path:
  HJR-Founder-OS/{entity-folder}/{subfolder}/{canonical-filename}
  e.g. HJR-Founder-OS/02-F3-Energy/contracts/2026-05-27_f3e_q2-distribution-agreement.pdf
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anthropic
import yaml
from slack_sdk import WebClient as _SlackWebClient
from slack_sdk.errors import SlackApiError as _SlackApiError

from . import filer_ledger
from .drive_connector import (
    DriveConnectorError,
    DriveFile,
    drive_file_to_document,
    ensure_folder_path,
    list_folder_files_with_md5,
    resolve_folder_path,
    upload_file,
)
from .gmail_reader import (
    GmailReaderError,
    download_attachment,
    get_message,
    list_messages_with_attachments,
    parse_message_metadata,
)

log = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ACCOUNTS_PATH = _REPO_ROOT / "data" / "maps" / "monitored-email-accounts.yaml"
_WATERMARKS_PATH = _REPO_ROOT / "data" / "cache" / "email-filing-watermarks.json"

# Self-imposed wall-clock budget. The Task Scheduler job has a 15-min hard
# ExecutionTimeLimit that SIGKILLs the process -- which previously killed runs
# before the end-of-run state save, freezing the watermark and re-scanning weeks
# of mail every run. We now stop cleanly under that limit (default 13 min),
# persisting per-account progress so the next run resumes. Doctrine: script-side
# self-bounding is the real control; the Task Scheduler limit only backstops it.
_RUN_BUDGET_SECONDS = int(os.environ.get("EMAIL_FILING_RUN_BUDGET_SECONDS", "780"))

# Maps entity code → Drive folder name under HJR-Founder-OS
_ENTITY_TO_DRIVE_FOLDER: dict[str, str] = {
    "HJRG": "01-HJR-Global",
    "F3E": "02-F3-Energy",
    "F3C": "03-F3-Community",
    "UFL": "04-UFL",
    "HJRPROD": "05-HJR-Productions",
    "HJRP": "06-HJR-Properties",
    "BDM": "07-Big-D-Media",
    "LEX": "08-Lexington-Services",
    "OSN": "09-One-Stop-Nutrition",
    "FNDR": "00-Founder",
}

_VALID_ENTITIES = frozenset(_ENTITY_TO_DRIVE_FOLDER.keys())

_VALID_SUBFOLDERS = frozenset({
    "contracts",      # signed agreements, distribution, employment, vendor
    "invoices",       # vendor invoices, supplier bills, expense reports
    "proposals",      # pitch decks, RFPs, business proposals
    "reports",        # performance, analytics, monthly/quarterly reports
    "brand-assets",   # logos, design files, brand guidelines
    "legal",          # NDAs, compliance docs, filings, registrations
    "financial",      # statements, forecasts, P&L, bank docs (business only)
    "correspondence", # official letters, regulatory correspondence
})

# Claude model for classification — haiku for cost efficiency on scheduled runs
_CLASSIFIER_MODEL = "claude-haiku-4-5-20251001"

# Default: process emails from the last 24 hours if no watermark exists
_DEFAULT_LOOKBACK_HOURS = int(os.environ.get("EMAIL_FILING_LOOKBACK_HOURS", "24"))

_NOTIFY_CHANNEL = os.environ.get("EMAIL_FILING_NOTIFY_CHANNEL", "cora-filing")

# Sanitize description to safe filename chars
_UNSAFE_CHARS_RE = re.compile(r"[^a-z0-9\-]")


class AttachmentFilerError(Exception):
    pass


# ────────────────────────────────────────────────────────────────────────────
# Watermark persistence
# ────────────────────────────────────────────────────────────────────────────


def _load_watermarks() -> dict[str, int]:
    if _WATERMARKS_PATH.exists():
        try:
            return json.loads(_WATERMARKS_PATH.read_text())
        except Exception:
            return {}
    return {}


def _save_watermarks(marks: dict[str, int]) -> None:
    _WATERMARKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _WATERMARKS_PATH.write_text(json.dumps(marks, indent=2))


# ────────────────────────────────────────────────────────────────────────────
# PHI guardrail (Fix 5)
# ────────────────────────────────────────────────────────────────────────────

_PHI_RE = re.compile(
    r"(service note|care plan|incident report|prior auth|iep|arc|support plan"
    r"|clinical note|assessment|discharge|intake form|medication|\bmom\b|\bdad\b)",
    re.IGNORECASE,
)


def _is_phi_risk(subject: str) -> bool:
    """Return True if the email subject matches PHI-risk patterns for LEX inboxes."""
    return bool(_PHI_RE.search(subject))


# ────────────────────────────────────────────────────────────────────────────
# Account list
# ────────────────────────────────────────────────────────────────────────────


def load_monitored_accounts() -> list[dict[str, Any]]:
    """Load accounts from monitored-email-accounts.yaml that have attachment_filer enabled."""
    if not _ACCOUNTS_PATH.exists():
        raise AttachmentFilerError(
            f"Monitored accounts file not found: {_ACCOUNTS_PATH}"
        )
    data = yaml.safe_load(_ACCOUNTS_PATH.read_text()) or {}
    accounts = data.get("accounts", [])
    eligible = [
        a for a in accounts
        if a.get("enabled", True) and a.get("attachment_filer", True)
    ]
    log.info("Loaded %d attachment_filer-eligible accounts", len(eligible))
    return eligible


# ────────────────────────────────────────────────────────────────────────────
# Claude classification
# ────────────────────────────────────────────────────────────────────────────

_CLASSIFICATION_SYSTEM = """\
You are Cora's document filing assistant for HJR Global, a multi-brand portfolio company.
You analyze emails with attachments and decide which ones are worth archiving in the
company's Google Drive, and where each attachment should be filed.

You MUST respond with valid JSON only — no markdown fences, no explanation outside JSON.

Response schema (array of decisions, one per attachment):
{
  "attachments": [
    {
      "filename": "<original filename>",
      "action": "file" | "skip",
      "entity": "<entity code>",        // required if action=file
      "subfolder": "<subfolder name>",  // required if action=file
      "description": "<slug>",          // required if action=file; lowercase-kebab-case, max 8 words
      "reason": "<one sentence>"
    }
  ]
}

Entity codes: HJRG (HJR Global), F3E (F3 Energy), F3C (F3 Community), UFL (United Fight League),
HJRPROD (HJR Productions), HJRP (HJR Properties), BDM (Big-D Media), LEX (Lexington Services),
OSN (One Stop Nutrition), FNDR (Founder/cross-portfolio).

Valid subfolders: contracts, invoices, proposals, reports, brand-assets, legal, financial, correspondence.

FILE attachments that are:
- Contracts, agreements, MOUs, distribution agreements, employment contracts, NDAs (signed)
- Invoices from vendors, suppliers, service providers
- Proposals, pitch decks, RFPs, SOWs
- Reports (monthly, quarterly, performance, financial, analytics)
- Legal filings, compliance documents, government correspondence
- Brand assets (logos, design files) from agencies or partners
- Financial statements, forecasts, bank documents

SKIP attachments that are:
- Platform notifications (Slack, Asana, GitHub, marketing tools)
- Newsletters or promotional/marketing emails
- Calendar invites (handled elsewhere)
- Shipping/order confirmations (unless it's a formal invoice)
- Auto-generated system reports from SaaS tools
- Personal photos, screen recordings, informal internal files
- Anything under 5KB (likely a tracking pixel or signature image)
"""


def classify_attachments(
    email_meta: dict[str, Any],
    attachments: list[dict[str, Any]],
    entity_hint: str | None = None,
) -> list[dict[str, Any]]:
    """Call Claude haiku to classify each attachment. Returns a list of decisions."""
    if not attachments:
        return []

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise AttachmentFilerError("ANTHROPIC_API_KEY not set")

    client = anthropic.Anthropic(api_key=api_key)

    att_lines = "\n".join(
        f"  - {a['filename']} ({a['mime_type']}, {a['size'] // 1024}KB)"
        for a in attachments
    )

    date_str = datetime.fromtimestamp(email_meta["date_ts"], tz=timezone.utc).strftime(
        "%Y-%m-%d"
    )

    hint_line = (
        f"Account entity context: {entity_hint} — default to this entity unless "
        "the email content clearly indicates otherwise.\n\n"
    ) if entity_hint else ""

    user_message = (
        f"{hint_line}"
        f"Email received {date_str}:\n"
        f"From: {email_meta['from']}\n"
        f"To: {email_meta['to']}\n"
        f"Subject: {email_meta['subject']}\n"
        f"Snippet: {email_meta['snippet']}\n\n"
        f"Attachments:\n{att_lines}"
    )

    try:
        response = client.messages.create(
            model=_CLASSIFIER_MODEL,
            max_tokens=1024,
            system=_CLASSIFICATION_SYSTEM,
            messages=[{"role": "user", "content": user_message}],
        )
    except Exception as exc:
        raise AttachmentFilerError(f"Claude classification failed: {exc}") from exc

    raw = response.content[0].text.strip()
    # Strip markdown code fences if Claude wrapped the JSON (e.g. ```json ... ```)
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]  # drop opening fence line
        raw = raw.rsplit("```", 1)[0].strip()  # drop closing fence

    try:
        parsed = json.loads(raw)
        decisions = parsed.get("attachments", [])
    except json.JSONDecodeError as exc:
        log.warning("Claude returned non-JSON for email %r: %s", email_meta["subject"], raw[:200])
        raise AttachmentFilerError(f"Claude response was not valid JSON: {exc}") from exc

    return decisions


# ────────────────────────────────────────────────────────────────────────────
# Canonical filename construction
# ────────────────────────────────────────────────────────────────────────────


def _canonical_filename(
    decision: dict[str, Any],
    original_filename: str,
    date_ts: int,
) -> str:
    """Build YYYY-MM-DD_{entity}_{description}.{ext} from a classification decision."""
    date_str = datetime.fromtimestamp(date_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    entity = decision.get("entity", "hjrg").lower()
    description = decision.get("description", "document").lower()

    # Sanitize description — only lowercase letters, digits, hyphens
    description = _UNSAFE_CHARS_RE.sub("-", description).strip("-")
    description = re.sub(r"-{2,}", "-", description)

    # Preserve original extension
    ext = ""
    if "." in original_filename:
        ext = "." + original_filename.rsplit(".", 1)[-1].lower()

    return f"{date_str}_{entity}_{description}{ext}"


# ────────────────────────────────────────────────────────────────────────────
# Single-email processing
# ────────────────────────────────────────────────────────────────────────────


def process_email(
    user_email: str,
    message_id: str,
    *,
    dry_run: bool = False,
    kb=None,
    entity_hint: str | None = None,
    content_ledger: dict[str, dict[str, Any]] | None = None,
    seen_messages: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Process one email: classify → download → md5-dedup → upload → index → record.

    Two idempotency layers (crash-safe, persisted incrementally):
      * message ledger — a fully-processed message is never re-classified
        (skips the Claude call entirely on re-scan).
      * content ledger — the same bytes (md5), arriving via ANY message under
        ANY name into ANY folder, are filed exactly once.

    Returns a list of NEW filing results (one per newly-filed attachment). An
    empty list means everything was skipped, deduped, or already processed.
    """
    if content_ledger is None:
        content_ledger = {}
    if seen_messages is None:
        seen_messages = set()

    try:
        msg = get_message(user_email, message_id)
    except GmailReaderError as exc:
        log.warning("Skipping message %s: %s", message_id, exc)
        return []

    meta = parse_message_metadata(msg)
    msg_key = filer_ledger.make_msg_key(meta.get("rfc_message_id", ""), meta["message_id"])

    # Message-level dedup: skip re-classification of a fully-processed message.
    if filer_ledger.message_done(seen_messages, msg_key):
        log.debug("Message %r already processed — skipping (no re-classify)", msg_key[:60])
        return []

    # PHI guardrail: skip LEX inbox emails that match client-care subject patterns
    if entity_hint and entity_hint.startswith("LEX") and _is_phi_risk(meta["subject"]):
        log.info(
            "PHI risk detected in %s subject=%r — skipping (LEX PHI guardrail)",
            user_email, meta["subject"][:80],
        )
        return []

    raw_attachments = [a for a in meta["attachments"] if a["size"] > 5000]
    if not raw_attachments:
        log.debug("Message %s has no substantial attachments — skipping", message_id)
        return []

    log.info(
        "Classifying %d attachment(s) from: %s / %s",
        len(raw_attachments), meta["from"], meta["subject"],
    )

    try:
        decisions = classify_attachments(meta, raw_attachments, entity_hint=entity_hint)
    except AttachmentFilerError as exc:
        log.warning("Classification failed for %s: %s", message_id, exc)
        return []

    # Build filename → attachment lookup
    att_by_name = {a["filename"]: a for a in raw_attachments}

    results: list[dict[str, Any]] = []
    skipped_count = 0

    for decision in decisions:
        if decision.get("action") != "file":
            skipped_count += 1
            log.debug(
                "Skip %r: %s",
                decision.get("filename"), decision.get("reason", ""),
            )
            continue

        entity = decision.get("entity", "").upper()
        subfolder = decision.get("subfolder", "").lower()
        orig_filename = decision.get("filename", "")

        # INVARIANT (WS9): every file that reaches upload_file() has a VALID entity
        # AND a valid subfolder, so it always lands in a named HJR-Founder-OS/{entity}/
        # {subfolder} path — never My Drive root, never a review/limbo folder. A
        # malformed classification (unknown entity or subfolder) is SKIPPED here, not
        # filed somewhere ambiguous. (Pairs with WS8 safe_drive_create, which fails
        # closed if a create ever reaches it without a parent folder.)
        if entity not in _VALID_ENTITIES:
            log.warning("Unknown entity %r in decision for %r — skipping", entity, orig_filename)
            continue
        if subfolder not in _VALID_SUBFOLDERS:
            log.warning("Unknown subfolder %r in decision for %r — skipping", subfolder, orig_filename)
            continue

        att = att_by_name.get(orig_filename)
        if not att:
            log.warning("Attachment %r not found in message — skipping", orig_filename)
            continue

        canonical = _canonical_filename(decision, orig_filename, meta["date_ts"])
        entity_folder = _ENTITY_TO_DRIVE_FOLDER[entity]
        drive_path_segments = [entity_folder, subfolder]
        drive_path_display = f"{entity_folder}/{subfolder}/{canonical}"

        if dry_run:
            log.info("[DRY RUN] Would file %r -> %s", orig_filename, drive_path_display)
            results.append({
                "original_filename": orig_filename,
                "canonical_filename": canonical,
                "drive_path": drive_path_display,
                "entity": entity,
                "subfolder": subfolder,
                "reason": decision.get("reason", ""),
                "dry_run": True,
            })
            continue

        # Download (needed to compute the content hash + to upload)
        try:
            content = download_attachment(user_email, message_id, att)
        except GmailReaderError as exc:
            log.warning("Download failed for %r: %s", orig_filename, exc)
            continue

        content_md5 = hashlib.md5(content).hexdigest()
        content_sha256 = hashlib.sha256(content).hexdigest()

        # Content-level dedup: these exact bytes were already filed (possibly via
        # a different email / name / folder). Skip — do not create a duplicate.
        prior = filer_ledger.content_record(content_ledger, content_md5)
        if prior is not None:
            log.info(
                "Content already filed (md5=%s) as %s — skipping %r",
                content_md5, prior.get("drive_path", "?"), orig_filename,
            )
            skipped_count += 1
            continue

        # Ensure folder exists in Drive
        try:
            folder_id = ensure_folder_path(drive_path_segments)
        except DriveConnectorError as exc:
            log.warning("Folder creation failed for %r: %s", drive_path_display, exc)
            continue

        # Upload (upload_file dedups by name AND by content md5 within the folder)
        try:
            file_id, web_link = upload_file(
                folder_id, canonical, content, att["mime_type"], content_md5=content_md5,
            )
        except DriveConnectorError as exc:
            log.warning("Upload failed for %r: %s", canonical, exc)
            continue

        log.info("Filed %r -> %s (%s)", orig_filename, drive_path_display, web_link)

        # Record content hash immediately so the very next attachment / message /
        # run dedups against it even if this run is killed mid-flight.
        filer_ledger.append_content(
            content_ledger, content_md5,
            file_id=file_id, web_link=web_link, drive_path=drive_path_display,
            canonical=canonical, sha256=content_sha256, source_email=user_email,
        )

        # Immediate KB indexing so Cora can answer questions without waiting for next sync
        if kb is not None:
            try:
                date_ts = meta["date_ts"]
                df = DriveFile(
                    file_id=file_id,
                    name=canonical,
                    mime_type=att["mime_type"],
                    path=f"HJR-Founder-OS/{drive_path_display}",
                    modified_time=date_ts,
                    created_time=date_ts,
                    owner_email=meta["from"],
                    web_view_link=web_link,
                    size_bytes=len(content),
                )
                doc = drive_file_to_document(df)
                kb.upsert_documents([doc])
                log.info("KB indexed: %r", canonical)
            except Exception as exc:
                log.warning("KB indexing failed for %r: %s", canonical, exc)

        results.append({
            "original_filename": orig_filename,
            "canonical_filename": canonical,
            "drive_path": drive_path_display,
            "web_link": web_link,
            "entity": entity,
            "subfolder": subfolder,
            "reason": decision.get("reason", ""),
            "dry_run": False,
        })

    # Mark the whole message processed so a re-scan never re-classifies it.
    # (Crash before this point => message reprocessed next run, but already-filed
    # attachments are caught by the content ledger above => still no duplicate.)
    if not dry_run:
        filer_ledger.record_message_done(
            seen_messages, msg_key,
            filed=len(results), skipped=skipped_count, subject=meta["subject"],
        )

    return results


# ────────────────────────────────────────────────────────────────────────────
# Per-account processing
# ────────────────────────────────────────────────────────────────────────────


def process_account(
    account: dict[str, Any],
    watermarks: dict[str, int],
    *,
    dry_run: bool = False,
    kb=None,
    content_ledger: dict[str, dict[str, Any]] | None = None,
    seen_messages: set[str] | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Run the full filer pipeline for one monitored email account.

    Returns a summary dict:
        {email, messages_scanned, filed, skipped, errors, list_failed, budget_hit}

    `list_failed` is True only if the message LISTING itself failed (so the
    caller must NOT advance the watermark). Per-message file errors do NOT set
    it — re-scanning is cheap and the ledgers dedup, so a single bad attachment
    must never permanently freeze the watermark (the 2026-05-28 → 06-14 bug).
    """
    user_email = account["email"]
    entity_hint: str | None = account.get("entity_default")
    since_ts = watermarks.get(user_email, int(time.time()) - _DEFAULT_LOOKBACK_HOURS * 3600)

    log.info(
        "Processing %s — looking back to %s",
        user_email,
        datetime.fromtimestamp(since_ts, tz=timezone.utc).isoformat(),
    )

    summary: dict[str, Any] = {
        "email": user_email,
        "messages_scanned": 0,
        "filed": 0,
        "skipped": 0,
        "errors": 0,
        "filed_items": [],
        "list_failed": False,
        "budget_hit": False,
    }

    try:
        message_ids = list_messages_with_attachments(user_email, since_ts)
    except GmailReaderError as exc:
        log.error("Cannot list messages for %s: %s", user_email, exc)
        summary["errors"] += 1
        summary["list_failed"] = True
        return summary

    summary["messages_scanned"] = len(message_ids)
    log.info("%s: %d messages with attachments to examine", user_email, len(message_ids))

    for msg_id in message_ids:
        if deadline is not None and time.time() > deadline:
            log.warning(
                "%s: run budget hit mid-account — stopping (watermark NOT advanced; resumes next run)",
                user_email,
            )
            summary["budget_hit"] = True
            break
        try:
            filed = process_email(
                user_email, msg_id,
                dry_run=dry_run, kb=kb, entity_hint=entity_hint,
                content_ledger=content_ledger, seen_messages=seen_messages,
            )
        except Exception as exc:
            log.exception("Unexpected error processing %s/%s: %s", user_email, msg_id, exc)
            summary["errors"] += 1
            continue

        if filed:
            summary["filed"] += len(filed)
            summary["filed_items"].extend(filed)
        else:
            summary["skipped"] += 1

    return summary


# ────────────────────────────────────────────────────────────────────────────
# Main entry point
# ────────────────────────────────────────────────────────────────────────────


def run_filer(
    *,
    accounts: list[dict[str, Any]] | None = None,
    dry_run: bool = False,
    kb=None,
) -> list[dict[str, Any]]:
    """Run the attachment filer across all (or specified) monitored accounts.

    Returns list of per-account summary dicts. Advances watermarks on success.
    """
    if accounts is None:
        accounts = load_monitored_accounts()

    if not accounts:
        log.warning("No enabled accounts configured in %s", _ACCOUNTS_PATH)
        return []

    watermarks = _load_watermarks()
    # Ledgers loaded ONCE into memory (O(1) checks); rows are appended to disk
    # the instant something is filed, so progress survives a kill mid-run.
    content_ledger = filer_ledger.load_content_ledger()
    seen_messages = filer_ledger.load_message_ledger()
    run_start = int(time.time())
    deadline = run_start + _RUN_BUDGET_SECONDS
    summaries: list[dict[str, Any]] = []

    for account in accounts:
        if time.time() > deadline:
            log.warning(
                "Run budget (%ds) exhausted before %s — deferring remaining accounts "
                "to next run", _RUN_BUDGET_SECONDS, account["email"],
            )
            break

        summary = process_account(
            account, watermarks, dry_run=dry_run, kb=kb,
            content_ledger=content_ledger, seen_messages=seen_messages,
            deadline=deadline,
        )
        summaries.append(summary)

        # Advance + PERSIST the watermark per-account, immediately. Advance
        # whenever the listing succeeded AND we finished the account within
        # budget — per-message file errors do not block it (ledgers dedup any
        # re-scan). A budget-hit account keeps its old watermark so the
        # unprocessed tail is re-listed next run. Saving per-account (not once at
        # the very end) is what breaks the kill-before-save spiral.
        if not dry_run and not summary["list_failed"] and not summary["budget_hit"]:
            watermarks[account["email"]] = run_start
            _save_watermarks(watermarks)
            log.info("Watermark advanced + saved for %s -> %d", account["email"], run_start)

    return summaries


# ────────────────────────────────────────────────────────────────────────────
# Reconcile — seed the content ledger from files already in Drive
# ────────────────────────────────────────────────────────────────────────────


def reconcile_ledger_from_drive(
    entities: list[str] | None = None,
) -> dict[str, int]:
    """Seed the content ledger from binary files already in the filer's Drive
    subfolders, so the next live run dedups against documents already on Drive.

    Read-only: never uploads, never creates folders, never modifies Drive. For
    each existing {entity}/{subfolder}, every binary file's md5Checksum that
    isn't already in the ledger is recorded. Run this once after deploying the
    crash-safe ledger (and after any manual de-dupe cleanup) so the very next
    run skips documents whose canonical copy already lives in Drive — regardless
    of which name/folder the classifier would otherwise pick.

    Returns {scanned, seeded, ledger_size}.
    """
    content_ledger = filer_ledger.load_content_ledger()
    targets = [e.upper() for e in (entities or list(_ENTITY_TO_DRIVE_FOLDER.keys()))]
    scanned = 0
    seeded = 0

    for ent in targets:
        folder = _ENTITY_TO_DRIVE_FOLDER.get(ent)
        if not folder:
            log.warning("reconcile: unknown entity %r — skipping", ent)
            continue
        for sub in sorted(_VALID_SUBFOLDERS):
            try:
                leaf_id = resolve_folder_path([folder, sub])
            except DriveConnectorError as exc:
                log.warning("reconcile: lookup failed for %s/%s: %s", folder, sub, exc)
                continue
            if not leaf_id:
                continue  # folder doesn't exist yet — nothing to seed
            try:
                files = list_folder_files_with_md5(leaf_id)
            except DriveConnectorError as exc:
                log.warning("reconcile: list failed for %s/%s: %s", folder, sub, exc)
                continue
            for f in files:
                scanned += 1
                md5 = f.get("md5Checksum")
                if not md5 or md5 in content_ledger:
                    continue
                filer_ledger.append_content(
                    content_ledger, md5,
                    file_id=f["id"], web_link=f.get("webViewLink", ""),
                    drive_path=f"{folder}/{sub}/{f.get('name', '')}",
                    canonical=f.get("name", ""), sha256="", source_email="reconcile",
                )
                seeded += 1
            if files:
                log.info("reconcile: %s/%s — %d files scanned", folder, sub, len(files))

    log.info(
        "reconcile complete: scanned=%d seeded=%d ledger_size=%d",
        scanned, seeded, len(content_ledger),
    )
    return {"scanned": scanned, "seeded": seeded, "ledger_size": len(content_ledger)}


# ────────────────────────────────────────────────────────────────────────────
# Slack summary notification
# ────────────────────────────────────────────────────────────────────────────


def post_slack_summary(summaries: list[dict[str, Any]]) -> bool:
    """Post a filing summary to the configured Slack channel. Non-fatal on failure."""
    total_filed = sum(s["filed"] for s in summaries)
    if total_filed == 0:
        log.info("No attachments filed this run — skipping Slack notification")
        return True

    lines = [f":file_folder: *Email Attachment Filing Summary* — {total_filed} file(s) archived"]
    for summary in summaries:
        if not summary["filed_items"]:
            continue
        lines.append(f"\n*{summary['email']}*")
        for item in summary["filed_items"]:
            if item.get("web_link"):
                lines.append(
                    f"  • <{item['web_link']}|{item['canonical_filename']}> -> `{item['drive_path'].rsplit('/', 1)[0]}`"
                )
            else:
                lines.append(
                    f"  • `{item['canonical_filename']}` -> `{item['drive_path'].rsplit('/', 1)[0]}`"
                )
            lines.append(f"    _{item.get('reason', '')}_")

    text = "\n".join(lines)

    try:
        token = os.environ.get("SLACK_BOT_TOKEN", "")
        if not token:
            log.warning("SLACK_BOT_TOKEN not set — Slack notification skipped")
            return False

        client = _SlackWebClient(token=token)
        client.chat_postMessage(channel=_NOTIFY_CHANNEL, text=text)
        log.info("Slack filing summary posted to #%s", _NOTIFY_CHANNEL)
        return True
    except _SlackApiError as exc:
        log.warning("Slack post failed: %s", exc.response.get("error", str(exc)))
        return False
    except Exception as exc:
        log.warning("Slack notification failed: %s", exc)
        return False
