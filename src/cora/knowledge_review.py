"""Harrison 👍/👎 knowledge-review approval flow.

When Cora surfaces a proposed memory update or reconciliation gap, it sends a
Slack DM to Harrison and logs a PENDING entry in data/cora-proposed-memory-updates.jsonl.

Harrison reacts with:
  +1  (👍) → APPROVED  — Cora executes the write action
  -1  (👎) → DISMISSED — Cora skips the action, logs dismissal
  speech_balloon (💬) → COMMENT_REQUESTED — Cora waits for follow-up text

The run_knowledge_review.py script (Mon-Fri 7am AZ) batches PENDING entries into
a single DM to Harrison and processes any previously-reacted entries.

Doctrine (LOCKED 2026-05-21):
  Harrison is sole authority. Cora NEVER auto-writes to decisions.md, Asana,
  or HubSpot without explicit Harrison 👍.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

log = logging.getLogger(__name__)

# ── File paths ───────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parents[2]
_REPLY_LOG_PATH = _REPO_ROOT / "data" / "cora-reply-log.jsonl"
_PROPOSED_UPDATES_PATH = _REPO_ROOT / "data" / "cora-proposed-memory-updates.jsonl"

_REPLY_LOCK = Lock()
_UPDATES_LOCK = Lock()

# ── Append-time idempotency (WS17-B item 2) ──────────────────────────────────
# propose_update is otherwise a blind append: a backfill / re-run that re-derives
# the same deterministic update_id (drive_fact:<id>, reconciliation gap_id,
# infocora-<ts>) would re-flood the ledger. We keep a process-cached set of every
# update_id already on disk, invalidated whenever the file's mtime changes (so a
# concurrent producer process's appends are picked up). Built lazily under the
# same _UPDATES_LOCK that guards the append.
_SEEN_IDS_CACHE: set[str] | None = None
_SEEN_IDS_KEY: tuple[str, float] | None = None  # (ledger path, mtime)


def _existing_update_ids() -> set[str]:
    """Return the set of update_ids already in the ledger (cache keyed on path+mtime).

    Keyed on BOTH the path and its mtime so the cache invalidates when the ledger
    is rewritten (a concurrent producer's append) AND when _PROPOSED_UPDATES_PATH
    is monkeypatched to a different file (tests). Must be called while holding
    _UPDATES_LOCK (it mutates the module cache)."""
    global _SEEN_IDS_CACHE, _SEEN_IDS_KEY
    try:
        mtime = _PROPOSED_UPDATES_PATH.stat().st_mtime
    except OSError:
        _SEEN_IDS_CACHE = set()
        _SEEN_IDS_KEY = None
        return _SEEN_IDS_CACHE
    key = (str(_PROPOSED_UPDATES_PATH), mtime)
    if _SEEN_IDS_CACHE is not None and _SEEN_IDS_KEY == key:
        return _SEEN_IDS_CACHE
    ids: set[str] = set()
    try:
        with _PROPOSED_UPDATES_PATH.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                uid = rec.get("update_id")
                if uid:
                    ids.add(uid)
    except OSError:
        ids = set()
    _SEEN_IDS_CACHE = ids
    _SEEN_IDS_KEY = key
    return ids

# ── Harrison's Slack user ID (from data/maps/slack-to-asana.yaml) ────────────
# Hardcoded for security — we never want to accidentally DM a non-Harrison user
# for approval-gate decisions.

HARRISON_SLACK_USER_ID = os.environ.get("HARRISON_SLACK_USER_ID", "U0B2RM2JYJ1")

# ── Reaction emoji names that map to approval actions ────────────────────────

APPROVE_REACTIONS = {"+1", "thumbsup", "white_check_mark", "heavy_check_mark"}
DISMISS_REACTIONS = {"-1", "thumbsdown", "x", "no_entry_sign"}
COMMENT_REACTIONS = {"speech_balloon", "thinking_face", "eyes"}

# ── Update types ─────────────────────────────────────────────────────────────

# These mirror the gap_type values used by reconciliation_engine.py
UPDATE_TYPE_ASANA_TASK    = "asana_task"       # Create Asana task
UPDATE_TYPE_HUBSPOT_NOTE  = "hubspot_note"     # Add HubSpot deal note
UPDATE_TYPE_DECISION      = "decision_capture" # Append to decisions.md
UPDATE_TYPE_TASK_CLOSE    = "task_close"       # Close open Asana task
UPDATE_TYPE_GENERIC       = "generic"          # Free-form action for Harrison


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Reply log ─────────────────────────────────────────────────────────────────


def log_reply_reaction(
    *,
    reactor_id: str,
    reaction: str,
    message_ts: str,
    channel_id: str,
    channel_name: str,
    event_type: str = "reaction_added",
    message_text: str = "",
) -> None:
    """Write one reaction event to data/cora-reply-log.jsonl.

    Only called from app.py's _handle_reaction() when Harrison reacts to a
    Cora-sent message. The message_ts is used to correlate back to the
    proposed-update entry that spawned the DM.
    """
    record = {
        "ts": _now_iso(),
        "reactor_id": reactor_id,
        "reaction": reaction,
        "action": classify_reaction(reaction),
        "message_ts": message_ts,
        "channel_id": channel_id,
        "channel_name": channel_name,
        "event_type": event_type,
        "message_text": message_text[:500] if message_text else "",
    }
    _REPLY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _REPLY_LOCK:
        with _REPLY_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    log.info(
        "knowledge_review: reaction logged reactor=%s reaction=%s action=%s msg_ts=%s",
        reactor_id, reaction, record["action"], message_ts,
    )


def classify_reaction(reaction: str) -> str:
    """Return 'APPROVED', 'DISMISSED', 'COMMENT_REQUESTED', or 'OTHER'."""
    base = (reaction or "").split("::", 1)[0].lower()
    if base in APPROVE_REACTIONS:
        return "APPROVED"
    if base in DISMISS_REACTIONS:
        return "DISMISSED"
    if base in COMMENT_REACTIONS:
        return "COMMENT_REQUESTED"
    return "OTHER"


# ── Proposed updates ──────────────────────────────────────────────────────────


def propose_update(
    *,
    update_id: str,
    update_type: str,
    description: str,
    payload: dict[str, Any],
    source_evidence: str = "",
    confidence: str = "HIGH",
    dm_message_ts: str = "",
    dm_channel_id: str = "",
) -> bool:
    """Record a proposed update in PENDING state. Returns True if appended,
    False if skipped as a duplicate (an entry with this update_id already exists).

    Called by run_knowledge_review.py after sending Harrison a DM. The
    dm_message_ts is the Slack message timestamp of the DM — used to correlate
    Harrison's reaction back to this entry.

    Idempotent (WS17-B item 2): a re-run/backfill that re-derives an existing
    deterministic update_id is a no-op, so producers can't re-flood the queue.

    Entry schema:
    {
      "update_id": str,          unique ID (typically reconciliation gap ID or uuid4)
      "update_type": str,        see UPDATE_TYPE_* constants
      "description": str,        human-readable summary for Harrison's DM
      "payload": dict,           structured data the executor needs to act
      "source_evidence": str,    excerpt from KB chunk that triggered this
      "confidence": str,         "HIGH" | "MED" | "LOW"
      "state": str,              "PENDING" | "APPROVED" | "DISMISSED" | "COMMENT_REQUESTED"
      "proposed_at": str,        ISO 8601
      "resolved_at": str|null,
      "dm_message_ts": str,      Slack message ts of the Harrison DM (for reaction correlation)
      "dm_channel_id": str,      Slack channel (DM channel ID) where the DM was sent
    }
    """
    entry = {
        "update_id": update_id,
        "update_type": update_type,
        "description": description,
        "payload": payload,
        "source_evidence": source_evidence[:1000] if source_evidence else "",
        "confidence": confidence,
        "state": "PENDING",
        "proposed_at": _now_iso(),
        "resolved_at": None,
        "dm_message_ts": dm_message_ts,
        "dm_channel_id": dm_channel_id,
    }
    _PROPOSED_UPDATES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _UPDATES_LOCK:
        if update_id in _existing_update_ids():
            log.info("knowledge_review: skip duplicate propose update_id=%s type=%s",
                     update_id, update_type)
            return False
        with _PROPOSED_UPDATES_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        # Keep the cache valid without a full re-scan: record the new id and the
        # post-write key so the next caller in this process doesn't rebuild.
        global _SEEN_IDS_CACHE, _SEEN_IDS_KEY
        if _SEEN_IDS_CACHE is not None:
            _SEEN_IDS_CACHE.add(update_id)
        try:
            _SEEN_IDS_KEY = (str(_PROPOSED_UPDATES_PATH),
                             _PROPOSED_UPDATES_PATH.stat().st_mtime)
        except OSError:
            _SEEN_IDS_KEY = None
    log.info(
        "knowledge_review: proposed update_id=%s type=%s confidence=%s",
        update_id, update_type, confidence,
    )
    return True


def load_proposed_updates() -> list[dict[str, Any]]:
    """Return all proposed updates, newest first."""
    if not _PROPOSED_UPDATES_PATH.exists():
        return []
    entries: list[dict[str, Any]] = []
    with _PROPOSED_UPDATES_PATH.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    log.warning("knowledge_review: skipped malformed proposed-update line")
    return list(reversed(entries))


def load_reply_log() -> list[dict[str, Any]]:
    """Return all reply-log entries, newest first."""
    if not _REPLY_LOG_PATH.exists():
        return []
    entries: list[dict[str, Any]] = []
    with _REPLY_LOG_PATH.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    log.warning("knowledge_review: skipped malformed reply-log line")
    return list(reversed(entries))


def resolve_update(update_id: str, new_state: str, reason: str = "") -> bool:
    """Update state of an entry in cora-proposed-memory-updates.jsonl.

    Rewrites the full file atomically. Returns True if the entry was found and
    updated, False if not found (already resolved or never proposed).

    new_state must be one of: 'APPROVED', 'DISMISSED', 'COMMENT_REQUESTED'.
    reason (optional) is recorded as resolved_reason for the audit trail -- used
    to distinguish a Harrison 👍 from an auto-approve / auto-expire (Phase 2.4).
    """
    if not _PROPOSED_UPDATES_PATH.exists():
        return False

    with _UPDATES_LOCK:
        entries: list[dict[str, Any]] = []
        found = False
        with _PROPOSED_UPDATES_PATH.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("update_id") == update_id and entry.get("state") == "PENDING":
                    entry["state"] = new_state
                    entry["resolved_at"] = _now_iso()
                    if reason:
                        entry["resolved_reason"] = reason
                    found = True
                entries.append(entry)

        if found:
            tmp_path = _PROPOSED_UPDATES_PATH.with_suffix(".tmp")
            with tmp_path.open("w", encoding="utf-8") as fh:
                for entry in entries:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            tmp_path.replace(_PROPOSED_UPDATES_PATH)

    return found


def get_pending_updates() -> list[dict[str, Any]]:
    """Return only PENDING updates, oldest first (for DM batching)."""
    all_updates = load_proposed_updates()
    pending = [u for u in all_updates if u.get("state") == "PENDING"]
    return list(reversed(pending))  # oldest first


def correlate_reactions_to_updates() -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Match reply-log reactions to proposed-update entries by DM message_ts.

    Returns list of (update_entry, reaction_entry) pairs for entries where
    Harrison has reacted and the update is still PENDING.
    Only includes APPROVED or DISMISSED reactions (not OTHER/COMMENT_REQUESTED
    for now — those require follow-up handling).
    """
    updates = load_proposed_updates()
    reactions = load_reply_log()

    # Build a map: dm_message_ts -> first actionable Harrison reaction
    ts_to_reaction: dict[str, dict[str, Any]] = {}
    for r in reactions:
        if r.get("reactor_id") != HARRISON_SLACK_USER_ID:
            continue
        if r.get("event_type") != "reaction_added":
            continue
        action = r.get("action", "OTHER")
        if action not in ("APPROVED", "DISMISSED"):
            continue
        msg_ts = r.get("message_ts", "")
        if msg_ts and msg_ts not in ts_to_reaction:
            ts_to_reaction[msg_ts] = r

    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for update in updates:
        if update.get("state") != "PENDING":
            continue
        dm_ts = update.get("dm_message_ts", "")
        if dm_ts and dm_ts in ts_to_reaction:
            pairs.append((update, ts_to_reaction[dm_ts]))

    return pairs


# ── DM formatting ─────────────────────────────────────────────────────────────

_TYPE_LABEL: dict[str, str] = {
    UPDATE_TYPE_ASANA_TASK:   "Asana task",
    UPDATE_TYPE_HUBSPOT_NOTE: "HubSpot note",
    UPDATE_TYPE_DECISION:     "Decision capture",
    UPDATE_TYPE_TASK_CLOSE:   "Close task",
    UPDATE_TYPE_GENERIC:      "Action",
}


def format_single_item_dm(update: dict[str, Any]) -> str:
    """Format one pending update as a standalone Slack DM message.

    Each message gets its own 👍/👎 reaction so Harrison approves or dismisses
    items individually rather than the whole batch at once.
    """
    conf = update.get("confidence", "?")
    utype = update.get("update_type", "generic")
    desc = update.get("description", "(no description)")
    evidence = update.get("source_evidence", "")
    conf_emoji = {"HIGH": "🔴", "MED": "🟡", "LOW": "⚪"}.get(conf, "⚪")
    type_label = _TYPE_LABEL.get(utype, utype)

    lines = [f"*[{type_label}]* {conf_emoji} `{conf}`\n{desc}"]
    if evidence:
        snippet = evidence[:300].replace("\n", " ")
        lines.append(f"_Source: {snippet}_")
    lines.append("\n👍 Approve · 👎 Dismiss")
    return "\n".join(lines)


def format_pending_dm(updates: list[dict[str, Any]]) -> str:
    """Format a batch of pending updates into a single Slack DM (legacy/fallback).

    Prefer send_individual_dms() which sends one message per item so each
    can be approved or dismissed independently.
    """
    if not updates:
        return ""

    lines = [f"*Cora knowledge review* — {len(updates)} pending update(s) for your approval:\n"]
    for i, u in enumerate(updates, 1):
        conf = u.get("confidence", "?")
        utype = u.get("update_type", "generic")
        desc = u.get("description", "(no description)")
        evidence = u.get("source_evidence", "")
        conf_emoji = {"HIGH": "🔴", "MED": "🟡", "LOW": "⚪"}.get(conf, "⚪")
        type_label = _TYPE_LABEL.get(utype, utype)

        lines.append(f"*{i}. [{type_label}]* {conf_emoji} `{conf}` — {desc}")
        if evidence:
            snippet = evidence[:200].replace("\n", " ")
            lines.append(f"   _Source: {snippet}…_")

    lines.append(
        "\n👍 React to approve all · 👎 React to dismiss all · 💬 React + reply to comment on specific items"
    )
    return "\n".join(lines)


def _build_slack_client(token: str):
    """Build a slack_sdk WebClient. Separated for testability."""
    from slack_sdk import WebClient as _WebClient
    return _WebClient(token=token)


def send_dm_to_harrison(
    message: str,
    slack_bot_token: str,
    _client_factory=None,
) -> str | None:
    """Send a Slack DM to Harrison. Returns message_ts on success, None on failure.

    Uses the bot token directly (not the Cora tool-dispatch layer) since this
    runs from a scheduled script outside the Bolt app context.

    _client_factory is an injection point for tests — pass a callable that returns
    a mock Slack client instead of building a real one.
    """
    if not slack_bot_token:
        log.error("knowledge_review: SLACK_BOT_TOKEN not set — cannot send DM")
        return None

    try:
        if _client_factory is not None:
            client = _client_factory()
        else:
            client = _build_slack_client(slack_bot_token)
        open_resp = client.conversations_open(users=[HARRISON_SLACK_USER_ID])
        dm_channel = open_resp["channel"]["id"]
        send_resp = client.chat_postMessage(
            channel=dm_channel,
            text=message,
            unfurl_links=False,
            unfurl_media=False,
        )
        msg_ts = send_resp.get("ts", "")
        log.info("knowledge_review: DM sent to Harrison ts=%s chars=%d", msg_ts, len(message))
        return msg_ts
    except Exception as exc:
        log.error("knowledge_review: DM send failed: %s", exc)
        return None


def send_individual_dms(
    updates: list[dict[str, Any]],
    slack_bot_token: str,
    _client_factory=None,
) -> dict[str, str]:
    """Send one DM per pending update. Returns {update_id: message_ts}.

    Skips updates that already have dm_message_ts set (already delivered).
    Adds a 0.5s delay between messages to stay within Slack rate limits.
    """
    import time as _time

    unsent = [u for u in updates if not u.get("dm_message_ts")]
    if not unsent:
        return {}

    if not slack_bot_token:
        log.error("knowledge_review: SLACK_BOT_TOKEN not set")
        return {}

    try:
        client = _client_factory() if _client_factory else _build_slack_client(slack_bot_token)
        open_resp = client.conversations_open(users=[HARRISON_SLACK_USER_ID])
        dm_channel = open_resp["channel"]["id"]
    except Exception as exc:
        log.error("knowledge_review: could not open DM channel: %s", exc)
        return {}

    results: dict[str, str] = {}
    for update in unsent:
        text = format_single_item_dm(update)
        try:
            resp = client.chat_postMessage(
                channel=dm_channel,
                text=text,
                unfurl_links=False,
                unfurl_media=False,
            )
            ts = resp.get("ts", "")
            results[update["update_id"]] = ts
            log.info("knowledge_review: DM sent for %s ts=%s", update["update_id"][:8], ts)
        except Exception as exc:
            log.warning("knowledge_review: DM failed for %s: %s", update["update_id"][:8], exc)
        _time.sleep(0.5)

    return results
