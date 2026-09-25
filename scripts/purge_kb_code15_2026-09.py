#!/usr/bin/env python3
r"""Code #15 combined KB purge -- ONE dry-run/apply script with a LANE REGISTRY
(S1 cq-d9d0c92cc797 lane ``s1_tokens``; RIDER B registers its lanes in THIS file).

WHY ONE SCRIPT. Code #15 S1 (API-token shapes at rest) and RIDER B (KB items that
must leave the index) both change ``knowledge_chunks``. One reviewed INTENT, one
apply, one APPLIED record: a chunk in a DELETE lane is never also REDACTED, the
D-087 LARGE test sees the union, and Harrison eyeballs a single manifest.

LANES (``LANES`` / ``register_lane``). A lane is ``Lane(name, action, select, ...)``
whose selector returns a ``LanePlan``:

    lane        -- the lane name (``s1_tokens``, RIDER B's ``rb*`` lanes)
    action      -- ``REDACT`` (UPDATE content/title in place) | ``DELETE``
                   (``kb_archive.delete_chunks``: vectors + the row)
    chunk_ids   -- what the apply acts on
    pre_sha256  -- chunk_id -> sha256(content NUL title) at dry-run (drift refusal)
    counts      -- ids/counts only, NEVER text, titles or LEX paths/names (D-082)
    files       -- file count for the D-087 LARGE test (0 for S1)
    holds       -- items deliberately NOT applied, each with its held-why
    stops       -- items whose safety gate tripped; a lane with ANY stop cannot be
                   applied (deselect it with ``--lanes``)

``s1_tokens`` (REDACT): every chunk whose ``content`` or ``title`` carries one of the
four ``cora.secret_tokens`` shapes. The SQL prefilter is GLOB-only (case-sensitive,
like the token prefixes; never LIKE) and only NARROWS candidates -- the regex
decides. ``metadata`` / ``deep_link`` / ``source_id`` / ``author`` and every other
KB table are COUNT-ONLY surfaces: reported, never rewritten. Rows in the LEX
partition (``entity`` starts with ``LEX``) are HELD by default; ``--release-lex
s1_tokens`` on the DRY-RUN moves them into the plan and records the release in the
INTENT, and the apply must repeat the same flag (a release is never implied). The
redaction is NOT durable against a re-ingest (a drive_sweep row is rewritten from
its Drive source when that file changes) -- the egress belt at every chunk renderer
still covers it; the record says so.

RIDER B lanes (cq-59c5048d0891; every one a DELETE lane; ids/counts only in every
record; LEX-partition rows HELD unless --release-lex <lane>; a lane whose input or
safety gate is missing STOPS -- it never silently no-ops; a lane with planned chunks
is RE-VERIFIED at --apply before any write):

    rb2_personal_finances  item 2: the pinned PERSONAL store 00-Founder/personal-finances
                           through the UNCHANGED positive-leaf gate (folder mode, direct
                           SA, ID-ONLY); expected 0 chunks -> a recorded, applied no-op.
    rb3_static_old_paths   item 3: kb-purge-rows.csv (--csv; utf-8-sig; strict parse; its
                           sha256 recorded) -> static_md rows by EXACT source_id equality,
                           existence-gated against the Founder-OS root (--founder-root):
                           PURGE only when the old path is ABSENT (a MOVE also needs its
                           new path live with >= 1 static_md row), else HELD with the why.
                           The root must carry its anchor folders (00-Founder, _shared) and
                           an absence counts only while they still answer (a gone mount
                           STOPS the lane); the root's digest is recorded and an apply
                           under a different root refuses.
    rb3_archived_nonmd     item 3: the positive leaf _archive\dedup-2026-09 (folder mode,
                           --expect-leaf dedup-2026-09) -- source='drive_sweep' ONLY; the
                           drive_asset cards are KEPT and counted.
    rb4_desktop_ini        item 4: desktop.ini rows (expected 0 -> a recorded no-op).
    rb8_ufl_equity         item 8: three hard-coded UFL file ids, drive_sweep rows only,
                           a per-id Drive positive gate (non-folder, not trashed, 04-UFL
                           ancestor) and the measured counts (drift STOPS), plus the
                           recorded re-ingest analysis (no exclusion is built).

The folder lanes call ``purge_cora_internal_kb.run_folder_mode`` directly (its dry-run
writes an id-only reviewed manifest under ``<out-dir>/kb-purge-code15-folders-<stamp>/``;
the apply re-runs it with ``apply=True``) -- never that script's ``main()``, so its
always-on static_md / title passes never run here.

RUN SHAPE (the refile_misrouted_session_captures / D-086 shape):

    .venv\Scripts\python.exe scripts\purge_kb_code15_2026-09.py --csv <kb-purge-rows.csv>
    .venv\Scripts\python.exe scripts\purge_kb_code15_2026-09.py --csv <csv> --release-lex s1_tokens
    .venv\Scripts\python.exe scripts\purge_kb_code15_2026-09.py --apply --manifest logs\kb-purge-code15-INTENT-<stamp>.json --csv <csv> [--lanes s1_tokens,...] [--release-lex s1_tokens]

  * The dry-run (default) opens the KB READ-ONLY (``kb_archive.connect_ro``: a
    ``mode=ro`` URI + ``query_only``) -- it cannot write the DB (D-290) -- and writes
    ``<out-dir>/kb-purge-code15-INTENT-<stamp>.json`` + ``.txt``. Both are
    self-scanned (``secrets_scan.scan_text`` + ``secret_tokens``) BEFORE they are
    written; a hit writes nothing and exits 1.
  * ``--apply`` refuses without ``--manifest``; refuses a manifest for another DB;
    refuses any selected lane with stops, an action that disagrees with the code, a
    LEX release the INTENT does not record (or one the apply does not repeat);
    refuses when a planned chunk's CURRENT ``entity`` is in the LEX partition and its
    lane is not released at this apply (re-read on the read-only handle AND under the
    write lock -- a re-tag is not content drift, and an edited INTENT can carry a
    held id); refuses when any chunk's current sha256 differs from the INTENT (or the
    chunk vanished) unless ``--accept-delta``; and when the DELETE-lane union is LARGE
    (>500 chunks or >100 files, D-087) it requires Cora STOPPED -- the heartbeat file
    must EXIST and be older than 300 s by both its content stamp and its mtime; a
    MISSING or unparseable heartbeat refuses (fail closed) unless ``--allow-live``.
    The heartbeat is read before the lanes' re-verification AND again under the
    write lock (BEGIN IMMEDIATE); both readings go in the APPLIED record. Every
    refusal happens BEFORE the first write (an in-lock refusal rolls back).
  * The apply connection is ``kb_archive.connect_rw`` (vec0 loaded, so the DELETE
    cascade really deletes) with ``PRAGMA secure_delete=ON``. REDACT is ``UPDATE
    knowledge_chunks SET content=?, title=?`` with the STRICT redactor (a redactor
    error skips that chunk -- a withheld marker is never persisted); vectors are not
    re-embedded. The ``APPLIED`` record is written AFTER the act, from what
    happened: per-lane outcome, before/after occurrence counts from a re-scan,
    heartbeat evidence, and the honest residuals.

No .env is loaded at import. The RIDER B Drive lanes (rb2, rb3_archived_nonmd, rb8)
load the repo .env (override=True, D-021) on FIRST Drive use, to build the read-only
direct-SA Drive service -- files.get / files.list only (the self-scan reads the live
.env VALUES through secrets_scan only to compare them).

Exit codes: 0 ok, 1 refused / fatal.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
import os
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any, Callable

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from cora import kb_archive, secret_tokens  # noqa: E402
import secrets_scan  # noqa: E402

log = logging.getLogger("kb-purge-code15")

SCRIPT_ID = "purge_kb_code15_2026-09"
INTENT_VERSION = 1
ACTIONS = ("REDACT", "DELETE")
LARGE_CHUNKS = 500          # D-087 LARGE threshold (chunks, DELETE-lane union)
LARGE_FILES = 100           # D-087 LARGE threshold (files, DELETE lanes)
HEARTBEAT_STOPPED_S = 300   # the bot is "stopped" only past 300 s by BOTH clocks
_ID_BATCH = 500


class Refused(Exception):
    """A gate refused the run. Raised BEFORE any write."""


def default_out_dir() -> Path:
    """``CORA_KB_PURGE_OUT_DIR`` (the conftest redirect) else ``<repo>/logs``."""
    env = os.environ.get("CORA_KB_PURGE_OUT_DIR", "").strip()
    return Path(env) if env else _REPO_ROOT / "logs"


# ── the lane registry ─────────────────────────────────────────────────────────
@dataclass
class LanePlan:
    lane: str
    action: str
    chunk_ids: list[str] = field(default_factory=list)
    pre_sha256: dict[str, str] = field(default_factory=dict)
    counts: dict[str, Any] = field(default_factory=dict)
    files: int = 0
    holds: list[dict[str, Any]] = field(default_factory=list)
    stops: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class RunContext:
    release_lex: frozenset[str] = frozenset()
    # ── RIDER B inputs (every field optional: a lane that needs a missing input STOPS) ──
    #: kb-purge-rows.csv (lane rb3_static_old_paths).
    csv_path: Path | None = None
    #: The Founder-OS root the CSV relpaths are relative to (the existence gate).
    founder_root: Path | None = None
    #: The static-md content-hash store -- READ-ONLY (a residual count, never written).
    hash_store: Path | None = None
    #: Where the folder lanes' id-only reviewed manifests are written (dry-run) / read (apply).
    folder_dir: Path | None = None
    #: --apply --accept-delta (passed through to the positive-leaf gate's delta check).
    accept_delta: bool = False
    #: Builds the read-only Drive service (the direct SA) on first use; None = no Drive.
    drive_factory: Callable[[], Any] | None = None
    _drive: Any = field(default=None, repr=False)
    _drive_error: str | None = field(default=None, repr=False)

    def drive_service(self) -> Any:
        """The Drive service, built ONCE per run; a build failure is remembered so
        every Drive lane stops with the same reason instead of retrying."""
        if self._drive is not None:
            return self._drive
        if self._drive_error is None:
            if self.drive_factory is None:
                self._drive_error = "no Drive service configured"
            else:
                try:
                    self._drive = self.drive_factory()
                    return self._drive
                except Exception as exc:  # noqa: BLE001 -- the lane STOPS; the type is the record
                    self._drive_error = f"Drive service unavailable ({type(exc).__name__})"
        raise RuntimeError(self._drive_error)


@dataclass(frozen=True)
class Lane:
    name: str
    action: str
    select: Callable[[sqlite3.Connection, RunContext], LanePlan]
    #: REDACT lanes: the STRICT at-rest redactor ``text -> (text, n)`` (raises on error).
    redact: Callable[[str], tuple[str, int]] | None = None
    #: REDACT lanes: residual occurrences in one text (the post-apply re-scan).
    residual: Callable[[str], int] | None = None
    description: str = ""
    #: Apply-time re-verification ``(ro_conn, ctx, plan) -> [refusal reasons]``, run
    #: BEFORE any write on a lane with planned chunks (RIDER B: the positive-leaf
    #: gate re-run, the CSV existence gate, the UFL positive gate). [] = verified.
    verify: Callable[[sqlite3.Connection, RunContext, LanePlan], list[str]] | None = None


LANES: dict[str, Lane] = {}


def register_lane(lane: Lane) -> None:
    if lane.action not in ACTIONS:
        raise ValueError(f"lane {lane.name}: action must be one of {ACTIONS}")
    if lane.action == "REDACT" and (lane.redact is None or lane.residual is None):
        raise ValueError(f"lane {lane.name}: a REDACT lane needs redact + residual")
    if lane.name in LANES:
        raise ValueError(f"lane {lane.name} already registered")
    LANES[lane.name] = lane


def chunk_digest(content: str | None, title: str | None) -> str:
    raw = f"{content or ''}\x00{title or ''}".encode("utf-8", "surrogatepass")
    return hashlib.sha256(raw).hexdigest()


def current_digests(conn: sqlite3.Connection, ids: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for i in range(0, len(ids), _ID_BATCH):
        batch = ids[i:i + _ID_BATCH]
        ph = ",".join("?" * len(batch))
        for cid, content, title in conn.execute(
                f"SELECT chunk_id, content, title FROM knowledge_chunks WHERE chunk_id IN ({ph})", batch):
            out[cid] = chunk_digest(content, title)
    return out


def is_lex_partition(entity: str | None) -> bool:
    return str(entity or "").upper().startswith("LEX")


# ── lane s1_tokens (Code #15 S1) ──────────────────────────────────────────────
S1_LANE = "s1_tokens"
_S1_REDACT_COLUMNS = ("content", "title")
_S1_COUNT_ONLY_COLUMNS = ("metadata", "source_id", "deep_link", "author")
# GLOB, never LIKE: case-sensitive like the token prefixes. A SUPERSET prefilter --
# it only narrows the candidate rows; secret_tokens' regexes decide every count.
_S1_GLOBS = ("*sk-*", "*xox[abprs]-*", "*AIza*",
             "*[12]/[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]*")


def _glob_where(cols: tuple[str, ...] | list[str]) -> str:
    return " OR ".join(f'"{c}" GLOB \'{g}\'' for c in cols for g in _S1_GLOBS)


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _other_table_counts(conn: sqlite3.Connection) -> dict[str, Any]:
    """Count-only token scan of every other regular KB table (never rewritten)."""
    rows = conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table'").fetchall()
    virtual = [n for n, sql in rows if (sql or "").upper().startswith("CREATE VIRTUAL TABLE")]
    out: dict[str, Any] = {}
    for name, sql in sorted(rows):
        if (name.startswith("sqlite_") or name.startswith("knowledge_") or name in virtual
                or any(name.startswith(v + "_") for v in virtual)):
            continue
        try:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({_quote_ident(name)})")
                    if any(t in (r[2] or "TEXT").upper() for t in ("TEXT", "CHAR", "CLOB"))]
            total = conn.execute(f"SELECT COUNT(*) FROM {_quote_ident(name)}").fetchone()[0]
            hit_rows = 0
            occ: dict[str, int] = {}
            if cols:
                sel = ", ".join(_quote_ident(c) for c in cols)
                where = " OR ".join(f"{_quote_ident(c)} GLOB '{g}'" for c in cols for g in _S1_GLOBS)
                for row in conn.execute(f"SELECT {sel} FROM {_quote_ident(name)} WHERE {where}"):
                    row_hit = False
                    for val in row:
                        if isinstance(val, str) and val:
                            for shape, n in secret_tokens.count_by_shape(val).items():
                                occ[shape] = occ.get(shape, 0) + n
                                row_hit = True
                    hit_rows += row_hit
            out[name] = {"rows": int(total), "hit_rows": hit_rows, "occurrences": occ}
        except sqlite3.Error as exc:
            out[name] = {"error": type(exc).__name__}
    return out


def select_s1_tokens(conn: sqlite3.Connection, ctx: RunContext) -> LanePlan:
    plan = LanePlan(lane=S1_LANE, action="REDACT")
    released = S1_LANE in ctx.release_lex
    cols = _S1_REDACT_COLUMNS + _S1_COUNT_ONLY_COLUMNS
    occurrences: dict[str, dict[str, int]] = {}
    count_only_chunks: dict[str, int] = {c: 0 for c in _S1_COUNT_ONLY_COLUMNS}
    count_only_only = 0
    by_partition = {"LEX": {"redact": 0, "held": 0}, "non-LEX": {"redact": 0, "held": 0}}
    prefilter_rows = 0
    sql = (f"SELECT chunk_id, source, entity, {', '.join(cols)} FROM knowledge_chunks "
           f"WHERE {_glob_where(cols)}")
    for row in conn.execute(sql):
        prefilter_rows += 1
        cid, source, entity = row[0], row[1] or "?", row[2]
        vals = dict(zip(cols, row[3:]))
        redact_hit = False
        other_hit = False
        for col in cols:
            val = vals[col]
            if not isinstance(val, str) or not val:
                continue
            counts = secret_tokens.count_by_shape(val)
            if not counts:
                continue
            for shape, n in counts.items():
                bucket = occurrences.setdefault(shape, {})
                key = f"{source}.{col}"
                bucket[key] = bucket.get(key, 0) + n
            if col in _S1_REDACT_COLUMNS:
                redact_hit = True
            else:
                count_only_chunks[col] += 1
                other_hit = True
        if not redact_hit:
            count_only_only += other_hit
            continue
        part = "LEX" if is_lex_partition(entity) else "non-LEX"
        if part == "LEX" and not released:
            plan.holds.append({"chunk_id": cid, "partition": "LEX",
                               "reason": "LEX partition -- held by default; re-run the dry-run "
                                         "with --release-lex s1_tokens to plan it"})
            by_partition[part]["held"] += 1
            continue
        plan.chunk_ids.append(cid)
        plan.pre_sha256[cid] = chunk_digest(vals["content"], vals["title"])
        by_partition[part]["redact"] += 1
    plan.counts = {
        "prefilter_rows": prefilter_rows,
        "occurrences": occurrences,                 # {shape: {"<source>.<column>": n}}
        "redact_chunks": len(plan.chunk_ids),
        "held_chunks": len(plan.holds),
        "count_only_chunks": count_only_chunks,     # chunks with a hit in that column
        "count_only_only_chunks": count_only_only,  # hits ONLY in count-only columns
        "by_partition": by_partition,
        "lex_released": released,
        "other_tables": _other_table_counts(conn),
    }
    return plan


def _s1_residual(text: str) -> int:
    return sum(secret_tokens.count_by_shape(text).values())


register_lane(Lane(
    name=S1_LANE, action="REDACT", select=select_s1_tokens,
    redact=secret_tokens.redact_secret_tokens_strict, residual=_s1_residual,
    description="Code #15 S1 (cq-d9d0c92cc797): API-token shapes redacted in place in content + title",
))


# ══ RIDER B lanes (cq-59c5048d0891 items 2, 3, 4, 8) ═════════════════════════
# Every RIDER B lane is a DELETE lane. Shared rules: ids and counts only in every
# record (D-082: no title, no content, no Drive name, no LEX path -- a CSV row is
# named by its index and a 16-hex path digest); LEX-partition rows are HELD unless
# the dry-run AND the apply both pass --release-lex <lane> (S1's mechanism); a lane
# whose input or safety gate is missing STOPS (it never silently no-ops), and a
# lane with planned chunks is RE-VERIFIED at --apply before any write.
FOUNDER_OS_ROOT_DEFAULT = Path(r"G:\My Drive\HJR-Founder-OS")
HASH_STORE_DEFAULT = _REPO_ROOT / "data" / "state" / "static-md-content-hashes.json"
_DRIVE_SOURCES = ("drive_sweep", "drive_asset")
_FOLDER_MIME = "application/vnd.google-apps.folder"

RB2_LANE = "rb2_personal_finances"
RB3_STATIC_LANE = "rb3_static_old_paths"
RB3_ARCHIVE_LANE = "rb3_archived_nonmd"
RB4_LANE = "rb4_desktop_ini"
RB8_LANE = "rb8_ufl_equity"
RIDER_B_LANES = (RB2_LANE, RB3_STATIC_LANE, RB3_ARCHIVE_LANE, RB4_LANE, RB8_LANE)


def _sha16(text: str | None) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _glob_escape(value: str) -> str:
    """SQLite GLOB has no escape character: each metacharacter goes in a class of
    its own (``[*]``, ``[?]``, ``[[]``); ``\\``, ``]``, ``(``, space are literal."""
    return "".join(f"[{c}]" if c in "*?[" else c for c in value)


def _pci():
    """scripts/purge_cora_internal_kb.py -- imported lazily (its import runs a
    module-level logging.basicConfig; only the Drive lanes need it)."""
    import purge_cora_internal_kb as pci  # noqa: PLC0415
    return pci


def _present(conn: sqlite3.Connection, ids: list[str]) -> list[str]:
    return sorted(current_digests(conn, list(ids)))


def _plan_rows(conn: sqlite3.Connection, plan: LanePlan, ids: list[str],
               ctx: RunContext) -> tuple[dict[str, dict[str, int]], int, set[str]]:
    """Plan ``ids`` into ``plan`` (chunk_ids + pre_sha256), HOLDING LEX-partition rows
    unless the lane is released. Returns ``(by_source_entity, held_lex, file_keys)`` --
    ``file_keys`` = the distinct Drive file ids / static_md paths behind the PLANNED
    rows (a count for the D-087 LARGE test; never written)."""
    released = plan.lane in ctx.release_lex
    by: dict[str, dict[str, int]] = {}
    held = 0
    files: set[str] = set()
    uniq = sorted(dict.fromkeys(ids))
    for i in range(0, len(uniq), _ID_BATCH):
        batch = uniq[i:i + _ID_BATCH]
        ph = ",".join("?" * len(batch))
        for cid, source, source_id, entity, content, title in conn.execute(
                f"SELECT chunk_id, source, source_id, entity, content, title FROM knowledge_chunks "
                f"WHERE chunk_id IN ({ph})", batch):
            if is_lex_partition(entity) and not released:
                plan.holds.append({"chunk_id": cid, "partition": "LEX",
                                   "reason": f"LEX partition -- held by default; re-run the dry-run with "
                                             f"--release-lex {plan.lane} to plan it"})
                held += 1
                continue
            plan.chunk_ids.append(cid)
            plan.pre_sha256[cid] = chunk_digest(content, title)
            src = str(source or "?")
            ent = str(entity or "?")
            by.setdefault(src, {})
            by[src][ent] = by[src].get(ent, 0) + 1
            sid = str(source_id or "")
            files.add(sid.split(":", 1)[0] if src in _DRIVE_SOURCES else sid)
    return by, held, files


def _refusal_text(exc: BaseException) -> str:
    """A gate refusal's reason for a record. The positive-leaf gate runs in ID-ONLY
    mode, so its RuntimeError messages carry ids only; any other error is recorded
    by TYPE (a Drive HttpError string can carry a URL + a response body)."""
    if isinstance(exc, RuntimeError):
        return str(exc)[:400]
    return type(exc).__name__


# ── folder lanes: rb2_personal_finances + rb3_archived_nonmd ─────────────────
@dataclass(frozen=True)
class FolderLaneSpec:
    lane: str
    item: str
    folder_id: str
    expect_leaf: str
    #: the Drive-copy sources the lane DELETES (the other one is counted, kept)
    sources: tuple[str, ...]
    #: the measured expectation recorded beside the selection (None = no expectation)
    expected_chunks: int | None = None


#: Item 2 -- the PERSONAL store (pinned in kb_exclusions). Measured 2026-09-24: 1,486
#: descendant files (a complete enumeration), 0 chunks in every source -> an applied
#: no-op. BOTH Drive-copy sources are selected on purpose: any row here is a leak of
#: a personal store (drive_asset already blacklists the segment, so 0 is expected).
RB2_SPEC = FolderLaneSpec(lane=RB2_LANE, item="item 2 (personal-finances pin + purge)",
                          folder_id="1l7Hms6KwISUelnB-ItLAF9vms6K_Wd9s",
                          expect_leaf="personal-finances", sources=_DRIVE_SOURCES, expected_chunks=0)
#: Item 3 (archived non-.md) -- the positive leaf _archive\dedup-2026-09 (the batch-1
#: landing folder; chain [dedup-2026-09, _archive, HJR-Founder-OS] = depth 3 under the
#: direct SA, so the UNCHANGED gate admits it). drive_sweep ONLY: the drive_asset
#: metadata cards are KEPT and counted, and the whole-_archive scope (option B) is OUT
#: -- both are Harrison's open ruling asks.
RB3_ARCHIVE_SPEC = FolderLaneSpec(lane=RB3_ARCHIVE_LANE, item="item 3 (archived non-.md, dedup-2026-09)",
                                  folder_id="1TSUGC4hAHjgbHuExFqf_4-lXyXm7opq5",
                                  expect_leaf="dedup-2026-09", sources=("drive_sweep",))


def _folder_manifest(ctx: RunContext, folder_id: str) -> Path | None:
    return None if ctx.folder_dir is None else ctx.folder_dir / f"purge-cora-internal-folder-{folder_id}.txt"


def select_folder_lane(spec: FolderLaneSpec, conn: sqlite3.Connection, ctx: RunContext) -> LanePlan:
    """The UNCHANGED positive-leaf gate (purge_cora_internal_kb.run_folder_mode, dry-run,
    ID-ONLY, source-restricted) -- it resolves the chain, refuses the root / a shallow
    chain / a leaf mismatch, enumerates the subtree and writes the id-only reviewed
    manifest the apply's gate re-reads. Its always-on static/title passes live in its
    main() and never run here."""
    plan = LanePlan(lane=spec.lane, action="DELETE")
    plan.counts = {"item": spec.item, "folder_id": spec.folder_id, "expect_leaf": spec.expect_leaf,
                   "service": "direct-sa", "sources_selected": list(spec.sources)}
    manifest = _folder_manifest(ctx, spec.folder_id)
    if manifest is None:
        plan.stops.append({"reason": "no folder-manifest directory configured"})
        return plan
    try:
        svc = ctx.drive_service()
    except RuntimeError as exc:
        plan.stops.append({"reason": str(exc)})
        return plan
    try:
        _ids, _f, _c, _ok, sels = _pci().run_folder_mode(
            conn, svc, [spec.folder_id], manifest.parent, apply=False,
            expect_leaf=spec.expect_leaf, sources=spec.sources, names=False)
    except Exception as exc:  # noqa: BLE001 -- the gate refused: the lane STOPS
        plan.stops.append({"reason": f"positive-leaf gate refused: {_refusal_text(exc)}"})
        return plan
    sel = sels[0]
    by, held, files = _plan_rows(conn, plan, sel.chunk_ids, ctx)
    plan.files = len(files)
    plan.counts.update({
        "chain_ids": [fid for _name, fid in sel.chain], "chain_depth": len(sel.chain),
        "complete": sel.complete, "files_enumerated": sel.n_descendants,
        "files_with_selected_chunks": len(sel.hits), "selected_chunks": len(sel.chunk_ids),
        "planned_chunks": len(plan.chunk_ids), "planned_files": plan.files, "held_lex_chunks": held,
        "by_source_entity": by, "kept_by_source": sel.kept,
        "allowlisted_basenames_selected": len(sel.allowlisted),
        "reviewed_manifest": manifest.name,
    })
    if spec.expected_chunks is not None:
        plan.counts["expected_chunks"] = spec.expected_chunks
        plan.counts["matches_expectation"] = len(sel.chunk_ids) == spec.expected_chunks
    if not sel.complete:
        plan.stops.append({"reason": f"the enumeration of {spec.folder_id} did not complete -- the selection "
                                     f"is a FLOOR (the gate refuses it at --apply); re-run the dry-run"})
    if not manifest.exists():
        plan.stops.append({"reason": "the reviewed folder manifest could not be written -- the apply's gate "
                                     "would refuse; re-run the dry-run"})
    return plan


def verify_folder_lane(spec: FolderLaneSpec, conn: sqlite3.Connection, ctx: RunContext,
                       plan: LanePlan) -> list[str]:
    """--apply: the gate again with ``apply=True`` (leaf, depth, complete walk, the
    reviewed manifest covering every selected file -- ``--accept-delta`` passes
    through; it writes nothing), then every PLANNED chunk must still be under the
    folder: a chunk still in the KB but no longer selected moved OUT since the
    dry-run, and deleting it would delete live content."""
    manifest = _folder_manifest(ctx, spec.folder_id)
    if manifest is None:
        return ["no folder-manifest directory configured"]
    try:
        svc = ctx.drive_service()
    except RuntimeError as exc:
        return [str(exc)]
    try:
        _ids, _f, _c, _ok, sels = _pci().run_folder_mode(
            conn, svc, [spec.folder_id], manifest.parent, apply=True, expect_leaf=spec.expect_leaf,
            accept_delta=ctx.accept_delta, sources=spec.sources, names=False)
    except Exception as exc:  # noqa: BLE001
        return [f"positive-leaf gate refused: {_refusal_text(exc)}"]
    current = set(sels[0].chunk_ids)
    gone = [cid for cid in plan.chunk_ids if cid not in current]
    moved = _present(conn, gone) if gone else []
    if moved:
        return [f"{len(moved)} planned chunk(s) are still in the KB but no longer under {spec.folder_id} "
                f"(moved out since the dry-run?) -- re-run the dry-run"]
    return []


# ── rb3_static_old_paths: kb-purge-rows.csv ──────────────────────────────────
CSV_HEADER = ("old_relpath", "new_relpath", "action")
CSV_ACTIONS = ("MOVE", "ARCHIVE")


class CsvRefused(Exception):
    """kb-purge-rows.csv failed a parse rule. The message names the ROW INDEX and the
    rule, never the path text."""


@dataclass(frozen=True)
class PurgeRow:
    index: int          # 1-based data-row number (the header is row 0)
    old: str
    new: str
    action: str


def _check_relpath(index: int, label: str, value: str) -> None:
    if not value:
        raise CsvRefused(f"row {index}: {label} is empty")
    if value != value.strip():
        raise CsvRefused(f"row {index}: {label} has leading/trailing whitespace")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise CsvRefused(f"row {index}: {label} carries a control character")
    if "/" in value:
        raise CsvRefused(f"row {index}: {label} uses '/' -- static_md source_ids are backslash relpaths "
                         f"and are matched EXACTLY (never rewritten)")
    if value.startswith("\\") or (len(value) >= 2 and value[1] == ":"):
        raise CsvRefused(f"row {index}: {label} is absolute -- relpaths under the Founder-OS root only")
    if any(seg in ("", ".", "..") for seg in value.split("\\")):
        raise CsvRefused(f"row {index}: {label} has an empty, '.' or '..' segment")


def parse_purge_rows_csv(path: Path) -> tuple[list[PurgeRow], str]:
    """``(rows, sha256)`` of kb-purge-rows.csv. UTF-8 with or without a BOM, LF or CRLF.
    Refuses (CsvRefused) a header other than exactly old_relpath,new_relpath,action; a
    missing or extra column; an action outside MOVE/ARCHIVE; an absolute path, a '..'
    / '.' / empty segment, a '/' separator, surrounding whitespace or a control
    character; old == new; a duplicate old path; no data rows. Fully blank lines are
    skipped by the csv reader; a row of empty fields is refused."""
    raw = Path(path).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CsvRefused("the CSV is not UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if tuple(reader.fieldnames or ()) != CSV_HEADER:
        raise CsvRefused(f"the header must be exactly {','.join(CSV_HEADER)}")
    rows: list[PurgeRow] = []
    seen: set[str] = set()
    for index, rec in enumerate(reader, 1):
        if None in rec:
            raise CsvRefused(f"row {index}: extra column(s)")
        if any(v is None for v in rec.values()):
            raise CsvRefused(f"row {index}: missing column(s)")
        old, new, action = rec["old_relpath"], rec["new_relpath"], rec["action"]
        if action not in CSV_ACTIONS:
            raise CsvRefused(f"row {index}: action must be one of {'/'.join(CSV_ACTIONS)} (exact)")
        _check_relpath(index, "old_relpath", old)
        _check_relpath(index, "new_relpath", new)
        if old == new:
            raise CsvRefused(f"row {index}: old_relpath == new_relpath")
        if old in seen:
            raise CsvRefused(f"row {index}: duplicate old_relpath")
        seen.add(old)
        rows.append(PurgeRow(index=index, old=old, new=new, action=action))
    if not rows:
        raise CsvRefused("no data rows")
    return rows, digest


def _static_ids(conn: sqlite3.Connection, relpath: str) -> list[str]:
    """static_md chunk ids by EXACT source_id equality (case-sensitive, no LIKE, no
    GLOB) -- ``_shared`` / ``_archive`` would be LIKE wildcards and LIKE ignores case."""
    return [r[0] for r in conn.execute(
        "SELECT chunk_id FROM knowledge_chunks WHERE source='static_md' AND source_id = ?", (relpath,))]


#: Top-level folders every real Founder-OS root carries. The existence gate runs only
#: under a root that has ALL of them (a typo'd / sub-folder / unrelated --founder-root
#: would read every old path as ABSENT), and an absence counts only while they still
#: answer (a mount that went away mid-gate).
FOUNDER_ROOT_ANCHORS = ("00-Founder", "_shared")


def _anchors_present(root: Path) -> bool:
    from cora import drive_io  # noqa: PLC0415
    return all(drive_io.exists(Path(root) / a) and (Path(root) / a).is_dir() for a in FOUNDER_ROOT_ANCHORS)


def _fs_exists(path: Path, root: Path | None = None) -> bool:
    """drive_io.exists, made safe for an ABSENCE verdict. drive_io.exists wraps
    pathlib.Path.exists, which SWALLOWS ENOENT / WinError 21 / 123: a gone drive
    letter returns False WITHOUT raising (DriveUnavailable fires only in hang /
    timeout mode). So a False is re-probed against ``root``'s anchor folders, and
    raises DriveUnavailable (an OSError) when they no longer answer -- "gone" must
    never read as "absent" here."""
    from cora import drive_io  # noqa: PLC0415
    if drive_io.exists(path):
        return True
    if root is not None and not _anchors_present(root):
        raise drive_io.DriveUnavailable("the Founder-OS root's anchor folders stopped answering -- mount gone?")
    return False


def _founder_root_ok(root: Path | None) -> bool:
    """The root exists, is a directory and carries every FOUNDER_ROOT_ANCHORS folder."""
    return root is not None and _fs_exists(root) and Path(root).is_dir() and _anchors_present(root)


def founder_root_digest(root: Path | None) -> str | None:
    """sha256 of the normalised absolute --founder-root (no filesystem access): the
    INTENT records it; an apply under a different root refuses."""
    if root is None:
        return None
    norm = os.path.normcase(os.path.abspath(str(root)))
    return hashlib.sha256(norm.encode("utf-8", "surrogatepass")).hexdigest()


def _hash_store_keys(path: Path | None) -> set[str] | None:
    """READ-ONLY view of the static-md content-hash store's keys (None = unreadable)."""
    if path is None:
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return set(data) if isinstance(data, dict) else None


def static_gate(row: PurgeRow, old_chunks: int, new_chunks: int, old_exists: bool,
                new_exists: bool) -> tuple[str, str]:
    """``(disposition, reason)``: PURGE only when the OLD path is ABSENT under the
    Founder-OS root -- and for a MOVE the NEW path must also be live with >= 1
    static_md row (the file really moved and was re-ingested). Anything else is HELD:
    the static sync re-ingests a file only on an mtime past its watermark or a
    content-hash change, so purging a live file's rows darkens it indefinitely."""
    if old_chunks == 0:
        return "NO_ROWS", "0 static_md rows at the old path (case or separator?) -- nothing to purge"
    if old_exists:
        what = ("the ARCHIVE is not applied (deferred to folder-audit batch 2?)" if row.action == "ARCHIVE"
                else "the MOVE is not applied")
        return "HELD", (f"old path still LIVE on disk -- {what}; purging would darken its only KB copy "
                        f"(the static sync re-ingests only on an mtime past its watermark or a hash change)")
    if row.action == "MOVE":
        if not new_exists:
            return "HELD", "MOVE target absent on disk -- the move cannot be confirmed"
        if new_chunks < 1:
            return "HELD", "MOVE target has no static_md rows yet -- purging now would leave the file dark"
        return "PURGE", "old path absent; the new path is live and re-ingested"
    return "PURGE", "old path absent (archived)"


def select_rb3_static(conn: sqlite3.Connection, ctx: RunContext) -> LanePlan:
    plan = LanePlan(lane=RB3_STATIC_LANE, action="DELETE")
    plan.counts = {"item": "item 3 (kb-purge-rows.csv static_md old paths)"}
    if ctx.csv_path is None:
        plan.stops.append({"reason": "no --csv given -- this lane needs kb-purge-rows.csv"})
        return plan
    try:
        rows, digest = parse_purge_rows_csv(ctx.csv_path)
    except CsvRefused as exc:
        plan.stops.append({"reason": f"CSV refused: {exc}"})
        return plan
    except OSError as exc:
        plan.stops.append({"reason": f"CSV unreadable ({type(exc).__name__})"})
        return plan
    plan.counts.update({"csv_name": Path(ctx.csv_path).name, "csv_sha256": digest, "csv_rows": len(rows)})
    try:
        root_ok = _founder_root_ok(ctx.founder_root)
    except OSError as exc:
        plan.stops.append({"reason": f"Founder-OS mount unavailable ({type(exc).__name__}) -- the existence "
                                     f"gate cannot run"})
        return plan
    if not root_ok:
        plan.stops.append({"reason": f"the Founder-OS root is missing, not a directory, or lacks its anchor "
                                     f"folders ({', '.join(FOUNDER_ROOT_ANCHORS)}) -- the existence gate cannot "
                                     f"run (a missing or wrong root would read every old path as absent)"})
        return plan
    plan.counts["founder_root_sha256"] = founder_root_digest(ctx.founder_root)
    keys = _hash_store_keys(ctx.hash_store)
    records: list[dict[str, Any]] = []
    purge_ids: list[str] = []
    try:
        for row in rows:
            old_ids = _static_ids(conn, row.old)
            new_n = len(_static_ids(conn, row.new))
            old_exists = _fs_exists(Path(ctx.founder_root) / row.old, ctx.founder_root)
            new_exists = _fs_exists(Path(ctx.founder_root) / row.new, ctx.founder_root)
            disposition, reason = static_gate(row, len(old_ids), new_n, old_exists, new_exists)
            records.append({
                "row": row.index, "action": row.action,
                "old_sha256_16": _sha16(row.old), "new_sha256_16": _sha16(row.new),
                "old_exists": old_exists, "new_exists": new_exists,
                "old_chunks": len(old_ids), "new_chunks": new_n,
                "hash_store_key_present": None if keys is None else row.old in keys,
                "disposition": disposition, "reason": reason,
            })
            if disposition == "PURGE":
                purge_ids.extend(old_ids)
            elif disposition == "HELD":
                plan.holds.append({"row": row.index, "action": row.action, "chunks": len(old_ids),
                                   "chunk_ids": sorted(old_ids), "reason": reason})
    except OSError as exc:
        plan.stops.append({"reason": f"Founder-OS mount unavailable mid-gate ({type(exc).__name__})"})
        return plan
    by, held_lex, files = _plan_rows(conn, plan, purge_ids, ctx)
    plan.files = len(files)
    plan.counts.update({
        "rows": records, "by_disposition": {d: sum(1 for r in records if r["disposition"] == d)
                                            for d in ("PURGE", "HELD", "NO_ROWS")},
        "planned_chunks": len(plan.chunk_ids), "held_row_chunks": sum(r["old_chunks"] for r in records
                                                                     if r["disposition"] == "HELD"),
        "held_lex_chunks": held_lex, "by_source_entity": by,
        "hash_store_readable": keys is not None,
    })
    return plan


def verify_rb3_static(conn: sqlite3.Connection, ctx: RunContext, plan: LanePlan) -> list[str]:
    """--apply: the SAME CSV (sha256) and the existence gate again for every PURGE row
    (old still absent; a MOVE's target still live with rows) -- a file that came back
    between the dry-run and the stop window is never darkened."""
    if ctx.csv_path is None:
        return ["--csv is required at --apply for this lane (its existence gate is re-run)"]
    try:
        rows, digest = parse_purge_rows_csv(ctx.csv_path)
    except (CsvRefused, OSError) as exc:
        return [f"CSV refused/unreadable at apply ({type(exc).__name__})"]
    if digest != plan.counts.get("csv_sha256"):
        return ["the CSV changed since the dry-run (sha256 differs) -- re-run the dry-run"]
    if founder_root_digest(ctx.founder_root) != plan.counts.get("founder_root_sha256"):
        return ["the --founder-root differs from the dry-run's (digest mismatch) -- apply with the SAME root, "
                "or re-run the dry-run"]
    try:
        if not _founder_root_ok(ctx.founder_root):
            return [f"the Founder-OS root is missing, not a directory, or lacks its anchor folders "
                    f"({', '.join(FOUNDER_ROOT_ANCHORS)})"]
        by_index = {r.index: r for r in rows}
        out: list[str] = []
        selectable: set[str] = set()
        for rec in plan.counts.get("rows") or []:
            if rec.get("disposition") != "PURGE":
                continue
            row = by_index.get(rec.get("row"))
            if row is None:
                out.append(f"row {rec.get('row')}: not in the CSV")
                continue
            selectable.update(_static_ids(conn, row.old))
            if _fs_exists(Path(ctx.founder_root) / row.old, ctx.founder_root):
                out.append(f"row {row.index}: the old path is LIVE again")
            if row.action == "MOVE":
                if not _fs_exists(Path(ctx.founder_root) / row.new, ctx.founder_root):
                    out.append(f"row {row.index}: the MOVE target is gone")
                elif not _static_ids(conn, row.new):
                    out.append(f"row {row.index}: the MOVE target has no static_md rows")
        out.extend(_not_selectable(conn, plan, selectable))
        return out
    except OSError as exc:
        return [f"Founder-OS mount unavailable ({type(exc).__name__})"]


def _not_selectable(conn: sqlite3.Connection, plan: LanePlan, selectable: set[str]) -> list[str]:
    """Every planned chunk must still be one the lane's selector picks NOW. A chunk
    that VANISHED is the drift gate's (``--accept-delta``); one that still exists but
    is no longer selectable -- or never was this lane's (an edited INTENT) -- refuses."""
    stray = _present(conn, [cid for cid in plan.chunk_ids if cid not in selectable])
    return [f"{len(stray)} planned chunk(s) are in the KB but not selectable by {plan.lane} now -- "
            f"re-run the dry-run"] if stray else []


# ── rb4_desktop_ini ──────────────────────────────────────────────────────────
def _desktop_ini_ids(conn: sqlite3.Connection) -> tuple[int, list[str]]:
    """``(prefilter_rows, chunk_ids)``: ``instr`` narrows (an exact substring, never
    LIKE); kb_exclusions.is_os_junk_filename decides -- on the TITLE for the
    Drive-copy sources, on the source_id basename for static_md."""
    from cora.kb_exclusions import is_os_junk_filename  # noqa: PLC0415
    prefilter = 0
    ids: list[str] = []
    for cid, source, source_id, title in conn.execute(
            "SELECT chunk_id, source, source_id, title FROM knowledge_chunks WHERE "
            "(source IN ('drive_sweep','drive_asset') AND instr(lower(coalesce(title,'')), 'desktop.ini') > 0) "
            "OR (source = 'static_md' AND instr(lower(coalesce(source_id,'')), 'desktop.ini') > 0)"):
        prefilter += 1
        if is_os_junk_filename(title if source in _DRIVE_SOURCES else source_id):
            ids.append(cid)
    return prefilter, ids


def verify_rb4_desktop_ini(conn: sqlite3.Connection, ctx: RunContext, plan: LanePlan) -> list[str]:
    return _not_selectable(conn, plan, set(_desktop_ini_ids(conn)[1]))


def select_rb4_desktop_ini(conn: sqlite3.Connection, ctx: RunContext) -> LanePlan:
    """desktop.ini rows in the Drive-copy sources (by title) and static_md (by the
    source_id basename). ``instr`` narrows (an exact substring -- never LIKE);
    kb_exclusions.is_os_junk_filename decides. Measured 0 on 2026-09-24."""
    plan = LanePlan(lane=RB4_LANE, action="DELETE")
    prefilter, ids = _desktop_ini_ids(conn)
    by, held, files = _plan_rows(conn, plan, ids, ctx)
    plan.files = len(files)
    plan.counts = {"item": "item 4 (desktop.ini)", "prefilter_rows": prefilter, "matched_chunks": len(ids),
                   "planned_chunks": len(plan.chunk_ids), "held_lex_chunks": held, "by_source_entity": by,
                   "expected_chunks": 0, "matches_expectation": len(ids) == 0}
    return plan


# ── rb8_ufl_equity ───────────────────────────────────────────────────────────
#: Item 8: EXACTLY these three Drive file ids (a constant -- never CLI input) and their
#: drive_sweep chunk counts as measured read-only on 2026-09-24. A count that drifts
#: STOPS the lane until someone re-reviews and edits this constant (a reviewed diff).
#: drive_asset rows of the same ids are KEPT (the pdf's card is a visibility-binder row,
#: out of scope; the two xlsx cards are metadata only).
RB8_UFL_EXPECTED: dict[str, int] = {
    "10EP-1JvWE99n7yfKxP_GsO4cBrIR6etc": 25,
    "1F76_2VshuRep2BIc2ft2uhQy2dcaVWmJ": 25,
    "1m_cstl-LjxnLNIxjfHvIFR3xtoBHgb0_": 10,
}
RB8_ANCESTOR_NAME = "04-UFL"
RB8_FLAT_WATERMARK_KEY = "drive_sweep_harrison@hjrglobal.com"
_RB8_MAX_HOPS = 30


def _iso(ts: int | float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()


def _parse_rfc3339(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _rb8_rows(conn: sqlite3.Connection, file_id: str) -> list[tuple[str, str]]:
    """``[(chunk_id, source)]`` of the Drive-copy rows keyed on this file id: the bare
    id or the legacy ``<id>:chunkN`` form (GLOB-escaped; never LIKE)."""
    return [(r[0], r[1]) for r in conn.execute(
        "SELECT chunk_id, source FROM knowledge_chunks WHERE source IN ('drive_sweep','drive_asset') "
        "AND (source_id = ? OR source_id GLOB ?)", (file_id, _glob_escape(file_id) + ":*"))]


def rb8_positive_gate(svc: Any, file_id: str) -> dict[str, Any]:
    """Read-only files.get: the id must be a NON-folder, NOT trashed, and sit under a
    folder named exactly ``04-UFL`` inside HJR-Founder-OS. Names are compared, never
    recorded; the result is booleans + the modifiedTime."""
    from cora.connectors import drive_sweep as ds  # noqa: PLC0415
    out: dict[str, Any] = {"ok": False, "why": "", "modified_time": None,
                           "in_04_ufl": False, "under_founders_os": False}
    meta = ds._retry_execute(svc.files().get(fileId=file_id, fields="id,mimeType,trashed,parents,modifiedTime"))
    out["modified_time"] = meta.get("modifiedTime")
    if meta.get("mimeType") == _FOLDER_MIME:
        out["why"] = "is a folder"
        return out
    if meta.get("trashed"):
        out["why"] = "is trashed"
        return out
    parents = list(meta.get("parents") or [])
    seen: set[str] = set()
    for _hop in range(_RB8_MAX_HOPS):
        if not parents or parents[0] in seen:
            break
        fid = parents[0]
        seen.add(fid)
        if fid == ds.FOUNDERS_OS_ROOT_ID:
            out["under_founders_os"] = True
        pm = ds._retry_execute(svc.files().get(fileId=fid, fields="id,name,parents"))
        if str(pm.get("name") or "") == RB8_ANCESTOR_NAME:
            out["in_04_ufl"] = True
        parents = list(pm.get("parents") or [])
    out["ok"] = bool(out["in_04_ufl"] and out["under_founders_os"])
    if not out["ok"]:
        out["why"] = "no 04-UFL ancestor inside HJR-Founder-OS"
    return out


def _flat_watermarks(conn: sqlite3.Connection) -> dict[str, int]:
    return {str(k): int(v) for k, v in conn.execute(
        "SELECT source, last_sync_at FROM sync_state WHERE source GLOB 'drive_sweep_*@*' "
        "AND source NOT GLOB 'drive_sweep_checkpoint_*'")}


def select_rb8_ufl(conn: sqlite3.Connection, ctx: RunContext) -> LanePlan:
    plan = LanePlan(lane=RB8_LANE, action="DELETE")
    try:
        flat = _flat_watermarks(conn)
        fos_ufl = [int(r[0]) for r in conn.execute(
            "SELECT last_sync_at FROM sync_state WHERE source GLOB 'founders_os_UFL_*'")]
    except sqlite3.Error as exc:
        flat, fos_ufl = {}, []
        plan.stops.append({"reason": f"sync_state unreadable ({type(exc).__name__}) -- the re-ingest "
                                     f"analysis cannot run"})
    flat_wm = flat.get(RB8_FLAT_WATERMARK_KEY)
    fos_wm = max(fos_ufl) if fos_ufl else None
    per_file: list[dict[str, Any]] = []
    ids: list[str] = []
    try:
        svc = ctx.drive_service()
    except RuntimeError as exc:
        svc = None
        plan.stops.append({"reason": str(exc)})
    for file_id, expected in RB8_UFL_EXPECTED.items():
        rows = _rb8_rows(conn, file_id)
        sweep_ids = sorted(c for c, s in rows if s == "drive_sweep")
        rec: dict[str, Any] = {"file_id": file_id, "drive_sweep_chunks": len(sweep_ids),
                               "expected_chunks": expected,
                               "drive_asset_kept": sum(1 for _c, s in rows if s == "drive_asset")}
        per_file.append(rec)
        if svc is None:
            continue
        try:
            gate = rb8_positive_gate(svc, file_id)
        except Exception as exc:  # noqa: BLE001 -- recorded by type; the lane STOPS
            gate = {"ok": False, "why": f"files.get failed ({type(exc).__name__})", "modified_time": None,
                    "in_04_ufl": False, "under_founders_os": False}
        mtime = _parse_rfc3339(gate.get("modified_time"))
        flat_next = flat_wm is None or mtime is None or mtime > flat_wm
        rec.update({"positive_gate": gate["ok"], "gate_why": gate["why"],
                    "modified_time": gate.get("modified_time"), "in_04_ufl": gate["in_04_ufl"],
                    "under_founders_os": gate["under_founders_os"],
                    "flat_next_pass_reingests": flat_next,
                    "other_flat_watermarks_below_mtime": (
                        None if mtime is None else sum(1 for k, v in flat.items()
                                                       if k != RB8_FLAT_WATERMARK_KEY and v < mtime)),
                    "tree_walk_reingests_when_reached": fos_wm is None or mtime is None or mtime > fos_wm})
        if not gate["ok"]:
            plan.stops.append({"reason": f"{file_id}: positive gate failed ({gate['why']})"})
            continue
        if len(sweep_ids) != expected:
            plan.stops.append({"reason": f"{file_id}: drive_sweep chunk count drift (expected {expected}, "
                                         f"found {len(sweep_ids)}) -- re-review, then update RB8_UFL_EXPECTED"})
            continue
        if flat_next:
            plan.stops.append({"reason": f"{file_id}: the flat sweep's NEXT pass would re-ingest it (modifiedTime "
                                         f"past the watermark, or no watermark) -- the spec says say so and stop"})
            continue
        ids.extend(sweep_ids)
    by, held, files = _plan_rows(conn, plan, ids, ctx)
    plan.files = len(files)
    plan.counts = {
        "item": "item 8 (UFL equity files, drive_sweep rows only)", "per_file": per_file,
        "planned_chunks": len(plan.chunk_ids), "held_lex_chunks": held, "by_source_entity": by,
        "flat_watermark_utc": _iso(flat_wm), "founders_os_ufl_watermark_utc": _iso(fos_wm),
        "reingest_analysis": {
            "flat_sweep_next_pass": ("NO -- every file's modifiedTime is before the flat-sweep watermark"
                                     if per_file and all(r.get("flat_next_pass_reingests") is False for r in per_file)
                                     else "YES or UNKNOWN for at least one file -- see per_file"),
            "tree_walk": ("LATENT -- sweep_founders_os writes the same source='drive_sweep' and has NO UFL "
                          "watermark (cutoff = now - 730 days): it re-ingests every file when the walk first "
                          "reaches 04-UFL (deferred behind the budget-interrupted LEX subtree today)"
                          if fos_wm is None else "a UFL watermark exists -- see tree_walk_reingests_when_reached"),
            "tombstone": "NONE -- no per-file tombstone exists; any modifiedTime change re-ingests the file",
            "exclusion_built": False,
        },
    }
    return plan


def verify_rb8_ufl(conn: sqlite3.Connection, ctx: RunContext, plan: LanePlan) -> list[str]:
    """--apply: the positive gate and the flat-sweep next-pass check again, per id."""
    try:
        svc = ctx.drive_service()
    except RuntimeError as exc:
        return [str(exc)]
    try:
        flat_wm = _flat_watermarks(conn).get(RB8_FLAT_WATERMARK_KEY)
    except sqlite3.Error as exc:
        return [f"sync_state unreadable ({type(exc).__name__})"]
    planned = set(plan.chunk_ids)
    out: list[str] = []
    rows_by_id = {fid: [c for c, s in _rb8_rows(conn, fid) if s == "drive_sweep"] for fid in RB8_UFL_EXPECTED}
    out.extend(_not_selectable(conn, plan, {c for ids in rows_by_id.values() for c in ids}))
    for file_id, sweep_ids in rows_by_id.items():
        if not any(c in planned for c in sweep_ids):
            continue
        try:
            gate = rb8_positive_gate(svc, file_id)
        except Exception as exc:  # noqa: BLE001
            out.append(f"{file_id}: files.get failed ({type(exc).__name__})")
            continue
        if not gate["ok"]:
            out.append(f"{file_id}: positive gate failed ({gate['why']})")
        mtime = _parse_rfc3339(gate.get("modified_time"))
        if flat_wm is None or mtime is None or mtime > flat_wm:
            out.append(f"{file_id}: the flat sweep's next pass would now re-ingest it")
    return out


register_lane(Lane(
    name=RB2_LANE, action="DELETE", select=partial(select_folder_lane, RB2_SPEC),
    verify=partial(verify_folder_lane, RB2_SPEC),
    description="RIDER B item 2: 00-Founder/personal-finances (pinned) -- folder mode, expected 0 (applied no-op)",
))
register_lane(Lane(
    name=RB3_STATIC_LANE, action="DELETE", select=select_rb3_static, verify=verify_rb3_static,
    description="RIDER B item 3: static_md rows of kb-purge-rows.csv old paths (existence-gated)",
))
register_lane(Lane(
    name=RB3_ARCHIVE_LANE, action="DELETE", select=partial(select_folder_lane, RB3_ARCHIVE_SPEC),
    verify=partial(verify_folder_lane, RB3_ARCHIVE_SPEC),
    description="RIDER B item 3: archived non-.md under _archive/dedup-2026-09 -- drive_sweep rows only",
))
register_lane(Lane(
    name=RB4_LANE, action="DELETE", select=select_rb4_desktop_ini, verify=verify_rb4_desktop_ini,
    description="RIDER B item 4: desktop.ini rows (expected 0)",
))
register_lane(Lane(
    name=RB8_LANE, action="DELETE", select=select_rb8_ufl, verify=verify_rb8_ufl,
    description="RIDER B item 8: three UFL equity files by id -- drive_sweep rows only",
))

RB_RESIDUALS = [
    "rb3_static_old_paths: the static-md content-hash store keeps the purged old paths' keys (this script "
    "never writes data/state) -- harmless while those paths stay absent.",
    "rb3_static_old_paths: a HELD row (its move/archive not applied yet -- e.g. row 1, deferred to folder-audit "
    "batch 2) needs a follow-up purge after the move lands.",
    "rb3_archived_nonmd: drive_asset metadata cards under dedup-2026-09 are KEPT (counted); the whole-_archive "
    "scope (option B: the _archive/_shared .md twins, the file directly under _archive) and drive_asset "
    "deletion are OUT pending Harrison's ruling.",
    "rb3_archived_nonmd: lossless is NOT verified -- keep-copy KB coverage of the archived duplicates cannot be "
    "proven by title; this purge may remove the only KB copy of a file's content.",
    "RIDER B lanes HOLD LEX-partition rows unless --release-lex <lane> is passed on the dry-run AND the apply.",
    "rb8_ufl_equity: the three files' drive_asset cards are KEPT; no tombstone exists -- a modifiedTime change "
    "re-ingests them through the flat sweep, and the tree walk (no UFL watermark) re-ingests all three when it "
    "first reaches 04-UFL. No exclusion was built (spec).",
    "rb2_personal_finances / rb4_desktop_ini: 0 chunks selected = a recorded no-op; the pin and the filename "
    "belt are the forward protection.",
]


# ── planning ──────────────────────────────────────────────────────────────────
def build_plans(conn: sqlite3.Connection, ctx: RunContext, lanes: list[str]) -> dict[str, LanePlan]:
    plans: dict[str, LanePlan] = {}
    for name in lanes:
        lane = LANES[name]
        try:
            plan = lane.select(conn, ctx)
        except Exception as exc:  # noqa: BLE001 -- a selector error STOPS its lane, loudly
            log.error("lane %s selector failed (%s) -- lane STOPPED", name, type(exc).__name__)
            plan = LanePlan(lane=name, action=lane.action,
                            stops=[{"reason": f"selector error: {type(exc).__name__}"}])
        if plan.lane != name or plan.action != lane.action:
            plan.stops.append({"reason": "selector returned a plan for another lane/action"})
        plans[name] = plan
    return plans


def union_summary(plans: list[LanePlan]) -> dict[str, Any]:
    delete_ids: set[str] = set()
    redact_ids: set[str] = set()
    files = 0
    for p in plans:
        if p.action == "DELETE":
            delete_ids.update(p.chunk_ids)
            files += int(p.files or 0)
        else:
            redact_ids.update(p.chunk_ids)
    overlap = redact_ids & delete_ids
    return {
        "delete_chunks": len(delete_ids), "delete_files": files,
        "redact_chunks": len(redact_ids), "redact_also_deleted": len(overlap),
        "is_large": len(delete_ids) > LARGE_CHUNKS or files > LARGE_FILES,
        "large_thresholds": {"chunks": LARGE_CHUNKS, "files": LARGE_FILES},
    }


# ── heartbeat (Cora-stopped proof) ────────────────────────────────────────────
def heartbeat_evidence(path: Path | None = None) -> dict[str, Any]:
    """Cora is STOPPED only when the heartbeat file EXISTS and both its content
    stamp and its mtime are older than 300 s. Missing / unparseable = NOT proven
    stopped (fail closed; the older ``live_bot_is_fresh`` helpers read a missing
    file as "stopped")."""
    from cora import health_endpoint
    hb = Path(path) if path is not None else Path(health_endpoint.HEARTBEAT_FILE)
    ev: dict[str, Any] = {"path": str(hb), "exists": hb.exists(), "content_age_s": None,
                          "mtime_age_s": None, "stopped": False}
    if not ev["exists"]:
        ev["why"] = "heartbeat file missing -- cannot prove Cora is stopped"
        return ev
    content_age = health_endpoint.heartbeat_age_seconds(hb)
    try:
        mtime_age = time.time() - hb.stat().st_mtime
    except OSError:
        mtime_age = None
    ev["content_age_s"] = None if content_age is None else round(content_age, 1)
    ev["mtime_age_s"] = None if mtime_age is None else round(mtime_age, 1)
    if content_age is None or mtime_age is None:
        ev["why"] = "heartbeat unparseable -- cannot prove Cora is stopped"
        return ev
    ev["stopped"] = min(content_age, mtime_age) > HEARTBEAT_STOPPED_S
    ev["why"] = "stale by both clocks" if ev["stopped"] else "heartbeat FRESH -- Cora looks live"
    return ev


# ── records (ids/counts only; self-scanned before they are written) ───────────
RESIDUALS = [
    "Not durable against a re-ingest: a drive_sweep row is rewritten from its Drive source "
    "file when that file's modifiedTime changes -- the egress belt at every chunk renderer "
    "still redacts it; making it durable needs the source edited (or ingest-time redaction).",
    "KB backups (data/cora_kb.db.bak-*) and any pre-apply copy taken in the stop window still "
    "hold the pre-redaction text -- rotating/deleting them is Harrison's call.",
    "Vectors are NOT re-embedded: the stored embedding was computed from the token-bearing text "
    "(an embedding is not invertible to it).",
    "Count-only surfaces (metadata, deep_link, source_id, author, every other KB table) are "
    "reported, never rewritten.",
    "LEX-partition rows are HELD unless the dry-run ran with --release-lex <lane> (and the apply "
    "repeats it).",
    "WAL: a pre-redaction page image can persist in the -wal file until a checkpoint resets it; "
    "secure_delete zeroes freed pages of the main file only.",
    "LARGE gate: a Cora started less than 60 s before the write lock has not written its first "
    "heartbeat yet, so the in-lock re-check cannot see it -- the stop window's disabled tasks and "
    "empty process listing are the gate for that window.",
    "Out of this script's reach: the G: session-capture notes, the Claude Desktop Cowork store "
    "and the ~/.claude Code transcripts keep any raw token they hold -- and redaction never "
    "un-leaks a secret: the credentials themselves must be rotated / revoked.",
]


def _secret_values() -> dict[str, str]:
    try:
        return secrets_scan.live_secret_values()
    except Exception:  # noqa: BLE001
        return {}


def record_leaks(text: str) -> list[str]:
    """Self-scan of a record before it is written: shape names / key names only."""
    found: list[str] = []
    if secret_tokens.has_secret_token(text):
        found.append("secret_tokens-shape")
    for h in secrets_scan.scan_text(text, "kb-purge-record", env_keys=set(),
                                    live_values=_secret_values()):
        found.append(f"{h.kind}:{h.key}")
    return found


def _stamp() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y%m%dT%H%M%S") + f"-{now.microsecond:06d}Z"


def _new_record_path(out_dir: Path, kind: str, stamp: str) -> Path:
    """An EXCLUSIVELY created ``kb-purge-code15-<kind>-<stamp>[-n].json`` -- a record is
    never overwritten (two dry-runs in one tick must not replace the reviewed INTENT)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for i in range(1, 100):
        p = out_dir / f"kb-purge-code15-{kind}-{stamp}{'' if i == 1 else f'-{i}'}.json"
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue
        os.close(fd)
        return p
    raise Refused(f"could not allocate a unique {kind} record name")


def render_intent_txt(payload: dict[str, Any]) -> str:
    lines = [f"Code #15 combined KB purge -- INTENT  {payload['generated_at']}",
             f"db: {payload['db']}",
             f"LEX released for: {', '.join(payload['release_lex']) or '(none)'}",
             f"union: {json.dumps(payload['union'], sort_keys=True)}",
             f"heartbeat at dry-run: {json.dumps(payload['heartbeat'], sort_keys=True)}", ""]
    for name, p in payload["lanes"].items():
        lines.append(f"== lane {name}  action={p['action']}  chunks={len(p['chunk_ids'])}  "
                     f"files={p['files']}  holds={len(p['holds'])}  stops={len(p['stops'])}")
        lines.append(f"   counts: {json.dumps(p['counts'], sort_keys=True)}")
        for cid in p["chunk_ids"]:
            lines.append(f"   {p['action']:<6} {cid}")
        for h in p["holds"]:
            ref = h.get("chunk_id") or (f"row {h['row']} ({h.get('chunks', 0)} chunk(s))" if "row" in h else "")
            lines.append(f"   HOLD   {ref}  {h.get('reason', '')}")
        for s in p["stops"]:
            lines.append(f"   STOP   {s.get('chunk_id', '')}  {s.get('reason', '')}")
        lines.append("")
    lines.append("Residuals:")
    lines.extend(f"  - {r}" for r in payload["residuals"])
    lines.append("")
    lines.append("Apply: --apply --manifest <this .json> [--lanes a,b] "
                 "[--release-lex <lane> (must repeat the dry-run's release)]")
    return "\n".join(lines) + "\n"


def write_intent(out_dir: Path, payload: dict[str, Any]) -> Path:
    body = json.dumps(payload, indent=1, ensure_ascii=False)
    txt = render_intent_txt(payload)
    leaks = record_leaks(body) + record_leaks(txt)
    if leaks:
        raise Refused(f"INTENT self-scan tripped ({sorted(set(leaks))}) -- nothing written")
    path = _new_record_path(out_dir, "INTENT", payload["stamp"])
    path.write_text(body, encoding="utf-8")
    path.with_suffix(".txt").write_text(txt, encoding="utf-8")
    return path


def write_applied(out_dir: Path, payload: dict[str, Any]) -> Path:
    body = json.dumps(payload, indent=1, ensure_ascii=False)
    leaks = record_leaks(body)
    if leaks:
        raise Refused(f"APPLIED self-scan tripped ({sorted(set(leaks))}) -- record withheld")
    path = _new_record_path(out_dir, "APPLIED", _stamp())
    path.write_text(body, encoding="utf-8")
    return path


# ── dry-run ───────────────────────────────────────────────────────────────────
def _folder_dir(out_dir: Path, stamp: str) -> Path:
    """The folder lanes' id-only reviewed manifests for ONE INTENT, keyed by its stamp
    (never shared with purge_cora_internal_kb.py's own logs/ manifests)."""
    return Path(out_dir) / f"kb-purge-code15-folders-{stamp}"


def run_dry(db: Path, out_dir: Path, lanes: list[str], release_lex: frozenset[str],
            heartbeat_path: Path | None = None, *, csv_path: Path | None = None,
            founder_root: Path | None = None, hash_store: Path | None = None,
            drive_factory: Callable[[], Any] | None = None) -> Path:
    unknown = sorted(set(release_lex) - set(lanes))
    if unknown:
        raise Refused(f"--release-lex names lane(s) not being planned: {unknown}")
    stamp = _stamp()
    ctx = RunContext(release_lex=frozenset(release_lex), csv_path=csv_path, founder_root=founder_root,
                     hash_store=hash_store, folder_dir=_folder_dir(out_dir, stamp), drive_factory=drive_factory)
    conn = kb_archive.connect_ro(db)      # mode=ro + query_only: the dry-run CANNOT write (D-290)
    try:
        plans = build_plans(conn, ctx, lanes)
    finally:
        conn.close()
    hb = heartbeat_evidence(heartbeat_path)
    payload = {
        "kind": "INTENT", "script": SCRIPT_ID, "version": INTENT_VERSION, "stamp": stamp,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db": str(Path(db).resolve()),
        "release_lex": sorted(ctx.release_lex),
        "lanes": {name: asdict(p) for name, p in plans.items()},
        "union": union_summary(list(plans.values())),
        "heartbeat": {k: hb[k] for k in ("exists", "content_age_s", "mtime_age_s", "stopped")},
        "folder_manifest_dir": ctx.folder_dir.name,
        "residuals": RESIDUALS + RB_RESIDUALS,
    }
    path = write_intent(out_dir, payload)
    for name, p in plans.items():
        log.info("lane %s: action=%s chunks=%d holds=%d stops=%d files=%d", name, p.action,
                 len(p.chunk_ids), len(p.holds), len(p.stops), p.files)
    log.info("union: %s", json.dumps(payload["union"], sort_keys=True))
    log.info("INTENT written -> %s (+ .txt). Review it, then --apply --manifest %s", path, path)
    return path


# ── apply ─────────────────────────────────────────────────────────────────────
def load_intent(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Refused(f"manifest unreadable: {type(exc).__name__}") from exc
    if not isinstance(data, dict) or data.get("kind") != "INTENT" or data.get("script") != SCRIPT_ID:
        raise Refused("manifest is not an INTENT written by this script")
    if not isinstance(data.get("lanes"), dict):
        raise Refused("manifest carries no lanes")
    return data


def _plan_from(d: dict[str, Any]) -> LanePlan:
    return LanePlan(**{k: v for k, v in d.items() if k in LanePlan.__dataclass_fields__})


def run_apply(db: Path, out_dir: Path, manifest: Path, *, lanes: list[str] | None,
              release_lex: frozenset[str], accept_delta: bool, allow_live: bool,
              heartbeat_path: Path | None = None, csv_path: Path | None = None,
              founder_root: Path | None = None, hash_store: Path | None = None,
              drive_factory: Callable[[], Any] | None = None) -> Path:
    data = load_intent(manifest)
    if str(Path(data.get("db", "")).resolve()) != str(Path(db).resolve()):
        raise Refused("manifest was written for a different --db")
    selected = list(lanes) if lanes else list(data["lanes"])
    missing_lanes = [n for n in selected if n not in data["lanes"]]
    if missing_lanes:
        raise Refused(f"lane(s) not in the manifest: {missing_lanes}")
    unregistered = [n for n in selected if n not in LANES]
    if unregistered:
        raise Refused(f"lane(s) not registered in this script: {unregistered}")
    plans = {n: _plan_from(data["lanes"][n]) for n in selected}
    for n, p in plans.items():
        if p.stops:
            raise Refused(f"lane {n} has {len(p.stops)} stop(s) -- deselect it with --lanes")
        if p.action != LANES[n].action:
            raise Refused(f"lane {n}: manifest action {p.action} != code action {LANES[n].action}")
    intent_released = set(data.get("release_lex") or [])
    for n in selected:
        if n in intent_released and n not in release_lex:
            raise Refused(f"the INTENT released LEX rows for lane {n}; repeat --release-lex {n} to apply them")
    not_recorded = sorted(set(release_lex) - intent_released)
    if not_recorded:
        raise Refused(f"--release-lex {not_recorded} is not recorded in the INTENT -- re-run the dry-run with it")

    union = union_summary(list(plans.values()))
    hb = heartbeat_evidence(heartbeat_path)
    if union["is_large"] and not hb["stopped"] and not allow_live:
        raise Refused(f"LARGE apply ({union['delete_chunks']} chunks / {union['delete_files']} files in "
                      f"DELETE lanes; D-087 >{LARGE_CHUNKS}/>{LARGE_FILES}) needs Cora STOPPED: "
                      f"{hb.get('why')} -- run inside the stop window, or pass --allow-live")

    all_ids = sorted({cid for p in plans.values() for cid in p.chunk_ids})
    ctx = RunContext(release_lex=frozenset(release_lex), csv_path=csv_path, founder_root=founder_root,
                     hash_store=hash_store, accept_delta=accept_delta, drive_factory=drive_factory,
                     folder_dir=_folder_dir(Path(manifest).resolve().parent, str(data.get("stamp") or "")))
    # Drift gate + the LEX partition gate + every lane's re-verification on a
    # READ-ONLY handle first: a refusal must precede every write (connect_rw alone
    # issues writer pragmas).
    ro = kb_archive.connect_ro(db)
    try:
        lex_refusals, _ = _partition_gate(ro, plans, frozenset(release_lex))
        if lex_refusals:
            raise Refused("; ".join(lex_refusals) + " -- nothing written; re-run the dry-run")
        verified = _verify_lanes(ro, ctx, plans)
        vanished, drifted = _drift(ro, plans, all_ids)
    finally:
        ro.close()
    if (vanished or drifted) and not accept_delta:
        raise Refused(f"{len(drifted)} chunk(s) changed and {len(vanished)} vanished since the dry-run "
                      "-- re-run the dry-run, or pass --accept-delta")

    conn = kb_archive.connect_rw(db)      # vec0 loaded: the DELETE cascade really deletes
    try:
        conn.execute("PRAGMA secure_delete=ON")
        conn.execute("BEGIN IMMEDIATE")   # ONE transaction: re-verify, redact, delete, commit
        # The LARGE gate again, UNDER the write lock: the first reading precedes the
        # lanes' re-verification (the folder lanes re-walk Drive for minutes), so a
        # Cora that came back meanwhile is only seen here. (A bot started < 60 s ago
        # has not written its first heartbeat yet -- the runbook's disabled tasks +
        # empty process listing cover that window, not this file.)
        hb_lock = heartbeat_evidence(heartbeat_path)
        if union["is_large"] and not hb_lock["stopped"] and not allow_live:
            conn.rollback()
            raise Refused(f"LARGE apply: Cora no longer looks STOPPED at the write lock ({hb_lock.get('why')}) "
                          f"-- nothing written; stop Cora and re-run the apply, or pass --allow-live")
        v2, d2 = _drift(conn, plans, all_ids)
        if (set(v2), set(d2)) != (set(vanished), set(drifted)):
            conn.rollback()
            raise Refused("the KB changed between the drift check and the write lock -- nothing written; "
                          "re-run the apply")
        lex_refusals, lex_released = _partition_gate(conn, plans, frozenset(release_lex))
        if lex_refusals:
            conn.rollback()
            raise Refused("; ".join(lex_refusals) + " (at the write lock) -- nothing written; re-run the dry-run")
        vanished_set = set(vanished)
        delete_set = {cid for p in plans.values() if p.action == "DELETE" for cid in p.chunk_ids} - vanished_set
        outcome: dict[str, Any] = {}
        try:
            for n, p in plans.items():
                if p.action == "REDACT":
                    outcome[n] = _redact_lane(conn, LANES[n], p, delete_set, vanished_set)
                else:
                    outcome[n] = {"action": "DELETE", "planned": len(p.chunk_ids),
                                  "vanished": len(set(p.chunk_ids) & vanished_set), "holds": len(p.holds)}
            if delete_set:
                # kb_archive's cascade (vectors first, the row last) COMMITS the whole transaction.
                totals = kb_archive.delete_chunks(conn, sorted(delete_set))
                outcome["_delete_union"] = {"deleted_ids": len(delete_set), "table_totals": totals}
            else:
                conn.commit()
        except Exception as exc:  # noqa: BLE001 -- nothing half-written: roll the transaction back
            conn.rollback()
            raise Refused(f"apply transaction rolled back ({type(exc).__name__}) -- nothing written") from exc
        # Post-apply re-scan, from what is now in the KB.
        for n, p in plans.items():
            if p.action == "REDACT":
                outcome[n]["occurrences_after"] = _residual_total(conn, LANES[n], outcome[n].pop("_targets"))
        if delete_set:
            outcome["_delete_union"]["remaining_after"] = len(current_digests(conn, sorted(delete_set)))
        try:
            busy, wal_frames, ckpt = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            checkpoint = {"mode": "PASSIVE", "busy": busy, "wal_frames": wal_frames, "checkpointed": ckpt}
        except sqlite3.Error as exc:
            checkpoint = {"mode": "PASSIVE", "error": type(exc).__name__}
    finally:
        conn.close()

    payload = {
        "kind": "APPLIED", "script": SCRIPT_ID, "applied_at": datetime.now(timezone.utc).isoformat(),
        "intent": str(manifest), "db": str(Path(db).resolve()), "lanes": selected,
        "release_lex": sorted(release_lex), "accept_delta": accept_delta, "allow_live": allow_live,
        "union": union, "heartbeat": {k: hb[k] for k in ("exists", "content_age_s", "mtime_age_s", "stopped")},
        "heartbeat_at_write_lock": {k: hb_lock[k] for k in ("exists", "content_age_s", "mtime_age_s", "stopped")},
        "lex_partition_gate": {"released_lanes": sorted(release_lex), "lex_chunks_in_released_lanes": lex_released},
        "drift": {"changed": sorted(set(drifted)), "vanished": vanished},
        "outcome": outcome, "wal_checkpoint": checkpoint,
        "verified": verified,
        "item_record": item_record(data["lanes"], selected, plans, outcome, set(vanished)),
        "residuals": RESIDUALS + RB_RESIDUALS,
    }
    path = write_applied(out_dir, payload)
    log.info("APPLIED: %s", json.dumps(outcome, sort_keys=True, default=str))
    log.info("APPLIED record -> %s", path)
    return path


def _drift(conn: sqlite3.Connection, plans: dict[str, LanePlan],
           all_ids: list[str]) -> tuple[list[str], list[str]]:
    """(vanished ids, changed ids) against every plan's pre_sha256."""
    now = current_digests(conn, all_ids)
    vanished = [cid for cid in all_ids if cid not in now]
    drifted = sorted({cid for p in plans.values() for cid in p.chunk_ids
                      if cid in now and now[cid] != p.pre_sha256.get(cid)})
    return vanished, drifted


def _lex_partition_ids(conn: sqlite3.Connection, ids: list[str]) -> list[str]:
    """The ids among ``ids`` whose CURRENT ``entity`` is in the LEX partition."""
    out: list[str] = []
    uniq = sorted(set(ids))
    for i in range(0, len(uniq), _ID_BATCH):
        batch = uniq[i:i + _ID_BATCH]
        ph = ",".join("?" * len(batch))
        out.extend(cid for cid, entity in conn.execute(
            f"SELECT chunk_id, entity FROM knowledge_chunks WHERE chunk_id IN ({ph})", batch)
            if is_lex_partition(entity))
    return sorted(out)


def _partition_gate(conn: sqlite3.Connection, plans: dict[str, LanePlan],
                    release_lex: frozenset[str]) -> tuple[list[str], dict[str, int]]:
    """The LEX hold, re-decided at --apply from the CURRENT ``entity`` of every planned
    id (the dry-run's hold is a point-in-time read, and chunk_digest covers content +
    title only -- a re-tag is not drift, and an edited INTENT can carry a held id).
    Returns ``(refusals, lex_in_released)``: a lane NOT released at this apply that
    plans a LEX-partition chunk refuses; a released lane's LEX count is recorded."""
    refusals: list[str] = []
    released: dict[str, int] = {}
    for n, p in plans.items():
        lex = _lex_partition_ids(conn, p.chunk_ids) if p.chunk_ids else []
        if n in release_lex:
            released[n] = len(lex)
        elif lex:
            refusals.append(f"lane {n}: {len(lex)} planned chunk(s) are in the LEX partition now and the lane "
                            f"is not released (--release-lex {n} on the dry-run AND the apply)")
    return refusals, released


def _verify_lanes(conn: sqlite3.Connection, ctx: RunContext, plans: dict[str, LanePlan]) -> dict[str, str]:
    """Run every selected lane's apply-time re-verification (RIDER B) on the
    READ-ONLY handle. A lane with 0 planned chunks is not re-verified (it deletes
    nothing, so a Drive outage must not block the rest). Any reason -> Refused,
    before any write. Returns {lane: "verified" | "skipped (0 planned)" | "n/a"}."""
    out: dict[str, str] = {}
    for n, p in plans.items():
        lane = LANES[n]
        if lane.verify is None:
            out[n] = "n/a"
            continue
        if not p.chunk_ids:
            out[n] = "skipped (0 planned)"
            continue
        try:
            reasons = list(lane.verify(conn, ctx, p) or [])
        except Exception as exc:  # noqa: BLE001 -- a verify crash refuses, loudly
            reasons = [f"re-verification error ({type(exc).__name__})"]
        if reasons:
            raise Refused(f"lane {n}: apply-time re-verification refused -- {'; '.join(reasons)} "
                          f"-- nothing written")
        out[n] = "verified"
    return out


def item_record(intent_lanes: dict[str, Any], selected: list[str], plans: dict[str, LanePlan],
                outcome: dict[str, Any], vanished: set[str]) -> dict[str, Any]:
    """Per lane: APPLIED / APPLIED (no-op) / NOT APPLIED, with the held-why and the
    stopped-why -- the record the folder audit reads to unblock batch 2."""
    rec: dict[str, Any] = {}
    for name, d in intent_lanes.items():
        stops = [str(s.get("reason", "")) for s in (d.get("stops") or []) if isinstance(s, dict)]
        if name not in selected:
            rec[name] = {"status": "NOT APPLIED",
                         "why": ("STOPPED at the dry-run: " + "; ".join(stops)) if stops
                                else "deselected with --lanes"}
            continue
        p = plans[name]
        planned = len(p.chunk_ids)
        van = len(set(p.chunk_ids) & vanished)
        r: dict[str, Any] = {"status": "APPLIED" if planned else "APPLIED (no-op: 0 chunks selected)",
                             "planned": planned, "vanished_before_apply": van, "holds": len(p.holds),
                             "held_why": sorted({str(h.get("reason", "")) for h in p.holds})}
        if p.action == "DELETE":
            r["deleted"] = planned - van
        else:
            r["rows_updated"] = (outcome.get(name) or {}).get("rows_updated")
        rec[name] = r
    return rec


def _redact_lane(conn: sqlite3.Connection, lane: Lane, plan: LanePlan,
                 delete_set: set[str], vanished: set[str]) -> dict[str, Any]:
    """UPDATE content/title in place with the lane's STRICT redactor, inside the
    caller's transaction (no commit here). A chunk also in a DELETE lane is skipped
    (it is deleted instead); a redactor error skips the chunk -- a withheld marker
    is never persisted over a chunk: a redactor that RETURNS the withheld marker
    (the egress redactor's fail-closed shape, were a lane ever bound to it) is
    treated as that same error, not written."""
    assert lane.redact is not None and lane.residual is not None
    targets = [cid for cid in plan.chunk_ids if cid not in delete_set and cid not in vanished]
    res: dict[str, Any] = {
        "action": "REDACT", "planned": len(plan.chunk_ids), "targets": len(targets),
        "skipped_also_deleted": sum(1 for c in plan.chunk_ids if c in delete_set),
        "skipped_vanished": sum(1 for c in plan.chunk_ids if c in vanished),
        "holds": len(plan.holds), "rows_updated": 0, "redactions": 0,
        "occurrences_before": 0, "errors": [], "_targets": targets,
    }
    for cid in targets:
        row = conn.execute("SELECT content, title FROM knowledge_chunks WHERE chunk_id=?", (cid,)).fetchone()
        if row is None:
            continue
        content, title = row
        res["occurrences_before"] += lane.residual(content or "") + lane.residual(title or "")
        try:
            new_c, n_c = lane.redact(content or "")
            new_t, n_t = lane.redact(title or "")
        except Exception as exc:  # noqa: BLE001 -- skip the chunk; never persist a withheld shell
            res["errors"].append({"chunk_id": cid, "error": type(exc).__name__})
            continue
        if new_c == secret_tokens.WITHHELD or new_t == secret_tokens.WITHHELD:
            res["errors"].append({"chunk_id": cid, "error": "withheld-returned"})
            continue
        if not (n_c or n_t):
            continue
        conn.execute("UPDATE knowledge_chunks SET content=?, title=? WHERE chunk_id=?",
                     (new_c if n_c else content, new_t if n_t else title, cid))
        res["rows_updated"] += 1
        res["redactions"] += n_c + n_t
    return res


def _residual_total(conn: sqlite3.Connection, lane: Lane, targets: list[str]) -> int:
    assert lane.residual is not None
    total = 0
    for cid in targets:
        row = conn.execute("SELECT content, title FROM knowledge_chunks WHERE chunk_id=?", (cid,)).fetchone()
        if row is not None:
            total += lane.residual(row[0] or "") + lane.residual(row[1] or "")
    return total


# ── main ──────────────────────────────────────────────────────────────────────
def _default_drive_service() -> Any:
    """The READ-ONLY Drive service for the RIDER B Drive lanes: the direct SA (a Viewer
    on HJR-Founder-OS) -- the builder sweep_founders_os and the positive-leaf gate's
    default use. The repo .env is loaded HERE, on first Drive use (D-021: the repo
    path, override=True) -- never at import, so importing this script (a test) can
    never clobber the importing process's environment."""
    from dotenv import load_dotenv  # noqa: PLC0415
    load_dotenv(_REPO_ROOT / ".env", override=True)
    return _pci()._drive_service(None)


#: Resolved per main() call (tests swap in an in-memory Drive).
DRIVE_SERVICE_FACTORY: Callable[[], Any] = _default_drive_service


def _names(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for v in values or []:
        out.extend(x.strip() for x in str(v).split(",") if x.strip())
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Code #15 combined KB purge (dry-run default).")
    ap.add_argument("--apply", action="store_true", help="Execute a reviewed INTENT manifest.")
    ap.add_argument("--manifest", default=None, help="INTENT json from the dry-run (required with --apply).")
    ap.add_argument("--lanes", action="append", default=None,
                    help="Lane subset (comma-separated or repeated). Default: every lane.")
    ap.add_argument("--release-lex", action="append", default=None,
                    help="Plan (dry-run) / apply LEX-partition rows for this lane. Never implied.")
    ap.add_argument("--accept-delta", action="store_true",
                    help="--apply: proceed although chunks changed/vanished since the dry-run.")
    ap.add_argument("--allow-live", action="store_true",
                    help="--apply: a LARGE apply proceeds without proof Cora is stopped.")
    ap.add_argument("--db", default=str(_REPO_ROOT / "data" / "cora_kb.db"))
    ap.add_argument("--out-dir", default=None, help="Where INTENT/APPLIED records go (default logs/).")
    ap.add_argument("--heartbeat", default=None, help="Heartbeat file (default: health_endpoint's).")
    ap.add_argument("--csv", default=None,
                    help=f"kb-purge-rows.csv for lane {RB3_STATIC_LANE} (dry-run AND --apply; the apply "
                         f"re-checks its sha256 and re-runs the existence gate).")
    ap.add_argument("--founder-root", default=str(FOUNDER_OS_ROOT_DEFAULT),
                    help="The Founder-OS root the CSV relpaths are relative to (read-only existence gate).")
    ap.add_argument("--hash-store", default=str(HASH_STORE_DEFAULT),
                    help="The static-md content-hash store (READ-ONLY; a residual count).")
    args = ap.parse_args(argv)

    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                            handlers=[logging.StreamHandler(sys.stdout)])
    db = Path(args.db)
    out_dir = Path(args.out_dir) if args.out_dir else default_out_dir()
    hb = Path(args.heartbeat) if args.heartbeat else None
    lanes = _names(args.lanes)
    release = frozenset(_names(args.release_lex))
    unknown = [n for n in lanes + sorted(release) if n not in LANES]
    if unknown:
        log.error("REFUSED: unknown lane(s) %s (registered: %s)", unknown, sorted(LANES))
        return 1
    if not db.exists():
        log.error("REFUSED: KB not found: %s", db)
        return 1
    inputs = dict(csv_path=Path(args.csv) if args.csv else None, founder_root=Path(args.founder_root),
                  hash_store=Path(args.hash_store), drive_factory=DRIVE_SERVICE_FACTORY)
    try:
        if not args.apply:
            run_dry(db, out_dir, lanes or list(LANES), release, heartbeat_path=hb, **inputs)
            return 0
        if not args.manifest:
            raise Refused("--apply requires --manifest <INTENT json> (run the dry-run first, D-086)")
        run_apply(db, out_dir, Path(args.manifest), lanes=lanes or None, release_lex=release,
                  accept_delta=args.accept_delta, allow_live=args.allow_live, heartbeat_path=hb, **inputs)
        return 0
    except Refused as exc:
        log.error("REFUSED: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
