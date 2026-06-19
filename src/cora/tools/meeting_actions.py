"""Meeting action items -- PULL flow (user-initiated, staged-write).

This replaces the PUSH model (the hourly "Cora - Meeting Action Capture" task
that auto-created/auto-assigned Asana tasks from every meeting -- the source of
Demi's "14 unwanted tasks" frustration). Instead of Cora deciding-and-creating,
a meeting ATTENDEE asks Cora in Slack:

    @Cora what were my action items from the F3 marketing sync?

and Cora:
  1. resolves the right transcript (scoped to meetings the asker ATTENDED,
     fetched by the asker's email so the window is THEIR meetings, not the org's;
     disambiguates with a pick-list when the hint matches more than one),
  2. returns a short summary + the action items meant for THEM (plus a small
     "unclear owner -- claim if yours" list),
  3. lets the user confirm which are actually theirs, and ONLY THEN
  4. creates those as Asana tasks assigned to the asker, via the staged-write
     confirm gate.

This embodies the North-Star invariant "decision-SUPPORT, not decision-MAKER":
the human confirms before any task is created. It does NOT change KB ingest or
recall -- "recall any item from any meeting" stays the existing entity-scoped,
PHI-guarded Cora KB Q&A path (the nightly Fireflies sync is untouched).

Security model (this tool self-enforces -- entity-scoping is a perf hint, not a
boundary; see tool_dispatch.py header):
  * ATTENDEE GATE (primary): the resolution window is fetched by the asker's
    own email (participant_email), and both the preview and the confirm
    re-verify attendee membership. A non-attendee gets nothing.
  * CHANNEL/DM SCOPE GATE: a meeting's content (and even its title/existence in a
    pick-list) surfaces only where it belongs -- LEX meetings only in a LEX
    channel (or a LEX person's DM); a specific-entity meeting only in that
    entity's channel, a founder/HJRG channel, or any DM (private to the asker).
    The scope + LEX gate is applied to EVERY candidate before it can appear in
    a pick-list, not just to the single resolved meeting.
  * LEX RAILS (carried forward verbatim from D-052): a meeting is treated as LEX
    if ANY of -- its title classifies LEX, a NAMED LEX lead attends, or an
    attendee's email is on a Lexington DOMAIN (so a generically-titled LEX
    meeting attended by non-lead Lexington staff is still caught). LEX capture
    must be enabled; the sub-entity must be in scope
    (LEX-LBHS / 42 CFR Part 2 EXCLUDED); clinically-titled meetings are skipped;
    title + summary + every item + due text PHI-scrubbed (scrub_lex_phi keeps
    staff names); created LEX tasks route ONLY into LEX-scoped projects (or are
    skipped if none).

Reuses the proven helpers in fireflies_action_extractor + fireflies_connector;
adds NO new auto-create behavior. Bot-loaded (registered in tool_dispatch) ->
activating it requires a bot restart.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from cora import org_roles
from cora.connectors import fireflies_action_extractor as fae
from cora.connectors.fireflies_connector import (
    FirefliesConnectorError,
    _classify_entity,
    _graphql_query,
    _is_phi_meeting,
    _normalize_title,
    _parse_date,
    _resolve_participant_slack_ids,
    _tag_fireflies_sub_entity,
)
from cora.tools import asana_client
from cora.tools.project_resolver import is_blocked_project, resolve_project

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ASANA_MAP_PATH = _REPO_ROOT / "data" / "maps" / "slack-to-asana.yaml"

# Resolution window. Pulls are about RECENT meetings ("what were my action items
# from [this week's] sync"); 14 days covers that. The fetch is scoped to the
# asker (participant_email) so the window is THEIR meetings, not the org's.
_WINDOW_DAYS = 14
_BATCH_SIZE = 50
_MAX_BATCHES = 3            # per asker-email; their own 14-day count is small
_MAX_SELECTED = 6           # cap tasks created in one confirm call (timeout safety)
_PICKLIST_CAP = 8           # max meetings shown in a disambiguation / recent list
_CREATE_BUDGET_SEC = 18.0   # self-bound the create loop under the 25s tool timeout
_DEDUP_SCAN_MAX = 500       # max open tasks scanned per project for the exact-name dedup

# Aggregator channels that may pull any NON-LEX meeting (cross-entity by design).
_AGGREGATOR_ENTITIES = frozenset({"FNDR", "HJRG"})

# Lexington email domains -> sub-entity, for the LEX-by-domain signal. The
# name-based detector (_tag_fireflies_sub_entity) only knows four named leads, so
# a generically-titled LEX meeting attended by Jen/Aaron/line-staff (no named
# lead) would otherwise classify non-LEX and leak unscrubbed. Order = most
# restrictive first so an LBHS (42 CFR Part 2) attendee always wins. The shared
# lexingtonservices.com domain spans LLC/LLA/admin, so it maps to GM-level "LEX"
# (the safe default -- _lex_gate still allows it, scrubbed, GM-scoped).
_LEX_DOMAIN_SUBENTITY: list[tuple[str, str]] = [
    ("lexingtonbhs.com", "LEX-LBHS"),
    ("lexingtontherapyservices.com", "LEX-LTS"),
    ("lexingtonservices.com", "LEX"),
]

# Shared transcript field block for all queries (no `sentences` -- the heavy
# field). `participants` is included so the secondary attendee check has data.
_TRANSCRIPT_FIELDS = """
    id
    title
    date
    meeting_link
    participants
    summary {
      overview
      short_summary
      action_items
    }
    meeting_attendees {
      displayName
      email
    }
