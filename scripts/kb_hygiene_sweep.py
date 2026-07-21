#!/usr/bin/env python3
r"""KB-staleness hygiene sweep -- the recurring supersede->archive->purge LOOP.

Turns the one-time 2026-07-21 KB cleanup (D-086) into a STANDING monthly loop so
superseded doctrine never keeps answering. Built ON the shared move/purge/manifest
core (src/cora/kb_archive.py) -- this script adds the recurring drivers:

  --marked     Tier 2: archive+purge every .md carrying a machine-readable
               ``<!-- KB-STATUS: SUPERSEDED ... -->`` banner. Dry-run by DEFAULT.
               --apply auto-applies SMALL sweeps live (WAL DELETE, no VACUUM, no
               Cora stop); a LARGE sweep (> --live-purge-max chunks OR
               > --live-move-max files) ESCALATES instead of auto-running -- it
               must go through deployment/run-kb-hygiene-apply.ps1 (Cora stopped),
               invoked here with --allow-large.

  --proactive  Tier 3: PROPOSE-ONLY staleness candidates (near-dupe / TTL one-offs
               / resolved decisions-pending). Never moves or purges. (Slice C.)

  --gc         Retention: hard-delete _archive files older than --purge-after-days,
               never inside the --restore-days easy-restore window, never a
               non-_archive path. (Slice D.)

  --revert MANIFEST   Move files back from _archive (files only; see kb_archive).
  --from-manifest MANIFEST   Finish a purge from a prior manifest's persisted
               chunk_ids (no re-walk).

SAFETY RAILS (non-negotiable, inherited from D-086 + kb_archive):
  * Archive, never delete files. Reversible mirrored _archive/ tree + manifest.
  * Dry-run is the DEFAULT and is read-only.
  * Layered over-inclusion defense: walk-skip (dotfiles/_archive/PHI/swept) ->
    banner-only collection -> HOLD hard-guard (watchtower/oneamerica/capital-raise/
    travel-points + copa-bhrf + the kb_exclusions confidential-store predicates) ->
    KEEP-as-class (a banner on a class file WARNS, never archives) -> the manifest.
  * Confidential stores (oneamerica/capital-raise/travel-points/copa-bhrf) are
    NEVER touched -- skipped at walk AND refused by the HOLD guard (fail-closed).
  * The drive-copy purge is self-guarded (a title mapping to > 2 Drive file-ids or
    a scaffolding basename is refused).

Usage (from repo root):
    .venv\Scripts\python.exe scripts\kb_hygiene_sweep.py --marked               # DRY-RUN
    .venv\Scripts\python.exe scripts\kb_hygiene_sweep.py --marked --apply       # auto-apply small; escalate large
    .venv\Scripts\python.exe scripts\kb_hygiene_sweep.py --marked --apply --allow-large   # Cora STOPPED (via the apply wrapper)
    .venv\Scripts\python.exe scripts\kb_hygiene_sweep.py --revert logs\kb-hygiene-manifest-<ts>.json

Exit codes: 0 ok, 1 fatal, 2 HOLD-guard tripped, 3 escalated (large sweep, not applied).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

from cora import kb_archive  # noqa: E402
from cora.kb_archive import ArchiveConfig, HoldGuardTripped  # noqa: E402
from cora.kb_exclusions import (  # noqa: E402
    is_copa_bhrf_path,
    is_cora_internal_path,  # noqa: F401 (available for future proactive detectors)
    is_dashboard_store_path,
    is_swept_path,
)

FOUNDER_OS_ROOT = Path(r"G:\My Drive\HJR-Founder-OS")
ARCHIVE_ROOT = FOUNDER_OS_ROOT / "_archive"
KB_DB_PATH = _REPO / "data" / "cora_kb.db"
LOG_DIR = _REPO / "logs"
_BATCH = 500
_HEAD_CHARS = 4096  # how many bytes of a file head to scan for the banner

# Auto-apply escalation ceilings (§7A): a run purging more chunks / moving more
# files than these does NOT auto-apply live -- it escalates to the Cora-stopped
# apply wrapper. Over-inclusion-at-scale guard.
DEFAULT_LIVE_PURGE_MAX = 500
DEFAULT_LIVE_MOVE_MAX = 100

# Retention windows (§7A). GC never deletes inside RESTORE_DAYS; it deletes at
# PURGE_AFTER_DAYS (>= RESTORE_DAYS, clamped).
DEFAULT_RESTORE_DAYS = 30
DEFAULT_PURGE_AFTER_DAYS = 180

# PHI folder segments -- MIRRORS scripts/incremental_sync_static.PHI_BLACKLIST_SEGMENTS
# (kept in sync so the sweep skips the same PHI folders the ingest walk skips).
PHI_BLACKLIST_SEGMENTS = {"consumers", "clients", "phi", "clinical", "ehr"}

# HOLD / KEEP guard sets (mirror the D-086 disposition lock; the sweep is stricter
# -- copa_loose_dup is None so ANY copa path aborts, class_exceptions is empty so a
# banner on a class file always WARNS instead of archiving).
HOLD_SEGMENTS = frozenset({"watchtower", "oneamerica", "capital-raise", "travel-points"})
KEEP_CLASS_BASENAMES = frozenset({
    "claude.md", "readme.md", "bootstrap.txt", "_context.md",
    "canonical-assets.md", "content-calendar.md",
})
KEEP_CLASS_SEGMENTS = frozenset({
    "production", "_session-captures", "_strategy-memos", "memory", "playbooks",
})
KEEP_CLASS_BASENAME_SUBSTR = ("brand-guidelines",)
_SCAFFOLD_BASENAMES = frozenset({
    "00-readme.md", "01-asana-tasks-to-create.md", "02-decisions-md-appends.md",
    "03-slack-summaries.md", "04-project-notes.md", "05-asana-cross-ref.md",
    "06-closure-proposals.md", "readme.md", "claude.md", "_context.md",
    "canonical-assets.md", "claude-ai-project-setup.md", "cascade-log.md",
})

# The banner (§2). Canonical token: KB-STATUS: SUPERSEDED <date> by <ref> [-- <reason>].
# The reason separator may be an em dash, "--", or " - " (all with surrounding
# whitespace) so both the ASCII and unicode conventions parse.
_BANNER_RE = re.compile(
    r"<!--\s*KB-STATUS:\s*SUPERSEDED\s+"
    r"(?P<date>\d{4}-\d{2}-\d{2})\s+by\s+"
    r"(?P<ref>.+?)"
    r"(?:\s+(?:—|--|-)\s+(?P<reason>.+?))?"
    r"\s*-->",
    re.IGNORECASE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("kb-hygiene-sweep")


def hygiene_cfg() -> ArchiveConfig:
    """The sweep's ArchiveConfig. Stricter than the D-086 tool: no copa whole-folder
    purge, no copa loose-dup exception, no class-exception bypass, and the
    kb_exclusions confidential-store predicates folded in as extra HOLD predicates
    (union, fail-closed)."""
    return ArchiveConfig(
        founder_os_root=FOUNDER_OS_ROOT,
        archive_root=ARCHIVE_ROOT,
        hold_segments=HOLD_SEGMENTS,
        keep_class_basenames=KEEP_CLASS_BASENAMES,
        keep_class_segments=KEEP_CLASS_SEGMENTS,
        keep_class_basename_substr=KEEP_CLASS_BASENAME_SUBSTR,
        class_exceptions=frozenset(),
        keep_substrings=(),
        scaffold_basenames=_SCAFFOLD_BASENAMES,
        drive_title_max_fileids=2,
        copa_purge_glob=None,
        copa_drive_titles=(),
        copa_loose_dup=None,
        batch=_BATCH,
        extra_hold_predicates=(is_dashboard_store_path, is_copa_bhrf_path),
    )


# ── banner + walk ──────────────────────────────────────────────────────────────
def parse_banner(head_text: str) -> dict | None:
    """Return {status, date, ref, reason} for the first KB-STATUS: SUPERSEDED
    banner in the head text, else None."""
    m = _BANNER_RE.search(head_text or "")
    if not m:
        return None
    return {
        "status": "SUPERSEDED",
        "date": m.group("date"),
        "ref": (m.group("ref") or "").strip(),
        "reason": (m.group("reason") or "").strip(),
    }


def is_phi_path(path: Path) -> bool:
    parts_lower = {p.lower() for p in path.parts}
    return bool(parts_lower & PHI_BLACKLIST_SEGMENTS)


def _walk_skip(path: Path) -> bool:
    """Structural walk skips mirroring incremental_sync_static (dotfiles, _archive,
    PHI folders, _brain/swept). Confidential-store + HOLD paths are handled by
    hold_reason in scan_marked (so a banner there is WARNED, not silently swept)."""
    if any(part.startswith(".") for part in path.parts):
        return True
    if "_archive" in str(path).lower():
        return True
    if is_phi_path(path):
        return True
    if is_swept_path(path):
        return True
    return False


def _read_head(path: Path, n: int = _HEAD_CHARS) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.read(n)
    except OSError:
        return ""


def scan_marked(cfg: ArchiveConfig) -> tuple[list[dict], list[dict]]:
    """Walk the Founder-OS tree for banner'd .md files.
    Returns (marked, refused): marked = [{rel, date, ref, reason}] eligible for
    archival; refused = [{rel, reason}] for banner'd files sitting in a HOLD /
    confidential path (never archived)."""
    marked: list[dict] = []
    refused: list[dict] = []
    for path in cfg.founder_os_root.rglob("*.md"):
        if not path.is_file():
            continue
        if _walk_skip(path):
            continue
        head = _read_head(path)
        banner = parse_banner(head)
        if not banner:
            continue
        try:
            rel = kb_archive.rel(path, cfg)
        except ValueError:
            continue
        hr = kb_archive.hold_reason(rel, cfg)
        if hr:
            refused.append({"rel": rel, "reason": hr})
            log.warning("BANNER on held/confidential path -- REFUSED (never archived): %s (%s)", rel, hr)
            continue
        marked.append({"rel": rel, **banner})
    return marked, refused


def select_marked(marked: list[dict], cfg: ArchiveConfig):
    """Run the banner'd relpaths through the shared manifest builder as a single
    cluster. build_move_manifest applies KEEP-as-class (a banner on a class file
    lands in class_filtered = WARN, never archived) and the HOLD belt."""
    relpaths = [m["rel"] for m in marked]
    cluster = [{
        "id": "marked", "section": "KB-STATUS: SUPERSEDED banners",
        "globs": [], "explicit": relpaths, "keep": [],
        "expected": str(len(relpaths)), "purge": True,
    }]
    return kb_archive.build_move_manifest(cluster, cfg)


# ── marked run (dry-run / apply / escalate) ────────────────────────────────────
def run_marked(cfg: ArchiveConfig, db_path: Path, *, apply: bool, allow_large: bool,
               live_purge_max: int, live_move_max: int, drive_purge: bool) -> dict:
    """Execute (or plan) the marked tier. Returns a result dict for the report."""
    marked, refused = scan_marked(cfg)
    log.info("Marked scan: %d banner'd file(s) eligible, %d refused (held/confidential).",
             len(marked), len(refused))

    archive, report, class_filtered, substr_filtered = select_marked(marked, cfg)
    if class_filtered:
        log.warning("%d banner'd file(s) are KEEP-as-class -- WARNED, not archived:", len(class_filtered))
        for p, w in class_filtered:
            log.warning("   KEEP-class banner (not archived): %s [%s]", p, w)

    # Purge selection (read-only; safe while Cora is live).
    static_ids: list[str] = []
    drive_ids: list[str] = []
    moved_static = copa_static = 0
    drive_included: list[dict] = []
    drive_skipped: list[dict] = []
    if archive:
        ro = kb_archive.connect_ro(db_path)
        try:
            static_ids, moved_static, copa_static = kb_archive.select_static_purge(ro, archive, cfg)
            if drive_purge:
                drive_ids, drive_included, drive_skipped = kb_archive.select_drive_purge(ro, archive, cfg)
        finally:
            ro.close()
    all_purge = sorted(set(static_ids) | set(drive_ids))

    moves = kb_archive.plan_moves(archive, cfg)
    mode = "APPLY" if apply else "DRY-RUN"

    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    manifest = LOG_DIR / f"kb-hygiene-manifest-{ts}.json"
    kb_archive.write_manifest(
        manifest, cfg, mode=mode, report=report, moves=moves,
        class_filtered=class_filtered, substr_filtered=substr_filtered,
        static_ids=static_ids, drive_ids=drive_ids, moved_static=moved_static,
        copa_static=copa_static, drive_included=drive_included,
        drive_skipped=drive_skipped, purge_enabled=drive_purge or bool(static_ids))

    result = {
        "mode": mode,
        "manifest": str(manifest),
        "marked_eligible": len(marked),
        "refused_held": refused,
        "keep_class_warned": [{"path": p, "why": w} for p, w in class_filtered],
        "to_archive": len(archive),
        "purge_chunks": len(all_purge),
        "static_chunks": len(static_ids),
        "drive_chunks": len(drive_ids),
        "drive_skipped_ambiguous": drive_skipped,
        "escalated": False,
        "applied": False,
    }

    if not apply:
        log.info("DRY-RUN: %d file(s) would be archived, %d chunk(s) purged. Manifest: %s",
                 len(archive), len(all_purge), manifest)
        return result

    # Escalation gate (§7A): a large sweep does NOT auto-apply live.
    too_big = len(all_purge) > live_purge_max or len(archive) > live_move_max
    if too_big and not allow_large:
        result["escalated"] = True
        log.warning(
            "ESCALATED (not applied): sweep would move %d file(s) / purge %d chunk(s) "
            "-- exceeds live ceiling (moves>%d or chunks>%d). Run the Cora-stopped apply: "
            "deployment\\run-kb-hygiene-apply.ps1  (which invokes --marked --apply --allow-large).",
            len(archive), len(all_purge), live_move_max, live_purge_max)
        return result

    # Apply. Small sweeps run live (WAL DELETE, no VACUUM, no Cora stop). A large
    # sweep reaches here only with --allow-large (Cora stopped by the wrapper).
    if archive:
        log.info("Moving %d file(s) into _archive ...", len(archive))
        try:
            kb_archive.execute_moves(moves, cfg)
        finally:
            kb_archive.write_manifest(
                manifest, cfg, mode=mode, report=report, moves=moves,
                class_filtered=class_filtered, substr_filtered=substr_filtered,
                static_ids=static_ids, drive_ids=drive_ids, moved_static=moved_static,
                copa_static=copa_static, drive_included=drive_included,
                drive_skipped=drive_skipped, purge_enabled=drive_purge or bool(static_ids))
        moved_ok = sum(1 for m in moves if m.get("moved"))
        failed = [m["src_rel"] for m in moves if str(m.get("result", "")).startswith("error")]
        log.info("Moved %d/%d file(s).", moved_ok, len(moves))
        if failed:
            log.error("%d file(s) FAILED to move (retryable): %s", len(failed), failed[:10])

    if all_purge:
        log.info("Purging %d chunk(s) from knowledge_chunks + both vec tables ...", len(all_purge))
        rw = kb_archive.connect_rw(db_path)
        try:
            totals = kb_archive.delete_chunks(rw, all_purge, cfg)
            log.info("Deleted: %s", totals)
        finally:
            rw.close()

    result["applied"] = True
    return result


# ── proactive tier (Slice C -- PROPOSE-ONLY; never moves or purges) ────────────
# Defaults tuned on dry-run before enabling; near-dupe threshold starts HIGH (a
# near-identical successor), well above the reconciliation "same topic" 0.72 floor.
DEFAULT_DUP_THRESHOLD = 0.90
DEFAULT_TTL_DAYS = 75
DEFAULT_PENDING_JACCARD = 0.55
DEFAULT_MAX_PROPOSALS = 25

_ONEOFF_PATTERNS = ("chrome-agent", "resume-prompt", "kickoff", "bootstrap",
                    "session-prompt", "cowork-prompt", "code-prompt")
_FILE_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9\-]{2,}")
_STOPWORDS = frozenset(
    "the a an and or of to in for on with is are was were be this that it as at by "
    "from we our you your they their but not have has had will would can may see per".split()
)


def _decisions_pending_path() -> Path:
    return Path(os.environ.get("FNDR_DECISIONS_PENDING_PATH")
                or (FOUNDER_OS_ROOT / "memory" / "decisions-pending.md"))


def _decisions_path() -> Path:
    return Path(os.environ.get("STRATEGY_DECISIONS_PATH")
                or (FOUNDER_OS_ROOT / "memory" / "decisions.md"))


def _file_date_ts(path: Path, mtime: float) -> float:
    m = _FILE_DATE_RE.search(path.name)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d").timestamp()
        except ValueError:
            pass
    return mtime


def _tokens(s: str) -> set[str]:
    return {w for w in _TOKEN_RE.findall((s or "").lower()) if w not in _STOPWORDS}


def _project_dir(rel: str) -> str:
    """The project folder for a file: the parent of a `_notes` folder, else the
    file's own parent."""
    parent = Path(rel).parent
    if parent.name.lower() == "_notes":
        return str(parent.parent)
    return str(parent)


