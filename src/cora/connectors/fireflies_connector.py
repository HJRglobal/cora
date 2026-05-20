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

import logging
import os
import time
from collections.abc import Iterator
from datetime import datetime, timezone

import httpx

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

    skip = 0
    transcript_count = 0
    skipped_phi = 0
    skipped_empty = 0

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

        for t in transcripts:
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

            yield Document(
                source="fireflies",
                source_id=transcript_id,
                entity=entity,
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
                        a.get("email", "") for a in (t.get("meeting_attendees") or [])
                    ],
                    "participants": t.get("participants") or [],
                },
            )
            transcript_count += 1

        if len(transcripts) < _BATCH_SIZE:
            break
        skip += _BATCH_SIZE
        time.sleep(0.5)  # gentle pause between paginated requests

    log.info(
        "Fireflies backfill done: %d transcripts yielded, %d skipped for PHI, %d skipped empty",
        transcript_count, skipped_phi, skipped_empty,
    )


def sync_delta(last_sync_ts: int) -> Iterator[Document]:
    """Pull transcripts modified since the last sync timestamp."""
    since_dt = datetime.fromtimestamp(last_sync_ts, tz=timezone.utc)
    yield from backfill(since=since_dt)
