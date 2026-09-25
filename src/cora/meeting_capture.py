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


def title_carve_out_reason(title: str, cfg: CaptureConfig) -> str:
    """The TITLE carve-outs, as one matcher: "title-marker:<m>" / "no-record-title:<term>",
    or "" when no title carve-out applies.

    One function so the event gate (qualify_event) and the auditor's transcript belt
    can never disagree about what a no-record title is (Code #14 r4: the auditor
    printed a carved COPA meeting's title because only the event side checked).
    Markers are literal substrings (brackets are the point); patterns are whole words.
    """
    lowered = _norm_ws(title or "").lower()
    for marker in cfg.skip_title_markers:
        if _norm_ws(marker) in lowered:
            return f"title-marker:{marker}"
    for term in cfg.no_record_title_patterns:
        if _matches_word(title or "", term):
            return f"no-record-title:{term}"
    return ""


#: The reason PREFIXES that mean "somebody ruled this meeting must not be recorded"
#: (Code #15 S4, cq-5f44ce934aeb). Only these make a recorded carved meeting a
#: carve-out BREACH. The literal names ("no_record_title", "[no-bot]") match no real
#: reason string -- every one of these is a prefix followed by a value, and the
#: marker list is roster config -- so the match is on prefix, never on equality.
NO_RECORD_REASON_PREFIXES: tuple[str, ...] = (
    "title-marker:", "no-record-title:", "no-record-email:", "no-record-domain:",
)

#: Every OTHER reason qualify_event can produce: the meeting is out of scope for
#: capture (not a meeting, no link, not attending), not ruled unrecordable. A
#: recording of one is reported as information, never alarmed as a breach. Exact
#: strings plus one prefix; anything in neither set is UNKNOWN and fails closed to a
#: breach (see classify_carved_recording).
QUALIFICATION_SKIP_REASONS: frozenset[str] = frozenset({
    "cancelled", "no-meeting-link", "all-day", "roster-user-declined",
})
QUALIFICATION_SKIP_PREFIXES: tuple[str, ...] = ("not-a-meeting:",)


def is_no_record_reason(reason: str) -> bool:
    """True for a no-record carve-out reason (a breach if it was recorded anyway)."""
    return (reason or "").startswith(NO_RECORD_REASON_PREFIXES)


def is_qualification_skip_reason(reason: str) -> bool:
    """True for a qualification skip (out of scope; a recording of it is no breach)."""
    r = reason or ""
    return r in QUALIFICATION_SKIP_REASONS or r.startswith(QUALIFICATION_SKIP_PREFIXES)


def no_record_reason(event: dict[str, Any], cfg: CaptureConfig) -> str:
    """The event's NO-RECORD carve-out reason, or "" when none applies.

    Evaluated REGARDLESS of qualify_event's order. qualify_event returns the first
    failing check, and a decline, a cancel or a missing link is checked before the
    carve-outs -- so its single reason can hide a `[no-bot]` on the same copy. The
    auditor asks this directly of every copy; qualify_event delegates to it, so the
    two can never disagree about what a no-record meeting is (one matcher, as
    title_carve_out_reason is for titles). Order and strings are qualify_event's:
    title, then address, then domain.
    """
    title_reason = title_carve_out_reason(event.get("summary") or "", cfg)
    if title_reason:
        return title_reason

    emails = event_emails(event)
    hit = emails & cfg.no_record_emails
    if hit:
        return f"no-record-email:{sorted(hit)[0]}"

    if cfg.no_record_attendee_domains:
        # sorted: `emails` is a set, and with two matching domains the reason must
        # not depend on string-hash order (the address branch above already sorts).
        for addr in sorted(emails):
            domain = addr.rsplit("@", 1)[-1]
            if domain in cfg.no_record_attendee_domains:
                return f"no-record-domain:{domain}"
    return ""


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

    Order matters: cheap structural disqualifiers first, then the roster user's own
    decline, then the no-record carve-outs. Every non-qualifying outcome carries a
    distinct reason string so the auditor can report WHY a meeting was skipped -- an
    unexplained skip is indistinguishable from a bug. Every reason is classified as
    either a qualification skip (QUALIFICATION_SKIP_REASONS / _PREFIXES) or a
    no-record carve-out (NO_RECORD_REASON_PREFIXES); a new reason must be added to
    one of them (the suite pins this by source scan).
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

    # The roster user declining is a QUALIFICATION skip -- they are not attending --
    # not a no-record carve-out. It still vetoes ACTING on the meeting (the ensure
    # lane never adds a bot to a meeting a roster member declined, and the auditor
    # leaves it out of `scheduled`), but a recording of it is not a carve-out breach:
    # nobody ruled the meeting unrecordable (Code #15 S4, cq-5f44ce934aeb; the 9/18
    # false alarm on a standing weekly meeting one of four invitees declined).
    for att in (event.get("attendees") or []):
        if not isinstance(att, dict):
            continue
        addr = (att.get("email") or "").strip().lower()
        is_self = bool(att.get("self")) or (roster_email and addr == roster_email.lower())
        if is_self and (att.get("responseStatus") or "").strip().lower() == "declined":
            return Qualification(False, "roster-user-declined")

    # The no-record carve-outs (title, address, domain) -- ONE matcher, shared with
    # the auditor's breach check (no_record_reason).
    no_record = no_record_reason(event, cfg)
    if no_record:
        return Qualification(False, no_record)

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


def rsvp_counts(day: str, *, path: Path | None = None) -> dict[str, Any]:
    """READ-ONLY count of cora@'s own RSVP accepts for meetings on `day`.

    Reads the ensure lane's `rsvp-accept` rows (lane=ensure, action=rsvp-accept,
    day==day, applied) and counts DISTINCT meetings, keyed on the D-254 identity
    (meeting_link, start_ts) with the event id as the fallback, because the lane
    re-runs every 15 minutes and an erroring accept is re-ledgered each cycle:

      accepted          -- non-LEX accepts
      accepted_lex      -- LEX accepts (`accepted:lex`, ruled 2026-09-19 ask 9.3)
      errors            -- meetings whose accept errored and was never accepted
      notetaker_present -- meetings skipped because a legacy bot was already invited
      available         -- False when the ledger cannot be read at all

    Never writes, never raises, and returns counts only (no ids, no titles), so
    the 07:22 audit may put them on a shared surface (D-145). A malformed line is
    skipped, not fatal.
    """
    out: dict[str, Any] = {
        "available": False, "accepted": 0, "accepted_lex": 0,
        "errors": 0, "notetaker_present": 0,
    }
    p = Path(path) if path is not None else ledger_path()
    outcomes: dict[tuple, set[str]] = {}
    try:
        if p.exists():
            with p.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(row, dict):
                        continue
                    if (row.get("lane") != "ensure" or row.get("action") != "rsvp-accept"
                            or row.get("day") != day or row.get("applied") is not True):
                        continue
                    link = str(row.get("meeting_link") or "").strip().lower()
                    start = row.get("start_ts") or 0
                    key: tuple = (link, start) if link and start else ("id", str(row.get("event_id") or ""))
                    outcomes.setdefault(key, set()).add(str(row.get("outcome") or ""))
        out["available"] = True
    except Exception as exc:  # noqa: BLE001
        log.warning("rsvp_counts: ledger unreadable (%s)", type(exc).__name__)
        return out
    for seen in outcomes.values():
        if "accepted:lex" in seen:
            out["accepted_lex"] += 1
        elif "accepted" in seen:
            out["accepted"] += 1
        elif "error" in seen:
            out["errors"] += 1
        elif "skipped:notetaker-present" in seen:
            out["notetaker_present"] += 1
    return out


# ── the ensure lane ──────────────────────────────────────────────────────────

