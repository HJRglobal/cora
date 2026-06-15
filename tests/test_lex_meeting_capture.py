"""Tests for LEX Meeting Action Capture relaxation (Harrison directive 2026-06-14).

LEX operational meetings now flow through capture, but SCOPED:
  - LEX-LBHS excluded (42 CFR Part 2).
  - LEX tasks route ONLY into LEX-scoped Asana projects (hard rail #1).
  - LEX digests post ONLY to LEX channels (hard rail #2).
  - task text is PHI-scrubbed (hard rail #4); scrubber error -> task still
    created + flagged (fail-safe).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora.connectors import fireflies_action_extractor as fae


_SCOPE_CFG = {
    "enabled": True,
    "included_sub_entities": ["LEX", "LEX-LLC", "LEX-LLA", "LEX-LTS"],
    "excluded_sub_entities": ["LEX-LBHS"],
}


def _transcript(title, action_items, attendees, tid="lexmtg1", date_ts=None):
    return {
        "id": tid,
        "title": title,
        "date": date_ts or (int(time.time()) - 3600),
        "duration": 1800,
        "summary": {"action_items": action_items, "overview": "ops"},
        "meeting_attendees": attendees,
    }


# ---------------------------------------------------------------------------
# Scope config
# ---------------------------------------------------------------------------

class TestLexScope:
    def setup_method(self):
        fae._lex_scope_cfg = None

    def teardown_method(self):
        fae._lex_scope_cfg = None

    def test_real_config_loads_enabled(self):
        """The shipped meeting-capture-lex-scope.yaml loads with capture enabled."""
        fae._lex_scope_cfg = None
        assert fae._lex_capture_enabled() is True

    def test_included_sub_entities_allowed(self):
        fae._lex_scope_cfg = _SCOPE_CFG
        for code in ("LEX", "LEX-LLC", "LEX-LLA", "LEX-LTS"):
            assert fae._lex_sub_entity_allowed(code) is True

    def test_lbhs_excluded(self):
        fae._lex_scope_cfg = _SCOPE_CFG
        assert fae._lex_sub_entity_allowed("LEX-LBHS") is False

    def test_unknown_sub_entity_not_allowed(self):
        """Fail-closed: a code not in the include list is not allowed."""
        fae._lex_scope_cfg = _SCOPE_CFG
        assert fae._lex_sub_entity_allowed("LEX-MYSTERY") is False

    def test_excluded_wins_over_included(self):
        """If a code is in both lists, excluded wins (Part-2 safety)."""
        fae._lex_scope_cfg = {
            "enabled": True,
            "included_sub_entities": ["LEX-LBHS"],
            "excluded_sub_entities": ["LEX-LBHS"],
        }
        assert fae._lex_sub_entity_allowed("LEX-LBHS") is False

    def test_disabled_master_switch(self):
        fae._lex_scope_cfg = {"enabled": False}
        assert fae._lex_capture_enabled() is False

    def test_config_read_error_fails_safe_off(self, tmp_path):
        """Unreadable scope config -> capture disabled (fail-safe)."""
        fae._lex_scope_cfg = None
        with patch.object(fae, "_LEX_SCOPE_PATH", tmp_path / "missing.yaml"):
            assert fae._lex_capture_enabled() is False


# ---------------------------------------------------------------------------
# Project containment (hard rail #1)
# ---------------------------------------------------------------------------

class TestLexProjectContainment:
    def setup_method(self):
        fae._known_lex_projects = None
        fae._capture_project_cfg = None

    def teardown_method(self):
        fae._known_lex_projects = None
        fae._capture_project_cfg = None

    def test_known_lex_projects_include_catch_all(self):
        """The LEX catch-all from the real config is in the known-LEX allowlist."""
        fae._known_lex_projects = None
        fae._capture_project_cfg = None
        known = fae._known_lex_project_gids()
        # [LEX-LLC] Operations -- General catch-all, shared by LEX + LEX-LLC.
        assert "1215470944114390" in known

    def test_known_lex_projects_exclude_non_lex(self):
        """A non-LEX project (F3E catch-all) is NOT in the LEX allowlist."""
        fae._known_lex_projects = None
        fae._capture_project_cfg = None
        known = fae._known_lex_project_gids()
        assert "1215470928454227" not in known  # F3E Operations -- General

    def test_resolve_lex_project_returns_known_lex_gid(self):
        fae._known_lex_projects = frozenset({"LEXPROJ", "LEXCATCH"})
        with patch.object(fae, "_resolve_project_smart", return_value="LEXPROJ"):
            gid = fae._resolve_lex_project("LEX-LLC", "do the thing", None, "LLC Sync")
        assert gid == "LEXPROJ"

    def test_resolve_lex_project_rejects_non_lex_and_falls_back(self):
        """If routing produces a non-LEX project, fall back to the LEX catch-all."""
        fae._known_lex_projects = frozenset({"LEXCATCH"})
        with (
            patch.object(fae, "_resolve_project_smart", return_value="F3EPROJ"),
            patch.object(fae, "_resolve_capture_project", side_effect=lambda e: "LEXCATCH" if e == "LEX" else None),
        ):
            gid = fae._resolve_lex_project("LEX-LTS", "task", None, "title")
        assert gid == "LEXCATCH"

    def test_resolve_lex_project_none_when_no_lex_project(self):
        """No LEX project anywhere -> None (caller skips, never leaks)."""
        fae._known_lex_projects = frozenset()
        with (
            patch.object(fae, "_resolve_project_smart", return_value=None),
            patch.object(fae, "_resolve_capture_project", return_value=None),
        ):
            assert fae._resolve_lex_project("LEX-LLC", "task", None, "title") is None


# ---------------------------------------------------------------------------
# Channel containment (hard rail #2)
# ---------------------------------------------------------------------------

class TestLexChannelContainment:
    def test_lex_channels_in_allowlist(self):
        for code in ("LEX", "LEX-LLC", "LEX-LLA", "LEX-LTS"):
            assert fae._ENTITY_CHANNEL[code] in fae._LEX_CHANNEL_ALLOWLIST

    def test_no_non_lex_channel_in_lex_allowlist(self):
        """The allowlist contains only LEX channel IDs, never #hjrg/#fndr/etc."""
        non_lex_channels = {
            fae._ENTITY_CHANNEL[c] for c in fae._ENTITY_CHANNEL
            if not c.upper().startswith("LEX")
        }
        assert fae._LEX_CHANNEL_ALLOWLIST.isdisjoint(non_lex_channels)

    def test_lbhs_not_routable(self):
        assert "LEX-LBHS" not in fae._ENTITY_CHANNEL


