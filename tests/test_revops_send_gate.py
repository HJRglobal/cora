"""R2 send gate end-to-end: kill-switch precedence, stale-stash, double-tap
idempotency, byte-exactness, recipient-subset (D-051 lenses 2/3/4), audit
privacy (lens 7), and prompt-injection posture (lens 8)."""

from __future__ import annotations

import base64
import json
import time
from email import message_from_bytes

import pytest

from cora.revops import cards, ledger, send_trust, sender, stash

HARRISON = "U0B2RM2JYJ1"
MAILBOX = "harrison@hjrglobal.com"
COUNTERPARTY = "josha@whamfoods.com"
CC_PARTY = "tommy@f3energy.com"

BODY = "Hi Josh,\n\nJust circling back on this. Any update?\n\nThanks!\nHarrison\n"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("CORA_REVOPS_DB", str(tmp_path / "revops_ledger.db"))
    monkeypatch.setenv("CORA_SEND_LIVE", "tier1")
    monkeypatch.setattr(sender, "_AUDIT_PATH", tmp_path / "cora-send-audit.jsonl")
    send_trust.clear_caches()
    yield
    send_trust.clear_caches()


@pytest.fixture()
def conn():
    c = ledger.connect()
    yield c
    c.close()


def _ctx(participants=None):
    return sender.ThreadContext(
        participants=set(participants or {MAILBOX, COUNTERPARTY, CC_PARTY}),
        last_rfc_message_id="<orig-123@mail.gmail.com>",
        references="<orig-123@mail.gmail.com>",
        subject="WHAM FOODS CLOSEOUT",
    )


@pytest.fixture()
def staged(conn, monkeypatch):
    key = ledger.upsert_thread(
        conn,
        mailbox=MAILBOX,
        gmail_thread_id="gt1",
        counterparty_name="Josh A. (Wham Foods)",
        workstream="Retail",
        entity="F3E",
        state="nudge_due",
        last_outbound_ts=time.time() - 10 * 86400,
    )
    sid = stash.create_stash(
        conn,
        thread_key=key,
        mailbox=MAILBOX,
        playbook_id="silence_nudge",
        gmail_thread_id="gt1",
        recipients=[COUNTERPARTY],
        cc=[],
        subject=None,
        body_text=BODY,
    )
    sent_calls: list[dict] = []

    def fake_send(mailbox, raw_b64, thread_id):
        sent_calls.append({"mailbox": mailbox, "raw": raw_b64, "thread_id": thread_id})
        return {"id": "sent-msg-1"}

    monkeypatch.setattr(sender, "fetch_thread_context", lambda m, t: _ctx())
    monkeypatch.setattr(sender, "_gmail_send_raw", fake_send)
    return {"conn": conn, "key": key, "sid": sid, "sent": sent_calls}


# ------------------------------------------------ lens 2: kill switch + stale

def test_env_off_beats_approved_card(staged, monkeypatch):
    """TEST-PINNED design invariant: env off refuses even a valid approval."""
    monkeypatch.setenv("CORA_SEND_LIVE", "off")
    outcome, msg = sender.send_stashed(staged["sid"], approver_id=HARRISON, conn=staged["conn"])
    assert outcome == "env_off"
    assert staged["sent"] == []
    row = stash.get_stash(staged["conn"], staged["sid"])
    assert row["status"] == "staged"  # stays approvable after the flip


def test_env_unset_is_off(staged, monkeypatch):
    monkeypatch.delenv("CORA_SEND_LIVE", raising=False)
    outcome, _ = sender.send_stashed(staged["sid"], approver_id=HARRISON, conn=staged["conn"])
    assert outcome == "env_off"
    assert staged["sent"] == []


def test_expired_card_can_never_fire(staged):
    conn = staged["conn"]
    conn.execute(
        "UPDATE send_stashes SET expires_ts = ? WHERE stash_id = ?",
        (time.time() - 1, staged["sid"]),
    )
    conn.commit()
    outcome, _ = sender.send_stashed(staged["sid"], approver_id=HARRISON, conn=conn)
    assert outcome == "expired"
    assert staged["sent"] == []
    row = stash.get_stash(conn, staged["sid"])
    assert row["status"] == "expired"
    assert row["body_text"] is None  # purged


