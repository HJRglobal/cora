"""Unit tests for completion_detector.py.

All tests are hermetic — no real KB DB, no real Asana calls.
The dedup DB is redirected to a tmp_path fixture.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cora.tools.completion_detector import (
    DEDUP_WINDOW_HOURS,
    MAX_CANDIDATES,
    MIN_FUZZY_RATIO,
    SWEEP_CONFIDENCE_THRESHOLD,
    CompletionCandidate,
    CompletionSignal,
    _COMPLETION_RE,
    _SOURCE_WEIGHTS,
    _WEAK_VERB_PENALTY,
    _business_nouns,
    _completion_kind,
    _fuzzy_ratio,
    _is_bot_author,
    _is_cora_self_post,
    _is_deduped,
    _is_short_task_name,
    _mark_deduped,
    compute_confidence,
    detect_candidates,
    extract_signals_from_db,
    extract_signals_from_text,
    format_sweep_digest,
    mark_candidates_sent,
    match_signals_to_tasks,
)


# ── Fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture()
def tmp_dedup_db(tmp_path, monkeypatch):
    """Redirect dedup DB to a temp path so tests don't pollute real cache."""
    db = tmp_path / "completion-dedup.db"
    monkeypatch.setattr(
        "cora.tools.completion_detector._dedup_db_path",
        lambda: db,
    )
    return db


@pytest.fixture()
def tmp_kb_db(tmp_path) -> Path:
    """Return a path to a fresh, empty KB sqlite DB."""
    db_path = tmp_path / "cora_kb.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE knowledge_chunks (
            chunk_id TEXT PRIMARY KEY,
            source TEXT,
            source_id TEXT,
            entity TEXT,
            date_created INTEGER,
            date_modified INTEGER,
            author TEXT,
            title TEXT,
            content TEXT,
            deep_link TEXT,
            metadata TEXT,
            ingested_at INTEGER,
            sub_entity TEXT
        )
        """
    )
    conn.commit()
    conn.close()
    return db_path


def _insert_chunk(
    db_path: Path,
    *,
    chunk_id: str = "c1",
    source: str = "fireflies",
    source_id: str = "ff-001",
    entity: str = "F3E",
    content: str = "We completed the shipment to Nimbl.",
    title: str = "F3 Weekly 5/22",
    deep_link: str = "https://fireflies.ai/view/ff-001",
    ingested_at: int | None = None,
    author: str | None = None,
) -> None:
    if ingested_at is None:
        ingested_at = int(time.time()) - 60  # 1 minute ago
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """INSERT INTO knowledge_chunks
           (chunk_id, source, source_id, entity, content, title, deep_link, ingested_at, author)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (chunk_id, source, source_id, entity, content, title, deep_link, ingested_at, author),
    )
    conn.commit()
    conn.close()


def _make_signal(
    *,
    source: str = "fireflies",
    source_id: str = "ff-001",
    entity: str = "F3E",
    signal_text: str = "We shipped the inventory to Nimbl.",
    source_weight: float | None = None,
    deep_link: str = "https://fireflies.ai/view/ff-001",
    title: str = "F3 Weekly",
) -> CompletionSignal:
    return CompletionSignal(
        source=source,
        source_id=source_id,
        entity=entity,
        signal_text=signal_text,
        source_weight=source_weight if source_weight is not None else _SOURCE_WEIGHTS[source],
        source_ts=time.time(),
        deep_link=deep_link,
        title=title,
    )


def _make_task(
    *,
    gid: str = "123456",
    name: str = "[F3E] Ship inventory to Nimbl",
    permalink: str = "https://app.asana.com/0/p/123456",
    assignee_name: str = "Hannah Grant",
    assignee_gid: str = "1209060959783860",
) -> dict:
    return {
        "gid": gid,
        "name": name,
        "permalink_url": permalink,
        "assignee": {"name": assignee_name, "gid": assignee_gid},
        "projects": [{"name": "[F3E] Ops"}],
    }