def gather_candidate_files(cfg: ArchiveConfig) -> list[dict]:
    """Walk once; return eligible non-banner'd, non-held, non-KEEP-class file
    records for the propose-only detectors."""
    out: list[dict] = []
    for path in cfg.founder_os_root.rglob("*.md"):
        if not path.is_file() or _walk_skip(path):
            continue
        try:
            rel = kb_archive.rel(path, cfg)
        except ValueError:
            continue
        if kb_archive.hold_reason(rel, cfg):
            continue
        if kb_archive.is_keep_as_class(rel, cfg):
            continue
        if parse_banner(_read_head(path)):
            continue  # already marked -> the marked tier's job
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        out.append({"path": path, "rel": rel, "name": path.name.lower(),
                    "mtime": mtime, "date_ts": _file_date_ts(path, mtime)})
    return out


def _fetch_file_centroid(conn, source_id: str) -> list[float] | None:
    """Mean-pool a static_md file's chunk vectors from knowledge_vec_f32 (zero embed
    cost). Returns None if the file has no vectors (never ingested / excluded)."""
    import struct

    from cora.knowledge_base.embeddings import EMBEDDING_DIM
    rows = conn.execute(
        "SELECT f.embedding FROM knowledge_chunks k JOIN knowledge_vec_f32 f "
        "ON f.chunk_id = k.chunk_id WHERE k.source='static_md' AND k.source_id=?",
        (source_id,),
    ).fetchall()
    vecs: list[list[float]] = []
    for (blob,) in rows:
        try:
            vecs.append(list(struct.unpack(f"{EMBEDDING_DIM}f", blob)))
        except Exception:  # noqa: BLE001
            continue
    if not vecs:
        return None
    dim = len(vecs[0])
    centroid = [0.0] * dim
    used = 0
    for v in vecs:
        if len(v) != dim:
            continue
        used += 1
        for i in range(dim):
            centroid[i] += v[i]
    if used == 0:
        return None
    return [x / used for x in centroid]


