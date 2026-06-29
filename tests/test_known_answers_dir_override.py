"""Drive-materialization (2026-06-29) — KNOWN_ANSWERS_DIR / DYNAMIC_ANSWERS_DIR overrides.

context_loader reads the known-answers store at MODULE IMPORT, so the override is
verified by reloading the module with the env set (mirrors how a bot restart picks it
up). The read side must follow KNOWN_ANSWERS_DIR exactly the way the gap_autofill write
side already does, so a fact Cora writes lands where context_loader (and Tag) read it.

dynamic_answers must NOT be coupled to KNOWN_ANSWERS_DIR (its snapshot writer still
targets the repo); it follows its own DYNAMIC_ANSWERS_DIR, default repo.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import context_loader as cl  # noqa: E402
from cora import dynamic_answers as da  # noqa: E402


@pytest.fixture()
def restore_modules():
    """Reload context_loader + dynamic_answers AFTER the test so a reload done with a
    monkeypatched env can't leave the override bleeding into later tests."""
    yield
    importlib.reload(cl)
    importlib.reload(da)


def test_context_loader_known_answers_dir_follows_env(tmp_path, monkeypatch, restore_modules):
    drive_dir = tmp_path / "_brain" / "known-answers"
    monkeypatch.setenv("KNOWN_ANSWERS_DIR", str(drive_dir))
    reloaded = importlib.reload(cl)
    assert reloaded._KNOWN_ANSWERS_DIR == drive_dir
    # the per-entity read map is rebuilt from the overridden dir
    assert reloaded._KNOWN_ANSWERS_PATHS["F3E"] == drive_dir / "f3e.md"
    assert reloaded._KNOWN_ANSWERS_PATHS["FNDR"] == drive_dir / "fndr.md"
    # LEX sub-entity firewall preserved: sub-entities still excluded from the read map
    assert "LEX-LLC" not in reloaded._KNOWN_ANSWERS_PATHS
    assert "LEX-LBHS" not in reloaded._KNOWN_ANSWERS_PATHS


def test_context_loader_defaults_to_repo_when_unset(monkeypatch, restore_modules):
    monkeypatch.delenv("KNOWN_ANSWERS_DIR", raising=False)
    reloaded = importlib.reload(cl)
    assert reloaded._KNOWN_ANSWERS_DIR == reloaded._REPO_ROOT / "design" / "known-answers"


def test_dynamic_answers_not_coupled_to_known_answers_dir(tmp_path, monkeypatch, restore_modules):
    # Only KNOWN_ANSWERS_DIR is set — dynamic must stay on the repo default, NOT follow it.
    monkeypatch.setenv("KNOWN_ANSWERS_DIR", str(tmp_path / "_brain" / "known-answers"))
    monkeypatch.delenv("DYNAMIC_ANSWERS_DIR", raising=False)
    reloaded = importlib.reload(da)
    assert reloaded._DYNAMIC_DIR == reloaded._REPO_ROOT / "design" / "known-answers" / "dynamic"


def test_dynamic_answers_dir_follows_its_own_env(tmp_path, monkeypatch, restore_modules):
    dyn = tmp_path / "dyn"
    monkeypatch.setenv("DYNAMIC_ANSWERS_DIR", str(dyn))
    reloaded = importlib.reload(da)
    assert reloaded._DYNAMIC_DIR == dyn