#: RSVP sub-step outcomes (cq-19b0298cf5be, R1 2026-09-08). A SUB-STEP on an
#: action row, deliberately NOT a fifth `action` value: EnsureResult's counts and
#: the script's `_ledger_worthy` key on the four-word action vocabulary, and a new
#: word there would silently fall out of every count.
#:
#: LEX IS INCLUDED (ruled 2026-09-19, ask 9.3). The old `skipped:lex-withheld`
#: outcome is RETIRED for cora@'s OWN RSVP only: a LEX invite is accepted like any
#: other and LABELLED `accepted:lex` so the 07:22 audit can count it separately.
#: Nothing else about LEX changes here -- qualify_event's carve-outs, the
#: veto-on-any-copy rule, the no-roster-copy skip, the notetaker check, the dual
#: write gate, display_title's redaction and meeting_recap's custodians-only
#: distribution are all untouched. The D-247 tension (attendance now == recording,
#: because Fireflies dispatches only to an accepted invite) was accepted by Harrison.
RSVP_OUTCOMES: frozenset[str] = frozenset({
    "",                          # no RSVP step (or not yet executed)
    "accepted",                  # cora@'s own entry set to accepted, read back verified
    "accepted:lex",              # the same write on a LEX event (is_lex_event) -- a LABEL, never a withhold
    "already-accepted",          # no API write
    "skipped:notetaker-present", # one mechanism per event -- a bot is already invited
    "skipped:no-roster-copy",    # sweep found cora@ invited but no roster copy to veto-check
    "error",                     # fetch/patch/read-back failed; rsvp_error carries why
})

#: Reason prefix on the sweep's no-roster-copy skip row. The script treats it as
#: structural (re-derived every run, never interesting afterwards).
RSVP_NO_ROSTER_COPY_REASON = "no-roster-copy"


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
    #: UTC epoch start -- with meeting_link this is the D-254 (link, start) identity
    #: the rsvp ledger row carries; the id is only the ADDRESS acted on.
    meeting_start_ts: int = 0
    #: RSVP sub-step (see RSVP_OUTCOMES). rsvp_planned marks a row the execute step
    #: should RSVP on; rsvp_event_id is the id on cora@'s OWN calendar when it differs
    #: from event_id (an externally-organised meeting has a different id per invitee).
    rsvp: str = ""
    rsvp_planned: bool = False
    rsvp_event_id: str = ""
    rsvp_error: str = ""


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


def own_response_status(event: dict[str, Any], identity: str) -> str:
    """responseStatus of `identity`'s own attendee entry on an event from ITS calendar.

    Google flags the calendar owner's entry `self: true`; the email match is the
    belt for an entry that arrives without the flag. "" when the identity is not
    on the guest list at all (e.g. it is the organiser of a link-less hold).
    """
    ident = (identity or "").strip().lower()
    for att in (event.get("attendees") or []):
        if not isinstance(att, dict):
            continue
        addr = (att.get("email") or "").strip().lower()
        if att.get("self") is True or (addr and addr == ident):
            return (att.get("responseStatus") or "").strip()
    return ""


