"""Tests for the grounded AI-search connector (all network mocked).

Covers per-provider parsing + citation cleaning, run_query retry/fail-soft/cost,
and the missing-key skip path. No real API calls are ever made.
"""

from __future__ import annotations

import types

import httpx
import pytest

from cora.connectors import ai_search as ai


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------
def test_parse_perplexity_flat_citations_and_search_results():
    raw = {
        "choices": [{"message": {"content": "F3 Energy is a solid pick."}}],
        "citations": ["https://f3energy.com", "not-a-url", "https://f3energy.com"],
        "search_results": [{"title": "Review", "url": "https://reddit.com/r/energy"}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 40},
    }
    r = ai._parse_perplexity(raw)
    assert r.text.startswith("F3 Energy")
    # dedup + non-url dropped, order preserved, search_results merged
    assert r.citations == ["https://f3energy.com", "https://reddit.com/r/energy"]
    assert r.input_tokens == 12 and r.output_tokens == 40
    assert r.num_searches == 1


def test_parse_gemini_grounding_metadata():
    raw = {
        "candidates": [{
            "content": {"parts": [{"text": "Top picks: "}, {"text": "Celsius, Ghost."}]},
            "groundingMetadata": {
                "webSearchQueries": ["best energy drink 2026"],
                "groundingChunks": [
                    {"web": {"uri": "https://healthline.com/x", "title": "Healthline"}},
                    {"web": {"uri": "https://example.com"}},
                ],
            },
        }],
        "usageMetadata": {"promptTokenCount": 8, "candidatesTokenCount": 25},
    }
    r = ai._parse_gemini(raw)
    assert "Celsius" in r.text
    assert r.citations == ["https://healthline.com/x", "https://example.com"]
    assert r.num_searches == 1
    assert r.input_tokens == 8 and r.output_tokens == 25


def test_parse_gemini_no_grounding_zero_searches():
    raw = {"candidates": [{"content": {"parts": [{"text": "hi"}]}}]}
    r = ai._parse_gemini(raw)
    assert r.num_searches == 0
    assert r.citations == []


def test_parse_openai_responses_object():
    usage = types.SimpleNamespace(input_tokens=30, output_tokens=120)
    ann = types.SimpleNamespace(type="url_citation", url="https://f3energy.com/pure")
    msg_block = types.SimpleNamespace(annotations=[ann])
    message_item = types.SimpleNamespace(type="message", content=[msg_block])
    search_item = types.SimpleNamespace(type="web_search_call", content=None)
    resp = types.SimpleNamespace(
        output_text="Try F3 Pure.",
        output=[search_item, message_item],
        usage=usage,
    )
    r = ai._parse_openai(resp)
    assert r.text == "Try F3 Pure."
    assert r.citations == ["https://f3energy.com/pure"]
    assert r.num_searches == 1
    assert r.input_tokens == 30 and r.output_tokens == 120


def test_parse_claude_web_blocks():
    text_block = types.SimpleNamespace(
        type="text", text="F3 Mood helps with calm focus.",
        citations=[types.SimpleNamespace(url="https://f3mood.com")],
    )
    tool_result = types.SimpleNamespace(
        type="web_search_tool_result",
        content=[types.SimpleNamespace(url="https://recess.com"),
                 types.SimpleNamespace(url="https://f3mood.com")],
    )
    usage = types.SimpleNamespace(
        input_tokens=50, output_tokens=200,
        server_tool_use=types.SimpleNamespace(web_search_requests=2),
    )
    resp = types.SimpleNamespace(content=[text_block, tool_result], usage=usage)
    r = ai._parse_claude(resp)
    assert "F3 Mood" in r.text
    assert r.citations == ["https://f3mood.com", "https://recess.com"]
    assert r.num_searches == 2
    assert r.input_tokens == 50 and r.output_tokens == 200


def test_clean_citations_filters_and_dedupes():
    assert ai._clean_citations(
        ["https://a.com", "ftp://x", "", None, "https://a.com", "http://b.com"]
    ) == ["https://a.com", "http://b.com"]


# ---------------------------------------------------------------------------
# run_query wrapper
# ---------------------------------------------------------------------------
def test_run_query_missing_key_is_skipped_not_error(monkeypatch):
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)

    def _boom(*a, **k):
        raise AssertionError("network must not be called when key is missing")

    monkeypatch.setattr(ai, "_post_json", _boom)
    r = ai.run_query("perplexity_sonar", "best energy drink?")
    assert r.skipped is True
    assert r.ok is False
    assert "PERPLEXITY_API_KEY" in (r.error or "")


def test_run_query_success_computes_and_logs_cost(monkeypatch):
    monkeypatch.setenv("PERPLEXITY_API_KEY", "pk-test")
    monkeypatch.setattr(ai, "_post_json", lambda *a, **k: {
        "choices": [{"message": {"content": "F3 Energy."}}],
        "citations": ["https://f3energy.com"],
        "usage": {"prompt_tokens": 10, "completion_tokens": 50},
    })
    r = ai.run_query("perplexity_sonar", "best energy drink?")
    assert r.ok
    assert r.text == "F3 Energy."
    assert r.citations == ["https://f3energy.com"]
    assert r.cost_usd > 0
    assert r.prompt == "best energy drink?"


def test_run_query_retries_transient_then_succeeds(monkeypatch):
    monkeypatch.setenv("PERPLEXITY_API_KEY", "pk-test")
    monkeypatch.setattr(ai.time, "sleep", lambda *_a, **_k: None)
    calls = {"n": 0}

    def _flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom")
        return {"choices": [{"message": {"content": "ok"}}], "usage": {}}

    monkeypatch.setattr(ai, "_post_json", _flaky)
    r = ai.run_query("perplexity_sonar", "q")
    assert r.ok
    assert calls["n"] == 2  # one retry


