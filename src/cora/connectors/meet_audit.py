"""Meet join audit -- a READ-ONLY seam over the Admin SDK Reports API (Code #14 R14-8).

THE LANE (ruled 2026-09-19, ask 9.6; D-324 "a LOCKED LANE", D-307 / D-308 shape).
The 07:22 meeting-capture audit cannot tell "nobody met" from "met and nothing
captured it" -- a group-calendar block with zero transcripts is only PRESUMED
unconvened (meeting_capture.presumed_unconvened_basis). Google Meet writes a
`call_ended` audit event per participant endpoint; reading those events turns the
presumption into evidence. This module is the ONLY place that reads them.

SHIPS DARK. Nothing here works until Harrison adds the scope below to the service
account's domain-wide-delegation grant (and the Admin SDK API is enabled for the
SA's project, and the impersonated subject holds the Reports privilege). Until
then every read comes back `dark:*` and meeting_capture renders exactly what it
renders today. The lane self-detects which step is missing:

    dark:scope    the token mint is refused (`unauthorized_client`): the scope is
                  not in the DWD grant
    dark:api      403 accessNotConfigured / SERVICE_DISABLED: the Admin SDK API is
                  off in the SA's GCP project
    dark:subject  403 "Not Authorized to access this resource/api": the subject
                  is not a super admin / lacks the Reports privilege
    dark:test     the suite's conftest redirect (no test ever mints a token)
    error         anything else, INCLUDING a refused subject (see below)
    partial       the page cap was hit before the result set was exhausted
    live          a complete read

BLAST RADIUS (state it plainly). `admin.reports.audit.readonly` against a super-
admin subject can read EVERY Workspace audit log -- login, drive, admin, token,
gmail -- not just Meet. The narrowing to Meet is CODE, not the scope: one
constant application name (`_APPLICATION = "meet"`), one event name, and a
source-pin test (tests/test_meet_audit.py) that fails if any other
`applicationName` reaches the list call. Same "the scope string is not the
guarantee" posture as gmail.compose vs gmail.send.

D-307. The subject is a HUMAN super admin (default harrison@hjrglobal.com,
override CORA_REPORTS_IMPERSONATE, resolved per call). cora@hjrglobal.com is
HARD-REFUSED: giving the capture identity admin reach would end the
scope-guarantee doctrine. A refused subject never reaches `with_subject`.

D-145 / D-082. A Meet audit event carries participant emails (external and
agency addresses on client meetings), display names, IPs and locations. They are
DROPPED at parse: the only per-endpoint value that leaves `_parse_event` is a
12-hex SHA-256 prefix used to count distinct endpoints, plus an is-human flag
decided in-function. Nothing identifying is returned, logged or stored.

The parameter KEY NAMES the parser reads (meeting_code, calendar_event_id,
conference_id, identifier, display_name, endpoint_id, duration_seconds,
start_timestamp_seconds) are VERIFY-AT-BUILD against a live read -- the staged
read-only `scripts/probe_meet_audit.py` prints the key names it observes (never
their values). A missing key makes a join unusable; it never crashes the read.

Not imported by the bot (cora.app never loads it): meeting_capture imports it
lazily inside audit_day's default reader. Script-side, no restart.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)

#: The ONE scope this seam requests, on its own credential (never merged into any
#: other builder's scope list).
REPORTS_SCOPE = "https://www.googleapis.com/auth/admin.reports.audit.readonly"
_SCOPES = [REPORTS_SCOPE]

#: The narrowing that the scope does not provide. A module constant, with no
#: parameter anywhere that could override it (source-pinned by tests).
_APPLICATION = "meet"
_EVENT_NAME = "call_ended"
_USER_KEY = "all"
_MAX_RESULTS = 1000
DEFAULT_MAX_PAGES = 20

ADMIN_SUBJECT_ENV = "CORA_REPORTS_IMPERSONATE"
_DEFAULT_SUBJECT = "harrison@hjrglobal.com"
#: Never impersonated for this read (D-307).
_REFUSED_SUBJECTS = frozenset({"cora@hjrglobal.com"})

STATE_LIVE = "live"
STATE_PARTIAL = "partial"
STATE_ERROR = "error"
STATE_DARK_SCOPE = "dark:scope"
STATE_DARK_API = "dark:api"
STATE_DARK_SUBJECT = "dark:subject"
STATE_DARK_TEST = "dark:test"
LANE_STATES: tuple[str, ...] = (
    STATE_LIVE, STATE_PARTIAL, STATE_ERROR,
    STATE_DARK_SCOPE, STATE_DARK_API, STATE_DARK_SUBJECT, STATE_DARK_TEST,
)

#: Endpoints that are a bot, not a person. The capture identity's own seat and the
#: legacy Fireflies notetaker join as participants; a bot alone in a room is not a
#: convened meeting. Decided in-function, never stored.
_NOTETAKER_IDENTIFIERS = frozenset({"cora@hjrglobal.com", "notetaker@fireflies.ai"})
_NOTETAKER_DOMAINS = ("@fireflies.ai",)
_NOTETAKER_NAME_MARKERS = ("notetaker", "fireflies")

_NON_CODE = re.compile(r"[^a-z0-9]")


class MeetAuditDark(Exception):
    """A lane state decided before any list call (e.g. the test redirect)."""

    def __init__(self, state: str, reason: str = ""):
        super().__init__(state)
        self.state = state
        self.reason = reason


class MeetAuditRefused(Exception):
    """The configured subject is refused for this read (D-307)."""


@dataclass(frozen=True)
class MeetJoin:
    """One participant endpoint's `call_ended` event, identifiers already dropped."""

    meeting_code: str        # normalized (lowercase alphanumerics); "" if absent
    calendar_event_id: str   # as the audit carries it; "" if absent
    conference_id: str
    start_ts: int            # UTC epoch the endpoint joined (0 if unknown)
    end_ts: int              # UTC epoch the endpoint left (0 if unknown)
    endpoint_key: str        # 12-hex hash, for DISTINCT counting only
    is_human: bool


