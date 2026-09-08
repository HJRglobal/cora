"""Deterministic self-inventory: WHAT Cora ingests, through WHICH doors, what is
EXCLUDED, and her own scheduled cadence -- read from live signals, never from
the knowledge base (ingest-integrity bundle I4, cq-3542e1b095b2, 2026-09-08).

THE DEFECT. On 9/3 Cora denied having "Cowork Cascade knowledge" three times
and on 9/4 affirmed it with invented specifics. A meta-question about her own
corpus was answered by embedding similarity over CONTENT: the query landed on
Anthropic marketing emails and on her own prior denial (the swept thread made
her wrong answer the #1 hit for the re-ask). Doctrine 3 (decisions.md
2026-09-03 "D-057 IS LEAKING"): the absence of a semantic hit is never evidence
of absence. The fix is a deterministic listing she consults BEFORE any
"I have / I don't have" claim.

WHAT IS LISTED (every line is a live read; nothing here is retrieved):
  * ingest DOORS -- every ``source`` the KB holds with its chunk count and the
    sync watermarks the KB records for it (``sync_state``), plus the gmail
    per-account watermark file;
  * her SCHEDULED TASKS -- the live Task Scheduler registry (``schtasks
    /Query /FO CSV /V``, Cora tasks only, cached 10 min) with next/last run,
    cadence and last result, overlaid with the run-marker ledger
    (``logs/task-runs.jsonl``: when it last actually WROTE something);
  * PINNED EXCLUSIONS -- every ``KB_EXCLUDED_FOLDER_IDS`` id with its label
    (the Drive door), the path-segment exclusions (the static_md door), the
    name-skipped subtree folders, the title belts, and the allowlisted views;
  * ENTITY PARTITIONS -- chunk counts per entity;
  * the claude-workspace MIRROR parity report (path + generated stamp + per-class
    mirrored counts) and the canonical pointer for the Cowork / Cascade question.

SCOPE. ``detail=True`` (founder channels + Harrison) lists mailbox addresses,
folder ids and every task; other channels get door names, counts and the ingest
tasks only -- the inventory never becomes a per-user roster leak.

WIRING. ``app._dispatch_qa`` forces the ``cora_self_inventory`` tool (F-23
tool_choice pattern) when ``is_self_inventory_question`` matches, so the listing
is in context before the model composes; normal KB retrieval still runs (a fact
question that happens to be phrased "do you have ..." must still be answered from
retrieval -- the inventory says which DOOR, the retrieval says WHAT). The tool's
output carries the reply-format rule: a miss is "not in my sources" with the
door named, never "I don't have that knowledge".
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from cora import kb_exclusions, run_marker

log = logging.getLogger("cora.self_inventory")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_fos_env = os.environ.get("FOUNDER_OS_ROOT", "").strip()
FOUNDER_OS_ROOT = Path(_fos_env) if _fos_env else Path(r"G:\My Drive\HJR-Founder-OS")
PARITY_REPORT_REL = Path("_shared") / "claude-workspace-mirror" / "PARITY-REPORT.md"
GMAIL_WATERMARKS_PATH = _REPO_ROOT / "data" / "cache" / "gmail-thread-watermarks.json"

#: The canonical answer for the question class that produced this tool.
COWORK_CASCADE_POINTER = (
    "For 'do you have the Cowork / Cascade / Claude-workspace knowledge': YES by two "
    "doors -- the static_md sync of the Founder OS tree (CLAUDE.md, memory/, playbooks, "
    "every _session-captures/ folder, project _notes/) and the claude-workspace mirror "
    "(skills, Cowork memory, the task INDEX) under _shared/claude-workspace-mirror/. "
    "Cite the FNDR known-answer 'how Cora knows Cowork / Cascade / Claude-workspace "
    "material (2026-09-04)' and PARITY-REPORT.md for live counts. Excluded by design: "
    "Cora's own build docs and system prompts (_shared/projects/cora, D-057), scheduled-"
    "task BODIES and Code memory (ZONE-X). Not available: live Cowork session state."
)

REPLY_FORMAT = (
    "REPLY FORMAT (binding): answer 'do you have / do you know about / are you ingesting X' "
    "from THIS inventory, not from retrieved chunks. If X names a source, folder, tool, "
    "channel or document class: say which DOOR it arrives through (or that it is EXCLUDED "
    "by design, naming the exclusion). If X is a fact and the retrieved knowledge below has "
    "nothing: say 'not in my sources' and name the doors that WOULD carry it -- never say "
    "'I don't have that knowledge' from a search miss, never invent a source, never "
    "mention chunk numbers or context blocks. Cite PARITY-REPORT.md for mirror counts. "
    "Do not queue a code session for a question about your own sources."
)

# ── routing predicate (unit-tested in tests/test_self_inventory.py) ──────────
# Every quantified class is bounded; no nested quantifiers (ReDoS discipline).
_LEAD = (r"^\s*(?:<@[A-Z0-9]{1,20}>\s*[,:\-]?\s*)?"
         r"(?:(?:hey|hi|ok|okay|so|quick\s+question)[,\s]*)?"      # greeting before the name ...
         r"(?:@?cora[,:\-]?\s*)?"
         r"(?:(?:hey|hi|ok|okay|so)[,\s]*)?")                       # ... or after it
# Source-class nouns: systems, plural collections, knowledge words. Singular
# concrete things (a file, a meeting, a number) are deliberately absent so
# "do you have Tommy's phone number" / "do you know when the meeting is" stay
# ordinary questions.
_SOURCE_NOUN = (
    r"(?:knowledge(?:\s+base)?|kb|corpus|sources?|doors?|feeds?|folders?|files|docs|documents|"
    r"notes|captures?|transcripts|emails|inbox(?:es)?|mailbox(?:es)?|slack|channels|drive|gmail|"
    r"calendars?|fireflies|meetings|asana|notion|hubspot|shopify|memory|memories|canon|decisions|"
    r"decision\s+log|playbooks?|skills?|cowork|cascade|backfill|mirror|claude[- ]workspace|"
    r"ingest\w{0,6}|index\w{0,6}|sync\w{0,6}|access)"
)
_P_HAVE_KNOW = re.compile(
    _LEAD + r"(?:do|did|can|could|would)\s+you\s+(?:(?:still|also|now|already|actually|even|really)\s+){0,2}"
    r"(?:have|know|see|access|ingest|index|read|track|hold|store|sync|remember|cover|include|"
    r"search|retrieve|pull|reach)\b[^\n]{0,80}?\b" + _SOURCE_NOUN + r"\b",
    re.IGNORECASE,
)
_P_ARE_YOU = re.compile(
    _LEAD + r"are\s+you\s+(?:(?:still|also|now|already|actually|even)\s+){0,2}"
    r"(?:ingesting|indexing|syncing|synced|reading|tracking|pulling|watching|monitoring|sweeping|"
    r"capturing|mirroring|connected|hooked\s+up|plugged\s+in)\b",
    re.IGNORECASE,
)
_P_WHAT_SOURCES = re.compile(
    r"\b(?:what|which)\s+(?:sources|doors|folders|files|feeds|systems|channels|data|knowledge|"
    r"inboxes|mailboxes|drives)\s+(?:do|did|can|are|have)\s+you\b",
    re.IGNORECASE,
)
_P_IN_YOUR = re.compile(
    r"\b(?:is|are|was|were)\b[^\n]{0,80}?\bin\s+your\s+(?:kb|knowledge(?:\s+base)?|sources|index|"
    r"memory|corpus|brain)\b",
    re.IGNORECASE,
)
_P_HAVE_YOU = re.compile(
    _LEAD + r"have\s+you\s+(?:(?:already|ever|since|now)\s+)?(?:ingested|indexed|read|seen|synced|"
    r"captured|swept|mirrored|picked\s+up|pulled\s+in)\b",
    re.IGNORECASE,
)
_P_ACCESS_TO = re.compile(
    r"\b(?:do|did|don't|do\s+not)\s+you\s+(?:(?:still|also|now|already|actually|even|really)\s+){0,2}"
    r"have\s+access\s+to\b",
    re.IGNORECASE,
)

_INTENT_PATTERNS = (_P_HAVE_KNOW, _P_ARE_YOU, _P_WHAT_SOURCES, _P_IN_YOUR, _P_HAVE_YOU, _P_ACCESS_TO)


def is_self_inventory_question(text: str) -> bool:
    """True when *text* asks Cora about her OWN sources / doors / coverage."""
    if not text or len(text) > 2000:
        return False
    return any(p.search(text) for p in _INTENT_PATTERNS)


# ── live readers (each injectable for tests; each fail-soft) ─────────────────
_REGISTRY_CACHE: dict[str, Any] = {"at": 0.0, "rows": None}
_REGISTRY_TTL_S = 600
_REGISTRY_LOCK = threading.Lock()
_TASK_NAME_RE = re.compile(r"cora", re.IGNORECASE)


def read_task_registry(timeout: float = 12.0, *, now: float | None = None) -> list[dict[str, str]]:
    """Cora's scheduled tasks from the LIVE registry (``schtasks /Query /FO CSV /V``).

    Cached for 10 minutes (the registry changes rarely; a Slack turn must not pay
    a Task Scheduler round-trip every time). Empty list on any failure -- the
    inventory then says the registry was unavailable, never that there are no tasks.
    """
    now = time.time() if now is None else now
    with _REGISTRY_LOCK:
        rows = _REGISTRY_CACHE.get("rows")
        if rows is not None and now - float(_REGISTRY_CACHE.get("at", 0.0)) < _REGISTRY_TTL_S:
            return list(rows)
    if os.name != "nt":
        return []
    try:
        out = subprocess.run(
            ["schtasks", "/Query", "/FO", "CSV", "/V"],
            capture_output=True, text=True, timeout=timeout, creationflags=_NO_WINDOW,
            encoding="utf-8", errors="replace",
        ).stdout
    except Exception as exc:  # noqa: BLE001 -- best effort
        log.warning("self_inventory: schtasks query failed: %s", exc)
        return []
    rows = parse_schtasks_csv(out)
    with _REGISTRY_LOCK:
        _REGISTRY_CACHE["rows"] = list(rows)
        _REGISTRY_CACHE["at"] = now
    return rows


def parse_schtasks_csv(text: str) -> list[dict[str, str]]:
    """Parse ``schtasks /FO CSV /V`` output into compact dicts, Cora tasks only.
    The verbose listing repeats the header row per task -- those are skipped."""
    rows: list[dict[str, str]] = []
    if not text:
        return rows
    reader = csv.reader(io.StringIO(text))
    header: list[str] | None = None
    for rec in reader:
        if not rec or len(rec) < 5:
            continue
        if rec[0] == "HostName":
            header = rec
            continue
        if header is None or len(rec) != len(header):
            continue
        d = dict(zip(header, rec))
        name = str(d.get("TaskName", "")).lstrip("\\")
        if not _TASK_NAME_RE.search(name):
            continue
        rows.append({
            "name": name,
            "state": str(d.get("Scheduled Task State", "") or d.get("Status", "")),
            "status": str(d.get("Status", "")),
            "next_run": str(d.get("Next Run Time", "")),
            "last_run": str(d.get("Last Run Time", "")),
            "last_result": str(d.get("Last Result", "")),
            "schedule_type": str(d.get("Schedule Type", "")),
            "start_time": str(d.get("Start Time", "")),
            "days": str(d.get("Days", "")),
            "repeat_every": str(d.get("Repeat: Every", "")),
        })
    rows.sort(key=lambda r: r["name"].lower())
    return rows


def cadence_of(row: dict[str, str]) -> str:
    """One human phrase for a registry row's cadence."""
    st = (row.get("schedule_type") or "").strip()
    rep = (row.get("repeat_every") or "").strip()
    start = (row.get("start_time") or "").strip()
    days = (row.get("days") or "").strip()
    if rep and rep.upper() not in ("N/A", "DISABLED"):
        return f"every {rep}"
    if st.lower().startswith("daily"):
        return f"daily {start}".strip()
    if st.lower().startswith("weekly"):
        return f"weekly {days} {start}".strip()
    if st.lower().startswith("monthly"):
        return f"monthly {days} {start}".strip()
    if st:
        return f"{st} {start}".strip()
    return "unknown"


