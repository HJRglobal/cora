r"""Audit the test suite for TIME ROT: fixtures that expire on a calendar date.

TIME ROT is a test that passes when written and silently turns main red weeks
later, because a HARDCODED date in its fixture must fall inside (or outside) a
window the code under test derives from `datetime.now()`. Two have bitten:

  2026-08-31  test_autowrite_digest_honest_zero.py pinned day="2026-08-24" and
              read it back through read_autowrite_scans(days=7). Four tests went
              red on a clean main, asserting on product wording for a path that
              was CORRECTLY seeing no rows.
  2026-12-31  test_a5_d051_remediation.py pinned "2026-12-31" as "the
              future-dated row" against a product that clamps on
              date.today() -- so on that date the test would have asserted the
              opposite of its own name. Found by THIS script before it fired.

WHY A CLOCK SHIFT, NOT A GREP
    Running the suite at clock = T+N tells you exactly what it will do on that
    real calendar date. A static scan cannot: at the time of writing, 62 test
    files hardcode a recent date alongside a days=/timedelta window and 19
    hardcode a FUTURE date, yet only ONE of them actually rots. The dominant
    (and correct) pattern in this repo is CLOCK INJECTION -- passing `today=` /
    `now=` into the product -- which makes a pinned literal permanently safe.
    test_knowledge_check.py has 159 date literals and 58 such injections, and
    is in no danger at all. A static rail here would be ~98% false positives.

THE TWO ARTEFACT CLASSES (this is the part worth not re-deriving)
    A clock shift produces false positives that are NOT rot. Both come from
    fixtures generated at RUNTIME, which by construction can never rot:

    1. mtime freshness. freezegun patches time.time()/datetime.now() but NOT
       os.stat, so `age = time.time() - path.stat().st_mtime` compares a SHIFTED
       now against a REAL mtime and reports a just-written file as exactly N
       days old (dynamic_answers reported "720.0h old, threshold 24h" at +30d).
       --shift-mtime makes the filesystem consistent with the fake clock and
       removes these -- but then INVERTS: a fixture deliberately backdated with
       os.utime reads as fresh instead (finance_adherence, session_capture,
       the stale-lock reclaim tests).
    2. Cross-thread timestamps. freezegun's patch does not reach a
       ThreadPoolExecutor worker, so a tool run through tool_dispatch.dispatch()
       stores a REAL ts that the main thread then reads against a SHIFTED now
       (test_confirm_dispatcher's freshest_changed_stash test).

    Hence the triage rule below: genuine rot does not involve a runtime
    timestamp at all, so it fails with the mtime shim ON *and* OFF. Anything
    failing in only one mode is mtime-dependent, i.e. an artefact.

TRIAGE RULE
    candidates = failures(shim ON) INTERSECT failures(shim OFF)
    then, for each candidate, confirm by reading the test that its fixture is a
    HARDCODED literal. If the timestamp is generated at runtime (time.time(),
    now(), os.utime), it cannot rot and is an artefact of the instrument.

HOW TO RUN (freezegun is deliberately NOT a project dependency)
    It is installed into a throwaway --target dir, so the venv the bot runs
    from is untouched and nothing is added to pyproject/uv.lock. The FIXES this
    finds are relative fixtures plus guard tests, which need no library.

      uv pip install --target %LOCALAPPDATA%\Temp\timerot-tools freezegun
      .venv\Scripts\python.exe scripts\audit_test_timerot.py --shifts 30,365,730

    Or --emit-plugin to write just the plugin and drive pytest yourself.

FIXING WHAT IT FINDS
    Prefer making the fixture RELATIVE to now (a `_recent_day(ago=N)` /
    `_future_and_past_dates()` helper) over freezing the clock, and add a guard
    test that fails BY NAME when the fixture leaves its window -- so the next
    rot reports its own cause instead of surfacing as confusing assertion
    failures on product wording. Then check the file's SIBLINGS: once a shared
    fixture helper becomes date-relative, any other test still hardcoding the
    matching filename/date silently stops exercising anything.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

PLUGIN_SOURCE = r'''"""Ephemeral pytest plugin: run the suite as if it were N days from now.

Written by scripts/audit_test_timerot.py. Not a project dependency.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

_SHIFT = int(os.environ.get("TIMEROT_SHIFT_DAYS", "0") or "0")
_SECS = _SHIFT * 86400
_TIME_ATTRS = ("st_mtime", "st_atime", "st_ctime", "st_birthtime")
_NS_ATTRS = ("st_mtime_ns", "st_atime_ns", "st_ctime_ns", "st_birthtime_ns")
_STATE = {}


class _ShiftedStat:
    """stat_result proxy whose timestamps move with the fake clock."""

    __slots__ = ("_s",)

    def __init__(self, s):
        self._s = s

    def __getattr__(self, name):
        v = getattr(self._s, name)
        if name in _TIME_ATTRS:
            return v + _SECS
        if name in _NS_ATTRS:
            return v + _SECS * 1_000_000_000
        return v

    def __getitem__(self, i):
        return self._s[i]

    def __iter__(self):
        return iter(self._s)


