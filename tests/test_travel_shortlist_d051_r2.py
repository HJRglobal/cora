"""Code #16 C2 -- D-051 ROUND-2 regressions for the travel lane's predicates, parser,
routing and the B1 prior-turn leg (fixer R2D).

Every test drives the REAL code path: the pure predicates on Slack text, route_turn on a
real (tmp-redirected) lane-thread store, or the real handle_mention /
handle_message_event / _dispatch_qa entry points with only infrastructure stubbed (the
wiring module's ``lane`` fixture). Guest names are SYNTHETIC ("Jordan Riverstone",
"Mike Jones"); loyalty numbers are made-up digit runs. Every table carries its
precision rows next to its recall rows, and every new regex is timed on the
whitespace-only 40k input and its neighbours.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

import cora.app as app_module
from cora import web_guard
from cora import travel_shortlist as ts
from cora.model_router import MODEL_SONNET
from test_travel_shortlist import _fx, _msg, _slack_client
from test_travel_shortlist_wiring import (  # noqa: F401 -- `lane` is a fixture
    _REAL_DM_HISTORY, _REAL_THREAD_HISTORY, ASK, ASK_TS, TRAVEL_CHANNEL, _card_call, _dm,
    _drain, _drive_dispatch, _mention, _model_path, _say_no_placeholder, _tessa, _web_rows,
    lane,
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


def _now():
    return datetime.now(timezone(timedelta(hours=-7)))


# ── r2:c2-egress#1 + r2:c2-trigger#7: digit-anchored loyalty numbers, brands, words ──

LOYALTY_BRAND_MUST_WITHHOLD = [
    # the finding's loyalty-number forms (digits after the program word, any connector)
    "search online for Scottsdale rates we can get with our Honors 482915736",
    "put it on our Honors 482915736 for Mike Jones",
    "search online for rates, honors: 123456789", "google rates, honors#482915736",
    "search online for rates, member 987654321", "google rates, member no 482915736",
    "google rates, rewards member 482915736", "Honors No: 482915736",
    "Membership No: 482915736", "rewards no: 482915736", "rewards#482915736",
    "google rates, company account 482915736 for mike jones", "acct#482915736",
    # brands and programs a Phoenix-based team books
    "google a Best Western in Mesa for Mike Jones",
    "google Wyndham Scottsdale for Mike Jones, Wyndham Rewards 123456789",
    "search online for rates, Best Western Rewards 123456789",
    "search online for rates, Choice Privileges 123456789",
    "google La Quinta in Mesa for Mike Jones", "google Andaz Scottsdale for Mike Jones",
    "search online for the Omni Montelucia for Mike Jones", "google Radisson for Mike Jones",
    "Residence Inn Scottsdale", "SpringHill Suites Scottsdale", "Home2 Suites Mesa",
    "the W Scottsdale", "Sonesta rates", "Loews availability",
    # lodging words the first cut missed (apartment/townhouse cue-gated like condo)
    "google an apartment in Mesa for Mike Jones Oct 17-21",
    "google a townhouse in Gilbert for Mike Jones oct 17-21",
    "google a furnished apartment for jordan riverstone in tempe",
    "google a guesthouse in scottsdale for jordan riverstone",
    "google a guest house in scottsdale for jordan riverstone",
    "google a timeshare in scottsdale for jordan riverstone",
    "google glamping for jordan riverstone near sedona oct 17-21",
    "google booking.com scottsdale for jordan riverstone",
    "google expedia scottsdale for jordan riverstone oct 17-21",
    "google where jordan riverstone can sleep in scottsdale oct 17-21",
]
LOYALTY_BRAND_MUST_NOT_WITHHOLD = [
    "our points no longer transfer", "what's the balance in account 10100?",
    "account 4000 is the revenue line", "graduated with honors in 2019", "member since 2019",
    "our omnichannel strategy", "the omni-channel plan", "omni channel budget",
    "according to the report", "the apartment complex in Mesa we're underwriting",
    "apartment units in Tempe for the Oct 17 report", "the townhouse development in Gilbert",
    "Booking a demo with Deposco", "rewards program ideas for F3", "google the Deposco API changelog",
    # a software crash is not a place to crash (Cora's own prose reads STRONG-only)
    "that's where the bot could crash", "find where the importer might crash",
]


class TestLoyaltyBrandRecall:
    @pytest.mark.parametrize("text", LOYALTY_BRAND_MUST_WITHHOLD)
    def test_withholds_in_plain_and_wire_form(self, text):
        assert ts.is_lodging_shaped(text), text
        assert ts.is_lodging_shaped(_wire(text)), text

    @pytest.mark.parametrize("text", LOYALTY_BRAND_MUST_NOT_WITHHOLD)
    def test_precision_rows_do_not(self, text):
        assert not ts.is_lodging_shaped(text), text
        assert not ts.is_lodging_strong(text), text

    @pytest.mark.parametrize("text", [
        "search online for Scottsdale rates we can get with our Honors 482915736",
        "google a Best Western in Mesa for Mike Jones",
        "google Wyndham Scottsdale for Mike Jones, Wyndham Rewards 123456789",
        "google an apartment in Mesa for Mike Jones Oct 17-21",
        "google glamping for jordan riverstone near sedona oct 17-21",
        "google expedia scottsdale for jordan riverstone oct 17-21",
    ])
    def test_the_web_ask_is_withheld_on_the_real_dispatch(self, lane, text):
        # web_guard WOULD attach (explicit intent) -- only the travel withhold stops it
        assert web_guard.evaluate(text, "HJRG", kb_meta={}, model=MODEL_SONNET).attach
        seen = _drive_dispatch(text, user=_tessa(), channel_id="D0TESSA", channel_name="dm",
                               entity="HJRG")
        assert seen and all(kw.get("web_tools") is False for kw in seen)
        assert any(r.get("reason") == "gate_skipped:travel_lane" for r in _web_rows())

    def test_a_prior_honors_number_turn_withholds_a_later_web_ask(self, lane):
        later = "search online for restaurants in old town this weekend"
        assert web_guard.evaluate(later, "HJRG", kb_meta={}, model=MODEL_SONNET).attach
        prior = [{"role": "user", "content": "put it on our Honors 482915736 for Mike Jones"}]
        seen = _drive_dispatch(later, user=_tessa(), channel_id="D0TESSA", channel_name="dm",
                               entity="HJRG", prior=prior)
        assert seen and seen[-1].get("web_tools") is False


# ── r2:c2-egress#0 (SPLIT -> fix): resort / inn / suites heading a named property ──

NAMED_PROPERTY_MUST_WITHHOLD = [
    "google the Phoenician resort for Mike Jones and Sarah Lee",
    "look up the Boulders Resort and Spa on the web for Tessa Jones",
    "search the web for a suite at the Sanctuary for Mike Jones",
    "google Hermosa Inn availability for Mike Jones",
    "google the phoenician resort for mike jones",          # resort/inn read any case
    "Embassy Suites", "Canyon Suites at the Phoenician", "staying at the Phoenician",
    "rooms at the Boulders for Mike Jones", "two nights at the Sanctuary", "a ski resort",
    "the Days Inn by the airport",
]
NAMED_PROPERTY_MUST_NOT_WITHHOLD = [
    "as a last resort, restart the bot", "what's our last resort", "the first resort we tried",
    "G Suite admin console", "google Adobe Creative Suite pricing", "renew the Adobe suite",
    "office suite", "a suite of tools", "the Suite 200 office lease", "Test suites passed",
    "our product suites", "Office Suites for lease", "managers resort to spreadsheets",
    "we had to resort to manual counts", "The Inn at the Biltmore account ordered 12 cases",
    "Full suite green: 19,342 passed.", "stay at home", "stay at the top of the list",
    "we stayed at 20 cases",
    # the lodging frame never reads meeting-space rooms or a system as a property
    "the board room at HQ", "Board room at the Biltmore at 10:00",
    "conference rooms at the Biltmore", "Meeting rooms at the Regus",
    "check the booking at Deposco", "stay at HQ", "rooms at the Office",
]


class TestNamedProperty:
    @pytest.mark.parametrize("text", NAMED_PROPERTY_MUST_WITHHOLD)
    def test_withholds(self, text):
        assert ts.is_lodging_shaped(text), text
        assert ts.is_lodging_strong(text), text       # STRONG: it counts in Cora's turns too

    @pytest.mark.parametrize("text", NAMED_PROPERTY_MUST_NOT_WITHHOLD)
    def test_idioms_and_software_senses_do_not(self, text):
        assert not ts.is_lodging_shaped(text), text
        assert not ts.is_lodging_strong(text), text

    @pytest.mark.parametrize("text", NAMED_PROPERTY_MUST_WITHHOLD[:4])
    def test_tessas_named_property_web_ask_is_withheld(self, lane, text):
        assert web_guard.evaluate(text, "HJRG", kb_meta={}, model=MODEL_SONNET).attach
        assert ts.route_turn(text, user_id=_tessa(), channel_id="D0TESSA",
                             channel_name="dm", today=TODAY) is None
        seen = _drive_dispatch(text, user=_tessa(), channel_id="D0TESSA", channel_name="dm",
                               entity="HJRG")
        assert seen and all(kw.get("web_tools") is False for kw in seen)


class TestLoosePathStaysLinear:
    @pytest.mark.parametrize("shape", [
        " " * 40000, "honors " * 5000, "honors:" * 5000, "honors-no " * 4000,
        "account " * 5000, "1" * 40000, "honors " + "1" * 40000, "where " * 7000,
        "where a " * 5000, "a-" * 20000, "a'" * 20000, "Stay at " * 5000, "x resort " * 4000,
        "Suites " * 5000, "Aa Suites " * 4000, "apartment " * 4000, "omni " * 7000,
        " " * 40000 + "Hermosa Inn",
    ], ids=["spaces", "honors", "honors-colon", "honors-no", "account", "digits",
            "honors-digits", "where", "where-a", "a-dash", "a-apos", "stay-at", "resort",
            "suites", "title-suites", "apartment", "omni", "spaces-inn"])
    def test_every_new_loose_pattern_is_linear(self, shape):
        def run():
            ts.is_lodging_shaped(shape)
            ts.is_lodging_strong(shape)
            ts._LODGING_STRONG_RE.search(shape.lower())
            ts._LODGING_WEAK_RE.search(shape.lower())
            list(ts._PROPERTY_RE.finditer(shape))
            ts._LODGING_AT_RE.search(shape)
        assert _best_of_3(run) < 0.25


# ── r2:c2-trigger#4 + r2:c2-egress#2: the prior-turn leg reads ORIGINAL authors ──

SUITE_GREEN = "Full suite green: 19,342 passed -- merged Sep 24."
ROOM_BOOKED = "Your 9/26 agenda: 10:00 board room with Alex, 2:00 conference room booked."
RELAY = ("On 9/15 you asked for hotels or Airbnbs in Mesa/Gilbert/Scottsdale, Oct 17-21, for "
         "Mike Jones, Sarah Lee and two others, on the company Hilton Honors account 482915736.")


def _dm_client(history_newest_first: list[dict]) -> MagicMock:
    client = _slack_client()
    client.chat_postMessage.side_effect = None
    client.chat_postMessage.return_value = {"ok": True}
    client.conversations_history.return_value = {"messages": history_newest_first}
    client.conversations_replies.return_value = {"messages": list(reversed(history_newest_first))}
    return client


def _cora(ts_: str, text: str) -> dict:
    return {"ts": ts_, "bot_id": "B1", "user": "UBOT", "text": text}


def _person(ts_: str, user: str, text: str) -> dict:
    return {"ts": ts_, "user": user, "text": text}


@pytest.fixture
def real_history(monkeypatch):
    """The REAL history builders (the lane fixture stubs them) with a known bot id."""
    monkeypatch.setattr(app_module, "_fetch_dm_history", _REAL_DM_HISTORY)
    monkeypatch.setattr(app_module, "_fetch_thread_history", _REAL_THREAD_HISTORY)
    monkeypatch.setattr(app_module, "_CORA_BOT_USER_ID", "UBOT", raising=False)


def _web_tools_through_the_dm(client, text, *, user, thread_ts=None) -> list:
    seen: list = []
    with _model_path(seen):
        _dm(client, text, user=user, ts_="1790000000.000990", thread_ts=thread_ts)
    assert seen, "the ordinary pipeline never reached the model"
    return [kw.get("web_tools") for kw in seen]


class TestPriorTurnLegByOriginalAuthor:
    @pytest.mark.parametrize("who", ["tessa", "harrison"])
    def test_coras_oldest_dm_message_is_not_scanned_as_the_persons(self, lane, real_history,
                                                                   who):
        """The builders fold Cora's leading turn into the first USER turn; a role filter
        alone scanned it (weak 'suite' + 'Sep 24' -> withheld an unrelated web ask)."""
        user = _tessa() if who == "tessa" else HARRISON
        client = _dm_client([_person("1790000000.000500", user, "nice, thanks"),
                             _cora("1790000000.000400", SUITE_GREEN)])
        hist = _REAL_DM_HISTORY(client, "D0X", "1790000000.000990")
        assert hist[0]["role"] == "user" and SUITE_GREEN in hist[0]["content"]   # the fold
        assert ts.is_lodging_shaped(hist[0]["content"])        # what the old leg read
        assert _web_tools_through_the_dm(client, "google the Deposco API changelog",
                                         user=user) == [True]

    def test_a_run_of_proactive_cora_posts_is_not_scanned_as_the_persons(self, lane,
                                                                         real_history):
        client = _dm_client([_cora("1790000000.000500", ROOM_BOOKED),
                             _cora("1790000000.000400", SUITE_GREEN)])
        assert _web_tools_through_the_dm(client, "google the Deposco API changelog",
                                         user=_tessa()) == [True]

    def test_a_cora_opened_pane_thread_parent_is_not_scanned_as_the_persons(self, lane,
                                                                            real_history):
        root = "1790000000.000100"
        client = _dm_client([_person("1790000000.000500", _tessa(), "ok"),
                             _cora(root, ROOM_BOOKED)])
        assert _web_tools_through_the_dm(client, "google the Deposco API changelog",
                                         user=_tessa(), thread_ts=root) == [True]

    def test_a_cora_relay_naming_guests_and_an_honors_number_withholds(self, lane,
                                                                       real_history):
        """r2:c2-egress#2 (ruled SPLIT -> fix): Cora's turns count with the STRONG tier."""
        later = "search online for good restaurants near old town scottsdale that weekend"
        assert web_guard.evaluate(later, "HJRG", kb_meta={}, model=MODEL_SONNET).attach
        client = _dm_client([_cora("1790000000.000500", RELAY),
                             _person("1790000000.000400", _tessa(),
                                     "what did I ask for about the october offsite?")])
        assert _web_tools_through_the_dm(client, later, user=_tessa()) == [False]
        assert any(r.get("reason") == "gate_skipped:travel_lane" for r in _web_rows())

    def test_a_folded_cora_opening_with_strong_lodging_still_withholds(self, lane,
                                                                       real_history):
        client = _dm_client([_person("1790000000.000500", _tessa(), "thanks"),
                             _cora("1790000000.000400", "Booked view: the hotel for Jordan "
                                                        "Riverstone is the Hilton.")])
        assert _web_tools_through_the_dm(client, "google the Deposco API changelog",
                                         user=_tessa()) == [False]

    @pytest.mark.parametrize("prior,withheld", [
        # a hand-built / copied list lost the builders' record: split on the fold's labels
        ([{"role": "user", "content": app_module._CORA_OPENED_MARK + SUITE_GREEN
           + app_module._CORA_REPLY_MARK + "nice, thanks"}], False),
        ([{"role": "user", "content": app_module._CORA_OPENED_MARK + SUITE_GREEN}], False),
        ([{"role": "user", "content": app_module._CORA_OPENED_MARK + SUITE_GREEN
           + app_module._CORA_REPLY_MARK + "rooms in scottsdale oct 17-21 for Jordan"}], True),
        ([{"role": "user", "content": app_module._CORA_OPENED_MARK + RELAY}], True),
        ([{"role": "user", "content": "what did the test run say?"},
          {"role": "assistant", "content": SUITE_GREEN}], False),
    ], ids=["fold+reply", "lone-fold", "fold+lodging-reply", "fold-strong", "plain"])
    def test_a_plain_list_splits_the_fold_by_its_labels(self, lane, prior, withheld):
        seen = _drive_dispatch("google the Deposco API changelog", user=_tessa(),
                               channel_id="D0TESSA", channel_name="dm", entity="HJRG",
                               prior=prior)
        assert seen and seen[-1].get("web_tools") is (not withheld)

    def test_the_builders_record_survives_only_as_an_attribute(self, real_history):
        client = _dm_client([_person("1790000000.000500", "U1", "nice"),
                             _cora("1790000000.000400", SUITE_GREEN)])
        hist = _REAL_DM_HISTORY(client, "D0X", "1790000000.000990")
        assert hist.authored == (("assistant", SUITE_GREEN), ("user", "nice"))
        assert app_module._prior_turns_by_author(hist) == (["nice"], [SUITE_GREEN])
        # the API only ever sees plain role/content dicts (consumers copy the list)
        assert list(hist) == [{"role": "user", "content": app_module._CORA_OPENED_MARK
                               + SUITE_GREEN + app_module._CORA_REPLY_MARK + "nice"}]


