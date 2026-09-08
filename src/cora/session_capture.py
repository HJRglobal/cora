"""Universal Session Capture — harvest Claude Code session transcripts.

Every Claude Code / Cowork-via-Code session writes a JSONL transcript under
``~/.claude/projects/<slug>/<session-id>.jsonl``. Sessions die on close unless
something persists them. This module is the backstop harvester: nightly it
reads transcripts that closed without writing their own capture note, distills
each with Haiku into a structured session-note, entity-tags it, and writes it
to ``<FounderOS>/<entity-folder>/_session-captures/YYYY-MM/`` where the nightly
``static_md`` sync ingests it into Cora's KB.

Locked decisions (Universal Session Capture spec, 2026-06-09):
  1. Distilled summaries (decisions / facts learned / action items / open
     questions), not raw verbatim.
  2. PHI sessions captured IN FULL — nothing redacted from the distillation.
  3. Promotion to canonical CLAUDE.md / memory/ stays gated behind Harrison's
     existing 👍 knowledge-review DM. This module only lands captures in the
     capture log + KB (searchable) — it never writes canonical memory.
  4. PHI storage is LEX-scoped + entity-tagged so the KB's existing
     sibling_guard / cross_entity_guard / lex_phi_access gate keep it scoped.

Scope: Claude Code transcripts on this machine (~/.claude/projects) AND the
Claude Desktop Cowork store (agent-mode transcripts, opt-in via harvest's
include_cowork; the runner enables it). The Cowork store was located on disk
2026-07-23: standard Agent-SDK JSONL under %LOCALAPPDATA%/Packages/*laude*/
LocalCache/Roaming/Claude/local-agent-mode-sessions/<ws>/<agent>/local_<uuid>/
.claude/projects/<slug>/<inner>.jsonl. Cowork captures share this pipeline
(distill / entity-tag / PHI->LEX / ledger dedup with a "cowork:" key prefix).

Entity routing rule: Haiku classifies which business the session is ABOUT
(default = the cwd's entity). If real PHI patterns are present, the note is
forced into the LEX-scoped store regardless, so client PHI is always gated.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import drive_io, phi_guard

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_HAIKU_MODEL = "claude-haiku-4-5"

# Default transcript root on this machine.
# NOTE: Path("") is truthy (== Path(".")), so guard on the string, not the Path.
_projects_env = os.environ.get("CLAUDE_PROJECTS_ROOT", "").strip()
PROJECTS_ROOT = (
    Path(_projects_env) if _projects_env else (Path.home() / ".claude" / "projects")
)

_fos_env = os.environ.get("FOUNDER_OS_ROOT", "").strip()
FOUNDER_OS_ROOT = Path(_fos_env) if _fos_env else Path(r"G:\My Drive\HJR-Founder-OS")

# entity code -> Founder OS top-level folder name (inverse of the static_md map).
ENTITY_FOLDERS: dict[str, str] = {
    "HJRG": "01-HJR-Global",
    "F3E": "02-F3-Energy",
    "F3C": "03-F3-Community",
    "UFL": "04-UFL",
    "HJRPROD": "05-HJR-Productions",
    "HJRP": "06-HJR-Properties",
    "BDM": "07-Big-D-Media",
    "LEX": "08-Lexington-Services",
    "OSN": "09-One-Stop-Nutrition",
    "FNDR": "00-Founder",
}

# top-level Founder OS folder -> entity (for cwd-based default inference).
_FOLDER_TO_ENTITY: dict[str, str] = {
    "01-HJR-Global": "HJRG",
    "02-F3-Energy": "F3E",
    "03-F3-Community": "F3C",
    "04-UFL": "UFL",
    "05-HJR-Productions": "HJRPROD",
    "06-HJR-Properties": "HJRP",
    "07-Big-D-Media": "BDM",
    "08-Lexington-Services": "LEX",
    "09-One-Stop-Nutrition": "OSN",
    "00-Founder": "FNDR",
}

VALID_ENTITIES: frozenset[str] = frozenset(ENTITY_FOLDERS) | frozenset(
    {"LEX-LLC", "LEX-LTS", "LEX-LBHS", "LEX-LLA"}
)

SURFACE = "code-session"
SURFACE_COWORK = "cowork-session"

# Quarantine (ingest-integrity bundle I2, cq-bc5e5b7512bd, 2026-09-08): a
# non-LEX distill whose transcript trips the VALUE-shaped prose PHI screen is
# HELD here -- never filed into the LEX partition (Leak #2), never KB-ingested.
# The folder sits under the id-pinned, path-excluded Cora build workspace
# (_shared/projects/cora -> kb_exclusions.KB_EXCLUDED_FOLDER_IDS + the
# _CORA_WORKSPACE_SEGMENTS path rule), so BOTH ingest doors skip it by
# construction, and every quarantined note carries a ``cora-quarantine-`` name so
# the drive_sweep title belt trips even if the folder were ever moved (a second
# ingest door needs its own belt).
QUARANTINE_DIRNAME = "_session-capture-quarantine"
QUARANTINE_PREFIX = "cora-quarantine-"

# Don't harvest a session whose last activity is younger than this — it may
# still be live; let it settle so we capture the finished conversation.
SETTLE_MINUTES = 30

# Distillation input cap (chars). The raw transcript stays on disk; this only
# bounds what we hand Haiku. Higher for PHI/LEX so nothing material is dropped.
_MAX_INPUT_CHARS = 24_000
_MAX_INPUT_CHARS_PHI = 60_000

# Bound accumulated turn-text while MERGING a Cowork session's transcript(s).
# The desktop store is ~2 GB with multi-MB transcripts; distill truncates to
# _MAX_INPUT_CHARS_PHI regardless, so reading past this is wasted work.
_COWORK_MAX_TEXT_CHARS = _MAX_INPUT_CHARS_PHI + 4_000

_REPO_ROOT = Path(__file__).resolve().parents[2]
LEDGER_PATH = _REPO_ROOT / "logs" / "session-captures.jsonl"

_DISTILL_PROMPT = """You are distilling a software/operations work session transcript into a durable memory note for a multi-business "Founder OS". Be faithful and concrete — this note is the only thing that survives the session.

