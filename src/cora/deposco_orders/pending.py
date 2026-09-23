"""Disk-backed pending-order store + append-only push ledger.

Design of record: SONNET-HANDOFF step 4. Mirrors `f3e_blog/publish_cards.py`'s
disk-backed-not-process-memory pattern for the same reason: the CLI stage
script and the always-on bot are two different interpreters, so a stash
minted in either one's process memory would not exist when the other reaches
for it.

UNLIKE publish_cards, this is ONE JSON FILE PER PENDING ENTRY
(`data/state/deposco-order-pending/{id}.json`), not a single shared
append-only event log. The two writers here (the CLI stage script and the
bot's tap handler) are temporally separated -- staging creates a file once
and exits; only the tap handler ever mutates it after that, so there is
never a genuine two-PROCESS race on one file. An atomic replace
(`drive_io.write_text_atomic`) on every write still guards against a crash
mid-write tearing the file, and a process-local `threading.Lock` around the
CLAIM step guards the race THAT DOES exist: two near-simultaneous taps on the
SAME running bot process (the same race `publish_cards._LOCK` guards).

Ledger (`data/state/deposco-push-ledger.jsonl`) is append-only, one row per
resolved pending entry. The per-channel CONSECUTIVE-CLEAN COUNTER is DERIVED
from it, never stored separately (1kkkkkkk honesty rail).

WHAT "CLEAN" MEANS IN THIS BUILD (a documented simplification, not a guess):
the full design definition requires a ship-confirm re-read days later plus
Alex's no-Nimbl-correction attestation -- neither is built this session (no
polling task, no attestation UI). `clean` here is `event == CONFIRMED`
(first-tap create + immediate read-back match) -- i.e. "provisional_clean" in
the design's own vocabulary. A follow-up build adds the ship-confirm pass
that promotes provisional_clean to clean or demotes it to not_clean; until
then this counter is a floor, never an inflated claim.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock

from .. import drive_io
from .preflight import PreflightResult

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_AZ = timezone(timedelta(hours=-7))
_CLAIM_LOCK = Lock()

STATE_STAGED = "STAGED"
STATE_CLAIMED = "CLAIMED"
STATE_CONFIRMED = "CONFIRMED"
STATE_FAILED = "FAILED"
STATE_UNKNOWN = "UNKNOWN"
STATE_ANOMALY_UPDATED = "ANOMALY_UPDATED"
STATE_MISMATCH = "MISMATCH"
STATE_DISMISSED = "DISMISSED"

#: UNKNOWN never auto-retries and locks the entry -- it belongs here with the
#: other terminal states even though it is also the least resolved of them.
TERMINAL_STATES = frozenset({
    STATE_CONFIRMED, STATE_FAILED, STATE_UNKNOWN, STATE_ANOMALY_UPDATED,
    STATE_MISMATCH, STATE_DISMISSED,
})

#: Ledger rows that represent a real push ATTEMPT outcome, as opposed to a
#: correction row (`root_cause_note`) appended later against the same number.
PUSH_OUTCOME_EVENTS = frozenset({
    STATE_CONFIRMED, STATE_FAILED, STATE_UNKNOWN, STATE_ANOMALY_UPDATED, STATE_MISMATCH,
})


def _now_iso() -> str:
    return datetime.now(_AZ).isoformat(timespec="seconds")


def pending_dir() -> Path:
    return Path(os.environ.get(
        "CORA_DEPOSCO_PENDING_DIR",
        str(_REPO_ROOT / "data" / "state" / "deposco-order-pending"),
    ))


def ledger_path() -> Path:
    return Path(os.environ.get(
        "CORA_DEPOSCO_PUSH_LEDGER_PATH",
        str(_REPO_ROOT / "data" / "state" / "deposco-push-ledger.jsonl"),
    ))


def demotion_state_path() -> Path:
    return Path(os.environ.get(
        "CORA_DEPOSCO_DEMOTION_STATE_PATH",
        str(_REPO_ROOT / "data" / "state" / "deposco-standing-demotion.json"),
    ))


def mint_id() -> str:
    return "deposco-" + secrets.token_hex(6)


def hash_payload(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


# ── Pending entries (one JSON file per id) ───────────────────────────────────


def _entry_path(pending_id: str) -> Path:
    return pending_dir() / f"{pending_id}.json"


def _write_entry(entry: dict) -> None:
    drive_io.write_text_atomic(
        _entry_path(entry["id"]), json.dumps(entry, indent=2, sort_keys=True),
    )


def get_entry(pending_id: str) -> dict | None:
    path = _entry_path(pending_id)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def stage_entry(
    *, channel: str, number: str, payload: dict, preflight_result: PreflightResult,
    env: str = "prod", spec_raw: dict | None = None, spec_path: str = "",
    authored_by: str = "",
) -> dict:
    """Persist a freshly staged, PUSHABLE entry. Called only by
    `scripts/run_deposco_order_stage.py` after `preflight_result.passed`.
    `env` is stamped so the tap handler pushes to the SAME environment this
    was staged (and preflighted) against -- never re-derived or guessed."""
    entry = {
        "id": mint_id(),
        "channel": channel,
        "number": number,
        "env": env,
        "state": STATE_STAGED,
        "payload": payload,
        "payload_hash": hash_payload(payload),
        "spec_raw": spec_raw or {},
        "spec_path": spec_path,
        "authored_by": authored_by,
        "preflight": {
            "passed": preflight_result.passed,
            "checked_at": preflight_result.checked_at,
            "checks": [asdict(c) for c in preflight_result.checks],
        },
        "created_at": _now_iso(),
        "claimed_by": "",
        "claimed_at": "",
        "dm_channel_id": "",
        "dm_message_ts": "",
    }
    _write_entry(entry)
    return entry


def record_delivery(pending_id: str, *, dm_channel_id: str, dm_message_ts: str) -> dict | None:
    """Record that the card actually landed in Slack. Does not touch `state`
    -- delivery and lifecycle state are orthogonal, same distinction
    `publish_cards.card_was_delivered` draws."""
    entry = get_entry(pending_id)
    if entry is None:
        return None
    entry["dm_channel_id"] = dm_channel_id
    entry["dm_message_ts"] = dm_message_ts
    _write_entry(entry)
    return entry


def claim_for_push(pending_id: str, actor_id: str) -> dict | None:
    """Atomically claim a STAGED entry so a push can begin.

    Returns the claimed entry, or None if it cannot be claimed (missing,
    already claimed, or already terminal) -- the caller must treat None as
    "someone else already has this" and say so without leaking WHICH state
    it is in to an unauthorized actor (that check happens one layer up, in
    `handler.py`, before this is ever called).
    """
    with _CLAIM_LOCK:
        entry = get_entry(pending_id)
        if entry is None or entry.get("state") != STATE_STAGED:
            return None
        entry["state"] = STATE_CLAIMED
        entry["claimed_by"] = actor_id
        entry["claimed_at"] = _now_iso()
        _write_entry(entry)
        return entry


def release_claim(pending_id: str, why: str) -> dict | None:
    """Hand a claimed entry back to STAGED (e.g. preflight drift detected at
    tap -- re-stage, never push). Refuses to move an entry out of a terminal
    state (mirrors publish_cards._release_claim's same guard)."""
    with _CLAIM_LOCK:
        entry = get_entry(pending_id)
        if entry is None or entry.get("state") in TERMINAL_STATES:
            return entry
        entry["state"] = STATE_STAGED
        entry["claimed_by"] = ""
        entry["claimed_at"] = ""
        entry["release_reason"] = why
        _write_entry(entry)
        return entry


def resolve(pending_id: str, outcome_state: str, **fields) -> dict | None:
    """Move a CLAIMED entry to a terminal state.

    Refuses to move an entry that is ALREADY terminal -- this is what makes
    UNKNOWN's lock real: once written, nothing in this module can move that
    entry again, ever, regardless of what a later caller passes.
    """
    if outcome_state not in TERMINAL_STATES:
        raise ValueError(f"{outcome_state!r} is not a terminal state")
    entry = get_entry(pending_id)
    if entry is None:
        return None
    if entry.get("state") in TERMINAL_STATES:
        return entry
    entry["state"] = outcome_state
    entry["resolved_at"] = _now_iso()
    entry.update(fields)
    _write_entry(entry)
    return entry


def dismiss(pending_id: str, actor_id: str, reason: str = "") -> dict | None:
    with _CLAIM_LOCK:
        entry = get_entry(pending_id)
        if entry is None or entry.get("state") != STATE_STAGED:
            return None
        entry["state"] = STATE_DISMISSED
        entry["dismissed_by"] = actor_id
        entry["dismiss_reason"] = reason
        entry["resolved_at"] = _now_iso()
        _write_entry(entry)
        return entry


# ── Ledger (append-only) ─────────────────────────────────────────────────────


def append_ledger_row(
    *, channel: str, number: str, event: str, clean: bool | None = None,
    actor: str = "", payload_hash: str = "", **extra,
) -> None:
    row = {
        "ts": int(time.time()), "at": _now_iso(), "channel": channel, "number": number,
        "event": event, "clean": clean, "actor": actor, "payload_hash": payload_hash,
    }
    row.update(extra)
    try:
        drive_io.append_text(ledger_path(), json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001 -- an audit failure must never break the reply
        log.error("deposco_orders.pending: ledger write FAILED for %s/%s: %s",
                  channel, number, exc)


def add_root_cause_note(channel: str, number: str, note: str, *, actor: str = "") -> None:
    """A correction row, not an edit of history -- ledgers here are
    append-only, same discipline as publish_cards' event log."""
    append_ledger_row(
        channel=channel, number=number, event="root_cause_note",
        root_cause_note=note, actor=actor,
    )


def _read_ledger() -> list[dict]:
    path = ledger_path()
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def consecutive_clean_count(channel: str) -> int:
    """Consecutive `clean` push-outcome rows for `channel`, most recent first,
    stopping at the first not-clean one. UA pushes never reach this ledger at
    all (only prod pushes are recorded here) so they never count."""
    push_rows = [r for r in _read_ledger()
                 if r.get("channel") == channel and r.get("event") in PUSH_OUTCOME_EVENTS]
    count = 0
    for row in reversed(push_rows):
        if row.get("clean") is True:
            count += 1
        else:
            break
    return count


def channel_stage_blocked(channel: str) -> tuple[bool, str]:
    """(blocked, reason). A not-clean last push blocks the channel's next
    stage until a root-cause note is appended for that same order number --
    enforced by `scripts/run_deposco_order_stage.py` calling this first."""
    rows = _read_ledger()
    push_rows = [r for r in rows if r.get("channel") == channel
                 and r.get("event") in PUSH_OUTCOME_EVENTS]
    if not push_rows:
        return False, ""
    last = push_rows[-1]
    if last.get("clean") is True:
        return False, ""
    number = last.get("number")
    has_note = any(
        r.get("event") == "root_cause_note" and r.get("number") == number
        for r in rows
    )
    if has_note:
        return False, ""
    return True, (
        f"channel {channel!r}'s last push ({number}) was not clean and has no "
        f"root-cause note yet -- add one before staging another order on this channel"
    )


# ── Standing demotion (ledger-derived-STYLE state; see the module docstring
# for why this needs its own small file rather than a pure ledger scan) ──────


def _load_demotion_state() -> dict:
    path = demotion_state_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def is_demoted(channel: str) -> bool:
    return bool(_load_demotion_state().get(channel, {}).get("demoted"))


def demote_channel(channel: str, *, reason: str) -> None:
    """Any not-clean push in standing flips the channel back to supervised,
    honoured over the env flag until Harrison re-flips (`clear_demotion`,
    which nothing in this build calls automatically)."""
    state = _load_demotion_state()
    state[channel] = {"demoted": True, "reason": reason, "at": _now_iso()}
    drive_io.write_text_atomic(
        demotion_state_path(), json.dumps(state, indent=2, sort_keys=True),
    )


def clear_demotion(channel: str) -> None:
    state = _load_demotion_state()
    state.pop(channel, None)
    drive_io.write_text_atomic(
        demotion_state_path(), json.dumps(state, indent=2, sort_keys=True),
    )


def standing_channels_from_env() -> set[str]:
    raw = os.environ.get("CORA_DEPOSCO_STANDING_CHANNELS", "") or ""
    return {c.strip() for c in raw.split(",") if c.strip()}


def approver_tier(channel: str) -> str:
    """'standing' or 'supervised'. Standing requires BOTH the env flag AND no
    ledger-derived demotion flag -- either alone is not enough (tested)."""
    if channel in standing_channels_from_env() and not is_demoted(channel):
        return "standing"
    return "supervised"
