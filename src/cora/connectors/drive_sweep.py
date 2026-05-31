"""Drive sweep connector — full-corpus multi-user DWD ingestion.

For each account in monitored-email-accounts.yaml with drive_sweep: true,
impersonates the user via Domain-wide Delegation (DWD), enumerates all
Drive files modified within the freshness window, extracts text content
by MIME type, runs Claude Haiku classification to separate signal from
noise, then embeds and stores surviving chunks in the KB.

Auth requirements (one-time Harrison action):
  Add 'https://www.googleapis.com/auth/drive.readonly' to the Cora SA
  DWD grants in admin.google.com → Security → API Controls → Domain-wide
  Delegation → edit the SA entry → add scope.

Supported file types:
  Google Docs       → Drive export as text/plain
  Google Sheets     → Drive export as XLSX → openpyxl → Haiku summary
  Google Slides     → Drive export as text/plain
  PDFs              → download + pdfplumber text extraction
  XLSX/DOCX/TXT/CSV → download + best-available extractor

Deduplication:
  source='drive_sweep', source_id=file_id.  Same file_id shared to multiple
  users is only ingested once (first-seen wins via upsert).

Watermark:
  Stored via kb.set_sync_state(f"drive_sweep_{email}", iso_timestamp).
  Incremental re-runs only process files modified after last sweep.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import textwrap
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from cora.knowledge_base.store import Document
from cora.phi_guard import _PHI_PATTERNS

log = logging.getLogger("cora.drive_sweep")

# ── MIME type routing ──────────────────────────────────────────────────────────

_GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
_GOOGLE_SHEET_MIME = "application/vnd.google-apps.spreadsheet"
_GOOGLE_SLIDE_MIME = "application/vnd.google-apps.presentation"
_GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"
_GOOGLE_FORM_MIME = "application/vnd.google-apps.form"
_GOOGLE_SCRIPT_MIME = "application/vnd.google-apps.script"

_SKIP_MIME_PREFIXES = (
    "image/",
    "video/",
    "audio/",
    "application/vnd.google-apps.folder",
    "application/vnd.google-apps.form",
    "application/vnd.google-apps.script",
    "application/vnd.google-apps.photo",
    "application/vnd.google-apps.drawing",
    "application/vnd.google-apps.map",
    "application/vnd.google-apps.shortcut",
    "application/zip",
    "application/gzip",
    "application/x-tar",
    "application/octet-stream",
)

_TEXT_MIME_TYPES = {
    "text/plain",
    "text/csv",
    "text/markdown",
    "text/html",
}

_PDF_MIME = "application/pdf"
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# ── Haiku classification ──────────────────────────────────────────────────────

_CLASSIFY_PROMPT = textwrap.dedent("""
You are classifying a business document for relevance to an HJR portfolio company.

File: {filename}
Owner: {user_name} ({user_email})
Likely entity: {entity}
Content preview:
---
{preview}
---

Rate business relevance 0-10:
  8-10 = contracts, signed agreements, financial data, meeting notes, strategic plans,
         operational SOPs, org charts, product specs, legal filings, cap tables,
         vendor agreements, staff rosters, compliance docs
  5-7  = internal communications, project notes, research, vendor correspondence
  2-4  = blank templates, rough drafts, duplicate copies, presentation shells
  0-1  = personal non-business files, temp files, system files, empty docs

Also suggest the most specific entity from:
  FNDR, HJRG, F3E, F3C, OSN, LEX, LEX-LLC, LEX-LTS, LEX-LBHS, LEX-LLA,
  UFL, BDM, HJRP, HJRP-CL, HJRP-LCI, HJRP-RR, HJRPROD