"""

# Asker-scoped query (participant_email) -- the window is the asker's meetings.
_BY_PARTICIPANT_QUERY = (
    "query T($email: String, $fromDate: DateTime, $toDate: DateTime, $limit: Int, $skip: Int) {"
    "  transcripts(participant_email: $email, fromDate: $fromDate, toDate: $toDate, limit: $limit, skip: $skip) {"
    + _TRANSCRIPT_FIELDS +
    "  }"
    "}"
)

# Unfiltered window (FALLBACK only -- used if the participant_email filter ever
# errors, so resolution still works).
_WINDOW_QUERY = (
    "query T($fromDate: DateTime, $toDate: DateTime, $limit: Int, $skip: Int) {"
    "  transcripts(fromDate: $fromDate, toDate: $toDate, limit: $limit, skip: $skip) {"
    + _TRANSCRIPT_FIELDS +
    "  }"
    "}"
)

# Single-transcript-by-id query (used to re-verify on the confirm call).
_TRANSCRIPT_BY_ID_QUERY = (
    "query Transcript($id: String!) {"
    "  transcript(id: $id) {"
    + _TRANSCRIPT_FIELDS +
    "  }"
    "}"
)

# Stopwords excluded when matching a selected item against the meeting text.
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "that", "this", "from", "into", "your", "their",
    "about", "will", "should", "would", "could", "have", "need", "must", "send",
    "make", "take", "follow", "update", "review", "meeting", "team", "next",
})

# ── Date / ordinal selectors (pick-list follow-up resolution) ────────────────
# A pick-list reply often selects by DATE ("June 18", "6/18", "today", "the
# 18th") or POSITION ("the first one", "last one") rather than echoing the
# hidden [id:...]. Title-only matching can't resolve those (a date/ordinal token
# isn't in the title), so we parse them, strip them, then title-match the rest.
#
# America/Phoenix is a fixed UTC-7 (no DST). ZoneInfo RAISES on this host (see
# repo doctrine), so use a fixed offset -- never zoneinfo.
_AZ_TZ = timezone(timedelta(hours=-7))

_MONTHS: dict[str, int] = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

_ORDINAL_WORDS: dict[str, int] = {
    "first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3,
    "fourth": 4, "4th": 4, "fifth": 5, "5th": 5, "sixth": 6, "6th": 6,
    "seventh": 7, "7th": 7, "eighth": 8, "8th": 8, "last": -1,
}

_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))  # longest-first
# "June 18", "Jun 18th", "December 3, 2026"
_RE_MONTH_DAY = re.compile(
    rf"\b({_MONTH_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:\s*,?\s*(\d{{4}}))?\b",
    re.IGNORECASE,
)
# ISO "2026-06-18" -- matched BEFORE the bare m/d regex so the year is captured
# and the whole span (incl. the year) is consumed (not left as a "2026" token).
_RE_ISO_DATE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
# "6/18", "06-18", "6/18/2026", "6/18/26" (US month/day order)
_RE_NUMERIC_DATE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b")
# relative day
_RE_RELATIVE_DATE = re.compile(r"\b(today|yesterday)\b", re.IGNORECASE)
# bare day-of-month ordinal: "the 18th", "18th" (requires the st/nd/rd/th suffix
# so it never matches a plain number)
_RE_DAY_ORDINAL = re.compile(r"\b(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)\b", re.IGNORECASE)
_ORD_ALT = "|".join(_ORDINAL_WORDS)
# ordinal SELECTION with explicit "one" context ("the first one", "2nd one")
_RE_ORDINAL_SELECT = re.compile(rf"\b(?:the\s+)?({_ORD_ALT})\s+one\b", re.IGNORECASE)
# a query that is ENTIRELY an ordinal word ("first", "the last") -> selection
_RE_ORDINAL_BARE = re.compile(rf"^(?:the\s+)?({_ORD_ALT})$", re.IGNORECASE)
# Pure filler left after stripping a selector -> treat as no title (so it can't
# become a bogus title filter). Only true filler; generic-but-real title words
# like "sync"/"standup" are NOT here.
_RESIDUAL_JUNK = frozenset({
    "", "the", "one", "that", "that one", "the one", "meeting", "the meeting",
    "call", "the call", "from", "from the meeting", "for",
})
# Filler words dropped on the LENIENT title retry (when the residual didn't
# title-match): so "Lexington Progress meeting June 18" still resolves via its
# real "Lexington Progress" core, WITHOUT falling back to the full meeting list
# (which would risk a wrong-meeting substitution). Generic-but-real title words
# (sync/standup/review/...) are NOT here -- they stay as discriminators.
_FILLER_TOKENS = frozenset({
    "the", "a", "an", "our", "my", "your", "with", "for", "on", "from", "about",
    "meeting", "meetings", "mtg", "call", "calls",
})


def _strip_filler(text: str) -> str:
    """Drop leading/standalone filler words from a residual title (lenient retry)."""
    out = [
        w for w in re.split(r"\s+", (text or "").strip())
        if w and w.lower().strip(".,;:-'\"") not in _FILLER_TOKENS
    ]
    return " ".join(out).strip()

_module_slack_map: dict[str, dict] | None = None  # module-level cache


# ---------------------------------------------------------------------------
# Asker identity (slack_user_id -> emails / asana gid / display name)
# ---------------------------------------------------------------------------

def _load_slack_map() -> dict[str, dict]:
    """slack_user_id -> slack-to-asana entry. Loaded directly (NOT via
    tool_dispatch) to avoid a circular import."""
    global _module_slack_map
    if _module_slack_map is not None:
        return _module_slack_map
    try:
        data = yaml.safe_load(_ASANA_MAP_PATH.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 -- degrade gracefully
        log.warning("meeting_actions: could not load slack-to-asana.yaml: %s", exc)
        _module_slack_map = {}
        return _module_slack_map
    out: dict[str, dict] = {}
    for entry in (data.get("users") or []):
        if isinstance(entry, dict) and entry.get("slack_user_id"):
            out[str(entry["slack_user_id"]).strip()] = entry
    _module_slack_map = out
    return _module_slack_map


def _asker_entry(slack_user_id: str) -> dict:
    return _load_slack_map().get(slack_user_id, {})


def _asker_emails(slack_user_id: str) -> set[str]:
    """The asker's known email addresses (primary + aliases), lowercased."""
    entry = _asker_entry(slack_user_id)
    emails: set[str] = set()
    primary = str(entry.get("asana_email", "") or "").strip().lower()
    if primary:
        emails.add(primary)
    for alias in (entry.get("email_aliases") or []):
        if alias:
            emails.add(str(alias).strip().lower())
    return emails


def _asker_name(slack_user_id: str) -> str:
    """Canonical display name for the asker (org-roles first, then the map, then
    a last-resort email local-part so item-ownership matching still has a shot)."""
    try:
        rec = org_roles.get_role(slack_user_id)
        if rec and rec.name:
            return rec.name
    except Exception as exc:  # noqa: BLE001
        log.debug("meeting_actions: org_roles lookup failed for %s: %s", slack_user_id, exc)
    disp = str(_asker_entry(slack_user_id).get("display_name", "") or "").strip()
    if disp:
        return disp
    # Last resort: capitalize the email local-part (helps _match_roster_name when
    # the local-part is a first name, e.g. tommy@ -> "Tommy").
    for e in sorted(_asker_emails(slack_user_id)):
        local = e.split("@", 1)[0]
        local = re.sub(r"[._-]+", " ", local).strip()
        if local:
            return local.title()
    return ""


def _asker_asana_gid(slack_user_id: str) -> str | None:
    gid = str(_asker_entry(slack_user_id).get("asana_user_gid", "") or "").strip()
    if not gid or "REPLACE" in gid:
        return None
    return gid


# ---------------------------------------------------------------------------
# Transcript fetching (asker-scoped)
# ---------------------------------------------------------------------------

def _window_bounds() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    return (now - timedelta(days=_WINDOW_DAYS)).isoformat(), now.isoformat()


def _recent_transcripts_unfiltered() -> list[dict]:
    """Org-wide window fetch (FALLBACK). Bounded by _MAX_BATCHES."""
    from_date, to_date = _window_bounds()
    out: list[dict] = []
    skip = 0
    for _ in range(_MAX_BATCHES):
        data = _graphql_query(
            _WINDOW_QUERY,
            {"limit": _BATCH_SIZE, "skip": skip, "fromDate": from_date, "toDate": to_date},
        )
        batch = data.get("transcripts") or []
        if not batch:
            break
        out.extend(batch)
        if len(batch) < _BATCH_SIZE:
            break
        skip += _BATCH_SIZE
    return out


