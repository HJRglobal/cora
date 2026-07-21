"""Tests for scripts/kb_hygiene_sweep.py -- the recurring --marked tier (Slice B).

No real Drive tree / no real KB: a temp Founder-OS tree + a temp plain sqlite DB.
The DB connectors are monkeypatched to plain sqlite3 so tests never need sqlite-vec.

Groups:
  A  banner parsing (em-dash / -- / - separators, no-reason, negatives)
  B  walk-skip + PHI
  C  scan_marked: collects banner'd files; REFUSES held/confidential paths
  D  select_marked: KEEP-as-class banner -> WARN, never archived
  E  run_marked dry-run: no mutation
  F  run_marked apply (small): moves + 3-table purge
  G  escalation: large sweep does NOT auto-apply; --allow-large bypasses
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest


def _load():
    sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
    import kb_hygiene_sweep as m  # noqa: E402
    return m


M = _load()

BANNER = "<!-- KB-STATUS: SUPERSEDED 2026-07-21 by 00-Founder\\new.md — old and busted -->"


@pytest.fixture
def fake(tmp_path, monkeypatch):
    root = tmp_path / "HJR-Founder-OS"
    arch = root / "_archive"
    logs = tmp_path / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(M, "FOUNDER_OS_ROOT", root)
    monkeypatch.setattr(M, "ARCHIVE_ROOT", arch)
    monkeypatch.setattr(M, "LOG_DIR", logs)

    def mk(rel, body="x", banner=True):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        text = (BANNER + "\n" if banner else "") + body
        p.write_text(text, encoding="utf-8")
        return p

    return root, arch, mk


def _db(tmp_path, monkeypatch):
    """Temp plain KB DB; patch the kb_archive connectors to plain sqlite3."""
    dbp = tmp_path / "kb.db"
    conn = sqlite3.connect(dbp)
    conn.execute("CREATE TABLE knowledge_chunks (chunk_id TEXT PRIMARY KEY, source TEXT, source_id TEXT, title TEXT, entity TEXT)")
    conn.execute("CREATE TABLE knowledge_vec_bin (chunk_id TEXT PRIMARY KEY, v TEXT)")
    conn.execute("CREATE TABLE knowledge_vec_f32 (chunk_id TEXT PRIMARY KEY, v TEXT)")
    conn.commit()
    conn.close()
    from cora import kb_archive
    monkeypatch.setattr(kb_archive, "connect_ro", lambda p: sqlite3.connect(str(dbp)))
    monkeypatch.setattr(kb_archive, "connect_rw", lambda p: sqlite3.connect(str(dbp)))
    return dbp


def _add(dbp, cid, source, source_id="", title="", entity="FNDR"):
    conn = sqlite3.connect(str(dbp))
    conn.execute("INSERT INTO knowledge_chunks VALUES (?,?,?,?,?)", (cid, source, source_id, title, entity))
    conn.execute("INSERT INTO knowledge_vec_bin VALUES (?,?)", (cid, "b"))
    conn.execute("INSERT INTO knowledge_vec_f32 VALUES (?,?)", (cid, "f"))
    conn.commit()
    conn.close()


def _count(dbp, cid):
    conn = sqlite3.connect(str(dbp))
    n = conn.execute("SELECT COUNT(*) FROM knowledge_chunks WHERE chunk_id=?", (cid,)).fetchone()[0]
    conn.close()
    return n


# ── A: banner parsing ─────────────────────────────────────────────────────────
def test_parse_banner_em_dash():
    b = M.parse_banner("<!-- KB-STATUS: SUPERSEDED 2026-07-21 by new/path.md — old -->")
    assert b == {"status": "SUPERSEDED", "date": "2026-07-21", "ref": "new/path.md", "reason": "old"}


def test_parse_banner_double_hyphen():
    b = M.parse_banner("<!-- KB-STATUS: SUPERSEDED 2026-07-21 by D-086 -- reason here -->")
    assert b["ref"] == "D-086" and b["reason"] == "reason here"


def test_parse_banner_no_reason():
    b = M.parse_banner("<!-- KB-STATUS: SUPERSEDED 2026-07-21 by 00-Founder\\x.md -->")
    assert b["ref"] == "00-Founder\\x.md" and b["reason"] == ""


def test_parse_banner_only_first_nonempty_line():
    # a banner AFTER a title/prose is documentation, NOT a real supersession
    assert M.parse_banner("# A title\n\n" + BANNER + "\nbody") is None
    # leading blank lines are allowed before a genuine first-line banner
    assert M.parse_banner("\n\n" + BANNER) is not None


def test_parse_banner_rejects_placeholder_ref():
    # the convention's OWN documentation example (a placeholder ref) must NOT match,
    # even on line 1 -- else any doc explaining the banner gets archived (the live
    # dry-run caught exactly this: the session prompt matched itself).
    ex = "<!-- KB-STATUS: SUPERSEDED 2026-07-21 by <relative/path/or/decision-ref> -- <one-line reason> -->"
    assert M.parse_banner(ex) is None


@pytest.mark.parametrize("txt", [
    "# just a title",
    "<!-- KB-STATUS: ACTIVE -->",
    "<!-- KB-STATUS: SUPERSEDED nodate by x -->",
    "",
])
def test_parse_banner_negatives(txt):
    assert M.parse_banner(txt) is None


# ── B: walk-skip + phi ────────────────────────────────────────────────────────
def test_is_phi_path():
    assert M.is_phi_path(Path(r"G:\x\clients\a.md"))
    assert not M.is_phi_path(Path(r"G:\x\normal\a.md"))


def test_walk_skip():
    assert M._walk_skip(Path(r"G:\x\_archive\a.md"))
    assert M._walk_skip(Path(r"G:\x\.hidden\a.md"))
    assert M._walk_skip(Path(r"G:\x\clinical\a.md"))
    assert not M._walk_skip(Path(r"G:\x\proj\a.md"))


# ── C: scan_marked ────────────────────────────────────────────────────────────
def test_scan_marked_collects_and_refuses(fake):
    root, arch, mk = fake
    mk(r"00-Founder\projects\p\note.md")                    # eligible
    mk(r"02-F3-Energy\_notes\old.md")                       # eligible
    mk(r"nobanner.md", banner=False)                        # no banner -> ignored
    mk(r"05-HJR-Productions\projects\watchtower\deal.md")   # HOLD -> refused
    mk(r"00-Founder\insurance\oneamerica\t.md")             # confidential -> refused
    mk(r"_archive\already\gone.md")                         # walk-skipped
    cfg = M.hygiene_cfg()
    marked, refused = M.scan_marked(cfg)
    rels = {m["rel"] for m in marked}
    assert r"00-Founder\projects\p\note.md" in rels
    assert r"02-F3-Energy\_notes\old.md" in rels
    assert not any("watchtower" in m["rel"] for m in marked)
    assert not any("oneamerica" in m["rel"] for m in marked)
    ref_rels = {r["rel"] for r in refused}
    assert any("watchtower" in r for r in ref_rels)
    assert any("oneamerica" in r for r in ref_rels)


def test_scan_ignores_documentation_example_banner(fake):
    """A doc that DOCUMENTS the banner (an example mid-prose) must NOT be collected
    -- the banner must be the FIRST non-empty line to count (regression for the
    live dry-run false-positive on the session prompt)."""
    root, arch, mk = fake
    p = root / "00-Founder" / "knowledge-doctrine.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# Knowledge persistence doctrine\n\nStamp superseded files: " + BANNER + "\n",
                 encoding="utf-8")
    marked, refused = M.scan_marked(M.hygiene_cfg())
    assert not any("knowledge-doctrine" in m["rel"] for m in marked)


# ── D: KEEP-as-class banner -> warn, not archive ──────────────────────────────
def test_marked_keep_class_warns_not_archives(fake):
    root, arch, mk = fake
    mk(r"00-Founder\projects\p\real.md")
    mk(r"00-Founder\projects\p\CLAUDE.md")     # class basename -> WARN
    mk(r"01-HJR-Global\memory\note.md")        # memory segment -> WARN
    cfg = M.hygiene_cfg()
    marked, refused = M.scan_marked(cfg)
    archive, report, class_filtered, substr_filtered = M.select_marked(marked, cfg)
    assert r"00-Founder\projects\p\real.md" in archive
    assert r"00-Founder\projects\p\CLAUDE.md" not in archive
    assert r"01-HJR-Global\memory\note.md" not in archive
    warned = {p for p, _ in class_filtered}
    assert r"00-Founder\projects\p\CLAUDE.md" in warned
    assert r"01-HJR-Global\memory\note.md" in warned


# ── E: dry-run ────────────────────────────────────────────────────────────────
def test_run_marked_dry_run_no_mutation(fake, tmp_path, monkeypatch):
    root, arch, mk = fake
    mk(r"00-Founder\p\a.md")
    dbp = _db(tmp_path, monkeypatch)
    cfg = M.hygiene_cfg()
    res = M.run_marked(cfg, dbp, apply=False, allow_large=False,
                       live_purge_max=500, live_move_max=100, drive_purge=True)
    assert res["mode"] == "DRY-RUN" and res["applied"] is False
    assert res["to_archive"] == 1
    assert (root / "00-Founder" / "p" / "a.md").exists()          # NOT moved
    assert not (arch / "00-Founder" / "p" / "a.md").exists()


# ── F: apply small ────────────────────────────────────────────────────────────
def test_run_marked_apply_small_moves_and_purges(fake, tmp_path, monkeypatch):
    root, arch, mk = fake
    mk(r"00-Founder\p\a.md")
    mk(r"02-F3-Energy\_notes\b.md")
    dbp = _db(tmp_path, monkeypatch)
    _add(dbp, "s1", "static_md", r"00-Founder\p\a.md")
    _add(dbp, "s2", "static_md", r"02-F3-Energy\_notes\b.md")
    _add(dbp, "keep", "static_md", r"unrelated\c.md")
    cfg = M.hygiene_cfg()
    res = M.run_marked(cfg, dbp, apply=True, allow_large=False,
                       live_purge_max=500, live_move_max=100, drive_purge=True)
    assert res["applied"] is True and res["escalated"] is False
    assert res["to_archive"] == 2 and res["purge_chunks"] == 2
    # files moved
    assert (arch / "00-Founder" / "p" / "a.md").exists()
    assert not (root / "00-Founder" / "p" / "a.md").exists()
    # chunks purged; unrelated kept
    assert _count(dbp, "s1") == 0 and _count(dbp, "s2") == 0
    assert _count(dbp, "keep") == 1


# ── G: escalation ─────────────────────────────────────────────────────────────
def test_run_marked_escalates_large_sweep(fake, tmp_path, monkeypatch):
    root, arch, mk = fake
    mk(r"00-Founder\p\a.md")
    mk(r"00-Founder\p\b.md")
    mk(r"00-Founder\p\c.md")
    dbp = _db(tmp_path, monkeypatch)
    cfg = M.hygiene_cfg()
    res = M.run_marked(cfg, dbp, apply=True, allow_large=False,
                       live_purge_max=500, live_move_max=1, drive_purge=True)  # move cap = 1
    assert res["escalated"] is True and res["applied"] is False
    # NOTHING moved
    assert (root / "00-Founder" / "p" / "a.md").exists()
    assert not (arch / "00-Founder" / "p" / "a.md").exists()


def test_run_marked_allow_large_bypasses(fake, tmp_path, monkeypatch):
    root, arch, mk = fake
    mk(r"00-Founder\p\a.md")
    mk(r"00-Founder\p\b.md")
    dbp = _db(tmp_path, monkeypatch)
    cfg = M.hygiene_cfg()
    res = M.run_marked(cfg, dbp, apply=True, allow_large=True,
                       live_purge_max=500, live_move_max=1, drive_purge=True)
    assert res["applied"] is True and res["escalated"] is False
    assert (arch / "00-Founder" / "p" / "a.md").exists()


# ── H: purge gated on move success (D-051 fix) ────────────────────────────────
def test_purge_gated_on_move_success(fake, tmp_path, monkeypatch):
    """A file whose move fails (CONFLICT / lock) KEEPS its chunks; only moved-ok
    files are purged -- else the KB drops a doc still live in the tree."""
    root, arch, mk = fake
    mk(r"00-Founder\p\a.md")
    mk(r"00-Founder\p\b.md")
    # pre-create a.md's archive dst so its move CONFLICTs (moved=False), src stays
    (arch / "00-Founder" / "p").mkdir(parents=True, exist_ok=True)
    (arch / "00-Founder" / "p" / "a.md").write_text("already", encoding="utf-8")
    dbp = _db(tmp_path, monkeypatch)
    _add(dbp, "sa", "static_md", r"00-Founder\p\a.md")
    _add(dbp, "sb", "static_md", r"00-Founder\p\b.md")
    res = M.run_marked(M.hygiene_cfg(), dbp, apply=True, allow_large=False,
                       live_purge_max=500, live_move_max=100, drive_purge=False)
    assert res["applied"] is True
    assert _count(dbp, "sa") == 1   # move CONFLICTed -> chunk KEPT
    assert _count(dbp, "sb") == 0   # moved OK -> purged
    assert (root / "00-Founder" / "p" / "a.md").exists()   # still live in the tree


# ── I: drive-purge opt-in + collision self-guard (D-051 fix) ──────────────────
def test_drive_purge_opt_in_default_off():
    assert M.build_arg_parser().parse_args([]).drive_purge is False


def test_hygiene_cfg_drive_max_fileids_is_one():
    assert M.hygiene_cfg().drive_title_max_fileids == 1


def test_drive_two_fileids_not_collateral_purged(tmp_path):
    """Under the hygiene cfg (max_fileids=1) a basename mapping to the superseded
    file's own copy + an UNRELATED same-named doc (2 file-ids) is refused, not
    collateral-purged."""
    from cora import kb_archive
    dbp = tmp_path / "kb.db"
    conn = sqlite3.connect(dbp)
    conn.execute("CREATE TABLE knowledge_chunks (chunk_id TEXT PRIMARY KEY, source TEXT, source_id TEXT, title TEXT, entity TEXT)")
    conn.execute("INSERT INTO knowledge_chunks VALUES ('y1','drive_sweep','fileY','roadmap.md','F3E')")
    conn.execute("INSERT INTO knowledge_chunks VALUES ('x1','drive_sweep','fileX','roadmap.md','OSN')")
    conn.commit()
    ids, inc, skip = kb_archive.select_drive_purge(conn, [r"02-F3-Energy\_notes\roadmap.md"], M.hygiene_cfg())
    assert ids == []   # 2 file-ids > max(1) -> ambiguous -> NOT purged
    assert any("ambiguous" in s["reason"] for s in skip)
