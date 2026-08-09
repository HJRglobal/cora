"""Regression pins for the D-051 remediation (2026-08-02, 8-lens/98-agent
review: 45 findings, 43 confirmed). One test per fixed defect class."""

from __future__ import annotations

import json
import time

import pytest

from cora.revops import cards, email_egress_guard as guard, ledger, send_trust, sender, stash, sweep
from cora.tools import tool_dispatch as td

HARRISON = "U0B2RM2JYJ1"
MAILBOX = "harrison@hjrglobal.com"
BODY = "Hi Josh,\n\nJust circling back on this. Any update?\n\nThanks!\nHarrison\n"
NOW = time.time()


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("CORA_REVOPS_DB", str(tmp_path / "revops.db"))
    monkeypatch.setenv("CORA_SEND_LIVE", "tier1")
    monkeypatch.setattr(sender, "_AUDIT_PATH", tmp_path / "audit.jsonl")
    send_trust.clear_caches()
    yield
    send_trust.clear_caches()


@pytest.fixture()
def conn():
    c = ledger.connect()
    yield c
    c.close()


def _thread(conn, tid="t1", **kw):
    base = dict(
        mailbox=MAILBOX, gmail_thread_id=tid, counterparty_name="Josh A. (Wham Foods)",
        workstream="Retail", entity="F3E", state="nudge_due",
        last_outbound_ts=NOW - 10 * 86400,
    )
    base.update(kw)
    return ledger.upsert_thread(conn, **base)


def _stash(conn, key, **kw):
    base = dict(
        thread_key=key, mailbox=MAILBOX, playbook_id="silence_nudge",
        gmail_thread_id="t1", recipients=["josha@whamfoods.com"], cc=[],
        subject="WHAM FOODS CLOSEOUT", body_text=BODY, thread_last_msg_id="m1",
    )
    base.update(kw)
    return stash.create_stash(conn, **base)


def _ctx(**kw):
    base = dict(
        participants={MAILBOX, "josha@whamfoods.com"},
        last_rfc_message_id="<m1@mail>", references="<m1@mail>",
        subject="WHAM FOODS CLOSEOUT", last_message_id="m1",
        last_is_inbound=False, message_count=2,
    )
    base.update(kw)
    return sender.ThreadContext(**base)


# ---------------- lens 1/2: card retry surface + in-flight stash --------------

def test_retryable_refusal_keeps_stash_staged():
    """thread_verify_failed leaves the stash live so the card can retry."""
    conn = ledger.connect()
    key = _thread(conn)
    sid = _stash(conn, key)
    import cora.revops.sender as s
    orig = s.fetch_thread_context
    s.fetch_thread_context = lambda m, t: (_ for _ in ()).throw(RuntimeError("gmail 500"))
    try:
        outcome, _ = sender.send_stashed(sid, approver_id=HARRISON, conn=conn)
    finally:
        s.fetch_thread_context = orig
    assert outcome == "thread_verify_failed"
    assert stash.get_stash(conn, sid)["status"] == "staged"
    conn.close()


def test_app_keeps_buttons_for_retryable_outcomes():
    import inspect
    import cora.app as app_mod

    src = inspect.getsource(app_mod._handle_revops_send_tap)
    for outcome in ("env_off", "thread_verify_failed", "tier_denied", "mailbox_denied"):
        assert outcome in src
    assert '"context"' in src or "'context'" in src  # typed-SEND line survives


def test_in_flight_sending_stash_counts_as_live(conn):
    key = _thread(conn)
    sid = _stash(conn, key)
    assert stash.claim_for_send(conn, sid, approved_by=HARRISON)
    assert stash.get_stash(conn, sid)["status"] == "sending"
    assert stash.get_staged_for_thread(conn, key) is not None


def test_sweep_does_not_duplicate_card_mid_send(conn, monkeypatch):
    key = _thread(conn, state="nudge_staged")
    sid = _stash(conn, key)
    stash.claim_for_send(conn, sid, approved_by=HARRISON)
    msgs = [{"sender": MAILBOX, "date_ts": NOW - 10 * 86400,
             "recipients": ["josha@whamfoods.com"], "label_ids": ["SENT"],
             "subject": "x", "body_text": "", "message_id": "m1"}]
    monkeypatch.setattr(sweep, "fetch_thread_messages", lambda m, t: msgs)
    report = sweep.sweep(conn, mode="stage", fetch=lambda m, t: msgs, now=NOW,
                         slack_client=None)
    assert report["restored_nudge_due"] == 0
    assert report["staged_cards"] == 0
    assert conn.execute("SELECT COUNT(*) FROM send_stashes").fetchone()[0] == 1


