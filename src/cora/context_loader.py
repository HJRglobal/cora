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

from cora import historical_access, user_notes, phi_guard, org_roles
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
        founder_text = _FOUNDER_PATH.read_text(encoding="utf-8")
        # Aggregators (FNDR/HJRG) keep the full brief; every other entity gets the
        # static head only and relies on KB retrieval for the dynamic current-state.
        if entity not in _FOUNDER_FULL_ENTITIES:
            founder_text = _slim_founder(founder_text)
        parts.append(founder_text)

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


def owned_kb_search(
    query: str,
    owner_emails: frozenset[str] | None,
    financial_only: bool = False,
    k: int = 12,
    query_vec: list[float] | None = None,
) -> list:
    """Tier-2 owner-scoped KB search through the shared instance + lock.

    Thin wrapper over KnowledgeBase.search_owned so callers (app.py grant
    path) keep the shared-connection lock discipline. Returns [] when the KB
    is unavailable. PHI filtering is the caller's job (historical_access.drop_phi).
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
    if not relevant and not notes_block:
        log.info("KB returned %d chunks but none passed distance threshold %.2f",
                 len(results), _KB_MAX_DISTANCE)
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
        # custodians (phi_custodian=True) and non-LEX retrievals are untouched.
        if kb_entity == "LEX" and not phi_custodian:
            relevant = _apply_lex_phi_scrub(relevant)

        log.info(
            "KB retrieved %d chunks (of %d returned) for entity=%s — best distance=%.3f",
            len(relevant), len(results), entity,
            relevant[0].distance if relevant else 0,
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
