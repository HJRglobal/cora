"""Otterly.AI public-API client -- the Google AI Overviews (AIO) source.

The 4 direct model surfaces (ChatGPT / Perplexity / Gemini-grounded / Claude)
cannot return Google AI Overviews. Otterly's public API can, so this client
pulls the AIO slice (brand coverage / share-of-voice / rank / sentiment /
citations) for each F3 brand report and hands it to the scorer as a 5th engine
(source-tag ``aio_otterly``). Our own direct scans stay authoritative for the 4
engines; Otterly is used ONLY for the AIO slice it uniquely provides.

API: base ``https://data.otterly.ai/v1``, Bearer auth (``OTTERLY_API_KEY``).
Rate limit 2,000 req / 5 min -- a full pull is only a handful of report calls,
so no throttle is needed. The key is scoped to a SINGLE workspace; if a second
workspace is ever added the key must be regenerated (see build prereqs).

Fail-soft contract: a missing key, an unmatched report, or any API error
returns an ``AioBrandSlice(available=False, error=...)`` -- it never raises, so
a dead Otterly never crashes a scan (the card just notes AIO is missing).

PHI guard OFF: F3 marketing/visibility data only.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)

_BASE_URL = os.environ.get("OTTERLY_BASE_URL", "https://data.otterly.ai/v1")
_HTTP_TIMEOUT = float(os.environ.get("OTTERLY_HTTP_TIMEOUT", "30"))
# Fallback AIO engine id if /engines discovery fails. Discovery (by matching a
# name containing "overview") is preferred and overrides this.
_AIO_ENGINE_FALLBACK = os.environ.get("OTTERLY_AIO_ENGINE", "google_ai_overviews")

ENGINE_TAG = "aio_otterly"


class OtterlyError(RuntimeError):
    """Raised internally on an API problem; callers get a fail-soft slice instead."""


@dataclass
class OtterlyCitation:
    url: str
    domain: str
    category: str = ""  # Otterly domainCategory (raw); canonical source_type is set later
    is_my_brand: bool = False


@dataclass
class AioBrandSlice:
    """Normalized Google-AI-Overviews slice for one brand, from Otterly."""

    brand_key: str
    report_id: str = ""
    report_title: str = ""
    presence: float | None = None          # brandCoverage, normalized to 0..100
    share_of_voice: float | None = None    # normalized to 0..100
    average_rank: float | None = None
    total_mentions: int = 0
    sentiment: dict | None = None          # {positive, neutral, negative, nss}
    competitor_mentions: dict[str, int] = field(default_factory=dict)
    citations: list[OtterlyCitation] = field(default_factory=list)
    engine: str = ENGINE_TAG
    available: bool = True
    error: str | None = None


def _key() -> str:
    return os.environ.get("OTTERLY_API_KEY", "")


def _get(path: str, params: dict | None = None) -> dict:
    """GET {base}{path} with Bearer auth; raise_for_status; return JSON.

    The single httpx seam -- tests monkeypatch this.
    """
    key = _key()
    if not key:
        raise OtterlyError("OTTERLY_API_KEY not set")
    url = f"{_BASE_URL}{path}"
    with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
        resp = client.get(url, headers={"Authorization": f"Bearer {key}"}, params=params or {})
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def list_engines() -> list[dict]:
    data = _get("/engines")
    return data.get("items") or data.get("engines") or (data if isinstance(data, list) else [])


def resolve_aio_engine_id() -> str:
    """Find the Google-AI-Overviews engine id. Prefer live discovery (match a
    name/id containing 'overview'); fall back to the configured default."""
    try:
        for eng in list_engines():
            if not isinstance(eng, dict):
                continue
            hay = " ".join(str(eng.get(k, "")) for k in ("id", "name", "slug", "key")).lower()
            if "overview" in hay:
                return str(eng.get("id") or eng.get("slug") or eng.get("key") or _AIO_ENGINE_FALLBACK)
    except Exception as exc:  # noqa: BLE001 -- discovery is best-effort
        log.warning("otterly: engine discovery failed (%s); using fallback %s",
                    exc, _AIO_ENGINE_FALLBACK)
    return _AIO_ENGINE_FALLBACK


def list_brand_reports(workspace_id: str | None = None) -> list[dict]:
    params = {"workspaceId": workspace_id} if workspace_id else None
    data = _get("/reports/brand", params=params)
    return data.get("items") or (data if isinstance(data, list) else [])


# ---------------------------------------------------------------------------
# Normalization helpers (pure)
# ---------------------------------------------------------------------------
def _norm_percent(value) -> float | None:
    """Normalize a coverage/share metric to 0..100. Otterly returns some as a
    0..1 fraction and some as a 0..100 percent; treat <=1.0 as a fraction."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v < 0:
        return 0.0
    return round(v * 100, 2) if v <= 1.0 else round(v, 2)


