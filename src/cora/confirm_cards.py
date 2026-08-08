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

import contextvars
import os
import secrets
import time
from threading import Lock

ACTION_CONFIRM = "cora_confirm_write"
ACTION_CANCEL = "cora_cancel_write"
ACTION_PICK = "cora_pick_candidate"
# One button per OFFERED meeting slot (v2 S2). Deliberately its own action id
# rather than reusing ACTION_PICK: a candidate pick answers an ambiguity ask
# (value = ask_id:key, resolved against the ask store), whereas a slot pick
# CONFIRMS a staged write (value = stash_id:slot_index, resolved against the
# schedule_meeting stash). Same-looking values, completely different authority.
ACTION_PICK_SLOT = "cora_pick_slot"

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
# stash_id -> {"kind": str, "user": str, "channel": str, "ts": float, "turn_id": str | None}
_INDEX: dict[str, dict] = {}

# ask_id -> {"user": str, "channel": str, "ts": float, "turn_id": str | None} (ask-stash
# owner index; separate namespace from confirm/cancel stash ids -- a picker answers a
# question, it never itself authorizes a write).
_ASK_INDEX_LOCK = Lock()
_ASK_INDEX: dict[str, dict] = {}

# ── Turn-scoped provenance (S1 fix, live-smoke 2026-08-02) ──────────────────
# app._dispatch_qa calls begin_turn() once, before the confirm interceptor and
# before the model's tool loop. Every stash minted from then on -- on the SAME
# logical call chain (this thread, or a context explicitly copied via
# contextvars.copy_context(), which claude_client._dispatch_tools_parallel does
# for each parallel tool call) -- is tagged with that turn's id. This lets
# tool_dispatch.freshest_changed_stash() tell "a stash THIS turn's own tool
# call minted" apart from "some stash changed in the shared (user, channel)
# store while my turn was in flight" -- the latter is a CONCURRENT turn's own
# mint under overlapping snapshot/diff windows (multiple @mentions within a
# couple seconds all landed on the SAME (user, channel), each triggering a
# different staged-write kind), which the old marker-free ts-tiebreak could
# cross-bind to the wrong reply's card. A stash minted with no active turn
# (turn_id=None -- e.g. a unit test that calls mint_stash_id directly) never
# matches any turn's ownership check by construction (None is never treated
# as equal to another None "current turn" -- see current_turn_id()'s callers).
_TURN_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "cora_confirm_turn_id", default=None,
)


def begin_turn() -> None:
    """Mark the start of a fresh turn's confirm-card provenance scope. Cheap
    (one token_hex mint); call unconditionally at the top of _dispatch_qa."""
    _TURN_ID.set(secrets.token_hex(8))


def current_turn_id() -> str | None:
    """The active turn's id, or None outside any turn scope."""
    return _TURN_ID.get()


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
        _INDEX[sid] = {"kind": kind, "user": user, "channel": channel, "ts": time.time(),
                       "turn_id": _TURN_ID.get()}
    return sid


def index_lookup(stash_id: str) -> dict | None:
    """Non-destructive: returns a COPY of the index entry, or None if unknown/expired-out."""
    with _INDEX_LOCK:
        entry = _INDEX.get(stash_id)
        return dict(entry) if entry else None


def index_release(stash_id: str) -> None:
    """Best-effort hygiene: drop the index entry outright. Not required for
    correctness (the grace-window prune is the backstop for entries nobody
    ever taps). Prefer index_mark_resolved() for a stash_id that reached a
    terminal claim/cancel outcome -- a hard pop here makes a racing SECOND
    tap read as 'orphaned' (implying nothing happened, ask again) instead of
    the more accurate 'already handled' (idempotent ack)."""
    with _INDEX_LOCK:
        _INDEX.pop(stash_id, None)


