"""Crash-safe idempotency ledgers for the email attachment auto-filer.

WHY THIS EXISTS (2026-06-14 root-cause):
  The filer already had dedup state (`data/cache/filed-message-ids.json`) and
  `upload_file()` already deduped by exact filename. Both failed in production:

  * The state dict was saved ONLY at the very end of `run_filer()`. The Task
    Scheduler job has a 15-minute `ExecutionTimeLimit`, and the per-account
    watermark had been frozen at 2026-05-28 -- so every run re-scanned ~2.5
    weeks of mail across 12 inboxes, blew past 15 min, and was KILLED before
    the end-of-run save ever ran. The dict was never written to disk (the file
    didn't even exist), so message-level dedup never persisted across runs.
  * Even with a working message-id dedup, the same document arrives via
    DISTINCT emails (e.g. an original Dropbox-Sign notice + a "Fwd:" of it),
    each with its own Message-ID + Date, classified independently into slightly
    different filenames/folders by the LLM. Neither a message-id ledger nor a
    same-name Drive check can catch that. Content (md5) dedup can.

DESIGN (mirrors the nudge_ledger.py append-only JSONL pattern):
  Two append-only JSONL ledgers under data/state/ (gitignored runtime state),
  paths overridable via env so tests can redirect them:

    FILER_CONTENT_LEDGER_PATH  -- one row per filed attachment, keyed on the
                                  content md5 (matches Drive's md5Checksum so the
                                  same value backstops against existing Drive
                                  files). Folder-agnostic: catches the same bytes
                                  arriving via any email, under any name, into any
                                  folder.
    FILER_MESSAGE_LEDGER_PATH  -- one row per FULLY-processed message, keyed on
                                  the RFC Message-ID (falling back to the Gmail
                                  message id when the header is absent). Lets a
                                  re-scanned message skip re-classification (no
                                  Claude call) entirely.

  Append-only => crash-safe: a mid-run kill loses at most the in-flight line.
  Callers load each ledger once per run into memory (O(1) checks) and append a
  row the instant an attachment is filed / a message completes, so progress
  survives a kill. Reads fail OPEN; writes are best-effort and never raise.

  md5 (not sha256) is the content key on purpose: Google Drive returns
  `md5Checksum` for binary files, so one consistent value powers the local
  ledger, the in-folder Drive backstop in upload_file(), and --reconcile (which
  can only learn md5 from existing Drive files). sha256 is also stored per row
  for forensics / future use, but md5 is the lookup key.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_STATE_DIR = _REPO_ROOT / "data" / "state"

_DEFAULT_CONTENT_PATH = _STATE_DIR / "filer-content-ledger.jsonl"
_DEFAULT_MESSAGE_PATH = _STATE_DIR / "filer-message-ledger.jsonl"

# Content rows kept ~1y: each row is tiny and a doc re-arriving within a year
# should still dedup. Message rows expire sooner -- once the watermark passes a
# message it is never re-listed, so the marker only needs to cover the active
# re-scan window.
_CONTENT_TTL_DAYS = int(os.environ.get("FILER_CONTENT_TTL_DAYS", "365"))
_MESSAGE_TTL_DAYS = int(os.environ.get("FILER_MESSAGE_TTL_DAYS", "60"))

_CONTENT_SCHEMA = {"_schema": "cora email-filer content ledger (key=md5)"}
_MESSAGE_SCHEMA = {"_schema": "cora email-filer message ledger (key=msg_key)"}


def _content_path() -> Path:
    return Path(os.environ.get("FILER_CONTENT_LEDGER_PATH") or _DEFAULT_CONTENT_PATH)


def _message_path() -> Path:
    return Path(os.environ.get("FILER_MESSAGE_LEDGER_PATH") or _DEFAULT_MESSAGE_PATH)


# ────────────────────────────────────────────────────────────────────────────
# Shared low-level IO
# ────────────────────────────────────────────────────────────────────────────


def _iter_rows(path: Path):
    """Yield parsed data rows (skipping the schema header + blank/bad lines)."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if not isinstance(row, dict) or "_schema" in row:
            continue
        yield row


