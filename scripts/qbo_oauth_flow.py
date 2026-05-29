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

If localhost redirect is blocked (Intuit Production rejects http://localhost):
    1. In the Intuit Developer Portal → Settings → Redirect URIs → Production, add:
           https://developer.intuit.com/v2/OAuth2Playground/RedirectUrl
    2. Run with --manual:
           uv run python scripts/qbo_oauth_flow.py --entity HJRG --manual
       The browser will redirect to the Playground page. Copy the FULL URL from the
       address bar (it contains ?code=...&realmId=...) and paste it into the terminal.

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


# Known realm IDs from Chrome Agent research (May 2026).
# Used to warn when the wrong company was selected during OAuth.
# LEXINGTON_FIRM_REALM is the accountant's own books — never correct for an entity client.
_LEXINGTON_FIRM_REALM = "416205631"

_KNOWN_REALM_IDS: dict[str, str] = {
    "F3E":   "9341454160552149",   # F3 Energy Holdings Inc
    "LEX":   "476503710",          # LLC Lexington LLC
    "OSN":   "9341456036989538",   # OSN CORE 4 LLC
    "BDM":   "9341454760124312",   # Big D Media
    "HJRG":  "9130349690118516",   # HJR GS
    "HJRP":  "123145677834422",    # HJR Properties
    "HRLLC": "9130351363051036",   # Harrison Rogers, LLC
}

_KNOWN_REALM_NAMES: dict[str, str] = {
    "9341454160552149": "F3 Energy Holdings Inc",
    "476503710":        "LLC Lexington LLC",
    "9341456036989538": "OSN CORE 4 LLC",
    "9341454760124312": "Big D Media",
    "9130349690118516": "HJR GS",
    "123145677834422":  "HJR Properties",
    "9130351363051036": "Harrison Rogers, LLC",
    _LEXINGTON_FIRM_REALM: "Lexington Firm (accountant's own company — NOT an entity client)",
}


def _cmd_bootstrap(entity: str, environment: str, manual: bool = False) -> int:
    log.info("Beginning QBO OAuth bootstrap for entity=%s env=%s manual=%s", entity, environment, manual)
    try:
        entry = start_oauth_flow(entity, environment=environment, manual_callback=manual)
    except QboAuthError as exc:
        log.error("OAuth flow failed: %s", exc)
        return 1

    realm_id = entry["realm_id"]
    print()
    print(f"  [OK] QBO authorized for entity={entity!r}")
    print(f"  [OK] realm_id={realm_id}")
    print(f"  [OK] environment={entry['environment']}")
    print(f"  [OK] access token valid for ~1 hour; refresh token valid ~100 days")
    print(f"  [OK] Tokens persisted to .credentials/qbo-tokens.json (gitignored)")
    print()

    # Warn if Lexington Firm's realm was captured (wrong company selected during auth)
    if realm_id == _LEXINGTON_FIRM_REALM:
        print(f"  [WARN] realm_id {realm_id} is Lexington Firm's accountant company.")
        print(f"  [WARN] This is almost certainly WRONG for entity={entity!r}.")
        print(f"  [WARN] During the OAuth browser flow, you need to select the")
        print(f"  [WARN] {entity} client company — NOT 'Lexington Firm'.")
        print(f"  [WARN] See instructions: before pasting the OAuth URL, open QBOA,")
        print(f"  [WARN] click into the {entity} company, THEN paste the OAuth URL")
        print(f"  [WARN] in a new tab so Intuit shows that company for authorization.")
        print()
        return 1

    # Warn if the realm_id doesn't match the known expected value for this entity
    expected = _KNOWN_REALM_IDS.get(entity.upper())
    if expected and realm_id != expected:
        known_name = _KNOWN_REALM_NAMES.get(realm_id, f"unknown company (realm {realm_id})")
        print(f"  [WARN] realm_id mismatch for entity={entity!r}.")
        print(f"  [WARN] Expected: {expected} ({_KNOWN_REALM_NAMES.get(expected, entity)})")
        print(f"  [WARN] Got:      {realm_id} ({known_name})")
        print(f"  [WARN] Wrong QBO company was selected. Re-run after entering the")
        print(f"  [WARN] correct company in QBOA first.")
        print()
        return 1

    return 0


def _cmd_list() -> int:
    entities = list_provisioned_entities()
    if not entities:
        print("No QBO entities provisioned yet. Run with --entity <CODE> to bootstrap one.")
        return 0
    print(f"Provisioned QBO entities ({len(entities)}):")
    for e in entities:
        print(f"  - {e}")
    return 0


def _cmd_refresh_all() -> int:
    results = refresh_all_entities()
    if not results:
        print("No entities provisioned - nothing to refresh.")
        return 0
    print(f"Refreshed {len(results)} entities:")
    failures = 0
    for entity, status in results.items():
        marker = "[OK]" if status == "ok" else "[FAIL]"
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
    parser.add_argument(
        "--manual",
        action="store_true",
        help=(
            "Manual copy-paste OAuth flow — skips the local callback server. "
            "Use when Intuit Production Redirect URIs reject http://localhost. "
            "Requires https://developer.intuit.com/v2/OAuth2Playground/RedirectUrl "
            "to be registered in the app's Production Redirect URIs."
        ),
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
    return _cmd_bootstrap(args.entity.upper(), environment, manual=args.manual)


if __name__ == "__main__":
    sys.exit(main())
