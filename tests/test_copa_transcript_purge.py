"""Tests for BUILD 2: the NDA'd COPA meeting-transcript purge + forward exclusion.

Covers:
  - kb_exclusions.is_copa_meeting_title (the shared TITLE matcher): hits the COPA
    diligence set, and does NOT match the live-data collisions (Maricopa / copayment
    / copacker / the Chrysler Voyager fleet / the LBHS-ARPA meeting).
  - scripts/purge_copa_transcripts.select_copa_chunks: source-scoped, exact-title,
    never a bare-'copa' LIKE.
  - the forward Fireflies-ingest exclusion in connectors/fireflies_connector.backfill:
    a COPA-titled meeting is dropped, an unrelated one is kept.
"""

from __future__ import annotations

import sqlite3
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

from cora.kb_exclusions import is_copa_meeting_title


# ── the shared title matcher ────────────────────────────────────────────────────
class TestMatcher:
    @pytest.mark.parametrize("title", [
        "Virtual Voyager/Copa Model Discussion",
        "LBHS COPA",
        "LBHS COPA summary 2026 05 15T21 58 11.000Z",
        "Copa Model Discussion",
        "Copa Health CFO/COO re Mesa Voyager Center",
        "copa-bhrf review",
        "COPA diligence sync",
    ])
    def test_matches_copa_set(self, title):
        assert is_copa_meeting_title(title) is True

    @pytest.mark.parametrize("title", [
        "Maricopa County Assessor's Office",
        "Fw: Maricopa County Payment Confirmation",
        "copayment plan review",
        "Your Next-Level Copacker.",
        "#054 2021 Chrysler Voyager L Minivan",
        "Factory Order Placement - x2 Chrysler Voyagers",
        "7034-Lexington Voyager Proof",
        "LBHS / ARPA meeting",        # LBHS but ARPA (funding), NOT the COPA acquisition
        "F3E Weekly Sales Sync",
        "",
    ])
    def test_does_not_match_collisions(self, title):
        assert is_copa_meeting_title(title) is False

    def test_none_safe(self):
        assert is_copa_meeting_title(None) is False


# ── purge selection ─────────────────────────────────────────────────────────────
def _load_script():
    sys.path.insert(0, str(_REPO / "scripts"))
    import purge_copa_transcripts as m
    return m


def _db(tmp_path):
    dbp = tmp_path / "kb.db"
    conn = sqlite3.connect(dbp)
    conn.execute("CREATE TABLE knowledge_chunks (chunk_id TEXT PRIMARY KEY, source TEXT, source_id TEXT, title TEXT, entity TEXT)")
    rows = [
        ("f1", "fireflies", "t1", "Virtual Voyager/Copa Model Discussion", "FNDR"),
        ("f2", "fireflies", "t1", "Virtual Voyager/Copa Model Discussion", "FNDR"),  # same meeting, 2 chunks
        ("f3", "fireflies", "t2", "LBHS / ARPA meeting", "LEX"),                     # NOT copa
        ("f4", "fireflies", "t3", "F3E Weekly Sync", "F3E"),                         # unrelated
        ("d1", "drive_asset", "fa", "LBHS COPA transcript 2026 05 15", "FNDR"),       # copa, drive copy
        ("d2", "drive_asset", "fb", "Maricopa County Assessor", "LEX"),              # collision, NOT copa
    ]
    conn.executemany("INSERT INTO knowledge_chunks VALUES (?,?,?,?,?)", rows)
    conn.commit()
    return dbp, conn


def test_select_fireflies_scope_only(tmp_path):
    m = _load_script()
    dbp, conn = _db(tmp_path)
    sel = m.select_copa_chunks(conn, ("fireflies",))
    # only the COPA fireflies meeting (2 chunks); ARPA + F3E excluded; drive out of scope
    assert set(sel.keys()) == {("fireflies", "Virtual Voyager/Copa Model Discussion")}
    assert sorted(sel[("fireflies", "Virtual Voyager/Copa Model Discussion")]) == ["f1", "f2"]
    conn.close()


def test_select_drive_scope_flags_copies(tmp_path):
    m = _load_script()
    dbp, conn = _db(tmp_path)
    sel = m.select_copa_chunks(conn, ("drive_asset",))
    assert set(sel.keys()) == {("drive_asset", "LBHS COPA transcript 2026 05 15")}
    assert sel[("drive_asset", "LBHS COPA transcript 2026 05 15")] == ["d1"]
    conn.close()


def test_select_never_matches_collisions(tmp_path):
    m = _load_script()
    dbp, conn = _db(tmp_path)
    sel = m.select_copa_chunks(conn, ("fireflies", "drive_asset"))
    matched_titles = {t for (_s, t) in sel.keys()}
    assert "LBHS / ARPA meeting" not in matched_titles
    assert "Maricopa County Assessor" not in matched_titles
    assert "F3E Weekly Sync" not in matched_titles
    conn.close()


# ── forward ingest exclusion (drive backfill through its real loop) ─────────────
def _ff():
    sys.path.insert(0, str(_REPO / "src"))
    from cora.connectors import fireflies_connector as m
    return m


def test_ingest_exclusion_drops_copa_keeps_unrelated(monkeypatch):
    m = _ff()
    txs = [
        {"id": "c1", "title": "Virtual Voyager/Copa Model Discussion", "date": "2026-06-10", "meeting_attendees": []},
        {"id": "n1", "title": "F3E Weekly Sales Sync", "date": "2026-06-10", "meeting_attendees": []},
    ]
    monkeypatch.setattr(m, "_graphql_query", lambda q, v: {"transcripts": txs if v.get("skip", 0) == 0 else []})
    monkeypatch.setattr(m, "_dedup_transcripts", lambda transcripts, ledger: (transcripts, {}, 0))
    monkeypatch.setattr(m, "_read_dedup_ledger", lambda: {})
    monkeypatch.setattr(m, "_write_dedup_ledger", lambda ledger: None)
    monkeypatch.setattr(m, "_format_transcript_content", lambda t: "some content")
    monkeypatch.setattr(m, "classify_lex_meeting", lambda t: types.SimpleNamespace(is_lex=False, hard_exclude_kb=False, reason=""))
    monkeypatch.setattr(m, "_is_phi_meeting", lambda title, entity: False)
    monkeypatch.setattr(m, "_parse_date", lambda d: 0)
    monkeypatch.setattr(m, "_resolve_participant_slack_ids", lambda a: [])

    docs = list(m.backfill(datetime(2026, 6, 1, tzinfo=timezone.utc)))
    titles = {d.title for d in docs}
    assert "Virtual Voyager/Copa Model Discussion" not in titles   # COPA dropped
    assert "F3E Weekly Sales Sync" in titles                        # unrelated kept
