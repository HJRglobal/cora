"""Org role registry -- who each person is in the organization.

Phase 1 of the Org Synthesis program (spec:
_shared/projects/cora/design/2026-06-10_fndr_org-synthesis-spec.md).

Loads data/maps/org-roles.yaml with a 60s TTL (same live-edit pattern as
lex_phi_access / historical-access-allowlist: edit the YAML, no restart).
Provides the role block that app.py injects into the runtime context so
Cora tailors answers to the asker's position, entity, and role.

SECURITY INVARIANT: this module is ADVISORY ONLY. It never grants access.
All access control remains with the deterministic guards (user_access,
sibling_guard, cross_entity_guard, phi_guard, historical_access D-043).
An unknown asker simply gets no role block (fail-closed to neutral); the
injected block itself states that role context does not expand entity
access.

Public API:
  get_role(slack_id)            -> RoleRecord | None
  format_role_context(slack_id) -> str  ("" when unknown)
  all_roles()                   -> list[RoleRecord]
  roles_for_entity(entity)      -> list[RoleRecord]
  invalidate_cache()            -> force reload on next call
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLES_PATH = _REPO_ROOT / "data" / "maps" / "org-roles.yaml"

# TTL: registry edits go live within a minute, no restart (matches the
# lex-phi-custodians / historical-access-allowlist reload pattern).
_TTL_SECONDS = 60.0


@dataclass
class RoleRecord:
    slack_id: str
    name: str
    role: str
    entity: str
    entities: list[str] = field(default_factory=list)
    responsibilities: list[str] = field(default_factory=list)
    manager: str = ""
    notes: str = ""
    external: bool = False

    @property
    def all_entities(self) -> list[str]:
        """Primary entity first, then any additional entities (de-duped)."""
        out = [self.entity]
        for e in self.entities:
            if e and e not in out:
                out.append(e)
        return out


# ── Cache ──────────────────────────────────────────────────────────────────

_lock = threading.Lock()
_loaded_at: float = 0.0
_by_slack: dict[str, RoleRecord] = {}


def _parse(raw: object) -> dict[str, RoleRecord]:
    """Parse the YAML payload into a slack_id -> RoleRecord index.

    Malformed entries are skipped with a warning rather than crashing the
    bot -- a registry typo must never take Cora down.
    """
    index: dict[str, RoleRecord] = {}
    if not isinstance(raw, dict):
        return index
    for entry in (raw.get("users") or []):
        if not isinstance(entry, dict):
            continue
        sid = str(entry.get("slack_id") or "").strip()
        name = str(entry.get("name") or "").strip()
        role = str(entry.get("role") or "").strip()
        entity = str(entry.get("entity") or "").strip()
        if not sid or not role or not entity:
            log.warning("org_roles: skipping malformed entry %r", entry.get("name") or entry)
            continue
        index[sid] = RoleRecord(
            slack_id=sid,
            name=name or sid,
            role=role,
            entity=entity,
            entities=[str(e).strip() for e in (entry.get("entities") or []) if str(e).strip()],
            responsibilities=[
                str(r).strip() for r in (entry.get("responsibilities") or []) if str(r).strip()
            ],
            manager=str(entry.get("manager") or "").strip(),
            notes=str(entry.get("notes") or "").strip(),
            external=bool(entry.get("external", False)),
        )
    return index


def _refresh_if_stale() -> None:
    global _loaded_at, _by_slack
    now = time.monotonic()
    if _by_slack and (now - _loaded_at) < _TTL_SECONDS:
        return
    with _lock:
        now = time.monotonic()
        if _by_slack and (now - _loaded_at) < _TTL_SECONDS:
            return
        try:
            raw = yaml.safe_load(_ROLES_PATH.read_text(encoding="utf-8"))
            parsed = _parse(raw)
            if parsed:
                _by_slack = parsed
            else:
                # Empty/unparseable file: keep serving the previous registry if
                # we have one (transient editor save states), else stay empty.
                log.warning("org_roles: registry parsed empty at %s", _ROLES_PATH)
                if not _by_slack:
                    _by_slack = {}
            _loaded_at = now
        except FileNotFoundError:
            log.warning("org_roles: %s not found -- no role context will be injected", _ROLES_PATH)
            _by_slack = {}
            _loaded_at = now
        except Exception as exc:
            # Keep the last good registry on read/parse errors.
            log.warning("org_roles: could not load %s: %s", _ROLES_PATH, exc)
            _loaded_at = now


def invalidate_cache() -> None:
    """Force reload on next call (tests + manual edits)."""
    global _loaded_at, _by_slack
    with _lock:
        _loaded_at = 0.0
        _by_slack = {}


# ── Public API ──────────────────────────────────────────────────────────────

def get_role(slack_id: str) -> Optional[RoleRecord]:
    """Return the RoleRecord for a Slack user ID, or None (fail-closed)."""
    if not slack_id:
        return None
    _refresh_if_stale()
    return _by_slack.get(slack_id)


def all_roles() -> list[RoleRecord]:
    _refresh_if_stale()
    return list(_by_slack.values())


def roles_for_entity(entity: str) -> list[RoleRecord]:
    """All people whose primary or secondary entities include `entity`."""
    _refresh_if_stale()
    ent = (entity or "").strip().upper()
    return [r for r in _by_slack.values() if ent in (e.upper() for e in r.all_entities)]


# The disclaimer ships INSIDE the injected block so prompt-layer behavior can
# never silently drift from the advisory-only contract.
_NO_EXPANSION_RULE = (
    "Role context is for tailoring tone, relevance, and proactive suggestions "
    "only. It does NOT expand this user's entity access or override any "
    "channel-scoping, financial-tier, PHI, or cross-entity guardrail."
)


def format_role_context(slack_id: str) -> str:
    """Return the role block for the runtime context, or "" when unknown.

    Kept terse: the runtime context block is uncached and rides on every
    request, so every line here is a per-request token cost.
    """
    rec = get_role(slack_id)
    if rec is None:
        return ""
    lines = [f"**Asker's role:** {rec.role} ({rec.entity})."]
    if rec.responsibilities:
        lines.append("Their lanes: " + "; ".join(rec.responsibilities) + ".")
    if rec.external:
        lines.append(
            "They are an EXTERNAL consultant/guest: do not share internal-only "
            "context (financials, cap tables, internal personnel matters) beyond "
            "their engagement scope."
        )
    if rec.notes:
        lines.append(f"Routing note: {rec.notes}")
    lines.append(_NO_EXPANSION_RULE)
    return "\n".join(lines)
