#!/usr/bin/env python3
"""Daily incremental Slack sync — sweeps all channels Cora is in, ingests to KB.

Watermark-driven (per-channel), mirrors incremental_sync_asana.py pattern exactly.
For each channel Cora is a member of:
  1. Fetch messages since last watermark (default: last 2 days if no watermark).
  2. For each message with replies, fetch thread replies.
  3. Group into thread-chunks (parent + replies together <= 500 tokens each).
  4. Derive entity from channel name via channel-routing.yaml.
  5. Ingest to KB via KnowledgeBase.upsert_documents().
  6. Advance per-channel watermark on clean run.

Scheduled as: cowork-cora-kb-sync-slack  2:00am AZ daily

Exit codes:
    0 = success (sync ran cleanly)
    1 = fatal error (no documents ingested, watermarks unchanged)
    2 = partial — transient connector error or missing scope on some channels
"""

import argparse
import fnmatch
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cora.connectors.slack_connector import (  # noqa: E402
    SlackConnectorError,
    get_channel_history,
    get_thread_replies,
    list_joined_channels,
    serialize_message,
    _CHANNEL_FETCH_SLEEP,
)
from cora.knowledge_base import KnowledgeBase, KnowledgeBaseError  # noqa: E402
from cora.slack_sweep_policy import should_ingest  # noqa: E402
from cora.knowledge_base.store import Document  # noqa: E402

import yaml  # noqa: E402

CORA_REPO_ROOT = Path(__file__).resolve().parents[1]
KB_DB_PATH     = CORA_REPO_ROOT / "data" / "cora_kb.db"
LOG_DIR        = CORA_REPO_ROOT / "logs"
WATERMARKS_PATH = CORA_REPO_ROOT / "data" / "cache" / "slack-sync-watermarks.json"
ROUTING_PATH   = CORA_REPO_ROOT / "design" / "channel-routing.yaml"

# Chunk size in characters (~500 tokens at ~4 chars/token)
MAX_CHUNK_CHARS = 2000
# Max messages to include in a single thread chunk
MAX_MESSAGES_PER_CHUNK = 20

# cq-8d16969e85fb: Cora's own bot user id (fallback when auth.test is
# unreachable). Used to tag bot-authored chunks at ingest so miners can
# exclude them and retrieval can label them — the self-poisoning class
# (the 7/20 pricing fabrication entered the KB as an untagged swept reply).
CORA_FALLBACK_USER_ID = "U0B44MDGC5R"

# Pure system-event noise — mirrors channel_sweep.py's fetch-time skip.
# Dropping these is not knowledge loss (tag-don't-drop applies to real replies).
_SKIP_SUBTYPES = {"channel_join", "channel_leave", "channel_topic"}


def _resolve_cora_user_id() -> str:
    """Cora's bot user id via auth.test, fail-soft to the known constant.

    Per the missed_message_catchup doctrine: a bare bot_id check counts EVERY
    app (Make.com, Tag); Cora-specific detection needs the auth.test user id.
    """
    try:
        from cora.connectors.slack_connector import _build_client
        resp = _build_client().auth_test()
        uid = (resp or {}).get("user_id", "")
        if uid:
            return uid
    except Exception as exc:  # noqa: BLE001 — never let tagging break the sweep
        logging.getLogger("kb-sync-slack").warning(
            "auth.test failed (%s) — falling back to %s", exc, CORA_FALLBACK_USER_ID)
    return CORA_FALLBACK_USER_ID


def _is_bot_msg(msg: dict[str, Any], cora_uid: str) -> bool:
    """True when the message was posted by ANY app (Cora, Make.com, Tag, ...)."""
    return bool(msg.get("bot_id")) or (bool(cora_uid) and msg.get("user") == cora_uid)


def _is_cora_msg(msg: dict[str, Any], cora_uid: str) -> bool:
    return bool(cora_uid) and msg.get("user") == cora_uid


def _bot_flags(msgs: list[dict[str, Any]], cora_uid: str) -> dict[str, bool]:
    """Per-chunk authorship flags (cq-8d16969e85fb, tag-don't-drop).

    bot_authored  — EVERY message in the chunk is app-posted (pure automation;
                    miners exclude these).
    has_cora_reply — at least one message is Cora's own reply (retrieval labels
                    these "not canon"; mixed chunks keep the human ask minable).
    """
    flags: dict[str, bool] = {}
    if msgs and all(_is_bot_msg(m, cora_uid) for m in msgs):
        flags["bot_authored"] = True
    if any(_is_cora_msg(m, cora_uid) for m in msgs):
        flags["has_cora_reply"] = True
    return flags


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = LOG_DIR / f"kb-sync-slack-{today}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


