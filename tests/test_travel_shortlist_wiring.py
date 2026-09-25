"""Code #16 C2 -- the travel shortlist lane driven through the REAL entry points.

A feature whose entry point is untested is untested (lessons 25 / 38): every test
here goes through the real ``handle_mention`` -> ``_dispatch_qa`` path or the real
``handle_message_event`` (im) -> ``_handle_dm_qa`` -> ``_dispatch_qa`` path, with
only infrastructure stubbed (rate limiter, channel-name resolution, bot id, history
fetches, the model call and the context loaders) and the lane's Anthropic client
injected from the live-shape fixture. The pooled search runs on the conftest-swapped
app._TRAVEL_SHORTLIST_POOL and is drained before assertions.

Covered: the lane intercepts a dated ask and posts ONE threaded card (channel +
both DM surfaces); a live gap ask cannot swallow a travel DM; the scheduler's
"availability" keyword cannot either; the review's must-not-fire examples VERBATIM
through the real handlers (the lane never fires, and the model turn they reach
carries no web tools); the B1 withhold on the real _dispatch_qa under the kill
switch, EVAL_MODE, an off-surface user and a lane-thread follow-up; the card's
text never feeds a later turn a web string (B4); the missed-message catch-up
drafts the fixed line; AST pins on the wiring; the listener table.
"""

from __future__ import annotations

import ast
import contextlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import cora.app as app_module
from cora import org_roles, web_guard
from cora import travel_shortlist as ts
from test_travel_shortlist import FakeAnthropic, _fx, _msg, _slack_client

# The REAL history readers, captured at import (the lane fixture stubs them per test).
_REAL_THREAD_HISTORY = app_module._fetch_thread_history
_REAL_DM_HISTORY = app_module._fetch_dm_history
_PLACEHOLDERS = (":thought_balloon: thinking…", ":mag: searching the web…")

HARRISON = "U0B2RM2JYJ1"
TRAVEL_CHANNEL = "C0C145H84JZ"
TRAVEL_CHANNEL_NAME = "hjr-travel-booking"
ASK_TS = "1790000000.000100"
ASK = ("can you search for hotels and airbnbs in the mesa/gilbert/scottsdale area for "
       "october 17th-october 21 for about 4 people, $300-$400/night, king bed, modern")
GOOGLE_PII = ("google hotels in scottsdale oct 17-21 for me, Tessa and the crew, use our "
              "Hilton Honors 123456789")
REVIEW_MUST_NOT_FIRE = [   # design-review.md lens 3 finding 4, VERBATIM
    "what's our last resort if the Oct 17 pallet misses?",
    "how much did we spend on equipment rentals in Scottsdale in September?",
    "renew the Adobe suite",
    "the hotel was great",
    "Tessa booked the Scottsdale hotel for Oct 17-21, add it to my calendar",
    "remember the Scottsdale hotel for Oct 17-21 is the Hilton",
    "pull up my emails about the Scottsdale hotel for Oct 17-21",
    "have a code session build hotel filters for Scottsdale Oct 17-21",
]


def _tessa() -> str:
    rec = org_roles.find_by_handle("tessa")
    assert rec is not None and rec.slack_id
    return rec.slack_id


def _drain():
    app_module._TRAVEL_SHORTLIST_POOL.shutdown(wait=True)


def _rows() -> list[dict]:
    p = ts.threads_path()
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def _web_rows() -> list[dict]:
    p = web_guard._USAGE_LEDGER
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


