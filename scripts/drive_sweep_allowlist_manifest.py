#!/usr/bin/env python3
"""Drive-sweep ALLOWLIST migration manifest -- READ-ONLY dry run (Code #13 slice 8, D-303).

What it answers: of every file the flat per-user Drive sweep has ingested for ONE
account (KB rows with source='drive_sweep' and metadata.user_email = the account),
which sit INSIDE the account's allowlisted folders, which sit OUTSIDE (grouped by
the top-level folder, i.e. the child of My Drive), and which can no longer be
resolved (404 / trashed / API error -- STALE KB rows, never a purge candidate here).

Why the Drive API: drive_sweep rows carry the BARE Drive file id as source_id and
NO folder chain in metadata, so ancestry must be resolved per file with
files.get(fields='id,name,parents,mimeType,trashed') under DWD as the account
(the same builder + retry helper the sweep uses) and then walked upward with
drive_sweep._ancestor_chain -- one lookup per DISTINCT folder, cached, and the
folder cache PERSISTS as JSON so re-runs after an allowlist edit are cheap. A
cached folder is never re-read: after a rename or move in Drive, delete the cache
(or pass --cache <new path>) -- only a purge folder whose name was REFUSED is
evicted automatically, so its rename is picked up (D-051 r3 docs#r3-0).

Writes: ONLY under --out-dir (the manifest .txt + the folder cache JSON). Never
the KB, never logs/ by default (both paths are explicit arguments so a test can
point them at a temp dir). Nothing here deletes anything.

The manifest has three decision sections:
  * CANDIDATES TO ADD TO THE ALLOWLIST -- every out-of-tree top-level folder that
    holds KB rows, with the yaml line to paste. The kickoff rule: a tree-adjacent
    BUSINESS folder is ADDED to drive_sweep_allowlist, never purged.
  * PURGE LINES -- per purgeable folder, the ready-to-run
      scripts/purge_cora_internal_kb.py --folder-id <id> --expect-leaf '<name>' --impersonate <account>
    line (the leaf a PowerShell SINGLE-quoted literal, every single-quote character
    doubled; a name PS 5.1 cannot pass intact -- a double quote, a trailing backslash,
    a control character, any non-ASCII character -- prints a REFUSED line instead;
    Code #15 C13-02) plus the
    runbook 4e ``$kids`` row to paste as printed, for the UNCHANGED positive-leaf gate (option (a): no new selector was
    added to the purge script). The gate refuses a chain shorter than 3
    ([folder, ..., root]), so a TOP-LEVEL folder (depth 2 under My Drive) is
    itself REFUSED: the lines are emitted per CHILD folder (depth 3) -- the 9/9
    precedent (pin the parent, purge per child) -- and files directly under My
    Drive or directly inside a top-level folder are flagged
    'unreachable by the gate -- move in Drive first'.
  * UNRESOLVED / STALE KB ROWS -- reported separately; never in any purge set.

Usage (normal PowerShell, Cora may be running -- this is read-only):
    .venv\\Scripts\\python.exe scripts\\drive_sweep_allowlist_manifest.py --db data\\cora_kb.db --out-dir logs\\drive-sweep-allowlist
Options:
    --account EMAIL        account whose flat-sweep rows to audit (default harrison@hjrglobal.com)
    --allowlist-id ID      allowlisted folder id (repeatable). Default: the account's
                           drive_sweep_allowlist row in --accounts-yaml, else the Founder-OS root.
    --accounts-yaml PATH   roster to read the allowlist from (default data/maps/monitored-email-accounts.yaml)
    --limit N              audit only the N largest files (by chunk count); 0 = all
    --cache PATH           folder-cache JSON (default <out-dir>/drive-sweep-allowlist-folder-cache.json)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora.connectors import drive_sweep as ds  # noqa: E402

log = logging.getLogger("drive_sweep_allowlist_manifest")

DEFAULT_ACCOUNT = "harrison@hjrglobal.com"
DEFAULT_ACCOUNTS_YAML = _REPO_ROOT / "data" / "maps" / "monitored-email-accounts.yaml"
CACHE_BASENAME = "drive-sweep-allowlist-folder-cache.json"
MANIFEST_PREFIX = "drive-sweep-allowlist-manifest-"
_FILE_FIELDS = "id,name,parents,mimeType,trashed"
_CACHE_FLUSH_EVERY = 200

# Buckets
IN_ALLOWLIST = "IN_ALLOWLIST"
OUTSIDE = "OUTSIDE"
UNRESOLVED = "UNRESOLVED"

# Synthetic group keys for OUTSIDE files no folder purge can reach
GROUP_ROOT_LOOSE = "__root_loose__"
GROUP_NO_PARENTS = "__no_parents__"

# The purge gate's depth floor, mirrored (chain [folder, ..., root] must be >= 3).
# Not imported from the purge script: this manifest must load without that
# script's optional dependencies, and the value is pinned by test against it.
PURGE_MIN_CHAIN_DEPTH = 3


# ── KB side (read-only SELECT) ────────────────────────────────────────────────
def load_kb_files(conn: sqlite3.Connection, account: str, *, limit: int = 0) -> list[dict[str, Any]]:
    """``[{file_id, title, chunks}]`` for the account's flat-sweep rows, largest first.
    source_id is the BARE file id (0 legacy ':chunkN' rows live, but the prefix is
    stripped defensively so a legacy row still groups with its file)."""
    rows = conn.execute(
        "SELECT source_id, MIN(title), COUNT(*) FROM knowledge_chunks "
        "WHERE source = 'drive_sweep' AND json_extract(metadata, '$.user_email') = ? "
        "GROUP BY source_id",
        (account,),
    ).fetchall()
    merged: dict[str, dict[str, Any]] = {}
    for source_id, title, n in rows:
        fid = str(source_id or "").split(":chunk", 1)[0]
        if not fid:
            continue
        ent = merged.setdefault(fid, {"file_id": fid, "title": str(title or ""), "chunks": 0})
        ent["chunks"] += int(n or 0)
        if not ent["title"] and title:
            ent["title"] = str(title)
    out = sorted(merged.values(), key=lambda r: (-r["chunks"], r["title"].lower(), r["file_id"]))
    if limit and limit > 0:
        out = out[:limit]
    return out


# ── Drive side (read-only files.get) ──────────────────────────────────────────
def _http_status(exc: BaseException) -> int | None:
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def resolve_file(service: Any, file_id: str, folder_cache: dict) -> dict[str, Any]:
    """One files.get for the FILE, then the cached folder walk.

    Returns ``{status, reason, name, mime, parents, chain[(id,name)], complete}``
    where status is 'ok' | 'missing' (404) | 'trashed' | 'error'. ``chain`` runs
    from the file's parent UP to the topmost resolved ancestor; with
    ``complete=True`` its last node is parentless (the root)."""
    out: dict[str, Any] = {"status": "ok", "reason": "", "name": "", "mime": "", "parents": [],
                           "chain": [], "complete": False}
    try:
        meta = ds._retry_execute(service.files().get(fileId=file_id, fields=_FILE_FIELDS))
    except Exception as exc:  # noqa: BLE001 -- classified, never fatal
        if _http_status(exc) == 404:
            out.update(status="missing", reason="404 not found (deleted or no longer visible)")
        else:
            out.update(status="error", reason=f"files.get failed: {type(exc).__name__}: {exc}")
        return out
    meta = meta if isinstance(meta, dict) else {}
    out["name"] = str(meta.get("name") or "")
    out["mime"] = str(meta.get("mimeType") or "")
    out["parents"] = [str(p) for p in (meta.get("parents") or [])]
    if meta.get("trashed"):
        out.update(status="trashed", reason="trashed in Drive (the sweep only lists trashed=false)")
        return out
    if not out["parents"]:
        out["complete"] = True          # nothing to walk; a parentless file is fully "resolved"
        return out
    chain, complete = ds._ancestor_chain(service, out["parents"], folder_cache)
    out["chain"] = [(str(fid), str(name)) for fid, name in chain]
    out["complete"] = bool(complete)
    if not complete:
        out.update(status="error", reason="ancestry walk incomplete (folder lookup failed, cycle or bound)")
    return out


# ── classification ────────────────────────────────────────────────────────────
def classify(resolved: dict[str, Any], allowlist: frozenset[str]) -> dict[str, Any]:
    """Bucket + purge reachability for one resolved file.

    ``group`` is the TOP-LEVEL folder (the child of the root) as ``(id, name)``, or
    a synthetic key for root-loose / parentless files. ``purge_folder`` is the
    shallowest folder the purge gate ADMITS (chain depth >= PURGE_MIN_CHAIN_DEPTH
    counted [folder, ..., root]) -- i.e. the child of the top-level folder -- or
    None when nothing reaches the file (flagged ``unreachable``)."""
    if resolved["status"] != "ok":
        return {"bucket": UNRESOLVED, "reason": resolved["reason"], "group": None, "root": None,
                "purge_folder": None, "unreachable": ""}
    chain = resolved["chain"]
    ids = {fid for fid, _n in chain}
    if ids & allowlist:
        return {"bucket": IN_ALLOWLIST, "reason": "", "group": None, "root": None,
                "purge_folder": None, "unreachable": ""}
    n = len(chain)
    root = chain[-1] if chain else None
    if n == 0:
        return {"bucket": OUTSIDE, "reason": "no parents (shared-with-me / orphaned)",
                "group": (GROUP_NO_PARENTS, "(no parents: shared-with-me / orphaned)"), "root": None,
                "purge_folder": None,
                "unreachable": "unreachable by the gate -- no folder to purge by; move in Drive first"}
    if n == 1:
        return {"bucket": OUTSIDE, "reason": "loose file directly under the root",
                "group": (GROUP_ROOT_LOOSE, f"(loose files directly under {root[1] or 'the root'})"), "root": root,
                "purge_folder": None,
                "unreachable": "unreachable by the gate -- move in Drive first (the gate refuses the root)"}
    top = chain[-2]                       # the child of the root = the top-level folder
    if n < PURGE_MIN_CHAIN_DEPTH:
        # the file sits DIRECTLY inside a top-level (depth-2) folder: the gate refuses that folder
        return {"bucket": OUTSIDE, "reason": "directly inside a top-level folder",
                "group": top, "root": root, "purge_folder": None,
                "unreachable": ("unreachable by the gate -- the top-level folder is depth 2 (REFUSED); "
                                "move the file into a sub-folder in Drive first")}
    purge_folder = chain[n - PURGE_MIN_CHAIN_DEPTH]   # shallowest admitted folder (child of the top-level)
    return {"bucket": OUTSIDE, "reason": "", "group": top, "root": root,
            "purge_folder": purge_folder, "unreachable": ""}


def build_report(
    conn: sqlite3.Connection, service: Any, *, account: str, allowlist: frozenset[str],
    folder_cache: dict, limit: int = 0, cache_path: Path | None = None,
) -> dict[str, Any]:
    """Resolve + classify every KB file of the account. Read-only except the
    folder cache (flushed every _CACHE_FLUSH_EVERY files when ``cache_path`` is given).

    A purge folder whose name the renderer will REFUSE is EVICTED from the cache
    before the final save (D-051 r3 docs#r3-0): ``_ancestor_chain`` never calls
    files.get for a cached folder, so after the prescribed rename in Drive the
    re-run rendered the OLD name and the same REFUSED line. Evicted, the next run
    re-fetches that one folder and sees the new name. A MOVED folder's cached
    parents are not refreshed this way -- the REFUSED line and runbook 4e / step 2
    say to delete the cache (or pass --cache <new path>) after a move."""
    files = load_kb_files(conn, account, limit=limit)
    entries: list[dict[str, Any]] = []
    for i, f in enumerate(files, 1):
        resolved = resolve_file(service, f["file_id"], folder_cache)
        cls = classify(resolved, allowlist)
        entries.append({**f, **resolved, **cls})
        if cache_path is not None and i % _CACHE_FLUSH_EVERY == 0:
            save_cache(cache_path, folder_cache)
    for e in entries:
        if e["bucket"] == OUTSIDE and e["purge_folder"] and not _ps_safe(e["purge_folder"][1]):
            folder_cache.pop(e["purge_folder"][0], None)
    if cache_path is not None:
        save_cache(cache_path, folder_cache)
    groups: dict[str, dict[str, Any]] = {}
    for e in entries:
        if e["bucket"] != OUTSIDE:
            continue
        gid, gname = e["group"]
        g = groups.setdefault(gid, {"id": gid, "name": gname, "root": e.get("root"), "files": 0, "chunks": 0,
                                    "purge_folders": {}, "unreachable_files": 0, "unreachable_chunks": 0})
        g["files"] += 1
        g["chunks"] += e["chunks"]
        if e["purge_folder"]:
            pid, pname = e["purge_folder"]
            pf = g["purge_folders"].setdefault(pid, {"id": pid, "name": pname, "files": 0, "chunks": 0})
            pf["files"] += 1
            pf["chunks"] += e["chunks"]
        else:
            g["unreachable_files"] += 1
            g["unreachable_chunks"] += e["chunks"]
    totals = {b: {"files": 0, "chunks": 0} for b in (IN_ALLOWLIST, OUTSIDE, UNRESOLVED)}
    for e in entries:
        totals[e["bucket"]]["files"] += 1
        totals[e["bucket"]]["chunks"] += e["chunks"]
    return {
        "account": account,
        "allowlist": sorted(allowlist),
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "limit": limit,
        "files_total": len(files),
        "entries": entries,
        "groups": groups,
        "totals": totals,
    }


# ── rendering ─────────────────────────────────────────────────────────────────
def _chain_txt(chain: list[tuple[str, str]]) -> str:
    """root -> ... -> parent (names), '(none)' when empty."""
    return " / ".join(name or "(unnamed)" for _fid, name in reversed(chain)) or "(none)"


class UnsafeLeafName(ValueError):
    """A folder name that cannot be passed through PowerShell 5.1 to a native exe
    intact. Refused LOUDLY -- the manifest prints a REFUSED line, never a mangled one."""


#: PowerShell treats ALL of these as single-quote characters (it also closes a
#: single-quoted string on the typographic ones): inside '...' each is escaped by
#: DOUBLING it (Code #15 C13-02 -- "Harrison's ..." folder names are common). Only the
#: ASCII one can reach the doubling now (a non-ASCII name is refused first, D-051 r1
#: rb-pins#0); the typographic ones stay listed as a belt.
_PS_SINGLE_QUOTES = ("'", "‘", "’", "‚", "‛")


def ps_single_quote(value: str) -> str:
    """``value`` as a PowerShell SINGLE-quoted literal (no ``$`` / backtick expansion;
    every single-quote character doubled). Refuses (UnsafeLeafName) what PS 5.1 cannot
    hand to a native exe intact: an ASCII double quote (5.1 does not escape an embedded
    ``"`` when it re-quotes an argument), a trailing backslash (it would escape that
    re-added closing quote), a CR / LF / NUL / other control character, an empty name,
    or ANY non-ASCII character -- PS 5.1 reads a BOM-less UTF-8 .ps1 (and Get-Content
    shows a BOM-less .txt) as Windows-1252, where UTF-8 continuation bytes such as 0x82 /
    0x91 / 0x92 decode to single-quote characters that close the literal early (D-016:
    the emitted PowerShell stays ASCII)."""
    raw = str(value or "")
    if not raw:
        raise UnsafeLeafName("empty folder name")
    if '"' in raw:
        raise UnsafeLeafName("the folder name contains a double quote")
    if raw.endswith("\\"):
        raise UnsafeLeafName("the folder name ends with a backslash")
    if any(ord(c) < 32 or ord(c) == 127 for c in raw):
        raise UnsafeLeafName("the folder name contains a control character")
    if not raw.isascii():
        raise UnsafeLeafName("the folder name contains a non-ASCII character")
    out = raw
    for q in _PS_SINGLE_QUOTES:
        out = out.replace(q, q + q)
    return f"'{out}'"


def _ps_safe(value: str) -> bool:
    """Would ``ps_single_quote`` accept ``value`` (i.e. the renderer print a line)?"""
    try:
        ps_single_quote(value)
    except UnsafeLeafName:
        return False
    return True


def purge_line(folder_id: str, leaf_name: str, account: str) -> str:
    """The ready-to-run dry-run line for the UNCHANGED positive-leaf gate. ``--apply``
    is added ONLY inside the stop window (deployment/runbook.md, slice-8 section).
    The leaf is a PowerShell single-quoted literal (ps_single_quote -- Code #15
    C13-02); an unsafe name raises UnsafeLeafName (the renderer prints REFUSED)."""
    return (f".venv\\Scripts\\python.exe scripts\\purge_cora_internal_kb.py --folder-id {folder_id} "
            f"--expect-leaf {ps_single_quote(leaf_name)} --impersonate {account}")


def kids_row(folder_id: str, leaf_name: str) -> str:
    """The runbook 4e ``$kids`` row for one purge folder, PS-safe (C13-02): paste it as
    printed -- the name is already a single-quoted literal with its quotes doubled."""
    return f"@{{id={ps_single_quote(folder_id)}; name={ps_single_quote(leaf_name)}}}"


def render_manifest(report: dict[str, Any]) -> str:
    acct = report["account"]
    lines: list[str] = []
    lines.append(f"DRIVE-SWEEP ALLOWLIST MIGRATION MANIFEST -- {acct} -- generated {report['generated']}")
    lines.append("READ-ONLY dry run (Code #13 slice 8, D-303). Nothing here was deleted.")
    lines.append("Allowlisted folders: " + ", ".join(
        f"{ds.ALLOWLIST_FOLDER_LABELS.get(fid, '(unlabelled folder)')} [{fid}]" for fid in report["allowlist"]) or "(none)")
    if report.get("limit"):
        lines.append(f"LIMIT: only the {report['limit']} largest files were audited.")
    t = report["totals"]
    lines.append(f"Files audited: {report['files_total']} | IN allowlist: {t[IN_ALLOWLIST]['files']} files / "
                 f"{t[IN_ALLOWLIST]['chunks']} chunks | OUTSIDE: {t[OUTSIDE]['files']} files / {t[OUTSIDE]['chunks']} chunks | "
                 f"UNRESOLVED (stale): {t[UNRESOLVED]['files']} files / {t[UNRESOLVED]['chunks']} chunks")
    lines.append("")
    groups = sorted(report["groups"].values(), key=lambda g: (-g["chunks"], g["name"].lower()))

    lines.append("=" * 78)
    lines.append("OUTSIDE THE ALLOWLIST -- grouped by top-level folder (the child of the root)")
    lines.append("=" * 78)
    if not groups:
        lines.append("(none)")
    for g in groups:
        root_txt = f" under {g['root'][1]}" if g.get("root") else ""
        lines.append(f"[{g['name']}]{' [' + g['id'] + ']' if not g['id'].startswith('__') else ''}{root_txt} -- "
                     f"{g['files']} files / {g['chunks']} chunks")
        for e in sorted((x for x in report["entries"] if x["bucket"] == OUTSIDE and x["group"][0] == g["id"]),
                        key=lambda x: (-x["chunks"], x["title"].lower())):
            flag = f"   <-- {e['unreachable']}" if e["unreachable"] else ""
            lines.append(f"    {e['title'] or e['name'] or '(untitled)'}  [{e['file_id']}]  x{e['chunks']}  "
                         f"path: {_chain_txt(e['chain'])}{flag}")
        lines.append(f"    subtotal: {g['files']} files / {g['chunks']} chunks")
        lines.append("")

    lines.append("=" * 78)
    lines.append("CANDIDATES TO ADD TO THE ALLOWLIST")
    lines.append("(kickoff rule: a tree-adjacent BUSINESS folder is ADDED to drive_sweep_allowlist, never purged;")
    lines.append(" paste the yaml line under the account's drive_sweep_allowlist, re-run this manifest, and the")
    lines.append(" folder moves to IN allowlist. Personal folders stay OUT and go to PURGE LINES.)")
    lines.append("=" * 78)
    cands = [g for g in groups if not g["id"].startswith("__")]
    if not cands:
        lines.append("(none)")
    for g in cands:
        lines.append(f"- {g['name']} [{g['id']}] -- {g['files']} files / {g['chunks']} chunks")
        lines.append(f"      - \"{g['id']}\"   # {g['name']}")
    lines.append("")

    lines.append("=" * 78)
    lines.append("PURGE LINES -- the UNCHANGED positive-leaf gate (scripts/purge_cora_internal_kb.py folder mode)")
    lines.append("(run each WITHOUT --apply first, in normal PS; it writes logs/purge-cora-internal-folder-<id>.txt;")
    lines.append(" --apply happens ONLY inside the stop window, one folder per --apply -- deployment/runbook.md.")
    lines.append(f" The gate refuses chains shorter than {PURGE_MIN_CHAIN_DEPTH} [folder, ..., root]: a top-level folder")
    lines.append(" (depth 2) is itself REFUSED, so lines are per CHILD folder -- the 9/9 precedent.)")
    lines.append("=" * 78)
    any_line = False
    for g in cands:
        lines.append(f"# {g['name']} [{g['id']}] is depth-2 (child of {g['root'][1] if g.get('root') else 'the root'}) "
                     f"-- REFUSED by the gate as a --folder-id; purge per child below or move it under a folder in Drive first")
        for pf in sorted(g["purge_folders"].values(), key=lambda p: (-p["chunks"], p["name"].lower())):
            lines.append(f"# {pf['files']} files / {pf['chunks']} chunks under {g['name']} / {pf['name']}")
            try:
                line, row = purge_line(pf["id"], pf["name"], acct), kids_row(pf["id"], pf["name"])
            except UnsafeLeafName as exc:
                lines.append(f"# REFUSED: folder {pf['id']} -- {exc}; PowerShell 5.1 cannot pass it to "
                             f"--expect-leaf intact. Rename the folder in Drive, then re-run this manifest "
                             f"(this folder was dropped from the folder cache so the re-run re-reads its "
                             f"name; after MOVING a folder in Drive, delete {CACHE_BASENAME} in the "
                             f"out-dir or pass --cache <new path> first).")
                continue
            lines.append(line)
            lines.append(f"#   runbook 4e $kids row (paste as printed): {row}")
            any_line = True
        if g["unreachable_files"]:
            lines.append(f"# {g['unreachable_files']} files / {g['unreachable_chunks']} chunks sit DIRECTLY in "
                         f"{g['name']} -- unreachable by the gate -- move in Drive first")
        lines.append("")
    for g in groups:
        if g["id"].startswith("__"):
            lines.append(f"# {g['name']}: {g['files']} files / {g['chunks']} chunks -- unreachable by the gate -- "
                         f"move in Drive first")
    if not any_line:
        lines.append("(no purgeable folder -- nothing to run)")
    lines.append("")

    lines.append("=" * 78)
    lines.append("UNRESOLVED / STALE KB ROWS -- NEVER in a purge set here (404 / trashed / API error)")
    lines.append("(a 404 or trashed file is a stale KB row for the monthly kb-hygiene sweep; an API error is a retry)")
    lines.append("=" * 78)
    unres = [e for e in report["entries"] if e["bucket"] == UNRESOLVED]
    if not unres:
        lines.append("(none)")
    for e in sorted(unres, key=lambda x: (x["status"], -x["chunks"])):
        lines.append(f"    {e['title'] or '(untitled)'}  [{e['file_id']}]  x{e['chunks']}  {e['status']}: {e['reason']}")
    lines.append("")

    lines.append("=" * 78)
    lines.append("IN THE ALLOWLIST (kept)")
    lines.append("=" * 78)
    ins = [e for e in report["entries"] if e["bucket"] == IN_ALLOWLIST]
    lines.append(f"{len(ins)} files / {sum(e['chunks'] for e in ins)} chunks")
    for e in sorted(ins, key=lambda x: (-x["chunks"], x["title"].lower()))[:200]:
        lines.append(f"    {e['title'] or e['name'] or '(untitled)'}  [{e['file_id']}]  x{e['chunks']}  "
                     f"path: {_chain_txt(e['chain'])}")
    if len(ins) > 200:
        lines.append(f"    ... +{len(ins) - 200} more in-allowlist files")
    return "\n".join(lines) + "\n"


def write_manifest(report: dict[str, Any], out_dir: Path) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{MANIFEST_PREFIX}{datetime.now().strftime('%Y-%m-%d')}.txt"
    path.write_text(render_manifest(report), encoding="utf-8")
    return path


# ── folder cache (the ONE persisted artefact besides the manifest) ────────────
def load_cache(path: Path) -> dict:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001 -- a corrupt cache is rebuilt, never fatal
        log.warning("folder cache unreadable (%s) -- starting empty", exc)
        return {}
    out: dict = {}
    if isinstance(raw, dict):
        for fid, entry in raw.items():
            if isinstance(entry, dict) and "parents" in entry:
                out[str(fid)] = {"parents": [str(p) for p in (entry.get("parents") or [])],
                                 "name": str(entry.get("name") or "")}
    return out


def save_cache(path: Path, cache: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cache, indent=0, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


# ── allowlist source ──────────────────────────────────────────────────────────
def allowlist_for_account(accounts_yaml: Path, account: str) -> tuple[frozenset[str], str]:
    """``(allowlist, source)`` -- the PRIMARY row's drive_sweep_allowlist when the
    account is in allowlist mode, else the Founder-OS root as the v1 default."""
    try:
        import yaml
        cfg = yaml.safe_load(Path(accounts_yaml).read_text(encoding="utf-8")) or {}
        for row in cfg.get("accounts") or []:
            if not isinstance(row, dict) or str(row.get("email") or "").lower() != account.lower():
                continue
            if ds.DRIVE_SWEEP_MODE_KEY not in row:
                continue
            mode, allow, err = ds.resolve_sweep_mode(row)
            if mode == ds.DRIVE_SWEEP_MODE_ALLOWLIST and allow and not err:
                return allow, f"{Path(accounts_yaml).name} ({account} drive_sweep_allowlist)"
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read %s (%s) -- using the Founder-OS root", accounts_yaml, exc)
    return frozenset({ds.FOUNDERS_OS_ROOT_ID}), "default (drive_sweep.FOUNDERS_OS_ROOT_ID)"


def _drive_service(account: str) -> Any:
    sa = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not sa or not Path(sa).exists():
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON not set or file not found -- "
                           "the manifest needs the Cora service account (read-only Drive, DWD as the account)")
    return ds._build_drive_service(sa, account)


# ── CLI ───────────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, help="Path to the KB sqlite DB (opened read-only).")
    ap.add_argument("--out-dir", required=True, help="Directory for the manifest + folder cache (the ONLY writes).")
    ap.add_argument("--account", default=DEFAULT_ACCOUNT, help=f"Account to audit (default {DEFAULT_ACCOUNT}).")
    ap.add_argument("--allowlist-id", action="append", default=[], metavar="FOLDER_ID",
                    help="Allowlisted folder id (repeatable). Default: the roster row, else the Founder-OS root.")
    ap.add_argument("--accounts-yaml", default=str(DEFAULT_ACCOUNTS_YAML),
                    help="Roster to read drive_sweep_allowlist from when --allowlist-id is not given.")
    ap.add_argument("--limit", type=int, default=0, help="Audit only the N largest files (0 = all).")
    ap.add_argument("--cache", default=None, help=f"Folder-cache JSON (default <out-dir>/{CACHE_BASENAME}).")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    from dotenv import load_dotenv
    load_dotenv(_REPO_ROOT / ".env", override=True)   # D-021 (Code #15 C13-01)

    out_dir = Path(args.out_dir)
    cache_path = Path(args.cache) if args.cache else out_dir / CACHE_BASENAME
    if args.allowlist_id:
        allowlist = frozenset(str(x).strip() for x in args.allowlist_id if str(x).strip())
        source = "--allowlist-id"
    else:
        allowlist, source = allowlist_for_account(Path(args.accounts_yaml), args.account)
    if not allowlist:
        log.error("empty allowlist -- refusing to classify everything as OUTSIDE")
        return 2
    log.info("allowlist (%s): %s", source, sorted(allowlist))

    db_path = Path(args.db)
    if not db_path.exists():
        log.error("KB not found: %s", db_path)
        return 2
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        service = _drive_service(args.account)
        folder_cache = load_cache(cache_path)
        log.info("folder cache: %d folders loaded from %s", len(folder_cache), cache_path)
        report = build_report(conn, service, account=args.account, allowlist=allowlist,
                              folder_cache=folder_cache, limit=args.limit, cache_path=cache_path)
    finally:
        conn.close()
    path = write_manifest(report, out_dir)
    t = report["totals"]
    log.info("MANIFEST written: %s -- files=%d in_allowlist=%d/%d outside=%d/%d unresolved=%d/%d (files/chunks); "
             "top-level OUTSIDE groups=%d; folder cache now %d folders",
             path, report["files_total"], t[IN_ALLOWLIST]["files"], t[IN_ALLOWLIST]["chunks"],
             t[OUTSIDE]["files"], t[OUTSIDE]["chunks"], t[UNRESOLVED]["files"], t[UNRESOLVED]["chunks"],
             len(report["groups"]), len(folder_cache))
    return 0


if __name__ == "__main__":
    sys.exit(main())
