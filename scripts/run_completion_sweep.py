#!/usr/bin/env python3
"""Daily completion sweep — cross-reference recent KB signals against open Asana tasks.

Runs once per day (registered via Windows Task Scheduler as
`cowork-cora-completion-sweep` at 7:00 AM AZ / 14:00 UTC).

Pipeline:
  1. Load all Asana user GIDs from slack-to-asana.yaml
  2. Fetch open tasks for each user (capped to avoid rate limits)
  3. Run completion_detector.detect_candidates() against last 25h of KB chunks
  4. If candidates found, post digest to #hjrg-leadership via Slack
  5. Mark candidates as sent (dedup so they don't resurface tomorrow)

Exits with code 0 on success or empty result, 1 on hard error.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

import yaml
from slack_sdk import WebClient as SlackWebClient
from slack_sdk.errors import SlackApiError

# Add the repo src/ to sys.path so we can import cora without installation.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora.tools import asana_client, completion_detector  # noqa: E402

# ── Configuration ──────────────────────────────────────────────────────────

_NOTIFY_CHANNEL = "hjrg-leadership"

# Pull tasks for up to this many users in one sweep (rate-limit guard).
_MAX_USERS = 15
_MAX_TASKS_PER_USER = 50

# ── Logging ────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("run_completion_sweep")


# ── Audit log ─────────────────────────────────────────────────────────────

def _write_audit(*, candidates: int, posted: bool, error: str | None) -> None:
    log_path = _REPO_ROOT / "logs" / "cora-completion-sweep.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": time.time(),
        "candidates": candidates,
        "posted": posted,
        "error": error,
    }
    try:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError as exc:
        log.warning("Could not write sweep audit log: %s", exc)


# ── User map loading ───────────────────────────────────────────────────────

def _load_asana_gids() -> list[str]:
    """Return all unique Asana GIDs from slack-to-asana.yaml."""
    map_path = _REPO_ROOT / "data" / "maps" / "slack-to-asana.yaml"
    try:
        data = yaml.safe_load(map_path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        log.warning("slack-to-asana.yaml not found — no user GIDs loaded")
        return []
    except Exception as exc:
        log.warning("Could not load slack-to-asana.yaml: %s", exc)
        return []

    gids: list[str] = []
    seen: set[str] = set()
    users = data.get("users", []) if isinstance(data, dict) else []
    for entry in users:
        if not isinstance(entry, dict):
            continue
        gid = entry.get("asana_user_gid") or entry.get("asana_gid")
        if gid and str(gid) not in seen:
            seen.add(str(gid))
            gids.append(str(gid))
    return gids


# ── Task fetching ──────────────────────────────────────────────────────────

def _fetch_all_open_tasks(asana_gids: list[str]) -> list[dict]:
    """Fetch open tasks for up to _MAX_USERS users, pooling results."""
    all_tasks: list[dict] = []
    seen_gids: set[str] = set()
    errors = 0

    for gid in asana_gids[:_MAX_USERS]:
        try:
            tasks = asana_client.get_user_tasks(gid, max_tasks=_MAX_TASKS_PER_USER)
            for t in tasks:
                task_gid = t.get("gid")
                if task_gid and task_gid not in seen_gids:
                    seen_gids.add(task_gid)
                    all_tasks.append(t)
        except asana_client.AsanaClientError as exc:
            log.warning("Asana error for gid=%s: %s", gid, exc)
            errors += 1

    log.info(
        "Fetched %d unique open tasks from %d users (%d Asana errors)",
        len(all_tasks), min(len(asana_gids), _MAX_USERS), errors,
    )
    return all_tasks


# ── Slack post ─────────────────────────────────────────────────────────────

def _post_to_slack(message: str) -> bool:
    """Post the digest to #hjrg-leadership. Returns True on success."""
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        log.error("SLACK_BOT_TOKEN not set — cannot post digest")
        return False
    client = SlackWebClient(token=token)
    try:
        client.chat_postMessage(channel=_NOTIFY_CHANNEL, text=message)
        log.info("Digest posted to #%s", _NOTIFY_CHANNEL)
        return True
    except SlackApiError as exc:
        log.error("Slack API error posting digest: %s", exc.response)
        return False


# ── Main ───────────────────────────────────────────────────────────────────

def main() -> int:
    log.info("=== Completion sweep starting ===")

    # 1. Load user GIDs
    asana_gids = _load_asana_gids()
    if not asana_gids:
        log.warning("No Asana GIDs found — nothing to sweep")
        _write_audit(candidates=0, posted=False, error="no_asana_gids")
        return 0

    # 2. Fetch open tasks
    open_tasks = _fetch_all_open_tasks(asana_gids)
    if not open_tasks:
        log.info("No open tasks found — sweep complete (nothing to match against)")
        _write_audit(candidates=0, posted=False, error=None)
        return 0

    # 3. Run detection — all entities, last 25h, dedup enforced
    try:
        candidates = completion_detector.detect_candidates(
            open_tasks,
            entities=None,  # cross-entity sweep
            apply_dedup=True,
        )
    except Exception as exc:
        log.exception("Detection failed: %s", exc)
        _write_audit(candidates=0, posted=False, error=str(exc))
        return 1

    log.info("Detection complete: %d candidates above threshold", len(candidates))

    # 4. Build and post digest
    digest = completion_detector.format_sweep_digest(candidates)

    # Always post — even the "nothing found" message is useful signal.
    posted = _post_to_slack(digest)

    # 5. Mark sent (dedup for next 48h)
    if candidates:
        completion_detector.mark_candidates_sent(candidates)

    _write_audit(candidates=len(candidates), posted=posted, error=None if posted else "slack_error")
    log.info("=== Completion sweep done — %d candidates, posted=%s ===", len(candidates), posted)
    return 0 if posted else 1


if __name__ == "__main__":
    # Load .env if available (for local / non-service runs)
    _env_path = _REPO_ROOT / ".env"
    if _env_path.exists():
        for line in _env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

    sys.exit(main())
