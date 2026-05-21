"""Google Drive connector — Phase 4 Layer 2 metadata indexing.

Walks designated indexable folders under the HJR-Founder-OS Drive root, indexes
file metadata (filename, path, mtime, owner, MIME type, webViewLink) into
Cora's knowledge base. Title-level semantic search covers "find me the
latest X" queries.

This is NOT content extraction — we index METADATA only. The filename plus
folder path carry the semantic signal (e.g. "2026-04_f3e_distributor-sales-
deck.pptx" embeds well against the query "F3 Energy distributor sales deck").
Content extraction from xlsx/docx/pdf bodies is Phase 5+.

Auth pattern matches calendar_client.py — Service Account + Domain-Wide
Delegation, impersonating Harrison's @hjrglobal.com identity to access the
HJR-Founder-OS Drive folders.

Required DWD scope (add to existing admin config):
    https://www.googleapis.com/auth/drive.readonly

Deep-link pattern: Drive API returns `webViewLink` per file. Looks like:
    https://drive.google.com/file/d/<file_id>/view
    https://docs.google.com/document/d/<file_id>/edit
    https://docs.google.com/spreadsheets/d/<file_id>/edit
"""

from __future__ import annotations

import datetime
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Iterator

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

log = logging.getLogger(__name__)


_DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# Impersonation identity. The service account doesn't own files; it acts AS
# Harrison's @hjrglobal.com identity to access shared / owned files. Override
# via CORA_DRIVE_IMPERSONATE env var if needed.
_DEFAULT_IMPERSONATE = "harrison@hjrglobal.com"

# Root folder for the HJR portfolio Drive. Resolved at first call via search.
# Override with CORA_DRIVE_ROOT_FOLDER_ID env var to skip lookup.
_DRIVE_ROOT_FOLDER_NAME = "HJR-Founder-OS"

# MIME types we want to index. Everything else is filtered out (junk files,
# images that aren't deliverables, system files, etc.).
_INDEXABLE_MIME_TYPES = frozenset({
    # Google Workspace native types
    "application/vnd.google-apps.document",
    "application/vnd.google-apps.spreadsheet",
    "application/vnd.google-apps.presentation",
    "application/vnd.google-apps.form",
    "application/vnd.google-apps.drawing",
    # Microsoft Office formats
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/msword",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
    # PDFs
    "application/pdf",
    # Images that may be deliverables (logos, brand assets, dielines)
    "image/png",
    "image/jpeg",
    "image/svg+xml",
})

# Folder MIME type — used for traversal, not for indexing
_FOLDER_MIME = "application/vnd.google-apps.folder"

# Skip these path segments entirely (PHI + archives + system noise). Case-
# insensitive. Exact-match on each segment of the path.
# Expanded 2026-05-21 after live Drive backfill spot-check surfaced
# Explanation-of-Benefits subfolders, payroll detail folders, personal-finance
# tax-return folders, and labor-distribution folders that contain employee /
# member PII not appropriate for an internal Slack assistant.
_BLACKLIST_SEGMENTS = frozenset({
    # PHI guardrail - clinical / EHR / client-named content (matches static_md)
    "consumers", "clients", "phi", "clinical", "ehr",
    # Payroll, labor allocations, employee compensation - employee PII / wage data
    "payroll", "payroll-detail", "payroll detail",
    "labor distribution", "labor-distribution",
    # Insurance / EOBs - member names, claim numbers, diagnosis codes
    "eob", "eobs", "eob's",
    # Personal finances - Harrison's K-1s, 1065s, IRS correspondence, SSN risk
    "personal-finances", "personal_finances", "personal finances",
    "taxes", "tax-returns", "tax returns",
    # Medical
    "medical",
    # Archives (we don't index legacy folders that have been retired)
    "_archive", "_archive_external", "archive",
    # System / hidden
    ".trash", ".obsidian",
})


