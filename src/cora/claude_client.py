"""Anthropic Claude API client with retry logic."""

import logging
import time

import anthropic

from .config import config

log = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 1024
_TIMEOUT = 25.0
_RETRY_DELAYS = (1, 2)  # seconds before attempt 1 and attempt 2

_client = anthropic.Anthropic(api_key=config.anthropic_api_key)


class ClaudeClientError(Exception):
    """Raised when the Claude API fails after all retries."""


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (anthropic.APIConnectionError, anthropic.APITimeoutError)):
        return True
    if isinstance(exc, anthropic.RateLimitError):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code >= 500
    return False


def generate_response(system_prompt: str, context: str, user_message: str) -> str:
    """Call Claude and return the response text.

    Raises ClaudeClientError on hard failure after 2 retries.
    """
    system = system_prompt + "\n\n---\n\n# Context\n\n" + context

    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            response = _client.messages.create(
                model=_MODEL,
                max_tokens=_MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": user_message}],
                timeout=_TIMEOUT,
            )
            return response.content[0].text
        except Exception as exc:
            if _is_retryable(exc) and attempt < 2:
                delay = _RETRY_DELAYS[attempt]
                log.warning(
                    "Claude API transient error (attempt %d/3), retrying in %ds: %s",
                    attempt + 1,
                    delay,
                    exc,
                )
                time.sleep(delay)
                last_exc = exc
            else:
                raise ClaudeClientError(f"Claude API error: {exc}") from exc

    raise ClaudeClientError(f"Claude API failed after 3 attempts: {last_exc}") from last_exc
