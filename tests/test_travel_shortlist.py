"""Code #16 C2 -- the travel shortlist lane (cora.travel_shortlist), unit level.

Kickoff 2026-09-21 section 3 (binding) for C2: constraint parsing (dates, area,
party size, budget, bed type) . outbound request payloads contain no names /
emails / loyalty identifiers (asserted on the ACTUAL request builder and on the
create kwargs of EVERY iteration) . <= 5 options . the "as of" stamp . malformed
dates handled. Plus the binding review amendments B5-B9: the strict predicate and
its bails, the allowlist belt (clean passes, tampered refuses), constants that are
identity-free, the web-only call's clamping / accounting, the record-URL parse
over live-shape SDK objects, the field sanitizer, the constant text= fallback and
the thread store.

Every Anthropic fake is built from tests/fixtures/travel_shortlist_web_search_basic.json
-- ONE live, synthetic, identity-free web_search_20250305 response recorded
2026-09-25 (encrypted fields blanked) -- via anthropic.types.Message.model_validate,
so the parser is pinned to the real SDK shapes, never to hand-rolled dicts.
The guest name used below is SYNTHETIC ("Jordan Riverstone"); no real ask text or
real guest name appears anywhere in this file.
"""

from __future__ import annotations

import copy
import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import anthropic
import pytest
from slack_sdk.web.slack_response import SlackResponse

from cora import org_roles, slack_egress, web_guard
from cora import travel_shortlist as ts

TODAY = date(2026, 9, 25)
NOW = datetime(2026, 9, 25, 9, 14, tzinfo=timezone(timedelta(hours=-7)))
MODEL = "claude-sonnet-5"
TRAVEL_CHANNEL = "C0C145H84JZ"
HARRISON = "U0B2RM2JYJ1"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "travel_shortlist_web_search_basic.json"

# A SYNTHETIC ask carrying every identity class the lane must keep off the web.
PII_ASK = (
    "can you search for hotels and airbnbs in the mesa/gilbert/scottsdale area for "
    "october 17th-october 21 for Jordan Riverstone (jordan.riverstone@example.com, "
    "480-555-0147) and 3 others, about 4 people, $300-$400/night, primary suite with a "
    "king bed, modern and camera-friendly -- use our Hilton Honors account 123456789"
)
PII_TOKENS = ("Jordan", "Riverstone", "jordan.riverstone@example.com", "example.com",
              "480-555-0147", "0147", "555", "Hilton", "Honors", "123456789", "account",
              "loyalty", "member")


# ── fixtures / fakes ─────────────────────────────────────────────────────────

def _fx() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _msg(d: dict) -> anthropic.types.Message:
    return anthropic.types.Message.model_validate(d)


def _fence_index(d: dict) -> int:
    return next(i for i, b in enumerate(d["content"]) if b["type"] == "text" and "```json" in b["text"])


def _mutate_options(d: dict, fn) -> dict:
    """Edit the options inside the fixture's ```json fence (the block that holds it)."""
    d = copy.deepcopy(d)
    i = _fence_index(d)
    text = d["content"][i]["text"]
    a = text.index("```json") + len("```json")
    b = text.index("```", a)
    opts = json.loads(text[a:b])
    opts = fn(opts)
    d["content"][i]["text"] = text[:a] + "\n" + json.dumps(opts, indent=2) + "\n" + text[b:]
    return d


def _split_pause(d: dict) -> tuple[dict, dict]:
    """The live response split into a pause_turn first half (2 searches) and a
    final second half (2 searches) -- the shape a server-tool pause produces."""
    d1, d2 = copy.deepcopy(d), copy.deepcopy(d)
    d1["content"] = d["content"][:5]
    d1["stop_reason"] = "pause_turn"
    d1["usage"]["server_tool_use"]["web_search_requests"] = 2
    d2["content"] = d["content"][5:]
    d2["usage"]["server_tool_use"]["web_search_requests"] = 2
    return d1, d2


class _Stream:
    """A completed stream: iterable (one event per content block, the way run_search
    walks a stream against its wall-clock deadline), closable, snapshot-readable."""

    def __init__(self, resp):
        self._resp = resp
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def __iter__(self):
        for block in getattr(self._resp, "content", None) or []:
            yield block

    def close(self):
        self.closed = True

    @property
    def current_message_snapshot(self):
        return self._resp

    def get_final_message(self):
        return self._resp


class FakeAnthropic:
    """messages.stream(**kw) records a DEEP COPY of every create's kwargs."""

    def __init__(self, responses):
        self.calls: list[dict] = []
        self._responses = list(responses)
        outer = self

        class _Messages:
            def stream(self, **kw):
                outer.calls.append(copy.deepcopy(kw))
                r = outer._responses.pop(0)
                if isinstance(r, BaseException):
                    raise r
                return _Stream(r)

        self.messages = _Messages()


def _sse_events(d: dict, *, blocks: int | None = None, finish: bool = True) -> list:
    """The recorded message replayed as the API's own stream events (public SDK event
    types, model_validate'd): message_start, a start/stop per content block (the first
    ``blocks`` only), then message_delta + message_stop when ``finish``."""
    start = copy.deepcopy(d)
    start["content"], start["stop_reason"] = [], None
    start["usage"] = dict(d["usage"], output_tokens=1,
                          server_tool_use={"web_search_requests": 0, "web_fetch_requests": 0})
    evs = [anthropic.types.RawMessageStartEvent.model_validate({"type": "message_start", "message": start})]
    for i, b in enumerate(d["content"][:blocks]):
        evs.append(anthropic.types.RawContentBlockStartEvent.model_validate(
            {"type": "content_block_start", "index": i, "content_block": b}))
        evs.append(anthropic.types.RawContentBlockStopEvent.model_validate(
            {"type": "content_block_stop", "index": i}))
    if finish:
        evs.append(anthropic.types.RawMessageDeltaEvent.model_validate(
            {"type": "message_delta", "delta": {"stop_reason": d["stop_reason"], "stop_sequence": None},
             "usage": {"output_tokens": d["usage"]["output_tokens"],
                       "server_tool_use": d["usage"]["server_tool_use"]}}))
        evs.append(anthropic.types.RawMessageStopEvent.model_validate({"type": "message_stop"}))
    return evs


class _RawSSE:
    """What the SDK's MessageStream iterates (its raw Stream): events, then an optional
    exception mid-stream (a ReadTimeout / an overloaded error after searches ran);
    ``pace`` seconds of real wall-clock between events (a trickling stream)."""

    def __init__(self, events, exc=None, pace=0.0):
        self.events, self.exc, self.pace = list(events), exc, pace
        self.closed = False
        self.yielded = 0

    def __iter__(self):
        for ev in self.events:
            if self.closed:
                return
            if self.pace:
                time.sleep(self.pace)
            self.yielded += 1
            yield ev
        if self.exc is not None:
            raise self.exc

    def close(self):
        self.closed = True


class SdkStreamAnthropic:
    """messages.stream(**kw) -> the REAL anthropic MessageStreamManager over a _RawSSE,
    so run_search reads the SDK's own accumulation (current_message_snapshot, close,
    get_final_message) -- never a hand-rolled stream."""

    def __init__(self, raws):
        self.calls: list[dict] = []
        self.raws = list(raws)
        outer = self

        class _Messages:
            def stream(self, **kw):
                outer.calls.append(copy.deepcopy(kw))
                raw = outer.raws.pop(0)
                from anthropic.lib.streaming import MessageStreamManager

                def _request():            # the SDK sends the request on __enter__
                    if isinstance(raw, BaseException):
                        raise raw
                    return raw
                return MessageStreamManager(_request, output_format=anthropic.NOT_GIVEN)

        self.messages = _Messages()


def _slack_resp(data: dict) -> SlackResponse:
    """A REAL non-dict slack_sdk response (lesson 68: a dict fake hides the seam)."""
    return SlackResponse(client=None, http_verb="POST", api_url="https://slack.test/api",
                         req_args={}, data=data, headers={}, status_code=200)


def _slack_client():
    client = MagicMock()
    counter = {"n": 0}

    def _post(**kw):
        counter["n"] += 1
        return _slack_resp({"ok": True, "channel": kw.get("channel"), "ts": f"1790000000.00{counter['n']:04d}"})

    client.chat_postMessage.side_effect = _post
    return client


def _constraints(**over) -> ts.TravelConstraints:
    base = dict(check_in=date(2026, 10, 17), check_out=date(2026, 10, 21),
                areas=("scottsdale",), party_size=4, budget_min=300, budget_max=400,
                beds="king", bedrooms=None, kind="both", styles=("modern", "photogenic"))
    base.update(over)
    return ts.TravelConstraints(**base)


def _rows() -> list[dict]:
    p = ts.threads_path()
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def _best_of_3(fn) -> float:
    best = float("inf")
    for _ in range(3):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best


# ── the fixture itself ───────────────────────────────────────────────────────

class TestLiveShapeFixture:
    def test_fixture_validates_as_an_sdk_message_and_is_identity_free(self):
        m = _msg(_fx())
        assert m.stop_reason == "end_turn" and m.usage.server_tool_use.web_search_requests == 4
        raw = FIXTURE.read_text(encoding="utf-8").lower()
        for tok in ("hilton", "honors", "riverstone", "account", "loyalty"):
            assert tok not in raw

    def test_record_urls_cover_every_option_and_citations(self):
        m = _msg(_fx())
        urls, errors = ts.collect_record_urls([m])
        assert errors == ()
        opts = ts.extract_json_options(ts.final_text(m))
        assert len(opts) == 5
        assert all(ts.normalize_url(o["url"]) in urls for o in opts)
        # a citation-only URL still counts (text-block citations are records too)
        assert ts.normalize_url(
            "https://www.airbnb.com/old-town-scottsdale-az/stays/condos") in urls


# ── predicates (B5) ──────────────────────────────────────────────────────────