def read_gmail_watermarks(path: Path = GMAIL_WATERMARKS_PATH) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def read_parity_report(root: Path = FOUNDER_OS_ROOT, timeout: float = 3.0) -> dict[str, Any]:
    """Generated stamp + per-class mirrored counts from PARITY-REPORT.md (fail-soft)."""
    path = root / PARITY_REPORT_REL
    out: dict[str, Any] = {"path": str(path), "available": False}
    try:
        from cora import drive_io
        text = drive_io.read_text(str(path), timeout=timeout, retry_seconds=0.0)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    out["available"] = True
    m = re.search(r"_Generated ([^_]{5,80}?) by", text)
    if m:
        out["generated"] = m.group(1).strip()
    counts: dict[str, dict[str, str]] = {}
    in_table = False
    header: list[str] = []
    for line in text.splitlines():
        if line.startswith("| class |"):
            in_table = True
            header = [c.strip() for c in line.strip("|").split("|")]
            continue
        if in_table:
            if not line.startswith("|") or line.startswith("|---"):
                if not line.startswith("|"):
                    in_table = False
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) == len(header) and cells[0] != "class":
                counts[cells[0]] = {"mirrored": cells[1] if len(cells) > 1 else "?",
                                    "quarantined": cells[2] if len(cells) > 2 else "?"}
    out["classes"] = counts
    qm = re.search(r"Quarantined[^\n]*\n- total: (\d+)", text)
    if qm:
        out["quarantined_total"] = int(qm.group(1))
    return out


