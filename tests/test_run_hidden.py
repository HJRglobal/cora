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
    code = _ps_code_only(REWRAP_PATH)
    assert "[switch]$Apply" in code, "apply must be opt-in"
    assert '$HeldBack = @("cowork-cora-service", "cora-watchdog")' in code
    assert "-Apply requires an ELEVATED PowerShell" in code
    assert "Export-ScheduledTask" in code, "must back up XML before modifying"
    # ORDER matters, not just presence: the export must happen BEFORE the
    # in-loop Set-ScheduledTask, or the rollback point does not exist when it
    # is needed. A presence-only pin passed with the two swapped.
    lines = code.splitlines()
    exp = next(i for i, ln in enumerate(lines) if "Export-ScheduledTask -TaskName $name" in ln)
    setl = next(i for i, ln in enumerate(lines) if "Set-ScheduledTask -TaskName $name -Action" in ln)
    assert exp < setl, "XML export must precede the action write"


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
                    # Assert the VALUE, not merely that a keyword named
                    # creationflags exists: `creationflags=0` would satisfy a
                    # presence-only check and put every window back.
                    flag = next(
                        (k.value for k in node.keywords if k.arg == "creationflags"), None
                    )
                    ok = flag is not None and ast.unparse(flag) in {
                        "_NO_WINDOW",
                        "_creation_flags()",
                    }
                    sites.append((path.relative_to(REPO_ROOT).as_posix(), node.lineno, ok))
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
        code = _ps_code_only(p)
        if "New-WrappedTaskAction" in code or "Set-WrappedTaskAction" in code:
            continue
        offenders.append(p.name)
    assert not offenders, f"setup scripts still registering console actions: {offenders}"


def test_no_setup_script_calls_new_scheduledtaskaction_directly():
    """New-ScheduledTaskAction is the console-action constructor. Only the
    shared helper may call it."""
    offenders = [p.name for p in SETUP_SCRIPTS if "New-ScheduledTaskAction" in _ps_code_only(p)]
    assert not offenders, (
        "these setup scripts bypass the helper and build a raw console action: "
        f"{offenders}"
    )


def test_setup_scripts_dot_source_the_helper_they_use():
    missing = []
    for p in SETUP_SCRIPTS:
        code = _ps_code_only(p)
        uses = "New-WrappedTaskAction" in code or "Set-WrappedTaskAction" in code
        if uses and "_task-action.ps1" not in code:
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


RESTART_PATH = REPO_ROOT / "deployment" / "restart-cora.ps1"


def test_restart_script_keeps_the_doctrine5_filter_exactly():
    """Doctrine 5's filter counts what it kills. Widening it to pythonw would
    make "one healthy instance = 2 matching processes" wrong.

    An earlier version of this test was MUTATION-PROVEN vacuous: widening the
    real Stop-Process filter to include pythonw.exe left it passing, because it
    asserted a SUBSTRING of the widened filter and its only other assertion was
    always-true. It now pins the exact literal on the lines that actually feed
    Stop-Process and the leftover/bot counts.
    """
    code = _ps_code_only(RESTART_PATH)
    doctrine = "Win32_Process -Filter \"Name='python.exe' OR Name='cora.exe'\""
    launcher = "Win32_Process -Filter \"Name='pythonw.exe'\""
    filter_lines = [ln.strip() for ln in code.splitlines() if "Win32_Process -Filter" in ln]
    # kill + leftover + count on the doctrine filter, and the launcher's own.
    assert len(filter_lines) >= 5, f"expected the full filter set, got {filter_lines}"
    for ln in filter_lines:
        assert (doctrine in ln) or (launcher in ln), f"unexpected process filter: {ln!r}"
        if doctrine in ln:
            # THE point: the doctrine-5 filter must not be widened, or the
            # "one healthy instance = 2 matching processes" count breaks.
            assert "pythonw" not in ln, "the doctrine-5 filter was widened to pythonw"
    assert sum(1 for ln in filter_lines if doctrine in ln) == 3, (
        "expected exactly the kill, leftover and count uses of the doctrine filter"
    )


