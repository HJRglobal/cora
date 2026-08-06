#!/usr/bin/env python3
"""Reconciling sweep for #info-for-cora -- intake route 3 of 3.

WHY A SWEEP EXISTS (verify-first finding 2026-08-06, cq-f1236540b61e)
---------------------------------------------------------------------
Channel `message` events do not reach the Cora app, so the D1 event-driven intake
has never once fired (full evidence in cora/info_intake.py's module docstring).
The @mention route now covers contributions that @-mention Cora. This sweep covers
the rest -- and it is the ONLY route that can ever see a post which generates no
event at all:

  * Cowork-connector posts. Harrison's 2026-07-10 F3 Pure pricing note carries
    user=U0B2RM2JYJ1, app_id=A08SF47R6P4, NO bot_id, NO subtype and NO @mention.
    It is an ordinary human contribution that produced no app_mention event and,
    with message.groups unsubscribed, no message event either. Nothing but polling
    can see it.
  * Plain un-@-mentioned statements from teammates.

Because every route derives the same deterministic infocora-{ts} update_id, this
sweep is safe to overlap with the live routes: anything already queued is a no-op
at knowledge_review.propose_update's id check.

SCOPE
-----
Top-level messages only. conversations.history returns exactly that (thread
replies live behind conversations.replies), which matches the intended scope --
threaded @mentions are already handled live by the mention route.

Excluded: anything with bot_id (Cora's own replies DO carry bot_id -- re-ingesting
them is the KB self-poisoning class, cq-8d16969e85fb), Cora's own user id, and the
join/leave/topic subtype noise.

ACK POLICY
----------
Acks post only for items this run actually QUEUED. A refusal or [QA] notice is
deliberately NOT necroposted onto a days-old message -- the live routes refuse in
the moment; the sweep is a backstop and stays quiet unless it did something.

Usage:
    python scripts/run_info_for_cora_sweep.py --dry-run     # review first
    python scripts/run_info_for_cora_sweep.py               # live
    python scripts/run_info_for_cora_sweep.py --since-days 90 --dry-run
    python scripts/run_info_for_cora_sweep.py --no-ack

Environment: SLACK_BOT_TOKEN (channels/groups:history + chat:write).
Standalone -- imports no bot-process module beyond the shared intake chokepoint,
so it activates from the working tree at its next fire with NO Cora restart.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

load_dotenv(_REPO_ROOT / ".env", override=True)

from cora import info_intake  # noqa: E402
from cora.slack_egress import sanitize_text  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("info_for_cora_sweep")

_WATERMARK_PATH = _REPO_ROOT / "data" / "state" / "info-for-cora-watermark.json"

# First run with no watermark: how far back to look. Deliberately bounded -- the
# channel has served as a Q&A surface since May and an unbounded first pass would
# re-read the whole history. Questions are skipped by the chokepoint anyway, but a
# bounded window keeps the first run reviewable.
_DEFAULT_BOOTSTRAP_DAYS = 30

# NOTE: the sweep does not carry a subtype ALLOW list -- see _eligible(), which
# rejects every subtyped message. The event path keeps its explicit list because
# it must stay behaviour-compatible with the D1 handler.


def _read_watermark() -> str:
    try:
        return str(json.loads(_WATERMARK_PATH.read_text(encoding="utf-8")).get("last_ts") or "")
    except Exception:  # noqa: BLE001 -- missing/corrupt watermark -> bootstrap window
        return ""


def _write_watermark(last_ts: str) -> None:
    """Atomic write so a kill mid-run cannot leave a truncated watermark."""
    _WATERMARK_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _WATERMARK_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"last_ts": last_ts}, indent=2), encoding="utf-8")
    tmp.replace(_WATERMARK_PATH)


def _fetch_messages(client, oldest: str, limit: int) -> list[dict[str, Any]]:
    """Top-level messages newer than `oldest`, OLDEST-FIRST so the watermark can
    advance monotonically and a mid-run failure only costs the unprocessed tail."""
    out: list[dict[str, Any]] = []
    cursor = None
    while True:
        resp = client.conversations_history(
            channel=info_intake.CHANNEL_ID, oldest=oldest, limit=200,
            inclusive=False, cursor=cursor,
        )
        out.extend(resp.get("messages") or [])
        if len(out) >= limit:
            break
        meta = resp.get("response_metadata") or {}
        cursor = meta.get("next_cursor")
        if not cursor:
            break
        time.sleep(1)  # courteous pagination
    out.sort(key=lambda m: float(m.get("ts") or 0))
    return out[:limit]


def _eligible(msg: dict[str, Any], bot_user_id: str) -> bool:
    # ANY subtype disqualifies, not just the known-noise list. A real human post
    # (and a Cowork-connector post) carries NO subtype at all -- verified on the
    # wire 2026-08-06 -- whereas Slack's channel-management messages carry a
    # varying and growing set of them. The explicit list missed
    # "made this channel private", which the first live dry-run duly queued as a
    # "fact". Allow-list the shape we want instead of chasing Slack's subtypes.
    if msg.get("bot_id") or msg.get("subtype"):
        return False
    user = msg.get("user") or ""
    if not user or (bot_user_id and user == bot_user_id):
        return False
    return bool((msg.get("text") or "").strip())


def _display_name(client, user_id: str, cache: dict[str, str]) -> str:
    if user_id in cache:
        return cache[user_id]
    name = user_id
    try:
        from cora import org_roles
        rec = org_roles.get_role(user_id)
        if rec and rec.name:
            name = rec.name
        else:
            info = client.users_info(user=user_id)
            prof = (info.get("user") or {}).get("profile") or {}
            name = prof.get("real_name") or prof.get("display_name") or user_id
    except Exception:  # noqa: BLE001 -- naming is cosmetic, never fatal
        log.debug("name lookup failed for %s", user_id, exc_info=True)
    cache[user_id] = name
    return name


def main() -> int:
    ap = argparse.ArgumentParser(description="Reconciling #info-for-cora intake sweep.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Classify and report; write nothing, post nothing.")
    ap.add_argument("--since-days", type=int, default=None,
                    help="Ignore the watermark and look back N days.")
    ap.add_argument("--max-messages", type=int, default=200,
                    help="Safety cap on messages processed in one run (default 200).")
    ap.add_argument("--no-ack", action="store_true",
                    help="Queue silently; post no threaded acks.")
    args = ap.parse_args()

    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        log.error("SLACK_BOT_TOKEN is not set -- cannot sweep.")
        return 2

    from slack_sdk import WebClient
    client = WebClient(token=token)

    bot_user_id = ""
    try:
        bot_user_id = client.auth_test().get("user_id") or ""
    except Exception:  # noqa: BLE001
        log.warning("auth.test failed; falling back to bot_id-only exclusion",
                    exc_info=True)

    if args.since_days is not None:
        oldest = f"{time.time() - args.since_days * 86400:.6f}"
        log.info("window: last %d days (watermark ignored)", args.since_days)
    else:
        oldest = _read_watermark()
        if not oldest:
            oldest = f"{time.time() - _DEFAULT_BOOTSTRAP_DAYS * 86400:.6f}"
            log.info("no watermark -- bootstrapping from the last %d days",
                     _DEFAULT_BOOTSTRAP_DAYS)
        else:
            log.info("window: since watermark ts=%s", oldest)

    try:
        messages = _fetch_messages(client, oldest, args.max_messages)
    except Exception:  # noqa: BLE001
        log.error("conversations.history failed -- aborting without advancing the "
                  "watermark", exc_info=True)
        return 1

    log.info("fetched %d top-level message(s)", len(messages))
    counts: dict[str, int] = {}
    names: dict[str, str] = {}
    high_water = ""

    for msg in messages:
        ts = str(msg.get("ts") or "")
        if not _eligible(msg, bot_user_id):
            high_water = ts or high_water
            continue
        user = msg.get("user") or ""
        result = info_intake.ingest(
            text=msg.get("text") or "",
            author_id=user,
            author_name=_display_name(client, user, names),
            ts=ts,
            route="sweep",
            dry_run=args.dry_run,
        )
        counts[result.outcome] = counts.get(result.outcome, 0) + 1
        log.info("  ts=%s user=%s -> %s%s", ts, user, result.outcome,
                 f" (entity {result.entity})" if result.stored else "")

        # Ack ONLY for what this run actually queued -- never necropost a refusal.
        if (result.stored and not args.dry_run and not args.no_ack and result.ack):
            try:
                client.chat_postMessage(
                    channel=info_intake.CHANNEL_ID,
                    text=sanitize_text(result.ack),
                    thread_ts=ts, unfurl_links=False, unfurl_media=False,
                )
            except Exception:  # noqa: BLE001 -- ack failure must not stall the sweep
                log.warning("ack post failed for ts=%s", ts, exc_info=True)

        # Advance only over messages we actually finished, so a crash re-reads the
        # tail rather than skipping it. ERROR does NOT advance (retry next run).
        if result.outcome != info_intake.ERROR:
            high_water = ts or high_water

    if args.dry_run:
        log.info("DRY-RUN -- watermark not advanced. Outcomes: %s",
                 counts or "{}")
        return 0

    if high_water:
        _write_watermark(high_water)
        log.info("watermark -> %s", high_water)
    log.info("sweep complete. Outcomes: %s", counts or "{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
