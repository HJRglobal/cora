"""Entity CLAUDE.md context loader with in-memory TTL cache.

Phase 3 augmentation: if a `query` is provided, the knowledge base is searched and
top-K relevant chunks are appended below the static context. The static portion
still uses the TTL cache; the combined output (static + KB) is never cached because
KB retrieval is query-dependent.
"""

import datetime
import logging
import threading
import time
from pathlib import Path

from cora.dynamic_answers import available_dynamic_entities, load_dynamic_answers

log = logging.getLogger(__name__)

_DRIVE_ROOT = Path("G:/My Drive/HJR-Founder-OS")

_ENTITY_PATHS: dict[str, Path] = {
    "F3E":      _DRIVE_ROOT / "02-F3-Energy" / "CLAUDE.md",
    "LEX":      _DRIVE_ROOT / "08-Lexington-Services" / "CLAUDE.md",
    # LEX sub-entity CLAUDE.md stubs — contain ONLY sub-entity-specific context.
    # These are intentionally narrow: no sibling entity financial data.
    "LEX-LLC":  _DRIVE_ROOT / "08-Lexington-Services" / "llc" / "CLAUDE.md",
    "LEX-LTS":  _DRIVE_ROOT / "08-Lexington-Services" / "lts" / "CLAUDE.md",
    "LEX-LBHS": _DRIVE_ROOT / "08-Lexington-Services" / "lbhs" / "CLAUDE.md",
    "LEX-LLA":  _DRIVE_ROOT / "08-Lexington-Services" / "lla" / "CLAUDE.md",
    "OSN":      _DRIVE_ROOT / "09-One-Stop-Nutrition" / "CLAUDE.md",
    "BDM":      _DRIVE_ROOT / "07-Big-D-Media" / "CLAUDE.md",
    "HJRG":     _DRIVE_ROOT / "01-HJR-Global" / "CLAUDE.md",
}

# LEX sub-entity channels receive their own stub CLAUDE.md only — NOT the
# founder CLAUDE.md and NOT the LEX parent CLAUDE.md.
#
# Why: the founder CLAUDE.md contains the entire TOM section with financial
# data, cap tables, and ownership details for ALL portfolio entities. The LEX
# parent CLAUDE.md similarly lists ALL sub-entity cap tables. Both documents
# are the vector for cross-entity data leaking into sub-entity channels.
#
# Sub-entity stubs (08-Lexington-Services/{llc,lts,lbhs,lla}/CLAUDE.md) are
# intentionally narrow: sub-entity-specific context only, no sibling data.
# The sub-entity system prompt (design/system-prompts/{llc,lts,...}.md) carries
# the knowledge of HJR Global back-office context that Cora needs.
_NO_FOUNDER_CONTEXT: frozenset[str] = frozenset({
    "LEX-LLC", "LEX-LTS", "LEX-LBHS", "LEX-LLA",
})

_FOUNDER_PATH: Path = _DRIVE_ROOT / "CLAUDE.md"

# Founder-level entity — the ONLY cross-entity aggregator. FNDR channels may
# surface every entity's dynamic snapshots; every other entity sees ONLY its own.
_FNDR_ENTITY = "FNDR"


def _allowed_snapshot_entities(entity: str) -> list[str]:
    """Return the dynamic-snapshot folders whose answers may load for `entity`.

    Entity-scope firewall for dynamic snapshots. Each snapshot folder
    (design/known-answers/dynamic/{E}) belongs to exactly one entity. A
    snapshot must NEVER surface in a sibling entity's context — e.g. F3E cash
    position or sales pipeline must not appear in an OSN, LEX, BDM, HJRP, or UFL
    channel, even when the startup prewarm has already loaded it for F3E.

    - FNDR: the founder-level aggregator — may see every entity's snapshots.
    - Any other entity (incl. LEX sub-entities): sees ONLY its own snapshots.
      A LEX sub-entity with no dynamic folder of its own simply gets none; it
      never inherits sibling or parent snapshots.
    """
    if entity == _FNDR_ENTITY:
        return available_dynamic_entities()
    return [entity]


def _load_scoped_dynamic_answers(entity: str) -> str:
    """Load only the dynamic answers this entity is permitted to see.

    Concatenates the rendered dynamic answers for each allowed snapshot folder
    (see _allowed_snapshot_entities). Returns "" when no allowed snapshot
    produces content.
    """
    parts = [load_dynamic_answers(snap) for snap in _allowed_snapshot_entities(entity)]
    return "\n\n".join(p for p in parts if p)

