"""Slice 3 (pipeline-integrity bundle, 2026-08-05) -- cq-5c6ff15610bd + D-128:
gap-autofill known_answer eligibility.

answer_quality_ok screens the DRAFTED ANSWER for durability. mine_eligibility is
upstream of it and screens the EXCHANGE ITSELF -- whether this gap is the kind of
thing a durable known fact can ever be, however well Haiku words it.

D-128 (cascaded 2026-08-05, cited not re-proposed): an exchange Cora herself
flagged as unresolved/uncertain, or that encodes a live disagreement between two
systems of record, is DECISION material, not FACT material.

The three named regressions from the kickoff, with VERBATIM live text from
_brain/known-answers/f3e.md:
  (a) the 8/3 fighter-compliance exchange -> decision-lane candidate, NO known_answer
  (b) the 8/5 capability-ask exchange     -> NO known_answer
  (c) a clean factual exchange            -> still converts (the lane keeps working)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import gap_autofill as ga


class _Chunk:
    """Minimal stand-in for a KB SearchResult (only .content is read here)."""

    def __init__(self, content: str, source: str = "slack"):
        self.content = content
        self.source = source
        self.distance = 0.5
        self.metadata: dict = {}
        self.title = "#f3-athletes"
        self.date_modified = None


# ── (a) the 8/3 fighter-compliance exchange, verbatim ────────────────────────

FIGHTER_GAP = {
    "ts": "2026-08-03T02:53:33.591766+00:00",
    "entity": "F3E",
    "channel": "f3-athletes",
    "user": "U0B2RM2JYJ1",
    "detector": "llm_sentinel",
    "question": ('no that is false this is the updated list of fighters that have '
                 'completed for the month of June: '
                 '<https://docs.google.com/spreadsheets/d/12-2ij|'
                 'F3_Fighter_IG_Tracking_MonthlyTabs_AutoComplete_ColorCoded.xlsx>'),
    "gap": ('User referenced an "updated" '
            'F3_Fighter_IG_Tracking_MonthlyTabs_AutoComplete_Col'),
}
# Cora's own reply, as it was mined into the approved known-answer on 8/5.
FIGHTER_EVIDENCE = [
    _Chunk("[Harrison] no that is false this is the updated list of fighters"),
    _Chunk(
        "[Cora] The discrepancy exists because the live fighter_compliance tool "
        "shows 0/61 completions for July, while the user referenced a separate "
        "Google Sheet they believe is the source of record. The root cause is a "
        "sync/access issue: Cora's compliance tool queries a fixed backend source, "
        "but lacks direct live connector access to verify whether the Google Sheet "
        "the user shared is actually the same file wired into that backend system."
    ),
]

# ── (b) the 8/5 capability-ask exchange, verbatim ────────────────────────────

CAPABILITY_GAP = {
    "ts": "2026-07-28T20:35:19.157162+00:00",
    "entity": "F3E",
    "channel": "f3e-leadership",
    "user": "U0B3VGWJTMJ",
    "detector": "code_queue_route",
    "question": ("what are some examples of tasks that Hannah Tommy or Alex could "
                 "delegate to you?"),
    "gap": "capability/knowledge ask routed from code-queue classifier",
}
CAPABILITY_EVIDENCE = [
    _Chunk("[Hannah] can you pull Q2 QBO P&L numbers into a spreadsheet by entity"),
    _Chunk("[Tommy] create the task with the slack thread reference attached"),
]

# ── (c) a clean factual exchange ─────────────────────────────────────────────

CLEAN_GAP = {
    "ts": "2026-06-23T10:00:00.000000+00:00",
    "entity": "F3E",
    "channel": "f3e-leadership",
    "user": "U0B3VGWJTMJ",
    "detector": "unknown_response",
    "question": "What size or specs are required for the 4 homepage heroes?",
    "gap": "F3 Pure homepage hero specs not in KB",
}
CLEAN_EVIDENCE = [
    _Chunk("[Larry] heroes are 2880x1620px 16:9 retina, JPG, max 800KB"),
    _Chunk("[Larry] sRGB at 72dpi, keep critical content in the center safe zone"),
]


# ── the three named regressions ──────────────────────────────────────────────

class TestNamedRegressions:
    def test_a_fighter_exchange_is_ineligible_as_disputed(self):
        eligible, why = ga.mine_eligibility(FIGHTER_GAP, FIGHTER_EVIDENCE)
        assert eligible is False
        assert why == ga.MINE_INELIGIBLE_DISPUTED

    def test_a_fighter_exchange_stages_no_known_answer(self):
        """draft_answer must refuse BEFORE the Haiku call -- the belt at the
        chokepoint, so no caller can stage it."""
        assert ga.draft_answer(FIGHTER_GAP, FIGHTER_EVIDENCE) is None

    def test_a_fighter_exchange_produces_a_decision_lane_candidate(self, monkeypatch):
        seen: list[dict] = []
        import cora.knowledge_review as kr
        monkeypatch.setattr(kr, "propose_update",
                            lambda **kw: seen.append(kw) or True)
        update_id = ga.route_disputed_to_decision_lane(FIGHTER_GAP, FIGHTER_EVIDENCE)
        assert update_id == "gap-dispute-2026-08-03T02:53:33.591766+00:00"
        assert len(seen) == 1
        assert seen[0]["update_type"] == kr.UPDATE_TYPE_DECISION
        # Provenance-stamped: the card must not read as a mined fact.
        assert "UNRESOLVED" in seen[0]["payload"]["decision_text"]
        assert seen[0]["payload"]["source"] == "gap_autofill_d128"
        assert seen[0]["payload"]["gap_ts"] == FIGHTER_GAP["ts"]
        # The [ENTITY] prefix is what decision_inbox.entity_of reads.
        assert seen[0]["description"].startswith("[F3E]")

    def test_b_capability_ask_stages_no_known_answer(self):
        eligible, why = ga.mine_eligibility(CAPABILITY_GAP, CAPABILITY_EVIDENCE)
        assert eligible is False
        assert why == ga.MINE_INELIGIBLE_ALREADY_ROUTED
        assert ga.draft_answer(CAPABILITY_GAP, CAPABILITY_EVIDENCE) is None

    def test_b_the_internal_classifier_string_is_the_marker(self):
        """is_capability_ask does NOT match this question (no second-person
        capability verb) -- the definitive signal is detector=='code_queue_route',
        i.e. the classifier ALREADY dispositioned this gap into the code queue.
        Mining it was double handling, and it is how an internal classifier string
        became a durable F3E "fact" on 2026-08-05."""
        from cora.knowledge_gaps import is_capability_ask
        assert is_capability_ask(CAPABILITY_GAP["question"]) is False
        assert "code_queue_route" in ga._ALREADY_ROUTED_DETECTORS

    def test_c_clean_factual_exchange_still_converts(self):
        """The milestone lane must keep working -- a precision fix that stops all
        conversion would be a worse outcome than the defect."""
        eligible, why = ga.mine_eligibility(CLEAN_GAP, CLEAN_EVIDENCE)
        assert eligible is True and why == ""


# ── the four eligibility classes ─────────────────────────────────────────────

class TestEligibilityClasses:
    def _gap(self, **kw):
        base = {"ts": "2026-08-01T00:00:00+00:00", "entity": "F3E",
                "detector": "unknown_response", "question": "q", "gap": "g"}
        base.update(kw)
        return base

    @pytest.mark.parametrize("question", [
        "can you access the RepRally dashboard?",
        "do you have access to the fighter sheet?",
        "are you able to pull wholesale listings?",
        "I just shared you the tracker",
    ])
    def test_capability_asks_excluded(self, question):
        ok, why = ga.mine_eligibility(self._gap(question=question))
        assert ok is False and why == ga.MINE_INELIGIBLE_CAPABILITY_ASK

    @pytest.mark.parametrize("text", [
        "what were the Clover store numbers?",
        "which role-briefing-config entry drives Matt's brief?",
        "what does fighter_compliance report for July?",
        "is Make.com scenario 4768887 still nudging?",
        "does the silent auto-approve still run?",
    ])
    def test_retired_processes_excluded(self, text):
        ok, why = ga.mine_eligibility(self._gap(question=text))
        assert ok is False and why == ga.MINE_INELIGIBLE_RETIRED

    @pytest.mark.parametrize("text", [
        "[QA] test the confirm button",
        "smoke test for the inventory tool",
        "this is a test, ignore this",
        "what's my test locker code?",
    ])
    def test_qa_scaffolding_excluded(self, text):
        ok, why = ga.mine_eligibility(self._gap(question=text))
        assert ok is False and why == ga.MINE_INELIGIBLE_QA

    @pytest.mark.parametrize("text", [
        "what's on my plate today",
        "how much cash do we have right now",
        "what's our balance at the moment",
        "what's my uptime",
    ])
    def test_ephemeral_questions_excluded(self, text):
        ok, why = ga.mine_eligibility(self._gap(question=text))
        assert ok is False and why == ga.MINE_INELIGIBLE_EPHEMERAL

    def test_ephemeral_screens_the_question_not_the_answer(self):
        """_SNAPSHOT_RE already screens ANSWERS; this class is the QUESTION mirror.
        A durable question whose answer happens to say "currently" is answer_quality_ok's
        problem, not an eligibility exclusion."""
        ok, _why = ga.mine_eligibility(
            self._gap(question="what are the wholesale MOQ tiers?",
                      gap="MOQ ladder not in KB"))
        assert ok is True


# ── D-128 detection ──────────────────────────────────────────────────────────

class TestD128Detection:
    def _gap(self, question="q", gap="g"):
        return {"ts": "2026-08-01T00:00:00+00:00", "entity": "F3E",
                "detector": "llm_sentinel", "question": question, "gap": gap}

    @pytest.mark.parametrize("cora_reply", [
        "I can't tell which number is right",
        "I cannot verify that sheet",
        "I shouldn't keep asserting the 0/61 figure",
        "I'm not certain which source is authoritative",
        "this is worth Harrison's attention",
        "flagging this for Harrison",
        "this needs Harrison's call",
        "the tool lacks direct live connector access to verify the sheet",
        "I can't reconcile the two counts",
    ])
    def test_cora_own_uncertainty_markers(self, cora_reply):
        assert ga.is_disputed_exchange(self._gap(), [_Chunk(f"[Cora] {cora_reply}")])

    @pytest.mark.parametrize("text", [
        "there's a discrepancy between the sheet and the backend",
        "the backend conflicts with the tracker",
        "those numbers don't match",
        "the two systems are out of sync",
        "no that is false, this is the updated list",
        "two different sources say different things",
        "which one is authoritative?",
        "this is still disputed",
    ])
    def test_source_disagreement_markers(self, text):
        assert ga.is_disputed_exchange(self._gap(question=text))

    def test_evidence_is_screened_not_just_the_gap(self):
        """Cora's replies live in the EVIDENCE: search_slack_evidence drops only
        PURE-bot chunks and deliberately keeps mixed human-ask + Cora-reply chunks
        (cq-8d16969e85fb). Screening the gap text alone would miss the whole class."""
        clean_gap = self._gap(question="what is the July completion count?",
                              gap="completion count not in KB")
        assert ga.is_disputed_exchange(clean_gap, None) is False
        assert ga.is_disputed_exchange(
            clean_gap, [_Chunk("[Cora] I can't tell which figure is right")]) is True

    def test_clean_exchange_is_not_disputed(self):
        assert ga.is_disputed_exchange(CLEAN_GAP, CLEAN_EVIDENCE) is False


# ── fail-closed ──────────────────────────────────────────────────────────────

class TestFailClosed:
    def test_screen_error_is_ineligible(self, monkeypatch):
        monkeypatch.setattr(ga, "is_disputed_exchange",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        ok, why = ga.mine_eligibility(CLEAN_GAP, CLEAN_EVIDENCE)
        assert ok is False and why == ga.MINE_INELIGIBLE_SCREEN_ERROR

    def test_capability_screen_import_error_is_ineligible(self, monkeypatch):
        import cora.knowledge_gaps as kg
        monkeypatch.setattr(kg, "is_capability_ask",
                            lambda t: (_ for _ in ()).throw(RuntimeError("down")))
        ok, why = ga.mine_eligibility(CLEAN_GAP, CLEAN_EVIDENCE)
        assert ok is False and why == ga.MINE_INELIGIBLE_SCREEN_ERROR

    def test_draft_answer_never_calls_the_model_when_ineligible(self, monkeypatch):
        """Cost + egress: an ineligible exchange must not reach an LLM at all."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-should-not-be-used")
        called = {"n": 0}

        def _explode(*_a, **_k):
            called["n"] += 1
            raise AssertionError("the model must not be called for an ineligible gap")
        monkeypatch.setitem(sys.modules, "anthropic", type(
            "M", (), {"Anthropic": _explode})())
        assert ga.draft_answer(FIGHTER_GAP, FIGHTER_EVIDENCE) is None
        assert called["n"] == 0


