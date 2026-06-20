"""WS7 — write a labeled cash-flow snapshot to a Cowork-readable Drive surface.

The Cowork daily-morning-brief SKILL has no way to read portfolio cash: its own
QBO connector is F3-Energy-Holdings-only (no realm param), and the all-entity cash
lives only in Cora's gsheets connector. This standalone scheduled writer does ONE
read of the CF_SUMMARY tab and writes a small, labeled JSON snapshot to a local
path on the Google-Drive mount (Drive-for-Desktop syncs it), which the brief SKILL
then reads. NOT bot-loaded — runs as its own scheduled task.

Snapshot contents (each LABELED with entity + as-of date):
  - portfolio ending cash (current week)
  - the ending-cash outlook: current week + next ~4 FORECAST weeks
  - per-entity weekly cash-flow rows (incl. LEX-LTS)
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


def build_snapshot(summary: gf.CashflowSummary, generated_at_iso: str, weeks: int = 4) -> dict:
    """Serialize a CashflowSummary into the labeled snapshot dict (pure)."""
    lts = summary.entity_by_code("LEX-LTS")
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
        # Per-entity WEEKLY CASH-FLOW rows (forecast/actual), not ending balances.
        "entities": [
            {
                "code": e.entity_code,
                "label": e.label,
                "forecast": e.forecast,
                "actual": e.actual,
            }
            for e in summary.entities
        ],
        # Convenience pointer for the cash-tight LEX-LTS line (from CF_SUMMARY).
        "lex_lts": (
            {"code": lts.entity_code, "label": lts.label,
             "forecast": lts.forecast, "actual": lts.actual}
            if lts else None
        ),
        "parse_warnings": list(summary.parse_warnings),
        "_note": "Per-entity rows are weekly cash flow; ending_cash_outlook is "
                 "portfolio ending-cash balances (current + forecast weeks).",
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

    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    snapshot = build_snapshot(summary, generated_at, weeks=args.weeks)

    if args.dry_run:
        print(json.dumps(snapshot, indent=2, ensure_ascii=False))
        log.info("[DRY RUN] would write %d entities + %d outlook weeks to %s",
                 len(snapshot["entities"]), len(snapshot["ending_cash_outlook"]), out_path)
        return 0

    try:
        _atomic_write_json(out_path, snapshot)
    except OSError as exc:
        # Fail-soft: e.g. the Drive mount (G:) isn't present when the task fires.
        # Leave the previous snapshot in place; surface the failure via exit code.
        log.error("Cash snapshot write failed (%s) — previous snapshot left untouched", exc)
        return 1
    log.info(
        "Wrote cash snapshot: %s (week=%s, as_of=%s, stale=%s, %d entities)",
        out_path, summary.week_label, summary.as_of_date, snapshot["is_stale"],
        len(snapshot["entities"]),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
