"""Daily QBO bank snapshot -- live per-account balances + books-freshness (A5 S1).

WHAT THIS IS. A deterministic, read-only sweep of every provisioned QBO realm,
capturing (a) each ACTIVE Bank / Credit Card account's register balance and
(b) the newest posted bank-side transaction date. No model computes anything
here (D-095); every number is a direct source read.

WHAT THIS IS NOT -- read before rendering any figure from it (D-116):

  Balances here come from the QBO **query API** (``Account.CurrentBalance``,
  the account REGISTER balance). The close pack's existing cash section reads
  the **BalanceSheet report**. Verified live 2026-08-04, those two surfaces
  disagree materially and can disagree in SIGN on the SAME account at the SAME
  instant:

      BDM  "Big D Media Chase" (acct 10)   register +11,758.94  report  -8,483.22
      HJRP bank total                      register 128,128.02  report  26,879.52
      OSNGF bank total                     register    -580.95  report   3,516.04

  This is not clock skew and not future-dated activity -- the BDM report figure
  is identical at as-of dates through 2030. They are different measures. So:

    * NEVER present a register balance as "cash per the books".
    * NEVER cross-flag the two as a reconciliation break.
    * ALWAYS carry the basis label (see ``BALANCE_BASIS``).

  Report basis also varies by realm: LEX renders on a Cash basis while the other
  ten render Accrual -- another reason the two surfaces are not comparable.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable

import yaml

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Where the machine-readable snapshot lands (Cora-owned working state).
SNAPSHOT_PATH = _REPO_ROOT / "data" / "state" / "qbo-bank-latest.json"

#: Config seed -- see the file's own header for why it exists.
CONFIG_PATH = _REPO_ROOT / "data" / "maps" / "qbo-bank-snapshot-config.yaml"

#: One-way Drive mirror, relative to the Founder-OS root.
MIRROR_RELPATH = Path("01-HJR-Global") / "accounting" / "live-snapshots" / "qbo-bank-latest.json"

#: The basis label every consumer must render alongside a figure from this file.
BALANCE_BASIS = "QBO account register (Account API)"

#: A snapshot older than this reads as stale, never as current.
DEFAULT_MAX_AGE_HOURS = 24.0

#: Newest-posted-bank-side-txn age past which the pack flags. Generous on purpose:
#: journal-entry-only close work does not advance the date, so a tight threshold
#: would flag correct books.
DEFAULT_STALE_TXN_DAYS = 14


def founder_os_root() -> Path:
    env = os.environ.get("FOUNDER_OS_ROOT", "").strip()
    return Path(env) if env else Path(r"G:\My Drive\HJR-Founder-OS")


def mirror_path() -> Path:
    return founder_os_root() / MIRROR_RELPATH


def stale_txn_days() -> int:
    raw = os.environ.get("FINANCE_BANK_TXN_STALE_DAYS", "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_STALE_TXN_DAYS
    return value if value > 0 else DEFAULT_STALE_TXN_DAYS


# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

def load_config(path: Path | None = None) -> dict[str, Any]:
    """Read the snapshot config. FAIL-CLOSED: any problem disables the portfolio
    total rather than assuming realms are safe to sum."""
    target = path or CONFIG_PATH
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.warning("qbo_bank_snapshot: config unreadable (%s) -- withholding portfolio total", exc)
        return {"portfolio_total": {"enabled": False, "roll_up_verified": False},
                "realms": {}, "_config_error": str(exc)[:200]}
    if not isinstance(raw, dict):
        return {"portfolio_total": {"enabled": False, "roll_up_verified": False},
                "realms": {}, "_config_error": "config root is not a mapping"}
    raw.setdefault("portfolio_total", {})
    raw.setdefault("realms", {})
    return raw


def _shell_realms(config: dict[str, Any]) -> set[str]:
    realms = config.get("realms") or {}
    if not isinstance(realms, dict):
        return set()
    return {
        str(code) for code, cfg in realms.items()
        if isinstance(cfg, dict) and cfg.get("shell") is True
    }


# ─────────────────────────────────────────────────────────────────────────────
# Build
# ─────────────────────────────────────────────────────────────────────────────

def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def build_realm(
    entity: str,
    *,
    query_accounts: Callable[[str], list[dict[str, Any]]],
    summarize: Callable[[list[dict[str, Any]]], dict[str, Any]],
    freshness: Callable[[str, set[str]], dict[str, Any]],
    shell: bool = False,
) -> dict[str, Any]:
    """One realm's block. Never raises -- a failure becomes ``status: error`` so
    one dead realm cannot blank the other ten."""
    block: dict[str, Any] = {
        "status": "ok",
        "error": None,
        "shell": shell,
        "basis": BALANCE_BASIS,
        "as_of_utc": _utc_now_iso(),
    }
    try:
        accounts = query_accounts(entity)
        summary = summarize(accounts)
        bank_ids = {a["id"] for a in accounts if a.get("type") == "Bank" and a.get("id")}
        fresh = freshness(entity, bank_ids)
        block.update({
            "accounts": accounts,
            **{k: summary[k] for k in (
                "bank_count", "cc_count", "bank_total", "cc_total",
                "cash_net_of_cards", "bank_unknown", "cc_unknown", "balances_complete",
            )},
            "newest_bank_txn_date": fresh.get("date"),
            "newest_bank_txn_per_type": fresh.get("per_type") or {},
            "freshness_types_covered": fresh.get("types_covered"),
            "freshness_types_expected": fresh.get("types_expected"),
            "freshness_errors": fresh.get("errors") or {},
        })
    except Exception as exc:  # noqa: BLE001 -- per-realm fail-soft is the contract
        log.error("qbo_bank_snapshot: realm %s failed: %s", entity, exc)
        block.update({
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}"[:300],
            "accounts": [],
            # Deliberately NOT zeroed: a failed realm must render UNKNOWN, never $0.
            "bank_total": None, "cc_total": None, "cash_net_of_cards": None,
            "balances_complete": False,
            "newest_bank_txn_date": None,
        })
    return block


def _portfolio_block(
    realms: dict[str, dict[str, Any]],
    config: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Portfolio roll-up, or (None, reason) when it must be withheld.

    Withheld when: disabled in config, roll-up not verified, ANY realm errored
    (a partial sum presented as a portfolio total is the D-117 failure), any
    realm's balances are incomplete, or -- the automatic safety belt -- a realm
    declared a cash-less SHELL is observed carrying money, which breaks the very
    premise that makes summing non-double-counting.
    """
    pt = config.get("portfolio_total") or {}
    if not pt.get("enabled", False):
        return None, "portfolio total disabled in config"
    if not pt.get("roll_up_verified", False):
        return None, "roll-up not verified in config (would risk double-counting)"

    errored = sorted(c for c, b in realms.items() if b.get("status") != "ok")
    if errored:
        return None, f"{len(errored)} realm(s) unavailable: {', '.join(errored)}"

    incomplete = sorted(c for c, b in realms.items() if not b.get("balances_complete"))
    if incomplete:
        return None, f"incomplete balances in: {', '.join(incomplete)}"

    shells = _shell_realms(config)
    for code in sorted(shells & set(realms)):
        block = realms[code]
        if (block.get("bank_total") or 0) != 0 or (block.get("cc_total") or 0) != 0:
            return None, (
                f"realm {code} is configured as a cash-less shell but is carrying a "
                f"balance -- summing may now double-count"
            )

    contributing = sorted(c for c in realms if c not in shells)
    return {
        "bank_total": round(sum(realms[c]["bank_total"] for c in contributing), 2),
        "cc_total": round(sum(realms[c]["cc_total"] for c in contributing), 2),
        "cash_net_of_cards": round(
            sum(realms[c]["cash_net_of_cards"] for c in contributing), 2
        ),
        "realms_included": contributing,
        "shell_realms_excluded": sorted(shells & set(realms)),
        "basis": BALANCE_BASIS,
    }, None


