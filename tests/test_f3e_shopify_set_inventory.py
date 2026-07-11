"""Tests for the f3e_shopify_set_inventory staged-write tool.

Coverage:
  - scope guard (F3E / founder allowed; other non-founder channels blocked)
  - quantity / product / location validation
  - Phase 1 preview (WRITE_PREVIEW, current -> target, expected_current handshake)
  - refuse-by-default location allowlist (synced location refused, office allowed)
  - location resolve miss / ambiguity
  - variant resolve miss / ambiguity (never guess)
  - un-stocked item at location
  - Phase 2 confirmed write (set_inventory_level called, WRITE_CONFIRMED, audit)
  - optimistic-concurrency re-preview (live != expected_current -> no write)
  - confirmed without expected_current -> fresh preview, no write
  - write failure -> graceful message, no crash
  - source-opacity (no platform/store name in any output)
  - allowlist loader fail-closed (missing file -> refuse all)
  - wiring (TOOL_DEFINITIONS / _TOOL_FUNCTIONS / _ENTITY_TOOLS / _TOOL_TIMEOUTS /
    tools_for_entity)
"""

from __future__ import annotations

import json
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora.connectors import shopify_client
from cora.connectors.shopify_client import (
    ShopifyConnectorError,
    VariantMatch,
)
from cora.tools import tool_dispatch
from cora.tools.tool_dispatch import (
    TOOL_DEFINITIONS,
    _ENTITY_TOOLS,
    _TOOL_FUNCTIONS,
    _TOOL_TIMEOUTS,
    _tool_f3e_shopify_set_inventory,
    tools_for_entity,
)

# ── constants ─────────────────────────────────────────────────────────────────

_HARRISON = tool_dispatch._HARRISON_SLACK_ID
_ALEX = "U0B3VGWJTMJ"  # a non-Harrison F3E user
_OFFICE = "1337 S Gilbert Rd"
_LOCS = [
    {"id": 81567023424, "name": _OFFICE},
    {"id": 110064533824, "name": "Nimbl"},
]
# (allowed_names_lc, alias_lc -> canonical_name_lc)
_CONFIG = (frozenset({_OFFICE.lower()}), {"office": _OFFICE.lower(), "the office": _OFFICE.lower()})

_PURE = VariantMatch(
    product_title="F3 Pure Original", variant_title="12 Pack",
    sku="PU-ORIG-12", variant_id=11, inventory_item_id=111,
)


def _stub(
    stack: ExitStack,
    *,
    locations=None,
    variants=None,
    current=132,
    set_result=240,
    config=None,
):
    """Patch the tool's connector + allowlist dependencies with happy defaults."""
    stack.enter_context(patch.object(
        shopify_client, "get_active_locations",
        return_value=list(_LOCS if locations is None else locations)))
    stack.enter_context(patch.object(
        shopify_client, "resolve_variants",
        return_value=list([_PURE] if variants is None else variants)))
    stack.enter_context(patch.object(
        shopify_client, "get_inventory_level", return_value=current))
    m_set = stack.enter_context(patch.object(
        shopify_client, "set_inventory_level", return_value=set_result))
    stack.enter_context(patch.object(
        tool_dispatch, "_load_shopify_write_config",
        return_value=(_CONFIG if config is None else config)))
    return m_set


def _call(entity="F3E", user=_ALEX, **kwargs) -> str:
    base = {"_channel_name": "f3e-leadership"}
    base.update(kwargs)
    return _tool_f3e_shopify_set_inventory(user, entity, base)


# ── scope guard ─────────────────────────────────────────────────────────────