def _domain_of(url: str) -> str:
    try:
        host = urlsplit(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:  # noqa: BLE001
        return ""


def _name_matches(name: str, aliases: tuple[str, ...] | list[str]) -> bool:
    n = (name or "").strip().lower()
    if not n:
        return False
    return any(n == a.strip().lower() or a.strip().lower() in n or n in a.strip().lower()
               for a in aliases if a)


def _match_report_for_brand(reports: list[dict], aliases) -> dict | None:
    """Pick the brand report whose brand / brandVariations best match our aliases."""
    for rep in reports:
        if not isinstance(rep, dict):
            continue
        candidates = [str(rep.get("brand") or ""), str(rep.get("reportTitle") or "")]
        candidates.extend(str(v) for v in (rep.get("brandVariations") or []))
        if any(_name_matches(c, aliases) for c in candidates):
            return rep
    return None


def _extract_brand_metrics(stats: dict, aliases) -> dict:
    """Pull the target brand's coverage/sov/rank/mentions/sentiment out of a
    /stats payload, tolerant of which sub-object carries them."""
    summary = stats.get("summary") or {}
    out: dict = {
        "presence": _norm_percent(summary.get("brandCoverage")),
        "share_of_voice": _norm_percent(summary.get("shareOfVoice")),
        "average_rank": summary.get("averageRank") if summary.get("averageRank") is not None
        else summary.get("averagePosition"),
        "total_mentions": int(summary.get("totalMentions") or 0),
        "sentiment": None,
    }
    # Search every brandMentions array for the target brand's richer per-brand row.
    for section in ("allBrandsAnalysis", "competitorBrandsAnalysis"):
        block = stats.get(section) or {}
        for bm in block.get("brandMentions") or []:
            if not isinstance(bm, dict):
                continue
            if _name_matches(str(bm.get("brand") or bm.get("name") or ""), aliases):
                if out["presence"] is None and bm.get("brandCoverage") is not None:
                    out["presence"] = _norm_percent(bm.get("brandCoverage"))
                if out["share_of_voice"] is None and bm.get("shareOfVoice") is not None:
                    out["share_of_voice"] = _norm_percent(bm.get("shareOfVoice"))
                if out["average_rank"] is None and bm.get("rank") is not None:
                    out["average_rank"] = bm.get("rank")
                if bm.get("sentiment"):
                    out["sentiment"] = bm.get("sentiment")
    try:
        out["average_rank"] = float(out["average_rank"]) if out["average_rank"] is not None else None
    except (TypeError, ValueError):
        out["average_rank"] = None
    return out


def _extract_competitor_mentions(stats: dict, aliases) -> dict[str, int]:
    """Map competitor brand -> mention count from the stats brandMentions rows."""
    out: dict[str, int] = {}
    for section in ("competitorBrandsAnalysis", "allBrandsAnalysis"):
        block = stats.get(section) or {}
        for bm in block.get("brandMentions") or []:
            if not isinstance(bm, dict):
                continue
            name = str(bm.get("brand") or bm.get("name") or "").strip()
            if not name or _name_matches(name, aliases):
                continue  # skip the target brand itself
            try:
                out[name] = int(bm.get("mentions") or bm.get("totalMentions") or 0)
            except (TypeError, ValueError):
                out[name] = 0
    # detectedBrands is a flatter fallback if the analysis blocks were empty.
    if not out:
        for db in stats.get("detectedBrands") or []:
            if isinstance(db, dict):
                name = str(db.get("name") or db.get("brand") or "").strip()
                if name and not _name_matches(name, aliases):
                    try:
                        out[name] = int(db.get("mentions") or 0)
                    except (TypeError, ValueError):
                        out[name] = 0
    return out


def _extract_citations(payload: dict) -> list[OtterlyCitation]:
    out: list[OtterlyCitation] = []
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        out.append(OtterlyCitation(
            url=url,
            domain=str(item.get("domain") or _domain_of(url)),
            category=str(item.get("domainCategory") or ""),
            is_my_brand=bool(item.get("isMyBrandDomain")),
        ))
    return out


# ---------------------------------------------------------------------------
# Top-level fetch (fail-soft)
# ---------------------------------------------------------------------------
def fetch_aio_slice(brand_key: str, aliases, *, start_date: str, end_date: str,
                    country: str = "us", workspace_id: str | None = None,
                    engine: str | None = None) -> AioBrandSlice:
    """Fetch the AIO slice for one brand. Never raises; unavailable on any problem."""
    if not _key():
        return AioBrandSlice(brand_key=brand_key, available=False,
                             error="OTTERLY_API_KEY not set")
    try:
        eng = engine or resolve_aio_engine_id()
        reports = list_brand_reports(workspace_id)
        rep = _match_report_for_brand(reports, aliases)
        if rep is None:
            return AioBrandSlice(brand_key=brand_key, available=False,
                                 error=f"no Otterly brand report matched {list(aliases)}")
        report_id = str(rep.get("id") or "")
        stats = get_brand_report_stats(report_id, start_date, end_date, country, eng)
        metrics = _extract_brand_metrics(stats, aliases)
        comp = _extract_competitor_mentions(stats, aliases)
        try:
            cites = _extract_citations(get_brand_report_citations(report_id, country))
        except Exception as exc:  # noqa: BLE001 -- citations are optional
            log.warning("otterly: citations fetch failed for %s (%s)", report_id, exc)
            cites = []
        return AioBrandSlice(
            brand_key=brand_key,
            report_id=report_id,
            report_title=str(rep.get("reportTitle") or ""),
            presence=metrics["presence"],
            share_of_voice=metrics["share_of_voice"],
            average_rank=metrics["average_rank"],
            total_mentions=metrics["total_mentions"],
            sentiment=metrics["sentiment"],
            competitor_mentions=comp,
            citations=cites,
        )
    except Exception as exc:  # noqa: BLE001 -- fail-soft
        log.warning("otterly: AIO fetch failed for brand %s (%s)", brand_key, exc)
        return AioBrandSlice(brand_key=brand_key, available=False, error=str(exc))


def get_brand_report_stats(report_id: str, start_date: str, end_date: str,
                           country: str, engine: str) -> dict:
    return _get(
        f"/reports/brand/{report_id}/stats",
        params={"startDate": start_date, "endDate": end_date,
                "country": country, "engines": engine},
    )


def get_brand_report_citations(report_id: str, country: str = "us", limit: int = 100) -> dict:
    return _get(
        f"/reports/brand/{report_id}/citations",
        params={"country": country, "limit": limit, "offset": 0},
    )
