"""Daily cadence sweep (R1 + R3): reads every tracked thread to the LAST
message (full-thread doctrine), advances states, computes nudge_due, and --
in stage mode only -- renders/stages nudges through the egress guard.

Prompt-injection posture (design lens 8): inbound email CONTENT can never
trigger a send, a transition to closed, or an escalation-downgrade. Ordinary
transitions here are driven purely by headers/metadata (who sent last, when).
The single place content is read is the deterministic escalation keyword
screen, whose only possible effect is escalation TO Harrison (the safe
direction), and which fails closed.

Modes:
  report (default) -- writes ledger state advancement + a log report. NO DMs,
                      no drafts, no cards. This is the B2 parallel-run posture
                      until the 5-clean-day verifier passes.
  stage            -- additionally stages nudges: Tier-1 card to the approver
                      when CORA_SEND_LIVE=tier1 and the playbook is tier 1,
                      else a Tier-0 reply draft in the mailbox's Drafts.
"""

from __future__ import annotations

import datetime as _dt
import logging
import sqlite3
import time
from email.utils import getaddresses
from typing import Any, Callable, Optional

from . import (
    email_egress_guard,
    ledger,
    nudge_templates,
    send_trust,
    sender,
    stash,
)

logger = logging.getLogger("cora.revops.sweep")

PLAYBOOK_ID = "silence_nudge"

# Addresses that count as "us" when deciding message direction. The v1 send
# mailbox is harrison@hjrglobal.com; his alias mailbox posts as f3energy too.
OWN_ALIASES = frozenset({"harrison@hjrglobal.com", "harrison@f3energy.com"})

_BOUNCE_SENDERS = ("mailer-daemon@", "postmaster@")


def _addr_of(raw: Optional[str]) -> str:
    if not raw:
        return ""
    pairs = getaddresses([raw])
    return (pairs[0][1] if pairs else raw).strip().lower()


def _addrs_of(raw_list: Any) -> list[str]:
    if not raw_list:
        return []
    if isinstance(raw_list, str):
        raw_list = [raw_list]
    out: list[str] = []
    for _, addr in getaddresses([", ".join(str(r) for r in raw_list)]):
        if addr and "@" in addr:
            out.append(addr.strip().lower())
    return out


def fetch_thread_messages(mailbox: str, gmail_thread_id: str) -> list[dict[str, Any]]:
    """Default fetcher: full-thread read via the gmail_reader DWD client."""
    from ..connectors import gmail_reader  # lazy import

    return gmail_reader.get_full_thread_text(mailbox, gmail_thread_id)