# ── r2:c2-trigger#0 + #1: the lodging noun must HEAD the object; "pull" scoped ──

MODIFIER_MUST_NOT_FIRE = [
    # the lodging noun modifies another head noun
    "suggest hotel restaurants in tempe for oct 17-21",
    "find the top hotel spas in scottsdale oct 17-21",
    "find hotel conference rooms in scottsdale for oct 17-21",
    "find hotel meeting space in scottsdale oct 17-21",
    "recommend hotel bars in scottsdale for a client dinner oct 17-18",
    "hotel partnership options in scottsdale for oct 17-21",
    "hotel sponsorship ideas for the ufl fight in vegas oct 17-18",
    "find hotel partners for OSN in mesa", "find hotel leads in hubspot",
    "need new hotel photos for the website", "hotel marketing ideas for F3",
    "find the best hotel restaurant in scottsdale", "get hotel address in scottsdale",
    "i need hotel wifi password", "find hotel reviews for the phoenician",
    "find hotel reservations for tessa", "need hotel rates for the budget",
    # the round-1 widening's finance / CRM / retrieval hijacks
    "pull hotel spend from quickbooks for q3", "pull up hotel charges on the amex for september",
    "pull up hotel charges in scottsdale for sep 1-15",
    "pull up lodging costs for the ufl event in vegas oct 17-18",
    "suggestions on hotel marketing", "we're trying to find hotels that carry f3 in tempe",
    "i'm searching for hotel buyers in phoenix", "can you pull airbnb payouts for q3",
    "pull up airbnb bookings for rogers ranch in october", "pull hotels from quickbooks for q3",
    # a first clause naming a system of record is a data / CRM pull, whatever the noun
    "search for hotels in hubspot", "find hotels in quickbooks for q3",
    "get hotels from the amex statement", "find airbnbs in the crm",
    "look for hotels in the p&l for september", "find hotels from last year's trip",
]
HEAD_NOUN_MUST_FIRE = [
    "find hotels near old town scottsdale oct 17-21", "find a hotel downtown phoenix oct 17-21",
    "find hotels scottsdale oct 17-21", "find hotels for oct 17-21 in scottsdale",
    "find hotels, oct 17-21, scottsdale", "find an airbnb for 6 people in sedona oct 17-21",
    "find hotels with a pool in scottsdale oct 17-21",
    "find hotels that have a pool in scottsdale oct 17-21",
    "find hotels that allow dogs in mesa oct 17-21",
    "recommend hotels close to the stadium in glendale oct 17-18",
    "find hotel rooms in tempe oct 17-21", "find hotels under $300 in scottsdale oct 17-21",
    "find hotels this weekend in sedona", "find a hotel oct 17-21 scottsdale",
    "find hotels 10/17-10/21 scottsdale", "find airbnbs/vrbos in sedona oct 17-21",
    "find a hotel or resort in scottsdale oct 17-21",
    "find hotels available oct 17-21 in scottsdale",
    "find a hotel walking distance from old town scottsdale oct 17-21",
    "find hotels w/ pool in scottsdale oct 17-21", "find hotels along the strip in vegas oct 17-18",
    "find hotels the weekend of oct 17 in sedona", "find some hotels!",
    "any suggestions on hotels in scottsdale for oct 17-21?",
    "we're trying to find an airbnb in sedona oct 17-21",
    "i'm searching for hotels in phoenix oct 17-21",
    "can you pull hotel options in scottsdale for oct 17-21", "pull up hotels in mesa oct 17-21",
    "pull up hotels for oct 17-21 in mesa", "pull up some hotel and airbnb options in tempe",
    "hotel or airbnb options in gilbert for oct 17-21", "hotel room options in tempe oct 17-21",
    "find hotels from $200 a night in mesa oct 17-21", "find hotels from oct 17 to 21 in tempe",
]


