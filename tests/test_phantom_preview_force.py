"""Phantom-preview tool_choice force (S6 rider, cq-904f849bc59a + cq-8866d3f7ac3b).

The 8/9 battery proved the model narrates staged-write previews with ZERO
tool_use across cora_remember, slack_send_dm and gmail_create_draft -- no tool
call, no stash, nothing for a confirm to execute. The fix forces the tool on the
first model turn.

The load-bearing test here is the MEASURED SAFE-SET (D-169): every candidate
string is run through the REAL detector against must-force / must-not-force
expectations, including the EXISTING detectors' own positives, so a branch that
steals an Asana / code-queue / delegate turn fails the suite. That displacement
class (D-158) is the reason the DW force had to be re-cut once already.
"""

from __future__ import annotations

import re
import time

import pytest

from cora import app as capp

# ── The safe set ────────────────────────────────────────────────────────────
# (message, expected force target or None). Read this table as the contract.

_MUST_FORCE = [
    # remember (cq-904f849bc59a: the live phantom class)
    ("remember the Tucson stove vendor is Apex Appliance", "cora_remember"),
    ("Cora, remember that Larry handles the BDM invoices", "cora_remember"),
    ("please remember the gate code is 4412", "cora_remember"),
    ("note that the Pure launch slipped to September", "cora_remember"),
    ("make a note that Shaun owns the DDD renewal", "cora_remember"),
    # slack DM
    ("dm Tommy that the pallet shipped", "slack_send_dm"),
    ("DM <@U0B3VGWJTMJ> the updated fighter list", "slack_send_dm"),
    ("slack Alex the new inventory counts", "slack_send_dm"),
    ("send a dm to Hannah about tomorrow's walkthrough", "slack_send_dm"),
    ("Cora, send a message to Justin re the close pack", "slack_send_dm"),
    # gmail draft
    ("draft an email to buyer@sprouts.com about the wholesale terms",
     "gmail_create_draft"),
    ("compose an email to the copacker with the revised volumes",
     "gmail_create_draft"),
    ("please write an email to Duane about samples", "gmail_create_draft"),
    ("draft a reply to that email from Blue Chip", "gmail_create_draft"),
]

_MUST_NOT_FORCE = [
    # Questions are never imperatives.
    "do you remember what Larry said about the invoices?",
    "did you dm Tommy already?",
    "should I draft an email to the buyer?",
    "what does BDM mean?",
    # Reflexive / broadcast objects are not a teammate DM.
    "message me the numbers when you have them",
    "dm everyone the updated schedule",
    "slack the channel when it lands",
    # Mid-sentence mentions must not trigger a start-anchored detector.
    "the vendor said they would remember our account number",
    "I already sent a dm to Tommy about this",
    "we should draft an email at some point",
    # Ordinary retrieval.
    "pull up my notes on the Tucson vendor",
    "what's on my plate",
    "show me the F3E pipeline",
]

# The existing detectors' OWN positives. A new branch that returns anything
# other than these is stealing a turn (D-158).
_MUST_STAY_WITH_EXISTING = [
    ("delete the Pure launch task", "asana_delete_task"),
    ("mark the invoice task done", "asana_complete_task"),
    ("create a task to draft an email to Bob", "asana_create_task"),
    ("create a task to add the SKU to the lexicon", "asana_create_task"),
    ("add a subtask to the Pure launch task", "asana_add_subtask"),
    ("queue a code session: the confirm buttons drop the card",
     "cora_queue_code_session"),
    ("delegate a job: research brief on Sprouts", "cora_delegate_work"),
]


def _force_for(message: str) -> str | None:
    """Exactly what _dispatch_qa computes, in the same precedence order."""
    if capp._code_queue_capture_intent(message):
        return "cora_queue_code_session"
    if capp._delegate_work_intent(message):
        return "cora_delegate_work"
    staged = capp._staged_write_force_tool(message)
    if staged:
        return staged
    return capp._asana_destructive_intent(message)


@pytest.fixture(autouse=True)
def _lexicon_off(monkeypatch):
    """Production today is CORA_LEXICON=resolve, so the lexicon force is
    inactive. Tests that want it live opt in explicitly."""
    monkeypatch.setenv("CORA_LEXICON", "resolve")
    yield


class TestSafeSet:
    @pytest.mark.parametrize("message,expected", _MUST_FORCE)
    def test_must_force(self, message, expected):
        assert _force_for(message) == expected

    @pytest.mark.parametrize("message", _MUST_NOT_FORCE)
    def test_must_not_force(self, message):
        assert _force_for(message) is None

    @pytest.mark.parametrize("message,expected", _MUST_STAY_WITH_EXISTING)
    def test_existing_detectors_are_not_robbed(self, message, expected):
        assert _force_for(message) == expected

    def test_the_table_is_actually_exercised(self):
        """A guard against the table being silently emptied by a refactor."""
        assert len(_MUST_FORCE) >= 14
        assert len(_MUST_NOT_FORCE) >= 13
        assert len(_MUST_STAY_WITH_EXISTING) >= 7


class TestForgetIsNeverForced:
    """cora_forget_note needs a note_id the model can only have after a prior
    cora_my_notes call, so forcing it blind would produce a nonsensical call."""

    @pytest.mark.parametrize("message", [
        "forget that note about the vendor",
        "delete the note about the gate code",
        "remove my note on the Tucson stove",
    ])
    def test_forget_never_forces_a_tool(self, message):
        assert capp._staged_write_force_tool(message) is None

    def test_forget_still_escalates_the_model(self):
        """It keeps the Sonnet escalation it already had."""
        assert capp._remember_or_forget_intent("forget that note about the vendor")


