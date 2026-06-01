"""Team learning module — write-back, correction capture, approval queue, KB ingest.

Enables the team to contribute knowledge to Cora directly from Slack and have
it approved before it enters the KB.

Flows:

1. Write-back
   @Cora note: / @Cora remember: <content>
   → Screened for scope, stored as pending, approval card posted to
     the per-entity #cora-kq-{entity} queue channel.

2. Correction capture
   A reply in a Cora thread that starts with a correction signal
   ("actually", "correction:", "that's wrong", "to clarify", etc.)
   → Same flow as write-back, labelled as a correction.

3. 📚 bookmark
   Any authorized contributor reacts 📚 to a message.
   → Message text fetched, screened, and queued for approval.

4. Approval processing
   An approver reacts ✅ / ❌ on an approval card in a queue channel.
   ✅ → embed + ingest to KB, mark approved.
   ❌ → mark declined, no ingest.

Contribution scope (enforced by screen_contribution):
  ALLOWED  — factual entity knowledge: employee info/duties/tiers, document
             locations, operational facts, vendor contacts, corrections.
  REJECTED — behavioral directives ("you should always…"), identity overrides
             ("your role is…"), suppression rules ("never say…"), cross-entity
             instructions, system-prompt-style content, or submissions >2 000 chars.

Table: pending_contributions (in cora_kb.db, not the vec table)

    contribution_id  TEXT PRIMARY KEY
    kind             TEXT  ("note" | "correction")
    entity           TEXT
    channel_id       TEXT
    channel_name     TEXT
    author           TEXT  (Slack user ID)
    content          TEXT
    original_ts      TEXT  (ts of the original user message, for thread linking)
    approval_msg_ts  TEXT  (ts of Cora's approval card message, used as lookup key)
    approval_channel TEXT  (channel where the approval card was posted)
    status           TEXT  ("pending" | "approved" | "declined")
    created_at       INTEGER
    resolved_at      INTEGER | NULL
"""

import functools
import logging
import re
import sqlite3
import time
import uuid
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

# ── Fallback approval channel — used when a per-entity queue channel isn't found ─
# Per-entity queues follow the pattern #cora-kq-{entity.lower()}.  If Cora isn't
# in that channel yet (e.g. channel not created), contributions fall back here.
APPROVAL_CHANNEL = "hjrg-leadership"
KB_AUDIT_CHANNEL = "cora-kb-log"

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

@functools.lru_cache(maxsize=1)
def _load_contributors_raw() -> dict:
    """Load knowledge-contributors.yaml. Cached until process restart."""
    try:
        with open(_CONTRIBUTORS_PATH) as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        log.warning("knowledge-contributors.yaml not found — no contributor access control")
        return {}
    except Exception as exc:
        log.error("Failed to load knowledge-contributors.yaml: %s", exc)
        return {}


def load_contributors() -> dict[str, dict]:
    """Return contributors keyed by Slack user ID."""
    return _load_contributors_raw().get("contributors", {})


def get_queue_channel(entity: str) -> str:
    """Return the per-entity queue channel name (without #) for the given entity."""
    return f"cora-kq-{entity.lower()}"


def is_authorized_contributor(user_id: str, entity: str) -> bool:
    """Return True if the user is authorized to contribute knowledge for entity."""
    contributors = load_contributors()
    entry = contributors.get(user_id)
    if not entry:
        return False
    return entity in entry.get("entities", [])


