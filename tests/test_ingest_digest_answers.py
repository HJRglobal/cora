"""Tests for ingest_digest_answers and digest builder resolved-gap filtering."""

import json
import sys
from pathlib import Path

import pytest

# scripts/ is not an installed package — add to path for import
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import ingest_digest_answers as iga
import generate_knowledge_gaps_digest as dgb


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

GAP_ID = "2026-05-18T09:10:15.865431+00:00"
GAP_DESC = "F3 Pure expected ROI -- no projections in context"
QUESTION = "What is the expected ROI for F3 Pure in year 1?"


def _make_digest(entity: str, entity_label: str, answer: str, gap_id: str = GAP_ID) -> str:
    """Build a minimal digest string with a single gap entry."""
    return (
        f"# Cora Knowledge Gaps Digest -- 2026-05-18\n\n"
        f"_Range: all gaps_\n\n"
        f"**Total gaps in this window: 1**\n\n"
        f"---\n\n"
        f"## How to use this digest\n\nInstructions.\n\n---\n\n"
        f"## {entity_label} ({entity}) -- 1 gap(s)\n\n"
        f"<!-- GAP_ID: {gap_id} -->\n"
        f"### {entity}-1: {GAP_DESC[:80]}\n\n"
        f"- **When:** 2026-05-18T09:10:15+00:00\n"
        f"- **Channel:** #test-channel\n"
        f"- **Asked by:** U123\n"
        f"- **Latency:** 1000ms · **Response sent:** 100 chars\n\n"
        f"**Question asked:**\n\n"
        f"> {QUESTION}\n\n"
        f"**Gap Cora flagged:**\n\n"
        f"> {GAP_DESC}\n\n"
        f"**Your answer:**\n\n"
        "```\n"
        f"{answer}\n"
        "```\n\n"
        "---\n\n"
    )


def _write_digest(tmp_path: Path, answer: str, entity: str = "FNDR",
                  entity_label: str = "Founder / cross-portfolio") -> Path:
    digest_file = tmp_path / "2026-05-18-digest.md"
    digest_file.write_text(_make_digest(entity, entity_label, answer), encoding="utf-8")
    return digest_file


# ---------------------------------------------------------------------------
# test_parse_basic_digest_with_one_answer
# ---------------------------------------------------------------------------

def test_parse_basic_digest_with_one_answer(tmp_path):
    digest_file = _write_digest(tmp_path, "The F3 Pure ROI target is 3x by end of 2026.")
    ka_dir = tmp_path / "known-answers"
    resolved_path = tmp_path / ".resolved-gaps.jsonl"

    result = iga.main.__wrapped__ if hasattr(iga.main, "__wrapped__") else None

    # Call via args simulation
    sys.argv = [
        "ingest_digest_answers.py",
        "--digest", str(digest_file),
        "--known-answers-dir", str(ka_dir),
        "--resolved-path", str(resolved_path),
    ]
    rc = iga.main()
    assert rc == 0

    fndr_file = ka_dir / "fndr.md"
    assert fndr_file.exists()
    content = fndr_file.read_text(encoding="utf-8")
    assert "F3 Pure ROI target is 3x" in content
    assert "## Known facts" in content

    resolved = json.loads(resolved_path.read_text(encoding="utf-8").strip())
    assert resolved["id"] == GAP_ID
    assert resolved["action"] == "answer"


# ---------------------------------------------------------------------------
# test_parse_skip_action
# ---------------------------------------------------------------------------

def test_parse_skip_action(tmp_path):
    digest_file = _write_digest(tmp_path, "SKIP")
    ka_dir = tmp_path / "known-answers"
    resolved_path = tmp_path / ".resolved-gaps.jsonl"

    sys.argv = [
        "ingest_digest_answers.py",
        "--digest", str(digest_file),
        "--known-answers-dir", str(ka_dir),
        "--resolved-path", str(resolved_path),
    ]
    rc = iga.main()
    assert rc == 0

    resolved = json.loads(resolved_path.read_text(encoding="utf-8").strip())
    assert resolved["action"] == "skip"
    assert resolved["id"] == GAP_ID

    # known-answers file should be initialized but no fact appended
    fndr_file = ka_dir / "fndr.md"
    content = fndr_file.read_text(encoding="utf-8")
    assert "F3 Pure" not in content