def sweep(
    conn: sqlite3.Connection,
    *,
    mode: str = "report",
    fetch: Callable[[str, str], list[dict[str, Any]]] = fetch_thread_messages,
    slack_client: Any = None,
    approver_dm_fallback: Optional[str] = None,
    now: Optional[float] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    ts_now = now if now is not None else time.time()
    today = _dt.date.fromtimestamp(ts_now).isoformat()
    cfg = send_trust.get_playbook(PLAYBOOK_ID)
    min_silence_days = cfg.min_silence_days if cfg else 7
    max_nudges = cfg.max_nudges if cfg else 2

    report: dict[str, Any] = {
        "mode": mode,
        "dry_run": dry_run,
        "checked": 0,
        "fetch_errors": 0,
        "advanced": {"replied": 0, "nudge_due": 0, "escalated": 0, "bounced": 0},
        "staged_cards": 0,
        "staged_drafts": 0,
        "guard_blocks": [],
        "surface_for_close": [],
        "expired_stashes": 0,
        "restored_nudge_due": 0,
    }

    if not dry_run:
        report["expired_stashes"] = stash.expire_stale(conn, ts_now)

    rows = [
        r
        for r in ledger.list_threads(conn)
        if r["state"] not in ledger.TERMINAL_STATES
    ]
    for row in rows:
        report["checked"] += 1
        try:
            messages = fetch(row["mailbox"], row["gmail_thread_id"])
        except Exception:  # noqa: BLE001 - fail soft per thread
            logger.exception("thread fetch failed for %s", row["thread_key"])
            report["fetch_errors"] += 1
            continue
        if not messages:
            report["fetch_errors"] += 1
            continue
        _advance_thread(
            conn,
            row,
            messages,
            report,
            ts_now=ts_now,
            today=today,
            min_silence_days=min_silence_days,
            max_nudges=max_nudges,
            dry_run=dry_run,
        )

    # nudge_staged threads whose stash died (expired/cancelled) re-enter nudge_due
    if not dry_run:
        for row in ledger.list_threads(conn, states=["nudge_staged"]):
            if stash.get_staged_for_thread(conn, row["thread_key"], ts_now) is None:
                if ledger.transition(
                    conn,
                    row["thread_key"],
                    "nudge_due",
                    actor="system",
                    source="sweep",
                    event_type="stash_lapsed",
                ):
                    report["restored_nudge_due"] += 1

    if mode == "stage" and not dry_run:
        _stage_nudges(
            conn,
            report,
            ts_now=ts_now,
            slack_client=slack_client,
            approver_dm_fallback=approver_dm_fallback,
            max_nudges=max_nudges,
        )
    return report


def _advance_thread(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    messages: list[dict[str, Any]],
    report: dict[str, Any],
    *,
    ts_now: float,
    today: str,
    min_silence_days: int,
    max_nudges: int,
    dry_run: bool,
) -> None:
    key = row["thread_key"]
    mailbox = (row["mailbox"] or "").lower()
    own = set(OWN_ALIASES) | {mailbox}
    last = messages[-1]
    last_from = _addr_of(last.get("sender"))
    # Clamp a future-dated Date header: a backdated/postdated reply must not be
    # able to fake silence or freshness (D-051 lens 4).
    last_ts = min(float(last.get("date_ts") or 0), ts_now)
    outbound = _is_outbound(last, own)

    # Observed participant emails (metadata) refresh the counterparty set.
    participants: set[str] = set()
    for msg in messages:
        participants.add(_addr_of(msg.get("sender")))
        participants.update(_addrs_of(msg.get("recipients")))
    counterparty_emails = sorted(a for a in participants if a and a not in own)
    if counterparty_emails and not dry_run:
        ledger.set_counterparty_emails(conn, key, counterparty_emails)

    if outbound:
        if not dry_run:
            ledger.update_observed_ts(conn, key, last_outbound_ts=last_ts)
        if row["state"] in ("hold", "escalated"):
            return
        days_silent = (ts_now - last_ts) / 86400 if last_ts else 0
        if days_silent < min_silence_days:
            # A fresh outbound (the nudge went out, or a human replied on the
            # thread themselves) supersedes any staged card/draft.
            if row["state"] in ("draft_staged", "nudge_staged", "nudge_due") and not dry_run:
                stash.cancel_staged_for_thread(conn, key)
                ledger.transition(
                    conn,
                    key,
                    "awaiting_reply",
                    actor="system",
                    source="sweep",
                    event_type="fresh_outbound",
                )
            return
        if (row["nudge_count"] or 0) >= max_nudges:
            report["surface_for_close"].append(
                {
                    "thread_key": key,
                    "counterparty": row["counterparty_name"],
                    "workstream": row["workstream"],
                    "days_silent": int(days_silent),
                    "nudge_count": row["nudge_count"],
                }
            )
            return
        if row["next_review_date"] and row["next_review_date"] > today:
            return
        # draft_staged deliberately NOT eligible: a still-unsent Tier-0 draft
        # must never be re-drafted daily (the B2 no-duplicate-nudge rule).
        if row["state"] in ("awaiting_reply", "replied") and not dry_run:
            if ledger.transition(
                conn,
                key,
                "nudge_due",
                actor="system",
                source="sweep",
                detail={"days_silent": int(days_silent)},
            ):
                report["advanced"]["nudge_due"] += 1
        return

    # ---- inbound last message ----
    if not dry_run:
        ledger.update_observed_ts(conn, key, last_inbound_ts=last_ts)
    if any(last_from.startswith(b) for b in _BOUNCE_SENDERS):
        # 'bounced' is a NON-terminal review state, deliberately: the From
        # prefix is attacker-shaped, so a forged mailer-daemon reply parks the
        # thread for human review rather than silently retiring it. It is
        # surfaced in the sweep report every run (D-051 lens 8).
        if not dry_run and ledger.transition(
            conn, key, "bounced", actor="system", source="sweep",
            detail={"observed_sender_prefix": last_from.split("@")[0][:20]},
        ):
            report["advanced"]["bounced"] += 1
        report.setdefault("bounced_for_review", []).append(
            {"thread_key": key, "counterparty": row["counterparty_name"]}
        )
        return

    # Deterministic escalation screen: content may only move a thread TOWARD
    # Harrison (fail-closed). Subject + a bounded slice of the body.
    screen_text = (last.get("subject") or "") + "\n" + (last.get("body_text") or "")[:2000]
    keyword = ledger.escalation_screen(screen_text)
    if keyword and row["state"] != "escalated":
        if not dry_run:
            stash.cancel_staged_for_thread(conn, key)
            if ledger.transition(
                conn,
                key,
                "escalated",
                actor="system",
                source="sweep",
                detail={"keyword": keyword},
            ):
                ledger.set_owner(conn, key, send_trust._DEFAULT_OWNER)
                report["advanced"]["escalated"] += 1
        return

    if row["state"] in ("hold", "escalated"):
        return
    if row["state"] != "replied" and not dry_run:
        stash.cancel_staged_for_thread(conn, key)
        if ledger.transition(conn, key, "replied", actor="system", source="sweep"):
            report["advanced"]["replied"] += 1


def _thread_subject(messages: Optional[list[dict[str, Any]]]) -> Optional[str]:
    for msg in reversed(messages or []):
        subj = (msg.get("subject") or "").strip()
        if subj:
            return subj
    return None


def _is_outbound(msg: dict[str, Any], own: set[str]) -> bool:
    """Direction from Gmail's own SENT label when present, From header only as
    a fallback for fixtures/readers that omit labels.

    A From header is attacker-spoofable; a Gmail label is not. This matters
    twice: silence detection, and (below) which message's recipient list seeds
    a nudge (D-051 lens 4/8).
    """
    # An EMPTY label list is still authoritative ("Gmail says not sent"), so
    # test for presence of the key, never truthiness -- `[] or fallback` would
    # hand a spoofed From header the decision.
    labels = msg.get("label_ids")
    if labels is None:
        labels = msg.get("labelIds")
    if labels is not None:
        return "SENT" in labels
    return _addr_of(msg.get("sender")) in own


def _nudge_recipients(
    conn: sqlite3.Connection, row: sqlite3.Row, messages: Optional[list[dict[str, Any]]]
) -> tuple[list[str], list[str]]:
    """To/Cc for the nudge: the non-own recipients of OUR last genuinely
    outbound message.

    There is deliberately NO fallback to the stored counterparty_emails set:
    that set is built from all thread headers, so an address injected by an
    inbound message could otherwise become a nudge recipient. No verifiable
    outbound recipient means no nudge (D-051 lens 4/8).
    """
    mailbox = (row["mailbox"] or "").lower()
    own = set(OWN_ALIASES) | {mailbox}
    for msg in reversed(messages or []):
        if _is_outbound(msg, own):
            to = [a for a in _addrs_of(msg.get("recipients")) if a not in own]
            if to:
                return [to[0]], to[1:]
    return [], []


def _stage_nudges(
    conn: sqlite3.Connection,
    report: dict[str, Any],
    *,
    ts_now: float,
    slack_client: Any,
    approver_dm_fallback: Optional[str],
    max_nudges: int,
) -> None:
    cfg = send_trust.get_playbook(PLAYBOOK_ID)
    for row in ledger.list_threads(conn, states=["nudge_due"]):
        key = row["thread_key"]
        if stash.get_staged_for_thread(conn, key, ts_now) is not None:
            continue  # idempotent across runs
        if (row["nudge_count"] or 0) >= max_nudges:
            continue
        days_silent = (
            int((ts_now - row["last_outbound_ts"]) / 86400)
            if row["last_outbound_ts"]
            else 0
        )
        body = nudge_templates.render_nudge(
            workstream=row["workstream"],
            counterparty_name=row["counterparty_name"],
            days_silent=days_silent,
        )
        if body is None:
            report["guard_blocks"].append(
                {"thread_key": key, "reason": "template unreadable; nothing staged"}
            )
            continue
        guard = email_egress_guard.check_email(
            body, workstream=row["workstream"], entity=row["entity"]
        )
        if not guard.ok:
            ledger.add_event(
                conn,
                key,
                event_type="guard_block",
                actor="system",
                source="sweep",
                detail={"guard": guard.to_dict(), "at": "stage"},
            )
            report["guard_blocks"].append(
                {"thread_key": key, "reason": guard.summary()}
            )
            _dm_owner_block(slack_client, row, guard, approver_dm_fallback)
            continue

        try:
            messages = fetch_thread_messages(row["mailbox"], row["gmail_thread_id"])
        except Exception:  # noqa: BLE001
            logger.exception("recipient derivation fetch failed for %s", key)
            messages = None
        to, cc = _nudge_recipients(conn, row, messages)
        if not to:
            report["guard_blocks"].append(
                {"thread_key": key, "reason": "no derivable recipient; nothing staged"}
            )
            continue

        tier = send_trust.effective_tier(PLAYBOOK_ID)
        if tier == 1 and cfg is not None:
            # Pin the subject and the thread's last-message id AT STAGE TIME so
            # the card shows exactly what will send and a later counterparty
            # reply cannot change it (D-051 lens 3/4).
            subject = _thread_subject(messages)
            sid = stash.create_stash(
                conn,
                thread_key=key,
                mailbox=row["mailbox"],
                playbook_id=PLAYBOOK_ID,
                gmail_thread_id=row["gmail_thread_id"],
                recipients=to,
                cc=cc,
                subject=subject,
                body_text=body,
                guard_results=guard.to_dict(),
                thread_last_msg_id=(messages[-1].get("message_id") if messages else None),
            )
            ledger.transition(
                conn,
                key,
                "nudge_staged",
                actor="system",
                source="sweep",
                event_type="card_staged",
                detail={"stash_id": sid},
            )
            _post_card(conn, slack_client, sid, cfg, approver_dm_fallback, guard)
            report["staged_cards"] += 1
        else:
            # Tier 0: a human sends. PHI already blocks above (class 6); any
            # surviving guard state here is clean, so just draft the reply.
            try:
                draft = sender.create_reply_draft(
                    row["mailbox"],
                    gmail_thread_id=row["gmail_thread_id"],
                    to=to,
                    cc=cc,
                    body_text=body,
                )
            except Exception:  # noqa: BLE001
                logger.exception("tier-0 reply draft failed for %s", key)
                report["guard_blocks"].append(
                    {"thread_key": key, "reason": "draft creation failed"}
                )
                continue
            ledger.transition(
                conn,
                key,
                "draft_staged",
                actor="system",
                source="sweep",
                event_type="draft_staged",
                detail={"draft_id": (draft or {}).get("id")},
            )
            report["staged_drafts"] += 1


def _post_card(
    conn: sqlite3.Connection,
    slack_client: Any,
    stash_id: str,
    cfg: send_trust.PlaybookConfig,
    approver_dm_fallback: Optional[str],
    guard: email_egress_guard.GuardResult,
) -> None:
    from . import cards

    if slack_client is None:
        return
    row = stash.get_stash(conn, stash_id)
    if row is None:
        return
    thread_row = ledger.get_thread(conn, row["thread_key"])
    fallback, blocks = cards.build_send_card(row, thread_row, guard)
    approver = cfg.approvers[0] if cfg.approvers else approver_dm_fallback
    if not approver:
        return
    try:
        resp = slack_client.chat_postMessage(
            channel=approver, text=fallback[:2900], blocks=blocks, unfurl_links=False
        )
        stash.set_card_ref(
            conn, stash_id, channel=resp["channel"], ts=resp["ts"]
        )
    except Exception:  # noqa: BLE001 - card post failure must not kill the sweep
        logger.exception("send-card post failed for stash %s", stash_id)


def _dm_owner_block(
    slack_client: Any,
    row: sqlite3.Row,
    guard: email_egress_guard.GuardResult,
    approver_dm_fallback: Optional[str],
) -> None:
    """Design rule: blocks on a Tier-1 stage = no card, owner DM with reason."""
    if slack_client is None:
        return
    owner = row["owner"] or send_trust.owner_for_workstream(row["workstream"]) or approver_dm_fallback
    if not owner:
        return
    try:
        slack_client.chat_postMessage(
            channel=owner,
            text=(
                f"Revops: the {row['workstream']} nudge for "
                f"{row['counterparty_name'] or row['thread_key']} was BLOCKED by the "
                f"email egress guard and no card was staged. {guard.summary()}"
            )[:2900],
        )
    except Exception:  # noqa: BLE001
        logger.exception("guard-block owner DM failed")
