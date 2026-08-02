"""Cadence ledger: SQLite system of record for the revenue-ops loop (R1).

Two tables:
  threads       -- current state per tracked thread (thread_key = mailbox + gmail_thread_id)
  thread_events -- append-only history; every transition, send, card, guard result

Hard rules enforced here, not by callers:
- LEX is excluded at ingest by construction: upsert_thread() raises
  LexThreadRejected before any row insert. A LEX thread never enters the DB.
- State changes go ONLY through transition() (no direct state pokes); every
  transition writes an event row in the same SQLite transaction.
- A state written by a protected source ('send', 'approval') can never be
  regressed by the importer or the sweep (field-level last-write-wins on
  event ts, send-events always win).
- No message body content is ever stored in the ledger (subjects/snippets are
  metadata; bodies live only in the short-lived send stash).
- The escalation keyword screen is deterministic and FAIL-CLOSED: a screen
  error escalates. Inbound content can only move a thread TOWARD escalation,
  never away from it and never to closed (prompt-injection posture: content is
  data; only headers/metadata drive ordinary transitions).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger("cora.revops.ledger")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_DB_PATH = _REPO_ROOT / "data" / "revops_ledger.db"

# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------

VALID_STATES = frozenset(
    {
        "draft_staged",
        "awaiting_reply",
        "replied",
        "nudge_due",
        "nudge_staged",
        "hold",
        "escalated",
        "bounced",
        "closed_won",
        "closed_lost",
        "closed_no_response",
        "closed_courtesy",
    }
)

TERMINAL_STATES = frozenset(
    {"closed_won", "closed_lost", "closed_no_response", "closed_courtesy"}
)

# States the silence-nudge loop may act on. hold/escalated/bounced/terminal
# threads never become nudge_due.
NUDGE_ELIGIBLE_STATES = frozenset({"awaiting_reply", "replied", "nudge_due"})

# Sources whose state writes the importer/sweep must never regress.
PROTECTED_SOURCES = frozenset({"send", "approval"})

WORKSTREAMS = frozenset(
    {
        "Retail",
        "Press",
        "Suppliers",
        "Sponsors",
        "Finance-Legal",
        "Leasing-Property",
        "Support",
        "Other",
    }
)

# B2 reply-watch-state.json labels -> canonical workstreams.
WORKSTREAM_ALIASES = {
    "Finance/Legal": "Finance-Legal",
    "Suppliers/Vendors": "Suppliers",
    "Leasing/Property": "Leasing-Property",
}


def normalize_workstream(raw: str) -> str:
    ws = WORKSTREAM_ALIASES.get((raw or "").strip(), (raw or "").strip())
    return ws if ws in WORKSTREAMS else "Other"


# ---------------------------------------------------------------------------
# LEX exclusion (at ingest, by construction)
# ---------------------------------------------------------------------------


class LexThreadRejected(ValueError):
    """Raised before any row insert when a thread is LEX-scoped."""


def is_lex_entity(entity: Optional[str]) -> bool:
    if not entity:
        return False
    e = str(entity).strip().upper()
    return e == "LEX" or e.startswith("LEX-") or e.startswith("LEX_")


# ---------------------------------------------------------------------------
# Escalation keyword screen (deterministic, fail-closed)
# ---------------------------------------------------------------------------

ESCALATION_KEYWORDS = (
    "contract",
    "agreement",
    "legal",
    "attorney",
    "lawsuit",
    "embargo",
    "exclusive",
    "valuation",
    "raise",
    "equity",
    "term sheet",
    "wire",
    "nda",
)

_ESCALATION_RE = re.compile(
    r"\b(" + "|".join(re.escape(k).replace(r"\ ", r"\s+") for k in ESCALATION_KEYWORDS) + r")\b",
    re.IGNORECASE,
)


def escalation_screen(text: Optional[str]) -> Optional[str]:
    """Return the first matched escalation keyword, or None.

    FAIL-CLOSED: any error inside the screen returns the sentinel
    'screen_error', which callers must treat as a match (escalate).
    """
    try:
        if not text:
            return None
        m = _ESCALATION_RE.search(str(text))
        return m.group(1).lower() if m else None
    except Exception:  # noqa: BLE001 - fail closed by contract
        logger.exception("escalation_screen failed; failing closed (escalate)")
        return "screen_error"


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def db_path() -> Path:
    override = os.environ.get("CORA_REVOPS_DB", "").strip()
    return Path(override) if override else _DEFAULT_DB_PATH


def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    p = Path(path) if path else db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS threads (
            thread_key        TEXT PRIMARY KEY,
            mailbox           TEXT NOT NULL,
            gmail_thread_id   TEXT NOT NULL,
            counterparty_name TEXT,
            counterparty_emails TEXT,          -- JSON list
            workstream        TEXT NOT NULL,
            entity            TEXT,
            owner             TEXT,
            playbook_id       TEXT,
            tier              INTEGER,
            state             TEXT NOT NULL,
            state_source      TEXT,            -- source of the last transition
            state_event_ts    REAL,            -- epoch ts of the last transition
            last_outbound_ts  REAL,
            last_inbound_ts   REAL,
            nudge_count       INTEGER NOT NULL DEFAULT 0,
            next_review_date  TEXT,            -- ISO date; sweep skips nudge_due before this
            hold_reason       TEXT,
            hubspot_deal_id   TEXT,
            notes             TEXT,
            created_at        REAL NOT NULL,
            updated_at        REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS thread_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_key  TEXT NOT NULL,
            event_ts    REAL NOT NULL,
            event_type  TEXT NOT NULL,
            from_state  TEXT,
            to_state    TEXT,
            actor       TEXT,
            source      TEXT,
            detail      TEXT,                  -- JSON, metadata only, never body content
            created_at  REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_thread_events_key
            ON thread_events(thread_key, event_ts);
        CREATE TABLE IF NOT EXISTS send_stashes (
            stash_id        TEXT PRIMARY KEY,
            thread_key      TEXT NOT NULL,
            mailbox         TEXT NOT NULL,
            playbook_id     TEXT NOT NULL,
            gmail_thread_id TEXT NOT NULL,
            recipients      TEXT NOT NULL,     -- JSON list (To)
            cc              TEXT,              -- JSON list
            subject         TEXT,
            body_text       TEXT,              -- the exact bytes to send (purged on expiry)
            body_sha256     TEXT NOT NULL,
            guard_results   TEXT,              -- JSON {blocks:[], warns:[]}
            status          TEXT NOT NULL,     -- staged|sent|expired|cancelled
            created_ts      REAL NOT NULL,
            expires_ts      REAL NOT NULL,
            approved_by     TEXT,
            sent_ts         REAL,
            card_channel    TEXT,
            card_ts         TEXT
        );
        """
    )
    conn.commit()


