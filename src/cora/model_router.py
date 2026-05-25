"""Per-request model selection — Sonnet vs Haiku based on query shape.

Default is Sonnet (Cora's existing model -- strong reasoning, good tool-use).
We switch to Haiku for queries that look operationally simple, on the bet that
~60% of @-mentions are short factual lookups or single-tool dispatches where
Haiku's 3-5x speed advantage matters more than Sonnet's reasoning depth.

Classification is heuristic-only (regex/substring on the user message). We don't
call out to a classifier model -- that would add the latency we're trying to save.

If the heuristic gets it wrong:
- Sonnet->Haiku misroute: Haiku might handle the query worse (give a slightly
  less synthesized answer). User can retry with more reasoning-heavy phrasing
  or Cora's reply is "good enough" anyway.
- Haiku->Sonnet misroute: just slower than necessary. No quality loss.

So errors lean conservative. We can tune the heuristic over time by watching
which Haiku responses get thumbs-down reactions.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# Model strings -- exposed for tests + claude_client.
MODEL_SONNET = "claude-sonnet-4-6"
MODEL_HAIKU = "claude-haiku-4-5-20251001"

# Default model when no router signal can be derived.
DEFAULT_MODEL = MODEL_SONNET

# Length threshold -- messages longer than this default to Sonnet.
LONG_MESSAGE_CHAR_THRESHOLD = 200

# Reasoning-intent indicators. If any appear (case-insensitive word boundary),
# force Sonnet. These keywords correlate with multi-step reasoning, comparison,
# strategic judgment, or "explain your work" expectations -- all Sonnet wins.
_SONNET_INDICATOR_PATTERNS = [
    r"\banaly[sz]e\b",
    r"\banaly[sz]is\b",
    r"\bcompare\b",
    r"\bcomparison\b",
    r"\brecommend\b",
    r"\brecommendation\b",
    r"\bshould (i|we|harrison)\b",
    r"\bwould you\b",
    r"\bexplain\b",
    r"\bwhy (is|are|did|do|does|would|should)\b",
    r"\bstrategy\b",
    r"\bstrategic\b",
    r"\bdeep dive\b",
    r"\bthorough\b",
    r"\beverything about\b",
    r"\bbreak down\b",
    r"\bpros and cons\b",
    r"\btrade.?offs?\b",
    r"\bpush back\b",
    r"\bdraft an? (email|post|memo|message|note|reply)\b",
    # Tool-invocation queries -- Haiku reliably fails to call tools for these,
    # answering from KB context instead. Force Sonnet so tools are invoked.
    r"\bhow (are|is|were|was) (sales|revenue|numbers|traffic|customers)\b",
    r"\bwhat'?s? (our|the|today'?s?|yesterday'?s?|this week'?s?) (sales|revenue|numbers|traffic)\b",
    r"\bshow (me )?(sales|revenue|inventory|stock|customers|traffic)\b",
    r"\b(sales|revenue|transactions?|ticket) (today|yesterday|this week|last week|this month)\b",
    r"\bhow (much|many) (did we|have we|do we)\b",
    r"\b(low|running low|out of) (on )?(stock|inventory)\b",
    r"\binventory (status|levels?|check|at)\b",
    r"\bwhat'?s? (low|running low|out)\b",
    r"\bfoot traffic\b",
    r"\bcustomer (count|traffic|trends?|numbers?|growth)\b",
    r"\bhow'?s? (gilbert|warner|mckellips|greenfield|val vista|pecos|GW|GM|GF|VVP)\b",
    r"\bwhat did (we|GW|GM|GF|VVP|gilbert|warner|mckellips|greenfield|val\s*vista|pecos)\b",
    r"\bwhat (sales|revenue|numbers?|did).{0,20}(GW|GM|GF|VVP|gilbert|warner|mckellips|greenfield|val\s*vista|pecos)\b",
]
_SONNET_INDICATOR_RE = re.compile("|".join(_SONNET_INDICATOR_PATTERNS), re.IGNORECASE)

# Haiku-friendly query patterns -- direct factual lookups, single-tool dispatches.
_HAIKU_HINT_PATTERNS = [
    r"\bwhat'?s? (on my|my) (plate|calendar|tasks?|schedule|deals?|pipeline)\b",
    r"\bshow me\b",
    r"\blist\b",
    r"\bfind\b",
    r"\bget\b",
    r"\bdo i have\b",
    r"\bam i free\b",
]
_HAIKU_HINT_RE = re.compile("|".join(_HAIKU_HINT_PATTERNS), re.IGNORECASE)


def choose_model(user_message: str) -> str:
    """Decide which Claude model to use for a given user message.

    Rules (first match wins):
      1. Empty / whitespace-only message -> Sonnet (defensive -- odd case, give it brains).
      2. Sonnet indicator present -> Sonnet.
      3. Long message (> LONG_MESSAGE_CHAR_THRESHOLD chars) -> Sonnet.
      4. Otherwise -> Haiku.

    Returns the canonical model string. Pass directly to claude_client functions.
    """
    if not user_message or not user_message.strip():
        return MODEL_SONNET

    stripped = user_message.strip()

    if _SONNET_INDICATOR_RE.search(stripped):
        return MODEL_SONNET

    if len(stripped) > LONG_MESSAGE_CHAR_THRESHOLD:
        return MODEL_SONNET

    return MODEL_HAIKU


def is_haiku(model: str) -> bool:
    """Convenience for logging -- is this the Haiku model string?"""
    return model == MODEL_HAIKU


def short_label(model: str) -> str:
    """Short human-readable label for logs: 'sonnet' or 'haiku'."""
    if model == MODEL_HAIKU:
        return "haiku"
    if model == MODEL_SONNET:
        return "sonnet"
    return model  # unknown -- log verbatim
