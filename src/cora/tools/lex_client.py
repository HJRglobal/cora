"""Lexington Services Cora tools.

lex_revalidation_status
    Reads Asana task 1215070649606664 (AZ DDD Therapy Revalidation) + its
    subtasks and stories. Returns days-remaining to 2026-06-30, open sub-task
    blockers, and last-comment age. Designed for the Sunday-evening
    #lex-leadership brief and any in-thread revalidation question.

lex_staff_pulse
    Reads the most-recently modified files from the Sean/Jen DDD staffing +
    driver safety Drive folder (ID: 1uU-nHtEz5bFNu-JTV4k5BidkfmKAqVfG).
    Parses CSV and Excel uploads and returns a staffing summary.
"""

import csv
import io
import logging
import os
from datetime import date, datetime, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)

_BASE = "https://app.asana.com/api/1.0"
_WORKSPACE_GID = "682743441507584"  # HJR Global workspace
_TIMEOUT = 10.0

# -----------------------------------------------------------------------
# Deadline constant
# -----------------------------------------------------------------------
_REVALIDATION_DEADLINE = date(2026, 6, 30)
_REVALIDATION_TASK_GID = "1215070649606664"


class LexClientError(Exception):
    """Raised when an Asana or data-access call fails."""


def _pat() -> str:
    val = os.environ.get("ASANA_PAT", "")
    if not val:
        raise LexClientError("ASANA_PAT not set in environment -- Asana tool-use disabled")
    return val


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_pat()}"}


# -----------------------------------------------------------------------
# Asana helpers
# -----------------------------------------------------------------------

def _get_task(task_gid: str) -> dict[str, Any]:
    """Fetch a single task by GID with relevant fields."""
    params = {
        "opt_fields": ",".join([
            "name",
            "completed",
            "due_on",
            "notes",
            "permalink_url",
            "modified_at",
            "assignee.name",
        ]),
    }
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.get(f"{_BASE}/tasks/{task_gid}", params=params, headers=_headers())
    except httpx.RequestError as exc:
        raise LexClientError(f"Asana network error: {exc}") from exc

    if r.status_code == 404:
        raise LexClientError(f"Asana task {task_gid} not found (404)")
    if r.status_code == 401:
        raise LexClientError("Asana 401 -- PAT invalid or revoked")
    if r.status_code >= 400:
        raise LexClientError(f"Asana {r.status_code}: {r.text[:200]}")
    return r.json().get("data", {})


def _get_subtasks(task_gid: str) -> list[dict[str, Any]]:
    """Fetch subtasks of a task."""
    params = {
        "opt_fields": "name,completed,due_on,assignee.name,permalink_url",
        "limit": 50,
    }
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.get(
                f"{_BASE}/tasks/{task_gid}/subtasks",
                params=params,
                headers=_headers(),
            )
    except httpx.RequestError as exc:
        raise LexClientError(f"Asana network error fetching subtasks: {exc}") from exc

    if r.status_code >= 400:
        # Subtask fetch failure is non-fatal -- return empty
        log.warning("lex_client: subtask fetch returned %d for task %s", r.status_code, task_gid)
        return []
    return r.json().get("data", []) or []


def _get_latest_story(task_gid: str) -> dict[str, Any] | None:
    """Return the most recent comment/story on the task, or None."""
    params = {
        "opt_fields": "created_at,created_by.name,text,type",
        "limit": 10,
    }
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.get(
                f"{_BASE}/tasks/{task_gid}/stories",
                params=params,
                headers=_headers(),
            )
    except httpx.RequestError as exc:
        log.warning("lex_client: stories fetch error for task %s: %s", task_gid, exc)
        return None

    if r.status_code >= 400:
        log.warning("lex_client: stories fetch %d for task %s", r.status_code, task_gid)
        return None

    stories = r.json().get("data", []) or []
    # Comments have type="comment"; filter for those
    comments = [s for s in stories if s.get("type") == "comment"]
    return comments[-1] if comments else None


# -----------------------------------------------------------------------
# Public interface
# -----------------------------------------------------------------------

