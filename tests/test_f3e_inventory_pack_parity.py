"""Slice 1 (pipeline-integrity bundle, 2026-08-05) -- 9-RED-2 / cq-861ca3630d31:
single-item SKU resolution rejected a literal pack-size suffix that the batch path
tolerated.

REPRO OF RECORD (weekly Slack-output clarity audit 2026-08-01, both reproduced
post-restart):
    "couldn't find 'F3 Pure Variety Pack 12-pack'"
    "couldn't find 'Pure Strawberry Lemonade 12-pack'"

Verify-first overturned the "two code paths" premise -- _resolve_and_preview_batch
calls the SAME _shopify_resolve per row. The asymmetry is the tool schema (the
single-item `product` description exemplifies 'Pure Original 12-pack'; items[]
carried none), and the matcher rejected that suffix at two layers: no alias key
carries it, and resolve_variants is an AND-of-tokens substring match so a
"12-pack" token missing from the title kills every candidate.

So these tests pin PARITY, not a special case: the same phrase must resolve
identically on the single-item path and the batch path, the raw query is always
tried first (additive), and ambiguity still ASKS rather than guessing.
"""

from __future__ import annotations

import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora.connectors import shopify_client
from cora.connectors.shopify_client import VariantMatch
from cora.tools import tool_dispatch
from cora.tools.tool_dispatch import (
    _tool_f3e_shopify_set_inventory,
    has_pending_shopify_write,
)

from test_f3e_shopify_set_inventory import _CHAN, _HQ, _LOCS, _ALEX, _CONFIG

# NOTE (D-051 lens-6 MEDIUM): TestAliasTolerance / TestLexiconLeg assert against the
# LIVE data/maps/f3e-sku-aliases.yaml, read fresh with no test hook. That is
# deliberate -- it doubles as a drift guard on curated canon -- and it is safe for the
# NEGATIVE pins because the Harrison-gated lexicon rail appends only to `learned:`,
# where the seed map wins on a normalized collision (`setdefault`), so an ambiguous
# bare word can never start resolving. If a future edit renames a SKU, these break
# loudly, which is the intended signal.

# The two products from the live repro, with titles that DO NOT contain the
# literal "12-pack" token the user typed -- that absence is the bug.
_VARIETY = VariantMatch(
    product_title="F3 PURE Energy Drink Variety Pack", variant_title="12 Pack",
    sku="F3-PureE-V4F", variant_id=41, inventory_item_id=52999599999001,
)
_PURESL = VariantMatch(
    product_title="F3 PURE Strawberry Lemonade Energy Drink", variant_title="12 Pack",
    sku="PURESL", variant_id=42, inventory_item_id=52999599999002,
)


def _titles_only_resolver(*variants):
    """A resolve_variants stub that behaves like the REAL one: AND-of-tokens
    substring over "<product_title> <variant_title> <sku>", plus the exact-SKU
    shortcut. This is what makes the repro faithful -- a stub that returns a
    fixed list regardless of query would hide the whole defect."""
    def _resolve(query, limit=25):
        q = (query or "").strip().lower()
        if not q:
            return []
        exact = [v for v in variants if v.sku and q == v.sku.lower()]
        if exact:
            return exact
        toks = [t for t in q.split() if t]
        out = []
        for v in variants:
            haystack = f"{v.product_title} {v.variant_title} {v.sku}".lower()
            if toks and all(t in haystack for t in toks):
                out.append(v)
        return out[:limit]
    return _resolve


def _stub_real_matching(stack: ExitStack, *variants, current=100, chan_cfg=None):
    stack.enter_context(patch.object(shopify_client, "get_active_locations",
                                     return_value=list(_LOCS)))
    stack.enter_context(patch.object(shopify_client, "resolve_variants",
                                     side_effect=_titles_only_resolver(*variants)))
    stack.enter_context(patch.object(shopify_client, "get_inventory_level",
                                     return_value=current))
    m_set = stack.enter_context(patch.object(shopify_client, "set_inventory_level",
                                             return_value=current))
    stack.enter_context(patch.object(tool_dispatch, "_load_shopify_write_config",
                                     return_value=_CONFIG))

    def _chan_cfg(name):
        n = (name or "").strip().lstrip("#").lower()
        return dict(chan_cfg) if (chan_cfg and n == _HQ) else {}
    stack.enter_context(patch.object(tool_dispatch, "_load_inventory_channel_config",
                                     side_effect=_chan_cfg))
    # The lexicon leg is flag-gated; keep it OFF here so these tests pin the
    # alias+resolve_variants ladder itself (a dedicated class below covers the
    # lexicon-active path).
    stack.enter_context(patch.object(tool_dispatch, "_lexicon_active",
                                     return_value=False))
    return m_set