@pytest.fixture
def lane(monkeypatch):
    """The lane's Anthropic client comes from the live fixture; the MODEL must never
    be called on a lane turn (it raises)."""
    fake = FakeAnthropic([_msg(_fx())])
    monkeypatch.setattr(ts, "_CLIENT_FACTORY", lambda: fake)
    monkeypatch.setattr(app_module.rate_limiter, "check", lambda *a, **k: (True, None))
    monkeypatch.setattr(app_module, "_resolve_bot_user_id", lambda client: "UBOT")
    monkeypatch.setattr(app_module, "_fetch_thread_history", lambda *a, **k: [])
    monkeypatch.setattr(app_module, "_fetch_dm_history", lambda *a, **k: [])
    monkeypatch.setattr(app_module.osn_shift_handler, "get_dm_state", lambda uid: {"step": "idle"})
    monkeypatch.setattr(app_module.knowledge_check, "enabled", lambda: False)
    monkeypatch.setattr(app_module.decision_alerts, "match_alert_reply", lambda *a, **k: None)
    monkeypatch.setattr(app_module.gap_autofill, "has_live_ask", lambda uid: False)
    monkeypatch.setattr(app_module.gap_autofill, "match_pending_ask", lambda *a, **k: None)

    def _no_model(*_a, **_k):
        raise AssertionError("the model was called on a travel-lane turn")

    monkeypatch.setattr(app_module, "generate_response", _no_model)
    monkeypatch.setattr(app_module, "generate_response_streaming", _no_model, raising=False)
    return fake


def _mention(client, say, text, *, user, ts_=ASK_TS, thread_ts=None, channel=TRAVEL_CHANNEL,
             name=TRAVEL_CHANNEL_NAME):
    ev = {"type": "app_mention", "channel": channel, "user": user,
          "text": f"<@UBOT> {text}", "ts": ts_}
    if thread_ts:
        ev["thread_ts"] = thread_ts
    with patch.object(app_module, "_resolve_channel_name", return_value=name):
        app_module.handle_mention(ev, say, client)


def _dm(client, text, *, user, ts_=ASK_TS, thread_ts=None, channel="D0TRAVELDM"):
    ev = {"type": "message", "channel_type": "im", "channel": channel, "user": user,
          "text": text, "ts": ts_}
    if thread_ts:
        ev["thread_ts"] = thread_ts
    app_module.handle_message_event(ev, client)


def _card_call(client):
    calls = [c.kwargs for c in client.chat_postMessage.call_args_list if c.kwargs.get("blocks")]
    assert len(calls) == 1, calls
    return calls[0]


# ── the lane through the real entry points ───────────────────────────────────

