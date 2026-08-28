"""One Cora Notetaker -- the capture roster, the DWD ensure lane, and the daily auditor.

Plan of record: _shared/projects/fireflies-deep-dive/
  2026-08-27_fndr_one-cora-notetaker-resolution-plan.md  (v2, RULED by Harrison)
Build seed: cq-ffcf6e4ffe7c. D-247 amendment.

THE ARCHITECTURE, in one paragraph. Meeting capture consolidates onto a SINGLE
Fireflies seat -- cora@hjrglobal.com -- whose own connected calendar auto-joins
everything on it. This module's ensure lane makes sure every qualifying roster
meeting IS on that calendar (guest-add where we can, an event copy where we
cannot), and its auditor checks each morning that what got captured matches what
was scheduled. Once the seat is live, cora@-on-the-event is the ONLY dispatch and
the notetaker@fireflies.ai invite habit retires.

TWO HALVES, TWO POSTURES -- this matters:

  * The ENSURE lane WRITES to calendars and ships DARK behind CORA_ONECORA_ENSURE
    (default "off"). It cannot write until BOTH the env flag says "live" AND the
    caller passes apply=True. As of 2026-08-27 cora@'s Fireflies seat is INVITED
    but NOT ACTIVE -- verified live, it does not appear in the Fireflies `users`
    query -- so enabling this lane before the seat activates would put meetings on
    a calendar nothing is listening to.

  * The AUDITOR is READ-ONLY and ships LIVE. It is load-bearing rather than nice
    to have: after the single-seat collapse there is no per-seat fallback, so a
    meeting the ensure lane misses is captured by NOTHING. A silent gap here is
    how the duplicate-capture bug survived three months.

WHY THE AUDITOR DIFFS AGAINST THE FIREFLIES API AND NOT THE KB. The nightly KB
ingest advances its watermark to the RUN START time while the Fireflies query
filters on MEETING DATE, so a transcript that only becomes available after the
03:30 run falls permanently outside every later window. Diffing calendars against
KB chunks would therefore report those as misses when the transcript exists. The
auditor asks Fireflies directly.

THE JOIN IS EXACT, NOT FUZZY. Fireflies' `cal_id` is the Google Calendar event id
VERBATIM -- verified live 2026-08-27: cal_id "e2n2n35b61sieue9j6fcns66rs" is
byte-identical to that event's Google `id`, and the recurring form
"<master>_20260827T190000Z" matches Google's instance id exactly. So the primary
join is by event id, with meeting link and then title+time only as fallbacks for
transcripts Fireflies never associated with a calendar entry (about half of them
carry no cal_id at all).

LEX POSTURE (D-247): LEX meetings are captured (capture-yes) but their TITLES are
never rendered into #founder-operations (team-visible-no). See `display_title`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROSTER_PATH = _REPO_ROOT / "data" / "maps" / "meeting-capture-roster.yaml"
_LEDGER_PATH = _REPO_ROOT / "logs" / "meeting-capture-ledger.jsonl"

#: #founder-operations. Allowlisted BY ID, not by name -- a channel rename must
#: not silently redirect an operations report.
OPS_CHANNEL = "C0BCUBUDHAR"

#: Phoenix is UTC-7 year round (no DST), so a fixed offset is correct here.
_AZ = timezone(timedelta(hours=-7))

#: Domains the Cora service account can impersonate. This mirrors the Google DWD
#: grant documented in data/maps/monitored-email-accounts.yaml; it is a property of
#: that grant, not a business choice, so it lives in code rather than the roster
#: YAML. Guest-add impersonates the ORGANISER, which is only possible when the
#: organiser sits in one of these domains -- everything else falls back to a copy.
DWD_DOMAINS: frozenset[str] = frozenset({
    "hjrglobal.com",
    "f3energy.com",
    "lexingtonservices.com",
    "unitedfightleague.com",
    "bigd.media",
})

#: The legacy invite habit: a Fireflies bot invited directly onto an event. Until
#: Harrison retires that habit (plan of record, one-mechanism rule) an event may
#: already carry it. Adding the capture identity on TOP of it dispatches a SECOND
#: bot to the same meeting -- precisely the duplicate-capture pattern this lane
#: exists to remove -- so its presence counts as already-covered.
LEGACY_NOTETAKER = "notetaker@fireflies.ai"

#: Google eventTypes that are not meetings. "Office" renders as workingLocation and
#: "Dentist Appointment" as outOfOffice -- both were live on the roster on
#: 2026-08-27 and both would otherwise be swept as capture candidates.
_MEETING_EVENT_TYPES: frozenset[str] = frozenset({"default", ""})


# ── feature flag ─────────────────────────────────────────────────────────────

def ensure_mode() -> str:
    """One of "off" (default) / "plan" / "live".

    A named enum rather than a truthy check, deliberately: a bare truthy flag is
    how `CORA_AUTOWRITE_LIVE=1` became a silent no-op (D-088 era). An unrecognised
    value falls back to "off" rather than to on.

      off  -- the lane refuses to run at all.
      plan -- it reads calendars and records what it WOULD do; never writes.
      live -- writes permitted, and still only when the caller passes apply=True.
    """
    v = (os.environ.get("CORA_ONECORA_ENSURE", "off") or "off").strip().lower()
    return v if v in ("off", "plan", "live") else "off"


# ── config ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RosterMember:
    name: str
    calendar_email: str
    enabled: bool = True


@dataclass(frozen=True)
class CaptureConfig:
    capture_identity: str
    members: tuple[RosterMember, ...]
    skip_title_markers: tuple[str, ...]
    no_record_title_patterns: tuple[str, ...]
    no_record_emails: frozenset[str]
    no_record_attendee_domains: tuple[str, ...]

    @property
    def active_members(self) -> tuple[RosterMember, ...]:
        return tuple(m for m in self.members if m.enabled)


def _as_bool(val: Any) -> bool:
    """Tolerant boolean for a hand-edited roster.

    `enabled: false` parses as a real bool, but `enabled: "false"` is a STRING and
    `bool("false")` is True -- so a quoted value would silently keep someone in the
    sweep after Harrison had switched them off. A missing value stays True (the
    documented default); an unrecognised one is treated as False, because in a
    capture roster the safe reading of "I do not understand this" is "do not
    record this person".
    """
    if isinstance(val, bool):
        return val
    if val is None:
        return True
    return str(val).strip().lower() in ("true", "yes", "on", "1")


class MeetingCaptureConfigError(Exception):
    """Raised when the roster file is missing or unusable."""


_cfg_cache: tuple[float, CaptureConfig] | None = None
_CFG_TTL_SEC = 60.0


def load_config(*, path: Path | None = None, force: bool = False) -> CaptureConfig:
    """Load the roster/carve-out config, cached for 60s (edit the YAML, no restart).

    FAIL-CLOSED, unlike org_roles: a malformed roster raises rather than serving the
    last good copy. org_roles keeps stale data on a parse error, which is right for
    an advisory role lookup and wrong here -- this file decides who gets RECORDED,
    and quietly capturing from a roster Harrison thought he had just edited is the
    one failure this lane must not have.
    """
    global _cfg_cache
    target = path or _ROSTER_PATH
    now = time.monotonic()
    if not force and path is None and _cfg_cache and (now - _cfg_cache[0]) < _CFG_TTL_SEC:
        return _cfg_cache[1]

    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MeetingCaptureConfigError(f"roster file not found: {target}") from exc
    except Exception as exc:
        raise MeetingCaptureConfigError(f"roster file unreadable ({target}): {exc}") from exc
    if not isinstance(raw, dict):
        raise MeetingCaptureConfigError(f"roster file is not a mapping: {target}")

    identity = (raw.get("capture_identity") or "").strip().lower()
    if not identity or "@" not in identity:
        raise MeetingCaptureConfigError("roster file has no usable capture_identity")

    members: list[RosterMember] = []
    seen_emails: set[str] = set()
    for entry in (raw.get("roster") or []):
        if not isinstance(entry, dict):
            continue
        email = (entry.get("calendar_email") or "").strip().lower()
        if not email or "@" not in email:
            log.warning("meeting-capture roster: entry %r has no calendar_email -- skipped",
                        entry.get("name"))
            continue
        if email in seen_emails:
            # The D-096 belt. One human, one calendar: two entries pointing at the
            # same physical calendar would act on every event twice.
            log.warning("meeting-capture roster: duplicate calendar_email %s -- keeping first",
                        email)
            continue
        seen_emails.add(email)
        members.append(RosterMember(
            name=(entry.get("name") or email).strip(),
            calendar_email=email,
            enabled=_as_bool(entry.get("enabled", True)),
        ))

    carve = raw.get("carve_outs") or {}
    if not isinstance(carve, dict):
        raise MeetingCaptureConfigError(
            "carve_outs must be a mapping -- refusing to run with no carve-outs. "
            "Silently treating a malformed carve_outs block as empty would capture "
            "every meeting the list was written to protect."
        )

    return _finish_config(identity, members, carve, path is None, now)


def _as_list(carve: dict, key: str) -> list[str]:
    """Read a carve-out list, refusing anything that is not a list.

    A bare string here is the dangerous shape: `no_record_title_patterns: counsel`
    is valid YAML and iterating it yields the CHARACTERS "c","o","u",... -- which
    as whole-word patterns match nothing, so the carve-out silently stops
    protecting anyone. Fail closed instead.
    """
    val = carve.get(key)
    if val is None:
        return []
    if not isinstance(val, list):
        raise MeetingCaptureConfigError(
            f"carve_outs.{key} must be a list, got {type(val).__name__} -- "
            "refusing to run rather than silently dropping a carve-out."
        )
    return [str(v).strip() for v in val if str(v).strip()]


def _finish_config(identity, members, carve, cacheable, now) -> CaptureConfig:
    cfg = CaptureConfig(
        capture_identity=identity,
        members=tuple(members),
        skip_title_markers=tuple(m.lower() for m in _as_list(carve, "skip_title_markers")),
        no_record_title_patterns=tuple(
            p.lower() for p in _as_list(carve, "no_record_title_patterns")
        ),
        no_record_emails=frozenset(e.lower() for e in _as_list(carve, "no_record_emails")),
        no_record_attendee_domains=tuple(
            d.lower().lstrip("@") for d in _as_list(carve, "no_record_attendee_domains")
        ),
    )
    if cacheable:
        global _cfg_cache
        _cfg_cache = (now, cfg)
    return cfg


# ── event helpers ────────────────────────────────────────────────────────────

def event_emails(event: dict[str, Any]) -> set[str]:
    """Every email associated with an event: organiser, creator, attendees."""
    out: set[str] = set()
    for key in ("organizer", "creator"):
        val = event.get(key)
        if isinstance(val, dict):
            addr = (val.get("email") or "").strip().lower()
            if addr:
                out.add(addr)
    for att in (event.get("attendees") or []):
        if isinstance(att, dict):
            addr = (att.get("email") or "").strip().lower()
            if addr:
                out.add(addr)
    return out


def event_start_ts(event: dict[str, Any]) -> int:
    """Event start as a UTC epoch second; 0 when unparseable or all-day."""
    start = event.get("start") or {}
    raw = start.get("dateTime")
    if not raw:
        return 0
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def event_time_label(event: dict[str, Any]) -> str:
    ts = event_start_ts(event)
    if not ts:
        return "all-day"
    return datetime.fromtimestamp(ts, _AZ).strftime("%H:%M")


def starts_on_day(event: dict[str, Any], day: str) -> bool:
    """True when the event's START falls inside the given AZ day.

    Google's events.list returns everything OVERLAPPING [timeMin, timeMax], so a
    23:30-00:30 meeting comes back on BOTH days. Counting it twice makes the second
    day report a permanent false miss -- the transcript can only ever match one of
    them. A meeting belongs to the day it starts.
    """
    ts = event_start_ts(event)
    if not ts:
        return False
    try:
        start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=_AZ)
    except ValueError:
        return True
    return int(start.timestamp()) <= ts < int((start + timedelta(days=1)).timestamp())


def meeting_key(event: dict[str, Any]) -> tuple:
    """Identity of the MEETING, which is not the identity of the calendar event.

    Google shares one event id across a domain, but an EXTERNALLY-organised meeting
    lands on each invitee's calendar as a SEPARATE event with its own id (the
    "_"-prefixed imported form). Measured live on 2026-08-26: one "Reddit Community
    Mentions" call existed as both `63b5da2780lt0hjbcpe6dcnarv` and
    `_f1jl4obdal8m4hi16cr3io9kcho44` -- same Meet link, same minute, two roster
    calendars, two ids.

    Keying the lane on event id would therefore make the ensure lane act TWICE on
    that meeting, putting two entries on the capture calendar and re-creating the
    duplicate-capture pattern this whole build exists to eliminate. So a meeting is
    identified by (meeting link, exact start), and the event id is only ever the
    ADDRESS we act on, never the identity.

    Exact start rather than a time bucket, deliberately: copies of one event share
    an identical scheduled start, so bucketing would buy nothing and would introduce
    a boundary case where two copies straddle a bucket edge.
    """
    from cora.tools.calendar_client import extract_meeting_link

    link = extract_meeting_link(event).strip().lower()
    if link:
        return ("link", link, event_start_ts(event))
    return ("event", (event.get("id") or "").strip())


def _norm_ws(text: str) -> str:
    """Collapse every run of whitespace (incl. NBSP) to a single space.

    A multi-word carve-out like "estate planning" is matched against a title a human
    typed or pasted. A double space, a newline, or a non-breaking space from a paste
    would otherwise defeat the pattern silently -- and a carve-out that fails to fire
    records a meeting somebody ruled must never be recorded.
    """
    return re.sub(r"[\s ]+", " ", text or "").strip()


def _word_pattern(term: str) -> re.Pattern[str]:
    """Whole-word matcher for a carve-out phrase.

    Word-bounded so "counsel" cannot fire inside "counselling" and -- the one that
    actually matters -- so a short term can never match inside an unrelated longer
    word and silently stop capture for a meeting that should be recorded.
    Bounded character classes only; no nested quantifiers (the ReDoS shape this
    repo has been bitten by repeatedly).
    """
    return re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)", re.IGNORECASE)


_word_cache: dict[str, re.Pattern[str]] = {}


def _matches_word(text: str, term: str) -> bool:
    key = _norm_ws(term)
    pat = _word_cache.get(key)
    if pat is None:
        pat = _word_pattern(key)
        _word_cache[key] = pat
    return bool(pat.search(_norm_ws(text)))


# ── LEX / PHI display rail ───────────────────────────────────────────────────

def is_lex_event(event: dict[str, Any]) -> bool:
    """Reuse the ONE LEX meeting detector rather than writing a second one.

    fireflies_connector.classify_lex_meeting is shaped for a Fireflies transcript
    dict, so the calendar event is adapted into that shape -- attendees[].email ->
    meeting_attendees[].email, organizer.email -> organizer_email. Fail-safe: any
    error classifies as LEX, because the consequence of a false negative is a LEX
    title rendered into a shared ops channel.
    """
    try:
        from cora.connectors.fireflies_connector import classify_lex_meeting

        organizer = (event.get("organizer") or {}) if isinstance(event.get("organizer"), dict) else {}
        adapted = {
            "title": event.get("summary") or "",
            "organizer_email": (organizer.get("email") or ""),
            "host_email": "",
            "meeting_attendees": [
                {"displayName": a.get("displayName") or "", "email": a.get("email") or ""}
                for a in (event.get("attendees") or []) if isinstance(a, dict)
            ],
            "participants": sorted(event_emails(event)),
        }
        return bool(classify_lex_meeting(adapted).is_lex)
    except Exception as exc:  # noqa: BLE001
        log.warning("LEX classification failed (%s) -- treating as LEX to protect the title", exc)
        return True


def _has_client_domain_attendee(event: dict[str, Any]) -> bool:
    """True when anyone on the event sits at a client-agency domain (e.g. .gov).

    The DISPLAY rail is deliberately stricter than the ingest classifier.
    classify_lex_meeting requires a LEX signal -- a Lexington domain, a named lead,
    a LEX title -- before a .gov attendee counts, which is correct for deciding
    whether to INGEST something. But a client meeting sitting on a non-LEX person's
    calendar with a generic title and only agency attendees would pass that test and
    have its title printed into a shared ops channel. Redacting one extra ops line
    costs nothing; printing one client title costs a lot (D-082).
    """
    try:
        from cora.connectors.fireflies_connector import _load_lex_detect_cfg

        suffixes = tuple(_load_lex_detect_cfg().get("client_domain_suffixes") or ())
    except Exception:  # noqa: BLE001
        suffixes = (".gov",)
    if not suffixes:
        return False
    for addr in event_emails(event):
        domain = addr.rsplit("@", 1)[-1]
        if domain.endswith(suffixes):
            return True
    return False


def display_title(event: dict[str, Any]) -> str:
    """The event title as it may appear in #founder-operations.

    A LEX or PHI-flagged meeting is rendered as its shape, never its subject:
    "LEX meeting, 09:00, organizer Shaun Hawkins". Over-redacting an ops line costs
    nothing; under-redacting one puts client-identifying text into a shared channel
    (D-082 class). Both screens run, and either one triggers redaction.
    """
    title = (event.get("summary") or "(untitled)").strip()
    organizer = (event.get("organizer") or {}) if isinstance(event.get("organizer"), dict) else {}
    who = (organizer.get("displayName") or organizer.get("email") or "unknown").strip()

    if is_lex_event(event) or _has_client_domain_attendee(event):
        redact = True
    else:
        try:
            from cora.phi_guard import is_any_phi

            redact = bool(is_any_phi(title))
        except Exception:  # noqa: BLE001
            redact = True
    if redact:
        # Deliberately NOT naming the organiser here. An earlier cut rendered
        # "LEX/PHI meeting, 11:00, organizer vreese@azdes.gov", which redacts the
        # title and then re-identifies the meeting on the same line -- the agency
        # address alone tells the channel which client programme it was. Shape and
        # time only; the event id is in the ledger if anyone needs to find it.
        return f"LEX/PHI meeting, {event_time_label(event)}"
    return title


# ── qualification ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Qualification:
    qualifies: bool
    reason: str
    meeting_link: str = ""


def qualify_event(
    event: dict[str, Any],
    cfg: CaptureConfig,
    *,
    roster_email: str = "",
) -> Qualification:
    """Decide whether one calendar event is in scope for capture.

    Order matters: cheap structural disqualifiers first, then consent carve-outs.
    Every non-qualifying outcome carries a distinct reason string so the auditor can
    report WHY a meeting was skipped -- an unexplained skip is indistinguishable
    from a bug.
    """
    from cora.tools.calendar_client import extract_meeting_link

    if (event.get("status") or "").strip().lower() == "cancelled":
        return Qualification(False, "cancelled")

    etype = (event.get("eventType") or "").strip().lower()
    if etype not in _MEETING_EVENT_TYPES:
        return Qualification(False, f"not-a-meeting:{etype}")

    link = extract_meeting_link(event)
    if not link:
        return Qualification(False, "no-meeting-link")

    # An all-day entry has `date` rather than `dateTime`. Even when one carries a
    # link there is no start time for a notetaker to join at, and copying it
    # produces an all-day event on the capture calendar that no bot can act on.
    if not (event.get("start") or {}).get("dateTime"):
        return Qualification(False, "all-day")

    # The roster user declining is a consent signal, not a scheduling detail.
    for att in (event.get("attendees") or []):
        if not isinstance(att, dict):
            continue
        addr = (att.get("email") or "").strip().lower()
        is_self = bool(att.get("self")) or (roster_email and addr == roster_email.lower())
        if is_self and (att.get("responseStatus") or "").strip().lower() == "declined":
            return Qualification(False, "roster-user-declined")

    title = (event.get("summary") or "")
    lowered = _norm_ws(title).lower()
    for marker in cfg.skip_title_markers:
        if _norm_ws(marker) in lowered:
            return Qualification(False, f"title-marker:{marker}")

    for term in cfg.no_record_title_patterns:
        if _matches_word(title, term):
            return Qualification(False, f"no-record-title:{term}")

    emails = event_emails(event)
    hit = emails & cfg.no_record_emails
    if hit:
        return Qualification(False, f"no-record-email:{sorted(hit)[0]}")

    if cfg.no_record_attendee_domains:
        for addr in emails:
            domain = addr.rsplit("@", 1)[-1]
            if domain in cfg.no_record_attendee_domains:
                return Qualification(False, f"no-record-domain:{domain}")

    return Qualification(True, "qualifies", meeting_link=link)


# ── ledger ───────────────────────────────────────────────────────────────────

def ledger_path() -> Path:
    """Overridable so tests never touch the real ledger."""
    return Path(os.environ.get("CORA_MEETING_CAPTURE_LEDGER", "") or _LEDGER_PATH)


def write_ledger(rows: list[dict[str, Any]]) -> None:
    """Append-only, one JSON object per line.

    No read-modify-write anywhere in this path, which is what makes it safe for the
    ensure lane and the auditor to write the same file from different processes --
    the repo's ledger doctrine (a lock would be process-local and useless here).
    """
    if not rows:
        return
    p = ledger_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:  # noqa: BLE001
        # A ledger failure must never take down the lane it is recording.
        log.error("meeting-capture ledger write failed: %s", exc)


# ── the ensure lane ──────────────────────────────────────────────────────────

@dataclass
class EnsureAction:
    member: str
    calendar_email: str
    event_id: str
    title: str            # already display-safe
    start_label: str
    action: str           # guest-add | copy | none | skip
    reason: str
    meeting_link: str = ""
    applied: bool = False
    error: str = ""


@dataclass
class EnsureResult:
    day: str
    mode: str
    applied: bool
    actions: list[EnsureAction] = field(default_factory=list)
    failed_calendars: list[tuple[str, str]] = field(default_factory=list)
    #: capture copies whose source meeting no longer exists at that time
    stale_copies: list[tuple[str, str]] = field(default_factory=list)  # (event_id, why)

    @property
    def qualifying(self) -> int:
        return sum(1 for a in self.actions if a.action in ("guest-add", "copy", "none"))

    @property
    def ensured(self) -> int:
        return sum(1 for a in self.actions if a.applied or a.action == "none")

    @property
    def skipped(self) -> int:
        return sum(1 for a in self.actions if a.action == "skip")


def _is_dwd_domain(email: str) -> bool:
    return email.rsplit("@", 1)[-1].strip().lower() in DWD_DOMAINS if "@" in email else False


def plan_ensure(
    day: str,
    cfg: CaptureConfig,
    *,
    list_events: Callable[[str, str], list[dict[str, Any]]] | None = None,
) -> EnsureResult:
    """Work out, without writing anything, what the ensure lane would do for `day`.

    Coverage is decided by ONE question: is this meeting already reachable from the
    capture identity's calendar? That is true if cora@ is an attendee on the source
    event, OR if the same meeting link already sits on cora@'s own calendar (from an
    earlier guest-add or copy). Keying on the LINK rather than on our own copy
    marker means a meeting Harrison invited cora@ to by hand also counts as covered
    -- which is what makes this safe to run alongside the manual habit during the
    Phase-2 overlap instead of duplicating it.
    """
    from cora.tools.calendar_client import CAPTURE_COPY_MARKER, extract_meeting_link

    if list_events is None:
        from cora.tools.calendar_client import list_events_for_day

        def list_events(email: str, d: str) -> list[dict[str, Any]]:
            return list_events_for_day(email, d)

    result = EnsureResult(day=day, mode=ensure_mode(), applied=False)

    # What the capture identity can already see.
    # Keyed (link, start) exactly like meeting_key -- NOT by link alone. A person's
    # static personal room link is reused for every 1:1 they host, so a link-only
    # set marks the SECOND meeting of the day on that link as already-covered and
    # it is then never ensured: a silent capture miss, the one failure this lane
    # exists to prevent.
    covered_meetings: set[tuple] = set()
    capture_events: list[dict[str, Any]] = []
    try:
        for ev in list_events(cfg.capture_identity, day):
            capture_events.append(ev)
            if extract_meeting_link(ev):
                covered_meetings.add(meeting_key(ev))
    except Exception as exc:  # noqa: BLE001
        # Honest degrade: without this read we cannot tell covered from uncovered,
        # so we refuse to plan writes rather than risk duplicating every meeting.
        result.failed_calendars.append((cfg.capture_identity, str(exc)[:200]))
        log.error("ensure: cannot read the capture identity's calendar (%s) -- planning nothing", exc)
        return result

    # Collect first, decide second. One meeting can surface as several calendar
    # events (see meeting_key), and we must act on it exactly once.
    #
    # CARVE-OUTS VETO THE MEETING, NOT ONE CALENDAR'S VIEW OF IT. Qualifying
    # per-event and grouping only the survivors would mean a `[no-bot]` Harrison
    # typed on HIS copy is ignored because Hannah's copy of the same meeting has
    # the original title -- the lane would record a meeting somebody explicitly
    # opted out of. So every copy is qualified, and ONE veto kills the meeting.
    candidates: dict[tuple, list[tuple[RosterMember, dict[str, Any]]]] = {}
    vetoed: dict[tuple, tuple[RosterMember, dict[str, Any], str]] = {}
    for member in cfg.active_members:
        try:
            events = list_events(member.calendar_email, day)
        except Exception as exc:  # noqa: BLE001
            result.failed_calendars.append((member.calendar_email, str(exc)[:200]))
            log.warning("ensure: calendar read failed for %s: %s", member.calendar_email, exc)
            continue

        for ev in events:
            if not (ev.get("id") or "").strip():
                continue
            if not starts_on_day(ev, day):
                continue   # belongs to the adjacent day; acted on there
            key = meeting_key(ev)
            q = qualify_event(ev, cfg, roster_email=member.calendar_email)
            if not q.qualifies:
                if key not in vetoed:
                    vetoed[key] = (member, ev, q.reason)
                continue
            candidates.setdefault(key, []).append((member, ev))

    # A veto on ANY copy removes the meeting from consideration entirely.
    for key, (member, ev, reason) in vetoed.items():
        candidates.pop(key, None)
        result.actions.append(EnsureAction(
            member=member.name, calendar_email=member.calendar_email,
            event_id=(ev.get("id") or ""), title=display_title(ev),
            start_label=event_time_label(ev), action="skip", reason=reason,
        ))

    for _key, entries in candidates.items():
        # Prefer to act through a copy whose organiser we can impersonate -- that is
        # the guest-add path, which is transparent to the room and rides the
        # organiser's own updates. Otherwise any copy will do; it becomes an
        # event copy either way.
        def _rank(pair: tuple[RosterMember, dict[str, Any]]) -> tuple[int, str]:
            org = (pair[1].get("organizer") or {}) if isinstance(pair[1].get("organizer"), dict) else {}
            org_email = (org.get("email") or "").strip().lower()
            return (0 if _is_dwd_domain(org_email) else 1, pair[1].get("id") or "")

        member, ev = sorted(entries, key=_rank)[0]
        eid = (ev.get("id") or "").strip()
        link = extract_meeting_link(ev).strip().lower()
        safe = display_title(ev)

        emails = event_emails(ev)
        if cfg.capture_identity in emails:
            covered_reason = "already-covered"
        elif LEGACY_NOTETAKER in emails:
            covered_reason = "legacy notetaker already invited -- not adding a second bot"
        elif meeting_key(ev) in covered_meetings:
            covered_reason = "already-covered"
        else:
            covered_reason = ""
        if covered_reason:
            result.actions.append(EnsureAction(
                member=member.name, calendar_email=member.calendar_email, event_id=eid,
                title=safe, start_label=event_time_label(ev),
                action="none", reason=covered_reason, meeting_link=link,
            ))
            continue

        organizer = (ev.get("organizer") or {}) if isinstance(ev.get("organizer"), dict) else {}
        org_email = (organizer.get("email") or "").strip().lower()
        # If the title was withheld the organiser must be too, on THIS row as well:
        # the reason is printed to the console and persisted to the ledger, so
        # naming an agency address here would undo the redaction two fields away.
        who = "withheld" if safe.startswith("LEX/PHI") else (org_email or "unknown")
        if org_email and _is_dwd_domain(org_email):
            action, reason = "guest-add", f"in-domain organizer {who}"
        else:
            action, reason = "copy", f"external organizer {who}"
        if len(entries) > 1:
            reason = f"{reason}; {len(entries)} calendar copies of this meeting, acting once"

        # Claim the meeting immediately so it cannot be planned twice in one run.
        covered_meetings.add(meeting_key(ev))

        result.actions.append(EnsureAction(
            member=member.name, calendar_email=member.calendar_email, event_id=eid,
            title=safe, start_label=event_time_label(ev),
            action=action, reason=reason, meeting_link=link,
        ))

    # RECONCILE OUR OWN COPIES. A copy is a snapshot: when the source meeting is
    # moved or cancelled the copy stays behind, and the plan of record promises
    # re-sync rather than a growing pile of ghosts on the capture calendar. A ghost
    # is not merely clutter -- it sends the notetaker to that Meet link at a time
    # nobody agreed to, which is a capture nobody consented to.
    #
    # Only copies THIS LANE created are ever touched: they are identified by the
    # marker written into their description, so a human-created event on the
    # capture calendar is never a candidate for deletion.
    live_keys = {meeting_key(ev) for _m, ev in
                 [(m, e) for entries in candidates.values() for m, e in entries]}
    for ev in capture_events:
        desc = ev.get("description") or ""
        if CAPTURE_COPY_MARKER not in desc:
            continue
        if meeting_key(ev) in live_keys:
            continue
        result.stale_copies.append((
            (ev.get("id") or ""),
            "source meeting no longer scheduled at this time (moved, cancelled, or carved out)",
        ))

    return result


def execute_ensure(
    result: EnsureResult,
    cfg: CaptureConfig,
    *,
    apply: bool = False,
    source_events: dict[str, dict[str, Any]] | None = None,
) -> EnsureResult:
    """Carry out a planned ensure run. Writes ONLY when mode=="live" AND apply.

    Two independent gates by design. The env flag is the operator's switch and the
    CLI flag is the runner's; either one alone leaves the lane read-only, so no
    single mistake can start writing to people's calendars.

    Guest-add failures degrade to a copy rather than to nothing: a 403 here is the
    ordinary "guests cannot invite others" case, and the whole point of the hybrid
    is that the copy path covers exactly what guest-add cannot reach.
    """
    from cora.tools import calendar_client as cc

    mode = ensure_mode()
    result.mode = mode
    writing = bool(apply and mode == "live")
    result.applied = writing
    if not writing:
        return result

    source_events = source_events or {}
    for act in result.actions:
        if act.action not in ("guest-add", "copy"):
            continue
        try:
            if act.action == "guest-add":
                try:
                    changed, why = cc.add_attendee(
                        user_email=act.calendar_email,
                        event_id=act.event_id,
                        attendee_email=cfg.capture_identity,
                    )
                    act.applied = True
                    act.reason = f"{act.reason} -> {why}" if changed else "already-present"
                    continue
                except Exception as exc:  # noqa: BLE001
                    log.info("ensure: guest-add failed for %s (%s) -- falling back to copy",
                             act.event_id, str(exc)[:160])
                    act.action = "copy"
                    act.reason = f"guest-add refused ({str(exc)[:80]}) -> copy"

            src = source_events.get(act.event_id)
            if src is None:
                src = cc.get_event(user_email=act.calendar_email, event_id=act.event_id)
            cc.insert_event_copy(target_email=cfg.capture_identity, source_event=src)
            act.applied = True
        except Exception as exc:  # noqa: BLE001
            act.error = str(exc)[:200]
            log.error("ensure: %s failed for event %s: %s", act.action, act.event_id, exc)

    # Remove ghosts, under the same gates as every other write. Deleting only ever
    # touches an event on the capture identity's OWN calendar that carries this
    # lane's marker -- never a human's event, and never anything on a roster
    # member's calendar.
    for event_id, why in list(result.stale_copies):
        try:
            cc.delete_event(user_email=cfg.capture_identity, event_id=event_id)
            log.info("ensure: removed stale capture copy %s (%s)", event_id, why)
        except Exception as exc:  # noqa: BLE001
            log.error("ensure: could not remove stale capture copy %s: %s", event_id, exc)

    return result


# ── the daily auditor (READ-ONLY, ships live) ────────────────────────────────
#
# Restores the CAPTURE-GAP slice of a Cowork-side sweep that went dark 2026-07-24.
# Deliberately narrow language: that sweep also drafted Asana tasks, appended
# decisions and posted summaries, and none of that is rebuilt here. What is
# rebuilt is the one thing whose absence let duplicate captures run unnoticed for
# three months -- a daily, deterministic "did what we scheduled actually get
# captured, exactly once?".
#
# No LLM anywhere in this path. A capture-gap report that hallucinates a meeting,
# or quietly summarises away a miss, is worse than no report.

#: How far before the audited day to ask Fireflies for transcripts. Fireflies
#: filters on MEETING date, but a transcript can land hours after the meeting; a
#: window that starts exactly at midnight would miss a late-processed capture of a
#: late-evening meeting and report it as a MISS. One day of slack on each side is
#: cheap -- transcripts outside the audited day are filtered out after the join.
_TRANSCRIPT_LOOKBACK_DAYS = 1
_TRANSCRIPT_LOOKAHEAD_DAYS = 2


@dataclass
class AuditedMeeting:
    event_id: str
    title: str            # already display-safe
    start_label: str
    organizer: str
    members: list[str] = field(default_factory=list)
    transcript_ids: list[str] = field(default_factory=list)
    match_basis: str = ""
    #: EVERY calendar event id that is a copy of this one meeting. An externally
    #: organised meeting has a different id on each invitee's calendar, and a
    #: transcript's cal_id may name any one of them.
    event_ids: list[str] = field(default_factory=list)


@dataclass
class AuditReport:
    day: str
    scheduled: int = 0
    captured: int = 0
    misses: list[AuditedMeeting] = field(default_factory=list)
    duplicates: list[AuditedMeeting] = field(default_factory=list)
    unmatched_transcripts: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    #: A meeting a carve-out removed from scope that WAS captured anyway. The most
    #: serious thing this auditor can find: a recording exists of a meeting somebody
    #: ruled must not be recorded. Suppress the TITLE, never the FACT.
    carve_out_breaches: list[tuple[str, str]] = field(default_factory=list)   # (safe title, reason)
    failed_calendars: list[tuple[str, str]] = field(default_factory=list)
    transcript_error: str = ""
    seat_note: str = ""


#: Marks an index key claimed by more than one meeting. Such a key can never
#: identify anything, so it must not resolve to the first claimant.
_AMBIGUOUS = ("__ambiguous__",)


def _norm_title(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _transcript_ts(t: dict[str, Any]) -> int:
    from cora.connectors.fireflies_connector import _parse_date

    return _parse_date(t.get("date")) or 0


def audit_day(
    day: str,
    cfg: CaptureConfig,
    *,
    list_events: Callable[[str, str], list[dict[str, Any]]] | None = None,
    fetch_transcripts: Callable[[str, str], list[dict[str, Any]]] | None = None,
    fetch_seats: Callable[[], list[dict[str, Any]]] | None = None,
) -> AuditReport:
    """Diff one day's scheduled roster meetings against Fireflies transcripts.

    Every external read is injectable so the whole diff is testable without a
    network. Each read also degrades independently: a calendar that 403s is named
    in the report rather than silently shrinking the denominator, because "0 misses"
    computed from a half-read roster is the single most dangerous output this thing
    could produce.
    """
    report = AuditReport(day=day)

    if list_events is None:
        from cora.tools.calendar_client import list_events_for_day

        def list_events(email: str, d: str) -> list[dict[str, Any]]:
            return list_events_for_day(email, d)

    # ── 1. what was scheduled ──
    # Keyed by MEETING, not by event: one externally-organised meeting lands on
    # each invitee's calendar with its own event id, and counting those separately
    # would inflate `scheduled` and report a captured meeting as a miss (measured
    # live 2026-08-26). See meeting_key.
    #
    # COLLECT THEN DECIDE, exactly as the ensure lane does. A carve-out on ANY copy
    # vetoes the whole meeting -- otherwise a `[no-bot]` Harrison typed on his copy
    # is ignored because Hannah's copy still has the original title, and the
    # meeting is then reported as a MISS, which is both wrong and a nag to "fix"
    # a gap he created on purpose.
    grouped: dict[tuple, list[tuple[RosterMember, dict[str, Any]]]] = {}
    for member in cfg.active_members:
        try:
            events = list_events(member.calendar_email, day)
        except Exception as exc:  # noqa: BLE001
            report.failed_calendars.append((member.calendar_email, str(exc)[:200]))
            continue
        for ev in events:
            if not (ev.get("id") or "").strip():
                continue
            if not starts_on_day(ev, day):
                continue   # audited on the day it starts, never on both
            grouped.setdefault(meeting_key(ev), []).append((member, ev))

    meetings: dict[tuple, AuditedMeeting] = {}
    raw_events: dict[tuple, dict[str, Any]] = {}
    #: meetings a carve-out removed from scope, kept so we can still notice if one
    #: of them was recorded anyway.
    carved: dict[tuple, tuple[dict[str, Any], str]] = {}

    for key, entries in grouped.items():
        veto: tuple[dict[str, Any], str] | None = None
        qualifying: list[tuple[RosterMember, dict[str, Any]]] = []
        for member, ev in entries:
            q = qualify_event(ev, cfg, roster_email=member.calendar_email)
            if q.qualifies:
                qualifying.append((member, ev))
            elif veto is None:
                veto = (ev, q.reason)

        if veto is not None:
            ev, reason = veto
            carved[key] = (ev, reason)
            # Only report skips that reflect a DECISION. Structural non-meetings
            # (an out-of-office block, a focus-time hold) are noise in an ops
            # channel and would bury the carve-outs a human should actually see.
            if not reason.startswith(("not-a-meeting", "no-meeting-link", "cancelled", "all-day")):
                # SHAPE, never the title. A no-record meeting is by definition one
                # somebody ruled must not be recorded; printing "Call with counsel"
                # into a shared ops channel publishes the very thing the carve-out
                # exists to keep out. The count and the reason are what is needed.
                report.skipped.append((f"a meeting at {event_time_label(ev)}", reason))
            continue

        if not qualifying:
            continue

        member, ev = qualifying[0]
        organizer = (ev.get("organizer") or {}) if isinstance(ev.get("organizer"), dict) else {}
        safe_title = display_title(ev)
        # When the title is withheld the organiser must be too: an agency address
        # alone ("organizer vreese@azdes.gov") names the client programme the title
        # was redacted to protect.
        redacted = safe_title.startswith("LEX/PHI")
        meetings[key] = AuditedMeeting(
            event_id=(ev.get("id") or "").strip(),
            title=safe_title,
            start_label=event_time_label(ev),
            organizer=("withheld" if redacted else (organizer.get("email") or "unknown")),
            members=[m.name for m, _ in qualifying],
            event_ids=[(e.get("id") or "").strip() for _m, e in qualifying],
        )
        raw_events[key] = ev

    report.scheduled = len(meetings)
    #: every calendar event id belonging to a meeting, so a transcript whose cal_id
    #: names ANY copy of it still joins to the one meeting.
    key_by_event_id: dict[str, tuple] = {}
    for key, meeting in meetings.items():
        for eid in meeting.event_ids:
            if eid:
                key_by_event_id[eid] = key

    # ── 2. what was captured ──
    transcripts: list[dict[str, Any]] = []
    if fetch_transcripts is None:
        fetch_transcripts = _default_fetch_transcripts
    try:
        start = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=_TRANSCRIPT_LOOKBACK_DAYS))
        end = (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=_TRANSCRIPT_LOOKAHEAD_DAYS))
        transcripts = fetch_transcripts(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
    except Exception as exc:  # noqa: BLE001
        report.transcript_error = str(exc)[:200]
        log.error("audit: Fireflies transcript fetch failed: %s", exc)

    # Restrict to transcripts whose meeting actually fell on the audited day (AZ).
    day_start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=_AZ)
    lo, hi = int(day_start.timestamp()), int((day_start + timedelta(days=1)).timestamp())
    same_day = [t for t in transcripts if lo <= _transcript_ts(t) < hi]

    # ── 3. join: cal_id is the Google event id VERBATIM, so this is exact ──
    from cora.tools.calendar_client import extract_meeting_link

    by_meeting: dict[tuple, list[dict[str, Any]]] = {}
    used: set[str] = set()
    for t in same_day:
        cal_id = (t.get("cal_id") or "").strip()
        key = key_by_event_id.get(cal_id) if cal_id else None
        if key is not None:
            by_meeting.setdefault(key, []).append(t)
            used.add(t.get("id") or "")

    # Fallback A: meeting link. About half of live transcripts carry no cal_id at
    # all, so without this the auditor would report most captured meetings missed.
    # AMBIGUITY IS NOT A MATCH. setdefault would silently bind a shared link (a
    # static personal room used for several meetings a day) to whichever meeting
    # was seen first -- attaching the transcript to the WRONG meeting, which reads
    # as a duplicate on one row and a miss on another. A key claimed by more than
    # one meeting is dropped from the index instead: the transcript falls through
    # to a weaker join, or is reported unmatched, which is honest.
    link_index: dict[str, tuple] = {}
    for key, ev in raw_events.items():
        link = extract_meeting_link(ev).strip().lower()
        if not link:
            continue
        if link in link_index and link_index[link] != key:
            link_index[link] = _AMBIGUOUS
        else:
            link_index.setdefault(link, key)
    for t in same_day:
        if (t.get("id") or "") in used:
            continue
        link = (t.get("meeting_link") or "").strip().lower()
        key = link_index.get(link) if link else None
        if key is not None and key is not _AMBIGUOUS:
            by_meeting.setdefault(key, []).append(t)
            used.add(t.get("id") or "")

    # Fallback B: normalised title within the same day. Last resort -- a title is
    # not an identity, so this can only ever attach a transcript to a meeting we
    # already know was scheduled that day.
    title_index: dict[str, tuple] = {}
    for key, ev in raw_events.items():
        nt = _norm_title(ev.get("summary") or "")
        if not nt:
            continue
        if nt in title_index and title_index[nt] != key:
            title_index[nt] = _AMBIGUOUS
        else:
            title_index.setdefault(nt, key)
    for t in same_day:
        if (t.get("id") or "") in used:
            continue
        key = title_index.get(_norm_title(t.get("title") or ""))
        if key is not None and key is not _AMBIGUOUS:
            by_meeting.setdefault(key, []).append(t)
            used.add(t.get("id") or "")

    for key, meeting in meetings.items():
        hits = by_meeting.get(key) or []
        meeting.transcript_ids = [h.get("id") or "" for h in hits]
        if not hits:
            report.misses.append(meeting)
        else:
            report.captured += 1
            if len(hits) > 1:
                report.duplicates.append(meeting)

    # ── 3b. was anything captured that a carve-out excluded? ──
    # Dropping carved meetings from the diff entirely would hide this: a bot
    # recording a no-record meeting is exactly what the carve-out exists to
    # prevent, so it is surfaced -- as a shape, never as a title.
    carved_event_ids: dict[str, tuple] = {}
    carved_links: dict[str, tuple] = {}
    for c_key, (c_ev, _c_reason) in carved.items():
        c_eid = (c_ev.get("id") or "").strip()
        if c_eid:
            carved_event_ids[c_eid] = c_key
        c_link = extract_meeting_link(c_ev).strip().lower()
        if c_link:
            carved_links[c_link] = c_key
    for t in same_day:
        if (t.get("id") or "") in used:
            continue
        hit = carved_event_ids.get((t.get("cal_id") or "").strip())
        if hit is None:
            t_link = (t.get("meeting_link") or "").strip().lower()
            hit = carved_links.get(t_link) if t_link else None
        if hit is None:
            continue
        used.add(t.get("id") or "")
        b_ev, b_reason = carved[hit]
        report.carve_out_breaches.append(
            (f"a meeting at {event_time_label(b_ev)}", b_reason)
        )

    # ── 4. captured but not on any roster calendar ──
    for t in same_day:
        if (t.get("id") or "") in used:
            continue
        safe_t = _transcript_display_title(t)
        report.unmatched_transcripts.append({
            "id": t.get("id") or "",
            "title": safe_t,
            "organizer": (
                "withheld" if safe_t.startswith("LEX/PHI")
                else (t.get("organizer_email") or t.get("host_email") or "unknown")
            ),
            "fred_joined": bool(((t.get("meeting_info") or {}) or {}).get("fred_joined") is True),
        })

    # ── 4b. is the exact join even available? ──
    # If the connector fell back to the legacy selection set, no transcript carries
    # cal_id and the whole diff is running on link/title fallbacks. That degrades
    # accuracy invisibly unless the report says so.
    try:
        from cora.connectors import fireflies_connector as _ffc

        if getattr(_ffc, "_extended_query_unavailable", False):
            report.transcript_error = (
                report.transcript_error
                or "Fireflies rejected the extended fields; running without the exact "
                   "cal_id join (link/title fallbacks only)"
            )
    except Exception:  # noqa: BLE001
        pass

    # ── 5. seat posture: the one-mechanism rule, watched ──
    if fetch_seats is None:
        fetch_seats = _default_fetch_seats
    try:
        seats = fetch_seats()
        emails = sorted((s.get("email") or "").lower() for s in seats if s.get("email"))
        identity_live = cfg.capture_identity in emails
        report.seat_note = (
            f"{len(emails)} Fireflies seat(s); capture identity "
            f"{'ACTIVE' if identity_live else 'NOT YET ACTIVE'}"
        )
    except Exception as exc:  # noqa: BLE001
        report.seat_note = f"seat roster unavailable ({str(exc)[:80]})"

    return report


def _transcript_display_title(t: dict[str, Any]) -> str:
    """LEX rail for a TRANSCRIPT (the calendar-event rail cannot see these).

    An unmatched transcript is by definition one we have no calendar event for, so
    display_title's event path does not apply. Route it through the same shared LEX
    detector in its native shape.
    """
    when = (
        datetime.fromtimestamp(_transcript_ts(t), _AZ).strftime("%H:%M")
        if _transcript_ts(t) else "?"
    )
    try:
        from cora.connectors.fireflies_connector import classify_lex_meeting

        # The client-domain screen must apply on BOTH rails. It was added to
        # display_title and not here, which left the transcript side -- the one that
        # renders captures with NO calendar event, i.e. the least-known meetings --
        # weaker than the event side.
        if classify_lex_meeting(t).is_lex or _transcript_has_client_domain(t):
            return f"LEX/PHI meeting, {when}"
    except Exception:  # noqa: BLE001
        return "LEX/PHI meeting (classification failed)"
    title = (t.get("title") or "(untitled)").strip()
    try:
        from cora.phi_guard import is_any_phi

        if is_any_phi(title):
            return "LEX/PHI meeting"
    except Exception:  # noqa: BLE001
        return "LEX/PHI meeting"
    return title


def _transcript_has_client_domain(t: dict[str, Any]) -> bool:
    """Client-agency domain on a TRANSCRIPT's attendees (the event rail's twin)."""
    try:
        from cora.connectors.fireflies_connector import (
            _load_lex_detect_cfg,
            _transcript_emails,
        )

        suffixes = tuple(_load_lex_detect_cfg().get("client_domain_suffixes") or ())
        if not suffixes:
            return False
        return any(
            addr.rsplit("@", 1)[-1].endswith(suffixes)
            for addr in _transcript_emails(t) if "@" in addr
        )
    except Exception:  # noqa: BLE001
        return True   # fail toward redaction


def _default_fetch_transcripts(from_date: str, to_date: str) -> list[dict[str, Any]]:
    """Ask Fireflies directly -- NOT the KB (see the module docstring)."""
    from cora.connectors.fireflies_connector import _BATCH_SIZE, _query_transcripts

    out: list[dict[str, Any]] = []
    skip = 0
    while True:
        data = _query_transcripts({
            "limit": _BATCH_SIZE, "skip": skip,
            "fromDate": from_date, "toDate": to_date,
        })
        batch = data.get("transcripts") or []
        out.extend(batch)
        if len(batch) < _BATCH_SIZE:
            break
        skip += _BATCH_SIZE
        if skip > 500:   # a day's audit can never legitimately need more
            break
    return out


def _default_fetch_seats() -> list[dict[str, Any]]:
    from cora.connectors.fireflies_connector import list_team_members

    return list_team_members()


# ── report rendering ─────────────────────────────────────────────────────────

def _esc(text: str) -> str:
    """Slack-escape untrusted text before it goes into an mrkdwn body.

    Calendar titles are USER-AUTHORED and really do contain angle brackets -- live
    examples on the roster include "Harrison <> Lukas BevNET" and "Tommy x Hannah
    <> Asana + HubSpot". In Slack mrkdwn `<...>` is link syntax, so an unescaped
    title renders mangled (and `&` can start an entity). sanitize_text deliberately
    preserves `<...>` because other callers depend on real link markup, so the
    escaping has to happen here, on the untrusted fragment only -- never over the
    whole message, which would destroy our own formatting.
    """
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_report(report: AuditReport) -> str:
    """Plain mrkdwn, alarms first. No Block Kit -- this message carries no buttons,
    and adding blocks would impose a 2,900-char section cap on a body that has none.
    """
    lines: list[str] = [f"*Meeting capture audit -- {report.day}*"]

    degraded = bool(report.failed_calendars or report.transcript_error)
    if report.transcript_error:
        # The headline must never read as a clean day when the capture side of the
        # diff never loaded. Misses would all be false; say so before anything else.
        lines.append(
            f":rotating_light: *Fireflies read FAILED* -- {_esc(report.transcript_error)}. "
            "Capture results below are NOT trustworthy."
        )
    if report.failed_calendars:
        who = ", ".join(f"{_esc(e)} ({_esc(err[:60])})" for e, err in report.failed_calendars)
        lines.append(f":warning: *{len(report.failed_calendars)} calendar(s) unreadable* -- {who}")

    lines.append(
        f"{report.scheduled} scheduled, {report.captured} captured, "
        f"{len(report.misses)} missed, {len(report.duplicates)} duplicated"
        + (" _(partial -- see above)_" if degraded else "")
    )

    if report.carve_out_breaches:
        lines.append(
            f"\n*:rotating_light: RECORDED DESPITE A CARVE-OUT "
            f"({len(report.carve_out_breaches)})*"
        )
        for shape, reason in report.carve_out_breaches[:10]:
            lines.append(f"  - {_esc(shape)}  _({_esc(reason)})_")
        if len(report.carve_out_breaches) > 10:
            lines.append(f"  _...and {len(report.carve_out_breaches) - 10} more_")

    if report.misses:
        lines.append(f"\n*:red_circle: Not captured ({len(report.misses)})*")
        # Chronological. Meetings are collected per roster member, so insertion
        # order interleaves each person's day and reads as scrambled times.
        for m in sorted(report.misses, key=lambda x: x.start_label)[:15]:
            lines.append(f"  - {m.start_label}  {_esc(m.title)}  _(organizer {_esc(m.organizer)})_")
        if len(report.misses) > 15:
            lines.append(f"  _...and {len(report.misses) - 15} more_")

    if report.duplicates:
        lines.append(f"\n*:heavy_multiplication_x: Captured more than once ({len(report.duplicates)})*")
        for m in sorted(report.duplicates, key=lambda x: x.start_label)[:10]:
            lines.append(f"  - {m.start_label}  {_esc(m.title)}  ({len(m.transcript_ids)} transcripts)")
        if len(report.duplicates) > 10:
            lines.append(f"  _...and {len(report.duplicates) - 10} more_")

    if report.unmatched_transcripts:
        lines.append(
            f"\n*:grey_question: Captured but not on a roster calendar "
            f"({len(report.unmatched_transcripts)})*"
        )
        for t in report.unmatched_transcripts[:10]:
            flag = " _[fred]_" if t.get("fred_joined") else ""
            lines.append(f"  - {_esc(t['title'])}  _(organizer {_esc(t['organizer'])})_{flag}")
        if len(report.unmatched_transcripts) > 10:
            lines.append(f"  _...and {len(report.unmatched_transcripts) - 10} more_")

    if report.skipped:
        lines.append(f"\n*:no_entry_sign: Carve-outs applied ({len(report.skipped)})*")
        for title, reason in report.skipped[:10]:
            lines.append(f"  - {_esc(title)}  _({_esc(reason)})_")
        if len(report.skipped) > 10:
            lines.append(f"  _...and {len(report.skipped) - 10} more_")

    if not (report.misses or report.duplicates or report.unmatched_transcripts
            or report.carve_out_breaches) and not degraded:
        # A weekend has no meetings, and "captured exactly once" over a denominator
        # of zero reads as a success it did not earn. This report posts every day.
        lines.append(
            "\n:white_check_mark: No qualifying roster meetings scheduled."
            if report.scheduled == 0
            else "\n:white_check_mark: Every scheduled meeting captured exactly once."
        )

    if report.seat_note:
        lines.append(f"\n_{report.seat_note}_")
    return "\n".join(lines)
