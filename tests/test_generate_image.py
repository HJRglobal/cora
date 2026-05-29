"""Tests for tools/generate_image.py — Slack handler for f3_generate_image.

All tests are pure-Python (no real API calls, no Drive credentials needed).
PhotoRoom client and Drive helpers are fully mocked.

Run: .venv\\Scripts\\python.exe -m pytest tests/test_generate_image.py -v
"""

from __future__ import annotations

import json
import pytest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

VALID_SPEC_DICT = {
    "spec_id": "test-pure-hero-001",
    "brand": "pure",
    "scene_name": "morning-walk-test",
    "feature": "ai_backgrounds",
    "main_image": {"type": "url", "value": "https://example.com/can.png"},
    "background": {
        "prompt": "Sunny morning park, golden hour, bokeh background",
        "guidance": {"scale": 0.7},
        "negative_prompt": "dark, indoors",
        "seed": None,
    },
    "output": {
        "format": "PNG",
        "size": "1920x900",
        "filename": "test-pure-hero.png",
        "alt_text": "Test hero",
    },
    "destination": {
        "type": "shopify_file_only",
        "shopify_target": {},
    },
    "metadata": {
        "requester": "test",
        "tier": 1,
    },
}

VALID_SPEC_JSON = json.dumps(VALID_SPEC_DICT)


@pytest.fixture
def mock_run_spec():
    """Patch photoroom_client.run_spec and _api_key to return a successful GenerateResult."""
    from cora.connectors import photoroom_client as pc
    result = pc.GenerateResult(
        spec_id="test-pure-hero-001",
        status="ok",
        shopify_file_gid="gid://shopify/MediaImage/123",
        cost_usd=0.10,
        error=None,
    )
    with patch.object(pc, "_api_key", return_value="test-api-key"), \
         patch.object(pc, "run_spec", return_value=result) as mock:
        yield mock


@pytest.fixture
def mock_batch_run():
    """Patch photoroom_client.batch_run and _api_key to return a successful BatchResults."""
    from cora.connectors import photoroom_client as pc
    results = pc.BatchResults(
        results=[
            pc.GenerateResult(
                spec_id="spec-1",
                status="ok",
                shopify_file_gid="gid://shopify/MediaImage/1",
                cost_usd=0.10,
                error=None,
            ),
            pc.GenerateResult(
                spec_id="spec-2",
                status="ok",
                shopify_file_gid="gid://shopify/MediaImage/2",
                cost_usd=0.10,
                error=None,
            ),
        ],
    )
    with patch.object(pc, "_api_key", return_value="test-api-key"), \
         patch.object(pc, "batch_run", return_value=results) as mock:
        yield mock


# ---------------------------------------------------------------------------
# Entity scope guard
# ---------------------------------------------------------------------------

class TestEntityScopeGuard:
    def test_lex_channel_blocked(self):
        from cora.tools.generate_image import handle_f3_generate_image
        result = handle_f3_generate_image("U123", "LEX", {"spec": VALID_SPEC_DICT})
        assert "only available in F3" in result.lower() or "f3 channels" in result.lower()

    def test_osn_channel_blocked(self):
        from cora.tools.generate_image import handle_f3_generate_image
        result = handle_f3_generate_image("U123", "OSN", {"spec": VALID_SPEC_DICT})
        assert "only available in F3" in result.lower() or "f3 channels" in result.lower()

    def test_f3e_channel_allowed(self, mock_run_spec):
        from cora.tools.generate_image import handle_f3_generate_image
        result = handle_f3_generate_image("U123", "F3E", {"spec": VALID_SPEC_DICT})
        # Should not be a scope-block message
        assert "only available" not in result.lower()

    def test_fndr_channel_allowed(self, mock_run_spec):
        from cora.tools.generate_image import handle_f3_generate_image
        result = handle_f3_generate_image("U123", "FNDR", {"spec": VALID_SPEC_DICT})
        assert "only available" not in result.lower()

    def test_batch_lex_blocked(self):
        from cora.tools.generate_image import handle_f3_batch_image_run
        result = handle_f3_batch_image_run("U123", "LEX", {"spec_folder_drive_id": "abc"})
        assert "only available" in result.lower()


# ---------------------------------------------------------------------------
# Input validation — f3_generate_image
# ---------------------------------------------------------------------------

