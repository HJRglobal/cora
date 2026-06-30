"""Graduated-trust SHADOW-MODE instrumentation (2026-06-29 spec).

Generates the data needed to decide whether to move the knowledge gate "off the
low-stakes majority" (the Tag brain-learning design,
00-Founder/tag-standup/2026-06-28_fndr_brain-learning-architecture.md) WITHOUT
changing any current behavior.

Cora today (WS17-C / D-060) is full-manual-gating: every knowledge item -> Harrison's
👍 (D-011). This module computes, per knowledge proposal, *what graduated trust WOULD
have done* and logs it -- acting on nothing. After ~2 weeks Harrison reviews the
shadow log (counts by tier, would-Tier-0 rate, would-Tier-0 false-positive rate)
and decides the flip.

CRUCIAL: flipping graduated Tier-0 live is a PARTIAL REVERSAL of WS17-C and touches
D-011. This module ONLY produces shadow data. It NEVER approves, writes, routes, or
mutates any proposal. The live approve/DM path is byte-identical with this on (a
test asserts it). The flip is Harrison's explicit decision, made FROM this data --
never from this code.

What it computes per proposal (known_answer / efficiency / generic):
  * coras_read_verdict -- the CORROBORATED/CONFLICTS/ADDS-CONTEXT/NET-NEW signal
    already computed by coras_read for the DM; persisted here instead of discarded.
  * category -- a deterministic (no-LLM, fail-safe) keyword classification into the
    allowlist (operational/SOP/who-owns-what/contacts/logistics/addresses/
    product-inventory) or denylist (money/contracts/legal/equity/comp/strategy) or
    "other". Denylist patterns are checked FIRST so a borderline item leans to the
    denylist (-> Tier 2) -- the conservative bias the near-zero-false-positive bar
    on Tier 0 demands.
  * shadow_tier / shadow_decision:
      Tier 0 (would-auto-approve)  -- CORROBORATED + allowlist category + no conflict
                                      + NOT LEX/PHI/clinical/Maricopa + NOT a denylist
                                      category + NOT cross-entity + contributor is a
                                      recognized teammate for that entity.
      Tier 1 (would-route-to-owner)-- entity-operational (allowlist, not high-stakes,
                                      no conflict) from that entity's AUTHORIZED OWNER
                                      (gap-domain-owners) but not corroborated.
      Tier 2 (harrison)            -- everything else: the high-stakes core + anything
                                      contradicting canon.

Logging is append-only to logs/graduated-trust-shadow-YYYY-MM-DD.jsonl:
  * a `shadow_decision` record at DM time, and
  * a `shadow_reaction` record once Harrison's real 👍/👎 lands (a later run),
    correlated by update_id -- so `--report` can measure would-Tier-0 items Harrison
    actually thumbs-down'd = false positives.

Kill switch: CORA_GRADUATED_SHADOW=0 disables all logging (the report still reads
whatever is on disk).

Ops: this is script-side (the scheduled knowledge-review drain imports it) -- no bot
restart. The module never imports app.py / tool_dispatch / claude_client.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Arizona is UTC-7 with NO DST -- a fixed offset is correct and robust on hosts
# without the IANA tz DB (same pattern as run_knowledge_review / strategy_memo).
_AZ = timezone(timedelta(hours=-7))

# ── Tiers / decisions ────────────────────────────────────────────────────────

TIER_0 = 0
TIER_1 = 1
TIER_2 = 2

DECISION_AUTO = "would-auto-approve"      # Tier 0
DECISION_OWNER = "would-route-to-owner"   # Tier 1
DECISION_HARRISON = "harrison"            # Tier 2

# ── Category vocabulary (from the 6/28 brain-learning doc) ───────────────────

ALLOWLIST_CATEGORIES = frozenset(
    {"operational", "sop", "ownership", "contacts", "logistics", "addresses",
     "product_inventory"}
)
DENYLIST_CATEGORIES = frozenset(
    {"money", "contracts", "legal", "equity", "comp", "strategy"}
)

_KNOWN_VERDICTS = frozenset({"CORROBORATED", "CONFLICTS", "ADDS-CONTEXT", "NET-NEW"})

# Cap text fed to the regex classifiers. Real knowledge claims are short; a long
# semi-trusted paste (a big #info-for-cora block) is bounded here so the regex
# pass can't add seconds of latency to the 7am drain (adversarial review: the
# email-shaped pattern backtracks ~O(n^2) on a long TLD-less domain string).
_MAX_CLASSIFY_CHARS = 2000

# ── Category classifier (deterministic, denylist-first, fail-safe) ───────────
# Denylist FIRST: a money/legal/equity/comp/contract/strategy keyword wins over any
# allowlist keyword in the same text, so a fact that smells expensive is never
# mis-binned as low-stakes-operational. Unknown text -> "other" (never Tier 0/1).

_DENYLIST_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("equity", re.compile(
        r"\b(cap[\s-]?table|equity|shareholders?|ownership stake|membership interest"
        r"|\bshares?\b|vesting|dilution|stake in the|cap stack|ownership (?:split|%)"
        r"|percent ownership)\b", re.I)),
    ("comp", re.compile(
        r"\b(salar(?:y|ies)|compensation|payroll|bonus(?:es)?|pay raises?|raises?"
        r"|wages?|hourly rate|severance|stipend|commission|benefits package|\bPTO\b"
        r"|paid time off)\b", re.I)),
    ("money", re.compile(
        r"(\$\s?\d"
        r"|\b\d+(?:\.\d+)?\s?(?:k|m|mm|million|thousand)\b\s*(?:in\s+)?"
        r"(?:revenue|sales|loan|deposit|cash|budget)"
        # spelled / per-unit / net-terms money cues the noun list misses (review LOW)
        r"|\b\d+\s+dollars?\b"
        r"|\bnet[\s-]?\d{2}\b"
        r"|\bprice\s+(?:list|per|point|increase|change|drop|hike)\b"
        r"|\bcost\s+(?:per|increase|of\s+goods|went\s+up|breakdown)\b"
        r"|\b(?:revenue|invoices?|payments?|deposits?|loans?|refinanc\w*|cash[\s-]?flow"
        r"|cash position|cash balance|budgets?|pricing|wire transfers?|profits?"
        r"|profitable|margins?|accounts? (?:receivable|payable)|owe[sd]?|spend(?:ing)?"
        r"|expenses?|reimburs\w*|financ(?:e|ed|es|ial|ing)|payable|receivable"
        r"|deductible|royalt\w+|funds?\s+transfer)\b)", re.I)),
    ("contracts", re.compile(
        r"\b(contracts?|agreements?|\bMSA\b|\bNDA\b|\bSOW\b|\bLOI\b|leases?"
        r"|renewal terms?|term sheet|amendments?|signed (?:the|a|an|off|by)"
        r"|executed (?:the|a|an)|counter[\s-]?part(?:y|ies)|clauses?|addendum"
        r"|purchase agreement|promissory)\b", re.I)),
    ("legal", re.compile(
        r"\b(lawsuits?|litigation|attorneys?|legal counsel|\blegal\b|compliance"
        r"|regulat\w+|\blien\b|\bUCC\b|settlements?|disputes?|subpoena|plaintiff"
        r"|defendant|cease and desist|liabilit\w+|indemnif\w+|copyright|trademark"
        r"|\bIP\b assignment|patent)\b", re.I)),
    ("strategy", re.compile(
        r"\b(strateg(?:y|ic|ies)|pivot(?:ing|ed)?|acquisition|\bM&A\b|mergers?"
        r"|fundrais\w*|investors?|valuation|roadmap|expansion plan|go[\s-]to[\s-]market"
        r"|exit strategy|board (?:meeting|decision|approval)|term sheet)\b", re.I)),
]

# A street-address shape, used in addition to the addresses keyword pattern.
_STREET_RE = re.compile(
    r"\b\d{1,6}\s+(?:[NSEW]\.?\s+)?[A-Za-z0-9][\w'.]*\s+"
    r"(?:st|street|ave|avenue|blvd|boulevard|rd|road|dr|drive|ln|lane|way|ct|court"
    r"|pl|place|pkwy|parkway|hwy|highway|ste|suite|cir|circle|ter|terrace)\b\.?",
    re.I)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_PHONE_RE = re.compile(r"\b(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b")

_ALLOWLIST_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("addresses", re.compile(
        r"\b(located at|address is|the address|mailing address|street address"
        r"|ship(?:ping)? address|suite\s+\d|\bste\.?\s*\d|\bunit\s+\d{1,4}\b)\b", re.I)),
    ("contacts", re.compile(
        r"\b(point of contact|\bPOC\b|reach out to|contact (?:is|for|info|at|details)"
        r"|phone number|cell (?:is|number|phone)|email (?:is|address)"
        r"|best (?:way|number|email) to reach)\b", re.I)),
    ("ownership", re.compile(
        r"\b(owns the|now owns|in charge of|responsible for|point person|owner of"
        r"|now manages|handles the|leads? (?:on|the)|reports? to|oversees|runs the"
        r"|is the (?:lead|owner|manager|point) (?:on|of|for))\b", re.I)),
    ("product_inventory", re.compile(
        r"\b(in stock|out of stock|inventory|units? (?:on hand|left|remaining|available"
        r"|in stock)|\bSKU\b|cases? (?:of|on hand|left|in stock)|reorder|stock level"
        r"|on hand|\bUPC\b|\bGTIN\b|case pack|pallets?|lot (?:number|code)"
        r"|product (?:codes?|line)|flavou?rs?)\b", re.I)),
    ("logistics", re.compile(
        r"\b(deliver(?:y|ies|ed)|shipments?|shipping|ships? (?:to|on|out)|pick[\s-]?up"
        r"|drop[\s-]?off|freight|3PL|warehouses?|logistics|scheduled (?:for|on)"
        r"|hours (?:are|of operation)|opens? (?:at|on)|closes? at|the timeline"
        r"|due date|deadlines?|lead time)\b", re.I)),
    ("sop", re.compile(
        r"\b(\bSOP\b|standard operating|the procedure|protocols?|workflows?"
        r"|the process (?:is|for|to)|process for|how (?:we|to|you) (?:do|handle|submit"
        r"|file|run|request)|step\s+\d|checklists?|the policy (?:is|for)"
        r"|onboarding (?:process|steps)|best practice)\b", re.I)),
    ("operational", re.compile(
        r"\b(uses?|use the|go to|located in|the tool|the system|log ?in|portal"
        r"|dashboard|the channel|where (?:is|to find|we (?:keep|store))|stored in"
        r"|lives (?:in|at|on)|found (?:in|at|on)|set up (?:in|on|the)|configured"
        r"|access(?:ed)? (?:via|through|at)|the link (?:is|to)|the file (?:is|lives)"
        r"|the (?:doc|sheet|folder) (?:is|lives|for))\b", re.I)),
]

_MARICOPA_RE = re.compile(r"\bmaricopa\b", re.I)


def _text_entities(text: str) -> set[str]:
    """Distinct portfolio entities whose keywords appear in `text`, reusing the
    cross_entity_guard keyword dictionaries (the same deterministic firewall maps).

    Paired families (F3E<->F3C) collapse to one so a brand-family cross-reference is
    NOT counted as cross-entity. Used to catch a claim whose TEXT spans 2+ entities
    even though its payload carries only a singular `entity` (known_answer / generic
    items never set an `entities` list) -- the spec's 'NOT cross-entity' Tier-0
    condition. Fail-safe to empty (no signal -> nothing forced high-stakes here)."""
    try:
        from .cross_entity_guard import _ENTITY_DEFS, PAIRED_ENTITIES
    except Exception:  # noqa: BLE001
        return set()
    found: set[str] = set()
    for code, ent in _ENTITY_DEFS.items():
        for pat in ent.patterns:
            if pat.search(text):
                found.add(code)
                break

    def _canon(code: str) -> str:
        # Stable representative for a paired family so F3E and F3C collapse to the
        # SAME key (min of the family) -- a brand-family cross-reference counts once.
        family = {code} | set(PAIRED_ENTITIES.get(code, set()))
        return min(family)

    return {_canon(c) for c in found}


def categorize(text: str) -> str:
    """Best-effort category for a knowledge claim. Denylist-first; "other" on miss.

    Pure/deterministic (no LLM): cheap, testable, and fail-safe -- an unknown shape
    falls to "other", which can never clear Tier 0/1.
    """
    t = (text or "")[:_MAX_CLASSIFY_CHARS]  # bounded so the regex pass can't stall the drain
    for cat, pat in _DENYLIST_PATTERNS:
        if pat.search(t):
            return cat
    if _STREET_RE.search(t):
        return "addresses"
    for cat, pat in _ALLOWLIST_PATTERNS:
        if pat.search(t):
            return cat
    if _EMAIL_RE.search(t) or _PHONE_RE.search(t):
        return "contacts"
    return "other"


# ── High-stakes detector ──────────────────────────────────────────────────────

def is_high_stakes(
    text: str,
    entity: str,
    category: str,
    entities: list[str] | None = None,
) -> tuple[bool, list[str]]:
    """True (with reasons) if this proposal is in the always-escalate core.

    Catches LEX entity, PHI/clinical/billing content, the Maricopa county
    behavioral-health context, any denylist category, and a cross-entity span --
    BOTH from the structured `entities` list (set only for efficiency items) AND
    from a keyword scan of the claim TEXT (the only signal for known_answer /
    #info-for-cora generics, which carry a singular entity). Fail-safe: a
    PHI-predicate error counts AS high-stakes (never under-flag).
    """
    text = (text or "")[:_MAX_CLASSIFY_CHARS]  # bounded (perf / ReDoS, review LOW)
    reasons: list[str] = []
    ent = (entity or "").strip().upper()
    if ent.startswith("LEX"):
        reasons.append("lex_entity")
    try:
        from .phi_guard import is_phi_risk, is_clinical_phi, is_lex_billing_status_phi
        if is_phi_risk(text) or is_clinical_phi(text) or is_lex_billing_status_phi(text):
            reasons.append("phi")
    except Exception:  # noqa: BLE001 -- fail-safe: treat an erroring check as high-stakes
        reasons.append("phi_check_error")
    if _MARICOPA_RE.search(text):
        reasons.append("maricopa")
    if category in DENYLIST_CATEGORIES:
        reasons.append(f"denylist_category:{category}")
    # Cross-entity via the structured list (efficiency items)...
    if entities:
        distinct = {e.strip().upper() for e in entities if e and str(e).strip()}
        if len(distinct) >= 2:
            reasons.append("cross_entity")
    # ...and via a TEXT keyword scan, so a known_answer/generic claim whose text
    # spans 2+ entities is caught even though its payload entity is singular
    # (closes the Tier-0 'NOT cross-entity' gap for the item types that CAN reach
    # Tier 0). Over-flagging only ever escalates toward Tier 2 -- the safe direction.
    if len(_text_entities(text)) >= 2:
        if "cross_entity" not in reasons:
            reasons.append("cross_entity")
        reasons.append("cross_entity_text")
    return (bool(reasons), reasons)


# ── Contributor recognition / authorized-owner (advisory lookups, fail-safe) ──

def _role_for(slack_id: str):
    if not slack_id:
        return None
    try:
        from . import org_roles
        return org_roles.get_role(slack_id)
    except Exception:  # noqa: BLE001
        return None


def _entity_matches(role_entities: list[str], entity: str) -> bool:
    ent = (entity or "").strip().upper()
    for e in role_entities or []:
        eu = (e or "").strip().upper()
        if not eu:
            continue
        if eu == ent or eu == "ALL":
            return True
        # LEX sub-entities collapse to the LEX family for recognition matching.
        if ent.startswith("LEX") and eu.startswith("LEX"):
            return True
    return False


def contributor_recognized(slack_id: str, entity: str) -> bool:
    """True iff slack_id is a recognized (non-external) teammate whose role
    entities include `entity`. Machine-mined items (no slack_id) are NOT
    recognized -- they can never clear Tier 0. Fail-safe to False."""
    rec = _role_for(slack_id)
    if rec is None:
        return False
    if getattr(rec, "external", False):
        return False
    try:
        return _entity_matches(rec.all_entities, entity)
    except Exception:  # noqa: BLE001
        return False


def authorized_owner(slack_id: str, entity: str) -> bool:
    """True iff slack_id is the entity's authorized domain owner
    (gap-domain-owners.yaml). Fail-safe to False."""
    if not slack_id:
        return False
    try:
        from .gap_autofill import resolve_owner
        owner = resolve_owner(entity)
    except Exception:  # noqa: BLE001
        return False
    return bool(owner) and owner == slack_id


# ── Per-proposal field extraction ─────────────────────────────────────────────

def claim_text(update: dict[str, Any]) -> str:
    """The richest text available for categorization, by update type."""
    payload = update.get("payload") or {}
    utype = update.get("update_type", "")
    if utype == "known_answer":
        parts = [str(payload.get("question") or ""), str(payload.get("answer") or "")]
        joined = " ".join(p for p in parts if p).strip()
        return joined or str(update.get("description") or "")
    if utype == "efficiency":
        parts = [str(payload.get("title") or ""), str(payload.get("recommendation") or "")]
        joined = " ".join(p for p in parts if p).strip()
        return joined or str(update.get("description") or "")
    return (str(payload.get("text") or "") or str(payload.get("answer") or "")
            or str(update.get("description") or "")).strip()


def contributor_id(update: dict[str, Any]) -> str:
    """Slack user ID of the human contributor, or "" for machine-mined items.

    known_answer: answered_by (set only for teammate_dm answers; mined ones are "").
    generic (#info-for-cora / folded note): author_id.
    efficiency: "" (machine-mined -> never a recognized teammate -> never Tier 0).
    """
    payload = update.get("payload") or {}
    utype = update.get("update_type", "")
    if utype == "known_answer":
        return str(payload.get("answered_by") or "").strip()
    if utype == "generic":
        return str(payload.get("author_id") or "").strip()
    return ""


def _entities_list(update: dict[str, Any]) -> list[str] | None:
    payload = update.get("payload") or {}
    ents = payload.get("entities")
    if isinstance(ents, list):
        return [str(e) for e in ents if e]
    return None


# ── Tier classification ────────────────────────────────────────────────────────

def classify_tier(
    *,
    coras_read_verdict: str,
    category: str,
    entity: str,
    contributor_id: str,
    claim_text: str = "",
    entities: list[str] | None = None,
) -> tuple[int, str, list[str]]:
    """Compute (shadow_tier, shadow_decision, reasons). Pure / no side-effects.

    Conservative by construction: high-stakes OR a conflict short-circuits to
    Tier 2; Tier 0 needs corroboration + an allowlist category + a recognized
    teammate; Tier 1 needs an allowlist category from the authorized owner.
    """
    verdict = (coras_read_verdict or "").strip().upper()
    high, reasons = is_high_stakes(claim_text, entity, category, entities)
    reasons = list(reasons)
    conflicts = verdict == "CONFLICTS"
    if conflicts:
        reasons.append("conflicts_canon")

    allow = category in ALLOWLIST_CATEGORIES
    corroborated = verdict == "CORROBORATED"
    teammate = contributor_recognized(contributor_id, entity)
    owner = authorized_owner(contributor_id, entity)

    if high or conflicts:
        return TIER_2, DECISION_HARRISON, reasons
    if corroborated and allow and teammate:
        return (TIER_0, DECISION_AUTO,
                reasons + ["corroborated", f"allowlist:{category}", "recognized_teammate"])
    if allow and owner:
        return (TIER_1, DECISION_OWNER,
                reasons + [f"allowlist:{category}", "authorized_owner",
                           f"verdict:{verdict or 'none'}"])
    # Tier 2 fallback -- record WHY it didn't qualify (review aid).
    if not allow:
        reasons.append(f"category_not_allowlisted:{category or 'other'}")
    elif corroborated and not teammate:
        reasons.append("corroborated_but_contributor_not_recognized")
    elif not corroborated and not owner:
        reasons.append("uncorroborated_and_not_authorized_owner")
    else:
        reasons.append("not_tier0_or_tier1")
    return TIER_2, DECISION_HARRISON, reasons


# ── Shadow record builder ──────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_preview(update: dict[str, Any]) -> str:
    """A short, PHI-rechecked description preview for the shadow log.

    The description already lives in cora-proposed-memory-updates.jsonl and the
    Harrison DM, so this adds no new exposure class -- but it is re-screened and
    blanked on any PHI hit (minimum-necessary)."""
    desc = str(update.get("description") or "")[:200]
    if not desc:
        return ""
    try:
        from .phi_guard import is_phi_risk, is_clinical_phi, is_lex_billing_status_phi
        if is_phi_risk(desc) or is_clinical_phi(desc) or is_lex_billing_status_phi(desc):
            return "[redacted]"
    except Exception:  # noqa: BLE001 -- fail-safe: blank rather than risk PHI
        return "[redacted]"
    return desc


def build_shadow_record(
    update: dict[str, Any],
    coras_read_verdict: str,
    *,
    now_iso: str | None = None,
) -> dict[str, Any]:
    """Build the `shadow_decision` record for one knowledge proposal.

    Records what graduated trust WOULD have done. Acts on nothing.
    """
    payload = update.get("payload") or {}
    utype = str(update.get("update_type", ""))
    entity = (str(payload.get("entity") or "FNDR")).strip().upper()
    text = claim_text(update)
    entities = _entities_list(update)
    contributor = contributor_id(update)
    category = categorize(text)
    verdict = (coras_read_verdict or "").strip().upper()
    if verdict not in _KNOWN_VERDICTS:
        verdict = ""  # normalize unknown / unavailable to ""

    tier, decision, reasons = classify_tier(
        coras_read_verdict=verdict,
        category=category,
        entity=entity,
        contributor_id=contributor,
        claim_text=text,
        entities=entities,
    )

    return {
        "type": "shadow_decision",
        "ts": now_iso or _now_iso(),
        "update_id": str(update.get("update_id") or ""),
        "update_type": utype,
        "entity": entity,
        "entities": entities or [],
        "category": category,
        "contributor": contributor,
        "contributor_recognized": contributor_recognized(contributor, entity),
        "authorized_owner": authorized_owner(contributor, entity),
        "coras_read_verdict": verdict,
        "conflicts": verdict == "CONFLICTS",
        "shadow_tier": tier,
        "shadow_decision": decision,
        "confidence": str(update.get("confidence") or ""),
        "reasons": reasons,
        "preview": _safe_preview(update),
    }


# ── Logging (append-only, fail-soft) ───────────────────────────────────────────

def shadow_enabled() -> bool:
    """The CORA_GRADUATED_SHADOW kill switch (default ON). The report still reads
    whatever is already on disk regardless of this flag."""
    return os.environ.get("CORA_GRADUATED_SHADOW", "1").strip().lower() not in (
        "0", "false", "no", "off"
    )


def _default_log_dir() -> Path:
    return Path(os.environ.get("CORA_GRADUATED_SHADOW_DIR") or _REPO_ROOT / "logs")


def _shadow_log_path(log_dir: Path | None = None, now: datetime | None = None) -> Path:
    az = now or datetime.now(_AZ)
    base = Path(log_dir) if log_dir is not None else _default_log_dir()
    return base / f"graduated-trust-shadow-{az.strftime('%Y-%m-%d')}.jsonl"


def record_shadow_decisions(
    items: list[dict[str, Any]],
    *,
    log_dir: Path | None = None,
    logger: logging.Logger | None = None,
) -> int:
    """Append one shadow_decision record per knowledge item. Returns count written.

    Each item is expected to carry `_coras_read_verdict` (stashed by the drain's
    _attach_coras_read); absent -> "" -> conservative (Tier 2). FAIL-SOFT: this is a
    pure side-effect on its own file and NEVER raises into the drain (the spec's
    "act on nothing" + byte-identical-DM invariant)."""
    lg = logger or log
    if not items or not shadow_enabled():
        return 0
    written = 0
    try:
        path = _shadow_log_path(log_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for it in items:
                try:
                    rec = build_shadow_record(it, str(it.get("_coras_read_verdict") or ""))
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    written += 1
                except Exception as exc:  # noqa: BLE001
                    lg.warning("graduated-shadow: record build failed for %s (%s)",
                               str(it.get("update_id", "?"))[:8], exc)
    except Exception as exc:  # noqa: BLE001
        lg.warning("graduated-shadow: decision logging failed (%s)", exc)
    return written


def record_shadow_reactions(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    log_dir: Path | None = None,
    logger: logging.Logger | None = None,
) -> int:
    """Append a shadow_reaction record for each resolved (update, reaction) pair so
    the report can correlate a would-Tier-0 item to Harrison's actual 👍/👎.

    Only APPROVED / DISMISSED are recorded. FAIL-SOFT; never raises into the drain.
    Records ALL reactions (not just Tier-0 ones) so the report can join by update_id."""
    lg = logger or log
    if not pairs or not shadow_enabled():
        return 0
    written = 0
    try:
        path = _shadow_log_path(log_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for update, reaction in pairs:
                try:
                    action = str((reaction or {}).get("action") or "")
                    if action not in ("APPROVED", "DISMISSED"):
                        continue
                    rec = {
                        "type": "shadow_reaction",
                        "ts": _now_iso(),
                        "update_id": str((update or {}).get("update_id") or ""),
                        "reaction_action": action,
                        "reaction": str((reaction or {}).get("reaction") or ""),
                    }
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    written += 1
                except Exception as exc:  # noqa: BLE001
                    lg.warning("graduated-shadow: reaction record failed (%s)", exc)
    except Exception as exc:  # noqa: BLE001
        lg.warning("graduated-shadow: reaction logging failed (%s)", exc)
    return written


# ── Reporting ──────────────────────────────────────────────────────────────────

def _iter_shadow_files(log_dir: Path | None = None):
    base = Path(log_dir) if log_dir is not None else _default_log_dir()
    if not base.exists():
        return []
    return sorted(base.glob("graduated-trust-shadow-*.jsonl"))


def _az_date_of(ts: str) -> str:
    """AZ calendar date (YYYY-MM-DD) of an ISO ts, "" on parse failure."""
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_AZ).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return ""


def build_report(log_dir: Path | None = None, *, days: int | None = None) -> dict[str, Any]:
    """Aggregate the shadow logs into stats: counts by tier, would-Tier-0 rate/week,
    and would-Tier-0 false-positive rate (Tier-0 items Harrison later thumbs-down'd).

    Read-only -- never mutates the logs. `days` (optional) limits to decisions whose
    AZ date is within the last N days.
    """
    cutoff_date: str | None = None
    if days is not None and days > 0:
        cutoff_date = (datetime.now(_AZ) - timedelta(days=days - 1)).strftime("%Y-%m-%d")

    # update_id -> latest decision record; update_id -> latest reaction action.
    decisions: dict[str, dict[str, Any]] = {}
    reactions: dict[str, str] = {}

    for path in _iter_shadow_files(log_dir):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            # A line can be valid JSON but not a dict (a bare number/string/null/
            # array from a partial write or an operator touch) -- skip it rather
            # than crash the whole report on rec.get() (adversarial review MEDIUM).
            if not isinstance(rec, dict):
                continue
            rtype = rec.get("type")
            uid = str(rec.get("update_id") or "")
            if not uid:
                continue
            if rtype == "shadow_decision":
                d = _az_date_of(rec.get("ts", ""))
                if cutoff_date and d and d < cutoff_date:
                    continue
                rec["_date"] = d
                decisions[uid] = rec  # last write wins (idempotent re-logs)
            elif rtype == "shadow_reaction":
                action = str(rec.get("reaction_action") or "")
                if action in ("APPROVED", "DISMISSED"):
                    reactions[uid] = action  # last reaction wins

    by_tier = Counter(d.get("shadow_tier") for d in decisions.values())
    by_decision = Counter(d.get("shadow_decision") for d in decisions.values())
    by_category = Counter(d.get("category") for d in decisions.values())
    by_entity = Counter(d.get("entity") for d in decisions.values())

    tier0 = [d for d in decisions.values() if d.get("shadow_tier") == TIER_0]
    tier0_ids = {d["update_id"] for d in tier0}
    tier0_reacted = [uid for uid in tier0_ids if uid in reactions]
    tier0_fp = [uid for uid in tier0_reacted if reactions[uid] == "DISMISSED"]  # wrong auto-approve
    tier0_tp = [uid for uid in tier0_reacted if reactions[uid] == "APPROVED"]
    tier0_pending = [uid for uid in tier0_ids if uid not in reactions]

    fp_rate = (len(tier0_fp) / len(tier0_reacted)) if tier0_reacted else 0.0

    # conflict rate
    conflicts = [d for d in decisions.values() if d.get("conflicts")]
    conflict_rate = (len(conflicts) / len(decisions)) if decisions else 0.0

    # date span -> would-Tier-0 per week
    dates = sorted({d.get("_date") for d in decisions.values() if d.get("_date")})
    span_days = 1
    if len(dates) >= 2:
        try:
            d0 = datetime.strptime(dates[0], "%Y-%m-%d")
            d1 = datetime.strptime(dates[-1], "%Y-%m-%d")
            span_days = max(1, (d1 - d0).days + 1)
        except Exception:  # noqa: BLE001
            span_days = max(1, len(dates))
    weeks = max(span_days / 7.0, 1 / 7.0)
    tier0_per_week = len(tier0) / weeks

    # per-ISO-week would-Tier-0 buckets (review aid)
    tier0_by_week: dict[str, int] = defaultdict(int)
    for d in tier0:
        dd = d.get("_date")
        if dd:
            try:
                iso = datetime.strptime(dd, "%Y-%m-%d").isocalendar()
                tier0_by_week[f"{iso[0]}-W{iso[1]:02d}"] += 1
            except Exception:  # noqa: BLE001
                pass

    return {
        "total_decisions": len(decisions),
        "by_tier": {str(k): v for k, v in sorted(by_tier.items(), key=lambda kv: (kv[0] is None, kv[0]))},
        "by_decision": dict(by_decision),
        "by_category": dict(by_category),
        "by_entity": dict(by_entity),
        "would_tier0": len(tier0),
        "would_tier0_per_week": round(tier0_per_week, 2),
        # The per-week figure is an extrapolation; on <1wk of data it is volatile
        # (1 day of 3 -> "21/week"). The flag lets format_report mark it provisional
        # so the raw total + FP rate (the load-bearing numbers) aren't misread.
        "would_tier0_rate_provisional": span_days < 7,
        "would_tier0_reacted": len(tier0_reacted),
        "would_tier0_pending": len(tier0_pending),
        "would_tier0_true_positives": len(tier0_tp),
        "would_tier0_false_positives": len(tier0_fp),
        "would_tier0_false_positive_rate": round(fp_rate, 4),
        "would_tier0_false_positive_ids": sorted(tier0_fp),
        "conflicts": len(conflicts),
        "conflict_rate": round(conflict_rate, 4),
        "date_span_days": span_days,
        "dates": dates,
        "tier0_by_iso_week": dict(sorted(tier0_by_week.items())),
        "reactions_seen": len(reactions),
    }


def format_report(stats: dict[str, Any]) -> str:
    """Human-readable rendering of build_report() output."""
    if stats.get("would_tier0_rate_provisional"):
        rate_line = (
            f"Would-Tier-0:                {stats['would_tier0']} in "
            f"{stats['date_span_days']}d  (PROVISIONAL -- <1wk of data; "
            f"~{stats['would_tier0_per_week']}/wk extrapolated)"
        )
    else:
        rate_line = (
            f"Would-Tier-0 rate:           {stats['would_tier0_per_week']} / week "
            f"({stats['would_tier0']} total)"
        )
    lines = [
        "=" * 64,
        "Cora graduated-trust SHADOW report (acts on nothing)",
        "=" * 64,
        f"Decisions logged:            {stats['total_decisions']}"
        + (f"  over {stats['date_span_days']}d "
           f"({stats['dates'][0]} .. {stats['dates'][-1]})" if stats.get("dates") else ""),
        "",
        "By tier (what graduated trust WOULD have done):",
        f"  Tier 0 would-auto-approve:   {stats['by_tier'].get('0', 0)}",
        f"  Tier 1 would-route-to-owner: {stats['by_tier'].get('1', 0)}",
        f"  Tier 2 harrison (today):     {stats['by_tier'].get('2', 0)}",
        "",
        rate_line,
        "",
        "Would-Tier-0 accuracy (vs Harrison's real reaction):",
        f"  reacted-on:                {stats['would_tier0_reacted']}"
        f"  (pending: {stats['would_tier0_pending']})",
        f"  Harrison APPROVED (ok):    {stats['would_tier0_true_positives']}",
        f"  Harrison DISMISSED (FP):   {stats['would_tier0_false_positives']}",
        f"  FALSE-POSITIVE RATE:       {stats['would_tier0_false_positive_rate'] * 100:.1f}%"
        "   (target ~0% before any flip)",
    ]
    if stats.get("would_tier0_false_positive_ids"):
        lines.append("  FP update_ids:             "
                     + ", ".join(stats["would_tier0_false_positive_ids"]))
    lines += [
        "",
        f"Conflicts flagged:           {stats['conflicts']}  "
        f"({stats['conflict_rate'] * 100:.1f}% of decisions)",
        "",
        "Would-Tier-0 by category:    "
        + (", ".join(f"{k}={v}" for k, v in sorted(stats["by_category"].items())) or "(none)"),
        "By entity:                   "
        + (", ".join(f"{k}={v}" for k, v in sorted(stats["by_entity"].items())) or "(none)"),
    ]
    if stats.get("tier0_by_iso_week"):
        lines.append("Tier-0 by ISO week:          "
                     + ", ".join(f"{k}={v}" for k, v in stats["tier0_by_iso_week"].items()))
    lines += [
        "",
        "NOTE: shadow only. Nothing here was auto-approved. The flip is a partial",
        "reversal of WS17-C / touches D-011 -- Harrison's explicit call from this data.",
        "=" * 64,
    ]
    return "\n".join(lines)
