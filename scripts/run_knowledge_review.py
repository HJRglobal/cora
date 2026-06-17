#!/usr/bin/env python3
"""Mon-Fri 7am AZ — knowledge-review DM and reaction-processing run.

Two jobs in one run:

1. PROCESS REACTIONS: Read cora-reply-log.jsonl, correlate Harrison reactions
   to pending entries in cora-proposed-memory-updates.jsonl, resolve state
   (APPROVED / DISMISSED), and log outcomes. APPROVED items are printed to
   stdout for downstream executors to act on (Component 3 reconciliation_engine
   calls this and handles the action dispatch).

2. SEND DM BATCH: If any updates remain PENDING (no reaction yet), DM Harrison
   a formatted batch summary with 👍/👎 instructions.

Scheduled as: cowork-cora-knowledge-review  Mon-Fri 7am AZ

Exit codes:
    0 = success (ran cleanly)
    1 = fatal error
    2 = partial — DM send failed or no SLACK_BOT_TOKEN
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cora.knowledge_review import (  # noqa: E402
    correlate_reactions_to_updates,
    get_pending_updates,
    propose_update,
    resolve_update,
    send_dm_to_harrison,
    send_individual_dms,
    HARRISON_SLACK_USER_ID,
    UPDATE_TYPE_GENERIC,
)

LOG_DIR = Path(__file__).resolve().parents[1] / "logs"

# Single-instance run lock (audit N2): the pending-DM batch posted 3x at
# 11:51/11:53/11:53 when invocations overlapped before dm_message_ts was
# patched. A best-effort lockfile makes a concurrent invocation a no-op.
_LOCK_PATH = Path(__file__).resolve().parents[1] / "data" / "state" / "knowledge-review.lock"
_LOCK_STALE_SECONDS = 20 * 60

# ── Phase 2.4 rebuild knobs (gate G-D) ───────────────────────────────────────
# Auto-expire: a PENDING item Harrison has SEEN (DM'd) but not acted on for this
# many days is auto-dismissed. Relaxed from the prior 48h now that new-item DMs
# batch WEEKLY (a 48h kill would drop an item before its next weekly review).
_PENDING_EXPIRY_DAYS = 14

# Auto-approve: HIGH-confidence NON-canonical GENERIC updates write WITHOUT a
# Harrison 👍 (the "I told Cora and she forgot" loop, Harrison #9). Scoped to
# known-answers writes only -- design/known-answers/*.md is operational KB, NOT
# canonical memory. Canonical writes (decision_capture -> decisions.md) and
# external actions (asana_task/task_close/hubspot_note) and efficiency findings
# ALWAYS require a 👍 (D-011 intact). Capped so a backlog can't flood in one run.
_AUTO_APPROVE_TYPES = frozenset({"known_answer"})
_MAX_AUTO_APPROVE_PER_RUN = 10

# Auto-approve floor: items proposed BEFORE this timestamp are never auto-approved.
# Initialized to "now" on the first run after this feature ships, so the
# pre-existing PENDING backlog (proposed before the feature) is NOT auto-written
# on the first post-restart run -- it rides the normal review/expire flow. Only
# genuinely NEW HIGH machine-mined known-answers (proposed after the floor)
# auto-approve.
_AUTOAPPROVE_FLOOR_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "state"
    / "knowledge-review-autoapprove-floor.txt"
)

# Weekly digest: new-item DMs are sent only on this weekday (Mon=0) in AZ time.
# Reaction-processing, auto-approve, and auto-expire still run EVERY scheduled
# day so approvals are acted on promptly and the queue is maintained daily.
_DIGEST_WEEKDAY = 0  # Monday


def _is_digest_day() -> bool:
    """True if today (Arizona) is the weekly digest day.

    Arizona observes NO DST, so a fixed UTC-7 offset is correct AND robust on
    hosts without the IANA tz DB. ZoneInfo('America/Phoenix') raises
    ZoneInfoNotFoundError on this host (no tzdata), which previously fell through
    the bare except to True and silently defeated the weekly cadence. Matches the
    fixed-offset pattern in strategy_memo.py / run_due_date_escalation.py."""
    az_now = datetime.now(timezone(timedelta(hours=-7)))
    return az_now.weekday() == _DIGEST_WEEKDAY


def _autoapprove_floor() -> str:
    """ISO timestamp before which PENDING items are NEVER auto-approved.

    Initialized to 'now' on first call so the pre-existing backlog rides the
    normal review/expire flow instead of flooding the KB on the first run.
    Returns '' on any error -> the caller auto-approves NOTHING (fail-safe)."""
    try:
        if _AUTOAPPROVE_FLOOR_PATH.exists():
            return _AUTOAPPROVE_FLOOR_PATH.read_text(encoding="utf-8").strip()
        _AUTOAPPROVE_FLOOR_PATH.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat()
        _AUTOAPPROVE_FLOOR_PATH.write_text(now, encoding="utf-8")
        return now
    except Exception:
        return ""


def _auto_approve_eligible(update: dict) -> bool:
    """True if this PENDING update may be written WITHOUT a Harrison 👍 (G-D):
    a HIGH-confidence non-canonical GENERIC (known-answers) update from a TRUSTED
    automated source. Excludes answer_source=='teammate_dm' -- those carry a
    HARD-CODED confidence='HIGH' for arbitrary teammate free text (not an assessed
    HIGH), so they stay Harrison-gated. The backlog floor (proposed_at >= floor) is
    applied by the caller."""
    if update.get("state") != "PENDING":
        return False
    if update.get("update_type") not in _AUTO_APPROVE_TYPES:
        return False
    if (update.get("confidence") or "").upper() != "HIGH":
        return False
    if (update.get("payload") or {}).get("answer_source") == "teammate_dm":
        return False
    return True


def _acquire_run_lock(log: logging.Logger) -> bool:
    """Return True if this process took the run lock, False if a fresh run holds it."""
    _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        age = time.time() - _LOCK_PATH.stat().st_mtime
        if age > _LOCK_STALE_SECONDS:
            log.warning("Clearing stale knowledge-review lock (age %.0fs)", age)
            _LOCK_PATH.unlink()
    except FileNotFoundError:
        pass
    try:
        fd = os.open(str(_LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    try:
        os.write(fd, f"{os.getpid()} {datetime.now(timezone.utc).isoformat()}\n".encode("utf-8"))
    finally:
        os.close(fd)
    return True


def _release_run_lock() -> None:
    try:
        _LOCK_PATH.unlink()
    except OSError:
        pass


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = LOG_DIR / f"knowledge-review-{today}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def _post_to_slack(token: str, channel: str, text: str) -> None:
    """Post a message to a Slack channel. Silently logs on failure."""
    if not token:
        return
    try:
        from slack_sdk import WebClient as _WC
        _WC(token=token).chat_postMessage(
            channel=channel, text=text, unfurl_links=False, unfurl_media=False
        )
    except Exception as exc:
        logging.getLogger("knowledge-review").warning(
            "gap-executor: Slack post to #%s failed: %s", channel, exc
        )


def _execute_approved_update(update: dict, slack_token: str, log: logging.Logger) -> None:
    """Execute one approved gap update. Dispatches by update_type.

    asana_task     → create the task via Asana API
    task_close     → mark the task complete via Asana API
    decision       → post formatted entry to #hjrg-leadership for manual add
    hubspot_note   → post formatted note to #hjrg-leadership with deal link
    generic        → post description to #hjrg-leadership
    """
    import json
    update_type = update.get("update_type", "generic")
    payload = update.get("payload") or {}
    desc = update.get("description", "")
    uid_short = update.get("update_id", "?")[:8]
    notify_ch = "hjrg-leadership"

    try:
        if update_type == "asana_task":
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
            from cora.tools.asana_client import create_task, AsanaClientError
            task_name = (payload.get("suggested_task_name") or desc)[:150].strip()
            notes = (
                f"Auto-created from Cora reconciliation gap.\n\n"
                f"Evidence: {update.get('source_evidence', '')[:400]}"
            )
            try:
                task = create_task(name=task_name, notes=notes)
                url = task.get("permalink_url", "")
                msg = f":white_check_mark: *Gap executor* created Asana task: <{url}|{task_name}> `[{uid_short}]`"
                log.info("gap-executor: created Asana task gid=%s name=%s", task.get("gid"), task_name)
            except AsanaClientError as exc:
                msg = f":warning: *Gap executor* could not create Asana task `[{uid_short}]`: {exc}\n> {task_name}"
                log.warning("gap-executor: create_task failed: %s", exc)
            _post_to_slack(slack_token, notify_ch, msg)

        elif update_type == "task_close":
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
            from cora.tools.asana_client import complete_task, AsanaClientError
            task_gid = payload.get("task_gid", "")
            task_name = payload.get("task_name", task_gid)
            task_url = payload.get("task_url", "")
            if task_gid:
                try:
                    complete_task(task_gid)
                    link = f"<{task_url}|{task_name}>" if task_url else task_name
                    msg = f":white_check_mark: *Gap executor* marked complete: {link} `[{uid_short}]`"
                    log.info("gap-executor: completed task gid=%s", task_gid)
                except AsanaClientError as exc:
                    msg = f":warning: *Gap executor* could not close task `[{uid_short}]`: {exc}\n> {task_name}"
                    log.warning("gap-executor: complete_task failed: %s", exc)
            else:
                msg = f":warning: *Gap executor* `[{uid_short}]` task_close missing task_gid — skipped."
                log.warning("gap-executor: task_close payload has no task_gid: %s", payload)
            _post_to_slack(slack_token, notify_ch, msg)

        elif update_type == "decision_capture":
            formatted = payload.get("formatted_entry") or payload.get("decision_text") or desc
            msg = (
                f":pencil: *Gap executor* `[{uid_short}]` — add to `memory/decisions.md`:\n"
                f"```{formatted[:600]}```"
            )
            log.info("gap-executor: decision_capture posted to #%s uid=%s", notify_ch, uid_short)
            _post_to_slack(slack_token, notify_ch, msg)

        elif update_type == "known_answer":
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
            from cora.gap_autofill import apply_known_answer
            ok, summary = apply_known_answer(payload)
            q_short = (payload.get("question") or desc)[:160]
            if ok:
                msg = (
                    f":white_check_mark: *Gap executor* `[{uid_short}]` learned a new answer "
                    f"({summary}):\n> Q: {q_short}\n> A: {(payload.get('answer') or '')[:300]}"
                )
                log.info("gap-executor: known_answer applied uid=%s", uid_short)
            else:
                msg = f":warning: *Gap executor* `[{uid_short}]` known_answer failed: {summary}"
                log.warning("gap-executor: known_answer failed uid=%s: %s", uid_short, summary)
            _post_to_slack(slack_token, notify_ch, msg)

        elif update_type == "efficiency":
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
            from cora.friction_mining import apply_efficiency
            ok, summary = apply_efficiency(payload)
            title = (payload.get("title") or desc)[:160]
            if ok:
                msg = (
                    f":bulb: *Gap executor* `[{uid_short}]` efficiency finding approved "
                    f"({summary}):\n> {title}\n"
                    f"> Route: {payload.get('route', '?')} | {payload.get('frequency', '')}"
                )
                log.info("gap-executor: efficiency applied uid=%s", uid_short)
            else:
                msg = f":warning: *Gap executor* `[{uid_short}]` efficiency apply failed: {summary}"
                log.warning("gap-executor: efficiency failed uid=%s: %s", uid_short, summary)
            _post_to_slack(slack_token, notify_ch, msg)

        elif update_type == "hubspot_note":
            deal_name = payload.get("deal_name", "(unknown deal)")
            deal_url = payload.get("deal_url", "")
            note_text = payload.get("note") or desc
            link = f"<{deal_url}|{deal_name}>" if deal_url else deal_name
            msg = (
                f":pencil: *Gap executor* `[{uid_short}]` — add HubSpot note to {link}:\n"
                f"> {note_text[:400]}"
            )
            log.info("gap-executor: hubspot_note posted to #%s uid=%s", notify_ch, uid_short)
            _post_to_slack(slack_token, notify_ch, msg)

        else:
            msg = f":information_source: *Gap executor* `[{uid_short}]` ({update_type}): {desc[:300]}"
            log.info("gap-executor: generic action posted uid=%s", uid_short)
            _post_to_slack(slack_token, notify_ch, msg)

    except Exception as exc:
        log.error("gap-executor: unexpected error for update %s: %s", uid_short, exc, exc_info=True)


def _auto_dismiss_stale_pending(entries: list, cutoff_dt, now_dt) -> int:
    """Flip to DISMISSED, in place, only PENDING entries that have ALREADY been
    DM'd to Harrison (dm_message_ts set) and left unreacted past cutoff_dt.
    Returns the count dismissed.

    A never-DM'd PENDING entry is intentionally left alone -- Harrison has not
    seen it yet (Step 2 DMs it this run). Dismissing un-shown entries on age
    alone silently drops a contribution posted right before a >48h gap (e.g. an
    #info-for-cora note Friday evening whose next review is Monday 7am)."""
    from datetime import datetime as _dt
    n = 0
    for e in entries:
        if e.get("state") == "PENDING" and e.get("dm_message_ts"):
            try:
                if _dt.fromisoformat(e["proposed_at"]) < cutoff_dt:
                    e["state"] = "DISMISSED"
                    e["resolved_at"] = now_dt.isoformat()
                    e["resolved_reason"] = "auto_expired_dmd_unreacted"
                    n += 1
            except Exception:
                pass
    return n


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would happen without sending DMs or writing state changes",
    )
    parser.add_argument(
        "--reset-dm-ts", action="store_true",
        help="Clear dm_message_ts on all PENDING items so they get re-sent as individual DMs",
    )
    parser.add_argument(
        "--force-digest", action="store_true",
        help="Send the new-item DM digest regardless of the weekly digest weekday",
    )
    args = parser.parse_args()

    _setup_logging()
    log = logging.getLogger("knowledge-review")
    log.info("=" * 60)
    log.info("Knowledge review run starting (dry_run=%s)", args.dry_run)

    # N2 race guard: refuse to run if another invocation is already in flight,
    # so the same PENDING batch can't be DM'd two or three times in a row.
    if not args.dry_run:
        if not _acquire_run_lock(log):
            log.warning("Another knowledge-review run holds the lock — skipping this invocation.")
            return 0
        import atexit
        atexit.register(_release_run_lock)

    exit_code = 0

    # ─── Optional: reset dm_message_ts so items get re-sent individually ─────
    if args.reset_dm_ts:
        _reset_all_dm_ts()
        log.info("Reset dm_message_ts on all PENDING items — they will be re-sent individually")

    # ─── Step 0: Auto-dismiss stale entries Harrison has SEEN but not acted on ─
    # Only entries already DM'd (dm_message_ts set) and left unreacted past 48h
    # are dismissed. A never-DM'd PENDING entry is NOT dismissed here -- Step 2
    # DMs it this run. Otherwise a fact posted right before a >48h gap (e.g. an
    # #info-for-cora note Friday evening, next review Monday 7am) would be
    # silently dropped before Harrison ever saw it.
    if not args.dry_run:
        import json as _json
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        from cora.knowledge_review import _PROPOSED_UPDATES_PATH, _UPDATES_LOCK
        now = _dt.now(_tz.utc)
        cutoff = now - _td(days=_PENDING_EXPIRY_DAYS)
        auto_dismissed = 0
        if _PROPOSED_UPDATES_PATH.exists():
            with _UPDATES_LOCK:
                raw = _PROPOSED_UPDATES_PATH.read_text(encoding="utf-8")
                entries = [_json.loads(l) for l in raw.splitlines() if l.strip()]
                auto_dismissed = _auto_dismiss_stale_pending(entries, cutoff, now)
                _PROPOSED_UPDATES_PATH.write_text(
                    "\n".join(_json.dumps(e) for e in entries) + "\n",
                    encoding="utf-8",
                )
        if auto_dismissed:
            log.info("Auto-dismissed %d stale entries (DM'd >%dd ago, no reaction)",
                     auto_dismissed, _PENDING_EXPIRY_DAYS)

    # ─── Step 1: Process any reactions Harrison has already made ─────────────
    pairs = correlate_reactions_to_updates()
    log.info("Found %d reaction-to-update correlations to process", len(pairs))

    approved_updates = []
    dismissed_updates = []

    for update, reaction in pairs:
        uid = update["update_id"]
        action = reaction["action"]
        log.info(
            "Resolving update_id=%s (%s) -> %s",
            uid[:8], update.get("update_type"), action,
        )
        if not args.dry_run:
            resolve_update(uid, action)

        if action == "APPROVED":
            approved_updates.append(update)
        elif action == "DISMISSED":
            dismissed_updates.append(update)

    if approved_updates:
        log.info("APPROVED %d updates — executing now:", len(approved_updates))
        slack_token = os.environ.get("SLACK_BOT_TOKEN", "")
        for u in approved_updates:
            log.info("  [%s] %s — %s", u["update_type"], u["update_id"][:8], u["description"][:120])
            _execute_approved_update(u, slack_token, log)

    if dismissed_updates:
        log.info("DISMISSED %d updates (no action taken)", len(dismissed_updates))

    # ─── Step 1.5: Auto-approve HIGH-confidence non-canonical GENERIC updates ─
    # (gate G-D, Harrison #9.) HIGH-confidence known-answers writes persist
    # WITHOUT a 👍 -- closes the "I told Cora and she forgot" loop. Canonical
    # writes (decision_capture) + external actions + efficiency findings always
    # require a reaction. Capped per run so a backlog can't flood. Runs daily.
    auto_approved = 0
    if not args.dry_run:
        slack_token = os.environ.get("SLACK_BOT_TOKEN", "")
        floor = _autoapprove_floor()  # excludes the pre-existing backlog; "" -> none
        eligible = [
            u for u in get_pending_updates()
            if _auto_approve_eligible(u) and floor and u.get("proposed_at", "") >= floor
        ]
        if len(eligible) > _MAX_AUTO_APPROVE_PER_RUN:
            log.info("Capping auto-approve: %d eligible -> top %d this run",
                     len(eligible), _MAX_AUTO_APPROVE_PER_RUN)
            eligible = eligible[:_MAX_AUTO_APPROVE_PER_RUN]
        for u in eligible:
            uid = u["update_id"]
            log.info("AUTO-APPROVE [%s] %s (HIGH non-canonical) — %s",
                     u.get("update_type"), uid[:8], u.get("description", "")[:120])
            _execute_approved_update(u, slack_token, log)
            resolve_update(uid, "APPROVED", reason="auto_approved_high_generic")
            auto_approved += 1
        if auto_approved:
            log.info("Auto-approved %d HIGH-confidence non-canonical update(s)", auto_approved)

    # ─── Step 2: Send DM batch for any still-PENDING updates ─────────────────
    # Cap at 5 DMs per run — keeps Harrison's review queue manageable.
    _MAX_DMS_PER_RUN = 5
    pending = get_pending_updates()
    unsent = [u for u in pending if not u.get("dm_message_ts")]
    if len(unsent) > _MAX_DMS_PER_RUN:
        log.info("Capping DM batch: %d unsent -> sending top %d (HIGH confidence first)",
                 len(unsent), _MAX_DMS_PER_RUN)
        unsent = sorted(unsent, key=lambda u: 0 if u.get("confidence") == "HIGH" else 1)
        unsent = unsent[:_MAX_DMS_PER_RUN]
    log.info("Found %d PENDING updates to DM Harrison about (%d not yet sent)", len(pending), len(unsent))

    if not unsent:
        log.info("All pending updates already have DM timestamps — nothing new to send")
        return exit_code

    slack_token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not slack_token or args.dry_run:
        if args.dry_run:
            for i, u in enumerate(unsent, 1):
                log.info("[DRY RUN] Item %d/%d: %s", i, len(unsent), u.get("description", "?")[:120])
        else:
            log.warning("SLACK_BOT_TOKEN not set — cannot send DM, exit_code=2")
            exit_code = 2
        return exit_code

    # Weekly digest cadence: only send NEW-item DMs on the digest weekday (reaction
    # processing, auto-approve, and auto-expire above already ran). --force-digest
    # overrides for a manual/ad-hoc send.
    if not (_is_digest_day() or args.force_digest):
        log.info(
            "Not the weekly digest day — %d new item(s) deferred to the next digest",
            len(unsent),
        )
        return exit_code

    # Digest header so Harrison sees the batch as one weekly review, then the
    # individually-reactable cards.
    send_dm_to_harrison(
        f"Cora weekly knowledge review: {len(unsent)} item(s) below. "
        f"React 👍 to approve or 👎 to dismiss each. "
        f"Un-actioned items auto-expire in {_PENDING_EXPIRY_DAYS} days.",
        slack_token,
    )

    log.info("Sending %d individual DMs to Harrison (user=%s)...", len(unsent), HARRISON_SLACK_USER_ID)
    sent_map = send_individual_dms(unsent, slack_token)  # {update_id: ts}

    if sent_map:
        log.info("Sent %d/%d DMs successfully", len(sent_map), len(unsent))
        for update in unsent:
            ts = sent_map.get(update["update_id"])
            if ts:
                _patch_dm_ts(update["update_id"], ts)
        log.info("Patched dm_message_ts on %d entries", len(sent_map))
    else:
        log.warning("No DMs were sent — check SLACK_BOT_TOKEN and im:write scope")
        exit_code = 2

    log.info(
        "Knowledge review complete — approved=%d dismissed=%d pending=%d (exit=%d)",
        len(approved_updates), len(dismissed_updates), len(pending), exit_code,
    )
    return exit_code


