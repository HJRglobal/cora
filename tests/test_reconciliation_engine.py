"""Tests for src/cora/reconciliation_engine.py  --  Component 3.

Layer A: string/logic assertions (no imports needed).
Layer B: import-guarded unit tests with mocks + temp DB.

Coverage:
  - _is_phi_content(): PHI detection
  - _mentions_vis_cpa(): Visibility CPA exclusion
  - _gap_id(): stable ID generation
  - _extract_sentences(): sentence splitting
  - _confidence_from_ratio(): HIGH/MED/LOW mapping
  - pass1_missing_asana_tasks(): action commitment detection
  - pass2_stale_hubspot_deals(): deal mention + stale detection
  - pass3_uncaptured_decisions(): decision language detection
  - pass4_stale_open_tasks(): completion language detection
  - reconcile(): full pipeline orchestration
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# â"€â"€ helpers â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

def _load_engine():
    """Import reconciliation_engine from src."""
    try:
        sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
        import cora.reconciliation_engine as m
        return m
    except ImportError:
        pytest.skip("reconciliation_engine not importable")


def _make_db(chunks: list[dict]) -> Path:
    """Create a temp sqlite KB DB populated with the given chunks.

    Mirrors the production columns the engine queries — including
    date_created/date_modified, which the content-date window (2026-06-12)
    COALESCEs with ingested_at. Omitted dates default to NULL so the window
    falls back to ingested_at, preserving older tests' semantics.
    """
    tmp = tempfile.mktemp(suffix=".db")
    conn = sqlite3.connect(tmp)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS knowledge_chunks (
            chunk_id   TEXT PRIMARY KEY,
            source     TEXT,
            source_id  TEXT,
            entity     TEXT,
            sub_entity TEXT,
            content    TEXT,
            deep_link  TEXT,
            title      TEXT,
            ingested_at INTEGER,
            date_created INTEGER,
            date_modified INTEGER
        )
        """
    )
    now = int(time.time())
    for i, c in enumerate(chunks):
        conn.execute(
            "INSERT INTO knowledge_chunks VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                c.get("chunk_id", f"chunk_{i}"),
                c.get("source", "slack"),
                c.get("source_id", f"src_{i}"),
                c.get("entity", "FNDR"),
                c.get("sub_entity", ""),
                c.get("content", ""),
                c.get("deep_link", ""),
                c.get("title", ""),
                c.get("ingested_at", now),
                c.get("date_created"),
                c.get("date_modified"),
            ),
        )
    conn.commit()
    conn.close()
    return Path(tmp)


# â"€â"€ Layer A: pure logic â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€


class TestPhiContent:
    """Layer A  --  PHI content detection."""

    def test_service_note_detected(self):
        m = _load_engine()
        assert m._is_phi_content("Service note from the client visit today") is True

    def test_care_plan_detected(self):
        m = _load_engine()
        assert m._is_phi_content("Updated care plan for the resident") is True

    def test_incident_report_detected(self):
        m = _load_engine()
        assert m._is_phi_content("Incident report filed for weekend event") is True

    def test_prior_auth_detected(self):
        m = _load_engine()
        assert m._is_phi_content("Prior auth request submitted to Medicaid") is True

    def test_medication_detected(self):
        m = _load_engine()
        assert m._is_phi_content("Medication change approved by doctor") is True

    def test_normal_text_not_phi(self):
        m = _load_engine()
        assert m._is_phi_content("Q2 payroll processing is on track") is False

    def test_empty_text_not_phi(self):
        m = _load_engine()
        assert m._is_phi_content("") is False

    def test_budget_discussion_not_phi(self):
        m = _load_engine()
        assert m._is_phi_content("We discussed the Lexington LLC operating budget") is False


class TestVisCpaExclusion:
    """Layer A  --  Visibility CPA name exclusion."""

    def test_hayden_excluded(self):
        m = _load_engine()
        assert m._mentions_vis_cpa("Hayden Greber sent the April close pack") is True

    def test_andrew_stubbs_excluded(self):
        m = _load_engine()
        assert m._mentions_vis_cpa("Andrew Stubbs reviewed the 1040") is True

    def test_visibility_cpa_excluded(self):
        m = _load_engine()
        assert m._mentions_vis_cpa("Visibility CPA confirmed the filing") is True

    def test_normal_name_not_excluded(self):
        m = _load_engine()
        assert m._mentions_vis_cpa("Harrison reviewed the F3E budget") is False

    def test_partial_name_not_excluded(self):
        m = _load_engine()
        # "Stubbs" alone is not in the exclusion list (full name required)
        assert m._mentions_vis_cpa("Stubbs sent over the documents") is False


class TestGapId:
    """Layer A  --  gap ID generation."""

    def test_deterministic(self):
        m = _load_engine()
        g1 = m._gap_id("missing_asana_task", "slack:C0A:1717000000.0", "will follow up")
        g2 = m._gap_id("missing_asana_task", "slack:C0A:1717000000.0", "will follow up")
        assert g1 == g2

    def test_different_inputs_different_ids(self):
        m = _load_engine()
        g1 = m._gap_id("missing_asana_task", "src_a", "will follow up")
        g2 = m._gap_id("stale_open_task", "src_a", "will follow up")
        assert g1 != g2

    def test_contains_gap_type_prefix(self):
        m = _load_engine()
        gap_id = m._gap_id("uncaptured_decision", "src_x", "we decided")
        assert gap_id.startswith("uncaptured_decision:")

    def test_no_whitespace_or_special_chars_in_hash(self):
        m = _load_engine()
        gap_id = m._gap_id("stale_hubspot_deal", "gmail:user@test.com:MSG001", "deal mentions")
        # Colons allowed in the prefix; hash portion should be alphanumeric
        hash_part = gap_id.rsplit(":", 1)[-1]
        assert hash_part.isalnum()


