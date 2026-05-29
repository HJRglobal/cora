"""Unit tests for help_responder.is_help_intent() and build_message()."""

import pytest

from cora.help_responder import build_message, is_help_intent


# ── is_help_intent ────────────────────────────────────────────────────────────

class TestIsHelpIntent:
    @pytest.mark.parametrize("msg", [
        "help",
        "help?",
        "HELP",
        "  help  ",
    ])
    def test_bare_help_keyword(self, msg):
        assert is_help_intent(msg) is True

    @pytest.mark.parametrize("msg", ["?", "??", "???"])
    def test_bare_question_marks(self, msg):
        assert is_help_intent(msg) is True

    @pytest.mark.parametrize("msg", [
        "what can you do",
        "what can you do?",
        "What can you do for me?",
    ])
    def test_what_can_you_do(self, msg):
        assert is_help_intent(msg) is True

    @pytest.mark.parametrize("msg", [
        "what do you do",
        "What do you do?",
    ])
    def test_what_do_you_do(self, msg):
        assert is_help_intent(msg) is True

    @pytest.mark.parametrize("msg", [
        "how do i use you",
        "how do we use you",
        "How do I use you?",
    ])
    def test_how_do_i_use_you(self, msg):
        assert is_help_intent(msg) is True

    @pytest.mark.parametrize("msg", [
        "what are your capabilities",
        "what your capabilities",
        "cora capabilities",
        "Cora capabilities",
    ])
    def test_capabilities_phrases(self, msg):
        assert is_help_intent(msg) is True

    @pytest.mark.parametrize("msg", [
        "how can you help",
        "how can you help me?",
    ])
    def test_how_can_you_help(self, msg):
        assert is_help_intent(msg) is True

    @pytest.mark.parametrize("msg", [
        "who are you",
        "Who are you?",
    ])
    def test_who_are_you(self, msg):
        assert is_help_intent(msg) is True

    def test_empty_string_is_help(self):
        assert is_help_intent("") is True

    def test_whitespace_only_is_help(self):
        assert is_help_intent("   ") is True

    def test_none_treated_as_empty(self):
        assert is_help_intent(None) is True

    @pytest.mark.parametrize("msg", [
        "what's our P&L for last month?",
        "what's open on me?",
        "show me OSN sales",
        "what was decided about the F3 launch?",
        "who is the buyer for Sprouts?",
    ])
    def test_real_questions_not_help(self, msg):
        assert is_help_intent(msg) is False

    def test_partial_match_in_real_question(self):
        # "can you help me find X" is a real request, not a capability inquiry
        assert is_help_intent("can you help me find the OSN report?") is False
        # "help" within a word should not trigger the bare-keyword pattern
        assert is_help_intent("unhelpful response") is False


# ── build_message ─────────────────────────────────────────────────────────────

class TestBuildMessage:
    def _build(self, entity="F3E", function="leadership", tier="TIER_1"):
        return build_message(entity, function, tier)

    def test_returns_string(self):
        result = self._build()
        assert isinstance(result, str)

    def test_not_empty(self):
        result = self._build()
        assert len(result) > 50

    def test_contains_try_asking(self):
        result = self._build()
        assert "Try asking:" in result

    def test_contains_read_only_disclosure(self):
        result = self._build()
        assert "read-only" in result.lower()

    def test_contains_feedback_nudge(self):
        result = self._build()
        assert "thumbs" in result.lower()

    # Entity scope in intro
    def test_fndr_entity_intro(self):
        result = build_message("FNDR", "founder", "TIER_1")
        assert "portfolio" in result.lower()

    def test_f3e_entity_intro(self):
        result = build_message("F3E", "leadership", "TIER_1")
        assert "F3" in result

    def test_lex_entity_intro(self):
        result = build_message("LEX", "leadership", "TIER_1")
        assert "Lexington" in result or "LEX" in result

    def test_osn_entity_intro(self):
        result = build_message("OSN", "leadership", "TIER_1")
        assert "One Stop" in result or "OSN" in result

    def test_unknown_entity_falls_back(self):
        result = build_message("XYZ", "ops", "TIER_3")
        assert "Cora" in result

    # Tier dispatch
    def test_tier1_mentions_financials(self):
        result = build_message("F3E", "leadership", "TIER_1")
        assert "financial" in result.lower() or "P&L" in result

    def test_tier1_mentions_quickbooks(self):
        result = build_message("F3E", "finance", "TIER_1")
        assert "QuickBooks" in result or "QBO" in result

    def test_tier3_sales_mentions_hubspot(self):
        result = build_message("F3E", "sales", "TIER_3")
        assert "HubSpot" in result or "pipeline" in result.lower()

    def test_tier3_ops_mentions_asana(self):
        result = build_message("OSN", "ops", "TIER_3")
        assert "Asana" in result

    def test_tier3_mentions_finance_redirect(self):
        result = build_message("F3E", "ops", "TIER_3")
        assert "finance" in result.lower()

    # Examples
    def test_leadership_examples_mention_pl(self):
        result = build_message("F3E", "leadership", "TIER_1")
        assert "P&L" in result or "P&amp;L" in result

    def test_sales_examples_mention_pipeline(self):
        result = build_message("F3E", "sales", "TIER_3")
        assert "pipeline" in result.lower()

    def test_ops_examples_mention_tasks(self):
        result = build_message("OSN", "ops", "TIER_3")
        assert "task" in result.lower()
