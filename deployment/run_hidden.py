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
RETENTION_DAYS = 14

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
    cleaned = cleaned.strip("-. ") or "task"
    return cleaned[:80]


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
        idx = raw.find(SENTINEL)
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
    try:
        entries = list(log_dir.glob("*.log"))
    except Exception:  # noqa: BLE001
        return 0
    for entry in entries:
        if entry.name == LAUNCHER_ERROR_LOG.name:
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed += 1
        except Exception:  # noqa: BLE001
            continue
    return removed


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
        log_handle.write(_header(slug, child))
        log_handle.flush()
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

        rc = proc.wait()
        log_handle.write("EXIT: {rc}\n".format(rc=rc))
    return rc


def _exit_code(rc: int) -> int:
    """Windows exit codes are a 32-bit DWORD.

    A child aborted by the CRT reports e.g. 0xC000013A, which arrives as
    3221225786 -- larger than a C int. Mask to 32 bits so sys.exit() hands
    Windows the value the child actually returned instead of relying on
    implementation-defined truncation.
    """
    return int(rc) & 0xFFFFFFFF


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv if argv is None else argv)
    try:
        return _exit_code(run(raw_command_line(), args))
    except Exception as exc:  # noqa: BLE001 -- never traceback to a dead stderr
        _append(LAUNCHER_ERROR_LOG, "{ts} BUG {exc!r}\n".format(ts=_stamp(), exc=exc))
        return EXIT_LAUNCHER_BUG


if __name__ == "__main__":
    sys.exit(main())
