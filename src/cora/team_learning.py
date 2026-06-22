"""Team learning module -- author-side knowledge contribution intake.

A teammate contributes a fact to Cora from Slack via:

1. @Cora note: / @Cora remember: <content>      (app._handle_note)
2. A correction reply in a Cora thread           (correction signal -> _handle_note)
3. A 📚 reaction on any message                   (app._handle_bookmark_reaction)

This module screens for scope/injection (screen_contribution), paraphrases for
author confirmation (paraphrase_note), and persists the pending-confirm state
(store/get/clear_pending_confirm). On the author's "yes" (app Path-0 confirm loop)
the contribution is FOLDED into the single Harrison-gated knowledge queue
(knowledge_review.propose_update, source 'info-for-cora') and, on Harrison's 👍,
written to design/known-answers/{entity}.md via gap_autofill.apply_contributed_note
-- the same path #info-for-cora uses.

WS17-C RETIRED the old parallel approval path: the per-entity #cora-kq approval
card, the per-entity-approver ✅ tier, the pending_contributions table, and the
source='team_note' KB write. Knowledge now lives in ONE store behind ONE gate (D-011).

Contribution scope (enforced by screen_contribution):
  ALLOWED  -- factual entity knowledge: employee info/duties/tiers, document
              locations, operational facts, vendor contacts, corrections.
  REJECTED -- behavioral directives ("you should always..."), identity overrides
              ("your role is..."), suppression rules ("never say..."), cross-entity
              instructions, system-prompt-style content, or submissions >2000 chars.
  PHI      -- refused at intake (app._handle_note) and re-checked at the write
              (apply_contributed_note); client data belongs in the EHR.

Table: pending_paraphrase_confirms (in cora_kb.db) -- the only table this module
owns; keyed (channel_id, thread_ts), 24h TTL.
"""

import logging
import re
import sqlite3
import time
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

# ── Contributors registry path ─────────────────────────────────────────────────
_CONTRIBUTORS_PATH = (
    Path(__file__).parent.parent.parent / "data" / "maps" / "knowledge-contributors.yaml"
)

# ── Correction signal patterns ─────────────────────────────────────────────────
_CORRECTION_PATTERNS = [
    r"^\s*actually[,\s]",
    r"^\s*correction[:\s]",
    r"^\s*that'?s?\s+(?:not\s+)?(?:wrong|incorrect|right|accurate)",
    r"^\s*to\s+clarify[,\s:]",
    r"^\s*just\s+to\s+clarify[,\s:]",
    r"^\s*fyi[,\s:]?\s+that'?s?\s+(?:not\s+)?(?:right|correct|accurate)",
    r"^\s*small\s+correction[:\s]",
    r"^\s*quick\s+correction[:\s]",
    r"^\s*not\s+(?:quite\s+)?(?:right|correct|accurate)[,\s]",
]
_CORRECTION_RE = re.compile("|".join(_CORRECTION_PATTERNS), re.IGNORECASE)

# ── Note / remember trigger ────────────────────────────────────────────────────
# Matches "note:" or "remember:" anywhere after the bot mention
_NOTE_RE = re.compile(r"\b(?:note|remember)\s*:\s*(.+)", re.IGNORECASE | re.DOTALL)

# ── Content scope screener ────────────────────────────────────────────────────
# Contributions must be factual entity knowledge only.  These patterns catch
# attempts to inject behavioral instructions or identity overrides into the KB.

_MAX_CONTRIBUTION_CHARS = 2000

# Each tuple: (compiled pattern, short human label shown in the rejection message)
_INJECTION_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\byou\s+(?:should|must|always|never|can'?t|cannot)\b", re.I),
     "behavioral directive"),
    (re.compile(r"\balways\s+(?:respond|say|tell|answer|reply|use)\b", re.I),
     "response directive"),
    (re.compile(r"\bfrom\s+now\s+on\b|\bgoing\s+forward\b|\bstarting\s+now\b", re.I),
     "temporal behavior override"),
    (re.compile(
        r"\byour\s+(?:role|job|purpose|new\s+instructions?|instructions?\s+are|system|persona)\b",
        re.I),
     "identity or instruction override"),
    (re.compile(r"\bignore\s+(?:previous|your|all\s+previous|prior)\b", re.I),
     "instruction override"),
    (re.compile(r"\bnever\s+(?:say|mention|tell|respond|reply|discuss|reveal)\b", re.I),
     "suppression directive"),
    (re.compile(r"\bdo\s+not\s+(?:say|mention|tell|respond|discuss|reveal)\b", re.I),
     "suppression directive"),
    (re.compile(r"\bdon'?t\s+(?:say|mention|tell|respond|discuss|reveal)\b", re.I),
     "suppression directive"),
    (re.compile(
        r"\bif\s+(?:someone|anyone|a\s+user)\s+asks?\b.{0,80}\b(?:respond|say|tell|reply)\b",
        re.I | re.DOTALL),
     "conditional behavior rule"),
    (re.compile(
        r"\bwhen\s+(?:asked|someone\s+asks?)\b.{0,80}\b(?:say|respond|tell|reply)\b",
        re.I | re.DOTALL),
     "conditional behavior rule"),
    (re.compile(r"\boverride\b|\bdisregard\b|\bbypass\b", re.I),
     "system override"),
    (re.compile(r"\bsystem\s+prompt\b|\bprompt\s+injection\b", re.I),
     "system prompt reference"),
    (re.compile(r"\bact\s+as\b|\bpretend\s+(?:you\s+are|to\s+be)\b|\byou\s+are\s+now\b", re.I),
     "persona override"),
]

