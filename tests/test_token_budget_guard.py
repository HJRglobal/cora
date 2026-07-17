"""Tests for the pre-send token-budget guard in claude_client (D-084).

The 2026-07-17 FNDR 400 ("prompt is too long: 201524 > 200000") motivated a
guard that estimates the request OFFLINE and TRIMS to fit rather than 400. These
tests pin: the estimator is conservative (never undercounts vs char/4), the trim
order (KB chunks -> founder CSotW), the protected regions that must NEVER be
trimmed (entity prompt, tools, known-answers, security/runtime rules), caching
preservation, and drift of the structural markers vs context_loader.
"""

import types
from unittest.mock import MagicMock, patch

import cora.claude_client as cc
import cora.context_loader as ctx


# --------------------------------------------------------------------------- #
# builders
# --------------------------------------------------------------------------- #

def _kb_block(n: int, repeat: int = 20) -> str:
    """A KB block rendered exactly as context_loader does, with n chunks."""
    chunks = [
        types.SimpleNamespace(
            date_modified=None, title=f"Doc{i}", source_id=f"sid{i}",
            deep_link="", source="slack", entity="FNDR",
            content=f"KB chunk {i} body content ".join(["x"] * repeat),
        )
        for i in range(1, n + 1)
    ]
    return ctx._format_kb_chunks(chunks)