def _recent_transcripts(asker_emails: set[str]) -> list[dict]:
    """Fetch the asker's recent meetings via the participant_email filter.

    Querying per asker-email keeps the window THEIR meetings (not the org's), so
    a busy org can't push the asker's own meeting out of a global cap. FAIL-SAFE:
    if the participant_email filter ever errors (e.g. an unexpected schema), fall
    back to the unfiltered org-wide window so resolution still works.
    """
    if not asker_emails:
        return []
    from_date, to_date = _window_bounds()
    by_id: dict[str, dict] = {}
    any_ok = False
    for email in sorted(asker_emails):
        try:
            skip = 0
            for _ in range(_MAX_BATCHES):
                data = _graphql_query(
                    _BY_PARTICIPANT_QUERY,
                    {"email": email, "limit": _BATCH_SIZE, "skip": skip,
                     "fromDate": from_date, "toDate": to_date},
                )
                any_ok = True
                batch = data.get("transcripts") or []
                for t in batch:
                    tid = t.get("id")
                    if tid:
                        by_id[tid] = t
                if len(batch) < _BATCH_SIZE:
                    break
                skip += _BATCH_SIZE
        except FirefliesConnectorError as exc:
            log.warning("meeting_actions: participant fetch failed for %s: %s", email, exc)
    if any_ok:
        return list(by_id.values())
    # Every participant query errored -> fall back to the org-wide window.
    log.warning("meeting_actions: participant_email filter unavailable -- using org-wide window")
    return _recent_transcripts_unfiltered()


def _fetch_transcript_by_id(transcript_id: str) -> dict | None:
    """Fetch one transcript by id. FAIL-SAFE: on any error fall back to scanning
    the org-wide window for the id, so the confirm step never breaks."""
    try:
        data = _graphql_query(_TRANSCRIPT_BY_ID_QUERY, {"id": transcript_id})
        t = data.get("transcript")
        if t:
            return t
    except FirefliesConnectorError as exc:
        log.warning("meeting_actions: by-id fetch failed (%s) -- window fallback", exc)
    except Exception as exc:  # noqa: BLE001
        log.warning("meeting_actions: by-id fetch error (%s) -- window fallback", exc)
    try:
        for t in _recent_transcripts_unfiltered():
            if (t.get("id") or "") == transcript_id:
                return t
    except FirefliesConnectorError as exc:
        log.warning("meeting_actions: window fallback failed: %s", exc)
    return None


# ---------------------------------------------------------------------------
# Classification (title OR participant-email LEX detector) + attendance
# ---------------------------------------------------------------------------

def _attendee_emails(transcript: dict) -> set[str]:
    """All attendee + participant email addresses on a transcript, lowercased."""
    out = {
        (a.get("email") or "").strip().lower()
        for a in (transcript.get("meeting_attendees") or [])
        if isinstance(a, dict)
    }
    out |= {
        str(p).strip().lower()
        for p in (transcript.get("participants") or [])
        if isinstance(p, str)
    }
    return {e for e in out if e}


def _lex_domain_subentity(transcript: dict) -> str | None:
    """LEX sub-entity implied by any attendee's email DOMAIN, or None.

    Most-restrictive first (LBHS / Part 2 wins). This complements the name-based
    _tag_fireflies_sub_entity so a LEX meeting with no NAMED lead still resolves.
    """
    emails = _attendee_emails(transcript)
    for domain, code in _LEX_DOMAIN_SUBENTITY:
        if any(e.endswith("@" + domain) for e in emails):
            return code
    return None


def _lex_scope_subentity(transcript: dict) -> str:
    """Resolve the LEX sub-entity used for the gate/exclusion decision.

    Combines the name-based detector + the email-domain detector, MOST-RESTRICTIVE
    WINS (an LBHS / Part-2 signal from EITHER source forces LEX-LBHS so the gate
    excludes it). Falls back to GM-level "LEX" (the safe default -- allowed,
    scrubbed, GM-scoped) when no sub-entity can be pinned.
    """
    try:
        name_sub = _tag_fireflies_sub_entity(transcript) or ""
    except Exception as exc:  # noqa: BLE001
        log.debug("meeting_actions: sub-entity tag failed: %s", exc)
        name_sub = ""
    domain_sub = _lex_domain_subentity(transcript) or ""
    if name_sub == "LEX-LBHS" or domain_sub == "LEX-LBHS":
        return "LEX-LBHS"          # 42 CFR Part 2 -- most restrictive
    if name_sub:
        return name_sub            # specific name-based sub-entity (LTS/LLA/LLC)
    if domain_sub:
        return domain_sub          # domain-based (LEX-LTS) or GM-level "LEX"
    return "LEX"


def _classify_meeting(transcript: dict) -> tuple[str, bool]:
    """Return (meeting_entity, is_lex).

    is_lex if ANY of: the title classifies LEX, a NAMED LEX lead attends
    (_tag_fireflies_sub_entity), or an attendee's email is on a Lexington DOMAIN
    (_lex_domain_subentity). The domain signal catches a generically-titled LEX
    meeting attended only by Jen/Aaron/line-staff -- none of the four named leads
    -- that title + name signals alone miss, so the LEX rails + PHI scrub still
    apply. When LEX, meeting_entity is "LEX".
    """
    title = (transcript.get("title") or "").strip()
    base = _classify_entity(title)
    try:
        name_sub = _tag_fireflies_sub_entity(transcript) or ""
    except Exception as exc:  # noqa: BLE001
        log.debug("meeting_actions: sub-entity tag failed: %s", exc)
        name_sub = ""
    domain_sub = _lex_domain_subentity(transcript) or ""
    is_lex = base == "LEX" or str(name_sub).upper().startswith("LEX") or bool(domain_sub)
    return ("LEX" if is_lex else base), is_lex


def _asker_attended(transcript: dict, asker_emails: set[str], slack_user_id: str) -> bool:
    """True if the asker attended this meeting.

    Primary: the asker's slack_user_id is among the attendee->slack resolutions
    (robust to null displayNames -- it maps on email). Secondary: a direct
    email-set intersection against attendees + participants.
    """
    try:
        if slack_user_id in set(
            _resolve_participant_slack_ids(transcript.get("meeting_attendees") or [])
        ):
            return True
    except Exception as exc:  # noqa: BLE001
        log.debug("meeting_actions: participant-slack resolve failed: %s", exc)
    if not asker_emails:
        return False
    present = {
        (a.get("email") or "").strip().lower()
        for a in (transcript.get("meeting_attendees") or [])
        if isinstance(a, dict)
    }
    present |= {
        str(p).strip().lower()
        for p in (transcript.get("participants") or [])
        if isinstance(p, str)
    }
    return bool(asker_emails & present)


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------

def _dedup_meetings(transcripts: list[dict]) -> list[dict]:
    """Collapse duplicate notetaker copies of the SAME meeting for resolution.

    Keyed on (meeting_link, normalized title, calendar day) -- meeting_link is
    the strong identity (so two genuinely-distinct same-title same-day meetings
    with different links stay separately selectable). Keeps the copy with the
    longest action_items text (the most-complete).
    """
    best: dict[tuple, dict] = {}
    for t in transcripts:
        ts = _parse_date(t.get("date"))
        day = (
            datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            if ts else ""
        )
        link = (t.get("meeting_link") or "").strip().lower()
        key = (link, _normalize_title(t.get("title")), day)
        cur = best.get(key)
        if cur is None:
            best[key] = t
            continue
        new_len = len(((t.get("summary") or {}).get("action_items") or ""))
        cur_len = len(((cur.get("summary") or {}).get("action_items") or ""))
        if new_len > cur_len:
            best[key] = t
    return sorted(
        best.values(),
        key=lambda t: _parse_date(t.get("date")) or 0,
        reverse=True,
    )


