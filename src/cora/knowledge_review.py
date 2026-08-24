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
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any

log = logging.getLogger(__name__)

# ── File paths ───────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parents[2]
_REPLY_LOG_PATH = _REPO_ROOT / "data" / "cora-reply-log.jsonl"
_PROPOSED_UPDATES_PATH = _REPO_ROOT / "data" / "cora-proposed-memory-updates.jsonl"
# Cold storage for rotated resolved/dismissed rows (WS17-B item 8). Keeps the live
# ledger small (PENDING + recently-resolved) so the per-op read+rewrite stays cheap.
_ARCHIVE_PATH = _REPO_ROOT / "data" / "cora-proposed-memory-updates.archive.jsonl"

_REPLY_LOCK = Lock()
_UPDATES_LOCK = Lock()
# Serializes one-tap button actions within the bot process so two concurrent
# taps (Socket Mode dispatches on a thread pool) cannot both pass the
# state==PENDING check and both apply -- which would duplicate an append-only
# efficiency finding or last-writer-wins clobber a same-entity known-answer .md
# rewrite (D-051 finding). Lock ordering is always _ONE_TAP_LOCK -> _UPDATES_LOCK
# (resolve_update takes the latter); nothing acquires them in the reverse order.
_ONE_TAP_LOCK = Lock()

# ── Append-time idempotency (WS17-B item 2) ──────────────────────────────────
# propose_update is otherwise a blind append: a backfill / re-run that re-derives
# the same deterministic update_id (drive_fact:<id>, reconciliation gap_id,
# infocora-<ts>) would re-flood the ledger. We keep a process-cached set of every
# update_id already on disk, invalidated whenever the file's mtime changes (so a
# concurrent producer process's appends are picked up). Built lazily under the
# same _UPDATES_LOCK that guards the append.
_SEEN_IDS_CACHE: set[str] | None = None
_SEEN_IDS_KEY: tuple[str, float] | None = None  # (ledger path, mtime)
_ARCHIVE_IDS_CACHE: set[str] | None = None
_ARCHIVE_IDS_KEY: tuple[str, float] | None = None  # (archive path, mtime)


def _ids_in_file(path: Path) -> set[str]:
    """update_ids in one ledger file. A MISSING file is an empty set (normal
    for a fresh repo); any OTHER read error RAISES -- after the WS-4 expiry +
    rotation, the archive is the SOLE idempotency barrier for thousands of
    drive-fact ids, and failing open (empty set) on a transient read error
    would let a producer silently re-propose the whole population. A loud
    failure aborts that propose and retries next run (watermark held)."""
    ids: set[str] = set()
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                uid = rec.get("update_id")
                if uid:
                    ids.add(uid)
    except FileNotFoundError:
        pass
    return ids


def _live_update_ids() -> set[str]:
    """update_ids in the LIVE ledger (cache keyed on path+mtime). Holds _UPDATES_LOCK."""
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
    _SEEN_IDS_CACHE = _ids_in_file(_PROPOSED_UPDATES_PATH)
    _SEEN_IDS_KEY = key
    return _SEEN_IDS_CACHE


def _archive_update_ids() -> set[str]:
    """update_ids in the ARCHIVE (cache keyed on path+mtime). Holds _UPDATES_LOCK."""
    global _ARCHIVE_IDS_CACHE, _ARCHIVE_IDS_KEY
    try:
        mtime = _ARCHIVE_PATH.stat().st_mtime
    except OSError:
        _ARCHIVE_IDS_CACHE = set()
        _ARCHIVE_IDS_KEY = None
        return _ARCHIVE_IDS_CACHE
    key = (str(_ARCHIVE_PATH), mtime)
    if _ARCHIVE_IDS_CACHE is not None and _ARCHIVE_IDS_KEY == key:
        return _ARCHIVE_IDS_CACHE
    _ARCHIVE_IDS_CACHE = _ids_in_file(_ARCHIVE_PATH)
    _ARCHIVE_IDS_KEY = key
    return _ARCHIVE_IDS_CACHE


def _existing_update_ids() -> set[str]:
    """All known update_ids = LIVE ledger ∪ ARCHIVE. The archive union is what keeps
    idempotency correct AFTER rotation — a resolved item moved to cold storage must
    still never be re-proposed (e.g. a re-detected reconciliation gap Harrison already
    dismissed). Must be called while holding _UPDATES_LOCK."""
    return _live_update_ids() | _archive_update_ids()


def _write_entries_atomic(
    path: Path,
    entries: list[dict[str, Any]],
    raw_lines: list[str] | None = None,
) -> None:
    """Rewrite a JSONL ledger atomically (tmp + replace) — no partial-write window.

    raw_lines are unparseable lines preserved VERBATIM (written first) so a rewrite
    never silently drops malformed data (adversarial review MEDIUM)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for line in (raw_lines or []):
            fh.write(line + "\n")
        for entry in entries:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    tmp.replace(path)


def rotate_resolved(max_age_days: int = 3, now: datetime | None = None) -> int:
    """Move resolved (APPROVED/DISMISSED) rows older than max_age_days to the
    archive, keeping the live ledger to PENDING + recently-resolved (WS17-B item 8).
    Returns the number of rows rotated. Crash-safe ORDER: append to the archive
    FIRST, then rewrite the live file — a crash between leaves rows in BOTH (the
    id-union dedups; the next rotation re-archives harmlessly) rather than losing
    them. Never raises into the caller is the caller's job; this raises on I/O."""
    now = now or datetime.now(timezone.utc)
    cutoff = now.timestamp() - max_age_days * 86400
    with _UPDATES_LOCK:
        if not _PROPOSED_UPDATES_PATH.exists():
            return 0
        keep: list[dict[str, Any]] = []
        archive: list[dict[str, Any]] = []
        with _PROPOSED_UPDATES_PATH.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    keep.append({"__malformed__": line})  # never drop unparseable data
                    continue
                state = rec.get("state")
                resolved_at = rec.get("resolved_at")
                old_enough = False
                if state in ("APPROVED", "DISMISSED") and resolved_at:
                    try:
                        ra = datetime.fromisoformat(str(resolved_at).replace("Z", "+00:00"))
                        if ra.tzinfo is None:
                            ra = ra.replace(tzinfo=timezone.utc)
                        old_enough = ra.timestamp() < cutoff
                    except Exception:
                        old_enough = False  # unparseable -> keep (fail-safe)
                (archive if old_enough else keep).append(rec)

        if not archive:
            return 0
        # Append to archive FIRST (crash-safe), then shrink the live file.
        _ARCHIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _ARCHIVE_PATH.open("a", encoding="utf-8") as fh:
            for rec in archive:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        # Rewrite the live file (malformed lines preserved verbatim).
        keep_records = [k for k in keep if "__malformed__" not in k]
        malformed = [k["__malformed__"] for k in keep if "__malformed__" in k]
        _write_entries_atomic(_PROPOSED_UPDATES_PATH, keep_records, raw_lines=malformed)
        # Invalidate caches (both files changed).
        global _SEEN_IDS_CACHE, _SEEN_IDS_KEY, _ARCHIVE_IDS_CACHE, _ARCHIVE_IDS_KEY
        _SEEN_IDS_CACHE = None
        _SEEN_IDS_KEY = None
        _ARCHIVE_IDS_CACHE = None
        _ARCHIVE_IDS_KEY = None
        return len(archive)

# ── Harrison's Slack user ID (from data/maps/slack-to-asana.yaml) ────────────
# Hardcoded for security — we never want to accidentally DM a non-Harrison user
# for approval-gate decisions.