def test_restart_script_kills_and_counts_the_launcher():
    """The launcher HOLDS the task's running instance, and the service is
    MultipleInstances=IgnoreNew (verified live). Leaving a launcher alive makes
    the following Start-ScheduledTask a silent no-op and Cora stays DOWN, while
    the $leftover guard reports zero because it cannot see pythonw."""
    code = _ps_code_only(RESTART_PATH)
    assert "$launcherFilter" in code, "no launcher filter defined"
    # killed...
    assert code.count("Where-Object $launcherFilter") >= 2, (
        "the launcher must be both killed and counted"
    )
    assert "$leftover += @(Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\"" in code, (
        "a surviving launcher must fail the pre-start leftover guard"
    )
    # ...and the filter must match BOTH service action shapes: the live
    # `-m cora.main` one and the `.venv\\Scripts\\cora.exe` one that
    # setup-windows-task.ps1 still registers (which carries no "cora.main").
    assert "cora.main" in code and "\\Scripts\\cora.exe" in code


def test_restart_script_reads_wrapped_state_from_the_task_definition():
    """Whether the service is wrapped is a property of the task DEFINITION.
    Inferring it from process presence reported "not rewrapped yet" whenever a
    wrapped service had failed to come up -- i.e. exactly when someone is
    debugging it."""
    code = _ps_code_only(RESTART_PATH)
    assert 'Get-ScheduledTask -TaskName "cowork-cora-service"' in code
    assert "$svcAction" in code


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
    code = _ps_code_only(HELPER_PATH)
    assert "261" in HELPER_PATH.read_text(encoding="ascii"), "the limit must be documented"
    assert "Set-ScheduledTask -TaskName $TaskName -Action" in code, (
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
    code = _ps_code_only(REWRAP_PATH)
    assert "Set-ScheduledTask -TaskName $name -Action $newAction" in code
    assert "Set-ScheduledTask -InputObject" not in code, (
        "the -InputObject path breaks schtasks-created tasks"
    )
    assert "Set-ScheduledTask -InputObject" not in _ps_code_only(HELPER_PATH)


# ---------------------------------------------------------------------------
# 12. kill-on-close job object (the ExecutionTimeLimit orphan fix)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "nt", reason="Windows job objects")
def test_job_object_is_created_with_kill_on_close():
    job = run_hidden._make_kill_on_close_job()
    assert job, "could not create the job object -- children would be orphanable"
    import ctypes

    ctypes.WinDLL("kernel32").CloseHandle(job)


def test_job_helpers_are_inert_off_windows(monkeypatch):
    monkeypatch.setattr(run_hidden.os, "name", "posix")
    assert run_hidden._make_kill_on_close_job() is None
    assert run_hidden._assign_to_job(object(), object()) is False


def test_assign_to_job_is_a_noop_without_a_job():
    assert run_hidden._assign_to_job(None, object()) is False


@pytest.mark.skipif(os.name != "nt", reason="Windows job objects")
def test_child_is_actually_assigned_to_the_job(logs):
    """The load-bearing assertion. Measured live: at a task's
    ExecutionTimeLimit, Task Scheduler kills the LAUNCHER and the child
    survives (probe: launchers 2->0, children 2->2, same PIDs, task State=Ready
    with LastTaskResult=267014). JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE makes the
    kernel kill the child when the launcher dies -- it must survive
    TerminateProcess, so no finally/atexit could do this."""
    seen = {}
    real = run_hidden._assign_to_job

    def spy(job, proc):
        seen["job"] = job
        seen["result"] = real(job, proc)
        return seen["result"]

    monkey = pytest.MonkeyPatch()
    # Also force the self-assign path off, so the CHILD assignment is what runs.
    monkey.setattr(run_hidden, "_assign_self_to_job", lambda job: False)
    monkey.setattr(run_hidden, "_assign_to_job", spy)
    try:
        # A child that lives briefly: AssignProcessToJobObject fails on an
        # already-exited process, so a fast-exiting child made this flaky.
        child = subprocess.list2cmdline(
            [sys.executable, "-c", "import time; time.sleep(0.6)"]
        )
        rc = run_hidden.run(_raw("jobcheck", child), _argv("jobcheck", child))
    finally:
        monkey.undo()
    assert rc == 0
    assert seen.get("job"), "no job object was created for the child"
    assert seen.get("result") is True, "the child was NOT assigned to the job"
    # A failed assignment must be recorded, not swallowed.
    text = next(logs.glob("jobcheck-*.log")).read_text(encoding="utf-8")
    assert "job-object assignment failed" not in text


def test_a_failed_job_assignment_is_logged_and_not_fatal(logs, monkeypatch):
    """Degrade to today's behaviour rather than failing the task."""
    monkeypatch.setattr(run_hidden, "_assign_self_to_job", lambda job: False)
    monkeypatch.setattr(run_hidden, "_assign_to_job", lambda job, proc: False)
    child = subprocess.list2cmdline([sys.executable, "-c", "import time; time.sleep(0.4)"])
    rc = run_hidden.run(_raw("nojob", child), _argv("nojob", child))
    assert rc == 0, "a job failure must never fail the task"
    text = next(logs.glob("nojob-*.log")).read_text(encoding="utf-8")
    assert "job-object assignment failed" in text


def test_job_creation_failure_does_not_break_the_run(logs, monkeypatch):
    monkeypatch.setattr(run_hidden, "_make_kill_on_close_job", lambda: None)
    child = _child_cmd(5)
    assert run_hidden.run(_raw("nojob2", child), _argv("nojob2", child)) == 5


# ---------------------------------------------------------------------------
# 13. D-051 remediation coverage (findings from the adversarial review)
# ---------------------------------------------------------------------------

DEPLOYMENT_PS1 = sorted((REPO_ROOT / "deployment").glob("*.ps1"))


def test_every_deployment_ps1_is_ascii_only():
    """D-016 as a REPO-WIDE rail, not a per-file pin.

    Its absence is why setup-hubspot-sync-task.ps1 and
    remove-security-monitor-task.ps1 sat NON-FUNCTIONAL at HEAD: em-dashes read
    as Windows-1252 broke string termination and the files would not parse.
    """
    assert len(DEPLOYMENT_PS1) > 100, "the glob found suspiciously few scripts"
    bad = {}
    for p in DEPLOYMENT_PS1:
        offenders = sorted({b for b in p.read_bytes() if b > 127})
        if offenders:
            bad[p.name] = [hex(b) for b in offenders[:5]]
    assert not bad, f"non-ASCII bytes (D-016) in: {bad}"


@pytest.mark.skipif(os.name != "nt", reason="PowerShell parser")
def test_every_deployment_ps1_parses():
    """A script that cannot parse cannot register anything. Two did not."""
    ps = (
        "$bad = @(); "
        "Get-ChildItem 'deployment\\*.ps1' | ForEach-Object { "
        "$e = $null; $t = $null; "
        "[void][System.Management.Automation.Language.Parser]::ParseFile("
        "$_.FullName, [ref]$t, [ref]$e); "
        "if ($e -and $e.Count -gt 0) { $bad += ($_.Name + ': ' + $e[0].Message) } }; "
        "if ($bad.Count -gt 0) { $bad | ForEach-Object { Write-Output $_ }; exit 1 } "
        "else { Write-Output 'ALL PARSE'; exit 0 }"
    )
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
        capture_output=True, text=True, timeout=300, cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, f"parse errors:\n{proc.stdout}\n{proc.stderr}"


# -- exit-code fidelity as a PROCESS, not just a return value ---------------


@pytest.mark.skipif(os.name != "nt", reason="Windows exit codes")
@pytest.mark.parametrize("code", [0, 1, 3, 42])
def test_launcher_process_exit_code_matches_the_child(code, tmp_path):
    """Runs the launcher as a real process, so this covers `_terminate(main())`
    and the REAL GetCommandLineW path -- neither of which any in-process test
    touches. That handoff to Windows is where a masked code actually lands."""
    proc = subprocess.run(
        [sys.executable, str(LAUNCHER_PATH), "--name", "proc-exit", "--",
         sys.executable, "-c", f"raise SystemExit({code})"],
        capture_output=True, text=True, timeout=120, cwd=str(tmp_path),
    )
    assert proc.returncode == code


@pytest.mark.skipif(os.name != "nt", reason="Windows exit codes")
def test_launcher_carries_a_hard_crash_code_larger_than_a_c_long(tmp_path):
    """MEASURED: sys.exit() cannot carry a value > 2**31 -- CPython runs it
    through PyLong_AsLong, which overflows to -1. The same child reported
    0xC0000005 run directly but 0xFFFFFFFF through the launcher, collapsing the
    whole hard-crash class (access violation, stack overrun) into one
    indistinguishable code. _terminate() uses ExitProcess for those."""
    child = "import ctypes; ctypes.windll.kernel32.ExitProcess(0xC0000005)"
    proc = subprocess.run(
        [sys.executable, str(LAUNCHER_PATH), "--name", "bigcode", "--",
         sys.executable, "-c", child],
        capture_output=True, text=True, timeout=120, cwd=str(tmp_path),
    )
    assert (proc.returncode & 0xFFFFFFFF) == 0xC0000005, (
        f"got {proc.returncode & 0xFFFFFFFF:#x}, expected 0xC0000005"
    )


def test_terminate_masks_before_exiting(monkeypatch):
    seen = {}
    monkeypatch.setattr(run_hidden.sys, "exit", lambda c: seen.setdefault("code", c))
    monkeypatch.setattr(run_hidden.os, "name", "posix")  # skip the ExitProcess branch
    run_hidden._terminate(-1)
    assert seen["code"] == 0xFFFFFFFF


# -- the child's stdout encoding --------------------------------------------


def test_child_env_pins_utf8_std_streams(monkeypatch):
    """MEASURED through a real task: a console-attached child gets
    stdout.encoding=utf-8 (PEP 528), but a child whose stdout is a FILE -- what
    this launcher does -- got cp1252, and print() of an emoji raised
    UnicodeEncodeError and exited 1 AFTER doing its side effects. Slack text,
    Asana task names and briefing bodies carry emoji routinely, so this was a
    data-dependent time bomb across the 85 python.exe tasks."""
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    env = run_hidden._child_env()
    assert env["PYTHONIOENCODING"] == "utf-8:backslashreplace"
    for key in list(os.environ)[:5]:
        assert key in env, "the rest of the environment must survive"


def test_child_env_respects_an_operator_override(monkeypatch):
    monkeypatch.setenv("PYTHONIOENCODING", "latin-1")
    assert run_hidden._child_env()["PYTHONIOENCODING"] == "latin-1"


def test_child_env_does_not_force_global_utf8_mode(monkeypatch):
    """PYTHONUTF8 would also change the default encoding of every open() in
    every script -- a real behaviour change. PYTHONIOENCODING is scoped to the
    std streams, which is the whole problem."""
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    assert "PYTHONUTF8" not in run_hidden._child_env()


@pytest.mark.skipif(os.name != "nt", reason="Windows console encoding")
def test_a_child_can_print_non_cp1252_text_through_the_launcher(logs, tmp_path):
    """End-to-end version of the regression that would have killed tasks."""
    script = tmp_path / "emoji.py"
    script.write_text("print('ok \\u2705 \\u2192')\n", encoding="utf-8")
    child = subprocess.list2cmdline([sys.executable, str(script)])
    rc = run_hidden.run(_raw("emoji", child), _argv("emoji", child))
    assert rc == 0, "printing non-cp1252 text must not fail the task"


# -- the job object's actual flag -------------------------------------------


@pytest.mark.skipif(os.name != "nt", reason="Windows job objects")
def test_job_object_really_has_kill_on_job_close():
    """SetInformationJobObject succeeds for ANY valid LimitFlags, so asserting
    a non-null handle proves nothing -- LimitFlags=0 would pass. Read the flag
    back out of the kernel."""
    import ctypes
    from ctypes import wintypes

    job = run_hidden._make_kill_on_close_job()
    assert job, "no job object"
    try:
        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class EXT(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ]
        k32.QueryInformationJobObject.restype = wintypes.BOOL
        info = EXT()
        ret = wintypes.DWORD(0)
        ok = k32.QueryInformationJobObject(
            job, run_hidden._JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info), ctypes.sizeof(info), ctypes.byref(ret),
        )
        assert ok, "QueryInformationJobObject failed"
        assert info.BasicLimitInformation.LimitFlags & 0x2000, (
            "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE is not set -- children would be "
            "orphaned by an ExecutionTimeLimit kill"
        )
    finally:
        ctypes.WinDLL("kernel32").CloseHandle(job)


