#!/usr/bin/env python3
"""One-time per-entity QBO OAuth bootstrap.

Run this ONCE per QBO company (e.g., one run for HJRG, one for F3E, one for OSN,
etc.). Each run:
  1. Opens a browser to the Intuit authorization page
  2. You sign in to QuickBooks, pick the company that maps to the entity, authorize
  3. Browser redirects to localhost:8765, this script captures the auth code
  4. Exchanges code for access + refresh tokens, persists to .credentials/qbo-tokens.json

Prerequisites in .env:
    QBO_CLIENT_ID=<from Intuit Developer Portal app>
    QBO_CLIENT_SECRET=<from same>
    QBO_REDIRECT_URI=http://localhost:8765/qbo-oauth-callback   # match the app config exactly
    QBO_ENVIRONMENT=production                                   # or "sandbox" for testing

Usage:
    cd C:\\Users\\Harri\\code\\cora
    uv run python scripts/qbo_oauth_flow.py --entity HJRG
    uv run python scripts/qbo_oauth_flow.py --entity F3E --sandbox

List provisioned entities:
    uv run python scripts/qbo_oauth_flow.py --list

Force refresh of all entities' access tokens (useful for the daily refresh task):
    uv run python scripts/qbo_oauth_flow.py --refresh-all
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Add src to path so we can import cora.* without installing
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cora.connectors.qbo_oauth import (  # noqa: E402
    QboAuthError,
    list_provisioned_entities,
    refresh_all_entities,
    start_oauth_flow,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("qbo_oauth_flow")


def _cmd_bootstrap(entity: str, environment: str) -> int:
    log.info("Beginning QBO OAuth bootstrap for entity=%s env=%s", entity, environment)
    try:
        entry = start_oauth_flow(entity, environment=environment)
    except QboAuthError as exc:
        log.error("OAuth flow failed: %s", exc)
        return 1

    print()
    print(f"  ✓ QBO authorized for entity={entity!r}")
    print(f"  ✓ realm_id={entry['realm_id']}")
    print(f"  ✓ environment={entry['environment']}")
    print(f"  ✓ access token valid for ~1 hour; refresh token valid ~100 days")
    print(f"  ✓ Tokens persisted to .credentials/qbo-tokens.json (gitignored)")
    print()
    return 0


def _cmd_list() -> int:
    entities = list_provisioned_entities()
    if not entities:
        print("No QBO entities provisioned yet. Run with --entity <CODE> to bootstrap one.")
        return 0
    print(f"Provisioned QBO entities ({len(entities)}):")
    for e in entities:
        print(f"  • {e}")
    return 0


def _cmd_refresh_all() -> int:
    results = refresh_all_entities()
    if not results:
        print("No entities provisioned — nothing to refresh.")
        return 0
    print(f"Refreshed {len(results)} entities:")
    failures = 0
    for entity, status in results.items():
        marker = "✓" if status == "ok" else "✗"
        print(f"  {marker} {entity}: {status}")
        if status != "ok":
            failures += 1
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--entity", help="Entity code to bootstrap (e.g. HJRG, F3E, OSN)")
    parser.add_argument(
        "--sandbox",
        action="store_true",
        help="Use Intuit sandbox environment instead of production",
    )
    parser.add_argument("--list", action="store_true", help="List provisioned entities")
    parser.add_argument(
        "--refresh-all",
        action="store_true",
        help="Force-refresh access tokens for all provisioned entities",
    )
    args = parser.parse_args()

    if args.list:
        return _cmd_list()
    if args.refresh_all:
        return _cmd_refresh_all()
    if not args.entity:
        parser.print_help()
        print("\nNo --entity / --list / --refresh-all specified.")
        return 2

    environment = "sandbox" if args.sandbox else "production"
    return _cmd_bootstrap(args.entity.upper(), environment)


if __name__ == "__main__":
    sys.exit(main())
