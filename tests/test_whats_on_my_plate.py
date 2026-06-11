"""Tests for whats_on_my_plate (Org Synthesis Phase 2, deliverable 1).

Invariants under test:
- dispatch wiring: TOOL_DEFINITIONS + _TOOL_FUNCTIONS + heavy timeout tier +
  global-core exposure (every entity channel and DMs)
- registry-driven scoping: role + lanes come from org_roles; unknown user gets
  a graceful fail-closed refusal with NO data fetched
- own-plate-only: the `person` parameter is Harrison-only
- external users (e.g. Jason Dorfman) get role scope only, no internal pulls
- LEX scope never gets a HubSpot section (Tier-1 doctrine)
- every entity system prompt carries the mandatory tool-call section
"""

from pathlib import Path
from unittest.mock import patch

import pytest

import cora.tools.tool_dispatch as td
from cora.org_roles import RoleRecord

HARRISON = "U0B2RM2JYJ1"
REPO_ROOT = Path(td.__file__).resolve().parents[3]


def _role(**kw) -> RoleRecord:
    base = dict(
        slack_id="U_TEST",
        name="Test User",
        role="Test Role",
        entity="F3E",
        responsibilities=["Lane one", "Lane two"],
    )
    base.update(kw)
    return RoleRecord(**base)


# --------------------------------------------------------------------------- #
# dispatch wiring
# --------------------------------------------------------------------------- #

def test_tool_definition_registered():
    names = [t["name"] for t in td.TOOL_DEFINITIONS]
    assert "whats_on_my_plate" in names


def test_tool_function_registered():
    assert td._TOOL_FUNCTIONS["whats_on_my_plate"] is td._tool_whats_on_my_plate


def test_heavy_timeout_tier():
    assert td._TOOL_TIMEOUTS["whats_on_my_plate"] == 25


def test_in_global_core():
    assert "whats_on_my_plate" in td._GLOBAL_CORE_TOOLS


@pytest.mark.parametrize(
    "entity",
    ["FNDR", "HJRG", "F3E", "OSN", "LEX", "LEX-LLC", "HJRP", "BDM", "UFL", "F3C", "HJRPROD", "OSNGW"],
)
def test_exposed_for_every_entity(entity):
    names = {t["name"] for t in td.tools_for_entity(entity)}
    assert "whats_on_my_plate" in names


def test_person_param_optional():
    defn = next(t for t in td.TOOL_DEFINITIONS if t["name"] == "whats_on_my_plate")
    assert defn["input_schema"]["required"] == []
    assert "person" in defn["input_schema"]["properties"]


def test_asana_get_my_tasks_defers_plate_phrase():
    # The old description claimed "what's on my plate" as its trigger; it must
    # now route that phrase to the new tool.
    defn = next(t for t in td.TOOL_DEFINITIONS if t["name"] == "asana_get_my_tasks")
    assert "whats_on_my_plate" in defn["description"]


# --------------------------------------------------------------------------- #
# fail-closed: unknown user
# --------------------------------------------------------------------------- #

def test_unknown_user_refused_no_data():
    with patch.object(td.org_roles, "get_role", return_value=None), \
         patch.object(td, "_plate_asana_section") as asana, \
         patch.object(td, "_plate_calendar_section") as cal, \
         patch.object(td, "_plate_hubspot_section") as hs:
        out = td._tool_whats_on_my_plate("U_UNKNOWN", "F3E", {})
    assert "org role registry" in out
    asana.assert_not_called()
    cal.assert_not_called()
    hs.assert_not_called()


def test_unknown_user_message_mentions_harrison_can_add():
    with patch.object(td.org_roles, "get_role", return_value=None):
        out = td._tool_whats_on_my_plate("U_UNKNOWN", "F3E", {})
    assert "Harrison" in out


# --------------------------------------------------------------------------- #
# own-plate composite
# --------------------------------------------------------------------------- #

