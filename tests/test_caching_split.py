"""Tests for the caching split (3-block system array + context_loader parts).

Covers:
  - claude_client._build_cached_system: 3-block shape + breakpoint placement,
    and byte-identical 2-block back-compat when static_context is falsy.
  - context_loader.load_context_parts: static vs KB separation.
  - context_loader.load_context: byte-identical to the legacy single-string
    contract, and the no-query cache-identity invariant.
  - sub-entity firewall preserved through load_context_parts (LEX-sub static
    excludes founder content).
"""

import cora.claude_client as cc
import cora.context_loader as ctx


# --------------------------------------------------------------------------- #
# _build_cached_system
# --------------------------------------------------------------------------- #

def test_build_cached_system_two_block_backcompat_when_no_static():
    blocks = cc._build_cached_system("SYS", "CTX")
    assert len(blocks) == 2
    # block 1 cached, block 2 not
    assert blocks[0]["text"] == "SYS"
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert blocks[1]["text"] == "\n\n---\n\n# Context\n\nCTX"
    assert "cache_control" not in blocks[1]


def test_build_cached_system_none_equals_omitted():
    # Passing static_context=None must be byte-identical to the 2-arg call.
    assert cc._build_cached_system("SYS", "CTX", static_context=None) == \
        cc._build_cached_system("SYS", "CTX")


def test_build_cached_system_empty_static_falls_back_to_two_block():
    # Empty string is falsy -> 2-block path (block 2 would be below the min
    # cacheable size anyway; no point in a no-op breakpoint).
    blocks = cc._build_cached_system("SYS", "CTX", static_context="")
    assert len(blocks) == 2


def test_build_cached_system_three_block_shape_and_breakpoints():
    blocks = cc._build_cached_system("SYS", "VOL", static_context="STATIC")
    assert len(blocks) == 3
    # block 1 (prompt) cached
    assert blocks[0]["text"] == "SYS"
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    # block 2 (static portfolio context) cached
    assert "STATIC" in blocks[1]["text"]
    assert blocks[1]["cache_control"] == {"type": "ephemeral"}
    # block 3 (volatile) NOT cached
    assert "VOL" in blocks[2]["text"]
    assert "cache_control" not in blocks[2]


def test_build_cached_system_three_block_keeps_content_distinct():
    blocks = cc._build_cached_system("SYS", "VOLATILE_ONLY", static_context="STATIC_ONLY")
    # static must not leak into the volatile block and vice-versa
    assert "VOLATILE_ONLY" not in blocks[1]["text"]
    assert "STATIC_ONLY" not in blocks[2]["text"]


# --------------------------------------------------------------------------- #
# load_context_parts / load_context
# --------------------------------------------------------------------------- #

def test_load_context_parts_returns_static_and_kb_separately(monkeypatch):
    monkeypatch.setattr(ctx, "_load_static_context", lambda e: "STATIC")
    monkeypatch.setattr(ctx, "_try_kb_retrieve", lambda *a, **k: "KBCHUNKS")
    static, kb = ctx.load_context_parts("F3E", query="how are sales?")
    assert static == "STATIC"
    assert kb == "KBCHUNKS"


def test_load_context_parts_no_query_returns_empty_kb(monkeypatch):
    monkeypatch.setattr(ctx, "_load_static_context", lambda e: "STATIC")
    called = {"kb": False}

    def _kb(*a, **k):
        called["kb"] = True
        return "SHOULD_NOT_APPEAR"

    monkeypatch.setattr(ctx, "_try_kb_retrieve", _kb)
    static, kb = ctx.load_context_parts("F3E")
    assert static == "STATIC"
    assert kb == ""
    assert called["kb"] is False  # KB retrieval skipped entirely


def test_load_context_parts_skip_kb_short_circuits(monkeypatch):
    monkeypatch.setattr(ctx, "_load_static_context", lambda e: "STATIC")
    called = {"kb": False}

    def _kb(*a, **k):
        called["kb"] = True
        return "X"

    monkeypatch.setattr(ctx, "_try_kb_retrieve", _kb)
    static, kb = ctx.load_context_parts("F3E", query="q", skip_kb=True)
    assert (static, kb) == ("STATIC", "")
    assert called["kb"] is False


def test_load_context_parts_kb_none_becomes_empty_string(monkeypatch):
    monkeypatch.setattr(ctx, "_load_static_context", lambda e: "STATIC")
    monkeypatch.setattr(ctx, "_try_kb_retrieve", lambda *a, **k: None)
    static, kb = ctx.load_context_parts("F3E", query="q")
    assert kb == ""


def test_load_context_byte_identical_with_kb(monkeypatch):
    # The legacy contract: static + separator + kb, joined exactly as before.
    monkeypatch.setattr(ctx, "_load_static_context", lambda e: "STATIC")
    monkeypatch.setattr(ctx, "_try_kb_retrieve", lambda *a, **k: "KBCHUNKS")
    assert ctx.load_context("F3E", query="q") == "STATIC\n\n---\n\nKBCHUNKS"


def test_load_context_byte_identical_no_kb(monkeypatch):
    monkeypatch.setattr(ctx, "_load_static_context", lambda e: "STATIC")
    monkeypatch.setattr(ctx, "_try_kb_retrieve", lambda *a, **k: None)
    assert ctx.load_context("F3E", query="q") == "STATIC"


def test_load_context_no_query_returns_cached_static_object(monkeypatch):
    # Cache-identity invariant (see test_context_loader): with no query the
    # wrapper must return the exact static object, not a re-joined copy.
    sentinel = "STATIC_SENTINEL"
    monkeypatch.setattr(ctx, "_load_static_context", lambda e: sentinel)
    result = ctx.load_context("FNDR")
    assert result is sentinel


# --------------------------------------------------------------------------- #
# sub-entity firewall preserved
# --------------------------------------------------------------------------- #

def test_load_context_parts_lex_sub_excludes_founder(monkeypatch, tmp_path):
    ctx._cache.clear()

    llc_stub = tmp_path / "llc.md"
    llc_stub.write_text("LLC sub-entity stub content", encoding="utf-8")

    founder_path = tmp_path / "FNDR.md"
    founder_path.write_text("FOUNDER CROSS ENTITY CAP TABLE", encoding="utf-8")

    monkeypatch.setitem(ctx._ENTITY_PATHS, "LEX-LLC", llc_stub)
    monkeypatch.setattr(ctx, "_FOUNDER_PATH", founder_path)

    static, kb = ctx.load_context_parts("LEX-LLC")
    assert "LLC sub-entity stub content" in static
    assert "FOUNDER CROSS ENTITY CAP TABLE" not in static  # firewall holds
    assert kb == ""
