"""Tests for the f3e_shopify_set_inventory staged-write tool (2026-07-10 hotfix).

The confirm path is now bound by a SERVER-SIDE pending-confirmation store keyed on
(slack_user, channel) -- NOT an LLM label echo (the D-051 echo-binding made the
write path unreachable: the model normalizes the variant label, so every confirm
re-previewed forever). Phase 1 previews + stashes the resolved write; Phase 2
confirmed=true executes the caller's pending entry after a FRESH live-qty re-check.

Every non-write return is WRITE_BLOCKED-wrapped and leads with "NOT WRITTEN"; the
write return carries WRITE_CONFIRMED. The claude_client narration net posts the
tool's own text so a mis-narrating model can't claim a phantom write.

Audit-path isolation + pending-store clearing are handled suite-wide by the
conftest autouse fixture (MED-3); this file relies on it.
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
from cora.connectors.shopify_client import ShopifyConnectorError, VariantMatch
from cora.tools import tool_dispatch
from cora.tools.tool_dispatch import (
    TOOL_DEFINITIONS,
    _ENTITY_TOOLS,
    _TOOL_FUNCTIONS,
    _TOOL_TIMEOUTS,
    _tool_f3e_shopify_set_inventory,
    has_pending_shopify_write,
    tools_for_entity,
)

_HARRISON = tool_dispatch._HARRISON_SLACK_ID
_ALEX = "U0B3VGWJTMJ"
_CHAN = "f3e-leadership"
_HQ = "f3-hq-inventory-adjustments"   # the channel with a default location + cases unit
_OFFICE = "1337 S Gilbert Rd"
_LOCS = [{"id": 81567023424, "name": _OFFICE}, {"id": 110064533824, "name": "Nimbl"}]
_CONFIG = (frozenset({_OFFICE.lower()}), {"office": _OFFICE.lower(), "the office": _OFFICE.lower()})
_PURE = VariantMatch(
    product_title="F3 PURE Original Energy Drink", variant_title="12 Pack",
    sku="PU-ORIG-12", variant_id=11, inventory_item_id=52999599030592,
)
_PURE_LABEL = "F3 PURE Original Energy Drink (12 Pack)"


def _stub(stack: ExitStack, *, locations=None, variants=None, current=202,
          levels=None, set_result=203, config=None, chan_cfg=None):
    """Patch the connector + allowlist deps. `levels` (list) -> side_effect for
    sequential get_inventory_level calls (preview then confirm); else `current`.
    `chan_cfg` (dict) -> the per-channel inventory default config the tool reads;
    defaults to {} (no channel default) so tests never depend on the real YAML."""
    stack.enter_context(patch.object(shopify_client, "get_active_locations",
                                     return_value=list(_LOCS if locations is None else locations)))
    stack.enter_context(patch.object(shopify_client, "resolve_variants",
                                     return_value=list([_PURE] if variants is None else variants)))
    if levels is not None:
        stack.enter_context(patch.object(shopify_client, "get_inventory_level", side_effect=list(levels)))
    else:
        stack.enter_context(patch.object(shopify_client, "get_inventory_level", return_value=current))
    m_set = stack.enter_context(patch.object(shopify_client, "set_inventory_level", return_value=set_result))
    stack.enter_context(patch.object(tool_dispatch, "_load_shopify_write_config",
                                     return_value=(_CONFIG if config is None else config)))

    # Channel-config is channel-SENSITIVE (side_effect on the name arg) so tests
    # actually exercise channel THREADING -- chan_cfg is returned ONLY for _HQ.
    def _chan_cfg(name):
        n = (name or "").strip().lstrip("#").lower()
        return dict(chan_cfg) if (chan_cfg and n == _HQ) else {}
    stack.enter_context(patch.object(tool_dispatch, "_load_inventory_channel_config",
                                     side_effect=_chan_cfg))
    return m_set


def _preview(entity="F3E", user=_ALEX, **kw) -> str:
    base = {"_channel_name": _CHAN}
    base.update(kw)
    return _tool_f3e_shopify_set_inventory(user, entity, base)


def _confirm(entity="F3E", user=_ALEX, **kw) -> str:
    base = {"_channel_name": _CHAN, "confirmed": True}
    base.update(kw)
    return _tool_f3e_shopify_set_inventory(user, entity, base)


# ── scope ─────────────────────────────────────────────────────────────────────

class TestScope:
    def test_non_founder_osn_blocked(self):
        r = _preview(entity="OSN", product="Pure", location="office", quantity=10)
        assert r.startswith("WRITE_BLOCKED")
        assert "NOT WRITTEN" in r and "F3E channels" in r

    def test_lex_blocked(self):
        r = _preview(entity="LEX-LLC", product="Pure", location="office", quantity=10)
        assert "F3E channels" in r

    def test_f3e_allowed_previews(self):
        with ExitStack() as s:
            _stub(s)
            r = _preview(product="pure original 12", location="office", quantity=203)
            assert r.startswith("WRITE_BLOCKED")
            assert "NOT WRITTEN" in r

    def test_founder_cross_entity_allowed(self):
        with ExitStack() as s:
            _stub(s)
            r = _preview(entity="OSN", user=_HARRISON, product="pure original 12",
                         location="office", quantity=203)
            assert "F3E channels" not in r
            assert "NOT WRITTEN" in r


# ── validation (all non-write, marker-led) ─────────────────────────────────────

class TestValidation:
    def test_missing_quantity(self):
        r = _preview(product="pure", location="office")
        assert r.startswith("WRITE_BLOCKED") and "NOT WRITTEN" in r and "number" in r.lower()

    def test_non_integer_quantity(self):
        r = _preview(product="pure", location="office", quantity="lots")
        assert "whole number" in r.lower()

    def test_negative_quantity(self):
        r = _preview(product="pure", location="office", quantity=-5)
        assert "negative" in r.lower()

    def test_missing_product(self):
        r = _preview(location="office", quantity=10)
        assert "product" in r.lower()

    def test_missing_location(self):
        r = _preview(product="pure", quantity=10)
        assert "location" in r.lower()


# ── Phase 1 preview + pending store ─────────────────────────────────────────────

class TestPreview:
    def test_preview_shows_current_and_target(self):
        with ExitStack() as s:
            _stub(s, current=202)
            r = _preview(product="pure original 12", location="office", quantity=203)
            assert _PURE_LABEL in r
            assert _OFFICE in r
            assert "202 -> 203" in r
            assert "confirm" in r.lower()

    def test_preview_stores_pending(self):
        with ExitStack() as s:
            _stub(s)
            assert not has_pending_shopify_write(_ALEX, _CHAN)
            _preview(product="pure original 12", location="office", quantity=203)
            assert has_pending_shopify_write(_ALEX, _CHAN)

    def test_preview_does_not_write(self):
        with ExitStack() as s:
            m_set = _stub(s)
            _preview(product="pure original 12", location="office", quantity=203)
            m_set.assert_not_called()

    def test_preview_source_opaque(self):
        with ExitStack() as s:
            _stub(s)
            r = _preview(product="pure original 12", location="office", quantity=203)
            assert "shopify" not in r.lower()


# ── location allowlist ──────────────────────────────────────────────────────────

class TestAllowlist:
    def test_synced_location_refused(self):
        with ExitStack() as s:
            m_set = _stub(s)
            r = _preview(product="pure original 12", location="nimbl", quantity=203)
            assert r.startswith("WRITE_BLOCKED") and "NOT WRITTEN" in r
            assert "nimbl" in r.lower()
            assert "shopify" not in r.lower()
            m_set.assert_not_called()
            assert not has_pending_shopify_write(_ALEX, _CHAN)  # refusal stores nothing

    def test_refusal_points_at_office(self):
        with ExitStack() as s:
            _stub(s)
            r = _preview(product="pure original 12", location="nimbl", quantity=203)
            assert "gilbert" in r.lower()

    def test_empty_allowlist_refuses_office(self):
        with ExitStack() as s:
            _stub(s, config=(frozenset(), {}))
            r = _preview(product="pure original 12", location=_OFFICE, quantity=203)
            assert "NOT WRITTEN" in r and _OFFICE in r


# ── resolution misses ────────────────────────────────────────────────────────────

class TestResolution:
    def test_location_not_found(self):
        with ExitStack() as s:
            _stub(s)
            r = _preview(product="pure original 12", location="atlantis", quantity=203)
            assert "NOT WRITTEN" in r and "Nimbl" in r

    def test_variant_no_match(self):
        with ExitStack() as s:
            m_set = _stub(s, variants=[])
            r = _preview(product="unicorn juice", location="office", quantity=203)
            assert "NOT WRITTEN" in r and "couldn't find" in r.lower()
            m_set.assert_not_called()

    def test_variant_ambiguous(self):
        v2 = VariantMatch(product_title="F3 PURE Original Energy Drink", variant_title="6 Pack",
                          sku="PU-ORIG-6", variant_id=12, inventory_item_id=999)
        with ExitStack() as s:
            m_set = _stub(s, variants=[_PURE, v2])
            r = _preview(product="pure original", location="office", quantity=203)
            assert "NOT WRITTEN" in r and ("2 variants" in r or "which one" in r.lower())
            assert "PU-ORIG-12" in r and "PU-ORIG-6" in r
            m_set.assert_not_called()

    def test_item_not_stocked(self):
        with ExitStack() as s:
            _stub(s, current=None)
            r = _preview(product="pure original 12", location="office", quantity=203)
            assert "NOT WRITTEN" in r and "stocked" in r.lower()


# ── Phase 2 confirm (pending store) -- the HIGH-1 regression tests ───────────────

class TestConfirmFlow:
    def test_preview_then_confirm_writes(self):
        with ExitStack() as s:
            m_set = _stub(s, levels=[202, 202], set_result=203)
            _preview(product="pure original 12", location="office", quantity=203)
            r = _confirm(product="pure original 12", location="office", quantity=203)
            m_set.assert_called_once_with(52999599030592, 81567023424, 203)
            assert r.startswith("WRITE_CONFIRMED")
            assert "202 -> 203" in r and _PURE_LABEL in r

    def test_paraphrased_expected_item_no_longer_blocks(self):
        """The exact live bug: the model echoes 'F3 Pure Original (12 Pack)' but the
        Shopify title is 'F3 PURE Original Energy Drink - 12 Pack'. The write must
        STILL happen -- identity is bound server-side, not by the echo."""
        with ExitStack() as s:
            m_set = _stub(s, levels=[202, 202], set_result=203)
            _preview(product="pure original 12", location="office", quantity=203)
            r = _confirm(product="F3 Pure Original", location="office", quantity=203,
                         expected_item="F3 Pure Original (12 Pack)",  # paraphrase
                         expected_current=202, expected_location="office")
            m_set.assert_called_once()
            assert r.startswith("WRITE_CONFIRMED")

    def test_confirm_needs_no_echo(self):
        """A bare confirmed=true (no product/location/quantity/expected_*) executes
        the pending write -- the pending store is the identity binding."""
        with ExitStack() as s:
            m_set = _stub(s, levels=[202, 202], set_result=203)
            _preview(product="pure original 12", location="office", quantity=203)
            r = _confirm()  # nothing but confirmed=True
            m_set.assert_called_once_with(52999599030592, 81567023424, 203)
            assert r.startswith("WRITE_CONFIRMED")

    def test_confirm_clears_pending(self):
        with ExitStack() as s:
            _stub(s, levels=[202, 202])
            _preview(product="pure original 12", location="office", quantity=203)
            assert has_pending_shopify_write(_ALEX, _CHAN)
            _confirm()
            assert not has_pending_shopify_write(_ALEX, _CHAN)

    def test_confirm_writes_audit_line(self):
        with ExitStack() as s:
            _stub(s, levels=[202, 202], set_result=203)
            _preview(product="pure original 12", location="office", quantity=203)
            _confirm()
        lines = tool_dispatch._SHOPIFY_WRITE_AUDIT_PATH.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["slack_user"] == _ALEX and rec["channel"] == _CHAN
        assert rec["variant"] == _PURE_LABEL and rec["location"] == _OFFICE
        assert rec["old"] == 202 and rec["new"] == 203

    def test_confirm_no_pending_re_previews(self):
        with ExitStack() as s:
            m_set = _stub(s, current=202)
            r = _confirm(product="pure original 12", location="office", quantity=203)
            m_set.assert_not_called()
            assert r.startswith("WRITE_BLOCKED") and "NOT WRITTEN" in r

    def test_ttl_expiry_re_previews(self):
        with ExitStack() as s:
            m_set = _stub(s, current=202)
            _preview(product="pure original 12", location="office", quantity=203)
            # Age the pending entry well past its TTL (clock-resolution-independent).
            for entry in tool_dispatch._PENDING_SHOPIFY_WRITES.values():
                entry["ts"] = 0.0
            r = _confirm(product="pure original 12", location="office", quantity=203)
            m_set.assert_not_called()
            assert r.startswith("WRITE_BLOCKED") and "NOT WRITTEN" in r

    def test_quantity_drift_re_previews(self):
        with ExitStack() as s:
            m_set = _stub(s, levels=[202, 250])  # preview sees 202, confirm re-check sees 250
            _preview(product="pure original 12", location="office", quantity=203)
            r = _confirm()
            m_set.assert_not_called()
            assert r.startswith("WRITE_BLOCKED") and "NOT WRITTEN" in r
            assert "250" in r  # shows the fresh live count

    def test_target_change_re_previews(self):
        with ExitStack() as s:
            m_set = _stub(s, levels=[202, 202])
            _preview(product="pure original 12", location="office", quantity=203)
            r = _confirm(product="pure original 12", location="office", quantity=250)  # new target
            m_set.assert_not_called()
            assert r.startswith("WRITE_BLOCKED") and "NOT WRITTEN" in r
            assert "250" in r

    def test_concurrency_repreview_then_confirm_writes(self):
        with ExitStack() as s:
            m_set = _stub(s, levels=[202, 250, 250], set_result=203)
            _preview(product="pure original 12", location="office", quantity=203)
            r1 = _confirm()                 # live moved to 250 -> re-preview, re-store
            assert r1.startswith("WRITE_BLOCKED") and "250" in r1
            m_set.assert_not_called()
            r2 = _confirm()                 # live now stable at 250 -> writes
            m_set.assert_called_once_with(52999599030592, 81567023424, 203)
            assert r2.startswith("WRITE_CONFIRMED")

    def test_changed_quantity_re_previews_from_pending(self):
        """'yes, but make it 210' re-previews the NEW target against the SAME
        resolved ids -- then a follow-up confirm writes it (review #2/#7)."""
        with ExitStack() as s:
            m_set = _stub(s, levels=[202, 202, 202], set_result=210)
            _preview(product="pure original 12", location="office", quantity=203)
            r1 = _confirm(product="pure original 12", location="office", quantity=210)
            m_set.assert_not_called()
            assert r1.startswith("WRITE_BLOCKED") and "210" in r1
            r2 = _confirm()  # bare confirm of the re-previewed 210
            m_set.assert_called_once_with(52999599030592, 81567023424, 210)
            assert r2.startswith("WRITE_CONFIRMED") and "210" in r2

    def test_changed_quantity_without_product_does_not_dead_end(self):
        """The dead-end the review flagged: a changed-qty confirm that omits
        product/location must reuse the pending's ids, not block on 'which product?'."""
        with ExitStack() as s:
            m_set = _stub(s, levels=[202, 202])
            _preview(product="pure original 12", location="office", quantity=203)
            r = _confirm(quantity=210)  # no product/location
            m_set.assert_not_called()
            assert r.startswith("WRITE_BLOCKED") and "210" in r
            assert "which product" not in r.lower() and "couldn't find" not in r.lower()

    def test_write_failure_is_blocked_not_confirmed(self):
        with ExitStack() as s:
            s.enter_context(patch.object(shopify_client, "get_active_locations", return_value=list(_LOCS)))
            s.enter_context(patch.object(shopify_client, "resolve_variants", return_value=[_PURE]))
            s.enter_context(patch.object(shopify_client, "get_inventory_level", return_value=202))
            s.enter_context(patch.object(shopify_client, "set_inventory_level",
                                         side_effect=ShopifyConnectorError("boom")))
            s.enter_context(patch.object(tool_dispatch, "_load_shopify_write_config", return_value=_CONFIG))
            _preview(product="pure original 12", location="office", quantity=203)
            r = _confirm()
            assert r.startswith("WRITE_BLOCKED") and "NOT WRITTEN" in r
            assert "WRITE_CONFIRMED" not in r


# ── crash safety (review #1): a tool crash must be source-opaque + NOT WRITTEN ──

class TestCrashSafety:
    def test_unexpected_exception_returns_source_opaque_not_written(self):
        # A non-Shopify error (e.g. a bad response shape) is NOT caught by the inner
        # ShopifyConnectorError handlers -> must be caught by the top-level wrapper.
        with ExitStack() as s:
            s.enter_context(patch.object(shopify_client, "get_active_locations",
                                         side_effect=ValueError("kaboom in resolve")))
            s.enter_context(patch.object(tool_dispatch, "_load_shopify_write_config", return_value=_CONFIG))
            r = _preview(product="pure original 12", location="office", quantity=203)
        assert r.startswith("WRITE_BLOCKED")
        assert "NOT WRITTEN" in r
        assert "shopify" not in r.lower()
        assert "kaboom" not in r.lower()  # raw exception text not surfaced


# ── the WRITE_BLOCKED / WRITE_CONFIRMED contract (for the narration net) ─────────

class TestContract:
    def test_every_nonwrite_is_write_blocked_with_marker(self):
        with ExitStack() as s:
            _stub(s)
            samples = [
                _preview(entity="OSN", product="p", location="office", quantity=1),  # scope
                _preview(product="pure original 12", location="nimbl", quantity=1),  # allowlist
                _preview(product="pure original 12", quantity=1),                    # missing loc
            ]
        for r in samples:
            assert r.startswith("WRITE_BLOCKED"), r[:40]
            assert "NOT WRITTEN" in r

    def test_write_return_is_write_confirmed(self):
        with ExitStack() as s:
            _stub(s, levels=[202, 202], set_result=203)
            _preview(product="pure original 12", location="office", quantity=203)
            r = _confirm()
            assert r.startswith("WRITE_CONFIRMED")


# ── allowlist loader (unchanged) ─────────────────────────────────────────────────

class TestAllowlistLoader:
    def test_real_seed_allows_office_refuses_nimbl(self):
        allowed, aliases = tool_dispatch._load_shopify_write_config()
        assert _OFFICE.lower() in allowed and "nimbl" not in allowed
        assert aliases.get("office") == _OFFICE.lower()

    def test_missing_file_fails_closed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(tool_dispatch, "_SHOPIFY_WRITE_LOC_PATH", tmp_path / "nope.yaml")
        assert tool_dispatch._load_shopify_write_config() == (frozenset(), {})

    def test_broken_yaml_fails_closed(self, monkeypatch, tmp_path):
        p = tmp_path / "broken.yaml"
        p.write_text("allowed_write_locations: [unterminated", encoding="utf-8")
        monkeypatch.setattr(tool_dispatch, "_SHOPIFY_WRITE_LOC_PATH", p)
        assert tool_dispatch._load_shopify_write_config() == (frozenset(), {})


# ── wiring ───────────────────────────────────────────────────────────────────────

class TestWiring:
    def _def(self):
        return next((t for t in TOOL_DEFINITIONS if t["name"] == "f3e_shopify_set_inventory"), None)

    def test_definition_exists(self):
        assert self._def() is not None

    def test_required_fields(self):
        d = self._def()
        props = d["input_schema"]["properties"]
        for f in ("product", "location", "quantity", "confirmed"):
            assert f in props and f in d["input_schema"]["required"]

    def test_mentions_staged_write(self):
        assert "STAGED-WRITE" in self._def()["description"].upper()

    def test_registered_and_callable(self):
        assert callable(_TOOL_FUNCTIONS.get("f3e_shopify_set_inventory"))

    def test_in_f3e_set(self):
        assert "f3e_shopify_set_inventory" in _ENTITY_TOOLS["F3E"]

    def test_timeout_75(self):
        assert _TOOL_TIMEOUTS.get("f3e_shopify_set_inventory") == 75

    def test_offered_f3e_not_lex(self):
        assert "f3e_shopify_set_inventory" in {t["name"] for t in tools_for_entity("F3E")}
        assert "f3e_shopify_set_inventory" not in {t["name"] for t in tools_for_entity("LEX")}

    def test_offered_cross_entity_founder(self):
        assert "f3e_shopify_set_inventory" in {t["name"] for t in tools_for_entity("OSN", cross_entity=True)}

    def test_not_in_verbatim_table_tools(self):
        assert "f3e_shopify_set_inventory" not in tool_dispatch.VERBATIM_TABLE_TOOLS


# ── SKU alias map (the #1 fix) -- tested against the REAL seeded YAML ────────────

class TestAliasMap:
    def test_exact_2026_07_21_failures_resolve(self):
        # the paraphrase Hannah typed today -> canonical SKU (deterministic)
        assert tool_dispatch._resolve_sku_alias("F3 Mood Strawberries & Cream 12-pack") == ("F3SC", True)
        assert tool_dispatch._resolve_sku_alias("strawberries and cream mood") == ("F3SC", True)

    def test_reordered_and_case_variants_resolve(self):
        assert tool_dispatch._resolve_sku_alias("ORIGINAL ENERGY 12 PACK") == ("F3-Original", True)
        assert tool_dispatch._resolve_sku_alias("citrus energy") == ("F3-Citrus", True)
        assert tool_dispatch._resolve_sku_alias("piña colada mood") == ("F3PC", True)

    def test_ambiguous_bare_word_not_auto_resolved(self):
        # 'variety' collides across Energy/Mood/Pure -> deliberately NOT mapped;
        # falls through unchanged so resolve_variants disambiguates.
        assert tool_dispatch._resolve_sku_alias("variety") == ("variety", False)
        assert tool_dispatch._resolve_sku_alias("original") == ("original", False)
        assert tool_dispatch._resolve_sku_alias("citrus") == ("citrus", False)

    def test_exact_shopify_title_passes_through(self):
        # the exact Shopify title is not an alias -> unchanged -> live fuzzy handles it
        q, hit = tool_dispatch._resolve_sku_alias("Strawberries & Cream Mood - 12 Pack")
        assert hit is False and q == "Strawberries & Cream Mood - 12 Pack"

    def test_closest_alias_suggests_on_near_miss(self):
        assert tool_dispatch._closest_alias("citrus energ") is not None

    def test_aliased_input_previews_fine(self):
        with ExitStack() as s:
            _stub(s, current=239)
            r = _preview(product="F3 Mood Strawberries & Cream 12-pack", location="office", quantity=239)
            assert r.startswith("WRITE_BLOCKED") and "NOT WRITTEN" in r
            assert has_pending_shopify_write(_ALEX, _CHAN)

    def test_alias_miss_refuses_and_may_suggest(self):
        with ExitStack() as s:
            _stub(s, variants=[])   # resolve_variants finds nothing
            r = _preview(product="totally unknown flavor zzz", location="office", quantity=5)
            assert r.startswith("WRITE_BLOCKED") and "won't guess" in r
            assert not has_pending_shopify_write(_ALEX, _CHAN)


# ── delta adjustments (add/remove N) + floor guard ──────────────────────────────

class TestDelta:
    def test_remove_previews_current_minus_delta(self):
        with ExitStack() as s:
            _stub(s, current=202)
            r = _preview(product="pure original 12", location="office", delta=-13)
            assert "202 -> 189" in r and "NOT WRITTEN" in r
            assert has_pending_shopify_write(_ALEX, _CHAN)

    def test_add_previews_current_plus_delta(self):
        with ExitStack() as s:
            _stub(s, current=202)
            r = _preview(product="pure original 12", location="office", delta=20)
            assert "202 -> 222" in r

    def test_floor_guard_refuses_below_zero(self):
        with ExitStack() as s:
            m_set = _stub(s, current=202)
            r = _preview(product="pure original 12", location="office", delta=-500)
            assert r.startswith("WRITE_BLOCKED") and "below zero" in r
            assert not has_pending_shopify_write(_ALEX, _CHAN)
            m_set.assert_not_called()

    def test_delta_confirm_writes_computed_absolute(self):
        with ExitStack() as s:
            m_set = _stub(s, levels=[202, 202])   # preview read, confirm re-check (no drift)
            _preview(product="pure original 12", location="office", delta=-13)
            r = _confirm()
            assert r.startswith("WRITE_CONFIRMED")
            m_set.assert_called_once_with(_PURE.inventory_item_id, 81567023424, 189)

    def test_delta_recomputes_against_drift(self):
        with ExitStack() as s:
            m_set = _stub(s, levels=[202, 210])   # drifted to 210 by confirm time
            _preview(product="pure original 12", location="office", delta=-13)
            r = _confirm()
            # a delta re-applies to the fresh count: 210 - 13 = 197, RE-PREVIEW (no write)
            assert r.startswith("WRITE_BLOCKED") and "197" in r and "moved" in r.lower()
            m_set.assert_not_called()


# ── channel-scoped defaults (location + unit=cases) ─────────────────────────────

class TestChannelDefaults:
    def test_default_location_and_cases_unit_applied(self):
        with ExitStack() as s:
            _stub(s, current=203, chan_cfg={"default_location": _OFFICE, "unit": "cases"})
            # no location given -> channel default fills it; unit labelled 'cases'
            r = _preview(_channel_name=_HQ, product="pure original 12", quantity=203)
            assert r.startswith("WRITE_BLOCKED") and "cases" in r
            assert "units" not in r.split("\n")[-2] if "\n" in r else True
            assert has_pending_shopify_write(_ALEX, _HQ)

    def test_cases_never_divides(self):
        with ExitStack() as s:
            _stub(s, current=202, chan_cfg={"default_location": _OFFICE, "unit": "cases"})
            r = _preview(_channel_name=_HQ, product="pure original 12", quantity=239)
            # the entered number is the absolute case count -- no /12 or x12
            assert "-> 239 cases" in r

    def test_non_configured_channel_still_asks_location(self):
        with ExitStack() as s:
            _stub(s)   # chan_cfg defaults to {} -> no default
            r = _preview(product="pure original 12", quantity=5)   # no location
            assert r.startswith("WRITE_BLOCKED") and "which location" in r.lower()

    def test_non_office_location_still_refused_in_hq_channel(self):
        with ExitStack() as s:
            m_set = _stub(s, chan_cfg={"default_location": _OFFICE, "unit": "cases"})
            # an explicit non-office location overrides the default and is refused
            r = _preview(_channel_name=_HQ, product="pure original 12", location="nimbl", quantity=5)
            assert r.startswith("WRITE_BLOCKED") and "sync" in r.lower()
            m_set.assert_not_called()

    def test_channel_default_threaded_not_leaked_to_other_channel(self):
        # same chan_cfg, but a preview in a DIFFERENT channel gets no default (proves
        # the channel name is threaded into the config lookup, not applied blanket).
        with ExitStack() as s:
            _stub(s, chan_cfg={"default_location": _OFFICE, "unit": "cases"})
            r = _preview(product="pure original 12", quantity=5)   # _CHAN=f3e-leadership
            assert r.startswith("WRITE_BLOCKED") and "which location" in r.lower()


# ── bulk multi-SKU: one message -> one preview table -> one confirm ─────────────

def _vmap():
    """Three DISTINCT variants keyed by canonical SKU (so bulk rows don't collide)."""
    return {
        "F3-Original": VariantMatch("F3 Original Energy", "12 Pack", "F3-Original", 1, 1001),
        "F3-Citrus": VariantMatch("F3 Citrus Energy", "12 Pack", "F3-Citrus", 2, 1002),
        "F3SC": VariantMatch("Strawberries & Cream Mood", "12 Pack", "F3SC", 3, 1003),
    }


class TestBulk:
    def test_one_preview_one_confirm_writes_all(self):
        vmap = _vmap()
        with ExitStack() as s:
            m_set = _stub(s, current=100, chan_cfg={"default_location": _OFFICE, "unit": "cases"})
            s.enter_context(patch.object(shopify_client, "resolve_variants",
                                         side_effect=lambda q: [vmap[q]] if q in vmap else []))
            items = [
                {"product": "original energy 12 pack", "delta": -24},
                {"product": "citrus energy", "delta": -18},
                {"product": "strawberries and cream mood", "delta": -13},
            ]
            r = _preview(_channel_name=_HQ, items=items)
            assert r.startswith("WRITE_BLOCKED")
            for lbl in ("F3 Original Energy", "F3 Citrus Energy", "Strawberries & Cream Mood"):
                assert lbl in r
            assert "cases" in r and has_pending_shopify_write(_ALEX, _HQ)
            r2 = _confirm(_channel_name=_HQ)
            assert r2.startswith("WRITE_CONFIRMED")
            assert m_set.call_count == 3
            written = {c.args[0]: c.args[2] for c in m_set.call_args_list}  # item_id -> target
            assert written == {1001: 76, 1002: 82, 1003: 87}   # 100 + delta, floor ok

    def test_unresolved_row_surfaced_not_dropped(self):
        vmap = _vmap()
        with ExitStack() as s:
            m_set = _stub(s, current=100, chan_cfg={"default_location": _OFFICE, "unit": "cases"})
            s.enter_context(patch.object(shopify_client, "resolve_variants",
                                         side_effect=lambda q: [vmap[q]] if q in vmap else []))
            items = [{"product": "original energy 12 pack", "delta": -5},
                     {"product": "nonexistent flavor", "delta": -5}]
            r = _preview(_channel_name=_HQ, items=items)
            assert "Skipped" in r and "nonexistent flavor" in r and "F3 Original Energy" in r
            r2 = _confirm(_channel_name=_HQ)
            assert r2.startswith("WRITE_CONFIRMED") and m_set.call_count == 1

    def test_all_unresolved_stages_nothing(self):
        with ExitStack() as s:
            m_set = _stub(s, chan_cfg={"default_location": _OFFICE, "unit": "cases"})
            s.enter_context(patch.object(shopify_client, "resolve_variants", side_effect=lambda q: []))
            r = _preview(_channel_name=_HQ, items=[{"product": "xyz", "delta": -5}])
            assert r.startswith("WRITE_BLOCKED") and "couldn't resolve any" in r.lower()
            assert not has_pending_shopify_write(_ALEX, _HQ)
            m_set.assert_not_called()

    def test_drift_repreviews_whole_batch_no_write(self):
        vmap = _vmap()
        with ExitStack() as s:
            # preview reads [100,100]; confirm re-reads [100, 90] -> row2 drifted
            m_set = _stub(s, levels=[100, 100, 100, 90],
                          chan_cfg={"default_location": _OFFICE, "unit": "cases"})
            s.enter_context(patch.object(shopify_client, "resolve_variants",
                                         side_effect=lambda q: [vmap[q]] if q in vmap else []))
            items = [{"product": "original energy 12 pack", "delta": -5},
                     {"product": "citrus energy", "delta": -5}]
            _preview(_channel_name=_HQ, items=items)
            r2 = _confirm(_channel_name=_HQ)
            assert r2.startswith("WRITE_BLOCKED") and "updated batch" in r2.lower()
            m_set.assert_not_called()

    def test_bulk_floor_guard_skips_row(self):
        vmap = _vmap()
        with ExitStack() as s:
            m_set = _stub(s, current=10, chan_cfg={"default_location": _OFFICE, "unit": "cases"})
            s.enter_context(patch.object(shopify_client, "resolve_variants",
                                         side_effect=lambda q: [vmap[q]] if q in vmap else []))
            # remove 5 (ok: 10->5) and remove 50 (floor -> skipped)
            items = [{"product": "original energy 12 pack", "delta": -5},
                     {"product": "citrus energy", "delta": -50}]
            r = _preview(_channel_name=_HQ, items=items)
            assert "Skipped" in r and "F3 Citrus Energy" in r   # floor-guarded row skipped
            r2 = _confirm(_channel_name=_HQ)
            assert r2.startswith("WRITE_CONFIRMED") and m_set.call_count == 1
            assert m_set.call_args_list[0].args[2] == 5