@dataclass
class MeetAuditRead:
    state: str
    joins: tuple[MeetJoin, ...] = ()
    events_read: int = 0
    pages: int = 0
    #: a short reason CLASS (never exception text, never an address)
    reason: str = ""
    window: tuple[int, int] = (0, 0)
    #: parameter KEY names observed on call_ended events (names, never values) --
    #: what the staged probe prints to verify the parser's assumptions
    param_keys: tuple[str, ...] = field(default_factory=tuple)

    @property
    def decisive(self) -> bool:
        """True only for a complete, live read."""
        return self.state == STATE_LIVE


# ── identity ─────────────────────────────────────────────────────────────────

def resolve_subject() -> str:
    """The admin subject to impersonate, resolved PER CALL. Refuses cora@ (D-307)."""
    subject = (os.environ.get(ADMIN_SUBJECT_ENV, "") or _DEFAULT_SUBJECT).strip().lower()
    if not subject or "@" not in subject:
        raise MeetAuditRefused("no usable admin subject configured")
    if subject in _REFUSED_SUBJECTS:
        raise MeetAuditRefused("the capture identity is never an admin subject (D-307)")
    return subject


def _service_account_path() -> str:
    val = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not val or not os.path.exists(val):
        raise MeetAuditDark(STATE_ERROR, "service account key file not configured")
    return val


def _build_reports_service_impl():
    """The Reports v1 service on its OWN credential: REPORTS_SCOPE alone, one
    admin subject. Raises MeetAuditRefused before any credential exists when the
    subject is refused."""
    subject = resolve_subject()
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_file(
        _service_account_path(), scopes=list(_SCOPES),
    )
    delegated = creds.with_subject(subject)
    return build("admin", "reports_v1", credentials=delegated, cache_discovery=False)


