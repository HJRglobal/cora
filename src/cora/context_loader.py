"""Entity CLAUDE.md context loader with in-memory TTL cache."""

import logging
import time
from pathlib import Path

from cora.dynamic_answers import load_dynamic_answers

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

_REPO_ROOT = Path(__file__).parent.parent.parent
_KNOWN_ANSWERS_DIR = _REPO_ROOT / "design" / "known-answers"

_KNOWN_ANSWERS_PATHS: dict[str, Path] = {
    "F3E":  _KNOWN_ANSWERS_DIR / "f3e.md",
    "LEX":  _KNOWN_ANSWERS_DIR / "lex.md",
    "OSN":  _KNOWN_ANSWERS_DIR / "osn.md",
    "BDM":  _KNOWN_ANSWERS_DIR / "bdm.md",
    "HJRG": _KNOWN_ANSWERS_DIR / "fndr.md",
    "FNDR": _KNOWN_ANSWERS_DIR / "fndr.md",
}

_TTL = 300  # seconds

# (content, cached_at, known_answers_mtime | None)
_cache: dict[str, tuple[str, float, float | None]] = {}


def _known_answers_mtime(entity: str) -> float | None:
    path = _KNOWN_ANSWERS_PATHS.get(entity)
    if path is None or not path.exists():
        return None
    return path.stat().st_mtime


def load_context(entity: str) -> str:
    """Return CLAUDE.md text for the entity, always appending founder-level below.

    Also appends design/known-answers/{entity}.md if it exists.
    In-memory cache with 5-minute TTL, invalidated early if the known-answers
    file is modified (mtime check). Falls back to founder-level only when the
    entity-specific CLAUDE.md is missing, logging a warning.
    """
    now = time.monotonic()
    cached = _cache.get(entity)
    if cached is not None:
        text, cached_at, ka_mtime = cached
        if now - cached_at < _TTL and _known_answers_mtime(entity) == ka_mtime:
            return text

    parts: list[str] = []

    if entity != "FNDR":
        entity_path = _ENTITY_PATHS.get(entity)
        if entity_path is not None:
            if entity_path.exists():
                parts.append(entity_path.read_text(encoding="utf-8"))
            else:
                log.warning(
                    "No CLAUDE.md for entity %s at %s -- falling back to founder-level only",
                    entity,
                    entity_path,
                )

    parts.append(_FOUNDER_PATH.read_text(encoding="utf-8"))

    # Append static known-answers if available
    ka_path = _KNOWN_ANSWERS_PATHS.get(entity)
    if ka_path is not None and ka_path.exists():
        ka_content = ka_path.read_text(encoding="utf-8").strip()
        if ka_content:
            parts.append("# Known Answers (from prior gap reviews)\n\n" + ka_content)
    else:
        log.info("no known-answers file for entity %s", entity)

    # Append dynamic answers interpolated from snapshots
    dynamic = load_dynamic_answers(entity)
    if dynamic:
        parts.append("# Dynamic Known Answers (refreshed from snapshots)\n\n" + dynamic)

    text = "\n\n---\n\n".join(parts)
    ka_mtime = _known_answers_mtime(entity)
    _cache[entity] = (text, now, ka_mtime)
    return text
