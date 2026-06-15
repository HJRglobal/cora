"""Fireflies connector — backfill + incremental sync of meeting transcripts.

What we ingest per transcript:
    - Title + date + duration
    - Attendees (names + emails)
    - Summary fields (overview, action_items, keywords, outline)
    - Full sentence-by-sentence transcript

Entity classification: title keyword matching (case-insensitive, ordered most-specific
first). Falls back to FNDR for general / founder-level / unclassifiable meetings.

PHI guardrail: LEX meetings with clinical title keywords (treatment, session note,
patient, consumer record, etc.) are skipped entirely. Operational LEX meetings
(staff syncs, compliance, ops, scheduling) ingest normally.

Auth: FIREFLIES_API_KEY in .env (with fallback to FIREFLIES_API_TOKEN / FIREFLIES_TOKEN).
"""

import json
import logging
import os
import re
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import yaml

from cora.knowledge_base.store import Document

log = logging.getLogger(__name__)

_GRAPHQL_ENDPOINT = "https://api.fireflies.ai/graphql"
_TIMEOUT = 30.0
_BATCH_SIZE = 25  # transcripts per GraphQL query (Fireflies allows up to 50)


# Entity classification: title keyword matching (case-insensitive). First match wins.
# Order matters — more specific patterns first to avoid mis-routing (e.g. "f3 pure"
# must match before "f3" alone).
_ENTITY_KEYWORDS: list[tuple[str, list[str]]] = [
    ("LEX", [
        "lexington services", "lex services", "lex llc", "lex lla", "lbhs", "lex-",
        "shaun hawkins", "jeff montgomery",
    ]),
    ("F3E", [
        "f3 pure", "f3 mood", "f3 energy", "f3e", "f3 retail", "f3 review", "f3 budget",
        "f3 amazon", "f3 sales", "blue chip", "bcb", "allen flavors", "drink labs",
        "sprouts", "whole foods",
    ]),
    ("F3C", ["f3 community", "lexington education foundation", "f3c"]),
    ("OSN", [
        "osn", "one stop nutrition", "g warner", "g mckellips", "greenfield", "val vista",
        "matt petrovich", "hayden greber",
    ]),
    ("BDM", ["bdm", "big d media", "big d"]),
    ("UFL", ["ufl", "united fight league", "unbeaten sports", "mas commercial"]),
    ("HJRP", [
        "hjrp", "hjr properties", "vitalant", "cinema lanes", "vine & branches",
        "vine and branches", "lci realty", "sharon carstens",
    ]),
    ("HJRPROD", [
        "hjrprod", "harrisonjrogers", "chokehold", "falling forward",
        "clouthub", "podcast", "hjr productions",
    ]),
    ("HJRG", [
        "hjrg", "hjr global", "visibility", "finance weekly", "tax planning",
        "andrew stubbs", "sarah bertoglio", "justin moran", "intercompany",
    ]),
]


# PHI guardrail: LEX meetings whose titles match any of these get skipped entirely.
_PHI_TITLE_KEYWORDS = {
    "treatment plan", "treatment review",
    "session note", "session review",
    "intake", "patient",
    "consumer record", "consumer review",
    "clinical assessment", "clinical review",
    "therapy session", "behavior plan", "behavioral plan",
    "case conference", "case review",
}


# Participant email/name fragments that identify a LEX sub-entity.
# Order matters: more specific sub-entities first. Shaun Hawkins is LEX-LLC GM;
# if he's the only named participant, it's an LLC meeting.
_FIREFLIES_PARTICIPANT_SUB_ENTITY: list[tuple[list[str], str]] = [
    (["justin.gilmore", "Justin Gilmore"], "LEX-LTS"),
    (["jared.harker",  "Jared Harker"],   "LEX-LBHS"),
    (["sandy.patel",   "Sandy Patel"],    "LEX-LLA"),
]
_SHAUN_IDENTIFIERS = ["shaun.hawkins", "Shaun Hawkins"]


