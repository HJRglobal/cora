"""WS7 — write a labeled cash-flow snapshot to a Cowork-readable Drive surface.

The Cowork daily-morning-brief SKILL has no way to read portfolio cash: its own
QBO connector is F3-Energy-Holdings-only (no realm param), and the all-entity cash
lives only in Cora's gsheets connector. This standalone scheduled writer reads the
CF_SUMMARY (portfolio) tab plus the CF_LTS tab and writes a small, labeled JSON
snapshot to a local path on the Google-Drive mount (Drive-for-Desktop syncs it),
which the brief SKILL then reads. NOT bot-loaded — runs as its own scheduled task.

Snapshot contents (LABELED, source-opaque):
  - portfolio ending cash (current week) + the ending-cash outlook
    (current week + next ~4 FORECAST weeks), from the CF_SUMMARY tab
  - the cash-tight LEX-LTS ending-cash line + its outlook, from a separate CF_LTS
    read (null if that read fails). CF_SUMMARY is the portfolio-totals tab and has
    no per-entity rows, so per-entity cash needs its own CF_* tab.
  - freshness: as_of_date, is_stale (sheet behind >10d), data_age_days,
    generated_at_utc (snapshot write time)

Fail-soft: on a read error the previous snapshot is LEFT IN PLACE (a stale-but-
labeled snapshot the consumer can detect beats no data), and the script exits
non-zero so the scheduled task surfaces the failure. The consumer must honor
is_stale / generated_at_utc and show "unavailable" rather than present stale
numbers as current (Harrison-locked: no silent stale fallback).

Source-opacity: the JSON uses canonical entity codes + human row labels only —
never a file id, sheet/tab name, or Drive link. The Cowork SKILL owns user-facing
opacity in its rendered output.

Usage:
    python scripts/write_cashflow_snapshot.py [--out PATH] [--dry-run] [--weeks N]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

# Load .env so GOOGLE_SERVICE_ACCOUNT_JSON (and any other connector creds) are set
# when this runs as a standalone scheduled task OUTSIDE the bot process. Without
# this the Drive/Sheets connector is disabled and the snapshot never writes.
# Matches the convention every other credential-reading script in scripts/ uses.
load_dotenv(dotenv_path=_REPO_ROOT / ".env", override=True)

from cora.connectors import gsheets_financials as gf  # noqa: E402

log = logging.getLogger("write_cashflow_snapshot")

_FOUNDER_OS_ROOT = Path(os.environ.get("FOUNDER_OS_ROOT") or r"G:\My Drive\HJR-Founder-OS")
_DEFAULT_OUT = _FOUNDER_OS_ROOT / "00-Founder" / "_cash-snapshot" / "cashflow-latest.json"


def _default_out_path() -> Path:
    override = os.environ.get("CORA_CASH_SNAPSHOT_PATH", "").strip()
    return Path(override) if override else _DEFAULT_OUT


def _lts_block(lts: gf.CashflowSummary | None, weeks: int) -> dict | None:
    """Build the LEX-LTS ending-cash line from a CF_LTS-tab summary.

    Returns None if the CF_LTS read failed (the portfolio headline still writes).
    LTS is the cash-tight entity Harrison tracks; this surfaces its ending cash +
    the same forward outlook as the portfolio line.
    """
    if lts is None:
        return None
    outlook = lts.ending_cash_outlook(weeks=weeks)
    age = lts.data_age_days()
    return {
        "code": "LEX-LTS",
        "label": "LEX-LTS",
        "week_label": lts.week_label,
        # Mirror the outlook anchor (actual-first); fall back to closing_balance
        # only when the outlook is empty.
        "ending_cash": (outlook[0]["ending_cash"] if outlook else lts.closing_balance),
        "ending_cash_outlook": outlook,
        "is_stale": lts.is_stale() or age is None,
        "data_age_days": age,
    }


def build_snapshot(
    summary: gf.CashflowSummary,
    generated_at_iso: str,
    weeks: int = 4,
    lts_summary: gf.CashflowSummary | None = None,
) -> dict:
    """Serialize the portfolio + LEX-LTS cash lines into the snapshot dict (pure).

    `summary` is the CF_SUMMARY (portfolio) read; `lts_summary` is the separate
    CF_LTS read (None if it failed). CF_SUMMARY is the portfolio-totals tab and has
    no per-entity rows, so per-entity cash comes from its own CF_* tab — here only
    LEX-LTS, the cash-tight entity Harrison tracks.
    """
    outlook = summary.ending_cash_outlook(weeks=weeks)
    age = summary.data_age_days()
    return {
        "generated_at_utc": generated_at_iso,
        "week_label": summary.week_label,
        "as_of_date": summary.as_of_date,
        # Fail-CLOSED freshness (D-051): an unparseable week label -> data_age_days is
        # None -> treat as STALE so the consumer shows "unavailable" rather than
        # presenting unknown-age cash as current.
        "is_stale": summary.is_stale() or age is None,
        "data_age_days": age,
        # Headline ending cash MIRRORS the outlook anchor (same actual-first
        # precedence) so the brief never shows two different "this week ending cash"
        # figures (D-051: closing_balance is forecast-first, the outlook is
        # actual-first; they disagreed mid-week). Falls back to closing_balance only
        # when the outlook is empty (target week not in the series).
        "portfolio_ending_cash": (outlook[0]["ending_cash"] if outlook else summary.closing_balance),
        "ending_cash_outlook": outlook,
        # The cash-tight LEX-LTS ending-cash line, from a SEPARATE CF_LTS read
        # (null if that read failed). CF_SUMMARY cannot supply per-entity cash.
        "lex_lts": _lts_block(lts_summary, weeks),
        "parse_warnings": list(summary.parse_warnings),
        "_note": "ending_cash_outlook is portfolio ending-cash balances (current + "
                 "forecast weeks); lex_lts is the LEX-LTS ending-cash line.",
    }


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp file in the same dir then replace, so a consumer never reads
    # a half-written snapshot.
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Write the labeled cash-flow snapshot.")
    ap.add_argument("--out", default=None, help="Output JSON path (overrides env/default)")
    ap.add_argument("--weeks", type=int, default=4, help="Forecast weeks of ending-cash outlook")
    ap.add_argument("--dry-run", action="store_true", help="Print the snapshot, write nothing")
    args = ap.parse_args(argv)

    out_path = Path(args.out) if args.out else _default_out_path()

    try:
        summary = gf.get_cashflow(tab_name="CF_SUMMARY")
    except gf.GsheetsConnectorError as exc:
        # Fail-soft: leave the previous snapshot in place; surface the failure.
        log.error("Cashflow read failed (%s) — previous snapshot left untouched", exc)
        return 1

    # LEX-LTS ending-cash line: a SEPARATE CF_LTS tab read, FAIL-SOFT. A CF_LTS
    # failure must not sink the snapshot (the portfolio headline is the primary
    # need) -- on error lex_lts is simply null.
    lts_summary = None
    try:
        lts_summary = gf.get_cashflow(tab_name=gf.ENTITY_TO_TAB["LEX-LTS"])
    except Exception as exc:
        # Optional enrichment — ANY failure degrades lex_lts to null and never sinks
        # the portfolio snapshot (the connector wraps API errors, but a truly
        # fail-soft optional read catches everything).
        log.warning("LEX-LTS cashflow read failed (%s) — lex_lts will be null", exc)

    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    snapshot = build_snapshot(summary, generated_at, weeks=args.weeks, lts_summary=lts_summary)

    if args.dry_run:
        print(json.dumps(snapshot, indent=2, ensure_ascii=False))
        log.info("[DRY RUN] would write %d outlook weeks (lex_lts=%s) to %s",
                 len(snapshot["ending_cash_outlook"]),
                 "yes" if snapshot["lex_lts"] else "no", out_path)
        return 0

    try:
        _atomic_write_json(out_path, snapshot)
    except OSError as exc:
        # Fail-soft: e.g. the Drive mount (G:) isn't present when the task fires.
        # Leave the previous snapshot in place; surface the failure via exit code.
        log.error("Cash snapshot write failed (%s) — previous snapshot left untouched", exc)
        return 1
    log.info(
        "Wrote cash snapshot: %s (week=%s, as_of=%s, stale=%s, lex_lts=%s)",
        out_path, summary.week_label, summary.as_of_date, snapshot["is_stale"],
        "yes" if snapshot["lex_lts"] else "no",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
