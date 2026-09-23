"""Tests for the human-authored order spec parser/validator.

Every check here runs WITHOUT a live API call (live checks are
test_deposco_orders_preflight.py's job). These are the checks that make the
two SO 081226 defect classes unreachable before a payload is ever built.
"""

from __future__ import annotations

import pytest

from cora.deposco_orders import spec as spec_mod

VALID_WHOLESALE = {
    "channel": "wholesale",
    "buyer_or_fc_code": "GOTHAM",
    "reference": "4471",
    "authored_by": "Harrison",
    "freight_terms": "Prepaid",
    "notes": "Confirm dock height before dispatch.",
    "lines": [
        {"sku": "PURE-Original", "qty": 208, "unit_price": "21.70"},
        {"sku": "PURE-Citrus", "qty": 208, "unit_price": "21.70"},
    ],
}

VALID_FBA = {
    "channel": "fba",
    "buyer_or_fc_code": "GEU3",
    "reference": "FBA19P6VCSJW",
    "authored_by": "Alex",
    "reference2": "779356-180",
    "notes": "Manufacturer barcode, no FNSKU. EXP 2028-08-12.",
    "lines": [
        {"sku": "F3VPM4", "qty": 180, "unit_price": "32.99", "msku": "F3VPM"},
    ],
}


def _errors(raw):
    return spec_mod.validate_spec(raw).errors


class TestChannelValidation:
    def test_valid_wholesale_spec_passes(self):
        result = spec_mod.validate_spec(VALID_WHOLESALE)
        assert result.ok, result.errors
        assert result.spec.channel == "wholesale"

    def test_valid_fba_spec_passes(self):
        result = spec_mod.validate_spec(VALID_FBA)
        assert result.ok, result.errors

    def test_unknown_channel_is_refused(self):
        raw = dict(VALID_WHOLESALE, channel="tiktok_fbt")
        assert any("channel must be one of" in e for e in _errors(raw))

    def test_wfs_channel_refused_no_address_book_yet(self):
        raw = dict(VALID_FBA, channel="wfs", buyer_or_fc_code="WALFC1",
                    reference="9480757WFA")
        errors = _errors(raw)
        assert any("WFS channel has no address book" in e for e in errors)


class TestHumanOnlyAuthorship:
    def test_missing_authored_by_is_refused(self):
        raw = dict(VALID_WHOLESALE)
        del raw["authored_by"]
        assert any("authored_by is required" in e for e in _errors(raw))

    def test_blank_authored_by_is_refused(self):
        raw = dict(VALID_WHOLESALE, authored_by="   ")
        assert any("authored_by is required" in e for e in _errors(raw))


class TestBuyerAndFcResolution:
    def test_unknown_buyer_code_is_refused(self):
        raw = dict(VALID_WHOLESALE, buyer_or_fc_code="NOTABUYER")
        assert any("not found in deposco-customers.yaml" in e for e in _errors(raw))

    def test_unknown_fc_code_is_refused(self):
        raw = dict(VALID_FBA, buyer_or_fc_code="ZZZ9")
        assert any("not found in deposco-amazon-fc.yaml" in e for e in _errors(raw))

    def test_buyer_code_is_case_insensitive(self):
        raw = dict(VALID_WHOLESALE, buyer_or_fc_code="gotham")
        result = spec_mod.validate_spec(raw)
        assert result.ok, result.errors
        assert result.spec.buyer_or_fc_code == "GOTHAM"


