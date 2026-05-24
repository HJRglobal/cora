"""Ad performance client — behavioral contract + Polar Analytics integration.

Wraps polar_client.generate_report() with:
  1. Source-opaque formatting: never expose platform names, account IDs,
     API sources, or channel grouping config in Slack replies
  2. "as of <date>" freshness label only — no tool/system attribution
  3. Option A creative link doctrine (locked 2026-05-23):
       - Spend/ROAS/CAC/CPO/POAS/CM numbers → no links
       - Creative asset rows → include <url|name> Slack link if URL in data
  4. Unknown-answer verbatim string + #f3e-marketing Slack post (24h throttle)
  5. Full audit log to logs/cora-ads-queries.jsonl
  6. Manus snapshot loaded from data/snapshots/ads/manus-insights/latest.yaml

Five public functions correspond to the five ads tools:
  get_performance_summary_text    — blended ROAS, spend, CAC, POAS, ncROAS
  get_channel_breakdown_text      — channel-grouped performance
  get_subbrand_performance_text   — F3 Pure / Mood / Energy split
  get_pixel_attribution_text      — Polar Pixel first-party attribution
  get_cm_waterfall_text           — CM1 → CM4 contribution margin waterfall

Behavioral contract locked 2026-05-23. See ads-territory-spec.md for rationale.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import yaml
from slack_sdk import WebClient as SlackWebClient
from slack_sdk.errors import SlackApiError

from ..connectors.polar_client import PolarConnectorError, PolarReport, generate_report

log = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────────────
# Behavioral contract constants (locked 2026-05-23)
# ────────────────────────────────────────────────────────────────────────────

# Verbatim unknown-answer response. Must be returned exactly as-is.
UNKNOWN_RESPONSE = (
    "I don't have that right now. I will notify the marketing team "
    "immediately to obtain the information and provide the correct and "
    "updated answer when you ask again."
)

# Slack channel (without #) for ad data gap notifications
_ADS_NOTIFY_CHANNEL = "f3e-marketing"

# Throttle window: one notification per topic per 24 hours
_THROTTLE_HOURS = 24

# Default lookback window (days) when the caller doesn't specify
_DEFAULT_LOOKBACK_DAYS = 30

# ────────────────────────────────────────────────────────────────────────────
# Path helpers
# ────────────────────────────────────────────────────────────────────────────

def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _throttle_path() -> Path:
    p = _repo_root() / "data" / "cache" / "ads-notify-throttle.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _audit_log_path() -> Path:
    p = _repo_root() / "logs" / "cora-ads-queries.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _manus_snapshot_path() -> Path:
    return _repo_root() / "data" / "snapshots" / "ads" / "manus-insights" / "latest.yaml"


# ────────────────────────────────────────────────────────────────────────────
# Throttle (same pattern as financial_client)
# ────────────────────────────────────────────────────────────────────────────

def _topic_key(topic: str) -> str:
    return hashlib.md5(topic.lower().strip().encode()).hexdigest()[:8]


def _load_throttle() -> dict:
    path = _throttle_path()
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return {}


def _save_throttle(data: dict) -> None:
    try:
        _throttle_path().write_text(json.dumps(data))
    except Exception as exc:
        log.warning("Could not save ads throttle state: %s", exc)


def is_throttled(topic: str) -> bool:
    data = _load_throttle()
    key = _topic_key(topic)
    last_sent = data.get(key, 0)
    return (time.time() - last_sent) < (_THROTTLE_HOURS * 3600)


def _set_throttled(topic: str) -> None:
    data = _load_throttle()
    data[_topic_key(topic)] = time.time()
    _save_throttle(data)


# ────────────────────────────────────────────────────────────────────────────
# Audit log
# ────────────────────────────────────────────────────────────────────────────

def _audit(
    tool: str,
    entity: str,
    channel: str,
    user: str,
    outcome: str,
    extra: Optional[dict] = None,
) -> None:
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tool": tool,
        "entity": entity,
        "channel": channel,
        "user": user,
        "outcome": outcome,
    }
    if extra:
        record.update(extra)
    try:
        with _audit_log_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception as exc:
        log.warning("Ads audit log write failed: %s", exc)


# ────────────────────────────────────────────────────────────────────────────
# Date helpers
# ────────────────────────────────────────────────────────────────────────────

def _date_range(lookback_days: int) -> tuple[str, str]:
    """Return (date_from, date_to) as YYYY-MM-DD strings for the last N days."""
    today = date.today()
    date_to = (today - timedelta(days=1)).isoformat()   # yesterday (data complete)
    date_from = (today - timedelta(days=lookback_days)).isoformat()
    return date_from, date_to


# ────────────────────────────────────────────────────────────────────────────
# Manus snapshot loader
# ────────────────────────────────────────────────────────────────────────────

def _load_manus_snapshot() -> dict:
    """Load the Manus insights YAML. Returns empty dict if missing/malformed."""
    path = _manus_snapshot_path()
    if not path.exists():
        log.debug("Manus snapshot not found at expected path")
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        log.warning("Failed to load Manus snapshot: %s", exc)
        return {}


# ────────────────────────────────────────────────────────────────────────────
# Number formatters
# ────────────────────────────────────────────────────────────────────────────

def _fmt_currency(val, decimals: int = 0) -> str:
    if val is None:
        return "n/a"
    try:
        v = float(val)
        if decimals:
            return f"${v:,.{decimals}f}"
        return f"${v:,.0f}"
    except (TypeError, ValueError):
        return "n/a"


def _fmt_x(val, decimals: int = 2) -> str:
    """Format as a multiplier (e.g. 3.50x)."""
    if val is None:
        return "n/a"
    try:
        return f"{float(val):.{decimals}f}x"
    except (TypeError, ValueError):
        return "n/a"


def _fmt_pct(val, decimals: int = 1) -> str:
    if val is None:
        return "n/a"
    try:
        return f"{float(val):.{decimals}f}%"
    except (TypeError, ValueError):
        return "n/a"


def _fmt_delta(val, fmt_fn, target, higher_is_better: bool = True) -> str:
    """Format a value with an optional target indicator."""
    formatted = fmt_fn(val)
    if val is None or target is None:
        return formatted
    try:
        v = float(val)
        t = float(target)
        if higher_is_better:
            indicator = "✓" if v >= t else "↓"
        else:
            indicator = "✓" if v <= t else "↑"
        return f"{formatted} {indicator}"
    except (TypeError, ValueError):
        return formatted


def _totals(report: PolarReport) -> dict:
    """Return the totals dict from a PolarReport, falling back to first row."""
    if report.total_data:
        return report.total_data
    if report.table_data:
        return report.table_data[0]
    return {}


# ────────────────────────────────────────────────────────────────────────────
# Gap notification
# ────────────────────────────────────────────────────────────────────────────

def notify_gap(topic: str, channel: str, user: str) -> str:
    """Post an ad data gap alert to #f3e-marketing (throttled 24h). Returns UNKNOWN_RESPONSE."""
    if is_throttled(topic):
        log.debug("Ads gap notification throttled for topic: %s", topic[:40])
        return UNKNOWN_RESPONSE

    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        log.warning("SLACK_BOT_TOKEN missing — cannot post ads gap notification")
        _set_throttled(topic)
        return UNKNOWN_RESPONSE

    try:
        client = SlackWebClient(token=token)
        client.chat_postMessage(
            channel=_ADS_NOTIFY_CHANNEL,
            text=(
                f":bar_chart: *Ad data gap* — Cora couldn't answer a question about: "
                f"*{topic}*\n"
                f"_Channel: {channel} | Requested by: <@{user}>_"
            ),
        )
        _set_throttled(topic)
        log.info("Posted ads gap notification for topic: %s", topic[:40])
    except SlackApiError as exc:
        log.warning("Failed to post ads gap notification: %s", exc)
        _set_throttled(topic)

    return UNKNOWN_RESPONSE


