"""Code #16 C1, D-051 r1 c1-monitor#0 -- the bot's SCAN pool crash path settles its own
stall finding. A scan that raises after ``scan_started`` has already DM'd Harrison the
honest failure line (A18); the pool body now also appends ``scan_failed`` for the scan
it started, so the nightly monitor does not WARN "never staged a card" forever.

Through the REAL ``_ca_run_scan`` and the REAL ``deliver_proposal``; only the scan
body is made to raise, and every Slack client is the non-dict SlackResponse fake.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import cora.app as app_module
from _chanarch_fakes import FakeSlack, chan, msg
from cora.channel_archive import clients, intents
from cora.channel_archive import monitor as mon
from cora.channel_archive import store as st
from test_channel_archive_monitor import MonSlack


def _texts(client) -> list:
    return [c.kwargs.get("text") for c in client.chat_postMessage.call_args_list]


def test_the_pool_crash_path_records_scan_failed_and_the_stall_settles(monkeypatch):
    f = FakeSlack(channels=[chan("C0CANDID01", "fx-dead-one")],
                  history={"C0CANDID01": [msg(200)]})
    monkeypatch.setattr(clients, "read_client_factory", lambda: f)
    monkeypatch.setattr(clients, "write_client_factory", lambda: f)

    def _boom(*a, **k):
        raise RuntimeError("scan blew up")
    monkeypatch.setattr("cora.channel_archive.scan.scan", _boom)
    client = MagicMock()
    assert app_module._CHANNEL_ARCHIVE_SCAN_GUARD.acquire(blocking=False)
    app_module._ca_run_scan(client, "DHARRISON1", None, "ask")
    assert _texts(client) == [intents.SCAN_FAILED_REPLY]
    evs = st.read_events()
    started = [e["scan_id"] for e in evs if e["event"] == "scan_started"]
    failed = [e for e in evs if e["event"] == "scan_failed"]
    assert len(started) == 1 and [e["scan_id"] for e in failed] == started
    assert failed[0]["error"] == "RuntimeError" and failed[0]["trigger"] == "ask"
    out = mon.reconcile(MonSlack(), now=time.time() + 2 * 3600, sleep=lambda s: None)
    assert not any("never staged" in x for x in out["findings"]), out["findings"]
    assert not app_module._CHANNEL_ARCHIVE_SCAN_GUARD.locked()


def test_record_scan_failed_settles_only_its_own_trigger_and_window():
    st.append_event("scan_started", scan_id="monthly001", trigger="monthly", ts=1000.0)
    st.append_event("scan_started", scan_id="askold0001", trigger="ask", ts=500.0)
    st.append_event("scan_started", scan_id="asknew0001", trigger="ask", ts=2000.0)
    st.append_event("scan_started", scan_id="askdone001", trigger="ask", ts=2100.0)
    st.append_event("staged", proposal_id="chanarch-aaaaaaaaaaaa", scan_id="askdone001", ts=2101.0)
    assert st.record_scan_failed(trigger="ask", since=1999.5, error="RuntimeError") == ["asknew0001"]
    assert st.record_scan_failed(trigger="ask", since=1999.5, error="RuntimeError") == []   # idempotent
