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

# Root founder (Harrison) -- a PHI custodian and topic-exempt on every surface
# (user_access.check_access returns None for him regardless of any block). His DM
# entity is pinned FNDR (app._handle_dm_qa), so the LEX-scope DM gate (W2-03)
# would otherwise silently drop his standing DM PHI relaxation. Same fixed ID as
# user_access._HARRISON_ID (established single-founder pattern).
_FOUNDER_ID = "U0B2RM2JYJ1"

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
      2. the request is in LEX scope — a LEX/LEX-* channel, OR a DM whose loaded
         entity is LEX scope (the founder is carved out; see below).

    A non-LEX channel ALWAYS returns False, even for a custodian, so LEX PHI can
    never surface outside LEX scope.

    W2-03 (2026-07): the DM branch is gated on LEX scope rather than the bare
    is_dm flag. In a DM the ``channel_entity`` is the asker's org-roles PRIMARY
    (app._handle_dm_qa); every non-founder custodian is LEX-primary today, so a
    real custodian DM (entity=LEX-*) still relaxes — but gating on scope makes
    this roster-independent: a custodian whose primary is NOT LEX can no longer
    pull LEX PHI from a non-LEX DM context. The single root founder is carved out
    (topic-exempt everywhere, DM entity pinned FNDR) so his standing DM PHI
    relaxation is preserved; he is a fixed identity, not a roster entry, so this
    adds no roster-dependent exposure.
    """
    if not is_custodian(user_id):
        return False
    if _is_lex_scope(channel_entity):
        return True
    if is_dm and user_id == _FOUNDER_ID:
        return True
    return False
