"""Tests for the Otterly AIO client (all network mocked via the _get seam)."""

from __future__ import annotations

import pytest

from cora.connectors import otterly_client as oc


ENERGY_ALIASES = ("F3 Energy", "F3Energy", "F3 energy drink")


def _fake_get(routes: dict):
    """Return a _get replacement that dispatches on path prefix."""
    def _get(path, params=None):
        for prefix, payload in routes.items():
            if path.startswith(prefix):
                if isinstance(payload, Exception):
                    raise payload
                return payload
        raise oc.OtterlyError(f"unrouted path {path}")
    return _get


def test_norm_percent_scales():
    assert oc._norm_percent(0.42) == 42.0     # fraction -> percent
    assert oc._norm_percent(42) == 42.0       # already percent
    assert oc._norm_percent(0) == 0.0
    assert oc._norm_percent(None) is None
    assert oc._norm_percent("x") is None


def test_domain_of_strips_www():
    assert oc._domain_of("https://www.healthline.com/a/b") == "healthline.com"
    assert oc._domain_of("https://reddit.com/r/x") == "reddit.com"


def test_match_report_by_alias():
    reports = [
        {"id": "r1", "brand": "Some Other Drink", "brandVariations": []},
        {"id": "r2", "brand": "F3 Energy", "brandVariations": ["F3Energy"]},
    ]
    rep = oc._match_report_for_brand(reports, ENERGY_ALIASES)
    assert rep and rep["id"] == "r2"


def test_extract_brand_metrics_from_summary_and_analysis():
    stats = {
        "summary": {"brandCoverage": 0.30, "shareOfVoice": 0.12, "averageRank": 2.5,
                    "totalMentions": 9},
        "allBrandsAnalysis": {"brandMentions": [
            {"brand": "F3 Energy", "sentiment": {"positive": 4, "neutral": 3,
                                                 "negative": 1, "nss": 40}},
        ]},
    }
    m = oc._extract_brand_metrics(stats, ENERGY_ALIASES)
    assert m["presence"] == 30.0
    assert m["share_of_voice"] == 12.0
    assert m["average_rank"] == 2.5
    assert m["total_mentions"] == 9
    assert m["sentiment"]["nss"] == 40


def test_extract_competitor_mentions_skips_target():
    stats = {"competitorBrandsAnalysis": {"brandMentions": [
        {"brand": "Celsius", "mentions": 20},
        {"brand": "F3 Energy", "mentions": 9},   # target -> skipped
        {"brand": "Ghost", "mentions": 5},
    ]}}
    comp = oc._extract_competitor_mentions(stats, ENERGY_ALIASES)
    assert comp == {"Celsius": 20, "Ghost": 5}


def test_extract_citations():
    payload = {"items": [
        {"url": "https://healthline.com/x", "domain": "healthline.com",
         "domainCategory": "News/Media", "isMyBrandDomain": False},
        {"url": "https://f3energy.com", "isMyBrandDomain": True},
        {"no_url": True},
    ]}
    cites = oc._extract_citations(payload)
    assert len(cites) == 2
    assert cites[0].category == "News/Media"
    assert cites[1].domain == "f3energy.com" and cites[1].is_my_brand is True


def test_fetch_aio_slice_missing_key(monkeypatch):
    monkeypatch.delenv("OTTERLY_API_KEY", raising=False)
    s = oc.fetch_aio_slice("energy", ENERGY_ALIASES, start_date="2026-06-30",
                           end_date="2026-07-06")
    assert s.available is False
    assert "OTTERLY_API_KEY" in (s.error or "")


def test_fetch_aio_slice_happy_path(monkeypatch):
    monkeypatch.setenv("OTTERLY_API_KEY", "otk-test")
    routes = {
        "/engines": {"items": [
            {"id": "chatgpt", "name": "ChatGPT"},
            {"id": "google_ai_overviews", "name": "Google AI Overviews"},
        ]},
        "/reports/brand/r2/stats": {
            "summary": {"brandCoverage": 0.25, "shareOfVoice": 0.10, "averageRank": 3,
                        "totalMentions": 5},
            "competitorBrandsAnalysis": {"brandMentions": [
                {"brand": "Celsius", "mentions": 15},
            ]},
        },
        "/reports/brand/r2/citations": {"items": [
            {"url": "https://reddit.com/r/energy", "domainCategory": "Social Media"},
        ]},
        "/reports/brand": {"items": [
            {"id": "r2", "brand": "F3 Energy", "brandVariations": ["F3Energy"]},
        ]},
    }
    monkeypatch.setattr(oc, "_get", _fake_get(routes))
    s = oc.fetch_aio_slice("energy", ENERGY_ALIASES, start_date="2026-06-30",
                           end_date="2026-07-06")
    assert s.available is True
    assert s.report_id == "r2"
    assert s.presence == 25.0
    assert s.share_of_voice == 10.0
    assert s.average_rank == 3.0
    assert s.competitor_mentions == {"Celsius": 15}
    assert len(s.citations) == 1
    assert s.engine == "aio_otterly"


def test_fetch_aio_slice_no_matching_report(monkeypatch):
    monkeypatch.setenv("OTTERLY_API_KEY", "otk-test")
    routes = {
        "/engines": {"items": [{"id": "google_ai_overviews", "name": "Google AI Overviews"}]},
        "/reports/brand": {"items": [{"id": "rX", "brand": "Unrelated"}]},
    }
    monkeypatch.setattr(oc, "_get", _fake_get(routes))
    s = oc.fetch_aio_slice("energy", ENERGY_ALIASES, start_date="a", end_date="b")
    assert s.available is False
    assert "no Otterly brand report matched" in (s.error or "")


def test_fetch_aio_slice_api_error_is_failsoft(monkeypatch):
    monkeypatch.setenv("OTTERLY_API_KEY", "otk-test")
    routes = {
        "/engines": {"items": [{"id": "google_ai_overviews", "name": "Google AI Overviews"}]},
        "/reports/brand": RuntimeError("otterly 500"),
    }
    monkeypatch.setattr(oc, "_get", _fake_get(routes))
    s = oc.fetch_aio_slice("energy", ENERGY_ALIASES, start_date="a", end_date="b")
    assert s.available is False
    assert "otterly 500" in (s.error or "")


def test_resolve_aio_engine_discovery_and_fallback(monkeypatch):
    monkeypatch.setenv("OTTERLY_API_KEY", "otk-test")
    monkeypatch.setattr(oc, "_get", _fake_get(
        {"/engines": {"items": [{"id": "aio-x", "name": "The AI Overview engine"}]}}
    ))
    assert oc.resolve_aio_engine_id() == "aio-x"

    def _boom(*a, **k):
        raise RuntimeError("engines down")

    monkeypatch.setattr(oc, "_get", _boom)
    assert oc.resolve_aio_engine_id() == oc._AIO_ENGINE_FALLBACK