# ────────────────────────────────────────────────────────────────────────────
# Tool 1 — Performance summary
# ────────────────────────────────────────────────────────────────────────────

# Metrics: spend + blended ROAS + ncROAS + POAS + blended CAC + paid ROAS + paid CPA
# + custom Net Revenue After Ads + Amazon cost + Amazon ACoS + Amazon net sales
_SUMMARY_METRICS = [
    "total_marketing_spend",
    "blended_roas",
    "acquisition_roas",
    "poas",
    "blended_cac",
    "paid_roas",
    "paid_cpa",
    "custom_60638",           # Net Revenue After Ads
    "amazonads_campaign.raw.cost",
    "acos",
    "amazonsp_order_items.computed.net_sales_amazon",
]


def get_performance_summary_text(
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
    channel: str = "F3E",
    user: str = "",
) -> str:
    """Fetch blended ad performance summary for the last N days.

    Returns a Slack-formatted text block (source-opaque, no platform names).
    """
    date_from, date_to = _date_range(lookback_days)
    topic = f"ads performance summary {date_from} to {date_to}"

    try:
        report = generate_report(
            metrics=_SUMMARY_METRICS,
            dimensions=[],
            date_from=date_from,
            date_to=date_to,
            granularity="none",
            settings={"attribution_model": "linear"},
        )
    except PolarConnectorError as exc:
        log.warning("Polar connector error (summary): %s", exc)
        _audit("ads_get_performance_summary", "F3E", channel, user, "error", {"error": str(exc)})
        return notify_gap(topic, channel, user)

    t = _totals(report)
    snap = _load_manus_snapshot()
    targets = snap.get("targets", {})

    blended_roas_floor = targets.get("blended_roas_floor")
    cac_ceiling = targets.get("cac_ceiling_usd")
    nc_roas_target = targets.get("nc_roas_target")

    # Amazon figures
    amz_spend = t.get("amazonads_campaign.raw.cost")
    amz_acos = t.get("acos")
    amz_sales = t.get("amazonsp_order_items.computed.net_sales_amazon")
    amz_acos_target = targets.get("amazon_acos_target")

    lines = [
        f"*Ad performance — last {lookback_days}d* (as of {date_to})",
        "",
        f"*Spend:* {_fmt_currency(t.get('total_marketing_spend'))}",
        f"*Blended ROAS:* {_fmt_delta(t.get('blended_roas'), _fmt_x, blended_roas_floor)}",
        f"*Paid ROAS:* {_fmt_x(t.get('paid_roas'))}",
        f"*New-customer ROAS:* {_fmt_delta(t.get('acquisition_roas'), _fmt_x, nc_roas_target)}",
        f"*POAS:* {_fmt_x(t.get('poas'))}",
        f"*Blended CAC:* {_fmt_delta(t.get('blended_cac'), _fmt_currency, cac_ceiling, higher_is_better=False)}",
        f"*Paid CPO:* {_fmt_currency(t.get('paid_cpa'))}",
        f"*Net revenue after ads:* {_fmt_currency(t.get('custom_60638'))}",
    ]

    # Amazon block (only if data present)
    if amz_spend is not None or amz_sales is not None:
        lines.append("")
        lines.append("*Amazon:*")
        if amz_spend is not None:
            lines.append(f"  Spend: {_fmt_currency(amz_spend)}")
        if amz_sales is not None:
            lines.append(f"  Sales: {_fmt_currency(amz_sales)}")
        if amz_acos is not None:
            lines.append(
                f"  ACoS: {_fmt_delta(amz_acos, _fmt_pct, amz_acos_target, higher_is_better=False)}"
            )

    _audit(
        "ads_get_performance_summary", "F3E", channel, user, "ok",
        {"rows": len(report.table_data), "date_from": date_from, "date_to": date_to},
    )
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────────────
# Tool 2 — Channel breakdown
# ────────────────────────────────────────────────────────────────────────────