def test_known_user_composite_sections():
    with patch.object(td.org_roles, "get_role", return_value=_role()), \
         patch.object(td, "_plate_asana_section", return_value="TASKS_BODY") as asana, \
         patch.object(td, "_plate_calendar_section", return_value="CAL_BODY") as cal, \
         patch.object(td, "_plate_hubspot_section", return_value="DEALS_BODY") as hs:
        out = td._tool_whats_on_my_plate("U_TEST", "F3E", {})
    assert "Test Role (F3E)" in out
    assert "Lane one; Lane two" in out
    assert "OPEN TASKS\nTASKS_BODY" in out
    assert "CALENDAR\nCAL_BODY" in out
    assert "DEAL PIPELINE\nDEALS_BODY" in out
    asana.assert_called_once_with("U_TEST", "F3E")
    cal.assert_called_once_with("U_TEST")
    hs.assert_called_once_with("U_TEST", "F3E")


def test_non_deal_owner_omits_pipeline_section():
    with patch.object(td.org_roles, "get_role", return_value=_role()), \
         patch.object(td, "_plate_asana_section", return_value="T"), \
         patch.object(td, "_plate_calendar_section", return_value="C"), \
         patch.object(td, "_plate_hubspot_section", return_value=None):
        out = td._tool_whats_on_my_plate("U_TEST", "OSN", {})
    assert "DEAL PIPELINE" not in out


def test_non_harrison_gets_no_stalled_decisions():
    with patch.object(td.org_roles, "get_role", return_value=_role()), \
         patch.object(td, "_plate_asana_section", return_value="T"), \
         patch.object(td, "_plate_calendar_section", return_value="C"), \
         patch.object(td, "_plate_hubspot_section", return_value=None), \
         patch.object(td, "_tool_fndr_open_decisions") as decisions:
        td._tool_whats_on_my_plate("U_TEST", "F3E", {})
    decisions.assert_not_called()


def test_harrison_own_plate_includes_stalled_decisions():
    with patch.object(td.org_roles, "get_role", return_value=_role(slack_id=HARRISON, entity="FNDR")), \
         patch.object(td, "_plate_asana_section", return_value="T"), \
         patch.object(td, "_plate_calendar_section", return_value="C"), \
         patch.object(td, "_plate_hubspot_section", return_value=None), \
         patch.object(td, "_tool_fndr_open_decisions", return_value="DECISIONS_BODY") as decisions:
        out = td._tool_whats_on_my_plate(HARRISON, "FNDR", {})
    assert "STALLED DECISIONS\nDECISIONS_BODY" in out
    decisions.assert_called_once()


# --------------------------------------------------------------------------- #
# person param: Harrison-only
# --------------------------------------------------------------------------- #

def test_other_person_refused_for_non_harrison():
    with patch.object(td.org_roles, "get_role", return_value=_role()), \
         patch.object(td, "_plate_asana_section") as asana, \
         patch.object(td, "resolve_name_to_slack_user_id") as resolver:
        out = td._tool_whats_on_my_plate("U_TEST", "F3E", {"person": "Tommy"})
    assert "Harrison-only" in out
    asana.assert_not_called()
    resolver.assert_not_called()


def test_harrison_override_resolves_target():
    with patch.object(td, "resolve_name_to_slack_user_id", return_value=("U_TARGET", "Tommy Anderson")) as resolver, \
         patch.object(td.org_roles, "get_role", return_value=_role(slack_id="U_TARGET", name="Tommy Anderson")), \
         patch.object(td, "_plate_asana_section", return_value="T") as asana, \
         patch.object(td, "_plate_calendar_section", return_value="C"), \
         patch.object(td, "_plate_hubspot_section", return_value=None):
        out = td._tool_whats_on_my_plate(HARRISON, "F3E", {"person": "Tommy"})
    resolver.assert_called_once_with("Tommy", channel_entity="F3E")
    asana.assert_called_once_with("U_TARGET", "F3E")
    assert "Tommy Anderson" in out


def test_harrison_override_unresolvable_name():
    with patch.object(td, "resolve_name_to_slack_user_id", return_value=(None, None)), \
         patch.object(td, "_plate_asana_section") as asana:
        out = td._tool_whats_on_my_plate(HARRISON, "F3E", {"person": "Zorp"})
    assert "Zorp" in out
    asana.assert_not_called()