def test_kill_on_job_close_constant_is_the_win32_value():
    assert run_hidden._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE == 0x00002000
    assert run_hidden._JOBOBJECT_EXTENDED_LIMIT_INFORMATION == 9


@pytest.mark.skipif(os.name != "nt", reason="Windows job objects")
def test_the_launcher_assigns_ITSELF_so_grandchildren_are_covered(logs, monkeypatch):
    """Assigning the CHILD after CreateProcess leaves a window in which
    cmd.exe has already spawned python OUTSIDE the job -- and the 8 cmd.exe
    actions are exactly the long sweeps with the orphan history. Job membership
    is inherited at creation, so the launcher assigns itself BEFORE spawning."""
    seen = {}
    real = run_hidden._assign_self_to_job
    monkeypatch.setattr(
        run_hidden, "_assign_self_to_job",
        lambda job: seen.setdefault("result", real(job)),
    )
    child = _child_cmd(0)
    assert run_hidden.run(_raw("selfjob", child), _argv("selfjob", child)) == 0
    assert seen.get("result") is True, "the launcher did not join its own job"


# -- log-write failures must not kill a child -------------------------------


def test_a_log_write_failure_cannot_kill_the_child(logs, monkeypatch):
    """With the child in a kill-on-close job, an exception from a write BETWEEN
    Popen and wait() would exit the launcher, close the job and have the kernel
    kill a child doing real work. A full disk must not become a killed job."""
    calls = {"n": 0}
    real_open = Path.open

    class ExplodingHandle:
        def __init__(self, inner):
            self._inner = inner

        def write(self, text):
            calls["n"] += 1
            raise OSError("disk full")

        def flush(self):
            raise OSError("disk full")

        def fileno(self):
            return self._inner.fileno()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            try:
                self._inner.close()
            except Exception:
                pass
            return False

    def patched_open(self, *a, **k):
        handle = real_open(self, *a, **k)
        if "tasks" in str(self):
            return ExplodingHandle(handle)
        return handle

    monkeypatch.setattr(Path, "open", patched_open)
    child = _child_cmd(0)
    rc = run_hidden.run(_raw("diskfull", child), _argv("diskfull", child))
    assert calls["n"] > 0, "the exploding handle was never used"
    assert rc == 0, f"a log-write failure must not fail the task (got {rc:#x})"


