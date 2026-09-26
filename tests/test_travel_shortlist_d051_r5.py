"""Code #16 C2 -- D-051 ROUND-5 regression (r5:c2-trigger#0): the postposed ceiling word
("$250/night max", r4:c2-trigger#4) must END the price phrase. "max" that OPENS an
occupancy / count phrase ("$289/night, max 6 guests", "max 4 per room", "max capacity 8")
describes a posted option -- a comment on the card is never a billed re-search -- and a
restated ask that names an occupancy after its price keeps its N-N price read.

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


def _assert_search(r, text, expect, stored_ask):
    assert r is not None and r.kind == "search" and r.followup, (text, r)
    base = ts.parse_constraints(stored_ask, today=TODAY).constraints
    for k in ("check_in", "check_out", "areas", "party_size", "budget_min", "budget_max",
              "beds", "bedrooms", "kind", "styles"):
        want = expect.get(k, getattr(base, k))
        assert getattr(r.constraints, k) == want, (text, k, getattr(r.constraints, k), want)


# A price + an occupancy/count phrase opened by the ceiling word = a DESCRIPTION of a
# posted option (the finding's rows + neighbours): never a billed re-search.
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
]

# The adjudicated postposed ceiling directives still re-run with the ceiling (r4 rows).
POSTPOSED_MUST_RERUN = [
    ("$250/night max", {"budget_min": None, "budget_max": 250}),
    ("250 a night max", {"budget_min": None, "budget_max": 250}),
    ("$250/night maximum", {"budget_min": None, "budget_max": 250}),
    ("$250 a night tops", {"budget_min": None, "budget_max": 250}),
    ("$250/night or less", {"budget_min": None, "budget_max": 250}),
    ("$250/night at most", {"budget_min": None, "budget_max": 250}),
    ("budget is $250/night max", {"budget_min": None, "budget_max": 250}),
    ("$250/night max please", {"budget_min": None, "budget_max": 250}),
    ("$250/night, max", {"budget_min": None, "budget_max": 250}),
    ("$250/night max.", {"budget_min": None, "budget_max": 250}),
    ("keep it $250/night max, 6 people", {"budget_min": None, "budget_max": 250, "party_size": 6}),
    ("$250/night max for 6 people", {"budget_min": None, "budget_max": 250, "party_size": 6}),
    ("oct 20-22, $250/night max", {"budget_min": None, "budget_max": 250, **OCT20}),
]

# A restated ask that names an occupancy after its price keeps the N-N price read it had
# before round 4 (the ceiling leg no longer conflicts with the list-item price read).
RESTATED_WITH_OCCUPANCY = [
    ("find hotels in mesa oct 20-22, $250/night, max 4 guests",
     {"areas": ("mesa",), "budget_min": 250, "budget_max": 250, "kind": "hotel", **OCT20}),
    ("hotels in mesa oct 20-22, $250/night, max 4 per room",
     {"areas": ("mesa",), "budget_min": 250, "budget_max": 250, "kind": "hotel", **OCT20}),
]


class TestTheCeilingWordEndsThePricePhrase:
    @pytest.mark.parametrize("text", OCCUPANCY_MUST_HELP)
    @pytest.mark.parametrize("stored", [STORED_S2, STORED_S1])
    def test_an_occupancy_after_a_price_is_a_comment_not_a_billed_re_search(self, route, text, stored):
        r = route(text, stored)
        assert r is not None and r.kind == "reply" and r.reply == ts.FOLLOWUP_HELP_REPLY, (text, r)

    @pytest.mark.parametrize("text,expect", POSTPOSED_MUST_RERUN)
    def test_the_postposed_ceiling_still_re_runs(self, route, text, expect):
        _assert_search(route(text), text, expect, STORED_S2)

    @pytest.mark.parametrize("text,expect", RESTATED_WITH_OCCUPANCY)
    def test_a_restated_ask_with_an_occupancy_re_runs_on_its_price(self, route, text, expect):
        r = route(text)
        assert r is not None and r.kind == "search", (text, r)
        for k, v in expect.items():
            assert getattr(r.constraints, k) == v, (text, k, getattr(r.constraints, k), v)

    def test_the_parser_never_reads_an_occupancy_as_a_ceiling(self):
        base = ts.parse_constraints(STORED_S2, today=TODAY).constraints
        merged, changed, _mal = ts.merge_followup(base, "love the casita, $289/night, max 6 guests",
                                                  today=TODAY)
        assert (merged.budget_min, merged.budget_max) == (300, 400)

    @pytest.mark.parametrize("shape", [
        " " * 40_000,
        "$250/night max " * 2_600,
        "$250/night, max " + " " * 40_000 + "6 guests",
        "$250/night" + " max" * 10_000,
    ], ids=["ws40k", "ceil-run", "ceil-ws-count", "max-run"])
    def test_the_ceiling_regexes_are_linear(self, shape):
        best = float("inf")
        for _ in range(3):
            t0 = time.perf_counter()
            ts._BUDGET_CEIL_POST_RE.search(shape)
            ts._REFINE_MONEY_RE.search(shape)
            best = min(best, time.perf_counter() - t0)
        assert best < 0.1, best
