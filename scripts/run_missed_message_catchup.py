#!/usr/bin/env python3
"""On-demand Missed-Message Catch-Up -- reconstruct + draft answers Cora missed while down.

Socket Mode drops events delivered while the bot is disconnected, so after an outage
Cora silently never answers anything @mentioned / DM'd / asked in a thread she was in.
This tool reconstructs that miss set from channel history over the outage window,
drafts an answer for each by WRAPPING the live answer pipeline (all guards inherited),
and -- only in live mode -- DMs Harrison one review card per message with Send/Edit/Skip
buttons. Nothing posts to any channel without Harrison's per-message tap.

DEFAULT IS DRY-RUN: it surfaces the review list and posts NOTHING (no cards, no ledger
writes). Pass --send-cards to actually DM the review cards to Harrison.

Run (from repo root):
    .venv\\Scripts\\python.exe scripts\\run_missed_message_catchup.py --auto-window
    .venv\\Scripts\\python.exe scripts\\run_missed_message_catchup.py --since 2026-07-14T20:00 --until 2026-07-15T19:07
    .venv\\Scripts\\python.exe scripts\\run_missed_message_catchup.py --auto-window --send-cards

Not a scheduled task -- fired by hand after a known outage. Script-side: generating
drafts + posting cards needs no bot restart. The Send/Edit/Skip buttons are handled by
the live bot (@app.action wiring), which needs one restart to arm.

Exit codes: 0 = ok, 1 = error / bad args, 2 = no outage window found (needs --since/--until).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_REPO_ROOT / ".env", override=True)
sys.path.insert(0, str(_REPO_ROOT / "src"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("missed_catchup")

# Highest-confidence detection tiers first when applying the surface cap.
_TIER_RANK = {"mention": 0, "dm": 1, "thread_participation": 2, "fuzzy": 3}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auto-window", action="store_true",
                        help="Derive the outage window from Cora's heartbeat gap in the logs (default when --since/--until omitted).")
    parser.add_argument("--since", type=str, default=None,
                        help="Window start (ISO-8601 or epoch). Overrides --auto-window.")
    parser.add_argument("--until", type=str, default=None,
                        help="Window end (ISO-8601 or epoch). Defaults to now if only --since given.")
    parser.add_argument("--send-cards", action="store_true",
                        help="LIVE: DM the review cards to Harrison + write pending ledger rows. Without this, dry-run (surface only, post nothing).")
    parser.add_argument("--include-fuzzy", action="store_true",
                        help="Also surface directed-at-Cora asks WITHOUT an @mention (lower confidence).")
    parser.add_argument("--no-draft", action="store_true",
                        help="Detection-only: list candidates + guard verdicts, skip answer generation (cheap, no Claude calls).")
    parser.add_argument("--no-classify", action="store_true",
                        help="Skip the Haiku still-open/resolved classifier (surface everything not already answered).")
    parser.add_argument("--max-surface", type=int, default=40,
                        help="Max candidates to draft/card this run (highest-confidence first). Default 40.")
    parser.add_argument("--channels", type=str, default=None,
                        help="Comma-separated channel NAMES to restrict to (default: all Cora is in).")
    parser.add_argument("--staleness-hours", type=float, default=24.0,
                        help="Mark asks older than this 'stale' (surfaced, not drafted). Default 24.")
    parser.add_argument("--min-gap-minutes", type=float, default=6.0,
                        help="Min heartbeat gap (minutes) that counts as an outage for --auto-window. Default 6.")
    parser.add_argument("--time-budget-min", type=float, default=20.0,
                        help="Wall-clock budget; exit cleanly when exceeded. Default 20.")
    args = parser.parse_args()

    from slack_sdk import WebClient
    from cora import missed_message_catchup as mmc

    slack_token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not slack_token:
        log.error("SLACK_BOT_TOKEN not set")
        return 1
    client = WebClient(token=slack_token)

    # ── Window ──────────────────────────────────────────────────────────────
    now = time.time()
    if args.since:
        try:
            oldest = mmc.parse_ts_arg(args.since)
            latest = mmc.parse_ts_arg(args.until) if args.until else now
        except Exception as exc:  # noqa: BLE001
            log.error("bad --since/--until: %s", exc)
            return 1
    else:
        window = mmc.derive_window(now=now, min_gap_minutes=args.min_gap_minutes)
        if window is None:
            log.error("No outage window auto-detected in the logs. Pass --since/--until.")
            return 2
        oldest, latest = window
    if latest <= oldest:
        log.error("window end (%s) <= start (%s)", latest, oldest)
        return 1
    log.info(
        "Outage window: %s  ->  %s  (%.1f min)",
        _fmt(oldest), _fmt(latest), (latest - oldest) / 60.0,
    )

    dry_run = not args.send_cards
    run_id = f"catchup-{int(now)}"
    deadline = time.monotonic() + args.time_budget_min * 60.0

    # Resolve Cora's own user id (for role-tagging + bot-authored detection).
    from cora import app as _app
    bot_id = _app._resolve_bot_user_id(client)

    # ── Enumerate + filter channels ───────────────────────────────────────────
    channels = mmc.list_catchup_channels(client)
    if args.channels:
        wanted = {c.strip().lstrip("#").lower() for c in args.channels.split(",") if c.strip()}
        channels = [c for c in channels if c["name"].lower() in wanted or (c["is_dm"] and "dm" in wanted)]
    log.info("Scanning %d channels (incl. DMs) over the window.", len(channels))

    classify_fn = None if args.no_classify else mmc.classify_still_open

    candidates = mmc.find_missed_messages(
        client, channels, oldest, latest,
        bot_id=bot_id,
        staleness_hours=args.staleness_hours,
        include_fuzzy=args.include_fuzzy,
        now=now,
        still_open_fn=classify_fn,
    )

    # Highest-confidence first, then most-recent first; apply the surface cap.
    candidates.sort(key=lambda c: (_TIER_RANK.get(c.detection_tier, 9), -_f(c.event_ts)))
    total = len(candidates)
    surfaced = candidates[: args.max_surface]
    dropped = total - len(surfaced)
    log.info("Found %d missed asks; surfacing %d (cap %d, %d over cap not surfaced).",
             total, len(surfaced), args.max_surface, max(0, dropped))

    # ── Draft + report + (live) card ──────────────────────────────────────────
    posted = 0
    counts: dict = {}
    for cand in surfaced:
        if time.monotonic() > deadline:
            log.warning("Time budget exhausted -- stopping. %d asks not processed.",
                        len(surfaced) - posted)
            break
        if cand.status != "stale":
            try:
                mmc.generate_draft(client, cand, draft_answer=not args.no_draft)
            except Exception as exc:  # noqa: BLE001
                cand.status = "error"
                cand.note = f"draft error: {exc}"
        counts[cand.status] = counts.get(cand.status, 0) + 1

        where = "DM" if cand.is_dm else f"#{cand.channel_name}"
        preview = cand.draft_text[:160].replace("\n", " ") if cand.draft_text else cand.note[:160]
        log.info("[%s] %s | %s | %s | %s",
                 cand.status.upper(), where, cand.detection_tier,
                 _fmt(_f(cand.event_ts)), preview)

        if not dry_run:
            fallback, blocks = mmc.build_review_card(cand)
            try:
                client.chat_postMessage(
                    channel=mmc.HARRISON_ID, text=fallback[:2900], blocks=blocks,
                    unfurl_links=False, unfurl_media=False,
                )
                if cand.status == "draft":
                    mmc.record_pending(cand, run_id)
                posted += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("card post failed for %s: %s", cand.catchup_id, exc)

    log.info("Summary by status: %s", counts or "(none)")
    if dropped > 0:
        log.warning("%d asks over the --max-surface cap were NOT surfaced this run.", dropped)
    if dry_run:
        log.info("DRY-RUN: no cards posted, no ledger rows written. Re-run with --send-cards to DM Harrison.")
    else:
        log.info("LIVE: posted %d review cards to Harrison. Approve/Edit/Skip each -- nothing posts without a tap.", posted)
    return 0


def _fmt(epoch: float) -> str:
    try:
        return datetime.fromtimestamp(float(epoch)).astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return str(epoch)


def _f(ts) -> float:
    try:
        return float(ts)
    except (TypeError, ValueError):
        return 0.0


if __name__ == "__main__":
    raise SystemExit(main())
