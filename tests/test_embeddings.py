"""Unit tests for knowledge_base.embeddings — retry, batching, error handling.

All tests mock the OpenAI client — no network calls are made.
"""

import pytest
from unittest.mock import MagicMock, patch, PropertyMock

from cora.knowledge_base import embeddings
from cora.knowledge_base.embeddings import (
    BATCH_SIZE,
    EMBEDDING_DIM,
    EmbeddingError,
    embed_query,
    embed_texts,
)


def _make_embedding_response(texts: list[str]) -> MagicMock:
    """Build a mock response shaped like openai.types.CreateEmbeddingResponse."""
    response = MagicMock()
    response.data = [
        MagicMock(embedding=[0.1] * EMBEDDING_DIM)
        for _ in texts
    ]
    return response


@pytest.fixture(autouse=True)
def reset_client():
    """Reset the module-level singleton client between tests."""
    original = embeddings._client
    embeddings._client = None
    yield
    embeddings._client = original


@pytest.fixture
def mock_openai(monkeypatch):
    """Patch OpenAI constructor and set OPENAI_API_KEY."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    client_instance = MagicMock()
    client_instance.embeddings.create.side_effect = lambda **kw: _make_embedding_response(kw["input"])
    with patch("cora.knowledge_base.embeddings.OpenAI", return_value=client_instance) as mock_cls:
        yield client_instance


# ── embed_texts ───────────────────────────────────────────────────────────────

class TestEmbedTexts:
    def test_empty_list_returns_empty(self):
        assert embed_texts([]) == []

    def test_single_text_returns_one_vector(self, mock_openai):
        result = embed_texts(["hello"])
        assert len(result) == 1
        assert len(result[0]) == EMBEDDING_DIM

    def test_multiple_texts_returns_one_vector_each(self, mock_openai):
        result = embed_texts(["a", "b", "c"])
        assert len(result) == 3
        assert all(len(v) == EMBEDDING_DIM for v in result)

    def test_batches_large_input(self, mock_openai):
        texts = [f"text {i}" for i in range(BATCH_SIZE + 5)]
        result = embed_texts(texts)
        assert len(result) == len(texts)
        # Should have been called twice (one full batch + one partial)
        assert mock_openai.embeddings.create.call_count == 2

    def test_single_batch_for_small_input(self, mock_openai):
        texts = ["a", "b", "c"]
        embed_texts(texts)
        assert mock_openai.embeddings.create.call_count == 1

    def test_no_api_key_raises_embedding_error(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(EmbeddingError, match="OPENAI_API_KEY"):
            embed_texts(["text"])

    def test_model_used_is_embedding_model(self, mock_openai):
        embed_texts(["test"])
        call_kwargs = mock_openai.embeddings.create.call_args[1]
        assert call_kwargs["model"] == embeddings.EMBEDDING_MODEL

    def test_encoding_format_is_float(self, mock_openai):
        embed_texts(["test"])
        call_kwargs = mock_openai.embeddings.create.call_args[1]
        assert call_kwargs["encoding_format"] == "float"


# ── retry behavior ────────────────────────────────────────────────────────────

class TestRetry:
    def test_retries_on_rate_limit(self, monkeypatch, mock_openai):
        from openai import RateLimitError

        call_count = {"n": 0}

        def raise_then_succeed(**kw):
            call_count["n"] += 1
            if call_count["n"] < 2:
                raise RateLimitError("rate limited", response=MagicMock(), body={})
            return _make_embedding_response(kw["input"])

        mock_openai.embeddings.create.side_effect = raise_then_succeed
        monkeypatch.setattr(embeddings, "_RETRY_DELAYS", (0, 0, 0))

        result = embed_texts(["test"])
        assert len(result) == 1
        assert call_count["n"] == 2

    def test_retries_on_timeout(self, monkeypatch, mock_openai):
        from openai import APITimeoutError

        call_count = {"n": 0}

        def raise_then_succeed(**kw):
            call_count["n"] += 1
            if call_count["n"] < 2:
                raise APITimeoutError(request=MagicMock())
            return _make_embedding_response(kw["input"])

        mock_openai.embeddings.create.side_effect = raise_then_succeed
        monkeypatch.setattr(embeddings, "_RETRY_DELAYS", (0, 0, 0))

        result = embed_texts(["test"])
        assert len(result) == 1
        assert call_count["n"] == 2

    def test_retries_on_connection_error(self, monkeypatch, mock_openai):
        from openai import APIConnectionError

        call_count = {"n": 0}

        def raise_then_succeed(**kw):
            call_count["n"] += 1
            if call_count["n"] < 2:
                raise APIConnectionError(request=MagicMock())
            return _make_embedding_response(kw["input"])

        mock_openai.embeddings.create.side_effect = raise_then_succeed
        monkeypatch.setattr(embeddings, "_RETRY_DELAYS", (0, 0, 0))

        result = embed_texts(["test"])
        assert len(result) == 1

    def test_raises_embedding_error_after_all_retries(self, monkeypatch, mock_openai):
        from openai import RateLimitError

        mock_openai.embeddings.create.side_effect = RateLimitError(
            "rate limited", response=MagicMock(), body={}
        )
        monkeypatch.setattr(embeddings, "_RETRY_DELAYS", (0, 0))

        with pytest.raises(EmbeddingError, match="attempts"):
            embed_texts(["test"])

    def test_non_transient_error_raises_immediately(self, monkeypatch, mock_openai):
        mock_openai.embeddings.create.side_effect = ValueError("unexpected format")
        monkeypatch.setattr(embeddings, "_RETRY_DELAYS", (0, 0, 0))

        with pytest.raises(EmbeddingError):
            embed_texts(["test"])
        assert mock_openai.embeddings.create.call_count == 1


# ── embed_query ───────────────────────────────────────────────────────────────

class TestEmbedQuery:
    def test_returns_single_vector(self, mock_openai):
        result = embed_query("what is the P&L?")
        assert len(result) == EMBEDDING_DIM

    def test_calls_embed_texts_with_list(self, mock_openai):
        with patch("cora.knowledge_base.embeddings.embed_texts", return_value=[[0.5] * EMBEDDING_DIM]) as mock_et:
            embed_query("test query")
        mock_et.assert_called_once_with(["test query"])

    def test_empty_result_raises_embedding_error(self, mock_openai):
        with patch("cora.knowledge_base.embeddings.embed_texts", return_value=[]):
            with pytest.raises(EmbeddingError, match="empty"):
                embed_query("test")
