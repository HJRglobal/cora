"""13-week cashflow SHADOW LEDGER -- weekly QBO actuals store (M2/S2).

WHAT THIS IS. The machine-side record of what actually moved through the banks
each week, per QBO realm, banked beside the forecast snapshots M1 writes. It is
what lets the parallel run ask "was last week's forecast right?" without anybody
retyping bank figures into the sheet.

TWO WINDOWS EVERY RUN, and the distinction is the whole point:

  PRELIMINARY (week W-1)  the week that just ended. QBO is structurally
                          INCOMPLETE for it at Monday 06:25 -- the transcript
                          says so out loud [26:39-27:07]: the bank feed has not
                          downloaded Friday-through-Sunday yet. Stamped
                          `posted-through <newest bank-side txn date>` and never
                          used for comparison or accuracy math.
  FINALIZED (week W-2)    a re-pull of the week before that, now matured in QBO.
                          It SUPERSEDES the preliminary file for the same week.
                          Every comparison and accuracy consumer binds here.

An append-only store that only ever wrote the just-ended week would freeze that
undercount forever and then measure forecast accuracy against it (D-051 Fin-1).

THE CASH PERIMETER. A cash event is a transaction touching a BANK account.
Buying supplies on a company card is NOT one -- no bank balance moves; PAYING
the card is, dated when the money actually leaves. Counting both double-counts
every carded dollar; counting only the purchase dates the outflow wrong. Card
purchases are therefore excluded BY CONSTRUCTION, and `cashflow_maps` refuses to
load a map that points a credit-card liability account at an expense category.

WHAT THIS IS NOT.

  * NOT the sheet's measure. These are BANK-CASH flows on a txn-date basis. The
    sheet's balance rows are Cash/CC (D-120). Reconcile like-for-like only, and
    say which is which -- every figure here carries `basis`.
  * NOT canonical. The Standing ACTUALS sheet stays canonical in v1 (fork F1).
    Nothing here writes to the sheet, ever.
  * NOT a zero when it is an UNKNOWN. A realm that errored, or whose every
    transaction type came back empty, records UNKNOWN and is excluded from the
    coverage numerator. Reporting $0 of activity for a realm we failed to read is
    the failure this store exists to avoid.

HR LLC (personal books) and the cash-less OSN shell are excluded at COLLECTION,
by the map loader, not merely un-rendered -- this file is mirrored into a folder
Justin and Hayden work in (D-124).
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable, Optional

from cora import cashflow_ledger as cl
from cora import cashflow_maps as cm

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Local canonical store, beside M1's forecast snapshots.
ACTUALS_DIR = cl.STORE_DIR / "actuals"

#: One-way Drive mirror, relative to the Founder-OS root.
MIRROR_ACTUALS_RELDIR = cl.MIRROR_RELDIR / "actuals"

#: Payload contract version -- bump when a consumer-visible field changes shape.
SCHEMA_VERSION = 1

WINDOW_PRELIMINARY = "preliminary"
WINDOW_FINALIZED = "finalized"

#: The basis label every consumer must render alongside a figure from this file.
FLOW_BASIS = (
    "QBO bank-account register lines (General Ledger, Accrual), bank-cash only, "
    "by transaction date"
)

#: What the cash perimeter means, carried in the payload so a reader never has to
#: guess whether card spend is in or out.
PERIMETER_NOTE = (
    "Cash events are transactions touching a BANK account. A card purchase is "
    "not a cash event; the bank-to-card PAYMENT is."
)

#: Tolerance on the GL-vs-recompute residual. Both sides read the same ledger, so
#: the honest expectation is exact agreement -- live 2026-08-05 it was $0.00
#: across 36 realm-weeks. A cent absorbs float representation, nothing more.
TIE_OUT_TOLERANCE = 0.01


class ActualsError(Exception):
    """A structural failure that must not overwrite a good window."""


# ────────────────────────────────────────────────────────────────────────────
# Paths / store
# ────────────────────────────────────────────────────────────────────────────

def actuals_filename(week_ending: datetime.date, window_kind: str) -> str:
    """Keyed by the WEEK it describes, not the run date.

    That is what makes "the finalized re-pull supersedes its preliminary" a real
    operation a consumer can perform: both files for one week sort together and
    `load_actuals` prefers the finalized one. Keying on the run date would leave
    two files describing the same week with no relationship between them. Same
    reasoning as M1's absolute week-ending keys -- the weekly column-roll ritual
    does not exist in the machine layer.
    """
    suffix = "final" if window_kind == WINDOW_FINALIZED else "prelim"
    return f"{week_ending.isoformat()}_{suffix}.json"


def actuals_path(week_ending: datetime.date, window_kind: str) -> Path:
    return ACTUALS_DIR / actuals_filename(week_ending, window_kind)


def mirror_actuals_path(week_ending: datetime.date, window_kind: str) -> Path:
    return (cl.founder_os_root() / MIRROR_ACTUALS_RELDIR
            / actuals_filename(week_ending, window_kind))


def load_window(week_ending: datetime.date, window_kind: str) -> Optional[dict]:
    path = actuals_path(week_ending, window_kind)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("could not read actuals %s: %s", path.name, exc)
        return None


def load_actuals(week_ending: datetime.date) -> Optional[dict]:
    """The best available payload for a week: FINALIZED if present, else preliminary.

    Comparison and accuracy consumers must not use this -- they bind to
    `load_finalized` only. This is for surfaces that legitimately show a
    provisional figure WITH its stamp (the Monday worksheet's pre-staged actuals).
    """
    return (load_window(week_ending, WINDOW_FINALIZED)
            or load_window(week_ending, WINDOW_PRELIMINARY))


def load_finalized(week_ending: datetime.date) -> Optional[dict]:
    """The matured window only. The single entry point for accuracy math."""
    return load_window(week_ending, WINDOW_FINALIZED)


def list_finalized_weeks() -> list[datetime.date]:
    """Every week with a finalized payload, oldest first."""
    if not ACTUALS_DIR.exists():
        return []
    out: list[datetime.date] = []
    for path in ACTUALS_DIR.glob("*_final.json"):
        try:
            out.append(datetime.date.fromisoformat(path.name.split("_", 1)[0]))
        except ValueError:
            log.warning("ignoring unparseable actuals filename: %s", path.name)
    return sorted(out)


def window_coverage(week_ending: datetime.date, window_kind: str) -> Optional[tuple[int, int]]:
    """(covered, expected) for a banked window, or None if unreadable.

    The missed-run check needs this: a dated FILE is not evidence of a banked
    week -- it could hold zero readable realms (D-127c).
    """
    payload = load_window(week_ending, window_kind)
    if not isinstance(payload, dict):
        return None
    covered, expected = payload.get("covered"), payload.get("expected")
    if not isinstance(covered, int) or not isinstance(expected, int):
        return None
    return covered, expected


# ────────────────────────────────────────────────────────────────────────────
# Week resolution -- from DATA, never from a calendar assumption
# ────────────────────────────────────────────────────────────────────────────

def resolve_windows(
    today: datetime.date,
    *,
    weekday_name: Optional[str] = None,
) -> tuple[str, datetime.date, datetime.date]:
    """(week_ending_weekday, W-1 ending, W-2 ending).

    The week-ending DAY comes from the sheet's own resolved grid, banked by M1 --
    never a hardcoded Friday (D-051 Fin-13). Any snapshot will do: the weekday is
    a stable property of the workbook, so an older snapshot answers just as well
    as this morning's and S2 does not fail merely because S1 missed a Monday.

    Raises ActualsError when no snapshot has ever been banked. That is the honest
    outcome: without the sheet's grid we do not know what "week ending" means
    here, and assuming Friday is precisely the assumption D-126/Fin-13 forbid.
    """
    if weekday_name is None:
        for snapshot_date in reversed(cl.list_snapshot_dates()):
            snapshot = cl.load_snapshot(snapshot_date)
            candidate = (snapshot or {}).get("week_ending_weekday")
            if candidate:
                weekday_name = str(candidate)
                break

    if not weekday_name:
        raise ActualsError(
            "no forecast snapshot carries a week-ending weekday -- run "
            "scripts/run_cashflow_forecast_snapshot.py first. The week grid is "
            "derived from the sheet, never assumed."
        )

    w1 = cl.last_completed_week_ending(weekday_name, today)
    if w1 is None:
        raise ActualsError(f"{weekday_name!r} is not a weekday name")
    return weekday_name, w1, w1 - datetime.timedelta(days=7)


# ────────────────────────────────────────────────────────────────────────────
# Rendering safety
# ────────────────────────────────────────────────────────────────────────────

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

#: Reason CODES for the payload. The underlying exception text carries the realm
#: id and the request URI (googleapiclient/httpx embed it) and lands in a folder
#: Justin and Hayden work in. Detail stays in the local, unmirrored log (D-127g).
_REASON_CODES: tuple[tuple[str, str], ...] = (
    ("auth error", "auth_error"),
    ("token", "auth_error"),
    ("HTTP 4", "api_client_error"),
    ("HTTP 5", "api_server_error"),
    ("did not carry the expected", "report_shape_changed"),
    ("HTTP error reaching", "network_error"),
    ("non-JSON", "bad_response"),
)


def reason_code(reason: str) -> str:
    """Map free-text failure text to a fixed, id-free, figure-free code."""
    text = str(reason or "")
    for needle, code in _REASON_CODES:
        if needle in text:
            return code
    return "unknown"


def safe_label(value: Any, limit: int = 80) -> str:
    """Neutralise a human-typed label before it enters the payload (D-123).

    Tab names and category rows are typed by people into YAML and into the sheet.
    They are data, not markup: strip control characters (which include the Slack
    control bytes a downstream render would otherwise carry) and bound the length.
    """
    text = _CONTROL_CHARS.sub("", str(value or "")).strip()
    return text[:limit]


# ────────────────────────────────────────────────────────────────────────────
# Per-realm build
# ────────────────────────────────────────────────────────────────────────────

def classify_rows(
    rows: list[dict],
    realm: str,
    category_map: cm.CategoryMap,
) -> dict[str, Any]:
    """Split a window's register rows onto sheet category rows.

    Keyed on ACCOUNT IDS, never on display names: the General Ledger's Split cell
    carries an account id whenever there is exactly one counterpart account, and
    a name-keyed map would break the first time somebody renames an account (and
    would carry LEX account names into this payload, which D-124 forbids).

    A `-Split-` row (several counterparts) has no single account to categorise
    onto, and an UNCONFIRMED mapping deliberately resolves to nothing. Both land
    in `uncategorized` WITH a reason count, so a reader can see how much of the
    week is unplaced instead of reading a tidy but partial category table.
    """
    categories: dict[str, float] = {}
    unplaced = 0.0
    unplaced_rows = 0
    reasons: dict[str, int] = {}

    for row in rows:
        amount = row.get("amount")
        if amount is None:
            continue
        account_id = row.get("split_account_id")
        category = (category_map.category_for(realm, account_id)
                    if account_id else None)
        if category:
            key = safe_label(category)
            categories[key] = round(categories.get(key, 0.0) + amount, 2)
            continue

        unplaced = round(unplaced + amount, 2)
        unplaced_rows += 1
        if not account_id:
            reasons["multi_line_split"] = reasons.get("multi_line_split", 0) + 1
        elif category_map.mapping(realm, account_id) is None:
            reasons["account_not_in_map"] = reasons.get("account_not_in_map", 0) + 1
        else:
            reasons["mapping_unconfirmed"] = reasons.get("mapping_unconfirmed", 0) + 1

    return {
        "categories": categories,
        "uncategorized": {
            "amount": unplaced,
            "rows": unplaced_rows,
            "reasons": reasons,
        },
    }


def split_gross(rows: list[dict]) -> dict[str, Any]:
    """Gross receipts / disbursements, with INTERNAL bank moves taken out of both.

    An internal move -- money leaving one of our own bank accounts and landing in
    another -- shows up in an account-filtered ledger as two rows under ONE
    transaction id whose amounts cancel. Left in, it inflates receipts AND
    disbursements by the same figure, so a $15K sweep between two accounts reads
    as $15K of income and $15K of spend in a week that saw neither. Net flow is
    unaffected either way, which is exactly why this is easy to miss.

    Grouped by transaction id (present on 100% of General Ledger data rows), so
    this is exact rather than a date/amount pairing heuristic. Generalised past
    `Transfer` on purpose: an internal sweep booked as a journal entry has the
    same two-legs-cancelling shape and the same absence of cash effect. A
    bank-to-CARD payment keeps only ONE leg inside the bank perimeter, so its
    group does not cancel and it stays a real disbursement -- which it is.
    """
    groups: dict[Any, list[dict]] = {}
    loose: list[dict] = []
    for row in rows:
        if row.get("amount") is None:
            continue
        txn_id = row.get("txn_id")
        if txn_id:
            groups.setdefault(txn_id, []).append(row)
        else:
            loose.append(row)

    internal = 0.0
    counted: list[dict] = list(loose)
    for members in groups.values():
        total = sum(r["amount"] for r in members)
        if len(members) > 1 and abs(total) <= 0.005:
            internal += sum(abs(r["amount"]) for r in members) / 2.0
            continue
        counted.extend(members)

    return {
        "receipts": round(sum(r["amount"] for r in counted if r["amount"] > 0), 2),
        "disbursements": round(sum(r["amount"] for r in counted if r["amount"] < 0), 2),
        "internal_transfers_excluded": round(internal, 2),
    }


def build_realm(
    realm: str,
    pairing: Optional[cm.RealmPairing],
    *,
    week_start: datetime.date,
    week_ending: datetime.date,
    category_map: cm.CategoryMap,
    query_accounts: Callable[[str], list[dict]],
    ledger_rows: Callable[[str, list[str], str, str], dict],
    recompute: Callable[[str, set[str], str, str], dict],
    freshness: Callable[[str, set[str]], dict],
) -> dict[str, Any]:
    """One realm's block for one window. Never raises.

    A realm that cannot be read records `status="error"` and NO figures, so one
    dead realm can neither blank the other eight nor contribute a zero that reads
    like a quiet week.
    """
    block: dict[str, Any] = {
        "status": "ok",
        "basis": FLOW_BASIS,
        "tab": safe_label(pairing.tab) if (pairing and pairing.tab) else None,
        "map_confirmed": bool(pairing and pairing.usable_for_accuracy),
        "map_confidence": safe_label(pairing.confidence) if pairing else "",
    }

    if pairing is None:
        block.update({
            "status": "refused",
            "reason_code": "realm_not_in_entity_map",
            "usable_for_comparison": False,
        })
        return block

    if not pairing.resolvable:
        # The LEX case: one QBO realm against five Lex tabs. Attributing its
        # activity to whichever tab we guessed would put LBHS/LTS/LLA money on
        # LLC. Refuse, and do not even COLLECT -- an unattributable realm's
        # figures have no home in this file (D-124: exclude at collection).
        block.update({
            "status": "refused",
            "reason_code": "realm_scope_undeclared",
            "candidate_tabs": [safe_label(t) for t in pairing.candidate_tabs],
            "usable_for_comparison": False,
        })
        return block

    try:
        accounts = query_accounts(realm)
        bank = {str(a["id"]) for a in accounts
                if a.get("type") == "Bank" and a.get("id")}
        if not bank:
            block.update({
                "status": "unknown",
                "reason_code": "no_bank_accounts",
                "usable_for_comparison": False,
            })
            return block

        gl = ledger_rows(realm, sorted(bank), week_start.isoformat(),
                         week_ending.isoformat())
        rows = gl.get("rows") or []
        net = round(sum(r["amount"] for r in rows if r.get("amount") is not None), 2)
        gross = split_gross(rows)

        check = recompute(realm, bank, week_start.isoformat(), week_ending.isoformat())

        # Freshness is guarded SEPARATELY from the flow read: they are independent
        # facts, and a freshness failure must not discard figures already read
        # (the A5 build_realm lesson).
        try:
            fresh = freshness(realm, bank)
        except Exception as exc:  # noqa: BLE001
            log.warning("cashflow_actuals: freshness failed for %s: %s", realm, exc)
            fresh = {"date": None, "errors": {"freshness": str(exc)[:200]}}

        opening = gl.get("opening_balance")
        closing = None if opening is None else round(opening + net, 2)

        block.update({
            "bank_accounts": len(bank),
            "rows": len(rows),
            "net_flow": net,
            "opening_bank_balance": opening,
            "closing_bank_balance": closing,
            **gross,
            "posted_through": fresh.get("date"),
            "duplicate_row_keys": gl.get("duplicate_row_keys", 0),
            "identity": gl.get("identity") or {},
            **classify_rows(rows, realm, category_map),
        })

        # THE TIE-OUT. Two independent computations of one week; disagreement
        # means one of them is wrong and we do not know which, so the window is
        # banked but excluded from comparison rather than published as a figure.
        residual = (None if check.get("net") is None
                    else round(net - check["net"], 2))
        tie_out: dict[str, Any] = {
            "recompute_net": check.get("net"),
            "residual": residual,
            "empty_types": check.get("empty_types") or [],
            "unexpected_keys": check.get("unexpected_keys") or {},
            "capped_types": check.get("capped_types") or [],
        }
        if residual is None:
            tie_out["status"] = "unavailable"
        elif abs(residual) <= TIE_OUT_TOLERANCE:
            tie_out["status"] = "ok"
        else:
            tie_out["status"] = "failed"
        block["tie_out"] = tie_out

        # An empty QueryResponse is NOT evidence of a quiet week (cq-db2fd53aa608).
        # ONE type returning nothing over seven days is ordinary -- most realms
        # book no Transfers most weeks. EVERY type returning nothing, with no
        # error raised AND no register line either, is far more likely an API
        # condition: QBO was observed 2026-08-05 serving empty responses for every
        # transaction type while Account queries kept working. Publishing "$0 of
        # activity" there would be a wrong number, not a missing one. The
        # trade-off is stated: a realm that genuinely transacted nothing for a
        # week also lands here, which across 36 observed realm-weeks never
        # happened (the quietest week carried 7 register lines).
        expected_types = check.get("types_expected")
        all_empty = (
            isinstance(expected_types, int)
            and len(tie_out["empty_types"]) == expected_types
            and not check.get("errors")
            and not rows
        )
        if all_empty:
            block.update({
                "status": "unknown",
                "reason_code": "all_transaction_types_empty",
                "net_flow": None,
                "receipts": None,
                "disbursements": None,
                "closing_bank_balance": None,
                "usable_for_comparison": False,
            })
            return block

        if check.get("errors"):
            tie_out["recompute_errors"] = sorted(
                {reason_code(v) for v in check["errors"].values()})

        block["usable_for_comparison"] = (
            tie_out["status"] == "ok"
            and bool(pairing.usable_for_accuracy)
            and not tie_out["unexpected_keys"]
            and not tie_out["capped_types"]
            # A broken register identity means the parse lost or duplicated rows;
            # the figure may be arithmetically fine and still describe the wrong
            # set of transactions.
            and not (gl.get("identity") or {}).get("failed")
        )
    except Exception as exc:  # noqa: BLE001 -- per-realm fail-soft is the contract
        log.error("cashflow_actuals: realm %s failed: %s", realm, exc)
        block.update({
            "status": "error",
            "reason_code": reason_code(str(exc)),
            # Deliberately NOT zeroed: a failed realm renders UNKNOWN, never $0.
            "net_flow": None,
            "receipts": None,
            "disbursements": None,
            "opening_bank_balance": None,
            "closing_bank_balance": None,
            "usable_for_comparison": False,
        })
    return block


# ────────────────────────────────────────────────────────────────────────────
# Window build
# ────────────────────────────────────────────────────────────────────────────

def build_window(
    realms: list[str],
    *,
    window_kind: str,
    week_ending: datetime.date,
    weekday_name: str,
    entity_map: cm.EntityMap,
    category_map: cm.CategoryMap,
    query_accounts: Callable[[str], list[dict]],
    ledger_rows: Callable[[str, list[str], str, str], dict],
    recompute: Callable[[str, set[str], str, str], dict],
    freshness: Callable[[str, set[str]], dict],
    today: Optional[datetime.date] = None,
    full_scope: Optional[list[str]] = None,
    week_source: str = "",
) -> dict[str, Any]:
    """Assemble one window's payload. Coverage rides the file as STRUCTURE (D-117)."""
    if window_kind not in (WINDOW_PRELIMINARY, WINDOW_FINALIZED):
        raise ActualsError(f"unknown window kind: {window_kind!r}")

    today = today or datetime.date.today()
    week_start = week_ending - datetime.timedelta(days=6)
    scope = list(full_scope if full_scope is not None else realms)

    blocks: dict[str, dict] = {}
    for realm in realms:
        if entity_map.is_excluded(realm):
            # Excluded at COLLECTION: HR LLC is personal books and the OSN shell
            # is cash-less. Never read, so nothing to leak downstream.
            log.warning("refusing to collect excluded realm: %s", realm)
            continue
        blocks[realm] = build_realm(
            realm, entity_map.pairing(realm),
            week_start=week_start, week_ending=week_ending,
            category_map=category_map,
            query_accounts=query_accounts, ledger_rows=ledger_rows,
            recompute=recompute, freshness=freshness,
        )

    covered = sum(1 for b in blocks.values() if b.get("status") == "ok")
    expected = len([r for r in scope if not entity_map.is_excluded(r)])

    # COVERAGE FLOOR. A window with no readable realm is not a window: writing
    # one lets a monitor that sees a dated file report green on a total failure
    # (D-127c). Refuse, leaving any previous file and the missed-run WARN intact.
    if covered == 0:
        raise ActualsError(
            f"no realm was readable (0 of {expected}) -- refusing to write an "
            "empty window that would read as a banked week"
        )

    tabs_covered = sorted({b["tab"] for b in blocks.values()
                           if b.get("tab") and b.get("status") == "ok"})

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": datetime.datetime.now(
            datetime.timezone.utc).replace(microsecond=0).isoformat(),
        "run_date": today.isoformat(),
        "window_kind": window_kind,
        "week_ending": week_ending.isoformat(),
        "week_start": week_start.isoformat(),
        "week_ending_weekday": weekday_name,
        "week_source": safe_label(week_source, 120),
        "basis": FLOW_BASIS,
        "cash_perimeter": PERIMETER_NOTE,
        "covered": covered,
        "expected": expected,
        "partial_sweep": sorted(set(scope)) != sorted(set(realms)),
        "supersedes": (actuals_filename(week_ending, WINDOW_PRELIMINARY)
                       if window_kind == WINDOW_FINALIZED else None),
        "realms": blocks,
        "excluded_realms": sorted(entity_map.excluded_realms),
        "derived_tabs": [safe_label(t) for t in entity_map.derived_tabs],
        "manual_entry_tabs": [safe_label(t) for t in entity_map.manual_entry_tabs],
        "tabs_covered": tabs_covered,
        "map_confirmed_pairs": entity_map.confirmed_count(),
        "notes": _window_notes(window_kind),
    }