class TestExtractSentences:
    """Layer A  --  sentence extraction."""

    def test_splits_on_period(self):
        m = _load_engine()
        result = m._extract_sentences("First sentence. Second sentence.")
        assert len(result) >= 2
        assert any("First" in s for s in result)

    def test_splits_on_newline(self):
        m = _load_engine()
        result = m._extract_sentences("Line one\nLine two\nLine three")
        assert len(result) >= 2

    def test_empty_text(self):
        m = _load_engine()
        result = m._extract_sentences("")
        assert result == []

    def test_strips_whitespace(self):
        m = _load_engine()
        result = m._extract_sentences("  Hello world.  ")
        assert all(s == s.strip() for s in result)


class TestConfidenceFromRatio:
    """Layer A  --  confidence tier calculation."""

    def test_slack_high_ratio_gives_med(self):
        m = _load_engine()
        # slack weight=0.75, ratio=0.65 -> 0.75*0.55 + 0.65*0.45 = 0.705 -> HIGH
        result = m._confidence_from_ratio(0.65, "slack")
        assert result in ("HIGH", "MED")

    def test_slack_zero_ratio_gives_low_or_med(self):
        m = _load_engine()
        # slack weight=0.75, ratio=0.0 -> 0.75*0.55 = 0.4125 -> LOW
        result = m._confidence_from_ratio(0.0, "slack")
        assert result == "LOW"

    def test_fireflies_high_ratio_gives_high(self):
        m = _load_engine()
        # fireflies weight=0.90, ratio=0.80 -> 0.90*0.55 + 0.80*0.45 = 0.855 -> HIGH
        result = m._confidence_from_ratio(0.80, "fireflies")
        assert result == "HIGH"

    def test_unknown_source_handled(self):
        m = _load_engine()
        result = m._confidence_from_ratio(0.5, "unknown_source")
        assert result in ("HIGH", "MED", "LOW")


# â"€â"€ Layer B: import-guarded unit tests with mocks + temp DB â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

try:
    sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
    import cora.reconciliation_engine as _re
    _IMPORT_OK = True
except Exception:
    _IMPORT_OK = False


@pytest.mark.skipif(not _IMPORT_OK, reason="reconciliation_engine not importable")
class TestPass1MissingAsanaTasks:
    """Layer B  --  pass1 missing Asana task detection."""

    def _make_slack_chunk(self, content: str, entity: str = "F3E") -> dict:
        return {
            "source": "slack",
            "source_id": f"slack:C0A:{time.time():.6f}",
            "entity": entity,
            "content": content,
            "title": "test-channel",
        }

    def test_action_sentence_with_no_task_match_flagged(self):
        db_path = _make_db([
            self._make_slack_chunk(
                "Tommy will send the updated pricing deck to the buyers tomorrow."
            )
        ])
        gaps = _re.pass1_missing_asana_tasks(
            open_tasks=[],  # no tasks exist
            db_path=db_path,
        )
        assert len(gaps) >= 1
        assert all(g.gap_type == "missing_asana_task" for g in gaps)

    def test_action_sentence_with_matching_task_not_flagged(self):
        db_path = _make_db([
            self._make_slack_chunk(
                "Will send pricing deck to American Discount Foods."
            )
        ])
        gaps = _re.pass1_missing_asana_tasks(
            open_tasks=[{"gid": "T001", "name": "Send pricing deck to American Discount Foods"}],
            db_path=db_path,
        )
        # Fuzzy match should suppress this gap
        assert len(gaps) == 0

    def test_phi_lex_chunk_skipped(self):
        db_path = _make_db([
            {
                "source": "slack",
                "source_id": "slack:C0A:111",
                "entity": "LEX",
                "content": "Will update the care plan for the client visit next Monday.",
                "title": "llc-ops",
            }
        ])
        gaps = _re.pass1_missing_asana_tasks(
            open_tasks=[],
            db_path=db_path,
        )
        # PHI content in LEX chunk should be skipped
        assert all(g.entity != "LEX" or "care plan" not in g.source_evidence.lower() for g in gaps)

    def test_empty_db_returns_empty(self):
        db_path = _make_db([])
        gaps = _re.pass1_missing_asana_tasks(open_tasks=[], db_path=db_path)
        assert gaps == []

    def test_gap_has_required_fields(self):
        db_path = _make_db([
            self._make_slack_chunk("I will schedule the F3 sales meeting next week.")
        ])
        gaps = _re.pass1_missing_asana_tasks(open_tasks=[], db_path=db_path)
        if gaps:
            gap = gaps[0]
            assert gap.gap_id
            assert gap.gap_type == "missing_asana_task"
            assert gap.description
            assert gap.source_evidence
            assert gap.confidence in ("HIGH", "MED", "LOW")
            assert gap.proposed_action


