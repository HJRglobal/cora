"""Grounded AI-search surfaces for the AI-visibility engine.

Four grounded / web-search model surfaces, each returning a normalized
``QueryResult``:

  * ``query_perplexity_sonar``  -- Perplexity Sonar (REST; native citations)
  * ``query_openai_web_search`` -- OpenAI Responses API + ``web_search`` tool
  * ``query_gemini_grounding``  -- Gemini + "Grounding with Google Search" (REST)
  * ``query_claude_web``        -- Anthropic Messages API + web-search tool

A common ``run_query(model, prompt)`` wrapper adds:
  * retry with backoff (3 attempts, 1s/2s/5s -- the gego reference cadence),
  * a per-provider concurrency cap (semaphore), and
  * per-call token / search cost logging (greppable ``ai_search usage`` line).

Fail-soft contract (never abort a scan):
  * Missing API key  -> QueryResult(skipped=True, error="...no key..."), no raise.
  * Transient error  -> retried up to 3x, then QueryResult(error=...), no raise.
  * Permanent error  -> QueryResult(error=...), no raise.
So one dead provider or one bad prompt degrades a scan; it never crashes it.

HTTP client is ``httpx`` (repo standard). The OpenAI/Anthropic paths use their
own SDKs. No new dependency is introduced.

PHI guard OFF: this handles F3 marketing/visibility prompts only, no health data.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)

# The four grounded surfaces this connector supports (mirrors
# ai_visibility.prompts.KNOWN_MODELS).
SUPPORTED_MODELS: frozenset[str] = frozenset(
    {"perplexity_sonar", "openai_web_search", "gemini_grounding", "claude_web"}
)

# Model ids -- env-overridable so they can be bumped without a code change.
PERPLEXITY_MODEL = os.environ.get("AI_VIS_PERPLEXITY_MODEL", "sonar")
OPENAI_MODEL = os.environ.get("AI_VIS_OPENAI_MODEL", "gpt-4.1")
GEMINI_MODEL = os.environ.get("AI_VIS_GEMINI_MODEL", "gemini-2.5-flash")
CLAUDE_WEB_MODEL = os.environ.get("AI_VIS_CLAUDE_MODEL", "claude-sonnet-4-6")

_HTTP_TIMEOUT = float(os.environ.get("AI_VIS_HTTP_TIMEOUT", "45"))
_RETRY_DELAYS = (1, 2, 5)  # seconds before retries 1/2/3 (gego cadence)
_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

# Per-provider concurrency cap. Grounded search endpoints rate-limit hard; a
# small cap keeps the fan-out polite. Override with AI_VIS_CONCURRENCY.
_CONCURRENCY = max(1, int(os.environ.get("AI_VIS_CONCURRENCY", "4")))
_SEMAPHORES: dict[str, threading.Semaphore] = {
    m: threading.Semaphore(_CONCURRENCY) for m in SUPPORTED_MODELS
}

# --- Cost model (UPPER estimates; used for the --max-cost-usd hard stop) -------
# The grounded per-search fee dominates; token rates are approximate ceilings.
_SEARCH_FEE = {  # USD per grounded search / request
    "perplexity_sonar": 0.005,
    "openai_web_search": 0.010,
    "gemini_grounding": 0.035,
    "claude_web": 0.010,
}
_IN_RATE = {  # USD per 1M input tokens (ceiling)
    "perplexity_sonar": 1.0,
    "openai_web_search": 3.0,
    "gemini_grounding": 0.30,
    "claude_web": 3.0,
}
_OUT_RATE = {  # USD per 1M output tokens (ceiling)
    "perplexity_sonar": 1.0,
    "openai_web_search": 12.0,
    "gemini_grounding": 2.5,
    "claude_web": 15.0,
}
# Nominal output tokens assumed per call for --dry-run planning only.
_NOMINAL_OUTPUT_TOKENS = 900


class AiSearchError(RuntimeError):
    """Permanent (non-retryable) provider error."""


@dataclass
class QueryResult:
    """Normalized result of one grounded-search call."""

    model: str
    prompt: str
    text: str = ""
    citations: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    num_searches: int = 0
    cost_usd: float = 0.0
    error: str | None = None
    skipped: bool = False  # True only when the provider key is missing

    @property
    def ok(self) -> bool:
        return self.error is None and not self.skipped


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------
def _clean_citations(urls) -> list[str]:
    """Dedupe + keep only http(s) URLs, order-preserving."""
    out: list[str] = []
    seen: set[str] = set()
    for u in urls or []:
        s = str(u or "").strip()
        if not s or not s.lower().startswith(("http://", "https://")):
            continue
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def estimate_call_cost(model: str, *, output_tokens: int = _NOMINAL_OUTPUT_TOKENS,
                       input_tokens: int = 200, num_searches: int = 1) -> float:
    """Estimate one call's USD cost (used for --dry-run planning + real logging)."""
    fee = _SEARCH_FEE.get(model, 0.01) * max(1, num_searches)
    tok = (input_tokens / 1_000_000) * _IN_RATE.get(model, 3.0) + (
        output_tokens / 1_000_000
    ) * _OUT_RATE.get(model, 12.0)
    return round(fee + tok, 6)


