"""Entity CLAUDE.md context loader with in-memory TTL cache.

Phase 3 augmentation: if a `query` is provided, the knowledge base is searched and
top-K relevant chunks are appended below the static context. The static portion
still uses the TTL cache; the combined output (static + KB) is never cached because
KB retrieval is query-dependent.
"""

import datetime
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
_KB_DB_PATH = _REPO_ROOT / "data" / "cora_kb.db"

# Phase 3 KB retrieval config — top-K chunks injected into context per query.
# K=8 balances signal density vs token cost. Chunks are ~500 tokens each, so
# K=8 adds ~4K tokens of KB context per query. Well within Claude's window.
_KB_TOP_K = 8
_KB_MAX_AGE_DAYS = 365
# Cosine distance threshold — anything above this is likely irrelevant noise.
# text-embedding-3-small typically returns dist 0.4-0.6 for strong matches,
# 0.8-1.0 for marginal, >1.1 for unrelated.
_KB_MAX_DISTANCE = 1.10

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


def load_context(entity: str, query: str | None = None) -> str:
    """Return CLAUDE.md text for the entity, always appending founder-level below.

    Also appends design/known-answers/{entity}.md if it exists, plus dynamic
    snapshot-based answers, plus (if `query` provided) top-K KB chunks retrieved
    via semantic search.

    The static portion is cached with a 5-minute TTL (mtime-invalidated). The
    KB portion is recomputed per query and NOT cached.
    """
    static_text = _load_static_context(entity)

    if not query:
        return static_text

    kb_section = _try_kb_retrieve(entity, query)
    if not kb_section:
        return static_text

    return static_text + "\n\n---\n\n" + kb_section


def _load_static_context(entity: str) -> str:
    """Existing static-context logic with the TTL cache. Extracted for clarity."""
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


def _try_kb_retrieve(entity: str, query: str) -> str | None:
    """Search the KB and return a formatted context block. Returns None on any failure.

    Failure modes that should return None (not raise):
    - KB db file doesn't exist (migration hasn't run)
    - OPENAI_API_KEY missing (KB embeddings disabled)
    - Network error reaching OpenAI
    - sqlite-vec query error
    - No results pass the relevance threshold

    Doctrine: KB retrieval is an UPGRADE, not a GATE. If it fails, fall back to
    static context — Cora still works without RAG.
    """
    if not _KB_DB_PATH.exists():
        log.debug("KB not initialized (no db at %s) — skipping retrieval", _KB_DB_PATH)
        return None

    try:
        from cora.knowledge_base import KnowledgeBase
        kb = KnowledgeBase(_KB_DB_PATH)
        try:
            results = kb.search(
                query,
                entity=entity,
                k=_KB_TOP_K,
                max_age_days=_KB_MAX_AGE_DAYS,
            )
        finally:
            kb.close()
    except Exception as exc:
        log.warning("KB retrieval failed for entity=%s query=%r: %s", entity, query[:60], exc)
        return None

    # Filter by relevance threshold
    relevant = [r for r in results if r.distance <= _KB_MAX_DISTANCE]
    if not relevant:
        log.info("KB returned %d chunks but none passed distance threshold %.2f",
                 len(results), _KB_MAX_DISTANCE)
        return None

    log.info(
        "KB retrieved %d chunks (of %d returned) for entity=%s — best distance=%.3f",
        len(relevant), len(results), entity,
        relevant[0].distance if relevant else 0,
    )

    return _format_kb_chunks(relevant)


def _format_kb_chunks(chunks: list) -> str:
    """Render KB SearchResult list as a context block for the system prompt."""
    lines = [
        "# Retrieved knowledge (semantically matched to user's question)",
        "",
        "(The following chunks were pulled from Cora's portfolio knowledge base via "
        "semantic vector search. They are the most relevant context to the user's "
        "question across CLAUDE.md briefs, decisions.md, project notes, and other "
        "static portfolio documentation. Use these to ground your answer — cite the "
        "source when you quote specific facts. If a chunk has a deep_link, preserve "
        "it as a Slack-mrkdwn `<url|label>` link in your reply per the Link Preservation rule.)",
        "",
    ]
    for i, r in enumerate(chunks, 1):
        # Format date if present
        date_str = ""
        if r.date_modified:
            try:
                date_str = f" — {datetime.date.fromtimestamp(r.date_modified).isoformat()}"
            except (OSError, ValueError):
                pass

        title = r.title or r.source_id

        # Wrap deep_link as Slack mrkdwn if it's a bare URL (computer:// or https://)
        if r.deep_link:
            if r.deep_link.startswith("<") and "|" in r.deep_link:
                # Already wrapped
                link_block = f" — {r.deep_link}"
            else:
                link_block = f" — <{r.deep_link}|{title}>"
        else:
            link_block = ""

        lines.append(
            f"## [{i}] {r.source} | {title} | entity={r.entity}{date_str}{link_block}"
        )
        lines.append("")
        lines.append(r.content.strip())
        lines.append("")

    return "\n".join(lines)