# File-level sensitive patterns - catches filenames like "Payroll 10-10
# detail.xlsx" that live in non-blacklisted folders. Three alternation arms,
# all case-insensitive, each calibrated to the real-world filename shapes we
# saw in the live backfill audit:
#
# Arm 1 (most patterns): both-side word boundary so "tax" doesn't match
#   "Texas". Catches "Payroll 10-10.xlsx", "Tax Return 2024.pdf", etc.
#
# Arm 2 (1065 / k-1 tax forms): leading boundary, then 1065 with optional
#   "x" suffix (1065x = amended return), then trailing boundary. Catches
#   "1065 filing.pdf" and "1065x amendment.pdf". Does NOT catch invoice
#   numbers like "10654" or "651065" because those break the boundary.
#
# Arm 3 (EOB - end-bounded only): catches filenames like
#   "HealthCareClaimEOB.xlsx" and "AetnaEOB.pdf" where EOB is glued onto a
#   preceding word with no separator. Requires only a trailing boundary
#   (end of name OR separator). Tradeoff is a small false-positive risk,
#   but Explanation-of-Benefits docs are PHI-shaped enough to justify
#   the broader catch.
_BLACKLIST_FILENAME_PATTERNS = re.compile(
    # Arm 1: standard both-side word boundary
    r"(?:^|[\s_.\-/])"
    r"(?:payroll|labor[\s_-]distribution|tax[\s_-]return|"
    r"w-?2|w-?9|ssn|paystub|pay[\s_-]stub|garnishment|"
    r"medical[\s_-]records?|client[\s_-]records?|treatment[\s_-]plan|"
    r"behavior[\s_-]plan|incident[\s_-]report)"
    r"(?:$|[\s_.\-/])"
    r"|"
    # Arm 2: 1065 (with optional x suffix for amendments) / k-1 — strict boundary
    r"(?:^|[\s_.\-/])(?:1065[x]?|k-?1)(?:$|[\s_.\-/])"
    r"|"
    # Arm 3: EOB — end-bounded only (catches HealthCareClaimEOB, AetnaEOB)
    r"eob(?:$|[\s_.\-/])",
    re.IGNORECASE,
)


class DriveConnectorError(Exception):
    """Raised when a Drive API call fails."""


@dataclass(frozen=True)
class DriveFile:
    """A single Drive file's metadata, ready for KB ingestion."""
    file_id: str
    name: str
    mime_type: str
    path: str               # e.g. "HJR-Founder-OS/02-F3-Energy/sales/deck.pptx"
    modified_time: int      # unix timestamp
    created_time: int
    owner_email: str
    web_view_link: str
    size_bytes: int


# ────────────────────────────────────────────────────────────────────────────
# Authentication — service account + DWD impersonation (matches calendar pattern)
# ────────────────────────────────────────────────────────────────────────────


def _service_account_path() -> str:
    val = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not val:
        raise DriveConnectorError(
            "GOOGLE_SERVICE_ACCOUNT_JSON not set in environment - Drive connector disabled"
        )
    if not os.path.exists(val):
        raise DriveConnectorError(
            f"GOOGLE_SERVICE_ACCOUNT_JSON path does not exist: {val}"
        )
    return val


def _impersonate_email() -> str:
    return os.environ.get("CORA_DRIVE_IMPERSONATE", _DEFAULT_IMPERSONATE).strip()


def _build_drive_service():
    """Build a Drive v3 service impersonating Harrison via DWD."""
    try:
        creds = service_account.Credentials.from_service_account_file(
            _service_account_path(),
            scopes=_DRIVE_SCOPES,
        )
    except Exception as exc:
        raise DriveConnectorError(
            f"Failed to load service account credentials: {exc}"
        ) from exc

    delegated = creds.with_subject(_impersonate_email())
    return build("drive", "v3", credentials=delegated, cache_discovery=False)


# ────────────────────────────────────────────────────────────────────────────
# Folder traversal
# ────────────────────────────────────────────────────────────────────────────


def _resolve_root_folder_id(service) -> str:
    """Find the HJR-Founder-OS root folder's Drive ID. Cached via env override."""
    override = os.environ.get("CORA_DRIVE_ROOT_FOLDER_ID", "").strip()
    if override:
        return override

    # Search for the folder by name. The impersonated user (Harrison) should
    # have direct access since HJR-Founder-OS lives in his Drive.
    try:
        resp = service.files().list(
            q=(
                f"name = '{_DRIVE_ROOT_FOLDER_NAME}' "
                f"and mimeType = '{_FOLDER_MIME}' "
                f"and trashed = false"
            ),
            fields="files(id, name, owners)",
            pageSize=10,
        ).execute()
    except HttpError as exc:
        raise DriveConnectorError(f"Drive search for root folder failed: {exc}") from exc

    files = resp.get("files", [])
    if not files:
        raise DriveConnectorError(
            f"Could not find Drive folder named {_DRIVE_ROOT_FOLDER_NAME!r}. "
            f"Check that {_impersonate_email()!r} has access to it, or set "
            f"CORA_DRIVE_ROOT_FOLDER_ID in .env to the exact folder id."
        )
    if len(files) > 1:
        log.warning(
            "Found %d folders named %r - picking first (id=%s). Set "
            "CORA_DRIVE_ROOT_FOLDER_ID in .env to disambiguate.",
            len(files), _DRIVE_ROOT_FOLDER_NAME, files[0]["id"],
        )
    return files[0]["id"]


