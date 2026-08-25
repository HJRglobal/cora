"""Klaviyo read client -- READ-ONLY BY CONSTRUCTION (C14, cq-118f8bbf842e).

WHY READ-ONLY IS STRUCTURAL AND NOT A PREFERENCE. `Ops Dept OS v1` (2026-08-08,
"P1 -- Klaviyo email consolidation", Hard guardrails) makes BOTH contact-list
deletion AND billing cleanup UNAUTHORIZED. The kickoff for this slice quoted only
the deletion half; the file forbids both. So a Klaviyo module that merely *avoids*
calling a write endpoint is not enough -- the guardrail has to survive a later
refactor by someone who has not read the Ops OS. It is enforced the way
`deposco_client` enforces its Phase-1 invariant: this module has exactly ONE
request primitive, `_get`, which passes a literal GET verb to httpx, no
other HTTP verb string appears anywhere in the file, and
`tests/test_klaviyo_audit.py` greps this source to keep it that way. Adding a
suppress/delete call is therefore a visible, reviewable act.

WHAT THE CHARGE BASIS ACTUALLY IS, and why it is derived rather than read.
Klaviyo exposes NO billing, plan, seat, invoice or team-member endpoint -- there
is no "what do we pay" call to make. What it does expose, at O(1), is a segment's
`profile_count`. So the charge basis is derived from the population that a
segment's own DEFINITION says is marketable, and the audit reports the definition
alongside the number so the derivation is auditable rather than asserted. The
same absence means the SEAT half of this audit cannot come from the API at all:
seats live in canon (`data/maps/org-roles.yaml`, `Ops Dept OS v1`) and
`klaviyo_audit` reads them from there.

ABSENT IS NEVER ZERO. With no `KLAVIYO_API_KEY` this module is DARK:
`configured()` returns False and every read returns None. It never raises on the
unconfigured path and it never returns an empty collection that a caller could
render as "0 profiles" -- a Klaviyo account showing zero marketable profiles
would read as a cancelled account, and that is exactly the blank-radar failure
`finance-renewal-radar.yaml` warns about in its own header.

THE API REVISION IS PINNED AND UNVERIFIED. Klaviyo's API is date-versioned via a
mandatory `revision` header. `_REVISION` below has NOT been exercised against the
live API from this process -- there is no credential in `.env` to do it with (the
same blocker `cq-44645e3f79a3` recorded on 2026-08-18). The first credentialed
run must therefore CONFIRM it: an unknown revision is an HTTP error, which
`_get` surfaces as None plus a WARNING naming the revision, so the failure is
legible instead of looking like an empty account.

CREDENTIALS NEVER LEAVE THIS MODULE. The key is read from the environment into a
private header, never logged, never interpolated into a URL or a query string,
and `_scrub` strips it from anything this module logs or raises.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

log = logging.getLogger(__name__)

_BASE = "https://a.klaviyo.com/api"

#: Klaviyo's mandatory date-versioned API revision. UNVERIFIED against the live
#: API -- see the module docstring. Bump only alongside a real credentialed run.
_REVISION = "2026-04-15"

_TIMEOUT_SEC = 20.0

#: A Klaviyo private key is `pk_` + hex. Scrubbed from any log/exception text.
_KEY_RE = re.compile(r"pk_[A-Za-z0-9]{4,}")


def _key() -> str:
    """Read per call, never snapshotted at import.

    A module-level constant reading os.environ is the `cq-06f4797db4f1` class:
    the value is frozen at bot start, so adding the credential to `.env` would
    appear to do nothing until a restart, and it silently defeats test isolation.
    """
    return (os.environ.get("KLAVIYO_API_KEY") or "").strip()


def configured() -> bool:
    """True when a credential is present. Callers branch on THIS, not on a
    falsy read result -- "unconfigured" and "configured but empty" are different
    facts and the audit renders them differently."""
    return bool(_key())


def _scrub(text: str) -> str:
    return _KEY_RE.sub("pk_<redacted>", str(text))


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Klaviyo-API-Key {_key()}",
        "revision": _REVISION,
        "accept": "application/vnd.api+json",
    }


def _get(path: str, params: dict[str, Any] | None = None,
         *, transport: Any = None) -> dict | None:
    """THE ONLY request primitive. Returns the parsed JSON body, or None on any
    failure (unconfigured, transport error, non-2xx, unparseable body).

    None is deliberately indistinguishable-by-type from "no data": every caller
    in `klaviyo_audit` treats None as UNKNOWN and says so in the report. What is
    NOT allowed is turning a failure into a zero.
    """
    if not configured():
        return None
    try:
        import httpx  # noqa: PLC0415 -- keep import cost off the bot's startup path
    except Exception:  # noqa: BLE001
        log.warning("klaviyo_client: httpx unavailable -- read skipped")
        return None
    url = f"{_BASE}{path}"
    try:
        kwargs: dict[str, Any] = {"timeout": _TIMEOUT_SEC}
        if transport is not None:
            kwargs["transport"] = transport
        with httpx.Client(**kwargs) as client:
            resp = client.request("GET", url, headers=_headers(), params=params or {})
        if resp.status_code >= 300:
            # Name the revision: an unknown/retired revision is the single most
            # likely first-run failure and it must not read as an empty account.
            log.warning("klaviyo_client: %s -> HTTP %s (revision=%s) %s",
                        path, resp.status_code, _REVISION,
                        _scrub(resp.text)[:300])
            return None
        return resp.json()
    except Exception as exc:  # noqa: BLE001 -- a read must never raise into a report
        log.warning("klaviyo_client: %s failed: %s", path, _scrub(str(exc)))
        return None


def _data(body: dict | None) -> list[dict]:
    """The JSON:API `data` list, normalised. A single-resource response wraps one
    dict; a collection wraps a list. Anything else yields []."""
    if not isinstance(body, dict):
        return []
    payload = body.get("data")
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return [d for d in payload if isinstance(d, dict)]
    return []


def get_account(*, transport: Any = None) -> dict | None:
    """Account attributes (organization name, industry, timezone). None when
    unconfigured or on failure.

    NOTE for anyone extending this: there is no plan, tier, seat or invoice field
    on this resource. Klaviyo does not expose one. Do not add a caller that
    expects to read the bill from here.
    """
    rows = _data(_get("/accounts", transport=transport))
    return rows[0] if rows else None


#: Pages to follow before giving up. 10 pages x 10 segments is far above any
#: plausible account, and a bound means a broken cursor cannot loop forever.
_MAX_PAGES = 10


def get_segments(*, transport: Any = None) -> list[dict] | None:
    """Every segment, each with its `definition` AND its `profile_count`.

    TWO CORRECTIONS THE REVIEW FORCED, both of which would have produced a
    confidently wrong report rather than an error:

    1. `profile_count` IS NOT AVAILABLE ON THE COLLECTION ENDPOINT. It is an
       additional-field on the single-segment resource only. Asking `/segments`
       for it returns segments with NO count, so `segment_profile_count` returned
       None for every one and the audit's headline number -- the whole charge
       basis -- could never be read. So: list first (cheap, definitions included),
       then GET each segment for its count. N is ~9 here.
    2. IT PAGINATED ONE PAGE AND CLAIMED "All segments". `page[size]` maxes at 10,
       so an account with more segments was silently truncated -- and a truncated
       segment list can hide the very segment that IS the billable population,
       which reads as "no charge basis could be derived" rather than as an error.
       Now follows `links.next` up to `_MAX_PAGES`.

    Returns None (not []) when unconfigured or on failure, so the caller can
    distinguish "we could not look" from "this account has no segments".
    """
    listed: list[dict] = []
    params: dict[str, Any] | None = {
        "fields[segment]": "name,definition,is_active,is_processing",
        "page[size]": "10",
    }
    cursor = ""
    for _ in range(_MAX_PAGES):
        page_params = dict(params or {})
        if cursor:
            page_params["page[cursor]"] = cursor
        body = _get("/segments", page_params, transport=transport)
        if body is None:
            # A mid-pagination failure must not return a PARTIAL list that reads
            # as complete -- that is the silent-truncation failure again.
            return None if not listed else None
        listed.extend(_data(body))
        cursor = _next_cursor(body)
        if not cursor:
            break

    # Second pass for the counts, which only the single-resource endpoint carries.
    out: list[dict] = []
    for seg in listed:
        sid = segment_id(seg)
        detailed = get_segment(sid, transport=transport) if sid else None
        # Fall back to the listed row rather than dropping the segment: a failed
        # count read leaves profile_count absent, which the audit renders as
        # "unknown" -- never as zero.
        out.append(detailed or seg)
    return out


def _next_cursor(body: dict | None) -> str:
    """The cursor from a JSON:API `links.next`, or ''."""
    if not isinstance(body, dict):
        return ""
    nxt = (body.get("links") or {}).get("next")
    if not isinstance(nxt, str) or not nxt:
        return ""
    try:
        from urllib.parse import parse_qs, urlparse  # noqa: PLC0415
        return (parse_qs(urlparse(nxt).query).get("page[cursor]") or [""])[0]
    except Exception:  # noqa: BLE001
        return ""


def get_segment(segment_id: str, *, transport: Any = None) -> dict | None:
    """One segment with its `profile_count` and `definition`."""
    sid = str(segment_id or "").strip()
    if not sid:
        return None
    rows = _data(_get(
        f"/segments/{sid}",
        {
            "additional-fields[segment]": "profile_count",
            "fields[segment]": "name,definition,profile_count,is_active,is_processing",
        },
        transport=transport,
    ))
    return rows[0] if rows else None


def segment_profile_count(segment: dict | None) -> int | None:
    """A segment's profile count, or None when the field is absent.

    None rather than 0: Klaviyo omits `profile_count` unless it is explicitly
    requested as an additional field, so a missing key means "not asked for",
    which is not the same as an empty segment.
    """
    if not isinstance(segment, dict):
        return None
    value = (segment.get("attributes") or {}).get("profile_count")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def segment_name(segment: dict | None) -> str:
    if not isinstance(segment, dict):
        return ""
    return str((segment.get("attributes") or {}).get("name") or "").strip()


def segment_id(segment: dict | None) -> str:
    if not isinstance(segment, dict):
        return ""
    return str(segment.get("id") or "").strip()


def marketing_consent_channels(segment: dict | None) -> tuple[str, ...]:
    """The marketing channels a segment's DEFINITION gates on ('email', 'sms').

    This is what makes the derived charge basis auditable: the audit reports "N
    profiles, and here is the consent condition the segment used to select them"
    rather than asserting that some named segment happens to be the billable
    population. Walks the condition tree defensively -- a definition shape we do
    not recognise yields () and the audit says the basis is unverifiable.
    """
    if not isinstance(segment, dict):
        return ()
    definition = (segment.get("attributes") or {}).get("definition")
    if not isinstance(definition, dict):
        return ()
    found: list[str] = []
    for group in definition.get("condition_groups") or []:
        if not isinstance(group, dict):
            continue
        for cond in group.get("conditions") or []:
            if not isinstance(cond, dict):
                continue
            if cond.get("type") != "profile-marketing-consent":
                continue
            consent = cond.get("consent")
            if not isinstance(consent, dict):
                continue
            channel = str(consent.get("channel") or "").strip().lower()
            if channel and channel not in found:
                found.append(channel)
    return tuple(found)


def is_marketable_definition(segment: dict | None) -> bool:
    """True when the segment selects on `can_receive_marketing: true`.

    The billable population is the one that CAN be marketed to. A segment that
    merely mentions a consent channel (e.g. "subscription: any") is not the same
    thing, so this checks the flag rather than the channel's presence.
    """
    if not isinstance(segment, dict):
        return False
    definition = (segment.get("attributes") or {}).get("definition")
    if not isinstance(definition, dict):
        return False
    for group in definition.get("condition_groups") or []:
        if not isinstance(group, dict):
            continue
        for cond in group.get("conditions") or []:
            if not isinstance(cond, dict) or cond.get("type") != "profile-marketing-consent":
                continue
            consent = cond.get("consent")
            if isinstance(consent, dict) and consent.get("can_receive_marketing") is True:
                return True
    return False


def subscription_status(segment: dict | None) -> str:
    """The `consent_status.subscription` a marketable segment gates on.

    'subscribed' is the strict billable population. 'any' is BROADER -- it admits
    profiles that can receive marketing without being actively subscribed -- and
    the audit must not present the two as the same count. Measured live on
    2026-08-25: "All Subscribed" gates on 'subscribed' (4,464) while "Never
    Opened (Email)" gates on 'any' (2,367), so subtracting one from the other
    would be arithmetic across two different populations.
    """
    if not isinstance(segment, dict):
        return ""
    definition = (segment.get("attributes") or {}).get("definition")
    if not isinstance(definition, dict):
        return ""
    for group in definition.get("condition_groups") or []:
        if not isinstance(group, dict):
            continue
        for cond in group.get("conditions") or []:
            if not isinstance(cond, dict) or cond.get("type") != "profile-marketing-consent":
                continue
            consent = cond.get("consent")
            if not isinstance(consent, dict):
                continue
            status = consent.get("consent_status")
            if isinstance(status, dict):
                sub = str(status.get("subscription") or "").strip().lower()
                if sub:
                    return sub
    return ""
