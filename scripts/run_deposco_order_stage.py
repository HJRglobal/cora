#!/usr/bin/env python3
"""F3E -- Stage one Deposco order for push (SONNET-HANDOFF step 5).

Reads a human-authored spec YAML (see `02-F3-Energy/orders/_templates/`),
validates it, runs the live GET-only preflight, and -- only if everything
passes -- persists a pending entry and DMs the push card to Harrison. NOTHING
is ever pushed by this script; a human tap in Slack is the only push path
(`deposco_orders.handler.process_push_tap`, wired in `app.py`).

NO --sweep. v1 is a CLI run by Harrison (2026-09-09 ruling item 5); an
outbox sweep for Alex-authored specs is a later build, after clean #3.

Usage:
    python scripts/run_deposco_order_stage.py --spec PATH [--env ua|prod] [--dry-run]

Exit codes:
  0 = staged (or a clean --dry-run)
  2 = spec is invalid (YAML/schema; nothing checked live)
  3 = the channel's stage is blocked (a not-clean push with no root-cause note)
  4 = the live preflight found a problem (number/reference exists, item
      missing, ATP short, shipVia unconfirmed)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora.deposco_orders import handler, payload as payload_mod  # noqa: E402
from cora.deposco_orders import spec as spec_mod  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    load_dotenv(_REPO_ROOT / ".env", override=True)

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, help="path to the spec YAML")
    parser.add_argument("--env", default="prod", choices=["ua", "prod"])
    parser.add_argument("--dry-run", action="store_true",
                        help="validate + build the payload; print it; stage nothing")
    args = parser.parse_args(argv)

    spec_path = Path(args.spec)
    try:
        text = spec_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"FATAL: cannot read {spec_path} ({exc.__class__.__name__})")
        return 2

    raw, parse_errors = spec_mod.parse_spec_yaml(text)
    if parse_errors:
        print("SPEC INVALID:")
        for e in parse_errors:
            print(f"  - {e}")
        return 2

    result = spec_mod.validate_spec(raw)
    if not result.ok:
        print("SPEC INVALID:")
        for e in result.errors:
            print(f"  - {e}")
        return 2

    if args.dry_run:
        payload = payload_mod.build_payload(result.spec)
        print(json.dumps(payload, indent=2))
        print("\n[dry-run] nothing staged, nothing sent")
        return 0

    outcome = handler.stage_order(
        result.spec, env=args.env, spec_raw=raw, spec_path=str(spec_path),
    )
    if not outcome.ok:
        if outcome.blocked_reason:
            print(f"STAGE BLOCKED: {outcome.blocked_reason}")
            return 3
        print("LIVE PREFLIGHT FAILED (nothing staged):")
        for e in outcome.errors:
            print(f"  - {e}")
        return 4

    delivered = handler.deliver_card(
        outcome.entry, outcome.card_fallback, outcome.card_blocks,
    )
    number = delivered.get("number", "")
    print(f"STAGED: {number} (id={delivered.get('id', '')}, env={args.env})")
    if delivered.get("dm_message_ts"):
        print("Card delivered to Harrison's DM.")
    else:
        print(
            "WARNING: the card was recorded but NOT delivered to Slack (no "
            "SLACK_BOT_TOKEN, or a send error). It still exists and can be "
            "delivered by re-running deliver_card on this id."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
