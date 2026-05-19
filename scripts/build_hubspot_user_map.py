#!/usr/bin/env python3
"""Auto-build slack-to-hubspot mapping by matching users on email.

Pulls HubSpot owners (via HUBSPOT_PRIVATE_APP_TOKEN) AND Slack workspace members
(via SLACK_BOT_TOKEN), joins on email, emits paste-ready YAML rows.

Usage:
    python scripts/build_hubspot_user_map.py

Output sections:
1. Auto-matched users → paste these into data/maps/slack-to-hubspot.yaml
2. Slack-only / HubSpot-only → can't be auto-mapped (email mismatch, no HubSpot
   account, only in Slack as guest, etc.)

Requires SLACK_BOT_TOKEN scopes: users:read + users:read.email (same as Asana mapper).
"""

import os
import sys

import httpx
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

load_dotenv()

_HUBSPOT_BASE = "https://api.hubapi.com"


def _dump_hubspot_owners(token: str) -> list[dict]:
    """Fetch all active HubSpot owners (paginated)."""
    owners: list[dict] = []
    after: str | None = None
    headers = {"Authorization": f"Bearer {token}"}
    while True:
        params = {"limit": 100}
        if after:
            params["after"] = after
        r = httpx.get(
            f"{_HUBSPOT_BASE}/crm/v3/owners",
            params=params,
            headers=headers,
            timeout=15.0,
        )
        if r.status_code != 200:
            raise RuntimeError(f"HubSpot owners {r.status_code}: {r.text[:300]}")
        body = r.json()
        owners.extend(body.get("results", []) or [])
        paging = body.get("paging", {}) or {}
        after = (paging.get("next") or {}).get("after")
        if not after:
            break
    return owners


def _dump_slack_users(token: str) -> list[dict]:
    client = WebClient(token=token)
    members: list[dict] = []
    cursor: str | None = None
    try:
        while True:
            resp = client.users_list(cursor=cursor, limit=200)
            members.extend(resp["members"])
            cursor = resp.get("response_metadata", {}).get("next_cursor") or None
            if not cursor:
                break
    except SlackApiError as exc:
        print(f"ERROR: Slack users.list failed: {exc.response['error']}", file=sys.stderr)
        raise
    return [
        m for m in members
        if not m.get("deleted") and not m.get("is_bot") and m.get("id") != "USLACKBOT"
    ]


def main() -> int:
    hs_token = os.environ.get("HUBSPOT_PRIVATE_APP_TOKEN", "")
    slack_token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not hs_token:
        print("ERROR: HUBSPOT_PRIVATE_APP_TOKEN not set in .env", file=sys.stderr)
        return 1
    if not slack_token:
        print("ERROR: SLACK_BOT_TOKEN not set in .env", file=sys.stderr)
        return 1

    print("Fetching HubSpot owners...", file=sys.stderr)
    hs_owners = _dump_hubspot_owners(hs_token)
    hs_by_email = {(o.get("email") or "").lower(): o for o in hs_owners if o.get("email")}
    print(
        f"  {len(hs_owners)} HubSpot owners, {len(hs_by_email)} with emails",
        file=sys.stderr,
    )

    print("Fetching Slack users...", file=sys.stderr)
    slack_members = _dump_slack_users(slack_token)
    slack_by_email = {
        (m.get("profile", {}).get("email") or "").lower(): m
        for m in slack_members
        if m.get("profile", {}).get("email")
    }
    print(
        f"  {len(slack_members)} active Slack members, {len(slack_by_email)} with emails",
        file=sys.stderr,
    )

    matched: list[tuple[str, dict, dict]] = []
    slack_only: list[dict] = []
    hubspot_only: list[dict] = []

    for email, slack_u in slack_by_email.items():
        if email in hs_by_email:
            matched.append((email, slack_u, hs_by_email[email]))
        else:
            slack_only.append(slack_u)
    for email, hs_u in hs_by_email.items():
        if email not in slack_by_email:
            hubspot_only.append(hs_u)

    print(f"# Auto-matched {len(matched)} users (present in both Slack and HubSpot).")
    print(f"# Paste the 'users:' block below into data/maps/slack-to-hubspot.yaml.")
    print()
    print("users:")
    for email, slack_u, hs_u in sorted(
        matched, key=lambda x: ((x[2].get("firstName") or "") + " " + (x[2].get("lastName") or "")).lower()
    ):
        first = hs_u.get("firstName") or ""
        last = hs_u.get("lastName") or ""
        display = (first + " " + last).strip() or email
        owner_id = hs_u.get("id") or ""
        slack_id = slack_u.get("id") or ""
        print(f"  # {display}  ({email})")
        print(f"  - slack_user_id: {slack_id}")
        print(f"    hubspot_owner_id: {owner_id}")
        print(f"    hubspot_email: {email}")
        print(f"    display_name: {display}")
        print()

    if slack_only:
        print(f"# === Slack-only ({len(slack_only)}) — not in HubSpot, can't be mapped: ===")
        for m in sorted(slack_only, key=lambda x: (x.get("real_name") or "").lower()):
            real_name = m.get("real_name") or m.get("name") or "(no name)"
            email = m.get("profile", {}).get("email") or "(no email)"
            print(f"#   {real_name}  <{email}>  (slack_id: {m['id']})")
        print()

    if hubspot_only:
        print(f"# === HubSpot-only ({len(hubspot_only)}) — not in Slack: ===")
        for u in sorted(
            hubspot_only,
            key=lambda x: ((x.get("firstName") or "") + " " + (x.get("lastName") or "")).lower(),
        ):
            first = u.get("firstName") or ""
            last = u.get("lastName") or ""
            display = (first + " " + last).strip() or "(no name)"
            print(f"#   {display}  <{u.get('email')}>  (owner_id: {u.get('id')})")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
