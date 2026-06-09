"""Unit tests for context_loader.load_context()."""

import logging

import pytest

import cora.context_loader as cl


def _clear_cache():
    cl._cache.clear()


def test_f3e_returns_both_entity_and_founder_content(monkeypatch, tmp_path):
    _clear_cache()

    f3e_path = tmp_path / "F3E.md"
    f3e_path.write_text("F3E entity content", encoding="utf-8")

    founder_path = tmp_path / "FNDR.md"
    founder_path.write_text("FNDR founder content", encoding="utf-8")

    monkeypatch.setitem(cl._ENTITY_PATHS, "F3E", f3e_path)
    monkeypatch.setattr(cl, "_FOUNDER_PATH", founder_path)

    result = cl.load_context("F3E")

    assert "F3E entity content" in result
    assert "FNDR founder content" in result


def test_osn_missing_falls_back_to_founder_with_warning(monkeypatch, tmp_path, caplog):
    _clear_cache()

    osn_path = tmp_path / "OSN.md"  # intentionally not created

    founder_path = tmp_path / "FNDR.md"
    founder_path.write_text("FNDR founder content", encoding="utf-8")

    monkeypatch.setitem(cl._ENTITY_PATHS, "OSN", osn_path)
    monkeypatch.setattr(cl, "_FOUNDER_PATH", founder_path)

    with caplog.at_level(logging.WARNING, logger="cora.context_loader"):
        result = cl.load_context("OSN")

    assert "FNDR founder content" in result
    assert "OSN" in caplog.text


def test_load_context_with_known_answers(monkeypatch, tmp_path):
    _clear_cache()

    f3e_path = tmp_path / "F3E.md"
    f3e_path.write_text("F3E entity content", encoding="utf-8")

    founder_path = tmp_path / "FNDR.md"
    founder_path.write_text("FNDR founder content", encoding="utf-8")

    ka_path = tmp_path / "f3e-known.md"
    ka_path.write_text("## Known facts\n\nSprouts buyer is John Smith.", encoding="utf-8")

    monkeypatch.setitem(cl._ENTITY_PATHS, "F3E", f3e_path)
    monkeypatch.setattr(cl, "_FOUNDER_PATH", founder_path)
    monkeypatch.setitem(cl._KNOWN_ANSWERS_PATHS, "F3E", ka_path)

    result = cl.load_context("F3E")

    assert "F3E entity content" in result
    assert "FNDR founder content" in result
    assert "Sprouts buyer is John Smith" in result
    assert "Known Answers" in result


def test_load_context_no_known_answers_file(monkeypatch, tmp_path):
    _clear_cache()

    f3e_path = tmp_path / "F3E.md"
    f3e_path.write_text("F3E entity content", encoding="utf-8")

    founder_path = tmp_path / "FNDR.md"
    founder_path.write_text("FNDR founder content", encoding="utf-8")

    missing_ka = tmp_path / "nonexistent-known.md"  # intentionally not created

    monkeypatch.setitem(cl._ENTITY_PATHS, "F3E", f3e_path)
    monkeypatch.setattr(cl, "_FOUNDER_PATH", founder_path)
    monkeypatch.setitem(cl._KNOWN_ANSWERS_PATHS, "F3E", missing_ka)
    monkeypatch.setattr(cl, "load_dynamic_answers", lambda entity: "")

    result = cl.load_context("F3E")

    assert "F3E entity content" in result
    assert "FNDR founder content" in result
    assert "Known Answers" not in result


def test_load_context_includes_dynamic_answers(monkeypatch, tmp_path):
    _clear_cache()

    f3e_path = tmp_path / "F3E.md"
    f3e_path.write_text("F3E entity content", encoding="utf-8")
    founder_path = tmp_path / "FNDR.md"
    founder_path.write_text("FNDR founder content", encoding="utf-8")

    monkeypatch.setitem(cl._ENTITY_PATHS, "F3E", f3e_path)
    monkeypatch.setattr(cl, "_FOUNDER_PATH", founder_path)
    monkeypatch.setattr(cl, "load_dynamic_answers", lambda entity: "Live revenue: $42k MRR.")

    result = cl.load_context("F3E")

    assert "Dynamic Known Answers" in result
    assert "Live revenue: $42k MRR." in result


def test_load_context_no_dynamic_answers_section_when_empty(monkeypatch, tmp_path):
    _clear_cache()

    f3e_path = tmp_path / "F3E.md"
    f3e_path.write_text("F3E entity content", encoding="utf-8")
    founder_path = tmp_path / "FNDR.md"
    founder_path.write_text("FNDR founder content", encoding="utf-8")

    monkeypatch.setitem(cl._ENTITY_PATHS, "F3E", f3e_path)
    monkeypatch.setattr(cl, "_FOUNDER_PATH", founder_path)
    monkeypatch.setattr(cl, "load_dynamic_answers", lambda entity: "")

    result = cl.load_context("F3E")

    assert "Dynamic Known Answers" not in result