def _single(product, **kw):
    base = {"_channel_name": _CHAN, "product": product, "location": "office",
            "quantity": 120}
    base.update(kw)
    return _tool_f3e_shopify_set_inventory(_ALEX, "F3E", base)


def _batch(products, **kw):
    items = [{"product": p, "quantity": 120, "location": "office"} for p in products]
    base = {"_channel_name": _CHAN, "items": items}
    base.update(kw)
    return _tool_f3e_shopify_set_inventory(_ALEX, "F3E", base)


# ── the ladder itself ─────────────────────────────────────────────────────────

class TestPackQueryLadder:
    def test_raw_query_is_always_first(self):
        """Exact-first is the additive guarantee: nothing that resolves today can
        change, because element 0 is the untouched normalized query."""
        ladder = tool_dispatch._pack_query_ladder("F3 Pure Variety Pack 12-pack")
        assert ladder[0] == "f3 pure variety pack 12 pack"

    def test_repro_1_strips_to_a_seeded_alias(self):
        ladder = tool_dispatch._pack_query_ladder("F3 Pure Variety Pack 12-pack")
        assert ladder == ["f3 pure variety pack 12 pack",
                          "f3 pure variety pack", "f3 pure variety"]

    def test_repro_2_strips_to_a_seeded_alias(self):
        ladder = tool_dispatch._pack_query_ladder("Pure Strawberry Lemonade 12-pack")
        assert "pure strawberry lemonade" in ladder

    @pytest.mark.parametrize("suffix", [
        "12-pack", "12 pack", "12pack", "12pk", "12 pk", "12 ct", "12 count",
        "pack of 12", "case", "cases", "pack", "packs",
    ])
    def test_suffix_family(self, suffix):
        ladder = tool_dispatch._pack_query_ladder(f"pure strawberry lemonade {suffix}")
        assert "pure strawberry lemonade" in ladder

    def test_never_strips_to_empty(self):
        """A bare pack-size is not a usable probe -- the ladder must not emit ""
        (which resolve_variants treats as 'return nothing' and the alias map
        would treat as a lookup key)."""
        for q in ("12-pack", "case", "pack", "12 pk"):
            ladder = tool_dispatch._pack_query_ladder(q)
            assert "" not in ladder and len(ladder) == 1

    def test_empty_query_yields_empty_ladder(self):
        assert tool_dispatch._pack_query_ladder("") == []
        assert tool_dispatch._pack_query_ladder(None) == []

    def test_no_qualifier_is_a_single_element(self):
        assert tool_dispatch._pack_query_ladder("pure original") == ["pure original"]

    def test_bounded(self):
        ladder = tool_dispatch._pack_query_ladder("x pack pack pack pack pack pack")
        assert len(ladder) <= tool_dispatch._PACK_LADDER_MAX_STEPS + 1

    def test_mid_string_qualifier_not_stripped(self):
        """Trailing-only by construction: a packaging word inside the name is
        load-bearing and must survive."""
        ladder = tool_dispatch._pack_query_ladder("variety pack sampler")
        assert ladder == ["variety pack sampler"]


# ── alias resolution ──────────────────────────────────────────────────────────

