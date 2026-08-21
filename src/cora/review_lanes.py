"""Which review lane a proposed update belongs to, and who may approve it.

THE PROBLEM THIS EXISTS FOR (cq-6b014816819c, the 2026-08-19 approval recon).
Over 30 days the knowledge queue took 296 proposals and produced 13 human
decisions -- 4.4% -- of which exactly ONE was a denial. 76 items aged out with
nobody deciding and 183 were still open, the oldest at 675 hours. The measured
cause is not approver capacity: 179 of the 296 were `task_close` / `asana_task` /
`hubspot_note` -- mechanical bookkeeping with no judgment content -- and they
buried the ~85 items that genuinely need Harrison. Adding a second approver to
one 296-item list would just give two people the same unreadable list, so the
queue is split FIRST.

Measured against the live ledger while building this (337 rows): every one of
the 89 `expired_unrouted` dismissals was a mechanical type, and NONE of the
judgment types ever reached that reason. The mechanical lane is exactly the
silent-age-out population.

WHAT THIS MODULE DOES AND DOES NOT DO.
It classifies an update into a lane and answers "may this actor approve this
item". It grants nothing on its own and it cannot be used to widen authority:

  * D-011 IS STRUCTURAL HERE, NOT CONFIGURED. `can_approve` returns True for a
    non-Harrison actor ONLY when the lane is MECHANICAL. The judgment lane
    (known_answer / efficiency / lexicon / an #info-for-cora contribution) and
    the decision lane write or stage CANON, and no entry in any YAML file can
    express permission for them. Adding a name to knowledge-approvers.yaml
    therefore cannot hand out canon authority even by mistake.
  * LEX-entity items are HARRISON-ONLY regardless of the approver list. This
    mirrors the rule the owner-routing path has always applied (LEX operational
    items are never routed, for PHI), and it means granting the mechanical
    surface to a non-custodian can never expose a LEX item.
  * The file ships with Harrison as the only approver. Granting the mechanical
    surface to anyone else is a separate, deliberate flip.

FAIL-CLOSED: an unreadable/empty/malformed file leaves Harrison as the only
approver, which is exactly today's behaviour.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

_APPROVERS_PATH = (
    Path(__file__).parent.parent.parent / "data" / "maps" / "knowledge-approvers.yaml"
)

# Same fixed id as user_access._HARRISON_ID / lex_phi_access._FOUNDER_ID.
_FOUNDER_ID = "U0B2RM2JYJ1"

LANE_JUDGMENT = "judgment"
LANE_DECISION = "decision"
LANE_MECHANICAL = "mechanical"
LANE_OPERATIONAL = "operational"

# The bookkeeping types. Deliberately a literal set rather than "everything that
# is not judgment": a NEW update_type must be classified on purpose, and until
# someone does that it lands in LANE_OPERATIONAL, which is Harrison-only and
# behaves exactly as it does today. A new type silently inheriting a delegated
# surface is the failure this shape prevents.
MECHANICAL_TYPES = frozenset({"asana_task", "task_close", "hubspot_note"})

_TTL = 60.0  # seconds -- the live-reload idiom used by lex_phi_access/org_roles
_cache: tuple[str, ...] | None = None
_loaded_at: float = 0.0


def _load_mechanical_approvers() -> tuple[str, ...]:
    """Slack ids allowed to approve the MECHANICAL lane. Always contains
    Harrison. Never caches a failed load (the lex_phi_access anti-pattern fix)."""
    global _cache, _loaded_at
    now = time.monotonic()
    if _cache is not None and (now - _loaded_at) < _TTL:
        return _cache
    ids: list[str] = []
    try:
        if _APPROVERS_PATH.exists():
            data = yaml.safe_load(_APPROVERS_PATH.read_text(encoding="utf-8")) or {}
            raw = (data.get("mechanical") or []) if isinstance(data, dict) else []
            for item in raw:
                # Accept a bare id or a {slack_id:, name:} row so the file can
                # carry names for humans without a second schema.
                sid = item.get("slack_id") if isinstance(item, dict) else item
                sid = str(sid or "").strip()
                if sid:
                    ids.append(sid)
        else:
            log.info("review_lanes: no approver file at %s -- Harrison only",
                     _APPROVERS_PATH)
    except Exception as exc:  # noqa: BLE001 -- fail closed to Harrison-only
        log.warning("review_lanes: could not read %s (%s) -- Harrison only",
                    _APPROVERS_PATH, exc)
        return (_FOUNDER_ID,)
    if _FOUNDER_ID not in ids:
        ids.insert(0, _FOUNDER_ID)
    _cache = tuple(dict.fromkeys(ids))
    _loaded_at = now
    return _cache


def mechanical_approvers() -> tuple[str, ...]:
    """Public read of the mechanical-lane approver list (Harrison always in)."""
    return _load_mechanical_approvers()


def reset_cache() -> None:
    """Drop the TTL cache. For tests and for a caller that has just rewritten
    the file and needs the next read to see it."""
    global _cache, _loaded_at
    _cache = None
    _loaded_at = 0.0


def lane_for(update_type: str | None, payload: dict | None = None) -> str:
    """The review lane for one proposed update.

    Delegates the judgment test to knowledge_review.is_knowledge_update so the
    judgment/operational boundary has exactly ONE definition -- the drain's
    split, propose_update's TTL-at-creation decision and this classification
    cannot drift apart (the same single-source rule that _is_knowledge_item
    already follows).
    """
    utype = (update_type or "").strip()
    if utype == "decision_capture":
        return LANE_DECISION
    try:
        from .knowledge_review import is_knowledge_update
        if is_knowledge_update(utype, payload or {}):
            return LANE_JUDGMENT
    except Exception:  # noqa: BLE001 -- classification must never raise
        log.warning("review_lanes: judgment test unavailable", exc_info=True)
    if utype in MECHANICAL_TYPES:
        return LANE_MECHANICAL
    return LANE_OPERATIONAL


def is_mechanical(update: dict | None) -> bool:
    """True when this ledger row belongs to the mechanical review surface."""
    u = update or {}
    return lane_for(u.get("update_type"), u.get("payload")) == LANE_MECHANICAL


def item_entity(update: dict | None) -> str:
    """The item's entity, upper-cased ('' when absent)."""
    return str(((update or {}).get("payload") or {}).get("entity") or "").strip().upper()


def can_approve(update: dict | None, actor_id: str) -> bool:
    """May `actor_id` approve or dismiss this ledger row?

    Harrison: always. Anyone else: only a MECHANICAL, non-LEX item, and only
    while they are listed in knowledge-approvers.yaml. There is no code path
    and no configuration by which a non-Harrison actor reaches the judgment,
    decision or operational lanes -- that is what keeps D-011 intact while the
    mechanical surface is delegable.
    """
    actor = str(actor_id or "").strip()
    if not actor:
        return False
    if actor == _FOUNDER_ID:
        return True
    if not is_mechanical(update):
        return False
    if item_entity(update).startswith("LEX"):
        # PHI. The owner-routing path has never sent a LEX operational item to
        # a teammate; delegating this surface must not become the way one gets
        # there.
        return False
    return actor in mechanical_approvers()


def is_review_approver(actor_id: str) -> bool:
    """True for anyone who might legitimately act on SOME review card.

    A cheap pre-filter for the reaction capture in app.py, which has no update
    in hand at the time it decides whether to log. It is deliberately broader
    than can_approve -- logging a reaction is not approving anything, and the
    real authorization runs per item at correlation time.
    """
    actor = str(actor_id or "").strip()
    return bool(actor) and (actor == _FOUNDER_ID or actor in mechanical_approvers())
