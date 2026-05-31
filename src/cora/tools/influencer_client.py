"""Influencer deliverable tracker — SQLite-backed compliance store for sponsored athletes.

Tracks promised social-media deliverables (posts, stories, reels, videos, etc.) for
sponsored athletes and influencers across F3 Energy and other HJR entities. Built for
Alex Cordova (F3E Post-Sale Logistics + Account Manager) as the primary operator.

Architecture:
- Local SQLite at data/influencer_tracker.db (alongside cora_kb.db).
- Status is computed on read: any 'pending' row with due_date < today surfaces as
  'overdue'. The stored status stays 'pending' until Alex explicitly marks it complete
  or waived — no cron job needed.
- HubSpot deal ID stored per deliverable for cross-referencing the source sponsorship
  deal (HubSpot deal links generated as clickable Slack mrkdwn).
- Entity-aware: deliverables tagged with entity code so the tool can filter by channel
  scope the same way all other Cora tools do.

Write doctrine (same as other Cora write tools):
- Cora shows a preview block first; confirmed=True required to commit.
- Audit log: actor / athlete / deliverable_id / action / status.
  Post content / notes NOT logged.

Automated monitoring:
- instagram_monitor.py (src/cora/connectors/) polls the F3 brand accounts' tagged
  media and brand hashtags every 2 hours via a Windows scheduled task.
- Detections are deduped via the detection_log table and posted to Slack for Alex to confirm.
- Athlete social handles are registered via the influencer_add_handle Cora tool.

Deferred for follow-up:
- Recurring deliverable templates (e.g. "2 posts/month" auto-generates rows each cycle).
- TikTok monitoring (pending TikTok Research API approval — scaffold in tiktok_monitor.py).
- Bulk import from HubSpot (sync all deals tagged as influencer deals → seed deliverables).
"""

import logging
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DB_PATH = _REPO_ROOT / "data" / "influencer_tracker.db"

# Platforms we recognise — kept loose so Alex can add any value; list used only for
# display hints in error messages.
_KNOWN_PLATFORMS = ("instagram", "tiktok", "youtube", "twitter", "x", "podcast", "facebook", "linkedin", "other")
_KNOWN_TYPES = ("post", "story", "reel", "video", "tweet", "shoutout", "podcast_mention", "review", "other")

HUBSPOT_DEAL_BASE_URL = "https://app.hubspot.com/contacts/246351746/deal/"


class InfluencerClientError(Exception):
    """Raised on invalid input or DB errors in this module."""


# ---------------------------------------------------------------------------
# DB init
# ---------------------------------------------------------------------------

