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

RUN SHAPE (the refile_misrouted_session_captures / D-086 shape):

    .venv\Scripts\python.exe scripts\purge_kb_code15_2026-09.py
    .venv\Scripts\python.exe scripts\purge_kb_code15_2026-09.py --release-lex s1_tokens
    .venv\Scripts\python.exe scripts\purge_kb_code15_2026-09.py --apply --manifest logs\kb-purge-code15-INTENT-<stamp>.json [--lanes s1_tokens] [--release-lex s1_tokens]

  * The dry-run (default) opens the KB READ-ONLY (``kb_archive.connect_ro``: a
    ``mode=ro`` URI + ``query_only``) -- it cannot write the DB (D-290) -- and writes
    ``<out-dir>/kb-purge-code15-INTENT-<stamp>.json`` + ``.txt``. Both are
    self-scanned (``secrets_scan.scan_text`` + ``secret_tokens``) BEFORE they are
    written; a hit writes nothing and exits 1.
  * ``--apply`` refuses without ``--manifest``; refuses a manifest for another DB;
    refuses any selected lane with stops, an action that disagrees with the code, a
    LEX release the INTENT does not record (or one the apply does not repeat);
    refuses when any chunk's current sha256 differs from the INTENT (or the chunk
    vanished) unless ``--accept-delta``; and when the DELETE-lane union is LARGE
    (>500 chunks or >100 files, D-087) it requires Cora STOPPED -- the heartbeat file
    must EXIST and be older than 300 s by both its content stamp and its mtime; a
    MISSING or unparseable heartbeat refuses (fail closed) unless ``--allow-live``.
    Every refusal happens BEFORE the first write.
  * The apply connection is ``kb_archive.connect_rw`` (vec0 loaded, so the DELETE
    cascade really deletes) with ``PRAGMA secure_delete=ON``. REDACT is ``UPDATE
    knowledge_chunks SET content=?, title=?`` with the STRICT redactor (a redactor
    error skips that chunk -- a withheld marker is never persisted); vectors are not
    re-embedded. The ``APPLIED`` record is written AFTER the act, from what
    happened: per-lane outcome, before/after occurrence counts from a re-scan,
    heartbeat evidence, and the honest residuals.

No .env is loaded: the script makes no API call and reads no credential (the
self-scan reads the live .env VALUES through secrets_scan only to compare them).

Exit codes: 0 ok, 1 refused / fatal.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
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
            lines.append(f"   HOLD   {h.get('chunk_id', '')}  {h.get('reason', '')}")
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
def run_dry(db: Path, out_dir: Path, lanes: list[str], release_lex: frozenset[str],
            heartbeat_path: Path | None = None) -> Path:
    unknown = sorted(set(release_lex) - set(lanes))
    if unknown:
        raise Refused(f"--release-lex names lane(s) not being planned: {unknown}")
    ctx = RunContext(release_lex=frozenset(release_lex))
    conn = kb_archive.connect_ro(db)      # mode=ro + query_only: the dry-run CANNOT write (D-290)
    try:
        plans = build_plans(conn, ctx, lanes)
    finally:
        conn.close()
    hb = heartbeat_evidence(heartbeat_path)
    stamp = _stamp()
    payload = {
        "kind": "INTENT", "script": SCRIPT_ID, "version": INTENT_VERSION, "stamp": stamp,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db": str(Path(db).resolve()),
        "release_lex": sorted(ctx.release_lex),
        "lanes": {name: asdict(p) for name, p in plans.items()},
        "union": union_summary(list(plans.values())),
        "heartbeat": {k: hb[k] for k in ("exists", "content_age_s", "mtime_age_s", "stopped")},
        "residuals": RESIDUALS,
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
              heartbeat_path: Path | None = None) -> Path:
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
    # Drift gate on a READ-ONLY handle first: a refusal must precede every write
    # (connect_rw alone issues writer pragmas).
    ro = kb_archive.connect_ro(db)
    try:
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
        v2, d2 = _drift(conn, plans, all_ids)
        if (set(v2), set(d2)) != (set(vanished), set(drifted)):
            conn.rollback()
            raise Refused("the KB changed between the drift check and the write lock -- nothing written; "
                          "re-run the apply")
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
        "drift": {"changed": sorted(set(drifted)), "vanished": vanished},
        "outcome": outcome, "wal_checkpoint": checkpoint, "residuals": RESIDUALS,
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


def _redact_lane(conn: sqlite3.Connection, lane: Lane, plan: LanePlan,
                 delete_set: set[str], vanished: set[str]) -> dict[str, Any]:
    """UPDATE content/title in place with the lane's STRICT redactor, inside the
    caller's transaction (no commit here). A chunk also in a DELETE lane is skipped
    (it is deleted instead); a redactor error skips the chunk -- a withheld marker
    is never persisted over a chunk."""
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
    try:
        if not args.apply:
            run_dry(db, out_dir, lanes or list(LANES), release, heartbeat_path=hb)
            return 0
        if not args.manifest:
            raise Refused("--apply requires --manifest <INTENT json> (run the dry-run first, D-086)")
        run_apply(db, out_dir, Path(args.manifest), lanes=lanes or None, release_lex=release,
                  accept_delta=args.accept_delta, allow_live=args.allow_live, heartbeat_path=hb)
        return 0
    except Refused as exc:
        log.error("REFUSED: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
