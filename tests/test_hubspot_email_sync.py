"""Tests for the hubspot_email_sync connector DM gate (audit N6, Phase 0.1).

The ambiguous-match "confirm attachment / no active deals" DM prompts were
relentless and memory-less. Phase 0.1 gates them OFF by default behind
CORA_HUBSPOT_EMAIL_SYNC_DM_ENABLED; full Alex+Tommy scoping lands in Phase 1.8
(where the sync_user behavioral tests will be added).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora.connectors import hubspot_email_sync as sync  # noqa: E402


@pytest.mark.parametrize("value", ["true", "True", "TRUE", "1", "yes", "on", " true "])
def test_dm_prompts_enabled_true(monkeypatch, value):
    monkeypatch.setenv("CORA_HUBSPOT_EMAIL_SYNC_DM_ENABLED", value)
    assert sync._dm_prompts_enabled() is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "", "  ", "disabled"])
def test_dm_prompts_enabled_false(monkeypatch, value):
    monkeypatch.setenv("CORA_HUBSPOT_EMAIL_SYNC_DM_ENABLED", value)
    assert sync._dm_prompts_enabled() is False


def test_dm_prompts_disabled_by_default(monkeypatch):
    monkeypatch.delenv("CORA_HUBSPOT_EMAIL_SYNC_DM_ENABLED", raising=False)
    assert sync._dm_prompts_enabled() is False