def _get_conn() -> sqlite3.Connection:
    """Return a connection to the influencer tracker DB, creating the schema if needed."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create tables if they don't exist. Safe to run on every connection open."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS influencer_handles (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            athlete_name    TEXT    NOT NULL,
            platform        TEXT    NOT NULL,
            handle          TEXT    NOT NULL,
            entity          TEXT    NOT NULL DEFAULT 'F3E',
            added_by        TEXT,
            added_at        TEXT    NOT NULL,
            UNIQUE(platform, handle)
        );
        CREATE INDEX IF NOT EXISTS idx_hdl_athlete  ON influencer_handles(athlete_name);
        CREATE INDEX IF NOT EXISTS idx_hdl_platform ON influencer_handles(platform, handle);

        CREATE TABLE IF NOT EXISTS detection_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            platform        TEXT    NOT NULL,
            post_id         TEXT    NOT NULL,
            brand_handle    TEXT    NOT NULL,
            athlete_name    TEXT,
            athlete_handle  TEXT,
            media_type      TEXT,
            post_url        TEXT,
            caption_snippet TEXT,
            detected_at     TEXT    NOT NULL,
            slack_notified  INTEGER NOT NULL DEFAULT 0,
            deliverable_id  INTEGER,
            UNIQUE(platform, post_id)
        );
        CREATE INDEX IF NOT EXISTS idx_det_platform    ON detection_log(platform, post_id);
        CREATE INDEX IF NOT EXISTS idx_det_detected_at ON detection_log(detected_at);

        CREATE TABLE IF NOT EXISTS influencer_deliverables (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            athlete_name    TEXT    NOT NULL,
            platform        TEXT    NOT NULL,
            deliverable_type TEXT   NOT NULL,
            due_date        TEXT,
            status          TEXT    NOT NULL DEFAULT 'pending',
            completion_link TEXT,
            notes           TEXT,
            hubspot_deal_id TEXT,
            entity          TEXT    NOT NULL DEFAULT 'F3E',
            created_by      TEXT,
            created_at      TEXT    NOT NULL,
            updated_at      TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_inf_athlete   ON influencer_deliverables(athlete_name);
        CREATE INDEX IF NOT EXISTS idx_inf_status    ON influencer_deliverables(status);
        CREATE INDEX IF NOT EXISTS idx_inf_due_date  ON influencer_deliverables(due_date);
        CREATE INDEX IF NOT EXISTS idx_inf_entity    ON influencer_deliverables(entity);
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Handle registry
# ---------------------------------------------------------------------------

def register_handle(
    *,
    athlete_name: str,
    platform: str,
    handle: str,
    entity: str = "F3E",
    added_by: str | None = None,
) -> dict[str, Any]:
    """Register a social media handle for an athlete.

    Handles are stored without the leading '@'. If the (platform, handle) pair
    already exists the row is updated with the new athlete_name / entity mapping
    (upsert semantics — one handle can only belong to one athlete per platform).

    Raises InfluencerClientError on blank inputs.
    """
    if not athlete_name or not athlete_name.strip():
        raise InfluencerClientError("athlete_name is required.")
    if not platform or not platform.strip():
        raise InfluencerClientError("platform is required (e.g. instagram, tiktok).")
    if not handle or not handle.strip():
        raise InfluencerClientError("handle is required.")

    clean_handle = handle.strip().lstrip("@").lower()
    clean_platform = platform.strip().lower()
    now_str = date.today().isoformat()

    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO influencer_handles
                (athlete_name, platform, handle, entity, added_by, added_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(platform, handle) DO UPDATE SET
                athlete_name = excluded.athlete_name,
                entity       = excluded.entity,
                added_by     = excluded.added_by,
                added_at     = excluded.added_at
            """,
            (
                athlete_name.strip(),
                clean_platform,
                clean_handle,
                entity.strip().upper(),
                added_by,
                now_str,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM influencer_handles WHERE platform = ? AND handle = ?",
            (clean_platform, clean_handle),
        ).fetchone()

    log.info(
        "influencer register_handle athlete=%r platform=%s handle=%s entity=%s added_by=%s",
        row["athlete_name"], row["platform"], row["handle"], row["entity"],
        added_by or "(unknown)",
    )
    return dict(row)


def get_athlete_by_handle(platform: str, handle: str) -> dict[str, Any] | None:
    """Look up an athlete record by their platform handle. Returns None if not registered.

    Accepts handles with or without the leading '@'.
    """
    clean_handle = handle.strip().lstrip("@").lower()
    clean_platform = platform.strip().lower()
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM influencer_handles WHERE platform = ? AND handle = ?",
            (clean_platform, clean_handle),
        ).fetchone()
    return dict(row) if row else None


def list_handles(
    *,
    entity: str | None = None,
    platform: str | None = None,
) -> list[dict[str, Any]]:
    """Return all registered handles, optionally filtered by entity or platform."""
    clauses, params = [], []
    if entity and entity.upper() != "FNDR":
        clauses.append("entity = ?")
        params.append(entity.upper())
    if platform:
        clauses.append("platform = ?")
        params.append(platform.strip().lower())
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with _get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM influencer_handles {where} ORDER BY athlete_name, platform",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Detection log (dedup for the automated scanner)
# ---------------------------------------------------------------------------

