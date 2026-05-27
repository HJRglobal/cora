"""sales_deck_client.py — F3 distributor sales deck generation via Make + Canva.

Cora flow:
  1. Tommy/Alex: "@cora sales deck for Hensley, F3 full lineup"
  2. Claude extracts intent → calls f3_create_sales_deck tool
  3. This handler calls Claude API to generate structured slide content
  4. POSTs a webhook payload to Make (MAKE_SALES_DECK_WEBHOOK_URL)
  5. Returns immediate acknowledgment; Make handles the rest asynchronously

Make scenario (build in Make.com):
  Trigger:    Webhook (Custom webhook — copy URL to MAKE_SALES_DECK_WEBHOOK_URL)
  Step 1:     Canva — Create a design from the F3 brand template
  Step 2:     Canva — Fill template variables with deck_content data
  Step 3:     Canva — Export design as PDF
  Step 4:     Google Drive — Upload PDF to F3 Sales Decks folder
  Step 5:     Google Drive — Get shareable link
  Step 6:     Slack — Send DM to requester_slack_user_id with the link

Webhook payload (POST application/json to Make):
  {
    "request_id":              str   — UUID4, idempotency key
    "requester_slack_user_id": str   — Slack user ID for DM callback ("U12AB34CD")
    "distributor_name":        str   — display name, e.g. "Hensley"
    "distributor_logo_url":    str|null  — optional PNG/JPG URL for cover slide logo
    "programs":                list  — e.g. ["pure","mood","energy"]
    "notes":                   str   — extra context from the requester
    "deck_content":            object — structured slide content (see below)
    "generated_at":            str   — ISO-8601 UTC
  }

deck_content schema:
  {
    "deck_title":    str,
    "deck_subtitle": str,
    "slides": [
      {
        "slide_number":    int,
        "type":            str,    — cover | brand_story | product_family | product |
                                      why_partner | support_programs | next_steps
        "heading":         str,
        "body":            str,    — optional prose paragraph
        "bullets":         list,   — key talking points
        "presenter_note":  str,    — what to SAY on this slide
        "canva_variables": object  — {variable_name: value} mapped to Canva template fields
      }
    ]
  }

Canva template variables expected by the master template:
  DISTRIBUTOR_NAME, DECK_SUBTITLE, SLIDE_HEADING, SLIDE_BODY,
  BULLET_1 … BULLET_5, PRESENTER_NOTE, PRODUCT_NAME, PRODUCT_TAGLINE,
  PRODUCT_DESCRIPTION, PRODUCT_FLAVORS, MSRP, DISTRIBUTOR_COST,
  DISTRIBUTOR_MARGIN, CASE_PACK, MAP_PRICE
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

import anthropic
import httpx

from ..config import config
from . import hubspot_client

log = logging.getLogger(__name__)

_ALLOWED_ENTITIES = frozenset({"F3E", "FNDR"})

# ---------------------------------------------------------------------------
# F3 Program Catalog
# TODO: migrate to a Google Drive spreadsheet so Harrison/Tommy can update
#       prices without touching code. Drive file ID → config.f3_program_catalog_drive_id
# ---------------------------------------------------------------------------

_F3_PROGRAM_CATALOG: dict[str, dict] = {
    "pure": {
        "display_name": "F3 Pure",
        "tagline": "Real energy for real life.",
        "description": (
            "Clean, natural energy for the health-conscious consumer. "
            "No artificial colors, light on sugar, real vitamins. "
            "Made for people who take care of themselves."
        ),
        "target_consumer": (
            "Health-conscious women 25–40 — pilates, farmers markets, morning walks. "
            "Buys at Sprouts, Whole Foods, specialty fitness studios."
        ),
        "retail_channels": "Natural grocery, health food, specialty fitness, DTC e-commerce",
        "skus": [
            {
                "name": "F3 Pure",
                "size": "12 fl oz",
                "flavors": ["Citrus Sunrise", "Wild Berry", "Watermelon Mint"],
                "msrp": "$3.49",
                "distributor_cost": "UPDATE_WITH_REAL_PRICE",
                "case_pack": "24-pack",
                "map": "$3.49",
                "distributor_margin": "~50%",
            }
        ],
        "key_differentiators": [
            "Clean label — no artificial colors or sweeteners",
            "Natural caffeine sourced from green coffee",
            "Real vitamin complex (B3, B6, B12)",
            "Only 60 calories per can",
        ],
    },
    "mood": {
        "display_name": "F3 Mood",
        "tagline": "Calm the Noise.™",
        "description": (
            "Functional calm-focus energy for high-performance professionals. "
            "L-theanine + nootropic blend delivers clarity without the crash. "
            "Earned calm after a demanding day — not a sleep aid."
        ),
        "target_consumer": (
            "Executives, medical professionals, first responders 30–50. "
            "Premium grocery, specialty wellness, professional channels."
        ),
        "retail_channels": "Premium grocery, specialty wellness, professional/medical channels, DTC",
        "skus": [
            {
                "name": "F3 Mood",
                "size": "12 fl oz",
                "flavors": ["Blueberry Lavender", "Peach Ginger", "Cucumber Mint"],
                "msrp": "$3.99",
                "distributor_cost": "UPDATE_WITH_REAL_PRICE",
                "case_pack": "24-pack",
                "map": "$3.99",
                "distributor_margin": "~50%",
            }
        ],
        "key_differentiators": [
            "L-theanine + adaptogens for calm focus (not sedation)",
            "Zero sugar, 10 calories",
            "No proprietary blends — full label transparency",
            "Positioned as the anti-anxiety energy drink",
        ],
    },
    "energy": {
        "display_name": "F3 Energy",
        "tagline": "Fuel What Matters.",
        "description": (
            "Premium functional energy — sustained performance without jitters or crash. "
            "Clean label, clinically-backed ingredients, four core flavors. "
            "The anchor SKU for mainstream grocery and convenience."
        ),
        "target_consumer": (
            "Active adults 22–45 seeking sustained energy. "
            "Grocery, convenience, fitness, mass retail."
        ),
        "retail_channels": "Grocery, convenience, fitness, mass retail, DTC e-commerce",
        "skus": [
            {
                "name": "F3 Energy",
                "size": "16 fl oz",
                "flavors": ["Tropical Punch", "Blue Raspberry", "Mango Storm", "Cherry Citrus"],
                "msrp": "$2.99",
                "distributor_cost": "UPDATE_WITH_REAL_PRICE",
                "case_pack": "24-pack",
                "map": "$2.99",
                "distributor_margin": "~50%",
            }
        ],
        "key_differentiators": [
            "200mg natural caffeine — sustained release formula",
            "Electrolyte replenishment blend",
            "No crash, no jitters — clinically validated",
            "16 oz at a $2.99 MSRP — competitive mainstream price point",
        ],
    },
}

_KNOWN_PROGRAMS = frozenset(_F3_PROGRAM_CATALOG.keys())

# ---------------------------------------------------------------------------
# F3 Company / Partnership context for Claude's content generation prompt
# ---------------------------------------------------------------------------

_F3_COMPANY_CONTEXT = """
F3 Energy is a premium functional beverage brand under HJR Global, founded by Harrison Roback.
The brand family includes three sub-brands: F3 Pure, F3 Mood, and F3 Energy.