# ---------------------------------------------------------------------------
# Network seams (monkeypatched in tests)
# ---------------------------------------------------------------------------
def _post_json(url: str, headers: dict, body: dict, *, params: dict | None = None) -> dict:
    """POST JSON, raise for status, return parsed JSON. The httpx seam for
    Perplexity + Gemini (tests monkeypatch this)."""
    with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
        resp = client.post(url, headers=headers, params=params or {}, json=body)
        resp.raise_for_status()
        return resp.json()


def _openai_client():
    from openai import OpenAI  # noqa: PLC0415 -- lazy so a missing SDK never blocks import

    return OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""), timeout=_HTTP_TIMEOUT)


def _anthropic_client():
    import anthropic  # noqa: PLC0415

    return anthropic.Anthropic(
        api_key=os.environ.get("ANTHROPIC_API_KEY", ""), timeout=_HTTP_TIMEOUT
    )


# ---------------------------------------------------------------------------
# Parsers (pure; unit-tested directly with fixtures)
# ---------------------------------------------------------------------------
def _parse_perplexity(raw: dict) -> QueryResult:
    r = QueryResult(model="perplexity_sonar", prompt="")
    choices = raw.get("choices") or []
    if choices:
        msg = (choices[0] or {}).get("message") or {}
        r.text = str(msg.get("content") or "").strip()
    # Perplexity returns citations either as a flat `citations` list of URLs or
    # as structured `search_results` [{title,url,date}]. Accept both.
    urls = list(raw.get("citations") or [])
    for sr in raw.get("search_results") or []:
        if isinstance(sr, dict) and sr.get("url"):
            urls.append(sr["url"])
    r.citations = _clean_citations(urls)
    r.num_searches = 1
    usage = raw.get("usage") or {}
    r.input_tokens = int(usage.get("prompt_tokens") or 0)
    r.output_tokens = int(usage.get("completion_tokens") or 0)
    return r


def _parse_gemini(raw: dict) -> QueryResult:
    r = QueryResult(model="gemini_grounding", prompt="")
    candidates = raw.get("candidates") or []
    cand = candidates[0] if candidates else {}
    parts = ((cand.get("content") or {}).get("parts")) or []
    r.text = " ".join(str(p.get("text") or "") for p in parts if isinstance(p, dict)).strip()
    urls: list[str] = []
    gmeta = cand.get("groundingMetadata") or {}
    for chunk in gmeta.get("groundingChunks") or []:
        web = (chunk or {}).get("web") or {}
        if web.get("uri"):
            urls.append(web["uri"])
    r.citations = _clean_citations(urls)
    # A grounded response counts as (at least) one search; use the number of
    # search queries the model issued when present.
    queries = gmeta.get("webSearchQueries") or []
    r.num_searches = max(1, len(queries)) if (urls or queries) else 0
    usage = raw.get("usageMetadata") or {}
    r.input_tokens = int(usage.get("promptTokenCount") or 0)
    r.output_tokens = int(usage.get("candidatesTokenCount") or 0)
    return r


