"""spec_generator.py — LLM-powered PhotoRoom spec generator.

Converts a plain-English creative brief into a fully-formed ImageSpec
using Claude (same Anthropic key Cora uses for chat).

Brand knowledge is distilled from the locked V1 F3 brand guidelines
(02-F3-Energy/brand/{brand}/brand-guidelines.md). Updated here when
brand guidelines change — no live Drive read needed, which keeps this
fast and offline-safe.

Public API:
    generate_spec_from_brief(brand, brief, output_size, output_filename) -> ImageSpec
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone

import anthropic

from ..config import config
from .photoroom_specs import (
    Background,
    BackgroundGuidance,
    Destination,
    ImageRef,
    ImageSpec,
    OutputSpec,
    SpecMetadata,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Brand guidelines distillations (V1, locked 2026-05-22)
# Updated here when creative direction changes.
# ---------------------------------------------------------------------------

_BRAND_GUIDES: dict[str, dict] = {
    "pure": {
        "tagline": "Real energy for real life.",
        "avatar": "Lauren — woman in her early 30s, health-conscious, active lifestyle",
        "vibe": (
            "Clean, bright, genuine energy. Natural light and outdoor settings. "
            "Feels authentic and wholesome, NOT performance-gym energy. "
            "Think Sprouts / Whole Foods shopper, pilates class, morning walks, "
            "farmers markets, weekend brunch."
        ),
        "photography": (
            "Golden hour preferred. Soft bokeh backgrounds. Vibrant but natural colors. "
            "Open air, greenery, water, sunlight. Lifestyle over sport. "
            "The can fits naturally in the scene — not staged or forced."
        ),
        "negative": (
            "gym, weightlifting, dark moody atmosphere, beige / neutral tones, "
            "office interiors, nightclub, artificial lighting, text overlays, logos"
        ),
        "palette_hint": "Bright greens, warm whites, sky blues, golden yellows",
        "default_guidance": 0.70,
    },
    "mood": {
        "tagline": "Calm the Noise.™",
        "avatar": "Marcus — executive, medical professional, or first-responder winding down",
        "vibe": (
            "Calm focus, clarity after a demanding day. Anti-anxiety energy, NOT a sleep aid. "
            "Sophisticated, minimal, intentional. Feels earned and restorative."
        ),
        "photography": (
            "Soft indoor or transitional lighting. Dusk or late afternoon natural light. "
            "Quiet spaces — a home office corner, a lounge chair, a terrace at sunset. "
            "The mood is exhale, not sleep."
        ),
        "negative": (
            "bed, pillow, sleep imagery, bright energetic colors, gym, "
            "crowded spaces, fast motion, text overlays, logos"
        ),
        "palette_hint": "Muted blues, lavender, warm grays, deep teal",
        "default_guidance": 0.68,
    },
    "energy": {
        "tagline": "Fuel. Focus. Finish.",
        "avatar": "Alex — MMA-adjacent athlete, competitor, driven professional",
        "vibe": (
            "Raw drive, mental clarity, physical performance. Bold and unapologetic. "
            "Signature visual: red duotone. Feels like the moment before you go."
        ),
        "photography": (
            "Dynamic lighting, high contrast, strong shadows. Gym, arena, training environment "
            "or peak-outdoor settings (mountain summit, urban rooftop at dusk). "
            "Action or charged stillness — never passive."
        ),
        "negative": (
            "soft pastel colors, relaxed lifestyle, beach leisure, "
            "text overlays, logos, cartoonish style"
        ),
        "palette_hint": "Bold red, black, white, deep charcoal",
        "default_guidance": 0.72,
    },
}

# Default can image URLs (Shopify CDN) for each brand.
# These are real production images used for compositing.
_DEFAULT_CAN_URLS: dict[str, str] = {
    "pure": (
        "https://cdn.shopify.com/s/files/1/0747/7084/1920/files/"
        "F3_StrawberryLemonade_Front_Pure.png?v=1765900041"
    ),
    "mood": "",   # TODO: fill when Mood Shopify assets are uploaded
    "energy": "", # TODO: fill when Energy Shopify assets are uploaded
}

_SYSTEM_PROMPT = """\
You are a senior creative director for F3, a premium functional energy drink brand.
Your job is to write precise, evocative AI image-generation prompts for PhotoRoom's
AI Backgrounds API, which composites a product image against an AI-generated scene.

Rules:
- The prompt describes ONLY the background scene — PhotoRoom composites the product
  separately. Do NOT describe the can, bottle, or product in the background prompt.
- Be specific: include lighting direction, time of day, color palette, depth-of-field
  cues, and emotional atmosphere.
- Keep the background_prompt between 60 and 180 words.
- The negative_prompt should list concrete things to exclude (comma-separated).
- guidance is a float 0.0–1.0 controlling how closely PhotoRoom follows the prompt
  (higher = more literal; lifestyle shots work best at 0.65–0.75).

