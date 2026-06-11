"""Tier 2-Finance — receipt/invoice retrieval for the finance role.

Spec of record: 2026-06-09 per-user email/Drive access spec (Tier 2-Finance
section). A scoped exception layered on Tier 2: a CONTENT-TYPE permission,
not a mailbox permission — the finance team gets *financial documents* from
any inbox, never the private/business emails themselves.

Hard rules (all enforced deterministically, pre-LLM, per D-034):
  - WHO:   exactly the Slack IDs in data/maps/finance-receipt-allowlist.yaml
           (Justin Moran, Eric Canku, Jerry Reick). Fail-closed.
  - WHERE: the #hjr-finance channel ONLY (channel_id pinned in the same
           config). Outside it, these users are normal Tier-2 users.
  - WHAT:  chunks tagged metadata.financial_document=true only. A
           non-financial retrieval request on this path is refused.
  - PHI:   Lexington client PHI excluded (ingest guards + a defensive
           pattern filter on every grant).
  - AUDIT: every cross-mailbox pull -> logs/finance-access-audit.jsonl.

Workflow (PROACTIVE — all three, per Harrison):
  1. On-demand retrieval in #hjr-finance (handled via app._dispatch_qa grant).
  2. Auto-file: retrieved + proactively-detected receipts/invoices are copied
     into the "Receipts & Invoices Inbox" Drive folder.
  3. Weekly digest (scripts/run_finance_receipt_digest.py, task
     `cowork-cora-finance-receipt-digest`): scan all monitored inboxes for
     newly-detected financial documents since the last watermark, file them,
     and post a digest to #hjr-finance. Watermark + dedup ledger guarantee
     each receipt surfaces once.

Google/Slack imports are deliberately LAZY (inside functions) so the guard
path (check_request) stays import-light for app.py and tests.
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

import yaml

from . import historical_access
from .finance_doc_classifier import is_financial_document
from .historical_access import AccessDecision, PASS
from .phi_guard import is_phi_risk

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ALLOWLIST_PATH = _REPO_ROOT / "data" / "maps" / "finance-receipt-allowlist.yaml"
_AUDIT_LOG_PATH = _REPO_ROOT / "logs" / "finance-access-audit.jsonl"
_WATERMARKS_PATH = _REPO_ROOT / "data" / "cache" / "finance-receipt-watermarks.json"
_FILED_IDS_PATH = _REPO_ROOT / "data" / "cache" / "finance-receipt-filed-ids.json"
_ACCOUNTS_PATH = _REPO_ROOT / "data" / "maps" / "monitored-email-accounts.yaml"

_CACHE_TTL_S = 60
_DEFAULT_LOOKBACK_DAYS = 7
_MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
_ONDEMAND_FILE_CAP = 5

# Financial-document nouns for the on-demand intent ("pull receipts from ...").
_FIN_NOUN_RE = re.compile(
    r"\b(?:receipts?|invoices?|statements?|bills?|order\s+confirmations?|"
    r"payment\s+(?:confirmations?|records?)|purchase\s+orders?)\b",
    re.IGNORECASE,
)
_AMOUNT_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d{2})?")

_REFUSE_NOT_ALLOWLISTED = (
    "Receipt and invoice retrieval in this channel is limited to the finance "
    "team allowlist. For your own email or files, DM me directly."
)
_REFUSE_NON_FINANCIAL = (
    "In this channel I can only retrieve financial documents — receipts, "
    "invoices, statements, order confirmations. For your own email or Drive "
    "files, DM me directly."
)


class FinanceReceiptsError(Exception):
    pass


# ────────────────────────────────────────────────────────────────────────────
# Config
# ────────────────────────────────────────────────────────────────────────────

_config_cache: tuple[float, dict] | None = None


def _load_config() -> dict:
    """Allowlist config with 60s TTL; never caches an empty read (fail-closed)."""
    global _config_cache
    now = time.monotonic()
    if _config_cache is not None and now - _config_cache[0] < _CACHE_TTL_S:
        return _config_cache[1]
    cfg: dict = {}
    try:
        raw = yaml.safe_load(_ALLOWLIST_PATH.read_text(encoding="utf-8")) or {}
        cfg = {
            "users": frozenset(str(u).strip() for u in (raw.get("users") or []) if u),
            "channel_id": str(raw.get("channel_id") or "").strip(),
            "channel_name": str(raw.get("channel_name") or "hjr-finance").strip(),
            "drive_folder_id": str(raw.get("drive_folder_id") or "").strip(),
        }
    except Exception as exc:  # noqa: BLE001
        log.error("finance_receipts: allowlist load failed (fail-closed): %s", exc)
        cfg = {"users": frozenset(), "channel_id": "", "channel_name": "", "drive_folder_id": ""}
    if cfg.get("users") and cfg.get("channel_id"):
        _config_cache = (now, cfg)
    return cfg


def invalidate_cache() -> None:
    """Test hook."""
    global _config_cache
    _config_cache = None


# ────────────────────────────────────────────────────────────────────────────
# Pre-LLM request gate
# ────────────────────────────────────────────────────────────────────────────


def check_request(slack_user_id: str, channel_id: str, text: str) -> AccessDecision:
    """Deterministic Tier 2-Finance gate. Only ever acts inside #hjr-finance.

    Returns PASS everywhere else — the normal Tier-2 rules then apply, so the
    finance power literally does not exist outside the pinned channel.
    """
    cfg = _load_config()
    if not channel_id or channel_id != cfg.get("channel_id"):
        return PASS

    is_financial_ask = bool(_FIN_NOUN_RE.search(text or ""))
    is_retrieval = historical_access.detect_retrieval_intent(text or "")
    # "pull receipts from Hannah" has a financial noun but may miss the
    # email-noun intent patterns — a retrieval verb + financial noun counts
    # as explicit retrieval on this path.
    verb_plus_fin = bool(
        is_financial_ask
        and re.search(historical_access._RETRIEVE_VERBS, text or "", re.IGNORECASE)
    )
    if not (is_retrieval or verb_plus_fin):
        return PASS  # general Q&A in #hjr-finance is untouched

    if slack_user_id not in cfg.get("users", frozenset()):
        return AccessDecision(action="respond", message=_REFUSE_NOT_ALLOWLISTED)

    if not is_financial_ask:
        # Allowlisted, explicit retrieval, but not for financial documents —
        # this path only ever serves financial documents.
        return AccessDecision(action="respond", message=_REFUSE_NON_FINANCIAL)

    # Optional source-person scoping: "receipts from Hannah for May".
    target = historical_access.detect_target_person(text or "", include_from=True)
    owner_emails: frozenset[str] | None = None
    label = "all monitored mailboxes"
    if target is not None:
        label, owner_emails = target
    return AccessDecision(
        action="grant", owner_emails=owner_emails, mode="finance", target_label=label,
    )


def drop_phi(results: list) -> list:
    """Defensive PHI filter (same as historical_access.drop_phi)."""
    return historical_access.drop_phi(results)


def format_finance_chunks(results: list, target_label: str, filed_links: dict[str, str]) -> str:
    """Render financial-document chunks as LLM context, with Drive links for
    any items auto-filed into the Receipts & Invoices Inbox."""
    if not results:
        return (
            "# Retrieved financial documents\n\n"
            f"No matching financial documents were found across {target_label} "
            "for this request. Say so plainly — do not invent items."
        )
    lines = [
        "# Retrieved financial documents (finance-team authorized)",
        "",
        f"(Source scope: {target_label}. Present the relevant items as a short "
        "list — vendor/sender, what it is, amount if visible, date, source "
        "mailbox. Include the 'Filed:' Drive link when present; never "
        "fabricate links.)",
        "",
    ]
    for i, r in enumerate(results, 1):
        owner = historical_access.chunk_owner_email(r) or "unknown"
        date_str = ""
        if r.date_modified:
            try:
                date_str = datetime.fromtimestamp(
                    r.date_modified, tz=timezone.utc
                ).strftime("%Y-%m-%d")
            except (OSError, ValueError, OverflowError):
                pass
        head = f"## [{i}] {r.title or r.source_id} | {date_str} | mailbox: {owner}"
        if getattr(r, "author", ""):
            head += f" | from: {r.author}"
        filed = filed_links.get(r.source_id)
        lines.extend([head, ""])
        if filed:
            lines.append(f"Filed: {filed}")
        lines.extend([(r.content or "").strip(), ""])
    return "\n".join(lines)


def audit(
    requester: str,
    query: str,
    owner_emails: frozenset[str] | None,
    items: list,
    channel: str = "",
    mode: str = "on_demand",
) -> None:
    """Append a finance-access record. EVERY cross-mailbox pull is logged."""
    try:
        _AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        source_mailboxes = sorted(
            {historical_access.chunk_owner_email(r) or "unknown" for r in items}
        ) if items and not isinstance(items[0], str) else []
        entry = {
            "ts": int(time.time()),
            "requester": requester,
            "channel": channel,
            "mode": mode,
            "query": (query or "")[:500],
            "scope": sorted(owner_emails) if owner_emails else "ANY",
            "source_mailboxes": source_mailboxes,
            "items": [
                getattr(r, "source_id", str(r)) for r in items
            ][:50],
        }
        with _AUDIT_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception as exc:  # noqa: BLE001 — audit failure must not break replies
        log.error("finance_receipts: audit write failed: %s", exc)


# ────────────────────────────────────────────────────────────────────────────
# Auto-file (shared by on-demand retrieval + the weekly digest)
# ────────────────────────────────────────────────────────────────────────────

_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._ \-]")


def _canonical_filename(date_ts: int, mailbox: str, original: str) -> str:
    day = datetime.fromtimestamp(date_ts or time.time(), tz=timezone.utc).strftime("%Y-%m-%d")
    local = (mailbox.split("@", 1)[0] or "unknown")[:24]
    safe = _UNSAFE_RE.sub("-", original).strip() or "attachment"
    return f"{day}_{local}_{safe}"


def _load_filed_ids() -> dict[str, Any]:
    if _FILED_IDS_PATH.exists():
        try:
            return json.loads(_FILED_IDS_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _save_filed_ids(filed: dict[str, Any]) -> None:
    _FILED_IDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _FILED_IDS_PATH.write_text(json.dumps(filed, indent=2), encoding="utf-8")


def file_message_attachments(
    mailbox: str, message_id: str, dry_run: bool = False
) -> list[dict[str, str]]:
    """Download a message's attachments and file them into the Receipts &
    Invoices Inbox Drive folder. Dedup-ledgered so each message files once.

    Returns [{"filename", "link"}] for everything filed (or already filed —
    the existing link is returned thanks to upload_file's name-dedup).
    """
    cfg = _load_config()
    folder_id = cfg.get("drive_folder_id")
    if not folder_id:
        log.warning("finance_receipts: no drive_folder_id configured — skipping auto-file")
        return []

    ledger_key = f"{mailbox}:{message_id}"
    filed_ids = _load_filed_ids()

    from .connectors.gmail_reader import (  # lazy — Google deps
        GmailReaderError, download_attachment, get_message, parse_message_metadata,
    )
    from .connectors.drive_connector import DriveConnectorError, upload_file  # lazy

    try:
        meta = parse_message_metadata(get_message(mailbox, message_id))
    except GmailReaderError as exc:
        log.warning("finance_receipts: get_message failed %s/%s: %s", mailbox, message_id, exc)
        return []

    out: list[dict[str, str]] = []
    for att in meta.get("attachments", []):
        filename = att.get("filename") or "attachment"
        size = att.get("size") or 0
        if size > _MAX_ATTACHMENT_BYTES:
            log.info("finance_receipts: skipping oversized attachment %s (%d bytes)", filename, size)
            continue
        canonical = _canonical_filename(meta.get("date_ts", 0), mailbox, filename)
        if dry_run:
            out.append({"filename": canonical, "link": "(dry-run)"})
            continue
        try:
            content = download_attachment(mailbox, message_id, att)
        except GmailReaderError as exc:
            log.warning("finance_receipts: download failed %s: %s", filename, exc)
            continue
        try:
            _file_id, link = upload_file(
                folder_id, canonical, content, att.get("mime_type") or "application/octet-stream",
            )
        except DriveConnectorError as exc:
            log.warning("finance_receipts: upload failed %s: %s", canonical, exc)
            continue
        out.append({"filename": canonical, "link": link})

    if out and not dry_run:
        filed_ids[ledger_key] = int(time.time())
        _save_filed_ids(filed_ids)
    return out


def auto_file_results(results: list, cap: int = _ONDEMAND_FILE_CAP) -> dict[str, str]:
    """Best-effort auto-file for on-demand retrieval results.

    For up to `cap` gmail results, files the message's attachments and returns
    {source_id: drive_link}. Never raises — filing is an upgrade, not a gate.
    """
    links: dict[str, str] = {}
    filed_ids = _load_filed_ids()
    count = 0
    for r in results:
        if count >= cap:
            break
        meta = getattr(r, "metadata", None) or {}
        mailbox = str(meta.get("user_email") or "")
        message_id = str(meta.get("message_id") or "")
        if not mailbox or not message_id or r.source != "gmail":
            continue
        if f"{mailbox}:{message_id}" in filed_ids:
            continue  # already filed by a prior pull / digest
        count += 1
        try:
            filed = file_message_attachments(mailbox, message_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("finance_receipts: auto-file error for %s: %s", r.source_id, exc)
            continue
        if filed:
            links[r.source_id] = filed[0]["link"]
    return links


# ────────────────────────────────────────────────────────────────────────────
# Weekly proactive digest
# ────────────────────────────────────────────────────────────────────────────


def _load_watermarks() -> dict[str, int]:
    if _WATERMARKS_PATH.exists():
        try:
            return json.loads(_WATERMARKS_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _save_watermarks(marks: dict[str, int]) -> None:
    _WATERMARKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _WATERMARKS_PATH.write_text(json.dumps(marks, indent=2), encoding="utf-8")


def _digest_accounts() -> list[str]:
    """All enabled, DWD-eligible monitored mailboxes ('all inboxes' per spec)."""
    try:
        raw = yaml.safe_load(_ACCOUNTS_PATH.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        log.error("finance_receipts: accounts load failed: %s", exc)
        return []
    out: list[str] = []
    for acct in raw.get("accounts", []) or []:
        if acct.get("enabled") and acct.get("dwd_eligible"):
            email = str(acct.get("email") or "").strip().lower()
            if email and email not in out:
                out.append(email)
    return out


def parse_amount(*texts: str) -> str:
    """Best-effort first $-amount across the given texts ('' when none)."""
    for t in texts:
        m = _AMOUNT_RE.search(t or "")
        if m:
            return m.group(0).replace(" ", "")
    return ""


def run_digest(dry_run: bool = False, lookback_days: int = _DEFAULT_LOOKBACK_DAYS) -> dict:
    """Scan all inboxes for new financial documents, file them, build a digest.

    Per-account ATOMIC watermark (D-038): each account's watermark advances as
    soon as that account finishes, so a mid-run kill never re-surfaces items.
    Dedup ledger (finance-receipt-filed-ids.json) guarantees once-only filing
    even across watermark resets.

    Returns {"rows": [...], "accounts_scanned": int, "errors": int} — the
    caller (scripts/run_finance_receipt_digest.py) posts the Slack digest.
    """
    from .connectors.gmail_reader import (  # lazy — Google deps
        GmailReaderError, get_message, list_messages_with_attachments,
        parse_message_metadata,
    )

    marks = _load_watermarks()
    filed_ids = _load_filed_ids()
    sync_start = int(time.time())
    default_since = sync_start - lookback_days * 86400

    rows: list[dict[str, str]] = []
    errors = 0
    accounts = _digest_accounts()

    for mailbox in accounts:
        since = int(marks.get(mailbox) or default_since)
        try:
            msg_ids = list_messages_with_attachments(mailbox, since, max_results=200)
        except GmailReaderError as exc:
            log.warning("finance_receipts digest: skipping %s: %s", mailbox, exc)
            errors += 1
            continue

        for message_id in msg_ids:
            ledger_key = f"{mailbox}:{message_id}"
            if ledger_key in filed_ids:
                continue
            try:
                meta = parse_message_metadata(get_message(mailbox, message_id))
            except GmailReaderError as exc:
                log.warning("finance_receipts digest: get %s failed: %s", message_id, exc)
                errors += 1
                continue

            subject = meta.get("subject", "")
            sender = meta.get("from", "")
            snippet = meta.get("snippet", "")
            filenames = tuple(
                a.get("filename", "") for a in meta.get("attachments", [])
            )

            # PHI guard: a Lexington client-record email never reaches the
            # finance channel, even if it carries an invoice-looking name.
            if is_phi_risk(f"{subject}\n{snippet}"):
                continue
            if not is_financial_document(
                subject, snippet, sender, attachment_names=filenames
            ):
                continue

            try:
                filed = file_message_attachments(mailbox, message_id, dry_run=dry_run)
            except Exception as exc:  # noqa: BLE001
                log.warning("finance_receipts digest: file failed %s: %s", message_id, exc)
                errors += 1
                continue
            if not filed:
                continue

            rows.append({
                "vendor": sender,
                "subject": subject,
                "amount": parse_amount(subject, snippet),
                "date": datetime.fromtimestamp(
                    meta.get("date_ts", sync_start), tz=timezone.utc
                ).strftime("%Y-%m-%d"),
                "mailbox": mailbox,
                "link": filed[0]["link"],
            })

        if not dry_run:
            marks[mailbox] = sync_start
            _save_watermarks(marks)  # atomic per account (D-038)

    if rows and not dry_run:
        audit(
            requester="system:finance-receipt-digest",
            query=f"weekly digest scan (lookback {lookback_days}d)",
            owner_emails=None,
            items=[f"{r['mailbox']}:{r['subject'][:80]}" for r in rows],
            mode="digest",
        )
    return {"rows": rows, "accounts_scanned": len(accounts), "errors": errors}


def format_digest(rows: list[dict[str, str]], accounts_scanned: int) -> str:
    """Slack mrkdwn digest for #hjr-finance."""
    if not rows:
        return (
            ":receipt: *Weekly receipts digest* — no new receipts or invoices "
            f"detected across {accounts_scanned} monitored inboxes this week."
        )
    lines = [
        f":receipt: *Weekly receipts digest* — {len(rows)} new financial "
        f"document(s) across {accounts_scanned} monitored inboxes, filed to "
        "the Receipts & Invoices Inbox:",
        "",
    ]
    for r in rows[:40]:
        amount = f" — {r['amount']}" if r["amount"] else ""
        link = f" — <{r['link']}|filed copy>" if r.get("link") and r["link"] != "(dry-run)" else ""
        lines.append(
            f"• *{r['subject'][:90]}*{amount} — {r['vendor'][:60]} — "
            f"{r['date']} — `{r['mailbox']}`{link}"
        )
    if len(rows) > 40:
        lines.append(f"…and {len(rows) - 40} more (see the Drive folder).")
    return "\n".join(lines)


def post_digest_to_slack(text: str) -> bool:
    """Post the digest to the pinned finance channel. Returns True on success."""
    cfg = _load_config()
    channel = cfg.get("channel_id") or cfg.get("channel_name")
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token or not channel:
        log.error("finance_receipts: missing SLACK_BOT_TOKEN or channel — digest not posted")
        return False
    try:
        from slack_sdk import WebClient  # lazy
        WebClient(token=token).chat_postMessage(
            channel=channel, text=text, unfurl_links=False, unfurl_media=False,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("finance_receipts: digest post failed: %s", exc)
        return False
