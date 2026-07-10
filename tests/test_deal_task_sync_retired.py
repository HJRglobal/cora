"""Regression guard: the HubSpot deal-task-sync path is retired.

The proposal-stage -> Asana task sync was migrated to Make.com scenario 4768886
(D-029); the host task "Cora - Deal Task Sync" was disabled 2026-07-08 (audit
MK-01) and the code path was retired in audit-v2 Slice 07 (2026-07-09). These
tests fail loudly if the script, its setup task, or a live reference is
resurrected.

Adapted from tests/test_clover_retired.py. deal-task-sync was a standalone
scripts/ script (never an importable cora.* module), so the guard asserts
file-absence + no live references rather than importlib raising.

NOTE (verified in Slice 07): scripts/run_deal_task_sync.py was only a READER of
data/hubspot_deal_snapshots.db. That DB's sole writer is the separate
scripts/run_hubspot_deal_monitor.py, which is NOT retired here -- so the shared
modules (cora.tools.asana_client, cora.tools.project_resolver) and the monitor
script are intentionally left intact and are NOT asserted absent.
"""

from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent


def test_deal_task_sync_script_is_gone():
    """The retired sync script must not exist."""
    assert not (_REPO / "scripts" / "run_deal_task_sync.py").exists(), (
        "run_deal_task_sync.py is retired (Slice 07) but reappeared under scripts/"
    )


def test_deal_task_sync_setup_ps1_is_gone():
    """The task-registration PS1 must not exist (a removed task must not be re-registrable)."""
    assert not (_REPO / "deployment" / "setup-deal-task-sync-task.ps1").exists(), (
        "setup-deal-task-sync-task.ps1 is retired (Slice 07) but reappeared under deployment/"
    )


def test_old_deal_task_sync_test_is_gone():
    """The old behavioral test imported the retired script and must not exist."""
    assert not (_REPO / "tests" / "test_deal_task_sync.py").exists(), (
        "tests/test_deal_task_sync.py imported the retired script but reappeared"
    )


def test_no_run_deal_task_sync_references_in_src_or_scripts():
    """No live module under src/cora or scripts may reference the retired script."""
    offenders = []
    for base in (_REPO / "src" / "cora", _REPO / "scripts"):
        for path in base.rglob("*.py"):
            if "run_deal_task_sync" in path.read_text(encoding="utf-8", errors="ignore"):
                offenders.append(str(path.relative_to(_REPO)))
    assert not offenders, (
        "run_deal_task_sync is retired but still referenced by: "
        + ", ".join(sorted(offenders))
    )


def test_no_deal_task_sync_ps1_or_task_resurrected():
    """No deployment/scripts .ps1 may re-register the retired Deal Task Sync task."""
    offenders = []
    for base in (_REPO / "deployment", _REPO / "scripts"):
        if not base.exists():
            continue
        for path in base.rglob("*.ps1"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "run_deal_task_sync" in text or "setup-deal-task-sync-task" in text:
                offenders.append(str(path.relative_to(_REPO)))
    assert not offenders, (
        "Deal Task Sync task/script resurrected in: " + ", ".join(sorted(offenders))
    )


def test_no_ps1_reregisters_task_by_display_name():
    """No deployment/scripts .ps1 may re-register the task by its display name.

    Closes the resurrection vector the filename checks miss (D-051 review): a PS1
    that does `Register-ScheduledTask -TaskName "Cora - Deal Task Sync"` pointed
    at a differently-named script would evade the run_deal_task_sync/PS1-name
    guards while silently reviving the retired task. The display name is allowed
    ONLY in data/maps/scheduled-task-state.yaml (the delist reminder), which is
    not scanned here.
    """
    offenders = []
    for base in (_REPO / "deployment", _REPO / "scripts"):
        if not base.exists():
            continue
        for path in base.rglob("*.ps1"):
            if "Cora - Deal Task Sync" in path.read_text(encoding="utf-8", errors="ignore"):
                offenders.append(str(path.relative_to(_REPO)))
    assert not offenders, (
        "Deal Task Sync re-registered by display name in: " + ", ".join(sorted(offenders))
    )
