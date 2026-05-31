#!/usr/bin/env python3
"""Daily per-user morning briefing — DMs each team member a personalized synthesis.

Pipeline per user:
  1. Fetch their open Asana tasks
  2. Query last 25h of KB (slack, gmail, fireflies, notion) for content mentioning them
  3. Call Claude Haiku to synthesize a concise 3-5 bullet briefing
  4. DM the user via Slack

Registered as Windows Task Scheduler task `cowork-cora-daily-briefing`
at 7:30am AZ (14:30 UTC).

Exit codes: 0 = success, 1 = fatal error, 2 = partial (some users failed)
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv
from slack_sdk import WebClient as SlackWebClient
from slack_sdk.errors import SlackApiError

load_dotenv()

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora.tools.asana_client import get_user_tasks, AsanaClientError  # noqa: E402

# ── Configuration ──────────────────────────────────────────────────────────────

_KB_DB_PATH       = _REPO_ROOT / "data" / "cora_kb.db"
_ASANA_MAP        = _REPO_ROOT / "data" / "maps" / "slack-to-asana.yaml"
_LOOKBACK_SECONDS = 25 * 3600
_MAX_TASKS        = 30
_MAX_CHUNKS       = 20    # per-user cap before sending to Haiku
_MAX_CHUNK_CHARS  = 500   # truncate long chunks

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("run_daily_briefing")


# ── User loading ───────────────────────────────────────────────────────────────

def _load_users() -> list[dict]:
    """Return users from slack-to-asana.yaml that have both a Slack ID and Asana GID."""
    try:
        data = yaml.safe_load(_ASANA_MAP.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        log.error("slack-to-asana.yaml not found at %s", _ASANA_MAP)
        return []
    except Exception as exc:
        log.error("Could not load slack-to-asana.yaml: %s", exc)
        return []

    users = []
    for entry in (data.get("users") or []):
        if not isinstance(entry, dict):
            continue
        sid  = entry.get("slack_user_id", "").strip()
        gid  = str(entry.get("asana_user_gid") or "").strip()
        name = entry.get("display_name", "").strip()
        if sid and gid and name:
            users.append({
                "slack_user_id": sid,
                "asana_user_gid": gid,
                "display_name": name,
                "first_name": name.split()[0],
            })
    return users


# ── Asana tasks ────────────────────────────────────────────────────────────────

def _fetch_tasks(asana_gid: str) -> list[dict]:
    try:
        return get_user_tasks(asana_gid, max_tasks=_MAX_TASKS)
    except AsanaClientError as exc:
        log.warning("Asana fetch failed for gid=%s: %s", asana_gid, exc)
        return []


def _format_task_line(task: dict) -> str:
    name = (task.get("name") or "Untitled task").strip()
    due  = task.get("due_on") or task.get("due_at", "")
    url  = task.get("permalink_url", "")
    label = f"{name} (due {due})" if due else name
    if url:
        return f"• {label}  — {url}"
    return f"• {label}"


# ── KB chunk query ─────────────────────────────────────────────────────────────

def _query_user_chunks(display_name: str, first_name: str) -> list[dict]:
    """Fetch recent KB chunks that mention the user by full name or first name."""
    if not _KB_DB_PATH.exists():
        log.warning("KB not found at %s — no context chunks available", _KB_DB_PATH)
        return []

    cutoff = int(time.time() - _LOOKBACK_SECONDS)
    conn = sqlite3.connect(str(_KB_DB_PATH))
    try:
        rows = conn.execute(
            """SELECT source, entity, title, content, deep_link
               FROM knowledge_chunks
               WHERE ingested_at >= ?
                 AND source IN ('slack', 'gmail', 'fireflies', 'notion')
                 AND (content LIKE ? OR content LIKE ?)
               ORDER BY ingested_at DESC
               LIMIT ?""",
            (cutoff, f"%{display_name}%", f"%{first_name}%", _MAX_CHUNKS),
        ).fetchall()
    finally:
        conn.close()

    return [
        {
            "source":    r[0],
            "entity":    r[1],
            "title":     (r[2] or "")[:80],
            "content":   (r[3] or "")[:_MAX_CHUNK_CHARS],
            "deep_link": r[4] or "",
        }
        for r in rows
    ]


# ── Claude Haiku synthesis ─────────────────────────────────────────────────────

def _build_briefing(
    *,
    api_key: str,
    display_name: str,
    first_name: str,
    tasks: list[dict],
    chunks: list[dict],
    today_str: str,
) -> str:
    """Call Claude Haiku to generate a personalized morning briefing."""
    tasks_text = (
        "\n".join(_format_task_line(t) for t in tasks)
        if tasks else "(no open tasks)"
    )

    chunk_lines = []
    for c in chunks:
        src     = c["source"].upper()
        ent     = c["entity"]
        title   = c["title"] or "(no title)"
        snippet = c["content"].replace("\n", " ")[:400]
        chunk_lines.append(f"[{src}/{ent}] {title}: {snippet}")

    context_text = "\n".join(chunk_lines) if chunk_lines else "(no recent activity found)"

    prompt = (
        f"You are Cora, an AI chief-of-staff assistant for HJR Global.\n"
        f"Write a concise morning briefing DM for {display_name}.\n\n"
        f"Today: {today_str}\n\n"
        f"== Open Asana Tasks ==\n{tasks_text}\n\n"
        f"== Recent Activity Mentioning {first_name} (last 25h: Slack, Gmail, Meetings) ==\n"
        f"{context_text}\n\n"
        f"Instructions:\n"
        f"- Begin: Good morning, {first_name}!\n"
        f"- List 2-4 open tasks that need attention (prioritize overdue or due today; skip routine ones)\n"
        f"- Summarize 1-3 notable activity items from the context that directly involve {first_name}\n"
        f"- End with a single-sentence offer to help\n"
        f"- Keep total under 280 words, plain text, no markdown headers\n"
        f"- If no tasks AND no relevant activity, say it's a quiet start and offer to help\n"
        f"- Do NOT fabricate tasks or events not shown above"
    )

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()


# ── Slack DM ───────────────────────────────────────────────────────────────────

def _open_dm(client: SlackWebClient, slack_user_id: str) -> str | None:
    try:
        resp = client.conversations_open(users=[slack_user_id])
        return resp["channel"]["id"]
    except SlackApiError as exc:
        log.warning("Could not open DM with %s: %s", slack_user_id, exc.response)
        return None


def _send_dm(client: SlackWebClient, dm_channel: str, text: str) -> bool:
    try:
        client.chat_postMessage(channel=dm_channel, text=text)
        return True
    except SlackApiError as exc:
        log.warning("Failed to send DM to channel %s: %s", dm_channel, exc.response)
        return False


# ── Audit log ──────────────────────────────────────────────────────────────────

def _write_audit(entries: list[dict]) -> None:
    log_path = _REPO_ROOT / "logs" / "cora-daily-briefing.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log_path.open("a", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(entry) + "\n")
    except OSError as exc:
        log.warning("Could not write briefing audit log: %s", exc)


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> int:
    log.info("=== Daily briefing starting ===")

    slack_token   = os.environ.get("SLACK_BOT_TOKEN", "")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")

    if not slack_token:
        log.error("SLACK_BOT_TOKEN not set — cannot send briefings")
        return 1
    if not anthropic_key:
        log.error("ANTHROPIC_API_KEY not set — cannot generate briefings")
        return 1

    users = _load_users()
    if not users:
        log.warning("No users found in slack-to-asana.yaml — nothing to brief")
        return 0

    log.info("Preparing briefings for %d users", len(users))
    slack_client = SlackWebClient(token=slack_token)
    today_str = datetime.now().strftime("%A, %B %d, %Y")  # "Friday, May 30, 2026"

    audit_entries: list[dict] = []
    errors = 0

    for user in users:
        name  = user["display_name"]
        sid   = user["slack_user_id"]
        gid   = user["asana_user_gid"]
        first = user["first_name"]

        log.info("Briefing %s (%s)...", name, sid)

        tasks  = _fetch_tasks(gid)
        chunks = _query_user_chunks(name, first)
        log.info("  %d open tasks, %d relevant KB chunks", len(tasks), len(chunks))

        try:
            briefing = _build_briefing(
                api_key=anthropic_key,
                display_name=name,
                first_name=first,
                tasks=tasks,
                chunks=chunks,
                today_str=today_str,
            )
        except Exception as exc:
            log.warning("Haiku synthesis failed for %s: %s", name, exc)
            errors += 1
            audit_entries.append({
                "ts": time.time(), "user": name, "sid": sid,
                "tasks": len(tasks), "chunks": len(chunks),
                "sent": False, "error": str(exc),
            })
            continue

        dm_channel = _open_dm(slack_client, sid)
        if not dm_channel:
            errors += 1
            audit_entries.append({
                "ts": time.time(), "user": name, "sid": sid,
                "tasks": len(tasks), "chunks": len(chunks),
                "sent": False, "error": "dm_open_failed",
            })
            continue

        sent = _send_dm(slack_client, dm_channel, briefing)
        if not sent:
            errors += 1

        audit_entries.append({
            "ts": time.time(), "user": name, "sid": sid,
            "tasks": len(tasks), "chunks": len(chunks),
            "sent": sent, "error": None,
        })
        log.info("  %s → DM sent: %s", name, sent)

        time.sleep(1)  # avoid Slack rate limits between users

    _write_audit(audit_entries)

    succeeded = len(users) - errors
    log.info("=== Daily briefing done — %d/%d succeeded ===", succeeded, len(users))
    return 0 if errors == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
