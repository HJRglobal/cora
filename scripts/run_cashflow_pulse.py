#!/usr/bin/env python3
"""Cross-Entity Cash Flow Pulse -- DM Harrison with portfolio-wide cash summary.

Fires daily at 15:30 UTC (8:30 AM AZ) to give a quick morning cash pulse
across all top-level entities.

Usage (Windows Task Scheduler):
    python scripts/run_cashflow_pulse.py [--dry-run]

Environment variables required:
    SLACK_BOT_TOKEN              For sending DMs
    GOOGLE_SA_CREDENTIALS_JSON   For gsheets connector
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

load_dotenv(_REPO_ROOT / ".env")

from cora.connectors.gsheets_financials import (  # noqa: E402
    entity_to_tab,
    get_cashflow,
    GsheetsConnectorError,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("cashflow_pulse")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HARRISON_SLACK_ID = "U0B2RM2JYJ1"

PULSE_ENTITIES: list[tuple[str, str]] = [
    ("FNDR",    "Portfolio"),
    ("F3E",     "F3 Energy"),
    ("OSN",     "One Stop Nutrition"),
    ("LEX",     "Lexington Services"),
    ("HJRG",    "HJR Global"),
    ("HJRP",    "HJR Properties"),
    ("BDM",     "Big D Media"),
    ("UFL",     "United Fight League"),
    ("HJRPROD", "HJR Productions"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_currency(value: float | None) -> str:
    """Format a float as a currency string, or '--' if None."""
    if value is None:
        return "--"
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.0f}"


def _runway_flag(closing_balance: float | None, actual: float | None, forecast: float | None) -> str:
    """Return status emoji based on cash runway."""
    if closing_balance is None:
        return ":question:"
    if closing_balance < 0:
        return ":rotating_light:"
    if actual is not None and forecast is not None:
        # weekly burn = negative actual flow
        weekly_burn = abs(actual) if actual < 0 else 0
        if weekly_burn > 0 and closing_balance < 2 * weekly_burn:
            return ":warning:"
    return ":white_check_mark:"


def _fetch_entity_data(entity_code: str) -> dict[str, Any] | None:
    """Fetch cashflow data for one entity. Returns None on failure."""
    try:
        tab = entity_to_tab(entity_code)
        summary = get_cashflow(tab_name=tab)
        return {
            "closing_balance": summary.closing_balance,
            "week_label": summary.week_label,
            "actual": summary.portfolio_actual,
            "forecast": summary.portfolio_forecast,
        }
    except GsheetsConnectorError as exc:
        log.warning("GsheetsConnectorError for entity=%s: %s", entity_code, exc)
        return None
    except Exception as exc:
        log.warning("Unexpected error for entity=%s: %s", entity_code, exc)
        return None


def build_pulse_message(results: list[dict[str, Any]]) -> str:
    """Build the Slack DM text for the cash pulse."""
    # Find week label from first successful result
    week_label = next(
        (r["week_label"] for r in results if r.get("ok") and r.get("week_label")),
        "this week",
    )

    lines = [
        f":bank: *Cross-Entity Cash Pulse -- {week_label}*",
        "",
        "```",
        f"{'Entity':<25} {'Ending Cash':>13}  Status",
        "-" * 50,
    ]

    flagged = 0
    for r in results:
        label = r["label"]
        if r.get("ok"):
            cash_str = _format_currency(r["closing_balance"])
            flag = _runway_flag(r["closing_balance"], r.get("actual"), r.get("forecast"))
            if ":warning:" in flag or ":rotating_light:" in flag:
                flagged += 1
            # Plain text flag chars for code block
            flag_char = "OK" if flag == ":white_check_mark:" else ("??" if flag == ":question:" else "!!")
            lines.append(f"{label:<25} {cash_str:>13}  {flag_char}")
        else:
            lines.append(f"{label:<25} {'--':>13}  ?? (unavailable)")

    lines.append("```")
    lines.append("")

    # Second pass: add emoji indicators after code block
    emoji_lines = []
    for r in results:
        if r.get("ok"):
            flag = _runway_flag(r["closing_balance"], r.get("actual"), r.get("forecast"))
            if flag in (":warning:", ":rotating_light:"):
                emoji_lines.append(f"{flag} *{r['label']}* -- {_format_currency(r['closing_balance'])}")

    if emoji_lines:
        lines.append(":triangular_flag_on_post: *Flagged entities:*")
        lines.extend(emoji_lines)
        lines.append("")

    ok_count = sum(1 for r in results if r.get("ok"))
    fail_count = len(results) - ok_count
    lines.append(f"_{ok_count} entities fetched, {fail_count} unavailable, {flagged} flagged_")

    return "\n".join(lines)


def send_dm(slack_client, user_id: str, text: str, dry_run: bool = False) -> None:
    """Open DM channel and post message to Harrison."""
    if dry_run:
        log.info("[DRY RUN] Would send DM to %s:\n%s", user_id, text)
        return
    resp = slack_client.conversations_open(users=[user_id])
    dm_channel = resp["channel"]["id"]
    slack_client.chat_postMessage(channel=dm_channel, text=text)
    log.info("Sent cash pulse DM to %s via channel %s", user_id, dm_channel)


def run(dry_run: bool = False) -> dict[str, int]:
    """Main entry point. Returns summary stats."""
    from slack_sdk import WebClient  # noqa: F401

    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not bot_token:
        log.error("SLACK_BOT_TOKEN not set -- cannot send DM")
        return {"entities_fetched": 0, "entities_failed": 0, "flagged": 0}

    slack_client = WebClient(token=bot_token)

    results: list[dict[str, Any]] = []

    for entity_code, label in PULSE_ENTITIES:
        log.info("Fetching cashflow for entity=%s", entity_code)
        data = _fetch_entity_data(entity_code)
        if data is not None:
            results.append({
                "entity_code": entity_code,
                "label": label,
                "ok": True,
                "closing_balance": data.get("closing_balance"),
                "week_label": data.get("week_label", ""),
                "actual": data.get("actual"),
                "forecast": data.get("forecast"),
            })
        else:
            results.append({
                "entity_code": entity_code,
                "label": label,
                "ok": False,
            })

    entities_fetched = sum(1 for r in results if r.get("ok"))
    entities_failed = len(results) - entities_fetched
    flagged = sum(
        1 for r in results
        if r.get("ok") and _runway_flag(
            r.get("closing_balance"), r.get("actual"), r.get("forecast")
        ) in (":warning:", ":rotating_light:")
    )

    if entities_fetched == 0:
        log.warning("All entity fetches failed -- skipping DM")
        return {"entities_fetched": 0, "entities_failed": entities_failed, "flagged": 0}

    message = build_pulse_message(results)

    try:
        send_dm(slack_client, HARRISON_SLACK_ID, message, dry_run=dry_run)
    except Exception as exc:
        log.error("Failed to send DM: %s", exc)

    return {
        "entities_fetched": entities_fetched,
        "entities_failed": entities_failed,
        "flagged": flagged,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cross-Entity Cash Flow Pulse")
    parser.add_argument("--dry-run", action="store_true", help="Log output instead of sending DM")
    args = parser.parse_args()

    result = run(dry_run=args.dry_run)
    log.info("cashflow_pulse result: %s", result)
