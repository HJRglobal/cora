"""Send-trust ladder loader + global kill switch (R2).

The LLM never decides whether something may send; this module does, from
data/maps/send-trust.yaml + the CORA_SEND_LIVE env kill switch. Fail-closed
everywhere: a missing/broken config, an unknown playbook, a tier-2 entry, or
an unknown mailbox all resolve to Tier 0 (draft-only).

v1 mailbox universe is a CODE constant: adding a send mailbox requires both a
config change AND a code change Harrison merges (structural, not procedural).

Kill-switch semantics: the bot process reads env at start (a flip needs a
restart, the code_queue_level lesson); scripts read per-fire. `off` beats
everything, including an already-approved card (test-pinned).
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger("cora.revops.send_trust")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CONFIG_PATH = _REPO_ROOT / "data" / "maps" / "send-trust.yaml"
_OWNERS_PATH = _REPO_ROOT / "data" / "maps" / "revops-owners.yaml"

# The ONLY mailboxes any tier-1 playbook may send from in v1. Expanding this
# set is a code change (deliberately), on top of the YAML allowlist.
V1_MAILBOX_UNIVERSE = frozenset({"harrison@hjrglobal.com"})

# Tiers the loader accepts. Tier 2 exists in the config SCHEMA only; any
# playbook configured tier 2 is HARD-REJECTED (dropped + error logged).
_ACCEPTED_TIERS = frozenset({0, 1})

_TTL_SECONDS = 60.0
_cache: dict[str, Any] = {"ts": 0.0, "playbooks": None}
_owners_cache: dict[str, Any] = {"ts": 0.0, "owners": None}


class PlaybookConfig:
    """Validated, immutable view of one playbook entry."""

    __slots__ = (
        "playbook_id",
        "tier",
        "mailbox_allowlist",
        "approvers",
        "recipient_class",
        "template_ref",
        "min_silence_days",
        "max_nudges",
    )

    def __init__(
        self,
        playbook_id: str,
        tier: int,
        mailbox_allowlist: frozenset[str],
        approvers: tuple[str, ...],
        recipient_class: str,
        template_ref: str,
        min_silence_days: int,
        max_nudges: int,
    ) -> None:
        self.playbook_id = playbook_id
        self.tier = tier
        self.mailbox_allowlist = mailbox_allowlist
        self.approvers = approvers
        self.recipient_class = recipient_class
        self.template_ref = template_ref
        self.min_silence_days = min_silence_days
        self.max_nudges = max_nudges


def _validate_playbook(pid: str, raw: dict[str, Any]) -> Optional[PlaybookConfig]:
    try:
        tier = int(raw.get("tier", 0))
    except (TypeError, ValueError):
        logger.error("send-trust: playbook %s has non-integer tier; REJECTED", pid)
        return None
    if tier not in _ACCEPTED_TIERS:
        logger.error(
            "send-trust: playbook %s configured tier %s; tier 2+ is HARD-REJECTED in v1",
            pid,
            tier,
        )
        return None
    raw_mailboxes = [str(m).strip().lower() for m in raw.get("mailbox_allowlist") or []]
    unknown = [m for m in raw_mailboxes if m not in V1_MAILBOX_UNIVERSE]
    if unknown:
        logger.error(
            "send-trust: playbook %s lists mailbox(es) outside the v1 universe %s; REJECTED",
            pid,
            unknown,
        )
        return None
    if tier == 1 and not raw_mailboxes:
        logger.error("send-trust: tier-1 playbook %s has empty mailbox allowlist; REJECTED", pid)
        return None
    approvers = tuple(str(a).strip() for a in raw.get("approvers") or [] if str(a).strip())
    if tier == 1 and not approvers:
        logger.error("send-trust: tier-1 playbook %s has no approvers; REJECTED", pid)
        return None
    recipient_class = str(raw.get("recipient_class") or "thread_participants_only")
    if recipient_class != "thread_participants_only":
        # v1 structural invariant: reply-only, recipients subset of thread.
        logger.error(
            "send-trust: playbook %s recipient_class %r unsupported in v1; REJECTED",
            pid,
            recipient_class,
        )
        return None
    nudge = raw.get("nudge") or {}
    try:
        min_silence = int(nudge.get("min_silence_days", 7))
        max_nudges = int(nudge.get("max_nudges", 2))
    except (TypeError, ValueError):
        min_silence, max_nudges = 7, 2
    return PlaybookConfig(
        playbook_id=pid,
        tier=tier,
        mailbox_allowlist=frozenset(raw_mailboxes),
        approvers=approvers,
        recipient_class=recipient_class,
        template_ref=str(raw.get("template_ref") or "design/playbooks/revops"),
        min_silence_days=min_silence,
        max_nudges=max_nudges,
    )


def load_playbooks(force: bool = False) -> dict[str, PlaybookConfig]:
    """Load + validate send-trust.yaml. Invalid entries are DROPPED (fail-closed
    to Tier 0); a broken/missing file yields an empty dict (everything Tier 0)."""
    now = time.time()
    if (
        not force
        and _cache["playbooks"] is not None
        and now - _cache["ts"] < _TTL_SECONDS
    ):
        return _cache["playbooks"]
    playbooks: dict[str, PlaybookConfig] = {}
    try:
        raw = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
        for pid, entry in (raw.get("playbooks") or {}).items():
            if not isinstance(entry, dict):
                logger.error("send-trust: playbook %s entry malformed; REJECTED", pid)
                continue
            cfg = _validate_playbook(str(pid), entry)
            if cfg is not None:
                playbooks[cfg.playbook_id] = cfg
    except FileNotFoundError:
        logger.error("send-trust: %s missing; ALL playbooks Tier 0", _CONFIG_PATH)
    except Exception:  # noqa: BLE001 - fail closed
        logger.exception("send-trust: config load failed; ALL playbooks Tier 0")
        playbooks = {}
    _cache["playbooks"] = playbooks
    _cache["ts"] = now
    return playbooks


def get_playbook(playbook_id: Optional[str]) -> Optional[PlaybookConfig]:
    if not playbook_id:
        return None
    return load_playbooks().get(playbook_id)


def effective_tier(playbook_id: Optional[str]) -> int:
    """The tier the ladder actually grants right now (kill switch applied).

    CORA_SEND_LIVE=off (or unset, or any unrecognized value) forces Tier 0 for
    every playbook, including one with an approved card in flight.
    """
    if send_live_mode() != "tier1":
        return 0
    cfg = get_playbook(playbook_id)
    if cfg is None:
        return 0
    return min(cfg.tier, 1)


def send_live_mode() -> str:
    """'off' | 'tier1'. Anything unrecognized is 'off' (fail-closed)."""
    val = os.environ.get("CORA_SEND_LIVE", "off").strip().lower()
    return "tier1" if val == "tier1" else "off"


def is_approver(playbook_id: Optional[str], slack_user_id: Optional[str]) -> bool:
    cfg = get_playbook(playbook_id)
    if cfg is None or not slack_user_id:
        return False
    return slack_user_id in cfg.approvers


def mailbox_allowed(playbook_id: Optional[str], mailbox: Optional[str]) -> bool:
    cfg = get_playbook(playbook_id)
    if cfg is None or not mailbox:
        return False
    mb = mailbox.strip().lower()
    return mb in cfg.mailbox_allowlist and mb in V1_MAILBOX_UNIVERSE


# ---------------------------------------------------------------------------
# Owner routing (revops-owners.yaml)
# ---------------------------------------------------------------------------

_DEFAULT_OWNER = "U0B2RM2JYJ1"  # Harrison


def owner_for_workstream(workstream: Optional[str]) -> str:
    now = time.time()
    if _owners_cache["owners"] is None or now - _owners_cache["ts"] >= _TTL_SECONDS:
        owners: dict[str, str] = {}
        default = _DEFAULT_OWNER
        try:
            raw = yaml.safe_load(_OWNERS_PATH.read_text(encoding="utf-8")) or {}
            default = str(raw.get("default_owner") or _DEFAULT_OWNER)
            for ws, uid in (raw.get("workstreams") or {}).items():
                if uid:
                    owners[str(ws)] = str(uid)
        except Exception:  # noqa: BLE001 - fail closed to Harrison
            logger.exception("revops-owners load failed; routing all to default owner")
            owners = {}
        _owners_cache["owners"] = owners
        _owners_cache["default"] = default
        _owners_cache["ts"] = now
    owners = _owners_cache["owners"] or {}
    return owners.get(workstream or "", _owners_cache.get("default", _DEFAULT_OWNER))


def clear_caches() -> None:
    """Test hook."""
    _cache["playbooks"] = None
    _cache["ts"] = 0.0
    _owners_cache["owners"] = None
    _owners_cache["ts"] = 0.0
