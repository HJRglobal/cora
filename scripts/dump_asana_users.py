#!/usr/bin/env python3
"""Dump all Asana workspace users to a paste-ready YAML block.

Usage:
    python scripts/dump_asana_users.py [> dump.yaml]

Reads ASANA_PAT from .env. Lists every user in the HJR Global workspace
(gid 682743441507584) sorted alphabetically. For each user, emits a YAML
row pre-populated with asana_user_gid, asana_email, display_name — leaving
slack_user_id as a placeholder for Harrison to fill in.

To finish the mapping for a person:
  1. Slack desktop → click their profile → ⋯ More → Copy member ID
  2. Paste that into the slack_user_id field
  3. Move the (now complete) row into data/maps/slack-to-asana.yaml

People who aren't in Asana can't be mapped (Cora returns a graceful "not
mapped" error for them — fall back to the static-context answer path).
"""

import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

_WORKSPACE_GID = "682743441507584"  # HJR Global


def main() -> int:
    pat = os.environ.get("ASANA_PAT", "")
    if not pat:
        print("ERROR: ASANA_PAT not set in .env", file=sys.stderr)
        return 1

    try:
        r = httpx.get(
            f"https://app.asana.com/api/1.0/workspaces/{_WORKSPACE_GID}/users",
            params={"opt_fields": "name,email,gid"},
            headers={"Authorization": f"Bearer {pat}"},
            timeout=15.0,
        )
    except httpx.RequestError as exc:
        print(f"ERROR: Asana network failure: {exc}", file=sys.stderr)
        return 1

    if r.status_code != 200:
        print(f"ERROR: Asana returned {r.status_code}: {r.text[:300]}", file=sys.stderr)
        return 1

    users = r.json().get("data", []) or []
    users.sort(key=lambda u: (u.get("name") or "").lower())

    print(f"# Asana workspace users — workspace {_WORKSPACE_GID}")
    print(f"# Found {len(users)} users. Generated {sys.argv[0]}.")
    print(f"# Paste rows you want into data/maps/slack-to-asana.yaml.")
    print(f"# Replace REPLACE_WITH_SLACK_ID with each person's Slack member ID:")
    print(f"#   Slack desktop -> click profile -> ... -> Copy member ID")
    print()
    print("users:")
    for u in users:
        name = u.get("name") or "(no name)"
        email = u.get("email") or "(no email)"
        gid = u.get("gid") or ""
        print(f"  # {name}  ({email})")
        print(f"  - slack_user_id: REPLACE_WITH_SLACK_ID")
        print(f"    asana_user_gid: {gid}")
        print(f"    asana_email: {email}")
        print(f"    display_name: {name}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