def index_mark_resolved(stash_id: str) -> None:
    """Mark a stash_id resolved (claimed/cancelled) WITHOUT deleting the index
    entry, so a racing second tap -- or a tap that arrives after the user
    typed a confirm instead (the typed path never touches this index at all,
    so it can't mark anything itself; the per-kind store simply has nothing
    left, which the caller treats the same way) -- reads as 'already handled'
    rather than 'orphaned'. The entry still ages out via the normal
    grace-window prune."""
    with _INDEX_LOCK:
        entry = _INDEX.get(stash_id)
        if entry is not None:
            entry["resolved"] = True


# ── Rendered-card registry (v2 S1) ─────────────────────────────────────────
# v1 knew how to mint and index a stash_id, but never recorded WHERE a card for
# it was actually rendered. Two live consequences (cq-fee6c9764950):
#
#   1. The TYPED path (a "confirm"/"cancel" the model routes to the tool with
#      confirmed=true, or the deterministic interceptor) consumes the per-kind
#      store entry and never touches this module at all -- so the already-posted
#      card kept LIVE Confirm/Cancel buttons on a stash that no longer exists.
#      Tapping them was honest ("already handled"), but the card visibly lied.
#   2. Nothing stopped the same stash_id from being rendered as a SECOND live
#      card, so one pending write could show two apparently-actionable copies.
#
# Fixed here by making "a card exists for this stash" first-class state:
#   * claim_card_attach() is an atomic one-shot -- the SECOND attempt to card a
#     given stash_id is refused, so one stash can never have two live cards no
#     matter which reply site (or how many concurrent turns) tries;
#   * register_card() records (channel, message_ts, preview_text) after the post
#     succeeds, so a closer can rebuild the card's terminal blocks WITHOUT
#     re-fetching the message;
#   * pop_cards() hands the coordinates over exactly once, so two racing closers
#     cannot both chat_update the same message (the same "never order two
#     independent HTTP calls" constraint the terminal-edit note below describes).
#
# Entries age out on the same INDEX_GRACE_SECONDS prune as the stash index.
_CARD_LOCK = Lock()
# stash_id -> {"attached": bool, "cards": [(channel_id, message_ts, preview_text)], "ts": float}
_CARDS: dict[str, dict] = {}


def _prune_cards_locked() -> None:
    now = time.time()
    dead = [k for k, e in _CARDS.items() if now - e.get("ts", 0) > INDEX_GRACE_SECONDS]
    for k in dead:
        _CARDS.pop(k, None)


def claim_card_attach(stash_id: str) -> bool:
    """Atomically claim the right to render THE card for `stash_id`. True for the
    first caller, False for every later one -- the caller must fall back to a
    plain text reply (the typed confirm path still works). One stash, one live
    card, by construction."""
    if not stash_id:
        return False
    with _CARD_LOCK:
        _prune_cards_locked()
        entry = _CARDS.get(stash_id)
        if entry is not None and entry.get("attached"):
            return False
        _CARDS[stash_id] = {"attached": True, "cards": [], "ts": time.time()}
    return True


def register_card(stash_id: str, channel_id: str, message_ts: str, preview_text: str) -> None:
    """Record where a card for `stash_id` actually landed, once the post/update
    that carries it has succeeded. Best-effort: an unregistered card simply
    cannot be auto-closed later (it still resolves honestly when tapped)."""
    if not stash_id or not channel_id or not message_ts:
        return
    with _CARD_LOCK:
        entry = _CARDS.setdefault(
            stash_id, {"attached": True, "cards": [], "ts": time.time()})
        coords = (channel_id, message_ts, preview_text or "")
        if coords not in entry["cards"]:
            entry["cards"].append(coords)


def pop_cards(stash_id: str) -> list[tuple[str, str, str]]:
    """Take (and clear) every rendered-card coordinate for `stash_id`. The
    'attached' claim deliberately SURVIVES the pop, so a closed card can never
    be silently replaced by a second live one for the same stash."""
    with _CARD_LOCK:
        entry = _CARDS.get(stash_id)
        if not entry:
            return []
        cards = list(entry.get("cards") or [])
        entry["cards"] = []
        return cards