def is_already_detected(platform: str, post_id: str) -> bool:
    """Return True if this platform post_id has already been logged."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM detection_log WHERE platform = ? AND post_id = ?",
            (platform.lower(), str(post_id)),
        ).fetchone()
    return row is not None


def log_detection(
    *,
    platform: str,
    post_id: str,
    brand_handle: str,
    athlete_name: str | None = None,
    athlete_handle: str | None = None,
    media_type: str | None = None,
    post_url: str | None = None,
    caption_snippet: str | None = None,
    slack_notified: bool = False,
    deliverable_id: int | None = None,
) -> dict[str, Any]:
    """Insert a detection event. Silently ignores duplicate (platform, post_id) pairs.

    Returns the inserted or existing row.
    """
    now_str = date.today().isoformat()
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO detection_log
                (platform, post_id, brand_handle, athlete_name, athlete_handle,
                 media_type, post_url, caption_snippet, detected_at,
                 slack_notified, deliverable_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                platform.lower(),
                str(post_id),
                brand_handle,
                athlete_name,
                athlete_handle,
                media_type,
                post_url,
                caption_snippet,
                now_str,
                1 if slack_notified else 0,
                deliverable_id,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM detection_log WHERE platform = ? AND post_id = ?",
            (platform.lower(), str(post_id)),
        ).fetchone()
    return dict(row)


def mark_detection_notified(platform: str, post_id: str) -> None:
    """Mark a detection row as Slack-notified after the message is posted."""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE detection_log SET slack_notified = 1 WHERE platform = ? AND post_id = ?",
            (platform.lower(), str(post_id)),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def add_deliverable(
    *,
    athlete_name: str,
    platform: str,
    deliverable_type: str,
    due_date: str | None = None,
    notes: str | None = None,
    hubspot_deal_id: str | None = None,
    entity: str = "F3E",
    created_by: str | None = None,
) -> dict[str, Any]:
    """Insert a new promised deliverable and return the created row as a dict.

    Raises InfluencerClientError on validation failures.
    """
    if not athlete_name or not athlete_name.strip():
        raise InfluencerClientError("athlete_name is required and cannot be blank.")
    if not platform or not platform.strip():
        raise InfluencerClientError("platform is required (e.g. instagram, tiktok, youtube).")
    if not deliverable_type or not deliverable_type.strip():
        raise InfluencerClientError("deliverable_type is required (e.g. post, story, reel).")

    # Validate optional due_date format
    if due_date:
        due_date = due_date.strip()
        try:
            date.fromisoformat(due_date)
        except ValueError:
            raise InfluencerClientError(
                f"due_date {due_date!r} is not a valid ISO date (YYYY-MM-DD)."
            )

    now_str = date.today().isoformat()
    with _get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO influencer_deliverables
                (athlete_name, platform, deliverable_type, due_date, status,
                 hubspot_deal_id, notes, entity, created_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)
            """,
            (
                athlete_name.strip(),
                platform.strip().lower(),
                deliverable_type.strip().lower(),
                due_date,
                hubspot_deal_id.strip() if hubspot_deal_id else None,
                notes,
                entity.strip().upper(),
                created_by,
                now_str,
                now_str,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM influencer_deliverables WHERE id = ?", (cur.lastrowid,)
        ).fetchone()

    log.info(
        "influencer add_deliverable id=%d athlete=%r platform=%r type=%r due=%s entity=%s created_by=%s",
        row["id"], row["athlete_name"], row["platform"],
        row["deliverable_type"], row["due_date"] or "none", row["entity"],
        created_by or "(unknown)",
    )
    return dict(row)