def test_try_write_never_raises():
    class Boom:
        def write(self, _):
            raise OSError("nope")

        def flush(self):
            raise OSError("nope")

    run_hidden._try_write(Boom(), "x")  # must not raise


# -- pruning: date validation and the oversize roll -------------------------


def test_prune_does_not_delete_a_stem_that_merely_looks_datelike(logs):
    """A raw lexical `stem[-10:] < cutoff` on any 10 characters holding two
    dashes would delete e.g. a stem ending "10-a-bcdef" regardless of age."""
    logs.mkdir(parents=True)
    decoy = logs / "weird-10-a-bcdef.log"
    decoy.write_text("x", encoding="utf-8")
    assert run_hidden.prune_old_logs(logs, days=45) == 0
    assert decoy.exists(), "a non-date stem must not be date-pruned"


def test_prune_deletes_by_filename_date_even_when_mtime_is_fresh(logs):
    """The service task's log name is fixed at process start and its mtime
    stays fresh for the process's whole life, so an mtime-only rule never
    collects it and one file grows without bound."""
    logs.mkdir(parents=True)
    old = logs / "cowork-cora-service-2020-01-01.log"
    old.write_text("x", encoding="utf-8")  # mtime = now
    assert run_hidden.prune_old_logs(logs, days=45) == 1
    assert not old.exists()