_REPO_ROOT = Path(__file__).parent.parent.parent
_KNOWN_ANSWERS_DIR = _REPO_ROOT / "design" / "known-answers"
_KB_DB_PATH = _REPO_ROOT / "data" / "cora_kb.db"

# Phase 3 KB retrieval config — top-K chunks injected into context per query.
# K=8 balances signal density vs token cost. Chunks are ~500 tokens each, so
# K=8 adds ~4K tokens of KB context per query. Well within Claude's window.
_KB_TOP_K = 8
_KB_MAX_AGE_DAYS = 365
# Cosine distance threshold — anything above this is likely irrelevant noise.
# Tuned 2026-05-19 based on Phase 3A+B smoke-test data: text-embedding-3-small
# returns higher absolute distances than initial estimates assumed. Real
# relevant matches across portfolio queries run 0.85-1.10 (not 0.4-0.6 as
# initially guessed). Bumping threshold 1.10 → 1.30 captures meaningful
# matches without flooding context with noise. >1.30 is genuinely unrelated.
# Revisit after Phase 3C eval suite collects measured precision/recall data.
_KB_MAX_DISTANCE = 1.30

# ── Shared KB instance ──────────────────────────────────────────────────────
# One long-lived KnowledgeBase (and its sqlite connection) is shared across all
# request threads and the startup prewarm thread, instead of opening + closing a
# fresh connection per request. This (a) lets the prewarm actually warm the
# connection the request path uses, and (b) stops the per-request schema-init
# work + log line. The connection is created check_same_thread=False; all access
# is serialized through _SHARED_KB_LOCK (KB searches are ~ms, so serializing is
# cheap and far simpler than a per-thread pool).
_shared_kb = None  # type: ignore[var-annotated]
_SHARED_KB_LOCK = threading.Lock()


def get_shared_kb():
    """Return the process-wide shared KnowledgeBase, creating it on first use.

    Returns None if the KB db doesn't exist yet (migration hasn't run) or if
    construction fails — callers must treat KB retrieval as a non-fatal upgrade.
    """
    global _shared_kb
    if _shared_kb is not None:
        return _shared_kb
    with _SHARED_KB_LOCK:
        if _shared_kb is not None:
            return _shared_kb
        if not _KB_DB_PATH.exists():
            return None
        try:
            from cora.knowledge_base import KnowledgeBase
            _shared_kb = KnowledgeBase(_KB_DB_PATH, check_same_thread=False)
        except Exception as exc:  # noqa: BLE001
            log.warning("shared KB init failed (non-fatal): %s", exc)
            return None
    return _shared_kb

_KNOWN_ANSWERS_PATHS: dict[str, Path] = {
    "F3E":  _KNOWN_ANSWERS_DIR / "f3e.md",
    "LEX":  _KNOWN_ANSWERS_DIR / "lex.md",
    "OSN":  _KNOWN_ANSWERS_DIR / "osn.md",
    "BDM":  _KNOWN_ANSWERS_DIR / "bdm.md",
    "HJRG": _KNOWN_ANSWERS_DIR / "fndr.md",
    "FNDR": _KNOWN_ANSWERS_DIR / "fndr.md",
}

_TTL = 300  # seconds

# LEX sub-entity channels route to e.g. "LEX-LLC"; the KB stores documents under "LEX".
# Map sub-entity codes → their parent entity so KB searches hit the right rows.
_LEX_PARENT: dict[str, str] = {
    "LEX-LLC":  "LEX",
    "LEX-LTS":  "LEX",
    "LEX-LBHS": "LEX",
    "LEX-LLA":  "LEX",
}

# (content, cached_at, known_answers_mtime | None)
_cache: dict[str, tuple[str, float, float | None]] = {}


def _known_answers_mtime(entity: str) -> float | None:
    path = _KNOWN_ANSWERS_PATHS.get(entity)
    if path is None or not path.exists():
        return None
    return path.stat().st_mtime


