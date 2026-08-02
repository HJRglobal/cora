"""S7: the lexicon golden set, running inside pytest forever.

resolve_cases hit lexicon.resolve against the REAL seed stores (read-only).
capture_cases run the applier against tmp copies of the stores (never the real
files). The hard gates: 100% ask-on-ambiguity and ZERO false resolutions.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from cora import lexicon, lexicon_writer

_REPO = Path(__file__).resolve().parents[1]
_GOLDEN = _REPO / "tests" / "golden" / "lexicon_golden.yaml"


def _load():
    data = yaml.safe_load(_GOLDEN.read_text(encoding="utf-8"))
    return data.get("resolve_cases") or [], data.get("capture_cases") or []


_RESOLVE_CASES, _CAPTURE_CASES = _load()


@pytest.fixture(autouse=True)
def _fresh_registry():
    lexicon.invalidate_cache()
    yield
    lexicon.invalidate_cache()


class TestGoldenCorpus:
    def test_corpus_shape(self):
        assert len(_RESOLVE_CASES) >= 50, "golden set shrank below 50 resolve cases"
        ids = [c["id"] for c in _RESOLVE_CASES] + [c["id"] for c in _CAPTURE_CASES]
        assert len(ids) == len(set(ids)), "duplicate golden case ids"
        expects = {c["expect"] for c in _RESOLVE_CASES}
        assert expects <= {"exact", "ask", "suggestion", "miss"}
        # Negative coverage floors (the design's must-have classes).
        assert sum(1 for c in _RESOLVE_CASES if c["expect"] == "ask") >= 4
        assert sum(1 for c in _RESOLVE_CASES if c["expect"] == "miss") >= 8


@pytest.mark.parametrize("case", _RESOLVE_CASES, ids=[c["id"] for c in _RESOLVE_CASES])
def test_golden_resolve(case):
    r = lexicon.resolve(case["utterance"], case["entity"])
    expect = case["expect"]
    if expect == "exact":
        assert r.status == "exact", f"{case['id']}: got {r.status}"
        assert r.canonical == case["canonical"], f"{case['id']}: {r.canonical}"
    elif expect == "ask":
        assert r.status == "ambiguous", f"{case['id']}: got {r.status}"
        assert r.canonical == "", f"{case['id']}: silent pick on an ambiguity"
        got = {c.canonical for c in r.candidates}
        assert got == set(case["candidates"]), f"{case['id']}: {got}"
    elif expect == "suggestion":
        assert r.status == "suggestion", f"{case['id']}: got {r.status}"
        assert r.canonical == "", f"{case['id']}: a suggestion must never resolve"
    elif expect == "miss":
        # miss OR suggestion is acceptable; exact/ambiguous is a FALSE RESOLUTION.
        assert r.status in ("miss", "suggestion"), f"{case['id']}: got {r.status}"
        assert r.canonical == "", f"{case['id']}: false resolution"


@pytest.mark.parametrize("case", _CAPTURE_CASES, ids=[c["id"] for c in _CAPTURE_CASES])
def test_golden_capture(case, tmp_path, monkeypatch):
    lex_dir = tmp_path / "lexicon"
    lex_dir.mkdir()
    monkeypatch.setenv("LEXICON_DIR", str(lex_dir))
    monkeypatch.setenv("LEXICON_SKU_ALIASES_PATH", str(tmp_path / "skus.yaml"))
    monkeypatch.setenv("LEXICON_USER_ALIASES_PATH", str(tmp_path / "users.yaml"))
    monkeypatch.setenv("LEXICON_ROSTER_PATH", str(tmp_path / "roster.yaml"))
    (tmp_path / "roster.yaml").write_text(
        'users:\n  - {slack_user_id: U1, display_name: "Jennifer Mortensen"}\n',
        encoding="utf-8")
    lexicon.invalidate_cache()
    ok, summary = lexicon_writer.apply_lexicon_update(dict(case["payload"]))
    if case["expect"] == "refused":
        assert not ok, f"{case['id']}: applied but must refuse ({summary})"
    else:
        assert ok, f"{case['id']}: refused but must apply ({summary})"