def mark_complete(
    *,
    deliverable_id: int,
    completion_link: str | None = None,
    notes: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Mark a deliverable as complete. Returns the updated row.

    Raises InfluencerClientError if the ID doesn't exist or is already complete/waived.
    """
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM influencer_deliverables WHERE id = ?", (deliverable_id,)
        ).fetchone()
        if not row:
            raise InfluencerClientError(
                f"Deliverable ID {deliverable_id} not found. "
                f"Use influencer_get_status to see current IDs."
            )
        if row["status"] in ("complete", "waived"):
            raise InfluencerClientError(
                f"Deliverable #{deliverable_id} ({row['athlete_name']} — {row['deliverable_type']}) "
                f"is already {row['status']}. Nothing to update."
            )
        now_str = date.today().isoformat()
        conn.execute(
            """
            UPDATE influencer_deliverables
            SET status = 'complete',
                completion_link = COALESCE(?, completion_link),
                notes = CASE WHEN ? IS NOT NULL THEN ? ELSE notes END,
                updated_at = ?
            WHERE id = ?
            """,
            (completion_link, notes, notes, now_str, deliverable_id),
        )
        conn.commit()
        updated = conn.execute(
            "SELECT * FROM influencer_deliverables WHERE id = ?", (deliverable_id,)
        ).fetchone()

    log.info(
        "influencer mark_complete id=%d athlete=%r link=%s actor=%s",
        deliverable_id, updated["athlete_name"],
        completion_link or "(none)", actor or "(unknown)",
    )
    return dict(updated)


def mark_waived(
    *,
    deliverable_id: int,
    notes: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Mark a deliverable as waived (excused / cancelled by agreement). Returns updated row."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM influencer_deliverables WHERE id = ?", (deliverable_id,)
        ).fetchone()
        if not row:
            raise InfluencerClientError(
                f"Deliverable ID {deliverable_id} not found. "
                f"Use influencer_get_status to see current IDs."
            )
        if row["status"] in ("complete", "waived"):
            raise InfluencerClientError(
                f"Deliverable #{deliverable_id} is already {row['status']}. Nothing to update."
            )
        now_str = date.today().isoformat()
        conn.execute(
            """
            UPDATE influencer_deliverables
            SET status = 'waived',
                notes = CASE WHEN ? IS NOT NULL THEN ? ELSE notes END,
                updated_at = ?
            WHERE id = ?
            """,
            (notes, notes, now_str, deliverable_id),
        )
        conn.commit()
        updated = conn.execute(
            "SELECT * FROM influencer_deliverables WHERE id = ?", (deliverable_id,)
        ).fetchone()

    log.info(
        "influencer mark_waived id=%d athlete=%r actor=%s",
        deliverable_id, updated["athlete_name"], actor or "(unknown)",
    )
    return dict(updated)


def get_deliverables(
    *,
    entity: str | None = None,
    athlete: str | None = None,
    include_complete: bool = False,
    include_waived: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Fetch deliverables with optional filters.

    Returns rows as dicts. 'status' field is adjusted for display:
    pending rows with due_date < today are labelled 'overdue' in the returned dict
    (the stored status column remains 'pending').
    """
    clauses = []
    params: list[Any] = []

    if not include_complete:
        clauses.append("status != 'complete'")
    if not include_waived:
        clauses.append("status != 'waived'")
    if entity and entity.upper() != "FNDR":
        clauses.append("entity = ?")
        params.append(entity.upper())
    if athlete:
        clauses.append("LOWER(athlete_name) LIKE ?")
        params.append(f"%{athlete.lower()}%")

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)

    with _get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM influencer_deliverables
            {where}
            ORDER BY
                CASE WHEN due_date IS NULL THEN 1 ELSE 0 END,
                due_date ASC,
                athlete_name ASC
            LIMIT ?
            """,
            params,
        ).fetchall()

    today = date.today().isoformat()
    result = []
    for row in rows:
        d = dict(row)
        # Compute display status
        if d["status"] == "pending" and d["due_date"] and d["due_date"] < today:
            d["display_status"] = "overdue"
        else:
            d["display_status"] = d["status"]
        result.append(d)
    return result


def get_compliance_report(
    *,
    entity: str | None = None,
    athlete: str | None = None,
) -> list[dict[str, Any]]:
    """Return a per-athlete compliance summary.

    Each entry: {athlete_name, total, complete, pending, overdue, waived, compliance_pct}.
    Sorted by compliance_pct ascending (worst first) so Alex sees who needs follow-up first.
    """
    where_parts = []
    params: list[Any] = []
    if entity and entity.upper() != "FNDR":
        where_parts.append("entity = ?")
        params.append(entity.upper())
    if athlete:
        where_parts.append("LOWER(athlete_name) LIKE ?")
        params.append(f"%{athlete.lower()}%")

    where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    today = date.today().isoformat()

    with _get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT
                athlete_name,
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'complete' THEN 1 ELSE 0 END) AS complete,
                SUM(CASE WHEN status = 'pending' AND (due_date IS NULL OR due_date >= ?) THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN status = 'pending' AND due_date < ? THEN 1 ELSE 0 END) AS overdue,
                SUM(CASE WHEN status = 'waived' THEN 1 ELSE 0 END) AS waived
            FROM influencer_deliverables
            {where}
            GROUP BY athlete_name
            ORDER BY athlete_name
            """,
            [today, today] + params,
        ).fetchall()

    result = []
    for row in rows:
        d = dict(row)
        # Compliance = complete / (total - waived), so waived don't penalise
        denominator = d["total"] - d["waived"]
        if denominator > 0:
            d["compliance_pct"] = round(100.0 * d["complete"] / denominator)
        else:
            d["compliance_pct"] = 100  # nothing owed after waivers
        result.append(d)

    # Sort worst compliance first
    result.sort(key=lambda x: x["compliance_pct"])
    return result


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

_STATUS_EMOJI = {
    "pending": "🟡",
    "overdue": "🔴",
    "complete": "✅",
    "waived": "⬜",
}


def _hubspot_link(deal_id: str | None) -> str:
    if not deal_id:
        return ""
    return f" | <{HUBSPOT_DEAL_BASE_URL}{deal_id}|HubSpot deal>"


