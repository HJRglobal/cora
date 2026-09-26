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
from cora import web_guard
from cora.model_router import MODEL_SONNET
from test_travel_shortlist import NOW, _fx, _msg, _slack_client
from test_travel_shortlist_wiring import (  # noqa: F401 -- `lane` is a fixture
    ASK_TS, TRAVEL_CHANNEL, _card_call, _dm, _drain, _drive_dispatch, _mention, _model_path,
    _say_no_placeholder, _tessa, _web_rows, lane,
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
    "find hotels on the drive to sedona oct 17-21",
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


# ── r3:c2-egress#2 (SPLIT -> fix): B1's recall never rides on the lane's head rule ──

# A frame-governed lodging noun the head rule declines (the lane's PRECISION rule) still
# withholds web on the normal path -- 'inn' is the one strict noun the loose tiers read
# as WEAK, so these reached a web-enabled turn with the guest names in the message.
E2_MUST_WITHHOLD = [
    "search for inns online for Mike Jones and Sarah Lee",
    "search for inns via google for Mike Jones",
    "search for an inn online for Mike Jones, loyalty is on file",
    "search for some inns online for Jordan Riverstone and his wife",
    "find inns and restaurants online for Mike Jones",
    "find inn/airbnb costs for Mike Jones",
    "find inns to partner with online for Mike Jones",
    "find inns in our pipeline for Mike Jones",
]


class TestLooseRecallIsIndependentOfTheHeadRule:
    @pytest.mark.parametrize("text", E2_MUST_WITHHOLD)
    def test_withholds_in_plain_and_wire_form(self, text):
        assert not ts._is_strict_ask(text), text             # the lane declines it...
        assert ts.is_lodging_shaped(text), text              # ...B1 still withholds
        assert ts.is_lodging_shaped(_wire(text)), text

    @pytest.mark.parametrize("text", GROUP_MUST_NOT_FIRE + E2_MUST_WITHHOLD)
    def test_every_head_rule_reject_with_a_lodging_noun_withholds(self, text):
        assert ts.is_lodging_shaped(text), text

    @pytest.mark.parametrize("text", [
        "search for inns online for Mike Jones and Sarah Lee",
        "search for inns via google for Mike Jones",
    ])
    def test_the_web_ask_is_withheld_on_the_real_dispatch(self, lane, text):
        # web_guard WOULD attach (explicit intent) -- only the travel withhold stops it
        assert web_guard.evaluate(text, "HJRG", kb_meta={}, model=MODEL_SONNET).attach
        seen = _drive_dispatch(text, user=_tessa(), channel_id="D0TESSA", channel_name="dm",
                               entity="HJRG")
        assert seen and all(kw.get("web_tools") is False for kw in seen)
        assert any(r.get("reason") == "gate_skipped:travel_lane" for r in _web_rows())

    @pytest.mark.parametrize("text", [
        "The Inn at the Biltmore account ordered 12 cases", "what's our last resort",
        "google the Deposco API changelog", "find the inn receipt from last month",
        "search online for good restaurants near there that weekend",
    ])
    def test_precision_rows_stay_clear(self, text):
        assert not ts.is_lodging_shaped(text), text

    @pytest.mark.parametrize("shape", [
        " " * 40000, "search for inns " * 2500, "find " + "a " * 20000 + "inn",
        "pull up inns in " * 2000,
    ], ids=["spaces", "search-inns", "det-x20000", "pull-in"])
    def test_the_frame_leg_is_linear(self, shape):
        assert _best_of_3(lambda: ts._frame_governs_lodging_noun(shape)) < 0.05
        assert _best_of_3(lambda: ts.is_lodging_shaped(shape)) < 0.25


# ── r3:c2-trigger#5: "where <name> could stay" / "a place for <name> to stay" ──

PERSON_STAY_MUST_WITHHOLD = [
    "google where jordan riverstone could stay",
    "google somewhere jordan riverstone can stay",
    "search the web for a place for jordan riverstone to stay",
    "google a spot for jordan riverstone to crash",
    "look up a place jordan riverstone can crash tonight",
    "google where mike jones would stay",
    "google where Jordan Riverstone might sleep",
    "google anywhere mike jones could stay near the arena",
    "search online for places for mike and sarah to stay",
]
PERSON_STAY_MUST_NOT_WITHHOLD = [
    "where the data should stay", "that's where the bot could crash",
    "find where the importer might crash", "the cache is where the tokens will stay",
    "where it could crash", "stay tuned", "google where the stadium is",
    "google where jordan riverstone works", "a place for everything",
]


class TestPersonStayPhrase:
    @pytest.mark.parametrize("text", PERSON_STAY_MUST_WITHHOLD)
    def test_withholds_as_a_persons_turn_only(self, text):
        assert ts.is_lodging_shaped(text), text
        assert ts.is_lodging_shaped(_wire(text)), text
        # kept OUT of the STRONG tier that also reads Cora's own prose
        assert not ts.is_lodging_strong(text) or "sleep" in text, text

    @pytest.mark.parametrize("text", PERSON_STAY_MUST_NOT_WITHHOLD)
    def test_precision_rows_stay_clear(self, text):
        assert not ts.is_lodging_shaped(text), text

    @pytest.mark.parametrize("text", PERSON_STAY_MUST_WITHHOLD[:4])
    def test_the_web_ask_is_withheld_on_the_real_dispatch(self, lane, text):
        assert web_guard.evaluate(text, "HJRG", kb_meta={}, model=MODEL_SONNET).attach
        seen = _drive_dispatch(text, user=_tessa(), channel_id="D0TESSA", channel_name="dm",
                               entity="HJRG")
        assert seen and all(kw.get("web_tools") is False for kw in seen)
        assert any(r.get("reason") == "gate_skipped:travel_lane" for r in _web_rows())

    def test_coras_prose_with_the_phrase_does_not_withhold(self, lane):
        """Cora's turns read the STRONG tier only: her "that's where the values should
        stay" / "where imports might crash" never blacks out a later web ask."""
        prior = [{"role": "user", "content": "what did the test run say?"},
                 {"role": "assistant", "content": "The cache is where builds would crash; "
                                                  "somewhere jobs can stay queued."}]
        seen = _drive_dispatch("google the Deposco API changelog", user=_tessa(),
                               channel_id="D0TESSA", channel_name="dm", entity="HJRG",
                               prior=prior)
        assert seen and seen[-1].get("web_tools") is True

    @pytest.mark.parametrize("shape", [
        " " * 40000, "where " * 7000, "where a " * 5000, "place for " * 4000,
        "where jordan " * 3000, "somewhere x could " * 2000, "spot " * 8000,
    ], ids=["spaces", "where", "where-a", "place-for", "where-name", "could", "spot"])
    def test_the_phrase_is_linear(self, shape):
        assert _best_of_3(lambda: ts._PERSON_STAY_RE.search(shape)) < 0.05
        assert _best_of_3(lambda: ts.is_lodging_shaped(shape)) < 0.25


# ── r3:c2-trigger#4 (SPLIT -> fix): a bare allowlisted area after the noun is parsed ──

HEAD_SLOT_AREA_ROWS = [
    # the r2 HEAD_NOUN_MUST_FIRE bare-area rows, and the finding's others
    ("find hotels scottsdale oct 17-21", ("scottsdale",)),
    ("find a hotel oct 17-21 scottsdale", ("scottsdale",)),
    ("find hotels, oct 17-21, scottsdale", ("scottsdale",)),
    ("find hotels 10/17-10/21 scottsdale", ("scottsdale",)),
    ("find hotels mesa/gilbert oct 17-21", ("mesa", "gilbert")),
    ("find hotels phx oct 17-21", ("phoenix",)),
    ("find a hotel downtown phoenix oct 17-21", ("phoenix",)),
    ("find hotels old town scottsdale oct 17-21", ("scottsdale",)),
    ("find hotels - scottsdale, oct 17-21", ("scottsdale",)),
    ("find hotels and airbnbs sedona oct 17-21", ("sedona",)),
    ("Hey Cora, find hotels scottsdale oct 17-21?", ("scottsdale",)),
]
# not the head slot (a person-name risk, or no allowlisted area there): the honest
# clarify stays, and a locative area elsewhere still wins
HEAD_SLOT_NOT_AN_AREA = [
    "find hotels for vegas oct 17-18", "find a hotel for charlotte oct 17-21",
    "find hotels for austin oct 17-21", "find hotels surprise me oct 17-21",
    "find hotels page 2 oct 17-21", "find hotels with a pool oct 17-21",
    "find hotels oct 17-21 gilbert's place",
]


class TestHeadSlotArea:
    @pytest.mark.parametrize("text,areas", HEAD_SLOT_AREA_ROWS)
    def test_the_area_after_the_noun_is_searched(self, text, areas):
        for v in (text, _wire(text)):
            r = ts.route_turn(v, user_id=HARRISON, channel_id="D0HARRISON", channel_name="dm",
                              today=TODAY)
            assert r is not None and r.kind == "search", (v, r)
            assert r.constraints.areas == areas, (v, r.constraints.areas)
            assert r.reply != ts.CLARIFY_AREA_REPLY

    @pytest.mark.parametrize("text", HEAD_SLOT_NOT_AN_AREA)
    def test_precision_no_head_slot_area(self, text):
        pr = ts.parse_constraints(text, today=TODAY)
        assert pr.constraints is None and "area" in pr.missing, (text, pr)

    def test_a_locative_area_elsewhere_still_wins(self):
        c = ts.parse_constraints("find hotels scottsdale oct 17-21 in mesa",
                                 today=TODAY).constraints
        assert c is not None and c.areas == ("mesa",)

    def test_a_follow_up_merge_never_reads_the_head_slot(self):
        stored = ts.parse_constraints("find hotels in scottsdale oct 17-21",
                                      today=TODAY).constraints
        merged, changed, _m = ts.merge_followup(stored, "the hotels mesa had were full",
                                                today=TODAY)
        assert not changed and merged.areas == ("scottsdale",)

    def test_through_harrisons_dm_the_bare_area_ask_posts_the_card(self, lane):
        client = _slack_client()
        _dm(client, "find hotels scottsdale oct 17-21", user=HARRISON)
        _drain()
        assert _card_call(client)["thread_ts"] == ASK_TS
        assert "Scottsdale" in lane.calls[-1]["messages"][0]["content"]

    @pytest.mark.parametrize("shape", [
        " " * 40000, "find hotels" + " " * 40000, "find hotels " + "oct 1 " * 6000,
        "find hotels " + ", " * 20000, "find hotels " + "10/1" * 10000,
        "find hotels " + "mesa/" * 8000, "find hotels " + " - " * 13000,
    ], ids=["spaces", "noun-spaces", "dates", "commas", "md", "mesa-slash", "dashes"])
    def test_the_head_slot_is_linear(self, shape):
        def run():
            ts._HEAD_SLOT_LEAD_RE.match(shape, 11)
            ts._head_slot_areas(shape)
            ts.parse_constraints(shape, today=TODAY)
        assert _best_of_3(run) < 0.05


# ── r3:c2-trigger#0 (adjudicated) + r3:c2-trigger#2 (SPLIT -> fix): the lane-thread gate ──

LANE_ROOT = "1790000000.000920"
# the 9/15-shaped stored ask: hotels AND airbnbs, 4 people, $300-400
STORED_ASK = "find hotels and airbnbs in scottsdale oct 17-21 for 4 people $300-400/night"

# A comment ON the posted card is never a billed re-search, and never a new constraint.
CARD_COMMENT_MUST_HELP = [
    "the 2nd option is $450/night, too pricey",
    "the first one says $389/night, that works",
    "the last one is only $210 a night!",
    "I'll send this to 2 people on the team",
    "can you check with jordan if oct 20-22 works",
    "thanks! the 3rd one is $450/night",
    "the 2nd option is 450 a night",
    "love the first one", "love the second one", "the first is too far", "thanks",
    "the second hotel looks great, thanks!",
    "the airbnb in gilbert looks perfect",
    "the hotel in mesa is only $210 a night!",
    "hotels in gilbert look perfect",
    "that one is $389 per night, works for me",
    "could you check with tessa if the 2nd one works",
    "can we check with 2 people on the team first",
    "i'll show this to 3 guests tomorrow",
    "nice, the one in scottsdale is $250 nightly",
    "the 3rd one sleeps 6 people",
    "the first one is about $389 a night",
    "the 2nd one is under $300 a night, nice",
    "thanks, gilbert",                                    # a person named Gilbert
    "Hotel Valley Ho is $329/night", "Hotel Valley Ho sleeps 6 people",
    "the 2nd one has two queens", "the 3rd one is for 6 people",
    "the 2nd one's $450/night", "it sleeps 6 people",
]
# A refinement verb / shape still re-runs on the merged fields.
DIRECTIVE_MUST_RERUN = [
    ("keep it under 250 a night", {"budget_max": 250, "budget_min": None}),
    ("$200-300/night please", {"budget_min": 200, "budget_max": 300}),
    ("max 250/night", {"budget_max": 250}),
    ("6 people now", {"party_size": 6}),
    ("actually it's 6 of us", {"party_size": 6}),
    ("same dates but 6 people", {"party_size": 6}),
    ("make it for 6 people", {"party_size": 6}),
    ("we're 6 people now", {"party_size": 6}),
    ("for 6 people", {"party_size": 6}),
    ("group of twelve", {"party_size": 12}),
    ("something less expensive, under $200 a night", {"budget_max": 200}),
    ("budget $250/night please", {"budget_min": 250, "budget_max": 250}),
    ("make it $300/night", {"budget_min": 300, "budget_max": 300}),
    ("try $300 a night", {"budget_min": 300, "budget_max": 300}),
    ("around $300 a night", {"budget_min": 300, "budget_max": 300}),
    ("can you check oct 20-22", {"check_in": date(2026, 10, 20)}),
    ("also check oct 20-22", {"check_in": date(2026, 10, 20)}),
    ("can you look in mesa", {"areas": ("mesa",)}),
    ("can you check mesa", {"areas": ("mesa",)}),
    ("thanks! try mesa instead", {"areas": ("mesa",)}),
    ("find hotels in mesa oct 17-21 from $200 a night",        # a re-typed full ask
     {"areas": ("mesa",), "budget_min": 200, "budget_max": 200}),
    ("hotels in mesa oct 17-21, 6 guests, $250/night",          # a restated noun-led ask
     {"areas": ("mesa",), "party_size": 6, "budget_min": 250, "budget_max": 250}),
    ("hotels in mesa oct 17-21 for 4 people $250/night",
     {"areas": ("mesa",), "party_size": 4, "budget_min": 250, "budget_max": 250}),
    # a request phrased with "be" / a relative clause is not a description
    ("can it be under $300 a night?", {"budget_min": None, "budget_max": 300}),
    ("something that's under $300 a night", {"budget_min": None, "budget_max": 300}),
    ("anything that is under $300 a night?", {"budget_min": None, "budget_max": 300}),
    ("the budget should be $300 a night", {"budget_min": 300, "budget_max": 300}),
    ("find a condo that sleeps 6 in scottsdale oct 20-22", {"check_in": date(2026, 10, 20)}),
    ("find a hotel that looks modern in mesa oct 20-22", {"areas": ("mesa",)}),
]
# A comment clause beside a refinement never lends it a field.
MIXED_ROWS = [
    ("love the first one, but can we do oct 20-22?",
     {"check_in": date(2026, 10, 20), "budget_min": 300, "budget_max": 400, "party_size": 4}),
    ("thanks! the 3rd one is $450/night, can we do oct 20-22?",
     {"check_in": date(2026, 10, 20), "budget_min": 300, "budget_max": 400}),
    ("try mesa, the airbnb one looked nice", {"areas": ("mesa",), "kind": "both"}),
    ("try mesa and send this to 2 people", {"areas": ("mesa",), "party_size": 4}),
    ("that one but under $300 a night", {"budget_min": None, "budget_max": 300}),
    ("two queens instead", {"beds": "two_queens"}),
]
# c2-trigger#2: the ordinary area / date moves re-run.
AREA_DATE_MUST_RERUN = [
    ("what about mesa?", {"areas": ("mesa",)}), ("how about tempe?", {"areas": ("tempe",)}),
    ("what about phoenix", {"areas": ("phoenix",)}), ("anything in mesa?", {"areas": ("mesa",)}),
    ("mesa?", {"areas": ("mesa",)}), ("prefer mesa", {"areas": ("mesa",)}),
    ("mesa or gilbert?", {"areas": ("mesa", "gilbert")}), ("same but mesa", {"areas": ("mesa",)}),
    ("add mesa", {"areas": ("mesa",)}), ("include gilbert", {"areas": ("gilbert",)}),
    ("look at mesa too", {"areas": ("mesa",)}), ("tempe too?", {"areas": ("tempe",)}),
    ("thanks, mesa?", {"areas": ("mesa",)}),
    ("let's do oct 20-22", {"check_in": date(2026, 10, 20)}),
    ("move it to oct 20-22", {"check_in": date(2026, 10, 20)}),
    ("push it to oct 20-22", {"check_in": date(2026, 10, 20)}),
    ("go with oct 20-22", {"check_in": date(2026, 10, 20)}),
    ("new dates are oct 20-22", {"check_in": date(2026, 10, 20)}),
    ("dates changed to oct 20-22", {"check_in": date(2026, 10, 20)}),
    ("switch the dates to oct 20-22", {"check_in": date(2026, 10, 20)}),
    ("it's actually oct 20-22", {"check_in": date(2026, 10, 20)}),
    ("can we move it to oct 20-22?", {"check_in": date(2026, 10, 20)}),
    ("sorry, dates are oct 18-22", {"check_in": date(2026, 10, 18)}),
    ("any in gilbert?", {"areas": ("gilbert",)}),
    ("hmm, too pricey. anything under $250 a night?", {"budget_min": None, "budget_max": 250}),
    ("these are great! could you also look for airbnbs?", {"kind": "rental"}),
]
# ...while a turn about something else, with no stay referent, stays the help line.
MOVE_MUST_HELP = [
    "in any case, the offsite moved to oct 20-22 in tempe",
    "draft a note to the team about the offsite in tempe oct 20-22",
    "the offsite got pushed to oct 20-22",
    "tell the team the retreat is now oct 20-22 in mesa",
    "the conference is oct 20-22 now", "jordan arrives oct 20-22",
    "can we do the meeting in mesa oct 20-22 instead?", "gilbert said the first one is great",
    "talk to austin about it", "what about gilbert's suggestion?", "send this to charlotte",
    "how about we ask gilbert", "move the meeting to oct 20-22", "push the offsite to oct 20-22",
    "let's do dinner in tempe oct 20-22", "the offsite dates are oct 20-22",
]


class TestLaneThreadGateRound3:
    def _seed(self):
        c = ts.parse_constraints(STORED_ASK, today=TODAY).constraints
        assert c.kind == "both" and (c.budget_min, c.budget_max) == (300, 400)
        ts.append_event("asked", channel="D0HARRISON", root_ts=LANE_ROOT,
                        constraints=c.to_record(), registered=True)

    def _route(self, text):
        return ts.route_turn(text, user_id=HARRISON, channel_id="D0HARRISON", channel_name="dm",
                             thread_root_ts=LANE_ROOT, lane_thread=True, today=TODAY)

    @pytest.mark.parametrize("text", CARD_COMMENT_MUST_HELP + MOVE_MUST_HELP)
    def test_a_comment_on_the_card_gets_the_help_line_and_bills_nothing(self, text):
        self._seed()
        r = self._route(text)
        assert r is not None and r.kind == "reply" and r.reply == ts.FOLLOWUP_HELP_REPLY, (text, r)

    @pytest.mark.parametrize("text,expect", DIRECTIVE_MUST_RERUN + MIXED_ROWS
                             + AREA_DATE_MUST_RERUN)
    def test_a_refinement_re_runs_on_the_merged_fields_only(self, text, expect):
        self._seed()
        r = self._route(text)
        assert r is not None and r.kind == "search" and r.followup, (text, r)
        for k, v in expect.items():
            assert getattr(r.constraints, k) == v, (text, k, getattr(r.constraints, k))

    @pytest.mark.parametrize("text", [
        "the 2nd option is $450/night, too pricey", "the first one is about $389 a night",
        "I'll send this to 2 people on the team", "the 3rd one sleeps 6 people",
        "the 2nd one is under $300 a night, nice", "Hotel Valley Ho is $329/night",
        "hotels like that one cost $329/night", "Hotel Valley Ho sleeps 6 people",
    ])
    def test_merge_never_reads_a_description_as_a_constraint(self, text):
        stored = ts.parse_constraints(STORED_ASK, today=TODAY).constraints
        merged, changed, malformed = ts.merge_followup(stored, text, today=TODAY)
        assert not changed and not malformed and merged == stored, (text, merged)

    def test_the_fresh_parser_is_unchanged(self):
        f = ts.parse_fields("hotels in mesa oct 17-21 for 4 people $250/night", today=TODAY)
        assert f["party_size"] == 4 and (f["budget_min"], f["budget_max"]) == (250, 250)
        f = ts.parse_fields("hotels in mesa oct 17-21, 6 guests", today=TODAY)
        assert f["party_size"] == 6

    def test_through_the_real_handler_a_price_remark_bills_nothing_and_what_about_re_runs(
            self, lane, monkeypatch):
        from concurrent.futures import ThreadPoolExecutor

        from test_travel_shortlist import _msg as _m
        from test_travel_shortlist_wiring import ASK
        client = _slack_client()
        _mention(client, lambda **k: None, ASK, user=_tessa())
        _drain()
        assert len(lane.calls) == 1
        monkeypatch.setattr(app_module, "_TRAVEL_SHORTLIST_POOL", ThreadPoolExecutor(1))
        client2 = _slack_client()
        _mention(client2, lambda **k: None, "the 2nd option is $450/night, too pricey",
                 user=_tessa(), ts_="1790000000.000300", thread_ts=ASK_TS)
        (call,) = client2.chat_postMessage.call_args_list
        assert call.kwargs["text"] == ts.FOLLOWUP_HELP_REPLY
        assert len(lane.calls) == 1                     # no second (billed) web call
        lane._responses.append(_m(_fx()))
        client3 = _slack_client()
        _mention(client3, lambda **k: None, "what about tempe?", user=_tessa(),
                 ts_="1790000000.000400", thread_ts=ASK_TS)
        _drain()
        assert _card_call(client3)["thread_ts"] == ASK_TS
        assert len(lane.calls) == 2 and "Tempe" in lane.calls[-1]["messages"][0]["content"]
        assert "450" not in lane.calls[-1]["messages"][0]["content"]

    @pytest.mark.parametrize("shape", [
        " " * 40000, "the " * 10000, "love " * 8000, "the first one " * 3000, "is $" * 10000,
        ", " * 20000, "? " * 20000, "what about " * 3000, "mesa " * 8000, "mesa/" * 8000,
        "mesa too " * 4000, "for 6 people " * 3000, "make it $" * 5000, "is about $3" * 4000,
        "let's do " * 5000, "move it to " * 3500, "can you check " * 3000, "st. " * 10000,
        "sleeps " * 6000, "dates are " * 4000, "hotels " * 6000, ", 6 people" * 4000,
        "is under $" * 4000,
    ], ids=["spaces", "the", "love", "first-one", "is-dollar", "commas", "questions",
            "what-about", "mesa", "mesa-slash", "mesa-too", "for-people", "make-it", "is-about",
            "lets-do", "move-it", "check", "st-dot", "sleeps", "dates-are", "hotels",
            "list-party", "is-under"])
    def test_the_round3_gate_is_linear(self, shape):
        stored = ts.parse_constraints(STORED_ASK, today=TODAY).constraints

        def run():
            for rx in (ts._CLAUSE_SPLIT_RE, ts._COMMENT_RE, ts._REFINE_RE, ts._REFINE_MONEY_RE,
                       ts._PARTY_DIRECTIVE_RE, ts._BUDGET_DIRECTIVE_RE, ts._REFINE_FIELD_RE,
                       ts._LISTING_FACT_RE, ts._MONEY_MENTION_RE, ts._PARTY_LIST_RE):
                rx.search(shape)
            ts._DATES_ARE_RE.match(shape)
            ts._NOUN_LED_RE.match(shape)
            ts._CARD_REF_RE.search(shape)
            ts._SLOT_LEAD_RE.match(shape)
            ts._SLOT_TAIL_RE.match(shape, 5)
            ts._COPULA_BEFORE_RE.search(shape[:24])
            ts._is_refinement(shape)
            ts.merge_followup(stored, shape, today=TODAY)
        assert _best_of_3(run) < 0.25


# ── r3:integration#1 + r3:c2-egress#0: consecutive same-author messages are ONE turn ──

from test_travel_shortlist_d051_r2 import (  # noqa: E402,F401 -- `real_history` is a fixture
    _cora, _dm_client, _person, _web_tools_through_the_dm, real_history,
)

LATER_DM = "google restaurants near old town scottsdale that weekend"
LATER_THREAD = "google the best steakhouses in scottsdale"


def _thread_web_tools(client, text, *, user, root, channel="C0F3ESALES", name="f3e-sales"):
    seen: list = []
    with _model_path(seen):
        _mention(client, _say_no_placeholder(), text, user=user, ts_="1790000000.000990",
                 thread_ts=root, channel=channel, name=name)
    assert seen, "the ordinary pipeline never reached the model"
    return [kw.get("web_tools") for kw in seen]


class TestConsecutivePersonMessagesAreOneTurn:
    def test_the_split_dm_ask_withholds_a_later_web_turn(self, lane, real_history):
        """Tessa's two quick messages merge into ONE user turn the model sees; the weak
        noun ('suites') and its cue ('oct 17-21') were judged apart."""
        assert web_guard.evaluate(LATER_DM, "HJRG", kb_meta={}, model=MODEL_SONNET).attach
        assert not ts.is_lodging_shaped(LATER_DM)
        client = _dm_client([
            _cora("1790000000.000600", "Got it - noted the dates."),
            _cora("1790000000.000500", "Happy to help. Which city?"),
            _person("1790000000.000400", _tessa(), "oct 17-21 for Mike Jones and Sarah Lee"),
            _person("1790000000.000300", _tessa(), "we need two suites"),
        ])
        hist = app_module._fetch_dm_history(client, "D0TESSA", "1790000000.000990")
        assert hist[0] == {"role": "user", "content": "we need two suites\noct 17-21 for "
                                                        "Mike Jones and Sarah Lee"}
        assert app_module._prior_turns_by_author(hist)[0] == [hist[0]["content"]]
        assert _web_tools_through_the_dm(client, LATER_DM, user=_tessa()) == [False]
        assert any(r.get("reason") == "gate_skipped:travel_lane" for r in _web_rows())

    @pytest.mark.parametrize("first,second", [
        ("Jordan Riverstone and his manager land Friday - can we get them a casita?",
         "yes, Oct 17-21"),
        ("Mike Jones wants a suite, phone 602-555-0142", "ok -- somewhere in Scottsdale"),
        ("put Mike Jones on our Honors", "482915736"),
        ("can we get Jordan a room", "Oct 17-21"),
        ("Jordan wants a rental", "in scottsdale"),
    ])
    def test_the_split_channel_thread_ask_withholds_a_later_web_turn(self, lane, real_history,
                                                                      first, second):
        """Two people's consecutive thread messages merge into one user turn."""
        assert web_guard.evaluate(LATER_THREAD, "F3E", kb_meta={}, model=MODEL_SONNET).attach
        assert not ts.is_lodging_shaped(first) and not ts.is_lodging_shaped(second)
        root = "1790000000.000200"
        client = _dm_client([
            _person("1790000000.000400", _tessa(), second),
            _person(root, "U0ALEX", first),
        ])
        assert _thread_web_tools(client, LATER_THREAD, user=_tessa(), root=root) == [False]

    def test_precision_an_interleaved_cora_reply_keeps_the_messages_apart(self, lane,
                                                                           real_history):
        """The model sees person / Cora / person as three turns -- no merged lodging
        turn, so an unrelated web ask attaches (unchanged from round 2)."""
        client = _dm_client([
            _person("1790000000.000500", _tessa(), "nice, thanks"),
            _cora("1790000000.000450", "Full suite green: 19,342 passed."),
            _person("1790000000.000400", _tessa(), "what did the test run say?"),
        ])
        assert _web_tools_through_the_dm(client, "google the Deposco API changelog",
                                         user=_tessa()) == [True]

    def test_precision_two_plain_person_messages_do_not_withhold(self, lane, real_history):
        client = _dm_client([
            _person("1790000000.000500", _tessa(), "and the Deposco sandbox too"),
            _person("1790000000.000400", _tessa(), "what changed in the API last week?"),
        ])
        assert _web_tools_through_the_dm(client, "google the Deposco API changelog",
                                         user=_tessa()) == [True]

    def test_the_record_groups_runs_exactly_like_the_merge(self, real_history):
        client = _dm_client([
            _cora("1790000000.000700", "c2"), _cora("1790000000.000600", "c1"),
            _person("1790000000.000500", "U1", "p3"), _person("1790000000.000400", "U1", "p2"),
            _cora("1790000000.000300", "c0"), _person("1790000000.000200", "U1", "p1"),
        ])
        hist = app_module._fetch_dm_history(client, "D0X", "1790000000.000990")
        person, cora = app_module._prior_turns_by_author(hist)
        assert person == ["p1", "p2\np3"] and cora == ["c0", "c1\nc2"]
        assert [t["content"] for t in hist if t["role"] == "user"] == ["p1", "p2\np3"]

    def test_a_leading_cora_run_stays_coras(self, real_history):
        client = _dm_client([_person("1790000000.000500", "U1", "nice"),
                             _cora("1790000000.000400", "a"), _cora("1790000000.000300", "b")])
        hist = app_module._fetch_dm_history(client, "D0X", "1790000000.000990")
        assert app_module._prior_turns_by_author(hist) == (["nice"], ["b\na"])
