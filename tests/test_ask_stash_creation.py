"""S4 ask-stash CREATION tests: the two deterministic ambiguity-ask branches
inside _shopify_resolve (lexicon collision, inventory alias/variant
ambiguity) must stash a picker-ready ask when there are <=5 candidates, and
must NOT stash (falling back to the existing text-only "which one?" ask,
design doc 4.2's explicit cap) beyond that.

Complements test_confirm_dispatcher.py / test_confirm_buttons_app.py, which
cover PICK RESOLUTION (resolve_shopify_ask_pick) against hand-constructed ask
entries -- this file proves the real _shopify_resolve code path is what
actually populates those entries.
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
from cora.tools import tool_dispatch as td

_HARRISON = td._HARRISON_SLACK_ID
_CHAN = "f3e-leadership"
_OFFICE = "1337 S Gilbert Rd"
_LOCS = [{"id": 81567023424, "name": _OFFICE}]
_CONFIG = (frozenset({_OFFICE.lower()}), {"office": _OFFICE.lower()})


def _stub(stack: ExitStack, *, variants=None):
    stack.enter_context(patch.object(shopify_client, "get_active_locations", return_value=list(_LOCS)))
    stack.enter_context(patch.object(shopify_client, "resolve_variants",
                                     return_value=list(variants if variants is not None else [])))
    stack.enter_context(patch.object(shopify_client, "get_inventory_level", return_value=202))
    stack.enter_context(patch.object(td, "_load_shopify_write_config", return_value=_CONFIG))
    stack.enter_context(patch.object(td, "_load_inventory_channel_config", return_value={}))


def _preview(product="pure original", location="office", quantity=203):
    return td._tool_f3e_shopify_set_inventory(_HARRISON, "F3E", {
        "product": product, "location": location, "quantity": quantity,
        "_channel_name": _CHAN,
    })


def _variants(n: int) -> list[VariantMatch]:
    return [
        VariantMatch(product_title=f"F3 Variant {i}", variant_title="", sku=f"SKU{i}",
                     variant_id=i, inventory_item_id=1000 + i)
        for i in range(n)
    ]


class TestVariantAmbiguityStashesAsk:
    def test_two_candidates_stashes_a_pickable_ask(self):
        with ExitStack() as s:
            _stub(s, variants=_variants(2))
            _preview()
        pending = td._peek_pending_ask(_HARRISON, _CHAN)
        assert pending is not None
        assert pending["ask_kind"] == "variant"
        assert len(pending["candidates"]) == 2
        assert pending["candidates"][0][0] == "0"  # candidate_key

    def test_five_candidates_still_stashes(self):
        with ExitStack() as s:
            _stub(s, variants=_variants(5))
            _preview()
        pending = td._peek_pending_ask(_HARRISON, _CHAN)
        assert pending is not None
        assert len(pending["candidates"]) == 5

    def test_six_candidates_does_not_stash_falls_back_to_text(self):
        # Design doc 4.2: beyond 5 candidates, fall back to the existing
        # text-only ask -- no picker card, no ask_stash entry at all.
        with ExitStack() as s:
            _stub(s, variants=_variants(6))
            result = _preview()
        assert td._peek_pending_ask(_HARRISON, _CHAN) is None
        assert "which one" in result.lower()

    def test_ask_carries_the_resolved_location_and_quantity(self):
        with ExitStack() as s:
            _stub(s, variants=_variants(2))
            _preview(quantity=250)
        pending = td._peek_pending_ask(_HARRISON, _CHAN)
        assert pending["loc_name"] == _OFFICE
        assert pending["quantity"] == 250

    def test_stale_ask_tap_never_destroys_a_newer_ask_in_the_same_slot(self):
        # D-051 adversarial review finding: the ask-stash used to pop the
        # (user, channel) slot UNCONDITIONALLY, checking the ask_id match only
        # AFTER the pop -- so a stale tap on an OLD ask (AID1) would silently
        # destroy a DIFFERENT, never-tapped, still-valid ask (AID2) that had
        # since overwritten the single slot, and AID2's own later tap would
        # then falsely read "superseded" too. Fixed by checking the id match
        # INSIDE the same lock as the pop (mirrors _claim_stash_by_id).
        with ExitStack() as s:
            _stub(s, variants=_variants(2))
            _preview(product="pure original")  # AID1
        aid1 = td._peek_pending_ask(_HARRISON, _CHAN)["ask_id"]
        with ExitStack() as s:
            _stub(s, variants=_variants(3))
            _preview(product="different product")  # AID2 overwrites the slot
        aid2 = td._peek_pending_ask(_HARRISON, _CHAN)["ask_id"]
        assert aid1 != aid2

        # Stale tap on AID1: honest superseded, AID2 untouched.
        outcome1, _msg1, _sid1 = td.resolve_shopify_ask_pick(aid1, _HARRISON, "0")
        assert outcome1 == "superseded"
        still_there = td._peek_pending_ask(_HARRISON, _CHAN)
        assert still_there is not None and still_there["ask_id"] == aid2

        # AID2's own (first, legitimate) tap must still work normally.
        with patch.object(shopify_client, "get_inventory_level", return_value=50):
            outcome2, msg2, sid2 = td.resolve_shopify_ask_pick(aid2, _HARRISON, "0")
        assert outcome2 == "preview"
        assert sid2 is not None
        assert "Variant 0" in msg2

    def test_pick_end_to_end_from_a_real_stash_executes_correctly(self):
        # Full chain: real _shopify_resolve ambiguity -> real ask_stash ->
        # resolve_shopify_ask_pick -> a fresh, executable Shopify confirm stash.
        with ExitStack() as s:
            _stub(s, variants=_variants(2))
            _preview(quantity=250)
        pending = td._peek_pending_ask(_HARRISON, _CHAN)
        ask_id = pending["ask_id"]
        with patch.object(shopify_client, "get_inventory_level", return_value=202):
            outcome, preview_text, stash_id = td.resolve_shopify_ask_pick(ask_id, _HARRISON, "1")
        assert outcome == "preview"
        assert stash_id is not None
        assert "Variant 1" in preview_text
        confirm_pending = td._peek_pending_shopify(_HARRISON, _CHAN)
        assert confirm_pending["stash_id"] == stash_id
        assert confirm_pending["inventory_item_id"] == 1001  # variant_id=1 -> item 1001

        # Confirming books the PICKED variant (never the model re-deriving it).
        with patch.object(shopify_client, "get_inventory_level", return_value=202), \
             patch.object(shopify_client, "set_inventory_level", return_value=250) as mock_set, \
             patch.object(td, "_load_shopify_write_config", return_value=_CONFIG):
            result = td.resolve_and_claim_stash(stash_id, _HARRISON, "confirm")
        assert result["outcome"] == "executed"
        mock_set.assert_called_once_with(1001, 81567023424, 250)


class TestLexiconAmbiguityStashesAsk:
    def test_lexicon_ambiguous_stashes_when_cap_respected(self):
        from cora import lexicon as _lexicon

        class _FakeEntry:
            def __init__(self, canonical, name):
                self.canonical = canonical
                self.canonical_name = name

        fake_res = _lexicon.Resolution(
            status="ambiguous", query="pure",
            candidates=(_FakeEntry("SKU-A", "Product A"), _FakeEntry("SKU-B", "Product B")),
        )
        with ExitStack() as s:
            _stub(s, variants=[])
            s.enter_context(patch.object(td, "_lexicon_active", return_value=True))
            s.enter_context(patch.object(_lexicon, "resolve", return_value=fake_res))
            result = _preview(product="pure")
        pending = td._peek_pending_ask(_HARRISON, _CHAN)
        assert pending is not None
        assert pending["ask_kind"] == "lexicon"
        assert "could mean" in result.lower()
