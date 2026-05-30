"""Team learning module — write-back, correction capture, approval queue, KB ingest.

Enables the team to contribute knowledge to Cora directly from Slack and have
designated entity approvers review it before it enters the KB.

Flows:

1. Write-back  (@Cora note: / @Cora remember:)
   → Cora paraphrases what she understood and asks the author to confirm.
   → Author replies "yes" / "correct" → contribution queued in entity KQ channel.
   → Author replies with a correction → Cora re-paraphrases and repeats.

2. Bookmark (📚 reaction on any message)
   → Same confirm loop as write-back, sourced from the reacted message text.

3. Correction capture
   A reply in a Cora thread starting with a correction signal.
   → Same confirm loop, labelled as a correction.

4. Approval processing
   Called from app.py when reaction_added fires on an approval card.
   ✅ → embed + ingest to KB, mark approved.
   ❌ → mark declined, no ingest.

Tables in cora_kb.db:

  pending_contributions   — queued items waiting for approver ✅/❌
  pending_note_confirms   — items waiting for author to confirm Cora's paraphrase
"""

import logging
import re
import sqlite3
import time
import uuid
from pathlib import Path

log = logging.getLogger(__name__)

# ── Fallback approval channel (used only when no entity KQ channel exists) ────
APPROVAL_CHANNEL = "hjrg-leadership"

# ── Harrison-only audit log: every approved contribution is echoed here ───────
KB_AUDIT_CHANNEL = "cora-kb-log"

# ── Entity → KQ channel routing for approval cards ────────────────────────────
# Each entity's contributions route to its private KQ channel so the designated
# approver(s) for that entity see them — not just Harrison in hjrg-leadership.
_ENTITY_KQ_CHANNEL: dict[str, str] = {
    "FNDR":    "cora-kq-fndr",
    "HJRG":    "cora-kq-hjrg",
    "F3E":     "cora-kq-f3e",
    "LEX":     "cora-kq-lex",
    "LEX-LLC": "cora-kq-lex-llc",
    "LEX-LTS": "cora-kq-lex-lts",
    "LEX-LBHS":"cora-kq-lex-lbhs",
    "LEX-LLA": "cora-kq-lex-lla",
    "OSN":     "cora-kq-osn",
    "OSNGM":   "cora-kq-osngm",
    "OSNVV":   "cora-kq-osnvv",
    "OSNGF":   "cora-kq-osngf",
    "OSNGW":   "cora-kq-osngw",
    "BDM":     "cora-kq-bdm",
}


def kq_channel_for_entity(entity: str) -> str:
    """Return the KQ channel name for the entity, or the fallback approval channel."""
    return _ENTITY_KQ_CHANNEL.get(entity, APPROVAL_CHANNEL)


# ── Confirmation detection ─────────────────────────────────────────────────────
_CONFIRM_RE = re.compile(
    r"^\s*(?:yes|yep|yup|yeah|correct|confirmed|confirm|that(?:'s|s) (?:right|correct)"
    r"|looks? good|perfect|exactly|approved|approve|sounds? good|great|right|good|ok(?:ay)?|"
    r"affirmative|spot on|100%|✅|👍)\s*[.!]?\s*$",
    re.IGNORECASE,
)


def is_confirmation(text: str) -> bool:
    """Return True if the text is a simple confirmation of Cora's paraphrase."""
    return bool(_CONFIRM_RE.match(text.strip()))


# ── Pending confirmation state (SQLite, survives restarts) ────────────────────

