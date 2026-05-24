"""[HJRG] Category 4 — Per-channel behavior tuning tests for FNDR scope.

Two layers (same pattern as test_fndr_open_decisions.py):

  Layer A — pure string assertions against fndr.md and channel_classifier source.
             Always runs. No src/ imports required.

  Layer B — channel_classifier unit tests via actual import.
             Skipped automatically when channel_classifier is unavailable
             (stale bash mount, missing deps, etc.).

All Layer A tests are documentation-as-tests: if a locked per-channel rule is
edited out of fndr.md, the test fails.
"""

import pathlib
import sys

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = pathlib.Path(__file__).parent.parent
_FNDR_PATH = _REPO_ROOT / "design" / "system-prompts" / "fndr.md"
_CLASSIFIER_PATH = _REPO_ROOT / "src" / "cora" / "channel_classifier.py"

FNDR = _FNDR_PATH.read_text(encoding="utf-8")
CLASSIFIER_SRC = _CLASSIFIER_PATH.read_text(encoding="utf-8")

# ---------------------------------------------------------------------------
# Layer B import guard
# ---------------------------------------------------------------------------

_CLASSIFIER_AVAILABLE = False
try:
    sys.path.insert(0, str(_REPO_ROOT / "src"))
    from cora.channel_classifier import classify_function, tier_label  # noqa: E402
    _CLASSIFIER_AVAILABLE = True
except (ImportError, SyntaxError):
    pass

_skip_if_no_classifier = pytest.mark.skipif(
    not _CLASSIFIER_AVAILABLE,
    reason="channel_classifier import unavailable (stale bash mount or missing deps)",
)


# ===========================================================================
# Layer A — fndr.md per-channel section existence
# ===========================================================================


class TestPerChannelSectionPresent:
    """The per-channel section must exist and be labelled correctly."""

    def test_section_header_present(self):
        assert "## Per-channel behavior (FNDR scope)" in FNDR

    def test_runtime_context_reference(self):
        # Section must explain HOW it keys off the runtime context block
        assert "runtime channel context" in FNDR.lower()


# ===========================================================================
# Layer A — #fndr channel rules
# ===========================================================================


class TestFndrChannelRules:
    """`#fndr` is Harrison-only; broadest scope; all tools appropriate."""

    def test_fndr_founder_function_mentioned(self):
        assert "Function: founder" in FNDR

    def test_fndr_broadest_scope(self):
        assert "Broadest scope" in FNDR or "broadest scope" in FNDR

    def test_fndr_all_tools_appropriate(self):
        # Should explicitly say all tools are appropriate / available here
        assert "All tools" in FNDR or "all tools" in FNDR

    def test_fndr_no_redactions_beyond_cross_entity_firewall(self):
        assert "cross-entity firewall" in FNDR


# ===========================================================================
# Layer A — #hjrg-leadership rules
# ===========================================================================


class TestHjrgLeadershipRules:
    """Leadership channel: no PHI, no source attribution, no BDM client confidential."""

    def test_leadership_channel_mentioned(self):
        assert "#hjrg-leadership" in FNDR

    def test_leadership_phi_rule(self):
        # Must explicitly prohibit PHI in the leadership channel context
        section = _extract_per_channel_section(FNDR)
        assert "phi" in section.lower()

    def test_leadership_source_attribution_rule(self):
        # Source-opacity must be mentioned for leadership
        section = _extract_per_channel_section(FNDR)
        assert "source attribution" in section.lower() or "source-opacity" in section.lower()

    def test_leadership_bdm_client_confidential_rule(self):
        section = _extract_per_channel_section(FNDR)
        assert "bdm client" in section.lower()

    def test_leadership_no_financial_source_references(self):
        section = _extract_per_channel_section(FNDR)
        # Should say no sheet names / file IDs in leadership channel
        assert "sheet name" in section.lower() or "file id" in section.lower() or "tab name" in section.lower()


# ===========================================================================
# Layer A — #hjrg-finance rules
# ===========================================================================


class TestHjrgFinanceRules:
    """Finance channel: max source-opacity, Visibility CPA exclusion, defer to Justin."""

    def test_finance_channel_mentioned(self):
        assert "#hjrg-finance" in FNDR

    def test_finance_source_opacity_max(self):
        section = _extract_per_channel_section(FNDR)
        assert "source-opacity" in section.lower() or "source opacity" in section.lower()

    def test_finance_visibility_cpa_exclusion_referenced(self):
        section = _extract_per_channel_section(FNDR)
        # Must reference Visibility CPA exclusion in the finance sub-section
        assert "visibility cpa" in section.lower()

    def test_finance_defer_to_justin(self):
        section = _extract_per_channel_section(FNDR)
        assert "justin" in section.lower()


# ===========================================================================
# Layer A — #hjrg-legal rules
# ===========================================================================


