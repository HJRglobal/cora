"""Byte-capable Slack file upload, with an honest failure the requester sees.

WHY THIS MODULE EXISTS (cq-b0a847ef0c8e)
----------------------------------------
`financial_client.upload_report_as_file` implemented Slack's 3-step external
upload correctly and had NEVER RUN in production: a grep of every log line ever
written returns zero hits -- no success, no failure, no fallback warning. Two
compounding reasons:

  1. `files:write` is not in the app's granted scopes, so step 1 raises
     `missing_scope` on every call; and
  2. the handler logged that at WARNING and returned False, and the caller
     silently posted the report inline instead. From the outside the lane looked
     like it did not exist, and from the inside it looked like it was never
     reached. A silent degrade with no user-visible signal is indistinguishable
     from dead code -- which is exactly how it was diagnosed.

So this module keeps the working transport, generalizes it from "a str of text"
to "bytes with a filename and a content type" (which is what an .xlsx export
needs), and changes the failure contract: a caller can now find out WHY the
upload did not happen and say so, instead of quietly serving a worse result.

SCOPE IS PROBED, NOT ASSUMED. `slack-app-config/manifest.json` is repo state and
may lag the live app in EITHER direction, so `files_write_granted()` reads the
`x-oauth-scopes` header Slack returns on `auth.test`. That makes the difference
between "the grant has not been made yet" and "the grant exists and something
else broke" answerable from a log line rather than by guessing.

NO AUTHORITY CHANGE. This uploads to a channel the caller already decided to
post in; it grants no new read, no new destination, and no send authority.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Any

log = logging.getLogger("cora.slack_file_upload")

# Outcome codes. Callers branch on these rather than a bare bool so a failure can
# be explained to the requester in their own words.
OK = "ok"
NO_SCOPE = "missing_scope"          # files:write not granted -- an admin action
NO_TOKEN = "no_token"               # SLACK_BOT_TOKEN missing
NO_HTTPX = "no_httpx"               # transport dependency absent
NOT_IN_CHANNEL = "not_in_channel"   # bot cannot post there
FAILED = "failed"                   # everything else

# What the requester is told, per outcome. Deliberately says what happened AND
# who can fix it -- "couldn't upload the file" alone sends people to the wrong
# place (they retry, or they ask Cora, when the fix is a Slack app grant).
_REQUESTER_NOTE: dict[str, str] = {
    NO_SCOPE: ("_I couldn't attach this as a file -- my Slack app is missing the "
               "`files:write` permission, so I've posted it inline instead. "
               "Harrison can grant it in the Slack app settings._"),
    NO_TOKEN: ("_I couldn't attach this as a file (Slack credentials unavailable), "
               "so I've posted it inline instead._"),
    NO_HTTPX: ("_I couldn't attach this as a file (upload transport unavailable), "
               "so I've posted it inline instead._"),
    NOT_IN_CHANNEL: ("_I couldn't attach this as a file -- I'm not a member of this "
                     "channel. Invite me and I'll attach it next time._"),
    FAILED: ("_I couldn't attach this as a file (the upload failed), so I've "
             "posted it inline instead._"),
}


def requester_note(outcome: str) -> str:
    """The one line a caller appends when it falls back to inline. Empty on OK."""
    if outcome == OK:
        return ""
    return _REQUESTER_NOTE.get(outcome, _REQUESTER_NOTE[FAILED])


# ── scope probe ─────────────────────────────────────────────────────────────
# auth.test is cheap but not free; the grant does not change mid-conversation.
_SCOPE_SHAPE = re.compile(r"[a-z0-9][a-z0-9._:-]{2,63}")
_SCOPE_CACHE: dict[str, Any] = {"at": 0.0, "scopes": None}
_SCOPE_TTL = 600.0
_SCOPE_LOCK = threading.Lock()


def granted_scopes(slack_client: Any, *, force: bool = False) -> frozenset[str] | None:
    """The bot token's granted scopes, from auth.test's `x-oauth-scopes` header.

    Returns None when the probe could not be completed -- callers must treat
    that as UNKNOWN and attempt the upload anyway, never as "not granted".
    Refusing on an inconclusive probe would turn a transient auth.test blip into
    a silent capability outage, which is the failure mode this module exists to
    end.
    """
    now = time.time()
    with _SCOPE_LOCK:
        if not force and _SCOPE_CACHE["scopes"] is not None \
                and (now - float(_SCOPE_CACHE["at"])) < _SCOPE_TTL:
            return _SCOPE_CACHE["scopes"]
    try:
        resp = slack_client.auth_test()
        headers = getattr(resp, "headers", None) or {}
        raw = ""
        for key in ("x-oauth-scopes", "X-OAuth-Scopes"):
            val = headers.get(key)
            if val:
                raw = val[0] if isinstance(val, (list, tuple)) else str(val)
                break
        if not raw:
            return None
        scopes = frozenset(s.strip() for s in raw.split(",") if s.strip())
        # VALIDATE what we parsed. A header we cannot recognize as a scope list
        # must read as UNKNOWN, never as a confident "not granted" -- otherwise
        # any object that merely responds to .get() (a test double, a changed
        # SDK response shape) silently disables the upload lane, which is the
        # exact class of invisible degrade this module was written to end.
        # Real Slack scopes look like `chat:write`, `users:read.email`,
        # `files:write`: lowercase, with : . _ - as the only separators.
        if not scopes or not all(_SCOPE_SHAPE.fullmatch(s) for s in scopes):
            log.debug("slack_file_upload: unrecognizable scope header -- treating "
                      "the grant as UNKNOWN rather than denied")
            return None
    except Exception:  # noqa: BLE001 -- an unknown probe must not block the upload
        log.debug("slack_file_upload: scope probe failed", exc_info=True)
        return None
    with _SCOPE_LOCK:
        _SCOPE_CACHE["at"] = now
        _SCOPE_CACHE["scopes"] = scopes
    log.info("slack_file_upload: granted scopes probed (files:write=%s)",
             "files:write" in scopes)
    return scopes


def files_write_granted(slack_client: Any) -> bool | None:
    """True / False / None (unknown). None means "attempt it and find out"."""
    scopes = granted_scopes(slack_client)
    if scopes is None:
        return None
    return "files:write" in scopes


def reset_scope_cache() -> None:
    with _SCOPE_LOCK:
        _SCOPE_CACHE["at"] = 0.0
        _SCOPE_CACHE["scopes"] = None


# ── the upload ──────────────────────────────────────────────────────────────
def upload_bytes(
    slack_client: Any,
    channel_id: str,
    filename: str,
    payload: bytes,
    title: str,
    thread_ts: str | None = None,
    content_type: str = "application/octet-stream",
) -> tuple[str, str]:
    """Upload bytes as a Slack file. Returns ``(outcome, detail)``.

    ``outcome == OK`` means the file is in the channel. Anything else means it is
    NOT, and the caller owes the requester `requester_note(outcome)` alongside
    whatever fallback it serves.

    Text callers should route through :func:`upload_text`, which applies the
    egress sanitizer. Binary payloads (xlsx) cannot be sanitized as text and are
    the caller's responsibility to have built from already-guarded values.
    """
    try:
        import httpx
    except ImportError:
        log.warning("slack_file_upload: httpx not installed -- cannot upload %s", filename)
        return NO_HTTPX, "httpx not installed"

    if not os.environ.get("SLACK_BOT_TOKEN", ""):
        log.warning("slack_file_upload: SLACK_BOT_TOKEN not set -- cannot upload %s", filename)
        return NO_TOKEN, "SLACK_BOT_TOKEN not set"

    if not channel_id:
        return FAILED, "no channel id"

    # Probe first so the log distinguishes "never granted" from "granted but
    # broke". An UNKNOWN probe falls through and attempts the upload.
    if files_write_granted(slack_client) is False:
        log.warning(
            "slack_file_upload: files:write is NOT granted on the live app -- "
            "%s was not uploaded and the caller must fall back visibly", filename)
        return NO_SCOPE, "files:write not granted"

    try:
        url_resp = slack_client.files_getUploadURLExternal(
            filename=filename, length=len(payload))
        if not url_resp.get("ok"):
            err = str(url_resp.get("error") or "")
            log.warning("slack_file_upload: getUploadURL failed for %s: %s", filename, err)
            return (NO_SCOPE if "missing_scope" in err else FAILED), err

        put_resp = httpx.put(
            url_resp["upload_url"], content=payload,
            headers={"Content-Type": content_type}, timeout=30.0)
        if put_resp.status_code not in (200, 201):
            log.warning("slack_file_upload: PUT failed for %s status=%s",
                        filename, put_resp.status_code)
            return FAILED, f"PUT status {put_resp.status_code}"

        complete_kwargs: dict[str, Any] = {
            "files": [{"id": url_resp["file_id"], "title": title}],
            "channel_id": channel_id,
        }
        if thread_ts:
            complete_kwargs["thread_ts"] = thread_ts
        complete_resp = slack_client.files_completeUploadExternal(**complete_kwargs)
        if not complete_resp.get("ok"):
            err = str(complete_resp.get("error") or "")
            log.warning("slack_file_upload: completeUpload failed for %s: %s", filename, err)
            if "not_in_channel" in err:
                return NOT_IN_CHANNEL, err
            return (NO_SCOPE if "missing_scope" in err else FAILED), err

        log.info("slack_file_upload: uploaded %s (%d bytes) to channel=%s",
                 filename, len(payload), channel_id)
        return OK, ""

    except Exception as exc:  # noqa: BLE001
        err = ""
        resp = getattr(exc, "response", None)
        if resp is not None:
            try:
                err = str(resp.get("error", "") or "")
            except Exception:  # noqa: BLE001
                err = ""
        err = err or str(exc)
        if "missing_scope" in err or "not_allowed_token_type" in err:
            log.warning("slack_file_upload: missing files:write scope uploading %s "
                        "-- caller must fall back visibly", filename)
            return NO_SCOPE, err
        if "not_in_channel" in err:
            return NOT_IN_CHANNEL, err
        log.warning("slack_file_upload: upload of %s failed: %s", filename, err)
        return FAILED, err


def upload_text(
    slack_client: Any,
    channel_id: str,
    title: str,
    content: str,
    thread_ts: str | None = None,
    filename: str | None = None,
) -> tuple[str, str]:
    """Upload text as a .txt file, egress-sanitized (W3-05).

    The bytes are PUT straight to Slack's upload URL via httpx, which bypasses
    the slack_egress WebClient patch (that only wraps chat_* sends), so the
    content AND the title -- which is shared via files_completeUploadExternal --
    go through the same sanitizer every chat send gets. Fail-soft: a sanitizer
    error must never drop the upload.
    """
    try:
        from ..slack_egress import sanitize_text
        content = sanitize_text(content)
        title = sanitize_text(title)
    except Exception:  # noqa: BLE001
        log.exception("slack_file_upload: egress sanitize failed; uploading raw")

    name = filename or f"cora-report-{int(time.time())}.txt"
    return upload_bytes(slack_client, channel_id, name, content.encode("utf-8"),
                        title, thread_ts, "text/plain; charset=utf-8")
