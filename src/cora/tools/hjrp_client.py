"""HJR Properties Cora tools.

hjrp_lease_status
    Reads the static lease register (data/maps/hjrp-leases.yaml) and returns a
    renewal-timeline view: per-building leases sorted by days-to-expiry, the
    upcoming renewal cluster(s) with monthly rent at risk, upcoming vacancies,
    and broker contacts.

    Lease economics (monthly rent, rent-at-risk) are financial data under the
    portfolio source-opacity rule -> this tool is TIER_1-gated (#hjrp-finance /
    #hjrp-leadership). The entity system prompt enforces the channel gate; the
    tool itself just formats the register.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any, Optional

import yaml

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]  # src/cora/tools/ -> repo root
_LEASES_PATH = _REPO_ROOT / "data" / "maps" / "hjrp-leases.yaml"

# Urgency thresholds in days-to-expiry.
_CRITICAL_DAYS = 30
_RED_DAYS = 90
_WATCH_DAYS = 180
# Only leases expiring within this window count toward the renewal-cluster view.
_CLUSTER_WINDOW_DAYS = 365

# Emoji markers (presented as-is in tool output, like the contracts dashboard).
_M_ALARM = "\U0001f6a8"   # 🚨 expired or <=30d
_M_RED = "\U0001f534"     # 🔴 <=90d
_M_YELLOW = "\U0001f7e1"  # 🟡 <=180d
_M_OK = "✅"          # ✅ >180d


class HjrpClientError(Exception):
    """Raised when the lease register cannot be read or parsed."""


def _load_properties(path: Path = _LEASES_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        raise HjrpClientError(f"Lease register not found at {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise HjrpClientError(f"Lease register parse error: {exc}") from exc
    props = data.get("properties")
    if not props:
        raise HjrpClientError("Lease register has no properties")
    return props


def _parse_end(value: Any) -> Optional[date]:
    """Return a date for an ISO lease_end, or None for MTM / missing / unparseable."""
    if not value or str(value).upper() == "MTM":
        return None
    try:
        return date.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


def _marker(days: int) -> str:
    if days <= _CRITICAL_DAYS:  # includes expired (negative)
        return _M_ALARM
    if days <= _RED_DAYS:
        return _M_RED
    if days <= _WATCH_DAYS:
        return _M_YELLOW
    return _M_OK


def _days_phrase(days: int) -> str:
    if days < 0:
        return f"expired {abs(days)}d ago"
    if days == 0:
        return "expires today"
    return f"{days}d"


def _broker_name(raw: str) -> str:
    """Strip an <email> suffix from a 'Name <email>' contact string."""
    return raw.split("<")[0].strip() if raw else raw


def format_lease_status(properties: list[dict[str, Any]], today: date) -> str:
    """Build the Slack mrkdwn lease-status dashboard. Pure + deterministic."""
    lines: list[str] = [f"*HJRP Lease Status* — as of {today.isoformat()}", ""]

    cluster_candidates: list[dict[str, Any]] = []  # dated, active, within window
    vacancies: list[dict[str, Any]] = []
    brokers: dict[str, str] = {}  # name -> contact string

    for prop in properties:
        leases = prop.get("leases") or []
        dated: list[tuple[int, dict[str, Any]]] = []
        renewing: list[dict[str, Any]] = []
        mtm: list[dict[str, Any]] = []
        no_date: list[dict[str, Any]] = []

        for lease in leases:
            status = (lease.get("status") or "active").lower()
            if lease.get("broker"):
                brokers[_broker_name(lease["broker"])] = lease["broker"]
            if status == "not_renewing":
                vacancies.append({**lease, "property": prop.get("name")})
            if status == "renewing":
                renewing.append(lease)
                continue
            end = _parse_end(lease.get("lease_end"))
            if end is None:
                if str(lease.get("lease_end") or "").upper() == "MTM":
                    mtm.append(lease)
                else:
                    no_date.append(lease)
                continue
            days = (end - today).days
            dated.append((days, lease))
            if status == "active" and 0 <= days <= _CLUSTER_WINDOW_DAYS:
                cluster_candidates.append({**lease, "end": end, "days": days})

        dated.sort(key=lambda x: x[0])

        name = prop.get("name", "Property")
        addr = prop.get("address", "")
        lines.append(f"*{name} ({addr})* — {len(leases)} leases")

        for days, lease in dated:
            end = _parse_end(lease.get("lease_end"))
            tail = ""
            if lease.get("status", "").lower() == "not_renewing":
                tail = " — NOT renewing, suite goes vacant"
            lines.append(
                f"  {_marker(days)} {lease.get('tenant')} (suite {lease.get('suite')}): "
                f"{end.isoformat()} ({_days_phrase(days)}){tail}"
            )
        for lease in renewing:
            note = lease.get("note") or "renewal in progress"
            lines.append(
                f"  • {lease.get('tenant')} (suite {lease.get('suite')}): renewing — {note}"
            )
        if mtm:
            lines.append("  Month-to-month: " + ", ".join(
                f"{l.get('tenant')} ({l.get('suite')})" for l in mtm
            ))
        if no_date:
            lines.append("  Term not on file: " + ", ".join(
                f"{l.get('tenant')} ({l.get('suite')})" for l in no_date
            ))
        lines.append("")

    # Renewal clusters: dates within the window shared by 2+ active leases.
    by_date: dict[date, list[dict[str, Any]]] = {}
    for c in cluster_candidates:
        by_date.setdefault(c["end"], []).append(c)
    clusters = sorted((d, ls) for d, ls in by_date.items() if len(ls) >= 2)

    if clusters:
        for d, ls in clusters:
            total = sum(int(l.get("monthly_rent") or 0) for l in ls)
            names = ", ".join(l.get("tenant") for l in ls)
            days = (d - today).days
            lines.append(
                f"{_M_ALARM} *Renewal cluster {d.isoformat()}* ({_days_phrase(days)}): "
                f"{len(ls)} leases = ${total:,}/mo at risk ({names}). "
                "Start renewal conversations now."
            )
        lines.append("")

    if vacancies:
        for v in vacancies:
            end = _parse_end(v.get("lease_end"))
            vacant_on = end.toordinal() + 1 if end else None
            when = date.fromordinal(vacant_on).isoformat() if vacant_on else "soon"
            broker = _broker_name(v.get("broker") or "")
            broker_str = f" {broker} relisting." if broker else ""
            lines.append(
                f"{_M_ALARM} *Upcoming vacancy:* {v.get('tenant')} (suite {v.get('suite')}, "
                f"{v.get('property')}) vacant {when}.{broker_str}"
            )
        lines.append("")

    if brokers:
        lines.append("*Brokers:* " + "; ".join(brokers.values()))

    return "\n".join(lines).rstrip()


def get_lease_status(today: Optional[date] = None) -> str:
    """Public entry point — returns Slack mrkdwn, ready to post as-is.

    Returns the standard unavailable message on any data-access failure.
    """
    try:
        properties = _load_properties()
    except HjrpClientError as exc:
        log.warning("hjrp_lease_status: %s", exc)
        return "I don't have that right now."

    if today is None:
        today = date.today()

    result = format_lease_status(properties, today)
    log.info(
        "hjrp_lease_status rendered: %d properties, %d total leases",
        len(properties),
        sum(len(p.get("leases") or []) for p in properties),
    )
    return result