def _ensure_confirm_table(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS pending_note_confirms (
            channel_id    TEXT NOT NULL,
            thread_ts     TEXT NOT NULL,
            entity        TEXT NOT NULL,
            channel_name  TEXT NOT NULL,
            author        TEXT NOT NULL,
            kind          TEXT NOT NULL DEFAULT 'note',
            raw_content   TEXT NOT NULL,
            paraphrase    TEXT NOT NULL,
            created_at    INTEGER NOT NULL,
            PRIMARY KEY (channel_id, thread_ts)
        );
    """)
    conn.commit()


def _get_confirm_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_KB_DB_PATH), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_confirm_table(conn)
    return conn


def store_pending_confirm(
    *,
    channel_id: str,
    thread_ts: str,
    entity: str,
    channel_name: str,
    author: str,
    kind: str,
    raw_content: str,
    paraphrase: str,
) -> None:
    """Save the awaiting-confirmation state for a thread."""
    conn = _get_confirm_conn()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO pending_note_confirms
               (channel_id, thread_ts, entity, channel_name, author, kind,
                raw_content, paraphrase, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (channel_id, thread_ts, entity, channel_name, author, kind,
             raw_content, paraphrase, int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()


def get_pending_confirm(channel_id: str, thread_ts: str) -> dict | None:
    """Return the pending confirmation state for a thread, or None."""
    conn = _get_confirm_conn()
    try:
        row = conn.execute(
            """SELECT channel_id, thread_ts, entity, channel_name, author, kind,
                      raw_content, paraphrase, created_at
               FROM pending_note_confirms WHERE channel_id=? AND thread_ts=?""",
            (channel_id, thread_ts),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    keys = ["channel_id", "thread_ts", "entity", "channel_name", "author", "kind",
            "raw_content", "paraphrase", "created_at"]
    return dict(zip(keys, row))


def clear_pending_confirm(channel_id: str, thread_ts: str) -> None:
    """Remove the confirmation state once resolved (confirmed or timed out)."""
    conn = _get_confirm_conn()
    try:
        conn.execute(
            "DELETE FROM pending_note_confirms WHERE channel_id=? AND thread_ts=?",
            (channel_id, thread_ts),
        )
        conn.commit()
    finally:
        conn.close()


# ── Paraphrase generation ──────────────────────────────────────────────────────

def paraphrase_note(raw_content: str, entity: str, correction: str | None = None) -> str:
    """Use Claude Haiku to generate a clear recap of what the user wants Cora to remember.

    If `correction` is provided, it's incorporated into an updated paraphrase.
    Returns a formatted string ready to post to Slack.
    """
    try:
        import anthropic as _anthropic
        from .config import config as _config

        client = _anthropic.Anthropic(api_key=_config.anthropic_api_key)

        if correction:
            user_text = (
                f"Original note: {raw_content}\n"
                f"User's correction: {correction}"
            )
            instruction = (
                "The user submitted a note and then corrected it. "
                "Incorporate the correction and write a fresh, specific recap."
            )
        else:
            user_text = raw_content
            instruction = "The user wants Cora to remember this."

        prompt = (
            f"{instruction}\n\n"
            f"Entity context: {entity}\n"
            f"Content: {user_text}\n\n"
            "Write a clear, specific recap (2-4 sentences) of the key facts, names, dates, "
            "and details. Start with \"Here's what I understood:\". End with "
            "\"Is this correct, or would you like to adjust anything?\""
        )

        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception as exc:
        log.warning("paraphrase_note failed, using raw content: %s", exc)
        return (
            f"Here's what I understood: {raw_content[:500]}\n"
            "Is this correct, or would you like to adjust anything?"
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

# ── Note trigger ───────────────────────────────────────────────────────────────
# Matches "note:" or "remember:" anywhere after the bot mention
_NOTE_RE = re.compile(r"\b(?:note|remember)\s*:\s*(.+)", re.IGNORECASE | re.DOTALL)

# ── DB path — same file as KB ─────────────────────────────────────────────────
_KB_DB_PATH = Path(__file__).parent.parent.parent / "data" / "cora_kb.db"


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
    """Build the Slack message text for an approval card posted to the entity's KQ channel."""
    kind_label = "📝 Team Note" if kind == "note" else ("📚 Bookmark" if kind == "bookmark" else "🔄 Correction")
    short_id = contribution_id[:8]
    return (
        f"{kind_label} pending approval `[{short_id}]`\n"
        f"*Entity:* {entity}  |  *Channel:* #{channel_name}  |  *From:* <@{author}>\n"
        f"```\n{content[:800]}\n```\n"
        f"React ✅ to approve → enters Cora's KB  |  ❌ to decline"
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