def _plan_rsvp(event: dict[str, Any]) -> tuple[str, bool]:
    """Decide the RSVP sub-step for one event (cora@'s own copy, or -- for a
    guest-add not yet applied -- the roster copy cora@ is about to be added to).

    Returns (rsvp, planned): a terminal skip reason with planned=False, or ("", True)
    when the execute step should accept. Order mirrors the execute-time re-check in
    `_rsvp_accept` so plan mode shows what live mode would do.

    There is NO LEX withhold (ruled 2026-09-19, ask 9.3). A LEX event plans an
    accept exactly like any other; `_rsvp_accept` labels the outcome
    `accepted:lex`. The consent gates that decide whether cora@ may be on a
    meeting at all run BEFORE this -- qualify_event's carve-outs and the
    veto-on-any-copy rule remove the meeting from `candidates`, and the sweep
    remainder skips any invite with no roster copy -- so removing the withhold
    widens nothing a carve-out forbids. `display_title` still redacts every LEX
    row; that rail is independent of this one.
    """
    if LEGACY_NOTETAKER in event_emails(event):
        return "skipped:notetaker-present", False
    return "", True


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

    # RSVP SWEEP SOURCE. Invites sitting on cora@'s own calendar at needsAction: a
    # guest-add whose accept failed last cycle, or a meeting a human invited cora@
    # to by hand. Lane-made copies are excluded (cora@ ORGANISES those -- there is
    # no invite to accept). Keyed like everything else so the roster copy, whose
    # qualification decides whether we may act, can be found.
    needs_action: dict[tuple, dict[str, Any]] = {}
    for ev in capture_events:
        if not extract_meeting_link(ev) or not starts_on_day(ev, day):
            continue
        if CAPTURE_COPY_MARKER in (ev.get("description") or ""):
            continue
        if own_response_status(ev, cfg.capture_identity) != "needsAction":
            continue
        needs_action.setdefault(meeting_key(ev), ev)

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
            meeting_start_ts=event_start_ts(ev),
        ))

    for key, entries in candidates.items():
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
            row = EnsureAction(
                member=member.name, calendar_email=member.calendar_email, event_id=eid,
                title=safe, start_label=event_time_label(ev),
                action="none", reason=covered_reason, meeting_link=link,
                meeting_start_ts=event_start_ts(ev),
            )
            # Covered, but is cora@'s own invite still unanswered? This roster copy
            # is the visible, already-qualified copy that lets us act (a veto on any
            # copy already removed the key from `candidates`). The RSVP hangs off
            # THIS row rather than a second one so the meeting is counted once.
            own = needs_action.pop(key, None)
            if own is not None:
                row.rsvp, row.rsvp_planned = _plan_rsvp(own)
                row.rsvp_event_id = (own.get("id") or "").strip()
                if row.rsvp_planned:
                    row.reason = f"{covered_reason}; capture identity RSVP pending -> accept"
            result.actions.append(row)
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

        # A guest-add is followed by an RSVP-accept as cora@ (R1). A copy is
        # organised BY cora@ and has no invite to answer. The guest-add runs the
        # same planner as the sweep (against the roster copy -- cora@ has no copy
        # yet) so plan mode shows the skip live mode would apply.
        rsvp, rsvp_planned = _plan_rsvp(ev) if action == "guest-add" else ("", False)
        result.actions.append(EnsureAction(
            member=member.name, calendar_email=member.calendar_email, event_id=eid,
            title=safe, start_label=event_time_label(ev),
            action=action, reason=reason, meeting_link=link,
            meeting_start_ts=event_start_ts(ev),
            rsvp=rsvp, rsvp_planned=rsvp_planned,
        ))

    # RSVP SWEEP REMAINDER. Unanswered invites on cora@'s calendar with NO qualifying
    # roster copy. A vetoed meeting is left alone silently -- its skip row above
    # already says why, and accepting would record a meeting somebody opted out of.
    # Anything else has no roster copy through which the veto set can be evaluated,
    # so the safe default is to skip it and say so (never accept blind).
    for key, own in needs_action.items():
        if key in vetoed:
            continue
        result.actions.append(EnsureAction(
            member="capture identity", calendar_email=cfg.capture_identity,
            event_id=(own.get("id") or "").strip(), title=display_title(own),
            start_label=event_time_label(own), action="skip",
            reason=f"{RSVP_NO_ROSTER_COPY_REASON}: capture identity invited but no roster "
                   "copy visible -- veto set cannot be evaluated",
            meeting_link=extract_meeting_link(own).strip().lower(),
            meeting_start_ts=event_start_ts(own),
            rsvp="skipped:no-roster-copy", rsvp_event_id=(own.get("id") or "").strip(),
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
                    # THE R1 STEP: accept the invite AS cora@. Runs whether the
                    # guest-add just happened or was already in place -- gated on
                    # the read-back status inside set_own_response, not on whether
                    # add_attendee changed anything, so a failed accept is retried
                    # next cycle and a completed one is a no-op.
                    act.rsvp = _rsvp_accept(act, cfg, cc, event_id=act.event_id)
                    continue
                except Exception as exc:  # noqa: BLE001
                    log.info("ensure: guest-add failed for %s (%s) -- falling back to copy",
                             act.event_id, str(exc)[:160])
                    act.action = "copy"
                    act.reason = f"guest-add refused ({str(exc)[:80]}) -> copy"
                    act.rsvp_planned = False   # cora@ organises the copy; nothing to accept
                    act.rsvp = ""              # drop any plan-time skip: no invite exists

            src = source_events.get(act.event_id)
            if src is None:
                src = cc.get_event(user_email=act.calendar_email, event_id=act.event_id)
            cc.insert_event_copy(target_email=cfg.capture_identity, source_event=src)
            act.applied = True
        except Exception as exc:  # noqa: BLE001
            act.error = str(exc)[:200]
            log.error("ensure: %s failed for event %s: %s", act.action, act.event_id, exc)

    # RSVP SWEEP: covered meetings whose invite on cora@'s calendar is still
    # unanswered. Same dual gate (we are past `if not writing: return`), same
    # helper, same skip order as the guest-add path. Only 'none' rows carry a
    # planned sweep RSVP -- a guest-add row handled its own above, and a row that
    # errored before cora@ ever reached the guest list must not be accepted on.
    for act in result.actions:
        if act.action == "none" and act.rsvp_planned and not act.rsvp:
            act.rsvp = _rsvp_accept(act, cfg, cc, event_id=act.rsvp_event_id or act.event_id)

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


def _rsvp_accept(act: EnsureAction, cfg: CaptureConfig, cc: Any, *, event_id: str) -> str:
    """Accept cora@'s own invite on one event. Returns an RSVP_OUTCOMES value.

    Called ONLY from execute_ensure past its dual write gate. Re-fetches cora@'s
    copy first so the checks run against the event AS IT IS NOW, not as it was at
    plan time -- design v1 s4a's case is a notetaker@ added AFTER the lane's
    guest-add, which must turn the accept into a skip (one mechanism per event).

    LEX IS INCLUDED (ruled 2026-09-19, ask 9.3; the D-247 capture-yes tension was
    accepted by Harrison). `is_lex_event(own)` is now a LABEL computed AFTER the
    write: a LEX accept reads `accepted:lex` so the 07:22 audit can count it
    apart. Its fail-safe True (a classifier error reads as LEX) can therefore only
    MISLABEL a non-LEX accept as `accepted:lex`; it can never withhold or skip the
    accept, because the write has already happened by the time it is asked.
    Every failure is RECORDED, never raised -- the lane's other actions and its
    ledger must complete.
    """
    try:
        own = cc.get_event(user_email=cfg.capture_identity, event_id=event_id)
        if LEGACY_NOTETAKER in event_emails(own):
            return "skipped:notetaker-present"
        changed, outcome = cc.set_own_response(
            user_email=cfg.capture_identity, event_id=event_id,
        )
        lex = False
        if changed and outcome == "accepted":
            lex = _lex_label(own)
            if lex:
                outcome = "accepted:lex"
        if changed:
            log.info("rsvp_accepted event_id=%s link=%s start_ts=%s lex=%s",
                     event_id, act.meeting_link, act.meeting_start_ts, lex)
        return outcome
    except Exception as exc:  # noqa: BLE001
        act.rsvp_error = str(exc)[:200]
        log.error("ensure: rsvp-accept failed for event %s: %s", event_id, exc)
        return "error"


def _lex_label(event: dict[str, Any]) -> bool:
    """`is_lex_event` as a LABEL for an accept that already happened.

    is_lex_event already fails safe to True; this wrapper only makes sure that a
    future change to it that lets an exception escape can still never turn a
    completed accept into an `error` outcome -- the worst a classifier fault may
    do on this path is label the accept `accepted:lex`.
    """
    try:
        return bool(is_lex_event(event))
    except Exception:  # noqa: BLE001
        return True


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

#: Organiser address suffix of a Google GROUP calendar (a shared team calendar,
#: not a person). A recurring block on such a calendar is a standing placeholder:
#: it lands on every subscriber's calendar whether or not anyone actually joins.
#: On the 2026-09-07 (Labor Day) audit ALL five "missed" meetings were of this
#: shape -- nobody convened them, and reporting them as MISSED accused the capture
#: lane of a gap it never had.
GROUP_CALENDAR_ORGANIZER_SUFFIX = "@group.calendar.google.com"

#: The basis string recorded on a presumed-unconvened meeting. There is no Meet /
#: Zoom audit-log read available today (no admin.reports scope, no Zoom API), so
#: the ONLY evidence is structural: a group-calendar organiser and zero transcripts.
#: That is a PRESUMPTION, not proof (measured live 2026-09-08: one group-calendar
#: block that day WAS convened and captured), which is why the bucket is labelled
#: "presumed" everywhere it surfaces and why the ids stay in the ledger.
UNCONVENED_BASIS_GROUP_CALENDAR = "group-calendar-organizer"

#: MEET JOIN AUDIT (Code #14 R14-8, ruled 2026-09-19 ask 9.6 -- a LOCKED LANE that
#: SHIPS DARK). When the Reports-API read (connectors/meet_audit) is live, the
#: presumption above can be upgraded to evidence. NARROW FIRST RUNG: ONLY meetings
#: that are PRESUMED unconvened today (group-calendar blocks) are ever
#: re-bucketed; a person-organised MISS is never touched in v1.
#: Counted in PEOPLE, not endpoints (D-051 lex-phi-identity-2: one person on a
#: laptop + phone, or a drop-and-rejoin, is two endpoints -- counting endpoints made
#: a solo block a false MISS). A person = the read's per-read hash of an EMAIL
#: identity (connectors/meet_audit MeetJoin.person_key); an endpoint without one is
#: "cannot tell who".
#:   0 human endpoints                 -> confirmed unconvened (..._MEET_NO_JOIN)
#:   1 person (any endpoints), or ONE
#:     unidentified endpoint           -> confirmed unconvened, solo (..._MEET_SOLO)
#:  >=2 distinct people                -> a MISS (CONVENED_BASIS_MEET): people met,
#:                                        nothing captured it -- stricter, never looser.
#:   anything else (an unidentified
#:     endpoint beside another)        -> CANNOT TELL: the lane decides nothing for
#:                                        that meeting; the presumption stands.
UNCONVENED_BASIS_MEET_NO_JOIN = "meet-audit:no-join"
UNCONVENED_BASIS_MEET_SOLO = "meet-audit:solo-join"
CONVENED_BASIS_MEET = "meet-audit:joined"
MEET_BASIS_CANNOT_TELL = "meet-audit:cannot-tell"
MEET_BUCKET_UNDECIDED = "undecided"

#: The read may DECIDE only after the audit log has had time to land. The 07:22
#: fire audits a day that ended ~7h earlier; a manual earlier run reads "lag".
MEET_AUDIT_LAG_FLOOR_H = 6
#: Read window around the audited AZ day (a meeting can end after midnight and its
#: call_ended event lands after it ends).
_MEET_READ_BEFORE_H = 1
_MEET_READ_AFTER_H = 6
#: A join matches a meeting when its [start, end] overlaps [event start - 30m,
#: event end + 60m]. Recurring series REUSE one meeting code, so the code alone is
#: never an identity -- (code, time window) is (feedback: a meeting is not a
#: calendar event).
_MEET_MATCH_BEFORE_S = 30 * 60
_MEET_MATCH_AFTER_S = 60 * 60
#: States in which the read decided nothing and the report says so in one line.
MEET_AUDIT_WARN_STATES = ("error", "partial", "contradiction", "lag")
_INSTANCE_SUFFIX = re.compile(r"_\d{8}T\d{6}Z$")


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
    #: Why this meeting was bucketed as presumed unconvened rather than missed
    #: (see UNCONVENED_BASIS_GROUP_CALENDAR). Empty on every other meeting. Every
    #: re-bucketing carries its reason, exactly as every qualify_event skip does.
    unconvened_basis: str = ""
    #: Set only on a MISS the Meet join log proved convened (CONVENED_BASIS_MEET):
    #: a presumed-unconvened block that >=2 people actually joined.
    convened_basis: str = ""


@dataclass
class AuditReport:
    day: str
    scheduled: int = 0
    captured: int = 0
    misses: list[AuditedMeeting] = field(default_factory=list)
    #: Scheduled, zero transcripts, but PRESUMED never convened (a group-calendar
    #: placeholder block with no join evidence). Kept apart from `misses` so a
    #: holiday of standing team blocks does not read as five capture failures --
    #: and kept OUT of the clean-day criterion's veto, because nothing was missed.
    unconvened: list[AuditedMeeting] = field(default_factory=list)
    duplicates: list[AuditedMeeting] = field(default_factory=list)
    unmatched_transcripts: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    #: A meeting a NO-RECORD carve-out (NO_RECORD_REASON_PREFIXES: a title marker, a
    #: no-record title / address / domain) removed from scope that WAS captured
    #: anyway. The most serious thing this auditor can find: a recording exists of a
    #: meeting somebody ruled must not be recorded. Suppress the TITLE, never the
    #: FACT. A carved meeting skipped for a QUALIFICATION reason (declined, cancelled,
    #: no link, ...) that was recorded is `carved_recordings`, never a breach (S4).
    carve_out_breaches: list[tuple[str, str]] = field(default_factory=list)   # (shape, reason)
    #: Positionally aligned with carve_out_breaches (appended only by
    #: _record_carved_hit). The no-record copy's event id -- or, for a breach the
    #: transcript-title belt found, the transcript's own cal_id ("" when it has
    #: none) -- and the Fireflies transcript id, which is what finding and deleting
    #: the recording needs. Ids only, never titles (D-082).
    carve_out_breach_event_ids: list[str] = field(default_factory=list)
    carve_out_breach_transcript_ids: list[str] = field(default_factory=list)
    #: A carved meeting skipped for a QUALIFICATION reason that was recorded anyway:
    #: information, not an alarm. ONE row per carved MEETING (every transcript joined
    #: to it collapses into that row): (shape, reason) plus the carved
    #: representative's event id, aligned the same way.
    carved_recordings: list[tuple[str, str]] = field(default_factory=list)
    carved_recording_event_ids: list[str] = field(default_factory=list)
    #: Aligned with carved_recordings: the Fireflies transcript id of EVERY recording
    #: joined to that carved meeting (ids only, D-082). More than one is a DUPLICATE
    #: capture, and vetoes the clean-day line like any other duplicate (D-051 s4#1:
    #: two recordings of a vetoed meeting otherwise read as a clean day).
    carved_recording_transcript_ids: list[list[str]] = field(default_factory=list)
    failed_calendars: list[tuple[str, str]] = field(default_factory=list)
    transcript_error: str = ""
    seat_note: str = ""
    #: cora@'s own RSVP accepts for the day (rsvp_counts), set by the audit SCRIPT
    #: after audit_day returns -- audit_day itself never reads the ensure ledger.
    #: None = not attached, and render_report then prints nothing for it.
    rsvp: dict[str, Any] | None = None
    #: Meet join audit lane (R14-8): the read's state after the eligibility checks
    #: (live | dark:* | error | partial | contradiction | lag), a fixed reason
    #: class, and how many call_ended events it read. "" = never consulted.
    meet_audit_state: str = ""
    meet_audit_reason: str = ""
    meet_events_read: int = 0
    #: presumed-unconvened meetings the live lane saw joins for but could not count
    #: in PEOPLE (an endpoint with no email identity beside another): the presumption
    #: stands, and the event id is kept so "cannot tell" is visible, never silent.
    meet_undecided_ids: list[str] = field(default_factory=list)

    @property
    def unconvened_confirmed(self) -> list[AuditedMeeting]:
        return [m for m in self.unconvened if m.unconvened_basis.startswith("meet-audit:")]

    @property
    def unconvened_presumed(self) -> list[AuditedMeeting]:
        return [m for m in self.unconvened if not m.unconvened_basis.startswith("meet-audit:")]

    @property
    def convened_misses(self) -> list[AuditedMeeting]:
        return [m for m in self.misses if m.convened_basis]

    @property
    def carved_recording_counts(self) -> list[int]:
        """Transcripts per carved_recordings row, aligned with it (1 for a row the
        aligned id list does not cover -- a report assembled by hand)."""
        ids = self.carved_recording_transcript_ids
        return [len(ids[i]) if i < len(ids) and ids[i] else 1
                for i in range(len(self.carved_recordings))]

    @property
    def carved_duplicated(self) -> int:
        """Carved meetings recorded MORE THAN ONCE -- a duplicate capture."""
        return sum(1 for n in self.carved_recording_counts if n > 1)


#: Marks an index key claimed by more than one meeting. Such a key can never
#: identify anything, so it must not resolve to the first claimant.
_AMBIGUOUS = ("__ambiguous__",)


def _norm_title(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _transcript_ts(t: dict[str, Any]) -> int:
    from cora.connectors.fireflies_connector import _parse_date

    return _parse_date(t.get("date")) or 0


def _transcript_time_label(t: dict[str, Any]) -> str:
    """HH:MM (AZ) of a transcript, "?" when it carries no usable date."""
    ts = _transcript_ts(t)
    return datetime.fromtimestamp(ts, _AZ).strftime("%H:%M") if ts else "?"


def roster_attendee_accepted(
    copies: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
    roster_emails: frozenset[str] | set[str],
) -> bool:
    """Did any ROSTER human RSVP `accepted` on any copy of this meeting?

    The second structural signal behind the unconvened presumption. A roster
    member's own attendee row is either flagged `self` (the copy came off their
    calendar) or carries their roster address; `accepted` on it means a person
    said they would be in the room, which is join evidence the organiser address
    alone cannot carry. A bot or external accepting counts for nothing here.
    """
    wanted = {str(e or "").strip().lower() for e in (roster_emails or ()) if e}
    for ev in (copies or ()):
        if not isinstance(ev, dict):
            continue
        for att in (ev.get("attendees") or []):
            if not isinstance(att, dict):
                continue
            addr = (att.get("email") or "").strip().lower()
            is_roster = bool(att.get("self")) or (addr in wanted)
            if is_roster and (att.get("responseStatus") or "").strip().lower() == "accepted":
                return True
    return False


def presumed_unconvened_basis(
    event: dict[str, Any],
    *,
    copies: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
    roster_emails: frozenset[str] | set[str] = frozenset(),
) -> str:
    """Why a scheduled-but-transcript-less meeting is PRESUMED unconvened, or "".

    Deterministic and network-free. TWO structural signals must agree before a
    zero-transcript meeting is re-bucketed out of the misses:

      1. the organiser is a Google GROUP calendar (a standing placeholder, not a
         meeting a person called) -- the organiser address is the one structural
         fact the event itself carries; AND
      2. NO roster human RSVP'd `accepted` on any copy of it (`copies`, every
         roster calendar's instance of the same meeting; `roster_emails`, the
         active roster). A group-calendar block that people accepted and the bot
         then failed to join is a genuine capture failure and must stay a MISS
         above the alarms, not sink below them as "presumed unconvened" while
         the clean-day line prints (D-051 review, Code #13 C-2).

    This is only ever consulted for a meeting with ZERO transcripts -- a
    group-calendar block that produced a transcript was convened by definition
    and is captured, never unconvened. Returns the basis string so the report can
    say WHY. Called with the event alone (no copies, no roster) rule 2 sees only
    that copy and only its `self`-flagged attendee rows, so the one-argument
    unit pins keep their pre-C-2 answers.
    """
    organizer = event.get("organizer") if isinstance(event.get("organizer"), dict) else {}
    addr = ((organizer or {}).get("email") or "").strip().lower()
    if not addr.endswith(GROUP_CALENDAR_ORGANIZER_SUFFIX):
        return ""
    if roster_attendee_accepted(copies if copies is not None else [event], roster_emails):
        return ""
    return UNCONVENED_BASIS_GROUP_CALENDAR


# ── Meet join audit consumer (R14-8) ─────────────────────────────────────────

def _event_end_ts(event: dict[str, Any]) -> int:
    end = (event.get("end") or {}).get("dateTime")
    if not end:
        return event_start_ts(event)
    try:
        dt = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except ValueError:
        return event_start_ts(event)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _meet_code(event: dict[str, Any]) -> str:
    """The normalized Google Meet code of an event, or "" for a Zoom / Teams /
    link-less meeting (those are never evaluated by the Meet join log)."""
    from cora.tools.calendar_client import extract_meeting_link

    link = (extract_meeting_link(event) or "").strip().lower()
    if "meet.google.com/" not in link:
        return ""
    from cora.connectors.meet_audit import normalize_meeting_code

    return normalize_meeting_code(link)


def _base_event_id(eid: str) -> str:
    return _INSTANCE_SUFFIX.sub("", (eid or "").strip())


def meet_joins_for(
    event: dict[str, Any],
    event_ids: list[str] | tuple[str, ...],
    joins: Any,
) -> list[Any]:
    """The MeetJoins that belong to ONE meeting: same Meet code (or the same
    calendar event id, instance suffix stripped) AND a join interval overlapping
    [event start - 30m, event end + 60m]. Keyed on (code, time window) -- a
    recurring series reuses one code, so yesterday's join never counts today."""
    code = _meet_code(event)
    if not code:
        return []
    start = event_start_ts(event)
    end = _event_end_ts(event)
    if not start:
        return []
    lo, hi = start - _MEET_MATCH_BEFORE_S, max(end, start) + _MEET_MATCH_AFTER_S
    bases = {_base_event_id(e) for e in (event_ids or ()) if e}
    out = []
    for j in (joins or ()):
        same = (j.meeting_code and j.meeting_code == code) or (
            j.calendar_event_id and _base_event_id(j.calendar_event_id) in bases)
        if not same:
            continue
        j_lo = j.start_ts or j.end_ts
        j_hi = j.end_ts or j.start_ts
        if not j_lo or j_hi < lo or j_lo > hi:
            continue
        out.append(j)
    return out


def classify_convened(joins: Any) -> tuple[str, str]:
    """(bucket, basis) from one meeting's matched joins, counting distinct HUMAN
    PEOPLE (a bot alone in the room is not a meeting; one person's laptop + phone,
    or a rejoin, is one person -- D-051 lex-phi-identity-2):
      >=2 people                      -> ("convened", CONVENED_BASIS_MEET)
      1 person, or 1 unidentified
        endpoint alone                -> ("unconvened", UNCONVENED_BASIS_MEET_SOLO)
      0 human endpoints               -> ("unconvened", UNCONVENED_BASIS_MEET_NO_JOIN)
      otherwise                       -> (MEET_BUCKET_UNDECIDED, MEET_BASIS_CANNOT_TELL)
    FAIL CLOSED: an endpoint with no email identity (anonymous, dial-in) may be the
    same person as another endpoint or a second one, so it never counts as a second
    person and never lets one identity read as solo -- the lane decides nothing."""
    humans = [j for j in (joins or ()) if getattr(j, "is_human", False)
              and getattr(j, "endpoint_key", "")]
    if not humans:
        return "unconvened", UNCONVENED_BASIS_MEET_NO_JOIN
    people = {j.person_key for j in humans if getattr(j, "person_key", "")}
    anonymous = {j.endpoint_key for j in humans if not getattr(j, "person_key", "")}
    if len(people) >= 2:
        return "convened", CONVENED_BASIS_MEET
    if (len(people) == 1 and not anonymous) or (not people and len(anonymous) == 1):
        return "unconvened", UNCONVENED_BASIS_MEET_SOLO
    return MEET_BUCKET_UNDECIDED, MEET_BASIS_CANNOT_TELL


def _creator_email(event: dict[str, Any]) -> str:
    creator = event.get("creator") if isinstance(event.get("creator"), dict) else {}
    return ((creator or {}).get("email") or "").strip().lower()


def _creator_in_domain(event: dict[str, Any]) -> bool:
    return _is_dwd_domain(_creator_email(event))


def _creator_external(event: dict[str, Any]) -> bool:
    """True only when the event NAMES a creator outside the Workspace domains. A
    missing creator is not "external" (the contradiction check still applies to it:
    fail closed)."""
    email = _creator_email(event)
    return bool(email) and not _is_dwd_domain(email)


def _apply_meet_audit(
    report: AuditReport,
    day: str,
    meetings: dict[tuple, AuditedMeeting],
    raw_events: dict[tuple, dict[str, Any]],
    by_meeting: dict[tuple, list[dict[str, Any]]],
    *,
    fetch_meet_joins: Callable[[datetime, datetime], Any] | None,
    clock: Callable[[], datetime] | None,
) -> None:
    """Upgrade PRESUMED-unconvened meetings to evidence, or change nothing.

    FAIL CLOSED. The read decides only when ALL hold: the lane is `live` (a
    complete, un-capped read -- never dark / error / partial); the lag floor has
    elapsed; and the CONTRADICTION CHECK passed -- every CAPTURED Meet meeting on
    the day that the org's log can record has >=1 matched join (a captured meeting
    with no join means the log is missing events, so its silence proves nothing).
    Otherwise every bucket is exactly what the structural logic decided and only
    report.meet_audit_state records why (a dark state renders byte-identically to
    the pre-R14-8 report).

    "Can record" (D-051 lex-phi-identity-4): the org's Meet audit log may not record
    a meeting HOSTED OUTSIDE the Workspace domains -- the premise the zero-join
    branch below already rests on. So a captured Meet whose event names an external
    creator is neither a contradiction (its missing joins say nothing about this
    log) nor a control (its joins, if any, prove nothing about in-domain coverage).
    A captured Meet with NO creator on the event is still checked (fail closed) but
    is not a control; only an in-domain-created captured Meet with a join is.

    When it decides, it touches ONLY report.unconvened (the narrow first rung):
    >=2 distinct PEOPLE moves the meeting to report.misses (stricter); one person
    confirms it unconvened (solo); endpoints it cannot count in people leave the
    presumption standing (report.meet_undecided_ids); zero confirms it unconvened
    ONLY when the evidence can reach it -- a same-day captured Meet meeting proved
    the log present (a control) AND the event's creator is in the Workspace domains
    (whose Meet activity the org's audit log records). Without those the zero
    stays a presumption. A person-organised MISS is never re-bucketed in v1.
    """
    if fetch_meet_joins is None:
        def fetch_meet_joins(start: datetime, end: datetime) -> Any:
            from cora.connectors.meet_audit import read_call_ended

            return read_call_ended(start, end)

    day_start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=_AZ)
    day_end = day_start + timedelta(days=1)
    start_utc = (day_start - timedelta(hours=_MEET_READ_BEFORE_H)).astimezone(timezone.utc)
    end_utc = (day_end + timedelta(hours=_MEET_READ_AFTER_H)).astimezone(timezone.utc)
    try:
        read = fetch_meet_joins(start_utc, end_utc)
        state = str(getattr(read, "state", "") or "error")
        report.meet_audit_reason = str(getattr(read, "reason", "") or "")
        report.meet_events_read = int(getattr(read, "events_read", 0) or 0)
        joins = tuple(getattr(read, "joins", ()) or ())
    except Exception as exc:  # noqa: BLE001 -- a broken read never breaks the audit
        report.meet_audit_state = "error"
        report.meet_audit_reason = type(exc).__name__
        log.warning("audit: Meet join read failed (%s) -- presumptions stand", type(exc).__name__)
        return
    report.meet_audit_state = state
    if state != "live":
        return

    now = clock() if clock is not None else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if now < day_end + timedelta(hours=MEET_AUDIT_LAG_FLOOR_H):
        report.meet_audit_state = "lag"
        report.meet_audit_reason = f"lag floor {MEET_AUDIT_LAG_FLOOR_H}h not elapsed"
        return

    # CONTRADICTION CHECK: the failing-capable cross-check. Every captured Meet
    # meeting the org's log can record must show >=1 join; one that does not means
    # the log is incomplete. An externally-created one is outside that log's reach
    # (lex-phi-identity-4) -- skipped, and never a control.
    controls = 0
    for key, meeting in meetings.items():
        if not by_meeting.get(key):
            continue
        ev = raw_events.get(key) or {}
        if not _meet_code(ev) or _creator_external(ev):
            continue
        if not meet_joins_for(ev, meeting.event_ids, joins):
            report.meet_audit_state = "contradiction"
            report.meet_audit_reason = "a captured Meet meeting has no join event"
            return
        if _creator_in_domain(ev):
            controls += 1

    keep: list[AuditedMeeting] = []
    for meeting in report.unconvened:
        key = next((k for k, m in meetings.items() if m is meeting), None)
        ev = raw_events.get(key) if key is not None else None
        if not ev or not _meet_code(ev):
            keep.append(meeting)          # Zoom / Teams / link-less: not evaluable
            continue
        bucket, basis = classify_convened(meet_joins_for(ev, meeting.event_ids, joins))
        if bucket == MEET_BUCKET_UNDECIDED:
            # cannot count the joins in people: decide nothing for this meeting
            report.meet_undecided_ids.append(meeting.event_id)
            keep.append(meeting)
            continue
        if bucket == "convened":
            meeting.convened_basis = basis
            meeting.unconvened_basis = ""
            report.misses.append(meeting)
            continue
        if basis == UNCONVENED_BASIS_MEET_NO_JOIN and not (controls and _creator_in_domain(ev)):
            keep.append(meeting)          # silence the evidence cannot reach
            continue
        meeting.unconvened_basis = basis
        keep.append(meeting)
    report.unconvened = keep


def classify_carved_recording(
    veto: tuple[dict[str, Any], str],
    no_record: tuple[dict[str, Any], str] | None,
    transcript: dict[str, Any],
    cfg: CaptureConfig,
) -> tuple[bool, dict[str, Any], str]:
    """Is a transcript joined to a CARVED meeting a breach? -> (is_breach, event, reason).

    The ONE classifier for both breach joins in audit_day (the early exact-cal_id
    join and 3b), so the two can never disagree (Code #15 S4, cq-5f44ce934aeb):

      * `no_record` -- the first copy, over EVERY copy of the meeting, whose
        no_record_reason is set. Never the stored veto reason alone: that is the
        first non-qualifying copy's first failing check, and a decline or a missing
        link checked first would hide a `[no-bot]` on the same or a later copy.
      * the transcript's OWN title -- a Fireflies-side retitle to a no-record title
        is a recording of exactly what the carve-out keeps out. This path consumes
        the transcript, so section 4's title belt would never see it.
      * a veto reason that is neither a known qualification skip nor a no-record
        reason is UNKNOWN (a reason added to qualify_event and never classified):
        FAIL CLOSED to a breach.

    Only a known qualification skip (declined, cancelled, no link, all-day, not a
    meeting) with none of the above is information, not an alarm.
    """
    if no_record is not None:
        ev, why = no_record
        return True, ev, why
    ev, why = veto
    t_why = title_carve_out_reason(transcript.get("title") or "", cfg)
    if t_why:
        return True, ev, t_why
    if not is_qualification_skip_reason(why):
        return True, ev, why
    return False, ev, why


def _record_carved_hit(
    report: AuditReport,
    *,
    is_breach: bool,
    shape: str,
    reason: str,
    event_id: str,
    transcript_id: str,
    repeat_of: int | None = None,
) -> int:
    """The ONLY writer of the carved-hit lists, so their alignment cannot drift.
    Returns the row written.

    `repeat_of` (informational hits only) is the row already recorded for the SAME
    carved meeting: the transcript id joins that row instead of adding a second
    identical line, so a vetoed meeting recorded twice reads as ONE meeting captured
    twice -- a duplicate -- never as two separate pieces of information (D-051 s4#1).
    A breach is never collapsed: each breaching recording is its own row, with the
    transcript id deleting it needs."""
    if is_breach:
        report.carve_out_breaches.append((shape, reason))
        report.carve_out_breach_event_ids.append(event_id)
        report.carve_out_breach_transcript_ids.append(transcript_id)
        return len(report.carve_out_breaches) - 1
    if repeat_of is not None:
        report.carved_recording_transcript_ids[repeat_of].append(transcript_id)
        return repeat_of
    report.carved_recordings.append((shape, reason))
    report.carved_recording_event_ids.append(event_id)
    report.carved_recording_transcript_ids.append([transcript_id])
    return len(report.carved_recordings) - 1


def audit_day(
    day: str,
    cfg: CaptureConfig,
    *,
    list_events: Callable[[str, str], list[dict[str, Any]]] | None = None,
    fetch_transcripts: Callable[[str, str], list[dict[str, Any]]] | None = None,
    fetch_seats: Callable[[], list[dict[str, Any]]] | None = None,
    fetch_meet_joins: Callable[[datetime, datetime], Any] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> AuditReport:
    """Diff one day's scheduled roster meetings against Fireflies transcripts.

    Every external read is injectable so the whole diff is testable without a
    network. Each read also degrades independently: a calendar that 403s is named
    in the report rather than silently shrinking the denominator, because "0 misses"
    computed from a half-read roster is the single most dangerous output this thing
    could produce.

    `fetch_meet_joins(start_utc, end_utc) -> meet_audit.MeetAuditRead` is the Meet
    join audit read (R14-8; default connectors.meet_audit.read_call_ended, DARK
    until the Reports scope is granted); `clock` is injectable for the lag floor.
    See _apply_meet_audit for when that read may decide anything.
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
    #: EVERY qualifying copy of each meeting (one per roster calendar it landed
    #: on), so the unconvened presumption can read an `accepted` RSVP off any
    #: copy, not just the representative one (C-2).
    raw_copies: dict[tuple, list[dict[str, Any]]] = {}
    roster_emails = frozenset(
        (m.calendar_email or "").strip().lower() for m in cfg.active_members if m.calendar_email
    )
    #: meetings a veto on any copy removed from scope -- a qualification skip OR a
    #: no-record carve-out -- kept so we can still notice if one of them was
    #: recorded anyway. (first vetoing copy, its reason)
    carved: dict[tuple, tuple[dict[str, Any], str]] = {}
    #: EVERY copy of each carved meeting (one event id per invitee calendar), so the
    #: breach join in 3b matches a transcript whose cal_id names ANY copy -- the main
    #: join already does this (key_by_event_id); joining only the first vetoing copy
    #: let a recorded carve-out fall through to section 4 and print its title
    #: (Code #14 r4, D-051 copa-audit-fallthrough-leak).
    carved_copies: dict[tuple, list[dict[str, Any]]] = {}
    #: the first copy of each carved meeting (over EVERY copy, qualifying or not)
    #: that carries a NO-RECORD carve-out, with that reason. `carved` keeps the FIRST
    #: veto reason, which a decline / cancel / missing link checked earlier -- on the
    #: same copy or an earlier one -- can hide; the breach decision reads this
    #: instead (Code #15 S4, cq-5f44ce934aeb). `skipped` is unchanged.
    carved_no_record: dict[tuple, tuple[dict[str, Any], str]] = {}

    for key, entries in grouped.items():
        veto: tuple[dict[str, Any], str] | None = None
        no_record_hit: tuple[dict[str, Any], str] | None = None
        qualifying: list[tuple[RosterMember, dict[str, Any]]] = []
        for member, ev in entries:
            q = qualify_event(ev, cfg, roster_email=member.calendar_email)
            if q.qualifies:
                qualifying.append((member, ev))
            elif veto is None:
                veto = (ev, q.reason)
            if no_record_hit is None:
                nr = no_record_reason(ev, cfg)
                if nr:
                    no_record_hit = (ev, nr)

        if veto is not None:
            ev, reason = veto
            carved[key] = (ev, reason)
            carved_copies[key] = [e for _m, e in entries]
            if no_record_hit is not None:
                carved_no_record[key] = no_record_hit
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
        raw_copies[key] = [e for _m, e in qualifying]

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

    #: carved meeting key -> its row in report.carved_recordings (informational
    #: hits only), so every recording of ONE vetoed meeting lands on one row.
    carved_info_rows: dict[tuple, int] = {}

    def _carved_hit(c_keys: list[tuple], t: dict[str, Any]) -> None:
        """A transcript joined to a carved meeting, at EITHER breach site: one
        classifier (classify_carved_recording), one writer (_record_carved_hit). The
        caller has already added it to `used` -- an informational hit must stay
        consumed too, or section 4 would print its title.

        `c_keys` is every carved meeting the join could mean -- one for an exact
        cal_id, possibly several for a static room link shared by carved meetings at
        different times (3b's link join is time-blind; the time-aware join is
        cq-4df9da0cb483). AMBIGUITY FAILS CLOSED: if ANY claimant would make it a
        breach, it is one. Binding the first claimant alone would let a declined
        10:00 meeting on the room absorb the recording of a `[no-bot]` 15:00 one as
        mere information -- a false negative the pre-S4 any-hit-is-a-breach rule
        could not produce.

        An INFORMATIONAL hit (no claimant breaches) names ONE carved meeting -- the
        claimant whose start is NEAREST the recording's time (first-seen on a tie) --
        and that one meeting is both its shape label and its collapse group: a second
        recording of the meeting joins its row as another transcript id -- a
        duplicate -- instead of printing a second identical line beside a clean-day
        verdict (D-051 s4#1). Binding the room's FIRST-SEEN claimant instead filed
        every link-only recording under that one meeting, whatever its time: a 10:00
        meeting recorded by cal_id and by link split over two rows and read clean,
        while a 09:00 and a 10:00 recorded once each collapsed into a phantom 09:00
        duplicate (D-051 r2 s3s4#r2-0). Breach precedence is unchanged: any
        breaching claimant wins, and a breach is never grouped. The pick only chooses
        AMONG carved claimants; which transcripts 3b claims at all is still the
        time-blind link join (cq-4df9da0cb483)."""
        results = [
            classify_carved_recording(carved[k], carved_no_record.get(k), t, cfg)
            for k in c_keys
        ]
        breach = next((res for res in results if res[0]), None)
        if breach is not None:
            is_breach, h_ev, h_reason = breach
            group = None
        else:
            t_ts = _transcript_ts(t)
            i = min(range(len(c_keys)),
                    key=lambda j: abs(event_start_ts(carved[c_keys[j]][0]) - t_ts))
            is_breach, h_ev, h_reason = results[i]
            group = c_keys[i]
        row = _record_carved_hit(
            report, is_breach=is_breach,
            shape=f"a meeting at {event_time_label(h_ev)}", reason=h_reason,
            event_id=(h_ev.get("id") or "").strip(), transcript_id=t.get("id") or "",
            repeat_of=carved_info_rows.get(group) if group is not None else None,
        )
        if group is not None:
            carved_info_rows.setdefault(group, row)

    # A cal_id naming ANY copy of a CARVED meeting is exact evidence too: the join
    # to that carved meeting is decided here -- BEFORE the link/title fallbacks.
    # Deciding it after them (as 3b used to) let a recorded carved call on a static
    # personal-room link be bound by link to ANOTHER meeting on that link: the breach
    # went silent and a real miss could read as a clean day (Code #14 r4 re-review,
    # bc61-F2). Only transcripts whose cal_id names a carved event are affected; every
    # other join is unchanged. Whether the hit is a BREACH (a no-record carve-out) or
    # information (a qualification skip) is _carved_hit's call, not this join's.
    carved_id_keys: dict[str, tuple] = {}
    for c_key, c_copies in carved_copies.items():
        for c_ev in c_copies:
            c_eid = (c_ev.get("id") or "").strip()
            if c_eid:
                carved_id_keys.setdefault(c_eid, c_key)
    for t in same_day:
        if (t.get("id") or "") in used:
            continue
        hit = carved_id_keys.get((t.get("cal_id") or "").strip())
        if hit is not None:
            used.add(t.get("id") or "")
            _carved_hit([hit], t)

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
            # The ONLY place a miss is decided. Zero transcripts is either a
            # capture failure (MISSED) or a placeholder nobody joined (presumed
            # UNCONVENED); the split lives here so no other path can re-bucket a
            # meeting. A group-calendar meeting WITH a transcript never reaches
            # this branch, so it can never be called unconvened -- and one a
            # roster human RSVP'd `accepted` on (any copy) stays a MISS (C-2).
            basis = presumed_unconvened_basis(
                raw_events[key],
                copies=raw_copies.get(key) or [raw_events[key]],
                roster_emails=roster_emails,
            )
            if basis:
                meeting.unconvened_basis = basis
                report.unconvened.append(meeting)
            else:
                report.misses.append(meeting)
        else:
            report.captured += 1
            if len(hits) > 1:
                report.duplicates.append(meeting)

    # ── 3a. Meet join audit (R14-8): may upgrade a PRESUMPTION to evidence ──
    _apply_meet_audit(
        report, day, meetings, raw_events, by_meeting,
        fetch_meet_joins=fetch_meet_joins, clock=clock,
    )

    # ── 3b. was anything captured that the veto excluded? ──
    # Dropping carved meetings from the diff entirely would hide this: a bot
    # recording a no-record meeting is exactly what the carve-out exists to
    # prevent, so it is surfaced -- as a shape, never as a title. Only a NO-RECORD
    # carve-out makes the recording a BREACH; a meeting vetoed for a qualification
    # reason (a roster invitee declined, a copy was cancelled or carried no link)
    # that was recorded anyway is reported as information (Code #15 S4,
    # cq-5f44ce934aeb). The same classifier decides at the early cal_id join above.
    # The VETO itself -- a decline on any copy keeps the meeting out of `scheduled`,
    # exactly as it keeps the ensure lane off it -- is unchanged.
    carved_event_ids: dict[str, tuple] = {}
    #: link -> EVERY carved meeting on it, first-seen first (a static room link can
    #: carry several carved meetings in a day; see _carved_hit's fail-closed rule).
    carved_links: dict[str, list[tuple]] = {}
    for c_key, c_copies in carved_copies.items():
        for c_ev in c_copies:
            c_eid = (c_ev.get("id") or "").strip()
            if c_eid:
                carved_event_ids.setdefault(c_eid, c_key)
            c_link = extract_meeting_link(c_ev).strip().lower()
            if c_link:
                claim = carved_links.setdefault(c_link, [])
                if c_key not in claim:
                    claim.append(c_key)
    for t in same_day:
        if (t.get("id") or "") in used:
            continue
        hit = carved_event_ids.get((t.get("cal_id") or "").strip())
        claimants: list[tuple] = [hit] if hit is not None else []
        if not claimants:
            t_link = (t.get("meeting_link") or "").strip().lower()
            claimants = list(carved_links.get(t_link) or []) if t_link else []
        if not claimants:
            continue
        used.add(t.get("id") or "")
        _carved_hit(claimants, t)

    # ── 4. captured but not on any roster calendar ──
    for t in same_day:
        if (t.get("id") or "") in used:
            continue
        # Belt (Code #14 r4, D-051 copa-audit-fallthrough-leak): a transcript whose OWN
        # title carries a title carve-out is a recording of exactly what the carve-out
        # exists to keep out -- whether or not the joins above could tie it to a
        # calendar copy (about half of transcripts carry no cal_id; links drift). It is
        # a breach, rendered as a SHAPE; its title and organiser are never printed.
        t_reason = title_carve_out_reason(t.get("title") or "", cfg)
        if t_reason:
            used.add(t.get("id") or "")
            # No calendar event: the ids are the transcript's own (its cal_id when
            # it carries one, "" otherwise) -- enough to find and delete it.
            _record_carved_hit(
                report, is_breach=True,
                shape=f"a meeting at {_transcript_time_label(t)}", reason=t_reason,
                event_id=(t.get("cal_id") or "").strip(), transcript_id=t.get("id") or "",
            )
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
    when = _transcript_time_label(t)
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


def _rsvp_line(counts: dict[str, Any] | None) -> str:
    """One aggregate line for cora@'s own RSVP accepts, or "" when not attached."""
    if not isinstance(counts, dict) or not counts.get("available"):
        return ""
    try:
        plain = int(counts.get("accepted") or 0)
        lex = int(counts.get("accepted_lex") or 0)
        errors = int(counts.get("errors") or 0)
    except (TypeError, ValueError):
        return ""
    return (f"_RSVP as cora@: {plain + lex} accepted ({lex} LEX), "
            f"{errors} unresolved error(s)_")


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

    # R14-8: `confirmed` is non-empty only when the Meet join log decided (live
    # lane); with the lane dark every line below is byte-identical to before.
    confirmed = report.unconvened_confirmed
    presumed = report.unconvened_presumed
    lines.append(
        f"{report.scheduled} scheduled, {report.captured} captured, "
        f"{len(report.misses)} missed, "
        + (f"{len(confirmed)} not convened (Meet join log), " if confirmed else "")
        + f"{len(presumed)} presumed unconvened, "
        f"{len(report.duplicates)} duplicated"
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
            joined = "  _[people joined per the Meet join log]_" if m.convened_basis else ""
            lines.append(f"  - {m.start_label}  {_esc(m.title)}  _(organizer {_esc(m.organizer)})_{joined}")
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

    if report.carved_recordings:
        # INFORMATIONAL (Code #15 S4): a meeting vetoed for a QUALIFICATION reason
        # -- a roster invitee declined, a copy was cancelled or had no link -- that
        # was recorded anyway. Not a breach (nobody ruled it unrecordable) and not
        # part of the clean-day verdict -- unless recorded more than once (a
        # duplicate, D-051 s4#1); beside the skip line it explains. SHAPE + reason
        # only, never the title or organiser, exactly as the breach block.
        lines.append(
            f"\n*:information_source: Recorded though skipped "
            f"({len(report.carved_recordings)})* -- a qualification skip, not a "
            "no-record carve-out"
        )
        # One line per carved MEETING; a meeting recorded more than once carries its
        # transcript count exactly as the duplicates block does (D-051 s4#1). A
        # once-recorded meeting's line is byte-identical to before.
        counts = report.carved_recording_counts
        for (shape, reason), n in list(zip(report.carved_recordings, counts))[:10]:
            dup = f"  ({n} transcripts)" if n > 1 else ""
            lines.append(f"  - {_esc(shape)}  _({_esc(reason)})_{dup}")
        if len(report.carved_recordings) > 10:
            lines.append(f"  _...and {len(report.carved_recordings) - 10} more_")

    if confirmed:
        # R14-8, live lane only: the Meet join log PROVED these group-calendar
        # blocks unconvened (no human joined, or only one person did). Same LEX
        # rail as every other line (title + organiser already display-safe).
        lines.append(
            f"\n*:white_circle: Not convened (Meet join log) ({len(confirmed)})* "
            "-- group-calendar blocks nobody met in"
        )
        for m in sorted(confirmed, key=lambda x: x.start_label)[:15]:
            solo = "  _[one person joined]_" if m.unconvened_basis == UNCONVENED_BASIS_MEET_SOLO else ""
            lines.append(f"  - {m.start_label}  {_esc(m.title)}  _(organizer {_esc(m.organizer)})_{solo}")
        if len(confirmed) > 15:
            lines.append(f"  _...and {len(confirmed) - 15} more_")

    if presumed:
        # Informational, below every alarm. These are standing group-calendar
        # blocks nobody joined -- a presumption (no Meet/Zoom audit-log read exists
        # today), so the heading says so. Title + organiser go through the same
        # LEX rail as a miss: display_title already redacted, and the organiser
        # was set to "withheld" alongside it.
        lines.append(
            f"\n*:white_circle: Presumed unconvened ({len(presumed)})* "
            "-- group-calendar blocks, no join evidence"
        )
        undecided = set(report.meet_undecided_ids)
        for m in sorted(presumed, key=lambda x: x.start_label)[:15]:
            # lex-phi-identity-2: the live lane saw joins it could not count in
            # people -- still a presumption, and the line says why (never silent).
            why = ("  _[joined -- the Meet join log cannot tell one person from two]_"
                   if m.event_id in undecided else "")
            lines.append(f"  - {m.start_label}  {_esc(m.title)}  _(organizer {_esc(m.organizer)})_{why}")
        if len(presumed) > 15:
            lines.append(f"  _...and {len(presumed) - 15} more_")

    if report.meet_audit_state in MEET_AUDIT_WARN_STATES:
        # The lane is provisioned but its read could not decide today. One line,
        # never an alarm of its own (the nightly health check WARNs on it).
        lines.append(
            f"\n:warning: Meet join log read {report.meet_audit_state} -- "
            "unconvened remain presumptions today"
        )

    # CLEAN-DAY CRITERION. Unconvened is deliberately NOT in this veto: a presumed
    # placeholder nobody joined is not a capture failure. Misses, duplicates,
    # unmatched captures, carve-out breaches and a degraded read all still are.
    # carved_recordings (a qualification-skipped meeting recorded anyway) is
    # information, not a failure, and is NOT in this veto either (Code #15 S4) --
    # UNLESS one was recorded more than once: a double capture is a duplicate
    # whatever the meeting's scope, and must never read as a clean day (D-051 s4#1).
    if not (report.misses or report.duplicates or report.unmatched_transcripts
            or report.carve_out_breaches or report.carved_duplicated) and not degraded:
        # A weekend has no meetings, and "captured exactly once" over a denominator
        # of zero reads as a success it did not earn. This report posts every day.
        if report.scheduled == 0:
            lines.append("\n:white_check_mark: No qualifying roster meetings scheduled.")
        elif presumed:
            # NO checkmark here. "Every convened meeting captured" is a claim the
            # auditor cannot prove: unconvened is a PRESUMPTION (no Meet/Zoom audit
            # log exists), so a day carrying one is reported as unverified, never
            # as clean (D-051 review, Code #13 C-2).
            lines.append(
                f"\n:white_circle: {len(presumed)} presumed unconvened -- not "
                "verified. Nothing else scheduled was missed or duplicated."
            )
        elif confirmed:
            # R14-8: every unconvened meeting is Meet-join-log EVIDENCE, nothing is
            # presumed, and nothing was missed -- the claim is now provable.
            lines.append(
                "\n:white_check_mark: Every convened meeting captured exactly once "
                f"({len(confirmed)} not convened, per the Meet join log)."
            )
        else:
            lines.append("\n:white_check_mark: Every scheduled meeting captured exactly once.")

    # INFORMATIONAL, below every alarm and after the clean-day verdict, and never
    # part of that verdict: how many invites cora@ accepted as itself for this
    # day (LEX counted apart, ruled 2026-09-19 ask 9.3). Counts only -- no ids,
    # no titles, no organisers.
    rsvp_line = _rsvp_line(report.rsvp)
    if rsvp_line:
        lines.append(f"\n{rsvp_line}")

    if report.seat_note:
        lines.append(f"\n_{report.seat_note}_")
    return "\n".join(lines)
