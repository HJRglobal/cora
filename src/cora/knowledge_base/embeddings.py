"""OpenAI embeddings wrapper with batching + retry.

Uses text-embedding-3-small (1536 dims, $0.02/1M tokens). Batches up to 100 texts per
API call (well under the 2048 limit but conservative on payload size). Retries on
transient errors with exponential backoff.
"""

import logging
import os
import time

from openai import APIConnectionError, APITimeoutError, OpenAI, RateLimitError

log = logging.getLogger(__name__)

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIM = 1536
BATCH_SIZE = 100
_TIMEOUT = 30.0
_RETRY_DELAYS = (1, 2, 5)


class EmbeddingError(Exception):
    """Raised when embedding generation fails after retries."""


_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            raise EmbeddingError(
                "OPENAI_API_KEY not set in environment — KB embeddings disabled"
            )
        _client = OpenAI(api_key=api_key, timeout=_TIMEOUT)
    return _client


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a list of texts. Returns one 1536-float vector per input, in order.

    Batches into BATCH_SIZE-sized API calls. Retries each batch up to 3 times on
    transient errors (RateLimitError, APITimeoutError, APIConnectionError).
    """
    if not texts:
        return []

    client = _get_client()
    out: list[list[float]] = []

    for batch_start in range(0, len(texts), BATCH_SIZE):
        batch = texts[batch_start : batch_start + BATCH_SIZE]
        embeddings = _embed_batch_with_retry(client, batch)
        out.extend(embeddings)
        log.debug("Embedded batch %d-%d", batch_start, batch_start + len(batch))

    return out


def _embed_batch_with_retry(client: OpenAI, batch: list[str]) -> list[list[float]]:
    """Embed one batch with retry on transient errors."""
    last_exc: Exception | None = None
    for attempt in range(len(_RETRY_DELAYS) + 1):
        try:
            response = client.embeddings.create(
                model=EMBEDDING_MODEL,
                input=batch,
                encoding_format="float",
            )
            return [item.embedding for item in response.data]
        except (RateLimitError, APITimeoutError, APIConnectionError) as exc:
            if attempt >= len(_RETRY_DELAYS):
                raise EmbeddingError(
                    f"OpenAI embeddings failed after {attempt + 1} attempts: {exc}"
                ) from exc
            delay = _RETRY_DELAYS[attempt]
            log.warning(
                "OpenAI embeddings transient error (attempt %d), retrying in %ds: %s",
                attempt + 1, delay, exc,
            )
            time.sleep(delay)
            last_exc = exc
        except Exception as exc:
            raise EmbeddingError(f"OpenAI embeddings error: {exc}") from exc

    raise EmbeddingError(f"OpenAI embeddings exhausted retries: {last_exc}")


def embed_query(query: str) -> list[float]:
    """Embed a single query string. Convenience wrapper around embed_texts."""
    results = embed_texts([query])
    if not results:
        raise EmbeddingError(f"Embedding returned empty result for query: {query[:50]}")
    return results[0]
