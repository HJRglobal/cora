"""S3 tests: lexicon prompt injection (flag-gated at CORA_LEXICON=full).

Pins: the '## Company lexicon' block rides the STATIC context (cached block 2)
only at level 'full'; LEX sub-entity channels NEVER get a block; caps hold; all
17 entity prompts carry the standing '## Company shorthand' section.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from cora import context_loader, lexicon

_REPO = Path(__file__).resolve().parents[1]
_FOUNDER_STUB = "# Founder brief\n\n# Current State of the World\n\n(dynamic)\n"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    context_loader._cache.clear()
    lexicon.invalidate_cache()
    # Static context reads G: via drive_io -- stub them so the test exercises
    # ONLY the lexicon append (entity/founder/known-answers reads become inert).
    monkeypatch.setattr(context_loader.drive_io, "exists", lambda *a, **k: False)
    monkeypatch.setattr(context_loader.drive_io, "read_text",
                        lambda *a, **k: _FOUNDER_STUB)
    monkeypatch.setattr(context_loader.drive_io, "stat_mtime", lambda *a, **k: None)
    yield
    context_loader._cache.clear()
    lexicon.invalidate_cache()


def _static(entity: str) -> str:
    return context_loader._build_static_context(entity, time.monotonic())


class TestInjection:
    def test_full_injects_block_into_static(self, monkeypatch):
        monkeypatch.setenv("CORA_LEXICON", "full")
        text = _static("F3E")
        assert "## Company lexicon" in text
        assert '"bcb"' in text

    @pytest.mark.parametrize("level", ["off", "resolve"])
    def test_below_full_injects_nothing(self, monkeypatch, level):
        monkeypatch.setenv("CORA_LEXICON", level)
        assert "## Company lexicon" not in _static("F3E")

    def test_lex_gm_gets_block_sub_entities_do_not(self, monkeypatch):
        monkeypatch.setenv("CORA_LEXICON", "full")
        assert "## Company lexicon" in _static("LEX")
        for sub in ("LEX-LLC", "LEX-LTS", "LEX-LBHS", "LEX-LLA"):
            context_loader._cache.clear()
            assert "## Company lexicon" not in _static(sub), sub

    def test_fndr_union_block_under_same_cap(self, monkeypatch):
        monkeypatch.setenv("CORA_LEXICON", "full")
        text = _static("FNDR")
        assert "## Company lexicon" in text
        start = text.index("## Company lexicon")
        block = text[start:]
        assert len(block) <= lexicon.MAX_BLOCK_CHARS + 200  # block + join residue

    def test_lexicon_failure_never_costs_static_context(self, monkeypatch):
        monkeypatch.setenv("CORA_LEXICON", "full")
        def _boom(entity):
            raise RuntimeError("synthetic lexicon failure")
        monkeypatch.setattr(lexicon, "format_lexicon_context", _boom)
        text = _static("F3E")  # must not raise
        assert "## Company lexicon" not in text

    def test_block_is_static_not_runtime(self, monkeypatch):
        """The block rides load_context_parts' STATIC element (cached block 2),
        never the kb/runtime element."""
        monkeypatch.setenv("CORA_LEXICON", "full")
        static_text, kb_text = context_loader.load_context_parts("F3E")
        assert "## Company lexicon" in static_text
        assert kb_text == ""


class TestPromptCoverage:
    def test_all_17_prompts_carry_company_shorthand_section(self):
        prompts_dir = _REPO / "design" / "system-prompts"
        files = sorted(prompts_dir.glob("*.md"))
        assert len(files) == 17
        for f in files:
            text = f.read_text(encoding="utf-8")
            assert "## Company shorthand" in text, f.name
            assert "ask which one is meant" in text, f.name
            assert "never overrides Known Answers" in text, f.name
