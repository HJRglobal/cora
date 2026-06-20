"""WS1-DRIVE: purge_cora_internal_kb must also catch Cora build/audit docs ingested
as Drive COPIES (drive_sweep/drive_asset), whose source_id is a Drive file id and
whose filename lives in `title`. The static_md-only purge missed these entirely.

Logic is tested against in-memory plain tables; the SQL is generic over chunk_id,
so no sqlite-vec extension is needed.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import purge_cora_internal_kb as purge  # noqa: E402


def _conn():
    c = sqlite3.connect(":memory:")
    c.execute(
        "CREATE TABLE knowledge_chunks "
        "(chunk_id TEXT PRIMARY KEY, source TEXT, source_id TEXT, title TEXT)"
    )
    c.execute("CREATE TABLE knowledge_vec_bin (chunk_id TEXT)")
    c.execute("CREATE TABLE knowledge_vec_f32 (chunk_id TEXT)")
    return c


def _seed(c):
    rows = [
        # drive_sweep copies -- source_id is a Drive file id, filename in title
        ("d1", "drive_sweep", "1ITRLIX_fileid", "2026-06-16_fndr_cora-rebuild-execution-log.md"),  # TARGETED
        ("d2", "drive_sweep", "1F6FO_fileid", "2026-06-16_fndr_cora-forensic-findings-report.md"),  # TARGETED
        ("d3", "drive_sweep", "1GZ_fileid", "cora-2026-06-06.log"),                                  # TARGETED (log)
        ("d4", "drive_asset", "1aX_fileid", "2026-06-16_fndr_cora-redesign-overhaul-proposal.md"),   # BROAD only
        ("d5", "drive_sweep", "1AX_fileid", "f3-brand-assets-cora-reference.md"),                     # LEGIT -> keep
        ("d6", "drive_sweep", "1zG_fileid", "2026-05-23_lex_cora-wishlist.md"),                       # LEGIT -> keep
        ("d7", "drive_sweep", "1cl_fileid", "02-F3-Energy CLAUDE.md"),                                # LEGIT -> keep
        # a drive_sweep row that happens to carry a real path source_id -> path rule
        ("d8", "drive_sweep", "_shared/projects/cora/design/x.md", "x.md"),                           # path -> purge
        # static_md (matched by source_id path, unchanged behavior)
        ("s1", "static_md", "_shared/projects/cora/CLAUDE.md", "CLAUDE.md"),                          # static path -> purge
        ("s2", "static_md", "02-F3-Energy/CLAUDE.md", "CLAUDE.md"),                                   # legit -> keep
        # other sources must never be scanned by the drive-copy pass
        ("g1", "gmail", "gmail:a@x:1", "Cora mentioned you in #Cora"),                                # keep
        ("k1", "slack", "slack:C0:1", "#cora-build thread 2026-05-27"),                               # keep
    ]
    c.executemany("INSERT INTO knowledge_chunks VALUES (?,?,?,?)", rows)
    c.executemany("INSERT INTO knowledge_vec_bin VALUES (?)", [(r[0],) for r in rows])
    c.executemany("INSERT INTO knowledge_vec_f32 VALUES (?)", [(r[0],) for r in rows])
    c.commit()


def test_targeted_selects_build_docs_and_logs_only():
    c = _conn(); _seed(c)
    ids, names = purge.target_drive_doc_copies(c, broad=False)
    assert set(ids) == {"d1", "d2", "d3", "d8"}        # execution-log, forensic, .log, path
    assert "d4" not in ids                              # proposal is broad-only
    assert "d5" not in ids and "d6" not in ids and "d7" not in ids  # legit spared


def test_broad_adds_ops_docs_but_still_spares_legit():
    c = _conn(); _seed(c)
    ids, _ = purge.target_drive_doc_copies(c, broad=True)
    assert "d4" in ids                                  # redesign-overhaul-proposal now caught
    assert {"d1", "d2", "d3", "d8"} <= set(ids)
    for keep in ("d5", "d6", "d7"):
        assert keep not in ids, keep                    # legit docs still spared


def test_drive_pass_never_touches_gmail_or_slack():
    c = _conn(); _seed(c)
    ids, _ = purge.target_drive_doc_copies(c, broad=True)
    assert "g1" not in ids and "k1" not in ids          # only drive_sweep/drive_asset scanned


def test_static_md_pass_unchanged():
    c = _conn(); _seed(c)
    ids, _ = purge.target_static_md(c)
    assert set(ids) == {"s1"}                           # cora project path; legit F3E CLAUDE spared


def test_delete_chunks_removes_from_all_three_tables():
    c = _conn(); _seed(c)
    drive_ids, _ = purge.target_drive_doc_copies(c, broad=False)
    static_ids, _ = purge.target_static_md(c)
    to_delete = list(drive_ids) + list(static_ids)
    totals = purge.delete_chunks(c, to_delete)
    assert totals["knowledge_chunks"] == len(to_delete)
    assert totals["knowledge_vec_bin"] == len(to_delete)
    assert totals["knowledge_vec_f32"] == len(to_delete)
    # legit rows survive
    remaining = {r[0] for r in c.execute("SELECT chunk_id FROM knowledge_chunks")}
    assert {"d5", "d6", "d7", "s2", "g1", "k1"} <= remaining
    assert not ({"d1", "d2", "d3", "d8", "s1"} & remaining)
