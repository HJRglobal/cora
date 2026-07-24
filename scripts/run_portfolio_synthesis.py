#!/usr/bin/env python3
"""Daily PORTFOLIO synthesis -> #founder-operations.

The operational sibling of the weekly Harrison-only strategy memo: a holdco-wide
daily briefing (cash across entities, pipeline posture, deadline radar, stalled
P0/P1 decisions) posted to #founder-operations. HJR Global folds into this post
(no separate HJRG synthesis).

Standalone script (D-047): imports channel_synthesis / strategy_memo only, never
app.py / tool_dispatch / claude_client -> no bot restart needed. The post routes
through the egress boundary (importing a cora module installs the sanitizer;
deliver_to_channel also calls sanitize_text explicitly).

Guardrails: operational tone only (blunt founder recs stay in the private weekly
memo); financial firewall enforced by the TIER_1 channel allowlist; PHI/Visibility
never surfaced (Lexington aggregate only); advisory only (D-011).

Usage:
    .venv\\Scripts\\python.exe scripts/run_portfolio_synthesis.py [--dry-run] [--channel C...]
    --dry-run        gather + synthesize + print; post nothing, write no snapshot
    --channel CID    override the target (smoke to #cora-build = C0B4B0URRQS)
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

load_dotenv(_REPO_ROOT / ".env", override=True)

# Importing a cora module installs the egress sanitizer (cora/__init__.py).
import cora  # noqa: E402,F401
from cora import channel_synthesis as cs  # noqa: E402

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
log = logging.getLogger("portfolio_synthesis")

# Live briefing text is non-ASCII (emoji/dashes); keep the console from crashing
# on cp1252 when printing the dry-run body.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily portfolio synthesis")
    parser.add_argument("--dry-run", action="store_true",
                        help="Gather + synthesize + print; post nothing")
    parser.add_argument("--channel", default=None,
                        help=f"Override target channel id (default "
                             f"{cs.SCOPE_CHANNELS['portfolio']}; smoke "
                             f"{cs.SMOKE_CHANNEL})")
    args = parser.parse_args()

    result = cs.run_portfolio(dry_run=args.dry_run, channel=args.channel)

    if args.dry_run:
        print("\n===== PORTFOLIO SYNTHESIS (DRY RUN) =====\n")
        print(result["body"])
        print("\n===== FOUNDER-OS MARKDOWN (would write to "
              "00-Founder/_daily-synthesis/YYYY-MM/) =====\n")
        print(result.get("founder_os_md") or "(none)")
        print("\n===== FACT BASE =====\n")
        print(result["facts"])

    log.info("portfolio_synthesis result: scope=%s synthesized=%s delivered=%s "
             "first_run=%s founder_os=%s", result["scope"], result["synthesized"],
             result["delivered"], result["first_run"],
             result.get("founder_os_path"))
    # Nonzero on a real (non-dry-run) post failure so Task Scheduler surfaces a
    # silent no-post instead of always reading success.
    return 0 if (args.dry_run or result["delivered"]) else 1


if __name__ == "__main__":
    sys.exit(main())
