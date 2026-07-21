#!/usr/bin/env python3
r"""Reusable Founder-OS KB archive-move + purge core.

Extracted (2026-07-21, D-086 -> KB-staleness LOOP) from the one-time
``scripts/archive_founder_os_kb.py`` so BOTH that tool AND the recurring
``scripts/kb_hygiene_sweep.py`` share one audited move/purge/manifest engine.
The one-time tool keeps its disposition-list clusters + CLI/orchestration and
imports this module; behavior is preserved byte-for-byte (its test suite pins it).

Everything here is parameterized by an ``ArchiveConfig`` -- this module holds NO
module-level Founder-OS root or guard constants, so a second caller (the hygiene
sweep) can pass its own roots/guards. The SQL (exact-IN + GLOB, never LIKE) and
the 3-table delete cascade (knowledge_vec_bin, knowledge_vec_f32,
knowledge_chunks) are the security/behavior invariants and are unchanged.

Not bot-loaded: only the two scripts import this. No app.py dependency -> no
restart needed to activate a change here.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# is_copa_bhrf_path is the LEX-NDA hard-guard predicate (kb_exclusions, d17d1d9).
from cora.kb_exclusions import is_copa_bhrf_path
from cora.knowledge_base import schema

log = logging.getLogger("cora.kb_archive")

_DEFAULT_BATCH = 500


class HoldGuardTripped(RuntimeError):
    """Raised when a held/sensitive path enters a computed archive set.

    The caller (CLI) translates this to exit code 2. Carries the offending
    cluster id, relpath, and reason for logging/diagnostics.
    """

    def __init__(self, cluster_id: str, relpath: str, reason: str) -> None:
        self.cluster_id = cluster_id
        self.relpath = relpath
        self.reason = reason
        super().__init__(f"HOLD-GUARD TRIPPED in cluster {cluster_id}: {relpath} -> {reason}")


@dataclass(frozen=True)
class ArchiveConfig:
    """Roots + guard sets for one caller. Pure data; no I/O.

    A caller builds this from its own constants so the core carries no
    Founder-OS-specific globals. Copa fields default to "disabled" so the
    recurring hygiene sweep never re-runs the one-time copa whole-folder purge.
    """

    founder_os_root: Path
    archive_root: Path
    hold_segments: frozenset[str] = frozenset()
    keep_class_basenames: frozenset[str] = frozenset()
    keep_class_segments: frozenset[str] = frozenset()
    keep_class_basename_substr: tuple[str, ...] = ()
    class_exceptions: frozenset[str] = frozenset()
    keep_substrings: tuple[str, ...] = ()
    scaffold_basenames: frozenset[str] = frozenset()
    drive_title_max_fileids: int = 2
    # copa_purge_glob=None -> no whole-folder copa static purge (hygiene sweep).
    copa_purge_glob: str | None = None
    copa_drive_titles: tuple[str, ...] = ()
    # copa_loose_dup=None -> ANY copa-bhrf path aborts (hygiene sweep never
    # touches copa). The one-time tool sets it to the single sanctioned dup path.
    copa_loose_dup: str | None = None
    batch: int = _DEFAULT_BATCH
    # Optional extra confidential-store predicate hook (union, fail-closed). The
    # hygiene sweep passes kb_exclusions.is_dashboard_store_path so oneamerica /
    # capital-raise / travel-points abort even without a literal hold_segment.
    extra_hold_predicates: tuple = field(default=())


# ── path predicates (pure) ────────────────────────────────────────────────────
def rel(path: Path, cfg: ArchiveConfig) -> str:
    """Backslash relpath from the config root (the KB source_id key + move key)."""
    return str(path.relative_to(cfg.founder_os_root))


def segments_lower(relpath: str) -> list[str]:
    return [p.lower() for p in relpath.replace("/", "\\").split("\\") if p]


def is_keep_as_class(relpath: str, cfg: ArchiveConfig) -> str | None:
    """Return a reason string if a candidate is KEEP-as-class, else None."""
    segs = segments_lower(relpath)
    base = segs[-1] if segs else ""
    if base in cfg.keep_class_basenames:
        return f"keep-as-class basename:{base}"
    if any(s in cfg.keep_class_segments for s in segs[:-1]):
        hit = next(s for s in segs[:-1] if s in cfg.keep_class_segments)
        return f"keep-as-class segment:{hit}"
    if any(sub in base for sub in cfg.keep_class_basename_substr):
        hit = next(sub for sub in cfg.keep_class_basename_substr if sub in base)
        return f"keep-as-class basename-substr:{hit}"
    return None


def hold_reason(relpath: str, cfg: ArchiveConfig) -> str | None:
    """Return an abort reason if a relpath is HARD-HELD/sensitive, else None.

    copa-bhrf: only ``cfg.copa_loose_dup`` (if set) is permitted; every other
    copa path aborts. Any ``cfg.extra_hold_predicates`` that returns True also
    aborts (union, fail-closed) -- used by the hygiene sweep to fold in the
    kb_exclusions confidential-store predicates.
    """
    segs = set(segments_lower(relpath))
    hit = segs & cfg.hold_segments
    if hit:
        return f"HOLD segment {sorted(hit)}"
    if is_copa_bhrf_path(relpath) and relpath != cfg.copa_loose_dup:
        return "copa-bhrf (only the sanctioned loose duplicate may be archived)"
    for pred in cfg.extra_hold_predicates:
        try:
            if pred(relpath):
                name = getattr(pred, "__name__", repr(pred))
                return f"confidential-store predicate:{name}"
        except Exception:  # noqa: BLE001 -- a broken predicate must fail CLOSED
            return "confidential-store predicate raised (fail-closed)"
    return None


def is_keep_substr(relpath: str, cfg: ArchiveConfig) -> str | None:
    low = relpath.lower()
    for sub in cfg.keep_substrings:
        if sub in low:
            return f"keep-substring:{sub}"
    return None


# ── manifest build (expand clusters -> archive set) ───────────────────────────
def build_move_manifest(
    clusters: list[dict], cfg: ArchiveConfig
) -> tuple[list[str], dict, list[tuple[str, str]], list[tuple[str, str]]]:
    """Expand clusters -> (sorted unique archive relpaths, per-cluster report,
    keep_as_class_filtered, keep_substr_filtered).

    Raises HoldGuardTripped if any HOLD path enters the set. Glob candidates AND
    explicit entries pass the KEEP-as-class + substring-KEEP filters; only
    ``cfg.class_exceptions`` paths bypass KEEP-as-class.
    """
    archive: dict[str, str] = {}          # relpath -> cluster_id (first wins; dedup)
    report: dict[str, dict] = {}
    class_filtered: list[tuple[str, str]] = []
    substr_filtered: list[tuple[str, str]] = []

    for cl in clusters:
        cid = cl["id"]
        keep_set = {k.replace("/", "\\") for k in cl.get("keep", [])}
        matched: list[str] = []

        # Globs -> candidates (KEEP filters apply).
        for pattern in cl.get("globs", []):
            pat = pattern.replace("\\", "/")   # pathlib glob wants forward slashes
            for p in cfg.founder_os_root.glob(pat):
                if not p.is_file():
                    continue
                r = rel(p, cfg)
                if r in keep_set:
                    continue
                sub = is_keep_substr(r, cfg)
                if sub:
                    substr_filtered.append((r, f"{cid}:{sub}"))
                    continue
                cls = is_keep_as_class(r, cfg)
                if cls:
                    class_filtered.append((r, f"{cid}:{cls}"))
                    continue
                matched.append(r)

        # Explicit entries: hand-verified but STILL pass substring-KEEP AND
        # KEEP-as-class (unless allowlisted in class_exceptions) -- defense in
        # depth so no class/held file slips through an explicit list.
        for e in cl.get("explicit", []):
            r = e.replace("/", "\\")
            if r in keep_set:
                continue
            sub = is_keep_substr(r, cfg)
            if sub:
                substr_filtered.append((r, f"{cid}:{sub}(explicit-skipped)"))
                continue
            if r not in cfg.class_exceptions:
                cls = is_keep_as_class(r, cfg)
                if cls:
                    class_filtered.append((r, f"{cid}:{cls}(explicit-blocked)"))
                    continue
            src = cfg.founder_os_root / r
            if not src.exists():
                log.warning("  [%s] explicit path not found on disk: %s", cid, r)
            matched.append(r)

        # HARD HOLD guard -- abort on any held/sensitive path.
        for r in matched:
            hr = hold_reason(r, cfg)
            if hr:
                log.error("HOLD-GUARD TRIPPED in cluster %s: %s -> %s", cid, r, hr)
                raise HoldGuardTripped(cid, r, hr)

        uniq = sorted(set(matched))
        report[cid] = {
            "section": cl.get("section", ""),
            "expected": cl.get("expected", ""),
            "count": len(uniq),
            "purge": cl.get("purge", True),
            "files": uniq,
        }
        for r in uniq:
            archive.setdefault(r, cid)

    return sorted(archive), report, class_filtered, substr_filtered


# ── KB purge selection (read-only SELECTs; ro or rw connection) ───────────────
def chunk_ids_for_static(conn, relpaths: list[str], cfg: ArchiveConfig) -> list[str]:
    ids: list[str] = []
    for i in range(0, len(relpaths), cfg.batch):
        batch = relpaths[i : i + cfg.batch]
        ph = ",".join("?" * len(batch))
        rows = conn.execute(
            f"SELECT chunk_id FROM knowledge_chunks WHERE source='static_md' AND source_id IN ({ph})",
            batch,
        ).fetchall()
        ids.extend(r[0] for r in rows)
    return ids


def select_static_purge(
    conn, archive_relpaths: list[str], cfg: ArchiveConfig
) -> tuple[list[str], int, int]:
    """chunk_ids to purge: static_md rows whose source_id is a moved archive path,
    PLUS (if cfg.copa_purge_glob is set) ALL copa-bhrf static_md rows (whole
    folder). Returns (chunk_ids, moved_static_count, copa_static_count).
    Exact-IN on the backslash relpath + GLOB for the folder -- NEVER LIKE."""
    moved_ids = set(chunk_ids_for_static(conn, archive_relpaths, cfg))
    copa_ids: set[str] = set()
    if cfg.copa_purge_glob:
        copa_rows = conn.execute(
            "SELECT chunk_id FROM knowledge_chunks WHERE source='static_md' AND source_id GLOB ?",
            (cfg.copa_purge_glob,),
        ).fetchall()
        copa_ids = {r[0] for r in copa_rows}
    all_ids = moved_ids | copa_ids
    return sorted(all_ids), len(moved_ids), len(copa_ids)


def select_drive_purge(
    conn, archive_relpaths: list[str], cfg: ArchiveConfig
) -> tuple[list[str], list[dict], list[dict]]:
    """Drive-copy (drive_sweep/drive_asset) purge, SELF-GUARDED by file-id count.
    Candidate titles = basenames of moved files + cfg.copa_drive_titles. A title
    is purged only when it maps to <= cfg.drive_title_max_fileids distinct
    file-ids AND is not a generic scaffolding basename. Returns
    (chunk_ids, included[{title,chunks,file_ids,sources}], skipped[...]).

    drive_sweep chunks carry a bare Drive file-id source_id and NO path, so
    title (basename) is the only usable key -- hence the self-guard against
    portfolio-wide basename collisions."""
    candidates: set[str] = set()
    for r in archive_relpaths:
        candidates.add(r.replace("/", "\\").split("\\")[-1])
    candidates.update(cfg.copa_drive_titles)

    skipped: list[dict] = []
    scaffold = sorted(t for t in candidates if t.lower() in cfg.scaffold_basenames)
    for t in scaffold:
        skipped.append({"title": t, "reason": "scaffolding-denylist"})
    query_titles = sorted(t for t in candidates if t.lower() not in cfg.scaffold_basenames)

    title_ids: dict[str, list[str]] = {}
    title_src: dict[str, dict[str, dict]] = {}
    for i in range(0, len(query_titles), cfg.batch):
        batch = query_titles[i : i + cfg.batch]
        ph = ",".join("?" * len(batch))
        rows = conn.execute(
            f"SELECT chunk_id, source_id, title, entity FROM knowledge_chunks "
            f"WHERE source IN ('drive_sweep','drive_asset') AND title IN ({ph})",
            batch,
        ).fetchall()
        for chunk_id, source_id, title, entity in rows:
            title_ids.setdefault(title, []).append(chunk_id)
            s = title_src.setdefault(title, {}).setdefault(source_id, {"chunks": 0, "entities": set()})
            s["chunks"] += 1
            s["entities"].add(str(entity or ""))

    included: list[dict] = []
    ids: list[str] = []
    for title in query_titles:
        if title not in title_ids:
            continue  # no drive copy exists for this filename
        srcs = title_src[title]
        if len(srcs) > cfg.drive_title_max_fileids:
            skipped.append({"title": title, "reason": f"ambiguous ({len(srcs)} file-ids)",
                            "chunks": len(title_ids[title])})
            continue
        included.append({
            "title": title,
            "chunks": len(title_ids[title]),
            "file_ids": len(srcs),
            "sources": [
                {"file_id": sid, "chunks": v["chunks"], "entities": sorted(v["entities"])}
                for sid, v in sorted(srcs.items())
            ],
        })
        ids.extend(title_ids[title])
    return ids, included, skipped


def delete_chunks(conn, chunk_ids: list[str], cfg: ArchiveConfig | None = None) -> dict:
    """Batched delete from all 3 tables (manual application-level cascade; there
    is NO SQL FK). rw connection only. Commits once at the end."""
    batch_size = cfg.batch if cfg is not None else _DEFAULT_BATCH
    totals = {"knowledge_vec_bin": 0, "knowledge_vec_f32": 0, "knowledge_chunks": 0}
    for i in range(0, len(chunk_ids), batch_size):
        batch = chunk_ids[i : i + batch_size]
        ph = ",".join("?" * len(batch))
        for tbl in ("knowledge_vec_bin", "knowledge_vec_f32", "knowledge_chunks"):
            cur = conn.execute(f"DELETE FROM {tbl} WHERE chunk_id IN ({ph})", batch)
            totals[tbl] += cur.rowcount
    conn.commit()
    return totals


# ── move phase ────────────────────────────────────────────────────────────────
def plan_moves(archive_relpaths: list[str], cfg: ArchiveConfig) -> list[dict]:
    moves = []
    for r in archive_relpaths:
        src = cfg.founder_os_root / r
        dst = cfg.archive_root / r
        moves.append({"src_rel": r, "dst_rel": str(Path("_archive") / r),
                      "src_exists": src.exists(), "dst_exists": dst.exists()})
    return moves


def execute_moves(moves: list[dict], cfg: ArchiveConfig) -> None:
    for m in moves:
        src = cfg.founder_os_root / m["src_rel"]
        dst = cfg.archive_root / m["src_rel"]
        if dst.exists() and not src.exists():
            m["moved"], m["result"] = False, "already-archived"
            continue
        if dst.exists() and src.exists():
            m["moved"], m["result"] = False, "CONFLICT: both src and dst exist -- skipped"
            log.warning("  CONFLICT (skipped): %s", m["src_rel"])
            continue
        if not src.exists():
            m["moved"], m["result"] = False, "src-missing"
            log.warning("  src missing (skipped): %s", m["src_rel"])
            continue
        # Per-file soft failure: a Windows lock / permission error on ONE file
        # (e.g. Drive sync holding it) must not strand the rest of the batch.
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            m["moved"], m["result"] = True, "moved"
        except OSError as exc:
            m["moved"], m["result"] = False, f"error: {exc}"
            log.error("  move FAILED (skipped, retryable): %s -> %s", m["src_rel"], exc)


def revert(manifest_path: Path, cfg: ArchiveConfig) -> int:
    """Move each moved==True file back from _archive to the live tree. Files only
    -- purged KB chunks are NOT restored here (re-ingest / restore db backup)."""
    data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    moves = data.get("moves", [])
    restored = 0
    for m in moves:
        if not m.get("moved"):
            continue
        dst = cfg.archive_root / m["src_rel"]       # where it now lives
        src = cfg.founder_os_root / m["src_rel"]    # where it came from
        if not dst.exists():
            log.warning("  revert: archived file missing: %s", m["src_rel"])
            continue
        if src.exists():
            log.warning("  revert: original path occupied, skipped: %s", m["src_rel"])
            continue
        src.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(dst), str(src))
        restored += 1
    log.info("Reverted %d file(s) back from _archive.", restored)
    log.warning("NOTE: --revert restores FILES only. Purged KB chunks are NOT restored "
                "here -- re-ingest (incremental_sync_static) or restore cora_kb.db from "
                "the pre-apply backup.")
    return 0


# ── manifest write ─────────────────────────────────────────────────────────────
def write_manifest(path: Path, cfg: ArchiveConfig, *, mode: str, report: dict,
                   moves: list[dict], class_filtered, substr_filtered, static_ids,
                   drive_ids, moved_static, copa_static, drive_included,
                   drive_skipped, purge_enabled) -> None:
    """Write the JSON manifest (source of truth + reversibility record) + a
    human-readable companion. The JSON PERSISTS the resolved purge chunk_ids so a
    resumed / purge-only run reads them instead of re-globbing a moved tree.
    Emits a top-level ``archived_date`` (YYYY-MM-DD) for GC-retention aging.
    Callers write this BEFORE any mutation, then again after moves complete."""
    total = len(moves)
    static_ids = list(static_ids)
    drive_ids = list(drive_ids)
    now = datetime.now()
    payload = {
        "generated_at": now.isoformat(timespec="seconds"),
        "archived_date": now.strftime("%Y-%m-%d"),
        "mode": mode,
        "founder_os_root": str(cfg.founder_os_root),
        "archive_root": str(cfg.archive_root),
        "total_files": total,
        "clusters": {cid: {k: v for k, v in r.items() if k != "files"} for cid, r in report.items()},
        "purge": {
            "enabled": purge_enabled,
            "static_md_chunks_total": len(static_ids),
            "static_md_from_moved_files": moved_static,
            "copa_bhrf_static_chunks": copa_static,
            "drive_copy_chunks_total": len(drive_ids),
            "drive_copy_included": drive_included,
            "drive_copy_skipped_ambiguous": drive_skipped,
            "static_chunk_ids": static_ids,
            "drive_chunk_ids": drive_ids,
        },
        "keep_as_class_filtered": [{"path": p, "why": w} for p, w in class_filtered],
        "keep_substring_filtered": [{"path": p, "why": w} for p, w in substr_filtered],
        "moves": moves,
    }
    Path(path).write_text(json.dumps(payload, indent=1), encoding="utf-8")
    log.info("Full manifest (JSON, reversible; persists purge chunk_ids) -> %s", path)

    txt = Path(path).with_suffix(".txt")
    with txt.open("w", encoding="utf-8") as fh:
        fh.write(f"Founder-OS KB archive+purge manifest  mode={mode}  {payload['generated_at']}\n")
        fh.write(f"TOTAL files to archive: {total}\n\n")
        fh.write("== Per-cluster ==\n")
        for cid, r in report.items():
            fh.write(f"  [{cid}] {r['count']:>4d} files  (expected {r['expected']})  purge={r['purge']}\n")
            fh.write(f"        {r['section']}\n")
        fh.write("\n== KB purge ==\n")
        fh.write(f"  static_md chunks: {len(static_ids)} (from moved files {moved_static} + copa-bhrf folder {copa_static})\n")
        fh.write(f"  drive-copy chunks: {len(drive_ids)}\n")
        fh.write(f"  drive-copy titles INCLUDED ({len(drive_included)}) -- review each file-id:\n")
        for d in drive_included:
            fh.write(f"      {d['title']}  ({d['chunks']} chunks / {d['file_ids']} file-id)\n")
            for s in d.get("sources", []):
                ents = ",".join(s.get("entities", [])) or "?"
                fh.write(f"          - file_id={s['file_id']}  ({s['chunks']} chunks, entity={ents})\n")
        fh.write(f"  drive-copy titles SKIPPED-ambiguous ({len(drive_skipped)}):\n")
        for d in drive_skipped:
            fh.write(f"      {d['title']}  [{d['reason']}]\n")
        fh.write(f"\n== KEEP-as-class filtered ({len(class_filtered)}) ==\n")
        for p, w in class_filtered:
            fh.write(f"      {p}  [{w}]\n")
        fh.write(f"\n== KEEP-substring filtered ({len(substr_filtered)}) ==\n")
        for p, w in substr_filtered:
            fh.write(f"      {p}  [{w}]\n")
        fh.write("\n== Moves (src -> _archive) ==\n")
        for m in moves:
            fh.write(f"      {m['src_rel']}\n")
    log.info("Human-readable manifest -> %s", txt)


# ── shared connection helpers ──────────────────────────────────────────────────
def connect_ro(db_path) -> sqlite3.Connection:
    """Read-only connection (safe while Cora is live). For purge SELECTs."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30.0)
    conn.execute("PRAGMA query_only=ON")
    return conn


def connect_rw(db_path) -> sqlite3.Connection:
    """Read-write connection via schema.connect (loads sqlite-vec, WAL,
    busy_timeout). REQUIRED for delete_chunks -- the vec0 virtual table
    knowledge_vec_bin needs the extension loaded to DELETE from."""
    return schema.connect(db_path)
