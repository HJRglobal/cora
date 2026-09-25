"""The Slack clients the dead-channel lane builds for itself (Code #16 C1, A11/A26).

  * READS go through a client with RateLimitErrorRetryHandler(5) (+ one connection
    retry -- a read is idempotent), paced by the scan itself.
  * WRITES -- the proposal card, the in-channel notice, conversations.archive and the
    correction line -- go through a client with ``retry_handlers=[]``: the SDK's
    default ConnectionErrorRetryHandler can fire AFTER Slack processed the request,
    which would post the notice twice and turn our own archive into a false
    ``already_archived``. An indeterminate write is read back, never re-sent.

PYTEST BELT (A26): conftest has no socket belt, and a script import runs
load_dotenv(override=True) -- in the primary checkout that puts the LIVE bot token
in os.environ. Every default factory therefore RAISES under pytest unless a test
injected its own (copy of code_queue._real_founder_os_target_under_pytest's shape).
"""
from __future__ import annotations

import os
import sys
from typing import Any, Callable


class LiveClientRefused(RuntimeError):
    """A default Slack client was requested inside a pytest run."""


def _under_pytest() -> bool:
    return "pytest" in sys.modules


def _token() -> str:
    return str(os.environ.get("SLACK_BOT_TOKEN", "") or "").strip()


def _build_read() -> Any:
    if _under_pytest():
        raise LiveClientRefused("channel_archive: a LIVE Slack read client was requested "
                                "under pytest -- inject a fake")
    from slack_sdk import WebClient  # noqa: PLC0415
    from slack_sdk.http_retry.builtin_handlers import (  # noqa: PLC0415
        ConnectionErrorRetryHandler, RateLimitErrorRetryHandler)
    token = _token()
    if not token:
        raise LiveClientRefused("channel_archive: SLACK_BOT_TOKEN is not set")
    return WebClient(token=token, retry_handlers=[RateLimitErrorRetryHandler(max_retry_count=5),
                                                  ConnectionErrorRetryHandler(max_retry_count=1)])


def _build_write() -> Any:
    if _under_pytest():
        raise LiveClientRefused("channel_archive: a LIVE Slack write client was requested "
                                "under pytest -- inject a fake")
    from slack_sdk import WebClient  # noqa: PLC0415
    token = _token()
    if not token:
        raise LiveClientRefused("channel_archive: SLACK_BOT_TOKEN is not set")
    return WebClient(token=token, retry_handlers=[])


#: Tests replace these two names (monkeypatch) with factories returning fakes.
read_client_factory: Callable[[], Any] = _build_read
write_client_factory: Callable[[], Any] = _build_write


def read_client() -> Any:
    return read_client_factory()


def write_client() -> Any:
    return write_client_factory()