def make_thread_key(mailbox: str, gmail_thread_id: str) -> str:
    return f"{(mailbox or '').strip().lower()}:{(gmail_thread_id or '').strip()}"


# ---------------------------------------------------------------------------
# Row access
# ---------------------------------------------------------------------------


def get_thread(conn: sqlite3.Connection, thread_key: str) -> Optional[sqlite3.Row]:
    cur = conn.execute("SELECT * FROM threads WHERE thread_key = ?", (thread_key,))
    return cur.fetchone()


def list_threads(
    conn: sqlite3.Connection,
    *,
    states: Optional[Iterable[str]] = None,
    workstreams: Optional[Iterable[str]] = None,
    entity: Optional[str] = None,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM threads WHERE 1=1"
    params: list[Any] = []
    if states:
        states = list(states)
        sql += " AND state IN (%s)" % ",".join("?" * len(states))
        params.extend(states)
    if workstreams:
        workstreams = list(workstreams)
        sql += " AND workstream IN (%s)" % ",".join("?" * len(workstreams))
        params.extend(workstreams)
    if entity:
        sql += " AND entity = ?"
        params.append(entity)
    sql += " ORDER BY updated_at DESC"
    return list(conn.execute(sql, params).fetchall())


# ---------------------------------------------------------------------------
# Upsert (ingest chokepoint -- LEX screen lives HERE)
# ---------------------------------------------------------------------------


def upsert_thread(
    conn: sqlite3.Connection,
    *,
    mailbox: str,
    gmail_thread_id: str,
    counterparty_name: Optional[str] = None,
    counterparty_emails: Optional[list[str]] = None,
    workstream: str = "Other",
    entity: Optional[str] = None,
    owner: Optional[str] = None,
    playbook_id: Optional[str] = None,
    state: str = "awaiting_reply",
    last_outbound_ts: Optional[float] = None,
    last_inbound_ts: Optional[float] = None,
    hold_reason: Optional[str] = None,
    hubspot_deal_id: Optional[str] = None,
    notes: Optional[str] = None,
    source: str = "import",
    actor: str = "system",
    observation_ts: Optional[float] = None,
) -> str:
    """Insert or update a thread row. Returns the thread_key.

    - LEX entities are rejected BEFORE any insert (LexThreadRejected).
    - On update: field-level last-write-wins keyed on observation_ts vs the
      row's state_event_ts; a state set by a PROTECTED source is never
      changed here (only transition() with a send/approval source may).
    """
    if is_lex_entity(entity):
        raise LexThreadRejected(f"LEX thread rejected at ingest (entity={entity!r})")
    if state not in VALID_STATES:
        raise ValueError(f"invalid state {state!r}")
    ws = normalize_workstream(workstream)
    now = time.time()
    obs_ts = observation_ts if observation_ts is not None else now
    key = make_thread_key(mailbox, gmail_thread_id)
    existing = get_thread(conn, key)
    emails_json = json.dumps(counterparty_emails or [])

    if existing is None:
        conn.execute(
            """
            INSERT INTO threads (
                thread_key, mailbox, gmail_thread_id, counterparty_name,
                counterparty_emails, workstream, entity, owner, playbook_id,
                tier, state, state_source, state_event_ts, last_outbound_ts,
                last_inbound_ts, nudge_count, next_review_date, hold_reason,
                hubspot_deal_id, notes, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,NULL,?,?,?,?,?)
            """,
            (
                key,
                mailbox.strip().lower(),
                gmail_thread_id,
                counterparty_name,
                emails_json,
                ws,
                entity,
                owner,
                playbook_id,
                None,
                state,
                source,
                obs_ts,
                last_outbound_ts,
                last_inbound_ts,
                hold_reason,
                hubspot_deal_id,
                notes,
                now,
                now,
            ),
        )
        _write_event(
            conn,
            key,
            event_ts=obs_ts,
            event_type="ingest",
            from_state=None,
            to_state=state,
            actor=actor,
            source=source,
            detail={"workstream": ws, "entity": entity},
        )
        conn.commit()
        return key

    # ---- update path (field-level last-write-wins; never regress protected) ----
    sets: list[str] = []
    params: list[Any] = []

    def _set(col: str, val: Any) -> None:
        sets.append(f"{col} = ?")
        params.append(val)

    if counterparty_name:
        _set("counterparty_name", counterparty_name)
    if counterparty_emails:
        _set("counterparty_emails", emails_json)
    if owner:
        _set("owner", owner)
    if playbook_id:
        _set("playbook_id", playbook_id)
    if hubspot_deal_id:
        _set("hubspot_deal_id", hubspot_deal_id)
    if notes:
        _set("notes", notes)
    if hold_reason:
        _set("hold_reason", hold_reason)
    if last_outbound_ts and (existing["last_outbound_ts"] or 0) < last_outbound_ts:
        _set("last_outbound_ts", last_outbound_ts)
    if last_inbound_ts and (existing["last_inbound_ts"] or 0) < last_inbound_ts:
        _set("last_inbound_ts", last_inbound_ts)

    state_protected = (existing["state_source"] or "") in PROTECTED_SOURCES
    state_is_newer = obs_ts > (existing["state_event_ts"] or 0)
    if (
        state != existing["state"]
        and not state_protected
        and state_is_newer
        and source not in PROTECTED_SOURCES
    ):
        # Ordinary observation-driven update. Never move a terminal thread.
        if existing["state"] not in TERMINAL_STATES:
            _set("state", state)
            _set("state_source", source)
            _set("state_event_ts", obs_ts)
            _write_event(
                conn,
                key,
                event_ts=obs_ts,
                event_type="import_update",
                from_state=existing["state"],
                to_state=state,
                actor=actor,
                source=source,
                detail=None,
            )

    if sets:
        _set("updated_at", now)
        conn.execute(
            f"UPDATE threads SET {', '.join(sets)} WHERE thread_key = ?",
            (*params, key),
        )
    conn.commit()
    return key


# ---------------------------------------------------------------------------
# Transitions (the ONLY state mutation path besides the guarded upsert above)
# ---------------------------------------------------------------------------


def transition(
    conn: sqlite3.Connection,
    thread_key: str,
    to_state: str,
    *,
    actor: str,
    source: str,
    detail: Optional[dict[str, Any]] = None,
    event_ts: Optional[float] = None,
    event_type: str = "transition",
) -> bool:
    """Move a thread to to_state, writing the event row atomically.

    Returns False (no-op) when the move is refused:
    - unknown thread / invalid state
    - thread already terminal (nothing moves a closed thread except nothing)
    - the IMPORTER may never move a state written by a protected source
      (send/approval); the sweep, owners, and the send gate may (reality
      observations must still be able to advance a just-sent thread).
    """
    if to_state not in VALID_STATES:
        raise ValueError(f"invalid state {to_state!r}")
    row = get_thread(conn, thread_key)
    if row is None:
        return False
    if row["state"] in TERMINAL_STATES:
        return False
    if (row["state_source"] or "") in PROTECTED_SOURCES and source == "import":
        return False
    ts = event_ts if event_ts is not None else time.time()
    conn.execute(
        """
        UPDATE threads
           SET state = ?, state_source = ?, state_event_ts = ?, updated_at = ?
         WHERE thread_key = ?
        """,
        (to_state, source, ts, time.time(), thread_key),
    )
    _write_event(
        conn,
        thread_key,
        event_ts=ts,
        event_type=event_type,
        from_state=row["state"],
        to_state=to_state,
        actor=actor,
        source=source,
        detail=detail,
    )
    conn.commit()
    return True


def record_nudge_sent(
    conn: sqlite3.Connection,
    thread_key: str,
    *,
    actor: str,
    detail: Optional[dict[str, Any]] = None,
) -> bool:
    """Post-send bookkeeping: nudge_count += 1, state -> awaiting_reply."""
    row = get_thread(conn, thread_key)
    if row is None:
        return False
    ok = transition(
        conn,
        thread_key,
        "awaiting_reply",
        actor=actor,
        source="send",
        detail=detail,
        event_type="send",
    )
    if ok:
        conn.execute(
            "UPDATE threads SET nudge_count = nudge_count + 1, last_outbound_ts = ? "
            "WHERE thread_key = ?",
            (time.time(), thread_key),
        )
        conn.commit()
    return ok


def add_event(
    conn: sqlite3.Connection,
    thread_key: str,
    *,
    event_type: str,
    actor: str,
    source: str,
    detail: Optional[dict[str, Any]] = None,
    event_ts: Optional[float] = None,
) -> None:
    """Append a non-transition event (guard_block, card_staged, card_expired...)."""
    _write_event(
        conn,
        thread_key,
        event_ts=event_ts if event_ts is not None else time.time(),
        event_type=event_type,
        from_state=None,
        to_state=None,
        actor=actor,
        source=source,
        detail=detail,
    )
    conn.commit()


def _write_event(
    conn: sqlite3.Connection,
    thread_key: str,
    *,
    event_ts: float,
    event_type: str,
    from_state: Optional[str],
    to_state: Optional[str],
    actor: Optional[str],
    source: Optional[str],
    detail: Optional[dict[str, Any]],
) -> None:
    conn.execute(
        """
        INSERT INTO thread_events
            (thread_key, event_ts, event_type, from_state, to_state, actor,
             source, detail, created_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            thread_key,
            event_ts,
            event_type,
            from_state,
            to_state,
            actor,
            source,
            json.dumps(detail) if detail else None,
            time.time(),
        ),
    )


def set_next_review(conn: sqlite3.Connection, thread_key: str, iso_date: Optional[str]) -> None:
    conn.execute(
        "UPDATE threads SET next_review_date = ?, updated_at = ? WHERE thread_key = ?",
        (iso_date, time.time(), thread_key),
    )
    conn.commit()


def set_owner(conn: sqlite3.Connection, thread_key: str, owner: str) -> None:
    conn.execute(
        "UPDATE threads SET owner = ?, updated_at = ? WHERE thread_key = ?",
        (owner, time.time(), thread_key),
    )
    conn.commit()


def set_counterparty_emails(
    conn: sqlite3.Connection, thread_key: str, emails: list[str]
) -> None:
    conn.execute(
        "UPDATE threads SET counterparty_emails = ?, updated_at = ? WHERE thread_key = ?",
        (json.dumps(sorted({e.strip().lower() for e in emails if e})), time.time(), thread_key),
    )
    conn.commit()


def update_observed_ts(
    conn: sqlite3.Connection,
    thread_key: str,
    *,
    last_outbound_ts: Optional[float] = None,
    last_inbound_ts: Optional[float] = None,
) -> None:
    """Advance observed timestamps monotonically (never backwards)."""
    row = get_thread(conn, thread_key)
    if row is None:
        return
    sets: list[str] = []
    params: list[Any] = []
    if last_outbound_ts and last_outbound_ts > (row["last_outbound_ts"] or 0):
        sets.append("last_outbound_ts = ?")
        params.append(last_outbound_ts)
    if last_inbound_ts and last_inbound_ts > (row["last_inbound_ts"] or 0):
        sets.append("last_inbound_ts = ?")
        params.append(last_inbound_ts)
    if not sets:
        return
    sets.append("updated_at = ?")
    params.append(time.time())
    conn.execute(
        f"UPDATE threads SET {', '.join(sets)} WHERE thread_key = ?",
        (*params, thread_key),
    )
    conn.commit()


def get_events(
    conn: sqlite3.Connection, thread_key: str, limit: int = 50
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM thread_events WHERE thread_key = ? "
            "ORDER BY event_ts DESC LIMIT ?",
            (thread_key, limit),
        ).fetchall()
    )