MUST_FIRE = [
    "can you search for hotels and airbnbs in the Scottsdale area for Oct 17-21",
    "find hotels in Scottsdale Oct 17-21 for 4 guests",
    "Hey Cora! Can you find a hotel in Tempe 10/17-10/21?",
    "looking for an airbnb in sedona dec 30 - jan 2",
    "I need a hotel in Phoenix Oct 17-21",
    "places to stay in Mesa oct 17 to oct 21",
    "hotel options in Gilbert for oct 17-21",
    "please find us some vacation rentals near Scottsdale oct 17-21",
    "could you recommend a few hotels in Chandler for October 17-21?",
    "we need lodging in Flagstaff Oct 17-21",
    "search for vrbos in the scottsdale area oct 17-21",
    "find hotels in scottsdale",           # frame + noun: the lane answers (clarify) -- never the model
]

# The review's hijack examples, VERBATIM (design-review.md, lens 3, finding 4).
REVIEW_MUST_NOT_FIRE = [
    "what's our last resort if the Oct 17 pallet misses?",
    "how much did we spend on equipment rentals in Scottsdale in September?",
    "renew the Adobe suite",
    "the hotel was great",
    "Tessa booked the Scottsdale hotel for Oct 17-21, add it to my calendar",
    "remember the Scottsdale hotel for Oct 17-21 is the Hilton",
    "pull up my emails about the Scottsdale hotel for Oct 17-21",
    "have a code session build hotel filters for Scottsdale Oct 17-21",
]
MORE_MUST_NOT_FIRE = [
    "find a resort in scottsdale oct 17-21",          # resort alone never triggers
    "find a king suite in scottsdale oct 17-21",       # suite alone never triggers
    "find rentals in scottsdale oct 17-21",            # bare rental never triggers
    "did you find hotels in Scottsdale for Oct 17-21?",
    "I think we should find a hotel in scottsdale eventually",
    "find the hotel receipt from Scottsdale Oct 17-21",
    "find a hotel in Scottsdale Oct 17-21 and book it",
    "cancel the hotel in scottsdale for oct 17-21",
    "can you confirm the hotel in scottsdale oct 17-21",
    "find a hotel in scottsdale oct 17-21 and create a task for Tessa",
    "email me hotels in scottsdale oct 17-21",
    "google hotels in scottsdale oct 17-21",          # no ask-for-options frame
]


class TestPredicates:
    @pytest.mark.parametrize("text", MUST_FIRE)
    def test_must_fire(self, text):
        assert ts.looks_like_travel_ask(text, user_id=HARRISON, channel_id="D0HARRISON",
                                        channel_type="im")
        assert ts.looks_like_travel_ask(text, user_id="U_ANYONE", channel_id=TRAVEL_CHANNEL)

    @pytest.mark.parametrize("text", REVIEW_MUST_NOT_FIRE + MORE_MUST_NOT_FIRE)
    def test_must_not_fire(self, text):
        assert not ts.looks_like_travel_ask(text, user_id=HARRISON, channel_id="D0HARRISON",
                                            channel_type="im")
        assert not ts.looks_like_travel_ask(text, user_id="U_ANYONE", channel_id=TRAVEL_CHANNEL)

    def test_surface_is_checked_first_and_runs_no_regex_off_surface(self, monkeypatch):
        called = []
        monkeypatch.setattr(ts, "_is_strict_ask", lambda t: called.append(t) or True)
        # a member's DM, and a channel that is not the travel channel
        assert not ts.looks_like_travel_ask(MUST_FIRE[1], user_id="U_MEMBER", channel_id="D0X",
                                            channel_type="im")
        assert not ts.looks_like_travel_ask(MUST_FIRE[1], user_id=HARRISON, channel_id="C0OTHER")
        assert called == []

    def test_tessa_dm_surface_resolves_through_the_roster(self):
        rec = org_roles.find_by_handle("tessa")
        assert rec is not None and rec.slack_id
        assert ts.looks_like_travel_ask(MUST_FIRE[1], user_id=rec.slack_id, channel_id="D0T",
                                        channel_type="im")

    def test_an_unresolved_handle_logs_a_warning_and_has_no_surface(self, monkeypatch, caplog):
        monkeypatch.setattr(org_roles, "find_by_handle", lambda h: None)
        monkeypatch.setattr(ts, "_warned_handles", set())
        monkeypatch.setattr(ts, "_handle_cache", None)
        caplog.set_level(logging.WARNING, logger="cora.travel_shortlist")
        assert not ts.on_surface(user_id="U_SOMEONE", channel_id="D0T", channel_type="im")
        assert any("did not resolve" in r.getMessage() for r in caplog.records)
        assert ts.on_surface(user_id=HARRISON, channel_id="D0H", channel_type="im")

    def test_the_handle_resolution_is_cached_and_keyed(self, monkeypatch):
        calls = []
        rec = org_roles.find_by_handle("tessa")
        monkeypatch.setattr(ts, "_handle_cache", None)
        monkeypatch.setattr(org_roles, "find_by_handle", lambda h: calls.append(h) or rec)
        lm = ts._load_map()
        assert ts._dm_user_ids(lm) == ts._dm_user_ids(lm) == frozenset({HARRISON, rec.slack_id})
        assert calls == ["tessa"]
        monkeypatch.setenv("HARRISON_SLACK_USER_ID", "U0OTHERFOUNDER")   # a new key re-resolves
        assert "U0OTHERFOUNDER" in ts._dm_user_ids(lm) and calls == ["tessa", "tessa"]

    @pytest.mark.parametrize("flag", ["retrieval_grant", "pending_write", "forced_tool"])
    def test_turn_level_bails(self, flag):
        assert not ts.looks_like_travel_ask(MUST_FIRE[1], user_id=HARRISON, channel_id="D0H",
                                            channel_type="im", **{flag: True})

    def test_loose_lodging_shape_is_recall_biased(self):
        for t in ("the hotel was great", "google hotels in scottsdale", "any places to stay?",
                  "Holiday Inn Express", "an air bnb", "short-term rentals", "HOTELS",
                  "a suite in scottsdale", "the resort for oct 17-21"):
            assert ts.is_lodging_shaped(t), t
        # DELIBERATE FLIP (D-051 r1 integration#2 / the two-tier ruling): a WEAK noun
        # (suite / resort / room / inn ...) withholds only with a cue -- a date, an
        # allowlisted area or a stay verb -- so "renew the Adobe suite" and "what's our
        # last resort" no longer black out web. Full tables: test_travel_shortlist_d051_r1.
        for t in ("what's our cash position?", "hot take", "suiteness", "", None,
                  "renew the Adobe suite", "what's our last resort"):
            assert not ts.is_lodging_shaped(t), t

    @pytest.mark.parametrize("shape", [
        " " * 40000,
        "hotel" + " " * 40000 + "x",
        "find hotels" + " " * 40000 + "x",
        "find" + " " * 40000 + "hotels in scottsdale oct 17-21",
        "oct 17" + " " * 40000 + "-21",
        "$300" + " " * 40000 + "/night",
        "in " * 13000,
        "10/" * 13000,
        "<" * 40000,
        "hotels/" * 5000,
    ], ids=["spaces", "hotel-spaces-x", "find-hotels-spaces", "find-spaces-ask", "date-spaces",
            "money-spaces", "in-x13000", "slash-x13000", "lt-x40000", "hotels-slash-x5000"])
    def test_every_regex_is_linear_on_degenerate_input(self, shape):
        """D-165/D-171: every new pattern timed on the whitespace-only 40k input
        (and neighbours). Budget 50 ms, best of 3."""
        def run():
            ts.is_lodging_shaped(shape)
            ts.looks_like_travel_ask(shape, user_id=HARRISON, channel_id="D0H", channel_type="im")
            ts.parse_constraints(shape, today=TODAY)
            ts.sanitize_field(shape, 160)
            ts.merge_followup(_constraints(), shape, today=TODAY)
            ts._PASSTHROUGH_RE.search(ts._norm(ts._clean(shape)))
            for rx in (ts._LODGING_STRONG_RE, ts._LODGING_WEAK_RE, ts._STAY_CUE_RE,
                       ts._DATE_CUE_RE):
                rx.search(shape)
        assert _best_of_3(run) < 0.05


# ── the parser (kickoff section 3: dates, area, party size, budget, bed type) ─

class TestDates:
    @pytest.mark.parametrize("text,ci,co", [
        ("hotels in scottsdale october 17th-october 21", date(2026, 10, 17), date(2026, 10, 21)),
        ("hotels in scottsdale Oct 17-21", date(2026, 10, 17), date(2026, 10, 21)),
        ("hotels in scottsdale Oct 17–21", date(2026, 10, 17), date(2026, 10, 21)),
        ("hotels in scottsdale 10/17-10/21", date(2026, 10, 17), date(2026, 10, 21)),
        ("hotels in scottsdale Oct 17 to Oct 21", date(2026, 10, 17), date(2026, 10, 21)),
        ("hotels in scottsdale October 17 - 21, 2026", date(2026, 10, 17), date(2026, 10, 21)),
        ("hotels in scottsdale 17-21 October", date(2026, 10, 17), date(2026, 10, 21)),
        ("hotels in scottsdale 10/17/2026 - 10/21/2026", date(2026, 10, 17), date(2026, 10, 21)),
        ("hotels in scottsdale oct 30 - nov 2", date(2026, 10, 30), date(2026, 11, 2)),
        ("hotels in scottsdale Oct 17 for 3 nights", date(2026, 10, 17), date(2026, 10, 20)),
        # a check-in on day 28+ (a first cut read these as malformed)
        ("hotels in scottsdale Oct 30 for 3 nights", date(2026, 10, 30), date(2026, 11, 2)),
        ("hotels in sedona Dec 30 for five nights", date(2026, 12, 30), date(2027, 1, 4)),
        ("hotels in scottsdale oct 17 through the 21st", date(2026, 10, 17), date(2026, 10, 21)),
        # no year -> the next occurrence on/after today; December rolls into January
        ("hotels in sedona dec 30 - jan 2", date(2026, 12, 30), date(2027, 1, 2)),
        ("hotels in sedona march 3-5", date(2027, 3, 3), date(2027, 3, 5)),
    ])
    def test_formats(self, text, ci, co):
        c = ts.parse_constraints(text, today=TODAY).constraints
        assert c is not None, text
        assert (c.check_in, c.check_out) == (ci, co)

    @pytest.mark.parametrize("text", [
        "hotels in scottsdale oct 17-17",          # 0 nights
        "hotels in scottsdale oct 1 - nov 15",     # > 30 nights
        "hotels in scottsdale oct 17-21, 2025",    # a past stay
        "hotels in scottsdale feb 30 - mar 2",     # no such day
        "hotels in scottsdale 13/40-13/42",        # no such month/day
        "hotels in scottsdale oct 17 for 45 nights",
    ])
    def test_malformed_dates_are_named_malformed_not_missing(self, text):
        pr = ts.parse_constraints(text, today=TODAY)
        assert pr.constraints is None and pr.malformed is True and "dates" in pr.missing

    @pytest.mark.parametrize("text", ["hotels in scottsdale next weekend",
                                      "hotels in scottsdale Oct 17", "hotels in scottsdale"])
    def test_missing_dates(self, text):
        pr = ts.parse_constraints(text, today=TODAY)
        assert pr.constraints is None and pr.missing == ("dates",) and pr.malformed is False


