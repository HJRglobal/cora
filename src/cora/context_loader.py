"""Entity CLAUDE.md context loader with in-memory TTL cache.

Phase 3 augmentation: if a `query` is provided, the knowledge base is searched and
top-K relevant chunks are appended below the static context. The static portion
still uses the TTL cache; the combined output (static + KB) is never cached because
KB retrieval is query-dependent.
"""

import datetime
import logging
import os
import threading
import time
from pathlib import Path

from cora import historical_access, user_notes, phi_guard, org_roles, drive_io
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

# ── Founder CLAUDE.md slimming ───────────────────────────────────────────────
# The founder CLAUDE.md is ~32K tokens, but ~93% of it (~30K) is the dynamic
# "Current State of the World" section (Top of Mind, active workstreams, recent
# decisions, delegates) that changes daily. Only the ~2.2K-token static brief
# above that marker is the stable portfolio "constitution".
#
# That dynamic section is ALSO chunked into the KB (source=static_md) and is
# already co-scanned on every non-LEX query (include_fndr=True), so injecting it
# wholesale into every entity's context is pure redundancy — retrieval surfaces
# the relevant current-state per question. So: aggregators (FNDR/HJRG) that ask
# portfolio-wide questions keep the FULL founder brief inlined; every other
# entity gets only the static brief, and leans on retrieval for the long tail.
# This also stops daily TOM edits from invalidating those entities' cached
# context block (the caching-split synergy).
_FOUNDER_DYNAMIC_MARKER = "# Current State of the World"
_FOUNDER_FULL_ENTITIES: frozenset[str] = frozenset({"FNDR", "HJRG"})


def _slim_founder(text: str) -> str:
    """Return the founder brief trimmed to its static head (everything before the
    dynamic 'Current State of the World' section), with a note that the dynamic
    portion is retrieval-served. Falls back to the full text if the marker is
    absent, so a founder-doc restructure never silently drops context.
    """
    idx = text.find(_FOUNDER_DYNAMIC_MARKER)
    if idx == -1:
        return text
    head = text[:idx].rstrip()
    return (
        head
        + "\n\n---\n\n_Portfolio Current State / Top of Mind / recent decisions are "
        "not inlined here — they live in Cora's knowledge base and are pulled in on "
        "demand. If the question needs current portfolio state, use the retrieved "
        "knowledge below; if it isn't there, say so rather than guessing._"
    )


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
# Drive-materialization (2026-06-29): the known-answers read path is env-overridable
# so it can point at Drive _brain/known-answers/ (the store Tag also reads), matching
# the write side in gap_autofill._known_answers_dir(). Read at MODULE IMPORT, so a
# change to KNOWN_ANSWERS_DIR takes effect on the next bot restart (context_loader is
# bot-loaded). Mirrors the gap_autofill `or`-fallback pattern exactly.
_KNOWN_ANSWERS_DIR = Path(os.environ.get("KNOWN_ANSWERS_DIR")
                          or _REPO_ROOT / "design" / "known-answers")
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

# Built from the canonical write map (known_answers_map.ENTITY_FILES) so the read
# side can never drift behind the write side again — the bug where gap answers for
# HJRP/UFL/F3C/HJRPROD were written to files Cora never read (WS17-B item 6).
# LEX sub-entity keys are EXCLUDED on purpose: their answers all share lex.md and
# surface only at the LEX (GM) level, never inside a sibling sub-entity channel.
from .known_answers_map import ENTITY_FILES as _ENTITY_FILES  # noqa: E402

_KNOWN_ANSWERS_PATHS: dict[str, Path] = {
    entity: _KNOWN_ANSWERS_DIR / filename
    for entity, filename in _ENTITY_FILES.items()
    if not entity.startswith("LEX-")
}

# ── G: mount resilience (2026-07-16) ─────────────────────────────────────────
# The static context (entity/founder CLAUDE.md + known-answers) lives on the local
# Google Drive (G:) mount. A transient unmount/remount must NOT freeze a request or
# the bot loop, so every G: touch here goes through drive_io. Because this is the
# interactive request path AND it has a TTL cache to fall back on, we fail FAST to
# cached context on a hiccup rather than make a user wait: the per-request mtime check
# does a single bounded attempt (retry_seconds=0), and a cache-miss build gets only a
# short ride-over window. KB retrieval (cora_kb.db, on C:) is UNAFFECTED by a G: outage,
# so Cora keeps answering from the KB even while the static brief is degraded.
_CTX_TIMEOUT_SECONDS = 5.0
_CTX_RETRY_SECONDS = 3.0

