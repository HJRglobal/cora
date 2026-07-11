"""Deterministic, pre-LLM dashboard -> Slack-surface access guard.

Each Cowork dashboard is readable only from its allowed Slack surface(s). This
module is the single enforcement point: every dashboard read tool calls
``check_dashboard_access(dashboard_id, slack_user_id, channel_name)`` FIRST and
returns the refusal string verbatim if it is non-None. Same class of guard as
``cross_entity_guard`` / the finance-receipt gate -- code-level, not prompt-only
(D-034).

FAIL-CLOSED by construction:
  * missing / unparseable YAML  -> empty map    -> every dashboard refuses
  * unlisted dashboard id       -> refuse (no existence leak)
  * empty / unknown channel     -> refuse
  * personal dashboards have empty channel/entity lists -> a DM from a listed
    dm_user is the ONLY pass.

Channel matching uses the RESOLVED channel NAME (the QA tool loop threads
``_channel_name`` and never ``_channel_id`` on the Q&A path -- D-052 / Slice-F).
The entity-class allowance is gated on ``entity_router.is_mapped`` so an unmapped
channel can never fall through ``route()``'s "FNDR" default into a founder-scoped
dashboard.

No-existence-leak: refusal copy is generic and never names the dashboard, its
backing store, the platform, or the allowed channels.

Loader mirrors ``lex_phi_access`` / ``finance_receipts``: 60s TTL, never cache an
empty / failed load (so a transient bad read is retried, not pinned).

PHI note: no LEX dashboard is mapped here today. Any FUTURE LEX dashboard entry
MUST additionally gate on ``lex_phi_access`` custodianship -- this channel-scope
guard is NOT a PHI custodian gate.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import yaml

from .entity_router import is_mapped, route

log = logging.getLogger(__name__)

_ACCESS_PATH = (
    Path(__file__).parent.parent.parent / "data" / "maps" / "dashboard-access.yaml"
)

# Sensitivities that get the "personal / DM" refusal copy.
_PERSONAL_SENS = frozenset({"PERSONAL", "HIGHLY_CONFIDENTIAL"})

# Generic, leak-free refusals. Never name the dashboard / store / platform / channels.
_REFUSE_PERSONAL = "I don't have that here -- ask me in a DM."
_REFUSE_ENTITY = "That's not available in this channel."

# 60s TTL cache; never cache an empty/failed load (same anti-pattern fix as
# lex_phi_access._load_custodian_ids / finance_receipts._load_config).
_cache: dict[str, dict[str, Any]] = {}
_loaded_at: float = 0.0
_TTL = 60.0  # seconds


def _load_access_map() -> dict[str, dict[str, Any]]:
    """Load the dashboard -> surface allowlist, keyed by dashboard id.

    Returns an empty dict on ANY error (fail-closed: every dashboard refuses).
    Each normalized entry has: sensitivity (upper), dm_users (frozenset of ids),
    allow_channels (frozenset of lowercased bare names), allow_entities (frozenset
    of upper entity codes).
    """
    global _cache, _loaded_at
    now = time.monotonic()
    if _cache and (now - _loaded_at) < _TTL:
        return _cache
    try:
        with open(_ACCESS_PATH, encoding="utf-8") as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}
        raw = data.get("dashboards", {})
        result: dict[str, dict[str, Any]] = {}
        for dash_id, entry in (raw or {}).items():
            if not isinstance(entry, dict):
                continue
            result[str(dash_id)] = {
                "sensitivity": str(entry.get("sensitivity", "")).strip().upper(),
                "dm_users": frozenset(
                    str(u).strip() for u in (entry.get("dm_users") or []) if u
                ),
                "allow_channels": frozenset(
                    str(c).strip().lstrip("#").lower()
                    for c in (entry.get("allow_channels") or [])
                    if c
                ),
                "allow_entities": frozenset(
                    str(e).strip().upper() for e in (entry.get("allow_entities") or []) if e
                ),
                # Backing-store config (folder/files/base/tables/title) for the
                # reader tools -- documentation-as-config, NOT part of the gate.
                "store": entry.get("store") if isinstance(entry.get("store"), dict) else {},
            }
        if result:  # never cache an empty/failed load
            _cache = result
            _loaded_at = now
        return result
    except FileNotFoundError:
        log.warning("dashboard-access.yaml not found -- all dashboards refuse (fail-closed)")
        return {}
    except Exception as exc:  # noqa: BLE001 -- fail-closed by design
        log.error("Failed to load dashboard-access.yaml: %s", exc)
        return {}


def _norm_channel(channel_name: str) -> str:
    return (channel_name or "").strip().lstrip("#").lower()


def check_dashboard_access(
    dashboard_id: str, slack_user_id: str, channel_name: str
) -> str | None:
    """Return a refusal STRING if access is denied, or None if it is allowed.

    The refusal, when non-None, is the complete user-facing message and leaks
    nothing about whether the dashboard exists.
    """
    dashboards = _load_access_map()
    entry = dashboards.get(dashboard_id)
    if not entry:
        # Unlisted dashboard OR fail-closed empty map -> refuse, surface-neutral.
        return _REFUSE_ENTITY

    refusal = (
        _REFUSE_PERSONAL if entry["sensitivity"] in _PERSONAL_SENS else _REFUSE_ENTITY
    )

    ch = _norm_channel(channel_name)
    if not ch:
        return refusal

    if ch == "dm":
        if slack_user_id and slack_user_id in entry["dm_users"]:
            return None
        return refusal

    # Named channel: explicit channel allow, OR a MAPPED channel whose routed
    # entity is allowed (is_mapped() blocks catch-all fall-through fail-open).
    if ch in entry["allow_channels"]:
        return None
    if entry["allow_entities"] and is_mapped(ch) and route(ch) in entry["allow_entities"]:
        return None
    return refusal


def store_for(dashboard_id: str) -> dict[str, Any]:
    """Return the raw backing-store config for a dashboard (folder / files / base
    / tables / title), or {} if unknown. Read-only convenience for the reader
    tools -- this is NOT an access check; callers gate with
    ``check_dashboard_access`` first."""
    return dict(_load_access_map().get(dashboard_id, {}).get("store", {}) or {})


def invalidate_cache() -> None:
    """Test hook: drop the TTL cache."""
    global _cache, _loaded_at
    _cache = {}
    _loaded_at = 0.0
