"""Code #16 C2 -- D-051 round-1 regressions for the travel lane's predicates, parser,
routing and wiring (fixer B1).

Every test drives the REAL code path (the pure predicates on Slack WIRE text, or the
real handle_mention / handle_message_event / _dispatch_qa entry points with only
infrastructure stubbed, via the wiring module's ``lane`` fixture). The guest name is
SYNTHETIC ("Jordan Riverstone"); the loyalty number is a made-up digit run.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import cora.app as app_module
from cora import active_thread_store, web_guard
from cora import travel_shortlist as ts
from cora.model_router import MODEL_SONNET
from test_travel_shortlist import MUST_FIRE, _slack_client
from test_travel_shortlist_wiring import (  # noqa: F401 -- `lane` is a fixture
    ASK, ASK_TS, TRAVEL_CHANNEL, _card_call, _dm, _drain, _drive_dispatch, _mention,
    _model_path, _say_no_placeholder, _tessa, _web_rows, lane,
)

_REPO_ROOT = Path(app_module.__file__).resolve().parents[2]
MEMBER = "U0B3RU5Q55G"          # an F3E member: off the lane's surface


def _wire(text: str) -> str:
    """Slack WIRE form: Slack escapes exactly & < > in event and history text."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _wire_variants(text: str) -> list[str]:
    out = []
    for base in (text, _wire(text)):
        out += [base, base.replace("-", "–"), base.replace("-", "—")]
    return out


def _best_of_3(fn) -> float:
    best = float("inf")
    for _ in range(3):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best


# ── harness-isolation#1: the lane's active-thread register never hits the repo db ─

class TestActiveThreadStoreIsRedirected:
    def test_a_lane_turn_registers_in_the_tmp_store_never_the_repo_db(self, lane, tmp_path):
        repo_db = _REPO_ROOT / "data" / "active_threads.db"
        before = repo_db.stat().st_mtime_ns if repo_db.exists() else None
        # the conftest belt points the module constant at THIS test's tmp dir
        assert Path(active_thread_store._DB_PATH).resolve().parent == tmp_path.resolve()
        _mention(_slack_client(), MagicMock(), ASK, user=_tessa())
        _drain()
        # the row the travel intercept registered landed in the redirected store...
        assert active_thread_store.is_active(TRAVEL_CHANNEL, ASK_TS)
        # ...and the repo's live SQLite store was never touched
        after = repo_db.stat().st_mtime_ns if repo_db.exists() else None
        assert after == before


# ── c2-egress#0/#1, c2-trigger#0, integration#3: the two-tier loose predicate ───

# Strict asks in the shapes the review named (B&B, Air B&B, a dash, a weak strict noun).
EXTRA_STRICT = [
    "can you find a B&B in Sedona Oct 17-21 for me and Jordan Riverstone?",
    "find an Air B&B in Scottsdale Oct 17-21",
    "find b&bs in sedona oct 17-21",
    "find short-term rentals in Scottsdale Oct 17-21",
    "find an inn in sedona",
    ASK,
]

# STRONG terms and WEAK noun + cue: each withholds, in Slack wire form.
LOOSE_MUST_WITHHOLD = [
    "can you find a B&B in Sedona Oct 17-21 for me and Jordan Riverstone?",
    "search the web for a b&b in Sedona for Jordan Riverstone",
    "find an Air B&B in Scottsdale",
    "find short–term rentals",
    "google rooms at the Hilton Scottsdale for Jordan Riverstone, Hilton Honors 123456789",
    "google Marriott Bonvoy availability, member number 987654321",
    "look up Hyatt Regency availability on the web",
    "search the web for corporate rates, our company Hilton account is 482915736",
    "our loyalty account is 482915736", "rewards #482915736", "IHG points account",
    "search the web for _hotels_ for Jordan Riverstone", "*Holiday Inn* Express",
    "a pasted <https://www.airbnb.com/rooms/1|listing>",
    "google a rental house in Scottsdale for Jordan Riverstone",
    "google condos in Scottsdale", "google a casita in Scottsdale", "a lodge near Sedona",
    "google a stay in Scottsdale", "rooms for oct 17-21", "a villa or cabin in sedona",
    "a 4 bedroom house in scottsdale", "a suite for 3 nights", "staying at a resort",
    "an inn this weekend", "rent a house to stay in", "the hotel was great", "HOTELS",
    "any places to stay?", "somewhere to crash", "the Four Seasons",
    "x " * 2000 + "hotel",                     # recall past the 1,000-char predicate cap
]