class TestScope:
    def test_non_founder_osn_blocked(self):
        r = _call(entity="OSN", user=_ALEX, product="Pure", location="office", quantity=10)
        assert "blocked" in r.lower()

    def test_lex_blocked(self):
        r = _call(entity="LEX-LLC", user=_ALEX, product="Pure", location="office", quantity=10)
        assert "blocked" in r.lower()

    def test_f3e_allowed(self):
        with ExitStack() as s:
            _stub(s)
            r = _call(entity="F3E", product="pure original 12", location="office", quantity=240)
            assert "blocked" not in r.lower()
            assert "WRITE_PREVIEW" in r

    def test_founder_from_other_channel_allowed(self):
        """Harrison (cross-entity) can set F3E inventory from any channel."""
        with ExitStack() as s:
            _stub(s)
            r = _call(entity="OSN", user=_HARRISON, product="pure original 12",
                      location="office", quantity=240)
            assert "blocked" not in r.lower()
            assert "WRITE_PREVIEW" in r

    def test_fndr_channel_allowed(self):
        with ExitStack() as s:
            _stub(s)
            r = _call(entity="FNDR", product="pure original 12", location="office", quantity=240)
            assert "blocked" not in r.lower()


# ── input validation ────────────────────────────────────────────────────────

class TestValidation:
    def test_missing_quantity(self):
        r = _call(product="pure", location="office")
        assert "quantity" in r.lower()

    def test_non_integer_quantity(self):
        r = _call(product="pure", location="office", quantity="lots")
        assert "whole number" in r.lower()

    def test_negative_quantity(self):
        r = _call(product="pure", location="office", quantity=-5)
        assert "negative" in r.lower()

    def test_missing_product(self):
        r = _call(location="office", quantity=10)
        assert "product" in r.lower()

    def test_missing_location(self):
        r = _call(product="pure", quantity=10)
        assert "location" in r.lower()


# ── Phase 1 preview ─────────────────────────────────────────────────────────

class TestPreview:
    def test_preview_shows_current_and_target(self):
        with ExitStack() as s:
            _stub(s, current=132)
            r = _call(product="pure original 12", location="office", quantity=240)
            assert "WRITE_PREVIEW" in r
            assert "F3 Pure Original (12 Pack)" in r
            assert _OFFICE in r
            assert "132" in r and "240" in r
            assert "expected_current=132" in r

    def test_preview_does_not_write(self):
        with ExitStack() as s:
            m_set = _stub(s)
            _call(product="pure original 12", location="office", quantity=240)
            m_set.assert_not_called()

    def test_preview_source_opaque(self):
        with ExitStack() as s:
            _stub(s)
            r = _call(product="pure original 12", location="office", quantity=240)
            assert "shopify" not in r.lower()
            assert "myshopify" not in r.lower()


# ── location allowlist (refuse-by-default) ──────────────────────────────────

class TestLocationAllowlist:
    def test_synced_location_refused(self):
        with ExitStack() as s:
            m_set = _stub(s)
            r = _call(product="pure original 12", location="nimbl", quantity=240)
            assert "can't set" in r.lower() or "cannot set" in r.lower() or "can't" in r.lower()
            assert "nimbl" in r.lower()
            m_set.assert_not_called()
            assert "shopify" not in r.lower()

    def test_refusal_names_allowed_locations(self):
        with ExitStack() as s:
            _stub(s)
            r = _call(product="pure original 12", location="nimbl", quantity=240)
            # points the user at the manually-writable location(s)
            assert "gilbert" in r.lower()

    def test_empty_allowlist_refuses_office_too(self):
        # Direct location name (resolves regardless of aliases) + empty allowlist
        # -> even the office is refused.
        with ExitStack() as s:
            _stub(s, config=(frozenset(), {}))
            r = _call(product="pure original 12", location=_OFFICE, quantity=240)
            assert "can't" in r.lower() or "cannot" in r.lower()


# ── resolution misses ───────────────────────────────────────────────────────