# ── inventory build ──────────────────────────────────────────────────────────
_SOURCE_DOOR_NOTES: dict[str, str] = {
    "static_md": "nightly 04:00 + midday 12:20 AZ walk of the Founder OS tree (.md + bootstrap.txt), entity by folder",
    "drive_sweep": "Drive files -- founders_os tree (entity by folder) + per-user flat sweeps",
    "drive_asset": "Drive file stubs (name/path/owner) for non-text files",
    "gmail": ("per-mailbox threaded sweep (roster in monitored-email-accounts.yaml); watermarks live in "
              "data/cache/gmail-thread-watermarks.json (listed below), not in the KB"),
    "slack": ("nightly channel sweep (bot-authored lines tagged, never canon); watermark kept by the "
              "channel-sweep task, not in the KB"),
    "fireflies": "meeting transcripts (COPA titles excluded)",
    "asana": "task sync",
    "notion": "Notion pages",
    "lex_dump_folder": "LEX dump folder (DDD/EVV manuals), daily 04:45 AZ",
    "user_note": "personal notes -- owner-only; EXCLUDED from every shared retrieval by construction",
}

_INGEST_TASK_HINT_RE = re.compile(
    r"kb-sync|sweep|capture|mirror|dump|extractor|harvest|static|gmail|slack|fireflies|asana|notion|"
    r"drive|ingest|hygiene|backfill|assets", re.IGNORECASE)