def test_harrison_override_unmapped_target_role():
    with patch.object(td, "resolve_name_to_slack_user_id", return_value=("U_TARGET", "New Hire")), \
         patch.object(td.org_roles, "get_role", return_value=None), \
         patch.object(td, "_plate_asana_section") as asana:
        out = td._tool_whats_on_my_plate(HARRISON, "F3E", {"person": "New Hire"})
    assert "org role registry" in out
    asana.assert_not_called()


# --------------------------------------------------------------------------- #
# external users
# --------------------------------------------------------------------------- #

def test_external_user_no_internal_pulls():
    rec = _role(name="Jason Dorfman", role="Outside Consultant", external=True)
    with patch.object(td.org_roles, "get_role", return_value=rec), \
         patch.object(td, "_plate_asana_section") as asana, \
         patch.object(td, "_plate_calendar_section") as cal, \
         patch.object(td, "_plate_hubspot_section") as hs:
        out = td._tool_whats_on_my_plate("U_EXT", "F3E", {})
    assert "EXTERNAL" in out
    assert "Jason Dorfman" in out
    asana.assert_not_called()
    cal.assert_not_called()
    hs.assert_not_called()


def test_real_registry_jason_is_external():
    # Registry-driven: the live org-roles.yaml marks Jason Dorfman external.
    td.org_roles.invalidate_cache()
    rec = td.org_roles.get_role("U0B6LQNSR25")
    assert rec is not None and rec.external
    with patch.object(td, "_plate_asana_section") as asana:
        out = td._tool_whats_on_my_plate("U0B6LQNSR25", "F3E", {})
    assert "EXTERNAL" in out
    asana.assert_not_called()


def test_real_registry_harrison_role():
    td.org_roles.invalidate_cache()
    rec = td.org_roles.get_role(HARRISON)
    assert rec is not None
    assert rec.entity == "FNDR"
    assert not rec.external


# --------------------------------------------------------------------------- #
# section helpers
# --------------------------------------------------------------------------- #

def test_asana_section_unmapped_user():
    with patch.object(td, "_load_slack_asana_map", return_value={}):
        out = td._plate_asana_section("U_X", "F3E")
    assert "unavailable" in out


def test_asana_section_error_fail_soft():
    mapping = {"U_X": {"asana_user_gid": "123", "asana_email": "x@hjrglobal.com"}}
    with patch.object(td, "_load_slack_asana_map", return_value=mapping), \
         patch.object(td.asana_client, "get_user_tasks", side_effect=td.asana_client.AsanaClientError("boom")):
        out = td._plate_asana_section("U_X", "F3E")
    assert "unavailable" in out


def test_asana_section_entity_filter_applied():
    mapping = {"U_X": {"asana_user_gid": "123"}}
    tasks = [
        {"name": "a", "projects": [{"name": "[F3E] Sales"}]},
        {"name": "b", "projects": [{"name": "[OSN] Ops"}]},
    ]
    with patch.object(td, "_load_slack_asana_map", return_value=mapping), \
         patch.object(td.asana_client, "get_user_tasks", return_value=tasks), \
         patch.object(td.asana_client, "format_tasks_for_llm", return_value="OK") as fmt:
        td._plate_asana_section("U_X", "F3E")
    filtered = fmt.call_args[0][0]
    assert [t["name"] for t in filtered] == ["a"]
    assert fmt.call_args.kwargs["entity_scope"] == "F3E"
    assert fmt.call_args.kwargs["total_before_filter"] == 2


def test_calendar_section_no_email():
    with patch.object(td, "_load_slack_asana_map", return_value={"U_X": {"asana_user_gid": "1"}}):
        out = td._plate_calendar_section("U_X")
    assert "unavailable" in out


def test_calendar_section_today_and_tomorrow():
    mapping = {"U_X": {"asana_email": "x@hjrglobal.com"}}
    with patch.object(td, "_load_slack_asana_map", return_value=mapping), \
         patch.object(td.calendar_client, "get_user_events", return_value=([], "label")) as get_ev, \
         patch.object(td.calendar_client, "format_events_for_llm", return_value="none"):
        td._plate_calendar_section("U_X")
    whens = [c.kwargs.get("when") or c.args[1] for c in get_ev.call_args_list]
    assert whens == ["today", "tomorrow"]


