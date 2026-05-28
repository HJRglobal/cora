"""Gmail read client — list messages and download attachments via Service Account + DWD.

Requires the gmail.modify scope on the DWD grant (superset of gmail.readonly; adds
label-write so we can stamp each processed email "Cora-Filed" for idempotency).

Required DWD scope to add in Google Admin → Security → API Controls → Domain-wide
Delegation (in addition to the existing gmail.compose entry):
    https://www.googleapis.com/auth/gmail.modify

Design notes:
- Each user's Gmail is accessed separately — the service account impersonates each
  mailbox owner (DWD). This enables org-wide scanning.
- The "Cora-Filed" label is created lazily on first use per user, with a green color
  so it's visually obvious in Gmail.
- Attachment bytes come back base64url-encoded from the API; we decode them here so
  callers receive plain bytes.
- Large attachments (>= attachment_size threshold in the response) require a separate
  attachments.get call. Small ones include the data inline. Both paths handled here.
"""

from __future__ import annotations

import base64
import email.utils
import logging
import os
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

log = logging.getLogger(__name__)

_GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
_CORA_LABEL_NAME = "Cora-Filed"

# Skip attachments larger than this (25 MB) — avoids downloading huge files
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024


class GmailReaderError(Exception):
    pass


# ────────────────────────────────────────────────────────────────────────────
# Auth
# ────────────────────────────────────────────────────────────────────────────


def _sa_path() -> str:
    val = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not val:
        raise GmailReaderError(
            "GOOGLE_SERVICE_ACCOUNT_JSON not set — Gmail reader disabled"
        )
    if not os.path.exists(val):
        raise GmailReaderError(
            f"GOOGLE_SERVICE_ACCOUNT_JSON path does not exist: {val}"
        )
    return val


def _build_service(user_email: str):
    """Build a Gmail v1 service that impersonates user_email via Domain-wide Delegation."""
    try:
        creds = service_account.Credentials.from_service_account_file(
            _sa_path(), scopes=_GMAIL_SCOPES
        )
    except Exception as exc:
        raise GmailReaderError(
            f"Failed to load service account credentials: {exc}"
        ) from exc

    delegated = creds.with_subject(user_email)
    try:
        return build("gmail", "v1", credentials=delegated, cache_discovery=False)
    except Exception as exc:
        raise GmailReaderError(f"Gmail service build failed: {exc}") from exc


# ────────────────────────────────────────────────────────────────────────────
# Message listing
# ────────────────────────────────────────────────────────────────────────────


def list_messages_with_attachments(
    user_email: str,
    after_ts: int,
    max_results: int = 100,
) -> list[str]:
    """Return message IDs received after after_ts (Unix seconds) that have attachments.

    Gmail's `has:attachment` filter covers any non-inline attachment part.
    The `after:` operator accepts a Unix timestamp.
    """
    service = _build_service(user_email)
    query = f"has:attachment after:{after_ts}"

    ids: list[str] = []
    page_token: str | None = None

    while len(ids) < max_results:
        batch = min(max_results - len(ids), 100)
        kwargs: dict[str, Any] = {
            "userId": "me",
            "q": query,
            "maxResults": batch,
        }
        if page_token:
            kwargs["pageToken"] = page_token

        try:
            resp = service.users().messages().list(**kwargs).execute()
        except HttpError as exc:
            status = exc.resp.status if exc.resp else "?"
            if status == 403:
                raise GmailReaderError(
                    f"Gmail 403 for {user_email} — service account lacks gmail.modify "
                    f"DWD scope. Add it in Google Admin → Security → API Controls."
                ) from exc
            raise GmailReaderError(
                f"Gmail list failed for {user_email} (HTTP {status}): {exc}"
            ) from exc

        for msg in resp.get("messages", []):
            ids.append(msg["id"])

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    log.debug(
        "list_messages_with_attachments(%s, after=%d): found %d messages",
        user_email, after_ts, len(ids),
    )
    return ids[:max_results]


# ────────────────────────────────────────────────────────────────────────────
# Message fetching + parsing
# ────────────────────────────────────────────────────────────────────────────


def get_message(user_email: str, message_id: str) -> dict[str, Any]:
    """Fetch full message including MIME payload."""
    service = _build_service(user_email)
    try:
        return service.users().messages().get(
            userId="me", id=message_id, format="full"
        ).execute()
    except HttpError as exc:
        raise GmailReaderError(
            f"Gmail get message {message_id} failed: {exc}"
        ) from exc


