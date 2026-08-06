"""13WCF shadow-ledger map loaders (M2/S2).

These loaders are the fail-closed layer: a bad edit must fail at LOAD, loudly,
before any figure is computed. Every test here is a "would have produced a wrong
number" case, not a schema nicety.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from cora import cashflow_maps as cm


def _write(tmp_path: Path, name: str, body: dict) -> Path:
    p = tmp_path / name
    p.write_text(yaml.safe_dump(body), encoding="utf-8")
    return p


def _entity_body(**over) -> dict:
    body = {
        "excluded_realms": ["HRLLC", "OSN"],
        "derived_tabs": ["CF_SUMMARY"],
        "manual_entry_tabs": ["CF_UFL"],
        "pairs": {
            "BDM": {"tab": "CF_BigDM", "confidence": "name-match-high",
                    "confirmed": False},
        },
    }
    body.update(over)
    return body


def _category_body(**over) -> dict:
    body = {
        "categories": {
            "receipts": ["Services"],
            "operating_disbursements": ["Rent", "Utilities"],
        },
        "expense_categories": ["Rent", "Utilities"],
        "realms": {},
        "transaction_types": {},
    }
    body.update(over)
    return body


# ── the shipped seed files ──────────────────────────────────────────────────

class TestShippedSeeds:
    """The files that actually ship must load and be honestly unconfirmed."""

    def test_entity_seed_loads(self):
        em = cm.load_entity_map()
        assert em.excluded_realms >= cm.HARD_EXCLUDED_REALMS
        assert em.pairs, "seed should carry candidate pairs for Justin to confirm"

    def test_entity_seed_is_entirely_unconfirmed(self):
        """Nothing may feed accuracy math until Justin flips a row."""
        assert cm.load_entity_map().confirmed_count() == 0

    def test_lex_refuses_until_its_split_is_declared(self):
        """One QBO LEX realm vs five Lex tabs. Guessing would attribute
        LBHS/LTS/LLA activity to LLC."""
        p = cm.load_entity_map().pairing("LEX")
        assert p is not None
        assert p.resolvable is False
        assert "split is not declared" in p.refusal_reason

    def test_excluded_realms_have_no_pairing(self):
        em = cm.load_entity_map()
        for realm in cm.HARD_EXCLUDED_REALMS:
            assert em.pairing(realm) is None
            assert em.is_excluded(realm)

    def test_category_seed_loads_with_no_account_guesses(self):
        cmap = cm.load_category_map()
        assert cmap.all_category_rows()
        assert cmap.accounts == {}, "accounts are populated by --discover, not seeded"

    def test_expense_rows_are_a_subset_of_category_rows(self):
        cmap = cm.load_category_map()
        assert cmap.expense_categories <= cmap.all_category_rows()


# ── entity map validation ───────────────────────────────────────────────────

class TestEntityMapValidation:
    def test_dropping_a_hard_exclusion_is_refused(self, tmp_path):
        """Editing HRLLC out of the file must not quietly enable personal books."""
        p = _write(tmp_path, "e.yaml", _entity_body(excluded_realms=["OSN"]))
        with pytest.raises(cm.MapError, match="HRLLC"):
            cm.load_entity_map(p)

    def test_mapping_an_excluded_realm_is_refused(self, tmp_path):
        p = _write(tmp_path, "e.yaml", _entity_body(pairs={
            "HRLLC": {"tab": "CF_HR LLC", "confirmed": True},
        }))
        with pytest.raises(cm.MapError, match="excluded_realms but also has a pairing"):
            cm.load_entity_map(p)

    def test_confirmed_but_unresolvable_is_a_contradiction(self, tmp_path):
        """Ticking the box without declaring the split must not slip through."""
        p = _write(tmp_path, "e.yaml", _entity_body(pairs={
            "LEX": {"tab": None, "confirmed": True,
                    "candidate_tabs": ["CF_LLC", "CF_LBHS"]},
        }))
        with pytest.raises(cm.MapError, match="confirmed but does not resolve"):
            cm.load_entity_map(p)

    def test_two_confirmed_realms_on_one_tab_is_refused(self, tmp_path):
        """Its actuals would double-count."""
        p = _write(tmp_path, "e.yaml", _entity_body(pairs={
            "BDM": {"tab": "CF_BigDM", "confirmed": True},
            "F3E": {"tab": "CF_BigDM", "confirmed": True},
        }))
        with pytest.raises(cm.MapError, match="double-count"):
            cm.load_entity_map(p)

    def test_unconfirmed_duplicate_is_allowed_during_discovery(self, tmp_path):
        """Discovery may propose competing candidates; only CONFIRMED collides."""
        p = _write(tmp_path, "e.yaml", _entity_body(pairs={
            "BDM": {"tab": "CF_BigDM", "confirmed": False},
            "F3E": {"tab": "CF_BigDM", "confirmed": False},
        }))
        assert cm.load_entity_map(p).confirmed_count() == 0

    def test_missing_file_refuses(self, tmp_path):
        with pytest.raises(cm.MapError, match="missing"):
            cm.load_entity_map(tmp_path / "nope.yaml")

    def test_unparseable_file_refuses(self, tmp_path):
        p = tmp_path / "e.yaml"
        p.write_text("pairs: [unclosed\n", encoding="utf-8")
        with pytest.raises(cm.MapError):
            cm.load_entity_map(p)


class TestRealmResolution:
    def test_single_named_tab_resolves(self, tmp_path):
        p = _write(tmp_path, "e.yaml", _entity_body(pairs={
            "BDM": {"tab": "CF_BigDM", "confirmed": True},
        }))
        pr = cm.load_entity_map(p).pairing("BDM")
        assert pr.resolvable and pr.usable_for_accuracy

    def test_attestation_unlocks_an_ambiguous_realm(self, tmp_path):
        p = _write(tmp_path, "e.yaml", _entity_body(pairs={
            "LEX": {"tab": "CF_LLC", "candidate_tabs": ["CF_LLC", "CF_LBHS"],
                    "scope_attested": True, "confirmed": True},
        }))
        assert cm.load_entity_map(p).pairing("LEX").usable_for_accuracy

    def test_filters_unlock_an_ambiguous_realm(self, tmp_path):
        p = _write(tmp_path, "e.yaml", _entity_body(pairs={
            "LEX": {"tab": None, "candidate_tabs": ["CF_LLC", "CF_LBHS"],
                    "filters": {"class": ["LLC"]}, "confirmed": True},
        }))
        assert cm.load_entity_map(p).pairing("LEX").resolvable

    def test_ambiguous_without_attestation_refuses(self, tmp_path):
        p = _write(tmp_path, "e.yaml", _entity_body(pairs={
            "LEX": {"tab": "CF_LLC", "candidate_tabs": ["CF_LLC", "CF_LBHS"]},
        }))
        assert cm.load_entity_map(p).pairing("LEX").resolvable is False

    def test_resolvable_but_unconfirmed_never_feeds_accuracy(self, tmp_path):
        p = _write(tmp_path, "e.yaml", _entity_body())
        pr = cm.load_entity_map(p).pairing("BDM")
        assert pr.resolvable is True
        assert pr.usable_for_accuracy is False


class TestTabStatus:
    def test_status_labels(self, tmp_path):
        p = _write(tmp_path, "e.yaml", _entity_body(pairs={
            "BDM": {"tab": "CF_BigDM", "confirmed": True},
            "F3E": {"tab": "CF_F3", "confirmed": False},
        }))
        em = cm.load_entity_map(p)
        assert em.tab_status("CF_BigDM") == "qbo-covered"
        assert em.tab_status("CF_F3") == "awaiting-map-confirmation"
        assert em.tab_status("CF_SUMMARY") == "derived-rollup"
        assert em.tab_status("CF_UFL") == "manual-entry (no QBO source)"
        assert em.tab_status("CF_Nothing") == "manual-entry (no QBO source)"

    def test_a_derived_rollup_is_not_reported_as_unmapped(self, tmp_path):
        """CF_SUMMARY has no QBO counterpart BY CONSTRUCTION -- reporting it as
        'not mapped yet' would imply work that does not exist."""
        em = cm.load_entity_map(_write(tmp_path, "e.yaml", _entity_body()))
        assert em.tab_status("CF_SUMMARY") == "derived-rollup"


# ── category map validation ─────────────────────────────────────────────────

class TestCategoryMapValidation:
    def test_cc_liability_on_an_expense_row_is_refused(self, tmp_path):
        """THE cash-perimeter assertion. A card purchase is not a cash event --
        the bank-to-card payment is. This mapping double-counts carded spend."""
        p = _write(tmp_path, "c.yaml", _category_body(realms={
            "F3E": {"accounts": {"42": {
                "qbo_account": "Amex Platinum", "account_type": "Credit Card",
                "category": "Rent", "confirmed": True,
            }}},
        }))
        with pytest.raises(cm.MapError, match="double-count carded spend"):
            cm.load_category_map(p)

    def test_cc_liability_on_a_non_expense_row_is_allowed(self, tmp_path):
        """A card account may legitimately carry a non-expense mapping."""
        p = _write(tmp_path, "c.yaml", _category_body(realms={
            "F3E": {"accounts": {"42": {
                "account_type": "Credit Card", "category": "Services",
            }}},
        }))
        assert cm.load_category_map(p).mapping("F3E", "42").is_cc_liability

    def test_the_assertion_fires_even_when_unconfirmed(self, tmp_path):
        """A wrong mapping must fail at load, not when someone ticks confirmed."""
        p = _write(tmp_path, "c.yaml", _category_body(realms={
            "F3E": {"accounts": {"42": {
                "account_type": "Credit Card", "category": "Utilities",
                "confirmed": False,
            }}},
        }))
        with pytest.raises(cm.MapError, match="double-count"):
            cm.load_category_map(p)

    def test_unknown_category_is_refused(self, tmp_path):
        p = _write(tmp_path, "c.yaml", _category_body(realms={
            "F3E": {"accounts": {"7": {"account_type": "Bank",
                                       "category": "Invented Row"}}},
        }))
        with pytest.raises(cm.MapError, match="unknown category"):
            cm.load_category_map(p)

    def test_expense_row_not_in_categories_is_refused(self, tmp_path):
        p = _write(tmp_path, "c.yaml",
                   _category_body(expense_categories=["Rent", "Ghost Row"]))
        with pytest.raises(cm.MapError, match="not in categories"):
            cm.load_category_map(p)

    def test_excluded_realm_with_mappings_is_refused(self, tmp_path):
        p = _write(tmp_path, "c.yaml", _category_body(realms={
            "HRLLC": {"accounts": {"1": {"account_type": "Bank"}}},
        }))
        with pytest.raises(cm.MapError, match="hard-excluded"):
            cm.load_category_map(p)

    def test_empty_categories_is_refused(self, tmp_path):
        p = _write(tmp_path, "c.yaml", _category_body(categories={}))
        with pytest.raises(cm.MapError, match="nothing to map onto"):
            cm.load_category_map(p)


class TestCategoryResolution:
    def _map(self, tmp_path, **acct):
        base = {"account_type": "Bank", "category": "Rent"}
        base.update(acct)
        p = _write(tmp_path, "c.yaml", _category_body(realms={
            "F3E": {"accounts": {"9": base}},
        }))
        return cm.load_category_map(p)

    def test_unconfirmed_mapping_yields_no_category(self, tmp_path):
        """It lands in `uncategorized` and gets reported -- never guessed onto
        a row nobody verified."""
        cmap = self._map(tmp_path, confirmed=False)
        assert cmap.category_for("F3E", "9") is None
        assert cmap.mapping("F3E", "9") is not None

    def test_confirmed_mapping_yields_the_category(self, tmp_path):
        assert self._map(tmp_path, confirmed=True).category_for("F3E", "9") == "Rent"

    def test_unknown_account_yields_no_category(self, tmp_path):
        assert self._map(tmp_path).category_for("F3E", "999") is None

    def test_bank_vs_cc_classification(self, tmp_path):
        cmap = self._map(tmp_path, account_type="Bank")
        m = cmap.mapping("F3E", "9")
        assert m.is_bank and not m.is_cc_liability


class TestLexNameOpacity:
    """LEX account names never render on a shared surface (D-124)."""

    def test_lex_renders_its_placeholder(self, tmp_path):
        p = _write(tmp_path, "c.yaml", _category_body(realms={
            "LEX": {"accounts": {"3": {
                "qbo_account": "Lexington Client Trust - Smith",
                "placeholder": "LEX acct 3", "account_type": "Bank",
            }}},
        }))
        m = cm.load_category_map(p).mapping("LEX", "3")
        assert m.display_name() == "LEX acct 3"
        assert "Smith" not in m.display_name()

    def test_lex_without_a_placeholder_stays_opaque(self, tmp_path):
        """The fallback must NOT be the real name."""
        p = _write(tmp_path, "c.yaml", _category_body(realms={
            "LEX": {"accounts": {"3": {
                "qbo_account": "Lexington Client Trust - Smith",
                "account_type": "Bank",
            }}},
        }))
        name = cm.load_category_map(p).mapping("LEX", "3").display_name()
        assert "Smith" not in name and "Lexington" not in name
        assert name == "LEX account 3"

    def test_non_lex_realms_render_their_real_name(self, tmp_path):
        p = _write(tmp_path, "c.yaml", _category_body(realms={
            "F3E": {"accounts": {"5": {"qbo_account": "Chase Operating",
                                       "account_type": "Bank"}}},
        }))
        assert cm.load_category_map(p).mapping("F3E", "5").display_name() == "Chase Operating"
