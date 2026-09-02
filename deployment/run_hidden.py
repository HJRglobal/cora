r"""Windowless launcher for Cora's scheduled tasks.

WHY THIS EXISTS
    Every Cora scheduled task action is a CONSOLE-subsystem executable
    (.venv\Scripts\python.exe, cmd.exe, powershell). Task Scheduler runs them
    in the founder's interactive session, so each fire allocates a console
    window that flashes up and STEALS FOCUS -- roughly 20 times an hour on the
    daily-driver host. There is no Task Scheduler setting that hides it (the
    "Hidden" flag only hides the task's row in the UI), and -WindowStyle
    Hidden still creates-then-hides a window (visible flash + focus theft).

THE MECHANISM (two Windows facts, in this order)
    1. pythonw.exe is a GUI-subsystem binary: Windows allocates NO console for
       it at all. Task Scheduler launching pythonw.exe produces no window.
    2. A console child spawned with CREATE_NO_WINDOW runs with a console that
       has no window. Its OWN children inherit that windowless console with
       default flags, so the whole descendant tree stays invisible without
       touching any of the scripts.

    Fact 1 alone is not enough: a console app spawned from a process with no
    console at all gets a BRAND NEW (visible) console. CREATE_NO_WINDOW on the
    direct child is what closes that, and it is why this launcher -- not a
    bare `pythonw.exe <script>` -- is the fix. Scripts stay on python.exe:
    1,300+ print() calls across scripts/ would crash under pythonw, where
    sys.stdout is None.

CONTRACT
    pythonw.exe deployment\run_hidden.py --name <task-slug> -- <exe> <args...>

    Everything after the FIRST " -- " is the child command line, taken
    VERBATIM from the raw process command line (GetCommandLineW) rather than
    re-joined from sys.argv. Re-joining is lossy for the `cmd /c cd /d <dir> &
    <exe> <args>` actions, whose quoting does not survive a tokenize/re-quote
    round trip.

    Child stdout+stderr are appended to logs\tasks\<slug>-<YYYY-MM-DD>.log
    (output that Task Scheduler discards today). The launcher exits with the
    child's exit code, so Task Scheduler's Last Result is unchanged.

STDOUT DISCIPLINE
    Under pythonw.exe sys.stdout/sys.stderr are None, so print() raises
    AttributeError. This module NEVER prints, and imports only stdlib modules
    that do not write to stdout on import.

ROLLBACK
    Actions are rewrapped by deployment\rewrap-tasks-hidden.ps1, which exports
    each task's XML first. To restore one task, from elevated PowerShell:
      Register-ScheduledTask -Xml (Get-Content <backup>.xml -Raw) -TaskName <name> -Force
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TASK_LOG_DIR = REPO_ROOT / "logs" / "tasks"
LAUNCHER_ERROR_LOG = TASK_LOG_DIR / "_launcher-errors.log"
# 45, not 14. Five tasks run MONTHLY (Log Compaction and kb-hygiene on day 1,
# QBO Monthly Reports on day 2, Expected Invoice Check and Klaviyo Billing Audit
# on day 9). At 14 days the previous run's log is always ~30 days old and so
# always already deleted by the next run -- and "did last month's run write
# anything?" is exactly the question those report jobs raise. 45 covers a
# monthly cadence with margin.
RETENTION_DAYS = 45

# Per-task log size cap. Above this, the file is rolled aside at the START of
# the next run (see _roll_if_oversized). Age-based retention alone cannot bound
# a single file, and the monthly compaction job globs logs/*.log
# NON-recursively, so it never sees logs/tasks at all.
MAX_TASK_LOG_BYTES = 25 * 1024 * 1024

# Launcher-only exit codes. Deliberately in the 0xE0 range so they can never be
# confused with a child's exit code (scripts use 0/1/2; Windows aborts use
# 0xC0000000-range values), which makes a rewrap regression obvious in the
# task's Last Result instead of looking like a script failure.
EXIT_USAGE = 0xE0         # 224 -- no child command line after " -- "
EXIT_LOG_SETUP = 0xE1     # 225 -- logs\tasks unusable
EXIT_SPAWN_FAILED = 0xE2  # 226 -- CreateProcess failed (missing exe, bad path)
EXIT_LAUNCHER_BUG = 0xE3  # 227 -- unexpected launcher error

SENTINEL = " -- "
_SAFE_SLUG_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)


def sanitize_slug(raw: str) -> str:
    r"""Make a task name safe as a filename component.

    Task names carry spaces and parentheses ("Cora - Daily Synthesis (F3E)").
    Anything outside [A-Za-z0-9._-] collapses to '-', which also neutralises
    path traversal ("..\..\evil" can never escape logs\tasks).
    """
    cleaned = "".join(c if c in _SAFE_SLUG_CHARS else "-" for c in (raw or ""))
    cleaned = cleaned.strip("-. ")
    # Cap AFTER the trim and re-trim AFTER the cap, in that order, so this
    # agrees with Get-TaskSlug in _task-action.ps1 for names over 80 chars.
    # (PowerShell trims then truncates; truncating a name whose 80th character
    # is '-' or '.' would otherwise leave a trailing separator on one side
    # only, and the advertised log filename would not be the real one.)
    cleaned = cleaned[:80].strip("-. ")
    return cleaned or "task"


def raw_command_line() -> str | None:
    """The process's own command line, exactly as Windows received it.

    Returns None off Windows or if the call is unavailable, so callers fall
    back to a sys.argv re-join.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes

        ctypes.windll.kernel32.GetCommandLineW.restype = ctypes.c_wchar_p
        return ctypes.windll.kernel32.GetCommandLineW()
    except Exception:  # noqa: BLE001 -- any failure just means use the fallback
        return None