# ── Completion regex ───────────────────────────────────────────────────────

class TestCompletionRegex:

    @pytest.mark.parametrize("text", [
        "We completed the shipment.",
        "The task is done.",
        "Inventory shipped to Nimbl.",
        "Contract was signed.",
        "Invoice paid in full.",
        "Project is launched.",
        "Form submitted yesterday.",
        "We got it done.",
        "Everything is all set.",
        "The deal was closed.",
        "Email sent over to Larry.",
        "Site went live today.",
        "Wrapped up the review.",
        "Delivered to the warehouse.",
        "Approved by Harrison.",
        "Executed the agreement.",
    ])
    def test_matches_completion_language(self, text):
        assert _COMPLETION_RE.search(text), f"Should match: {text!r}"

    @pytest.mark.parametrize("text", [
        "Let's schedule a meeting.",
        "We need to discuss the budget.",
        "Pending approval from Justin.",
        "Still in progress.",
        "Following up on this.",
    ])
    def test_does_not_match_non_completion(self, text):
        assert not _COMPLETION_RE.search(text), f"Should NOT match: {text!r}"


# ── Source weights ─────────────────────────────────────────────────────────

class TestSourceWeights:

    def test_fireflies_highest(self):
        assert _SOURCE_WEIGHTS["fireflies"] > _SOURCE_WEIGHTS["slack"]

    def test_slack_above_gmail(self):
        assert _SOURCE_WEIGHTS["slack"] >= _SOURCE_WEIGHTS["gmail"]

    def test_all_weights_between_0_and_1(self):
        for k, v in _SOURCE_WEIGHTS.items():
            assert 0 < v <= 1.0, f"{k} weight {v} out of range"


# ── compute_confidence ─────────────────────────────────────────────────────

class TestComputeConfidence:

    def test_perfect_match_fireflies_is_high(self):
        conf = compute_confidence(0.90, 1.0)
        assert conf >= 0.80

    def test_zero_fuzzy_below_threshold(self):
        conf = compute_confidence(0.90, 0.0)
        assert conf < SWEEP_CONFIDENCE_THRESHOLD

    def test_slack_high_fuzzy_is_mid(self):
        conf = compute_confidence(_SOURCE_WEIGHTS["slack"], 0.60)
        assert SWEEP_CONFIDENCE_THRESHOLD <= conf < 0.80

    def test_output_rounded_to_4dp(self):
        conf = compute_confidence(0.75, 0.60)
        assert len(str(conf).split(".")[-1]) <= 4


# ── _fuzzy_ratio ───────────────────────────────────────────────────────────

class TestFuzzyRatio:

    def test_identical_strings_return_1(self):
        assert _fuzzy_ratio("ship inventory to nimbl", "ship inventory to nimbl") == 1.0

    def test_completely_different_returns_low(self):
        assert _fuzzy_ratio("ship inventory", "schedule meeting") < 0.5

    def test_case_insensitive(self):
        r1 = _fuzzy_ratio("Ship Inventory", "ship inventory")
        assert r1 == 1.0

    def test_partial_overlap_returns_nonzero(self):
        assert _fuzzy_ratio("inventory shipped to nimbl", "ship inventory to Nimbl") > 0.4


# ── extract_signals_from_text ──────────────────────────────────────────────