def _match_query(query: str, transcripts: list[dict]) -> list[dict]:
    """Return transcripts whose title matches the free-text query.

    Substring match first; then an all-significant-tokens match; else empty.
    """
    q = (query or "").strip().lower()
    if not q:
        return []
    subs = [t for t in transcripts if q in (t.get("title") or "").lower()]
    if subs:
        return subs
    toks = [w for w in re.findall(r"[a-z0-9]+", q) if len(w) >= 3]
    if toks:
        tok_matches = [
            t for t in transcripts
            if all(w in (t.get("title") or "").lower() for w in toks)
        ]
        if tok_matches:
            return tok_matches
    return []


def _meeting_date_str(transcript: dict) -> str:
    ts = _parse_date(transcript.get("date"))
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        if ts else "unknown date"
    )


# ── Date / ordinal-aware resolution (pick-list follow-up) ────────────────────

def _today_az() -> tuple[int, int, int]:
    now = datetime.now(_AZ_TZ)
    return (now.year, now.month, now.day)


def _shift_day(ymd: tuple[int, int, int], delta_days: int) -> tuple[int, int, int]:
    base = datetime(ymd[0], ymd[1], ymd[2], tzinfo=_AZ_TZ) + timedelta(days=delta_days)
    return (base.year, base.month, base.day)


def _meeting_ymd_utc(transcript: dict) -> tuple[int, int, int] | None:
    """The meeting's (year, month, day) in UTC -- the SAME day the pick-list
    labels it (_meeting_date_str uses UTC). None if no parseable date."""
    ts = _parse_date(transcript.get("date"))
    if not ts:
        return None
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return (dt.year, dt.month, dt.day)


def _meeting_ymds(transcript: dict) -> set[tuple[int, int, int]]:
    """The meeting's (year, month, day) in BOTH UTC and AZ-local. Used only for
    RELATIVE hints ("today"/"yesterday"), which the user means in AZ from their
    seat -- so an evening-AZ meeting (next-day UTC) still answers to "yesterday".
    Empty when the transcript has no parseable date."""
    ts = _parse_date(transcript.get("date"))
    if not ts:
        return set()
    out: set[tuple[int, int, int]] = set()
    for tz in (timezone.utc, _AZ_TZ):
        dt = datetime.fromtimestamp(ts, tz=tz)
        out.add((dt.year, dt.month, dt.day))
    return out


def _date_hint_matches(
    transcript: dict, hint: tuple[int | None, int | None, int, bool]
) -> bool:
    """True if the transcript's date matches a (year|None, month|None, day,
    relative) hint.

    EXPLICIT dates ("June 18", "6/18", "the 18th") match ONLY the UTC day -- the
    exact day the pick-list showed the user, so a typed date can never resolve a
    meeting labeled a different day. RELATIVE dates ("today"/"yesterday") match
    EITHER the UTC or the AZ-local day (the user means AZ; the label is UTC).
    year/month may be None (a bare day-of-month matches on day alone)."""
    y, m, d, relative = hint
    if relative:
        days = _meeting_ymds(transcript)
    else:
        u = _meeting_ymd_utc(transcript)
        days = {u} if u else set()
    for (yy, mm, dd) in days:
        if d is not None and dd != d:
            continue
        if m is not None and mm != m:
            continue
        if y is not None and yy != y:
            continue
        return True
    return False


def _extract_selectors(
    query: str,
) -> tuple[tuple[int | None, int | None, int, bool] | None, int | None, str]:
    """Pull a DATE hint and/or an ORDINAL selection out of the query.

    Returns (date_hint, ordinal, residual): date_hint is (year|None, month|None,
    day, relative_bool) or None; ordinal is 1-based (or -1 for "last") or None;
    residual is the query with the date/ordinal tokens removed (the title part).
    Ordinals are only recognized as a SELECTION ("first one", "the last", or a
    query that is ENTIRELY an ordinal word) -- never a bare "first" living inside
    a title like "First Quarter Review"."""
    residual = query or ""
    ordinal: int | None = None

    # 1) Ordinal selection WITH explicit "one" context ("the first one").
    om = _RE_ORDINAL_SELECT.search(residual)
    if om:
        ordinal = _ORDINAL_WORDS.get(om.group(1).lower())
        residual = residual[: om.start()] + " " + residual[om.end():]

    # 2) Bare ordinal: the WHOLE query is just an ordinal word ("first", "2nd",
    #    "the last"). Checked BEFORE the date regexes so a bare digit-ordinal
    #    ("2nd"/"3rd") is a POSITION like its word form, not day-of-month 2/3.
    #    (Day-of-month "the 18th"/"9th"+ falls through -- not in _ORDINAL_WORDS.)
    if ordinal is None:
        bm = _RE_ORDINAL_BARE.match(residual.strip())
        if bm:
            ordinal = _ORDINAL_WORDS.get(bm.group(1).lower())
            residual = ""

    # 3) Date hint (first match wins): ISO, month-name+day, numeric m/d,
    #    relative, then bare day-of-month ordinal. relative=True only for
    #    today/yesterday (the only AZ-from-the-user's-seat readings).
    date_hint: tuple[int | None, int | None, int, bool] | None = None
    if residual:
        iso = _RE_ISO_DATE.search(residual)
        if iso:
            year, mon, day = int(iso.group(1)), int(iso.group(2)), int(iso.group(3))
            if 1 <= mon <= 12 and 1 <= day <= 31:
                date_hint = (year, mon, day, False)
                residual = residual[: iso.start()] + " " + residual[iso.end():]
        if date_hint is None:
            md = _RE_MONTH_DAY.search(residual)
            if md:
                mon = _MONTHS.get(md.group(1).lower())
                day = int(md.group(2))
                year = int(md.group(3)) if md.group(3) else None
                if mon and 1 <= day <= 31:
                    date_hint = (year, mon, day, False)
                    residual = residual[: md.start()] + " " + residual[md.end():]
        if date_hint is None:
            nd = _RE_NUMERIC_DATE.search(residual)
            if nd:
                mon = int(nd.group(1))
                day = int(nd.group(2))
                yr = nd.group(3)
                year = (2000 + int(yr)) if (yr and len(yr) == 2) else (int(yr) if yr else None)
                if 1 <= mon <= 12 and 1 <= day <= 31:
                    date_hint = (year, mon, day, False)
                    residual = residual[: nd.start()] + " " + residual[nd.end():]
        if date_hint is None:
            rd = _RE_RELATIVE_DATE.search(residual)
            if rd:
                ry, rm, rday = _today_az()
                if rd.group(1).lower() == "yesterday":
                    ry, rm, rday = _shift_day((ry, rm, rday), -1)
                date_hint = (ry, rm, rday, True)
                residual = residual[: rd.start()] + " " + residual[rd.end():]
        if date_hint is None:
            dd = _RE_DAY_ORDINAL.search(residual)
            if dd:
                day = int(dd.group(1))
                if 1 <= day <= 31:
                    date_hint = (None, None, day, False)
                    residual = residual[: dd.start()] + " " + residual[dd.end():]

    residual = re.sub(r"\s+", " ", residual).strip(" \t-,.;:")
    if residual.lower() in _RESIDUAL_JUNK:
        residual = ""
    return date_hint, ordinal, residual


