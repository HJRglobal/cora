"""
run_retroactive_hashtag_scan.py

One-time retroactive scan: checks each fighter's Instagram account for posts
containing F3 hashtags (#DrinkF3, #F3Energy, #DrinkF3Energy) since June 1, 2026.

For any fighter with a qualifying post:
- Finds their row in the "June 2026" tab of the fighter tracker sheet
- Writes the earliest qualifying post date to column C (Hard Post)

Works for Business/Creator IG accounts only (Personal accounts are skipped —
they need to switch account type first, per the separate TOM note).

Usage:
    cd C:\\Users\\Harri\\code\\cora
    .venv\\Scripts\\python.exe scripts\\run_retroactive_hashtag_scan.py
    .venv\\Scripts\\python.exe scripts\\run_retroactive_hashtag_scan.py --dry-run

Flags:
    --dry-run   Print what would be written without touching the sheet.
"""

import argparse
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import urllib.parse

import requests

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from dotenv import load_dotenv

load_dotenv(_REPO_ROOT / ".env", override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_GRAPH_BASE = "https://graph.facebook.com/v19.0"

# Only count posts on or after this date
_SINCE_ISO = "2026-06-01T00:00:00+00:00"

# Fighter tracker Google Sheet
_SHEET_ID = "1tPpsdUrvXaYq7Cz77L5yYwEC6plptO_xcGY3JncPK28"
_SHEET_TAB = "June 2026"

# Hashtags to match (lowercase, without #). A post qualifies if any appear in its caption.
_F3_HASHTAGS = {"drinkf3", "f3energy", "drinkf3energy"}

# Rate-limit: Instagram Business Discovery API allows ~200 calls/hour.
# We have up to 62 fighters so one call each is fine; adding a small sleep anyway.
_API_SLEEP_S = 0.5

# ---------------------------------------------------------------------------
# Env vars
# ---------------------------------------------------------------------------

_IG_USER_ID = os.environ.get("INSTAGRAM_F3E_USER_ID", "")
_IG_TOKEN = os.environ.get("INSTAGRAM_F3E_ACCESS_TOKEN", "")
_SA_PATH = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
_IMPERSONATE = "harrison@hjrglobal.com"


# ---------------------------------------------------------------------------
# Instagram: Business Discovery API
# ---------------------------------------------------------------------------

def _get_fighter_media(handle: str) -> list[dict]:
    """
    Fetch a fighter's recent public IG media via the Business Discovery API.
    Returns a list of media objects: {id, caption, timestamp, permalink, media_type}.
    Returns [] for Personal accounts or any error (logged at debug level).

    NOTE: We must build the URL manually and keep curly braces unencoded.
    requests.get(params=...) percent-encodes { } which breaks IG's field parser
    and causes it to drop the username param entirely (error: "username required").
    """
    fields = (
        "business_discovery.as(user)"
        "{media.limit(50){id,caption,timestamp,permalink,media_type}}"
    )
    # Encode everything EXCEPT curly braces — IG's field parser requires bare { }
    encoded_fields = urllib.parse.quote(fields, safe="{},.")
    encoded_handle = urllib.parse.quote(handle, safe="")
    encoded_token  = urllib.parse.quote(_IG_TOKEN, safe="")
    url = (
        f"{_GRAPH_BASE}/{_IG_USER_ID}"
        f"?fields={encoded_fields}"
        f"&username={encoded_handle}"
        f"&access_token={encoded_token}"
    )
    try:
        resp = requests.get(url, timeout=20)
        data = resp.json()
        if "error" in data:
            err = data["error"]
            code = err.get("code")
            msg = err.get("message", "")
            err_type = err.get("type", "")
            # Personal account: code 100, OAuthException, message contains "Invalid user id"
            if code == 100 and "OAuthException" in err_type and "Invalid user id" in msg:
                log.info("  @%s: Personal account — skipping (needs to switch to Business/Creator)", handle)
            else:
                # Log full error so we can diagnose permission / token issues
                log.warning(
                    "  @%s: API error code=%s type=%s — %s",
                    handle, code, err_type, msg[:200],
                )
            return []
        # Navigate: data -> user -> media -> data
        user_node = data.get("user") or {}
        media_node = user_node.get("media") or {}
        return media_node.get("data") or []
    except Exception as exc:
        log.warning("  @%s: request exception — %s", handle, exc)
        return []


def _has_f3_hashtag(caption: str) -> bool:
    """Return True if caption contains any F3 campaign hashtag."""
    caption_lower = caption.lower()
    return any(f"#{tag}" in caption_lower for tag in _F3_HASHTAGS)


# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------

def _get_sheets_service():
    """Build a Google Sheets API service using the Cora service account."""
    from google.oauth2 import service_account  # type: ignore
    from googleapiclient.discovery import build  # type: ignore

    # Use only the Drive scope — same as drive_connector.py.
    # This is what's configured for DWD in Google Workspace Admin.
    # Drive scope is a superset that includes Sheets read/write access.
    scopes = ["https://www.googleapis.com/auth/drive"]
    creds = service_account.Credentials.from_service_account_file(
        _SA_PATH, scopes=scopes
    ).with_subject(_IMPERSONATE)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _read_sheet_rows(service) -> list[list]:
    """Read columns A–E rows 1–58 from the June 2026 tab."""
    result = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=_SHEET_ID,
            range=f"'{_SHEET_TAB}'!A1:E58",
        )
        .execute()
    )
    return result.get("values", [])