# ── the rejection must ROUTE or LOG, never vanish ────────────────────────────

class TestRejectionsAreVisible:
    def test_every_reason_is_registered_in_the_aggregator(self):
        """A new reason string that is not in _REASON_CODES buckets to "other" and
        goes invisible in the review digest -- the exact failure this lens guards."""
        reasons = [
            ga.MINE_INELIGIBLE_CAPABILITY_ASK, ga.MINE_INELIGIBLE_ALREADY_ROUTED,
            ga.MINE_INELIGIBLE_RETIRED, ga.MINE_INELIGIBLE_QA,
            ga.MINE_INELIGIBLE_EPHEMERAL, ga.MINE_INELIGIBLE_DISPUTED,
            ga.MINE_INELIGIBLE_SCREEN_ERROR,
        ]
        for r in reasons:
            assert r in ga._REASON_CODES, r
            code, _decaying = ga._REASON_CODES[r]
            assert code != "other"

    def test_rejection_log_line_matches_the_aggregator_regex(self, caplog):
        import logging
        with caplog.at_level(logging.INFO, logger="cora.gap_autofill"):
            ga.draft_answer(FIGHTER_GAP, FIGHTER_EVIDENCE)
        lines = [r.getMessage() for r in caplog.records]
        matched = [ln for ln in lines if ga._REJECTION_LOG_RE.search(ln)]
        assert matched, lines
        gap_id, reason = ga._REJECTION_LOG_RE.search(matched[0]).groups()
        assert gap_id == FIGHTER_GAP["ts"]
        assert ga._REASON_CODES[reason.strip()][0] == "disputed_d128"

    def test_reasons_carry_no_raw_gap_text(self):
        """PHI-safety of the log/aggregate path: the reason is always one of the
        fixed canned strings, never mined content."""
        _ok, why = ga.mine_eligibility(FIGHTER_GAP, FIGHTER_EVIDENCE)
        assert "fighter" not in why.lower()
        assert "google" not in why.lower()
        assert why in ga._REASON_CODES


