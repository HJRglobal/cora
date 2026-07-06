"""W6-01 Fix-A (D-073) purge: scripts/purge_lex_restricted_kb.py now targets PHI-CONTENT
LBHS/LTS chunks (business KEPT). Seeds rows DIRECTLY via SQL (the ingest drop prevents
seeding through upsert_documents), then exercises the purge helpers against a real schema DB.
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

_PHI = "Client was diagnosed with autism and started on risperidone."
_PHI_BILL = "The client John Smith's BHRF service authorization is still pending."
_BIZ = "LBHS staff payroll and PTO balances for June."
_BIZ_AGG = "BHRF client billing volume rose 12 percent this quarter."

# (chunk_id, source, source_id, entity, sub_entity, content)
_SEED = [
    ("c-g-lbhs-phi", "gmail",       "g1", "LEX", "LEX-LBHS", _PHI),       # default scope, PHI -> purge
    ("c-d-lts-phi",  "drive_sweep", "d1", "LEX", "LEX-LTS",  _PHI_BILL),  # default scope, PHI -> purge
    ("c-g-lbhs-biz", "gmail",       "g2", "LEX", "LEX-LBHS", _BIZ),       # default scope, BUSINESS -> KEEP
    ("c-d-lts-biz",  "drive_sweep", "d2", "LEX", "LEX-LTS",  _BIZ_AGG),   # default scope, aggregate biz -> KEEP
    ("c-g-llc",      "gmail",       "g3", "LEX", "LEX-LLC",  _PHI),       # NOT restricted sub-entity
    ("c-g-gen",      "gmail",       "g4", "LEX", None,       _PHI),       # GM-level (NULL)
    ("c-s-lbhs-phi", "slack",       "s1", "LEX", "LEX-LBHS", _PHI),       # non-default source, PHI
    ("c-m-lbhs-biz", "static_md",   "m1", "LEX", "LEX-LBHS", _BIZ),       # non-default source, business
    ("c-f3e",        "gmail",       "f1", "F3E", None,       _PHI),       # non-LEX
]


@pytest.fixture
def seeded_db(tmp_path):
    db = tmp_path / "kb.db"
    KnowledgeBase(db).close()  # create schema
    conn = schema.connect(db)
    for cid, src, sid, ent, sub, content in _SEED:
        conn.execute(
            "INSERT INTO knowledge_chunks "
            "(chunk_id, source, source_id, entity, sub_entity, title, content, ingested_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (cid, src, sid, ent, sub, f"{cid} title", content, 0),
        )
    conn.commit()
    yield db, conn
    conn.close()


_STAFF: set = set()  # no roster in tests; the seeded PHI names are not staff


def test_default_scope_targets_phi_only(seeded_db):
    _, conn = seeded_db
    ids = purge.target_chunk_ids(conn, purge.RESTRICTED_INGEST_SOURCES, _STAFF)
    # PHI gmail/drive chunks targeted; business (biz) + LLC/general/slack/static_md/F3E excluded
    assert ids == {"c-g-lbhs-phi", "c-d-lts-phi"}


def test_business_chunks_never_targeted_even_all_sources(seeded_db):
    _, conn = seeded_db
    ids = purge.target_chunk_ids(conn, None, _STAFF)
    # every PHI-content LBHS/LTS chunk regardless of source; business chunks stay out
    assert ids == {"c-g-lbhs-phi", "c-d-lts-phi", "c-s-lbhs-phi"}
    assert "c-g-lbhs-biz" not in ids and "c-d-lts-biz" not in ids and "c-m-lbhs-biz" not in ids


def test_include_source_widens_to_phi_only(seeded_db):
    _, conn = seeded_db
    ids = purge.target_chunk_ids(conn, purge.RESTRICTED_INGEST_SOURCES + ("slack",), _STAFF)
    assert ids == {"c-g-lbhs-phi", "c-d-lts-phi", "c-s-lbhs-phi"}


def test_phi_breakdown_counts(seeded_db):
    _, conn = seeded_db
    bd = purge.phi_breakdown(conn, _STAFF)
    # LEX-LBHS gmail: 1 PHI (c-g-lbhs-phi) + 1 business (c-g-lbhs-biz)
    assert bd["total"][("LEX-LBHS", "gmail")] == 2
    assert bd["phi"][("LEX-LBHS", "gmail")] == 1
    # LEX-LTS drive_sweep: 1 PHI + 1 aggregate business
    assert bd["phi"][("LEX-LTS", "drive_sweep")] == 1


def test_non_default_rows_flag_phi_vs_business(seeded_db):
    _, conn = seeded_db
    nd = {(sub, src, is_phi) for sub, src, _t, _sid, is_phi in purge.non_default_rows(conn, _STAFF)}
    assert nd == {("LEX-LBHS", "slack", True), ("LEX-LBHS", "static_md", False)}


def test_backup_then_delete_then_idempotent(seeded_db, tmp_path):
    _, conn = seeded_db
    ids = purge.target_chunk_ids(conn, purge.RESTRICTED_INGEST_SOURCES, _STAFF)
    bak = tmp_path / "purge.bak.jsonl"
    n = purge.backup_rows(conn, ids, bak)
    assert n == 2 and bak.exists()
    lines = [json.loads(x) for x in bak.read_text(encoding="utf-8").splitlines()]
    assert {r["chunk_id"] for r in lines} == {"c-g-lbhs-phi", "c-d-lts-phi"}
    assert all("content" in r and "sub_entity" in r for r in lines)

    totals = purge.delete_chunks(conn, ids)
    assert totals["knowledge_chunks"] == 2

    # idempotent: a second default-scope target is now empty
    assert purge.target_chunk_ids(conn, purge.RESTRICTED_INGEST_SOURCES, _STAFF) == set()
    # business + non-restricted rows untouched (KEPT)
    remaining = {r[0] for r in conn.execute("SELECT chunk_id FROM knowledge_chunks").fetchall()}
    assert {"c-g-lbhs-biz", "c-d-lts-biz", "c-g-llc", "c-g-gen",
            "c-s-lbhs-phi", "c-m-lbhs-biz", "c-f3e"} <= remaining
    assert "c-g-lbhs-phi" not in remaining and "c-d-lts-phi" not in remaining


# ── D-051 re-gate finding 3 (resolved PER-CHUNK): the purge mirrors the per-chunk ingest ──
def _seed_rows(conn, rows):
    for cid, src, sid, sub, content in rows:
        conn.execute(
            "INSERT INTO knowledge_chunks "
            "(chunk_id, source, source_id, entity, sub_entity, title, content, ingested_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (cid, src, sid, "LEX", sub, f"{cid} title", content, 0),
        )
    conn.commit()


def test_multichunk_only_localized_phi_chunk_purged(tmp_path):
    """A multi-chunk doc: ONLY the chunk whose own content carries PHI is purged; the doc's
    BUSINESS/boilerplate sibling chunks are KEPT (per-chunk). This is the property that keeps
    large mixed LBHS/LTS business docs (cash flow / P&L / tracking) from being wholesale
    over-purged -- the verify-first reason whole-doc grouping was rejected."""
    db = tmp_path / "kb.db"
    KnowledgeBase(db).close()
    conn = schema.connect(db)
    _seed_rows(conn, [
        ("m0", "drive_sweep", "d-multi", "LEX-LBHS", "LBHS weekly cash flow cover page."),
        ("m1", "drive_sweep", "d-multi", "LEX-LBHS", "Client Marcus was diagnosed with autism."),  # localized PHI
        ("m2", "drive_sweep", "d-multi", "LEX-LBHS", "Appendix: aggregate program hours by month."),
        ("b0", "gmail",       "b-biz",   "LEX-LBHS", "LBHS payroll batch for June."),               # business
    ])
    ids = purge.target_chunk_ids(conn, None, _STAFF)
    assert ids == {"m1"}                          # ONLY the localized-PHI chunk; business kept
    bd = purge.phi_breakdown(conn, _STAFF)
    assert bd["phi"][("LEX-LBHS", "drive_sweep")] == 1
    assert bd["total"][("LEX-LBHS", "drive_sweep")] == 3   # 2 business chunks retained
    assert bd["phi"][("LEX-LBHS", "gmail")] == 0
    conn.close()


def test_large_business_doc_not_over_purged(tmp_path):
    """The verify-first regression: a large financial spreadsheet (many business chunks) tagged
    LEX-LBHS is NOT purged just because a name + a billing term + 'Lexington' appear in DIFFERENT
    chunks -- per-chunk evaluation keeps each business chunk (whole-doc JOIN would over-purge all)."""
    db = tmp_path / "kb.db"
    KnowledgeBase(db).close()
    conn = schema.connect(db)
    _seed_rows(conn, [
        ("f0", "drive_sweep", "cashflow", "LEX-LBHS", "Weekly Cash Flow Standing ACTUAL - Lexington entities."),
        ("f1", "drive_sweep", "cashflow", "LEX-LBHS", "Row: beginning cash balance and billing receipts."),
        ("f2", "drive_sweep", "cashflow", "LEX-LBHS", "Prepared by Justin Gilmore; reviewed weekly."),  # a name, elsewhere
        ("f3", "drive_sweep", "cashflow", "LEX-LBHS", "Row: ending cash balance and net working capital."),
    ])
    # None of the business chunks carries LOCAL client PHI -> nothing purged.
    assert purge.target_chunk_ids(conn, None, _STAFF) == set()
    conn.close()


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
