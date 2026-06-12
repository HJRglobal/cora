"""Founder strategy layer -- Org Synthesis Phase 4 (weekly portfolio memo).

Weekly standalone scheduled script (NOT bot-process code -- importing this
module must never pull in app.py / tool_dispatch / claude_client; the D-047
no-restart invariant). Gathers a deterministic cross-entity fact base, snapshots
it for week-over-week deltas, synthesizes a strategy memo with the FULL model
(Sonnet -- the one place quality beats cost), and delivers it to Harrison ONLY:
a Slack DM plus a markdown file under 00-Founder/_strategy-memos/ (the nightly
static_md sync ingests it, so Cora can answer "what did last week's memo say").

GATHER (deterministic, fail-soft per section -- a dead source degrades to a
stub line, never kills the memo):
  - per-entity cash position + week-over-week delta (gsheets Standing ACTUALS,
    same source as the daily Cash Flow Pulse)
  - pipeline posture (HubSpot F3E Retail + default pipelines: open totals,
    stage mix, movement vs last week, aging deals)
  - stalled P0/P1 decisions with ages (memory/decisions-pending.md)
  - portfolio deadline radar (Asana tasks due in the next 14d + overdue counts
    per owner, across the slack-to-asana roster)
  - the week's approved efficiency-backlog entries + still-pending friction
    findings (Org Synthesis Phase 3 output)
  - notable KB activity (last 7d swept-content counts per entity -- momentum)
  - uptime/health one-liner (heartbeat age)

SNAPSHOT each gather to data/state/strategy-memo-snapshots/YYYY-MM-DD.json so
next week's run computes real deltas ("OSN cash down 2 weeks straight",
"deal unmoved 3 memos running"). The first run says "first run -- no deltas
yet" honestly.

SYNTHESIZE with Sonnet, FAIL-CLOSED: any API error produces a short factual
rollup with a "synthesis unavailable" note -- never a hallucinated memo.

Hard rules (locked):
  - Harrison-only. The memo is NEVER posted to any channel or any other
    user's DM. Delivery is hard-coded to HARRISON_SLACK_ID.
  - Client-level LEX PHI never appears (aggregate posture only): LEX tasks
    are counted, never itemized; is_phi_risk() drops flagged content
    everywhere; the synthesis prompt forbids PHI.
  - Recommendations are ADVISORY -- nothing auto-executes, no Asana or
    decisions.md writes (D-011).
  - Visibility CPA content excluded from anything itemized.
  - org-roles / registry data is advisory context only (D-044).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .phi_guard import is_phi_risk, is_visibility_cpa_mention

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HARRISON_SLACK_ID = "U0B2RM2JYJ1"

SONNET_MODEL = "claude-sonnet-4-6"
_SYNTH_MAX_TOKENS = 2400

KB_ACTIVITY_DAYS = 7            # KB momentum window
DEADLINE_RADAR_DAYS = 14        # Asana due-date horizon
BACKLOG_RECENT_DAYS = 7         # "this week's" approved efficiency entries
PIPELINE_AGING_DAYS = 14        # deal untouched this long = aging
MAX_RADAR_ITEMS = 20            # itemized radar lines (soonest first)
SNAPSHOT_KEEP = 26              # ~6 months of weekly snapshots

# Same roster the Cash Flow Pulse reads (entity code -> display label).
CASH_ENTITIES: list[tuple[str, str]] = [
    ("FNDR",    "Portfolio"),
    ("F3E",     "F3 Energy"),
    ("OSN",     "One Stop Nutrition"),
    ("LEX",     "Lexington Services"),
    ("HJRG",    "HJR Global"),
    ("HJRP",    "HJR Properties"),
    ("BDM",     "Big D Media"),
    ("UFL",     "United Fight League"),
    ("HJRPROD", "HJR Productions"),
]

PIPELINES: list[tuple[str, str]] = [
    ("f3e_retail", "F3E Retail"),
    ("default",    "UFL/OSN/BDM (default)"),
]

_KB_SOURCES = ("slack", "gmail", "fireflies")


# ---------------------------------------------------------------------------
# Paths (env-overridable for tests)
# ---------------------------------------------------------------------------

def _snapshot_dir() -> Path:
    return Path(os.environ.get("STRATEGY_SNAPSHOT_DIR")
                or _REPO_ROOT / "data" / "state" / "strategy-memo-snapshots")


def _memo_root() -> Path:
    return Path(os.environ.get("STRATEGY_MEMO_DIR")
                or r"G:\My Drive\HJR-Founder-OS\00-Founder\_strategy-memos")


def _decisions_pending_path() -> Path:
    return Path(os.environ.get("STRATEGY_DECISIONS_PATH")
                or r"G:\My Drive\HJR-Founder-OS\memory\decisions-pending.md")


def _backlog_path() -> Path:
    return Path(os.environ.get("EFFICIENCY_BACKLOG_PATH")
                or _REPO_ROOT / "design" / "efficiency-backlog.md")


def _kb_db_path() -> Path:
    return Path(os.environ.get("STRATEGY_KB_DB_PATH")
                or _REPO_ROOT / "data" / "cora_kb.db")


def _asana_map_path() -> Path:
    return Path(os.environ.get("STRATEGY_ASANA_MAP_PATH")
                or _REPO_ROOT / "data" / "maps" / "slack-to-asana.yaml")


def _heartbeat_path() -> Path:
    return Path(os.environ.get("STRATEGY_HEARTBEAT_PATH")
                or _REPO_ROOT / "data" / "health" / "heartbeat.txt")


def _today() -> date:
    return datetime.now(timezone.utc).astimezone(
        timezone(timedelta(hours=-7))).date()   # America/Phoenix (no DST)


# ---------------------------------------------------------------------------
# Gather: per-entity cash (gsheets Standing ACTUALS -- Cash Flow Pulse source)
# ---------------------------------------------------------------------------

def gather_cash() -> dict[str, Any]:
    from .connectors.gsheets_financials import (
        GsheetsConnectorError, entity_to_tab, get_cashflow,
    )
    entities: dict[str, Any] = {}
    week_label = ""
    for code, label in CASH_ENTITIES:
        try:
            summary = get_cashflow(tab_name=entity_to_tab(code))
            entities[code] = {
                "label": label,
                "closing_balance": summary.closing_balance,
                "actual": summary.portfolio_actual,
                "forecast": summary.portfolio_forecast,
            }
            week_label = week_label or summary.week_label
        except GsheetsConnectorError as exc:
            log.warning("strategy_memo: cash fetch failed for %s: %s", code, exc)
            entities[code] = {"label": label, "error": True}
        except Exception as exc:  # noqa: BLE001 -- fail-soft per entity
            log.warning("strategy_memo: cash fetch error for %s: %s", code, exc)
            entities[code] = {"label": label, "error": True}
    fetched = sum(1 for e in entities.values() if not e.get("error"))
    return {"ok": fetched > 0, "week_label": week_label, "entities": entities}


# ---------------------------------------------------------------------------
# Gather: pipeline posture (HubSpot)
# ---------------------------------------------------------------------------

def _parse_hs_ts(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def gather_pipeline(
    *,
    fetch_fn: Callable[[str], list[dict[str, Any]]] | None = None,
    stage_names: dict[str, str] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    from .tools import hubspot_client

    if fetch_fn is None:
        fetch_fn = hubspot_client.get_deals_by_pipeline
    pipeline_ids = {"f3e_retail": hubspot_client.PIPELINE_F3E_RETAIL,
                    "default": "default"}
    now = now or time.time()
    aging_cutoff = now - PIPELINE_AGING_DAYS * 86400

    out: dict[str, Any] = {"ok": False, "pipelines": {}}
    for key, label in PIPELINES:
        try:
            deals = fetch_fn(pipeline_ids[key])
        except Exception as exc:  # noqa: BLE001 -- fail-soft per pipeline
            log.warning("strategy_memo: pipeline fetch failed for %s: %s", key, exc)
            out["pipelines"][key] = {"label": label, "error": True}
            continue
        names = stage_names if stage_names is not None else getattr(
            hubspot_client, "_STAGE_NAME_CACHE", {})

        stages: dict[str, dict[str, Any]] = {}
        open_count = 0
        open_amount = 0.0
        aging: list[dict[str, Any]] = []
        for deal in deals:
            props = deal.get("properties") or {}
            stage_id = str(props.get("dealstage") or "")
            stage = names.get(stage_id, stage_id) or "(unknown)"
            if "closed" in stage.lower():
                continue
            try:
                amount = float(props.get("amount") or 0)
            except (TypeError, ValueError):
                amount = 0.0
            open_count += 1
            open_amount += amount
            bucket = stages.setdefault(stage, {"count": 0, "amount": 0.0})
            bucket["count"] += 1
            bucket["amount"] += amount
            modified = _parse_hs_ts(props.get("hs_lastmodifieddate"))
            if modified is not None and modified < aging_cutoff:
                aging.append({
                    "name": str(props.get("dealname") or "(unnamed)")[:80],
                    "stage": stage,
                    "amount": amount,
                    "idle_days": int((now - modified) // 86400),
                })
        aging.sort(key=lambda d: -d["idle_days"])
        out["pipelines"][key] = {
            "label": label,
            "open_count": open_count,
            "open_amount": round(open_amount, 2),
            "stages": stages,
            "aging": aging[:8],
        }
        out["ok"] = True
    return out


# ---------------------------------------------------------------------------
# Gather: stalled P0/P1 decisions (memory/decisions-pending.md)
# ---------------------------------------------------------------------------

def gather_stalled_decisions(*, today: date | None = None) -> dict[str, Any]:
    path = _decisions_pending_path()
    try:
        content = path.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log.warning("strategy_memo: decisions-pending unreadable: %s", exc)
        return {"ok": False, "decisions": []}
    today = today or _today()

    resolved = re.search(r"^## Recently resolved\b", content, re.MULTILINE)
    parseable = content[:resolved.start()] if resolved else content

    decisions: list[dict[str, Any]] = []
    for block in re.split(r"\n(?=### )", parseable):
        if not block.startswith("### "):
            continue
        topic = block.split("\n", 1)[0][4:].strip()
        if topic == "[Topic]":
            continue   # the "How to use" template skeleton, not a real entry
        # The template's "P0 / P1 / P2 / P3" alternatives line must not match;
        # annotated real values ("P0 (decision Monday)") must.
        sev = re.search(r"\*\*Severity\*\*:\s*(P\d)\b(?!\s*/)", block)
        if not sev or sev.group(1) not in ("P0", "P1"):
            continue
        entity_m = re.search(r"\*\*Entity\*\*:\s*([^\n]+)", block)
        entity = entity_m.group(1).strip() if entity_m else "FNDR"
        age_days: int | None = None
        touched = re.search(r"\*\*Last touched\*\*:\s*[^\n]*?(\d{4}-\d{2}-\d{2})", block)
        if touched:
            try:
                age_days = (today - datetime.strptime(
                    touched.group(1), "%Y-%m-%d").date()).days
            except ValueError:
                pass
        owner_m = re.search(r"\*\*Owner of next nudge\*\*:\s*([^\n]+)", block)
        text_blob = topic + " " + entity
        if is_phi_risk(text_blob) or is_visibility_cpa_mention(text_blob):
            continue
        decisions.append({
            "topic": topic[:140],
            "entity": entity[:60],
            "severity": sev.group(1),
            "age_days": age_days,
            "owner": (owner_m.group(1).strip() if owner_m else "unassigned")[:60],
        })
    decisions.sort(key=lambda d: (d["severity"], -(d["age_days"] or 0)))
    return {"ok": True, "decisions": decisions}


# ---------------------------------------------------------------------------
# Gather: portfolio deadline radar (Asana, 14d horizon)
# ---------------------------------------------------------------------------

def _is_lex_task(task: dict[str, Any]) -> bool:
    """LEX tasks are counted, never itemized (aggregate posture)."""
    for proj in task.get("projects") or []:
        name = str((proj or {}).get("name") or "")
        if name.upper().startswith(("[LEX", "[LTS", "[LBHS", "[LLA")):
            return True
    return False


def gather_deadline_radar(
    *,
    today: date | None = None,
    get_tasks_fn: Callable[[str], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    import yaml

    if get_tasks_fn is None:
        from .tools.asana_client import get_user_tasks
        get_tasks_fn = lambda gid: get_user_tasks(gid, max_tasks=100)  # noqa: E731

    try:
        raw = yaml.safe_load(_asana_map_path().read_text(encoding="utf-8")) or {}
        users = raw.get("users") or []
    except Exception as exc:  # noqa: BLE001
        log.warning("strategy_memo: slack-to-asana map unreadable: %s", exc)
        return {"ok": False}

    today = today or _today()
    horizon = today + timedelta(days=DEADLINE_RADAR_DAYS)

    items: list[dict[str, Any]] = []
    overdue_by_owner: dict[str, int] = {}
    due_count = 0
    overdue_count = 0
    lex_aggregate = 0
    users_failed = 0
    for user in users:
        gid = str(user.get("asana_user_gid") or "")
        owner = str(user.get("display_name") or "unknown")
        if not gid:
            continue
        try:
            tasks = get_tasks_fn(gid)
        except Exception as exc:  # noqa: BLE001 -- fail-soft per user
            log.warning("strategy_memo: task fetch failed for %s: %s", owner, exc)
            users_failed += 1
            continue
        for task in tasks:
            if task.get("completed"):
                continue
            due_raw = task.get("due_on") or ""
            try:
                due = datetime.strptime(due_raw, "%Y-%m-%d").date()
            except (TypeError, ValueError):
                continue
            if due > horizon:
                continue
            is_overdue = due < today
            if is_overdue:
                overdue_count += 1
                overdue_by_owner[owner] = overdue_by_owner.get(owner, 0) + 1
            else:
                due_count += 1
            name = str(task.get("name") or "")
            # Aggregate-only for LEX; drop anything PHI-flagged or Visibility.
            if _is_lex_task(task) or is_phi_risk(name) or is_visibility_cpa_mention(name):
                lex_aggregate += 1
                continue
            items.append({
                "name": name[:100],
                "owner": owner,
                "due_on": due_raw,
                "overdue": is_overdue,
            })
    items.sort(key=lambda t: t["due_on"])
    return {
        "ok": True,
        "due_14d": due_count,
        "overdue": overdue_count,
        "overdue_by_owner": dict(sorted(
            overdue_by_owner.items(), key=lambda kv: -kv[1])),
        "items": items[:MAX_RADAR_ITEMS],
        "aggregate_only": lex_aggregate,
        "users_failed": users_failed,
    }


# ---------------------------------------------------------------------------
# Gather: efficiency findings (Phase 3 output)
# ---------------------------------------------------------------------------

def gather_efficiency(*, today: date | None = None) -> dict[str, Any]:
    today = today or _today()
    cutoff = today - timedelta(days=BACKLOG_RECENT_DAYS)

    approved_recent: list[dict[str, str]] = []
    approved_total = 0
    path = _backlog_path()
    if path.exists():
        try:
            for m in re.finditer(r"^## \[(\d{4}-\d{2}-\d{2})\]\s*(.+)$",
                                 path.read_text(encoding="utf-8"), re.MULTILINE):
                approved_total += 1
                try:
                    entry_date = datetime.strptime(m.group(1), "%Y-%m-%d").date()
                except ValueError:
                    continue
                if entry_date >= cutoff:
                    approved_recent.append({"date": m.group(1),
                                            "title": m.group(2).strip()[:120]})
        except Exception as exc:  # noqa: BLE001
            log.warning("strategy_memo: backlog parse failed: %s", exc)

    pending: list[dict[str, str]] = []
    try:
        from .knowledge_review import load_proposed_updates
        for update in load_proposed_updates():
            if update.get("update_type") != "efficiency":
                continue
            if update.get("state") != "PENDING":
                continue
            payload = update.get("payload") or {}
            pending.append({
                "title": str(payload.get("title") or "")[:120],
                "entity": str(payload.get("entity") or "")[:20],
                "route": str(payload.get("route") or "")[:40],
            })
    except Exception as exc:  # noqa: BLE001
        log.warning("strategy_memo: pending efficiency load failed: %s", exc)

    return {"ok": True, "approved_recent": approved_recent,
            "approved_total": approved_total, "pending": pending}


# ---------------------------------------------------------------------------
# Gather: KB activity momentum (aggregate counts only)
# ---------------------------------------------------------------------------

def gather_kb_activity(*, db_path: Path | None = None) -> dict[str, Any]:
    db_path = db_path or _kb_db_path()
    if not db_path.exists():
        return {"ok": False}
    cutoff = int(time.time() - KB_ACTIVITY_DAYS * 86400)
    placeholders = ",".join("?" * len(_KB_SOURCES))
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                f"""
                SELECT entity, COUNT(*) FROM knowledge_chunks
                WHERE ingested_at >= ? AND source IN ({placeholders})
                GROUP BY entity ORDER BY COUNT(*) DESC
                """,
                [cutoff, *_KB_SOURCES],
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("strategy_memo: KB activity query failed: %s", exc)
        return {"ok": False}
    return {"ok": True,
            "by_entity": {str(e or "FNDR").upper(): int(c) for e, c in rows}}


# ---------------------------------------------------------------------------
# Gather: health one-liner
# ---------------------------------------------------------------------------

def gather_health(*, now: float | None = None) -> dict[str, Any]:
    path = _heartbeat_path()
    try:
        raw = path.read_text(encoding="utf-8").strip()
        beat = datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except Exception as exc:  # noqa: BLE001
        log.warning("strategy_memo: heartbeat unreadable: %s", exc)
        return {"ok": False}
    age = int((now or time.time()) - beat)
    if age < 600:
        line = f"Cora healthy (heartbeat {age}s ago)"
    else:
        line = f"Cora heartbeat STALE ({age // 3600}h {age % 3600 // 60}m ago)"
    return {"ok": True, "line": line, "age_seconds": age}


# ---------------------------------------------------------------------------
# Snapshots + deltas
# ---------------------------------------------------------------------------

def save_snapshot(gathered: dict[str, Any], *, today: date | None = None) -> Path:
    today = today or _today()
    snap_dir = _snapshot_dir()
    snap_dir.mkdir(parents=True, exist_ok=True)
    path = snap_dir / f"{today.isoformat()}.json"
    path.write_text(json.dumps(gathered, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    # Retention: keep the newest SNAPSHOT_KEEP snapshots.
    snaps = sorted(snap_dir.glob("????-??-??.json"))
    for old in snaps[:-SNAPSHOT_KEEP]:
        try:
            old.unlink()
        except OSError:
            pass
    return path


def load_prior_snapshots(*, today: date | None = None,
                         limit: int = 8) -> list[dict[str, Any]]:
    """Prior snapshots, newest first, excluding today's."""
    today = today or _today()
    snap_dir = _snapshot_dir()
    if not snap_dir.exists():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(snap_dir.glob("????-??-??.json"), reverse=True):
        if path.stem >= today.isoformat():
            continue
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            continue
        if len(out) >= limit:
            break
    return out


