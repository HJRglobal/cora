"""Unit tests for LEX sub-entity signal tagging and KB visibility filtering."""

import pytest

from cora.connectors.asana_connector import _tag_asana_sub_entity_for_project
from cora.connectors.fireflies_connector import _tag_fireflies_sub_entity
from cora.knowledge_base.store import build_sub_entity_filter


# ── build_sub_entity_filter ───────────────────────────────────────────────────

def test_filter_lex_llc():
    result = build_sub_entity_filter("LEX-LLC")
    assert result is not None
    sql, params = result
    # STRICT MODE: only explicitly-tagged chunks returned; NULL-tagged rows excluded.
    assert "sub_entity IN" in sql
    assert "IS NULL" not in sql
    assert "LEX-LLC" in params


def test_filter_lex_lts():
    sql, params = build_sub_entity_filter("LEX-LTS")
    assert "LEX-LTS" in params
    assert "LEX-LLC" not in params


def test_filter_lex_lbhs():
    sql, params = build_sub_entity_filter("LEX-LBHS")
    assert "LEX-LBHS" in params


def test_filter_lex_lla():
    sql, params = build_sub_entity_filter("LEX-LLA")
    assert "LEX-LLA" in params


def test_filter_returns_none_for_non_lex():
    assert build_sub_entity_filter("F3E") is None
    assert build_sub_entity_filter("OSN") is None
    assert build_sub_entity_filter("LEX") is None  # GM level — no sub-entity scoping
    assert build_sub_entity_filter("FNDR") is None


# ── _tag_asana_sub_entity_for_project ─────────────────────────────────────────

def test_asana_llc_by_team_gid():
    project = {"name": "LLC - Ops", "team": {"gid": "1209152915815732", "name": "LLC"}}
    assert _tag_asana_sub_entity_for_project(project) == "LEX-LLC"


def test_asana_lla_by_team_gid():
    project = {"name": "LLA - Scheduling", "team": {"gid": "1209152923740446", "name": "LLA"}}
    assert _tag_asana_sub_entity_for_project(project) == "LEX-LLA"


def test_asana_lbhs_by_team_gid():
    project = {"name": "LBHS - Compliance", "team": {"gid": "1209152923740451", "name": "LBHS"}}
    assert _tag_asana_sub_entity_for_project(project) == "LEX-LBHS"


def test_asana_lts_by_name_keyword():
    project = {"name": "Lexington Therapies - Q3 Goals", "team": {"gid": "9999", "name": "LEX"}}
    assert _tag_asana_sub_entity_for_project(project) == "LEX-LTS"


def test_asana_lts_by_lts_keyword():
    project = {"name": "LTS Scheduling", "team": {"gid": "9999", "name": "LEX"}}
    assert _tag_asana_sub_entity_for_project(project) == "LEX-LTS"


def test_asana_lts_by_therapies_keyword():
    project = {"name": "Therapies Staff Planning", "team": {"gid": "9999", "name": "LEX"}}
    assert _tag_asana_sub_entity_for_project(project) == "LEX-LTS"


def test_asana_unknown_team_returns_none():
    project = {"name": "LEX - General Ops", "team": {"gid": "999000", "name": "LEX"}}
    assert _tag_asana_sub_entity_for_project(project) is None


def test_asana_no_team_returns_none():
    project = {"name": "LEX - Staff", "team": None}
    assert _tag_asana_sub_entity_for_project(project) is None


# ── _tag_fireflies_sub_entity ─────────────────────────────────────────────────

def _make_transcript(*attendees: tuple[str, str]) -> dict:
    return {
        "meeting_attendees": [
            {"displayName": name, "email": email}
            for name, email in attendees
        ]
    }


def test_fireflies_lts_by_name():
    t = _make_transcript(("Justin Gilmore", "justin@lexington.com"), ("Harrison", "h@hjr.com"))
    assert _tag_fireflies_sub_entity(t) == "LEX-LTS"


def test_fireflies_lts_by_email():
    t = _make_transcript(("JG", "justin.gilmore@lexington.com"), ("Harrison", "h@hjr.com"))
    assert _tag_fireflies_sub_entity(t) == "LEX-LTS"


def test_fireflies_lbhs_by_name():
    t = _make_transcript(("Jared Harker", "jared@lbhs.com"))
    assert _tag_fireflies_sub_entity(t) == "LEX-LBHS"


def test_fireflies_lla_by_name():
    t = _make_transcript(("Sandy Patel", "sandy@lla.com"))
    assert _tag_fireflies_sub_entity(t) == "LEX-LLA"


def test_fireflies_llc_via_shaun():
    t = _make_transcript(("Shaun Hawkins", "shaun.hawkins@lex.com"), ("Harrison", "h@hjr.com"))
    assert _tag_fireflies_sub_entity(t) == "LEX-LLC"


def test_fireflies_cross_sub_entity_returns_none():
    """Meeting with Justin (LTS) and Jared (LBHS) → cross-sub-entity, no tag."""
    t = _make_transcript(
        ("Justin Gilmore", "justin@lex.com"),
        ("Jared Harker", "jared@lex.com"),
    )
    assert _tag_fireflies_sub_entity(t) is None


def test_fireflies_unknown_attendees_returns_none():
    t = _make_transcript(("Alice Smith", "alice@example.com"))
    assert _tag_fireflies_sub_entity(t) is None


def test_fireflies_empty_attendees_returns_none():
    t = {"meeting_attendees": []}
    assert _tag_fireflies_sub_entity(t) is None
