r"""Tests for deployment/run_hidden.py -- the windowless task launcher.

The launcher sits in front of EVERY Cora scheduled task, so a defect here takes
the whole estate down at once. These tests pin the four properties the estate
depends on:

  1. the child's exit code reaches Task Scheduler unchanged (Last Result),
  2. child output lands in logs\tasks\<slug>-<date>.log,
  3. a launcher-side failure is distinguishable from a script failure,
  4. the module never writes to stdout (under pythonw.exe sys.stdout is None,
     so a single print() would break every task).

Every test redirects the module's log directory into tmp_path -- the launcher
writes under the repo's logs\tasks, and a test must never pollute it.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER_PATH = REPO_ROOT / "deployment" / "run_hidden.py"
REWRAP_PATH = REPO_ROOT / "deployment" / "rewrap-tasks-hidden.ps1"


def _load_launcher():
    """Import run_hidden.py by path -- deployment/ is not a package."""
    spec = importlib.util.spec_from_file_location("cora_run_hidden", LAUNCHER_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


run_hidden = _load_launcher()


@pytest.fixture()
def logs(tmp_path, monkeypatch):
    """Redirect the launcher's log directory into tmp_path."""
    d = tmp_path / "tasks"
    monkeypatch.setattr(run_hidden, "TASK_LOG_DIR", d)
    monkeypatch.setattr(run_hidden, "LAUNCHER_ERROR_LOG", d / "_launcher-errors.log")
    return d


def _child_cmd(code: int) -> str:
    """A command line for a child that prints a marker and exits with `code`."""
    return subprocess.list2cmdline(
        [sys.executable, "-c", f"print('CHILD-MARKER'); raise SystemExit({code})"]
    )


def _argv(slug: str, child: str) -> list[str]:
    return ["run_hidden.py", "--name", slug, "--", child]


def _raw(slug: str, child: str) -> str:
    """The raw command line Windows would hand the launcher."""
    return f'pythonw.exe "run_hidden.py" --name {slug} -- {child}'


# ---------------------------------------------------------------------------
# 1. exit-code passthrough
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", [0, 1, 2, 3, 42, 255])
def test_child_exit_code_is_returned_verbatim(code, logs):
    """Task Scheduler's Last Result must keep meaning what it means today."""
    child = _child_cmd(code)
    rc = run_hidden.run(_raw("probe", child), _argv("probe", child))
    assert rc == code


def test_large_windows_exit_code_survives_the_32bit_mask():
    """A CRT-aborted child reports 0xC000013A (3221225786) -- bigger than a C
    int. Masking to 32 bits must be the identity for such codes, not a
    truncation to something that looks like success."""
    assert run_hidden._exit_code(0xC000013A) == 0xC000013A
    assert run_hidden._exit_code(0) == 0
    assert run_hidden._exit_code(1) == 1
    # A negative code (POSIX signal convention) must not become 0/success.
    assert run_hidden._exit_code(-1) == 0xFFFFFFFF


def test_main_returns_child_code(logs, monkeypatch):
    """main() end to end over the raw-command-line path (what Windows does)."""
    child = _child_cmd(9)
    monkeypatch.setattr(run_hidden, "raw_command_line", lambda: _raw("probe", child))
    assert run_hidden.main(_argv("probe", child)) == 9


def test_main_returns_child_code_via_the_argv_fallback(logs, monkeypatch):
    """Same, with GetCommandLineW unavailable. argv arrives TOKENIZED (the CRT
    splits it), so the fallback's list2cmdline re-join reproduces a runnable
    command line."""
    monkeypatch.setattr(run_hidden, "raw_command_line", lambda: None)
    argv = [
        "run_hidden.py",
        "--name",
        "probe",
        "--",
        sys.executable,
        "-c",
        "raise SystemExit(9)",
    ]
    assert run_hidden.main(argv) == 9


# ---------------------------------------------------------------------------
# 2. logging
# ---------------------------------------------------------------------------


