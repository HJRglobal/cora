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


# ── r4:c2-trigger#1: a listed field is read (or the help line), never silently dropped ──

P6, B250 = {"party_size": 6}, {"budget_min": 250, "budget_max": 250}
# A kept clause that is ONLY a field fragment is a LIST ITEM when another kept clause
# carries a field: it is read into the re-run.
LIST_MUST_RERUN = [
    ("oct 20-22, 6 people", {**OCT20, **P6}), ("6 people, oct 20-22", {**OCT20, **P6}),
    ("can we do oct 20-22, 6 people?", {**OCT20, **P6}),
    ("new dates: oct 20-22, 6 people", {**OCT20, **P6}),
    ("oct 20-22, $250/night", {**OCT20, **B250}),
    ("oct 20-22, 6 people, $250/night", {**OCT20, **P6, **B250}),
    ("oct 20-22 for 6 people, $250/night", {**OCT20, **P6, **B250}),
    ("mesa, oct 20-22", {**OCT20, "areas": ("mesa",)}),
    ("gilbert, oct 20-22 please", {**OCT20, "areas": ("gilbert",)}),
    ("mesa. oct 20-22", {**OCT20, "areas": ("mesa",)}),
    ("mesa or gilbert, oct 20-22", {**OCT20, "areas": ("mesa", "gilbert")}),
    # neighbours
    ("oct 20-22, and 6 people", {**OCT20, **P6}), ("oct 20-22, 6 guests please", {**OCT20, **P6}),
    ("oct 20-22, about 6 people", {**OCT20, **P6}), ("oct 20-22; 6 people", {**OCT20, **P6}),
    ("oct 20-22, 6 ppl", {**OCT20, **P6}), ("oct 20-22, twelve people", {**OCT20, "party_size": 12}),
    ("oct 20-22, 250 a night", {**OCT20, **B250}), ("move it to oct 20-22, 6 people", {**OCT20, **P6}),
    ("oct 20-22, 6 people total", {**OCT20, **P6}),
    ("oct 20-22, ~$250/night", {**OCT20, **B250}),
    ("6 people, mesa?", {**P6, "areas": ("mesa",)}),
    ("oct 20-22, 6 people, mesa", {**OCT20, **P6, "areas": ("mesa",)}),
    ("mesa, gilbert, oct 20-22", {**OCT20, "areas": ("mesa", "gilbert")}),
    ("old town scottsdale, oct 20-22", {**OCT20, "areas": ("scottsdale",)}),
    ("oct 20-22. jordan, 6 people", {**OCT20, **P6}),
    ("hotels oct 20-22, mesa", {**OCT20, "areas": ("mesa",), "kind": "hotel"}),
    ("hotels in mesa oct 17-21, 6 guests, $250/night",            # restated (r3 pinned)
     {"areas": ("mesa",), **P6, **B250, "kind": "hotel"}),
    ("try mesa and send this to 2 people", {"areas": ("mesa",)}),  # r3 MIXED (pinned)
]
# ...a listed field the lane cannot read gets the help line, never a partial re-search;
# a lone fragment (or one beside a verb that carries no field) keeps today's help line.
LIST_MUST_HELP = [
    "oct 20-22 and 6 people", "oct 20-22 & 6 people", "oct 20-22 plus 6 people",
    "can we do oct 20-22 and 6 people?", "oct 20-22 and $250/night",
    "oct 20-22, $250", "oct 20-22, under $250", "oct 20-22, 40 people", "oct 20-22, $5/night",
    "try mesa, $250", "mesa, oct 20-22, 250 bucks",
    "6 people", "$250/night", "thanks, gilbert", "search again, 6 people", "try again, gilbert",
    "6 people, $250/night", "mesa, $250/night",
]


class TestAListedFieldIsReadNeverDropped:
    @pytest.mark.parametrize("stored_ask", [STORED_S2, STORED_S3], ids=["s2", "s3"])
    @pytest.mark.parametrize("text,expect", LIST_MUST_RERUN)
    def test_a_list_item_is_read_into_the_re_run(self, route, text, expect, stored_ask):
        _assert_search(route(text, stored_ask), text, expect, stored_ask)

    @pytest.mark.parametrize("stored_ask", [STORED_S2, STORED_S3], ids=["s2", "s3"])
    @pytest.mark.parametrize("text", LIST_MUST_HELP)
    def test_an_unreadable_or_lone_fragment_gets_the_help_line(self, route, text, stored_ask):
        _assert_help(route(text, stored_ask), text)

    @pytest.mark.parametrize("shape", [
        " " * 40000, ", 6 people" * 4000, "oct 20-22, 6 people, " * 1900, "mesa, " * 6600,
        "oct 20-22 and 6 people " * 1700, ", $250/night" * 3300, "and 6 " * 6600,
        ", $2" * 10000, "mesa/" * 8000, " and $250/night" * 2600,
    ], ids=["spaces", "list-party", "oct-list", "mesa-list", "and-people", "dollar-list",
            "and-six", "bare-dollar", "mesa-slash", "and-dollar"])
    def test_the_list_item_reader_is_linear(self, shape):
        stored = ts.parse_constraints(STORED_S3, today=TODAY).constraints

        def regexes():
            for rx in (ts._PARTY_FRAG_RE, ts._PRICE_FRAG_RE, ts._MONEY_FRAG_RE):
                rx.match(shape)
            ts._LIST_JOINED_RE.search(shape)

        def predicates():
            ts.merge_followup(stored, shape, today=TODAY)
        assert _best_of_3(regexes) < 0.1
        assert _best_of_3(predicates) < 0.1