HARRISON_SLACK_USER_ID = os.environ.get("HARRISON_SLACK_USER_ID", "U0B2RM2JYJ1")

# ── Reaction emoji names that map to approval actions ────────────────────────

APPROVE_REACTIONS = {"+1", "thumbsup", "white_check_mark", "heavy_check_mark"}
DISMISS_REACTIONS = {"-1", "thumbsdown", "x", "no_entry_sign"}
COMMENT_REACTIONS = {"speech_balloon", "thinking_face", "eyes"}

# ── One-tap approve (2026-07-09 write-path) ──────────────────────────────────
# Block Kit action_ids on the knowledge-review DM. The bot's Socket-Mode
# interactivity handler (app.py) processes a click IMMEDIATELY -- keeping the
# Harrison-only human gate (D-011 intact; friction-removal, NOT auto-approve).
# The emoji 👍/👎 path is unchanged as the belt-and-braces (still processed at
# the next scheduled run), so nothing regresses if interactivity is disabled.
ACTION_APPROVE = "knowledge_approve"
ACTION_DISMISS = "knowledge_dismiss"

# Decisions lane (Fork 4, 2026-08-01): decision_capture cards carry their OWN
# action ids so the tap routes to process_decision_tap (inbox filing), never to
# process_one_tap_action / apply_knowledge_update -- keeping the type
# structurally unreachable from the graduated-trust autowrite path, which
# reuses apply_knowledge_update.
ACTION_DECISION_ACCEPT = "decision_accept"
ACTION_DECISION_DISMISS = "decision_dismiss"

# ── Update types ─────────────────────────────────────────────────────────────

# These mirror the gap_type values used by reconciliation_engine.py
UPDATE_TYPE_ASANA_TASK    = "asana_task"       # Create Asana task
UPDATE_TYPE_HUBSPOT_NOTE  = "hubspot_note"     # Add HubSpot deal note
UPDATE_TYPE_DECISION      = "decision_capture" # Harrison decision card -> NON-canon inbox
UPDATE_TYPE_TASK_CLOSE    = "task_close"       # Close open Asana task
UPDATE_TYPE_GENERIC       = "generic"          # Free-form action for Harrison
UPDATE_TYPE_LEXICON       = "lexicon"          # Company-lexicon term -> file of record


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Knowledge vs operational classification (single source of truth) ──────────
# Mirrors run_knowledge_review._is_knowledge_item so the drain's knowledge/
# operational split and propose_update's TTL decision can NEVER drift (the
# drive_extractor 'person' fact is a bare 'generic' with no info-for-cora source
# -> OPERATIONAL, and must be classified the same in both places). KNOWLEDGE =
# the two knowledge update_types OR a generic contributed via #info-for-cora (a
# human-fed fact). Everything else is an OPERATIONAL nudge routed to owners.
_KNOWLEDGE_UPDATE_TYPES = frozenset({"known_answer", "efficiency", "lexicon"})


def is_knowledge_update(update_type: str | None, payload: dict | None) -> bool:
    """True if this update belongs in Harrison's knowledge queue (never TTL-
    expired), vs an operational nudge routed to owners (TTL-bounded)."""
    if update_type in _KNOWLEDGE_UPDATE_TYPES:
        return True
    if update_type == UPDATE_TYPE_GENERIC and (payload or {}).get("source") == "info-for-cora":
        return True
    return False


# ── Operational TTL-at-creation (flywheel-gate-fixes Slice 2, 2026-07-24) ─────
# An operational nudge proposal (asana_task / hubspot_note / decision_capture /
# task_close / a non-info-for-cora generic) gets an explicit expires_at stamped
# at creation, so the standing PENDING pool is bounded PER ITEM rather than only
# by a global constant read at drain time. KNOWLEDGE items get expires_at=None
# and are NEVER TTL-expired (the D-051 never-expire-unseen guarantee for
# Harrison's queue). The drain's _auto_expire_unrouted_operational honors
# expires_at when present and falls back to proposed_at +
# _OPERATIONAL_UNROUTED_EXPIRY_DAYS for pre-existing rows (back-compat). Default
# 7d tightens the pool from a ~14d-of-inflow window (~286) to ~7d (~140);
# env-overridable via CORA_OPERATIONAL_TTL_DAYS.
_DEFAULT_OPERATIONAL_TTL_DAYS = 7


def _operational_ttl_days() -> int:
    try:
        v = int(os.environ.get("CORA_OPERATIONAL_TTL_DAYS",
                               _DEFAULT_OPERATIONAL_TTL_DAYS))
        return v if v > 0 else _DEFAULT_OPERATIONAL_TTL_DAYS
    except (TypeError, ValueError):
        return _DEFAULT_OPERATIONAL_TTL_DAYS


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
      "expires_at": str|null,    operational: proposed_at + TTL; knowledge: null
      "dm_message_ts": str,      Slack message ts of the Harrison DM (for reaction correlation)
      "dm_channel_id": str,      Slack channel (DM channel ID) where the DM was sent
    }
    """
    _now = datetime.now(timezone.utc)
    # TTL-at-creation (Slice 2): knowledge items never expire (expires_at=None);
    # operational nudges expire proposed_at + TTL. Classified with the SINGLE
    # source of truth so it can't drift from the drain's knowledge/op split.
    # decision_capture is its OWN lane (Fork 4): NOT knowledge (it never enters
    # Harrison's knowledge queue or the autowrite scan) but never-expiring by
    # locked design -- the drain's expiry helpers also skip it by type.
    if is_knowledge_update(update_type, payload) or update_type == UPDATE_TYPE_DECISION:
        expires_at = None
    else:
        expires_at = (_now + timedelta(days=_operational_ttl_days())).isoformat()
    entry = {
        "update_id": update_id,
        "update_type": update_type,
        "description": description,
        "payload": payload,
        "source_evidence": source_evidence[:1000] if source_evidence else "",
        "confidence": confidence,
        "state": "PENDING",
        "proposed_at": _now.isoformat(),
        "resolved_at": None,
        "expires_at": expires_at,
        "dm_message_ts": dm_message_ts,
        "dm_channel_id": dm_channel_id,
    }
    _PROPOSED_UPDATES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _UPDATES_LOCK:
        # Short-circuit membership against the two cached sets (live, then archive)
        # instead of materializing their union every call (adversarial review LOW).
        if update_id in _live_update_ids() or update_id in _archive_update_ids():
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


def outcome_text(action: str, update_type: str, success: bool = True) -> str:
    """THE per-type outcome sentence, shared by the button path and the emoji path.

    C4 (cq-e33ce0545e85): these two paths had forked. The emoji path branched by
    update_type and was test-pinned; `process_one_tap_action` returned ONE string
    for every type -- "Saved to Cora's known-answers." -- even though
    `apply_knowledge_update` routes `efficiency` to design/efficiency-backlog.md
    and `lexicon` to the lexicon file of record. The rendered result contradicted
    itself inside a single line:

        "Saved to Cora's known-answers. (appended to efficiency-backlog.md)"

    Harrison saw that five times on the morning of 2026-08-24. Every one-tap test
    used a known_answer fixture, so nothing caught it. One definition now, so the
    two cannot drift again.
    """
    if action == "APPROVED":
        if not success:
            return (":warning: Approved -- but the automatic save didn't go through; "
                    "I've flagged it in #hjrg-leadership.")
        if update_type == "known_answer":
            return ":white_check_mark: Saved to Cora's known-answers."
        if update_type == "efficiency":
            return ":white_check_mark: Logged to the efficiency backlog."
        if update_type == UPDATE_TYPE_LEXICON:
            return ":white_check_mark: Added to the company lexicon."
        if update_type == UPDATE_TYPE_DECISION:
            return (":inbox_tray: Filed to your decisions inbox (non-canon; "
                    "promotion stays with the cascade).")
        if update_type == UPDATE_TYPE_HUBSPOT_NOTE:
            # Say what actually happens. Nothing is written to HubSpot.
            return (":memo: Handed off to #hjrg-leadership for someone to add in "
                    "HubSpot -- I don't write HubSpot notes myself.")
        return ":white_check_mark: Approved -- I've recorded this."
    if action == "DISMISSED":
        return ":x: Dismissed -- no action taken."
    return ""


# The three affordance sentences every review card ends with. A card that has
# been resolved must not keep advertising them: after the terminal edit the
# button is gone and the emoji is a no-op, so the line is an instruction to do
# something that silently does nothing. On 2026-08-24 Harrison reacted to 19
# cards and 14 of those reactions (74%) were dead -- 11 on cards he had already
# executed nine hours earlier, which still read "Approve / Dismiss (or tap a
# button below)". The same class was fixed once before for the briefing card
# (app._strip_reaction_affordance, "the card never advertises an affordance it
# has just disabled"); this generalises it to the review cards.
# The exact affordance sentences the three card builders append
# (format_single_item_dm :647, the decision card :775, the mechanical card
# :722). Matched as LITERALS, deliberately: this repo has shipped five ReDoS
# regressions in "clever" patterns, and a card footer is a fixed string emitted
# by code in this same module -- there is nothing to generalise over. If a
# builder's wording changes, the pin below fails and the author updates both.
_MECH_AFFORDANCE_DOES = (":+1: do it \u00b7 :-1: dismiss  "
                         "(I carry it out on the next review run)")
# hubspot_note is the odd one out: `_execute_approved_update` writes NOTHING to
# HubSpot for it -- it posts a reminder to #hjrg-leadership for a human. The
# card said "I carry it out", the batch header said "React :+1: to carry one
# out", and the ack said "Approved -- I've recorded this": three statements and
# zero writes. asana_task and task_close DO write, so the promise is true for
# two of the three mechanical types and false for the third. Making the card
# honest is the fix here; performing the write is a new connector-write class
# and is Harrison's call, seeded separately.
_MECH_AFFORDANCE_HANDOFF = (":+1: hand it off \u00b7 :-1: dismiss  "
                            "(I post it to #hjrg-leadership at the next review "
                            "run -- I don't write HubSpot notes myself)")


def mechanical_affordance_line(update_type: str) -> str:
    """The mechanical card's footer, honest per type."""
    if update_type == UPDATE_TYPE_HUBSPOT_NOTE:
        return _MECH_AFFORDANCE_HANDOFF
    return _MECH_AFFORDANCE_DOES


