"""User-level Q&A access control for Cora.

Enforces that team members can only ask Cora about entities they are
authorized for, regardless of which channel the question comes from.

Two checks run before every Cora response:
  1. Channel entity scope (channel-routing.yaml) — what entity is THIS channel?
  2. User entity scope (user-permissions.yaml) — is THIS user allowed to ask
     about that entity?

Both must pass. A senior person in a channel they're not scoped for still gets
redirected. A scoped user asking a blocked sensitive topic gets a one-line refusal.

Harrison (root authority) bypasses all checks.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

_PERMISSIONS_PATH = (
    Path(__file__).parent.parent.parent / "data" / "maps" / "user-permissions.yaml"
)

# Simple TTL cache — reload the file at most once every 60 seconds.
# Avoids lru_cache permanently caching a stale/empty result on startup.
_permissions_cache: dict[str, Any] = {}
_permissions_loaded_at: float = 0.0
_PERMISSIONS_TTL = 60.0  # seconds

_HARRISON_ID = "U0B2RM2JYJ1"


# ── 'legal' deflection (Phase 1.6 precision) ───────────────────────────────
# The blanket keyword list over-blocked ordinary business talk that merely
# mentions a contract / agreement / liability ("distribution agreement volume",
# "liability insurance for the warehouse"). Block the 'legal' topic only when the
# message names a genuinely privileged/contentious matter, OR pairs a legal-
# adjacent word with a sensitivity signal. Word-bounded to avoid substring false
# positives ("sue" inside "issue").
_LEGAL_STRONG_RE = re.compile(
    r"\b(?:lawsuit|litigation|attorney|counsel|subpoena|deposition|"
    r"indemnif\w*|cease and desist|breach of contract|privileged|"
    r"arbitration|legal action|legal advice|legal opinion)\b",
    re.I,
)
_LEGAL_WEAK_RE = re.compile(
    r"\b(?:contract|agreement|nda|legal|liabilit\w*)\b", re.I,
)
_LEGAL_SENSITIVITY_RE = re.compile(
    r"\b(?:dispute|breach|sue|suing|sued|lawsuit|litigation|attorney|counsel|"
    r"damages|negligence|settlement|arbitration|liable)\b",
    re.I,
)
# 2026-06-30 over-deflection fix: dropped routine-commercial verbs (terminate,
# penalty, default, violation, enforce) from the sensitivity signal. Paired with
# an ordinary WEAK term (contract/agreement) they read as a genuine legal matter
# and refused Alex/Tommy's normal deal talk ("early-termination penalty on the
# sponsorship contract"). Genuinely-contentious escalations are still caught by
# _LEGAL_STRONG_RE (lawsuit / litigation / attorney / breach of contract / ...)
# or by the remaining sensitivity terms above.


def _legal_is_blocked(msg_lower: str) -> bool:
    """True only for a genuine legal matter: a privileged/contentious term on its
    own, or a legal-adjacent word paired with a sensitivity signal. Ordinary
    business mentions of a contract/agreement/liability are allowed."""
    if _LEGAL_STRONG_RE.search(msg_lower):
        return True
    return bool(_LEGAL_WEAK_RE.search(msg_lower) and _LEGAL_SENSITIVITY_RE.search(msg_lower))


# ── 'financials' deflection (2026-06-30 over-deflection fix, v3 synthesis) ────
# History (three adversarial review rounds): the original matcher was a flat
# substring list (cost/spend/margin/income...) that over-refused COMMERCIAL sales
# questions and mis-fired on substrings (cost->Costco). A word-bounded rewrite
# under-blocked plain-English company finance. A block-unless-deal-scoped version
# then (a) LEAKED company roll-ups phrased by category ("our revenue from
# products" — a plural category read as a single deal) and (b) OVER-BLOCKED
# commercial questions naming a customer as a PROPER NOUN ("revenue from Whole
# Foods" — no generic deal word to trip the gate). Proper nouns (Whole Foods,
# Sprouts, a rep's name, a region) can't be enumerated, so a block-by-default
# gate structurally over-refuses the most common shape a sales owner types.
#
# MODEL — precision-favoring, block only on a clear COMPANY signal:
#   1. CANON            — terms ALWAYS company-level (P&L, cash, EBITDA, payroll,
#                         AR/AP, cogs, overhead...). Block context-free.
#   2. bare "financials" — a company request on its own; block UNLESS the message
#                         is about a SPECIFIC deal ("pull the deal financials").
#   3. FINANCE_TERM     — deal-collidable money words/idioms (profit, revenue,
#                         margin, income, "make money", "in the black", owe...).
#                         Block ONLY when a COMPANY_SCOPE signal is present
#                         (we/our/company/an entity name/an aggregate/a category
#                         roll-up) AND it is NOT about a SPECIFIC single deal.
#   Everything else PASSES — a bare, unscoped money word ("what's the margin")
#   defaults to commercial, which is the whole point of this fix.
#
# LAYERING (this is defense-in-depth, NOT the sole guarantee — do not over-claim):
#   - This deterministic pre-LLM block is layer 1. It cannot classify a bare,
#     context-free money word or a proper-noun customer, so it favors PRECISION
#     (few commercial false-positives) and leans on the other layers for recall.
#   - Layer 2 = the prompt TIER_3 hard-stop (the LLM can read "Whole Foods
#     revenue"=commercial vs "company revenue"=finance from context).
#   - Layer 3 = the tool-level TIER_1 gate on the QBO/cashflow tools
#     (tool_dispatch): live finance DATA is gated regardless of this matcher.
#   - Accepted residual (covered by layers 2+3): a bare scopeless finance term
#     ("what's the revenue"), rare money-verb idioms ("how much did we clear",
#     "are we up/down"), and the "bottom line" discourse marker are NOT blocked
#     here — blocking them would re-introduce the over-deflection this fix removes.
# Cap-table / equity is a SEPARATE topic ('cap_table'), never duplicated here.
_FINANCIALS_CANON_RE = re.compile(
    r"\b(?:"
    r"p\s*&\s*l|p\s*and\s*l|p/l|profit\s+and\s+loss|profit\s*&\s*loss|"
    r"income\s+statement|balance\s+sheet|"
    r"net\s+income|net\s+loss|operating\s+income|"  # P&L bottom-lines: never per-deal ("profit on a deal" is FINANCE_TERM)
    r"cogs|cost\s+of\s+goods(?:\s+sold)?|"
    r"ebitda|ebit|"
    r"net\s+worth|net\s+operating\s+income|noi|cap\s+rate|debt\s+service|refinanc\w*|"
    r"cash\s+flow|cash\s+position|cash\s+balance|cash\s+on\s+hand|cash\s+reserves?|"
    r"cash\s+runway|cash\s+situation|cash\s+positive|how\s+much\s+cash|enough\s+cash|"
    r"how(?:'?s|\s+is)\s+cash|bank\s+balance|money\s+in\s+the\s+bank|"
    r"burn\s+rate|cash\s+burn|monthly\s+burn|net\s+burn|"
    r"financial\s+(?:performance|statements?|position|health|report|results?|picture|overview)|"
    r"profitability|"
    r"payrolls?|"
    r"accounts?\s+receivable|accounts?\s+payable|"
    r"a/?r\s+aging|a/?p\s+aging|receivables?\s+aging|payables?\s+aging|"
    r"quickbooks|qbo|"
    r"total\s+expenses|operating\s+expenses|overhead|"
    r"company'?s?\s+budget|"
    r"how\s+much\s+(?:are|did|do|have)\s+we\s+los(?:e|ing|t)"  # "how much are we losing"
    r")\b",
    re.I,
)

# Bare "financials" — a company request on its own; gated only on specific-deal.
_FINANCIALS_BARE_RE = re.compile(r"\bfinancials\b", re.I)

# Deal-COLLIDABLE finance terms/idioms (block only with a COMPANY_SCOPE signal).
# profits? covers gross/net/operating profit; margins? covers gross/net/operating
# margin; incomes? covers net/operating income.
_FINANCE_TERM_RE = re.compile(
    r"\b(?:profits?|profitab\w*|revenues?|incomes?|margins?|earnings|debts?|"
    r"runways?|finances|financially)\b"
    r"|\blos(?:e|es|ing|t)\s+money\b"
    r"|\b(?:bring(?:ing|s)?\s+in|brought\s+in|bringing\s+home|pull(?:ing|s)?\s+in|pulled\s+in)\b"
    r"|\bin\s+the\s+(?:black|red)\b"
    r"|\bowe[sd]?\b"
    r"|\bmak(?:e|es|ing)\s+money\b|\bmade\s+money\b",
    re.I,
)

# COMPANY_SCOPE — the signal that a FINANCE_TERM is a company/aggregate question,
# not a single-deal one: first person / "the company" / an entity name / an
# aggregate roll-up cue / a plural CATEGORY of business (products/accounts/...).
# Period qualifiers (this quarter / last month) are DELIBERATELY excluded: they
# attach equally to a named-account commercial question ("revenue from Whole Foods
# this month") and would re-introduce over-deflection.
_FINANCIAL_COMPANY_SCOPE_RE = re.compile(
    r"\b(?:we|our|ours|the\s+company|companies|the\s+business|the\s+firm|"
    r"corporate|the\s+books|the\s+org|company)\b"
    r"|\b(?:f3e|f3\s+energy|ufl|osn|bdm|hjrp|hjrg)\b"
    r"|\b(?:across|company[-\s]?wide|portfolio[-\s]?wide|firm[-\s]?wide|"
    r"group[-\s]?wide|consolidated|combined|overall|entire)\b"
    r"|\btotal\s+(?:revenue|profits?|income|sales|margins?|earnings|spend)\b"
    r"|\ball\s+(?:accounts|deals|customers|clients|products|stores|orders|units|sales|channels|regions)\b"
    r"|\bevery\s+(?:account|deal|customer|client|product|store|order|channel|region)\b"
    r"|\bper\s+(?:product|unit|store|account|channel|customer|region)\b"
    r"|\bby\s+(?:product|store|channel|account|customer|region)\b"
    r"|\b(?:products|accounts|customers|clients|stores|channels|regions|brands|wholesale|retail|pipeline)\b",
    re.I,
)

# SPECIFIC single-deal reference — the strongest COMMERCIAL signal; overrides
# company-scope. A SINGULAR determiner (the/this/that/a/an/each) + up to 3
# intervening words (a name, e.g. "the mma lab deal") + a singular deal/
# relationship/event noun. Because it names ONE deal, it is commercial even
# alongside "our" ("our margin on the Sprouts deal" — "the Sprouts deal" fires).
# Possessives (our/its/their/...) are deliberately EXCLUDED as determiners: "our"
# is COMPANY_SCOPE, and "our <X X X> unit" would bridge an aggregate ("our margin
# overall per unit") into a false specific-deal. "unit"/"product" are excluded as
# nouns for the same reason (they collide with "per unit"/"per product"
# aggregates). Bounded {0,3} — no catastrophic backtracking.
_SPECIFIC_DEAL_RE = re.compile(
    r"\b(?:the|this|that|a|an|each)\s+(?:\w+\s+){0,3}"
    r"(?:deal|order|account|customer|client|invoice|po|purchase\s+order|sku|"
    r"case|pallet|shipment|sponsorship|partnership|relationship|store|"
    r"booth|event|activation|contract|trade\s*show|tradeshow|flavors?|flavours?)\b",
    re.I,
)


def _financials_is_blocked(msg_lower: str) -> bool:
    """True for a genuine COMPANY-LEVEL finance question. Commercial / deal-level
    money talk (deal value, PO/order amount, price, margin on an order, invoice
    paid-status, deal/account/named-customer revenue) is allowed.

    See the block comment above for the CANON / bare-financials / FINANCE_TERM +
    COMPANY_SCOPE model and the layering/residual rationale.
    """
    if _FINANCIALS_CANON_RE.search(msg_lower):
        return True
    specific_deal = bool(_SPECIFIC_DEAL_RE.search(msg_lower))
    if _FINANCIALS_BARE_RE.search(msg_lower) and not specific_deal:
        return True
    if (
        _FINANCE_TERM_RE.search(msg_lower)
        and _FINANCIAL_COMPANY_SCOPE_RE.search(msg_lower)
        and not specific_deal
    ):
        return True
    return False


def _load_permissions() -> dict[str, Any]:
    """Load user-permissions.yaml with a 60s TTL cache.

    Uses a simple time-based cache instead of lru_cache to avoid the risk of
    permanently caching an empty dict if the file isn't readable on first call.
    """
    global _permissions_cache, _permissions_loaded_at
    now = time.monotonic()
    if _permissions_cache and (now - _permissions_loaded_at) < _PERMISSIONS_TTL:
        return _permissions_cache
    try:
        with open(_PERMISSIONS_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        result = data.get("users", {})
        if result:  # only cache non-empty results
            _permissions_cache = result
            _permissions_loaded_at = now
        return result
    except FileNotFoundError:
        log.warning("user-permissions.yaml not found — all users get FNDR-level access")
        return {}
    except Exception as exc:
        log.error("Failed to load user-permissions.yaml: %s", exc)
        return {}


def is_authorized(user_id: str, entity: str) -> bool:
    """Return True if the user is allowed to receive answers about this entity.

    Harrison always returns True. Users not in the file default to FNDR-only
    (cross-entity overview access, no sub-entity detail).
    """
    if user_id == _HARRISON_ID:
        return True

    users = _load_permissions()
    entry = users.get(user_id)
    if not entry:
        # Unknown user — allow FNDR and HJRG only (catch-all channels)
        return entity in ("FNDR", "HJRG")

    allowed = entry.get("allowed_entities", [])
    if allowed == "all":
        return True

    # Allow if entity matches or is a parent of an allowed entity
    # e.g. user allowed for LEX-LLC can still interact in #lex channels
    if entity in allowed:
        return True

    # Allow parent entity if user has a sub-entity
    # e.g. entity=LEX, user has LEX-LLC → allow LEX channels too
    for allowed_entity in allowed:
        if allowed_entity.startswith(entity + "-"):
            return True

    return False


def has_unrestricted_entity_access(user_id: str) -> bool:
    """True if the user is authorized for ALL entities (allowed_entities: 'all').

    Such a user works portfolio-wide (e.g. cross-entity finance or HR). Used by the
    DM path to resolve their DM to an aggregator scope (HJRG) rather than their
    narrow org-roles home entity — otherwise cross_entity_guard would redirect
    cross-entity questions they're fully authorized to ask. Fail-closed: unknown /
    unlisted users are NOT unrestricted.
    """
    if user_id == _HARRISON_ID:
        return True
    entry = _load_permissions().get(user_id)
    return bool(entry) and entry.get("allowed_entities") == "all"


def blocked_topics(user_id: str) -> list[str]:
    """Return the list of sensitive topics blocked for this user."""
    if user_id == _HARRISON_ID:
        return []
    users = _load_permissions()
    entry = users.get(user_id, {})
    return entry.get("sensitive_topics_blocked", [])


def check_access(
    user_id: str,
    entity: str,
    user_message: str,
    phi_custodian: bool = False,
    tier: str | None = None,
) -> str | None:
    """Full access check. Returns a redirect message string if blocked, None if allowed.

    Checks:
      1. Entity authorization — is the user allowed to ask about this entity?
      2. Sensitive topic detection — is the question about a blocked topic?

    `phi_custodian` (default False): when True, the `phi` topic block is skipped
    for THIS request only. The caller sets it via lex_phi_access.phi_allowed(),
    which is fail-closed and already verified the user is an authorized LEX
    custodian asking inside LEX scope. All other topic blocks (financials, hr,
    legal, cap_table) and the entity-authorization check are unaffected — this
    flag never opens cross-entity flow.

    `tier` (default None): the channel's financial-access tier ("TIER_1" /
    "TIER_3") from channel_classifier.tier_label(). In a TIER_1 channel
    (leadership / finance / founder / build, or any HJRG channel) financial
    discussion is permitted, so the deterministic `financials` topic block is
    NOT pre-empted there — it would contradict the prompt's own "TIER_1 permits
    financial discussion" rule. Default None is treated as non-TIER_1 (the
    fail-safe restrictive posture), so existing callers keep the old behavior.
    Only the `financials` topic is tier-aware; hr / legal / phi / cap_table stay
    tier-blind (cap-table and PHI are Harrison/EHR-only in every channel).

    Returns None (pass) or a one-sentence redirect (block).
    """
    is_tier_1 = tier == "TIER_1"
    # Entity check.
    # Refusal copy is channel/topic-relative and MUST NOT emit an internal entity
    # code (FNDR/HJRG/F3E/...). Leaking the code both confuses operators and exposes
    # internal taxonomy (the 2026-06-01 #f3-events incident: Alex was refused 3x with
    # "I can only assist with FNDR topics", leaking the code on the access-gate default).
    if not is_authorized(user_id, entity):
        return (
            "That's outside what I can help with in this channel. Ask me in the "
            "channel for the team that owns it and I'll answer there."
        )

    # Sensitive topic check
    blocked = blocked_topics(user_id)
    if not blocked:
        return None

    msg_lower = user_message.lower()

    # NOTE: 'financials' is matched by _financials_is_blocked() (word-bounded,
    # restricted-finance only), not by a substring list — see the loop below.
    topic_patterns = {
        "hr": [
            "salary", "compensation", "pay rate", "hire", "fire", "terminate",
            "performance review", "employee complaint", "disciplinary",
            "benefits", "pto", "vacation", "sick", "401k",
        ],
        "legal": [
            "contract", "agreement", "nda", "lawsuit", "litigation", "legal",
            "attorney", "counsel", "sue", "liability", "indemnif",
        ],
        "phi": [
            "client", "patient", "diagnosis", "treatment", "medication",
            "care plan", "progress note", "clinical", "ddd", "hcbs",
            "behavioral health", "therapy session",
        ],
        "cap_table": [
            "equity", "ownership", "cap table", "shares", "percent", "stake",
            "investor", "dilution", "valuation", "funding round",
        ],
        "cross_entity": [],  # handled by entity check above
    }

    for topic in blocked:
        # Authorized LEX PHI custodian (in LEX scope) — skip the phi block only.
        if topic == "phi" and phi_custodian:
            continue
        if topic == "financials":
            # TIER_1 channels permit financial discussion — don't pre-empt there
            # (the tool-level TIER_1 gate + prompt still govern actual data). In
            # TIER_3 / unknown-tier channels, block ONLY genuine company-level
            # finance; commercial/deal-level money talk passes to the LLM.
            if is_tier_1:
                continue
            matched = _financials_is_blocked(msg_lower)
        elif topic == "legal":
            # Two-signal precision (Phase 1.6) instead of a blanket keyword match.
            matched = _legal_is_blocked(msg_lower)
        else:
            patterns = topic_patterns.get(topic, [])
            matched = any(p in msg_lower for p in patterns)
        if matched:
            redirects = {
                "financials": "Company financials (P&L, cash, payroll) go in a finance channel or to Harrison.",
                "hr": "HR matters go to Hannah Grant or Harrison.",
                "legal": "That's a legal matter. Reach Emily Stubbs.",
                "phi": "Client-specific health info stays in the EHR. Ask the clinical lead.",
                "cap_table": "Ownership details need Harrison.",
            }
            return redirects.get(topic, "That topic is outside your access scope here.")

    return None
