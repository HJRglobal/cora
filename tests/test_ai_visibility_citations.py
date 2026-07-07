"""Tests for citation verification + source-type tagging (network mocked)."""

from __future__ import annotations

import pytest

from cora.ai_visibility import citations as cit

COMPETITORS = ("Red Bull", "Monster", "Celsius", "Alani Nu", "Ghost", "C4", "Magic Mind")


# ---------------------------------------------------------------------------
# Source-type tagging
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("url,expected", [
    ("https://f3energy.com/products/energy", "own_site"),
    ("https://www.f3mood.com", "own_site"),
    ("https://www.reddit.com/r/energydrinks/x", "reddit"),
    ("https://en.wikipedia.org/wiki/Energy_drink", "wikipedia"),
    ("https://youtu.be/abc123", "youtube"),
    ("https://www.youtube.com/watch?v=x", "youtube"),
    ("https://www.trustpilot.com/review/celsius", "review"),
    ("https://www.healthline.com/nutrition/energy-drinks", "news"),
    ("https://www.celsius.com/products", "competitor"),
    ("https://www.redbull.com/us-en/energydrink", "competitor"),
    ("https://somesite.com/best-energy-drinks-2026", "listicle"),
    ("https://randomblog.io/my-post", "other"),
])
def test_classify_source_type(url, expected):
    assert cit.classify_source_type(url, competitor_names=COMPETITORS) == expected


def test_competitor_tiny_token_not_matched():
    # "C4" -> token "c4" (len 2) must NOT match arbitrary domains.
    assert cit.classify_source_type("https://arc4systems.com/x",
                                    competitor_names=COMPETITORS) != "competitor"


def test_own_site_beats_review_substring():
    # own-site precedence even if path contains 'review'
    assert cit.classify_source_type("https://f3energy.com/reviews",
                                    competitor_names=COMPETITORS) == "own_site"


def test_domain_of():
    assert cit.domain_of("https://www.Healthline.com/a") == "healthline.com"
    assert cit.domain_of("https://sub.reddit.com:443/x") == "sub.reddit.com"
    assert cit.domain_of("not a url") == ""


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------
def test_verify_url_status_interpretation(monkeypatch):
    statuses = {
        "https://ok.com": 200,
        "https://redir.com": 301,
        "https://blocked.com": 403,       # present-but-blocked -> keep
        "https://ratelimited.com": 429,   # keep
        "https://gone.com": 404,          # phantom -> drop
        "https://err.com": 410,           # phantom -> drop
        "https://dns-fail.com": None,     # network failure -> drop
    }
    monkeypatch.setattr(cit, "_probe", lambda url, timeout: statuses[url])
    assert cit.verify_url("https://ok.com") is True
    assert cit.verify_url("https://redir.com") is True
    assert cit.verify_url("https://blocked.com") is True
    assert cit.verify_url("https://ratelimited.com") is True
    assert cit.verify_url("https://gone.com") is False
    assert cit.verify_url("https://err.com") is False
    assert cit.verify_url("https://dns-fail.com") is False


def test_verify_url_rejects_non_http():
    assert cit.verify_url("ftp://x.com") is False
    assert cit.verify_url("") is False


def test_resolve_citations_drops_phantoms_and_tags(monkeypatch):
    statuses = {
        "https://f3energy.com": 200,
        "https://www.celsius.com/x": 200,
        "https://healthline.com/best-energy-drinks": 200,
        "https://phantom.com/made-up": 404,
    }
    monkeypatch.setattr(cit, "_probe", lambda url, timeout: statuses.get(url))
    out = cit.resolve_citations(
        list(statuses.keys()) + ["https://f3energy.com"],  # dup dropped
        competitor_names=COMPETITORS,
    )
    urls = [c.url for c in out]
    assert "https://phantom.com/made-up" not in urls  # phantom dropped
    assert len(out) == 3  # dedup + phantom drop
    by_url = {c.url: c for c in out}
    assert by_url["https://f3energy.com"].source_type == "own_site"
    assert by_url["https://www.celsius.com/x"].source_type == "competitor"
    assert by_url["https://healthline.com/best-energy-drinks"].source_type == "news"
    assert all(c.resolved for c in out)


def test_resolve_citations_verify_off_keeps_all(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("must not probe when verify=False")

    monkeypatch.setattr(cit, "_probe", _boom)
    out = cit.resolve_citations(
        ["https://f3energy.com", "https://x.com"], competitor_names=COMPETITORS, verify=False
    )
    assert len(out) == 2


def test_verify_urls_concurrent(monkeypatch):
    monkeypatch.setattr(cit, "_probe",
                        lambda url, timeout: 200 if "good" in url else 404)
    res = cit.verify_urls(["https://good1.com", "https://bad.com", "https://good2.com"])
    assert res == {"https://good1.com": True, "https://bad.com": False,
                   "https://good2.com": True}