def _window_notes(window_kind: str) -> list[str]:
    notes = [
        PERIMETER_NOTE,
        "These are BANK-CASH flows on a transaction-date basis. The sheet's "
        "balance rows are Cash/CC (D-120) -- reconcile like-for-like only.",
        "UNKNOWN is never zero. A realm with status error/unknown/refused "
        "contributes no figure and is not counted as covered.",
        "A realm whose entity-map pair is unconfirmed renders UNCONFIRMED and "
        "never feeds comparison or accuracy math (D-118).",
        "tie_out compares this file's General Ledger figure against an "
        "independent rebuild from the query API. status != ok means the week is "
        "banked but not usable for comparison.",
    ]
    if window_kind == WINDOW_PRELIMINARY:
        notes.insert(1, (
            "PRELIMINARY: QBO is structurally incomplete for the week that just "
            "ended -- expect Friday-Sunday activity to be missing. Check "
            "posted_through per realm and verify against the bank portal. Never "
            "use a preliminary window for comparison or accuracy math."
        ))
    else:
        notes.insert(1, (
            "FINALIZED: a re-pull of a matured week. This SUPERSEDES the "
            "preliminary file for the same week; comparison and accuracy "
            "consumers bind here and nowhere else."
        ))
    return notes


# ────────────────────────────────────────────────────────────────────────────
# Category-map discovery
#
# The map cannot be written from a chart of accounts alone: most of a realm's 285
# accounts never appear opposite a bank transaction. Discovery therefore proposes
# only the accounts that ACTUALLY showed up as the counterpart of bank-side
# activity over a lookback window, ordered by how much money moved through them,
# so Justin confirms the rows that matter first.
#
# Every proposal lands `confirmed: false`. A guess Justin rubber-stamps is worse
# than a blank, so the keyword hints below are conservative and a weak match
# proposes NO category at all rather than a plausible one.
# ────────────────────────────────────────────────────────────────────────────

