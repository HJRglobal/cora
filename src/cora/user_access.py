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

import functools
import logging
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

_PERMISSIONS_PATH = (
    Path(__file__).parent.parent.parent / "data" / "maps" / "user-permissions.yaml"
)

_HARRISON_ID = "U0B2RM2JYJ1"


@functools.lru_cache(maxsize=1)
def _load_permissions() -> dict[str, Any]:
    try:
        with open(_PERMISSIONS_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data.get("users", {})
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


def blocked_topics(user_id: str) -> list[str]:
    """Return the list of sensitive topics blocked for this user."""
    if user_id == _HARRISON_ID:
        return []
    users = _load_permissions()
    entry = users.get(user_id, {})
    return entry.get("sensitive_topics_blocked", [])


def check_access(user_id: str, entity: str, user_message: str) -> str | None:
    """Full access check. Returns a redirect message string if blocked, None if allowed.

    Checks:
      1. Entity authorization — is the user allowed to ask about this entity?
      2. Sensitive topic detection — is the question about a blocked topic?

    Returns None (pass) or a one-sentence redirect (block).
    """
    # Entity check
    if not is_authorized(user_id, entity):
        users = _load_permissions()
        entry = users.get(user_id, {})
        name = entry.get("name", "you")
        allowed = entry.get("allowed_entities", ["FNDR", "HJRG"])
        if isinstance(allowed, list) and allowed:
            entity_hint = allowed[0]
            return (
                f"I can only assist with {entity_hint} topics in your authorized channels."
            )
        return "That entity is outside your access scope."

    # Sensitive topic check
    blocked = blocked_topics(user_id)
    if not blocked:
        return None

    msg_lower = user_message.lower()

    topic_patterns = {
        "financials": [
            "p&l", "profit", "loss", "revenue", "cash flow", "balance sheet",
            "financial", "budget", "spend", "cost", "expense", "income",
            "ebitda", "margin", "qbo", "quickbooks", "invoice", "payroll",
        ],
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
        patterns = topic_patterns.get(topic, [])
        if any(p in msg_lower for p in patterns):
            redirects = {
                "financials": "Financial questions go to Harrison or Justin.",
                "hr": "HR matters go to Hannah Grant or Harrison.",
                "legal": "That's a legal matter. Reach Emily Stubbs.",
                "phi": "Client-specific health info stays in the EHR. Ask the clinical lead.",
                "cap_table": "Ownership details need Harrison.",
            }
            return redirects.get(topic, "That topic is outside your access scope here.")

    return None
