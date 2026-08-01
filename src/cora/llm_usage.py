"""Shared usage-line logger for direct anthropic call sites outside claude_client.

Closes the script-side observability gap (2026-07-31 batch-API pilot, slice 1):
Cora's scheduled scripts and connectors call the Anthropic API directly and
previously logged NO usage lines, so their spend was invisible to
``cora_health_report.py``'s billing parse. Every such call site now logs ONE
uniform line via :func:`log_usage`:

    claude usage iter=1 input=123 cache_create=0 cache_read=0 output=45 model=claude-haiku-4-5 caller=gap_autofill

The line PREFIX mirrors ``claude_client._log_usage`` exactly (field names,
order, single spaces) so the existing billing regex keeps matching bot lines;
``model=`` / ``caller=`` (and the optional ``via=`` the batch client adds) are
TRAILING additions that the unanchored regex tolerates. The health report
buckets lines BY the presence of ``caller=``: absent -> bot (claude_client),
present -> script-side, keyed per caller.

STDLIB-ONLY by design: this module must never import app.py / tool_dispatch /
claude_client / anthropic, so the D-047 standalone modules (channel_synthesis,
strategy_memo, friction_mining, session_capture, ...) can import it without
violating their no-bot-process-import guard.
"""

from __future__ import annotations

import logging
from typing import Any

_log = logging.getLogger("cora.llm_usage")

# Fields are single-space separated; a value containing whitespace would break
# the parse, so values are sanitized defensively (model ids / caller slugs
# never legitimately contain whitespace).
_LINE = "claude usage iter=%d input=%d cache_create=%d cache_read=%d output=%d model=%s caller=%s"


def _token(value: Any) -> str:
    """Coerce a field value to a single whitespace-free token ('-' if empty)."""
    text = str(value or "").strip()
    if not text:
        return "-"
    return "_".join(text.split())


def log_usage(
    response: Any,
    *,
    caller: str,
    model: str = "",
    iteration: int = 1,
    via: str = "",
    logger: logging.Logger | None = None,
) -> None:
    """Log one uniform usage line for an Anthropic ``Message`` response.

    ``caller`` is a stable short slug identifying the call site (e.g.
    ``"gap_autofill"``, ``"code_queue.kickoff"``). ``model`` defaults to
    ``response.model`` (always populated on a real API Message); pass it
    explicitly only when the response object may lack it. ``via`` marks the
    transport on batch-client paths (``"batch"`` / ``"sync-fallback"``).

    NEVER raises and NEVER logs message content -- observability only, exactly
    like claude_client._log_usage (mocks / malformed usage objects are skipped
    silently).
    """
    try:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        cache_create = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        model_token = _token(model or getattr(response, "model", ""))
        line = _LINE
        args: list[Any] = [
            int(iteration), input_tokens, cache_create, cache_read,
            output_tokens, model_token, _token(caller),
        ]
        if via:
            line += " via=%s"
            args.append(_token(via))
        (logger or _log).info(line, *args)
    except (TypeError, ValueError, AttributeError):
        # Mock usage objects, missing fields, etc. -- skip silently.
        pass