#: (needle, category) in PRIORITY order -- first match wins, so the specific
#: cases sit above the general ones they would otherwise be swallowed by:
#: "leasehold improvement" before "lease"/"rent", "interest income" before
#: "interest", "cost of goods" before "cost".
_CATEGORY_HINTS: tuple[tuple[str, str], ...] = (
    ("leasehold improv", "Leasehold Improvements"),
    ("interest income", "Interest Income"),
    ("payroll", "Payroll and Prof Fees"),
    ("salaries", "Payroll and Prof Fees"),
    ("wages", "Payroll and Prof Fees"),
    ("professional fee", "Payroll and Prof Fees"),
    ("prof fee", "Payroll and Prof Fees"),
    ("accounting", "Payroll and Prof Fees"),
    ("legal", "Payroll and Prof Fees"),
    ("contract labor", "Payroll and Prof Fees"),
    ("advertis", "Advertising and Marketing"),
    ("marketing", "Advertising and Marketing"),
    ("utilit", "Utilities"),
    ("electric", "Utilities"),
    ("water", "Utilities"),
    ("internet", "Utilities"),
    ("telephone", "Utilities"),
    ("cost of goods", "Direct Hard Costs"),
    ("subcontract", "Direct Hard Costs"),
    ("direct cost", "Direct Hard Costs"),
    ("rent", "Rent"),
    ("furniture", "Large Equipment/Furniture acquisitions"),
    ("equipment", "Large Equipment/Furniture acquisitions"),
    ("interest and principal", "Interest and Principal"),
    ("interest expense", "Interest and Principal"),
    ("note payable", "Interest and Principal"),
    ("loan", "Interest and Principal"),
    ("paid-in", "Contributions/Draws"),
    ("distribution", "Contributions/Draws"),
    ("contribution", "Contributions/Draws"),
    ("draw", "Contributions/Draws"),
    ("management fee", "Services"),
)