_CHANNEL_METRICS = [
    "total_marketing_spend",
    "blended_roas",
    "paid_roas",
    "blended_cac",
    "paid_cpa",
    "custom_60638",           # Net Revenue After Ads
]
_CHANNEL_DIMENSION = "custom_internal-default-channel-grouping"


def get_channel_breakdown_text(
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
    channel: str = "F3E",
    user: str = "",
) -> str:
    """Fetch per-channel ad performance breakdown for the last N days.

    Source-opaque: channel grouping names come from Polar's custom dimension
    (configured by Harrison in Polar UI). No platform names in output.
    """
    date_from, date_to = _date_range(lookback_days)
    topic = f"ads channel breakdown {date_from} to {date_to}"

    try:
        report = generate_report(
            metrics=_CHANNEL_METRICS,
            dimensions=[_CHANNEL_DIMENSION],
            date_from=date_from,
            date_to=date_to,
            granularity="none",
            settings={"attribution_model": "linear"},
            ordering=[{"columnKey": "total_marketing_spend", "direction": "DESC"}],
            limit=20,
        )
    except PolarConnectorError as exc:
        log.warning("Polar connector error (channel breakdown): %s", exc)
        _audit("ads_get_channel_breakdown", "F3E", channel, user, "error", {"error": str(exc)})
        return notify_gap(topic, channel, user)

    if not report.table_data:
        return "No channel data for that period."

    totals = _totals(report)
    lines = [
        f"*Ad performance by channel — last {lookback_days}d* (as of {date_to})",
        "",
    ]

    for row in report.table_data:
        ch_label = row.get(_CHANNEL_DIMENSION) or row.get("custom_internal-default-channel-grouping") or "Unknown"
        spend = _fmt_currency(row.get("total_marketing_spend"))
        roas = _fmt_x(row.get("paid_roas") or row.get("blended_roas"))
        cac = _fmt_currency(row.get("blended_cac") or row.get("paid_cpa"))
        lines.append(f"*{ch_label}* — spend {spend} | ROAS {roas} | CAC {cac}")

    lines.extend([
        "",
        f"*Total spend:* {_fmt_currency(totals.get('total_marketing_spend'))}",
        f"*Blended ROAS:* {_fmt_x(totals.get('blended_roas'))}",
    ])

    _audit(
        "ads_get_channel_breakdown", "F3E", channel, user, "ok",
        {"rows": len(report.table_data), "date_from": date_from, "date_to": date_to},
    )
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────────────
# Tool 3 — Sub-brand performance
# ────────────────────────────────────────────────────────────────────────────

