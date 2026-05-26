"""
Tests for photoroom_client.py -- HTTP client, rate limiter, Shopify upload,
budget governance, Slack formatting.

All external HTTP calls are mocked. No live API credits consumed.
"""

from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cora.connectors.photoroom_client import (
    BATCH_HARD_CAP,
    COST_PER_IMAGE_USD,
    BatchResults,
    GenerateResult,
    PhotoroomAPIError,
    PhotoroomBudgetError,
    PhotoroomConfigError,
    PhotoroomError,
    PhotoroomRateLimiter,
    ShopifyUploadError,
    _api_key,
    _check_budget,
    _iso_week_key,
    _load_weekly_spend,
    _log_spend,
    batch_run,
    format_batch_results_for_slack,
    format_result_for_slack,
    generate_ai_background,
    run_spec,
    upload_to_shopify,
)
from cora.connectors.photoroom_specs import validate_spec
import cora.connectors.photoroom_client as _pc


# ---------------------------------------------------------------------------
# Config patching helpers
# ---------------------------------------------------------------------------
# Config is a frozen dataclass; we cannot monkeypatch individual fields.
# Instead we replace the module-level `config` in photoroom_client with a
# dataclasses.replace() copy that has the test values.


def _patch_config(monkeypatch, **overrides):
    """Replace photoroom_client.config with a copy that has the given overrides."""
    import cora.connectors.photoroom_client as pc
    new_cfg = dataclasses.replace(pc.config, **overrides)
    monkeypatch.setattr(pc, "config", new_cfg)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_spec_dict(**overrides) -> dict:
    base = {
        "spec_id": "test-001",
        "brand": "pure",
        "scene_name": "test-scene",
        "feature": "ai_backgrounds",
        "main_image": {"type": "url", "value": "https://example.com/can.png"},
        "background": {
            "prompt": "Sunny park",
            "guidance": {"scale": 0.7},
        },
        "output": {
            "format": "PNG",
            "size": "1920x900",
            "filename": "test-output.png",
            "alt_text": "Test alt text",
        },
        "destination": {
            "type": "shopify_file_only",
            "shopify_target": {},
        },
    }
    base.update(overrides)
    return base


def _make_spec(**overrides):
    return validate_spec(_minimal_spec_dict(**overrides))


# ---------------------------------------------------------------------------
# PhotoroomRateLimiter
# ---------------------------------------------------------------------------


class TestRateLimiter:
    def test_initial_state_allows_calls(self):
        limiter = PhotoroomRateLimiter(calls_per_min=5)
        for _ in range(5):
            limiter.wait_if_needed()
        assert len(limiter.history) == 5

    def test_history_bounded_by_maxlen(self):
        limiter = PhotoroomRateLimiter(calls_per_min=3)
        for _ in range(5):
            limiter.history.append(time.monotonic())
        assert len(limiter.history) == 3

    def test_expired_entries_are_pruned(self):
        limiter = PhotoroomRateLimiter(calls_per_min=2)
        old_time = time.monotonic() - 65
        limiter.history.append(old_time)
        limiter.history.append(old_time)
        # Old entries pruned -- new call appended, no sleep
        limiter.wait_if_needed()
        assert len(limiter.history) == 1


# ---------------------------------------------------------------------------
# Config / API key
# ---------------------------------------------------------------------------


class TestApiKey:
    def test_raises_config_error_if_missing(self, monkeypatch):
        _patch_config(monkeypatch, photoroom_api_key="")
        with pytest.raises(PhotoroomConfigError):
            _api_key()

    def test_returns_key_when_set(self, monkeypatch):
        _patch_config(monkeypatch, photoroom_api_key="test-key-123")
        assert _api_key() == "test-key-123"


# ---------------------------------------------------------------------------
# Budget governance
# ---------------------------------------------------------------------------