def _append_row(path: Path, schema: dict[str, Any], row: dict[str, Any]) -> bool:
    """Append one JSON row, writing the schema header first if the file is new.

    Best-effort: any failure is logged and swallowed (never raises).
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not path.exists()
        with path.open("a", encoding="utf-8") as f:
            if new_file:
                f.write(json.dumps(schema) + "\n")
            f.write(json.dumps(row) + "\n")
        return True
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("filer_ledger append failed (%s): %s", path, exc)
        return False


# ────────────────────────────────────────────────────────────────────────────
# Content ledger (keyed on md5)
# ────────────────────────────────────────────────────────────────────────────


def load_content_ledger() -> dict[str, dict[str, Any]]:
    """Load {md5: record}, last-row-wins, pruning entries older than the TTL.

    Returned dict is the in-memory working copy for a run; callers mutate it in
    lockstep with append_content() so subsequent checks within the same run see
    rows just filed. Fail-open: a missing/unreadable file yields {}.
    """
    path = _content_path()
    cutoff = int(time.time()) - _CONTENT_TTL_DAYS * 86400
    out: dict[str, dict[str, Any]] = {}
    for row in _iter_rows(path):
        md5 = row.get("md5")
        ts = row.get("filed_at")
        if not md5 or not isinstance(ts, int):
            continue
        if ts <= cutoff:
            continue
        out[md5] = row  # last wins
    return out


def append_content(
    ledger: dict[str, dict[str, Any]],
    md5: str,
    *,
    file_id: str,
    web_link: str,
    drive_path: str,
    canonical: str,
    sha256: str = "",
    source_email: str = "",
) -> bool:
    """Record that the content with this md5 has been filed, and update `ledger`.

    Appends a durable row AND mutates the in-memory dict so later attachments in
    the same run dedup against it. Best-effort persistence (never raises).
    """
    if not md5:
        return False
    row = {
        "md5": md5,
        "sha256": sha256,
        "file_id": file_id,
        "web_link": web_link,
        "drive_path": drive_path,
        "canonical": canonical,
        "source_email": source_email,
        "filed_at": int(time.time()),
    }
    ledger[md5] = row
    return _append_row(_content_path(), _CONTENT_SCHEMA, row)


def content_record(ledger: dict[str, dict[str, Any]], md5: str) -> dict[str, Any] | None:
    """Return the prior filing record for this md5, or None if never filed."""
    if not md5:
        return None
    return ledger.get(md5)


# ────────────────────────────────────────────────────────────────────────────
# Message ledger (keyed on msg_key = rfc_message_id or "gmail:<id>")
# ────────────────────────────────────────────────────────────────────────────


def load_message_ledger() -> set[str]:
    """Load the set of msg_keys already FULLY processed, pruned by TTL.

    Fail-open: missing/unreadable file yields an empty set.
    """
    path = _message_path()
    cutoff = int(time.time()) - _MESSAGE_TTL_DAYS * 86400
    out: set[str] = set()
    for row in _iter_rows(path):
        key = row.get("msg_key")
        ts = row.get("filed_at")
        if not key or not isinstance(ts, int):
            continue
        if ts <= cutoff:
            continue
        out.add(key)
    return out


def message_done(seen: set[str], msg_key: str) -> bool:
    """True if this message was fully processed in a prior run."""
    return bool(msg_key) and msg_key in seen


def record_message_done(
    seen: set[str],
    msg_key: str,
    *,
    filed: int = 0,
    skipped: int = 0,
    subject: str = "",
) -> bool:
    """Mark a message fully processed: append a durable row + update `seen`."""
    if not msg_key:
        return False
    seen.add(msg_key)
    row = {
        "msg_key": msg_key,
        "filed": int(filed),
        "skipped": int(skipped),
        "subject": subject[:120],
        "filed_at": int(time.time()),
    }
    return _append_row(_message_path(), _MESSAGE_SCHEMA, row)


def make_msg_key(rfc_message_id: str, gmail_message_id: str) -> str:
    """Stable per-message dedup key.

    Prefer the RFC Message-ID header (stable across mailboxes / re-fetches).
    Fall back to the Gmail message id when the header is absent -- the old code
    short-circuited on an empty rfc id and silently never deduped.
    """
    rfc = (rfc_message_id or "").strip()
    if rfc:
        return rfc
    gid = (gmail_message_id or "").strip()
    return f"gmail:{gid}" if gid else ""
