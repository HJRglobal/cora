"""S5 tests: lexicon mining -- lane A (telemetry), lane B (swept Slack),
fingerprints, caps, and the dry-run-default runner contract."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cora import lexicon, lexicon_mining
from cora.lexicon_mining import (
    LANE_B_MIN_HUMANS,
    LANE_B_MIN_USES,
    LexCandidate,
    is_already_proposed,
    mine_lane_a,
    mine_lane_b,
    run_mining,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_NOW = int(time.time())


@pytest.fixture()
def stores(tmp_path, monkeypatch):
    lex_dir = tmp_path / "lexicon"
    lex_dir.mkdir()
    (lex_dir / "f3e.yaml").write_text(
        "version: 1\nentity: F3E\nterms:\n"
        '  - {term: "bcb", type: vendor, canonical: "BLUE-CHIP", canonical_name: "Blue Chip Beverage", source: seed}\n'
        '  - {term: "bqt seed", type: project, canonical: "BQT-CANON", canonical_name: "BQT Canonical Project", source: seed}\n',
        encoding="utf-8")
    monkeypatch.setenv("LEXICON_DIR", str(lex_dir))
    monkeypatch.setenv("LEXICON_SKU_ALIASES_PATH", str(tmp_path / "no-skus.yaml"))
    monkeypatch.setenv("LEXICON_USER_ALIASES_PATH", str(tmp_path / "no-users.yaml"))
    monkeypatch.setenv("LEXICON_RESOLUTIONS_PATH", str(tmp_path / "resolutions.jsonl"))
    monkeypatch.setenv("LEXICON_FINGERPRINTS_PATH", str(tmp_path / "fingerprints.jsonl"))
    monkeypatch.setenv("LEXICON_CANDIDATES_PATH", str(tmp_path / "candidates.jsonl"))
    lexicon.invalidate_cache()
    yield tmp_path
    lexicon.invalidate_cache()


def _telemetry_row(*, ts, status, user, query="the bqt", qhash="h-miss",
                   canonical="", event="resolve", entity="F3E", channel="c1"):
    return {"ts": ts, "event": event, "entity": entity, "channel": channel,
            "user": user, "consumer": "t", "status": status,
            "query_display": query, "query_hash": qhash, "canonical": canonical,
            "matched_term": ""}


def _write_telemetry(path: Path, rows) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _rephrase_pair(user, t0, canonical="BQT-CANON"):
    return [
        _telemetry_row(ts=t0, status="miss", user=user),
        _telemetry_row(ts=t0 + 60, status="exact", user=user, query="bqt seed",
                       qhash="h-hit", canonical=canonical),
    ]


# ── Lane A ───────────────────────────────────────────────────────────────────


class TestLaneA:
    def test_two_pairs_two_users_is_eligible(self, stores):
        rows = _rephrase_pair("U1", _NOW - 5000) + _rephrase_pair("U2", _NOW - 3000)
        _write_telemetry(stores / "resolutions.jsonl", rows)
        cands = mine_lane_a()
        assert len(cands) == 1
        c = cands[0]
        assert (c.term, c.canonical, c.type) == ("the bqt", "BQT-CANON", "project")
        assert c.canonical_name == "BQT Canonical Project"
        assert c.contributor_id == ""  # below Tier-0 thresholds

    def test_single_user_not_eligible(self, stores):
        rows = _rephrase_pair("U1", _NOW - 5000) + _rephrase_pair("U1", _NOW - 3000)
        _write_telemetry(stores / "resolutions.jsonl", rows)
        assert mine_lane_a() == []

    def test_tier0_contributor_needs_counts_and_confirm(self, stores):
        rows = (_rephrase_pair("U1", _NOW - 9000)
                + _rephrase_pair("U2", _NOW - 6000)
                + _rephrase_pair("U1", _NOW - 3000)
                + [_telemetry_row(ts=_NOW - 2900, status="confirmed",
                                  event="resolution_confirmed", user="U1",
                                  query="bqt seed", canonical="BQT-CANON")])
        _write_telemetry(stores / "resolutions.jsonl", rows)
        cands = mine_lane_a()
        assert len(cands) == 1
        assert cands[0].contributor_id == "U1"

    def test_no_confirm_no_contributor_even_at_counts(self, stores):
        rows = (_rephrase_pair("U1", _NOW - 9000)
                + _rephrase_pair("U2", _NOW - 6000)
                + _rephrase_pair("U1", _NOW - 3000))
        _write_telemetry(stores / "resolutions.jsonl", rows)
        cands = mine_lane_a()
        assert len(cands) == 1
        assert cands[0].contributor_id == ""

    def test_withheld_lex_display_never_proposed(self, stores):
        rows = []
        for u, t0 in (("U1", _NOW - 5000), ("U2", _NOW - 3000)):
            rows.append(_telemetry_row(ts=t0, status="miss", user=u,
                                       query="[withheld]", entity="LEX"))
            rows.append(_telemetry_row(ts=t0 + 60, status="exact", user=u,
                                       query="evv", qhash="h-hit",
                                       canonical="EVV", entity="LEX"))
        _write_telemetry(stores / "resolutions.jsonl", rows)
        assert mine_lane_a() == []

    def test_unknown_canonical_skipped(self, stores):
        rows = _rephrase_pair("U1", _NOW - 5000, canonical="NOT-IN-REGISTRY") + \
            _rephrase_pair("U2", _NOW - 3000, canonical="NOT-IN-REGISTRY")
        _write_telemetry(stores / "resolutions.jsonl", rows)
        assert mine_lane_a() == []

    def test_cross_user_hit_never_pairs(self, stores):
        rows = [
            _telemetry_row(ts=_NOW - 5000, status="miss", user="U1"),
            _telemetry_row(ts=_NOW - 4940, status="exact", user="U2",
                           query="bqt seed", qhash="h-hit", canonical="BQT-CANON"),
        ]
        _write_telemetry(stores / "resolutions.jsonl", rows)
        assert mine_lane_a() == []


# ── Lane B ───────────────────────────────────────────────────────────────────


def _chunk(content, *, entity="F3E", sid="s1"):
    return {"source": "slack", "source_id": sid, "entity": entity,
            "content": content, "title": "", "ingested_at": _NOW}


def _bqt_chunks(n_chunks=5, speakers=("Alex Cordova", "Hannah Grant", "Justin Moran")):
    """Chunks where the acronym BQT co-occurs with a known canonical_name.
    A 'use' is chunk-level (the extractor dedups within a chunk), so
    n_chunks >= LANE_B_MIN_USES makes the term eligible."""
    chunks = []
    for i in range(n_chunks):
        lines = [f"{spk}: we should get BQT lined up with Blue Chip Beverage"
                 for spk in speakers]
        chunks.append(_chunk("\n".join(lines), sid=f"s{i}"))
    return chunks


class TestLaneB:
    def test_recurring_acronym_with_canonical_cooccurrence(self, stores):
        cands, ledger_only = mine_lane_b(chunks=_bqt_chunks(),
                                         embed_fn=lambda t: [])
        assert len(cands) == 1
        c = cands[0]
        assert c.term == "BQT"
        assert c.canonical == "BLUE-CHIP"
        assert c.type == "vendor"          # typed from the co-occurring entry
        assert c.lane == "mined"
        assert c.contributor_id == ""      # machine-mined -> Tier 2 always

    def test_below_use_threshold_yields_nothing(self, stores):
        chunks = _bqt_chunks(n_chunks=LANE_B_MIN_USES - 1)
        cands, _ = mine_lane_b(chunks=chunks, embed_fn=lambda t: [])
        assert cands == []

    def test_below_human_threshold_yields_nothing(self, stores):
        chunks = _bqt_chunks(speakers=("Alex Cordova", "Hannah Grant"))
        cands, _ = mine_lane_b(chunks=chunks, embed_fn=lambda t: [])
        assert cands == []

    def test_speaker_names_never_become_candidates(self, stores):
        cands, ledger_only = mine_lane_b(chunks=_bqt_chunks(),
                                         embed_fn=lambda t: [])
        all_terms = [c.term for c in cands] + [r["term"] for r in ledger_only]
        assert "Alex Cordova" not in all_terms
        assert "Hannah Grant" not in all_terms

    def test_existing_lexicon_surface_skipped(self, stores):
        chunks = [_chunk("\n".join(
            f"{spk}: BCB order is in with Blue Chip Beverage"
            for spk in ("Alex Cordova", "Hannah Grant", "Justin Moran")), sid=f"s{i}")
            for i in range(5)]
        cands, ledger_only = mine_lane_b(chunks=chunks, embed_fn=lambda t: [])
        assert all(c.term != "BCB" for c in cands)

    def test_no_canonical_is_ledger_only(self, stores):
        chunks = [_chunk("\n".join(
            f"{spk}: ping ZQX about the samples"
            for spk in ("Alex Cordova", "Hannah Grant", "Justin Moran")), sid=f"s{i}")
            for i in range(5)]
        cands, ledger_only = mine_lane_b(chunks=chunks, embed_fn=lambda t: [])
        assert cands == []
        assert any(r["term"] == "ZQX" and r["reason"] == "no_confident_canonical"
                   for r in ledger_only)

    def test_lex_chunks_belt_skipped(self, stores):
        chunks = [{**c, "entity": "LEX"} for c in _bqt_chunks()]
        cands, ledger_only = mine_lane_b(chunks=chunks, embed_fn=lambda t: [])
        assert cands == [] and ledger_only == []

    def test_phi_shaped_term_dropped(self, stores):
        chunks = [_chunk("\n".join(
            f"{spk}: Bob Smith Authorization needs review with Blue Chip Beverage"
            for spk in ("Alex Cordova", "Hannah Grant", "Justin Moran")), sid=f"s{i}")
            for i in range(3)]
        cands, ledger_only = mine_lane_b(chunks=chunks, embed_fn=lambda t: [])
        assert all("Bob Smith" not in c.term for c in cands)
        assert all("Bob Smith" not in str(r) for r in ledger_only)


# ── Fingerprints + run contract ──────────────────────────────────────────────


def _cand(**over) -> LexCandidate:
    base = dict(term="the bqt", entity="F3E", canonical="BQT-CANON",
                canonical_name="BQT Canonical Project", type="project",
                lane="resolver", events=2, users={"U1", "U2"})
    base.update(over)
    return LexCandidate(**base)


class TestFingerprints:
    def test_exact_and_fuzzy_dedup(self, stores):
        ledger = [{"fingerprint": _cand().fingerprint, "term": "the bqt",
                   "entity": "F3E"}]
        assert is_already_proposed(_cand(), ledger)
        assert is_already_proposed(_cand(term="the  bqt!"), ledger)  # fuzzy/norm
        assert not is_already_proposed(_cand(term="completely different"), ledger)

    def test_record_proposal_writes_ledger(self, stores):
        lexicon_mining.record_proposal(_cand(), "lexicon-abc")
        rows = [json.loads(l) for l in
                (stores / "fingerprints.jsonl").read_text(encoding="utf-8").splitlines()]
        assert rows[0]["update_id"] == "lexicon-abc"
        assert rows[0]["lane"] == "resolver"


class TestRunContract:
    def _seed_lane_a(self, stores):
        rows = _rephrase_pair("U1", _NOW - 5000) + _rephrase_pair("U2", _NOW - 3000)
        _write_telemetry(stores / "resolutions.jsonl", rows)

    def test_dry_run_default_writes_nothing(self, stores, monkeypatch):
        monkeypatch.setenv("CORA_LEXICON", "full")
        self._seed_lane_a(stores)
        summary = run_mining(dry_run=True, lane="a")
        assert summary["lane_a_candidates"] == 1
        assert summary["proposed"] == 0
        assert not (stores / "fingerprints.jsonl").exists()
        assert not (stores / "candidates.jsonl").exists()

    def test_write_below_full_is_ledger_only(self, stores, monkeypatch):
        monkeypatch.setenv("CORA_LEXICON", "resolve")
        self._seed_lane_a(stores)
        proposed = []
        monkeypatch.setattr(lexicon_mining, "propose_lexicon",
                            lambda c, d: proposed.append(c) or "id")
        summary = run_mining(dry_run=False, lane="a")
        assert summary["proposed"] == 0
        assert proposed == []
        assert "candidates-ledger-only" in summary.get("note", "")

    def test_write_at_full_proposes_and_records(self, stores, monkeypatch):
        monkeypatch.setenv("CORA_LEXICON", "full")
        self._seed_lane_a(stores)
        proposed = []
        monkeypatch.setattr(lexicon_mining, "draft_description",
                            lambda c: "a drafted line")
        monkeypatch.setattr(lexicon_mining, "propose_lexicon",
                            lambda c, d: proposed.append((c, d)) or "lexicon-x")
        summary = run_mining(dry_run=False, lane="a")
        assert summary["proposed"] == 1
        assert len(proposed) == 1
        assert (stores / "fingerprints.jsonl").exists()

    def test_failed_draft_is_fail_closed(self, stores, monkeypatch):
        monkeypatch.setenv("CORA_LEXICON", "full")
        self._seed_lane_a(stores)
        monkeypatch.setattr(lexicon_mining, "draft_description", lambda c: None)
        called = []
        monkeypatch.setattr(lexicon_mining, "propose_lexicon",
                            lambda c, d: called.append(c) or "id")
        summary = run_mining(dry_run=False, lane="a")
        assert summary["proposed"] == 0 and called == []

    def test_cap_enforced(self, stores, monkeypatch):
        monkeypatch.setenv("CORA_LEXICON", "full")
        cands = [_cand(term=f"term {i}", canonical="BQT-CANON") for i in range(9)]
        monkeypatch.setattr(lexicon_mining, "mine_lane_a", lambda **k: cands)
        monkeypatch.setattr(lexicon_mining, "draft_description", lambda c: "d")
        proposed = []
        monkeypatch.setattr(lexicon_mining, "propose_lexicon",
                            lambda c, d: proposed.append(c) or "id")
        summary = run_mining(dry_run=False, lane="a")
        assert summary["proposed"] == 5 and len(proposed) == 5

    def test_already_proposed_skipped(self, stores, monkeypatch):
        monkeypatch.setenv("CORA_LEXICON", "full")
        self._seed_lane_a(stores)
        lexicon_mining.record_proposal(_cand(), "lexicon-old")
        summary = run_mining(dry_run=True, lane="a")
        assert summary["already_proposed"] == 1
        assert summary["candidates"] == []


# ── Doctrine guards ──────────────────────────────────────────────────────────


class TestNoBotProcessImport:
    def test_import_does_not_pull_bot_modules(self):
        code = (
            "import sys; sys.path.insert(0, r'%s'); "
            "import cora.lexicon_mining; "
            "bad = [m for m in ('cora.app', 'cora.tools.tool_dispatch', 'cora.claude_client')"
            " if m in sys.modules]; "
            "assert not bad, f'bot-process modules imported: {bad}'"
        ) % str(_REPO_ROOT / "src")
        result = subprocess.run([sys.executable, "-c", code],
                                capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stderr


class TestSourceWiring:
    def test_runner_dry_run_default_and_write_flag(self):
        src = (_REPO_ROOT / "scripts" / "run_lexicon_mining.py").read_text(encoding="utf-8")
        assert '"--write"' in src
        assert "dry_run=not args.write" in src

    def test_ps1_doctrine(self):
        src = (_REPO_ROOT / "deployment" / "setup-lexicon-mining-task.ps1").read_text(
            encoding="utf-8")
        assert r".venv\Scripts\python.exe" in src
        assert "uv run" not in src
        assert "Sunday" in src and "17:50" in src
        assert all(ord(c) < 128 for c in src), "PS1 must be ASCII-only (D-016)"

    def test_executor_branch_exists(self):
        src = (_REPO_ROOT / "scripts" / "run_knowledge_review.py").read_text(encoding="utf-8")
        assert 'update_type == "lexicon"' in src
        assert "apply_lexicon_update" in src