def _cash_balance(snapshot: dict[str, Any], code: str) -> float | None:
    ent = ((snapshot.get("cash") or {}).get("entities") or {}).get(code) or {}
    if ent.get("error"):
        return None
    return ent.get("closing_balance")


def compute_deltas(current: dict[str, Any],
                   priors: list[dict[str, Any]]) -> dict[str, Any]:
    """Week-over-week deltas + multi-week streaks. priors = newest first."""
    if not priors:
        return {"first_run": True}
    prev = priors[0]
    deltas: dict[str, Any] = {"first_run": False,
                              "prev_date": prev.get("date", "")}

    # Cash: WoW delta + consecutive-decline streak per entity.
    cash: dict[str, Any] = {}
    chain = [current] + priors
    for code, _label in CASH_ENTITIES:
        cur = _cash_balance(current, code)
        before = _cash_balance(prev, code)
        if cur is None or before is None:
            continue
        streak = 0
        for newer, older in zip(chain, chain[1:]):
            a, b = _cash_balance(newer, code), _cash_balance(older, code)
            if a is None or b is None or a >= b:
                break
            streak += 1
        cash[code] = {"delta": round(cur - before, 2), "decline_streak": streak}
    deltas["cash"] = cash

    # Pipeline: open totals + per-stage count movement.
    pipes: dict[str, Any] = {}
    for key, _label in PIPELINES:
        cur_p = ((current.get("pipeline") or {}).get("pipelines") or {}).get(key) or {}
        prev_p = ((prev.get("pipeline") or {}).get("pipelines") or {}).get(key) or {}
        if cur_p.get("error") or prev_p.get("error") or not cur_p or not prev_p:
            continue
        stage_moves: dict[str, int] = {}
        all_stages = set(cur_p.get("stages") or {}) | set(prev_p.get("stages") or {})
        for stage in all_stages:
            c = ((cur_p.get("stages") or {}).get(stage) or {}).get("count", 0)
            p = ((prev_p.get("stages") or {}).get(stage) or {}).get("count", 0)
            if c != p:
                stage_moves[stage] = c - p
        pipes[key] = {
            "open_count_delta": (cur_p.get("open_count", 0)
                                 - prev_p.get("open_count", 0)),
            "open_amount_delta": round(cur_p.get("open_amount", 0.0)
                                       - prev_p.get("open_amount", 0.0), 2),
            "stage_moves": stage_moves,
        }
    deltas["pipeline"] = pipes

    # Decisions: how many consecutive memos each current topic has appeared in.
    def _topics(snapshot: dict[str, Any]) -> set[str]:
        return {d.get("topic", "") for d in
                ((snapshot.get("decisions") or {}).get("decisions") or [])}

    unmoved: dict[str, int] = {}
    prior_topic_sets = [_topics(s) for s in priors]
    for topic in _topics(current):
        streak = 1
        for topic_set in prior_topic_sets:
            if topic in topic_set:
                streak += 1
            else:
                break
        if streak >= 2:
            unmoved[topic] = streak
    deltas["unmoved_decisions"] = unmoved
    return deltas