def _parse_openai(resp) -> QueryResult:
    r = QueryResult(model="openai_web_search", prompt="")
    r.text = str(getattr(resp, "output_text", "") or "").strip()
    urls: list[str] = []
    searches = 0
    for item in getattr(resp, "output", None) or []:
        itype = getattr(item, "type", None) or (item.get("type") if isinstance(item, dict) else None)
        if itype == "web_search_call":
            searches += 1
        content = getattr(item, "content", None)
        if content is None and isinstance(item, dict):
            content = item.get("content")
        for block in content or []:
            anns = getattr(block, "annotations", None)
            if anns is None and isinstance(block, dict):
                anns = block.get("annotations")
            for ann in anns or []:
                atype = getattr(ann, "type", None) or (
                    ann.get("type") if isinstance(ann, dict) else None
                )
                if atype == "url_citation":
                    url = getattr(ann, "url", None) or (
                        ann.get("url") if isinstance(ann, dict) else None
                    )
                    if url:
                        urls.append(url)
    r.citations = _clean_citations(urls)
    r.num_searches = max(searches, 1 if urls else 0)
    usage = getattr(resp, "usage", None)
    if usage is not None:
        r.input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        r.output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    return r


def _parse_claude(resp) -> QueryResult:
    r = QueryResult(model="claude_web", prompt="")
    text_parts: list[str] = []
    urls: list[str] = []
    searches = 0
    for block in getattr(resp, "content", None) or []:
        btype = getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else None)
        if btype == "text":
            txt = getattr(block, "text", None) or (block.get("text") if isinstance(block, dict) else "")
            if txt:
                text_parts.append(str(txt))
            cits = getattr(block, "citations", None) or (
                block.get("citations") if isinstance(block, dict) else None
            )
            for c in cits or []:
                url = getattr(c, "url", None) or (c.get("url") if isinstance(c, dict) else None)
                if url:
                    urls.append(url)
        elif btype in ("server_tool_use", "web_search_tool_result"):
            searches += 1 if btype == "server_tool_use" else 0
            results = getattr(block, "content", None) or (
                block.get("content") if isinstance(block, dict) else None
            )
            for res in results or []:
                url = getattr(res, "url", None) or (res.get("url") if isinstance(res, dict) else None)
                if url:
                    urls.append(url)
    r.text = "\n".join(text_parts).strip()
    r.citations = _clean_citations(urls)
    usage = getattr(resp, "usage", None)
    if usage is not None:
        r.input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        r.output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        stu = getattr(usage, "server_tool_use", None)
        if stu is not None:
            searches = int(getattr(stu, "web_search_requests", searches) or searches)
    r.num_searches = max(searches, 1 if urls else 0)
    return r


# ---------------------------------------------------------------------------
# Per-provider callers (raise on error; run_query handles retry/fail-soft)
# ---------------------------------------------------------------------------
def query_perplexity_sonar(prompt: str) -> QueryResult:
    key = os.environ.get("PERPLEXITY_API_KEY", "")
    if not key:
        return QueryResult(model="perplexity_sonar", prompt=prompt, skipped=True,
                           error="PERPLEXITY_API_KEY not set")
    raw = _post_json(
        "https://api.perplexity.ai/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        body={"model": PERPLEXITY_MODEL,
              "messages": [{"role": "user", "content": prompt}]},
    )
    r = _parse_perplexity(raw)
    r.prompt = prompt
    return r


def query_gemini_grounding(prompt: str) -> QueryResult:
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        return QueryResult(model="gemini_grounding", prompt=prompt, skipped=True,
                           error="GEMINI_API_KEY not set")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    raw = _post_json(
        url,
        headers={"Content-Type": "application/json"},
        params={"key": key},
        body={"contents": [{"parts": [{"text": prompt}]}],
              "tools": [{"google_search": {}}]},
    )
    r = _parse_gemini(raw)
    r.prompt = prompt
    return r