_CARD_AFFORDANCE_LINES = (
    "\U0001F44D Approve \u00b7 \U0001F44E Dismiss  (or tap a button below)",
    "\U0001F44D Accept \u00b7 \U0001F44E Dismiss  (or tap a button below)",
    _MECH_AFFORDANCE_DOES,
    _MECH_AFFORDANCE_HANDOFF,
)
_CARD_AFFORDANCE_RE = re.compile(
    "|".join(re.escape(line) for line in _CARD_AFFORDANCE_LINES)
)

_RESOLVED_NOTE = "_(Resolved -- buttons and reactions no longer apply to this card.)_"


def strip_card_affordance(text: str) -> str:
    """Replace a card's live-affordance line with a resolved note. Returns the
    text unchanged when there is nothing to strip. Cosmetic and fail-soft: a
    regex miss must never block a terminal edit."""
    try:
        if not text:
            return text
        cleaned = _CARD_AFFORDANCE_RE.sub("", text).rstrip()
        if cleaned == text.rstrip():
            return text
        return f"{cleaned}\n\n{_RESOLVED_NOTE}"
    except Exception:  # noqa: BLE001
        return text


def terminal_card_blocks(orig_blocks: list[dict[str, Any]] | None,
                         outcome: str) -> list[dict[str, Any]]:
    """Rebuild a resolved card: keep the sections, strip the stale affordance
    line, drop every actions block, append the outcome as context.

    Shared by the button handler in app.py and the emoji path in the review run
    so a card looks the same however it was resolved -- before this, the emoji
    path never edited the card at all (there is no chat_update anywhere in the
    review script), so an emoji-approved card kept LIVE buttons indefinitely
    under a header still reading "N item(s) below for your approval".
    """
    sections: list[dict[str, Any]] = []
    for b in (orig_blocks or []):
        if not isinstance(b, dict) or b.get("type") != "section":
            continue
        txt = ((b.get("text") or {}).get("text") or "")
        stripped = strip_card_affordance(txt)
        if stripped != txt:
            b = {**b, "text": {**b["text"], "text": stripped}}
        sections.append(b)
    if not sections:
        return [{"type": "section", "text": {"type": "mrkdwn", "text": outcome}}]
    return sections + [
        {"type": "context", "elements": [{"type": "mrkdwn", "text": outcome}]}
    ]


def actionable_reaction_ts() -> set[str]:
    """Every message_ts carrying an actionable `reaction_added` in the reply log.

    Read BEFORE the Step-0 expiry/escalation passes so they cannot retire a row
    that has, in fact, been answered. Two live passes could do exactly that:
    `_auto_dismiss_stale_pending` resolved rows with the reason
    "auto_expired_dmd_unreacted" WITHOUT ever checking for a reaction, and
    `_escalate_stale_mechanical` retires a row as "escalated_unanswered" once its
    escalation budget runs out -- both run before Step 1 reads the reactions at
    all. A 👍 arriving on a row at the end of its budget was discarded silently
    and the row filed as unanswered. That is the founder's approval being
    dropped, which is the exact class this audit exists to close.
    """
    out: set[str] = set()
    try:
        for r in load_reply_log():
            if r.get("event_type") != "reaction_added":
                continue
            if r.get("action", "OTHER") not in ("APPROVED", "DISMISSED"):
                continue
            ts = str(r.get("message_ts") or "")
            if ts:
                out.add(ts)
    except Exception:  # noqa: BLE001 -- a read error must never widen expiry
        log.warning("actionable_reaction_ts: reply-log read failed", exc_info=True)
    return out


def classify_unmatched_reactions() -> dict[str, list[dict[str, Any]]]:
    """Actionable reactions that `correlate_reactions_to_updates` will NOT act on.

    They were previously dropped with no log line, no counter and no ack: the
    correlator iterates ledger rows, so a reaction whose ts matches no PENDING
    row simply never appears. On 2026-08-24 that silently swallowed 14 of
    Harrison's 19 reactions.

    Two populations, which need different answers:
      already_resolved -- the ts maps to a ledger row that is no longer PENDING.
                          He re-approved something already done; a card that
                          stopped advertising its affordance would have prevented
                          it, and he is owed a "no action needed" note.
      no_ledger_row    -- the ts maps to nothing, i.e. a BATCH HEADER (three of
                          them post per run) or an unrelated Cora message.
    """
    out: dict[str, list[dict[str, Any]]] = {"already_resolved": [], "no_ledger_row": []}
    try:
        updates = load_proposed_updates()
        by_ts: dict[str, dict[str, Any]] = {}
        for u in updates:
            ts = str(u.get("dm_message_ts") or "")
            if ts:
                by_ts.setdefault(ts, u)
        seen: set[tuple[str, str, str]] = set()
        for r in load_reply_log():
            if r.get("event_type") != "reaction_added":
                continue
            if r.get("action", "OTHER") not in ("APPROVED", "DISMISSED"):
                continue
            ts = str(r.get("message_ts") or "")
            if not ts:
                continue
            key = (ts, str(r.get("reactor_id") or ""), str(r.get("reaction") or ""))
            if key in seen:
                continue
            seen.add(key)
            row = by_ts.get(ts)
            if row is None:
                out["no_ledger_row"].append(r)
            elif row.get("state") != "PENDING":
                out["already_resolved"].append({**r, "_row": row})
    except Exception:  # noqa: BLE001 -- diagnostics are never load-bearing
        log.warning("classify_unmatched_reactions failed", exc_info=True)
    return out