class TestAreas:
    @pytest.mark.parametrize("text,areas", [
        ("hotels in the mesa/gilbert/scottsdale area oct 17-21", ("mesa", "gilbert", "scottsdale")),
        ("hotels in Mesa, Gilbert or Scottsdale oct 17-21", ("mesa", "gilbert", "scottsdale")),
        ("hotels near downtown phoenix oct 17-21", ("phoenix",)),
        ("hotels around old town scottsdale oct 17-21", ("scottsdale",)),
        ("hotels in the Scottsdale area oct 17-21", ("scottsdale",)),
        ("hotels in st. louis oct 17-21", ("st-louis",)),
        ("hotels in queen creek oct 17-21", ("queen-creek",)),
        ("hotels in Page oct 17-21", ("page-az",)),
        ("hotels in phoenix, tempe, mesa, chandler oct 17-21", ("phoenix", "tempe", "mesa")),
    ])
    def test_locative_areas(self, text, areas):
        assert ts.parse_constraints(text, today=TODAY).constraints.areas == areas

    @pytest.mark.parametrize("text", [
        "find a hotel near Gilbert's place oct 17-21",   # possessive: a person, not a place
        "find a hotel for Chandler oct 17-21",           # not locative
        "find hotels, surprise me, oct 17-21",           # not locative
        "find a hotel in the tempest oct 17-21",         # a longer word is never a place
        "find a hotel in Boise oct 17-21",               # not on the allowlist
        "find a hotel near Jordan's gym oct 17-21",
    ])
    def test_non_areas_ask_for_a_city(self, text):
        pr = ts.parse_constraints(text, today=TODAY)
        assert pr.constraints is None and "area" in pr.missing

    def test_try_x_instead_in_a_follow_up(self):
        f = ts.parse_fields("try Tempe instead", today=TODAY)
        assert f["areas"] == ("tempe",)


class TestFields:
    @pytest.mark.parametrize("text,party", [
        ("about 4 people", 4), ("4 guests", 4), ("party of 4", 4), ("for 4 people", 4),
        ("4 adults", 4), ("four people", 4), ("~4 people", 4), ("group of twelve", 12),
        ("for 4 nights", None), ("25 people", None), ("4 rooms", None),
    ])
    def test_party_size(self, text, party):
        assert ts.parse_fields(f"hotels in mesa oct 17-21 {text}", today=TODAY)["party_size"] == party

    @pytest.mark.parametrize("text,lo,hi", [
        ("$300-$400/night", 300, 400), ("$300 to $400 per night", 300, 400),
        ("under $400/night", None, 400), ("around $350 a night", 350, 350),
        ("300-400 a night", 300, 400), ("at least $250/night", 250, None),
        ("$400 - $300 per night", 300, 400), ("$1,200 nightly", 1200, 1200),
        ("$1,200 total", None, None), ("$5 a night", None, None), ("budget is flexible", None, None),
    ])
    def test_budget_per_night_only(self, text, lo, hi):
        f = ts.parse_fields(f"hotels in mesa oct 17-21 {text}", today=TODAY)
        assert (f["budget_min"], f["budget_max"]) == (lo, hi)

    @pytest.mark.parametrize("text,beds", [
        ("primary suite with a king bed", "king"), ("king-size bed", "king"),
        ("two queens", "two_queens"), ("2 queen beds", "two_queens"), ("a queen bed", "queen"),
        ("hotels in queen creek", "any"), ("", "any"),
    ])
    def test_bed_type(self, text, beds):
        assert ts.parse_fields(f"hotels in mesa oct 17-21 {text}", today=TODAY)["beds"] == beds

    @pytest.mark.parametrize("text,n", [("3 bedrooms", 3), ("3-bedroom", 3), ("2br", 2),
                                         ("two bedroom", 2), ("15 bedrooms", None)])
    def test_bedrooms(self, text, n):
        assert ts.parse_fields(f"rentals in mesa oct 17-21 {text}", today=TODAY)["bedrooms"] == n

    @pytest.mark.parametrize("text,kind", [
        ("find hotels in mesa oct 17-21", "hotel"), ("find an airbnb in mesa oct 17-21", "rental"),
        ("find hotels and airbnbs in mesa oct 17-21", "both"),
        ("find places to stay in mesa oct 17-21", "both"),
    ])
    def test_kind(self, text, kind):
        assert ts.parse_constraints(text, today=TODAY).constraints.kind == kind

    def test_styles_are_a_fixed_vocabulary(self):
        f = ts.parse_fields("modern, clean, camera-friendly, quiet, pool, walkable, luxury, "
                            "boho vibes", today=TODAY)
        assert f["styles"] == ("modern", "clean", "photogenic", "quiet", "pool", "walkable", "luxury")

    def test_identity_has_no_field_to_land_in(self):
        c = ts.parse_constraints(PII_ASK, today=TODAY).constraints
        assert c == ts.TravelConstraints(
            check_in=date(2026, 10, 17), check_out=date(2026, 10, 21),
            areas=("mesa", "gilbert", "scottsdale"), party_size=4, budget_min=300,
            budget_max=400, beds="king", bedrooms=None, kind="both",
            styles=("modern", "photogenic"))
        rec = json.dumps(c.to_record())
        for tok in PII_TOKENS:
            assert tok.lower() not in rec.lower(), tok

    def test_constraints_are_frozen(self):
        c = _constraints()
        with pytest.raises(Exception):
            c.areas = ("tucson",)  # type: ignore[misc]


# ── the request builder + the belt (B6/B7) ───────────────────────────────────

class TestRequestBuilderAndBelt:
    def _req(self, c=None, uses=4):
        return ts.build_request(c or _constraints(), max_uses=uses, model=MODEL)

    def test_the_request_is_fields_only(self):
        c = ts.parse_constraints(PII_ASK, today=TODAY).constraints
        r = ts.build_request(c, max_uses=4, model=MODEL)
        assert set(r) == {"model", "max_tokens", "system", "messages", "tools", "thinking"}
        assert r["system"] == ts.TRAVEL_SYSTEM
        assert r["thinking"] == {"type": "disabled"} and r["max_tokens"] == 4096
        assert len(r["messages"]) == 1 and r["messages"][0]["role"] == "user"
        assert r["tools"] == [{
            "type": "web_search_20250305", "name": "web_search", "max_uses": 4,
            "blocked_domains": web_guard.BLOCKED_DOMAINS,
            "user_location": {"type": "approximate", "city": "Phoenix", "region": "Arizona",
                              "country": "US", "timezone": "America/Phoenix"}}]
        blob = json.dumps(r).lower()
        for tok in PII_TOKENS:
            assert tok.lower() not in blob, tok
        # no entity prompt / KB / history / caller identity: nothing but the three blocks
        assert "cora" not in blob and "harrison" not in blob and "tessa" not in blob
        assert ts.assert_request_clean(r, c) is None

    def test_rendered_content_is_exactly_the_template(self):
        c = ts.parse_constraints(PII_ASK, today=TODAY).constraints
        assert ts.render_request_text(c) == (
            "Find lodging listings on the public web for this stay.\n"
            "Area: Mesa, Arizona; Gilbert, Arizona; Scottsdale, Arizona.\n"
            "Check-in: Saturday, October 17, 2026. Check-out: Wednesday, October 21, 2026 (4 nights).\n"
            "Guests: 4.\n"
            "Nightly rate range: 300 to 400 US dollars.\n"
            "Beds: at least one king bed.\n"
            "Type: hotels and vacation rentals.\n"
            "Style: modern, photogenic.")
        assert web_guard._screen_query(ts.render_request_text(c)) is None

    @pytest.mark.parametrize("tamper,reason", [
        (lambda r: r["messages"][0].update(content=r["messages"][0]["content"] + " Jordan Riverstone"), "content"),
        (lambda r: r.update(system=r["system"] + " The guest is Jordan Riverstone."), "system"),
        (lambda r: r["tools"].append({"type": "web_fetch_20250910", "name": "web_fetch"}), "tools"),
        (lambda r: r["tools"][0].update(max_uses=40), "max_uses"),
        (lambda r: r["tools"][0].update(blocked_domains=[]), "tools"),
        (lambda r: r["messages"].insert(0, {"role": "user", "content": "prior turn"}), "messages"),
        (lambda r: r.update(metadata={"user_id": "U0B3KH5UZJ7"}), "keys"),
        (lambda r: r.update(thinking={"type": "enabled", "budget_tokens": 2000}), "thinking"),
        (lambda r: r.update(model="claude-haiku-4-5"), "model"),
    ])
    def test_a_tampered_request_is_refused(self, tamper, reason):
        c = _constraints()
        r = self._req(c)
        assert ts.assert_request_clean(r, c) is None
        tamper(r)
        assert ts.assert_request_clean(r, c) == reason

    def test_a_render_that_carried_a_foreign_token_is_refused(self, monkeypatch):
        """The token allowlist is its own layer: even a render() bug that put a name
        or a loyalty number into the content is refused."""
        c = _constraints()
        real = ts.render_request_text
        for extra, reason in ((" Jordan", "word_token"), (" 123456789", "number_token"),
                              (" 777", "number_token"), (" Hilton", "word_token")):
            monkeypatch.setattr(ts, "render_request_text", lambda cc, _e=extra: real(cc) + _e)
            r = ts.build_request(c, max_uses=4, model=MODEL)
            assert ts.assert_request_clean(r, c) == reason, extra

    def test_constraints_out_of_range_are_refused(self):
        c = _constraints()
        r = self._req(c)
        bad = _constraints(party_size=99)
        assert ts.assert_request_clean(r, bad) == "constraints"
        bad = _constraints(areas=("atlantis",))
        assert ts.assert_request_clean(r, bad) == "constraints"

    def test_the_belt_fails_closed_on_garbage(self):
        assert ts.assert_request_clean(None, _constraints()) == "keys"
        assert ts.assert_request_clean({"model": 1}, _constraints()) == "keys"
        r = self._req()
        r["messages"] = "not a list"
        assert ts.assert_request_clean(r, _constraints()) == "messages"

    def test_constants_are_identity_free(self):
        """B6: the fixed parts of every request carry no roster name, no 'Cora', no
        internal entity token, no email, no 5+ digit run, no loyalty word."""
        consts = [ts.TRAVEL_SYSTEM, json.dumps(ts._tool_def(4)), *ts._T_BEDS.values(),
                  *ts._T_KIND.values(), ts._T_HEAD, ts._T_AREA, ts._T_DATES, ts._T_GUESTS,
                  ts._T_BUDGET_RANGE, ts._T_BUDGET_MAX, ts._T_BUDGET_MIN, ts._T_BUDGET_AROUND,
                  ts._T_BEDROOMS, ts._T_STYLE]
        blob = "\n".join(consts)
        words = {w.lower() for w in ts._WORD_RE.findall(blob)}
        names = set()
        for rec in org_roles.all_roles():
            names.update(p.lower() for p in (rec.name or "").split() if len(p) >= 3)
        assert not (words & names), sorted(words & names)
        assert "cora" not in words
        assert not web_guard._ENTITY_TOKEN_RE.search(blob)
        assert not web_guard._EMAIL_RE.search(blob)
        import re
        # the only digit run is the API's own tool-type version string
        assert not re.search(r"\d{5,}", blob.replace("web_search_20250305", "web_search"))
        for w in ("hilton", "honors", "marriott", "bonvoy", "hyatt", "ihg", "loyalty", "member",
                  "account", "points"):
            assert w not in words, w

    def test_max_uses_is_clamped_into_the_per_ask_budget(self):
        assert self._req(uses=99)["tools"][0]["max_uses"] == 4
        assert self._req(uses=0)["tools"][0]["max_uses"] == 1