class TestLaneThroughRealHandlers:
    def test_channel_mention_posts_ack_then_one_threaded_card(self, lane):
        client, say = _slack_client(), MagicMock()
        _mention(client, say, ASK, user=_tessa())
        _drain()
        texts = [c.kwargs["text"] for c in client.chat_postMessage.call_args_list]
        assert texts[0] == ts.ACK_TEXT
        card = _card_call(client)
        assert card["channel"] == TRAVEL_CHANNEL and card["thread_ts"] == ASK_TS
        assert card["unfurl_links"] is False and card["unfurl_media"] is False
        c = ts.parse_constraints(ASK).constraints
        assert card["text"] == ts.card_text(c, 5)
        say.assert_not_called()
        assert [r["event"] for r in _rows()] == ["asked", "posted"]
        assert _rows()[0]["root_ts"] == ASK_TS
        # the lane's ONE outbound request carried only the parsed fields
        (call,) = lane.calls
        assert call["messages"] == [{"role": "user", "content": ts.render_request_text(c)}]

    def test_tessas_dm_posts_the_card_threaded_under_her_ask(self, lane):
        client = _slack_client()
        _dm(client, "find hotels in the mesa/gilbert/scottsdale area for oct 17-21 for 4 people",
            user=_tessa())
        _drain()
        card = _card_call(client)
        assert card["channel"] == "D0TRAVELDM" and card["thread_ts"] == ASK_TS

    def test_harrisons_dm_runs_the_lane(self, lane):
        client = _slack_client()
        _dm(client, ASK, user=HARRISON)
        _drain()
        assert _card_call(client)["thread_ts"] == ASK_TS

    def test_a_live_gap_ask_cannot_swallow_a_travel_dm(self, lane, monkeypatch):
        """A non-question imperative passes the generic capture test; without the
        exclusion the greedy top-level gap capture would file it as an answer."""
        captured = []
        monkeypatch.setattr(app_module.gap_autofill, "has_live_ask", lambda uid: True)

        def _match(user_id, thread_ts, allow_toplevel=False):
            if allow_toplevel:
                captured.append(user_id)
                return {"ask_id": "gap-1"}
            return None

        monkeypatch.setattr(app_module.gap_autofill, "match_pending_ask", _match)
        monkeypatch.setattr(app_module.gap_autofill, "record_ask_answer",
                            lambda *a, **k: pytest.fail("the travel DM was captured as a gap answer"))
        client = _slack_client()
        _dm(client, "find hotels in the mesa/gilbert/scottsdale area for oct 17-21 for 4 people",
            user=_tessa())
        _drain()
        assert captured == []
        _card_call(client)

    def test_the_schedulers_availability_keyword_cannot_take_a_travel_dm(self, lane, monkeypatch):
        handled = []
        monkeypatch.setattr(app_module.osn_shift_handler, "handle_dm",
                            lambda **k: handled.append(k))
        client = _slack_client()
        _dm(client, "find hotels with availability in scottsdale oct 17-21", user=HARRISON)
        _drain()
        assert handled == []
        _card_call(client)
        # ...but a user MID-FLOW in the scheduler stays there
        monkeypatch.setattr(app_module.osn_shift_handler, "get_dm_state",
                            lambda uid: {"step": "collecting_days"})
        _dm(_slack_client(), "find hotels with availability in scottsdale oct 17-21",
            user=HARRISON, ts_="1790000000.000200")
        assert len(handled) == 1

    def test_off_surface_turns_never_reach_the_lane(self, lane, monkeypatch):
        """The same dated ask in another channel, and in a member's DM, runs the
        REAL _dispatch_qa to the ordinary model turn -- with web tools withheld."""
        fired = []
        monkeypatch.setattr(ts, "execute_route", lambda *a, **k: fired.append(1))
        seen = _drive_dispatch(ASK, user=HARRISON, channel_id="C0F3ESALES",
                               channel_name="f3e-sales", entity="F3E")
        seen += _drive_dispatch(ASK, user="U0B3RU5Q55G", channel_id="D0MEMBER",
                                channel_name="dm", entity="F3E")
        assert fired == [] and _rows() == []
        assert len(seen) == 2 and all(kw.get("web_tools") is False for kw in seen)

    def test_kill_switch_off_replies_and_never_calls_the_model(self, lane, monkeypatch):
        monkeypatch.setenv("CORA_TRAVEL_SHORTLIST", "off")
        client = _slack_client()
        _mention(client, MagicMock(), ASK, user=_tessa())
        _drain()
        (call,) = client.chat_postMessage.call_args_list
        assert call.kwargs["text"] == ts.OFF_REPLY and call.kwargs["thread_ts"] == ASK_TS
        assert lane.calls == [] and _rows() == []

    def test_a_lane_thread_follow_up_re_runs_on_merged_fields(self, lane, monkeypatch):
        client = _slack_client()
        _mention(client, MagicMock(), ASK, user=_tessa())
        _drain()
        # the first drain shut the per-test pool; the follow-up needs a live one
        monkeypatch.setattr(app_module, "_TRAVEL_SHORTLIST_POOL", ThreadPoolExecutor(max_workers=1))
        lane._responses.append(_msg(_fx()))
        client2 = _slack_client()
        _mention(client2, MagicMock(), "same dates but 6 people", user=_tessa(),
                 ts_="1790000000.000300", thread_ts=ASK_TS)
        _drain()
        assert _card_call(client2)["thread_ts"] == ASK_TS
        assert "Guests: 6." in lane.calls[-1]["messages"][0]["content"]
        # "book the second one" is deterministic -- never the model
        client3 = _slack_client()
        _mention(client3, MagicMock(), "book the second one", user=_tessa(),
                 ts_="1790000000.000400", thread_ts=ASK_TS)
        (call,) = client3.chat_postMessage.call_args_list
        assert call.kwargs["text"] == ts.FOLLOWUP_HELP_REPLY


# ── the model path: must-not-fire + the B1 withhold ──────────────────────────