def _resolve_meetings(query: str, transcripts: list[dict]) -> list[dict]:
    """Resolve a free-text meeting hint to transcript(s) -- DATE- and ORDINAL-
    aware (a pick-list follow-up that selects by date or position resolves to the
    offered meeting). `transcripts` is newest-first (from _dedup_meetings).

    Plain title queries are unchanged. When a date/ordinal IS present, the
    FULL-query title match is preferred: if the user's exact words (date-looking
    tokens and all) name a real meeting title -- e.g. "Q2 6/30 Forecast" or
    "11th Hour Review" -- that title match is honored, and the selector
    interpretation is the FALLBACK. The two interpretations are UNION'd: if they
    disagree (the title names meeting A while a stripped date pins meeting B) we
    return BOTH as a pick-list rather than silently choosing one -- so the tool
    NEVER silently resolves to the wrong meeting. A real title that matches
    nothing is NOT replaced by a date/position guess over unrelated meetings."""
    q = (query or "").strip()
    if not q:
        return []
    date_hint, ordinal, residual = _extract_selectors(q)
    # The FULL, un-stripped query as a title (so an in-title date isn't hijacked).
    title_full = _match_query(q, transcripts)
    if date_hint is None and ordinal is None:
        return title_full  # plain-title path (identical to the old behavior)

    # Selector interpretation: title-match the residual, then narrow by
    # date/ordinal. A non-empty residual that doesn't match is retried leniently
    # (filler words dropped); only an ALL-filler residual becomes a pure
    # date/ordinal query over the full set -- a real-but-unmatched title yields
    # no selector match (no wrong-meeting fall-back).
    if residual:
        title_base = _match_query(residual, transcripts)
        if not title_base:
            core = _strip_filler(residual)
            title_base = list(transcripts) if not core else _match_query(core, transcripts)
    else:
        title_base = list(transcripts)

    if date_hint is not None:
        sel = [t for t in title_base if _date_hint_matches(t, date_hint)]
    else:
        # Ordinal selects by position within the rows actually shown (capped to
        # the pick-list size), 1-based, newest-first; "last" = the last shown.
        pool = title_base[:_PICKLIST_CAP]
        if not pool or ordinal is None:
            sel = []
        elif ordinal == -1:
            sel = [pool[-1]]
        elif 1 <= ordinal <= len(pool):
            sel = [pool[ordinal - 1]]
        else:
            sel = []

    # Union title_full + selector result, deduped by object identity, preserving
    # the newest-first order of `transcripts`. >1 -> the caller shows a pick-list.
    chosen = {id(t) for t in title_full} | {id(t) for t in sel}
    return [t for t in transcripts if id(t) in chosen]


# ---------------------------------------------------------------------------
# Scope gate (channel / DM) + LEX gate
# ---------------------------------------------------------------------------

def _normalize_channel_entity(entity: str) -> str:
    """Normalize a (possibly sub-)entity channel code to its top-level code for
    comparison against a meeting's classified entity."""
    from cora.tools.tool_dispatch import _SUBENTITY_PARENT  # local import: avoid cycle
    e = (entity or "FNDR").upper()
    return _SUBENTITY_PARENT.get(e, e)


def _scope_ok(meeting_entity: str, channel_entity: str, is_dm: bool) -> tuple[bool, str]:
    """Decide whether a meeting may surface for this channel/DM.

    LEX meetings: only a LEX channel or a LEX person's DM (channel_entity LEX*).
    Non-LEX meetings: the meeting's own entity channel, a founder/HJRG channel,
    or any DM (private to the asker -- the attendee gate already proved they were
    in the room).
    """
    me = (meeting_entity or "FNDR").upper()
    ce = (channel_entity or "FNDR").upper()
    if me.startswith("LEX"):
        if ce.startswith("LEX"):
            return True, ""
        return False, (
            "That's a Lexington meeting -- for privacy I can only pull its details "
            "from a Lexington channel (or a DM with me if you're on the Lexington team)."
        )
    # Non-LEX meeting.
    if is_dm:
        return True, ""
    if ce in _AGGREGATOR_ENTITIES:
        return True, ""
    if _normalize_channel_entity(ce) == me:
        return True, ""
    return False, (
        f"That meeting is scoped to {me}. To avoid posting its details where they "
        f"don't belong, ask me from a {me} channel, a founder channel, or DM me."
    )


def _lex_gate(transcript: dict, title: str, meeting_entity: str) -> tuple[bool, str, str]:
    """Apply the D-052 LEX rails. Returns (ok, refusal_or_empty, scoped_entity).

    scoped_entity is the LEX sub-entity (e.g. LEX-LLC) for routing/scrubbing, or
    "" for non-LEX meetings (the caller skips LEX handling then).
    """
    if (meeting_entity or "").upper() != "LEX":
        return True, "", ""
    if not fae._lex_capture_enabled():
        return False, (
            "Lexington meeting capture is turned off right now, so I can't pull "
            "action items from this meeting."
        ), ""
    # Most-restrictive-wins sub-entity (name + email-domain) so an LBHS attendee
    # is excluded even when no named lead is present.
    scoped = _lex_scope_subentity(transcript)
    if not fae._lex_sub_entity_allowed(scoped):
        # LEX-LBHS (42 CFR Part 2) or otherwise out of scope.
        return False, (
            "This Lexington meeting is in a confidentiality scope I'm not able to "
            "pull action items from. Please handle it directly."
        ), scoped
    # Clinical-title belt-and-suspenders (minimum-necessary).
    if _is_phi_meeting(title, "LEX"):
        return False, (
            "That looks like a clinical Lexington meeting -- I don't pull action "
            "items from clinical meetings. Please handle it directly."
        ), scoped
    return True, "", scoped


def _visible_meetings(
    transcripts: list[dict], channel_entity: str, is_dm: bool
) -> list[dict]:
    """Filter to meetings that may surface in this channel/DM.

    Applies the SCOPE gate (and, for LEX, the full LEX gate) to EVERY candidate
    BEFORE it can appear in a pick-list -- so a LEX or cross-entity meeting's
    title/date/existence never leaks into the wrong channel.
    """
    out: list[dict] = []
    for t in transcripts:
        title = (t.get("title") or "").strip()
        meeting_entity, is_lex = _classify_meeting(t)
        if not _scope_ok(meeting_entity, channel_entity, is_dm)[0]:
            continue
        if is_lex and not _lex_gate(t, title, meeting_entity)[0]:
            continue
        out.append(t)
    return out


# ---------------------------------------------------------------------------
# Candidate items
# ---------------------------------------------------------------------------

def _scrub_for_lex(text: str, is_lex: bool) -> str:
    """PHI-scrub text only when the meeting is LEX; pass through otherwise."""
    if not text:
        return text
    return fae._scrub_lex_text(text) if is_lex else text


def _split_candidates(
    parsed_items: list[dict[str, Any]],
    asker_name: str,
    roster: list[str],
) -> tuple[list[dict], list[dict]]:
    """Split grounded action items into (mine, unclear).

    mine    = items whose grounded assignee canonically matches the asker.
    unclear = items with no grounded owner (Haiku found none, or it was
              off-roster) -- the asker may claim one as theirs.
    Items clearly owned by SOMEONE ELSE are excluded: the asker can only create
    their own tasks (we never recreate the auto-assign-to-others push behavior).
    """
    asker_canon = fae._match_roster_name(asker_name, roster) or (asker_name or "").strip().lower()
    mine: list[dict] = []
    unclear: list[dict] = []
    for item in parsed_items:
        assignee = item.get("assignee_name")
        if not assignee:
            unclear.append(item)
            continue
        canon = fae._match_roster_name(assignee, roster)
        if canon and asker_canon and canon.lower() == str(asker_canon).lower():
            mine.append(item)
        elif canon:
            pass  # on-roster, owned by SOMEONE ELSE -> excluded (never the asker's to create)
        else:
            unclear.append(item)  # named but off-roster (vendor/mis-parse) -> claimable
    return mine, unclear