def detect_near_dupes(cfg: ArchiveConfig, db_path: Path, files: list[dict], *,
                      threshold: float, max_proposals: int) -> list[dict]:
    """Within each folder, PROPOSE archiving the OLDER of any dated pair whose
    doc-level centroid cosine >= threshold. Propose-only; never acts."""
    from cora.reconciliation_engine import _cosine_sim

    groups: dict[str, list[dict]] = {}
    for f in files:
        groups.setdefault(str(Path(f["rel"]).parent), []).append(f)

    proposals: list[dict] = []
    conn = kb_archive.connect_ro(db_path)
    try:
        for group in groups.values():
            if len(group) < 2:
                continue
            for f in group:
                if "centroid" not in f:
                    try:
                        f["centroid"] = _fetch_file_centroid(conn, f["rel"])
                    except Exception:  # noqa: BLE001 -- fail-soft, skip the file
                        f["centroid"] = None
            withvec = sorted((f for f in group if f.get("centroid")), key=lambda f: f["date_ts"])
            proposed: set[str] = set()
            for i in range(len(withvec)):
                older = withvec[i]
                if older["rel"] in proposed:
                    continue
                for j in range(i + 1, len(withvec)):
                    newer = withvec[j]
                    sim = _cosine_sim(older["centroid"], newer["centroid"])
                    if sim >= threshold:
                        proposals.append({
                            "kind": "near-dupe",
                            "path": older["rel"],
                            "superseded_by": newer["rel"],
                            "cosine": round(sim, 4),
                            "reason": f"near-duplicate of newer {newer['rel']} (cosine {sim:.3f})",
                        })
                        proposed.add(older["rel"])
                        break
    finally:
        conn.close()
    proposals.sort(key=lambda p: -p["cosine"])
    return proposals[:max_proposals]


