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
    It is PATH-keyed, so it covers static_md. On ``drive_sweep`` (bare file id,
    no path) the same folder is closed by its Drive folder id in
    ``KB_EXCLUDED_FOLDER_IDS`` (pinned 2026-09-03 -- the DOOR); the filename rule
    below is that door's BELT, never its sole guard (decisions.md 2026-09-03
    "D-057 IS LEAKING", Doctrine 1).
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
# sweep can never fail-open on a YAML parse error. The first four are dashboard
# backing-stores; keep those in sync with the `kb_ingest: never` /
# `kb_excluded_folders` entries in data/maps/dashboard-access.yaml. The
# copa-bhrf entry is a SEPARATE class (a LEX NDA'd M&A-diligence project folder,
# NOT a dashboard -- deliberately NOT in dashboard-access.yaml); it blocks the
# drive_sweep re-ingest of the copa-bhrf tree after the 2026-07-21 KB purge
# (see is_copa_bhrf_path below + decision §2c). The _shared/projects/cora entry
# is a THIRD class: Cora's own build workspace (D-057) -- see its comment.
KB_EXCLUDED_FOLDER_IDS: frozenset[str] = frozenset(
    {
        "1INi4fLXG23xao-d_yf56Wrbrah54pIBB",  # 00-Founder/insurance/oneamerica (PERSONAL)
        "1BZI6v5pmpgrt7G2dPsAib3u3S-HqB7ZP",  # 02-F3-Energy/projects/capital-raise (HIGHLY CONFIDENTIAL)
        "1NPBNBfx3MMjqQM_WnmL6jOJSaRAQf752",  # 00-Founder/travel-points (PERSONAL)
        "1HEHpMWgkJkHmV1wfWIiT5OhBI0p5p2P-",  # Downloads/OneAmerica-Handoff dup (PERSONAL, F-09 parked #2)
        "112C7ljGRI5VO_ic66fVGQk4kf6IC40HQ",  # 08-Lexington-Services/projects/copa-bhrf (LEX NDA -- 2026-07-21 purge)
        # 01-HJR-Global/accounting/cashflow-ledger -- the 13WCF shadow-ledger
        # mirror. Closes the "carried finding" from the M1 cascade report
        # (_shared/projects/cora/2026-08-05_fndr_13wcf-M1-CASCADE-REPORT.md):
        # until the first mirror run created this folder there was no id to pin,
        # so the drive_sweep door rested entirely on is_finance_worksheet_title
        # -- a FILENAME heuristic carrying the whole boundary. Pinning the folder
        # makes that door PATH-covered and demotes the title rule to a belt.
        #
        # The parent id alone is sufficient AND future-proof: sweep_founders_os
        # passes this set as skip_folder_ids, and its BFS neither processes a
        # skipped folder nor enqueues its subfolders, so the whole subtree is
        # pruned -- today's forecast-snapshots/ and the actuals/, worksheets/,
        # candidates/ and outlook-entities/ folders M2-M4 will add. The flat
        # per-user sweep is covered separately by _expanded_excluded_folder_ids.
        # Verified live 2026-08-05: this id resolves to "cashflow-ledger" under
        # accounting <- 01-HJR-Global <- HJR-Founder-OS.
        "1aDnmz3oY7QZxsH7mv7_ZDu7cUyDWLhy7",  # 01-HJR-Global/accounting/cashflow-ledger (13WCF mirror)
        # _shared/projects/cora -- the Cora build workspace that D-057 exists to
        # keep OUT of the KB. Closes "D-057 IS LEAKING" (decisions.md 2026-09-03,
        # cq-11e9abda254a): the keystone folder rule below
        # (_CORA_WORKSPACE_SEGMENTS) is PATH-keyed, so it covers static_md only;
        # drive_sweep stores a bare Drive file id and NO path, so on that door the
        # folder had rested on the is_cora_internal_title FILENAME heuristic alone
        # since the 6/19 WS1 exclusion shipped -- and 157 of its 550 .md files
        # carry no ``cora`` token at all (the _fndr_/_cora_ naming split), a hard
        # floor no keyword can reach. Live-retrievable on 9/3: the project
        # CLAUDE.md (87 chunks), the system-architecture reference (57), even an
        # entity system prompt (hjrg.md, 25). The pin is the DOOR; the title rule
        # (incl. its ``mirror`` keyword for ZONE-X) stays the BELT.
        #
        # The PARENT is pinned, never the child _mirror/: the child has no id
        # until the first mirror apply creates it, and a pinned parent prunes the
        # whole subtree -- sweep_founders_os passes this set as skip_folder_ids
        # and its BFS neither processes a skipped folder nor enqueues its
        # subfolders (tests/test_drive_sweep.py TestPinnedParentPrunesSubtree);
        # the flat per-user sweep is covered by _expanded_excluded_folder_ids. So
        # the mirror's _mirror/ (created by its first apply) and its _removed/ +
        # _quarantine/ children are covered without their ids ever existing at
        # pin time. The one allowlisted view
        # under this folder (code-session-backlog.md, _KB_ALLOWLIST_BASENAMES)
        # still ingests through static_md -- path-keyed, unaffected by folder ids;
        # only its redundant drive_sweep twin stops (the audit-A1 double ingest).
        # Verified live 2026-09-03 (read-only; forward exact-name chain from
        # FOUNDERS_OS_ROOT_ID with exactly one hit per level AND a reverse
        # parents walk, both agreeing): this id resolves to "cora" under
        # projects <- _shared <- HJR-Founder-OS (221 direct children incl.
        # CLAUDE.md, _notes, design). Pinned by Harrison's 9/3 ruling "A) Option 1".
        "1YNObhKwo8RITgrRbw3MFpf-0hIiLWTx9",  # _shared/projects/cora (D-057 workspace; 2026-09-03 parent pin)
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
    """True if a Drive folder id is KB-excluded: a personal/confidential dashboard
    store, the LEX copa-bhrf NDA folder, the 13WCF ledger, or the Cora build
    workspace (D-057). drive_sweep skips the whole subtree of a pinned id."""
    return bool(folder_id) and folder_id in KB_EXCLUDED_FOLDER_IDS


# Generated finance WORKING STORES Cora writes into the Founder-OS accounting
# tree. Two families, one rule:
#
#   forecast-assist/  -- the A5 S2b weekly worksheet (superseded at 13WCF M3)
#   cashflow-ledger/  -- the 13-week shadow ledger (M1+): forecast snapshots,
#                        QBO actuals, derived outlooks, worksheets, candidates
#
# These are working documents, not knowledge: a cross-portfolio cash-forecast
# written every Monday would static_md-ingest as HJRG chunks. Those chunks are
# unreachable from every Slack channel (no channel routes to HJRG) but ARE
# reachable from founder-scoped surfaces (D-092 MCP, interactive sessions), and
# accreting near-duplicate finance chunks weekly is KB pollution regardless. The
# ledger adds a sharper reason: its candidates/ files are UNTRUSTED INPUT
# (D-123) and its founder outlook carries war-chest and portfolio figures that
# must never become retrievable chunks.
#
# Segment-based rather than folder-id-based on purpose: the folders are created
# by their first run, so there is no id to pin at build time, and this predicate
# is wired at the STORE chokepoint, which covers every connector including
# static_md. Once a folder EXISTS, pin its id in KB_EXCLUDED_FOLDER_IDS too --
# that is what makes the enumeration-time drive_sweep path path-covered instead
# of leaving the title heuristic below load-bearing.
#   cashflow-ledger  -- PINNED 2026-08-05 (id above)
#   forecast-assist  -- not pinned; superseded at 13WCF M3, so the segment rule
#                       carries it out rather than acquiring a new id to retire
_FINANCE_WORKSHEET_SEGMENTS: frozenset[str] = frozenset({
    "forecast-assist",
    "cashflow-ledger",
})

# Generated finance filenames, for the door the segment rule cannot see
# (drive_sweep stores a bare file id and NO path).
#
# EVERY generated ledger artifact is date-prefixed, so the rule anchors on that
# shape rather than on a bare keyword. Both edges matter:
#   * A bare "forecast"/"cashflow-worksheet" substring OVER-matches and silently
#     blocks real business documents forever ("2026-forecast-model.xlsx",
#     "LLC-cashflow-worksheet-v3.xlsx") -- and the store logs only a count, so
#     it is near-undiagnosable.
#   * Anchoring too tightly UNDER-matches Drive-side decoration: a
#     Drive-for-Desktop conflict copy "…_forecast (1).json" or a "Copy of …"
#     prefix must still be caught, so the shape is searched, not full-matched.
_FINANCE_GENERATED_NAME_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[_-].*?"
    r"(forecast|actuals|cashflow-worksheet|prelim-w\d|final-w\d)",
    re.IGNORECASE,
)

# The ledger's non-dated generated files.
_FINANCE_GENERATED_EXACT: frozenset[str] = frozenset({
    "outlook-founder", "ledger",
})


def is_finance_worksheet_path(path_or_source_id: str) -> bool:
    """True if a path sits inside a generated finance working store that must
    never be KB-ingested. Segment-based, case-insensitive, handles ``/`` and ``\\``."""
    segs = {s.lower() for s in _segments(str(path_or_source_id or ""))}
    return bool(segs & _FINANCE_WORKSHEET_SEGMENTS)


def is_finance_worksheet_title(title: str) -> bool:
    """True if a Drive file's TITLE marks it a generated finance working file.

    The path predicate above covers static_md, whose source_id IS the path. It
    does NOT cover ``drive_sweep``, which sets ``source_id`` to a bare Drive file
    id and stores no path -- and ``sweep_founders_os`` walks 01-HJR-Global. So the
    same file would have been ingested as HJRG chunks through the other door.
    Title-keyed, following the COPA meeting-export precedent, and scoped at the
    call site to Drive sources only so an ordinary email mentioning the phrase is
    unaffected.
    """
    raw = str(title or "").lower()
    if not raw:
        return False
    # Match the FULL title as well as its basename: a Drive display name may
    # itself contain "/" (a date like "8/11"), and path-splitting it would drop
    # the very token we are looking for. Same reasoning as is_cora_internal_title.
    base = _basename(raw)
    for name in (raw, base):
        if "forecast-assist" in name:
            return True
        if _FINANCE_GENERATED_NAME_RE.search(name):
            return True
        if name.rsplit(".", 1)[0] in _FINANCE_GENERATED_EXACT:
            return True
    return False


def is_dashboard_store_path(path_or_source_id: str) -> bool:
    """True if a filesystem path, Drive path, or path-shaped source_id sits inside
    a personal / highly-confidential dashboard store (capital-raise, oneamerica,
    travel-points). Segment-based, case-insensitive, handles ``/`` and ``\\``.

    Over-exclusion is bounded to those distinctive folder names and is the safe
    direction here (these stores must never be KB-ingested)."""
    segs = {s.lower() for s in _segments(str(path_or_source_id or ""))}
    return bool(segs & _DASHBOARD_STORE_SEGMENTS)


# ─────────────────────────────────────────────────────────────────────────────
# LEX NDA'd project folders (2026-07-21 KB cleanup, decision §2c): the copa-bhrf
# LBHS-COPA M&A-diligence folder is NDA'd -- its chunks were purged from the KB
# and it must never re-ingest. Unlike the dashboard stores, only ONE canonical
# copy stays on Drive IN PLACE (outside _archive), so a path-segment exclusion at
# the ingest chokepoint (store upsert_documents Step 0 + incremental_sync_static)
# is what keeps it out; the drive_sweep path (no stored path) is covered by the
# copa-bhrf folder-id in KB_EXCLUDED_FOLDER_IDS above. "copa-bhrf" is a full path
# segment, so this never false-matches "Maricopa"/"Copayment"/"copack".
# ─────────────────────────────────────────────────────────────────────────────
_LEX_NDA_SEGMENTS: frozenset[str] = frozenset({"copa-bhrf"})


def is_copa_bhrf_path(path_or_source_id: str) -> bool:
    """True if a filesystem path, Drive path, or path-shaped source_id sits inside
    the LEX copa-bhrf NDA project folder. Segment-based, case-insensitive, handles
    ``/`` and ``\\``. Distinct from the dashboard stores; kept separate so the
    "dashboard" semantics stay clean. Matches the whole ``copa-bhrf`` folder
    segment only -- never a "copa" substring."""
    segs = {s.lower() for s in _segments(str(path_or_source_id or ""))}
    return bool(segs & _LEX_NDA_SEGMENTS)


# The NDA'd LBHS-COPA / Copa Health M&A-diligence MEETINGS (Fireflies transcripts +
# their Drive copies) live OUTSIDE the copa-bhrf project folder, so is_copa_bhrf_path
# (path-segment) can't catch them -- they are keyed by MEETING TITLE. Anchor on the
# whole word "copa" (the acronym / "Copa Health" / "Copa Model"); NEVER a bare
# "voyager" (Lexington's fleet Chrysler Voyager minivans collide with it across the
# corpus) and NEVER a "copa" substring (Maricopa / copayment / copacker are spared by
# the word boundary). Used by BOTH the one-time purge (scripts/purge_copa_transcripts)
# and the forward Fireflies-ingest exclusion.
_COPA_MEETING_TITLE_RE = re.compile(r"\bcopa\b", re.IGNORECASE)


def is_copa_meeting_title(title: str) -> bool:
    """True if a meeting/transcript TITLE names the NDA'd COPA diligence (whole-word
    'copa', case-insensitive). Excludes Maricopa/copayment/copacker (word boundary)
    and bare 'Voyager' (the fleet minivan). Empty/None -> False."""
    return bool(_COPA_MEETING_TITLE_RE.search(str(title or "")))


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

# KB ALLOWLIST (2026-07-28, code-session queue): a small set of GENERATED views
# that live under the Cora workspace but ARE intended as org-answerable knowledge
# ("@Cora what's in the build queue"). These override BOTH the keystone folder
# rule and the filename rule so they ingest. Exact basename match only (never a
# substring), case-insensitive -- a tightly-scoped positive exception so the
# folder-rule surface is not widened. The backlog is a clean generated status
# list, not audit prose, so it does not reintroduce the fabricated-diagnostic
# failure mode the folder rule guards against.
_KB_ALLOWLIST_BASENAMES: frozenset[str] = frozenset({"code-session-backlog.md"})


def _is_kb_allowlisted(raw: str) -> bool:
    """True if a path / source_id / title's basename is an explicitly-allowlisted
    generated view that must ingest despite the Cora-workspace exclusion."""
    return _basename(str(raw or "")).lower() in _KB_ALLOWLIST_BASENAMES

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
    r"audit|review|sweep|exec-summary|backlog|"
    # ``mirror`` (2026-09-03, claude-workspace mirror lane): every file that
    # scripts/mirror_claude_workspace.py writes into the KB-EXCLUDED ZONE-X
    # folder (_shared/projects/cora/_mirror/) carries a ``cora-mirror-`` prefix
    # -- Cowork task prompt BODIES (LEX-prefixed tasks included) and the cora
    # repo's own agent memory. The folder rule keeps them out of static_md; this
    # keyword is the drive_sweep belt (a title-only door with no path) so the
    # 06:00 sweep can never ingest a body through the other door (D-057/D-086).
    # TARGETED, not broad-only, so both scopes and the default purge scope see it.
    r"mirror"
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
    if _is_kb_allowlisted(raw):
        return False  # generated views ingest despite the workspace/filename rules
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
    if _is_kb_allowlisted(title):
        return False  # generated views ingest despite the filename rule
    return _name_is_build_doc(title, broad=broad) or _name_is_build_doc(
        _basename(title), broad=broad
    )