def test_double_tap_single_send(staged):
    outcome1, _ = sender.send_stashed(staged["sid"], approver_id=HARRISON, conn=staged["conn"])
    outcome2, _ = sender.send_stashed(staged["sid"], approver_id=HARRISON, conn=staged["conn"])
    assert outcome1 == "sent"
    assert outcome2 == "already_resolved"
    assert len(staged["sent"]) == 1


def test_non_approver_refused(staged):
    outcome, _ = sender.send_stashed(staged["sid"], approver_id="U0B3RU5Q55G", conn=staged["conn"])
    assert outcome == "not_authorized"
    assert staged["sent"] == []


def test_edit_then_old_card_tap_refuses(staged):
    """Lens 2: edit-then-old-card-tap -- the old stash is cancelled."""
    outcome, msg, new_sid = cards.restage_with_edit(
        staged["sid"], HARRISON, BODY + "PS: samples on the way!", conn=staged["conn"]
    )
    assert outcome == "restaged" and new_sid
    old_outcome, _ = sender.send_stashed(staged["sid"], approver_id=HARRISON, conn=staged["conn"])
    assert old_outcome == "already_resolved"
    assert staged["sent"] == []


# ------------------------------------------------------ lens 3: byte-exactness

def test_sent_bytes_match_stash_exactly(staged):
    outcome, _ = sender.send_stashed(staged["sid"], approver_id=HARRISON, conn=staged["conn"])
    assert outcome == "sent"
    raw = staged["sent"][0]["raw"]
    msg = message_from_bytes(base64.urlsafe_b64decode(raw))
    assert msg.get_payload(decode=True).decode("utf-8") == BODY
    assert msg["To"] == COUNTERPARTY
    assert msg["In-Reply-To"] == "<orig-123@mail.gmail.com>"
    assert msg["Subject"].startswith("Re: ")
    assert staged["sent"][0]["thread_id"] == "gt1"


def test_hash_mismatch_refuses(staged):
    conn = staged["conn"]
    conn.execute(
        "UPDATE send_stashes SET body_text = ? WHERE stash_id = ?",
        (BODY + "tampered", staged["sid"]),
    )
    conn.commit()
    outcome, _ = sender.send_stashed(staged["sid"], approver_id=HARRISON, conn=conn)
    assert outcome == "hash_mismatch"
    assert staged["sent"] == []


def test_guard_rerun_at_send_blocks(staged):
    conn = staged["conn"]
    bad = BODY.replace("circling back", "circling — back")
    conn.execute(
        "UPDATE send_stashes SET body_text = ?, body_sha256 = ? WHERE stash_id = ?",
        (bad, stash.sha256_text(bad), staged["sid"]),
    )
    conn.commit()
    outcome, msg = sender.send_stashed(staged["sid"], approver_id=HARRISON, conn=conn)
    assert outcome == "guard_blocked"
    assert staged["sent"] == []
    assert stash.get_stash(conn, staged["sid"])["status"] == "cancelled"


# ------------------------------------------- lens 4: recipient subset + thread

def test_recipient_outside_thread_refused(staged, monkeypatch):
    monkeypatch.setattr(
        sender, "fetch_thread_context", lambda m, t: _ctx({MAILBOX, "someoneelse@x.com"})
    )
    outcome, msg = sender.send_stashed(staged["sid"], approver_id=HARRISON, conn=staged["conn"])
    assert outcome == "recipient_violation"
    assert staged["sent"] == []


def test_thread_verify_failure_fails_closed(staged, monkeypatch):
    def boom(m, t):
        raise RuntimeError("gmail down")

    monkeypatch.setattr(sender, "fetch_thread_context", boom)
    outcome, _ = sender.send_stashed(staged["sid"], approver_id=HARRISON, conn=staged["conn"])
    assert outcome == "thread_verify_failed"
    assert staged["sent"] == []