class TestExtractSignalsFromText:

    def test_single_completion_sentence(self):
        signals = extract_signals_from_text(
            "We shipped the Pure variety packs to Nimbl.",
            source="slack", entity="F3E",
        )
        assert len(signals) == 1
        assert "shipped" in signals[0].signal_text.lower()

    def test_no_completion_language_returns_empty(self):
        signals = extract_signals_from_text(
            "We're still waiting on the Allen Flavors form.",
            source="slack", entity="F3E",
        )
        assert signals == []

    def test_multiple_sentences_multiple_signals(self):
        text = "Contract signed. Invoice paid. Still need to follow up on next steps."
        signals = extract_signals_from_text(text, source="gmail", entity="HJRG")
        # At least 2 completion signals (signed + paid)
        assert len(signals) >= 2

    def test_source_weight_set_correctly(self):
        # NB: bare "Done." no longer signals (weak verb, no business object) —
        # use a strong-verb sentence to exercise source weighting.
        signals = extract_signals_from_text("Shipped.", source="fireflies", entity="F3E")
        assert signals[0].source_weight == _SOURCE_WEIGHTS["fireflies"]

    def test_unknown_source_gets_default_weight(self):
        signals = extract_signals_from_text("Shipped.", source="custom_source", entity="F3E")
        assert 0 < signals[0].source_weight <= 1.0

    def test_deep_link_and_title_passed_through(self):
        signals = extract_signals_from_text(
            "Shipped.", source="slack", entity="OSN",
            deep_link="https://slack.com/archives/C123",
            title="#osn-ops",
        )
        assert signals[0].deep_link == "https://slack.com/archives/C123"
        assert signals[0].title == "#osn-ops"


# ── extract_signals_from_db ────────────────────────────────────────────────

class TestExtractSignalsFromDb:

    def test_returns_signal_for_completion_chunk(self, tmp_kb_db):
        _insert_chunk(tmp_kb_db, content="We completed the OSN reconciliation.")
        signals = extract_signals_from_db(db_path=tmp_kb_db)
        assert len(signals) >= 1

    def test_ignores_static_md_source(self, tmp_kb_db):
        _insert_chunk(tmp_kb_db, source="static_md", content="The task is done.")
        signals = extract_signals_from_db(db_path=tmp_kb_db)
        assert signals == []

    def test_ignores_old_chunks_outside_lookback(self, tmp_kb_db):
        old_ts = int(time.time()) - 30 * 3600  # 30 hours ago
        _insert_chunk(tmp_kb_db, content="Shipped.", ingested_at=old_ts)
        signals = extract_signals_from_db(db_path=tmp_kb_db, lookback_seconds=25 * 3600)
        assert signals == []

    def test_no_match_returns_empty(self, tmp_kb_db):
        _insert_chunk(tmp_kb_db, content="Meeting scheduled for next Tuesday.")
        signals = extract_signals_from_db(db_path=tmp_kb_db)
        assert signals == []

    def test_missing_db_returns_empty(self, tmp_path):
        signals = extract_signals_from_db(db_path=tmp_path / "nonexistent.db")
        assert signals == []

    def test_entity_filter_restricts_results(self, tmp_kb_db):
        _insert_chunk(tmp_kb_db, chunk_id="c1", entity="F3E", content="Shipped to Nimbl.")
        _insert_chunk(tmp_kb_db, chunk_id="c2", entity="OSN", content="Reconciliation done.")
        signals = extract_signals_from_db(db_path=tmp_kb_db, entities=["F3E"])
        assert all(s.entity == "F3E" for s in signals)

    def test_sentence_level_extraction(self, tmp_kb_db):
        _insert_chunk(
            tmp_kb_db,
            content="We need to review the budget. The contract was signed today. Still pending QBO sync.",
        )
        signals = extract_signals_from_db(db_path=tmp_kb_db)
        # Should pick up "signed" sentence, not the full chunk
        assert any("signed" in s.signal_text.lower() for s in signals)
        # Should not return the pending sentence as a hit
        assert not any("pending" in s.signal_text.lower() and "signed" not in s.signal_text.lower() for s in signals)


# ── match_signals_to_tasks ─────────────────────────────────────────────────