Brand positioning: Premium functional beverages with clean labels, real ingredients,
and purpose-built formulations for distinct consumer occasions.

Growth stage: Emerging brand with strong DTC traction, expanding into retail distribution.
Each distributor partner gets dedicated co-op marketing support, POS materials,
and social media assets from the in-house BDM (Brand and Digital Marketing) team.

Support programs available for distributor partners:
  - Co-op marketing funds (% of volume commitment)
  - In-store POS display materials (shelf talkers, endcap displays, cooler decals)
  - Social media content assets (imagery, video, Stories templates)
  - Staff training materials and brand education deck
  - Launch support (demo events, in-store activation)
  - Direct brand team contact (sales + marketing)

Distribution partnership terms (high level, confirm with Tommy):
  - MOQ: 1 pallet per SKU for initial order
  - Net 30 payment terms standard
  - Exclusive territory negotiable based on volume commitment
  - Quarterly business review program for key partners
"""


# ---------------------------------------------------------------------------
# Claude content generation
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are a senior CPG brand strategist generating structured slide content
for an F3 Energy distributor sales presentation. Your job is to write compelling,
professional copy that helps the F3 sales team close the distribution partnership.

Rules:
- Tone: confident, clear, relationship-focused. Not salesy or hyperbolic.
- Tailor the deck to the specific distributor (use their name, reference their market if notes provided)
- Each slide should have a clear single message
- Bullets should be punchy (8 words max each) and scannable
- Presenter notes should tell the rep WHAT TO SAY, not repeat what's on the slide
- Output ONLY valid JSON matching the exact schema. No prose, no markdown fences.

Output schema:
{
  "deck_title": "string",
  "deck_subtitle": "string",
  "slides": [
    {
      "slide_number": 1,
      "type": "cover",
      "heading": "string",
      "body": "string",
      "bullets": [],
      "presenter_note": "string",
      "canva_variables": {"DISTRIBUTOR_NAME": "...", "DECK_SUBTITLE": "..."}
    }
  ]
}

Slide types and Canva variables:
  cover          → DISTRIBUTOR_NAME, DECK_SUBTITLE
  brand_story    → SLIDE_HEADING, SLIDE_BODY, BULLET_1…BULLET_4
  product_family → SLIDE_HEADING, SLIDE_BODY, BULLET_1…BULLET_3
  product        → PRODUCT_NAME, PRODUCT_TAGLINE, PRODUCT_DESCRIPTION,
                   PRODUCT_FLAVORS, MSRP, DISTRIBUTOR_COST, DISTRIBUTOR_MARGIN,
                   CASE_PACK, MAP_PRICE
  why_partner    → SLIDE_HEADING, SLIDE_BODY, BULLET_1…BULLET_4
  support_programs → SLIDE_HEADING, BULLET_1…BULLET_5
  next_steps     → SLIDE_HEADING, BULLET_1…BULLET_4
"""