def split_child_command(raw: str | None, argv: list[str]) -> tuple[str, str]:
    """Return (slug, child_command_line).

    The launcher's own arguments are exactly `--name <slug>`, so the FIRST
    " -- " in the raw line is always the launcher's sentinel even when the
    wrapped task's own arguments contain " -- " later on.
    """
    slug = ""
    for i, arg in enumerate(argv):
        if arg == "--name" and i + 1 < len(argv):
            slug = argv[i + 1]
            break
        if arg.startswith("--name="):
            slug = arg.split("=", 1)[1]
            break

    child = ""
    if raw:
        # Anchor the search PAST our own arguments before looking for the
        # sentinel. Searching from position 0 means a repo path, a venv path or
        # a hand-registered --name value containing " -- " would be mistaken for
        # the sentinel and the recovered child command would be a garbage
        # fragment. Get-TaskSlug never emits a space, so this cannot bite from
        # the rewrap path -- but the launcher should not depend on its caller.
        start = 0
        marker = "run_hidden.py"
        anchor = raw.rfind(marker)
        if anchor >= 0:
            start = anchor + len(marker)
        name_at = raw.find("--name", start)
        if name_at >= 0:
            rest = raw[name_at + len("--name"):]
            stripped = rest.lstrip()
            if stripped.startswith('"'):
                close = stripped.find('"', 1)
                if close > 0:
                    start = name_at + len("--name") + (len(rest) - len(stripped)) + close + 1
        idx = raw.find(SENTINEL, start)
        if idx >= 0:
            child = raw[idx + len(SENTINEL):].strip()
    if not child:
        # Fallback (non-Windows, or GetCommandLineW unavailable): re-join argv.
        # Lossy for cmd.exe quoting, which is why the raw line is preferred.
        if "--" in argv:
            tail = argv[argv.index("--") + 1:]
            if tail:
                child = subprocess.list2cmdline(tail)
    return sanitize_slug(slug), child


