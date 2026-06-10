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

Scope (confirmed reachable): Claude Code transcripts on this machine. The
Claude Desktop Cowork store is a separate, undocumented location and is NOT
harvested here — those sessions rely on the session-end self-capture doctrine.

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

from . import phi_guard

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

# Don't harvest a session whose last activity is younger than this — it may
# still be live; let it settle so we capture the finished conversation.
SETTLE_MINUTES = 30

# Distillation input cap (chars). The raw transcript stays on disk; this only
# bounds what we hand Haiku. Higher for PHI/LEX so nothing material is dropped.
_MAX_INPUT_CHARS = 24_000
_MAX_INPUT_CHARS_PHI = 60_000

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
                date_str: str, phi: bool) -> str:
    """Render the distilled session into the locked note schema."""
    def _bullets(items: list[str]) -> str:
        if not items:
            return "  - (none)"
        return "\n".join(f"  - {it}" for it in items)

    entity = distilled["entity"]
    header = f"## {date_str} — {SURFACE} — {entity} — {distilled['topic']}"
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
                  root: Path = FOUNDER_OS_ROOT) -> Path:
    """Compute the .md path: <root>/<folder>/_session-captures/YYYY-MM/<file>."""
    folder = entity_folder(entity)
    month = when.strftime("%Y-%m")
    date = when.strftime("%Y-%m-%d")
    short = session_id[:8] if session_id else uuid.uuid4().hex[:8]
    fname = f"{date}_{SURFACE}_{short}.md"
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
) -> list[CaptureResult]:
    """Harvest un-captured sessions in the lookback window. Returns results."""
    now = _now_epoch()
    cutoff = now - lookback_hours * 3600
    settle = now - SETTLE_MINUTES * 60
    captured = load_captured_ids(ledger_path)
    results: list[CaptureResult] = []
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
        phi = phi_guard.is_phi_risk(session.text)
        default_entity = entity_from_cwd(session.cwd)

        distilled = distill(
            session.text, default_entity, phi=phi, client=anthropic_client
        )
        if distilled is None:
            # Fail-closed: do not write, do not mark captured — retry next run.
            results.append(CaptureResult(
                session_id=session.session_id, entity=default_entity,
                note_path=None, phi=phi, distilled=False,
                skipped_reason="distill_failed",
            ))
            continue

        entity = distilled["entity"]
        # PHI present -> force into the LEX-scoped, access-controlled store.
        if phi:
            entity = "LEX" if not entity.startswith("LEX") else entity
            distilled["entity"] = entity

        when = datetime.now(timezone.utc)
        npath = note_path_for(entity, when, session.session_id, root=founder_os_root)
        note = render_note(distilled, session, when.strftime("%Y-%m-%d"), phi)

        result = CaptureResult(
            session_id=session.session_id, entity=entity, note_path=npath,
            phi=phi, distilled=True,
            meta={"topic": distilled["topic"], "n_turns": session.n_turns},
        )

        if dry_run:
            log.info("[DRY] would write %s (entity=%s phi=%s)", npath, entity, phi)
            results.append(result)
            continue

        try:
            npath.parent.mkdir(parents=True, exist_ok=True)
            npath.write_text(note, encoding="utf-8")
        except OSError as exc:
            log.error("session_capture: failed writing %s: %s", npath, exc)
            result.skipped_reason = "write_failed"
            result.note_path = None
            results.append(result)
            continue

        if with_kb and kb is not None:
            _ingest_note(kb, npath, entity, distilled, session, founder_os_root)

        append_ledger({
            "session_id": session.session_id,
            "entity": entity,
            "phi": phi,
            "note_path": str(npath),
            "topic": distilled["topic"],
            "captured_at": when.isoformat(),
        }, ledger_path)
        log.info("Captured session %s -> %s (entity=%s phi=%s)",
                 session.session_id[:8], npath.name, entity, phi)
        results.append(result)

    return results


def _ingest_note(kb: Any, npath: Path, entity: str, distilled: dict[str, Any],
                 session: ParsedSession, root: Path) -> None:
    """Upsert a freshly-written note into the KB immediately (idempotent).

    Uses source="static_md" + source_id=<relative path> so it shares identity
    with the nightly static_md sync (replace-on-conflict => no duplicate).
    sub_entity tagging for LEX is applied by the store's upsert Step 0.
    """
    try:
        from .knowledge_base.store import Document
        rel = str(npath.relative_to(root)) if npath.is_relative_to(root) else str(npath)
        stat = npath.stat()
        kb.upsert_documents([Document(
            source="static_md",
            source_id=rel,
            entity="LEX" if entity.startswith("LEX-") else entity,
            sub_entity=entity if entity.startswith("LEX-") else None,
            content=npath.read_text(encoding="utf-8", errors="replace"),
            date_created=int(stat.st_ctime),
            date_modified=int(stat.st_mtime),
            title=f"Session capture — {distilled['topic']}",
            deep_link=f"computer://{npath}",
            metadata={"path": rel, "session_id": session.session_id, "kind": "session_capture"},
        )])
    except Exception as exc:  # noqa: BLE001 — KB ingest is best-effort
        log.warning("session_capture: immediate KB ingest failed for %s: %s", npath, exc)
