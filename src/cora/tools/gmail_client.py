"""Gmail API v1 client — draft creation via Service Account + Domain-wide Delegation.

Phase 4 write tools — draft creation only (no send). Reverses 2026-05-18
read-only doctrine per Harrison decision 2026-05-21. Mirrors the staged-write
pattern used by asana_create_task: Cora drafts to the asker's own Drafts
folder, the asker reviews and sends from Gmail themselves.

Architecture:
- Same service account as calendar / drive (cora-calendar-sa).
- Required DWD scope: https://www.googleapis.com/auth/gmail.compose
  (compose-only — does NOT include send, modify, or delete; minimum-privilege).
- Service account impersonates the asker's Google identity so the draft
  appears in their own Drafts folder, NOT a shared mailbox.

Write doctrine (LOCKED 2026-05-21):
- No send capability. Only `create_draft`. The user must open Gmail and
  send manually. This guarantees a human-in-the-loop checkpoint.
- All drafts are audit-logged at INFO with sender / recipient count /
  subject / draft ID. Body content is NOT logged (PHI / privacy).
- Plain-text body only in v1. HTML + attachments are future enhancements.

Deferred for follow-up:
- Reply-to-thread (would require threadId from Gmail search — separate read
  capability).
- Attachments (need a Drive-to-Gmail handoff).
- Send capability (deliberate omission — drafts are the staged-write artifact).
"""

import base64
import logging
import os
from email.mime.text import MIMEText
from email.utils import getaddresses
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

log = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/gmail.compose"]


class GmailClientError(Exception):
    """Raised when a Gmail API call fails."""


def _service_account_path() -> str:
    val = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not val:
        raise GmailClientError(
            "GOOGLE_SERVICE_ACCOUNT_JSON not set in environment — Gmail tool-use disabled"
        )
    if not os.path.exists(val):
        raise GmailClientError(
            f"GOOGLE_SERVICE_ACCOUNT_JSON path does not exist: {val}"
        )
    return val


def _build_service(user_email: str):
    """Build a Gmail service that impersonates user_email via Domain-wide Delegation."""
    try:
        creds = service_account.Credentials.from_service_account_file(
            _service_account_path(),
            scopes=_SCOPES,
        )
    except Exception as exc:
        raise GmailClientError(
            f"Failed to load service account credentials: {exc}"
        ) from exc

    delegated = creds.with_subject(user_email)
    return build("gmail", "v1", credentials=delegated, cache_discovery=False)


def _normalize_recipients(value: Any) -> list[str]:
    """Accept a string (comma-separated) or list and return a clean list of email addresses.

    Light validation: each entry must look like an email (have @ in it). Strips whitespace.
    Returns empty list if value is None or empty.
    """
    if not value:
        return []
    if isinstance(value, str):
        raw = value
    elif isinstance(value, list):
        raw = ",".join(str(x) for x in value)
    else:
        raise GmailClientError(f"Unsupported recipients type: {type(value).__name__}")

    # email.utils.getaddresses handles "Name <email@x.com>" and bare emails alike
    parsed = getaddresses([raw])
    emails: list[str] = []
    for _name, addr in parsed:
        addr = (addr or "").strip()
        if not addr:
            continue
        if "@" not in addr:
            raise GmailClientError(
                f"Recipient {addr!r} doesn't look like an email address. Use the full address (e.g. name@domain.com)."
            )
        emails.append(addr)
    return emails


def _build_mime_message(
    *,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    sender: str,
) -> str:
    """Build an RFC 2822 message and base64url-encode for the Gmail API."""
    msg = MIMEText(body, "plain", "utf-8")
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    msg["Subject"] = subject
    msg["From"] = sender

    raw_bytes = msg.as_bytes()
    return base64.urlsafe_b64encode(raw_bytes).decode("utf-8")


def create_draft(
    *,
    sender_email: str,
    to: Any,
    subject: str,
    body: str,
    cc: Any = None,
    bcc: Any = None,
) -> dict[str, Any]:
    """Create a Gmail draft in `sender_email`'s Drafts folder.

    Returns the created draft dict (includes `id` and the embedded `message`).
    The draft will appear in the user's Gmail Drafts folder where they can
    edit and send manually.

    Raises GmailClientError on auth / network / API failure or invalid input.
    """
    if not sender_email or "@" not in sender_email:
        raise GmailClientError(f"sender_email must be a valid email, got {sender_email!r}")
    if not subject or not subject.strip():
        raise GmailClientError("create_draft requires a non-empty subject")
    if not body or not body.strip():
        raise GmailClientError("create_draft requires a non-empty body")

    to_list = _normalize_recipients(to)
    if not to_list:
        raise GmailClientError("create_draft requires at least one recipient in `to`")
    cc_list = _normalize_recipients(cc)
    bcc_list = _normalize_recipients(bcc)

    raw = _build_mime_message(
        to=to_list,
        subject=subject.strip(),
        body=body,
        cc=cc_list or None,
        bcc=bcc_list or None,
        sender=sender_email,
    )

    try:
        service = _build_service(sender_email)
        draft = (
            service.users()
            .drafts()
            .create(userId="me", body={"message": {"raw": raw}})
            .execute()
        )
    except HttpError as exc:
        status = exc.resp.status if exc.resp else "?"
        if status == 403:
            raise GmailClientError(
                f"Gmail 403 for {sender_email} — service account lacks gmail.compose "
                f"delegation for this user's domain. Harrison may need to add the scope "
                f"to Domain-wide Delegation in admin.google.com."
            ) from exc
        if status == 401:
            raise GmailClientError(
                f"Gmail 401 for {sender_email} — service account credentials rejected. "
                f"Check that the SA JSON is valid and DWD is configured."
            ) from exc
        if status == 400:
            raise GmailClientError(
                f"Gmail 400 — Gmail rejected the draft (bad MIME or invalid recipient): "
                f"{exc}"
            ) from exc
        raise GmailClientError(f"Gmail API HTTP {status}: {exc}") from exc
    except Exception as exc:
        raise GmailClientError(f"Gmail API error: {exc}") from exc

    return draft


def gmail_drafts_url() -> str:
    """The Gmail Drafts folder URL — user clicks to see / edit / send the draft."""
    return "https://mail.google.com/mail/u/0/#drafts"


def format_created_draft_for_llm(
    draft: dict[str, Any],
    *,
    sender_email: str,
    to: list[str],
    subject: str,
    cc: list[str] | None = None,
) -> str:
    """Render a freshly-created draft as a Slack-mrkdwn confirmation line."""
    draft_id = draft.get("id") or "(no id)"
    to_str = ", ".join(to) if to else "(no recipients)"
    cc_str = f"\n- Cc: {', '.join(cc)}" if cc else ""
    drafts_link = gmail_drafts_url()

    return (
        f"Gmail draft CREATED in {sender_email}'s Drafts folder. Surface this to the user:\n"
        f"- To: {to_str}{cc_str}\n"
        f"- Subject: {subject}\n"
        f"- Draft ID: {draft_id}\n"
        f"- Open in Gmail: <{drafts_link}|Drafts>\n"
        f"\n"
        f"Tell the user the draft is ready to review + send from their Gmail Drafts. "
        f"Format the Drafts link as a Slack hyperlink (preserve the <url|name> syntax)."
    )
