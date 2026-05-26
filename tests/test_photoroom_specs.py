"""
Tests for photoroom_specs.py — Pydantic schema + validation.
"""

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from cora.connectors.photoroom_specs import (
    BackgroundGuidance,
    Destination,
    ImageRef,
    ImageSpec,
    OutputSpec,
    SpecMetadata,
    load_spec_file,
    validate_spec,
    validate_spec_json,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "sample-specs"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_spec(**overrides) -> dict:
    """Return a minimal valid spec dict, with optional field overrides."""
    base = {
        "spec_id": "test-spec-001",
        "brand": "pure",
        "scene_name": "test-scene",
        "feature": "ai_backgrounds",
        "main_image": {"type": "url", "value": "https://example.com/can.png"},
        "background": {
            "prompt": "Sunlit park bench, morning light",
            "guidance": {"scale": 0.7},
        },
        "output": {
            "format": "PNG",
            "size": "1920x900",
            "filename": "test-output.png",
            "alt_text": "F3 Pure test image",
        },
        "destination": {
            "type": "shopify_file_only",
            "shopify_target": {},
            "wire_strategy": "replace_or_insert",
        },
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# ImageRef
# ---------------------------------------------------------------------------


class TestImageRef:
    def test_url_type(self):
        ref = ImageRef(type="url", value="https://example.com/img.png")
        assert ref.type == "url"

    def test_drive_type(self):
        ref = ImageRef(type="drive_file_id", value="1abc123xyz")
        assert ref.type == "drive_file_id"

    def test_shopify_file_url_type(self):
        ref = ImageRef(type="shopify_file_url", value="https://cdn.shopify.com/img.png")
        assert ref.type == "shopify_file_url"

    def test_invalid_type(self):
        with pytest.raises(ValidationError):
            ImageRef(type="s3_bucket", value="s3://bucket/key")  # type: ignore[arg-type]

    def test_empty_value_rejected(self):
        with pytest.raises(ValidationError):
            ImageRef(type="url", value="")


# ---------------------------------------------------------------------------
# BackgroundGuidance
# ---------------------------------------------------------------------------


class TestBackgroundGuidance:
    def test_defaults(self):
        g = BackgroundGuidance()
        assert g.scale == 0.7
        assert g.image_ref is None

    def test_scale_bounds(self):
        g = BackgroundGuidance(scale=0.0)
        assert g.scale == 0.0
        g2 = BackgroundGuidance(scale=1.0)
        assert g2.scale == 1.0

    def test_scale_out_of_bounds(self):
        with pytest.raises(ValidationError):
            BackgroundGuidance(scale=1.5)

    def test_with_image_ref(self):
        g = BackgroundGuidance(
            image_ref=ImageRef(type="drive_file_id", value="1abc"), scale=0.5
        )
        assert g.image_ref.type == "drive_file_id"


# ---------------------------------------------------------------------------
# Destination validation
# ---------------------------------------------------------------------------


class TestDestination:
    def test_shopify_file_only_no_targets_needed(self):
        d = Destination(type="shopify_file_only")
        assert d.type == "shopify_file_only"

    def test_pdp_hero_requires_product_handle(self):
        with pytest.raises(ValidationError, match="product_handle"):
            Destination(type="pdp_hero")

    def test_pdp_hero_ok_with_product_handle(self):
        d = Destination(
            type="pdp_hero",
            shopify_target={"product_handle": "f3-pure-strawberry"},
        )
        assert d.shopify_target.product_handle == "f3-pure-strawberry"

    def test_collection_hero_requires_collection_handle(self):
        with pytest.raises(ValidationError, match="collection_handle"):
            Destination(type="collection_hero")

    def test_homepage_hero_requires_section_id(self):
        with pytest.raises(ValidationError, match="section_id"):
            Destination(type="homepage_hero_section")

    def test_homepage_hero_ok(self):
        d = Destination(
            type="homepage_hero_section",
            shopify_target={"section_id": "pure-hero"},
        )
        assert d.shopify_target.section_id == "pure-hero"


# ---------------------------------------------------------------------------
# ImageSpec root model
# ---------------------------------------------------------------------------


class TestImageSpec:
    def test_valid_minimal_spec(self):
        spec = validate_spec(_minimal_spec())
        assert spec.spec_id == "test-spec-001"
        assert spec.brand == "pure"

    def test_invalid_brand(self):
        with pytest.raises(ValidationError, match="brand must be one of"):
            validate_spec(_minimal_spec(brand="cola"))

    def test_valid_brands(self):
        for brand in ("pure", "mood", "energy"):
            spec = validate_spec(_minimal_spec(brand=brand))
            assert spec.brand == brand

    def test_feature_edit_with_ai_requires_enabled(self):
        spec_dict = _minimal_spec(feature="edit_with_ai")
        # edit_with_ai.enabled is False by default — should raise
        with pytest.raises(ValidationError, match="edit_with_ai.enabled=true"):
            validate_spec(spec_dict)

    def test_feature_edit_with_ai_ok_when_enabled(self):
        spec_dict = _minimal_spec(feature="edit_with_ai")
        spec_dict["edit_with_ai"] = {"enabled": True, "additional_images": []}
        spec = validate_spec(spec_dict)
        assert spec.feature == "edit_with_ai"

    def test_edit_with_ai_too_many_images(self):
        spec_dict = _minimal_spec(feature="edit_with_ai")
        spec_dict["edit_with_ai"] = {
            "enabled": True,
            "additional_images": [
                {"type": "url", "value": f"https://example.com/{i}.png"}
                for i in range(5)
            ],
        }
        with pytest.raises(ValidationError, match="at most 4"):
            validate_spec(spec_dict)

    def test_output_size_pattern(self):
        spec_dict = _minimal_spec()
        spec_dict["output"]["size"] = "not-a-size"
        with pytest.raises(ValidationError):
            validate_spec(spec_dict)

    def test_output_size_valid_formats(self):
        for size in ("1920x900", "1200x1200", "800x600"):
            spec_dict = _minimal_spec()
            spec_dict["output"]["size"] = size
            spec = validate_spec(spec_dict)
            assert spec.output.size == size

    def test_background_prompt_required(self):
        spec_dict = _minimal_spec()
        spec_dict["background"]["prompt"] = ""
        with pytest.raises(ValidationError):
            validate_spec(spec_dict)


# ---------------------------------------------------------------------------
# validate_spec_json
# ---------------------------------------------------------------------------


class TestValidateSpecJson:
    def test_valid_json_string(self):
        spec_dict = _minimal_spec()
        spec = validate_spec_json(json.dumps(spec_dict))
        assert spec.spec_id == "test-spec-001"

    def test_invalid_json_raises_value_error(self):
        with pytest.raises(ValueError, match="not valid JSON"):
            validate_spec_json("{not: valid json}")

    def test_valid_json_invalid_schema_raises_validation_error(self):
        with pytest.raises(ValidationError):
            validate_spec_json(json.dumps({"spec_id": "x"}))  # missing required fields


# ---------------------------------------------------------------------------
# Fixture files
# ---------------------------------------------------------------------------


class TestFixtureFiles:
    @pytest.mark.parametrize(
        "fixture_name",
        [
            "f3-pure-hero-morning-walk-001.json",
            "f3-pure-pdp-hero-001.json",
            "f3-pure-collection-hero-001.json",
        ],
    )
    def test_fixture_parses_cleanly(self, fixture_name: str):
        path = FIXTURES_DIR / fixture_name
        assert path.exists(), f"Fixture file missing: {path}"
        spec = load_spec_file(path)
        assert spec.spec_id
        assert spec.brand == "pure"

    def test_load_spec_file_missing(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_spec_file(tmp_path / "no-such-file.json")
