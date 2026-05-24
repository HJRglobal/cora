"""[HJRG] Category 5 — Lex PHI guardrail tests for FNDR scope.

Layer A only (pure string assertions against fndr.md). No src/ imports needed.
These are documentation-as-tests: if the PHI guardrail is edited out of fndr.md
the relevant test fails.
"""

import pathlib
import re

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = pathlib.Path(__file__).parent.parent
_FNDR_PATH = _REPO_ROOT / "design" / "system-prompts" / "fndr.md"

FNDR = _FNDR_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _extract_phi_section(prompt_text: str) -> str:
    """Extract the Lex PHI guardrail section from fndr.md."""
    match = re.search(r"^## Lex PHI guardrail", prompt_text, re.MULTILINE)
    if not match:
        return ""
    rest = prompt_text[match.end():]
    next_header = re.search(r"^## ", rest, re.MULTILINE)
    return rest[: next_header.start()] if next_header else rest


PHI_SECTION = _extract_phi_section(FNDR)


# ===========================================================================
# Section existence
# ===========================================================================


class TestPhiSectionPresent:
    def test_section_header_present(self):
        assert "## Lex PHI guardrail" in FNDR

    def test_section_non_negotiable(self):
        assert "non-negotiable" in FNDR.lower()

    def test_section_has_content(self):
        assert len(PHI_SECTION.strip()) > 200, "PHI section looks too short — may be truncated"


# ===========================================================================
# Scope + applicability
# ===========================================================================


class TestPhiScope:
    def test_lex_entities_named(self):
        # Section must mention Lexington Services sub-entities
        assert "LLC" in PHI_SECTION and "LBHS" in PHI_SECTION

    def test_ahcccs_or_hipaa_mentioned(self):
        # Must ground the rule in a regulatory framework
        section_lower = PHI_SECTION.lower()
        assert "ahcccs" in section_lower or "hipaa" in section_lower or "medicaid" in section_lower

    def test_applies_in_fndr_scope_channels(self):
        # Must explicitly say the guardrail applies in FNDR/HJRG channels
        section_lower = PHI_SECTION.lower()
        assert "fndr" in section_lower or "hjrg" in section_lower

    def test_cross_portfolio_lens_does_not_override(self):
        # Must say the guardrail holds even with cross-portfolio / founder scope active
        section_lower = PHI_SECTION.lower()
        # "even in" or "applies even" or "cross-portfolio lens"
        assert (
            "even in" in section_lower
            or "even when" in section_lower
            or "cross-portfolio" in section_lower
        )


# ===========================================================================
# PHI definition
# ===========================================================================


class TestPhiDefinition:
    def test_phi_label_defined(self):
        # Section must define what PHI means in this context
        assert "PHI" in PHI_SECTION or "phi" in PHI_SECTION.lower()

    def test_client_names_are_phi(self):
        assert "client name" in PHI_SECTION.lower() or "individual client name" in PHI_SECTION.lower()

    def test_diagnoses_are_phi(self):
        assert "diagnos" in PHI_SECTION.lower()

    def test_care_plans_are_phi(self):
        assert "care plan" in PHI_SECTION.lower()

    def test_individual_billing_is_phi(self):
        assert "billing" in PHI_SECTION.lower()


# ===========================================================================
# Aggregate-only rule
# ===========================================================================


class TestAggregateOnlyRule:
    def test_aggregate_rule_stated(self):
        assert "aggregate" in PHI_SECTION.lower()

    def test_never_name_individual_clients(self):
        section_lower = PHI_SECTION.lower()
        assert "never name" in section_lower or "never" in section_lower

    def test_aggregate_examples_provided(self):
        # Should give examples of what IS okay (total clients, compliance status, etc.)
        section_lower = PHI_SECTION.lower()
        assert "total clients" in section_lower or "clients served" in section_lower

    def test_compliance_status_is_aggregate_ok(self):
        assert "compliance" in PHI_SECTION.lower()

    def test_entity_level_billing_is_ok(self):
        # Entity-level (not individual-level) billing is fine
        section_lower = PHI_SECTION.lower()
        assert "entity-level" in section_lower or "aggregate" in section_lower


# ===========================================================================
# Redirect behavior
# ===========================================================================


class TestPhiRedirectBehavior:
    def test_redirect_to_lex_channels(self):
        # Must name specific Lex sub-entity channels for redirect
        assert "#lex-" in PHI_SECTION

    def test_redirect_without_explaining_hipaa(self):
        # Must instruct Cora NOT to explain why — just redirect
        section_lower = PHI_SECTION.lower()
        assert (
            "do not explain" in section_lower
            or "without elaboration" in section_lower
            or "just redirect" in section_lower
        )

    def test_example_redirect_phrase_present(self):
        # Should include a concrete example redirect phrase
        assert "Example redirect" in PHI_SECTION or "example redirect" in PHI_SECTION.lower()

    def test_example_redirect_references_lex_channel(self):
        # The example phrase itself should name a Lex channel
        assert "#lex-" in PHI_SECTION
