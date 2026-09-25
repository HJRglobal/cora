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