def _significant_tokens(text: str) -> set[str]:
    return {
        w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(w) >= 4 and w not in _STOPWORDS
    }


def _item_matches_meeting(item: str, action_items_text: str) -> bool:
    """Lenient integrity check: does this selected task text correspond to the
    meeting's actual action items? Blocks clearly-fabricated text on the write
    path without rejecting Haiku's legitimate rephrasings. FAIL-OPEN when there's
    nothing to match on (e.g. empty meeting text or an all-stopword item)."""
    if not action_items_text:
        return True
    itoks = _significant_tokens(item)
    if not itoks:
        return True
    atext = action_items_text.lower()
    hits = sum(1 for w in itoks if w in atext)
    # Require at least 2 significant tokens present (or all of them, for a short
    # 1-token item) to appear in the meeting's action-item text.
    return hits >= min(2, len(itoks))


# ---------------------------------------------------------------------------
# Preview + create formatting
# ---------------------------------------------------------------------------

def _format_preview(
    transcript: dict,
    transcript_id: str,
    is_lex: bool,
    mine: list[dict],
    unclear: list[dict],
) -> str:
    title = _scrub_for_lex((transcript.get("title") or "").strip(), is_lex)
    date_str = _meeting_date_str(transcript)
    summary = transcript.get("summary") or {}
    raw_summary = (
        summary.get("short_summary")
        or summary.get("overview")
        or ((summary.get("action_items") or "")[:400])
        or ""
    ).strip()
    summary_text = _scrub_for_lex(raw_summary, is_lex)

    lines: list[str] = [f"MEETING: {title} ({date_str})"]
    if summary_text:
        lines.append("")
        lines.append(f"Summary: {summary_text}")
    lines.append("")
    if mine:
        lines.append("YOUR action items from this meeting:")
        for i, it in enumerate(mine, 1):
            due = it.get("due_mention")
            due_str = f"  (mentioned: {_scrub_for_lex(str(due), is_lex)})" if due else ""
            lines.append(f"  {i}. {_scrub_for_lex(it['task'], is_lex)}{due_str}")
    else:
        lines.append("I didn't find any action items in this meeting assigned to you.")
    if unclear:
        lines.append("")
        lines.append("Items with no clear owner (claim if one is yours):")
        for j, it in enumerate(unclear, len(mine) + 1):
            lines.append(f"  {j}. {_scrub_for_lex(it['task'], is_lex)}")

    lines.append("")
    lines.append(
        "INSTRUCTIONS FOR CORA (do not show this line to the user): present the "
        "summary and the numbered items above, then ask which the user wants you "
        "to create as Asana tasks assigned to THEM. Do NOT auto-create. When they "
        "choose, call meeting_action_items again with confirmed=true, "
        f"transcript_id=\"{transcript_id}\", and selected_items set to the exact "
        "task texts they picked. If they don't want any, create nothing."
    )
    return "\n".join(lines)