class TestHeadNounFrame:
    @pytest.mark.parametrize("text", MODIFIER_MUST_NOT_FIRE)
    def test_must_not_fire(self, text):
        assert not ts.looks_like_travel_ask(text, user_id=HARRISON, channel_id="D0HARRISON",
                                            channel_type="im"), text
        assert not ts.looks_like_travel_ask(text, user_id="U_ANYONE",
                                            channel_id=TRAVEL_CHANNEL), text
        assert ts.route_turn(text, user_id=HARRISON, channel_id="D0HARRISON",
                             channel_name="dm", today=TODAY) is None

    @pytest.mark.parametrize("text", HEAD_NOUN_MUST_FIRE)
    def test_must_fire(self, text):
        for v in (text, _wire(text), text.replace("-", "–")):
            assert ts.looks_like_travel_ask(v, user_id=HARRISON, channel_id="D0HARRISON",
                                            channel_type="im"), v
            assert ts.is_lodging_shaped(v), v                    # strict stays a subset of loose

    @pytest.mark.parametrize("text", [
        "suggest hotel restaurants in tempe for oct 17-21",
        "pull up hotel charges in scottsdale for sep 1-15",
        "pull hotel spend from quickbooks for q3",
        "hotel sponsorship ideas for the ufl fight in vegas oct 17-18",
    ])
    def test_through_the_real_handlers_the_lane_never_takes_them(self, lane, monkeypatch, text):
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

    @pytest.mark.parametrize("shape", [
        " " * 40000, "find hotel" + " " * 40000, "find hotels " + "and " * 10000,
        "pull up " * 5000 + "hotels", "pull hotels" + " or hotels" * 4000,
        "hotels" + " and hotels" * 4000 + " options", "find hotels that " * 2500,
        "find hotel" + "s" * 40000,
    ], ids=["spaces", "noun-spaces", "noun-and", "pull-x5000", "pull-or", "options-and",
            "that", "noun-s"])
    def test_the_head_rule_and_pull_are_linear(self, shape):
        def run():
            ts._HEAD_NEXT_RE.match(shape, 10)
            ts._PULL_RE.match(shape)
            ts._NOUN_OPTIONS_RE.match(shape)
            ts._FRAME_RE.match(shape)
            ts.looks_like_travel_ask(shape, user_id=HARRISON, channel_id="D0H", channel_type="im")
        assert _best_of_3(run) < 0.05


