#!/usr/bin/env python3
"""Purge already-ingested personal / highly-confidential DASHBOARD-STORE docs
from the KB (dashboard read layer, 2026-07-11).

Pairs with the ingest-time exclusion (src/cora/kb_exclusions.py):
  * drive_sweep skips the excluded folders at enumeration (KB_EXCLUDED_FOLDER_IDS).
  * store.upsert_documents Step-0 drops any path-bearing doc (static_md source_id /
    drive_asset metadata.path) under an excluded store.
The exclusion only stops FUTURE ingestion; THIS removes what is already there.

The excluded stores (their .md/.xlsx were swept into the KB and are retrievable
in channel Q&A right now):
  * 02-F3-Energy/projects/capital-raise  (HIGHLY CONFIDENTIAL deal docs)
  * 00-Founder/insurance/oneamerica      (PERSONAL insurance tracker)
  * 00-Founder/travel-points             (PERSONAL)

Targeting (union):
  PATH   -- chunk whose source_id (static_md) OR metadata.path (drive_asset) sits
            under an excluded store, via is_dashboard_store_path.
  DRIVE  -- chunk (drive_sweep/drive_asset) whose source_id (a Drive file id) is a
            file inside an excluded folder subtree, resolved live from Drive
            (skip with --no-drive if Drive is unreachable; PATH still runs).

Usage (--dry-run is read-only + safe anytime; STOP Cora before --apply -- the
delete contends with live KB writes):
    .venv\\Scripts\\python.exe scripts\\purge_dashboard_kb.py                 # dry-run report
    .venv\\Scripts\\python.exe scripts\\purge_dashboard_kb.py --no-drive      # dry-run, path-only
    .venv\\Scripts\\python.exe scripts\\purge_dashboard_kb.py --apply         # DELETE (Cora stopped)
After --apply, reclaim disk: .venv\\Scripts\\python.exe scripts\\reclaim_kb_space.py

Exit codes: 0 ok, 1 fatal.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

from cora.kb_exclusions import (  # noqa: E402
    KB_EXCLUDED_FOLDER_IDS,
    is_dashboard_store_path,
)
from cora.knowledge_base import schema  # noqa: E402

KB_DB_PATH = _REPO / "data" / "cora_kb.db"
_BATCH = 500
_DRIVE_COPY_SOURCES = ("drive_sweep", "drive_asset")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("purge-dashboard-kb")


def _drive_excluded_file_ids(max_nodes: int = 5000) -> frozenset[str]:
    """Live-resolve every file id inside the excluded folder subtrees. Returns an
    empty set (with a warning) if Drive is unreachable so PATH targeting still runs."""
    try:
        from cora.connectors.drive_connector import _build_drive_service
        service = _build_drive_service()
    except Exception as exc:  # noqa: BLE001
        log.warning("Drive unreachable (%s) -- skipping DRIVE targeting; PATH still runs.", exc)
        return frozenset()

    file_ids: set[str] = set()
    folders: list[str] = list(KB_EXCLUDED_FOLDER_IDS)
    seen_folders: set[str] = set(folders)
    try:
        while folders and (len(file_ids) + len(seen_folders)) < max_nodes:
            fid = folders.pop()
            page_token = None
            while True:
                resp = service.files().list(
                    q=f"'{fid}' in parents and trashed = false",
                    fields="nextPageToken, files(id, mimeType)",
                    pageSize=100,
                    pageToken=page_token,
                ).execute()
                for f in resp.get("files", []):
                    if f.get("mimeType") == "application/vnd.google-apps.folder":
                        if f["id"] not in seen_folders:
                            seen_folders.add(f["id"])
                            folders.append(f["id"])
                    else:
                        file_ids.add(f["id"])
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
    except Exception as exc:  # noqa: BLE001
        log.warning("Drive folder walk partial (%s) -- using what was gathered.", exc)
    return frozenset(file_ids)


def _base_id(source_id: str) -> str:
    return (source_id or "").split(":", 1)[0]


def target_chunks(conn, drive_file_ids: frozenset[str]):
    """Read-only. Return (rows_to_delete, per_source_counts, sample_titles).
    rows_to_delete = list of chunk_id."""
    rows = conn.execute(
        "SELECT chunk_id, source, source_id, title, entity, metadata FROM knowledge_chunks"
    ).fetchall()
    to_delete: list[str] = []
    per_source: dict[str, int] = {}
    samples: dict[str, set[str]] = {}
    for chunk_id, source, source_id, title, entity, metadata in rows:
        source_id = source_id or ""
        hit = False
        # PATH match: static_md source_id, or drive_asset metadata.path.
        if is_dashboard_store_path(source_id):
            hit = True
        else:
            try:
                meta = json.loads(metadata) if metadata else {}
                if is_dashboard_store_path(str(meta.get("path", ""))):
                    hit = True
            except (ValueError, TypeError):
                pass
        # DRIVE match: drive-copy chunk whose file id is inside an excluded folder.
        if not hit and source in _DRIVE_COPY_SOURCES and drive_file_ids:
            if source_id in drive_file_ids or _base_id(source_id) in drive_file_ids:
                hit = True
        if hit:
            to_delete.append(chunk_id)
            key = f"{source}/{entity}"
            per_source[key] = per_source.get(key, 0) + 1
            samples.setdefault(key, set()).add(str(title or source_id))
    return to_delete, per_source, samples


def delete_chunks(conn, chunk_ids) -> dict:
    totals = {"knowledge_vec_bin": 0, "knowledge_vec_f32": 0, "knowledge_chunks": 0}
    ids = list(chunk_ids)
    for i in range(0, len(ids), _BATCH):
        batch = ids[i : i + _BATCH]
        ph = ",".join("?" * len(batch))
        for tbl in ("knowledge_vec_bin", "knowledge_vec_f32", "knowledge_chunks"):
            cur = conn.execute(f"DELETE FROM {tbl} WHERE chunk_id IN ({ph})", batch)
            totals[tbl] += cur.rowcount
    conn.commit()
    return totals


def main() -> int:
    ap = argparse.ArgumentParser(description="Purge dashboard-store docs from the KB.")
    ap.add_argument("--apply", action="store_true", help="Delete (default is a read-only dry-run).")
    ap.add_argument("--dry-run", action="store_true", help="Report only (default).")
    ap.add_argument("--db", default=str(KB_DB_PATH), help="Path to the KB sqlite DB.")
    ap.add_argument("--no-drive", action="store_true",
                    help="Skip the live Drive folder walk (PATH targeting only).")
    args = ap.parse_args()
    apply_changes = args.apply and not args.dry_run

    db_path = Path(args.db)
    if not db_path.exists():
        log.error("KB database not found: %s", db_path)
        return 1

    drive_file_ids = frozenset() if args.no_drive else _drive_excluded_file_ids()
    log.info("DRIVE targeting: %d file ids resolved from excluded folders%s",
             len(drive_file_ids), " (skipped: --no-drive)" if args.no_drive else "")

    conn = schema.connect(db_path)
    try:
        to_delete, per_source, samples = target_chunks(conn, drive_file_ids)
        log.info("=== Dashboard-store purge scope ===")
        for key in sorted(per_source):
            log.info("  %-24s %5d chunks", key, per_source[key])
            for t in sorted(samples[key])[:12]:
                log.info("        %s", t)
        log.info("  TOTAL chunks that --apply would delete: %d", len(to_delete))

        try:
            manifest = _REPO / "logs" / "purge-dashboard-kb.txt"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            with manifest.open("w", encoding="utf-8") as fh:
                fh.write("# Dashboard-store KB purge manifest\n")
                for key in sorted(samples):
                    fh.write(f"# {key} ({per_source[key]} chunks)\n")
                    for t in sorted(samples[key]):
                        fh.write(f"  {t}\n")
            log.info("  Full manifest -> %s", manifest)
        except Exception as exc:  # noqa: BLE001
            log.warning("  could not write manifest: %s", exc)

        if not apply_changes:
            log.info("Dry-run -- nothing deleted. Re-run with --apply (Cora STOPPED), "
                     "then reclaim_kb_space.py.")
            return 0
        if not to_delete:
            log.info("Nothing to delete.")
            return 0
        log.info("Deleting %d chunks...", len(to_delete))
        totals = delete_chunks(conn, to_delete)
        log.info("Deleted: %s", totals)
        log.info("Reclaim disk with: .venv\\Scripts\\python.exe scripts\\reclaim_kb_space.py")
    except Exception as exc:  # noqa: BLE001
        log.error("purge failed: %s", exc, exc_info=True)
        conn.close()
        return 1
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