# ------------------- lens 2/3: post-send honesty + purge ----------------------

def test_timeout_after_send_reports_indeterminate(conn, monkeypatch):
    key = _thread(conn)
    sid = _stash(conn, key)
    monkeypatch.setattr(sender, "fetch_thread_context", lambda m, t: _ctx())
    monkeypatch.setattr(
        sender, "_gmail_send_raw",
        lambda m, r, t: (_ for _ in ()).throw(TimeoutError("read timed out")),
    )
    outcome, msg = sender.send_stashed(sid, approver_id=HARRISON, conn=conn)
    assert outcome == "send_indeterminate"
    assert "cannot confirm" in msg
    assert stash.get_stash(conn, sid)["status"] == "send_indeterminate"


def test_bookkeeping_failure_still_reports_sent(conn, monkeypatch):
    key = _thread(conn)
    sid = _stash(conn, key)
    monkeypatch.setattr(sender, "fetch_thread_context", lambda m, t: _ctx())
    monkeypatch.setattr(sender, "_gmail_send_raw", lambda m, r, t: {"id": "sent1"})
    monkeypatch.setattr(
        stash, "finalize_sent",
        lambda c, s: (_ for _ in ()).throw(RuntimeError("db locked")),
    )
    outcome, msg = sender.send_stashed(sid, approver_id=HARRISON, conn=conn)
    assert outcome == "sent_bookkeeping_failed"
    assert msg.startswith("SENT")
    # the audit line was written BEFORE bookkeeping, so the send is recorded
    lines = [json.loads(l) for l in sender._AUDIT_PATH.read_text().splitlines()]
    assert lines[-1]["outcome"] == "sent"


def test_sent_and_failed_rows_purge_the_body(conn, monkeypatch):
    key = _thread(conn)
    sid = _stash(conn, key)
    monkeypatch.setattr(sender, "fetch_thread_context", lambda m, t: _ctx())
    monkeypatch.setattr(sender, "_gmail_send_raw", lambda m, r, t: {"id": "sent1"})
    sender.send_stashed(sid, approver_id=HARRISON, conn=conn)
    row = stash.get_stash(conn, sid)
    assert row["status"] == "sent"
    assert row["body_text"] is None
    assert row["body_sha256"]  # the durable record survives


# ------------------------- lens 3: card fidelity ------------------------------

def test_card_escapes_fence_breakout(conn):
    key = _thread(conn)
    sid = _stash(conn, key, body_text="Hi\n```\nnot a fence\n```\nBye\n")
    row = stash.get_stash(conn, sid)
    _, blocks = cards.build_send_card(row, ledger.get_thread(conn, key),
                                      guard.GuardResult())
    body_block = [b for b in blocks if b.get("type") == "section"][1]["text"]["text"]
    assert "\n```\nnot a fence" not in body_block  # cannot break out
    assert stash.get_stash(conn, sid)["body_text"].count("```") == 2  # bytes intact


def test_card_shows_pinned_subject(conn):
    key = _thread(conn)
    sid = _stash(conn, key)
    row = stash.get_stash(conn, sid)
    _, blocks = cards.build_send_card(row, ledger.get_thread(conn, key),
                                      guard.GuardResult())
    header = blocks[0]["text"]["text"]
    assert "Subject: Re: WHAM FOODS CLOSEOUT" in header


def test_send_uses_pinned_subject_not_live_thread(conn, monkeypatch):
    key = _thread(conn)
    sid = _stash(conn, key)
    captured = {}
    monkeypatch.setattr(
        sender, "fetch_thread_context",
        lambda m, t: _ctx(subject="ATTACKER CHANGED THE SUBJECT"),
    )

    def _capture(mailbox, raw, thread_id):
        captured["raw"] = raw
        return {"id": "s1"}

    monkeypatch.setattr(sender, "_gmail_send_raw", _capture)
    sender.send_stashed(sid, approver_id=HARRISON, conn=conn)
    import base64
    from email import message_from_bytes

    msg = message_from_bytes(base64.urlsafe_b64decode(captured["raw"]))
    assert "WHAM FOODS CLOSEOUT" in msg["Subject"]
    assert "ATTACKER" not in msg["Subject"]


# --------------------- lens 4/8: staleness + recipients -----------------------

