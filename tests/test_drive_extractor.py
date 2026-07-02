"""Tests for src/cora/connectors/drive_extractor.py — Builds 2 & 3.

45 tests across 10 classes.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from cora.connectors.drive_extractor import (
    _fact_id,
    _store_facts,
    ensure_facts_table,
    extract_facts_for_file,
    run_extraction,
    run_proposal_loop,
    _build_proposed_action,
    _WATERMARK_EXTRACT,
    _WATERMARK_PROPOSE,
)


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture()
def tmp_db(tmp_path: Path) -> Path:
    """Create a minimal cora_kb.db with required tables."""
    db = tmp_path / "cora_kb.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""
        CREATE TABLE knowledge_chunks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            source      TEXT NOT NULL,
            source_id   TEXT NOT NULL,
            entity      TEXT,
            sub_entity  TEXT,
            content     TEXT,
            metadata    TEXT DEFAULT '{}',
            ingested_at INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE sync_state (
            source              TEXT PRIMARY KEY,
            last_sync_at        INTEGER NOT NULL,
            last_source_modified INTEGER
        )
    """)
    conn.commit()
    conn.close()
    # Also create the facts table
    ensure_facts_table(db)
    return db


def _seed_chunk(
    db: Path,
    source_id: str,
    entity: str,
    content: str,
    sub_entity: str | None = None,
    source: str = "drive_sweep",
    ingested_at: int | None = None,
    filename: str = "test.pdf",
) -> None:
    if ingested_at is None:
        ingested_at = int(time.time())
    meta = json.dumps({"filename": filename})
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO knowledge_chunks (source, source_id, entity, sub_entity, content, metadata, ingested_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (source, source_id, entity, sub_entity, content, meta, ingested_at),
    )
    conn.commit()
    conn.close()