Respond with JSON only (no markdown):
{{"score": <0-10>, "entity": "<code>", "summary": "<one sentence>", "discard_reason": "<if score < 4, why>"}}
""").strip()


def _classify(anthropic_client: Any, filename: str, user_name: str,
              user_email: str, entity: str, preview: str) -> dict:
    """Run Haiku classification on a content preview. Returns parsed dict."""
    prompt = _CLASSIFY_PROMPT.format(
        filename=filename,
        user_name=user_name,
        user_email=user_email,
        entity=entity,
        preview=preview[:2000],
    )
    try:
        msg = anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        # Strip markdown fences if Haiku adds them
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL).strip()
        return json.loads(raw)
    except Exception as exc:
        log.warning("drive_sweep: Haiku classification failed for %s: %s", filename, exc)
        return {"score": 5, "entity": entity, "summary": filename, "discard_reason": ""}


# ── Content extraction helpers ────────────────────────────────────────────────

def _extract_google_doc(service: Any, file_id: str) -> str:
    """Export a Google Doc as plain text."""
    try:
        data = _retry_execute(service.files().export(
            fileId=file_id, mimeType="text/plain"
        ))
        return data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
    except Exception as exc:
        log.debug("drive_sweep: export failed for Doc %s: %s", file_id, exc)
        return ""


def _extract_google_sheet(service: Any, file_id: str) -> str:
    """Export a Google Sheet as XLSX, parse all tabs with openpyxl."""
    try:
        import openpyxl
    except ImportError:
        return ""
    try:
        data = _retry_execute(service.files().export(
            fileId=file_id,
            mimeType=_XLSX_MIME,
        ))
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
        parts: list[str] = []
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            rows_text: list[str] = []
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) for c in row if c is not None and str(c).strip()]
                if cells:
                    rows_text.append("\t".join(cells))
            if rows_text:
                parts.append(f"[Sheet: {sheet_name}]\n" + "\n".join(rows_text[:200]))
        return "\n\n".join(parts)
    except Exception as exc:
        log.debug("drive_sweep: sheet export failed for %s: %s", file_id, exc)
        return ""


def _extract_pdf_bytes(data: bytes) -> str:
    """Extract text from PDF bytes using pdfplumber."""
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages: list[str] = []
            for page in pdf.pages[:40]:  # cap at 40 pages
                text = page.extract_text()
                if text:
                    pages.append(text)
            return "\n\n".join(pages)
    except ImportError:
        log.warning("drive_sweep: pdfplumber not installed -- skipping PDF")
        return ""
    except Exception as exc:
        log.debug("drive_sweep: pdfplumber extraction failed: %s", exc)
        return ""


def _extract_xlsx_bytes(data: bytes) -> str:
    """Parse XLSX binary with openpyxl."""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
        parts: list[str] = []
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            rows_text: list[str] = []
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) for c in row if c is not None and str(c).strip()]
                if cells:
                    rows_text.append("\t".join(cells))
            if rows_text:
                parts.append(f"[Sheet: {sheet_name}]\n" + "\n".join(rows_text[:200]))
        return "\n\n".join(parts)
    except Exception as exc:
        log.debug("drive_sweep: XLSX parse failed: %s", exc)
        return ""


def _download_and_extract(service: Any, file_meta: dict) -> str:
    """Download a binary file and extract text based on MIME type."""
    mime = file_meta.get("mimeType", "")
    file_id = file_meta["id"]
    try:
        data: bytes = _retry_execute(service.files().get_media(fileId=file_id))
    except Exception as exc:
        log.debug("drive_sweep: download failed for %s: %s", file_id, exc)
        return ""

    if mime == _PDF_MIME:
        return _extract_pdf_bytes(data)
    if mime == _XLSX_MIME:
        return _extract_xlsx_bytes(data)
    if mime in _TEXT_MIME_TYPES or mime.startswith("text/"):
        return data.decode("utf-8", errors="replace")
    if mime == _DOCX_MIME:
        # Try python-docx if available, otherwise treat as unsupported
        try:
            from docx import Document as DocxDocument
            doc = DocxDocument(io.BytesIO(data))
            return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        except ImportError:
            pass
    return ""


def _extract_content(service: Any, file_meta: dict) -> str:
    """Route to the right extractor for a Drive file."""
    mime = file_meta.get("mimeType", "")
    file_id = file_meta["id"]

    # Skip known-noise MIME types immediately
    for prefix in _SKIP_MIME_PREFIXES:
        if mime.startswith(prefix) or mime == prefix:
            return ""

    if mime == _GOOGLE_DOC_MIME:
        return _extract_google_doc(service, file_id)
    if mime == _GOOGLE_SHEET_MIME:
        return _extract_google_sheet(service, file_id)
    if mime in (_GOOGLE_SLIDE_MIME,):
        return _extract_google_doc(service, file_id)  # same export path
    # Binary files
    return _download_and_extract(service, file_meta)


# ── Chunking + KB storage ─────────────────────────────────────────────────────

_CHUNK_SIZE = 1400   # characters per KB chunk
_CHUNK_OVERLAP = 150


def _chunk_text(text: str) -> list[str]:
    """Split long text into overlapping chunks."""
    if len(text) <= _CHUNK_SIZE:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + _CHUNK_SIZE
        chunks.append(text[start:end])
        start = end - _CHUNK_OVERLAP
    return chunks


def _ingest_file(kb: Any, file_meta: dict, content: str,
                 classification: dict, user: dict) -> int:
    """Store classified content in the KB as a single Document. Returns chunk count ingested."""
    entity = classification.get("entity") or user.get("entity_default", "FNDR")
    sub_entity: str | None = None
    if "-" in entity and entity.startswith("LEX"):
        sub_entity = entity
        entity = "LEX"

    summary = classification.get("summary", file_meta["name"])
    file_id = file_meta["id"]
    modified_iso = file_meta.get("modifiedTime", "")
    drive_link = f"https://drive.google.com/file/d/{file_id}/view"

    date_modified: int | None = None
    if modified_iso:
        try:
            date_modified = int(datetime.fromisoformat(modified_iso.replace("Z", "+00:00")).timestamp())
        except Exception:
            pass

    doc = Document(
        source="drive_sweep",
        source_id=file_id,
        entity=entity,
        sub_entity=sub_entity,
        title=file_meta["name"],
        content=content,
        deep_link=drive_link,
        author=user.get("name", user["email"]),
        date_modified=date_modified,
        metadata={
            "mime_type": file_meta.get("mimeType", ""),
            "user_email": user["email"],
            "user_name": user.get("name", user["email"]),
            "haiku_score": classification.get("score", 5),
            "haiku_summary": summary,
            "modified_time": modified_iso,
            "drive_link": drive_link,
        },
    )
    try:
        return kb.upsert_documents([doc])
    except Exception as exc:
        log.warning("drive_sweep: upsert_documents failed for %s: %s", file_meta["name"], exc)
        return 0


# ── Per-user sweep ────────────────────────────────────────────────────────────

_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"

_SUPPORTED_MIME_QUERY = " or ".join([
    f"mimeType='{_GOOGLE_DOC_MIME}'",
    f"mimeType='{_GOOGLE_SHEET_MIME}'",
    f"mimeType='{_GOOGLE_SLIDE_MIME}'",
    f"mimeType='{_PDF_MIME}'",
    f"mimeType='{_XLSX_MIME}'",
    f"mimeType='{_DOCX_MIME}'",
    "mimeType='text/plain'",
    "mimeType='text/csv'",
    "mimeType='text/markdown'",
])

# Files from these folders won't get an entity override (they're already labelled)
_NOISE_FOLDER_PATTERNS = re.compile(
    r"(trash|archive|old|backup|template|\\.cache|\\.tmp|receipts?/personal)",
    re.IGNORECASE,
)


def _retry_execute(request: Any, max_retries: int = 3) -> Any:
    """Execute a Google API request, retrying on 429 (rate limit) or 503 (unavailable).

    Sleeps exponentially (2s, 4s, 8s) between retries. Raises the original
    HttpError if all retries are exhausted.
    """
    from googleapiclient.errors import HttpError

    for attempt in range(max_retries + 1):
        try:
            return request.execute()
        except HttpError as exc:
            status = exc.resp.status if exc.resp else 0
            if status in (429, 503) and attempt < max_retries:
                sleep_seconds = 2 ** (attempt + 1)
                log.warning(
                    "drive_sweep: Google API returned %d — retrying in %ds (attempt %d/%d)",
                    status, sleep_seconds, attempt + 1, max_retries,
                )
                time.sleep(sleep_seconds)
                continue
            raise


def _build_drive_service(sa_json_path: str, user_email: str):
    """Build a Drive v3 service impersonating user_email via DWD."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_file(
        sa_json_path, scopes=[_DRIVE_SCOPE]
    ).with_subject(user_email)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def sweep_user(
    user: dict,
    sa_json_path: str,
    kb: Any,
    anthropic_client: Any,
    freshness_days: int = 730,
    dry_run: bool = False,
    seen_file_ids: set[str] | None = None,
) -> dict:
    """Sweep one user's Drive. Returns stats dict."""
    email = user["email"]
    name = user.get("name", email)
    entity_default = user.get("entity_default", "FNDR")
    is_lex_user = email.endswith("@lexingtonservices.com")

    if seen_file_ids is None:
        seen_file_ids = set()

    stats = {"files_enumerated": 0, "files_extracted": 0,
             "chunks_ingested": 0, "phi_skipped": 0, "noise_filtered": 0,
             "dedup_skipped": 0}

    # Watermark for incremental sync
    watermark_key = f"drive_sweep_{email}"
    last_run_ts: int | None = None
    try:
        state = kb.get_sync_state(watermark_key)
        if state and isinstance(state[0], int):
            last_run_ts = state[0]
    except Exception:
        pass

    cutoff = datetime.now(timezone.utc) - timedelta(days=freshness_days)
    if last_run_ts:
        watermark_dt = datetime.fromtimestamp(last_run_ts, tz=timezone.utc)
        # Use the more recent of freshness cutoff vs watermark
        cutoff = max(cutoff, watermark_dt)

    cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%S")
    log.info("drive_sweep: sweeping %s (%s) modified since %s", email, name, cutoff_str)

    try:
        service = _build_drive_service(sa_json_path, email)
    except Exception as exc:
        log.error("drive_sweep: could not build Drive service for %s: %s", email, exc)
        return stats

    # Build Drive files.list query
    q = (
        f"({_SUPPORTED_MIME_QUERY})"
        f" and trashed = false"
        f" and modifiedTime > '{cutoff_str}'"
    )

    page_token = None
    run_start = datetime.now(timezone.utc)

    while True:
        try:
            resp = service.files().list(
                q=q,
                spaces="drive",
                fields="nextPageToken, files(id, name, mimeType, modifiedTime, size, webViewLink, parents)",
                pageSize=100,
                pageToken=page_token,
            ).execute()
        except Exception as exc:
            log.error("drive_sweep: files.list failed for %s: %s", email, exc)
            break

        files = resp.get("files", [])
        stats["files_enumerated"] += len(files)

        for file_meta in files:
            file_id = file_meta["id"]
            filename = file_meta.get("name", "")

            # Cross-user dedup
            if file_id in seen_file_ids:
                stats["dedup_skipped"] += 1
                continue
            seen_file_ids.add(file_id)

            # Skip very small files (likely empty/template)
            size_str = file_meta.get("size", "0")
            try:
                if int(size_str) < 200:
                    stats["noise_filtered"] += 1
                    continue
            except (ValueError, TypeError):
                pass

            # Extract content
            content = _extract_content(service, file_meta)
            if not content or len(content.strip()) < 150:
                stats["noise_filtered"] += 1
                continue

            # PHI guard for Lex users — quarantine before classification
            if is_lex_user and _PHI_PATTERNS.search(content[:5000]):
                log.debug("drive_sweep: PHI guard triggered for %s/%s", email, filename)
                stats["phi_skipped"] += 1
                continue

            stats["files_extracted"] += 1

            # Haiku classification
            preview = content[:2000]
            classification = _classify(
                anthropic_client, filename, name, email, entity_default, preview
            )
            score = classification.get("score", 0)

            if score < 4:
                log.debug("drive_sweep: discarded %s (score=%s: %s)",
                          filename, score, classification.get("discard_reason", ""))
                stats["noise_filtered"] += 1
                continue

            log.info("drive_sweep: ingesting %s (score=%s, entity=%s)",
                     filename, score, classification.get("entity", entity_default))

            if dry_run:
                stats["chunks_ingested"] += 1
                continue

            n = _ingest_file(kb, file_meta, content, classification, user)
            stats["chunks_ingested"] += n

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    # Advance watermark only on successful (non-dry-run) sweep
    if not dry_run:
        try:
            kb.set_sync_state(watermark_key, int(run_start.timestamp()))
        except Exception as exc:
            log.warning("drive_sweep: could not advance watermark for %s: %s", email, exc)

    log.info(
        "drive_sweep: %s done -- enumerated=%d extracted=%d ingested=%d "
        "phi_skipped=%d noise=%d dedup=%d",
        email,
        stats["files_enumerated"], stats["files_extracted"], stats["chunks_ingested"],
        stats["phi_skipped"], stats["noise_filtered"], stats["dedup_skipped"],
    )
    return stats


