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
