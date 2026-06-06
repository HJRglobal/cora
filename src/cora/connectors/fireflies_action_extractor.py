"""Fireflies Meeting Action Item Extractor -- auto-capture action items from meetings.

After each meeting completes in Fireflies, this module:
1. Fetches transcripts since last watermark
2. Uses Claude Haiku to parse action_items text into structured tasks
3. Creates Asana tasks for each action item (assignee resolved from attendee emails)
4. Posts a digest to the entity's leadership Slack channel

PHI guardrail: LEX meetings are always skipped entirely.
Watermark: data/state/meeting_action_watermark.json stores last processed timestamp.

Usage:
    from cora.connectors.fireflies_action_extractor import run_action_capture
    result = run_action_capture(dry_run=False)
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anthropic
import httpx
import yaml

from cora.connectors.fireflies_connector import (
    _TRANSCRIPTS_QUERY,
    _classify_entity,
    _graphql_query,
    _is_phi_meeting,
    _parse_date,
    FirefliesConnectorError,
)
from cora.tools.asana_client import AsanaClientError, create_task

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WATERMARK_PATH = _REPO_ROOT / "data" / "state" / "meeting_action_watermark.json"
_ASANA_MAP_PATH = _REPO_ROOT / "data" / "maps" / "slack-to-asana.yaml"
_DEFAULT_LOOKBACK_HOURS = 24

# Entity -> leadership Slack channel mapping (LEX is intentionally absent -- PHI)
_ENTITY_CHANNEL: dict[str, str] = {
    "F3E":     "#f3-leadership",
    "OSN":     "#osn-leadership",
    "BDM":     "#bdm-leadership",
    "UFL":     "#ufl-leadership",
    "HJRP":    "#hjrp-leadership",
    "HJRPROD": "#hjrprod-leadership",
    "HJRG":    "#hjrg-leadership",
    "FNDR":    "#fndr",
    "F3C":     "#fndr",  # route F3 Community to founder channel
}

# Asana task notes template
_TASK_NOTES_TEMPLATE = (
    "Auto-captured from Fireflies meeting: {meeting_title}\n"
    "Date: {meeting_date}\n"
    "Original action item text: {raw_text}"
)

# Haiku model for parsing (cost-efficient)
_HAIKU_MODEL = "claude-haiku-4-5"

# Haiku prompt for action item parsing
_PARSE_PROMPT = """You are parsing meeting action items into structured tasks.

Given the following raw action items text from a meeting, extract each distinct task
and return a JSON array. Each element must have exactly these fields:
  - "task": a clear, concise task description (string)
  - "assignee_name": the person responsible by name (string or null if unclear)
  - "due_mention": any date or timeframe mentioned for this task (string or null)

Return ONLY valid JSON -- no markdown, no explanation. Example:
[
  {{"task": "Send proposal to client", "assignee_name": "Tommy", "due_mention": "by Friday"}},
  {{"task": "Update inventory spreadsheet", "assignee_name": null, "due_mention": null}}
]

