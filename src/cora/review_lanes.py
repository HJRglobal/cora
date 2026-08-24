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
the 89 `expired_unrouted` dismissals was a mechanical type. On the LIVE ledger
the mechanical lane is exactly the silent-age-out population. (Stated as scope,
not as an absolute: the 19.5k-row archive holds 83 `decision_capture` and 13
bare-`generic` age-outs from 2026-07. The decision ones predate Fork 4's
never-expire rule and cannot recur; the `generic` mechanism is still armed and
is deliberately left alone -- see the note on it in run_knowledge_review.)

WHAT THIS MODULE DOES AND DOES NOT DO.
It classifies an update into a lane and answers "may this actor approve this
item". It grants nothing on its own and it cannot be used to widen authority:

  * D-011 IS STRUCTURAL HERE, NOT CONFIGURED. `can_approve` returns True for a
    non-Harrison actor ONLY when the lane is MECHANICAL. The judgment lane
    (known_answer / efficiency / lexicon / an #info-for-cora contribution) and
    the decision lane write or stage CANON, and no entry in any YAML file can
    express permission for them. Adding a name to knowledge-approvers.yaml
    therefore cannot hand out canon authority even by mistake.
  * LEX CONTENT IS HARRISON-ONLY, and the test for it is deliberately not the
    entity field alone. `payload.entity` is absent on 116 of the 124 live
    PENDING mechanical rows -- two of the three reconciliation passes that
    produce them never set it -- so a `startswith("LEX")` test on its own read
    "unknown" as "not LEX" and therefore as delegable. An unknown entity is now
    a REFUSAL, and the decision lane's own four-predicate content screen runs on
    top of it, catching rows whose description says "mentioned in fireflies
    (LEX)" while their payload says nothing at all.
  * The file ships with Harrison as the only approver, AND the whole surface is
    behind CORA_MECHANICAL_REVIEW (default off). Both halves are checked here,
    so a name sitting in the YAML ahead of the flip confers nothing.

FAIL-CLOSED: an unreadable/empty/malformed file leaves Harrison as the only
approver, which is exactly today's behaviour.
"""

from __future__ import annotations

import logging
import os
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

# How long an unrouted operational/mechanical row waits before its review
# deadline passes. Lives HERE rather than in run_knowledge_review because three
# processes now need the same answer: the review run (escalation + dry-run
# preview) and flywheel_metrics, which reports the backlog to both health
# surfaces. A second copy is exactly the drift `_KNOWLEDGE_TYPES` already needs
# a pinning test to police.
OPERATIONAL_UNROUTED_EXPIRY_DAYS = 14

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
    """The item's entity, upper-cased ('' when absent or unusable).

    isinstance-guarded on the payload: `can_approve` is called from
    correlate_reactions_to_updates, which is the first un-wrapped statement of
    the review run, so one malformed ledger row must not take the whole run
    down (D-051 lens-2). Before this change `correlate` never touched payload
    at all.
    """
    payload = (update or {}).get("payload")
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("entity") or "").strip().upper()


def mechanical_review_enabled() -> bool:
    """on | off (default). The mechanical review surface's kill switch.

    Lives HERE, not in run_knowledge_review, because the bot process needs it
    too: without a shared definition, `is_review_approver` in app.py's reaction
    handler was ungated, so adding a name to the approver YAML started logging
    that person's reactions on every Cora message anywhere even with the surface
    off -- i.e. the two halves of the documented "deliberate two-part flip" were
    not independent in code (D-051 lens-2 LOW). Read per call,
    whitelist-validated, fail-closed to off -- the CORA_CONFIRM_BUTTONS idiom.
    """
    return (os.environ.get("CORA_MECHANICAL_REVIEW", "off") or "off").strip().lower() == "on"


def content_screen_excludes(update: dict | None) -> tuple[bool, str]:
    """(excluded, reason) -- the DECISION lane's own screen, applied to a
    mechanical row.

    D-051 lens-4 HIGH. The mechanical card renders `description`, which
    reconciliation_engine builds as `'... -- {source} says: "{evidence[:200]}"'`
    -- up to 200 characters lifted VERBATIM out of a gmail / slack / fireflies
    KB chunk. Sending that to a delegated approver is a new egress of a third
    party's mailbox content to someone who is not its owner, with none of the
    Tier-1 ownership treatment D-043 requires. The decision lane already faces
    exactly this risk and answers it with a four-predicate fail-closed screen;
    running the SAME function here means one implementation rather than a second
    one that drifts, and it catches what `payload.entity` cannot:

      * lex_token -- "mentioned in fireflies (LEX)" in the description of a row
        whose payload carries no entity at all (21 such PENDING rows measured);
      * phi / qa -- content predicates the entity field says nothing about.

    Fail-closed by inheritance: screen_decision returns (True, "screen_error")
    on any exception.
    """
    try:
        from .decision_inbox import screen_decision
        return screen_decision(update or {})
    except Exception:  # noqa: BLE001 -- unavailable screen must exclude, never admit
        log.warning("review_lanes: content screen unavailable -- excluding", exc_info=True)
        return True, "screen_error"


def can_approve(update: dict | None, actor_id: str) -> bool:
    """May `actor_id` approve or dismiss this ledger row?

    Harrison: always. Anyone else: only while the surface is enabled, only a
    MECHANICAL item, only one whose entity is KNOWN and non-LEX, only one the
    content screen clears, and only while they are listed in
    knowledge-approvers.yaml. There is no code path and no configuration by
    which a non-Harrison actor reaches the judgment, decision or operational
    lanes -- that is what keeps D-011 intact while the mechanical surface is
    delegable.

    THE ENTITY CHECK FAILS CLOSED, and that is the correction the D-051 review
    forced (lens-2/3/4 all found it independently). The first cut asked
    `item_entity(update).startswith("LEX")`, which reads a MISSING entity as
    "not LEX" and therefore as delegable. Measured on the live ledger: 116 of
    124 PENDING mechanical rows carry no `payload.entity` at all -- pass 2
    (`stale_hubspot_deal`) and pass 4 (`stale_open_task`) in
    reconciliation_engine never set it -- so the guarantee this module's own
    docstring stated, "granting the mechanical surface to a non-custodian can
    never expose a LEX item", was false for 94% of the population. Two of those
    rows target Asana project 1215470944114390 = [LEX-LLC] Operations -- General,
    and a 👍 on one would have CLOSED a LEX-LLC task. An absent entity is an
    unknown, and an unknown on a PHI boundary is a no.
    """
    actor = str(actor_id or "").strip()
    if not actor:
        return False
    if actor == _FOUNDER_ID:
        return True
    if not mechanical_review_enabled():
        return False
    if not is_mechanical(update):
        return False
    entity = item_entity(update)
    if not entity or entity.startswith("LEX"):
        return False
    if content_screen_excludes(update)[0]:
        return False
    return actor in mechanical_approvers()


def past_review_deadline(update: dict | None, now_dt) -> bool:
    """Is this PENDING mechanical row past its review deadline?

    THE ONE deadline definition. Moved here from run_knowledge_review so the
    escalation pass, the dry-run preview and the health-surface backlog count
    cannot disagree about what "overdue" means -- a disagreement that would
    show up as an alarm firing on a different population than the one the run
    actually escalates.

    Malformed or absent timestamps read as NOT expired (fail-safe: the row stays
    pending and un-escalated rather than being acted on).
    """
    from datetime import datetime as _dt, timedelta as _td
    u = update or {}
    if u.get("state") != "PENDING" or not is_mechanical(u):
        return False
    try:
        exp = u.get("expires_at")
        if exp:
            deadline = _dt.fromisoformat(exp)
        else:
            # Pre-TTL-at-creation rows carry no expires_at; use the same
            # fallback window the expiry pass used for them.
            deadline = (_dt.fromisoformat(u["proposed_at"])
                        + _td(days=OPERATIONAL_UNROUTED_EXPIRY_DAYS))
        return now_dt >= deadline
    except Exception:  # noqa: BLE001 -- one malformed row must never raise
        return False


def is_review_approver(actor_id: str) -> bool:
    """True for anyone who might legitimately act on SOME review card.

    A cheap pre-filter for the reaction capture in app.py, which has no update
    in hand at the time it decides whether to log. It is deliberately broader
    than can_approve -- logging a reaction is not approving anything, and the
    real authorization runs per item at correlation time -- but it is NOT
    broader than the kill switch: with the surface off this is Harrison-only,
    so a name sitting in the YAML ahead of the flip changes nothing at all,
    which is what the file's own instructions promise.
    """
    actor = str(actor_id or "").strip()
    if not actor:
        return False
    if actor == _FOUNDER_ID:
        return True
    return mechanical_review_enabled() and actor in mechanical_approvers()