# ---------------------------------------------------------------------------
# Deterministic fact sheet (synthesis input AND fallback memo basis)
# ---------------------------------------------------------------------------

def _fmt_money(value: float | None) -> str:
    if value is None:
        return "--"
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.0f}"


def _fmt_delta(value: float | None) -> str:
    if value is None:
        return ""
    arrow = "up" if value >= 0 else "down"
    return f"({arrow} {_fmt_money(abs(value))} WoW)"


def build_facts_text(gathered: dict[str, Any], deltas: dict[str, Any]) -> str:
    lines: list[str] = []
    today = gathered.get("date", "")
    lines.append(f"PORTFOLIO FACT BASE -- {today}")
    if deltas.get("first_run"):
        lines.append("NOTE: first run -- no prior snapshot, no deltas yet.")
    else:
        lines.append(f"Deltas vs snapshot {deltas.get('prev_date', '')}.")

    # CASH
    lines.append("\n== CASH (Standing ACTUALS) ==")
    cash = gathered.get("cash") or {}
    if cash.get("ok"):
        if cash.get("week_label"):
            lines.append(f"Sheet week: {cash['week_label']}")
        for code, _label in CASH_ENTITIES:
            ent = (cash.get("entities") or {}).get(code) or {}
            if ent.get("error"):
                lines.append(f"- {ent.get('label', code)}: unavailable")
                continue
            d = (deltas.get("cash") or {}).get(code) or {}
            bits = [f"- {ent.get('label', code)}: "
                    f"{_fmt_money(ent.get('closing_balance'))}"]
            if d.get("delta") is not None:
                bits.append(_fmt_delta(d["delta"]))
            if d.get("decline_streak", 0) >= 2:
                bits.append(f"[cash down {d['decline_streak']} weeks straight]")
            lines.append(" ".join(b for b in bits if b))
    else:
        lines.append("(cash source unavailable this week)")

    # PIPELINE
    lines.append("\n== PIPELINE (HubSpot) ==")
    pipeline = gathered.get("pipeline") or {}
    if pipeline.get("ok"):
        for key, label in PIPELINES:
            p = (pipeline.get("pipelines") or {}).get(key) or {}
            if p.get("error") or not p:
                lines.append(f"- {label}: unavailable")
                continue
            d = (deltas.get("pipeline") or {}).get(key) or {}
            head = (f"- {label}: {p.get('open_count', 0)} open deals, "
                    f"{_fmt_money(p.get('open_amount'))}")
            if d:
                head += (f" (count {d.get('open_count_delta', 0):+d}, "
                         f"{_fmt_money(d.get('open_amount_delta'))} WoW)")
            lines.append(head)
            for stage, bucket in (p.get("stages") or {}).items():
                lines.append(f"    {stage}: {bucket.get('count', 0)} / "
                             f"{_fmt_money(bucket.get('amount'))}")
            for move_stage, move in (d.get("stage_moves") or {}).items():
                lines.append(f"    stage move: {move_stage} {move:+d}")
            for deal in (p.get("aging") or [])[:5]:
                lines.append(f"    AGING: {deal['name']} ({deal['stage']}, "
                             f"{_fmt_money(deal['amount'])}, idle "
                             f"{deal['idle_days']}d)")
    else:
        lines.append("(pipeline source unavailable this week)")

    # DECISIONS
    lines.append("\n== STALLED P0/P1 DECISIONS ==")
    decisions = gathered.get("decisions") or {}
    rows = decisions.get("decisions") or []
    if decisions.get("ok") and rows:
        for d in rows[:12]:
            age = f"{d['age_days']}d old" if d.get("age_days") is not None else "age unknown"
            streak = (deltas.get("unmoved_decisions") or {}).get(d["topic"], 0)
            tail = f" [unmoved {streak} memos running]" if streak >= 2 else ""
            lines.append(f"- [{d['severity']}] [{d['entity']}] {d['topic']} "
                         f"({age}; next nudge: {d['owner']}){tail}")
    elif decisions.get("ok"):
        lines.append("(no open P0/P1 decisions)")
    else:
        lines.append("(decisions source unavailable this week)")

    # DEADLINES
    lines.append(f"\n== DEADLINE RADAR (next {DEADLINE_RADAR_DAYS}d) ==")
    radar = gathered.get("deadlines") or {}
    if radar.get("ok"):
        lines.append(f"Due in window: {radar.get('due_14d', 0)} | "
                     f"Overdue: {radar.get('overdue', 0)}")
        owners = radar.get("overdue_by_owner") or {}
        if owners:
            lines.append("Overdue by owner: " + ", ".join(
                f"{name} {count}" for name, count in list(owners.items())[:8]))
        for item in (radar.get("items") or []):
            marker = "OVERDUE" if item.get("overdue") else f"due {item.get('due_on')}"
            lines.append(f"- {item['name']} ({item['owner']}, {marker})")
        if radar.get("aggregate_only"):
            lines.append(f"({radar['aggregate_only']} additional dated tasks "
                         "counted aggregate-only -- LEX/PHI posture)")
    else:
        lines.append("(deadline source unavailable this week)")

    # EFFICIENCY
    lines.append("\n== EFFICIENCY FINDINGS (Phase 3) ==")
    eff = gathered.get("efficiency") or {}
    if eff.get("ok"):
        recent = eff.get("approved_recent") or []
        if recent:
            lines.append("Approved this week:")
            for entry in recent:
                lines.append(f"- [{entry['date']}] {entry['title']}")
        else:
            lines.append("No newly approved efficiency-backlog entries this week "
                         f"({eff.get('approved_total', 0)} total all-time).")
        pending = eff.get("pending") or []
        if pending:
            lines.append("Pending Harrison review:")
            for entry in pending[:6]:
                lines.append(f"- [{entry.get('entity', '?')}] {entry['title']} "
                             f"(route: {entry.get('route', '?')})")
    else:
        lines.append("(efficiency source unavailable this week)")

    # KB ACTIVITY
    lines.append(f"\n== KB ACTIVITY (last {KB_ACTIVITY_DAYS}d, "
                 "swept content per entity -- momentum) ==")
    kb = gathered.get("kb_activity") or {}
    if kb.get("ok"):
        by_entity = kb.get("by_entity") or {}
        lines.append(", ".join(f"{e} {c}" for e, c in
                               list(by_entity.items())[:10]) or "(none)")
    else:
        lines.append("(KB activity unavailable this week)")

    # HEALTH
    lines.append("\n== SYSTEM HEALTH ==")
    health = gathered.get("health") or {}
    lines.append(health.get("line") or "(health signal unavailable this week)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Synthesis (Sonnet, FAIL-CLOSED)
# ---------------------------------------------------------------------------

_SYNTH_PROMPT = """\
You are writing the weekly portfolio strategy memo for Harrison, the founder
of a multi-entity holding portfolio (HJR Global is the holdco / shared-services
spine; F3 Energy, One Stop Nutrition, Lexington Services, HJR Properties,
Big D Media, UFL, HJR Productions are the operating entities).

Below is the verified fact base gathered this week, including week-over-week
deltas. Write a memo of roughly 600-900 words with EXACTLY these five sections:

1. PORTFOLIO PULSE -- 5-line state of the world.
2. WHAT CHANGED -- deltas vs last week's snapshot. If the fact base says this
   is the first run, say "first run -- no deltas yet" honestly.
3. RISKS & DEADLINES -- hard dates, aging items, cash runway flags.
4. RECOMMENDATIONS -- 3 to 5 recommendations. Each one: the recommendation,
   the reasoning, AND the trade-off, tagged with the entity in brackets like
   [F3E]. Apply the holdco lens: when something is duplicated across entities
   or is back-office in nature, ask "should this live at HJR Global?". Fold in
   the approved efficiency-backlog entries where relevant.
5. WATCH LIST -- items not yet actionable but trending.

Hard rules:
- Use ONLY facts present in the fact base. Never invent numbers, deals,
  dates, or names. If a section's source was unavailable, say so plainly.
- Recommendations are ADVISORY for Harrison only; never instruct anyone else
  or imply anything will execute automatically.
- Never include client names, diagnoses, or any client-level health
  information (Lexington data stays aggregate).
- Plain text with the five numbered section headers. No markdown tables.

FACT BASE:
{facts}
"""


def synthesize_memo(facts_text: str) -> str | None:
    """Sonnet synthesis. FAIL-CLOSED: None on any API error -- the caller
    falls back to a deterministic factual rollup, never a hallucinated memo."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        log.warning("strategy_memo: ANTHROPIC_API_KEY not set -- no synthesis")
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=SONNET_MODEL,
            max_tokens=_SYNTH_MAX_TOKENS,
            messages=[{"role": "user",
                       "content": _SYNTH_PROMPT.format(facts=facts_text)}],
        )
        text = (response.content[0].text or "").strip()
    except Exception as exc:  # noqa: BLE001 -- fail-closed by design
        log.warning("strategy_memo: synthesis failed: %s", exc)
        return None
    if not text or is_phi_risk(text):
        return None
    return text


def fallback_memo(facts_text: str) -> str:
    return (
        "SYNTHESIS UNAVAILABLE this week -- below is the factual rollup only "
        "(no recommendations were generated; nothing was hallucinated to fill "
        "the gap).\n\n" + facts_text
    )


# ---------------------------------------------------------------------------
# Delivery (Harrison ONLY) + memo file
# ---------------------------------------------------------------------------

def build_memo_document(memo_body: str, *, today: date | None = None) -> str:
    today = today or _today()
    return (
        f"# Weekly Portfolio Strategy Memo -- {today.isoformat()}\n\n"
        "_Org Synthesis Phase 4. Advisory only -- recommendations execute "
        "nothing. Harrison-only distribution._\n\n"
        f"{memo_body}\n"
    )


def write_memo_file(document: str, *, today: date | None = None) -> Path:
    today = today or _today()
    month_dir = _memo_root() / today.strftime("%Y-%m")
    month_dir.mkdir(parents=True, exist_ok=True)
    path = month_dir / f"{today.isoformat()}_fndr_weekly-strategy-memo.md"
    path.write_text(document, encoding="utf-8")
    return path


def deliver_to_harrison(memo_body: str, *, today: date | None = None) -> bool:
    """DM the memo to Harrison. Recipient is HARD-CODED -- this memo is never
    posted to a channel or any other user's DM."""
    from slack_sdk import WebClient

    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        log.error("strategy_memo: SLACK_BOT_TOKEN not set -- cannot DM")
        return False
    today = today or _today()
    text = (f":compass: *Weekly Portfolio Strategy Memo -- {today.isoformat()}*\n"
            "_Advisory only. Full copy filed to 00-Founder/_strategy-memos._\n\n"
            f"{memo_body}")
    try:
        client = WebClient(token=token)
        resp = client.conversations_open(users=[HARRISON_SLACK_ID])
        client.chat_postMessage(channel=resp["channel"]["id"], text=text[:39000])
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("strategy_memo: DM delivery failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def _safe_gather(label: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """A gatherer that raises degrades to {'ok': False} -- never kills the memo."""
    try:
        result = fn()
        if not isinstance(result, dict):
            return {"ok": False}
        return result
    except Exception as exc:  # noqa: BLE001
        log.warning("strategy_memo: gather '%s' failed: %s", label, exc, exc_info=True)
        return {"ok": False}


def gather_all(*, today: date | None = None) -> dict[str, Any]:
    today = today or _today()
    return {
        "date": today.isoformat(),
        "cash": _safe_gather("cash", gather_cash),
        "pipeline": _safe_gather("pipeline", gather_pipeline),
        "decisions": _safe_gather("decisions",
                                  lambda: gather_stalled_decisions(today=today)),
        "deadlines": _safe_gather("deadlines",
                                  lambda: gather_deadline_radar(today=today)),
        "efficiency": _safe_gather("efficiency",
                                   lambda: gather_efficiency(today=today)),
        "kb_activity": _safe_gather("kb_activity", gather_kb_activity),
        "health": _safe_gather("health", gather_health),
    }


def run_memo(
    *,
    dry_run: bool = False,
    today: date | None = None,
    gather_fn: Callable[[], dict[str, Any]] | None = None,
    synth_fn: Callable[[str], str | None] | None = None,
    deliver_fn: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """One full memo run. dry_run: gather + synthesize but write/send NOTHING
    (no snapshot, no memo file, no DM) -- the rollout-gate review mode."""
    today = today or _today()
    synth_fn = synth_fn or synthesize_memo
    deliver_fn = deliver_fn or (
        lambda body: deliver_to_harrison(body, today=today))

    gathered = gather_fn() if gather_fn else gather_all(today=today)
    priors = load_prior_snapshots(today=today)
    deltas = compute_deltas(gathered, priors)
    facts = build_facts_text(gathered, deltas)

    memo_body = synth_fn(facts)
    synthesized = memo_body is not None
    if memo_body is None:
        memo_body = fallback_memo(facts)

    document = build_memo_document(memo_body, today=today)

    memo_path = ""
    delivered = False
    if not dry_run:
        save_snapshot(gathered, today=today)
        try:
            memo_path = str(write_memo_file(document, today=today))
        except Exception as exc:  # noqa: BLE001 -- file write must not block the DM
            log.error("strategy_memo: memo file write failed: %s", exc)
        delivered = deliver_fn(memo_body)

    return {
        "dry_run": dry_run,
        "date": today.isoformat(),
        "first_run": bool(deltas.get("first_run")),
        "synthesized": synthesized,
        "sections_ok": {k: bool((gathered.get(k) or {}).get("ok"))
                        for k in ("cash", "pipeline", "decisions", "deadlines",
                                  "efficiency", "kb_activity", "health")},
        "memo_path": memo_path,
        "delivered": delivered,
        "facts": facts,
        "memo": memo_body,
    }
