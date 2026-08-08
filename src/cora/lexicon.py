"""Company lexicon: ONE resolver over THREE stores (Lexicon Flywheel v1).

Stores (files of record, never migrated or duplicated):
  1. ``data/maps/lexicon/{entity}.yaml`` + ``_shared.yaml`` -- location / project /
     acronym / vendor / channel / process terms (NEW, this module's growth surface).
  2. ``data/maps/f3e-sku-aliases.yaml`` -- F3E products (existing; loaded as F3E
     ``product`` entries; the file's own consumer semantics are untouched).
  3. ``data/maps/user-aliases.yaml`` -- people (existing; loaded as ``person``
     entries; the anchored-matching consumers keep their own matching rules --
     this module only ever does EXACT normalized matching on those surfaces).

INVARIANTS (test-pinned, load-bearing):
  - ADVISORY: the lexicon never grants access. Every deterministic guard
    (user_access, cross_entity, sibling, PHI, channel tier) runs unchanged.
    A resolution only rewrites a QUERY; it never bypasses an allowlist.
  - ADDITIVE: a lexicon miss or a lexicon load failure leaves every consumer's
    behavior exactly as it is today. The lexicon is never a gate.
  - Fuzzy NEVER auto-applies: exact normalized match resolves; 2+ exact hits =
    ambiguous and the caller ASKS; fuzzy (difflib >= 0.75) is a SUGGESTION only.

Flag: ``CORA_LEXICON=off|resolve|full`` (default ``off`` = fully inert).
  - ``resolve``: loader + resolver + telemetry live for programmatic consumers.
  - ``full``: adds prompt injection, proposals, and the teach tool.
HONEST RESTART SEMANTICS: the bot process snapshots ``.env`` at startup
(config.py runs load_dotenv at import), so flipping the value for the BOT
requires the .env edit AND a restart (the cq-06f4797db4f1 lesson). Scheduled
scripts re-run load_dotenv at each fire and see the new value immediately.

Loader follows the org_roles pattern: 60s TTL live-reload, thread lock,
keep-last-good on parse error, malformed entries skipped with a warning,
fail-closed empty for unknown entities.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]

# All paths env-overridable (tests point them at tmp fixtures; live code never sets these).
_DEFAULT_LEXICON_DIR = _REPO_ROOT / "data" / "maps" / "lexicon"
_DEFAULT_SKU_PATH = _REPO_ROOT / "data" / "maps" / "f3e-sku-aliases.yaml"
_DEFAULT_USER_ALIASES_PATH = _REPO_ROOT / "data" / "maps" / "user-aliases.yaml"
_DEFAULT_LOG_PATH = _REPO_ROOT / "logs" / "lexicon-resolutions.jsonl"

_TTL_SECONDS = 60.0

# Prompt-block caps (design lock): max terms / max chars per entity block.
MAX_BLOCK_TERMS = 40
MAX_BLOCK_CHARS = 2500
_USAGE_WINDOW_DAYS = 90

_VALID_TYPES = frozenset(
    {"location", "project", "acronym", "vendor", "channel", "process", "product", "person"}
)

# Entities whose channels get the injected prompt block (mirrors the
# known-answers read map exactly: LEX at GM level only, never LEX-* sub-entity
# channels; store/property channels inherit nothing). Sub-entity channels still
# get scope-filtered programmatic resolution via resolve().
_INJECTABLE_ENTITIES = frozenset(
    {"F3E", "OSN", "BDM", "HJRP", "UFL", "F3C", "HJRPROD", "HJRG", "FNDR", "LEX"}
)

# Aggregators resolve against the union of all files (LEX entries are
# staff/ops-only by construction, so the union is safe).
_UNION_ENTITIES = frozenset({"FNDR", "HJRG"})

# Sub-entity -> parent collapse (mirrors context_loader._LEX_PARENT/_STORE_PARENT).
_SUB_PARENT: dict[str, str] = {
    "OSNGF": "OSN", "OSNGM": "OSN", "OSNGW": "OSN", "OSNVV": "OSN",
    "HJRP-1337": "HJRP", "HJRP-1555": "HJRP", "HJRP-LCI": "HJRP",
    "HJRP-CL": "HJRP", "HJRP-RR": "HJRP",
    "F3": "F3E",
    "LEX-LLC": "LEX", "LEX-LTS": "LEX", "LEX-LBHS": "LEX", "LEX-LLA": "LEX",
}

_KNOWN_ENTITIES = frozenset(
    {"F3E", "OSN", "LEX", "HJRP", "UFL", "BDM", "HJRPROD", "F3C", "HJRG", "FNDR", "SHARED"}
)


def lexicon_level() -> str:
    """CORA_LEXICON: 'off' (default; fully inert), 'resolve', or 'full'.
    Unknown values fail closed to 'off'."""
    v = (os.environ.get("CORA_LEXICON", "off") or "off").strip().lower()
    return v if v in ("off", "resolve", "full") else "off"


def _lexicon_dir() -> Path:
    return Path(os.environ.get("LEXICON_DIR") or _DEFAULT_LEXICON_DIR)


def _sku_aliases_path() -> Path:
    return Path(os.environ.get("LEXICON_SKU_ALIASES_PATH") or _DEFAULT_SKU_PATH)


def _user_aliases_path() -> Path:
    return Path(os.environ.get("LEXICON_USER_ALIASES_PATH") or _DEFAULT_USER_ALIASES_PATH)


def _log_path() -> Path:
    return Path(os.environ.get("LEXICON_RESOLUTIONS_PATH") or _DEFAULT_LOG_PATH)


def norm_term(s: str) -> str:
    """Normalize a term/alias for comparison. MIRRORS tool_dispatch._norm_alias
    exactly (lowercase, keep alnum + space + '&', other punctuation -> space,
    collapse whitespace); parity is test-pinned. Do NOT fork the behavior."""
    s = (s or "").lower().strip()
    kept = "".join(c if (c.isalnum() or c in " &") else " " for c in s)
    return " ".join(kept.split())


@dataclass(frozen=True)
class LexEntry:
    term: str
    type: str
    canonical: str
    canonical_name: str
    entity: str
    aliases: tuple[str, ...] = ()
    scope: str = ""
    notes: str = ""
    source: str = ""
    source_file: str = ""

    def surfaces(self) -> tuple[str, ...]:
        """All normalized lookup surfaces for this entry (term + aliases)."""
        out = []
        for raw in (self.term, *self.aliases):
            n = norm_term(raw)
            if n and n not in out:
                out.append(n)
        return tuple(out)


@dataclass(frozen=True)
class Resolution:
    status: str  # "exact" | "ambiguous" | "suggestion" | "miss"
    query: str = ""
    canonical: str = ""
    canonical_name: str = ""
    type: str = ""
    matched_term: str = ""
    source_file: str = ""
    candidates: tuple[LexEntry, ...] = ()
    suggestion: str = ""  # display form of the closest term (NEVER auto-applied)


_MISS = Resolution(status="miss")

# ── Cache (org_roles pattern: double-checked locking, keep-last-good) ─────────

_lock = threading.Lock()
_loaded_at: float = 0.0
_entries_by_entity: dict[str, list[LexEntry]] = {}
_load_ok: bool = False


def invalidate_cache() -> None:
    """Force reload on next call (tests + manual edits)."""
    global _loaded_at, _entries_by_entity, _load_ok
    with _lock:
        _loaded_at = 0.0
        _entries_by_entity = {}
        _load_ok = False


def _parse_lexicon_file(path: Path) -> list[LexEntry]:
    """Parse one data/maps/lexicon/*.yaml file. Malformed entries are skipped
    with a warning; a malformed FILE raises (caller keeps last-good)."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"lexicon file {path.name} is not a mapping")
    entity = str(raw.get("entity") or path.stem).strip().upper()
    if entity.startswith("_"):
        entity = "SHARED"
    out: list[LexEntry] = []
    for item in (raw.get("terms") or []):
        if not isinstance(item, dict):
            log.warning("lexicon: skipping malformed entry in %s: %r", path.name, item)
            continue
        term = str(item.get("term") or "").strip()
        etype = str(item.get("type") or "").strip().lower()
        canonical = str(item.get("canonical") or "").strip()
        canonical_name = str(item.get("canonical_name") or "").strip()
        if not term or not canonical or not canonical_name or etype not in _VALID_TYPES:
            log.warning("lexicon: skipping malformed entry in %s: %r", path.name, item)
            continue
        aliases = tuple(
            str(a).strip() for a in (item.get("aliases") or []) if str(a).strip()
        )
        out.append(LexEntry(
            term=term, type=etype, canonical=canonical, canonical_name=canonical_name,
            entity=entity, aliases=aliases,
            scope=str(item.get("scope") or "").strip().upper(),
            notes=str(item.get("notes") or "").strip(),
            source=str(item.get("source") or "").strip(),
            source_file=str(path),
        ))
    return out


def _parse_sku_store(path: Path) -> list[LexEntry]:
    """Load f3e-sku-aliases.yaml as F3E product entries. Reads the seed 'skus'
    mapping plus the append-only 'learned' LIST (rows of {sku, aliases}) that
    approved lexicon proposals write to -- a list so a second alias for the same
    SKU is a NEW appended row, never an edit of an existing line (revert
    integrity). Read-only here; the file's own consumer
    (tool_dispatch._load_sku_aliases) keeps its exact semantics."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("f3e-sku-aliases.yaml is not a mapping")
    out: list[LexEntry] = []

    def _add(sku: str, aliases, source: str) -> None:
        sku = str(sku).strip()
        alias_list = tuple(str(a).strip() for a in (aliases or []) if str(a).strip())
        if not sku or not alias_list:
            return
        out.append(LexEntry(
            term=alias_list[0], type="product", canonical=sku,
            canonical_name=f"{alias_list[0]} (SKU {sku})", entity="F3E",
            aliases=alias_list[1:], source=source, source_file=str(path),
        ))

    skus = raw.get("skus") or {}
    if isinstance(skus, dict):
        for sku, aliases in skus.items():
            _add(sku, aliases, "sku-map")
    for row in (raw.get("learned") or []):
        if isinstance(row, dict):
            _add(row.get("sku") or "", row.get("aliases"), "learned")
    return out


def _parse_person_store(path: Path) -> list[LexEntry]:
    """Load user-aliases.yaml as org-wide person entries. Reads the seed
    'aliases' mapping plus the append-only 'learned_aliases' mapping. EXACT
    normalized matching only -- the anchored-matching consumers (first-name /
    prefix / fuzzy-0.88 rules) keep their own logic; this store's presence here
    is read-through + telemetry, never a loosening of those rules."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("user-aliases.yaml is not a mapping")
    merged: dict[str, list[str]] = {}

    def _fold(name: str, aliases) -> None:
        name = str(name).strip()
        if not name:
            return
        merged.setdefault(name, [])
        for a in (aliases or []):
            a = str(a).strip()
            if a and a not in merged[name]:
                merged[name].append(a)

    mapping = raw.get("aliases") or {}
    if isinstance(mapping, dict):
        for name, aliases in mapping.items():
            _fold(name, aliases)
    # 'learned_aliases' is an append-only LIST of {name, aliases} rows (a list so
    # a second alias for the same person is a NEW row, never a line edit).
    for row in (raw.get("learned_aliases") or []):
        if isinstance(row, dict):
            _fold(row.get("name") or "", row.get("aliases"))
    out: list[LexEntry] = []
    for name, aliases in merged.items():
        out.append(LexEntry(
            term=name, type="person", canonical=name, canonical_name=name,
            entity="SHARED", aliases=tuple(aliases),
            source="user-aliases", source_file=str(path),
        ))
    return out


def _load_all() -> dict[str, list[LexEntry]]:
    by_entity: dict[str, list[LexEntry]] = {}
    lex_dir = _lexicon_dir()
    if lex_dir.is_dir():
        for path in sorted(lex_dir.glob("*.yaml")):
            try:
                for e in _parse_lexicon_file(path):
                    by_entity.setdefault(e.entity, []).append(e)
            except Exception as exc:  # noqa: BLE001 -- one bad file never blanks the rest
                log.warning("lexicon: could not parse %s: %s", path.name, exc)
    sku_path = _sku_aliases_path()
    if sku_path.exists():
        try:
            for e in _parse_sku_store(sku_path):
                by_entity.setdefault(e.entity, []).append(e)
        except Exception as exc:  # noqa: BLE001
            log.warning("lexicon: could not parse %s: %s", sku_path.name, exc)
    ua_path = _user_aliases_path()
    if ua_path.exists():
        try:
            for e in _parse_person_store(ua_path):
                by_entity.setdefault(e.entity, []).append(e)
        except Exception as exc:  # noqa: BLE001
            log.warning("lexicon: could not parse %s: %s", ua_path.name, exc)
    return by_entity


def _refresh_if_stale() -> None:
    global _loaded_at, _entries_by_entity, _load_ok
    now = time.monotonic()
    if _load_ok and (now - _loaded_at) < _TTL_SECONDS:
        return
    with _lock:
        now = time.monotonic()
        if _load_ok and (now - _loaded_at) < _TTL_SECONDS:
            return
        try:
            parsed = _load_all()
            if parsed or not _entries_by_entity:
                _entries_by_entity = parsed
            else:
                # Parsed empty while we hold a previous good registry: keep
                # serving it (transient editor save states).
                log.warning("lexicon: registry parsed empty -- keeping last good")
            _load_ok = True
            _loaded_at = now
        except Exception as exc:  # noqa: BLE001 -- keep last good registry
            log.warning("lexicon: load failed (%s) -- keeping last good registry", exc)
            _loaded_at = now
            _load_ok = True


def _collapse_entity(entity: str) -> tuple[str, str]:
    """(parent_entity, derived_scope). A sub-entity collapses to its parent
    with itself as the scope filter; a parent entity has no scope filter."""
    ent = (entity or "").strip().upper()
    if ent in _SUB_PARENT:
        return _SUB_PARENT[ent], ent
    if ent.startswith("LEX-"):
        return "LEX", ent
    if ent.startswith("HJRP-"):
        return "HJRP", ent
    return ent, ""


def _entries_for(entity: str, scope: Optional[str]) -> list[LexEntry]:
    """Entries in scope for a channel entity: entity file + SHARED, with parent
    collapse + scope filter; FNDR/HJRG = union of everything; unknown entity =
    fail-closed empty."""
    _refresh_if_stale()
    parent, derived_scope = _collapse_entity(entity)
    eff_scope = (scope or derived_scope or "").strip().upper()
    if parent in _UNION_ENTITIES:
        pools: Iterable[list[LexEntry]] = _entries_by_entity.values()
        out = [e for pool in pools for e in pool]
    elif parent in _KNOWN_ENTITIES:
        out = list(_entries_by_entity.get(parent, ())) + list(_entries_by_entity.get("SHARED", ()))
    else:
        return []
    if eff_scope:
        out = [e for e in out if e.scope in ("", eff_scope)]
    return out


def resolve(
    query: str,
    entity: str,
    types: Optional[Iterable[str]] = None,
    scope: Optional[str] = None,
    *,
    consumer: str = "",
    channel: str = "",
    user: str = "",
) -> Resolution:
    """Resolve a shorthand query against the entity-scoped lexicon.

    Ladder (design lock): (1) exact normalized match on term or alias resolves;
    (2) 2+ distinct exact hits = ambiguous -> the caller ASKS, never guesses;
    (3) fuzzy (difflib >= 0.75) produces a SUGGESTION only, NEVER auto-applied;
    (4) miss -> caller behavior unchanged (ADDITIVE invariant).

    Telemetry: when ``consumer`` is provided the resolution is logged to the
    chokepoint ledger (fail-soft). Callers doing internal probing (evals,
    rendering) omit ``consumer`` and produce no telemetry.
    """
    try:
        entries = _entries_for(entity, scope)
    except Exception as exc:  # noqa: BLE001 -- ADDITIVE: load failure == miss
        log.warning("lexicon: resolve degraded to miss (%s)", exc)
        entries = []
    type_filter = frozenset(t.strip().lower() for t in types) if types else None
    if type_filter:
        entries = [e for e in entries if e.type in type_filter]

    norm_q = norm_term(query)
    result = _MISS
    if norm_q and entries:
        hits: list[LexEntry] = []
        seen: set[tuple[str, str]] = set()
        for e in entries:
            if norm_q in e.surfaces():
                key = (e.canonical, e.type)
                if key not in seen:
                    seen.add(key)
                    hits.append(e)
        if len(hits) == 1:
            h = hits[0]
            result = Resolution(
                status="exact", query=query, canonical=h.canonical,
                canonical_name=h.canonical_name, type=h.type,
                matched_term=h.term, source_file=h.source_file,
                candidates=(h,),
            )
        elif len(hits) >= 2:
            result = Resolution(status="ambiguous", query=query, candidates=tuple(hits))
        else:
            import difflib
            by_norm: dict[str, LexEntry] = {}
            for e in entries:
                for s in e.surfaces():
                    by_norm.setdefault(s, e)
            m = difflib.get_close_matches(norm_q, list(by_norm.keys()), n=1, cutoff=0.75)
            if m:
                cand = by_norm[m[0]]
                # Suggestion surfaces the DISPLAY form of the matched term.
                display = cand.term if norm_term(cand.term) == m[0] else next(
                    (a for a in cand.aliases if norm_term(a) == m[0]), cand.term)
                result = Resolution(
                    status="suggestion", query=query, suggestion=display,
                    candidates=(cand,),
                )
            else:
                result = Resolution(status="miss", query=query)
    else:
        result = Resolution(status="miss", query=query)

    if consumer:
        first = result.candidates[0] if result.candidates else None
        log_event(
            entity=entity, channel=channel, user=user, consumer=consumer,
            status=result.status, query=query,
            canonical=result.canonical or (first.canonical if first else ""),
            matched_term=result.matched_term or (first.term if first else ""),
        )
    return result


def find_ambiguous_in_text(
    text: str,
    entity: str,
    types: Optional[Iterable[str]] = None,
    scope: Optional[str] = None,
) -> Optional[Resolution]:
    """Longest lexicon surface present in `text` -- returned ONLY if it is
    ambiguous, else None (v2 S7, cq-483109dfea11).

    resolve() answers "is THIS term ambiguous?" about a single query string. It
    cannot see the user's own words, and that is where the ask was being lost:
    the model canonicalizes the phrase before the tool is ever called ("the
    variety pack at the office" arrives as product_query="pure variety pack"),
    so an ambiguous USER phrase reaches the resolver pre-disambiguated, resolves
    exact, and the which-one-did-you-mean ask never fires. Five-plus live repros
    8/1-8/2. This scans the VERBATIM text instead, so ambiguity is judged on what
    the human actually said.

    LONGEST-MATCH-WINS, which is what keeps it from over-asking: if the user
    typed a specific "pure variety pack" and the lexicon also carries an
    ambiguous shorter "variety pack", the longer surface is present too and
    shadows it -- no ask. An ambiguous surface only wins when nothing longer
    containing it is also present, i.e. the user really was non-specific.

    Fail-soft to None (a load failure or an unusable text is simply "no
    ambiguity found"), preserving the module's ADDITIVE invariant."""
    if not (text or "").strip():
        return None
    try:
        entries = _entries_for(entity, scope)
    except Exception as exc:  # noqa: BLE001 -- ADDITIVE: load failure == no finding
        log.warning("lexicon: find_ambiguous_in_text degraded (%s)", exc)
        return None
    type_filter = frozenset(t.strip().lower() for t in types) if types else None
    if type_filter:
        entries = [e for e in entries if e.type in type_filter]
    if not entries:
        return None

    # surface -> the distinct (canonical, type) pairs it can mean.
    by_surface: dict[str, list[LexEntry]] = {}
    for e in entries:
        for s in e.surfaces():
            if not s:
                continue
            bucket = by_surface.setdefault(s, [])
            if (e.canonical, e.type) not in {(b.canonical, b.type) for b in bucket}:
                bucket.append(e)

    norm_text = norm_term(text)
    if not norm_text:
        return None
    padded = f" {norm_text} "
    # A trailing plural is tolerated ("variety packs" hits the "variety pack"
    # surface). Cheap and low-risk: it only ever widens what counts as PRESENT,
    # and the ask is the conservative outcome. Anything richer belongs in
    # norm_term, which is parity-pinned against tool_dispatch._norm_alias.
    present = [s for s in by_surface if f" {s} " in padded or f" {s}s " in padded]
    if not present:
        return None

    present.sort(key=len, reverse=True)
    for surface in present:
        if len(by_surface[surface]) < 2:
            continue  # unambiguous here
        shadowed = any(
            other != surface and len(other) > len(surface) and surface in other
            for other in present
        )
        if shadowed:
            # The user WAS specific -- a longer surface containing this one is
            # also present, so this shorter ambiguity is not what they meant.
            continue
        return Resolution(status="ambiguous", query=surface,
                          candidates=tuple(by_surface[surface]))
    return None


# ── Telemetry (lane-A chokepoint ledger) ─────────────────────────────────────


def log_event(
    *,
    entity: str,
    status: str,
    query: str,
    channel: str = "",
    user: str = "",
    consumer: str = "",
    canonical: str = "",
    matched_term: str = "",
    event: str = "resolve",
) -> None:
    """Append one row to logs/lexicon-resolutions.jsonl. FAIL-SOFT: telemetry
    failure never breaks a reply (the _audit_shopify_write idiom).

    PHI at rest: EVERY row's display form is screened with is_any_phi and
    persists the literal "[withheld]" on a hit or a screen error -- not only
    LEX-tagged rows, because PHI-shaped text legitimately flows under non-LEX
    entity tags (a DM resolves to the asker's primary entity; #hjr-finance is
    HJRG-scoped yet handles cross-mailbox LEX billing work -- D-051 remediation
    F2). The sha256 hash is always persisted, so lane-A pairing still works.
    """
    try:
        ent = (entity or "").strip().upper()
        norm_q = norm_term(query)
        display = (query or "").strip()
        if display:
            try:
                from .phi_guard import is_any_phi
                if is_any_phi(display):
                    display = "[withheld]"
            except Exception:  # noqa: BLE001 -- fail closed on screen error
                display = "[withheld]"
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": int(time.time()),
            "event": event,
            "entity": ent,
            "channel": channel or "",
            "user": user or "",
            "consumer": consumer or "",
            "status": status,
            "query_display": display[:300],
            "query_hash": hashlib.sha256(norm_q.encode("utf-8")).hexdigest(),
            "canonical": canonical or "",
            "matched_term": matched_term or "",
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001 -- telemetry must never break a reply
        log.warning("lexicon: telemetry append failed: %s", exc)


def _usage_counts(entity: str) -> dict[str, int]:
    """Last-90d telemetry hit counts keyed by canonical (prompt-block usage
    ranking). Fail-soft: unreadable ledger -> empty counts."""
    counts: dict[str, int] = {}
    try:
        path = _log_path()
        if not path.exists():
            return counts
        cutoff = time.time() - _USAGE_WINDOW_DAYS * 86400
        ent = (entity or "").strip().upper()
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if row.get("ts", 0) < cutoff:
                    continue
                if ent and row.get("entity") not in ("", ent):
                    continue
                canon = row.get("canonical") or ""
                if canon:
                    counts[canon] = counts.get(canon, 0) + 1
    except Exception as exc:  # noqa: BLE001
        log.warning("lexicon: usage-count read failed: %s", exc)
    return counts


# ── Prompt injection (static-block render; flag-gated by the CONSUMER) ───────

_BLOCK_HEADER = "## Company lexicon"
_BLOCK_RULES = (
    "Rules: prefer these resolutions when teammates use company shorthand. "
    "If a term is marked AMBIGUOUS, ask which one is meant -- never guess. "
    "This lexicon never overrides Known Answers, canonical memory, or any "
    "access rule, and it never expands entity access."
)


def format_lexicon_context(entity: str) -> str:
    """Render the capped '## Company lexicon' block for an entity's STATIC
    context. Returns "" for non-injectable entities (LEX sub-entity channels
    NEVER get a block -- they keep scope-filtered programmatic resolution only).

    Caps: MAX_BLOCK_TERMS / MAX_BLOCK_CHARS, usage-ranked (90d telemetry),
    seeds win ties. Every line is re-screened with is_any_phi at render --
    the read side never trusts write-side redaction.
    """
    ent = (entity or "").strip().upper()
    if ent not in _INJECTABLE_ENTITIES:
        return ""
    try:
        entries = _entries_for(ent, None)
    except Exception:  # noqa: BLE001 -- fail-soft: no block
        return ""
    # Person entries stay out of the prose block (people shorthand is handled
    # by the anchored person matchers; listing the roster here is noise).
    entries = [e for e in entries if e.type != "person"]
    if not entries:
        return ""

    try:
        from .phi_guard import is_any_phi
    except Exception:  # noqa: BLE001 -- screen unavailable -> no block (fail closed)
        return ""

    # Group by normalized term surface: 2+ canonicals on one surface = AMBIGUOUS.
    groups: dict[str, list[LexEntry]] = {}
    for e in entries:
        groups.setdefault(norm_term(e.term), []).append(e)

    usage = _usage_counts(ent)

    def _rank(item: tuple[str, list[LexEntry]]) -> tuple:
        _norm, grp = item
        hits = max((usage.get(e.canonical, 0) for e in grp), default=0)
        seed = any(e.source == "seed" for e in grp)
        return (-hits, 0 if seed else 1, grp[0].term.lower())

    lines: list[str] = [_BLOCK_HEADER, ""]
    shown = 0
    total = len(groups)
    budget = MAX_BLOCK_CHARS - len(_BLOCK_RULES) - 80  # reserve rules + overflow line
    used = sum(len(l) + 1 for l in lines)
    for _norm_surface, grp in sorted(groups.items(), key=_rank):
        if shown >= MAX_BLOCK_TERMS:
            break
        first = grp[0]
        distinct = {(e.canonical, e.type) for e in grp}
        if len(distinct) >= 2:
            cands = " | ".join(dict.fromkeys(e.canonical_name for e in grp))
            line = f'- "{first.term}" = AMBIGUOUS: ask ({cands})'
        else:
            alias_note = f" (aliases: {', '.join(first.aliases)})" if first.aliases else ""
            line = f'- "{first.term}"{alias_note} = {first.canonical_name} [{first.type}]'
        if is_any_phi(line):
            continue  # render screen: never egress a PHI-shaped line
        if used + len(line) + 1 > budget:
            break
        lines.append(line)
        used += len(line) + 1
        shown += 1
    if shown == 0:
        return ""
    if shown < total:
        lines.append(f"(+{total - shown} more -- the resolver knows them all)")
    lines.append("")
    lines.append(_BLOCK_RULES)
    return "\n".join(lines)
