"""Entry point — Socket Mode bot startup with auto-restart and lifecycle logging."""

import logging
import logging.handlers
import os
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path

from slack_bolt.adapter.socket_mode import SocketModeHandler

from .app import app
from .config import config
from .context_loader import _load_static_context

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HEARTBEAT_FILE = _REPO_ROOT / "data" / "health" / "heartbeat.txt"

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
        uptime = int(time.monotonic() - start)
        log.info("heartbeat alive uptime_s=%d", uptime)
        try:
            _HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
            _HEARTBEAT_FILE.write_text(
                datetime.now(timezone.utc).isoformat() + "\n", encoding="utf-8"
            )
        except Exception as exc:
            log.warning("heartbeat: failed to write sentinel file: %s", exc)


_ALL_ENTITIES = [
    "FNDR", "HJRG", "F3E", "F3C", "OSN", "LEX",
    "LEX-LLC", "LEX-LTS", "LEX-LBHS", "LEX-LLA",
    "UFL", "BDM", "HJRP", "HJRPROD",
]


def _prewarm_contexts(log: logging.Logger) -> None:
    """Load all entity CLAUDE.md files into the TTL cache at startup.

    Runs in a background daemon thread so it doesn't delay Socket Mode connection.
    Eliminates the first-request cold-cache penalty (up to 2s per entity per 5-min window).
    """
    loaded = 0
    for entity in _ALL_ENTITIES:
        try:
            _load_static_context(entity)
            loaded += 1
        except Exception as exc:
            log.warning("prewarm: failed for entity=%s: %s", entity, exc)
    log.info("prewarm: loaded %d/%d entity contexts into cache", loaded, len(_ALL_ENTITIES))


def _prewarm_kb(log: logging.Logger) -> None:
    """Warm the KB vector index at startup so the first complex query is fast.

    The first vector search after a restart pays a one-time disk cost loading the
    sqlite-vec (vec0) index over the ~200K-chunk KB into the OS page cache — observed
    ~25s on 2026-06-06. This issues one throwaway nearest-neighbour scan with a dummy
    zero vector (no OpenAI embed call, supplied via query_vec=) to absorb that cost
    before any user request. Runs in a background daemon thread.
    """
    try:
        from .context_loader import _KB_DB_PATH
        from .knowledge_base import KnowledgeBase

        if not _KB_DB_PATH.exists():
            log.info("kb-prewarm: no KB db at %s — skipping", _KB_DB_PATH)
            return
        start = time.monotonic()
        kb = KnowledgeBase(_KB_DB_PATH)
        try:
            # 1536 dims = text-embedding-3-small. Zero vector is a valid MATCH target;
            # we only care about loading the index pages, not the results.
            kb.search("", entity="FNDR", k=1, max_age_days=None, query_vec=[0.0] * 1536)
        finally:
            kb.close()
        log.info("kb-prewarm: vector index warmed in %.1fs", time.monotonic() - start)
    except Exception as exc:
        log.warning("kb-prewarm: failed (non-fatal): %s", exc)


def main() -> None:
    _setup_logging()
    log = logging.getLogger(__name__)
    log.info("Cora starting up…")

    # Pre-warm all entity contexts in background so first requests don't pay the
    # cold-cache penalty (Google Drive read per entity, up to 2s each).
    threading.Thread(
        target=_prewarm_contexts,
        args=(log,),
        name="ContextPrewarm",
        daemon=True,
    ).start()

    # Warm the KB vector index too (separate thread — it's the slow one, ~25s, and
    # must not delay the context prewarm or Socket Mode connection).
    threading.Thread(
        target=_prewarm_kb,
        args=(log,),
        name="KBPrewarm",
        daemon=True,
    ).start()

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
