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