# ── r2:c2-trigger#2 + #3: refinements re-run; turns about another subject do not ──

LANE_ROOT = "1790000000.000910"

MUST_RERUN = [
    ("can we do oct 20-22?", {"check_in": date(2026, 10, 20)}),
    ("also check oct 20-22", {"check_in": date(2026, 10, 20)}),
    ("can you search oct 24-28", {"check_in": date(2026, 10, 24)}),
    ("same thing but in phoenix", {"areas": ("phoenix",)}),
    ("what's available in mesa?", {"areas": ("mesa",)}),
    ("can you look in tempe too?", {"areas": ("tempe",)}),
    ("please look in chandler", {"areas": ("chandler",)}),
    ("search again for oct 20-22 in mesa", {"areas": ("mesa",), "check_in": date(2026, 10, 20)}),
    ("6 people now", {"party_size": 6}),
    ("actually it's 6 of us", {"party_size": 6}),
    ("keep it under 250 a night", {"budget_max": 250}),
    ("$200-300/night please", {"budget_min": 200, "budget_max": 300}),
    ("max 250/night", {"budget_max": 250}),
    ("prefer a king", {"beds": "king"}),
    ("something with a pool", {"styles": ("pool",)}),
    ("modern please", {"styles": ("modern",)}),
    ("change dates to oct 20-22", {"check_in": date(2026, 10, 20)}),
    ("switch to tempe", {"areas": ("tempe",)}),
    ("what about oct 20-22? we're coming from denver",            # 'from X' is not a place
     {"check_in": date(2026, 10, 20), "areas": ("scottsdale",)}),
    ("hotels near old town with free breakfast oct 20-22", {"check_in": date(2026, 10, 20)}),
    ("find hotels near the meeting venue in mesa oct 18-22", {"areas": ("mesa",)}),
    ("in the scottsdale area oct 20-22?", {"check_in": date(2026, 10, 20)}),
    # an other-subject word beside the lodging noun is an amenity or an aside
    ("hotels in mesa with a gym", {"areas": ("mesa",)}),
    ("what about flights and hotels in phoenix?", {"areas": ("phoenix",)}),
    ("hotels with a restaurant on site, oct 20-22", {"check_in": date(2026, 10, 20)}),
    ("hotels within a short drive of old town scottsdale oct 20-22",
     {"check_in": date(2026, 10, 20)}),
    ("something with a pool and an airport shuttle", {"styles": ("pool",)}),
    ("check in oct 20, check out oct 22", {"check_in": date(2026, 10, 20)}),
    ("arriving oct 20th and leaving on the 23rd", {"check_out": date(2026, 10, 23)}),
]
MUST_HELP = [
    "in the meantime, what's the weather in phoenix?",
    "what about flights to phoenix on oct 17?",
    "try southwest flights from denver instead",
    "what about rental cars in scottsdale?",
    "and in mesa, is there a good gym?",
    "oct 17 is when jordan lands in phoenix",
    "thanks! also what about the offsite agenda for oct 20-22",
    "what about dinner in tempe oct 20-22?",
    "can we do the meeting in mesa oct 20-22 instead?",
    "in any case, the offsite moved to oct 20-22 in tempe",
    "book the second one",
    "what time is check in on oct 20-22?",
]


