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

CROSS-PROCESS RESIDUAL (measured, not estimated). The first version of this note
claimed the exposure was "the ~seconds the 7:30am script holds its own in-memory
copy". That was wrong by two orders of magnitude and was corrected in D-051
review: the script loads state ONCE at the top of its run and saves ONCE at the
end, with every per-user LLM briefing build in between. Measured from
logs/cora-daily-briefing.jsonl across 39 live runs: median 296s, p90 358s, max
935s -- and all 39 exceeded 60s. A tap landing in that window was reverted by
the script's stale copy AFTER the card had already been edited to "Enabled ..."
with its buttons dropped, so the state recovered on the next run but the card
Harrison had acted on stayed a permanent lie.

`apply_enrollment_delta` below closes almost all of it by re-reading the file
immediately before the write, so the surviving window is that function's own
microseconds rather than the whole run. Two processes still are not mutually
exclusive -- that needs an OS-level lock across both -- but the residual is now
genuinely small rather than merely described as such, and the temp file is
process-unique so neither writer can publish the other's partial file.

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


def save_state(state: dict, path: Path | None = None) -> bool:
    """Atomically persist the delivery state (tmp file + os.replace).

    Returns True on success. The script used a plain write_text, which on a
    crash mid-write leaves a truncated file that then reads as empty -- silently
    un-enrolling everyone. os.replace is atomic on Windows and POSIX alike.

    The temp file is PROCESS-UNIQUE (D-051 lens-2/5): the bot and the scheduled
    script both write this state, and a shared fixed tmp name lets one process
    os.replace() the other's half-written file into place -- the exact torn read
    the atomic write exists to prevent."""
    target = path or STATE_PATH
    tmp = target.with_suffix(f".json.{os.getpid()}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, target)
        return True
    except OSError as exc:
        log.warning("Could not persist briefing delivery state: %s", exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def apply_enrollment_delta(sid: str, name: str, *, enable: bool,
                           review_id: str, path: Path | None = None) -> bool:
    """Re-read the state file and apply ONLY this one verdict, then save.

    D-051 lens-1/2/5 HIGH: the scheduled briefing script loads the state ONCE at
    the top of its run and saves it ONCE at the end, with every per-user LLM
    briefing build in between -- minutes, not the "~seconds" the first cut of
    this module claimed. A tap landing inside that window was silently reverted
    by the script's stale in-memory copy, AFTER the card had already been edited
    to "Enabled ..." with its buttons removed. The state recovered on the next
    run; the card Harrison had already acted on stayed a permanent lie.

    Re-reading immediately before the write shrinks the loss window from the
    whole run to this function's own microseconds, and makes the losing writer
    the SCRIPT (whose own save is a full-state overwrite) rather than the human
    verdict. It does not make the two processes mutually exclusive -- that needs
    an OS-level lock across both -- so the residual is now genuinely small
    rather than merely described as such."""
    with _LOCK:
        fresh = load_state(path)
        now = time.time()
        if enable:
            fresh["enabled"][sid] = {"name": name, "enabled_at": now,
                                     "via": "digest_button"}
            fresh["declined"].pop(sid, None)
        else:
            fresh["declined"][sid] = {"name": name, "declined_at": now,
                                      "via": "digest_button"}
            fresh["enabled"].pop(sid, None)
        fresh["pending_reviews"] = [
            p for p in fresh.get("pending_reviews", [])
            if str(p.get("review_id") or "") != review_id
        ]
        return save_state(fresh, path)


def _harrison_id() -> str:
    """Resolved lazily from tool_dispatch so there is exactly one source of truth
    for who Harrison is, without this module importing tool_dispatch at import
    time (it is imported by a script that must stay light)."""
    from .tools.tool_dispatch import _HARRISON_SLACK_ID
    return _HARRISON_SLACK_ID


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
    from . import confirm_cards as _cc
    blocks: list[dict] = list(_cc.chunk_mrkdwn_sections(text))
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

    # Re-read + apply just this verdict (see apply_enrollment_delta): the
    # scheduled script holds a whole-run-old copy of this file.
    persisted = apply_enrollment_delta(sid, name, enable=enable,
                                       review_id=review_id)
    if not persisted:
        # NEVER report success on a failed write: the caller edits the card to
        # the outcome text and drops the buttons, so an unreported failure is
        # indistinguishable from success to the only human in the loop.
        log.warning("briefing enrollment write FAILED for %s (review=%s)",
                    name, review_id)
        return "write_failed", (
            "I couldn't save that just now -- nothing changed. Try the button "
            "again in a moment, or react :+1: / :-1: instead.")

    if enable:
        outcome = "enabled"
        msg = (f"Enabled -- {name} starts receiving their briefing at the "
               f"next weekday run.")
    else:
        outcome = "declined"
        msg = f"Skipped -- {name} dropped from review and delivery."

    log.info("briefing enrollment %s for %s via button (review=%s)",
             outcome, name, review_id)
    return outcome, msg