def get_revalidation_status() -> str:
    """Fetch AZ DDD Therapy Revalidation status from Asana and format for Slack.

    Returns a Slack mrkdwn string ready to be posted as-is.
    """
    try:
        task = _get_task(_REVALIDATION_TASK_GID)
    except LexClientError as exc:
        log.warning("lex_revalidation_status: task fetch failed: %s", exc)
        return "I don't have that right now."

    today = date.today()
    days_remaining = (_REVALIDATION_DEADLINE - today).days

    # Deadline marker
    if task.get("completed"):
        deadline_line = "REVALIDATION COMPLETE"
    elif days_remaining < 0:
        deadline_line = f"DEADLINE PASSED {abs(days_remaining)}d ago -- URGENT"
    elif days_remaining == 0:
        deadline_line = "DEADLINE IS TODAY -- URGENT"
    elif days_remaining <= 7:
        deadline_line = f"{days_remaining}d remaining to 6/30 -- CRITICAL"
    elif days_remaining <= 14:
        deadline_line = f"{days_remaining}d remaining to 6/30 -- HIGH"
    elif days_remaining <= 30:
        deadline_line = f"{days_remaining}d remaining to 6/30 -- WATCH"
    else:
        deadline_line = f"{days_remaining}d remaining to 6/30"

    # Emoji marker
    if days_remaining <= 7 and not task.get("completed"):
        marker = "CRITICAL"
    elif days_remaining <= 30 and not task.get("completed"):
        marker = "WARNING"
    else:
        marker = "OK" if task.get("completed") else "WATCH"

    lines = [
        f"*AZ DDD Therapy Revalidation* -- {deadline_line}",
        "",
    ]

    # Subtasks
    try:
        subtasks = _get_subtasks(_REVALIDATION_TASK_GID)
    except LexClientError:
        subtasks = []

    if subtasks:
        open_subs = [s for s in subtasks if not s.get("completed")]
        done_subs = [s for s in subtasks if s.get("completed")]

        if open_subs:
            lines.append(f"*Open blockers ({len(open_subs)}):*")
            for sub in open_subs[:8]:
                name = sub.get("name", "(unnamed)")
                due = sub.get("due_on") or "no due date"
                assignee = (sub.get("assignee") or {}).get("name") or "unassigned"
                link = sub.get("permalink_url") or ""
                if link:
                    lines.append(f"  - <{link}|{name}> -- {assignee}, due {due}")
                else:
                    lines.append(f"  - {name} -- {assignee}, due {due}")
            if len(open_subs) > 8:
                lines.append(f"  ... and {len(open_subs) - 8} more")
        else:
            lines.append("No open sub-task blockers.")

        lines.append(f"*Completed:* {len(done_subs)} of {len(subtasks)} sub-tasks done.")
    else:
        lines.append("No sub-tasks found on this task.")

    lines.append("")

    # Last comment age
    story = _get_latest_story(_REVALIDATION_TASK_GID)
    if story:
        raw_ts = story.get("created_at") or ""
        try:
            dt = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - dt).days
            author = (story.get("created_by") or {}).get("name") or "unknown"
            if age_days == 0:
                age_str = "today"
            elif age_days == 1:
                age_str = "yesterday"
            else:
                age_str = f"{age_days}d ago"
            lines.append(f"*Last comment:* {age_str} by {author}")
        except (ValueError, TypeError):
            lines.append("*Last comment:* (date parse error)")
    else:
        lines.append("*Last comment:* none on record")

    lines.append("")
    task_link = task.get("permalink_url") or ""
    if task_link:
        lines.append(f"<{task_link}|Open full task in Asana>")

    log.info(
        "lex_revalidation_status days_remaining=%d subtasks=%d open=%d",
        days_remaining,
        len(subtasks),
        len([s for s in subtasks if not s.get("completed")]) if subtasks else 0,
    )

    return "\n".join(lines)


_STAFF_PULSE_FOLDER_ID = "1uU-nHtEz5bFNu-JTV4k5BidkfmKAqVfG"

# Column-name synonyms for common staffing fields (case-insensitive substring match)
_COL_OPEN_POSITION = ("open position", "vacancy", "vacant", "unfilled", "opening")
_COL_TERMINATION   = ("terminat", "separated", "resigned", "left", "exit")
_COL_TRAINING      = ("training", "compliance", "certif", "expir")
_COL_STATUS        = ("status", "active", "inactive", "employed")
_COL_NAME          = ("name", "employee", "staff", "worker", "driver")


def _drive_service():
    """Build a Drive v3 service via direct SA credentials.

    Uses impersonate=False because the LEX staffing folder is shared directly
    with the SA email, not necessarily accessible via DWD as Harrison.
    """
    try:
        from ..connectors.drive_connector import _build_drive_service
        return _build_drive_service(impersonate=False)
    except ImportError as exc:
        raise LexClientError(f"Drive connector not available: {exc}") from exc
    except Exception as exc:
        raise LexClientError(f"Drive auth failed: {exc}") from exc


def _list_folder_files(service, folder_id: str) -> list[dict[str, Any]]:
    """Return files in the folder sorted newest-first (by modifiedTime)."""
    try:
        from googleapiclient.errors import HttpError
    except ImportError as exc:
        raise LexClientError(f"Google API client not available: {exc}") from exc

    try:
        resp = (
            service.files()
            .list(
                q=f"'{folder_id}' in parents and trashed=false",
                fields="files(id,name,mimeType,modifiedTime,size)",
                orderBy="modifiedTime desc",
                pageSize=20,
            )
            .execute()
        )
    except Exception as exc:
        raise LexClientError(f"Drive folder listing failed: {exc}") from exc

    return resp.get("files", [])


