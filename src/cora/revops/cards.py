"""Approve-card builder + tap processor for Tier-1 sends (R2, D-081 pattern).

The card shows the EXACT bytes that will send (the stash body, fenced); the
stash id rides in every button's value (thread-anchored identity, immune to
the (user, channel) wrong-pending residual). All correctness lives here, not
in the app.py handlers: Harrison gate, single-shot semantics, guard re-runs.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import re
import sqlite3
import threading
import time
from typing import Any, Optional

from . import email_egress_guard, ledger, send_trust, sender, stash

logger = logging.getLogger("cora.revops.cards")

ACTION_SEND = "revops_send_approve"
ACTION_EDIT = "revops_send_edit"
ACTION_SKIP = "revops_send_skip"
ACTION_CLOSE = "revops_send_close"
VIEW_EDIT_SUBMIT = "revops_send_edit_submit"

_ONE_TAP_LOCK = threading.Lock()

_SKIP_REVIEW_PUSH_DAYS = 7
_MAX_CARD_BODY_CHARS = 2600


def _fence_safe(text: str) -> str:
    """Neutralize backtick runs so a body can never break out of the ``` fence
    and render as something other than the bytes that will send (D-051 lens 3).
    Display-only: the stash bytes are untouched."""
    return re.sub(r"`{3,}", lambda m: "`​" * len(m.group(0)), text or "")


def _days_silent(thread_row: Optional[sqlite3.Row], now: Optional[float] = None) -> int:
    if thread_row is None or not thread_row["last_outbound_ts"]:
        return 0
    ts = now if now is not None else time.time()
    return max(0, int((ts - thread_row["last_outbound_ts"]) / 86400))


def build_send_card(
    stash_row: sqlite3.Row,
    thread_row: Optional[sqlite3.Row],
    guard_result: email_egress_guard.GuardResult,
) -> tuple[str, list[dict[str, Any]]]:
    """Returns (fallback_text, blocks)."""
    sid = stash_row["stash_id"]
    counterparty = (thread_row["counterparty_name"] if thread_row else None) or "unknown counterparty"
    workstream = (thread_row["workstream"] if thread_row else None) or "?"
    entity = (thread_row["entity"] if thread_row else None) or "?"
    nudge_count = (thread_row["nudge_count"] if thread_row else 0) or 0
    days = _days_silent(thread_row)
    recipients = ", ".join(json.loads(stash_row["recipients"] or "[]"))
    cc = ", ".join(json.loads(stash_row["cc"] or "[]"))
    expires = _dt.datetime.fromtimestamp(stash_row["expires_ts"]).strftime("%a %m/%d %H:%M")
    note = (thread_row["notes"] if thread_row else None) or ""

    body = stash_row["body_text"] or ""
    display_body = _fence_safe(body)
    truncated = ""
    if len(display_body) > _MAX_CARD_BODY_CHARS:
        display_body = display_body[:_MAX_CARD_BODY_CHARS]
        truncated = "\n(display truncated; the send is the full stashed bytes, hash-pinned)"
    if display_body != body:
        truncated += (
            "\n(display-only: backtick runs shown escaped so the preview cannot "
            "break out of the code block; the SENT bytes are unmodified)"
        )

    subject = stash_row["subject"] or "(reply subject from the thread)"
    header = (
        f"*Silence nudge ready: {counterparty}*\n"
        f"{workstream} | {entity} | {days}d silent | nudge {nudge_count + 1}\n"
        f"From: {stash_row['mailbox']}\n"
        f"To: {recipients}" + (f"\nCc: {cc}" if cc else "") + f"\nSubject: Re: {subject}"
    )
    if note:
        header += f"\nThread note: {note[:200]}"

    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header[:2900]}},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*Message (sends byte-exact):*\n```" + display_body + "```" + truncated,
            },
        },
    ]
    if guard_result.warns:
        warn_text = "\n".join(":warning: " + w["reason"] for w in guard_result.warns)
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": warn_text[:2900]}],
            }
        )
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"Typed fallback: reply `SEND {sid}` | expires {expires} (48h)",
                }
            ],
        }
    )
    blocks.append(
        {
            "type": "actions",
            "block_id": f"revops_send_{sid}"[:255],
            "elements": [
                {
                    "type": "button",
                    "action_id": ACTION_SEND,
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "✅ Send"},
                    "value": sid,
                },
                {
                    "type": "button",
                    "action_id": ACTION_EDIT,
                    "text": {"type": "plain_text", "text": "✏️ Edit"},
                    "value": sid,
                },
                {
                    "type": "button",
                    "action_id": ACTION_SKIP,
                    "text": {"type": "plain_text", "text": "\U0001f5d1️ Skip"},
                    "value": sid,
                },
                {
                    "type": "button",
                    "action_id": ACTION_CLOSE,
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "\U0001f6ab Close thread"},
                    "value": sid,
                },
            ],
        }
    )
    fallback = f"Silence nudge ready for {counterparty} ({workstream}, {days}d silent)"
    return fallback, blocks


def edit_modal_view(
    stash_id: str, dm_channel: str, dm_ts: str, body_text: str
) -> dict[str, Any]:
    """Modal prefilled with the staged body. Submit -> restage_with_edit
    (a NEW stash + NEW card; editing never sends)."""
    return {
        "type": "modal",
        "callback_id": VIEW_EDIT_SUBMIT,
        "private_metadata": json.dumps(
            {"stash_id": stash_id, "dm_channel": dm_channel, "dm_ts": dm_ts}
        ),
        "title": {"type": "plain_text", "text": "Edit nudge"},
        "submit": {"type": "plain_text", "text": "Stage new card"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "revops_edit_block",
                "label": {"type": "plain_text", "text": "Message body (re-guarded, restaged as a new card; nothing sends now)"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "revops_edit_input",
                    "multiline": True,
                    "initial_value": body_text or "",
                },
            }
        ],
    }


def process_send_action(
    stash_id: str,
    actor_id: str,
    *,
    action: str,
    conn: Optional[sqlite3.Connection] = None,
) -> tuple[str, str]:
    """Handle ✅ / 🗑️ / 🚫 (and typed SEND). Returns (outcome, message).

    Outcomes: sent | skipped | closed | not_authorized | not_found |
    already_resolved | expired | plus every sender.send_stashed outcome.
    """
    own = conn is None
    if own:
        conn = ledger.connect()
    try:
        with _ONE_TAP_LOCK:
            return _process_inner(conn, stash_id, actor_id, action)
    except Exception:  # noqa: BLE001 - a tap must never crash the bot
        logger.exception("process_send_action failed for %s", stash_id)
        return ("error", "Something went wrong handling that tap; nothing was sent.")
    finally:
        if own:
            conn.close()


def _process_inner(
    conn: sqlite3.Connection, stash_id: str, actor_id: str, action: str
) -> tuple[str, str]:
    row = stash.get_stash(conn, stash_id)
    if row is None:
        return ("not_found", "No such send card; nothing was done.")
    if not send_trust.is_approver(row["playbook_id"], actor_id):
        return ("not_authorized", "Only Harrison can act on send cards in v1.")

    if action == "send":
        return sender.send_stashed(stash_id, approver_id=actor_id, conn=conn)

    if action == "skip":
        if not stash.cancel_stash(conn, stash_id):
            return ("already_resolved", f"This card is already {row['status']}.")
        next_review = (
            _dt.date.today() + _dt.timedelta(days=_SKIP_REVIEW_PUSH_DAYS)
        ).isoformat()
        ledger.transition(
            conn,
            row["thread_key"],
            "awaiting_reply",
            actor=actor_id,
            source="owner",
            event_type="nudge_skipped",
            detail={"stash_id": stash_id, "next_review_date": next_review},
        )
        ledger.set_next_review(conn, row["thread_key"], next_review)
        return (
            "skipped",
            f"Skipped; nothing was sent. The thread re-enters the nudge queue after {next_review}.",
        )

    if action == "close":
        stash.cancel_stash(conn, stash_id)
        moved = ledger.transition(
            conn,
            row["thread_key"],
            "closed_courtesy",
            actor=actor_id,
            source="owner",
            event_type="thread_closed",
            detail={"stash_id": stash_id, "via": "send_card"},
        )
        if moved:
            return ("closed", "Thread closed (courtesy). Nothing was sent.")
        return ("already_resolved", "Thread was already closed; nothing was sent.")

    return ("error", f"Unknown action {action!r}; nothing was done.")


def restage_with_edit(
    stash_id: str,
    actor_id: str,
    edited_text: str,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> tuple[str, str, Optional[str]]:
    """✏️ Edit path: any edit produces a NEW stash + NEW card (F-23 doctrine;
    the old card can never fire the old bytes again).

    Returns (outcome, message, new_stash_id | None).
    """
    own = conn is None
    if own:
        conn = ledger.connect()
    try:
        with _ONE_TAP_LOCK:
            row = stash.get_stash(conn, stash_id)
            if row is None:
                return ("not_found", "No such send card.", None)
            if not send_trust.is_approver(row["playbook_id"], actor_id):
                return ("not_authorized", "Only Harrison can edit send cards in v1.", None)
            if row["status"] != "staged" or stash.is_expired(row):
                return (
                    "already_resolved",
                    f"This card is already {row['status']} or expired; start from a fresh sweep.",
                    None,
                )
            edited_text = (edited_text or "").strip()
            if not edited_text:
                return ("empty", "Edited message is empty; kept the original card.", None)

            thread_row = ledger.get_thread(conn, row["thread_key"])
            guard = email_egress_guard.check_email(
                edited_text,
                workstream=thread_row["workstream"] if thread_row else None,
                entity=thread_row["entity"] if thread_row else None,
            )
            if not guard.ok:
                return (
                    "guard_blocked",
                    "Edit rejected by the egress guard: " + guard.summary(),
                    None,
                )
            if not stash.cancel_stash(conn, stash_id):
                return ("already_resolved", "Card was handled while you were editing.", None)
            new_id = stash.create_stash(
                conn,
                thread_key=row["thread_key"],
                mailbox=row["mailbox"],
                playbook_id=row["playbook_id"],
                gmail_thread_id=row["gmail_thread_id"],
                recipients=json.loads(row["recipients"] or "[]"),
                cc=json.loads(row["cc"] or "[]"),
                subject=row["subject"],
                body_text=edited_text + ("\n" if not edited_text.endswith("\n") else ""),
                guard_results=guard.to_dict(),
            )
            ledger.add_event(
                conn,
                row["thread_key"],
                event_type="card_restaged",
                actor=actor_id,
                source="owner",
                detail={"old_stash_id": stash_id, "new_stash_id": new_id, "edited": True},
            )
            return ("restaged", "Edited message staged as a new card.", new_id)
    except Exception:  # noqa: BLE001
        logger.exception("restage_with_edit failed for %s", stash_id)
        return ("error", "Edit failed; the original card is unchanged.", None)
    finally:
        if own:
            conn.close()
