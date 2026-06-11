#!/usr/bin/env python3
"""Daily per-user morning briefing -- org-roles-driven (Org Synthesis Phase 2, deliverable 2).

The briefing roster and role data come from data/maps/org-roles.yaml via
cora.org_roles (D-044: the canonical org role registry). The old per-user
briefing config YAML (data/maps) RETIRED in this deliverable -- this was the
locked consolidation point (D-044 item 5).

Per-user briefing content mirrors the whats_on_my_plate composite and REUSES
its section builders from tool_dispatch (no forked logic):
  - role + lanes (org-roles registry)
  - open Asana tasks, entity-scoped to the user's primary entity, capped at 10
  - today + tomorrow calendar
  - HubSpot deal pipeline for users who own a pipeline (LEX scope NEVER gets
    a pipeline section -- Tier-1 doctrine, enforced inside the shared builder)
  - stalled P0/P1 decisions -- Harrison only
plus the recent-activity KB scan (last 25h of Slack/Gmail/Fireflies/Notion
chunks mentioning the user) that the briefing has always carried.

Exclusion rules (fail-closed):
  - external consultants (external: true, e.g. Jason Dorfman) -- never receive
    internal proactive comms
  - registry-only people (no slack_id, e.g. Tessa Miller) -- no Slack identity
  - anyone NOT in the registry is skipped by construction (the registry IS the
    roster; no fallback path exists)

ROLLOUT DOCTRINE (locked 2026-06-11): digest-to-Harrison-first.
  - DEFAULT mode is --digest-only: Harrison gets ONE DM containing every
    user's would-be briefing for review. No per-user DMs are sent.
  - Per-user delivery requires the explicit --send-users flag, which flips on
    only after Harrison's explicit go (re-register the task with the flag, or
    setup-daily-briefing-task.ps1 -SendUsers).

Registered as Windows Task Scheduler task "Cora - Daily Briefing"
at 7:30am AZ, weekdays.

Exit codes: 0 = success, 1 = fatal error, 2 = partial (some users failed)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from slack_sdk import WebClient as SlackWebClient
from slack_sdk.errors import SlackApiError

load_dotenv()

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import org_roles  # noqa: E402
from cora.org_roles import RoleRecord  # noqa: E402

# Plate section builders -- SHARED with the whats_on_my_plate tool
# (tool_dispatch). Reused, not forked: any fix to plate scoping/fail-soft
# behavior applies to the briefing automatically.
from cora.tools.tool_dispatch import (  # noqa: E402
    _HARRISON_SLACK_ID,
    _plate_asana_section,
    _plate_calendar_section,
    _plate_hubspot_section,
    _safe_plate_section,
    _tool_fndr_open_decisions,
)

# ---- Configuration -----------------------------------------------------------

_KB_DB_PATH        = _REPO_ROOT / "data" / "cora_kb.db"
_LOOKBACK_SECONDS  = 25 * 3600
_MAX_CHUNKS        = 20    # per-user cap before sending to Haiku
_MAX_CHUNK_CHARS   = 500   # truncate long chunks
_DIGEST_CHUNK_CHARS = 3500  # split the Harrison digest into messages this size
_HAIKU_MODEL       = "claude-haiku-4-5-20251001"

# ---- Logging -----------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("run_daily_briefing")


# ---- Roster (org role registry, D-044) ----------------------------------------

def _load_briefing_roster() -> tuple[list[RoleRecord], list[str]]:
    """Active briefing recipients from the org role registry.

    Returns (roster, excluded_notes). Included: every registry entry with a
    slack_id and external != true. Excluded (logged + reported): external
    consultants and registry-only people. Unknown/unmapped users are skipped
    fail-closed by construction -- the registry IS the roster.
    """
    roster: list[RoleRecord] = []
    excluded: list[str] = []
    for rec in org_roles.all_roles():
        if not rec.slack_id:
            excluded.append(f"{rec.name} (registry-only, no Slack identity)")
            continue
        if rec.external:
            excluded.append(f"{rec.name} (external consultant -- no internal proactive comms)")
            continue
        roster.append(rec)
    return roster, excluded


# ---- Plate sections (shared builders) -----------------------------------------

def _compose_sections(rec: RoleRecord) -> str:
    """Compose the role-scoped plate sections for one user.

    Mirrors _tool_whats_on_my_plate's composition exactly (minus the tool's
    reply-format trailer): role header, open tasks, calendar, deal pipeline
    for owners (omitted for LEX scope inside the shared builder), stalled
    decisions for Harrison only. Every section is fail-soft.
    """
    sid = rec.slack_id
    entity = rec.entity

    header = [f"ROLE\n{rec.name} -- {rec.role} ({rec.entity})"]
    if rec.responsibilities:
        header.append("Lanes: " + "; ".join(rec.responsibilities))

    sections: list[str] = ["\n".join(header)]
    sections.append(
        "OPEN TASKS\n" + _safe_plate_section("Open tasks", _plate_asana_section, sid, entity)
    )
    sections.append(
        "CALENDAR\n" + _safe_plate_section("Calendar", _plate_calendar_section, sid)
    )
    try:
        deals = _plate_hubspot_section(sid, entity)
    except Exception:
        log.exception("briefing: deal pipeline section crashed for %s", rec.name)
        deals = "(Deal pipeline section unavailable right now.)"
    if deals is not None:
        sections.append("DEAL PIPELINE\n" + str(deals))
    if sid == _HARRISON_SLACK_ID:
        sections.append(
            "STALLED DECISIONS\n"
            + _safe_plate_section(
                "Stalled decisions", _tool_fndr_open_decisions, sid, entity, {}
            )
        )
    return "\n\n".join(sections)


# ---- KB chunk query (recent activity mentioning the user) ---------------------

def _query_user_chunks(display_name: str, first_name: str) -> list[dict]:
    """Fetch recent KB chunks that mention the user by full name or first name."""
    if not _KB_DB_PATH.exists():
        log.warning("KB not found at %s -- no context chunks available", _KB_DB_PATH)
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


# ---- Claude Haiku synthesis ----------------------------------------------------

def _synthesize(
    *,
    api_key: str,
    rec: RoleRecord,
    sections_text: str,
    chunks: list[dict],
    today_str: str,
) -> str:
    """Call Claude Haiku to turn the plate sections + recent activity into a DM."""
    first_name = rec.name.split()[0]

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
        f"Write a concise morning briefing DM for {rec.name}, whose role is: {rec.role}.\n\n"
        f"Today: {today_str}\n\n"
        f"== Their plate (role-scoped; sections below are authoritative) ==\n"
        f"{sections_text}\n\n"
        f"== Recent Activity Mentioning {first_name} (last 25h: Slack, Gmail, Meetings) ==\n"
        f"{context_text}\n\n"
        f"Instructions:\n"
        f"- Begin with: Good morning, {first_name}!\n"
        f"- Open with a one-line acknowledgment of their role and lanes where relevant\n"
        f"- List 2-4 open tasks needing attention today (prioritize overdue or due today); "
        f"preserve any <url|name> Slack links verbatim\n"
        f"- Mention today's calendar in one line if events exist\n"
        f"- If a DEAL PIPELINE section is present, give a 1-2 line role-relevant summary\n"
        f"- If a STALLED DECISIONS section is present, surface the 1-2 most urgent items\n"
        f"- Summarize 1-3 notable activity items that directly involve {first_name}\n"
        f"- End with a single-sentence offer to help\n"
        f"- Keep total under 320 words, plain text, no markdown headers or bullet symbols\n"
        f"- If no tasks AND no relevant activity, say it is a quiet start and offer to help\n"
        f"- Do NOT add financial figures not present above\n"
        f"- Do NOT fabricate tasks or events not shown above"
    )

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=_HAIKU_MODEL,
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()


def build_user_briefing(rec: RoleRecord, *, api_key: str, today_str: str) -> str:
    """Build the full briefing text for one registry user."""
    sections_text = _compose_sections(rec)
    chunks = _query_user_chunks(rec.name, rec.name.split()[0])
    return _synthesize(
        api_key=api_key,
        rec=rec,
        sections_text=sections_text,
        chunks=chunks,
        today_str=today_str,
    )


# ---- Digest composition --------------------------------------------------------

def _compose_digest_header(today_str: str, excluded: list[str]) -> str:
    lines = [
        f"DAILY BRIEFING DIGEST -- {today_str}",
        "Review mode: per-user delivery is OFF. Below is the briefing each user "
        "WOULD receive. To flip on per-user delivery after review, re-register "
        "the task with --send-users (setup-daily-briefing-task.ps1 -SendUsers).",
    ]
    if excluded:
        lines.append("Excluded from delivery: " + "; ".join(excluded) + ".")
    return "\n".join(lines)


def _chunk_digest(header: str, blocks: list[str]) -> list[str]:
    """Split the digest into Slack-friendly messages of ~_DIGEST_CHUNK_CHARS."""
    messages: list[str] = []
    cur = header
    for b in blocks:
        candidate = (cur + "\n\n" + b) if cur else b
        if cur and len(candidate) > _DIGEST_CHUNK_CHARS:
            messages.append(cur)
            cur = b
        else:
            cur = candidate
    if cur:
        messages.append(cur)
    return messages


# ---- Slack helpers -------------------------------------------------------------

def _open_dm(client: SlackWebClient, slack_user_id: str) -> str | None:
    try:
        resp = client.conversations_open(users=[slack_user_id])
        return resp["channel"]["id"]
    except SlackApiError as exc:
        log.warning("Could not open DM with %s: %s", slack_user_id, exc.response)
        return None


def _send_message(client: SlackWebClient, channel: str, text: str) -> bool:
    try:
        client.chat_postMessage(channel=channel, text=text)
        return True
    except SlackApiError as exc:
        log.warning("Failed to send message to %s: %s", channel, exc.response)
        return False


# ---- Audit log -----------------------------------------------------------------

def _write_audit(entries: list[dict]) -> None:
    log_path = _REPO_ROOT / "logs" / "cora-daily-briefing.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log_path.open("a", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(entry) + "\n")
    except OSError as exc:
        log.warning("Could not write briefing audit log: %s", exc)


# ---- CLI / main ----------------------------------------------------------------

def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Org-roles-driven daily briefing")
    p.add_argument(
        "--digest-only",
        action="store_true",
        help="(DEFAULT) build every user's briefing and DM Harrison ONE review "
             "digest; no per-user DMs are sent",
    )
    p.add_argument(
        "--send-users",
        action="store_true",
        help="DM each user their own briefing -- only after Harrison's explicit "
             "go on the digest output",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="build briefings and print to stdout; send nothing to Slack",
    )
    p.add_argument(
        "--user",
        default="",
        help="limit to one user (slack_id or case-insensitive name substring)",
    )
    args = p.parse_args(argv)
    if args.digest_only and args.send_users:
        p.error("--digest-only and --send-users are mutually exclusive")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    mode = "send" if args.send_users else "digest"
    log.info("=== Daily briefing starting (mode=%s, dry_run=%s) ===", mode, args.dry_run)

    slack_token   = os.environ.get("SLACK_BOT_TOKEN", "")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")

    if not anthropic_key:
        log.error("ANTHROPIC_API_KEY not set -- cannot generate briefings")
        return 1
    if not slack_token and not args.dry_run:
        log.error("SLACK_BOT_TOKEN not set -- cannot send briefings")
        return 1

    roster, excluded = _load_briefing_roster()
    for note in excluded:
        log.info("Excluded from briefing delivery: %s", note)
    if args.user:
        needle = args.user.strip().lower()
        roster = [
            r for r in roster
            if r.slack_id.lower() == needle or needle in r.name.lower()
        ]
    if not roster:
        log.warning("No briefing recipients in the org role registry -- nothing to do")
        return 0

    log.info("Building role-scoped briefings for %d registry users", len(roster))
    today_str = datetime.now().strftime("%A, %B %d, %Y")

    audit_entries: list[dict] = []
    errors = 0
    built: list[tuple[RoleRecord, str]] = []

    for rec in roster:
        log.info("Briefing %s (role=%s, entity=%s)...", rec.name, rec.role, rec.entity)
        try:
            text = build_user_briefing(rec, api_key=anthropic_key, today_str=today_str)
            built.append((rec, text))
        except Exception as exc:
            log.warning("Briefing build failed for %s: %s", rec.name, exc)
            errors += 1
            built.append((rec, f"(briefing could not be built: {exc})"))
            audit_entries.append({
                "ts": time.time(), "user": rec.name, "sid": rec.slack_id,
                "role": rec.role, "entity": rec.entity, "mode": mode,
                "sent": False, "error": str(exc),
            })

    if args.dry_run:
        for rec, text in built:
            print(f"\n=== {rec.name} ({rec.role}, {rec.entity}) ===\n{text}")
        log.info("=== Dry run done -- %d briefings built, %d errors ===", len(built), errors)
        return 0 if errors == 0 else 2

    slack_client = SlackWebClient(token=slack_token)

    if mode == "digest":
        header = _compose_digest_header(today_str, excluded)
        blocks = [
            f"=== {rec.name} ({rec.role}, {rec.entity}) ===\n{text}"
            for rec, text in built
        ]
        dest = _open_dm(slack_client, _HARRISON_SLACK_ID)
        if not dest:
            log.error("Could not open the Harrison digest DM -- nothing sent")
            return 1
        sent_all = True
        for msg in _chunk_digest(header, blocks):
            if not _send_message(slack_client, dest, msg):
                sent_all = False
                errors += 1
        audit_entries.append({
            "ts": time.time(), "user": "digest->Harrison", "sid": _HARRISON_SLACK_ID,
            "mode": mode, "users_included": len(built),
            "sent": sent_all, "error": None if sent_all else "partial_digest_send",
        })
        log.info("Digest sent to Harrison (%d user briefings, sent_ok=%s)", len(built), sent_all)
    else:
        for rec, text in built:
            dest = _open_dm(slack_client, rec.slack_id)
            if not dest:
                errors += 1
                audit_entries.append({
                    "ts": time.time(), "user": rec.name, "sid": rec.slack_id,
                    "role": rec.role, "entity": rec.entity, "mode": mode,
                    "sent": False, "error": "dm_open_failed",
                })
                continue
            sent = _send_message(slack_client, dest, text)
            if not sent:
                errors += 1
            audit_entries.append({
                "ts": time.time(), "user": rec.name, "sid": rec.slack_id,
                "role": rec.role, "entity": rec.entity, "mode": mode,
                "sent": sent, "error": None,
            })
            log.info("  %s -> %s sent: %s", rec.name, dest, sent)
            time.sleep(1)  # gentle Slack rate-limit buffer

    _write_audit(audit_entries)

    succeeded = len(roster) - errors
    log.info("=== Daily briefing done -- %d/%d succeeded ===", succeeded, len(roster))
    return 0 if errors == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