def _tag_fireflies_sub_entity(transcript: dict) -> str | None:
    """Resolve LEX sub-entity from transcript attendee names/emails.

    Returns None for cross-sub-entity meetings (multiple matched) or when
    no sub-entity signals are present (untagged = GM-level / shared LEX content).
    """
    attendees = transcript.get("meeting_attendees") or []
    participant_text = " ".join(
        (a.get("displayName") or "") + " " + (a.get("email") or "")
        for a in attendees if isinstance(a, dict)
    ).lower()
    matched = []
    for identifiers, code in _FIREFLIES_PARTICIPANT_SUB_ENTITY:
        if any(ident.lower() in participant_text for ident in identifiers):
            matched.append(code)
    if len(matched) == 1:
        return matched[0]
    if len(matched) > 1:
        return None  # cross-sub-entity meeting — leave untagged so all LEX can see it
    if any(s.lower() in participant_text for s in _SHAUN_IDENTIFIERS):
        return "LEX-LLC"
    return None


# ── Participant → Slack ID resolution ─────────────────────────────────────────
# data/maps/slack-to-asana.yaml is the authoritative source; email_aliases
# covers cross-domain users (e.g. larry@bigd.media ↔ larry@hjrglobal.com).

_ASANA_MAP_PATH = Path(__file__).resolve().parents[3] / "data" / "maps" / "slack-to-asana.yaml"
_email_to_slack: dict[str, str] | None = None  # module-level cache


