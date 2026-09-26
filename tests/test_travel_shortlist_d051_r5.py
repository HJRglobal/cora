"""Code #16 C2 -- D-051 ROUND-5/6 regressions for the lane-thread price grammar.

r5:c2-trigger#0: r4:c2-trigger#4's POSTPOSED ceiling leg ("$250/night max") read an
occupancy opened by the ceiling word ("love the casita, $289/night, max 6 guests", "max 4
per room", "max capacity 8") as a price ceiling -- a comment on the posted card became a
billed re-search at the listing's price -- and made a restated ask naming an occupancy
after its price conflict with the list-item price read (the help line).

r6 (c2-egress#0-#2, c2-trigger#0-#1): the r5 lookahead that tried to tell a ceiling from a
count failed BOTH ways ("$250/night max 3 bedrooms" dropped the cap inside a billed
re-search; "max: 6 guests", "max. 6 guests", "max occ 4" still read as a ceiling). So the
postposed leg is REMOVED: a postposed ceiling gets the honest help line (it names "under
$250 a night"), nothing is billed, and the redesign is seeded. The copula form ("our max
is $250 a night") and every other r4 directive still re-run (tests/..._r4_refine.py).

Every row drives the REAL ``travel_shortlist.route_turn(lane_thread=True)`` with in-process
stubs only (``append_event`` raises, so nothing can write). No network.
"""

from __future__ import annotations

import time
from datetime import date

import pytest

from cora import travel_shortlist as ts
from cora import web_guard

TODAY = date(2026, 9, 25)
HARRISON = "U0B2RM2JYJ1"
ROOT = "1790000000.005500"
# the 9/15-shaped card: Mesa/Gilbert/Scottsdale, Oct 17-21, 4 people, $300-400
STORED_S2 = ("find hotels and airbnbs in mesa, gilbert or scottsdale oct 17-21 for 4 people "
             "$300-400/night")
STORED_S1 = "find hotels in scottsdale oct 17-21 for 4 people"
OCT20 = {"check_in": date(2026, 10, 20), "check_out": date(2026, 10, 22)}


@pytest.fixture
def route(monkeypatch):
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


# A price + an occupancy/count phrase = a DESCRIPTION of a posted option: never a billed
# re-search, never the listing's price as the new budget (r5 rows + the r6 residuals).
OCCUPANCY_MUST_HELP = [
    "love the casita, $289/night, max 6 guests",
    "second option $412/night, max 4 per room",
    "$289/night, max 6 guests, looks perfect",
    "valley ho: $329/night, max 2 per room, so we'd need 2 rooms",
    "casita $289/night. max 6 guests.",
    "$289/night\nmax 6 guests",
    "the w: $450 a night, max 2 guests per room. too small",
    "casita $289 a night max 4 people, thoughts?",
    "$289/night max six guests",
    "$300 a night, max capacity 8",
    "$310/night, max occupancy 4, pool",
    "$289/night, at most 4 guests",
    # r6 residuals of the lookahead
    "love it, $289/night, max: 6 guests",
    "$289/night, max (6 guests)",
    "$289/night, max. 6 guests",
    "$289/night, max number of guests 6",
    "$289/night, max stay 7 nights",
    "$289/night, max eleven guests",
    "$289/night, max occ 4",
    "$289/night, max party size 6",
]

# The postposed ceiling is not read: the help line, never a partial or a guessed ceiling.
POSTPOSED_MUST_HELP = [
    "$250/night max", "250 a night max", "$250/night maximum", "$250 a night tops",
    "$250/night or less", "$250/night at most", "$250/night max please", "$250/night, max",
    "$250/night max.", "$250/night max!",
    # a listed postposed price with another field: the list reader cannot read it -> help,
    # never a dates-only re-search that drops the stated cap
    "$250/night max, oct 20-22", "oct 20-22, $250/night max", "$250/night max, oct 20-22 please",
]