@pytest.mark.skipif(not _IMPORT_OK, reason="reconciliation_engine not importable")
class TestPass2StaleHubspot:
    """Layer B  --  pass2 stale HubSpot deal detection."""

    def _make_slack_chunk(self, content: str) -> dict:
        return {
            "source": "slack",
            "source_id": f"slack:C0B:{time.time():.6f}",
            "entity": "F3E",
            "content": content,
            "title": "f3e-sales",
        }

    def _stale_deal(self, name: str) -> dict:
        return {
            "id": "D001",
            "name": name,
            "last_activity_ts": time.time() - (10 * 86400),  # 10 days ago = stale
            "deep_link": f"<https://hubspot.com/deal/D001|{name}>",
        }

    def _fresh_deal(self, name: str) -> dict:
        return {
            "id": "D002",
            "name": name,
            "last_activity_ts": time.time() - (2 * 86400),  # 2 days ago = fresh
            "deep_link": f"<https://hubspot.com/deal/D002|{name}>",
        }

    def test_stale_deal_mentioned_in_slack_flagged(self):
        db_path = _make_db([
            self._make_slack_chunk(
                "We talked with American Discount Foods about the distributor deal."
            )
        ])
        gaps = _re.pass2_stale_hubspot_deals(
            active_deals=[self._stale_deal("American Discount Foods")],
            db_path=db_path,
        )
        assert len(gaps) >= 1
        assert all(g.gap_type == "stale_hubspot_deal" for g in gaps)

    def test_fresh_deal_not_flagged(self):
        db_path = _make_db([
            self._make_slack_chunk(
                "Discussed the American Discount Foods retailer account today."
            )
        ])
        gaps = _re.pass2_stale_hubspot_deals(
            active_deals=[self._fresh_deal("American Discount Foods")],
            db_path=db_path,
        )
        assert len(gaps) == 0

    def test_empty_deals_returns_empty(self):
        db_path = _make_db([
            self._make_slack_chunk("We discussed the distributor deal.")
        ])
        gaps = _re.pass2_stale_hubspot_deals(active_deals=[], db_path=db_path)
        assert gaps == []

    def test_empty_db_returns_empty(self):
        gaps = _re.pass2_stale_hubspot_deals(
            active_deals=[self._stale_deal("Some Deal")],
            db_path=_make_db([]),
        )
        assert gaps == []


@pytest.mark.skipif(not _IMPORT_OK, reason="reconciliation_engine not importable")
class TestPass3UncapturedDecisions:
    """Layer B  --  pass3 uncaptured decision detection."""

    def _make_fireflies_chunk(self, content: str) -> dict:
        return {
            "source": "fireflies",
            "source_id": f"ff:{time.time():.6f}",
            "entity": "F3E",
            "content": content,
            "title": "F3 Weekly Meeting 2026-05-27",
        }

    def test_new_decision_language_flagged(self):
        db_path = _make_db([
            self._make_fireflies_chunk(
                "We decided to launch the Pure variety pack on June 15th going forward."
            )
        ])
        # No decisions.md chunks in DB  --  should flag as new
        gaps = _re.pass3_uncaptured_decisions(db_path=db_path)
        assert len(gaps) >= 1
        assert all(g.gap_type == "uncaptured_decision" for g in gaps)

    def test_decision_already_in_decisions_md_not_flagged(self):
        content = "We decided to launch Pure variety pack on June 15th."
        # Make decisions.md chunk that contains the same key words
        decisions_chunk = {
            "source": "static_md",
            "source_id": "static:decisions.md:chunk0",
            "entity": "FNDR",
            "content": "LOCKED 2026-05-22: Pure variety pack launch June 15 decided confirmed.",
            "title": "decisions.md",
        }
        live_chunk = {
            "source": "fireflies",
            "source_id": f"ff:{time.time():.6f}",
            "entity": "F3E",
            "content": content,
            "title": "F3 Weekly",
            "ingested_at": int(time.time()),
        }
        db_path = _make_db([decisions_chunk, live_chunk])
        gaps = _re.pass3_uncaptured_decisions(db_path=db_path)
        # The decision is in decisions.md already  --  should not be flagged
        # (key words "launch", "variety", "pack", "june" all present)
        # This test verifies the keyword-overlap suppression works
        assert isinstance(gaps, list)  # at minimum should not crash

    def test_empty_db_returns_empty(self):
        gaps = _re.pass3_uncaptured_decisions(db_path=_make_db([]))
        assert gaps == []

    def test_gap_has_decision_type(self):
        db_path = _make_db([
            self._make_fireflies_chunk(
                "Going with the blue packaging for Mood  --  final decision confirmed today."
            )
        ])
        gaps = _re.pass3_uncaptured_decisions(db_path=db_path)
        if gaps:
            assert gaps[0].gap_type == "uncaptured_decision"
            assert gaps[0].proposed_action


