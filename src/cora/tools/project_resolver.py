"""Asana Project Resolver -- route task creation to the correct project.

Loads data/maps/asana-project-map.yaml and applies a multi-tier matching
strategy to find the most-specific project GID for a given task.

Routing priority order:
  1. Hard-block check: entity is in blocked_projects list → raises BlockedProjectError
  2. UFL paused check: entity is UFL + paused=true → always returns monitor-only project
  3. Assignee rule: if assignee_gid matches a configured person → override project
  4. Meeting title pattern: Fireflies meeting title keyword match
  5. Brand rule (F3E only): Pure/Mood/Energy keyword detected → brand-specific project
     - event keywords + brand detected → brand event project
     - social keywords + brand detected → brand social project
  6. Keyword rules (ordered): first rule whose keywords overlap the task context wins
  7. catch_all_gid: fallback when nothing else matches

Usage:
    from cora.tools.project_resolver import resolve_project

    project_gid = resolve_project(
        entity="F3E",
        task_text="Send wholesale proposal to GNC regional buyer",
        assignee_gid="1213638047870465",
        meeting_title="F3E Sales Weekly Sync",
    )
    # Returns "1214824237490027"  ([F3E] Sales Pipeline — Tommy)
    # because assignee_gid matches Tommy's GID
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MAP_PATH = _REPO_ROOT / "data" / "maps" / "asana-project-map.yaml"

# Module-level cache — reloaded on import, refreshed at process startup.
# YAML changes take effect on next Cora restart.
_project_map: dict[str, Any] | None = None


class BlockedProjectError(Exception):
    """Raised when a task would be routed to a hard-blocked project."""


def _load_map() -> dict[str, Any]:
    """Load (and cache) the asana-project-map.yaml."""
    global _project_map
    if _project_map is not None:
        return _project_map
    try:
        raw = _MAP_PATH.read_text(encoding="utf-8")
        _project_map = yaml.safe_load(raw) or {}
        log.debug("project_resolver: loaded %d entities from %s",
                  len(_project_map.get("entities", {})), _MAP_PATH.name)
    except Exception as exc:
        log.error("project_resolver: failed to load %s: %s", _MAP_PATH, exc)
        _project_map = {}
    return _project_map


def _normalize(text: str) -> str:
    """Lowercase, collapse whitespace."""
    return re.sub(r"\s+", " ", text.lower().strip())


def _keywords_match(keywords: list[str], haystack: str) -> bool:
    """Return True if any keyword is found as a substring in haystack."""
    hay = _normalize(haystack)
    for kw in keywords:
        if _normalize(kw) in hay:
            return True
    return False


def _detect_brand(text: str, brand_rules: list[dict]) -> str | None:
    """Return detected brand name ('pure', 'mood', 'energy') or None."""
    for rule in brand_rules:
        detect_kws = rule.get("detect_keywords") or []
        if _keywords_match(detect_kws, text):
            return rule.get("brand")
    return None


def resolve_project(
    entity: str,
    task_text: str = "",
    assignee_gid: str | None = None,
    meeting_title: str | None = None,
) -> str | None:
    """Return the best Asana project GID for this task.

    Args:
        entity:        Entity code (e.g. "F3E", "OSN", "LEX-LLC").
        task_text:     Task name + description combined (used for keyword matching).
        assignee_gid:  Asana user GID of the intended assignee (optional).
        meeting_title: Fireflies meeting title (optional, used for meeting-title rules).

    Returns:
        Asana project GID string, or None if no project can be determined.

    Raises:
        BlockedProjectError: if the entity maps exclusively to a blocked project
                             (this should not happen in normal routing, but is a
                             safety guard for edge cases).
    """
    data = _load_map()
    entities = data.get("entities") or {}

    # Normalize entity: check exact match, then parent entity fallback
    entity_cfg = entities.get(entity)
    if entity_cfg is None:
        # Try parent entity fallback (e.g. LEX-LLC -> LEX)
        parent = entity.split("-")[0]
        entity_cfg = entities.get(parent)
        if entity_cfg is not None:
            log.debug("project_resolver: no config for %s, using parent %s", entity, parent)

    if entity_cfg is None:
        log.warning("project_resolver: no config for entity=%s -- returning None", entity)
        return None

    catch_all = entity_cfg.get("catch_all_gid") or None
    blocked = set(str(g) for g in (data.get("blocked_projects") or []))

    # ── Tier 0: UFL paused ──────────────────────────────────────────────────
    if entity_cfg.get("paused"):
        log.info("project_resolver: entity=%s is paused, routing to catch-all monitor project", entity)
        return catch_all

    # ── Tier 1: Assignee rules ───────────────────────────────────────────────
    if assignee_gid:
        for rule in (entity_cfg.get("assignee_rules") or []):
            if str(rule.get("asana_gid", "")).strip() == assignee_gid:
                project_gid = str(rule.get("project_gid", "")).strip()
                if project_gid and project_gid not in blocked:
                    log.debug("project_resolver: assignee match -> %s", project_gid)
                    return project_gid

    # ── Tier 2: Meeting title patterns ──────────────────────────────────────
    if meeting_title:
        for rule in (entity_cfg.get("meeting_title_rules") or []):
            patterns = rule.get("title_patterns") or []
            if _keywords_match(patterns, meeting_title):
                project_gid = str(rule.get("project_gid", "")).strip()
                if project_gid and project_gid not in blocked:
                    log.debug("project_resolver: meeting title match -> %s", project_gid)
                    return project_gid

    # ── Tier 3: Brand detection (F3E only) ───────────────────────────────────
    brand_rules = entity_cfg.get("brand_rules") or []
    combined_text = f"{task_text} {meeting_title or ''}"
    detected_brand: str | None = None
    if brand_rules:
        detected_brand = _detect_brand(combined_text, brand_rules)

    # ── Tier 4: Keyword rules ────────────────────────────────────────────────
    for rule in (entity_cfg.get("keyword_rules") or []):
        keywords = rule.get("keywords") or []
        if not _keywords_match(keywords, combined_text):
            continue

        # This rule matched — check if it defers to brand routing
        if rule.get("brand_detect") and brand_rules:
            if detected_brand:
                # Find the brand rule and pick event or social project
                for br in brand_rules:
                    if br.get("brand") == detected_brand:
                        # Determine event vs social based on which keyword list matched
                        event_kws = ["event", "activation", "sponsor", "sponsorship",
                                     "appearance", "booth", "pop-up", "pop up",
                                     "community event", "mma event", "fight", "athlete appearance", "gym"]
                        if _keywords_match(event_kws, combined_text):
                            gid = br.get("event_project_gid", "")
                        else:
                            gid = br.get("social_project_gid", "")
                        if gid and str(gid) not in blocked:
                            log.debug("project_resolver: brand=%s -> %s", detected_brand, gid)
                            return str(gid)
            # No brand detected — fall through to fallback GID
            fallback = rule.get("fallback_project_gid", "")
            if fallback and str(fallback) not in blocked:
                log.debug("project_resolver: brand_detect fallback -> %s", fallback)
                return str(fallback)
            continue  # try next rule

        # Normal keyword match — use the project_gid
        project_gid = str(rule.get("project_gid", "")).strip()
        if project_gid and project_gid not in blocked:
            log.debug("project_resolver: keyword match -> %s (matched keywords from rule %s)",
                      project_gid, rule.get("project_gid"))
            return project_gid

    # ── Tier 5: Catch-all ────────────────────────────────────────────────────
    if catch_all and catch_all not in blocked:
        log.debug("project_resolver: catch-all -> %s", catch_all)
        return catch_all

    log.warning("project_resolver: no project found for entity=%s -- task will be orphaned", entity)
    return None


def get_blocked_project_gids() -> set[str]:
    """Return the set of hard-blocked project GIDs (Harrison Private)."""
    data = _load_map()
    return set(str(g) for g in (data.get("blocked_projects") or []))


def is_blocked_project(project_gid: str) -> bool:
    """Return True if this project GID is in the hard-blocked list."""
    return project_gid in get_blocked_project_gids()


def reload_map() -> None:
    """Force reload of the project map from disk (for testing)."""
    global _project_map
    _project_map = None
    _load_map()