class TestResolution:
    def test_location_not_found(self):
        with ExitStack() as s:
            _stub(s)
            r = _call(product="pure original 12", location="atlantis", quantity=240)
            assert "couldn't pin down" in r.lower() or "known locations" in r.lower()
            # lists known locations
            assert "Nimbl" in r

    def test_location_ambiguous(self):
        locs = [{"id": 1, "name": "East Warehouse"}, {"id": 2, "name": "West Warehouse"}]
        with ExitStack() as s:
            _stub(s, locations=locs)
            r = _call(product="pure original 12", location="warehouse", quantity=240)
            assert "known locations" in r.lower() or "couldn't pin down" in r.lower()

    def test_variant_no_match(self):
        with ExitStack() as s:
            _stub(s, variants=[])
            r = _call(product="unicorn juice", location="office", quantity=240)
            assert "couldn't find" in r.lower()
            assert "guess" in r.lower()

    def test_variant_ambiguous(self):
        v2 = VariantMatch(product_title="F3 Pure Original", variant_title="6 Pack",
                          sku="PU-ORIG-6", variant_id=12, inventory_item_id=112)
        with ExitStack() as s:
            m_set = _stub(s, variants=[_PURE, v2])
            r = _call(product="pure original", location="office", quantity=240)
            assert "2 variants" in r or "which one" in r.lower()
            assert "PU-ORIG-12" in r and "PU-ORIG-6" in r
            m_set.assert_not_called()

    def test_item_not_stocked_at_location(self):
        with ExitStack() as s:
            _stub(s, current=None)
            r = _call(product="pure original 12", location="office", quantity=240)
            assert "isn't stocked" in r.lower() or "not stocked" in r.lower()


# ── Phase 2 confirmed write ─────────────────────────────────────────────────

class TestConfirmedWrite:
    def test_confirmed_writes_and_confirms(self, monkeypatch, tmp_path):
        monkeypatch.setattr(tool_dispatch, "_SHOPIFY_WRITE_AUDIT_PATH",
                            tmp_path / "writes.jsonl")
        with ExitStack() as s:
            m_set = _stub(s, current=132, set_result=240)
            r = _call(product="pure original 12", location="office",
                      quantity=240, confirmed=True, expected_current=132)
            m_set.assert_called_once_with(111, 81567023424, 240)
            assert "WRITE_CONFIRMED" in r
            assert "132" in r and "240" in r
            assert "F3 Pure Original (12 Pack)" in r
            assert "shopify" not in r.lower()

    def test_confirmed_writes_audit_line(self, monkeypatch, tmp_path):
        audit = tmp_path / "writes.jsonl"
        monkeypatch.setattr(tool_dispatch, "_SHOPIFY_WRITE_AUDIT_PATH", audit)
        with ExitStack() as s:
            _stub(s, current=132, set_result=240)
            _call(user=_ALEX, product="pure original 12", location="office",
                  quantity=240, confirmed=True, expected_current=132)
        lines = audit.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["slack_user"] == _ALEX
        assert rec["channel"] == "f3e-leadership"
        assert rec["variant"] == "F3 Pure Original (12 Pack)"
        assert rec["location"] == _OFFICE
        assert rec["old"] == 132
        assert rec["new"] == 240

    def test_concurrency_repreview_when_count_moved(self):
        with ExitStack() as s:
            m_set = _stub(s, current=200)   # live moved to 200
            r = _call(product="pure original 12", location="office",
                      quantity=240, confirmed=True, expected_current=132)  # stale
            m_set.assert_not_called()
            assert "WRITE_PREVIEW" in r
            assert "200" in r          # shows the fresh current
            assert "expected_current=200" in r

    def test_confirmed_without_expected_current_re_previews(self):
        with ExitStack() as s:
            m_set = _stub(s, current=132)
            r = _call(product="pure original 12", location="office",
                      quantity=240, confirmed=True)   # no expected_current
            m_set.assert_not_called()
            assert "WRITE_PREVIEW" in r

    def test_write_failure_is_graceful(self):
        with ExitStack() as s:
            s.enter_context(patch.object(
                shopify_client, "get_active_locations", return_value=list(_LOCS)))
            s.enter_context(patch.object(
                shopify_client, "resolve_variants", return_value=[_PURE]))
            s.enter_context(patch.object(
                shopify_client, "get_inventory_level", return_value=132))
            s.enter_context(patch.object(
                shopify_client, "set_inventory_level",
                side_effect=ShopifyConnectorError("boom")))
            s.enter_context(patch.object(
                tool_dispatch, "_load_shopify_write_config", return_value=_CONFIG))
            r = _call(product="pure original 12", location="office",
                      quantity=240, confirmed=True, expected_current=132)
            assert "didn't go through" in r.lower() or "not changed" in r.lower()
            assert "shopify" not in r.lower()