#: Account TYPE as a fallback signal, used only when no keyword matched. Weaker
#: than a name match on purpose -- it says which SIDE of the sheet a row belongs
#: on, not which row.
_TYPE_HINTS: dict[str, str] = {
    "Income": "Services",
    "Other Income": "Interest Income",
    "Equity": "Contributions/Draws",
    "Fixed Asset": "Large Equipment/Furniture acquisitions",
    "Long Term Liability": "Interest and Principal",
    "Cost of Goods Sold": "Direct Hard Costs",
}


#: Share of a counterpart account's movement that must sit on one side before the
#: direction check will veto a name match. Below this the account genuinely sees
#: both directions and the name is the better signal.
_DIRECTION_DOMINANCE = 0.9

#: Sheet rows that legitimately carry money in BOTH directions, so the direction
#: check must not veto them. `Contributions/Draws` says so in its own name: a
#: contribution is money in, a draw is money out, and the sheet keeps them on one
#: row. Live 2026-08-06 the veto wrongly suppressed two real LEX equity accounts
#: ($72,000 and $18,000) before this exemption existed -- a check that fires on a
#: legitimate case is the same failure class it was built to prevent.
_BIDIRECTIONAL_ROWS: frozenset[str] = frozenset({"Contributions/Draws"})


def suggest_category(
    account: dict,
    valid_rows: set[str],
    *,
    expense_rows: Optional[set[str]] = None,
    inflow: float = 0.0,
    outflow: float = 0.0,
) -> tuple[Optional[str], str]:
    """(category or None, confidence). Never guesses past the hint tables.

    A category the sheet does not carry is never proposed -- the sheet's row
    labels are the contract, and inventing one would put money on a row nobody
    can reconcile against.

    THE DIRECTION CHECK is what stops a plausible-but-backwards mapping. Live
    2026-08-06 discovery proposed HJRP's `Rents Receivable` for the sheet's *Rent*
    row on the strength of the word "rent" -- but that account only ever appears
    opposite DEPOSITS: it is rent COLLECTED, and filing $180,742.98 of income as
    rent expense would have been a large, confident, wrong number. So an account
    whose movement sits overwhelmingly on one side cannot be proposed for a row on
    the other.

    Deliberately NOT done with the account's classification. Accrual bookkeeping
    puts a LIABILITY opposite most real outflows (a bill payment clears A/P,
    payroll clears Accrued Payroll), so an "expense rows must be Expense-
    classified" rule would reject the commonest correct case. The observed
    direction of money is the signal that survives the accrual structure.
    """
    expense_rows = expense_rows or set()
    total = inflow + outflow
    mostly_in = total > 0 and (inflow / total) >= _DIRECTION_DOMINANCE
    mostly_out = total > 0 and (outflow / total) >= _DIRECTION_DOMINANCE

    def _directionally_wrong(category: str) -> bool:
        if category in _BIDIRECTIONAL_ROWS:
            return False
        is_expense_row = category in expense_rows
        return (mostly_in and is_expense_row) or (mostly_out and not is_expense_row)

    haystack = f"{account.get('fqn') or ''} {account.get('name') or ''}".lower()
    for needle, category in _CATEGORY_HINTS:
        if needle in haystack and category in valid_rows:
            if _directionally_wrong(category):
                return None, "direction-conflict"
            return category, "name-match-high"

    by_type = _TYPE_HINTS.get(str(account.get("type") or ""))
    if by_type and by_type in valid_rows and not _directionally_wrong(by_type):
        return by_type, "type-match-medium"
    return None, "unmapped"


