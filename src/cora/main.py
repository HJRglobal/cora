"""Entry point — Socket Mode bot startup."""

import logging
import logging.handlers
import os
from datetime import date

from slack_bolt.adapter.socket_mode import SocketModeHandler

from .app import app
from .config import config


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
    stream_handler.setLevel(logging.INFO)

    level = getattr(logging, config.log_level.upper(), logging.INFO)
    logging.basicConfig(level=level, handlers=[file_handler, stream_handler])


def main() -> None:
    _setup_logging()
    log = logging.getLogger(__name__)

    log.info("Cora Socket Mode connecting…")

    handler = SocketModeHandler(app, config.slack_app_token)

    try:
        handler.start()
    except KeyboardInterrupt:
        log.info("Cora shutting down.")


if __name__ == "__main__":
    main()