# ── r4:c2-trigger#4: the adjudicated directives in copula / contraction / postposed form ──

MAX250, MAX300 = {"budget_min": None, "budget_max": 250}, {"budget_min": None, "budget_max": 300}
DIRECTIVE_FORMS_MUST_RERUN = [
    ("our max is $250 a night", MAX250), ("the max is $250 a night", MAX250),
    ("budget's $250 a night", B250), ("our budget's 250 a night", B250),
    ("$250/night max", MAX250), ("250 a night max", MAX250),
    ("make that 6 people", P6), ("actually it's for 6 people", P6),
    ("oh it's for 6 people, not 4", P6),
    # neighbours
    ("$250/night maximum", MAX250), ("$250 a night tops", MAX250),
    ("$250/night or less", MAX250), ("250 a night at most", MAX250),
    ("the budget is under $300 a night", MAX300), ("the budget's under $300 a night", MAX300),
    ("our limit is $250 a night", MAX250), ("the cap is $250/night", MAX250),
    ("max would be $250 a night", MAX250), ("the max's $250 a night", MAX250),
    ("our budget was $250 a night", B250), ("budget is $250/night max", MAX250),
    ("make that for 6 people", P6), ("it's for 6 people", P6), ("it is for 6 people", P6),
    # the r3 contrast rows still re-run
    ("our budget is $250 a night", B250), ("top budget is $250/night", B250),
    ("it's actually 6 people", P6), ("change it to 6 people", P6),
]
# A card-reference / description form stays a comment.
DESCRIPTION_FORMS_MUST_HELP = [
    "the second one is $250 a night", "the first one is for 6 people",
    "the 2nd one's $250/night max", "the first one is $250/night max",
    "that one is $250 a night or less", "it's $250 a night max", "the price is $250 a night",
    "the rate is $250 a night", "the max occupancy is 6 people",
    "the hotel in mesa is $250/night max", "Hotel Valley Ho is $250/night max",
    "the second one is under $300 a night", "the airbnb is for 6 people",
    "that one's for 6 people", "the hotel's max is $250 a night", "our max is $250",
    "max is 6 people", "make that oct 20-22",
]


class TestTheDirectiveFormsReRun:
    @pytest.mark.parametrize("stored_ask", [STORED_S2, STORED_S3], ids=["s2", "s3"])
    @pytest.mark.parametrize("text,expect", DIRECTIVE_FORMS_MUST_RERUN)
    def test_a_directive_in_copula_or_postposed_form_re_runs(self, route, text, expect,
                                                              stored_ask):
        _assert_search(route(text, stored_ask), text, expect, stored_ask)

    @pytest.mark.parametrize("stored_ask", [STORED_S2, STORED_S3], ids=["s2", "s3"])
    @pytest.mark.parametrize("text", DESCRIPTION_FORMS_MUST_HELP)
    def test_a_description_of_a_posted_option_stays_a_comment(self, route, text, stored_ask):
        _assert_help(route(text, stored_ask), text)

    def test_the_fresh_parser_is_unchanged(self):
        f = ts.parse_fields("hotels in mesa oct 17-21 $250/night max", today=TODAY)
        assert (f["budget_min"], f["budget_max"]) == (250, 250)
        f = ts.parse_fields("hotels in mesa oct 17-21, the max is $250 a night", today=TODAY)
        assert (f["budget_min"], f["budget_max"]) == (250, 250)

    @pytest.mark.parametrize("shape", [
        " " * 40000, "max is $" * 5000, "budget's $2" * 3600, "it's for " * 4400,
        "$250/night max " * 2600, "make that " * 4000, "the max is " * 3600,
        "budget " * 5700, "max's " * 6600, "$250/night " * 3600,
    ], ids=["spaces", "max-is", "budget-s", "its-for", "night-max", "make-that", "the-max-is",
            "budget", "max-s", "per-night"])
    def test_the_directive_forms_are_linear(self, shape):
        stored = ts.parse_constraints(STORED_S3, today=TODAY).constraints

        def regexes():
            for rx in (ts._BUDGET_CEIL_SUBJ_RE, ts._BUDGET_CEIL_POST_RE, ts._BUDGET_DIRECTIVE_RE,
                       ts._REFINE_MONEY_RE, ts._PARTY_DIRECTIVE_RE, ts._MONEY_MENTION_RE):
                rx.search(shape)
            for i in range(0, 40000, 997):
                ts._COPULA_BEFORE_RE.search(shape[max(0, i - 24):i])

        def predicates():
            ts._is_refinement(shape)
            ts.merge_followup(stored, shape, today=TODAY)
        assert _best_of_3(regexes) < 0.1
        assert _best_of_3(predicates) < 0.1