class TestAliasTolerance:
    def test_repro_1_resolves(self):
        sku, hit, resolved_from = tool_dispatch._resolve_sku_alias_tolerant(
            "F3 Pure Variety Pack 12-pack")
        assert (sku, hit) == ("F3-PureE-V4F", True)
        assert resolved_from == "F3 Pure Variety Pack 12-pack"

    def test_repro_2_resolves(self):
        sku, hit, resolved_from = tool_dispatch._resolve_sku_alias_tolerant(
            "Pure Strawberry Lemonade 12-pack")
        assert (sku, hit) == ("PURESL", True)
        assert resolved_from == "Pure Strawberry Lemonade 12-pack"

    def test_exact_hit_reports_no_resolved_from(self):
        """An exact alias hit is byte-identical to the pre-ladder behavior -- and
        must NOT claim a resolution the user didn't need explained."""
        sku, hit, resolved_from = tool_dispatch._resolve_sku_alias_tolerant("pure original")
        assert (sku, hit, resolved_from) == ("PURE-Original", True, "")

    def test_ambiguous_bare_word_still_not_resolved(self):
        """The safety property: stripping must never manufacture a resolution for
        a word the map deliberately leaves ambiguous."""
        for q in ("variety", "variety pack", "original", "citrus"):
            _sku, hit, _rf = tool_dispatch._resolve_sku_alias_tolerant(q)
            assert hit is False, q

    def test_unknown_product_still_misses(self):
        q, hit, _rf = tool_dispatch._resolve_sku_alias_tolerant("totally unknown zzz 12-pack")
        assert hit is False and q == "totally unknown zzz 12-pack"

    def test_underlying_exact_helper_unchanged(self):
        """_resolve_sku_alias keeps its exact-only contract (test-pinned elsewhere
        and shared with the lexicon parity pins) -- the tolerance is the wrapper."""
        assert tool_dispatch._resolve_sku_alias("F3 Pure Variety Pack 12-pack") == (
            "F3 Pure Variety Pack 12-pack", False)

    def test_closest_alias_hints_through_the_suffix(self):
        assert tool_dispatch._closest_alias("pure strawberry lemonade 12-pack") is not None


# ── resolve_variants retry (product in Shopify, not in the alias map) ─────────

class TestVariantRetry:
    def test_and_of_tokens_miss_is_retried_stripped(self):
        unaliased = VariantMatch(
            product_title="F3 PURE Brand New Flavor", variant_title="12 Pack",
            sku="PURE-NEW", variant_id=99, inventory_item_id=52999599999099,
        )
        with patch.object(shopify_client, "resolve_variants",
                          side_effect=_titles_only_resolver(unaliased)):
            # raw: the literal "12-pack" token is absent from the haystack -> 0
            assert shopify_client.resolve_variants("pure brand new flavor 12-pack") == []
            matches, resolved_from = tool_dispatch._resolve_variants_pack_tolerant(
                "pure brand new flavor 12-pack")
            assert [m.sku for m in matches] == ["PURE-NEW"]
            assert resolved_from == "pure brand new flavor 12-pack"

    def test_hit_on_the_raw_query_does_not_retry(self):
        calls: list[str] = []

        def _resolve(query, limit=25):
            calls.append(query)
            return [_PURESL]
        with patch.object(shopify_client, "resolve_variants", side_effect=_resolve):
            matches, resolved_from = tool_dispatch._resolve_variants_pack_tolerant("PURESL")
        assert len(calls) == 1 and resolved_from == "" and matches

    def test_total_miss_returns_empty(self):
        with patch.object(shopify_client, "resolve_variants",
                          side_effect=_titles_only_resolver(_PURESL)):
            matches, resolved_from = tool_dispatch._resolve_variants_pack_tolerant(
                "nothing like this exists 12-pack")
        assert matches == [] and resolved_from == ""


# ── end-to-end parity: the whole point of the slice ──────────────────────────

