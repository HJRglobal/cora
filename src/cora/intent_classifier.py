"""Intent classifier for Cora — classifies questions before the pipeline runs.

Classifies each user message into one of four intent categories to enable
efficient pipeline routing:

  FINANCIAL   → skip KB retrieval; go straight to financial tool call.
                Never read or write the semantic cache (data changes constantly).
  TASK_LOOKUP → use Asana tool; fetch fewer KB chunks (k=4).
                Short cache TTL (5 min) because tasks change frequently.
  SIMPLE      → KB-only retrieval; small k (k=3); long cache TTL (1 hour).
                Covers quick factual questions: brand colors, taglines, who is X.
  COMPLEX     → full pipeline; default k; standard cache TTL (30 min).

Classification uses keyword/regex rules — no LLM call. ~microseconds per classify.

Rules are intentionally conservative: when uncertain, classify COMPLEX (full pipeline)
rather than skipping retrieval that might be needed.
"""

import html
import re
from enum import Enum
from dataclasses import dataclass


# ── Intent enum ───────────────────────────────────────────────────────────────

class Intent(str, Enum):
    FINANCIAL   = "financial"     # cash, P&L, margins, financial pulse
    TASK_LOOKUP = "task_lookup"   # tasks, Asana, what's on my plate
    IDENTITY    = "identity"      # who am I, do you know me — never cache (user-specific)
    SIMPLE      = "simple"        # quick factual: tagline, colors, who is X
    COMPLEX     = "complex"       # default — full pipeline


# ── Routing hints ─────────────────────────────────────────────────────────────

@dataclass
class RoutingHints:
    """Routing configuration derived from intent classification."""
    intent: Intent
    skip_kb: bool           # skip KB retrieval entirely
    kb_k_override: int | None  # override default k=8 (None = use default)
    bypass_cache: bool      # do not read or write semantic cache
    cache_ttl: int          # seconds to cache the response (0 = don't cache)


# ── Pattern lists ──────────────────────────────────────────────────────────────

_FINANCIAL_PATTERNS = [
    r"\bcash\s*(position|flow|balance|on\s*hand)\b",
    r"\bp[&\s]+l\b",
    r"\bprofit\s*(&|and)\s*loss\b",
    r"\bmargin[s]?\b",
    r"\bbreakeven\b",
    r"\bweekly\s*cash\b",
    r"\bfinancial\s*(pulse|state|status|report|summary|snapshot)\b",
    r"\bhow\s*(much\s*money|are\s*we\s*(making|losing|spending))\b",
    r"\bnet\s*(income|loss|revenue)\b",
    r"\bactuals?\b",
    r"\bforecast[s]?\b",
    r"\bcash[\s\-]flow\b",
    r"\byear[\s\-]?to[\s\-]?date\b",
    r"\bYTD\b",
    r"\bquarterly\s*(results?|numbers?|performance)\b",
    r"\bhow\s+is\s+\w+\s+(tracking|doing|performing)\s+financially\b",
    r"\bwhat['']?s\s+the\s+(cash|financial)\b",
    r"\bbalance\s*sheet\b",
    r"\bAR\s+aging\b",
    r"\bAP\s+aging\b",
    r"\bgross\s*(margin|profit|revenue)\b",
    r"\boperating\s*(loss|income|expenses?)\b",
]

_TASK_PATTERNS = [
    r"\bmy\s*(open\s*)?tasks?\b",
    r"\bwhat['']?s\s+on\s+my\s+plate\b",
    r"\bassigned\s+to\s+me\b",
    r"\btasks?\s*(assigned|due|open|overdue|pending)\b",
    r"\bopen\s+(action\s+items?|tasks?)\b",
    r"\bwhat\s+(do\s+I\s+have|should\s+I\s+(work\s+on|do)\s+(today|now|next))\b",
    r"\bpending\s+(tasks?|items?|work)\b",
    r"\bdue\s+(today|this\s+week|soon)\b",
    r"\boverdue\s+(tasks?|items?)\b",
    r"\baction\s+items?\b",
    r"\bwhat['']?s\s+left\b",
    r"\basana\b",
]

