"""Deterministic self-inventory: WHAT Cora ingests, through WHICH doors, what is
EXCLUDED, and her own scheduled cadence -- read from live signals, never from
the knowledge base (ingest-integrity bundle I4, cq-3542e1b095b2, 2026-09-08;
D-051 lens-E remediation the same day).

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
    sync watermarks the KB records for it (``sync_state``; a watermark counts as
    FRESH only inside 48h -- a 97-day-old founders_os key used to read as fresh),
    plus the gmail per-account watermark file;
  * LIVE TOOL CONNECTORS -- the tool families offered in the asker's channel
    (HubSpot, Asana, QBO, Calendar, Shopify ...). These are NOT knowledge-base
    sources; the D-051 review showed the first cut turned "can you access
    HubSpot?" into "HubSpot is not one of my sources" while hubspot_* tools sat
    in the model's own tool list;
  * her SCHEDULED TASKS -- the live Task Scheduler registry (``schtasks
    /Query /FO CSV /V``, Cora tasks only, cached 10 min) with next/last run,
    cadence and last result, overlaid with the run-marker ledger
    (``logs/task-runs.jsonl``: when it last actually WROTE something);
  * PINNED EXCLUSIONS -- every ``KB_EXCLUDED_FOLDER_IDS`` id with its label
    (the Drive door), the path-segment exclusions (the static_md door), the
    name-skipped subtree folders, the title belts, and the allowlisted views;
  * ENTITY PARTITIONS -- chunk counts per entity;
  * the claude-workspace MIRROR parity report (path + generated stamp + per-class
    mirrored counts) and the canonical pointer for the Cowork / Cascade question
    -- rendered as an affirmation ONLY while the static_md door and a mirror
    signal are both live; otherwise as "the doors exist by design, no live
    signal this turn" (a constant "YES" would reproduce the 9/4 mode the moment
    a lane went dark).

SCOPE. ``detail=True`` (founder channels + Harrison) lists mailbox addresses,
folder ids, file paths and every task; other channels get door names, counts,
the asker's own partition and the ingest lanes only -- the inventory never
becomes a per-user roster leak, and an NDA'd project's name never reaches an
unrelated channel.

ROUTING. ``is_self_inventory_question`` is a grammar for questions about
SOURCES and INGESTION -- "do you have access to the Cowork Cascade knowledge",
"are you ingesting the session captures", "what's in your knowledge base" --
and deliberately NOT for questions about CONTENT ("do you have the EVV docs",
"have you seen the Sprouts contract", "what data do you have on Kroger") or
about LIVE SYSTEMS ("can you access HubSpot"): the object must be a source
zone or the knowledge base itself, an object followed by a specifier tail
("... transcript FROM the Gotham call", "... thread WHERE Matt approved") is
content, and an imperative write ("complete the task -- the deck is in your
knowledge base right") is never a meta-question. The D-051 lens-E measurement
of the first cut: 27 of 50 realistic asks hijacked, 17 of 28 acceptance
phrasings missed, 7 staged-write / Asana turns displaced.

WIRING. ``app._dispatch_qa`` forces the ``cora_self_inventory`` tool (F-23
tool_choice pattern) when the predicate matches -- BELOW the code-queue,
delegate, staged-write and Asana forces (an explicit command or write always
wins), never on a Tier-2 retrieval-grant turn, and the semantic-cache READ is
bypassed for the turn so a cached non-inventory answer cannot pre-empt it.
Normal KB retrieval still runs (the inventory says which DOOR, the retrieval
says WHAT). The tool's output carries the reply-format rule: a miss is "not in
my sources" with the door named, never "I don't have that knowledge".
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

#: A sync watermark older than this is STALE (the founders_os door had 66-97 day
#: old keys that the first cut counted as "fresh" because they were non-null).
FRESH_HOURS = 48

#: The canonical answer for the question class that produced this tool -- rendered
#: ONLY while both doors show a live signal (see COWORK_CASCADE_POINTER_DARK).
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
COWORK_CASCADE_POINTER_DARK = (
    "For 'do you have the Cowork / Cascade / Claude-workspace knowledge': the two doors "
    "that carry it BY DESIGN are the static_md sync of the Founder OS tree and the "
    "claude-workspace mirror under _shared/claude-workspace-mirror/ -- but this turn shows "
    "NO live signal for {which}. Say the doors exist by design and that you cannot confirm "
    "they are live right now; do NOT affirm with specifics, do NOT deny having the "
    "knowledge. Cite PARITY-REPORT.md by path for counts."
)

REPLY_FORMAT = (
    "REPLY FORMAT (binding): answer 'do you have / do you know about / are you ingesting X' "
    "from THIS inventory, not from retrieved chunks. This inventory lists KNOWLEDGE-BASE "
    "doors; the LIVE TOOL CONNECTORS line lists systems you reach through tools (HubSpot, "
    "Asana, QuickBooks, Calendar, Shopify ...) -- if X is one of those, the answer is YES "
    "via that tool, never 'not in my sources'. If X names a source, folder, channel or "
    "document class: say which DOOR it arrives through (or that it is EXCLUDED by design, "
    "naming the exclusion). If X is a fact and the retrieved knowledge below has nothing: "
    "say 'not in my sources' and name the doors that WOULD carry it -- never say 'I don't "
    "have that knowledge' from a search miss, never invent a source, never mention chunk "
    "numbers or context blocks. Cite PARITY-REPORT.md for mirror counts. Do not queue a "
    "code session for a question about your own sources."
)

# ── routing predicate (unit-tested in tests/test_self_inventory.py) ──────────
# Every quantified class is bounded; no nested quantifiers (ReDoS discipline).
_LEAD = (r"^\s*(?:<@[A-Z0-9]{1,20}>\s*[,:\-]?\s*)?"
         r"(?:(?:hey|hi|hello|yo|ok|okay|so)[,!\s]*)?"           # greeting before the name ...
         r"(?:@?cora\b[\s,:!.\-\u2014\u2013]*)?"                 # ... the name + any punctuation ...
         r"(?:(?:hey|hi|hello|ok|okay|so)[,!\s]*)?"              # ... or after it ...
         r"(?:(?:quick\s+(?:question|q)|question|one\s+more(?:\s+thing)?)[\s,:!.\-\u2014\u2013]*)?")
_YOU = r"(?:you|u|ya)"
_ADV = r"(?:(?:still|also|now|already|actually|even|really|currently)\s+){0,2}"
# SOURCE-ZONE nouns: systems, collections, knowledge words -- things that ARE a
# source, never a concrete item ("the notes", "the emails about X", "the EVV docs"
# are content questions answered by retrieval). Live tool systems (hubspot /
# asana / qbo / shopify / calendar / web) are deliberately ABSENT: the model has
# those tools in its list and answers "can you access HubSpot" from it.
_SOURCE_NOUN = (
    r"(?:knowledge(?:\s+base)?|kb|corpus|index|sources?|doors?|feeds?|inputs?|"
    r"folders?|founder[- ]?(?:os|drive)|drive|mailbox(?:es)?|inbox(?:es)?|"
    r"(?:my|our|the\s+team'?s?)\s+emails?|email\s+(?:sweep|history|archive)|"
    r"slack|channels?\s+(?:sweep|history)|fireflies|gmail|notion|"
    r"session[- ]captures?|captures|transcripts|playbooks?|skills?|memory|memories|canon|"
    r"decisions?\s+log|cowork|cascade|mirror|claude[- ]workspace|"
    r"backfill|ingest\w{0,6}|index\w{0,6}|sync\w{0,6}|sweeps?|"
    r"computers?\s+backups?|backups?|downloads|desktop|"
    r"(?:cowork|cascade|claude|code|session)\s+(?:stuff|material|knowledge|docs|notes|history))"
)
# An object followed by a specifier is a CONTENT question ("the fireflies transcript
# FROM the Gotham call", "the slack thread WHERE Matt approved the PO"), and so is an
# object followed by a WORK verb ("read the files in the Founder-OS folder AND
# SUMMARIZE them" -- D-051 Code #13 review AD-2: the folder noun alone routed that
# content request to the inventory dump).
_TAIL = (
    r"(?![^\n]{0,40}?\b(?:from|about|for|re|regarding|on|where|when|that|which|of|between|"
    r"and\s+(?:then\s+)?(?:summari[sz]e|tell|give|send|draft|write|list|pull|make|let|explain|compare|"
    r"put|post|share|email|dm|create|update|flag|note|report|find|check))\b)"
)
_P_HAVE_KNOW = re.compile(
    _LEAD + r"(?:do|did|can|could|would|don't|dont|do\s+not)\s+" + _YOU + r"\s+" + _ADV
    + r"(?:have|know\s+(?:about|of)|know|see|access|ingest|index|read|hold|store|sync|remember|"
    r"cover|include|search|retrieve|pull|reach|get|track)\b[^\n]{0,60}?\b" + _SOURCE_NOUN + r"\b" + _TAIL,
    re.IGNORECASE,
)
_P_ARE_YOU = re.compile(
    _LEAD + r"are\s+" + _YOU + r"\s+" + _ADV
    + r"(?:ingesting|indexing|syncing|synced\s+(?:to|with)|sweeping|mirroring|capturing|harvesting|"
    r"pulling\s+in|picking\s+up|reading\s+from|connected\s+to|hooked\s+up\s+to|plugged\s+in(?:to)?|"
    r"watching|tracking|monitoring)\b[^\n]{0,60}?\b" + _SOURCE_NOUN + r"\b" + _TAIL,
    re.IGNORECASE,
)
_P_WHAT_SOURCES = re.compile(
    r"\b(?:what|which)\s+(?:(?:knowledge|data)\s+sources|sources|doors|folders|feeds|systems|inboxes|"
    r"mailboxes|drives|(?:slack\s+)?channels)\s+(?:do|did|can|are|have|does)\s+" + _YOU + r"\s+" + _ADV
    + r"(?:have|ingest\w*|index\w*|read|sweep\w*|sync\w*|pull|see|know|cover|track|monitor|get|access|use|"
    r"draw)\b",
    re.IGNORECASE,
)
_P_YOUR_SOURCES = re.compile(
    r"\b(?:what|which|where)\s+(?:are|is|do|does|did)\s+(?:all\s+)?your\s+(?:knowledge|kb|knowledge\s+base|"
    r"corpus|index|sources|doors|feeds|inputs|data\s+sources)\b",
    re.IGNORECASE,
)
_P_IN_YOUR = re.compile(
    r"\b(?:is|are|was|were|isn't|aren't|what's|whats|what\s+is|what\s+are|does|do)\b[^\n]{0,80}?"
    r"\b(?:in|inside|part\s+of|available\s+(?:in|to))\s+your\s+(?:kb|knowledge(?:\s+base)?|sources|index|"
    r"memory|corpus|brain|training)\b",
    re.IGNORECASE,
)
_P_YOUR_KNOWLEDGE_INCLUDES = re.compile(
    r"\b(?:does|do|did|will|would)\s+your\s+(?:knowledge|kb|knowledge\s+base|sources|index|corpus|memory)\s+"
    r"(?:include|cover|have|contain|reach|extend\s+to|know\s+about)\b",
    re.IGNORECASE,
)
_P_AVAILABLE_TO_YOU = re.compile(
    r"\b(?:is|are|was|were)\b[^\n]{0,80}?\b(?:available|visible|accessible|exposed)\s+to\s+" + _YOU + r"\b",
    re.IGNORECASE,
)
_P_HAVE_YOU = re.compile(
    _LEAD + r"(?:have|had|haven't|havent)\s+" + _YOU + r"\s+(?:(?:already|ever|since|now|actually)\s+)?"
    r"(?:got|been\s+given|been\s+fed|ingested|indexed|read|seen|synced|captured|swept|mirrored|picked\s+up|"
    r"pulled\s+in|got\s+access\s+to|gained\s+access\s+to)\b[^\n]{0,60}?\b" + _SOURCE_NOUN + r"\b" + _TAIL,
    re.IGNORECASE,
)
_P_ACCESS_TO = re.compile(
    r"\b(?:do|did|don't|dont|do\s+not|can|could|can't|cant)\s+" + _YOU + r"\s+" + _ADV
    + r"(?:have\s+access\s+to|access|get\s+(?:to|at|into)|see\s+into|reach|read)\b[^\n]{0,60}?\b"
    + _SOURCE_NOUN + r"\b" + _TAIL,
    re.IGNORECASE,
)
_P_TAG = re.compile(
    r"\b" + _YOU + r"\s+(?:don't|dont|do\s+not|didn't|never)\s+(?:have|see|get|ingest|index|read)\b[^\n]{0,60}?\b"
    + _SOURCE_NOUN + r"\b[^\n]{0,25}?\b(?:do|did)\s+" + _YOU + r"\b",
    re.IGNORECASE,
)
_P_WHICH_OF = re.compile(
    r"\bwhich\s+of\s+(?:the|these|those|our|all)\b[^\n]{0,50}?\b" + _SOURCE_NOUN + r"\b[^\n]{0,40}?\bcan\s+"
    + _YOU + r"\s+(?:see|access|read|reach|search|use)\b",
    re.IGNORECASE,
)
# An IMPERATIVE WRITE is never a meta-question, however the sentence continues
# ("complete the task -- the deck is in your knowledge base right"). Same
# start-anchored shape as the staged-write / Asana detectors it must yield to.
_IMPERATIVE_WRITE_RE = re.compile(
    _LEAD + r"(?:please\s+|pls\s+|can\s+you\s+|could\s+you\s+|go\s+ahead\s+and\s+)?"
    r"(?:complete|mark|delete|remove|create|add|make|dm|slack|message|msg|draft|compose|write|send|email|"
    r"remember|note|schedule|book|update|move|assign|queue|delegate|close|reopen|archive|post|reply|forward|"
    r"set|change|rename|log|record|save|cancel|finish|ship|file)\b",
    re.IGNORECASE,
)

# Code #13 slice 1, routing half (cq-2e02f1fd0f65; ruled 2026-09-10): "can you
# access / see / reach / read <connector | tool | folder>" JOINS the force.
# This REVERSES the lens-E #2 choice above ("the model has those tools in its list
# and answers 'can you access HubSpot' from it"): the 9/10 founder-DM transcript
# shows the model DENYING capabilities it had, so the answer is now inventory-
# derived -- the tool renders live connectors as NOT-sources and names the door.
# The imperative-write exclusion still runs first, so "send the Shopify file to
# Larry" is never hijacked.
#
# SHAPE (D-051 Code #13 review AD-2): the connector noun must END the clause. The
# first cut had no tail at all, so ordinary TOOL-USE asks were forced to the
# inventory ("can you see my calendar FOR FRIDAY?", "can you query QBO FOR OSN'S Q2
# REVENUE?", "can you open the Asana TASK ...", "can you use the web TO FIND ...")
# -- and because a forced tool sets web_gate_skip, the web was withheld on the very
# phrase that asked for it. "QBO for OSN" stays a meta-question: the ruled entity
# qualifier is admitted EXPLICITLY (an entity code, never "for Friday"), which is
# why this is a clause-END requirement and not the generic _TAIL specifier guard.
# The generic tool-use verbs (use / query / pull from / get into / hit / open) are
# gone from these two patterns: with an object clause they are ASKS, and without
# one the access / see / reach / read forms already cover the meta-question.
# Content-object nouns (file / folder / document / spreadsheet / sheet) are gone
# from the connector class: they are the model's to read, and "can you read the
# files in that folder?" still routes through _P_HAVE_KNOW's folder SOURCE noun.
_CONNECTOR_NOUN = (
    r"(?:hubspot|asana|quickbooks(?:\s+online)?|qbo|shopify|(?:google\s+)?calendar|deposco|klaviyo|"
    r"make(?:\.com)?|notion|(?:the\s+)?web|web\s+search|(?:live\s+)?tools?|connectors?|integrations?|apis?|"
    r"(?:google\s+)?drive)"
)
# "QBO for OSN" / "HubSpot for F3E" -- an ENTITY code after the connector is a
# qualifier on the connector, never a content object (ruled 2026-09-10).
_CONNECTOR_ENTITY_QUALIFIER = (
    r"(?:\s+for\s+(?:the\s+)?(?:osn(?:g[wmf]|vv)?|f3e?|f3c|lex(?:-(?:llc|lts|lbhs|lla))?|llc|lts|lbhs|lla|ufl|"
    r"bdm|hjrp(?:-\w{1,6})?|hjrg|hjr|fndr|hjrprod|pod|hrllc|pure|lexington|founder))?"
)
# The clause END: an optional bounded adverbial, then punctuation / end of line /
# "or|and <another connector>". Anything else after the noun is an object clause
# and the message stays with the model + its tool list.
_CONNECTOR_CLAUSE_END = (
    _CONNECTOR_ENTITY_QUALIFIER
    + r"(?:\s+(?:at\s+all|right\s+now|yet|now|today|still|too|as\s+well|directly|live|from\s+here|here|"
    r"for\s+me|on\s+your\s+end))?"
    r"\s*(?:$|[?!.,;:)\n]|\s+(?:or|and)\s+(?:the\s+|our\s+|my\s+)?" + _CONNECTOR_NOUN + r"\b)"
)
_P_CAN_ACCESS_CONNECTOR = re.compile(
    _LEAD + r"(?:can|could|do|did|don't|dont|do\s+not|can't|cant|will|would)\s+" + _YOU + r"\s+" + _ADV
    + r"(?:have\s+access\s+to|access|see|reach|read|connect\s+to|talk\s+to|log\s+into|touch|work\s+with)\s+"
    r"(?:the\s+|our\s+|my\s+|your\s+|any\s+|all\s+(?:the\s+)?|those\s+|these\s+|that\s+)?"
    + _CONNECTOR_NOUN + r"\b" + _CONNECTOR_CLAUSE_END,
    re.IGNORECASE,
)
_P_ABLE_TO_ACCESS_CONNECTOR = re.compile(
    _LEAD + r"are\s+" + _YOU + r"\s+" + _ADV
    + r"(?:able\s+to|allowed\s+to|permitted\s+to|set\s+up\s+to|wired\s+(?:up\s+)?to|connected\s+to|"
    r"hooked\s+up\s+to|plugged\s+into|integrated\s+with)\s+(?:(?:access|see|reach|read)\s+)?"
    r"(?:the\s+|our\s+|my\s+)?" + _CONNECTOR_NOUN + r"\b" + _CONNECTOR_CLAUSE_END,
    re.IGNORECASE,
)

_INTENT_PATTERNS = (
    _P_HAVE_KNOW, _P_ARE_YOU, _P_WHAT_SOURCES, _P_YOUR_SOURCES, _P_IN_YOUR, _P_YOUR_KNOWLEDGE_INCLUDES,
    _P_AVAILABLE_TO_YOU, _P_HAVE_YOU, _P_ACCESS_TO, _P_TAG, _P_WHICH_OF,
    _P_CAN_ACCESS_CONNECTOR, _P_ABLE_TO_ACCESS_CONNECTOR,
)


def is_self_inventory_question(text: str) -> bool:
    """True when *text* asks Cora about her OWN sources / doors / coverage."""
    if not text or len(text) > 2000:
        return False
    if _IMPERATIVE_WRITE_RE.search(text):
        return False
    return any(p.search(text) for p in _INTENT_PATTERNS)


# ── live readers (each injectable for tests; each fail-soft) ─────────────────
_REGISTRY_CACHE: dict[str, Any] = {"at": 0.0, "rows": None}
_REGISTRY_TTL_S = 600
_REGISTRY_LOCK = threading.Lock()
# "Cora - X", "cowork-cora-x", "\cora-watchdog" -- never "Decorator Update Task"
_TASK_NAME_RE = re.compile(r"(?:^|[\s\-_\\])cora(?:$|[\s\-_])", re.IGNORECASE)


def read_task_registry(timeout: float = 12.0, *, now: float | None = None) -> list[dict[str, str]]:
    """Cora's scheduled tasks from the LIVE registry (``schtasks /Query /FO CSV /V``).

    Cached for 10 minutes (the registry changes rarely; a Slack turn must not pay
    a Task Scheduler round-trip every time). The spawn runs INSIDE the lock so two
    concurrent cold calls cost one query, not two. Empty list on any failure --
    the inventory then says the registry was unavailable, never that there are no
    tasks.
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
        _REGISTRY_CACHE["rows"] = list(rows)
        _REGISTRY_CACHE["at"] = now
        return rows