def test_child_output_is_captured_to_a_dated_task_log(logs):
    child = _child_cmd(0)
    run_hidden.run(_raw("My-Task", child), _argv("My-Task", child))
    written = list(logs.glob("My-Task-*.log"))
    assert len(written) == 1, f"expected one dated log, got {written}"
    text = written[0].read_text(encoding="utf-8")
    assert "CHILD-MARKER" in text, "child stdout was not captured"
    assert "=== run_hidden My-Task" in text, "missing run header"
    assert "CMD: " in text
    assert "EXIT: 0" in text


def test_stderr_is_folded_into_the_same_log(logs):
    child = subprocess.list2cmdline(
        [sys.executable, "-c", "import sys; sys.stderr.write('ERR-MARKER'); raise SystemExit(1)"]
    )
    rc = run_hidden.run(_raw("errs", child), _argv("errs", child))
    assert rc == 1
    text = next(logs.glob("errs-*.log")).read_text(encoding="utf-8")
    assert "ERR-MARKER" in text


def test_log_dir_is_created_on_demand(logs):
    assert not logs.exists()
    child = _child_cmd(0)
    run_hidden.run(_raw("mk", child), _argv("mk", child))
    assert logs.is_dir()


def test_repeat_runs_append_rather_than_truncate(logs):
    child = _child_cmd(0)
    for _ in range(2):
        run_hidden.run(_raw("appendme", child), _argv("appendme", child))
    text = next(logs.glob("appendme-*.log")).read_text(encoding="utf-8")
    assert text.count("=== run_hidden appendme") == 2


# ---------------------------------------------------------------------------
# 3. launcher-side failures are distinguishable from script failures
# ---------------------------------------------------------------------------


def test_missing_child_command_returns_the_usage_code_and_is_logged(logs):
    rc = run_hidden.run("pythonw.exe run_hidden.py --name lonely", ["run_hidden.py", "--name", "lonely"])
    assert rc == run_hidden.EXIT_USAGE == 0xE0
    errs = (logs / "_launcher-errors.log").read_text(encoding="utf-8")
    assert "USAGE" in errs and "lonely" in errs


def test_unspawnable_child_returns_the_spawn_code_and_is_logged(logs):
    child = subprocess.list2cmdline([str(logs / "does-not-exist.exe"), "--x"])
    rc = run_hidden.run(_raw("ghost", child), _argv("ghost", child))
    assert rc == run_hidden.EXIT_SPAWN_FAILED == 0xE2
    errs = (logs / "_launcher-errors.log").read_text(encoding="utf-8")
    assert "SPAWN" in errs
    # The per-task log must also say so, so the failure is visible where
    # someone debugging that task would actually look.
    assert "spawn failed" in next(logs.glob("ghost-*.log")).read_text(encoding="utf-8")


def test_unwritable_log_dir_returns_the_log_setup_code(logs, monkeypatch):
    def boom(*_a, **_k):
        raise OSError("read-only volume")

    monkeypatch.setattr(Path, "mkdir", boom)
    child = _child_cmd(0)
    rc = run_hidden.run(_raw("nolog", child), _argv("nolog", child))
    assert rc == run_hidden.EXIT_LOG_SETUP == 0xE1


