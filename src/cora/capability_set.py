"""Derived capability vocabulary for the honesty rail (Code #13 slice 1,
cq-2a88e32a75ea; charter v1 section 1, 2026-09-08).

THE DEFECT. On 2026-09-10 (09:16-10:30 AZ) the model answered five unmatched
verb-shaped founder DMs with "I don't have visibility into the code queue or
staged items right now ... use the Asana interface directly", "I don't have a
way to ship code sessions directly", "I don't have a tool to close code-queue
items", "I don't have tools to approve or stage code-queue items" -- six minutes
after the same bot STAGED a row on a typed verb -- and named an internal tool
(`cora_queue_code_session`) to the founder. S2' (slack_egress.screen_phantom_
write_claims) is blind to these by design: they are not WRITE claims, they are
CAPABILITY denials, and a denial is only false when it is about something the
bot actually has.

THE DISCRIMINATOR IS THE CAPABILITY SET, NOT THE PHRASE. "I don't have that
document" and "I can't tell from here" are honest sentences about things the bot
truly lacks; "I don't have visibility into the code queue" is false because the
queue verbs exist. So the set of things-the-bot-has is DERIVED at call time, never
hand-listed, from three live sources:

  1. the TOOL REGISTRY -- tool_dispatch.tools_for_entity(entity, cross_entity),
     i.e. exactly the tools the model was offered in that channel (a LEX channel
     has no HubSpot tools, so a HubSpot denial there is honest);
  2. the QUEUE-VERB TABLE -- code_queue's typed-verb grammar (stage / approve /
     dismiss / ship) and the queue OBJECTS those verbs act on; the founder's
     door is the typed verb, a teammate's door is `queue a code session: ...`;
  3. the LADDER REGISTRY -- data/ladder-registry.yaml (Code #13 slice 7), where a
     lane may declare `capability_terms`; absent file = no contribution.

WHAT IS HAND-WRITTEN HERE is only an ALIAS table: the natural-language names a
registry TOKEN goes by ("qbo" -> "quickbooks"; "dm" -> "direct message";
"queue" -> "code queue" / "staged items"). An alias contributes ONLY when its
token is present in the offered registry -- remove every hubspot_* tool from an
entity's exposure and "hubspot" drops out of that entity's set with it. A token
with no alias row still contributes itself (a new `klaviyo_*` tool makes
"klaviyo" a capability the day it ships). Test-pinned: an alias whose token is
absent never appears.

No LLM anywhere in this module. Every reader is fail-soft: an import error or a
malformed registry yields a SMALLER set (fewer trips), never a crash and never a
larger one.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
#: Test override only. None = cora.ladder_registry.registry_path() (the ONE
#: resolver for the registry file; CORA_LADDER_REGISTRY_PATH, read-only).
LADDER_REGISTRY_PATH: Path | None = None

# Tokens a tool NAME carries that say nothing about WHAT the tool reaches: verbs,
# shapes, scopes, connectives. Dropped before a name contributes terms.
_GENERIC_TOKENS: frozenset[str] = frozenset({
    "get", "my", "create", "update", "add", "set", "by", "status", "summary", "pulse",
    "check", "run", "list", "log", "complete", "delete", "index", "state", "items", "item",
    "the", "a", "for", "to", "and", "of", "on", "whats", "what", "is", "in", "recent",
    "detail", "breakdown", "performance", "waterfall", "attribution", "generate", "batch",
    "drafts", "draft", "handle", "handles", "user", "users", "location", "program",
    "portfolio", "points", "aging", "sheet", "loss", "profit", "expense", "vendor", "spend",
    "transactions", "events", "event", "meeting", "task", "tasks", "comment", "subtask",
    "stage", "deal", "deals", "deliverable", "note", "open", "channel", "card", "cards",
    "voice", "radar", "crm", "pipeline", "candidates", "self", "session", "code", "work",
    "forget", "remember", "person", "send", "schedule", "cm", "pixel", "subbrand", "ar",
    "ap", "balance", "cashflow", "close", "pack", "financial", "f3", "f3e", "fndr", "hjrp",
    "osn", "lex", "cora", "cowork", "revalidation", "ledger", "warehouse", "shopify", "set",
    # Plain English words that happen to sit in a tool name. Each of these made an
    # HONEST sentence trip on the first cut ("I don't have VISIBILITY into what the
    # landlord decided" matched f3e_ai_VISIBILITY; "I can't take ACTION" would
    # match meeting_ACTION_items; "that CONTENT" fndr_CONTENT_pipeline). They
    # contribute only through their COMPOUND (ai visibility, action items, content
    # pipeline ...), added explicitly below.
    "ai", "visibility", "action", "content", "personal", "travel", "capital", "completion",
    "brand", "cultural", "creator", "production", "press", "program", "points", "channel",
    "items", "by", "location", "sales", "compliance", "my", "on",
})

# Tool-name token PAIRS that identify a family only together (see _GENERIC_TOKENS:
# the halves are ordinary words). (token_a, token_b) -> alias key.
_COMPOUND_TOKENS: tuple[tuple[str, str, str], ...] = (
    ("ai", "visibility", "ai_visibility"),
    ("meeting", "action", "action_items"),
    ("content", "pipeline", "content_pipeline"),
    ("press", "pipeline", "press_pipeline"),
    ("production", "pipeline", "production_pipeline"),
    ("brand", "voice", "brand_voice"),
    ("cultural", "radar", "cultural_radar"),
    ("creator", "crm", "creator_crm"),
    ("completion", "candidates", "completion_candidates"),
    ("capital", "program", "capital_program"),
    ("travel", "points", "travel_points"),
    ("inventory", "location", "inventory_by_location"),
    ("fighter", "compliance", "fighter"),
    ("sales", "deck", "deck"),
    ("sales", "pulse", "sales_pulse"),
)

# Natural-language synonyms for a registry TOKEN. A row contributes ONLY when its
# token appears in a tool name the asker's channel was offered (see module doc).
# Every alias is matched as a whole word/phrase after hyphen/underscore -> space
# normalization; plurals are spelled out where the plural is the common form.
_TOKEN_ALIASES: dict[str, tuple[str, ...]] = {
    "asana": ("asana", "asana task", "asana tasks", "asana board"),
    "hubspot": ("hubspot", "crm", "pipeline", "deals", "deal pipeline", "sales pipeline"),
    "qbo": ("qbo", "quickbooks", "quickbooks online", "p&l", "p and l", "profit and loss",
            "balance sheet", "ar aging", "ap aging", "general ledger"),
    "calendar": ("calendar", "google calendar", "calendar events"),
    "gmail": ("gmail", "email", "emails", "inbox", "mailbox"),
    "shopify": ("shopify", "shopify store", "shopify inventory", "dtc inventory"),
    "inventory": ("inventory", "inventory levels", "stock levels", "stock"),
    "deposco": ("deposco", "warehouse inventory"),
    "dm": ("dm", "dms", "direct message", "direct messages", "slack dm", "slack message"),
    "queue": ("code queue", "queue item", "queue items", "staged item", "staged items",
              "code session", "code sessions", "code session queue", "backlog item",
              "build queue", "the queue"),
    "delegate": ("delegate", "delegated work", "delegated job", "background job",
                 "research brief"),
    "notes": ("notes", "personal notes", "my notes", "your notes", "saved notes"),
    "dossier": ("dossier", "person dossier"),
    "ads": ("ads", "ad performance", "ad spend", "meta ads", "google ads", "ads data"),
    "blog": ("blog", "blog drafts", "blog post drafts", "blog cards"),
    "deck": ("sales deck", "deck", "pitch deck"),
    "image": ("image", "images", "image generation"),
    "plate": ("plate", "my plate", "on my plate"),
    "decisions": ("open decisions", "decisions", "stalled decisions", "pending decisions"),
    "contracts": ("contracts dashboard", "contracts"),
    "lease": ("lease", "leases", "lease status", "lease renewals"),
    "ai_visibility": ("ai visibility", "ai visibility scan"),
    "influencer": ("influencer", "influencers", "influencer tracker"),
    "fighter": ("fighter", "fighters", "fighter compliance"),
    "revops": ("revops", "revops ledger", "rev ops"),
    "dashboards": ("dashboards", "dashboard", "cowork dashboards"),
    "lexicon": ("lexicon",),
    "action_items": ("action items", "meeting action items"),
    "rangeme": ("rangeme",),
    "brand_voice": ("brand voice", "brand voice check"),
    "cultural_radar": ("cultural radar",),
    "creator_crm": ("creator crm",),
    "production_pipeline": ("production pipeline",),
    "press_pipeline": ("press pipeline",),
    "content_pipeline": ("content pipeline",),
    "completion_candidates": ("completion candidates",),
    "capital_program": ("capital program",),
    "oneamerica": ("oneamerica",),
    "travel_points": ("travel points",),
    "cashflow": ("cash flow", "cash flow forecast", "cash forecast", "13 week"),
    "inventory_by_location": ("inventory by location",),
    "sales_pulse": ("sales pulse", "shopify sales"),
}

#: Family labels + a short "how to ask" for the honest template's `Try:` hint.
#: Keyed by the registry token that identifies the family. A family absent from
#: the offered registry never renders (same rule as the aliases).
_FAMILY_HINTS: dict[str, str] = {
    "asana": "ask me for your Asana tasks, or to create / complete one",
    "hubspot": "ask me for your HubSpot deals or a pipeline summary",
    "qbo": "ask me for a QuickBooks P&L, balance sheet or AR/AP aging by entity",
    "calendar": "ask me what's on your calendar today or tomorrow",
    "gmail": "ask me to pull up your inbox or draft an email",
    "shopify": "ask me for Shopify inventory or the sales pulse",
    "inventory": "ask me for F3E inventory by location or the inventory pulse",
    "dm": "ask me to DM a named teammate (I stage it first)",
    "delegate": "say 'delegate: <the job>' and I stage a background job",
    "notes": "say 'remember <fact>' or 'show my notes'",
    "dossier": "ask me for a dossier on a named person",
    "ads": "ask me for the ads performance summary",
    "blog": "ask me for the blog card drafts",
    "decisions": "ask me for the open decisions",
    "plate": "ask 'what's on my plate'",
}

_QUEUE_HINT_FOUNDER = ("`stage cq-<12 hex>` / `approve cq-<12 hex>` / `dismiss cq-<12 hex>` "
                       "-- one verb per message, on its own line")
_QUEUE_HINT_TEAMMATE = "'queue a code session: <your ask>' -- it lands in Harrison's queue"

_TOKEN_SPLIT_RE = re.compile(r"[_\-\s]+")


def _norm(s: str) -> str:
    """Lower-case; hyphen / underscore / slash -> space; collapse runs. Applied to
    BOTH the reply window and every term so 'code-queue' == 'code queue'."""
    return re.sub(r"\s+", " ", re.sub(r"[-_/]+", " ", str(s or "").lower())).strip()


def _offered_tool_names(entity: str | None, cross_entity: bool) -> list[str]:
    try:
        from cora.tools import tool_dispatch as td  # lazy: heavy module, avoid import cycles
        return [str(t.get("name") or "") for t in td.tools_for_entity((entity or "FNDR").upper(), bool(cross_entity))]
    except Exception as exc:  # noqa: BLE001 -- a missing registry is a SMALLER set, never a crash
        log.warning("capability_set: tool registry unavailable: %s", exc)
        return []


def registry_tool_names() -> frozenset[str]:
    """EVERY tool name in the registry (all entities) -- the internal-symbol set the
    toolname leak half of the screen reads. Fail-soft to empty."""
    try:
        from cora.tools import tool_dispatch as td
        return frozenset(str(t.get("name") or "") for t in td.TOOL_DEFINITIONS if t.get("name"))
    except Exception as exc:  # noqa: BLE001
        log.warning("capability_set: TOOL_DEFINITIONS unavailable: %s", exc)
        return frozenset()


def queue_verbs() -> tuple[str, ...]:
    """The typed queue verbs, read from code_queue's own grammar (never re-listed)."""
    try:
        from cora import code_queue as cq
        pat = getattr(cq, "_QUEUE_VERB_RE").pattern
        m = re.search(r"\(([a-z|]+)\)\\s\+\(cq-", pat)
        verbs = tuple(m.group(1).split("|")) if m else ()
        if hasattr(cq, "_SHIP_VERB_RE"):
            verbs = verbs + ("ship",)
        return verbs
    except Exception as exc:  # noqa: BLE001
        log.warning("capability_set: queue verb grammar unavailable: %s", exc)
        return ()