class TestStrayLineRefusal:
    """The 081226 stray-Energy-line defect class, refused at validation."""

    def test_sku_outside_the_pinned_map_is_refused(self):
        raw = dict(VALID_WHOLESALE, lines=[
            {"sku": "F3-BOGUS-SKU", "qty": 10, "unit_price": "21.70"},
        ])
        assert any("is not in the pinned SKU map" in e for e in _errors(raw))

    def test_sku_outside_the_buyers_allowed_set_is_refused(self):
        """Gotham = Pure only -- an Energy line is refused HERE, not caught
        by Nimbl after the fact."""
        raw = dict(VALID_WHOLESALE, lines=[
            {"sku": "F3-Original", "qty": 10, "unit_price": "18.00"},
        ])
        errors = _errors(raw)
        assert any("allowed-SKU set" in e for e in errors)

    def test_a_pure_line_is_allowed_for_gotham(self):
        raw = dict(VALID_WHOLESALE, lines=[
            {"sku": "F3-PureE-V4F", "qty": 10, "unit_price": "20.00"},
        ])
        result = spec_mod.validate_spec(raw)
        assert result.ok, result.errors

    def test_fba_has_no_per_buyer_sku_restriction(self):
        raw = dict(VALID_FBA, lines=[
            {"sku": "PURE-Original", "qty": 100, "unit_price": "21.70"},
        ])
        result = spec_mod.validate_spec(raw)
        assert result.ok, result.errors


class TestLineFieldValidation:
    def test_missing_lines_is_refused(self):
        raw = dict(VALID_WHOLESALE, lines=[])
        assert any("at least one line is required" in e for e in _errors(raw))

    def test_qty_zero_is_refused(self):
        raw = dict(VALID_WHOLESALE, lines=[
            {"sku": "PURE-Original", "qty": 0, "unit_price": "21.70"},
        ])
        assert any("qty must be > 0" in e for e in _errors(raw))

    def test_qty_negative_is_refused(self):
        raw = dict(VALID_WHOLESALE, lines=[
            {"sku": "PURE-Original", "qty": -5, "unit_price": "21.70"},
        ])
        assert any("qty must be > 0" in e for e in _errors(raw))

    def test_non_integer_qty_is_refused(self):
        raw = dict(VALID_WHOLESALE, lines=[
            {"sku": "PURE-Original", "qty": "a bunch", "unit_price": "21.70"},
        ])
        assert any("qty must be an integer" in e for e in _errors(raw))

    def test_fractional_qty_is_refused_not_silently_truncated(self):
        """D-051 review, 2026-09-23: int(208.5) == 208 -- a fractional qty
        must be refused outright, never silently rounded to a DIFFERENT
        number than what was typed."""
        raw = dict(VALID_WHOLESALE, lines=[
            {"sku": "PURE-Original", "qty": 208.5, "unit_price": "21.70"},
        ])
        result = spec_mod.validate_spec(raw)
        assert not result.ok, "a fractional qty must never validate"
        assert any("whole number" in e for e in result.errors)

    def test_fractional_qty_as_a_string_is_also_refused(self):
        raw = dict(VALID_WHOLESALE, lines=[
            {"sku": "PURE-Original", "qty": "208.5", "unit_price": "21.70"},
        ])
        assert any("whole number" in e for e in _errors(raw))

    def test_boolean_qty_is_refused_not_coerced(self):
        """bool is an int subclass in Python -- int(True) == 1 would silently
        accept a typo'd `qty: true`."""
        raw = dict(VALID_WHOLESALE, lines=[
            {"sku": "PURE-Original", "qty": True, "unit_price": "21.70"},
        ])
        assert any("qty must be an integer" in e for e in _errors(raw))

    def test_whole_number_float_qty_is_allowed(self):
        """208.0 is a whole number even though it arrived as a float (a
        common YAML-parsing shape) -- only a genuine fraction is refused."""
        raw = dict(VALID_WHOLESALE, lines=[
            {"sku": "PURE-Original", "qty": 208.0, "unit_price": "21.70"},
        ])
        result = spec_mod.validate_spec(raw)
        assert result.ok, result.errors
        assert result.spec.lines[0].qty == 208

    def test_missing_unit_price_is_refused(self):
        raw = dict(VALID_WHOLESALE, lines=[{"sku": "PURE-Original", "qty": 10}])
        assert any("unit_price is required" in e for e in _errors(raw))

    def test_non_numeric_unit_price_is_refused(self):
        raw = dict(VALID_WHOLESALE, lines=[
            {"sku": "PURE-Original", "qty": 10, "unit_price": "cheap"},
        ])
        assert any("is not a number" in e for e in _errors(raw))

    def test_negative_unit_price_is_refused(self):
        raw = dict(VALID_WHOLESALE, lines=[
            {"sku": "PURE-Original", "qty": 10, "unit_price": "-1"},
        ])
        assert any("unit_price must be >= 0" in e for e in _errors(raw))

    def test_sub_cent_unit_price_is_refused(self):
        """D-051 review, 2026-09-23: a float-summed, once-rounded order total
        can diverge from the displayed per-line unitPrice values by a cent
        when a price carries more than 2 decimal places -- refused here,
        before it can reach payload.py's sum."""
        raw = dict(VALID_WHOLESALE, lines=[
            {"sku": "PURE-Original", "qty": 10, "unit_price": "21.705"},
        ])
        errors = _errors(raw)
        assert any("more than 2 decimal places" in e for e in errors)

    def test_whole_dollar_unit_price_is_allowed(self):
        raw = dict(VALID_WHOLESALE, lines=[
            {"sku": "PURE-Original", "qty": 10, "unit_price": "20"},
        ])
        assert spec_mod.validate_spec(raw).ok

    def test_zero_unit_price_is_allowed(self):
        """Observed live on real FBA orders (V3, 2026-09-23) -- zero is a
        real, if unhelpful, value; not itself a defect."""
        raw = dict(VALID_FBA, lines=[
            {"sku": "F3VPM4", "qty": 180, "unit_price": "0.0"},
        ])
        result = spec_mod.validate_spec(raw)
        assert result.ok, result.errors

    @pytest.mark.parametrize("bad_price", ["nan", "NaN", "inf", "Infinity", "-inf"])
    def test_non_finite_unit_price_is_refused_not_crashed(self, bad_price):
        """D-051 review, 2026-09-23: Decimal('nan') < 0 RAISES
        InvalidOperation (uncaught) and Decimal('inf').as_tuple() has a
        non-numeric exponent that would crash the decimal-places check --
        both must be refused before either comparison runs."""
        raw = dict(VALID_WHOLESALE, lines=[
            {"sku": "PURE-Original", "qty": 10, "unit_price": bad_price},
        ])
        result = spec_mod.validate_spec(raw)
        assert not result.ok
        assert any("finite" in e for e in result.errors)


