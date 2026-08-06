"""Weekly 13-week cashflow QBO ACTUALS (13WCF shadow ledger S2).

Read-only. Pulls each provisioned QBO realm's BANK-CASH activity for two windows
and banks them under `data/state/cashflow-ledger/actuals/`, mirrored one-way into
the Founder-OS accounting tree.

    PRELIMINARY  the week that just ended. QBO is structurally INCOMPLETE for it
                 at this hour -- the bank feed has not downloaded Friday through
                 Sunday. Stamped `posted-through` per realm and NEVER used for
                 comparison or forecast-accuracy math.
    FINALIZED    a re-pull of the week before that, now matured in QBO. It
                 supersedes its own preliminary file. Comparison and accuracy
                 consumers bind here and nowhere else.

Scheduled Mon 06:25 AZ as `cowork-cora-cashflow-actuals`
(deployment/setup-cashflow-actuals-task.ps1) -- 10 minutes after the S1 forecast
snapshot, whose banked week grid tells this job what "week ending" means.

THE CASH PERIMETER: a cash event is a transaction touching a BANK account. A card
purchase is not one; the bank-to-card PAYMENT is. Card spend is therefore
excluded by construction, and the map loader refuses to point a credit-card
liability account at an expense category.

Nothing here writes to the sheet, ever (A5 lock). HR LLC (personal books) and the
cash-less OSN shell are excluded at COLLECTION by the map loader, not merely
un-rendered -- the mirror lands in a folder Justin and Hayden work in.

Usage:
    python scripts/run_cashflow_actuals.py --dry-run
    python scripts/run_cashflow_actuals.py
    python scripts/run_cashflow_actuals.py --window final --dry-run
    python scripts/run_cashflow_actuals.py --realms F3E,BDM --dry-run

`--dry-run` writes nothing, but is NOT side-effect-free: it makes live read-only
QBO calls, and authenticating a realm whose ACCESS token has expired rotates that
token through the normal OAuth refresh. True of any QBO read.

Exit codes: 0 = every realm read cleanly; 1 = at least one realm errored or a
window is not usable for comparison (the windows are still written, those realms
marked UNKNOWN); 2 = total failure or a refused write, previous files left in
place.
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_REPO_ROOT / ".env", override=True)
sys.path.insert(0, str(_REPO_ROOT / "src"))

# D-119: Windows consoles default to cp1252, and QBO memo text carries characters
# outside it. --dry-run is the only pre-flight gate before this feeds a finance
# surface, so it must never be the thing that breaks.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):  # pragma: no cover
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            _REPO_ROOT / "logs"
            / f"cashflow-actuals-{datetime.datetime.now().strftime('%Y-%m-%d')}.log",
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger("cashflow-actuals")

from cora import cashflow_actuals as ca  # noqa: E402
from cora import cashflow_ledger as cl  # noqa: E402
from cora import cashflow_maps as cm  # noqa: E402
from cora import drive_io  # noqa: E402
from cora import qbo_bank_snapshot as qbs  # noqa: E402


def _fmt(value: float | None) -> str:
    """ASCII money; UNKNOWN is never 0 (D-117). Sign outside the symbol."""
    if value is None:
        return "UNKNOWN"
    if abs(value) < 0.5:
        return "$0"
    return f"-${abs(value):,.0f}" if value < 0 else f"${value:,.0f}"


def render_dry_run(payload: dict) -> str:
    """Plain-ASCII summary for Harrison's pre-flight review."""
    out: list[str] = []
    kind = str(payload.get("window_kind", "")).upper()
    out.append(f"13WCF QBO ACTUALS -- {kind} (dry run -- nothing written)")
    out.append(f"  week          : {payload.get('week_start')} .. "
               f"{payload.get('week_ending')} "
               f"(ends {payload.get('week_ending_weekday')})")
    out.append(f"  week grid from: {payload.get('week_source')}")
    out.append(f"  basis         : {payload.get('basis')}")
    awaiting = payload.get("awaiting_map_confirmation") or []
    out.append(f"  coverage      : {payload.get('covered')} of "
               f"{payload.get('expected')} realms"
               + (f" (+{len(awaiting)} awaiting map confirmation: "
                  f"{', '.join(awaiting)} -- an expected gap, not a failure)"
                  if awaiting else ""))
    out.append(f"  entity map    : {payload.get('map_confirmed_pairs')} confirmed pair(s)")
    if payload.get("supersedes"):
        out.append(f"  supersedes    : {payload['supersedes']}")
    out.append("")
    out.append(f"  {'realm':7s} {'net flow':>13s} {'receipts':>13s} {'disburse':>13s}"
               f" {'closing':>13s} {'posted':>11s} {'tie':>5s}  status")

    for realm in sorted(payload.get("realms") or {}):
        block = payload["realms"][realm]
        tie = (block.get("tie_out") or {}).get("status", "-")
        status = str(block.get("status"))
        if block.get("status") == "ok" and not block.get("map_confirmed"):
            status += " UNCONFIRMED"
        if block.get("reason_code"):
            status += f" [{block['reason_code']}]"
        out.append(
            f"  {realm:7s} {_fmt(block.get('net_flow')):>13s} "
            f"{_fmt(block.get('receipts')):>13s} "
            f"{_fmt(block.get('disbursements')):>13s} "
            f"{_fmt(block.get('closing_bank_balance')):>13s} "
            f"{str(block.get('posted_through') or '-'):>11s} "
            f"{tie[:5]:>5s}  {status}")
        if block.get("internal_transfers_excluded"):
            out.append(f"          internal bank-to-bank moves excluded from "
                       f"receipts/disbursements: "
                       f"{_fmt(block['internal_transfers_excluded'])}")
        unplaced = block.get("uncategorized") or {}
        if unplaced.get("rows"):
            out.append(f"          uncategorised: {_fmt(unplaced.get('amount'))} "
                       f"over {unplaced['rows']} row(s) -- {unplaced.get('reasons')}")
        if block.get("categories"):
            named = ", ".join(f"{k} {_fmt(v)}"
                              for k, v in sorted(block["categories"].items()))
            out.append(f"          categorised: {named}")
        balances = block.get("balances") or {}
        if balances and not balances.get("complete"):
            out.append(f"          bank balances WITHHELD -- the ledger rendered "
                       f"{balances.get('accounts_with_opening')} of "
                       f"{balances.get('bank_accounts')} account(s); a partial sum "
                       "is not this realm's balance")
        chain = block.get("chain_check") or {}
        if chain.get("status") == "not_adjacent":
            out.append(f"          chain vs {chain.get('prior_week_ending')}: "
                       f"NOT COMPARED, {chain.get('gap_weeks')} week(s) apart -- "
                       "backfill the gap with --week")
        elif chain.get("residual"):
            out.append(f"          chain vs {chain['prior_week_ending']}: "
                       f"{_fmt(chain['residual'])} booked into a finalised week "
                       "(advisory)")
        tie_block = block.get("tie_out") or {}
        if tie_block.get("status") == "failed":
            out.append(f"          TIE-OUT FAILED residual "
                       f"{_fmt(tie_block.get('residual'))} -- banked but NOT "
                       "usable for comparison")
        if tie_block.get("status") == "unavailable":
            out.append("          TIE-OUT DID NOT RUN -- the independent check was "
                       "unavailable, so this figure is UNVERIFIED and excluded "
                       "from comparison")
        if tie_block.get("unexpected_keys"):
            out.append(f"          UNEXPECTED QUERY KEY(S): "
                       f"{tie_block['unexpected_keys']} -- a QBO response key "
                       "changed; treat this realm as UNKNOWN")
        if tie_block.get("capped_types"):
            out.append(f"          PAGE CAP HIT on {tie_block['capped_types']} -- "
                       "the window may be incomplete")

    refused = sorted(r for r, b in (payload.get("realms") or {}).items()
                     if b.get("status") == "refused")
    if refused:
        out.append("")
        out.append(f"  REFUSED ({len(refused)}) -- not collected, not counted as covered:")
        for realm in refused:
            block = payload["realms"][realm]
            detail = ""
            if block.get("candidate_tabs"):
                detail = (f" -- could be any of {', '.join(block['candidate_tabs'])}; "
                          "needs scope_attested or filters")
            out.append(f"    {realm}: {block.get('reason_code')}{detail}")

    out.append("")
    if payload.get("manual_entry_tabs"):
        out.append("  manual-entry tabs (no QBO source, not a gap): "
                   + ", ".join(payload["manual_entry_tabs"]))
    if payload.get("derived_tabs"):
        out.append("  derived roll-ups (nothing to map): "
                   + ", ".join(payload["derived_tabs"]))
    out.append("  never collected (excluded realms): "
               + ", ".join(payload.get("excluded_realms") or []))
    out.append("")
    for note in payload.get("notes") or []:
        out.append(f"  NOTE: {note}")
    return "\n".join(out)