def parse_message_metadata(msg: dict[str, Any]) -> dict[str, Any]:
    """Extract sender, subject, date, snippet and attachment descriptors from a full message.

    Returns::
        {
          "message_id": str,
          "thread_id": str,
          "from": str,
          "to": str,
          "subject": str,
          "date_ts": int,   # Unix seconds
          "snippet": str,
          "labels": list[str],
          "attachments": [
            {
              "filename": str,
              "mime_type": str,
              "size": int,
              "attachment_id": str | None,  # None means data is inline
              "data": str | None,            # base64url inline data (if attachment_id is None)
            }
          ],
        }
    """
    headers = {
        h["name"].lower(): h["value"]
        for h in msg.get("payload", {}).get("headers", [])
    }

    date_str = headers.get("date", "")
    try:
        date_ts = int(email.utils.parsedate_to_datetime(date_str).timestamp())
    except Exception:
        date_ts = int(msg.get("internalDate", "0")) // 1000

    return {
        "message_id": msg["id"],
        "thread_id": msg.get("threadId", ""),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "subject": headers.get("subject", "(no subject)"),
        "date_ts": date_ts,
        "snippet": msg.get("snippet", ""),
        "labels": msg.get("labelIds", []),
        "attachments": _extract_attachment_parts(msg.get("payload", {})),
    }


def _extract_attachment_parts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Recursively collect non-inline attachment descriptors from the MIME tree."""
    results: list[dict[str, Any]] = []

    def walk(part: dict[str, Any]) -> None:
        filename = part.get("filename", "").strip()
        mime = part.get("mimeType", "application/octet-stream")
        body = part.get("body", {})

        part_headers = {
            h["name"].lower(): h["value"]
            for h in part.get("headers", [])
        }
        content_disp = part_headers.get("content-disposition", "")
        is_inline = content_disp.lower().startswith("inline")

        if filename and not is_inline:
            att: dict[str, Any] = {
                "filename": filename,
                "mime_type": mime,
                "size": body.get("size", 0),
                "attachment_id": body.get("attachmentId"),
                "data": body.get("data"),
            }
            results.append(att)

        for sub in part.get("parts", []):
            walk(sub)

    walk(payload)
    return results


# ────────────────────────────────────────────────────────────────────────────
# Attachment download
# ────────────────────────────────────────────────────────────────────────────


def download_attachment(
    user_email: str,
    message_id: str,
    attachment: dict[str, Any],
) -> bytes:
    """Download and decode one attachment descriptor returned by parse_message_metadata.

    Handles both the inline-data path (small attachments) and the separate
    attachments.get path (large attachments with an attachmentId).
    Returns raw bytes. Raises GmailReaderError on failure or oversized file.
    """
    size = attachment.get("size", 0)
    if size > MAX_ATTACHMENT_BYTES:
        raise GmailReaderError(
            f"Attachment {attachment['filename']!r} is {size // 1024}KB — "
            f"exceeds {MAX_ATTACHMENT_BYTES // 1024 // 1024}MB limit, skipping"
        )

    att_id = attachment.get("attachment_id")
    inline_data = attachment.get("data")

    if att_id:
        service = _build_service(user_email)
        try:
            resp = service.users().messages().attachments().get(
                userId="me", messageId=message_id, id=att_id
            ).execute()
        except HttpError as exc:
            raise GmailReaderError(
                f"Attachment download failed for {attachment['filename']!r}: {exc}"
            ) from exc
        raw_b64 = resp.get("data", "")
    elif inline_data:
        raw_b64 = inline_data
    else:
        raise GmailReaderError(
            f"Attachment {attachment['filename']!r} has neither attachmentId nor inline data"
        )

    # Gmail encodes with base64url; pad to multiple of 4 before decoding
    padded = raw_b64 + "=" * (-len(raw_b64) % 4)
    return base64.urlsafe_b64decode(padded)


# ────────────────────────────────────────────────────────────────────────────
# Label management
# ────────────────────────────────────────────────────────────────────────────


def ensure_cora_label(user_email: str) -> str:
    """Get or create the 'Cora-Filed' label for user_email. Returns the label ID."""
    service = _build_service(user_email)
    try:
        resp = service.users().labels().list(userId="me").execute()
    except HttpError as exc:
        raise GmailReaderError(f"Label list failed for {user_email}: {exc}") from exc

    for label in resp.get("labels", []):
        if label.get("name") == _CORA_LABEL_NAME:
            return label["id"]

    try:
        created = service.users().labels().create(
            userId="me",
            body={
                "name": _CORA_LABEL_NAME,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
                "color": {
                    "backgroundColor": "#16a766",
                    "textColor": "#ffffff",
                },
            },
        ).execute()
    except HttpError as exc:
        raise GmailReaderError(
            f"Label creation failed for {user_email}: {exc}"
        ) from exc

    log.info("Created Gmail label %r for %s (id=%s)", _CORA_LABEL_NAME, user_email, created["id"])
    return created["id"]


def apply_label(user_email: str, message_id: str, label_id: str) -> None:
    """Apply label_id to message_id in user_email's mailbox."""
    service = _build_service(user_email)
    try:
        service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"addLabelIds": [label_id]},
        ).execute()
    except HttpError as exc:
        raise GmailReaderError(
            f"Apply label failed for message {message_id}: {exc}"
        ) from exc


