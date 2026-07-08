#!/usr/bin/env python3
"""Daily ENTITY synthesis -> the entity's leadership channel.

One parameterized runner for every operating-entity daily synthesis. Each post is
a Tag-style operational briefing (Moved / Needs you / Due soon / Watch), scoped
strictly to the entity, source-opaque, cash included (all targets are TIER_1).

Entities: f3e | osn | ufl | bdm | hjrp | hjrprod | f3c | lex
  (LEX is aggregate/PHI-safe: counts-only deadlines, no client detail, a layered
   output PHI gate; posts only to #lex-leadership.)

Standalone script (D-047): imports channel_synthesis / strategy_memo only -> no bot
restart. Posts route through the egress boundary; the TIER_1 channel allowlist is
the financial-firewall gate.

Usage:
    .venv\\Scripts\\python.exe scripts/run_entity_synthesis.py --entity f3e [--dry-run] [--channel C...]
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

import cora  # noqa: E402,F401 -- installs the egress sanitizer
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
log = logging.getLogger("entity_synthesis")

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

_ENTITY_CHOICES = ["f3e", "osn", "ufl", "bdm", "hjrp", "hjrprod", "f3c", "lex"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily entity synthesis")
    parser.add_argument("--entity", required=True, choices=_ENTITY_CHOICES,
                        help="Entity code to synthesize")
    parser.add_argument("--dry-run", action="store_true",
                        help="Gather + synthesize + print; post nothing")
    parser.add_argument("--channel", default=None,
                        help=f"Override target channel id (smoke {cs.SMOKE_CHANNEL})")
    args = parser.parse_args()

    result = cs.run_entity(args.entity, dry_run=args.dry_run, channel=args.channel)

    if args.dry_run:
        print(f"\n===== {args.entity.upper()} SYNTHESIS (DRY RUN) =====\n")
        print(result["body"])
        print("\n===== FACT BASE =====\n")
        print(result["facts"])

    log.info("entity_synthesis result: scope=%s synthesized=%s delivered=%s "
             "first_run=%s", result["scope"], result["synthesized"],
             result["delivered"], result["first_run"])
    return 0 if (args.dry_run or result["delivered"]) else 1


if __name__ == "__main__":
    sys.exit(main())