def parse_schtasks_csv(text: str) -> list[dict[str, str]]:
    """Parse ``schtasks /FO CSV /V`` output into compact dicts, Cora tasks only.
    The verbose listing repeats the header row per task -- those are skipped -- and
    lists a task once PER TRIGGER, so a two-trigger task is folded to one row
    (``triggers`` counts them; the earliest next run is kept)."""
    rows: dict[str, dict[str, str]] = {}
    if not text:
        return []
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
        row = {
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
            "triggers": "1",
        }
        prev = rows.get(name)
        if prev is None:
            rows[name] = row
        else:
            prev["triggers"] = str(int(prev.get("triggers", "1")) + 1)
            # a second trigger adds a second start time to the cadence phrase
            if row["start_time"] and row["start_time"] not in prev["start_time"]:
                prev["start_time"] = f"{prev['start_time']} + {row['start_time']}".strip(" +")
    out = list(rows.values())
    out.sort(key=lambda r: r["name"].lower())
    return out


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


# Live tool families, by tool-name shape. Read from tool_dispatch.tools_for_entity
# at call time so the list is exactly what the model is offered in that channel.
_TOOL_FAMILIES: tuple[tuple[str, Callable[[str], bool]], ...] = (
    ("HubSpot CRM (live deals / contacts)", lambda n: "hubspot" in n),
    ("Asana (live tasks)", lambda n: n.startswith("asana_")),
    ("QuickBooks Online (live P&L / balance sheet / AR-AP)", lambda n: n.startswith("qbo_")),
    ("Google Calendar (live events)", lambda n: n.startswith("calendar_")),
    ("Gmail (own-inbox reads / drafts)", lambda n: n.startswith("gmail_")),
    ("Cash-flow sheets (live weekly forecast)", lambda n: n.startswith("financial_")),
    ("Shopify (live orders / inventory)", lambda n: "shopify" in n),
    ("Deposco warehouse (live)", lambda n: "deposco" in n),
    ("Ads performance (live)", lambda n: n.startswith("ads_")),
    ("Influencer / fighter tracker (live)", lambda n: n.startswith(("influencer_", "fighter_"))),
    ("Slack DM send (staged)", lambda n: n == "slack_send_dm"),
    ("Personal notes (owner-only store)", lambda n: n in ("cora_remember", "cora_my_notes")),
)
_WEB_CONNECTOR_NOTE = "Web search / fetch (gated per turn; explicit web intent; off in LEX by default)"