class TestHjrgLegalRules:
    """Legal channel: escalate to Emily Stubbs, no legal language drafting."""

    def test_legal_channel_mentioned(self):
        assert "#hjrg-legal" in FNDR

    def test_legal_emily_stubbs_escalation(self):
        # Emily Stubbs must be the named escalation target in legal channel context
        section = _extract_per_channel_section(FNDR)
        assert "emily stubbs" in section.lower()

    def test_legal_no_draft_legal_language(self):
        section = _extract_per_channel_section(FNDR)
        assert "legal language" in section.lower() or "draft legal" in section.lower()

    def test_legal_no_contract_clauses(self):
        section = _extract_per_channel_section(FNDR)
        assert "contract clause" in section.lower() or "legal notices" in section.lower()

    def test_legal_offer_to_frame_for_emily(self):
        # Must offer an alternative (frame the ask for Emily)
        section = _extract_per_channel_section(FNDR)
        assert "emily" in section.lower()
        # Should suggest helping frame the request
        assert "frame" in section.lower() or "help harrison" in section.lower()


# ===========================================================================
# Layer A — #cowork-daily-briefs rules
# ===========================================================================


class TestCoworkDailyBriefsRules:
    """cowork-daily-briefs is automated brief drop only — no interactive Q&A."""

    def test_cowork_daily_briefs_mentioned(self):
        assert "cowork-daily-briefs" in FNDR

    def test_cowork_redirect_instruction(self):
        section = _extract_per_channel_section(FNDR)
        # Must instruct Cora to redirect @-mentions, not answer questions
        assert "morning brief" in section.lower() or "briefs only" in section.lower()

    def test_cowork_do_not_answer(self):
        section = _extract_per_channel_section(FNDR)
        # Must tell Cora NOT to answer questions in this channel
        assert "do not answer" in section.lower() or "not answer" in section.lower() or "do not respond" in section.lower()

    def test_cowork_redirects_to_appropriate_channel(self):
        section = _extract_per_channel_section(FNDR)
        assert "appropriate channel" in section.lower() or "#fndr" in section.lower()


# ===========================================================================
# Layer B — channel_classifier.py unit tests
# ===========================================================================


class TestChannelClassifierLegal:
    """Legal is now a KNOWN_FUNCTION — verify classify_function and tier_label."""

    @_skip_if_no_classifier
    def test_legal_in_known_functions_source(self):
        assert '"legal"' in CLASSIFIER_SRC or "'legal'" in CLASSIFIER_SRC

    @_skip_if_no_classifier
    def test_hjrg_legal_classifies_as_legal(self):
        assert classify_function("hjrg-legal") == "legal"

    @_skip_if_no_classifier
    def test_lex_legal_classifies_as_legal(self):
        assert classify_function("lex-legal") == "legal"

    @_skip_if_no_classifier
    def test_fndr_legal_classifies_as_legal(self):
        assert classify_function("fndr-legal") == "legal"

    @_skip_if_no_classifier
    def test_legal_tier_is_tier3_for_fndr(self):
        # Legal is not in TIER_1_FUNCTIONS — so FNDR entity + legal function → TIER_3
        assert tier_label("FNDR", "legal") == "TIER_3"


class TestChannelClassifierExisting:
    """Regression: existing classifications still hold after adding 'legal'."""

    @_skip_if_no_classifier
    def test_fndr_channel_classifies_as_founder(self):
        assert classify_function("fndr") == "founder"

    @_skip_if_no_classifier
    def test_hjrg_leadership_classifies_as_leadership(self):
        assert classify_function("hjrg-leadership") == "leadership"

    @_skip_if_no_classifier
    def test_hjrg_finance_classifies_as_finance(self):
        assert classify_function("hjrg-finance") == "finance"

    @_skip_if_no_classifier
    def test_cowork_daily_briefs_classifies_as_unknown(self):
        # cowork-daily-briefs: prefix=cowork, rest=daily-briefs, first_segment=daily → unknown
        assert classify_function("cowork-daily-briefs") == "unknown"

    @_skip_if_no_classifier
    def test_founder_function_is_tier1(self):
        assert tier_label("FNDR", "founder") == "TIER_1"

    @_skip_if_no_classifier
    def test_leadership_function_is_tier1(self):
        assert tier_label("FNDR", "leadership") == "TIER_1"

    @_skip_if_no_classifier
    def test_finance_function_is_tier1(self):
        assert tier_label("FNDR", "finance") == "TIER_1"

    @_skip_if_no_classifier
    def test_unknown_function_is_tier3(self):
        assert tier_label("FNDR", "unknown") == "TIER_3"


# ===========================================================================
# Helpers
# ===========================================================================


def _extract_per_channel_section(prompt_text: str) -> str:
    """Extract the Per-channel behavior section from fndr.md.

    Starts at '## Per-channel behavior' and ends at the next '## ' header.
    Returns the section content as a string (empty string if section not found).
    """
    import re

    match = re.search(r"^## Per-channel behavior", prompt_text, re.MULTILINE)
    if not match:
        return ""

    rest = prompt_text[match.end():]
    next_header = re.search(r"^## ", rest, re.MULTILINE)
    if next_header:
        return rest[: next_header.start()]
    return rest
