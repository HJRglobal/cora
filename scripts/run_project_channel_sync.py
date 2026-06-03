#!/usr/bin/env python3
"""Daily 9am AZ — auto-create Slack channels for new Asana projects.

Polls Asana for projects in workspace 682743441507584, compares against a
local registry of already-created channels, and creates a Slack channel for
each new project that isn't in the denylist.

After creating a channel:
  1. Invites all entity members (from user-permissions.yaml) who have access.
  2. Posts a welcome message with the Asana project deep link.
  3. Saves the mapping to data/cache/project-channels.json.

Usage:
    .venv\\Scripts\\python.exe scripts\\run_project_channel_sync.py
    .venv\\Scripts\\python.exe scripts\\run_project_channel_sync.py --dry-run
    .venv\\Scripts\\python.exe scripts\\run_project_channel_sync.py --limit 5

Registered as Windows Task Scheduler task: cowork-cora-project-channel-sync
Schedule: Daily 9:00am AZ (16:00 UTC)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_REPO_ROOT / ".env", override=True)
sys.path.insert(0, str(_REPO_ROOT / "src"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            _REPO_ROOT / "logs" / "project-channel-sync.log",
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger("project-channel-sync")

_WORKSPACE_GID = "682743441507584"
_REGISTRY_PATH = _REPO_ROOT / "data" / "cache" / "project-channels.json"
_DENYLIST_PATH = _REPO_ROOT / "data" / "maps" / "project-channel-denylist.yaml"
_PERMISSIONS_PATH = _REPO_ROOT / "data" / "maps" / "user-permissions.yaml"

# Entity code prefix extraction — strips "[F3E]", "[OSN]" etc. from project names
_ENTITY_PREFIX_RE = re.compile(r"^\s*\[([A-Z0-9\-]+)\]\s*")


# ── Registry helpers ────────────────────────────────────────────────────────────

def _load_registry() -> dict:
    """Load {asana_gid: slack_channel_id} registry."""
    _REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        return json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_registry(registry: dict) -> None:
    _REGISTRY_PATH.write_text(json.dumps(registry, indent=2), encoding="utf-8")


# ── Denylist ────────────────────────────────────────────────────────────────────

def _load_denylist() -> list[str]:
    try:
        data = yaml.safe_load(_DENYLIST_PATH.read_text(encoding="utf-8")) or {}
        return [str(p).lower() for p in data.get("patterns", [])]
    except Exception as exc:
        log.warning("Could not load denylist: %s", exc)
        return []


def _is_denylisted(project_name: str, patterns: list[str]) -> bool:
    name_lower = project_name.lower()
    return any(p in name_lower for p in patterns)


# ── Channel name slug ───────────────────────────────────────────────────────────

def _slugify(project_name: str) -> str:
    """Convert an Asana project name to a valid Slack channel name.

    Rules: lowercase, hyphens, max 80 chars, strip entity prefix like [F3E].
    """
    # Strip entity prefix
    name = _ENTITY_PREFIX_RE.sub("", project_name).strip()
    # Lowercase, replace non-alphanumeric with hyphens
    name = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    # Collapse multiple hyphens
    name = re.sub(r"-{2,}", "-", name)
    return name[:80]


# ── Entity members ──────────────────────────────────────────────────────────────

def _load_entity_members(entity: str) -> list[str]:
    """Return Slack user IDs for users who have access to this entity."""
    try:
        data = yaml.safe_load(_PERMISSIONS_PATH.read_text(encoding="utf-8")) or {}
        users = data.get("users", [])
        members = []
        for user in users:
            allowed = user.get("allowed_entities", [])
            # Check both exact match and "ALL"
            if "ALL" in allowed or entity in allowed:
                sid = user.get("slack_user_id", "")
                if sid:
                    members.append(sid)
        return members
    except Exception as exc:
        log.warning("Could not load user permissions: %s", exc)
        return []


def _extract_entity(project_name: str) -> str:
    """Extract entity code from project name prefix like [F3E] → 'F3E'."""
    m = _ENTITY_PREFIX_RE.match(project_name)
    return m.group(1) if m else "FNDR"


# ── Asana project fetch ────────────────────────────────────────────────────────

def _fetch_asana_projects(asana_token: str) -> list[dict]:
    """Fetch all non-archived projects from the workspace."""
    try:
        import requests
        headers = {"Authorization": f"Bearer {asana_token}"}
        projects = []
        url = (
            f"https://app.asana.com/api/1.0/projects"
            f"?workspace={_WORKSPACE_GID}"
            f"&archived=false"
            f"&opt_fields=gid,name,created_at,archived"
            f"&limit=100"
        )
        while url:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            body = resp.json()
            projects.extend(body.get("data", []))
            next_page = (body.get("next_page") or {})
            url = next_page.get("uri")
        log.info("Fetched %d Asana projects", len(projects))
        return projects
    except Exception as exc:
        log.error("Asana project fetch failed: %s", exc)
        return []


# ── Slack channel creation ─────────────────────────────────────────────────────

def _create_channel_and_invite(
    client,
    channel_name: str,
    member_ids: list[str],
    project_name: str,
    project_gid: str,
    dry_run: bool,
) -> str | None:
    """Create a Slack channel, invite members, post welcome. Returns channel ID or None."""
    from slack_sdk.errors import SlackApiError

    if dry_run:
        log.info("[DRY RUN] Would create #%s and invite %d members", channel_name, len(member_ids))
        return f"DRY_RUN_{channel_name}"

    # Create channel
    ch_id = None
    try:
        resp = client.conversations_create(name=channel_name, is_private=False)
        ch_id = resp["channel"]["id"]
        log.info("Created #%s (%s)", channel_name, ch_id)
    except SlackApiError as exc:
        if exc.response.get("error") == "name_taken":
            # Try with -2 suffix
            alt_name = f"{channel_name}-2"
            try:
                resp = client.conversations_create(name=alt_name, is_private=False)
                ch_id = resp["channel"]["id"]
                log.info("Created #%s (%s) [name_taken fallback]", alt_name, ch_id)
            except Exception as inner_exc:
                log.warning("Channel creation failed for %s and %s: %s", channel_name, alt_name, inner_exc)
                return None
        else:
            log.warning("conversations.create failed for #%s: %s", channel_name, exc)
            return None

    if not ch_id:
        return None

    # Invite members in batches of 30 (Slack limit)
    if member_ids:
        for i in range(0, len(member_ids), 30):
            batch = member_ids[i:i + 30]
            try:
                client.conversations_invite(channel=ch_id, users=",".join(batch))
                log.info("Invited %d members to #%s", len(batch), channel_name)
            except SlackApiError as exc:
                if "already_in_channel" not in str(exc):
                    log.warning("conversations.invite failed for #%s: %s", channel_name, exc)
            time.sleep(0.5)  # Rate limit: Tier 2 = 20/min

    # Post welcome message
    asana_url = f"https://app.asana.com/0/{project_gid}/list"
    welcome = (
        f":tada: *New project channel for: {project_name}*\n\n"
        f"This channel was auto-created by Cora when the Asana project was detected.\n"
        f"• *Asana project:* <{asana_url}|{project_name}>\n"
        f"• *@mention Cora* here to ask project-specific questions.\n"
        f"• *Pin key decisions* by reacting 📚 to any message.\n\n"
        f"_To stop auto-creating channels for a project type, update `data/maps/project-channel-denylist.yaml`._"
    )
    try:
        client.chat_postMessage(
            channel=ch_id,
            text=welcome,
            unfurl_links=False,
            unfurl_media=False,
        )
    except Exception as exc:
        log.warning("Failed to post welcome to #%s: %s", channel_name, exc)

    return ch_id


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None, help="Max new channels to create per run")
    args = parser.parse_args()

    log.info("=== Project Channel Sync starting (dry_run=%s) ===", args.dry_run)

    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    asana_token = os.environ.get("ASANA_PAT", "")
    if not bot_token:
        log.error("SLACK_BOT_TOKEN not set")
        return 1
    if not asana_token:
        log.error("ASANA_PAT not set")
        return 1

    from slack_sdk import WebClient
    client = WebClient(token=bot_token)

    registry = _load_registry()
    denylist = _load_denylist()

    projects = _fetch_asana_projects(asana_token)
    if not projects:
        log.warning("No Asana projects fetched — exiting")
        return 0

    new_count = 0
    for project in projects:
        gid = project.get("gid", "")
        name = project.get("name", "").strip()

        if not gid or not name:
            continue
        if gid in registry:
            continue  # Already created
        if _is_denylisted(name, denylist):
            log.debug("Denylist skip: %s", name)
            continue

        entity = _extract_entity(name)
        channel_name = _slugify(name)
        if not channel_name:
            log.warning("Could not slugify project name '%s' — skipping", name)
            continue

        member_ids = _load_entity_members(entity)
        log.info("New project: %s → #%s (entity=%s, %d members)", name, channel_name, entity, len(member_ids))

        ch_id = _create_channel_and_invite(
            client=client,
            channel_name=channel_name,
            member_ids=member_ids,
            project_name=name,
            project_gid=gid,
            dry_run=args.dry_run,
        )

        if ch_id and not args.dry_run:
            registry[gid] = ch_id
            _save_registry(registry)
            log.info("Registry updated: %s → %s", gid, ch_id)

        new_count += 1
        if args.limit and new_count >= args.limit:
            log.info("Reached --limit %d — stopping", args.limit)
            break

        time.sleep(1.0)  # Pace between channel creations

    log.info("=== Project Channel Sync complete — %d new channels ===", new_count)
    return 0


if __name__ == "__main__":
    sys.exit(main())