def live_tool_families(entity: str | None) -> list[str]:
    """Human labels for the tool families offered in *entity*'s channel (fail-soft)."""
    try:
        from cora.tools import tool_dispatch as td
        names = {t["name"] for t in td.tools_for_entity((entity or "FNDR").upper())}
    except Exception as exc:  # noqa: BLE001
        log.warning("self_inventory: tool list unavailable: %s", exc)
        return []
    fams = [label for label, pred in _TOOL_FAMILIES if any(pred(n) for n in names)]
    fams.append(_WEB_CONNECTOR_NOTE)
    return fams


# ── inventory build ──────────────────────────────────────────────────────────
_SOURCE_DOOR_NOTES: dict[str, str] = {
    "static_md": "nightly 04:00 + midday 12:20 AZ walk of the Founder OS tree (.md + bootstrap.txt), entity by folder",
    "drive_sweep": ("Drive files -- founders_os tree (entity by folder) + per-user flat sweeps (markdown inside the "
                    "Founder OS tree left to static_md); an account may be pinned to ALLOWLIST-BY-FOLDER mode "
                    "(D-303: only its allowlisted tree is ingested, everything else skipped + counted; see the "
                    "drive-sweep modes line)"),
    "drive_asset": "Drive file stubs (name/path/owner) for non-text files",
    "gmail": ("per-mailbox threaded sweep (roster in monitored-email-accounts.yaml); watermarks live in "
              "data/cache/gmail-thread-watermarks.json (listed below), not in the KB"),
    "slack": ("nightly channel sweep (bot-authored lines tagged, never canon); watermark kept by the "
              "channel-sweep task, not in the KB"),
    "fireflies": "meeting transcripts (one NDA'd project's meetings excluded by title)",
    "asana": "task sync",
    "notion": "Notion pages",
    "lex_dump_folder": "LEX dump folder (DDD/EVV manuals), daily 04:45 AZ",
    "user_note": "personal notes -- owner-only; EXCLUDED from every shared retrieval by construction",
}
_PATH_TOKEN_RE = re.compile(r"\S+\.(?:json|yaml|yml|md|txt)\b")