def test_calendar_section_error_fail_soft():
    mapping = {"U_X": {"asana_email": "x@hjrglobal.com"}}
    with patch.object(td, "_load_slack_asana_map", return_value=mapping), \
         patch.object(td.calendar_client, "get_user_events", side_effect=td.calendar_client.CalendarClientError("boom")):
        out = td._plate_calendar_section("U_X")
    assert "today" in out and "tomorrow" in out


@pytest.mark.parametrize("entity", ["LEX", "LEX-LLC", "LEX-LTS", "LEX-LBHS", "LEX-LLA"])
def test_hubspot_section_lex_scope_always_omitted(entity):
    mapping = {"U_X": {"hubspot_owner_id": "999"}}
    with patch.object(td, "_load_slack_hubspot_map", return_value=mapping), \
         patch.object(td.hubspot_client, "get_owner_deals") as deals:
        assert td._plate_hubspot_section("U_X", entity) is None
    deals.assert_not_called()


def test_hubspot_section_non_owner_omitted():
    with patch.object(td, "_load_slack_hubspot_map", return_value={}):
        assert td._plate_hubspot_section("U_X", "F3E") is None


def test_hubspot_section_owner_pipeline_scoped():
    mapping = {"U_X": {"hubspot_owner_id": "999"}}
    with patch.object(td, "_load_slack_hubspot_map", return_value=mapping), \
         patch.object(td.hubspot_client, "get_owner_deals", return_value=[]) as deals, \
         patch.object(td.hubspot_client, "format_deals_for_llm", return_value="DEALS") as fmt:
        out = td._plate_hubspot_section("U_X", "F3E")
    assert out == "DEALS"
    deals.assert_called_once_with("999", pipeline_id=td.HUBSPOT_PIPELINE_BY_ENTITY["F3E"])
    assert fmt.call_args.kwargs["pipeline_filter_applied"] is True


def test_hubspot_section_error_fail_soft():
    mapping = {"U_X": {"hubspot_owner_id": "999"}}
    with patch.object(td, "_load_slack_hubspot_map", return_value=mapping), \
         patch.object(td.hubspot_client, "get_owner_deals", side_effect=td.hubspot_client.HubSpotClientError("boom")):
        out = td._plate_hubspot_section("U_X", "F3E")
    assert out is not None and "Temporary issue" in out


# --------------------------------------------------------------------------- #
# source-opacity + prompts
# --------------------------------------------------------------------------- #

def test_composite_carries_no_financial_figures_instruction():
    with patch.object(td.org_roles, "get_role", return_value=_role()), \
         patch.object(td, "_plate_asana_section", return_value="T"), \
         patch.object(td, "_plate_calendar_section", return_value="C"), \
         patch.object(td, "_plate_hubspot_section", return_value=None):
        out = td._tool_whats_on_my_plate("U_TEST", "F3E", {})
    assert "Do not add financial figures" in out


def test_all_entity_prompts_carry_plate_section():
    prompts_dir = REPO_ROOT / "design" / "system-prompts"
    missing = [
        f.name
        for f in sorted(prompts_dir.glob("*.md"))
        if "## What's on my plate" not in f.read_text(encoding="utf-8")
    ]
    assert not missing, f"prompts missing the plate section: {missing}"


# --------------------------------------------------------------------------- #
# 2026-06-11 live-crash regressions (Cowork bug report)
# --------------------------------------------------------------------------- #

def test_format_tasks_for_llm_nonempty_returns_string():
    # REGRESSION: commit d5f2e6f (2026-06-03) truncated asana_client.py mid-loop;
    # format_tasks_for_llm fell off the end and returned None for every NON-EMPTY
    # task list, crashing whats_on_my_plate live (TypeError on str concat) and
    # silently nulling asana_get_my_tasks for a week. Pin the non-empty path.
    from cora.tools import asana_client
    out = asana_client.format_tasks_for_llm(
        [
            {
                "name": "Task A",
                "due_on": "2026-06-15",
                "permalink_url": "https://app.asana.com/t/1",
                "projects": [{"name": "[F3E] Sales"}],
                "notes": "context",
            },
            {"name": "Task B", "projects": []},
        ]
    )
    assert isinstance(out, str)
    assert "<https://app.asana.com/t/1|Task A>" in out
    assert "Task B" in out


