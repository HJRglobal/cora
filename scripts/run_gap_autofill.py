#!/usr/bin/env python3
"""Daily 6:00am AZ -- knowledge-gap autofill run (mine Slack KB, escalate to owners).

Pipeline per open gap (logs/knowledge-gaps.jsonl minus resolved minus
already-handled):

  1. MINE -- entity-scoped KB search over swept Slack conversation chunks
     (source="slack"), Haiku drafts an answer (fail-closed). Confident drafts
     are proposed via knowledge_review -> Harrison's 7am thumbs-up DM.
  2. ASK -- if no evidence and the gap is older than ESCALATE_AFTER_HOURS,
     DM the entity domain owner (data/maps/gap-domain-owners.yaml) once.
     Replies are captured by app.py and routed through the same gate.
  3. Otherwise leave the gap open for the next run / the weekly digest.

Schedule order matters: runs AFTER the 2am Slack KB sync and BEFORE the 7am
knowledge-review DM batch, so fresh conversation data is searchable and new
proposals ride the same morning DM.

Scheduled as: cowork-cora-gap-autofill  Daily 6:00am AZ
Register with: deployment\\setup-gap-autofill-task.ps1 (elevated PowerShell)

Usage:
    .venv\\Scripts\\python.exe scripts\\run_gap_autofill.py [--dry-run]
        [--max-gaps N] [--max-asks N] [--no-escalate]

Exit codes: 0 = clean, 1 = fatal error
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_REPO_ROOT / ".env", override=True)

sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import gap_autofill as ga  # noqa: E402

LOG_DIR = _REPO_ROOT / "logs"
DEFAULT_MAX_GAPS = 10


def _setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"gap-autofill-{datetime.now().strftime('%Y-%m-%d')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger("gap-autofill")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Knowledge-gap autofill run.")
    p.add_argument("--dry-run", action="store_true",
                   help="Report what would happen; no proposals, DMs, or state writes.")
    p.add_argument("--max-gaps", type=int, default=DEFAULT_MAX_GAPS,
                   help=f"Max gaps to process this run (default {DEFAULT_MAX_GAPS}).")
    p.add_argument("--max-asks", type=int, default=ga.MAX_ASKS_PER_RUN,
                   help=f"Max escalation DMs this run (default {ga.MAX_ASKS_PER_RUN}).")
    p.add_argument("--no-escalate", action="store_true",
                   help="Stage 1 mining only; never send escalation DMs.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    log = _setup_logging()
    log.info("=" * 60)
    log.info("Gap autofill run starting (dry_run=%s)", args.dry_run)

    gaps = ga.load_open_gaps()
    log.info("Open gaps: %d (processing up to %d)", len(gaps), args.max_gaps)
    if not gaps:
        log.info("Nothing to do.")
        return 0

    # Open the KB once for the whole run.
    kb = None
    try:
        from cora.knowledge_base.store import KnowledgeBase
        kb = KnowledgeBase(_REPO_ROOT / "data" / "cora_kb.db")
    except Exception as exc:  # noqa: BLE001
        log.warning("KB unavailable (%s) -- Stage 1 mining disabled this run", exc)

    slack_client = None
    if not args.no_escalate and not args.dry_run:
        token = os.environ.get("SLACK_BOT_TOKEN", "")
        if token:
            try:
                from slack_sdk import WebClient
                slack_client = WebClient(token=token)
            except Exception as exc:  # noqa: BLE001
                log.warning("Slack client init failed (%s) -- escalation disabled", exc)
        else:
            log.warning("SLACK_BOT_TOKEN not set -- escalation disabled")

    state = ga.load_state()
    n_mined = n_asked = n_left = 0
    asks_sent = 0

    for gap in gaps[: args.max_gaps]:
        gap_ts = gap.get("ts", "?")
        entity = gap.get("entity", "FNDR")

        # -- Stage 1: mine swept Slack conversations --------------------------
        evidence = ga.search_slack_evidence(kb, gap) if kb else []
        draft = ga.draft_answer(gap, evidence) if evidence else None

        if draft:
            if args.dry_run:
                log.info("[DRY] would propose (%s, %s): Q=%r A=%r",
                         entity, draft["confidence"],
                         gap.get("question", "")[:80], draft["answer"][:120])
            else:
                update_id = ga.propose_known_answer(
                    gap, draft["answer"],
                    confidence=draft["confidence"],
                    answer_source="slack_kb",
                    citation=draft["citation"],
                )
                state[gap_ts] = {"state": "proposed", "via": "slack_kb",
                                 "update_id": update_id, "at": ga._now_iso()}
                ga.save_state(state)
                log.info("Proposed answer for gap %s (%s, %s) update=%s",
                         gap_ts, entity, draft["confidence"], update_id)
            n_mined += 1
            continue

        # -- Stage 2: escalate to the domain owner ----------------------------
        if (not args.no_escalate and asks_sent < args.max_asks
                and ga.should_escalate(gap)):
            if args.dry_run:
                owner = ga.resolve_owner(entity)
                log.info("[DRY] would escalate gap %s (%s) to owner %s",
                         gap_ts, entity, owner or "(none)")
                n_asked += 1
                asks_sent += 1
                continue
            if slack_client is not None:
                ask = ga.escalate_gap(gap, slack_client)
                if ask:
                    state[gap_ts] = {"state": "asked", "ask_id": ask["ask_id"],
                                     "at": ga._now_iso()}
                    ga.save_state(state)
                    n_asked += 1
                    asks_sent += 1
                    continue

        n_left += 1
        log.info("Gap %s (%s) left open -- evidence=%d, age=%.0fh",
                 gap_ts, entity, len(evidence), ga.gap_age_hours(gap))

    log.info("Run complete: %d mined+proposed, %d escalated, %d left open",
             n_mined, n_asked, n_left)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        logging.getLogger("gap-autofill").error("Fatal error", exc_info=True)
        sys.exit(1)
