"""Regression guard: the Clover (point-of-sale) connector is retired.

Clover was retired portfolio-wide (D-027/D-032); the OSN metrics digest moved to
QBO and the OSN inventory pass was dropped (Phase 3 item C, 2026-06-17). These
tests fail loudly if the module is resurrected or a live importer sneaks back in.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent


def test_clover_client_module_is_gone():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("cora.connectors.clover_client")


def test_no_clover_client_references_in_src_or_scripts():
    """No live module under src/cora or scripts may reference clover_client."""
    offenders = []
    for base in (_REPO / "src" / "cora", _REPO / "scripts"):
        for path in base.rglob("*.py"):
            if "clover_client" in path.read_text(encoding="utf-8", errors="ignore"):
                offenders.append(str(path.relative_to(_REPO)))
    assert not offenders, (
        "clover_client is retired but still referenced by: " + ", ".join(sorted(offenders))
    )
