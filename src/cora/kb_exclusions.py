"""Shared predicate: is a file Cora's OWN build/audit/forensic metadata?

Cora's build docs (forensic findings, rebuild execution logs, code-prompts,
cascade reports, phase scopes, north-star plans, this project's CLAUDE.md and
design/ scaffold) live under ``_shared/projects/cora/`` in the Founder OS Drive
tree. They are OPERATIONAL metadata, not org knowledge. Ingesting them into the
KB lets Cora retrieve and recite her own audit notes — and even her own system
prompts — as fact (the fabricated-"diagnostic" failure mode that prompted WS1).

This predicate keeps that doc set OUT of KB ingestion and powers the one-time
purge (``purge_cora_internal_kb.py``). One rule, several surfaces:

  - ``is_cora_internal_path(Path)``      — INGEST, static_md walk (``incremental_sync_static.py``)
  - ``is_cora_internal_source_id(str)``  — PURGE, by stored source_id (``\\`` or ``/`` separators)
  - ``is_cora_internal_title(str)``      — INGEST (``drive_sweep``) + PURGE, by the stored
                                            filename/``title``. ``drive_sweep`` walks Harrison's
                                            whole Drive (the Founder OS lives there), so these
                                            docs land under a Drive-FILE-ID source_id with the
                                            filename in ``title`` — the path rules can't see
                                            them, so we match the filename instead.

Why the title surface exists (the WS1-completion finding, 2026-06-19): the
static_md path was empty; the real leak was ``drive_sweep`` ingesting
``cora-rebuild-execution-log.md``, ``cora-forensic-findings-report.md``,
``cora-*.log``, etc. straight from Drive under file-id source_ids that no
path rule could match.

Scope notes:
  * The folder rule is the keystone: anything under ``_shared/projects/cora/``.
    Sibling projects (gmail-deep-dive, reddit-strategy, wikipedia-strategy, …)
    are NOT matched and stay ingested — only the ``cora`` project is excluded.
  * The filename rule is the workhorse for Drive copies (no path on the source_id).
    Requires a ``cora`` token AND a WHOLE-WORD build keyword. Both edges are anchored:
    the keyword with ``\\b`` (so "fix" never fires inside "fixed", "plan" inside
    "planning") and the ``cora`` token with a left lookbehind ``(?<![a-z0-9])`` so it is
    never a mid-word substring ("pecora", "decora", "mancora", "incora", "deCORAtions"
    are all spared). Underscores are normalized to hyphens first so ``\\b`` works across
    both separators (``CORA_IMPROVEMENT_BACKLOG`` matches). The targeted set includes
    audit/review/sweep -- Cora's own self-audits are the docs that produced the diagnostic.
  * A NEGATIVE guard (``_LEGIT_FAMILY_RE``) spares the named business-doc families
    (``…-cora-reference``, ``…_cora-wishlist``, ``…-cora-mapping``,
    ``cora-f3-monitor-privacy-policy``) EVEN with a soft keyword suffix
    (``cora-wishlist-review``) -- but NOT when a STRONG build keyword is also present
    (``cora-mapping-rebuild-execution-log`` is a genuine build doc and IS caught).
  * ``broad=True`` is used by the drive_sweep INGEST guard (over-excluding Cora's own
    ops docs is harmless; under-excluding re-opens the leak) and by the purge
    ``--scope broad`` full clean. It adds the long tail of Cora ops/session docs.

  ACCEPTED LIMITATIONS (filename heuristic; mitigated by the human-gated dry-run on the
  destructive purge + the cora_self_check/WS4 behavioral backstops):
    - A doc for a person/entity literally named "Cora" plus a build keyword
      (``Cora_Martinez_performance_review``) still matches. Rare; the affected doc
      types (HR, LEX client files) are sensitive and not wanted broadly in the KB anyway.
    - SPACE-delimited Cora doc names (``CORA Task Notes``) and keyword-BEFORE-cora
      orderings (``rebuild-...-cora.md``) UNDER-match. Non-canonical (Cora's real build
      docs are hyphen-``cora``-first), reversible at ingest. Not widened on purpose:
      normalizing spaces / decoupling order would worsen the person-name over-match
      above, and over-deletion is the cardinal sin on a one-time destructive purge.
"""

from __future__ import annotations

import re
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Dashboard read layer (2026-07-11): personal / highly-confidential dashboard
# backing-store folders that must NEVER be KB-ingested.
# ─────────────────────────────────────────────────────────────────────────────
# The `.json` state files are already excluded by an accident of the sweep MIME
# allow-list (application/json is not requested), but their `.md` / `.xlsx`
# siblings (capital-raise deal docs, the OneAmerica tracker workbook) WOULD be
# swept. Hardcoded here -- NOT read from dashboard-access.yaml -- so an ingest
# sweep can never fail-open on a YAML parse error. Keep in sync with the
# `kb_ingest: never` / `kb_excluded_folders` entries in
# data/maps/dashboard-access.yaml.
KB_EXCLUDED_FOLDER_IDS: frozenset[str] = frozenset(
    {
        "1INi4fLXG23xao-d_yf56Wrbrah54pIBB",  # 00-Founder/insurance/oneamerica (PERSONAL)
        "1BZI6v5pmpgrt7G2dPsAib3u3S-HqB7ZP",  # 02-F3-Energy/projects/capital-raise (HIGHLY CONFIDENTIAL)
        "1NPBNBfx3MMjqQM_WnmL6jOJSaRAQf752",  # 00-Founder/travel-points (PERSONAL)
    }
)