class TestSingleBatchParity:
    @pytest.mark.parametrize("phrase,expect_sku", [
        ("F3 Pure Variety Pack 12-pack", "F3-PureE-V4F"),
        ("Pure Strawberry Lemonade 12-pack", "PURESL"),
    ])
    def test_repro_phrase_previews_on_the_single_item_path(self, phrase, expect_sku):
        with ExitStack() as s:
            _stub_real_matching(s, _VARIETY, _PURESL)
            out = _single(phrase)
        assert out.startswith("WRITE_BLOCKED") and "NOT WRITTEN" in out
        assert "couldn't find" not in out
        assert has_pending_shopify_write(_ALEX, _CHAN)

    @pytest.mark.parametrize("phrase", [
        "F3 Pure Variety Pack 12-pack",
        "Pure Strawberry Lemonade 12-pack",
    ])
    def test_same_phrase_previews_on_the_batch_path(self, phrase):
        with ExitStack() as s:
            _stub_real_matching(s, _VARIETY, _PURESL)
            out = _batch([phrase])
        assert out.startswith("WRITE_BLOCKED") and "NOT WRITTEN" in out
        assert "couldn't resolve any of those" not in out

    def test_preview_names_the_resolution(self):
        """resolved_from provenance: the human confirms the RESOLUTION, not just
        the action -- the same rule the lexicon path already follows."""
        with ExitStack() as s:
            _stub_real_matching(s, _VARIETY, _PURESL)
            out = _single("Pure Strawberry Lemonade 12-pack")
        assert "resolved from" in out and "Pure Strawberry Lemonade 12-pack" in out

    def test_bare_names_still_work_unchanged(self):
        with ExitStack() as s:
            _stub_real_matching(s, _VARIETY, _PURESL)
            out = _single("pure strawberry lemonade")
        assert out.startswith("WRITE_BLOCKED") and "NOT WRITTEN" in out

    def test_ambiguous_still_asks_never_guesses(self):
        """Two products both match "variety" -- the >1 branch is untouched, so the
        tool asks. This is the property that makes the ladder safe."""
        other = VariantMatch(
            product_title="F3 MOOD Variety Pack", variant_title="12 Pack",
            sku="F3VPM4", variant_id=43, inventory_item_id=52999599999003,
        )
        with ExitStack() as s:
            _stub_real_matching(s, _VARIETY, other)
            out = _single("variety pack 12-pack")
        assert out.startswith("WRITE_BLOCKED")
        assert "Which one?" in out or "won't guess" in out
        assert not has_pending_shopify_write(_ALEX, _CHAN)

    def test_unknown_product_still_refuses(self):
        with ExitStack() as s:
            _stub_real_matching(s, _VARIETY, _PURESL)
            out = _single("totally unknown zzz 12-pack")
        assert out.startswith("WRITE_BLOCKED") and "won't guess" in out
        assert not has_pending_shopify_write(_ALEX, _CHAN)


# ── D-051 review HIGH: the pack SIZE must not be silently discarded ──────────

class TestPackSizeVerification:
    """The ladder strips a pack qualifier to FIND the product. The first cut also
    threw away the SIZE: "original energy 24 pack" stripped to "original energy",
    hit the alias map, and resolved CONFIDENTLY to the 12-pack SKU -- a guess
    presented as a resolution on a real-money write path, made worse by the new
    provenance line reading like the 24-pack had been understood. Every seeded alias
    is a 12-pack, so any other size the user named was coerced."""

    @pytest.mark.parametrize("query,expected", [
        ("original energy 24 pack", 24),
        ("tropical energy 24 pack", 24),
        ("original energy 4 pk", 4),
        ("orangesicle pack of 24", 24),
        ("pina colada mood 24ct", 24),
        ("pure variety pack 24-pack", 24),
        ("Pure Strawberry Lemonade 12-pack", 12),
        ("original energy 12 cases", 12),
        ("pure original", None),
        ("variety pack", None),
        ("pure original case", None),
    ])
    def test_named_pack_size_extraction(self, query, expected):
        assert tool_dispatch._named_pack_size(query) == expected

    @pytest.mark.parametrize("named,label,conflicts", [
        (24, "F3 Original Energy Drink (12 Pack)", True),
        (4, "F3 Original Energy Drink (12 Pack)", True),
        (12, "F3 Original Energy Drink (12 Pack)", False),
        (None, "F3 Original Energy Drink (12 Pack)", False),
        # Unverifiable is NOT wrong: a label with no size passes, or the legitimate
        # "... 12-pack" case would break whenever a title omits the size.
        (12, "F3 Original Energy Drink", False),
        (24, "F3 Original Energy Drink", False),
    ])
    def test_pack_size_conflicts(self, named, label, conflicts):
        assert tool_dispatch._pack_size_conflicts(named, label) is conflicts

    @pytest.mark.parametrize("query", [
        "F3 Pure Variety Pack 24-pack",
        "Pure Strawberry Lemonade 24 pack",
        "pure strawberry lemonade pack of 6",
    ])
    def test_mismatched_size_refuses_and_writes_nothing(self, query):
        with ExitStack() as s:
            m_set = _stub_real_matching(s, _VARIETY, _PURESL)
            out = _single(query)
        assert out.startswith("WRITE_BLOCKED") and "won't assume" in out
        assert m_set.call_count == 0
        assert not has_pending_shopify_write(_ALEX, _CHAN)

    def test_matching_size_still_resolves(self):
        with ExitStack() as s:
            _stub_real_matching(s, _VARIETY, _PURESL)
            out = _single("Pure Strawberry Lemonade 12-pack")
        assert out.startswith("WRITE_BLOCKED") and "NOT WRITTEN" in out
        assert has_pending_shopify_write(_ALEX, _CHAN)