def open_card_stash_ids() -> list[str]:
    """Every stash_id that still has at least one rendered, un-closed card."""
    with _CARD_LOCK:
        _prune_cards_locked()
        return [k for k, e in _CARDS.items() if e.get("cards")]


def reset_cards_for_tests() -> None:
    with _CARD_LOCK:
        _CARDS.clear()


def mint_ask_id(user: str, channel: str) -> str:
    with _ASK_INDEX_LOCK:
        _prune_locked(_ASK_INDEX)
        aid = secrets.token_hex(8)
        while aid in _ASK_INDEX:
            aid = secrets.token_hex(8)
        _ASK_INDEX[aid] = {"user": user, "channel": channel, "ts": time.time(),
                          "turn_id": _TURN_ID.get()}
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


# Matches _tool_calendar_schedule_meeting's own slots[:3] cap.
MAX_SLOT_BUTTONS = 3


def build_slot_picker_blocks(preview_text: str, stash_id: str,
                             slot_labels: list[str]) -> list[dict]:
    """Meeting-proposal card: one button per OFFERED slot, plus Cancel (v2 S2).

    v1 gave schedule_meeting a single Confirm that always booked slots[0], while
    the typed path let the user pick any of the up-to-3 offered options -- so the
    button silently did something different from what the words next to it
    offered. One button per slot removes the divergence; a slot the stash never
    offered still cannot be booked, because the index is resolved against the
    stash's OWN slot list server-side (_execute_claimed_schedule_meeting's
    exact-match check is unchanged and remains the real gate)."""
    elements = []
    for idx, label in enumerate(slot_labels[:MAX_SLOT_BUTTONS]):
        btn = label if len(label) <= _BTN_LABEL_MAX else label[: _BTN_LABEL_MAX - 3] + "..."
        elements.append({
            "type": "button",
            "action_id": ACTION_PICK_SLOT,
            "style": "primary",
            "text": {"type": "plain_text", "text": btn},
            "value": f"{stash_id}:{idx}",
        })
    elements.append({
        "type": "button",
        "action_id": ACTION_CANCEL,
        "style": "danger",
        "text": {"type": "plain_text", "text": "Cancel"},
        "value": stash_id,
    })
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": preview_text}},
        {"type": "actions", "block_id": f"cora_slot_actions_{stash_id}",
         "elements": elements},
    ]


def terminal_blocks(outcome_text: str) -> list[dict]:
    """A resolved card: outcome text, no actions block (buttons dropped)."""
    return [{"type": "section", "text": {"type": "mrkdwn", "text": outcome_text}}]


# ── Terminal-edit race (S2, live-smoke 2026-08-02 + D-051 re-review) ────────
# resolve_and_claim_stash's atomic pop already guarantees at most ONE caller
# ever sees a genuine outcome for a given stash_id -- but two racing taps on
# the SAME card each independently call client.chat_update, and nothing
# orders those two HTTP round-trips. Live: a slow winner's real "Done: ..."
# edit landed first, then a fast loser's "Already handled" edit (dispatched
# on its own thread, no execute() to wait for) landed SECOND and clobbered
# it. A first attempt at a fix here used a "claim a slot, whoever writes
# first wins" registry -- REJECTED on D-051 re-review: it only blocks the
# clobber in ONE arrival order (a winner's claim already recorded before the
# loser checks). If the FASTER, non-informative "already_handled" caller
# reaches the registry first (the common case, since it has no real work to
# wait for), it still gets to fire its OWN chat_update, and the slower
# winner's edit arriving afterward provides no guarantee of being APPLIED
# last by Slack's servers -- the original clobber, just with the roles that
# happen to race swapped. There is no registry/locking scheme that can order
# two independent HTTP calls after the fact. The actual fix lives in app.py:
# an "already_handled" (or same-card-race-loser) outcome NEVER calls
# chat_update on the shared card at all -- ephemeral-only. There is always a
# legitimate winner (that is what makes an outcome "already handled" in the
# first place) who will edit the card with the real result, so nothing is
# lost by the loser leaving it alone.