def format_status_report_for_llm(
    rows: list[dict[str, Any]],
    *,
    entity_scope: str | None = None,
    report_label: str = "Influencer Deliverables",
) -> str:
    """Render the open deliverable list as Slack mrkdwn for the LLM to surface."""
    if not rows:
        scope_note = f" for *{entity_scope}*" if entity_scope else ""
        return (
            f"No open influencer deliverables found{scope_note}. "
            f"All caught up, or no deliverables have been logged yet."
        )

    overdue = [r for r in rows if r["display_status"] == "overdue"]
    pending = [r for r in rows if r["display_status"] == "pending"]

    lines = [f"*{report_label}* — {len(rows)} open item(s)"]
    if entity_scope:
        lines[0] += f" [{entity_scope}]"

    if overdue:
        lines.append(f"\n*🔴 Overdue ({len(overdue)})*")
        for r in overdue:
            lines.append(_format_deliverable_line(r))

    if pending:
        lines.append(f"\n*🟡 Pending ({len(pending)})*")
        for r in pending:
            lines.append(_format_deliverable_line(r))

    lines.append(
        "\n_Use `influencer_log_deliverable` action=complete to mark one done, "
        "or ask Cora for a compliance report._"
    )
    return "\n".join(lines)


def _format_deliverable_line(r: dict[str, Any]) -> str:
    emoji = _STATUS_EMOJI.get(r["display_status"], "❓")
    due = f" — due {r['due_date']}" if r["due_date"] else ""
    hs_link = _hubspot_link(r.get("hubspot_deal_id"))
    platform = r["platform"].capitalize()
    d_type = r["deliverable_type"].replace("_", " ")
    notes_snippet = f" _{r['notes'][:60]}…_" if r.get("notes") and len(r["notes"]) > 10 else ""
    return (
        f"  {emoji} *#{r['id']}* {r['athlete_name']} — {platform} {d_type}{due}"
        f"{hs_link}{notes_snippet}"
    )


def format_compliance_report_for_llm(
    rows: list[dict[str, Any]],
    *,
    entity_scope: str | None = None,
) -> str:
    """Render the per-athlete compliance table as Slack mrkdwn."""
    if not rows:
        scope_note = f" for *{entity_scope}*" if entity_scope else ""
        return f"No influencer deliverable data found{scope_note}. Nothing logged yet."

    lines = ["*Influencer Compliance Report*"]
    if entity_scope:
        lines[0] += f" [{entity_scope}]"
    lines.append("")

    for r in rows:
        pct = r["compliance_pct"]
        if pct >= 90:
            health = "🟢"
        elif pct >= 60:
            health = "🟡"
        else:
            health = "🔴"

        overdue_note = f" *({r['overdue']} overdue)*" if r["overdue"] else ""
        lines.append(
            f"{health} *{r['athlete_name']}* — {pct}% compliance "
            f"({r['complete']}/{r['total'] - r['waived']} complete{overdue_note})"
        )

    lines.append("")
    lines.append(
        "_Compliance = completed deliverables ÷ total owed (waivers excluded). "
        "Ask Cora for full status or specific athlete details._"
    )
    return "\n".join(lines)


def format_logged_deliverable_for_llm(
    row: dict[str, Any],
    *,
    action: str,
) -> str:
    """Render a just-created or just-updated deliverable as Slack mrkdwn confirmation."""
    emoji = _STATUS_EMOJI.get(row.get("display_status") or row["status"], "✅")
    hs_link = _hubspot_link(row.get("hubspot_deal_id"))
    due = f"\n- Due: {row['due_date']}" if row.get("due_date") else ""
    link_line = f"\n- Post: <{row['completion_link']}|View>" if row.get("completion_link") else ""

    if action == "add":
        verb = "LOGGED"
        detail = (
            f"New deliverable LOGGED. Surface this to the user:\n"
            f"- #{row['id']} — {row['athlete_name']}: "
            f"{row['platform'].capitalize()} {row['deliverable_type'].replace('_', ' ')}"
            f"{due}{hs_link}\n"
            f"Tell the user the deliverable is tracked and will appear in status reports."
        )
    elif action == "complete":
        verb = "COMPLETE"
        detail = (
            f"Deliverable #{row['id']} marked COMPLETE. Surface this to the user:\n"
            f"- {emoji} {row['athlete_name']} — {row['platform'].capitalize()} "
            f"{row['deliverable_type'].replace('_', ' ')}{link_line}"
        )
    else:  # waived
        verb = "WAIVED"
        detail = (
            f"Deliverable #{row['id']} marked WAIVED. Surface this to the user:\n"
            f"- {row['athlete_name']} — {row['platform'].capitalize()} "
            f"{row['deliverable_type'].replace('_', ' ')} (excused)"
        )

    _ = verb  # used only in logging; keep for future use
    return detail