def _resolve_realms(args_realms: str, entity_map: cm.EntityMap) -> tuple[list[str], list[str]]:
    """(realms to sweep, full scope). Raises SystemExit(2) on an out-of-scope name."""
    from cora.connectors.qbo_oauth import list_provisioned_entities  # noqa: PLC0415

    full_scope = [r for r in sorted(list_provisioned_entities())
                  if not entity_map.is_excluded(r)]
    if not args_realms.strip():
        return list(full_scope), full_scope

    # Case-insensitive ALLOWLIST against sweepable scope, never a denylist against
    # a name someone else normalises (D-127f -- an exact-string exclusion of the
    # personal-books tab was defeated by different casing in M1).
    by_fold = {r.casefold(): r for r in full_scope}
    requested = [r.strip() for r in args_realms.split(",") if r.strip()]
    realms: list[str] = []
    rejected: list[str] = []
    for name in requested:
        canonical = by_fold.get(name.casefold())
        if canonical is None:
            rejected.append(name)
        elif canonical not in realms:
            realms.append(canonical)
    if rejected:
        log.error("not in sweepable scope, refusing: %s", ", ".join(sorted(rejected)))
        raise SystemExit(2)
    return realms, full_scope


def _mirror(payload: dict) -> None:
    """One-way Drive mirror. Fail-soft and change-gated: a Drive blip must never
    kill the local write, and an unchanged payload must not churn the mount.

    The full payload is mirrored. There is no name-leak class left to strip: the
    General Ledger parse never captures memo/name/description, and account
    identifiers in the payload are numeric ids, not names (D-124).
    """
    week_ending = datetime.date.fromisoformat(payload["week_ending"])
    target = ca.mirror_actuals_path(week_ending, payload["window_kind"])
    body = json.dumps(payload, indent=2, sort_keys=True)
    try:
        existing = drive_io.read_text(target) if drive_io.exists(target) else None
        if existing is not None and ca.same_ignoring_stamps(existing, body):
            log.info("Drive mirror unchanged -- skipping write")
            return
        drive_io.write_text_atomic(target, body)
        log.info("mirrored to %s", target)
    except drive_io.DriveUnavailable as exc:
        log.warning("Drive mirror skipped (mount unavailable): %s", exc)
    except OSError as exc:
        log.warning("Drive mirror failed: %s", exc)