# A WEAK noun with no cue never withholds (Cora's own prose, office suites, a resort idiom).
LOOSE_MUST_NOT_WITHHOLD = [
    "Full suite green: 19,342 passed.", "as a last resort, restart the bot", "test suite passed",
    "the Suite 200 office lease", "google Adobe Creative Suite pricing", "renew the Adobe suite",
    "The Inn at the Biltmore account ordered 12 cases", "what's our last resort",
    "stay tuned", "our points no longer transfer", "we have 2 maybe", "hot take", "suiteness",
    "what's our cash position?", "google the Deposco API changelog",
    "search online for good restaurants near there that weekend", "", None,
]


class TestLoosePredicateTwoTiers:
    @pytest.mark.parametrize("text", MUST_FIRE + EXTRA_STRICT)
    def test_every_strict_ask_in_wire_form_is_lodging_shaped(self, text):
        """strict is a subset of loose: every ask the lane takes is B1-withheld too."""
        for v in _wire_variants(text):
            assert ts._is_strict_ask(v), v
            assert ts.is_lodging_shaped(v), v

    @pytest.mark.parametrize("text", LOOSE_MUST_WITHHOLD)
    def test_strong_terms_and_cued_weak_nouns_withhold(self, text):
        for v in _wire_variants(text):
            assert ts.is_lodging_shaped(v), v

    @pytest.mark.parametrize("text", LOOSE_MUST_NOT_WITHHOLD)
    def test_uncued_weak_nouns_do_not(self, text):
        assert not ts.is_lodging_shaped(text), text
        if text:
            assert not ts.is_lodging_shaped(_wire(text)), text

    @pytest.mark.parametrize("shape", [
        " " * 40000, "–" * 40000, "_" * 40000, "<" * 40000, "&amp;" * 8000,
        "rooms " * 7000, "stay " * 8000, "room in mesa oct 17 " * 2000, "b & " * 10000,
        "loyalty " * 5000, "<a" * 20000, "in " * 13000, "oct " * 10000,
        " " * 40000 + "hotel",
    ], ids=["spaces", "dashes", "underscores", "lt", "amp", "rooms", "stays", "cued-rooms",
            "b-amp", "loyalty", "lt-a", "in", "oct", "spaces-hotel"])
    def test_the_uncapped_loose_path_is_linear(self, shape):
        """D-165/D-171: every new pattern timed on the whitespace-only 40k input and its
        neighbours -- the loose views are UNCAPPED, so this is the real bound."""
        assert _best_of_3(lambda: ts.is_lodging_shaped(shape)) < 0.25


WEB_ASKS_THAT_MUST_WITHHOLD = [
    "google rooms at the Hilton Scottsdale Oct 17-21 for Jordan Riverstone, Hilton Honors 123456789",
    "google Marriott Bonvoy availability Scottsdale member 987654321",
    "google condos in Scottsdale for Jordan Riverstone",
    "google a rental house in Scottsdale for Jordan Riverstone",
    "search the web for _hotels_ in Scottsdale for Jordan Riverstone",
    "search the web for a b&amp;b in Sedona oct 17-21 for Jordan Riverstone",
    "look up Hyatt Regency Scottsdale availability Oct 17-21 on the web",
    "search the web for Hilton Honors corporate rates, our account is 482915736",
    "search online for a villa or cabin in sedona for Jordan Riverstone",
]


class TestB1RecallOnTheRealDispatch:
    @pytest.mark.parametrize("text", WEB_ASKS_THAT_MUST_WITHHOLD)
    def test_an_off_surface_web_ask_is_withheld(self, lane, text):
        # web_guard WOULD attach (explicit intent) -- only the travel withhold stops it
        assert web_guard.evaluate(text, "F3E", kb_meta={}, model=MODEL_SONNET).attach
        seen = _drive_dispatch(text, user=MEMBER, channel_id="C0F3ESALES",
                               channel_name="f3e-sales", entity="F3E")
        assert seen and all(kw.get("web_tools") is False for kw in seen)
        assert any(r.get("reason") == "gate_skipped:travel_lane" for r in _web_rows())

    def test_a_wire_form_b_and_b_ask_in_tessas_dm_window_withholds_a_later_web_turn(self, lane):
        """The live DM window holds only her raw ask (the ack and card are threaded)."""
        prior = [{"role": "user", "content": "can you find a B&amp;B in Sedona Oct 17-21 for me "
                                             "and Jordan Riverstone? our rewards number is 123456789"}]
        later = "search online for good restaurants near there that weekend"
        assert web_guard.evaluate(later, "HJRG", kb_meta={}, model=MODEL_SONNET).attach
        seen = _drive_dispatch(later, user=_tessa(), channel_id="D0TESSA", channel_name="dm",
                               entity="HJRG", prior=prior)
        assert seen and seen[-1].get("web_tools") is False
        assert any(r.get("reason") == "gate_skipped:travel_lane" for r in _web_rows())


