"""Slack interactive Confirm/Cancel + candidate-picker buttons over the existing
F-23 staged-write stashes (v1, design doc 2026-08-02).

One shared component: every staged-write preview, across all 9 stash kinds
(asana, shopify, calendar, lexicon, code_queue, delegated, remember,
forget_note, schedule_meeting), gets an opaque stash_id at preview time. A
button tap carries ONLY that id -- never a payload, never model-echoed text --
and the tap handler in app.py resolves it back to (kind, user, channel) via the
index here, then re-verifies against the LIVE store before acting (a stash_id
that no longer matches its store's current entry is honestly "superseded").

CORA_CONFIRM_BUTTONS=off|on -- off is the full kill switch for BUTTONS only:
previews stay byte-identical text-only. Typed "@Cora confirm" is unaffected
either way; this flag controls whether Slack Block Kit buttons render
alongside the preview text.

Deliberately pure: this module holds no per-kind store logic (those live next
to their existing pending stores in tool_dispatch.py) and never imports
tool_dispatch, so there is no circular-import risk. It knows how to mint /
index / render -- nothing about what a stash_id actually authorizes.
"""

from __future__ import annotations

import os
import secrets
import time
from threading import Lock

ACTION_CONFIRM = "cora_confirm_write"
ACTION_CANCEL = "cora_cancel_write"
ACTION_PICK = "cora_pick_candidate"

# Matches every existing pending store's TTL (asana/shopify/calendar/lexicon/
# code_queue/delegated all use 600s independently -- kept as one named constant
# here for the 3 NEW Class-B stashes + the ask-stash so they match by construction).
STASH_TTL_SECONDS = 600

# How long a claimed/expired stash_id still resolves via the index to an honest
# "superseded/expired" reply instead of a bare "orphaned" one, mirroring the
# Shopify tombstone precedent (cq-ed29165fca97, 6h). Purely a memory/UX nicety --
# an index-absent id is ALWAYS treated as orphaned regardless of cause (forged,
# never-existed, pruned, or bot-restart-cleared collapse to the same honest
# reply; no distinction is ever leaked to the tapper).
INDEX_GRACE_SECONDS = 6 * 3600

_INDEX_LOCK = Lock()
# stash_id -> {"kind": str, "user": str, "channel": str, "ts": float}
_INDEX: dict[str, dict] = {}

# ask_id -> {"user": str, "channel": str, "ts": float} (ask-stash owner index;
# separate namespace from confirm/cancel stash ids -- a picker answers a
# question, it never itself authorizes a write).
_ASK_INDEX_LOCK = Lock()
_ASK_INDEX: dict[str, dict] = {}


def confirm_buttons_enabled() -> bool:
    """off (default) | on. Same idiom as CORA_SEND_LIVE/CORA_LEXICON/etc: read
    per-call, whitelist-validated, fail-closed to off on anything unrecognized.
    The always-on bot process snapshots .env at start -- flipping this for the
    LIVE bot needs a restart (documented kill switch)."""
    v = (os.environ.get("CORA_CONFIRM_BUTTONS", "off") or "off").strip().lower()
    return v == "on"


def _prune_locked(index: dict[str, dict]) -> None:
    now = time.time()
    dead = [k for k, e in index.items() if now - e.get("ts", 0) > INDEX_GRACE_SECONDS]
    for k in dead:
        index.pop(k, None)


def mint_stash_id(kind: str, user: str, channel: str) -> str:
    """Mint a fresh opaque id and index (kind, user, channel) under it. Called by
    each store's writer at preview time (Class A) or stash time (Class B/ask)."""
    with _INDEX_LOCK:
        _prune_locked(_INDEX)
        sid = secrets.token_hex(8)
        while sid in _INDEX:  # astronomically unlikely; cheap to guard
            sid = secrets.token_hex(8)
        _INDEX[sid] = {"kind": kind, "user": user, "channel": channel, "ts": time.time()}
    return sid


def index_lookup(stash_id: str) -> dict | None:
    """Non-destructive: returns a COPY of the index entry, or None if unknown/expired-out."""
    with _INDEX_LOCK:
        entry = _INDEX.get(stash_id)
        return dict(entry) if entry else None


def index_release(stash_id: str) -> None:
    """Best-effort hygiene: drop the index entry once its stash is claimed or
    cancelled. Not required for correctness (the grace-window prune is the
    backstop for entries nobody ever taps)."""
    with _INDEX_LOCK:
        _INDEX.pop(stash_id, None)


def mint_ask_id(user: str, channel: str) -> str:
    with _ASK_INDEX_LOCK:
        _prune_locked(_ASK_INDEX)
        aid = secrets.token_hex(8)
        while aid in _ASK_INDEX:
            aid = secrets.token_hex(8)
        _ASK_INDEX[aid] = {"user": user, "channel": channel, "ts": time.time()}
    return aid


def ask_index_lookup(ask_id: str) -> dict | None:
    with _ASK_INDEX_LOCK:
        entry = _ASK_INDEX.get(ask_id)
        return dict(entry) if entry else None


def ask_index_release(ask_id: str) -> None:
    with _ASK_INDEX_LOCK:
        _ASK_INDEX.pop(ask_id, None)


def build_confirm_blocks(preview_text: str, stash_id: str) -> list[dict]:
    """The shared card: existing preview text verbatim + Confirm/Cancel, both
    button values = stash_id ONLY (invariant #1 -- no payload, no echo)."""
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": preview_text}},
        {
            "type": "actions",
            "block_id": f"cora_confirm_actions_{stash_id}",
            "elements": [
                {
                    "type": "button",
                    "action_id": ACTION_CONFIRM,
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Confirm"},
                    "value": stash_id,
                },
                {
                    "type": "button",
                    "action_id": ACTION_CANCEL,
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "Cancel"},
                    "value": stash_id,
                },
            ],
        },
    ]


_BTN_LABEL_MAX = 75  # Slack plain_text button label limit


def build_picker_blocks(prompt_text: str, ask_id: str, candidates: list[tuple[str, str]]) -> list[dict]:
    """Candidate-picker card. `candidates` is [(candidate_key, label), ...],
    already capped to <=5 by the caller (design doc 4.2 -- beyond 5, fall back
    to the existing text ask). value = "{ask_id}:{candidate_key}" (the one
    exception to stash_id-only values -- an ask answers a question, it does
    not itself authorize a write)."""
    elements = []
    for key, label in candidates:
        btn_label = label if len(label) <= _BTN_LABEL_MAX else label[: _BTN_LABEL_MAX - 3] + "..."
        elements.append({
            "type": "button",
            "action_id": ACTION_PICK,
            "text": {"type": "plain_text", "text": btn_label},
            "value": f"{ask_id}:{key}",
        })
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": prompt_text}},
        {"type": "actions", "block_id": f"cora_pick_actions_{ask_id}", "elements": elements},
    ]


def terminal_blocks(outcome_text: str) -> list[dict]:
    """A resolved card: outcome text, no actions block (buttons dropped)."""
    return [{"type": "section", "text": {"type": "mrkdwn", "text": outcome_text}}]
