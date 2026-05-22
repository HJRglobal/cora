"""Anthropic Claude API client with retry logic + tool-use loop."""

import logging
import time

import anthropic

from .config import config
from .tools.tool_dispatch import TOOL_DEFINITIONS, dispatch

log = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 2048
_TIMEOUT = 60.0  # bumped 25→60 for tool-use loops where the second pass synthesizes
                 # large tool results (e.g. 25-event week calendar). Anthropic SDK has
                 # its own internal retries; we just need to give them headroom.
_RETRY_DELAYS = (1, 2)  # seconds before attempt 1 and attempt 2
_MAX_TOOL_ITERATIONS = 3  # safety cap on tool-use loop

_client = anthropic.Anthropic(api_key=config.anthropic_api_key)


class ClaudeClientError(Exception):
    """Raised when the Claude API fails after all retries.

    The underlying anthropic exception is preserved on `__cause__` (via
    `raise ... from exc` below) so callers can classify the failure for
    user-facing messages via user_facing_message().
    """


def user_facing_message(exc: ClaudeClientError) -> str:
    """Return a user-readable message for a ClaudeClientError.

    Classifies the underlying anthropic exception and returns a specific
    message per failure mode so the user knows whether to retry, whether
    it's their problem, or whether Harrison needs to intervene.

    Falls back to the generic "trouble reaching Claude" string when the
    underlying exception is unknown or missing.
    """
    underlying = exc.__cause__

    # Anthropic exception hierarchy:
    #   anthropic.APIStatusError  (parent of HTTP-status errors)
    #     - anthropic.AuthenticationError    → 401
    #     - anthropic.PermissionDeniedError  → 403
    #     - anthropic.BadRequestError        → 400
    #     - anthropic.NotFoundError          → 404
    #     - anthropic.RateLimitError         → 429
    #     - anthropic.InternalServerError    → 5xx (including 529 overloaded)
    #   anthropic.APIConnectionError  (network)
    #     - anthropic.APITimeoutError

    if isinstance(underlying, anthropic.APIStatusError):
        status = getattr(underlying, "status_code", 0)
        if status == 529:
            return (
                "Anthropic's API is overloaded right now (HTTP 529). This usually "
                "clears in a minute or two — please retry."
            )
        if status in (401, 403):
            return (
                f"Cora's API key is failing (HTTP {status}). Harrison needs to check "
                f"the Anthropic API key — Cora can't recover from this without intervention."
            )
        if status == 429:
            return (
                "Hit Anthropic's rate limit (HTTP 429). Wait about 30 seconds and "
                "retry — Cora will throttle herself if this keeps happening."
            )
        if status == 400:
            return (
                "Cora sent a request Anthropic didn't accept (HTTP 400). This is "
                "likely a bug — Harrison should check Cora's logs. Try rephrasing "
                "your question in case it helps."
            )
        if status >= 500:
            return (
                f"Anthropic's API is having upstream issues (HTTP {status}). Try "
                f"again in a few minutes."
            )
        # Any other status code we didn't explicitly map
        return (
            f"Claude API returned HTTP {status} — usually transient. Try again in a "
            f"moment; if it persists, Harrison should check the logs."
        )

    if isinstance(underlying, anthropic.APITimeoutError):
        return (
            "Anthropic took too long to respond. Try a shorter or simpler question, "
            "or retry — Cora may have hit a complexity wall on this request."
        )

    if isinstance(underlying, anthropic.APIConnectionError):
        return (
            "Network trouble reaching Anthropic. If this keeps happening, the host "
            "machine's internet might be having issues — Harrison should check."
        )

    # Fallback for any other failure mode (including ClaudeClientError raised
    # without an underlying exception, e.g. the "Tool-use loop exited unexpectedly"
    # path at the bottom of generate_response).
    return "I'm having trouble reaching Claude right now — try again in a moment."


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (anthropic.APIConnectionError, anthropic.APITimeoutError)):
        return True
    if isinstance(exc, anthropic.RateLimitError):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code >= 500
    return False


def _create_with_retry(**kwargs) -> anthropic.types.Message:
    """Call messages.create with retry on transient errors."""
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            return _client.messages.create(**kwargs)
        except Exception as exc:
            if _is_retryable(exc) and attempt < 2:
                delay = _RETRY_DELAYS[attempt]
                log.warning(
                    "Claude API transient error (attempt %d/3), retrying in %ds: %s",
                    attempt + 1, delay, exc,
                )
                time.sleep(delay)
                last_exc = exc
            else:
                raise ClaudeClientError(f"Claude API error: {exc}") from exc
    raise ClaudeClientError(f"Claude API failed after 3 attempts: {last_exc}") from last_exc


def _extract_text(response: anthropic.types.Message) -> str:
    """Pull text content from a response that may also have tool_use blocks."""
    parts = []
    for block in response.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(p for p in parts if p).strip()


def generate_response(
    system_prompt: str,
    context: str,
    user_message: str,
    slack_user_id: str = "",
    entity: str = "FNDR",
) -> str:
    """Call Claude (with tool-use loop) and return the final response text.

    slack_user_id is bound into the tool dispatcher so tools like asana_get_my_tasks
    resolve to the right Asana account. Pass empty string if there's no asking user
    (in which case tools that need a user will return a graceful error).

    entity is the routed channel entity (F3E, LEX, OSN, BDM, FNDR, etc.) — passed
    through to the dispatcher so tools can scope their results to the channel's entity.

    Raises ClaudeClientError on hard failure after retries.
    """
    system = system_prompt + "\n\n---\n\n# Context\n\n" + context

    # Conversation accumulator — starts with the user's message, grows with each
    # tool_use / tool_result exchange.
    messages: list[dict] = [{"role": "user", "content": user_message}]

    for iteration in range(_MAX_TOOL_ITERATIONS + 1):
        response = _create_with_retry(
            model=_MODEL,
            max_tokens=_MAX_TOKENS,
            system=system,
            messages=messages,
            tools=TOOL_DEFINITIONS,
            timeout=_TIMEOUT,
        )

        if response.stop_reason != "tool_use":
            # Model is done — return whatever text it produced
            return _extract_text(response) or "(Cora returned no text)"

        if iteration >= _MAX_TOOL_ITERATIONS:
            log.warning(
                "Tool-use iteration cap (%d) hit — returning partial response",
                _MAX_TOOL_ITERATIONS,
            )
            return _extract_text(response) or (
                "I tried to look that up but couldn't finish in time — try rephrasing."
            )

        # Capture assistant turn (must include tool_use blocks verbatim per API contract)
        messages.append({"role": "assistant", "content": response.content})

        # Execute each tool_use block in this turn
        tool_results = []
        for block in response.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            tool_name = block.name
            tool_input = block.input or {}
            log.info(
                "tool_use iter=%d tool=%s slack_user=%s entity=%s input=%s",
                iteration, tool_name, slack_user_id or "(none)", entity, tool_input,
            )
            result_str = dispatch(tool_name, tool_input, slack_user_id, entity)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_str,
            })

        messages.append({"role": "user", "content": tool_results})

    # Should not reach here given the iteration check above, but defensive fallback
    raise ClaudeClientError("Tool-use loop exited unexpectedly")