class TestBudgetGovernance:
    def test_log_and_load_spend(self, tmp_path, monkeypatch):
        spend_path = tmp_path / "photoroom-spend.jsonl"
        monkeypatch.setattr("cora.connectors.photoroom_client._SPEND_LOG_PATH", spend_path)
        monkeypatch.setattr("cora.connectors.photoroom_client._weekly_spend_cache", {})
        week = _iso_week_key()
        _log_spend({"week": week, "status": "ok", "cost_usd": 0.10})
        _log_spend({"week": week, "status": "ok", "cost_usd": 0.10})
        spend = _load_weekly_spend()
        assert abs(spend - 0.20) < 1e-9

    def test_error_entries_not_counted(self, tmp_path, monkeypatch):
        spend_path = tmp_path / "photoroom-spend.jsonl"
        monkeypatch.setattr("cora.connectors.photoroom_client._SPEND_LOG_PATH", spend_path)
        monkeypatch.setattr("cora.connectors.photoroom_client._weekly_spend_cache", {})
        week = _iso_week_key()
        _log_spend({"week": week, "status": "error", "cost_usd": 0.10})
        spend = _load_weekly_spend()
        assert spend == 0.0

    def test_budget_check_passes_under_cap(self, tmp_path, monkeypatch):
        spend_path = tmp_path / "photoroom-spend.jsonl"
        monkeypatch.setattr("cora.connectors.photoroom_client._SPEND_LOG_PATH", spend_path)
        monkeypatch.setattr("cora.connectors.photoroom_client._weekly_spend_cache", {})
        _check_budget(50.0)

    def test_budget_check_raises_at_cap(self, tmp_path, monkeypatch):
        spend_path = tmp_path / "photoroom-spend.jsonl"
        monkeypatch.setattr("cora.connectors.photoroom_client._SPEND_LOG_PATH", spend_path)
        monkeypatch.setattr("cora.connectors.photoroom_client._weekly_spend_cache", {})
        week = _iso_week_key()
        for _ in range(500):
            _log_spend({"week": week, "status": "ok", "cost_usd": 0.10})
        with pytest.raises(PhotoroomBudgetError, match="Weekly budget hit"):
            _check_budget(50.0)


# ---------------------------------------------------------------------------
# generate_ai_background -- mocked HTTP
# ---------------------------------------------------------------------------


