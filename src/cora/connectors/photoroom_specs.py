"""
photoroom_specs.py — Pydantic schema for PhotoRoom image spec JSON.

This is the contract between Claude.ai Strategy (spec author) and Cora (executor).
Every spec is a self-contained JSON object that fully describes one image generation job.

Schema mirrors photoroom-orchestrator-spec-2026-05-25.md exactly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Nested models
# ---------------------------------------------------------------------------


class ImageRef(BaseModel):
    """Reference to an image by type + value."""

    type: Literal["shopify_file_url", "drive_file_id", "url"]
    value: str = Field(..., min_length=1)


class BackgroundGuidance(BaseModel):
    """Optional reference image + blend scale for background guidance."""

    image_ref: Optional[ImageRef] = None
    scale: float = Field(default=0.7, ge=0.0, le=1.0)


class Background(BaseModel):
    """PhotoRoom background generation parameters."""

    prompt: str = Field(..., min_length=1, description="Describe the desired background scene")
    guidance: BackgroundGuidance = Field(default_factory=BackgroundGuidance)
    negative_prompt: Optional[str] = None
    seed: Optional[int] = Field(default=None, ge=0)


class EditWithAI(BaseModel):
    """Multi-reference AI editing parameters (future capability)."""

    enabled: bool = False
    additional_images: list[ImageRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_additional_images(self) -> "EditWithAI":
        if self.enabled and len(self.additional_images) > 4:
            raise ValueError("edit_with_ai supports at most 4 additional_images")
        return self


class OutputSpec(BaseModel):
    """Output format and destination filename."""

    format: Literal["PNG", "JPG", "WEBP"] = "PNG"
    size: str = Field(
        default="1920x900",
        pattern=r"^\d+x\d+$",
        description="WIDTHxHEIGHT in pixels, e.g. '1920x900'",
    )
    filename: str = Field(..., min_length=1, description="Output filename, e.g. 'f3-pure-hero-01.png'")
    alt_text: str = Field(..., min_length=1, description="Shopify alt text for accessibility")


class ShopifyTarget(BaseModel):
    """Shopify-side wiring target."""

    section_id: Optional[str] = None
    template_file: Optional[str] = None
    product_handle: Optional[str] = None
    collection_handle: Optional[str] = None


class Destination(BaseModel):
    """Where the generated image should be wired after upload."""

    type: Literal[
        "shopify_file_only",
        "homepage_hero_section",
        "collection_hero",
        "pdp_hero",
    ]
    shopify_target: ShopifyTarget = Field(default_factory=ShopifyTarget)
    wire_strategy: Literal["replace_or_insert", "insert_only", "replace_only"] = "replace_or_insert"

    @model_validator(mode="after")
    def check_required_fields(self) -> "Destination":
        t = self.type
        st = self.shopify_target
        if t == "pdp_hero" and not st.product_handle:
            raise ValueError("pdp_hero destination requires shopify_target.product_handle")
        if t == "collection_hero" and not st.collection_handle:
            raise ValueError("collection_hero destination requires shopify_target.collection_handle")
        if t == "homepage_hero_section" and not st.section_id:
            raise ValueError("homepage_hero_section destination requires shopify_target.section_id")
        return self


class SpecMetadata(BaseModel):
    """Provenance + context for the spec (not passed to PhotoRoom)."""

    requester: Optional[str] = None
    strategy_chat_session: Optional[str] = None
    brand_lineage: Optional[str] = None
    tier: Optional[int] = Field(default=None, ge=1, le=3)


# ---------------------------------------------------------------------------
# Root model
# ---------------------------------------------------------------------------


VALID_BRANDS = {"pure", "mood", "energy"}


class ImageSpec(BaseModel):
    """
    Root image spec model.

    Validated before any PhotoRoom API call — invalid specs short-circuit
    with a Slack error reply rather than consuming an API credit.
    """

    spec_id: str = Field(..., min_length=1)
    brand: str = Field(..., description="One of: pure, mood, energy")
    scene_name: str = Field(..., min_length=1)
    feature: Literal["ai_backgrounds", "edit_with_ai"] = "ai_backgrounds"
    main_image: ImageRef
    background: Background
    edit_with_ai: EditWithAI = Field(default_factory=EditWithAI)
    output: OutputSpec
    destination: Destination
    metadata: SpecMetadata = Field(default_factory=SpecMetadata)

    @model_validator(mode="after")
    def check_brand(self) -> "ImageSpec":
        if self.brand not in VALID_BRANDS:
            raise ValueError(
                f"brand must be one of {sorted(VALID_BRANDS)}, got '{self.brand}'"
            )
        return self

    @model_validator(mode="after")
    def check_feature_consistency(self) -> "ImageSpec":
        if self.feature == "edit_with_ai" and not self.edit_with_ai.enabled:
            raise ValueError("feature='edit_with_ai' requires edit_with_ai.enabled=true")
        return self


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def validate_spec(raw: dict[str, Any]) -> ImageSpec:
    """
    Parse and validate a raw dict into an ImageSpec.

    Raises pydantic.ValidationError (with field-level detail) on invalid input.
    Callers should catch and relay the error message to Slack.
    """
    return ImageSpec.model_validate(raw)


def validate_spec_json(json_str: str) -> ImageSpec:
    """Parse a JSON string and validate it as an ImageSpec."""
    try:
        raw = json.loads(json_str)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Spec is not valid JSON: {exc}") from exc
    return validate_spec(raw)


def load_spec_file(path: str | Path) -> ImageSpec:
    """Load a spec from a .json file on disk."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Spec file not found: {p}")
    text = p.read_text(encoding="utf-8")
    return validate_spec_json(text)