def test_cache_returns_same_instance_within_ttl(monkeypatch, tmp_path):
    _clear_cache()

    founder_path = tmp_path / "FNDR.md"
    founder_path.write_text("original content", encoding="utf-8")

    monkeypatch.setattr(cl, "_FOUNDER_PATH", founder_path)
    monkeypatch.setattr(cl, "_ENTITY_PATHS", {})

    result1 = cl.load_context("FNDR")

    # overwrite the file — a re-read would return different content
    founder_path.write_text("modified content", encoding="utf-8")

    result2 = cl.load_context("FNDR")

    assert result1 is result2  # same object from cache
    assert "original content" in result1


# ── Founder CLAUDE.md slimming (section 10.3) ───────────────────────────────

_FOUNDER_DOC = (
    "# HJR Founder OS\n\nStatic brief: portfolio principles.\n\n"
    "---\n\n# Current State of the World\n\nTOM: secret dynamic stuff.\n"
)


def test_non_aggregator_entity_gets_slim_founder(monkeypatch, tmp_path):
    _clear_cache()
    f3e = tmp_path / "F3E.md"
    f3e.write_text("F3E entity content", encoding="utf-8")
    founder = tmp_path / "FNDR.md"
    founder.write_text(_FOUNDER_DOC, encoding="utf-8")
    monkeypatch.setitem(cl._ENTITY_PATHS, "F3E", f3e)
    monkeypatch.setattr(cl, "_FOUNDER_PATH", founder)

    result = cl.load_context("F3E")

    assert "Static brief: portfolio principles." in result   # static head kept
    assert "TOM: secret dynamic stuff." not in result         # dynamic section dropped
    assert "knowledge base" in result                         # retrieval note present


def test_fndr_keeps_full_founder(monkeypatch, tmp_path):
    _clear_cache()
    founder = tmp_path / "FNDR.md"
    founder.write_text(_FOUNDER_DOC, encoding="utf-8")
    monkeypatch.setattr(cl, "_FOUNDER_PATH", founder)
    monkeypatch.setattr(cl, "_ENTITY_PATHS", {})

    result = cl.load_context("FNDR")

    assert "Static brief: portfolio principles." in result
    assert "TOM: secret dynamic stuff." in result             # aggregator keeps full


def test_hjrg_keeps_full_founder(monkeypatch, tmp_path):
    _clear_cache()
    founder = tmp_path / "FNDR.md"
    founder.write_text(_FOUNDER_DOC, encoding="utf-8")
    monkeypatch.setitem(cl._ENTITY_PATHS, "HJRG", tmp_path / "missing-hjrg.md")
    monkeypatch.setattr(cl, "_FOUNDER_PATH", founder)

    result = cl.load_context("HJRG")

    assert "TOM: secret dynamic stuff." in result             # HJRG is an aggregator


def test_slim_falls_back_to_full_when_marker_absent(monkeypatch, tmp_path):
    _clear_cache()
    f3e = tmp_path / "F3E.md"
    f3e.write_text("F3E entity content", encoding="utf-8")
    founder = tmp_path / "FNDR.md"
    founder.write_text("# Founder\n\nNo marker here, all static.\n", encoding="utf-8")
    monkeypatch.setitem(cl._ENTITY_PATHS, "F3E", f3e)
    monkeypatch.setattr(cl, "_FOUNDER_PATH", founder)

    result = cl.load_context("F3E")

    assert "No marker here, all static." in result            # full inject fallback


def test_lex_sub_entity_still_gets_no_founder(monkeypatch, tmp_path):
    _clear_cache()
    llc = tmp_path / "llc.md"
    llc.write_text("LLC stub content", encoding="utf-8")
    founder = tmp_path / "FNDR.md"
    founder.write_text(_FOUNDER_DOC, encoding="utf-8")
    monkeypatch.setitem(cl._ENTITY_PATHS, "LEX-LLC", llc)
    monkeypatch.setattr(cl, "_FOUNDER_PATH", founder)

    result = cl.load_context("LEX-LLC")

    assert "LLC stub content" in result
    assert "Static brief: portfolio principles." not in result  # firewall: no founder at all
    assert "TOM: secret dynamic stuff." not in result


def test_slim_founder_unit():
    out = cl._slim_founder(_FOUNDER_DOC)
    assert "Static brief: portfolio principles." in out
    assert "TOM: secret dynamic stuff." not in out
    # no marker -> returned unchanged
    assert cl._slim_founder("no marker text") == "no marker text"