def _ladder_terms() -> dict[str, str]:
    """`capability_terms` declared on ladder-registry lanes (slice 7). Only EXPLICIT
    declarations count -- lane ids are never tokenized (a lane called
    phantom_write_screen must not make 'write' a capability). Absent file = {}."""
    try:
        from cora import ladder_registry  # lazy: the registry owns its path + parser
        data = ladder_registry.load(LADDER_REGISTRY_PATH)
    except Exception as exc:  # noqa: BLE001 -- a malformed registry contributes nothing
        log.warning("capability_set: ladder registry unreadable: %s", exc)
        return {}
    if not data.get("available", True):
        return {}
    out: dict[str, str] = {}
    lanes = data.get("lanes") if isinstance(data, dict) else None
    for lane in lanes or []:
        if not isinstance(lane, dict):
            continue
        terms = lane.get("capability_terms")
        if not isinstance(terms, list):
            continue
        hint = str(lane.get("ask_hint") or f"ask about the {lane.get('lane', 'lane')} lane")
        for t in terms:
            if isinstance(t, str) and t.strip():
                out.setdefault(_norm(t), hint)
    return out


def capability_terms(entity: str | None, *, cross_entity: bool = False,
                     founder: bool = False) -> dict[str, str]:
    """{normalized term: 'Try:' hint} for everything the bot HAS in this channel.

    Sources, in order: the offered tool registry (+ alias rows whose token is
    present), the queue-verb table (objects; the verbs themselves are hint text),
    the ladder registry's explicit `capability_terms`. Deterministic; fail-soft."""
    terms: dict[str, str] = {}
    names = _offered_tool_names(entity, cross_entity)
    present_tokens: set[str] = set()
    for name in names:
        toks = [t for t in _TOKEN_SPLIT_RE.split(name.lower()) if t]
        if toks and toks[0] == "cora" and len(toks) > 1:
            toks = toks[1:]
        for t in toks:
            if t and t not in _GENERIC_TOKENS:
                present_tokens.add(t)
        # compound tokens the alias table keys on (the halves are generic words)
        for a, b, key in _COMPOUND_TOKENS:
            if a in toks and b in toks:
                present_tokens.add(key)
        if "queue" in toks:
            present_tokens.add("queue")
        if "delegate" in toks:
            present_tokens.add("delegate")
        if any(t in ("remember", "notes", "note") for t in toks):
            present_tokens.add("notes")
        if "cashflow" in toks or ("financial" in toks and "cashflow" in name):
            present_tokens.add("cashflow")
    for tok in sorted(present_tokens):
        if tok == "queue":
            continue  # queue objects are added below with the founder-aware hint
        hint = _FAMILY_HINTS.get(tok, f"ask me directly -- I have {tok.replace('_', ' ')} tools in this channel")
        if "_" not in tok:
            terms.setdefault(_norm(tok), hint)   # a compound key contributes ONLY its aliases
        for alias in _TOKEN_ALIASES.get(tok, ()):
            terms.setdefault(_norm(alias), hint)
    if "queue" in present_tokens or founder:
        qhint = _QUEUE_HINT_FOUNDER if founder else _QUEUE_HINT_TEAMMATE
        for alias in _TOKEN_ALIASES["queue"]:
            terms.setdefault(_norm(alias), qhint)
    for t, hint in _ladder_terms().items():
        terms.setdefault(t, hint)
    return terms


def find_capability_term(window: str, terms: dict[str, str]) -> tuple[str, str] | None:
    """(term, hint) for the LONGEST capability term present in *window* as a whole
    word/phrase (after normalization), or None. Longest-first so 'code queue'
    wins over a bare 'queue' alias when both are present."""
    if not window or not terms:
        return None
    w = " " + _norm(window) + " "
    for term in sorted(terms, key=len, reverse=True):
        if not term:
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", w):
            return term, terms[term]
    return None


def mentions_cq_id(text: str) -> bool:
    return bool(re.search(r"\bcq-[0-9a-f]{12}\b", str(text or ""), re.IGNORECASE))
