"""KB connectors — each module emits Documents to the Knowledge Base.

Pattern: each connector exposes
    backfill(since: datetime) -> Iterator[Document]
    sync_delta() -> Iterator[Document]   # reads sync_state for the watermark

The shared ingestion pipeline (KnowledgeBase.upsert_documents) chunks + embeds + stores.

Connectors:
    asana_connector — tasks + comments + project descriptions, entity by project prefix
    (more coming in Phase 3D)
"""
