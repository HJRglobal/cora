"""Tests for the efficiency mining pass (friction_mining.py, Org Synthesis Phase 3).

Layer A: source assertions against run_knowledge_review.py / deployment PS1 /
runner script (executor wiring, D-005, dry-run mode).
Layer B: unit tests on synthetic corpora with env-overridden paths and an
injectable embed function (no network, no OpenAI, no Anthropic).
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import friction_mining as fm


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Deterministic keyword embeddings: texts sharing a keyword land on the same
# unit vector; unmatched texts each get a distinct orthogonal-ish vector.
_BUCKETS = ["pricing", "nimbl", "export", "contract", "subscription",
            "schedule", "invoice", "warehouse"]


def _kw_embed(texts: list[str]) -> list[list[float]]:
    dim = len(_BUCKETS) + 64
    out = []
    fallback = len(_BUCKETS)
    for t in texts:
        vec = [0.0] * dim
        for i, kw in enumerate(_BUCKETS):
            if kw in t.lower():
                vec[i] = 1.0
                break
        else:
            vec[fallback % dim] = 1.0
            fallback += 1
        out.append(vec)
    return out


def _chunk(content, entity="F3E", source="slack", days_ago=1.0,
           sub_entity=None, title="thread", source_id="sid"):
    return {
        "source": source, "source_id": source_id, "entity": entity,
        "sub_entity": sub_entity, "content": content, "title": title,
        "ingested_at": int(time.time() - days_ago * 86400),
    }


def _make_db(path: Path, chunks=(), cache_rows=()):
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE knowledge_chunks (
            source TEXT, source_id TEXT, entity TEXT, sub_entity TEXT,
            content TEXT, title TEXT, ingested_at INTEGER, deep_link TEXT
        )""")
    conn.execute("""
        CREATE TABLE semantic_cache (
            cache_id TEXT PRIMARY KEY, entity TEXT, question TEXT,
            embedding BLOB, response TEXT, created_at INTEGER,
            ttl_seconds INTEGER DEFAULT 1800, hit_count INTEGER DEFAULT 0
        )""")
    for c in chunks:
        conn.execute(
            "INSERT INTO knowledge_chunks (source, source_id, entity, sub_entity,"
            " content, title, ingested_at) VALUES (?,?,?,?,?,?,?)",
            (c["source"], c["source_id"], c["entity"], c["sub_entity"],
             c["content"], c["title"], c["ingested_at"]))
    for i, (entity, question, hit_count, created_ago_days) in enumerate(cache_rows):
        conn.execute(
            "INSERT INTO semantic_cache (cache_id, entity, question, embedding,"
            " response, created_at, hit_count) VALUES (?,?,?,?,?,?,?)",
            (f"c{i}", entity, question, b"\x00", "resp",
             int(time.time() - created_ago_days * 86400), hit_count))
    conn.commit()
    conn.close()