@contextlib.contextmanager
def _model_path(seen: list):
    """Stub the model and the context loaders so the REAL _dispatch_qa runs to the
    model call; record what reached it (no network, no ledger writes)."""
    def fake_generate(*_a, meta=None, **kw):
        seen.append(kw)
        if meta is not None:
            meta["used_tools"] = False
        return "An answer."

    posts = {"n": 0}
    cache = MagicMock()
    cache.lookup.return_value = None
    hints = SimpleNamespace(bypass_cache=True, skip_kb=True, kb_k_override=None, cache_ttl=300)
    with contextlib.ExitStack() as st:
        p = st.enter_context
        p(patch.object(app_module, "generate_response", side_effect=fake_generate))
        p(patch.object(app_module.ic, "classify", return_value="qa"))
        p(patch.object(app_module.ic, "routing_hints", return_value=hints))
        p(patch.object(app_module.sc, "get_cache", return_value=cache))
        p(patch.object(app_module.kb_embeddings, "embed_query", return_value=[0.0] * 8))
        p(patch.object(app_module, "load_context_parts", return_value=("static", "kb")))
        p(patch.object(app_module, "_build_grant_context", return_value="grant"))
        p(patch.object(app_module, "load_prompt", return_value="sys"))
        p(patch.object(app_module.model_router, "choose_model", return_value="model-x"))
        p(patch.object(app_module.user_identity, "display_name", return_value="Someone"))
        p(patch.object(app_module.user_identity, "get_user", return_value=None))
        p(patch.object(app_module.knowledge_check, "recall_ask_note", return_value=""))
        p(patch.object(app_module._tool_dispatch, "describe_live_pendings", return_value=""))
        p(patch.object(app_module.active_thread_store, "register"))
        p(patch.object(app_module, "_extract_and_log_gap", side_effect=lambda t, *a, **k: t))
        yield


def _say_no_placeholder():
    """A say() whose PLACEHOLDER post fails (-> the non-streaming model path); every
    other post (a deterministic reply, the answer) succeeds."""
    def say(**kw):
        if kw.get("text") in _PLACEHOLDERS:
            raise RuntimeError("no placeholder")
        return {"ok": True}
    return say


class TestMustNotFireThroughRealHandlers:
    @pytest.mark.parametrize("text", REVIEW_MUST_NOT_FIRE)
    def test_channel_mention(self, lane, monkeypatch, text):
        fired = []
        monkeypatch.setattr(ts, "execute_route", lambda *a, **k: fired.append(1))
        seen: list = []
        said: list = []
        base = _say_no_placeholder()
        with _model_path(seen):
            _mention(_slack_client(), lambda **kw: said.append(kw) or base(**kw), text,
                     user=HARRISON)
        assert fired == [] and _rows() == []
        # the turn was answered by the ordinary pipeline (the model, or a deterministic
        # gate such as the Tier-2 DM redirect for "pull up my emails ...")
        assert seen or said
        assert all(kw.get("web_tools") is False for kw in seen)

    @pytest.mark.parametrize("text", REVIEW_MUST_NOT_FIRE)
    def test_harrison_dm(self, lane, monkeypatch, text):
        fired = []
        monkeypatch.setattr(ts, "execute_route", lambda *a, **k: fired.append(1))
        seen: list = []
        client = _slack_client()
        client.chat_postMessage.side_effect = None
        client.chat_postMessage.return_value = {"ok": True}
        with _model_path(seen):
            _dm(client, text, user=HARRISON)
        assert fired == [] and _rows() == []
        assert all(kw.get("web_tools") is False for kw in seen)


def _drive_dispatch(text, *, user, channel_id, channel_name, entity="FNDR", root=None):
    seen: list = []
    with _model_path(seen):
        app_module._dispatch_qa(
            channel_id=channel_id, channel_name=channel_name, user_id=user,
            user_message=text, reply_thread_ts=root or ASK_TS, entity=entity,
            client=_slack_client(), say=_say_no_placeholder(), prior_messages=[],
            root_thread_ts=root or ASK_TS)
    return seen