class TestLexiconLaneGate:
    TEACH = "the term BDM means Big D Media"
    ADD = "add BDM to the lexicon"

    def test_detector_matches_regardless_of_the_lane(self):
        assert capp._lexicon_teach_intent(self.TEACH)
        assert capp._lexicon_teach_intent(self.ADD)

    def test_no_force_while_the_lane_is_below_full(self):
        """CORA_LEXICON=resolve today: the tool answers every call with
        'isn't enabled yet', so forcing it would replace a useful reply with a
        dead end."""
        assert capp._staged_write_force_tool(self.TEACH) is None
        assert capp._staged_write_force_tool(self.ADD) is None

    def test_forces_when_the_lane_is_full(self, monkeypatch):
        monkeypatch.setenv("CORA_LEXICON", "full")
        assert capp._staged_write_force_tool(self.TEACH) == "cora_lexicon_add"
        assert capp._staged_write_force_tool(self.ADD) == "cora_lexicon_add"

    def test_lane_read_per_call_not_snapshotted(self, monkeypatch):
        assert capp._staged_write_force_tool(self.ADD) is None
        monkeypatch.setenv("CORA_LEXICON", "full")
        assert capp._staged_write_force_tool(self.ADD) == "cora_lexicon_add"

    def test_teach_outranks_remember_when_live(self, monkeypatch):
        monkeypatch.setenv("CORA_LEXICON", "full")
        # A teach phrased as a note must reach the lexicon, not personal notes.
        assert capp._staged_write_force_tool(
            "add F3E to the glossary") == "cora_lexicon_add"


class TestForcedToolsAreExposedAndStaged:
    def test_every_forced_tool_is_globally_exposed(self):
        """tool_choice can only name a tool present in the turn's tool list."""
        from cora.tools.tool_dispatch import _GLOBAL_CORE_TOOLS
        for tool in ("cora_remember", "slack_send_dm", "gmail_create_draft",
                     "cora_lexicon_add"):
            assert tool in _GLOBAL_CORE_TOOLS

    def test_every_forced_tool_refuses_to_write_unconfirmed(self):
        """The safety premise: a forced first call FILES NOTHING. Each tool's
        unconfirmed branch previews and stashes rather than executing."""
        from cora.tools import tool_dispatch as td
        import inspect
        for fn_name in ("_tool_cora_remember", "_tool_slack_send_dm",
                        "_tool_gmail_create_draft", "_tool_cora_lexicon_add"):
            src = inspect.getsource(getattr(td, fn_name))
            assert "confirmed" in src, f"{fn_name} has no confirm gate"


class TestNoReDoS:
    """D-165: three self-inflicted ReDoS in this arc, all found by review not
    tests. Every new regex is timed on a 40k adversarial input; a comment does
    not fail, an assertion does."""

    # Built inside the test, never parametrized: a 40k pytest id overflows the
    # 32767-char PYTEST_CURRENT_TEST environment variable on Windows.
    @staticmethod
    def _probes() -> list[tuple[str, str]]:
        return [
            ("vocative-spaces", "cora" + " " * 40000 + "x"),
            ("dm-spaces", "dm" + " " * 40000 + "x"),
            ("draft-spaces", "draft an" + " " * 40000 + "email"),
            ("term-tail", "the term " + "a" * 40000),
            ("add-lexicon-tail", "add " + "a" * 40000 + " to the lexicon"),
            ("remember-words", "remember " + "x " * 20000),
        ]

    def test_detectors_are_linear_on_40k(self):
        for name, probe in self._probes():
            start = time.perf_counter()
            capp._staged_write_force_tool(probe)
            elapsed = time.perf_counter() - start
            assert elapsed < 1.0, f"{name}: detector took {elapsed:.2f}s on 40k"

    def test_each_new_pattern_individually(self):
        probe = "cora" + " " * 40000 + "x"
        for pattern in (capp._SLACK_DM_INTENT_RE, capp._GMAIL_DRAFT_INTENT_RE,
                        capp._LEXICON_TEACH_INTENT_RE):
            start = time.perf_counter()
            pattern.search(probe)
            assert time.perf_counter() - start < 1.0, pattern.pattern[:60]

    def test_no_unbounded_quantifier_pairs_in_the_new_patterns(self):
        """Structural guard: a bare '.*' or '.+' next to another quantifier is
        the shape that produced all three outages."""
        for pattern in (capp._SLACK_DM_INTENT_RE, capp._GMAIL_DRAFT_INTENT_RE,
                        capp._LEXICON_TEACH_INTENT_RE):
            assert ".*" not in pattern.pattern
            assert ".+" not in pattern.pattern


class TestWiring:
    def test_dispatch_orders_staged_writes_between_delegate_and_asana(self):
        """Source-level pin (AST-free but literal-anchored): the staged-write
        force must sit AFTER the code-queue/delegate branches and BEFORE the
        Asana fallthrough, or 'create a task to draft an email' breaks."""
        import inspect
        src = inspect.getsource(capp._dispatch_qa)
        i_delegate = src.index("_delegate_work_intent")
        i_staged = src.index("_staged_write_force_tool")
        i_asana = src.index("_asana_destructive_intent")
        assert i_delegate < i_staged < i_asana

    def test_forced_turn_still_skips_the_web_gate(self):
        """A forced staged-write turn is a write turn, never a research turn."""
        import inspect
        src = inspect.getsource(capp._dispatch_qa)
        assert 'web_gate_skip = "forced_tool"' in src
