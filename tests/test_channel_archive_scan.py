"""Code #16 C1, D-051 r1 c1-false-inactive#1 -- the scan fails closed WITHOUT the monitor.

A10: a channel the ledger shows archived-then-unarchived appears only in section B
(``unarchived_before``). That used to rest entirely on the monitor's ``unarchived_seen``
row; when the monitor had not written it (a history it could not page, a blind night),
a reopened channel came back as a clean section-A row inside Archive-all. Now a channel
whose latest lane archive has no ``unarchived_seen`` after it and that is OPEN in the
member list (the scan lists only open channels) is section B with the date and actor
unknown. Through the REAL dry-run scan (deliver_proposal -> load_context -> scan ->
classify) over the non-dict SlackResponse fake.
"""
from __future__ import annotations

from _chanarch_fakes import DAY, HARRISON, NOW, FakeSlack, chan, msg, no_sleep
from cora.channel_archive import clients, deliver
from cora.channel_archive import store as st

PID = "chanarch-aaaaaaaaaaaa"


def _world(monkeypatch):
    f = FakeSlack(channels=[chan("C0REOPEN01", "fx-reopened"), chan("C0CANDID01", "fx-dead-one")],
                  history={"C0REOPEN01": [msg(191)], "C0CANDID01": [msg(200)]})
    monkeypatch.setattr(clients, "read_client_factory", lambda: f)
    monkeypatch.setattr(clients, "write_client_factory", lambda: f)
    return f


def _archived_by_the_lane(cid="C0REOPEN01", at=NOW - 200 * DAY):
    st.append_ledger("intent", proposal_id=PID, channel_id=cid, tapped_by=HARRISON, ts=at)
    st.append_ledger("outcome", proposal_id=PID, channel_id=cid, outcome="archived", ts=at + 3)


def test_a_reopened_channel_without_the_monitors_row_is_section_b_not_a(monkeypatch):
    _world(monkeypatch)
    _archived_by_the_lane()
    out = deliver.deliver_proposal(trigger="manual", now=NOW, sleep=no_sleep, dry_run=True)
    assert out["blind"] is None
    assert "C0REOPEN01" not in out["candidate_ids"]
    assert out["b_reasons"].get("C0REOPEN01") == "unarchived_before"
    assert out["candidate_ids"] == ["C0CANDID01"]           # a plain dead channel still proposes


def test_the_monitors_row_still_supplies_the_date_and_actor(monkeypatch):
    _world(monkeypatch)
    _archived_by_the_lane()
    st.append_ledger("unarchived_seen", channel_id="C0REOPEN01", unarchive_ts=NOW - 150 * DAY,
                     by="UPERSON01", ts=NOW - 149 * DAY)
    ua = st.unarchive_state(st.read_ledger())
    assert ua["C0REOPEN01"] == {"at": NOW - 150 * DAY, "by": "UPERSON01"}
    out = deliver.deliver_proposal(trigger="manual", now=NOW, sleep=no_sleep, dry_run=True)
    assert out["b_reasons"].get("C0REOPEN01") == "unarchived_before"


def test_unarchive_state_marks_an_unconfirmed_reopen():
    _archived_by_the_lane()
    ua = st.unarchive_state(st.read_ledger())
    assert ua["C0REOPEN01"] == {"at": None, "by": "", "unconfirmed": True}
    # a later unarchived_seen for an EARLIER archive does not cover a re-archive after it
    st.append_ledger("unarchived_seen", channel_id="C0REOPEN01", unarchive_ts=NOW - 190 * DAY,
                     by="UPERSON01", ts=NOW - 189 * DAY)
    _archived_by_the_lane(at=NOW - 100 * DAY)
    assert st.unarchive_state(st.read_ledger())["C0REOPEN01"]["unconfirmed"] is True


def test_a_reconciled_archive_keys_on_its_archive_message_ts():
    """The monitor stamps ts=now on a reconciled outcome; the unarchive it saw came
    BEFORE now, so the pairing must use the archive message's own ts."""
    st.append_ledger("outcome", proposal_id=PID, channel_id="C0REOPEN01", outcome="archived (reconciled)",
                     archive_ts=NOW - 10 * DAY, ts=NOW)
    st.append_ledger("unarchived_seen", channel_id="C0REOPEN01", unarchive_ts=NOW - 5 * DAY,
                     by="UPERSON01", ts=NOW)
    assert st.unarchive_state(st.read_ledger())["C0REOPEN01"] == {"at": NOW - 5 * DAY, "by": "UPERSON01"}
