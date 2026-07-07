"""Citation verification + source-type tagging.

AI answers hallucinate 3-13% of their citations, so every cited URL is
resolve-checked (HEAD, falling back to GET) and phantom URLs are dropped before
storage. Survivors are tagged by source type -- own_site | reddit | wikipedia |
youtube | review | listicle | news | competitor | other -- which is the seed for
the Phase-2 gap->content loop (which prompts surface competitors, and which
sources they cite). Do NOT build Phase 2 here; just tag cleanly.

Verification is deliberately lenient about "present but blocked" (401/403/405/
429/999): those hosts exist and the path is plausibly real, so they are kept --
only clear phantoms (404/410, DNS/connection failure, timeout) are dropped.

PHI guard OFF: marketing/visibility data only.
"""

from __future__ import annotations

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)

# The F3 first-party web properties. The runner may extend per brand.
F3_OWN_DOMAINS: frozenset[str] = frozenset(
    {"f3energy.com", "f3pure.com", "f3mood.com", "drinkf3.com", "drinkf3energy.com"}
)

_REDDIT = ("reddit.com", "redd.it")
_WIKI = ("wikipedia.org", "wikimedia.org")
_YOUTUBE = ("youtube.com", "youtu.be")
_REVIEW = ("trustpilot.com", "influenster.com", "sitejabber.com", "productreview.com.au",
           "amazon.com", "yelp.com")
# A curated (non-exhaustive) news / editorial-media set common to beverage queries.
_NEWS = ("nytimes.com", "cnn.com", "forbes.com", "businessinsider.com", "bloomberg.com",
         "reuters.com", "theguardian.com", "washingtonpost.com", "usatoday.com", "wsj.com",
         "healthline.com", "menshealth.com", "womenshealthmag.com", "eatthis.com",
         "verywellfit.com", "verywellhealth.com", "medicalnewstoday.com", "today.com",
         "cnet.com", "techcrunch.com", "self.com", "shape.com", "prevention.com",
         "goodhousekeeping.com", "delish.com")

_HTTP_TIMEOUT = float(os.environ.get("AI_VIS_CITATION_TIMEOUT", "6"))
_VERIFY_WORKERS = max(1, int(os.environ.get("AI_VIS_CITATION_WORKERS", "8")))
# Present-but-blocked statuses: the host exists; keep the citation.
_BLOCKED_OK = frozenset({401, 403, 405, 406, 429, 999})
_UA = "Mozilla/5.0 (compatible; CoraAIVisibility/1.0; +https://hjrglobal.com)"

_LISTICLE_RE = re.compile(r"(?:^|[/\-])(best|top|vs|versus|roundup|buying-guide|guide)(?:[/\-]|$)")


@dataclass
class Citation:
    url: str
    domain: str
    resolved: bool
    source_type: str


def domain_of(url: str) -> str:
    try:
        host = urlsplit(url).netloc.lower().split(":")[0]
        return host[4:] if host.startswith("www.") else host
    except Exception:  # noqa: BLE001
        return ""


def _host_matches(domain: str, suffixes) -> bool:
    return any(domain == s or domain.endswith("." + s) for s in suffixes)


def _competitor_token(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def classify_source_type(url: str, *, own_domains=F3_OWN_DOMAINS,
                         competitor_names: tuple[str, ...] = ()) -> str:
    """Best-effort source-type tag from the URL alone (Phase-2 seed)."""
    domain = domain_of(url)
    if not domain:
        return "other"
    if _host_matches(domain, own_domains):
        return "own_site"
    # Competitor-owned domain (token >= 4 chars to avoid tiny-token false hits).
    flat = domain.replace(".", "")
    for name in competitor_names:
        tok = _competitor_token(name)
        if len(tok) >= 4 and tok in flat:
            return "competitor"
    if _host_matches(domain, _WIKI):
        return "wikipedia"
    if _host_matches(domain, _REDDIT):
        return "reddit"
    if _host_matches(domain, _YOUTUBE):
        return "youtube"
    path = urlsplit(url).path.lower()
    if _host_matches(domain, _REVIEW) or "review" in path or "review" in domain:
        return "review"
    if _host_matches(domain, _NEWS):
        return "news"
    if _LISTICLE_RE.search(path):
        return "listicle"
    return "other"


# ---------------------------------------------------------------------------
# Verification (network seam is _probe)
# ---------------------------------------------------------------------------
def _probe(url: str, timeout: float) -> int | None:
    """HEAD (fallback GET) the URL. Return the HTTP status, or None on a network
    failure (DNS/connect/timeout). The single network seam -- mocked in tests."""
    headers = {"User-Agent": _UA}
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as c:
            try:
                r = c.head(url)
                if r.status_code == 405 or r.status_code == 501:
                    r = c.get(url)
                return r.status_code
            except httpx.HTTPError:
                r = c.get(url)
                return r.status_code
    except httpx.HTTPError:
        return None
    except Exception:  # noqa: BLE001 -- any unexpected error = treat as unverifiable
        return None


def verify_url(url: str, *, timeout: float = _HTTP_TIMEOUT) -> bool:
    """True if the URL plausibly resolves (2xx/3xx or a present-but-blocked
    status); False for a clear phantom (404/410, DNS/connect failure)."""
    if not url or not url.lower().startswith(("http://", "https://")):
        return False
    status = _probe(url, timeout)
    if status is None:
        return False
    return status < 400 or status in _BLOCKED_OK


def verify_urls(urls, *, timeout: float = _HTTP_TIMEOUT) -> dict[str, bool]:
    """Concurrently verify a list of URLs -> {url: resolves}."""
    uniq = list(dict.fromkeys(u for u in urls if u))
    if not uniq:
        return {}
    results: dict[str, bool] = {}
    with ThreadPoolExecutor(max_workers=_VERIFY_WORKERS) as pool:
        for url, ok in zip(uniq, pool.map(lambda u: verify_url(u, timeout=timeout), uniq)):
            results[url] = ok
    return results


def resolve_citations(urls, *, own_domains=F3_OWN_DOMAINS,
                      competitor_names: tuple[str, ...] = (), verify: bool = True,
                      timeout: float = _HTTP_TIMEOUT) -> list[Citation]:
    """Verify (unless verify=False) + tag citations; drop phantoms.

    Returns only the survivors, each tagged by source type. Logs how many
    phantom URLs were dropped.
    """
    uniq = list(dict.fromkeys(u for u in urls if u and str(u).strip()))
    verdicts = verify_urls(uniq, timeout=timeout) if verify else {u: True for u in uniq}
    kept: list[Citation] = []
    dropped = 0
    for url in uniq:
        if not verdicts.get(url, False):
            dropped += 1
            continue
        kept.append(Citation(
            url=url,
            domain=domain_of(url),
            resolved=True,
            source_type=classify_source_type(
                url, own_domains=own_domains, competitor_names=competitor_names
            ),
        ))
    if dropped:
        log.info("ai_visibility citations: kept %d, dropped %d phantom(s) of %d",
                 len(kept), dropped, len(uniq))
    return kept
