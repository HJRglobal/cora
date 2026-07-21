"""Tests for the --proactive propose-only detectors (Slice C) of kb_hygiene_sweep.

Propose-only: every assertion checks that detectors PROPOSE candidates and never
move/purge. Near-dupe monkeypatches the KB-vector fetch so no real KB is needed.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest


def _load():
    sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
    import kb_hygiene_sweep as m  # noqa: E402
    return m


M = _load()

BANNER = "<!-- KB-STATUS: SUPERSEDED 2026-07-21 by x.md -->"
NOW = datetime(2026, 7, 21).timestamp()
DAY = 86400.0


# ── helpers ───────────────────────────────────────────────────────────────────
def test_file_date_ts_from_name():
    ts = M._file_date_ts(Path(r"a\2026-01-15_x.md"), 999.0)
    assert ts == datetime.strptime("2026-01-15", "%Y-%m-%d").timestamp()


def test_file_date_ts_falls_back_to_mtime():
    assert M._file_date_ts(Path(r"a\no-date.md"), 555.0) == 555.0


def test_project_dir_notes_parent():
    assert M._project_dir(r"02-F3-Energy\projects\p\_notes\x.md") == r"02-F3-Energy\projects\p"
    assert M._project_dir(r"02-F3-Energy\projects\p\x.md") == r"02-F3-Energy\projects\p"


# ── gather_candidate_files exclusions ─────────────────────────────────────────
@pytest.fixture
def fake(tmp_path, monkeypatch):
    root = tmp_path / "HJR-Founder-OS"
    monkeypatch.setattr(M, "FOUNDER_OS_ROOT", root)
    monkeypatch.setattr(M, "ARCHIVE_ROOT", root / "_archive")

    def mk(rel, body="hello world content", banner=False):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text((BANNER + "\n" if banner else "") + body, encoding="utf-8")
        return p

    return root, mk


def test_gather_excludes_banner_held_keepclass(fake):
    root, mk = fake
    mk(r"00-Founder\p\good.md")
    mk(r"00-Founder\p\marked.md", banner=True)                # banner'd -> excluded
    mk(r"00-Founder\p\CLAUDE.md")                             # keep-class -> excluded
    mk(r"05-HJR-Productions\projects\watchtower\deal.md")     # held -> excluded
    mk(r"_archive\old\x.md")                                  # walk-skip
    cfg = M.hygiene_cfg()
    rels = {f["rel"] for f in M.gather_candidate_files(cfg)}
    assert r"00-Founder\p\good.md" in rels
    assert r"00-Founder\p\marked.md" not in rels
    assert r"00-Founder\p\CLAUDE.md" not in rels
    assert not any("watchtower" in r for r in rels)
    assert not any("_archive" in r for r in rels)


# ── near-dupe ─────────────────────────────────────────────────────────────────
def test_detect_near_dupes_proposes_older(monkeypatch):
    from cora import kb_archive
    monkeypatch.setattr(kb_archive, "connect_ro", lambda p: _Dummy())
    centroids = {
        r"proj\2026-01-01_a.md": [1.0, 0.0, 0.0],       # oldest
        r"proj\2026-02-01_b.md": [0.99, 0.01, 0.0],     # near-dupe of a (newer)
        r"proj\2026-03-01_c.md": [0.0, 1.0, 0.0],       # dissimilar (newest)
    }
    monkeypatch.setattr(M, "_fetch_file_centroid", lambda conn, sid: centroids.get(sid))
    files = [{"rel": rel, "date_ts": M._file_date_ts(Path(rel), 0.0)} for rel in centroids]
    props = M.detect_near_dupes(M.hygiene_cfg(), Path("x.db"), files, threshold=0.9, max_proposals=25)
    paths = {p["path"] for p in props}
    assert r"proj\2026-01-01_a.md" in paths          # older of the near-dupe pair
    assert r"proj\2026-03-01_c.md" not in paths       # dissimilar, never proposed
    assert props[0]["superseded_by"] == r"proj\2026-02-01_b.md"


def test_detect_near_dupes_below_threshold_none(monkeypatch):
    from cora import kb_archive
    monkeypatch.setattr(kb_archive, "connect_ro", lambda p: _Dummy())
    centroids = {r"p\a.md": [1.0, 0.0], r"p\b.md": [0.0, 1.0]}
    monkeypatch.setattr(M, "_fetch_file_centroid", lambda conn, sid: centroids.get(sid))
    files = [{"rel": rel, "date_ts": i} for i, rel in enumerate(centroids)]
    assert M.detect_near_dupes(M.hygiene_cfg(), Path("x.db"), files, threshold=0.9, max_proposals=25) == []


class _Dummy:
    def execute(self, *a, **k):  # pragma: no cover - never called (fetch is patched)
        raise AssertionError("should not query")

    def close(self):
        pass


# ── TTL one-offs ──────────────────────────────────────────────────────────────
def _f(rel, *, date, mtime):
    return {"rel": rel, "name": Path(rel).name.lower(),
            "date_ts": datetime.strptime(date, "%Y-%m-%d").timestamp(), "mtime": mtime}


def test_ttl_oneoffs_proposes_old_with_newer_activity():
    files = [
        _f(r"02-F3-Energy\projects\p\_notes\2026-01-01_chrome-agent-x.md", date="2026-01-01", mtime=NOW - 190 * DAY),
        _f(r"02-F3-Energy\projects\p\_notes\2026-01-15_RESUME-PROMPT-a.md", date="2026-01-15", mtime=NOW - 180 * DAY),
        _f(r"02-F3-Energy\projects\p\_notes\2026-02-01_RESUME-PROMPT-b.md", date="2026-02-01", mtime=NOW - 170 * DAY),
        _f(r"02-F3-Energy\projects\p\main.md", date="2026-07-20", mtime=NOW - 1 * DAY),  # recent activity
    ]
    props = M.detect_ttl_oneoffs(M.hygiene_cfg(), files, ttl_days=75, now_ts=NOW, max_proposals=25)
    paths = {p["path"] for p in props}
    assert r"02-F3-Energy\projects\p\_notes\2026-01-01_chrome-agent-x.md" in paths
    assert r"02-F3-Energy\projects\p\_notes\2026-01-15_RESUME-PROMPT-a.md" in paths   # older resume
    assert r"02-F3-Energy\projects\p\_notes\2026-02-01_RESUME-PROMPT-b.md" not in paths  # latest resume kept
    assert r"02-F3-Energy\projects\p\main.md" not in paths                             # not a one-off


def test_ttl_oneoffs_no_newer_activity_not_proposed():
    files = [_f(r"02-F3-Energy\projects\q\_notes\2026-01-01_kickoff.md", date="2026-01-01", mtime=NOW - 190 * DAY)]
    props = M.detect_ttl_oneoffs(M.hygiene_cfg(), files, ttl_days=75, now_ts=NOW, max_proposals=25)
    assert props == []   # nothing newer in the project -> not clearly superseded


def test_ttl_oneoffs_recent_not_proposed():
    files = [
        _f(r"p\_notes\2026-07-01_chrome-agent.md", date="2026-07-01", mtime=NOW - 20 * DAY),  # <75d
        _f(r"p\newer.md", date="2026-07-20", mtime=NOW - 1 * DAY),
    ]
    props = M.detect_ttl_oneoffs(M.hygiene_cfg(), files, ttl_days=75, now_ts=NOW, max_proposals=25)
    assert props == []


# ── resolved decisions-pending ────────────────────────────────────────────────
def test_detect_resolved_pending(tmp_path):
    pending = tmp_path / "decisions-pending.md"
    decisions = tmp_path / "decisions.md"
    pending.write_text(
        "# Pending\n"
        "- P1: Migrate OSN accounting to QuickBooks fully retiring Clover\n"
        "- P0: Something totally unrelated about warehouse pallet racking layout\n",
        encoding="utf-8")
    decisions.write_text(
        "2026-07-01 Migrate OSN accounting to QuickBooks fully retiring Clover\n"
        "2026-06-01 Some other decision about marketing budgets entirely different\n",
        encoding="utf-8")
    props = M.detect_resolved_pending(pending, decisions, jaccard=0.55, max_proposals=25)
    assert len(props) == 1
    assert "OSN accounting" in props[0]["item"]
    assert props[0]["match_jaccard"] >= 0.55


def test_detect_resolved_pending_missing_files_soft(tmp_path):
    assert M.detect_resolved_pending(tmp_path / "nope.md", tmp_path / "nope2.md",
                                     jaccard=0.55, max_proposals=25) == []
