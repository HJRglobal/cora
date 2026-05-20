"""Phase 3 Knowledge Base — vector DB + RAG over portfolio data.

Doctrine:
- sqlite-vec local vector store (no external infra)
- OpenAI text-embedding-3-small for embeddings ($0.02/1M tokens, 1536 dims)
- Sentence-aware chunking, 500 tokens / 50 overlap
- Per-source connectors (Fireflies, Asana, HubSpot, Gmail, Drive, Notion, Slack, static_md)
- Entity-scoped retrieval at query time
- 180-day default history depth
- PHI guardrail: Lex client content excluded entirely from KB

See _shared/projects/cora/design/phase-3-knowledge-base.md for the full architecture.
"""

from .store import (
    KnowledgeBase,
    KnowledgeBaseError,
    SearchResult,
)

__all__ = ["KnowledgeBase", "KnowledgeBaseError", "SearchResult"]
