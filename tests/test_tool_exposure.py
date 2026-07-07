"""Tests for per-entity tool exposure (tools_for_entity + _build_cached_tools).

The map is a performance/tool-selection layer, not a security boundary, so the
key invariants are: every channel keeps the global core; entity channels shed
unrelated tools; aggregators + the founder see everything; sub-entities resolve
to their parent; and the map never references a non-existent tool.
"""

import cora.claude_client as cc
import cora.tools.tool_dispatch as td

ALL_NAMES = {t["name"] for t in td.TOOL_DEFINITIONS}


def _names(entity, cross_entity=False):
    return {t["name"] for t in td.tools_for_entity(entity, cross_entity)}


# --------------------------------------------------------------------------- #
# map integrity
# --------------------------------------------------------------------------- #

def test_map_references_only_real_tools():
    mapped = set(td._GLOBAL_CORE_TOOLS)
    for tools in td._ENTITY_TOOLS.values():
        mapped |= set(tools)
    unknown = mapped - ALL_NAMES
    assert not unknown, f"map references non-existent tools: {unknown}"


# --------------------------------------------------------------------------- #
# global dispatch-registry parity (W3-03)
#
# TOOL_DEFINITIONS (the JSON schemas the LLM sees) and _TOOL_FUNCTIONS (the
# dispatch map) must stay in lock-step: a tool defined but not wired (or wired
# but not defined) is the drift class this audit had to verify by hand. Timeouts
# must reference only real tools. These assert the whole set, not per-tool.
# --------------------------------------------------------------------------- #

def test_definitions_and_functions_are_in_lockstep():
    defined = {t["name"] for t in td.TOOL_DEFINITIONS}
    dispatched = set(td._TOOL_FUNCTIONS)
    assert defined == dispatched, (
        "TOOL_DEFINITIONS and _TOOL_FUNCTIONS diverged. "
        f"defined-but-not-dispatched={defined - dispatched}; "
        f"dispatched-but-not-defined={dispatched - defined}"
    )


def test_every_dispatch_target_is_callable():
    non_callable = {name for name, fn in td._TOOL_FUNCTIONS.items() if not callable(fn)}
    assert not non_callable, f"_TOOL_FUNCTIONS has non-callable targets: {non_callable}"


def test_tool_definitions_have_no_duplicate_names():
    names = [t["name"] for t in td.TOOL_DEFINITIONS]
    dupes = {n for n in names if names.count(n) > 1}
    assert not dupes, f"duplicate tool names in TOOL_DEFINITIONS: {dupes}"


def test_timeouts_reference_only_real_tools():
    # A per-tool timeout for a tool that no longer exists is dead config; every
    # timeout key must be a registered tool. (Not every tool needs an explicit
    # timeout — many deliberately fall back to _DEFAULT_TOOL_TIMEOUT.)
    orphan_timeouts = set(td._TOOL_TIMEOUTS) - set(td._TOOL_FUNCTIONS)
    assert not orphan_timeouts, (
        f"_TOOL_TIMEOUTS references tools not in _TOOL_FUNCTIONS: {orphan_timeouts}"
    )


def test_image_gen_timeouts_exceed_internal_photoroom_budget():
    # W3-02: the dispatch timeout MUST exceed photoroom_client's internal httpx
    # budgets, else a real generation is abandoned mid-flight and the user gets
    # a spurious "Tool timed out" (W3-01 made this a true wall-clock bound). The
    # single-generation POST budget is 60s (photoroom_client.py:254); a spec can
    # also spend up to 30s each on the main + reference image downloads (:177).
    _PHOTOROOM_GENERATION_BUDGET = 60  # httpx.post(..., timeout=60.0)
    for tool in ("f3_generate_image", "f3_create_image", "f3_batch_image_run"):
        assert tool in td._TOOL_TIMEOUTS, f"{tool} must have an explicit timeout (W3-02)"
        assert td._TOOL_TIMEOUTS[tool] > _PHOTOROOM_GENERATION_BUDGET, (
            f"{tool} dispatch timeout {td._TOOL_TIMEOUTS[tool]}s <= the 60s PhotoRoom "
            "generation budget -> a real generation would spuriously time out"
        )
    # f3_create_image adds a Haiku brief->spec step before the same PhotoRoom
    # path, so it must be at least as generous as the plain generator.
    assert td._TOOL_TIMEOUTS["f3_create_image"] >= td._TOOL_TIMEOUTS["f3_generate_image"]
    # batch runs specs in series -> it must be the most generous of the three.
    assert td._TOOL_TIMEOUTS["f3_batch_image_run"] >= td._TOOL_TIMEOUTS["f3_create_image"]


def test_every_tool_reachable_via_aggregator():
    # FNDR is the catch-all; it must expose every tool so nothing is orphaned.
    assert _names("FNDR") == ALL_NAMES


def test_global_core_present_for_every_entity():
    for entity in ["F3E", "OSN", "LEX", "HJRP", "BDM", "UFL", "F3C", "HJRPROD"]:
        got = _names(entity)
        assert td._GLOBAL_CORE_TOOLS <= got, f"{entity} missing core tools"


