"""D-326 IN CODE for the dead-channel lane (Code #16 C1, amendment A12).

A row is STAGED at T1 -- an Archive button that will act -- only when ALL of:
  * the LIVE ladder registry ranks ``slack-channel-archive`` at T1 or above (the bot
    never writes the registry; a ``promoted`` event is a Code/Cowork commit), and
  * ``policy.acting_tier() == "T1"`` (the exact flag word ``act``, not demoted), and
  * the scope for the row's channel type is KNOWN present (public: channels:manage;
    private: groups:write). An unknown grant is not a grant.
Otherwise the row stages T0 ("Mark to archive"): a tap records Harrison's agreement.

At TAP time a T1 row archives only if the same three still hold, with the registry
re-read and the scopes probed with ``force=True``. The split between refusals is
deliberate (the design review's MEDIUM on stranded buttons):
  * RECORD (write ``agreed``, the row resolves): the lane is demoted, the registry
    now says T0, or the scope is KNOWN missing -- his intent is recorded, and the
    reply says exactly why nothing was archived;
  * TRANSIENT (write nothing, buttons stay): the registry could not be read, the flag
    is not ``act`` in THIS process (the .env was edited but the bot not restarted),
    or the scope probe was inconclusive.
"""
from __future__ import annotations

from typing import Any

from . import policy

LANE = "slack-channel-archive"
SCOPE_PUBLIC = "channels:manage"
SCOPE_PRIVATE = "groups:write"

ARCHIVE, RECORD, TRANSIENT = "archive", "record", "transient"


def registry_allows_t1() -> bool | None:
    """True when the live registry row ranks >= T1, False when it ranks T0, None when
    the registry (or the row) cannot be read."""
    try:
        from .. import ladder_registry as lr  # noqa: PLC0415
        reg = lr.load()
        if not reg.get("available"):
            return None
        tier = lr.tier_of(LANE, reg)
        if tier is None:
            return None
        return int(lr.TIER_RANK.get(tier, 0)) >= 1
    except Exception:  # noqa: BLE001
        return None


def required_scope(is_private: bool) -> str:
    return SCOPE_PRIVATE if is_private else SCOPE_PUBLIC


def scope_state(scopes: frozenset | None, is_private: bool) -> str:
    """"present" | "missing" | "unknown"."""
    if scopes is None:
        return "unknown"
    return "present" if required_scope(is_private) in scopes else "missing"


def granted_scopes(client: Any, *, force: bool = False) -> frozenset | None:
    try:
        from ..tools.slack_file_upload import granted_scopes as _gs  # noqa: PLC0415
        return _gs(client, force=force)
    except Exception:  # noqa: BLE001
        return None


def stage_tier(is_private: bool, *, scopes: frozenset | None, registry_t1: bool | None,
               acting: str) -> str:
    if registry_t1 is True and acting == "T1" and scope_state(scopes, is_private) == "present":
        return "T1"
    return "T0"


def t0_reason() -> str:
    """Why a T0 row's tap recorded instead of archiving -- tier-truthful."""
    if policy.acting_tier() == "T1" and registry_allows_t1() is True:
        return "this card was a T0 card"
    if policy.is_demoted():
        return "the lane is demoted (the nightly monitor found an archive it could not attribute to a tap)"
    return "the lane is at T0"


DEMOTED_AFTER_CARD = ("the lane was demoted after this card went out (the nightly monitor "
                      "found an archive it could not attribute to a tap)")
#: A T1 row tapped through a button that was DRAWN as "Mark to archive" (A12): the
#: label on screen promised a record, so a record is all it does.
MARK_BUTTON = ("the button you tapped was a Mark button — it only records (the card was drawn "
               "while the lane acted at T0)")


def tap_gate(row: dict, client: Any, *, scopes: frozenset | None = None,
             probe_scopes: bool = True, demoted_after_card: bool = False) -> tuple[str, str]:
    """(ARCHIVE | RECORD | TRANSIENT, reason) for a tap on *row*. ``demoted_after_card``
    is the card's A12 demotion HISTORY (``Proposal.demoted``): a demotion newer than
    the card keeps its T1 rows T0-equivalent even after Harrison cleared it."""
    if row.get("tier") != "T1":
        return RECORD, t0_reason()
    if policy.is_demoted() or demoted_after_card:
        return RECORD, DEMOTED_AFTER_CARD
    reg_ok = registry_allows_t1()
    if reg_ok is None:
        return TRANSIENT, "the ladder registry could not be read"
    if reg_ok is False:
        return RECORD, "the ladder registry says the lane is at T0"
    if policy.mode() != "act":
        return TRANSIENT, "the archive switch is not on in the running bot"
    if probe_scopes:
        scopes = granted_scopes(client, force=True)
    state = scope_state(scopes, bool(row.get("is_private")))
    if state == "unknown":
        return TRANSIENT, "I could not confirm my Slack scopes just now"
    if state == "missing":
        scope = required_scope(bool(row.get("is_private")))
        return RECORD, (f"the Slack scope {scope} is missing — Harrison adds it in the Slack app "
                        "config and reinstalls")
    return ARCHIVE, ""


def pre_notice_check() -> tuple[bool, str]:
    """The no-network re-check IMMEDIATELY before the in-channel notice (A12)."""
    if policy.is_demoted():
        return False, "the lane was demoted a moment ago"
    if policy.mode() != "act":
        return False, "the archive switch went off a moment ago"
    if registry_allows_t1() is not True:
        return False, "the ladder registry no longer says T1"
    return True, ""
