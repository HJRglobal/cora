"""R1 cadence ledger: states, transitions, LEX exclusion, escalation screen,
importer clobber protection (D-051 lens 5)."""

from __future__ import annotations

import time

import pytest

from cora.revops import ledger


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("CORA_REVOPS_DB", str(tmp_path / "revops_ledger.db"))
    c = ledger.connect()
    yield c
    c.close()


def _mk(conn, **kw):
    base = dict(
        mailbox="harrison@hjrglobal.com",
        gmail_thread_id="t1",
        counterparty_name="Josh A. (Wham Foods)",
        workstream="Retail",
        entity="F3E",
        state="awaiting_reply",
    )
    base.update(kw)
    return ledger.upsert_thread(conn, **base)


# ---------------------------------------------------------------- LEX screen

def test_lex_thread_rejected_before_insert(conn):
    for entity in ("LEX", "LEX-LLC", "lex-lts", "LEX_LBHS"):
        with pytest.raises(ledger.LexThreadRejected):
            _mk(conn, gmail_thread_id="lex1", entity=entity)
    assert ledger.list_threads(conn) == []


def test_non_lex_entities_allowed(conn):
    _mk(conn, gmail_thread_id="a", entity="F3E")
    _mk(conn, gmail_thread_id="b", entity="HJRP")
    _mk(conn, gmail_thread_id="c", entity="PERS")
    assert len(ledger.list_threads(conn)) == 3


# ------------------------------------------------------------- state machine

def test_invalid_state_rejected(conn):
    with pytest.raises(ValueError):
        _mk(conn, state="sent")  # not a valid state name


def test_transition_writes_event_row(conn):
    key = _mk(conn)
    assert ledger.transition(conn, key, "nudge_due", actor="sys", source="sweep")
    row = ledger.get_thread(conn, key)
    assert row["state"] == "nudge_due"
    events = ledger.get_events(conn, key)
    assert any(e["to_state"] == "nudge_due" and e["source"] == "sweep" for e in events)


def test_terminal_states_never_move(conn):
    key = _mk(conn)
    assert ledger.transition(conn, key, "closed_courtesy", actor="h", source="owner")
    assert not ledger.transition(conn, key, "awaiting_reply", actor="sys", source="sweep")
    assert not ledger.transition(conn, key, "nudge_due", actor="sys", source="send")
    assert ledger.get_thread(conn, key)["state"] == "closed_courtesy"


def test_importer_cannot_regress_send_written_state(conn):
    """D-051 lens 5: importer vs send-event race."""
    key = _mk(conn)
    ledger.record_nudge_sent(conn, key, actor="U0B2RM2JYJ1")
    row = ledger.get_thread(conn, key)
    assert row["state"] == "awaiting_reply"
    assert row["state_source"] == "send"
    assert row["nudge_count"] == 1

    # A later import observation may NOT touch the send-written state...
    _mk(conn, state="nudge_due", observation_ts=time.time() + 999)
    assert ledger.get_thread(conn, key)["state"] == "awaiting_reply"
    # ...and transition(source='import') is refused outright.
    assert not ledger.transition(conn, key, "nudge_due", actor="i", source="import")
    # The sweep (reality) may still advance it.
    assert ledger.transition(conn, key, "replied", actor="sys", source="sweep")


def test_import_reimport_is_idempotent(conn):
    key = _mk(conn, observation_ts=1000.0)
    _mk(conn, observation_ts=1000.0)  # same file re-imported
    events = ledger.get_events(conn, key)
    assert len([e for e in events if e["event_type"] == "import_update"]) == 0
    assert len(ledger.list_threads(conn)) == 1


def test_import_with_fresher_ts_advances_state(conn):
    key = _mk(conn, observation_ts=1000.0)
    _mk(conn, state="hold", hold_reason="no nudge warranted", observation_ts=2000.0)
    row = ledger.get_thread(conn, key)
    assert row["state"] == "hold"
    assert row["hold_reason"] == "no nudge warranted"


def test_observed_ts_monotonic(conn):
    key = _mk(conn, last_outbound_ts=5000.0)
    ledger.update_observed_ts(conn, key, last_outbound_ts=4000.0)
    assert ledger.get_thread(conn, key)["last_outbound_ts"] == 5000.0
    ledger.update_observed_ts(conn, key, last_outbound_ts=6000.0)
    assert ledger.get_thread(conn, key)["last_outbound_ts"] == 6000.0


# --------------------------------------------------------- escalation screen

def test_escalation_screen_matches_keywords():
    assert ledger.escalation_screen("we need to review the contract terms") == "contract"
    assert ledger.escalation_screen("Term Sheet attached") == "term sheet"
    assert ledger.escalation_screen("wire the funds") == "wire"
    assert ledger.escalation_screen("signed NDA") == "nda"
    assert ledger.escalation_screen("live embargo negotiation") == "embargo"


def test_escalation_screen_word_boundaries():
    # 'wire' inside 'wireless', 'nda' inside 'agenda'/'Monday' must not trip.
    assert ledger.escalation_screen("the wireless setup on Monday agenda") is None
    assert ledger.escalation_screen("just checking in on samples") is None


def test_escalation_screen_fail_closed(monkeypatch):
    monkeypatch.setattr(
        ledger, "_ESCALATION_RE", None  # .search on None raises -> screen_error
    )
    assert ledger.escalation_screen("anything") == "screen_error"


# ------------------------------------------------------------- normalization

def test_workstream_normalization():
    assert ledger.normalize_workstream("Finance/Legal") == "Finance-Legal"
    assert ledger.normalize_workstream("Suppliers/Vendors") == "Suppliers"
    assert ledger.normalize_workstream("Leasing/Property") == "Leasing-Property"
    assert ledger.normalize_workstream("Press") == "Press"
    assert ledger.normalize_workstream("weird-label") == "Other"
