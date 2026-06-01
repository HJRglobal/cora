#!/usr/bin/env python3
"""5:30am AZ daily â€” cross-source reconciliation sweep.

Runs after all four KB sync tasks (Slack 2am, Gmail 2:30am, Asana 3am,
Fireflies 3:30am, static_md 4am, Drive 4:30am) have completed so the KB
contains fresh content for all sources.

Pipeline:
  1. Fetch open Asana tasks (all assigned users in workspace).
  2. Fetch active HubSpot deals from F3E Retail pipeline.
  3. Call reconciliation_engine.reconcile() â€” runs four passes:
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
    2 = partial â€” some passes failed or KB is empty
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
            logging.StreamHandler(open(sys.stdout.fileno(), "w", encoding="utf-8", errors="replace", closefd=False)),
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
            log.warning("slack-to-asana.yaml not found â€” using empty task list")
            return []

        with maps_path.open(encoding="utf-8") as fh:
            mapping = yaml.safe_load(fh) or {}

        users = mapping.get("users", [])
        all_tasks: dict[str, dict] = {}  # gid -> task (dedup)

        for user in users:
            asana_gid = user.get("asana_user_gid", "") or user.get("asana_gid", "")
            if not asana_gid:
                continue
            try:
                tasks = get_user_tasks(asana_gid, max_tasks=200)
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


def _build_name_to_sid_map() -> dict[str, str]:
    """Build display_name (lower) â†’ slack_user_id from slack-to-asana.yaml."""
    maps_path = _REPO_ROOT / "data" / "maps" / "slack-to-asana.yaml"
    try:
        import yaml
        data = yaml.safe_load(maps_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logging.getLogger("reconciliation").warning(
            "Could not load slack-to-asana.yaml for stale-task DMs: %s", exc
        )
        return {}
    result: dict[str, str] = {}
    for entry in (data.get("users") or []):
        if not isinstance(entry, dict):
            continue
        name = entry.get("display_name", "").strip()
        sid  = entry.get("slack_user_id", "").strip()
        if name and sid:
            result[name.lower()] = sid
    return result


def _dm_stale_task_assignees(gaps: list[ReconciliationGap]) -> None:
    """Send a gentle DM to each assignee whose tasks may already be done.

    Groups multiple stale gaps per user into a single DM. Skips users whose
    Slack ID cannot be resolved. Never DMs Harrison Rogers directly here â€”
    he already sees all gaps in the knowledge_review queue.
    """
    import os
    from slack_sdk import WebClient as SlackWebClient
    from slack_sdk.errors import SlackApiError

    log = logging.getLogger("reconciliation")

    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        log.warning("SLACK_BOT_TOKEN not set â€” skipping stale task DMs")
        return

    name_to_sid = _build_name_to_sid_map()
    if not name_to_sid:
        log.warning("No nameâ†’Slack ID map available â€” skipping stale task DMs")
        return

    slack = SlackWebClient(token=token)

    # Group gaps by assignee
    by_assignee: dict[str, list[ReconciliationGap]] = {}
    for gap in gaps:
        assignee = (gap.payload.get("assignee_name") or "").strip()
        if not assignee:
            continue
        by_assignee.setdefault(assignee, []).append(gap)

    # Harrison Rogers sees all gaps via knowledge_review DMs already.
    # DM-ing him about his own tasks here would be duplicate noise.
    _SKIP_DM_NAMES = {“harrison rogers”, “harrison”}

    for assignee_name, user_gaps in by_assignee.items():
        if assignee_name.lower() in _SKIP_DM_NAMES:
            log.info(
                “Skipping DM to %r -- routed to knowledge_review queue instead”,
                assignee_name,
            )
            continue
        slack_id = name_to_sid.get(assignee_name.lower())
        if not slack_id:
            log.info(
                “Could not resolve Slack ID for assignee %r -- no DM sent”, assignee_name
            )
            continue

        try:
            dm_resp    = slack.conversations_open(users=[slack_id])
            dm_channel = dm_resp["channel"]["id"]
        except SlackApiError as exc:
            log.warning(
                "Could not open DM for %s (%s): %s", assignee_name, slack_id, exc.response
            )
            continue

        first_name = assignee_name.split()[0]
        task_lines = []
        for gap in user_gaps[:5]:  # cap at 5 tasks per DM
            task_name = (gap.payload.get("task_name") or gap.title or "a task").strip()
            task_url  = gap.payload.get("task_url", "")
            if task_url:
                task_lines.append(f"â€¢ <{task_url}|{task_name}>")
            else:
                task_lines.append(f"â€¢ {task_name}")

        tasks_block = "\n".join(task_lines)
        msg = (
            f"Hey {first_name}! Cora noticed some recent Slack or email activity that "
            f"suggests one or more of your open tasks may already be wrapped up:\n\n"
            f"{tasks_block}\n\n"
            f"Could you check and mark them complete in Asana if they're done? "
            f"No pressure â€” just keeping the board tidy. Thanks! ðŸ™Œ"
        )

        try:
            slack.chat_postMessage(channel=dm_channel, text=msg)
            log.info(
                "Sent stale-task DM to %s (%s) â€” %d gap(s)",
                assignee_name, slack_id, len(user_gaps),
            )
        except SlackApiError as exc:
            log.warning(
                "Failed to send stale-task DM to %s: %s", assignee_name, exc.response
            )


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
        "--passes", type=str, default="1,2,3,4,5",
        help="Comma-separated list of passes to run (default: 1,2,3,4,5)",
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

    # â”€â”€â”€ Fetch open tasks and active deals â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    log.info("Fetching open Asana tasks...")
    open_tasks = _fetch_open_tasks()
    log.info("Fetched %d open Asana tasks", len(open_tasks))

    log.info("Fetching active HubSpot deals...")
    active_deals = _fetch_active_deals()
    log.info("Fetched %d active HubSpot deals", len(active_deals))

    # â”€â”€â”€ Build Anthropic client for pass 5 â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    anthropic_client = None
    if 5 in passes:
        import os
        import anthropic as _anthropic
        _api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if _api_key:
            try:
                anthropic_client = _anthropic.Anthropic(api_key=_api_key)
                log.info("Anthropic client ready for pass 5")
            except Exception as exc:
                log.warning("Could not build Anthropic client for pass 5: %s", exc)
        else:
            log.warning("ANTHROPIC_API_KEY not set â€” pass 5 will be skipped")

    # â”€â”€â”€ Run reconciliation â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    log.info("Running reconciliation passes %s over last %.0fh of KB...", passes, args.lookback_hours)
    try:
        gaps = reconcile(
            open_tasks,
            active_deals,
            lookback_seconds=lookback_seconds,
            db_path=KB_DB_PATH if KB_DB_PATH.exists() else None,
            passes=passes,
            anthropic_client=anthropic_client,
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
        log.info("No actionable gaps â€” nothing to propose")
        # Write empty gaps file so scheduled-task scheduler knows it ran
        _write_gaps_file([], date_str)
        return 0

    # â”€â”€â”€ Write gaps file â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    gaps_path = _write_gaps_file(gaps, date_str)
    log.info("Wrote %d gaps to %s", len(gaps), gaps_path)

    # â”€â”€â”€ Propose updates to Harrison via knowledge_review â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    high_med = [g for g in gaps if g.confidence in _REVIEW_CONFIDENCES]
    log.info("Queuing %d HIGH/MED gaps for Harrison review (+1/-1)", len(high_med))

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
        "Reconciliation sweep done â€” %d proposed, %d skipped (exit=%d)",
        proposed, skipped, exit_code,
    )

    # â”€â”€â”€ DM individual users for stale open task gaps â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    stale_gaps = [g for g in high_med if g.gap_type == "stale_open_task"]
    if stale_gaps and not args.dry_run:
        log.info("Sending stale-task DMs for %d gap(s)...", len(stale_gaps))
        _dm_stale_task_assignees(stale_gaps)
    elif stale_gaps and args.dry_run:
        log.info("[DRY RUN] Would DM assignees for %d stale-task gap(s)", len(stale_gaps))

    if skipped > 0:
        exit_code = 2

    return exit_code


if __name__ == "__main__":
    sys.exit(main())