def test_send_failure_closes_stash_no_retry(staged, monkeypatch):
    def boom(mailbox, raw, tid):
        raise RuntimeError("api 500")

    monkeypatch.setattr(sender, "_gmail_send_raw", boom)
    outcome, _ = sender.send_stashed(staged["sid"], approver_id=HARRISON, conn=staged["conn"])
    assert outcome == "send_failed"
    assert stash.get_stash(staged["conn"], staged["sid"])["status"] == "send_failed"
    # a re-tap cannot fire it again
    outcome2, _ = sender.send_stashed(staged["sid"], approver_id=HARRISON, conn=staged["conn"])
    assert outcome2 == "already_resolved"


# ------------------------------------------------------- audit + ledger trail

def test_audit_line_has_hash_but_no_body(staged, tmp_path):
    outcome, _ = sender.send_stashed(staged["sid"], approver_id=HARRISON, conn=staged["conn"])
    assert outcome == "sent"
    lines = [
        json.loads(l)
        for l in sender._AUDIT_PATH.read_text(encoding="utf-8").splitlines()
    ]
    rec = lines[-1]
    assert rec["outcome"] == "sent"
    assert rec["approver"] == HARRISON
    assert rec["sha256"] == stash.sha256_text(BODY)
    assert "circling back" not in json.dumps(rec)  # no body content, ever


def test_send_updates_ledger_state_and_nudge_count(staged):
    sender.send_stashed(staged["sid"], approver_id=HARRISON, conn=staged["conn"])
    row = ledger.get_thread(staged["conn"], staged["key"])
    assert row["state"] == "awaiting_reply"
    assert row["state_source"] == "send"
    assert row["nudge_count"] == 1
    events = ledger.get_events(staged["conn"], staged["key"])
    assert any(e["event_type"] == "send" for e in events)


# ---------------------------------------------------- card processor behavior

def test_skip_cancels_and_pushes_review(staged):
    outcome, msg = cards.process_send_action(
        staged["sid"], HARRISON, action="skip", conn=staged["conn"]
    )
    assert outcome == "skipped"
    assert stash.get_stash(staged["conn"], staged["sid"])["status"] == "cancelled"
    row = ledger.get_thread(staged["conn"], staged["key"])
    assert row["state"] == "awaiting_reply"
    assert row["next_review_date"] is not None


def test_close_transitions_courtesy(staged):
    outcome, _ = cards.process_send_action(
        staged["sid"], HARRISON, action="close", conn=staged["conn"]
    )
    assert outcome == "closed"
    assert ledger.get_thread(staged["conn"], staged["key"])["state"] == "closed_courtesy"


def test_card_actions_gated_to_approver(staged):
    for action in ("send", "skip", "close"):
        outcome, _ = cards.process_send_action(
            staged["sid"], "U0B3VGWJTMJ", action=action, conn=staged["conn"]
        )
        assert outcome == "not_authorized", action


def test_edit_reguards_and_restages(staged):
    outcome, msg, new_sid = cards.restage_with_edit(
        staged["sid"], HARRISON, "New text — with an em dash", conn=staged["conn"]
    )
    assert outcome == "guard_blocked" and new_sid is None
    # original card untouched by a blocked edit
    assert stash.get_stash(staged["conn"], staged["sid"])["status"] == "staged"


def test_card_shows_exact_body_and_stash_id(staged):
    from cora.revops import email_egress_guard

    row = stash.get_stash(staged["conn"], staged["sid"])
    thread_row = ledger.get_thread(staged["conn"], staged["key"])
    guard = email_egress_guard.check_email(BODY, workstream="Retail", entity="F3E")
    fallback, blocks = cards.build_send_card(row, thread_row, guard)
    section_texts = [
        b["text"]["text"] for b in blocks if b.get("type") == "section"
    ]
    assert any(BODY.strip() in t for t in section_texts)  # body displayed verbatim
    flat = json.dumps(blocks)
    assert staged["sid"] in flat  # identity rides in button values
    assert "SEND " + staged["sid"] in flat  # typed fallback documented on-card
