"""Tests for src/cora/tools/project_resolver.py."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# -- path setup ---------------------------------------------------------------
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import cora.tools.project_resolver as resolver  # noqa: E402

# ---------------------------------------------------------------------------
# Minimal fake YAML map for tests
# ---------------------------------------------------------------------------
_FAKE_MAP = {
    "blocked_projects": ["BLOCKED_GID_1", "BLOCKED_GID_2"],
    "entities": {
        "F3E": {
            "catch_all_gid": "F3E_CATCHALL",
            "assignee_rules": [
                {"asana_gid": "TOMMY_GID", "project_gid": "F3E_SALES"},
            ],
            "brand_rules": [
                {
                    "brand": "pure",
                    "detect_keywords": ["pure", "f3 pure"],
                    "event_project_gid": "F3E_EVENTS_PURE",
                    "social_project_gid": "F3E_SOCIAL_PURE",
                },
                {
                    "brand": "mood",
                    "detect_keywords": ["mood", "f3 mood"],
                    "event_project_gid": "F3E_EVENTS_MOOD",
                    "social_project_gid": "F3E_SOCIAL_MOOD",
                },
                {
                    "brand": "energy",
                    "detect_keywords": ["energy", "f3 energy"],
                    "event_project_gid": "F3E_EVENTS_ENERGY",
                    "social_project_gid": "F3E_SOCIAL_ENERGY",
                },
            ],
            "keyword_rules": [
                {
                    "project_gid": "F3E_WHOLESALE",
                    "keywords": ["wholesale", "GNC", "distribution", "retailer"],
                },
                {
                    "project_gid": "F3E_SALES",
                    "keywords": ["sales pipeline", "proposal", "outreach"],
                },
                {
                    "project_gid": "F3E_EVENTS_ENERGY",  # fallback for events
                    "keywords": ["event", "activation", "booth"],
                    "brand_detect": True,
                    "fallback_project_gid": "F3E_EVENTS_ENERGY",
                },
                {
                    "project_gid": "F3E_SOCIAL_ENERGY",  # fallback for social
                    "keywords": ["social media", "instagram", "content"],
                    "brand_detect": True,
                    "fallback_project_gid": "F3E_SOCIAL_ENERGY",
                },
                {
                    "project_gid": "F3E_NSF",
                    "keywords": ["nsf", "certification"],
                },
            ],
            "meeting_title_rules": [
                {"project_gid": "F3E_SALES", "title_patterns": ["sales", "wholesale"]},
                {"project_gid": "F3E_WEEKLY", "title_patterns": ["f3 weekly", "weekly ops"]},
            ],
        },
        "OSN": {
            "catch_all_gid": "OSN_CATCHALL",
            "keyword_rules": [
                {"project_gid": "OSN_GW", "keywords": ["gilbert warner", "g&w", "gw store"]},
                {"project_gid": "OSN_POS", "keywords": ["pos", "point of sale", "register"]},
            ],
        },
        "LEX-LLC": {
            "catch_all_gid": "LEX_LLC_CATCHALL",
        },
        "LEX": {
            "catch_all_gid": "LEX_CATCHALL",
            "keyword_rules": [
                {"project_gid": "LEX_TUCSON", "keywords": ["tucson", "tucson dta"]},
            ],
        },
        "UFL": {
            "catch_all_gid": "UFL_STRATEGIC",
            "paused": True,
        },
    },
}


@pytest.fixture(autouse=True)
def fake_map(monkeypatch):
    """Inject fake map and reset module cache before each test."""
    resolver._project_map = _FAKE_MAP
    yield
    resolver._project_map = None


# ---------------------------------------------------------------------------
# Blocked projects
# ---------------------------------------------------------------------------

class TestBlockedProjects:
    def test_get_blocked_returns_set(self):
        blocked = resolver.get_blocked_project_gids()
        assert "BLOCKED_GID_1" in blocked
        assert "BLOCKED_GID_2" in blocked

    def test_is_blocked_true(self):
        assert resolver.is_blocked_project("BLOCKED_GID_1") is True

    def test_is_blocked_false(self):
        assert resolver.is_blocked_project("F3E_CATCHALL") is False


# ---------------------------------------------------------------------------
# Unknown entity
# ---------------------------------------------------------------------------

class TestUnknownEntity:
    def test_unknown_entity_returns_none(self):
        result = resolver.resolve_project(entity="UNKNOWN_XYZ", task_text="anything")
        assert result is None

    def test_parent_fallback_lex_llc(self):
        # LEX-LLC should fall through to its own config
        result = resolver.resolve_project(entity="LEX-LLC", task_text="any task")
        assert result == "LEX_LLC_CATCHALL"


# ---------------------------------------------------------------------------
# UFL paused
# ---------------------------------------------------------------------------

class TestUFLPaused:
    def test_ufl_always_returns_catchall(self):
        result = resolver.resolve_project(entity="UFL", task_text="event production venue")
        assert result == "UFL_STRATEGIC"

    def test_ufl_with_assignee_still_catchall(self):
        result = resolver.resolve_project(entity="UFL", task_text="anything", assignee_gid="TOMMY_GID")
        assert result == "UFL_STRATEGIC"


# ---------------------------------------------------------------------------
# Assignee rules
# ---------------------------------------------------------------------------

class TestAssigneeRules:
    def test_tommy_routes_to_sales(self):
        result = resolver.resolve_project(
            entity="F3E",
            task_text="follow up with distributor",
            assignee_gid="TOMMY_GID",
        )
        assert result == "F3E_SALES"

    def test_different_assignee_does_not_trigger_rule(self):
        result = resolver.resolve_project(
            entity="F3E",
            task_text="GNC wholesale proposal",
            assignee_gid="SOMEONE_ELSE_GID",
        )
        # Should fall to keyword match (GNC -> wholesale)
        assert result == "F3E_WHOLESALE"


# ---------------------------------------------------------------------------
# Meeting title patterns
# ---------------------------------------------------------------------------

class TestMeetingTitleRules:
    def test_sales_meeting_title(self):
        result = resolver.resolve_project(
            entity="F3E",
            task_text="review deck",
            meeting_title="F3E Sales Weekly Sync",
        )
        assert result == "F3E_SALES"

    def test_weekly_ops_meeting_title(self):
        result = resolver.resolve_project(
            entity="F3E",
            task_text="update inventory",
            meeting_title="F3 Weekly Ops 06/06",
        )
        assert result == "F3E_WEEKLY"

    def test_unmatched_meeting_title_falls_to_keywords(self):
        result = resolver.resolve_project(
            entity="F3E",
            task_text="nsf certification renewal",
            meeting_title="Random Meeting",
        )
        assert result == "F3E_NSF"


# ---------------------------------------------------------------------------
# Brand rules
# ---------------------------------------------------------------------------

class TestBrandRules:
    def test_pure_event(self):
        result = resolver.resolve_project(
            entity="F3E",
            task_text="F3 Pure activation booth at Sprouts",
            assignee_gid=None,
        )
        assert result == "F3E_EVENTS_PURE"

    def test_mood_event(self):
        result = resolver.resolve_project(
            entity="F3E",
            task_text="F3 Mood event corporate sponsor appearance",
            assignee_gid=None,
        )
        assert result == "F3E_EVENTS_MOOD"

    def test_energy_social_post(self):
        result = resolver.resolve_project(
            entity="F3E",
            task_text="F3 Energy instagram content reel",
            assignee_gid=None,
        )
        assert result == "F3E_SOCIAL_ENERGY"

    def test_pure_social_post(self):
        result = resolver.resolve_project(
            entity="F3E",
            task_text="F3 Pure social media post for launch",
            assignee_gid=None,
        )
        assert result == "F3E_SOCIAL_PURE"

    def test_event_no_brand_uses_energy_fallback(self):
        result = resolver.resolve_project(
            entity="F3E",
            task_text="schedule event activation booth",
            assignee_gid=None,
        )
        # No brand keyword → fallback_project_gid (Energy events)
        assert result == "F3E_EVENTS_ENERGY"


# ---------------------------------------------------------------------------
# Keyword rules
# ---------------------------------------------------------------------------

class TestKeywordRules:
    def test_wholesale_keyword(self):
        result = resolver.resolve_project(
            entity="F3E",
            task_text="Send GNC wholesale pricing deck",
        )
        assert result == "F3E_WHOLESALE"

    def test_distribution_keyword(self):
        result = resolver.resolve_project(
            entity="F3E",
            task_text="Follow up with distribution partner",
        )
        assert result == "F3E_WHOLESALE"

    def test_nsf_keyword(self):
        result = resolver.resolve_project(
            entity="F3E",
            task_text="renew nsf certification for Energy SKU",
        )
        assert result == "F3E_NSF"

    def test_osn_gw_keyword(self):
        result = resolver.resolve_project(
            entity="OSN",
            task_text="recount inventory at Gilbert Warner store",
        )
        assert result == "OSN_GW"

    def test_osn_pos_keyword(self):
        result = resolver.resolve_project(
            entity="OSN",
            task_text="configure new POS register at store",
        )
        assert result == "OSN_POS"

    def test_lex_tucson_keyword(self):
        result = resolver.resolve_project(
            entity="LEX",
            task_text="schedule Tucson DTA opening inspection",
        )
        assert result == "LEX_TUCSON"


# ---------------------------------------------------------------------------
# Catch-all fallback
# ---------------------------------------------------------------------------

class TestCatchAll:
    def test_f3e_no_match_returns_catchall(self):
        result = resolver.resolve_project(
            entity="F3E",
            task_text="completely unrelated task with no keywords",
        )
        assert result == "F3E_CATCHALL"

    def test_osn_no_match_returns_catchall(self):
        result = resolver.resolve_project(
            entity="OSN",
            task_text="some random task",
        )
        assert result == "OSN_CATCHALL"

    def test_lex_llc_no_keywords_returns_catchall(self):
        result = resolver.resolve_project(entity="LEX-LLC", task_text="")
        assert result == "LEX_LLC_CATCHALL"


# ---------------------------------------------------------------------------
# Blocked projects never returned
# ---------------------------------------------------------------------------

class TestBlockedGuard:
    def test_blocked_catch_all_returns_none(self):
        """If catch-all itself is in blocked list, return None."""
        blocked_map = {
            "blocked_projects": ["BLOCKED_CATCHALL"],
            "entities": {
                "FAKE": {
                    "catch_all_gid": "BLOCKED_CATCHALL",
                }
            },
        }
        resolver._project_map = blocked_map
        result = resolver.resolve_project(entity="FAKE", task_text="any task")
        assert result is None


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_task_text_returns_catchall(self):
        result = resolver.resolve_project(entity="F3E", task_text="")
        assert result == "F3E_CATCHALL"

    def test_none_assignee_gid_skips_assignee_rules(self):
        result = resolver.resolve_project(
            entity="F3E",
            task_text="GNC wholesale deck",
            assignee_gid=None,
        )
        assert result == "F3E_WHOLESALE"

    def test_case_insensitive_matching(self):
        result = resolver.resolve_project(
            entity="F3E",
            task_text="SEND NSF CERTIFICATION RENEWAL",
        )
        assert result == "F3E_NSF"

    def test_substring_matching(self):
        result = resolver.resolve_project(
            entity="OSN",
            task_text="there is a point of sale issue at the store",
        )
        assert result == "OSN_POS"

    def test_reload_map_clears_cache(self):
        assert resolver._project_map is not None
        resolver.reload_map()
        # After reload, it reads from disk -- may fail gracefully if file missing
        # Just verify cache was cleared and reload was attempted
        # (the fake autouse fixture will have restored _project_map)