The session's working directory suggests it is about entity: {default_entity}.
Valid entity codes: HJRG, F3E, F3C, UFL, HJRPROD, HJRP, BDM, LEX, OSN, FNDR (FNDR = founder/cross-entity/infra). LEX = Lexington Services (care provider — may contain PHI).

Return ONLY a JSON object, no prose, with these keys:
{{
  "entity": "<the single entity code this session is MOST about; use {default_entity} if unclear>",
  "topic": "<one concise line: what this session accomplished>",
  "decisions": ["<concrete decision locked, if any>"],
  "facts": ["<durable fact learned that future sessions need>"],
  "action_items": ["<open to-do with owner if known>"],
  "open_questions": ["<unresolved question>"]
}}

Rules:
- Lists may be empty ([]). Do not invent items. Keep each item to one short sentence.
- If the session touches Lexington (LEX) client/employee/parent health info, DO NOT redact or omit it — capture the specifics faithfully (this note is stored in a PHI-scoped, access-controlled store).
- Prefer specifics (names, IDs, file paths, commit hashes, dollar amounts) over vague summaries.

TRANSCRIPT:
{transcript}
"""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ParsedSession:
    session_id: str
    path: Path
    cwd: str | None
    last_activity_epoch: float
    started_iso: str | None
    ended_iso: str | None
    text: str
    n_turns: int


@dataclass
class CaptureResult:
    session_id: str
    entity: str
    note_path: Path | None
    phi: bool
    distilled: bool
    skipped_reason: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    # I2: True when the note was HELD in the quarantine folder (non-LEX distill +
    # value-shaped PHI in the transcript) instead of being filed + KB-ingested.
    quarantined: bool = False


# ---------------------------------------------------------------------------
# Transcript discovery + parsing
# ---------------------------------------------------------------------------


def iter_transcript_files(projects_root: Path = PROJECTS_ROOT) -> Iterator[Path]:
    """Yield top-level session transcript .jsonl files.

    Skips per-subagent transcripts (``*/subagents/agent-*.jsonl``) — those are
    captured as part of their parent session.
    """
    if not projects_root.exists():
        return
    for path in projects_root.rglob("*.jsonl"):
        if "subagents" in path.parts:
            continue
        yield path


def _extract_text(content: Any) -> str:
    """Flatten a message ``content`` (str or list of blocks) into plain text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append(str(block.get("text", "")))
        elif btype == "thinking":
            continue  # internal reasoning — not durable signal
        elif btype == "tool_use":
            parts.append(f"[tool: {block.get('name', '?')}]")
        elif btype == "tool_result":
            inner = block.get("content")
            txt = _extract_text(inner) if inner is not None else ""
            if txt:
                parts.append(f"[result: {txt[:400]}]")
    return "\n".join(p for p in parts if p)