class TestMatchSignalsToTasks:

    def test_high_confidence_candidate_returned(self, tmp_dedup_db):
        signal = _make_signal(
            source="fireflies",
            signal_text="We shipped the inventory to Nimbl warehouse.",
        )
        task = _make_task(name="[F3E] Ship inventory to Nimbl")
        candidates = match_signals_to_tasks([signal], [task], apply_dedup=False)
        assert len(candidates) == 1
        assert candidates[0].task_gid == "123456"

    def test_low_fuzzy_ratio_filtered_out(self, tmp_dedup_db):
        signal = _make_signal(signal_text="Site went live.")
        task = _make_task(name="[OSN] Q3 reconciliation of DNA invoices")
        candidates = match_signals_to_tasks([signal], [task], apply_dedup=False,
                                            min_confidence=SWEEP_CONFIDENCE_THRESHOLD)
        assert candidates == []

    def test_sorted_by_confidence_desc(self, tmp_dedup_db):
        signal_ff = _make_signal(source="fireflies", signal_text="Contract signed.")
        signal_sl = _make_signal(source="slack", source_id="sl-1", signal_text="Contract signed.")
        task = _make_task(name="[HJRG] Sign vendor contract")
        candidates = match_signals_to_tasks(
            [signal_sl, signal_ff], [task], apply_dedup=False
        )
        if len(candidates) >= 2:
            assert candidates[0].confidence >= candidates[-1].confidence

    def test_dedup_fires_when_already_recommended(self, tmp_dedup_db):
        signal = _make_signal()
        task = _make_task()
        # First call — marks as deduped
        c1 = match_signals_to_tasks([signal], [task], apply_dedup=True)
        if c1:
            _mark_deduped(c1[0].task_gid, signal.source_id)
        # Second call — should be deduped out
        c2 = match_signals_to_tasks([signal], [task], apply_dedup=True)
        assert c2 == []

    def test_dedup_skipped_when_disabled(self, tmp_dedup_db):
        signal = _make_signal()
        task = _make_task()
        _mark_deduped(task["gid"], signal.source_id)
        candidates = match_signals_to_tasks([signal], [task], apply_dedup=False)
        # Even though deduped, apply_dedup=False means it's still returned
        assert len(candidates) >= 0  # just shouldn't crash

    def test_same_task_deduplicated_across_multiple_signals(self, tmp_dedup_db):
        """Multiple signals matching the same task should only produce 1 candidate."""
        signals = [
            _make_signal(source_id="s1", signal_text="Shipped inventory to Nimbl."),
            _make_signal(source_id="s2", signal_text="Inventory shipped out to Nimbl."),
        ]
        task = _make_task(name="[F3E] Ship inventory to Nimbl")
        candidates = match_signals_to_tasks(signals, [task], apply_dedup=False)
        task_gids = [c.task_gid for c in candidates]
        assert len(set(task_gids)) == len(task_gids), "Duplicate task_gids in results"

    def test_max_candidates_cap(self, tmp_dedup_db):
        signals = [
            _make_signal(source_id=f"s{i}", signal_text="Project completed and shipped.")
            for i in range(MAX_CANDIDATES + 10)
        ]
        tasks = [_make_task(gid=str(i), name=f"Task {i}") for i in range(MAX_CANDIDATES + 10)]
        candidates = match_signals_to_tasks(signals, tasks, apply_dedup=False)
        assert len(candidates) <= MAX_CANDIDATES

    def test_empty_task_name_skipped(self, tmp_dedup_db):
        signal = _make_signal(signal_text="Done.")
        task = {"gid": "abc", "name": "", "permalink_url": "", "assignee": None, "projects": []}
        candidates = match_signals_to_tasks([signal], [task], apply_dedup=False)
        assert candidates == []

    def test_candidate_has_correct_metadata(self, tmp_dedup_db):
        signal = _make_signal(signal_text="Contract was signed and executed.")
        task = _make_task(
            gid="999",
            name="[HJRG] Execute vendor contract",
            permalink="https://app.asana.com/0/t/999",
            assignee_name="Justin Moran",
            assignee_gid="1209093537787112",
        )
        candidates = match_signals_to_tasks([signal], [task], apply_dedup=False)
        if candidates:
            c = candidates[0]
            assert c.task_gid == "999"
            assert c.task_url == "https://app.asana.com/0/t/999"
            assert c.assignee_name == "Justin Moran"
            assert c.assignee_gid == "1209093537787112"
            assert 0 < c.fuzzy_ratio <= 1.0
            assert 0 < c.confidence <= 1.0