class TestGenerateAiBackground:
    @patch("cora.connectors.photoroom_client.httpx.get")
    @patch("cora.connectors.photoroom_client.httpx.post")
    def test_success_returns_bytes(self, mock_post, mock_get, monkeypatch):
        _patch_config(
            monkeypatch,
            photoroom_api_key="test-key",
            photoroom_use_sandbox=False,
            photoroom_base_url="https://image-api.photoroom.com/v2",
        )
        mock_get.return_value = MagicMock(status_code=200, content=b"FAKEIMGBYTES", is_success=True)
        mock_get.return_value.raise_for_status = lambda: None
        mock_post.return_value = MagicMock(status_code=200, content=b"FAKEPNGBYTES", is_success=True)
        spec = _make_spec()
        result = generate_ai_background(spec)
        assert result == b"FAKEPNGBYTES"
        assert mock_post.called

    @patch("cora.connectors.photoroom_client.httpx.get")
    @patch("cora.connectors.photoroom_client.httpx.post")
    def test_401_raises_api_error(self, mock_post, mock_get, monkeypatch):
        _patch_config(
            monkeypatch,
            photoroom_api_key="bad-key",
            photoroom_use_sandbox=False,
            photoroom_base_url="https://image-api.photoroom.com/v2",
        )
        mock_get.return_value = MagicMock(status_code=200, content=b"FAKEIMGBYTES")
        mock_get.return_value.raise_for_status = lambda: None
        mock_post.return_value = MagicMock(status_code=401, text="Unauthorized", is_success=False)
        spec = _make_spec()
        with pytest.raises(PhotoroomAPIError) as exc_info:
            generate_ai_background(spec)
        assert exc_info.value.status == 401

    @patch("cora.connectors.photoroom_client.httpx.get")
    @patch("cora.connectors.photoroom_client.httpx.post")
    def test_500_raises_api_error(self, mock_post, mock_get, monkeypatch):
        _patch_config(
            monkeypatch,
            photoroom_api_key="test-key",
            photoroom_use_sandbox=False,
            photoroom_base_url="https://image-api.photoroom.com/v2",
        )
        mock_get.return_value = MagicMock(status_code=200, content=b"FAKEIMGBYTES")
        mock_get.return_value.raise_for_status = lambda: None
        mock_post.return_value = MagicMock(status_code=500, text="Internal Server Error", is_success=False)
        spec = _make_spec()
        with pytest.raises(PhotoroomAPIError) as exc_info:
            generate_ai_background(spec)
        assert exc_info.value.status == 500

    def test_missing_api_key_raises_config_error(self, monkeypatch):
        _patch_config(monkeypatch, photoroom_api_key="")
        spec = _make_spec()
        with pytest.raises(PhotoroomConfigError):
            generate_ai_background(spec)

    @patch("cora.connectors.photoroom_client.httpx.get")
    @patch("cora.connectors.photoroom_client.httpx.post")
    def test_sandbox_uses_different_url(self, mock_post, mock_get, monkeypatch):
        _patch_config(monkeypatch, photoroom_api_key="test-key", photoroom_use_sandbox=True)
        mock_get.return_value = MagicMock(status_code=200, content=b"BYTES")
        mock_get.return_value.raise_for_status = lambda: None
        mock_post.return_value = MagicMock(status_code=200, content=b"SANDBOXPNG", is_success=True)
        spec = _make_spec()
        generate_ai_background(spec)
        call_url = mock_post.call_args[0][0]
        assert "sdk.photoroom.com" in call_url

    @patch("cora.connectors.photoroom_client.httpx.get")
    @patch("cora.connectors.photoroom_client.httpx.post")
    def test_with_guidance_image_ref_sends_second_file(self, mock_post, mock_get, monkeypatch):
        _patch_config(
            monkeypatch,
            photoroom_api_key="test-key",
            photoroom_use_sandbox=False,
            photoroom_base_url="https://image-api.photoroom.com/v2",
        )
        mock_get.return_value = MagicMock(status_code=200, content=b"FAKEIMGBYTES")
        mock_get.return_value.raise_for_status = lambda: None
        mock_post.return_value = MagicMock(status_code=200, content=b"PNG", is_success=True)
        spec_dict = _minimal_spec_dict()
        spec_dict["background"]["guidance"]["image_ref"] = {
            "type": "url",
            "value": "https://example.com/ref.png",
        }
        spec = validate_spec(spec_dict)
        generate_ai_background(spec)
        files_arg = mock_post.call_args.kwargs["files"]
        assert "background.guidance.imageFile" in files_arg


# ---------------------------------------------------------------------------
# upload_to_shopify -- mocked Shopify graphql
# ---------------------------------------------------------------------------