_SCOPE_HELP = (
    "Contributions must be *factual entity knowledge* — employee info, file locations, "
    "operational facts, or corrections. They cannot change how Cora behaves. "
    "Contact Harrison if you think this was flagged in error."
)


def screen_contribution(content: str) -> tuple[bool, str]:
    """Check a contribution for scope violations before queuing.

    Returns (True, "") if the content is acceptable, or (False, reason) if it
    should be rejected.  Reasons are user-facing Slack mrkdwn strings.
    """
    if len(content) > _MAX_CONTRIBUTION_CHARS:
        return False, (
            f"Submission is too long ({len(content):,} chars — max {_MAX_CONTRIBUTION_CHARS:,}). "
            "Break it into smaller, specific facts and submit each separately."
        )

    for pattern, label in _INJECTION_PATTERNS:
        if pattern.search(content):
            return False, (
                f"⛔ Flagged as a *{label}* — can't queue this. {_SCOPE_HELP}"
            )

    return True, ""


# ── DB path — same file as KB ─────────────────────────────────────────────────
_KB_DB_PATH = Path(__file__).parent.parent.parent / "data" / "cora_kb.db"


# ── Contributors registry ─────────────────────────────────────────────────────

_contributors_cache: dict = {}
_contributors_loaded_at: float = 0.0
_CONTRIBUTORS_TTL = 60.0  # seconds


def _load_contributors_raw() -> dict:
    """Load knowledge-contributors.yaml with a 60s TTL cache.

    Uses time-based cache instead of lru_cache to avoid permanently caching
    an empty dict if the file isn't readable on the first call at startup.
    """
    import time as _time
    global _contributors_cache, _contributors_loaded_at
    now = _time.monotonic()
    if _contributors_cache and (now - _contributors_loaded_at) < _CONTRIBUTORS_TTL:
        return _contributors_cache
    try:
        with open(_CONTRIBUTORS_PATH, encoding="utf-8") as f:
            result = yaml.safe_load(f) or {}
        if result:
            _contributors_cache = result
            _contributors_loaded_at = now
        return result
    except FileNotFoundError:
        log.warning("knowledge-contributors.yaml not found — no contributor access control")
        return {}
    except Exception as exc:
        log.error("Failed to load knowledge-contributors.yaml: %s", exc)
        return {}


def load_contributors() -> dict[str, dict]:
    """Return contributors keyed by Slack user ID."""
    return _load_contributors_raw().get("contributors", {})