class TestGenerateImageInputValidation:
    def test_no_spec_no_drive_id_returns_error(self):
        from cora.tools.generate_image import handle_f3_generate_image
        result = handle_f3_generate_image("U123", "F3E", {})
        assert "no spec provided" in result.lower()

    def test_both_spec_and_drive_id_returns_error(self):
        from cora.tools.generate_image import handle_f3_generate_image
        result = handle_f3_generate_image(
            "U123", "F3E",
            {"spec": VALID_SPEC_DICT, "spec_drive_file_id": "some-id"},
        )
        assert "not both" in result.lower()

    def test_invalid_spec_dict_returns_validation_error(self):
        from cora.tools.generate_image import handle_f3_generate_image
        bad_spec = {"brand": "unknown-brand", "spec_id": "x"}
        result = handle_f3_generate_image("U123", "F3E", {"spec": bad_spec})
        assert "validation" in result.lower() or "error" in result.lower()

    def test_missing_required_spec_fields_returns_error(self):
        from cora.tools.generate_image import handle_f3_generate_image
        result = handle_f3_generate_image("U123", "F3E", {"spec": {"brand": "pure"}})
        assert "validation" in result.lower() or "error" in result.lower()


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_valid_spec_no_api_call(self):
        from cora.tools.generate_image import handle_f3_generate_image
        from cora.connectors import photoroom_client as pc
        with patch.object(pc, "run_spec") as mock_run:
            result = handle_f3_generate_image(
                "U123", "F3E",
                {"spec": VALID_SPEC_DICT, "dry_run": True},
            )
            mock_run.assert_not_called()
        assert "dry run" in result.lower()
        assert "$0.10" in result
        assert "test-pure-hero-001" in result

    def test_dry_run_batch_no_api_call(self):
        from cora.tools.generate_image import handle_f3_batch_image_run
        from cora.connectors import photoroom_client as pc
        mock_specs = [(VALID_SPEC_DICT, "spec1.json"), (VALID_SPEC_DICT, "spec2.json")]
        with patch("cora.tools.generate_image._download_drive_folder_specs", return_value=mock_specs), \
             patch.object(pc, "batch_run") as mock_batch:
            result = handle_f3_batch_image_run(
                "U123", "F3E",
                {"spec_folder_drive_id": "folder-123", "dry_run": True},
            )
            mock_batch.assert_not_called()
        assert "dry run" in result.lower()
        assert "$0.20" in result


# ---------------------------------------------------------------------------
# Drive file ID loading
# ---------------------------------------------------------------------------

class TestDriveFileLoading:
    def test_drive_file_id_loads_spec(self, mock_run_spec):
        from cora.tools.generate_image import handle_f3_generate_image
        with patch("cora.tools.generate_image._download_drive_json", return_value=VALID_SPEC_DICT) as mock_dl:
            result = handle_f3_generate_image(
                "U123", "F3E",
                {"spec_drive_file_id": "drive-file-id-123"},
            )
            mock_dl.assert_called_once_with("drive-file-id-123")
        # Should not be an error message
        assert "could not load" not in result.lower()

    def test_drive_download_failure_returns_error(self):
        from cora.tools.generate_image import handle_f3_generate_image
        with patch(
            "cora.tools.generate_image._download_drive_json",
            side_effect=ValueError("HTTP 404: File not found"),
        ):
            result = handle_f3_generate_image(
                "U123", "F3E",
                {"spec_drive_file_id": "bad-id"},
            )
        assert "could not load spec" in result.lower()
        assert "404" in result

    def test_drive_folder_empty_returns_error(self):
        from cora.tools.generate_image import handle_f3_batch_image_run
        with patch(
            "cora.tools.generate_image._download_drive_folder_specs",
            side_effect=ValueError("No .json spec files found"),
        ):
            result = handle_f3_batch_image_run(
                "U123", "F3E",
                {"spec_folder_drive_id": "empty-folder"},
            )
        assert "could not load" in result.lower() or "no .json" in result.lower()


# ---------------------------------------------------------------------------
# Successful generation
# ---------------------------------------------------------------------------

