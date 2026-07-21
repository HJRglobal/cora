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
    ap.add_argument("--db", default=str(KB_DB_PATH), help="Path to the KB sqlite DB.")
    ap.add_argument("--revert", metavar="MANIFEST", help="Reverse the moves recorded in a manifest JSON.")
    ap.add_argument("--report", metavar="PATH", help="Write the candidate/run report JSON to PATH.")
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
        log.info("--proactive detectors are not yet implemented (Slice C).")
    if args.gc:
        log.info("--gc retention is not yet implemented (Slice D).")

    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=1), encoding="utf-8")
        log.info("Report -> %s", args.report)

    if report.get("marked", {}).get("escalated"):
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
