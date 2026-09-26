"""Code #16 C2 -- D-051 ROUND-4 regressions for the travel lane's WITHHOLD side (fixer R4B).

  * r4:c2-egress#0 + r4:integration#0 -- B1's prior-turn PERSON leg is the UNION of the
    joined same-author runs (a noun and its cue split across two messages) AND each
    pre-merge person message (the first-clause frame leg and the capability bail scoped
    to their own message). Cora's side is unchanged (STRONG tier over her runs).
  * r4:c2-egress#1 -- _PERSON_STAY_RE's person slot takes a coordinated group
    ("where mike jones and sarah lee could stay"); still a person-turn-only pattern.
  * r4:c2-trigger#2 (SPLIT -> fix, narrow) -- an IDLE user's DM that plainly asks for
    lodging (the frame governs a strict lodging noun) escapes the OSN scheduler's bare
    "availability" keyword and takes the ordinary path (web withheld by B1); lane
    ownership stays on the strict predicate, mid-flow users stay with the scheduler.

Every test drives the REAL code path: the pure predicates on Slack text, the real
history builders (_fetch_thread_history / _fetch_dm_history) into the real
_dispatch_qa, or the real handle_message_event (im) entry point with only
infrastructure stubbed (the wiring module's ``lane`` fixture). Guest names are
SYNTHETIC. Every table carries its precision rows next to its recall rows, and every
changed regex is timed on the whitespace-only 40k input and adversarial 40k inputs.
"""

from __future__ import annotations

import time

import pytest

import cora.app as app_module
from cora import travel_shortlist as ts
from cora import web_guard
from cora.model_router import MODEL_SONNET
from test_travel_shortlist import _slack_client
from test_travel_shortlist_d051_r2 import (  # noqa: F401 -- `real_history` is a fixture
    _cora, _dm_client, _person, _web_tools_through_the_dm, real_history,
)
from test_travel_shortlist_d051_r3 import (
    LATER_DM, LATER_THREAD, PERSON_STAY_MUST_NOT_WITHHOLD, PERSON_STAY_MUST_WITHHOLD,
    _thread_web_tools,
)
from test_travel_shortlist_wiring import (  # noqa: F401 -- `lane` is a fixture
    _dm, _drain, _drive_dispatch, _model_path, _tessa, _web_rows, lane,
)

HARRISON = "U0B2RM2JYJ1"
MEMBER = "U0B3RU5Q55G"