def discover_category_candidates(
    realm: str,
    rows: list[dict],
    accounts: list[dict],
    valid_rows: set[str],
    *,
    expense_rows: Optional[set[str]] = None,
) -> tuple[dict[str, dict], dict[str, int]]:
    """(candidates, skipped counts) for one realm, busiest account first.

    ``rows`` are the bank-side register lines from the lookback window; the
    COUNTERPART account is what gets mapped. Two kinds of row are excluded rather
    than proposed:

      * `-Split-` rows -- several counterparts, so no single account to map;
      * rows whose counterpart is itself a BANK or CREDIT-CARD account. Those are
        the perimeter, not a category: a bank-to-bank counterpart is an internal
        move that `split_gross` already keeps out of receipts and disbursements,
        and giving it a spend category would file a sweep between two of our own
        accounts as an expense.

    LEX-prefixed realms get an opaque `placeholder` and NO `qbo_account`: those
    names are confirmed through Harrison by DM, never rendered into a file that
    lands in a shared folder (D-124).
    """
    by_id = {str(a["id"]): a for a in accounts if a.get("id")}
    perimeter = {str(a["id"]) for a in accounts
                 if a.get("type") in ("Bank", "Credit Card") and a.get("id")}
    seen: dict[str, dict] = {}
    skipped = {"multi_line_split": 0, "perimeter_counterpart": 0}

    for row in rows:
        account_id = row.get("split_account_id")
        amount = row.get("amount")
        if amount is None:
            continue
        if not account_id:
            skipped["multi_line_split"] += 1
            continue
        if str(account_id) in perimeter:
            skipped["perimeter_counterpart"] += 1
            continue
        bucket = seen.setdefault(str(account_id),
                                {"rows": 0, "amount": 0.0, "in": 0.0, "out": 0.0})
        bucket["rows"] += 1
        bucket["amount"] = round(bucket["amount"] + abs(amount), 2)
        bucket["in" if amount > 0 else "out"] += abs(amount)

    opaque = cm.realm_names_are_opaque(realm)
    out: dict[str, dict] = {}
    for account_id, stats in sorted(
        seen.items(), key=lambda kv: (-kv[1]["amount"], kv[0])
    ):
        account = by_id.get(account_id) or {}
        category, confidence = suggest_category(
            account, valid_rows, expense_rows=expense_rows,
            inflow=stats["in"], outflow=stats["out"])
        entry: dict[str, Any] = {
            "account_type": safe_label(account.get("type")),
            "category": category,
            "confidence": confidence,
            "confirmed": False,
            "observed_rows": stats["rows"],
            "observed_abs_amount": stats["amount"],
            "observed_inflow": round(stats["in"], 2),
            "observed_outflow": round(stats["out"], 2),
        }
        if opaque:
            entry["placeholder"] = f"{realm} acct {account_id}"
        else:
            entry["qbo_account"] = safe_label(account.get("fqn")
                                             or account.get("name"), 120)
        out[account_id] = entry
    return out, skipped