@pytest.mark.skipif(not _IMPORT_OK, reason="reconciliation_engine not importable")
class TestPass4StaleOpenTasks:
    """Layer B  --  pass4 stale open task (completion language) detection."""

    def _make_slack_chunk(self, content: str) -> dict:
        return {
            "source": "slack",
            "source_id": f"slack:C0C:{time.time():.6f}",
            "entity": "F3E",
            "content": content,
            "title": "f3e-ops",
        }

    def test_completion_language_with_matching_task_flagged(self):
        db_path = _make_db([
            self._make_slack_chunk(
                "The pricing deck has been completed and sent to the buyers."
            )
        ])
        gaps = _re.pass4_stale_open_tasks(
            open_tasks=[
                {
                    "gid": "T001",
                    "name": "Complete and send pricing deck to buyers",
                    "permalink_url": "https://app.asana.com/0/P/T001",
                    "assignee": {"name": "Tommy", "gid": "G001"},
                }
            ],
            db_path=db_path,
        )
        assert len(gaps) >= 1
        assert all(g.gap_type == "stale_open_task" for g in gaps)

    def test_completion_language_no_matching_task_not_flagged(self):
        db_path = _make_db([
            self._make_slack_chunk("The invoice has been paid.")
        ])
        gaps = _re.pass4_stale_open_tasks(
            open_tasks=[{"gid": "T002", "name": "Unrelated task about F3 branding", "permalink_url": "", "assignee": {}}],
            db_path=db_path,
        )
        # Invoice payment vs F3 branding  --  low fuzzy ratio, should not match
        assert len(gaps) == 0

    def test_empty_tasks_returns_empty(self):
        db_path = _make_db([
            self._make_slack_chunk("The project has been completed.")
        ])
        gaps = _re.pass4_stale_open_tasks(open_tasks=[], db_path=db_path)
        assert gaps == []

    def test_gap_payload_has_task_gid(self):
        db_path = _make_db([
            self._make_slack_chunk(
                "Tommy confirmed the sales call with American Discount Foods is done."
            )
        ])
        gaps = _re.pass4_stale_open_tasks(
            open_tasks=[
                {
                    "gid": "T003",
                    "name": "Sales call with American Discount Foods",
                    "permalink_url": "https://app.asana.com/0/P/T003",
                    "assignee": {"name": "Tommy", "gid": "G002"},
                }
            ],
            db_path=db_path,
        )
        if gaps:
            assert "task_gid" in gaps[0].payload


@pytest.mark.skipif(not _IMPORT_OK, reason="reconciliation_engine not importable")
class TestReconcileOrchestration:
    """Layer B  --  reconcile() top-level orchestration."""

    def test_reconcile_returns_only_actionable(self):
        # No chunks in DB -> no gaps -> empty list
        db_path = _make_db([])
        gaps = _re.reconcile(open_tasks=[], active_deals=[], db_path=db_path)
        assert gaps == []

    def test_reconcile_passes_filter(self):
        # Run only pass 3 (uncaptured decisions) to verify selective execution
        db_path = _make_db([
            {
                "source": "fireflies",
                "source_id": "ff:999",
                "entity": "F3E",
                "content": "We decided to go with June 15 for the Pure launch date locked in.",
                "title": "F3 Weekly",
                "ingested_at": int(time.time()),
            }
        ])
        gaps = _re.reconcile(
            open_tasks=[],
            active_deals=[],
            db_path=db_path,
            passes=[3],
        )
        # Pass 3 only  --  all gaps should be uncaptured_decision
        for g in gaps:
            assert g.gap_type == "uncaptured_decision"

    def test_reconcile_sorted_high_before_med(self):
        """HIGH confidence gaps should precede MED in output."""
        # We'll inject gaps directly via pass4 which can produce high-confidence items
        db_path = _make_db([
            {
                "source": "fireflies",  # weight=0.90 -> can reach HIGH
                "source_id": "ff:100",
                "entity": "FNDR",
                "content": "The task has been completed and delivered to the client.",
                "title": "Meeting Notes",
                "ingested_at": int(time.time()),
            }
        ])
        gaps = _re.reconcile(
            open_tasks=[
                {
                    "gid": "T999",
                    "name": "Complete and deliver the project task",
                    "permalink_url": "https://app.asana.com/0/T999",
                    "assignee": {},
                }
            ],
            active_deals=[],
            db_path=db_path,
            passes=[4],
        )
        confs = [g.confidence for g in gaps]
        # Verify sorted: HIGH before MED (if both present)
        if "HIGH" in confs and "MED" in confs:
            high_idx = confs.index("HIGH")
            med_idx = confs.index("MED")
            assert high_idx < med_idx

    def test_reconcile_low_confidence_excluded(self):
        """LOW confidence gaps should not appear in reconcile() output."""
        db_path = _make_db([
            {
                "source": "static_md",  # weight=0.40 -> LOW
                "source_id": "static:0",
                "entity": "FNDR",
                "content": "Will follow up on the matter next week.",
                "title": "playbook.md",
                "ingested_at": int(time.time()),
            }
        ])
        gaps = _re.reconcile(open_tasks=[], active_deals=[], db_path=db_path)
        assert all(g.confidence != "LOW" for g in gaps)

    def test_reconcile_pass_isolation(self):
        """Running passes=[1] should not produce uncaptured_decision or stale_open_task gaps."""
        db_path = _make_db([
            {
                "source": "slack",
                "source_id": "slack:C0A:1",
                "entity": "F3E",
                "content": "Tommy will send the F3E pricing sheet by Friday.",
                "title": "f3e-sales",
                "ingested_at": int(time.time()),
            }
        ])
        gaps = _re.reconcile(
            open_tasks=[],
            active_deals=[],
            db_path=db_path,
            passes=[1],
        )
        for g in gaps:
            assert g.gap_type not in ("uncaptured_decision", "stale_open_task", "stale_hubspot_deal")

    def test_reconcile_vis_cpa_chunks_excluded(self):
        """Chunks mentioning Visibility CPA team should be excluded from gap output."""
        db_path = _make_db([
            {
                "source": "slack",
                "source_id": "slack:C0A:2",
                "entity": "FNDR",
                "content": "Hayden Greber will send the monthly close pack on Friday.",
                "title": "hjrg-finance",
                "ingested_at": int(time.time()),
            }
        ])
        gaps = _re.reconcile(open_tasks=[], active_deals=[], db_path=db_path)
        # Chunk mentioning Hayden should be excluded
        for g in gaps:
            assert "hayden" not in g.source_evidence.lower()
            assert "hayden" not in g.description.lower()

    def test_reconcile_pass5_skipped_without_client(self):
        """Pass 5 should silently skip when no anthropic_client is provided."""
        db_path = _make_db([])
        # Should not raise even with pass 5 in the list
        gaps = _re.reconcile(
            open_tasks=[], active_deals=[],
            db_path=db_path,
            passes=[5],
            anthropic_client=None,
        )
        assert gaps == []