def correlate_reactions_to_updates() -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Match reply-log reactions to proposed-update entries by DM message_ts.

    Returns list of (update_entry, reaction_entry) pairs for entries that are
    still PENDING and carry an actionable reaction from someone AUTHORIZED to
    act on that particular item.
    Only includes APPROVED or DISMISSED reactions (not OTHER/COMMENT_REQUESTED
    for now — those require follow-up handling).

    Authorization moved from the reactor to the (reactor, item) pair when the
    mechanical review surface was split out (cq-6b014816819c). The old filter
    was `reactor_id != HARRISON` at the top of the loop, which is the same
    answer for a Harrison-only roster and the ONLY thing that has to change for
    a delegated mechanical approver to be able to act. It is deliberately NOT a
    widening: review_lanes.can_approve returns True for a non-Harrison actor
    only on a non-LEX MECHANICAL row, so a name added to the approver file
    cannot reach a judgment or decision card through this path.
    """
    from . import review_lanes

    updates = load_proposed_updates()
    reactions = load_reply_log()

    # dm_message_ts -> the actionable reactions on it, in load_reply_log's own
    # order (NEWEST first -- not arrival order, as the first version of this
    # comment said). Several people can react to the same card, so the actor can
    # no longer be collapsed away before the item is known; the first AUTHORIZED
    # one in that order wins, which for a Harrison-only roster is byte-identical
    # to the previous first-match rule.
    ts_to_reactions: dict[str, list[dict[str, Any]]] = {}
    for r in reactions:
        if r.get("event_type") != "reaction_added":
            continue
        if r.get("action", "OTHER") not in ("APPROVED", "DISMISSED"):
            continue
        msg_ts = r.get("message_ts", "")
        if msg_ts:
            ts_to_reactions.setdefault(msg_ts, []).append(r)

    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for update in updates:
        if update.get("state") != "PENDING":
            continue
        dm_ts = update.get("dm_message_ts", "")
        if not dm_ts:
            continue
        # A Slack ts is unique per CHANNEL, not globally, and the reply log can
        # now accumulate a second person's reactions from other conversations.
        # The row already stores where its card was posted, so compare it when
        # it is there (D-051 lens-2 hardening; rows written before dm_channel_id
        # existed carry "" and keep the ts-only match).
        row_channel = str(update.get("dm_channel_id") or "")
        for r in ts_to_reactions.get(dm_ts, []):
            if row_channel and str(r.get("channel_id") or "") != row_channel:
                continue
            if review_lanes.can_approve(update, r.get("reactor_id", "")):
                pairs.append((update, r))
                break

    return pairs


# ── DM formatting ─────────────────────────────────────────────────────────────

_TYPE_LABEL: dict[str, str] = {
    UPDATE_TYPE_ASANA_TASK:   "Asana task",
    UPDATE_TYPE_HUBSPOT_NOTE: "HubSpot note",
    UPDATE_TYPE_DECISION:     "Decision capture",
    UPDATE_TYPE_TASK_CLOSE:   "Close task",
    UPDATE_TYPE_GENERIC:      "Action",
    UPDATE_TYPE_LEXICON:      "Lexicon term",
}


def _lexicon_card_detail(update: dict[str, Any]) -> str:
    """One structured detail line for a lexicon card (term, meaning, type,
    entity, lane, evidence counts). Re-screened with is_any_phi at THIS egress
    (the read side never trusts write-side redaction); fail-closed withheld."""
    payload = update.get("payload") or {}
    canonical = str(payload.get("canonical") or "")
    canon_note = (f", canonical: {canonical}"
                  if canonical and canonical != payload.get("canonical_name") else "")
    detail = (
        f"`{payload.get('term', '?')}` -> {payload.get('canonical_name', '?')} "
        f"[{payload.get('type', '?')}, {payload.get('entity', '?')}{canon_note}] "
        f"(lane: {payload.get('lane', '?')}"
        + (f", evidence: {payload.get('evidence')}" if payload.get("evidence") else "")
        + ")"
    )
    try:
        from .phi_guard import is_any_phi
        if is_any_phi(detail):
            return "_(term details withheld -- PHI-shaped; see the file diff on approval)_"
    except Exception:  # noqa: BLE001 -- screen failure withholds (fail-closed)
        return "_(term details withheld -- screen unavailable)_"
    return detail


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
    if utype == UPDATE_TYPE_LEXICON:
        lines.append(_lexicon_card_detail(update))
    if evidence:
        snippet = evidence[:300].replace("\n", " ")
        lines.append(f"_Source: {snippet}_")
    # WS17-C: Cora's read (advisory; computed + stashed by run_knowledge_review,
    # "" when unavailable). Decision-SUPPORT only -- never affects the gate.
    # WS-5: the read is Haiku-composed and can carry literal **bold** into this
    # DM (a proactive surface outside format_reply) -- normalize it here.
    coras_read = update.get("_coras_read", "")
    if coras_read:
        from .reply_formatter import normalize_slack_bold
        lines.append(normalize_slack_bold(coras_read))
    lines.append("\n👍 Approve · 👎 Dismiss  (or tap a button below)")
    return "\n".join(lines)


def build_single_item_blocks(update: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """(fallback_text, Block Kit blocks) for one knowledge-review DM.

    The blocks carry ✅/👎 buttons whose `value` is the update_id, handled by the
    bot's Socket-Mode interactivity for instant one-tap approve/dismiss. The text
    is the same string format_single_item_dm produces, kept as the notification
    fallback AND so the emoji 👍/👎 path still works if interactivity is off.
    """
    text = format_single_item_dm(update)
    uid = str(update.get("update_id", ""))
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}},
        {
            "type": "actions",
            "block_id": f"kr_actions_{uid}"[:255],
            "elements": [
                {
                    "type": "button",
                    "action_id": ACTION_APPROVE,
                    "text": {"type": "plain_text", "text": "✅ Approve & save"},
                    "style": "primary",
                    "value": uid,
                },
                {
                    "type": "button",
                    "action_id": ACTION_DISMISS,
                    "text": {"type": "plain_text", "text": "👎 Dismiss"},
                    "value": uid,
                },
            ],
        },
    ]
    return text, blocks


def _bare_url(value: str) -> str:
    """The URL out of a field that may ALREADY be Slack mrkdwn.

    D-051 lens-4: every `hubspot_note` payload stores `deal_url` pre-wrapped as
    `<https://app.hubspot.com/...|Deal Name>` (built that way upstream in
    run_reconciliation), while `task_url` stores a bare URL. Re-wrapping the
    first produced `<<https://...|Name>|Name>`, which Slack parses to the first
    `>` -- a dead link with an invalid scheme and a trailing fragment of literal
    junk, on 100% of hubspot_note cards. `_format_owner_item_line` has the same
    bug; this card copied the pattern rather than inheriting a working one.
    """
    v = str(value or "").strip()
    if v.startswith("<") and v.endswith(">"):
        v = v[1:-1]
    return v.split("|", 1)[0].strip()


def format_mechanical_dm(update: dict[str, Any]) -> str:
    """One mechanical review card (cq-6b014816819c).

    Deliberately terse: this lane is bookkeeping, and its whole problem was that
    179 items of it made a 296-item queue unreadable. It carries the entity so
    an approver who is not the founder can tell whose task they are closing.
    """
    utype = update.get("update_type", "generic")
    payload = update.get("payload") or {}
    desc = update.get("description", "(no description)")
    entity = str(payload.get("entity") or "").strip().upper() or "?"
    label = _TYPE_LABEL.get(utype, utype)
    lines = [f"*[{label}]* `{entity}`\n{desc}"]
    link = _bare_url(payload.get("task_url") or payload.get("deal_url") or "")
    name = payload.get("task_name") or payload.get("deal_name") or ""
    if link:
        lines.append(f"<{link}|{name or 'open in the tool'}>")
    elif name:
        lines.append(name)
    lines.append(mechanical_affordance_line(update.get("update_type", "")))
    return "\n".join(lines)


def build_mechanical_blocks(update: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """(fallback_text, blocks) for a mechanical review card -- NO BUTTONS.

    The one-tap button handler applies through apply_knowledge_update, which has
    no branch for asana_task / task_close / hubspot_note; a button here would
    either report a save nothing performed or need a second implementation of
    the executor that already exists in run_knowledge_review. The emoji path is
    the one that really executes these three types today, so the card offers
    exactly that and says when it will happen. Wiring buttons means lifting that
    executor into this module -- a follow-up, not a footnote on this one.
    """
    # Sanitized HERE, not left to the egress wrapper (D-051 lens-4 MED).
    # slack_egress patches only the `text`/`markdown_text` kwargs, and Slack
    # renders `blocks` and IGNORES `text` when both are present -- so on a
    # card the string a human actually reads never passed the guard, only the
    # invisible fallback did. That matters more here than on the other card
    # types because `description` carries up to 200 characters lifted verbatim
    # out of a gmail/slack/fireflies chunk: an inbound message containing
    # `<https://attacker.example|Approve this>` would otherwise render as a live
    # link inside a DM from Cora that is asking the reader to approve something.
    text = format_mechanical_dm(update)
    try:
        from .slack_egress import sanitize_text
        safe = sanitize_text(text)
    except Exception:  # noqa: BLE001 -- a card is never worth a crash
        log.warning("mechanical card: sanitize unavailable", exc_info=True)
        safe = text
    return safe, [{"type": "section", "text": {"type": "mrkdwn", "text": safe[:2900]}}]


def format_decision_dm(update: dict[str, Any]) -> str:
    """One decision_capture card body (Fork 4). States the non-canon contract
    inline so a tap is never mistaken for a decisions.md write."""
    conf = update.get("confidence", "?")
    desc = (update.get("description") or "(no description)").strip()
    try:  # defensive: no raw <U...> tokens on a Harrison-facing card
        from .tools.user_identity import resolve_slack_mentions
        desc = resolve_slack_mentions(desc)
    except Exception:  # noqa: BLE001
        pass
    conf_emoji = {"HIGH": "🔴", "MED": "🟡", "LOW": "⚪"}.get(conf, "⚪")
    lines = [f"*[Decision capture]* {conf_emoji} `{conf}`\n{desc}"]
    evidence = update.get("source_evidence", "")
    if evidence:
        lines.append(f"_Source: {evidence[:300].replace(chr(10), ' ')}_")
    lines.append(
        "_📥 Accept files this to your decisions inbox (NON-canon; promotion to "
        "decisions.md stays with the Cowork cascade). This card never expires._"
    )
    lines.append("\n👍 Accept · 👎 Dismiss  (or tap a button below)")
    return "\n".join(lines)


def build_decision_blocks(update: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """(fallback_text, Block Kit blocks) for one decision card. Same shape as
    build_single_item_blocks but with the decision action ids, so app.py routes
    the tap to process_decision_tap (inbox filing) -- never the knowledge
    applier. The emoji 👍/👎 fallback still works via the scheduled executor's
    decision branch."""
    text = format_decision_dm(update)
    uid = str(update.get("update_id", ""))
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}},
        {
            "type": "actions",
            "block_id": f"dc_actions_{uid}"[:255],
            "elements": [
                {
                    "type": "button",
                    "action_id": ACTION_DECISION_ACCEPT,
                    "text": {"type": "plain_text", "text": "📥 Accept → inbox"},
                    "style": "primary",
                    "value": uid,
                },
                {
                    "type": "button",
                    "action_id": ACTION_DECISION_DISMISS,
                    "text": {"type": "plain_text", "text": "👎 Dismiss"},
                    "value": uid,
                },
            ],
        },
    ]
    return text, blocks


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
    block_builder=None,
    recipient_id: str | None = None,
) -> dict[str, str]:
    """Send one DM per pending update. Returns {update_id: message_ts}.

    Skips updates that already have dm_message_ts set (already delivered).
    Adds a 0.5s delay between messages to stay within Slack rate limits.
    block_builder defaults to build_single_item_blocks (knowledge cards); the
    decisions lane passes build_decision_blocks and the mechanical review
    surface passes build_mechanical_blocks.

    recipient_id defaults to Harrison, which is every pre-existing caller's
    behaviour. The mechanical surface (cq-6b014816819c) is the first lane whose
    recipient is not necessarily him, and it goes through THIS function rather
    than a parallel sender so the rate-limit pacing, the skip-if-already-sent
    rule and the failed-send-leaves-it-PENDING contract stay in one place.
    """
    import time as _time

    builder = block_builder or build_single_item_blocks
    unsent = [u for u in updates if not u.get("dm_message_ts")]
    if not unsent:
        return {}

    if not slack_bot_token:
        log.error("knowledge_review: SLACK_BOT_TOKEN not set")
        return {}

    target = str(recipient_id or HARRISON_SLACK_USER_ID).strip() or HARRISON_SLACK_USER_ID
    try:
        client = _client_factory() if _client_factory else _build_slack_client(slack_bot_token)
        open_resp = client.conversations_open(users=[target])
        dm_channel = open_resp["channel"]["id"]
    except Exception as exc:
        log.error("knowledge_review: could not open DM channel: %s", exc)
        return {}

    results: dict[str, str] = {}
    for update in unsent:
        text, blocks = builder(update)
        try:
            resp = client.chat_postMessage(
                channel=dm_channel,
                text=text,
                blocks=blocks,
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


# ── One-tap approve: shared executor + processor (2026-07-09 write-path) ─────
# The KNOWLEDGE types the review DM carries are ALL local file writes (no
# connector writes): known_answer -> gap_autofill.apply_known_answer (writes the
# live Drive _brain/known-answers store via env KNOWN_ANSWERS_DIR), efficiency ->
# friction_mining.apply_efficiency, an #info-for-cora generic ->
# gap_autofill.apply_contributed_note. This function is bot-loadable (function-
# level imports avoid any import cycle) so the Socket-Mode button handler in
# app.py can apply an item the instant Harrison taps Approve. The scheduled
# run_knowledge_review executor is UNCHANGED and stays the belt-and-braces for
# emoji reactions; an item is claimed by exactly one path (resolve_update flips
# PENDING once), so there is no double-processing.

def apply_knowledge_update(update: dict[str, Any]) -> tuple[bool, str]:
    """Execute an approved KNOWLEDGE update (local writes only). Returns
    (ok, summary). Never raises. Non-knowledge types are refused (the button is
    knowledge-only; connector writes route to owners / stay Harrison-run)."""
    utype = (update or {}).get("update_type", "")
    payload = (update or {}).get("payload") or {}
    try:
        if utype == "known_answer":
            from .gap_autofill import apply_known_answer
            ok, summary = apply_known_answer(payload)
            if ok:
                try:  # WS-3 golden-set auto-growth (fail-soft; parity w/ scheduled run)
                    from .golden_set import append_case_from_known_answer
                    append_case_from_known_answer(payload)
                except Exception:  # noqa: BLE001
                    log.warning("golden-set auto-growth failed (non-fatal)", exc_info=True)
            return ok, summary
        if utype == "efficiency":
            from .friction_mining import apply_efficiency
            return apply_efficiency(payload)
        if utype == UPDATE_TYPE_LEXICON:
            from .lexicon_writer import apply_lexicon_update
            ok, summary = apply_lexicon_update(payload)
            if ok:
                try:  # golden-set auto-growth (fail-soft; parity w/ known_answer)
                    from .golden_set import append_case_from_lexicon
                    append_case_from_lexicon(payload)
                except Exception:  # noqa: BLE001
                    log.warning("golden-set auto-growth failed (non-fatal)", exc_info=True)
            return ok, summary
        if utype == "generic" and payload.get("source") == "info-for-cora":
            from .gap_autofill import apply_contributed_note
            ok, summary = apply_contributed_note(payload)
            if ok:
                try:
                    from .golden_set import append_case_from_note
                    append_case_from_note(payload)
                except Exception:  # noqa: BLE001
                    log.warning("golden-set auto-growth failed (non-fatal)", exc_info=True)
            return ok, summary
        return False, f"update type '{utype}' is not one-tap-approvable"
    except Exception as exc:  # noqa: BLE001 -- handler must not crash the bot
        log.error("knowledge_review: apply_knowledge_update failed: %s", exc, exc_info=True)
        return False, f"apply failed: {exc}"


def _find_update(update_id: str) -> dict[str, Any] | None:
    for u in load_proposed_updates():
        if u.get("update_id") == update_id:
            return u
    return None


def process_one_tap_action(
    update_id: str, actor_id: str, *, approve: bool,
) -> tuple[str, str]:
    """Handle a one-tap Approve/Dismiss on a knowledge-review DM.

    Returns (outcome, user_message) where outcome is one of:
      not_authorized | not_found | already_resolved | approved | apply_failed |
      dismissed.

    Authorization is per ITEM (review_lanes.can_approve), not per reactor. For
    every card this handler renders today that resolves to Harrison-only, which
    is what D-011 requires: the judgment and decision lanes are Harrison-only in
    code and no configuration can express otherwise. The mechanical lane is
    delegable but is refused here outright -- see the guard below.

    Concurrency: the whole load->state-check->apply->resolve critical section
    runs under _ONE_TAP_LOCK so two concurrent taps (Socket Mode dispatches on a
    thread pool) cannot both pass the PENDING check and both apply. The update is
    re-read from disk INSIDE the lock, so the second tap sees the first's
    APPROVED/DISMISSED state and no-ops. apply-first-then-resolve (not the
    reverse) so an apply FAILURE leaves the item PENDING -- never 'approved but
    unsaved'. Cross-process (button vs the scheduled run) is additionally guarded
    by each applier's own dedup (known_answer resolved-ledger, contributed_note
    line, efficiency same-day-title).
    """
    from . import review_lanes

    # Cheap pre-check BEFORE the lookup so a stranger's tap still cannot tell a
    # real update_id from a made-up one. Per-item authorization follows inside
    # the lock; this only keeps the not_found/not_authorized split from becoming
    # an existence oracle for someone with no review role at all.
    if not review_lanes.is_review_approver(actor_id):
        log.warning("knowledge_review: one-tap action by non-approver %s ignored", actor_id)
        return "not_authorized", "Only Harrison can approve knowledge items."

    with _ONE_TAP_LOCK:
        update = _find_update(update_id)
        if update is None:
            return "not_found", "I can't find that item anymore (it may have expired)."
        # Authorization AFTER the lookup, because it is now per ITEM: the lane
        # decides who may act. For every card this handler actually renders
        # today (the judgment lane) the answer is Harrison and nobody else, so
        # this is the same gate it has always been.
        if not review_lanes.can_approve(update, actor_id):
            log.warning("knowledge_review: one-tap action by unauthorized %s ignored "
                        "(lane=%s)", actor_id,
                        review_lanes.lane_for(update.get("update_type"),
                                              update.get("payload")))
            return "not_authorized", "Only Harrison can approve knowledge items."
        # A MECHANICAL row must never reach apply_knowledge_update: that applier
        # has no branch for asana_task/task_close/hubspot_note, so it would
        # report a save that no writer performed. The mechanical surface is
        # emoji-correlated and executes through run_knowledge_review's
        # _execute_approved_update, which does have those branches. Nothing
        # renders a button on a mechanical card, so reaching here means a stale
        # or forged tap -- answer honestly rather than fabricating an outcome.
        if review_lanes.is_mechanical(update):
            return ("not_authorized",
                    "That one is handled on the mechanical review surface -- "
                    "react to its card with :+1: or :-1: instead.")
        if update.get("state") != "PENDING":
            return ("already_resolved",
                    f"Already handled ({str(update.get('state', '')).lower() or 'resolved'}).")

        if not approve:
            resolve_update(update_id, "DISMISSED", reason="one_tap_button")
            log.info("knowledge_review: one-tap DISMISS %s", update_id[:8])
            return "dismissed", "👎 Dismissed — I won't save this."

        ok, summary = apply_knowledge_update(update)
        if not ok:
            log.warning("knowledge_review: one-tap approve apply failed %s: %s",
                        update_id[:8], summary)
            # Left PENDING; apply failures here are ~always permanent (empty
            # answer / looks-like-PHI), so no auto-retry is promised -- it will
            # auto-expire at the next review if not otherwise resolved.
            return ("apply_failed",
                    f"⚠️ Couldn't save: {summary}. Not stored (it may be empty or look like PHI).")
        resolve_update(update_id, "APPROVED", reason="one_tap_button")
        log.info("knowledge_review: one-tap APPROVE %s (%s)", update_id[:8], summary)
        # C4: was a hard-coded "Saved to Cora's known-answers" for EVERY type,
        # which contradicted its own parenthetical on efficiency and lexicon
        # items ("Saved to known-answers. (appended to efficiency-backlog.md)").
        return "approved", (f"{outcome_text('APPROVED', update.get('update_type', ''))} "
                            f"({summary})")


def process_decision_tap(
    update_id: str, actor_id: str, *, approve: bool,
) -> tuple[str, str]:
    """Handle a one-tap Accept/Dismiss on a DECISION card (Fork 4).

    Same contract and concurrency discipline as process_one_tap_action
    (Harrison-only, _ONE_TAP_LOCK, re-read inside the lock, apply-first-then-
    resolve), but the accept path files to the NON-canon decisions inbox via
    decision_inbox.apply_decision_accept -- deliberately NOT
    apply_knowledge_update, so the autowrite path can never reach this type.

    Outcomes: not_authorized | not_found | already_resolved | accepted |
    excluded | apply_failed | dismissed. A DETERMINISTIC LEX/PHI re-screen
    refusal at apply time DISMISSES the item (reason lex_phi_excluded) -- it
    should never have rendered, and with no TTL on this lane an un-dismissable
    refused row would sit PENDING forever. A screen ERROR (transient: import/
    env failure inside the screen) is NOT terminal -- the row stays PENDING and
    the tap reports apply_failed (D-051 screen-error-terminal-mass-dismiss)."""
    if actor_id != HARRISON_SLACK_USER_ID:
        log.warning("knowledge_review: decision tap by non-Harrison %s ignored", actor_id)
        return "not_authorized", "Only Harrison can act on decision cards."

    with _ONE_TAP_LOCK:
        update = _find_update(update_id)
        if update is None:
            return "not_found", "I can't find that decision anymore."
        if update.get("state") != "PENDING":
            return ("already_resolved",
                    f"Already handled ({str(update.get('state', '')).lower() or 'resolved'}).")

        if not approve:
            resolve_update(update_id, "DISMISSED", reason="one_tap_button")
            log.info("knowledge_review: decision one-tap DISMISS %s", update_id[:8])
            return "dismissed", "👎 Dismissed — not filed."

        from .decision_inbox import apply_decision_accept
        ok, summary = apply_decision_accept(update, via="one_tap_button")
        if not ok:
            if (summary.startswith("excluded:")
                    and "screen_error" not in summary):
                # Fail-closed LEX/PHI wall tripped DETERMINISTICALLY at the
                # durable write. (A screen_error falls through to apply_failed
                # below -- transient, retryable, never a terminal dismissal.)
                resolve_update(update_id, "DISMISSED", reason="lex_phi_excluded")
                log.warning("knowledge_review: decision %s excluded at apply (%s)",
                            update_id[:8], summary)
                return ("excluded",
                        "🚫 Withheld — this looks LEX/PHI-scoped, so it can't be "
                        "filed (fail-closed). Dismissed.")
            log.warning("knowledge_review: decision one-tap apply failed %s: %s",
                        update_id[:8], summary)
            # Left PENDING (transient I/O failure): the emoji 👍 path or a re-run
            # can retry; the card never expires so nothing is silently lost.
            return "apply_failed", f"⚠️ Couldn't file: {summary}. Left pending."
        resolve_update(update_id, "APPROVED", reason="one_tap_button")
        log.info("knowledge_review: decision one-tap ACCEPT %s (%s)", update_id[:8], summary)
        return ("accepted",
                f"📥 Filed to your decisions inbox — non-canon; promotion to "
                f"decisions.md stays with the cascade. ({summary})")


# ── Graduated-trust AUTO-WRITE (§7B, 2026-07-21; D-011 relaxed -> reversible) ────
# WS17-C's SILENT auto-approve was retired (D-060). This RE-INTRODUCES auto-write
# for LOW-STAKES knowledge, but DELIBERATELY and safely:
#   * env-gated, DEFAULT OFF (CORA_AUTOWRITE_LIVE unset -> today's behavior exactly)
#   * tier-scoped: the graduated_trust classifier keeps Tier-2 (money/contracts/
#     legal/equity/comp/PHI/LEX/cross-entity/conflicts-with-canon) Harrison-gated
#     BY CONSTRUCTION; only Tier 0/1 can reach apply_autowrite
#   * fully AUDITED (logs/cora-autowrite-audit.jsonl) + REVERTIBLE (one-tap in the
#     weekly digest). Oversight-after-the-fact replaces the per-item gate.
# The CALLER (run_knowledge_review) owns the tier decision + an independent
# is_high_stakes belt; this module owns the durable write + audit + revert.

_AUTOWRITE_AUDIT_PATH = _REPO_ROOT / "logs" / "cora-autowrite-audit.jsonl"
_AUTOWRITE_LOCK = Lock()
ACTION_AUTOWRITE_REVERT = "kb_autowrite_revert"


def autowrite_level() -> str:
    """CORA_AUTOWRITE_LIVE: 'off' (default -> nothing auto-writes), 'tier0' (only
    CORROBORATED + allowlist + recognized-teammate items auto-write), or 'all'
    (Tier-0 AND Tier-1). Tier-2 is NEVER auto-written at any level."""
    v = (os.environ.get("CORA_AUTOWRITE_LIVE", "off") or "off").strip().lower()
    return v if v in ("off", "tier0", "all") else "off"


def _autowrite_target_files(update: dict[str, Any] | None = None) -> list[Path]:
    """The files an auto-write may append to (env-aware), snapshotted around an
    apply so the revert payload is the exact inserted block.

    When the update is a LEXICON item, the target is DETERMINISTIC (payload
    type/entity routing) -- return just that one file so a concurrent write to
    any other target inside the snapshot window can never be misattributed as
    this apply's revert payload (D-051 remediation F11). Fail-soft to the full
    set if the routing is unavailable."""
    if (update or {}).get("update_type") == UPDATE_TYPE_LEXICON:
        try:
            from .lexicon_writer import target_path_for
            return [target_path_for((update or {}).get("payload") or {})]
        except Exception:  # noqa: BLE001
            pass
    files: list[Path] = []
    try:
        from .gap_autofill import _known_answers_dir
        kd = _known_answers_dir()
        if kd.exists():
            files.extend(sorted(kd.glob("*.md")))
    except Exception:  # noqa: BLE001
        pass
    try:
        from .friction_mining import _backlog_path
        bp = _backlog_path()
        if bp.exists():
            files.append(bp)
    except Exception:  # noqa: BLE001
        pass
    # Lexicon stores (S4): the three files of record a lexicon apply may touch.
    # Appended AFTER the known-answers/backlog set so the existing revert
    # behavior for those files stays byte-identical (test-pinned).
    try:
        from . import lexicon as _lexicon
        lex_dir = _lexicon._lexicon_dir()
        if lex_dir.is_dir():
            files.extend(sorted(lex_dir.glob("*.yaml")))
        for p in (_lexicon._sku_aliases_path(), _lexicon._user_aliases_path()):
            if p.exists():
                files.append(p)
    except Exception:  # noqa: BLE001
        pass
    return files


def _snapshot(files: list[Path]) -> dict[str, str]:
    snap: dict[str, str] = {}
    for f in files:
        try:
            snap[str(f)] = f.read_text(encoding="utf-8")
        except OSError:
            snap[str(f)] = ""
    return snap


def _diff_added(before: str, after: str) -> list[str]:
    """The lines present in `after` but not `before` (the inserted block)."""
    import difflib
    b = before.splitlines()
    a = after.splitlines()
    added: list[str] = []
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(a=b, b=a, autojunk=False).get_opcodes():
        if tag in ("insert", "replace"):
            added.extend(a[j1:j2])
    return added


def _remove_block(lines: list[str], block: list[str]) -> list[str] | None:
    """Remove the first contiguous occurrence of `block` from `lines`. Returns the
    new list, or None if the block is not present (file changed / already gone)."""
    if not block:
        return None
    n = len(block)
    for i in range(0, len(lines) - n + 1):
        if lines[i:i + n] == block:
            return lines[:i] + lines[i + n:]
    return None


def log_autowrite(record: dict[str, Any]) -> None:
    """Append one auto-write audit record to logs/cora-autowrite-audit.jsonl."""
    record.setdefault("ts", _now_iso())
    _AUTOWRITE_AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _AUTOWRITE_LOCK:
        with _AUTOWRITE_AUDIT_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _read_autowrite_records_locked() -> list[dict[str, Any]]:
    if not _AUTOWRITE_AUDIT_PATH.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in _AUTOWRITE_AUDIT_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def read_autowrite_audit(since_ts: float | None = None) -> list[dict[str, Any]]:
    """Read audit records (optionally only ts >= since_ts epoch seconds)."""
    with _AUTOWRITE_LOCK:
        records = _read_autowrite_records_locked()
    if since_ts is None:
        return records
    out = []
    for rec in records:
        try:
            if datetime.fromisoformat(str(rec.get("ts", ""))).timestamp() >= since_ts:
                out.append(rec)
        except ValueError:
            out.append(rec)
    return out


def apply_autowrite(update: dict[str, Any], *, tier: int, reason: str,
                    contributor: str = "") -> tuple[bool, str]:
    """Auto-apply a low-stakes knowledge update WITHOUT a Harrison gate, capturing
    a revert payload + an audit line. Reuses the SAME idempotent executor the gated
    path uses (apply_knowledge_update), so an auto-write is byte-identical to a
    Harrison-approved one -- including the fail-closed PHI re-check inside the
    appliers. Returns (ok, summary). Never raises."""
    # Fork 4 invariant belt: decision_capture is never-autowrite-eligible BY
    # TYPE. Structurally it can't reach here (the drain's decision lane never
    # enters the autowrite scan, and apply_knowledge_update refuses the type),
    # but the invariant is load-bearing enough to state at this chokepoint too.
    if (update or {}).get("update_type") == UPDATE_TYPE_DECISION:
        return False, "decision_capture is never autowrite-eligible (by type)"
    targets = _autowrite_target_files(update)
    before = _snapshot(targets)
    try:
        ok, summary = apply_knowledge_update(update)
    except Exception as exc:  # noqa: BLE001 -- apply already never raises, belt anyway
        return False, f"apply failed: {exc}"
    if not ok:
        return ok, summary
    after = _snapshot(_autowrite_target_files(update))
    target_file = ""
    added: list[str] = []
    for path, aft in after.items():
        if aft != before.get(path, ""):
            target_file = path
            added = _diff_added(before.get(path, ""), aft)
            break
    uid = str(update.get("update_id", ""))
    # Audit BEFORE resolve (D-051 fix): a crash between the two must leave the item
    # PENDING (idempotently re-applied next run) rather than APPROVED-but-unaudited/
    # unrevertable. Dedup: an idempotent no-op re-apply yields added==[]; do NOT
    # shadow the original real-payload record with an empty one -- (re-)audit only
    # when there is a real payload OR no prior record exists for this uid.
    prior = any(r.get("update_id") == uid and r.get("decision_reason") != "revert"
                for r in read_autowrite_audit())
    if added or not prior:
        log_autowrite({
            "update_id": uid,
            "update_type": update.get("update_type", ""),
            "entity": (update.get("payload") or {}).get("entity", ""),
            "tier": tier,
            "decision_reason": reason,
            "contributor": contributor,
            "summary": summary,
            "reverted": False,
            "revert": {"target_file": target_file, "added_lines": added},
        })
    resolve_update(uid, "APPROVED", reason=reason)
    log.info("knowledge_review: AUTO-WRITE %s tier=%d (%s)", uid[:8], tier, summary)
    return ok, summary


def process_autowrite_revert(update_id: str, actor_id: str) -> tuple[str, str]:
    """Harrison-only revert of an auto-write: remove the exact inserted block from
    the target .md (the next static sync self-heals any KB copy via
    replace-on-conflict), mark the audit record reverted, and flip the proposed
    update to DISMISSED. Idempotent: a second revert is a no-op. Returns
    (outcome, message)."""
    if actor_id != HARRISON_SLACK_USER_ID:
        return "not_authorized", "Only Harrison can revert an auto-write."
    tf = ""
    removed = 0
    with _AUTOWRITE_LOCK:
        records = _read_autowrite_records_locked()
        target = None
        for rec in reversed(records):
            if rec.get("update_id") == update_id and rec.get("decision_reason") != "revert":
                target = rec
                break
        if target is None:
            return "not_found", "I can't find that auto-write in the audit log."
        if target.get("reverted"):
            return "already_reverted", "That auto-write was already reverted."
        rv = target.get("revert") or {}
        tf = rv.get("target_file") or ""
        added = rv.get("added_lines") or []
        if tf and added:
            p = Path(tf)
            try:
                lines = p.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                return "error", f"Couldn't read {tf}: {exc}"
            new_lines = _remove_block(lines, added)
            if new_lines is None:
                # The file was edited since the auto-write -- the exact block is no
                # longer present. Do NOT mark reverted (leave it re-attemptable) and
                # do NOT report a success that removed nothing (D-051 fix).
                return ("content_changed",
                        f"{Path(tf).name} was edited since the auto-write -- I could not find the "
                        "exact block to remove. Left it un-reverted; please remove it manually.")
            # Post-revert reparse guard (lexicon D-051 remediation F9, HIGH): a
            # block that CREATED a YAML section (learned:/learned_aliases:/a
            # terms: re-open) may have later rows appended under it -- removing
            # the header would orphan them and yaml.safe_load of the whole store
            # would start failing (fail-soft loaders then blank the alias map).
            # Refuse the revert instead of corrupting the file.
            if tf.endswith((".yaml", ".yml")):
                try:
                    import yaml
                    yaml.safe_load("\n".join(new_lines) + "\n")
                except Exception:  # noqa: BLE001
                    return ("content_changed",
                            f"Reverting this block would leave {Path(tf).name} unparseable "
                            "(a later entry depends on it). Left it un-reverted; remove the "
                            "lines manually.")
            try:
                p.write_text("\n".join(new_lines) + ("\n" if new_lines else ""), encoding="utf-8")
                removed = len(added)
            except OSError as exc:
                return "error", f"Couldn't rewrite {tf}: {exc}"
        # The block was removed (or there was no .md payload to remove) -> revert.
        # mark reverted + append a revert marker; rewrite the whole audit atomically
        target["reverted"] = True
        records.append({"ts": _now_iso(), "update_id": update_id,
                        "update_type": target.get("update_type", ""),
                        "decision_reason": "revert", "reverted": True,
                        "summary": f"reverted by {actor_id}; removed {removed} line(s) from {tf}"})
        tmp = _AUTOWRITE_AUDIT_PATH.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        tmp.replace(_AUTOWRITE_AUDIT_PATH)
    # outside the audit lock: flip the proposed update so it isn't treated as applied
    try:
        resolve_update(update_id, "DISMISSED", reason="autowrite_reverted")
    except Exception:  # noqa: BLE001
        pass
    tail = Path(tf).name if tf else "the target file"
    return "reverted", f"↩️ Reverted -- removed the auto-written block from {tail}."


def build_autowrite_digest_blocks(records: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """(fallback_text, Block Kit) for the weekly auto-write digest: one section +
    a one-tap Revert button per item (value = update_id)."""
    n = len(records)
    # D5 (Rider D): the second line tells Harrison the digest is a safety net, not
    # a to-do -- the only action is Revert, and only if something looks wrong.
    header = (f":robot_face: *Cora auto-learned {n} item(s) this week* (Tier 0/1, reversible)"
              f"\n_Nothing needed unless something looks wrong -- tap ↩️ Revert to undo an item._")
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
    ]
    for rec in records[:20]:
        uid = str(rec.get("update_id", ""))
        ent = rec.get("entity", "")
        summ = str(rec.get("summary", ""))[:200]
        txt = (f"*[{rec.get('update_type', '')}]* tier {rec.get('tier', '?')}"
               f"{(' · ' + ent) if ent else ''}\n{summ}")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": txt[:2900]}})
        blocks.append({"type": "actions", "block_id": f"aw_{uid}"[:255], "elements": [
            {"type": "button", "action_id": ACTION_AUTOWRITE_REVERT,
             "text": {"type": "plain_text", "text": "↩️ Revert"}, "style": "danger",
             "value": uid}]})
    return header, blocks
