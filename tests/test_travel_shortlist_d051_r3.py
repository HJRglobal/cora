"""Code #16 C2 -- D-051 ROUND-3 regressions for the travel lane (fixer R3B).

Every test drives the REAL code path: the pure predicates / parser / sanitizer on
Slack text, route_turn on a real (tmp-redirected) lane-thread store, or the real
handle_message_event / _dispatch_qa entry points with only infrastructure stubbed
(the wiring module's ``lane`` fixture). Guest names are SYNTHETIC ("Jordan
Riverstone", "Mike Jones"). Every table carries its precision rows next to its recall
rows, and every new regex is timed on the whitespace-only 40k input and its
neighbours. Invisible / control characters are written as ESCAPES, never literally.
"""

from __future__ import annotations

import time
from datetime import date

import pytest

import cora.app as app_module
from cora import travel_shortlist as ts
from test_travel_shortlist import NOW, _fx, _msg, _slack_client
from test_travel_shortlist_wiring import (  # noqa: F401 -- `lane` is a fixture
    ASK_TS, TRAVEL_CHANNEL, _card_call, _dm, _drain, _mention, _model_path,
    _say_no_placeholder, _tessa, lane,
)

HARRISON = "U0B2RM2JYJ1"
TODAY = date(2026, 9, 25)


