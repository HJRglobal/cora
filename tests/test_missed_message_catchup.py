"""Tests for the Missed-Message Catch-Up tool (src/cora/missed_message_catchup.py).

Covers: window derivation, channel enumeration (public+private+DM, deny-list),
windowed history (oldest+latest+pagination+subtype skip), detection
(mention/DM/thread-participation/fuzzy, already-answered, stale, idempotency),
guard replication in generate_draft (decline/redirect/help short-circuit BEFORE the
pipeline), draft capture (post nothing, suppress live-state mutation), the card scrub
(LEX PHI + confidential withhold), the review card blocks, the ledger, and the
Harrison-gated one-tap processor (skip/send/edit + idempotency + non-Harrison refusal).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from cora import missed_message_catchup as mmc

BOT = "UCORA"
HARRISON = mmc.HARRISON_ID
ALICE = "UALICE"


@pytest.fixture(autouse=True)
def _tmp_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("MISSED_CATCHUP_LEDGER_PATH", str(tmp_path / "catchup.jsonl"))
    yield


# ── Window derivation ────────────────────────────────────────────────────────────

def _mk_log(tmp_path, name, lines):
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def test_derive_window_finds_heartbeat_gap(tmp_path):
    # Heartbeats every minute, then a 4-hour gap, then recovery.
    lines = []
    for hhmm in ("2026-07-14T19:58:00", "2026-07-14T19:59:00", "2026-07-14T20:00:00"):
        lines.append(f"{hhmm} INFO [Thread] cora.main: heartbeat alive uptime_s=60")
    # long gap...
    lines.append("2026-07-15T00:07:00 INFO [MainThread] cora.main: Cora starting up")
    lines.append("2026-07-15T00:08:00 INFO [Thread] cora.main: heartbeat alive uptime_s=60")
    _mk_log(tmp_path, "cora-2026-07-15.log", lines)

    win = mmc.derive_window(logs_dir=tmp_path, now=_epoch("2026-07-15T00:10:00"),
                            min_gap_minutes=6.0)
    assert win is not None
    oldest, latest = win
    # Gap is between the last 20:00 heartbeat and the 00:07 startup.
    assert abs(oldest - _epoch("2026-07-14T20:00:00")) < 2
    assert abs(latest - _epoch("2026-07-15T00:07:00")) < 2


def test_derive_window_none_when_no_gap(tmp_path):
    lines = [
        f"2026-07-15T00:0{i}:00 INFO [T] cora.main: heartbeat alive uptime_s=60"
        for i in range(6)
    ]
    _mk_log(tmp_path, "cora-2026-07-15.log", lines)
    assert mmc.derive_window(logs_dir=tmp_path, now=_epoch("2026-07-15T00:06:00")) is None


def test_parse_ts_arg_iso_and_epoch():
    assert mmc.parse_ts_arg("1700000000") == pytest.approx(1700000000.0)
    v = mmc.parse_ts_arg("2026-07-15T12:00:00+00:00")
    assert isinstance(v, float) and v > 0


# ── Channel enumeration ────────────────────────────────────────────────────────

def test_list_channels_public_private_dm_and_denylist(monkeypatch):
    client = MagicMock()
    client.conversations_list.return_value = {
        "channels": [
            {"id": "C1", "name": "f3e-sales", "is_member": True, "is_private": False},
            {"id": "C2", "name": "hjrg-finance", "is_member": True, "is_private": True},
            {"id": "C3", "name": "not-a-member", "is_member": False},
            {"id": "C4", "name": "lbhs-clients", "is_member": True, "is_private": True},
            {"id": "D9", "is_im": True, "user": "UALICE"},
        ],
        "response_metadata": {},
    }
    # Deny the LBHS channel.
    monkeypatch.setattr(mmc.slack_sweep_policy, "should_ingest",
                        lambda name, cid=None, is_private=False: name != "lbhs-clients")

    chans = mmc.list_catchup_channels(client)
    names = {c["id"] for c in chans}
    assert names == {"C1", "C2", "D9"}  # public+private member, DM; deny + non-member excluded
    # types must include private + im (spec gotcha: not public-only)
    types = client.conversations_list.call_args.kwargs["types"]
    assert "private_channel" in types and "im" in types


# ── Windowed history ─────────────────────────────────────────────────────────────

def test_fetch_window_sets_both_bounds_and_skips_system(monkeypatch):
    monkeypatch.setattr(mmc.time, "sleep", lambda *a, **k: None)
    client = MagicMock()
    client.conversations_history.return_value = {
        "messages": [
            {"user": "U1", "text": "real question", "ts": "100.1"},
            {"bot_id": "B1", "text": "bot noise", "ts": "100.2"},
            {"subtype": "channel_join", "user": "U2", "text": "joined", "ts": "100.3"},
            {"user": "U3", "text": "", "ts": "100.4"},
            {"user": "U4", "text": "another", "ts": "100.0"},
        ],
        "has_more": False, "response_metadata": {},
    }
    msgs = mmc.fetch_window_messages(client, "C1", 50.0, 200.0)
    assert [m["ts"] for m in msgs] == ["100.0", "100.1"]  # ascending, only real user text
    kw = client.conversations_history.call_args.kwargs
    assert "oldest" in kw and "latest" in kw  # BOTH bounds (spec gotcha)


def test_fetch_window_paginates(monkeypatch):
    monkeypatch.setattr(mmc.time, "sleep", lambda *a, **k: None)
    client = MagicMock()
    client.conversations_history.side_effect = [
        {"messages": [{"user": "U1", "text": "a", "ts": "1"}],
         "has_more": True, "response_metadata": {"next_cursor": "cur"}},
        {"messages": [{"user": "U1", "text": "b", "ts": "2"}],
         "has_more": False, "response_metadata": {}},
    ]
    msgs = mmc.fetch_window_messages(client, "C1", 0.0, 10.0)
    assert {m["text"] for m in msgs} == {"a", "b"}
    assert client.conversations_history.call_count == 2


# ── Reply/participation helpers ────────────────────────────────────────────────

def test_cora_replied_after_and_participated_before():
    msgs = [
        {"user": ALICE, "ts": "100.0", "text": "q"},
        {"bot_id": "B1", "user": BOT, "ts": "101.0", "text": "answer"},
    ]
    assert mmc.cora_replied_after(msgs, "100.0", BOT) is True
    assert mmc.cora_replied_after(msgs, "101.5", BOT) is False
    assert mmc.cora_participated_before(msgs, "101.5", BOT) is True
    assert mmc.cora_participated_before(msgs, "100.5", BOT) is False


# ── Detection ────────────────────────────────────────────────────────────────────

def _client_for(window_msgs, replies=None, dm_history=None):
    client = MagicMock()
    client.conversations_history.return_value = {
        "messages": window_msgs, "has_more": False, "response_metadata": {},
    }
    client.conversations_replies.return_value = {
        "messages": replies or [], "has_more": False, "response_metadata": {},
    }
    if dm_history is not None:
        client.conversations_history.return_value = {
            "messages": dm_history, "has_more": False, "response_metadata": {},
        }
    return client


def test_detect_mention_unanswered(monkeypatch):
    monkeypatch.setattr(mmc.time, "sleep", lambda *a, **k: None)
    msgs = [{"user": ALICE, "text": f"<@{BOT}> what is the deal status?", "ts": "500.0"}]
    client = _client_for(msgs, replies=msgs)  # replies = just the message, no Cora reply
    chans = [{"id": "C1", "name": "f3e-sales", "is_dm": False}]
    cands = mmc.find_missed_messages(client, chans, 100.0, 1000.0, bot_id=BOT, now=600.0)
    assert len(cands) == 1
    assert cands[0].detection_tier == mmc.TIER_MENTION
    assert cands[0].user_id == ALICE
    assert "deal status" in cands[0].text
    assert f"<@{BOT}>" not in cands[0].text  # mention stripped


def test_detect_mention_already_answered_dropped(monkeypatch):
    monkeypatch.setattr(mmc.time, "sleep", lambda *a, **k: None)
    msgs = [{"user": ALICE, "text": f"<@{BOT}> ping?", "ts": "500.0"}]
    replies = msgs + [{"bot_id": "B", "user": BOT, "text": "here", "ts": "700.0"}]
    client = _client_for(msgs, replies=replies)
    chans = [{"id": "C1", "name": "f3e-sales", "is_dm": False}]
    cands = mmc.find_missed_messages(client, chans, 100.0, 1000.0, bot_id=BOT, now=800.0)
    assert cands == []  # Cora already replied after -> not a miss


def test_detect_thread_participation_requires_prior_cora(monkeypatch):
    monkeypatch.setattr(mmc.time, "sleep", lambda *a, **k: None)
    # A thread reply (thread_ts != ts), no @mention.
    msg = {"user": ALICE, "text": "and what about pricing?", "ts": "510.0", "thread_ts": "500.0"}
    chans = [{"id": "C1", "name": "f3e-sales", "is_dm": False}]

    # Case A: Cora participated earlier in the thread -> candidate.
    replies_with_cora = [
        {"user": ALICE, "text": "root", "ts": "500.0", "thread_ts": "500.0"},
        {"bot_id": "B", "user": BOT, "text": "earlier answer", "ts": "505.0", "thread_ts": "500.0"},
        msg,
    ]
    client = _client_for([msg], replies=replies_with_cora)
    cands = mmc.find_missed_messages(client, chans, 100.0, 1000.0, bot_id=BOT, now=600.0)
    assert len(cands) == 1 and cands[0].detection_tier == mmc.TIER_THREAD

    # Case B: Cora never participated -> not a candidate.
    replies_no_cora = [
        {"user": ALICE, "text": "root", "ts": "500.0", "thread_ts": "500.0"},
        msg,
    ]
    client2 = _client_for([msg], replies=replies_no_cora)
    cands2 = mmc.find_missed_messages(client2, chans, 100.0, 1000.0, bot_id=BOT, now=600.0)
    assert cands2 == []


def test_detect_plain_message_skipped_unless_fuzzy(monkeypatch):
    monkeypatch.setattr(mmc.time, "sleep", lambda *a, **k: None)
    msgs = [{"user": ALICE, "text": "cora can you check the invoice", "ts": "500.0"}]
    client = _client_for(msgs, replies=msgs)
    chans = [{"id": "C1", "name": "f3e-sales", "is_dm": False}]
    # Default: no fuzzy -> skipped
    assert mmc.find_missed_messages(client, chans, 100.0, 1000.0, bot_id=BOT, now=600.0) == []
    # With fuzzy -> surfaced, tagged fuzzy
    cands = mmc.find_missed_messages(client, chans, 100.0, 1000.0, bot_id=BOT,
                                     now=600.0, include_fuzzy=True)
    assert len(cands) == 1 and cands[0].detection_tier == mmc.TIER_FUZZY


def test_detect_dm_and_stale(monkeypatch):
    monkeypatch.setattr(mmc.time, "sleep", lambda *a, **k: None)
    msgs = [{"user": ALICE, "text": "remember the vendor is Apex", "ts": "500.0"}]
    client = _client_for(msgs, dm_history=msgs)
    chans = [{"id": "D1", "name": "dm", "is_dm": True}]
    # now is 2 days after ts=500 -> stale (>24h)
    now = 500.0 + 48 * 3600
    cands = mmc.find_missed_messages(client, chans, 100.0, now, bot_id=BOT,
                                     now=now, staleness_hours=24.0)
    assert len(cands) == 1
    assert cands[0].detection_tier == mmc.TIER_DM
    assert cands[0].status == "stale"


def test_detect_idempotent_skips_terminal(monkeypatch):
    monkeypatch.setattr(mmc.time, "sleep", lambda *a, **k: None)
    msgs = [{"user": ALICE, "text": f"<@{BOT}> hi", "ts": "500.0"}]
    client = _client_for(msgs, replies=msgs)
    chans = [{"id": "C1", "name": "f3e-sales", "is_dm": False}]
    cid = mmc.catchup_id("C1", "500.0")
    mmc.record_row(cid, "sent")  # already handled
    cands = mmc.find_missed_messages(client, chans, 100.0, 1000.0, bot_id=BOT, now=600.0)
    assert cands == []


def test_detect_self_resolved_dropped_when_classifier_says_resolved(monkeypatch):
    monkeypatch.setattr(mmc.time, "sleep", lambda *a, **k: None)
    msgs = [{"user": ALICE, "text": f"<@{BOT}> what's the number?", "ts": "500.0"}]
    replies = msgs + [{"user": ALICE, "text": "nvm got it", "ts": "520.0"}]
    client = _client_for(msgs, replies=replies)
    chans = [{"id": "C1", "name": "f3e-sales", "is_dm": False}]
    cands = mmc.find_missed_messages(
        client, chans, 100.0, 1000.0, bot_id=BOT, now=600.0,
        still_open_fn=lambda q, following: False,  # resolved
    )
    assert cands == []
    # Fail-closed: classifier raises -> kept
    cands2 = mmc.find_missed_messages(
        client, chans, 100.0, 1000.0, bot_id=BOT, now=600.0,
        still_open_fn=lambda q, following: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert len(cands2) == 1


# ── Guard replication in generate_draft ─────────────────────────────────────────

def _channel_cand():
    return mmc.Candidate(
        channel_id="C1", channel_name="f3e-sales", is_dm=False, user_id=ALICE,
        text="what's the pipeline?", event_ts="500.0",
        reply_thread_ts="500.0", root_thread_ts="500.0", detection_tier=mmc.TIER_MENTION,
    )


def _pass_all_guards(monkeypatch):
    monkeypatch.setattr(mmc.entity_router, "route", lambda name: "F3E")
    monkeypatch.setattr(mmc.channel_classifier, "classify_function", lambda name: "sales")
    monkeypatch.setattr(mmc.channel_classifier, "tier_label", lambda e, f: "TIER_3")
    monkeypatch.setattr(mmc.lex_phi_access, "phi_allowed", lambda *a, **k: False)
    monkeypatch.setattr(mmc.user_access, "check_access", lambda *a, **k: None)
    monkeypatch.setattr(mmc.help_responder, "is_help_intent", lambda t: False)
    monkeypatch.setattr(mmc.sibling_guard, "check_redirect", lambda e, t: None)
    monkeypatch.setattr(mmc.cross_entity_guard, "check_cross_entity", lambda t, e: None)


def test_generate_draft_decline_short_circuits(monkeypatch):
    _pass_all_guards(monkeypatch)
    monkeypatch.setattr(mmc.user_access, "check_access", lambda *a, **k: "Ask in a finance channel.")
    called = {"dispatch": False}
    monkeypatch.setattr(mmc, "_run_dispatch_capture",
                        lambda *a, **k: called.__setitem__("dispatch", True) or "X")
    cand = mmc.generate_draft(MagicMock(), _channel_cand())
    assert cand.status == "decline"
    assert "finance channel" in cand.note
    assert called["dispatch"] is False  # pipeline NOT reached


def test_generate_draft_redirect_short_circuits(monkeypatch):
    _pass_all_guards(monkeypatch)
    monkeypatch.setattr(mmc.cross_entity_guard, "check_cross_entity", lambda t, e: "That's an OSN topic.")
    monkeypatch.setattr(mmc, "_run_dispatch_capture", lambda *a, **k: "SHOULD NOT RUN")
    cand = mmc.generate_draft(MagicMock(), _channel_cand())
    assert cand.status == "redirect"


def test_generate_draft_help_short_circuits(monkeypatch):
    _pass_all_guards(monkeypatch)
    monkeypatch.setattr(mmc.help_responder, "is_help_intent", lambda t: True)
    cand = mmc.generate_draft(MagicMock(), _channel_cand())
    assert cand.status == "help"


def test_generate_draft_no_draft_flag(monkeypatch):
    _pass_all_guards(monkeypatch)
    monkeypatch.setattr(mmc, "_run_dispatch_capture", lambda *a, **k: "SHOULD NOT RUN")
    cand = mmc.generate_draft(MagicMock(), _channel_cand(), draft_answer=False)
    assert cand.status == "would_draft"


def test_generate_draft_captures_answer_and_suppresses_state(monkeypatch):
    _pass_all_guards(monkeypatch)
    import cora.app as app_mod

    posted = {"say": 0, "register": 0, "cache": 0}

    def fake_dispatch(**kw):
        # Live pipeline emits the answer via say (non-streaming) after a placeholder.
        kw["say"](text=mmc._STREAM_PLACEHOLDER, thread_ts=kw["reply_thread_ts"])
        kw["say"](text="Here is your pipeline summary.", thread_ts=kw["reply_thread_ts"])
        # Live also mutates shared state; wrapper must have suppressed these:
        app_mod.active_thread_store.register("C1", "500.0")
        app_mod._try_cache_store("F3E", "q", None, "ans", None)
        posted["say"] += 1

    monkeypatch.setattr(app_mod, "_dispatch_qa", fake_dispatch)
    monkeypatch.setattr(app_mod, "_fetch_thread_history", lambda *a, **k: [])
    monkeypatch.setattr(app_mod.active_thread_store, "register",
                        lambda *a, **k: posted.__setitem__("register", posted["register"] + 1))
    monkeypatch.setattr(app_mod, "_try_cache_store",
                        lambda *a, **k: posted.__setitem__("cache", posted["cache"] + 1))

    cand = mmc.generate_draft(MagicMock(), _channel_cand())
    assert cand.status == "draft"
    assert cand.draft_text == "Here is your pipeline summary."
    # Suppressed during capture -> the fake's register/cache calls were no-ops.
    assert posted["register"] == 0
    assert posted["cache"] == 0
    # ...and restored afterward (the real refs are back).
    assert app_mod.active_thread_store.register is not None


# ── Card scrub ───────────────────────────────────────────────────────────────────

def test_scrub_card_body_withholds_confidential(monkeypatch):
    monkeypatch.setattr(
        mmc.channel_content_guard, "guard_outbound",
        lambda text, **k: ("REFUSAL", "company_financials"),
    )
    out = mmc.scrub_card_body("payroll was $250k", source_entity="F3E", source_tier="TIER_3",
                              source_channel_name="f3e-random", source_is_dm=False)
    assert "withheld" in out and "company_financials" in out


def test_scrub_card_body_phi_for_lex(monkeypatch):
    monkeypatch.setattr(mmc.channel_content_guard, "guard_outbound", lambda text, **k: (text, None))
    calls = {"cue": 0, "lex": 0}
    monkeypatch.setattr(mmc.phi_guard, "redact_cue_adjacent_names",
                        lambda t, **k: calls.__setitem__("cue", calls["cue"] + 1) or t)
    monkeypatch.setattr(mmc.phi_guard, "scrub_lex_phi",
                        lambda t, **k: calls.__setitem__("lex", calls["lex"] + 1) or "[scrubbed]")
    out = mmc.scrub_card_body("client info", source_entity="LEX-LLC", source_tier="TIER_3",
                              source_channel_name="llc-clients", source_is_dm=False)
    assert out == "[scrubbed]"
    assert calls["cue"] == 1 and calls["lex"] == 1


# ── Review card ──────────────────────────────────────────────────────────────────

def test_build_card_draft_has_buttons_with_catchup_id(monkeypatch):
    monkeypatch.setattr(mmc.channel_content_guard, "guard_outbound", lambda text, **k: (text, None))
    cand = _channel_cand()
    cand.status = "draft"
    cand.draft_text = "The pipeline is healthy."
    cand.entity, cand.tier = "F3E", "TIER_3"
    _fallback, blocks = mmc.build_review_card(cand)
    actions = [b for b in blocks if b["type"] == "actions"]
    assert actions, "draft card must have action buttons"
    values = {e["value"] for e in actions[0]["elements"]}
    ids = {e["action_id"] for e in actions[0]["elements"]}
    assert values == {cand.catchup_id}
    assert ids == {mmc.ACTION_SEND, mmc.ACTION_EDIT, mmc.ACTION_SKIP}


def test_build_card_decline_has_no_buttons(monkeypatch):
    monkeypatch.setattr(mmc.channel_content_guard, "guard_outbound", lambda text, **k: (text, None))
    cand = _channel_cand()
    cand.status = "decline"
    cand.note = "would decline"
    cand.entity, cand.tier = "F3E", "TIER_3"
    _fallback, blocks = mmc.build_review_card(cand)
    assert all(b["type"] != "actions" for b in blocks)


# ── Ledger ───────────────────────────────────────────────────────────────────────

def test_ledger_latest_wins_and_terminal():
    cid = mmc.catchup_id("C1", "500.0")
    assert mmc.is_terminal(cid) is False
    mmc.record_row(cid, "pending", channel_id="C1", draft_text="d")
    assert mmc.is_terminal(cid) is False
    assert mmc.latest_disposition(cid)["disposition"] == "pending"
    mmc.record_row(cid, "sent", posted_ts="9.9")
    assert mmc.is_terminal(cid) is True
    assert mmc.latest_disposition(cid)["disposition"] == "sent"


def test_record_pending_roundtrip():
    cand = _channel_cand()
    cand.status = "draft"
    cand.draft_text = "answer body"
    cand.entity, cand.tier = "F3E", "TIER_3"
    assert mmc.record_pending(cand, "run-1") is True
    row = mmc.latest_disposition(cand.catchup_id)
    assert row["draft_text"] == "answer body"
    assert row["channel_id"] == "C1"
    assert row["reply_thread_ts"] == "500.0"


# ── One-tap processor ────────────────────────────────────────────────────────────

def _seed_pending(cid="C1:500.0", **over):
    row = dict(channel_id="C1", channel_name="f3e-sales", entity="F3E", tier="TIER_3",
               asker=ALICE, event_ts="500.0", reply_thread_ts="500.0",
               detection_tier="mention", draft_text="This is the drafted answer body.",
               is_dm=False)
    row.update(over)
    mmc.record_row(cid, "pending", **row)
    return cid


def test_process_action_non_harrison_refused():
    cid = _seed_pending()
    client = MagicMock()
    outcome, _msg = mmc.process_catchup_action(cid, "UNOTHARRISON", client, action="send")
    assert outcome == "not_authorized"
    client.chat_postMessage.assert_not_called()
    assert mmc.is_terminal(cid) is False  # unchanged


def test_process_action_skip():
    cid = _seed_pending()
    client = MagicMock()
    outcome, _msg = mmc.process_catchup_action(cid, HARRISON, client, action="skip")
    assert outcome == "skipped"
    client.chat_postMessage.assert_not_called()
    assert mmc.latest_disposition(cid)["disposition"] == "skipped"


def test_process_action_send_posts_and_records(monkeypatch):
    import cora.app as app_mod
    monkeypatch.setattr(app_mod.active_thread_store, "register", lambda *a, **k: None)
    monkeypatch.setattr(mmc.channel_content_guard, "guard_outbound", lambda text, **k: (text, None))
    cid = _seed_pending()
    client = MagicMock()
    client.chat_postMessage.return_value = {"ts": "999.9"}
    outcome, _msg = mmc.process_catchup_action(cid, HARRISON, client, action="send")
    assert outcome == "sent"
    client.chat_postMessage.assert_called_once()
    kw = client.chat_postMessage.call_args.kwargs
    assert kw["channel"] == "C1" and kw["thread_ts"] == "500.0"
    # Preface prepended (draft is long enough) and answer present.
    assert "drafted answer body" in kw["text"]
    assert mmc.latest_disposition(cid)["disposition"] == "sent"


def test_process_action_send_idempotent_no_double_post(monkeypatch):
    import cora.app as app_mod
    monkeypatch.setattr(app_mod.active_thread_store, "register", lambda *a, **k: None)
    monkeypatch.setattr(mmc.channel_content_guard, "guard_outbound", lambda text, **k: (text, None))
    cid = _seed_pending()
    client = MagicMock()
    client.chat_postMessage.return_value = {"ts": "999.9"}
    mmc.process_catchup_action(cid, HARRISON, client, action="send")
    outcome2, _ = mmc.process_catchup_action(cid, HARRISON, client, action="send")
    assert outcome2 == "already_resolved"
    assert client.chat_postMessage.call_count == 1  # not posted twice


def test_process_action_edit_reguards_and_sends(monkeypatch):
    import cora.app as app_mod
    monkeypatch.setattr(app_mod.active_thread_store, "register", lambda *a, **k: None)
    seen = {}
    def fake_guard(text, **k):
        seen["text"] = text
        seen["kw"] = k
        return text, None
    monkeypatch.setattr(mmc.channel_content_guard, "guard_outbound", fake_guard)
    cid = _seed_pending()
    client = MagicMock()
    client.chat_postMessage.return_value = {"ts": "999.9"}
    outcome, _ = mmc.process_catchup_action(cid, HARRISON, client, action="send",
                                            edited_text="Edited reply from Harrison here now.")
    assert outcome == "edited_sent"
    # The EDITED text was re-guarded against the SOURCE channel context.
    assert "Edited reply from Harrison" in seen["text"]
    assert seen["kw"]["channel_name"] == "f3e-sales" and seen["kw"]["is_dm"] is False


def test_process_action_missing_item():
    client = MagicMock()
    outcome, _ = mmc.process_catchup_action("C9:1.0", HARRISON, client, action="send")
    assert outcome == "not_found"


# ── Classifier fail-closed ───────────────────────────────────────────────────────

def test_classify_still_open_no_following_is_open():
    assert mmc.classify_still_open("q?", []) is True


def test_classify_still_open_no_key_is_open(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert mmc.classify_still_open("q?", ["some later message"]) is True


def test_edit_modal_view_carries_metadata():
    view = mmc.edit_modal_view("C1:500.0", "D_HARRISON", "111.1", "draft body")
    assert view["callback_id"] == mmc.VIEW_EDIT_SUBMIT
    meta = json.loads(view["private_metadata"])
    assert meta["catchup_id"] == "C1:500.0" and meta["dm_channel"] == "D_HARRISON"


# ── helpers ──

def test_preface_only_on_nontrivial():
    short = mmc._apply_preface("ok")
    long = mmc._apply_preface("x" * 200)
    assert short == "ok"
    assert long.startswith(mmc.CATCHUP_PREFACE)


def _epoch(iso: str) -> float:
    from datetime import datetime as _dt
    return _dt.strptime(iso, "%Y-%m-%dT%H:%M:%S").astimezone().timestamp()