def test_retention_window_covers_a_monthly_cadence():
    """Five tasks run monthly; at 14 days the previous run's log was always
    already deleted before the next run could be compared against it."""
    assert run_hidden.RETENTION_DAYS >= 32


def test_oversized_log_is_rolled_aside_not_deleted(logs):
    logs.mkdir(parents=True)
    big = logs / "loud-2026-09-02.log"
    big.write_bytes(b"x" * 128)
    assert run_hidden._roll_if_oversized(big, cap=64) is True
    assert not big.exists()
    prev = logs / "loud-2026-09-02.log.prev"
    assert prev.exists() and prev.read_bytes() == b"x" * 128, "one generation must be kept"


def test_small_log_is_left_alone(logs):
    logs.mkdir(parents=True)
    small = logs / "quiet-2026-09-02.log"
    small.write_bytes(b"x" * 8)
    assert run_hidden._roll_if_oversized(small, cap=64) is False
    assert small.exists()


def test_roll_never_raises_on_a_missing_file(logs):
    assert run_hidden._roll_if_oversized(logs / "nope.log", cap=1) is False


def test_task_log_size_is_capped():
    assert 0 < run_hidden.MAX_TASK_LOG_BYTES <= 256 * 1024 * 1024


# -- sentinel anchoring ------------------------------------------------------