class TestRefinementGateRound2:
    def _seed(self):
        c = ts.parse_constraints("find hotels in scottsdale oct 17-21 for 4 people",
                                 today=TODAY).constraints
        ts.append_event("asked", channel="D0HARRISON", root_ts=LANE_ROOT,
                        constraints=c.to_record(), registered=True)

    def _route(self, text):
        return ts.route_turn(text, user_id=HARRISON, channel_id="D0HARRISON", channel_name="dm",
                             thread_root_ts=LANE_ROOT, lane_thread=True, today=TODAY)

    @pytest.mark.parametrize("text,expect", MUST_RERUN)
    def test_an_ordinary_refinement_re_runs_on_the_merged_fields(self, text, expect):
        self._seed()
        r = self._route(text)
        assert r is not None and r.kind == "search" and r.followup, (text, r)
        for k, v in expect.items():
            assert getattr(r.constraints, k) == v, (text, k, getattr(r.constraints, k))

    @pytest.mark.parametrize("text", MUST_HELP)
    def test_a_turn_about_another_subject_gets_the_help_line(self, text):
        self._seed()
        r = self._route(text)
        assert r is not None and r.kind == "reply" and r.reply == ts.FOLLOWUP_HELP_REPLY, (text, r)

    def test_the_help_line_shows_the_grammar_it_declined(self):
        assert "try Oct 20–22 instead" in ts.FOLLOWUP_HELP_REPLY
        assert "under $250 a night" in ts.FOLLOWUP_HELP_REPLY

    def test_through_the_real_handler_weather_bills_nothing_and_new_dates_re_run(
            self, lane, monkeypatch):
        client = _slack_client()
        _mention(client, MagicMock(), ASK, user=_tessa())
        _drain()
        assert len(lane.calls) == 1
        monkeypatch.setattr(app_module, "_TRAVEL_SHORTLIST_POOL", ThreadPoolExecutor(1))
        client2 = _slack_client()
        _mention(client2, MagicMock(), "in the meantime, what's the weather in phoenix?",
                 user=_tessa(), ts_="1790000000.000300", thread_ts=ASK_TS)
        (call,) = client2.chat_postMessage.call_args_list
        assert call.kwargs["text"] == ts.FOLLOWUP_HELP_REPLY
        assert len(lane.calls) == 1                     # no second (billed) web call
        lane._responses.append(_msg(_fx()))
        client3 = _slack_client()
        _mention(client3, MagicMock(), "can we do oct 20-22?", user=_tessa(),
                 ts_="1790000000.000400", thread_ts=ASK_TS)
        _drain()
        assert _card_call(client3)["thread_ts"] == ASK_TS
        assert len(lane.calls) == 2 and "October 20" in lane.calls[-1]["messages"][0]["content"]

    @pytest.mark.parametrize("shape", [
        " " * 40000, "can you " * 5000, "also " * 8000, "$" * 40000, "1/" * 20000,
        "in the " * 6000, "something more " * 3000, "6 people " * 4000, "weather " * 5000,
        "under " * 7000, "check in oct 1 " * 2500, "arriving " * 5000,
    ], ids=["spaces", "can-you", "also", "dollars", "slashes", "in-the", "something", "people",
            "weather", "under", "check-in", "arriving"])
    def test_the_gate_is_linear(self, shape):
        def run():
            ts._REFINE_RE.search(shape)
            ts._REFINE_START_RE.match(shape)
            ts._OFF_TOPIC_RE.search(shape)
            ts._is_refinement(shape)
            ts._LOCATIVE_BEFORE_FOLLOWUP_RE.search(shape[:40])
        assert _best_of_3(run) < 0.25


