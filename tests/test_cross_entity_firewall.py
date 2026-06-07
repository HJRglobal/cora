"""Cross-entity firewall tests (pre-launch hardening).

Two firewall guarantees are verified here:

1. Dynamic snapshot answers are entity-scoped in context_loader: a sibling
   entity's snapshot (e.g. F3E cash position / sales pipeline) must never leak
   into an OSN, LEX, BDM, HJRP, or UFL context, even though the startup prewarm
   loads every entity's snapshots. FNDR is the only cross-entity aggregator.

2. The OSN and F3E system prompts carry the explicit cross-entity refusal /
   redirect language added during pre-launch testing.
"""

import pathlib

import pytest

import cora.context_loader as cl

_PROMPTS_DIR = pathlib.Path(__file__).parent.parent / "design" / "system-prompts"


def _clear_cache():
    cl._cache.clear()


def _wire_static_paths(monkeypatch, tmp_path):
    """Point entity/founder/known-answer paths at tmp files so tests are isolated."""
    founder_path = tmp_path / "FNDR.md"
    founder_path.write_text("FNDR founder content", encoding="utf-8")
    monkeypatch.setattr(cl, "_FOUNDER_PATH", founder_path)

    # Give each entity a tiny stub CLAUDE.md.
    for entity in ("F3E", "OSN", "LEX", "BDM"):
        p = tmp_path / f"{entity}.md"
        p.write_text(f"{entity} entity content", encoding="utf-8")
        monkeypatch.setitem(cl._ENTITY_PATHS, entity, p)

    # No static known-answers files — keep the focus on dynamic snapshots.
    monkeypatch.setattr(cl, "_KNOWN_ANSWERS_PATHS", {})


# ---------------------------------------------------------------------------
# 1. Dynamic snapshot entity scoping
# ---------------------------------------------------------------------------


def test_osn_context_excludes_f3e_snapshot(monkeypatch, tmp_path):
    """OSN context must NOT contain F3E snapshot data."""
    _clear_cache()
    _wire_static_paths(monkeypatch, tmp_path)
    # Fake snapshot loader tags each entity's snapshot uniquely.
    monkeypatch.setattr(cl, "load_dynamic_answers", lambda e: f"SNAPSHOT-DATA-FOR-{e}")

    result = cl.load_context("OSN")

    assert "SNAPSHOT-DATA-FOR-OSN" in result
    assert "SNAPSHOT-DATA-FOR-F3E" not in result
    assert "SNAPSHOT-DATA-FOR-FNDR" not in result


def test_f3e_context_excludes_lex_snapshot(monkeypatch, tmp_path):
    """F3E context must NOT contain LEX snapshot data."""
    _clear_cache()
    _wire_static_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(cl, "load_dynamic_answers", lambda e: f"SNAPSHOT-DATA-FOR-{e}")

    result = cl.load_context("F3E")

    assert "SNAPSHOT-DATA-FOR-F3E" in result
    assert "SNAPSHOT-DATA-FOR-LEX" not in result
    assert "SNAPSHOT-DATA-FOR-OSN" not in result


def test_fndr_context_includes_all_snapshots(monkeypatch, tmp_path):
    """FNDR is the cross-entity aggregator — it sees every entity's snapshot."""
    _clear_cache()
    _wire_static_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(cl, "load_dynamic_answers", lambda e: f"SNAPSHOT-DATA-FOR-{e}")
    # Deterministic set of entity folders regardless of the real filesystem.
    monkeypatch.setattr(
        cl, "available_dynamic_entities", lambda: ["F3E", "OSN", "FNDR", "LEX", "BDM"]
    )

    result = cl.load_context("FNDR")

    for entity in ("F3E", "OSN", "FNDR", "LEX", "BDM"):
        assert f"SNAPSHOT-DATA-FOR-{entity}" in result


def test_allowed_snapshot_entities_non_fndr_is_self_only():
    """Non-FNDR entities resolve to their own folder only."""
    assert cl._allowed_snapshot_entities("OSN") == ["OSN"]
    assert cl._allowed_snapshot_entities("F3E") == ["F3E"]
    # A LEX sub-entity never inherits sibling/parent snapshots.
    assert cl._allowed_snapshot_entities("LEX-LLC") == ["LEX-LLC"]


# ---------------------------------------------------------------------------
# 2. System-prompt firewall language
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def osn_prompt() -> str:
    return (_PROMPTS_DIR / "osn.md").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def f3e_prompt() -> str:
    return (_PROMPTS_DIR / "f3e.md").read_text(encoding="utf-8")


def test_osn_prompt_has_cross_entity_refusal(osn_prompt):
    """osn.md must carry the cross-entity firewall refusal language."""
    assert "Cross-entity firewall" in osn_prompt
    assert "I'm scoped to OSN here" in osn_prompt
    assert "even if you have it in your context window" in osn_prompt


def test_f3e_prompt_has_lex_redirect(f3e_prompt):
    """f3e.md must carry the Lexington redirect language."""
    assert "That's a Lexington question" in f3e_prompt
    assert "#lex-leadership" in f3e_prompt
    assert "I'm scoped to F3 Energy here" in f3e_prompt
