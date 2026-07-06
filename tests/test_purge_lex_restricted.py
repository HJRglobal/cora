"""W6-01 purge: scripts/purge_lex_restricted_kb.py scope + reversibility + idempotency.

Seeds rows DIRECTLY via SQL (the ingest drop now prevents seeding gmail/drive LBHS/LTS
through upsert_documents), then exercises the purge helpers against a real schema DB.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from cora.knowledge_base import schema  # noqa: E402
from cora.knowledge_base.store import KnowledgeBase  # noqa: E402
import purge_lex_restricted_kb as purge  # noqa: E402

# (chunk_id, source, source_id, entity, sub_entity)
_SEED = [
    ("c-g-lbhs", "gmail",       "g1", "LEX", "LEX-LBHS"),   # default scope
    ("c-d-lts",  "drive_sweep", "d1", "LEX", "LEX-LTS"),    # default scope
    ("c-g-llc",  "gmail",       "g2", "LEX", "LEX-LLC"),    # NOT restricted
    ("c-g-gen",  "gmail",       "g3", "LEX", None),         # GM-level
    ("c-s-lbhs", "slack",       "s1", "LEX", "LEX-LBHS"),   # non-default source
    ("c-m-lbhs", "static_md",   "m1", "LEX", "LEX-LBHS"),   # non-default source
    ("c-f3e",    "gmail",       "f1", "F3E", None),         # non-LEX
]


@pytest.fixture
def seeded_db(tmp_path):
    db = tmp_path / "kb.db"
    KnowledgeBase(db).close()  # create schema
    conn = schema.connect(db)
    for cid, src, sid, ent, sub in _SEED:
        conn.execute(
            "INSERT INTO knowledge_chunks "
            "(chunk_id, source, source_id, entity, sub_entity, title, content, ingested_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (cid, src, sid, ent, sub, f"{cid} title", f"{cid} content", 0),
        )
    conn.commit()
    yield db, conn
    conn.close()


def test_default_scope_is_gmail_drive_only(seeded_db):
    _, conn = seeded_db
    ids = purge.target_chunk_ids(conn, purge.RESTRICTED_INGEST_SOURCES)
    assert ids == {"c-g-lbhs", "c-d-lts"}  # LLC/general/slack/static_md/F3E excluded


def test_all_sources_scope(seeded_db):
    _, conn = seeded_db
    ids = purge.target_chunk_ids(conn, None)
    assert ids == {"c-g-lbhs", "c-d-lts", "c-s-lbhs", "c-m-lbhs"}  # every LBHS/LTS row


def test_include_source_widens(seeded_db):
    _, conn = seeded_db
    ids = purge.target_chunk_ids(conn, purge.RESTRICTED_INGEST_SOURCES + ("slack",))
    assert ids == {"c-g-lbhs", "c-d-lts", "c-s-lbhs"}


def test_non_default_rows_surface_for_review(seeded_db):
    _, conn = seeded_db
    nd = {(sub, src) for sub, src, _title, _sid in purge.non_default_rows(conn)}
    assert nd == {("LEX-LBHS", "slack"), ("LEX-LBHS", "static_md")}


def test_backup_then_delete_then_idempotent(seeded_db, tmp_path):
    _, conn = seeded_db
    ids = purge.target_chunk_ids(conn, purge.RESTRICTED_INGEST_SOURCES)
    bak = tmp_path / "purge.bak.jsonl"
    n = purge.backup_rows(conn, ids, bak)
    assert n == 2 and bak.exists()
    # backup carries full row content (reversible-audit)
    lines = [json.loads(x) for x in bak.read_text(encoding="utf-8").splitlines()]
    assert {r["chunk_id"] for r in lines} == {"c-g-lbhs", "c-d-lts"}
    assert all("content" in r and "sub_entity" in r for r in lines)

    totals = purge.delete_chunks(conn, ids)
    assert totals["knowledge_chunks"] == 2

    # idempotent: a second default-scope target is now empty
    assert purge.target_chunk_ids(conn, purge.RESTRICTED_INGEST_SOURCES) == set()
    # non-restricted rows untouched
    remaining = {r[0] for r in conn.execute("SELECT chunk_id FROM knowledge_chunks").fetchall()}
    assert {"c-g-llc", "c-g-gen", "c-s-lbhs", "c-m-lbhs", "c-f3e"} <= remaining
    assert "c-g-lbhs" not in remaining and "c-d-lts" not in remaining


# ── D-051 finding 6: --apply refuses while the live bot's heartbeat is fresh ──
def test_apply_refused_when_bot_running(monkeypatch, tmp_path):
    hb = tmp_path / "heartbeat.txt"
    hb.write_text("alive")  # fresh mtime
    monkeypatch.setattr(purge, "HEARTBEAT_PATH", hb)
    monkeypatch.setattr(sys, "argv", ["purge_lex_restricted_kb.py", "--apply"])
    assert purge.main() == 3  # refused: service appears running


def test_apply_force_bypasses_heartbeat_guard(monkeypatch, tmp_path):
    hb = tmp_path / "heartbeat.txt"
    hb.write_text("alive")  # fresh
    monkeypatch.setattr(purge, "HEARTBEAT_PATH", hb)
    monkeypatch.setattr(purge, "KB_DB_PATH", tmp_path / "does-not-exist.db")
    monkeypatch.setattr(sys, "argv", ["purge_lex_restricted_kb.py", "--apply", "--force"])
    # --force skips the guard; the missing DB then returns 1 -> proves the guard was bypassed.
    assert purge.main() == 1
