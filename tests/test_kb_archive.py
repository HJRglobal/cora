"""Tests for src/cora/kb_archive.py -- the shared archive/purge core.

The one-time D-086 tool's own suite (test_archive_founder_os_kb.py) already
pins the core THROUGH the wrappers (parity). These tests prove the core
GENERALIZES for the recurring kb_hygiene_sweep: a different config (fresh roots,
copa_purge_glob disabled, extra confidential-store predicates), the new
archived_date manifest field, and the fail-closed extra-hold-predicate contract.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from cora import kb_archive
from cora.kb_archive import ArchiveConfig, HoldGuardTripped


def _cfg(root: Path, **over) -> ArchiveConfig:
    base = dict(
        founder_os_root=root,
        archive_root=root / "_archive",
        hold_segments=frozenset({"watchtower"}),
        keep_class_basenames=frozenset({"claude.md", "readme.md"}),
        keep_class_segments=frozenset({"memory", "playbooks"}),
        keep_class_basename_substr=("brand-guidelines",),
        class_exceptions=frozenset(),
        keep_substrings=(),
        scaffold_basenames=frozenset({"00-readme.md"}),
        drive_title_max_fileids=2,
        copa_purge_glob=None,          # hygiene-sweep default: no copa whole-folder purge
        copa_drive_titles=(),
        copa_loose_dup=None,           # any copa path aborts
        batch=500,
    )
    base.update(over)
    return ArchiveConfig(**base)


def _db(tmp_path):
    dbp = tmp_path / "kb.db"
    conn = sqlite3.connect(dbp)
    conn.execute("CREATE TABLE knowledge_chunks (chunk_id TEXT PRIMARY KEY, source TEXT, source_id TEXT, title TEXT, entity TEXT)")
    conn.execute("CREATE TABLE knowledge_vec_bin (chunk_id TEXT PRIMARY KEY, v TEXT)")
    conn.execute("CREATE TABLE knowledge_vec_f32 (chunk_id TEXT PRIMARY KEY, v TEXT)")
    conn.commit()
    return conn


def _add(conn, cid, source, source_id="", title="", entity="FNDR"):
    conn.execute("INSERT INTO knowledge_chunks VALUES (?,?,?,?,?)", (cid, source, source_id, title, entity))
    conn.execute("INSERT INTO knowledge_vec_bin VALUES (?,?)", (cid, "b"))
    conn.execute("INSERT INTO knowledge_vec_f32 VALUES (?,?)", (cid, "f"))
    conn.commit()


def _mk(root: Path, rel: str, body="x") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


# ── generalized roots / keep filters ──────────────────────────────────────────
def test_core_uses_config_roots_not_module_globals(tmp_path):
    cfg = _cfg(tmp_path / "SomeOtherRoot")
    _mk(cfg.founder_os_root, r"proj\_notes\a.md")
    _mk(cfg.founder_os_root, r"proj\CLAUDE.md")
    clusters = [{"id": "c", "globs": [r"proj\**\*.md"], "explicit": [], "keep": [],
                 "expected": "", "purge": True}]
    archive, report, cls, sub = kb_archive.build_move_manifest(clusters, cfg)
    assert r"proj\_notes\a.md" in archive
    assert r"proj\CLAUDE.md" not in archive           # keep-as-class basename
    assert any("CLAUDE" in p or "claude" in p for p, _ in cls)


def test_rel_uses_config_root(tmp_path):
    cfg = _cfg(tmp_path / "R")
    p = cfg.founder_os_root / "a" / "b.md"
    assert kb_archive.rel(p, cfg) == r"a\b.md"


# ── extra_hold_predicates: confidential-store union, fail-closed ───────────────
def test_extra_hold_predicate_trips(tmp_path):
    def looks_confidential(rp: str) -> bool:
        return "oneamerica" in rp.lower()
    cfg = _cfg(tmp_path / "R", extra_hold_predicates=(looks_confidential,))
    assert kb_archive.hold_reason(r"00-Founder\insurance\oneamerica\x.md", cfg) is not None
    assert kb_archive.hold_reason(r"00-Founder\normal\x.md", cfg) is None


def test_extra_hold_predicate_that_raises_fails_closed(tmp_path):
    def boom(rp: str) -> bool:
        raise ValueError("broken predicate")
    cfg = _cfg(tmp_path / "R", extra_hold_predicates=(boom,))
    reason = kb_archive.hold_reason(r"anything.md", cfg)
    assert reason is not None and "fail-closed" in reason


def test_build_manifest_aborts_on_extra_hold_predicate(tmp_path):
    def looks_confidential(rp: str) -> bool:
        return "capital-raise" in rp.lower()
    cfg = _cfg(tmp_path / "R", extra_hold_predicates=(looks_confidential,))
    _mk(cfg.founder_os_root, r"proj\capital-raise\deck.md")
    clusters = [{"id": "c", "globs": [], "explicit": [r"proj\capital-raise\deck.md"],
                 "keep": [], "expected": "", "purge": True}]
    with pytest.raises(HoldGuardTripped):
        kb_archive.build_move_manifest(clusters, cfg)


def test_copa_path_always_holds_when_loose_dup_none(tmp_path):
    cfg = _cfg(tmp_path / "R")   # copa_loose_dup=None
    assert kb_archive.hold_reason(r"08-Lexington-Services\projects\copa-bhrf\x.md", cfg) is not None


# ── static purge: copa GLOB disabled by default ───────────────────────────────
def test_static_purge_no_copa_glob_when_disabled(tmp_path):
    cfg = _cfg(tmp_path / "R")           # copa_purge_glob=None
    conn = _db(tmp_path)
    _add(conn, "1", "static_md", r"a\b.md")
    _add(conn, "c1", "static_md", r"08-Lexington-Services\projects\copa-bhrf\x.md")
    ids, moved, copa = kb_archive.select_static_purge(conn, [r"a\b.md"], cfg)
    assert ids == ["1"]              # copa chunk NOT purged (glob disabled)
    assert moved == 1 and copa == 0


def test_static_purge_copa_glob_when_enabled(tmp_path):
    cfg = _cfg(tmp_path / "R", copa_purge_glob=r"08-Lexington-Services\projects\copa-bhrf\*")
    conn = _db(tmp_path)
    _add(conn, "c1", "static_md", r"08-Lexington-Services\projects\copa-bhrf\x.md")
    ids, moved, copa = kb_archive.select_static_purge(conn, [], cfg)
    assert ids == ["c1"] and copa == 1


# ── drive purge self-guard generalizes ────────────────────────────────────────
def test_drive_purge_self_guard(tmp_path):
    cfg = _cfg(tmp_path / "R")
    conn = _db(tmp_path)
    for i, fid in enumerate(["A", "B", "C"]):
        _add(conn, f"x{i}", "drive_sweep", fid, "collides.md")
    ids, inc, skip = kb_archive.select_drive_purge(conn, [r"p\collides.md"], cfg)
    assert ids == [] and any("ambiguous" in s["reason"] for s in skip)


# ── delete cascade ─────────────────────────────────────────────────────────────
def test_delete_chunks_three_tables(tmp_path):
    cfg = _cfg(tmp_path / "R")
    conn = _db(tmp_path)
    _add(conn, "k1", "static_md", "p1")
    _add(conn, "k2", "static_md", "p2")
    totals = kb_archive.delete_chunks(conn, ["k1"], cfg)
    assert totals == {"knowledge_vec_bin": 1, "knowledge_vec_f32": 1, "knowledge_chunks": 1}
    assert conn.execute("SELECT COUNT(*) FROM knowledge_chunks WHERE chunk_id='k2'").fetchone()[0] == 1


# ── manifest carries archived_date (for GC retention) ──────────────────────────
def test_write_manifest_emits_archived_date(tmp_path):
    cfg = _cfg(tmp_path / "R")
    man = tmp_path / "m.json"
    kb_archive.write_manifest(
        man, cfg, mode="DRY-RUN",
        report={"c": {"section": "s", "expected": "1", "count": 1, "purge": True}},
        moves=[{"src_rel": r"a\b.md", "dst_rel": r"_archive\a\b.md"}],
        class_filtered=[], substr_filtered=[], static_ids=["s1"], drive_ids=[],
        moved_static=1, copa_static=0, drive_included=[], drive_skipped=[],
        purge_enabled=True)
    data = json.loads(man.read_text(encoding="utf-8"))
    assert "archived_date" in data and len(data["archived_date"]) == 10  # YYYY-MM-DD
    assert data["purge"]["static_chunk_ids"] == ["s1"]


# ── move + revert round-trip with config roots ────────────────────────────────
def test_move_revert_roundtrip_config(tmp_path):
    cfg = _cfg(tmp_path / "R")
    _mk(cfg.founder_os_root, r"d\a.md", "body")
    moves = kb_archive.plan_moves([r"d\a.md"], cfg)
    kb_archive.execute_moves(moves, cfg)
    assert (cfg.archive_root / "d" / "a.md").exists()
    assert not (cfg.founder_os_root / "d" / "a.md").exists()
    man = tmp_path / "man.json"
    man.write_text(json.dumps({"moves": moves}), encoding="utf-8")
    kb_archive.revert(man, cfg)
    assert (cfg.founder_os_root / "d" / "a.md").read_text(encoding="utf-8") == "body"