@pytest.fixture()
def paths(tmp_path, monkeypatch):
    """Redirect every friction_mining path to tmp."""
    db = tmp_path / "kb.db"
    monkeypatch.setenv("FRICTION_KB_DB_PATH", str(db))
    monkeypatch.setenv("KNOWLEDGE_GAPS_LOG_PATH", str(tmp_path / "gaps.jsonl"))
    monkeypatch.setenv("FRICTION_LEDGER_PATH", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("EFFICIENCY_BACKLOG_PATH", str(tmp_path / "backlog.md"))
    return tmp_path


def _finding(signal_type=fm.SIGNAL_MANUAL_STEPS, entity="F3E",
             representative="every week i export the inventory spreadsheet",
             count=3):
    return fm.FrictionFinding(
        signal_type=signal_type, entity=entity, entities=[entity],
        representative=representative, count=count,
        evidence=[{"excerpt": representative, "title": "t", "source": "slack"}],
    )


def _draft(confidence="HIGH"):
    return {"title": "Automate the weekly inventory export",
            "recommendation": "Build a Make.com scenario for the export.",
            "route": "make_com", "confidence": confidence}


# ---------------------------------------------------------------------------
# Layer B -- corpus loading exclusions
# ---------------------------------------------------------------------------

class TestQueryChunks:
    def test_missing_db_returns_empty(self, paths):
        assert fm.query_chunks() == []

    def test_loads_recent_chunks(self, paths):
        _make_db(paths / "kb.db", chunks=[_chunk("hello team, quick pricing question?")])
        assert len(fm.query_chunks()) == 1

    def test_lex_entity_excluded_at_sql(self, paths):
        _make_db(paths / "kb.db", chunks=[
            _chunk("normal ops content", entity="LEX"),
            _chunk("normal ops content 2", entity="LEX-LLC"),
            _chunk("f3e content", entity="F3E"),
        ])
        chunks = fm.query_chunks()
        assert [c["entity"] for c in chunks] == ["F3E"]

    def test_lex_sub_entity_excluded(self, paths):
        _make_db(paths / "kb.db", chunks=[
            _chunk("content", entity="FNDR", sub_entity="LEX-LLC"),
        ])
        assert fm.query_chunks() == []

    def test_phi_content_dropped_any_entity(self, paths):
        _make_db(paths / "kb.db", chunks=[
            _chunk("we updated the care plan for the new client", entity="F3E"),
            _chunk("clean sentence about pricing", entity="F3E"),
        ])
        chunks = fm.query_chunks()
        assert len(chunks) == 1
        assert "pricing" in chunks[0]["content"]

    def test_visibility_cpa_dropped(self, paths):
        _make_db(paths / "kb.db", chunks=[
            _chunk("Hayden Greber sent the OSN metrics deck", entity="OSN"),
        ])
        assert fm.query_chunks() == []

    def test_lookback_window(self, paths):
        _make_db(paths / "kb.db", chunks=[
            _chunk("old content about pricing", days_ago=20),
            _chunk("new content about pricing", days_ago=2),
        ])
        chunks = fm.query_chunks(lookback_days=14)
        assert len(chunks) == 1
        assert "new" in chunks[0]["content"]


class TestLoadQuestions:
    def test_gap_questions_exclude_lex_and_phi(self, paths):
        gaps = [
            {"ts": "2099-01-01T00:00:00+00:00", "entity": "LEX",
             "question": "what is the rate book rate?", "gap": "x"},
            {"ts": "2099-01-01T00:00:00+00:00", "entity": "F3E",
             "question": "what is the patient diagnosis?", "gap": "x"},
            {"ts": "2099-01-01T00:00:00+00:00", "entity": "F3E",
             "question": "what is the Nimbl address?", "gap": "x"},
        ]
        (paths / "gaps.jsonl").write_text(
            "\n".join(json.dumps(g) for g in gaps) + "\n", encoding="utf-8")
        out = fm.load_gap_questions(lookback_days=10**6)
        assert len(out) == 1
        assert "Nimbl" in out[0]["text"]

    def test_cache_questions_weight_and_lex_exclusion(self, paths):
        _make_db(paths / "kb.db", cache_rows=[
            ("F3E", "what is the nimbl address?", 2, 1),
            ("LEX", "what is the lex thing?", 0, 1),
        ])
        out = fm.load_cache_questions()
        assert len(out) == 1
        assert out[0]["weight"] == 3

    def test_cache_table_absent_ok(self, paths, tmp_path):
        db = tmp_path / "bare.db"
        sqlite3.connect(str(db)).close()
        assert fm.load_cache_questions(db_path=db) == []


# ---------------------------------------------------------------------------
# Layer B -- detectors on synthetic corpora
# ---------------------------------------------------------------------------

class TestRepeatedQuestions:
    def test_three_similar_questions_yield_finding(self, paths):
        chunks = [
            _chunk("Hey, what is our wholesale pricing for the variety pack?"),
            _chunk("Quick one -- can someone share the wholesale pricing again?"),
            _chunk("What pricing do we quote new retail accounts?"),
        ]
        findings = fm.detect_repeated_questions(chunks, [], [], embed_fn=_kw_embed)
        assert len(findings) == 1
        f = findings[0]
        assert f.signal_type == fm.SIGNAL_REPEATED_QUESTION
        assert f.count == 3
        assert f.entity == "F3E"

    def test_two_similar_questions_no_finding(self, paths):
        chunks = [
            _chunk("What is our wholesale pricing for retailers?"),
            _chunk("Can you share the pricing sheet again please?"),
        ]
        assert fm.detect_repeated_questions(chunks, [], [], embed_fn=_kw_embed) == []

    def test_cache_hit_count_adds_weight(self, paths):
        cache = [
            {"text": "what is the nimbl warehouse address?", "entity": "F3E",
             "origin": "cora_cache", "weight": 2},
            {"text": "where is nimbl located?", "entity": "F3E",
             "origin": "cora_cache", "weight": 1},
        ]
        findings = fm.detect_repeated_questions([], [], cache, embed_fn=_kw_embed)
        assert len(findings) == 1
        assert findings[0].count == 3

    def test_quoted_reply_lines_not_counted(self, paths):
        # One genuine ask + two quoted copies of it (email reply chains)
        # must NOT count as 3 occurrences.
        chunks = [
            _chunk("What is our wholesale pricing for retailers?"),
            _chunk("> What is our wholesale pricing for retailers?"),
            _chunk(">> What is our wholesale pricing for retailers?"),
        ]
        assert fm.detect_repeated_questions(chunks, [], [], embed_fn=_kw_embed) == []

    def test_embed_failure_yields_nothing(self, paths):
        def boom(texts):
            raise RuntimeError("no api")
        chunks = [_chunk("What is the pricing?") for _ in range(3)]
        assert fm.detect_repeated_questions(chunks, [], [], embed_fn=boom) == []


class TestManualSteps:
    def test_explicit_weekly_ritual_single_sentence(self, paths):
        chunks = [_chunk(
            "Every week I have to export the inventory counts into the spreadsheet manually.")]
        findings = fm.detect_manual_steps(chunks, embed_fn=_kw_embed)
        assert len(findings) == 1
        assert findings[0].signal_type == fm.SIGNAL_MANUAL_STEPS

    def test_manual_cue_without_recurrence_ignored(self, paths):
        chunks = [_chunk("I exported the inventory spreadsheet this morning.")]
        assert fm.detect_manual_steps(chunks, embed_fn=_kw_embed) == []

    def test_recurrence_without_manual_cue_ignored(self, paths):
        chunks = [_chunk("We meet every week on Monday to talk strategy and goals.")]
        assert fm.detect_manual_steps(chunks, embed_fn=_kw_embed) == []

    def test_two_similar_nonexplicit_rituals_cluster(self, paths):
        chunks = [
            _chunk("As usual I need to export the order data into the report by hand."),
            _chunk("Monthly we export the order numbers and paste into the deck."),
        ]
        findings = fm.detect_manual_steps(chunks, embed_fn=_kw_embed)
        assert len(findings) == 1
        assert findings[0].count == 2


class TestStaleHandoffs:
    def test_old_request_without_followup_flagged(self, paths):
        chunks = [
            _chunk("Can you please send over the signed contract when you get a chance?",
                   days_ago=9),
            _chunk("Totally unrelated chatter about the warehouse move.", days_ago=2),
        ]
        findings = fm.detect_stale_handoffs(chunks, embed_fn=_kw_embed)
        assert len(findings) == 1
        assert findings[0].signal_type == fm.SIGNAL_STALE_HANDOFF

    def test_old_request_with_followup_not_flagged(self, paths):
        chunks = [
            _chunk("Can you please send over the signed contract when you get a chance?",
                   days_ago=9),
            _chunk("Here is the signed contract, sorry for the delay.", days_ago=2),
        ]
        assert fm.detect_stale_handoffs(chunks, embed_fn=_kw_embed) == []

    def test_recent_request_not_flagged(self, paths):
        chunks = [
            _chunk("Can you please send over the signed contract?", days_ago=2),
        ]
        assert fm.detect_stale_handoffs(chunks, embed_fn=_kw_embed) == []


class TestCrossEntityDuplication:
    def test_same_vendor_two_entities_flagged(self, paths):
        chunks = [
            _chunk("We pay the subscription for the scheduling software monthly.",
                   entity="F3E"),
            _chunk("Our subscription invoice for the scheduling tool came in again.",
                   entity="OSN"),
        ]
        findings = fm.detect_cross_entity_duplication(chunks, embed_fn=_kw_embed)
        assert len(findings) == 1
        f = findings[0]
        assert f.entity == "FNDR"
        assert f.entities == ["F3E", "OSN"]

    def test_same_vendor_one_entity_not_flagged(self, paths):
        chunks = [
            _chunk("We pay the subscription monthly.", entity="F3E"),
            _chunk("The subscription renewed again.", entity="F3E"),
        ]
        assert fm.detect_cross_entity_duplication(chunks, embed_fn=_kw_embed) == []

    def test_aggregator_entities_skipped(self, paths):
        chunks = [
            _chunk("We pay the subscription monthly.", entity="FNDR"),
            _chunk("The subscription invoice arrived.", entity="OSN"),
        ]
        assert fm.detect_cross_entity_duplication(chunks, embed_fn=_kw_embed) == []


# ---------------------------------------------------------------------------
# Layer B -- fingerprint ledger dedup (D-030 pattern)
# ---------------------------------------------------------------------------

class TestFingerprintDedup:
    def test_exact_fingerprint_dedup(self, paths):
        f = _finding()
        fm.record_proposal(f, "friction-abc")
        assert fm.is_already_proposed(f, fm.load_ledger())

    def test_paraphrase_dedup_same_signal(self, paths):
        fm.record_proposal(_finding(
            representative="every week i export the inventory spreadsheet"), "u1")
        near = _finding(representative="every week i export the inventory spreadsheets")
        assert fm.is_already_proposed(near, fm.load_ledger())

    def test_different_signal_type_not_dedup(self, paths):
        fm.record_proposal(_finding(signal_type=fm.SIGNAL_MANUAL_STEPS), "u1")
        other = _finding(signal_type=fm.SIGNAL_STALE_HANDOFF)
        assert not fm.is_already_proposed(other, fm.load_ledger())

    def test_fresh_finding_not_dedup(self, paths):
        assert not fm.is_already_proposed(_finding(), fm.load_ledger())


# ---------------------------------------------------------------------------
# Layer B -- fail-closed drafting
# ---------------------------------------------------------------------------

class TestFailClosedDrafting:
    def test_no_api_key_returns_none(self, paths, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert fm.draft_proposal(_finding()) is None

    def _fake_anthropic(self, monkeypatch, response_text=None, raises=False):
        mod = types.ModuleType("anthropic")

        class _Msgs:
            def create(self, **kwargs):
                if raises:
                    raise RuntimeError("api down")
                return types.SimpleNamespace(
                    content=[types.SimpleNamespace(text=response_text)])

        class _Client:
            def __init__(self, api_key=""):
                self.messages = _Msgs()

        mod.Anthropic = _Client
        monkeypatch.setitem(sys.modules, "anthropic", mod)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    def test_api_error_returns_none(self, paths, monkeypatch):
        self._fake_anthropic(monkeypatch, raises=True)
        assert fm.draft_proposal(_finding()) is None

    def test_malformed_json_returns_none(self, paths, monkeypatch):
        self._fake_anthropic(monkeypatch, response_text="not json at all")
        assert fm.draft_proposal(_finding()) is None

    def test_not_worth_proposing_returns_none(self, paths, monkeypatch):
        self._fake_anthropic(monkeypatch, response_text=json.dumps(
            {"worth_proposing": False}))
        assert fm.draft_proposal(_finding()) is None

    def test_good_draft_parsed(self, paths, monkeypatch):
        self._fake_anthropic(monkeypatch, response_text=json.dumps({
            "worth_proposing": True, "title": "Automate export",
            "recommendation": "Use Make.com.", "route": "make_com",
            "confidence": "HIGH"}))
        draft = fm.draft_proposal(_finding())
        assert draft == {"title": "Automate export", "recommendation": "Use Make.com.",
                         "route": "make_com", "confidence": "HIGH"}

    def test_phi_in_draft_rejected(self, paths, monkeypatch):
        self._fake_anthropic(monkeypatch, response_text=json.dumps({
            "worth_proposing": True, "title": "Update the care plan flow",
            "recommendation": "Change the patient intake form.",
            "route": "process_change", "confidence": "HIGH"}))
        assert fm.draft_proposal(_finding()) is None

    def test_invalid_route_and_confidence_normalized(self, paths, monkeypatch):
        self._fake_anthropic(monkeypatch, response_text=json.dumps({
            "worth_proposing": True, "title": "Do the thing",
            "recommendation": "Do it well.", "route": "magic",
            "confidence": "MAYBE"}))
        draft = fm.draft_proposal(_finding())
        assert draft["route"] == "process_change"
        assert draft["confidence"] == "MED"


# ---------------------------------------------------------------------------
# Layer B -- propose_update wiring + cap enforcement + dry run
# ---------------------------------------------------------------------------

class TestProposeWiring:
    def test_propose_efficiency_calls_knowledge_review(self, paths, monkeypatch):
        import cora.knowledge_review as kr
        calls = []
        monkeypatch.setattr(kr, "propose_update", lambda **kw: calls.append(kw))
        update_id = fm.propose_efficiency(_finding(), _draft())
        assert update_id.startswith("friction-")
        assert len(calls) == 1
        kw = calls[0]
        assert kw["update_type"] == "efficiency"
        assert kw["confidence"] == "HIGH"
        assert kw["payload"]["route"] == "make_com"
        assert kw["payload"]["fingerprint"]
        assert "Efficiency finding (F3E)" in kw["description"]

    def _run(self, paths, n_findings, confidences, dry_run=False, max_proposals=5):
        """Drive run_mining with n distinct explicit manual-step rituals.

        Each ritual carries a different _BUCKETS keyword so _kw_embed puts
        every ritual in its own cluster (n distinct findings).
        """
        assert n_findings <= len(_BUCKETS)
        rituals = [
            f"Every week I have to manually update the {_BUCKETS[i]} numbers "
            f"in the report."
            for i in range(n_findings)
        ]
        _make_db(paths / "kb.db", chunks=[_chunk(r) for r in rituals])
        drafts = iter(confidences)
        proposed = []

        def draft_fn(f):
            return _draft(confidence=next(drafts))

        def propose_fn(f, d):
            proposed.append((f, d))
            return f"friction-{len(proposed):012d}"

        summary = fm.run_mining(
            signals={fm.SIGNAL_MANUAL_STEPS}, dry_run=dry_run,
            max_proposals=max_proposals, embed_fn=_kw_embed,
            draft_fn=draft_fn, propose_fn=propose_fn,
        )
        return summary, proposed

    def test_cap_enforcement_high_first(self, paths):
        confidences = ["MED", "MED", "HIGH", "MED", "HIGH", "MED", "HIGH", "MED"]
        summary, proposed = self._run(paths, 8, confidences)
        assert len(proposed) == 5
        assert [d["confidence"] for _, d in proposed][:3] == ["HIGH", "HIGH", "HIGH"]
        assert len(summary["proposed"]) == 5

    def test_dry_run_writes_nothing(self, paths):
        summary, proposed = self._run(paths, 3, ["HIGH"] * 3, dry_run=True)
        assert summary["dry_run"] is True
        assert len(summary["proposed"]) == 3
        assert proposed == []                       # propose_fn never called
        assert not (paths / "ledger.jsonl").exists()  # no ledger writes

    def test_second_run_dedups_via_ledger(self, paths):
        self._run(paths, 3, ["HIGH"] * 3)
        # second run, same corpus
        rituals_summary, proposed2 = self._run(paths, 3, ["HIGH"] * 3)
        assert proposed2 == []
        assert rituals_summary["after_dedup"] == 0

    def test_draft_fn_none_proposes_nothing(self, paths):
        _make_db(paths / "kb.db", chunks=[_chunk(
            "Every week I have to export the inventory spreadsheet manually.")])
        summary = fm.run_mining(
            signals={fm.SIGNAL_MANUAL_STEPS}, embed_fn=_kw_embed,
            draft_fn=lambda f: None, propose_fn=lambda f, d: "x",
        )
        assert summary["proposed"] == []
        assert not (paths / "ledger.jsonl").exists()


# ---------------------------------------------------------------------------
# Layer B -- executor (apply_efficiency)
# ---------------------------------------------------------------------------

class TestApplyEfficiency:
    def _payload(self, title="Automate the export"):
        return {
            "signal_type": fm.SIGNAL_MANUAL_STEPS, "entity": "F3E",
            "title": title, "recommendation": "Build a Make.com scenario.",
            "route": "make_com", "frequency": "observed 3x in the last 14 days",
            "evidence": [{"excerpt": "every week i export", "title": "t",
                          "source": "slack"}],
        }

    def test_creates_backlog_with_header(self, paths):
        ok, summary = fm.apply_efficiency(self._payload())
        assert ok
        text = (paths / "backlog.md").read_text(encoding="utf-8")
        assert text.startswith("# Efficiency Backlog")
        assert "Automate the export" in text
        assert "make_com" in text

    def test_appends_not_overwrites(self, paths):
        fm.apply_efficiency(self._payload("First finding"))
        fm.apply_efficiency(self._payload("Second finding"))
        text = (paths / "backlog.md").read_text(encoding="utf-8")
        assert "First finding" in text and "Second finding" in text
        assert text.count("# Efficiency Backlog") == 1

    def test_dedups_same_day_title(self, paths):
        # D-051 concurrency fix: apply_efficiency is now reachable concurrently
        # (the one-tap button) and could be re-approved -- a same-day/title block
        # must not be duplicated.
        ok1, _ = fm.apply_efficiency(self._payload("Automate the CSV export"))
        ok2, summary2 = fm.apply_efficiency(self._payload("Automate the CSV export"))
        assert ok1 and ok2 and "skipped duplicate" in summary2
        text = (paths / "backlog.md").read_text(encoding="utf-8")
        assert text.count("## [") == 1
        assert text.count("Automate the CSV export") == 1

    def test_never_raises(self, paths, monkeypatch):
        # Point the backlog under an existing FILE so mkdir(parents=True)
        # raises -- platform-safe unwritable target.
        blocker = paths / "blocker"
        blocker.write_text("x", encoding="utf-8")
        monkeypatch.setenv("EFFICIENCY_BACKLOG_PATH",
                           str(blocker / "sub" / "backlog.md"))
        ok, summary = fm.apply_efficiency(self._payload())
        assert ok is False
        assert "apply failed" in summary


# ---------------------------------------------------------------------------
# Layer B -- standalone-script guarantee (no bot-process imports)
# ---------------------------------------------------------------------------

class TestNoBotProcessImport:
    def test_import_does_not_pull_bot_modules(self):
        code = (
            "import sys; sys.path.insert(0, r'%s'); "
            "import cora.friction_mining; "
            "bad = [m for m in ('cora.app', 'cora.tool_dispatch', 'cora.claude_client')"
            " if m in sys.modules]; "
            "assert not bad, f'bot-process modules imported: {bad}'"
        ) % str(_REPO_ROOT / "src")
        result = subprocess.run([sys.executable, "-c", code],
                                capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# Layer A -- source assertions
# ---------------------------------------------------------------------------

class TestSourceWiring:
    def test_knowledge_review_executor_has_efficiency_branch(self):
        src = (_REPO_ROOT / "scripts" / "run_knowledge_review.py").read_text(encoding="utf-8")
        assert 'update_type == "efficiency"' in src
        assert "from cora.friction_mining import apply_efficiency" in src

    def test_runner_script_exists_with_dry_run(self):
        src = (_REPO_ROOT / "scripts" / "run_friction_mining.py").read_text(encoding="utf-8")
        assert "--dry-run" in src
        assert "run_mining" in src

    def test_setup_ps1_doctrine_compliance(self):
        ps1 = (_REPO_ROOT / "deployment" / "setup-friction-mining-task.ps1")
        assert ps1.exists()
        src = ps1.read_text(encoding="utf-8")
        assert r".venv\Scripts\python.exe" in src       # D-005 absolute venv python
        assert "uv run" not in src                       # never uv (D-005)
        assert "Sunday" in src                           # weekly Sunday slot
        assert all(ord(ch) < 128 for ch in src)          # ASCII-only (D-016)

    def test_module_constants(self):
        assert fm.MAX_PROPOSALS_PER_RUN == 5
        assert fm.UPDATE_TYPE_EFFICIENCY == "efficiency"