_SUBBRAND_METRICS = [
    "total_marketing_spend",
    "blended_roas",
    "paid_roas",
    "blended_cac",
    "custom_60638",           # Net Revenue After Ads
    "custom_60639",           # Subscription Share of Revenue
    "custom_60640",           # Cross-Channel Revenue
]
_SUBBRAND_DIMENSION = "custom_5621"   # F3 Sub-Brand


def get_subbrand_performance_text(
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
    channel: str = "F3E",
    user: str = "",
) -> str:
    """Fetch per-sub-brand (Pure / Mood / Energy) performance for the last N days."""
    date_from, date_to = _date_range(lookback_days)
    topic = f"ads sub-brand performance {date_from} to {date_to}"

    try:
        report = generate_report(
            metrics=_SUBBRAND_METRICS,
            dimensions=[_SUBBRAND_DIMENSION],
            date_from=date_from,
            date_to=date_to,
            granularity="none",
            settings={"attribution_model": "linear"},
            ordering=[{"columnKey": "total_marketing_spend", "direction": "DESC"}],
            limit=10,
        )
    except PolarConnectorError as exc:
        log.warning("Polar connector error (subbrand): %s", exc)
        _audit("ads_get_subbrand_performance", "F3E", channel, user, "error", {"error": str(exc)})
        return notify_gap(topic, channel, user)

    if not report.table_data:
        return "No sub-brand data for that period."

    totals = _totals(report)
    lines = [
        f"*Ad performance by brand — last {lookback_days}d* (as of {date_to})",
        "",
    ]

    for row in report.table_data:
        brand = row.get(_SUBBRAND_DIMENSION) or row.get("custom_5621") or "Unknown"
        spend = _fmt_currency(row.get("total_marketing_spend"))
        roas = _fmt_x(row.get("blended_roas") or row.get("paid_roas"))
        cac = _fmt_currency(row.get("blended_cac"))
        net_rev = row.get("custom_60638")
        sub_share = row.get("custom_60639")
        line = f"*{brand}* — spend {spend} | ROAS {roas} | CAC {cac}"
        if net_rev is not None:
            line += f" | net rev {_fmt_currency(net_rev)}"
        if sub_share is not None:
            line += f" | sub share {_fmt_pct(sub_share)}"
        lines.append(line)

    lines.extend([
        "",
        f"*Total spend:* {_fmt_currency(totals.get('total_marketing_spend'))}",
        f"*Blended ROAS:* {_fmt_x(totals.get('blended_roas'))}",
    ])

    _audit(
        "ads_get_subbrand_performance", "F3E", channel, user, "ok",
        {"rows": len(report.table_data), "date_from": date_from, "date_to": date_to},
    )
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────────────
# Tool 4 — Pixel attribution
# ────────────────────────────────────────────────────────────────────────────

_PIXEL_METRICS = [
    "pixel_roas",
    "pixel_paid_roas",
    "pixel_cac",
    "pixel_paid_cac",
    "pixel_paid_cost_per_order",
    "paid_roas",               # platform-reported ROAS (for delta context)
    "total_marketing_spend",
]


