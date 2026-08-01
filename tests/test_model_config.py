"""Env-gated model selection (sonnet5-refresh, 2026-07-29).

Pins the CORA_SONNET_MODEL / CORA_OPUS_MODEL override contract and the
single-source-of-truth wiring so a future edit can't silently:
  - break the .env-line flip / rollback,
  - fork one of the derived sites back onto a hardcoded model, or
  - accidentally fold the AI-visibility measurement engine into the flip.

Assertions test the RESOLVER + the literal default constant, never the import-time
constant against a fixed string, so this file stays green whether or not Harrison has
flipped CORA_SONNET_MODEL in the real .env (D-051 remediation).
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


def test_code_default_is_sonnet_4_6():
    # The FLIP is opt-in: the code default (used when CORA_SONNET_MODEL is unset) must
    # stay 4.6, so a merge never silently moves the hot path to 5. This asserts the literal
    # default constant + the resolver-with-no-env, NOT the import-time constant -- so it
    # stays green after Harrison flips the real .env (that's a config change, not a code one).
    assert model_router._SONNET_DEFAULT == "claude-sonnet-4-6"


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
    original = model_router.MODEL_SONNET
    monkeypatch.setenv("CORA_SONNET_MODEL", "claude-sonnet-5-canarytest")
    try:
        importlib.reload(model_router)
        assert model_router.MODEL_SONNET == "claude-sonnet-5-canarytest"
        assert model_router.DEFAULT_MODEL == "claude-sonnet-5-canarytest"
    finally:
        # Restore the exact pre-test resolution by PINNING the original
        # import-time value through the reload (monkeypatch teardown then
        # returns the process env to its true prior state). The old restore
        # (delenv + reload, trusting the module's repo-root-anchored
        # load_dotenv to re-establish the flip) broke in git worktrees: no
        # .env at the worktree root, so the reload fell back to the code
        # default while every already-collected test module still held the
        # collection-time constant -- 49 order-dependent failures downstream
        # in test_model_router. Pinning is flip-safe AND checkout-agnostic.
        monkeypatch.setenv("CORA_SONNET_MODEL", original)
        importlib.reload(model_router)
        assert model_router.MODEL_SONNET == original
        assert model_router.DEFAULT_MODEL == original


# ---------------------------------------------------------------------------
# AI-visibility engine stays INDEPENDENT (its own knob, not the flip)
# ---------------------------------------------------------------------------

def test_ai_search_is_not_folded_into_the_flip():
    ai = (_SRC / "connectors" / "ai_search.py").read_text(encoding="utf-8")
    # The measurement engine keeps AI_VIS_CLAUDE_MODEL and must NOT read the
    # shared Cora flip var -- changing it would silently change what's measured.
    assert "AI_VIS_CLAUDE_MODEL" in ai
    assert "CORA_SONNET_MODEL" not in ai


# ---------------------------------------------------------------------------
# Opus resolver (sales-deck tool) -- default IS the bump (claude-opus-5)
# ---------------------------------------------------------------------------

def test_resolve_opus_default(monkeypatch):
    from cora.tools import sales_deck_client
    monkeypatch.delenv("CORA_OPUS_MODEL", raising=False)
    assert sales_deck_client._resolve_opus_model() == "claude-opus-5"


def test_resolve_opus_override_rollback(monkeypatch):
    from cora.tools import sales_deck_client
    monkeypatch.setenv("CORA_OPUS_MODEL", "claude-opus-4-7")
    assert sales_deck_client._resolve_opus_model() == "claude-opus-4-7"


@pytest.mark.parametrize("blank", ["", "   "])
def test_resolve_opus_blank_falls_back(monkeypatch, blank):
    from cora.tools import sales_deck_client
    monkeypatch.setenv("CORA_OPUS_MODEL", blank)
    assert sales_deck_client._resolve_opus_model() == "claude-opus-5"


def test_opus_wiring_structural():
    """The sales-deck call must use the env-gated constant, not a literal, and the
    old opus-4-7 literal must be gone from the call path."""
    sdc = (_SRC / "tools" / "sales_deck_client.py").read_text(encoding="utf-8")
    assert 'os.environ.get("CORA_OPUS_MODEL"' in sdc
    assert "model=_OPUS_MODEL" in sdc
    assert '"claude-opus-4-7"' not in sdc  # no hardcoded old model on the call path


def test_opus_call_has_no_sampling_params():
    """Sonnet-5/Opus-5 reject non-default temperature/top_p/top_k. Pin that the
    sales-deck create() call carries none (guards a future accidental add). thinking is
    the ONE allowed param (disabled) -- excluded from the banned set below."""
    sdc = (_SRC / "tools" / "sales_deck_client.py").read_text(encoding="utf-8")
    # Locate the messages.create(...) block for the deck synthesis call.
    idx = sdc.index("model=_OPUS_MODEL")
    window = sdc[idx: idx + 400]
    for banned in ("temperature", "top_p", "top_k", "budget_tokens"):
        assert banned not in window, f"unexpected {banned} on the opus call"


# ---------------------------------------------------------------------------
# D-051: every flippable Sonnet/Opus call site MUST disable thinking
# ---------------------------------------------------------------------------
# Sonnet 5 / Opus 5 run adaptive thinking by default and share the max_tokens budget
# with the visible answer, so a call that omits `thinking` truncates/blanks after the
# flip (silent, no exception). Each site below resolves to the flippable model and must
# carry thinking={"type":"disabled"}. Windowed so a NEW call in one of these files is
# caught too. The Haiku call in code_queue (model=_HAIKU_MODEL) is intentionally not a
# marker -- Haiku is not flipped and defaults to no thinking.
_FLIPPABLE_CALL_SITES = [
    ("claude_client.py", "model=effective_model"),
    ("strategy_memo.py", "model=SONNET_MODEL"),
    ("code_queue.py", "model=_SONNET_MODEL"),
    # channel_synthesis builds a shared params DICT (batch pilot slice 3), so
    # the marker is the dict form -- same invariant, same window check.
    ("channel_synthesis.py", '"model": sm.SONNET_MODEL'),
    ("tools/person_dossier.py", "model=_SYNTH_MODEL"),
    ("tools/sales_deck_client.py", "model=_OPUS_MODEL"),
]


@pytest.mark.parametrize("relpath,marker", _FLIPPABLE_CALL_SITES)
def test_flippable_call_sites_disable_thinking(relpath, marker):
    lines = (_SRC / relpath).read_text(encoding="utf-8").splitlines()
    hits = [i for i, ln in enumerate(lines) if marker in ln]
    assert hits, f"marker {marker!r} not found in {relpath} -- call site moved/renamed?"
    for i in hits:
        window = "\n".join(lines[max(0, i - 3): i + 8])
        assert '"type": "disabled"' in window or "_THINKING_DISABLED" in window, (
            f"{relpath}: the {marker!r} call near line {i + 1} does not disable thinking "
            f"-- it will truncate/blank on a Sonnet-5/Opus-5 flip (D-051)"
        )
