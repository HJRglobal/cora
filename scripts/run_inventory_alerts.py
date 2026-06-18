#!/usr/bin/env python3
"""Inventory + Reorder Alerts -- F3E inventory threshold monitoring.

F3E pass:
  Calls get_f3e_inventory_pulse_text() and posts flagged items (lines
  containing the warning/critical emoji) to #f3e-leadership.

(The OSN pass was removed 2026-06-17 with the Clover retirement -- per-SKU
store inventory has no QBO equivalent, so OSN low-stock alerts are no longer
produced unless a replacement POS/inventory source is named.)

Throttle: 7-day per-SKU key to prevent spam.

Usage (Windows Task Scheduler):
    python scripts/run_inventory_alerts.py [--dry-run]

Environment variables required:
    SLACK_BOT_TOKEN              For posting alerts
    (F3E uses Drive/Google creds)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

load_dotenv(_REPO_ROOT / ".env")

import cora  # noqa: E402,F401 -- installs the egress sanitizer (cora/__init__.py) at module load

LOG_DIR = _REPO_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s -- %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            LOG_DIR / f"cora-{datetime.now().strftime('%Y-%m-%d')}.log",
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger("inventory_alerts")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_THROTTLE_PATH = _REPO_ROOT / "data" / "state" / "inventory_alert_throttle.json"
_THROTTLE_SECONDS = 7 * 86400  # 7 days

# Channel ID -- use leadership channel (no dedicated #f3e-ops yet)
_F3E_CHANNEL = "C0B4KRQT3LY"   # #f3e-leadership (fallback for #f3e-ops)

# Warning/critical emoji from inventory_client.py
_FLAG_CRITICAL = "\U0001f6a8"  # 🚨
_FLAG_WARNING = "⚠️"  # ⚠️
_FLAG_WARNING_ALT = "⚠"   # ⚠ (without variation selector)


def _load_throttle() -> dict:
    if _THROTTLE_PATH.exists():
        try:
            return json.loads(_THROTTLE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_throttle(throttle: dict) -> None:
    _THROTTLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _THROTTLE_PATH.write_text(json.dumps(throttle, indent=2), encoding="utf-8")


def _is_throttled(throttle: dict, key: str) -> bool:
    ts = throttle.get(key)
    return ts is not None and (time.time() - ts) < _THROTTLE_SECONDS


def _post_message(slack_client, channel: str, text: str, dry_run: bool) -> bool:
    if dry_run:
        log.info("[DRY-RUN] Post to %s: %.120s", channel, text)
        return True
    try:
        slack_client.chat_postMessage(channel=channel, text=text)
        return True
    except Exception as exc:
        log.warning("Failed to post to %s: %s", channel, exc)
        return False


# ---------------------------------------------------------------------------
# F3E Pass
# ---------------------------------------------------------------------------

def _is_flagged_line(line: str) -> bool:
    """Return True if line contains a warning or critical flag emoji."""
    return (
        _FLAG_CRITICAL in line
        or _FLAG_WARNING in line
        or _FLAG_WARNING_ALT in line
    )


def run_f3e_pass(slack_client, throttle: dict, dry_run: bool) -> dict[str, Any]:
    stats = {"posted": 0, "throttled": 0, "error": None}

    try:
        from cora.tools.inventory_client import get_f3e_inventory_pulse_text, UNKNOWN_RESPONSE
        pulse_text = get_f3e_inventory_pulse_text()
    except Exception as exc:
        stats["error"] = str(exc)
        log.error("F3E inventory fetch error: %s", exc)
        return stats

    if not pulse_text or pulse_text == UNKNOWN_RESPONSE:
        log.info("F3E inventory pulse returned no data / UNKNOWN_RESPONSE")
        return stats

    # Extract flagged lines
    flagged_lines = [
        line for line in pulse_text.splitlines() if _is_flagged_line(line)
    ]

    if not flagged_lines:
        log.info("F3E inventory: no flagged lines found")
        return stats

    # Throttle per SKU key derived from line content
    new_flagged = []
    for line in flagged_lines:
        # Use first 80 chars as a stable key
        key = f"f3e:{line.strip()[:80]}"
        if _is_throttled(throttle, key):
            stats["throttled"] += 1
        else:
            new_flagged.append((key, line))

    if not new_flagged:
        return stats

    flagged_text = "\n".join(line for _, line in new_flagged)
    msg = f":warning: *F3E Inventory Alert*\n{flagged_text}"

    if _post_message(slack_client, _F3E_CHANNEL, msg, dry_run):
        for key, _ in new_flagged:
            throttle[key] = time.time()
        stats["posted"] = len(new_flagged)
        log.info("Posted F3E inventory alert: %d items", len(new_flagged))

    return stats


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(dry_run: bool = False) -> dict[str, Any]:
    from slack_sdk import WebClient as SlackWebClient

    bot_token = os.getenv("SLACK_BOT_TOKEN")
    if not bot_token:
        log.error("SLACK_BOT_TOKEN not set")
        return {"error": "SLACK_BOT_TOKEN not set"}

    slack = SlackWebClient(token=bot_token)
    throttle = _load_throttle()

    log.info("Starting inventory alerts, dry_run=%s", dry_run)

    f3e_stats = run_f3e_pass(slack, throttle, dry_run)
    log.info("F3E pass: %s", f3e_stats)

    if not dry_run:
        _save_throttle(throttle)

    result = {
        "f3e_posted": f3e_stats.get("posted", 0),
        "f3e_throttled": f3e_stats.get("throttled", 0),
        "f3e_error": f3e_stats.get("error"),
    }
    log.info("Inventory alerts complete: %s", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cora inventory alert script")
    parser.add_argument("--dry-run", action="store_true", help="Log without posting")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
    sys.exit(0)