def _install_mtime_shim():
    real_stat, real_lstat = os.stat, os.lstat

    def stat(*a, **k):
        return _ShiftedStat(real_stat(*a, **k))

    def lstat(*a, **k):
        return _ShiftedStat(real_lstat(*a, **k))

    os.stat, os.lstat = stat, lstat
    return real_stat, real_lstat


def pytest_load_initial_conftests(early_config, parser, args):
    """EARLIEST hook: the freeze must start before any conftest or product
    import, or the test's own clock and the product's disagree by the shift."""
    if not _SHIFT:
        return
    from freezegun import freeze_time

    target = datetime.now(timezone.utc) + timedelta(days=_SHIFT)
    # tick=True so elapsed-time logic still behaves; a hard-frozen instant
    # produces failures that are artefacts of the instrument.
    fr = freeze_time(target, tick=True)
    fr.start()
    _STATE["freezer"] = fr
    _STATE["target"] = target
    if os.environ.get("TIMEROT_SHIFT_MTIME", "0") != "0":
        _STATE["realstat"] = _install_mtime_shim()


def pytest_report_header(config):
    if not _SHIFT:
        return None
    return (f"TIMEROT: clock +{_SHIFT}d -> {_STATE.get('target')} "
            f"(mtime shim: {'on' if 'realstat' in _STATE else 'off'})")


def pytest_unconfigure(config):
    saved = _STATE.pop("realstat", None)
    if saved is not None:
        os.stat, os.lstat = saved
    fr = _STATE.pop("freezer", None)
    if fr is not None:
        fr.stop()
'''

_FAIL_RE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.MULTILINE)


def parse_failures(pytest_output: str) -> set[str]:
    """Test ids from a `pytest -q` short summary (FAILED/ERROR lines)."""
    return {m.group(1).split(" - ")[0] for m in _FAIL_RE.finditer(pytest_output)}


def genuine_candidates(with_shim: set[str], without_shim: set[str]) -> set[str]:
    """THE TRIAGE RULE.

    Genuine time rot never involves a runtime-generated timestamp, so it fails
    in BOTH mtime modes. A test failing in only one mode flips with mtime
    semantics, which means its fixture was created at runtime (os.utime, or a
    file written during the test) and therefore cannot rot.
    """
    return with_shim & without_shim


def _run(shift: int, shim: bool, plugin_dir: Path, extra: list[str]) -> str:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(plugin_dir) + os.pathsep + env.get("PYTHONPATH", "")
    env["TIMEROT_SHIFT_DAYS"] = str(shift)
    env["TIMEROT_SHIFT_MTIME"] = "1" if shim else "0"
    cmd = [sys.executable, "-m", "pytest", "-p", "timeshift_plugin", "-q", *extra]
    proc = subprocess.run(cmd, cwd=str(_REPO_ROOT), env=env,
                          capture_output=True, text=True)
    return proc.stdout + proc.stderr


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Audit the suite for time rot.")
    ap.add_argument("--shifts", default="30,365,730",
                    help="comma-separated day offsets to test (default 30,365,730)")
    ap.add_argument("--tools-dir", default="",
                    help="dir holding freezegun + the plugin (default: a temp dir)")
    ap.add_argument("--emit-plugin", action="store_true",
                    help="write the plugin and exit; drive pytest yourself")
    ap.add_argument("pytest_args", nargs="*",
                    help="extra args passed through to pytest (e.g. a test path)")
    args = ap.parse_args(argv)

    tools = Path(args.tools_dir) if args.tools_dir else Path(
        tempfile.gettempdir()) / "timerot-tools"
    tools.mkdir(parents=True, exist_ok=True)
    (tools / "timeshift_plugin.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
    print(f"plugin written: {tools / 'timeshift_plugin.py'}")
    if args.emit_plugin:
        return 0

    if not (tools / "freezegun").exists():
        print("\nfreezegun is not in the tools dir. It is deliberately NOT a project\n"
              "dependency -- install it into the throwaway dir first:\n"
              f'  uv pip install --target "{tools}" freezegun\n')
        return 2

    shifts = [int(s) for s in str(args.shifts).split(",") if s.strip()]
    all_candidates: dict[int, set[str]] = {}
    for shift in shifts:
        print(f"\n=== clock +{shift}d ===")
        out_off = _run(shift, False, tools, args.pytest_args)
        out_on = _run(shift, True, tools, args.pytest_args)
        fails_off, fails_on = parse_failures(out_off), parse_failures(out_on)
        cands = genuine_candidates(fails_on, fails_off)
        all_candidates[shift] = cands
        print(f"  failures  shim OFF: {len(fails_off)}   shim ON: {len(fails_on)}")
        print(f"  artefacts (one mode only): {len(fails_off ^ fails_on)}")
        print(f"  CANDIDATES (both modes):   {len(cands)}")
        for c in sorted(cands):
            print(f"    {c}")

    union = set().union(*all_candidates.values()) if all_candidates else set()
    print("\n=== SUMMARY ===")
    if not union:
        print("No time-rot candidates at any tested shift.")
    else:
        print("Confirm each by READING the test: a hardcoded literal is genuine\n"
              "rot; a runtime timestamp (time.time()/now()/os.utime) is an\n"
              "artefact of the instrument and cannot rot.")
        for c in sorted(union):
            print(f"  {c}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