def _patch_dm_ts(update_id: str, dm_ts: str) -> None:
    """Patch dm_message_ts on a proposed-update entry in-place (atomic rewrite)."""
    import json
    from cora.knowledge_review import _PROPOSED_UPDATES_PATH, _UPDATES_LOCK

    if not _PROPOSED_UPDATES_PATH.exists():
        return

    with _UPDATES_LOCK:
        entries = []
        with _PROPOSED_UPDATES_PATH.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("update_id") == update_id and not entry.get("dm_message_ts"):
                    entry["dm_message_ts"] = dm_ts
                entries.append(entry)

        tmp = _PROPOSED_UPDATES_PATH.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        tmp.replace(_PROPOSED_UPDATES_PATH)


def _reset_all_dm_ts() -> int:
    """Clear dm_message_ts on all PENDING items so they get re-sent as individual DMs."""
    import json
    from cora.knowledge_review import _PROPOSED_UPDATES_PATH, _UPDATES_LOCK

    if not _PROPOSED_UPDATES_PATH.exists():
        return 0

    count = 0
    with _UPDATES_LOCK:
        entries = []
        with _PROPOSED_UPDATES_PATH.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("state") == "PENDING" and entry.get("dm_message_ts"):
                    entry["dm_message_ts"] = ""
                    entry["dm_channel_id"] = ""
                    count += 1
                entries.append(entry)

        tmp = _PROPOSED_UPDATES_PATH.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        tmp.replace(_PROPOSED_UPDATES_PATH)

    return count


if __name__ == "__main__":
    sys.exit(main())