Always respond with ONLY a valid JSON object, no markdown, no explanation:
{
  "background_prompt": "...",
  "negative_prompt": "...",
  "guidance": 0.70
}
"""


def _build_user_message(brand: str, brief: str) -> str:
    guide = _BRAND_GUIDES[brand]
    return (
        f"Brand: F3 {brand.capitalize()}\n"
        f"Tagline: {guide['tagline']}\n"
        f"Avatar: {guide['avatar']}\n"
        f"Brand vibe: {guide['vibe']}\n"
        f"Photography direction: {guide['photography']}\n"
        f"Palette hint: {guide['palette_hint']}\n\n"
        f"Creative brief from the team:\n\"{brief}\"\n\n"
        f"Write the PhotoRoom background prompt for this brief."
    )


def _call_claude(brand: str, brief: str) -> dict:
    """Ask Claude to generate background_prompt + negative_prompt + guidance.

    Returns parsed dict with those three keys.
    Raises ValueError on parse failure.
    """
    client = anthropic.Anthropic(api_key=config.anthropic_api_key)
    user_msg = _build_user_message(brand, brief)

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",  # fast + cheap for structured JSON
        max_tokens=512,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    raw = response.content[0].text.strip()

    # Strip any accidental markdown fences
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Claude returned non-JSON for spec generation: {exc}\nRaw: {raw[:300]}"
        ) from exc

    required = {"background_prompt", "negative_prompt", "guidance"}
    missing = required - set(data)
    if missing:
        raise ValueError(f"Claude response missing keys {missing}. Got: {list(data)}")

    return data


def generate_spec_from_brief(
    brand: str,
    brief: str,
    output_size: str = "1920x900",
    output_filename: str | None = None,
    main_image_url: str | None = None,
    requester: str | None = None,
) -> ImageSpec:
    """Generate a complete ImageSpec from a plain-English creative brief.

    Args:
        brand: "pure" | "mood" | "energy"
        brief: Plain-English description of the desired scene.
        output_size: "WIDTHxHEIGHT" string (default 1920x900 for hero banners).
        output_filename: Override the auto-generated filename.
        main_image_url: Override the default can image URL for this brand.
        requester: Slack user ID or name for metadata / audit trail.

    Returns:
        ImageSpec with destination=drive_review_folder (Drive upload, no Shopify wiring).

    Raises:
        ValueError: If brand is invalid or Claude returns malformed JSON.
        PhotoroomError: Propagated from the underlying client if called.
    """
    brand = brand.lower().strip()
    if brand not in _BRAND_GUIDES:
        raise ValueError(
            f"Unknown brand {brand!r}. Valid: {', '.join(sorted(_BRAND_GUIDES))}"
        )

    guide = _BRAND_GUIDES[brand]

    # Generate the background prompt via Claude
    log.info("spec_generator: calling Claude for brand=%s brief=%r", brand, brief[:80])
    llm_output = _call_claude(brand, brief)

    background_prompt: str = llm_output["background_prompt"]
    negative_prompt: str = llm_output["negative_prompt"]
    guidance_scale: float = float(llm_output.get("guidance", guide["default_guidance"]))
    guidance_scale = max(0.0, min(1.0, guidance_scale))  # clamp to [0, 1]

    # Build spec ID and filename
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    short_id = uuid.uuid4().hex[:6]
    spec_id = f"brief-{brand}-{ts}-{short_id}"

    if not output_filename:
        safe_brief = re.sub(r"[^a-z0-9]+", "-", brief.lower())[:40].strip("-")
        output_filename = f"f3-{brand}-{safe_brief}-{short_id}.png"

    # Can image URL
    image_url = main_image_url or _DEFAULT_CAN_URLS.get(brand, "")
    if not image_url:
        raise ValueError(
            f"No default can image URL configured for brand '{brand}'. "
            "Pass main_image_url explicitly."
        )

    return ImageSpec(
        spec_id=spec_id,
        brand=brand,
        scene_name=f"brief-{ts}",
        feature="ai_backgrounds",
        main_image=ImageRef(type="url", value=image_url),
        background=Background(
            prompt=background_prompt,
            guidance=BackgroundGuidance(scale=guidance_scale),
            negative_prompt=negative_prompt,
            seed=None,
        ),
        output=OutputSpec(
            format="PNG",
            size=output_size,
            filename=output_filename,
            alt_text=f"F3 {brand.capitalize()} — {brief[:80]}",
        ),
        destination=Destination(
            type="drive_review_folder",
            # folder_id resolved at run_spec() time from config
        ),
        metadata=SpecMetadata(
            requester=requester,
            tier=1,
        ),
    )