def _generate_deck_content(
    distributor_name: str,
    programs: list[str],
    notes: str,
    hubspot_data: dict | None = None,
) -> dict:
    """Call Claude to generate structured slide content. Returns parsed dict."""
    selected = {k: _F3_PROGRAM_CATALOG[k] for k in programs if k in _F3_PROGRAM_CATALOG}

    # Build HubSpot context block for Claude
    if hubspot_data:
        contact_line = ""
        if hubspot_data.get("primary_contact_name"):
            contact_line = (
                f"  Primary contact: {hubspot_data['primary_contact_name']}"
                + (f", {hubspot_data['primary_contact_title']}" if hubspot_data.get("primary_contact_title") else "")
            )
        location_line = ""
        if hubspot_data.get("city") or hubspot_data.get("state"):
            parts = [p for p in [hubspot_data.get("city"), hubspot_data.get("state")] if p]
            location_line = f"  Location: {', '.join(parts)}"
        deals_line = ""
        if hubspot_data.get("open_deals"):
            deals_line = "  Existing F3E deals in CRM:\n" + "\n".join(
                f"    - {d}" for d in hubspot_data["open_deals"]
            )
        industry_line = f"  Industry: {hubspot_data['industry']}" if hubspot_data.get("industry") else ""
        website_line = f"  Website: {hubspot_data['website']}" if hubspot_data.get("website") else ""
        crm_block = "\n".join(
            line for line in [
                "CRM record found for this distributor:",
                f"  Name: {hubspot_data['name']}",
                website_line, industry_line, location_line, contact_line, deals_line,
            ]
            if line
        )
    else:
        crm_block = "No CRM record found — proceed with the provided info only."

    family_slide = "3. Product family overview (one slide showing all programs side-by-side)" if len(programs) > 1 else ""
    product_start = 3 + (1 if len(programs) > 1 else 0)
    product_slides = "\n".join(
        f"{product_start + i}. {_F3_PROGRAM_CATALOG[p]['display_name']} product slide"
        for i, p in enumerate(programs)
        if p in _F3_PROGRAM_CATALOG
    )

    user_message = f"""Generate a distributor sales deck for: {distributor_name}

Programs to include: {', '.join(p.upper() for p in programs)}
Additional context from requester: {notes or 'None provided'}

{crm_block}

F3 Energy company context:
{_F3_COMPANY_CONTEXT}

Program details:
{json.dumps(selected, indent=2)}

Build the following slides:
1. Cover slide
2. F3 Energy brand story / who we are
{family_slide}
{product_slides}
- Why partner with F3 slide
- Support programs slide
- Next steps / CTA slide

If a CRM contact name is available, address the deck to them on the cover and next-steps slides.
If there are existing deals in the CRM, reference the existing relationship warmly in the next-steps slide.
If location/territory is known, tailor any regional references accordingly.
"""

    client = anthropic.Anthropic(api_key=config.anthropic_api_key)
    response = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=4096,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    raw = response.content[0].text.strip()
    # Strip markdown code fences if model wraps response
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    return json.loads(raw)


# ---------------------------------------------------------------------------
# Main tool handler
# ---------------------------------------------------------------------------

