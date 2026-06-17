"""Tests for purge_denied_kb (Phase 1.4) -- the source+tag sensitive-content purge.

Logic is tested against in-memory plain tables with an injected deny-policy; the
SQL is generic over chunk_id, so no sqlite-vec extension is needed.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import purge_denied_kb as purge  # noqa: E402
from cora import slack_sweep_policy  # noqa: E402


def _conn():
    c = sqlite3.connect(":memory:")
    c.execute(
        "CREATE TABLE knowledge_chunks "
        "(chunk_id TEXT PRIMARY KEY, source TEXT, source_id TEXT, sub_entity TEXT)"
    )
    c.execute("CREATE TABLE knowledge_vec_bin (chunk_id TEXT)")
    c.execute("CREATE TABLE knowledge_vec_f32 (chunk_id TEXT)")
    return c


def _seed(c):
    c.executemany(
        "INSERT INTO knowledge_chunks VALUES (?,?,?,?)",
        [
            ("k1", "gmail", "gmail:a@x:1", "LEX-LBHS"),    # tag -> purge
            ("k2", "drive_sweep", "fileid123", "LEX-LTS"),  # tag -> purge
            ("k3", "drive_sweep", "fileid456", "LEX-LBH"),  # typo tag -> purge
            ("k4", "slack", "slack:C0BAD1:1.2:0", "LEX"),   # denied slack chan -> purge
            ("k5", "slack", "slack:C0GOOD:1.2:0", "LEX"),   # allowed slack -> KEEP
            ("k6", "gmail", "gmail:b@x:1", None),           # keyword-less NULL LEX -> KEEP
            ("k7", "gmail", "gmail:c@x:1", "LEX-LLC"),      # LLC tag -> KEEP
        ],
    )


def test_targets_tags_and_denied_slack(monkeypatch):
    monkeypatch.setattr(slack_sweep_policy, "_cache", {"deny_by_id": ["C0BAD1"]})
    c = _conn(); _seed(c)
    ids, breakdown = purge.target_chunk_ids(c)
    assert ids == {"k1", "k2", "k3", "k4"}
    assert breakdown["tag LEX-LBHS/LTS/LBH"] == 3
    assert breakdown["slack denied-channel total"] == 1


def test_residual_and_other_subentities_kept(monkeypatch):
    monkeypatch.setattr(slack_sweep_policy, "_cache", {"deny_by_id": ["C0BAD1"]})
    c = _conn(); _seed(c)
    ids, _ = purge.target_chunk_ids(c)
    assert "k5" not in ids   # allowed slack channel
    assert "k6" not in ids   # keyword-less NULL LEX residual stays (source+tag)
    assert "k7" not in ids   # LEX-LLC stays (only LBHS/LTS removed)


def test_delete_removes_from_all_three_tables(monkeypatch):
    c = _conn()
    c.execute("INSERT INTO knowledge_chunks VALUES ('z','gmail','g','LEX-LBHS')")
    c.executemany("INSERT INTO knowledge_vec_bin VALUES (?)", [("z",), ("keep",)])
    c.execute("INSERT INTO knowledge_vec_f32 VALUES ('z')")
    totals = purge.delete_chunks(c, ["z"])
    assert totals["knowledge_chunks"] == 1
    assert totals["knowledge_vec_bin"] == 1
    assert totals["knowledge_vec_f32"] == 1
    assert [r[0] for r in c.execute("SELECT chunk_id FROM knowledge_vec_bin")] == ["keep"]


def test_delete_batches_large_lists():
    c = _conn()
    ids = [f"c{i}" for i in range(1200)]
    c.executemany("INSERT INTO knowledge_chunks VALUES (?,?,?,?)",
                  [(i, "gmail", "g", "LEX-LBHS") for i in ids])
    c.executemany("INSERT INTO knowledge_vec_bin VALUES (?)", [(i,) for i in ids])
    c.executemany("INSERT INTO knowledge_vec_f32 VALUES (?)", [(i,) for i in ids])
    totals = purge.delete_chunks(c, ids)
    assert totals["knowledge_chunks"] == 1200
    assert totals["knowledge_vec_bin"] == 1200
