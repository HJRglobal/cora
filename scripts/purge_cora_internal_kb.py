#!/usr/bin/env python3
"""Purge already-ingested Cora build/audit/forensic docs from the KB (WS1).

Pairs with the ingest-time exclusion (src/cora/kb_exclusions.py, wired into
incremental_sync_static.py): the exclusion stops FUTURE ingestion of Cora's own
build/audit/forensic/code-prompt docs under ``_shared/projects/cora/``; this
removes what is already in the KB. Those chunks are why a "diagnose yourself"
query could RAG-narrate Cora's own audit notes (a fabricated "diagnostic").

Selectors (unioned; a chunk matching several is deleted once):
  STATIC_MD  -- chunks with source='static_md' whose source_id is cora-internal
                (folder ``_shared/projects/cora/`` or a cora-build filename),
                decided by the SAME predicate the ingest path uses.
  DRIVE-COPY -- drive_sweep/drive_asset chunks whose stored TITLE (the Drive
                filename) is a cora-build doc (``--scope targeted|broad``).
  FOLDER     -- (2026-09-03, ``--folder-id <id>``, repeatable) drive_sweep/
                drive_asset chunks whose source_id is a descendant FILE id of
                that Drive folder, enumerated read-only by the same BFS
                sweep_founders_os uses. This is the door the title heuristic
                cannot be: the 9/3 cowork-side Drive measurement counted 157 of
                the 550 .md files under _shared/projects/cora with no ``cora``
                token at all, so no ``--scope`` can reach them.
  NOTES      -- (opt-in, --include-notes) user_note chunks whose content matches
                a suspicious-fabrication pattern (default: minute press /
                diagnostic / self-diagnos). Reported for Harrison to eyeball;
                deleted only with --apply --include-notes.

Usage (--dry-run is read-only + safe anytime, even with Cora live; STOP Cora
before --apply because the delete contends with live writes):
    .venv\\Scripts\\python.exe scripts\\purge_cora_internal_kb.py                 # dry-run report
    .venv\\Scripts\\python.exe scripts\\purge_cora_internal_kb.py --apply         # delete static_md cora docs
    .venv\\Scripts\\python.exe scripts\\purge_cora_internal_kb.py --apply --include-notes
    .venv\\Scripts\\python.exe scripts\\purge_cora_internal_kb.py --db <path>      # target a specific DB
    .venv\\Scripts\\python.exe scripts\\purge_cora_internal_kb.py --scope broad --folder-id <drive-folder-id>
After --apply, reclaim disk: .venv\\Scripts\\python.exe scripts\\reclaim_kb_space.py

Manifests (every selected file, for the human eyeball BEFORE any --apply):
    logs/purge-cora-internal-<scope>.txt          static_md + drive-copy (title) selections
    logs/purge-cora-internal-folder-<id>.txt      one per --folder-id: name, file id, chunks
                                                  (the REVIEWED set; written by the dry-run)
    logs/purge-cora-internal-selected-<utc>.json  every selected chunk row (all passes), written BEFORE the delete
    logs/purge-cora-internal-folder-<id>.applied-<utc>.txt   per-folder record written AFTER the delete (+ totals footer)
    logs/purge-cora-internal-applied-<utc>.json   selected rows file + the deleted-per-table totals, AFTER the delete

--apply gates for folder mode (D-086 -- over-deletion is the cardinal sin):
    * ``--expect-leaf <name>`` is REQUIRED and must equal the resolved folder's own
      name (case-insensitive) -- a pasted sibling id (``_shared``, ``projects``, an
      entity folder) can no longer purge a whole partition;
    * a folder whose resolved chain is root-level or top-level (chain length <= 2)
      is refused in every mode;
    * the dry-run manifest for that id must exist and every file about to be
      deleted must be IN it; a file that appeared since the eyeball is refused
      unless ``--accept-delta`` is passed;
    * ONE --folder-id per --apply (the leaf gate and the reviewed manifest are per
      folder; a dry-run may take several);
    * the pre-delete JSON dump of every selected row must write successfully, else
      nothing is deleted; the per-folder ``.applied-<utc>.txt`` records and the
      applied JSON are written AFTER the delete, from what actually happened, so
      an "applied" artifact can never exist for a run that deleted nothing.
    The reviewed manifest is a REVIEW RECORD, not a selection editor: deleting a
    row from it does not spare that file -- the file becomes an unreviewed delta
    (refused), and ``--accept-delta`` would then delete it.

Exit codes: 0 ok, 1 fatal.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from dotenv import load_dotenv

load_dotenv()

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

from cora.kb_exclusions import (  # noqa: E402
    _is_kb_allowlisted,
    is_cora_internal_source_id,
    is_cora_internal_title,
)
from cora.knowledge_base import schema  # noqa: E402

KB_DB_PATH = _REPO / "data" / "cora_kb.db"
_BATCH = 500

# drive_sweep + drive_asset copy Founder-OS Drive files into the KB under a
# Drive-FILE-ID source_id with the filename in `title` -- the path-based
# source_id rule can't see them, so we match the stored title here. (This is the
# dominant leak vector that the static_md-only purge missed; see WS1-DRIVE.)
_DRIVE_COPY_SOURCES = ("drive_sweep", "drive_asset")

# ── Folder-ancestry mode (2026-09-03, cq-11e9abda254a "D-057 IS LEAKING") ────
# The title selector is a FILENAME heuristic: it needs a ``cora[-_]`` token and a
# build keyword. The 9/3 cowork-side Drive measurement counted 157 of the 550
# .md files under _shared/projects/cora with no ``cora`` token at all (the
# ``_fndr_`` vs ``_cora_`` naming split), so ``--scope broad`` structurally
# cannot reach the leak class that finding sized. Folder ancestry is the
# selector that can: ``--folder-id <id>``
# enumerates EVERY descendant file id of a Drive folder -- the same
# BFS-by-``'<id>' in parents`` enumeration sweep_founders_os uses, read-only --
# and selects the drive_sweep/drive_asset chunks whose source_id IS one of
# those file ids. A Drive file id is stable across renames and moves, so a doc
# later moved into an ``_archive/`` subfolder is still found; this BFS therefore
# deliberately does NOT apply the sweep's _FOUNDERS_OS_SKIP_FOLDERS name skips.
#
# The folder selector deliberately does NOT honour _KB_ALLOWLIST_BASENAMES (the
# title/path selectors do): once the parent folder is id-pinned, drive_sweep
# never re-ingests ANYTHING under it, so sparing the drive_sweep twin of
# code-session-backlog.md would leave a STALE duplicate that never refreshes
# while its static_md copy (path-keyed, allowlist-honoured) keeps ingesting
# nightly. Deleting the twin is the post-pin steady state. Allowlisted names are
# FLAGGED in the log and the manifest header so the eyeball sees the choice.
#
# Guards -- over-deletion is the cardinal sin:
#   * the id is resolved to its full name chain FIRST (and refused unless it is
#     a folder) and the chain is logged + written into the manifest header, so
#     the eyeball sees WHICH folder is about to be purged, not just an opaque id;
#   * the Founder-OS ROOT id is refused outright (it would select the whole KB
#     Drive corpus);
#   * an enumeration that did not complete (an API error mid-walk, or the
#     folder cap) is a loud WARN on the dry-run and a hard REFUSAL on --apply,
#     so a partial set can never masquerade as the whole leak having been closed;
#   * (D-051 2026-09-03, purge lens MED-1/MED-2) --apply additionally requires
#     --expect-leaf to name the folder, refuses root/top-level chains, refuses
#     any file not present in the reviewed dry-run manifest (--accept-delta to
#     override), and refuses to delete when its own record cannot be written.
_MIN_CHAIN_DEPTH = 3          # chain [folder, ..., root] shorter than this = root/top-level = too broad
                              # (depth 3 admits _shared/projects with --expect-leaf projects -- a
                              # conscious floor; the resolved chain is in every log line + manifest)
_FOUNDERS_OS_ROOT_ID = "1TfxuKxzXz0-NipAFYqbK5AxowAy-LIPG"  # == drive_sweep.FOUNDERS_OS_ROOT_ID (pinned by test)
_GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"
_FOLDER_ENUM_MAX_FOLDERS = 5000


def _drive_service() -> Any:
    """Direct-SA Drive v3 service (the SA is a Viewer on HJR-Founder-OS) -- the
    same builder sweep_founders_os uses. Raises with a clear message when the SA
    path is not configured; folder mode never proceeds on a guessed credential."""
    sa = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not sa or not Path(sa).exists():
        raise RuntimeError(
            "GOOGLE_SERVICE_ACCOUNT_JSON not set or file not found -- "
            "--folder-id needs the Cora service account (read-only Drive)"
        )
    from cora.connectors import drive_sweep as ds  # local import: network deps only when used
    return ds._build_sa_drive_service_direct(sa)


def resolve_folder_chain(service: Any, folder_id: str, *, max_depth: int = 25) -> list[tuple[str, str]]:
    """Read-only. Walk a folder's parents upward.

    Returns ``[(name, id), ...]`` from the folder itself up to its topmost
    ancestor. Raises if the id does not resolve to a FOLDER -- a file id or a
    typo must never be walked as a subtree."""
    from cora.connectors import drive_sweep as ds
    chain: list[tuple[str, str]] = []
    fid = folder_id
    for _ in range(max_depth):
        meta = ds._retry_execute(
            service.files().get(fileId=fid, fields="id,name,mimeType,parents")
        )
        if not chain and meta.get("mimeType") != _GOOGLE_FOLDER_MIME:
            raise RuntimeError(
                f"{folder_id} is not a Drive folder (mimeType={meta.get('mimeType')!r})"
            )
        chain.append((str(meta.get("name") or ""), str(meta.get("id") or fid)))
        parents = meta.get("parents") or []
        if not parents:
            break
        fid = parents[0]
    return chain


def format_chain(chain: list[tuple[str, str]]) -> str:
    return " <- ".join(f"{name} ({fid})" for name, fid in chain)


def enumerate_folder_files(
    service: Any, folder_id: str, *, max_folders: int = _FOLDER_ENUM_MAX_FOLDERS
) -> tuple[dict[str, str], bool]:
    """Read-only BFS over a Drive folder subtree -> ``({file_id: name}, complete)``.

    Mirrors the sweep_founders_os enumeration SHAPE (BFS by ``'<folder>' in
    parents and trashed = false``, ``spaces=drive``, ``_retry_execute``, the same
    SA): subfolders are enqueued as they are seen, so a pinned/purged PARENT
    covers its whole subtree. The sweep issues two listings per folder (files,
    then subfolders at pageSize 100); this issues ONE combined listing at
    pageSize 1000. Every non-folder MIME is included (the purge must see whatever ANY
    connector may have ingested, not only the sweep's allow-list) and no folder
    NAME is skipped (see the block comment above). ``complete`` is False if any
    listing failed after retry or the folder cap was hit -- the caller must treat
    the set as a FLOOR, never as the whole leak."""
    from cora.connectors import drive_sweep as ds
    files: dict[str, str] = {}
    queue: list[str] = [folder_id]
    visited: set[str] = set()
    complete = True
    while queue:
        fid = queue.pop(0)
        if fid in visited:
            continue
        if len(visited) >= max_folders:
            log.warning("folder-mode: folder cap %d hit under %s -- enumeration INCOMPLETE",
                        max_folders, folder_id)
            complete = False
            break
        visited.add(fid)
        page_token: str | None = None
        while True:
            kwargs: dict[str, Any] = dict(
                q=f"'{fid}' in parents and trashed = false",
                spaces="drive",
                fields="nextPageToken, files(id,name,mimeType)",
                pageSize=1000,
            )
            if page_token:
                kwargs["pageToken"] = page_token
            try:
                resp = ds._retry_execute(service.files().list(**kwargs))
            except Exception as exc:  # noqa: BLE001 -- fail-CLOSED: signal incomplete
                log.warning("folder-mode: listing failed under %s (%s) -- enumeration INCOMPLETE",
                            fid, exc)
                complete = False
                break
            if resp.get("incompleteSearch"):
                # Drive signals it could not search every corpus for this page.
                log.warning("folder-mode: incompleteSearch under %s -- enumeration INCOMPLETE", fid)
                complete = False
            for f in resp.get("files", []):
                child_id = str(f.get("id") or "")
                if not child_id:
                    continue
                if f.get("mimeType") == _GOOGLE_FOLDER_MIME:
                    if child_id not in visited:
                        queue.append(child_id)
                else:
                    files[child_id] = str(f.get("name") or "")
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    return files, complete


def target_folder_descendants(
    conn, file_names: Mapping[str, str]
) -> tuple[list[str], dict[str, tuple[str, int]]]:
    """Read-only. drive_sweep/drive_asset chunks whose source_id is one of the
    enumerated descendant file ids (the bare id every Drive connector writes, or a
    legacy ``<id>:chunkN`` form). Only the Drive-copy sources are scanned: a
    static_md row is path-keyed and a gmail/slack row can never carry a Drive file
    id as its source_id, so neither is touched by this pass.

    Returns ``(chunk_ids, {file_id: (name, chunk_count)})``."""
    if not file_names:
        return [], {}
    ph = ",".join("?" * len(_DRIVE_COPY_SOURCES))
    rows = conn.execute(
        f"SELECT chunk_id, source_id, title FROM knowledge_chunks WHERE source IN ({ph})",
        _DRIVE_COPY_SOURCES,
    ).fetchall()
    ids: list[str] = []
    hits: dict[str, tuple[str, int]] = {}
    for chunk_id, source_id, title in rows:
        sid = str(source_id or "")
        key = sid if sid in file_names else sid.split(":", 1)[0]
        if key not in file_names:
            continue
        ids.append(chunk_id)
        name, n = hits.get(key, (file_names[key] or str(title or ""), 0))
        hits[key] = (name, n + 1)
    return ids, hits


def _one_line(s: str) -> str:
    """A Drive name may carry CR/LF; a manifest row must stay one line so the
    reviewed set parses back exactly (a lost row would read as an unreviewed delta)."""
    return re.sub(r"[\r\n]+", " ", str(s or ""))


@dataclass
class FolderSelection:
    """What ONE --folder-id resolved to and selected (read-only facts)."""
    folder_id: str
    chain: list[tuple[str, str]]
    complete: bool
    n_descendants: int
    hits: dict[str, tuple[str, int]]
    chunk_ids: list[str]
    allowlisted: list[str] = field(default_factory=list)


def write_folder_manifest(
    path: Path, *, folder_id: str, chain: list[tuple[str, str]], complete: bool,
    n_descendants: int, hits: Mapping[str, tuple[str, int]],
    allowlisted: Iterable[str] = (), footer: str | None = None,
) -> None:
    """The full, auditable per-folder manifest: header (id, resolved chain,
    completeness, enumeration size) then one row per selected file --
    ``name <TAB> file_id <TAB> chunks`` -- and an optional ``#`` footer (the
    applied record's DELETED totals). The dry-run writes the reviewed manifest;
    the applied record is written after the delete by write_applied_records()."""
    path.parent.mkdir(parents=True, exist_ok=True)
    total_chunks = sum(n for _, n in hits.values())
    with path.open("w", encoding="utf-8") as fh:
        fh.write(f"# Cora-internal purge manifest -- FOLDER MODE  folder_id={folder_id}\n")
        fh.write(f"# resolved chain (leaf <- root): {format_chain(chain)}\n")
        fh.write(f"# enumeration complete: {complete}  (relative to the service account's Drive view; "
                 f"trashed files excluded)\n")
        fh.write(f"# descendant files enumerated: {n_descendants}\n")
        fh.write(f"# files with KB chunks: {len(hits)}   chunks: {total_chunks}\n")
        allow = sorted(allowlisted)
        fh.write("# allowlisted basenames selected (drive_sweep twin only; the static_md copy stays): "
                 + (", ".join(allow) if allow else "none") + "\n")
        fh.write("# name\tfile_id\tchunks\n")
        for fid, (name, n) in sorted(hits.items(), key=lambda kv: (kv[1][0].lower(), kv[0])):
            fh.write(f"  {_one_line(name)}\t{fid}\t{n}\n")
        if footer:
            fh.write("# " + _one_line(footer).lstrip("# ") + "\n")


def parse_manifest_file_ids(path: Path) -> set[str]:
    """The file ids recorded in a previously written folder manifest -- the set a
    human REVIEWED. Rows are ``  <name>\\t<file_id>\\t<chunks>``; parsed from the
    right so a tab inside a Drive name cannot shift the id column."""
    ids: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("  "):
            continue
        parts = line.rstrip("\n").rsplit("\t", 2)
        if len(parts) == 3 and parts[1]:
            ids.add(parts[1])
    return ids


def dump_selected_rows(conn, chunk_ids: list[str], path: Path) -> int:
    """Write every chunk row about to be deleted (chunk_id, source, source_id,
    title) as JSON -- the full record of an irreversible action, written BEFORE
    the delete. Returns the row count. Raises on any failure (the caller must then
    NOT delete)."""
    rows: list[dict[str, Any]] = []
    ids = list(chunk_ids)
    for i in range(0, len(ids), _BATCH):
        batch = ids[i : i + _BATCH]
        ph = ",".join("?" * len(batch))
        for chunk_id, source, source_id, title in conn.execute(
            f"SELECT chunk_id, source, source_id, title FROM knowledge_chunks WHERE chunk_id IN ({ph})",
            batch,
        ).fetchall():
            rows.append({"chunk_id": chunk_id, "source": source,
                         "source_id": source_id, "title": title})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"written_utc": datetime.now(timezone.utc).isoformat(),
                                "count": len(rows), "rows": rows},
                               indent=1, ensure_ascii=False), encoding="utf-8")
    return len(rows)


def run_folder_mode(
    conn, service: Any, folder_ids: list[str], logs_dir: Path, *,
    apply: bool = False, expect_leaf: str | None = None, accept_delta: bool = False,
) -> tuple[list[str], int, int, bool, list[FolderSelection]]:
    """Resolve, enumerate and select for every --folder-id (read-only).

    Returns ``(chunk_ids, files_hit, chunks_hit, all_complete, selections)`` --
    chunk ids and file counts are DEDUPED across folders. On a dry-run the
    reviewed manifest is written per folder. With ``apply=True`` NOTHING is
    written here: every gate runs first -- exactly one folder; ``expect_leaf``
    given and equal to the resolved leaf; the reviewed manifest present and
    covering every selected file (unless ``accept_delta``); the enumeration
    complete -- and any failure raises BEFORE a record could exist. The applied
    records are written by write_applied_records() AFTER the delete, from what
    actually happened. Also raises, in every mode, for the Founder-OS root, a
    non-folder, an unresolvable id and a root/top-level chain."""
    if apply and not expect_leaf:
        raise RuntimeError(
            "REFUSED: --apply with --folder-id requires --expect-leaf <folder name> "
            "(the resolved folder's own name) -- a pasted sibling id must not purge a partition."
        )
    if apply and len(folder_ids) != 1:
        raise RuntimeError(
            f"REFUSED: --apply takes exactly ONE --folder-id (got {len(folder_ids)}) -- the leaf "
            f"gate and the reviewed manifest are per folder; apply them one at a time."
        )
    selections: list[FolderSelection] = []
    for raw_id in folder_ids:
        folder_id = str(raw_id or "").strip()
        if not folder_id:
            raise RuntimeError("REFUSED: empty --folder-id")
        if folder_id == _FOUNDERS_OS_ROOT_ID:
            raise RuntimeError(
                f"REFUSED: {folder_id} is the HJR-Founder-OS ROOT -- that would select the "
                f"entire Drive corpus. Pass the specific excluded folder."
            )
        chain = resolve_folder_chain(service, folder_id)
        log.info("  FOLDER-MODE %s resolves to: %s", folder_id, format_chain(chain))
        if len(chain) < _MIN_CHAIN_DEPTH:
            raise RuntimeError(
                f"REFUSED: {folder_id} resolves to a root/top-level folder ({format_chain(chain)}) "
                f"-- too broad for a purge. Pass the specific excluded folder."
            )
        leaf = chain[0][0]
        if expect_leaf is not None and leaf.strip().casefold() != expect_leaf.strip().casefold():
            raise RuntimeError(
                f"REFUSED: --expect-leaf {expect_leaf!r} does not match the resolved folder "
                f"{leaf!r} ({format_chain(chain)})."
            )
        names, complete = enumerate_folder_files(service, folder_id)
        ids, hits = target_folder_descendants(conn, names)
        n_chunks = sum(n for _, n in hits.values())
        log.info("  FOLDER-MODE descendant files enumerated: %d (complete=%s); "
                 "with KB chunks: %d files / %d chunks", len(names), complete, len(hits), n_chunks)
        allowlisted = sorted({name for name, _n in hits.values() if _is_kb_allowlisted(name)})
        if allowlisted:
            log.info("  FOLDER-MODE note: %d allowlisted basename(s) selected -- the drive_sweep twin "
                     "only; the static_md copy (path-keyed, allowlist-honoured) stays and keeps "
                     "refreshing: %s", len(allowlisted), ", ".join(allowlisted))
        for fid, (name, n) in sorted(hits.items(), key=lambda kv: kv[1][0].lower())[:40]:
            log.info("      %s  [%s]  x%d", name, fid, n)
        if len(hits) > 40:
            log.info("      ... +%d more files (see the folder manifest)", len(hits) - 40)
        if not complete:
            if apply:
                raise RuntimeError(
                    f"REFUSED: the enumeration of {folder_id} did not complete, so the selection is "
                    f"a FLOOR, not the leak. Nothing written, nothing deleted. Re-run when Drive "
                    f"answers fully."
                )
            log.warning("  FOLDER-MODE enumeration of %s is INCOMPLETE -- the selection is a "
                        "FLOOR; --apply will refuse until a full walk succeeds", folder_id)
        reviewed_manifest = logs_dir / f"purge-cora-internal-folder-{folder_id}.txt"
        if apply:
            # MED-2: pin the delete to the set a human eyeballed. The reviewed
            # manifest is never overwritten by --apply and nothing is written here.
            if not reviewed_manifest.exists():
                raise RuntimeError(
                    f"REFUSED: no reviewed dry-run manifest at {reviewed_manifest} -- run the "
                    f"dry-run first and eyeball it (D-086)."
                )
            reviewed = parse_manifest_file_ids(reviewed_manifest)
            delta = sorted(set(hits) - reviewed, key=lambda k: hits[k][0].lower())
            if delta and not accept_delta:
                shown = ", ".join(f"{hits[k][0]} [{k}]" for k in delta[:20])
                raise RuntimeError(
                    f"REFUSED: {len(delta)} file(s) under {folder_id} were not in the reviewed "
                    f"manifest (appeared since the eyeball): {shown}"
                    f"{' ...' if len(delta) > 20 else ''}. Re-run the dry-run and eyeball again, "
                    f"or pass --accept-delta."
                )
            if delta:
                log.warning("  FOLDER-MODE --accept-delta: %d unreviewed file(s) included", len(delta))
        else:
            try:
                write_folder_manifest(reviewed_manifest, folder_id=folder_id, chain=chain,
                                      complete=complete, n_descendants=len(names), hits=hits,
                                      allowlisted=allowlisted)
                log.info("  Folder manifest written -> %s", reviewed_manifest)
            except Exception as exc:  # noqa: BLE001 -- dry-run: nothing is deleted
                log.warning("  could not write folder manifest: %s", exc)
        selections.append(FolderSelection(folder_id=folder_id, chain=chain, complete=complete,
                                          n_descendants=len(names), hits=dict(hits),
                                          chunk_ids=list(ids), allowlisted=allowlisted))
    deduped = list(dict.fromkeys(c for sel in selections for c in sel.chunk_ids))
    files_seen = {fid for sel in selections for fid in sel.hits}
    all_complete = all(sel.complete for sel in selections)
    return deduped, len(files_seen), len(deduped), all_complete, selections


def write_applied_records(
    selections: Iterable[FolderSelection], totals: Mapping[str, int], logs_dir: Path, stamp: str,
) -> list[Path]:
    """AFTER the delete: one ``purge-cora-internal-folder-<id>.applied-<stamp>.txt``
    per folder -- the reviewed-manifest shape plus a DELETED footer carrying the
    per-table totals. Returns the paths written. Raises on failure; the caller
    logs it (the delete has already happened, so nothing can be aborted -- the
    pre-delete selected-rows JSON remains the record)."""
    footer = f"DELETED {datetime.now(timezone.utc).isoformat()} totals={dict(totals)}"
    written: list[Path] = []
    for sel in selections:
        path = logs_dir / f"purge-cora-internal-folder-{sel.folder_id}.applied-{stamp}.txt"
        write_folder_manifest(path, folder_id=sel.folder_id, chain=sel.chain, complete=sel.complete,
                              n_descendants=sel.n_descendants, hits=sel.hits,
                              allowlisted=sel.allowlisted, footer=footer)
        written.append(path)
    return written

# Suspicious-fabrication phrases for the opt-in user_note sweep. These are the
# shapes of a fabricated self-"diagnostic" note (e.g. the Minute Press miss).
_DEFAULT_NOTE_PATTERN = r"minute press|self-?diagnos|diagnostic finding|finding-code|fabricat"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("purge-cora-internal-kb")


def target_static_md(conn) -> tuple[list[str], list[str]]:
    """Read-only. Return (chunk_ids, sample source_ids) for cora-internal static_md."""
    rows = conn.execute(
        "SELECT chunk_id, source_id FROM knowledge_chunks WHERE source='static_md'"
    ).fetchall()
    ids: list[str] = []
    sources: set[str] = set()
    for chunk_id, source_id in rows:
        if is_cora_internal_source_id(str(source_id or "")):
            ids.append(chunk_id)
            sources.add(str(source_id or ""))
    return ids, sorted(sources)


def target_drive_doc_copies(conn, *, broad: bool = False) -> tuple[list[str], list[str]]:
    """Read-only. Cora build/audit docs ingested as Drive copies (drive_sweep/asset).

    Matches on the stored `title` (the filename) OR the source_id, since the Drive
    copy's source_id is a file id with no path. `broad=True` widens to Cora's full
    ops/build doc set. Returns (chunk_ids, sample 'title' filenames).
    """
    ph = ",".join("?" * len(_DRIVE_COPY_SOURCES))
    rows = conn.execute(
        f"SELECT chunk_id, source_id, title FROM knowledge_chunks WHERE source IN ({ph})",
        _DRIVE_COPY_SOURCES,
    ).fetchall()
    ids: list[str] = []
    names: set[str] = set()
    for chunk_id, source_id, title in rows:
        if is_cora_internal_title(str(title or ""), broad=broad) or is_cora_internal_source_id(
            str(source_id or "")
        ):
            ids.append(chunk_id)
            names.add(str(title or source_id or ""))
    return ids, sorted(names)


def target_notes(conn, pattern: str) -> list[tuple[str, str, str]]:
    """Read-only. Return [(chunk_id, source_id, content_excerpt)] for matching notes."""
    rx = re.compile(pattern, re.IGNORECASE)
    rows = conn.execute(
        "SELECT chunk_id, source_id, content FROM knowledge_chunks WHERE source='user_note'"
    ).fetchall()
    hits: list[tuple[str, str, str]] = []
    for chunk_id, source_id, content in rows:
        text = str(content or "")
        if rx.search(text):
            excerpt = " ".join(text.split())[:160]
            hits.append((chunk_id, str(source_id or ""), excerpt))
    return hits


def delete_chunks(conn, chunk_ids) -> dict:
    """Batched delete from every vector table + knowledge_chunks (discovered via
    schema.vec_cascade_tables, incl. knowledge_vec_bin_v2). Returns rows deleted
    per table."""
    tables = schema.vec_cascade_tables(conn)
    totals = {tbl: 0 for tbl in tables}
    ids = list(chunk_ids)
    for i in range(0, len(ids), _BATCH):
        batch = ids[i : i + _BATCH]
        ph = ",".join("?" * len(batch))
        for tbl in tables:
            cur = conn.execute(f"DELETE FROM {tbl} WHERE chunk_id IN ({ph})", batch)
            totals[tbl] += cur.rowcount
    conn.commit()
    return totals


def select_for_delete(*passes: Iterable[str]) -> tuple[list[str], int]:
    """UNION of the selector passes: first-seen order, each chunk exactly once;
    plus the number of CHUNKS that more than one pass selected (a measured
    overlap, not a count of duplicate list entries)."""
    lists = [list(p) for p in passes]
    membership = Counter(c for p in lists for c in set(p))
    selected = list(dict.fromkeys(c for p in lists for c in p))
    overlap = sum(1 for c in selected if membership[c] > 1)
    return selected, overlap


def folder_apply_gate(folder_ids_given: bool, folder_complete: bool) -> str | None:
    """The --apply gate for folder mode: the refusal message when a --folder-id
    enumeration did not complete (the selection is a FLOOR, not the leak), else
    None. Kept out of main() so the destructive branch has test execution."""
    if folder_ids_given and not folder_complete:
        return ("REFUSING --apply: a --folder-id enumeration did not complete, so the folder "
                "selection is a floor, not the leak. Nothing was deleted (the static_md/title "
                "passes are held too). Re-run when Drive answers fully, or re-run without "
                "--folder-id to apply the title/static selections alone.")
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Purge Cora build/audit docs from the KB (WS1).")
    ap.add_argument("--apply", action="store_true", help="Delete (default is a read-only dry-run).")
    ap.add_argument("--dry-run", action="store_true", help="Report only (this is the default).")
    ap.add_argument("--db", default=str(KB_DB_PATH), help="Path to the KB sqlite DB.")
    ap.add_argument("--include-notes", action="store_true",
                    help="Also sweep+delete suspicious user_note chunks (opt-in).")
    ap.add_argument("--note-pattern", default=_DEFAULT_NOTE_PATTERN,
                    help="Regex (case-insensitive) for the suspicious-note sweep.")
    ap.add_argument("--scope", choices=("targeted", "broad"), default="targeted",
                    help="Drive-copy breadth. targeted (default): build/audit/forensic/"
                         "log artifacts. broad: also reviews/proposals/plans/specs/"
                         "code-session docs (still cora- + keyword; legit docs spared).")
    ap.add_argument("--folder-id", action="append", default=[], metavar="DRIVE_FOLDER_ID",
                    help="Folder-ancestry mode (repeatable): also select every drive_sweep/"
                         "drive_asset chunk whose source_id is a descendant file of this "
                         "Drive folder (read-only BFS; unioned with the title selection). "
                         "Writes logs/purge-cora-internal-folder-<id>.txt.")
    ap.add_argument("--expect-leaf", default=None, metavar="FOLDER_NAME",
                    help="Folder mode: the resolved folder's own name (e.g. cora). REQUIRED with "
                         "--apply; when given on a dry-run it is validated the same way.")
    ap.add_argument("--accept-delta", action="store_true",
                    help="Folder mode --apply: also delete files that were NOT in the reviewed "
                         "dry-run manifest (appeared since the eyeball). Default: refuse.")
    args = ap.parse_args()
    folder_id_args = [str(f).strip() for f in (args.folder_id or []) if str(f).strip()]
    apply_changes = args.apply and not args.dry_run
    broad = args.scope == "broad"

    db_path = Path(args.db)
    if not db_path.exists():
        log.error("KB database not found: %s", db_path)
        return 1

    conn = schema.connect(db_path)
    try:
        static_ids, static_sources = target_static_md(conn)
        log.info("=== Purge scope (Cora build/audit docs) [drive-copy scope=%s] ===", args.scope)
        log.info("  STATIC_MD cora-internal chunks: %d  (across %d files)",
                 len(static_ids), len(static_sources))
        for sid in static_sources[:40]:
            log.info("      %s", sid)
        if len(static_sources) > 40:
            log.info("      ... +%d more files", len(static_sources) - 40)

        drive_ids, drive_names = target_drive_doc_copies(conn, broad=broad)
        log.info("  DRIVE-COPY (drive_sweep/drive_asset) cora-internal chunks: %d  (across %d files)",
                 len(drive_ids), len(drive_names))
        for nm in drive_names[:40]:
            log.info("      %s", nm)
        if len(drive_names) > 40:
            log.info("      ... +%d more files (see full manifest below)", len(drive_names) - 40)

        # Full auditable manifest: the inline log samples at 40, so write EVERY
        # selected filename to disk -- a broad --apply must be reviewable in full
        # before it irreversibly deletes anything.
        try:
            manifest = _REPO / "logs" / f"purge-cora-internal-{args.scope}.txt"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            with manifest.open("w", encoding="utf-8") as fh:
                fh.write(f"# Cora-internal purge manifest  scope={args.scope}\n")
                fh.write(f"# static_md files ({len(static_sources)}):\n")
                for s in static_sources:
                    fh.write(f"  {s}\n")
                fh.write(f"# drive-copy files ({len(drive_names)}):\n")
                for n in drive_names:
                    fh.write(f"  {n}\n")
            log.info("  Full file manifest written -> %s", manifest)
        except Exception as exc:  # noqa: BLE001
            log.warning("  could not write manifest: %s", exc)

        note_hits: list[tuple[str, str, str]] = []
        if args.include_notes:
            note_hits = target_notes(conn, args.note_pattern)
            log.info("  USER_NOTE suspicious matches (pattern=%r): %d", args.note_pattern, len(note_hits))
            for chunk_id, source_id, excerpt in note_hits:
                log.info("      [%s] %s", chunk_id, excerpt)
        else:
            preview = target_notes(conn, args.note_pattern)
            log.info("  USER_NOTE suspicious matches (report-only; pass --include-notes to delete): %d",
                     len(preview))
            for chunk_id, source_id, excerpt in preview:
                log.info("      [%s] %s", chunk_id, excerpt)

        folder_ids: list[str] = []
        folder_files = folder_chunks = 0
        folder_complete = True
        folder_selections: list[FolderSelection] = []
        if folder_id_args:
            service = _drive_service()
            folder_ids, folder_files, folder_chunks, folder_complete, folder_selections = run_folder_mode(
                conn, service, folder_id_args, _REPO / "logs",
                apply=apply_changes, expect_leaf=args.expect_leaf,
                accept_delta=args.accept_delta)
        elif args.expect_leaf or args.accept_delta:
            log.warning("--expect-leaf / --accept-delta have no effect without --folder-id")

        # UNION: a chunk selected by several passes is deleted exactly once;
        # ``overlap`` counts CHUNKS that more than one pass selected (measured).
        to_delete, overlap = select_for_delete(
            static_ids, drive_ids, folder_ids, [h[0] for h in note_hits])
        log.info("  COUNTS  static_md %d files / %d chunks | drive-copy(title, scope=%s) %d files / %d chunks"
                 " | folder-mode %d files / %d chunks%s",
                 len(static_sources), len(static_ids), args.scope, len(drive_names), len(drive_ids),
                 folder_files, folder_chunks,
                 f" | notes {len(note_hits)}" if args.include_notes else "")
        log.info("  TOTAL chunks that --apply would delete: %d (unique; %d selected by more than one pass)",
                 len(to_delete), overlap)

        if not apply_changes:
            log.info("Dry-run -- nothing deleted. Re-run with --apply (Cora STOPPED), "
                     "then reclaim_kb_space.py.")
            return 0

        refusal = folder_apply_gate(bool(folder_id_args), folder_complete)
        if refusal:
            log.error(refusal)
            return 1

        if not to_delete:
            log.info("Nothing to delete.")
            return 0

        # INTENT first (D-051 MED-2 / write-finding-3 pattern): every selected row
        # is dumped BEFORE the delete; if that cannot be written, nothing is deleted.
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        logs_dir = _REPO / "logs"
        selected_path = logs_dir / f"purge-cora-internal-selected-{stamp}.json"
        n_dumped = dump_selected_rows(conn, to_delete, selected_path)
        log.info("Selected-rows intent written (%d rows) -> %s", n_dumped, selected_path)

        log.info("Deleting %d chunks from knowledge_chunks + every vec table...", len(to_delete))
        totals = delete_chunks(conn, to_delete)
        log.info("Deleted: %s", totals)
        log.info("Reclaim disk with: .venv\\Scripts\\python.exe scripts\\reclaim_kb_space.py")

        # RECORD after: the applied artifacts describe what actually happened, so an
        # "applied" file can never exist for a run that deleted nothing.
        try:
            applied_json = logs_dir / f"purge-cora-internal-applied-{stamp}.json"
            applied_json.write_text(json.dumps({
                "deleted_utc": datetime.now(timezone.utc).isoformat(),
                "selected_rows_file": selected_path.name,
                "count": len(to_delete),
                "deleted": dict(totals),
            }, indent=1), encoding="utf-8")
            log.info("Applied record written -> %s", applied_json)
            for pth in write_applied_records(folder_selections, totals, logs_dir, stamp):
                log.info("Applied folder record written -> %s", pth)
        except Exception as exc:  # noqa: BLE001 -- the delete already happened; say so loudly
            log.error("delete SUCCEEDED but the applied record could not be written (%s) -- the "
                      "pre-delete intent file %s is the record of what was deleted", exc, selected_path)
            conn.close()
            return 1
    except RuntimeError as exc:
        # A deliberate refusal (root/top-level id, expect-leaf mismatch, unreviewed
        # delta, unwritable record, missing SA) -- the reason IS the message.
        log.error("%s", exc)
        conn.close()
        return 1
    except Exception as exc:  # noqa: BLE001
        log.error("purge failed: %s", exc, exc_info=True)
        conn.close()
        return 1
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
