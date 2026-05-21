"""Help-intent detection and tier-aware capability blurbs.

When a user @-mentions Cora with a help-style message ("what can you do",
"help", "capabilities", lone "?"), this module:

1. Detects the intent via simple regex / keyword match (no LLM call needed).
2. Returns a tier-aware capability blurb scoped to the channel's function
   and entity.

The handler in app.py intercepts BEFORE the Claude API call when help-intent
is detected. Saves tokens and ensures the help response is consistent +
deterministic across channels.

All variants comply with the brevity doctrine: ≤120 words, plain prose, no
emojis, deep links where appropriate.
"""

from __future__ import annotations

import re


# Patterns that match "I need to know what you can do for me" intent.
# Designed to be conservative — we'd rather miss a help-intent and route to
# Claude than misclassify a real question as help-intent.
_HELP_PATTERNS = [
    re.compile(r"^\s*help\s*\??\s*$", re.IGNORECASE),
    re.compile(r"^\s*\?+\s*$"),
    re.compile(r"\bwhat\s+can\s+you\s+do\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+do\s+you\s+do\b", re.IGNORECASE),
    re.compile(r"\bhow\s+do\s+(i|we)\s+use\s+you\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+(are\s+)?your\s+capabilities\b", re.IGNORECASE),
    re.compile(r"\bcora\s+capabilities\b", re.IGNORECASE),
    re.compile(r"\bhow\s+can\s+you\s+help\b", re.IGNORECASE),
    re.compile(r"\bwho\s+are\s+you\b", re.IGNORECASE),
]


def is_help_intent(user_message: str) -> bool:
    """Return True if the user message is a request to learn what Cora can do.

    Conservative: any pattern match returns True. False positives route to
    Claude (which can still handle the question gracefully); false negatives
    route to the help message (which is fine — better to over-explain than
    under-explain to a confused user).
    """
    if not user_message or not user_message.strip():
        # Bare @-mention with no text -> treat as help-intent
        return True
    return any(p.search(user_message) for p in _HELP_PATTERNS)


# ────────────────────────────────────────────────────────────────────────────
# Tier-aware capability blurbs
# ────────────────────────────────────────────────────────────────────────────


def build_message(entity: str, function: str, tier: str) -> str:
    """Return a tier+entity-aware capability blurb scoped to the channel.

    Stays ≤120 words. Plain prose. No emojis. Includes 2-3 example questions
    appropriate to the channel function, a read-only disclosure, and a
    feedback-loop nudge (react with thumbs to flag good/bad answers).
    """
    intro = _intro_for_entity(entity)
    capabilities = _capabilities_for_tier(tier, function, entity)
    examples = _examples_for_function(function, entity)

    return (
        f"{intro}\n\n"
        f"{capabilities}\n\n"
        f"Try asking: {examples}\n\n"
        f"I'm read-only. Update Asana / HubSpot / QBO / Drive directly and I'll "
        f"see it on my next sync. If my answer is off, react with a thumbs-up "
        f"or thumbs-down so I can learn what's working."
    )


def _intro_for_entity(entity: str) -> str:
    """One-line intro framed for the channel's entity scope."""
    by_entity = {
        "FNDR": "I'm Cora, your portfolio assistant. I work across HJR Global and every portfolio entity.",
        "HJRG": "I'm Cora, your portfolio assistant. This channel is HJR Global (the holdco), so I can also talk cross-portfolio.",
        "F3E":  "I'm Cora. In this channel I'm scoped to F3 Energy (and the paired F3 Community nonprofit when relevant).",
        "LEX":  "I'm Cora. In this channel I'm scoped to Lexington Services (LLC, LLA, LBHS). PHI stays in the EHR, not here.",
        "OSN":  "I'm Cora. In this channel I'm scoped to One Stop Nutrition (4 stores).",
        "BDM":  "I'm Cora. In this channel I'm scoped to Big D Media — your in-house creative agency.",
        "UFL":  "I'm Cora. In this channel I'm scoped to UFL.",
        "HJRP": "I'm Cora. In this channel I'm scoped to HJR Properties.",
        "HJRPROD": "I'm Cora. In this channel I'm scoped to HJR Productions (podcast / book / personal brand).",
    }
    return by_entity.get(entity, "I'm Cora, your portfolio assistant.")


def _capabilities_for_tier(tier: str, function: str, entity: str) -> str:
    """List 3-4 capabilities scoped to the channel tier + function."""
    is_tier_1 = tier == "TIER_1"

    if is_tier_1:
        return (
            "I can answer strategic questions across this entity's projects, decisions, "
            "and operations — grounded in your portfolio briefs, prior decisions, meeting "
            "transcripts, and live data from Asana, HubSpot, Google Calendar, and "
            "QuickBooks Online (P&L, balance sheet, AR/AP aging, recent transactions). "
            "Tier-1 financial topics are fully in scope here."
        )

    # Tier-3 — scope by function
    if function == "sales":
        return (
            "Open deals from HubSpot (entity pipeline), account questions from briefs, "
            "and Asana tasks assigned to you. Tier-3 channel — financial deep-dives go "
            "in the entity's #*-finance or #*-leadership channel."
        )
    if function == "ops":
        return (
            "Your Asana tasks, project status from briefs and meeting notes, calendar "
            "events, and operational context for this entity. Tier-3 — financial "
            "deep-dives go in the entity's #*-finance channel."
        )
    if function == "hr":
        return (
            "Your Asana tasks, calendar events, and people/role context from portfolio "
            "briefs. Tier-3 — payroll, comp, and financial topics go in the entity's "
            "#*-finance or #*-leadership channel."
        )
    if function == "clients":
        return (
            "Account context, your Asana tasks, calendar events, and deal status from "
            "briefs. Tier-3 — internal financials go in the entity's #*-finance channel."
        )

    return (
        "Your Asana tasks, calendar events, and questions answered from portfolio "
        "briefs and decisions. Tier-3 — financial deep-dives go in the entity's "
        "#*-finance or #*-leadership channel."
    )


def _examples_for_function(function: str, entity: str) -> str:
    """2-3 example questions tailored to the channel function + entity."""
    if function in ("leadership", "finance", "founder", "build"):
        return (
            "\"what's our P&L for last month?\", \"what's open on me?\", \"what was "
            "decided about [topic]?\""
        )
    if function == "sales":
        return (
            "\"what's in my pipeline?\", \"what's the latest on [account]?\", \"what "
            "tasks are open on me?\""
        )
    if function == "ops":
        return (
            "\"what tasks are open on me?\", \"what's the status on [project]?\", "
            "\"what was decided in [meeting]?\""
        )
    if function == "hr":
        return (
            "\"what tasks are open on me?\", \"what's on my calendar this week?\", "
            "\"what's the latest on [person/role]?\""
        )
    return (
        "\"what's open on me?\", \"what's the latest on [topic]?\", \"what was "
        "decided about [topic]?\""
    )
