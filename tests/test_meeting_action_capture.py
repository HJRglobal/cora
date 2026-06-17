"""Tests for fireflies_action_extractor -- Meeting -> Action Auto-Capture.

Coverage:
  - Watermark read/write (missing file, corrupt file, valid file)
  - Email -> Asana GID resolution (match by attendee, fallback, no match)
  - Haiku action item parsing (happy path, empty text, bad JSON, API error)
  - PHI guardrail (LEX always skipped, clinical keywords skipped)
  - Entity -> channel routing
  - run_action_capture dry_run flow (no external calls)
  - run_action_capture error handling (Fireflies API failure, Asana failure)
  - Watermark advance logic
  - Slack post logic (token missing, API ok, API error)
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora.connectors import fireflies_action_extractor as fae


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_transcript(
    title: str = "F3 Weekly Review",
    action_items: str = "Tommy: send proposal by Friday",
    attendees: list[dict] | None = None,
    date_ts: int | None = None,
) -> dict[str, Any]:
    if attendees is None:
        attendees = [
            {"displayName": "Tommy Anderson", "email": "tommy@hjrglobal.com"},
            {"displayName": "Harrison Rogers", "email": "harrison@hjrglobal.com"},
        ]
    if date_ts is None:
        date_ts = int(time.time()) - 3600
    return {
        "id": "abc123",
        "title": title,
        "date": date_ts,
        "duration": 3600,
        "summary": {
            "action_items": action_items,
            "overview": "Weekly review of F3 operations",
        },
        "meeting_attendees": attendees,
    }


_MOCK_ASANA_MAP = {
    "users": [
        {
            "slack_user_id": "U001",
            "asana_user_gid": "1111111111111111",
            "asana_email": "tommy@hjrglobal.com",
            "display_name": "Tommy Anderson",
            "email_aliases": ["tommy@f3energy.com"],
        },
        {
            "slack_user_id": "U002",
            "asana_user_gid": "2222222222222222",
            "asana_email": "harrison@hjrglobal.com",
            "display_name": "Harrison Rogers",
        },
    ]
}

_MOCK_PARSED_TASKS = [
    {"task": "Send proposal to client", "assignee_name": "Tommy", "due_mention": "by Friday"},
    {"task": "Update inventory", "assignee_name": None, "due_mention": None},
]


# ---------------------------------------------------------------------------
# Watermark tests
# ---------------------------------------------------------------------------

class TestWatermark:
    def test_read_watermark_missing_file(self, tmp_path):
        """Missing watermark file returns default 24h-ago timestamp and empty id set."""
        with patch.object(fae, "_WATERMARK_PATH", tmp_path / "missing.json"):
            ts, ids = fae._read_watermark()
        assert ts > 0
        assert ts < int(time.time())
        assert ts >= int(time.time()) - (25 * 3600)  # within 25h of now
        assert ids == set()

    def test_read_watermark_valid_file(self, tmp_path):
        """Valid watermark file returns stored timestamp and IDs."""
        wpath = tmp_path / "watermark.json"
        expected_ts = 1700000000
        wpath.write_text(json.dumps({
            "last_processed_ts": expected_ts,
            "processed_ids": ["id1", "id2"],
        }), encoding="utf-8")
        with patch.object(fae, "_WATERMARK_PATH", wpath):
            ts, ids = fae._read_watermark()
        assert ts == expected_ts
        assert ids == {"id1", "id2"}

    def test_read_watermark_corrupt_file(self, tmp_path):
        """Corrupt watermark file falls back to default."""
        wpath = tmp_path / "watermark.json"
        wpath.write_text("not valid json", encoding="utf-8")
        with patch.object(fae, "_WATERMARK_PATH", wpath):
            ts, ids = fae._read_watermark()
        assert ts > 0
        assert ids == set()

    def test_write_watermark_creates_dirs(self, tmp_path):
        """Write watermark creates parent directories."""
        wpath = tmp_path / "subdir" / "nested" / "watermark.json"
        with patch.object(fae, "_WATERMARK_PATH", wpath):
            fae._write_watermark(1700000000, {"abc", "def"})
        assert wpath.exists()
        data = json.loads(wpath.read_text())
        assert data["last_processed_ts"] == 1700000000
        assert set(data["processed_ids"]) == {"abc", "def"}

    def test_write_watermark_updates_existing(self, tmp_path):
        """Write watermark overwrites existing file."""
        wpath = tmp_path / "watermark.json"
        wpath.write_text(json.dumps({"last_processed_ts": 1000}), encoding="utf-8")
        with patch.object(fae, "_WATERMARK_PATH", wpath):
            fae._write_watermark(2000, {"x"})
        data = json.loads(wpath.read_text())
        assert data["last_processed_ts"] == 2000
        assert data["processed_ids"] == ["x"]

    def test_write_watermark_caps_ids_at_200(self, tmp_path):
        """Write watermark keeps only last 200 IDs."""
        wpath = tmp_path / "watermark.json"
        big_set = {f"id{i}" for i in range(250)}
        with patch.object(fae, "_WATERMARK_PATH", wpath):
            fae._write_watermark(1000, big_set)
        data = json.loads(wpath.read_text())
        assert len(data["processed_ids"]) == 200


# ---------------------------------------------------------------------------
# Asana GID resolution tests
# ---------------------------------------------------------------------------

class TestResolveAssigneeGid:
    def setup_method(self):
        # Reset module cache before each test
        fae._email_to_asana_gid = None

    def _patch_yaml(self):
        return patch("builtins.open", side_effect=self._fake_open)

    def _fake_yaml_load(self):
        return patch("yaml.safe_load", return_value=_MOCK_ASANA_MAP)

    def test_resolves_by_name_match(self):
        """Matches assignee name (case-insensitive) against attendee displayName."""
        with patch.object(fae, "_ASANA_MAP_PATH", MagicMock()):
            fae._email_to_asana_gid = {
                "tommy@hjrglobal.com": "1111111111111111",
                "harrison@hjrglobal.com": "2222222222222222",
            }
            attendees = [
                {"displayName": "Tommy Anderson", "email": "tommy@hjrglobal.com"},
                {"displayName": "Harrison Rogers", "email": "harrison@hjrglobal.com"},
            ]
            gid = fae._resolve_assignee_gid("Tommy", attendees)
        assert gid == "1111111111111111"

    def test_resolves_case_insensitive(self):
        """Name matching is case-insensitive."""
        fae._email_to_asana_gid = {"tommy@hjrglobal.com": "1111111111111111"}
        attendees = [{"displayName": "Tommy Anderson", "email": "tommy@hjrglobal.com"}]
        gid = fae._resolve_assignee_gid("tommy", attendees)
        assert gid == "1111111111111111"

    def test_returns_none_for_unknown_name(self):
        """Returns None when name doesn't match any attendee."""
        fae._email_to_asana_gid = {"tommy@hjrglobal.com": "1111111111111111"}
        attendees = [{"displayName": "Tommy Anderson", "email": "tommy@hjrglobal.com"}]
        gid = fae._resolve_assignee_gid("Shaun", attendees)
        assert gid is None

    def test_returns_none_for_none_assignee(self):
        """Returns None for None assignee_name."""
        fae._email_to_asana_gid = {}
        gid = fae._resolve_assignee_gid(None, [])
        assert gid is None

    def test_returns_none_for_email_not_in_map(self):
        """Returns None when attendee email not in asana map."""
        fae._email_to_asana_gid = {}  # empty map
        attendees = [{"displayName": "External Person", "email": "external@outside.com"}]
        gid = fae._resolve_assignee_gid("External", attendees)
        assert gid is None

    def test_load_email_to_asana_gid_from_yaml(self, tmp_path):
        """Loads and caches email->gid map from yaml file."""
        fae._email_to_asana_gid = None
        yaml_content = {
            "users": [
                {
                    "slack_user_id": "U001",
                    "asana_user_gid": "9999999999",
                    "asana_email": "test@example.com",
                    "email_aliases": ["test@alias.com"],
                }
            ]
        }
        yaml_path = tmp_path / "slack-to-asana.yaml"
        import yaml as _yaml
        yaml_path.write_text(_yaml.dump(yaml_content), encoding="utf-8")
        with patch.object(fae, "_ASANA_MAP_PATH", yaml_path):
            result = fae._load_email_to_asana_gid()
        assert result["test@example.com"] == "9999999999"
        assert result["test@alias.com"] == "9999999999"