def _load_email_to_slack() -> dict[str, str]:
    """Build email→Slack ID map from slack-to-asana.yaml (loaded once per process)."""
    global _email_to_slack
    if _email_to_slack is not None:
        return _email_to_slack

    try:
        data = yaml.safe_load(_ASANA_MAP_PATH.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        log.warning("fireflies: could not load slack-to-asana.yaml: %s", exc)
        _email_to_slack = {}
        return _email_to_slack

    result: dict[str, str] = {}
    for entry in (data.get("users") or []):
        if not isinstance(entry, dict):
            continue
        sid = entry.get("slack_user_id", "").strip()
        if not sid:
            continue
        primary = entry.get("asana_email", "").strip().lower()
        if primary:
            result[primary] = sid
        for alias in (entry.get("email_aliases") or []):
            if alias:
                result[str(alias).strip().lower()] = sid

    _email_to_slack = result
    return _email_to_slack


def _resolve_participant_slack_ids(attendees: list[dict]) -> list[str]:
    """Map attendee email addresses to Slack user IDs.

    Returns a deduplicated list of resolved Slack IDs. Attendees whose
    emails don't appear in slack-to-asana.yaml are silently skipped.
    """
    email_map = _load_email_to_slack()
    seen: set[str] = set()
    slack_ids: list[str] = []
    for a in attendees:
        if not isinstance(a, dict):
            continue
        email = (a.get("email") or "").strip().lower()
        if not email:
            continue
        sid = email_map.get(email)
        if sid and sid not in seen:
            seen.add(sid)
            slack_ids.append(sid)
    return slack_ids


class FirefliesConnectorError(Exception):
    pass


def _token() -> str:
    """Read Fireflies API token from common env var names."""
    for name in ("FIREFLIES_API_KEY", "FIREFLIES_API_TOKEN", "FIREFLIES_TOKEN"):
        val = os.environ.get(name, "")
        if val:
            return val
    raise FirefliesConnectorError(
        "No Fireflies token found in env (tried FIREFLIES_API_KEY, "
        "FIREFLIES_API_TOKEN, FIREFLIES_TOKEN)"
    )


def _classify_entity(title: str) -> str:
    """Classify a meeting to an entity code by title keywords. Defaults to FNDR."""
    title_lower = title.lower()
    for entity, keywords in _ENTITY_KEYWORDS:
        for kw in keywords:
            if kw in title_lower:
                return entity
    return "FNDR"


def _is_phi_meeting(title: str, entity: str) -> bool:
    """Return True if this LEX meeting should be excluded for PHI reasons."""
    if entity != "LEX":
        return False
    title_lower = title.lower()
    return any(kw in title_lower for kw in _PHI_TITLE_KEYWORDS)


def _graphql_query(query: str, variables: dict | None = None) -> dict:
    """POST a GraphQL query to Fireflies. Raises FirefliesConnectorError on failure."""
    headers = {
        "Authorization": f"Bearer {_token()}",
        "Content-Type": "application/json",
    }
    body = {"query": query, "variables": variables or {}}
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.post(_GRAPHQL_ENDPOINT, headers=headers, json=body)
    except httpx.RequestError as exc:
        raise FirefliesConnectorError(f"Fireflies network error: {exc}") from exc

    if r.status_code == 401:
        raise FirefliesConnectorError("Fireflies 401 — API key invalid or revoked")
    if r.status_code == 429:
        log.warning("Fireflies 429 rate-limited; sleeping 10s")
        time.sleep(10)
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.post(_GRAPHQL_ENDPOINT, headers=headers, json=body)
    if r.status_code >= 500:
        raise FirefliesConnectorError(f"Fireflies {r.status_code}: {r.text[:200]}")
    if r.status_code != 200:
        raise FirefliesConnectorError(f"Fireflies {r.status_code}: {r.text[:200]}")

    data = r.json()
    if "errors" in data:
        raise FirefliesConnectorError(f"Fireflies GraphQL errors: {data['errors']}")
    return data.get("data", {}) or {}


# GraphQL query — pulls everything we need for a Document
_TRANSCRIPTS_QUERY = """
query Transcripts($limit: Int, $skip: Int, $fromDate: DateTime, $toDate: DateTime) {
  transcripts(limit: $limit, skip: $skip, fromDate: $fromDate, toDate: $toDate) {
    id
    title
    date
    duration
    transcript_url
    meeting_link
    organizer_email
    host_email
    participants
    summary {
      overview
      keywords
      action_items
      outline
      shorthand_bullet
      bullet_gist
      gist
      short_summary
    }
    sentences {
      index
      speaker_name
      text
    }
    meeting_attendees {
      displayName
      email
    }
  }
}
"""


def _parse_date(date_field) -> int | None:
    """Fireflies returns date as Unix ms timestamp OR ISO string. Normalize to seconds."""
    if date_field is None:
        return None
    if isinstance(date_field, (int, float)):
        # Heuristic: if > year 3000 in seconds, it's milliseconds
        ts = int(date_field)
        if ts > 32503680000:  # year 3000 in seconds
            ts = ts // 1000
        return ts
    if isinstance(date_field, str):
        try:
            return int(datetime.fromisoformat(date_field.replace("Z", "+00:00")).timestamp())
        except Exception:
            return None
    return None


# ── Duplicate-meeting dedup (2026-06-14) ──────────────────────────────────────
# Org-wide Fireflies rollout means multiple attendees' notetakers each capture
# the SAME meeting -> near-identical transcripts ingested as separate KB rows
# (different transcript ids -> different (source, source_id) -> separate rows).
# This inflates chunk counts and double-feeds friction-mining. We collapse these
# at ingest, keyed on (meeting_link, start_time) within a tolerance window,
# keeping the most-complete transcript. A persistent ledger records which ids
# collapsed into which canonical so re-running sync never resurrects a dropped
# duplicate (idempotent).

_DEDUP_TOLERANCE_SEC = 300  # +/- 5 min: notetaker copies of one meeting cluster here
_DEDUP_LEDGER_PATH = Path(__file__).resolve().parents[3] / "data" / "state" / "fireflies-dedup-ledger.json"
_DEDUP_LEDGER_MAX = 5000  # cap entries (only duplicated meetings get one)


def _normalize_title(title: str | None) -> str:
    """Lowercase + collapse whitespace for the title-based fallback key."""
    return re.sub(r"\s+", " ", (title or "").strip().lower())


def _participant_set(transcript: dict) -> frozenset[str]:
    """Lowercased attendee-email set (for the title-based fallback key)."""
    return frozenset(
        (a.get("email") or "").strip().lower()
        for a in (transcript.get("meeting_attendees") or [])
        if isinstance(a, dict) and (a.get("email") or "").strip()
    )


def _meeting_dedup_key(transcript: dict) -> tuple:
    """Identity key for grouping duplicate copies of the same meeting.

    Primary: ("link", meeting_link). Fallback when no meeting_link:
    ("title", normalized_title, participant_email_set). The start-time window
    (applied separately) distinguishes recurring meetings that reuse one link.
    """
    link = (transcript.get("meeting_link") or "").strip().lower()
    if link:
        return ("link", link)
    return ("title", _normalize_title(transcript.get("title")), _participant_set(transcript))


def _transcript_completeness(transcript: dict) -> tuple[int, int, int]:
    """Completeness proxy: (sentence count, summary length, title length).

    Higher = more complete. Used to pick the canonical copy among duplicates.
    """
    sentences = transcript.get("sentences") or []
    summary = transcript.get("summary") or {}
    overview = summary.get("overview") or ""
    action_items = summary.get("action_items") or ""
    return (len(sentences), len(overview) + len(action_items), len(transcript.get("title") or ""))


def _ledger_key(transcript: dict) -> str:
    """Stable string ledger key derived from the canonical transcript.

    Buckets the start time to the tolerance window so the same canonical maps
    to the same ledger key across runs (its start time is fixed).
    """
    key = _meeting_dedup_key(transcript)
    bucket = (_parse_date(transcript.get("date")) or 0) // _DEDUP_TOLERANCE_SEC
    if key[0] == "link":
        return f"link::{key[1]}::{bucket}"
    return f"title::{key[1]}::{bucket}::{'|'.join(sorted(key[2]))}"


def _read_dedup_ledger() -> dict:
    """Load the dedup ledger (audit + idempotency). Empty dict on any error."""
    try:
        if _DEDUP_LEDGER_PATH.exists():
            data = json.loads(_DEDUP_LEDGER_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as exc:
        log.warning("Could not read fireflies dedup ledger: %s", exc)
    return {}


def _write_dedup_ledger(ledger: dict) -> None:
    """Persist the dedup ledger, capped to the most-recently-updated entries."""
    try:
        _DEDUP_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        if len(ledger) > _DEDUP_LEDGER_MAX:
            # Keep the most recently updated entries (bounded growth).
            kept = sorted(
                ledger.items(), key=lambda kv: kv[1].get("updated", 0), reverse=True
            )[:_DEDUP_LEDGER_MAX]
            ledger = dict(kept)
        _DEDUP_LEDGER_PATH.write_text(json.dumps(ledger, indent=2), encoding="utf-8")
    except Exception as exc:
        log.error("Could not write fireflies dedup ledger: %s", exc)


def _dedup_transcripts(transcripts: list[dict], ledger: dict) -> tuple[list[dict], dict, int]:
    """Collapse duplicate-meeting transcripts.

    Returns (winners, updated_ledger, collapsed_count). Idempotent: any id
    already recorded as collapsed in a prior run is dropped immediately and
    never re-yielded. Among the remaining copies of one meeting (same key +
    within the time window) the most-complete transcript is kept; the rest are
    recorded as collapsed.
    """
    dropped_ids: set[str] = set()
    for entry in ledger.values():
        for cid in (entry.get("collapsed_ids") or []):
            dropped_ids.add(cid)

    def _ts(t: dict) -> int:
        return _parse_date(t.get("date")) or 0

    # Deterministic order so greedy windowing + tiebreaks are reproducible.
    ordered = sorted(
        [t for t in transcripts if t.get("id")],
        key=lambda t: (_ts(t), t.get("id") or ""),
    )

    clusters: list[dict] = []  # {"key": tuple, "anchor": int, "members": [dict]}
    for t in ordered:
        if (t.get("id") or "") in dropped_ids:
            continue  # idempotency: a previously-collapsed copy never resurrects
        key = _meeting_dedup_key(t)
        ts = _ts(t)
        placed = False
        for c in clusters:
            if c["key"] == key and abs(ts - c["anchor"]) <= _DEDUP_TOLERANCE_SEC:
                c["members"].append(t)
                placed = True
                break
        if not placed:
            clusters.append({"key": key, "anchor": ts, "members": [t]})

    def _winner_sort_key(t: dict):
        comp = _transcript_completeness(t)
        # most-complete first; deterministic smallest-id tiebreak
        return (-comp[0], -comp[1], -comp[2], (t.get("id") or ""))

    winners: list[dict] = []
    collapsed_count = 0
    new_ledger = dict(ledger)
    now = int(time.time())

    for c in clusters:
        members = c["members"]
        winner = min(members, key=_winner_sort_key)
        winner_id = winner.get("id") or ""
        winners.append(winner)
        losers = [m for m in members if (m.get("id") or "") != winner_id]
        if not losers:
            continue
        collapsed_count += len(losers)
        lkey = _ledger_key(winner)
        entry = new_ledger.get(lkey) or {"canonical_id": winner_id, "collapsed_ids": []}
        if not entry.get("canonical_id"):
            entry["canonical_id"] = winner_id
        merged = set(entry.get("collapsed_ids") or [])
        for m in losers:
            merged.add(m.get("id") or "")
        entry["collapsed_ids"] = sorted(merged)
        entry["updated"] = now
        new_ledger[lkey] = entry
        log.info(
            "Fireflies dedup: meeting %r -> kept %s, collapsed %d copy(ies): %s",
            (winner.get("title") or "")[:60], winner_id, len(losers),
            ",".join(m.get("id") or "" for m in losers),
        )

    return winners, new_ledger, collapsed_count


def _format_transcript_content(t: dict) -> str:
    """Build the chunkable content string from a transcript."""
    lines: list[str] = []
    title = t.get("title") or "(untitled meeting)"

    # Date (formatted for human readability)
    date_ts = _parse_date(t.get("date"))
    date_str = (
        datetime.fromtimestamp(date_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        if date_ts else "(no date)"
    )

    duration_sec = t.get("duration") or 0
    duration_min = int(duration_sec / 60) if duration_sec else 0

    lines.append(f"[Fireflies Meeting] {title}")
    lines.append(f"Date: {date_str}")
    if duration_min:
        lines.append(f"Duration: {duration_min} min")

    # Attendees
    attendees = t.get("meeting_attendees") or []
    if attendees:
        names = []
        for a in attendees:
            name = (a.get("displayName") or "").strip() or (a.get("email") or "").strip()
            if name:
                names.append(name)
        if names:
            lines.append(f"Attendees: {', '.join(names)}")

    # Summary blocks
    summary = t.get("summary") or {}
    overview = (summary.get("overview") or "").strip()
    if overview:
        lines.append("")
        lines.append("Overview:")
        lines.append(overview)

    action_items = (summary.get("action_items") or "").strip()
    if action_items:
        lines.append("")
        lines.append("Action Items:")
        lines.append(action_items)

    keywords = summary.get("keywords")
    if keywords:
        kw_str = ", ".join(keywords) if isinstance(keywords, list) else str(keywords)
        if kw_str.strip():
            lines.append("")
            lines.append(f"Keywords: {kw_str}")

    outline = (summary.get("outline") or "").strip()
    if outline:
        lines.append("")
        lines.append("Outline:")
        lines.append(outline)

    # Full transcript text — sentence by sentence
    sentences = t.get("sentences") or []
    if sentences:
        lines.append("")
        lines.append("Transcript:")
        for s in sentences:
            speaker = (s.get("speaker_name") or "").strip() or "Speaker"
            text = (s.get("text") or "").strip()
            if text:
                lines.append(f"[{speaker}] {text}")

    return "\n".join(lines)


def backfill(since: datetime) -> Iterator[Document]:
    """Yield Documents for all transcripts since the given datetime.

    Paginates through Fireflies via skip/limit. PHI-suspicious LEX meetings are
    skipped entirely; everything else is classified by title and yielded.
    """
    # Fireflies expects ISO 8601 with timezone
    from_date = since.replace(tzinfo=timezone.utc).isoformat() if since.tzinfo is None else since.isoformat()
    to_date = datetime.now(timezone.utc).isoformat()

    # Phase 1: paginate the full window into memory. Dedup is a run-level
    # operation (duplicate copies of one meeting can land on different pages),
    # so we collect all transcripts before grouping. Matches the existing
    # all-transcripts pattern in run_action_capture.
    all_transcripts: list[dict] = []
    skip = 0
    while True:
        variables = {
            "limit": _BATCH_SIZE,
            "skip": skip,
            "fromDate": from_date,
            "toDate": to_date,
        }
        log.info("Fireflies query: skip=%d limit=%d", skip, _BATCH_SIZE)
        try:
            data = _graphql_query(_TRANSCRIPTS_QUERY, variables)
        except FirefliesConnectorError as exc:
            log.error("Fireflies query failed at skip=%d: %s", skip, exc)
            raise

        transcripts = data.get("transcripts") or []
        if not transcripts:
            break
        all_transcripts.extend(transcripts)
        if len(transcripts) < _BATCH_SIZE:
            break
        skip += _BATCH_SIZE
        time.sleep(0.5)  # gentle pause between paginated requests

    # Phase 2: collapse duplicate-meeting transcripts (multiple notetakers),
    # keeping the most-complete copy. Persist the ledger for audit + so a
    # re-run never resurrects a dropped duplicate.
    ledger = _read_dedup_ledger()
    winners, ledger, collapsed = _dedup_transcripts(all_transcripts, ledger)
    _write_dedup_ledger(ledger)
    if collapsed:
        log.info(
            "Fireflies dedup: %d transcripts -> %d unique meetings (%d duplicate copies collapsed)",
            len(all_transcripts), len(winners), collapsed,
        )

    # Phase 3: filter (empty title / PHI / empty content) + yield the winners.
    transcript_count = 0
    skipped_phi = 0
    skipped_empty = 0
    for t in winners:
        title = (t.get("title") or "").strip()
        if not title:
            skipped_empty += 1
            continue

        entity = _classify_entity(title)

        if _is_phi_meeting(title, entity):
            skipped_phi += 1
            log.info("PHI guardrail: skipping LEX meeting %r", title)
            continue

        content = _format_transcript_content(t)
        if not content.strip():
            skipped_empty += 1
            continue

        transcript_id = t.get("id", "")
        if not transcript_id:
            skipped_empty += 1
            continue

        meeting_ts = _parse_date(t.get("date"))
        permalink = t.get("transcript_url") or f"https://app.fireflies.ai/view/{transcript_id}"
        sub_entity = _tag_fireflies_sub_entity(t) if entity == "LEX" else None

        meeting_attendees = t.get("meeting_attendees") or []
        yield Document(
            source="fireflies",
            source_id=transcript_id,
            entity=entity,
            sub_entity=sub_entity,
            content=content,
            date_created=meeting_ts,
            date_modified=meeting_ts,
            author=(t.get("organizer_email") or t.get("host_email") or ""),
            title=title,
            deep_link=f"<{permalink}|{title}>",
            metadata={
                "transcript_id": transcript_id,
                "duration_sec": t.get("duration"),
                "attendee_emails": [
                    (a.get("email") or "")
                    for a in meeting_attendees if isinstance(a, dict)
                ],
                "participant_slack_ids": _resolve_participant_slack_ids(meeting_attendees),
                "participants": t.get("participants") or [],
            },
        )
        transcript_count += 1

    log.info(
        "Fireflies backfill done: %d transcripts yielded, %d skipped for PHI, %d skipped empty",
        transcript_count, skipped_phi, skipped_empty,
    )


def sync_delta(last_sync_ts: int) -> Iterator[Document]:
    """Pull transcripts modified since the last sync timestamp."""
    since_dt = datetime.fromtimestamp(last_sync_ts, tz=timezone.utc)
    yield from backfill(since=since_dt)


# ── DWD coverage monitoring (team membership + per-host recency probe) ─────────
# Used by fireflies_coverage.py / run_fireflies_coverage.py to verify every DWD
# user's meetings are actually being captured (not just Harrison's). Reads only
# membership / transcript counts / recency — never titles or content — so the
# PHI guardrail is not engaged here.

# Admin-only query (verified live 2026-06-08; Harrison is workspace admin).
# Field set confirmed against the live schema: see CP-1 in the relay.
_USERS_QUERY = """
query Users {
  users {
    user_id
    email
    name
    num_transcripts
    minutes_consumed
    recent_transcript
    recent_meeting
    is_admin
    integrations
  }
}
"""


def list_team_members() -> list[dict]:
    """Enumerate Fireflies workspace members (admin `users` query).

    Returns a normalized list of dicts:
        {email, name, num_transcripts, minutes_consumed, integrations, is_admin}

    `email` is lowercased for downstream case-insensitive matching. `num_transcripts`
    is normalized to an int (the API returns null for members who have never recorded).
    Raises FirefliesConnectorError if the query fails or `users` is not permitted —
    callers (coverage monitor) treat that as "could not enumerate" and degrade gracefully.
    """
    data = _graphql_query(_USERS_QUERY)
    raw = data.get("users") or []
    members: list[dict] = []
    for u in raw:
        if not isinstance(u, dict):
            continue
        members.append(
            {
                "email": (u.get("email") or "").strip().lower(),
                "name": (u.get("name") or "").strip(),
                "num_transcripts": int(u.get("num_transcripts") or 0),
                "minutes_consumed": u.get("minutes_consumed"),
                "integrations": u.get("integrations") or [],
                "is_admin": bool(u.get("is_admin")),
            }
        )
    return members


# Per-host recency probe. NOTE: organizer_email is a single String (the build plan's
# `organizers: [String]` was wrong — rejected by the live schema; corrected at CP-1).
_RECENT_HOST_QUERY = """
query RecentHost($org: String, $fromDate: DateTime, $toDate: DateTime) {
  transcripts(organizer_email: $org, fromDate: $fromDate, toDate: $toDate, limit: 1) {
    id
    title
    date
    organizer_email
  }
}
"""


def has_recent_host_meeting(email: str, days: int = 30) -> bool:
    """Return True if `email` HOSTED (organized) a meeting in the last `days`.

    CORRECTNESS NOTE: this reflects "a meeting this email organized was recorded",
    which only happens if someone with a connected calendar was in the room — it is
    NOT proof that this person's own calendar is connected. The coverage classifier
    uses it ONLY to refine people who are already Fireflies members; it must never
    promote a non-member to COVERED. Raises FirefliesConnectorError on API failure.
    """
    now = datetime.now(timezone.utc)
    from_iso = (now - timedelta(days=days)).isoformat()
    to_iso = now.isoformat()
    data = _graphql_query(
        _RECENT_HOST_QUERY,
        {"org": (email or "").strip().lower(), "fromDate": from_iso, "toDate": to_iso},
    )
    return bool(data.get("transcripts") or [])