class TestB1WithholdOnTheRealDispatch:
    """B11: the real _dispatch_qa, each turn 'google hotels in scottsdale oct 17-21 ...'
    (explicit web intent -- web_guard WOULD attach), asserting web_tools=False and the
    ledgered gate_skipped:travel_lane."""

    def _assert_withheld(self, seen):
        assert seen and all(kw.get("web_tools") is False for kw in seen)
        assert any(r.get("reason") == "gate_skipped:travel_lane" for r in _web_rows())

    def test_kill_switch_off(self, lane, monkeypatch):
        monkeypatch.setenv("CORA_TRAVEL_SHORTLIST", "off")
        self._assert_withheld(_drive_dispatch(GOOGLE_PII, user=_tessa(), channel_id=TRAVEL_CHANNEL,
                                              channel_name=TRAVEL_CHANNEL_NAME))

    def test_eval_mode(self, lane, monkeypatch):
        monkeypatch.setenv("CORA_EVAL_MODE", "1")
        self._assert_withheld(_drive_dispatch(GOOGLE_PII, user=_tessa(), channel_id=TRAVEL_CHANNEL,
                                              channel_name=TRAVEL_CHANNEL_NAME))

    def test_off_surface_user(self, lane):
        self._assert_withheld(_drive_dispatch(GOOGLE_PII, user="U0B3RU5Q55G", channel_id="C0F3ESALES",
                                              channel_name="f3e-sales", entity="F3E"))

    def test_lane_thread_follow_up_that_reaches_the_model(self, lane):
        ts.append_event("asked", channel=TRAVEL_CHANNEL, root_ts=ASK_TS,
                        constraints=ts.parse_constraints(ASK).constraints.to_record())
        # no lodging noun at all: only the thread-root leg can withhold it
        self._assert_withheld(_drive_dispatch(
            "google the reviews for the second one and add it to my calendar",
            user=_tessa(), channel_id=TRAVEL_CHANNEL, channel_name=TRAVEL_CHANNEL_NAME))

    def test_lane_thread_google_pii_is_answered_by_the_lane_on_fields_only(self, lane):
        ts.append_event("asked", channel=TRAVEL_CHANNEL, root_ts=ASK_TS,
                        constraints=ts.parse_constraints(ASK).constraints.to_record())
        seen = _drive_dispatch(GOOGLE_PII, user=_tessa(), channel_id=TRAVEL_CHANNEL,
                               channel_name=TRAVEL_CHANNEL_NAME)
        _drain()
        assert seen == []                         # never the model
        blob = json.dumps(lane.calls).lower()
        for tok in ("tessa", "hilton", "honors", "123456789", "crew"):
            assert tok not in blob, tok

    def test_a_non_lodging_turn_still_attaches(self, lane):
        """The withhold is scoped: an explicit web ask with no lodging noun, outside a
        lane thread, still carries web tools."""
        seen = _drive_dispatch("google the latest Arizona heat advisory news", user=_tessa(),
                               channel_id=TRAVEL_CHANNEL, channel_name=TRAVEL_CHANNEL_NAME)
        assert seen and seen[-1].get("web_tools") is True

    def test_an_unreadable_lane_store_withholds(self, lane, monkeypatch, tmp_path):
        d = tmp_path / "store-is-a-dir"
        d.mkdir()
        monkeypatch.setenv("CORA_TRAVEL_SHORTLIST_THREADS_PATH", str(d))
        seen = _drive_dispatch("google the latest Arizona heat advisory news", user=_tessa(),
                               channel_id=TRAVEL_CHANNEL, channel_name=TRAVEL_CHANNEL_NAME)
        assert seen and seen[-1].get("web_tools") is False


# ── B4: the card never feeds a later turn a web string ───────────────────────