# ---------------------------------------------------------------------------
# Haiku parsing tests
# ---------------------------------------------------------------------------

class TestParseActionItemsWithHaiku:
    def test_happy_path_returns_tasks(self):
        """Valid JSON response from Haiku returns structured task list."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps(_MOCK_PARSED_TASKS))]
        with (
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
            patch("anthropic.Anthropic") as mock_client_cls,
            patch.object(fae, "_roster_names", return_value=[]),  # parse-only: no grounding
        ):
            mock_client_cls.return_value.messages.create.return_value = mock_response
            result = fae._parse_action_items_with_haiku("Tommy should send proposal by Friday")
        assert len(result) == 2
        assert result[0]["task"] == "Send proposal to client"
        assert result[0]["assignee_name"] == "Tommy"
        assert result[0]["due_mention"] == "by Friday"
        assert result[1]["assignee_name"] is None

    def test_empty_text_returns_empty_list(self):
        """Empty action items text returns empty list without calling API."""
        with patch("anthropic.Anthropic") as mock_cls:
            result = fae._parse_action_items_with_haiku("")
        assert result == []
        mock_cls.assert_not_called()

    def test_whitespace_only_returns_empty_list(self):
        """Whitespace-only text returns empty list."""
        result = fae._parse_action_items_with_haiku("   \n\t  ")
        assert result == []

    def test_missing_api_key_returns_empty(self):
        """Missing ANTHROPIC_API_KEY returns empty list."""
        with patch.dict("os.environ", {}, clear=True):
            # Ensure key is absent
            import os
            os.environ.pop("ANTHROPIC_API_KEY", None)
            result = fae._parse_action_items_with_haiku("some action items")
        assert result == []

    def test_bad_json_returns_empty(self):
        """Bad JSON from Haiku returns empty list."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="not valid json")]
        with (
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
            patch("anthropic.Anthropic") as mock_client_cls,
        ):
            mock_client_cls.return_value.messages.create.return_value = mock_response
            result = fae._parse_action_items_with_haiku("some action items")
        assert result == []

    def test_non_list_json_returns_empty(self):
        """Non-list JSON (object) returns empty list."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text='{"task": "foo"}')]
        with (
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
            patch("anthropic.Anthropic") as mock_client_cls,
        ):
            mock_client_cls.return_value.messages.create.return_value = mock_response
            result = fae._parse_action_items_with_haiku("some action items")
        assert result == []

    def test_strips_markdown_code_fences(self):
        """Strips ```json ... ``` markdown fences before parsing."""
        raw = "```json\n" + json.dumps(_MOCK_PARSED_TASKS) + "\n```"
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=raw)]
        with (
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
            patch("anthropic.Anthropic") as mock_client_cls,
        ):
            mock_client_cls.return_value.messages.create.return_value = mock_response
            result = fae._parse_action_items_with_haiku("some action items")
        assert len(result) == 2

    def test_api_exception_returns_empty(self):
        """API exception returns empty list (doesn't raise)."""
        with (
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
            patch("anthropic.Anthropic") as mock_client_cls,
        ):
            mock_client_cls.return_value.messages.create.side_effect = Exception("API error")
            result = fae._parse_action_items_with_haiku("some action items")
        assert result == []

    def test_items_without_task_field_skipped(self):
        """Items with empty task field are skipped."""
        data = [
            {"task": "", "assignee_name": "Tommy", "due_mention": None},
            {"task": "Valid task", "assignee_name": None, "due_mention": None},
        ]
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps(data))]
        with (
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
            patch("anthropic.Anthropic") as mock_client_cls,
        ):
            mock_client_cls.return_value.messages.create.return_value = mock_response
            result = fae._parse_action_items_with_haiku("some text")
        assert len(result) == 1
        assert result[0]["task"] == "Valid task"