#: The indirection the suite's conftest redirects (to a `dark:test` raise), so no
#: test ever mints a token or reaches the Reports endpoint.
_build_reports_service = _build_reports_service_impl


# ── classification ───────────────────────────────────────────────────────────

def _http_status(exc: Any) -> int:
    try:
        return int(getattr(getattr(exc, "resp", None), "status", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _http_text(exc: Any) -> str:
    parts: list[str] = []
    content = getattr(exc, "content", b"")
    if isinstance(content, bytes):
        parts.append(content.decode("utf-8", errors="replace"))
    elif content:
        parts.append(str(content))
    try:
        details = getattr(exc, "error_details", None)
        if details:
            parts.append(json.dumps(details, default=str))
    except Exception:  # noqa: BLE001
        pass
    parts.append(str(getattr(exc, "reason", "") or ""))
    return " ".join(parts).lower()


def classify_error(exc: BaseException) -> tuple[str, str]:
    """(state, reason class) for a failed token mint or list call. The reason is a
    fixed CLASS string -- never the exception text, which can carry the subject's
    address or a request URL."""
    if isinstance(exc, MeetAuditDark):
        return exc.state, exc.reason or exc.state
    if isinstance(exc, MeetAuditRefused):
        return STATE_ERROR, "subject refused"
    name = type(exc).__name__
    text = str(exc).lower()
    if name == "RefreshError":
        if "unauthorized_client" in text:
            return STATE_DARK_SCOPE, "scope not in the DWD grant"
        return STATE_ERROR, "token refresh failed"
    if name == "HttpError":
        status = _http_status(exc)
        body = _http_text(exc)
        if status == 403 and ("accessnotconfigured" in body or "service_disabled" in body):
            return STATE_DARK_API, "Admin SDK API not enabled"
        if status in (401, 403) and "not authorized" in body:
            return STATE_DARK_SUBJECT, "subject lacks the Reports privilege"
        return STATE_ERROR, f"http {status or 'unknown'}"
    return STATE_ERROR, name


# ── parse ────────────────────────────────────────────────────────────────────

def normalize_meeting_code(value: str) -> str:
    """'https://meet.google.com/vot-cade-nce?x' / 'VOT-CADE-NCE' / 'votcadence'
    -> 'votcadence'. Linear: str ops + one negated-class substitution."""
    s = (value or "").strip().lower()
    marker = "meet.google.com/"
    if marker in s:
        s = s.split(marker, 1)[1]
        s = s.split("?", 1)[0].split("#", 1)[0].split("/", 1)[0]
    return _NON_CODE.sub("", s)


def _rfc3339(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _epoch(value: Any) -> int:
    if value in (None, ""):
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        pass
    try:
        text = str(value).strip().replace("Z", "+00:00")
        return int(datetime.fromisoformat(text).timestamp())
    except (TypeError, ValueError):
        return 0


def _param_map(event: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for p in (event.get("parameters") or []):
        if not isinstance(p, dict) or not p.get("name"):
            continue
        for k in ("value", "intValue", "boolValue", "multiValue", "multiIntValue"):
            if k in p:
                out[str(p["name"])] = p[k]
                break
    return out


def _is_notetaker(identifier: str, display_name: str) -> bool:
    ident = (identifier or "").strip().lower()
    if ident in _NOTETAKER_IDENTIFIERS or ident.endswith(_NOTETAKER_DOMAINS):
        return True
    name = (display_name or "").strip().lower()
    return any(m in name for m in _NOTETAKER_NAME_MARKERS)


def _parse_event(activity: dict[str, Any], event: dict[str, Any]) -> MeetJoin | None:
    """One call_ended event -> a MeetJoin with every identifier DROPPED, or None
    when it cannot be joined to a meeting or counted as a distinct endpoint."""
    params = _param_map(event)
    code = normalize_meeting_code(str(params.get("meeting_code") or ""))
    cal_id = str(params.get("calendar_event_id") or "").strip()
    if not code and not cal_id:
        return None
    identifier = str(params.get("identifier") or "")
    display = str(params.get("display_name") or "")
    endpoint = str(params.get("endpoint_id") or "") or identifier
    if not endpoint:
        return None
    key = hashlib.sha256(endpoint.encode("utf-8", errors="replace")).hexdigest()[:12]
    human = not _is_notetaker(identifier, display)
    end_ts = _epoch(((activity.get("id") or {}) if isinstance(activity.get("id"), dict) else {}).get("time"))
    start_ts = _epoch(params.get("start_timestamp_seconds"))
    if not start_ts and end_ts:
        dur = _epoch(params.get("duration_seconds"))
        start_ts = end_ts - dur if dur else end_ts
    if not end_ts and start_ts:
        dur = _epoch(params.get("duration_seconds"))
        end_ts = start_ts + dur if dur else start_ts
    # identifier / display / endpoint / actor / ip / location go out of scope here
    return MeetJoin(
        meeting_code=code, calendar_event_id=cal_id,
        conference_id=str(params.get("conference_id") or "").strip(),
        start_ts=start_ts, end_ts=end_ts, endpoint_key=key, is_human=human,
    )


# ── the read ─────────────────────────────────────────────────────────────────

def read_call_ended(
    start_utc: datetime,
    end_utc: datetime,
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
    service: Any = None,
) -> MeetAuditRead:
    """Every Meet `call_ended` event in [start_utc, end_utc), read to exhaustion.

    NEVER raises and NEVER writes. The ONLY API call is
    ``activities().list(userKey='all', applicationName=_APPLICATION,
    eventName=_EVENT_NAME, ...)``. A failure returns a state (see the module
    docstring) with a reason CLASS, and the caller keeps its existing logic.
    """
    window = (int(_epoch_dt(start_utc)), int(_epoch_dt(end_utc)))
    out = MeetAuditRead(state=STATE_ERROR, window=window)
    try:
        svc = service if service is not None else _build_reports_service()
        joins: list[MeetJoin] = []
        keys_seen: set[str] = set()
        token: str | None = None
        pages = 0
        while True:
            kwargs: dict[str, Any] = dict(
                userKey=_USER_KEY, applicationName=_APPLICATION, eventName=_EVENT_NAME,
                startTime=_rfc3339(start_utc), endTime=_rfc3339(end_utc),
                maxResults=_MAX_RESULTS,
            )
            if token:
                kwargs["pageToken"] = token
            resp = svc.activities().list(**kwargs).execute(num_retries=1) or {}
            pages += 1
            for activity in (resp.get("items") or []):
                if not isinstance(activity, dict):
                    continue
                for event in (activity.get("events") or []):
                    if not isinstance(event, dict) or event.get("name") != _EVENT_NAME:
                        continue
                    out.events_read += 1
                    keys_seen.update(_param_map(event).keys())
                    j = _parse_event(activity, event)
                    if j is not None:
                        joins.append(j)
            token = resp.get("nextPageToken") or None
            if not token:
                out.state = STATE_LIVE
                break
            if pages >= max_pages:
                out.state = STATE_PARTIAL
                out.reason = f"page cap {max_pages} reached"
                break
        out.joins = tuple(joins)
        out.pages = pages
        out.param_keys = tuple(sorted(keys_seen))
    except Exception as exc:  # noqa: BLE001
        out.state, out.reason = classify_error(exc)
        out.joins = ()
        log.info("meet-audit read: state=%s reason=%s", out.state, out.reason)
    return out


def probe_lane(*, service: Any = None) -> MeetAuditRead:
    """The lane state from a read of the last hour. Contents are discarded beyond
    counts and key names; same never-raise / never-write contract."""
    now = datetime.now(timezone.utc)
    return read_call_ended(now - timedelta(hours=1), now, service=service)


def _epoch_dt(dt: datetime) -> float:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()