def test_format_tasks_for_llm_scoped_footer_returns_string():
    from cora.tools import asana_client
    out = asana_client.format_tasks_for_llm(
        [{"name": "Task A", "projects": [{"name": "[OSN] Ops"}]}],
        entity_scope="OSN",
        total_before_filter=5,
    )
    assert isinstance(out, str)
    assert "[Scope: showing OSN-tagged tasks only." in out


def test_safe_plate_section_catches_exception():
    def boom(*_args):
        raise RuntimeError("section exploded")
    out = td._safe_plate_section("Open tasks", boom, "U_X", "F3E")
    assert out == "(Open tasks section unavailable right now.)"


def test_safe_plate_section_coerces_none():
    out = td._safe_plate_section("Calendar", lambda *_: None, "U_X")
    assert out == "(Calendar section returned no data.)"


def test_composite_survives_section_crash():
    # The composition site must degrade a crashing section, never raise.
    with patch.object(td.org_roles, "get_role", return_value=_role()), \
         patch.object(td, "_plate_asana_section", side_effect=RuntimeError("boom")), \
         patch.object(td, "_plate_calendar_section", return_value="CAL"), \
         patch.object(td, "_plate_hubspot_section", return_value=None):
        out = td._tool_whats_on_my_plate("U_TEST", "F3E", {})
    assert "OPEN TASKS\n(Open tasks section unavailable right now.)" in out
    assert "CALENDAR\nCAL" in out


def test_composite_survives_section_returning_none():
    with patch.object(td.org_roles, "get_role", return_value=_role()), \
         patch.object(td, "_plate_asana_section", return_value=None), \
         patch.object(td, "_plate_calendar_section", return_value="CAL"), \
         patch.object(td, "_plate_hubspot_section", return_value=None):
        out = td._tool_whats_on_my_plate("U_TEST", "F3E", {})
    assert "OPEN TASKS\n(Open tasks section returned no data.)" in out


def test_composite_survives_hubspot_crash():
    with patch.object(td.org_roles, "get_role", return_value=_role()), \
         patch.object(td, "_plate_asana_section", return_value="T"), \
         patch.object(td, "_plate_calendar_section", return_value="C"), \
         patch.object(td, "_plate_hubspot_section", side_effect=RuntimeError("boom")):
        out = td._tool_whats_on_my_plate("U_TEST", "F3E", {})
    assert "DEAL PIPELINE\n(Deal pipeline section unavailable right now.)" in out


def test_asana_helper_coerces_formatter_none():
    # Belt-and-braces inside the helper itself: a formatter regression returning
    # None must degrade to a counted-but-unrendered message, not None.
    mapping = {"U_X": {"asana_user_gid": "123"}}
    tasks = [{"name": "a", "projects": [{"name": "[F3E] Sales"}]}]
    with patch.object(td, "_load_slack_asana_map", return_value=mapping), \
         patch.object(td.asana_client, "get_user_tasks", return_value=tasks), \
         patch.object(td.asana_client, "format_tasks_for_llm", return_value=None):
        out = td._plate_asana_section("U_X", "F3E")
    assert isinstance(out, str)
    assert "1 open task(s) found" in out


@pytest.mark.parametrize(
    "msg",
    [
        "what's on my plate",
        "What is on my plate today?",
        "whats on my plate",
        "catch me up on my work",
        "what do I have going on today",
        "how's my day looking",
    ],
)
def test_model_router_plate_queries_force_sonnet(msg):
    # REGRESSION: Haiku narrated a degraded plate tool result as "no open tasks"
    # (2026-06-11). Plate queries are multi-source composites -> Sonnet.
    from cora import model_router
    assert model_router.choose_model(msg) == model_router.MODEL_SONNET


def test_model_router_simple_lookup_still_haiku():
    from cora import model_router
    assert model_router.choose_model("list my tasks") == model_router.MODEL_HAIKU
