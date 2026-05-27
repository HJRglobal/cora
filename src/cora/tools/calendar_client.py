"""Google Calendar v3 client — read + write, Service Account + Domain-wide Delegation.

Phase 2 #9 part 2 scope:
- Read endpoint: events.list (per-user, via DWD impersonation)
- Write endpoint: events.insert (staged-write pattern — confirmed=True gate)

Write tool added 2026-05-23:
- create_event() mirrors gmail_client.create_draft() staged-write pattern.
- DWD impersonation: event lands in the asker's own primary calendar.
- Required DWD scope: https://www.googleapis.com/auth/calendar.events
  (supersedes calendar.readonly — Harrison must add this scope in
  admin.google.com → Security → API controls → Domain-wide Delegation,
  same DWD entry as gmail.compose).

Architecture:
- Service Account `cora-calendar@cora-calendar-readonly.iam.gserviceaccount.com`
- Unique ID 117814221557902200858 (registered in Workspace admin DWD)
- Authorized scopes: https://www.googleapis.com/auth/calendar.events
  (calendar.events is a superset of calendar.readonly — one scope covers both)
- Service account impersonates the asking Slack user's Google identity.

Deep-link pattern: Google's `htmlLink` field returned per event. Looks like:
  https://www.google.com/calendar/event?eid=...

Write doctrine (mirrors gmail_create_draft / asana_create_task):
- Cora shows a preview block first and requires confirmed=True before calling.
- Audit log: asker / event_id / summary / attendee_count / start_datetime.
  Event body / description is NOT logged.
- Default time zone: America/Phoenix (AZ — no DST).
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

log = logging.getLogger(__name__)

# calendar.events is a superset of calendar.readonly — covers both read + write.
_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
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


# ---------------------------------------------------------------------------
# Write — create_event (staged-write, confirmed=True gate)
# ---------------------------------------------------------------------------

def _parse_datetime_input(value: str, tz_name: str = _DEFAULT_TZ) -> str:
    """Accept a datetime string in several common formats and return RFC 3339.

    Accepted inputs:
      - "2026-05-25T14:00" or "2026-05-25T14:00:00"  (naive — treated as tz_name)
      - "2026-05-25T14:00:00-07:00"                   (already offset-aware — returned as-is)
      - "2026-05-25 14:00"                             (space separator — normalised)

    Always returns a string like "2026-05-25T14:00:00-07:00".
    Raises CalendarClientError on unrecognisable input.
    """
    value = value.strip().replace(" ", "T")

    # Try offset-aware parse first (Python 3.7+ fromisoformat handles +HH:MM)
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is not None:
            return dt.isoformat()
        # Naive — apply the requested timezone
    except ValueError:
        raise CalendarClientError(
            f"Cannot parse datetime {value!r}. Use ISO format, e.g. '2026-05-25T14:00' "
            f"or '2026-05-25T14:00:00-07:00'."
        )

    # Phoenix is UTC-7 year-round (no DST)
    tz_offset = timedelta(hours=-7) if "Phoenix" in tz_name else timedelta(hours=0)
    tz = timezone(tz_offset)
    return dt.replace(tzinfo=tz).isoformat()


def create_event(
    *,
    user_email: str,
    summary: str,
    start: str,
    end: str,
    attendees: list[str] | None = None,
    description: str | None = None,
    location: str | None = None,
    time_zone: str = _DEFAULT_TZ,
) -> dict[str, Any]:
    """Create a Calendar event in user_email's primary calendar.

    Parameters
    ----------
    user_email   : Google Workspace email to impersonate (DWD).
    summary      : Event title.
    start        : Start datetime — ISO 8601, e.g. "2026-05-25T14:00" or
                   "2026-05-25T14:00:00-07:00". Naive datetimes treated as time_zone.
    end          : End datetime — same format as start.
    attendees    : Optional list of email addresses. Invites are sent by Google
                   if notification settings allow.
    description  : Optional free-text event body.
    location     : Optional location string.
    time_zone    : IANA tz name — default "America/Phoenix".

    Returns the created event resource dict (includes `id` and `htmlLink`).
    Raises CalendarClientError on validation or API failure.
    """
    if not summary or not summary.strip():
        raise CalendarClientError("create_event requires a non-empty summary (title).")
    if not start:
        raise CalendarClientError("create_event requires a start datetime.")
    if not end:
        raise CalendarClientError("create_event requires an end datetime.")

    start_rfc = _parse_datetime_input(start, time_zone)
    end_rfc = _parse_datetime_input(end, time_zone)

    # Validate end > start
    try:
        start_dt = datetime.fromisoformat(start_rfc)
        end_dt = datetime.fromisoformat(end_rfc)
        if end_dt <= start_dt:
            raise CalendarClientError(
                f"Event end ({end_rfc}) must be after start ({start_rfc})."
            )
    except CalendarClientError:
        raise
    except Exception:
        pass  # fromisoformat edge-case; let the API catch it

    body: dict[str, Any] = {
        "summary": summary.strip(),
        "start": {"dateTime": start_rfc, "timeZone": time_zone},
        "end": {"dateTime": end_rfc, "timeZone": time_zone},
    }
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    if attendees:
        # Validate + de-dup
        clean: list[str] = []
        for addr in attendees:
            addr = (addr or "").strip()
            if not addr:
                continue
            if "@" not in addr:
                raise CalendarClientError(
                    f"Attendee {addr!r} doesn't look like an email address."
                )
            clean.append(addr)
        if clean:
            body["attendees"] = [{"email": a} for a in clean]

    try:
        service = _build_service(user_email)
        event = (
            service.events()
            .insert(calendarId="primary", body=body, sendUpdates="all")
            .execute()
        )
    except HttpError as exc:
        status = exc.resp.status if exc.resp else "?"
        if status == 403:
            raise CalendarClientError(
                f"Calendar 403 for {user_email} — service account lacks "
                f"calendar.events DWD scope. Harrison needs to update Domain-wide "
                f"Delegation in admin.google.com: replace calendar.readonly with "
                f"https://www.googleapis.com/auth/calendar.events for the SA "
                f"(Unique ID 108247979419622966179)."
            ) from exc
        if status == 400:
            raise CalendarClientError(
                f"Calendar 400 — API rejected the event body: {exc}"
            ) from exc
        raise CalendarClientError(f"Calendar API HTTP {status}: {exc}") from exc
    except CalendarClientError:
        raise
    except Exception as exc:
        raise CalendarClientError(f"Calendar API error: {exc}") from exc

    return event


def format_created_event_for_llm(
    event: dict[str, Any],
    *,
    user_email: str,
) -> str:
    """Render a freshly-created event as a Slack-mrkdwn confirmation block."""
    event_id = event.get("id") or "(no id)"
    html_link = event.get("htmlLink") or ""
    summary = event.get("summary") or "(no title)"
    start_raw = (event.get("start") or {}).get("dateTime") or ""
    attendees_raw = event.get("attendees") or []

    # Format start for display
    start_display = start_raw
    if start_raw:
        try:
            dt = datetime.fromisoformat(start_raw)
            phoenix_tz = timezone(timedelta(hours=-7))
            start_display = dt.astimezone(phoenix_tz).strftime("%a %Y-%m-%d %H:%M AZ")
        except Exception:
            pass

    attendee_list = [a.get("email", "") for a in attendees_raw if a.get("email")]
    attendees_str = (
        f"\n- Attendees: {', '.join(attendee_list)}" if attendee_list else ""
    )

    link_str = f"<{html_link}|Open in Google Calendar>" if html_link else "(no link)"

    return (
        f"Calendar event CREATED in {user_email}'s primary calendar. Surface this to the user:\n"
        f"- Title: {summary}\n"
        f"- Start: {start_display}{attendees_str}\n"
        f"- Event ID: {event_id}\n"
        f"- {link_str}\n"
        f"\n"
        f"Tell the user the event is live on their calendar. Format the Open link as a "
        f"Slack hyperlink (preserve the <url|name> syntax). If attendees were invited, "
        f"mention that Google sent them invitations."
    )


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


# ---------------------------------------------------------------------------
# freebusy query + meeting-slot finder
# ---------------------------------------------------------------------------

_WORK_START_HOUR = 9    # 9 AM America/Phoenix
_WORK_END_HOUR   = 17   # 5 PM America/Phoenix
_SLOT_STEP_MIN   = 15   # 15-minute granularity
_PHOENIX_TZ      = timezone(timedelta(hours=-7))  # Arizona never observes DST


def get_free_busy(
    requester_email: str,
    calendar_emails: list[str],
    time_min: datetime,
    time_max: datetime,
) -> dict[str, list[tuple[datetime, datetime]]]:
    """Query the Google Calendar freebusy API for multiple calendars.

    Returns {email: [(busy_start_utc, busy_end_utc), ...]} for each email.
    Uses DWD impersonation as requester_email so only one service build is needed.

    If a calendar returns API errors (e.g. user not in domain), that calendar is
    treated as *fully busy* for the window -- safe over-approximation avoids
    double-booking at the cost of possibly missing a slot.
    """
    body: dict[str, Any] = {
        "timeMin": time_min.isoformat().replace("+00:00", "Z"),
        "timeMax": time_max.isoformat().replace("+00:00", "Z"),
        "timeZone": _DEFAULT_TZ,
        "items": [{"id": email} for email in calendar_emails],
    }

    try:
        service = _build_service(requester_email)
        result  = service.freebusy().query(body=body).execute()
    except HttpError as exc:
        status = exc.resp.status if exc.resp else "?"
        if status == 403:
            raise CalendarClientError(
                f"Freebusy 403 -- service account lacks DWD scope for {requester_email}. "
                "Harrison must ensure https://www.googleapis.com/auth/calendar.events "
                "is listed in Domain-wide Delegation (admin.google.com)."
            ) from exc
        raise CalendarClientError(f"Freebusy API HTTP {status}: {exc}") from exc
    except CalendarClientError:
        raise
    except Exception as exc:
        raise CalendarClientError(f"Freebusy API error: {exc}") from exc

    calendars = result.get("calendars") or {}
    busy: dict[str, list[tuple[datetime, datetime]]] = {}

    for email in calendar_emails:
        cal_data = calendars.get(email) or {}
        errors   = cal_data.get("errors") or []
        if errors:
            log.warning(
                "get_free_busy: calendar %s returned errors %s -- treating as fully busy",
                email, errors,
            )
            busy[email] = [(time_min, time_max)]
            continue
        periods: list[tuple[datetime, datetime]] = []
        for period in cal_data.get("busy") or []:
            try:
                s = datetime.fromisoformat(period["start"].replace("Z", "+00:00"))
                e = datetime.fromisoformat(period["end"].replace("Z", "+00:00"))
                periods.append((s, e))
            except (KeyError, ValueError) as exc:
                log.warning("get_free_busy: could not parse busy period %s: %s", period, exc)
        busy[email] = periods

    return busy


def _round_up_to_slot(dt: datetime) -> datetime:
    """Round a UTC-aware datetime UP to the next _SLOT_STEP_MIN (15-min) boundary.

    If the datetime is already on a boundary it is returned unchanged.
    """
    total_seconds = int(dt.timestamp())
    step_seconds  = _SLOT_STEP_MIN * 60
    remainder     = total_seconds % step_seconds
    if remainder == 0:
        return dt
    return dt + timedelta(seconds=(step_seconds - remainder))


def find_next_available_slot(
    busy_by_email: dict[str, list[tuple[datetime, datetime]]],
    duration_minutes: int = 30,
    search_from: "datetime | None" = None,
    search_days: int = 7,
) -> "tuple[datetime, datetime] | None":
    """Scan forward to find the first slot free for ALL calendars.

    Rules:
    - Mon-Fri only (weekends skipped)
    - 9 AM to 5 PM America/Phoenix (UTC-7, no DST)
    - 15-minute slot steps
    - slot must not overlap any busy block for any participant

    Returns (slot_start_utc, slot_end_utc) or None if no slot found in window.
    """
    now        = search_from or datetime.now(timezone.utc)
    candidate  = _round_up_to_slot(now)
    end_search = now + timedelta(days=search_days)
    duration   = timedelta(minutes=duration_minutes)

    # Flatten all busy periods into one sorted list for efficient skip-ahead
    all_busy: list[tuple[datetime, datetime]] = []
    for periods in busy_by_email.values():
        all_busy.extend(periods)
    all_busy.sort(key=lambda t: t[0])

    while candidate < end_search:
        cand_az = candidate.astimezone(_PHOENIX_TZ)

        # --- Skip weekends ---
        if cand_az.weekday() >= 5:  # 5=Sat, 6=Sun
            days_to_monday = 7 - cand_az.weekday()  # Sat->2, Sun->1
            next_monday_az = (cand_az + timedelta(days=days_to_monday)).replace(
                hour=_WORK_START_HOUR, minute=0, second=0, microsecond=0
            )
            candidate = next_monday_az.astimezone(timezone.utc)
            continue

        # --- Before work hours -- jump to 9 AM same day ---
        if cand_az.hour < _WORK_START_HOUR:
            today_start_az = cand_az.replace(
                hour=_WORK_START_HOUR, minute=0, second=0, microsecond=0
            )
            candidate = today_start_az.astimezone(timezone.utc)
            continue

        # --- Slot end would bleed past 5 PM -- jump to next workday 9 AM ---
        slot_end    = candidate + duration
        slot_end_az = slot_end.astimezone(_PHOENIX_TZ)
        past_eod = (
            cand_az.hour >= _WORK_END_HOUR
            or slot_end_az.hour > _WORK_END_HOUR
            or (slot_end_az.hour == _WORK_END_HOUR and slot_end_az.minute > 0)
        )
        if past_eod:
            next_day_az = (cand_az + timedelta(days=1)).replace(
                hour=_WORK_START_HOUR, minute=0, second=0, microsecond=0
            )
            candidate = next_day_az.astimezone(timezone.utc)
            continue

        # --- Check for overlap with any busy block ---
        blocking: "tuple[datetime, datetime] | None" = None
        for busy_start, busy_end in all_busy:
            if busy_start >= slot_end:
                break   # sorted -- no further period can overlap
            if busy_end <= candidate:
                continue
            blocking = (busy_start, busy_end)
            break

        if blocking is None:
            return (candidate, slot_end)

        # Jump past the end of the blocking busy period (round up to next slot)
        candidate = _round_up_to_slot(blocking[1])

    return None


def find_meeting_slot(
    requester_email: str,
    calendar_emails: list[str],
    duration_minutes: int = 30,
    search_days: int = 7,
) -> "tuple[datetime, datetime] | None":
    """High-level convenience: freebusy query + slot scan in one call.

    Returns (slot_start_utc, slot_end_utc) or None.
    Raises CalendarClientError on API failure.
    """
    now      = datetime.now(timezone.utc)
    time_max = now + timedelta(days=search_days)
    busy     = get_free_busy(requester_email, calendar_emails, now, time_max)
    return find_next_available_slot(
        busy,
        duration_minutes=duration_minutes,
        search_from=now,
        search_days=search_days,
    )


def format_slot_proposal_for_llm(
    slot_start: datetime,
    slot_end: datetime,
    participant_names: list[str],
    title: str = "Meeting",
) -> str:
    """Render the proposed slot as a Slack-friendly preview block.

    Returns a string Claude should present to the user verbatim.
    Embeds proposed_start / proposed_end as ISO strings so Claude can pass
    them straight back in the Phase 2 confirmed=true call without re-parsing.
    """
    start_az = slot_start.astimezone(_PHOENIX_TZ)
    end_az   = slot_end.astimezone(_PHOENIX_TZ)

    # Windows-safe strftime (no %-d padding trick)
    day_str   = start_az.strftime("%A, %B") + f" {start_az.day}, {start_az.year}"
    start_str = start_az.strftime("%I:%M %p").lstrip("0") + " AZ"
    end_str   = end_az.strftime("%I:%M %p").lstrip("0") + " AZ"
    dur_min   = int((slot_end - slot_start).total_seconds() / 60)

    if len(participant_names) <= 2:
        names_str = " & ".join(participant_names)
    else:
        names_str = ", ".join(participant_names[:-1]) + f" & {participant_names[-1]}"

    # Explicit -07:00 offset (Phoenix, no DST) for Phase 2 passback
    start_iso = start_az.strftime("%Y-%m-%dT%H:%M:00-07:00")
    end_iso   = end_az.strftime("%Y-%m-%dT%H:%M:00-07:00")

    return (
        "SLOT FOUND -- present this as a clear preview block to the user:\n"
        f"- *Title:* {title}\n"
        f"- *Day:* {day_str}\n"
        f"- *Time:* {start_str} - {end_str} ({dur_min} min)\n"
        f"- *Participants:* {names_str}\n"
        "\n"
        "Tell the user this is the next available opening that works for everyone, "
        "and ask for their explicit confirmation before booking.\n"
        "\n"
        "Once they confirm, call calendar_schedule_meeting again with:\n"
        f'  confirmed: true\n'
        f'  proposed_start: "{start_iso}"\n'
        f'  proposed_end: "{end_iso}"\n'
        "  (keep title and participants the same as this call)"
    )