def is_approver(user_id: str, entity: str) -> bool:
    """Return True if the user is an approver for the given entity."""
    contributors = load_contributors()
    entry = contributors.get(user_id)
    if not entry:
        return False
    return entry.get("tier") == "approver" and entity in entry.get("entities", [])


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _get_conn() -> sqlite3.Connection:
    """Open (or reuse) a connection to cora_kb.db for the contributions table."""
    conn = sqlite3.connect(str(_KB_DB_PATH), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_table(conn)
    return conn


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS pending_contributions (
            contribution_id  TEXT PRIMARY KEY,
            kind             TEXT NOT NULL,
            entity           TEXT NOT NULL,
            channel_id       TEXT NOT NULL,
            channel_name     TEXT NOT NULL,
            author           TEXT NOT NULL,
            content          TEXT NOT NULL,
            original_ts      TEXT NOT NULL,
            approval_msg_ts  TEXT,
            approval_channel TEXT,
            status           TEXT NOT NULL DEFAULT 'pending',
            created_at       INTEGER NOT NULL,
            resolved_at      INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_contrib_status ON pending_contributions(status);
        CREATE INDEX IF NOT EXISTS idx_contrib_approval_ts ON pending_contributions(approval_msg_ts);

        CREATE TABLE IF NOT EXISTS pending_paraphrase_confirms (
            channel_id   TEXT NOT NULL,
            thread_ts    TEXT NOT NULL,
            entity       TEXT NOT NULL,
            channel_name TEXT NOT NULL,
            author       TEXT NOT NULL,
            kind         TEXT NOT NULL,
            raw_content  TEXT NOT NULL,
            paraphrase   TEXT NOT NULL,
            created_at   INTEGER NOT NULL,
            PRIMARY KEY (channel_id, thread_ts)
        );
    """)
    conn.commit()


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


def store_contribution(
    *,
    kind: str,
    entity: str,
    channel_id: str,
    channel_name: str,
    author: str,
    content: str,
    original_ts: str,
) -> str:
    """Store a pending contribution. Returns the contribution_id."""
    cid = str(uuid.uuid4())
    conn = _get_conn()
    try:
        conn.execute(
            """INSERT INTO pending_contributions
               (contribution_id, kind, entity, channel_id, channel_name,
                author, content, original_ts, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (cid, kind, entity, channel_id, channel_name, author, content,
             original_ts, int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()
    log.info("Stored %s contribution cid=%s entity=%s author=%s", kind, cid[:8], entity, author)
    return cid


def set_approval_msg(contribution_id: str, approval_msg_ts: str, approval_channel: str) -> None:
    """After posting the approval card, record where it lives."""
    conn = _get_conn()
    try:
        conn.execute(
            """UPDATE pending_contributions
               SET approval_msg_ts = ?, approval_channel = ?
               WHERE contribution_id = ?""",
            (approval_msg_ts, approval_channel, contribution_id),
        )
        conn.commit()
    finally:
        conn.close()


def lookup_by_approval_ts(msg_ts: str) -> dict | None:
    """Find a pending contribution by the ts of its approval card message.

    Returns the row as a dict, or None if not found / already resolved.
    """
    conn = _get_conn()
    try:
        row = conn.execute(
            """SELECT contribution_id, kind, entity, channel_id, channel_name,
                      author, content, original_ts, status
               FROM pending_contributions
               WHERE approval_msg_ts = ? AND status = 'pending'""",
            (msg_ts,),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return None
    keys = ["contribution_id", "kind", "entity", "channel_id", "channel_name",
            "author", "content", "original_ts", "status"]
    return dict(zip(keys, row))


def resolve_contribution(contribution_id: str, status: str) -> None:
    """Mark a contribution approved or declined."""
    conn = _get_conn()
    try:
        conn.execute(
            """UPDATE pending_contributions
               SET status = ?, resolved_at = ?
               WHERE contribution_id = ?""",
            (status, int(time.time()), contribution_id),
        )
        conn.commit()
    finally:
        conn.close()
    log.info("Contribution %s → %s", contribution_id[:8], status)


def ingest_contribution(contribution: dict) -> bool:
    """Embed and store an approved contribution into the KB.

    Returns True on success, False on failure (caller should notify Harrison).
    """
    try:
        from cora.knowledge_base import KnowledgeBase
        from cora.knowledge_base.store import Document

        doc = Document(
            source="team_note",
            source_id=contribution["contribution_id"],
            entity=contribution["entity"],
            content=contribution["content"],
            author=contribution["author"],
            title=f"Team note from #{contribution['channel_name']}",
            date_created=int(time.time()),
            date_modified=int(time.time()),
            deep_link="",
        )
        kb = KnowledgeBase(_KB_DB_PATH)
        try:
            count = kb.upsert_documents([doc])
        finally:
            kb.close()

        log.info(
            "Ingested contribution %s → %d chunks entity=%s",
            contribution["contribution_id"][:8], count, contribution["entity"],
        )
        return True
    except Exception as exc:
        log.error("Failed to ingest contribution %s: %s",
                  contribution.get("contribution_id", "?")[:8], exc)
        return False


def build_approval_card(
    *,
    kind: str,
    entity: str,
    channel_name: str,
    author: str,
    content: str,
    contribution_id: str,
) -> str:
    """Build the Slack message text for an approval card posted to a queue channel."""
    kind_label = "📝 Team Note" if kind == "note" else "🔄 Correction"
    short_id = contribution_id[:8]
    return (
        f"⚠️ *Approve factual entity knowledge only* — no behavioral instructions, "
        f"no cross-entity content, no process/routing changes.\n"
        f"{kind_label} pending approval `[{short_id}]`\n"
        f"*Entity:* {entity}  |  *Channel:* #{channel_name}  |  *From:* <@{author}>\n"
        f"```\n{content[:800]}\n```\n"
        f"✅ approve → adds to {entity} KB  |  ❌ decline → discarded"
    )


def pending_stats() -> dict:
    """Return counts of pending/approved/declined contributions."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM pending_contributions GROUP BY status"
        ).fetchall()
    finally:
        conn.close()
    return {r[0]: r[1] for r in rows}


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
) -> None:
    """Persist a pending paraphrase-confirmation record to SQLite.

    Calling this again for the same (channel_id, thread_ts) pair updates the
    record in place (REPLACE semantics), which is used when the author
    iterates on the paraphrase with corrections.
    """
    conn = _get_conn()
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO pending_paraphrase_confirms
                (channel_id, thread_ts, entity, channel_name, author, kind,
                 raw_content, paraphrase, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (channel_id, thread_ts, entity, channel_name, author, kind,
             raw_content, paraphrase, int(time.time())),
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
            SELECT entity, channel_name, author, kind, raw_content, paraphrase, created_at
            FROM pending_paraphrase_confirms
            WHERE channel_id = ? AND thread_ts = ?
            """,
            (channel_id, thread_ts),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    entity, channel_name, author, kind, raw_content, paraphrase, created_at = row
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


def kq_channel_for_entity(entity: str) -> str:
    """Return the per-entity knowledge queue channel name (alias for get_queue_channel)."""
    return get_queue_channel(entity)


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