# ---------------------------------------------------------------------------
# test_parse_route_action
# ---------------------------------------------------------------------------

def test_parse_route_action(tmp_path):
    digest_file = _write_digest(tmp_path, "ROUTE: ask Tommy for F3 Pure distribution projections")
    ka_dir = tmp_path / "known-answers"
    resolved_path = tmp_path / ".resolved-gaps.jsonl"

    sys.argv = [
        "ingest_digest_answers.py",
        "--digest", str(digest_file),
        "--known-answers-dir", str(ka_dir),
        "--resolved-path", str(resolved_path),
    ]
    rc = iga.main()
    assert rc == 0

    fndr_file = ka_dir / "fndr.md"
    content = fndr_file.read_text(encoding="utf-8")
    assert "ask Tommy" in content
    assert "## Routing rules" in content

    resolved = json.loads(resolved_path.read_text(encoding="utf-8").strip())
    assert resolved["action"] == "route"


# ---------------------------------------------------------------------------
# test_parse_empty_answer_defers
# ---------------------------------------------------------------------------

def test_parse_empty_answer_defers(tmp_path):
    placeholder = "(leave empty to defer · write SKIP to mark trivial · write the answer to feed back to Cora · write ROUTE: ask [person] for routing rule)"
    digest_file = _write_digest(tmp_path, placeholder)
    ka_dir = tmp_path / "known-answers"
    resolved_path = tmp_path / ".resolved-gaps.jsonl"

    sys.argv = [
        "ingest_digest_answers.py",
        "--digest", str(digest_file),
        "--known-answers-dir", str(ka_dir),
        "--resolved-path", str(resolved_path),
    ]
    rc = iga.main()
    assert rc == 0

    # Nothing should be written to resolved-gaps
    assert not resolved_path.exists()


# ---------------------------------------------------------------------------
# test_entity_override_tag
# ---------------------------------------------------------------------------

def test_entity_override_tag(tmp_path):
    # Gap captured under FNDR, but answer says to route to F3E
    digest_file = _write_digest(
        tmp_path,
        "[ENTITY: F3E] The Sprouts buyer is John Smith, last contact 2026-05-01.",
        entity="FNDR",
        entity_label="Founder / cross-portfolio",
    )
    ka_dir = tmp_path / "known-answers"
    resolved_path = tmp_path / ".resolved-gaps.jsonl"

    sys.argv = [
        "ingest_digest_answers.py",
        "--digest", str(digest_file),
        "--known-answers-dir", str(ka_dir),
        "--resolved-path", str(resolved_path),
    ]
    rc = iga.main()
    assert rc == 0

    # Answer should go to f3e.md, not fndr.md
    f3e_file = ka_dir / "f3e.md"
    assert f3e_file.exists()
    content = f3e_file.read_text(encoding="utf-8")
    assert "John Smith" in content

    # Resolved record: target_entity=F3E, captured_entity=FNDR
    resolved = json.loads(resolved_path.read_text(encoding="utf-8").strip())
    assert resolved["target_entity"] == "F3E"
    assert resolved["captured_entity"] == "FNDR"
    assert resolved["action"] == "answer"


# ---------------------------------------------------------------------------
# test_resolved_ids_filter_in_digest_builder
# ---------------------------------------------------------------------------

def test_resolved_ids_filter_in_digest_builder(tmp_path):
    gap_ts = "2026-05-18T09:10:15.865431+00:00"

    # Create a gaps JSONL with one entry
    gaps_file = tmp_path / "knowledge-gaps.jsonl"
    gaps_file.write_text(
        json.dumps({
            "ts": gap_ts,
            "entity": "FNDR",
            "gap": "test gap",
            "question": "q?",
            "response_chars": 100,
            "latency_ms": 1000,
            "channel": "cora-build",
            "user": "U123",
        }) + "\n",
        encoding="utf-8",
    )

    # Create a resolved-gaps JSONL marking that gap as done
    resolved_file = tmp_path / ".resolved-gaps.jsonl"
    resolved_file.write_text(
        json.dumps({"id": gap_ts, "action": "answer", "timestamp": gap_ts}) + "\n",
        encoding="utf-8",
    )

    resolved_ids = dgb.load_resolved_ids(resolved_file)
    gaps = dgb.load_gaps(gaps_file, resolved_ids)

    assert len(gaps) == 0  # gap was filtered out as already resolved