class TestSuccessfulGeneration:
    def test_successful_run_returns_slack_message(self, mock_run_spec):
        from cora.tools.generate_image import handle_f3_generate_image
        result = handle_f3_generate_image("U123", "F3E", {"spec": VALID_SPEC_DICT})
        assert isinstance(result, str)
        assert len(result) > 10

    def test_result_is_source_opaque(self, mock_run_spec):
        from cora.tools.generate_image import handle_f3_generate_image
        result = handle_f3_generate_image("U123", "F3E", {"spec": VALID_SPEC_DICT})
        # Must not mention PhotoRoom or raw Shopify GIDs
        assert "photoroom" not in result.lower()
        assert "shopify" not in result.lower()
        assert "sk_pr_" not in result.lower()

    def test_batch_success_returns_summary(self, mock_batch_run):
        from cora.tools.generate_image import handle_f3_batch_image_run
        with patch(
            "cora.tools.generate_image._download_drive_folder_specs",
            return_value=[(VALID_SPEC_DICT, "spec1.json"), (VALID_SPEC_DICT, "spec2.json")],
        ):
            result = handle_f3_batch_image_run(
                "U123", "F3E",
                {"spec_folder_drive_id": "folder-123"},
            )
        assert isinstance(result, str)
        assert len(result) > 10

    def test_batch_result_is_source_opaque(self, mock_batch_run):
        from cora.tools.generate_image import handle_f3_batch_image_run
        with patch(
            "cora.tools.generate_image._download_drive_folder_specs",
            return_value=[(VALID_SPEC_DICT, "spec1.json"), (VALID_SPEC_DICT, "spec2.json")],
        ):
            result = handle_f3_batch_image_run(
                "U123", "F3E",
                {"spec_folder_drive_id": "folder-123"},
            )
        assert "photoroom" not in result.lower()
        assert "shopify" not in result.lower()


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    def test_budget_error_surfaced_cleanly(self):
        from cora.tools.generate_image import handle_f3_generate_image
        from cora.connectors.photoroom_client import PhotoroomBudgetError
        with patch("cora.tools.generate_image.run_spec", side_effect=PhotoroomBudgetError("cap hit")):
            result = handle_f3_generate_image("U123", "F3E", {"spec": VALID_SPEC_DICT})
        assert "budget" in result.lower() or "cap" in result.lower()

    def test_config_error_surfaced_cleanly(self):
        from cora.tools.generate_image import handle_f3_generate_image
        from cora.connectors.photoroom_client import PhotoroomConfigError
        with patch("cora.tools.generate_image.run_spec", side_effect=PhotoroomConfigError("no key")):
            result = handle_f3_generate_image("U123", "F3E", {"spec": VALID_SPEC_DICT})
        assert "not configured" in result.lower() or "config" in result.lower()

    def test_photoroom_api_error_surfaced_cleanly(self):
        from cora.tools.generate_image import handle_f3_generate_image
        from cora.connectors.photoroom_client import PhotoroomError
        with patch("cora.tools.generate_image.run_spec", side_effect=PhotoroomError("500 server error")):
            result = handle_f3_generate_image("U123", "F3E", {"spec": VALID_SPEC_DICT})
        assert "failed" in result.lower() or "error" in result.lower()

    def test_unexpected_exception_handled(self):
        from cora.tools.generate_image import handle_f3_generate_image
        with patch("cora.tools.generate_image.run_spec", side_effect=RuntimeError("unexpected")):
            result = handle_f3_generate_image("U123", "F3E", {"spec": VALID_SPEC_DICT})
        assert "unexpected" in result.lower() or "error" in result.lower()

    def test_batch_missing_folder_id(self):
        from cora.tools.generate_image import handle_f3_batch_image_run
        result = handle_f3_batch_image_run("U123", "F3E", {})
        assert "missing" in result.lower() or "required" in result.lower()

    def test_batch_all_specs_invalid_returns_error(self):
        from cora.tools.generate_image import handle_f3_batch_image_run
        bad_specs = [
            ({"brand": "invalid-brand"}, "bad1.json"),
            ({"not": "a spec"}, "bad2.json"),
        ]
        with patch(
            "cora.tools.generate_image._download_drive_folder_specs",
            return_value=bad_specs,
        ):
            result = handle_f3_batch_image_run(
                "U123", "F3E",
                {"spec_folder_drive_id": "folder-123"},
            )
        assert "no valid specs" in result.lower() or "validation" in result.lower()

    def test_batch_budget_error_before_run(self):
        from cora.tools.generate_image import handle_f3_batch_image_run
        from cora.connectors.photoroom_client import PhotoroomBudgetError
        with patch(
            "cora.tools.generate_image._download_drive_folder_specs",
            return_value=[(VALID_SPEC_DICT, "spec1.json")],
        ), patch("cora.tools.generate_image.batch_run", side_effect=PhotoroomBudgetError("cap")):
            result = handle_f3_batch_image_run(
                "U123", "F3E",
                {"spec_folder_drive_id": "folder-123"},
            )
        assert "budget" in result.lower() or "cap" in result.lower()


# ---------------------------------------------------------------------------
# tool_dispatch wiring
# ---------------------------------------------------------------------------

class TestToolDispatchWiring:
    def test_f3_generate_image_in_tool_functions(self):
        from cora.tools.tool_dispatch import _TOOL_FUNCTIONS
        assert "f3_generate_image" in _TOOL_FUNCTIONS

    def test_f3_batch_image_run_in_tool_functions(self):
        from cora.tools.tool_dispatch import _TOOL_FUNCTIONS
        assert "f3_batch_image_run" in _TOOL_FUNCTIONS

    def test_f3_generate_image_in_tool_definitions(self):
        from cora.tools.tool_dispatch import TOOL_DEFINITIONS
        names = [t["name"] for t in TOOL_DEFINITIONS]
        assert "f3_generate_image" in names

    def test_f3_batch_image_run_in_tool_definitions(self):
        from cora.tools.tool_dispatch import TOOL_DEFINITIONS
        names = [t["name"] for t in TOOL_DEFINITIONS]
        assert "f3_batch_image_run" in names

    def test_dispatch_routes_f3_generate_image(self, mock_run_spec):
        from cora.tools.tool_dispatch import dispatch
        result = dispatch(
            "f3_generate_image",
            {"spec": VALID_SPEC_DICT},
            slack_user_id="U123",
            entity="F3E",
        )
        assert isinstance(result, str)
        assert len(result) > 5