def _seed_fact(
    db: Path,
    fact_id: str,
    source_id: str,
    entity: str,
    fact_type: str,
    subject: str,
    detail: str,
    confidence: str = "HIGH",
    extracted_at: int | None = None,
) -> None:
    if extracted_at is None:
        extracted_at = int(time.time())
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        INSERT INTO drive_extracted_facts
            (fact_id, source_id, entity, sub_entity, fact_type, subject, detail, confidence, extracted_at, metadata)
        VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, '{}')
        """,
        (fact_id, source_id, entity, fact_type, subject, detail, confidence, extracted_at),
    )
    conn.commit()
    conn.close()


def _make_anthropic_client(
    facts: list[dict],
    raise_exc: Exception | None = None,
) -> MagicMock:
    """Return a mock Anthropic client that returns the given facts list."""
    client = MagicMock()
    if raise_exc is not None:
        client.messages.create.side_effect = raise_exc
    else:
        payload = json.dumps({"facts": facts})
        msg = SimpleNamespace(content=[SimpleNamespace(text=payload)])
        client.messages.create.return_value = msg
    return client


# ── Class 1: ensure_facts_table ────────────────────────────────────────────────

class TestEnsureFactsTable:
    def test_creates_table(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE sync_state (source TEXT PRIMARY KEY, last_sync_at INTEGER, last_source_modified INTEGER)"
        )
        conn.commit()
        conn.close()
        ensure_facts_table(db)
        conn = sqlite3.connect(str(db))
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        conn.close()
        assert "drive_extracted_facts" in tables

    def test_idempotent(self, tmp_db: Path) -> None:
        """Calling twice does not raise."""
        ensure_facts_table(tmp_db)
        ensure_facts_table(tmp_db)


# ── Class 2: _fact_id ──────────────────────────────────────────────────────────

class TestFactId:
    def test_deterministic(self) -> None:
        a = _fact_id("src1", "decision", "Launch F3 Pure")
        b = _fact_id("src1", "decision", "Launch F3 Pure")
        assert a == b

    def test_case_insensitive_subject(self) -> None:
        a = _fact_id("src1", "decision", "Launch F3 Pure")
        b = _fact_id("src1", "decision", "launch f3 pure")
        assert a == b

    def test_different_source_ids(self) -> None:
        a = _fact_id("src1", "decision", "Launch F3 Pure")
        b = _fact_id("src2", "decision", "Launch F3 Pure")
        assert a != b

    def test_different_fact_types(self) -> None:
        a = _fact_id("src1", "decision", "Pure")
        b = _fact_id("src1", "company", "Pure")
        assert a != b


# ── Class 3: _store_facts ─────────────────────────────────────────────────────

class TestStoreFacts:
    def test_stores_valid_facts(self, tmp_db: Path) -> None:
        facts = [
            {"fact_type": "decision", "subject": "Launch 6/15", "detail": "Pure launches 6/15/2026.", "confidence": "HIGH"},
        ]
        stored = _store_facts(facts, "file1", "F3E", None, tmp_db)
        assert stored >= 1

    def test_upsert_updates_existing(self, tmp_db: Path) -> None:
        facts = [{"fact_type": "decision", "subject": "Launch", "detail": "Old detail.", "confidence": "HIGH"}]
        _store_facts(facts, "file1", "F3E", None, tmp_db)
        facts2 = [{"fact_type": "decision", "subject": "Launch", "detail": "New detail.", "confidence": "MED"}]
        _store_facts(facts2, "file1", "F3E", None, tmp_db)
        conn = sqlite3.connect(str(tmp_db))
        row = conn.execute("SELECT detail, confidence FROM drive_extracted_facts LIMIT 1").fetchone()
        conn.close()
        assert row[0] == "New detail."
        assert row[1] == "MED"

    def test_skips_incomplete_facts(self, tmp_db: Path) -> None:
        facts = [
            {"fact_type": "decision", "subject": "", "detail": "Missing subject.", "confidence": "HIGH"},
            {"fact_type": "", "subject": "F3 Pure", "detail": "Missing type.", "confidence": "HIGH"},
        ]
        stored = _store_facts(facts, "file1", "F3E", None, tmp_db)
        assert stored == 0

    def test_empty_facts_returns_zero(self, tmp_db: Path) -> None:
        assert _store_facts([], "file1", "F3E", None, tmp_db) == 0

    def test_normalizes_invalid_confidence(self, tmp_db: Path) -> None:
        facts = [{"fact_type": "decision", "subject": "X", "detail": "Y.", "confidence": "INVALID"}]
        _store_facts(facts, "file1", "F3E", None, tmp_db)
        conn = sqlite3.connect(str(tmp_db))
        row = conn.execute("SELECT confidence FROM drive_extracted_facts LIMIT 1").fetchone()
        conn.close()
        assert row[0] == "MED"


# ── Class 4: extract_facts_for_file — PHI + length guards ─────────────────────

class TestExtractFactsForFileGuards:
    def test_skips_lex_entities(self) -> None:
        client = _make_anthropic_client([{"fact_type": "person", "subject": "Someone", "detail": "X", "confidence": "HIGH"}])
        result = extract_facts_for_file(client, "src1", "doc.pdf", "LEX", None, "some content with enough text to pass the length check here")
        assert result == []
        client.messages.create.assert_not_called()

    def test_skips_lex_sub_entities(self) -> None:
        client = _make_anthropic_client([])
        result = extract_facts_for_file(client, "src1", "doc.pdf", "LEX-LLC", None, "some content with enough text to pass the length check here")
        assert result == []

    def test_skips_short_content(self) -> None:
        client = _make_anthropic_client([])
        result = extract_facts_for_file(client, "src1", "doc.pdf", "F3E", None, "short")
        assert result == []
        client.messages.create.assert_not_called()

    def test_skips_visibility_cpa_content(self) -> None:
        client = _make_anthropic_client([])
        content = "Hayden Greber sent the Q3 financial report with all the details about the OSN accounts and distributions."
        result = extract_facts_for_file(client, "src1", "doc.pdf", "OSN", None, content)
        assert result == []

    def test_handles_haiku_api_error(self) -> None:
        client = _make_anthropic_client([], raise_exc=RuntimeError("API down"))
        content = "F3 Energy signed a distribution agreement with ADF for Q1 2026 totaling $125,000 in committed orders across 4 SKUs."
        result = extract_facts_for_file(client, "src1", "doc.pdf", "F3E", None, content)
        assert result == []

    def test_handles_non_json_response(self) -> None:
        client = MagicMock()
        client.messages.create.return_value = SimpleNamespace(
            content=[SimpleNamespace(text="This is not JSON at all.")]
        )
        content = "F3 Energy signed a distribution agreement with ADF for Q1 2026 totaling $125,000 in committed orders across 4 SKUs."
        result = extract_facts_for_file(client, "src1", "doc.pdf", "F3E", None, content)
        assert result == []


# ── Class 5: extract_facts_for_file — happy path ──────────────────────────────

class TestExtractFactsForFileHappyPath:
    def test_returns_facts_from_haiku(self) -> None:
        facts = [
            {"fact_type": "deal", "subject": "ADF Distribution", "detail": "Q1 2026 deal.", "confidence": "HIGH"},
            {"fact_type": "amount", "subject": "$125K", "detail": "Committed order value.", "confidence": "MED"},
        ]
        client = _make_anthropic_client(facts)
        content = "F3 Energy signed a distribution agreement with ADF for Q1 2026 totaling $125,000 in committed orders across 4 SKUs."
        result = extract_facts_for_file(client, "src1", "doc.pdf", "F3E", None, content)
        assert len(result) == 2

    def test_filters_visibility_cpa_facts(self) -> None:
        facts = [
            {"fact_type": "person", "subject": "Hayden Greber", "detail": "Sent Q3 report.", "confidence": "HIGH"},
            {"fact_type": "deal", "subject": "OSN Deal", "detail": "New distribution.", "confidence": "HIGH"},
        ]
        client = _make_anthropic_client(facts)
        content = "The OSN distribution agreement was finalized with ABC corp for fiscal year 2026 distribution across southwest region."
        result = extract_facts_for_file(client, "src1", "doc.pdf", "OSN", None, content)
        subjects = [f["subject"] for f in result]
        assert "Hayden Greber" not in subjects
        assert "OSN Deal" in subjects

    def test_truncates_content_to_max(self) -> None:
        client = _make_anthropic_client([])
        long_content = "A" * 5000
        extract_facts_for_file(client, "src1", "doc.pdf", "F3E", None, long_content)
        call_args = client.messages.create.call_args
        user_msg = call_args[1]["messages"][0]["content"]
        # Content in user msg should be truncated
        assert len(user_msg) < 3000


# ── Class 6: run_extraction ────────────────────────────────────────────────────

class TestRunExtraction:
    def test_processes_recent_chunks(self, tmp_db: Path) -> None:
        _seed_chunk(tmp_db, "file1", "F3E",
                    "F3 Energy signed a distribution agreement with ADF for Q1 2026 totaling $125,000 committed.",
                    ingested_at=int(time.time()))
        facts = [{"fact_type": "deal", "subject": "ADF Distribution", "detail": "Q1 2026.", "confidence": "HIGH"}]
        client = _make_anthropic_client(facts)
        stats = run_extraction(client, db_path=tmp_db, lookback_days=7)
        assert stats["files_processed"] >= 1
        assert stats["facts_extracted"] >= 1

    def test_skips_lex_entities(self, tmp_db: Path) -> None:
        _seed_chunk(tmp_db, "lex_file1", "LEX-LLC",
                    "LEX LLC staff meeting notes for the week regarding case management activities.",
                    ingested_at=int(time.time()))
        client = _make_anthropic_client([{"fact_type": "decision", "subject": "X", "detail": "Y", "confidence": "HIGH"}])
        stats = run_extraction(client, db_path=tmp_db, lookback_days=7)
        assert stats["files_processed"] == 0

    def test_skips_old_chunks_without_backfill(self, tmp_db: Path) -> None:
        old_ts = int(time.time()) - 30 * 86400  # 30 days ago
        _seed_chunk(tmp_db, "file_old", "F3E",
                    "Old F3 Energy distribution agreement with ADF for previous year totaling $50,000.",
                    ingested_at=old_ts)
        client = _make_anthropic_client([])
        stats = run_extraction(client, db_path=tmp_db, lookback_days=7)
        assert stats["files_processed"] == 0

    def test_backfill_ignores_watermark(self, tmp_db: Path) -> None:
        # Set a recent watermark
        conn = sqlite3.connect(str(tmp_db))
        conn.execute(
            "INSERT INTO sync_state (source, last_sync_at) VALUES (?, ?)",
            (_WATERMARK_EXTRACT, int(time.time()) - 3600),
        )
        conn.commit()
        conn.close()

        old_ts = int(time.time()) - 10 * 86400
        _seed_chunk(tmp_db, "file_old", "F3E",
                    "F3 Energy distribution agreement with ADF for Q1 2026 totaling $125,000 committed orders.",
                    ingested_at=old_ts)
        client = _make_anthropic_client([{"fact_type": "deal", "subject": "ADF", "detail": "Q1 deal.", "confidence": "HIGH"}])
        stats = run_extraction(client, db_path=tmp_db, lookback_days=30, backfill=True)
        assert stats["files_processed"] >= 1

    def test_dry_run_does_not_write_facts(self, tmp_db: Path) -> None:
        _seed_chunk(tmp_db, "file1", "F3E",
                    "F3 Energy signed a distribution agreement with ADF for Q1 2026 totaling $125,000.",
                    ingested_at=int(time.time()))
        facts = [{"fact_type": "deal", "subject": "ADF", "detail": "Q1 deal.", "confidence": "HIGH"}]
        client = _make_anthropic_client(facts)
        run_extraction(client, db_path=tmp_db, lookback_days=7, dry_run=True)
        conn = sqlite3.connect(str(tmp_db))
        count = conn.execute("SELECT COUNT(*) FROM drive_extracted_facts").fetchone()[0]
        conn.close()
        assert count == 0

    def test_advances_watermark_on_success(self, tmp_db: Path) -> None:
        _seed_chunk(tmp_db, "file1", "F3E",
                    "F3 Energy signed a distribution agreement with ADF for Q1 2026 totaling $125,000.",
                    ingested_at=int(time.time()))
        client = _make_anthropic_client([])
        before = int(time.time())
        run_extraction(client, db_path=tmp_db, lookback_days=7)
        conn = sqlite3.connect(str(tmp_db))
        row = conn.execute(
            "SELECT last_sync_at FROM sync_state WHERE source = ?",
            (_WATERMARK_EXTRACT,),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] >= before

    def test_skips_non_drive_sweep_chunks(self, tmp_db: Path) -> None:
        _seed_chunk(tmp_db, "slack1", "F3E",
                    "F3 Energy Slack message about distribution ADF for Q1 2026 totaling $125,000.",
                    source="slack",
                    ingested_at=int(time.time()))
        client = _make_anthropic_client([{"fact_type": "deal", "subject": "ADF", "detail": "Slack deal.", "confidence": "HIGH"}])
        stats = run_extraction(client, db_path=tmp_db, lookback_days=7)
        assert stats["files_processed"] == 0


# ── Class 7: _build_proposed_action ───────────────────────────────────────────

class TestBuildProposedAction:
    def test_decision(self) -> None:
        result = _build_proposed_action("decision", "F3 Launch", "Set for 6/15.", "F3E", "file1")
        assert "decisions.md" in result
        assert "F3E" in result

    def test_project(self) -> None:
        result = _build_proposed_action("project", "Pure Launch", "Launch project.", "F3E", "file1")
        assert "Asana" in result

    def test_deal(self) -> None:
        result = _build_proposed_action("deal", "ADF Deal", "Q1 deal.", "F3E", "file1")
        assert "HubSpot" in result

    def test_company(self) -> None:
        result = _build_proposed_action("company", "ADF Inc", "Partner.", "F3E", "file1")
        assert "HubSpot" in result

    def test_person(self) -> None:
        result = _build_proposed_action("person", "Tommy", "Sales lead.", "F3E", "file1")
        assert "contact" in result.lower() or "Tommy" in result

    def test_amount(self) -> None:
        result = _build_proposed_action("amount", "$125K", "Order value.", "F3E", "file1")
        assert "125K" in result or "Review" in result


# ── Class 8: run_proposal_loop — guards ───────────────────────────────────────

class TestRunProposalLoopGuards:
    def test_skips_lex_facts(self, tmp_db: Path) -> None:
        _seed_fact(tmp_db, "fact_lex_1", "lex_file", "LEX-LLC", "decision", "Staffing", "Hired 2 new workers.")
        with patch("cora.knowledge_review.propose_update") as mock_kr:
            stats = run_proposal_loop(db_path=tmp_db)
        mock_kr.assert_not_called()
        assert stats["skipped"] >= 1

    def test_skips_amount_fact_type(self, tmp_db: Path) -> None:
        _seed_fact(tmp_db, "fact_amt_1", "file1", "F3E", "amount", "$125K", "Committed order.")
        with patch("cora.knowledge_review.propose_update") as mock_kr:
            stats = run_proposal_loop(db_path=tmp_db)
        mock_kr.assert_not_called()
        assert stats["skipped"] >= 1

    def test_skips_low_confidence(self, tmp_db: Path) -> None:
        _seed_fact(tmp_db, "fact_low_1", "file1", "F3E", "decision", "Launch", "Set for 6/15.", confidence="LOW")
        with patch("cora.knowledge_review.propose_update") as mock_kr:
            stats = run_proposal_loop(db_path=tmp_db)
        mock_kr.assert_not_called()

    def test_dry_run_does_not_call_propose(self, tmp_db: Path) -> None:
        _seed_fact(tmp_db, "fact_dry_1", "file1", "F3E", "decision", "Launch", "Set for 6/15.")
        with patch("cora.knowledge_review.propose_update") as mock_kr:
            stats = run_proposal_loop(db_path=tmp_db, dry_run=True)
        mock_kr.assert_not_called()
        # But should log as would-propose
        assert stats["proposed"] >= 1


# ── Class 9: run_proposal_loop — happy path ───────────────────────────────────

class TestRunProposalLoopHappyPath:
    def test_proposes_high_confidence_decision(self, tmp_db: Path) -> None:
        _seed_fact(tmp_db, "fact_dec_1", "file1", "F3E", "decision", "Pure Launch Date", "Locked for 6/15/2026.")
        with patch("cora.knowledge_review.propose_update") as mock_kr:
            stats = run_proposal_loop(db_path=tmp_db)
        mock_kr.assert_called_once()
        assert stats["proposed"] == 1

    def test_proposes_med_confidence_project(self, tmp_db: Path) -> None:
        _seed_fact(tmp_db, "fact_proj_1", "file1", "OSN", "project", "Inventory Recon", "Quarterly reconciliation.",
                   confidence="MED")
        with patch("cora.knowledge_review.propose_update") as mock_kr:
            stats = run_proposal_loop(db_path=tmp_db)
        assert stats["proposed"] == 1

    def test_advances_watermark_after_proposals(self, tmp_db: Path) -> None:
        _seed_fact(tmp_db, "fact_wp_1", "file1", "F3E", "deal", "ADF Deal", "Q1 2026 distribution.")
        before = int(time.time())
        with patch("cora.knowledge_review.propose_update"):
            run_proposal_loop(db_path=tmp_db)
        conn = sqlite3.connect(str(tmp_db))
        row = conn.execute(
            "SELECT last_sync_at FROM sync_state WHERE source = ?",
            (_WATERMARK_PROPOSE,),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] >= before

    def test_handles_propose_update_error(self, tmp_db: Path) -> None:
        _seed_fact(tmp_db, "fact_err_1", "file1", "F3E", "decision", "Launch", "6/15.")
        with patch("cora.knowledge_review.propose_update", side_effect=RuntimeError("KB down")):
            stats = run_proposal_loop(db_path=tmp_db)
        assert stats["errors"] >= 1


# ── Class 10: integration — extraction -> proposals ────────────────────────────

class TestIntegration:
    def test_extract_then_propose_pipeline(self, tmp_db: Path) -> None:
        """End-to-end: seed chunk -> extract facts -> propose update."""
        _seed_chunk(
            tmp_db, "file_e2e", "F3E",
            "F3 Energy signed a distribution deal with ADF for Q1 2026 totaling $125,000 in committed purchase orders.",
            ingested_at=int(time.time()),
        )
        haiku_facts = [
            {"fact_type": "deal", "subject": "ADF Distribution Q1 2026", "detail": "Signed $125,000 committed order.", "confidence": "HIGH"},
        ]
        client = _make_anthropic_client(haiku_facts)

        # Phase 1: extract
        stats = run_extraction(client, db_path=tmp_db, lookback_days=1)
        assert stats["facts_stored"] >= 1

        # Phase 2: propose
        with patch("cora.knowledge_review.propose_update") as mock_kr:
            pstats = run_proposal_loop(db_path=tmp_db)

        assert pstats["proposed"] >= 1
        mock_kr.assert_called()
        call_kwargs = mock_kr.call_args[1]
        assert "ADF Distribution Q1 2026" in call_kwargs.get("description", "")
        assert "F3E" in call_kwargs.get("description", "")


# ── Class 10: run_proposal_loop — per-run cap (WS17-B item 2) ─────────────────

class TestRunProposalLoopCap:
    """A backfill once proposed ~17k facts in one run. The per-run cap bounds new
    proposals; when it bites we hold the watermark so deferred facts re-run (not
    silently dropped) and idempotency-skipped dups don't count toward the cap."""

    def _seed_n(self, db, n, prefix):
        for i in range(n):
            _seed_fact(db, f"{prefix}{i}", f"src-{prefix}{i}", "FNDR",
                       "person", f"Subject {prefix}{i}", "some factual detail text")

    def test_cap_bites_and_holds_watermark(self, tmp_db, monkeypatch):
        import cora.connectors.drive_extractor as de
        monkeypatch.setattr(de, "_MAX_PROPOSALS_PER_RUN", 2)
        self._seed_n(tmp_db, 4, "f")
        with patch("cora.knowledge_review.propose_update", return_value=True) as mock:
            stats = run_proposal_loop(db_path=tmp_db)
        assert stats["proposed"] == 2
        assert stats["capped"] is True
        assert mock.call_count == 2  # stopped at the cap, didn't scan the rest
        conn = sqlite3.connect(str(tmp_db))
        row = conn.execute("SELECT last_sync_at FROM sync_state WHERE source=?",
                           (_WATERMARK_PROPOSE,)).fetchone()
        conn.close()
        assert row is None  # watermark NOT advanced -> deferred facts re-run next time

    def test_under_cap_advances_watermark(self, tmp_db, monkeypatch):
        import cora.connectors.drive_extractor as de
        monkeypatch.setattr(de, "_MAX_PROPOSALS_PER_RUN", 50)
        self._seed_n(tmp_db, 3, "g")
        with patch("cora.knowledge_review.propose_update", return_value=True):
            stats = run_proposal_loop(db_path=tmp_db)
        assert stats["proposed"] == 3
        assert stats["capped"] is False
        conn = sqlite3.connect(str(tmp_db))
        row = conn.execute("SELECT last_sync_at FROM sync_state WHERE source=?",
                           (_WATERMARK_PROPOSE,)).fetchone()
        conn.close()
        assert row is not None and row[0] > 0  # watermark advanced

    def test_dedup_skips_do_not_count_toward_cap(self, tmp_db, monkeypatch):
        import cora.connectors.drive_extractor as de
        monkeypatch.setattr(de, "_MAX_PROPOSALS_PER_RUN", 2)
        self._seed_n(tmp_db, 4, "h")
        # First two are already-proposed dups (False), then two genuinely new (True).
        with patch("cora.knowledge_review.propose_update",
                   side_effect=[False, False, True, True]):
            stats = run_proposal_loop(db_path=tmp_db)
        assert stats["proposed"] == 2   # only newly-appended count
        assert stats["skipped"] == 2    # the two dups
        # WS-4 cap-order fix: the cap landed exactly on the LAST fact, so
        # nothing was actually deferred -- capped is False and the watermark
        # advances (the old post-propose check held it pointlessly, costing a
        # full re-query round). capped=True is reserved for genuinely
        # unexamined facts (see test_cap_bites_and_holds_watermark).
        assert stats["capped"] is False

    def test_cap_zero_proposes_nothing(self, tmp_db, monkeypatch):
        # WS-4: the old post-propose cap check leaked 1 proposal/run at cap 0.
        import cora.connectors.drive_extractor as de
        monkeypatch.setattr(de, "_MAX_PROPOSALS_PER_RUN", 0)
        self._seed_n(tmp_db, 3, "i")
        with patch("cora.knowledge_review.propose_update", return_value=True) as mock:
            stats = run_proposal_loop(db_path=tmp_db)
        assert stats["proposed"] == 0
        assert stats["capped"] is True
        assert mock.call_count == 0

    def test_pause_gate_short_circuits(self, tmp_db, monkeypatch):
        # WS-4 disposition: DRIVE_EXTRACTOR_PROPOSALS_ENABLED=0 pauses the
        # proposal loop entirely (call-time read, no watermark movement) while
        # extraction stays untouched.
        monkeypatch.setenv("DRIVE_EXTRACTOR_PROPOSALS_ENABLED", "0")
        self._seed_n(tmp_db, 3, "j")
        with patch("cora.knowledge_review.propose_update", return_value=True) as mock:
            stats = run_proposal_loop(db_path=tmp_db)
        assert stats["paused"] is True
        assert stats["proposed"] == 0
        assert mock.call_count == 0
        conn = sqlite3.connect(str(tmp_db))
        row = conn.execute("SELECT last_sync_at FROM sync_state WHERE source=?",
                           (_WATERMARK_PROPOSE,)).fetchone()
        conn.close()
        assert row is None  # watermark untouched -- resume picks up cleanly

    def test_pause_gate_default_is_enabled(self, tmp_db, monkeypatch):
        monkeypatch.delenv("DRIVE_EXTRACTOR_PROPOSALS_ENABLED", raising=False)
        self._seed_n(tmp_db, 1, "k")
        with patch("cora.knowledge_review.propose_update", return_value=True) as mock:
            stats = run_proposal_loop(db_path=tmp_db)
        assert stats.get("paused") is not True
        assert stats["proposed"] == 1
        assert mock.call_count == 1


