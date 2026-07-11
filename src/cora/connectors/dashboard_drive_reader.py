"""Drive-backed JSON store readers for the Cora dashboard read layer.

Reuses the existing Drive SA + domain-wide-delegation service
(``drive_connector._build_drive_service``) and the by-fileId download
(``drive_financial_reader._download_file_bytes``). Two readers:

  * ``read_json_by_id(file_id)`` -- download a known JSON file and parse it
    (the OneAmerica policies/transactions/history stores, pinned by id).
  * ``newest_json_by_title(folder_id, title)`` -- pick the NEWEST file matching
    a title in a folder and parse it. The capital-program "Sync to Cora" bridge
    writes a NEW same-titled file per sync, so newest-by-modifiedTime wins.

60s TTL cache; never cache a failed / empty read; fail-soft (returns None on any
error -- these readers never raise).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from .drive_connector import _build_drive_service
from .drive_financial_reader import _download_file_bytes

log = logging.getLogger(__name__)

_TTL = 60.0  # seconds
_cache: dict[str, tuple[float, Any]] = {}


def _cache_get(key: str) -> Any | None:
    entry = _cache.get(key)
    if entry and (time.monotonic() - entry[0]) < _TTL:
        return entry[1]
    return None


def _cache_set(key: str, value: Any) -> None:
    _cache[key] = (time.monotonic(), value)


def clear_cache() -> None:
    """Test hook."""
    _cache.clear()


def read_json_by_id(file_id: str) -> dict[str, Any] | None:
    """Download and parse a JSON file by Drive id. Returns None on any failure."""
    if not file_id:
        return None
    key = f"id:{file_id}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    try:
        raw = _download_file_bytes(file_id)
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 -- fail-soft
        log.warning("dashboard_drive_reader: read_json_by_id failed id=%s: %s", file_id, exc)
        return None
    if isinstance(data, dict) and data:  # never cache an empty/failed read
        _cache_set(key, data)
        return data
    return data if isinstance(data, dict) else None


def newest_file_id_by_title(folder_id: str, title: str) -> str | None:
    """Return the id of the newest (by modifiedTime) file named ``title`` in
    ``folder_id``, or None. Never raises."""
    if not folder_id or not title:
        return None
    safe_title = title.replace("\\", "\\\\").replace("'", "\\'")
    try:
        service = _build_drive_service()
        resp = (
            service.files()
            .list(
                q=f"'{folder_id}' in parents and name = '{safe_title}' and trashed = false",
                fields="files(id, name, modifiedTime)",
                orderBy="modifiedTime desc",
                pageSize=25,
            )
            .execute()
        )
    except Exception as exc:  # noqa: BLE001 -- fail-soft
        log.warning(
            "dashboard_drive_reader: folder list failed folder=%s title=%s: %s",
            folder_id, title, exc,
        )
        return None
    files = resp.get("files", [])
    return files[0]["id"] if files else None


def newest_json_by_title(folder_id: str, title: str) -> dict[str, Any] | None:
    """Read + parse the newest same-titled JSON file in a folder. None on failure."""
    key = f"newest:{folder_id}:{title}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    file_id = newest_file_id_by_title(folder_id, title)
    if not file_id:
        return None
    data = read_json_by_id(file_id)
    if isinstance(data, dict) and data:  # never cache an empty/failed read
        _cache_set(key, data)
    return data
