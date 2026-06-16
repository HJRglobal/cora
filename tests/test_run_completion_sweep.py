"""Tests for run_completion_sweep posting gate (audit N3, Phase 0.1).

Completion-sweep Slack posting is muted by default (near-zero precision); the
precision rebuild is Phase 1.5. Detection + audit still run.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import run_completion_sweep as sweep  # noqa: E402


@pytest.mark.parametrize("value", ["true", "True", "TRUE", "1", "yes", "on", " true "])
def test_post_enabled_true(monkeypatch, value):
    monkeypatch.setenv("COMPLETION_SWEEP_POST_ENABLED", value)
    assert sweep._post_enabled() is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "", "  ", "muted"])
def test_post_enabled_false(monkeypatch, value):
    monkeypatch.setenv("COMPLETION_SWEEP_POST_ENABLED", value)
    assert sweep._post_enabled() is False


def test_post_disabled_by_default(monkeypatch):
    monkeypatch.delenv("COMPLETION_SWEEP_POST_ENABLED", raising=False)
    assert sweep._post_enabled() is False