#: How far back the mirror reconcile looks. Bounded on purpose -- this is a
#: self-heal for a mount blip, not a full-history sync.
_MIRROR_BACKFILL_WEEKS = 6


def _mirror_backfill(today: datetime.date) -> None:
    """Mirror any recent local window the shared folder is missing.

    `_mirror` only ever pushes the payload just built, so a Drive mount blip at
    06:25 left that week permanently absent from the folder Justin and Hayden work
    in -- one local warning, no retry, and the health check reads the LOCAL store
    so it never noticed. Fail-soft throughout: a mirror that cannot be reconciled
    must not fail the run that already banked its data.
    """
    for weeks_back in range(1, _MIRROR_BACKFILL_WEEKS + 1):
        week = today - datetime.timedelta(days=7 * weeks_back)
        for kind in (ca.WINDOW_FINALIZED, ca.WINDOW_PRELIMINARY):
            local = ca.actuals_path(week, kind)
            if not local.exists():
                continue
            target = ca.mirror_actuals_path(week, kind)
            try:
                if drive_io.exists(target):
                    continue
                drive_io.write_text_atomic(target, local.read_text(encoding="utf-8"))
                log.warning("mirror backfill: %s was missing from Drive -- "
                            "restored", target.name)
            except (drive_io.DriveUnavailable, OSError) as exc:
                log.warning("mirror backfill skipped for %s: %s", target.name, exc)
                return


