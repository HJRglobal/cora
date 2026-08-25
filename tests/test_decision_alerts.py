"""C8 (cq-c3454e25f7cf): answering a stalled-decision alert in-thread.

THE 8/19 INCIDENT, reconstructed from live artifacts during recon. The daily
14:00 pass 2 DM'd Harrison "Stalled P1 decision (untouched >30d) -- AI Summit
revenue: which entity books it (HJR Productions vs Harrison Rogers LLC)?". He
replied in-thread with the deciding word, "HJR productions". The logs record
`dm_qa routed ... thread=True text=HJR productions` and, immediately after,
`thread_history: fetched 0 turns` -- 15 context-free characters routed to Haiku,
which replied with clarifying questions. The decision stayed stalled until it was
ruled by hand five days later.

THREE DEFECTS, all pinned below:

  NO ALERT HAD AN IDENTITY. `_send_dm` discarded chat_postMessage's response and
  persisted only a throttle hash. Nothing in the repo could recognise a reply as
  being TO an alert, because no alert was identifiable.

  ROUTING HAD NO CLAIMANT. The DM branch tried knowledge-check capture, gap-ask
  capture, Tier-2 retrieval, the shift scheduler, then plain Q&A. A decision-alert
  thread matched nothing.

  THE MODEL WAS ALSO BLIND. Both history builders satisfied the "must start with
  a user turn" API rule by POPPING leading assistant turns. In a thread Cora
  started, her alert is the only prior message -- so the pop emptied the list.
  That made EVERY Cora-initiated alert thread context-free on its first human
  reply, not just decision alerts.

NOTHING HERE WRITES CANON. decisions-pending.md is read-only to all of Cora and
stays that way; Confirm records the answer and stops the re-ask (D-011).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cora import decision_alerts as da
from cora.app import _ensure_user_first
from cora.tools import tool_dispatch as td

HARRISON = "U0B2RM2JYJ1"
TOPIC = ("AI Summit revenue — which entity books it "
         "(HJR Productions vs Harrison Rogers LLC)?")


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("DECISION_ALERT_STATE_PATH", str(tmp_path / "alerts.json"))
    return tmp_path / "alerts.json"


def _alert(ts="1787173218.141609", topic=TOPIC):
    return da.record_alert(topic=topic, severity="P1", entity="HJRPROD",
                           owner=HARRISON, surfaced="2026-07-20",
                           dm_channel_id="D0B4CTD3B09", alert_message_ts=ts,
                           target_user_id=HARRISON)


# ── the model was blind ─────────────────────────────────────────────────────

def test_a_cora_opened_thread_no_longer_arrives_empty():
    """THE regression pin. Popping the leading assistant turn emptied the list
    for every thread Cora started -- the logged `fetched 0 turns`."""
    out = _ensure_user_first([
        {"role": "assistant", "content": "Stalled P1 decision: " + TOPIC},
        {"role": "user", "content": "HJR productions"},
    ])
    assert len(out) == 1 and out[0]["role"] == "user"
    assert "AI Summit revenue" in out[0]["content"], "the alert was thrown away"
    assert "HJR productions" in out[0]["content"], "the reply was thrown away"


def test_the_context_is_labelled_not_silently_re_roled():
    """The model must not be told the human said what Cora said."""
    out = _ensure_user_first([
        {"role": "assistant", "content": "Cora's alert"},
        {"role": "user", "content": "the answer"},
    ])
    body = out[0]["content"]
    assert body.index("Cora opened this thread") < body.index("Cora's alert")
    assert "[The reply to it:]" in body


def test_alternation_and_ordinary_histories_are_untouched():
    normal = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
    assert _ensure_user_first(list(normal)) == normal
    assert _ensure_user_first([]) == []
    # a thread with ONLY Cora's message and no reply still yields nothing to send
    assert _ensure_user_first([{"role": "assistant", "content": "only"}]) == []
    roles = [t["role"] for t in _ensure_user_first([
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "b"}])]
    assert roles == ["user", "assistant"]


# ── the alert now has an identity ───────────────────────────────────────────

def test_a_threaded_reply_matches_its_alert():
    _alert()
    got = da.match_alert_reply(HARRISON, "1787173218.141609")
    assert got and got["topic"] == TOPIC


def test_a_top_level_message_never_matches():
    """THREADED-ONLY on purpose: it keeps this out of the greedy-capture contest
    the DM branch already runs."""
    _alert()
    assert da.match_alert_reply(HARRISON, "") is None
    assert da.match_alert_reply(HARRISON, None) is None


def test_a_different_thread_never_matches():
    _alert()
    assert da.match_alert_reply(HARRISON, "9999999999.000000") is None


def test_someone_elses_reply_never_matches():
    _alert()
    assert da.match_alert_reply("U_OTHER", "1787173218.141609") is None


def test_an_expired_alert_never_matches():
    _alert()
    future = datetime.now(timezone.utc) + timedelta(days=da.TTL_DAYS + 1)
    assert da.match_alert_reply(HARRISON, "1787173218.141609", now=future) is None


def test_a_resolved_alert_never_matches_again():
    _alert()
    da.mark_state("1787173218.141609", da.STATE_ANSWERED, answer="HJR productions")
    assert da.match_alert_reply(HARRISON, "1787173218.141609") is None


def test_a_corrupt_state_file_never_breaks_a_dm(state):
    state.write_text("{not json", encoding="utf-8")
    assert da.match_alert_reply(HARRISON, "1.1") is None


def test_the_topic_key_is_stable_across_processes():
    """hashlib, never the builtin hash() -- that is siphash-randomized per
    interpreter and would give a different key every run (the C6 defect, in a
    module that would otherwise have inherited it)."""
    import subprocess
    code = ("import sys; sys.path.insert(0, r'%s');"
            "from cora import decision_alerts as d; print(d.topic_key(%r))"
            % (str(Path(__file__).resolve().parents[1] / "src"), TOPIC))
    import os
    seen = {subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, env=dict(os.environ, PYTHONHASHSEED=s),
                           check=True).stdout.strip()
            for s in ("0", "1", "999")}
    assert len(seen) == 1


# ── never re-ask ────────────────────────────────────────────────────────────

def test_an_answered_topic_is_suppressed_from_re_alerting():
    """Load-bearing. `age_days` keys on the file's "Last touched" line and an
    in-thread answer never touches that file, so without this the SAME decision
    re-alerts every 7 days until Harrison hand-edits it."""
    _alert()
    da.mark_state("1787173218.141609", da.STATE_ANSWERED, answer="HJR productions")
    assert da.topic_key(TOPIC) in da.answered_topic_keys()


def test_a_declined_answer_leaves_the_decision_open():
    """"Not my area" is not a decision. Same rule gap_autofill applies."""
    _alert()
    da.mark_state("1787173218.141609", da.STATE_DECLINED)
    assert da.topic_key(TOPIC) not in da.answered_topic_keys()


@pytest.mark.parametrize("text", ["no idea", "not sure yet", "not my call",
                                  "still thinking", "park it for now"])
def test_decline_phrases_are_recognised(text):
    assert da.is_decline(text) is True


@pytest.mark.parametrize("text", ["HJR productions", "book it to HJRP",
                                  "no -- put it under Harrison Rogers LLC"])
def test_a_real_answer_is_not_a_decline(text):
    assert da.is_decline(text) is False


# ── the confirm path ────────────────────────────────────────────────────────

def test_the_preview_states_what_confirm_actually_does():
    """It must not promise to close the decision -- nothing in Cora may write
    decisions-pending.md."""
    rec = _alert()
    preview = da.build_close_preview(rec, "HJR productions")
    assert "AI Summit revenue" in preview
    assert "HJR productions" in preview
    assert "does NOT edit" in preview and "decisions-pending.md" in preview


def test_decision_close_is_a_full_class_b_kind():
    """Joining _CLASSB_KINDS is what gives it the arbitration, the connector
    footer strip, the confirm/cancel card and the typed-confirm path."""
    assert "decision_close" in td._CLASSB_KINDS
    assert "decision_close" in td._CLASSB


def test_confirm_records_the_stashed_answer_not_a_re_echo():
    _alert()
    sid = td.stash_decision_close(HARRISON, "1787173218.141609", "HJR productions")
    assert sid
    entry = td._classb_take("decision_close", HARRISON, "dm")
    assert entry["answer"] == "HJR productions"
    msg = td._execute_claimed_decision_close(entry, HARRISON)
    assert "Recorded" in msg
    assert da.topic_key(TOPIC) in da.answered_topic_keys()


def test_confirming_an_expired_alert_says_so_instead_of_claiming_success():
    msg = td._execute_claimed_decision_close(
        {"alert_key": "nope", "answer": "x"}, HARRISON)
    assert "still open" in msg
    assert "Recorded" not in msg


def test_an_empty_payload_is_refused():
    msg = td._execute_claimed_decision_close({"alert_key": "", "answer": ""},
                                             HARRISON)
    assert "empty" in msg


def test_the_executor_is_wired_into_the_shared_dispatch():
    import inspect
    src = inspect.getsource(td._execute_claimed_stash)
    assert 'if kind == "decision_close":' in src
