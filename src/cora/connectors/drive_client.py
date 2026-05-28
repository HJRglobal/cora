"""Thin Drive download helper.

Provides download_file_bytes(file_id) used by photoroom_client._resolve_image
for ImageRef(type="drive_file_id") specs.  Shares auth with drive_connector.
"""

from __future__ import annotations

import io
import logging

from googleapiclient.http import MediaIoBaseDownload

from .drive_connector import DriveConnectorError, _build_drive_service

log = logging.getLogger(__name__)


class DriveClientError(Exception):
    pass


def download_file_bytes(file_id: str, impersonate: bool = True) -> bytes:
    """Download a Drive file by ID and return raw bytes.

    impersonate=False uses direct SA credentials instead of DWD — use this for
    files in folders shared directly with the SA email.
    Raises DriveClientError on auth failure or HTTP error.
    """
    try:
        service = _build_drive_service(impersonate=impersonate)
    except DriveConnectorError as exc:
        raise DriveClientError(f"Drive auth failed: {exc}") from exc

    try:
        request = service.files().get_media(fileId=file_id)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buf.getvalue()
    except Exception as exc:
        raise DriveClientError(f"Drive download failed for file {file_id!r}: {exc}") from exc


def list_folder_files(folder_id: str, name_contains: str = "", impersonate: bool = True) -> list[dict]:
    """Return files in a Drive folder, newest-first.

    Args:
        folder_id:     Drive folder ID to list.
        name_contains: Optional case-insensitive substring filter on filename.
        impersonate:   False = direct SA credentials (for SA-shared folders).

    Returns list of dicts with keys: id, name, mimeType, modifiedTime.
    Raises DriveClientError on failure.
    """
    try:
        service = _build_drive_service(impersonate=impersonate)
    except DriveConnectorError as exc:
        raise DriveClientError(f"Drive auth failed: {exc}") from exc

    q = f"'{folder_id}' in parents and trashed=false"
    if name_contains:
        q += f" and name contains '{name_contains}'"

    try:
        resp = (
            service.files()
            .list(
                q=q,
                fields="files(id,name,mimeType,modifiedTime)",
                orderBy="modifiedTime desc",
                pageSize=50,
            )
            .execute()
        )
    except Exception as exc:
        raise DriveClientError(f"Drive folder list failed for {folder_id!r}: {exc}") from exc

    return resp.get("files", [])