# ---------------------------------------------------------------------------
# PHI guardrail tests
# ---------------------------------------------------------------------------

class TestPhiGuardrail:
    def test_lex_title_classifies_to_lex(self):
        """A Lexington-titled meeting classifies to the LEX entity."""
        from cora.connectors.fireflies_connector import _classify_entity
        assert _classify_entity("Lexington Services Staff Sync") == "LEX"

    def test_lex_routes_only_to_lex_channels(self):
        """LEX (relaxed 2026-06-14) routes ONLY to LEX channels in the allowlist."""
        # LEX + included sub-entities are present and every LEX channel is in the
        # hard-containment allowlist (hard rail #2).
        assert "LEX" in fae._ENTITY_CHANNEL
        for code, chan in fae._ENTITY_CHANNEL.items():
            if code.upper().startswith("LEX"):
                assert chan in fae._LEX_CHANNEL_ALLOWLIST
        # LBHS is excluded from capture -> it must have NO digest channel.
        assert "LEX-LBHS" not in fae._ENTITY_CHANNEL

    def test_phi_meeting_flagged(self):
        """PHI detection still works for clinical title keywords on LEX."""
        from cora.connectors.fireflies_connector import _is_phi_meeting
        assert _is_phi_meeting("Patient Intake Meeting", "LEX") is True

    def test_non_lex_not_phi(self):
        """Non-LEX entities are never flagged as PHI."""
        from cora.connectors.fireflies_connector import _is_phi_meeting
        assert _is_phi_meeting("F3 Weekly Review", "F3E") is False


# ---------------------------------------------------------------------------
# Channel routing tests
# ---------------------------------------------------------------------------

class TestChannelRouting:
    def test_f3e_routes_to_f3_leadership(self):
        assert fae._ENTITY_CHANNEL["F3E"] == "#f3-leadership"

    def test_osn_routes_to_osn_leadership(self):
        assert fae._ENTITY_CHANNEL["OSN"] == "#osn-leadership"

    def test_lex_routes_to_lex_leadership(self):
        # LEX capture relaxed 2026-06-14: GM-level LEX -> #lex-leadership.
        assert fae._ENTITY_CHANNEL["LEX"] == "C0B3A3U7WS3"

    def test_lex_llc_routes_to_llc_leadership(self):
        assert fae._ENTITY_CHANNEL["LEX-LLC"] == "C0B5SJDHB9C"

    def test_lbhs_has_no_channel(self):
        # Excluded from capture (42 CFR Part 2) -> no digest channel at all.
        assert "LEX-LBHS" not in fae._ENTITY_CHANNEL

    def test_fndr_routes_to_fndr(self):
        assert fae._ENTITY_CHANNEL["FNDR"] == "#fndr"

    def test_hjrg_routes_to_hjrg_leadership(self):
        assert fae._ENTITY_CHANNEL["HJRG"] == "#hjrg-leadership"

    def test_ufl_routes_to_ufl_leadership(self):
        assert fae._ENTITY_CHANNEL["UFL"] == "#ufl-leadership"


# ---------------------------------------------------------------------------
# Slack post tests
# ---------------------------------------------------------------------------

