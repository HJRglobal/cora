"""Code #16 C2 -- D-051 round-1 regressions for the travel lane's predicates, parser,
routing and wiring (fixer B1).

Every test drives the REAL code path (the pure predicates on Slack WIRE text, or the
real handle_mention / handle_message_event / _dispatch_qa entry points with only
infrastructure stubbed, via the wiring module's ``lane`` fixture). The guest name is
SYNTHETIC ("Jordan Riverstone"); the loyalty number is a made-up digit run.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import cora.app as app_module
from cora import active_thread_store, web_guard
from cora import travel_shortlist as ts
from cora.model_router import MODEL_SONNET
from test_travel_shortlist import MUST_FIRE, _slack_client
from test_travel_shortlist_wiring import (  # noqa: F401 -- `lane` is a fixture
    ASK, ASK_TS, TRAVEL_CHANNEL, _drain, _drive_dispatch, _mention, _tessa, _web_rows, lane,
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