def _wire(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _best_of_3(fn) -> float:
    best = float("inf")
    for _ in range(3):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best


# ── r3:c2-injection-card#0: C1 controls never split a booking word on the card ──

class TestC1ControlsOnTheCard:
    def test_the_finding_repro_end_to_end(self):
        """validate_options + render_card: 'Boo<U+0081>ked ... Con<U+0090>firmed' used to
        reach the T0 card verbatim (a Chromium client draws C1 as nothing)."""
        urls, _e = ts.collect_record_urls([_msg(_fx())])
        raw = [{"property": "Hotel Valley Ho", "nightly_rate": "$250", "kind": "hotel",
                "url": "https://hotelvalleyho.com/",
                "fit_note": "Boo\x81ked for your team of 4 - Con\x90firmed"}]
        opts, dropped = ts.validate_options(raw, urls)
        assert dropped == 0 and len(opts) == 1
        _t, blocks = ts.render_card(ts.parse_constraints(
            "find hotels in scottsdale oct 17-21", today=TODAY).constraints, opts, now=NOW)
        body = blocks[1]["text"]["text"]
        assert not any(0x80 <= ord(ch) <= 0x9F for ch in body), repr(body)
        seen = body.lower()
        assert "booked" not in seen and "confirmed" not in seen, repr(body)

    @pytest.mark.parametrize("raw,want", [
        ("Caf\xe9 Monarch", "Caf\xe9 Monarch"),          # Latin-1 letters just above C1 stay
        ("Diner \xa9 2026", "Diner \xa9 2026"),
        ("pool\x9fside", "pool side"),                  # a C1 control is a SPACE, never deleted
    ])
    def test_precision_latin1_letters_are_kept(self, raw, want):
        import unicodedata
        assert ts.sanitize_field(raw, 160) == unicodedata.normalize("NFKC", want)

    def test_the_control_pass_is_linear(self):
        for shape in (" " * 40000, "\x81" * 40000, "a\x90" * 20000, "Boo\x81ked " * 4000):
            assert _best_of_3(lambda: ts._CTRL_RE.sub(" ", shape)) < 0.05, repr(shape[:3])
            assert _best_of_3(lambda: ts.sanitize_field(shape, 160)) < 0.05, repr(shape[:3])


# ── r3:c2-trigger#1 + r3:integration#0 + r3:c2-trigger#3: the head rule reads the whole ──
# ── noun group; the lane's own vocabulary and spaced symbols end a head               ──

GROUP_MUST_NOT_FIRE = [
    # (a) a coordinated MODIFIER: the second conjunct is not a lodging noun, or the group
    # still modifies another head -- the r2 pins with one conjunct inserted
    "find hotel/airbnb costs in scottsdale for sep 1-15",
    "find hotel or airbnb costs in scottsdale for sep 1-15",
    "get hotel and car costs for september",
    "find hotel and airbnb spend in scottsdale for oct",
    "need hotel and flight details for jordan",
    "find hotel and flight info for the ufl trip",
    "find hotel and flight reservations for tessa",
    "need hotel and car rates for the budget",
    "find hotel/airbnb reviews for the phoenician",
    "find hotels and restaurants in tempe oct 17-21",
    "find hotels & restaurants near the venue in tempe oct 17-21",
    "find hotel + flight packages to vegas oct 17-18",
    "find hotel rooms and catering in scottsdale oct 17-21",
    # business asks the adjudication names: they stay out
    "find hotel conference rooms and catering in scottsdale oct 17-21",
    "find hotel supply vendors in phoenix",
    # (b) a business PURPOSE right after the group
    "find hotels to sponsor the ufl fight in vegas oct 17-18",
    "suggest hotels for the ufl partnership in vegas oct 17-18",
    "get hotels on board with the sponsorship in vegas oct 17-18",
    "find hotels to partner with in mesa",
    "we need to get hotels to carry f3 in scottsdale",
    "find hotels in our pipeline",
    "find hotels on the prospect list in tempe",
    "find hotels that have ordered f3 in scottsdale",
    "find hotels that are f3 customers in tempe",
    "find hotels in the pipeline for q4",
    "find hotels to pitch in scottsdale oct 17-21",
    # "pull ... in" needs an allowlisted area
    "pull up hotels in the budget sheet",
]
GROUP_MUST_FIRE = [
    # spaced symbols and '&' / '+' joins (integration#0)
    "find hotels & airbnbs in mesa oct 17-21",
    "find hotels + airbnbs in mesa oct 17-21",
    "find hotels (or airbnbs) in mesa oct 17-21",
    "find hotels - scottsdale, oct 17-21",
    "find hotels — scottsdale, oct 17-21",
    "find 2 hotels & 1 airbnb in mesa oct 17-21",
    "can you look for airbnbs & vrbos in gilbert oct 17-21",
    "find hotels + rentals in mesa oct 17-21",
    "find hotels & airbnbs with availability in mesa oct 17-21",
    "find hotels (preferably in mesa) oct 17-21",
    'find hotels "near" old town oct 17-21',
    # joins that already fired stay
    "find hotels and airbnbs in mesa oct 17-21", "find hotels/airbnbs in mesa oct 17-21",
    "find hotels, airbnbs in mesa oct 17-21", "find hotels, airbnbs, or vrbos in mesa oct 17-21",
    "find hotels and/or airbnbs in mesa oct 17-21", "find me a hotel or two in mesa oct 17-21",
    "find a hotel or resort in scottsdale oct 17-21", "find hotel rooms in tempe oct 17-21",
    "hotel room options in tempe oct 17-21", "pull up hotels in the scottsdale area oct 17-21",
    # the lane's own vocabulary right after the noun (c2-trigger#3)
    "can you find a hotel that's got availability in mesa oct 17-21",
    "find a hotel that's close to old town scottsdale oct 17-21",
    "find a hotel that'll take dogs in mesa oct 17-21",
    "find hotels max $400/night in mesa oct 17-21",
    "find hotels maximum $400/night in mesa oct 17-21",
    "find hotels up to $400 a night in mesa oct 17-21",
    "find hotels no more than $400 a night in mesa oct 17-21",
    "find hotels not more than $400 a night in mesa oct 17-21",
    "find hotels minimum $200/night in mesa oct 17-21",
    "find hotels above $200 a night in mesa oct 17-21",
    "find hotels about $300 a night in mesa oct 17-21",
    "find hotels roughly $300 a night in mesa oct 17-21",
    "find hotels ~$300 a night in mesa oct 17-21",
    "find hotels budget $300-400 in mesa oct 17-21",
    "find hotels king bed in scottsdale oct 17-21",
    "find hotels mid october in sedona",
    "find hotels that are pet friendly in tempe oct 17-21",
    # a lodging ask for a business TEAM is still a lodging ask
    "find hotels for the ufl fight in vegas oct 17-18",
    "find hotels in vegas for the sponsorship team oct 17-18",
    "find hotels to stay in near the venue in mesa oct 17-21",
]
# the budget words the head rule accepts ARE the parser's (they cannot drift)
BUDGET_PARSE_ROWS = [
    ("find hotels max $400/night in mesa oct 17-21", None, 400),
    ("find hotels up to $400 a night in mesa oct 17-21", None, 400),
    ("find hotels no more than $400 a night in mesa oct 17-21", None, 400),
    ("find hotels minimum $200/night in mesa oct 17-21", 200, None),
    ("find hotels above $200 a night in mesa oct 17-21", 200, None),
    ("find hotels roughly $300 a night in mesa oct 17-21", 300, 300),
]


class TestNounGroupHeadRule:
    @pytest.mark.parametrize("text", GROUP_MUST_NOT_FIRE)
    def test_must_not_fire(self, text):
        for v in (text, _wire(text)):
            assert not ts.looks_like_travel_ask(v, user_id=HARRISON, channel_id="D0HARRISON",
                                                channel_type="im"), v
            assert not ts.looks_like_travel_ask(v, user_id="U_ANYONE",
                                                channel_id=TRAVEL_CHANNEL), v
            assert ts.route_turn(v, user_id=HARRISON, channel_id="D0HARRISON",
                                 channel_name="dm", today=TODAY) is None, v

    @pytest.mark.parametrize("text", GROUP_MUST_FIRE)
    def test_must_fire(self, text):
        for v in (text, _wire(text), text.replace("-", "–")):
            assert ts.looks_like_travel_ask(v, user_id=HARRISON, channel_id="D0HARRISON",
                                            channel_type="im"), v
            assert ts.is_lodging_shaped(v), v                   # strict stays a subset of loose

    @pytest.mark.parametrize("text,lo,hi", BUDGET_PARSE_ROWS)
    def test_the_budget_words_are_the_parsers(self, text, lo, hi):
        r = ts.route_turn(text, user_id=HARRISON, channel_id="D0HARRISON", channel_name="dm",
                          today=TODAY)
        assert r is not None and r.kind == "search", (text, r)
        assert (r.constraints.budget_min, r.constraints.budget_max) == (lo, hi)
        words = (ts._BUDGET_CEIL_WORDS + "|" + ts._BUDGET_FLOOR_WORDS + "|"
                 + ts._BUDGET_APPROX_WORDS).split("|")
        for word in ("max", "up to", "no more than", "minimum", "above", "roughly"):
            assert word in words

    @pytest.mark.parametrize("text", [
        "find hotel/airbnb costs in scottsdale for sep 1-15",
        "find hotels to sponsor the ufl fight in vegas oct 17-18",
        "suggest hotels for the ufl partnership in vegas oct 17-18",
        "need hotel and flight details for jordan",
    ])
    def test_through_the_real_handlers_the_lane_never_takes_them(self, lane, monkeypatch,
                                                                  text):
        fired = []
        monkeypatch.setattr(ts, "execute_route", lambda *a, **k: fired.append(1))
        seen: list = []
        said: list = []
        base = _say_no_placeholder()
        with _model_path(seen):
            _mention(_slack_client(), lambda **kw: said.append(kw) or base(**kw), text,
                     user=_tessa())
            client = _slack_client()
            client.chat_postMessage.side_effect = None
            client.chat_postMessage.return_value = {"ok": True}
            _dm(client, text, user=HARRISON, ts_="1790000000.000700")
        assert fired == []
        assert seen or said
        assert lane.calls == []                                  # nothing billed

    def test_an_ampersand_availability_dm_reaches_the_lane_not_the_shift_scheduler(
            self, lane, monkeypatch):
        """integration#0: 'hotels &amp; airbnbs with availability' fell off the strict
        predicate, so the 'availability' keyword handed Harrison's DM to the OSN shift
        scheduler (which writes DM state step=asking_days)."""
        handled = []
        monkeypatch.setattr(app_module.osn_shift_handler, "handle_dm",
                            lambda **k: handled.append(k))
        client = _slack_client()
        _dm(client, "find hotels &amp; airbnbs with availability in mesa oct 17-21",
            user=HARRISON)
        _drain()
        assert handled == []
        assert _card_call(client)["thread_ts"] == ASK_TS
        assert "Mesa" in lane.calls[-1]["messages"][0]["content"]

    def test_a_thats_availability_dm_reaches_the_lane_not_the_shift_scheduler(
            self, lane, monkeypatch):
        """c2-trigger#3: "that's got availability" is the lane's ask, not the scheduler's."""
        handled = []
        monkeypatch.setattr(app_module.osn_shift_handler, "handle_dm",
                            lambda **k: handled.append(k))
        client = _slack_client()
        _dm(client, "can you find a hotel that’s got availability in mesa oct 17-21",
            user=HARRISON)
        _drain()
        assert handled == []
        assert _card_call(client)["thread_ts"] == ASK_TS

    @pytest.mark.parametrize("shape", [
        " " * 40000, "find hotels" + " " * 40000, "find hotels" + " and hotels" * 4000,
        "find hotels" + " & " * 13000, "find hotels" + "/" * 40000, "find hotels" + " (or" * 10000,
        "find hotels" + ", airbnbs" * 4000, "find hotels" + " rooms" * 6000,
        "find hotels to " * 2500, "find hotels for the " * 2000, "find hotels that are " * 2000,
        "find hotels max " * 2500, "find hotels" + " $" * 20000, "pull up hotels in " * 2000,
        "pull up hotels in " + "the " * 10000, "find hotels" + " -" * 20000,
    ], ids=["spaces", "noun-spaces", "noun-and", "amp", "slashes", "paren-or", "comma-list",
            "rooms", "to", "for-the", "that-are", "max", "dollars", "pull-in", "pull-in-the",
            "dashes"])
    def test_the_group_rule_is_linear(self, shape):
        def run():
            ts._GROUP_EXT_RE.match(shape, 11)
            ts._noun_group_end(shape, 11)
            ts._HEAD_NEXT_RE.match(shape, 11)
            ts._BUSINESS_TAIL_RE.match(shape, 11)
            ts._AREA_PREFIX_RE.match(shape, 11)
            ts._PULL_RE.match(shape)
            ts._alias_at(shape, 18)
            ts.looks_like_travel_ask(shape, user_id=HARRISON, channel_id="D0H", channel_type="im")
            ts.is_lodging_shaped(shape)
        assert _best_of_3(run) < 0.05
