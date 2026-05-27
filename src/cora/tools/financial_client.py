"""Financial data client -- behavioral contract + Slack notification.

Wraps gsheets_financials.get_cashflow() with:
  1. Source-opaque formatting: never expose file IDs, sheet names, Drive links
  2. "as of <date>" freshness label only
  3. Unknown-answer verbatim string + #hjrg-finance post with 24h throttle
  4. Full audit log to logs/cora-finance-queries.jsonl

Behavioral contract locked 2026-05-21. See decisions.md entry for rationale.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from slack_sdk import WebClient as SlackWebClient
from slack_sdk.errors import SlackApiError

from ..connectors.gsheets_financials import (
    CashflowSummary,
    EntityRow,
    GsheetsConnectorError,
    entity_to_tab,
    get_cashflow,
)

log = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────────────
# Behavioral contract constants (locked 2026-05-21)
# ────────────────────────────────────────────────────────────────────────────

# Verbatim unknown-answer response.  Must be returned exactly as-is.
UNKNOWN_RESPONSE = (
    "I don't have that right now. I will notify the finance department "
    "immediately to obtain the information and provide the correct and "
    "updated answer when you ask again."
)

# Slack channel (without #) for finance gap notifications
_FINANCE_NOTIFY_CHANNEL = "hjrg-finance"

# Throttle window: one notification per topic per 24 hours
_THROTTLE_HOURS = 24

# ────────────────────────────────────────────────────────────────────────────
# File paths
# ────────────────────────────────────────────────────────────────────────────

def _repo_root() -> Path:
    """Resolve the cora repo root (parent of src/)."""
    return Path(__file__).resolve().parents[3]


def _throttle_path() -> Path:
    p = _repo_root() / "data" / "cache" / "finance-notify-throttle.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _audit_log_path() -> Path:
    p = _repo_root() / "logs" / "cora-finance-queries.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


# ────────────────────────────────────────────────────────────────────────────
# Throttle helpers
# ────────────────────────────────────────────────────────────────────────────

def _topic_key(topic: str) -> str:
    """Stable hash of a topic string for use as the throttle dict key."""
    return hashlib.sha1(topic.lower().strip().encode()).hexdigest()[:12]


def _load_throttle() -> dict:
    path = _throttle_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_throttle(data: dict) -> None:
    _throttle_path().write_text(
        json.dumps(data, indent=2), encoding="utf-8"
    )


def is_throttled(topic: str) -> bool:
    """Return True if a notification for this topic was already sent within 24h."""
    data = _load_throttle()
    key = _topic_key(topic)
    last_sent = data.get(key, 0)
    return (time.time() - last_sent) < (_THROTTLE_HOURS * 3600)


def _set_throttled(topic: str) -> None:
    data = _load_throttle()
    data[_topic_key(topic)] = time.time()
    _save_throttle(data)


# ────────────────────────────────────────────────────────────────────────────
# Slack client (built from env -- avoids circular import with app.py)
# ────────────────────────────────────────────────────────────────────────────

def _slack_client() -> SlackWebClient:
    """Build a Slack WebClient using the bot token from the environment."""
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    return SlackWebClient(token=token)


# ────────────────────────────────────────────────────────────────────────────
# Audit log
# ────────────────────────────────────────────────────────────────────────────

def _audit(
    *,
    channel: str,
    user: str,
    query_summary: str,
    result_type: str,
    entity_filter: Optional[str] = None,
) -> None:
    """Append one JSONL line to the finance queries audit log."""
    entry = {
        "ts": time.time(),
        "channel": channel,
        "user": user,
        "query": query_summary,
        "result_type": result_type,
        "entity_filter": entity_filter,
    }
    try:
        with _audit_log_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError as exc:
        log.warning("Could not write finance audit log: %s", exc)


# ────────────────────────────────────────────────────────────────────────────
# Formatting helpers
# ────────────────────────────────────────────────────────────────────────────

def _fmt_currency(val: Optional[float]) -> str:
    if val is None:
        return "—"
    sign = "-" if val < 0 else ""
    return f"{sign}${abs(val):,.0f}"


def _fmt_diff(val: Optional[float]) -> str:
    """Format a variance: negative = over budget, positive = under."""
    if val is None:
        return "-"
    symbol = "+" if val >= 0 else ""
    return f"{symbol}{_fmt_currency(val)}"


def _entity_line(row: EntityRow) -> str:
    """Format a single entity row for Slack mrkdwn."""
    label = row.entity_code if row.entity_code else row.label
    parts = [f"  *{label}*"]
    if row.actual is not None:
        parts.append(f"actual {_fmt_currency(row.actual)}")
    if row.forecast is not None:
        parts.append(f"forecast {_fmt_currency(row.forecast)}")
    if row.diff is not None:
        parts.append(f"diff {_fmt_diff(row.diff)}")
    return " | ".join(parts)


def _format_summary_full(
    s: CashflowSummary,
    entity_filter: Optional[str] = None,
    entity_label: Optional[str] = None,
) -> str:
    """Format a CashflowSummary for Slack.

    entity_filter: if set, filters rows to this entity code only (used for CF_SUMMARY reads).
    entity_label:  human label for the header and totals, e.g. "OSN", "LEX-LLC", "Portfolio".
                   Defaults to "Portfolio" when reading the CF_SUMMARY tab.
    """
    label = entity_label or "Portfolio"
    is_portfolio = label == "Portfolio"

    lines: list[str] = []
    if is_portfolio:
        lines.append(f"*Cash Flow -- {s.week_label}* (as of {s.as_of_date})")
    else:
        lines.append(f"*{label} Cash Flow -- {s.week_label}* (as of {s.as_of_date})")
    lines.append("")

    entities_to_show = s.entities
    if entity_filter:
        code = entity_filter.upper()
        entities_to_show = [
            e for e in s.entities
            if e.entity_code.upper() == code
            or e.entity_code.upper().startswith(code + "-")
        ]
        if not entities_to_show:
            return (
                f"No cash flow data found for *{entity_filter}* "
                f"in {s.week_label} (as of {s.as_of_date}). "
                "Ask Hayden or Justin to confirm the sheet has been updated."
            )

    if entities_to_show:
        for row in entities_to_show:
            lines.append(_entity_line(row))
        lines.append("")

    # Totals -- show for all tabs; label them by entity not "Portfolio"
    if any(
        v is not None for v in
        [s.portfolio_forecast, s.portfolio_actual, s.portfolio_diff]
    ):
        lines.append(f"*{label} Total*")
        if s.portfolio_forecast is not None:
            lines.append(f"  Forecast: {_fmt_currency(s.portfolio_forecast)}")
        if s.portfolio_actual is not None:
            lines.append(f"  Actual:   {_fmt_currency(s.portfolio_actual)}")
        if s.portfolio_diff is not None:
            lines.append(f"  Diff:     {_fmt_diff(s.portfolio_diff)}")
        lines.append("")

    # Balances
    if s.opening_balance is not None:
        lines.append(f"  Opening balance: {_fmt_currency(s.opening_balance)}")
    if s.closing_balance is not None:
        lines.append(f"  Closing balance: {_fmt_currency(s.closing_balance)}")

    return "\n".join(lines).strip()


# ────────────────────────────────────────────────────────────────────────────
# Public interface
# ────────────────────────────────────────────────────────────────────────────

def get_cashflow_text(
    *,
    entity_filter: Optional[str] = None,
    channel: str = "",
    user: str = "",
    question: str = "",
) -> str:
    """Fetch the cashflow sheet and return a Slack-formatted summary string.

    entity_filter: entity code used to select the correct tab (e.g. "OSN", "LEX-LLC").
                   Reads the entity-specific tab directly rather than filtering CF_SUMMARY.
    question: raw user question -- used to detect OSN Core4 (distribution/partner) intent.
    Returns UNKNOWN_RESPONSE on any error (caller should then call notify_gap).
    """
    try:
        entity_code = entity_filter or "FNDR"
        tab = entity_to_tab(entity_code, question=question)
        summary = get_cashflow(tab_name=tab)
        # Entity label for formatter: FNDR/HJRG reads CF_SUMMARY -> "Portfolio";
        # all other entities use their code as the label (e.g. "OSN", "LEX-LLC").
        if entity_code.upper() in ("FNDR", "HJRG", ""):
            e_label = "Portfolio"
        else:
            e_label = entity_code.upper()
        # Tab is already entity-scoped -- no row filtering needed, only labeling.
        result = _format_summary_full(summary, entity_filter=None, entity_label=e_label)
        _audit(
            channel=channel,
            user=user,
            query_summary=f"cashflow entity={entity_filter} tab={tab}",
            result_type="success",
            entity_filter=entity_filter,
        )
        return result
    except GsheetsConnectorError as exc:
        log.error("GsheetsConnectorError in get_cashflow_text: %s", exc)
        _audit(
            channel=channel,
            user=user,
            query_summary=f"cashflow entity={entity_filter}",
            result_type="connector_error",
            entity_filter=entity_filter,
        )
        return UNKNOWN_RESPONSE
    except Exception as exc:
        log.exception("Unexpected error in get_cashflow_text: %s", exc)
        _audit(
            channel=channel,
            user=user,
            query_summary=f"cashflow entity={entity_filter}",
            result_type="unexpected_error",
            entity_filter=entity_filter,
        )
        return UNKNOWN_RESPONSE


def get_osn_pulse_text(
    *,
    channel: str = "",
    user: str = "",
) -> str:
    """Fetch the OSN Consolidated cashflow tab and return a store-by-store financial snapshot.

    Purpose-built for OSN's multi-store structure. Reads the OSN Consolidated tab, uses
    osn_entities() to extract per-store rows, and formats a store-level comparison with
    actual vs forecast variance flags. Source-opaque -- no sheet/file names surfaced.

    Returns UNKNOWN_RESPONSE on any connector or parse error.
    """
    # OSN store code → human-readable label for the Slack output
    _OSN_STORE_LABELS: dict[str, str] = {
        "OSN-GW":  "Gilbert & Warner",
        "OSN-WR":  "Gilbert & Warner",   # alternate code
        "OSN-MK":  "Gilbert & McKellips",
        "OSN-GM":  "Gilbert & McKellips", # alternate code
        "OSN-GF":  "Greenfield & 60",
        "OSN-VV":  "Val Vista & Pecos",
        "OSN-VVP": "Val Vista & Pecos",   # alternate code
    }

    try:
        tab = entity_to_tab("OSN", question="")
        summary = get_cashflow(tab_name=tab)
        store_rows = summary.osn_entities()

        lines: list[str] = [
            f"*OSN Financial Pulse -- {summary.week_label}* (as of {summary.as_of_date})",
            "",
        ]

        if not store_rows:
            lines.append("No store-level rows found in the OSN financial data.")
            lines.append(
                "Ask Hayden or Justin to confirm the OSN Consolidated tab is populated."
            )
        else:
            lines.append("*Store breakdown:*")
            for row in store_rows:
                store_label = _OSN_STORE_LABELS.get(row.entity_code.upper(), row.label)
                parts = [f"  *{store_label}*"]
                if row.actual is not None:
                    parts.append(f"actual {_fmt_currency(row.actual)}")
                if row.forecast is not None:
                    parts.append(f"forecast {_fmt_currency(row.forecast)}")
                if row.diff is not None:
                    diff_str = _fmt_diff(row.diff)
                    # Flag negative actuals vs forecast with a warning emoji
                    flag = " :rotating_light:" if (row.diff is not None and row.diff < 0) else ""
                    parts.append(f"diff {diff_str}{flag}")
                lines.append(" | ".join(parts))

        lines.append("")

        # Portfolio total line
        if any(v is not None for v in [
            summary.portfolio_forecast,
            summary.portfolio_actual,
            summary.portfolio_diff,
        ]):
            lines.append("*OSN Total*")
            if summary.portfolio_forecast is not None:
                lines.append(f"  Forecast: {_fmt_currency(summary.portfolio_forecast)}")
            if summary.portfolio_actual is not None:
                lines.append(f"  Actual:   {_fmt_currency(summary.portfolio_actual)}")
            if summary.portfolio_diff is not None:
                diff_str = _fmt_diff(summary.portfolio_diff)
                flag = " :rotating_light:" if summary.portfolio_diff < 0 else ""
                lines.append(f"  Diff:     {diff_str}{flag}")

        result = "\n".join(lines).strip()
        _audit(
            channel=channel,
            user=user,
            query_summary=f"osn_financial_pulse tab={tab}",
            result_type="success",
            entity_filter="OSN",
        )
        log.info(
            "osn_financial_pulse tab=%s week=%s store_rows=%d as_of=%s",
            tab,
            summary.week_label,
            len(store_rows),
            summary.as_of_date,
        )
        return result

    except GsheetsConnectorError as exc:
        log.error("GsheetsConnectorError in get_osn_pulse_text: %s", exc)
        _audit(
            channel=channel,
            user=user,
            query_summary="osn_financial_pulse",
            result_type="connector_error",
            entity_filter="OSN",
        )
        return UNKNOWN_RESPONSE
    except Exception as exc:
        log.exception("Unexpected error in get_osn_pulse_text: %s", exc)
        _audit(
            channel=channel,
            user=user,
            query_summary="osn_financial_pulse",
            result_type="unexpected_error",
            entity_filter="OSN",
        )
        return UNKNOWN_RESPONSE


def notify_gap(
    topic: str,
    channel: str = "",
    user: str = "",
) -> str:
    """Post a finance gap alert to #hjrg-finance (throttled to once per 24h per topic).

    Returns UNKNOWN_RESPONSE exactly -- this string should be passed back to the
    Slack user as Cora's response. Call freely; deduplication is handled internally.
    """
    _audit(
        channel=channel,
        user=user,
        query_summary=f"gap_notify topic={topic!r}",
        result_type="gap_notify",
    )

    if is_throttled(topic):
        log.debug("Finance gap notification throttled for topic: %s", topic)
        return UNKNOWN_RESPONSE

    _set_throttled(topic)
    try:
        user_ref = f"<@{user}>" if user else "(unknown user)"
        msg = (
            f":warning: *Finance data gap -- Cora could not answer*\n"
            f"> Topic: {topic}\n"
            f"> Channel: {channel or '(unknown)'}\n"
            f"> Requested by: {user_ref}\n"
            "Please update the relevant sheet so Cora can answer correctly "
            "when asked again."
        )
        client = _slack_client()
        client.chat_postMessage(channel=_FINANCE_NOTIFY_CHANNEL, text=msg)
        log.info(
            "Finance gap notification posted to #%s for topic: %s",
            _FINANCE_NOTIFY_CHANNEL,
            topic,
        )
    except SlackApiError as exc:
        log.warning(
            "Slack API error posting finance gap notification: %s", exc.response
        )
    except Exception as exc:
        log.warning("Failed to post finance gap notification: %s", exc)

    return UNKNOWN_RESPONSE
