"""Code #15 R8 (cq-592baba613f1): the Saturday task's setup PS1 + the vendored
inventory PS1 it runs. Static assertions (ASCII-only per D-016, the safety
invariants, the pinned bytes) plus a syntax parse -- the setup script touches the
live task estate, so its mutating path is never executed here (mirrors
test_claude_mirror_schedule_ps1)."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SETUP = REPO / "deployment" / "setup-hygiene-drive-weekly-task.ps1"
VENDORED = REPO / "deployment" / "hygiene" / "folder-audit-inventory.ps1"
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

import run_hygiene_drive_weekly as rh  # noqa: E402

PINNED_SHA256 = "339d456deca9ef1c861b842895aaa07370c822ad9c01a4e9e43e504a409d7251"
FIRE_AT = "02:40"


def _code_only(path: Path) -> str:
    """PS1 text minus <# #> block comments and # line comments."""
    text = path.read_text(encoding="ascii")
    text = re.sub(r"<#.*?#>", "", text, flags=re.S)
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


@pytest.mark.parametrize("path", [SETUP, VENDORED])
def test_ascii_only(path):
    raw = path.read_bytes()
    assert all(b < 128 for b in raw), f"{path.name} must be ASCII-only (D-016)"


@pytest.mark.skipif(os.name != "nt", reason="PowerShell parser")
@pytest.mark.parametrize("path", [SETUP, VENDORED])
def test_parses(path):
    ps = (f"$ErrorActionPreference='Stop'; "
          f"$null = [System.Management.Automation.PSParser]::Tokenize("
          f"(Get-Content -Raw '{path}'), [ref]$null); 'OK'")
    proc = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0 and "OK" in proc.stdout, f"{proc.stdout}\n{proc.stderr}"


def test_setup_registers_one_weekly_saturday_trigger_windowless_apply():
    code = _code_only(SETUP)
    assert '$TaskName   = "cowork-cora-hygiene-drive-weekly"' in code
    assert f'New-ScheduledTaskTrigger -Weekly -DaysOfWeek Saturday -At "{FIRE_AT}"' in code
    assert "_task-action.ps1" in code and "New-WrappedTaskAction" in code
    assert "New-ScheduledTaskAction" not in code
    assert "run_hygiene_drive_weekly.py" in code and "--apply" in code
    assert "-RunLevel Limited" in code and "-LogonType Interactive" in code
    assert "-StartWhenAvailable" in code and "New-TimeSpan -Hours 1" in code
    assert r"C:\Users\Harri\code\cora\.venv\Scripts\python.exe" in code          # D-005
    assert "$after.Triggers.Count" in code


def test_the_fire_minute_is_unclaimed_by_every_other_script_and_the_live_estate():
    claimed = {}
    for p in (REPO / "deployment").glob("*.ps1"):
        if p == SETUP:
            continue
        src = p.read_text(encoding="utf-8", errors="replace")
        found = set(re.findall(r"-At\s+\"?'?(\d\d:\d\d)", src))
        found |= set(re.findall(r'\$(?:FireAt|HourMin)\s*=\s*"(\d\d:\d\d)"', src))
        found |= set(re.findall(r"New\s*=\s*'(\d\d:\d\d)'", src))
        if FIRE_AT in found:
            claimed[p.name] = found
    assert not claimed, f"{FIRE_AT} is claimed by {sorted(claimed)}"
    estate = (REPO / "deployment" / "manifest" / "task-estate.md").read_text(encoding="utf-8")
    rows = re.findall(r"^\| `([^`]+)` \| ([^|]+) \|", estate, flags=re.M)
    assert rows, "the committed live-registry manifest must parse, or this belt is vacuous"
    # DELIBERATE FLIP (Code #16, inherited red on main 86a45ffc): the task is now
    # REGISTERED -- 86a45ffc regenerated the manifest from the live registry with it
    # in. The belt still holds: no OTHER live task may fire at 02:40, and the
    # registered row itself must carry the scripted minute.
    others = [(name, trig) for name, trig in rows if name != "cowork-cora-hygiene-drive-weekly"]
    assert not any(FIRE_AT in trig for _, trig in others), "another live task already fires at 02:40"
    own = [trig for name, trig in rows if name == "cowork-cora-hygiene-drive-weekly"]
    assert all(FIRE_AT in trig for trig in own), own


def test_the_vendored_inventory_is_the_pinned_reviewed_bytes():
    raw = VENDORED.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == PINNED_SHA256 == rh.INVENTORY_PS1_SHA256
    assert not raw.startswith(b"\xef\xbb\xbf") and b"\r" not in raw
    assert rh.verify_ps1(VENDORED) is None


def test_gitattributes_keeps_the_vendored_bytes_verbatim():
    """text=auto would check the LF original out as CRLF on Windows and the pinned
    sha256 would refuse every Saturday run after the merge."""
    attrs = (REPO / ".gitattributes").read_text(encoding="utf-8")
    assert re.search(r"^deployment/hygiene/folder-audit-inventory\.ps1\s+-text\s*$", attrs, flags=re.M)


def test_the_vendored_inventory_keeps_its_guards_and_no_destructive_cmdlet():
    code = _code_only(VENDORED)
    assert "exit 2" in code and "$outDirIsInsideRoot" in code          # OutDir inside Root -> refuse
    assert "exit 3" in code                                            # Root missing -> refuse
    for verb in ("Remove-Item", "Move-Item", "Rename-Item", "Copy-Item", "Set-ItemProperty", "Clear-Content"):
        assert verb not in code, verb
    new_items = re.findall(r"New-Item[^\n]*", code)
    assert new_items == ["New-Item -Path $outDirNorm -ItemType Directory | Out-Null"]
    assert "[int]$MaxHashMB = 200" in code and "($bytes -le $maxBytes) -or $HashAll" in code
    for target in ("$filesCsvPath", "$dirsCsvPath", "$summaryPath", "$runLogPath"):
        assert f"Join-Path -Path $outDirNorm" in code and target in code


def test_verify_ps1_refuses_a_tampered_copy(tmp_path):
    bad = tmp_path / "inv.ps1"
    bad.write_bytes(VENDORED.read_bytes() + b"\n# edited\n")
    assert "sha256" in rh.verify_ps1(bad)
    assert "not readable" in rh.verify_ps1(tmp_path / "missing.ps1")


def test_runbook_carries_the_task_entry():
    rb = (REPO / "deployment" / "runbook.md").read_text(encoding="utf-8")
    assert "cowork-cora-hygiene-drive-weekly" in rb and "setup-hygiene-drive-weekly-task.ps1" in rb
    head, rest = rb.split("<!-- BEGIN GENERATED: task-registry -->", 1)
    gen, tail = rest.split("<!-- END GENERATED: task-registry -->", 1)
    # DELIBERATE FLIP (Code #16, inherited red on main 86a45ffc): the generated block
    # is regenerated from the LIVE registry, and the task is registered now, so it
    # carries the row. The hand-written entry must still live OUTSIDE the block.
    assert "setup-hygiene-drive-weekly-task.ps1" in head + tail, "the hand-written entry is outside the generated block"
    assert "cowork-cora-hygiene-drive-weekly" in gen and FIRE_AT in gen
