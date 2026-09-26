"""Code #16 C2 -- D-051 ROUND-4 regressions for the lane-thread follow-up grammar
(fixer R4C: r4:c2-trigger#0, #1, #4).

Every row drives the REAL ``travel_shortlist.route_turn(lane_thread=True)``: the stored
constraints come from ``parse_constraints(...)`` of a real ask and are handed to the
router through an in-process stub of ``latest_constraints``; the web gate, the lane
switch, eval mode and the search budget are stubbed in-process; ``append_event`` raises,
so nothing can write. No network. Every table carries its precision rows next to its
recall rows, and every new regex is timed on the whitespace-only 40k input and an
adversarial 40k input.
"""

from __future__ import annotations

import time
from datetime import date

import pytest

from cora import travel_shortlist as ts
from cora import web_guard

TODAY = date(2026, 9, 25)
HARRISON = "U0B2RM2JYJ1"
ROOT = "1790000000.004400"
# the 9/15-shaped card: Mesa/Gilbert/Scottsdale, Oct 17-21, 4 people, $300-400
STORED_S2 = ("find hotels and airbnbs in mesa, gilbert or scottsdale oct 17-21 for 4 people "
             "$300-400/night")
# the r3 stored ask: Scottsdale only
STORED_S3 = "find hotels and airbnbs in scottsdale oct 17-21 for 4 people $300-400/night"
OCT20 = {"check_in": date(2026, 10, 20), "check_out": date(2026, 10, 22)}


def _best_of_3(fn) -> float:
    best = float("inf")
    for _ in range(3):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best


@pytest.fixture
def route(monkeypatch):
    """route(text, stored_ask) -> the real lane-thread Route, stubs in-process only."""
    stored = {"c": None}
    monkeypatch.setattr(ts, "latest_constraints", lambda ch, root, **k: stored["c"])
    monkeypatch.setattr(ts, "lane_enabled", lambda: True)
    monkeypatch.setattr(ts, "eval_mode", lambda: False)
    monkeypatch.setattr(ts, "search_budget", lambda: 4)
    monkeypatch.setattr(web_guard, "web_tools_enabled", lambda: True)
    monkeypatch.setattr(web_guard, "web_model_supported", lambda _m: True)

    def _no_write(*a, **k):
        raise AssertionError("a lane-thread route must not write")
    monkeypatch.setattr(ts, "append_event", _no_write)

    def _route(text, stored_ask=STORED_S2):
        c = ts.parse_constraints(stored_ask, today=TODAY).constraints
        assert c is not None
        stored["c"] = c
        return ts.route_turn(text, user_id=HARRISON, channel_id="D0HARRISON", channel_name="dm",
                             thread_root_ts=ROOT, lane_thread=True, today=TODAY)
    return _route


def _assert_help(r, text):
    assert r is not None and r.kind == "reply" and r.reply == ts.FOLLOWUP_HELP_REPLY, (text, r)


def _assert_search(r, text, expect, stored_ask):
    assert r is not None and r.kind == "search" and r.followup, (text, r)
    base = ts.parse_constraints(stored_ask, today=TODAY).constraints
    for k in ("check_in", "check_out", "areas", "party_size", "budget_min", "budget_max",
              "beds", "bedrooms", "kind", "styles"):
        want = expect.get(k, getattr(base, k))
        assert getattr(r.constraints, k) == want, (text, k, getattr(r.constraints, k), want)


# ── r4:c2-trigger#0: a refinement verb whose object is a posted option is a comment ──

