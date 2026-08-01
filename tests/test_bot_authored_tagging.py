"""cq-8d16969e85fb — bot-reply tagging at slack KB ingest + downstream exclusion.

Covers the four pieces of the tag-don't-drop design:
  1. Ingest tagging: incremental_sync_slack._bot_flags / _chunk_thread pairs /
     subtype skip constants.
  2. Miner exclusion: gap_autofill (post-filter), friction_mining +
     reconciliation_engine (SQL predicate) never see bot_authored chunks.
  3. Retrieval labeling: context_loader._format_kb_chunks per-chunk label.
  4. Retro-tag backfill: scripts/retro_tag_bot_slack_chunks.classify_chunk.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

CORA = "U0B44MDGC5R"


def _load_sync():
    try:
        import incremental_sync_slack as m
        return m
    except ImportError:
        pytest.skip("incremental_sync_slack not importable")


def _load_retro():
    try:
        import retro_tag_bot_slack_chunks as m
        return m
    except ImportError:
        pytest.skip("retro_tag_bot_slack_chunks not importable")


# ── 1. Ingest tagging ─────────────────────────────────────────────────────────

class TestBotFlags:
    def test_human_only_no_flags(self):
        m = _load_sync()
        msgs = [{"user": "U111", "text": "hi"}, {"user": "U222", "text": "yo"}]
        assert m._bot_flags(msgs, CORA) == {}

    def test_all_cora_gets_both_flags(self):
        m = _load_sync()
        msgs = [{"user": CORA, "text": "here is your digest"}]
        assert m._bot_flags(msgs, CORA) == {"bot_authored": True,
                                            "has_cora_reply": True}

    def test_mixed_human_and_cora_only_has_cora_reply(self):
        """The load-bearing case: a human ask + Cora's reply share one chunk —
        the human question must stay minable, so bot_authored must NOT be set."""
        m = _load_sync()
        msgs = [{"user": "U111", "text": "what's the wholesale price?"},
                {"user": CORA, "text": "the price is ..."}]
        assert m._bot_flags(msgs, CORA) == {"has_cora_reply": True}

    def test_non_cora_bot_only_bot_authored(self):
        m = _load_sync()
        msgs = [{"bot_id": "B0MAKE", "text": "Make.com scenario alert"}]
        assert m._bot_flags(msgs, CORA) == {"bot_authored": True}

    def test_empty_messages_no_flags(self):
        m = _load_sync()
        assert m._bot_flags([], CORA) == {}

    def test_join_leave_topic_in_skip_set(self):
        m = _load_sync()
        assert {"channel_join", "channel_leave", "channel_topic"} <= m._SKIP_SUBTYPES


class TestChunkThreadPairs:
    def test_returns_text_and_msgs_pairs(self, monkeypatch):
        m = _load_sync()
        monkeypatch.setattr(m, "serialize_message",
                            lambda msg: f"<{msg.get('user')}>: {msg.get('text')}")
        parent = {"user": "U111", "text": "question?"}
        replies = [{"user": CORA, "text": "answer."}]
        chunks = m._chunk_thread(parent, replies, "general")
        assert len(chunks) == 1
        text, msgs = chunks[0]
        assert text.startswith("#general")
        assert "<U111>: question?" in text
        assert msgs == [parent] + replies

    def test_split_chunks_carry_their_own_msgs(self, monkeypatch):
        m = _load_sync()
        monkeypatch.setattr(m, "serialize_message",
                            lambda msg: f"<{msg.get('user')}>: " + "x" * 1500)
        parent = {"user": "U111", "text": "a"}
        replies = [{"user": CORA, "text": "b"}]
        chunks = m._chunk_thread(parent, replies, "general")
        assert len(chunks) == 2
        # Chunk 0 = the human parent only; chunk 1 = Cora's reply only.
        assert chunks[0][1] == [parent]
        assert chunks[1][1] == [replies[0]]
        # Per-chunk flags therefore differ (the precision the pairs buy us).
        assert m._bot_flags(chunks[0][1], CORA) == {}
        assert m._bot_flags(chunks[1][1], CORA) == {"bot_authored": True,
                                                    "has_cora_reply": True}


class TestResolveCoraUserId:
    def test_falls_back_to_constant_on_error(self, monkeypatch):
        m = _load_sync()
        import cora.connectors.slack_connector as sc
        monkeypatch.setattr(sc, "_build_client",
                            MagicMock(side_effect=RuntimeError("no token")))
        assert m._resolve_cora_user_id() == m.CORA_FALLBACK_USER_ID

    def test_uses_auth_test_user_id(self, monkeypatch):
        m = _load_sync()
        import cora.connectors.slack_connector as sc
        client = MagicMock()
        client.auth_test.return_value = {"user_id": "U_LIVE"}
        monkeypatch.setattr(sc, "_build_client", lambda: client)
        assert m._resolve_cora_user_id() == "U_LIVE"


# ── 2. Miner exclusion ────────────────────────────────────────────────────────

def _kb_row(chunk_id, content, metadata=None, entity="F3E", source="slack"):
    now = int(time.time())
    return (chunk_id, source, f"sid-{chunk_id}", entity, None, content,
            "", f"title-{chunk_id}", now, now, now,
            json.dumps(metadata) if metadata else None)


@pytest.fixture()
def mini_kb(tmp_path):
    """Minimal knowledge_chunks table covering both miners' SELECT columns."""
    db = tmp_path / "kb.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""
        CREATE TABLE knowledge_chunks (
            chunk_id TEXT PRIMARY KEY, source TEXT, source_id TEXT,
            entity TEXT, sub_entity TEXT, content TEXT, deep_link TEXT,
            title TEXT, ingested_at INTEGER, date_created INTEGER,
            date_modified INTEGER, metadata TEXT)
    """)
    rows = [
        _kb_row("c-human", "human asked about pricing follow-up"),
        _kb_row("c-bot", "[ts] <U0B44MDGC5R>: here is the digest",
                {"bot_authored": True, "has_cora_reply": True}),
        _kb_row("c-mixed", "human ask + cora reply",
                {"has_cora_reply": True}),
    ]
    conn.executemany(
        "INSERT INTO knowledge_chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return db


class TestFrictionMiningExclusion:
    def test_bot_authored_excluded_mixed_kept(self, mini_kb):
        from cora import friction_mining as fm
        chunks = fm.query_chunks(db_path=mini_kb)
        ids = {c["source_id"] for c in chunks}
        assert "sid-c-bot" not in ids
        assert "sid-c-human" in ids
        assert "sid-c-mixed" in ids


class TestReconciliationExclusion:
    def test_bot_authored_excluded_mixed_kept(self, mini_kb):
        from cora import reconciliation_engine as re_
        chunks = re_._query_kb_chunks(sources=["slack"], db_path=mini_kb)
        ids = {c["source_id"] for c in chunks}
        assert "sid-c-bot" not in ids
        assert "sid-c-human" in ids
        assert "sid-c-mixed" in ids


class TestGapAutofillExclusion:
    def _result(self, metadata=None):
        return SimpleNamespace(source="slack", distance=0.5,
                               content="benign business content",
                               metadata=metadata)

    def test_bot_authored_excluded_mixed_kept(self, monkeypatch):
        from cora import gap_autofill as ga
        kb = MagicMock()
        kb.search.return_value = [
            self._result({"bot_authored": True, "has_cora_reply": True}),
            self._result({"has_cora_reply": True}),
            self._result(None),
        ]
        out = ga.search_slack_evidence(kb, {"question": "q", "gap": "g",
                                            "entity": "F3E"})
        metas = [getattr(r, "metadata", None) for r in out]
        assert len(out) == 2
        assert all(not (m or {}).get("bot_authored") for m in metas)


# ── 3. Retrieval labeling ─────────────────────────────────────────────────────

class TestFormatKbChunksLabel:
    def _chunk(self, metadata=None):
        return SimpleNamespace(source="slack", source_id="sid", title="t",
                               entity="F3E", date_modified=None, deep_link="",
                               content="body", metadata=metadata)

    def test_cora_own_reply_label(self):
        from cora.context_loader import _format_kb_chunks
        out = _format_kb_chunks(
            [self._chunk({"bot_authored": True, "has_cora_reply": True})])
        assert "CORA'S OWN PRIOR REPLY" in out

    def test_mixed_chunk_label(self):
        from cora.context_loader import _format_kb_chunks
        out = _format_kb_chunks([self._chunk({"has_cora_reply": True})])
        assert "includes Cora's own prior reply" in out

    def test_non_cora_bot_label(self):
        from cora.context_loader import _format_kb_chunks
        out = _format_kb_chunks([self._chunk({"bot_authored": True})])
        assert "bot/automation-authored" in out

    def test_untagged_chunk_no_label(self):
        from cora.context_loader import _format_kb_chunks
        out = _format_kb_chunks([self._chunk(None)])
        assert "CORA'S OWN PRIOR REPLY" not in out
        assert "bot/automation-authored" not in out


# ── 4. Retro-tag backfill classification ─────────────────────────────────────

class TestRetroClassify:
    def test_bot_only_chunk(self):
        m = _load_retro()
        content = "#alerts\n[2026-05-28 05:08 UTC] <U0B44MDGC5R>: has joined"
        assert m.classify_chunk(content) == {"bot_authored": True,
                                             "has_cora_reply": True}

    def test_mixed_chunk(self):
        m = _load_retro()
        content = ("#f3e-sales\n[ts] <U111AAAAA>: what's the price?\n"
                   "[ts] <U0B44MDGC5R>: the price is X")
        assert m.classify_chunk(content) == {"has_cora_reply": True}

    def test_human_only_chunk(self):
        m = _load_retro()
        content = "#general\n[ts] <U111AAAAA>: hello\n[ts] <U222BBBBB>: hi"
        assert m.classify_chunk(content) == {}

    def test_non_cora_bot_chunk(self):
        m = _load_retro()
        content = "#alerts\n[ts] <B0MAKE123>: scenario completed"
        assert m.classify_chunk(content) == {"bot_authored": True}

    def test_no_speaker_headers_untaggable(self):
        m = _load_retro()
        assert m.classify_chunk("raw continuation text with no headers") == {}

    def test_in_text_mention_not_a_speaker(self):
        """<@U...> mentions inside message text must not classify the chunk."""
        m = _load_retro()
        content = "#general\n[ts] <U111AAAAA>: ping <@U0B44MDGC5R> please"
        assert m.classify_chunk(content) == {}

    def test_unknown_speaker_not_bot(self):
        m = _load_retro()
        content = "#general\n[ts] <unknown>: something"
        assert m.classify_chunk(content) == {}
