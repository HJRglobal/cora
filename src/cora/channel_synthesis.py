"""Daily leadership-channel synthesis -- the operational sibling of the weekly
Harrison-only strategy memo (strategy_memo.py).

Two DISTINCT products share the gather/snapshot/synthesis machinery:
  - strategy_memo.py  -> WEEKLY, Harrison-DM-only, blunt founder-strategy memo.
    Its "never post to a channel" invariant is unchanged and lives in its module.
  - channel_synthesis.py (this module) -> DAILY, team-appropriate operational
    syntheses posted to leadership channels (portfolio + 8 entities).

Standalone-script invariant (D-047): this module and its runners must NEVER
import app.py / tool_dispatch.py / claude_client.py, so no bot restart is ever
needed. It reuses strategy_memo's PUBLIC gather/snapshot/facts/delta helpers and
imports only pure modules (phi_guard, reply_formatter, slack_egress,
asana_filters) + connectors lazily.

Guardrails (preserve-list -- see the 2026-07-07 build spec):
  - Financial firewall: cash posts ONLY to a TIER_1 channel. A standalone script
    cannot resolve a channel's tier from its id (channel_classifier is name-based
    and #founder-operations even mis-classifies as unknown/TIER_3), so the
    explicit id allowlist below IS the tier gate, fail-closed.
  - Entity firewall (cross_entity_guard) -- no cross-entity bleed in an entity
    synthesis (post-synthesis assertion).
  - PHI / LEX wall (phi_guard) -- LEX aggregate-only; the LEX synthesis is the
    highest-stakes surface (its own gather + prompt + output backstop).
  - Source-opacity / egress (slack_egress) -- every send through the boundary;
    F3E source-opaque.
  - Visibility-CPA exclusion; D-011 (advisory only, no Asana/decisions/KB writes).
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from . import asana_filters as af
from . import strategy_memo as sm

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Channels + TIER_1 allowlist (the financial-firewall gate)
# ---------------------------------------------------------------------------
# Scope -> target channel id. ALL are TIER_1 (leadership/founder/build per
# channel_classifier.TIER_1_FUNCTIONS), verified private 2026-07-07. HJRG folds
# into the portfolio post (no separate HJRG channel synthesis).
SCOPE_CHANNELS: dict[str, str] = {
    "portfolio": "C0BCUBUDHAR",  # #founder-operations (holdco; covers HJRG)
    "f3e":       "C0B4KRQT3LY",  # #f3e-leadership
    "hjrp":      "C0B3A3W2A3H",  # #hjrp-leadership
    "osn":       "C0B3TCEF4KT",  # #osn-leadership
    "lex":       "C0B3A3U7WS3",  # #lex-leadership
    "bdm":       "C0B3PF5QK9C",  # #bdm-leadership
    "ufl":       "C0B3N5YG1SR",  # #ufl-leadership
    "hjrprod":   "C0BFCM2TV55",  # #hjrprod-leadership
    "f3c":       "C0BFCMB2JFR",  # #f3c-leadership
}

# #cora-build -- the dry-run/smoke target (also TIER_1: function "build").
SMOKE_CHANNEL = "C0B4B0URRQS"

# The financial firewall, defense-in-depth: cash may post ONLY to an id in this
# set. Any other channel id is refused (fail-closed) -- see deliver_to_channel.
_TIER1_CHANNEL_IDS: frozenset[str] = frozenset(SCOPE_CHANNELS.values()) | {SMOKE_CHANNEL}

_MAX_SLACK_CHARS = 39000  # mirror strategy_memo.deliver_to_harrison


def _assert_tier1(channel_id: str) -> bool:
    """True iff channel_id is an allowlisted TIER_1 synthesis target. This is the
    only sanctioned tier check on the standalone path (no runtime id->name map)."""
    return bool(channel_id) and channel_id in _TIER1_CHANNEL_IDS


def _resolve_channel(scope: str, channel: str | None) -> str:
    """Resolve the target channel for a scope, honoring a safe --channel override.

    An override is accepted ONLY if it is the scope's own channel or the smoke
    channel. Any other id is refused (raises) so a manual `--channel` cannot post
    one entity's cash/pipeline into a DIFFERENT entity's leadership channel -- a
    cross-entity confidentiality hole the bare TIER_1 gate would not catch since
    all leadership channels are TIER_1 (D-051 #5)."""
    default = SCOPE_CHANNELS.get(scope)
    if channel is None:
        return default
    if channel == SMOKE_CHANNEL or channel == default:
        return channel
    raise ValueError(
        f"--channel {channel!r} is not allowed for scope {scope!r}: target only its "
        f"own channel ({default}) or the smoke channel ({SMOKE_CHANNEL}).")


def _scrub_visibility_cpa(text: str) -> str:
    """Neutralize Visibility-CPA individual names in any team-facing channel post.

    The Visibility-CPA exclusion: external accounting (Hayden Greber / the Stubbs /
    etc.) must never be named as an action owner or target in Cora's automated
    output. A stalled-decision's free-text "owner of next nudge" can name one
    (e.g. Harrison wrote "Andrew Stubbs or Justin to follow up"), and that owner
    rides into BOTH the synthesis and the deterministic fallback. Applying the
    neutralization at THIS single delivery chokepoint covers every scope and both
    the synth and fallback paths. Fail-open (a broken pattern never blocks a send)."""
    if not text:
        return text
    try:
        from .phi_guard import _VIS_CPA_PATTERN
        return _VIS_CPA_PATTERN.sub("external accounting", text)
    except Exception:  # noqa: BLE001
        return text


# ---------------------------------------------------------------------------
# Delivery -- channel post (sibling to strategy_memo.deliver_to_harrison)
# ---------------------------------------------------------------------------

def deliver_to_channel(channel_id: str, body: str, *, today: date | None = None) -> bool:
    """Post a synthesis to *channel_id*. Fail-soft (log + return False, never raise).

    Financial firewall: refuses (posts NOTHING) if channel_id is not TIER_1
    allowlisted. Routes through the egress boundary -- explicit sanitize_text
    (belt-and-suspenders; the WebClient class patch also sanitizes the text=
    kwarg, but the explicit call is robust to a positional-arg refactor) plus
    normalize_slack_bold (Sonnet emits ** which Slack renders literally).

    Does NOT touch deliver_to_harrison or its hard-coded Harrison-only rule.
    """
    if not _assert_tier1(channel_id):
        log.error("channel_synthesis: refusing to post to non-TIER_1 channel %s "
                  "(financial firewall, fail-closed)", channel_id)
        return False
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        log.error("channel_synthesis: SLACK_BOT_TOKEN not set -- cannot post")
        return False
    if not body:
        log.error("channel_synthesis: empty body -- nothing to post to %s", channel_id)
        return False

    from slack_sdk import WebClient

    from .reply_formatter import normalize_slack_bold
    from .slack_egress import sanitize_text

    text = sanitize_text(
        normalize_slack_bold(_scrub_visibility_cpa(body)))[:_MAX_SLACK_CHARS]
    try:
        client = WebClient(token=token)
        client.chat_postMessage(channel=channel_id, text=text)
        log.info("channel_synthesis: posted to %s (%d chars)", channel_id, len(text))
        return True
    except Exception as exc:  # noqa: BLE001 -- fail-soft; never raise
        log.error("channel_synthesis: post to %s failed: %s", channel_id, exc)
        return False


def _today() -> date:
    return sm._today()


# ---------------------------------------------------------------------------
# Per-scope snapshots (day-over-day deltas; separate from the weekly memo)
# ---------------------------------------------------------------------------

def _synthesis_snapshot_root() -> Path:
    return Path(os.environ.get("SYNTHESIS_SNAPSHOT_DIR")
                or sm._REPO_ROOT / "data" / "state" / "synthesis-snapshots")


def _scope_snapshot_dir(scope: str) -> Path:
    """A distinct snapshot dir per scope (portfolio / f3e / osn / ...), so daily
    deltas never collide with the weekly memo's strategy-memo-snapshots."""
    return _synthesis_snapshot_root() / scope


# ---------------------------------------------------------------------------
# Synthesis (Sonnet, FAIL-CLOSED, output PHI backstop)
# ---------------------------------------------------------------------------

def _default_phi_check(text: str) -> bool:
    """Output backstop for the portfolio + non-LEX entity syntheses.

    Uses the NARROW is_clinical_phi (DOB / ICD-10 / 'diagnosed with X' / bare
    diagnosis terms / medication names). DELIBERATELY NOT the broad is_phi_risk:
    that over-trips on ordinary aggregate program vocab (AHCCCS / Medicaid /
    assessment / discharge / member id) that legitimately appears in a holdco
    operational post's Lexington aggregate line, and would false-drop it to the
    fallback every run. Clinical PHI (a leaked diagnosis / med) is the real
    hazard the backstop must catch; the gather + prompt layers are the primary
    guarantee that no client detail reaches the facts at all. LEX has its own
    stricter output gate (see synthesize_channel_entity)."""
    from .phi_guard import is_clinical_phi
    return is_clinical_phi(text)


def _synthesize(prompt_text: str, *, phi_check=None) -> str | None:
    """One Sonnet synthesis call. FAIL-CLOSED: None on missing key / API error /
    empty output / a positive PHI check -- the caller falls back to a deterministic
    factual rollup, never a hallucinated or PHI-bearing post."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        log.warning("channel_synthesis: ANTHROPIC_API_KEY not set -- no synthesis")
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=sm.SONNET_MODEL,
            max_tokens=sm._SYNTH_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt_text}],
        )
        text = (response.content[0].text or "").strip()
    except Exception as exc:  # noqa: BLE001 -- fail-closed by design
        log.warning("channel_synthesis: synthesis failed: %s", exc)
        return None
    if not text:
        return None
    checker = phi_check or _default_phi_check
    try:
        if checker(text):
            log.warning("channel_synthesis: synthesized output tripped PHI check "
                        "-- dropping to factual fallback")
            return None
    except Exception:  # noqa: BLE001 -- a broken checker must fail CLOSED
        log.exception("channel_synthesis: PHI check raised -- dropping to fallback")
        return None
    return text


_PORTFOLIO_PROMPT = """\
You are writing the DAILY portfolio operations briefing for the #founder-operations
channel of a multi-entity holding company (HJR Global is the holdco / shared-services
spine; the operating entities are F3 Energy, One Stop Nutrition, Lexington Services,
HJR Properties, Big D Media, United Fight League, HJR Productions, F3 Community).

Below is today's verified fact base, including day-over-day deltas. Write a concise,
operational briefing (roughly 200-320 words) with these sections, each on its own
line with a bold header:

*Portfolio pulse* -- 2-3 lines: the overall cash position and the single most
important movement of the day.
*Cash* -- the notable per-entity closing balances and day-over-day changes, plus any
multi-day decline streaks. Lexington is aggregate only.
*Pipeline* -- open deal posture and any stage movement or aging deals worth attention.
*Deadlines* -- what is due soon and what is overdue (counts plus the few that matter).
*Needs Harrison* -- the stalled P0/P1 decisions: the shortest possible list of what
needs a founder call. This is an operational status list, not strategic advice.

Hard rules:
- Use ONLY facts present in the fact base. Never invent numbers, deals, dates, or
  names. If a section's source was unavailable, say so in one short line.
- This is an OPERATIONAL status post for the team -- NOT founder strategy. Do NOT make
  business-restructuring recommendations or blunt strategic calls; those live in the
  private weekly memo. Report the state of the world and what needs a decision.
- Never include any client name, diagnosis, or client-level health information --
  Lexington data stays strictly aggregate.
- Advisory / operational only; nothing here executes automatically.
- Plain text, Slack-friendly. Use *single asterisks* for bold. No markdown tables.

FACT BASE:
{facts}
"""


def synthesize_channel_portfolio(facts_text: str) -> str | None:
    """Operational holdco rollup for #founder-operations (Sonnet, FAIL-CLOSED)."""
    return _synthesize(_PORTFOLIO_PROMPT.format(facts=facts_text))


# ---------------------------------------------------------------------------
# Orchestrator (per-scope; channel_synthesis analog of strategy_memo.run_memo)
# ---------------------------------------------------------------------------

def run_synthesis(
    scope: str,
    *,
    gather_fn,
    synth_fn,
    deliver_fn,
    deltas_fn=None,
    facts_fn=None,
    dry_run: bool = False,
    today: date | None = None,
    snapshot_dir: Path | None = None,
) -> dict:
    """One synthesis run for *scope*. dry_run: gather + synthesize but write/send
    NOTHING (no snapshot, no post) -- the rollout-gate review mode. Snapshots key
    per-scope so day-over-day deltas never collide across scopes or with the
    weekly memo. No Drive memo file is written (channel post + snapshot only)."""
    today = today or _today()
    deltas_fn = deltas_fn or sm.compute_deltas
    facts_fn = facts_fn or sm.build_facts_text
    snap_dir = snapshot_dir or _scope_snapshot_dir(scope)

    gathered = gather_fn()
    priors = sm.load_prior_snapshots(today=today, snapshot_dir=snap_dir)
    deltas = deltas_fn(gathered, priors)
    facts = facts_fn(gathered, deltas)

    body = synth_fn(facts)
    synthesized = body is not None
    if body is None:
        body = sm.fallback_memo(facts)
    # Visibility-CPA neutralization on the FINAL body (synth or fallback) so the
    # dry-run preview faithfully matches what posts; deliver_to_channel repeats it
    # idempotently as a defense-in-depth chokepoint.
    body = _scrub_visibility_cpa(body)

    delivered = False
    if not dry_run:
        sm.save_snapshot(gathered, today=today, snapshot_dir=snap_dir)
        delivered = deliver_fn(body)

    return {
        "scope": scope,
        "dry_run": dry_run,
        "date": today.isoformat(),
        "first_run": bool(deltas.get("first_run")),
        "synthesized": synthesized,
        "delivered": delivered,
        "facts": facts,
        "body": body,
    }


def run_portfolio(*, dry_run: bool = False, today: date | None = None,
                  channel: str | None = None) -> dict:
    """Portfolio synthesis -> #founder-operations (holdco; covers HJRG)."""
    today = today or _today()
    ch = _resolve_channel("portfolio", channel)
    return run_synthesis(
        "portfolio",
        gather_fn=lambda: _portfolio_gather(today),
        synth_fn=synthesize_channel_portfolio,
        deliver_fn=lambda body: deliver_to_channel(ch, body, today=today),
        dry_run=dry_run,
        today=today,
    )


def _portfolio_gather(today: date) -> dict:
    """Portfolio fact base = gather_all, then SCRUB client PHI from any LEX-tagged
    stalled-decision topic (D-051 #1/#2/#7). #founder-operations is a channel, so a
    LEX client name in a decision topic must not reach it via the synth OR the
    deterministic fallback; non-LEX decision topics (business names) are untouched."""
    gathered = sm.gather_all(today=today)
    dec = gathered.get("decisions") or {}
    if dec.get("ok"):
        scrubbed = [
            ({**d, "topic": _scrub_client_phi(str(d.get("topic", "")))}
             if _is_lex_tagged(str(d.get("entity", ""))) else d)
            for d in dec.get("decisions", [])
        ]
        gathered = {**gathered, "decisions": {**dec, "decisions": scrubbed}}
    return gathered


# ===========================================================================
# Per-entity synthesis
# ===========================================================================

_ENTITY_LABELS: dict[str, str] = {
    "F3E":     "F3 Energy",
    "OSN":     "One Stop Nutrition",
    "UFL":     "United Fight League",
    "BDM":     "Big D Media",
    "HJRP":    "HJR Properties",
    "HJRPROD": "HJR Productions",
    "F3C":     "F3 Community",
    "LEX":     "Lexington Services",
}

# Pipeline mode per entity: "f3e" -> the F3E Retail pipeline (all F3E);
# "default" -> the shared default pipeline, subset by f3_entity; absent -> no
# pipeline section (HJRP / HJRPROD / F3C / LEX are not deal-pipeline entities).
_ENTITY_PIPELINE_MODE: dict[str, str] = {
    "F3E": "f3e", "OSN": "default", "UFL": "default", "BDM": "default",
}

# F3C shares F3 Energy's cash tab (entity_to_tab("F3C") == "CF_F3"), so fetching
# it would post F3E's exact figures to #f3c-leadership (D3). Omit F3C cash with an
# explicit note instead.
_CASH_OMIT_ENTITIES: frozenset[str] = frozenset({"F3C"})

# Decision-tag tokens per entity: decisions-pending Entity fields are free-text,
# multi-value (e.g. "F3E, HJRPROD", "F3E / POD"), so match ANY token as a word,
# not string equality (D7).
_ENTITY_DECISION_TOKENS: dict[str, tuple[str, ...]] = {
    "F3E":     ("F3E",),
    "OSN":     ("OSN",),
    "UFL":     ("UFL",),
    "BDM":     ("BDM",),
    "HJRP":    ("HJRP",),
    "HJRPROD": ("HJRPROD", "POD", "FF"),
    "F3C":     ("F3C",),
    # Bare "LLC" dropped (D-051 finding 9): it matched generic corporate suffixes
    # ("HJR Global LLC") and mis-routed non-LEX decisions into #lex-leadership.
    # "LEX" word-matches "LEX-LLC"/"LEX-LTS" etc., so LEX-LLC is still covered.
    "LEX":     ("LEX", "LTS", "LBHS", "LLA"),
}

# A decision is "LEX-tagged" if its free-text Entity field carries any LEX token.
# Used to (a) scrub client PHI from LEX decision topics and (b) EXCLUDE a
# LEX-cross-tagged decision from a non-LEX entity's itemized post (D-051 #1/#2/#7).
_LEX_DECISION_RE = re.compile(r"\b(?:LEX|LTS|LBHS|LLA)\b", re.IGNORECASE)


def _is_lex_tagged(entity_field: str) -> bool:
    return bool(_LEX_DECISION_RE.search(entity_field or ""))


def _scrub_client_phi(text: str) -> str:
    """Redact client-identifying PHI from a short free-text string (a LEX decision
    topic). Uses scrub_lex_phi (staff-preserving) -- same recall-biased redactor as
    the LEX synth output gate -- so client names in a LEX decision topic never reach
    a channel post via EITHER the synthesis or the deterministic fallback (D-051
    #1/#7). Fail-CLOSED: a pathological input that makes the scrub raise collapses to
    a safe placeholder rather than leaking the raw topic."""
    if not text:
        return text
    try:
        from .phi_guard import scrub_lex_phi
        return scrub_lex_phi(text, allowed_names=_lex_staff_names())
    except Exception:  # noqa: BLE001
        return "[LEX decision topic redacted for PHI]"

# Short scope caveat woven into the synth prompt for the low-activity entities.
_ENTITY_SCOPE_NOTE: dict[str, str] = {
    "UFL": ("This entity is currently PAUSED -- a short 'quiet day' post is "
            "expected and correct; do not manufacture activity."),
    "F3C": ("This is a small nonprofit arm -- brief posts are expected; do not "
            "manufacture activity."),
    "HJRPROD": ("This is the media/production arm -- posts may be brief."),
}


# ---------------------------------------------------------------------------
# Entity gathers (scoped, fail-soft)
# ---------------------------------------------------------------------------

def gather_cash_for_entity(entity: str) -> dict:
    """Single-entity closing balance from the Standing ACTUALS tab. F3C omits
    (shares F3E's tab); a dead source degrades to {'error': True}."""
    label = _ENTITY_LABELS.get(entity, entity)
    if entity in _CASH_OMIT_ENTITIES:
        return {"ok": False, "omitted": True, "label": label,
                "note": "Cash is tracked under F3 Energy (shared entity ledger)."}
    from .connectors.gsheets_financials import (
        GsheetsConnectorError, entity_to_tab, get_cashflow,
    )
    try:
        summary = get_cashflow(tab_name=entity_to_tab(entity))
        return {"ok": True, "label": label,
                "closing_balance": summary.closing_balance,
                "week_label": getattr(summary, "week_label", "")}
    except GsheetsConnectorError as exc:
        log.warning("channel_synthesis: cash fetch failed for %s: %s", entity, exc)
        return {"ok": False, "error": True, "label": label}
    except Exception as exc:  # noqa: BLE001 -- fail-soft
        log.warning("channel_synthesis: cash error for %s: %s", entity, exc)
        return {"ok": False, "error": True, "label": label}


def _summarize_deals(deals: list, stage_names: dict, now: float) -> dict:
    """Open-deal rollup (count / amount / stage mix / aging) -- mirrors
    strategy_memo.gather_pipeline's per-pipeline math for a single deal list."""
    aging_cutoff = now - sm.PIPELINE_AGING_DAYS * 86400
    stages: dict[str, dict] = {}
    open_count = 0
    open_amount = 0.0
    aging: list[dict] = []
    for deal in deals:
        props = deal.get("properties") or {}
        stage_id = str(props.get("dealstage") or "")
        stage = stage_names.get(stage_id, stage_id) or "(unknown)"
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
        modified = sm._parse_hs_ts(props.get("hs_lastmodifieddate"))
        if modified is not None and modified < aging_cutoff:
            aging.append({"name": str(props.get("dealname") or "(unnamed)")[:80],
                          "stage": stage, "amount": amount,
                          "idle_days": int((now - modified) // 86400)})
    aging.sort(key=lambda d: -d["idle_days"])
    return {"open_count": open_count, "open_amount": round(open_amount, 2),
            "stages": stages, "aging": aging[:8]}


def gather_pipeline_for_entity(entity: str, *, fetch_fn=None,
                               stage_names: dict | None = None,
                               now: float | None = None) -> dict:
    """Entity pipeline posture. F3E -> the F3E Retail pipeline; OSN/UFL/BDM ->
    the shared default pipeline subset by f3_entity (get_deals_by_filter, which
    server-side filters on f3_entity and drops closed). Other entities omit."""
    mode = _ENTITY_PIPELINE_MODE.get(entity)
    if not mode:
        return {"ok": False, "omitted": True}
    now = now or time.time()
    from .tools import hubspot_client

    if fetch_fn is None:
        if mode == "f3e":
            # The F3E Retail pipeline is single-entity by construction, so fetch the
            # WHOLE pipeline (no f3_entity AND-filter) -- matches strategy_memo's
            # weekly memo and avoids dropping retail deals whose f3_entity tag is
            # unset (D-051 #8/#13). _summarize_deals drops Closed Won/Lost by stage
            # name, so including closed deals here is harmless.
            fetch_fn = lambda: hubspot_client.get_deals_by_pipeline(  # noqa: E731
                hubspot_client.PIPELINE_F3E_RETAIL)
        else:
            # OSN/UFL/BDM genuinely share the default pipeline -> subset by f3_entity.
            fetch_fn = lambda: hubspot_client.get_deals_by_filter(  # noqa: E731
                entity=entity, pipeline_id="default")
    try:
        deals = fetch_fn()
    except Exception as exc:  # noqa: BLE001 -- fail-soft
        log.warning("channel_synthesis: pipeline fetch failed for %s: %s", entity, exc)
        return {"ok": False, "error": True}
    names = stage_names if stage_names is not None else getattr(
        hubspot_client, "_STAGE_NAME_CACHE", {})
    summary = _summarize_deals(deals, names, now)
    return {"ok": True, "label": f"{_ENTITY_LABELS.get(entity, entity)} pipeline",
            **summary}


def gather_deadline_radar_for_entity(entity: str, *, today: date | None = None,
                                     get_tasks_fn=None,
                                     itemize: bool = True) -> dict:
    """Entity-scoped deadline radar (14d horizon). Filters tasks to *entity*'s
    project prefixes. PHI/Visibility-flagged names are counted but NEVER itemized.
    itemize=False (LEX) returns counts only -- no task names at all."""
    import yaml

    if get_tasks_fn is None:
        from .tools.asana_client import get_user_tasks
        get_tasks_fn = lambda gid: get_user_tasks(gid, max_tasks=100)  # noqa: E731
    from .phi_guard import is_phi_risk, is_visibility_cpa_mention

    try:
        raw = yaml.safe_load(sm._asana_map_path().read_text(encoding="utf-8")) or {}
        users = raw.get("users") or []
    except Exception as exc:  # noqa: BLE001
        log.warning("channel_synthesis: asana map unreadable: %s", exc)
        return {"ok": False}

    today = today or _today()
    horizon = today + timedelta(days=sm.DEADLINE_RADAR_DAYS)
    items: list[dict] = []
    overdue_by_owner: dict[str, int] = {}
    due_count = 0
    overdue_count = 0
    redacted = 0
    users_failed = 0
    for user in users:
        gid = str(user.get("asana_user_gid") or "")
        owner = str(user.get("display_name") or "unknown")
        if not gid:
            continue
        try:
            tasks = get_tasks_fn(gid)
        except Exception as exc:  # noqa: BLE001 -- fail-soft per user
            log.warning("channel_synthesis: task fetch failed for %s: %s", owner, exc)
            users_failed += 1
            continue
        for task in tasks:
            if task.get("completed"):
                continue
            if not af.task_belongs_to_entity(task, entity):
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
            # A task cross-listed in a LEX project must never be itemized in a
            # NON-LEX post -- its name can carry a client name that is_phi_risk
            # misses (D-051 #3; mirrors strategy_memo._is_lex_task's unconditional
            # guard). Counted in the aggregate totals above, name never shown.
            lex_cross = entity != "LEX" and af.task_belongs_to_entity(task, "LEX")
            if (not itemize) or lex_cross or is_phi_risk(name) or is_visibility_cpa_mention(name):
                redacted += 1
                continue
            items.append({"name": name[:100], "owner": owner,
                          "due_on": due_raw, "overdue": is_overdue})
    items.sort(key=lambda t: t["due_on"])
    return {
        "ok": True,
        "due_14d": due_count,
        "overdue": overdue_count,
        "overdue_by_owner": dict(sorted(overdue_by_owner.items(),
                                        key=lambda kv: -kv[1])),
        "items": items[:sm.MAX_RADAR_ITEMS],
        "redacted": redacted,
        "users_failed": users_failed,
    }


def gather_decisions_for_entity(entity: str, *, today: date | None = None) -> dict:
    """Stalled P0/P1 decisions filtered to *entity* by word-boundary token match on
    the free-text Entity field (D7). gather_stalled_decisions PHI-filters topic+entity
    with the BROAD is_phi_risk, which misses bare/possessive client names -- so:
      - a NON-LEX entity DROPS any LEX-cross-tagged decision (e.g. Entity "F3E, LEX"):
        its topic can carry a LEX client name that would otherwise reach a non-LEX
        leadership post unscrubbed (D-051 #2).
      - the LEX entity SCRUBS each kept topic through scrub_lex_phi, so a client name
        in a LEX decision topic never reaches #lex-leadership via the synth OR the
        deterministic fallback (D-051 #1/#7)."""
    base = sm.gather_stalled_decisions(today=today)
    if not base.get("ok"):
        return base
    entity = entity.upper()
    tokens = _ENTITY_DECISION_TOKENS.get(entity, (entity,))
    pats = [re.compile(r"\b" + re.escape(t) + r"\b", re.IGNORECASE) for t in tokens]
    is_lex = entity == "LEX"
    kept = []
    for d in base.get("decisions", []):
        ef = str(d.get("entity", ""))
        if not any(p.search(ef) for p in pats):
            continue
        if not is_lex and _is_lex_tagged(ef):
            continue  # LEX-cross-tagged decision excluded from a non-LEX post (PHI)
        if is_lex:
            d = {**d, "topic": _scrub_client_phi(str(d.get("topic", "")))}
        kept.append(d)
    return {"ok": True, "decisions": kept}


def gather_kb_for_entity(entity: str) -> dict:
    """Entity KB momentum (last 7d swept-content count), summing the entity's own
    bucket plus its "ENTITY-" sub-entity buckets (so LEX includes LEX-LLC but HJRP
    excludes HJRPROD)."""
    base = sm.gather_kb_activity()
    if not base.get("ok"):
        return base
    by = base.get("by_entity") or {}
    total = sum(c for e, c in by.items()
                if e == entity or e.startswith(entity + "-"))
    return {"ok": True, "count": total}


# ---------------------------------------------------------------------------
# F3E ecom fold (source-opaque) -- folds run_f3e_ecom_brief into the F3E synthesis
# ---------------------------------------------------------------------------
# Reproduces the proven section logic of scripts/run_f3e_ecom_brief.py so the
# ecom detail (DTC / paid / subscriptions / inventory / production) rides in the
# F3E synthesis and the standalone #f3-ops-cockpit task can be retired (Harrison
# 2026-07-07 decision #3). The RETAIL line is intentionally omitted here -- the
# F3E pipeline section already covers it (no double-report). SOURCE-OPAQUE
# (f3e.md non-negotiable): never name the platform / sheet / ad network. Every
# section fail-soft.

_ECOM_WINDOW_DAYS = 30
_ECOM_RUN2_PROJECT_GID = "1215472268404903"  # [F3E] F3 Production - Run 2
_ECOM_PAID_METRICS = ["total_marketing_spend", "blended_net_sales",
                      "blended_roas", "blended_total_orders"]
_ECOM_PAID_DIMENSION = "custom_internal-default-channel-grouping"
_ECOM_SUB_METRICS = [
    "recharge_sales_products.computed.net_sales",
    "recharge_sales_products.raw.total_active_subscriptions",
]


def _ecom_num(value) -> float:
    if value is None:
        return 0.0
    try:
        return float(str(value).replace(",", "").replace("$", "").strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def _ecom_money(value) -> str:
    return f"${_ecom_num(value):,.0f}"


def gather_f3e_ecom(*, today: date | None = None) -> dict:
    """Source-opaque F3E ecom/ops facts (DTC / paid / subscriptions / inventory /
    production). Fail-soft per section; returns {'ok': True, 'lines': [...]} where
    lines are ready-to-read fact strings. Never raises."""
    today = today or _today()
    lines: list[str] = []

    # DTC
    try:
        from .connectors import shopify_client
        d7 = shopify_client.get_sales_pulse("7d")
        d30 = shopify_client.get_sales_pulse("30d")
        lines.append(f"- DTC: {_ecom_money(d7.net_revenue_usd)} net / "
                     f"{d7.order_count} orders / {_ecom_money(d7.avg_order_value_usd)} "
                     f"AOV (7d); {_ecom_money(d30.net_revenue_usd)} net (30d)")
    except Exception as exc:  # noqa: BLE001
        log.warning("channel_synthesis: F3E DTC section unavailable: %s", exc)
        lines.append("- DTC: not available")

    # Paid (blended) + Subscriptions
    cur_start = today - timedelta(days=_ECOM_WINDOW_DAYS)
    win = (cur_start.isoformat(), today.isoformat())
    try:
        from .connectors import polar_client
        rep = polar_client.generate_report(
            metrics=_ECOM_PAID_METRICS, dimensions=[_ECOM_PAID_DIMENSION],
            date_from=win[0], date_to=win[1], granularity="none")
        spend = rep.total_data.get("total_marketing_spend")
        mer = _ecom_num(rep.total_data.get("blended_roas"))
        bnet = rep.total_data.get("blended_net_sales")
        lines.append(f"- Paid (blended): {_ecom_money(spend)} spend / {mer:.2f}x MER / "
                     f"{_ecom_money(bnet)} net (30d)")
    except Exception as exc:  # noqa: BLE001
        log.warning("channel_synthesis: F3E paid section unavailable: %s", exc)
        lines.append("- Paid (blended): not connected")

    try:
        from .connectors import polar_client
        rep = polar_client.generate_report(
            metrics=_ECOM_SUB_METRICS, dimensions=[],
            date_from=win[0], date_to=win[1], granularity="none")
        net = rep.total_data.get("recharge_sales_products.computed.net_sales")
        active = int(_ecom_num(rep.total_data.get(
            "recharge_sales_products.raw.total_active_subscriptions")))
        lines.append(f"- Subscriptions: {_ecom_money(net)} net / {active} active (30d)")
    except Exception as exc:  # noqa: BLE001
        log.warning("channel_synthesis: F3E subs section unavailable: %s", exc)
        lines.append("- Subscriptions: not connected")

    # Inventory (beverage low-stock; the single F3E inventory source -- supersedes
    # run_inventory_alerts' pulse to avoid double-reporting)
    try:
        from .connectors import shopify_client
        variants = shopify_client.get_inventory_status(low_stock_threshold=10)
        beverages = [v for v in variants if shopify_client.is_beverage_product(
            getattr(v, "product_type", ""), getattr(v, "product_title", ""))]
        low = [v for v in beverages if getattr(v, "low_stock", False)]
        if not low:
            lines.append("- Inventory: all healthy")
        else:
            low.sort(key=lambda v: getattr(v, "qty_on_hand", 0))
            named = "; ".join(
                f"{' '.join(p for p in (v.product_title, v.variant_title) if p)} "
                f"({v.qty_on_hand})" for v in low[:5])
            extra = len(low) - 5
            suffix = f" +{extra} more" if extra > 0 else ""
            lines.append(f"- Inventory: {len(low)} low/critical -- {named}{suffix}")
    except Exception as exc:  # noqa: BLE001
        log.warning("channel_synthesis: F3E inventory section unavailable: %s", exc)
        lines.append("- Inventory: not available")

    # Production (Run-2)
    try:
        from .tools import asana_client
        tasks = asana_client.get_project_tasks(_ECOM_RUN2_PROJECT_GID, max_tasks=100)
        open_tasks = [t for t in tasks if not t.get("completed")]
        overdue = []
        upcoming: list[date] = []
        for t in open_tasks:
            d = asana_client._parse_due_date(t.get("due_on") or t.get("due_at") or "")
            if d is None:
                continue
            (overdue if d < today else upcoming).append(d)
        next_due = min(upcoming).isoformat() if upcoming else "-"
        lines.append(f"- Production (Run-2): {len(open_tasks)} open, "
                     f"{len(overdue)} overdue -- next due {next_due}")
    except Exception as exc:  # noqa: BLE001
        log.warning("channel_synthesis: F3E production section unavailable: %s", exc)
        lines.append("- Production (Run-2): not available")

    return {"ok": True, "lines": lines}


# ---------------------------------------------------------------------------
# Entity gather orchestration + facts + deltas
# ---------------------------------------------------------------------------

def gather_all_for_entity(entity: str, *, today: date | None = None) -> dict:
    """Scoped fact base for one entity, each section fail-soft."""
    entity = entity.upper()
    today = today or _today()
    if entity == "LEX":
        return _gather_all_for_lex(today=today)
    gathered = {
        "entity": entity,
        "date": today.isoformat(),
        "cash": sm._safe_gather("cash", lambda: gather_cash_for_entity(entity)),
        "pipeline": sm._safe_gather(
            "pipeline", lambda: gather_pipeline_for_entity(entity)),
        "decisions": sm._safe_gather(
            "decisions", lambda: gather_decisions_for_entity(entity, today=today)),
        "deadlines": sm._safe_gather(
            "deadlines",
            lambda: gather_deadline_radar_for_entity(entity, today=today)),
        "kb_activity": sm._safe_gather(
            "kb_activity", lambda: gather_kb_for_entity(entity)),
        "health": sm._safe_gather("health", sm.gather_health),
    }
    if entity == "F3E":
        gathered["ecom"] = sm._safe_gather("ecom", lambda: gather_f3e_ecom(today=today))
    return gathered


def _gather_all_for_lex(*, today: date) -> dict:
    """LEX (Lexington Services) aggregate fact base -- the highest-stakes surface.

    AGGREGATE POSTURE by construction: cash is the single LEX-corp consolidated tab
    (no per-sub, no client detail); deadlines are COUNTS ONLY (itemize=False -> no
    task names at all, since a LEX task name can carry a client name); decisions come
    through gather_stalled_decisions which already PHI-filters; no pipeline, no ecom.
    Client-level PHI never enters the facts -- that is the primary guarantee (the
    synth prompt + output PHI gate are backstops, not the guarantee)."""
    return {
        "entity": "LEX",
        "date": today.isoformat(),
        "cash": sm._safe_gather("cash", lambda: gather_cash_for_entity("LEX")),
        "pipeline": {"ok": False, "omitted": True},
        "decisions": sm._safe_gather(
            "decisions", lambda: gather_decisions_for_entity("LEX", today=today)),
        "deadlines": sm._safe_gather(
            "deadlines", lambda: gather_deadline_radar_for_entity(
                "LEX", today=today, itemize=False)),
        "kb_activity": sm._safe_gather("kb_activity", lambda: gather_kb_for_entity("LEX")),
        "health": sm._safe_gather("health", sm.gather_health),
    }


def _change(value: float | None) -> str:
    if value is None:
        return ""
    arrow = "up" if value >= 0 else "down"
    return f"({arrow} {sm._fmt_money(abs(value))} since prior)"


def compute_entity_deltas(entity: str, current: dict, priors: list) -> dict:
    """Day-over-day deltas for a single entity (cash, pipeline, unmoved decisions).
    priors newest-first. Returns {'first_run': True} when no prior snapshot."""
    if not priors:
        return {"first_run": True}
    prev = priors[0]
    out: dict = {"first_run": False, "prev_date": prev.get("date", "")}

    def _cash(snap):
        c = snap.get("cash") or {}
        return c.get("closing_balance") if c.get("ok") else None

    cur, before = _cash(current), _cash(prev)
    if cur is not None and before is not None:
        chain = [current] + priors
        streak = 0
        for newer, older in zip(chain, chain[1:]):
            a, b = _cash(newer), _cash(older)
            if a is None or b is None or a >= b:
                break
            streak += 1
        out["cash"] = {"delta": round(cur - before, 2), "decline_streak": streak}

    cur_p = current.get("pipeline") or {}
    prev_p = prev.get("pipeline") or {}
    if cur_p.get("ok") and prev_p.get("ok"):
        moves: dict[str, int] = {}
        all_stages = set(cur_p.get("stages") or {}) | set(prev_p.get("stages") or {})
        for stage in all_stages:
            c = ((cur_p.get("stages") or {}).get(stage) or {}).get("count", 0)
            p = ((prev_p.get("stages") or {}).get(stage) or {}).get("count", 0)
            if c != p:
                moves[stage] = c - p
        out["pipeline"] = {
            "open_count_delta": cur_p.get("open_count", 0) - prev_p.get("open_count", 0),
            "open_amount_delta": round(cur_p.get("open_amount", 0.0)
                                       - prev_p.get("open_amount", 0.0), 2),
            "stage_moves": moves,
        }

    def _topics(snap):
        return {d.get("topic", "") for d in
                ((snap.get("decisions") or {}).get("decisions") or [])}

    unmoved: dict[str, int] = {}
    prior_sets = [_topics(s) for s in priors]
    for topic in _topics(current):
        streak = 1
        for topic_set in prior_sets:
            if topic in topic_set:
                streak += 1
            else:
                break
        if streak >= 2:
            unmoved[topic] = streak
    out["unmoved_decisions"] = unmoved
    return out


def build_entity_facts_text(entity: str, gathered: dict, deltas: dict) -> str:
    label = _ENTITY_LABELS.get(entity, entity)
    lines: list[str] = [f"{label.upper()} FACT BASE -- {gathered.get('date', '')}"]
    if deltas.get("first_run"):
        lines.append("NOTE: first run -- no prior snapshot, no deltas yet.")
    else:
        lines.append(f"Deltas vs snapshot {deltas.get('prev_date', '')} "
                     "(prior business day).")

    # CASH
    lines.append("\n== CASH ==")
    cash = gathered.get("cash") or {}
    if cash.get("omitted"):
        lines.append(cash.get("note") or "(cash omitted for this entity)")
    elif cash.get("ok"):
        d = (deltas.get("cash") or {})
        bits = [f"- {cash.get('label', label)}: {sm._fmt_money(cash.get('closing_balance'))}"]
        if d.get("delta") is not None:
            bits.append(_change(d["delta"]))
        if d.get("decline_streak", 0) >= 2:
            bits.append(f"[cash down {d['decline_streak']} days straight]")
        # Source-opaque (f3e.md): the label already reads "Week of ..."; do NOT
        # prefix "sheet" -- naming the source is forbidden in the F3E post and a
        # needless source hint elsewhere (D-051 #6).
        if cash.get("week_label"):
            bits.append(f"({cash['week_label']})")
        lines.append(" ".join(b for b in bits if b))
    else:
        lines.append("(cash source unavailable today)")

    # PIPELINE
    pipeline = gathered.get("pipeline") or {}
    if pipeline.get("omitted"):
        pass  # entity has no deal pipeline -- omit the section entirely
    else:
        lines.append("\n== PIPELINE (retail / sales) ==")
        if pipeline.get("ok"):
            d = (deltas.get("pipeline") or {})
            head = (f"- {pipeline.get('open_count', 0)} open deals, "
                    f"{sm._fmt_money(pipeline.get('open_amount'))}")
            if d:
                head += (f" (count {d.get('open_count_delta', 0):+d}, "
                         f"{sm._fmt_money(d.get('open_amount_delta'))} since prior)")
            lines.append(head)
            for stage, bucket in (pipeline.get("stages") or {}).items():
                lines.append(f"    {stage}: {bucket.get('count', 0)} / "
                             f"{sm._fmt_money(bucket.get('amount'))}")
            for move_stage, move in (d.get("stage_moves") or {}).items():
                lines.append(f"    stage move: {move_stage} {move:+d}")
            for deal in (pipeline.get("aging") or [])[:5]:
                lines.append(f"    AGING: {deal['name']} ({deal['stage']}, "
                             f"{sm._fmt_money(deal['amount'])}, idle "
                             f"{deal['idle_days']}d)")
        else:
            lines.append("(pipeline source unavailable today)")

    # ECOM (F3E only)
    ecom = gathered.get("ecom") or {}
    if ecom.get("ok"):
        lines.append("\n== ECOM / OPS (source-opaque) ==")
        lines.extend(ecom.get("lines") or [])

    # DEADLINES
    lines.append("\n== DEADLINES (next 14d) ==")
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
    else:
        lines.append("(deadline source unavailable today)")

    # DECISIONS
    lines.append("\n== STALLED P0/P1 DECISIONS ==")
    decisions = gathered.get("decisions") or {}
    rows = decisions.get("decisions") or []
    if decisions.get("ok") and rows:
        for d in rows[:10]:
            age = f"{d['age_days']}d old" if d.get("age_days") is not None else "age unknown"
            lines.append(f"- [{d['severity']}] {d['topic']} ({age}; "
                         f"next: {d['owner']})")
    elif decisions.get("ok"):
        lines.append("(no open P0/P1 decisions)")
    else:
        lines.append("(decisions source unavailable today)")

    # KB MOMENTUM
    kb = gathered.get("kb_activity") or {}
    if kb.get("ok"):
        lines.append(f"\n== ACTIVITY == last 7d swept content: {kb.get('count', 0)} items")

    # HEALTH
    health = gathered.get("health") or {}
    if health.get("ok"):
        lines.append(f"\n{health.get('line', '')}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entity synthesis prompt + synth (Moved / Needs you / Due / Watch)
# ---------------------------------------------------------------------------

_ENTITY_PROMPT = """\
You are writing the DAILY operational synthesis for the {label} leadership channel.
{scope_note}
Below is today's verified fact base for {label}, with day-over-day deltas. Write a
tight, operational post (roughly 150-220 words) with these sections, each a bold
header on its own line (omit a section that has nothing to say):

*Moved* -- what changed since the prior business day (cash, pipeline, deadlines).
*Needs you* -- the few decisions or stalled items that need a leader's attention.
*Due soon* -- the near-term deadlines and overdue items that matter.
*Watch* -- items trending but not yet actionable (inventory flags, aging deals, momentum).

Hard rules:
- Use ONLY facts present in the fact base. Never invent numbers, deals, dates, or
  names. A quiet day is fine to state plainly.
- SOURCE-OPAQUE: describe activity by function ("DTC", "subscriptions", "paid",
  "retail pipeline", "production"), NEVER name the underlying platform, tool, sheet,
  or ad network.
- Stay STRICTLY within {label}. Do NOT mention, compare against, or route to any
  other portfolio entity.
- Never include client names, diagnoses, or client-level health information.
- Advisory / operational only; nothing here executes automatically.
- Plain text, Slack-friendly. *single asterisks* for bold. No markdown tables.

FACT BASE:
{facts}
"""


_LEX_PROMPT = """\
You are writing the DAILY operational synthesis for the Lexington Services leadership
channel (#lex-leadership). Lexington is a REGULATED Arizona DDD / behavioral-health
services provider. This post is AGGREGATE, GM-level operations ONLY, and is the
highest-stakes channel in the portfolio for privacy.

ABSOLUTE PHI RULES (non-negotiable):
- NEVER include any client / patient / member / participant / resident NAME, initials,
  or any personal identifier.
- NEVER include a diagnosis, medication, date of birth, clinical note, or any
  client-level health information.
- NEVER describe an individual person's status, authorization, billing, eligibility,
  or placement.
- Speak ONLY in aggregate: totals, counts, cash, deadline load by STAFF owner,
  program-level status. If any fact looks client-specific, OMIT it entirely -- do
  not paraphrase or summarize it.

Below is today's verified, AGGREGATE fact base. Write a tight operational post
(roughly 120-180 words) with bold headers (omit any empty section):
*Moved* -- aggregate changes since the prior business day (cash, workload counts).
*Needs you* -- stalled leadership decisions needing attention (no client detail).
*Due soon* -- aggregate deadline counts and overdue load by staff owner.
*Watch* -- aggregate program-level trends worth monitoring.

Other rules:
- Use ONLY facts in the fact base; never invent. A quiet day is fine to state plainly.
- Advisory / operational only; nothing executes automatically.
- Plain text, Slack-friendly. *single asterisks* for bold. No markdown tables.

FACT BASE:
{facts}
"""


def _lex_staff_names() -> set[str]:
    """LEX-context staff roster (from the slack-to-asana map) passed to the name
    scrubber as names to PRESERVE, so a staff owner in the overdue-by-owner line is
    not mistaken for a client. A broader-than-LEX roster is fine (safe direction):
    a client name simply will not be on it and so is redacted near a cue."""
    try:
        import yaml
        raw = yaml.safe_load(sm._asana_map_path().read_text(encoding="utf-8")) or {}
        return {str(u.get("display_name", "")).strip()
                for u in (raw.get("users") or []) if u.get("display_name")}
    except Exception:  # noqa: BLE001
        return set()


def synthesize_channel_lex(facts_text: str) -> str | None:
    """LEX aggregate synthesis (Sonnet, FAIL-CLOSED) with a layered output PHI gate.

    Defense-in-depth (the aggregate GATHER is the PRIMARY guarantee -- no client
    detail is in the facts):
      1. Hard drop-to-fallback on is_clinical_phi (DOB / ICD-10 / diagnosed-with /
         bare dx term / med name). NARROW by design -- it does NOT trip on ordinary
         aggregate program vocab (member / active / AHCCCS / Medicaid / assessment),
         so it never false-blocks a legitimate aggregate post. is_phi_risk and
         is_lex_billing_status_phi are DELIBERATELY excluded from the hard gate for
         exactly that over-trip reason (D4).
      2. scrub_lex_phi redacts client-identifying content -- diagnoses / meds / DOB
         / care-recipient-noun-governed names ("client Maria" -> "client [name
         redacted]") / non-staff possessive names -- WITHOUT the Title-case-near-cue
         pass that would corrupt the "*Moved*"/"*Watch*" section headers. Recall-biased
         (over-redacts a stray possessive), which is the correct LEX posture. Staff
         names preserved via the roster. Wrapped fail-CLOSED (a pathological input
         that makes the scrub raise drops the post rather than risk an unscrubbed one).
      3. Re-check is_clinical_phi on the scrubbed text (belt).
    Accepted residual (documented in phi_guard): a bare client name NOT governed by a
    care-recipient noun and not possessive, produced against instructions -- not
    closable by regex; the aggregate gather + prompt + custodian/channel containment
    are the primary net."""
    from .phi_guard import is_clinical_phi, scrub_lex_phi
    text = _synthesize(_LEX_PROMPT.format(facts=facts_text), phi_check=is_clinical_phi)
    if text is None:
        return None
    try:
        scrubbed = scrub_lex_phi(text, allowed_names=_lex_staff_names())
    except Exception:  # noqa: BLE001 -- cannot guarantee a scrub -> fail closed
        log.exception("channel_synthesis: LEX scrub raised -- dropping to fallback")
        return None
    if is_clinical_phi(scrubbed):
        log.warning("channel_synthesis: LEX output still tripped clinical PHI after "
                    "scrub -- dropping to factual fallback")
        return None
    return scrubbed


def synthesize_channel_entity(entity: str, facts_text: str, *, phi_check=None) -> str | None:
    """Entity operational synthesis (Sonnet, FAIL-CLOSED). LEX routes to the stricter
    aggregate/PHI gate; all others get the source-opaque operational prompt with a
    cross-entity observability check. phi_check overrides the default backstop."""
    entity = entity.upper()
    if entity == "LEX":
        return synthesize_channel_lex(facts_text)
    label = _ENTITY_LABELS.get(entity, entity)
    prompt = _ENTITY_PROMPT.format(
        label=label, scope_note=_ENTITY_SCOPE_NOTE.get(entity, ""),
        facts=facts_text)
    text = _synthesize(prompt, phi_check=phi_check)
    if text is None:
        return None
    # Cross-entity observability (NOT a hard block). The REAL firewall is the
    # entity-scoped GATHER: only this entity's cash / pipeline / deadlines /
    # decisions ever enter the facts, so the synthesis cannot surface another
    # entity's PRIVATE data (structural guarantee, test-pinned). A foreign-entity
    # KEYWORD in the output is almost always a legitimate collaborator/vendor
    # reference from this entity's OWN task ("liaise with Big D Media on the can
    # graphic") -- and since the deterministic fallback carries the SAME facts,
    # dropping to fallback would NOT remove the mention, only degrade quality. So
    # we LOG it for review and keep the post. The prompt still instructs the model
    # to stay strictly within scope. Never fires for the paired F3E/F3C.
    try:
        from .cross_entity_guard import check_cross_entity
        if check_cross_entity(text, entity):
            log.info("channel_synthesis: %s synthesis references another entity "
                     "(likely a legitimate collaborator mention; entity-scoped "
                     "gather is the firewall) -- kept, logged for review", entity)
    except Exception:  # noqa: BLE001 -- observability must never block the post
        log.exception("channel_synthesis: cross-entity check raised")
    return text


def run_entity(entity: str, *, dry_run: bool = False, today: date | None = None,
               channel: str | None = None) -> dict:
    """One entity synthesis -> its leadership channel."""
    entity = entity.upper()
    today = today or _today()
    scope = entity.lower()
    if scope not in SCOPE_CHANNELS:
        raise ValueError(f"no channel configured for entity {entity!r}")
    ch = _resolve_channel(scope, channel)
    return run_synthesis(
        scope,
        gather_fn=lambda: gather_all_for_entity(entity, today=today),
        synth_fn=lambda facts: synthesize_channel_entity(entity, facts),
        deliver_fn=lambda body: deliver_to_channel(ch, body, today=today),
        deltas_fn=lambda gathered, priors: compute_entity_deltas(entity, gathered, priors),
        facts_fn=lambda gathered, deltas: build_entity_facts_text(entity, gathered, deltas),
        dry_run=dry_run,
        today=today,
    )