def merge_category_candidates(
    existing: dict,
    discovered: dict[str, dict[str, dict]],
) -> tuple[dict, dict[str, int]]:
    """Fold proposals into the map file's parsed body. Returns (merged, counts).

    A row Justin has CONFIRMED is never touched -- not its category, not its
    confidence, not its name. Discovery re-runs weekly and a confirm that a later
    run could silently revert is not a confirm. Everything else is refreshed so
    the observed counts stay current.
    """
    merged = dict(existing or {})
    realms = dict(merged.get("realms") or {})
    counts = {"added": 0, "refreshed": 0, "kept_confirmed": 0}

    for realm, candidates in discovered.items():
        realm_block = dict(realms.get(realm) or {})
        accounts = dict(realm_block.get("accounts") or {})
        for account_id, entry in candidates.items():
            current = accounts.get(account_id)
            if isinstance(current, dict) and current.get("confirmed"):
                counts["kept_confirmed"] += 1
                continue
            counts["refreshed" if current else "added"] += 1
            accounts[account_id] = entry
        realm_block["accounts"] = accounts
        realms[realm] = realm_block

    merged["realms"] = realms
    return merged, counts


# ────────────────────────────────────────────────────────────────────────────
# Advisory cross-checks -- labelled references, never pass/fail
# ────────────────────────────────────────────────────────────────────────────