def detect_ttl_oneoffs(cfg: ArchiveConfig, files: list[dict], *, ttl_days: int,
                       now_ts: float, max_proposals: int) -> list[dict]:
    """PROPOSE archiving dated one-off _notes docs (chrome-agent / RESUME-PROMPT /
    KICKOFF / bootstrap / *-prompt) older than ttl_days whose project has newer
    activity. ALWAYS keeps the latest RESUME-PROMPT per project."""
    latest_resume: dict[str, dict] = {}
    newest_activity: dict[str, float] = {}
    for f in files:
        pd = _project_dir(f["rel"])
        newest_activity[pd] = max(newest_activity.get(pd, 0.0), f["mtime"])
        if "resume-prompt" in f["name"]:
            cur = latest_resume.get(pd)
            if cur is None or f["date_ts"] > cur["date_ts"]:
                latest_resume[pd] = f

    proposals: list[dict] = []
    for f in files:
        rel, name = f["rel"], f["name"]
        parent_segs = {s.lower() for s in Path(rel).parts[:-1]}
        if "_notes" not in parent_segs:
            continue
        if not any(pat in name for pat in _ONEOFF_PATTERNS):
            continue
        age_days = (now_ts - f["date_ts"]) / 86400.0
        if age_days < ttl_days:
            continue
        pd = _project_dir(rel)
        if "resume-prompt" in name and latest_resume.get(pd) is f:
            continue  # keep the latest RESUME-PROMPT per project
        if newest_activity.get(pd, 0.0) <= f["mtime"]:
            continue  # no newer activity in the project -> not clearly superseded
        proposals.append({
            "kind": "ttl-oneoff",
            "path": rel,
            "age_days": int(age_days),
            "reason": f"one-off ({name}) ~{int(age_days)}d old; project has newer activity",
        })
    proposals.sort(key=lambda p: -p["age_days"])
    return proposals[:max_proposals]


