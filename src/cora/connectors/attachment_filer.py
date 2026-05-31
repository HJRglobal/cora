"""Email attachment auto-filer — watches Gmail inboxes, classifies attachments with
Claude, and stores them in the canonically correct HJR-Founder-OS Drive folder.

Flow (per monitored account):
  1. List unprocessed emails with attachments since last watermark.
  2. Skip any already labeled "Cora-Filed" (idempotency within a watermark window).
  3. Call Claude (haiku) to classify each attachment: entity, subfolder, canonical
     filename, or skip if the attachment isn't worth archiving.
  4. For each "file" decision: download bytes → ensure Drive folder → upload → KB index.
  5. Apply the "Cora-Filed" label to the message.
  6. Advance the per-account watermark.
  7. Post a Slack summary to EMAIL_FILING_NOTIFY_CHANNEL if anything was filed.

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

from .drive_connector import (
    DriveConnectorError,
    DriveFile,
    drive_file_to_document,
    ensure_folder_path,
    upload_file,
)
from .gmail_reader import (
    GmailReaderError,
    apply_label,
    download_attachment,
    ensure_cora_label,
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
_FILED_IDS_PATH = _REPO_ROOT / "data" / "cache" / "filed-message-ids.json"
_DEDUP_TTL_DAYS = 30

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
# Cross-account deduplication (Fix 2)
# ────────────────────────────────────────────────────────────────────────────


def _load_filed_ids() -> dict[str, int]:
    """Load {rfc_message_id: filed_at_ts}, pruning entries older than _DEDUP_TTL_DAYS."""
    if not _FILED_IDS_PATH.exists():
        return {}
    try:
        data = json.loads(_FILED_IDS_PATH.read_text())
    except Exception:
        return {}
    cutoff = int(time.time()) - _DEDUP_TTL_DAYS * 86400
    return {k: v for k, v in data.items() if isinstance(v, int) and v > cutoff}


def _save_filed_ids(filed_ids: dict[str, int]) -> None:
    _FILED_IDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _FILED_IDS_PATH.write_text(json.dumps(filed_ids, indent=2))


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
    label_id: str,
    dry_run: bool = False,
    kb=None,
    entity_hint: str | None = None,
    filed_ids: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Process one email: classify → download → upload → index → label.

    Returns a list of filing results (one per filed attachment). Empty list
    means all attachments were skipped or the email was already processed.
    """
    try:
        msg = get_message(user_email, message_id)
    except GmailReaderError as exc:
        log.warning("Skipping message %s: %s", message_id, exc)
        return []

    meta = parse_message_metadata(msg)
    rfc_msg_id = meta.get("rfc_message_id", "")

    # Already processed on a previous run (same mailbox)
    if label_id in meta["labels"]:
        log.debug("Message %s already labeled Cora-Filed — skipping", message_id)
        return []

    # Cross-account deduplication: same RFC Message-ID filed from another inbox
    if rfc_msg_id and filed_ids is not None and rfc_msg_id in filed_ids:
        log.info(
            "Message %r already filed from another account — stamping and skipping",
            rfc_msg_id[:60],
        )
        if not dry_run:
            try:
                apply_label(user_email, message_id, label_id)
            except GmailReaderError as exc:
                log.warning("Label stamp failed for dedup skip %s: %s", message_id, exc)
        return []

    # PHI guardrail: skip LEX inbox emails that match client-care subject patterns
    if entity_hint and entity_hint.startswith("LEX") and _is_phi_risk(meta["subject"]):
        log.info(
            "PHI risk detected in %s subject=%r — skipping (LEX PHI guardrail)",
            user_email, meta["subject"][:80],
        )
        if not dry_run:
            try:
                apply_label(user_email, message_id, label_id)
            except GmailReaderError as exc:
                log.warning("Label stamp failed for PHI skip %s: %s", message_id, exc)
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

    for decision in decisions:
        if decision.get("action") != "file":
            log.debug(
                "Skip %r: %s",
                decision.get("filename"), decision.get("reason", ""),
            )
            continue

        entity = decision.get("entity", "").upper()
        subfolder = decision.get("subfolder", "").lower()
        orig_filename = decision.get("filename", "")

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

        # Download
        try:
            content = download_attachment(user_email, message_id, att)
        except GmailReaderError as exc:
            log.warning("Download failed for %r: %s", orig_filename, exc)
            continue

        # Ensure folder exists in Drive
        try:
            folder_id = ensure_folder_path(drive_path_segments)
        except DriveConnectorError as exc:
            log.warning("Folder creation failed for %r: %s", drive_path_display, exc)
            continue

        # Upload
        try:
            file_id, web_link = upload_file(
                folder_id, canonical, content, att["mime_type"]
            )
        except DriveConnectorError as exc:
            log.warning("Upload failed for %r: %s", canonical, exc)
            continue

        log.info("Filed %r -> %s (%s)", orig_filename, drive_path_display, web_link)

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

    # Register RFC Message-ID in cross-account dedup store after successful filing
    if results and rfc_msg_id and filed_ids is not None and not dry_run:
        filed_ids[rfc_msg_id] = int(time.time())

    # Label the message as processed regardless of how many were filed
    # (so we don't re-examine it if it runs again within the watermark window)
    if not dry_run:
        try:
            apply_label(user_email, message_id, label_id)
        except GmailReaderError as exc:
            log.warning("Failed to apply Cora-Filed label to %s: %s", message_id, exc)

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
    filed_ids: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Run the full filer pipeline for one monitored email account.

    Returns a summary dict:
        {email, messages_scanned, filed, skipped, errors}
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
    }

    try:
        label_id = ensure_cora_label(user_email)
    except GmailReaderError as exc:
        log.error("Cannot get Cora-Filed label for %s: %s", user_email, exc)
        summary["errors"] += 1
        return summary

    try:
        message_ids = list_messages_with_attachments(user_email, since_ts)
    except GmailReaderError as exc:
        log.error("Cannot list messages for %s: %s", user_email, exc)
        summary["errors"] += 1
        return summary

    summary["messages_scanned"] = len(message_ids)
    log.info("%s: %d messages with attachments to examine", user_email, len(message_ids))

    for msg_id in message_ids:
        try:
            filed = process_email(
                user_email, msg_id, label_id,
                dry_run=dry_run, kb=kb,
                entity_hint=entity_hint, filed_ids=filed_ids,
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
    filed_ids = _load_filed_ids()
    run_start = int(time.time())
    summaries: list[dict[str, Any]] = []

    for account in accounts:
        summary = process_account(
            account, watermarks, dry_run=dry_run, kb=kb, filed_ids=filed_ids,
        )
        summaries.append(summary)

        # Advance watermark for this account if no errors
        if not dry_run and summary["errors"] == 0:
            watermarks[account["email"]] = run_start
            log.info(
                "Watermark advanced for %s -> %d", account["email"], run_start
            )

    if not dry_run:
        _save_watermarks(watermarks)
        _save_filed_ids(filed_ids)

    return summaries


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
