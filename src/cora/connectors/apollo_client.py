"""Apollo.io People Search API connector.

Used by the F3 LinkedIn Spy weekly scanner (scripts/run_linkedin_spy.py) to find
retail buyers and head executives for F3 outreach prospecting.

Endpoint: POST https://api.apollo.io/v1/mixed_people/api_search
Auth: X-Api-Key header (APOLLO_API_KEY in .env)

Rate limits (Professional plan):
    50 requests/minute | 200 requests/hour | 600 requests/day

Credit note: people search calls are FREE — no credit deduction. Credits are only
consumed when contact details (email, phone) are "revealed." This connector never
triggers reveals; it only returns publicly visible fields (title, company, linkedin_url).
"""

import logging
import os
import time
from typing import Any, Iterator

import httpx

log = logging.getLogger(__name__)

_SEARCH_ENDPOINT = "https://api.apollo.io/v1/mixed_people/api_search"
_TIMEOUT = 15.0
_MIN_REQUEST_INTERVAL = 1.3  # seconds between calls — well under 50/min limit


class ApolloClientError(Exception):
    """Raised on API errors or configuration problems."""


def _api_key() -> str:
    key = os.environ.get("APOLLO_API_KEY", "")
    if not key:
        raise ApolloClientError("APOLLO_API_KEY not set in .env — Apollo search disabled.")
    return key


def _headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "X-Api-Key": _api_key(),
    }


def search_people(
    *,
    person_titles: list[str],
    keywords: str = "",  # intentionally unused in API call -- see note below
    person_locations: list[str] | None = None,
    page: int = 1,
    per_page: int = 100,
) -> dict[str, Any]:
    """Single-page people search. Returns the raw Apollo response dict.

    Fields returned without credit cost:
        id, title, organization.name, linkedin_url, city, state, country
        (first_name / last_name come back null until a credit reveal is triggered)

    NOTE: q_keywords is NOT sent to Apollo. When combined with person_titles and
    person_locations it returns 0 results (Apollo phrase-matches against profile
    content, not as a relevance signal). Title targeting + channel_fit YAML
    provide sufficient narrowing without keywords.

    Raises ApolloClientError on HTTP or API errors.
    """
    body: dict[str, Any] = {
        "page": page,
        "per_page": min(per_page, 100),
    }
    if person_titles:
        body["person_titles"] = person_titles
    if person_locations:
        body["person_locations"] = person_locations

    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.post(_SEARCH_ENDPOINT, headers=_headers(), json=body)
    except httpx.RequestError as exc:
        raise ApolloClientError(f"Apollo network error: {exc}") from exc

    if resp.status_code == 401:
        raise ApolloClientError("Apollo API key invalid or expired (401 Unauthorized).")
    if resp.status_code == 429:
        raise ApolloClientError("Apollo rate limit hit (429). Back off and retry.")
    if not resp.is_success:
        raise ApolloClientError(f"Apollo API error {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    if data.get("status") not in ("success", None):
        raise ApolloClientError(f"Apollo returned non-success status: {data}")

    return data


def iter_people_pages(
    *,
    person_titles: list[str],
    keywords: str = "",
    person_locations: list[str] | None = None,
    per_page: int = 100,
    max_pages: int = 3,
) -> Iterator[list[dict[str, Any]]]:
    """Yield successive pages of normalized prospect dicts, respecting rate limits.

    Stops when Apollo returns an empty page or max_pages is reached.
    """
    for page_num in range(1, max_pages + 1):
        if page_num > 1:
            time.sleep(_MIN_REQUEST_INTERVAL)

        log.info(
            "apollo: fetching page %d (titles=%d)",
            page_num, len(person_titles),
        )
        try:
            data = search_people(
                person_titles=person_titles,
                keywords=keywords,
                person_locations=person_locations,
                page=page_num,
                per_page=per_page,
            )
        except ApolloClientError as exc:
            log.error("apollo: search failed on page %d — %s", page_num, exc)
            break

        people = data.get("people") or []
        if not people:
            log.info("apollo: empty page %d — stopping pagination", page_num)
            break

        total = data.get("total_entries", "?")
        log.info(
            "apollo: page %d — %d people returned (total available: %s)",
            page_num, len(people), total,
        )
        yield [_normalize_person(p) for p in people]


def _normalize_person(raw: dict[str, Any]) -> dict[str, Any]:
    """Extract fields we care about from a raw Apollo person object."""
    org = raw.get("organization") or {}
    if isinstance(org, list):
        org = org[0] if org else {}

    first = raw.get("first_name")
    last = raw.get("last_name")
    name_parts = [p for p in (first, last) if p]

    return {
        "apollo_id": raw.get("id", ""),
        "name": " ".join(name_parts) if name_parts else None,
        "title": raw.get("title") or raw.get("headline") or "",
        "company": org.get("name") or "",
        "linkedin_url": raw.get("linkedin_url") or "",
        "city": raw.get("city") or "",
        "state": raw.get("state") or "",
        "country": raw.get("country") or "US",
    }