def _download_file_bytes(service, file_id: str, mime_type: str) -> bytes:
    """Download a file's raw bytes, exporting Google Sheets as CSV if needed."""
    try:
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaIoBaseDownload
    except ImportError as exc:
        raise LexClientError(f"Google API client not available: {exc}") from exc

    buf = io.BytesIO()
    try:
        if mime_type == "application/vnd.google-apps.spreadsheet":
            request = service.files().export_media(
                fileId=file_id,
                mimeType="text/csv",
            )
        elif mime_type == "application/vnd.google-apps.document":
            request = service.files().export_media(
                fileId=file_id,
                mimeType="text/plain",
            )
        else:
            request = service.files().get_media(fileId=file_id)
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    except Exception as exc:
        raise LexClientError(f"Drive download failed for {file_id}: {exc}") from exc

    return buf.getvalue()


def _col_index(headers: list[str], synonyms: tuple[str, ...]) -> int | None:
    """Return the first column index whose header contains any synonym (case-insensitive)."""
    for i, h in enumerate(headers):
        h_lower = h.lower().replace("\n", " ")
        if any(s in h_lower for s in synonyms):
            return i
    return None


_TODAY = None  # Refreshed per-call via _today()

def _today() -> date:
    return date.today()


def _compliance_status(val: str) -> str:
    """Classify a compliance cell value as 'current', 'expired', or 'missing'.

    LEX spreadsheets use three conventions:
      - Dates (mm/dd/yyyy or mm/dd/yy): future = current, past = expired
      - 'x' or 'X': checkbox-style compliant marker
      - 'NEED', 'NDD', 'N/A', 'pending': missing / not done
    """
    v = val.strip()
    if not v:
        return "missing"
    vl = v.lower()
    if vl in ("x",):
        return "current"
    if vl in ("need", "ndd", "n/a", "na", "pending", "tbd"):
        return "missing"
    # Try to parse as a date
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            d = datetime.strptime(v, fmt).date()
            return "current" if d >= _today() else "expired"
        except ValueError:
            continue
    # Unknown value — treat as current if non-empty to avoid false alarms
    return "current"


def _parse_csv_bytes(raw: bytes, filename: str) -> str:
    """Parse CSV bytes and return a human-readable summary."""
    try:
        text = raw.decode("utf-8-sig", errors="replace")
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
    except Exception as exc:
        return f"Could not parse {filename}: {exc}"

    if not rows:
        return f"{filename}: empty file"

    headers = [h.strip() for h in rows[0]]
    data = rows[1:]
    total_rows = len(data)

    if total_rows == 0:
        return f"{filename}: header only, no data rows"

    lines = [f"*{filename}* — {total_rows} records"]

    # Find and summarize key columns
    name_col   = _col_index(headers, _COL_NAME)
    status_col = _col_index(headers, _COL_STATUS)
    train_col  = _col_index(headers, _COL_TRAINING)
    term_col   = _col_index(headers, _COL_TERMINATION)
    open_col   = _col_index(headers, _COL_OPEN_POSITION)

    # Active vs inactive count
    if status_col is not None:
        statuses = [r[status_col].strip().lower() for r in data if len(r) > status_col]
        active = sum(1 for s in statuses if "active" in s or "employed" in s or s == "yes" or s == "1")
        inactive = sum(1 for s in statuses if "inactive" in s or "terminat" in s or s == "no" or s == "0")
        lines.append(f"  Active: {active}  |  Inactive/termed: {inactive}")

    # Open positions
    if open_col is not None:
        open_vals = [r[open_col].strip() for r in data if len(r) > open_col]
        open_count = sum(1 for v in open_vals if v.lower() in ("yes", "true", "1", "open", "vacant"))
        if open_count:
            lines.append(f"  Open positions: {open_count}")

    # Recent terminations — rows where term_col is non-empty
    if term_col is not None:
        termed = [r for r in data if len(r) > term_col and r[term_col].strip() and r[term_col].strip().lower() not in ("", "no", "false", "0")]
        if termed:
            lines.append(f"  Recent terminations/separations: {len(termed)}")
            if name_col is not None:
                for r in termed[:5]:
                    n = r[name_col].strip() if len(r) > name_col else "?"
                    t = r[term_col].strip()
                    lines.append(f"    - {n}: {t}")

    # Training compliance — handles dates, "NEED"/"NDD"/"x" (LEX spreadsheet conventions)
    if train_col is not None:
        train_vals = [r[train_col].strip() for r in data if len(r) > train_col]
        statuses = [_compliance_status(v) for v in train_vals]
        compliant = statuses.count("current")
        expired   = statuses.count("expired")
        missing   = statuses.count("missing")
        parts = []
        if compliant:
            parts.append(f"{compliant} current")
        if expired:
            parts.append(f"{expired} expired")
        if missing:
            parts.append(f"{missing} missing/needed")
        if parts:
            lines.append(f"  Training/compliance: {', '.join(parts)}")

    # If no known columns found, show column names so Harrison can update the parser
    if all(c is None for c in [name_col, status_col, train_col, term_col, open_col]):
        lines.append(f"  Columns found: {', '.join(headers[:10])}")
        lines.append("  (No standard staffing columns detected — update _COL_* mappings in lex_client.py)")

    return "\n".join(lines)