# A pick of / a remark on a posted option ("the one in gilbert") is never a billed
# re-search narrowed to that option's area -- the finding's rows, the adjudicated
# pre-existing ones ("let's try ..." / "switch it to ..."), and their neighbours.
PICK_MUST_HELP = [
    "let's go with the one in gilbert", "i'd go with the one in gilbert",
    "we should go with the one in tempe", "let's do the one in gilbert",
    "look at the one in gilbert, it has a pool", "look at the second one in scottsdale",
    "change it to the one in gilbert", "let's look at the one in gilbert",
    "let's try the one in gilbert", "switch it to the one in gilbert",
    "what about the one in gilbert?", "how about that one in mesa?",
    "go with the airbnb in gilbert", "let's go with the airbnb in gilbert",
    "try the second hotel in mesa", "could you look at the first one in scottsdale",
    "can we do the one in tempe?", "let's go with the $250 one in mesa",
    "let's go with the cheap one in mesa", "go with the other one in tempe",
    "let's switch it to the one in gilbert", "let's make it the one in gilbert",
    "move it to the one in mesa", "i think we go with the first option in gilbert",
    "look at the listing in mesa", "we'll go with this one in gilbert", "try that place in mesa",
    "let's do the airbnb in scottsdale for oct 20-22",
    "something cheaper than the one in gilbert", "let's go with those hotels in mesa",
    "go with the ones in gilbert",
    # a question about options whose tail is not a field
    "is anything in gilbert pet friendly?", "is anything in mesa dog friendly?",
    "anything in gilbert that allows pets?", "is there anything in tempe with free parking",
]
# A refinement verb whose object IS a field still re-runs on the merged fields.
FIELD_OBJECT_MUST_RERUN = [
    ("let's look at gilbert", {"areas": ("gilbert",)}),
    ("look at mesa too", {"areas": ("mesa",)}),
    ("go with oct 20-22", OCT20), ("let's do oct 20-22", OCT20),
    ("let's go with oct 20-22", OCT20), ("change it to oct 20-22", OCT20),
    ("switch it to oct 20-22", OCT20), ("go with the dates oct 20-22", OCT20),
    ("let's try mesa", {"areas": ("mesa",)}), ("try one in mesa", {"areas": ("mesa",)}),
    ("anything in mesa?", {"areas": ("mesa",)}), ("any in gilbert?", {"areas": ("gilbert",)}),
    ("anything in mesa too?", {"areas": ("mesa",)}),
    ("anything in mesa or gilbert?", {"areas": ("mesa", "gilbert")}),
    ("anything in mesa please", {"areas": ("mesa",)}),
    ("anything in mesa for oct 20-22?", {**OCT20, "areas": ("mesa",)}),
    ("anything in mesa under $300 a night?",
     {"areas": ("mesa",), "budget_min": None, "budget_max": 300}),
    ("anything in mesa with a pool?", {"areas": ("mesa",), "styles": ("pool",)}),
    ("anything in mesa for 6 people?", {"areas": ("mesa",), "party_size": 6}),
    ("let's do the same thing in mesa", {"areas": ("mesa",)}),
    ("let's try the gilbert area", {"areas": ("gilbert",)}),
    ("try the hotels in mesa", {"areas": ("mesa",), "kind": "hotel"}),
    ("what about the airbnbs?", {"kind": "rental"}),
    ("let's do a king", {"beds": "king"}),
    ("let's go with the one in gilbert, but for 6 people", {"party_size": 6}),
]


class TestAVerbOnAPostedOptionIsAComment:
    @pytest.mark.parametrize("stored_ask", [STORED_S2, STORED_S3], ids=["s2", "s3"])
    @pytest.mark.parametrize("text", PICK_MUST_HELP)
    def test_a_pick_or_a_remark_on_a_posted_option_gets_the_help_line(self, route, text,
                                                                      stored_ask):
        _assert_help(route(text, stored_ask), text)

    @pytest.mark.parametrize("stored_ask", [STORED_S2, STORED_S3], ids=["s2", "s3"])
    @pytest.mark.parametrize("text,expect", FIELD_OBJECT_MUST_RERUN)
    def test_a_verb_on_a_field_still_re_runs(self, route, text, expect, stored_ask):
        _assert_search(route(text, stored_ask), text, expect, stored_ask)

    @pytest.mark.parametrize("text", ["let's go with the one in gilbert",
                                      "is anything in gilbert pet friendly?",
                                      "switch it to the one in gilbert"])
    def test_merge_never_reads_the_options_area(self, text):
        stored = ts.parse_constraints(STORED_S2, today=TODAY).constraints
        merged, changed, malformed = ts.merge_followup(stored, text, today=TODAY)
        assert not changed and not malformed and merged == stored, (text, merged)

    @pytest.mark.parametrize("shape", [
        " " * 40000, "go with the one " * 2500, "the " * 10000, "let's go with the " * 2200,
        "anything in mesa " * 2300, "anything in " * 3600, "try the $1 one " * 2600,
        "switch it to the " * 2300, "any in mesa/" * 3300, "the one in gilbert " * 2100,
    ], ids=["spaces", "go-with-the-one", "the", "lets-go-with-the", "anything-in-mesa",
            "anything-in", "try-the-dollar-one", "switch-it-to", "any-in-slash", "the-one-in"])
    def test_the_object_check_is_linear(self, shape):
        capped = ts._clean(shape).lower()            # what every clause predicate sees

        def regexes():
            for m in ts._REFINE_RE.finditer(shape):
                ts._CARD_OBJ_RE.match(shape, m.end())
            ts._CARD_OBJ_RE.match(" " + shape, 0)
            ts._verb_takes_card(shape)

        def predicates():
            ts._asks_about_options(capped)
            ts._is_refinement(shape)
        assert _best_of_3(regexes) < 0.1
        assert _best_of_3(predicates) < 0.1
