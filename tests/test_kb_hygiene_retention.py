"""Tests for Slice D of kb_hygiene_sweep: --gc retention, --from-manifest resume,
and the report composition. Retention safety (§7D) is the focus:
  * never deletes inside the restore-days window (threshold clamped up)
  * never touches a path outside _archive (resolved-path containment)
  * reads ONLY this loop's kb-hygiene-manifest-* (never the D-086 manifest)
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest


def _load():
    sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
    import kb_hygiene_sweep as m  # noqa: E402
    return m


M = _load()
NOW = datetime(2026, 7, 21).timestamp()


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "HJR-Founder-OS"
    arch = root / "_archive"
    logs = tmp_path / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(M, "FOUNDER_OS_ROOT", root)
    monkeypatch.setattr(M, "ARCHIVE_ROOT", arch)
    monkeypatch.setattr(M, "LOG_DIR", logs)
    return root, arch, logs


def _manifest(logs, name, archived_date, moves):
    (logs / name).write_text(json.dumps({
        "archived_date": archived_date, "generated_at": archived_date + "T00:00:00",
        "moves": moves,
    }), encoding="utf-8")


def _mkarch(arch, rel, body="x"):
    p = arch / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


# ── GC retention ──────────────────────────────────────────────────────────────
def test_gc_deletes_aged_only_on_apply(env):
    root, arch, logs = env
    _mkarch(arch, r"00-Founder\p\old.md")
    _manifest(logs, "kb-hygiene-manifest-2026-01-01_000000.json", "2026-01-01",
              [{"src_rel": r"00-Founder\p\old.md", "moved": True}])
    cfg = M.hygiene_cfg()
    # dry-run: eligible, not deleted
    r = M.run_gc(cfg, restore_days=30, purge_after_days=180, apply=False, now_ts=NOW)
    assert r["count"] == 1 and r["applied"] is False
    assert (arch / "00-Founder" / "p" / "old.md").exists()
    # apply: deleted
    r = M.run_gc(cfg, restore_days=30, purge_after_days=180, apply=True, now_ts=NOW)
    assert r["count"] == 1 and r["applied"] is True
    assert not (arch / "00-Founder" / "p" / "old.md").exists()


def test_gc_keeps_within_restore_window(env):
    root, arch, logs = env
    _mkarch(arch, r"p\recent.md")
    _manifest(logs, "kb-hygiene-manifest-2026-07-10_000000.json", "2026-07-10",
              [{"src_rel": r"p\recent.md", "moved": True}])
    cfg = M.hygiene_cfg()
    r = M.run_gc(cfg, restore_days=30, purge_after_days=180, apply=True, now_ts=NOW)
    assert r["count"] == 0
    assert (arch / "p" / "recent.md").exists()   # 11d old -> kept


def test_gc_threshold_clamped_to_restore_days(env):
    """§7D: purge_after < restore must NOT delete inside the restore window."""
    root, arch, logs = env
    _mkarch(arch, r"p\x.md")
    _manifest(logs, "kb-hygiene-manifest-2026-06-25_000000.json", "2026-06-25",  # ~26d old
              [{"src_rel": r"p\x.md", "moved": True}])
    cfg = M.hygiene_cfg()
    # even with an absurd purge_after=1, restore_days=30 floors it -> 26d kept
    r = M.run_gc(cfg, restore_days=30, purge_after_days=1, apply=True, now_ts=NOW)
    assert r["effective_purge_after_days"] == 30 and r["count"] == 0
    assert (arch / "p" / "x.md").exists()


def test_gc_refuses_non_archive_path(env):
    root, arch, logs = env
    # a corrupt/hostile manifest entry that escapes _archive via ..
    escape = root / "NOT-ARCHIVE" / "escape.md"
    escape.parent.mkdir(parents=True, exist_ok=True)
    escape.write_text("live", encoding="utf-8")
    _manifest(logs, "kb-hygiene-manifest-2026-01-01_000000.json", "2026-01-01",
              [{"src_rel": r"..\NOT-ARCHIVE\escape.md", "moved": True}])
    cfg = M.hygiene_cfg()
    r = M.run_gc(cfg, restore_days=30, purge_after_days=180, apply=True, now_ts=NOW)
    assert r["count"] == 0
    assert any("not under _archive" in e["why"] for e in r["errors"])
    assert escape.exists()   # NEVER touched


def test_gc_ignores_d086_manifest(env):
    root, arch, logs = env
    _mkarch(arch, r"p\from-d086.md")
    # the one-time D-086 manifest uses a DIFFERENT prefix -> GC must not read it
    _manifest(logs, "archive-founder-os-manifest-2026-01-01_000000.json", "2026-01-01",
              [{"src_rel": r"p\from-d086.md", "moved": True}])
    cfg = M.hygiene_cfg()
    r = M.run_gc(cfg, restore_days=30, purge_after_days=180, apply=True, now_ts=NOW)
    assert r["count"] == 0
    assert (arch / "p" / "from-d086.md").exists()


# ── from-manifest resume ──────────────────────────────────────────────────────
def test_run_from_manifest_purges(tmp_path, monkeypatch):
    dbp = tmp_path / "kb.db"
    conn = sqlite3.connect(dbp)
    for t in ("knowledge_chunks", "knowledge_vec_bin", "knowledge_vec_f32"):
        col = "chunk_id TEXT PRIMARY KEY, source TEXT, source_id TEXT, title TEXT, entity TEXT" if t == "knowledge_chunks" else "chunk_id TEXT PRIMARY KEY, v TEXT"
        conn.execute(f"CREATE TABLE {t} ({col})")
    for cid in ("s1", "d1", "keep"):
        conn.execute("INSERT INTO knowledge_chunks (chunk_id) VALUES (?)", (cid,))
        conn.execute("INSERT INTO knowledge_vec_bin VALUES (?,?)", (cid, "b"))
        conn.execute("INSERT INTO knowledge_vec_f32 VALUES (?,?)", (cid, "f"))
    conn.commit()
    conn.close()
    from cora import kb_archive
    monkeypatch.setattr(kb_archive, "connect_rw", lambda p: sqlite3.connect(str(dbp)))
    man = tmp_path / "m.json"
    man.write_text(json.dumps({"purge": {"static_chunk_ids": ["s1"], "drive_chunk_ids": ["d1"]}}), encoding="utf-8")
    r = M.run_from_manifest(man, dbp, apply=True)
    assert r["applied"] is True and r["chunks"] == 2
    c = sqlite3.connect(str(dbp))
    assert c.execute("SELECT COUNT(*) FROM knowledge_chunks WHERE chunk_id IN ('s1','d1')").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM knowledge_chunks WHERE chunk_id='keep'").fetchone()[0] == 1
    c.close()


# ── report composition ────────────────────────────────────────────────────────
def test_compose_report_escalation_and_proactive():
    report = {
        "marked": {"to_archive": 250, "purge_chunks": 900, "applied": False, "escalated": True,
                   "refused_held": [{"rel": "x"}], "keep_class_warned": [], "manifest": "logs/m.json"},
        "proactive": {"near_dupes": [{"path": "a.md"}], "ttl_oneoffs": [], "resolved_pending": []},
        "gc": {"count": 3, "applied": True, "effective_purge_after_days": 180},
    }
    txt = M.compose_report(report)
    assert "ESCALATED" in txt and "run-kb-hygiene-apply.ps1" in txt
    assert "near-dupe: 1" in txt
    assert "GC" in txt and "3 aged" in txt