def build_snapshot(
    entities: list[str],
    *,
    query_accounts: Callable[[str], list[dict[str, Any]]],
    summarize: Callable[[list[dict[str, Any]]], dict[str, Any]],
    freshness: Callable[[str, set[str]], dict[str, Any]],
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the full snapshot. Coverage rides the file as STRUCTURE (D-117)."""
    cfg = config if config is not None else load_config()
    shells = _shell_realms(cfg)

    realms: dict[str, dict[str, Any]] = {}
    for entity in entities:
        realms[entity] = build_realm(
            entity,
            query_accounts=query_accounts,
            summarize=summarize,
            freshness=freshness,
            shell=entity in shells,
        )

    covered = sum(1 for b in realms.values() if b.get("status") == "ok")
    portfolio, withheld = _portfolio_block(realms, cfg)

    return {
        "generated_at_utc": _utc_now_iso(),
        "basis": BALANCE_BASIS,
        "covered": covered,
        "expected": len(entities),
        "realms": realms,
        "portfolio": portfolio,
        "portfolio_withheld_reason": withheld,
        "errors": {c: b.get("error") for c, b in realms.items() if b.get("status") != "ok"},
    }


# ─────────────────────────────────────────────────────────────────────────────
# Persist / load
# ─────────────────────────────────────────────────────────────────────────────

def write_snapshot(snapshot: dict[str, Any], path: Path | None = None) -> Path:
    """Atomic local write (temp + replace) so a reader never sees a partial file."""
    target = path or SNAPSHOT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(target)
    return target


def load_snapshot(path: Path | None = None) -> dict[str, Any] | None:
    """Read the snapshot, or None when absent/unparseable. Callers must render
    'unavailable' on None -- never a zero."""
    target = path or SNAPSHOT_PATH
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("qbo_bank_snapshot: could not read %s: %s", target, exc)
        return None


def snapshot_age_hours(snapshot: dict[str, Any], now: datetime.datetime | None = None) -> float | None:
    """Age of a snapshot in hours, or None when the stamp is missing/unparseable.

    None means UNKNOWN age -- consumers must treat that as stale, not as fresh.
    """
    raw = (snapshot or {}).get("generated_at_utc")
    if not raw:
        return None
    try:
        stamped = datetime.datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=datetime.timezone.utc)
    current = now or datetime.datetime.now(datetime.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=datetime.timezone.utc)
    return (current - stamped).total_seconds() / 3600.0


def txn_age_days(
    txn_date: str | None,
    today: datetime.date | None = None,
) -> int | None:
    """Age in days of a posted transaction date. None when absent/unparseable.

    Clamped at 0: QBO legitimately carries future-dated transactions (F3E held a
    Deposit dated tomorrow on 2026-08-04), and a negative age would render as
    nonsense rather than as "current".
    """
    if not txn_date:
        return None
    try:
        posted = datetime.date.fromisoformat(str(txn_date)[:10])
    except ValueError:
        return None
    return max(0, ((today or datetime.date.today()) - posted).days)
