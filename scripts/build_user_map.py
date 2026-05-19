#!/usr/bin/env python3
"""Auto-build slack-to-asana mapping by matching users on email.

Pulls Asana workspace users (via ASANA_PAT) AND Slack workspace members
(via SLACK_BOT_TOKEN), joins on email, and emits paste-ready YAML rows
for every user present in both systems.

Usage:
    python scripts/build_user_map.py

Output sections:
1. Auto-matched users → paste these into data/maps/slack-to-asana.yaml
2. Slack-only / Asana-only → can't be auto-mapped (different emails, no
   Asana access, only in Slack as guest, etc.)

Requires SLACK_BOT_TOKEN to have these scopes:
  - users:read
  - users:read.email

If users:read.email is missing, Slack will return profiles without emails
and the matching will return zero. In that case, add the scope in the
Slack app config and re-run (no reinstall needed if the bot is already in
the workspace — but Slack will email you a re-auth link).
"""

import os
import sys

import httpx
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

load_dotenv()

_ASANA_WORKSPACE_GID = "682743441507584"  # HJR Global


def _dump_asana_users(pat: str) -> list[dict]:
    r = httpx.get(
        f"https://app.asana.com/api/1.0/workspaces/{_ASANA_WORKSPACE_GID}/users",
        params={"opt_fields": "name,email,gid"},
        headers={"Authorization": f"Bearer {pat}"},
        timeout=15.0,
    )
    r.raise_for_status()
    return r.json().get("data", []) or []


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
        if "missing_scope" in (exc.response.get("error") or ""):
            print(
                "Add scopes 'users:read' and 'users:read.email' in the Slack app config, "
                "then reinstall the app to the workspace.",
                file=sys.stderr,
            )
        raise
    # Filter out bots / deleted / Slackbot itself
    return [
        m for m in members
        if not m.get("deleted") and not m.get("is_bot") and m.get("id") != "USLACKBOT"
    ]


def main() -> int:
    pat = os.environ.get("ASANA_PAT", "")
    slack_token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not pat:
        print("ERROR: ASANA_PAT not set in .env", file=sys.stderr)
        return 1
    if not slack_token:
        print("ERROR: SLACK_BOT_TOKEN not set in .env", file=sys.stderr)
        return 1

    print("Fetching Asana users...", file=sys.stderr)
    asana_users = _dump_asana_users(pat)
    asana_by_email = {
        (u.get("email") or "").lower(): u for u in asana_users if u.get("email")
    }
    print(f"  {len(asana_users)} Asana users, {len(asana_by_email)} with emails", file=sys.stderr)

    print("Fetching Slack users...", file=sys.stderr)
    slack_members = _dump_slack_users(slack_token)
    slack_by_email = {
        (m.get("profile", {}).get("email") or "").lower(): m
        for m in slack_members
        if m.get("profile", {}).get("email")
    }
    print(f"  {len(slack_members)} active Slack members, {len(slack_by_email)} with emails", file=sys.stderr)

    # Join on email
    matched: list[tuple[str, dict, dict]] = []
    slack_only: list[dict] = []
    asana_only: list[dict] = []

    for email, slack_u in slack_by_email.items():
        if email in asana_by_email:
            matched.append((email, slack_u, asana_by_email[email]))
        else:
            slack_only.append(slack_u)
    for email, asana_u in asana_by_email.items():
        if email not in slack_by_email:
            asana_only.append(asana_u)

    # Emit YAML rows
    print(f"# Auto-matched {len(matched)} users (present in both Slack and Asana).")
    print(f"# Paste the 'users:' block below into data/maps/slack-to-asana.yaml")
    print(f"# (replacing or appending to the existing users: list).")
    print()
    print("users:")
    for email, slack_u, asana_u in sorted(matched, key=lambda x: (x[2].get("name") or "").lower()):
        name = asana_u.get("name") or "(no name)"
        gid = asana_u.get("gid") or ""
        slack_id = slack_u.get("id") or ""
        print(f"  # {name}  ({email})")
        print(f"  - slack_user_id: {slack_id}")
        print(f"    asana_user_gid: {gid}")
        print(f"    asana_email: {email}")
        print(f"    display_name: {name}")
        print()

    # Diagnostic sections
    if slack_only:
        print(f"# === Slack-only ({len(slack_only)}) — not in Asana, can't be mapped: ===")
        for m in sorted(slack_only, key=lambda x: (x.get("real_name") or "").lower()):
            real_name = m.get("real_name") or m.get("name") or "(no name)"
            email = m.get("profile", {}).get("email") or "(no email)"
            print(f"#   {real_name}  <{email}>  (slack_id: {m['id']})")
        print()

    if asana_only:
        print(f"# === Asana-only ({len(asana_only)}) — not in Slack, can't be mapped: ===")
        for u in sorted(asana_only, key=lambda x: (x.get("name") or "").lower()):
            print(f"#   {u.get('name')}  <{u.get('email')}>  (asana_gid: {u.get('gid')})")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