# ── the web-only call (B7) ───────────────────────────────────────────────────

class TestRunSearch:
    def test_pii_never_reaches_any_iteration_and_continuations_are_structural(self):
        """THE kickoff assertion, on the create kwargs of EVERY iteration: a named,
        email/phone/loyalty-bearing ask -> none of those tokens in any create; tools
        exactly [web_search_20250305]; one user message; the resume re-sends only
        the API's own assistant content."""
        c = ts.parse_constraints(PII_ASK, today=TODAY).constraints
        request = ts.build_request(c, max_uses=4, model=MODEL)
        assert ts.assert_request_clean(request, c) is None
        d1, d2 = _split_pause(_fx())
        fake = FakeAnthropic([_msg(d1), _msg(d2)])
        out = ts.run_search(request, budget=4, client_factory=lambda: fake)
        assert out.status == "ok" and len(out.options) == 5 and out.searches == 4
        assert len(fake.calls) == 2
        for kw in fake.calls:
            blob = json.dumps(kw).lower()
            for tok in PII_TOKENS:
                assert tok.lower() not in blob, tok
            assert [t["type"] for t in kw["tools"]] == ["web_search_20250305"]
            assert kw["system"] == ts.TRAVEL_SYSTEM
            assert kw["messages"][0] == request["messages"][0]
            assert 0 < kw["timeout"] <= ts.API_TIMEOUT      # the time left on ONE deadline
            assert kw["thinking"] == {"type": "disabled"}
        assert len(fake.calls[0]["messages"]) == 1
        assert fake.calls[1]["messages"][1] == {
            "role": "assistant", "content": _msg(d1).model_dump(mode="json", exclude_none=True)["content"]}
        # the resume is clamped to what the ask has left (4 - 2 used)
        assert fake.calls[1]["tools"][0]["max_uses"] == 2

    def test_no_resume_once_the_budget_is_spent(self):
        d1, _d2 = _split_pause(_fx())
        fake = FakeAnthropic([_msg(d1)])
        out = ts.run_search(ts.build_request(_constraints(), max_uses=2, model=MODEL), budget=2,
                            client_factory=lambda: fake)
        assert len(fake.calls) == 1
        assert out.status == "unreadable"      # the paused half carries no answer

    def test_usage_is_ledgered_tagged_and_also_on_error(self, caplog):
        caplog.set_level(logging.INFO, logger="cora.llm_usage")
        d1, _d2 = _split_pause(_fx())
        fake = FakeAnthropic([_msg(d1), anthropic.APIConnectionError(request=MagicMock())])
        out = ts.run_search(ts.build_request(_constraints(), max_uses=4, model=MODEL), budget=4,
                            entity="FNDR", client_factory=lambda: fake)
        assert out.status == "failed" and out.error == "api_error:APIConnectionError"
        rows = [json.loads(l) for l in web_guard._USAGE_LEDGER.read_text(encoding="utf-8").splitlines()]
        assert rows[-1]["event"] == "usage" and rows[-1]["searches"] == 2
        assert rows[-1]["channel"] == ts.LANE_CHANNEL
        assert ts.lane_searches_today() == 2
        assert any("caller=travel_shortlist" in r.getMessage() for r in caplog.records)

    # D-051 r1 c2-webcall#3: 'record_usage also on error' recorded NOTHING for the
    # create that failed -- searches only grew after get_final_message returned -- so
    # a first create that died mid-stream after server-side searches ran never drew
    # down the lane or org cap, and every retry got a fresh budget.
    def test_a_first_create_that_dies_mid_stream_is_charged_its_partial_searches(self, caplog):
        import httpx
        caplog.set_level(logging.INFO, logger="cora.llm_usage")
        raw = _RawSSE(_sse_events(_fx(), blocks=3, finish=False), exc=httpx.ReadTimeout("stalled"))
        fake = SdkStreamAnthropic([raw])       # blocks 1+2 are web_search server_tool_use
        out = ts.run_search(ts.build_request(_constraints(), max_uses=4, model=MODEL), budget=4,
                            client_factory=lambda: fake)
        assert out.status == "failed" and out.error == "api_error:ReadTimeout" and out.searches == 2
        assert ts.lane_searches_today() == 2
        rows = [json.loads(l) for l in web_guard._USAGE_LEDGER.read_text(encoding="utf-8").splitlines()]
        assert [r["searches"] for r in rows if r.get("event") == "usage"] == [2]
        lines = [r.getMessage() for r in caplog.records if "caller=travel_shortlist" in r.getMessage()]
        assert len(lines) == 1 and "via=partial" in lines[0]

    def test_a_resume_that_dies_mid_stream_adds_its_partial_to_the_first_creates(self):
        import httpx
        d1, d2 = _split_pause(_fx())
        # d2's content: [text, server_tool_use, server_tool_use, result, result, text...]
        raws = [_RawSSE(_sse_events(d1)), _RawSSE(_sse_events(d2, blocks=2, finish=False),
                                                  exc=anthropic.APIConnectionError(request=MagicMock()))]
        out = ts.run_search(ts.build_request(_constraints(), max_uses=4, model=MODEL), budget=4,
                            client_factory=lambda: SdkStreamAnthropic(raws))
        assert out.status == "failed" and out.searches == 3
        assert ts.lane_searches_today() == 3

    def test_a_stream_that_opened_but_never_started_is_charged_the_creates_max_uses(self):
        import httpx
        fake = SdkStreamAnthropic([_RawSSE([], exc=httpx.ReadTimeout("no first byte"))])
        out = ts.run_search(ts.build_request(_constraints(), max_uses=3, model=MODEL), budget=3,
                            client_factory=lambda: fake)
        assert out.status == "failed" and out.searches == 3     # conservative: never under-count
        assert ts.lane_searches_today() == 3

    def test_a_request_that_never_opened_a_stream_charges_nothing(self):
        fake = SdkStreamAnthropic([anthropic.APIConnectionError(request=MagicMock())])
        out = ts.run_search(ts.build_request(_constraints(), max_uses=4, model=MODEL), budget=4,
                            client_factory=lambda: fake)
        assert out.status == "failed" and out.searches == 0
        assert ts.lane_searches_today() == 0

    # D-051 r1 c2-webcall#2: timeout=90 on messages.stream is httpx's PER-READ timeout
    # (the longest gap between chunks), not a bound on the whole create -- a stream
    # that keeps delivering events was never cut and held the lane's only worker.
    def test_a_trickling_stream_is_cut_at_the_wall_clock_deadline(self, monkeypatch):
        monkeypatch.setattr(ts, "API_TIMEOUT", 0.2)
        events = _sse_events(_fx())
        raw = _RawSSE(events, pace=0.05)             # 27 events: >= 1.35 s if never cut
        t0 = time.monotonic()
        out = ts.run_search(ts.build_request(_constraints(), max_uses=4, model=MODEL), budget=4,
                            client_factory=lambda: SdkStreamAnthropic([raw]))
        elapsed = time.monotonic() - t0
        assert out.status == "failed" and out.error == "api_error:WallClockTimeout"
        assert raw.closed and raw.yielded < len(events)
        assert elapsed < 1.0
        # whatever the cut create had searched is charged, never lost
        assert 0 <= out.searches <= 4 and ts.lane_searches_today() == out.searches

    def test_one_deadline_spans_both_iterations(self, monkeypatch):
        monkeypatch.setattr(ts, "API_TIMEOUT", 5.0)
        d1, d2 = _split_pause(_fx())
        fake = SdkStreamAnthropic([_RawSSE(_sse_events(d1), pace=0.02),    # >= 0.26 s
                                   _RawSSE(_sse_events(d2))])
        out = ts.run_search(ts.build_request(_constraints(), max_uses=4, model=MODEL), budget=4,
                            client_factory=lambda: fake)
        assert out.status == "ok" and len(fake.calls) == 2
        assert fake.calls[0]["timeout"] <= 5.0
        # the resume gets only what is LEFT of the one deadline (a stall there is cut too)
        assert fake.calls[1]["timeout"] <= 5.0 - 0.25

    def test_the_real_sdk_stream_parses_the_live_shape_end_to_end(self):
        fake = SdkStreamAnthropic([_RawSSE(_sse_events(_fx()))])
        out = ts.run_search(ts.build_request(_constraints(), max_uses=4, model=MODEL), budget=4,
                            client_factory=lambda: fake)
        assert out.status == "ok" and len(out.options) == 5 and out.searches == 4
        assert ts.lane_searches_today() == 4

    def test_a_default_client_refuses_under_pytest(self, monkeypatch):
        monkeypatch.setattr(ts, "_CLIENT_FACTORY", None)
        out = ts.run_search(ts.build_request(_constraints(), max_uses=4, model=MODEL), budget=4)
        assert out.status == "failed" and out.error == "api_error:RuntimeError"

    def test_an_error_object_result_is_recorded_and_a_blank_answer_is_a_failure(self):
        d = _fx()
        for b in d["content"]:
            if b["type"] == "web_search_tool_result":
                b["content"] = {"type": "web_search_tool_result_error", "error_code": "max_uses_exceeded"}
        d = _mutate_options(d, lambda opts: [])
        out = ts.run_search(ts.build_request(_constraints(), model=MODEL), budget=4,
                            client_factory=lambda: FakeAnthropic([_msg(d)]))
        assert out.status == "failed" and out.error == "tool_error:max_uses_exceeded"
        assert out.tool_errors == ("max_uses_exceeded",) * 4

    def test_one_error_object_does_not_sink_the_other_results(self):
        d = _fx()
        d["content"][3]["content"] = {"type": "web_search_tool_result_error", "error_code": "unavailable"}
        out = ts.run_search(ts.build_request(_constraints(), model=MODEL), budget=4,
                            client_factory=lambda: FakeAnthropic([_msg(d)]))
        assert out.status == "ok" and out.tool_errors == ("unavailable",)
        assert 1 <= len(out.options) <= 5

    def test_a_fabricated_url_is_dropped(self):
        d = _mutate_options(_fx(), lambda o: [dict(o[0], url="https://scottsdale-deals.example/book")] + o[1:])
        out = ts.run_search(ts.build_request(_constraints(), model=MODEL), budget=4,
                            client_factory=lambda: FakeAnthropic([_msg(d)]))
        assert out.status == "ok" and out.dropped == 1 and len(out.options) == 4
        assert all("scottsdale-deals" not in o["url"] for o in out.options)

    def test_a_citation_split_json_still_parses(self):
        """The API splits text at citation boundaries -- including INSIDE the fence.
        Joined with '' the fence parses; a '\\n' join would break the JSON string."""
        d = _fx()
        i = _fence_index(d)
        text = d["content"][i]["text"]
        cut1 = text.index("Iconic mid-century")
        cut2 = cut1 + len("Iconic mid-century modern hotel")
        cit = copy.deepcopy(d["content"][11]["citations"][0])
        d["content"][i:i + 1] = [
            {"type": "text", "text": text[:cut1], "citations": None},
            {"type": "text", "text": text[cut1:cut2], "citations": [cit]},
            {"type": "text", "text": text[cut2:], "citations": None},
        ]
        out = ts.run_search(ts.build_request(_constraints(), model=MODEL), budget=4,
                            client_factory=lambda: FakeAnthropic([_msg(d)]))
        assert out.status == "ok" and len(out.options) == 5
        assert out.options[0]["fit_note"].startswith("Iconic mid-century modern hotel")

    def test_no_fence_is_unreadable_not_no_fit(self):
        d = _fx()
        i = _fence_index(d)
        d["content"][i]["text"] = "Here are some ideas: Hotel Valley Ho is lovely."
        out = ts.run_search(ts.build_request(_constraints(), model=MODEL), budget=4,
                            client_factory=lambda: FakeAnthropic([_msg(d)]))
        assert out.status == "unreadable"

    def test_at_most_five_options(self):
        d = _mutate_options(_fx(), lambda o: o + o)
        out = ts.run_search(ts.build_request(_constraints(), model=MODEL), budget=4,
                            client_factory=lambda: FakeAnthropic([_msg(d)]))
        assert len(out.options) == 5

    def test_the_card_renders_the_apis_own_record_url_never_the_models_string(self):
        """D-051 r1 c2-injection-card#2: normalize_url drops the fragment (and folds host
        case / one trailing '/'), so a model-invented '#/redirect?to=...' on a record URL
        matched -- and the MODEL's string went into the href. The card now renders the
        first raw URL the API returned for that record."""
        def mut(o):
            o[0] = dict(o[0], url="https://HotelValleyHo.com/#/redirect?to=evil")
            return o
        d = _mutate_options(_fx(), mut)
        out = ts.run_search(ts.build_request(_constraints(), model=MODEL), budget=4,
                            client_factory=lambda: FakeAnthropic([_msg(d)]))
        assert out.status == "ok" and out.dropped == 0
        assert out.options[0]["url"] == "https://hotelvalleyho.com/"
        _t, blocks = ts.render_card(_constraints(), out.options, now=NOW)
        body = blocks[1]["text"]["text"]
        assert body.startswith("*<https://hotelvalleyho.com/|") and "redirect" not in body

    def test_a_record_whose_raw_string_is_unsafe_to_render_is_dropped(self):
        """The RENDERED string is checked too: a record carrying '|' or '<' in a fragment
        (dropped by normalization) would break the <url|label> token."""
        rec_url = "https://www.hotelvalleyho.com/rooms#a|b"
        records, _e = ts.collect_record_urls([{"content": [{"type": "web_search_tool_result",
                                                            "content": [{"url": rec_url}]}]}])
        assert records == {"https://www.hotelvalleyho.com/rooms": rec_url}
        opts, dropped = ts.validate_options(
            [{"property": "Hotel Valley Ho", "url": "https://www.hotelvalleyho.com/rooms",
              "kind": "hotel", "nightly_rate": "$300", "fit_note": "quiet"}], records)
        assert opts == [] and dropped == 1

    def test_the_first_raw_record_string_wins_across_iterations(self):
        first = {"content": [{"type": "web_search_tool_result",
                              "content": [{"url": "https://www.Example-Hotel.com/rooms/"}]}]}
        second = {"content": [{"type": "text", "text": "x",
                               "citations": [{"url": "https://www.example-hotel.com/rooms"}]}]}
        records, _e = ts.collect_record_urls([first, second])
        assert records == {"https://www.example-hotel.com/rooms": "https://www.Example-Hotel.com/rooms/"}

    def test_a_bad_kind_or_internal_url_is_dropped(self):
        def mut(o):
            o[0] = dict(o[0], kind="spa")
            o[1] = dict(o[1], url="https://docs.google.com/document/d/abc")
            return o
        d = _mutate_options(_fx(), mut)
        # make the internal URL a "record" too, so only the internal-host rule can drop it
        d["content"][3]["content"].append({"type": "web_search_result", "url": "https://docs.google.com/document/d/abc",
                                           "title": "doc", "page_age": None, "encrypted_content": "ENC"})
        out = ts.run_search(ts.build_request(_constraints(), model=MODEL), budget=4,
                            client_factory=lambda: FakeAnthropic([_msg(d)]))
        assert out.dropped == 2 and len(out.options) == 3