class TestHistoryReadersSeeOnlyTheConstantText:
    def _posted(self, lane):
        client = _slack_client()
        _mention(client, MagicMock(), ASK, user=_tessa())
        _drain()
        return _card_call(client)

    def test_thread_and_dm_history(self, lane, monkeypatch):
        monkeypatch.setattr(app_module, "_CORA_BOT_USER_ID", "UBOT", raising=False)
        card = self._posted(lane)
        web_strings = []
        for b in card["blocks"][1:-1]:
            web_strings.append(b["text"]["text"])
        msgs = [{"ts": ASK_TS, "user": _tessa(), "text": f"<@UBOT> {ASK}"},
                {"ts": "1790000000.000900", "bot_id": "B1", "user": "UBOT", "text": card["text"],
                 "blocks": card["blocks"]}]
        client = MagicMock()
        client.conversations_replies.return_value = {"messages": msgs}
        client.conversations_history.return_value = {"messages": list(reversed(msgs))}
        thread = _REAL_THREAD_HISTORY(client, TRAVEL_CHANNEL, ASK_TS, "1790000000.009")
        dm = _REAL_DM_HISTORY(client, "D0X", "1790000000.009")
        assert thread and dm
        for hist in (thread, dm):
            blob = json.dumps(hist)
            assert card["text"] in blob or json.dumps(card["text"])[1:-1] in blob
            for s in web_strings:
                assert s not in blob
            for opt in ("Hotel Valley Ho", "airbnb.com", "Tempur-Pedic", "photogenic stay"):
                assert opt not in blob, opt


# ── missed-message catch-up drafts the fixed line (B2) ───────────────────────

class TestMissedMessageCatchup:
    def test_the_catch_up_draft_is_the_fixed_eval_line_and_nothing_posts(self, lane):
        from cora import missed_message_catchup as mmc
        cand = mmc.Candidate(channel_id="D0HDM", channel_name="dm", is_dm=True, user_id=HARRISON,
                             text=ASK, event_ts=ASK_TS, reply_thread_ts=None,
                             root_thread_ts=ASK_TS, detection_tier="dm")
        client = MagicMock()
        client.conversations_history.return_value = {"messages": []}
        draft = mmc._run_dispatch_capture(client, cand, "FNDR", True)
        assert draft == ts.EVAL_REPLY
        client.chat_postMessage.assert_not_called()
        assert lane.calls == [] and _rows() == []


# ── wiring pins (AST, never a text grep) + the listener table ────────────────

_APP_PATH = Path(app_module.__file__)


def _func(name: str) -> ast.FunctionDef:
    tree = ast.parse(_APP_PATH.read_text(encoding="utf-8"))
    return next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)


def _call_lines(fn: ast.AST, attr: str) -> list[int]:
    out = []
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            f = n.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if name == attr:
                out.append(n.lineno)
    return sorted(out)


class TestWiringPins:
    def test_intercept_sits_after_the_confirm_interceptor_and_before_cache_and_model(self):
        fn = _func("_dispatch_qa")
        lane = _call_lines(fn, "route_turn")
        assert len(lane) == 1
        assert _call_lines(fn, "try_confirm_pending_write")[0] < lane[0]
        assert lane[0] < _call_lines(fn, "describe_live_pendings")[0]
        assert lane[0] < _call_lines(fn, "get_cache")[0]
        assert lane[0] < min(_call_lines(fn, "generate_response"))
        assert len(_call_lines(fn, "execute_route")) == 1

    def test_the_web_gate_leg_and_the_web_clean_preflight(self):
        fn = _func("_dispatch_qa")
        skips = [n for n in ast.walk(fn) if isinstance(n, ast.Assign)
                 and any(getattr(t, "id", "") == "web_gate_skip" for t in n.targets)
                 and isinstance(n.value, ast.Constant) and n.value.value == "travel_lane"]
        assert len(skips) == 1
        gate_if = next(n for n in ast.walk(fn) if isinstance(n, ast.If)
                       and isinstance(n.test, ast.Name) and n.test.id == "_travel_web_withhold")
        assert gate_if.body[0] is skips[0]
        clean = next(n for n in ast.walk(fn) if isinstance(n, ast.Assign)
                     and any(getattr(t, "id", "") == "web_clean" for t in n.targets)
                     and isinstance(n.value, ast.BoolOp))
        negs = [v for v in clean.value.values if isinstance(v, ast.UnaryOp)
                and isinstance(v.op, ast.Not) and getattr(v.operand, "id", "") == "_travel_web_withhold"]
        assert len(negs) == 1

    def test_the_dm_capture_exclusion(self):
        fn = _func("handle_message_event")
        gen = next(n for n in ast.walk(fn) if isinstance(n, ast.Assign)
                   and any(getattr(t, "id", "") == "_generic_intent_ok" for t in n.targets))
        names = [getattr(v.operand, "id", "") for v in gen.value.values
                 if isinstance(v, ast.UnaryOp) and isinstance(v.op, ast.Not)]
        assert "_travel_dm_ask" in names

    def test_the_pool_is_one_worker_and_off_the_listener_pool(self):
        pool = app_module._TRAVEL_SHORTLIST_POOL
        assert pool._max_workers == 1
        assert pool._thread_name_prefix == "travel-shortlist"

    def test_the_handlers_are_still_the_registered_listeners(self):
        names = set()
        for listener in app_module.app._listeners:
            fn = getattr(listener, "ack_function", None)
            if fn is not None:
                names.add(getattr(fn, "__name__", ""))
        assert {"handle_mention", "handle_message_event"} <= names
        for helper in ("_travel_forced_tool_turn", "_travel_lane_thread_state",
                       "_travel_dm_ask_intent", "_travel_ask_escapes_shift_keywords",
                       "_submit_travel_shortlist"):
            assert helper not in names, f"a decorator was orphaned onto {helper}"


