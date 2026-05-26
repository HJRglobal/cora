"""User identity layer — unified Slack ↔ Asana ↔ HubSpot person resolution.

Single source of truth for resolving a person across all three systems Cora
touches. Loaded lazily on first call and cached in-process (maps change rarely;
restart Cora to pick up edits to the YAML files).

Maps managed:
  data/maps/slack-to-asana.yaml    — Slack ID ↔ Asana GID
  data/maps/slack-to-hubspot.yaml  — Slack ID ↔ HubSpot owner ID
  data/maps/user-aliases.yaml      — name/nickname → canonical display_name

Public API:
  display_name(slack_user_id)     → "Hannah Grant" | slack_user_id (fallback)
  asana_gid(slack_user_id)        → "1209060959783860" | None
  slack_id_from_asana(asana_gid)  → "U0B3AEQS0NB" | None   ← KEY for @mentions
  slack_id_from_name(name)        → "U0B3AEQS0NB" | None
  hubspot_owner_id(slack_user_id) → "83346026" | None
  all_asana_gids()                → list of all known Asana GIDs
  all_users()                     → list of UserRecord
  get_user(slack_user_id)         → UserRecord | None
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ASANA_MAP   = _REPO_ROOT / "data" / "maps" / "slack-to-asana.yaml"
_HUBSPOT_MAP = _REPO_ROOT / "data" / "maps" / "slack-to-hubspot.yaml"
_ALIASES_MAP = _REPO_ROOT / "data" / "maps" / "user-aliases.yaml"

# ── Data structures ────────────────────────────────────────────────────────

@dataclass
class UserRecord:
    slack_user_id: str
    display_name: str
    asana_gid: Optional[str] = None
    asana_email: Optional[str] = None
    hubspot_owner_id: Optional[str] = None
    hubspot_email: Optional[str] = None
    # Resolved aliases (all lower-cased variants that map to this user)
    aliases: list[str] = field(default_factory=list)

    @property
    def slack_mention(self) -> str:
        """Slack mrkdwn @mention string."""
        return f"<@{self.slack_user_id}>"

    @property
    def first_name(self) -> str:
        return self.display_name.split()[0] if self.display_name else self.display_name


# ── In-process cache ───────────────────────────────────────────────────────

_lock = threading.Lock()
_cache: "_IdentityCache | None" = None


class _IdentityCache:
    """Loaded once; holds all lookup indexes."""

    def __init__(self) -> None:
        self._by_slack:  dict[str, UserRecord] = {}   # slack_id → record
        self._by_asana:  dict[str, str] = {}          # asana_gid → slack_id
        self._by_alias:  dict[str, str] = {}          # lower(alias) → slack_id
        self._load()

    # ── Loaders ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        self._load_asana_map()
        self._load_hubspot_map()
        self._load_aliases()
        log.info(
            "user_identity: loaded %d users, %d asana, %d hubspot, %d aliases",
            len(self._by_slack),
            len(self._by_asana),
            sum(1 for u in self._by_slack.values() if u.hubspot_owner_id),
            len(self._by_alias),
        )

    def _load_asana_map(self) -> None:
        try:
            data = yaml.safe_load(_ASANA_MAP.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            log.warning("user_identity: slack-to-asana.yaml not found")
            return
        except Exception as exc:
            log.warning("user_identity: could not load slack-to-asana.yaml: %s", exc)
            return

        for entry in (data.get("users") or []):
            if not isinstance(entry, dict):
                continue
            sid = entry.get("slack_user_id", "").strip()
            if not sid:
                continue
            gid = str(entry.get("asana_user_gid") or entry.get("asana_gid") or "").strip() or None
            rec = UserRecord(
                slack_user_id=sid,
                display_name=entry.get("display_name") or sid,
                asana_gid=gid,
                asana_email=entry.get("asana_email"),
            )
            self._by_slack[sid] = rec
            if gid:
                self._by_asana[gid] = sid

    def _load_hubspot_map(self) -> None:
        try:
            data = yaml.safe_load(_HUBSPOT_MAP.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            log.warning("user_identity: slack-to-hubspot.yaml not found")
            return
        except Exception as exc:
            log.warning("user_identity: could not load slack-to-hubspot.yaml: %s", exc)
            return

        for entry in (data.get("users") or []):
            if not isinstance(entry, dict):
                continue
            sid = entry.get("slack_user_id", "").strip()
            owner_id = str(entry.get("hubspot_owner_id") or "").strip() or None
            if not sid or not owner_id:
                continue
            if sid in self._by_slack:
                self._by_slack[sid].hubspot_owner_id = owner_id
                self._by_slack[sid].hubspot_email = entry.get("hubspot_email")
            else:
                # HubSpot-only user (shouldn't happen in normal config, but handle gracefully)
                self._by_slack[sid] = UserRecord(
                    slack_user_id=sid,
                    display_name=entry.get("display_name") or sid,
                    hubspot_owner_id=owner_id,
                    hubspot_email=entry.get("hubspot_email"),
                )

    def _load_aliases(self) -> None:
        try:
            data = yaml.safe_load(_ALIASES_MAP.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            log.warning("user_identity: user-aliases.yaml not found")
            return
        except Exception as exc:
            log.warning("user_identity: could not load user-aliases.yaml: %s", exc)
            return

        alias_map: dict[str, list[str]] = data.get("aliases") or {}
        # Build a reverse index: canonical display_name → slack_user_id
        name_to_sid: dict[str, str] = {
            rec.display_name.lower(): sid
            for sid, rec in self._by_slack.items()
        }

        for canonical_name, alias_list in alias_map.items():
            sid = name_to_sid.get(canonical_name.lower())
            if sid is None:
                # User in aliases but not in Asana map (e.g. Tessa after access revocation)
                log.debug("user_identity: alias canonical_name=%r not in slack-to-asana", canonical_name)
                continue
            rec = self._by_slack[sid]
            # Add canonical name itself as an alias
            self._by_alias[canonical_name.lower()] = sid
            for alias in (alias_list or []):
                self._by_alias[str(alias).lower()] = sid
                rec.aliases.append(str(alias).lower())

    # ── Lookup methods ────────────────────────────────────────────────────

    def get_by_slack(self, slack_id: str) -> Optional[UserRecord]:
        return self._by_slack.get(slack_id)

    def get_slack_id_from_asana(self, asana_gid: str) -> Optional[str]:
        return self._by_asana.get(str(asana_gid))

    def get_slack_id_from_name(self, name: str) -> Optional[str]:
        return self._by_alias.get(name.lower().strip())

    def all_records(self) -> list[UserRecord]:
        return list(self._by_slack.values())

    def all_asana_gids(self) -> list[str]:
        return list(self._by_asana.keys())


# ── Module-level helpers ───────────────────────────────────────────────────

def _get_cache() -> _IdentityCache:
    global _cache
    if _cache is None:
        with _lock:
            if _cache is None:
                _cache = _IdentityCache()
    return _cache


def invalidate_cache() -> None:
    """Force reload on next call. Call after editing YAML maps without restarting."""
    global _cache
    with _lock:
        _cache = None


# ── Public API ─────────────────────────────────────────────────────────────

def get_user(slack_user_id: str) -> Optional[UserRecord]:
    """Return the full UserRecord for a Slack user ID, or None."""
    return _get_cache().get_by_slack(slack_user_id)


def display_name(slack_user_id: str) -> str:
    """Return human display name for a Slack ID, falling back to the ID itself."""
    rec = get_user(slack_user_id)
    return rec.display_name if rec else slack_user_id


def asana_gid(slack_user_id: str) -> Optional[str]:
    """Return the Asana GID for a Slack user, or None if not mapped."""
    rec = get_user(slack_user_id)
    return rec.asana_gid if rec else None


def slack_id_from_asana(asana_user_gid: str) -> Optional[str]:
    """Reverse lookup: Asana GID → Slack user ID. Used for @mention in digests."""
    return _get_cache().get_slack_id_from_asana(str(asana_user_gid))


def slack_id_from_name(name: str) -> Optional[str]:
    """Name/alias → Slack user ID. Case-insensitive. Returns None if not found."""
    return _get_cache().get_slack_id_from_name(name)


def hubspot_owner_id(slack_user_id: str) -> Optional[str]:
    """Return the HubSpot owner ID for a Slack user, or None if not mapped."""
    rec = get_user(slack_user_id)
    return rec.hubspot_owner_id if rec else None


def slack_mention(slack_user_id: str) -> str:
    """Return Slack mrkdwn @mention string, e.g. '<@U0B3AEQS0NB>'."""
    return f"<@{slack_user_id}>"


def all_users() -> list[UserRecord]:
    """Return all known UserRecords."""
    return _get_cache().all_records()


def all_asana_gids() -> list[str]:
    """Return all known Asana GIDs (for sweep scripts that pool tasks)."""
    return _get_cache().all_asana_gids()


def resolve_person(identifier: str) -> Optional[UserRecord]:
    """Best-effort resolution: try Slack ID, then Asana GID, then name alias.

    Useful when the caller doesn't know which system the identifier comes from.
    """
    cache = _get_cache()
    # Try direct Slack ID
    rec = cache.get_by_slack(identifier)
    if rec:
        return rec
    # Try Asana GID reverse lookup
    sid = cache.get_slack_id_from_asana(identifier)
    if sid:
        return cache.get_by_slack(sid)
    # Try name/alias
    sid = cache.get_slack_id_from_name(identifier)
    if sid:
        return cache.get_by_slack(sid)
    return None
