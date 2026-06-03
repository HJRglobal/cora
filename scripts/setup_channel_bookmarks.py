#!/usr/bin/env python3
"""Set up and maintain Slack channel bookmarks for all entity leadership channels.

Reads data/maps/channel-bookmarks.yaml and upserts bookmarks into each configured
channel. Idempotent — safe to re-run. Adds new bookmarks, updates changed ones,
leaves any extra bookmarks that are not in the config alone (no deletions unless
--prune is passed).

Usage:
    .venv\\Scripts\\python.exe scripts\\setup_channel_bookmarks.py
    .venv\\Scripts\\python.exe scripts\\setup_channel_bookmarks.py --dry-run
    .venv\\Scripts\\python.exe scripts\\setup_channel_bookmarks.py --channel C0B4KRQT3LY
    .venv\\Scripts\\python.exe scripts\\setup_channel_bookmarks.py --prune
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_REPO_ROOT / ".env", override=True)
sys.path.insert(0, str(_REPO_ROOT / "src"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)
log = logging.getLogger("setup-bookmarks")

_CONFIG_PATH = _REPO_ROOT / "data" / "maps" / "channel-bookmarks.yaml"


def _load_config() -> list[dict]:
    data = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    return data.get("channels", [])


def _get_existing_bookmarks(client, channel_id: str) -> list[dict]:
    """Fetch current bookmarks for a channel. Returns list of bookmark dicts."""
    try:
        resp = client.bookmarks_list(channel_id=channel_id)
        if not resp.get("ok"):
            log.warning("bookmarks.list failed for %s: %s", channel_id, resp.get("error"))
            return []
        return resp.get("bookmarks", [])
    except Exception as exc:
        log.warning("bookmarks.list error for %s: %s", channel_id, exc)
        return []


def _upsert_bookmarks(
    client,
    channel_id: str,
    channel_name: str,
    desired: list[dict],
    dry_run: bool,
    prune: bool,
) -> tuple[int, int, int]:
    """Add/update bookmarks for one channel. Returns (added, updated, pruned) counts."""
    existing = _get_existing_bookmarks(client, channel_id)
    existing_by_title = {b["title"]: b for b in existing}
    added = updated = pruned = 0

    for bm in desired:
        title = bm["title"]
        link = bm["link"]
        emoji = bm.get("emoji", "")

        if title in existing_by_title:
            ex = existing_by_title[title]
            if ex.get("link") != link:
                # Link changed — update
                if dry_run:
                    log.info("[DRY RUN] Would update bookmark '%s' in #%s", title, channel_name)
                else:
                    try:
                        client.bookmarks_edit(
                            bookmark_id=ex["id"],
                            channel_id=channel_id,
                            title=title,
                            link=link,
                        )
                        log.info("Updated bookmark '%s' in #%s", title, channel_name)
                        updated += 1
                    except Exception as exc:
                        log.warning("bookmarks.edit failed for '%s' in #%s: %s", title, channel_name, exc)
            else:
                log.debug("Bookmark '%s' in #%s unchanged — skipping", title, channel_name)
        else:
            # New bookmark — add
            if dry_run:
                log.info("[DRY RUN] Would add bookmark '%s' to #%s: %s", title, channel_name, link)
            else:
                try:
                    add_kwargs = {
                        "channel_id": channel_id,
                        "title": title,
                        "type": "link",
                        "link": link,
                    }
                    if emoji:
                        add_kwargs["emoji"] = emoji
                    client.bookmarks_add(**add_kwargs)
                    log.info("Added bookmark '%s' to #%s", title, channel_name)
                    added += 1
                except Exception as exc:
                    log.warning("bookmarks.add failed for '%s' in #%s: %s", title, channel_name, exc)

    if prune:
        desired_titles = {bm["title"] for bm in desired}
        for ex in existing:
            if ex["title"] not in desired_titles:
                if dry_run:
                    log.info("[DRY RUN] Would prune bookmark '%s' from #%s", ex["title"], channel_name)
                else:
                    try:
                        client.bookmarks_remove(bookmark_id=ex["id"], channel_id=channel_id)
                        log.info("Pruned bookmark '%s' from #%s", ex["title"], channel_name)
                        pruned += 1
                    except Exception as exc:
                        log.warning("bookmarks.remove failed for '%s' in #%s: %s", ex["title"], channel_name, exc)

    return added, updated, pruned


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Print planned changes without applying them")
    parser.add_argument("--channel", type=str, default=None, help="Limit to a single channel ID")
    parser.add_argument("--prune", action="store_true", help="Remove bookmarks not in the config")
    args = parser.parse_args()

    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not bot_token:
        log.error("SLACK_BOT_TOKEN not set")
        return 1

    from slack_sdk import WebClient
    client = WebClient(token=bot_token)

    config = _load_config()
    if not config:
        log.error("No channel configs found in %s", _CONFIG_PATH)
        return 1

    total_added = total_updated = total_pruned = 0

    for ch_config in config:
        channel_id = ch_config.get("channel_id", "")
        channel_name = ch_config.get("channel_name", channel_id)
        desired = ch_config.get("bookmarks", [])

        if args.channel and channel_id != args.channel:
            continue

        log.info("Processing #%s (%s) — %d desired bookmarks", channel_name, channel_id, len(desired))
        added, updated, pruned = _upsert_bookmarks(
            client, channel_id, channel_name, desired,
            dry_run=args.dry_run, prune=args.prune,
        )
        total_added += added
        total_updated += updated
        total_pruned += pruned

    log.info("Done. Added=%d Updated=%d Pruned=%d", total_added, total_updated, total_pruned)
    return 0


if __name__ == "__main__":
    sys.exit(main())