def test_run_query_permanent_error_returns_error_not_raise(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gk")
    monkeypatch.setattr(ai.time, "sleep", lambda *_a, **_k: None)

    def _bad(*a, **k):
        raise ValueError("permanent bad request")

    monkeypatch.setattr(ai, "_post_json", _bad)
    r = ai.run_query("gemini_grounding", "q")
    assert r.ok is False
    assert r.skipped is False
    assert "permanent bad request" in (r.error or "")


def test_run_query_exhausts_retries_on_persistent_429(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gk")
    monkeypatch.setattr(ai.time, "sleep", lambda *_a, **_k: None)
    calls = {"n": 0}
    req = httpx.Request("POST", "https://x")

    def _rate_limited(*a, **k):
        calls["n"] += 1
        raise httpx.HTTPStatusError(
            "429", request=req, response=httpx.Response(429, request=req)
        )

    monkeypatch.setattr(ai, "_post_json", _rate_limited)
    r = ai.run_query("gemini_grounding", "q")
    assert r.ok is False
    assert calls["n"] == len(ai._RETRY_DELAYS) + 1  # initial + all retries


def test_run_query_unknown_model():
    r = ai.run_query("mystery_model", "q")
    assert r.ok is False
    assert "unknown model" in (r.error or "")


def test_is_retryable_classification():
    req = httpx.Request("GET", "https://x")
    assert ai._is_retryable(httpx.ConnectError("x")) is True
    assert ai._is_retryable(
        httpx.HTTPStatusError("", request=req, response=httpx.Response(503, request=req))
    ) is True
    assert ai._is_retryable(
        httpx.HTTPStatusError("", request=req, response=httpx.Response(400, request=req))
    ) is False
    assert ai._is_retryable(ValueError("bad json")) is False
    assert ai._is_retryable(RuntimeError("Server overloaded, try again")) is True


def test_redact_scrubs_keys_query_params_and_bearer(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyABCDEFGH_secretvalue123")
    text = ("HTTP 400 for url 'https://x/v1beta/models/g:generateContent?"
            "key=AIzaSyABCDEFGH_secretvalue123' Authorization: Bearer sk-ant-abcdef123456")
    out = ai._redact(text)
    assert "AIzaSyABCDEFGH_secretvalue123" not in out
    assert "key=***REDACTED***" in out
    assert "Bearer ***REDACTED***" in out


def test_gemini_key_never_leaks_into_error(monkeypatch):
    """A Gemini HTTP error must not carry the API key into the stored/logged error."""
    secret = "AIzaSy_VERY_secret_key_0001"
    monkeypatch.setenv("GEMINI_API_KEY", secret)
    monkeypatch.setattr(ai.time, "sleep", lambda *_a, **_k: None)

    def _err(*a, **k):
        # simulate httpx echoing the request URL (worst case) with the key in it
        raise ValueError(f"Server error 400 for url 'https://g/generateContent?key={secret}'")

    monkeypatch.setattr(ai, "_post_json", _err)
    r = ai.run_query("gemini_grounding", "q")
    assert r.ok is False
    assert secret not in (r.error or "")
    assert "***REDACTED***" in (r.error or "")


def test_gemini_uses_header_not_query_param(monkeypatch):
    """Regression: the Gemini key goes in the x-goog-api-key header, never ?key=."""
    monkeypatch.setenv("GEMINI_API_KEY", "gk-test")
    captured = {}

    def _capture(url, headers=None, body=None, *, params=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["params"] = params
        return {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}

    monkeypatch.setattr(ai, "_post_json", _capture)
    ai.query_gemini_grounding("q")
    assert captured["headers"].get("x-goog-api-key") == "gk-test"
    assert "key=" not in captured["url"]
    assert not captured.get("params")  # no key in query params


def test_estimate_call_cost_positive_and_search_fee_dominates():
    c = ai.estimate_call_cost("gemini_grounding", output_tokens=800, input_tokens=200,
                              num_searches=1)
    assert c > 0
    # more searches -> higher cost
    assert ai.estimate_call_cost("claude_web", num_searches=3) > ai.estimate_call_cost(
        "claude_web", num_searches=1
    )


def test_openai_run_query_uses_client_seam(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    resp = types.SimpleNamespace(
        output_text="Celsius and F3 Energy.",
        output=[types.SimpleNamespace(
            type="message",
            content=[types.SimpleNamespace(
                annotations=[types.SimpleNamespace(type="url_citation", url="https://x.com")]
            )],
        )],
        usage=types.SimpleNamespace(input_tokens=5, output_tokens=15),
    )
    fake_client = types.SimpleNamespace(
        responses=types.SimpleNamespace(create=lambda **kw: resp)
    )
    monkeypatch.setattr(ai, "_openai_client", lambda: fake_client)
    r = ai.run_query("openai_web_search", "best energy drink?")
    assert r.ok
    assert r.citations == ["https://x.com"]
    assert r.cost_usd > 0


def test_claude_run_query_uses_client_seam(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    resp = types.SimpleNamespace(
        content=[types.SimpleNamespace(type="text", text="F3 Mood.", citations=[])],
        usage=types.SimpleNamespace(input_tokens=5, output_tokens=15, server_tool_use=None),
    )
    fake_client = types.SimpleNamespace(
        messages=types.SimpleNamespace(create=lambda **kw: resp)
    )
    monkeypatch.setattr(ai, "_anthropic_client", lambda: fake_client)
    r = ai.run_query("claude_web", "calm drink?")
    assert r.ok
    assert r.text == "F3 Mood."
