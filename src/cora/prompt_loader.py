"""Entity system prompt loader with in-memory cache (no TTL — restart to refresh)."""

import logging
from pathlib import Path

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent.parent.parent / "design" / "system-prompts"

_ENTITY_FILES: dict[str, str] = {
    "F3E":  "f3e.md",
    "LEX":  "lex.md",
    "OSN":  "osn.md",
    "BDM":  "bdm.md",
    "FNDR": "fndr.md",
    "HJRG": "fndr.md",  # HJRG uses FNDR prompt in Phase 1
}

_cache: dict[str, str] = {}


def load_prompt(entity: str) -> str:
    """Return the system prompt text for the given entity code.

    Falls back to FNDR prompt and logs ERROR when the entity file is missing.
    Raises RuntimeError if the FNDR prompt itself is missing (bot should not run).
    """
    if entity in _cache:
        return _cache[entity]

    filename = _ENTITY_FILES.get(entity, "fndr.md")
    path = _PROMPTS_DIR / filename

    if path.exists():
        text = path.read_text(encoding="utf-8")
        _cache[entity] = text
        return text

    if entity == "FNDR" or filename == "fndr.md":
        raise RuntimeError(
            f"FNDR system prompt missing at {path}. Bot cannot run without it."
        )

    log.error(
        "System prompt missing for entity %s (%s) — falling back to FNDR prompt",
        entity,
        path,
    )
    return _load_fndr_fallback()


def _load_fndr_fallback() -> str:
    if "FNDR" in _cache:
        return _cache["FNDR"]

    fndr_path = _PROMPTS_DIR / "fndr.md"
    if not fndr_path.exists():
        raise RuntimeError(
            f"FNDR system prompt missing at {fndr_path}. Bot cannot run without it."
        )

    text = fndr_path.read_text(encoding="utf-8")
    _cache["FNDR"] = text
    return text
