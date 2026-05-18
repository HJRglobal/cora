"""Entity CLAUDE.md context loader with in-memory TTL cache."""

import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

_DRIVE_ROOT = Path("G:/My Drive/HJR-Founder-OS")

_ENTITY_PATHS: dict[str, Path] = {
    "F3E":  _DRIVE_ROOT / "02-F3-Energy" / "CLAUDE.md",
    "LEX":  _DRIVE_ROOT / "08-Lexington-Services" / "CLAUDE.md",
    "OSN":  _DRIVE_ROOT / "09-One-Stop-Nutrition" / "CLAUDE.md",
    "BDM":  _DRIVE_ROOT / "07-Big-D-Media" / "CLAUDE.md",
    "HJRG": _DRIVE_ROOT / "01-HJR-Global" / "CLAUDE.md",
}

_FOUNDER_PATH: Path = _DRIVE_ROOT / "CLAUDE.md"

_TTL = 300  # seconds

_cache: dict[str, tuple[str, float]] = {}


def load_context(entity: str) -> str:
    """Return CLAUDE.md text for the entity, always appending founder-level below.

    In-memory cache with 5-minute TTL. Falls back to founder-level only when the
    entity-specific file is missing, logging a warning.
    """
    now = time.monotonic()
    cached = _cache.get(entity)
    if cached is not None:
        text, cached_at = cached
        if now - cached_at < _TTL:
            return text

    parts: list[str] = []

    if entity != "FNDR":
        entity_path = _ENTITY_PATHS.get(entity)
        if entity_path is not None:
            if entity_path.exists():
                parts.append(entity_path.read_text(encoding="utf-8"))
            else:
                log.warning(
                    "No CLAUDE.md for entity %s at %s — falling back to founder-level only",
                    entity,
                    entity_path,
                )

    parts.append(_FOUNDER_PATH.read_text(encoding="utf-8"))

    text = "\n\n---\n\n".join(parts)
    _cache[entity] = (text, now)
    return text