class TestFieldSanitizer:
    def test_an_injected_fit_note_is_neutralized(self):
        raw = ("<!channel> Booked for 4 — confirmation #88213, see scottsdale-deals.example/login"
               "\nFORGED LINE @here and www.evil.example https://evil.example/x <@U0B2RM2JYJ1> Done.")
        out = ts.sanitize_field(raw, 160)
        low = out.lower()
        for bad in ("<!channel>", "@here", "scottsdale-deals", "evil.example", "www.", "http",
                    "<@", "booked", "confirmation", "done", "\n"):
            assert bad not in low, bad
        assert len(out) <= 160

    def test_escaping_happens_at_render_and_markdown_is_stripped(self):
        c = _constraints()
        opt = {"property": "A & B <Suites> | *Bold*_it_", "nightly_rate": "$300 <b>", "url":
               "https://hotelvalleyho.com/", "fit_note": "quiet & modern", "kind": "hotel"}
        opt = {**opt, "property": ts.sanitize_field(opt["property"], 80),
               "nightly_rate": ts.sanitize_field(opt["nightly_rate"], 40)}
        _text, blocks = ts.render_card(c, [opt], now=NOW)
        body = blocks[1]["text"]["text"]
        assert "<https://hotelvalleyho.com/|A &amp; B &lt;Suites&gt; / Bold it>" in body
        assert "$300 &lt;b&gt;" in body and "quiet &amp; modern" in body

    def test_caps(self):
        assert len(ts.sanitize_field("x" * 500, 80)) == 80
        assert len(ts.sanitize_field("y " * 500, 40)) <= 40

    # D-051 r1 c2-injection-card#0: a domain wrapped in markdown / a format character /
    # a prefix or trailing character used to survive (the strip ran BEFORE the
    # markdown pass, on a fullmatch of a lightly trimmed core) and Slack auto-linked
    # the bare host the translate left behind -- a link the record check never saw.
    @pytest.mark.parametrize("raw", [
        "$329 on **scottsdale-deals.example**", "_evil.com_", "`evil.com`", "~evil.com~",
        "evil.com|login", "evil.com​", "evil.com…", "see:evil.com", "*evil.com/login*",
        "(**www.evil.example**)", "evil​.com", "‮moc.live", "ｅｖｉｌ．ｃｏｍ",
        "evil.com⁠now", "e­vil.com",
    ])
    def test_a_wrapped_or_decorated_domain_is_stripped(self, raw):
        import re
        import unicodedata
        out = ts.sanitize_field(raw, 160)
        folded = unicodedata.normalize("NFKC", out)     # a fullwidth host folds to ASCII
        assert not re.search(r"[a-z0-9-]\.[a-z]{2}", folded, re.IGNORECASE), (raw, out)
        assert "evil.com" not in folded.lower() and "scottsdale-deals" not in folded
        assert not [ch for ch in out if unicodedata.category(ch) == "Cf"], (raw, out)

    # D-051 r1 c2-injection-card#1: '_' is a word character, so the \b anchors never
    # matched inside Slack italics and the markdown pass then exposed the bare word.
    @pytest.mark.parametrize("raw", [
        "_Booked_ for 4 guests", "Room _reserved_ under your name", "_Confirmed_: king suite",
        "_Done_", "__Booked__", "booked_for_4", "Rebooked for your dates", "Prebooked king suite",
        "Your bookings are set", "Overbooked but we got you in", "*BOOKED*", "~held~ for you",
        "re-booked", "Un​booked", "Book​ed for you",
    ])
    def test_booking_words_are_neutralized_through_markdown_and_compounds(self, raw):
        low = ts.sanitize_field(raw, 160).lower()
        for bad in ("book", "reserv", "confirm", "done", "held"):
            assert bad not in low, (raw, low)

    @pytest.mark.parametrize("raw", ["_@here_ great pool", "heads up @channel_", "*@everyone*",
                                     "@​here", "＠here now"])
    def test_at_specials_are_stripped_through_markdown(self, raw):
        import re
        out = ts.sanitize_field(raw, 160)
        # a format character becomes a SPACE ("@ here" pings no one; a removed one would
        # have glued "Booked​for" into an un-neutralized "Bookedfor")
        assert not re.search(r"@(?:here|channel|everyone)", out, re.IGNORECASE), (raw, out)

    def test_the_sanitizer_is_linear_on_degenerate_input(self):
        for shape in (" " * 40000, "a." * 20000, "_" * 40000, "​" * 40000, "book" * 10000,
                      "*a.bc*" * 6000, "@" * 40000):
            assert _best_of_3(lambda: ts.sanitize_field(shape, 160)) < 0.05, shape[:10]

    def test_a_decorated_domain_never_reaches_the_rendered_card(self):
        """End to end through validate_options + render_card (the finding's own repro)."""
        m = _msg(_fx())
        urls, _e = ts.collect_record_urls([m])
        raw = [{"property": "**Hotel Valley Ho**", "nightly_rate": "$329 on **scottsdale-deals.example**",
                "url": "https://hotelvalleyho.com/", "kind": "hotel",
                "fit_note": "_Booked_ for 4 -- now at `scottsdale-deals.example/login` _@here_"}]
        opts, dropped = ts.validate_options(raw, urls)
        assert dropped == 0 and len(opts) == 1
        _t, blocks = ts.render_card(_constraints(), opts, now=NOW)
        body = blocks[1]["text"]["text"].lower()
        for bad in ("scottsdale-deals", "booked", "@here"):
            assert bad not in body, bad

    def test_every_mrkdwn_text_object_on_the_card_is_verbatim(self):
        """Structural backstop: verbatim stops Slack auto-linking/auto-parsing whatever a
        field still carries; the explicit <url|label> links keep working."""
        def walk(node):
            if isinstance(node, dict):
                if node.get("type") == "mrkdwn":
                    yield node
                for v in node.values():
                    yield from walk(v)
            elif isinstance(node, list):
                for v in node:
                    yield from walk(v)
        for opts, dropped in ((TestCard()._options(), 0), ([], 0), ([], 2)):
            _t, blocks = ts.render_card(_constraints(), opts, dropped=dropped, now=NOW)
            objs = list(walk(blocks))
            assert objs and all(o.get("verbatim") is True for o in objs), objs
        _t, blocks = ts.render_card(_constraints(), TestCard()._options(), now=NOW)
        assert blocks[1]["text"]["text"].startswith("*<https://")


