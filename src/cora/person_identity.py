"""Per-person identity resolver for the involvement-dossier layer (North Star pillar 4).

Derives a single `PersonIdentity` for a teammate from the YAMLs Cora ALREADY
maintains -- it does NOT introduce a new identity map that can drift (the same
lesson as the role-briefing-config.yaml -> org-roles.yaml retirement). The
human-readable consolidation lives at `_brain/reference/team-identity-map.md`;
`scripts/check_identity_map.py` asserts the two stay in sync (roster-drift guard).

Source order (spec section 3):
  - data/maps/org-roles.yaml          role / entity / manager / external
  - data/maps/slack-to-asana.yaml     asana_gid + asana_email + email_aliases
  - data/maps/slack-to-hubspot.yaml   hubspot_owner_id
  - data/maps/user-aliases.yaml       name aliases (via user_identity for name->slack)
  - data/maps/monitored-email-accounts.yaml  DWD-eligible mailbox list (already
                                              EXCLUDES Demi's personal box)

Flags:
  - lex_staff   -- PRIMARY entity is LEX* (Shaun / Jen / Jeff / Aaron / Sara).
                   Drives the LEX PHI wall on their involvement synthesis. Defined
                   on the PRIMARY entity (not "any entity touches LEX") so a
                   cross-entity controller like Justin is NOT misclassified; his
                   incidental LEX activity is still caught by the non-LEX clinical
                   backstop in person_dossier.
  - external    -- org-roles `external: true` (Jason Dorfman).
  - exclude_personal_mailbox / exclude_maricopa -- the two narrow per-person
                   handling exclusions that have NO first-class YAML field. They
                   are LOCKED policy facts (build spec sections 5 + 10) co-located
                   with the resolver, NOT a parallel identity map: identity KEYS
                   still come only from the maintained YAMLs. Demi's personal-mailbox
                   exclusion is ALSO structural (she has no monitored mailbox -> her
                   mailboxes list is empty regardless of the flag); the flag is
                   defense-in-depth. Alina's Maricopa exclusion has no structural
                   backstop, so the flag is the control (applied at the Fireflies pull).

SECURITY INVARIANT: this module resolves IDENTITY only. It never grants access.
The founder-or-self gate lives in person_dossier / tool_dispatch.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from . import org_roles

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ASANA_MAP = _REPO_ROOT / "data" / "maps" / "slack-to-asana.yaml"
_HUBSPOT_MAP = _REPO_ROOT / "data" / "maps" / "slack-to-hubspot.yaml"
_MAILBOX_MAP = _REPO_ROOT / "data" / "maps" / "monitored-email-accounts.yaml"

# ── The two LOCKED per-person handling exclusions (spec sections 5 + 10). ──────
# Keyed by slug. Policy, not identity -- intentionally NOT a YAML map (anti-drift:
# identity keys come from the YAMLs; these two behavior flags have no YAML home).
_EXCLUDE_PERSONAL_MAILBOX_SLUGS: frozenset[str] = frozenset({"demi-bagby"})
_EXCLUDE_MARICOPA_SLUGS: frozenset[str] = frozenset({"alina-thomas"})


@dataclass
class PersonIdentity:
    slack_id: str
    name: str
    slug: str
    role: str
    entity: str
    manager: str = ""                       # resolved manager display name ("" -> Harrison)
    primary_email: str = ""                 # Google identity for DWD impersonation
    email_aliases: list[str] = field(default_factory=list)
    all_emails: list[str] = field(default_factory=list)   # primary + aliases + mailbox addrs (lowercased, unique)
    mailboxes: list[str] = field(default_factory=list)     # DWD-eligible + enabled mailbox addresses to impersonate
    asana_gid: Optional[str] = None
    hubspot_owner_id: Optional[str] = None
    lex_staff: bool = False
    external: bool = False
    exclude_personal_mailbox: bool = False
    exclude_maricopa: bool = False

    @property
    def dossier_filename(self) -> str:
        return f"{self.slug}.md"


# ── slug ────────────────────────────────────────────────────────────────────

def slugify(name: str) -> str:
    """firstname-lastname, lowercase-hyphenated -> matches `_brain/people/{slug}.md`."""
    return re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")


# ── cached YAML loads (lazy singleton; invalidate to reload) ──────────────────

_lock = threading.Lock()
_asana_by_slack: dict[str, dict] | None = None
_hubspot_by_slack: dict[str, dict] | None = None
_mailboxes_by_slack: dict[str, list[dict]] | None = None


def _load_yaml(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        log.warning("person_identity: %s not found", path)
        return {}
    except Exception as exc:  # noqa: BLE001 -- degrade gracefully, never crash the bot
        log.warning("person_identity: could not load %s: %s", path, exc)
        return {}


def _asana_map() -> dict[str, dict]:
    global _asana_by_slack
    if _asana_by_slack is None:
        with _lock:
            if _asana_by_slack is None:
                out: dict[str, dict] = {}
                for e in (_load_yaml(_ASANA_MAP).get("users") or []):
                    if isinstance(e, dict) and e.get("slack_user_id"):
                        out[str(e["slack_user_id"]).strip()] = e
                _asana_by_slack = out
    return _asana_by_slack


def _hubspot_map() -> dict[str, dict]:
    global _hubspot_by_slack
    if _hubspot_by_slack is None:
        with _lock:
            if _hubspot_by_slack is None:
                out: dict[str, dict] = {}
                for e in (_load_yaml(_HUBSPOT_MAP).get("users") or []):
                    if isinstance(e, dict) and e.get("slack_user_id"):
                        out[str(e["slack_user_id"]).strip()] = e
                _hubspot_by_slack = out
    return _hubspot_by_slack


def _mailbox_map() -> dict[str, list[dict]]:
    """slack_user_id -> list of monitored-account rows belonging to that user."""
    global _mailboxes_by_slack
    if _mailboxes_by_slack is None:
        with _lock:
            if _mailboxes_by_slack is None:
                out: dict[str, list[dict]] = {}
                for a in (_load_yaml(_MAILBOX_MAP).get("accounts") or []):
                    if not isinstance(a, dict):
                        continue
                    sid = str(a.get("slack_user_id") or "").strip()
                    if sid:
                        out.setdefault(sid, []).append(a)
                _mailboxes_by_slack = out
    return _mailboxes_by_slack


def invalidate_cache() -> None:
    """Force reload of the YAML-derived maps on next call (tests + manual edits)."""
    global _asana_by_slack, _hubspot_by_slack, _mailboxes_by_slack
    with _lock:
        _asana_by_slack = None
        _hubspot_by_slack = None
        _mailboxes_by_slack = None
    try:
        org_roles.invalidate_cache()
    except Exception:  # noqa: BLE001
        pass


# ── assembly ──────────────────────────────────────────────────────────────────

def _resolve_manager_name(manager_slack_id: str) -> str:
    """Manager slack_id -> display name. '' (the org-roles convention) -> Harrison."""
    if not manager_slack_id:
        return "Harrison Rogers"
    rec = org_roles.get_role(manager_slack_id)
    if rec and rec.name:
        return rec.name
    ent = _asana_map().get(manager_slack_id) or {}
    return str(ent.get("display_name") or manager_slack_id)


def _assemble(slack_id: str | None, rec: "org_roles.RoleRecord | None") -> PersonIdentity | None:
    """Build a PersonIdentity from an org-roles record (+ the slack-keyed maps).

    `rec` is the role record; `slack_id` may be None for a registry-only person
    (e.g. Tessa) whose dossier still exists but who has no live identity to pull.
    """
    if rec is None and not slack_id:
        return None
    name = (rec.name if rec else "") or (slack_id or "")
    if not name:
        return None
    slug = slugify(name)

    asana_entry = _asana_map().get(slack_id or "", {}) if slack_id else {}
    hubspot_entry = _hubspot_map().get(slack_id or "", {}) if slack_id else {}
    mailbox_rows = _mailbox_map().get(slack_id or "", []) if slack_id else []

    # asana_gid (slack-to-asana uses asana_user_gid; the older key is asana_gid)
    gid = str(asana_entry.get("asana_user_gid") or asana_entry.get("asana_gid") or "").strip()
    asana_gid = gid if (gid and "REPLACE" not in gid) else None

    owner_id = str(hubspot_entry.get("hubspot_owner_id") or "").strip()
    hubspot_owner_id = owner_id or None

    # Emails: primary = asana_email; aliases = email_aliases (slack-to-asana).
    primary_email = str(asana_entry.get("asana_email") or "").strip().lower()
    aliases = [str(a).strip().lower() for a in (asana_entry.get("email_aliases") or []) if a]

    # Mailboxes: DWD-eligible + enabled monitored accounts (impersonation targets).
    mailboxes: list[str] = []
    mailbox_aliases: list[str] = []
    for row in mailbox_rows:
        addr = str(row.get("email") or "").strip().lower()
        if not addr:
            continue
        for ka in (row.get("known_aliases") or []):
            if ka:
                mailbox_aliases.append(str(ka).strip().lower())
        if bool(row.get("enabled")) and bool(row.get("dwd_eligible")) and addr not in mailboxes:
            mailboxes.append(addr)

    # all_emails: union of every known address, primary first, unique-preserving.
    all_emails: list[str] = []
    for e in [primary_email, *aliases, *mailboxes, *mailbox_aliases]:
        if e and e not in all_emails:
            all_emails.append(e)
    if not primary_email and all_emails:
        primary_email = all_emails[0]

    entity = (rec.entity if rec else "") or ""
    lex_staff = entity.upper().startswith("LEX")
    external = bool(rec.external) if rec else False

    return PersonIdentity(
        slack_id=slack_id or "",
        name=name,
        slug=slug,
        role=(rec.role if rec else ""),
        entity=entity,
        manager=_resolve_manager_name(rec.manager if rec else ""),
        primary_email=primary_email,
        email_aliases=aliases,
        all_emails=all_emails,
        mailboxes=mailboxes,
        asana_gid=asana_gid,
        hubspot_owner_id=hubspot_owner_id,
        lex_staff=lex_staff,
        external=external,
        exclude_personal_mailbox=(slug in _EXCLUDE_PERSONAL_MAILBOX_SLUGS),
        exclude_maricopa=(slug in _EXCLUDE_MARICOPA_SLUGS),
    )


# ── public API ────────────────────────────────────────────────────────────────

def resolve(slack_id: str) -> Optional[PersonIdentity]:
    """Resolve a teammate by Slack user ID. Fail-closed: unknown -> None."""
    if not slack_id:
        return None
    rec = org_roles.get_role(slack_id)
    if rec is None:
        # No role record. We still resolve IF the slack-to-asana map knows them
        # (a mapped-but-unrostered user), so the tool can pull; entity/role blank.
        if slack_id not in _asana_map():
            return None
    return _assemble(slack_id, rec)


def resolve_by_name(name: str) -> Optional[PersonIdentity]:
    """Resolve by a display name / alias (uses user-aliases via user_identity)."""
    if not name:
        return None
    # Lazy import: user_identity lives under tools/ and pulls in nothing heavy,
    # but keeping the import local avoids any import-order coupling.
    try:
        from .tools import user_identity
        sid = user_identity.slack_id_from_name(name)
    except Exception as exc:  # noqa: BLE001
        log.debug("person_identity: name resolution failed for %r: %s", name, exc)
        sid = None
    if sid:
        return resolve(sid)
    # Fall back to a direct slug/exact-name scan of the roster.
    target_slug = slugify(name)
    for rec in org_roles.all_roles():
        if slugify(rec.name) == target_slug:
            return _assemble(rec.slack_id or None, rec)
    return None


def all_people() -> list[PersonIdentity]:
    """Every rostered person (incl. registry-only entries like Tessa).

    Used by the weekly refresh to iterate the roster. A registry-only person
    (no slack_id) resolves to a minimal identity with no pull keys -- the refresh
    pulls nothing for them and leaves their dossier untouched.
    """
    out: list[PersonIdentity] = []
    seen: set[str] = set()
    for rec in org_roles.all_roles():
        ident = _assemble(rec.slack_id or None, rec)
        if ident and ident.slug not in seen:
            seen.add(ident.slug)
            out.append(ident)
    return out
