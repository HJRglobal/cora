#!/usr/bin/env python3
r"""Re-file the session captures the harvester MISROUTED into the LEX partition
(Leak #2, ingest-integrity bundle I2(c), cq-bc5e5b7512bd, 2026-09-08).

THE DEFECT. ``session_capture._finalize_capture`` (through 2026-09-08) forced
``entity = "LEX"`` whenever ``phi_guard.is_phi_risk(transcript)`` tripped -- and
that ingestion-grade regex trips on ordinary session vocabulary (measured on the
real transcripts: ``diagnosis`` in 50 of 116, ``arc`` in 45, ``assessment``,
``discharge``, ``patient`` ...). Result: 142 non-LEX Code/Cowork sessions since
2026-06-11 were written into ``08-Lexington-Services/_session-captures/`` with a
false "PHI: yes (LEX-scoped, access-controlled)" stamp and KB-ingested under the
LEX partition -- invisible to FNDR/F3E asks, and non-LEX content inside the
custodian-gated store. The harvester fix (same commit) never re-homes to LEX
again; THIS script repairs the existing population.

WHAT IT DOES -- per harvester-written file in the LEX captures folder whose header
says LEX and whose body carries the PHI stamp:

  1. RE-CLASSIFY the note's entity with Haiku (the original distill's entity was
     lost when the override rewrote the header; the note is the durable record,
     the transcripts are already partly gone). Default = the entity implied by
     the session's ``Source cwd`` line, exactly as the harvester does.
  2. Decide ONE action:
       KEEP        -- Haiku says LEX / LEX-*: a genuine Lexington session. Untouched
                      (LEX content changes belong to the LEX bundle).
       MOVE        -- non-LEX and the note text carries no value-shaped PHI
                      (``phi_guard.is_prose_phi_risk`` is False): re-file into
                      ``<entity>/_session-captures/YYYY-MM/`` (same filename),
                      header entity rewritten, the false PHI line replaced by a
                      provenance line; purge its LEX-partition chunks (static_md by
                      the old path, drive_sweep/drive_asset twins by exact title);
                      re-ingest under the correct partition.
       QUARANTINE  -- non-LEX BUT the note carries value-shaped PHI: moved into the
                      KB-excluded founder-only quarantine folder (the same one the
                      fixed harvester uses), chunks purged, NOT re-ingested.
       HOLD        -- Haiku failed or answered outside the valid entity set: the
                      file is listed and left in place. Never guess (kickoff §8).
  3. TWINS -- for every capture file (moved or not) the Drive door (drive_sweep /
     drive_asset, keyed by exact title) may carry an entity that disagrees with
     the static_md row (the folder-deterministic one). Those disagreeing Drive
     rows are deleted; the founders_os sweep writes a folder-correct twin the
     next time it enumerates the file, and static_md serves it meanwhile (I5).

RUN SHAPE (doctrine a, decisions.md 2026-09-03): the dry-run writes an INTENT
file and is read-only on Drive and the KB; ``--apply --manifest <intent>``
executes exactly the reviewed rows (content-hash checked per file) and writes an
APPLIED record AFTER the act from what actually happened. The KB partition counts
before/after are printed and recorded (D-257 outputs).

    .venv\\Scripts\\python.exe scripts\\refile_misrouted_session_captures.py
    .venv\\Scripts\\python.exe scripts\\refile_misrouted_session_captures.py --limit 5
    .venv\\Scripts\\python.exe scripts\\refile_misrouted_session_captures.py --apply --manifest logs\\refile-session-captures-INTENT-<stamp>.json

GATES (D-086 over-deletion is the cardinal sin; D-145 LEX wall):
  * every source path must sit under ``08-Lexington-Services/_session-captures/``
    and match the harvester's filename shape -- anything else STOPS the run;
  * every destination must sit under an entity ``_session-captures/`` folder or
    the quarantine folder -- anything else STOPS the run;
  * a KEEP/HOLD row never moves; LEX content is never moved anywhere (the script
    only moves non-LEX content OUT of the LEX partition);
  * ``--apply`` refuses without a reviewed manifest, refuses a file whose content
    changed since the dry-run (unless ``--accept-delta``), and -- because 142
    files is a LARGE hygiene sweep (D-087: >100 files) -- refuses while the live
    bot's heartbeat is fresh unless ``--allow-live`` (run it inside the restart's
    stop window, runbook precedent 2026-09-03 doctrine e);
  * the KB is opened through ``schema.connect`` (vec0 loaded) so the vector
    cascade really deletes (a plain sqlite3 connection passes vacuously).

Exit codes: 0 ok, 1 fatal / refused, 2 a hold guard tripped.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_REPO_ROOT / ".env", override=True)
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import drive_io, phi_guard  # noqa: E402
from cora import session_capture as scap  # noqa: E402
from cora.kb_archive import delete_chunks  # noqa: E402

log = logging.getLogger("refile-session-captures")

LEX_FOLDER = scap.ENTITY_FOLDERS["LEX"]                     # 08-Lexington-Services
CAPTURES_DIRNAME = "_session-captures"
_HAIKU_MODEL = "claude-haiku-4-5"
_MAX_NOTE_CHARS = 12_000
_LARGE_SWEEP_FILES = 100                                    # D-087 LARGE threshold (files)
_LARGE_SWEEP_CHUNKS = 500                                   # D-087 LARGE threshold (chunks, twins included)

# The harvester's filename shape: YYYY-MM-DD_<surface>_<8 hex>.md
_HARVESTER_NAME_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})_(code-session|cowork-session)_([0-9a-f]{8})\.md$"
)
# The harvester's header: ## <date> — <surface> — <ENTITY> — <topic>
_HEADER_RE = re.compile(r"^## (\S+) — (\S+) — ([A-Z][A-Z0-9-]*) — (.*)$")
_PHI_LINE = "- PHI: yes (LEX-scoped, access-controlled)"
_CWD_LINE_RE = re.compile(r"^- Source cwd: (.*)$", re.MULTILINE)
_SID_LINE_RE = re.compile(r"^- Source session id: (.*)$", re.MULTILINE)

_CLASSIFY_PROMPT = """You are re-classifying a distilled work-session note for a multi-business "Founder OS".