# ── D-128 routing guards (LEX / PHI / cap / idempotency) ─────────────────────

class TestRoutingGuards:
    def _lex_gap(self):
        return {**FIGHTER_GAP, "entity": "LEX-LLC"}

    def test_lex_entity_never_routes(self, monkeypatch):
        seen: list[dict] = []
        import cora.knowledge_review as kr
        monkeypatch.setattr(kr, "propose_update", lambda **kw: seen.append(kw) or True)
        assert ga.route_disputed_to_decision_lane(self._lex_gap(), FIGHTER_EVIDENCE) is None
        assert seen == []

    def test_phi_flagged_never_routes(self, monkeypatch):
        seen: list[dict] = []
        import cora.knowledge_review as kr
        monkeypatch.setattr(kr, "propose_update", lambda **kw: seen.append(kw) or True)
        monkeypatch.setattr(ga, "is_any_phi", lambda t: True)
        assert ga.route_disputed_to_decision_lane(FIGHTER_GAP, FIGHTER_EVIDENCE) is None
        assert seen == []

    def test_phi_screen_error_fails_closed(self, monkeypatch):
        seen: list[dict] = []
        import cora.knowledge_review as kr
        monkeypatch.setattr(kr, "propose_update", lambda **kw: seen.append(kw) or True)
        monkeypatch.setattr(ga, "is_any_phi",
                            lambda t: (_ for _ in ()).throw(RuntimeError("screen down")))
        assert ga.route_disputed_to_decision_lane(FIGHTER_GAP, FIGHTER_EVIDENCE) is None
        assert seen == []

    def test_missing_gap_ts_never_routes(self, monkeypatch):
        seen: list[dict] = []
        import cora.knowledge_review as kr
        monkeypatch.setattr(kr, "propose_update", lambda **kw: seen.append(kw) or True)
        assert ga.route_disputed_to_decision_lane({**FIGHTER_GAP, "ts": ""}) is None
        assert seen == []

    def test_update_id_is_deterministic_so_reruns_dedup(self, monkeypatch):
        """propose_update is idempotent on update_id, so a nightly re-run of the
        same still-open gap cannot mint a second card."""
        import cora.knowledge_review as kr
        monkeypatch.setattr(kr, "propose_update", lambda **kw: True)
        a = ga.route_disputed_to_decision_lane(FIGHTER_GAP, FIGHTER_EVIDENCE)
        b = ga.route_disputed_to_decision_lane(FIGHTER_GAP, FIGHTER_EVIDENCE)
        assert a == b

    def test_propose_failure_is_swallowed(self, monkeypatch):
        import cora.knowledge_review as kr
        monkeypatch.setattr(kr, "propose_update",
                            lambda **kw: (_ for _ in ()).throw(RuntimeError("disk full")))
        assert ga.route_disputed_to_decision_lane(FIGHTER_GAP, FIGHTER_EVIDENCE) is None

    def test_block_reason_is_shared_with_the_dry_run(self):
        """The dry-run report and the real route must run the SAME screens: the
        first live dry-run said "would route to the decisions lane" for a LEX gap
        the real path refuses. A dry run that over-promises is worse than none,
        because it IS the rollout gate."""
        assert ga.decision_route_block_reason(FIGHTER_GAP) == ""
        assert "LEX" in ga.decision_route_block_reason(self._lex_gap())
        assert ga.decision_route_block_reason({**FIGHTER_GAP, "ts": ""}) == "no gap ts"

    def test_block_reason_phi_and_error(self, monkeypatch):
        monkeypatch.setattr(ga, "is_any_phi", lambda t: True)
        assert ga.decision_route_block_reason(FIGHTER_GAP) == "PHI-flagged"
        monkeypatch.setattr(ga, "is_any_phi",
                            lambda t: (_ for _ in ()).throw(RuntimeError("down")))
        assert "fail-closed" in ga.decision_route_block_reason(FIGHTER_GAP)

    def test_per_run_cap_exists_and_matches_the_ask_cap(self):
        """Cap parity with MAX_ASKS_PER_RUN: a corpus-wide sweep must not flood
        Harrison's never-expiring decision cards."""
        assert ga.MAX_DECISION_ROUTES_PER_RUN == ga.MAX_ASKS_PER_RUN == 3

    def test_decision_lane_screens_lex_and_phi_downstream_too(self):
        """Third-belt check: decision_inbox re-screens at the drain and again at the
        durable write, so this routing is not the only guard."""
        from cora import decision_inbox
        excluded, reason = decision_inbox.screen_decision(
            {"description": "[LEX-LLC] Unresolved: something", "payload": {"entity": "LEX-LLC"}})
        assert excluded is True and reason == "lex_entity"
