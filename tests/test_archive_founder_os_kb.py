"""Tests for scripts/archive_founder_os_kb.py -- the Founder-OS KB archive+purge pass.

Covers the safety-critical machinery WITHOUT touching the real Drive tree or the
live KB: a temp Founder-OS tree + a temp sqlite DB (plain tables -- no sqlite-vec
needed; the DELETE statements are table-agnostic).

Groups:
  A  HOLD hard-guard aborts on any held/sensitive path (exit 2), copa exception.
  B  expand_archive_set honors KEEP-as-class (glob), CLASS_EXCEPTIONS (explicit
     bypass), explicit-class-blocked, per-cluster keep, substring-KEEP.
  C  select_static_purge -- exact IN (a '_' is NOT a wildcard) + copa whole-folder GLOB.
  D  select_drive_purge -- self-guard: distinctive included, scaffolding + >2-file-id skipped.
  E  delete_chunks -- 3-table batched cascade.
  F  moves + revert round-trip (reversibility).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest


def _load():
    sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
    import archive_founder_os_kb as m  # noqa: E402
    return m


M = _load()


# ── fixtures ────────────────────────────────────────────────────────────────
@pytest.fixture
def fake_os(tmp_path, monkeypatch):
    """A tiny Founder-OS tree + patched module roots."""
    root = tmp_path / "HJR-Founder-OS"
    arch = root / "_archive"
    monkeypatch.setattr(M, "FOUNDER_OS_ROOT", root)
    monkeypatch.setattr(M, "ARCHIVE_ROOT", arch)

    def mk(rel, body="x"):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        return p

    return root, arch, mk


def _db(tmp_path):
    """Temp KB DB with the 3 cascade tables (plain -- delete is table-agnostic)."""
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


# ── A: HOLD guard ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("relpath", [
    r"05-HJR-Productions\projects\watchtower\_notes\deal.md",
    r"00-Founder\insurance\oneamerica\tracker.md",
    r"02-F3-Energy\projects\capital-raise\deck.md",
    r"00-Founder\travel-points\points.md",
])
def test_hold_reason_flags_held(relpath):
    assert M._hold_reason(relpath) is not None


def test_hold_reason_copa_only_loose_dup_allowed():
    # the one sanctioned copa archive path is allowed; any other copa path aborts
    assert M._hold_reason(M.COPA_LOOSE_DUP) is None
    other = r"08-Lexington-Services\projects\copa-bhrf\LBHS - COPA Research Project\_notes\2026-05-15-fireflies-context-doc.md"
    assert M._hold_reason(other) is not None


def test_hold_reason_ignores_normal_paths():
    assert M._hold_reason(r"00-Founder\tag-standup\x.md") is None


def test_expand_aborts_on_hold(fake_os):
    root, arch, mk = fake_os
    mk(r"05-HJR-Productions\projects\watchtower\_notes\deal.md")
    clusters = [{"id": "bad", "globs": [r"05-HJR-Productions\projects\watchtower\**\*.md"],
                 "explicit": [], "keep": [], "expected": "0", "purge": True}]
    with pytest.raises(SystemExit) as e:
        M.expand_archive_set(clusters)
    assert e.value.code == 2


def test_expand_aborts_on_hold_via_explicit(fake_os):
    root, arch, mk = fake_os
    mk(r"02-F3-Energy\projects\capital-raise\deck.md")
    clusters = [{"id": "bad", "globs": [],
                 "explicit": [r"02-F3-Energy\projects\capital-raise\deck.md"],
                 "keep": [], "expected": "0", "purge": True}]
    with pytest.raises(SystemExit) as e:
        M.expand_archive_set(clusters)
    assert e.value.code == 2


# ── B: KEEP filters ───────────────────────────────────────────────────────────
def test_glob_drops_keep_as_class(fake_os):
    root, arch, mk = fake_os
    mk(r"proj\_notes\a.md")
    mk(r"proj\CLAUDE.md")          # class basename
    mk(r"proj\README.md")          # class basename
    mk(r"proj\_notes\00-README.md")  # NOT README.md exact -> archivable
    clusters = [{"id": "c", "globs": [r"proj\**\*.md"], "explicit": [], "keep": [],
                 "expected": "", "purge": True}]
    archive, report, cls, sub = M.expand_archive_set(clusters)
    assert r"proj\_notes\a.md" in archive
    assert r"proj\_notes\00-README.md" in archive       # 00-README is archivable
    assert r"proj\CLAUDE.md" not in archive
    assert r"proj\README.md" not in archive
    assert any("CLAUDE.md" in p for p, _ in cls)


def test_memory_segment_kept_even_if_explicit(fake_os):
    """LEAK-2: a memory/** file listed explicitly must STILL be class-blocked."""
    root, arch, mk = fake_os
    mk(r"06-HJR-Properties\rogers-ranch\memory\stub.md")
    clusters = [{"id": "c", "globs": [],
                 "explicit": [r"06-HJR-Properties\rogers-ranch\memory\stub.md"],
                 "keep": [], "expected": "", "purge": True}]
    archive, report, cls, sub = M.expand_archive_set(clusters)
    assert archive == []
    assert any("memory" in w for _, w in cls)


def test_class_exception_explicit_bypasses(fake_os):
    root, arch, mk = fake_os
    for rel in M.CLASS_EXCEPTIONS:
        mk(rel)
    clusters = [{"id": "c", "globs": [], "explicit": list(M.CLASS_EXCEPTIONS),
                 "keep": [], "expected": "", "purge": True}]
    archive, report, cls, sub = M.expand_archive_set(clusters)
    assert set(archive) == set(M.CLASS_EXCEPTIONS)


def test_per_cluster_keep_excludes(fake_os):
    root, arch, mk = fake_os
    mk(r"d\a.md"); mk(r"d\keepme.md")
    clusters = [{"id": "c", "globs": [r"d\*.md"], "explicit": [],
                 "keep": [r"d\keepme.md"], "expected": "", "purge": True}]
    archive, *_ = M.expand_archive_set(clusters)
    assert r"d\a.md" in archive and r"d\keepme.md" not in archive


def test_substring_keep_guard(fake_os, monkeypatch):
    root, arch, mk = fake_os
    monkeypatch.setattr(M, "KEEP_SUBSTRINGS", ("bootstrap-context-2026-05-24",))
    mk(r"_shared\projects\cora\bootstrap-context-2026-05-24\kit.md")
    clusters = [{"id": "c", "globs": [r"_shared\projects\cora\**\*.md"], "explicit": [],
                 "keep": [], "expected": "", "purge": True}]
    archive, report, cls, sub = M.expand_archive_set(clusters)
    assert archive == []
    assert any("bootstrap-context" in w for _, w in sub)


# ── C: static purge selection (exact IN + copa GLOB) ──────────────────────────
def test_static_purge_exact_in_no_underscore_wildcard(tmp_path):
    conn = _db(tmp_path)
    _add(conn, "1", "static_md", r"a\b_c.md")     # target (has an underscore)
    _add(conn, "2", "static_md", r"a\bXc.md")     # decoy: '_' must NOT wildcard-match
    _add(conn, "3", "gmail", r"a\b_c.md")         # wrong source
    ids, moved, copa = M.select_static_purge(conn, [r"a\b_c.md"])
    assert ids == ["1"]           # exact match only; NOT chunk 2, NOT chunk 3
    assert moved == 1 and copa == 0


def test_static_purge_copa_whole_folder_glob(tmp_path):
    conn = _db(tmp_path)
    _add(conn, "c1", "static_md", r"08-Lexington-Services\projects\copa-bhrf\CLAUDE.md")
    _add(conn, "c2", "static_md", r"08-Lexington-Services\projects\copa-bhrf\_notes\x.md")
    _add(conn, "c3", "static_md", r"08-Lexington-Services\projects\copa-bhrf\LBHS - COPA Research Project\_notes\y.md")
    _add(conn, "n1", "static_md", r"08-Lexington-Services\projects\copa-bhrfOTHER\z.md")  # NOT copa-bhrf
    _add(conn, "n2", "static_md", r"08-Lexington-Services\projects\other\w.md")
    ids, moved, copa = M.select_static_purge(conn, [])
    assert set(ids) == {"c1", "c2", "c3"}
    assert copa == 3


# ── D: drive-copy purge self-guard ───────────────────────────────────────────
def test_drive_purge_distinctive_included(tmp_path):
    conn = _db(tmp_path)
    _add(conn, "d1", "drive_sweep", "fileidA", "2026-06-24_fndr_tag-standup-RUNBOOK.md")
    _add(conn, "d2", "drive_asset", "fileidA", "2026-06-24_fndr_tag-standup-RUNBOOK.md")  # same file-id
    ids, inc, skip = M.select_drive_purge(conn, [r"00-Founder\tag-standup\2026-06-24_fndr_tag-standup-RUNBOOK.md"])
    assert set(ids) == {"d1", "d2"}
    assert inc and inc[0]["file_ids"] == 1


def test_drive_purge_ambiguous_skipped(tmp_path):
    conn = _db(tmp_path)
    # same title across 3 DISTINCT file-ids -> collision -> skipped
    for i, fid in enumerate(["A", "B", "C"]):
        _add(conn, f"x{i}", "drive_sweep", fid, "notes.md")
    ids, inc, skip = M.select_drive_purge(conn, [r"proj\notes.md"])
    assert ids == []
    assert any("ambiguous" in s["reason"] for s in skip)


def test_drive_purge_scaffolding_denylist_skipped(tmp_path):
    conn = _db(tmp_path)
    _add(conn, "r1", "drive_sweep", "F1", "00-README.md")
    ids, inc, skip = M.select_drive_purge(conn, [r"proj\_notes\2026-05-18-nightly-sweep\00-README.md"])
    assert ids == []
    assert any(s["title"] == "00-README.md" and "scaffold" in s["reason"] for s in skip)


def test_drive_purge_copa_titles_included(tmp_path):
    conn = _db(tmp_path)
    _add(conn, "cd1", "drive_sweep", "COPA1", "2026-05-15-fireflies-context-doc.md")
    _add(conn, "cd2", "drive_sweep", "COPA2", "2026-05-15-fireflies-context-doc-supplement-tldr-and-reframings.md")
    ids, inc, skip = M.select_drive_purge(conn, [])   # copa titles added unconditionally
    assert set(ids) == {"cd1", "cd2"}


def test_drive_purge_included_carries_sources_for_review(tmp_path):
    """D-051 NEEDS_HARRISON fix: each included title records its file-ids + entity
    so the human manifest review can spot a 2-way basename collision."""
    conn = _db(tmp_path)
    _add(conn, "e1", "drive_sweep", "FID", "2026-06-24_fndr_tag-standup-RUNBOOK.md", entity="FNDR")
    ids, inc, skip = M.select_drive_purge(conn, [r"00-Founder\tag-standup\2026-06-24_fndr_tag-standup-RUNBOOK.md"])
    assert ids == ["e1"]
    assert inc[0]["sources"][0]["file_id"] == "FID"
    assert "FNDR" in inc[0]["sources"][0]["entities"]


# ── G: manifest persists purge chunk_ids (resume/two-phase safety) ────────────
def test_write_manifest_persists_purge_chunk_ids(fake_os, tmp_path):
    """D-051 CONFIRMED #1 fix: the manifest carries the resolved purge chunk_ids so
    a purge-only resume reads them instead of re-globbing a moved tree."""
    import json
    root, arch, mk = fake_os
    man = tmp_path / "m.json"   # tmp_path exists; `root` isn't created until mk() is called
    report = {"c": {"section": "s", "expected": "1", "count": 1, "purge": True}}
    moves = [{"src_rel": r"a\b.md", "dst_rel": r"_archive\a\b.md"}]
    M.write_manifest(man, mode="DRY-RUN", report=report, moves=moves,
                     class_filtered=[], substr_filtered=[],
                     static_ids=["s1", "s2"], drive_ids=["d1"],
                     moved_static=2, copa_static=0,
                     drive_included=[{"title": "b.md", "chunks": 1, "file_ids": 1,
                                      "sources": [{"file_id": "F", "chunks": 1, "entities": ["FNDR"]}]}],
                     drive_skipped=[], purge_enabled=True)
    data = json.loads(man.read_text(encoding="utf-8"))
    assert data["purge"]["static_chunk_ids"] == ["s1", "s2"]
    assert data["purge"]["drive_chunk_ids"] == ["d1"]
    assert data["purge"]["static_md_chunks_total"] == 2
    # a resume reads these back exactly (no re-glob)
    reload_static = data["purge"]["static_chunk_ids"]
    reload_drive = data["purge"]["drive_chunk_ids"]
    assert sorted(set(reload_static) | set(reload_drive)) == ["d1", "s1", "s2"]


# ── E: delete cascade ─────────────────────────────────────────────────────────
def test_delete_chunks_all_three_tables(tmp_path):
    conn = _db(tmp_path)
    _add(conn, "k1", "static_md", "p1")
    _add(conn, "k2", "static_md", "p2")
    totals = M.delete_chunks(conn, ["k1"])
    assert totals == {"knowledge_vec_bin": 1, "knowledge_vec_f32": 1, "knowledge_chunks": 1}
    for tbl in ("knowledge_chunks", "knowledge_vec_bin", "knowledge_vec_f32"):
        assert conn.execute(f"SELECT COUNT(*) FROM {tbl} WHERE chunk_id='k1'").fetchone()[0] == 0
        assert conn.execute(f"SELECT COUNT(*) FROM {tbl} WHERE chunk_id='k2'").fetchone()[0] == 1


# ── F: move + revert round-trip ───────────────────────────────────────────────
def test_move_and_revert_roundtrip(fake_os):
    root, arch, mk = fake_os
    mk(r"00-Founder\tag-standup\a.md", "content-a")
    mk(r"_shared\hygiene-pending-moves\b.md", "content-b")
    relpaths = [r"00-Founder\tag-standup\a.md", r"_shared\hygiene-pending-moves\b.md"]
    moves = M.plan_moves(relpaths)
    M.execute_moves(moves)
    # moved: originals gone, archive copies present
    for rel in relpaths:
        assert not (root / rel).exists()
        assert (arch / rel).exists()
    assert all(m["moved"] for m in moves)
    # revert restores originals
    import json
    manifest = root / "man.json"
    manifest.write_text(json.dumps({"moves": moves}), encoding="utf-8")
    M.revert(manifest)
    for rel in relpaths:
        assert (root / rel).exists()
        assert not (arch / rel).exists()
    assert (root / relpaths[0]).read_text(encoding="utf-8") == "content-a"


def test_execute_moves_missing_src_is_soft(fake_os):
    root, arch, mk = fake_os
    moves = M.plan_moves([r"does\not\exist.md"])
    M.execute_moves(moves)
    assert moves[0]["moved"] is False and moves[0]["result"] == "src-missing"


def test_execute_moves_conflict_skips(fake_os):
    root, arch, mk = fake_os
    mk(r"d\c.md", "src")
    (arch / "d").mkdir(parents=True, exist_ok=True)
    (arch / "d" / "c.md").write_text("already", encoding="utf-8")
    moves = M.plan_moves([r"d\c.md"])
    M.execute_moves(moves)
    assert moves[0]["moved"] is False and "CONFLICT" in moves[0]["result"]
    assert (root / "d" / "c.md").read_text(encoding="utf-8") == "src"  # untouched