# Served as the static block when the mount is gone AND there is no cached context to
# fall back to (e.g. a G: outage spanning a restart). Deliberately minimal + honest;
# the KB retrieval block still rides alongside it in the request.
_DEGRADED_STATIC_CONTEXT = (
    "_Portfolio context is briefly unavailable (Cora's document store is "
    "reconnecting). Ground your answer in the retrieved knowledge below and general "
    "context; if that isn't enough to answer confidently, say so rather than guessing._"
)

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
    """mtime of the entity's known-answers file, or None when it has none / the file
    is absent (mount UP). Raises drive_io.DriveUnavailable if the G: mount is gone, so
    the cache-validity check can serve cached context instead of touching a dead mount.
    Single bounded attempt (retry_seconds=0) — this runs on EVERY request."""
    path = _KNOWN_ANSWERS_PATHS.get(entity)
    if path is None:
        return None
    return drive_io.stat_mtime(path, timeout=_CTX_TIMEOUT_SECONDS, retry_seconds=0)


def load_context_parts(
    entity: str,
    query: str | None = None,
    skip_kb: bool = False,
    kb_k: int | None = None,
    query_vec: list[float] | None = None,
    asker_emails: frozenset[str] | None = None,
    asker_unrestricted: bool = False,
    kb_meta: dict | None = None,
    asker_slack_id: str | None = None,
    asker_is_dm: bool = False,
    phi_custodian: bool = False,
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

    Tier-1 per-user access control (historical_access): gmail/drive_sweep
    chunks owned by someone other than the asker are header-stripped before
    they enter kb_text. asker_emails is the asker's owned-mailbox set
    (None/empty = unknown asker = FAIL-CLOSED, strip everything personal);
    asker_unrestricted=True (Harrison override) skips stripping. When kb_meta
    is provided, kb_meta["unstripped_personal"]=True signals that personal
    chunks rode through UNSTRIPPED — callers must not put the response in the
    shared semantic cache.

    Personal-note overlay (Org Synthesis Phase 5): when asker_slack_id is
    provided, the asker's own user_note chunks matching the query are
    co-retrieved (owner-filtered at the SQL layer in store.search_user_notes)
    and appended as a labeled block. Any response using them also sets
    kb_meta["unstripped_personal"]=True — same cache-skip invariant.
    asker_is_dm widens the note scope to ALL the asker's notes; channel asks
    only see notes saved in the channel's entity scope (a LEX-scoped note can
    never surface in a non-LEX channel reply).
    """
    static_text = _load_static_context(entity)

    if not query or skip_kb:
        return static_text, ""

    effective_k = kb_k if kb_k is not None else _KB_TOP_K
    kb_section = _try_kb_retrieve(
        entity, query, k=effective_k, query_vec=query_vec,
        asker_emails=asker_emails, asker_unrestricted=asker_unrestricted,
        kb_meta=kb_meta, asker_slack_id=asker_slack_id, asker_is_dm=asker_is_dm,
        phi_custodian=phi_custodian,
    ) or ""
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
    """Static-context logic with the TTL cache, hardened against a G: mount outage.

    A transient Google-Drive unmount must never freeze a request or crash the loop.
    Every G: read here goes through drive_io; on drive_io.DriveUnavailable we serve
    the last cached value (even if stale) or, if this entity was never cached, a
    minimal degraded block. This function therefore NEVER raises on a G: outage.
    """
    now = time.monotonic()
    cached = _cache.get(entity)
    if cached is not None:
        text, cached_at, ka_mtime = cached
        # The cache-validity check touches G: (known-answers mtime). If the mount is
        # briefly gone, serve the cached value rather than attempt any further G: read.
        try:
            fresh_mtime = _known_answers_mtime(entity)
        except drive_io.DriveUnavailable:
            log.warning(
                "context: G: mount unavailable checking %s known-answers mtime — "
                "serving cached static context", entity,
            )
            return text
        if now - cached_at < _TTL and fresh_mtime == ka_mtime:
            return text

    try:
        return _build_static_context(entity, now)
    except drive_io.DriveUnavailable:
        if cached is not None:
            log.warning(
                "context: G: mount unavailable building %s — serving last cached "
                "static context (may be stale); KB retrieval unaffected", entity,
            )
            return cached[0]
        log.warning(
            "context: G: mount unavailable and no cached context for %s — serving "
            "minimal degraded context; KB retrieval unaffected", entity,
        )
        return _DEGRADED_STATIC_CONTEXT


def _build_static_context(entity: str, now: float) -> str:
    """Build (and cache) the entity's static context from G:. Raises
    drive_io.DriveUnavailable if the mount is gone mid-build; a genuine missing file
    with the mount UP keeps its prior behavior (exists()->False branch, or a
    FileNotFoundError from the founder read, exactly as before)."""
    parts: list[str] = []

    if entity != "FNDR":
        entity_path = _ENTITY_PATHS.get(entity)
        if entity_path is not None:
            if drive_io.exists(entity_path, timeout=_CTX_TIMEOUT_SECONDS,
                               retry_seconds=_CTX_RETRY_SECONDS):
                parts.append(drive_io.read_text(
                    entity_path, timeout=_CTX_TIMEOUT_SECONDS,
                    retry_seconds=_CTX_RETRY_SECONDS))
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
        founder_text = drive_io.read_text(
            _FOUNDER_PATH, timeout=_CTX_TIMEOUT_SECONDS, retry_seconds=_CTX_RETRY_SECONDS)
        # Aggregators (FNDR/HJRG) keep the full brief; every other entity gets the
        # static head only and relies on KB retrieval for the dynamic current-state.
        if entity not in _FOUNDER_FULL_ENTITIES:
            founder_text = _slim_founder(founder_text)
        parts.append(founder_text)

    # Append static known-answers if available
    ka_path = _KNOWN_ANSWERS_PATHS.get(entity)
    if ka_path is not None and drive_io.exists(
        ka_path, timeout=_CTX_TIMEOUT_SECONDS, retry_seconds=_CTX_RETRY_SECONDS
    ):
        ka_content = drive_io.read_text(
            ka_path, timeout=_CTX_TIMEOUT_SECONDS, retry_seconds=_CTX_RETRY_SECONDS
        ).strip()
        if ka_content:
            parts.append("# Known Answers (from prior gap reviews)\n\n" + ka_content)
    else:
        log.info("no known-answers file for entity %s", entity)

    # Append dynamic answers interpolated from snapshots — entity-scoped so a
    # sibling entity's snapshot (e.g. F3E cash position) can never leak into
    # this context. FNDR is the only entity that aggregates across all. These are
    # LOCAL (repo) reads, unaffected by a G: outage.
    dynamic = _load_scoped_dynamic_answers(entity)
    if dynamic:
        parts.append("# Dynamic Known Answers (refreshed from snapshots)\n\n" + dynamic)

    text = "\n\n---\n\n".join(parts)
    ka_mtime = _known_answers_mtime(entity)
    _cache[entity] = (text, now, ka_mtime)
    return text


def owned_kb_search(
    query: str,
    owner_emails: frozenset[str] | None,
    financial_only: bool = False,
    k: int = 12,
    query_vec: list[float] | None = None,
    recency_first: bool = False,
) -> list:
    """Tier-2 owner-scoped KB search through the shared instance + lock.

    Thin wrapper over KnowledgeBase.search_owned so callers (app.py grant
    path) keep the shared-connection lock discipline. Returns [] when the KB
    is unavailable. PHI filtering is the caller's job (historical_access.drop_phi).
    recency_first re-orders the relevant candidates newest-first (F-21).
    """
    kb = get_shared_kb()
    if kb is None:
        return []
    with _SHARED_KB_LOCK:
        return kb.search_owned(
            query,
            owner_emails=owner_emails,
            financial_only=financial_only,
            k=k,
            query_vec=query_vec,
            recency_first=recency_first,
        )


def _note_entity_scope(entity: str) -> tuple[str, ...]:
    """Acceptable note-entity values for a CHANNEL ask (DMs pass scope=None).

    Notes store the channel entity they were saved in verbatim. Channel
    retrieval sees notes saved in that same scope plus FNDR (DM/default
    notes) — except LEX sub-entity channels, which stay firewalled from
    FNDR-scoped content just like the rest of their context.
    """
    if entity in _NO_FOUNDER_CONTEXT or entity == "FNDR":
        return (entity,)
    return (entity, "FNDR")


def _try_user_notes_overlay(
    kb,
    entity: str,
    query: str,
    query_vec: list[float] | None,
    asker_slack_id: str | None,
    asker_is_dm: bool,
    asker_unrestricted: bool,
    kb_meta: dict | None,
) -> str:
    """Retrieve the asker's own personal notes matching the query (Phase 5).

    Owner exclusion is enforced inside store.search_user_notes at the SQL
    layer — this helper only decides scope and formats the labeled block.
    Failures return "" (notes are an upgrade, never a gate)."""
    if not asker_slack_id:
        return ""
    try:
        scope = None if asker_is_dm else _note_entity_scope(entity)
        with _SHARED_KB_LOCK:
            note_results = kb.search_user_notes(
                query,
                owner_slack=asker_slack_id,
                k=user_notes.NOTE_OVERLAY_K,
                entity_scope=scope,
                unrestricted=asker_unrestricted,
                query_vec=query_vec,
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("user-note overlay failed for asker=%s: %s", asker_slack_id, exc)
        return ""
    note_results = [r for r in note_results if r.distance <= _KB_MAX_DISTANCE]
    if not note_results:
        return ""
    # Personal-note content must never enter the shared semantic cache —
    # another user's similar question would replay a private note (the
    # existing D-043 unstripped_personal invariant, reused).
    if kb_meta is not None:
        kb_meta["unstripped_personal"] = True
    log.info(
        "user-note overlay: %d note(s) for asker=%s entity=%s best=%.3f",
        len(note_results), asker_slack_id, entity, note_results[0].distance,
    )
    return user_notes.format_notes_overlay(note_results, asker_slack_id)


def _try_kb_retrieve(
    entity: str,
    query: str,
    k: int = _KB_TOP_K,
    query_vec: list[float] | None = None,
    asker_emails: frozenset[str] | None = None,
    asker_unrestricted: bool = False,
    kb_meta: dict | None = None,
    asker_slack_id: str | None = None,
    asker_is_dm: bool = False,
    phi_custodian: bool = False,
) -> str | None:
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

    # Personal-note overlay (Phase 5): the asker's own notes matching the
    # query, owner-filtered at the SQL layer. Computed independently of the
    # canonical results — a note can answer a question the org corpus can't.
    notes_block = _try_user_notes_overlay(
        kb, entity, query, query_vec, asker_slack_id, asker_is_dm,
        asker_unrestricted, kb_meta,
    )

    # Filter by relevance threshold
    relevant = [r for r in results if r.distance <= _KB_MAX_DISTANCE]
    # WS-1 gap detection: expose the retrieval outcome to the caller. Set ONLY
    # when the search actually ran (the early returns above -- missing db,
    # kb None, search exception -- leave these unset so an infra failure never
    # reads as a knowledge miss). Zero added work: the values are already
    # computed on this path.
    if kb_meta is not None:
        kb_meta["kb_search_ran"] = True
        kb_meta["kb_relevant_hits"] = len(relevant)
        kb_meta["kb_notes_hit"] = bool(notes_block)
        # WS-1 kb_miss calibration (D-066 follow-up): the closest returned
        # chunk's distance and the raw returned count, BOTH regardless of the
        # _KB_MAX_DISTANCE gate. kb_miss requires 0 relevant hits, which is
        # empirically unreachable at ~560K chunks (even orthogonal vocabulary
        # retrieves ~12 chunks well under 1.08). These are instrumentation only
        # -- NOT a gate change -- so a week of real best-distance data can
        # calibrate kb_miss to a distance FLOOR with Harrison rather than a
        # guess. Zero added retrieval work; distances are already computed.
        kb_meta["kb_chunks_returned"] = len(results)
        kb_meta["kb_best_distance"] = (
            round(min(r.distance for r in results), 4) if results else None
        )
    if not relevant and not notes_block:
        log.info("KB returned %d chunks but none passed distance threshold %.2f",
                 len(results), _KB_MAX_DISTANCE)
        # WS4: cross-entity fallback for a cross-entity-authorized asker (the
        # founder, or a founder-channel asker) looking for a shared vendor/contact
        # tagged to a DIFFERENT entity. Replaces a confident "no record" with a
        # confidence-labeled wider-portfolio result. LEX excluded for non-custodians.
        fallback = _try_cross_entity_fallback(
            query, query_vec, kb_entity, asker_emails, asker_unrestricted, phi_custodian,
        )
        if fallback:
            if kb_meta is not None:
                kb_meta["cross_entity_fallback"] = True
                kb_meta["unstripped_personal"] = True  # asker-scoped -> never cache
            return fallback
        return None

    main_block = ""
    if relevant:
        # Tier-1 per-user access control: header-strip personal (gmail/drive_sweep)
        # chunks the asker doesn't own. Fail-closed — an unknown asker
        # (asker_emails None/empty) gets everything personal stripped. The
        # unstripped_personal flag tells the caller the response must not enter
        # the shared semantic cache.
        relevant, unstripped_personal = historical_access.apply_tier1(
            relevant, asker_emails or frozenset(), asker_unrestricted,
        )
        if kb_meta is not None and unstripped_personal:
            kb_meta["unstripped_personal"] = True

        # Content-level PHI scrub (F-2 / 2.3): a LEX-authorized NON-custodian whose
        # question misses the `phi` keyword gate could otherwise surface raw PHI
        # from a LEX chunk. Scrub retrieved LEX chunk text for non-custodians;
        # custodians (phi_custodian=True) are untouched.
        if kb_entity == "LEX" and not phi_custodian:
            relevant = _apply_lex_phi_scrub(relevant)
        elif not phi_custodian:
            # W2-01: content-level PHI backstop for a LEX-PHI chunk MIS-TAGGED under a
            # non-LEX entity (the LEX scrub above never fires for it). Withhold it —
            # deterministic net mirroring drive_materializer._phi_wall's non-LEX branch.
            relevant = _withhold_non_lex_phi(relevant)

        if relevant:
            log.info(
                "KB retrieved %d chunks (of %d returned) for entity=%s — best distance=%.3f",
                len(relevant), len(results), entity,
                relevant[0].distance,
            )
            main_block = _format_kb_chunks(relevant)

    if notes_block:
        return f"{main_block}\n\n{notes_block}" if main_block else notes_block
    return main_block


def _apply_lex_phi_scrub(results: list) -> list:
    """Content-level PHI redaction for a NON-custodian's LEX retrieval (F-2 / 2.3).

    The `phi` keyword topic-gate + entity-siloing + custodian gate are the access
    controls; this is defense-in-depth for the residual where a LEX-authorized
    non-custodian asks a question that misses the keyword list and a retrieved LEX
    chunk carries raw PHI. Reuses phi_guard.scrub_lex_phi over each chunk's
    content, preserving staff/operational names (the org-roles roster) so ordinary
    LEX ops content stays readable. Custodians never reach this path (callers gate
    on phi_custodian); non-LEX retrievals are never scrubbed.

    FAIL-CLOSED on a scrub error: WITHHOLD the chunk content rather than surface raw
    PHI -- matches the capture pipeline's _scrub_lex_text and the codebase's
    fail-closed PHI doctrine.

    Titles, filenames, and the pre-baked deep-link LABEL are the highest-density
    LEX client identifiers (Fireflies meeting titles, per-client filenames) and are
    frequently BARE names that scrub_lex_phi cannot reliably catch (it keys on a
    care-recipient cue or possessive). The deep_link is also pre-wrapped at ingest
    (`<permalink|title>`) so _format_kb_chunks renders its embedded label verbatim,
    NOT a re-scrubbed title. So for a non-custodian we NEUTRALIZE the citation
    entirely (generic title, no link); the scrubbed content still carries the
    answer's substance, and the deep_link URL was only an opaque file/GID id anyway.
    """
    try:
        staff = {r.name for r in org_roles.all_roles() if getattr(r, "name", "")}
    except Exception:  # noqa: BLE001
        staff = set()
    for r in results:
        try:
            r.content = phi_guard.scrub_lex_phi(r.content, allowed_names=staff)
            # B5 (2026-06-17): also redact a bare non-staff name sitting near a PHI
            # cue (the residual scrub_lex_phi misses -- "the client, Madison, ..." /
            # "incident involving Jalen"). Retrieval-only; cue-scoped so ordinary
            # ops prose is untouched. Inside the same try -> fail-closed on error.
            r.content = phi_guard.redact_cue_adjacent_names(r.content, allowed_names=staff)
        except Exception:  # noqa: BLE001 -- fail CLOSED, never surface raw PHI
            log.warning(
                "LEX PHI scrub failed on chunk %s; WITHHOLDING content (fail-closed)",
                getattr(r, "chunk_id", "?"),
            )
            r.content = "[content withheld -- PHI scrub error]"
        # Neutralize the citation (title + deep-link label) -- client names leak
        # through both and the scrub can't reliably catch bare names.
        try:
            r.title = "LEX knowledge base entry"
            r.deep_link = ""
        except Exception:  # noqa: BLE001
            pass
    return results


def _citation_carries_phi(r, staff: set) -> bool:
    """Should a KEPT non-LEX chunk's citation (title + deep_link LABEL) be neutralized?

    The title + deep_link label are a distinct citation surface the body predicate never
    vetted — apply_tier1 strips them ONLY for gmail/drive_sweep, so a fireflies/slack/asana/
    notion chunk keeps its raw title, and a LEX client name is frequently a BARE meeting
    TITLE that no body cue reveals (the exact reason _apply_lex_phi_scrub neutralizes the LEX
    citation). Neutralize when the citation:
      - is a fireflies MEETING title (the per-client-name surface apply_tier1 does not strip;
        meeting-title citations are low value, so blanking here cheaply closes the dominant
        bare-client-name case, D-051 finding 1), OR
      - trips the live PHI predicate (a clinical / program-billing title), OR
      - carries a cue-adjacent client name (redact_cue_adjacent_names alters it).
    Accepted residual: a bare client name with NO cue in a slack/asana/notion title — the
    same class _apply_lex_phi_scrub's body redactor documents; the custodian gate + entity
    siloing + fireflies-first classify_lex_meeting remain the primary net.
    """
    title = getattr(r, "title", "") or ""
    dl = getattr(r, "deep_link", "") or ""
    label = dl.split("|", 1)[1].rstrip(">") if dl.startswith("<") and "|" in dl else ""
    citation = f"{title} {label}".strip()
    if getattr(r, "source", "") == "fireflies":
        return True
    if not citation:
        return False
    if phi_guard.non_lex_phi_backstop_trips_live(citation, allowed_names=staff):
        return True
    return phi_guard.redact_cue_adjacent_names(citation, allowed_names=staff) != citation


def _withhold_non_lex_phi(results: list) -> list:
    """Content-level PHI backstop for a NON-custodian's NON-LEX retrieval (W2-01, 2026-07-05).

    The LEX scrub (_apply_lex_phi_scrub) fires only when the resolved channel entity is
    LEX. A LEX-PHI chunk mis-tagged under a NON-LEX entity (e.g. entity=FNDR/F3E via a
    tagging miss) is retrievable on a non-LEX query — include_fndr pulls FNDR chunks into
    every non-LEX channel — and, without this, is served UNSCRUBBED. That residual was
    backstopped only by the prompt-only FNDR guardrail (violating D-034: deterministic
    code over prompt). This is the deterministic net.

    Uses phi_guard.non_lex_phi_backstop_trips_LIVE (D-051 findings 3/4/8): the high-volume
    per-query path must not over-refuse legitimate OSN/F3E product copy (bare melatonin/ADHD
    mentions) or aggregate holdco finance — so bare dx-term/med-name need a care/program cue
    and the billing/status leg needs a non-staff individual. The Drive/dossier egress keeps
    the stricter unconditional non_lex_phi_backstop_trips.

    Two actions per chunk: (1) WITHHOLD the chunk when its BODY trips (drop it, never the
    whole answer). (2) For a kept chunk, NEUTRALIZE its citation (title + deep_link) when the
    citation surface carries a client name the body predicate never saw (finding 1).

    FAIL-CLOSED: a predicate error withholds the chunk (never surface un-vetted content).
    Custodians never reach this path (callers gate on phi_custodian); LEX retrievals take
    _apply_lex_phi_scrub instead.
    """
    try:
        staff = {r.name for r in org_roles.all_roles() if getattr(r, "name", "")}
    except Exception:  # noqa: BLE001
        staff = set()
    kept: list = []
    for r in results:
        try:
            if phi_guard.non_lex_phi_backstop_trips_live(
                getattr(r, "content", "") or "", allowed_names=staff
            ):
                log.warning(
                    "W2-01: withholding non-LEX chunk %s (entity=%s) — carries LEX-client "
                    "PHI (mis-tagged?)",
                    getattr(r, "chunk_id", "?"), getattr(r, "entity", "?"),
                )
                continue
        except Exception:  # noqa: BLE001 — fail CLOSED: withhold on predicate error
            log.warning(
                "W2-01: PHI backstop error on chunk %s; WITHHOLDING (fail-closed)",
                getattr(r, "chunk_id", "?"),
            )
            continue
        # Kept: the body was vetted, but title/deep-link were not — neutralize a
        # client-name-bearing citation (fail-closed on error).
        try:
            if _citation_carries_phi(r, staff):
                r.title = "knowledge base entry"
                r.deep_link = ""
        except Exception:  # noqa: BLE001 — fail CLOSED
            r.title = "knowledge base entry"
            r.deep_link = ""
        kept.append(r)
    return kept


# Business entities searched in the cross-entity fallback (WS4). LEX is added
# only for a custodian in LEX scope; the channel's own entity is dropped (already
# searched). FNDR is the founder corpus.
_CROSS_ENTITY_FALLBACK_ENTITIES: tuple[str, ...] = (
    "F3E", "F3C", "OSN", "UFL", "BDM", "HJRP", "HJRPROD", "HJRG", "FNDR",
)


def _try_cross_entity_fallback(
    query: str,
    query_vec: list[float] | None,
    kb_entity: str,
    asker_emails: frozenset[str] | None,
    asker_unrestricted: bool,
    phi_custodian: bool,
) -> str | None:
    """When an entity-scoped search is empty AND the asker has cross-entity
    authority (the founder, or a founder/holdco channel), search the wider
    portfolio for a shared vendor/contact tagged to another entity. Returns a
    CONFIDENCE-LABELED block, or None when nothing is found (never a fabricated
    "no record"). Reuses the security-reviewed per-entity kb.search().

    LEX-store is EXCLUDED unless the asker is a LEX custodian in LEX scope
    (phi_custodian): a printer's identity is harmless cross-entity, a Lexington
    clinical/client contact is not.
    """
    if not (asker_unrestricted or kb_entity in ("FNDR", "HJRG")):
        return None
    kb = get_shared_kb()
    if kb is None:
        return None

    entities = [e for e in _CROSS_ENTITY_FALLBACK_ENTITIES if e != kb_entity]
    if phi_custodian and kb_entity != "LEX" and "LEX" not in entities:
        entities.append("LEX")

    seen: set[str] = set()
    merged: list = []
    try:
        with _SHARED_KB_LOCK:
            for ent in entities:
                res = kb.search(
                    query, entity=ent, k=_KB_TOP_K, max_age_days=_KB_MAX_AGE_DAYS,
                    include_fndr=False, query_vec=query_vec,
                )
                for r in res:
                    if r.distance <= _KB_MAX_DISTANCE and r.chunk_id not in seen:
                        seen.add(r.chunk_id)
                        merged.append(r)
    except Exception as exc:  # noqa: BLE001
        log.warning("cross-entity fallback search failed: %s", exc)
        return None

    # Belt-and-suspenders: never surface a LEX chunk to a non-custodian, even if
    # one slipped through entity tagging.
    if not phi_custodian:
        merged = [r for r in merged if (r.entity or "").upper() != "LEX"]
    if not merged:
        return None

    merged.sort(key=lambda r: r.distance)
    merged = merged[:_KB_TOP_K]
    merged, _ = historical_access.apply_tier1(
        merged, asker_emails or frozenset(), asker_unrestricted,
    )
    # W2-01: the fallback spans every business entity, so a LEX-PHI chunk mis-tagged
    # under a non-LEX entity can ride in here even though the entity==LEX filter above
    # already dropped correctly-tagged LEX rows for a non-custodian. Apply the same
    # content-level backstop as the main path.
    if not phi_custodian:
        merged = _withhold_non_lex_phi(merged)
    if not merged:
        return None

    log.info(
        "cross-entity fallback: %d chunks from %s (channel=%s)",
        len(merged), sorted({r.entity for r in merged}), kb_entity,
    )
    block = _format_kb_chunks(merged)
    return (
        "_Nothing in this channel's own records. A wider portfolio search turned up "
        "the following — confirm before acting, and note it may belong to another "
        "entity:_\n\n" + block
    )


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