def is_authorized_contributor(user_id: str, entity: str) -> bool:
    """Return True if the user is authorized to contribute knowledge for entity."""
    contributors = load_contributors()
    entry = contributors.get(user_id)
    if not entry:
        return False
    return entity in entry.get("entities", [])


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _get_conn() -> sqlite3.Connection:
    """Open a connection to cora_kb.db for the pending-paraphrase-confirms table."""
    conn = sqlite3.connect(str(_KB_DB_PATH), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_table(conn)
    return conn


def _ensure_table(conn: sqlite3.Connection) -> None:
    # WS17-C retired the pending_contributions approval queue; this module now
    # owns only the paraphrase-confirm state. An existing pending_contributions
    # table (from before the fold) is left untouched -- harmless, never read.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS pending_paraphrase_confirms (
            channel_id      TEXT NOT NULL,
            thread_ts       TEXT NOT NULL,
            entity          TEXT NOT NULL,
            channel_name    TEXT NOT NULL,
            author          TEXT NOT NULL,
            kind            TEXT NOT NULL,
            raw_content     TEXT NOT NULL,
            paraphrase      TEXT NOT NULL,
            created_at      INTEGER NOT NULL,
            preview_msg_ts  TEXT,
            PRIMARY KEY (channel_id, thread_ts)
        );
    """)
    conn.commit()
    # Idempotent migration: add preview_msg_ts to existing tables that predate this column.
    try:
        conn.execute("ALTER TABLE pending_paraphrase_confirms ADD COLUMN preview_msg_ts TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists — safe to ignore


# ── Public API ─────────────────────────────────────────────────────────────────

def parse_note(user_message: str) -> str | None:
    """Extract the note content from an @Cora note: <content> message.

    Returns the content string, or None if the message is not a note command.
    """
    m = _NOTE_RE.search(user_message)
    if not m:
        return None
    content = m.group(1).strip()
    return content if content else None


def is_correction(text: str) -> bool:
    """Return True if the text looks like a correction to a prior Cora reply."""
    return bool(_CORRECTION_RE.search(text))


# ---------------------------------------------------------------------------
# Paraphrase-confirm loop helpers
# ---------------------------------------------------------------------------
# When a team member submits a note, Cora paraphrases it and asks for
# confirmation before storing.  These three functions persist the pending
# state to SQLite so it survives a Cora restart (previously all in-memory).
# ---------------------------------------------------------------------------

_CONFIRM_TTL_SECONDS = 86_400  # 24 hours — stale confirms auto-expire


def store_pending_confirm(
    channel_id: str,
    thread_ts: str,
    entity: str,
    channel_name: str,
    author: str,
    kind: str,
    raw_content: str,
    paraphrase: str,
    preview_msg_ts: str | None = None,
) -> None:
    """Persist a pending paraphrase-confirmation record to SQLite.

    Calling this again for the same (channel_id, thread_ts) pair updates the
    record in place (REPLACE semantics), which is used when the author
    iterates on the paraphrase with corrections.

    preview_msg_ts: the ts of Cora's paraphrase message — used to update that
    message in-place via chat.update once the author confirms.
    """
    conn = _get_conn()
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO pending_paraphrase_confirms
                (channel_id, thread_ts, entity, channel_name, author, kind,
                 raw_content, paraphrase, created_at, preview_msg_ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (channel_id, thread_ts, entity, channel_name, author, kind,
             raw_content, paraphrase, int(time.time()), preview_msg_ts),
        )
        conn.commit()
    finally:
        conn.close()


def get_pending_confirm(channel_id: str, thread_ts: str) -> dict | None:
    """Return the pending confirm record for this thread, or None if absent/expired."""
    conn = _get_conn()
    try:
        row = conn.execute(
            """
            SELECT entity, channel_name, author, kind, raw_content, paraphrase,
                   created_at, preview_msg_ts
            FROM pending_paraphrase_confirms
            WHERE channel_id = ? AND thread_ts = ?
            """,
            (channel_id, thread_ts),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    entity, channel_name, author, kind, raw_content, paraphrase, created_at, preview_msg_ts = row
    # Expire records older than TTL
    if time.time() - created_at > _CONFIRM_TTL_SECONDS:
        clear_pending_confirm(channel_id, thread_ts)
        return None
    return {
        "entity": entity,
        "channel_name": channel_name,
        "author": author,
        "kind": kind,
        "raw_content": raw_content,
        "paraphrase": paraphrase,
        "created_at": created_at,
        "preview_msg_ts": preview_msg_ts,
    }


def clear_pending_confirm(channel_id: str, thread_ts: str) -> None:
    """Delete a pending confirm record once the author confirms or abandons."""
    conn = _get_conn()
    try:
        conn.execute(
            "DELETE FROM pending_paraphrase_confirms WHERE channel_id = ? AND thread_ts = ?",
            (channel_id, thread_ts),
        )
        conn.commit()
    finally:
        conn.close()


def paraphrase_note(raw_content: str, entity: str, correction: str | None = None) -> str:
    """Use Claude Haiku to produce a clean 1-2 sentence knowledge note.

    If *correction* is provided, incorporate it into the paraphrase to reflect
    the author's clarification.

    Returns the paraphrased string.  On any API error, returns the raw_content
    unchanged so the flow can continue.
    """
    import os as _os
    try:
        import anthropic as _anthropic
    except ImportError:
        return raw_content

    api_key = _os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return raw_content

    correction_clause = (
        f"\n\nThe author sent this correction: {correction!r}. "
        "Incorporate it into your revised paraphrase."
    ) if correction else ""

    prompt = (
        f"You are helping log a business knowledge note for entity {entity}.\n"
        f"Raw note from team member:\n{raw_content}\n"
        f"{correction_clause}\n\n"
        "Write a clean, factual 1-2 sentence knowledge note in third-person past tense "
        "that captures the key information. Be concise. Output only the note text."
    )

    try:
        client = _anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        return (msg.content[0].text or raw_content).strip()
    except Exception as exc:
        log.warning("paraphrase_note: Haiku call failed (%s) -- using raw content", exc)
        return raw_content


def is_confirmation(text: str) -> bool:
    """Return True if the text is a simple confirmation (yes, ok, looks good, etc.)."""
    _CONFIRM_TOKENS = frozenset({
        "yes", "yep", "yeah", "yup", "y",
        "ok", "okay", "k",
        "sure", "correct", "confirmed", "confirm",
        "looks good", "lgtm", "approved", "approve",
        "sounds good", "perfect", "great", "good",
        "send it", "go ahead", "do it",
    })
    normalized = text.strip().lower().rstrip("!.")
    return normalized in _CONFIRM_TOKENS
