"""LEX PHI custodian access gate for Cora.

Compliance-critical. Governs the ONE narrow case where Cora is permitted to
surface Lexington (LEX) Protected Health Information: a fixed allowlist of
authorized custodians, asking inside LEX scope.

Decision (Harrison directive, Universal Session Capture spec 2026-06-09):
  - LEX/PHI sessions are captured + stored in full in the LEX-scoped KB.
  - Cora surfaces LEX PHI ONLY when BOTH:
        (a) the requester's Slack ID is on the custodian allowlist, AND
        (b) the request is in a designated LEX channel OR a DM with that user.
  - Everyone else: the existing PHI refusal stands.
  - LEX PHI NEVER surfaces in a non-LEX channel for ANY user.
  - sibling_guard + cross_entity_guard remain fully enforced. This gate only
    relaxes the `phi` topic block within LEX scope; it NEVER opens cross-entity
    flow.

FAIL-CLOSED by design: unknown user, unresolved/empty config, or non-LEX
channel  ->  phi_allowed() returns False (refuse).

This module GRANTS nothing on its own — it returns a boolean that the existing
user_access.check_access() consults. It cannot bypass the entity-authorization
check, the sibling guard, or the cross-entity guard.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

_CUSTODIANS_PATH = (
    Path(__file__).parent.parent.parent / "data" / "maps" / "lex-phi-custodians.yaml"
)

# Simple TTL cache — reload at most once per 60s. Never cache an empty/failed
# load (same anti-pattern fix as user_access._load_permissions).
_cache: frozenset[str] = frozenset()
_loaded_at: float = 0.0
_TTL = 60.0  # seconds


def _load_custodian_ids() -> frozenset[str]:
    """Load the custodian Slack-ID allowlist with a 60s TTL cache.

    Returns an empty frozenset on any error (fail-closed: nobody is a custodian).
    """
    global _cache, _loaded_at
    now = time.monotonic()
    if _cache and (now - _loaded_at) < _TTL:
        return _cache
    try:
        with open(_CUSTODIANS_PATH, encoding="utf-8") as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}
        ids = {
            str(c["slack_id"]).strip()
            for c in data.get("custodians", [])
            if isinstance(c, dict) and c.get("slack_id")
        }
        result = frozenset(ids)
        if result:  # only cache a non-empty result
            _cache = result
            _loaded_at = now
        return result
    except FileNotFoundError:
        log.warning("lex-phi-custodians.yaml not found — no PHI custodians (fail-closed)")
        return frozenset()
    except Exception as exc:  # noqa: BLE001 — fail-closed by design
        log.error("Failed to load lex-phi-custodians.yaml: %s", exc)
        return frozenset()


def is_custodian(user_id: str) -> bool:
    """Return True if *user_id* is on the LEX PHI custodian allowlist."""
    if not user_id:
        return False
    return user_id in _load_custodian_ids()


def _is_lex_scope(channel_entity: str | None) -> bool:
    """True if the channel's entity is LEX or a LEX sub-entity (LEX-LLC, etc.)."""
    if not channel_entity:
        return False
    e = channel_entity.upper()
    return e == "LEX" or e.startswith("LEX-")


def phi_allowed(user_id: str, channel_entity: str | None, is_dm: bool = False) -> bool:
    """Return True only if Cora may surface LEX PHI for this request.

    FAIL-CLOSED. Requires BOTH:
      1. user_id is on the custodian allowlist, AND
      2. the request is in LEX scope — a LEX/LEX-* channel, OR a DM with the user.

    A non-LEX channel ALWAYS returns False, even for a custodian, so LEX PHI can
    never surface outside LEX scope. DMs are LEX scope only for custodians (a
    custodian DM is, by definition, a DM with an authorized custodian).
    """
    if not is_custodian(user_id):
        return False
    if is_dm:
        # A direct message handler only reaches here for the messaging user;
        # the custodian check above already restricts this to allowlisted users.
        return True
    return _is_lex_scope(channel_entity)
