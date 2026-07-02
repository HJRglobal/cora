"""WS-1 deterministic gap detection (gap_detection.py + wiring).

Covers: the two detectors (kb_miss / unknown_response), every veto class
(deflections, LEX, PHI, smalltalk, tools, notes, fallback, eval mode), the
7d dedup + daily cap + overflow counter, thread-once, the private_source DM
flag, the gap TTL, and the sentinel path's unchanged behavior.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

import cora.gap_detection as gd
import cora.gap_autofill as ga
from cora.knowledge_gaps import log_gap


QUESTION = "who is the stove vendor for the Tucson site?"


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("GAP_DETECTION_STATE_PATH",
                       str(tmp_path / "gap_detection_state.json"))
    monkeypatch.setenv("KNOWLEDGE_GAPS_LOG_PATH",
                       str(tmp_path / "knowledge-gaps.jsonl"))
    monkeypatch.setenv("RESOLVED_GAPS_PATH",
                       str(tmp_path / ".resolved-gaps.jsonl"))
    monkeypatch.setenv("GAP_AUTOFILL_STATE_PATH",
                       str(tmp_path / "gap_autofill_state.json"))
    monkeypatch.delenv("CORA_EVAL_MODE", raising=False)
    gd._THREAD_LOGGED.clear()
    return tmp_path


def _read_gaps(tmp_path):
    path = tmp_path / "knowledge-gaps.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l]


def _kb_miss_meta():
    return {"kb_search_ran": True, "kb_relevant_hits": 0, "kb_notes_hit": False}


def _detect(tmp_path, *, entity="F3E", channel="f3e-leadership", user="U1",
            question=QUESTION, response="Here is a long substantive answer "
            "about the vendor with plenty of details in it.",
            kb_meta=None, gen_meta=None, is_dm=False, thread_key=""):
    return gd.maybe_log_gap(
        entity=entity, channel=channel, user=user, question=question,
        response_text=response, latency_ms=1200,
        kb_meta=kb_meta, gen_meta=gen_meta, is_dm=is_dm, thread_key=thread_key,
    )


# ── kb_miss detector ─────────────────────────────────────────────────────────

class TestKbMiss:
    def test_kb_miss_logs(self, _isolated_state):
        det = _detect(_isolated_state, kb_meta=_kb_miss_meta())
        assert det == "kb_miss"
        gaps = _read_gaps(_isolated_state)
        assert len(gaps) == 1
        rec = gaps[0]
        assert rec["detector"] == "kb_miss"
        assert rec["entity"] == "F3E"
        assert rec["question"] == QUESTION
        assert "private_source" not in rec

    def test_no_log_when_search_never_ran(self, _isolated_state):
        assert _detect(_isolated_state, kb_meta={}) is None
        assert _detect(_isolated_state, kb_meta=None) is None
        assert not _read_gaps(_isolated_state)

    def test_no_log_when_hits_present(self, _isolated_state):
        meta = {"kb_search_ran": True, "kb_relevant_hits": 3}
        assert _detect(_isolated_state, kb_meta=meta) is None

    def test_no_log_on_personal_note_hit(self, _isolated_state):
        meta = {"kb_search_ran": True, "kb_relevant_hits": 0, "kb_notes_hit": True}
        assert _detect(_isolated_state, kb_meta=meta) is None

    def test_no_log_on_cross_entity_fallback(self, _isolated_state):
        meta = {"kb_search_ran": True, "kb_relevant_hits": 0,
                "cross_entity_fallback": True}
        assert _detect(_isolated_state, kb_meta=meta) is None

    def test_no_kb_miss_when_tools_used(self, _isolated_state):
        # A tool supplied the answer -- retrieval emptiness is not a gap.
        det = _detect(_isolated_state, kb_meta=_kb_miss_meta(),
                      gen_meta={"used_tools": True})
        assert det is None


# ── unknown_response detector ────────────────────────────────────────────────

class TestUnknownResponse:
    def test_locked_unknown_response_logs(self, _isolated_state):
        det = _detect(_isolated_state, response=gd.UNKNOWN_RESPONSE_TEXT)
        assert det == "unknown_response"
        assert _read_gaps(_isolated_state)[0]["detector"] == "unknown_response"

    def test_short_i_dont_have_that_logs(self, _isolated_state):
        det = _detect(_isolated_state, response="I don't have that right now.")
        assert det == "unknown_response"

    def test_curly_apostrophe_matches(self, _isolated_state):
        det = _detect(_isolated_state,
                      response="I don’t have that right now.")
        assert det == "unknown_response"

    def test_couldnt_find_logs(self, _isolated_state):
        det = _detect(_isolated_state,
                      response="I couldn't find any record of that vendor.")
        assert det == "unknown_response"

    def test_unknown_wins_even_with_tools(self, _isolated_state):
        # The finance tool returning UNKNOWN_RESPONSE is exactly the data-gap
        # signal we want, even though a tool ran.
        det = _detect(_isolated_state, response=gd.UNKNOWN_RESPONSE_TEXT,
                      gen_meta={"used_tools": True})
        assert det == "unknown_response"

    def test_long_helpful_answer_mentioning_lack_is_not_unknown(self, _isolated_state):
        long_reply = ("I don't have the exact figure at hand, but here is the "
                      "full context you need: " + "detail " * 80)
        assert _detect(_isolated_state, response=long_reply) is None

    def test_pin_against_financial_client_constant(self):
        # gap_detection duplicates the locked phrase to avoid importing the
        # finance connector stack on the hot path -- pin the two together.
        from cora.tools.financial_client import UNKNOWN_RESPONSE
        assert gd.UNKNOWN_RESPONSE_TEXT == UNKNOWN_RESPONSE


# ── deflection veto (guard refusals working as designed are NOT gaps) ───────

_DEFLECTION_SAMPLES = [
    "That's company financials — ask in #f3e-finance or #f3e-leadership.",
    "That's a legal matter. Reach Emily Stubbs.",
    "That's HR. Bring it to Hannah Grant or Harrison.",
    "Client-specific health info stays in the EHR. Ask the clinical lead.",
    "I'm not able to discuss that.",
    "Ownership details need Harrison.",
    "That needs Harrison.",
    "All media goes through Harrison.",
    "I don't speculate. Ask again when the data exists.",
    "Financial details are only available in this entity's dedicated finance channel.",
    "QuickBooks financial data is available in TIER_1 channels only "
    "(finance, leadership, founder, or build channels).",
    "That's outside what I can help with in this channel. Ask me in the "
    "channel for the team that owns it and I'll answer there.",
    "That topic is outside your access scope here.",
    "That's UFL — ask in an #ufl-* channel. I'm scoped to F3 Energy here.",
    "That information is confidential to LBHS and cannot be discussed here.",
    "This channel is for morning briefs only — ask me in the right channel.",
    "Company financials (P&L, cash, payroll) go in a finance channel or to Harrison.",
]


class TestDeflectionVeto:
    @pytest.mark.parametrize("reply", _DEFLECTION_SAMPLES)
    def test_deflections_never_log(self, _isolated_state, reply):
        # Even with a kb_miss-shaped meta, a deflection reply vetoes logging.
        assert _detect(_isolated_state, response=reply,
                       kb_meta=_kb_miss_meta()) is None
        assert not _read_gaps(_isolated_state)

    def test_curly_quote_deflection_vetoed(self, _isolated_state):
        assert _detect(_isolated_state,
                       response="That’s a legal matter. Reach Emily Stubbs.",
                       kb_meta=_kb_miss_meta()) is None

    def test_long_answer_containing_channel_hint_not_vetoed(self, _isolated_state):
        # The deflection veto only applies to short replies; a substantive
        # answer that happens to reference a channel is not a deflection.
        long_reply = ("The vendor relationship works like this: " + "x " * 220
                      + "and you can also ask in #f3e-ops for updates.")
        assert not gd.is_deflection(long_reply)


# ── scope vetoes ─────────────────────────────────────────────────────────────

class TestScopeVetoes:
    @pytest.mark.parametrize("entity", ["LEX", "LEX-LLC", "LEX-LBHS", "lex-lts"])
    def test_lex_entities_never_detect(self, _isolated_state, entity):
        assert _detect(_isolated_state, entity=entity,
                       kb_meta=_kb_miss_meta()) is None
        assert not _read_gaps(_isolated_state)

    def test_phi_question_never_logged(self, _isolated_state, monkeypatch):
        monkeypatch.setattr(gd, "is_phi_risk", lambda text: True)
        assert _detect(_isolated_state, kb_meta=_kb_miss_meta()) is None

    @pytest.mark.parametrize("msg", [
        "thanks!", "ok", "hello", "good morning", "got it", "ping",
        "yes", "sounds good", "ty", "hey", "cool cool",
    ])
    def test_smalltalk_never_logged(self, _isolated_state, msg):
        assert _detect(_isolated_state, question=msg,
                       kb_meta=_kb_miss_meta()) is None

    def test_eval_mode_never_logs(self, _isolated_state, monkeypatch):
        monkeypatch.setenv("CORA_EVAL_MODE", "1")
        assert _detect(_isolated_state, kb_meta=_kb_miss_meta()) is None

    def test_detector_error_is_swallowed(self, _isolated_state, monkeypatch):
        monkeypatch.setattr(gd, "_maybe_log_gap_inner",
                            lambda **kw: 1 / 0)
        assert _detect(_isolated_state, kb_meta=_kb_miss_meta()) is None


# ── dedup, cap, thread-once ─────────────────────────────────────────────────

class TestDedupAndCap:
    def test_same_question_dedups_within_window(self, _isolated_state):
        assert _detect(_isolated_state, kb_meta=_kb_miss_meta()) == "kb_miss"
        assert _detect(_isolated_state, kb_meta=_kb_miss_meta()) is None
        # Case/punctuation variants dedup too.
        assert _detect(_isolated_state,
                       question=QUESTION.upper() + "??",
                       kb_meta=_kb_miss_meta()) is None
        assert len(_read_gaps(_isolated_state)) == 1

    def test_different_entity_is_a_different_gap(self, _isolated_state):
        assert _detect(_isolated_state, entity="F3E",
                       kb_meta=_kb_miss_meta()) == "kb_miss"
        assert _detect(_isolated_state, entity="OSN",
                       kb_meta=_kb_miss_meta()) == "kb_miss"

    def test_daily_cap_and_overflow(self, _isolated_state, monkeypatch):
        monkeypatch.setenv("CORA_GAP_DETECT_DAILY_CAP", "3")
        for i in range(5):
            _detect(_isolated_state,
                    question=f"what is the vendor number {i} for tucson?",
                    kb_meta=_kb_miss_meta())
        gaps = _read_gaps(_isolated_state)
        assert len(gaps) == 3
        state = json.loads(
            (_isolated_state / "gap_detection_state.json").read_text(encoding="utf-8"))
        assert state["count"] == 3
        assert state["overflow"] == 2

    def test_cap_resets_on_new_day(self, _isolated_state, monkeypatch):
        monkeypatch.setenv("CORA_GAP_DETECT_DAILY_CAP", "1")
        assert _detect(_isolated_state, kb_meta=_kb_miss_meta()) == "kb_miss"
        # Simulate yesterday's state.
        path = _isolated_state / "gap_detection_state.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        state["day"] = "2000-01-01"
        path.write_text(json.dumps(state), encoding="utf-8")
        assert _detect(_isolated_state,
                       question="a totally different substantive question here",
                       kb_meta=_kb_miss_meta()) == "kb_miss"

    def test_dedup_window_expires(self, _isolated_state):
        assert _detect(_isolated_state, kb_meta=_kb_miss_meta()) == "kb_miss"
        path = _isolated_state / "gap_detection_state.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        old = (datetime.now(timezone.utc)
               - timedelta(days=gd._DEDUP_WINDOW_DAYS + 1)).isoformat()
        state["recent"] = {k: old for k in state.get("recent", {})}
        state["day"] = "2000-01-01"  # also reset the day counter
        path.write_text(json.dumps(state), encoding="utf-8")
        assert _detect(_isolated_state, kb_meta=_kb_miss_meta()) == "kb_miss"

    def test_thread_logs_once(self, _isolated_state):
        key = "C123:1719800000.100"
        assert _detect(_isolated_state, thread_key=key,
                       kb_meta=_kb_miss_meta()) == "kb_miss"
        assert _detect(_isolated_state, thread_key=key,
                       question="another substantive follow-up question here",
                       kb_meta=_kb_miss_meta()) is None


# ── DM privacy flag + escalation guard ──────────────────────────────────────

class TestDmPrivacy:
    def test_dm_detection_carries_private_source(self, _isolated_state):
        assert _detect(_isolated_state, channel="dm", is_dm=True,
                       kb_meta=_kb_miss_meta()) == "kb_miss"
        rec = _read_gaps(_isolated_state)[0]
        assert rec["private_source"] is True

    def test_private_gap_never_escalates(self):
        gap = {"ts": "2026-06-01T00:00:00+00:00", "entity": "F3E",
               "channel": "f3e-leadership", "question": "q", "gap": "g",
               "private_source": True}
        assert ga.should_escalate(gap) is False

    def test_dm_channel_gap_never_escalates(self):
        gap = {"ts": "2026-06-01T00:00:00+00:00", "entity": "F3E",
               "channel": "dm", "question": "q", "gap": "g"}
        assert ga.should_escalate(gap) is False

    def test_old_channel_gap_still_escalates(self):
        gap = {"ts": "2026-06-01T00:00:00+00:00", "entity": "F3E",
               "channel": "f3e-leadership", "question": "q", "gap": "g"}
        assert ga.should_escalate(gap) is True


# ── gap TTL ──────────────────────────────────────────────────────────────────

class TestGapTtl:
    def _log_gap_at(self, tmp_path, ts_iso, question="old q here please"):
        path = tmp_path / "knowledge-gaps.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": ts_iso, "entity": "FNDR", "channel": "cora-build",
                "user": "U1", "question": question, "response_chars": 10,
                "gap": "some gap", "latency_ms": 5,
            }) + "\n")

    def test_expires_only_stale_gaps(self, _isolated_state):
        now = datetime.now(timezone.utc)
        self._log_gap_at(_isolated_state,
                         (now - timedelta(days=45)).isoformat(), "stale one")
        self._log_gap_at(_isolated_state,
                         (now - timedelta(days=5)).isoformat(), "fresh one")
        assert ga.expire_stale_gaps() == 1
        open_gaps = ga.load_open_gaps()
        assert len(open_gaps) == 1
        assert open_gaps[0]["question"] == "fresh one"
        resolved = (_isolated_state / ".resolved-gaps.jsonl").read_text(encoding="utf-8")
        rec = json.loads(resolved.splitlines()[0])
        assert rec["action"] == "expired"
        assert rec["source"] == "gap_ttl"

    def test_dry_run_writes_nothing(self, _isolated_state):
        now = datetime.now(timezone.utc)
        self._log_gap_at(_isolated_state,
                         (now - timedelta(days=45)).isoformat())
        assert ga.expire_stale_gaps(dry_run=True) == 1
        assert not (_isolated_state / ".resolved-gaps.jsonl").exists()
        assert len(ga.load_open_gaps()) == 1

    def test_idempotent(self, _isolated_state):
        now = datetime.now(timezone.utc)
        self._log_gap_at(_isolated_state,
                         (now - timedelta(days=45)).isoformat())
        assert ga.expire_stale_gaps() == 1
        assert ga.expire_stale_gaps() == 0


# ── sentinel path unchanged + record shape ───────────────────────────────────

class TestSentinelPath:
    def test_sentinel_records_tag_llm_sentinel(self, _isolated_state):
        log_gap(entity="F3E", channel="ch", user="U1", question="q?",
                response_chars=10, gap="g", latency_ms=5)
        rec = _read_gaps(_isolated_state)[0]
        assert rec["detector"] == "llm_sentinel"
        assert "private_source" not in rec

    def test_app_helper_extracts_sentinel_and_tags(self, _isolated_state):
        from cora.app import _extract_and_log_gap
        text = "Answer body. [CORA_KNOWLEDGE_GAP: missing vendor list]"
        cleaned = _extract_and_log_gap(text, "F3E", "f3e-leadership", "U1",
                                       "who is the vendor for tucson?", 500)
        assert "[CORA_KNOWLEDGE_GAP" not in cleaned
        rec = _read_gaps(_isolated_state)[0]
        assert rec["detector"] == "llm_sentinel"
        assert rec["gap"] == "missing vendor list"

    def test_app_helper_runs_detectors_without_sentinel(self, _isolated_state):
        from cora.app import _extract_and_log_gap
        out = _extract_and_log_gap(
            "Plain answer with no sentinel and enough length.",
            "F3E", "f3e-leadership", "U1",
            "who is the stove vendor for tucson site?", 500,
            kb_meta=_kb_miss_meta(), gen_meta={}, is_dm=False,
            thread_key="C1:1.0",
        )
        assert out == "Plain answer with no sentinel and enough length."
        rec = _read_gaps(_isolated_state)[0]
        assert rec["detector"] == "kb_miss"
