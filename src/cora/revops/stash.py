"""Server-side send stash (R2): the byte-exact staging area for Tier-1 sends.

F-23 doctrine, adapted for email: the approve card stashes the fully rendered
message SERVER-SIDE; approval sends the STASH, never a model re-echo. The
stash id rides in the Block Kit button value (thread-anchored identity, not
the (user, channel) key -- the documented wrong-pending residual).

- Persistent (SQLite, same DB as the ledger) so a bot restart cannot orphan an
  in-flight card into a phantom-approve.
- 48h expiry: an expired stash can NEVER fire; expiry purges the body text
  (the ledger/audit keep only the sha256).
- Single-shot: status moves staged -> sent|cancelled|expired exactly once.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from typing import Any, Optional

from . import ledger

STASH_TTL_SECONDS = 48 * 3600


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def create_stash(
    conn: sqlite3.Connection,
    *,
    thread_key: str,
    mailbox: str,
    playbook_id: str,
    gmail_thread_id: str,
    recipients: list[str],
    cc: Optional[list[str]] = None,
    subject: Optional[str] = None,
    body_text: str,
    guard_results: Optional[dict[str, Any]] = None,
    thread_last_msg_id: Optional[str] = None,
) -> str:
    stash_id = secrets.token_hex(8)
    now = time.time()
    conn.execute(
        """
        INSERT INTO send_stashes (
            stash_id, thread_key, mailbox, playbook_id, gmail_thread_id,
            recipients, cc, subject, body_text, body_sha256, guard_results,
            status, created_ts, expires_ts, thread_last_msg_id
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,'staged',?,?,?)
        """,
        (
            stash_id,
            thread_key,
            mailbox.strip().lower(),
            playbook_id,
            gmail_thread_id,
            json.dumps([r.strip().lower() for r in recipients]),
            json.dumps([c.strip().lower() for c in (cc or [])]),
            subject,
            body_text,
            sha256_text(body_text),
            json.dumps(guard_results or {}),
            now,
            now + STASH_TTL_SECONDS,
            thread_last_msg_id,
        ),
    )
    conn.commit()
    return stash_id


def get_stash(conn: sqlite3.Connection, stash_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM send_stashes WHERE stash_id = ?", (stash_id,)
    ).fetchone()


def get_staged_for_thread(
    conn: sqlite3.Connection, thread_key: str, now: Optional[float] = None
) -> Optional[sqlite3.Row]:
    """The live stash for a thread, if any.

    'sending' counts as LIVE: a stash claimed for send is in flight in the bot
    process, and the sweep (a separate process) must not read it as dead and
    stage a duplicate card mid-send (D-051 lens 2).
    """
    ts = now if now is not None else time.time()
    return conn.execute(
        "SELECT * FROM send_stashes WHERE thread_key = ? "
        "AND status IN ('staged','sending') AND expires_ts > ? "
        "ORDER BY created_ts DESC LIMIT 1",
        (thread_key, ts),
    ).fetchone()


def cancel_staged_for_thread(conn: sqlite3.Connection, thread_key: str) -> int:
    cur = conn.execute(
        "UPDATE send_stashes SET status='cancelled', body_text=NULL "
        "WHERE thread_key = ? AND status = 'staged'",
        (thread_key,),
    )
    conn.commit()
    return cur.rowcount


def is_expired(row: sqlite3.Row, now: Optional[float] = None) -> bool:
    return (now if now is not None else time.time()) > (row["expires_ts"] or 0)


def set_card_ref(
    conn: sqlite3.Connection, stash_id: str, *, channel: str, ts: str
) -> None:
    conn.execute(
        "UPDATE send_stashes SET card_channel = ?, card_ts = ? WHERE stash_id = ?",
        (channel, ts, stash_id),
    )
    conn.commit()


def claim_for_send(
    conn: sqlite3.Connection, stash_id: str, *, approved_by: str
) -> bool:
    """Atomically claim a staged stash for sending (staged -> sending).

    Single-shot by construction: a double-tap loses the UPDATE race and gets
    False. A crash mid-'sending' leaves a row that can never be claimed again
    (safe: no double-send; expiry sweeps it to expired later).
    """
    cur = conn.execute(
        "UPDATE send_stashes SET status='sending', approved_by=? "
        "WHERE stash_id = ? AND status = 'staged' AND expires_ts > ?",
        (approved_by, stash_id, time.time()),
    )
    conn.commit()
    return cur.rowcount == 1


def finalize_sent(conn: sqlite3.Connection, stash_id: str) -> None:
    """sending -> sent, PURGING the body (the sha256 is the durable record).

    Terminal rows keep no message text: the 48h body-purge contract is about
    bodies at rest, not just about expiry (D-051 lens 7)."""
    conn.execute(
        "UPDATE send_stashes SET status='sent', sent_ts=?, body_text=NULL "
        "WHERE stash_id = ? AND status = 'sending'",
        (time.time(), stash_id),
    )
    conn.commit()


def mark_send_failed(conn: sqlite3.Connection, stash_id: str, *, indeterminate: bool = False) -> None:
    """sending -> send_failed (body purged; the stash can never re-fire).

    indeterminate=True records that the Gmail call may have been accepted
    before the error surfaced, so the outcome is UNKNOWN rather than 'not sent'.
    """
    conn.execute(
        "UPDATE send_stashes SET status=?, body_text=NULL "
        "WHERE stash_id = ? AND status = 'sending'",
        ("send_indeterminate" if indeterminate else "send_failed", stash_id),
    )
    conn.commit()


def cancel_stash(conn: sqlite3.Connection, stash_id: str) -> bool:
    """staged -> cancelled (skip/edit/close). Purges the body immediately."""
    cur = conn.execute(
        "UPDATE send_stashes SET status='cancelled', body_text=NULL "
        "WHERE stash_id = ? AND status = 'staged'",
        (stash_id,),
    )
    conn.commit()
    return cur.rowcount == 1


def expire_stale(conn: sqlite3.Connection, now: Optional[float] = None) -> int:
    """Expire every overdue non-terminal stash and PURGE its body text.

    An expired stash can never fire: claim_for_send requires status='staged'
    AND expires_ts in the future, and this sweep removes the bytes entirely.
    Covers stuck 'sending' rows too (crash mid-send: card is dead, thread
    re-enters nudge_due at the next sweep).
    """
    ts = now if now is not None else time.time()
    cur = conn.execute(
        "UPDATE send_stashes SET status='expired', body_text=NULL "
        "WHERE status IN ('staged','sending') AND expires_ts <= ?",
        (ts,),
    )
    conn.commit()
    return cur.rowcount
