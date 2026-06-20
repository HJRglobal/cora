"""WS11: F3E inventory snapshot correctness.

The live snapshot was internally contradictory -- negative on-hand (Shopify
oversell), variant count mislabeled/inflated as "SKUs", no consistency guard.
These pin the fixes: clamp negatives, dedup variants by id, distinguish SKU vs
variant count + total units.
"""

from unittest.mock import patch

from cora.connectors import shopify_client as sc


def _prod(pid, title, variants):
    return {"id": pid, "title": title, "variants": variants}


def _var(vid, title, sku, qty):
    return {"id": vid, "title": title, "sku": sku,
            "inventory_quantity": qty, "inventory_item_id": vid}


class TestInventoryCorrectness:
    def setup_method(self):
        sc._cache_clear()

    def _status(self, products, threshold=10):
        with patch.object(sc, "_store_config", return_value=("store.myshopify.com", "tok")), \
             patch.object(sc, "_get_paginated", return_value=products):
            return sc.get_inventory_status(low_stock_threshold=threshold)

    def test_negative_inventory_clamped_to_zero(self):
        v = self._status([_prod(1, "F3 Energy Original", [_var(11, "Original", "EN-ORIG", -5)])])
        assert v[0].qty_on_hand == 0          # oversell negative -> 0, never "-5 units"
        assert v[0].low_stock is True

    def test_duplicate_product_variants_deduped(self):
        p = _prod(1, "F3 Energy", [
            _var(11, "Original", "EN-ORIG", 100),
            _var(12, "Citrus", "EN-CIT", 50),
        ])
        v = self._status([p, p])              # same product twice (pagination overlap)
        assert len(v) == 2                    # deduped by variant id, not 4

    def test_format_distinguishes_skus_from_variants_and_total(self):
        v = self._status([_prod(1, "F3 Energy", [
            _var(11, "Original", "EN-ORIG", 5),
            _var(12, "Citrus", "EN-CIT", 8),
            _var(13, "Variety", "EN-VAR", 3),
        ])])
        text = sc.format_inventory_for_llm(v, low_stock_only=True)
        assert "3 SKUs, 3 variants" in text   # accurate SKU vs variant count
        assert "16 units on hand" in text     # 5 + 8 + 3, self-consistent total

    def test_full_listing_reports_total_units(self):
        v = self._status([_prod(1, "F3 Energy", [_var(11, "Original", "EN-ORIG", 5000)])])
        text = sc.format_inventory_for_llm(v, low_stock_only=False)
        assert "5,000 units on hand" in text  # never "all at 0" while units > 0

    def test_location_available_clamped(self):
        sc._cache_clear()
        with patch.object(sc, "_store_config", return_value=("store", "tok")), \
             patch.object(sc, "_get_locations", return_value={"nimbl 3pl": 99}), \
             patch.object(sc, "_get_paginated", side_effect=[
                 [{"inventory_item_id": 11, "available": -7}],                       # inv_levels
                 [_prod(1, "F3 Pure Variety", [_var(11, "", "PURE-VAR", 0)])],       # products
             ]):
            skus = sc.get_inventory_by_location("nimbl")
        assert skus and skus[0].available == 0  # negative available clamped