# --------------------------------------------------------------------------- #
# aggregators + founder
# --------------------------------------------------------------------------- #

def test_aggregators_get_all_tools():
    assert _names("FNDR") == ALL_NAMES
    assert _names("HJRG") == ALL_NAMES


def test_founder_cross_entity_gets_all_tools_from_any_channel():
    # Harrison asking in an OSN channel still gets the full toolset.
    assert _names("OSN", cross_entity=True) == ALL_NAMES
    assert _names("LEX", cross_entity=True) == ALL_NAMES


def test_unknown_entity_falls_back_to_core_only():
    assert _names("WIDGETS-INC") == set(td._GLOBAL_CORE_TOOLS)


# --------------------------------------------------------------------------- #
# entity scoping
# --------------------------------------------------------------------------- #

def test_f3e_has_its_tools_and_sheds_others():
    f3e = _names("F3E")
    assert "f3e_shopify_sales_pulse" in f3e
    assert "ads_get_performance_summary" in f3e
    assert "fighter_compliance" in f3e
    assert "qbo_get_profit_loss" in f3e
    assert "hubspot_get_my_deals" in f3e
    # not F3E's
    assert "lex_revalidation_status" not in f3e
    assert "hjrp_lease_status" not in f3e
    assert "osn_financial_pulse" not in f3e


def test_osn_scope():
    osn = _names("OSN")
    assert "osn_financial_pulse" in osn
    assert "qbo_get_profit_loss" in osn
    assert "hubspot_get_my_deals" in osn
    assert "f3e_shopify_sales_pulse" not in osn
    assert "ads_get_performance_summary" not in osn
    assert "lex_revalidation_status" not in osn


def test_lex_scope_excludes_hubspot():
    lex = _names("LEX")
    assert "lex_revalidation_status" in lex
    assert "qbo_get_profit_loss" in lex
    # HubSpot is blocked for LEX per Tier-1 doctrine
    assert "hubspot_get_my_deals" not in lex
    assert "hubspot_update_deal_stage" not in lex


def test_hjrp_scope():
    hjrp = _names("HJRP")
    assert "hjrp_lease_status" in hjrp
    assert "qbo_get_balance_sheet" in hjrp
    assert "f3e_shopify_inventory" not in hjrp


def test_ufl_has_hubspot_no_qbo():
    ufl = _names("UFL")
    assert "hubspot_get_my_deals" in ufl
    assert "qbo_get_profit_loss" not in ufl  # UFL not QBO-provisioned
    assert "financial_get_cashflow" in ufl   # core still available


def test_lean_entities_get_core_only():
    for entity in ["F3C", "HJRPROD"]:
        assert _names(entity) == set(td._GLOBAL_CORE_TOOLS)


def test_bdm_gets_image_and_hubspot_and_qbo():
    bdm = _names("BDM")
    assert "f3_create_image" in bdm
    assert "hubspot_get_my_deals" in bdm
    assert "qbo_get_profit_loss" in bdm
    assert "ads_get_performance_summary" not in bdm  # Polar is F3E/FNDR scope


# --------------------------------------------------------------------------- #
# sub-entity resolution
# --------------------------------------------------------------------------- #

def test_subentities_resolve_to_parent():
    assert _names("LEX-LLC") == _names("LEX")
    assert _names("OSNGF") == _names("OSN")
    assert _names("HJRP-1337") == _names("HJRP")
    assert _names("HJRP-RR") == _names("HJRP")


# --------------------------------------------------------------------------- #
# ordering + _build_cached_tools
# --------------------------------------------------------------------------- #

def test_tools_for_entity_preserves_definition_order():
    f3e = td.tools_for_entity("F3E")
    f3e_names = [t["name"] for t in f3e]
    full_order = [t["name"] for t in td.TOOL_DEFINITIONS if t["name"] in set(f3e_names)]
    assert f3e_names == full_order


def test_build_cached_tools_entity_scoped_and_smaller():
    f3e = cc._build_cached_tools("F3E")
    fndr = cc._build_cached_tools("FNDR")
    lean = cc._build_cached_tools("F3C")
    assert len(lean) < len(f3e) < len(fndr) == len(td.TOOL_DEFINITIONS)
    # cache_control sits on the last tool only
    assert f3e[-1].get("cache_control") == {"type": "ephemeral"}
    assert all("cache_control" not in t for t in f3e[:-1])


def test_build_cached_tools_default_is_full_set():
    # No-arg / default entity preserves the old behavior (all tools).
    assert len(cc._build_cached_tools()) == len(td.TOOL_DEFINITIONS)


def test_build_cached_tools_cross_entity_overrides_scope():
    scoped = cc._build_cached_tools("LEX")
    full = cc._build_cached_tools("LEX", cross_entity=True)
    assert len(full) == len(td.TOOL_DEFINITIONS)
    assert len(scoped) < len(full)
