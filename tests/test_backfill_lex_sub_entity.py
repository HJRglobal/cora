"""Tests for the LEX sub-entity backfill script."""
import importlib
import re
import sys
import types
from pathlib import Path

import pytest

# Import the detection function directly without needing a real DB
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import backfill_lex_sub_entity as _mod


def _detect(title: str, content: str = "") -> str | None:
    return _mod._detect_sub_entity(title, content)


class TestDetectSubEntity:
    def test_llc_bracket_tag(self):
        assert _detect("[LEX-LLC] Grow to 750 Members") == "LEX-LLC"

    def test_llc_hcbs_keyword(self):
        assert _detect("HCBS billing report Q1") == "LEX-LLC"

    def test_llc_supported_living(self):
        assert _detect("Supported Living placements 2026") == "LEX-LLC"

    def test_lts_bracket_tag(self):
        assert _detect("[LEX-LTS] DDD Therapy Revalidation") == "LEX-LTS"

    def test_lts_provider_type(self):
        assert _detect("Provider Type 15 deadline", "revalidation required by June 30") == "LEX-LTS"

    def test_lts_lexington_therapeutic(self):
        assert _detect("Lexington Therapeutic Services overview") == "LEX-LTS"

    def test_lbhs_acronym(self):
        assert _detect("LBHS Q2 census numbers") == "LEX-LBHS"

    def test_lbhs_jared(self):
        assert _detect("", "Jared Harker sent the BHRF report") == "LEX-LBHS"

    def test_lla_bracket_tag(self):
        assert _detect("[LEX-LLA] Sandy Patel services agreement") == "LEX-LLA"

    def test_lla_sandy_patel(self):
        assert _detect("Sandy Patel membership repurchase 2023") == "LEX-LLA"

    def test_general_lex_returns_none(self):
        assert _detect("Lexington payroll report May 2026") is None

    def test_general_training_returns_none(self):
        assert _detect("Staff training slides Q2") is None

    def test_ambiguous_returns_none(self):
        # Contains both LLC and LBHS keywords -- ambiguous, stay NULL
        assert _detect("[LEX-LLC] LBHS billing overlap") is None

    def test_empty_returns_none(self):
        assert _detect("", "") is None

    def test_case_insensitive(self):
        assert _detect("hcbs client documentation") == "LEX-LLC"

    def test_lbhs_copa(self):
        assert _detect("COPA audit 2026", "behavioral health records") == "LEX-LBHS"


class TestPhiGuardIntegration:
    """Ensure the phi_guard centralized names are accessible."""

    def test_visibility_cpa_names_importable(self):
        from cora.phi_guard import VISIBILITY_CPA_NAMES, is_visibility_cpa_mention
        assert "hayden greber" in VISIBILITY_CPA_NAMES
        assert is_visibility_cpa_mention("call Hayden Greber about the report")
        assert not is_visibility_cpa_mention("call Harrison about the report")

    def test_all_expected_names_present(self):
        from cora.phi_guard import VISIBILITY_CPA_NAMES
        for name in ("andrew stubbs", "sarah bertoglio", "emily stubbs",
                     "michael dibenedetto", "andrew lee", "visibility cpa"):
            assert name in VISIBILITY_CPA_NAMES, f"Missing: {name}"