def test_alias_hit_does_not_reprobe_raw_text():
    """D-051 review MEDIUM: the retry source was passed on EVERY call, so a stale
    canonical (renamed/retired SKU) silently re-probed the user's raw words and could
    bind a different variant -- contradicting this path's own "an alias hit is
    authoritative" contract. A stale canonical must refuse honestly."""
    calls: list[str] = []

    def _resolve(query, limit=25):
        calls.append(query)
        return []          # the canonical is stale -> nothing matches
    with ExitStack() as s:
        s.enter_context(patch.object(shopify_client, "get_active_locations",
                                     return_value=list(_LOCS)))
        s.enter_context(patch.object(shopify_client, "resolve_variants",
                                     side_effect=_resolve))
        s.enter_context(patch.object(tool_dispatch, "_load_shopify_write_config",
                                     return_value=_CONFIG))
        s.enter_context(patch.object(tool_dispatch, "_load_inventory_channel_config",
                                     return_value={}))
        s.enter_context(patch.object(tool_dispatch, "_lexicon_active",
                                     return_value=False))
        out = _single("pure original")          # exact alias hit -> PURE-Original
    assert out.startswith("WRITE_BLOCKED") and "couldn't find" in out
    assert calls == ["PURE-Original"], calls    # no raw re-probe


# ── unchanged guardrails (D-079 / F-23 confirm gate, floor, magnitude) ────────

class TestGuardrailsUnchanged:
    def test_confirm_gate_still_two_calls(self):
        """The fix must not weaken F-23: a preview is NOT a write, and the confirm
        executes the SERVER-resolved pending entry."""
        with ExitStack() as s:
            m_set = _stub_real_matching(s, _VARIETY, _PURESL, current=100)
            out = _single("Pure Strawberry Lemonade 12-pack", quantity=120)
            assert m_set.call_count == 0          # preview writes nothing
            confirmed = _tool_f3e_shopify_set_inventory(
                _ALEX, "F3E", {"_channel_name": _CHAN, "confirmed": True})
        assert out.startswith("WRITE_BLOCKED")
        assert confirmed.startswith("WRITE_CONFIRMED")
        assert m_set.call_count == 1

    def test_floor_guard_still_refuses_below_zero(self):
        with ExitStack() as s:
            m_set = _stub_real_matching(s, _VARIETY, _PURESL, current=3)
            out = _single("Pure Strawberry Lemonade 12-pack", quantity=None, delta=-10)
        assert out.startswith("WRITE_BLOCKED") and "below zero" in out
        assert m_set.call_count == 0

    def test_absurd_absolute_guard_still_fires(self):
        with ExitStack() as s:
            m_set = _stub_real_matching(s, _VARIETY, _PURESL, current=535)
            out = _single("Pure Strawberry Lemonade 12-pack", quantity=5003)
        assert out.startswith("WRITE_BLOCKED")
        assert m_set.call_count == 0
        assert not has_pending_shopify_write(_ALEX, _CHAN)

    def test_write_location_allowlist_still_enforced(self):
        with ExitStack() as s:
            m_set = _stub_real_matching(s, _VARIETY, _PURESL)
            out = _single("Pure Strawberry Lemonade 12-pack", location="Nimbl")
        assert out.startswith("WRITE_BLOCKED") and "can't set inventory at" in out
        assert m_set.call_count == 0


# ── lexicon leg (CORA_LEXICON=resolve is the LIVE config) ────────────────────