# ── the card (B4/B9) ─────────────────────────────────────────────────────────

class TestCard:
    def _options(self):
        m = _msg(_fx())
        urls, _e = ts.collect_record_urls([m])
        opts, _d = ts.validate_options(ts.extract_json_options(ts.final_text(m)), urls)
        return opts

    def test_card_has_at_most_five_options_an_as_of_stamp_and_the_fields_clause(self):
        c = ts.parse_constraints(PII_ASK, today=TODAY).constraints
        text, blocks = ts.render_card(c, self._options() * 2, now=NOW)
        sections = [b for b in blocks if b["type"] == "section"]
        assert len(sections) == 1 + 5
        ctx = blocks[-1]["elements"][0]["text"]
        assert ctx.startswith("As of Sep 25, 2026 9:14 AM AZ")
        assert "rates as the search results showed them" in ctx and "taxes/fees may apply" in ctx
        assert ("searched with area, dates, party size, nightly budget, beds, lodging type and "
                "style only") in ctx
        assert "no names, no loyalty accounts" in ctx and "nothing is booked; booking stays with a person" in ctx

    def test_the_fields_clause_follows_what_was_rendered(self):
        c = _constraints(party_size=None, budget_min=None, budget_max=None, beds="any", styles=())
        assert ts.searched_fields_clause(c) == "area, dates and lodging type"

    def test_text_fallback_is_constant_and_carries_no_model_string(self):
        c = ts.parse_constraints(PII_ASK, today=TODAY).constraints
        opts = self._options()
        text, _blocks = ts.render_card(c, opts, now=NOW)
        assert text == ("Lodging shortlist: 5 options — Mesa, Gilbert, Scottsdale, Oct 17–21, "
                        "2026, 4 guests. Open the card; nothing is booked.")
        for o in opts:
            assert o["property"] not in text and o["url"] not in text
            assert (o["fit_note"] or "zz-none") not in text

    def test_zero_options_copy_is_distinct(self):
        c = _constraints()
        _t, blocks = ts.render_card(c, [], now=NOW)
        assert blocks[1]["text"]["text"] == ts.NO_FIT_TEXT
        _t, blocks = ts.render_card(c, [], dropped=2, now=NOW)
        assert blocks[1]["text"]["text"] == ts.NO_VERIFIED_FIT_TEXT
        assert ts.SEARCH_FAILED_REPLY not in (ts.NO_FIT_TEXT, ts.NO_VERIFIED_FIT_TEXT)

    def test_spans(self):
        assert ts._short_span(date(2026, 10, 30), date(2026, 11, 2)) == "Oct 30 – Nov 2, 2026"
        assert ts._short_span(date(2026, 12, 30), date(2027, 1, 2)) == "Dec 30, 2026 – Jan 2, 2027"


# ── copy pins (A27 / honesty review: pins that CAN fail) ─────────────────────

def _rendered_copy() -> list[str]:
    c = ts.parse_constraints(PII_ASK, today=TODAY).constraints
    text, blocks = ts.render_card(c, TestCard()._options(), now=NOW)
    t0, b0 = ts.render_card(c, [], now=NOW)
    return [text, t0, blocks[0]["text"]["text"], blocks[-1]["elements"][0]["text"],
            b0[1]["text"]["text"], ts.ACK_TEXT, ts.OFF_REPLY, ts.WEB_OFF_REPLY, ts.CAP_REPLY,
            ts.MODEL_REPLY, ts.EVAL_REPLY, ts.BELT_REPLY, ts.START_FAILED_REPLY,
            ts.SEARCH_FAILED_REPLY, ts.UNREADABLE_REPLY, ts.POST_FAILED_REPLY,
            ts.FOLLOWUP_HELP_REPLY, ts.CLARIFY_DATES_REPLY, ts.CLARIFY_AREA_REPLY,
            ts.CLARIFY_BOTH_REPLY, ts.CLARIFY_MALFORMED_REPLY, ts.NO_VERIFIED_FIT_TEXT]


class TestCopyPins:
    @pytest.mark.parametrize("idx", range(len(_rendered_copy())))
    def test_no_rail_hit_even_in_enforce(self, idx, monkeypatch, caplog):
        text = _rendered_copy()[idx]
        monkeypatch.setenv("CORA_SENTINEL_ENFORCE", "enforce")
        caplog.set_level(logging.WARNING, logger=slack_egress.__name__)
        assert slack_egress.screen_phantom_write_claims(text, tool_use_count=0) == text
        assert not [r for r in caplog.records if slack_egress.PHANTOM_LOG_KEY in r.getMessage()]
        assert slack_egress.sanitize_text(text) == text
        for word in ("booked", "reserved", "done", "all set"):
            assert word not in text.lower().replace("nothing is booked", "").replace(
                "booking stays", ""), word


# ── the thread store ─────────────────────────────────────────────────────────

class TestThreadStore:
    def test_append_read_and_lane_thread(self):
        c = _constraints()
        assert not ts.is_lane_thread(TRAVEL_CHANNEL, "1790000000.000100", now=NOW)
        assert ts.append_event("asked", channel=TRAVEL_CHANNEL, root_ts="1790000000.000100",
                               constraints=c.to_record(), now=NOW)
        # the 48 h lane-thread bound (D-051 r1) is read against an injected clock here
        assert ts.is_lane_thread(TRAVEL_CHANNEL, "1790000000.000100", now=NOW)
        assert not ts.is_lane_thread(TRAVEL_CHANNEL, "1790000000.000200", now=NOW)
        assert not ts.is_lane_thread("C0OTHER", "1790000000.000100", now=NOW)
        assert ts.latest_constraints(TRAVEL_CHANNEL, "1790000000.000100", now=NOW) == c
        raw = ts.threads_path().read_text(encoding="utf-8")
        assert "scottsdale" in raw and "Jordan" not in raw

    def test_a_tampered_record_is_never_trusted_into_a_request(self):
        rec = _constraints().to_record()
        rec["areas"] = ["Jordan Riverstone's house"]
        ts.append_event("asked", channel=TRAVEL_CHANNEL, root_ts="1.1", constraints=rec, now=NOW)
        assert ts.latest_constraints(TRAVEL_CHANNEL, "1.1", now=NOW) is None
        rec2 = _constraints().to_record()
        rec2["party_size"] = "4"
        assert ts.TravelConstraints.from_record(rec2) is None

    def test_an_unreadable_store_raises_for_the_caller_to_decide(self, monkeypatch, tmp_path):
        d = tmp_path / "a-directory.jsonl"
        d.mkdir()
        monkeypatch.setenv("CORA_TRAVEL_SHORTLIST_THREADS_PATH", str(d))
        with pytest.raises(OSError):
            ts.is_lane_thread(TRAVEL_CHANNEL, "1.1")
        s = ts.threads_summary(now=NOW)
        assert s["available"] is False

    def test_summary_windows_and_counts(self):
        old = NOW - timedelta(days=8)
        ts.append_event("belt_refused", channel=TRAVEL_CHANNEL, root_ts="1", reason="word_token", now=old)
        for i in range(3):
            ts.append_event("asked", channel=TRAVEL_CHANNEL, root_ts=f"2.{i}", now=NOW)
        ts.append_event("posted", channel=TRAVEL_CHANNEL, root_ts="2.0", options=3, now=NOW)
        ts.append_event("search_failed", channel=TRAVEL_CHANNEL, root_ts="2.1", error="x", now=NOW)
        with open(ts.threads_path(), "a", encoding="utf-8") as fh:
            fh.write("{not json\n")
        s = ts.threads_summary(now=NOW)
        assert (s["asks"], s["posted"], s["search_failed"], s["belt_refused"], s["bad_lines"]) == (3, 1, 1, 0, 1)
        assert s["available"] and s["exists"]