def _wire(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _best_of_3(fn) -> float:
    best = float("inf")
    for _ in range(3):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best


# ── r4:c2-egress#0 + r4:integration#0: the prior-turn person leg is a UNION ────────

LATER_SEDONA = "google the best restaurants in sedona"
ROOT = "1790000000.000200"

# (earlier person message(s), the frame-only inn ask) -- the ask never opens its run,
# so the run's first clause is the earlier message and the frame leg read only that.
HUMAN_ROOTED_THREAD_ROWS = [
    ("UFL shoot logistics thread", "can you find some inns for Mike Jones and Sarah Lee?"),
    ("UFL shoot logistics thread", "look for an inn for Jordan Riverstone and his wife"),
    ("Jordan and Sarah land Friday.", "can you find an inn for Mike Jones and Sarah Lee"),
    ("quick one", "find an inn for Mike Jones and Sarah Lee"),
    # the r3 c2-egress#2 row (E2_MUST_WITHHOLD) in a non-leading run position
    ("thanks", "search for inns online for Mike Jones and Sarah Lee"),
]
# (the inn ask, a later same-author message naming a capability word) -- the ask opens
# its run, but the capability bail read the WHOLE run.
CAPABILITY_TAIL_ROWS = [
    ("find some inns for Mike Jones and Sarah Lee", "I'll email them the list"),
    ("look for an inn for Jordan Riverstone and his wife", "and put it on the calendar"),
]


class TestPriorTurnPersonLegIsAUnion:
    @pytest.mark.parametrize("first,ask", HUMAN_ROOTED_THREAD_ROWS)
    def test_a_frame_only_ask_after_an_earlier_person_message_withholds(
            self, lane, real_history, first, ask):
        """A human-rooted channel thread: the root and the reply merge into one user
        turn; the inn ask is lodging-shaped ALONE but not as the joined run."""
        assert web_guard.evaluate(LATER_THREAD, "F3E", kb_meta={}, model=MODEL_SONNET).attach
        assert not ts.is_lodging_shaped(LATER_THREAD)
        assert ts.is_lodging_shaped(ask) and not ts.is_lodging_shaped(first + "\n" + ask)
        client = _dm_client([
            _person("1790000000.000400", _tessa(), ask),
            _person(ROOT, "U0ALEX", first),
        ])
        assert _thread_web_tools(client, LATER_THREAD, user=_tessa(), root=ROOT) == [False]
        assert any(r.get("reason") == "gate_skipped:travel_lane" for r in _web_rows())

    @pytest.mark.parametrize("ask,tail", CAPABILITY_TAIL_ROWS)
    def test_a_capability_word_later_in_the_run_does_not_bail_the_ask(
            self, lane, real_history, ask, tail):
        """A Cora-opened thread: the inn ask OPENS the person run, but the run-wide
        capability bail read the tail's 'email' / 'calendar'."""
        assert ts.is_lodging_shaped(ask) and not ts.is_lodging_shaped(ask + "\n" + tail)
        client = _dm_client([
            _person("1790000000.000500", _tessa(), tail),
            _person("1790000000.000400", _tessa(), ask),
            _cora(ROOT, "Opening a thread for the UFL shoot logistics."),
        ])
        assert _thread_web_tools(client, LATER_THREAD, user=_tessa(), root=ROOT) == [False]

    @pytest.mark.parametrize("first,ask", HUMAN_ROOTED_THREAD_ROWS[2:])
    def test_two_quick_dms_before_a_web_ask_withhold(self, lane, real_history, first, ask):
        """integration#0 through the real _fetch_dm_history: two consecutive person DMs,
        then an unrelated explicit web ask."""
        assert web_guard.evaluate(LATER_SEDONA, "HJRG", kb_meta={}, model=MODEL_SONNET).attach
        client = _dm_client([
            _person("1790000000.000500", _tessa(), ask),
            _person("1790000000.000400", _tessa(), first),
        ])
        hist = app_module._fetch_dm_history(client, "D0TESSA", "1790000000.000990")
        assert hist[0]["content"] == first + "\n" + ask            # ONE merged user turn
        assert _web_tools_through_the_dm(client, LATER_SEDONA, user=_tessa()) == [False]

    def test_the_split_cue_run_still_withholds(self, lane, real_history):
        """The r3 class the runs closed stays closed: neither message alone is lodging."""
        first, second = "we need two suites", "oct 17-21 for Mike Jones and Sarah Lee"
        assert not ts.is_lodging_shaped(first) and not ts.is_lodging_shaped(second)
        client = _dm_client([_person("1790000000.000500", _tessa(), second),
                             _person("1790000000.000400", _tessa(), first)])
        assert _web_tools_through_the_dm(client, LATER_DM, user=_tessa()) == [False]

    @pytest.mark.parametrize("msgs", [
        ["and the Deposco sandbox too", "what changed in the API last week?"],
        ["The Inn at the Biltmore account ordered 12 cases", "find the inn receipt from last month"],
        ["renew the Adobe suite", "what's our last resort"],
        ["where the data should stay", "find where the importer might crash"],
    ], ids=["plain", "inn-account", "idioms", "software-stay"])
    def test_precision_a_run_of_non_lodging_messages_attaches(self, lane, real_history, msgs):
        assert not any(ts.is_lodging_shaped(m) for m in msgs)
        assert not ts.is_lodging_shaped("\n".join(msgs))
        client = _dm_client([_person(f"1790000000.00{500 - i}", _tessa(), m)
                             for i, m in enumerate(reversed(msgs))])
        assert _web_tools_through_the_dm(client, "google the Deposco API changelog",
                                         user=_tessa()) == [True]

    def test_precision_coras_side_is_unchanged(self, lane, real_history):
        """Cora's run reads the STRONG tier only -- her own two posts, one of which a
        PERSON's loose leg would withhold (a task title she echoes: 'find an inn for
        Jordan Riverstone'), never do."""
        c1, c2 = "Here is the recap. Open task:", "find an inn for Jordan Riverstone"
        assert ts.is_lodging_shaped(c2) and not ts.is_lodging_strong(c2)
        client = _dm_client([_person("1790000000.000600", _tessa(), "nice, thanks"),
                             _cora("1790000000.000500", c2), _cora("1790000000.000400", c1),
                             _person("1790000000.000300", _tessa(), "what did we decide?")])
        assert _web_tools_through_the_dm(client, "google the Deposco API changelog",
                                         user=_tessa()) == [True]

    def test_the_union_is_the_runs_then_each_member_of_a_multi_message_run(self, real_history):
        client = _dm_client([
            _cora("1790000000.000700", "c2"), _cora("1790000000.000600", "c1"),
            _person("1790000000.000500", "U1", "p3"), _person("1790000000.000400", "U1", "p2"),
            _cora("1790000000.000300", "c0"), _person("1790000000.000200", "U1", "p1"),
        ])
        hist = app_module._fetch_dm_history(client, "D0X", "1790000000.000990")
        assert app_module._prior_turns_by_author(hist, with_members=True) == (
            ["p1", "p2\np3", "p2", "p3"], ["c0", "c1\nc2"])
        # the default (the r3 pins) is the runs alone
        assert app_module._prior_turns_by_author(hist) == (["p1", "p2\np3"], ["c0", "c1\nc2"])

    def test_a_list_without_the_record_is_unchanged(self):
        plain = [{"role": "user", "content": "a\nb"}, {"role": "assistant", "content": "c"}]
        assert (app_module._prior_turns_by_author(plain, with_members=True)
                == app_module._prior_turns_by_author(plain) == (["a\nb"], ["c"]))


# ── r4:c2-egress#1: the person slot takes a coordinated group ──────────────────────

PERSON_GROUP_MUST_WITHHOLD = [
    # the finding's four rows
    "google where mike jones and sarah lee could stay",
    "google where jordan riverstone and his wife can stay",
    "search online for a place for Mike Jones and Sarah Lee to stay",
    "google somewhere Mike and Sarah Jones can stay",
    # the other conjunctions, and a group on each side
    "google where mike jones & sarah lee could stay",
    "search online for a spot for mike jones + sarah lee to crash",
    "look up somewhere jordan riverstone and his manager might sleep",
    "google anywhere mike jones and sarah lee would stay near the arena",
    "search the web for places for mike jones and the lee family to stay",
]
PERSON_GROUP_MUST_NOT_WITHHOLD = [
    # determiner / pronoun subjects stay out, with or without a group
    "where the data and the logs should stay", "where the bot and the importer could crash",
    "that's where the api and the queue could crash", "where it and the cache might crash",
    "google where mike jones and sarah lee work", "google where the stadium and the arena are",
    "a place for everything and everyone", "somewhere mike and sarah went last year",
]


class TestPersonStayCoordinatedGroup:
    @pytest.mark.parametrize("text", PERSON_GROUP_MUST_WITHHOLD)
    def test_withholds_as_a_persons_turn_only(self, text):
        assert ts.is_lodging_shaped(text), text
        assert ts.is_lodging_shaped(_wire(text)), text
        # kept OUT of the STRONG tier that also reads Cora's own prose
        assert not ts.is_lodging_strong(text) or "sleep" in text, text

    @pytest.mark.parametrize("text", PERSON_GROUP_MUST_NOT_WITHHOLD
                             + PERSON_STAY_MUST_NOT_WITHHOLD)
    def test_precision_rows_stay_clear(self, text):
        assert not ts.is_lodging_shaped(text), text

    @pytest.mark.parametrize("text", PERSON_STAY_MUST_WITHHOLD)
    def test_the_r3_rows_still_withhold(self, text):
        assert ts.is_lodging_shaped(text), text

    @pytest.mark.parametrize("text", PERSON_GROUP_MUST_WITHHOLD[:4])
    def test_the_web_ask_is_withheld_on_the_real_dispatch(self, lane, text):
        # web_guard WOULD attach (explicit intent) -- only the travel withhold stops it
        assert web_guard.evaluate(text, "HJRG", kb_meta={}, model=MODEL_SONNET).attach
        seen = _drive_dispatch(text, user=_tessa(), channel_id="D0TESSA", channel_name="dm",
                               entity="HJRG")
        assert seen and all(kw.get("web_tools") is False for kw in seen)
        assert any(r.get("reason") == "gate_skipped:travel_lane" for r in _web_rows())

    def test_coras_prose_with_a_group_does_not_withhold(self, lane):
        prior = [{"role": "user", "content": "what did the test run say?"},
                 {"role": "assistant", "content": "That's where builds and imports could "
                                                  "crash; somewhere jobs and retries can stay."}]
        seen = _drive_dispatch("google the Deposco API changelog", user=_tessa(),
                               channel_id="D0TESSA", channel_name="dm", entity="HJRG",
                               prior=prior)
        assert seen and seen[-1].get("web_tools") is True

    @pytest.mark.parametrize("shape", [
        " " * 40000, "where mike and " * 2700, "where a and b " * 2900, "place for x and " * 2500,
        "somewhere " + "mike & " * 5700, "where " + "x + " * 10000, "and " * 10000,
        "where mike jones and sarah lee " * 1300, "spots for " + "ab and cd " * 4000,
    ], ids=["spaces", "where-and", "where-a-and-b", "place-for-and", "amp", "plus", "and",
            "group", "spots-group"])
    def test_the_phrase_is_linear(self, shape):
        assert len(shape) >= 39000
        assert _best_of_3(lambda: ts._PERSON_STAY_RE.search(shape)) < 0.1
        assert _best_of_3(lambda: ts.is_lodging_shaped(shape)) < 0.25


# ── r4:c2-trigger#2: an idle user's plainly-lodging DM escapes the scheduler keyword ──

ESCAPE_ROWS = [
    # the finding's seven rows: lodging asks the r3 head rule declines, with 'availability'
    "find a hotel and a rental car with availability in scottsdale oct 17-21",
    "need a hotel and rental car, check availability oct 17-21 in scottsdale",
    "find hotel room blocks with availability in scottsdale oct 17-21",
    "can you find a hotel room block with availability in scottsdale oct 17-21 for 12 people",
    "find hotels and flights with availability for oct 17-21 in scottsdale",
    "find hotels for the partnership meeting with availability in scottsdale oct 17-21",
    "find hotels for the ufl sponsorship summit with availability in vegas oct 17-18",
]
SCHEDULER_ROWS = [
    "submit availability", "i want to submit availability", "my availability this week",
    "can i update my availability", "what is my schedule", "my shifts", "when do i work",
    "check availability for oct 20-22 instead",      # outside a lane thread (r1 pin)
    # lodging-shaped but NOT a framed ask: the escape is narrow (verdict-1's pre-Code-16
    # class; the scheduler keeps them exactly as before)
    "check hotel availability in scottsdale oct 17-21",
    "does the hyatt in scottsdale have availability oct 17-21?",
]


def _shift_calls(monkeypatch) -> list:
    handled: list = []
    monkeypatch.setattr(app_module.osn_shift_handler, "handle_dm",
                        lambda **k: handled.append(k))
    return handled


def _dm_ordinary(text, *, user, ts_="1790000000.000100"):
    seen: list = []
    client = _slack_client()
    client.chat_postMessage.side_effect = None
    client.chat_postMessage.return_value = {"ok": True}
    with _model_path(seen):
        _dm(client, text, user=user, ts_=ts_)
    return seen, client


class TestIdleLodgingDmEscapesTheSchedulerKeyword:
    @pytest.mark.parametrize("text", ESCAPE_ROWS)
    @pytest.mark.parametrize("who", ["harrison", "tessa"])
    def test_an_idle_users_lodging_dm_takes_the_ordinary_path(self, lane, monkeypatch,
                                                             text, who):
        user = HARRISON if who == "harrison" else _tessa()
        assert app_module._dm_is_shift_message(user, text)       # the keyword WOULD claim it
        assert not ts.looks_like_travel_ask(text, user_id=user, channel_id="D0TRAVELDM",
                                            channel_type="im")   # the lane declines it
        handled = _shift_calls(monkeypatch)
        fired: list = []
        monkeypatch.setattr(ts, "execute_route", lambda *a, **k: fired.append(1))
        seen, _client = _dm_ordinary(text, user=user)
        assert handled == []                                     # never the scheduler
        assert fired == [] and lane.calls == []                  # nor the lane (nothing billed)
        assert seen and all(kw.get("web_tools") is False for kw in seen)   # B1 withholds

    @pytest.mark.parametrize("text", ESCAPE_ROWS[:3])
    def test_a_mid_flow_user_stays_with_the_scheduler(self, lane, monkeypatch, text):
        handled = _shift_calls(monkeypatch)
        monkeypatch.setattr(app_module.osn_shift_handler, "get_dm_state",
                            lambda uid: {"step": "collecting_days"})
        _dm(_slack_client(), text, user=HARRISON)
        assert len(handled) == 1

    @pytest.mark.parametrize("text", SCHEDULER_ROWS)
    def test_scheduler_phrases_still_reach_the_scheduler(self, lane, monkeypatch, text):
        handled = _shift_calls(monkeypatch)
        _dm(_slack_client(), text, user=HARRISON)
        assert len(handled) == 1, text

    def test_a_mid_flow_reply_still_reaches_the_scheduler(self, lane, monkeypatch):
        handled = _shift_calls(monkeypatch)
        monkeypatch.setattr(app_module.osn_shift_handler, "get_dm_state",
                            lambda uid: {"step": "collecting_days"})
        _dm(_slack_client(), "i'm available monday", user=HARRISON)
        assert len(handled) == 1

    def test_the_strict_ask_still_owns_the_lane(self, lane, monkeypatch):
        """Lane ownership is unchanged: the strict ask with 'availability' runs the lane."""
        handled = _shift_calls(monkeypatch)
        client = _slack_client()
        _dm(client, "find hotels with availability in scottsdale oct 17-21", user=HARRISON)
        _drain()
        assert handled == [] and len(lane.calls) == 1

    def test_an_unreadable_predicate_leaves_the_scheduler_its_turn(self, lane, monkeypatch):
        handled = _shift_calls(monkeypatch)

        def boom(_t):
            raise RuntimeError("predicate failed")

        monkeypatch.setattr(ts, "_frame_governs_lodging_noun", boom)
        _dm(_slack_client(), ESCAPE_ROWS[2], user=HARRISON)
        assert len(handled) == 1
