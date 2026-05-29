"""Unit tests for the voice composition layer in prompt_loader."""

from textwrap import dedent

import cora.prompt_loader as pl


def _clear():
    pl.clear_cache()


def test_voice_block_appended_when_voice_yaml_present(monkeypatch, tmp_path):
    _clear()

    # Write a minimal entity prompt + voice yaml in the tmp prompts dir
    (tmp_path / "lex.md").write_text("LEX base prompt body.", encoding="utf-8")
    (tmp_path / "_voice.yaml").write_text(
        dedent(
            """
            defaults:
              voice: default voice line
              emoji_use: sparingly
              verbosity: balanced
            entities:
              LEX:
                voice: warm family-company tone
                emoji_use: sparingly
                verbosity: balanced
            """
        ).strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(pl, "_PROMPTS_DIR", tmp_path)

    result = pl.load_prompt("LEX")

    assert "LEX base prompt body." in result
    assert "## Voice + tone" in result
    assert "warm family-company tone" in result
    assert "**Emoji use:** sparingly." in result
    assert "**Verbosity:** balanced." in result


def test_voice_block_skipped_when_voice_yaml_missing(monkeypatch, tmp_path):
    _clear()

    # Only the .md file — no _voice.yaml
    (tmp_path / "f3e.md").write_text("F3E plain prompt", encoding="utf-8")
    monkeypatch.setattr(pl, "_PROMPTS_DIR", tmp_path)

    result = pl.load_prompt("F3E")

    # Voice block uses defaults (sparingly/balanced) and no voice text — composer
    # returns the prompt unchanged (then _UNIVERSAL_RULES is appended).
    assert result.startswith("F3E plain prompt")


def test_lex_inherits_voice_from_defaults_via_inherits_key(monkeypatch, tmp_path):
    _clear()

    (tmp_path / "lex.md").write_text("LEX body", encoding="utf-8")
    (tmp_path / "_voice.yaml").write_text(
        dedent(
            """
            defaults:
              voice: shared default voice
              emoji_use: none
              verbosity: terse
            entities:
              LEX:
                inherits: defaults
            """
        ).strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(pl, "_PROMPTS_DIR", tmp_path)

    result = pl.load_prompt("LEX")

    assert "shared default voice" in result
    assert "**Emoji use:** none." in result
    assert "**Verbosity:** terse." in result


def test_voice_field_override_wins_over_defaults(monkeypatch, tmp_path):
    _clear()

    (tmp_path / "bdm.md").write_text("BDM body", encoding="utf-8")
    (tmp_path / "_voice.yaml").write_text(
        dedent(
            """
            defaults:
              voice: default
              emoji_use: sparingly
              verbosity: balanced
            entities:
              BDM:
                voice: BDM-direct
                emoji_use: none
                verbosity: terse
            """
        ).strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(pl, "_PROMPTS_DIR", tmp_path)

    result = pl.load_prompt("BDM")

    assert "BDM-direct" in result
    assert "**Emoji use:** none." in result
    assert "**Verbosity:** terse." in result
    # Defaults should NOT leak through where BDM specified its own
    assert "voice: default" not in result
    assert "**Emoji use:** sparingly." not in result
    assert "**Verbosity:** balanced." not in result
