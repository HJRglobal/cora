"""Local /health endpoint + outbound dead-man's-switch ping (Stage 2 item 1).

Two pieces, both optional and fail-soft:

1. HTTP /health endpoint (HEALTH_PORT, default 8787; HEALTH_PORT=0 disables).
   Binds 127.0.0.1 by default (HEALTH_BIND to override). Returns 200 + JSON when
   the heartbeat sentinel is fresh, 503 when stale/missing. Serves local checks
   today and a Cloudflare Tunnel probe later -- an external monitor cannot reach
   this desktop directly.

2. Outbound ping loop (HEALTH_PING_URL, off unless set). The piece that actually
   detects a dead machine: every HEALTH_PING_INTERVAL_S (default 300) it GETs the
   configured dead-man's-switch URL (healthchecks.io free tier / UptimeRobot
   heartbeat monitor) -- but only while the heartbeat sentinel is fresh, so the
   external service alerts when pings stop for ANY reason: process crash, machine
   off, power loss, network down.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
HEARTBEAT_FILE = _REPO_ROOT / "data" / "health" / "heartbeat.txt"

# Heartbeat writes every 60s; three missed beats = stale.
FRESH_SECS = 180

_DEFAULT_PORT = 8787
_DEFAULT_BIND = "127.0.0.1"
_DEFAULT_PING_INTERVAL_S = 300
# First ping fires early so the monitor sees life soon after a restart, but late
# enough that the heartbeat thread has written its first sentinel (60s cadence).
_FIRST_PING_DELAY_S = 90

log = logging.getLogger("cora.health")


def heartbeat_age_seconds(path: Path | None = None) -> float | None:
    """Age of the heartbeat sentinel in seconds, or None if missing/unparseable."""
    if path is None:
        path = HEARTBEAT_FILE  # resolved at call time so tests can patch the module attr
    try:
        content = path.read_text(encoding="utf-8").strip()
        hb_time = datetime.fromisoformat(content.replace("Z", "+00:00"))
        if hb_time.tzinfo is None:
            hb_time = hb_time.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - hb_time).total_seconds()
    except Exception:
        return None


def health_payload(path: Path | None = None) -> tuple[bool, dict]:
    """(healthy, JSON-able payload) based on heartbeat sentinel freshness."""
    age = heartbeat_age_seconds(path)
    healthy = age is not None and age <= FRESH_SECS
    return healthy, {
        "status": "ok" if healthy else "stale",
        "heartbeat_age_s": None if age is None else round(age, 1),
        "fresh_threshold_s": FRESH_SECS,
    }


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        if self.path.split("?")[0].rstrip("/") not in ("", "/health"):
            self.send_error(404)
            return
        healthy, payload = health_payload()
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200 if healthy else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        # Route http.server's per-request stderr chatter to our logger at DEBUG.
        log.debug("health-endpoint: " + fmt, *args)


def start_health_server() -> ThreadingHTTPServer | None:
    """Start the /health HTTP server in a daemon thread. Returns None if disabled."""
    raw_port = os.environ.get("HEALTH_PORT", str(_DEFAULT_PORT)).strip()
    try:
        port = int(raw_port)
    except ValueError:
        log.warning("health-endpoint: invalid HEALTH_PORT=%r -- disabled", raw_port)
        return None
    if port <= 0:
        log.info("health-endpoint: disabled (HEALTH_PORT=%s)", raw_port)
        return None
    bind = os.environ.get("HEALTH_BIND", _DEFAULT_BIND).strip() or _DEFAULT_BIND
    try:
        server = ThreadingHTTPServer((bind, port), _HealthHandler)
    except OSError as exc:
        log.warning("health-endpoint: could not bind %s:%d (%s) -- disabled", bind, port, exc)
        return None
    threading.Thread(
        target=server.serve_forever, name="HealthEndpoint", daemon=True
    ).start()
    log.info("health-endpoint: serving http://%s:%d/health", bind, port)
    return server


def _ping_once(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            resp.read()
        return True
    except Exception as exc:
        log.warning("health-ping: ping failed (non-fatal): %s", exc)
        return False


def ping_loop(
    stop: threading.Event,
    url: str | None = None,
    interval_s: int | None = None,
    heartbeat_path: Path | None = None,
) -> None:
    """Dead-man's-switch loop: GET url every interval while the heartbeat is fresh.

    Skipping the ping when the heartbeat is stale is deliberate -- a wedged bot
    with a live ping loop must still trip the external alert.
    """
    url = (url if url is not None else os.environ.get("HEALTH_PING_URL", "")).strip()
    if not url:
        log.info("health-ping: disabled (HEALTH_PING_URL not set)")
        return
    if interval_s is None:
        try:
            interval_s = int(os.environ.get("HEALTH_PING_INTERVAL_S", str(_DEFAULT_PING_INTERVAL_S)))
        except ValueError:
            interval_s = _DEFAULT_PING_INTERVAL_S
    log.info("health-ping: enabled, interval=%ds", interval_s)

    delay = _FIRST_PING_DELAY_S
    while not stop.wait(delay):
        delay = interval_s
        age = heartbeat_age_seconds(heartbeat_path)
        if age is None or age > FRESH_SECS:
            log.warning("health-ping: skipping ping (heartbeat stale, age_s=%s)", age)
            continue
        _ping_once(url)


def start_ping_thread() -> threading.Event:
    """Start the dead-man's-switch ping loop in a daemon thread.

    Returns the stop Event (left unset for process lifetime).
    """
    stop = threading.Event()
    threading.Thread(
        target=ping_loop, args=(stop,), name="HealthPing", daemon=True
    ).start()
    return stop