# ── r2:c2-trigger#5: the date shorthands parse; P2 never reads a check-out month ──

class TestDateShorthandsRound2:
    @pytest.mark.parametrize("text,ci,co", [
        ("find hotels in scottsdale 10/17-21", date(2026, 10, 17), date(2026, 10, 21)),
        ("find hotels in scottsdale 10/17 to 21", date(2026, 10, 17), date(2026, 10, 21)),
        ("find hotels in scottsdale 10/17 for 2 nights", date(2026, 10, 17), date(2026, 10, 19)),
        ("find hotels in scottsdale 10/17, 2 nights", date(2026, 10, 17), date(2026, 10, 19)),
        ("find hotels in scottsdale oct 17, 2 nights", date(2026, 10, 17), date(2026, 10, 19)),
        ("find hotels in scottsdale 3 nights starting oct 17", date(2026, 10, 17),
         date(2026, 10, 20)),
        ("find hotels in scottsdale for 2 nights from 10/17", date(2026, 10, 17),
         date(2026, 10, 19)),
        ("find hotels in scottsdale oct 17 - 10/21", date(2026, 10, 17), date(2026, 10, 21)),
        ("find hotels in scottsdale 10/17 - oct 21", date(2026, 10, 17), date(2026, 10, 21)),
        # the silently-WRONG stays P2 used to produce (Oct 7-10, Oct 5-10, Oct 3-10)
        ("find hotels in scottsdale oct 7 - 10/21", date(2026, 10, 7), date(2026, 10, 21)),
        ("find hotels in scottsdale oct 5 - 10/7", date(2026, 10, 5), date(2026, 10, 7)),
        ("find hotels in scottsdale oct 3-10/5", date(2026, 10, 3), date(2026, 10, 5)),
        # unchanged neighbours
        ("find hotels in scottsdale oct 17-21, 2026", date(2026, 10, 17), date(2026, 10, 21)),
        ("find hotels in scottsdale 10/17-10/21", date(2026, 10, 17), date(2026, 10, 21)),
        ("find hotels in scottsdale oct 17 for 4 nights", date(2026, 10, 17), date(2026, 10, 21)),
    ])
    def test_formats(self, text, ci, co):
        c = ts.parse_constraints(text, today=TODAY).constraints
        assert c is not None, text
        assert (c.check_in, c.check_out) == (ci, co), text
        r = ts.route_turn(text, user_id=HARRISON, channel_id="D0HARRISON", channel_name="dm",
                          today=TODAY)
        assert r is not None and r.kind == "search", (text, r)

    @pytest.mark.parametrize("text", [
        "find hotels in scottsdale 10/17-10/5",          # a check-out before check-in, 30+ nights
        "find hotels in scottsdale 13/40-21",            # no such day
    ])
    def test_still_malformed(self, text):
        pr = ts.parse_constraints(text, today=TODAY)
        assert pr.constraints is None and pr.malformed is True

    @pytest.mark.parametrize("shape", [
        " " * 40000, "10/1" * 10000, "1/1-" * 10000, "oct 1 - " * 5000,
        "3 nights starting " * 2500, "10/17, " * 6000, "10/17 for " * 4000, "oct 1 - 1" * 4000,
    ], ids=["spaces", "md", "md-dash", "oct-dash", "nights-first", "md-comma", "md-for",
            "mixed"])
    def test_the_new_date_patterns_are_linear(self, shape):
        def run():
            for rx in (ts._DATE_P1B, ts._DATE_P4B, ts._DATE_P7, ts._DATE_P5B, ts._DATE_P5C,
                       ts._DATE_P2, ts._DATE_P5):
                rx.search(shape)
            ts._parse_dates(ts._norm(shape), TODAY)
        assert _best_of_3(run) < 0.25
        assert _best_of_3(lambda: ts.parse_constraints(shape, today=TODAY)) < 0.05


