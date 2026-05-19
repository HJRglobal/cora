"""Google Calendar v3 client — read-only, Service Account + Domain-wide Delegation.

Phase 2 #9 part 2 scope:
- Single endpoint: events.list (per-user, via DWD impersonation)
- Service Account credentials from .credentials/cora-calendar-sa.json
- DWD impersonation: with_subject(user_email) — service account acts AS the user
- No write methods (read-only by design)

Architecture:
- Service Account `cora-calendar@cora-calendar-readonly.iam.gserviceaccount.com`
- Unique ID 108247979419622966179 (registered in Workspace admin DWD)
- Authorized scope: https://www.googleapis.com/auth/calendar.readonly
- Service account impersonates the asking Slack user's Google identity to read
  THEIR calendar (not the service account's own — service accounts have no calendar).

Deep-link pattern: Google's `htmlLink` field returned per event. Looks like:
  https://www.google.com/calendar/event?eid=...
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

log = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
_DEFAULT_MAX_EVENTS = 25
# Default to America/Phoenix — Harrison + HJR portfolio is AZ-based
_DEFAULT_TZ = "America/Phoenix"


class CalendarClientError(Exception):
    """Raised when a Calendar API call fails."""


def _service_account_path() -> str:
    val = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not val:
        raise CalendarClientError(
            "GOOGLE_SERVICE_ACCOUNT_JSON not set in environment — Calendar tool-use disabled"
        )
    if not os.path.exists(val):
        raise CalendarClientError(
            f"GOOGLE_SERVICE_ACCOUNT_JSON path does not exist: {val}"
        )
    return val


def _build_service(user_email: str):
    """Build a Calendar service that impersonates user_email via Domain-wide Delegation."""
    try:
        creds = service_account.Credentials.from_service_account_file(
            _service_account_path(),
            scopes=_SCOPES,
        )
    except Exception as exc:
        raise CalendarClientError(
            f"Failed to load service account credentials: {exc}"
        ) from exc

    delegated = creds.with_subject(user_email)
    return build("calendar", "v3", credentials=delegated, cache_discovery=False)


def _parse_when(when: str) -> tuple[datetime, datetime, str]:
    """Resolve a 'when' parameter into (time_min, time_max, label).

    Accepts:
      - "today"      → today 00:00 → today 23:59:59 AZ
      - "tomorrow"   → tomorrow 00:00 → tomorrow 23:59:59 AZ
      - "this_week"  → now → 7 days from now
      - "next_week"  → today + 7 days → today + 14 days
      - "YYYY-MM-DD" → that day, 00:00 → 23:59:59 AZ

    All returned datetimes are timezone-aware (UTC, suitable for Calendar API timeMin/timeMax).
    Label is a human-readable description of the window for the tool result.
    """
    # We'll do day-arithmetic in Phoenix time, then convert to UTC for the API.
    # Phoenix is UTC-7 year-round (no DST).
    phoenix_offset = timedelta(hours=-7)
    now_az = datetime.now(timezone(phoenix_offset))
    when = (when or "today").strip().lower()

    if when in ("today", ""):
        start_az = now_az.replace(hour=0, minute=0, second=0, microsecond=0)
        end_az = start_az + timedelta(days=1) - timedelta(seconds=1)
        label = f"today ({start_az.strftime('%Y-%m-%d')})"
    elif when == "tomorrow":
        start_az = (now_az + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end_az = start_az + timedelta(days=1) - timedelta(seconds=1)
        label = f"tomorrow ({start_az.strftime('%Y-%m-%d')})"
    elif when == "this_week":
        start_az = now_az
        end_az = now_az + timedelta(days=7)
        label = "the next 7 days"
    elif when == "next_week":
        start_az = (now_az + timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
        end_az = start_az + timedelta(days=7)
        label = "next week (7 days starting " + start_az.strftime("%Y-%m-%d") + ")"
    else:
        # Try YYYY-MM-DD
        try:
            day = datetime.strptime(when, "%Y-%m-%d").replace(tzinfo=timezone(phoenix_offset))
        except ValueError as exc:
            raise CalendarClientError(
                f"Unrecognized 'when' value: {when!r}. Accepts: today, tomorrow, this_week, "
                f"next_week, or YYYY-MM-DD."
            ) from exc
        start_az = day.replace(hour=0, minute=0, second=0, microsecond=0)
        end_az = start_az + timedelta(days=1) - timedelta(seconds=1)
        label = day.strftime("%Y-%m-%d")

    # Convert to UTC for the Calendar API (RFC 3339)
    time_min = start_az.astimezone(timezone.utc)
    time_max = end_az.astimezone(timezone.utc)
    return time_min, time_max, label


def get_user_events(
    user_email: str,
    when: str = "today",
    max_events: int = _DEFAULT_MAX_EVENTS,
) -> tuple[list[dict[str, Any]], str]:
    """Fetch upcoming events from a user's primary calendar within a window.

    Returns (events_list, window_label). user_email must be a Google Workspace user
    whose domain is authorized via Domain-wide Delegation.

    Raises CalendarClientError on auth / network / API failure.
    """
    time_min, time_max, label = _parse_when(when)

    try:
        service = _build_service(user_email)
        result = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=time_min.isoformat().replace("+00:00", "Z"),
                timeMax=time_max.isoformat().replace("+00:00", "Z"),
                singleEvents=True,
                orderBy="startTime",
                maxResults=max_events,
            )
            .execute()
        )
    except HttpError as exc:
        status = exc.resp.status if exc.resp else "?"
        if status == 403:
            raise CalendarClientError(
                f"Calendar 403 for {user_email} — service account lacks delegation for this "
                f"user's domain, or user not in Workspace. Harrison may need to add the "
                f"domain to Domain-wide Delegation in admin.google.com."
            ) from exc
        if status == 404:
            raise CalendarClientError(
                f"Calendar 404 for {user_email} — user has no primary calendar or doesn't exist."
            ) from exc
        raise CalendarClientError(f"Calendar API HTTP {status}: {exc}") from exc
    except Exception as exc:
        raise CalendarClientError(f"Calendar API error: {exc}") from exc

    return result.get("items", []) or [], label


def format_events_for_llm(events: list[dict[str, Any]], window_label: str) -> str:
    """Render event list as a string suitable for a tool_result content block.

    Each event title wrapped in Slack mrkdwn hyperlink syntax `<htmlLink|title>`.
    Tool consumer (Claude) should preserve those links verbatim in user-facing replies.
    """
    if not events:
        return f"No calendar events found for {window_label}."

    lines = [f"Found {len(events)} calendar event(s) for {window_label}:"]
    lines.append(
        "(Event titles below are Slack-formatted hyperlinks — preserve the `<url|name>` "
        "syntax verbatim in your reply so the user can click through to open in Google Calendar.)"
    )

    for e in events:
        title = e.get("summary") or "(no title)"
        html_link = e.get("htmlLink", "")

        # Start time — could be date-only (all-day) or dateTime
        start = e.get("start") or {}
        end = e.get("end") or {}
        if "dateTime" in start:
            # Timed event — format as local hour:min
            try:
                dt = datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00"))
                # Convert to Phoenix for display
                phoenix_tz = timezone(timedelta(hours=-7))
                dt_az = dt.astimezone(phoenix_tz)
                start_str = dt_az.strftime("%Y-%m-%d %H:%M AZ")
            except Exception:
                start_str = start["dateTime"]
        elif "date" in start:
            start_str = f"{start['date']} (all-day)"
        else:
            start_str = "(no start time)"

        # Duration
        duration_str = ""
        if "dateTime" in start and "dateTime" in end:
            try:
                s = datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00"))
                ed = datetime.fromisoformat(end["dateTime"].replace("Z", "+00:00"))
                dur_min = int((ed - s).total_seconds() / 60)
                duration_str = f" ({dur_min}min)"
            except Exception:
                pass

        # Attendees (up to 4 names)
        attendees_raw = e.get("attendees") or []
        attendee_names = []
        for a in attendees_raw[:4]:
            name = a.get("displayName") or a.get("email", "")
            if name:
                attendee_names.append(name)
        more = len(attendees_raw) - 4
        attendees_str = ""
        if attendee_names:
            attendees_str = f" — with {', '.join(attendee_names)}"
            if more > 0:
                attendees_str += f" +{more} more"

        # Location preview
        location = e.get("location") or ""
        location_str = f" @ {location[:60]}" if location else ""

        # Title with link
        title_with_link = f"<{html_link}|{title}>" if html_link else title

        lines.append(
            f"- [{start_str}{duration_str}] {title_with_link}{attendees_str}{location_str}"
        )

    return "\n".join(lines)