def detect_resolved_pending(pending_path: Path, decisions_path: Path, *,
                            jaccard: float, max_proposals: int) -> list[dict]:
    """PROPOSE closing decisions-pending items whose outcome already appears in
    decisions.md (token-overlap match). Fail-soft if either file is unreadable.
    The >7-day stalled-P0 escalation is a SEPARATE live task (run_due_date_escalation)
    -- this detector only surfaces the resolved-but-not-removed hygiene gap."""
    proposals: list[dict] = []
    try:
        if not pending_path.exists() or not decisions_path.exists():
            return proposals
        ptext = pending_path.read_text(encoding="utf-8", errors="replace")
        dtext = decisions_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return proposals

    items = [ln.strip() for ln in ptext.splitlines() if ln.strip().startswith(("-", "*", "+"))]
    dlines = [t for t in (_tokens(ln) for ln in dtext.splitlines()) if len(t) >= 4]

    for item in items:
        it = _tokens(item)
        if len(it) < 4:
            continue
        best = 0.0
        for dt in dlines:
            inter = len(it & dt)
            if inter == 0:
                continue
            j = inter / len(it | dt)
            if j > best:
                best = j
        if best >= jaccard:
            proposals.append({
                "kind": "resolved-pending",
                "item": item[:200],
                "match_jaccard": round(best, 3),
                "reason": f"pending item may already be resolved in decisions.md (overlap {best:.2f})",
            })
    proposals.sort(key=lambda p: -p["match_jaccard"])
    return proposals[:max_proposals]