def test_launcher_bug_is_swallowed_into_an_exit_code(logs, monkeypatch):
    """main() must never let an exception escape: under pythonw there is no
    stderr to print a traceback to, and Task Scheduler would report a generic
    crash instead of a code that names the launcher."""

    def boom(*_a, **_k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(run_hidden, "run", boom)
    monkeypatch.setattr(run_hidden, "raw_command_line", lambda: None)
    assert run_hidden.main(["run_hidden.py"]) == run_hidden.EXIT_LAUNCHER_BUG == 0xE3


def test_launcher_exit_codes_cannot_collide_with_ordinary_script_codes():
    codes = {
        run_hidden.EXIT_USAGE,
        run_hidden.EXIT_LOG_SETUP,
        run_hidden.EXIT_SPAWN_FAILED,
        run_hidden.EXIT_LAUNCHER_BUG,
    }
    assert len(codes) == 4, "launcher exit codes must be distinct"
    assert all(0xE0 <= c <= 0xEF for c in codes)


# ---------------------------------------------------------------------------
# 4. stdout discipline (the pythonw invariant)
# ---------------------------------------------------------------------------


def test_launcher_never_writes_to_stdout():
    """Under pythonw.exe sys.stdout/sys.stderr are None, so print() raises
    AttributeError and the task dies before spawning anything. Pin it in the
    source: no print() calls, no sys.stdout/sys.stderr writes."""
    tree = ast.parse(LAUNCHER_PATH.read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    printed = [
        n for n in calls if isinstance(n.func, ast.Name) and n.func.id == "print"
    ]
    assert not printed, "run_hidden.py must never print (sys.stdout is None under pythonw)"
    src = LAUNCHER_PATH.read_text(encoding="utf-8")
    for forbidden in ("sys.stdout.write", "sys.stderr.write"):
        assert forbidden not in src, f"run_hidden.py must not use {forbidden}"


def test_launcher_imports_only_stdlib():
    """A third-party import (dotenv, requests, ...) would make every task
    depend on the venv resolving correctly under pythonw, and some packages
    write to stdout on import."""
    tree = ast.parse(LAUNCHER_PATH.read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    assert roots <= {
        "__future__",
        "os",
        "subprocess",
        "sys",
        "time",
        "datetime",
        "pathlib",
        "ctypes",
    }, f"unexpected imports: {roots}"


# ---------------------------------------------------------------------------
# 5. CREATE_NO_WINDOW -- the actual point of the launcher
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "nt", reason="CREATE_NO_WINDOW is Windows-only")
def test_creation_flags_request_no_window_on_windows():
    assert run_hidden._creation_flags() == subprocess.CREATE_NO_WINDOW
    assert run_hidden._creation_flags() != 0


def test_creation_flags_degrade_to_zero_where_the_constant_is_absent(monkeypatch):
    monkeypatch.delattr(subprocess, "CREATE_NO_WINDOW", raising=False)
    assert run_hidden._creation_flags() == 0


@pytest.mark.skipif(os.name != "nt", reason="Windows console semantics")
def test_child_is_spawned_with_the_no_window_flag(logs, monkeypatch):
    """Belt to the empirical probe: assert the flag actually reaches Popen, so
    a refactor cannot quietly drop it and leave the windows back."""
    seen = {}
    real_popen = subprocess.Popen

    class Spy(real_popen):  # type: ignore[misc]
        def __init__(self, *a, **kw):
            seen["creationflags"] = kw.get("creationflags")
            seen["cwd"] = kw.get("cwd", "<not passed>")
            super().__init__(*a, **kw)

    monkeypatch.setattr(subprocess, "Popen", Spy)
    child = _child_cmd(0)
    run_hidden.run(_raw("flagcheck", child), _argv("flagcheck", child))
    assert seen["creationflags"] == subprocess.CREATE_NO_WINDOW
    # cwd must NOT be forced: the launcher inherits the task's
    # WorkingDirectory, including the 17 tasks registered with an empty one.
    assert seen["cwd"] in (None, "<not passed>")


# ---------------------------------------------------------------------------
# 6. slug sanitisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("cowork-cora-backup", "cowork-cora-backup"),
        ("Cora - Drive Sweep", "Cora---Drive-Sweep"),
        ("Cora - Daily Synthesis (F3E)", "Cora---Daily-Synthesis--F3E"),
        ("", "task"),
        ("...", "task"),
    ],
)
def test_sanitize_slug(raw, expected):
    assert run_hidden.sanitize_slug(raw) == expected


@pytest.mark.parametrize("attack", ["../../evil", r"..\..\evil", "a/b/c", "C:\\abs\\path"])
def test_slug_cannot_escape_the_task_log_dir(attack, logs):
    slug = run_hidden.sanitize_slug(attack)
    assert "/" not in slug and "\\" not in slug and ":" not in slug
    resolved = (logs / f"{slug}-2026-01-01.log").resolve()
    assert resolved.parent == logs.resolve(), "a slug must stay inside logs/tasks"


def test_slug_is_length_capped():
    assert len(run_hidden.sanitize_slug("x" * 500)) == 80


