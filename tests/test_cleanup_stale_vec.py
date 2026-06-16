"""Tests for the KB orphan-vector sweep (audit F-15).

The orphan-detection / delete SQL is generic over a chunk_id LEFT JOIN, so it is
tested against plain in-memory tables (no sqlite-vec extension needed).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import cleanup_stale_vec as sweep  # noqa: E402


def _conn():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE knowledge_chunks (chunk_id TEXT PRIMARY KEY)")
    c.execute("CREATE TABLE knowledge_vec_bin (chunk_id TEXT)")
    c.execute("CREATE TABLE knowledge_vec_f32 (chunk_id TEXT)")
    return c


def test_find_orphans_detects_missing_chunks():
    c = _conn()
    c.execute("INSERT INTO knowledge_chunks VALUES ('a')")
    c.executemany("INSERT INTO knowledge_vec_bin VALUES (?)",
                  [("a",), ("orphan1",), ("orphan2",)])
    assert set(sweep.find_orphans(c, "knowledge_vec_bin")) == {"orphan1", "orphan2"}


def test_find_orphans_clean_when_all_have_chunks():
    c = _conn()
    c.execute("INSERT INTO knowledge_chunks VALUES ('a')")
    c.execute("INSERT INTO knowledge_vec_f32 VALUES ('a')")
    assert sweep.find_orphans(c, "knowledge_vec_f32") == []


def test_delete_orphans_removes_only_orphans():
    c = _conn()
    c.execute("INSERT INTO knowledge_chunks VALUES ('a')")
    c.executemany("INSERT INTO knowledge_vec_bin VALUES (?)", [("a",), ("orphan1",)])
    assert sweep.delete_orphans(c, "knowledge_vec_bin", ["orphan1"]) == 1
    remaining = [r[0] for r in c.execute("SELECT chunk_id FROM knowledge_vec_bin")]
    assert remaining == ["a"]


def test_delete_orphans_empty_is_noop():
    c = _conn()
    assert sweep.delete_orphans(c, "knowledge_vec_bin", []) == 0
