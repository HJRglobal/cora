"""Sentence-aware text chunker for KB ingestion.

Splits long text into ~500-token chunks with ~50-token overlap, preserving sentence
boundaries when possible. Uses tiktoken's cl100k_base encoding to count tokens
(same encoding used by text-embedding-3-small).

Why this approach (vs LangChain RecursiveCharacterTextSplitter):
- Sentence-aware preserves semantic units better for retrieval
- Hard token cap prevents OpenAI API errors (8K-token max input per chunk)
- Overlap improves recall on queries that land near chunk boundaries
- No new heavy dependency (just tiktoken, which we need for accurate token counts)
"""

import logging
import re

import tiktoken

log = logging.getLogger(__name__)

DEFAULT_CHUNK_TOKENS = 500
DEFAULT_OVERLAP_TOKENS = 50
HARD_MAX_TOKENS = 8000  # OpenAI embedding endpoint limit per input

_encoder = tiktoken.get_encoding("cl100k_base")

# Split on sentence terminators (.?!), keeping the terminator with the preceding sentence.
# Handles common abbreviations imperfectly but acceptably — RAG is forgiving.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\(\[])")


def count_tokens(text: str) -> int:
    """Return the cl100k_base token count of text."""
    return len(_encoder.encode(text, disallowed_special=()))


def _sentences(text: str) -> list[str]:
    """Split text into sentences. Falls back to line-based splitting for sentence-free input."""
    text = text.strip()
    if not text:
        return []

    # First try sentence-style splitting
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]

    # If we got only 1 sentence and the original had newlines, fall back to line-based
    if len(sentences) <= 1 and "\n" in text:
        return [line.strip() for line in text.split("\n") if line.strip()]

    return sentences


def chunk_text(
    text: str,
    chunk_tokens: int = DEFAULT_CHUNK_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> list[str]:
    """Split text into chunks of approximately `chunk_tokens`, with `overlap_tokens` overlap.

    Returns a list of chunk strings. Each chunk is bounded by HARD_MAX_TOKENS regardless
    of chunk_tokens setting — over-long inputs are hard-truncated to prevent API errors.
    """
    if not text or not text.strip():
        return []

    sentences = _sentences(text)
    if not sentences:
        return []

    chunks: list[str] = []
    current: list[str] = []  # sentences accumulated for the current chunk
    current_tokens = 0

    for sentence in sentences:
        s_tokens = count_tokens(sentence)

        # A single sentence is too long — hard-truncate it at HARD_MAX_TOKENS as its own chunk
        if s_tokens > HARD_MAX_TOKENS:
            if current:
                chunks.append(" ".join(current))
                current = []
                current_tokens = 0
            # Hard truncate
            tokens = _encoder.encode(sentence, disallowed_special=())[:HARD_MAX_TOKENS]
            chunks.append(_encoder.decode(tokens))
            continue

        # If adding this sentence would exceed chunk_tokens, finalize current chunk first
        if current_tokens + s_tokens > chunk_tokens and current:
            chunks.append(" ".join(current))

            # Start a new chunk with overlap from the end of the previous one
            if overlap_tokens > 0:
                overlap_sentences: list[str] = []
                overlap_count = 0
                for s in reversed(current):
                    t = count_tokens(s)
                    if overlap_count + t > overlap_tokens:
                        break
                    overlap_sentences.insert(0, s)
                    overlap_count += t
                current = overlap_sentences
                current_tokens = overlap_count
            else:
                current = []
                current_tokens = 0

        current.append(sentence)
        current_tokens += s_tokens

    # Finalize the last chunk
    if current:
        chunks.append(" ".join(current))

    return chunks
