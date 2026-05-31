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
        log.info("APPROVED %d updates:", len(approved_updates))
        for u in approved_updates:
            log.info("  [%s] %s — %s", u["update_type"], u["update_id"][:8], u["description"][:120])
            # Print JSON line to stdout so downstream scripts/orchestrators can pick up
            import json
            print("APPROVED:" + json.dumps({
                "update_id": u["update_id"],
                "update_type": u["update_type"],
                "payload": u["payload"],
                "description": u["description"],
            }))

    if dismissed_updates:
        log.info("DISMISSED %d updates (no action taken)", len(dismissed_updates))

    # ─── Step 2: Send DM batch for any still-PENDING updates ─────────────────
    pending = get_pending_updates()
    unsent = [u for u in pending if not u.get("dm_message_ts")]
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