def test_a_repo_path_containing_the_sentinel_does_not_break_recovery():
    """Searching for " -- " from position 0 would match inside the pythonw
    path, the launcher path, or a hand-registered --name value."""
    child = r"C:\py\python.exe run.py"
    raw = (
        r'"C:\odd -- dir\.venv\Scripts\pythonw.exe" '
        r'"C:\odd -- dir\deployment\run_hidden.py" --name t -- ' + child
    )
    slug, got = run_hidden.split_child_command(raw, ["run_hidden.py", "--name", "t", "--"])
    assert slug == "t"
    assert got == child


def test_a_quoted_name_containing_the_sentinel_does_not_break_recovery():
    child = r"C:\py\python.exe run.py"
    raw = r'pythonw.exe run_hidden.py --name "a -- b" -- ' + child
    _, got = run_hidden.split_child_command(raw, ["run_hidden.py", "--name", "a -- b", "--"])
    assert got == child


# -- the cmd.exe action shape, actually executed ----------------------------


@pytest.mark.skipif(os.name != "nt", reason="cmd.exe")
def test_a_cmd_slash_c_action_actually_runs_through_the_launcher(logs, tmp_path):
    """The 8 kb-sync/reconciliation tasks use
    `cmd.exe /c cd /d "<dir>" & "<exe>" "<script>"`. Verbatim recovery is only
    useful if the reconstructed line still RUNS."""
    script = tmp_path / "who.py"
    script.write_text("import os; print('CWD', os.getcwd())\n", encoding="utf-8")
    child = f'cmd.exe /c cd /d "{tmp_path}" & "{sys.executable}" "{script}"'
    rc = run_hidden.run(_raw("cmdclass", child), _argv("cmdclass", child))
    assert rc == 0
    text = next(logs.glob("cmdclass-*.log")).read_text(encoding="utf-8")
    assert "CWD" in text, "the cmd.exe chain did not run"
    assert str(tmp_path).lower() in text.lower(), "cd /d did not take effect"


# -- the empty-WorkingDirectory branch (17 live tasks) ----------------------