class TestPostSlackSummary:
    def test_no_tasks_does_nothing(self):
        """Empty task list skips Slack post."""
        with patch("httpx.Client") as mock_http:
            fae._post_slack_summary("#f3-leadership", "Meeting", [], dry_run=False)
        mock_http.assert_not_called()

    def test_missing_token_logs_warning(self):
        """Missing SLACK_BOT_TOKEN logs warning and returns."""
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("httpx.Client") as mock_http,
        ):
            import os
            os.environ.pop("SLACK_BOT_TOKEN", None)
            fae._post_slack_summary(
                "#f3-leadership", "Meeting",
                [{"task_name": "Task", "assignee_name": None, "permalink_url": ""}],
            )
        mock_http.assert_not_called()

    def test_dry_run_skips_http(self):
        """dry_run=True skips HTTP call."""
        with (
            patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test"}),
            patch("httpx.Client") as mock_http,
        ):
            fae._post_slack_summary(
                "#f3-leadership", "Meeting",
                [{"task_name": "Task", "assignee_name": "Tommy", "permalink_url": "https://app.asana.com/task/1"}],
                dry_run=True,
            )
        mock_http.assert_not_called()

    def test_posts_to_correct_channel(self):
        """Posts message to specified channel."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp

        with (
            patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test"}),
            patch("httpx.Client", return_value=mock_client),
        ):
            fae._post_slack_summary(
                "#f3-leadership", "F3 Weekly",
                [{"task_name": "Send proposal", "assignee_name": "Tommy", "permalink_url": ""}],
            )

        call_kwargs = mock_client.post.call_args
        assert call_kwargs[1]["json"]["channel"] == "#f3-leadership"
        assert "F3 Weekly" in call_kwargs[1]["json"]["text"]


# ---------------------------------------------------------------------------
# run_action_capture integration tests
# ---------------------------------------------------------------------------

class TestRunActionCapture:
    def setup_method(self):
        fae._email_to_asana_gid = None

    def _mock_haiku_response(self, tasks=None):
        """Return a mock Anthropic response with given tasks."""
        if tasks is None:
            tasks = _MOCK_PARSED_TASKS
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps(tasks))]
        return mock_response

    def test_dry_run_no_asana_or_slack_calls(self, tmp_path):
        """dry_run=True fetches transcripts but skips Asana + Slack."""
        transcript = _make_transcript()
        mock_ff_data = {"transcripts": [transcript]}
        watermark_path = tmp_path / "watermark.json"

        with (
            patch.object(fae, "_WATERMARK_PATH", watermark_path),
            patch("cora.connectors.fireflies_action_extractor._graphql_query", return_value=mock_ff_data),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
            patch("anthropic.Anthropic") as mock_anth,
            patch("cora.connectors.fireflies_action_extractor.create_task") as mock_create,
            patch("cora.connectors.fireflies_action_extractor._resolve_project_smart", return_value="PROJ"),
            patch("httpx.Client") as mock_http,
        ):
            fae._email_to_asana_gid = {"tommy@hjrglobal.com": "1111"}
            mock_anth.return_value.messages.create.return_value = self._mock_haiku_response()
            result = fae.run_action_capture(dry_run=True)

        mock_create.assert_not_called()
        # Slack HTTP should not have been called (dry_run)
        assert result["meetings_processed"] == 1
        # both parsed tasks have a resolved project (patched) -> both pass the
        # Phase 1.5 project guard (assignee is not required)
        assert result["tasks_created"] == 2

    def test_lex_lbhs_meeting_skipped(self, tmp_path):
        """LEX-LBHS meetings stay excluded (42 CFR Part 2), even with LEX capture on."""
        # Jared Harker attendee -> sub-entity LEX-LBHS (excluded).
        transcript = _make_transcript(
            title="Lexington Services Ops Sync",
            attendees=[{"displayName": "Jared Harker", "email": "jared@lexingtonservices.com"}],
        )
        mock_ff_data = {"transcripts": [transcript]}
        watermark_path = tmp_path / "watermark.json"

        with (
            patch.object(fae, "_WATERMARK_PATH", watermark_path),
            patch.object(fae, "_lex_scope_cfg", {
                "enabled": True,
                "included_sub_entities": ["LEX", "LEX-LLC", "LEX-LLA", "LEX-LTS"],
                "excluded_sub_entities": ["LEX-LBHS"],
            }),
            patch("cora.connectors.fireflies_action_extractor._graphql_query", return_value=mock_ff_data),
            patch("anthropic.Anthropic") as mock_anth,
            patch("cora.connectors.fireflies_action_extractor.create_task") as mock_create,
        ):
            result = fae.run_action_capture(dry_run=True)

        mock_anth.assert_not_called()
        mock_create.assert_not_called()
        assert result["meetings_processed"] == 0
        assert result["tasks_created"] == 0

    def test_lex_capture_disabled_skips_all_lex(self, tmp_path):
        """With the scope config disabled, ALL LEX meetings are skipped (old behavior)."""
        transcript = _make_transcript(title="Lexington Services Staff Sync")
        mock_ff_data = {"transcripts": [transcript]}
        watermark_path = tmp_path / "watermark.json"

        with (
            patch.object(fae, "_WATERMARK_PATH", watermark_path),
            patch.object(fae, "_lex_scope_cfg", {"enabled": False}),
            patch("cora.connectors.fireflies_action_extractor._graphql_query", return_value=mock_ff_data),
            patch("anthropic.Anthropic") as mock_anth,
            patch("cora.connectors.fireflies_action_extractor.create_task") as mock_create,
        ):
            result = fae.run_action_capture(dry_run=True)

        mock_anth.assert_not_called()
        mock_create.assert_not_called()
        assert result["meetings_processed"] == 0

    def test_no_action_items_skipped(self, tmp_path):
        """Transcripts with no action items are skipped."""
        transcript = _make_transcript(action_items="")
        mock_ff_data = {"transcripts": [transcript]}
        watermark_path = tmp_path / "watermark.json"

        with (
            patch.object(fae, "_WATERMARK_PATH", watermark_path),
            patch("cora.connectors.fireflies_action_extractor._graphql_query", return_value=mock_ff_data),
            patch("anthropic.Anthropic") as mock_anth,
        ):
            result = fae.run_action_capture(dry_run=True)

        mock_anth.assert_not_called()
        assert result["meetings_processed"] == 0

    def test_empty_transcripts_returns_zeros(self, tmp_path):
        """No transcripts returns all-zero result."""
        watermark_path = tmp_path / "watermark.json"
        with (
            patch.object(fae, "_WATERMARK_PATH", watermark_path),
            patch("cora.connectors.fireflies_action_extractor._graphql_query", return_value={"transcripts": []}),
        ):
            result = fae.run_action_capture(dry_run=True)

        assert result == {"meetings_processed": 0, "tasks_created": 0, "errors": []}

    def test_fireflies_error_logged_to_errors(self, tmp_path):
        """Fireflies API failure is captured in errors list."""
        from cora.connectors.fireflies_connector import FirefliesConnectorError
        watermark_path = tmp_path / "watermark.json"

        with (
            patch.object(fae, "_WATERMARK_PATH", watermark_path),
            patch(
                "cora.connectors.fireflies_action_extractor._graphql_query",
                side_effect=FirefliesConnectorError("API down"),
            ),
        ):
            result = fae.run_action_capture(dry_run=True)

        assert len(result["errors"]) == 1
        assert "API down" in result["errors"][0]

    def test_asana_error_logged_to_errors(self, tmp_path):
        """Asana create_task failure is captured in errors list (non-dry-run)."""
        from cora.tools.asana_client import AsanaClientError
        transcript = _make_transcript()
        mock_ff_data = {"transcripts": [transcript]}
        watermark_path = tmp_path / "watermark.json"

        with (
            patch.object(fae, "_WATERMARK_PATH", watermark_path),
            patch("cora.connectors.fireflies_action_extractor._graphql_query", return_value=mock_ff_data),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
            patch("anthropic.Anthropic") as mock_anth,
            patch("cora.connectors.fireflies_action_extractor.create_task",
                  side_effect=AsanaClientError("PAT invalid")),
            patch("cora.connectors.fireflies_action_extractor._resolve_project_smart", return_value="PROJ"),
            patch("cora.connectors.fireflies_action_extractor._post_slack_summary"),
        ):
            fae._email_to_asana_gid = {}
            mock_anth.return_value.messages.create.return_value = self._mock_haiku_response()
            result = fae.run_action_capture(dry_run=False)

        assert len(result["errors"]) > 0
        assert "PAT invalid" in result["errors"][0]

    def test_watermark_advances_after_processing(self, tmp_path):
        """Watermark advances to latest transcript timestamp after successful run."""
        future_ts = int(time.time()) + 100
        transcript = _make_transcript(date_ts=future_ts)
        mock_ff_data = {"transcripts": [transcript]}
        watermark_path = tmp_path / "watermark.json"

        with (
            patch.object(fae, "_WATERMARK_PATH", watermark_path),
            patch("cora.connectors.fireflies_action_extractor._graphql_query", return_value=mock_ff_data),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
            patch("anthropic.Anthropic") as mock_anth,
            patch("cora.connectors.fireflies_action_extractor.create_task") as mock_create,
            patch("cora.connectors.fireflies_action_extractor._post_slack_summary"),
        ):
            fae._email_to_asana_gid = {}
            mock_anth.return_value.messages.create.return_value = self._mock_haiku_response()
            mock_create.return_value = {"gid": "9999", "permalink_url": "https://app.asana.com/t/9999"}
            fae.run_action_capture(dry_run=False)

        # Watermark file should now exist and have the new ts
        assert watermark_path.exists()
        data = json.loads(watermark_path.read_text())
        assert data["last_processed_ts"] == future_ts


# ---------------------------------------------------------------------------
# Dedup hardening tests (Fix 1, 2026-06-06)
# ---------------------------------------------------------------------------

class TestDedupHardening:
    def setup_method(self):
        fae._email_to_asana_gid = None

    def _haiku(self, tasks=None):
        mock = MagicMock()
        mock.content = [MagicMock(text=json.dumps(tasks or _MOCK_PARSED_TASKS))]
        return mock

    def test_double_run_creates_zero_duplicates(self, tmp_path):
        """Running twice over the same transcript creates tasks once, never twice.

        Run 1 creates tasks and persists the transcript id to the watermark.
        Run 2 sees the same transcript already in processed_ids and skips it
        entirely -- create_task must NOT fire again.
        """
        transcript = _make_transcript()  # id="abc123"
        mock_ff_data = {"transcripts": [transcript]}
        watermark_path = tmp_path / "watermark.json"

        def _run():
            with (
                patch.object(fae, "_WATERMARK_PATH", watermark_path),
                patch("cora.connectors.fireflies_action_extractor._graphql_query", return_value=mock_ff_data),
                patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
                patch("anthropic.Anthropic") as mock_anth,
                patch("cora.connectors.fireflies_action_extractor.create_task") as mock_create,
                patch("cora.connectors.fireflies_action_extractor._resolve_project_smart", return_value="PROJ"),
                patch("cora.connectors.fireflies_action_extractor.find_recent_duplicate_task", return_value=None),
                patch("cora.connectors.fireflies_action_extractor._post_slack_summary"),
            ):
                fae._email_to_asana_gid = {}
                mock_anth.return_value.messages.create.return_value = self._haiku()
                mock_create.return_value = {"gid": "9999", "permalink_url": "https://app.asana.com/t/9999"}
                res = fae.run_action_capture(dry_run=False)
                return res, mock_create.call_count

        res1, calls1 = _run()
        res2, calls2 = _run()

        assert calls1 == 2          # 2 parsed action items created on first run
        assert calls2 == 0          # nothing re-created on second run
        assert res2["meetings_processed"] == 0
        assert res2["tasks_created"] == 0

    def test_creation_time_guard_skips_existing_open_task(self, tmp_path):
        """If an identical open task already exists, create_task is not called."""
        transcript = _make_transcript()
        mock_ff_data = {"transcripts": [transcript]}
        watermark_path = tmp_path / "watermark.json"

        with (
            patch.object(fae, "_WATERMARK_PATH", watermark_path),
            patch("cora.connectors.fireflies_action_extractor._graphql_query", return_value=mock_ff_data),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
            patch("anthropic.Anthropic") as mock_anth,
            patch("cora.connectors.fireflies_action_extractor.create_task") as mock_create,
            patch("cora.connectors.fireflies_action_extractor.find_recent_duplicate_task", return_value="111"),
            patch("cora.connectors.fireflies_action_extractor._post_slack_summary"),
        ):
            fae._email_to_asana_gid = {}
            mock_anth.return_value.messages.create.return_value = self._haiku()
            result = fae.run_action_capture(dry_run=False)

        mock_create.assert_not_called()
        assert result["tasks_created"] == 0

    def test_watermark_persisted_per_meeting(self, tmp_path):
        """Transcript id is persisted to the watermark after the meeting is processed."""
        transcript = _make_transcript()  # id="abc123"
        mock_ff_data = {"transcripts": [transcript]}
        watermark_path = tmp_path / "watermark.json"

        with (
            patch.object(fae, "_WATERMARK_PATH", watermark_path),
            patch("cora.connectors.fireflies_action_extractor._graphql_query", return_value=mock_ff_data),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
            patch("anthropic.Anthropic") as mock_anth,
            patch("cora.connectors.fireflies_action_extractor.create_task") as mock_create,
            patch("cora.connectors.fireflies_action_extractor.find_recent_duplicate_task", return_value=None),
            patch("cora.connectors.fireflies_action_extractor._post_slack_summary"),
        ):
            fae._email_to_asana_gid = {}
            mock_anth.return_value.messages.create.return_value = self._haiku()
            mock_create.return_value = {"gid": "9999", "permalink_url": ""}
            fae.run_action_capture(dry_run=False)

        data = json.loads(watermark_path.read_text())
        assert "abc123" in data["processed_ids"]


# ---------------------------------------------------------------------------
# Project routing tests (Fix 3, 2026-06-06)
# ---------------------------------------------------------------------------

class TestProjectRouting:
    def setup_method(self):
        fae._email_to_asana_gid = None
        fae._capture_project_cfg = None

    def teardown_method(self):
        fae._capture_project_cfg = None

    def test_resolve_capture_project(self):
        fae._capture_project_cfg = {"projects": {"F3E": "12345", "OSN": ""}}
        assert fae._resolve_capture_project("F3E") == "12345"
        assert fae._resolve_capture_project("OSN") is None   # blank -> None
        assert fae._resolve_capture_project("HJRG") is None  # unmapped -> None

    def test_capture_custom_fields_status_priority_entity(self):
        fae._capture_project_cfg = {"custom_fields": {
            "status_field_gid": "S", "status_not_started_option": "S0",
            "priority_field_gid": "P", "priority_medium_option": "P0",
            "entity_field_gid": "E", "entity_options": {"F3E": "EF3"},
        }}
        assert fae._capture_custom_fields("F3E") == {"S": "S0", "P": "P0", "E": "EF3"}
        # Entity option missing -> entity field omitted, status/priority still set
        assert fae._capture_custom_fields("OSN") == {"S": "S0", "P": "P0"}

    def test_capture_custom_fields_empty_when_unconfigured(self):
        fae._capture_project_cfg = {}
        assert fae._capture_custom_fields("F3E") == {}

    def test_run_routes_task_into_project_and_tags(self, tmp_path):
        transcript = _make_transcript()  # F3 Weekly Review -> F3E
        mock_ff = {"transcripts": [transcript]}
        wpath = tmp_path / "wm.json"
        # _make_transcript() title "F3 Weekly Review" classifies to FNDR.
        fae._capture_project_cfg = {
            "projects": {"FNDR": "PROJ1"},
            "custom_fields": {"status_field_gid": "S", "status_not_started_option": "S0"},
        }
        haiku = MagicMock()
        haiku.content = [MagicMock(text=json.dumps([{"task": "X", "assignee_name": None, "due_mention": None}]))]

        with (
            patch.object(fae, "_WATERMARK_PATH", wpath),
            patch("cora.connectors.fireflies_action_extractor._graphql_query", return_value=mock_ff),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
            patch("anthropic.Anthropic") as mock_anth,
            patch("cora.connectors.fireflies_action_extractor.create_task") as mock_create,
            patch("cora.connectors.fireflies_action_extractor.set_task_custom_fields") as mock_cf,
            patch("cora.connectors.fireflies_action_extractor.find_recent_duplicate_task", return_value=None),
            patch("cora.connectors.fireflies_action_extractor._post_slack_summary"),
            # Stub project_resolver so this test validates custom-field tagging in isolation
            patch("cora.connectors.fireflies_action_extractor._resolve_project_smart", return_value="PROJ1"),
        ):
            fae._email_to_asana_gid = {}
            mock_anth.return_value.messages.create.return_value = haiku
            mock_create.return_value = {"gid": "777", "permalink_url": ""}
            fae.run_action_capture(dry_run=False)

        assert mock_create.call_args.kwargs["project_gid"] == "PROJ1"
        mock_cf.assert_called_once_with("777", {"S": "S0"})


# ---------------------------------------------------------------------------
# Roster grounding (B3, 2026-06-13)
# ---------------------------------------------------------------------------

class TestRosterGrounding:
    # Includes the names whose substrings used to collide: Alex/Alina (Al),
    # Alex Cordova (Lex), Hannah Grant (Ann), Jennifer Mortensen (Mort/Jen).
    ROSTER = ["Alex Cordova", "Alina Thomas", "Hannah Grant", "Harrison Rogers",
              "Jennifer Mortensen", "Larry Stone", "Tommy Anderson"]

    def test_exact_full_name(self):
        assert fae._match_roster_name("Tommy Anderson", self.ROSTER) == "Tommy Anderson"

    def test_first_name_maps_to_full(self):
        assert fae._match_roster_name("Tommy", self.ROSTER) == "Tommy Anderson"

    def test_case_insensitive(self):
        assert fae._match_roster_name("hannah", self.ROSTER) == "Hannah Grant"

    def test_transcription_slip_fuzzy(self):
        assert fae._match_roster_name("Harrson", self.ROSTER) == "Harrison Rogers"

    def test_first_name_prefix_unambiguous(self):
        # "Jen" is a prefix of exactly one first name (Jennifer) -> resolves.
        assert fae._match_roster_name("Jen", self.ROSTER) == "Jennifer Mortensen"

    def test_off_roster_returns_none(self):
        assert fae._match_roster_name("Some Vendor", self.ROSTER) is None

    def test_substring_tokens_no_longer_mis_assign(self):
        # The bug the review caught: unanchored substring matched these.
        assert fae._match_roster_name("Lex", self.ROSTER) is None   # was -> Alex Cordova
        assert fae._match_roster_name("Ann", self.ROSTER) is None   # was -> Hannah Grant
        assert fae._match_roster_name("Al", self.ROSTER) is None    # was -> first 'al' match
        assert fae._match_roster_name("Mort", self.ROSTER) is None  # was -> Jennifer Mortensen

    def test_empty_inputs_return_none(self):
        assert fae._match_roster_name("Tommy", []) is None
        assert fae._match_roster_name(None, self.ROSTER) is None
        assert fae._match_roster_name("  ", self.ROSTER) is None

    def test_fyi_dropped_and_assignee_validated_not_canonicalized(self):
        # Validate-only: the PARSED name is kept on a match (not the canonical),
        # so the downstream displayName resolver still works.
        items = [
            {"task": "Tommy sent the proposal", "assignee_name": "Tommy",
             "due_mention": None, "is_actionable": False},
            {"task": "Send the deck", "assignee_name": "Hannah",
             "due_mention": "Friday", "is_actionable": True},
        ]
        out = fae._ground_and_filter_items(items, self.ROSTER)
        assert len(out) == 1
        assert out[0]["task"] == "Send the deck"
        assert out[0]["assignee_name"] == "Hannah"          # parsed name kept, NOT "Hannah Grant"
        assert out[0]["due_mention"] == "Friday"

    def test_nickname_kept_for_downstream_resolution(self):
        # "Jen" matches Jennifer Mortensen but we KEEP "Jen" so the attendee
        # displayName "Jen Mortensen" still resolves downstream.
        items = [{"task": "Email the guardians", "assignee_name": "Jen",
                  "due_mention": None, "is_actionable": True}]
        assert fae._ground_and_filter_items(items, self.ROSTER)[0]["assignee_name"] == "Jen"

    def test_missing_is_actionable_is_kept(self):
        items = [{"task": "Do the thing", "assignee_name": None, "due_mention": None}]
        assert len(fae._ground_and_filter_items(items, self.ROSTER)) == 1

    def test_string_false_and_zero_dropped(self):
        items = [
            {"task": "string false", "assignee_name": None, "is_actionable": "false"},
            {"task": "int zero", "assignee_name": None, "is_actionable": 0},
            {"task": "string true kept", "assignee_name": None, "is_actionable": "true"},
        ]
        out = fae._ground_and_filter_items(items, self.ROSTER)
        assert [o["task"] for o in out] == ["string true kept"]

    def test_off_roster_assignee_nulled(self):
        items = [{"task": "Call the vendor", "assignee_name": "Random Person",
                  "due_mention": None, "is_actionable": True}]
        assert fae._ground_and_filter_items(items, self.ROSTER)[0]["assignee_name"] is None

    def test_non_string_assignee_coerced_to_none(self):
        # A list/number assignee must not crash the downstream .lower().
        items = [{"task": "x", "assignee_name": ["Tommy", "Alex"], "is_actionable": True}]
        assert fae._ground_and_filter_items(items, self.ROSTER)[0]["assignee_name"] is None

    def test_assignee_kept_when_no_roster(self):
        items = [{"task": "Call the vendor", "assignee_name": "Tommy",
                  "due_mention": None, "is_actionable": True}]
        assert fae._ground_and_filter_items(items, [])[0]["assignee_name"] == "Tommy"

    def test_long_task_capped(self):
        items = [{"task": "x" * 300, "assignee_name": None,
                  "due_mention": None, "is_actionable": True}]
        out = fae._ground_and_filter_items(items, self.ROSTER)
        assert len(out[0]["task"]) <= fae._MAX_TASK_LEN

    def test_empty_task_dropped(self):
        items = [{"task": "   ", "assignee_name": "Tommy",
                  "due_mention": None, "is_actionable": True}]
        assert fae._ground_and_filter_items(items, self.ROSTER) == []

    def test_non_dict_item_skipped(self):
        items = ["not a dict", {"task": "ok", "assignee_name": None, "due_mention": None}]
        out = fae._ground_and_filter_items(items, self.ROSTER)
        assert len(out) == 1 and out[0]["task"] == "ok"

    def test_parse_applies_grounding(self):
        data = [
            {"task": "Ship the order", "assignee_name": "Tommy",
             "due_mention": "Monday", "is_actionable": True},
            {"task": "Order already shipped", "assignee_name": "Tommy",
             "due_mention": None, "is_actionable": False},
        ]
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps(data))]
        with (
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
            patch("anthropic.Anthropic") as mock_cls,
            patch.object(fae, "_roster_names", return_value=["Tommy Anderson", "Hannah Grant"]),
        ):
            mock_cls.return_value.messages.create.return_value = mock_response
            result = fae._parse_action_items_with_haiku("some action items")
        assert len(result) == 1                       # FYI item dropped
        assert result[0]["task"] == "Ship the order"
        assert result[0]["assignee_name"] == "Tommy"  # validated, parsed name kept


# ---------------------------------------------------------------------------
# Precision guards (Phase 1.5): FYI/Cora-actor noise filter
# ---------------------------------------------------------------------------

class TestPrecisionNoiseFilter:
    ROSTER = ["Tommy Anderson", "Hannah Grant"]

    def test_fyi_prefix_items_dropped(self):
        items = [
            {"task": "FYI the warehouse closes early Friday", "assignee_name": "Tommy", "is_actionable": True},
            {"task": "Heads up - rent is due", "assignee_name": "Tommy", "is_actionable": True},
            {"task": "Status update: launch on track", "assignee_name": None, "is_actionable": True},
        ]
        assert fae._ground_and_filter_items(items, self.ROSTER) == []

    def test_cora_actor_dropped(self):
        items = [{"task": "Cora posted the weekly digest", "assignee_name": None, "is_actionable": True}]
        assert fae._ground_and_filter_items(items, self.ROSTER) == []

    def test_cora_as_object_kept(self):
        # "Cora" as recipient, not actor -> a real task, kept.
        items = [{"task": "Send Cora the updated roster", "assignee_name": "Tommy", "is_actionable": True}]
        out = fae._ground_and_filter_items(items, self.ROSTER)
        assert len(out) == 1 and out[0]["task"] == "Send Cora the updated roster"

    def test_fyi_inside_text_kept(self):
        # FYI not at the start -> real action, kept (anchored match).
        items = [{"task": "Send the FYI deck to the board", "assignee_name": "Tommy", "is_actionable": True}]
        assert len(fae._ground_and_filter_items(items, self.ROSTER)) == 1

    def test_is_noise_task_unit(self):
        assert fae._is_noise_task("FYI: nothing to do") is True
        assert fae._is_noise_task("Cora will post the digest") is True
        assert fae._is_noise_task("Ship the order to Nimbl") is False


# ---------------------------------------------------------------------------
# Precision guards (Phase 1.5): project-required (no orphan tasks)
# ---------------------------------------------------------------------------

class TestProjectGuard:
    def setup_method(self):
        fae._email_to_asana_gid = None

    def _haiku(self, tasks):
        mock = MagicMock()
        mock.content = [MagicMock(text=json.dumps(tasks))]
        return mock

    def test_no_project_skips_task(self, tmp_path):
        """A non-LEX task whose project does not resolve is skipped, not orphaned."""
        transcript = _make_transcript()
        wpath = tmp_path / "wm.json"
        with (
            patch.object(fae, "_WATERMARK_PATH", wpath),
            patch("cora.connectors.fireflies_action_extractor._graphql_query", return_value={"transcripts": [transcript]}),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "k"}),
            patch("anthropic.Anthropic") as mock_anth,
            patch("cora.connectors.fireflies_action_extractor._resolve_project_smart", return_value=None),
            patch("cora.connectors.fireflies_action_extractor.create_task") as mock_create,
            patch("cora.connectors.fireflies_action_extractor.find_recent_duplicate_task", return_value=None),
            patch("cora.connectors.fireflies_action_extractor._post_slack_summary"),
        ):
            fae._email_to_asana_gid = {"tommy@hjrglobal.com": "1111"}
            mock_anth.return_value.messages.create.return_value = self._haiku(
                [{"task": "Send proposal to client", "assignee_name": "Tommy",
                  "due_mention": None, "is_actionable": True}]
            )
            result = fae.run_action_capture(dry_run=False)
        mock_create.assert_not_called()
        assert result["tasks_created"] == 0

    def test_resolved_project_creates_task(self, tmp_path):
        transcript = _make_transcript()
        wpath = tmp_path / "wm.json"
        with (
            patch.object(fae, "_WATERMARK_PATH", wpath),
            patch("cora.connectors.fireflies_action_extractor._graphql_query", return_value={"transcripts": [transcript]}),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "k"}),
            patch("anthropic.Anthropic") as mock_anth,
            patch("cora.connectors.fireflies_action_extractor._resolve_project_smart", return_value="PROJ"),
            patch("cora.connectors.fireflies_action_extractor.create_task") as mock_create,
            patch("cora.connectors.fireflies_action_extractor.find_recent_duplicate_task", return_value=None),
            patch("cora.connectors.fireflies_action_extractor._post_slack_summary"),
        ):
            fae._email_to_asana_gid = {"tommy@hjrglobal.com": "1111"}
            mock_create.return_value = {"gid": "9", "permalink_url": ""}
            mock_anth.return_value.messages.create.return_value = self._haiku(
                [{"task": "Send proposal to client", "assignee_name": "Tommy",
                  "due_mention": None, "is_actionable": True}]
            )
            result = fae.run_action_capture(dry_run=False)
        mock_create.assert_called_once()
        assert result["tasks_created"] == 1