# ---------------------------------------------------------------------------
# 7. command-line splitting (the verbatim-quoting guarantee)
# ---------------------------------------------------------------------------


def test_raw_command_line_is_taken_verbatim_after_the_first_sentinel():
    """The cmd.exe actions carry quoting that does not survive a tokenize /
    re-quote round trip, so the child command line must come from the raw
    process command line untouched."""
    child = 'cmd.exe /c cd /d "C:\\Users\\Harri\\code\\cora" & "C:\\p y\\python.exe" "C:\\s\\x.py"'
    raw = f'pythonw.exe "run_hidden.py" --name cowork-cora-kb-sync-asana -- {child}'
    slug, got = run_hidden.split_child_command(raw, ["run_hidden.py", "--name", "cowork-cora-kb-sync-asana", "--"])
    assert slug == "cowork-cora-kb-sync-asana"
    assert got == child, "quoting must survive byte-for-byte"


def test_first_sentinel_wins_when_the_task_args_also_contain_a_double_dash():
    """The launcher's own args are only `--name <slug>`, so the FIRST ' -- ' is
    always the sentinel even when the wrapped command has one of its own."""
    child = "python.exe run.py -- --passthrough 1"
    raw = f"pythonw.exe run_hidden.py --name t -- {child}"
    slug, got = run_hidden.split_child_command(raw, ["run_hidden.py", "--name", "t", "--"])
    assert slug == "t"
    assert got == child


def test_argv_fallback_when_no_raw_command_line_is_available():
    """Off Windows (and if GetCommandLineW ever fails) the launcher re-joins
    argv. Lossier, but it must still produce a runnable command line."""
    slug, got = run_hidden.split_child_command(
        None, ["run_hidden.py", "--name", "fb", "--", "python.exe", "a b.py", "--flag"]
    )
    assert slug == "fb"
    assert got == subprocess.list2cmdline(["python.exe", "a b.py", "--flag"])


def test_name_equals_form_is_accepted():
    slug, _ = run_hidden.split_child_command(None, ["run_hidden.py", "--name=eq", "--", "x.exe"])
    assert slug == "eq"


def test_missing_name_falls_back_to_a_safe_slug():
    slug, got = run_hidden.split_child_command(None, ["run_hidden.py", "--", "x.exe"])
    assert slug == "task"
    assert got == "x.exe"


def test_raw_command_line_is_none_off_windows(monkeypatch):
    monkeypatch.setattr(run_hidden.os, "name", "posix")
    assert run_hidden.raw_command_line() is None


# ---------------------------------------------------------------------------
# 8. log retention
# ---------------------------------------------------------------------------


def test_retention_prunes_only_logs_older_than_the_window(logs):
    logs.mkdir(parents=True)
    old = logs / "old-2020-01-01.log"
    fresh = logs / "fresh-2026-09-01.log"
    for f in (old, fresh):
        f.write_text("x", encoding="utf-8")
    stale = time.time() - 20 * 86400
    os.utime(old, (stale, stale))

    removed = run_hidden.prune_old_logs(logs, days=14)
    assert removed == 1
    assert not old.exists()
    assert fresh.exists()


def test_retention_never_deletes_the_launcher_error_log(logs):
    logs.mkdir(parents=True)
    errs = logs / "_launcher-errors.log"
    errs.write_text("x", encoding="utf-8")
    stale = time.time() - 400 * 86400
    os.utime(errs, (stale, stale))
    assert run_hidden.prune_old_logs(logs, days=14) == 0
    assert errs.exists(), "the launcher's own failure record must be kept"


def test_retention_ignores_non_log_files(logs):
    logs.mkdir(parents=True)
    keep = logs / "notes.txt"
    keep.write_text("x", encoding="utf-8")
    stale = time.time() - 400 * 86400
    os.utime(keep, (stale, stale))
    assert run_hidden.prune_old_logs(logs, days=14) == 0
    assert keep.exists()


def test_retention_never_raises_on_a_missing_dir(tmp_path):
    assert run_hidden.prune_old_logs(tmp_path / "nope", days=14) == 0


