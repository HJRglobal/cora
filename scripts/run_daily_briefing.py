#!/usr/bin/env python3
"""Daily per-user morning briefing -- DMs each team member a personalized synthesis.

Pipeline per user:
  1. Load role config from data/maps/role-briefing-config.yaml
  2. Fetch their open Asana tasks (entity-filtered for non-FNDR roles)
  3. Query last 25h of KB (slack, gmail, fireflies, notion) for content mentioning them
  4. Pull optional role-specific data (HubSpot deals, financial snapshot, deal aging)
  5. Call Claude Haiku to synthesize a concise, role-aware briefing
  6. DM the user via Slack (or post to briefing_channel if configured)

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

# ---- Configuration -----------------------------------------------------------

_KB_DB_PATH        = _REPO_ROOT / "data" / "cora_kb.db"
_ASANA_MAP         = _REPO_ROOT / "data" / "maps" / "slack-to-asana.yaml"
_ROLE_CONFIG       = _REPO_ROOT / "data" / "maps" / "role-briefing-config.yaml"
_LOOKBACK_SECONDS  = 25 * 3600
_MAX_TASKS         = 30
_MAX_CHUNKS        = 20    # per-user cap before sending to Haiku
_MAX_CHUNK_CHARS   = 500   # truncate long chunks
_ENTITY_TASK_LIMIT = 15    # max tasks shown for non-FNDR entity roles

# Asana project prefix filter per entity (mirrors tool_dispatch.ENTITY_PROJECT_PREFIXES)
_ENTITY_PREFIXES: dict[str, list[str]] = {
    "F3E":     ["[F3E]", "[F3 ", "[F3-", "[F3C]"],
    "LEX":     ["[LEX]", "[LEX-"],
    "OSN":     ["[OSN]"],
    "BDM":     ["[BDM]"],
    "UFL":     ["[UFL]"],
    "HJRP":    ["[HJRP]", "[HJRP-"],
    "HJRPROD": ["[HJRPROD]", "[POD]", "[FF]", "[HJR-PB]", "[CHK]", "[CHB]"],
    "HJRG":    ["[HJRG]"],
    "FNDR":    [],  # no filter -- see all tasks
}

# ---- Logging -----------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("run_daily_briefing")


# ---- User + role loading -----------------------------------------------------

def _load_asana_users() -> dict[str, dict]:
    """Return a dict keyed by slack_user_id from slack-to-asana.yaml."""
    try:
        data = yaml.safe_load(_ASANA_MAP.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        log.error("slack-to-asana.yaml not found at %s", _ASANA_MAP)
        return {}
    users = {}
    for entry in (data.get("users") or []):
        if not isinstance(entry, dict):
            continue
        sid  = entry.get("slack_user_id", "").strip()
        gid  = str(entry.get("asana_user_gid") or "").strip()
        name = entry.get("display_name", "").strip()
        if sid and gid and name:
            users[sid] = {
                "slack_user_id": sid,
                "asana_user_gid": gid,
                "display_name": name,
                "first_name": name.split()[0],
            }
    return users


def _load_role_config() -> dict[str, dict]:
    """Return a dict keyed by slack_user_id from role-briefing-config.yaml.

    Falls back to empty dict if file is missing -- all users will receive
    the generic briefing with entity=FNDR and no extra data sources.
    """
    if not _ROLE_CONFIG.exists():
        log.warning(
            "role-briefing-config.yaml not found at %s -- using generic briefings", _ROLE_CONFIG
        )
        return {}
    try:
        data = yaml.safe_load(_ROLE_CONFIG.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        log.warning("Could not load role-briefing-config.yaml: %s -- using generic briefings", exc)
        return {}
    config: dict[str, dict] = {}
    for entry in (data.get("users") or []):
        if not isinstance(entry, dict):
            continue
        sid = entry.get("slack_user_id", "").strip()
        if sid:
            config[sid] = entry
    return config


def _load_users() -> list[dict]:
    """Merge Asana user records with role config. Returns list of merged user dicts."""
    asana_users = _load_asana_users()
    role_config = _load_role_config()

    merged = []
    for sid, asana_data in asana_users.items():
        role_entry = role_config.get(sid, {})
        if role_entry.get("skip_briefing"):
            log.info("Skipping %s (skip_briefing=true)", asana_data["display_name"])
            continue
        merged.append({
            **asana_data,
            "role":             role_entry.get("role", "Team Member"),
            "entity":           role_entry.get("entity", "FNDR"),
            "extra_data":       role_entry.get("extra_data") or [],
            "briefing_channel": (role_entry.get("briefing_channel") or "").strip(),
        })
    return merged


# ---- Asana tasks (entity-filtered) ------------------------------------------

def _fetch_tasks(asana_gid: str, entity: str) -> list[dict]:
    """Fetch Asana tasks for a user, filtered by entity if not FNDR."""
    try:
        tasks = get_user_tasks(asana_gid, max_tasks=_MAX_TASKS)
    except AsanaClientError as exc:
        log.warning("Asana fetch failed for gid=%s: %s", asana_gid, exc)
        return []

    if entity == "FNDR" or entity not in _ENTITY_PREFIXES:
        return tasks

    prefixes = _ENTITY_PREFIXES[entity]
    if not prefixes:
        return tasks

    filtered = []
    for t in tasks:
        memberships = t.get("memberships") or []
        proj_names = [(m.get("project") or {}).get("name", "") for m in memberships]
        proj_names += [p.get("name", "") for p in (t.get("projects") or [])]
        if any(pn.startswith(pfx) for pn in proj_names for pfx in prefixes):
            filtered.append(t)
    return filtered[:_ENTITY_TASK_LIMIT]


def _format_task_line(task: dict) -> str:
    name  = (task.get("name") or "Untitled task").strip()
    due   = task.get("due_on") or task.get("due_at", "")
    url   = task.get("permalink_url", "")
    label = f"{name} (due {due})" if due else name
    if url:
        return f"- {label}  ({url})"
    return f"- {label}"


# ---- KB chunk query ----------------------------------------------------------

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


# ---- Role-specific data pulls ------------------------------------------------

def _fetch_hubspot_f3e_summary() -> str:
    try:
        from cora.tools.hubspot_client import get_f3e_pipeline_summary_text
        return get_f3e_pipeline_summary_text()
    except Exception as exc:
        log.warning("HubSpot F3E pull failed: %s", exc)
        return "(HubSpot F3E data unavailable)"


def _fetch_hubspot_all_summary() -> str:
    try:
        from cora.tools.hubspot_client import (
            get_f3e_pipeline_summary_text,
            get_deals_by_pipeline,
            PIPELINE_UFL_OSN_BDM,
        )
        f3e_text = get_f3e_pipeline_summary_text()
        other_deals = get_deals_by_pipeline(PIPELINE_UFL_OSN_BDM)
        other_count = len(other_deals)
        return f"{f3e_text}\n\nDefault pipeline: {other_count} active deal(s)."
    except Exception as exc:
        log.warning("HubSpot all-pipeline pull failed: %s", exc)
        return "(HubSpot data unavailable)"


def _fetch_financial_snapshot() -> str:
    try:
        from cora.connectors.gsheets_financials import get_cashflow_for_entity
        result = get_cashflow_for_entity("FNDR")
        if isinstance(result, str):
            return result[:600]
        return "(financial snapshot unavailable)"
    except Exception as exc:
        log.warning("Financial snapshot pull failed: %s", exc)
        return "(financial data unavailable)"


def _fetch_deal_aging_summary() -> str:
    """Summarize deals currently exceeding age thresholds."""
    try:
        db_path = _REPO_ROOT / "data" / "hubspot_deal_snapshots.db"
        if not db_path.exists():
            return "(no deal snapshot data yet)"
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT deal_name, stage_name, last_seen_ts "
                "FROM deal_last_stage ORDER BY last_seen_ts ASC LIMIT 20"
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return "(no active deals in snapshot)"
        now = time.time()
        thresholds = {
            "Identify": 14, "Outreach": 10, "Sample Sent": 7,
            "Qualified": 21, "Proposal": 14, "Negotiation": 7,
        }
        aging_lines = []
        for deal_name, stage_name, last_seen_ts in rows:
            age_days = int((now - (last_seen_ts or now)) / 86400)
            threshold = thresholds.get(stage_name, 21)
            if age_days >= threshold:
                aging_lines.append(
                    f"- {deal_name} ({stage_name}, {age_days}d -- threshold {threshold}d)"
                )
        if not aging_lines:
            return "(no deals currently exceeding age thresholds)"
        return "Aging deals:\n" + "\n".join(aging_lines)
    except Exception as exc:
        log.warning("Deal aging summary failed: %s", exc)
        return "(deal aging data unavailable)"


_EXTRA_DATA_FETCHERS: dict[str, object] = {
    "hubspot_f3e": _fetch_hubspot_f3e_summary,
    "hubspot_all": _fetch_hubspot_all_summary,
    "financial":   _fetch_financial_snapshot,
    "deal_aging":  _fetch_deal_aging_summary,
}

_EXTRA_DATA_LABELS: dict[str, str] = {
    "hubspot_f3e": "F3E Sales Pipeline (HubSpot)",
    "hubspot_all": "Sales Pipelines Overview (HubSpot)",
    "financial":   "Cash Flow Snapshot",
    "deal_aging":  "Deal Aging Alerts",
}


def _fetch_extra_data(extra_data: list[str]) -> dict[str, str]:
    """Fetch all requested extra data sources. Returns label -> content dict."""
    result: dict[str, str] = {}
    for key in extra_data:
        fetcher = _EXTRA_DATA_FETCHERS.get(key)
        if callable(fetcher):
            label = _EXTRA_DATA_LABELS.get(key, key)
            result[label] = fetcher()
        else:
            log.warning("Unknown extra_data key: %s", key)
    return result


# ---- Claude Haiku synthesis --------------------------------------------------

def _build_briefing(
    *,
    api_key: str,
    display_name: str,
    first_name: str,
    role: str,
    entity: str,
    tasks: list[dict],
    chunks: list[dict],
    extra_data: dict[str, str],
    today_str: str,
) -> str:
    """Call Claude Haiku to generate a personalized, role-aware morning briefing."""
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

    extra_sections = ""
    for label, content in extra_data.items():
        extra_sections += f"\n== {label} ==\n{content}\n"

    entity_note = f" (filtered to {entity} projects)" if entity != "FNDR" else ""

    prompt = (
        f"You are Cora, an AI chief-of-staff assistant for HJR Global.\n"
        f"Write a concise morning briefing DM for {display_name}, whose role is: {role}.\n\n"
        f"Today: {today_str}\n\n"
        f"== Open Tasks{entity_note} ==\n{tasks_text}\n\n"
        f"== Recent Activity Mentioning {first_name} (last 25h: Slack, Gmail, Meetings) ==\n"
        f"{context_text}{extra_sections}\n"
        f"Instructions:\n"
        f"- Begin with: Good morning, {first_name}!\n"
        f"- Briefly acknowledge their role context where relevant\n"
        f"- List 2-4 open tasks needing attention today (prioritize overdue or due today)\n"
        f"- Summarize 1-3 notable activity items from context that directly involve {first_name}\n"
        f"- If extra data sections present (HubSpot, cash flow, deal aging), include a 1-2 line "
        f"  role-relevant summary\n"
        f"- End with a single-sentence offer to help\n"
        f"- Keep total under 320 words, plain text, no markdown headers or bullet symbols\n"
        f"- If no tasks AND no relevant activity, say it is a quiet start and offer to help\n"
        f"- Do NOT fabricate tasks or events not shown above"
    )

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()


# ---- Slack helpers -----------------------------------------------------------

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


# ---- Audit log ---------------------------------------------------------------

def _write_audit(entries: list[dict]) -> None:
    log_path = _REPO_ROOT / "logs" / "cora-daily-briefing.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log_path.open("a", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(entry) + "\n")
    except OSError as exc:
        log.warning("Could not write briefing audit log: %s", exc)


# ---- Main --------------------------------------------------------------------

def main() -> int:
    log.info("=== Daily briefing starting ===")

    slack_token   = os.environ.get("SLACK_BOT_TOKEN", "")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")

    if not slack_token:
        log.error("SLACK_BOT_TOKEN not set -- cannot send briefings")
        return 1
    if not anthropic_key:
        log.error("ANTHROPIC_API_KEY not set -- cannot generate briefings")
        return 1

    users = _load_users()
    if not users:
        log.warning("No users found -- nothing to brief")
        return 0

    log.info("Preparing role-aware briefings for %d users", len(users))
    slack_client = SlackWebClient(token=slack_token)
    today_str = datetime.now().strftime("%A, %B %d, %Y")

    audit_entries: list[dict] = []
    errors = 0

    for user in users:
        name   = user["display_name"]
        sid    = user["slack_user_id"]
        gid    = user["asana_user_gid"]
        first  = user["first_name"]
        role   = user["role"]
        entity = user["entity"]
        extra  = user["extra_data"]
        b_chan = user["briefing_channel"]

        log.info("Briefing %s (role=%s, entity=%s)...", name, role, entity)

        tasks      = _fetch_tasks(gid, entity)
        chunks     = _query_user_chunks(name, first)
        extra_data = _fetch_extra_data(extra)
        log.info(
            "  %d tasks (entity=%s), %d chunks, %d extra sources",
            len(tasks), entity, len(chunks), len(extra_data),
        )

        try:
            briefing = _build_briefing(
                api_key=anthropic_key,
                display_name=name,
                first_name=first,
                role=role,
                entity=entity,
                tasks=tasks,
                chunks=chunks,
                extra_data=extra_data,
                today_str=today_str,
            )
        except Exception as exc:
            log.warning("Haiku synthesis failed for %s: %s", name, exc)
            errors += 1
            audit_entries.append({
                "ts": time.time(), "user": name, "sid": sid, "role": role,
                "entity": entity, "tasks": len(tasks), "chunks": len(chunks),
                "sent": False, "error": str(exc),
            })
            continue

        # Resolve delivery channel
        if b_chan:
            dest = b_chan
        else:
            dest = _open_dm(slack_client, sid)
            if not dest:
                errors += 1
                audit_entries.append({
                    "ts": time.time(), "user": name, "sid": sid, "role": role,
                    "entity": entity, "tasks": len(tasks), "chunks": len(chunks),
                    "sent": False, "error": "dm_open_failed",
                })
                continue

        sent = _send_message(slack_client, dest, briefing)
        if not sent:
            errors += 1

        audit_entries.append({
            "ts": time.time(), "user": name, "sid": sid, "role": role,
            "entity": entity, "tasks": len(tasks), "chunks": len(chunks),
            "extra_data_keys": list(extra_data.keys()),
            "sent": sent, "error": None,
        })
        log.info("  %s -> %s sent: %s", name, dest, sent)

        time.sleep(1)  # gentle Slack rate-limit buffer

    _write_audit(audit_entries)

    succeeded = len(users) - errors
    log.info("=== Daily briefing done -- %d/%d succeeded ===", succeeded, len(users))
    return 0 if errors == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
