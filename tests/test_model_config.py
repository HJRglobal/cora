"""Env-gated model selection (sonnet5-refresh, 2026-07-29).

Pins the CORA_SONNET_MODEL / CORA_OPUS_MODEL override contract and the
single-source-of-truth wiring so a future edit can't silently:
  - break the .env-line flip / rollback,
  - fork one of the derived sites back onto a hardcoded model, or
  - accidentally fold the AI-visibility measurement engine into the flip.

The defaults keep every value identical to the pre-refresh literals, so this
file is green whether or not Harrison has flipped .env.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from cora import claude_client, code_queue, model_router, strategy_memo

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src" / "cora"


# ---------------------------------------------------------------------------
# Sonnet resolver
# ---------------------------------------------------------------------------

def test_resolve_sonnet_default(monkeypatch):
    monkeypatch.delenv("CORA_SONNET_MODEL", raising=False)
    assert model_router._resolve_sonnet_model() == "claude-sonnet-4-6"


def test_resolve_sonnet_override(monkeypatch):
    monkeypatch.setenv("CORA_SONNET_MODEL", "claude-sonnet-5")
    assert model_router._resolve_sonnet_model() == "claude-sonnet-5"


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_resolve_sonnet_blank_falls_back(monkeypatch, blank):
    monkeypatch.setenv("CORA_SONNET_MODEL", blank)
    assert model_router._resolve_sonnet_model() == "claude-sonnet-4-6"


def test_resolve_sonnet_strips_whitespace(monkeypatch):
    monkeypatch.setenv("CORA_SONNET_MODEL", "  claude-sonnet-5  ")
    assert model_router._resolve_sonnet_model() == "claude-sonnet-5"


# ---------------------------------------------------------------------------
# Single source of truth -- all Cora-Sonnet sites derive from model_router
# ---------------------------------------------------------------------------

def test_single_source_values_equal():
    # In the default (offline) test env these are all claude-sonnet-4-6; after a
    # real .env flip they are all claude-sonnet-5. Either way they must MATCH --
    # a fork would mean one site was hardcoded again.
    assert claude_client._MODEL == model_router.MODEL_SONNET
    assert strategy_memo.SONNET_MODEL == model_router.MODEL_SONNET
    assert code_queue._SONNET_MODEL == model_router.MODEL_SONNET
    assert model_router.DEFAULT_MODEL == model_router.MODEL_SONNET


def test_default_env_values_are_sonnet_4_6(monkeypatch):
    # Guard the offline default explicitly (conftest never sets CORA_SONNET_MODEL).
    assert model_router.MODEL_SONNET == "claude-sonnet-4-6"
    assert claude_client._MODEL == "claude-sonnet-4-6"


def test_wiring_is_structural():
    """The derived sites must IMPORT the constant, not re-declare a literal."""
    cc = (_SRC / "claude_client.py").read_text(encoding="utf-8")
    sm = (_SRC / "strategy_memo.py").read_text(encoding="utf-8")
    cq = (_SRC / "code_queue.py").read_text(encoding="utf-8")
    for src in (cc, sm, cq):
        assert "from .model_router import MODEL_SONNET" in src
        # No stray hardcoded 4-6 literal left behind on these paths.
        assert '"claude-sonnet-4-6"' not in src
        assert "'claude-sonnet-4-6'" not in src


def test_reload_picks_up_env(monkeypatch):
    """MODEL_SONNET is resolved at import -- a fresh process (restart) sees the
    override. Reload in isolation, then restore so downstream tests are unaffected."""
    monkeypatch.setenv("CORA_SONNET_MODEL", "claude-sonnet-5-canarytest")
    try:
        importlib.reload(model_router)
        assert model_router.MODEL_SONNET == "claude-sonnet-5-canarytest"
        assert model_router.DEFAULT_MODEL == "claude-sonnet-5-canarytest"
    finally:
        monkeypatch.delenv("CORA_SONNET_MODEL", raising=False)
        importlib.reload(model_router)
        assert model_router.MODEL_SONNET == "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# AI-visibility engine stays INDEPENDENT (its own knob, not the flip)
# ---------------------------------------------------------------------------

def test_ai_search_is_not_folded_into_the_flip():
    ai = (_SRC / "connectors" / "ai_search.py").read_text(encoding="utf-8")
    # The measurement engine keeps AI_VIS_CLAUDE_MODEL and must NOT read the
    # shared Cora flip var -- changing it would silently change what's measured.
    assert "AI_VIS_CLAUDE_MODEL" in ai
    assert "CORA_SONNET_MODEL" not in ai