def get_pixel_attribution_text(
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
    channel: str = "F3E",
    user: str = "",
) -> str:
    """Fetch first-party pixel attribution vs platform-reported ROAS.

    Helps identify platform over-reporting. No platform names in output.
    """
    date_from, date_to = _date_range(lookback_days)
    topic = f"ads pixel attribution {date_from} to {date_to}"

    try:
        report = generate_report(
            metrics=_PIXEL_METRICS,
            dimensions=[],
            date_from=date_from,
            date_to=date_to,
            granularity="none",
            settings={},         # pixel metrics don't use attribution model
        )
    except PolarConnectorError as exc:
        log.warning("Polar connector error (pixel): %s", exc)
        _audit("ads_get_pixel_attribution", "F3E", channel, user, "error", {"error": str(exc)})
        return notify_gap(topic, channel, user)

    t = _totals(report)

    pixel_roas = t.get("pixel_roas")
    pixel_paid_roas = t.get("pixel_paid_roas")
    platform_roas = t.get("paid_roas")

    # Attribution delta: platform over-reporting vs first-party pixel
    delta_str = "n/a"
    if pixel_paid_roas is not None and platform_roas is not None:
        try:
            delta = float(platform_roas) - float(pixel_paid_roas)
            sign = "+" if delta >= 0 else ""
            delta_str = f"{sign}{delta:.2f}x (platform reports {sign}{delta:.2f}x vs pixel)"
        except (TypeError, ValueError):
            pass

    lines = [
        f"*First-party attribution — last {lookback_days}d* (as of {date_to})",
        "",
        f"*Pixel ROAS (blended):* {_fmt_x(pixel_roas)}",
        f"*Pixel ROAS (paid):* {_fmt_x(pixel_paid_roas)}",
        f"*Pixel CAC (blended):* {_fmt_currency(t.get('pixel_cac'))}",
        f"*Pixel CAC (paid):* {_fmt_currency(t.get('pixel_paid_cac'))}",
        f"*Pixel CPO (paid):* {_fmt_currency(t.get('pixel_paid_cost_per_order'))}",
        f"*Platform-reported paid ROAS:* {_fmt_x(platform_roas)}",
    ]

    if delta_str != "n/a":
        lines.append(f"*Attribution gap:* {delta_str}")

    lines.extend([
        "",
        f"*Total spend:* {_fmt_currency(t.get('total_marketing_spend'))}",
    ])

    _audit(
        "ads_get_pixel_attribution", "F3E", channel, user, "ok",
        {"date_from": date_from, "date_to": date_to},
    )
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────────────
# Tool 5 — Contribution margin waterfall
# ────────────────────────────────────────────────────────────────────────────

_CM_METRICS = [
    "contribution_margin_1",
    "contribution_margin_1_ratio",
    "contribution_margin_2",
    "contribution_margin_2_ratio",
    "contribution_margin_3",
    "contribution_margin_3_ratio",
    "contribution_margin_4",
    "contribution_margin_4_ratio",
]


def get_cm_waterfall_text(
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
    channel: str = "F3E",
    user: str = "",
) -> str:
    """Fetch CM1 → CM4 contribution margin waterfall for the last N days.

    CM3 is the primary health metric — target floor is in the Manus snapshot.
    """
    date_from, date_to = _date_range(lookback_days)
    topic = f"ads CM waterfall {date_from} to {date_to}"

    try:
        report = generate_report(
            metrics=_CM_METRICS,
            dimensions=[],
            date_from=date_from,
            date_to=date_to,
            granularity="none",
            settings={},
        )
    except PolarConnectorError as exc:
        log.warning("Polar connector error (CM waterfall): %s", exc)
        _audit("ads_get_cm_waterfall", "F3E", channel, user, "error", {"error": str(exc)})
        return notify_gap(topic, channel, user)

    t = _totals(report)
    snap = _load_manus_snapshot()
    cm3_floor = (snap.get("targets") or {}).get("cm3_floor_pct")

    def _cm_line(label: str, val_key: str, pct_key: str, target_pct=None) -> str:
        val = t.get(val_key)
        pct = t.get(pct_key)
        pct_str = _fmt_delta(pct, _fmt_pct, target_pct) if target_pct else _fmt_pct(pct)
        return f"*{label}:* {_fmt_currency(val)} ({pct_str})"

    lines = [
        f"*Contribution margin waterfall — last {lookback_days}d* (as of {date_to})",
        "",
        _cm_line("CM1 (after COGS)", "contribution_margin_1", "contribution_margin_1_ratio"),
        _cm_line("CM2 (after variable opex)", "contribution_margin_2", "contribution_margin_2_ratio"),
        _cm_line("CM3 (after marketing)", "contribution_margin_3", "contribution_margin_3_ratio", cm3_floor),
        _cm_line("CM4 (after fixed opex)", "contribution_margin_4", "contribution_margin_4_ratio"),
    ]

    if cm3_floor:
        lines.append(f"\n_CM3 target floor: {cm3_floor}%_")

    _audit(
        "ads_get_cm_waterfall", "F3E", channel, user, "ok",
        {"date_from": date_from, "date_to": date_to},
    )
    return "\n".join(lines)