class TestUploadToShopify:
    @patch("cora.connectors.photoroom_client.shopify_client.graphql")
    def test_success_returns_gid(self, mock_graphql):
        mock_graphql.return_value = {
            "data": {
                "fileCreate": {
                    "files": [{"id": "gid://shopify/MediaImage/123", "alt": "test", "fileStatus": "READY"}],
                    "userErrors": [],
                }
            }
        }
        gid = upload_to_shopify(b"PNG_BYTES", "Test alt", "test.png")
        assert gid == "gid://shopify/MediaImage/123"

    @patch("cora.connectors.photoroom_client.shopify_client.graphql")
    def test_user_errors_raise_shopify_upload_error(self, mock_graphql):
        mock_graphql.return_value = {
            "data": {
                "fileCreate": {
                    "files": [],
                    "userErrors": [{"field": "originalSource", "message": "Invalid base64"}],
                }
            }
        }
        with pytest.raises(ShopifyUploadError, match="userErrors"):
            upload_to_shopify(b"BAD", "alt", "bad.png")

    @patch("cora.connectors.photoroom_client.shopify_client.graphql")
    def test_empty_files_raises_shopify_upload_error(self, mock_graphql):
        mock_graphql.return_value = {
            "data": {
                "fileCreate": {
                    "files": [],
                    "userErrors": [],
                }
            }
        }
        with pytest.raises(ShopifyUploadError, match="no files"):
            upload_to_shopify(b"BYTES", "alt", "empty.png")

    @patch("cora.connectors.photoroom_client.shopify_client.graphql")
    def test_base64_encoding_is_correct(self, mock_graphql):
        import base64
        mock_graphql.return_value = {
            "data": {
                "fileCreate": {
                    "files": [{"id": "gid://shopify/MediaImage/1", "alt": "t", "fileStatus": "READY"}],
                    "userErrors": [],
                }
            }
        }
        test_bytes = b"\x89PNG\r\n\x1a\nFAKEDATA"
        upload_to_shopify(test_bytes, "alt", "img.png")
        call_vars = mock_graphql.call_args[0][1]
        original_source = call_vars["files"][0]["originalSource"]
        assert original_source.startswith("data:image/png;base64,")
        encoded_part = original_source.replace("data:image/png;base64,", "")
        decoded = base64.b64decode(encoded_part)
        assert decoded == test_bytes


# ---------------------------------------------------------------------------
# run_spec -- dry_run mode
# ---------------------------------------------------------------------------