def annotate_advisory(
    payload: dict,
    *,
    register_snapshot: Optional[dict] = None,
    prior_finalized: Optional[dict] = None,
) -> dict:
    """Attach the two cross-checks that are REFERENCES, not verdicts.

    Both exist because the design asks for a reconciliation anchor, and both are
    deliberately toothless (D-120, finance F10): they compare figures that are
    allowed to differ, so flagging on them would train the reader to ignore the
    one signal that marks a real gap.

    register_reference -- the A5 daily bank snapshot's register balance. That is
        a DIFFERENT MEASURE at a DIFFERENT INSTANT: `Account.CurrentBalance` as of
        whenever the snapshot ran, against a General Ledger closing balance as of
        the window end. Verified live that register and report figures disagree
        materially on the same account at the same moment, so this renders as a
        labelled reference and `comparable` is true only when the snapshot's own
        as-of date IS the window end. There is no register SERIES to match
        against: A5 keeps one overwritten `qbo-bank-latest.json`, so the matched
        as-of pairs the design imagined do not exist yet -- said plainly here
        rather than faked with the nearest available number.

    chain_check -- the prior FINALIZED window's closing balance against this
        window's opening balance. Same measure, same source, so a non-zero
        residual is real information: it means activity was booked INTO a week
        that had already been finalised (a back-date or a hand edit). Still
        advisory: the correct response is a human look at the books, not a
        withheld figure.
    """
    finalized = payload.get("window_kind") == WINDOW_FINALIZED

    for realm, block in (payload.get("realms") or {}).items():
        if block.get("status") != "ok":
            continue

        if finalized and register_snapshot:
            snap_realm = ((register_snapshot.get("realms") or {}).get(realm) or {})
            if snap_realm.get("status") == "ok":
                as_of = str(snap_realm.get("as_of_utc") or "")[:10]
                block["register_reference"] = {
                    "bank_total": snap_realm.get("bank_total"),
                    "as_of": as_of or None,
                    "basis": register_snapshot.get("basis"),
                    "comparable": bool(as_of) and as_of == payload.get("week_ending"),
                    "note": (
                        "Account-register balance, a DIFFERENT measure at a "
                        "different instant from this window's ledger closing "
                        "balance. A gap is not a reconciliation break (D-120)."
                    ),
                }

        if finalized and prior_finalized:
            prior_block = ((prior_finalized.get("realms") or {}).get(realm) or {})
            prior_close = prior_block.get("closing_bank_balance")
            opening = block.get("opening_bank_balance")
            if prior_close is not None and opening is not None:
                block["chain_check"] = {
                    "prior_week_ending": prior_finalized.get("week_ending"),
                    "prior_closing_balance": prior_close,
                    "residual": round(opening - prior_close, 2),
                    "note": (
                        "Same measure, same source: a non-zero residual means "
                        "activity was booked into a week already finalised. "
                        "Advisory -- a human look at the books, not a withheld "
                        "figure."
                    ),
                }
    return payload