def _hours_ago(epoch: float | int | None, now: float) -> str:
    try:
        if not epoch:
            return "never"
        h = (now - float(epoch)) / 3600.0
        return f"{h:.0f}h ago" if h >= 1 else f"{h * 60:.0f}m ago"
    except Exception:  # noqa: BLE001
        return "?"


def build_inventory(
    *,
    kb: Any,
    kb_lock: Any = None,
    detail: bool,
    registry_reader: Callable[[], list[dict[str, str]]] = read_task_registry,
    markers_reader: Callable[[], dict[str, dict]] = run_marker.latest_by_task,
    gmail_reader: Callable[[], dict[str, Any]] = read_gmail_watermarks,
    parity_reader: Callable[[], dict[str, Any]] = read_parity_report,
    now: float | None = None,
) -> dict[str, Any]:
    """Assemble the inventory from live signals. Every section fail-soft."""
    now = time.time() if now is None else now
    inv: dict[str, Any] = {"detail": detail, "generated_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat()}

    # 1. KB doors + partitions + sync watermarks
    stats: dict[str, Any] = {}
    sync_states: dict[str, tuple[int, int | None]] = {}
    kb_err = None
    if kb is None:
        kb_err = "knowledge base unavailable"
    else:
        try:
            lock = kb_lock if kb_lock is not None else threading.Lock()
            with lock:
                stats = kb.stats() or {}
                lister = getattr(kb, "list_sync_states", None)
                sync_states = dict(lister()) if callable(lister) else {}
        except Exception as exc:  # noqa: BLE001
            kb_err = f"KB read error: {type(exc).__name__}"
    inv["kb_error"] = kb_err
    inv["total_chunks"] = int(stats.get("total_chunks", 0) or 0)
    inv["by_entity"] = {str(k): int(v) for k, v in (stats.get("by_entity") or {}).items()}
    doors: list[dict[str, Any]] = []
    for src, n in sorted((stats.get("by_source") or {}).items(), key=lambda kv: -int(kv[1])):
        door = {"source": str(src), "chunks": int(n), "note": _SOURCE_DOOR_NOTES.get(str(src), "")}
        marks = []
        for key, val in sorted(sync_states.items()):
            base_key = key.split("_")[0] if not key.startswith(("drive_sweep_", "founders_os_")) else key
            if key == src or (src == "drive_sweep" and key.startswith(("drive_sweep_", "founders_os_"))) \
               or (src == "drive_asset" and key == "drive_assets") or base_key == src:
                marks.append((key, val[0] if isinstance(val, (tuple, list)) and val else None))
        if marks:
            fresh = [m for m in marks if m[1]]
            door["watermarks"] = len(marks)
            door["watermarks_fresh"] = len(fresh)
            door["newest_sync"] = _hours_ago(max((m[1] for m in fresh), default=None), now)
            if detail:
                door["watermark_keys"] = [f"{k} ({_hours_ago(v, now)})" for k, v in marks]
        doors.append(door)
    inv["doors"] = doors
    gm = gmail_reader() or {}
    if gm:
        inv["gmail_accounts"] = len(gm)
        fresh = [v for v in gm.values() if isinstance(v, (int, float)) and v > 0]
        inv["gmail_newest_sync"] = _hours_ago(max(fresh), now) if fresh else "unknown"
        if detail:
            # the sweep file stores epoch seconds per mailbox; render them as ages
            inv["gmail_watermarks"] = {
                str(k): (_hours_ago(v, now) if isinstance(v, (int, float)) else str(v)[:19])
                for k, v in sorted(gm.items())
            }

    # 2. Scheduled tasks (live registry) + run markers
    try:
        registry = registry_reader() or []
    except Exception:  # noqa: BLE001
        registry = []
    try:
        markers = markers_reader() or {}
    except Exception:  # noqa: BLE001
        markers = {}
    tasks: list[dict[str, Any]] = []
    for row in registry:
        name = row.get("name", "")
        t: dict[str, Any] = {
            "name": name, "state": row.get("state", ""), "cadence": cadence_of(row),
            "next_run": row.get("next_run", ""), "last_run": row.get("last_run", ""),
            "last_result": row.get("last_result", ""),
            "ingest": bool(_INGEST_TASK_HINT_RE.search(name)),
        }
        mk = markers.get(name)
        if mk:
            t["marker"] = {"ts": str(mk.get("ts", ""))[:19], "ok": mk.get("ok"), "outputs": mk.get("outputs")}
        tasks.append(t)
    inv["tasks"] = tasks
    inv["registry_available"] = bool(registry)

    # 3. Exclusions (the module of record: kb_exclusions)
    labels = getattr(kb_exclusions, "KB_EXCLUDED_FOLDER_LABELS", {})
    walk_only = getattr(kb_exclusions, "KB_EXCLUDED_WALK_ONLY_IDS", frozenset())
    inv["excluded_folders"] = [
        {"id": fid, "label": labels.get(fid, "(unlabelled pin)"),
         "kind": "computers-backup root (ancestry walk)" if fid in walk_only else "pinned folder (subtree pruned)"}
        for fid in sorted(kb_exclusions.KB_EXCLUDED_FOLDER_IDS, key=lambda f: labels.get(f, f))
    ]
    inv["excluded_path_segments"] = {
        "dashboard stores": sorted(kb_exclusions._DASHBOARD_STORE_SEGMENTS),
        "finance working stores": sorted(kb_exclusions._FINANCE_WORKSHEET_SEGMENTS),
        "LEX NDA project": sorted(kb_exclusions._LEX_NDA_SEGMENTS),
        "Cora build workspace (D-057)": ["/".join(kb_exclusions._CORA_WORKSPACE_SEGMENTS)],
        "static-tree skips": ["_brain/swept", "_delegated-work", "_archive", "dot-dirs", "PHI segments (consumers/clients/phi/clinical/ehr)"],
    }
    inv["title_belts"] = [
        "Cora build/audit docs: a `cora-` token + a build keyword (forensic/rebuild/audit/review/cascade/mirror/quarantine ...)",
        "Generated finance files: date-prefixed forecast/actuals/cashflow-worksheet names, forecast-assist",
        "NDA'd COPA meetings: whole-word `copa` in a meeting title",
        "Banking identifiers: routing/SWIFT/IBAN/account values are REDACTED at every chunk egress (deep link kept)",
    ]
    inv["allowlisted_views"] = sorted(kb_exclusions._KB_ALLOWLIST_BASENAMES)
    inv["drive_skip_folder_names"] = sorted(_drive_skip_names())

    # 4. Mirror parity + canonical pointer
    try:
        inv["parity"] = parity_reader() or {"available": False}
    except Exception as exc:  # noqa: BLE001
        inv["parity"] = {"available": False, "error": str(exc)}
    inv["cowork_cascade_pointer"] = COWORK_CASCADE_POINTER
    return inv


def _drive_skip_names() -> frozenset[str]:
    try:
        from cora.connectors import drive_sweep
        return frozenset(drive_sweep._FOUNDERS_OS_SKIP_FOLDERS)
    except Exception:  # noqa: BLE001
        return frozenset()


# ── rendering ────────────────────────────────────────────────────────────────
def render_inventory(inv: dict[str, Any]) -> str:
    detail = bool(inv.get("detail"))
    lines: list[str] = ["Cora self-inventory (live signals; deterministic -- not retrieved from the KB):"]
    if inv.get("kb_error"):
        lines.append(f"- Knowledge base: {inv['kb_error']}")
    else:
        lines.append(f"- Knowledge base: {inv.get('total_chunks', 0):,} chunks.")
    lines.append("")
    lines.append("INGEST DOORS (source | chunks | last sync the KB knows | how it arrives):")
    for d in inv.get("doors", []):
        sync = d.get("newest_sync", "no watermark in KB")
        wm = f" [{d.get('watermarks_fresh', 0)}/{d.get('watermarks', 0)} watermarks fresh]" if d.get("watermarks") else ""
        lines.append(f"- {d['source']} | {d['chunks']:,} | {sync}{wm} | {d.get('note', '')}")
        if detail and d.get("watermark_keys"):
            lines.append("    keys: " + "; ".join(d["watermark_keys"][:40]))
    if inv.get("gmail_accounts"):
        lines.append(f"- gmail sweep watermark file: {inv['gmail_accounts']} mailboxes tracked, newest sync "
                     f"{inv.get('gmail_newest_sync', 'unknown')}"
                     + (" -- " + "; ".join(f"{k} {v}" for k, v in list(inv.get("gmail_watermarks", {}).items())[:40]) if detail else ""))
    lines.append("")
    lines.append("ENTITY PARTITIONS (chunks): " + ", ".join(f"{k} {v:,}" for k, v in inv.get("by_entity", {}).items()))
    lines.append("")
    excl = inv.get("excluded_folders", [])
    if detail:
        lines.append("PINNED EXCLUSIONS -- never ingested through the Drive door (folder id | label):")
        for f in excl:
            lines.append(f"- {f['id']} | {f['label']} | {f['kind']}")
        lines.append("Path-segment exclusions (static_md door): "
                     + "; ".join(f"{k}: {', '.join(v)}" for k, v in inv.get("excluded_path_segments", {}).items()))
    else:
        # A non-founder channel learns THAT exclusions exist and of what kinds --
        # never the folder names or ids: "capital-raise (HIGHLY CONFIDENTIAL)" is
        # itself a fact a teammate is not cleared for. The generic answer to "do you
        # have X" for an excluded X is "not in my sources", not a confirmation.
        n_roots = sum(1 for f in excl if "computers-backup" in f["kind"])
        lines.append(f"PINNED EXCLUSIONS: {len(excl)} Drive folders are excluded by design (personal and "
                     f"confidential stores, an NDA project, finance working stores, the Cora build workspace, "
                     f"{n_roots} PC backup roots) plus the static-tree skips (_brain/swept, _delegated-work, "
                     f"_archive, PHI segments). Folder names and ids are founder-level.")
    lines.append("Drive subtree names skipped in the Founder-OS walk: " + ", ".join(inv.get("drive_skip_folder_names", [])))
    lines.append("Title belts: " + " | ".join(inv.get("title_belts", [])))
    lines.append("Allowlisted views ingested despite the Cora-workspace exclusion: " + ", ".join(inv.get("allowlisted_views", [])))
    lines.append("")
    tasks = inv.get("tasks", [])
    if not inv.get("registry_available"):
        lines.append("SCHEDULED TASKS: live registry unavailable this turn (say so; do not guess cadences).")
    else:
        shown = tasks if detail else [t for t in tasks if t.get("ingest")]
        lines.append(f"SCHEDULED TASKS ({len(shown)} of {len(tasks)} Cora tasks{'' if detail else ' -- ingest lanes only in this channel'}; name | state | cadence | next | last result | last wrote):")
        for t in shown:
            mk = t.get("marker") or {}
            wrote = f"{mk.get('ts', '')} outputs={mk.get('outputs')}" if mk else "no run marker"
            lines.append(f"- {t['name']} | {t['state']} | {t['cadence']} | next {t['next_run']} | last result {t['last_result']} | {wrote}")
    lines.append("")
    p = inv.get("parity", {}) or {}
    if p.get("available"):
        cls = ", ".join(f"{k} {v.get('mirrored')} mirrored/{v.get('quarantined')} quarantined" for k, v in (p.get("classes") or {}).items())
        lines.append(f"CLAUDE-WORKSPACE MIRROR: PARITY-REPORT.md generated {p.get('generated', '?')} -- {cls}"
                     + (f"; quarantined total {p['quarantined_total']}" if "quarantined_total" in p else "")
                     + f" ({p.get('path')})")
    else:
        lines.append(f"CLAUDE-WORKSPACE MIRROR: PARITY-REPORT.md not readable this turn ({p.get('error', 'absent')}) -- cite it by path: {p.get('path', PARITY_REPORT_REL)}")
    lines.append("")
    lines.append(inv.get("cowork_cascade_pointer", COWORK_CASCADE_POINTER))
    lines.append("")
    lines.append(REPLY_FORMAT)
    if not detail:
        lines.append("(Mailbox addresses, watermark keys and the non-ingest task list are founder-level -- ask in a founder channel.)")
    return "\n".join(lines)