# ── caps ─────────────────────────────────────────────────────────────────────

class TestCaps:
    def _usage(self, searches, channel):
        web_guard.record_usage(searches, 0, entity="FNDR", channel_name=channel)

    def test_budget_is_the_min_of_per_ask_lane_and_org(self, monkeypatch):
        assert ts.search_budget() == 4
        self._usage(6, ts.LANE_CHANNEL)
        assert ts.lane_searches_today() == 6 and ts.search_budget() == 2
        self._usage(2, ts.LANE_CHANNEL)
        assert ts.search_budget() == 0
        monkeypatch.setenv("CORA_TRAVEL_SHORTLIST_DAILY_SEARCHES", "20")
        self._usage(29, "")            # interactive Q&A usage counts against the ORG cap
        assert ts.search_budget() == 3
        monkeypatch.setenv("CORA_WEB_SEARCH_DAILY_CAP", "10")
        assert ts.search_budget() == 0

    # D-051 r1 c2-webcall#1 + c2-egress#2 (B7: "clamp EACH create to min(per-ask
    # remaining, lane cap - lane today, org cap - org today); 0 -> no call"). The
    # budget used to be read ONCE on the listener at route time and frozen into the
    # Route; a queued job never re-read the ledger, so asks routed while an earlier
    # job was in flight each billed a full 4.
    def test_two_asks_routed_before_either_ran_never_overrun_the_lane_cap(self, monkeypatch):
        self._usage(4, ts.LANE_CHANNEL)                       # lane 4/8 already today
        fake = FakeAnthropic([_msg(_fx()), _msg(_fx())])
        monkeypatch.setattr(ts, "_CLIENT_FACTORY", lambda: fake)
        client, jobs = _slack_client(), []
        for root in ("1790000000.000801", "1790000000.000802"):
            r = _route(MUST_FIRE[1])
            assert r.kind == "search" and r.budget == 4          # both routed on the same stale ledger
            ts.execute_route(r, channel_id="D0HARRISON", thread_root_ts=root, entity="FNDR",
                             user_id=HARRISON, client=client, say=MagicMock(),
                             submit=lambda fn, *a, **k: jobs.append((fn, a, k)) or True, now=NOW)
        for fn, a, k in jobs:                                    # the 1-worker pool, in order
            fn(*a, **k)
        assert len(fake.calls) == 1                              # the second job made NO create
        assert ts.lane_searches_today() == 8
        assert client.chat_postMessage.call_args.kwargs["text"] == ts.CAP_REPLY
        refused = [r for r in _rows() if r["event"] == "refused"]
        assert refused == [{**refused[0], "root_ts": "1790000000.000802", "reason": "daily_cap",
                            "stage": "job", "searches": 0}]

    def test_a_create_is_shrunk_to_the_live_caps_and_structurally_rebelted(self):
        request = ts.build_request(_constraints(), max_uses=4, model=MODEL)
        self._usage(6, ts.LANE_CHANNEL)                       # filled AFTER the route was belted
        fake = FakeAnthropic([_msg(_fx())])
        ts.run_search(request, budget=4, client_factory=lambda: fake)
        (kw,) = fake.calls
        assert kw["tools"] == [ts._tool_def(2)]
        assert {k: v for k, v in kw.items() if k not in ("tools", "timeout")} == {
            k: v for k, v in request.items() if k != "tools"}

    def test_the_resume_re_reads_the_caps_between_creates(self, monkeypatch):
        monkeypatch.setenv("CORA_WEB_SEARCH_DAILY_CAP", "10")
        self._usage(4, "")                                     # org 4/10 -> the route's budget is 4
        assert ts.search_budget() == 4
        d1, d2 = _split_pause(_fx())
        fake = FakeAnthropic([_msg(d1), _msg(d2)])
        real_stream = fake.messages.stream

        def concurrent(**kw):                                  # an ordinary web turn lands mid-job
            if len(fake.calls) == 0:
                self._usage(3, "")
            return real_stream(**kw)
        fake.messages.stream = concurrent
        out = ts.run_search(ts.build_request(_constraints(), max_uses=4, model=MODEL), budget=4,
                            client_factory=lambda: fake)
        assert len(fake.calls) == 2 and out.status == "ok"
        # per-ask left 2, but org left = 10 - (4 + 3 + 2) = 1
        assert fake.calls[1]["tools"][0]["max_uses"] == 1

    def test_a_job_capped_at_run_time_posts_the_cap_line_and_settles(self):
        self._usage(8, ts.LANE_CHANNEL)
        client, fake = _slack_client(), FakeAnthropic([])
        ts._search_job(ts.build_request(_constraints(), max_uses=4, model=MODEL), _constraints(),
                       channel_id=TRAVEL_CHANNEL, root_ts="1790000000.000803", entity="FNDR",
                       user_id=HARRISON, budget=4, client=client, client_factory=lambda: fake)
        assert fake.calls == []
        assert client.chat_postMessage.call_args.kwargs["text"] == ts.CAP_REPLY
        (row,) = _rows()
        assert (row["event"], row["reason"], row["stage"], row["root_ts"]) == (
            "refused", "daily_cap", "job", "1790000000.000803")
        assert ts.lane_searches_today() == 8

    def test_env_defaults_and_fail_closed_spellings(self, monkeypatch):
        assert ts.lane_enabled() and ts.lane_daily_cap() == 8
        for off in ("off", "0", "false", "disabled", "nope"):
            monkeypatch.setenv("CORA_TRAVEL_SHORTLIST", off)
            assert not ts.lane_enabled(), off
        monkeypatch.setenv("CORA_TRAVEL_SHORTLIST_DAILY_SEARCHES", "junk")
        assert ts.lane_daily_cap() == 8


# ── routing (B2/B3) + execution ──────────────────────────────────────────────

def _route(text, **kw):
    kw.setdefault("user_id", HARRISON)
    kw.setdefault("channel_id", "D0HARRISON")
    kw.setdefault("channel_name", "dm")
    return ts.route_turn(text, today=TODAY, **kw)


class TestRouteTurn:
    def test_a_full_ask_routes_to_search_with_the_budget(self):
        r = _route(MUST_FIRE[1])
        assert r.kind == "search" and r.budget == 4 and r.constraints.areas == ("scottsdale",)

    def test_not_this_lanes_turn(self):
        assert _route("what's our cash position?") is None
        assert _route(MUST_FIRE[1], user_id="U_MEMBER") is None
        assert _route(MUST_FIRE[1], retrieval_grant=True) is None
        assert _route(MUST_FIRE[1], forced_tool_probe=lambda: True) is None

    @pytest.mark.parametrize("env,reply", [
        ({"CORA_TRAVEL_SHORTLIST": "off"}, ts.OFF_REPLY),
        ({"CORA_WEB_TOOLS": "off"}, ts.WEB_OFF_REPLY),
        ({"CORA_WEB_SEARCH_DAILY_CAP": "0"}, ts.CAP_REPLY),
        ({"CORA_EVAL_MODE": "1"}, ts.EVAL_REPLY),
    ])
    def test_never_falls_through(self, monkeypatch, env, reply):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        r = _route(MUST_FIRE[1])
        assert r.kind == "reply" and r.reply == reply

    def test_unsupported_model(self, monkeypatch):
        monkeypatch.setattr(ts, "lane_model", lambda: "claude-haiku-4-5")
        assert _route(MUST_FIRE[1]).reply == ts.MODEL_REPLY

    @pytest.mark.parametrize("text,reply", [
        ("find hotels in scottsdale", ts.CLARIFY_DATES_REPLY),
        ("find hotels near Gilbert's place oct 17-21", ts.CLARIFY_AREA_REPLY),
        ("find hotels", ts.CLARIFY_BOTH_REPLY),
        ("find hotels in scottsdale oct 17-17", ts.CLARIFY_MALFORMED_REPLY),
    ])
    def test_clarify_replies_never_echo_the_ask(self, text, reply):
        r = _route(text)
        assert r.kind == "reply" and r.reply == reply
        assert "gilbert" not in r.reply.lower()

    def test_lane_thread_follow_ups(self, monkeypatch):
        root = "1790000000.000900"
        # written at the REAL clock: route_turn reads the 48 h lane-thread bound at now
        ts.append_event("asked", channel="D0HARRISON", root_ts=root,
                        constraints=_constraints().to_record())
        kw = dict(thread_root_ts=root, lane_thread=True)
        r = _route("same dates but 6 people", **kw)
        assert r.kind == "search" and r.followup and r.constraints.party_size == 6
        assert r.constraints.areas == ("scottsdale",)
        r = _route("try Tempe instead", **kw)
        assert r.kind == "search" and r.constraints.areas == ("tempe",)
        for t in ("cheaper ones", "book the second one", "is the first one pet friendly?",
                  "google hotels in scottsdale oct 17-21 for me, Tessa and the crew, use our "
                  "Hilton Honors 123456789"):
            r = _route(t, **kw)
            assert r is not None and r.kind in ("reply", "search"), t
        r = _route("book the second one", **kw)
        assert r.reply == ts.FOLLOWUP_HELP_REPLY
        # another capability's words reach the ordinary path (B1 still withholds web there)
        assert _route("add the second one to my calendar", **kw) is None
        assert _route("remember the second one is the one we like", **kw) is None
        monkeypatch.setenv("CORA_EVAL_MODE", "1")
        assert _route("same dates but 6 people", **kw).reply == ts.EVAL_REPLY

    def test_the_named_loyalty_follow_up_re_runs_on_fields_only(self):
        root = "1790000000.000901"
        ts.append_event("asked", channel=TRAVEL_CHANNEL, root_ts=root,
                        constraints=_constraints(areas=("mesa",)).to_record())
        r = ts.route_turn("google hotels in scottsdale oct 18-22 for me, Tessa and the crew, use our "
                          "Hilton Honors 123456789", user_id="U_ANY", channel_id=TRAVEL_CHANNEL,
                          thread_root_ts=root, lane_thread=True, today=TODAY)
        assert r.kind == "search" and r.constraints.areas == ("scottsdale",)
        req = ts.build_request(r.constraints, max_uses=r.budget, model=MODEL)
        blob = json.dumps(req).lower()
        for tok in ("tessa", "hilton", "honors", "123456789", "crew"):
            assert tok not in blob