# ────────────────────────────────────────────────────────────────────────────
# Persist
# ────────────────────────────────────────────────────────────────────────────

def write_window(
    payload: dict,
    *,
    overwrite: bool = False,
    today: Optional[datetime.date] = None,
) -> Path:
    """Write one window atomically to the local store.

    Two re-runs are REFUSED because they destroy information rather than refresh
    it, and a documented non-zero exit is exactly what invites them (D-127d):

      * a PRELIMINARY payload over an existing FINALIZED file for the same week
        -- that downgrades a matured figure to a structurally incomplete one;
      * a PARTIAL sweep over a full file -- the realms it never asked about would
        read as missing to every consumer.

    Re-pulling the same KIND of window is allowed and is not destructive: the
    whole design rests on QBO being re-readable, which is what makes the
    finalized re-pull possible in the first place.
    """
    week_raw = payload.get("week_ending")
    kind = payload.get("window_kind")
    if not week_raw or kind not in (WINDOW_PRELIMINARY, WINDOW_FINALIZED):
        raise ActualsError("payload has no week_ending / window_kind")
    week_ending = datetime.date.fromisoformat(week_raw)

    # A future-dated window (typo'd --week, clock skew) would become the store's
    # max week and blind the missed-run check for months (D-127c).
    if week_ending > (today or datetime.date.today()):
        raise ActualsError(f"refusing to write a future-dated window: {week_ending}")

    if kind == WINDOW_PRELIMINARY and not overwrite:
        final = actuals_path(week_ending, WINDOW_FINALIZED)
        if final.exists():
            raise ActualsError(
                f"{final.name} already exists -- a preliminary pull would "
                "replace a matured week with a structurally incomplete one. "
                "Pass --overwrite only if you mean it."
            )

    path = actuals_path(week_ending, kind)
    if payload.get("partial_sweep") and path.exists() and not overwrite:
        raise ActualsError(
            f"{path.name} exists and this is a PARTIAL sweep -- the realms it "
            "never read would look missing to every consumer. Re-run without "
            "--realms, or pass --overwrite."
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    # Process-unique tmp: a manual run overlapping the scheduled one would
    # otherwise race on one fixed path and land a half-written payload.
    tmp = path.with_suffix(f".json.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def same_ignoring_stamps(left: str, right: str) -> bool:
    """Compare two payloads ignoring per-run timestamps, so an unchanged window
    does not churn the Drive mount."""
    def _strip(text: str) -> Any:
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text
        if isinstance(data, dict):
            data.pop("generated_at_utc", None)
            data.pop("run_date", None)
        return json.dumps(data, sort_keys=True)

    return _strip(left) == _strip(right)
