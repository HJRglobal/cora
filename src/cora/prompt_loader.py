"""Entity system prompt loader with in-memory cache (no TTL — restart to refresh).

Voice/tone is layered on top of the entity .md file by appending a voice block
loaded from design/system-prompts/_voice.yaml. The .md file owns scope + knowledge;
the YAML owns tone. Restart the bot to refresh either.
"""

import logging
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent.parent.parent / "design" / "system-prompts"
_VOICE_YAML_FILENAME = "_voice.yaml"

_ENTITY_FILES: dict[str, str] = {
    "F3E":      "f3e.md",
    "LEX":      "lex.md",       # GM-level: #lex, #lex-leadership, #lex-finance, etc.
    "LEX-LLC":  "llc.md",       # Lexington LLC sub-entity: #llc-*
    "LEX-LTS":  "lts.md",       # Lexington Therapies sub-entity: #lts-*
    "LEX-LBHS": "lbhs.md",      # Lexington Behavioral Health: #lbhs-*
    "LEX-LLA":  "lla.md",       # Lex Life Academy sub-entity: #lla-*
    "OSN":      "osn.md",
    "BDM":      "bdm.md",
    "FNDR":     "fndr.md",
    "HJRG":     "fndr.md",      # HJRG uses FNDR prompt
    "HJRP":     "hjrp.md",      # HJR Properties entity prompt
}

_cache: dict[str, str] = {}
_voice_cache: dict | None = None

# Universal rules appended to EVERY system prompt — existing and future.
# Edit here, not in individual .md files. Restart to pick up changes.
_UNIVERSAL_RULES = """

---

## Universal response rules (non-negotiable — applies in every channel)

- **Answer only what was asked, then stop.** Give one complete, correct answer and nothing more. No elaboration, context, caveats, or "also worth noting…" unless directly asked. Let the user ask follow-ups — they will if they need more. Exception: tool outputs (financial data, sales pulse, decision queues, task lists) are presented in full without truncation.
- **Lead with the answer.** The first sentence IS the full answer for most questions. Number, status, or direction first — reasoning only if the question was clearly analytical.
- **Pleasant and brief.** Warm, helpful, and collegial — Cora is a teammate, not a search engine. But brevity IS the kindness here: a tight, accurate answer respects everyone's time more than a thorough one.
- **No filler openings.** Never start with "Sure," "Great question," "Of course," "Happy to help," or any other acknowledgment of the question. Begin with the answer.
- **No emojis.** None, in any channel.
- **Never name data sources.** No system names, file names, or sheet names in any reply. "I don't have that right now" and stop — no explanation of what you'd need to look it up.
- **Never encourage breaks, sleep, or pauses.** Harrison sets his own cadence. No "sleep on it," "take a break," or concern-coded check-ins about workload or energy.
"""


def load_prompt(entity: str) -> str:
    """Return the system prompt text for the given entity code.

    Composition: <entity .md text> + <voice block from _voice.yaml>.
    The voice block is appended so per-entity scope/knowledge in the .md
    file is read first, then tone instructions cap off the prompt.

    Falls back to FNDR prompt and logs ERROR when the entity file is missing.
    Raises RuntimeError if the FNDR prompt itself is missing (bot should not run).
    """
    if entity in _cache:
        return _cache[entity]

    filename = _ENTITY_FILES.get(entity, "fndr.md")
    path = _PROMPTS_DIR / filename

    if path.exists():
        text = path.read_text(encoding="utf-8")
        composed = _compose_with_voice(text, entity) + _UNIVERSAL_RULES
        _cache[entity] = composed
        return composed

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
    composed = _compose_with_voice(text, "FNDR") + _UNIVERSAL_RULES
    _cache["FNDR"] = composed
    return composed


def _load_voice_config() -> dict:
    """Load _voice.yaml once, cache for process lifetime. Empty dict if missing.

    Path is derived from _PROMPTS_DIR at call time so test monkeypatches of
    _PROMPTS_DIR also redirect voice loading.
    """
    global _voice_cache
    if _voice_cache is not None:
        return _voice_cache

    voice_path = _PROMPTS_DIR / _VOICE_YAML_FILENAME

    if not voice_path.exists():
        log.debug(
            "_voice.yaml not found at %s — voice block will be skipped (entity prompts run unmodified)",
            voice_path,
        )
        _voice_cache = {"defaults": {}, "entities": {}}
        return _voice_cache

    try:
        with voice_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to parse _voice.yaml: %s — voice block will be skipped", exc)
        _voice_cache = {"defaults": {}, "entities": {}}
        return _voice_cache

    _voice_cache = {
        "defaults": data.get("defaults") or {},
        "entities": data.get("entities") or {},
    }
    return _voice_cache


def _resolve_voice_block(entity: str) -> dict:
    """Resolve the merged voice block for an entity, walking inheritance.

    Inheritance chain: entity block → 'inherits' target → defaults.
    First field wins per key.
    """
    config = _load_voice_config()
    entities_block = config.get("entities", {})
    defaults_block = config.get("defaults", {})

    # Build the inheritance chain
    chain: list[dict] = []
    seen: set[str] = set()
    current = entities_block.get(entity)
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if not isinstance(current, dict):
            break
        chain.append(current)
        inherits = current.get("inherits")
        if inherits == "defaults":
            break
        if isinstance(inherits, str) and inherits in entities_block:
            current = entities_block[inherits]
        else:
            break
    chain.append(defaults_block)

    # Merge — first hit per key wins
    merged: dict = {}
    for step in chain:
        if not isinstance(step, dict):
            continue
        for key, value in step.items():
            if key == "inherits":
                continue
            if key not in merged:
                merged[key] = value

    return merged


def _compose_with_voice(prompt_text: str, entity: str) -> str:
    """Append a voice block to the entity prompt text.

    Voice block is appended after the entity .md so the prompt reads as:
    <entity scope + knowledge> → <voice + tone>. Tone instructions sit closest
    to the model's reply, which empirically improves their effect.
    """
    voice = _resolve_voice_block(entity)
    if not voice:
        return prompt_text

    voice_text = (voice.get("voice") or "").strip()
    emoji = voice.get("emoji_use", "sparingly")
    verbosity = voice.get("verbosity", "balanced")

    if not voice_text and emoji == "sparingly" and verbosity == "balanced":
        # Nothing distinctive to inject — skip the block entirely
        return prompt_text

    block_lines = [
        "",
        "---",
        "",
        "## Voice + tone (per-entity)",
        "",
    ]
    if voice_text:
        block_lines.append(voice_text)
        block_lines.append("")
    block_lines.append(f"**Emoji use:** {emoji}.")
    block_lines.append(f"**Verbosity:** {verbosity}.")
    block_lines.append("")

    return prompt_text + "\n".join(block_lines)


def clear_cache() -> None:
    """Drop the in-memory prompt + voice cache. Useful in tests and after restarts."""
    global _voice_cache
    _cache.clear()
    _voice_cache = None
