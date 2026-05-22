"""Anthropic Claude API client with retry logic + tool-use loop.

Two public entry points:
  - generate_response()           — blocking, returns full text after model completes
  - generate_response_streaming() — same contract + a per-delta update_callback so
                                    callers can progressively edit a Slack message
                                    or surface partial output elsewhere
"""

import logging
import time
from typing import Callable, Optional

import anthropic

from .config import config
from .tools.tool_dispatch import TOOL_DEFINITIONS, dispatch

log = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 1024  # lowered 2048→1024 on 2026-05-21 — Cora replies are almost
                    # always <500 tokens; tighter ceiling = faster streaming + lower
                    # tail latency. If a reply gets clipped at max_tokens, bump back up.
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


def _build_cached_system(system_prompt: str, context: str) -> list[dict]:
    """Build the system field as a 2-block array for prompt caching.

    Block 1: the entity system prompt + voice — deterministic per entity, cached.
    Block 2: the KB / runtime context — query-specific, NOT cached.

    Anthropic's prompt cache will hit on block 1 for any subsequent request
    in the same entity within the ~5-minute ephemeral cache window. The cache
    miss rate is dominated by block 2 (different KB chunks per question), but
    that's the smaller block; block 1 carries most of the prompt mass.

    Cache-control rules (Anthropic):
      - cache_control on a block caches that block AND everything before it.
      - Putting cache_control only on block 1 makes block 1 cacheable while
        block 2 stays per-request. That's what we want.
    """
    return [
        {
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": "\n\n---\n\n# Context\n\n" + context,
        },
    ]


def _build_cached_tools() -> list[dict]:
    """Return TOOL_DEFINITIONS with cache_control on the last tool.

    Caches the full tool-definitions block for the ephemeral cache window.
    Tool definitions are large (~3-5k tokens) and static across all requests,
    so this is a free win.

    Anthropic rule: cache_control on the last tool caches the entire tools
    array as a single cacheable unit.
    """
    if not TOOL_DEFINITIONS:
        return []
    tools = list(TOOL_DEFINITIONS)
    tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
    return tools