# ── r2:c2-trigger#6: "free cancellation" / "cancellable" is a filter, not a bail ──

class TestCancellationIsAFilter:
    @pytest.mark.parametrize("text", [
        "find hotels in sedona oct 17-21 with free cancellation",
        "can you find a hotel in scottsdale oct 17-21 with free cancellation?",
        "find hotels in scottsdale oct 17-21 with a flexible cancellation policy",
        "find hotels in scottsdale oct 17-21 with free cancelation",
        "find hotels in scottsdale oct 17-21 that are cancellable",
    ])
    def test_must_fire(self, text):
        assert ts.looks_like_travel_ask(text, user_id=HARRISON, channel_id="D0HARRISON",
                                        channel_type="im"), text
        r = ts.route_turn(text, user_id=HARRISON, channel_id="D0HARRISON", channel_name="dm",
                          today=TODAY)
        assert r is not None and r.kind == "search", (text, r)

    @pytest.mark.parametrize("text", [
        "cancel the hotel in scottsdale for oct 17-21",
        "can you find a hotel in scottsdale oct 17-21 and cancel the other one",
        "find hotels in scottsdale oct 17-21, i cancelled the old ones",
        "find hotels in scottsdale oct 17-21, we're canceling the airbnb",
        "can you confirm the hotel in scottsdale oct 17-21",
    ])
    def test_the_verb_still_bails(self, text):
        assert not ts.looks_like_travel_ask(text, user_id=HARRISON, channel_id="D0HARRISON",
                                            channel_type="im"), text

    def test_through_harrisons_dm_the_lane_posts_the_card(self, lane):
        client = _slack_client()
        _dm(client, "find hotels in sedona oct 17-21 with free cancellation", user=HARRISON)
        _drain()
        assert _card_call(client)["thread_ts"] == ASK_TS
        assert "Sedona" in lane.calls[-1]["messages"][0]["content"]