def run_proactive(cfg: ArchiveConfig, db_path: Path, *, dup_threshold: float,
                  ttl_days: int, jaccard: float, max_proposals: int, now_ts: float) -> dict:
    files = gather_candidate_files(cfg)
    near = detect_near_dupes(cfg, db_path, files, threshold=dup_threshold, max_proposals=max_proposals)
    ttl = detect_ttl_oneoffs(cfg, files, ttl_days=ttl_days, now_ts=now_ts, max_proposals=max_proposals)
    resolved = detect_resolved_pending(_decisions_pending_path(), _decisions_path(),
                                       jaccard=jaccard, max_proposals=max_proposals)
    log.info("Proactive candidates (PROPOSE-ONLY): %d near-dupe, %d ttl-oneoff, %d resolved-pending "
             "(from %d candidate files).", len(near), len(ttl), len(resolved), len(files))
    return {
        "candidate_files_scanned": len(files),
        "near_dupes": near,
        "ttl_oneoffs": ttl,
        "resolved_pending": resolved,
    }


# ── retention GC (Slice D) ─────────────────────────────────────────────────────
# GC reads ONLY this loop's manifests (never the one-time D-086
# archive-founder-os-manifest-*), so the big cleanup archive is never GC'd here.
_HYGIENE_MANIFEST_GLOB = "kb-hygiene-manifest-*.json"