# ────────────────────────────────────────────────────────────────────────────
# Full-thread text extraction (for KB ingestion)
# ────────────────────────────────────────────────────────────────────────────


def list_threads_since(
    user_email: str,
    since_ts: int,
    max_results: int = 200,
) -> list[str]:
    """Return thread IDs whose messages were received/updated after since_ts.

    Uses Gmail's `after:` search operator (Unix timestamp). Returns thread IDs,
    NOT message IDs — one thread may contain many messages.

    Requires gmail.modify (or gmail.readonly) DWD scope.
    """
    service = _build_service(user_email)
    query = f"after:{since_ts}"
    thread_ids: list[str] = []
    page_token: str | None = None

    while len(thread_ids) < max_results:
        batch = min(max_results - len(thread_ids), 100)
        kwargs: dict[str, Any] = {
            "userId": "me",
            "q": query,
            "maxResults": batch,
        }
        if page_token:
            kwargs["pageToken"] = page_token

        try:
            resp = service.users().threads().list(**kwargs).execute()
        except HttpError as exc:
            status = exc.resp.status if exc.resp else "?"
            if status == 403:
                raise GmailReaderError(
                    f"Gmail threads.list 403 for {user_email} — check DWD scope."
                ) from exc
            raise GmailReaderError(
                f"Gmail threads.list failed for {user_email} (HTTP {status}): {exc}"
            ) from exc

        for t in resp.get("threads", []):
            thread_ids.append(t["id"])

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    log.debug(
        "list_threads_since(%s, after=%d): found %d threads",
        user_email, since_ts, len(thread_ids),
    )
    return thread_ids[:max_results]


def _extract_text_from_part(part: dict[str, Any]) -> str:
    """Recursively extract plain-text body from a MIME part."""
    mime = part.get("mimeType", "")
    body = part.get("body", {})

    if mime == "text/plain":
        raw = body.get("data", "")
        if raw:
            try:
                padded = raw + "=" * (-len(raw) % 4)
                return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
            except Exception:
                return ""
    elif mime == "text/html":
        # Skip HTML parts — we prefer plain text
        return ""
    elif mime.startswith("multipart/"):
        parts = part.get("parts", [])
        texts = [_extract_text_from_part(p) for p in parts]
        return "\n".join(t for t in texts if t.strip())

    return ""


def get_full_thread_text(
    user_email: str,
    thread_id: str,
) -> list[dict[str, Any]]:
    """Fetch full thread and return per-message metadata + body text.

    Returns a list of message dicts:
      {
        "message_id": str,
        "thread_id": str,
        "sender": str,
        "recipients": str,
        "subject": str,
        "date_ts": int,       Unix seconds
        "body_text": str,     plain text body (may be empty for HTML-only emails)
        "attachment_names": list[str],
        "label_ids": list[str],
      }

    All messages in the thread are returned, oldest first.
    Uses FULL format to get complete MIME tree.
    """
    service = _build_service(user_email)
    try:
        resp = service.users().threads().get(
            userId="me",
            id=thread_id,
            format="full",
        ).execute()
    except HttpError as exc:
        raise GmailReaderError(
            f"Gmail threads.get failed for {thread_id}: {exc}"
        ) from exc

    messages = resp.get("messages", [])
    results: list[dict[str, Any]] = []

    for msg in messages:
        headers = {
            h["name"].lower(): h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }

        date_str = headers.get("date", "")
        try:
            date_ts = int(email.utils.parsedate_to_datetime(date_str).timestamp())
        except Exception:
            date_ts = int(msg.get("internalDate", "0")) // 1000

        payload = msg.get("payload", {})
        body_text = _extract_text_from_part(payload).strip()

        # Collect attachment names (non-inline)
        att_parts = _extract_attachment_parts(payload)
        attachment_names = [a["filename"] for a in att_parts if a.get("filename")]

        results.append({
            "message_id": msg["id"],
            "thread_id": thread_id,
            "sender": headers.get("from", ""),
            "recipients": headers.get("to", ""),
            "subject": headers.get("subject", "(no subject)"),
            "date_ts": date_ts,
            "body_text": body_text[:4000],  # Cap at 4KB per message for KB chunk size
            "attachment_names": attachment_names,
            "label_ids": msg.get("labelIds", []),
        })

    # Oldest first (Gmail returns newest-first in thread)
    results.sort(key=lambda m: m["date_ts"])
    return results