def load_context_parts(
    entity: str,
    query: str | None = None,
    skip_kb: bool = False,
    kb_k: int | None = None,
    query_vec: list[float] | None = None,
) -> tuple[str, str]:
    """Return (static_text, kb_text) for the entity, kept as separate strings.

    static_text: the deterministic per-entity portfolio context — entity
      CLAUDE.md + founder CLAUDE.md + known-answers + dynamic snapshots. This is
      mtime-stable and TTL-cached (see _load_static_context). It is the block the
      caching split in claude_client caches as block 2 of the system array.
    kb_text: the query-specific top-K KB chunks, or "" when there is no query,
      KB is skipped, or retrieval finds nothing past the distance threshold.
      This is the volatile, never-cached portion.

    Splitting the two lets callers cache the large static mass (the founder
    CLAUDE.md alone is ~30K tokens) while keeping the per-query KB chunks in an
    uncached block. load_context() composes them back for the legacy contract.

    query_vec: pre-computed embedding for `query`. When provided, forwarded to
    _try_kb_retrieve so store.search() can skip its own embed_query() call.
    Saves one OpenAI API round-trip per request.
    """
    static_text = _load_static_context(entity)

    if not query or skip_kb:
        return static_text, ""

    effective_k = kb_k if kb_k is not None else _KB_TOP_K
    kb_section = _try_kb_retrieve(entity, query, k=effective_k, query_vec=query_vec) or ""
    return static_text, kb_section


def load_context(
    entity: str,
    query: str | None = None,
    skip_kb: bool = False,
    kb_k: int | None = None,
    query_vec: list[float] | None = None,
) -> str:
    """Return CLAUDE.md text for the entity, always appending founder-level below.

    Also appends design/known-answers/{entity}.md if it exists, plus dynamic
    snapshot-based answers, plus (if `query` provided) top-K KB chunks retrieved
    via semantic search.

    The static portion is cached with a 5-minute TTL (mtime-invalidated). The
    KB portion is recomputed per query and NOT cached.

    Thin wrapper over load_context_parts() that joins the static + KB portions
    into the single-string contract this function has always returned. When
    there is no KB portion it returns the cached static object verbatim (so the
    TTL cache-identity invariant holds).
    """
    static_text, kb_text = load_context_parts(
        entity, query=query, skip_kb=skip_kb, kb_k=kb_k, query_vec=query_vec
    )
    if not kb_text:
        return static_text
    return static_text + "\n\n---\n\n" + kb_text


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

    # LEX sub-entity channels are firewalled from the founder context.
    # The founder CLAUDE.md and LEX parent CLAUDE.md both contain cross-entity
    # financial data (cap tables, cash flow, ownership) for ALL sub-entities.
    # Sub-entity channels must not receive that data — their own stub CLAUDE.md
    # is the only entity context they get.
    if entity not in _NO_FOUNDER_CONTEXT:
        parts.append(_FOUNDER_PATH.read_text(encoding="utf-8"))

    # Append static known-answers if available
    ka_path = _KNOWN_ANSWERS_PATHS.get(entity)
    if ka_path is not None and ka_path.exists():
        ka_content = ka_path.read_text(encoding="utf-8").strip()
        if ka_content:
            parts.append("# Known Answers (from prior gap reviews)\n\n" + ka_content)
    else:
        log.info("no known-answers file for entity %s", entity)

    # Append dynamic answers interpolated from snapshots — entity-scoped so a
    # sibling entity's snapshot (e.g. F3E cash position) can never leak into
    # this context. FNDR is the only entity that aggregates across all.
    dynamic = _load_scoped_dynamic_answers(entity)
    if dynamic:
        parts.append("# Dynamic Known Answers (refreshed from snapshots)\n\n" + dynamic)

    text = "\n\n---\n\n".join(parts)
    ka_mtime = _known_answers_mtime(entity)
    _cache[entity] = (text, now, ka_mtime)
    return text


def _try_kb_retrieve(entity: str, query: str, k: int = _KB_TOP_K, query_vec: list[float] | None = None) -> str | None:
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

    kb = get_shared_kb()
    if kb is None:
        return None

    try:
        # LEX sub-entity channels (e.g. "LEX-LLC") store KB docs under parent entity "LEX".
        kb_entity = _LEX_PARENT.get(entity, entity)
        sub_entity_scope = entity if entity in _LEX_PARENT else None
        # LEX sub-entity channels must NOT receive FNDR-entity KB chunks.
        # The founder CLAUDE.md is indexed under entity=FNDR and contains
        # cross-entity financial data for all portfolio entities.
        include_fndr = entity not in _NO_FOUNDER_CONTEXT
        # Shared connection — serialize access (KB searches are ms-scale).
        with _SHARED_KB_LOCK:
            results = kb.search(
                query,
                entity=kb_entity,
                k=k,
                max_age_days=_KB_MAX_AGE_DAYS,
                include_fndr=include_fndr,
                sub_entity=sub_entity_scope,
                query_vec=query_vec,
            )
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