class TestReferenceShape:
    def test_wholesale_reference_must_be_alnum_and_hyphen_only(self):
        raw = dict(VALID_WHOLESALE, reference="PO 4471")
        assert any("does not match the expected wholesale shape" in e for e in _errors(raw))

    def test_fba_reference_must_look_like_an_amazon_shipment_id(self):
        raw = dict(VALID_FBA, reference="not-a-shipment-id")
        assert any("does not match the expected fba shape" in e for e in _errors(raw))

    def test_missing_reference_is_refused(self):
        raw = dict(VALID_WHOLESALE)
        del raw["reference"]
        assert any("reference is required" in e for e in _errors(raw))

    def test_reference_case_is_canonicalized_to_uppercase(self):
        """D-051 review, 2026-09-23: '4471a' and '4471A' derived the
        IDENTICAL Deposco order number (build_order_number uppercases its own
        copy) while the ORIGINAL-case reference landed in the payload's
        otherReferenceNumber/customerOrderNumber -- a false idempotency
        collision. One canonical form (set here) removes it."""
        raw = dict(VALID_WHOLESALE, reference="4471a")
        result = spec_mod.validate_spec(raw)
        assert result.ok, result.errors
        assert result.spec.reference == "4471A"

    def test_two_specs_differing_only_in_reference_case_are_no_longer_identical(self):
        lower = spec_mod.validate_spec(dict(VALID_WHOLESALE, reference="4471a")).spec
        upper = spec_mod.validate_spec(dict(VALID_WHOLESALE, reference="4471A")).spec
        assert lower.reference == upper.reference, (
            "both must canonicalize to the SAME reference -- they are the same PO"
        )

    def test_reference_too_long_for_the_buyer_code_is_refused_here_not_later(self):
        """D-051 review, 2026-09-23: the wholesale reference regex alone
        allows up to 29 chars, but payload.py's derived order number
        (F3E-W-{BUYER}-{PO}) has its own <=30-char limit -- for GOTHAM (6
        chars) that leaves only ~17 chars for the reference. This must be
        refused at validation, matching the module's own 'refused HERE,
        never mangled later' claim, not surface later as a PayloadBuildError."""
        raw = dict(VALID_WHOLESALE, reference="A" * 20)
        result = spec_mod.validate_spec(raw)
        assert not result.ok
        assert any("too long" in e for e in result.errors)

    def test_a_reference_that_fits_the_buyer_code_budget_is_allowed(self):
        raw = dict(VALID_WHOLESALE, reference="A" * 17)
        result = spec_mod.validate_spec(raw)
        assert result.ok, result.errors