class TestExecuteRoute:
    def _exec(self, route, client, *, submit=None, say=None, root="1790000000.000500"):
        submitted = []
        ts.execute_route(route, channel_id=TRAVEL_CHANNEL, thread_root_ts=root, entity="FNDR",
                         user_id=HARRISON, client=client, say=say or MagicMock(),
                         submit=submit or (lambda fn, *a, **k: submitted.append((fn, a, k)) or True),
                         now=NOW)
        return submitted

    def test_a_reply_posts_threaded_without_unfurls(self):
        client = _slack_client()
        self._exec(ts.Route("reply", ts.OFF_REPLY, "lane_off"), client)
        kw = client.chat_postMessage.call_args.kwargs
        assert kw == {"channel": TRAVEL_CHANNEL, "text": ts.OFF_REPLY, "unfurl_links": False,
                      "unfurl_media": False, "thread_ts": "1790000000.000500"}
        assert _rows() == []

    def test_eval_mode_uses_say_only(self, monkeypatch):
        monkeypatch.setenv("CORA_EVAL_MODE", "1")
        client, say = _slack_client(), MagicMock()
        route = ts.Route("search", constraints=_constraints(), budget=4)
        subs = self._exec(route, client, say=say)
        client.chat_postMessage.assert_not_called()
        assert say.call_args.kwargs["text"] == ts.EVAL_REPLY
        assert subs == []
        (row,) = _rows()           # the refusal only: shape, no thread key
        assert (row["event"], row["stage"], row["reason"], row["root_ts"]) == ("refused", "gate", "eval", "")
        assert not ts.is_lane_thread(TRAVEL_CHANNEL, "1790000000.000500")

    @pytest.mark.parametrize("reason,reply", [("web_off", ts.WEB_OFF_REPLY),
                                              ("model_unsupported", ts.MODEL_REPLY),
                                              ("daily_cap", ts.CAP_REPLY)])
    def test_a_gate_refusal_is_ledgered_shape_only_and_routing_inert(self, reason, reply):
        client = _slack_client()
        self._exec(ts.Route("reply", reply, reason), client)
        assert client.chat_postMessage.call_args.kwargs["text"] == reply
        (row,) = _rows()
        assert row == {"ts": row["ts"], "event": "refused", "channel": TRAVEL_CHANNEL, "root_ts": "",
                       "stage": "gate", "reason": reason}
        assert not ts.is_lane_thread(TRAVEL_CHANNEL, "1790000000.000500")

    @pytest.mark.parametrize("route", [ts.Route("reply", ts.CLARIFY_DATES_REPLY, "clarify_dates"),
                                       ts.Route("reply", ts.FOLLOWUP_HELP_REPLY, "followup_help"),
                                       ts.Route("reply", ts.OFF_REPLY, "lane_off")])
    def test_a_non_refusal_reply_writes_nothing(self, route):
        self._exec(route, _slack_client())
        assert _rows() == []

    def test_a_search_acks_registers_and_submits(self):
        client = _slack_client()
        route = ts.Route("search", constraints=_constraints(), budget=3)
        submitted = []
        ts.execute_route(route, channel_id=TRAVEL_CHANNEL, thread_root_ts="1790000000.000500",
                         entity="FNDR", user_id=HARRISON, client=client, say=MagicMock(),
                         submit=lambda fn, *a, **k: submitted.append((fn, a, k)) or True,
                         now=NOW, ask_ts="1790000000.000500")     # a top-level ask
        assert client.chat_postMessage.call_args.kwargs["text"] == ts.ACK_TEXT
        (fn, args, kw), = submitted
        assert fn is ts._search_job and args[1] == _constraints()
        assert args[0]["tools"][0]["max_uses"] == 3 and kw["root_ts"] == "1790000000.000500"
        (row,) = _rows()
        assert row["event"] == "asked" and row["constraints"] == _constraints().to_record()
        assert row["registered"] is True
        assert ts.is_lane_thread(TRAVEL_CHANNEL, "1790000000.000500", now=NOW)

    def test_no_thread_root_threads_the_card_under_the_ack(self):
        client = _slack_client()
        subs = self._exec(ts.Route("search", constraints=_constraints(), budget=4), client, root=None)
        ack_ts = "1790000000.000001"
        assert "thread_ts" not in client.chat_postMessage.call_args.kwargs
        assert subs[0][2]["root_ts"] == ack_ts and _rows()[0]["root_ts"] == ack_ts

    def test_a_belt_refusal_is_ledgered_and_nothing_is_submitted(self, monkeypatch):
        real = ts.build_request

        def tampered(c, **kw):
            r = real(c, **kw)
            r["messages"][0]["content"] += " Jordan Riverstone"
            return r

        monkeypatch.setattr(ts, "build_request", tampered)
        client = _slack_client()
        subs = self._exec(ts.Route("search", constraints=_constraints(), budget=4), client)
        assert subs == []
        assert client.chat_postMessage.call_args.kwargs["text"] == ts.BELT_REPLY
        (row,) = _rows()
        assert row == {**row, "event": "belt_refused", "reason": "content"}
        assert "Jordan" not in json.dumps(row)

    def test_a_build_crash_is_a_refusal_not_silence(self, monkeypatch):
        monkeypatch.setattr(ts, "build_request", lambda *a, **k: (_ for _ in ()).throw(KeyError("x")))
        client = _slack_client()
        subs = self._exec(ts.Route("search", constraints=_constraints(), budget=4), client)
        assert subs == []
        assert client.chat_postMessage.call_args.kwargs["text"] == ts.BELT_REPLY
        assert [(r["event"], r["reason"]) for r in _rows()] == [("belt_refused", "build_error")]

    def test_a_refused_submit_says_so(self):
        client = _slack_client()
        self._exec(ts.Route("search", constraints=_constraints(), budget=4), client,
                   submit=lambda *a, **k: False)
        assert client.chat_postMessage.call_args.kwargs["text"] == ts.START_FAILED_REPLY
        assert [r["event"] for r in _rows()] == ["asked", "search_failed"]


class TestSearchJob:
    def _job(self, responses, client):
        ts._search_job(ts.build_request(_constraints(), max_uses=4, model=MODEL), _constraints(),
                       channel_id=TRAVEL_CHANNEL, root_ts="1790000000.000700", entity="FNDR",
                       user_id=HARRISON, budget=4, client=client,
                       client_factory=lambda: FakeAnthropic(responses))

    def test_ok_posts_one_card_with_constant_text_and_no_unfurls(self):
        client = _slack_client()
        self._job([_msg(_fx())], client)
        kw = client.chat_postMessage.call_args.kwargs
        assert kw["thread_ts"] == "1790000000.000700" and kw["unfurl_links"] is False
        assert kw["unfurl_media"] is False
        assert kw["text"] == ts.card_text(_constraints(), 5)
        assert len([b for b in kw["blocks"] if b["type"] == "section"]) == 6
        (row,) = _rows()
        assert row["event"] == "posted" and row["options"] == 5 and row["searches"] == 4
        rows = [json.loads(l) for l in web_guard._USAGE_LEDGER.read_text(encoding="utf-8").splitlines()]
        assert any(r.get("event") == "attach" and r.get("reason") == "travel_shortlist" for r in rows)

    @pytest.mark.parametrize("responses,line,err", [
        ([anthropic.APITimeoutError(request=MagicMock())], ts.SEARCH_FAILED_REPLY, "api_error:APITimeoutError"),
        (["UNREADABLE"], ts.UNREADABLE_REPLY, "unreadable"),
    ])
    def test_failures_get_one_honest_line(self, responses, line, err):
        if responses == ["UNREADABLE"]:
            d = _fx()
            d["content"][_fence_index(d)]["text"] = "no fence here"
            responses = [_msg(d)]
        client = _slack_client()
        self._job(responses, client)
        assert client.chat_postMessage.call_args.kwargs["text"] == line
        (row,) = _rows()
        assert row["event"] == "search_failed" and row["error"] == err

    def test_a_failed_card_post_is_its_own_outcome(self):
        client = _slack_client()
        calls = {"n": 0}

        def post(**kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("slack down")
            return _slack_resp({"ok": True, "ts": "1.2"})

        client.chat_postMessage.side_effect = post
        self._job([_msg(_fx())], client)
        assert client.chat_postMessage.call_args.kwargs["text"] == ts.POST_FAILED_REPLY
        assert [r["event"] for r in _rows()] == ["post_failed"]

    def test_a_crash_inside_the_job_still_posts_a_line(self, monkeypatch):
        monkeypatch.setattr(ts, "run_search", lambda *a, **k: (_ for _ in ()).throw(ValueError("x")))
        client = _slack_client()
        self._job([], client)
        assert client.chat_postMessage.call_args.kwargs["text"] == ts.SEARCH_FAILED_REPLY
        assert _rows()[0]["error"] == "job_error"


class TestImportLight:
    def test_module_does_not_import_anthropic_or_slack_at_import(self):
        import ast
        tree = ast.parse(Path(ts.__file__).read_text(encoding="utf-8"))
        top = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                top.add(node.module.split(".")[0])
        assert not (top & {"anthropic", "slack_sdk", "slack_bolt"}), top
