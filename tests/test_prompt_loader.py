"""Unit tests for prompt_loader.load_prompt()."""

import logging

import pytest

import cora.prompt_loader as pl


def _clear_cache():
    pl.clear_cache()


def test_load_f3e(monkeypatch, tmp_path):
    _clear_cache()

    (tmp_path / "f3e.md").write_text("F3E system prompt", encoding="utf-8")
    monkeypatch.setattr(pl, "_PROMPTS_DIR", tmp_path)

    result = pl.load_prompt("F3E")

    assert result == "F3E system prompt"


def test_missing_entity_falls_back_to_fndr_with_error_logged(monkeypatch, tmp_path, caplog):
    _clear_cache()

    (tmp_path / "fndr.md").write_text("FNDR system prompt", encoding="utf-8")
    # osn.md intentionally absent
    monkeypatch.setattr(pl, "_PROMPTS_DIR", tmp_path)

    with caplog.at_level(logging.ERROR, logger="cora.prompt_loader"):
        result = pl.load_prompt("OSN")

    assert result == "FNDR system prompt"
    assert "OSN" in caplog.text


def test_cache_returns_same_string_on_repeated_calls(monkeypatch, tmp_path):
    _clear_cache()

    f3e_path = tmp_path / "f3e.md"
    f3e_path.write_text("F3E system prompt", encoding="utf-8")
    monkeypatch.setattr(pl, "_PROMPTS_DIR", tmp_path)

    result1 = pl.load_prompt("F3E")

    # overwrite the file — a re-read would return different text
    f3e_path.write_text("modified prompt", encoding="utf-8")

    result2 = pl.load_prompt("F3E")

    assert result1 is result2
    assert result1 == "F3E system prompt"


# --- Lex sub-entity codes (added 2026-05-23 siloing fix) ---


@pytest.mark.parametrize("entity_code, filename, description", [
    ("LEX-LLC",  "llc.md",  "Lexington LLC — Shaun Hawkins, DDD/HCBS/DTA ops"),
    ("LEX-LTS",  "lts.md",  "Lexington Therapies — Justin Gilmore, therapy services"),
    ("LEX-LBHS", "lbhs.md", "Lexington Behavioral Health — Jared Harker"),
    ("LEX-LLA",  "lla.md",  "Lex Life Academy — Sandy Patel, school/clinic"),
])
def test_lex_sub_entity_loads_correct_file(monkeypatch, tmp_path, entity_code, filename, description):
    """Each Lex sub-entity code loads its own .md file, not the FNDR fallback."""
    _clear_cache()

    # Write a distinctive prompt for this sub-entity
    prompt_text = f"You are Cora for {entity_code}. {description}"
    (tmp_path / filename).write_text(prompt_text, encoding="utf-8")
    # Also write fndr.md so fallback path doesn't raise RuntimeError
    (tmp_path / "fndr.md").write_text("FNDR fallback prompt", encoding="utf-8")
    monkeypatch.setattr(pl, "_PROMPTS_DIR", tmp_path)

    result = pl.load_prompt(entity_code)

    assert len(result) > 100 or entity_code in result  # file loaded, not empty
    assert "FNDR fallback" not in result, f"{entity_code} fell back to FNDR prompt unexpectedly"
    assert entity_code in result, f"Expected {entity_code} to appear in its own prompt"


def test_lex_gm_still_routes_to_lex_md(monkeypatch, tmp_path):
    """LEX entity code still loads lex.md (GM-level), not a sub-entity file."""
    _clear_cache()

    (tmp_path / "lex.md").write_text("LEX GM-level prompt — four sub-entities", encoding="utf-8")
    (tmp_path / "fndr.md").write_text("FNDR fallback prompt", encoding="utf-8")
    monkeypatch.setattr(pl, "_PROMPTS_DIR", tmp_path)

    result = pl.load_prompt("LEX")

    assert "GM-level" in result
    assert "FNDR fallback" not in result
