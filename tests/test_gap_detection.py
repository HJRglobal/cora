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
    monkeypatch.setenv("KB_DECISION_LOG_PATH",
                       str(tmp_path / "kb-retrieval-decisions.jsonl"))
    monkeypatch.delenv("CORA_EVAL_MODE", raising=False)
    monkeypatch.delenv("CORA_KB_MISS_SHADOW_FLOOR", raising=False)
    gd._THREAD_LOGGED.clear()
    return tmp_path


def _read_decisions(tmp_path):
    path = tmp_path / "kb-retrieval-decisions.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l]


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
            kb_meta=None, gen_meta=None, is_dm=False, thread_key="",
            thread_context=False):
    return gd.maybe_log_gap(
        entity=entity, channel=channel, user=user, question=question,
        response_text=response, latency_ms=1200,
        kb_meta=kb_meta, gen_meta=gen_meta, is_dm=is_dm, thread_key=thread_key,
        thread_context=thread_context,
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


# ── unknown_response length-INDEPENDENCE (D-066 follow-up calibration hotfix) ──
#
# The 2026-07-02 #cora-build smokes proved the blanket _UNKNOWN_MAX_CHARS gate
# skipped the archetypal miss: Cora's answer-first house style pads a genuine
# "I don't have that" reply to 550-700 chars, so a 566-char locked-phrase reply
# and a 657-char "I don't have that context." reply BOTH went undetected. The
# fix runs the prefix-anchored openers regardless of length; the anywhere-in
# containment path keeps the short-reply guard.

# Rebuilt from the two live smokes (both >_UNKNOWN_MAX_CHARS, both real misses).
_SMOKE_LOCKED_OPENER = (
    gd.UNKNOWN_RESPONSE_TEXT
    + " In the meantime, here are a few pointers you can chase: the office "
      "facilities log, the standing ops SOP folder, and whoever currently owns "
      "vendor onboarding. I've also flagged this so the right owner can add a "
      "canonical answer, and once that lands I'll be able to answer it directly "
      "the next time anyone asks in this channel or by DM."
)  # opens with the exact locked phrase -> startswith(locked) path
_SMOKE_THAT_CONTEXT = (
    "I don't have that context. There's nothing in what I can see that covers "
    "an official SOP for that, and I don't want to guess at something that "
    "reads like a policy. Your best bets are the ops SOP folder in Drive, the "
    "facilities/office-management owner, and the leadership channel for the "
    "team that would own it -- any of those is more likely to have a definitive "
    "answer, and I've noted the gap so it can be filled going forward."
)  # opens with a prefix-anchored _UNKNOWN_RES shape -> regex path


class TestUnknownResponseLengthIndependence:
    def test_smoke_replies_are_long(self):
        # Guard the fixtures themselves: both MUST exceed the old gate, or the
        # test would pass trivially under the buggy code.
        assert len(_SMOKE_LOCKED_OPENER) > gd._UNKNOWN_MAX_CHARS
        assert len(_SMOKE_THAT_CONTEXT) > gd._UNKNOWN_MAX_CHARS

    def test_long_locked_phrase_opener_fires(self, _isolated_state):
        assert gd.is_unknown_response(_SMOKE_LOCKED_OPENER) is True
        assert _detect(_isolated_state, response=_SMOKE_LOCKED_OPENER) == "unknown_response"

    def test_long_that_context_opener_fires(self, _isolated_state):
        # The exact 657-char smoke shape ("I don't have that context. ...").
        assert gd.is_unknown_response(_SMOKE_THAT_CONTEXT) is True
        assert _detect(_isolated_state, response=_SMOKE_THAT_CONTEXT) == "unknown_response"

    def test_long_couldnt_find_opener_fires(self, _isolated_state):
        reply = ("I couldn't find any record of that in what I can see. " +
                 "Here is where to look instead: " + "the ops folder, " * 40)
        assert len(reply) > gd._UNKNOWN_MAX_CHARS
        assert gd.is_unknown_response(reply) is True

    def test_long_reply_quoting_locked_phrase_midtext_not_unknown(self, _isolated_state):
        # FALSE-POSITIVE GUARD: a long reply that EMBEDS the locked phrase as a
        # quote (not the reply's own verdict) must NOT fire -- the containment
        # path is length-capped, and the openers are prefix-anchored.
        reply = ("Here is the full vendor picture for the Tucson site. " +
                 "background " * 40 +
                 " During the call Larry said, \"" + gd.UNKNOWN_RESPONSE_TEXT +
                 "\" but the signed PO from Nimbl already confirms the order.")
        assert len(reply) > gd._UNKNOWN_MAX_CHARS
        assert gd.is_unknown_response(reply) is False
        assert _detect(_isolated_state, response=reply) is None

    def test_long_helpful_answer_with_the_determiner_still_not_unknown(self, _isolated_state):
        # Regression companion to the existing short case: "I don't have THE
        # exact figure, but here's the answer" opens with a determiner OUTSIDE
        # the matched set (that/this/any/it), so a long helpful answer that
        # merely notes a lack must still NOT fire even without the length gate.
        reply = ("I don't have the exact figure at hand, but here is the full "
                 "breakdown you need: " + "line item " * 60)
        assert len(reply) > gd._UNKNOWN_MAX_CHARS
        assert gd.is_unknown_response(reply) is False

    def test_short_containment_still_fires(self):
        # Behavior-preserving: a SHORT reply containing the locked phrase still
        # fires via the length-capped containment path (unchanged pre/post-fix).
        reply = "Per policy: " + gd.UNKNOWN_RESPONSE_TEXT
        assert len(reply) <= gd._UNKNOWN_MAX_CHARS
        assert gd.is_unknown_response(reply) is True


class TestDeflectionCollisionAfterWidening:
    """The widened unknown matcher must not swallow deflections. Deflection
    openers ('That's...', 'I'm not able...', 'I don't speculate') are DISJOINT
    from unknown openers ('I don't have that/this/any/it', 'I couldn't find',
    'I have no record'), and the deflection veto still runs BEFORE the unknown
    check."""

    def test_long_deflection_opener_not_caught_by_widened_unknown(self):
        # A long refusal opening with a deflection phrase must not read as an
        # unknown_response (the openers do not overlap).
        for opener in (
            "That's company financials",
            "That's a legal matter",
            "I'm not able to discuss that",
            "I don't speculate",
        ):
            reply = opener + ". " + "context " * 80  # > any length gate
            assert len(reply) > gd._UNKNOWN_MAX_CHARS
            assert gd.is_unknown_response(reply) is False, opener

    def test_deflection_still_vetoes_when_it_opens_unknown_shaped(self, _isolated_state):
        # A short reply that OPENS unknown-shaped but is really a deflection
        # (contains a deflection phrase) is vetoed first -- deflection wins.
        reply = ("I don't have that here. That's company financials -- ask in "
                 "#f3e-finance.")
        assert len(reply) <= gd._DEFLECTION_MAX_CHARS
        assert _detect(_isolated_state, response=reply,
                       kb_meta=_kb_miss_meta()) is None
        assert not _read_gaps(_isolated_state)

    def test_long_deflection_opening_unknown_shaped_not_unknown(self, _isolated_state):
        # Adversarial review MED (Finding 2): is_deflection caps at 400 chars,
        # but the widened unknown prefix path is length-independent. A >400-char
        # blocked-topic reply that OPENS unknown-shaped AND carries a deflection
        # phrase must NOT read as unknown_response (the in-predicate deflection
        # re-check catches it where is_deflection's 400 cap misses).
        reply = ("I don't have visibility into that here. That's company "
                 "financials -- ask in #f3e-finance or bring it to Harrison. " +
                 "background " * 45)
        assert len(reply) > gd._DEFLECTION_MAX_CHARS
        assert gd.is_unknown_response(reply) is False
        # No kb_meta -> kb_miss can't fire either; the reply must not log at all.
        assert _detect(_isolated_state, response=reply) is None


class TestRetrievalDecisionLog:
    """2026-07-09 kb_miss calibration slice: a per-query decision stream +
    a SHADOW kb_miss verdict. LOG-ONLY -- the shadow verdict must never create a
    gap, DM, route, or write. Its purpose is to build the answerable distance
    distribution Harrison locks the real floor from (~1 week out)."""

    def _good(self, best):
        # A good retrieval: chunks under the gate, distance = best.
        return {"kb_search_ran": True, "kb_relevant_hits": 5,
                "kb_notes_hit": False, "kb_chunks_returned": 12,
                "kb_best_distance": best}

    def test_good_answer_logs_decision_no_gap_no_shadow(self, _isolated_state):
        det = _detect(_isolated_state, kb_meta=self._good(0.62),
                      response="Here is a solid, well-sourced answer with detail.")
        assert det is None                       # no gap on a good answer
        assert not _read_gaps(_isolated_state)
        rows = _read_decisions(_isolated_state)
        assert len(rows) == 1
        assert rows[0]["best_distance"] == 0.62
        assert rows[0]["shadow_kb_miss"] is False
        assert rows[0]["relevant_hits"] == 5

    def test_weak_retrieval_flags_shadow_but_logs_no_gap(self, _isolated_state):
        # best_distance ABOVE the provisional floor with no other source ->
        # shadow_kb_miss True, but NO real gap (real kb_miss needs 0 hits).
        det = _detect(_isolated_state, kb_meta=self._good(1.22),
                      response="Here is a weakly-grounded but substantive answer.")
        assert det is None
        assert not _read_gaps(_isolated_state)     # real detector unchanged
        rows = _read_decisions(_isolated_state)
        assert len(rows) == 1 and rows[0]["shadow_kb_miss"] is True
        assert rows[0]["shadow_floor"] == gd._DEFAULT_SHADOW_KB_MISS_FLOOR

    def test_shadow_floor_env_override(self, _isolated_state, monkeypatch):
        monkeypatch.setenv("CORA_KB_MISS_SHADOW_FLOOR", "0.90")
        _detect(_isolated_state, kb_meta=self._good(0.95),
                response="A substantive answer grounded just past a tight floor.")
        rows = _read_decisions(_isolated_state)
        assert rows[0]["shadow_floor"] == 0.90 and rows[0]["shadow_kb_miss"] is True

    def test_shadow_excludes_tool_note_fallback_thread(self, _isolated_state):
        # used_tools -> not a shadow miss even at high distance.
        _detect(_isolated_state, kb_meta=self._good(1.4),
                gen_meta={"used_tools": True},
                response="A tool-answered reply with real content.")
        # notes_hit -> not a shadow miss.
        meta_notes = self._good(1.4); meta_notes["kb_notes_hit"] = True
        _detect(_isolated_state, kb_meta=meta_notes,
                response="An answer that came from the asker's own note.")
        # thread_context -> not a shadow miss.
        _detect(_isolated_state, kb_meta=self._good(1.4), thread_context=True,
                response="An answer sourced from the prior thread messages.")
        rows = _read_decisions(_isolated_state)
        assert len(rows) == 3
        assert all(r["shadow_kb_miss"] is False for r in rows)

    def test_no_decision_line_when_search_never_ran(self, _isolated_state):
        # Pure tool path (no KB search) -> no retrieval decision to record.
        _detect(_isolated_state, kb_meta={"kb_search_ran": False},
                gen_meta={"used_tools": True},
                response="A calendar tool answer with no KB retrieval at all.")
        assert _read_decisions(_isolated_state) == []

    def test_deflection_does_not_pollute_the_distribution(self, _isolated_state):
        # A guard refusal is vetoed BEFORE the decision log, so its weak
        # retrieval never enters the answerable distribution.
        _detect(_isolated_state, kb_meta=self._good(1.4),
                response="That's company financials -- ask in #f3e-finance.")
        assert _read_decisions(_isolated_state) == []

    def test_decision_record_has_no_question_or_response_text(self, _isolated_state):
        _detect(_isolated_state, question="what brand of coffee is in the kitchen?",
                kb_meta=self._good(1.25),
                response="I do have a detailed answer with sensitive wording here.")
        row = _read_decisions(_isolated_state)[0]
        blob = json.dumps(row).lower()
        assert "coffee" not in blob and "sensitive wording" not in blob
        # only numeric + entity/channel fields
        assert set(row) == {"ts", "entity", "channel", "best_distance",
                            "chunks_returned", "relevant_hits", "notes_hit",
                            "cross_entity_fallback", "used_tools",
                            "thread_context", "shadow_kb_miss", "shadow_floor"}

    def test_unknown_response_gap_still_logs_and_decision_recorded(self, _isolated_state):
        # A genuine unknown miss: the real unknown_response gap fires AND the
        # decision line is recorded (both streams, no interference).
        det = _detect(_isolated_state, response=_SMOKE_THAT_CONTEXT,
                      kb_meta=self._good(1.074))
        assert det == "unknown_response"
        assert len(_read_gaps(_isolated_state)) == 1
        assert len(_read_decisions(_isolated_state)) == 1


class TestDeflectionPointerVsReason:
    """2026-07-02 ROUND-2 addendum: Cora's answer-first house style appends
    "...or ask in #channel" pointers to GENUINE unknown replies. The old
    anywhere-in-reply deflection veto (<=400 chars) ate those short misses. Now a
    channel POINTER never vetoes an unknown-shape OPENER; only an unambiguous
    refusal REASON does. A pure redirect (non-unknown opener) is still vetoed."""

    # Near-verbatim rebuild of the live 2026-07-02 miss ("who maintains the
    # office plants?"): opens with the locked-ish unknown shape, ends with a
    # channel pointer, no refusal reason, <= the 400-char deflection cap.
    _PLANT_MISS = (
        "I don't have that information. There's nothing in what I can see that "
        "names who handles office plant care, and I don't want to guess -- your "
        "best bet is whoever owns office/facilities, or ask in #hjrg or "
        "#hjrg-operations and someone there can point you to the right person."
    )

    def test_plant_miss_fixture_shape(self):
        # Guard the fixture: it must be <= the 400-char cap (so it exercises the
        # OLD bug path) and carry a channel pointer with no refusal reason.
        assert len(self._PLANT_MISS) <= gd._DEFLECTION_MAX_CHARS
        assert any(rx.search(gd._normalize_reply(self._PLANT_MISS))
                   for rx in gd._DEFLECTION_POINTER_RES)
        assert not any(rx.search(gd._normalize_reply(self._PLANT_MISS))
                       for rx in gd._DEFLECTION_REASON_RES)

    def test_plant_miss_now_fires_unknown_response(self, _isolated_state):
        assert gd.is_unknown_response(self._PLANT_MISS) is True
        assert _detect(_isolated_state, question="who maintains the office plants?",
                       response=self._PLANT_MISS) == "unknown_response"

    def test_short_unknown_opener_plus_pointer_fires(self, _isolated_state):
        # The core bug in miniature.
        reply = "I don't have that. You could ask in #hjrg-operations."
        assert gd.is_unknown_response(reply) is True
        assert _detect(_isolated_state, response=reply) == "unknown_response"

    def test_reason_led_redirect_with_channel_still_vetoed(self, _isolated_state):
        # A true guard refusal states its REASON up front (non-unknown opener) --
        # must stay vetoed even though it also names a channel.
        reply = "That's company financials -- ask me in #f3e-finance."
        assert _detect(_isolated_state, response=reply,
                       kb_meta=_kb_miss_meta()) is None
        assert not _read_gaps(_isolated_state)

    def test_unknown_opener_carrying_a_reason_still_vetoed(self, _isolated_state):
        # An unknown-shaped opener that ALSO carries an unambiguous refusal reason
        # is a guard working as designed -> stays vetoed.
        reply = ("I don't have that. That's a legal matter -- reach Emily Stubbs, "
                 "or ask in #hjrg.")
        assert gd.is_unknown_response(reply) is False
        assert _detect(_isolated_state, response=reply,
                       kb_meta=_kb_miss_meta()) is None

    def test_long_unknown_opener_with_pointer_fires(self, _isolated_state):
        # Length-independence AND pointer-exclusion together: a >400-char miss
        # that opens unknown-shaped and ends with a channel pointer fires.
        reply = ("I don't have that context. " + "background detail " * 30
                 + " Your best route is to ask in #hjrg-operations.")
        assert len(reply) > gd._DEFLECTION_MAX_CHARS
        assert gd.is_unknown_response(reply) is True
        assert _detect(_isolated_state, response=reply) == "unknown_response"

    def test_is_deflection_public_api_unchanged(self):
        # The generic is_deflection() predicate is UNCHANGED (pointer still
        # matches anywhere, <=400): only the gap-logging path applies the
        # opener-aware REASON-only scoping. Documents the deliberate split.
        assert gd.is_deflection("I don't have that. Ask in #hjrg.") is True


class TestToolRefusalNotLogged:
    """Adversarial review MED (Finding 1): the widened matcher would otherwise
    log a tool's own "not found" relay as an unknown_response gap. Those are
    tool RESULTS, not knowledge gaps, and the person_dossier relay carries a
    person name into the egress-bound log. The unknown_response branch is gated
    on used_tools (mirroring kb_miss), with an exception for the locked finance
    phrase."""

    def test_meeting_picklist_relay_with_tools_not_logged(self, _isolated_state):
        # Mirrors meeting_actions.py:1376 ("I couldn't find a meeting matching
        # ...") -- matches _UNKNOWN_RES[2], >350 chars, tool path.
        reply = ("I couldn't find a meeting matching \"Q3 planning\" that you "
                 "attended in the last 14 days. Here are the recent meetings you "
                 "did attend -- reply with a number and I'll pull its action "
                 "items: 1) F3 Weekly, 2) OSN Ops sync, 3) BDM leadership, "
                 "4) Founder review, 5) HJRG finance weekly. If none of these is "
                 "the one you meant, tell me the meeting name or date and I'll "
                 "look again.")
        assert len(reply) > gd._UNKNOWN_MAX_CHARS  # also the newly-exposed >350 case
        assert _detect(_isolated_state, response=reply,
                       gen_meta={"used_tools": True}) is None
        assert not _read_gaps(_isolated_state)

    def test_dossier_no_signals_relay_with_tools_not_logged(self, _isolated_state):
        # Mirrors person_dossier.py:686 ("I don't have any reachable
        # work-involvement signals for {name} ...") -- matches _UNKNOWN_RES[0]
        # via "any" and carries a person name; must NOT enter the gap log.
        reply = ("I don't have any reachable work-involvement signals for Jane "
                 "Contractor in the last 90 days across what I can see. Nothing "
                 "surfaced in email, calendar, or shared docs for that person.")
        assert _detect(_isolated_state,
                       question="what is jane contractor working on?",
                       response=reply, gen_meta={"used_tools": True}) is None
        assert not _read_gaps(_isolated_state)

    def test_locked_finance_phrase_still_logs_even_with_tools(self, _isolated_state):
        # The exception: the finance connector returning the exact locked
        # UNKNOWN phrase IS the data-gap signal, even though a tool ran.
        det = _detect(_isolated_state, response=gd.UNKNOWN_RESPONSE_TEXT,
                      gen_meta={"used_tools": True})
        assert det == "unknown_response"

    def test_kb_miss_reply_no_tools_still_logs(self, _isolated_state):
        # Guard against over-correction: a genuine KB-miss unknown reply with
        # NO tool involved must still log (the main smoke path).
        det = _detect(_isolated_state, response=_SMOKE_THAT_CONTEXT,
                      gen_meta={})
        assert det == "unknown_response"


# ── kb_miss calibration fields (best_distance + chunks_returned) ─────────────

def _kb_miss_meta_with_distance(best=1.057, returned=12):
    m = _kb_miss_meta()
    m["kb_best_distance"] = best
    m["kb_chunks_returned"] = returned
    return m


class TestCalibrationFields:
    def test_kb_miss_record_carries_distance_and_count(self, _isolated_state):
        det = _detect(_isolated_state, kb_meta=_kb_miss_meta_with_distance())
        assert det == "kb_miss"
        rec = _read_gaps(_isolated_state)[0]
        assert rec["best_distance"] == 1.057
        assert rec["chunks_returned"] == 12

    def test_unknown_response_record_carries_distance_when_search_ran(self, _isolated_state):
        # The smoke case: retrieval DID run (12 chunks < gate) yet the reply was
        # an unknown_response -- exactly the data that shows kb_miss's gate is
        # unreachable. best_distance must ride along.
        meta = {"kb_search_ran": True, "kb_relevant_hits": 12,
                "kb_best_distance": 1.074, "kb_chunks_returned": 12}
        det = _detect(_isolated_state, response=_SMOKE_THAT_CONTEXT, kb_meta=meta)
        assert det == "unknown_response"
        rec = _read_gaps(_isolated_state)[0]
        assert rec["best_distance"] == 1.074
        assert rec["chunks_returned"] == 12

    def test_record_omits_fields_when_absent(self, _isolated_state):
        # A tool-only / no-search path leaves the fields off the record entirely
        # (pre-existing records + non-KB records stay clean).
        det = _detect(_isolated_state, response=gd.UNKNOWN_RESPONSE_TEXT,
                      kb_meta={}, gen_meta={"used_tools": True})
        assert det == "unknown_response"
        rec = _read_gaps(_isolated_state)[0]
        assert "best_distance" not in rec
        assert "chunks_returned" not in rec


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

    def test_interior_bold_deflection_vetoed(self, _isolated_state):
        # The prompt contract sanctions interior *bold*; markers must not
        # split the veto patterns (adversarial review MEDIUM).
        for reply in (
            "That's *company financials* - take it to the finance channel.",
            "That's a *legal matter*. Reach Emily Stubbs.",
            "I'm *not able* to discuss that.",
        ):
            assert _detect(_isolated_state, response=reply,
                           kb_meta=_kb_miss_meta()) is None, reply

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

    def test_clinical_phi_question_never_logged(self, _isolated_state, monkeypatch):
        # Adversarial review HIGH: is_phi_risk alone misses bare clinical
        # terms; all THREE predicates must screen the question.
        monkeypatch.setattr(gd, "is_clinical_phi", lambda text: True)
        assert _detect(_isolated_state, kb_meta=_kb_miss_meta()) is None

    def test_admin_phi_question_never_logged(self, _isolated_state, monkeypatch):
        monkeypatch.setattr(gd, "is_lex_billing_status_phi", lambda text: True)
        assert _detect(_isolated_state, kb_meta=_kb_miss_meta()) is None

    def test_real_clinical_text_screened(self, _isolated_state):
        # End-to-end with the real detector: a clinical question in a NON-LEX
        # channel must not enter the gap log.
        det = _detect(_isolated_state, entity="HJRG",
                      question="our client was diagnosed with autism and takes risperidone",
                      kb_meta=_kb_miss_meta())
        assert det is None
        assert not _read_gaps(_isolated_state)

    def test_thread_context_skips_kb_miss(self, _isolated_state):
        # The answer source was the thread itself (prior_messages), invisible
        # to kb_relevant_hits (adversarial review MEDIUM).
        det = gd.maybe_log_gap(
            entity="F3E", channel="f3e-leadership", user="U1",
            question=QUESTION, response_text="answered from the thread above, in detail",
            latency_ms=10, kb_meta=_kb_miss_meta(), gen_meta={},
            is_dm=False, thread_key="", thread_context=True,
        )
        assert det is None

    def test_thread_context_does_not_block_unknown_response(self, _isolated_state):
        det = gd.maybe_log_gap(
            entity="F3E", channel="f3e-leadership", user="U1",
            question=QUESTION, response_text=gd.UNKNOWN_RESPONSE_TEXT,
            latency_ms=10, kb_meta={}, gen_meta={},
            is_dm=False, thread_key="", thread_context=True,
        )
        assert det == "unknown_response"

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

    def test_capped_question_can_log_tomorrow(self, _isolated_state, monkeypatch):
        # Adversarial review MEDIUM: a cap hit must NOT record the dedup key,
        # or the capped question is silently suppressed for the 7d window.
        monkeypatch.setenv("CORA_GAP_DETECT_DAILY_CAP", "1")
        assert _detect(_isolated_state, kb_meta=_kb_miss_meta()) == "kb_miss"
        capped_q = "a second distinct substantive question about vendors"
        assert _detect(_isolated_state, question=capped_q,
                       kb_meta=_kb_miss_meta()) is None  # capped
        # New day: the previously-capped question logs.
        path = _isolated_state / "gap_detection_state.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        state["day"] = "2000-01-01"
        path.write_text(json.dumps(state), encoding="utf-8")
        assert _detect(_isolated_state, question=capped_q,
                       kb_meta=_kb_miss_meta()) == "kb_miss"

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

    def test_company_finance_gap_never_escalates(self):
        # Adversarial review MEDIUM (R1b): the unknown_response detector now
        # reliably logs finance-tool misses from TIER_1 channels; escalation
        # must never quote a company-finance question to a domain owner
        # (D-064 canon decides).
        gap = {"ts": "2026-06-01T00:00:00+00:00", "entity": "F3E",
               "channel": "f3e-finance",
               "question": "what was our net cash burn last week?", "gap": "g"}
        assert ga.should_escalate(gap) is False

    def test_commercial_money_gap_still_escalates(self):
        # D-064 precision: deal-level money talk is NOT company finance.
        gap = {"ts": "2026-06-01T00:00:00+00:00", "entity": "F3E",
               "channel": "f3e-sales",
               "question": "did SJ Food Brokers pay invoice 8562 yet?", "gap": "g"}
        assert ga.should_escalate(gap) is True

    def test_finance_screen_error_fails_closed(self, monkeypatch):
        import cora.user_access as ua
        monkeypatch.setattr(ua, "_financials_is_blocked",
                            lambda t: (_ for _ in ()).throw(RuntimeError("boom")))
        gap = {"ts": "2026-06-01T00:00:00+00:00", "entity": "F3E",
               "channel": "f3e-leadership", "question": "q", "gap": "g"}
        assert ga.should_escalate(gap) is False

    def test_kb_miss_gaps_never_escalate(self):
        # kb_miss = mining-only telemetry (can fire on a correctly-answered
        # question); only unknown_response / llm_sentinel gaps reach owners.
        gap = {"ts": "2026-06-01T00:00:00+00:00", "entity": "F3E",
               "channel": "f3e-leadership", "question": "q", "gap": "g",
               "detector": "kb_miss"}
        assert ga.should_escalate(gap) is False
        gap["detector"] = "unknown_response"
        assert ga.should_escalate(gap) is True

    def test_clinical_phi_gap_never_escalates(self):
        # Adversarial review HIGH (second half): the escalation screen uses
        # the same 3-predicate union as the write gates.
        gap = {"ts": "2026-06-01T00:00:00+00:00", "entity": "HJRG",
               "channel": "hjrg-leadership",
               "question": "was Jalen diagnosed with autism and given risperidone?",
               "gap": "g"}
        assert ga.should_escalate(gap) is False


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