_IDENTITY_PATTERNS = [
    r"\bwho\s+am\s+i\b",
    r"\bdo\s+you\s+know\s+who\s+i\s+am\b",
    r"\bdo\s+you\s+know\s+me\b",
    r"\bwhat\s+is\s+my\s+name\b",
    r"\bwho\s+are\s+you\s+talking\s+to\b",
    r"\bwho\s+is\s+asking\b",
]

_SIMPLE_PATTERNS = [
    r"\btagline\b",
    r"\bbrand\s*(colors?|palette|fonts?|typography|voice)\b",
    r"\bwho\s+is\s+\w+",
    r"\blaunch\s*date\b",
    r"\bwhat\s+is\s+(the\s+)?\w+\s*(tagline|color|font)\b",
    r"\bowner\s+of\b",
    r"\bwho\s+owns\b",
    r"\bcap\s*table\b",
    r"\boperating\s*agreement\b",
    r"\bphone\s*number\b",
    r"\bemail\s*(address)?\b",
    r"\bwhat\s+(is|are)\s+(the\s+)?(F3|Pure|Mood|Energy)\s+(colors?|tagline|avatar|fonts?)\b",
    r"\bwhen\s+(does|did|is)\s+.+\s+launch\b",
    r"\bwhat\s+are\s+(the\s+)?hours?\b",
]

# Compile once at import time
_FINANCIAL_RE = re.compile("|".join(_FINANCIAL_PATTERNS), re.IGNORECASE)
_TASK_RE      = re.compile("|".join(_TASK_PATTERNS),      re.IGNORECASE)
_IDENTITY_RE  = re.compile("|".join(_IDENTITY_PATTERNS),  re.IGNORECASE)
_SIMPLE_RE    = re.compile("|".join(_SIMPLE_PATTERNS),    re.IGNORECASE)


# ── Public API ────────────────────────────────────────────────────────────────

def classify(user_message: str, entity: str = "") -> Intent:
    """Classify a user message into an Intent category.

    Rules applied in priority order: FINANCIAL > TASK_LOOKUP > SIMPLE > COMPLEX.
    COMPLEX is the safe default — it runs the full pipeline and never skips context.

    entity is accepted for future entity-specific rule variants (e.g., OSN questions
    about "store count" are operational, not financial). Currently unused.
    """
    text = html.unescape(user_message)
    if _FINANCIAL_RE.search(text):
        return Intent.FINANCIAL
    if _IDENTITY_RE.search(text):
        return Intent.IDENTITY
    if _TASK_RE.search(text):
        return Intent.TASK_LOOKUP
    if _SIMPLE_RE.search(text):
        return Intent.SIMPLE
    return Intent.COMPLEX


def routing_hints(intent: Intent) -> RoutingHints:
    """Return routing configuration for a given intent.

    These are the pipeline switches downstream code reads:
      - skip_kb          → pass to context_loader (skip KB retrieval)
      - kb_k_override    → pass to context_loader (override top-K)
      - bypass_cache     → pass to semantic_cache (skip read + write)
      - cache_ttl        → pass to semantic_cache.store()
    """
    if intent == Intent.FINANCIAL:
        return RoutingHints(
            intent=intent,
            skip_kb=True,       # live financial tool provides the data; KB adds noise
            kb_k_override=None,
            bypass_cache=True,  # financial data changes; never serve stale
            cache_ttl=0,
        )
    if intent == Intent.IDENTITY:
        return RoutingHints(
            intent=intent,
            skip_kb=True,       # identity is injected in runtime_context; KB irrelevant
            kb_k_override=None,
            bypass_cache=True,  # answer is per-user — caching would serve wrong person
            cache_ttl=0,
        )
    if intent == Intent.TASK_LOOKUP:
        return RoutingHints(
            intent=intent,
            skip_kb=False,
            kb_k_override=4,    # Asana tool dominates; fewer KB chunks needed
            bypass_cache=False,
            cache_ttl=300,      # tasks change fast — 5-minute TTL
        )
    if intent == Intent.SIMPLE:
        return RoutingHints(
            intent=intent,
            skip_kb=False,
            kb_k_override=3,    # one or two chunks usually sufficient
            bypass_cache=False,
            cache_ttl=3600,     # simple facts stable for 1 hour
        )
    # COMPLEX — full pipeline, standard 30-minute cache
    return RoutingHints(
        intent=intent,
        skip_kb=False,
        kb_k_override=None,
        bypass_cache=False,
        cache_ttl=1800,
    )
