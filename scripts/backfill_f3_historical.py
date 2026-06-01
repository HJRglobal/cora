#!/usr/bin/env python3
"""One-time backfill of historical F3 Energy sales data into HubSpot.

Source: Google Sheet 1Qpwt29BIk9qCxfGhrsEdoEp3HKz-jCuSqVRjANOduqU
        (Teniel + Mike Martinez sales ledger, mid-2024 through Oct 2025)

What this script creates per account:
  1. Company (deduped by name — skips if already exists)
  2. Deal linked to company (correct stage, owner, custom tags)
  3. Note with transaction history attached to deal

Owner assignments:
  Tommy Anderson  (162944825) — retail accounts, route prospects
  Alex Cordova    (160262948) — brand partnerships, sports bar, distribution

Stages used (F3E Retail pipeline 2313722582):
  Identify  3760235201 — prospects, never bought
  Qualified 3760235204 — active buyers with 2025 transaction history
  Proposal  3760204497 — large open PO / outstanding deals

Custom properties applied to every deal:
  hjr_deal_type     retail_account | distribution | brand_partnership | event_sponsorship
  f3_entity         F3E
  f3_deal_direction revenue_in | spend_out

Run:
    python scripts/backfill_f3_historical.py --dry-run   # preview only
    python scripts/backfill_f3_historical.py             # live import
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from dotenv import load_dotenv
load_dotenv(dotenv_path=_REPO_ROOT / ".env", override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(
            open(sys.stdout.fileno(), "w", encoding="utf-8", errors="replace", closefd=False)
        ),
        logging.FileHandler(_REPO_ROOT / "logs" / "f3-backfill.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("f3-backfill")

# ── Owner / pipeline constants ────────────────────────────────────────────────
TOMMY  = "162944825"
ALEX   = "160262948"
F3E_PIPELINE = "2313722582"
STAGE_IDENTIFY  = "3760235201"
STAGE_QUALIFIED = "3760235204"
STAGE_PROPOSAL  = "3760204497"
STAGE_CLOSED_LOST = "3760235207"

# ── Account data ──────────────────────────────────────────────────────────────
# Format per entry:
# (company_name, address, deal_name, stage, amount, deal_type, owner, notes)

TOMMY_ACTIVE = [
    (
        "Reliant",
        "26090 Ynez Rd Suite B, Temecula, CA 92591",
        "Reliant — F3E Retail Account",
        STAGE_QUALIFIED, 3010.76, "retail_account", TOMMY,
        "Historical account from 2024-2025 ledger. Recurring orders ~78 cases/delivery "
        "(Original + Citrus + Tropical mix). Most recent: Invoice 62037 $1,336.92 (7/22/25), "
        "Invoice 62031 $891.28 (7/7/25). "
        "OPEN INVOICE: Invoice 8042 $445.64 dated 10/1/25 — status Invoiced, not yet confirmed paid. "
        "Tommy to verify and close or collect.",
    ),
    (
        "The Blind Group",
        "5031 East Washington St, Phoenix, AZ 85034",
        "The Blind Group — F3E Retail Account",
        STAGE_QUALIFIED, 2628.00, "retail_account", TOMMY,
        "Historical account. High-volume buyer — 70-76 case orders. "
        "Invoice 3786 $1,368.00 (7/25/25), Invoice 62034 $1,260.00 (7/25/25). Active.",
    ),
    (
        "John Moon Muscle Foods",
        "100 Keystone Industrial Park Rd Unit 1B, Dunmore, PA 18512",
        "John Moon Muscle Foods — F3E Retail Account",
        STAGE_QUALIFIED, 1664.00, "retail_account", TOMMY,
        "Historical account. PA-based. Large single order: Invoice 62032 $1,664.00 (7/10/25). "
        "104 cases — F3 Energy Original + Citrus Clarity mix. Note: American Fitness Wholesalers "
        "shares same address — may be related entities at same facility. Tommy to verify.",
    ),
    (
        "MMA Lab",
        "Phoenix, AZ",
        "MMA Lab — F3E Retail Account (gym shop)",
        STAGE_QUALIFIED, 427.45, "retail_account", TOMMY,
        "Historical account. Recurring gym-channel buyer. "
        "Invoice 1001 $252.00 (8/29/25), Invoice 62041 $175.45 (8/20/25). Active.",
    ),
    (
        "RepFitness",
        "Arizona",
        "RepFitness — F3E Retail Account",
        STAGE_QUALIFIED, 432.00, "retail_account", TOMMY,
        "Historical account. Invoice 3785 $216.00 (9/10/25), Invoice 62030 $216.00 (7/18/25). Active.",
    ),
    (
        "Az Tec",
        "Overgaard, AZ",
        "Az Tec — F3E Retail Account",
        STAGE_QUALIFIED, 519.84, "retail_account", TOMMY,
        "Historical account. AKA 'Aztec' in some ledger rows — same location, deduped. "
        "Invoice 62044 $259.92 (8/29/25 — Paid). "
        "OPEN INVOICE: Invoice 8040 $259.92 dated 10/1/25 — status Invoiced, not yet confirmed paid. "
        "Tommy to verify and collect.",
    ),
    (
        "The Gym TG",
        "1126 S Gilbert Rd, Mesa, AZ 85204",
        "The Gym TG — F3E Retail Account",
        STAGE_QUALIFIED, 550.64, "retail_account", TOMMY,
        "Historical account. Fitness gym channel. "
        "Invoice 3787 $208.00 (9/17/25), Invoice 62039 $256.00 (8/13/25), "
        "Invoice 62036 $86.64 (8/5/25). Active.",
    ),
    (
        "San Tan Ford",
        "Arizona",
        "San Tan Ford — F3E Retail Account",
        STAGE_QUALIFIED, 414.00, "retail_account", TOMMY,
        "Historical account. "
        "Invoice 3788 $216.00 (9/29/25 — Paid), Invoice 62035 $198.00 (7/31/25). Active.",
    ),
    (
        "VS Fight Shop",
        "Arizona",
        "VS Fight Shop — F3E Retail Account",
        STAGE_QUALIFIED, 152.00, "retail_account", TOMMY,
        "OPEN INVOICE: Invoice 3784 $152.00 dated 9/9/25 — status Invoiced, not yet confirmed paid. "
        "8 cases at $19/case. Tommy to verify receipt and collect.",
    ),
    (
        "Shell Gas Gilbert",
        "777 S Gilbert Rd, Gilbert, AZ 85296",
        "Shell Gas Gilbert — F3E Retail Account",
        STAGE_QUALIFIED, 378.00, "retail_account", TOMMY,
        "Historical account. C-store channel. "
        "Invoice 62038 $234.00 (8/12/25), Invoice 3830 $144.00 (6/27/25). Active.",
    ),
    (
        "FunBox",
        "5255 E Brown Rd, Mesa, AZ 85205",
        "FunBox — F3E Retail Account",
        STAGE_QUALIFIED, 140.00, "retail_account", TOMMY,
        "Historical account. Invoice 62033 $140.00 (7/24/25). Active.",
    ),
    (
        "One Stop Nutrition",
        "Arizona (multiple locations)",
        "One Stop Nutrition — F3E Account",
        STAGE_QUALIFIED, 241.80, "retail_account", TOMMY,
        "Historical account. Mixed Energy + Mood order via consignment. "
        "PO 07172025 — Energy (Strawberry Lemonade, Tropical Theory, Citrus, Original) + "
        "Mood (Orange, Peach, Strawberry Cream). Invoice 62027 $241.80 (7/17/25). "
        "Contact email on file: osnseth@gmail.com. Note: OSN is also an HJR portfolio company "
        "— coordinate with Harrison before treating as standard retail account.",
    ),
    (
        "Chevron Scottsdale Thomas",
        "6930 E Thomas Rd, Scottsdale, AZ",
        "Chevron Scottsdale Thomas — F3E Retail Account",
        STAGE_QUALIFIED, 85.00, "retail_account", TOMMY,
        "Historical account. C-store/gas channel. Invoice 62026 $85.00 (7/15/25).",
    ),
    (
        "Circle 7 Mart",
        "415 E McKellips Rd, Mesa, AZ 85203",
        "Circle 7 Mart — F3E Retail Account",
        STAGE_QUALIFIED, 224.00, "retail_account", TOMMY,
        "Historical account. C-store channel. "
        "Invoice 3829 $224.00 (6/27/25). Also appears in Mike Martinez earlier ledger. Active.",
    ),
    (
        "Elsa Market",
        "Arizona",
        "Elsa Market — F3E Retail Account",
        STAGE_QUALIFIED, 51.00, "retail_account", TOMMY,
        "Historical account. C-store channel. Invoice 62025 $51.00 (6/30/25). "
        "Note: appears as 'Elsa's Market', 'Elsa Food Mart', 'Elsa Market' in ledger — deduped to single record.",
    ),
]

TOMMY_OPEN_PO = [
    (
        "American Fitness Wholesalers",
        "100 Keystone Industrial Park Rd, Dunmore, PA 18512",
        "American Fitness Wholesalers — F3E Open PO",
        STAGE_PROPOSAL, 26624.00, "distribution", TOMMY,
        "LARGE OPEN CONSIGNMENT PO: PO 13398 — 1,664 cases at $16.00/case = $26,624.00 total. "
        "Status: TBD — payment not confirmed. "
        "Note: shares address with John Moon Muscle Foods — may be related entities at same facility. "
        "Tommy to contact and confirm whether PO is fulfilled, outstanding, or cancelled. "
        "Deal left open for Tommy to determine status.",
    ),
]

TOMMY_HISTORICAL_REACTIVATION = [
    # Mike Martinez accounts worth Tommy reactivating
    (
        "Phoenix Hospitality Group",
        "Phoenix, AZ",
        "Phoenix Hospitality Group — F3E Retail Account",
        STAGE_IDENTIFY, 227.43, "retail_account", TOMMY,
        "Historical Mike Martinez account. Single transaction: Check 1230 $227.43 (8/8/24). "
        "Hospitality/hotel channel — unusual for F3E. Tommy to determine if worth reactivating.",
    ),
    (
        "Runway Express",
        "Gilbert, AZ",
        "Runway Express / Soni Gas — F3E Retail Account",
        STAGE_IDENTIFY, 0.0, "retail_account", TOMMY,
        "Historical Mike Martinez account. C-store/gas channel. "
        "Also appears as 'Soni Food & Gas/Runway Express'. Tommy to reactivate outreach.",
    ),
    (
        "Jesse's Food Mart",
        "Arizona",
        "Jesse's Food Mart — F3E Retail Account",
        STAGE_IDENTIFY, 0.0, "retail_account", TOMMY,
        "Historical Mike Martinez account. C-store channel. Tommy to reactivate outreach.",
    ),
    (
        "Grab n Go",
        "Arizona",
        "Grab n Go — F3E Retail Account",
        STAGE_IDENTIFY, 0.0, "retail_account", TOMMY,
        "Historical Mike Martinez account. C-store channel. Tommy to reactivate outreach.",
    ),
    (
        "Glendale Super Gasoline",
        "Glendale, AZ",
        "Glendale Super Gasoline — F3E Retail Account",
        STAGE_IDENTIFY, 0.0, "retail_account", TOMMY,
        "Historical Mike Martinez account. C-store/gas channel. Tommy to reactivate outreach.",
    ),
    (
        "Super Plus Chevron",
        "Mesa, AZ",
        "Super Plus Chevron — F3E Retail Account",
        STAGE_IDENTIFY, 236.40, "retail_account", TOMMY,
        "Historical Mike Martinez account. Note: marked 'Pre-sold, Not Delivered' (8/24/24) — "
        "$236.40 was invoiced but product was never delivered. Resolution unknown. "
        "Tommy to determine current status and whether to re-engage.",
    ),
    (
        "Mesa Star Mart",
        "Mesa, AZ",
        "Mesa Star Mart — F3E Retail Account",
        STAGE_IDENTIFY, 236.40, "retail_account", TOMMY,
        "Historical Mike Martinez account. Note: marked 'Pre-sold, Not Delivered' (8/24/24) — "
        "$236.40 was invoiced but product was never delivered. Resolution unknown. "
        "Tommy to determine current status and whether to re-engage.",
    ),
]

ALEX_ACCOUNTS = [
    (
        "Kennedy Club Fitness",
        "United States (Shopify — DTC)",
        "Keith Swank / Kennedy Club Fitness — F3E Brand Partnership",
        STAGE_IDENTIFY, 36.00, "brand_partnership", ALEX,
        "Contact: Keith Swank. Shopify DTC order — (1) Peach Paradise Mood 12-pack + "
        "(1) Orangesicle Mood 12-pack. $36.00 (7/25/25). "
        "Fitness club buyer, Mood-focused. Alex to evaluate niche fitness club channel opportunity.",
    ),
    (
        "Hole 9 Yards Sports Bar",
        "868 N Gilbert Rd, Gilbert, AZ",
        "Hole 9 Yards Sports Bar — F3E Brand Partnership",
        STAGE_QUALIFIED, 0.0, "brand_partnership", ALEX,
        "Sports bar venue account. Recurring buyer in Mike Martinez ledger. "
        "Multiple transactions at Gilbert AZ location. "
        "Sports bar/venue channel makes this an Alex conversation for brand activation potential. "
        "Amount: multiple transactions, total not confirmed — Tommy or Alex to pull full history.",
    ),
    (
        "Stone Brewery / 7-Eleven FOASC",
        "Southern California (29 franchise locations)",
        "Stone Brewery / 7-Eleven FOASC — F3E Distribution Partnership",
        STAGE_IDENTIFY, 27115.22, "distribution", ALEX,
        "Distribution partnership via Stone Brewery (intermediary) covering 29 7-Eleven FOASC "
        "franchise locations in Southern California. All 29 locations serviced 10/23/24. "
        "Ledger shows $27,115.22 as 'Stone Brewery Future Deposit' — payment status UNKNOWN. "
        "Alex to determine: (1) was deposit collected? (2) is Stone Brewery relationship still active? "
        "(3) can the 29-location SoCal placement be revived? "
        "Deal left open for Alex to finalize current status. "
        "Sample locations: 7-Eleven #33404 (Inglewood CA), #37046 (LA), #18889 (N Hollywood CA).",
    ),
]

TOMMY_PROSPECTS = [
    # Route prospect list — never transacted, Identify stage
    ("JZ Market",                    "Tempe, AZ",              "JZ Market — F3E Prospect"),
    ("Mission Market",               "Tempe, AZ",              "Mission Market — F3E Prospect"),
    ("Mesa Star Chevron",            "Mesa, AZ",               "Mesa Star Chevron — F3E Prospect"),
    ("Ventura Market",               "San Tan Valley, AZ",     "Ventura Market — F3E Prospect"),
    ("UT Mobile BHC Hayden Road",    "Scottsdale, AZ",         "UT Mobile BHC Hayden Road — F3E Prospect"),
    ("Kings Mini Mart",              "Phoenix, AZ (35th Ave)", "Kings Mini Mart — F3E Prospect"),
    ("Last Stop Chevron",            "Ajo, AZ",                "Last Stop Chevron — F3E Prospect"),
    ("Cave Creek Chevron",           "Cave Creek, AZ",         "Cave Creek Chevron — F3E Prospect"),
    ("Guadalupe Market",             "Guadalupe, AZ",          "Guadalupe Market — F3E Prospect"),
    ("Quick Stop Liquor",            "Tucson, AZ",             "Quick Stop Liquor — F3E Prospect"),
    ("Chandler Liquor",              "Chandler, AZ",           "Chandler Liquor — F3E Prospect"),
    ("A1 Liquor",                    "Glendale, AZ",           "A1 Liquor — F3E Prospect"),
    ("Apache Liquor",                "Apache Junction, AZ",    "Apache Liquor — F3E Prospect"),
    ("T's Liquor",                   "Tempe, AZ",              "T's Liquor — F3E Prospect"),
    ("7-Eleven Mesa Brown",          "Mesa, AZ",               "7-Eleven Mesa Brown — F3E Prospect"),
    ("7-Eleven Tempe Apache",        "Tempe, AZ",              "7-Eleven Tempe Apache — F3E Prospect"),
    ("7-Eleven Scottsdale",          "Scottsdale, AZ",         "7-Eleven Scottsdale — F3E Prospect"),
    ("7-Eleven Chandler",            "Chandler, AZ",           "7-Eleven Chandler — F3E Prospect"),
    ("Arco Gilbert",                 "Gilbert, AZ",            "Arco Gilbert — F3E Prospect"),
    ("Arco Globe",                   "Globe, AZ",              "Arco Globe — F3E Prospect"),
    ("Valero Phoenix North",         "Phoenix, AZ",            "Valero Phoenix North — F3E Prospect"),
    ("Valero Phoenix South",         "Phoenix, AZ",            "Valero Phoenix South — F3E Prospect"),
    ("Mobile Phoenix",               "Phoenix, AZ",            "Mobile Phoenix — F3E Prospect"),
    ("At Your Convenience",          "Mesa, AZ",               "At Your Convenience — F3E Prospect"),
    ("CK Food Mart",                 "Phoenix, AZ",            "CK Food Mart — F3E Prospect"),
    ("HoleBrook Truck Plaza",        "Holbrook, AZ",           "HoleBrook Truck Plaza — F3E Prospect"),
    ("3 Brothers Chevron",           "Arizona",                "3 Brothers Chevron — F3E Prospect"),
    ("Sandhu Chevron 1",             "Arizona",                "Sandhu Chevron 1 — F3E Prospect"),
    ("Sandhu Chevron 2",             "Arizona",                "Sandhu Chevron 2 — F3E Prospect"),
    ("Sandhu Chevron 3",             "Arizona",                "Sandhu Chevron 3 — F3E Prospect"),
    ("7th Street Cafe",              "Phoenix, AZ",            "7th Street Cafe — F3E Prospect"),
    ("Quikfill Shell",               "Arizona",                "Quikfill Shell — F3E Prospect"),
    ("Fairway Liquor",               "Arizona",                "Fairway Liquor — F3E Prospect"),
    ("Quik Fill Gilbert",            "Gilbert, AZ",            "Quik Fill Gilbert — F3E Prospect"),
    ("Shell Tempe 7602",             "Tempe, AZ",              "Shell Tempe 7602 — F3E Prospect"),
    ("SK Chevron",                   "Mesa, AZ",               "SK Chevron — F3E Prospect"),
]


# ── Import logic ──────────────────────────────────────────────────────────────

def _import_full_account(entry: tuple, dry_run: bool) -> bool:
    """Create company + deal + note for a full account entry. Returns True on success."""
    from cora.tools.hubspot_client import (
        HubSpotClientError,
        associate_company_to_deal,
        create_company,
        create_deal,
        create_note,
        find_company_by_name,
    )

    name, address, deal_name, stage, amount, deal_type, owner, notes = entry

    # Dedup company
    existing_co = find_company_by_name(name)
    if existing_co:
        log.info("  Company exists (%s): %s — skipping create", existing_co, name)
        company_id = existing_co
    else:
        if dry_run:
            log.info("  [DRY] Would create company: %s | %s", name, address)
            company_id = "DRY_CO_ID"
        else:
            try:
                company_id = create_company(name=name, address=address)
                log.info("  Created company %s: %s", company_id, name)
            except HubSpotClientError as exc:
                log.error("  Failed to create company %s: %s", name, exc)
                return False
        time.sleep(0.2)

    # Create deal
    if dry_run:
        log.info(
            "  [DRY] Would create deal: %s | stage=%s | $%.2f | owner=%s | type=%s",
            deal_name, stage, amount, owner, deal_type,
        )
        deal_id = "DRY_DEAL_ID"
    else:
        try:
            deal_id = create_deal(
                deal_name=deal_name,
                pipeline_id=F3E_PIPELINE,
                stage_id=stage,
                contact_id=None,
                owner_id=owner,
            )
            # Apply custom tags
            from cora.tools.hubspot_client import _headers, _BASE
            import httpx as _httpx
            props = {
                "hjr_deal_type": deal_type,
                "f3_entity": "F3E",
                "f3_deal_direction": "revenue_in",
                "amount": str(amount) if amount else "",
            }
            with _httpx.Client(timeout=10) as c:
                c.patch(
                    f"{_BASE}/crm/v3/objects/deals/{deal_id}",
                    headers=_headers(),
                    json={"properties": props},
                )
            log.info("  Created deal %s: %s ($%.2f)", deal_id, deal_name, amount)
        except HubSpotClientError as exc:
            log.error("  Failed to create deal %s: %s", deal_name, exc)
            return False
        time.sleep(0.2)

    # Associate company to deal
    if not dry_run and company_id != "DRY_CO_ID" and deal_id != "DRY_DEAL_ID":
        associate_company_to_deal(company_id, deal_id)
        time.sleep(0.15)

    # Add note
    if dry_run:
        log.info("  [DRY] Would add note (%d chars)", len(notes))
    else:
        try:
            note_body = f"F3 Historical Backfill — imported from 2024-2025 sales ledger.\n\n{notes}"
            create_note(body=note_body, deal_id=deal_id)
            time.sleep(0.2)
        except HubSpotClientError as exc:
            log.warning("  Note failed for %s: %s", deal_name, exc)

    return True


def _import_prospect(entry: tuple, dry_run: bool) -> bool:
    """Create company + deal for a prospect (no transaction history, no note)."""
    from cora.tools.hubspot_client import (
        HubSpotClientError,
        associate_company_to_deal,
        create_company,
        create_deal,
        find_company_by_name,
    )
    import httpx as _httpx
    from cora.tools.hubspot_client import _headers, _BASE

    name, address, deal_name = entry

    existing_co = find_company_by_name(name)
    if existing_co:
        log.info("  Prospect company exists (%s): %s — skipping", existing_co, name)
        return True

    if dry_run:
        log.info("  [DRY] Prospect: %s | %s", name, address)
        return True

    try:
        company_id = create_company(name=name, address=address)
        time.sleep(0.2)
        deal_id = create_deal(
            deal_name=deal_name,
            pipeline_id=F3E_PIPELINE,
            stage_id=STAGE_IDENTIFY,
            owner_id=TOMMY,
        )
        props = {
            "hjr_deal_type": "retail_account",
            "f3_entity": "F3E",
            "f3_deal_direction": "revenue_in",
        }
        with _httpx.Client(timeout=10) as c:
            c.patch(
                f"{_BASE}/crm/v3/objects/deals/{deal_id}",
                headers=_headers(),
                json={"properties": props},
            )
        associate_company_to_deal(company_id, deal_id)
        time.sleep(0.2)
        log.info("  Prospect imported: %s", name)
        return True
    except HubSpotClientError as exc:
        log.error("  Prospect failed %s: %s", name, exc)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("F3 Historical HubSpot Backfill%s", " [DRY RUN]" if args.dry_run else "")

    results = {"ok": 0, "fail": 0}

    sections = [
        ("Tommy — Active Accounts (Qualified)", TOMMY_ACTIVE),
        ("Tommy — Open PO / Large Deal",        TOMMY_OPEN_PO),
        ("Tommy — Historical Reactivation",     TOMMY_HISTORICAL_REACTIVATION),
        ("Alex — Brand Partnership Accounts",   ALEX_ACCOUNTS),
    ]

    for section_name, entries in sections:
        log.info("--- %s ---", section_name)
        for entry in entries:
            ok = _import_full_account(entry, args.dry_run)
            results["ok" if ok else "fail"] += 1
            time.sleep(0.3)

    log.info("--- Tommy — Route Prospects (Identify) ---")
    for entry in TOMMY_PROSPECTS:
        ok = _import_prospect(entry, args.dry_run)
        results["ok" if ok else "fail"] += 1
        time.sleep(0.15)

    total = results["ok"] + results["fail"]
    log.info(
        "Backfill complete: %d/%d succeeded (%d failed)",
        results["ok"], total, results["fail"],
    )
    return 0 if results["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