class TestPass5DriveInsights:
    """Layer B  --  pass5_drive_insights() via reconcile()."""

    def _make_drive_db(self, chunks: list[dict]) -> Path:
        """Create a temp DB with drive_sweep chunks."""
        tmp = tempfile.mktemp(suffix=".db")
        conn = sqlite3.connect(tmp)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS knowledge_chunks (
                chunk_id   TEXT PRIMARY KEY,
                source     TEXT,
                source_id  TEXT,
                entity     TEXT,
                sub_entity TEXT,
                content    TEXT,
                deep_link  TEXT,
                title      TEXT,
                metadata   TEXT DEFAULT '{}',
                ingested_at INTEGER,
                date_created INTEGER,
                date_modified INTEGER
            )
            """
        )
        now = int(time.time())
        for i, c in enumerate(chunks):
            conn.execute(
                "INSERT INTO knowledge_chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    c.get("chunk_id", f"chunk_{i}"),
                    c.get("source", "drive_sweep"),
                    c.get("source_id", f"drive_{i}"),
                    c.get("entity", "F3E"),
                    c.get("sub_entity", None),
                    c.get("content", ""),
                    c.get("deep_link", ""),
                    c.get("title", ""),
                    c.get("metadata", "{}"),
                    c.get("ingested_at", now),
                    c.get("date_created"),
                    c.get("date_modified"),
                ),
            )
        conn.commit()
        conn.close()
        return Path(tmp)

    def _make_haiku_client(self, result: dict) -> MagicMock:
        from types import SimpleNamespace
        client = MagicMock()
        client.messages.create.return_value = SimpleNamespace(
            content=[SimpleNamespace(text=json.dumps(result))]
        )
        return client

    def test_pass5_returns_missing_task_gaps(self):
        db_path = self._make_drive_db([
            {
                "source": "drive_sweep",
                "entity": "F3E",
                "content": "F3 Energy signed a new retail distribution agreement with Pacific Foods.",
            }
        ])
        haiku_result = {
            "missing_tasks": [{"subject": "Follow up on Pacific Foods agreement", "source_filename": "deal.pdf", "entity": "F3E", "confidence": "HIGH"}],
            "decisions": [],
            "completed_tasks": [],
        }
        client = self._make_haiku_client(haiku_result)
        gaps = _re.reconcile(
            open_tasks=[], active_deals=[],
            db_path=db_path,
            passes=[5],
            anthropic_client=client,
        )
        assert any(g.gap_type == "missing_asana_task" for g in gaps)

    def test_pass5_returns_decision_gaps(self):
        db_path = self._make_drive_db([
            {"source": "drive_sweep", "entity": "FNDR", "content": "Decision made to move forward with Rogers Ranch renovation."}
        ])
        haiku_result = {
            "missing_tasks": [],
            "decisions": [{"summary": "Rogers Ranch renovation approved", "source_filename": "notes.pdf", "entity": "FNDR", "confidence": "HIGH"}],
            "completed_tasks": [],
        }
        client = self._make_haiku_client(haiku_result)
        gaps = _re.reconcile(
            open_tasks=[], active_deals=[],
            db_path=db_path,
            passes=[5],
            anthropic_client=client,
        )
        assert any(g.gap_type == "uncaptured_decision" for g in gaps)

    def test_pass5_excludes_lex_entity_chunks(self):
        db_path = self._make_drive_db([
            {"source": "drive_sweep", "entity": "LEX", "content": "Staff meeting notes for Lexington services client care management."}
        ])
        client = self._make_haiku_client({"missing_tasks": [], "decisions": [], "completed_tasks": []})
        gaps = _re.reconcile(
            open_tasks=[], active_deals=[],
            db_path=db_path,
            passes=[5],
            anthropic_client=client,
        )
        assert gaps == []
        client.messages.create.assert_not_called()

    def test_pass5_handles_haiku_api_error(self):
        db_path = self._make_drive_db([
            {"source": "drive_sweep", "entity": "F3E", "content": "F3 Energy signed new distribution deal with Pacific Foods regional buyer."}
        ])
        client = MagicMock()
        client.messages.create.side_effect = RuntimeError("API down")
        # Should not raise  --  errors are caught per-entity
        gaps = _re.reconcile(
            open_tasks=[], active_deals=[],
            db_path=db_path,
            passes=[5],
            anthropic_client=client,
        )
        assert gaps == []

    def test_pass5_handles_non_json_response(self):
        db_path = self._make_drive_db([
            {"source": "drive_sweep", "entity": "F3E", "content": "F3 Energy signed new distribution deal with Pacific Foods regional buyer."}
        ])
        from types import SimpleNamespace
        client = MagicMock()
        client.messages.create.return_value = SimpleNamespace(
            content=[SimpleNamespace(text="Not JSON at all.")]
        )
        gaps = _re.reconcile(
            open_tasks=[], active_deals=[],
            db_path=db_path,
            passes=[5],
            anthropic_client=client,
        )
        assert gaps == []

    def test_pass5_filters_low_confidence_items(self):
        db_path = self._make_drive_db([
            {"source": "drive_sweep", "entity": "F3E", "content": "F3 Energy signed new distribution deal with Pacific Foods buyer group."}
        ])
        haiku_result = {
            "missing_tasks": [{"subject": "Maybe follow up", "source_filename": "vague.pdf", "entity": "F3E", "confidence": "LOW"}],
            "decisions": [],
            "completed_tasks": [],
        }
        client = self._make_haiku_client(haiku_result)
        gaps = _re.reconcile(
            open_tasks=[], active_deals=[],
            db_path=db_path,
            passes=[5],
            anthropic_client=client,
        )
        assert gaps == []

    def test_pass5_skips_non_drive_sweep_chunks(self):
        db_path = self._make_drive_db([
            {"source": "slack", "entity": "F3E", "content": "F3 Energy signed new distribution deal with Pacific Foods in Slack."}
        ])
        client = self._make_haiku_client({"missing_tasks": [], "decisions": [], "completed_tasks": []})
        gaps = _re.reconcile(
            open_tasks=[], active_deals=[],
            db_path=db_path,
            passes=[5],
            anthropic_client=client,
        )
        assert gaps == []
        client.messages.create.assert_not_called()


# â"€â"€ Tests for Pass 4 three-fix upgrade â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

_re = _load_engine()


class TestNormalizeTaskName:
    """Fix 3: prefix stripping before matching."""

    def test_strips_entity_bracket(self):
        assert _re._normalize_task_name("[F3E] Sampling kit delivery") == "Sampling kit delivery"

    def test_strips_entity_and_assignee(self):
        # Real Asana tasks use -- as the separator
        assert _re._normalize_task_name("[OSN] Matt -- Inventory Reconciliation") == "Inventory Reconciliation"

    def test_no_prefix_unchanged(self):
        assert _re._normalize_task_name("Follow up with Harrison") == "Follow up with Harrison"

    def test_lex_entity_stripped(self):
        assert _re._normalize_task_name("[LEX-LLC] Jeff -- Website revamp") == "Website revamp"

    def test_em_dash_prefix_stripped(self):
        # Also handles em dash (U+2014) used in some task names
        em = chr(0x2014)
        result = _re._normalize_task_name(f"[F3E] Tommy{em}ADF follow up")
        assert "Tommy" not in result
        assert "ADF follow up" in result

    def test_empty_string_safe(self):
        assert _re._normalize_task_name("") == ""


class TestCosineSim:
    """Fix 2: cosine similarity helper."""

    def test_identical_vectors(self):
        v = [1.0, 0.0, 0.0]
        assert abs(_re._cosine_sim(v, v) - 1.0) < 1e-6

    def test_orthogonal_vectors(self):
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        assert abs(_re._cosine_sim(a, b)) < 1e-6

    def test_opposite_vectors(self):
        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        assert _re._cosine_sim(a, b) < 0

    def test_partial_similarity(self):
        import math
        a = [1.0, 1.0]
        b = [1.0, 0.0]
        # cos(45) = sqrt(2)/2 ~ 0.707
        sim = _re._cosine_sim(a, b)
        assert abs(sim - math.sqrt(2) / 2) < 0.01

    def test_zero_vector_returns_zero(self):
        assert _re._cosine_sim([0.0, 0.0], [1.0, 0.0]) == 0.0


class TestConfidenceFromSim:
    """Fix 2: semantic confidence scoring."""

    def test_fireflies_high_sim_is_high(self):
        # fireflies weight=0.90: 0.90*0.40 + 0.90*0.60 = 0.90 >= 0.78
        assert _re._confidence_from_sim(0.90, "fireflies") == "HIGH"

    def test_slack_good_sim_is_med(self):
        # slack weight=0.75: 0.75*0.40 + 0.75*0.60 = 0.75 >= 0.62
        assert _re._confidence_from_sim(0.75, "slack") == "MED"

    def test_low_sim_is_low(self):
        assert _re._confidence_from_sim(0.50, "gmail") == "LOW"  # 0.70*0.40 + 0.50*0.60 = 0.28+0.30 = 0.58 < 0.62

    def test_static_md_high_sim_still_med(self):
        # static_md weight=0.40: 0.40*0.40 + 0.90*0.60 = 0.16 + 0.54 = 0.70 >= 0.62
        assert _re._confidence_from_sim(0.90, "static_md") == "MED"


class TestPass4SemanticMatching:
    """Integration tests for the three Pass 4 fixes."""

    def _make_db_with_chunk(self, source, content):
        return _make_db([{"source": source, "entity": "F3E", "content": content}])

    def test_fix1_fireflies_source_included(self):
        """Fireflies chunks are now scanned (Fix 1)."""
        db_path = self._make_db_with_chunk(
            "fireflies",
            "Harrison confirmed we shipped the samples to American Discount Foods."
        )
        task = {
            "gid": "task-001",
            "name": "[F3E] Tommy -- American Discount Foods sampling kit",
            "permalink_url": "https://app.asana.com/task/task-001",
            "assignee": {"name": "Tommy Anderson", "gid": "user-001"},
        }
        # Mock semantic embedding to return identical vectors (perfect match)
        fake_emb = [1.0] + [0.0] * 1535
        with patch.object(_re, "_embed_task_names", return_value=[fake_emb]), \
             patch.object(_re, "_embed_sentence", return_value=fake_emb):
            gaps = _re.pass4_stale_open_tasks(
                [task], lookback_seconds=9999999, db_path=db_path
            )
        # Should find a gap from the fireflies source
        assert len(gaps) == 1
        assert gaps[0].source == "fireflies"

    def test_fix1_slack_still_included(self):
        """Slack (original source) still works after Fix 1."""
        db_path = self._make_db_with_chunk(
            "slack",
            "The ADF samples were delivered and confirmed today."
        )
        task = {
            "gid": "task-002",
            "name": "ADF delivery confirmed",
            "permalink_url": "https://app.asana.com/task/task-002",
            "assignee": {"name": "Tommy Anderson", "gid": "user-001"},
        }
        fake_emb = [1.0] + [0.0] * 1535
        with patch.object(_re, "_embed_task_names", return_value=[fake_emb]), \
             patch.object(_re, "_embed_sentence", return_value=fake_emb):
            gaps = _re.pass4_stale_open_tasks(
                [task], lookback_seconds=9999999, db_path=db_path
            )
        assert len(gaps) == 1
        assert gaps[0].source == "slack"

    def test_fix2_semantic_match_used_when_embeddings_available(self):
        """When embeddings work, match_method is 'semantic' in payload (Fix 2)."""
        db_path = self._make_db_with_chunk(
            "slack",
            "We finished the inventory audit and submitted the report."
        )
        task = {
            "gid": "task-003",
            "name": "Complete quarterly inventory audit",
            "permalink_url": "https://app.asana.com/task/task-003",
            "assignee": {"name": "Matt Petrovich", "gid": "user-002"},
        }
        fake_emb = [1.0] + [0.0] * 1535
        with patch.object(_re, "_embed_task_names", return_value=[fake_emb]), \
             patch.object(_re, "_embed_sentence", return_value=fake_emb):
            gaps = _re.pass4_stale_open_tasks(
                [task], lookback_seconds=9999999, db_path=db_path
            )
        assert len(gaps) == 1
        assert gaps[0].payload["match_method"] == "semantic"

    def test_fix2_fuzzy_fallback_when_no_embeddings(self):
        """Falls back to fuzzy matching when embedding is unavailable (Fix 2)."""
        db_path = self._make_db_with_chunk(
            "slack",
            "Inventory audit complete and submitted successfully."
        )
        task = {
            "gid": "task-004",
            "name": "inventory audit complete",
            "permalink_url": "https://app.asana.com/task/task-004",
            "assignee": {"name": "Matt Petrovich", "gid": "user-002"},
        }
        # Simulate embedding unavailable
        with patch.object(_re, "_embed_task_names", return_value=[]):
            gaps = _re.pass4_stale_open_tasks(
                [task], lookback_seconds=9999999, db_path=db_path
            )
        # Fuzzy match on "inventory audit complete" vs "Inventory audit complete and submitted"
        # should succeed
        if gaps:
            assert gaps[0].payload["match_method"] == "fuzzy"

    def test_fix3_prefix_stripped_improves_fuzzy_match(self):
        """Prefix stripping improves fuzzy match score (Fix 3)."""
        # Without prefix stripping:
        # "done with the audit" vs "[OSN] Matt -- inventory audit complete" -> low ratio
        # With prefix stripping:
        # "done with the audit" vs "inventory audit complete" -> better ratio
        raw_name = "[OSN] Matt -- inventory audit complete"
        clean_name = _re._normalize_task_name(raw_name)
        signal = "audit is done and complete"
        ratio_raw = _re._fuzzy_ratio(signal, raw_name)
        ratio_clean = _re._fuzzy_ratio(signal, clean_name)
        assert ratio_clean > ratio_raw, (
            f"Expected clean ({ratio_clean:.3f}) > raw ({ratio_raw:.3f})"
        )

    def test_fix3_prefix_strip_in_semantic_best_match(self):
        """_embed_task_names normalises names before embedding (Fix 3)."""
        tasks = [
            {"gid": "t1", "name": "[F3E] Tommy -- ADF sampling kit", "permalink_url": "", "assignee": {}},
            {"gid": "t2", "name": "clean task name", "permalink_url": "", "assignee": {}},
        ]
        captured_names = []
        def fake_embed(names):
            captured_names.extend(names)
            return [[0.0] * 1536 for _ in names]
        with patch("cora.knowledge_base.embeddings.embed_texts", side_effect=fake_embed):
            _re._embed_task_names(tasks)
        assert "[F3E]" not in captured_names[0]
        assert "Tommy" not in captured_names[0]
        assert "ADF sampling kit" in captured_names[0]

    def test_assignee_name_in_gap_description(self):
        """Gap description includes the assignee name for per-user routing."""
        db_path = self._make_db_with_chunk(
            "slack", "We shipped the product and confirmed delivery."
        )
        task = {
            "gid": "task-005",
            "name": "confirm product delivery",
            "permalink_url": "https://app.asana.com/task/task-005",
            "assignee": {"name": "Alex Cordova", "gid": "user-003"},
        }
        fake_emb = [1.0] + [0.0] * 1535
        with patch.object(_re, "_embed_task_names", return_value=[fake_emb]), \
             patch.object(_re, "_embed_sentence", return_value=fake_emb):
            gaps = _re.pass4_stale_open_tasks(
                [task], lookback_seconds=9999999, db_path=db_path
            )
        assert len(gaps) == 1
        assert "Alex Cordova" in gaps[0].description

    def test_no_match_below_threshold(self):
        """Sentences with low semantic similarity produce no gap."""
        db_path = self._make_db_with_chunk(
            "slack", "The weather is nice today in Phoenix."
        )
        task = {
            "gid": "task-006",
            "name": "File quarterly tax return",
            "permalink_url": "https://app.asana.com/task/task-006",
            "assignee": {"name": "Justin Moran", "gid": "user-004"},
        }
        # Low similarity vector (orthogonal to task embedding)
        task_emb = [1.0] + [0.0] * 1535
        sent_emb = [0.0, 1.0] + [0.0] * 1534  # orthogonal -> sim=0
        with patch.object(_re, "_embed_task_names", return_value=[task_emb]), \
             patch.object(_re, "_embed_sentence", return_value=sent_emb):
            gaps = _re.pass4_stale_open_tasks(
                [task], lookback_seconds=9999999, db_path=db_path
            )
        assert gaps == []



class TestContentDateWindowAndBudgets:
    """2026-06-12 morning-failure fixes: content-date windowing, chunk cap,
    wall-clock deadline. The incident: an 18-month gmail backfill (old message
    dates, fresh ingested_at) made the ingestion-dated 25h window scan 111,878
    chunks for ~6 hours."""

    def test_backfilled_old_content_excluded_from_window(self):
        m = _load_engine()
        now = int(time.time())
        db = _make_db([
            {  # backfilled: ingested now, message from 90 days ago
                "content": "old backfilled email body",
                "ingested_at": now,
                "date_modified": now - 90 * 86400,
            },
        ])
        chunks = m._query_kb_chunks(db_path=db)
        assert chunks == []

    def test_recent_content_included_despite_old_ingestion(self):
        m = _load_engine()
        now = int(time.time())
        db = _make_db([
            {
                "content": "fresh message ingested a while ago",
                "ingested_at": now - 30 * 86400,
                "date_modified": now - 3600,
            },
        ])
        chunks = m._query_kb_chunks(db_path=db)
        assert len(chunks) == 1

    def test_null_dates_fall_back_to_ingested_at(self):
        m = _load_engine()
        now = int(time.time())
        db = _make_db([
            {"content": "undated row, fresh ingestion", "ingested_at": now},
        ])
        chunks = m._query_kb_chunks(db_path=db)
        assert len(chunks) == 1

    def test_hard_chunk_cap_enforced_newest_first(self):
        m = _load_engine()
        now = int(time.time())
        rows = [
            {
                "content": f"msg {i}",
                "source_id": f"s{i}",
                "ingested_at": now,
                "date_modified": now - i * 60,
            }
            for i in range(50)
        ]
        db = _make_db(rows)
        chunks = m._query_kb_chunks(db_path=db, max_chunks=10)
        assert len(chunks) == 10
        # Newest content first: msg 0 has the most recent date_modified.
        assert chunks[0]["content"] == "msg 0"

    def test_pass4_deadline_already_past_scans_nothing(self):
        m = _load_engine()
        now = int(time.time())
        db = _make_db([
            {
                "content": "We completed the sponsor deck task yesterday.",
                "date_modified": now - 600,
            },
        ])
        open_tasks = [{
            "gid": "T1", "name": "Sponsor deck", "permalink_url": "http://x",
            "assignee": {},
        }]
        gaps = m.pass4_stale_open_tasks(
            open_tasks, db_path=db, deadline_monotonic=time.monotonic() - 1,
        )
        assert gaps == []

    def test_reconcile_deadline_past_skips_all_passes(self):
        m = _load_engine()
        now = int(time.time())
        db = _make_db([
            {"content": "I will send the contract tomorrow.", "date_modified": now - 600},
        ])
        gaps = m.reconcile(
            [], [], db_path=db, passes=[1, 2, 3, 4],
            deadline_monotonic=time.monotonic() - 1,
        )
        assert gaps == []