class TestRunSpecDryRun:
    def test_dry_run_returns_dry_run_status(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cora.connectors.photoroom_client._SPEND_LOG_PATH", tmp_path / "spend.jsonl")
        monkeypatch.setattr("cora.connectors.photoroom_client._weekly_spend_cache", {})
        _patch_config(monkeypatch, photoroom_weekly_budget_usd=50.0)
        spec = _make_spec()
        result = run_spec(spec, dry_run=True)
        assert result.status == "dry_run"
        assert result.cost_usd == COST_PER_IMAGE_USD

    def test_dry_run_does_not_call_photoroom(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cora.connectors.photoroom_client._SPEND_LOG_PATH", tmp_path / "spend.jsonl")
        monkeypatch.setattr("cora.connectors.photoroom_client._weekly_spend_cache", {})
        _patch_config(monkeypatch, photoroom_weekly_budget_usd=50.0)
        spec = _make_spec()
        with patch("cora.connectors.photoroom_client.generate_ai_background") as mock_gen:
            run_spec(spec, dry_run=True)
            mock_gen.assert_not_called()

    def test_dry_run_respects_budget_cap(self, tmp_path, monkeypatch):
        spend_path = tmp_path / "spend.jsonl"
        monkeypatch.setattr("cora.connectors.photoroom_client._SPEND_LOG_PATH", spend_path)
        monkeypatch.setattr("cora.connectors.photoroom_client._weekly_spend_cache", {})
        _patch_config(monkeypatch, photoroom_weekly_budget_usd=0.05)
        week = _iso_week_key()
        _log_spend({"week": week, "status": "ok", "cost_usd": 0.10})
        spec = _make_spec()
        with pytest.raises(PhotoroomBudgetError):
            run_spec(spec, dry_run=True)


# ---------------------------------------------------------------------------
# batch_run
# ---------------------------------------------------------------------------


class TestBatchRun:
    def test_batch_cap_enforced(self):
        specs = [_make_spec() for _ in range(BATCH_HARD_CAP + 1)]
        with pytest.raises(PhotoroomError, match="hard cap"):
            batch_run(specs)

    def test_batch_at_cap_limit_ok(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cora.connectors.photoroom_client._SPEND_LOG_PATH", tmp_path / "spend.jsonl")
        monkeypatch.setattr("cora.connectors.photoroom_client._weekly_spend_cache", {})
        _patch_config(monkeypatch, photoroom_weekly_budget_usd=50.0)
        specs = [_make_spec() for _ in range(3)]
        with patch("cora.connectors.photoroom_client.run_spec") as mock_run:
            mock_run.return_value = GenerateResult(spec_id="test-001", status="ok", cost_usd=0.10)
            results = batch_run(specs)
        assert results.ok_count == 3
        assert mock_run.call_count == 3

    def test_batch_continues_after_per_spec_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cora.connectors.photoroom_client._SPEND_LOG_PATH", tmp_path / "spend.jsonl")
        monkeypatch.setattr("cora.connectors.photoroom_client._weekly_spend_cache", {})
        _patch_config(monkeypatch, photoroom_weekly_budget_usd=50.0)
        specs = [_make_spec() for _ in range(3)]
        call_count = [0]

        def mock_run(spec, dry_run=False):
            call_count[0] += 1
            if call_count[0] == 2:
                raise PhotoroomAPIError(500, "Server error")
            return GenerateResult(spec_id=spec.spec_id, status="ok", cost_usd=0.10)

        with patch("cora.connectors.photoroom_client.run_spec", side_effect=mock_run):
            results = batch_run(specs)

        assert results.ok_count == 2
        assert results.error_count == 1

    def test_budget_error_stops_batch(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cora.connectors.photoroom_client._SPEND_LOG_PATH", tmp_path / "spend.jsonl")
        monkeypatch.setattr("cora.connectors.photoroom_client._weekly_spend_cache", {})
        _patch_config(monkeypatch, photoroom_weekly_budget_usd=50.0)
        specs = [_make_spec() for _ in range(3)]

        def mock_run(spec, dry_run=False):
            raise PhotoroomBudgetError("Budget hit")

        with patch("cora.connectors.photoroom_client.run_spec", side_effect=mock_run):
            with pytest.raises(PhotoroomBudgetError):
                batch_run(specs)


# ---------------------------------------------------------------------------
# Slack formatting -- source opacity
# ---------------------------------------------------------------------------


class TestSlackFormatting:
    def test_dry_run_format(self, monkeypatch):
        _patch_config(monkeypatch, photoroom_weekly_budget_usd=50.0)
        result = GenerateResult(
            spec_id="test-001",
            status="dry_run",
            cost_usd=0.10,
            cumulative_weekly_usd=1.50,
        )
        text = format_result_for_slack(result)
        assert "dry_run=false" in text
        assert "$0.10" in text
        assert "PhotoRoom" not in text
        assert "Shopify" not in text

    def test_ok_format(self, monkeypatch):
        _patch_config(monkeypatch, photoroom_weekly_budget_usd=50.0)
        result = GenerateResult(
            spec_id="test-002",
            status="ok",
            shopify_file_gid="gid://shopify/MediaImage/999",
            cost_usd=0.10,
            cumulative_weekly_usd=4.30,
            duration_ms=4250,
        )
        text = format_result_for_slack(result)
        assert "test-002" in text
        assert "$0.10" in text
        assert "4,250ms" in text
        assert "PhotoRoom" not in text
        assert "Shopify" not in text
        assert "gid://" not in text

    def test_error_format(self):
        result = GenerateResult(
            spec_id="test-003",
            status="error",
            error="API call failed",
        )
        text = format_result_for_slack(result)
        assert "test-003" in text
        assert "failed" in text.lower() or "error" in text.lower() or "failed" in text or chr(0x274C) in text

    def test_batch_format_ok_and_errors(self):
        results = BatchResults(
            results=[
                GenerateResult(spec_id="s1", status="ok", cost_usd=0.10),
                GenerateResult(spec_id="s2", status="ok", cost_usd=0.10),
                GenerateResult(spec_id="s3", status="error", error="timeout"),
            ]
        )
        text = format_batch_results_for_slack(results)
        assert "2 ok" in text
        assert "1 error" in text
        assert "$0.20" in text
        assert "PhotoRoom" not in text
        assert "Shopify" not in text