# Distinctive folder-name segments of the excluded dashboard stores. A source_id
# that is a PATH (static_md) or a Drive `metadata.path` (drive_asset) sitting under
# one of these folders is dropped at the store chokepoint (upsert_documents Step 0).
# drive_sweep stores no path (source_id = bare Drive file id) and is instead handled
# by the folder-id exclusion above at enumeration time.
_DASHBOARD_STORE_SEGMENTS: frozenset[str] = frozenset(
    {"capital-raise", "oneamerica", "travel-points"}
)


def is_excluded_folder(folder_id: str) -> bool:
    """True if a Drive folder id is a personal/confidential dashboard store that
    must never be KB-ingested."""
    return bool(folder_id) and folder_id in KB_EXCLUDED_FOLDER_IDS


def is_dashboard_store_path(path_or_source_id: str) -> bool:
    """True if a filesystem path, Drive path, or path-shaped source_id sits inside
    a personal / highly-confidential dashboard store (capital-raise, oneamerica,
    travel-points). Segment-based, case-insensitive, handles ``/`` and ``\\``.

    Over-exclusion is bounded to those distinctive folder names and is the safe
    direction here (these stores must never be KB-ingested)."""
    segs = {s.lower() for s in _segments(str(path_or_source_id or ""))}
    return bool(segs & _DASHBOARD_STORE_SEGMENTS)


def folder_ids_excluded(
    parents: list[str] | None, folder_set: frozenset[str] | set[str] | None = None
) -> bool:
    """True if ANY of a file's parent folder ids is KB-excluded.

    ``folder_set`` lets a caller pass an EXPANDED set (excluded roots + their
    descendant subfolders) so a flat per-user sweep also skips NESTED files; it
    defaults to the direct roots. The founders_os tree walk instead prunes whole
    subtrees via ``skip_folder_ids``.
    """
    check = folder_set if folder_set is not None else KB_EXCLUDED_FOLDER_IDS
    return any(p in check for p in (parents or []))

# The Cora build workspace. Any file under this folder sequence is build/ops
# metadata, never org knowledge.
_CORA_WORKSPACE_SEGMENTS: tuple[str, ...] = ("_shared", "projects", "cora")

# Keyword matching anchors on \b...\b over a name where underscores have first been
# normalized to hyphens (see _name_is_build_doc). Two bugs this avoids, both caught by
# the WS1-DRIVE reviews: (1) sub-word over-match -- "fix" must not fire inside "fixed",
# "plan" inside "planning"; \b blocks that (the char after is alphanumeric). (2) the
# underscore under-match -- \b is NOT a boundary at "_" (underscore is a word char), so
# WITHOUT normalization "CORA_IMPROVEMENT_BACKLOG" would escape; normalizing _->- fixes it.

# TARGETED filename rule: a ``cora`` token AND a build-doc keyword token. Default for the
# purge; the unambiguous self-diagnostic class (forensic/rebuild/audit/review/findings/
# exec-summary/backlog ...). Real docs like cora-slack-comms-review / cora-14-day-infra-
# review / cora-exec-summary ("Forensic Audit Executive Summary") are the ones that
# caused the fabricated diagnostic, so they live here, not in broad.
_CORA_BUILD_DOC_RE = re.compile(
    r"(?<![a-z0-9])cora[-_].*?\b("
    r"forensic|rebuild|execution-log|code-prompt|build-plan|build-queue|"
    r"master-build|cascade-report|cascade|incident-triage|north-star|"
    r"findings|phase-?\d|synthesis-and-path|report-synthesis|audit-addendum|"
    r"audit|review|sweep|exec-summary|backlog"
    r")\b",
    re.IGNORECASE,
)

# Cora's raw runtime logs (e.g. ``cora-2026-06-06.log``). Never org knowledge.
_CORA_LOG_RE = re.compile(r"^cora[-_].*\.log$", re.IGNORECASE)

# BROAD: the long tail of Cora ops/build/session docs. The drive_sweep INGEST guard uses
# THIS scope (over-excluding Cora's own ops docs from the KB is harmless; under-excluding
# re-opens the self-diagnostic leak), and the purge --scope broad uses it for a full clean.
_CORA_BUILD_DOC_BROAD_RE = re.compile(
    r"(?<![a-z0-9])cora[-_].*?\b("
    r"proposal|game-plan|overhaul|redesign|training|checklist|scaling|comms|infra|"
    r"spec|wiring|closeout|kickoff|gap|plan|prompt|caching|connector|setup|dedup|"
    r"session|whats-on|knowledge|nudge|guard|filer|fix|brief|"
    r"code|build|bootstrap|connections|archive|backfill"
    r")\b",
    re.IGNORECASE,
)