# ── integration#2: the prior-turn leg reads the PERSON's turns only; both skips named ─

HARRISON = "U0B2RM2JYJ1"


class TestPriorTurnLegAndSkipLabel:
    @pytest.mark.parametrize("user,channel,entity", [
        (HARRISON, "D0HARRISON", "FNDR"),     # the founder DM (a custodian surface)
        ("tessa", "D0TESSA", "HJRG"),         # Tessa's DM (non-custodian)
    ])
    def test_coras_own_lodging_words_in_the_window_do_not_withhold(self, lane, user, channel,
                                                                    entity):
        """Cora's replies ("full suite green", a hotel list she posted) are the bot's
        prose, not a person's PII-bearing ask -- an unrelated explicit web ask attaches."""
        user = _tessa() if user == "tessa" else user
        prior = [{"role": "user", "content": "what did the test run say?"},
                 {"role": "assistant", "content": "Full suite green: 19,342 passed. Earlier I "
                                                  "listed three hotels in Scottsdale for Oct 17-21."}]
        seen = _drive_dispatch("google the Deposco API changelog", user=user, channel_id=channel,
                               channel_name="dm", entity=entity, prior=prior)
        assert seen and seen[-1].get("web_tools") is True

    def test_a_persons_lodging_ask_in_the_window_still_withholds(self, lane):
        prior = [{"role": "user", "content": "rooms in scottsdale oct 17-21 for Jordan Riverstone"}]
        seen = _drive_dispatch("google the Deposco API changelog", user=_tessa(),
                               channel_id="D0TESSA", channel_name="dm", entity="HJRG", prior=prior)
        assert seen and seen[-1].get("web_tools") is False

    def test_a_custodian_turn_skipped_by_both_names_travel_lane_too(self, lane):
        """Harrison's DM is phi_custodian; the travel withhold forces web_clean False, so
        the phi_custodian leg fires FIRST -- the ledger must still name travel_lane."""
        seen = _drive_dispatch("google hotels in scottsdale oct 17-21", user=HARRISON,
                               channel_id="D0HARRISON", channel_name="dm", entity="FNDR")
        assert seen and seen[-1].get("web_tools") is False
        reasons = [r.get("reason") for r in _web_rows()]
        assert "gate_skipped:phi_custodian+travel_lane" in reasons, reasons


# ── c2-trigger#1/#4/#7: the frame governs the noun; bails scoped; two polite layers ─

# The review's stolen turns: the lodging noun sits in a prepositional phrase or is a
# definite reference, so the frame governs something else.
GOVERNING_MUST_NOT_FIRE = [
    "can you recommend a restaurant near the hotel in scottsdale for oct 17-21",
    "any ideas for team dinner near the hotel in scottsdale oct 17-21?",
    "suggest a coffee shop by the hotel in tempe oct 17-21",
    "i need the address of the hotel in scottsdale",
    "get the hotel address in scottsdale",
    "find out which hotel we're using for the scottsdale offsite",
    "i need the hotel wifi password",
    "i want to know which hotel tessa picked",
    "find parking near the hotel in scottsdale oct 17-21",
    "recommend a gym by our hotel in mesa oct 17-21",
]
# Bail scopes: booking/admin words bail in the FIRST clause; capability words anywhere.
SCOPED_MUST_NOT_FIRE = [
    "find a hotel in Scottsdale Oct 17-21 and book it",
    "find hotels in scottsdale oct 17-21. add the best one to my calendar",
    "can you find hotels in scottsdale oct 17-21? remember we like the pool",
    "find my hotel confirmation",
]
# Realistic asks that must reach the lane (price words, a trailing sentence, stacked
# politeness, "pull", governed nouns with closed determiners/adjectives).
NEW_MUST_FIRE = [
    "find a hotel in scottsdale oct 17-21, nothing too expensive",
    "can you find hotels in scottsdale oct 17-21? not too expensive please",
    "can you find hotels in scottsdale for oct 17-21? dates are confirmed",
    "can you help me find hotels in scottsdale oct 17-21",
    "Hi! Can you help me look for hotels or Airbnbs in the Mesa/Gilbert/Scottsdale area "
    "for Oct 17-21?",
    "can you pull hotel options in scottsdale for oct 17-21",
    "could you please help us find a hotel in tempe oct 17-21",
    "pull up hotels in mesa oct 17-21",
    "i need to find a hotel in scottsdale oct 17-21",
    "find the best hotels in scottsdale oct 17-21",
    "find a nice, quiet hotel in sedona oct 17-21",
    "find a few good hotels in phoenix oct 17-21",
    "find a 4-star hotel in phoenix oct 17-21",
    "can you find a pet friendly hotel in mesa oct 17-21",
    "we're trying to find an airbnb in sedona oct 17-21",
    "find an Air B&B in Scottsdale Oct 17-21",
]


