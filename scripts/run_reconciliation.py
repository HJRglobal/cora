#!/usr/bin/env python3
"""5:30am AZ daily — cross-source reconciliation sweep.

Runs after all four KB sync tasks (Slack 2am, Gmail 2:30am, Asana 3am,
Fireflies 3:30am, static_md 4am, Drive 4:30am) have completed so the KB
contains fresh content for all sources.

Pipeline:
  1. Fetch open Asana tasks (all assigned users in workspace).
  2. Fetch active HubSpot deals from F3E Retail pipeline.
  3. Call reconciliation_engine.reconcile() — runs four passes:
       Pass 1: Missing Asana tasks (action commitments without tasks)
       Pass 2: Stale HubSpot deals (deal mentions, no HubSpot activity in 7d)
       Pass 3: Uncaptured decisions (decision language not in decisions.md)
       Pass 4: Stale open tasks (completion language in Slack/Gmail)
  4. Write gaps to data/reconciliation/YYYY-MM-DD-gaps.jsonl.
  5. For each HIGH/MED gap: call knowledge_review.propose_update() so
     Harrison sees it in the Mon-Fri 7am DM review.

PHI guardrail: reconciliation_engine skips LEX chunks with PHI content.
Visibility CPA exclusion: CPA team names never appear in gap descriptions.
Harrison sole-authority doctrine: this script NEVER writes to decisions.md,
Asana, or HubSpot. It only queues proposals.

Exit codes:
    0 = success
    1 = fatal error
    2 = partial — some passes failed or KB is empty
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora.reconciliation_engine import (  # noqa: E402
    reconcile,
    ReconciliationGap,
    DEFAULT_LOOKBACK_SECONDS,
)
from cora.knowledge_review import (  # noqa: E402
    propose_update,
    UPDATE_TYPE_ASANA_TASK,
    UPDATE_TYPE_HUBSPOT_NOTE,
    UPDATE_TYPE_DECISION,
    UPDATE_TYPE_TASK_CLOSE,
)

LOG_DIR          = _REPO_ROOT / "logs"
GAPS_DIR         = _REPO_ROOT / "data" / "reconciliation"
KB_DB_PATH       = _REPO_ROOT / "data" / "cora_kb.db"

# Map gap_type -> knowledge_review update_type constant
_GAP_TYPE_TO_UPDATE_TYPE = {
    "missing_asana_task":   UPDATE_TYPE_ASANA_TASK,
    "stale_hubspot_deal":   UPDATE_TYPE_HUBSPOT_NOTE,
    "uncaptured_decision":  UPDATE_TYPE_DECISION,
    "stale_open_task":      UPDATE_TYPE_TASK_CLOSE,
}

# Only queue HIGH + MED for Harrison review
_REVIEW_CONFIDENCES = {"HIGH", "MED"}


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = LOG_DIR / f"reconciliation-{today}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def _fetch_open_tasks() -> list[dict]:
    """Fetch open Asana tasks across all workspace users.

    Uses asana_client.get_user_tasks for each known user GID from
    data/maps/slack-to-asana.yaml. Returns combined deduplicated list.
    """
    try:
        import yaml
        from cora.tools.asana_client import get_user_tasks, AsanaClientError

        maps_path = _REPO_ROOT / "data" / "maps" / "slack-to-asana.yaml"
        if not maps_path.exists():
            log = logging.getLogger("reconciliation")
            log.warning("slack-to-asana.yaml not found — using empty task list")
            return []

        with maps_path.open(encoding="utf-8") as fh:
            mapping = yaml.safe_load(fh) or {}

        users = mapping.get("users", [])
        all_tasks: dict[str, dict] = {}  # gid -> task (dedup)

        for user in users:
            asana_gid = user.get("asana_gid", "")
            if not asana_gid:
                continue
            try:
                tasks = get_user_tasks(asana_gid, max_tasks=50)
                for t in tasks:
                    task_gid = t.get("gid") or t.get("id", "")
                    if task_gid:
                        all_tasks[str(task_gid)] = t
            except AsanaClientError as exc:
                logging.getLogger("reconciliation").warning(
                    "asana fetch failed for user %s: %s", asana_gid, exc
                )

        return list(all_tasks.values())

    except Exception as exc:
        logging.getLogger("reconciliation").warning(
            "Could not fetch Asana tasks: %s", exc
        )
        return []


def _fetch_active_deals() -> list[dict]:
    """Fetch active HubSpot deals from F3E Retail pipeline.

    Returns list of deal dicts with:
      name, id, last_activity_ts (unix seconds), deep_link
    """
    try:
        from cora.tools.hubspot_client import (
            HubSpotClientError,
            get_deals_by_pipeline,
            PIPELINE_F3E_RETAIL,
        )
        deals_raw = get_deals_by_pipeline(PIPELINE_F3E_RETAIL)
        deals = []
        for d in deals_raw:
            deal_id = str(d.get("id") or d.get("gid", ""))
            name = d.get("name") or d.get("properties", {}).get("dealname", "")
            # last activity: try hs_lastmodifieddate or closedate
            props = d.get("properties") or {}
            last_mod_str = props.get("hs_lastmodifieddate", "") or props.get("notes_last_updated", "")
            try:
                from datetime import datetime
                last_mod_ts = datetime.fromisoformat(
                    last_mod_str.replace("Z", "+00:00")
                ).timestamp() if last_mod_str else 0.0
            except Exception:
                last_mod_ts = 0.0

            from cora.tools.hubspot_client import _PORTAL_ID
            deal_url = f"https://app.hubspot.com/contacts/{_PORTAL_ID}/deal/{deal_id}"

            deals.append({
                "id": deal_id,
                "name": name,
                "last_activity_ts": last_mod_ts,
                "deep_link": f"<{deal_url}|{name}>",
                "stage": props.get("dealstage", ""),
            })
        return deals

    except Exception as exc:
        logging.getLogger("reconciliation").warning(
            "Could not fetch HubSpot deals: %s", exc
        )
        return []


def _write_gaps_file(gaps: list[ReconciliationGap], date_str: str) -> Path:
    """Write gaps to data/reconciliation/YYYY-MM-DD-gaps.jsonl."""
    GAPS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = GAPS_DIR / f"{date_str}-gaps.jsonl"
    with out_path.open("w", encoding="utf-8") as fh:
        for gap in gaps:
            record = {
                "gap_id": gap.gap_id,
                "gap_type": gap.gap_type,
                "description": gap.description,
                "source_evidence": gap.source_evidence,
                "source": gap.source,
                "source_id": gap.source_id,
                "entity": gap.entity,
                "confidence": gap.confidence,
                "proposed_action": gap.proposed_action,
                "payload": gap.payload,
                "deep_link": gap.deep_link,
                "title": gap.title,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run reconciliation but do NOT propose updates to knowledge_review",
    )
    parser.add_argument(
        "--passes", type=str, default="1,2,3,4",
        help="Comma-separated list of passes to run (default: 1,2,3,4)",
    )
    parser.add_argument(
        "--lookback-hours", type=float, default=25.0,
        help="Hours of KB history to scan (default: 25)",
    )
    args = parser.parse_args()

    _setup_logging()
    log = logging.getLogger("reconciliation")
    log.info("=" * 60)
    log.info("Reconciliation sweep starting (dry_run=%s)", args.dry_run)

    try:
        passes = [int(p.strip()) for p in args.passes.split(",") if p.strip()]
    except ValueError:
        log.error("Invalid --passes value: %s", args.passes)
        return 1

    lookback_seconds = args.lookback_hours * 3600
    date_str = datetime.now().strftime("%Y-%m-%d")
    exit_code = 0

    # ─── Fetch open tasks and active deals ────────────────────────────────────
    log.info("Fetching open Asana tasks...")
    open_tasks = _fetch_open_tasks()
    log.info("Fetched %d open Asana tasks", len(open_tasks))

    log.info("Fetching active HubSpot deals...")
    active_deals = _fetch_active_deals()
    log.info("Fetched %d active HubSpot deals", len(active_deals))

    # ─── Run reconciliation ───────────────────────────────────────────────────
    log.info("Running reconciliation passes %s over last %.0fh of KB...", passes, args.lookback_hours)
    try:
        gaps = reconcile(
            open_tasks,
            active_deals,
            lookback_seconds=lookback_seconds,
            db_path=KB_DB_PATH if KB_DB_PATH.exists() else None,
            passes=passes,
        )
    except Exception as exc:
        log.error("reconciliation_engine.reconcile() failed: %s", exc, exc_info=True)
        return 1

    log.info(
        "Reconciliation complete: %d actionable gaps found (high=%d, med=%d)",
        len(gaps),
        sum(1 for g in gaps if g.confidence == "HIGH"),
        sum(1 for g in gaps if g.confidence == "MED"),
    )

    if not gaps:
        log.info("No actionable gaps — nothing to propose")
        # Write empty gaps file so scheduled-task scheduler knows it ran
        _write_gaps_file([], date_str)
        return 0

    # ─── Write gaps file ──────────────────────────────────────────────────────
    gaps_path = _write_gaps_file(gaps, date_str)
    log.info("Wrote %d gaps to %s", len(gaps), gaps_path)

    # ─── Propose updates to Harrison via knowledge_review ─────────────────────
    high_med = [g for g in gaps if g.confidence in _REVIEW_CONFIDENCES]
    log.info("Queuing %d HIGH/MED gaps for Harrison's 👍/👎 review", len(high_med))

    proposed = 0
    skipped = 0

    for gap in high_med:
        update_type = _GAP_TYPE_TO_UPDATE_TYPE.get(gap.gap_type, "generic")

        if args.dry_run:
            log.info(
                "[DRY RUN] Would propose: [%s] %s confidence=%s",
                gap.gap_type, gap.description[:80], gap.confidence,
            )
            proposed += 1
            continue

        try:
            propose_update(
                update_id=gap.gap_id,
                update_type=update_type,
                description=gap.description,
                payload=gap.payload,
                source_evidence=gap.source_evidence,
                confidence=gap.confidence,
            )
            proposed += 1
            log.info(
                "Proposed update gap_id=%s type=%s confidence=%s",
                gap.gap_id[:20], gap.gap_type, gap.confidence,
            )
        except Exception as exc:
            log.warning("Failed to propose update for gap %s: %s", gap.gap_id[:20], exc)
            skipped += 1

    log.info(
        "Reconciliation sweep done — %d proposed, %d skipped (exit=%d)",
        proposed, skipped, exit_code,
    )

    if skipped > 0:
        exit_code = 2

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