def test_stale_thread_refuses_send(conn, monkeypatch):
    key = _thread(conn)
    sid = _stash(conn, key)
    monkeypatch.setattr(
        sender, "fetch_thread_context",
        lambda m, t: _ctx(last_is_inbound=True, last_message_id="m2-new-reply"),
    )
    sent = []
    monkeypatch.setattr(sender, "_gmail_send_raw", lambda m, r, t: sent.append(1))
    outcome, msg = sender.send_stashed(sid, approver_id=HARRISON, conn=conn)
    assert outcome == "stale_thread"
    assert sent == []
    assert stash.get_stash(conn, sid)["status"] == "cancelled"


def test_recipients_never_come_from_injected_addresses(conn):
    key = _thread(conn)
    row = ledger.get_thread(conn, key)
    # An inbound message naming an attacker address; NO outbound message.
    inbound_only = [
        {"sender": "attacker@evil.com", "recipients": ["attacker2@evil.com"],
         "label_ids": [], "date_ts": NOW}
    ]
    to, cc = sweep._nudge_recipients(conn, row, inbound_only)
    assert to == [] and cc == []  # no verifiable outbound recipient -> no nudge


def test_spoofed_own_alias_inbound_is_not_outbound(conn):
    key = _thread(conn)
    row = ledger.get_thread(conn, key)
    spoofed = [
        {"sender": MAILBOX, "recipients": ["attacker@evil.com"],
         "label_ids": [], "date_ts": NOW},  # no SENT label = not ours
    ]
    to, _ = sweep._nudge_recipients(conn, row, spoofed)
    assert to == []


def test_sent_label_drives_direction_over_from_header(conn):
    key = _thread(conn)
    msgs = [{"sender": "someone-else@x.com", "recipients": ["josha@whamfoods.com"],
             "label_ids": ["SENT"], "date_ts": NOW - 10 * 86400, "subject": "s",
             "body_text": "", "message_id": "m1"}]
    sweep.sweep(conn, fetch=lambda m, t: msgs, now=NOW)
    assert ledger.get_thread(conn, key)["state"] == "nudge_due"  # read as outbound


def test_future_dated_reply_cannot_fake_silence(conn):
    key = _thread(conn, state="awaiting_reply")
    msgs = [{"sender": MAILBOX, "recipients": ["josha@whamfoods.com"],
             "label_ids": ["SENT"], "date_ts": NOW + 400 * 86400, "subject": "s",
             "body_text": "", "message_id": "m1"}]
    sweep.sweep(conn, fetch=lambda m, t: msgs, now=NOW)
    # clamped to now -> zero days silent -> no nudge
    assert ledger.get_thread(conn, key)["state"] == "awaiting_reply"


# -------------------------- lens 5: importer safety ---------------------------

def test_importer_cannot_de_escalate(conn):
    key = _thread(conn, state="escalated", owner=HARRISON)
    ledger.upsert_thread(
        conn, mailbox=MAILBOX, gmail_thread_id="t1", workstream="Retail",
        entity="F3E", state="awaiting_reply", owner="U0B3RU5Q55G",
        source="import", observation_ts=NOW + 9999,
    )
    row = ledger.get_thread(conn, key)
    assert row["state"] == "escalated"
    assert row["owner"] == HARRISON  # owner not reset either


def test_importer_cannot_unhold(conn):
    key = _thread(conn, state="hold", hold_reason="no nudge warranted")
    ledger.upsert_thread(
        conn, mailbox=MAILBOX, gmail_thread_id="t1", workstream="Retail",
        entity="F3E", state="awaiting_reply", source="import",
        observation_ts=NOW + 9999,
    )
    assert ledger.get_thread(conn, key)["state"] == "hold"


def test_reimport_does_not_duplicate_notes(conn):
    for _ in range(3):
        ledger.upsert_thread(
            conn, mailbox=MAILBOX, gmail_thread_id="t9", workstream="Retail",
            entity="F3E", state="awaiting_reply", notes="B2 nudge draft staged: r123",
            source="import", observation_ts=NOW,
        )
    row = ledger.get_thread(conn, ledger.make_thread_key(MAILBOX, "t9"))
    assert row["notes"] == "B2 nudge draft staged: r123"


def test_send_recorded_even_when_thread_went_terminal(conn):
    key = _thread(conn)
    ledger.transition(conn, key, "closed_courtesy", actor=HARRISON, source="owner")
    moved = ledger.record_nudge_sent(conn, key, actor=HARRISON, detail={"x": 1})
    assert moved is False
    row = ledger.get_thread(conn, key)
    assert row["nudge_count"] == 1  # the delivered email IS counted
    events = ledger.get_events(conn, key)
    assert any(e["event_type"] == "send" for e in events)  # and audited


# ------------------------- lens 6: guard lexicon ------------------------------