@pytest.mark.skipif(os.name != "nt", reason="PowerShell helper")
def test_helper_handles_an_empty_working_directory():
    """17 live tasks are registered with an EMPTY WorkingDirectory.
    New-ScheduledTaskAction -WorkingDirectory '' THROWS, so the helper's
    if/else is load-bearing -- "simplifying" it away would break registration
    for those tasks with a green suite."""
    ps = r"""
$ErrorActionPreference='Stop'
. "{helper}"
$a = New-WrappedTaskAction -TaskName 'cowork-cora-service' -Execute 'C:\p\python.exe' -Argument '-m cora.main'
Write-Output ("EXEC=" + $a.Execute)
Write-Output ("WD=[" + $a.WorkingDirectory + "]")
$threw = 'no'
try { New-ScheduledTaskAction -Execute 'C:\p\python.exe' -Argument 'x' -WorkingDirectory '' | Out-Null }
catch { $threw = 'yes' }
Write-Output ("EMPTYWD_THROWS=" + $threw)
""".replace("{helper}", str(HELPER_PATH))
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
        capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    out = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
    assert out["EXEC"].lower().endswith("pythonw.exe")
    assert out["WD"] == "[]", "an empty WorkingDirectory must stay empty"
    assert out["EMPTYWD_THROWS"] == "yes", (
        "if New-ScheduledTaskAction ever accepts -WorkingDirectory '', the "
        "helper's if/else can be simplified -- until then it is load-bearing"
    )


# -- slug parity, derived from the REAL PowerShell function -----------------


@pytest.mark.skipif(os.name != "nt", reason="PowerShell helper")
def test_powershell_slug_is_a_fixed_point_of_the_python_sanitiser_for_real():
    """The earlier version hand-typed the PowerShell side, so changing
    Get-TaskSlug's regex could not fail it. Derive the slugs by CALLING
    Get-TaskSlug, including the >80-character case where the two used to
    disagree (PowerShell trimmed then truncated; Python capped then trimmed)."""
    names = [
        "Cora - Drive Sweep",
        "Cora - Daily Synthesis (F3E)",
        "cowork-cora-backup",
        "cora-watchdog",
        "Cora - Log Compaction",
        "..\\..\\evil",
        "",
        "...",
        ("y" * 79) + " zzz",
        "A - " + ("b" * 100),
    ]
    quoted = ",".join("'" + n.replace("'", "''") + "'" for n in names)
    ps = (
        '. "%s"\n$names = @(%s)\nforeach ($n in $names) { Write-Output (Get-TaskSlug $n) }'
        % (HELPER_PATH, quoted)
    )
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
        capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    ps_slugs = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    assert len(ps_slugs) == len(names), f"expected {len(names)} slugs, got {ps_slugs}"
    for name, slug in zip(names, ps_slugs):
        assert slug, "empty slug"
        assert run_hidden.sanitize_slug(slug) == slug, (
            f"PowerShell slug {slug!r} for {name!r} is not a fixed point of the "
            "Python sanitiser -- the advertised log filename would not be the real one"
        )


# -- the rewrap script's remediated behaviour -------------------------------


def test_rewrap_selftest_fails_on_a_thrown_assertion():
    """MEASURED: with $ErrorActionPreference=Continue a THROWN assertion
    printed and the self-test still reported PASSED and exited 0 -- so the only
    automated gate on the transformation logic was unsound for any breakage
    that throws rather than returning a wrong value."""
    code = _ps_code_only(REWRAP_PATH)
    assert '$ErrorActionPreference = "Stop"' in code
    assert "FAIL (threw)" in code, "a throw must be recorded as a failure"


def test_rewrap_verifies_cadence_not_just_trigger_count():
    """11 Cora tasks carry their cadence in Trigger.Repetition (PT15M, PT4H,
    the watchdog's PT5M). A dropped repetition leaves Triggers.Count at 1 and
    turns a 15-minute task into a once-daily one."""
    code = _ps_code_only(REWRAP_PATH)
    assert "Get-ActionFingerprint" in code
    for field in ("Repetition.Interval", "Repetition.Duration", "StartBoundary",
                  "RunLevel", "UserId", "LogonType", "ExecutionTimeLimit",
                  "MultipleInstances"):
        assert field in code, f"the fingerprint does not cover {field}"
    assert "CADENCE/SETTINGS CHANGED" in code