# ── connector-error handling ────────────────────────────────────────────────

class TestConnectorErrors:
    def test_location_fetch_error_soft_fails(self):
        with ExitStack() as s:
            s.enter_context(patch.object(
                shopify_client, "get_active_locations",
                side_effect=ShopifyConnectorError("down")))
            r = _call(product="pure", location="office", quantity=10)
            assert "don't have that" in r.lower()


# ── allowlist loader ────────────────────────────────────────────────────────

class TestAllowlistLoader:
    def test_real_seed_allows_office_refuses_nimbl(self):
        allowed, aliases = tool_dispatch._load_shopify_write_config()
        assert _OFFICE.lower() in allowed
        assert "nimbl" not in allowed
        # "office" alias resolves to the office canonical name
        assert aliases.get("office") == _OFFICE.lower()

    def test_missing_file_fails_closed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(tool_dispatch, "_SHOPIFY_WRITE_LOC_PATH",
                            tmp_path / "does-not-exist.yaml")
        assert tool_dispatch._load_shopify_write_config() == (frozenset(), {})

    def test_broken_yaml_fails_closed(self, monkeypatch, tmp_path):
        p = tmp_path / "broken.yaml"
        p.write_text("allowed_write_locations: [unterminated", encoding="utf-8")
        monkeypatch.setattr(tool_dispatch, "_SHOPIFY_WRITE_LOC_PATH", p)
        assert tool_dispatch._load_shopify_write_config() == (frozenset(), {})

    def test_bare_string_entry_supported(self, monkeypatch, tmp_path):
        p = tmp_path / "bare.yaml"
        p.write_text('allowed_write_locations: ["Warehouse A"]', encoding="utf-8")
        monkeypatch.setattr(tool_dispatch, "_SHOPIFY_WRITE_LOC_PATH", p)
        allowed, aliases = tool_dispatch._load_shopify_write_config()
        assert allowed == frozenset({"warehouse a"})
        assert aliases == {}


# ── wiring ──────────────────────────────────────────────────────────────────

class TestWiring:
    def _def(self):
        return next((t for t in TOOL_DEFINITIONS if t["name"] == "f3e_shopify_set_inventory"), None)

    def test_definition_exists(self):
        assert self._def() is not None

    def test_definition_required_fields(self):
        d = self._def()
        props = d["input_schema"]["properties"]
        required = d["input_schema"]["required"]
        for f in ("product", "location", "quantity", "confirmed", "expected_current"):
            assert f in props
        for f in ("product", "location", "quantity", "confirmed"):
            assert f in required

    def test_definition_mentions_staged_write(self):
        assert "STAGED-WRITE" in self._def()["description"].upper()

    def test_registered_in_tool_functions(self):
        assert "f3e_shopify_set_inventory" in _TOOL_FUNCTIONS
        assert callable(_TOOL_FUNCTIONS["f3e_shopify_set_inventory"])

    def test_in_f3e_entity_set(self):
        assert "f3e_shopify_set_inventory" in _ENTITY_TOOLS["F3E"]

    def test_has_timeout(self):
        assert _TOOL_TIMEOUTS.get("f3e_shopify_set_inventory") == 20

    def test_offered_to_f3e_not_lex(self):
        f3e_names = {t["name"] for t in tools_for_entity("F3E")}
        lex_names = {t["name"] for t in tools_for_entity("LEX")}
        assert "f3e_shopify_set_inventory" in f3e_names
        assert "f3e_shopify_set_inventory" not in lex_names

    def test_offered_cross_entity_to_founder(self):
        names = {t["name"] for t in tools_for_entity("OSN", cross_entity=True)}
        assert "f3e_shopify_set_inventory" in names

    def test_not_in_verbatim_table_tools(self):
        """Its preview/confirm output must be sanitized, not passed through verbatim."""
        assert "f3e_shopify_set_inventory" not in tool_dispatch.VERBATIM_TABLE_TOOLS