# Ingest lanes by NAME: a positive family AND no writer/notifier token. The first
# cut's single hint regex ("asana|sweep|capture ...") admitted the Asana nudge
# writer, the revops send lane, the decision-capture inbox and the coverage DM
# nudges as "ingest lanes" while missing the attachment filer and the
# inventory-state sync (D-051 lens E #7).
_INGEST_TASK_RE = re.compile(
    r"kb-sync|drive[ -]sweep|founders-os|session-capture|claude-mirror|channel-sweep|dump[ -]folder|"
    r"attachment[ -]filer|inventory-state-sync|drive-extractor|backfill|kb-hygiene|assets|static|ingest|harvest",
    re.IGNORECASE,
)
_NON_INGEST_TASK_RE = re.compile(
    r"nudge|revops|decision-capture|coverage|completion-sweep|digest|briefing|synthesis|memo|report|alert|"
    r"monitor|health|scan|review|autofill|reconciliation|knowledge-check|deliverable|metrics|token|backup|"
    r"compaction|watchdog|service|security|pulse|spy|linkedin|influencer",
    re.IGNORECASE,
)


def is_ingest_task(name: str) -> bool:
    return bool(_INGEST_TASK_RE.search(name)) and not _NON_INGEST_TASK_RE.search(name)


def _hours_ago(epoch: float | int | None, now: float) -> str:
    try:
        if not epoch:
            return "never"
        h = (now - float(epoch)) / 3600.0
        return f"{h:.0f}h ago" if h >= 1 else f"{h * 60:.0f}m ago"
    except Exception:  # noqa: BLE001
        return "?"