def _is_blacklisted_path(path_segments: list[str]) -> bool:
    """Return True if the path should be skipped due to PHI/PII blacklist.

    Two checks:
    1. Exact-match: any path segment that exactly matches a folder name in
       _BLACKLIST_SEGMENTS (lowercased). Catches blacklisted folder trees.
    2. Filename pattern: the LAST segment (filename) matches the sensitive
       filename pattern (catches loose files like "Payroll 10-10.xlsx" that
       live in otherwise non-blacklisted folders).
    """
    lower_segments = [s.lower() for s in path_segments]

    # Check 1: any segment exactly matches a blacklist entry
    if set(lower_segments) & _BLACKLIST_SEGMENTS:
        return True

    # Check 2: the last segment (filename) matches a sensitive-file pattern
    if lower_segments:
        if _BLACKLIST_FILENAME_PATTERNS.search(lower_segments[-1]):
            return True

    return False


def _list_children(service, folder_id: str) -> list[dict]:
    """List immediate children of a folder, paging through results."""
    results: list[dict] = []
    page_token: str | None = None
    while True:
        try:
            resp = service.files().list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields=(
                    "nextPageToken, files(id, name, mimeType, modifiedTime, "
                    "createdTime, owners, webViewLink, size, parents)"
                ),
                pageSize=200,
                pageToken=page_token,
            ).execute()
        except HttpError as exc:
            raise DriveConnectorError(
                f"Drive list_children failed for folder {folder_id}: {exc}"
            ) from exc

        results.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return results


def walk_drive(
    service,
    root_folder_id: str,
    root_path: str = _DRIVE_ROOT_FOLDER_NAME,
    modified_after: int | None = None,
) -> Iterator[DriveFile]:
    """Depth-first walk under root_folder_id, yielding DriveFile per indexable file.

    Args:
        service: Drive API service handle from _build_drive_service()
        root_folder_id: starting folder
        root_path: human-readable path prefix for indexed files
        modified_after: if set (unix ts), only yield files modified after this

    Skips:
        - Folders / files under blacklisted segments (PHI, archive, etc.)
        - Files with MIME types outside _INDEXABLE_MIME_TYPES
        - Trashed files (the API query already excludes them)
    """
    stack: list[tuple[str, str]] = [(root_folder_id, root_path)]

    while stack:
        folder_id, current_path = stack.pop()
        try:
            children = _list_children(service, folder_id)
        except DriveConnectorError as exc:
            log.warning("Skipping folder %s due to error: %s", current_path, exc)
            continue

        for child in children:
            child_name = child.get("name", "")
            child_mime = child.get("mimeType", "")
            child_path = f"{current_path}/{child_name}"
            path_segments = child_path.split("/")

            if _is_blacklisted_path(path_segments):
                log.debug("Blacklisted path skipped: %s", child_path)
                continue

            if child_mime == _FOLDER_MIME:
                # Recurse into subfolder
                stack.append((child["id"], child_path))
                continue

            if child_mime not in _INDEXABLE_MIME_TYPES:
                continue

            # Modified-time filter (incremental sync)
            modified_iso = child.get("modifiedTime", "")
            try:
                modified_ts = int(
                    datetime.datetime.fromisoformat(
                        modified_iso.replace("Z", "+00:00")
                    ).timestamp()
                )
            except (ValueError, AttributeError):
                modified_ts = int(time.time())
            if modified_after is not None and modified_ts <= modified_after:
                continue

            try:
                created_ts = int(
                    datetime.datetime.fromisoformat(
                        child.get("createdTime", "").replace("Z", "+00:00")
                    ).timestamp()
                )
            except (ValueError, AttributeError):
                created_ts = modified_ts

            owners = child.get("owners", [])
            owner_email = owners[0].get("emailAddress", "") if owners else ""

            try:
                size_bytes = int(child.get("size", 0))
            except (ValueError, TypeError):
                size_bytes = 0

            yield DriveFile(
                file_id=child["id"],
                name=child_name,
                mime_type=child_mime,
                path=child_path,
                modified_time=modified_ts,
                created_time=created_ts,
                owner_email=owner_email,
                web_view_link=child.get("webViewLink", ""),
                size_bytes=size_bytes,
            )


