"""Repo-docs sync (DR/VM step 1, slice M3; cq-a296aa8e0a2e): copy and defaults that had
drifted from the code / the live estate must be DERIVED, not retyped.

  * the per-user rate-cap refusal in app.py names rate_limiter._USER_LIMIT (30), not a
    hardcoded "10/hour" (the 2026-08-09 architecture pass found the drift);
  * the runbook's rate-limited entry states the same number;
  * deployment/setup-backup-task.ps1's default trigger equals the LIVE backup trigger
    recorded in the committed task-estate manifest (the file said 1:00PM while the task
    ran at 20:30 since 2026-07-27 -- a re-run of the script would have moved the task);
  * the ps1 stays ASCII-only (D-016).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from cora import rate_limiter  # noqa: E402

_APP = REPO / "src" / "cora" / "app.py"
_RUNBOOK = REPO / "deployment" / "runbook.md"
_BACKUP_PS1 = REPO / "deployment" / "setup-backup-task.ps1"
_MANIFEST = REPO / "deployment" / "manifest" / "task-estate.json"


def test_app_rate_cap_copy_is_derived_from_the_constant():
    src = _APP.read_text(encoding="utf-8")
    assert re.search(r"\(\d+/hour\)", src) is None, "a hardcoded N/hour literal is back in app.py"
    sites = re.findall(r"per-user mention cap \(\{rate_limiter\._USER_LIMIT\}/hour\)", src)
    assert len(sites) == 2, f"expected the two per-user refusal sites to read the constant, found {len(sites)}"
    chan = re.findall(r"mention cap \(\{rate_limiter\._CHANNEL_LIMIT\}/hour\)", src)
    assert len(chan) == 2, f"expected the two per-channel refusal sites to read the constant, found {len(chan)}"
    assert rate_limiter._USER_LIMIT == 30 and rate_limiter._CHANNEL_LIMIT == 50  # noqa: SLF001 -- the runbook pins the same numbers


def test_runbook_states_the_enforced_cap():
    text = _RUNBOOK.read_text(encoding="utf-8")
    assert f"per-user ({rate_limiter._USER_LIMIT}/hr" in text  # noqa: SLF001
    assert "per-user (10/hr)" not in text


def test_backup_setup_default_matches_the_live_trigger():
    ps1 = _BACKUP_PS1.read_text(encoding="utf-8")
    m = re.search(r'^\$TRIGGER_TIME\s*=\s*"([^"]+)"', ps1, re.MULTILINE)
    assert m, "setup-backup-task.ps1 lost its $TRIGGER_TIME line"
    assert m.group(1) == "8:30PM"
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    backup = next(t for t in manifest["tasks"] if t["name"] == "cowork-cora-backup")
    assert backup["trigger_text"] == "daily 20:30", backup["trigger_text"]


def test_backup_setup_ps1_is_ascii_only():
    raw = _BACKUP_PS1.read_bytes()
    bad = [i for i, b in enumerate(raw) if b > 127]
    assert not bad, f"non-ASCII byte at offset {bad[0]} (D-016)"