# ── Watermark helpers ─────────────────────────────────────────────────────────


def _load_watermarks() -> dict[str, float]:
    if not WATERMARKS_PATH.exists():
        return {}
    try:
        with WATERMARKS_PATH.open(encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save_watermarks(wm: dict[str, float]) -> None:
    WATERMARKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = WATERMARKS_PATH.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(wm, fh, indent=2)
    tmp.replace(WATERMARKS_PATH)


# ── Entity routing ────────────────────────────────────────────────────────────


def _load_routing() -> list[dict[str, str]]:
    if not ROUTING_PATH.exists():
        return []
    with ROUTING_PATH.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data.get("routes", [])


def _resolve_entity(channel_name: str, routes: list[dict[str, str]]) -> str:
    """Map a channel name to an entity code using channel-routing.yaml first-match logic."""
    name = channel_name.lstrip("#").lower()
    for route in routes:
        pattern = route.get("pattern", "")
        if fnmatch.fnmatch(name, pattern):
            return route.get("entity", "FNDR")
    return "FNDR"


def _resolve_sub_entity(entity: str, channel_name: str) -> str | None:
    """Derive sub_entity tag for LEX sub-entities."""
    lex_map = {
        "LEX-LLC": "LEX-LLC",
        "LEX-LTS": "LEX-LTS",
        "LEX-LBHS": "LEX-LBHS",
        "LEX-LLA": "LEX-LLA",
    }
    return lex_map.get(entity)


# ── Chunking ──────────────────────────────────────────────────────────────────


def _chunk_thread(
    parent_msg: dict[str, Any],
    replies: list[dict[str, Any]],
    channel_name: str,
) -> list[tuple[str, list[dict[str, Any]]]]:
    """Combine parent + replies into chunks of at most MAX_CHUNK_CHARS characters.

    Returns list of (chunk_text, chunk_msgs) pairs — the message dicts that went
    into each chunk ride along so the caller can compute per-chunk authorship
    flags (cq-8d16969e85fb).
    """
    all_msgs = [parent_msg] + replies

    chunks: list[tuple[str, list[dict[str, Any]]]] = []
    current: list[str] = []
    current_msgs: list[dict[str, Any]] = []
    current_len = 0

    for msg in all_msgs:
        line = serialize_message(msg)
        if current_len + len(line) > MAX_CHUNK_CHARS and current:
            chunks.append((f"#{channel_name}\n" + "\n".join(current), current_msgs))
            current = []
            current_msgs = []
            current_len = 0
        current.append(line)
        current_msgs.append(msg)
        current_len += len(line)

    if current:
        chunks.append((f"#{channel_name}\n" + "\n".join(current), current_msgs))

    return chunks


def _ts_to_int(ts: str) -> int:
    """Convert a Slack message ts string ('1717000000.123456') to Unix int."""
    return int(float(ts))


# ── Main sync ─────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fallback-days", type=int, default=2,
        help="Days to look back if no watermark exists for a channel (default 2)",
    )
    parser.add_argument("--batch-size", type=int, default=50)
    args = parser.parse_args()

    _setup_logging()
    log = logging.getLogger("kb-sync-slack")
    log.info("=" * 60)
    log.info("Slack incremental sync starting")

    routes = _load_routing()
    watermarks = _load_watermarks()
    cora_uid = _resolve_cora_user_id()
    log.info("Bot-authorship tagging active (cora_uid=%s)", cora_uid)
    kb = KnowledgeBase(KB_DB_PATH)

    sync_start = int(time.time())
    fallback_ts = sync_start - (args.fallback_days * 86400)

    total_channels = 0
    total_docs = 0
    total_chunks = 0
    skipped_channels = 0
    exit_code = 0
    new_watermarks: dict[str, float] = {}

    try:
        channels = list_joined_channels()
        log.info("Found %d joined channels to sync", len(channels))
    except SlackConnectorError as exc:
        log.error("list_joined_channels failed: %s", exc)
        kb.close()
        return 1

    for ch in channels:
        ch_id = ch["id"]
        ch_name = ch["name"]
        if not should_ingest(ch_name, ch_id, bool(ch.get("is_private"))):
            log.info("Skipping #%s -- ingestion deny-list (Phase 1.4)", ch_name)
            continue
        entity = _resolve_entity(ch_name, routes)
        sub_entity = _resolve_sub_entity(entity, ch_name)

        watermark_ts = watermarks.get(ch_id, fallback_ts)
        log.info(
            "Syncing #%s (entity=%s, oldest=%.0f)", ch_name, entity, watermark_ts
        )

        try:
            messages = get_channel_history(ch_id, oldest_ts=watermark_ts)
        except SlackConnectorError as exc:
            log.warning("Skipping #%s: %s", ch_name, exc)
            skipped_channels += 1
            exit_code = 2
            time.sleep(_CHANNEL_FETCH_SLEEP)
            continue

        if not messages:
            log.debug("No new messages in #%s", ch_name)
            new_watermarks[ch_id] = float(sync_start)
            time.sleep(_CHANNEL_FETCH_SLEEP)
            total_channels += 1
            continue

        # Group messages by thread_ts (parent ts); plain messages get their own ts.
        # join/leave/topic system events are pure noise — skipped outright
        # (cq-8d16969e85fb; mirrors channel_sweep.py's fetch-time skip).
        threads: dict[str, dict[str, Any]] = {}
        for msg in messages:
            if msg.get("subtype") in _SKIP_SUBTYPES:
                continue
            thread_ts = msg.get("thread_ts") or msg.get("ts")
            if thread_ts not in threads:
                threads[thread_ts] = {"parent": None, "replies": []}
            if msg.get("ts") == thread_ts:
                threads[thread_ts]["parent"] = msg
            else:
                threads[thread_ts]["replies"].append(msg)

        docs_batch: list[Document] = []

        for thread_ts, thread_data in threads.items():
            parent = thread_data.get("parent")
            if parent is None:
                continue  # orphan reply — skip

            # Fetch replies from Slack if thread has more replies than we have
            reply_count = parent.get("reply_count", 0)
            local_replies = thread_data["replies"]
            if reply_count > len(local_replies):
                try:
                    all_replies = get_thread_replies(ch_id, thread_ts)
                    local_replies = all_replies
                except Exception as exc:
                    log.warning(
                        "get_thread_replies(%s, %s) failed: %s", ch_id, thread_ts, exc
                    )
            local_replies = [
                m for m in local_replies if m.get("subtype") not in _SKIP_SUBTYPES
            ]

            chunks = _chunk_thread(parent, local_replies, ch_name)
            parent_ts = _ts_to_int(thread_ts)
            source_id = f"slack:{ch_id}:{thread_ts}"
            deep_link = f"https://hjrglobal.slack.com/archives/{ch_id}/p{thread_ts.replace('.', '')}"

            # ALL of a thread's chunks share the BARE source_id: the store's
            # replace-on-conflict dedups keys batch-wide, so a re-sweep
            # replaces the whole thread family atomically even when the chunk
            # COUNT changed (threads GROW as replies land; the old
            # ':chunkN'-when-multi scheme orphaned the bare row the moment a
            # 1-chunk thread became 2 chunks -- D-051 review, same class as
            # the gmail quote-strip shrink).
            for chunk_text, chunk_msgs in chunks:
                metadata = {"channel_id": ch_id, "channel_name": ch_name,
                            "thread_ts": thread_ts}
                metadata.update(_bot_flags(chunk_msgs, cora_uid))
                doc = Document(
                    source="slack",
                    source_id=source_id,
                    entity=entity,
                    content=chunk_text,
                    date_created=parent_ts,
                    date_modified=parent_ts,
                    title=f"#{ch_name} thread {datetime.fromtimestamp(parent_ts, tz=timezone.utc).strftime('%Y-%m-%d')}",
                    deep_link=deep_link,
                    sub_entity=sub_entity,
                    metadata=metadata,
                )
                docs_batch.append(doc)

            if len(docs_batch) >= args.batch_size:
                chunk_count = kb.upsert_documents(docs_batch)
                total_chunks += chunk_count
                total_docs += len(docs_batch)
                log.info(
                    "Batch upserted: %d docs / %d chunks (channel=#%s)",
                    len(docs_batch), chunk_count, ch_name,
                )
                docs_batch = []

        if docs_batch:
            chunk_count = kb.upsert_documents(docs_batch)
            total_chunks += chunk_count
            total_docs += len(docs_batch)
            log.info(
                "Final batch for #%s: %d docs / %d chunks",
                ch_name, len(docs_batch), chunk_count,
            )

        new_watermarks[ch_id] = float(sync_start)
        total_channels += 1
        time.sleep(_CHANNEL_FETCH_SLEEP)

    # Persist watermarks for all successfully synced channels
    merged = {**watermarks, **new_watermarks}
    _save_watermarks(merged)
    log.info(
        "Slack sync complete — %d channels synced, %d skipped, %d docs -> %d chunks (exit=%d)",
        total_channels, skipped_channels, total_docs, total_chunks, exit_code,
    )

    kb.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