def run_gc(cfg: ArchiveConfig, *, restore_days: int, purge_after_days: int,
           apply: bool, now_ts: float) -> dict:
    """Retention: hard-delete _archive files whose sweep manifest is older than
    purge_after_days. NEVER inside the restore_days easy-restore window (the
    effective threshold is clamped up to restore_days), and NEVER a path outside
    _archive (resolved-path containment check). Deletes FILES only -- KB chunks
    were already purged at archive time; the KB .bak is the deeper fallback."""
    effective = max(purge_after_days, restore_days)  # never delete inside restore window
    arch_root = cfg.archive_root.resolve()
    result = {"effective_purge_after_days": effective, "restore_days": restore_days,
              "applied": apply, "targets": [], "errors": [], "manifests_aged": 0}
    for man_path in sorted(LOG_DIR.glob(_HYGIENE_MANIFEST_GLOB)):
        try:
            data = json.loads(man_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        dstr = data.get("archived_date") or str(data.get("generated_at", ""))[:10]
        try:
            man_ts = datetime.strptime(dstr, "%Y-%m-%d").timestamp()
        except ValueError:
            continue
        if (now_ts - man_ts) / 86400.0 < effective:
            continue  # within retention -> keep
        result["manifests_aged"] += 1
        for m in data.get("moves", []):
            if not m.get("moved"):
                continue
            try:
                target = (cfg.archive_root / m["src_rel"]).resolve()
            except (OSError, ValueError):
                continue
            # SAFETY (§7D): the target MUST be under _archive -- never a live path.
            try:
                target.relative_to(arch_root)
            except ValueError:
                result["errors"].append({"rel": m["src_rel"], "why": "not under _archive -- refused"})
                continue
            if not target.exists():
                continue
            result["targets"].append(m["src_rel"])
            if apply:
                try:
                    target.unlink()
                except OSError as exc:
                    result["errors"].append({"rel": m["src_rel"], "why": str(exc)})
    result["count"] = len(result["targets"])
    log.info("GC: %d aged _archive file(s) %s (retention %dd, restore floor %dd).",
             result["count"], "deleted" if apply else "eligible (dry-run)",
             effective, restore_days)
    return result


def run_from_manifest(manifest_path: Path, db_path: Path, *, apply: bool) -> dict:
    """Finish a purge from a prior manifest's persisted chunk_ids (no re-walk).
    Recovery path for a crashed apply."""
    data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    pg = data.get("purge", {})
    all_purge = sorted(set(pg.get("static_chunk_ids", [])) | set(pg.get("drive_chunk_ids", [])))
    log.info("from-manifest: %d chunk(s) to purge (no re-walk).", len(all_purge))
    result = {"chunks": len(all_purge), "applied": False}
    if apply and all_purge:
        rw = kb_archive.connect_rw(db_path)
        try:
            totals = kb_archive.delete_chunks(rw, all_purge)
            log.info("Deleted: %s", totals)
        finally:
            rw.close()
        result["applied"] = True
    return result


# ── report / DM (Slice D) ──────────────────────────────────────────────────────
HARRISON_SLACK_ID = "U0B2RM2JYJ1"


def compose_report(report: dict) -> str:
    """Compact mrkdwn digest for Harrison: what the marked tier did + proactive
    candidates to review + GC. Alarms/actions first."""
    lines = [":broom: *KB Hygiene Sweep*"]
    mk = report.get("marked")
    if mk:
        verb = "archived" if mk.get("applied") else "would archive"
        head = f"*MARKED*: {mk['to_archive']} banner'd file(s) {verb}, {mk['purge_chunks']} chunk(s) purged."
        if mk.get("escalated"):
            head = (f":warning: *MARKED ESCALATED (NOT applied)*: {mk['to_archive']} file(s) / "
                    f"{mk['purge_chunks']} chunk(s) exceed the live ceiling -- run "
                    "`deployment\\run-kb-hygiene-apply.ps1` (Cora stopped).")
        lines.append(head)
        if mk.get("refused_held"):
            lines.append(f"   - {len(mk['refused_held'])} banner(s) on held/confidential paths REFUSED.")
        if mk.get("keep_class_warned"):
            lines.append(f"   - {len(mk['keep_class_warned'])} banner(s) on KEEP-as-class files WARNED (not archived).")
    pro = report.get("proactive")
    if pro:
        lines.append("*PROACTIVE* (review, then add the KB-STATUS banner to the real ones):")
        for key, label in (("near_dupes", "near-dupe"), ("ttl_oneoffs", "ttl one-off"),
                           ("resolved_pending", "resolved decisions-pending")):
            items = pro.get(key, [])
            lines.append(f"   - {label}: {len(items)}")
            for it in items[:5]:
                lines.append(f"       • {it.get('path') or it.get('item', '')}")
    gc = report.get("gc")
    if gc:
        verb = "deleted" if gc.get("applied") else "eligible"
        lines.append(f"*GC*: {gc['count']} aged _archive file(s) {verb} "
                     f"(retention {gc['effective_purge_after_days']}d).")
    if mk and mk.get("manifest"):
        lines.append(f"_Manifest: {mk['manifest']}_")
    return "\n".join(lines)


def deliver_report(text: str) -> bool:
    """DM the report to Harrison ONLY (hard-coded recipient). Sanitized via
    slack_egress (B1 doctrine) before the post."""
    from slack_sdk import WebClient

    from cora import slack_egress
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        log.error("kb-hygiene: SLACK_BOT_TOKEN not set -- cannot DM report")
        return False
    try:
        safe = slack_egress.sanitize_text(text)
    except Exception:  # noqa: BLE001
        safe = text
    try:
        client = WebClient(token=token)
        resp = client.conversations_open(users=[HARRISON_SLACK_ID])
        client.chat_postMessage(channel=resp["channel"]["id"], text=safe[:39000])
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("kb-hygiene: DM delivery failed: %s", exc)
        return False


# ── main ────────────────────────────────────────────────────────────────────────
def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="KB-staleness hygiene sweep (dry-run default).")
    ap.add_argument("--marked", action="store_true", help="Run the banner-driven archive+purge tier.")
    ap.add_argument("--proactive", action="store_true", help="Run the propose-only detectors (Slice C).")
    ap.add_argument("--gc", action="store_true", help="Retention: hard-delete aged _archive files (Slice D).")
    ap.add_argument("--apply", action="store_true", help="Execute (default is a read-only dry-run).")
    ap.add_argument("--dry-run", action="store_true", help="Report only (the default).")
    ap.add_argument("--allow-large", action="store_true",
                    help="Bypass the auto-apply escalation ceiling (Cora STOPPED -- via the apply wrapper).")
    ap.add_argument("--no-drive-purge", action="store_true",
                    help="Skip purging drive_sweep/drive_asset copies (static_md purge only).")
    ap.add_argument("--live-purge-max", type=int, default=DEFAULT_LIVE_PURGE_MAX)
    ap.add_argument("--live-move-max", type=int, default=DEFAULT_LIVE_MOVE_MAX)
    ap.add_argument("--restore-days", type=int, default=DEFAULT_RESTORE_DAYS)
    ap.add_argument("--purge-after-days", type=int, default=DEFAULT_PURGE_AFTER_DAYS)
    ap.add_argument("--dup-threshold", type=float, default=DEFAULT_DUP_THRESHOLD,
                    help="near-dupe centroid-cosine threshold (proactive).")
    ap.add_argument("--ttl-days", type=int, default=DEFAULT_TTL_DAYS,
                    help="age past which a one-off _notes doc is proposed (proactive).")
    ap.add_argument("--jaccard", type=float, default=DEFAULT_PENDING_JACCARD,
                    help="decisions-pending resolved-match token-overlap threshold.")
    ap.add_argument("--max-proposals", type=int, default=DEFAULT_MAX_PROPOSALS,
                    help="cap per proactive detector.")
    ap.add_argument("--db", default=str(KB_DB_PATH), help="Path to the KB sqlite DB.")
    ap.add_argument("--revert", metavar="MANIFEST", help="Reverse the moves recorded in a manifest JSON.")
    ap.add_argument("--from-manifest", metavar="MANIFEST",
                    help="Finish a purge from a prior manifest's persisted chunk_ids (no re-walk).")
    ap.add_argument("--report", metavar="PATH", help="Write the candidate/run report JSON to PATH.")
    ap.add_argument("--slack", action="store_true", help="DM the composed report to Harrison.")
    return ap


