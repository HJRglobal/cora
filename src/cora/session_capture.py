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


def distill(text: str, default_entity: str, *, phi: bool,
            client: Any = None) -> dict[str, Any] | None:
    """Distill a transcript with Haiku. Fail-closed: returns None on any error.

    `client` may be injected (tests); otherwise an Anthropic client is built
    from ANTHROPIC_API_KEY.
    """
    cap = _MAX_INPUT_CHARS_PHI if phi else _MAX_INPUT_CHARS
    transcript = text[:cap]

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

    prompt = _DISTILL_PROMPT.format(default_entity=default_entity, transcript=transcript)
    try:
        resp = client.messages.create(
            model=_HAIKU_MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
    except Exception as exc:  # noqa: BLE001 — fail-closed
        log.warning("session_capture: Haiku distill failed: %s", exc)
        return None

    return _parse_distilled(raw, default_entity)


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

    entity = str(obj.get("entity", "") or "").strip().upper()
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
                date_str: str, phi: bool, *, surface: str = SURFACE) -> str:
    """Render the distilled session into the locked note schema."""
    def _bullets(items: list[str]) -> str:
        if not items:
            return "  - (none)"
        return "\n".join(f"  - {it}" for it in items)

    entity = distilled["entity"]
    header = f"## {date_str} — {surface} — {entity} — {distilled['topic']}"
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


def _finalize_capture(
    session: ParsedSession, *, surface: str, ledger_key: str,
    captured: set[str], dry_run: bool, with_kb: bool,
    founder_os_root: Path, ledger_path: Path,
    anthropic_client: Any, kb: Any,
) -> CaptureResult:
    """Distill -> entity-tag -> PHI-route -> write -> ledger for one parsed
    session. Shared by the Code and Cowork harvest loops so PHI routing, the
    fail-closed distill skip, the fail-soft G: write, and dedup are IDENTICAL
    on both paths. ``ledger_key`` (Code: raw id; Cowork: ``cowork:<id>``) is the
    dedup + ledger key; ``session.session_id`` remains the clean id shown in the
    note + filename."""
    phi = phi_guard.is_phi_risk(session.text)
    default_entity = entity_from_cwd(session.cwd)

    distilled = distill(session.text, default_entity, phi=phi, client=anthropic_client)
    if distilled is None:
        # Fail-closed: do not write, do not mark captured — retry next run.
        return CaptureResult(
            session_id=session.session_id, entity=default_entity,
            note_path=None, phi=phi, distilled=False,
            skipped_reason="distill_failed",
        )

    entity = distilled["entity"]
    # PHI present -> force into the LEX-scoped, access-controlled store.
    if phi:
        entity = "LEX" if not entity.startswith("LEX") else entity
        distilled["entity"] = entity

    when = datetime.now(timezone.utc)
    npath = note_path_for(entity, when, session.session_id,
                          root=founder_os_root, surface=surface)
    note = render_note(distilled, session, when.strftime("%Y-%m-%d"), phi, surface=surface)

    result = CaptureResult(
        session_id=session.session_id, entity=entity, note_path=npath,
        phi=phi, distilled=True,
        meta={"topic": distilled["topic"], "n_turns": session.n_turns,
              "surface": surface},
    )

    if dry_run:
        log.info("[DRY] would write %s (entity=%s phi=%s surface=%s)",
                 npath, entity, phi, surface)
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

    if with_kb and kb is not None:
        _ingest_note(kb, npath, entity, distilled, session, founder_os_root,
                     content=note, when=when)

    append_ledger({
        "session_id": ledger_key,
        "entity": entity,
        "phi": phi,
        "note_path": str(npath),
        "topic": distilled["topic"],
        "surface": surface,
        "captured_at": when.isoformat(),
    }, ledger_path)
    captured.add(ledger_key)
    log.info("Captured session %s -> %s (entity=%s phi=%s surface=%s)",
             ledger_key, npath.name, entity, phi, surface)
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
) -> list[CaptureResult]:
    """Harvest un-captured sessions in the lookback window. Returns results.

    Two sources, each with its own budget so neither starves the other:
      * Code sessions under ``projects_root`` (~/.claude/projects).
      * Cowork desktop agent-mode sessions (opt-in via ``include_cowork``; the
        runner enables it, the module default is OFF so unit tests / other
        callers see the exact prior Code-only behavior). Cowork ledger keys are
        ``cowork:`` prefixed to namespace them off Code-session ids.
    """
    now = _now_epoch()
    cutoff = now - lookback_hours * 3600
    settle = now - SETTLE_MINUTES * 60
    captured = load_captured_ids(ledger_path)
    results: list[CaptureResult] = []

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
        results.append(_finalize_capture(
            session, surface=SURFACE, ledger_key=session.session_id,
            captured=captured, dry_run=dry_run, with_kb=with_kb,
            founder_os_root=founder_os_root, ledger_path=ledger_path,
            anthropic_client=anthropic_client, kb=kb))

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
                results.append(CaptureResult(
                    session_id=sess_dir.name, entity="FNDR", note_path=None,
                    phi=False, distilled=False, skipped_reason="scheduled_task"))
                continue
            cw_processed += 1
            results.append(_finalize_capture(
                session, surface=SURFACE_COWORK, ledger_key=ledger_key,
                captured=captured, dry_run=dry_run, with_kb=with_kb,
                founder_os_root=founder_os_root, ledger_path=ledger_path,
                anthropic_client=anthropic_client, kb=kb))

    return results


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