# == Class 11: run_proposal_loop cross-run forward-progress (WS17-B item 2) =====

class TestRunProposalLoopForwardProgress:
    """Integration test against the REAL propose_update: a capped run defers the
    remainder, and successive runs drain it via idempotency without dropping or
    duplicating any fact. This is the test that would have caught the silent-drop
    risk the adversarial review flagged."""

    def _seed_n(self, db, n, prefix):
        for i in range(n):
            _seed_fact(db, f"{prefix}{i}", f"src-{prefix}{i}", "FNDR",
                       "person", f"Subject {prefix}{i}", "some factual detail text")

    def test_three_runs_drain_seven_facts_no_loss(self, tmp_db, tmp_path, monkeypatch):
        import cora.connectors.drive_extractor as de
        import cora.knowledge_review as kr
        monkeypatch.setattr(de, "_MAX_PROPOSALS_PER_RUN", 3)
        ledger = tmp_path / "proposed.jsonl"
        monkeypatch.setattr(kr, "_PROPOSED_UPDATES_PATH", ledger)

        self._seed_n(tmp_db, 7, "z")  # 7 candidate facts, cap 3

        kr._SEEN_IDS_CACHE = None
        s1 = run_proposal_loop(db_path=tmp_db)      # real propose_update
        assert s1["proposed"] == 3 and s1["capped"] is True

        kr._SEEN_IDS_CACHE = None                    # simulate a fresh process
        s2 = run_proposal_loop(db_path=tmp_db)
        assert s2["proposed"] == 3 and s2["skipped"] == 3 and s2["capped"] is True

        kr._SEEN_IDS_CACHE = None
        s3 = run_proposal_loop(db_path=tmp_db)
        assert s3["proposed"] == 1 and s3["skipped"] == 6 and s3["capped"] is False

        # Exactly 7 distinct facts landed — none dropped, none duplicated.
        ids = {json.loads(l)["update_id"]
               for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()}
        assert len(ids) == 7
        assert all(i.startswith("drive_fact:") for i in ids)