def handle_f3_create_sales_deck(
    slack_user_id: str,
    entity: str,
    tool_input: dict[str, Any],
) -> str:
    """Slack tool handler for f3_create_sales_deck.

    Generates structured slide content via Claude, then fires a Make webhook
    that triggers the Canva → Drive → Slack DM pipeline asynchronously.
    """
    if entity not in _ALLOWED_ENTITIES:
        return (
            "Sales deck generation is only available in F3 or Founder channels. "
            "Ask from #f3e-leadership, #f3-sales, or a similar F3 channel."
        )

    webhook_url = config.make_sales_deck_webhook_url
    if not webhook_url:
        return (
            "Sales deck generation is not configured yet. "
            "Ask Harrison to add MAKE_SALES_DECK_WEBHOOK_URL to Cora's environment."
        )

    distributor_name: str = (tool_input.get("distributor_name") or "").strip()
    if not distributor_name:
        return "Missing `distributor_name`. Who are you presenting to?"

    raw_programs: list[str] = tool_input.get("programs") or list(_KNOWN_PROGRAMS)
    programs = [p.lower().strip() for p in raw_programs if p.lower().strip() in _KNOWN_PROGRAMS]
    if not programs:
        valid = ", ".join(sorted(_KNOWN_PROGRAMS))
        return f"No valid programs specified. Valid options: {valid}."

    notes: str = (tool_input.get("notes") or "").strip()
    distributor_logo_url: str | None = tool_input.get("distributor_logo_url") or None

    # --- HubSpot enrichment (best-effort; never blocks deck generation) ---
    hubspot_data = hubspot_client.search_distributor_company(distributor_name)
    if hubspot_data:
        log.info(
            "sales_deck: HubSpot enrichment found company=%r contact=%r",
            hubspot_data.get("name"), hubspot_data.get("primary_contact_name"),
        )
    else:
        log.info("sales_deck: no HubSpot record for distributor=%r", distributor_name)

    # --- Generate slide content via Claude ---
    try:
        deck_content = _generate_deck_content(distributor_name, programs, notes, hubspot_data)
    except json.JSONDecodeError as exc:
        log.exception("sales_deck: Claude returned non-JSON for distributor=%s", distributor_name)
        return f"Failed to generate slide content (unexpected model output): {exc}"
    except Exception as exc:
        log.exception("sales_deck: content generation failed distributor=%s", distributor_name)
        return f"Failed to generate slide content: {exc}"

    # --- Build Make webhook payload ---
    payload = {
        "request_id": str(uuid.uuid4()),
        "requester_slack_user_id": slack_user_id,
        "distributor_name": distributor_name,
        "distributor_logo_url": distributor_logo_url,
        "programs": programs,
        "notes": notes,
        "deck_content": deck_content,
        # hubspot_data gives Make the contact name/title for the Canva "Prepared for:" field,
        # the territory for any regional slide variables, and the HubSpot URL for auto-logging.
        "hubspot_data": hubspot_data,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    # --- Fire webhook (Make responds 200 immediately; Canva pipeline runs async) ---
    try:
        resp = httpx.post(webhook_url, json=payload, timeout=15.0)
        resp.raise_for_status()
    except httpx.TimeoutException:
        log.error("sales_deck: Make webhook timed out distributor=%s", distributor_name)
        return (
            f"The deck request for *{distributor_name}* was sent but the automation "
            "gateway timed out confirming receipt. Check with Harrison — the Make "
            "scenario may still have triggered."
        )
    except httpx.HTTPStatusError as exc:
        log.error(
            "sales_deck: Make webhook HTTP %s distributor=%s",
            exc.response.status_code, distributor_name,
        )
        return (
            f"Deck generation failed — Make returned HTTP {exc.response.status_code}. "
            "Check that the Make scenario is active and the webhook URL is correct."
        )
    except Exception as exc:
        log.exception("sales_deck: Make webhook error distributor=%s", distributor_name)
        return f"Deck generation failed — could not reach Make: {exc}"

    # --- Acknowledgment ---
    program_labels = " · ".join(
        _F3_PROGRAM_CATALOG[p]["display_name"] for p in programs if p in _F3_PROGRAM_CATALOG
    )
    slide_count = len(deck_content.get("slides", []))

    lines = [
        f"Your *{distributor_name}* sales deck is being built now.",
        f"Programs: {program_labels}  |  {slide_count} slides generated",
    ]

    if hubspot_data:
        crm_bits = []
        if hubspot_data.get("primary_contact_name"):
            contact_str = hubspot_data["primary_contact_name"]
            if hubspot_data.get("primary_contact_title"):
                contact_str += f", {hubspot_data['primary_contact_title']}"
            crm_bits.append(f"addressed to *{contact_str}*")
        location_parts = [p for p in [hubspot_data.get("city"), hubspot_data.get("state")] if p]
        if location_parts:
            crm_bits.append(f"territory: {', '.join(location_parts)}")
        if hubspot_data.get("open_deals"):
            crm_bits.append(f"{len(hubspot_data['open_deals'])} existing deal(s) referenced")
        if crm_bits:
            lines.append(f"CRM match found — {' · '.join(crm_bits)}")
    else:
        lines.append("_No CRM record found for this distributor — deck uses provided info only._")

    lines += [
        "",
        "Canva is filling the brand template and the finished PDF will be saved to "
        "Google Drive. I'll DM you the link when it's ready — usually under 2 minutes.",
    ]

    if distributor_logo_url:
        lines.append("Distributor logo will be embedded on the cover slide.")
    else:
        lines.append(
            "_Tip: Re-run with `distributor_logo_url` to auto-embed their logo on the cover._"
        )

    return "\n".join(lines)
