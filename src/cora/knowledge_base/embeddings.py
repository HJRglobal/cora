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
# OpenAI's embeddings endpoint rejects any single request whose inputs sum to
# more than 300,000 tokens. BATCH_SIZE alone (count-based) is not enough: 100
# dense chunks (each up to the chunker's 8,000-token hard max) can blow past
# 300K and the whole batch 400s, which previously dropped large spreadsheets
# (e.g. Rita Tracking.xlsx) from the KB entirely. We cap each batch by BOTH the
# count AND a token budget kept safely under the hard limit.
MAX_BATCH_TOKENS = 250_000
_TIMEOUT = 30.0
_RETRY_DELAYS = (1, 2, 5)


class EmbeddingError(Exception):
    """Raised when embedding generation fails after retries."""


_client: OpenAI | None = None
_encoder = None


def _count_tokens(text: str) -> int:
    """Best-effort token count for batch budgeting.

    Uses the same cl100k encoding the chunker uses. Falls back to a
    conservative chars/4 estimate if tiktoken is unavailable for any reason
    (budgeting only -- correctness does not depend on exactness).
    """
    global _encoder
    if _encoder is None:
        try:
            import tiktoken
            _encoder = tiktoken.get_encoding("cl100k_base")
        except Exception:  # pragma: no cover - tiktoken is a hard dep in practice
            _encoder = False
    if _encoder:
        try:
            return len(_encoder.encode(text, disallowed_special=()))
        except Exception:
            pass
    return (len(text) // 4) + 1


def _iter_token_batches(texts: list[str]):
    """Yield batches bounded by BOTH BATCH_SIZE (count) and MAX_BATCH_TOKENS.

    A single oversized input (> MAX_BATCH_TOKENS) is emitted as its own batch
    so it is never silently dropped; the chunker already caps individual chunks
    well under the per-input limit, so this is a safety net rather than a
    routine path.
    """
    batch: list[str] = []
    batch_tokens = 0
    for text in texts:
        t_tokens = _count_tokens(text)
        if batch and (
            len(batch) >= BATCH_SIZE
            or batch_tokens + t_tokens > MAX_BATCH_TOKENS
        ):
            yield batch
            batch = []
            batch_tokens = 0
        batch.append(text)
        batch_tokens += t_tokens
    if batch:
        yield batch


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

    for batch in _iter_token_batches(texts):
        embeddings = _embed_batch_with_retry(client, batch)
        out.extend(embeddings)
        log.debug("Embedded batch of %d texts", len(batch))

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
