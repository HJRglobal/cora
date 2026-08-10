"""Daily-briefing delivery enrollment: the state both the script and the bot share.

Background (S6 migration 1, 2026-08-09). `scripts/run_daily_briefing.py` sends
Harrison one "WOULD-BE BRIEFING" review DM per user and enrols that user only
when Harrison reacts :+1:. The reaction is resolved at the NEXT scheduled fire
(`_process_pending_reviews` calls reactions_get on each outstanding message), so
enrollment has always been a script-side, next-morning affair.

Adding Enable/Skip buttons puts a SECOND writer on that state file: the button
tap runs in the always-on bot process, the reaction resolver runs in the
scheduled script. Two processes read-modify-writing one JSON file is exactly the
shape that loses a write. So the state I/O moved HERE, and both sides import it:

  * one loader/saver (atomic tmp+replace, so a reader never sees a half-written
    file, and a crash mid-write cannot truncate the real one);
  * one in-process lock, which is what actually serialises the realistic race
    (two fast taps on the same card);
  * one enrollment mutation (`process_enrollment_tap`) that the bot calls and
    whose effect is identical to the reaction verdict the script applies.

CROSS-PROCESS RESIDUAL (accepted, documented rather than papered over): a tap
that lands DURING the ~seconds the 7:30am script holds its own in-memory copy
can still be overwritten by the script's end-of-run save. This is bounded and
self-healing -- the review message is still pending, so Harrison's tap can be
repeated, or a :+1: reaction resolves it at the next fire. A real fix is an
OS-level file lock across both processes, which is a bigger change than this
migration warrants (the script already carries its own single-instance run
lock, so the only overlap window is one short run per weekday).

D-011 intact: enrollment is Harrison-only on BOTH paths. This module never
writes canonical memory; it only records who has opted in to receiving their
own briefing.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from threading import RLock

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_PATH = _REPO_ROOT / "data" / "state" / "briefing-delivery.json"

# Button action ids. Deliberately their OWN ids rather than reusing the confirm
# card's: a briefing enrollment is not a staged write, has no stash, and is
# authorized Harrison-only rather than requester-scoped. Sharing an action id
# would let one handler's authorization model be applied to the other's payload.
ACTION_ENABLE = "cora_briefing_enable"
ACTION_SKIP = "cora_briefing_skip"

_LOCK = RLock()


def _empty_state() -> dict:
    return {"enabled": {}, "declined": {}, "pending_reviews": []}


def load_state(path: Path | None = None) -> dict:
    """Read the delivery state, normalised. A missing/corrupt file reads as empty
    (the script's long-standing behavior -- a bad file must never crash the
    morning briefing; the worst case is a user reappearing in the review batch).

    `path` exists so the SCRIPT can pass its own module constant, which its test
    suite redirects to a tmp file. The bot always uses the default. Both point at
    the same real file in production (the script's constant IS STATE_PATH)."""
    target = path or STATE_PATH
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    return {
        "enabled": dict(raw.get("enabled") or {}),
        "declined": dict(raw.get("declined") or {}),
        "pending_reviews": list(raw.get("pending_reviews") or []),
    }


def save_state(state: dict, path: Path | None = None) -> None:
    """Atomically persist the delivery state (tmp file + os.replace).

    The script used a plain write_text, which on a crash mid-write leaves a
    truncated file that then reads as empty -- silently un-enrolling everyone.
    os.replace is atomic on Windows and POSIX alike."""
    target = path or STATE_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        log.warning("Could not persist briefing delivery state: %s", exc)


def _harrison_id() -> str:
    """Resolved lazily from tool_dispatch so there is exactly one source of truth
    for who Harrison is, without this module importing tool_dispatch at import
    time (it is imported by a script that must stay light)."""
    from .tools.tool_dispatch import _HARRISON_SLACK_ID
    return _HARRISON_SLACK_ID


# Slack's section-text hard limit is 3000 chars; leave headroom for the mrkdwn
# wrapper. A review message carries a full plate (role, tasks, calendar,
# pipeline, decisions, recent activity) and routinely runs longer than one
# section, so the body is CHUNKED across sections rather than truncated --
# posting blocks makes `text=` a notification fallback only, so a capped single
# section would silently shorten what Harrison actually reviews.
_SECTION_CHARS = 2900
_MAX_BODY_BLOCKS = 40  # Slack allows 50 blocks; leave room for the actions block


def _chunk_for_sections(text: str) -> list[str]:
    """Split on line boundaries into <=_SECTION_CHARS pieces (hard-split only a
    single line that is itself too long)."""
    chunks: list[str] = []
    current = ""
    for line in (text or "").split("\n"):
        while len(line) > _SECTION_CHARS:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:_SECTION_CHARS])
            line = line[_SECTION_CHARS:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > _SECTION_CHARS:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [""]


def build_review_blocks(body_text: str, review_id: str) -> list[dict]:
    """(blocks) for one would-be-briefing review message: the body + Enable/Skip.

    Button values are the OPAQUE review_id, never the target user's Slack id --
    the id resolves server-side to the pending-review entry that names the user
    (design invariant #1: a button value is a handle, never a payload). A forged
    value can therefore only ever address a review that really exists, and only
    for the one person authorised to tap.

    The body is sanitize_text-wrapped at CONSTRUCTION: Block Kit bodies bypass
    the class-level WebClient egress patch, which only covers `text=` (D-168).
    """
    text = body_text or ""
    try:
        from .slack_egress import sanitize_text
        text = sanitize_text(text)
    except Exception:  # noqa: BLE001 -- sanitizer is a belt, never a blocker
        pass
    pieces = _chunk_for_sections(text)
    if len(pieces) > _MAX_BODY_BLOCKS:
        pieces = pieces[:_MAX_BODY_BLOCKS]
        pieces[-1] = pieces[-1][: _SECTION_CHARS - 40] + "\n... (truncated)"
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": p}} for p in pieces
    ]
    blocks.append({
        "type": "actions",
        "block_id": f"cora_briefing_actions_{review_id}"[:255],
        "elements": [
            {
                "type": "button",
                "action_id": ACTION_ENABLE,
                "style": "primary",
                "text": {"type": "plain_text", "text": "Enable delivery"},
                "value": review_id,
            },
            {
                "type": "button",
                "action_id": ACTION_SKIP,
                "text": {"type": "plain_text", "text": "Skip"},
                "value": review_id,
            },
        ],
    })
    return blocks