def _build_one(
    *,
    window_kind: str,
    week_ending: datetime.date,
    weekday_name: str,
    realms: list[str],
    full_scope: list[str],
    entity_map: cm.EntityMap,
    category_map: cm.CategoryMap,
    today: datetime.date,
    week_source: str,
) -> dict | None:
    """Build + annotate one window, or None when it broke structurally."""
    from cora.tools import qbo_client as qc  # noqa: PLC0415

    try:
        payload = ca.build_window(
            realms,
            window_kind=window_kind,
            week_ending=week_ending,
            weekday_name=weekday_name,
            entity_map=entity_map,
            category_map=category_map,
            # The FLOW perimeter, which includes inactive accounts: a realm that
            # sweeps money out of an account and then closes it would otherwise
            # keep only the receiving leg inside the perimeter, and the sweep
            # would read as real income at a $0.00 tie-out residual.
            query_accounts=qc.query_flow_perimeter_accounts,
            ledger_rows=qc.general_ledger_bank_rows,
            recompute=qc.bank_side_flow,
            freshness=qc.newest_bank_side_txn_date,
            today=today,
            full_scope=full_scope,
            week_source=week_source,
        )
    except ca.ActualsError as exc:
        # build_window is per-realm fail-soft, so reaching here means something
        # structural broke. Never overwrite a good window with a broken one.
        log.error("%s window build failed: %s -- previous file left in place",
                  window_kind, exc)
        return None

    prior = None
    if window_kind == ca.WINDOW_FINALIZED:
        earlier = [w for w in ca.list_finalized_weeks() if w < week_ending]
        if earlier:
            prior = ca.load_finalized(earlier[-1])
    return ca.annotate_advisory(
        payload,
        register_snapshot=qbs.load_snapshot(),
        prior_finalized=prior,
    )


#: Where the LEX name-confirm artifact lands. logs/ is LOCAL ONLY -- never
#: mirrored to Drive, never KB-ingested -- because it is the one file in this
#: build that carries LEX account NAMES, which are confirmed through Harrison by
#: DM and never rendered on a shared surface (D-124).
_LEX_CONFIRM_DIR = _REPO_ROOT / "logs"


def _cell(value: object) -> str:
    """One markdown table cell from an externally-authored string (D-123).

    QBO account names are typed by people. A pipe or a control character in one
    silently mangles the table Harrison reads and confirms from -- and this is the
    one surface in the build that renders these names at all, so it is exactly
    where the sanitizer must not be skipped.
    """
    return ca.safe_label(value, 120).replace("|", r"\|")