# ── Main entry point ──────────────────────────────────────────────────────────

def run_sweep(
    sa_json_path: str,
    accounts_yaml_path: str,
    kb: Any,
    anthropic_client: Any,
    freshness_days: int = 730,
    dry_run: bool = False,
    only_email: str | None = None,
) -> dict:
    """Sweep all enabled drive_sweep accounts. Returns aggregate stats."""
    with open(accounts_yaml_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    accounts = [
        a for a in cfg.get("accounts", [])
        if a.get("enabled", True) and a.get("dwd_eligible", False)
        and a.get("drive_sweep", False)
    ]

    if only_email:
        accounts = [a for a in accounts if a["email"] == only_email]

    log.info("drive_sweep: starting sweep of %d accounts (freshness=%dd, dry_run=%s)",
             len(accounts), freshness_days, dry_run)

    aggregate: dict = {
        "accounts_swept": 0, "files_enumerated": 0, "files_extracted": 0,
        "chunks_ingested": 0, "phi_skipped": 0, "noise_filtered": 0,
        "dedup_skipped": 0,
    }
    seen_file_ids: set[str] = set()

    for user in accounts:
        stats = sweep_user(
            user=user,
            sa_json_path=sa_json_path,
            kb=kb,
            anthropic_client=anthropic_client,
            freshness_days=freshness_days,
            dry_run=dry_run,
            seen_file_ids=seen_file_ids,
        )
        aggregate["accounts_swept"] += 1
        for k in ("files_enumerated", "files_extracted", "chunks_ingested",
                  "phi_skipped", "noise_filtered", "dedup_skipped"):
            aggregate[k] += stats.get(k, 0)

    log.info(
        "drive_sweep: COMPLETE -- accounts=%d enumerated=%d extracted=%d "
        "ingested=%d phi_skipped=%d noise=%d dedup=%d",
        aggregate["accounts_swept"], aggregate["files_enumerated"],
        aggregate["files_extracted"], aggregate["chunks_ingested"],
        aggregate["phi_skipped"], aggregate["noise_filtered"],
        aggregate["dedup_skipped"],
    )
    return aggregate