def _parse_excel_bytes(raw: bytes, filename: str) -> str:
    """Parse Excel bytes and return a human-readable summary."""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        wb.close()
    except Exception as exc:
        return f"Could not parse Excel {filename}: {exc}"

    if not rows:
        return f"{filename}: empty workbook"

    # Convert to list of string rows — same path as CSV parser
    str_rows = [[str(c) if c is not None else "" for c in row] for row in rows]
    # Write to an in-memory CSV and re-use the CSV parser
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerows(str_rows)
    return _parse_csv_bytes(buf.getvalue().encode("utf-8"), filename)


_PARSEABLE_MIMES = {
    "text/csv",
    "text/plain",
    "application/csv",
    "application/vnd.google-apps.spreadsheet",
    "application/vnd.google-apps.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
}


def _parse_text_bytes(raw: bytes, filename: str) -> str:
    """Return a truncated plain-text summary for Google Docs and text files."""
    try:
        text = raw.decode("utf-8-sig", errors="replace").strip()
    except Exception as exc:
        return f"Could not read {filename}: {exc}"
    if not text:
        return f"{filename}: empty document"
    # Return first 800 chars — enough for Claude to summarize the doc's purpose
    preview = text[:800].replace("\r\n", "\n")
    truncated = " _(truncated)_" if len(text) > 800 else ""
    return f"*{filename}*\n{preview}{truncated}"


def get_staff_pulse() -> str:
    """Return LEX staffing pulse from the Sean/Jen Drive upload folder.

    Reads the most-recently modified files from the staffing Drive folder,
    parses CSV and Excel uploads, and returns a staffing summary.
    """
    log.info("lex_staff_pulse: reading Drive folder %s", _STAFF_PULSE_FOLDER_ID)

    try:
        service = _drive_service()
    except LexClientError as exc:
        log.warning("lex_staff_pulse: drive auth failed: %s", exc)
        return "I don't have that right now — Drive credentials are not available."

    try:
        files = _list_folder_files(service, _STAFF_PULSE_FOLDER_ID)
    except LexClientError as exc:
        log.warning("lex_staff_pulse: folder listing failed: %s", exc)
        return "I don't have that right now — couldn't read the staffing folder."

    log.info("lex_staff_pulse: folder %s returned %d file(s)", _STAFF_PULSE_FOLDER_ID, len(files))
    if files:
        log.info("lex_staff_pulse: files=%s", [(f.get("name"), f.get("mimeType")) for f in files[:5]])

    if not files:
        return (
            "The LEX staffing folder exists but contains no files yet. "
            "Ask Sean or Jen to upload a DDD staffing report or driver safety CSV."
        )

    parseable = [f for f in files if f.get("mimeType") in _PARSEABLE_MIMES]
    if not parseable:
        names = [f.get("name", "?") for f in files[:5]]
        return (
            f"The staffing folder has {len(files)} file(s) but none are CSV or spreadsheet format. "
            f"Files found: {', '.join(names)}. "
            "Ask Sean or Jen to upload CSV or Excel files."
        )

    summaries: list[str] = []
    # Parse up to 5 most-recent parseable files
    for f in parseable[:5]:
        fid   = f["id"]
        fname = f.get("name", fid)
        fmime = f.get("mimeType", "")
        mtime = f.get("modifiedTime", "")[:10]  # YYYY-MM-DD

        try:
            raw = _download_file_bytes(service, fid, fmime)
        except LexClientError as exc:
            summaries.append(f"*{fname}* — could not download: {exc}")
            continue

        if fmime in (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.ms-excel",
        ):
            summaries.append(_parse_excel_bytes(raw, fname) + f" _(updated {mtime})_")
        elif fmime == "application/vnd.google-apps.document":
            summaries.append(_parse_text_bytes(raw, fname) + f" _(updated {mtime})_")
        else:
            # CSV, text/plain, Google Sheets (exported as CSV)
            summaries.append(_parse_csv_bytes(raw, fname) + f" _(updated {mtime})_")

    if not summaries:
        return "I don't have that right now — could not read any files from the staffing folder."

    header = "*LEX Staffing Pulse*\n"
    log.info("lex_staff_pulse: parsed %d file(s) from folder", len(summaries))
    return header + "\n\n".join(summaries)