def _write_lex_confirm_artifact(
    realm: str,
    accounts: list[dict],
    candidates: dict[str, dict],
    today: datetime.date,
) -> Path:
    """Stage the Harrison-DM confirm sheet for an opaque realm.

    Two asks in one file: the account->category rows (which the shared map holds
    only as opaque placeholders), and the BANK-account list -- because those bank
    accounts are the concrete mechanism that could split the one QBO LEX realm
    across the sheet's five Lex tabs via the entity map's `filters`, which is the
    single thing standing between LEX and being readable at all.
    """
    path = _LEX_CONFIRM_DIR / f"cashflow-{realm.lower()}-name-confirm-{today}.md"
    by_id = {str(a["id"]): a for a in accounts if a.get("id")}
    lines = [
        f"# {realm} account confirm -- 13WCF category map ({today})",
        "",
        "LOCAL ONLY. Not mirrored to Drive, not KB-ingested. These names are "
        "confirmed through Harrison by DM (D-124); the shared map carries opaque "
        "placeholders only.",
        "",
        "## Bank accounts -- what the 1:N split would have to key on",
        "",
        f"QBO exposes ONE {realm} realm against five Lex tabs, so the extractor "
        f"REFUSES to compute for it. The ONLY declaration it honours today is a "
        f"single `tab` plus `scope_attested: true` -- an attestation that the "
        f"company file IS exactly that one tab. `filters` is NOT implemented and "
        f"now refuses rather than silently reading the realm whole. If the file "
        f"really covers all five entities, leaving this realm UNKNOWN is the "
        f"correct answer, and these accounts are the evidence for what a future "
        f"per-tab split (`tab_splits`) would need:",
        "",
        "| account id | type | name |",
        "|---|---|---|",
    ]
    for account in sorted(accounts, key=lambda a: str(a.get("fqn") or "")):
        if account.get("type") == "Bank":
            lines.append(f"| {_cell(account['id'])} | {_cell(account['type'])} | "
                         f"{_cell(account.get('fqn') or account.get('name'))} |")
    lines += [
        "",
        "## Counterpart accounts seen opposite bank activity",
        "",
        "| placeholder | account id | name | type | suggested category | rows | $ moved |",
        "|---|---|---|---|---|---|---|",
    ]
    for account_id, entry in candidates.items():
        account = by_id.get(account_id) or {}
        lines.append(
            f"| {_cell(entry.get('placeholder'))} | {_cell(account_id)} | "
            f"{_cell(account.get('fqn') or account.get('name') or '?')} | "
            f"{_cell(entry.get('account_type'))} | "
            f"{_cell(entry.get('category') or '(none)')} | "
            f"{entry.get('observed_rows')} | {entry.get('observed_abs_amount')} |")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _discover(args, entity_map: cm.EntityMap, today: datetime.date) -> int:
    """Populate the category map from what actually moved through the banks."""
    import yaml  # noqa: PLC0415

    from cora.tools import qbo_client as qc  # noqa: PLC0415

    try:
        realms, _scope = _resolve_realms(args.realms, entity_map)
    except SystemExit:
        return 2

    end = today
    start = end - datetime.timedelta(days=max(1, args.lookback_days))
    log.info("discovering category candidates over %s .. %s across %d realm(s)",
             start, end, len(realms))

    raw = cm._read_yaml(cm.CATEGORY_MAP_PATH)
    valid_rows = {row for rows in (raw.get("categories") or {}).values()
                  for row in (rows or [])}
    expense_rows = {str(r) for r in (raw.get("expense_categories") or [])}
    if not valid_rows:
        log.error("the category map carries no category rows to map onto")
        return 2

    discovered: dict[str, dict[str, dict]] = {}
    lex_artifacts: list[Path] = []
    failed: list[str] = []

    for realm in realms:
        pairing = entity_map.pairing(realm)
        if pairing is None:
            # The window path refuses an unmapped realm BEFORE its first API call.
            # Discovery must match that, or it becomes the weaker of the two
            # collection boundaries: a personal or holding entity provisioned
            # under a code `excluded_realms` does not name would have its whole
            # chart of accounts and 90 days of register lines read, and --apply
            # would write its account names into a git-tracked file.
            log.warning("%s has no entity-map pairing -- not reading it "
                        "(add a pairing first)", realm)
            continue
        try:
            accounts = qc.query_all_accounts(realm)
            bank = sorted(str(a["id"]) for a in accounts if a.get("type") == "Bank")
            if not bank:
                log.warning("%s has no bank accounts -- skipping", realm)
                continue
            gl = qc.general_ledger_bank_rows(realm, bank, start.isoformat(),
                                             end.isoformat())
        except Exception as exc:  # noqa: BLE001 -- one dead realm must not lose the rest
            log.error("discovery failed for %s: %s", realm, exc)
            failed.append(realm)
            continue

        candidates, skipped = ca.discover_category_candidates(
            realm, gl.get("rows") or [], accounts, valid_rows,
            expense_rows=expense_rows)
        log.info("%s: %d row(s), %d counterpart account(s); not proposable: "
                 "%d multi-line split, %d perimeter counterpart", realm,
                 gl.get("row_count", 0), len(candidates),
                 skipped["multi_line_split"], skipped["perimeter_counterpart"])
        if candidates:
            discovered[realm] = candidates
        # An unresolvable realm is exactly the one whose confirm artifact matters
        # most -- it cannot be read at all until the split is declared. Gated on
        # --apply: this is the one file in the build carrying LEX account names in
        # plaintext, and "--discover prints, --apply writes" has to be true of it
        # too or the operator's mental model is wrong about precisely that file.
        if cm.realm_names_are_opaque(realm):
            if args.apply:
                lex_artifacts.append(_write_lex_confirm_artifact(
                    realm, accounts, candidates, today))
            else:
                log.info("%s: --apply would write the local-only name-confirm "
                         "artifact for this realm", realm)
        elif pairing and not pairing.resolvable:
            log.warning("%s is unresolvable (%s)", realm, pairing.refusal_reason)

    if not discovered:
        log.error("nothing discovered -- category map left untouched")
        return 2

    merged, counts = ca.merge_category_candidates(raw, discovered)
    log.info("candidates: %d new, %d refreshed, %d confirmed row(s) left "
             "untouched", counts["added"], counts["refreshed"],
             counts["kept_confirmed"])
    for path in lex_artifacts:
        log.info("LOCAL-ONLY confirm artifact (Harrison DM, never mirrored): %s", path)

    if not args.apply:
        print(f"\nDISCOVERY (dry run -- {cm.CATEGORY_MAP_PATH.name} NOT written)")
        for realm in sorted(discovered):
            print(f"\n  {realm}: {len(discovered[realm])} candidate account(s)")
            for account_id, entry in list(discovered[realm].items())[:12]:
                name = entry.get("qbo_account") or entry.get("placeholder")
                print(f"    {account_id:>10s}  {str(entry.get('category') or '(none)'):38s}"
                      f"  {entry.get('confidence'):18s}  ${entry['observed_abs_amount']:>12,.2f}"
                      f"  {name}")
            if len(discovered[realm]) > 12:
                print(f"    ... {len(discovered[realm]) - 12} more")
        print("\n  Re-run with --apply to write them (all confirmed: false).")
        return 1 if failed else 0

    # Preserve the file's comment header: it carries the cash-perimeter rationale
    # and the D-124 rule, and yaml.safe_dump would silently drop all of it.
    original = cm.CATEGORY_MAP_PATH.read_text(encoding="utf-8")
    header = original.split("\ncategories:", 1)[0]
    body = yaml.safe_dump(merged, sort_keys=False, allow_unicode=True,
                          default_flow_style=False, width=100)
    tmp = cm.CATEGORY_MAP_PATH.with_suffix(f".yaml.{os.getpid()}.tmp")
    tmp.write_text(f"{header}\n{body}", encoding="utf-8")
    tmp.replace(cm.CATEGORY_MAP_PATH)
    log.info("wrote %s", cm.CATEGORY_MAP_PATH)

    try:
        cm.load_category_map()
    except cm.MapError as exc:
        # The loader is the contract. If what we just wrote does not load, say so
        # loudly -- a map that fails at load is better than one that silently
        # mis-categorises, but neither should ship unnoticed.
        log.error("the map we just wrote does NOT load: %s", exc)
        return 2
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help=("print the windows; write no file and no Drive mirror. Still makes "
              "live read-only QBO calls, which can rotate an expired access "
              "token as a normal side effect of authenticating"))
    parser.add_argument("--window", choices=("both", "prelim", "final"),
                        default="both",
                        help="which window(s) to pull (default: both)")
    parser.add_argument("--realms", default="",
                        help="comma-separated realm codes (default: all provisioned)")
    parser.add_argument("--no-mirror", action="store_true",
                        help="write locally but skip the Drive mirror")
    parser.add_argument(
        "--week", default="",
        help=("THE RECOVERY LEVER. Pull a specific WEEK-ENDING date (YYYY-MM-DD) "
              "instead of the two windows derived from today -- use it to backfill "
              "a week a missed Monday left with no finalized window. Combine with "
              "--window final. Pass the week-ending date itself; --date is a fake "
              "'today' and is not the way to do this."))
    parser.add_argument("--date", default="",
                        help=("override today (YYYY-MM-DD). Shifts BOTH derived "
                              "windows, so to reach week X you would have to pass "
                              "X+8..X+14 -- use --week instead. Testing only."))
    parser.add_argument(
        "--overwrite", action="store_true",
        help=("allow a write that would otherwise be refused as destructive: "
              "overwriting a full file with a narrowed --realms sweep, or "
              "replacing a window with one that covers FEWER realms"))
    parser.add_argument(
        "--discover", action="store_true",
        help=("propose account->category rows for the category map from what "
              "actually moved through the banks over --lookback-days. Prints by "
              "default; needs --apply to write. Never touches a confirmed row."))
    parser.add_argument("--apply", action="store_true",
                        help="with --discover: write the proposals to the map file")
    parser.add_argument("--lookback-days", type=int, default=90,
                        help="with --discover: how far back to look (default 90)")
    args = parser.parse_args(argv)

    try:
        today = (datetime.date.fromisoformat(args.date.strip())
                 if args.date.strip() else datetime.date.today())
    except ValueError:
        log.error("--date must be YYYY-MM-DD")
        return 2

    try:
        entity_map = cm.load_entity_map()
        category_map = cm.load_category_map()
    except cm.MapError as exc:
        # A bad map must fail LOUDLY at load rather than quietly producing a wrong
        # attribution -- that is the whole reason the loader validates.
        log.error("map unusable: %s", exc)
        return 2

    if args.discover:
        # Discovery needs no week grid: it looks at a lookback window, not at the
        # sheet's week boundaries.
        return _discover(args, entity_map, today)

    try:
        weekday_name, w1, w2 = ca.resolve_windows(today)
    except ca.ActualsError as exc:
        log.error("%s", exc)
        return 2

    # --week names the WEEK-ENDING to pull, so a backfill asks for the week it
    # actually wants. Deriving it from a fake `today` meant asking for week X by
    # passing X+8..X+14, which is documented nowhere and fails confusingly.
    if args.week.strip():
        try:
            target = datetime.date.fromisoformat(args.week.strip())
        except ValueError:
            log.error("--week must be YYYY-MM-DD (the week-ENDING date)")
            return 2
        expected_day = target.strftime("%A")
        if expected_day != weekday_name:
            log.error("--week %s is a %s, but this workbook's weeks end on %s. "
                      "Pass the week-ENDING date.", target, expected_day, weekday_name)
            return 2
        if target >= today:
            log.error("--week %s has not closed yet (today is %s)", target, today)
            return 2
        w1 = w2 = target
        log.info("--week %s: pulling that week only", target)

    # The snapshot that ACTUALLY supplied the weekday, not merely the newest one:
    # week_source is the field a reader consults to answer "where did this week
    # boundary come from?", so naming a snapshot that supplied nothing misdirects.
    _weekday, source_date = ca.week_grid_source()
    week_source = (f"forecast snapshot {source_date.isoformat()}"
                   if source_date else "unknown")
    log.info("week ending %s (from %s); PRELIMINARY W-1=%s, FINALIZED W-2=%s",
             weekday_name, week_source, w1, w2)

    try:
        realms, full_scope = _resolve_realms(args.realms, entity_map)
    except SystemExit:
        return 2
    if not realms:
        log.error("no sweepable QBO realms -- previous files left in place")
        return 2
    log.info("sweeping %d of %d realm(s): %s", len(realms), len(full_scope),
             ", ".join(realms))
    log.info("never collected (excluded_realms): %s",
             ", ".join(sorted(entity_map.excluded_realms)))

    wanted: list[tuple[str, datetime.date]] = []
    if args.window in ("both", "prelim"):
        wanted.append((ca.WINDOW_PRELIMINARY, w1))
    if args.window in ("both", "final"):
        wanted.append((ca.WINDOW_FINALIZED, w2))

    built = 0
    degraded = False
    for window_kind, week_ending in wanted:
        payload = _build_one(
            window_kind=window_kind, week_ending=week_ending,
            weekday_name=weekday_name, realms=realms, full_scope=full_scope,
            entity_map=entity_map, category_map=category_map, today=today,
            week_source=week_source,
        )
        if payload is None:
            degraded = True
            continue

        if args.dry_run:
            print(render_dry_run(payload))
            print()
            built += 1
            # Fall through to the same health checks below. The dry run is
            # documented as the only pre-flight gate before this feeds a finance
            # surface, and the task PS1 hands it to the operator as THE review
            # command -- so it must not exit 0 on a window where realms were
            # unreadable or the tie-out broke. It used to.
            degraded = _report_window_health(payload, window_kind) or degraded
            continue

        try:
            path = ca.write_window(payload, overwrite=args.overwrite, today=today)
        except ca.ActualsError as exc:
            log.error("%s", exc)
            degraded = True
            continue

        built += 1
        log.info("wrote %s (%d/%d realms)", path, payload["covered"],
                 payload["expected"])

        if not args.no_mirror:
            _mirror(payload)
            _mirror_backfill(today)

        degraded = _report_window_health(payload, window_kind) or degraded

    if built == 0:
        return 2
    return 1 if degraded else 0