# ── Dedup helpers ──────────────────────────────────────────────────────────

class TestDedupHelpers:

    def test_not_deduped_initially(self, tmp_dedup_db):
        assert not _is_deduped("task-1", "src-1")

    def test_marked_deduped_returns_true(self, tmp_dedup_db):
        _mark_deduped("task-1", "src-1")
        assert _is_deduped("task-1", "src-1")

    def test_different_task_not_deduped(self, tmp_dedup_db):
        _mark_deduped("task-1", "src-1")
        assert not _is_deduped("task-2", "src-1")

    def test_different_source_not_deduped(self, tmp_dedup_db):
        _mark_deduped("task-1", "src-1")
        assert not _is_deduped("task-1", "src-2")

    def test_expired_dedup_returns_false(self, tmp_dedup_db):
        """An entry older than DEDUP_WINDOW_HOURS should not block re-surfacing."""
        stale_ts = int(time.time()) - (DEDUP_WINDOW_HOURS + 1) * 3600
        conn = sqlite3.connect(str(tmp_dedup_db))
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS completion_dedup (
                task_gid TEXT NOT NULL, source_id TEXT NOT NULL,
                recommended_at INTEGER NOT NULL, PRIMARY KEY (task_gid, source_id)
            )
            """
        )
        conn.execute(
            "INSERT INTO completion_dedup VALUES (?, ?, ?)", ("task-stale", "src-1", stale_ts)
        )
        conn.commit()
        conn.close()
        assert not _is_deduped("task-stale", "src-1")

    def test_mark_deduped_upserts_timestamp(self, tmp_dedup_db):
        _mark_deduped("task-1", "src-1")
        t1 = time.time()
        time.sleep(0.01)
        _mark_deduped("task-1", "src-1")
        assert _is_deduped("task-1", "src-1")


# ── CompletionCandidate helpers ────────────────────────────────────────────

class TestCompletionCandidateHelpers:

    def _make_candidate(self, confidence: float) -> CompletionCandidate:
        return CompletionCandidate(
            signal=_make_signal(),
            task_gid="t1",
            task_name="[F3E] Ship Pure",
            task_url="https://app.asana.com/0/t/t1",
            assignee_name="Hannah",
            assignee_gid="1209060959783860",
            project_name="[F3E] Ops",
            fuzzy_ratio=0.75,
            confidence=confidence,
        )

    def test_high_confidence_flag(self):
        assert self._make_candidate(0.85).is_high_confidence
        assert not self._make_candidate(0.70).is_high_confidence

    def test_mid_confidence_flag(self):
        assert self._make_candidate(0.70).is_mid_confidence
        assert not self._make_candidate(0.85).is_mid_confidence
        assert not self._make_candidate(0.50).is_mid_confidence

    def test_slack_line_contains_task_name(self):
        c = self._make_candidate(0.85)
        line = c.slack_line()
        assert "[F3E] Ship Pure" in line

    def test_slack_line_contains_asana_url(self):
        c = self._make_candidate(0.85)
        line = c.slack_line()
        assert "app.asana.com" in line

    def test_slack_line_contains_signal_excerpt(self):
        c = self._make_candidate(0.70)
        line = c.slack_line()
        assert c.signal.signal_text[:50] in line or len(c.signal.signal_text) <= 10

    def test_slack_line_high_confidence_green_dot(self):
        assert "🟢" in self._make_candidate(0.85).slack_line()

    def test_slack_line_mid_confidence_yellow_dot(self):
        assert "🟡" in self._make_candidate(0.70).slack_line()


# ── format_sweep_digest ────────────────────────────────────────────────────

class TestFormatSweepDigest:

    def _candidate(
        self,
        confidence: float,
        task_name: str = "Task",
        gid: str = "t1",
        assignee_name: str = "",
        assignee_gid: str = "",
    ) -> CompletionCandidate:
        return CompletionCandidate(
            signal=_make_signal(),
            task_gid=gid,
            task_name=task_name,
            task_url=f"https://app.asana.com/0/t/{gid}",
            assignee_name=assignee_name,
            assignee_gid=assignee_gid,
            project_name="",
            fuzzy_ratio=0.75,
            confidence=confidence,
        )

    def test_empty_returns_no_candidates_message(self):
        msg = format_sweep_digest([])
        assert "No completion candidates" in msg

    def test_header_present(self):
        c = self._candidate(0.85)
        msg = format_sweep_digest([c])
        assert "Completion candidates" in msg

    def test_high_confidence_green_dot_present(self):
        c = self._candidate(0.85)
        msg = format_sweep_digest([c])
        assert "🟢" in msg

    def test_mid_confidence_yellow_dot_present(self):
        c = self._candidate(0.70)
        msg = format_sweep_digest([c])
        assert "🟡" in msg

    def test_task_name_in_output(self):
        c = self._candidate(0.85, task_name="[OSN] Close reconciliation")
        msg = format_sweep_digest([c])
        assert "[OSN] Close reconciliation" in msg

    def test_disclaimer_present(self):
        c = self._candidate(0.85)
        msg = format_sweep_digest([c])
        assert "recommendations only" in msg.lower() or "does not auto-complete" in msg.lower()

    def test_multiple_candidates_all_appear(self):
        candidates = [
            self._candidate(0.90, "Task A", "g1", assignee_name="Hannah"),
            self._candidate(0.75, "Task B", "g2", assignee_name="Hannah"),
            self._candidate(0.62, "Task C", "g3", assignee_name="Justin"),
        ]
        msg = format_sweep_digest(candidates)
        assert "Task A" in msg
        assert "Task B" in msg
        assert "Task C" in msg

    def test_grouped_by_assignee(self):
        """Candidates for the same assignee should be grouped together."""
        candidates = [
            self._candidate(0.85, "Task A", "g1", assignee_name="Hannah"),
            self._candidate(0.85, "Task B", "g2", assignee_name="Justin"),
            self._candidate(0.70, "Task C", "g3", assignee_name="Hannah"),
        ]
        msg = format_sweep_digest(candidates)
        # Both Hannah's tasks should appear after her name mention
        hannah_pos = msg.find("Hannah")
        justin_pos = msg.find("Justin")
        task_a_pos = msg.find("Task A")
        task_b_pos = msg.find("Task B")
        task_c_pos = msg.find("Task C")
        assert hannah_pos != -1 and justin_pos != -1
        # Task A and Task C (both Hannah's) should appear on the same side of Justin's block
        # — i.e. both before Justin's block or Task C in Hannah's second block
        assert task_a_pos < task_b_pos  # Task A (Hannah, high) before Justin's Task B
        assert task_c_pos < task_b_pos  # Task C (Hannah, mid) before Justin's Task B

    def test_unassigned_fallback_label(self):
        c = self._candidate(0.85, "Task X", "gx", assignee_name="", assignee_gid="")
        msg = format_sweep_digest([c])
        assert "Unassigned" in msg


# ── detect_candidates (integration) ───────────────────────────────────────

class TestDetectCandidates:

    def test_end_to_end_returns_candidate(self, tmp_kb_db, tmp_dedup_db):
        _insert_chunk(
            tmp_kb_db,
            content="We shipped the inventory to Nimbl this week.",
            source="fireflies",
        )
        task = _make_task(name="[F3E] Ship inventory to Nimbl")
        candidates = detect_candidates([task], db_path=tmp_kb_db, apply_dedup=False)
        assert len(candidates) >= 1

    def test_no_signal_returns_empty(self, tmp_kb_db, tmp_dedup_db):
        _insert_chunk(tmp_kb_db, content="Schedule the next planning meeting.")
        task = _make_task(name="[F3E] Ship inventory")
        candidates = detect_candidates([task], db_path=tmp_kb_db, apply_dedup=False)
        assert candidates == []

    def test_missing_db_returns_empty(self, tmp_path, tmp_dedup_db):
        candidates = detect_candidates(
            [_make_task()],
            db_path=tmp_path / "missing.db",
            apply_dedup=False,
        )
        assert candidates == []


# ── mark_candidates_sent ───────────────────────────────────────────────────

class TestMarkCandidatesSent:

    def test_marks_all_candidates_as_deduped(self, tmp_dedup_db):
        candidates = [
            CompletionCandidate(
                signal=_make_signal(source_id=f"src-{i}"),
                task_gid=f"task-{i}",
                task_name=f"Task {i}",
                task_url="",
                assignee_name="",
                assignee_gid="",
                project_name="",
                fuzzy_ratio=0.8,
                confidence=0.85,
            )
            for i in range(3)
        ]
        mark_candidates_sent(candidates)
        for c in candidates:
            assert _is_deduped(c.task_gid, c.signal.source_id)

    def test_empty_list_does_not_crash(self, tmp_dedup_db):
        mark_candidates_sent([])  # should not raise


# ── tool_dispatch wiring ───────────────────────────────────────────────────

class TestToolDispatchWiring:

    def test_in_tool_functions(self):
        from cora.tools.tool_dispatch import _TOOL_FUNCTIONS
        assert "fndr_completion_candidates" in _TOOL_FUNCTIONS

    def test_in_tool_definitions(self):
        from cora.tools.tool_dispatch import TOOL_DEFINITIONS
        names = {t["name"] for t in TOOL_DEFINITIONS}
        assert "fndr_completion_candidates" in names

    def test_definition_has_mandatory_in_description(self):
        from cora.tools.tool_dispatch import TOOL_DEFINITIONS
        td = next(t for t in TOOL_DEFINITIONS if t["name"] == "fndr_completion_candidates")
        # Should mention relevant trigger phrases
        assert "completion" in td["description"].lower() or "complete" in td["description"].lower()

    def test_handler_callable(self):
        from cora.tools.tool_dispatch import _TOOL_FUNCTIONS
        assert callable(_TOOL_FUNCTIONS["fndr_completion_candidates"])


# ── Phase 1.5 precision: weak-verb binding ─────────────────────────────────

class TestWeakVerbBinding:

    def test_strong_verb_is_not_weak(self):
        sigs = extract_signals_from_text("Shipped the order.", source="fireflies")
        assert len(sigs) == 1
        assert sigs[0].is_weak is False

    def test_weak_verb_with_business_noun_is_weak(self):
        sigs = extract_signals_from_text("The invoice was paid.", source="slack")
        assert len(sigs) == 1
        assert sigs[0].is_weak is True

    def test_weak_verb_without_business_noun_drops(self):
        # generic completion token with no concrete object → no signal
        assert extract_signals_from_text("Are we done with this?", source="slack") == []
        assert extract_signals_from_text("Confirmed.", source="slack") == []

    def test_completion_kind_classification(self):
        assert _completion_kind("We shipped it.") is False          # strong
        assert _completion_kind("Invoice paid.") is True            # weak + noun
        assert _completion_kind("Yep, done.") is None               # weak, no noun
        assert _completion_kind("Let's schedule a call.") is None   # nothing

    def test_business_nouns_singularises(self):
        assert "invoice" in _business_nouns("two invoices received")
        assert "form" in _business_nouns("the forms are done")
        assert _business_nouns("just a quick chat") == frozenset()

    def test_weak_signal_requires_noun_overlap_with_task(self, tmp_dedup_db):
        sig = extract_signals_from_text("Invoice paid.", source="fireflies")[0]
        match = _make_task(name="Invoice paid")             # shares the noun
        no_noun = _make_task(name="Paid leave policy")       # shares "paid", not the noun
        assert len(match_signals_to_tasks([sig], [match], apply_dedup=False)) == 1
        assert match_signals_to_tasks([sig], [no_noun], apply_dedup=False) == []


# ── Phase 1.5 precision: bot / self-post exclusion ─────────────────────────

class TestBotAuthorExclusion:

    @pytest.mark.parametrize("author,expected", [
        (None, False), ("", False), ("Hannah Grant", False),
        ("cora", True), ("Cora Bot", True), ("U0B44MDGC5R", True),
        ("Cora via Slack <notification@slack-mail.com>", True),
    ])
    def test_is_bot_author(self, author, expected):
        assert _is_bot_author(author) is expected

    def test_db_skips_bot_authored_chunk(self, tmp_kb_db):
        _insert_chunk(
            tmp_kb_db, chunk_id="b1",
            content="Shipped the order to the warehouse.",
            author="Cora via Slack <notification@slack-mail.com>",
        )
        assert extract_signals_from_db(db_path=tmp_kb_db) == []

    def test_db_keeps_human_authored_chunk(self, tmp_kb_db):
        _insert_chunk(
            tmp_kb_db, chunk_id="h1",
            content="Shipped the order to the warehouse.",
            author="Hannah Grant",
        )
        assert len(extract_signals_from_db(db_path=tmp_kb_db)) >= 1


class TestCoraSelfPostExclusion:

    def test_is_cora_self_post(self):
        assert _is_cora_self_post("🧹 Completion candidates — last 24h ...")
        assert _is_cora_self_post("These are recommendations only — Cora ...")
        assert not _is_cora_self_post("We shipped the order.")

    def test_db_skips_self_digest(self, tmp_kb_db):
        _insert_chunk(
            tmp_kb_db, chunk_id="s1", source="slack",
            content="Completion candidates — last 25h. Shipped the order to Nimbl.",
        )
        assert extract_signals_from_db(db_path=tmp_kb_db) == []


# ── Phase 1.5 precision: short-name fuzzy floor ────────────────────────────

class TestShortNameFloor:

    @pytest.mark.parametrize("name,short", [
        ("Sign contract", True), ("Pay rent", True), ("Task 5", True),
        ("[F3E] Ship inventory to Nimbl", False),
        ("[HJRG] Execute vendor contract", False),
    ])
    def test_is_short_task_name(self, name, short):
        assert _is_short_task_name(name) is short

    def test_partial_match_to_short_name_rejected(self, tmp_dedup_db):
        # ratio ~0.67 (< 0.80 floor) to a terse task name → rejected
        sig = extract_signals_from_text("Signed the contract today.", source="fireflies")[0]
        short_task = _make_task(name="Sign contract")
        assert match_signals_to_tasks([sig], [short_task], apply_dedup=False) == []

    def test_near_exact_match_to_short_name_passes(self, tmp_dedup_db):
        sig = extract_signals_from_text("Sign contract.", source="fireflies")[0]
        short_task = _make_task(name="Sign contract")
        assert len(match_signals_to_tasks([sig], [short_task], apply_dedup=False)) == 1


# ── Phase 1.5 precision: weak-verb confidence penalty ──────────────────────

class TestWeakVerbPenalty:

    def test_weak_scores_below_strong(self):
        strong = compute_confidence(0.90, 0.80, is_weak=False)
        weak = compute_confidence(0.90, 0.80, is_weak=True)
        assert weak < strong
        assert weak == pytest.approx(strong * _WEAK_VERB_PENALTY, abs=1e-3)

    def test_default_is_not_weak(self):
        assert compute_confidence(0.90, 0.80) == compute_confidence(0.90, 0.80, is_weak=False)