@pytest.mark.parametrize(
    "text",
    ["helps with preventing disease", "healed my anxiety", "treatments for insomnia",
     "natural remedies", "anxiety relief in a can"],
)
def test_claims_inflections_block(text):
    assert any(b["class"] == "health_claims"
               for b in guard.check_email(text, entity="F3E").blocks)


@pytest.mark.parametrize(
    "text",
    ["let's prevent delays on the PO", "we can treat you to samples at the show",
     "this should heal the gap in the schedule"],
)
def test_benign_verbs_do_not_block(text):
    assert not any(b["class"] == "health_claims"
                   for b in guard.check_email(text, entity="F3E").blocks)


def test_lowercase_nsf_blocks():
    assert any(b["class"] == "nsf_context"
               for b in guard.check_email("we are nsf certified", entity="F3E").blocks)


def test_cross_sentence_nsf_blocks():
    text = "Our Pure line is expanding. It is NSF Certified for Sport."
    assert any(b["class"] == "nsf_context"
               for b in guard.check_email(text, entity="F3E").blocks)


@pytest.mark.parametrize(
    "text",
    ["we closed a USD 2m raise", "raised 2 million dollars", "valued at USD 30M"],
)
def test_press_figure_formats_block(text):
    assert any(b["class"] == "press_figures"
               for b in guard.check_email(text, workstream="Press").blocks)


@pytest.mark.parametrize("text", ["est. 2022", "started the company in 2022",
                                  "launched back in 2022"])
def test_founded_variants_block(text):
    assert any(b["class"] == "founded_2022" for b in guard.check_email(text).blocks)


def test_null_entity_still_gets_claims_screen():
    r = guard.check_email("cures anxiety", entity=None)
    assert any(b["class"] == "health_claims" for b in r.blocks)


def test_counterparty_google_host_without_doc_path_passes():
    r = guard.check_email("reach us via docs.google.com for the portal")
    assert not any(b["class"] == "internal_refs" for b in r.blocks)
    assert any(b["class"] == "internal_refs" for b in
               guard.check_email("https://docs.google.com/spreadsheets/d/abc123").blocks)


def test_dash_lookalikes_warn():
    r = guard.check_email("Monday ― Friday")
    assert any(w["class"] == "dash_lookalike" for w in r.warns)


def test_guard_reasons_carry_no_body_text():
    r = guard.check_email(
        "Our secret project Falcon cures anxiety, see G:\\My Drive\\secret.xlsx",
        entity="F3E",
    )
    blob = json.dumps(r.to_dict())
    assert "Falcon" not in blob
    assert "secret.xlsx" not in blob
    assert not r.ok


# ------------------------- lens 7/8: scoping + PHI ----------------------------

def test_null_entity_threads_hidden_from_non_harrison(conn):
    ledger.upsert_thread(
        conn, mailbox=MAILBOX, gmail_thread_id="tn", counterparty_name="Mystery Corp",
        workstream="Other", entity=None, state="awaiting_reply",
    )
    conn.close()
    out = td._tool_revops_ledger_status("U0B3RU5Q55G", "F3E", {})
    assert "Mystery Corp" not in out
    assert "Mystery Corp" in td._tool_revops_ledger_status(HARRISON, "FNDR", {})


def test_typed_send_is_dm_only():
    import inspect
    import cora.app as app_mod

    src = inspect.getsource(app_mod._dispatch_qa)
    idx = src.find("SEND\\s+")
    assert idx > 0
    assert 'channel_name == "dm"' in src[max(0, idx - 400):idx]


def test_draft_guard_screens_subject(monkeypatch):
    import cora.phi_guard as pg

    seen = {}
    monkeypatch.setattr(
        pg, "is_any_phi", lambda t: seen.setdefault("text", t) or "PATIENT" in t
    )
    monkeypatch.setattr(td, "_load_slack_asana_map",
                        lambda: {"U1": {"asana_email": "x@hjrglobal.com"}})
    # v2b S5: gmail_create_draft is now a real staged write, so the PHI screen
    # runs on the UNCONFIRMED (preview) call -- strictly stronger than before,
    # because PHI is refused BEFORE anything is stashed and therefore can never
    # become a confirmable pending or a tappable card.
    td._CLASSB["gmail_draft"]["store"].clear()
    out = td._tool_gmail_create_draft(
        "U1", "F3E",
        {"_channel_name": "f3e-leadership", "to": "a@b.com",
         "subject": "PATIENT records", "body": "hi"},
    )
    assert "PHI" in out and "refused" in out
    assert td._CLASSB["gmail_draft"]["peek"]("U1", "f3e-leadership") is None, (
        "a PHI-blocked draft must never be stashed")