def _log_usage(response: anthropic.types.Message, iteration: int) -> None:
    """Log token usage including cache hit/miss accounting.

    Wrapped in try/except so a mock or malformed Usage object never breaks the
    request — logging is observability, not a correctness contract.
    """
    try:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        # Coerce to int defensively — in production these are always ints from
        # the Anthropic SDK Usage object, but tests use MagicMock where the
        # attributes auto-generate as Mock instances.
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        cache_create = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        log.info(
            "claude usage iter=%d input=%d cache_create=%d cache_read=%d output=%d",
            iteration, input_tokens, cache_create, cache_read, output_tokens,
        )
    except (TypeError, ValueError, AttributeError):
        # Mock usage objects, missing fields, etc. — skip silently.
        pass


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

    Uses Anthropic prompt caching: the system_prompt + tool definitions are cached
    ephemerally (~5min TTL). Cache hits across iterations within one request AND
    across requests within the same entity. Expect ~5-10x faster input processing
    on cache hits.

    Raises ClaudeClientError on hard failure after retries.
    """
    system_blocks = _build_cached_system(system_prompt, context)
    cached_tools = _build_cached_tools()

    # Conversation accumulator — starts with the user's message, grows with each
    # tool_use / tool_result exchange.
    messages: list[dict] = [{"role": "user", "content": user_message}]

    for iteration in range(_MAX_TOOL_ITERATIONS + 1):
        response = _create_with_retry(
            model=_MODEL,
            max_tokens=_MAX_TOKENS,
            system=system_blocks,
            messages=messages,
            tools=cached_tools,
            timeout=_TIMEOUT,
        )
        _log_usage(response, iteration)

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


# ───────────────────────────────────────────────────────────────────────────
# Streaming variant
# ───────────────────────────────────────────────────────────────────────────

# Type alias for the update callback. Receives the cumulative response text so far
# (NOT just the latest delta). Caller decides whether/how to push to a UI surface
# (e.g., Slack chat_update with rate-limiting).
UpdateCallback = Callable[[str], None]


def _stream_with_retry(**kwargs):
    """Open a streaming response context with retry on transient errors.

    Returns the stream context manager. Caller is responsible for iterating it
    and calling `get_final_message()`. Same retry semantics as _create_with_retry.
    """
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            return _client.messages.stream(**kwargs)
        except Exception as exc:
            if _is_retryable(exc) and attempt < 2:
                delay = _RETRY_DELAYS[attempt]
                log.warning(
                    "Claude streaming transient error (attempt %d/3), retrying in %ds: %s",
                    attempt + 1, delay, exc,
                )
                time.sleep(delay)
                last_exc = exc
            else:
                raise ClaudeClientError(f"Claude API error: {exc}") from exc
    raise ClaudeClientError(f"Claude streaming failed after 3 attempts: {last_exc}") from last_exc


def generate_response_streaming(
    system_prompt: str,
    context: str,
    user_message: str,
    update_callback: Optional[UpdateCallback] = None,
    slack_user_id: str = "",
    entity: str = "FNDR",
) -> str:
    """Streaming variant of generate_response.

    Calls Claude with `messages.stream()` and invokes `update_callback(text)` on
    every text-delta event with the CUMULATIVE response text so far (not just the
    new delta). Caller is responsible for:
      - Rate-limiting actual UI updates (Slack chat_update etc.) — this function
        calls the callback on EVERY text delta, which could be many per second.
      - Handling callback exceptions — they propagate out of this function. If
        the caller wants to swallow them, they should wrap update_callback.

    Returns the final accumulated text (same string the callback was last called
    with, unless the final iteration emitted text after the last callback fired).

    Tool-use loop: identical to generate_response. On stop_reason=tool_use,
    dispatch tools and loop. Text accumulates across iterations — if iter 0
    emits "Let me check that..." and iter 1 emits "Here's what I found...",
    the user sees the concatenation. Matches the model's intent.

    update_callback may be None (e.g., during tests) — in that case no
    progressive updates fire, but the function still returns the final text.

    Raises ClaudeClientError on hard failure after retries.
    """
    system_blocks = _build_cached_system(system_prompt, context)
    cached_tools = _build_cached_tools()

    messages: list[dict] = [{"role": "user", "content": user_message}]
    accumulated_text = ""

    def _maybe_push(text: str) -> None:
        if update_callback is not None:
            update_callback(text)

    for iteration in range(_MAX_TOOL_ITERATIONS + 1):
        try:
            with _stream_with_retry(
                model=_MODEL,
                max_tokens=_MAX_TOKENS,
                system=system_blocks,
                messages=messages,
                tools=cached_tools,
                timeout=_TIMEOUT,
            ) as stream:
                for event in stream:
                    # Two event shapes carry text:
                    #   content_block_delta with delta.type == text_delta
                    #   (some SDK versions also expose a `.text` attribute on event)
                    event_type = getattr(event, "type", None)
                    if event_type != "content_block_delta":
                        continue
                    delta = getattr(event, "delta", None)
                    if delta is None:
                        continue
                    if getattr(delta, "type", None) != "text_delta":
                        continue
                    chunk = getattr(delta, "text", "") or ""
                    if chunk:
                        accumulated_text += chunk
                        _maybe_push(accumulated_text)

                final = stream.get_final_message()
        except ClaudeClientError:
            raise
        except Exception as exc:  # noqa: BLE001 — defensive; SDK errors should be caught above
            raise ClaudeClientError(f"Streaming error: {exc}") from exc

        _log_usage(final, iteration)

        if final.stop_reason != "tool_use":
            # Model is done — return the cumulative text.
            # `accumulated_text` should already match the final message's text
            # content, but extract from final as a safety net if streaming dropped events.
            final_text = _extract_text(final)
            if final_text and final_text != accumulated_text:
                # Stream missed some text — push the corrected final
                accumulated_text = final_text
                _maybe_push(accumulated_text)
            return accumulated_text or "(Cora returned no text)"

        if iteration >= _MAX_TOOL_ITERATIONS:
            log.warning(
                "Tool-use iteration cap (%d) hit during streaming — returning partial response",
                _MAX_TOOL_ITERATIONS,
            )
            return accumulated_text or (
                "I tried to look that up but couldn't finish in time — try rephrasing."
            )

        # Capture assistant turn (must include tool_use blocks verbatim per API contract)
        messages.append({"role": "assistant", "content": final.content})

        # Execute each tool_use block in this turn (sequential, same as non-streaming)
        tool_results = []
        for block in final.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            tool_name = block.name
            tool_input = block.input or {}
            log.info(
                "tool_use (stream) iter=%d tool=%s slack_user=%s entity=%s input=%s",
                iteration, tool_name, slack_user_id or "(none)", entity, tool_input,
            )
            result_str = dispatch(tool_name, tool_input, slack_user_id, entity)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_str,
            })

        messages.append({"role": "user", "content": tool_results})

    raise ClaudeClientError("Tool-use loop exited unexpectedly during streaming")
