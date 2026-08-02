"""Shared LLM rate table + est-USD formula for delegated work (Phase 1).

Design of record: 2026-08-01 delegated-work design, section 10. Pinned to
STANDARD Sonnet pricing ($3 in / $15 out per MTok) -- deliberately conservative
across the 8/31 intro-pricing boundary -- with the 4-term token formula
(input, cache_create x1.25, cache_read x0.1, output) PLUS $0.01 per web search
(server-side search billing is invisible to token usage; 12 searches = $0.12 =
6% of the per-job cap, so it must count).

STDLIB-ONLY (the llm_usage doctrine): importable by the D-047 standalone
modules with no bot-process dependency. The health-report ``_MODEL_RATES``
table stays the DISPLAY-side twin of this metering-side helper.
"""

from __future__ import annotations

# USD per million tokens, standard Sonnet.
SONNET_INPUT_USD_PER_MTOK = 3.0
SONNET_OUTPUT_USD_PER_MTOK = 15.0
# Anthropic cache multipliers on the input rate.
CACHE_CREATE_MULT = 1.25
CACHE_READ_MULT = 0.10
# Server-side web search, per executed search.
WEB_SEARCH_USD = 0.01


def estimate_usd(
    input_tokens: int,
    cache_create_tokens: int,
    cache_read_tokens: int,
    output_tokens: int,
    searches: int = 0,
) -> float:
    """The 4-term formula + web-search surcharge. All args are TOTALS."""
    per_in = SONNET_INPUT_USD_PER_MTOK / 1_000_000
    per_out = SONNET_OUTPUT_USD_PER_MTOK / 1_000_000
    usd = (
        max(0, int(input_tokens or 0)) * per_in
        + max(0, int(cache_create_tokens or 0)) * per_in * CACHE_CREATE_MULT
        + max(0, int(cache_read_tokens or 0)) * per_in * CACHE_READ_MULT
        + max(0, int(output_tokens or 0)) * per_out
        + max(0, int(searches or 0)) * WEB_SEARCH_USD
    )
    return round(usd, 6)