class TestWfsUnconditionalRefusal:
    """D-051 review, 2026-09-23: WFS and FBA shared the exact same address
    lookup (an Amazon-only facility map) with the WFS refusal firing only on
    a MISS -- refused unconditionally now, before any lookup."""

    def test_wfs_is_refused_even_if_the_fc_code_matches_a_real_amazon_facility(self):
        raw = {
            "channel": "wfs", "buyer_or_fc_code": "GEU3", "reference": "9480757WFA",
            "authored_by": "Harrison",
            "lines": [{"sku": "PURE-Original", "qty": 10, "unit_price": "21.70"}],
        }
        result = spec_mod.validate_spec(raw)
        assert not result.ok
        assert any("WFS channel has no address book" in e for e in result.errors)


class TestMskuCrossCheck:
    """The F3VPM vs F3VPM4 class -- an optional per-line msku is cross-checked
    against the pinned Amazon merchant-SKU map."""

    def test_correct_msku_passes(self):
        raw = dict(VALID_FBA, lines=[
            {"sku": "F3VPM4", "qty": 180, "unit_price": "32.99", "msku": "F3VPM"},
        ])
        result = spec_mod.validate_spec(raw)
        assert result.ok, result.errors

    def test_wrong_msku_is_refused(self):
        raw = dict(VALID_FBA, lines=[
            {"sku": "F3VPM4", "qty": 180, "unit_price": "32.99", "msku": "F3VPM4"},
        ])
        errors = _errors(raw)
        assert any("does not match the pinned Amazon merchant SKU" in e for e in errors)

    def test_absent_msku_is_not_checked(self):
        raw = dict(VALID_FBA, lines=[
            {"sku": "F3VPM4", "qty": 180, "unit_price": "32.99"},
        ])
        result = spec_mod.validate_spec(raw)
        assert result.ok, result.errors


class TestFreightTerms:
    def test_prepaid_is_accepted(self):
        raw = dict(VALID_WHOLESALE, freight_terms="Prepaid")
        assert spec_mod.validate_spec(raw).ok

    def test_third_party_is_accepted(self):
        raw = dict(VALID_WHOLESALE, freight_terms="Third Party")
        assert spec_mod.validate_spec(raw).ok

    def test_unknown_freight_terms_is_refused(self):
        raw = dict(VALID_WHOLESALE, freight_terms="Collect")
        assert any("freight_terms must be" in e for e in _errors(raw))


class TestYamlParsing:
    def test_parse_and_validate_a_valid_yaml_document(self):
        text = """
channel: wholesale
buyer_or_fc_code: GOTHAM
reference: "4471"
authored_by: Harrison
lines:
  - sku: PURE-Original
    qty: 208
    unit_price: "21.70"
"""
        result = spec_mod.parse_and_validate(text)
        assert result.ok, result.errors

    def test_invalid_yaml_syntax_is_reported_not_raised(self):
        result = spec_mod.parse_and_validate("channel: [unterminated")
        assert not result.ok
        assert result.errors

    def test_empty_document_is_reported(self):
        result = spec_mod.parse_and_validate("")
        assert not result.ok
        assert "empty" in result.errors[0]

    def test_non_mapping_root_is_reported(self):
        result = spec_mod.parse_and_validate("- just\n- a\n- list\n")
        assert not result.ok