def _format_picklist(matches: list[dict], header: str) -> str:
    lines = [header, ""]
    for m in matches:
        title = (m.get("title") or "").strip()
        # Defense-in-depth: scrub a LEX meeting's title (it only reaches here in a
        # LEX channel, since _visible_meetings filters LEX out of non-LEX channels).
        _, is_lex = _classify_meeting(m)
        shown = fae._scrub_lex_text(title) if is_lex else title
        lines.append(f"- {shown} ({_meeting_date_str(m)})  [id:{m.get('id', '')}]")
    lines.append("")
    lines.append(
        "INSTRUCTIONS FOR CORA (do not show the bracketed ids to the user): show "
        "the user the titles + dates and ask which meeting they mean. When they "
        "pick one, call meeting_action_items again with transcript_id set to that "
        "meeting's id from the [id:...] tag above (NOT confirmed yet -- this just "
        "loads that meeting's items)."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Create (the staged write)
# ---------------------------------------------------------------------------

def _dedup_key(name: str) -> str:
    """Normalize a task name for dedup comparison.

    Collapse internal whitespace, truncate to the same _MAX_TASK_LEN cap creation
    applies, then lowercase. Truncating BOTH sides neutralizes the name-construction
    ORDER difference between the retired push (truncated RAW then scrubbed -- a long
    LEX name could end up >160) and this pull (scrubs then truncates at 160), and the
    whitespace-collapse absorbs minor LLM-relay drift. RESIDUAL (documented, accepted):
    when PHI straddles char 160 the scrubbed CONTENT can still differ (scrub-before vs
    scrub-after truncation) -- a narrow LEX-only, >160-char, TRANSIENT (push retired)
    case that fails OPEN (a duplicate, never a blocked legitimate task).
    """
    s = re.sub(r"\s+", " ", (name or "").strip())
    if len(s) > fae._MAX_TASK_LEN:
        s = s[:fae._MAX_TASK_LEN].rstrip()
    return s.lower()


def _open_task_names_in_project(project_gid: str, cache: dict) -> dict[str, str] | None:
    """normalized open-task-name (_dedup_key) -> gid for a project's INCOMPLETE tasks.

    Fetched once per project per confirm-call (cached in `cache`). Returns None
    on any fetch error so the caller FAILS OPEN (a dedup read must never block a
    legitimate create). Deterministic -- this replaces the Asana typeahead lookup,
    which is unreliable for the long descriptive names action items carry (it let
    a duplicate through in the 2026-06-18 smoke). Best-effort: scans at most
    _DEDUP_SCAN_MAX open tasks (project order, not newest-first) -- a duplicate
    past that ceiling is missed (logged); the typeahead net is the backstop.
    """
    if project_gid in cache:
        return cache[project_gid]
    namemap: dict[str, str] | None
    try:
        tasks = asana_client.get_project_tasks(project_gid, max_tasks=_DEDUP_SCAN_MAX)
        if len(tasks) >= _DEDUP_SCAN_MAX:
            log.warning(
                "meeting_actions: dedup scan hit the %d-task cap for project %s -- "
                "a duplicate beyond the cap may be missed", _DEDUP_SCAN_MAX, project_gid,
            )
        namemap = {}
        for t in tasks:
            key = _dedup_key(t.get("name") or "")
            gid = t.get("gid")
            if key and gid:
                namemap.setdefault(key, gid)  # first wins; we only need existence
    except Exception as exc:  # noqa: BLE001 -- dedup is best-effort; fail OPEN
        log.warning("meeting_actions: project dedup scan failed for %s: %s", project_gid, exc)
        namemap = None
    cache[project_gid] = namemap
    return namemap


def _existing_open_dup(project_gid: str, task_name: str, cache: dict) -> str | None:
    """Return the GID of an existing OPEN task with this (normalized) name in the
    TARGET project, else None. Project-scoped + deterministic: an open task with the
    same name in the same project (where both push and pull route) is the duplicate
    class the typeahead dedup missed."""
    if not project_gid or not task_name:
        return None
    namemap = _open_task_names_in_project(project_gid, cache)
    if not namemap:
        return None
    return namemap.get(_dedup_key(task_name))


def _create_selected(
    slack_user_id: str,
    transcript: dict,
    transcript_id: str,
    meeting_entity: str,
    is_lex: bool,
    scoped_entity: str,
    selected: list[str],
    dry_run: bool = False,
) -> str:
    """Create the selected action items as Asana tasks assigned to the ASKER.

    Each selected item is integrity-checked against the meeting's action_items
    text (no fabricated tasks), capped, and (for LEX) PHI-scrubbed + routed to a
    LEX-only project. Self-bounds on elapsed time so a big batch can't blow the
    25s tool timeout and silently leave partial creates.
    """
    assignee_gid = _asker_asana_gid(slack_user_id)
    if not assignee_gid:
        return (
            "I can't create these for you -- your Asana mapping isn't set up yet. "
            "Ask Harrison to add your row to the Slack-to-Asana map, then try again."
        )

    title = (transcript.get("title") or "").strip()
    display_title = _scrub_for_lex(title, is_lex)
    date_str = _meeting_date_str(transcript)
    route_entity = scoped_entity if is_lex else meeting_entity
    capture_fields = fae._capture_custom_fields(route_entity)
    # Match against the SAME redaction level as the selected text. The previewed
    # items the user passes back are already PHI-scrubbed (for LEX), so matching
    # them against RAW action-items would let redaction suppress a true token
    # match and silently drop a legitimate LEX task. Scrub both -> like-for-like.
    match_text = _scrub_for_lex(
        ((transcript.get("summary") or {}).get("action_items") or ""), is_lex
    )

    created: list[dict] = []
    skipped: list[str] = []          # no project to land in
    not_in_meeting: list[str] = []   # failed the meeting-content integrity check
    already_open: list[str] = []     # an open task already exists (not duplicated)
    create_failed: list[str] = []    # Asana create call errored
    created_keys: set[str] = set()   # dedup-keys created THIS call (in-call dup guard)
    _proj_dedup_cache: dict[str, dict[str, str] | None] = {}  # project_gid -> {name: gid} | None
    deadline = time.monotonic() + _CREATE_BUDGET_SEC
    budget_hit = False

    for raw in selected[:_MAX_SELECTED]:
        if time.monotonic() > deadline:
            budget_hit = True
            break
        task_name = str(raw or "").strip()
        if not task_name:
            continue

        # Integrity: only create tasks that correspond to this meeting's action
        # items (blocks fabricated/cross-meeting text on the write path).
        if not _item_matches_meeting(task_name, match_text):
            not_in_meeting.append(task_name)
            continue

        task_name = _scrub_for_lex(task_name, is_lex)
        if len(task_name) > fae._MAX_TASK_LEN:
            task_name = task_name[:fae._MAX_TASK_LEN].rstrip()

        # Project routing. LEX is routed + validated to LEX-scoped projects ONLY
        # (hard rail #1, carried from D-052). Non-LEX uses the smart resolver.
        if is_lex:
            project_gid = fae._resolve_lex_project(route_entity, task_name, assignee_gid, display_title)
            if not project_gid:
                skipped.append(task_name)
                continue
        else:
            project_gid = resolve_project(
                entity=meeting_entity, task_text=task_name, assignee_gid=assignee_gid,
                meeting_title=title,
            )
            if project_gid and is_blocked_project(project_gid):
                project_gid = None
            if not project_gid:
                # Symmetric with the capture guard: never orphan into My Tasks.
                skipped.append(task_name)
                continue

        if is_lex:
            notes = (
                "Created from a Lexington meeting at your request via Cora.\n"
                f"Date: {date_str}\nMeeting: {display_title}\n"
                "PHI minimized for Asana; full context in Fireflies."
            )
        else:
            notes = (
                "Created from a meeting at your request via Cora.\n"
                f"Date: {date_str}\nMeeting: {display_title}"
            )

        if dry_run:
            created.append({"task_name": task_name, "permalink_url": "", "gid": "dry-run"})
            continue

        # In-call guard: the same item selected twice in one confirm shouldn't
        # create two tasks (the project namemap was fetched before this loop).
        key = _dedup_key(task_name)
        if key in created_keys:
            already_open.append(task_name)
            continue

        # Creation-time dedup: don't double-create a task that already exists open.
        # PRIMARY (deterministic): normalized exact-name match among the TARGET
        # project's open tasks -- catches the duplicate class the typeahead missed
        # in the smoke (a push-created task identical to a pull request, both in the
        # same capture project). SECONDARY: the workspace-wide typeahead net for a
        # cross-project name match (best-effort; unreliable for long names).
        dup = _existing_open_dup(project_gid, task_name, _proj_dedup_cache)
        if not dup:
            try:
                dup = asana_client.find_recent_duplicate_task(task_name, within_days=7)
            except Exception:  # noqa: BLE001 -- dedup is best-effort; fail open
                dup = None
        if dup:
            already_open.append(task_name)
            continue

        try:
            task = asana_client.create_task(
                name=task_name, assignee_gid=assignee_gid,
                project_gid=project_gid, notes=notes,
            )
        except asana_client.AsanaClientError as exc:
            log.warning("meeting_actions create failed for %r: %s", task_name, exc)
            create_failed.append(task_name)
            continue
        gid = task.get("gid", "")
        if gid and capture_fields:
            try:
                asana_client.set_task_custom_fields(gid, capture_fields)
            except Exception as exc:  # noqa: BLE001 -- field tagging best-effort
                log.debug("meeting_actions custom-field tagging skipped: %s", exc)
        created_keys.add(key)
        created.append({"task_name": task_name, "permalink_url": task.get("permalink_url", ""), "gid": gid})

    log.info(
        "meeting_action_items CREATE asker=%s meeting=%r entity=%s created=%d already_open=%d skipped=%d not_in_meeting=%d create_failed=%d budget_hit=%s",
        slack_user_id, title, route_entity, len(created), len(already_open), len(skipped), len(not_in_meeting), len(create_failed), budget_hit,
    )

    if not created:
        if already_open and not (skipped or not_in_meeting or create_failed):
            n = len(already_open)
            return (
                f"You already have an open task for "
                f"{'that' if n == 1 else f'all {n} of those'} -- I didn't create a "
                "duplicate. Find them in Asana under your open tasks."
            )
        msg = "I wasn't able to create any of those tasks."
        if create_failed:
            msg += " (Some couldn't be saved to Asana -- please try again.)"
        if already_open:
            msg += " (Some already had an open task -- not duplicated.)"
        if not_in_meeting:
            msg += " (Some didn't match anything in that meeting's action items.)"
        if skipped:
            msg += " (Some had no project to land in.)"
        return msg

    out = [
        "WRITE_CONFIRMED -- post the following as your entire response "
        "(no preamble, no meta-commentary):",
        "",
        f"Done -- created {len(created)} task{'s' if len(created) != 1 else ''} "
        "in Asana, assigned to you:",
    ]
    for c in created:
        url = c.get("permalink_url") or ""
        link = f" <{url}|open>" if url else ""
        out.append(f"- {c['task_name']}{link}")
    tail_notes = []
    if already_open:
        tail_notes.append(f"{len(already_open)} already had an open task -- not duplicated")
    if create_failed:
        tail_notes.append(f"{len(create_failed)} couldn't be saved to Asana -- try again")
    if skipped:
        tail_notes.append(f"skipped {len(skipped)} (no project to land in)")
    if not_in_meeting:
        tail_notes.append(f"skipped {len(not_in_meeting)} that didn't match the meeting")
    if budget_hit:
        tail_notes.append("stopped early -- ask again to create the rest")
    if tail_notes:
        out.append(f"({'; '.join(tail_notes)}.)")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Tool entry point
# ---------------------------------------------------------------------------

def run_meeting_action_items(
    slack_user_id: str,
    entity: str,
    _input: dict,
    *,
    dry_run: bool = False,
) -> str:
    """Pull flow entry point. See module docstring for the full contract."""
    input_data = _input or {}
    meeting_query = str(input_data.get("meeting_query", "") or "").strip()
    transcript_id = str(input_data.get("transcript_id", "") or "").strip()
    confirmed = input_data.get("confirmed", False) is True
    raw_selected = input_data.get("selected_items")
    if isinstance(raw_selected, str):
        selected_items = [raw_selected]
    elif isinstance(raw_selected, list):
        selected_items = raw_selected
    else:
        selected_items = []  # defensive: a non-list/non-str -> treat as no selection
    # is_dm: the QA loop threads channel_name (set to "dm" for DMs at app.py),
    # but NOT channel_id -- so derive DM-ness from channel_name, with the
    # channel_id check kept as belt-and-suspenders for any caller that does pass it.
    is_dm = (
        str(input_data.get("_channel_name", "") or "").strip().lower() == "dm"
        or str(input_data.get("_channel_id", "") or "").startswith("D")
    )

    asker_emails = _asker_emails(slack_user_id)
    if not asker_emails:
        return (
            "I can't match you to a meeting attendee -- your account isn't in my "
            "Slack-to-Asana map yet. Ask Harrison to add you, then try again."
        )

    # ── CONFIRM (the staged write) ──────────────────────────────────────────
    if confirmed:
        if not transcript_id:
            return (
                "meeting_action_items: confirmed=true requires transcript_id. Run "
                "the tool first WITHOUT confirmed to load the meeting + its items, "
                "then confirm with that meeting's id."
            )
        if not selected_items:
            return (
                "No items selected. Ask the user which action items they want "
                "created, then call again with selected_items set to those task texts."
            )
        try:
            transcript = _fetch_transcript_by_id(transcript_id)
        except FirefliesConnectorError as exc:
            log.warning("meeting_actions confirm fetch failed: %s", exc)
            return "I couldn't reach the meeting service just now -- please try again shortly."
        if not transcript:
            return (
                "I couldn't re-find that meeting to confirm. Ask me for the meeting "
                "again, then choose which items to create."
            )
        # Re-verify ATTENDEE + SCOPE + LEX rails on the write path (gates run
        # before the irreversible create).
        if not _asker_attended(transcript, asker_emails, slack_user_id):
            log.info("meeting_actions confirm refused (non-attendee) asker=%s", slack_user_id)
            return "I can only create action items for meetings you attended."
        title = (transcript.get("title") or "").strip()
        meeting_entity, is_lex = _classify_meeting(transcript)
        ok, reason = _scope_ok(meeting_entity, entity, is_dm)
        if not ok:
            return reason
        lex_ok, lex_reason, scoped_entity = _lex_gate(transcript, title, meeting_entity)
        if not lex_ok:
            return lex_reason
        return _create_selected(
            slack_user_id, transcript, transcript_id, meeting_entity,
            is_lex, scoped_entity, list(selected_items), dry_run=dry_run,
        )

    # ── PREVIEW / RESOLVE (read-only) ───────────────────────────────────────
    visible: list[dict] = []  # in scope for the grounded not-found fallback below
    try:
        if transcript_id:
            transcript = _fetch_transcript_by_id(transcript_id)
            if not transcript:
                return (
                    "I couldn't load that meeting -- ask me for it again by title "
                    "or date."
                )
            transcripts = [transcript]
        else:
            window = _recent_transcripts(asker_emails)
            attended = [
                t for t in window
                if _asker_attended(t, asker_emails, slack_user_id)
            ]
            attended = _dedup_meetings(attended)
            visible = _visible_meetings(attended, entity, is_dm)
            if not meeting_query:
                # No hint: offer the asker's recent meetings that are pullable here.
                if not visible:
                    return (
                        "I don't see any meetings here that I can pull your action "
                        f"items from in the last {_WINDOW_DAYS} days. Tell me a "
                        "meeting title or date, or ask from the right channel."
                    )
                return _format_picklist(
                    visible[:_PICKLIST_CAP],
                    "Which meeting do you want your action items from? Recent meetings you attended:",
                )
            # Date/ordinal-aware so a pick-list follow-up ("June 18", "the first
            # one") resolves to the offered meeting -- title-only matching can't.
            transcripts = _resolve_meetings(meeting_query, visible)
    except FirefliesConnectorError as exc:
        log.warning("meeting_actions resolve failed: %s", exc)
        return "I couldn't reach the meeting service just now -- please try again shortly."

    if not transcripts:
        # Ground the model: when the hint doesn't resolve but the asker DOES have
        # pullable meetings here, return the real list so Cora relays actual
        # titles/dates instead of fabricating one (the 2026-06-18 "last one was
        # June 4" hallucination). Falls to a plain message only when nothing is
        # pullable here (e.g. a transcript_id miss, where `visible` is empty).
        if visible:
            return _format_picklist(
                visible[:_PICKLIST_CAP],
                f"I couldn't find a meeting matching \"{meeting_query}\" that you "
                f"attended in the last {_WINDOW_DAYS} days and can pull here. These "
                "are the ones you attended that I CAN pull from -- tell me which one "
                "(by title or date):",
            )
        return (
            f"I couldn't find a meeting you attended in the last {_WINDOW_DAYS} days "
            f"matching \"{meeting_query}\" that I can pull here. Tell me a more "
            "specific title or date, or ask from the right channel."
        )
    if len(transcripts) > 1:
        return _format_picklist(
            transcripts[:_PICKLIST_CAP],
            f"I found a few meetings matching \"{meeting_query}\" that you attended -- which one?",
        )

    transcript = transcripts[0]
    resolved_id = (transcript.get("id") or "").strip() or transcript_id

    # Attendee re-check (a transcript_id passed directly must also be one the
    # asker attended -- never let an id bypass the attendee gate).
    if not _asker_attended(transcript, asker_emails, slack_user_id):
        log.info("meeting_actions preview refused (non-attendee) asker=%s", slack_user_id)
        return "I can only pull action items for meetings you attended."

    title = (transcript.get("title") or "").strip()
    meeting_entity, is_lex = _classify_meeting(transcript)
    ok, reason = _scope_ok(meeting_entity, entity, is_dm)
    if not ok:
        return reason
    lex_ok, lex_reason, scoped_entity = _lex_gate(transcript, title, meeting_entity)
    if not lex_ok:
        return lex_reason

    action_text = ((transcript.get("summary") or {}).get("action_items") or "").strip()
    parsed = fae._parse_action_items_with_haiku(action_text) if action_text else []
    roster = fae._roster_names()
    mine, unclear = _split_candidates(parsed, _asker_name(slack_user_id), roster)

    log.info(
        "meeting_action_items PREVIEW asker=%s meeting=%r entity=%s is_lex=%s mine=%d unclear=%d",
        slack_user_id, title, meeting_entity, is_lex, len(mine), len(unclear),
    )
    return _format_preview(transcript, resolved_id, is_lex, mine, unclear)