def test_a_run_prunes_stale_logs(logs):
    logs.mkdir(parents=True)
    old = logs / "ancient-2020-01-01.log"
    old.write_text("x", encoding="utf-8")
    stale = time.time() - 90 * 86400
    os.utime(old, (stale, stale))
    child = _child_cmd(0)
    run_hidden.run(_raw("now", child), _argv("now", child))
    assert not old.exists()


# ---------------------------------------------------------------------------
# 9. the rewrap script's own self-test, run inside the suite
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "nt", reason="PowerShell rewrap script")
def test_rewrap_script_self_test_passes():
    """rewrap-tasks-hidden.ps1 -SelfTest table-drives the action transformation
    over all seven action shapes present in the live estate. It touches no
    tasks, so it is safe inside the suite."""
    proc = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(REWRAP_PATH),
            "-SelfTest",
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, f"self-test failed:\n{proc.stdout}\n{proc.stderr}"
    assert "SELF-TEST PASSED" in proc.stdout


def test_rewrap_script_is_ascii_only():
    """D-016: PowerShell 5.1 reads UTF-8 as Windows-1252."""
    raw = REWRAP_PATH.read_bytes()
    assert all(b < 128 for b in raw), "rewrap-tasks-hidden.ps1 must be ASCII-only"


def test_rewrap_script_defaults_to_dry_run_and_holds_back_the_service():
    src = REWRAP_PATH.read_text(encoding="ascii")
    assert "[switch]$Apply" in src, "apply must be opt-in"
    assert '$HeldBack = @("cowork-cora-service", "cora-watchdog")' in src
    assert "-Apply requires an ELEVATED PowerShell" in src
    assert "Export-ScheduledTask" in src, "must back up XML before modifying"


# ---------------------------------------------------------------------------
# 10. repo-wide rail: every helper spawn asks for no window
# ---------------------------------------------------------------------------


