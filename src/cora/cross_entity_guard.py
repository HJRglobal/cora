"""Deterministic pre-LLM cross-entity redirect guard.

When Cora operates in an entity-scoped channel (e.g. #osn-leadership) and a user
asks about a DIFFERENT portfolio entity (e.g. "what's F3 Energy's monthly
revenue?"), this guard intercepts BEFORE the LLM/tool-call path and returns the
correct one-sentence redirect directly.

Why code-level instead of prompt-only:
    The prompt-only firewall ("before calling any tool, check the entity scope")
    is unreliable — the model routes to tools first and applies the scope check
    afterward (or not at all), so it surfaces cross-entity data before the
    refusal ever fires. A deterministic pre-LLM guard makes the redirect immune
    to that ordering and to LLM helpfulness bias. Same pattern as
    sibling_guard.py (LEX intra-family). sibling_guard handles LEX→LEX; this
    guard handles every cross-FAMILY case (OSN→F3E, LEX→F3E, F3E→OSN, ...).

Matching is case-insensitive with word boundaries to avoid false positives
(e.g. "villa" must not trigger the LLA redirect; "results" must not trigger LTS).

FNDR and HJRG channels are pass-through: FNDR is the cross-entity aggregator and
#hjrg-* channels are explicitly allowed to ask portfolio-wide (founder doctrine).
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class _EntityDef:
    name: str                       # Full name used in the redirect sentence
    channel_hint: str               # Where to send the asker
    patterns: tuple[re.Pattern, ...]  # Pre-compiled keyword patterns


def _compile(*keywords: str) -> tuple[re.Pattern, ...]:
    """Compile each keyword as a word-boundary, case-insensitive pattern.

    Trailing/leading whitespace in a keyword is stripped before wrapping in
    \\b...\\b — the word boundary already prevents substring false positives
    (e.g. "lla" will not match inside "villa").
    """
    return tuple(
        re.compile(r"\b" + re.escape(kw.strip()) + r"\b", re.IGNORECASE)
        for kw in keywords
    )


# One entry per non-FNDR portfolio entity. The KEY is the entity family code
# (channels in that family will NOT be blocked against their own entry).
_ENTITY_DEFS: dict[str, _EntityDef] = {
    "F3E": _EntityDef(
        "F3 Energy", "#f3e-leadership or #f3e-finance",
        _compile("f3 energy", "f3energy", "f3e", "f3 pure", "f3pure", "f3 mood",
                 "f3mood", "energy drink", "shopify", "dtc", "cotton 3pl"),
    ),
    "LEX": _EntityDef(
        "Lexington Services", "#lex-leadership or #llc-leadership",
        _compile("lexington", "lex services", "lex-llc", "lex llc", "lbhs",
                 "lla", "lex-lts", "lts", "ddd", "hcbs", "tucson dta",
                 "revalidation"),
    ),
    "OSN": _EntityDef(
        "One Stop Nutrition", "#osn-leadership",
        _compile("one stop nutrition", "osn", "gilbert warner", "gilbert mckellips",
                 "greenfield", "val vista", "nutrition store", "four stores",
                 "4 stores", "matt petrovich"),
    ),
    "UFL": _EntityDef(
        "United Fight League", "#ufl-leadership",
        _compile("united fight league", "ufl", "fight league", "mma league",
                 "team ownership", "ufl sponsor"),
    ),
    "BDM": _EntityDef(
        "Big D Media", "#bdm-leadership",
        _compile("big d media", "bdm", "larry stone", "content agency"),
    ),
    "HJRP": _EntityDef(
        "HJR Properties", "#hjrp-leadership",
        _compile("hjr properties", "hjrp", "1337 gilbert", "1555 gilbert",
                 "north hampton", "south hampton", "rogers ranch", "payson cabin",
                 "cinema lanes", "lci realty"),
    ),
    "HJRPROD": _EntityDef(
        "HJR Productions", "#hjrprod-leadership",
        _compile("hjr productions", "hjrprod", "chokehold", "falling forward",
                 "clouthub", "hjr podcast"),
    ),
    "F3C": _EntityDef(
        "F3 Community", "#f3c-leadership",
        _compile("f3 community", "f3c", "lexington education foundation",
                 "nonprofit arm"),
    ),
}

# Channels that may ask portfolio-wide — no cross-entity block.
_PASS_THROUGH: frozenset[str] = frozenset({"FNDR", "HJRG"})


def _channel_family(channel_entity: str) -> str:
    """Collapse a channel entity code to its blockable family.

    OSN sub-stores (OSNGW/OSNGM/OSNGF/OSNVV) → "OSN".
    LEX sub-entities (LEX-LLC/LEX-LTS/LEX-LBHS/LEX-LLA) → "LEX".
    Everything else maps to itself.
    """
    ce = (channel_entity or "").upper()
    if ce.startswith("OSN"):
        return "OSN"
    if ce.startswith("LEX"):
        return "LEX"
    return ce


def check_cross_entity(message_text: str, channel_entity: str) -> str | None:
    """Return a one-sentence redirect if message asks about a non-channel entity.

    Returns None when:
      - message_text or channel_entity is empty
      - the channel is FNDR or HJRG (cross-entity aggregators)
      - the channel family is not a recognized blockable entity
      - no non-channel entity keyword is detected

    When a match fires, the returned string is the COMPLETE response — callers
    must post it as-is with no additions and skip the LLM entirely.
    First-match-wins in _ENTITY_DEFS insertion order.
    """
    if not message_text or not channel_entity:
        return None

    if channel_entity.upper() in _PASS_THROUGH:
        return None

    family = _channel_family(channel_entity)
    self_def = _ENTITY_DEFS.get(family)
    if self_def is None:
        # Unrecognized / non-firewalled channel family — do not interfere.
        return None

    for code, ent in _ENTITY_DEFS.items():
        if code == family:
            continue  # never block an entity's own questions
        for pat in ent.patterns:
            if pat.search(message_text):
                return (
                    f"That's a {ent.name} question — ask in {ent.channel_hint}. "
                    f"I'm scoped to {self_def.name} here."
                )

    return None