def test_rewrap_skips_disabled_tasks_by_default():
    """18 of the 94 are disabled by design; a disabled task never fires, so
    wrapping it buys nothing and spends blast radius."""
    code = _ps_code_only(REWRAP_PATH)
    assert "[switch]$IncludeDisabled" in code
    assert '$row.State -eq "Disabled"' in code


def test_rewrap_can_still_apply_a_priority_to_an_already_wrapped_task():
    """The priority lookup used to sit BELOW the already-wrapped `continue`, so
    once setup-backup-task.ps1 re-registered a wrapped action at Priority 7 the
    override could never be applied again by any number of re-runs."""
    code = _ps_code_only(REWRAP_PATH)
    assert "$priorityNeeded" in code
    lines = code.splitlines()
    prio = next(i for i, ln in enumerate(lines) if "$priorityNeeded =" in ln)
    skip = next(i for i, ln in enumerate(lines) if "already wrapped" in ln)
    assert prio < skip, "priority must be evaluated BEFORE the already-wrapped skip"


def test_rewrap_writes_a_loadable_xml_backup():
    """MEASURED: Export-ScheduledTask emits a string declaring
    encoding="UTF-16". Written with -Encoding UTF8 the declaration and the bytes
    disagree, and XmlDocument.Load / `schtasks /Create /XML` both refuse it --
    so the reflexive restore command failed at exactly the wrong moment."""
    code = _ps_code_only(REWRAP_PATH)
    assert "Set-Content -Path $backupPath -Encoding Unicode" in code
    assert "-Encoding UTF8" not in code
    assert "$probe.Load($backupPath)" in code, "existence is not validity"


def test_rewrap_self_heals_a_stale_wrap():
    """A task wrapped against an OLD launcher path (repo moved, venv rebuilt)
    used to be reported "already wrapped" by the very tool meant to repair it,
    while every fire failed with 0x80070002."""
    code = _ps_code_only(REWRAP_PATH)
    assert "Get-OriginalCommandLine" in code
    assert "STALE launcher path or slug" in code


def test_rewrap_only_with_no_match_is_an_error():
    code = _ps_code_only(REWRAP_PATH)
    assert "-Only matched no task" in code


def test_rewrap_separates_action_and_settings_failures():
    """One shared try reported a settings failure as "the action failed" and
    sent the operator to roll back a wrap that had actually succeeded."""
    code = _ps_code_only(REWRAP_PATH)
    assert "the ACTION was not applied" in code
    assert "the action WAS applied but Priority was not" in code


def test_setup_scripts_report_a_failed_wrap():
    """Set-WrappedTaskAction returns $false on five distinct failure paths
    (task absent, >1 action, launcher missing, Set-ScheduledTask threw --
    including "elevation may be required" -- and a read-back mismatch). Piping
    it to Out-Null threw the only signal away."""
    offenders = []
    for p in SETUP_SCRIPTS:
        for ln in _ps_code_only(p).splitlines():
            if "Set-WrappedTaskAction" in ln and "Out-Null" in ln:
                offenders.append(p.name)
    assert not offenders, f"these discard the wrap result: {offenders}"


def test_health_check_names_the_launcher_exit_codes():
    """The 0xE0-0xE3 codes exist so a rewrap regression is obvious in Last
    Result -- but the only consumer that reads Last Result had no entries for
    them, so a broken launcher would print 94 unexplained lines."""
    src = (REPO_ROOT / "scripts" / "nightly_health_check.py").read_text(encoding="utf-8")
    for code in (224, 225, 226, 227):
        assert f"    {code}: " in src, f"no Last Result hint for {code}"
    assert "check_windowless_launcher" in src
    assert "all_results.append(check_windowless_launcher())" in src
