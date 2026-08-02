"""Tier-1 send executor (R2): the ONLY module in the codebase that may call
the Gmail send API. `tests/test_no_raw_gmail_send.py` enforces this as a CI
guard (the test_no_raw_slack_post pattern).

`send_stashed()` is NOT a general send tool and is NOT exposed to the LLM.
It fires only from the approve-card handler / typed-SEND fallback in app.py,
takes a stash id (never message text), and enforces, in order:

  1. CORA_SEND_LIVE=tier1        (env off beats everything, incl. approval)
  2. stash exists, status=staged, unexpired (48h)
  3. approver is on the playbook's approver list (Harrison-only in v1)
  4. playbook still resolves to tier 1 under current config
  5. mailbox on the playbook allowlist AND the code-level v1 universe
  6. byte-exactness: sha256(body) == the hash pinned at stage time
  7. egress guard re-run on the stashed bytes (config may have changed)
  8. reply-only threading: live thread fetch; recipients must be a subset of
     the thread's actual participants (new-recipient sends are structurally
     impossible); In-Reply-To/References set from the last message
  9. atomic claim (staged -> sending) so a double-tap can never double-send

Audit: every attempt (sent or refused at step >= 6) appends to
logs/cora-send-audit.jsonl -- approver, mailbox, thread, playbook, guard
results, body sha256. NO body content, ever.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass, field
from email.mime.text import MIMEText
from email.utils import getaddresses
from pathlib import Path
from typing import Any, Optional

from . import email_egress_guard, ledger, send_trust, stash

logger = logging.getLogger("cora.revops.sender")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_AUDIT_PATH = _REPO_ROOT / "logs" / "cora-send-audit.jsonl"


@dataclass
class ThreadContext:
    participants: set[str] = field(default_factory=set)
    last_rfc_message_id: Optional[str] = None
    references: Optional[str] = None
    subject: Optional[str] = None


def _extract_header(headers: list[dict[str, str]], name: str) -> Optional[str]:
    for h in headers or []:
        if (h.get("name") or "").lower() == name.lower():
            return h.get("value")
    return None


def fetch_thread_context(mailbox: str, gmail_thread_id: str) -> ThreadContext:
    """Read live thread metadata (participants + threading headers).

    Uses the gmail_reader DWD client (read side). Raises on any failure --
    callers treat that as a refusal (fail-closed), never as 'skip the check'.
    """
    from ..connectors import gmail_reader  # lazy: googleapiclient import cost

    service = gmail_reader._build_service(mailbox)
    thread = (
        service.users()
        .threads()
        .get(
            userId="me",
            id=gmail_thread_id,
            format="metadata",
            metadataHeaders=["Message-ID", "From", "To", "Cc", "Subject", "References"],
        )
        .execute()
    )
    ctx = ThreadContext()
    messages = thread.get("messages") or []
    for msg in messages:
        headers = (msg.get("payload") or {}).get("headers") or []
        raw_addrs: list[str] = []
        for hname in ("From", "To", "Cc"):
            val = _extract_header(headers, hname)
            if val:
                raw_addrs.append(val)
        for _, addr in getaddresses(raw_addrs):
            if addr and "@" in addr:
                ctx.participants.add(addr.strip().lower())
        if ctx.subject is None:
            subj = _extract_header(headers, "Subject")
            if subj:
                ctx.subject = subj
    if messages:
        last_headers = (messages[-1].get("payload") or {}).get("headers") or []
        ctx.last_rfc_message_id = _extract_header(last_headers, "Message-ID")
        prior_refs = _extract_header(last_headers, "References") or ""
        if ctx.last_rfc_message_id:
            ctx.references = (prior_refs + " " + ctx.last_rfc_message_id).strip()
        last_subj = _extract_header(last_headers, "Subject")
        if last_subj:
            ctx.subject = last_subj
    return ctx


def _build_reply_mime(
    *,
    to: list[str],
    cc: list[str],
    subject: str,
    body_text: str,
    in_reply_to: Optional[str],
    references: Optional[str],
) -> str:
    msg = MIMEText(body_text, "plain", "utf-8")
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    subj = subject or ""
    if subj and not subj.lower().startswith("re:"):
        subj = "Re: " + subj
    msg["Subject"] = subj
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


def _gmail_send_raw(mailbox: str, raw_b64: str, gmail_thread_id: str) -> dict[str, Any]:
    """THE sanctioned Gmail send call site. Do not add another anywhere.

    gmail.compose (already on the DWD grant) authorizes send; the ladder in
    send_stashed() is the safety, per the 2026-08-01 locked design.
    """
    from ..tools import gmail_client  # lazy: googleapiclient import cost

    service = gmail_client._build_service(mailbox)
    return (
        service.users()
        .messages()
        .send(userId="me", body={"raw": raw_b64, "threadId": gmail_thread_id})
        .execute()
    )


def create_reply_draft(
    mailbox: str,
    *,
    gmail_thread_id: str,
    to: list[str],
    cc: Optional[list[str]] = None,
    body_text: str,
    subject: Optional[str] = None,
) -> dict[str, Any]:
    """Tier-0 path: create a THREADED reply draft (a human clicks Send).

    Uses drafts().create -- creating a draft is not a send; the CI guard
    only polices messages().send / drafts().send.
    """
    from ..tools import gmail_client  # lazy

    ctx = fetch_thread_context(mailbox, gmail_thread_id)
    raw = _build_reply_mime(
        to=to,
        cc=list(cc or []),
        subject=subject or ctx.subject or "",
        body_text=body_text,
        in_reply_to=ctx.last_rfc_message_id,
        references=ctx.references,
    )
    service = gmail_client._build_service(mailbox)
    return (
        service.users()
        .drafts()
        .create(
            userId="me",
            body={"message": {"raw": raw, "threadId": gmail_thread_id}},
        )
        .execute()
    )


def _audit(record: dict[str, Any]) -> None:
    try:
        _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), **record}) + "\n")
    except Exception:  # noqa: BLE001 - audit failure must not mask the outcome
        logger.exception("send-audit write failed")


def send_stashed(
    stash_id: str,
    *,
    approver_id: str,
    conn=None,
) -> tuple[str, str]:
    """Execute an approved Tier-1 send. Returns (outcome, human_message).

    Outcomes: env_off | not_found | already_resolved | expired |
    not_authorized | tier_denied | mailbox_denied | hash_mismatch |
    guard_blocked | thread_verify_failed | recipient_violation |
    send_failed | sent
    """
    own = conn is None
    if own:
        conn = ledger.connect()
    try:
        return _send_stashed_inner(conn, stash_id, approver_id)
    finally:
        if own:
            conn.close()


def _send_stashed_inner(conn, stash_id: str, approver_id: str) -> tuple[str, str]:
    # 1. Kill switch FIRST: off beats everything, including an approved card.
    if send_trust.send_live_mode() != "tier1":
        return (
            "env_off",
            "CORA_SEND_LIVE is off; nothing was sent. The stash stays staged "
            "until it expires. Flip the env + restart to enable Tier-1 sends.",
        )

    row = stash.get_stash(conn, stash_id)
    if row is None:
        return ("not_found", "No such send stash; nothing was sent.")
    if row["status"] != "staged":
        return (
            "already_resolved",
            f"This card is already {row['status']}; nothing more was sent.",
        )
    if stash.is_expired(row):
        stash.expire_stale(conn)
        ledger.add_event(
            conn,
            row["thread_key"],
            event_type="card_expired",
            actor="system",
            source="send_gate",
            detail={"stash_id": stash_id},
        )
        return ("expired", "This send card expired (48h); nothing was sent.")

    playbook_id = row["playbook_id"]
    if not send_trust.is_approver(playbook_id, approver_id):
        return ("not_authorized", "Only the configured approver can fire a send.")
    if send_trust.effective_tier(playbook_id) != 1:
        return (
            "tier_denied",
            "This playbook no longer resolves to Tier 1 under current config; "
            "nothing was sent.",
        )
    mailbox = row["mailbox"]
    if not send_trust.mailbox_allowed(playbook_id, mailbox):
        return ("mailbox_denied", f"Mailbox {mailbox} is not send-allowlisted.")

    body_text = row["body_text"] or ""
    if stash.sha256_text(body_text) != row["body_sha256"]:
        _audit(
            {
                "outcome": "hash_mismatch",
                "stash_id": stash_id,
                "approver": approver_id,
                "mailbox": mailbox,
                "thread_key": row["thread_key"],
                "playbook": playbook_id,
                "sha256": row["body_sha256"],
            }
        )
        return (
            "hash_mismatch",
            "Stash bytes no longer match the hash pinned at stage time; refusing.",
        )

    thread_row = ledger.get_thread(conn, row["thread_key"])
    workstream = thread_row["workstream"] if thread_row else None
    entity = thread_row["entity"] if thread_row else None

    # 7. Guard re-run at send time on the exact stashed bytes.
    guard = email_egress_guard.check_email(
        body_text, workstream=workstream, entity=entity
    )
    if not guard.ok:
        stash.cancel_stash(conn, stash_id)
        ledger.add_event(
            conn,
            row["thread_key"],
            event_type="guard_block",
            actor=approver_id,
            source="send_gate",
            detail={"stash_id": stash_id, "guard": guard.to_dict(), "at": "send"},
        )
        _audit(
            {
                "outcome": "guard_blocked",
                "stash_id": stash_id,
                "approver": approver_id,
                "mailbox": mailbox,
                "thread_key": row["thread_key"],
                "playbook": playbook_id,
                "guard": guard.to_dict(),
                "sha256": row["body_sha256"],
            }
        )
        return ("guard_blocked", f"Egress guard blocked the send: {guard.summary()}")

    # 8. Reply-only threading + recipient-subset against the LIVE thread.
    recipients = [r for r in json.loads(row["recipients"] or "[]") if r]
    cc = [c for c in json.loads(row["cc"] or "[]") if c]
    if not recipients:
        return ("recipient_violation", "Stash has no recipients; refusing.")
    try:
        ctx = fetch_thread_context(mailbox, row["gmail_thread_id"])
    except Exception:  # noqa: BLE001 - fail closed
        logger.exception("live thread verify failed for stash %s", stash_id)
        return (
            "thread_verify_failed",
            "Could not verify the live thread; nothing was sent (fail-closed).",
        )
    outsiders = [
        a for a in (*recipients, *cc) if a.strip().lower() not in ctx.participants
    ]
    if outsiders:
        _audit(
            {
                "outcome": "recipient_violation",
                "stash_id": stash_id,
                "approver": approver_id,
                "mailbox": mailbox,
                "thread_key": row["thread_key"],
                "playbook": playbook_id,
                "outsiders": outsiders,
                "sha256": row["body_sha256"],
            }
        )
        return (
            "recipient_violation",
            "Recipient(s) not on the live thread: "
            + ", ".join(outsiders)
            + ". Tier-1 sends are reply-only to existing participants.",
        )

    # 9. Atomic claim -- double-tap loses the race here.
    if not stash.claim_for_send(conn, stash_id, approved_by=approver_id):
        return ("already_resolved", "This card was already handled; nothing more was sent.")

    raw = _build_reply_mime(
        to=recipients,
        cc=cc,
        subject=row["subject"] or ctx.subject or "",
        body_text=body_text,
        in_reply_to=ctx.last_rfc_message_id,
        references=ctx.references,
    )
    try:
        resp = _gmail_send_raw(mailbox, raw, row["gmail_thread_id"])
    except Exception as exc:  # noqa: BLE001
        logger.exception("gmail send failed for stash %s", stash_id)
        stash.mark_send_failed(conn, stash_id)
        _audit(
            {
                "outcome": "send_failed",
                "stash_id": stash_id,
                "approver": approver_id,
                "mailbox": mailbox,
                "thread_key": row["thread_key"],
                "playbook": playbook_id,
                "sha256": row["body_sha256"],
                "error": str(exc)[:200],
            }
        )
        return ("send_failed", "Gmail send failed; the stash is closed, nothing sent.")

    stash.finalize_sent(conn, stash_id)
    message_id = (resp or {}).get("id")
    ledger.record_nudge_sent(
        conn,
        row["thread_key"],
        actor=approver_id,
        detail={
            "stash_id": stash_id,
            "gmail_message_id": message_id,
            "sha256": row["body_sha256"],
            "playbook": playbook_id,
        },
    )
    _audit(
        {
            "outcome": "sent",
            "stash_id": stash_id,
            "approver": approver_id,
            "mailbox": mailbox,
            "thread_key": row["thread_key"],
            "gmail_thread_id": row["gmail_thread_id"],
            "gmail_message_id": message_id,
            "playbook": playbook_id,
            "guard": guard.to_dict(),
            "sha256": row["body_sha256"],
            "recipients": recipients,
            "cc": cc,
        }
    )
    return (
        "sent",
        f"Sent. Reply went to {', '.join(recipients)} on the existing thread; "
        f"audit logged (sha256 {row['body_sha256'][:12]}...).",
    )
