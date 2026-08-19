"""Step 7.5 queue reconciliation for the 8/19 reliability-rails / read-lanes bundle.

Transitions ONLY the seeds this branch actually closed. Dry-run by default;
`--apply` performs the writes through
``code_queue.process_queue_action(ACTION_MARK_SHIPPED, cq_id, HARRISON_ID)`` --
never by hand-editing the jsonl or the backlog (loop step 7.5).

Without this the queue reads stale-positive and the Monday menu re-offers shipped
work (the 2026-07-31 incident: 14 D-095 seeds still PROPOSED after merge). The
inverse failure is quieter and just as bad: marking a seed SHIPPED because *some*
of it landed. Three of the items below are deliberately left open, and one is
marked shipped on an OVERTURNED premise rather than on the work it asked for --
noted per-id so the record says what actually happened.

Evidence of record:
  00-Founder/projects/review-org-remodel-alignment/
      2026-08-18_fndr_ops-finance-session-review-and-corrections.md  (watchdog addendum)
      2026-08-18_fndr_decisions-lane-delivery-audit.md
  _shared/projects/cora/_notes/2026-08-19_fndr_cora-code-prompt-reliability-rails-read-lanes.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO_ROOT / ".env", override=True)

from cora import code_queue  # noqa: E402

HARRISON_ID = "U0B2RM2JYJ1"

SHIPPED: dict[str, str] = {
    "cq-7915a8647cff": (
        "watchdog/heartbeat rails -- instance ledger, write-failure alarm, hourly "
        "tick, restart verification, health-check liveness (slice 1). NOTE: two of "
        "the three forensic premises were overturned -- all four historical "
        "restarts DID restart; the log-naming trap hid the evidence"),
    "cq-0d163e5f9c22": (
        "restart-cora.ps1 process-shape counter fixed -- the live service is "
        "`-m cora.main`, so there is no cora.exe and the old check printed 0+0 "
        "and warned on every restart (slice 1)"),
    "cq-86c283d95a34": (
        "F3E ecom source-opacity -- the red test's own token list was stale "
        "(\"tiktok\" is a sales CHANNEL here); exception made explicit, scrubber "
        "vocabulary GREW, and the two red tests are green (slice 2)"),
    "cq-e63feff3a0bf": (
        "Fireflies diarization-collapse canary -- ingest detection + chunk "
        "tagging + retrieval label + weekly digest section, plus a staged retro "
        "sweep. Measured 126 of 735 stored meetings collapsed (slice 3)"),
    "cq-6fbaf37b1ee7": (
        "knowledge-check memory of its own asks -- recall window + late-answer "
        "re-anchored confirm card + runtime recall note (slice 4a). The seed's "
        "title/summary are WITHHELD by construction so the presumed match could "
        "not be confirmed from the record; built from the tracker evidence as the "
        "kickoff instructed, and the live event log corroborates the 8/14 shape"),
    "cq-ab0a8e753f19": (
        "knowledge_check --force now bypasses the item cooldown its help text "
        "always claimed, at both selection call sites (slice 4b)"),
    "cq-fe9ec84a5ca2": (
        "F3E Production Pipeline read lane -- D-077 registry entry + GET-only "
        "reader + channel gate, price-free by construction on two rails (slice 6). "
        "The D-051 review found BOTH rails leaky and the column projection written "
        "from documentation rather than probed -- every read 422'd into a "
        "fetch-ALL and lot/COA/contacts/run-date rendered empty; fixed against the "
        "live schema. NO PAT grant needed after all: the read-only key already "
        "returns HTTP 200 on that base"),
    "cq-232fe6a541ff": (
        "decisions lane -- gate-date escalation at ANY severity keyed on DELIVERY, "
        "one lane parser, delivery evidence, propose-only Airtable transcription "
        "(slice 8). The audit half was already done 8/18; this is the mechanism it "
        "specified. The D-051 review caught the intake DEAD on this host (the "
        "write-PAT env var is unset, and the read PAT reads that base fine) and "
        "the alarm unable to ever clear -- both fixed; the live sync now proposes "
        "all SIX open tracker decisions with their gate dates"),
    "cq-1b6554a58fae": (
        "inventory Reason-line guards -- the entity-guard half was REAL (prose "
        "writes were never recognized, so \"sent to the OSN pop-up\" routed the "
        "write) and is fixed; the HR half was NOT a defect (the smoke ran as "
        "Harrison, who is unrestricted on HR -- Alex, who is blocked, is correctly "
        "refused on that exact text). Both directions now pinned"),
    "cq-d5945e401fca": (
        "QBO BalanceSheet needs the start_date/end_date PAIR -- a lone end_date "
        "was ignored exactly like as_of_date. Measured live per variant; June "
        "dry-run now lists all 10 _bs files with no period-mismatch skips (7a)"),
    "cq-69ffc4b44bf6": (
        "PREMISE OVERTURNED, closed with evidence rather than the requested "
        "retag/purge: neither named chunk is mis-tagged (one is Harrison's own "
        "Fireflies tracker, the other is this session's kickoff note), and the "
        "population is 2,384 of 370,193 non-LEX chunks (0.64%), most of them "
        "correctly withheld. Shipped a read-only census script instead; narrowing "
        "a PHI backstop is its own decision"),
}

LEFT_OPEN: dict[str, str] = {
    "cq-7fa883cb2220": (
        "LEX maintenance-Airtable read lane (APPROVED HIGH) -- BLOCKED ON INPUT. "
        "The base id is recorded nowhere in the repo or the Founder OS docs (a "
        "portfolio-wide grep finds five Airtable bases and none is a maintenance "
        "base), and this session had no connector budget to discover the schema. "
        "Needs: base id + table names + the status/requester/vendor field names + "
        "whether the read PAT can reach it. Building it blind would have shipped a "
        "tool that returns nothing"),
    "cq-25db72b0a5cb": (
        "LEX build ask, details withheld at capture -- no recoverable content, so "
        "nothing was built. Needs Harrison to restate the ask"),
    "cq-0337b00c0966": (
        "per-meeting-triggered Fireflies sweep -- stretch only if trivially cheap; "
        "it is not (a new trigger surface), and the canary it was attached to now "
        "ships independently"),
    "cq-7ebfc2f9c014": "Tommy pricing pair, f3e.md routing section -- stretch, not started",
    "cq-59939661c455": "sales-deck fix-or-retire -- stretch, not started",
    "cq-7ea3aebff7e7": (
        "link-retrieval capability -- stretch; it was to fold into the read-lane "
        "work, and only one of the two read lanes shipped"),
    "cq-a0a37b5b6de4": "BDM approved-folder rail -- stretch, verify-first not started",
    "cq-6b014816819c": (
        "knowledge-queue mechanical/judgment split -- explicitly next-bundle "
        "unless capacity allowed; it did not. Hannah's mechanical-surface approver "
        "grant still waits on this"),
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Perform the transitions. Omitted = report only.")
    args = ap.parse_args(argv)

    print(f"Step 7.5 -- reliability rails / read lanes "
          f"({'APPLY' if args.apply else 'DRY-RUN'})\n")

    rc = 0
    for cq_id, label in SHIPPED.items():
        if not args.apply:
            print(f"  [dry-run] would mark SHIPPED  {cq_id}  {label}")
            continue
        try:
            result = code_queue.process_queue_action(
                code_queue.ACTION_MARK_SHIPPED, cq_id, HARRISON_ID)
            print(f"  SHIPPED  {cq_id}  -> {result}")
        except Exception as exc:  # noqa: BLE001 -- one bad id must not abort the rest
            print(f"  FAILED   {cq_id}  -> {type(exc).__name__}: {exc}")
            rc = 1

    print("\n  Left OPEN on purpose (do NOT mark these shipped):")
    for cq_id, why in LEFT_OPEN.items():
        print(f"    {cq_id}  {why}")

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