class TestStrictFrame:
    @pytest.mark.parametrize("text", NEW_MUST_FIRE)
    def test_must_fire(self, text):
        for v in _wire_variants(text):
            assert ts.looks_like_travel_ask(v, user_id=HARRISON, channel_id="D0HARRISON",
                                            channel_type="im"), v
            assert ts.looks_like_travel_ask(v, user_id="U_ANYONE", channel_id=TRAVEL_CHANNEL), v

    @pytest.mark.parametrize("text", GOVERNING_MUST_NOT_FIRE + SCOPED_MUST_NOT_FIRE)
    def test_must_not_fire(self, text):
        assert not ts.looks_like_travel_ask(text, user_id=HARRISON, channel_id="D0HARRISON",
                                            channel_type="im")
        assert not ts.looks_like_travel_ask(text, user_id="U_ANYONE", channel_id=TRAVEL_CHANNEL)

    @pytest.mark.parametrize("shape", [
        "can you " * 5000, "find " + "a " * 20000 + "hotel", "find " + "nice, and " * 4000 + "x",
        "help me " * 5000 + "find hotels", "find the top " + "9 " * 13000,
    ], ids=["polite-x5000", "det-x20000", "adj-x4000", "help-x5000", "top-x13000"])
    def test_the_frame_regexes_are_linear_even_uncapped(self, shape):
        def run():
            ts._FRAME_RE.match(shape)
            ts._NOUN_OPTIONS_RE.match(shape)
            ts._PLACES_RE.match(shape)
            ts._BAIL_RE.search(shape)
            ts._CAPABILITY_BAIL_RE.search(shape)
            ts.looks_like_travel_ask(shape, user_id=HARRISON, channel_id="D0H", channel_type="im")
        assert _best_of_3(run) < 0.05


class TestStrictFrameThroughRealHandlers:
    @pytest.mark.parametrize("text", GOVERNING_MUST_NOT_FIRE)
    def test_the_lane_never_takes_a_governed_elsewhere_turn(self, lane, monkeypatch, text):
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

    @pytest.mark.parametrize("text", NEW_MUST_FIRE)
    def test_harrisons_dm_runs_the_lane(self, lane, text):
        client = _slack_client()
        _dm(client, _wire(text), user=HARRISON)
        _drain()
        assert _card_call(client)["thread_ts"] == ASK_TS


# ── integration#0 + c2-trigger#3: a lane thread is one the LANE created; 48 h bound ─

PANE_ROOT = "1790000000.000050"      # an existing thread (a pane chat / a channel thread)
IN_THREAD_ASK_TS = "1790000000.000300"


def _now():
    return datetime.now(timezone(timedelta(hours=-7)))


def _constraints():
    return ts.parse_constraints(ASK).constraints


