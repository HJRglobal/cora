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
