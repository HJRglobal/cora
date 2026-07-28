"""End-to-end test of scripts/migrate_kb_partition_key.py (Slice 2-1).

Builds a small KB (mocked embeddings), then drives the migration CLI as a subprocess:
populate -> --swap (arm) -> verify search uses v2 -> --unarm (rollback) -> --drop-legacy.
Confirms counts, checkpoint arming, resumability/idempotency, and the drop-legacy guard.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from cora.knowledge_base import embeddings
from cora.knowledge_base.store import (
    Document, KnowledgeBase, _V2_BIN, _LEGACY_BIN, _PARTITION_READY_KEY,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "migrate_kb_partition_key.py"
_DIM = 1536


def _vec(text):
    v = [0.0] * _DIM
    v[hash(text) % _DIM] = 1.0
    return v


@pytest.fixture(autouse=True)
def patch_embeddings(monkeypatch):
    monkeypatch.setattr(embeddings, "embed_texts", lambda ts: [_vec(t) for t in ts])
    monkeypatch.setattr(embeddings, "embed_query", _vec)


def _run(dbpath, *args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(dbpath), "--force", *args],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )


def _build_kb(dbpath) -> int:
    kb = KnowledgeBase(dbpath)
    docs = [Document(source="test", source_id=f"{e}-{i}", entity=e,
                     content=f"{e} content {i} widgets", title="t")
            for e in ("F3E", "OSN", "LEX", "FNDR") for i in range(5)]
    n = kb.upsert_documents(docs)
    f32 = kb._conn.execute("SELECT COUNT(*) FROM knowledge_vec_f32").fetchone()[0]
    kb.close()
    return f32


def test_migration_populate_swap_dropslegacy(tmp_path):
    dbpath = tmp_path / "mig_kb.db"
    f32 = _build_kb(dbpath)
    assert f32 > 0

    # 1. populate v2
    r = _run(dbpath)
    assert r.returncode == 0, r.stderr
    assert "v2 fully populated" in r.stdout

    kb = KnowledgeBase(dbpath)
    assert kb._table_exists(_V2_BIN)
    assert kb._conn.execute(f"SELECT COUNT(*) FROM {_V2_BIN}").fetchone()[0] == f32
    assert kb.get_checkpoint(_PARTITION_READY_KEY) is None      # populate does NOT arm
    assert kb._bin_search_table() == _LEGACY_BIN                # still legacy pre-arm
    kb.close()

    # 2. populate again -> idempotent (skips all, still complete)
    r = _run(dbpath)
    assert r.returncode == 0 and "already in v2" in r.stdout

    # 3. --swap arms the checkpoint
    r = _run(dbpath, "--swap")
    assert r.returncode == 0 and "ARMED" in r.stdout
    kb = KnowledgeBase(dbpath)
    assert kb.get_checkpoint(_PARTITION_READY_KEY)["ready"] is True
    assert kb._bin_search_table() == _V2_BIN                    # fresh instance -> v2
    hits = kb.search("F3E content 1 widgets", entity="F3E", max_age_days=None)
    assert any(h.entity == "F3E" for h in hits)
    kb.close()

    # 4. --unarm rolls back
    r = _run(dbpath, "--unarm")
    assert r.returncode == 0 and "UNARMED" in r.stdout
    kb = KnowledgeBase(dbpath)
    assert kb.get_checkpoint(_PARTITION_READY_KEY) is None
    assert kb._bin_search_table() == _LEGACY_BIN
    kb.close()

    # 5. re-arm, then --drop-legacy removes the old table
    assert _run(dbpath, "--swap").returncode == 0
    r = _run(dbpath, "--drop-legacy")
    assert r.returncode == 0 and "DROPPED" in r.stdout
    kb = KnowledgeBase(dbpath)
    assert not kb._table_exists(_LEGACY_BIN)
    assert kb._table_exists(_V2_BIN)
    assert kb._bin_search_table() == _V2_BIN
    # search still works post-drop
    assert kb.search("OSN content 2 widgets", entity="OSN", max_age_days=None) is not None
    kb.close()


def test_drop_legacy_refuses_when_unarmed(tmp_path):
    dbpath = tmp_path / "mig_kb2.db"
    _build_kb(dbpath)
    _run(dbpath)                                                # populate only (not armed)
    r = _run(dbpath, "--drop-legacy")
    assert r.returncode == 3 and "not armed" in r.stderr
    kb = KnowledgeBase(dbpath)
    assert kb._table_exists(_LEGACY_BIN)                        # legacy preserved
    kb.close()


def test_swap_refuses_on_count_mismatch(tmp_path):
    dbpath = tmp_path / "mig_kb3.db"
    _build_kb(dbpath)
    # create an EMPTY v2 (count 0) then try to swap -> must refuse (0 != f32)
    kb = KnowledgeBase(dbpath)
    kb._conn.execute(
        f"CREATE VIRTUAL TABLE {_V2_BIN} USING vec0(chunk_id TEXT PRIMARY KEY, "
        f"entity TEXT PARTITION KEY, embedding bit[{_DIM}])")
    kb._conn.commit()
    kb.close()
    r = _run(dbpath, "--swap")
    assert r.returncode == 1 and "REFUSING to arm" in r.stderr
