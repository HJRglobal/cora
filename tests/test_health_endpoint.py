"""Tests for src/cora/health_endpoint.py (Stage 2 uptime monitoring)."""

import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cora import health_endpoint as he  # noqa: E402


def _write_heartbeat(path: Path, age_seconds: float) -> None:
    ts = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ts.isoformat() + "\n", encoding="utf-8")


# ── heartbeat_age_seconds ─────────────────────────────────────────────────────


class TestHeartbeatAge:
    def test_fresh_heartbeat_age(self, tmp_path):
        hb = tmp_path / "heartbeat.txt"
        _write_heartbeat(hb, age_seconds=10)
        age = he.heartbeat_age_seconds(hb)
        assert age is not None
        assert 5 <= age <= 60

    def test_missing_file_returns_none(self, tmp_path):
        assert he.heartbeat_age_seconds(tmp_path / "nope.txt") is None

    def test_garbage_content_returns_none(self, tmp_path):
        hb = tmp_path / "heartbeat.txt"
        hb.write_text("not a timestamp", encoding="utf-8")
        assert he.heartbeat_age_seconds(hb) is None

    def test_naive_timestamp_treated_as_utc(self, tmp_path):
        hb = tmp_path / "heartbeat.txt"
        naive = datetime.now(timezone.utc).replace(tzinfo=None)
        hb.write_text(naive.isoformat(), encoding="utf-8")
        age = he.heartbeat_age_seconds(hb)
        assert age is not None
        assert age < 60


# ── health_payload ────────────────────────────────────────────────────────────


class TestHealthPayload:
    def test_fresh_is_ok(self, tmp_path):
        hb = tmp_path / "heartbeat.txt"
        _write_heartbeat(hb, age_seconds=10)
        healthy, payload = he.health_payload(hb)
        assert healthy is True
        assert payload["status"] == "ok"
        assert payload["heartbeat_age_s"] >= 0

    def test_stale_is_not_ok(self, tmp_path):
        hb = tmp_path / "heartbeat.txt"
        _write_heartbeat(hb, age_seconds=he.FRESH_SECS + 60)
        healthy, payload = he.health_payload(hb)
        assert healthy is False
        assert payload["status"] == "stale"

    def test_missing_is_not_ok(self, tmp_path):
        healthy, payload = he.health_payload(tmp_path / "nope.txt")
        assert healthy is False
        assert payload["heartbeat_age_s"] is None


# ── HTTP endpoint ─────────────────────────────────────────────────────────────


@pytest.fixture
def http_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), he._HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _get(server, path):
    port = server.server_address[1]
    try:
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5)
        return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


class TestHttpEndpoint:
    def test_health_200_when_fresh(self, http_server, tmp_path):
        hb = tmp_path / "heartbeat.txt"
        _write_heartbeat(hb, age_seconds=5)
        with patch.object(he, "HEARTBEAT_FILE", hb):
            status, body = _get(http_server, "/health")
        assert status == 200
        assert json.loads(body)["status"] == "ok"

    def test_health_503_when_stale(self, http_server, tmp_path):
        hb = tmp_path / "heartbeat.txt"
        _write_heartbeat(hb, age_seconds=he.FRESH_SECS + 120)
        with patch.object(he, "HEARTBEAT_FILE", hb):
            status, body = _get(http_server, "/health")
        assert status == 503
        assert json.loads(body)["status"] == "stale"

    def test_health_503_when_missing(self, http_server, tmp_path):
        with patch.object(he, "HEARTBEAT_FILE", tmp_path / "nope.txt"):
            status, _ = _get(http_server, "/health")
        assert status == 503

    def test_unknown_path_404(self, http_server):
        status, _ = _get(http_server, "/admin")
        assert status == 404

    def test_root_path_serves_health(self, http_server, tmp_path):
        hb = tmp_path / "heartbeat.txt"
        _write_heartbeat(hb, age_seconds=5)
        with patch.object(he, "HEARTBEAT_FILE", hb):
            status, _ = _get(http_server, "/")
        assert status == 200


