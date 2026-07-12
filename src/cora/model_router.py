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
    # DTC inventory WRITE intent (2026-07-10 hotfix) -- a write flow is not a Haiku
    # job. Catches the "set X at the office to N" turn; the bare-"yes" confirm turn
    # is escalated separately in app.py when a pending shopify write exists.
    r"\b(set|update|change|adjust|correct|bump|lower|raise)\b[^?!.]{0,60}\b(inventory|stock|on.?hand|units?)\b",
    r"\bset\b[^?!.]{0,60}\bto\s+\d+\b",
    r"\b(inventory|stock)\b[^?!.]{0,30}\bto\s+\d+\b",
    r"\bfoot traffic\b",
    r"\bcustomer (count|traffic|trends?|numbers?|growth)\b",
    r"\bhow'?s? (gilbert|warner|mckellips|greenfield|val vista|pecos|GW|GM|GF|VVP)\b",
    r"\bwhat did (we|GW|GM|GF|VVP|gilbert|warner|mckellips|greenfield|val\s*vista|pecos)\b",
    r"\bwhat (sales|revenue|numbers?|did).{0,20}(GW|GM|GF|VVP|gilbert|warner|mckellips|greenfield|val\s*vista|pecos)\b",
    # whats_on_my_plate composite queries -- a multi-source tool whose reply must
    # faithfully narrate several sections. Haiku misnarrated a degraded tool
    # result as "no open tasks" on 2026-06-11; plate queries force Sonnet.
    r"\b(on|off) (my|the|\w+'?s?) plate\b",
    r"\bmy plate\b",
    r"\bcatch me up\b",
    r"\bwhat (do|did|does) (i|\w+) have going on\b",
    r"\bmy (day|workload) (look|looking)\b",
    r"\bhow'?s? my day\b",
    # cora_person_dossier composite queries -- same multi-source-narration risk as
    # whats_on_my_plate (Haiku misnarrates degraded/empty sources). Force Sonnet for
    # founder check-ins + self check-ins. Anchored to a person/self subject so ordinary
    # phrases ("our involvement with the D-Backs", "been working on the deck") don't match.
    r"\bcheck in on\b",
    r"\bwhat (has|have) \w+ been (working|up to|doing|involved)\b",
    r"\bwhat have i been (working|up to|doing|involved)\b",
    r"\bwhat i'?ve been (working|up to|doing)\b",
    r"\b\w+'?s (recent )?involvement\b",
    # Composite / dashboard tool families (F-01, 2026-07-12). Haiku under-calls
    # these tools and answers from KB instead (F-16/F-22 -- meeting_action_items,
    # personal_oneamerica_portfolio, personal_capital_program_state,
    # fndr_content_pipeline, f3e_creator_crm). Force Sonnet so the tool is invoked;
    # the prompt directives (F-16/F-22/parked-1) make it MANDATORY. Bias-to-Sonnet
    # is safe (Haiku->Sonnet misroute = slower only). The channel_content_guard
    # (F-08) still blocks any confidential figure that reaches a wrong channel.
    r"\baction items?\b",
    r"\bmy (to-?dos?|action items?|takeaways?) from\b",
    r"\b(cash value|policy loans?|whole.?life|death benefit)\b",
    r"\bOneAmerica\b",
    r"\b(capital program|cap table|the raise|equity seats?)\b",
    r"\b(content pipeline|content calendar|freelancer deliverables?)\b",
    r"\b(creator crm|creator roster|sponsorship pipeline)\b",
    # Calendar-READ intents (F-02, 2026-07-12). On Haiku the model skipped
    # calendar_get_my_events and FABRICATED an outage ("I don't have access to your
    # calendar"); force Sonnet so the read tool is actually invoked. Anchored so
    # ordinary "free" / "meeting" prose elsewhere doesn't over-match.
    r"\bon my (calendar|schedule|agenda)\b",
    r"\bwhat'?s? on my (calendar|schedule|agenda)\b",
    r"\bmy (calendar|schedule|agenda) (today|tomorrow|this week|look|for)\b",
    r"\bwhat'?s? my (schedule|calendar|agenda)\b",
    r"\bam i free\b",
    r"\b(do i have|what) (any )?(meetings?|events?|calls?)\b",
    r"\bfree (today|tomorrow|this (week|morning|afternoon)|on \w+day)\b",
]
_SONNET_INDICATOR_RE = re.compile("|".join(_SONNET_INDICATOR_PATTERNS), re.IGNORECASE)

# Haiku-friendly query patterns -- direct factual lookups, single-tool dispatches.
# NOTE: this list is advisory/dead (choose_model never consults it); "plate" was
# removed 2026-06-11 (Sonnet-forced) and calendar/schedule/agenda/"am i free"/
# "do i have meetings" are ALSO Sonnet-forced now (F-02, 2026-07-12) despite still
# appearing below -- the Sonnet indicators above win, this list changes nothing.
_HAIKU_HINT_PATTERNS = [
    r"\bwhat'?s? (on my|my) (calendar|tasks?|schedule|deals?|pipeline)\b",
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
