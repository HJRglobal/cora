"""Entry point — Socket Mode bot startup with auto-restart and lifecycle logging."""

import logging
import logging.handlers
import os
import threading
import time
from datetime import date

from slack_bolt.adapter.socket_mode import SocketModeHandler

from .app import app
from .config import config

# Seconds to wait before each successive restart attempt (capped at last value).
_BACKOFF = (1, 2, 5, 10, 30)

# Reset the restart counter after a session this long (indicates a stable connection).
_STABLE_RUN_SECS = 60


def _setup_logging() -> None:
    log_dir = os.path.join(os.path.dirname(__file__), "..", "..", "logs")
    log_dir = os.path.normpath(log_dir)
    os.makedirs(log_dir, exist_ok=True)

    log_file = os.path.join(log_dir, f"cora-{date.today()}.log")

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_file, when="midnight", backupCount=30, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)

    level = getattr(logging, config.log_level.upper(), logging.INFO)
    logging.basicConfig(level=level, handlers=[file_handler, stream_handler])


def _heartbeat(stop: threading.Event, log: logging.Logger) -> None:
    start = time.monotonic()
    while not stop.wait(60):
        log.info("heartbeat alive uptime_s=%d", int(time.monotonic() - start))


def main() -> None:
    _setup_logging()
    log = logging.getLogger(__name__)
    log.info("Cora starting up…")

    attempt = 0
    last_error = ""

    while True:
        # Backoff before every restart (not before the very first start).
        if attempt > 0:
            delay = _BACKOFF[min(attempt - 1, len(_BACKOFF) - 1)]
            log.warning(
                "Restarting in %ds (restart #%d, last_error=%s)",
                delay,
                attempt,
                last_error,
            )
            try:
                time.sleep(delay)
            except KeyboardInterrupt:
                log.info("Cora shutting down (KeyboardInterrupt).")
                return

        attempt += 1
        log.info("Cora Socket Mode connecting… (attempt #%d)", attempt)

        stop_heartbeat = threading.Event()
        heartbeat = threading.Thread(
            target=_heartbeat,
            args=(stop_heartbeat, log),
            name="Heartbeat",
            daemon=True,
        )

        handler: SocketModeHandler | None = None
        run_start = time.monotonic()

        try:
            handler = SocketModeHandler(app, config.slack_app_token)

            # Hook the SDK's lifecycle events with our logger so disconnects appear
            # in *our* log file regardless of how Bolt's internal logger is configured.
            handler.client.on_close_listeners.append(
                lambda code, reason: log.warning(
                    "WebSocket CLOSE received (code=%s reason=%s)", code, reason or ""
                )
            )
            handler.client.on_error_listeners.append(
                lambda exc: log.error(
                    "WebSocket error: %s: %s", type(exc).__name__, exc
                )
            )

            heartbeat.start()
            handler.start()

            # Reaching here means handler.start() returned without raising — unexpected.
            runtime = time.monotonic() - run_start
            if runtime >= _STABLE_RUN_SECS:
                attempt = 0
            last_error = f"handler.start() returned without exception after {runtime:.1f}s"
            log.error(last_error)

        except KeyboardInterrupt:
            log.info("Cora shutting down (KeyboardInterrupt).")
            stop_heartbeat.set()
            return

        except Exception as exc:
            runtime = time.monotonic() - run_start
            if runtime >= _STABLE_RUN_SECS:
                attempt = 0
            last_error = f"{type(exc).__name__}: {exc}"
            log.exception("SocketModeHandler raised after %.1fs: %s", runtime, exc)

        finally:
            stop_heartbeat.set()
            if handler is not None:
                try:
                    handler.close()
                except Exception:
                    pass


if __name__ == "__main__":
    main()
