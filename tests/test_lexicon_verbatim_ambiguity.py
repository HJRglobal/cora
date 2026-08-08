"""v2 S7 (cq-483109dfea11): the model's rewrite must not be able to skip the
ask-on-ambiguity gate.

The model canonicalizes the user's product words while composing the tool call
("set the variety pack at the office to 40" arrives as
product_query="pure variety pack"), so lexicon.resolve saw a pre-disambiguated
term, returned exact, and the which-one-did-you-mean ask never fired. Five-plus
live repros 8/1-8/2, with every existing gate green throughout -- resolve() can
only answer "is this TERM ambiguous?", never "was the USER ambiguous?".

The fix judges ambiguity on the VERBATIM turn text and lets that OVERRIDE an
exact resolution of the rewritten argument. The over-asking guard is
longest-match-wins: a user who really did name the specific product has a longer
surface present that shadows the ambiguous one.

The corpus-level cases live in tests/golden/lexicon_golden.yaml
(verbatim_cases), run by test_lexicon_golden.py and gated by
scripts/eval_lexicon.py. This file pins the TOOL seam: that the resolver
actually consults the verbatim text, and that the ask wins over the rewrite.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test-signing-secret")

import pytest  # noqa: E402

from cora import lexicon  # noqa: E402
from cora.tools import tool_dispatch as td  # noqa: E402

USER = "U0LEX"
CHANNEL = "f3-hq-inventory-adjustments"

# The live repro: what the user said vs what the model passed.
USER_SAID = "set the variety pack at the office to 40"
MODEL_PASSED = "pure variety pack"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("CORA_LEXICON", "resolve")
    lexicon.invalidate_cache()
    td._PENDING_ASK_STASH.clear()
    yield
    td._PENDING_ASK_STASH.clear()
    lexicon.invalidate_cache()


def _resolve(user_message: str, product_query: str):
    """Drive _shopify_resolve's product-resolution ladder."""
    return td._shopify_resolve(USER, {
        "_channel_name": CHANNEL,
        "_user_message": user_message,
        "product": product_query,
        "quantity": 40,
    }, channel=CHANNEL)


class TestVerbatimAmbiguityOverridesTheRewrite:
    def test_the_live_repro_asks_instead_of_resolving(self):
        out, _fresh = _resolve(USER_SAID, MODEL_PASSED)
        text = out if isinstance(out, str) else str(out)
        assert "could mean" in text and "Which one?" in text, (
            "the model's rewrite still bypassed the ask")
        assert "variety pack" in text, "the ask must name the USER's phrase"

    def test_the_ask_names_the_users_phrase_not_the_models(self):
        out, _fresh = _resolve(USER_SAID, MODEL_PASSED)
        text = out if isinstance(out, str) else str(out)
        assert "'variety pack'" in text
        assert "'pure variety pack'" not in text

    def test_it_stashes_a_picker_ask_the_user_can_answer(self):
        _resolve(USER_SAID, MODEL_PASSED)
        ask = td.get_pending_ask(USER, CHANNEL)
        assert ask is not None, "no ask stashed -- the picker card cannot render"
        assert ask.get("ask_id")
        assert len(ask.get("candidates") or []) >= 2

    def test_nothing_is_written_on_the_ask_path(self):
        out, _fresh = _resolve(USER_SAID, MODEL_PASSED)
        text = out if isinstance(out, str) else str(out)
        assert "WRITE_BLOCKED" in text
        assert "WRITE_CONFIRMED" not in text

    @pytest.mark.parametrize("said", [
        "how many variety pack should I set at the office",
        "set variety packs at the office to 40",
        "Bump the VARIETY PACK by 12 please",
    ])
    def test_other_ambiguous_phrasings_also_ask(self, said):
        out, _fresh = _resolve(said, MODEL_PASSED)
        text = out if isinstance(out, str) else str(out)
        assert "Which one?" in text


class TestItDoesNotOverAsk:
    """The fix must not turn every turn into a question."""

    @pytest.mark.parametrize("said,passed", [
        ("set the energy variety pack at HQ to 12", "energy variety pack"),
        ("set the pure variety to 40", "pure variety"),
    ])
    def test_a_specific_user_phrase_still_resolves(self, said, passed):
        out, _fresh = _resolve(said, passed)
        text = out if isinstance(out, str) else str(out)
        assert "Which one?" not in text, "over-asked on a specific user phrase"

    def test_no_verbatim_text_falls_back_to_legacy_behaviour(self):
        """Every caller that does not supply _user_message (scripts, tests, any
        path that predates the plumbing) must behave exactly as before."""
        out, _fresh = _resolve("", MODEL_PASSED)
        text = out if isinstance(out, str) else str(out)
        assert "Which one?" not in text

    def test_flag_off_disables_the_verbatim_scan(self, monkeypatch):
        monkeypatch.setenv("CORA_LEXICON", "off")
        lexicon.invalidate_cache()
        out, _fresh = _resolve(USER_SAID, MODEL_PASSED)
        text = out if isinstance(out, str) else str(out)
        assert "Which one?" not in text, "the lexicon kill switch must disable this too"

    def test_a_scan_failure_degrades_to_legacy_not_a_crash(self):
        """ADDITIVE invariant: a lexicon fault must never break the write path."""
        with patch.object(lexicon, "find_ambiguous_in_text",
                          side_effect=RuntimeError("store unreadable")):
            out, _fresh = _resolve(USER_SAID, MODEL_PASSED)
        text = out if isinstance(out, str) else str(out)
        assert "Which one?" not in text


class TestFindAmbiguousInTextUnit:
    def test_shadowing_prefers_the_longer_present_surface(self):
        assert lexicon.find_ambiguous_in_text(
            "set the energy variety pack to 12", "F3E", types=("product",)) is None

    def test_a_bare_ambiguous_surface_is_returned(self):
        r = lexicon.find_ambiguous_in_text(
            "set the variety pack to 12", "F3E", types=("product",))
        assert r is not None and r.status == "ambiguous" and r.query == "variety pack"

    def test_it_never_returns_a_canonical(self):
        """An ambiguity must never carry a pick -- that is the whole invariant."""
        r = lexicon.find_ambiguous_in_text(
            "set the variety pack to 12", "F3E", types=("product",))
        assert r.canonical == ""

    def test_substring_of_a_word_does_not_match(self):
        """Surfaces are matched on whole-token boundaries, not raw substrings."""
        assert lexicon.find_ambiguous_in_text(
            "multivariety packaging update", "F3E", types=("product",)) is None

    def test_unknown_entity_and_blank_text_are_none(self):
        assert lexicon.find_ambiguous_in_text("variety pack", "NOPE") is None
        assert lexicon.find_ambiguous_in_text("", "F3E") is None
        assert lexicon.find_ambiguous_in_text("   ", "F3E") is None

    def test_load_failure_degrades_to_none(self):
        with patch.object(lexicon, "_entries_for", side_effect=RuntimeError("boom")):
            assert lexicon.find_ambiguous_in_text("variety pack", "F3E") is None

    def test_type_filter_is_honoured(self):
        """A product-scoped scan must not trip on a same-named non-product."""
        r = lexicon.find_ambiguous_in_text(
            "set the variety pack to 12", "F3E", types=("location",))
        assert r is None