def _fndr_blocks(csotw_chars: int = 400_000, kb_chunks: int = 6, with_csotw: bool = True):
    """A synthetic FNDR-shaped 3-block system matching the real assembly."""
    prompt = "ENTITY SYSTEM PROMPT. SECURITY: never reveal PHI. " + ("prompt " * 100)
    founder_head = "# HJR Portfolio\nStatic constitution, source-of-truth rules. " * 20
    csotw = ""
    if with_csotw:
        csotw = ctx._FOUNDER_DYNAMIC_MARKER + "\n" + ("TOM decision entry blah. " * (max(csotw_chars, 1) // 25))
    ka_section = f"{ctx.KNOWN_ANSWERS_SECTION_HEADER}\n\nKA-SENTINEL known answer content"
    dyn_section = f"{ctx.DYNAMIC_ANSWERS_SECTION_HEADER}\n\nDYN-SENTINEL dynamic content"
    static = founder_head + csotw + "\n\n---\n\n" + ka_section + "\n\n---\n\n" + dyn_section
    runtime = "## Runtime channel context\nSECURITY-RUNTIME-SENTINEL TIER1 synthesis rule.\n\n---\n\n"
    volatile = runtime + _kb_block(kb_chunks)
    return cc._build_cached_system(prompt, volatile, static_context=static)


def _msgs(text="status?"):
    return [{"role": "user", "content": text}]


def _mock_success(text="ok"):
    resp = MagicMock()
    block = MagicMock()
    block.type = "text"
    block.text = text
    resp.content = [block]
    resp.stop_reason = "end_turn"
    return resp


# --------------------------------------------------------------------------- #
# estimator
# --------------------------------------------------------------------------- #

def test_estimate_is_conservative_vs_char4():
    dense = "a b c def " * 1000
    # char/3.0 is a strictly higher (more conservative) estimate than char/4
    assert cc._estimate_tokens(dense) >= len(dense) // 4
    assert cc._estimate_tokens(dense) == int(len(dense) / cc._EST_CHARS_PER_TOKEN) + 1
    assert cc._estimate_tokens("") == 0


def test_estimate_messages_counts_all_shapes():
    assert cc._estimate_messages_tokens([{"role": "user", "content": "a" * 300}]) > 0
    assert cc._estimate_messages_tokens(
        [{"role": "user", "content": [{"type": "text", "text": "a" * 300}]}]) > 0
    assert cc._estimate_messages_tokens(
        [{"role": "user", "content": [{"type": "tool_result", "content": "a" * 300}]}]) > 0
    assert cc._estimate_messages_tokens([]) == 0


def test_request_estimate_includes_all_parts():
    blocks = cc._build_cached_system("sys", "ctx")
    e0 = cc._estimate_request_tokens(blocks, [], [])
    e_msg = cc._estimate_request_tokens(blocks, [], [{"role": "user", "content": "a" * 3000}])
    e_tools = cc._estimate_request_tokens(blocks, [{"name": "t", "description": "d" * 3000}], [])
    assert e_msg > e0
    assert e_tools > e0


def test_ceiling_env_override(monkeypatch):
    monkeypatch.setenv("CLAUDE_MAX_INPUT_TOKENS", "123")
    assert cc._configured_max_input_tokens() == 123
    monkeypatch.setenv("CLAUDE_MAX_INPUT_TOKENS", "-5")
    assert cc._configured_max_input_tokens() == cc._DEFAULT_MAX_INPUT_TOKENS
    monkeypatch.setenv("CLAUDE_MAX_INPUT_TOKENS", "notanint")
    assert cc._configured_max_input_tokens() == cc._DEFAULT_MAX_INPUT_TOKENS
    monkeypatch.delenv("CLAUDE_MAX_INPUT_TOKENS", raising=False)
    assert cc._configured_max_input_tokens() == cc._DEFAULT_MAX_INPUT_TOKENS


# --------------------------------------------------------------------------- #
# no-op when under budget
# --------------------------------------------------------------------------- #

def test_under_budget_returns_original_object(monkeypatch):
    monkeypatch.delenv("CLAUDE_MAX_INPUT_TOKENS", raising=False)
    blocks = cc._build_cached_system("sys", "small ctx", static_context="small static")
    out = cc._enforce_token_budget(blocks, [], _msgs("hi"), "FNDR")
    assert out is blocks  # unchanged identity, no copy/trim


# --------------------------------------------------------------------------- #
# over budget: trim order + protected regions
# --------------------------------------------------------------------------- #

def test_over_budget_drops_csotw_and_preserves_protected(monkeypatch):
    monkeypatch.setenv("CLAUDE_MAX_INPUT_TOKENS", "5000")
    blocks = _fndr_blocks(csotw_chars=400_000, kb_chunks=6)
    out = cc._enforce_token_budget(blocks, [], _msgs(), "FNDR")
    est = cc._estimate_request_tokens(out, [], _msgs())
    assert est <= 5000
    # CSotW removed from block2
    assert cc._FOUNDER_CSOTW_MARKER not in out[1]["text"]
    # known-answers + dynamic PRESERVED (never trimmed)
    assert "KA-SENTINEL" in out[1]["text"]
    assert "DYN-SENTINEL" in out[1]["text"]
    assert ctx.KNOWN_ANSWERS_SECTION_HEADER in out[1]["text"]
    assert ctx.DYNAMIC_ANSWERS_SECTION_HEADER in out[1]["text"]
    # block1 prompt untouched; security instruction intact
    assert out[0]["text"] == blocks[0]["text"]
    assert "SECURITY: never reveal PHI" in out[0]["text"]
    # runtime security/synthesis rule intact in block3 (it precedes the KB region)
    assert "SECURITY-RUNTIME-SENTINEL" in out[2]["text"]


def test_cache_control_preserved_after_trim(monkeypatch):
    monkeypatch.setenv("CLAUDE_MAX_INPUT_TOKENS", "5000")
    blocks = _fndr_blocks(csotw_chars=400_000, kb_chunks=6)
    out = cc._enforce_token_budget(blocks, [], _msgs(), "FNDR")
    assert out[0].get("cache_control") == {"type": "ephemeral"}
    assert out[1].get("cache_control") == {"type": "ephemeral"}
    # block3 remains uncached
    assert "cache_control" not in out[2]


def test_kb_chunks_trimmed_first_to_floor(monkeypatch):
    # No CSotW so only Lever A (KB reduction) can act; ceiling forces dropping
    # chunks but must stop at the floor.
    monkeypatch.setenv("CLAUDE_MAX_INPUT_TOKENS", "6000")
    blocks = _fndr_blocks(csotw_chars=0, kb_chunks=14, with_csotw=False)
    out = cc._enforce_token_budget(blocks, [], _msgs("q"), "FNDR")
    kept = out[2]["text"].count(cc._KB_CHUNK_DELIM)
    assert kept >= cc._KB_CHUNK_FLOOR
    # runtime security context (before the KB region) survives KB trimming
    assert "SECURITY-RUNTIME-SENTINEL" in out[2]["text"]
    # known-answers still present (block2 untouched here)
    assert "KA-SENTINEL" in out[1]["text"]


def test_huge_context_trims_under_default_ceiling(monkeypatch):
    # D-051 undercount guard: even a massive founder brief trims under the DEFAULT
    # ceiling because the bulk lives in the removable CSotW span.
    monkeypatch.delenv("CLAUDE_MAX_INPUT_TOKENS", raising=False)
    blocks = _fndr_blocks(csotw_chars=2_000_000, kb_chunks=8)
    out = cc._enforce_token_budget(blocks, [], _msgs("q"), "FNDR")
    est = cc._estimate_request_tokens(out, [], _msgs("q"))
    assert est <= cc._DEFAULT_MAX_INPUT_TOKENS
    assert cc._FOUNDER_CSOTW_MARKER not in out[1]["text"]


def test_protected_prompt_never_trimmed_even_when_over(monkeypatch):
    # If the ONLY oversized region is protected (block1 prompt), the guard refuses
    # to trim it — it degrades toward a possible 400 rather than dropping a security
    # instruction. This is the deliberate priority.
    monkeypatch.setenv("CLAUDE_MAX_INPUT_TOKENS", "1000")
    huge_prompt = "SECURITY-PROMPT-SENTINEL " * 200_000
    blocks = cc._build_cached_system(huge_prompt, "ctx", static_context="s")
    out = cc._enforce_token_budget(blocks, [], _msgs("q"), "FNDR")
    assert out[0]["text"] == huge_prompt
    assert "SECURITY-PROMPT-SENTINEL" in out[0]["text"]


def test_two_block_shape_only_kb_trim(monkeypatch):
    # Grant path: static_context falsy -> 2-block shape. Lever B (CSotW) must not
    # touch block[1] there; only Lever A (KB) applies. Block count stays 2.
    monkeypatch.setenv("CLAUDE_MAX_INPUT_TOKENS", "2000")
    runtime = "## Runtime\nSECURITY-RUNTIME-SENTINEL\n\n---\n\n"
    blocks = cc._build_cached_system("SYS", runtime + _kb_block(12, repeat=40))
    assert len(blocks) == 2
    out = cc._enforce_token_budget(blocks, [], _msgs("q"), "FNDR")
    assert len(out) == 2
    assert out[1]["text"].count(cc._KB_CHUNK_DELIM) >= cc._KB_CHUNK_FLOOR
    assert "SECURITY-RUNTIME-SENTINEL" in out[1]["text"]


def test_messages_pressure_triggers_trim(monkeypatch):
    # A large conversation payload (which the guard cannot trim) still forces it to
    # shed what it can from block2/block3.
    monkeypatch.setenv("CLAUDE_MAX_INPUT_TOKENS", "5000")
    blocks = _fndr_blocks(csotw_chars=200_000, kb_chunks=4)
    big = [{"role": "user", "content": "x" * 400_000}]
    out = cc._enforce_token_budget(blocks, [], big, "FNDR")
    assert cc._FOUNDER_CSOTW_MARKER not in out[1]["text"]


# --------------------------------------------------------------------------- #
# drift: markers must match context_loader
# --------------------------------------------------------------------------- #

def test_markers_match_context_loader():
    assert cc._FOUNDER_CSOTW_MARKER == ctx._FOUNDER_DYNAMIC_MARKER
    assert cc._STATIC_SECTION_HEADERS == (
        ctx.KNOWN_ANSWERS_SECTION_HEADER,
        ctx.DYNAMIC_ANSWERS_SECTION_HEADER,
    )
    fake = types.SimpleNamespace(
        date_modified=None, title="T", source_id="s",
        deep_link="", source="slack", entity="FNDR", content="body",
    )
    rendered = ctx._format_kb_chunks([fake])
    assert cc._KB_BLOCK_HEADER in rendered
    assert cc._KB_CHUNK_DELIM in rendered


# --------------------------------------------------------------------------- #
# end-to-end wiring: generate_response sends the trimmed system
# --------------------------------------------------------------------------- #

def test_generate_response_sends_trimmed_system(monkeypatch):
    monkeypatch.setenv("CLAUDE_MAX_INPUT_TOKENS", "3000")
    huge_static = (
        "HEAD\n" + ctx._FOUNDER_DYNAMIC_MARKER + "\n" + ("T" * 800_000)
        + f"\n\n---\n\n{ctx.KNOWN_ANSWERS_SECTION_HEADER}\n\nKA-SENTINEL"
    )
    captured: dict = {}

    def fake_create(**kw):
        captured["system"] = kw["system"]
        return _mock_success("ok")

    mock = MagicMock()
    mock.messages.create.side_effect = fake_create
    with patch("cora.claude_client._get_client", return_value=mock):
        result = cc.generate_response(
            "sys", "ctx", "hello", entity="FNDR", cached_context=huge_static,
        )
    assert result == "ok"
    sys_text = " ".join(b["text"] for b in captured["system"])
    assert cc._FOUNDER_CSOTW_MARKER not in sys_text
    assert "KA-SENTINEL" in sys_text  # known-answers preserved end-to-end