def _report_window_health(payload: dict, window_kind: str) -> bool:
    """Log what an operator must act on. Returns True if the run is degraded."""
    degraded = False
    realms = payload.get("realms") or {}

    problems = sorted(r for r, b in realms.items()
                      if b.get("status") in ("error", "unknown"))
    if problems:
        log.error("realm(s) UNKNOWN in %s window: %s", window_kind,
                  ", ".join(problems))
        degraded = True

    untied = sorted(r for r, b in realms.items()
                    if (b.get("tie_out") or {}).get("status") == "failed")
    if untied:
        # Loud on purpose: the figure is banked and looks fine, so nothing else in
        # the estate would tell anybody it disagrees with the ledger.
        log.error("TIE-OUT FAILED in %s window for: %s -- banked but excluded "
                  "from comparison", window_kind, ", ".join(untied))
        degraded = True

    # A tie-out that never RAN is not a clean week. One QBO error on any of the
    # nine typed queries makes the recompute unavailable, and the whole premise of
    # this module is that agreement is a check a failure BREAKS -- so a week where
    # the check was switched off has to be as visible as one where it failed.
    # Otherwise exit 0, covered, monitor green, figure published, guard silently off.
    unchecked = sorted(r for r, b in realms.items()
                       if (b.get("tie_out") or {}).get("status") == "unavailable")
    if unchecked:
        log.error("TIE-OUT DID NOT RUN in %s window for: %s -- the independent "
                  "check was unavailable, so these figures are banked UNVERIFIED "
                  "and excluded from comparison. Re-pull when QBO is healthy.",
                  window_kind, ", ".join(unchecked))
        degraded = True

    return degraded


if __name__ == "__main__":
    raise SystemExit(main())