# ────────────────────────────────────────────────────────────────────────────
# Document construction for KB ingestion
# ────────────────────────────────────────────────────────────────────────────


# Path-prefix -> entity classifier. Matches the static_md ENTITY_FOLDERS pattern
# so Drive assets get the same entity scoping as their CLAUDE.md siblings.
_ENTITY_FOLDERS = {
    "01-HJR-Global": "HJRG",
    "02-F3-Energy": "F3E",
    "03-F3-Community": "F3C",
    "04-UFL": "UFL",
    "05-HJR-Productions": "HJRPROD",
    "06-HJR-Properties": "HJRP",
    "07-Big-D-Media": "BDM",
    "08-Lexington-Services": "LEX",
    "09-One-Stop-Nutrition": "OSN",
    "00-Founder": "FNDR",
}


def _classify_entity(path: str) -> str:
    """Best-effort entity classification from the Drive path.

    Path format: 'HJR-Founder-OS/02-F3-Energy/sales/deck.pptx'
    Look at the second segment (index 1) - if it matches an ENTITY_FOLDERS
    prefix, use that entity. Otherwise FNDR (cross-portfolio).
    """
    parts = path.split("/")
    if len(parts) < 2:
        return "FNDR"
    candidate = parts[1]
    return _ENTITY_FOLDERS.get(candidate, "FNDR")


def drive_file_to_document(df: DriveFile):
    """Convert a DriveFile to a knowledge_base Document for ingestion.

    Local import to avoid circular dependency at module load.
    The 'content' field is synthetic metadata text - the filename plus path
    carries the semantic signal for search. Real content extraction is Phase 5.
    """
    from ..knowledge_base.store import Document  # noqa: PLC0415

    entity = _classify_entity(df.path)

    # Human-readable title — strip extension, replace separators
    title = df.name
    for ext in (
        ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".pdf",
        ".png", ".jpg", ".jpeg", ".svg",
    ):
        if title.lower().endswith(ext):
            title = title[: -len(ext)]
            break
    title = title.replace("_", " ").replace("-", " ").strip()

    # Synthetic content: filename + path + type. This is what gets embedded.
    # Future Phase 5 will append actual file body text here.
    content_parts = [
        f"Drive file: {df.name}",
        f"Path: {df.path}",
        f"Type: {df.mime_type}",
    ]
    if df.owner_email:
        content_parts.append(f"Owner: {df.owner_email}")
    content_parts.append(
        f"Modified: {datetime.date.fromtimestamp(df.modified_time).isoformat()}"
    )
    content = "\n".join(content_parts)

    return Document(
        source="drive_asset",
        source_id=df.file_id,  # Drive's file_id is globally unique + stable
        entity=entity,
        content=content,
        date_created=df.created_time,
        date_modified=df.modified_time,
        author=df.owner_email,
        title=title,
        deep_link=df.web_view_link,
        metadata={
            "path": df.path,
            "mime_type": df.mime_type,
            "size_bytes": df.size_bytes,
        },
    )


# ────────────────────────────────────────────────────────────────────────────
# High-level backfill / incremental APIs (used by scripts/)
# ────────────────────────────────────────────────────────────────────────────


def backfill(modified_after: int | None = None) -> Iterator:
    """Walk all of HJR-Founder-OS, yield Documents for every indexable file.

    Used by scripts/backfill_drive_assets.py and scripts/incremental_sync_drive.py.
    Pass `modified_after` (unix ts) for incremental sync to filter to only
    recently-changed files.
    """
    service = _build_drive_service()
    root_id = _resolve_root_folder_id(service)
    log.info(
        "Drive walk starting at root %r (id=%s, impersonating=%s, modified_after=%s)",
        _DRIVE_ROOT_FOLDER_NAME, root_id, _impersonate_email(), modified_after,
    )

    count = 0
    for df in walk_drive(service, root_id, modified_after=modified_after):
        count += 1
        yield drive_file_to_document(df)
        if count % 50 == 0:
            log.info("Drive walk progress: %d indexable files yielded so far", count)

    log.info("Drive walk complete: %d total indexable files", count)
