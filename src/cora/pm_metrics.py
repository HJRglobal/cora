"""PM-hub Phase 1 adoption + hygiene instrumentation (2026-07-15).

Two responsibilities:

  1. log_pm_action() -- append ONE line to logs/pm-actions.jsonl for every task action
     Cora performs via her tools (create / complete / delete / update / comment /
     subtask / meeting-capture). This is the authoritative, per-person Cora-attributed
     record: the single-PAT Asana model attributes every Cora write to Harrison, so
     `created_by` can't distinguish Cora-vs-UI -- the log is the ground truth for the
     Cora side. NO task TITLE is ever persisted (gid + entity + action only): these
     edit tools act CROSS-ENTITY (a founder / FNDR / HJRG asker can act on their own
     LEX task, so the channel entity can't reliably gate a LEX title), so titles are
     omitted UNCONDITIONALLY -- that closes invariant #2 (LEX aggregate-only) with no
     dependence on the recorded entity, and the digest never needs a title anyway.

  2. run() / format_digest() -- the weekly PM-adoption digest (scheduled, script-side):
     Cora-vs-UI created/completed, overdue WoW trend, staleness, per-person engagement.
     This IS the Phase-2 go/no-go instrument.

Standalone: imports only stdlib + yaml at module load (asana_client is imported lazily
inside the digest gather) -- never the bot process, so the scheduled digest script does
not drag app.py in (D-047).
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ACTION_LOG = _REPO_ROOT / "logs" / "pm-actions.jsonl"
_SNAPSHOT_DIR = _REPO_ROOT / "data" / "state" / "pm-adoption-snapshots"
_ROSTER = _REPO_ROOT / "data" / "maps" / "slack-to-asana.yaml"

_STALE_DAYS = 14
_SNAPSHOTS_KEPT = 26
HARRISON_DM = "U0B2RM2JYJ1"
FOUNDER_OPS_CHANNEL = "C0BCUBUDHAR"  # #founder-operations

# Which logged actions count toward the Cora-vs-UI created / completed split.
_CREATE_ACTIONS = frozenset({"create", "subtask"})
_COMPLETE_ACTIONS = frozenset({"complete"})


def log_pm_action(action: str, actor: str, entity: str, gid: str,
                  *, title: str | None = None, extra: dict | None = None) -> None:
    """Append one PM-action record. NEVER raises (a logging failure must not break a task
    write).

    The `title` argument is accepted (call sites pass it) but is DELIBERATELY NOT
    persisted: these edit tools act cross-entity, so the channel `entity` cannot reliably
    tell whether the resolved task is LEX -- persisting a title would leak a LEX client
    name into this at-rest sink whenever a LEX task is acted on from a FNDR/HJRG/founder
    context (D-051 2026-07-15). Omitting titles unconditionally closes invariant #2; the
    adoption metric + digest only ever need gid + entity + action. `extra` must carry NO
    task content -- only structural metadata (field names, parent gid, via-source).
    """
    try:
        _ACTION_LOG.parent.mkdir(parents=True, exist_ok=True)
        entry: dict[str, Any] = {
            "ts": int(time.time()),
            "action": str(action),
            "actor": str(actor or ""),
            "entity": (entity or "").upper(),
            "gid": str(gid or ""),
        }
        if extra:
            entry["extra"] = extra
        with _ACTION_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception as exc:  # noqa: BLE001 -- never break the write path
        log.error("pm_metrics: action-log write failed: %s", exc)


# ── digest ──────────────────────────────────────────────────────────────────

def _load_roster() -> list[dict]:
    try:
        with _ROSTER.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return data.get("users", []) or []
    except Exception as exc:  # noqa: BLE001
        log.error("pm_metrics: roster load failed: %s", exc)
        return []


def read_actions(since_ts: int) -> list[dict]:
    """Parse pm-actions.jsonl, returning entries with ts >= since_ts. Skips bad lines."""
    out: list[dict] = []
    if not _ACTION_LOG.exists():
        return out
    try:
        with _ACTION_LOG.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if int(e.get("ts", 0)) >= since_ts:
                    out.append(e)
    except Exception as exc:  # noqa: BLE001
        log.error("pm_metrics: action-log read failed: %s", exc)
    return out


def _parse_iso(v) -> datetime | None:
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _parse_date(v) -> date | None:
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except Exception:
        return None


def run(lookback_days: int = 7, stale_days: int = _STALE_DAYS,
        now: datetime | None = None, write_state: bool = True) -> dict:
    """Gather the weekly PM-adoption metrics. Fail-soft: the Asana-state gather is wrapped
    so a connector failure still yields the (authoritative) Cora-log metrics."""
    now = now or datetime.now(timezone.utc)
    window_start = now - timedelta(days=lookback_days)
    prev_start = window_start - timedelta(days=lookback_days)
    ws_ts = int(window_start.timestamp())
    ps_ts = int(prev_start.timestamp())

    this_week = read_actions(ws_ts)
    prev_week = [e for e in read_actions(ps_ts) if int(e.get("ts", 0)) < ws_ts]

    def _bucket(entries):
        by_action, by_actor, by_entity = {}, {}, {}
        for e in entries:
            by_action[e.get("action", "?")] = by_action.get(e.get("action", "?"), 0) + 1
            by_actor[e.get("actor", "?")] = by_actor.get(e.get("actor", "?"), 0) + 1
            by_entity[e.get("entity", "?")] = by_entity.get(e.get("entity", "?"), 0) + 1
        return by_action, by_actor, by_entity

    tw_action, tw_actor, tw_entity = _bucket(this_week)
    pw_action, _, _ = _bucket(prev_week)
    cora_created = sum(tw_action.get(a, 0) for a in _CREATE_ACTIONS)
    cora_completed = sum(tw_action.get(a, 0) for a in _COMPLETE_ACTIONS)

    result: dict[str, Any] = {
        "generated_at": now.isoformat(),
        "lookback_days": lookback_days,
        "cora": {
            "total_this_week": len(this_week),
            "total_prev_week": len(prev_week),
            "by_action": tw_action,
            "by_action_prev": pw_action,
            "by_entity": tw_entity,
            "by_actor": tw_actor,
            "created": cora_created,
            "completed": cora_completed,
        },
        "asana": None,
        "asana_error": None,
    }

    try:
        result["asana"] = _gather_asana_state(window_start, now, stale_days, tw_actor)
    except Exception as exc:  # noqa: BLE001 -- Cora-log metrics still deliver
        log.exception("pm_metrics: asana state gather failed")
        result["asana_error"] = str(exc)

    result["overdue_wow"] = _snapshot_and_wow(result, now, write_state)
    return result


def _gather_asana_state(window_start: datetime, now: datetime, stale_days: int,
                        cora_actor_counts: dict) -> dict:
    """Enumerate open + completed-this-week tasks across the roster (one call per mapped
    user: completed_since=window_start returns incomplete + completed-since in one shot)."""
    from .tools import asana_client  # lazy -- keep it out of log_pm_action's import path

    roster = _load_roster()
    since_iso = window_start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    stale_cut = now - timedelta(days=stale_days)
    today = now.date()
    fields = ["name", "completed", "completed_at", "created_at", "modified_at", "due_on"]

    seen: dict[str, dict] = {}
    per_person: list[dict] = []
    errors = 0
    covered = 0
    for u in roster:
        agid = str(u.get("asana_user_gid", "") or "")
        if not agid or "REPLACE" in agid:
            continue
        covered += 1
        try:
            tasks = asana_client.get_user_tasks(
                agid, max_tasks=200, opt_fields=fields, completed_since=since_iso,
            )
        except Exception:  # noqa: BLE001 -- one bad mailbox never sinks the digest
            errors += 1
            continue
        p_open = p_overdue = 0
        for t in tasks:
            gid = str(t.get("gid") or "")
            if gid and gid not in seen:
                seen[gid] = t
            if not t.get("completed"):
                p_open += 1
                d = _parse_date(t.get("due_on"))
                if d and d < today:
                    p_overdue += 1
        per_person.append({
            "name": u.get("display_name", agid),
            "slack": u.get("slack_user_id", ""),
            "open": p_open,
            "overdue": p_overdue,
            "cora_actions": cora_actor_counts.get(u.get("slack_user_id", ""), 0),
        })

    open_total = overdue_total = stale_total = created_week = completed_week = 0
    for t in seen.values():
        created = _parse_iso(t.get("created_at"))
        if created and created >= window_start:
            created_week += 1
        if t.get("completed"):
            comp = _parse_iso(t.get("completed_at"))
            if comp and comp >= window_start:
                completed_week += 1
            continue
        open_total += 1
        d = _parse_date(t.get("due_on"))
        if d and d < today:
            overdue_total += 1
        modified = _parse_iso(t.get("modified_at"))
        if modified and modified < stale_cut:
            stale_total += 1

    return {
        "open_total": open_total,
        "overdue_total": overdue_total,
        "stale_total": stale_total,
        "stale_days": stale_days,
        "created_this_week": created_week,
        "completed_this_week": completed_week,
        "roster_covered": covered,
        "fetch_errors": errors,
        "per_person": sorted(per_person, key=lambda p: p["open"], reverse=True),
    }


def _snapshot_and_wow(result: dict, now: datetime, write_state: bool) -> dict:
    """Write today's snapshot (unless write_state=False) and return the WoW overdue delta
    vs the most-recent PRIOR snapshot. delta=None when there is no prior or Asana failed."""
    asana = result.get("asana") or {}
    snap = {
        "date": now.date().isoformat(),
        "ts": int(now.timestamp()),
        "overdue_total": asana.get("overdue_total"),
        "open_total": asana.get("open_total"),
        "stale_total": asana.get("stale_total"),
        "cora_total": result["cora"]["total_this_week"],
    }
    prior_overdue = None
    try:
        _SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        for p in reversed(sorted(_SNAPSHOT_DIR.glob("*.json"))):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if d.get("date") == snap["date"]:
                continue
            if d.get("overdue_total") is not None:
                prior_overdue = d["overdue_total"]
                break
        if write_state:
            (_SNAPSHOT_DIR / f"{snap['date']}.json").write_text(
                json.dumps(snap), encoding="utf-8")
            for old in sorted(_SNAPSHOT_DIR.glob("*.json"))[:-_SNAPSHOTS_KEPT]:
                try:
                    old.unlink()
                except Exception:
                    pass
    except Exception as exc:  # noqa: BLE001
        log.error("pm_metrics: snapshot failed: %s", exc)

    cur = asana.get("overdue_total")
    if cur is not None and prior_overdue is not None:
        return {"current": cur, "prior": prior_overdue, "delta": cur - prior_overdue}
    return {"current": cur, "prior": prior_overdue, "delta": None}


def format_digest(result: dict) -> str:
    """Render the digest as Slack mrkdwn. COUNTS ONLY -- never a task title (PHI-safe by
    construction; per-person lines carry display names, not task content)."""
    cora = result["cora"]
    asana = result.get("asana")
    days = result["lookback_days"]
    lines: list[str] = []
    lines.append(f"*PM adoption -- past {days} days* (Cora as the primary task interface)")
    lines.append("")
    lines.append(
        f"*Managed through Cora this week:* {cora['total_this_week']} actions "
        f"(prev week: {cora['total_prev_week']})"
    )
    if cora["by_action"]:
        parts = ", ".join(
            f"{k} {v}" for k, v in sorted(cora["by_action"].items(), key=lambda x: -x[1])
        )
        lines.append(f"   {parts}")
    if cora["by_entity"]:
        ent = ", ".join(
            f"{k} {v}" for k, v in sorted(cora["by_entity"].items(), key=lambda x: -x[1])
        )
        lines.append(f"   by entity: {ent}")

    if asana:
        created_via_cora = min(cora["created"], asana["created_this_week"])
        completed_via_cora = min(cora["completed"], asana["completed_this_week"])
        created_ui = max(0, asana["created_this_week"] - cora["created"])
        completed_ui = max(0, asana["completed_this_week"] - cora["completed"])
        lines.append("")
        lines.append(
            f"*Created this week:* {asana['created_this_week']} in Asana -- "
            f"~{created_via_cora} via Cora, ~{created_ui} directly in Asana"
        )
        lines.append(
            f"*Completed this week:* {asana['completed_this_week']} in Asana -- "
            f"~{completed_via_cora} via Cora, ~{completed_ui} directly in Asana"
        )
        lines.append(
            "   _single-PAT model: Cora counts are exact (her action log); 'directly' "
            "is an estimate = Asana total minus Cora-attributed._"
        )
        wow = result.get("overdue_wow") or {}
        delta = wow.get("delta")
        trend = f" ({delta:+d} WoW)" if delta is not None else ""
        lines.append("")
        lines.append(
            f"*Open:* {asana['open_total']} | *Overdue:* {asana['overdue_total']}{trend} | "
            f"*Stale (no update {asana['stale_days']}d+):* {asana['stale_total']}"
        )
        if asana.get("fetch_errors"):
            lines.append(f"   _{asana['fetch_errors']} roster mailbox fetch(es) failed this run._")

        pp = asana.get("per_person") or []
        engaged = sorted([p for p in pp if p["cora_actions"] > 0],
                         key=lambda x: -x["cora_actions"])[:10]
        idle = sorted([p for p in pp if p["cora_actions"] == 0 and p["open"] > 0],
                      key=lambda x: -x["open"])[:8]
        lines.append("")
        lines.append("*Per-person engagement:*")
        if engaged:
            for p in engaged:
                lines.append(
                    f"   - {p['name']}: {p['cora_actions']} via Cora | "
                    f"{p['open']} open ({p['overdue']} overdue)"
                )
        else:
            lines.append("   - No one managed tasks through Cora this week.")
        if idle:
            lines.append(
                "   - Not using Cora yet (have open tasks): "
                + ", ".join(p["name"] for p in idle)
            )
    else:
        err = f": {result['asana_error']}" if result.get("asana_error") else ""
        lines.append("")
        lines.append(f"_Asana state unavailable this run{err} -- showing Cora-log metrics only._")

    lines.append("")
    lines.append(
        "_Phase-1 adoption instrument -- review the trend over ~4-6 weeks for the "
        "Phase-2 go/no-go (did conversational-first lift completion + cut staleness?)._"
    )
    return "\n".join(lines)


def post_digest(text: str, also_channel: bool = False) -> bool:
    """DM the digest to Harrison; optionally also post to #founder-operations. Returns
    True if every intended post succeeded."""
    import os
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        log.error("pm_metrics: SLACK_BOT_TOKEN missing -- digest not posted")
        return False
    from slack_sdk import WebClient  # lazy
    wc = WebClient(token=token)
    ok = True
    try:
        dm = wc.conversations_open(users=[HARRISON_DM])["channel"]["id"]
        wc.chat_postMessage(channel=dm, text=text, unfurl_links=False, unfurl_media=False)
    except Exception as exc:  # noqa: BLE001
        log.error("pm_metrics: DM post failed: %s", exc)
        ok = False
    if also_channel:
        try:
            wc.chat_postMessage(channel=FOUNDER_OPS_CHANNEL, text=text,
                                unfurl_links=False, unfurl_media=False)
        except Exception as exc:  # noqa: BLE001
            log.error("pm_metrics: channel post failed: %s", exc)
            ok = False
    return ok