# Negative guard: legit business docs that merely CARRY a cora- token. These named
# families are spared in BOTH scopes EVEN WITH a soft keyword suffix
# (e.g. cora-wishlist-review). But a family name that ALSO carries a STRONG build
# keyword (cora-mapping-rebuild-execution-log) is a genuine build doc and is NOT spared.
_LEGIT_FAMILY_RE = re.compile(
    r"(?<![a-z0-9])cora[-_](?:reference|wishlist|mapping|f3-monitor-privacy)",
    re.IGNORECASE,
)
_CORA_STRONG_BUILD_RE = re.compile(
    r"(?<![a-z0-9])cora[-_].*?\b("
    r"forensic|rebuild|execution-log|cascade|incident-triage|north-star|findings|audit"
    r")\b",
    re.IGNORECASE,
)


def _segments(s: str) -> list[str]:
    """Split a path or source_id on either separator into non-empty segments."""
    return [p for p in re.split(r"[\\/]+", s or "") if p]


def _basename(raw: str) -> str:
    parts = _segments(raw)
    return parts[-1] if parts else (raw or "")


def _contains_subsequence(parts: list[str], seq: tuple[str, ...]) -> bool:
    lp = [p.lower() for p in parts]
    ls = [s.lower() for s in seq]
    n = len(ls)
    if n == 0 or len(lp) < n:
        return False
    return any(lp[i : i + n] == ls for i in range(len(lp) - n + 1))


def _name_is_build_doc(name: str, *, broad: bool = False) -> bool:
    # Normalize underscores to hyphens so \b keyword anchoring works across BOTH
    # separators (CORA_IMPROVEMENT_BACKLOG.md must match, like cora-improvement-backlog).
    norm = (name or "").replace("_", "-")
    is_build = bool(
        _CORA_BUILD_DOC_RE.search(norm)
        or _CORA_LOG_RE.match(norm)
        or (broad and _CORA_BUILD_DOC_BROAD_RE.search(norm))
    )
    if not is_build:
        return False
    # A protected business-doc family is spared ONLY when it carries no STRONG build
    # keyword -- so f3-brand-assets-cora-reference / cora-wishlist-review stay safe, but
    # cora-mapping-rebuild-execution-log (a genuine build doc) is still caught.
    if _LEGIT_FAMILY_RE.search(norm) and not _CORA_STRONG_BUILD_RE.search(norm):
        return False
    return True


def _is_cora_internal(raw: str, *, broad: bool = False) -> bool:
    parts = _segments(raw)
    if _contains_subsequence(parts, _CORA_WORKSPACE_SEGMENTS):
        return True
    name = parts[-1] if parts else (raw or "")
    return _name_is_build_doc(name, broad=broad)


def is_cora_internal_path(path: Path) -> bool:
    """True if a filesystem path is one of Cora's own build/audit/forensic docs."""
    return _is_cora_internal(str(path))


def is_cora_internal_source_id(source_id: str) -> bool:
    """True if a stored KB source_id refers to a Cora build/audit/forensic doc."""
    return _is_cora_internal(source_id or "")


def is_swept_path(path: Path) -> bool:
    """True if a filesystem path is under the _brain/swept/ materialization subtree.

    Drive-materialization (2026-06-29): _brain/swept/{ENTITY}/YYYY-MM-DD.md holds the
    nightly distilled digests. EVERY static-tree KB ingest walk (incremental_sync_static
    AND migrate_static_md — the full rebuild) must skip them, or they feed back into the
    KB (loop + bloat, and a LEX-aggregate digest re-ingested as FNDR-scoped static_md).
    Require BOTH "_brain" AND "swept" segments so the curated _brain layers
    (known-answers / reference / people) are NEVER excluded — they MUST keep ingesting.
    Shared here (with is_cora_internal_path) so a third static walk can't drift again.
    """
    parts_lower = {p.lower() for p in path.parts}
    return "_brain" in parts_lower and "swept" in parts_lower


def is_cora_internal_title(title: str, *, broad: bool = False) -> bool:
    """True if a stored KB ``title`` (a Drive filename) is a Cora build/audit doc.

    Used where the source_id carries no path — chiefly ``drive_sweep`` copies of
    Founder-OS Drive files, whose source_id is a Drive file id. A Drive display name
    may itself contain ``/`` (e.g. a date like "6/4"), so we match the FULL title
    (the keyword search finds the cora- token wherever it sits) AND its basename — we
    must never path-split a filename and lose the token. ``broad=True`` widens to
    Cora's full ops/build doc set; the default stays narrow (build/audit + logs).
    """
    title = title or ""
    return _name_is_build_doc(title, broad=broad) or _name_is_build_doc(
        _basename(title), broad=broad
    )