# A restated ask reads its own price N-N (as before round 4); an occupancy after it does
# not conflict with that read any more.
RESTATED_MUST_RERUN = [
    ("find hotels in mesa oct 20-22, $250/night, max 4 guests",
     {"areas": ("mesa",), "budget_min": 250, "budget_max": 250, "kind": "hotel", **OCT20}),
    ("hotels in mesa oct 20-22, $250/night, max 4 per room",
     {"areas": ("mesa",), "budget_min": 250, "budget_max": 250, "kind": "hotel", **OCT20}),
    ("hotels in mesa oct 17-21, $250/night max",
     {"areas": ("mesa",), "budget_min": 250, "budget_max": 250, "kind": "hotel"}),
]

# The copula ceiling directive and "budget is" still re-run with the stated value.
DIRECTIVE_STILL_RERUNS = [
    ("our max is $250 a night", {"budget_min": None, "budget_max": 250}),
    ("the max is $250 a night", {"budget_min": None, "budget_max": 250}),
    ("budget is $250/night max", {"budget_min": 250, "budget_max": 250}),
    ("keep it $250/night max, 6 people", {"budget_min": 250, "budget_max": 250, "party_size": 6}),
    ("under $250 a night", {"budget_min": None, "budget_max": 250}),
]


class TestAPostposedCeilingIsNotReadAsADirective:
    @pytest.mark.parametrize("text", OCCUPANCY_MUST_HELP)
    @pytest.mark.parametrize("stored", [STORED_S2, STORED_S1], ids=["s2", "s1"])
    def test_an_occupancy_after_a_price_is_a_comment_not_a_billed_re_search(self, route, text, stored):
        _assert_help(route(text, stored), text)

    @pytest.mark.parametrize("text", POSTPOSED_MUST_HELP)
    @pytest.mark.parametrize("stored", [STORED_S2, STORED_S1], ids=["s2", "s1"])
    def test_a_postposed_ceiling_gets_the_help_line(self, route, text, stored):
        _assert_help(route(text, stored), text)

    @pytest.mark.parametrize("text,expect", RESTATED_MUST_RERUN)
    def test_a_restated_ask_re_runs_on_its_own_price(self, route, text, expect):
        r = route(text)
        assert r is not None and r.kind == "search", (text, r)
        for k, v in expect.items():
            assert getattr(r.constraints, k) == v, (text, k, getattr(r.constraints, k), v)

    @pytest.mark.parametrize("text,expect", DIRECTIVE_STILL_RERUNS)
    def test_the_copula_and_verb_directives_still_re_run(self, route, text, expect):
        r = route(text)
        assert r is not None and r.kind == "search" and r.followup, (text, r)
        for k, v in expect.items():
            assert getattr(r.constraints, k) == v, (text, k, getattr(r.constraints, k), v)

    def test_the_parser_never_reads_an_occupancy_as_a_ceiling(self):
        base = ts.parse_constraints(STORED_S2, today=TODAY).constraints
        for text in ("love the casita, $289/night, max 6 guests", "$289/night, max: 6 guests"):
            merged, _changed, _mal = ts.merge_followup(base, text, today=TODAY)
            assert (merged.budget_min, merged.budget_max) == (300, 400), text

    def test_the_postposed_leg_is_gone(self):
        assert not hasattr(ts, "_BUDGET_CEIL_POST_RE")
        assert not hasattr(ts, "_BUDGET_CEIL_POST")

    @pytest.mark.parametrize("shape", [
        " " * 40_000,
        "$250/night max " * 2_600,
        "$250/night, max " + " " * 40_000 + "6 guests",
        "$250/night" + " max" * 10_000,
    ], ids=["ws40k", "ceil-run", "ceil-ws-count", "max-run"])
    def test_the_money_regexes_are_linear(self, shape):
        best = float("inf")
        for _ in range(3):
            t0 = time.perf_counter()
            ts._REFINE_MONEY_RE.search(shape)
            ts._BUDGET_CEIL_SUBJ_RE.search(shape)
            best = min(best, time.perf_counter() - t0)
        assert best < 0.1, best
