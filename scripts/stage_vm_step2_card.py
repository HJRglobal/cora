r"""VM step 2 -- the ONE T0 propose-only card to Harrison's DM (DR/VM step 1, slice M4).

WHAT THIS IS
    The scoping packet (_shared/projects/cora/2026-09-23_cora_vm-step1-scoping-packet.md)
    is input to Harrison + Justin. This script renders its decision surface as ONE Slack
    card in Harrison's DM: the provider cost table WITH the BAA column on every row
    (charter D2: BAA status is a mandatory VISIBLE input at the later PHI decision point,
    presented now because it costs nothing), the band verdict, and the "Justin's sanity
    check" line. It carries NO action button that spends, provisions or signs anything
    (charter C5: spend is a write; T0 = propose-only). Harrison reads it and acts by hand.

    It also holds the D2 DECISION-CARD TEMPLATE for the LATER KB-restore step: the only
    surface that may ask for Harrison's PHI-adjacent sign-off, and it MUST render both
    mandatory inputs (BAA status per provider; whether Emily weighed in, yes/no + date).
    render_d2_card() is a TEMPLATE -- it is never posted by this script.

    Dry-run by default (prints the blocks). --apply posts ONE message to Harrison's DM
    through the sanitized egress (cora import installs the WebClient patch; sanitize_text is
    called explicitly too). A Slack post is a connector write: Harrison runs --apply.

    .venv\Scripts\python.exe scripts\stage_vm_step2_card.py            # dry-run: print the card
    .venv\Scripts\python.exe scripts\stage_vm_step2_card.py --apply    # Harrison's hand: post it once

DATA
    PROVIDER_QUOTES below is the packet's cost table -- every number carries its public
    source URL and access date (kickoff section 4 #7). Numbers are LIST prices for the ruled
    spec (Windows Server, 16 vCPU, 64 / 128 GB, ~1 TiB SSD, 7 daily restore points, 730 h),
    verified against the cited pages by an independent pass on 2026-09-23 (the corrections it
    found are folded in). No console was opened, no account created, no quote accepted.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO_ROOT / ".env", override=True)

log = logging.getLogger("stage_vm_step2_card")

HARRISON_ID = os.environ.get("HARRISON_SLACK_USER_ID", "U0B2RM2JYJ1")
RULED_BAND_USD = (300, 600)                 # charter D1: ~$300-600/mo; Justin sanity-checks
ACCESSED = "2026-09-22/23"
PACKET_PATH = r"G:\My Drive\HJR-Founder-OS\_shared\projects\cora\2026-09-23_cora_vm-step1-scoping-packet.md"

#: One row per shortlisted provider. monthly figures = VM (Windows list price, 730 h) +
#: ~1 TiB SSD + 7-point snapshot estimate; egress assumed inside the free allowance.
PROVIDER_QUOTES: list[dict[str, Any]] = [
    {
        "provider": "Microsoft Azure", "region": "West US 3 (Phoenix, AZ)",
        "sku_64": "Standard_D16as_v5", "vm_64_usd": 1039.52, "sku_128": "Standard_E16as_v5", "vm_128_usd": 1197.20,
        "storage": "Premium SSD P30 1,024 GiB", "storage_usd": 122.88, "backup": "incremental snapshots (Std HDD, ~7 pts)", "backup_usd": 57.34,
        "total_64_usd": 1219.74, "total_128_usd": 1377.42,
        "egress": "100 GB free, then $0.087/GB",
        "baa": "YES -- included by default in the Microsoft Product Terms / DPA (no separate contract); VMs, Storage, Backup, VNet in scope",
        "baa_url": "https://learn.microsoft.com/en-us/azure/compliance/offerings/offering-hipaa-us",
        "in_band_path": "8 vCPU/64 GB E8as_v5 Windows = $598.60 compute-only but ~$778 LOADED (P30 + snapshots) -- NOT in band at the same spec; only Azure Hybrid Benefit (own Windows Server licenses + SA, Linux rate: D16as_v5 $502 / E8as_v5 $330 compute) or a 4 vCPU CI-only start reaches the band",
        "sources": [
            "https://prices.azure.com/api/retail/prices (Retail Prices API, armRegionName=westus3, Consumption; D16as_v5 1.424/h, E16as_v5 1.64/h, P30 LRS 122.88/mo, LRS snapshots 0.05/GB-mo)",
            "https://azure.microsoft.com/en-us/pricing/details/bandwidth/",
            "https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/general-purpose/dasv5-series",
        ],
        "verification": "31/34 checks confirmed on the cited pages; 3 attribution corrections (SKU spec pages, BAA scope citation), no value changed",
    },
    {
        "provider": "Amazon Web Services", "region": "us-west-2 (Oregon); a Phoenix Local Zone us-west-2-phx-1 exists (priced separately)",
        "sku_64": "m7i.4xlarge", "vm_64_usd": 1125.95, "sku_128": "r7i.4xlarge", "vm_128_usd": 1309.91,
        "storage": "EBS gp3 1,024 GiB", "storage_usd": 81.92, "backup": "EBS snapshots, 7 daily (5%/day churn est.)", "backup_usd": 69.12,
        "total_64_usd": 1276.99, "total_128_usd": 1460.95,
        "egress": "100 GB free, then $0.09/GB",
        "baa": "YES -- self-service acceptance in AWS Artifact; EC2 + EBS are HIPAA-eligible services",
        "baa_url": "https://aws.amazon.com/compliance/hipaa-eligible-services-reference/",
        "in_band_path": "8 vCPU/64 GB r7i.2xlarge Windows = $654.96 compute, ~$806 LOADED -- NOT in band at the same spec (only 8 vCPU/32 GB m7i.2xlarge COMPUTE-ONLY, $563, sits in band, below the 64 GB floor); the band needs a re-rule or a 4 vCPU CI-only start",
        "sources": [
            "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/us-west-2/index.csv (Price List Bulk API; Windows On-Demand, gp3, snapshots)",
            "https://aws.amazon.com/ec2/pricing/on-demand/ (100 GB/month free data transfer out)",
            "https://aws.amazon.com/compliance/hipaa-compliance/",
        ],
        "verification": "25/28 confirmed; corrections: the free-egress and Backup-storage figures live on other official pages than first cited; a Phoenix Local Zone DOES exist",
    },
    {
        "provider": "Google Cloud", "region": "us-west4 (Las Vegas); us-west1 (Oregon) ~6% cheaper; us-west2 (LA) higher tier",
        "sku_64": "n2-standard-16 + Windows license", "vm_64_usd": 1176.02, "sku_128": "n2-highmem-16 + Windows license", "vm_128_usd": 1398.93,
        "storage": "pd-ssd zonal 1,024 GiB", "storage_usd": 191.49, "backup": "regional standard snapshots, 7 pts", "backup_usd": 74.55,
        "total_64_usd": 1442.06, "total_128_usd": 1664.97,
        "egress": "Premium Tier $0.12/GiB (first TiB), ~1 GB free",
        "baa": "YES -- self-serve acceptance in the Cloud console (IAM & Admin > Privacy & Security); Compute Engine + Persistent Disk covered",
        "baa_url": "https://cloud.google.com/security/compliance/hipaa",
        "in_band_path": "Windows license is $0.046/vCPU-h everywhere ($268.64/mo at 8 vCPU): n2-highmem-8 8 vCPU/64 GB = $699 VM-only, ~$965 LOADED -- NOT in band; only 4 vCPU sizes (CI-only) reach the band",
        "sources": [
            "https://cloud.google.com/products/compute/pricing/general-purpose (N2 table, region selector, Default USD)",
            "https://cloud.google.com/compute/disks-image-pricing",
            "https://cloud.google.com/vpc/network-pricing",
        ],
        "verification": "25/27 confirmed; correction: Los Angeles is priced ABOVE Las Vegas (not the same tier)",
    },
    {
        "provider": "Vultr (non-hyperscaler alternative)", "region": "Los Angeles (lax); no Arizona region",
        "sku_64": "vc2-16c-64gb (shared vCPU) + Windows license", "vm_64_usd": 576.00, "sku_128": "voc-m-16c-128gb (dedicated) + Windows license (derived)", "vm_128_usd": 896.00,
        "storage": "1,280 GB NVMe included on the 64 GB plan; the 128 GB plan has 800 GB local + $20 NVMe block top-up ($0.10/GB)",
        "storage_usd": 0.00, "storage_128_usd": 20.00, "backup": "snapshots $0.05/GB-mo x 7 full images", "backup_usd": 350.00,
        "total_64_usd": 926.00, "total_128_usd": 1266.00,
        "egress": "10 TB/mo included per plan + 2 TB account free, then $0.01/GB",
        "baa": "CONDITIONAL -- executes a BAA only through sales for its 'Configurable HIPAA Server' offering (ToS s.17); not self-serve; LA is marked HIPAA-compliant on its matrix",
        "baa_url": "https://www.vultr.com/legal/tos/",
        "in_band_path": "voc-m-8c-64gb dedicated 8 vCPU/64 GB $320 + license $128 + block top-up $60 = $508 BEFORE backups; with 7 daily snapshots at $0.05/GB-mo the loaded figure is ~$648-858 depending on image size -- NOT in band at the same spec (2-point automatic backups, +20%, ~$572 is the closest); the 128 GB license figure is DERIVED from the $16/vCPU pattern, not published",
        "sources": [
            "https://www.vultr.com/servers/windows/ (Windows plan + license table)",
            "https://api.vultr.com/v2/plans?type=voc-m (public API: monthly_cost)",
            "https://docs.vultr.com/support/platform/billing/does-vultr-charge-for-stored-snapshots",
        ],
        "verification": "25/34 confirmed; the 128 GB Windows license, the snapshot size and the totals are DERIVED (arithmetic correct, inputs partly unpublished) -- confidence medium",
    },
]

#: The D2 decision-card TEMPLATE (charter D2): the ONLY surface that may ask for Harrison's
#: sign-off on a PHI-adjacent move, and it MUST show both mandatory inputs.
D2_MANDATORY_FIELDS: tuple[str, ...] = ("BAA status per provider", "Emily weighed in? (yes/no + date)",
                                        "PHI-adjacent bytes proposed to move", "Harrison sign-off")


def in_band(total: float) -> bool:
    lo, hi = RULED_BAND_USD
    return lo <= total <= hi


SLACK_SECTION_LIMIT = 3000   # Slack refuses a section text above this; we never slice, we refuse


def render_provider_row(q: dict[str, Any]) -> str:
    """ONE provider's row (mrkdwn): both totals with computed band verdicts, storage PER
    SIZE when the plan needs a top-up on one row, egress, and the BAA column."""
    flag64 = "in band" if in_band(q["total_64_usd"]) else "OUTSIDE band"
    flag128 = "in band" if in_band(q["total_128_usd"]) else "OUTSIDE band"
    s64 = q["storage_usd"]
    s128 = q.get("storage_128_usd", s64)
    storage = f"storage ${s64:,.0f}" if s128 == s64 else f"storage ${s64:,.0f} (64 GB) / ${s128:,.0f} (128 GB)"
    return (f"- *{q['provider']}* ({q['region']}): 64 GB `{q['sku_64']}` *${q['total_64_usd']:,.0f}/mo* ({flag64}) | "
            f"128 GB `{q['sku_128']}` *${q['total_128_usd']:,.0f}/mo* ({flag128}) | {storage} + backup ~${q['backup_usd']:,.0f} | "
            f"egress {q['egress']} | *BAA:* {q['baa']}\n    in-band path: {q['in_band_path']}")


def render_cost_table_text() -> str:
    """The whole table as one string (the packet / dry-run view); the card posts one
    section PER PROVIDER so no row can be truncated."""
    head = f"*Provider cost table -- Windows Server VM, 16 vCPU, ~1 TiB SSD, 7 restore points, list prices, accessed {ACCESSED}. Ruled band: ${RULED_BAND_USD[0]}-{RULED_BAND_USD[1]}/mo.*"
    return "\n".join([head] + [render_provider_row(q) for q in PROVIDER_QUOTES])


def band_headline() -> str:
    """Derived, never typed: how many 64 GB rows sit outside the band at the ruled spec."""
    n, total = sum(1 for q in PROVIDER_QUOTES if not in_band(q["total_64_usd"])), len(PROVIDER_QUOTES)
    lead = "Every one of the" if n == total else ("None of the" if n == 0 else f"{n} of the")
    return (f"{lead} {total} shortlisted rows lands OUTSIDE the ruled ${RULED_BAND_USD[0]}-{RULED_BAND_USD[1]}/mo band at the "
            f"16 vCPU spec ({n}/{total}): the Windows Server license alone is ~$537/mo at 16 vCPU on every hyperscaler. "
            "Loaded to the same spec (64 GB floor, ~1 TiB SSD, 7 restore points) NO pay-as-you-go 8 vCPU row is in band either "
            "(Vultr ~$648-858 depending on image size; Azure ~$778 / AWS ~$806 / GCP ~$965); the band is reached only by "
            "Azure Hybrid Benefit (own Windows Server licenses + SA), a 4 vCPU CI-only start, or a re-ruled band -- see each row.")


def build_card() -> tuple[str, list[dict[str, Any]]]:
    """(fallback text, blocks). Propose-only: no button spends, provisions or signs. One
    section per provider so the D2-mandatory BAA column can never be cut by a length slice."""
    header = ("*VM step 2 provisioning -- ready for Justin's sanity check + your go* (DR/VM step 1, M4; T0 propose-only)\n"
              + band_headline())
    justin = ("*Justin's sanity check (charter D1):* a Gmail DRAFT is staged in the packet (D-137: Harrison sends) asking him for "
              "(1) the checkpoint date and (2) his READ on the four options -- incl. whether HJR holds Windows Server licenses with "
              "Software Assurance (unlocks Azure Hybrid Benefit). The band-vs-size RULING is Harrison's after that read (C5 / D-108). "
              "Cover-line date reads `[Justin to name]`.")
    footer = (f"Packet: `{PACKET_PATH}` (spec, PHI-free sandbox scope by exclusion, secrets posture, step-2 parity checklist, D2 card template, rollback). "
              "NO provisioning, NO account, NO spend, NO quote accepted -- this card asks for nothing but a read. "
              "Decision owner: Harrison after Justin's read (charter C5: spend is a write).")
    head_line = f"*Provider cost table -- Windows Server VM, 16 vCPU, ~1 TiB SSD, 7 restore points, list prices, accessed {ACCESSED}. Ruled band: ${RULED_BAND_USD[0]}-{RULED_BAND_USD[1]}/mo. BAA column on every row (charter D2).*"
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": head_line}},
    ]
    for q in PROVIDER_QUOTES:
        row = render_provider_row(q)
        if len(row) > SLACK_SECTION_LIMIT:
            raise ValueError(f"provider row for {q['provider']} exceeds Slack's section limit ({len(row)} chars) -- shorten the notes, never truncate")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": row}})
    blocks += [
        {"type": "section", "text": {"type": "mrkdwn", "text": justin}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]},
    ]
    for b in blocks:
        t = b.get("text", {}).get("text", "") if isinstance(b.get("text"), dict) else ""
        if len(t) > SLACK_SECTION_LIMIT:
            raise ValueError(f"a card section exceeds Slack's limit ({len(t)} chars)")
    fallback = "VM step 2 provisioning -- ready for Justin's sanity check + your go (T0 propose-only; see the packet)"
    return fallback, blocks


def render_d2_card(*, baa_status_per_provider: dict[str, str], emily_weighed_in: str, emily_date: str,
                   phi_bytes_proposed: str, target_provider: str) -> tuple[str, list[dict[str, Any]]]:
    """TEMPLATE for the LATER KB-restore step (charter D2). Never posted by this script.
    Renders every mandatory field; a caller that omits or blanks one gets a ValueError,
    not a card. Charter D2 (Harrison-edited): BAA status and Emily's input are MANDATORY
    VISIBLE INPUTS -- inputs, NOT preconditions (consistent with D-108: Harrison is the sole
    ultimate authority). So 'no' renders truthfully as a visible input; it never blocks."""
    if not baa_status_per_provider or any(not str(s).strip() for s in baa_status_per_provider.values()):
        raise ValueError("D2 card requires a NON-BLANK BAA status for every listed provider")
    if target_provider not in baa_status_per_provider:
        raise ValueError(f"D2 card: target provider {target_provider!r} has no BAA status line")
    if emily_weighed_in not in ("yes", "no"):
        raise ValueError("D2 card requires emily_weighed_in = yes|no")
    if emily_weighed_in == "yes":
        import re as _re  # noqa: PLC0415
        if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", emily_date or ""):
            raise ValueError("D2 card requires the date Emily weighed in as YYYY-MM-DD")
    if not phi_bytes_proposed:
        raise ValueError("D2 card requires the PHI-adjacent bytes proposed to move (or 'none')")
    baa_lines = "\n".join(f"- {p}: {s}" for p, s in sorted(baa_status_per_provider.items()))
    emily = (f"yes ({emily_date})" if emily_weighed_in == "yes"
             else "no -- Emily has NOT weighed in (charter D2: a mandatory VISIBLE input, not a precondition; "
                  "standing flag: PHI-adjacent data on a host without a BAA is a regulatory exposure for LEX)")
    text = (f"*D2 decision card -- PHI-adjacent move to `{target_provider}` needs HARRISON'S EXPLICIT SIGN-OFF*\n"
            f"*{D2_MANDATORY_FIELDS[0]}:*\n{baa_lines}\n"
            f"*{D2_MANDATORY_FIELDS[1]}:* {emily}\n"
            f"*{D2_MANDATORY_FIELDS[2]}:* {phi_bytes_proposed}\n"
            f"*{D2_MANDATORY_FIELDS[3]}:* pending -- reply in this thread; nothing moves until then.")
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    return "D2 decision card -- PHI-adjacent move needs Harrison's sign-off", blocks


def assert_propose_only(blocks: list[dict[str, Any]]) -> None:
    """No actions block, no button, no interactive element of any kind."""
    for b in blocks:
        if b.get("type") == "actions" or "accessory" in b:
            raise ValueError("propose-only card must carry no interactive element")
        for el in b.get("elements", []) or []:
            if el.get("type") not in ("mrkdwn", "plain_text", "image"):
                raise ValueError(f"propose-only card must carry no interactive element (found {el.get('type')})")


def post(blocks: list[dict[str, Any]], fallback: str) -> bool:
    """ONE DM to Harrison via the sanitized egress. Harrison's hand (--apply)."""
    import cora  # noqa: F401,PLC0415 -- installs the WebClient egress patch in-process
    from cora import slack_egress  # noqa: PLC0415
    from slack_sdk import WebClient  # noqa: PLC0415
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("SLACK_BOT_TOKEN not set")
    client = WebClient(token=token)
    opened = client.conversations_open(users=[HARRISON_ID])
    channel = opened["channel"]["id"]
    for b in blocks:
        t = b.get("text", {})
        if isinstance(t, dict) and t.get("text"):
            t["text"] = slack_egress.sanitize_text(t["text"])
        for el in b.get("elements", []) or []:
            if el.get("text"):
                el["text"] = slack_egress.sanitize_text(el["text"])
    resp = client.chat_postMessage(channel=channel, text=slack_egress.sanitize_text(fallback), blocks=blocks,
                                   unfurl_links=False, unfurl_media=False)
    log.info("vm-step2 card posted ts=%s", resp.get("ts"))
    return bool(resp.get("ok"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Stage the T0 'VM step 2 provisioning' card (dry-run default).")
    ap.add_argument("--apply", action="store_true", help="post ONE card to Harrison's DM (Harrison runs this)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    fallback, blocks = build_card()
    assert_propose_only(blocks)
    if not args.apply:
        print("DRY-RUN -- the card that --apply would post to Harrison's DM:\n")
        print(json.dumps(blocks, indent=2, ensure_ascii=False))
        print("\n(no interactive elements; nothing posted)")
        return 0
    ok = post(blocks, fallback)
    print("posted" if ok else "post FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