Action items text:
{action_items_text}"""


# ---------------------------------------------------------------------------
# Watermark management
# ---------------------------------------------------------------------------

def _read_watermark() -> tuple[int, set[str]]:
    """Read last-processed timestamp + set of processed transcript IDs.

    Returns (timestamp, processed_ids_set).
    Timestamp defaults to 24 hours ago if file is missing or corrupt.
    processed_ids prevents reprocessing the same transcript regardless of
    timestamp -- fixes the bug where meeting_ts == since_ts and the watermark
    never advances, causing the same transcript to post to Slack every hour.
    """
    try:
        if _WATERMARK_PATH.exists():
            data = json.loads(_WATERMARK_PATH.read_text(encoding="utf-8"))
            ts = data.get("last_processed_ts")
            ids = set(data.get("processed_ids") or [])
            if isinstance(ts, (int, float)) and ts > 0:
                return int(ts), ids
    except Exception as exc:
        log.warning("Could not read action watermark: %s", exc)
    # Default: 24 hours ago, no processed IDs
    return int(time.time()) - (_DEFAULT_LOOKBACK_HOURS * 3600), set()


def _write_watermark(ts: int, processed_ids: set[str]) -> None:
    """Write last-processed timestamp + processed transcript IDs to watermark file.

    Keeps only the last 200 transcript IDs to bound file size.
    """
    try:
        _WATERMARK_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Cap to 200 most-recent IDs (arbitrary order -- just prevents unbounded growth)
        ids_list = list(processed_ids)[-200:]
        _WATERMARK_PATH.write_text(
            json.dumps({"last_processed_ts": ts, "processed_ids": ids_list}, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        log.error("Could not write action watermark: %s", exc)


# ---------------------------------------------------------------------------
# Asana user map (email -> asana_user_gid)
# ---------------------------------------------------------------------------

_email_to_asana_gid: dict[str, str] | None = None  # module-level cache


def _load_email_to_asana_gid() -> dict[str, str]:
    """Build email -> Asana GID map from slack-to-asana.yaml."""
    global _email_to_asana_gid
    if _email_to_asana_gid is not None:
        return _email_to_asana_gid

    try:
        data = yaml.safe_load(_ASANA_MAP_PATH.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        log.warning("Could not load slack-to-asana.yaml: %s", exc)
        _email_to_asana_gid = {}
        return _email_to_asana_gid

    result: dict[str, str] = {}
    for entry in (data.get("users") or []):
        if not isinstance(entry, dict):
            continue
        gid = str(entry.get("asana_user_gid", "")).strip()
        if not gid:
            continue
        primary = entry.get("asana_email", "").strip().lower()
        if primary:
            result[primary] = gid
        for alias in (entry.get("email_aliases") or []):
            if alias:
                result[str(alias).strip().lower()] = gid

    _email_to_asana_gid = result
    return _email_to_asana_gid


def _resolve_assignee_gid(
    assignee_name: str | None,
    attendees: list[dict],
) -> str | None:
    """Resolve assignee GID from name + attendee list via email map.

    Matches assignee_name (case-insensitive substring) against attendee displayNames,
    then looks up email in slack-to-asana.yaml.
    """
    if not assignee_name:
        return None

    email_map = _load_email_to_asana_gid()
    name_lower = assignee_name.lower().strip()

    for attendee in attendees:
        display = (attendee.get("displayName") or "").lower()
        email = (attendee.get("email") or "").strip().lower()
        if name_lower in display or display in name_lower:
            gid = email_map.get(email)
            if gid:
                return gid

    # Fallback: direct email match if assignee_name looks like an email
    if "@" in assignee_name:
        return email_map.get(assignee_name.lower())

    return None


# ---------------------------------------------------------------------------
# Claude Haiku parsing
# ---------------------------------------------------------------------------

def _parse_action_items_with_haiku(action_items_text: str) -> list[dict[str, Any]]:
    """Use Claude Haiku to parse raw action_items text into structured task list.

    Returns list of dicts with keys: task, assignee_name, due_mention.
    Returns empty list if parsing fails or text is empty.
    """
    if not action_items_text or not action_items_text.strip():
        return []

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        log.error("ANTHROPIC_API_KEY not set -- cannot parse action items")
        return []

    client = anthropic.Anthropic(api_key=api_key)
    prompt = _PARSE_PROMPT.format(action_items_text=action_items_text.strip())

    try:
        response = client.messages.create(
            model=_HAIKU_MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(
                line for line in lines
                if not line.startswith("```")
            ).strip()
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            log.warning("Haiku returned non-list JSON: %r", raw[:200])
            return []
        # Validate each item has required fields
        result = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            task = str(item.get("task") or "").strip()
            if not task:
                continue
            result.append({
                "task": task,
                "assignee_name": item.get("assignee_name") or None,
                "due_mention": item.get("due_mention") or None,
            })
        return result
    except json.JSONDecodeError as exc:
        log.warning("Haiku JSON parse failed: %s", exc)
        return []
    except Exception as exc:
        log.error("Haiku action item parsing error: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Slack notification
# ---------------------------------------------------------------------------

def _post_slack_summary(
    channel: str,
    meeting_title: str,
    created_tasks: list[dict[str, Any]],
    dry_run: bool = False,
) -> None:
    """Post a meeting action item digest to a Slack channel.

    Each created_task dict should have: task_name, assignee_name, permalink_url.
    """
    if not created_tasks:
        return

    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        log.warning("SLACK_BOT_TOKEN not set -- skipping Slack notification")
        return

    lines = [f":calendar: *Action items captured from {meeting_title}*", ""]
    n = len(created_tasks)
    lines.append(f"{n} task{'s' if n != 1 else ''} created in Asana:")

    for t in created_tasks:
        url = t.get("permalink_url", "")
        name = t.get("task_name", "")
        assignee = t.get("assignee_name", "")
        assignee_str = f" (-> {assignee})" if assignee else ""
        link = f"<{url}|open>" if url else ""
        link_str = f" {link}" if link else ""
        lines.append(f"  - {name}{assignee_str}{link_str}")

    text = "\n".join(lines)
    log.info("Posting action digest to %s: %d tasks", channel, n)

    if dry_run:
        log.info("[DRY RUN] Would post to %s:\n%s", channel, text)
        return

    try:
        with httpx.Client(timeout=10.0) as c:
            r = c.post(
                "https://slack.com/api/chat.postMessage",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={"channel": channel, "text": text},
            )
        data = r.json()
        if not data.get("ok"):
            log.warning("Slack post failed for %s: %s", channel, data.get("error"))
    except Exception as exc:
        log.error("Slack notification error for %s: %s", channel, exc)


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run_action_capture(dry_run: bool = False) -> dict[str, Any]:
    """Fetch new transcripts, parse action items, create Asana tasks, post Slack digest.

    Returns:
        {
            "meetings_processed": int,
            "tasks_created": int,
            "errors": list[str],
        }
    """
    result: dict[str, Any] = {
        "meetings_processed": 0,
        "tasks_created": 0,
        "errors": [],
    }

    since_ts, processed_ids = _read_watermark()
    since_dt = datetime.fromtimestamp(since_ts, tz=timezone.utc)
    log.info("Action capture: fetching transcripts since %s (known_ids=%d)",
             since_dt.isoformat(), len(processed_ids))

    # Fetch transcripts since watermark
    from_date = since_dt.isoformat()
    to_date = datetime.now(timezone.utc).isoformat()

    skip = 0
    batch_size = 25
    latest_ts = since_ts
    all_transcripts: list[dict] = []

    while True:
        variables = {
            "limit": batch_size,
            "skip": skip,
            "fromDate": from_date,
            "toDate": to_date,
        }
        try:
            data = _graphql_query(_TRANSCRIPTS_QUERY, variables)
        except FirefliesConnectorError as exc:
            msg = f"Fireflies query failed at skip={skip}: {exc}"
            log.error(msg)
            result["errors"].append(msg)
            break

        transcripts = data.get("transcripts") or []
        if not transcripts:
            break
        all_transcripts.extend(transcripts)
        if len(transcripts) < batch_size:
            break
        skip += batch_size
        time.sleep(0.5)

    log.info("Action capture: %d transcripts fetched", len(all_transcripts))

    # Process each transcript
    # Group by entity for batched Slack posting
    entity_results: dict[str, list[dict[str, Any]]] = {}

    for transcript in all_transcripts:
        title = (transcript.get("title") or "").strip()
        if not title:
            continue

        # ID-based dedup: skip any transcript already processed in a prior run.
        # This is the primary guard against the re-processing bug where
        # meeting_ts == since_ts so the watermark never advances.
        transcript_id = transcript.get("id") or ""
        if transcript_id and transcript_id in processed_ids:
            log.info("Skipping already-processed transcript id=%s title=%r", transcript_id, title)
            continue

        entity = _classify_entity(title)

        # PHI guardrail: skip ALL LEX meetings (not just clinical ones)
        if entity == "LEX":
            log.info("PHI guardrail: skipping LEX meeting %r", title)
            continue

        # Also apply clinical PHI check for any meeting
        if _is_phi_meeting(title, entity):
            log.info("PHI guardrail (clinical): skipping %r", title)
            continue

        summary = transcript.get("summary") or {}
        action_items_text = (summary.get("action_items") or "").strip()
        if not action_items_text:
            continue  # No action items -- nothing to do

        meeting_ts = _parse_date(transcript.get("date"))
        if meeting_ts and meeting_ts > latest_ts:
            latest_ts = meeting_ts

        # Mark this transcript as processed so it won't be reprocessed next hour
        if transcript_id:
            processed_ids.add(transcript_id)

        meeting_date_str = (
            datetime.fromtimestamp(meeting_ts, tz=timezone.utc).strftime("%Y-%m-%d")
            if meeting_ts else "unknown date"
        )

        attendees = transcript.get("meeting_attendees") or []

        # Parse action items with Haiku
        parsed_tasks = _parse_action_items_with_haiku(action_items_text)
        if not parsed_tasks:
            log.info("No parseable action items in %r", title)
            continue

        log.info("Processing %d action items from %r", len(parsed_tasks), title)
        result["meetings_processed"] += 1

        created_tasks: list[dict[str, Any]] = []

        for item in parsed_tasks:
            task_name = item["task"]
            assignee_name = item.get("assignee_name")
            due_mention = item.get("due_mention")

            # Resolve assignee GID
            assignee_gid = _resolve_assignee_gid(assignee_name, attendees)

            # Build task notes
            notes = _TASK_NOTES_TEMPLATE.format(
                meeting_title=title,
                meeting_date=meeting_date_str,
                raw_text=action_items_text[:500],
            )
            if due_mention:
                notes += f"\nDue mention: {due_mention}"

            # Create Asana task
            if dry_run:
                log.info(
                    "[DRY RUN] Would create task: %r  assignee_gid=%s",
                    task_name, assignee_gid,
                )
                created_tasks.append({
                    "task_name": task_name,
                    "assignee_name": assignee_name,
                    "permalink_url": "",
                    "asana_gid": "dry-run",
                })
                result["tasks_created"] += 1
            else:
                try:
                    created = create_task(
                        name=task_name,
                        assignee_gid=assignee_gid,
                        notes=notes,
                    )
                    permalink = created.get("permalink_url", "")
                    log.info(
                        "Created Asana task: gid=%s  name=%r  assignee=%s",
                        created.get("gid"), task_name, assignee_gid,
                    )
                    created_tasks.append({
                        "task_name": task_name,
                        "assignee_name": assignee_name,
                        "permalink_url": permalink,
                        "asana_gid": created.get("gid", ""),
                    })
                    result["tasks_created"] += 1
                except AsanaClientError as exc:
                    msg = f"Asana create_task failed for {task_name!r}: {exc}"
                    log.error(msg)
                    result["errors"].append(msg)

        # Store results keyed by (entity, meeting_title) for Slack posting
        key = f"{entity}||{title}"
        entity_results.setdefault(key, []).extend(created_tasks)

    # Post Slack digests per meeting
    for key, tasks in entity_results.items():
        entity_code, meeting_title = key.split("||", 1)
        channel = _ENTITY_CHANNEL.get(entity_code)
        if not channel:
            log.info("No channel mapped for entity %s -- skipping Slack post", entity_code)
            continue
        _post_slack_summary(channel, meeting_title, tasks, dry_run=dry_run)

    # Always write watermark: update timestamp if advanced, always persist processed IDs.
    # Writing processed_ids even when timestamp didn't advance is critical -- it ensures
    # that transcripts with meeting_ts == since_ts are never reprocessed.
    new_ts = max(latest_ts, since_ts)
    _write_watermark(new_ts, processed_ids)
    if new_ts > since_ts:
        log.info("Watermark advanced to %d (%s)", new_ts,
                 datetime.fromtimestamp(new_ts, tz=timezone.utc).isoformat())
    else:
        log.info("Watermark unchanged at %d; %d transcript IDs now tracked", new_ts, len(processed_ids))

    log.info(
        "Action capture complete: meetings=%d tasks=%d errors=%d",
        result["meetings_processed"],
        result["tasks_created"],
        len(result["errors"]),
    )
    return result