def _subprocess_spawn_sites():
    """Every subprocess.run/Popen/call/check_* call in shipped code.

    Keyed on the actual AST call sites rather than on a list of filenames: the
    original plan named 10 sites and there were 13 (cora_health_report.py,
    diagnostic.py and security_monitor.py -- the last of which fires every 15
    minutes -- were missing from it). A rail that enumerates files under-reports
    exactly the sites nobody remembered.
    """
    methods = {"run", "Popen", "call", "check_output", "check_call"}
    sites = []
    for folder in ("src", "scripts", "deployment"):
        for path in sorted((REPO_ROOT / folder).rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:  # pragma: no cover
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                if (
                    isinstance(f, ast.Attribute)
                    and f.attr in methods
                    and isinstance(f.value, ast.Name)
                    and f.value.id == "subprocess"
                ):
                    has_flags = any(k.arg == "creationflags" for k in node.keywords)
                    sites.append((path.relative_to(REPO_ROOT).as_posix(), node.lineno, has_flags))
    return sites


def test_every_subprocess_spawn_passes_creationflags():
    """A console child spawned from a parent with NO console gets a brand new
    visible window. Once tasks run under pythonw via run_hidden.py, any spawn
    that forgets creationflags is a window back on Harrison's desktop."""
    sites = _subprocess_spawn_sites()
    assert sites, "the AST scan found no spawn sites at all -- the rail is broken"
    missing = [(f, ln) for f, ln, ok in sites if not ok]
    assert not missing, (
        "these spawns do not pass creationflags (use "
        '`getattr(subprocess, "CREATE_NO_WINDOW", 0)`): ' + repr(missing)
    )


def test_the_spawn_rail_covers_the_known_hot_files():
    """Guard against the rail silently scanning nothing (a bad glob would make
    the test above vacuous). These files are known to spawn helpers."""
    covered = {f for f, _, _ in _subprocess_spawn_sites()}
    for expected in (
        "scripts/nightly_health_check.py",
        "src/cora/mcp_server.py",
        "scripts/cora_health_report.py",
        "scripts/diagnostic.py",
        "scripts/security_monitor.py",
        "deployment/run_hidden.py",
    ):
        assert expected in covered, f"{expected} not seen by the spawn rail"


# ---------------------------------------------------------------------------
# 11. the shared _task-action.ps1 helper + setup-script parity
# ---------------------------------------------------------------------------

HELPER_PATH = REPO_ROOT / "deployment" / "_task-action.ps1"
SETUP_SCRIPTS = sorted((REPO_ROOT / "deployment").glob("setup-*.ps1"))


def test_helper_is_ascii_only():
    assert all(b < 128 for b in HELPER_PATH.read_bytes()), "D-016: _task-action.ps1 must be ASCII"


def test_every_setup_script_registers_a_windowless_action():
    """A setup script re-run must not hand a console action back to the
    scheduler. Each one must build its action with New-WrappedTaskAction, or
    (for the schtasks.exe /Create scripts, whose /TR is capped at 261 chars)
    wrap the registered action afterwards with Set-WrappedTaskAction."""
    assert SETUP_SCRIPTS, "no setup scripts found -- the rail would be vacuous"
    offenders = []
    for p in SETUP_SCRIPTS:
        src = p.read_text(encoding="ascii", errors="replace")
        if "New-WrappedTaskAction" in src or "Set-WrappedTaskAction" in src:
            continue
        offenders.append(p.name)
    assert not offenders, f"setup scripts still registering console actions: {offenders}"


def test_no_setup_script_calls_new_scheduledtaskaction_directly():
    """New-ScheduledTaskAction is the console-action constructor. Only the
    shared helper may call it."""
    offenders = [
        p.name
        for p in SETUP_SCRIPTS
        if "New-ScheduledTaskAction" in p.read_text(encoding="ascii", errors="replace")
    ]
    assert not offenders, (
        "these setup scripts bypass the helper and build a raw console action: "
        f"{offenders}"
    )


def test_setup_scripts_dot_source_the_helper_they_use():
    missing = []
    for p in SETUP_SCRIPTS:
        src = p.read_text(encoding="ascii", errors="replace")
        uses = "New-WrappedTaskAction" in src or "Set-WrappedTaskAction" in src
        if uses and "_task-action.ps1" not in src:
            missing.append(p.name)
    assert not missing, f"uses the helper without dot-sourcing it: {missing}"


def test_the_powershell_slug_is_a_fixed_point_of_the_python_sanitiser():
    """The rewrap/setup scripts NAME the log file; run_hidden.py only defends.
    If the Python sanitiser rewrote the slug PowerShell passed, the real log
    filename would differ from the one the setup script printed."""
    for ps_slug in [
        "Cora-Drive-Sweep",
        "Cora-Daily-Synthesis-F3E",
        "cowork-cora-backup",
        "cora-watchdog",
        "cowork-cora-kb-sync-asana",
        "Cora-Log-Compaction",
        "x" * 80,
    ]:
        assert run_hidden.sanitize_slug(ps_slug) == ps_slug, ps_slug


def test_restart_script_does_not_kill_or_count_the_launcher():
    """restart-cora.ps1's kill filter must stay on python.exe/cora.exe. If
    pythonw.exe were added, a restart would kill the launcher and orphan (or
    double-count) the bot child."""
    src = (REPO_ROOT / "deployment" / "restart-cora.ps1").read_text(encoding="ascii")
    kill_lines = [
        ln for ln in src.splitlines()
        if "Win32_Process -Filter" in ln and "Stop-Process" not in ln
    ]
    assert kill_lines, "could not find the process-filter lines"
    # The two kill/count filters must not name pythonw.
    for ln in kill_lines:
        if "pythonw.exe" in ln:
            # only the informational launcher lookup may mention pythonw
            assert "run_hidden.py" in src, "pythonw filter without the launcher lookup"
    assert "Name='python.exe' OR Name='cora.exe'" in src


@pytest.mark.skipif(os.name != "nt", reason="PowerShell helper")
def test_helper_builds_a_wrapped_action_object():
    """Dot-source the helper and build an action. New-ScheduledTaskAction only
    constructs an in-memory object -- nothing is registered, so this is safe to
    run inside the suite."""
    ps = r"""
$ErrorActionPreference='Stop'
. "{helper}"
$a = New-WrappedTaskAction -TaskName 'Cora - Drive Sweep' -Execute 'C:\p\python.exe' -Argument '"C:\s\x.py" --flag' -WorkingDirectory 'C:\repo'
Write-Output ("EXEC=" + $a.Execute)
Write-Output ("ARGS=" + $a.Arguments)
Write-Output ("WD=" + $a.WorkingDirectory)
Write-Output ("SLUG=" + (Get-TaskSlug 'Cora - Drive Sweep'))
""".replace("{helper}", str(HELPER_PATH))
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
        capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    out = dict(
        line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line
    )
    assert out["EXEC"].lower().endswith("pythonw.exe")
    assert "run_hidden.py" in out["ARGS"]
    assert out["SLUG"] == "Cora-Drive-Sweep"
    assert out["WD"] == r"C:\repo", "WorkingDirectory must be carried through"
    # The original command must survive verbatim after the sentinel...
    assert out["ARGS"].endswith(r'-- C:\p\python.exe "C:\s\x.py" --flag')
    # ...and the Python launcher must recover exactly that from the raw line.
    raw = f'pythonw.exe {out["ARGS"]}'
    slug, child = run_hidden.split_child_command(raw, ["run_hidden.py", "--name", out["SLUG"], "--"])
    assert slug == "Cora-Drive-Sweep"
    assert child == r'C:\p\python.exe "C:\s\x.py" --flag'


@pytest.mark.skipif(os.name != "nt", reason="PowerShell helper")
def test_helper_falls_back_to_a_console_action_when_the_launcher_is_missing():
    """A setup script must still register a WORKING task on a host where the
    venv has not been built. A flashing window is a nuisance; an unregistered
    task is an outage."""
    ps = r"""
$ErrorActionPreference='Stop'
. "{helper}"
$script:CoraLauncher = 'C:\definitely\missing\run_hidden.py'
$a = New-WrappedTaskAction -TaskName 't' -Execute 'C:\p\python.exe' -Argument 'x.py' -WarningAction SilentlyContinue
Write-Output ("EXEC=" + $a.Execute)
Write-Output ("ARGS=" + $a.Arguments)
""".replace("{helper}", str(HELPER_PATH))
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
        capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    out = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
    assert out["EXEC"] == r"C:\p\python.exe", "must degrade to the original command"
    assert out["ARGS"] == "x.py"


def test_helper_documents_the_schtasks_261_char_limit():
    """The reason the schtasks scripts wrap AFTER /Create rather than wrapping
    the /TR string. Measured: schtasks rejects /TR over 261 characters, and the
    wrapper prefix alone is ~113."""
    src = HELPER_PATH.read_text(encoding="ascii")
    assert "261" in src
    assert "Set-ScheduledTask -TaskName $TaskName -Action" in src, (
        "-InputObject fails with 0x80070057 on schtasks-created tasks"
    )


def _ps_code_only(path: Path) -> str:
    """PowerShell source with whole-line comments stripped.

    A source pin must not match the comment that EXPLAINS the thing being
    banned. This exact trap bit twice in this session: the mcp_server read-only
    rail false-positived on a "deployment/run_hidden.py" path containing "/run",
    and the first cut of the rail below false-positived on the comment
    documenting why -InputObject is not used.
    """
    return "\n".join(
        ln for ln in path.read_text(encoding="ascii").splitlines()
        if not ln.lstrip().startswith("#")
    )


def test_rewrap_applies_via_action_not_inputobject():
    """Set-ScheduledTask -InputObject fails with 0x80070057 'The parameter is
    incorrect' on any task registered by schtasks.exe /Create -- 7 of the 94."""
    src = REWRAP_PATH.read_text(encoding="ascii")
    assert "Set-ScheduledTask -TaskName $name -Action $newAction" in src
    code = _ps_code_only(REWRAP_PATH)
    assert "Set-ScheduledTask -InputObject" not in code, (
        "the -InputObject path breaks schtasks-created tasks"
    )
    assert "Set-ScheduledTask -InputObject" not in _ps_code_only(HELPER_PATH)