Valid entity codes: HJRG, F3E, F3C, UFL, HJRPROD, HJRP, BDM, LEX, OSN, FNDR (FNDR = founder / cross-entity / infra / personal). LEX = Lexington Services (a care provider). The session's working directory suggests: {default_entity}.

Return ONLY a JSON object: {{"entity": "<the single entity code this note is MOST about>"}}. If you genuinely cannot tell, answer {{"entity": "UNSURE"}} -- never guess.

Rules: a note ABOUT Cora's own build, Cora's code, tests, PHI guards or Lexington's data handling is FNDR unless it is about Lexington's business operations. A note about Lexington's clients, staff, website, programs or billing is LEX.

NOTE:
{note}
"""

ACTIONS = ("KEEP", "MOVE", "QUARANTINE", "HOLD")


@dataclass
class Row:
    src_rel: str
    filename: str
    sha256: str
    session_id: str
    captured_at: str
    header_entity: str
    cwd_entity: str
    target_entity: str
    action: str
    reason: str
    dst_rel: str = ""
    static_source_ids: list[str] = field(default_factory=list)
    kb_static_chunks: int = 0
    kb_drive_chunks: int = 0
    kb_drive_entities: list[str] = field(default_factory=list)
    topic: str = ""                       # the note's header topic -- the reviewer's eyeball column
    # apply-time outcome (APPLIED record only)
    result: str = ""


@dataclass
class TwinRow:
    filename: str
    static_entity: str
    drive_entities: list[str]
    drive_chunks: int
    result: str = ""


# ── discovery + parsing ───────────────────────────────────────────────────────
def lex_captures_root(root: Path) -> Path:
    return root / LEX_FOLDER / CAPTURES_DIRNAME


def quarantine_root(root: Path) -> Path:
    return scap.quarantine_root(root)


def iter_harvester_files(root: Path) -> list[Path]:
    """Harvester-written notes under the LEX captures folder, sorted. Hand-written
    ``*_lex_*.md`` recaps never match the filename shape and are never touched."""
    base = lex_captures_root(root)
    if not base.exists():
        return []
    out = [p for p in sorted(base.rglob("*.md")) if _HARVESTER_NAME_RE.match(p.name)]
    return out


def parse_note(text: str) -> dict[str, Any]:
    """Header entity / surface / topic / cwd / session id / phi-stamp presence."""
    first = text.split("\n", 1)[0].strip()
    m = _HEADER_RE.match(first)
    info: dict[str, Any] = {
        "header_ok": bool(m), "date": "", "surface": "", "entity": "", "topic": "",
        "cwd": "", "session_id": "", "phi_stamp": _PHI_LINE in text,
    }
    if m:
        info.update(date=m.group(1), surface=m.group(2), entity=m.group(3), topic=m.group(4))
    cm = _CWD_LINE_RE.search(text)
    if cm:
        info["cwd"] = cm.group(1).strip()
    sm = _SID_LINE_RE.search(text)
    if sm:
        info["session_id"] = sm.group(1).strip()
    return info


def rel_key(path: Path, root: Path) -> str:
    """The static_md source_id form: the path relative to the Founder OS root, as
    the harvester / static sync write it (OS separators)."""
    return str(path.relative_to(root))


def source_id_variants(rel: str) -> list[str]:
    """Both separator spellings -- the KB holds whichever the writer used."""
    fwd = rel.replace("\\", "/")
    back = rel.replace("/", "\\")
    return sorted({rel, fwd, back})


def load_ledger_index(ledger_path: Path) -> dict[str, dict]:
    """note_path basename -> ledger row (the harvester writes one row per capture)."""
    idx: dict[str, dict] = {}
    if not ledger_path.exists():
        return idx
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        np_ = str(rec.get("note_path") or "")
        if np_:
            idx[Path(np_).name] = rec
    return idx


# ── classification ────────────────────────────────────────────────────────────
_ENTITY_ALIASES = {"F3": "F3E", "F3 ENERGY": "F3E", "LEXINGTON": "LEX", "FOUNDER": "FNDR"}


def neutralize_for_classifier(note_text: str) -> str:
    """The note as the classifier must see it: the misrouting's OWN tokens removed.

    The header carries the OVERRIDDEN entity ("— LEX —") and the body the false
    stamp ("PHI: yes (LEX-scoped, access-controlled)"). Left in, they bias the
    classifier toward LEX -- the first full dry-run (2026-09-08) re-classified the
    Mood Variety FBA session and three Walmart sessions as LEX on exactly that
    cue. The header entity becomes "?" and the stamp line is dropped; the cwd and
    session-id lines are dropped too (the cwd is passed as the default separately)."""
    lines = note_text.split("\n")
    if lines:
        m = _HEADER_RE.match(lines[0].strip())
        if m:
            lines[0] = f"## {m.group(1)} — {m.group(2)} — ? — {m.group(4)}"
    keep = [ln for ln in lines
            if ln.strip() != _PHI_LINE and not ln.startswith("- Source cwd:")
            and not ln.startswith("- Source session id:")]
    return "\n".join(keep)


def classify_entity(note_text: str, default_entity: str, client: Any,
                    raw_out: dict | None = None) -> str | None:
    """Haiku entity re-classification. None on ANY failure (-> HOLD). ``raw_out``
    receives the raw answer (for the HOLD row's reason -- an entity code or a
    fragment, never PHI-bearing note text)."""
    if client is None:
        return None
    prompt = _CLASSIFY_PROMPT.format(default_entity=default_entity,
                                     note=neutralize_for_classifier(note_text)[:_MAX_NOTE_CHARS])
    try:
        resp = client.messages.create(
            model=_HAIKU_MODEL, max_tokens=120,   # a fenced, pretty-printed answer still fits
            messages=[{"role": "user", "content": prompt}],
        )
        try:
            from cora.llm_usage import log_usage
            log_usage(resp, caller="refile_session_captures", model=_HAIKU_MODEL)
        except Exception:  # noqa: BLE001 -- usage logging is best-effort
            pass
        raw = resp.content[0].text.strip()
    except Exception as exc:  # noqa: BLE001 -- fail-closed -> HOLD
        log.warning("classify failed: %s", exc)
        return None
    if raw_out is not None:
        raw_out["raw"] = raw[:80]
    if raw.startswith("```"):   # a fenced answer is still an answer (the harvester strips the same way)
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return None
    ent = scap.normalize_entity((obj or {}).get("entity", ""))
    ent = _ENTITY_ALIASES.get(ent, ent)
    if ent in ("UNSURE", "UNKNOWN", "UNCLEAR", "?"):
        return None                       # an honest UNSURE holds the file (D-051 lens A #2)
    return ent if ent in scap.VALID_ENTITIES else None


def decide(target: str | None, note_text: str) -> tuple[str, str]:
    """(action, reason) from the re-classified entity + the prose PHI screen."""
    if target is None:
        return "HOLD", "classifier failed or answered outside the valid entity set"
    if target.startswith("LEX"):
        return "KEEP", f"re-classified {target}: genuine Lexington session stays in place"
    if phi_guard.is_prose_phi_risk(note_text):
        return "QUARANTINE", (f"re-classified {target} but the note carries value-shaped PHI "
                              f"(is_prose_phi_risk) -- founder review, not KB-ingested")
    return "MOVE", f"re-classified {target}: non-LEX content out of the LEX partition"


# ── KB selection (read-only) ──────────────────────────────────────────────────
# knowledge_chunks has no index on `title`, and the Drive-door rows are matched by
# exact title, so every per-file title query is a full scan of the Drive partition
# (~400K rows). ONE scan builds an in-memory index of every capture-shaped Drive
# row; counts, selection and the twin diff all read that dict (a 3-file dry-run
# with per-file scans ran past two minutes on the live KB, 2026-09-08).
DriveIndex = dict[str, list[tuple[str, str, str]]]   # title -> [(chunk_id, entity, source)]


def load_drive_capture_index(conn) -> DriveIndex:
    idx: DriveIndex = {}
    for cid, title, ent, src in conn.execute(
        "SELECT chunk_id, title, entity, source FROM knowledge_chunks "
        "WHERE source IN ('drive_sweep','drive_asset') "
        "AND (title GLOB '*_code-session_*' OR title GLOB '*_cowork-session_*')"   # GLOB: `_` is literal
    ):
        t = str(title or "")
        if _HARVESTER_NAME_RE.match(t):
            idx.setdefault(t, []).append((str(cid), str(ent), str(src)))
    return idx


def kb_counts_for(conn, static_ids: list[str], filename: str,
                  drive_index: DriveIndex) -> tuple[int, int, list[str]]:
    ph = ",".join("?" * len(static_ids))
    n_static = conn.execute(
        f"SELECT count(*) FROM knowledge_chunks WHERE source='static_md' AND source_id IN ({ph})",
        static_ids,
    ).fetchone()[0]
    drive_rows = drive_index.get(filename, [])
    return int(n_static), len(drive_rows), sorted({ent for _cid, ent, _src in drive_rows})


def select_chunk_ids(conn, static_ids: list[str], filename: str, drive_index: DriveIndex,
                     target_entity: str | None = None) -> list[str]:
    """The LEX static rows of the file + its Drive-door rows carrying the MISFILED
    tag (LEX*). A Drive row already tagged with the TARGET entity is correct and
    stays (the Drive door matches by TITLE, so two distinct Drive files can share a
    harvester filename -- D-051 lens B #4)."""
    ph = ",".join("?" * len(static_ids))
    ids = [r[0] for r in conn.execute(
        f"SELECT chunk_id FROM knowledge_chunks WHERE source='static_md' AND source_id IN ({ph})",
        static_ids).fetchall()]
    drive_rows = drive_index.get(filename, [])
    file_ids = {str(src) for _cid, _ent, src in drive_rows}
    if len(drive_rows) and len(conn.execute(
            "SELECT DISTINCT source_id FROM knowledge_chunks WHERE title = ? AND source IN ('drive_sweep','drive_asset')",
            (filename,)).fetchall()) > 1:
        log.warning("%s: more than one Drive file id shares this title -- only LEX-tagged rows are purged", filename)
    for cid, ent, _src in drive_rows:
        if str(ent).upper().startswith("LEX"):
            ids.append(cid)
        elif target_entity and str(ent).upper() == str(target_entity).upper():
            continue                       # already right -- keep
        else:
            ids.append(cid)                # a third, wrong tag (e.g. FNDR twin of an F3E note): remove
    del file_ids
    return list(dict.fromkeys(ids))


def find_twin_disagreements(conn, exclude_filenames: set[str], drive_index: DriveIndex) -> list[TwinRow]:
    """Capture files whose Drive-door rows carry an entity different from the
    static_md (folder) entity. Files being moved are excluded (their twins are
    deleted by the move itself). One static scan + the in-memory Drive index."""
    static: dict[str, set[str]] = {}
    for sid, ent in conn.execute(
        "SELECT source_id, entity FROM knowledge_chunks WHERE source='static_md' "
        "AND source_id GLOB '*_session-captures*'"
    ):
        base = str(sid).replace("\\", "/").rsplit("/", 1)[-1]
        if _HARVESTER_NAME_RE.match(base):
            static.setdefault(base, set()).add(str(ent))
    out: list[TwinRow] = []
    for base, ents in sorted(static.items()):
        if base in exclude_filenames or len(ents) != 1:
            continue
        static_entity = next(iter(ents))
        bad = [(cid, ent) for cid, ent, _src in drive_index.get(base, []) if ent != static_entity]
        if bad:
            out.append(TwinRow(filename=base, static_entity=static_entity,
                               drive_entities=sorted({ent for _cid, ent in bad}),
                               drive_chunks=len(bad)))
    return out


def select_twin_chunk_ids(twin: TwinRow, drive_index: DriveIndex) -> list[str]:
    return [cid for cid, ent, _src in drive_index.get(twin.filename, []) if ent != twin.static_entity]


def static_entity_now(conn, filename: str) -> str | None:
    """The folder entity the static_md door holds for *filename* RIGHT NOW (None when
    absent or ambiguous) -- re-checked at apply so a twin decided at dry-run time is
    never executed against a file the static sync has since re-homed."""
    ents = {str(e) for (e,) in conn.execute(
        "SELECT DISTINCT entity FROM knowledge_chunks WHERE source='static_md' AND source_id GLOB ?",
        (f"*{filename}",))}
    return next(iter(ents)) if len(ents) == 1 else None


def partition_counts(conn) -> dict[str, Any]:
    """Capture-chunk counts per entity per door -- the D-257 before/after output."""
    out: dict[str, Any] = {"static_md": {}, "drive": {}}
    for ent, n in conn.execute(
        "SELECT entity, count(*) FROM knowledge_chunks WHERE source='static_md' "
        "AND source_id GLOB '*_session-captures*' GROUP BY entity"):
        out["static_md"][str(ent)] = int(n)
    for ent, n in conn.execute(
        "SELECT entity, count(*) FROM knowledge_chunks WHERE source IN ('drive_sweep','drive_asset') "
        "AND (title GLOB '*_code-session_*' OR title GLOB '*_cowork-session_*') GROUP BY entity"):
        out["drive"][str(ent)] = int(n)
    return out


# ── destinations ──────────────────────────────────────────────────────────────
def dest_for(action: str, target_entity: str, filename: str, root: Path) -> Path:
    m = _HARVESTER_NAME_RE.match(filename)
    month = m.group(1)[:7] if m else datetime.now(timezone.utc).strftime("%Y-%m")
    if action == "QUARANTINE":
        return quarantine_root(root) / month / f"{scap.QUARANTINE_PREFIX}{filename}"
    folder = scap.entity_folder(target_entity)
    return root / folder / CAPTURES_DIRNAME / month / filename


def rewrite_note(text: str, target_entity: str, action: str, src_rel: str, when: str) -> str:
    """Header entity LEX -> target; the false PHI stamp -> a provenance line."""
    first, rest = (text.split("\n", 1) + [""])[:2]
    m = _HEADER_RE.match(first.strip())
    if m:
        first = f"## {m.group(1)} — {m.group(2)} — {target_entity} — {m.group(4)}"
    if action == "QUARANTINE":
        prov = (f"- QUARANTINED: {when} -- PHI-risk on a non-LEX session (re-filed from "
                f"{src_rel}); founder review; not KB-ingested (ingest-integrity 2026-09, "
                f"cq-bc5e5b7512bd)")
    else:
        prov = (f"- Re-filed: {when} from {src_rel} (false PHI stamp; ingest-integrity "
                f"2026-09, cq-bc5e5b7512bd)")
    if _PHI_LINE in rest:
        rest = rest.replace(_PHI_LINE, prov, 1)
    else:
        rest = rest.rstrip("\n") + "\n" + prov + "\n"
    return first + "\n" + rest


# ── path gates (D-086) ────────────────────────────────────────────────────────
class GateTripped(RuntimeError):
    pass


def assert_src_allowed(src: Path, root: Path) -> None:
    base = lex_captures_root(root).resolve()
    try:
        src.resolve().relative_to(base)
    except ValueError as exc:
        raise GateTripped(f"source outside the LEX captures folder: {src}") from exc
    if not _HARVESTER_NAME_RE.match(src.name):
        raise GateTripped(f"source is not a harvester-written note: {src.name}")


def assert_dst_allowed(dst: Path, root: Path) -> None:
    r = dst.resolve()
    q = quarantine_root(root).resolve()
    try:
        r.relative_to(q)
        return
    except ValueError:
        pass
    for folder in scap.ENTITY_FOLDERS.values():
        if folder == LEX_FOLDER:
            continue  # never INTO the LEX partition
        try:
            r.relative_to((root / folder / CAPTURES_DIRNAME).resolve())
            return
        except ValueError:
            continue
    raise GateTripped(f"destination outside every entity _session-captures folder: {dst}")


# ── plan (dry-run) ────────────────────────────────────────────────────────────
def build_plan(conn, root: Path, ledger_path: Path, client: Any, *, limit: int | None = None,
               dry_reclassify: bool = True) -> tuple[list[Row], list[TwinRow]]:
    ledger = load_ledger_index(ledger_path)
    drive_index = load_drive_capture_index(conn)
    rows: list[Row] = []
    files = iter_harvester_files(root)
    if limit:
        files = files[:limit]
    for path in files:
        assert_src_allowed(path, root)
        text = drive_io.read_text(str(path), encoding="utf-8")
        info = parse_note(text)
        rel = rel_key(path, root)
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        led = ledger.get(path.name, {})
        cwd_entity = scap.entity_from_cwd(info["cwd"] or None)
        if not info["header_ok"]:
            rows.append(Row(rel, path.name, sha, str(led.get("session_id") or info["session_id"] or ""),
                            str(led.get("captured_at", "")), info["entity"], cwd_entity, "",
                            "HOLD", "unparseable header -- left in place"))
            continue
        if info["entity"] != "LEX" or not info["phi_stamp"]:
            rows.append(Row(rel, path.name, sha, str(led.get("session_id") or info["session_id"] or ""),
                            str(led.get("captured_at", "")), info["entity"], cwd_entity,
                            info["entity"], "KEEP",
                            "not a phi-forced file (header entity %s, stamp=%s)"
                            % (info["entity"], info["phi_stamp"])))
            continue
        raw_out: dict = {}
        target = classify_entity(text, cwd_entity, client, raw_out) if dry_reclassify else None
        action, reason = decide(target, text)
        if action == "HOLD" and raw_out.get("raw"):
            reason += f" (raw answer: {raw_out['raw']!r})"
        row = Row(rel, path.name, sha, str(led.get("session_id") or info["session_id"] or ""),
                  str(led.get("captured_at", "")), info["entity"], cwd_entity,
                  target or "", action, reason, topic=str(info.get("topic") or "")[:80])
        if action in ("MOVE", "QUARANTINE"):
            dst = dest_for(action, target or "FNDR", path.name, root)
            assert_dst_allowed(dst, root)
            row.dst_rel = rel_key(dst, root)
            row.static_source_ids = source_id_variants(rel)
            row.kb_static_chunks, row.kb_drive_chunks, row.kb_drive_entities = kb_counts_for(
                conn, row.static_source_ids, path.name, drive_index)
        rows.append(row)
    if limit:
        # a partial file list cannot decide the corpus-wide twin pass: a not-yet-
        # planned misfiled file's CORRECT Drive tag would read as the disagreement
        log.info("twin pass skipped under --limit (partial plan)")
        return rows, []
    # exclude every file that is moving, quarantined OR HELD: for a held misfiled
    # file the static (LEX) row is the WRONG one and its Drive twin the right one
    excluded = {r.filename for r in rows if r.action in ("MOVE", "QUARANTINE", "HOLD")}
    twins = find_twin_disagreements(conn, excluded, drive_index)
    return rows, twins


def summarize(rows: list[Row], twins: list[TwinRow]) -> dict[str, Any]:
    by_action: dict[str, int] = {a: 0 for a in ACTIONS}
    by_target: dict[str, int] = {}
    chunks_static = chunks_drive = 0
    for r in rows:
        by_action[r.action] = by_action.get(r.action, 0) + 1
        if r.action in ("MOVE", "QUARANTINE"):
            by_target[r.target_entity] = by_target.get(r.target_entity, 0) + 1
            chunks_static += r.kb_static_chunks
            chunks_drive += r.kb_drive_chunks
    return {
        "files_scanned": len(rows), "by_action": by_action, "moves_by_target": by_target,
        "lex_chunks_to_purge_static": chunks_static, "lex_chunks_to_purge_drive": chunks_drive,
        "twin_disagreements": len(twins),
        "twin_chunks_to_delete": sum(t.drive_chunks for t in twins),
    }


def write_intent(path: Path, root: Path, rows: list[Row], twins: list[TwinRow],
                 before: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "kind": "INTENT", "generated_at": datetime.now(timezone.utc).isoformat(),
        "founder_os_root": str(root), "summary": summarize(rows, twins),
        "partition_counts_before": before,
        "rows": [asdict(r) for r in rows], "twins": [asdict(t) for t in twins],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    txt = path.with_suffix(".txt")
    lines = [f"# re-file INTENT {payload['generated_at']}  root={root}",
             f"# {json.dumps(payload['summary'])}", "",
             f"{'ACTION':10} {'TARGET':8} {'static':>6} {'drive':>6}  file  |  topic  ->  destination / reason"]
    for r in rows:
        dst = r.dst_rel or "-"
        lines.append(f"{r.action:10} {r.target_entity or '-':8} {r.kb_static_chunks:6d} {r.kb_drive_chunks:6d}  "
                     f"{r.filename}  |  {(r.topic or '(no topic)')[:60]}  ->  {dst}  [{r.reason}]")
    lines.append("")
    lines.append(f"# twin disagreements (Drive-door rows whose entity != the folder entity): {len(twins)}")
    for t in twins:
        lines.append(f"TWIN       static={t.static_entity:6} drive={','.join(t.drive_entities):12} "
                     f"chunks={t.drive_chunks:3d}  {t.filename}")
    txt.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── apply ─────────────────────────────────────────────────────────────────────
def load_intent(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("kind") != "INTENT":
        raise GateTripped(f"{path} is not an INTENT manifest")
    return data


def _bounded_unlink(path: Path, attempts: int = 3) -> None:
    """Unlink through the bounded Drive I/O shim, retried (the Drive client holds a
    just-read file open for a moment; the second try succeeds)."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            drive_io._run_bounded(lambda: os.unlink(path), drive_io.TIMEOUT_SECONDS)
            return
        except FileNotFoundError:
            return
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.5 * (i + 1))
    raise RuntimeError(f"unlink failed after {attempts} attempts: {last}")


def apply_rows(conn, kb: Any, root: Path, ledger_path: Path, rows: list[Row], twins: list[TwinRow],
               *, accept_delta: bool) -> dict[str, Any]:
    """Execute the reviewed rows. Per row, in a CONVERGENT order (D-051 lens B #1):
    gates -> hash-check (a changed file is re-DECIDED under --accept-delta, never
    executed on the stale decision) -> KB purge of the LEX rows FIRST -> write dst
    (skipped when an interrupted earlier run already wrote the identical text) ->
    unlink src (retried) -> (MOVE only) re-ingest -> ledger row. Any interruption
    leaves a state the NEXT run recognises and finishes: src gone + dst present =
    already applied; src present + identical dst = resume; a purge with no file
    move is healed by the nightly static sync (it re-ingests the still-LEX file).
    Per-row soft failure with rollback; the batch continues."""
    when = datetime.now(timezone.utc).isoformat()
    drive_index = load_drive_capture_index(conn)
    totals: dict[str, int] = {}
    n_moved = n_quar = n_reingested = n_skipped = n_resumed = 0
    for r in rows:
        if r.action not in ("MOVE", "QUARANTINE"):
            r.result = "untouched"
            continue
        src = root / r.src_rel
        dst = root / r.dst_rel
        try:
            assert_src_allowed(src, root)
            assert_dst_allowed(dst, root)
            if not src.exists():
                if dst.exists():
                    r.result = "already-applied (src gone, dst present -- an earlier run finished this row)"
                    n_resumed += 1
                else:
                    r.result = "src-missing"; n_skipped += 1
                continue
            text = drive_io.read_text(str(src), encoding="utf-8")
            sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if sha != r.sha256:
                if not accept_delta:
                    r.result = "content-changed-since-dry-run (skipped; --accept-delta to force)"
                    n_skipped += 1
                    continue
                # --accept-delta: the CONTENT is new, so the decision is re-derived from
                # it -- the dry-run's action is never executed on text nobody eyeballed
                action_now, reason_now = decide(r.target_entity or None, text)
                if action_now != r.action:
                    r.result = (f"decision-changed-since-dry-run (was {r.action}, now {action_now}: "
                                f"{reason_now}) -- skipped; re-run the dry-run")
                    n_skipped += 1
                    continue
            new_text = rewrite_note(text, r.target_entity, r.action, r.src_rel, when)
            resumed = False
            if dst.exists():
                existing = drive_io.read_text(str(dst), encoding="utf-8")
                # the provenance line carries a timestamp -- compare with it neutralised
                if _strip_when(existing) != _strip_when(new_text):
                    r.result = "dst-exists-with-different-content (skipped; resolve by hand)"
                    n_skipped += 1
                    continue
                resumed = True
                new_text = existing        # keep the earlier run's stamp
            # 1. KB first: the LEX rows of this file + its misfiled Drive twins
            ids = select_chunk_ids(conn, r.static_source_ids, r.filename, drive_index, r.target_entity)
            if ids:
                t = delete_chunks(conn, ids)
                for k, v in t.items():
                    totals[k] = totals.get(k, 0) + int(v)
            # 2. the file: write (unless resuming), then unlink the source
            if not resumed:
                drive_io.write_text_atomic(dst, new_text, encoding="utf-8")
            _bounded_unlink(src)
            if resumed:
                n_resumed += 1
            if r.action == "MOVE" and kb is not None:
                try:
                    from cora.knowledge_base.store import Document
                    ts = int(datetime.now(timezone.utc).timestamp())
                    kb.upsert_documents([Document(
                        source="static_md", source_id=r.dst_rel, entity=r.target_entity,
                        sub_entity=None, content=new_text, date_created=ts, date_modified=ts,
                        title=f"Session capture — {parse_note(new_text)['topic']}",
                        deep_link=f"computer://{dst}",
                        metadata={"path": r.dst_rel, "session_id": r.session_id,
                                  "kind": "session_capture", "refiled_from": r.src_rel},
                    )])
                    n_reingested += 1
                except Exception as exc:  # noqa: BLE001 -- the nightly static sync catches up
                    log.warning("re-ingest failed for %s (%s) -- the static sync will ingest it", dst, exc)
            scap.append_ledger({
                "session_id": r.session_id, "entity": r.target_entity, "phi": r.action == "QUARANTINE",
                "quarantined": r.action == "QUARANTINE", "note_path": str(dst),
                "refiled_from": str(src), "refiled_at": when, "captured_at": r.captured_at,
                "surface": "refile",
            }, ledger_path)
            r.result = ("moved" if r.action == "MOVE" else "quarantined") + (" (resumed)" if resumed else "")
            if r.action == "MOVE":
                n_moved += 1
            else:
                n_quar += 1
        except GateTripped:
            raise
        except Exception as exc:  # noqa: BLE001 -- per-row soft failure, batch continues
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            r.result = f"error: {exc}"
            n_skipped += 1
            log.error("row FAILED %s: %s", r.filename, exc)
    n_twin_chunks = n_twin_stale = 0
    for t in twins:
        # re-verify at apply time: the static door must still say what the dry-run saw
        now_ent = static_entity_now(conn, t.filename)
        if now_ent != t.static_entity:
            t.result = f"skipped: static entity is now {now_ent!r} (dry-run saw {t.static_entity!r})"
            n_twin_stale += 1
            continue
        ids = select_twin_chunk_ids(t, drive_index)
        if ids:
            tt = delete_chunks(conn, ids)
            for k, v in tt.items():
                totals[k] = totals.get(k, 0) + int(v)
            n_twin_chunks += len(ids)
        t.result = f"deleted {len(ids)} chunk(s)"
    return {"moved": n_moved, "quarantined": n_quar, "reingested": n_reingested, "resumed": n_resumed,
            "skipped": n_skipped, "twin_chunks_deleted": n_twin_chunks, "twins_skipped_stale": n_twin_stale,
            "delete_totals": totals}


_WHEN_RE = re.compile(r"\d{4}-\d{2}-\d{2}T[0-9:.+\-]+")


def _strip_when(text: str) -> str:
    return _WHEN_RE.sub("<when>", text)


def write_applied(path: Path, intent_path: Path, rows: list[Row], twins: list[TwinRow],
                  outcome: dict[str, Any], before: dict[str, Any], after: dict[str, Any]) -> None:
    payload = {
        "kind": "APPLIED", "applied_at": datetime.now(timezone.utc).isoformat(),
        "intent": str(intent_path), "outcome": outcome,
        "partition_counts_before": before, "partition_counts_after": after,
        "rows": [asdict(r) for r in rows], "twins": [asdict(t) for t in twins],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def live_bot_is_fresh() -> bool:
    try:
        from cora import health_endpoint
        age = health_endpoint.heartbeat_age_seconds()
        return age is not None and age <= 300
    except Exception:  # noqa: BLE001
        return False


# ── main ──────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Re-file misrouted session captures (Leak #2).")
    ap.add_argument("--apply", action="store_true", help="Execute a reviewed INTENT manifest.")
    ap.add_argument("--manifest", default=None, help="INTENT json from the dry-run (required with --apply).")
    ap.add_argument("--accept-delta", action="store_true",
                    help="--apply: also process files whose content changed since the dry-run.")
    ap.add_argument("--allow-live", action="store_true",
                    help="--apply: proceed even though the live bot's heartbeat is fresh (D-087 LARGE "
                         "sweeps run inside the stop window; this overrides that gate).")
    ap.add_argument("--limit", type=int, default=None, help="Dry-run: only the first N files.")
    ap.add_argument("--db", default=str(_REPO_ROOT / "data" / "cora_kb.db"))
    ap.add_argument("--root", default=str(scap.FOUNDER_OS_ROOT))
    ap.add_argument("--ledger", default=str(scap.LEDGER_PATH))
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    root = Path(args.root)
    db = Path(args.db)
    ledger_path = Path(args.ledger)
    logs_dir = _REPO_ROOT / "logs"
    if not root.exists():
        log.error("Founder OS root not found: %s", root)
        return 1
    if not db.exists():
        log.error("KB not found: %s", db)
        return 1

    from cora.knowledge_base import schema
    from cora.knowledge_base.store import KnowledgeBase

    if not args.apply:
        client = None
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if api_key:
            try:
                import anthropic
                client = anthropic.Anthropic(api_key=api_key)
            except Exception as exc:  # noqa: BLE001
                log.warning("anthropic client unavailable (%s) -- every phi-forced file will HOLD", exc)
        else:
            log.warning("ANTHROPIC_API_KEY not set -- every phi-forced file will HOLD")
        conn = schema.connect(db, read_only=True)
        try:
            before = partition_counts(conn)
            try:
                rows, twins = build_plan(conn, root, ledger_path, client, limit=args.limit)
            except GateTripped as exc:
                log.error("GATE TRIPPED: %s", exc)
                return 2
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            intent = logs_dir / f"refile-session-captures-INTENT-{stamp}.json"
            write_intent(intent, root, rows, twins, before)
        finally:
            conn.close()
        summ = summarize(rows, twins)
        log.info("DRY-RUN summary: %s", json.dumps(summ))
        log.info("partition counts BEFORE (capture chunks): %s", json.dumps(before))
        log.info("INTENT written -> %s (+ .txt table). Eyeball it, then run with "
                 "--apply --manifest %s inside the stop window.", intent, intent)
        return 0

    if not args.manifest:
        log.error("REFUSED: --apply requires --manifest <INTENT json> (run the dry-run first, D-086)")
        return 1
    intent_path = Path(args.manifest)
    try:
        data = load_intent(intent_path)
    except (OSError, ValueError, GateTripped) as exc:
        log.error("REFUSED: %s", exc)
        return 1
    rows = [Row(**{k: v for k, v in r.items() if k in Row.__dataclass_fields__}) for r in data["rows"]]
    twins = [TwinRow(**{k: v for k, v in t.items() if k in TwinRow.__dataclass_fields__}) for t in data["twins"]]
    actionable = [r for r in rows if r.action in ("MOVE", "QUARANTINE")]
    if str(data.get("founder_os_root")) != str(root):
        log.error("REFUSED: manifest root %s != --root %s", data.get("founder_os_root"), root)
        return 1
    total_chunks = (sum(r.kb_static_chunks + r.kb_drive_chunks for r in actionable)
                    + sum(t.drive_chunks for t in twins))
    is_large = len(actionable) > _LARGE_SWEEP_FILES or total_chunks > _LARGE_SWEEP_CHUNKS
    if is_large and live_bot_is_fresh() and not args.allow_live:
        log.error("REFUSED: %d files / %d chunks is a LARGE sweep (D-087 >%d files or >%d chunks) and the "
                  "live bot's heartbeat is fresh -- run inside the restart's stop window, or pass --allow-live.",
                  len(actionable), total_chunks, _LARGE_SWEEP_FILES, _LARGE_SWEEP_CHUNKS)
        return 1
    for r in actionable:  # every path gate BEFORE any write
        try:
            assert_src_allowed(root / r.src_rel, root)
            assert_dst_allowed(root / r.dst_rel, root)
        except GateTripped as exc:
            log.error("GATE TRIPPED (nothing written): %s", exc)
            return 2
    kb = KnowledgeBase(db)
    conn = kb._conn  # schema.connect'ed (vec0 loaded) -- the cascade really deletes
    try:
        before = partition_counts(conn)
        outcome = apply_rows(conn, kb, root, ledger_path, rows, twins, accept_delta=args.accept_delta)
        after = partition_counts(conn)
    except GateTripped as exc:
        log.error("GATE TRIPPED mid-run: %s", exc)
        return 2
    finally:
        try:
            kb.close()
        except Exception:  # noqa: BLE001
            pass
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    applied = logs_dir / f"refile-session-captures-APPLIED-{stamp}.json"
    write_applied(applied, intent_path, rows, twins, outcome, before, after)
    log.info("APPLIED: %s", json.dumps(outcome))
    log.info("partition counts BEFORE: %s", json.dumps(before))
    log.info("partition counts AFTER:  %s", json.dumps(after))
    log.info("APPLIED record -> %s", applied)
    return 0


if __name__ == "__main__":
    sys.exit(main())