class TestLaneThreadRegistration:
    def _exec(self, *, root, ask_ts, followup=False):
        route = ts.Route("search", constraints=_constraints(), budget=4, followup=followup)
        client = _slack_client()
        ts.execute_route(route, channel_id=TRAVEL_CHANNEL, thread_root_ts=root, entity="FNDR",
                         user_id=HARRISON, client=client, say=MagicMock(),
                         submit=lambda *a, **k: True, ask_ts=ask_ts)
        return client

    def test_a_top_level_ask_registers_its_own_thread(self):
        self._exec(root=ASK_TS, ask_ts=ASK_TS)
        assert ts.is_lane_thread(TRAVEL_CHANNEL, ASK_TS)
        assert ts.latest_constraints(TRAVEL_CHANNEL, ASK_TS) == _constraints()

    def test_an_ask_inside_an_existing_thread_posts_there_but_never_registers_it(self):
        client = self._exec(root=PANE_ROOT, ask_ts=IN_THREAD_ASK_TS)
        assert client.chat_postMessage.call_args.kwargs["thread_ts"] == PANE_ROOT
        # the card's own "posted" row (written by the pooled job) does not register it either
        ts.append_event("posted", channel=TRAVEL_CHANNEL, root_ts=PANE_ROOT, options=5)
        assert not ts.is_lane_thread(TRAVEL_CHANNEL, PANE_ROOT)
        assert ts.latest_constraints(TRAVEL_CHANNEL, PANE_ROOT) is None
        # ...while the monitor still counts the ask
        assert ts.threads_summary()["asks"] == 1

    def test_a_follow_up_in_a_lane_thread_keeps_it_registered(self):
        self._exec(root=ASK_TS, ask_ts=ASK_TS)
        self._exec(root=ASK_TS, ask_ts=IN_THREAD_ASK_TS, followup=True)
        assert ts.is_lane_thread(TRAVEL_CHANNEL, ASK_TS)

    def test_no_root_means_the_ack_is_the_lanes_own_thread(self):
        client = self._exec(root=None, ask_ts=None)          # /cora-ask
        ack_ts = client.chat_postMessage.call_args.kwargs.get("thread_ts") or "1790000000.000001"
        assert ts.is_lane_thread(TRAVEL_CHANNEL, ack_ts)

    def test_the_lane_thread_expires_48_hours_after_its_last_asked_or_posted_row(self):
        c = _constraints().to_record()
        t0 = _now() - timedelta(hours=60)
        ts.append_event("asked", channel=TRAVEL_CHANNEL, root_ts="9.1", constraints=c, now=t0)
        assert not ts.is_lane_thread(TRAVEL_CHANNEL, "9.1")
        assert ts.latest_constraints(TRAVEL_CHANNEL, "9.1") is None
        # a card posted 13 h later refreshes the clock...
        ts.append_event("posted", channel=TRAVEL_CHANNEL, root_ts="9.1", options=5,
                        now=t0 + timedelta(hours=13))
        assert ts.is_lane_thread(TRAVEL_CHANNEL, "9.1")
        assert ts.latest_constraints(TRAVEL_CHANNEL, "9.1") == _constraints()
        # ...and the bound is read against the caller's clock
        assert not ts.is_lane_thread(TRAVEL_CHANNEL, "9.1", now=_now() + timedelta(hours=2))


class TestLaneThreadScopeThroughRealHandlers:
    def test_an_ask_inside_a_dm_pane_chat_does_not_hijack_the_chat(self, lane):
        """Agents & AI Apps pane: every message in one chat carries the chat root as
        thread_ts. The ask's card posts in the chat, but the chat is NOT a lane thread
        -- a later ordinary question reaches the model, never the help line."""
        client = _slack_client()
        _dm(client, ASK, user=HARRISON, ts_=IN_THREAD_ASK_TS, thread_ts=PANE_ROOT)
        _drain()
        assert _card_call(client)["thread_ts"] == PANE_ROOT
        assert not ts.is_lane_thread("D0TRAVELDM", PANE_ROOT)
        seen: list = []
        client2 = _slack_client()
        client2.chat_postMessage.side_effect = None
        client2.chat_postMessage.return_value = {"ok": True}
        with _model_path(seen):
            _dm(client2, "what's our cash position this week?", user=HARRISON,
                ts_="1790000000.000400", thread_ts=PANE_ROOT)
        texts = [c.kwargs.get("text") for c in client2.chat_postMessage.call_args_list]
        assert ts.FOLLOWUP_HELP_REPLY not in texts
        assert seen                                   # the ordinary pipeline answered

    def test_an_in_thread_channel_mention_ask_does_not_register_the_thread(self, lane):
        client = _slack_client()
        _mention(client, MagicMock(), ASK, user=_tessa(), ts_=IN_THREAD_ASK_TS,
                 thread_ts=PANE_ROOT)
        _drain()
        assert _card_call(client)["thread_ts"] == PANE_ROOT
        assert not ts.is_lane_thread(TRAVEL_CHANNEL, PANE_ROOT)

    def test_a_top_level_dm_ask_registers_its_thread(self, lane):
        client = _slack_client()
        _dm(client, ASK, user=HARRISON)
        _drain()
        assert ts.is_lane_thread("D0TRAVELDM", ASK_TS)
