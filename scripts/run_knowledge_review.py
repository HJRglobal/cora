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
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cora.knowledge_review import (  # noqa: E402
    correlate_reactions_to_updates,
    get_pending_updates,
    propose_update,
    resolve_update,
    send_individual_dms,
    HARRISON_SLACK_USER_ID,
    UPDATE_TYPE_GENERIC,
)

LOG_DIR = Path(__file__).resolve().parents[1] / "logs"


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
    args = parser.parse_args()

    _setup_logging()
    log = logging.getLogger("knowledge-review")
    log.info("=" * 60)
    log.info("Knowledge review run starting (dry_run=%s)", args.dry_run)

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
        cutoff = now - _td(hours=48)
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
            log.info("Auto-dismissed %d stale entries (DM'd >48h ago, no reaction)", auto_dismissed)

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
