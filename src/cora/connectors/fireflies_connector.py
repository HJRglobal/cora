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
from dataclasses import dataclass
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


# ── Shared LEX meeting detector (WS2, 2026-06-19) ─────────────────────────────
# ONE detector, used by BOTH the KB-ingest path (backfill, below) AND the
# meeting-capture pull tool (meeting_actions). Title-only classification missed
# LEX program/client meetings with a generic title and no Lexington-domain or
# named-lead attendee -- e.g. a Lexington probation "1st Budget Class" organized
# by an @hjrglobal.com staffer (Alina) with *.maricopa.gov participants. Those
# were tagged HJRG/FNDR and ingested, exposing criminal-justice client PII
# outside LEX. This detector adds program-title / known-organizer / government-
# client-domain signals on top of the existing title + named-lead + email-domain.
#
# hard_exclude_kb = a LEX PROGRAM / CLIENT-facing / DDD / clinical / LBHS meeting.
# Per Harrison (2026-06-19) those are NEVER ingested into the KB (hard-exclude,
# not scrub). Plain LEX OPERATIONAL meetings (staff sync / ops / compliance)
# still ingest LEX-scoped, exactly as before. The capture path uses is_lex +
# sub_entity (its own gate decides capture); it still routes a program meeting's
# items LEX-scoped + PHI-scrubbed (it does not consult hard_exclude_kb).

# The LEX title keywords already maintained in _ENTITY_KEYWORDS (single source).
_LEX_TITLE_KEYWORDS: list[str] = next(
    (kw for ent, kw in _ENTITY_KEYWORDS if ent == "LEX"), []
)

# Lexington email domains -> sub-entity (most restrictive first; LBHS/Part 2 wins).
_LEX_EMAIL_DOMAINS: list[tuple[str, str]] = [
    ("lexingtonbhs.com", "LEX-LBHS"),
    ("lexingtontherapyservices.com", "LEX-LTS"),
    ("lexingtonservices.com", "LEX"),
]

# CARE/clinical-program titles that are LEX/healthcare-specific (an F3/OSN/podcast/
# HR meeting is never titled these) -> SELF-SUFFICIENT LEX signal, like DDD/clinical.
# Closes the review-2 gap: a genuine LEX care meeting run by a non-allowlisted
# @hjrglobal.com facilitator with only private-email clients (no .gov) is still caught.
_DEFAULT_LEX_CARE_TITLE_PATTERNS = [
    "hcbs", "dta", "day treatment", "anger management",
]
# Business-AMBIGUOUS program titles (these recur in F3/OSN/HR/podcast contexts) ->
# count as LEX ONLY when corroborated by a real LEX signal.
_DEFAULT_LEX_PROGRAM_TITLE_PATTERNS = [
    "budget class", "financial literacy", "financial class", "life skills",
    "day program", "parenting class", "re-entry", "reentry", "job readiness",
    "employment readiness", "independent living", "skill building",
    "skills building", "community integration", "probation", "drug court",
]
_DEFAULT_DDD_TITLE_PATTERNS = [
    "ddd", "division of developmental disabilities", "isp meeting", "isp review",
    "individual service plan", "olcr",
]
# HJRG/other-domain staff who ORGANIZE Lexington programs (the gap the email-
# domain signal misses). Counts as a LEX signal only when corroborated.
_DEFAULT_LEX_PROGRAM_ORGANIZERS = ["alina@hjrglobal.com"]
# External / government CLIENT domain suffixes. A .gov attendee is a strong
# client-facing signal but counts as LEX only when corroborated by another LEX
# signal (so an HJRG regulatory meeting with one .gov attendee is not LEX).
_DEFAULT_EXTERNAL_CLIENT_DOMAIN_SUFFIXES = [".gov"]

_LEX_DETECT_CFG_PATH = (
    Path(__file__).resolve().parents[3] / "data" / "maps" / "meeting-capture-lex-scope.yaml"
)
_lex_detect_cfg: dict | None = None