def _is_fresh(epoch: Any, now: float) -> bool:
    try:
        return bool(epoch) and (now - float(epoch)) <= FRESH_HOURS * 3600
    except (TypeError, ValueError):
        return False


def build_inventory(
    *,
    kb: Any,
    kb_lock: Any = None,
    detail: bool,
    entity: str | None = None,
    registry_reader: Callable[[], list[dict[str, str]]] | None = None,
    markers_reader: Callable[[], dict[str, dict]] | None = None,
    gmail_reader: Callable[[], dict[str, Any]] | None = None,
    parity_reader: Callable[[], dict[str, Any]] | None = None,
    connectors_reader: Callable[[str | None], list[str]] | None = None,
    sweep_modes_reader: Callable[[], list[dict[str, Any]]] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Assemble the inventory from live signals. Every section fail-soft.

    The readers default to the module functions AT CALL TIME (not at def time),
    so a test that monkeypatches ``self_inventory.read_task_registry`` really
    stubs the read -- the first cut bound the originals as default arguments and
    the "stubbed" unit test spawned schtasks and read G: (D-051 lens F #4).
    """
    registry_reader = registry_reader or read_task_registry
    markers_reader = markers_reader or run_marker.latest_by_task
    gmail_reader = gmail_reader or read_gmail_watermarks
    parity_reader = parity_reader or read_parity_report
    connectors_reader = connectors_reader or live_tool_families
    sweep_modes_reader = sweep_modes_reader or read_drive_sweep_modes
    now = time.time() if now is None else now
    inv: dict[str, Any] = {"detail": detail, "entity": (entity or "").upper() or None,
                           "generated_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat()}

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
            fresh = [m for m in marks if _is_fresh(m[1], now)]
            door["watermarks"] = len(marks)
            door["watermarks_fresh"] = len(fresh)
            door["watermarks_stale_keys"] = [k for k, v in marks if not _is_fresh(v, now)]
            newest = max((m[1] for m in marks if m[1]), default=None)
            door["newest_sync"] = _hours_ago(newest, now)
            if detail:
                door["watermark_keys"] = [f"{k} ({_hours_ago(v, now)})" for k, v in marks]
        doors.append(door)
    inv["doors"] = doors
    try:
        gm = gmail_reader() or {}
    except Exception:  # noqa: BLE001
        gm = {}
    if gm:
        inv["gmail_accounts"] = len(gm)
        epochs = [v for v in gm.values() if isinstance(v, (int, float)) and v > 0]
        inv["gmail_newest_sync"] = _hours_ago(max(epochs), now) if epochs else "unknown"
        inv["gmail_stale_accounts"] = sum(1 for v in gm.values() if not _is_fresh(v, now))
        if detail:
            # the sweep file stores epoch seconds per mailbox; render them as ages
            inv["gmail_watermarks"] = {
                str(k): (_hours_ago(v, now) if isinstance(v, (int, float)) else str(v)[:19])
                for k, v in sorted(gm.items())
            }

    # 1b. Live tool connectors -- NOT knowledge sources
    try:
        inv["live_connectors"] = list(connectors_reader(inv["entity"]) or [])
    except Exception:  # noqa: BLE001
        inv["live_connectors"] = []

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
            "ingest": is_ingest_task(name),
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
    # D-303 (Code #13 slice 8): per-account Drive sweep MODE beside the pinned
    # exclusions -- "do you ingest my Downloads" is answered from a listing.
    try:
        inv["drive_sweep_modes"] = list(sweep_modes_reader() or [])
    except Exception as exc:  # noqa: BLE001
        inv["drive_sweep_modes"] = []
        inv["drive_sweep_modes_error"] = str(exc)
    inv["excluded_path_segments"] = {
        "dashboard stores": sorted(kb_exclusions._DASHBOARD_STORE_SEGMENTS),
        "finance working stores": sorted(kb_exclusions._FINANCE_WORKSHEET_SEGMENTS),
        "LEX NDA project": sorted(kb_exclusions._LEX_NDA_SEGMENTS),
        "Cora build workspace (D-057)": ["/".join(kb_exclusions._CORA_WORKSPACE_SEGMENTS)],
        "static-tree skips": ["_brain/swept", "_delegated-work", "_archive", "dot-dirs", "PHI segments (consumers/clients/phi/clinical/ehr)"],
    }
    # the generic belt list is safe for every channel; the NDA'd project's own token is founder-level
    inv["title_belts"] = [
        "Cora build/audit docs: a `cora-` token + a build keyword (forensic/rebuild/audit/review/cascade/mirror/quarantine ...)",
        "Generated finance files: date-prefixed forecast/actuals/cashflow-worksheet names, forecast-assist",
        "NDA'd project meetings: a whole-word project token in a meeting title (token founder-level)",
        "Banking identifiers: routing/SWIFT/IBAN/account values are REDACTED at every chunk egress (deep link kept)",
    ]
    inv["title_belts_founder"] = [
        b.replace("(token founder-level)", "(token: `copa`)") for b in inv["title_belts"]
    ]
    inv["allowlisted_views"] = sorted(kb_exclusions._KB_ALLOWLIST_BASENAMES)
    inv["drive_skip_folder_names"] = sorted(_drive_skip_names())

    # 4. Mirror parity + canonical pointer (live-gated)
    try:
        inv["parity"] = parity_reader() or {"available": False}
    except Exception as exc:  # noqa: BLE001
        inv["parity"] = {"available": False, "error": str(exc)}
    static_live = any(d["source"] == "static_md" for d in doors)
    mirror_task = any("claude-mirror" in (t.get("name") or "").lower() for t in tasks)
    mirror_live = bool(inv["parity"].get("available")) or mirror_task
    inv["cowork_cascade_live"] = bool(static_live and mirror_live)
    if inv["cowork_cascade_live"]:
        inv["cowork_cascade_pointer"] = COWORK_CASCADE_POINTER
    else:
        dark = []
        if not static_live:
            dark.append("the static_md door (no static_md chunks read this turn)")
        if not mirror_live:
            dark.append("the claude-workspace mirror (parity report unreadable and no mirror task in the registry)")
        inv["cowork_cascade_pointer"] = COWORK_CASCADE_POINTER_DARK.format(which=" and ".join(dark) or "a door")
    return inv


def _drive_skip_names() -> frozenset[str]:
    try:
        from cora.connectors import drive_sweep
        return frozenset(drive_sweep._FOUNDERS_OS_SKIP_FOLDERS)
    except Exception:  # noqa: BLE001
        return frozenset()


ACCOUNTS_YAML_PATH = _REPO_ROOT / "data" / "maps" / "monitored-email-accounts.yaml"


def read_drive_sweep_modes(path: Path | None = None) -> list[dict[str, Any]]:
    """Per-account Drive sweep MODES from the roster (D-303, Code #13 slice 8):
    one row per account that carries a ``drive_sweep_mode`` key, with its
    allowlisted folders rendered by LABEL (drive_sweep.ALLOWLIST_FOLDER_LABELS)
    and any config error the sweep would refuse on. Read-only; fail-soft (an
    unreadable roster -> []). Accounts without the key are denylist and are not
    listed -- the renderer says so in one line."""
    import yaml  # lazy: not a module-level dependency of the inventory
    from cora.connectors import drive_sweep  # lazy: the same bot-loaded reader as _drive_skip_names
    p = Path(path) if path else ACCOUNTS_YAML_PATH
    try:
        cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("self_inventory: drive sweep modes unreadable (%s)", exc)
        return []
    out: list[dict[str, Any]] = []
    for row in cfg.get("accounts") or []:
        if not isinstance(row, dict) or drive_sweep.DRIVE_SWEEP_MODE_KEY not in row:
            continue
        mode, allow, err = drive_sweep.resolve_sweep_mode(row)
        out.append({
            "email": str(row.get("email") or ""),
            "mode": mode,
            "swept": bool(row.get("enabled", True)) and bool(row.get("dwd_eligible", False))
                     and bool(row.get("drive_sweep", False)),
            "allowlist": [
                {"id": fid, "label": drive_sweep.ALLOWLIST_FOLDER_LABELS.get(fid, "(unlabelled folder)")}
                for fid in sorted(allow)
            ],
            "error": err,
        })
    return out


# ── rendering ────────────────────────────────────────────────────────────────
def _channel_note(note: str) -> str:
    """A door note without repo / Drive file paths (founder-level metadata)."""
    return _PATH_TOKEN_RE.sub("a founder-level file", note)


def render_inventory(inv: dict[str, Any]) -> str:
    detail = bool(inv.get("detail"))
    lines: list[str] = ["Cora self-inventory (live signals; deterministic -- not retrieved from the KB):"]
    if inv.get("kb_error"):
        lines.append(f"- Knowledge base: {inv['kb_error']}")
    else:
        lines.append(f"- Knowledge base: {inv.get('total_chunks', 0):,} chunks.")
    lines.append("")
    lines.append(f"INGEST DOORS (source | chunks | newest sync the KB knows | watermarks fresh within {FRESH_HOURS}h | how it arrives):")
    for d in inv.get("doors", []):
        sync = d.get("newest_sync", "no watermark in KB")
        wm = ""
        if d.get("watermarks"):
            n_stale = len(d.get("watermarks_stale_keys") or [])
            wm = f" [{d.get('watermarks_fresh', 0)}/{d.get('watermarks', 0)} fresh" + (f"; {n_stale} STALE" if n_stale else "") + "]"
        note = d.get("note", "") if detail else _channel_note(d.get("note", ""))
        lines.append(f"- {d['source']} | {d['chunks']:,} | {sync}{wm} | {note}")
        if detail and d.get("watermark_keys"):
            lines.append("    keys: " + "; ".join(d["watermark_keys"][:40]))
        if detail and d.get("watermarks_stale_keys"):
            lines.append("    STALE (older than %dh): %s" % (FRESH_HOURS, "; ".join(d["watermarks_stale_keys"][:40])))
    if inv.get("gmail_accounts"):
        stale = inv.get("gmail_stale_accounts", 0)
        lines.append(f"- gmail sweep watermarks: {inv['gmail_accounts']} mailboxes tracked, newest sync "
                     f"{inv.get('gmail_newest_sync', 'unknown')}" + (f", {stale} STALE (>{FRESH_HOURS}h)" if stale else "")
                     + (" -- " + "; ".join(f"{k} {v}" for k, v in list(inv.get("gmail_watermarks", {}).items())[:40]) if detail else ""))
    lines.append("")
    conns = inv.get("live_connectors") or []
    if conns:
        lines.append("LIVE TOOL CONNECTORS in this channel (NOT knowledge-base sources -- 'can you access HubSpot / "
                     "Asana / QuickBooks / the calendar / Shopify' is answered YES from this list, never 'not in my "
                     "sources'): " + "; ".join(conns))
    else:
        lines.append("LIVE TOOL CONNECTORS: tool list unavailable this turn -- answer live-system questions from the "
                     "tools you were offered, never from this inventory.")
    lines.append("")
    by_entity = inv.get("by_entity", {}) or {}
    if detail:
        lines.append("ENTITY PARTITIONS (chunks): " + ", ".join(f"{k} {v:,}" for k, v in by_entity.items()))
    else:
        # a channel learns its OWN partition size and the total -- not how large
        # another entity's (e.g. the LEX) partition is
        ent = inv.get("entity") or ""
        parent = ent.split("-")[0] if ent else ""
        own = {k: v for k, v in by_entity.items() if k in (ent, parent)}
        own_txt = ", ".join(f"{k} {v:,}" for k, v in own.items()) or "n/a"
        lines.append(f"ENTITY PARTITIONS: {len(by_entity)} entity partitions; this channel's: {own_txt} "
                     f"(other partitions' sizes are founder-level)")
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
    # D-303: per-account Drive sweep MODE, beside the pinned exclusions. A
    # non-founder channel learns THAT allowlist mode exists and how many accounts
    # run it -- never which mailbox or which folder ids.
    modes = inv.get("drive_sweep_modes", []) or []
    n_allow = sum(1 for m in modes if m.get("mode") == "allowlist")
    if detail:
        if modes:
            lines.append("DRIVE SWEEP MODES (D-303; per account in monitored-email-accounts.yaml -- every account "
                         "not listed here is denylist; mailbox | mode | allowlisted folders):")
            for m in modes:
                folders = ", ".join(f"{a.get('label')} [{a.get('id')}]" for a in m.get("allowlist") or []) or "(none)"
                flag = f" | CONFIG ERROR (row not swept): {m.get('error')}" if m.get("error") else ""
                swept = "" if m.get("swept", True) else " | not currently swept (enabled/dwd/drive_sweep off)"
                lines.append(f"- {m.get('email')} | {m.get('mode')} | {folders}{flag}{swept}")
        else:
            err = inv.get("drive_sweep_modes_error")
            lines.append("DRIVE SWEEP MODES: no account pinned to allowlist mode (every flat sweep is denylist)"
                         + (f" -- roster unreadable this turn: {err}" if err else ""))
    else:
        lines.append(f"Drive sweep modes: {n_allow} account(s) run in ALLOWLIST-BY-FOLDER mode (only the allowlisted "
                     f"tree is ingested from that Drive; everything else is skipped and counted, never held); all "
                     f"other accounts are denylist. Mailboxes and folder ids are founder-level.")
    lines.append("Drive subtree names skipped in the Founder-OS walk: " + ", ".join(inv.get("drive_skip_folder_names", [])))
    belts = inv.get("title_belts_founder" if detail else "title_belts", inv.get("title_belts", []))
    lines.append("Title belts: " + " | ".join(belts))
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
    path_txt = p.get("path") if detail else "PARITY-REPORT.md (path founder-level)"
    if p.get("available"):
        cls = ", ".join(f"{k} {v.get('mirrored')} mirrored/{v.get('quarantined')} quarantined" for k, v in (p.get("classes") or {}).items())
        lines.append(f"CLAUDE-WORKSPACE MIRROR: PARITY-REPORT.md generated {p.get('generated', '?')} -- {cls}"
                     + (f"; quarantined total {p['quarantined_total']}" if "quarantined_total" in p else "")
                     + f" ({path_txt})")
    else:
        lines.append(f"CLAUDE-WORKSPACE MIRROR: PARITY-REPORT.md not readable this turn ({p.get('error', 'absent')}) -- cite it by path: "
                     f"{path_txt if detail else PARITY_REPORT_REL}")
    lines.append("")
    lines.append(inv.get("cowork_cascade_pointer", COWORK_CASCADE_POINTER))
    lines.append("")
    lines.append(REPLY_FORMAT)
    if not detail:
        lines.append("(Mailbox addresses, watermark keys, file paths, other partitions' sizes and the non-ingest task list "
                     "are founder-level -- ask in a founder channel.)")
    return "\n".join(lines)