def _append(path: Path, text: str) -> None:
    """Best-effort append. A logging failure must never fail a task."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", errors="replace") as fh:
            fh.write(text)
    except Exception:  # noqa: BLE001
        pass


def _try_write(handle, text: str) -> None:
    """Write to the task log, swallowing any failure.

    The module's rule is that a logging failure must never fail a task, but it
    was only implemented inside _append(). Once the child runs in a
    kill-on-close job, an exception from a write BETWEEN Popen and wait() is
    far worse than a lost log line: run() propagates, main() returns
    EXIT_LAUNCHER_BUG, the launcher exits, the job closes, and the kernel kills
    a child that was doing real work. A full disk must not become a killed job.
    """
    try:
        handle.write(text)
        handle.flush()
    except Exception:  # noqa: BLE001
        pass


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def prune_old_logs(log_dir: Path, days: int = RETENTION_DAYS) -> int:
    r"""Delete logs\tasks\*.log older than `days`. Never raises.

    Only *.log files are considered, and _launcher-errors.log is kept
    permanently -- it is the one file that records the launcher itself failing,
    which is exactly what someone would go looking for weeks later.
    """
    removed = 0
    cutoff = time.time() - days * 86400
    date_cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        entries = list(log_dir.glob("*.log"))
    except Exception:  # noqa: BLE001
        return 0
    for entry in entries:
        if entry.name == LAUNCHER_ERROR_LOG.name:
            continue
        try:
            stale_mtime = entry.stat().st_mtime < cutoff
            # ALSO prune on the date in the FILENAME. The log name is fixed at
            # process START, so a long-running child -- above all the always-on
            # service task -- keeps its mtime fresh forever and an mtime-only
            # rule would never delete it, growing one file without bound.
            # Windows refuses to unlink a file that is still open, so the LIVE
            # log is protected automatically and only gets collected once the
            # process holding it has exited.
            stale_name = False
            stem = entry.stem
            if len(stem) > 10:
                tail = stem[-10:]
                # Parse it as a real date rather than lexically comparing any
                # 10 characters that happen to hold two dashes: a stem ending
                # "10-a-bcdef" would sort below the cutoff and be deleted
                # regardless of its age.
                try:
                    datetime.strptime(tail, "%Y-%m-%d")
                except ValueError:
                    pass
                else:
                    stale_name = tail < date_cutoff
            if stale_mtime or stale_name:
                entry.unlink()
                removed += 1
        except Exception:  # noqa: BLE001
            continue
    return removed


# --- Kill-on-close job object -----------------------------------------------
#
# MEASURED 2026-09-02 on this host: when a task hits its ExecutionTimeLimit,
# Task Scheduler terminates the TASK'S process -- now this launcher -- and the
# child SURVIVES. A 1-minute-limit probe wrapping a 600s sleeper reported
# "launchers 2 -> 0, children 2 -> 2, same PIDs", task State=Ready,
# LastTaskResult=267014: the scheduler believed the task had ended while the
# real work ran on, unbounded and invisible. That is the same failure this repo
# already hit with the `cmd /c` wrapper ("the task's ExecutionTimeLimit kills
# the cmd /c wrapper but NOT the python child"), and adding this launcher would
# otherwise have extended it to the 84 tasks whose action is a direct exe.
#
# The fix has to survive TerminateProcess, which runs no cleanup code -- so no
# atexit / finally / signal handler can do it. A Windows job object with
# JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE is enforced by the KERNEL: when the last
# handle to the job closes (i.e. when this process dies, however it dies),
# every process still in the job is killed. Grandchildren are covered too --
# a process assigned to a job has its descendants join the same job.
#
# Consequence worth knowing: when the launcher exits normally, anything the
# task left running is also killed. No Cora task relies on leaving a background
# process behind -- every restart path goes through Start-ScheduledTask or
# schtasks /Run, so the restarted service is spawned by the Task Scheduler
# service and is NOT in this job.
#
# Best-effort throughout: any failure degrades to the pre-job behaviour (an
# orphanable child) rather than failing the task.
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOBOBJECT_EXTENDED_LIMIT_INFORMATION = 9  # JobObjectExtendedLimitInformation


def _make_kill_on_close_job():
    """A job object whose members die when this process does. None on failure."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
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

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        k32.SetInformationJobObject.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]

        handle = k32.CreateJobObjectW(None, None)
        if not handle:
            return None
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(
            handle,
            _JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            k32.CloseHandle(handle)
            return None
        return handle
    except Exception:  # noqa: BLE001 -- never fail a task over a cleanup nicety
        return None


def _assign_self_to_job(job) -> bool:
    """Put THIS process into `job`, so descendants inherit membership.

    Nested jobs are supported from Windows 8 on, so this works even though Task
    Scheduler may already have placed the task in a job of its own.
    """
    if job is None or os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        return bool(k32.AssignProcessToJobObject(job, k32.GetCurrentProcess()))
    except Exception:  # noqa: BLE001
        return False


def _assign_to_job(job, proc) -> bool:
    """Put an already-started child into `job`. True if it took effect.

    The window between CreateProcess and this call is microseconds, and the
    child is a fresh interpreter that has not spawned anything yet.
    """
    if job is None or os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        return bool(k32.AssignProcessToJobObject(job, int(proc._handle)))
    except Exception:  # noqa: BLE001
        return False


def _roll_if_oversized(log_path: Path, cap: int = MAX_TASK_LOG_BYTES) -> bool:
    """Roll a task log aside once it exceeds `cap`. Never raises.

    Task Scheduler used to DISCARD child output; capturing it means a verbose
    or runaway task can now write GBs into one dated file well inside the
    retention window, and disk pressure on the founder's machine is exactly the
    condition that turns a swallowed log-write failure into a killed job. One
    previous generation is kept as <name>.log.prev, so the last run before the
    growth is still readable.

    A file the OS will not let us rename (a still-open long-running log) is
    left alone -- the cap is best-effort housekeeping, never a reason to fail.
    """
    try:
        if not log_path.exists() or log_path.stat().st_size <= cap:
            return False
        prev = log_path.with_suffix(log_path.suffix + ".prev")
        try:
            if prev.exists():
                prev.unlink()
        except Exception:  # noqa: BLE001
            pass
        log_path.replace(prev)
        return True
    except Exception:  # noqa: BLE001
        return False


def _header(slug: str, child: str) -> str:
    now_utc = datetime.now(timezone.utc)
    # AZ is UTC-7 year-round (no DST), so a fixed offset is exact here and
    # avoids importing zoneinfo just for a header line.
    now_az = now_utc + timedelta(hours=-7)
    return (
        "\n=== run_hidden {slug} | {utc} UTC | {az} AZ ===\n"
        "CMD: {cmd}\n".format(
            slug=slug,
            utc=now_utc.strftime("%Y-%m-%d %H:%M:%S"),
            az=now_az.strftime("%Y-%m-%d %H:%M:%S"),
            cmd=child,
        )
    )


def _child_env() -> dict:
    r"""The child's environment, with std-stream encoding pinned to UTF-8.

    MEASURED 2026-09-02 through a real scheduled task: a console-attached child
    (today's shape) gets sys.stdout.encoding='utf-8' via PEP 528, but a child
    whose stdout is a FILE -- which is exactly what this launcher does -- gets
    the locale encoding, here cp1252. A bare print() of any character cp1252
    cannot represent then raises UnicodeEncodeError and kills the script
    mid-run, AFTER its side effects. The probe exited 1 on print("emoji ...").

    That would have been a data-dependent, delayed failure across the 85
    python.exe tasks -- Slack text, Asana task names, meeting titles and
    briefing bodies all routinely carry emoji and arrows.

    PYTHONIOENCODING is the narrowest fix: it governs the std STREAMS only.
    PYTHONUTF8/-X utf8 would also change the default encoding for every open()
    in every script, which is a real behaviour change. The backslashreplace
    error handler is belt-and-braces (UTF-8 can encode anything) so a stream
    write can never again be the thing that fails a task. An operator-set value
    is respected.
    """
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8:backslashreplace")
    return env


def _creation_flags() -> int:
    """CREATE_NO_WINDOW, or 0 where the constant does not exist (POSIX)."""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run(raw: str | None, argv: list[str]) -> int:
    slug, child = split_child_command(raw, argv)
    if not child:
        _append(
            LAUNCHER_ERROR_LOG,
            "{ts} USAGE slug={slug} no child command line after ' -- '\n".format(
                ts=_stamp(), slug=slug
            ),
        )
        return EXIT_USAGE

    try:
        TASK_LOG_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        _append(
            LAUNCHER_ERROR_LOG,
            "{ts} LOGSETUP slug={slug} {exc!r}\n".format(
                ts=_stamp(), slug=slug, exc=exc
            ),
        )
        return EXIT_LOG_SETUP

    prune_old_logs(TASK_LOG_DIR)

    log_path = TASK_LOG_DIR / "{slug}-{day}.log".format(
        slug=slug, day=datetime.now(timezone.utc).strftime("%Y-%m-%d")
    )
    _roll_if_oversized(log_path)
    try:
        log_handle = log_path.open("a", encoding="utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        _append(
            LAUNCHER_ERROR_LOG,
            "{ts} LOGOPEN slug={slug} {path} {exc!r}\n".format(
                ts=_stamp(), slug=slug, path=log_path, exc=exc
            ),
        )
        return EXIT_LOG_SETUP

    with log_handle:
        # Best-effort, like every other write to this handle: the module's rule
        # is that a logging failure must never fail a task, and an unprotected
        # header write meant a full disk returned EXIT_LAUNCHER_BUG *without
        # ever running the task at all*.
        _try_write(log_handle, _header(slug, child))
        job = _make_kill_on_close_job()
        # Assign THIS process to the job BEFORE spawning. Job membership is
        # inherited at creation, so every descendant -- including the python
        # grandchild behind the 8 `cmd.exe /c ...` actions -- is in the job with
        # no race. Assigning the child after CreateProcess would leave a window
        # in which cmd.exe had already spawned python outside the job, and those
        # are precisely the long-running sweeps with the orphan history.
        self_assigned = _assign_self_to_job(job)
        try:
            # cwd is deliberately NOT set: the launcher inherits the task's
            # WorkingDirectory, so the child gets byte-identical cwd semantics
            # to today -- including the 17 tasks registered with an EMPTY
            # WorkingDirectory, which run from the Task Scheduler default.
            # Forcing the repo root here would be a silent behaviour change.
            proc = subprocess.Popen(  # noqa: S603 -- operator-supplied command line
                child,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=_creation_flags(),
                env=_child_env(),
                close_fds=False,
            )
        except Exception as exc:  # noqa: BLE001
            log_handle.write("LAUNCHER: spawn failed: {exc!r}\n".format(exc=exc))
            _append(
                LAUNCHER_ERROR_LOG,
                "{ts} SPAWN slug={slug} cmd={cmd} {exc!r}\n".format(
                    ts=_stamp(), slug=slug, cmd=child, exc=exc
                ),
            )
            return EXIT_SPAWN_FAILED

        if not (self_assigned or _assign_to_job(job, proc)):
            # Degraded, not fatal: the child runs exactly as it does today, but
            # an ExecutionTimeLimit kill would orphan it. Recorded so a stuck
            # task is diagnosable rather than mysterious.
            #
            # Only worth saying if the child is STILL RUNNING: assignment also
            # fails for a process that has already exited, and a fast, healthy
            # task should not log an alarming line about a risk it never faced.
            if proc.poll() is None:
                _try_write(
                    log_handle,
                    "LAUNCHER: job-object assignment failed; child would "
                    "survive a task-timeout kill\n",
                )
        rc = proc.wait()
        _try_write(log_handle, "EXIT: {rc}\n".format(rc=rc))
    return rc


def _exit_code(rc: int) -> int:
    """Normalise a child return code to a 32-bit DWORD."""
    return int(rc) & 0xFFFFFFFF


def _terminate(rc: int) -> None:
    """Exit the launcher with EXACTLY `rc`, including codes >= 2**31.

    MEASURED 2026-09-02: sys.exit() cannot carry a value larger than a C long.
    CPython's SystemExit handler runs it through PyLong_AsLong, which overflows
    and exits -1 instead. The same child (ExitProcess(0xC0000005)) reported
    Last Result 0xC0000005 run directly but 0xFFFFFFFF through the launcher --
    so the entire hard-crash class (0xC0000005 access violation, 0xC0000409
    stack overrun, 0xC000013A) collapsed to -1 and became indistinguishable
    from every other large code. Masking to 32 bits was necessary but NOT
    sufficient; the handoff to Windows is the other half.

    ExitProcess takes a DWORD, so it carries the value exactly. It skips
    interpreter cleanup, which is why it is called only here -- after the log
    handle's `with` block has already closed.
    """
    code = _exit_code(rc)
    if os.name == "nt" and code > 0x7FFFFFFF:
        try:
            import ctypes
            from ctypes import wintypes

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.ExitProcess.argtypes = [wintypes.UINT]
            k32.ExitProcess(code)
        except Exception:  # noqa: BLE001 -- fall through to sys.exit
            pass
    sys.exit(code)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv if argv is None else argv)
    try:
        return _exit_code(run(raw_command_line(), args))
    except Exception as exc:  # noqa: BLE001 -- never traceback to a dead stderr
        _append(LAUNCHER_ERROR_LOG, "{ts} BUG {exc!r}\n".format(ts=_stamp(), exc=exc))
        return EXIT_LAUNCHER_BUG


if __name__ == "__main__":
    _terminate(main())