def main() -> int:
    args = build_arg_parser().parse_args()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    cfg = hygiene_cfg()

    if args.revert:
        return kb_archive.revert(Path(args.revert), cfg)

    if not FOUNDER_OS_ROOT.exists():
        log.error("Founder OS root not found: %s", FOUNDER_OS_ROOT)
        return 1
    db_path = Path(args.db)
    if not db_path.exists():
        log.error("KB database not found: %s", db_path)
        return 1

    apply_changes = args.apply and not args.dry_run

    if args.from_manifest:
        run_from_manifest(Path(args.from_manifest), db_path, apply=apply_changes)
        return 0

    # Default action: if no tier flag given, run marked (dry-run) as the safe default.
    run_any = args.marked or args.proactive or args.gc
    if not run_any:
        args.marked = True

    report: dict = {"generated_at": datetime.now().isoformat(timespec="seconds")}

    if args.marked:
        try:
            report["marked"] = run_marked(
                cfg, db_path, apply=apply_changes, allow_large=args.allow_large,
                live_purge_max=args.live_purge_max, live_move_max=args.live_move_max,
                drive_purge=not args.no_drive_purge)
        except HoldGuardTripped as exc:
            log.error("%s", exc)
            return 2

    if args.proactive:
        report["proactive"] = run_proactive(
            cfg, db_path, dup_threshold=args.dup_threshold, ttl_days=args.ttl_days,
            jaccard=args.jaccard, max_proposals=args.max_proposals,
            now_ts=datetime.now().timestamp())
    if args.gc:
        report["gc"] = run_gc(
            cfg, restore_days=args.restore_days, purge_after_days=args.purge_after_days,
            apply=apply_changes, now_ts=datetime.now().timestamp())

    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=1), encoding="utf-8")
        log.info("Report -> %s", args.report)

    if args.slack:
        deliver_report(compose_report(report))

    if report.get("marked", {}).get("escalated"):
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