# ---------------------------------------------------------------------------
# End-to-end run behavior
# ---------------------------------------------------------------------------

class TestLexRunBehavior:
    def setup_method(self):
        fae._email_to_asana_gid = None
        fae._lex_scope_cfg = _SCOPE_CFG
        fae._known_lex_projects = frozenset({"LEXPROJ"})

    def teardown_method(self):
        fae._lex_scope_cfg = None
        fae._known_lex_projects = None
        fae._email_to_asana_gid = None

    def _haiku(self, tasks):
        mock = MagicMock()
        mock.content = [MagicMock(text=json.dumps(tasks))]
        return mock

    def test_llc_meeting_processed_and_routed_to_llc_channel(self, tmp_path):
        """An LLC meeting (Shaun attendee) is processed, routed to a LEX project,
        and its digest goes to #llc-leadership only."""
        transcript = _transcript(
            "Lexington Services Staff Sync",
            "Shaun to confirm the new van checklist by Friday",
            [{"displayName": "Shaun Hawkins", "email": "shaun@lexingtonservices.com"}],
        )
        wpath = tmp_path / "wm.json"
        captured_post = {}

        def _fake_post(channel, title, tasks, dry_run=False):
            captured_post["channel"] = channel

        with (
            patch.object(fae, "_WATERMARK_PATH", wpath),
            patch("cora.connectors.fireflies_action_extractor._graphql_query",
                  return_value={"transcripts": [transcript]}),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "k", "SLACK_BOT_TOKEN": "x"}),
            patch("anthropic.Anthropic") as mock_anth,
            patch.object(fae, "_resolve_project_smart", return_value="LEXPROJ"),
            patch.object(fae, "_staff_allowed_names", return_value={"Shaun Hawkins"}),
            patch("cora.connectors.fireflies_action_extractor.create_task") as mock_create,
            patch("cora.connectors.fireflies_action_extractor.find_recent_duplicate_task", return_value=None),
            patch("cora.connectors.fireflies_action_extractor._post_slack_summary", side_effect=_fake_post),
        ):
            fae._email_to_asana_gid = {"shaun@lexingtonservices.com": "SHAUNGID"}
            mock_anth.return_value.messages.create.return_value = self._haiku(
                [{"task": "Confirm the new van checklist", "assignee_name": "Shaun",
                  "due_mention": "Friday", "is_actionable": True}]
            )
            result = fae.run_action_capture(dry_run=False)

        assert result["meetings_processed"] == 1
        assert result["tasks_created"] == 1
        # hard rail #1: routed to a LEX-scoped project
        assert mock_create.call_args.kwargs["project_gid"] == "LEXPROJ"
        # hard rail #2: digest only to the LLC leadership channel
        assert captured_post["channel"] == fae._ENTITY_CHANNEL["LEX-LLC"]

    def test_lex_task_text_is_phi_scrubbed(self, tmp_path):
        """A member name + diagnosis in the action item is scrubbed from the task."""
        transcript = _transcript(
            "Lexington Services Staff Sync",
            "follow up on a member's plan",
            [{"displayName": "Shaun Hawkins", "email": "shaun@lexingtonservices.com"}],
        )
        wpath = tmp_path / "wm.json"

        with (
            patch.object(fae, "_WATERMARK_PATH", wpath),
            patch("cora.connectors.fireflies_action_extractor._graphql_query",
                  return_value={"transcripts": [transcript]}),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "k"}),
            patch("anthropic.Anthropic") as mock_anth,
            patch.object(fae, "_resolve_project_smart", return_value="LEXPROJ"),
            patch.object(fae, "_staff_allowed_names", return_value={"Shaun Hawkins"}),
            patch("cora.connectors.fireflies_action_extractor.create_task") as mock_create,
            patch("cora.connectors.fireflies_action_extractor.find_recent_duplicate_task", return_value=None),
            patch("cora.connectors.fireflies_action_extractor._post_slack_summary"),
        ):
            fae._email_to_asana_gid = {}
            mock_anth.return_value.messages.create.return_value = self._haiku(
                [{"task": "Update Bob Smith's autism support plan",
                  "assignee_name": "Shaun", "due_mention": None, "is_actionable": True}]
            )
            fae.run_action_capture(dry_run=False)

        created_name = mock_create.call_args.kwargs["name"]
        assert "Bob Smith" not in created_name
        assert "autism" not in created_name.lower()
        assert "support plan" in created_name  # operational text survives
        # notes must NOT carry the raw action-item dump for LEX
        notes = mock_create.call_args.kwargs["notes"]
        assert "Bob Smith" not in notes

    def test_scrubber_failure_flags_task_not_dropped(self, tmp_path):
        """If the scrubber raises, the task is still created + flagged (fail-safe)."""
        transcript = _transcript(
            "Lexington Services Staff Sync",
            "do the thing",
            [{"displayName": "Shaun Hawkins", "email": "shaun@lexingtonservices.com"}],
        )
        wpath = tmp_path / "wm.json"

        with (
            patch.object(fae, "_WATERMARK_PATH", wpath),
            patch("cora.connectors.fireflies_action_extractor._graphql_query",
                  return_value={"transcripts": [transcript]}),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "k"}),
            patch("anthropic.Anthropic") as mock_anth,
            patch.object(fae, "_resolve_project_smart", return_value="LEXPROJ"),
            patch.object(fae, "scrub_lex_phi", side_effect=RuntimeError("boom")),
            patch.object(fae, "_staff_allowed_names", return_value=set()),
            patch("cora.connectors.fireflies_action_extractor.create_task") as mock_create,
            patch("cora.connectors.fireflies_action_extractor.find_recent_duplicate_task", return_value=None),
            patch("cora.connectors.fireflies_action_extractor._post_slack_summary"),
        ):
            fae._email_to_asana_gid = {}
            mock_anth.return_value.messages.create.return_value = self._haiku(
                [{"task": "Sensitive raw line about a client", "assignee_name": None,
                  "due_mention": None, "is_actionable": True}]
            )
            result = fae.run_action_capture(dry_run=False)

        assert result["tasks_created"] == 1
        mock_create.assert_called_once()
        assert "[review for PHI]" in mock_create.call_args.kwargs["name"]

    def test_lex_task_never_created_without_lex_project(self, tmp_path):
        """If no LEX project resolves, the LEX task is skipped (never leaks)."""
        transcript = _transcript(
            "Lexington Services Staff Sync",
            "do the thing",
            [{"displayName": "Shaun Hawkins", "email": "shaun@lexingtonservices.com"}],
        )
        wpath = tmp_path / "wm.json"

        with (
            patch.object(fae, "_WATERMARK_PATH", wpath),
            patch("cora.connectors.fireflies_action_extractor._graphql_query",
                  return_value={"transcripts": [transcript]}),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "k"}),
            patch("anthropic.Anthropic") as mock_anth,
            patch.object(fae, "_resolve_lex_project", return_value=None),
            patch.object(fae, "_staff_allowed_names", return_value=set()),
            patch("cora.connectors.fireflies_action_extractor.create_task") as mock_create,
            patch("cora.connectors.fireflies_action_extractor.find_recent_duplicate_task", return_value=None),
            patch("cora.connectors.fireflies_action_extractor._post_slack_summary"),
        ):
            fae._email_to_asana_gid = {}
            mock_anth.return_value.messages.create.return_value = self._haiku(
                [{"task": "Sensitive task", "assignee_name": None,
                  "due_mention": None, "is_actionable": True}]
            )
            result = fae.run_action_capture(dry_run=False)

        mock_create.assert_not_called()
        assert result["tasks_created"] == 0