def _load_lex_detect_cfg() -> dict:
    """Additive LEX-detection lists from meeting-capture-lex-scope.yaml merged
    onto baked-in defaults. Fail-safe: defaults only on a read error."""
    global _lex_detect_cfg
    if _lex_detect_cfg is not None:
        return _lex_detect_cfg
    extra: dict = {}
    try:
        loaded = yaml.safe_load(_LEX_DETECT_CFG_PATH.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            extra = loaded
        elif loaded is not None:
            log.warning(
                "lex-detect cfg is not a mapping (%s) -- using defaults only",
                type(loaded).__name__,
            )
    except Exception as exc:  # noqa: BLE001
        log.debug("lex-detect cfg read failed (%s) -- defaults only", exc)
        extra = {}

    def _merge(key: str, default: list[str]) -> list[str]:
        vals = list(default)
        raw = extra.get(key)
        if isinstance(raw, str):
            raw = [raw]            # a bare string is ONE pattern, not a char sequence
        if not isinstance(raw, list):
            raw = []               # ignore any other shape (int/dict/None)
        for v in raw:
            if not isinstance(v, (str, int, float)):
                continue
            s = str(v).strip().lower()
            if s and s not in vals:
                vals.append(s)
        return vals

    _lex_detect_cfg = {
        "program_titles": _merge("lex_program_title_patterns", _DEFAULT_LEX_PROGRAM_TITLE_PATTERNS),
        "care_titles": _merge("lex_care_title_patterns", _DEFAULT_LEX_CARE_TITLE_PATTERNS),
        "ddd_titles": _merge("ddd_title_patterns", _DEFAULT_DDD_TITLE_PATTERNS),
        "organizers": _merge("lex_program_organizers", _DEFAULT_LEX_PROGRAM_ORGANIZERS),
        "client_domain_suffixes": _merge(
            "external_client_domains", _DEFAULT_EXTERNAL_CLIENT_DOMAIN_SUFFIXES
        ),
    }
    return _lex_detect_cfg


@dataclass(frozen=True)
class LexVerdict:
    """Result of the shared LEX meeting detector."""

    is_lex: bool
    sub_entity: str | None
    hard_exclude_kb: bool
    reason: str


def _transcript_emails(transcript: dict) -> set[str]:
    """Lowercased set of attendee + participant + organizer/host emails."""
    out: set[str] = set()
    for a in (transcript.get("meeting_attendees") or []):
        if isinstance(a, dict):
            e = (a.get("email") or "").strip().lower()
            if e:
                out.add(e)
    for p in (transcript.get("participants") or []):
        if isinstance(p, str) and p.strip():
            out.add(p.strip().lower())
    for k in ("organizer_email", "host_email"):
        e = (transcript.get(k) or "").strip().lower()
        if e:
            out.add(e)
    return out


def _lex_domain_sub(emails: set[str]) -> str | None:
    for domain, code in _LEX_EMAIL_DOMAINS:
        if any(e.endswith("@" + domain) for e in emails):
            return code
    return None


def classify_lex_meeting(transcript: dict) -> LexVerdict:
    """Single source of truth: is this a Lexington meeting, which sub-entity, and
    must it be hard-excluded from the KB? Used by BOTH ingest and capture."""
    cfg = _load_lex_detect_cfg()
    tl = (transcript.get("title") or "").lower()
    emails = _transcript_emails(transcript)
    organizer = (
        (transcript.get("organizer_email") or transcript.get("host_email") or "").strip().lower()
    )

    domain_sub = _lex_domain_sub(emails)
    try:
        name_sub = _tag_fireflies_sub_entity(transcript)
    except Exception:  # noqa: BLE001
        name_sub = None
    # title_lex: a LEX keyword in the title. "lex-" is matched at a WORD BOUNDARY
    # so it does not fire on "Duplex-"/"Complex-".
    title_lex = any(kw in tl for kw in _LEX_TITLE_KEYWORDS if kw != "lex-") or bool(
        re.search(r"\blex-", tl)
    )
    program = any(p in tl for p in cfg["program_titles"])
    care = any(p in tl for p in cfg["care_titles"])
    ddd = any(p in tl for p in cfg["ddd_titles"])
    clinical = any(p in tl for p in _PHI_TITLE_KEYWORDS)
    lex_org = bool(organizer) and organizer in cfg["organizers"]
    domains = {e.split("@", 1)[1] for e in emails if "@" in e}
    suffixes = tuple(cfg["client_domain_suffixes"])
    gov = bool(suffixes) and any(d.endswith(suffixes) for d in domains)

    # Unambiguous LEX identity: a Lexington email domain, a named LEX lead, or a
    # LEX title keyword.
    strong_id = bool(domain_sub or name_sub or title_lex)
    # DDD / clinical / care titles are LEX/healthcare-specific -> self-sufficient.
    specific_lex_title = bool(ddd or clinical or care)
    # A known LEX-program organizer hosting government/CLIENT (.gov) attendees is a
    # LEX program even with a generic title (the "Budget Class" root case).
    org_plus_gov = lex_org and gov
    # PROGRAM titles ("budget class", "financial literacy", "day program", ...) are
    # business-AMBIGUOUS, so they count ONLY when corroborated by a real LEX signal.
    # Otherwise a non-LEX "F3 Financial Class" or a podcast "Financial Literacy"
    # would be mis-classified LEX and silently hard-excluded from the KB.
    program_corroborated = program and bool(strong_id or specific_lex_title or lex_org or gov)

    is_lex = bool(strong_id or specific_lex_title or program_corroborated or org_plus_gov)
    if not is_lex:
        return LexVerdict(False, None, False, "no-lex-signal")

    signals: list[str] = []
    if domain_sub:
        signals.append("domain")
    if name_sub:
        signals.append("named-lead")
    if title_lex:
        signals.append("title")
    if care:
        signals.append("care")
    if ddd:
        signals.append("ddd")
    if clinical:
        signals.append("clinical")
    if program_corroborated:
        signals.append("program")
    if org_plus_gov:
        signals.append("organizer+client-gov")

    if domain_sub == "LEX-LBHS" or name_sub == "LEX-LBHS":
        sub: str | None = "LEX-LBHS"
    elif name_sub:
        sub = name_sub
    elif domain_sub:
        sub = domain_sub
    else:
        sub = "LEX"

    # Once known LEX, a program / DDD / clinical / government-client / LBHS meeting
    # is client-facing -> hard-exclude from the KB.
    hard = bool(program or care or ddd or clinical or gov or sub == "LEX-LBHS")
    reason = "+".join(signals) + ("|kb-exclude" if hard else "")
    return LexVerdict(True, sub, hard, reason)


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
# Tighter window for merging two transcripts that share a title+participants key but
# carry DIFFERENT links (the multi-organizer case). True copies of one meeting start
# at the same instant; this window keeps them while refusing to collapse two
# different same-titled/same-attendee meetings that merely fall inside +/-5 min (WS13).
_TITLE_MERGE_TOLERANCE_SEC = 180  # +/- 3 min
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


def _meeting_dedup_keys(transcript: dict) -> list[tuple]:
    """All identity keys a transcript can match a cluster on (WS13).

    A meeting captured by two attendees' notetakers can carry DIFFERENT
    meeting_links, so the link key alone misses that duplicate. Return BOTH:
      - ("link", meeting_link) when a link is present, AND
      - ("title", normalized_title, participant_email_set) when BOTH the title
        AND the participant set are non-empty (an empty/anonymous transcript must
        never cross-match on a degenerate ("title", "", frozenset()) key).
    Clustering treats two transcripts as the same meeting if ANY key matches
    within the time window. A transcript with neither a link nor a meaningful
    title+participants gets a unique ("solo", id) key so it clusters with nothing.
    """
    keys: list[tuple] = []
    link = (transcript.get("meeting_link") or "").strip().lower()
    if link:
        keys.append(("link", link))
    ntitle = _normalize_title(transcript.get("title"))
    pset = _participant_set(transcript)
    if ntitle and pset:
        keys.append(("title", ntitle, pset))
    if not keys:
        keys.append(("solo", transcript.get("id") or ""))
    return keys


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

    clusters: list[dict] = []  # {"keys": set[tuple], "anchor": int, "members": [dict]}
    for t in ordered:
        if (t.get("id") or "") in dropped_ids:
            continue  # idempotency: a previously-collapsed copy never resurrects
        keys = set(_meeting_dedup_keys(t))
        ts = _ts(t)
        placed = False
        for c in clusters:
            dt = abs(ts - c["anchor"])
            if dt > _DEDUP_TOLERANCE_SEC:
                continue
            overlap = keys & c["keys"]
            if not overlap:
                continue
            # A shared LINK = the same meeting instance within the window. A shared
            # TITLE+participants key WITHOUT a shared link is the multi-organizer
            # (different-link) case (WS13) — merge it only under a TIGHT window, so
            # two genuinely-DIFFERENT meetings that happen to share a generic title +
            # the same attendee set can't be collapsed (which would silently DROP one
            # from the KB). Cluster keys are the ANCHOR's only (no accumulation), so a
            # later copy can never transitively bridge via a borrowed link key.
            link_overlap = any(k[0] == "link" for k in overlap)
            if link_overlap or dt <= _TITLE_MERGE_TOLERANCE_SEC:
                c["members"].append(t)
                placed = True
                break
        if not placed:
            clusters.append({"keys": set(keys), "anchor": ts, "members": [t]})

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
    skipped_lex_excluded = 0
    for t in winners:
        title = (t.get("title") or "").strip()
        if not title:
            skipped_empty += 1
            continue

        entity = _classify_entity(title)

        # Shared LEX detector: hard-exclude LEX program/client/DDD/LBHS/clinical
        # meetings from the KB entirely (WS2). Plain LEX ops still ingest LEX-scoped.
        # On a detector error, SKIP this transcript (privacy-safe) rather than crash
        # the whole nightly sync -- a malformed transcript can't be proven non-LEX.
        try:
            lexv = classify_lex_meeting(t)
        except Exception as exc:  # noqa: BLE001
            skipped_lex_excluded += 1
            log.warning("LEX detector error on %r (%s) -- skipping to be safe", title, exc)
            continue
        if lexv.is_lex:
            if lexv.hard_exclude_kb:
                skipped_lex_excluded += 1
                log.info("LEX hard-exclude from KB: %r (%s)", title, lexv.reason)
                continue
            entity = "LEX"

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
        "Fireflies backfill done: %d transcripts yielded, %d skipped for PHI, "
        "%d LEX program/client hard-excluded from KB, %d skipped empty",
        transcript_count, skipped_phi, skipped_lex_excluded, skipped_empty,
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