def _iso_to_epoch(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def parse_transcript(path: Path) -> ParsedSession | None:
    """Parse a transcript JSONL into a ParsedSession, or None if unusable."""
    session_id = path.stem
    cwd: str | None = None
    first_ts: str | None = None
    last_ts: str | None = None
    last_epoch = path.stat().st_mtime
    turns: list[str] = []
    n_turns = 0

    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("sessionId"):
                    session_id = d["sessionId"]
                if d.get("cwd") and not cwd:
                    cwd = d["cwd"]
                ts = d.get("timestamp")
                if ts:
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts
                msg = d.get("message")
                if isinstance(msg, dict) and msg.get("role") in ("user", "assistant"):
                    text = _extract_text(msg.get("content"))
                    # Skip pure tool-result/system noise and empty turns.
                    if text and not _is_noise_turn(text):
                        turns.append(f"{msg['role'].upper()}: {text}")
                        n_turns += 1
    except OSError as exc:
        log.warning("session_capture: cannot read %s: %s", path, exc)
        return None

    if n_turns == 0:
        return None

    last_epoch = _iso_to_epoch(last_ts) or last_epoch
    return ParsedSession(
        session_id=session_id,
        path=path,
        cwd=cwd,
        last_activity_epoch=last_epoch,
        started_iso=first_ts,
        ended_iso=last_ts,
        text="\n\n".join(turns),
        n_turns=n_turns,
    )


_NOISE_PREFIXES = (
    "Caveat: The messages below",
    "<command-name>",
    "<local-command-stdout>",
    "[Request interrupted",
)


def _is_noise_turn(text: str) -> bool:
    stripped = text.lstrip()
    return any(stripped.startswith(p) for p in _NOISE_PREFIXES)


# ---------------------------------------------------------------------------
# Cowork desktop store (Claude Desktop agent-mode transcripts)
# ---------------------------------------------------------------------------
# The desktop app writes standard Agent-SDK JSONL transcripts under
#   %LOCALAPPDATA%/Packages/*laude*/LocalCache/Roaming/Claude/
#     local-agent-mode-sessions/<ws>/<agent>/local_<uuid>/.claude/projects/<slug>/<inner>.jsonl
# (validated on disk 2026-07-23). The sibling `claude-code-sessions` tree holds
# only per-session UI-state JSON (title/metadata); its local_<uuid> ids are
# DISJOINT from the agent-mode ids, so the agent-mode transcript is the harvest
# unit -- it is where the actual conversation lives. Each agent-mode session dir
# is one capture; its top-level transcript(s) are merged (resumes), deduped by
# message uuid, `type:"result"` summary lines skipped, and text-bounded.


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _discover_cowork_roots() -> list[Path]:
    """Discover the Cowork agent-mode transcript root(s) WITHOUT hardcoding the
    package hash. Honors COWORK_SESSIONS_ROOT (a single explicit root, for tests
    / relocation). Never raises."""
    override = os.environ.get("COWORK_SESSIONS_ROOT", "").strip()
    if override:
        return [Path(override)]
    local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
    if not local_appdata:
        return []
    roots: list[Path] = []
    try:
        for pkg in (Path(local_appdata) / "Packages").glob("*laude*"):
            root = (pkg / "LocalCache" / "Roaming" / "Claude"
                    / "local-agent-mode-sessions")
            if root.exists():
                roots.append(root)
    except OSError:
        pass
    return roots


def iter_cowork_session_dirs(roots: list[Path]) -> Iterator[Path]:
    """Yield agent-mode session dirs (``local_<uuid>`` holding a ``.claude/projects``)
    across every workspace/agent guid under each root. Fail-soft per level."""
    for root in roots:
        try:
            ws_dirs = [w for w in root.iterdir() if w.is_dir()]
        except OSError:
            continue
        for ws in ws_dirs:
            try:
                agent_dirs = [a for a in ws.iterdir() if a.is_dir()]
            except OSError:
                continue
            for agent in agent_dirs:
                try:
                    sess_dirs = [s for s in agent.iterdir() if s.is_dir()]
                except OSError:
                    continue
                for sess in sess_dirs:
                    if not sess.name.startswith("local_"):
                        continue
                    if (sess / ".claude" / "projects").exists():
                        yield sess


def _cowork_transcripts_for(session_dir: Path) -> list[Path]:
    """Top-level agent-mode transcripts for a session dir, oldest-first.

    Uses ``projects/*/*.jsonl`` (the observed ``<slug>/<inner>.jsonl`` depth) so
    the subagent subtree is never descended -- cheap and bounded on the 2 GB
    store. Belt-and-suspenders skip of any subagents / mcp-logs path segment."""
    proj = session_dir / ".claude" / "projects"
    out: list[Path] = []
    try:
        for tf in proj.glob("*/*.jsonl"):
            parts = tf.parts
            if "subagents" in parts or any("mcp-logs" in p for p in parts):
                continue
            out.append(tf)
    except OSError:
        return []
    out.sort(key=_safe_mtime)
    return out


def parse_cowork_session(session_dir: Path,
                         transcripts: list[Path] | None = None) -> ParsedSession | None:
    """Parse a Cowork agent-mode session dir into a ParsedSession.

    Merges the dir's top-level transcript(s) in mtime order, DEDUPES by message
    ``uuid`` (resumed transcripts repeat ids), SKIPS ``type:"result"`` summary
    entries (duplicates of per-message data), bounds accumulated turn-text for
    the ~2 GB store, and falls back to file mtime for last-activity when
    per-message timestamps are absent. ``session_id`` = the dir name
    (``local_<uuid>``), the stable Cowork session id."""
    if transcripts is None:
        transcripts = _cowork_transcripts_for(session_dir)
    if not transcripts:
        return None

    cwd: str | None = None
    first_ts: str | None = None
    last_ts: str | None = None
    last_epoch = 0.0
    turns: list[str] = []
    n_turns = 0
    seen_uuids: set[str] = set()
    total_chars = 0
    done = False

    for tf in transcripts:
        last_epoch = max(last_epoch, _safe_mtime(tf))
        if done:
            continue
        try:
            with open(tf, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if d.get("type") == "result":
                        continue  # session-summary duplicate
                    if d.get("cwd") and not cwd:
                        cwd = d["cwd"]
                    ts = d.get("timestamp")
                    if ts:
                        if first_ts is None:
                            first_ts = ts
                        last_ts = ts
                    uid = d.get("uuid")
                    if uid is not None:
                        if uid in seen_uuids:
                            continue  # resumed-session duplicate message
                        seen_uuids.add(uid)
                    if d.get("type") not in ("user", "assistant"):
                        continue
                    msg = d.get("message")
                    if isinstance(msg, dict) and msg.get("role") in ("user", "assistant"):
                        text = _extract_text(msg.get("content"))
                        if text and not _is_noise_turn(text):
                            turns.append(f"{msg['role'].upper()}: {text}")
                            n_turns += 1
                            total_chars += len(text)
                            if total_chars >= _COWORK_MAX_TEXT_CHARS:
                                done = True
                                break
        except OSError as exc:
            log.warning("session_capture: cannot read cowork transcript %s: %s", tf, exc)
            continue

    if n_turns == 0:
        return None
    last_epoch = _iso_to_epoch(last_ts) or last_epoch
    return ParsedSession(
        session_id=session_dir.name,
        path=session_dir,
        cwd=cwd,
        last_activity_epoch=last_epoch,
        started_iso=first_ts,
        ended_iso=last_ts,
        text="\n\n".join(turns),
        n_turns=n_turns,
    )


# A Cowork agent-mode session launched by a Windows scheduled task opens with a
# harness-injected first user turn: `<scheduled-task name="..." file="...">`.
# ~Half of the in-window Cowork sessions are such automation runs (validated on
# the live store 2026-07-23) -- including the two tasks this capture RETIRES --
# and their distilled notes would be pure automation noise. Skip them (Cowork
# only; the Code path is unchanged). Keyed on the START of the first turn so a
# normal chat that merely mentions the tag is never mistaken for one.
_SCHEDULED_TASK_MARKER = "<scheduled-task"


def _is_scheduled_task_session(session: ParsedSession) -> bool:
    text = session.text.lstrip()
    if text.startswith("USER: "):
        text = text[len("USER: "):].lstrip()
    return text.startswith(_SCHEDULED_TASK_MARKER)


# ---------------------------------------------------------------------------
# Entity inference
# ---------------------------------------------------------------------------


def entity_from_cwd(cwd: str | None) -> str:
    """Best-effort entity from the session's working directory. Default FNDR.

    A Founder OS entity-folder cwd maps to that entity; anything else (incl. the
    Cora repo) defaults to FNDR (founder / cross-entity / infra).
    """
    if not cwd:
        return "FNDR"
    try:
        p = Path(cwd)
        if p.is_relative_to(FOUNDER_OS_ROOT):
            rel = p.relative_to(FOUNDER_OS_ROOT)
            if rel.parts:
                return _FOLDER_TO_ENTITY.get(rel.parts[0], "FNDR")
    except (ValueError, OSError):
        pass
    return "FNDR"


def entity_folder(entity: str) -> str:
    """Map an entity code (incl. LEX sub-entities) to its Founder OS folder."""
    if entity.startswith("LEX-"):
        return ENTITY_FOLDERS["LEX"]
    return ENTITY_FOLDERS.get(entity, ENTITY_FOLDERS["FNDR"])


# ---------------------------------------------------------------------------
# Distillation
# ---------------------------------------------------------------------------


def _build_distill_prompt(text: str, default_entity: str, *, phi: bool) -> str:
    """The exact distill prompt for one transcript -- shared verbatim by the
    sync path (distill) and the batch path (_batch_distill), so batching
    changes transport, never content."""
    cap = _MAX_INPUT_CHARS_PHI if phi else _MAX_INPUT_CHARS
    return _DISTILL_PROMPT.format(default_entity=default_entity,
                                  transcript=text[:cap])


def distill(text: str, default_entity: str, *, phi: bool,
            client: Any = None) -> dict[str, Any] | None:
    """Distill a transcript with Haiku. Fail-closed: returns None on any error.

    `client` may be injected (tests); otherwise an Anthropic client is built
    from ANTHROPIC_API_KEY.
    """
    if client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            log.warning("session_capture: ANTHROPIC_API_KEY not set — skipping distill")
            return None
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
        except Exception as exc:  # noqa: BLE001 — fail-closed
            log.warning("session_capture: anthropic client init failed: %s", exc)
            return None

    prompt = _build_distill_prompt(text, default_entity, phi=phi)
    try:
        resp = client.messages.create(
            model=_HAIKU_MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        from .llm_usage import log_usage
        log_usage(resp, caller="session_capture", model=_HAIKU_MODEL)
        raw = resp.content[0].text.strip()
    except Exception as exc:  # noqa: BLE001 — fail-closed
        log.warning("session_capture: Haiku distill failed: %s", exc)
        return None

    return _parse_distilled(raw, default_entity)


#: Haiku answers "LEXINGTON" / "F3 Energy" / "Founder" often enough that a strict
#: VALID_ENTITIES check silently fell back to the cwd default (D-051 lens A).
ENTITY_ALIASES: dict[str, str] = {
    "F3": "F3E", "F3 ENERGY": "F3E", "F3-ENERGY": "F3E", "F3ENERGY": "F3E",
    "LEXINGTON": "LEX", "LEXINGTON SERVICES": "LEX", "LEX SERVICES": "LEX",
    "FOUNDER": "FNDR", "FOUNDER OS": "FNDR", "FOUNDER-OS": "FNDR", "FNDR-OS": "FNDR", "PERSONAL": "FNDR",
    "HJR": "HJRG", "HJR GLOBAL": "HJRG", "HJR-GLOBAL": "HJRG",
    "OSN NUTRITION": "OSN", "ONE STOP NUTRITION": "OSN",
    "HJR PRODUCTIONS": "HJRPROD", "HJR PROPERTIES": "HJRP",
    "F3 COMMUNITY": "F3C", "UNITED FIGHT LEAGUE": "UFL",
}


def normalize_entity(raw: str) -> str:
    """Upper-cased, alias-folded entity code (may still be invalid -- caller checks)."""
    ent = str(raw or "").strip().upper().strip(".,;:")
    return ENTITY_ALIASES.get(ent, ent)


def _parse_distilled(raw: str, default_entity: str) -> dict[str, Any] | None:
    """Parse Haiku's JSON output, normalize + validate the entity. None on failure."""
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None

    entity = normalize_entity(obj.get("entity", ""))
    if entity not in VALID_ENTITIES:
        entity = default_entity

    def _as_list(v: Any) -> list[str]:
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        if isinstance(v, str) and v.strip():
            return [v.strip()]
        return []

    return {
        "entity": entity,
        "topic": str(obj.get("topic", "") or "").strip() or "(no topic)",
        "decisions": _as_list(obj.get("decisions")),
        "facts": _as_list(obj.get("facts")),
        "action_items": _as_list(obj.get("action_items")),
        "open_questions": _as_list(obj.get("open_questions")),
    }


# ---------------------------------------------------------------------------
# Note rendering + path
# ---------------------------------------------------------------------------


def render_note(distilled: dict[str, Any], session: ParsedSession,
                date_str: str, phi: bool, *, surface: str = SURFACE,
                quarantined: bool = False) -> str:
    """Render the distilled session into the locked note schema."""
    def _bullets(items: list[str]) -> str:
        if not items:
            return "  - (none)"
        return "\n".join(f"  - {it}" for it in items)

    entity = distilled["entity"]
    header = f"## {date_str} — {surface} — {entity} — {distilled['topic']}"
    if quarantined:
        # I2: a non-LEX note held for founder review -- the stamp says WHY it is
        # here and that it is not knowledge (not KB-ingested, not canon).
        phi_line = (f"- QUARANTINED: PHI-risk on a non-LEX ({entity}) session -- founder review; "
                    f"not KB-ingested (ingest-integrity 2026-09, cq-bc5e5b7512bd)\n")
    else:
        phi_line = "- PHI: yes (LEX-scoped, access-controlled)\n" if phi else ""
    return (
        f"{header}\n\n"
        f"- Decisions:\n{_bullets(distilled['decisions'])}\n"
        f"- Facts learned:\n{_bullets(distilled['facts'])}\n"
        f"- Action items:\n{_bullets(distilled['action_items'])}\n"
        f"- Open questions:\n{_bullets(distilled['open_questions'])}\n"
        f"- Source session id: {session.session_id}\n"
        f"- Source cwd: {session.cwd or '(unknown)'}\n"
        f"{phi_line}"
        f"- Captured: {datetime.now(timezone.utc).isoformat()}\n"
    )


def note_path_for(entity: str, when: datetime, session_id: str,
                  root: Path = FOUNDER_OS_ROOT, *, surface: str = SURFACE) -> Path:
    """Compute the .md path: <root>/<folder>/_session-captures/YYYY-MM/<file>."""
    folder = entity_folder(entity)
    month = when.strftime("%Y-%m")
    date = when.strftime("%Y-%m-%d")
    # session_id may be a ledger-prefixed / "local_"-prefixed id; strip both so
    # the filename short is the clean transcript-id head, unique per session.
    clean = session_id.split(":", 1)[-1]
    clean = clean[len("local_"):] if clean.startswith("local_") else clean
    short = clean[:8] if clean else uuid.uuid4().hex[:8]
    fname = f"{date}_{surface}_{short}.md"
    return root / folder / "_session-captures" / month / fname


def quarantine_root(root: Path = FOUNDER_OS_ROOT) -> Path:
    """The founder-only, KB-excluded quarantine folder (see QUARANTINE_DIRNAME)."""
    return root / "_shared" / "projects" / "cora" / QUARANTINE_DIRNAME


def quarantine_note_path_for(entity: str, when: datetime, session_id: str,
                             root: Path = FOUNDER_OS_ROOT, *, surface: str = SURFACE) -> Path:
    """Quarantine path: <root>/_shared/projects/cora/_session-capture-quarantine/
    YYYY-MM/cora-quarantine-<the regular filename>. Same date/surface/short-id
    filename so the re-file tooling can recognise the session; the prefix is the
    drive_sweep title belt."""
    regular = note_path_for(entity, when, session_id, root=root, surface=surface)
    return quarantine_root(root) / when.strftime("%Y-%m") / f"{QUARANTINE_PREFIX}{regular.name}"


def route_capture(distilled_entity: str, text: str) -> tuple[str, bool, bool]:
    """Decide (entity, phi, quarantined) for a distilled session -- the Leak #2 fix.

    * A LEX / LEX-* distill keeps the STRICT ingestion posture: ``phi`` is
      ``phi_guard.is_phi_risk`` (over-flagging costs nothing there -- the note is
      LEX-filed either way and the flag only adds the access-control stamp).
    * A NON-LEX distill is NEVER re-homed to LEX. ``phi`` is the value-shaped
      prose screen (``phi_guard.is_prose_phi_risk``: a DOB value, an ICD-10 code,
      "diagnosed with", a programme id number, a diagnosis/medication tied to a
      named individual). When it fires the note is QUARANTINED -- held in the
      founder-only, KB-excluded folder and alerted -- instead of filed.

    Until 2026-09-08 the strict screen decided the filing and re-homed every hit
    to LEX; measured on the real transcripts it tripped on ``diagnosis``
    (root-cause), ``arc``, ``assessment``, ``discharge``, ``patient`` ... and 142
    non-LEX sessions since June landed in 08-Lexington-Services under a false PHI
    stamp (decisions.md 2026-09-03 harvester entry + its 9/4 correction).
    """
    entity = distilled_entity
    if entity.startswith("LEX"):
        return entity, phi_guard.is_phi_risk(text), False
    phi = phi_guard.is_prose_phi_risk(text)
    return entity, phi, phi


# ---------------------------------------------------------------------------
# Ledger (dedup by session id)
# ---------------------------------------------------------------------------


def load_captured_ids(ledger_path: Path = LEDGER_PATH) -> set[str]:
    """Return the set of session ids already harvested."""
    ids: set[str] = set()
    if not ledger_path.exists():
        return ids
    try:
        with open(ledger_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sid = rec.get("session_id")
                if sid:
                    ids.add(sid)
    except OSError:
        pass
    return ids


def append_ledger(rec: dict[str, Any], ledger_path: Path = LEDGER_PATH) -> None:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _now_epoch() -> float:
    return datetime.now(timezone.utc).timestamp()


# Sentinel distinguishing "no batch pre-distill ran" (sync-distill inline)
# from "the batch ran and this item failed BOTH transports" (None -> the same
# fail-closed skip a failed sync distill produces).
_NO_PREDISTILL = object()


def _finalize_capture(
    session: ParsedSession, *, surface: str, ledger_key: str,
    captured: set[str], dry_run: bool, with_kb: bool,
    founder_os_root: Path, ledger_path: Path,
    anthropic_client: Any, kb: Any,
    pre_distilled: Any = _NO_PREDISTILL,
) -> CaptureResult:
    """Distill -> entity-tag -> PHI-route -> write -> ledger for one parsed
    session. Shared by the Code and Cowork harvest loops so PHI routing, the
    fail-closed distill skip, the fail-soft G: write, and dedup are IDENTICAL
    on both paths. ``ledger_key`` (Code: raw id; Cowork: ``cowork:<id>``) is the
    dedup + ledger key; ``session.session_id`` remains the clean id shown in the
    note + filename. ``pre_distilled`` carries a batch-path result (parsed dict,
    or None when batch AND its per-item sync fallback both failed)."""
    # The PRE-distill posture is deliberately the strict ingestion screen and is
    # unchanged: it sizes the Haiku input cap (more room for a PHI-shaped
    # transcript) and keeps PHI-shaped transcripts off the Message Batch (no
    # 29-day at-rest copy). It no longer decides WHERE the note is filed --
    # route_capture does, after the distill has named the entity (I2, Leak #2).
    phi_strict = phi_guard.is_phi_risk(session.text)
    default_entity = entity_from_cwd(session.cwd)

    if pre_distilled is _NO_PREDISTILL:
        distilled = distill(session.text, default_entity, phi=phi_strict,
                            client=anthropic_client)
    else:
        distilled = pre_distilled
    if distilled is None:
        # Fail-closed: do not write, do not mark captured — retry next run.
        return CaptureResult(
            session_id=session.session_id, entity=default_entity,
            note_path=None, phi=phi_strict, distilled=False,
            skipped_reason="distill_failed",
        )

    # NEVER re-home a non-LEX distill to LEX. A LEX distill keeps the strict
    # stamp; a non-LEX distill that trips the value-shaped prose screen is
    # QUARANTINED (held + alerted), not filed.
    entity, phi, quarantined = route_capture(distilled["entity"], session.text)
    distilled["entity"] = entity

    when = datetime.now(timezone.utc)
    if quarantined:
        npath = quarantine_note_path_for(entity, when, session.session_id,
                                         root=founder_os_root, surface=surface)
    else:
        npath = note_path_for(entity, when, session.session_id,
                              root=founder_os_root, surface=surface)
    note = render_note(distilled, session, when.strftime("%Y-%m-%d"), phi,
                       surface=surface, quarantined=quarantined)

    result = CaptureResult(
        session_id=session.session_id, entity=entity, note_path=npath,
        phi=phi, distilled=True, quarantined=quarantined,
        meta={"topic": distilled["topic"], "n_turns": session.n_turns,
              "surface": surface},
    )
    if quarantined:
        log.warning(
            "session_capture: QUARANTINED %s (entity=%s surface=%s) -> %s -- PHI-risk on a "
            "non-LEX session; founder review, not KB-ingested",
            ledger_key, entity, surface, npath.name,
        )

    if dry_run:
        log.info("[DRY] would write %s (entity=%s phi=%s quarantined=%s surface=%s)",
                 npath, entity, phi, quarantined, surface)
        captured.add(ledger_key)
        return result

    try:
        # G: write, atomic + timeout-bounded (make_parents creates the YYYY-MM dir).
        # A transient unmount raises drive_io.DriveUnavailable (an OSError, caught
        # here -> this session is skipped + retries next run) instead of hanging the
        # nightly capture process.
        drive_io.write_text_atomic(npath, note, encoding="utf-8")
    except OSError as exc:
        log.error("session_capture: failed writing %s: %s", npath, exc)
        result.skipped_reason = "write_failed"
        result.note_path = None
        return result

    # A quarantined note is NEVER KB-ingested (its folder is excluded on both doors
    # too -- this is the belt for the immediate-ingest path).
    if with_kb and kb is not None and not quarantined:
        _ingest_note(kb, npath, entity, distilled, session, founder_os_root,
                     content=note, when=when)

    append_ledger({
        "session_id": ledger_key,
        "entity": entity,
        "phi": phi,
        "quarantined": quarantined,
        "note_path": str(npath),
        "topic": distilled["topic"],
        "surface": surface,
        "captured_at": when.isoformat(),
    }, ledger_path)
    captured.add(ledger_key)
    log.info("Captured session %s -> %s (entity=%s phi=%s quarantined=%s surface=%s)",
             ledger_key, npath.name, entity, phi, quarantined, surface)
    return result


def harvest(
    *,
    lookback_hours: int = 24,
    max_sessions: int = 50,
    dry_run: bool = False,
    with_kb: bool = False,
    projects_root: Path = PROJECTS_ROOT,
    founder_os_root: Path = FOUNDER_OS_ROOT,
    ledger_path: Path = LEDGER_PATH,
    anthropic_client: Any = None,
    kb: Any = None,
    include_cowork: bool = False,
    cowork_roots: list[Path] | None = None,
    max_cowork_sessions: int | None = None,
    use_batch: bool = False,
) -> list[CaptureResult]:
    """Harvest un-captured sessions in the lookback window. Returns results.

    Two sources, each with its own budget so neither starves the other:
      * Code sessions under ``projects_root`` (~/.claude/projects).
      * Cowork desktop agent-mode sessions (opt-in via ``include_cowork``; the
        runner enables it, the module default is OFF so unit tests / other
        callers see the exact prior Code-only behavior). Cowork ledger keys are
        ``cowork:`` prefixed to namespace them off Code-session ids.

    ``use_batch`` (pilot slice 3; module default OFF, the runner opts in) runs
    ALL pending distills as ONE Message Batch (50% off) before finalizing --
    collection and finalization order, budgets, dedup, PHI routing, and the
    fail-closed skip are byte-identical to the sync path. Ignored when a
    ``anthropic_client`` is injected (tests / bespoke callers keep sync).
    """
    now = _now_epoch()
    cutoff = now - lookback_hours * 3600
    settle = now - SETTLE_MINUTES * 60
    captured = load_captured_ids(ledger_path)
    # Collection phase: ("pending", (session, surface, ledger_key)) entries in
    # the EXACT order the sync path would have finalized them, with
    # ("skipped", CaptureResult) rows interleaved where pre-distill skips land.
    entries: list[tuple[str, Any]] = []

    # --- Code sessions (~/.claude/projects) ---
    processed = 0
    for path in iter_transcript_files(projects_root):
        if processed >= max_sessions:
            break
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff or mtime > settle:
            continue  # outside window, or too fresh (may be live)

        session = parse_transcript(path)
        if session is None:
            continue
        if session.session_id in captured:
            continue

        processed += 1
        entries.append(("pending", (session, SURFACE, session.session_id)))

    # --- Cowork desktop agent-mode sessions ---
    if include_cowork:
        roots = cowork_roots if cowork_roots is not None else _discover_cowork_roots()
        cw_budget = max_cowork_sessions if max_cowork_sessions is not None else max_sessions
        cw_processed = 0
        for sess_dir in iter_cowork_session_dirs(roots):
            if cw_processed >= cw_budget:
                break
            transcripts = _cowork_transcripts_for(sess_dir)
            if not transcripts:
                continue
            mtime = max((_safe_mtime(p) for p in transcripts), default=0.0)
            if mtime < cutoff or mtime > settle:
                continue  # outside window, or too fresh (may be live)
            ledger_key = f"cowork:{sess_dir.name}"
            if ledger_key in captured:
                continue
            session = parse_cowork_session(sess_dir, transcripts=transcripts)
            if session is None:
                continue
            # Skip scheduled-task automation runs (pure noise) BEFORE the Haiku
            # call. Not budget-consuming and not ledger-marked -- they age out of
            # the window on their own; surfaced as a skipped result for the run log.
            if _is_scheduled_task_session(session):
                log.info("session_capture: skipping cowork scheduled-task session %s",
                         sess_dir.name)
                entries.append(("skipped", CaptureResult(
                    session_id=sess_dir.name, entity="FNDR", note_path=None,
                    phi=False, distilled=False, skipped_reason="scheduled_task")))
                continue
            cw_processed += 1
            entries.append(("pending", (session, SURFACE_COWORK, ledger_key)))

    # Batch pre-distill (one submission for every pending session). Empty dict
    # => every item finalizes through the unchanged sync distill path.
    pending = [payload for kind, payload in entries if kind == "pending"]
    pre: dict[str, Any] = {}
    if use_batch and pending and anthropic_client is None:
        pre = _batch_distill(pending)

    results: list[CaptureResult] = []
    for kind, payload in entries:
        if kind == "skipped":
            results.append(payload)
            continue
        session, surface, ledger_key = payload
        results.append(_finalize_capture(
            session, surface=surface, ledger_key=ledger_key,
            captured=captured, dry_run=dry_run, with_kb=with_kb,
            founder_os_root=founder_os_root, ledger_path=ledger_path,
            anthropic_client=anthropic_client, kb=kb,
            pre_distilled=pre.get(ledger_key, _NO_PREDISTILL)))

    return results


def _batch_distill(pending: list[tuple[ParsedSession, str, str]]) -> dict[str, Any]:
    """One Message Batch over every pending session's distill prompt.

    Returns ``{ledger_key: parsed-dict-or-None}``; an EMPTY dict means "no
    batch ran" (leg disabled, or an unexpected helper error) and every item
    falls back to the inline sync distill. custom_ids are positional
    ("item-N") -- opaque by construction, so no session id / cwd / PHI
    material ever rides in a batch identifier. Item-level batch failures were
    already retried SYNC inside batch_generate; a None value here is the same
    terminal state as a failed sync distill (fail-closed: the session is not
    ledger-marked and retries next run).

    Scheduling (verified dependency graph): the capture task fires 05:15 AZ
    with a 1h ExecutionTimeLimit, and its --with-kb ingest is what makes
    notes visible to the 07:00 knowledge review. The default 900s deadline +
    bounded sync fallback keeps the whole run well inside both.
    """
    from . import batch_client
    if not batch_client.batch_enabled("CORA_BATCH_CAPTURE"):
        return {}
    try:
        deadline = float(os.environ.get("CORA_BATCH_CAPTURE_DEADLINE_S", "900"))
        keys: list[tuple[str, str]] = []  # (ledger_key, default_entity)
        requests: list[dict] = []
        skipped_phi = 0
        for session, _surface, ledger_key in pending:
            # PHI posture (D-051 2026-08-01 finding 2): a PHI-flagged
            # transcript stays on the SYNC distill (omit from the batch -> the
            # finalize loop distills it inline). Batch results are retrievable
            # Anthropic-side for 29 days by API key; PHI-bearing distills must
            # not gain that at-rest copy.
            # Both screens: the strict ingestion screen AND the value-shaped prose
            # screen -- a transcript either one flags must not gain the 29-day
            # at-rest batch copy (D-051 lens A).
            if phi_guard.is_phi_risk(session.text) or phi_guard.is_prose_phi_risk(session.text):
                skipped_phi += 1
                continue
            default_entity = entity_from_cwd(session.cwd)
            prompt = _build_distill_prompt(session.text, default_entity, phi=False)
            requests.append({
                "custom_id": f"item-{len(keys)}",
                "params": {"model": _HAIKU_MODEL, "max_tokens": 1500,
                           "messages": [{"role": "user", "content": prompt}]},
            })
            keys.append((ledger_key, default_entity))
        if skipped_phi:
            log.info("session_capture: %d PHI-flagged session(s) kept on the "
                     "sync distill path (excluded from the batch)", skipped_phi)
        if not requests:
            return {}
        results = batch_client.batch_generate(
            requests, caller="session_capture", deadline_s=deadline)
    except Exception as exc:  # noqa: BLE001 -- belt: never worse than sync
        log.warning("session_capture: batch distill unavailable (%s) -- "
                    "falling back to per-session sync distills", exc)
        return {}
    out: dict[str, Any] = {}
    for i, (ledger_key, default_entity) in enumerate(keys):
        msg = results.get(f"item-{i}")
        if msg is None:
            out[ledger_key] = None
            continue
        try:
            raw = msg.content[0].text.strip()
        except Exception:  # noqa: BLE001 -- malformed message == failed distill
            out[ledger_key] = None
            continue
        out[ledger_key] = _parse_distilled(raw, default_entity)
    return out


def _ingest_note(kb: Any, npath: Path, entity: str, distilled: dict[str, Any],
                 session: ParsedSession, root: Path, *, content: str, when: datetime) -> None:
    """Upsert a freshly-written note into the KB immediately (idempotent).

    Uses source="static_md" + source_id=<relative path> so it shares identity
    with the nightly static_md sync (replace-on-conflict => no duplicate).
    sub_entity tagging for LEX is applied by the store's upsert Step 0.

    Takes the note CONTENT + capture time directly (the note was JUST written a few
    lines up), so the immediate ingest never RE-READS the file off the G: mount --
    a naked read-back there could hang in the ~30s unmount/remount window this branch
    was built to survive (D-051, 2026-07-16). The nightly static_md sync reconciles
    date_created/date_modified from the real file stat on its next pass (idempotent).
    """
    try:
        from .knowledge_base.store import Document
        rel = str(npath.relative_to(root)) if npath.is_relative_to(root) else str(npath)
        ts = int(when.timestamp())
        kb.upsert_documents([Document(
            source="static_md",
            source_id=rel,
            entity="LEX" if entity.startswith("LEX-") else entity,
            sub_entity=entity if entity.startswith("LEX-") else None,
            content=content,
            date_created=ts,
            date_modified=ts,
            title=f"Session capture — {distilled['topic']}",
            deep_link=f"computer://{npath}",
            metadata={"path": rel, "session_id": session.session_id, "kind": "session_capture"},
        )])
    except Exception as exc:  # noqa: BLE001 — KB ingest is best-effort
        log.warning("session_capture: immediate KB ingest failed for %s: %s", npath, exc)
