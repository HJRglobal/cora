"""Slack event-delivery idempotency (cq-479b157f8c00).

Socket Mode is at-least-once: an ack lost on a flapping WebSocket makes Slack
redeliver, and each delivery previously ran the FULL Q&A pipeline (two
contradictory replies 14ms apart, live 7/27). Pins the seen-cache atomics and
the middleware's halt/pass-through/fail-open contract.
"""

from __future__ import annotations

import threading
import time

import pytest

from cora import event_dedup


@pytest.fixture(autouse=True)
def _reset():
    event_dedup.reset_for_tests()
    yield
    event_dedup.reset_for_tests()


class TestSeenCache:
    def test_first_seen_false_repeat_true(self):
        assert event_dedup.is_duplicate("Ev123") is False
        assert event_dedup.is_duplicate("Ev123") is True
        assert event_dedup.is_duplicate("Ev456") is False  # distinct id unaffected

    def test_empty_id_never_dedups(self):
        assert event_dedup.is_duplicate("") is False
        assert event_dedup.is_duplicate("") is False

    def test_ttl_expiry_reallows(self, monkeypatch):
        t = {"now": 1000.0}
        monkeypatch.setattr(event_dedup.time, "monotonic", lambda: t["now"])
        assert event_dedup.is_duplicate("EvT", ttl_secs=60) is False
        t["now"] = 1030.0
        assert event_dedup.is_duplicate("EvT", ttl_secs=60) is True   # inside TTL
        t["now"] = 1070.0
        assert event_dedup.is_duplicate("EvT", ttl_secs=60) is False  # expired

    def test_concurrent_race_exactly_one_wins(self):
        results: list[bool] = []
        barrier = threading.Barrier(8)

        def probe():
            barrier.wait()
            results.append(event_dedup.is_duplicate("EvRACE"))

        threads = [threading.Thread(target=probe) for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert results.count(False) == 1   # exactly one delivery proceeds
        assert results.count(True) == 7

    def test_prune_keeps_dict_bounded(self, monkeypatch):
        t = {"now": 1000.0}
        monkeypatch.setattr(event_dedup.time, "monotonic", lambda: t["now"])
        for i in range(4100):
            event_dedup.is_duplicate(f"Ev{i}", ttl_secs=60)
        t["now"] = 2000.0  # everything above is past TTL
        event_dedup.is_duplicate("EvFRESH", ttl_secs=60)  # triggers the prune
        assert len(event_dedup._SEEN) < 4100


class TestMiddleware:
    """The app.py middleware contract, exercised via the real registered function."""

    def _mw(self):
        from cora import app as app_mod
        return app_mod._dedup_event_deliveries

    def _body(self, event_id, etype="app_mention"):
        return {"event_id": event_id, "event": {"type": etype, "ts": "1.2"}}

    def test_second_delivery_halted_with_200_ack(self):
        import logging
        mw = self._mw()
        calls = {"n": 0}

        def nxt():
            calls["n"] += 1

        body = self._body("EvDUP1")
        assert mw(body=body, next=nxt, logger=logging.getLogger("t")) is None
        resp = mw(body=body, next=nxt, logger=logging.getLogger("t"))
        assert calls["n"] == 1                    # dispatched exactly once
        assert resp is not None and resp.status == 200  # halted AND acked

    def test_no_event_id_always_passes_through(self):
        # Commands / block-actions / shortcuts carry no event_id.
        import logging
        mw = self._mw()
        calls = {"n": 0}

        def nxt():
            calls["n"] += 1

        for _ in range(2):
            mw(body={"command": "/cora-ask", "text": "hi"}, next=nxt,
               logger=logging.getLogger("t"))
        assert calls["n"] == 2

    def test_seen_cache_error_fails_open(self, monkeypatch):
        import logging
        mw = self._mw()
        monkeypatch.setattr(event_dedup, "is_duplicate",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        calls = {"n": 0}

        def nxt():
            calls["n"] += 1

        mw(body=self._body("EvERR"), next=nxt, logger=logging.getLogger("t"))
        assert calls["n"] == 1                    # dedup failure never blocks dispatch

    def test_distinct_event_ids_same_message_both_pass(self):
        """The app_mention + message dual-path for ONE Slack message has DIFFERENT
        event_ids by design (W1-01 governs that class) -- both must dispatch."""
        import logging
        mw = self._mw()
        calls = {"n": 0}

        def nxt():
            calls["n"] += 1

        mw(body=self._body("EvA", "app_mention"), next=nxt, logger=logging.getLogger("t"))
        mw(body=self._body("EvB", "message"), next=nxt, logger=logging.getLogger("t"))
        assert calls["n"] == 2