class TestPooledSearchCannotOutliveItsTest:
    """The ordering proof (copied from test_cq_kickoff_pool_isolation): the pooled
    body blocks until the CLASS-scoped probe's teardown releases it (bounded 1 s),
    i.e. until after this test's whole teardown -- monkeypatch undo included --
    unless the conftest drain finishes it first. With the drain it runs while the
    test's thread-store redirect and env are still in place."""

    @pytest.fixture(scope="class")
    def probe(self):
        import threading
        rec: dict = {"release": threading.Event(), "done": threading.Event()}
        yield rec
        finished_before_undo = rec["done"].is_set()
        rec["release"].set()
        rec["done"].wait(10)
        assert finished_before_undo, "a pooled travel search outlived its test's teardown"
        assert rec.get("seen_path") == rec["expected_path"], rec.get("seen_path")
        assert rec.get("seen_env") == "inside"

    def test_a_pooled_search_body_finishes_inside_this_tests_redirects(self, probe, monkeypatch):
        import os
        monkeypatch.setenv("TRAVEL_POOL_PROBE", "inside")
        expected = ts.threads_path()
        assert expected.resolve() != ts._DEFAULT_THREADS_PATH.resolve()
        probe["expected_path"] = expected

        def _body():
            try:
                probe["release"].wait(1.0)
                probe["seen_path"] = ts.threads_path()
                probe["seen_env"] = os.environ.get("TRAVEL_POOL_PROBE")
            finally:
                probe["done"].set()

        assert app_module._submit_travel_shortlist(_body) is True


class TestPoolIsolation:
    _seen: list = []   # holds the OBJECTS (a kept reference: no id reuse after GC)

    def test_each_test_gets_a_fresh_pool(self):
        assert isinstance(app_module._TRAVEL_SHORTLIST_POOL, ThreadPoolExecutor)
        TestPoolIsolation._seen.append(app_module._TRAVEL_SHORTLIST_POOL)

    def test_each_test_gets_a_fresh_pool_again(self):
        assert TestPoolIsolation._seen, "runs after the test above"
        assert all(app_module._TRAVEL_SHORTLIST_POOL is not p for p in TestPoolIsolation._seen)

    def test_a_refused_submit_returns_false(self, monkeypatch):
        class _Dead:
            def submit(self, *a, **k):
                raise RuntimeError("cannot schedule new futures after shutdown")
        monkeypatch.setattr(app_module, "_TRAVEL_SHORTLIST_POOL", _Dead())
        assert app_module._submit_travel_shortlist(lambda: None) is False