# ── start_health_server config gating ─────────────────────────────────────────


class TestStartHealthServer:
    def test_port_zero_disables(self, monkeypatch):
        monkeypatch.setenv("HEALTH_PORT", "0")
        assert he.start_health_server() is None

    def test_invalid_port_disables(self, monkeypatch):
        monkeypatch.setenv("HEALTH_PORT", "banana")
        assert he.start_health_server() is None

    def test_default_port_starts_and_serves(self, monkeypatch, tmp_path):
        # Ephemeral-ish port to avoid collisions with a live Cora on 8787.
        monkeypatch.setenv("HEALTH_PORT", "18791")
        monkeypatch.setenv("HEALTH_BIND", "127.0.0.1")
        server = he.start_health_server()
        assert server is not None
        try:
            hb = tmp_path / "heartbeat.txt"
            _write_heartbeat(hb, age_seconds=5)
            with patch.object(he, "HEARTBEAT_FILE", hb):
                status, _ = _get(server, "/health")
            assert status == 200
        finally:
            server.shutdown()
            server.server_close()


# ── ping loop ─────────────────────────────────────────────────────────────────


class TestPingLoop:
    def test_no_url_returns_immediately(self, monkeypatch):
        monkeypatch.delenv("HEALTH_PING_URL", raising=False)
        stop = threading.Event()
        # Must return without waiting (no URL configured).
        he.ping_loop(stop, url="")

    def test_pings_when_fresh(self, tmp_path):
        hb = tmp_path / "heartbeat.txt"
        _write_heartbeat(hb, age_seconds=5)
        stop = threading.Event()
        calls = []

        def fake_ping(url):
            calls.append(url)
            stop.set()  # one ping then exit
            return True

        with patch.object(he, "_ping_once", fake_ping), \
             patch.object(he, "_FIRST_PING_DELAY_S", 0.01):
            he.ping_loop(stop, url="https://hc-ping.example/abc", interval_s=1,
                         heartbeat_path=hb)
        assert calls == ["https://hc-ping.example/abc"]

    def test_skips_ping_when_stale(self, tmp_path):
        hb = tmp_path / "heartbeat.txt"
        _write_heartbeat(hb, age_seconds=he.FRESH_SECS + 60)
        stop = threading.Event()
        calls = []

        def fake_ping(url):
            calls.append(url)
            return True

        original_wait = stop.wait
        waits = {"n": 0}

        def counting_wait(timeout=None):
            waits["n"] += 1
            if waits["n"] >= 3:
                stop.set()
                return True
            return original_wait(0.01)

        with patch.object(he, "_ping_once", fake_ping), \
             patch.object(he, "_FIRST_PING_DELAY_S", 0.01), \
             patch.object(stop, "wait", counting_wait):
            he.ping_loop(stop, url="https://hc-ping.example/abc", interval_s=1,
                         heartbeat_path=hb)
        assert calls == []  # stale heartbeat must never ping

    def test_ping_failure_is_nonfatal(self, tmp_path):
        hb = tmp_path / "heartbeat.txt"
        _write_heartbeat(hb, age_seconds=5)
        stop = threading.Event()
        attempts = {"n": 0}

        def failing_urlopen(*a, **k):
            attempts["n"] += 1
            stop.set()
            raise OSError("connection refused")

        with patch.object(he.urllib.request, "urlopen", failing_urlopen), \
             patch.object(he, "_FIRST_PING_DELAY_S", 0.01):
            he.ping_loop(stop, url="https://hc-ping.example/abc", interval_s=1,
                         heartbeat_path=hb)
        assert attempts["n"] == 1  # raised, loop survived and exited via stop


# ── start_ping_thread ─────────────────────────────────────────────────────────


def test_start_ping_thread_returns_stop_event(monkeypatch):
    monkeypatch.setenv("HEALTH_PING_URL", "")
    stop = he.start_ping_thread()
    assert isinstance(stop, threading.Event)
    assert not stop.is_set()