def _write_cell(service, row_1indexed: int, col_letter: str, value: str, dry_run: bool):
    """Write a single cell. row_1indexed is 1-based sheet row number."""
    cell = f"'{_SHEET_TAB}'!{col_letter}{row_1indexed}"
    if dry_run:
        log.info("    DRY RUN — would write '%s' -> %s", value, cell)
        return
    service.spreadsheets().values().update(
        spreadsheetId=_SHEET_ID,
        range=cell,
        valueInputOption="USER_ENTERED",
        body={"values": [[value]]},
    ).execute()
    log.info("    Wrote '%s' -> %s", value, cell)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(dry_run: bool = False) -> None:
    # Validate env
    missing = [v for v in ("_IG_USER_ID", "_IG_TOKEN", "_SA_PATH") if not globals()[v]]
    if missing:
        log.error("Missing required env vars: %s", missing)
        sys.exit(1)

    since_dt = datetime.fromisoformat(_SINCE_ISO)
    log.info("Retroactive hashtag scan — since %s", _SINCE_ISO)
    log.info("Hashtags: %s", ", ".join(f"#{t}" for t in _F3_HASHTAGS))
    log.info("Dry run: %s", dry_run)

    # Load all fighters from DB
    db_path = _REPO_ROOT / "data" / "influencer_tracker.db"
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    fighters = db.execute(
        "SELECT athlete_name, handle FROM influencer_handles WHERE platform='instagram' ORDER BY athlete_name"
    ).fetchall()
    db.close()
    log.info("Loaded %d fighters from DB\n", len(fighters))

    # Load sheet
    sheets = _get_sheets_service()
    rows = _read_sheet_rows(sheets)

    # Map: lowercase handle (no @) -> sheet row number (1-based)
    handle_to_row: dict[str, int] = {}
    for i, row in enumerate(rows, start=1):
        if len(row) >= 2 and row[1]:
            key = row[1].lstrip("@").lower().strip()
            handle_to_row[key] = i
    log.info("Sheet: %d fighter rows mapped in '%s'\n", len(handle_to_row), _SHEET_TAB)

    # Scan each fighter
    found: list[dict] = []
    skipped_personal: list[str] = []
    no_posts: list[str] = []

    for fighter in fighters:
        name = fighter["athlete_name"]
        handle = fighter["handle"].lower().strip()
        log.info("Scanning @%s (%s)...", handle, name)

        media = _get_fighter_media(handle)
        time.sleep(_API_SLEEP_S)

        if not media:
            skipped_personal.append(f"@{handle} ({name})")
            continue

        # Find qualifying posts since June 1
        qualifying = []
        for post in media:
            ts = post.get("timestamp", "")
            caption = post.get("caption") or ""
            if not ts:
                continue
            try:
                post_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                continue
            if post_dt < since_dt:
                continue
            if _has_f3_hashtag(caption):
                qualifying.append({
                    "post_dt": post_dt,
                    "permalink": post.get("permalink", ""),
                    "caption_snippet": caption[:120],
                })

        if not qualifying:
            log.info("  No F3 hashtag posts since June 1")
            no_posts.append(f"@{handle} ({name})")
            continue

        # Earliest qualifying post
        qualifying.sort(key=lambda x: x["post_dt"])
        first = qualifying[0]
        # Format as M/D/YYYY (no leading zeros) — cross-platform safe
        post_dt = first["post_dt"]
        date_str = f"{post_dt.month}/{post_dt.day}/{post_dt.year}"  # e.g. 6/5/2026

        log.info(
            "  FOUND %d qualifying post(s). Earliest: %s",
            len(qualifying), date_str,
        )
        log.info("  URL: %s", first["permalink"])  # noqa: E501
        log.info("  Caption: %s...", first["caption_snippet"])

        # Find row in sheet
        sheet_row = handle_to_row.get(handle)
        if sheet_row is None:
            log.warning("  @%s not found in sheet — cannot write", handle)
            found.append({"name": name, "handle": handle, "date": date_str, "written": False, "note": "not in sheet"})
            continue

        # Check if col C already has a value
        row_data = rows[sheet_row - 1]
        existing_c = row_data[2] if len(row_data) > 2 else ""
        if existing_c:
            log.info("  Col C already has '%s' — skipping write", existing_c)
            found.append({"name": name, "handle": handle, "date": date_str, "written": False, "note": f"col C already: {existing_c}"})
            continue

        _write_cell(sheets, sheet_row, "C", date_str, dry_run)
        found.append({"name": name, "handle": handle, "date": date_str, "written": not dry_run})

    # Summary
    print("\n" + "=" * 60)
    print("RETROACTIVE HASHTAG SCAN COMPLETE")
    print("=" * 60)

    written = [r for r in found if r.get("written")]
    found_not_written = [r for r in found if not r.get("written")]

    print(f"\nFighters scanned:          {len(fighters)}")
    print(f"Personal accounts skipped: {len(skipped_personal)}")
    print(f"No qualifying posts:       {len(no_posts)}")
    print(f"Posts found + written:     {len(written)}")
    print(f"Posts found, not written:  {len(found_not_written)}")

    if written:
        print("\n✅ Written to sheet:")
        for r in written:
            print(f"   {r['name']} (@{r['handle']}): {r['date']}")

    if found_not_written:
        print("\n⚠️  Found but NOT written:")
        for r in found_not_written:
            print(f"   {r['name']} (@{r['handle']}): {r['date']} — {r.get('note','')}")

    if skipped_personal:
        print(f"\n🔒 Personal accounts skipped ({len(skipped_personal)}):")
        for s in skipped_personal:
            print(f"   {s}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Retroactive F3 hashtag scan for fighter roster")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be written without touching the sheet",
    )
    args = parser.parse_args()
    run(dry_run=args.dry_run)