class TestLexiconLeg:
    def test_stripped_form_resolves_through_the_lexicon(self):
        from cora import lexicon

        with ExitStack() as s:
            s.enter_context(patch.object(tool_dispatch, "_lexicon_active",
                                         return_value=True))
            res = tool_dispatch._lexicon_resolve_pack_tolerant(
                "Pure Strawberry Lemonade 12-pack", channel=_CHAN, user=_ALEX)
        assert res is not None and res.status == "exact"
        assert res.canonical == "PURESL"
        assert lexicon is not None

    def test_exactly_one_resolve_event_per_turn(self):
        """Probing 3 ladder candidates through resolve(consumer=...) would treble
        the lexicon-resolver counts the flywheel monitor reads -- probes run silent
        and exactly one `resolve` event is logged, against the ORIGINAL query.

        A second row is emitted under the DISTINCT event name resolve_raw_surface
        (D-051 lens-5 MEDIUM): the raw surface the user typed genuinely MISSED, and
        that miss is what lexicon_mining reads to learn the pack-suffixed alias.
        Folding it into the "exact" row erased the learning signal; a distinct event
        name keeps it out of every count that filters on event == "resolve"."""
        from cora import lexicon

        events: list[dict] = []
        with ExitStack() as s:
            s.enter_context(patch.object(lexicon, "log_event",
                                         side_effect=lambda **kw: events.append(kw)))
            tool_dispatch._lexicon_resolve_pack_tolerant(
                "Pure Strawberry Lemonade 12-pack", channel=_CHAN, user=_ALEX)
        resolves = [e for e in events if e.get("event", "resolve") == "resolve"]
        assert len(resolves) == 1
        assert resolves[0]["query"] == "Pure Strawberry Lemonade 12-pack"
        assert resolves[0]["consumer"] == "f3e_shopify_set_inventory"
        assert resolves[0]["status"] == "exact"
        raw_rows = [e for e in events if e.get("event") == "resolve_raw_surface"]
        assert len(raw_rows) == 1
        assert raw_rows[0]["status"] in ("miss", "suggestion")
        assert raw_rows[0]["query"] == "Pure Strawberry Lemonade 12-pack"

    def test_no_raw_surface_row_when_the_raw_query_resolved(self):
        from cora import lexicon

        events: list[dict] = []
        with ExitStack() as s:
            s.enter_context(patch.object(lexicon, "log_event",
                                         side_effect=lambda **kw: events.append(kw)))
            tool_dispatch._lexicon_resolve_pack_tolerant(
                "pure original", channel=_CHAN, user=_ALEX)
        assert [e for e in events if e.get("event") == "resolve_raw_surface"] == []

    def test_miss_still_logs_the_raw_result(self):
        from cora import lexicon

        events: list[dict] = []
        with ExitStack() as s:
            s.enter_context(patch.object(lexicon, "log_event",
                                         side_effect=lambda **kw: events.append(kw)))
            res = tool_dispatch._lexicon_resolve_pack_tolerant(
                "zzz nothing here 12-pack", channel=_CHAN, user=_ALEX)
        assert len(events) == 1
        assert res is not None and res.status in ("miss", "suggestion")

    def test_lexicon_failure_degrades_to_the_alias_ladder(self):
        """ADDITIVE invariant: a lexicon blow-up must leave the write path exactly
        as capable as the legacy alias step, never dead-end it."""
        with ExitStack() as s:
            _stub_real_matching(s, _VARIETY, _PURESL)
            s.enter_context(patch.object(tool_dispatch, "_lexicon_active",
                                         return_value=True))
            s.enter_context(patch.object(tool_dispatch, "_lexicon_resolve_pack_tolerant",
                                         side_effect=RuntimeError("lexicon down")))
            out = _single("Pure Strawberry Lemonade 12-pack")
        assert out.startswith("WRITE_BLOCKED") and "NOT WRITTEN" in out
        assert "couldn't find" not in out


# ── schema parity (the source of the single-vs-batch asymmetry) ───────────────

def test_items_product_description_documents_the_same_resolution():
    """The asymmetry that produced the defect: single-item `product` exemplified
    'Pure Original 12-pack' while items[].product had no description at all, so
    the model phrased the two paths differently. Both now document one contract."""
    spec = next(t for t in tool_dispatch.TOOL_DEFINITIONS
                if t["name"] == "f3e_shopify_set_inventory")
    props = spec["input_schema"]["properties"]
    assert "12-pack" in props["product"]["description"]
    item_desc = props["items"]["items"]["properties"]["product"]["description"]
    assert "pack-size" in item_desc and "single-item" in item_desc