def find_pending_review(state: dict, review_id: str) -> dict | None:
    """The pending-review entry carrying this opaque review id, if any."""
    if not review_id:
        return None
    for p in state.get("pending_reviews", []):
        if str(p.get("review_id") or "") == review_id:
            return p
    return None


def process_enrollment_tap(review_id: str, actor_id: str, *, enable: bool
                           ) -> tuple[str, str]:
    """Apply an Enable/Skip button tap. Returns (outcome, message).

    Outcomes: enabled | declined | not_authorized | orphaned | already_handled.

    Authorization is Harrison-only, checked against the REAL action payload user
    (D-011): the review DM is in Harrison's own DM channel, so in practice only
    he can tap -- but authorization that relies on the surface being private is
    authorization by accident, and this handler is the only thing standing
    between a forged payload and enrolling an arbitrary teammate into daily DMs.

    Idempotent with the reaction path by CONSUMING the pending-review entry: the
    script's `_process_pending_reviews` only acts on entries still in the list,
    so a tapped user is never re-resolved by a later :+1:, and a second tap on
    the same card reads as already_handled rather than re-enrolling.
    """
    if actor_id != _harrison_id():
        return "not_authorized", "Only Harrison can change briefing delivery."

    with _LOCK:
        state = load_state()
        entry = find_pending_review(state, review_id)
        if entry is None:
            # Either a stale card from before a state reset, or the reaction
            # path already resolved this user at the last run. Both are
            # honestly "nothing left to do here" -- never a silent re-enroll.
            return "already_handled", (
                "That review was already resolved -- no change made.")

        sid = str(entry.get("sid") or "")
        name = entry.get("name") or sid
        if not sid:
            return "orphaned", "That review entry is incomplete -- no change made."

        now = time.time()
        if enable:
            state["enabled"][sid] = {
                "name": name, "enabled_at": now, "via": "digest_button",
            }
            state["declined"].pop(sid, None)
            msg = (f"Enabled -- {name} starts receiving their briefing at the "
                   f"next weekday run.")
            outcome = "enabled"
        else:
            state["declined"][sid] = {
                "name": name, "declined_at": now, "via": "digest_button",
            }
            state["enabled"].pop(sid, None)
            msg = f"Skipped -- {name} dropped from review and delivery."
            outcome = "declined"

        state["pending_reviews"] = [
            p for p in state.get("pending_reviews", [])
            if str(p.get("review_id") or "") != review_id
        ]
        save_state(state)

    log.info("briefing enrollment %s for %s via button (review=%s)",
             outcome, name, review_id)
    return outcome, msg