def query_openai_web_search(prompt: str) -> QueryResult:
    if not os.environ.get("OPENAI_API_KEY", ""):
        return QueryResult(model="openai_web_search", prompt=prompt, skipped=True,
                           error="OPENAI_API_KEY not set")
    client = _openai_client()
    resp = client.responses.create(
        model=OPENAI_MODEL,
        tools=[{"type": "web_search"}],
        input=prompt,
    )
    r = _parse_openai(resp)
    r.prompt = prompt
    return r


def query_claude_web(prompt: str) -> QueryResult:
    if not os.environ.get("ANTHROPIC_API_KEY", ""):
        return QueryResult(model="claude_web", prompt=prompt, skipped=True,
                           error="ANTHROPIC_API_KEY not set")
    client = _anthropic_client()
    resp = client.messages.create(
        model=CLAUDE_WEB_MODEL,
        max_tokens=1024,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
        messages=[{"role": "user", "content": prompt}],
    )
    r = _parse_claude(resp)
    r.prompt = prompt
    return r


_PROVIDERS = {
    "perplexity_sonar": query_perplexity_sonar,
    "openai_web_search": query_openai_web_search,
    "gemini_grounding": query_gemini_grounding,
    "claude_web": query_claude_web,
}


# ---------------------------------------------------------------------------
# Retry / fail-soft wrapper
# ---------------------------------------------------------------------------
def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_STATUS
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    name = type(exc).__name__
    if name in {"RateLimitError", "APITimeoutError", "APIConnectionError",
                "InternalServerError", "APIConnectionTimeoutError"}:
        return True
    msg = str(exc).lower()
    return "429" in msg or "timeout" in msg or "temporarily" in msg or "overloaded" in msg


def run_query(model: str, prompt: str) -> QueryResult:
    """Query one grounded surface with retry, concurrency cap, and cost logging.

    Never raises: missing key -> skipped; transient -> retried; final failure ->
    error set. Cost is computed + logged (greppable ``ai_search usage`` line).
    """
    if model not in _PROVIDERS:
        return QueryResult(model=model, prompt=prompt,
                           error=f"unknown model {model!r}")

    sem = _SEMAPHORES.get(model)
    if sem is not None:
        sem.acquire()
    try:
        last_exc: Exception | None = None
        for attempt in range(len(_RETRY_DELAYS) + 1):
            try:
                r = _PROVIDERS[model](prompt)
            except Exception as exc:  # noqa: BLE001 -- fail-soft by design
                if _is_retryable(exc) and attempt < len(_RETRY_DELAYS):
                    delay = _RETRY_DELAYS[attempt]
                    log.warning("ai_search %s transient (attempt %d/%d), retry %ds: %s",
                                model, attempt + 1, len(_RETRY_DELAYS) + 1, delay, exc)
                    time.sleep(delay)
                    last_exc = exc
                    continue
                log.warning("ai_search %s failed (attempt %d): %s", model, attempt + 1, exc)
                return QueryResult(model=model, prompt=prompt, error=str(exc))
            # Success path (includes the fail-soft skipped result).
            if r.skipped:
                log.warning("ai_search %s skipped: %s", model, r.error)
                return r
            r.cost_usd = estimate_call_cost(
                model, output_tokens=r.output_tokens or _NOMINAL_OUTPUT_TOKENS,
                input_tokens=r.input_tokens or 200, num_searches=r.num_searches or 1,
            )
            log.info(
                "ai_search usage model=%s input=%d output=%d searches=%d cites=%d cost_usd=%.5f",
                model, r.input_tokens, r.output_tokens, r.num_searches,
                len(r.citations), r.cost_usd,
            )
            return r
        return QueryResult(model=model, prompt=prompt,
                           error=f"exhausted retries: {last_exc}")
    finally:
        if sem is not None:
            sem.release()
