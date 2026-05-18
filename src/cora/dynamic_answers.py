"""Dynamic known-answers loader — interpolates snapshot data into per-entity templates."""

import json
import logging
import time
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).parent.parent.parent
_DYNAMIC_DIR = _REPO_ROOT / "design" / "known-answers" / "dynamic"

_TTL = 300  # seconds

# entity -> (text, cached_at, fingerprint)
_cache: dict[str, tuple[str, float, float]] = {}


def _load_snapshot(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    return yaml.safe_load(text) or {}


def snapshot_fingerprint(entity: str) -> float:
    """Sum of mtimes for all YAML answer files + referenced snapshot files.

    Returns 0.0 if the entity dynamic directory doesn't exist.
    Called on every cache-validity check so keep it cheap (small YAML files only).
    """
    entity_dir = _DYNAMIC_DIR / entity
    if not entity_dir.exists():
        return 0.0

    total = 0.0
    for yaml_path in entity_dir.glob("*.yaml"):
        try:
            total += yaml_path.stat().st_mtime
            raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
            snap_rel = raw.get("snapshot_path")
            if snap_rel:
                snap_path = _REPO_ROOT / snap_rel
                if snap_path.exists():
                    total += snap_path.stat().st_mtime
        except Exception:
            pass
    return total


def load_dynamic_answers(entity: str) -> str:
    """Return interpolated dynamic answers for entity; empty string if none exist.

    Scans design/known-answers/dynamic/{entity}/*.yaml. Each file must have:
      topic, template, fallback, snapshot_path, source.staleness_threshold_hours.

    Uses fallback + logs WARNING when snapshot is missing or stale.
    Logs ERROR and skips the answer when YAML is malformed or a template key is absent.
    In-memory cache with 5-minute TTL + mtime-based invalidation.
    """
    now = time.monotonic()
    cached = _cache.get(entity)
    if cached is not None:
        text, cached_at, fp = cached
        if now - cached_at < _TTL and snapshot_fingerprint(entity) == fp:
            return text

    entity_dir = _DYNAMIC_DIR / entity
    if not entity_dir.exists():
        return ""

    parts: list[str] = []

    for yaml_path in sorted(entity_dir.glob("*.yaml")):
        try:
            raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            log.error("dynamic_answers: malformed YAML %s: %s", yaml_path.name, exc)
            continue

        if not isinstance(raw, dict):
            log.error("dynamic_answers: %s is not a dict, skipping", yaml_path.name)
            continue

        topic = raw.get("topic", yaml_path.stem)
        template = raw.get("template", "")
        fallback = raw.get("fallback", "")
        snap_rel = raw.get("snapshot_path")
        threshold_hours = float((raw.get("source") or {}).get("staleness_threshold_hours", 24))

        if not snap_rel:
            log.warning("dynamic_answers: no snapshot_path in %s/%s", entity, yaml_path.name)
            if fallback:
                parts.append(fallback)
            continue

        snap_path = _REPO_ROOT / snap_rel

        if not snap_path.exists():
            log.warning(
                "dynamic_answers: snapshot missing for %s/%s at %s, using fallback",
                entity,
                topic,
                snap_path,
            )
            if fallback:
                parts.append(fallback)
            continue

        age_hours = (time.time() - snap_path.stat().st_mtime) / 3600.0
        if age_hours > threshold_hours:
            log.warning(
                "dynamic_answers: stale snapshot for %s/%s (%.1fh old, threshold %.0fh), using fallback",
                entity,
                topic,
                age_hours,
                threshold_hours,
            )
            if fallback:
                parts.append(fallback)
            continue

        try:
            snap_data = _load_snapshot(snap_path)
        except Exception as exc:
            log.error(
                "dynamic_answers: failed to load snapshot for %s/%s: %s",
                entity,
                topic,
                exc,
            )
            if fallback:
                parts.append(fallback)
            continue

        try:
            rendered = template.format(**snap_data)
        except KeyError as exc:
            log.warning(
                "dynamic_answers: template key %s missing in snapshot for %s/%s, using fallback",
                exc,
                entity,
                topic,
            )
            if fallback:
                parts.append(fallback)
            continue

        parts.append(rendered)

    text = "\n\n".join(parts)
    _cache[entity] = (text, now, snapshot_fingerprint(entity))
    return text
