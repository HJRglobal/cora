"""S4 fix (live-smoke 2026-08-02, cq-08166dcf283d): the app-side remember/
forget-note preview intent detector that forces Sonnet on the PREVIEW turn.
Haiku live-fabricated preview-shaped TEXT with zero tool_use on a clear
"remember that..." command -- no stash minted, a buttonless confirm card
exposed it. Mirrors test_asana_intent_detector.py's structure."""

import pytest

import cora.app as app


class TestRememberIntent:
    @pytest.mark.parametrize("msg", [
        "remember that the wifi password is x",
        "remember the wifi password is x",
        "please remember that the wifi password is x",
        "note that the wifi password is x",
        "please note that the deadline moved to Friday",
        "make a note that the deadline moved to Friday",
    ])
    def test_matches(self, msg):
        assert app._remember_or_forget_intent(msg) is True

    @pytest.mark.parametrize("msg", [
        "do you remember what we discussed last week?",
        "did you remember to save that?",
        "I still remember when we launched Pure Original",  # "remember" mid-sentence, not a command
        "what's on my plate",
        "delete the SMOKE F23 CLEAN task",
        "remember that?",  # question -- excluded
        "",
        "   ",
    ])
    def test_non_matches(self, msg):
        assert app._remember_or_forget_intent(msg) is False


class TestForgetNoteIntent:
    @pytest.mark.parametrize("msg", [
        "forget that note",
        "please forget the note about the vendor",
        "delete that note",
        "remove my note about the wifi password",
    ])
    def test_matches(self, msg):
        assert app._remember_or_forget_intent(msg) is True

    @pytest.mark.parametrize("msg", [
        "did you forget the note?",
        "forget it",  # no "note" -- not this detector's concern
        "delete the SMOKE F23 CLEAN task",  # a task, not a note
        "what notes do I have saved?",
    ])
    def test_non_matches(self, msg):
        assert app._remember_or_forget_intent(msg) is False
