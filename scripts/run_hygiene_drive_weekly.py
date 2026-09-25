#!/usr/bin/env python3
r"""Weekly Founder-OS Drive hygiene inventory + report (Code #15 R8, cq-592baba613f1).

Saturday 02:40 AZ (``cowork-cora-hygiene-drive-weekly``, Harrison registers it
with ``deployment\setup-hygiene-drive-weekly-task.ps1``). Script-side only: the
bot never imports this, so it goes live at its next fire with no restart.

WHAT IT DOES (``--apply``, what the task passes)
------------------------------------------------
 1. Checks the audit root (the G: mount) is reachable -- else no run, exit 2.
 2. Verifies the VENDORED inventory PS1 (``deployment\hygiene\folder-audit-
    inventory.ps1``) against its pinned sha256 and ASCII-only -- else exit 3.
    The runner executes reviewed repo code, never the editable Drive copy.
 3. Snapshots the out-dir listing (``Downloads\hjr-folder-audit``).
 4. Runs the PS1 WINDOWLESS (CREATE_NO_WINDOW) with ``-MaxHashMB 0 -Quiet``:
    a NO-HASH, read-only walk of the whole tree (~90 s measured 9/24).
 5. A non-zero PS1 exit -> no report, exit 4.
 6. Finds the new stamp by diffing the listing (a pre-existing stamp = a
    collision -> exit 5; Export-Csv would have overwritten someone's run). A
    walk that wrote its CSVs / run log but NO summary (the PS1 still exits 0
    when the summary write fails) is an INCOMPLETE WALK: report + an
    ``incomplete`` ledger row, so rotation reaches its files -- exit 1.
 7. Parses the summary + run log (counts only; the run log's lines carry paths).
 8. SANITY FLOOR: files AND folders >= 80% of the HIGH-WATER full run (the
    largest of the newest KEEP_RUNNER_STAMPS eligible runs of step 9 -- the 9/21
    baseline counts only while it is among them; anchoring on the prior alone
    let the floor ratchet down 20% a week), 0 walk errors (WALK-*/FATAL/
    OUTPUT-WRITE run-log lines), both CSVs readable and their row counts == the
    summary totals, and the prior's two CSVs readable. A failure writes an
    INCOMPLETE WALK report (no lists, no deltas), records the stamp as
    ``incomplete``, and exits 1 -- never a clean report on a partial walk (the
    "0 violations" failure mode).
 9. Prior = the newest earlier FULL-ROOT stamp this runner recorded as passing
    the floor (its stamp ledger), or the 2026-09-21 19:48 hashing baseline;
    never a hand/subtree run, never a run that failed the floor, never a run
    older than the newest operator re-baseline (below).
10. Writes ``<report_dir>\YYYY-MM-DD_fndr_hygiene-findings.md`` ONLY if
    report_dir exists (never mkdir -- a mkdir on a Drive mount can mint a
    "(1)" twin), only at the exact path, and never over a same-named file that
    lacks the generator marker.
11. Writes ``manifest-desktopini-YYYY-MM-DD.csv`` (DELETE / D3-desktopini,
    non-LEX only, apply.ps1 schema) and the full-list sidecar
    ``hygiene-findings-full-YYYY-MM-DD.csv`` into the out-dir, create-only.
12. Keep-4 rotation: deletes ONLY the files of stamps THIS RUNNER recorded
    creating (full root, MaxHashMB 0), beyond the newest four and never the
    newest clean one; never the 20260921-1948 baseline, a hand or subtree run,
    or any manifest-* / _applied-* / _dryrun-* file.
13. Logs counts only to stdout (run_hidden appends it to logs\tasks).

``--dry-run`` (the default) runs the same walk into a private temp dir, prints
the counts, and writes NOTHING: no report, no manifest, no sidecar, no ledger
row, no rotation. The temp dir (it holds LEX paths) is removed afterwards.
``--from-stamp <stamp>`` rebuilds the report from an existing run in the
out-dir without walking (``--apply`` writes the .md + sidecar only).

``--rebaseline <stamp>`` is the operator's escape hatch for a LEGITIMATE shrink
(a partition moved out of the tree, a purge) that the high-water floor would
otherwise refuse every week: a failed floor is ledgered ``incomplete``, never
becomes eligible, and so could never move the anchor (D-051 R2 rb2#r2-0 /
lens#r2-1). It names a stamp this runner recorded (full root, NO-HASH) whose
walk passes every check EXCEPT the floor, and ``--apply`` appends a
``rebaselined`` row to the stamp ledger: from then on that stamp is eligible
and no older run -- the 9/21 baseline included -- is a prior or part of the
high-water. The baseline's files stay on disk (the RIDER B proposer reads
them). ``--dry-run`` (the default) validates and prints, writing nothing. The
INCOMPLETE report prints the exact commands whenever the walk itself was sound.

The Notion ``[Hygiene] Drive`` page stays Cowork-side: the rewritten Cowork
hygiene-drive task reads this .md and publishes it (Cora has no Notion write
path, and this bundle adds no lane).

CONFIG (env, read at call time; every one is redirected to tmp in tests/conftest.py)
    HYGIENE_INVENTORY_PS1      the vendored PS1
    HYGIENE_AUDIT_ROOT         G:\My Drive\HJR-Founder-OS
    HYGIENE_AUDIT_OUTDIR       C:\Users\Harri\Downloads\hjr-folder-audit
    HYGIENE_REPORT_DIR         <root>\_shared\hygiene-pending-moves
    HYGIENE_STAMP_LEDGER_PATH  data\state\hygiene-drive-stamps.jsonl
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import drive_io  # noqa: E402
from cora import hygiene_drive_report as hdr  # noqa: E402

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

#: sha256 of the reviewed inventory PS1 (the Drive original, 2026-09-21, 655 lines,
#: ASCII, LF, no BOM). The vendored copy must match it byte for byte; a mismatch
#: refuses the run. .gitattributes marks the copy -text so checkout never converts it.
INVENTORY_PS1_SHA256 = "339d456deca9ef1c861b842895aaa07370c822ad9c01a4e9e43e504a409d7251"
BASELINE_STAMP = "20260921-1948"      # the 9/21 hashing baseline -- never rotated
KEEP_RUNNER_STAMPS = 4
FLOOR_RATIO = 0.80
PS1_TIMEOUT_S = 2700

DEFAULT_ROOT = r"G:\My Drive\HJR-Founder-OS"
DEFAULT_OUTDIR = r"C:\Users\Harri\Downloads\hjr-folder-audit"
DEFAULT_REPORT_SUBDIR = r"_shared\hygiene-pending-moves"

EXIT_OK, EXIT_INCOMPLETE, EXIT_UNAVAILABLE, EXIT_REFUSED, EXIT_PS1, EXIT_STAMP, EXIT_REPORT = 0, 1, 2, 3, 4, 5, 6

_STAMP_RE = r"\d{8}-\d{4}"
_SUMMARY_NAME_RE = re.compile(rf"^inventory-summary-({_STAMP_RE})\.txt$")
ROTATION_RE = re.compile(rf"^(?:inventory-(?:files|dirs|summary)|_run-log)-({_STAMP_RE})\.(?:csv|txt)$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def stamp_files(stamp: str) -> tuple[str, str, str, str]:
    return (f"inventory-files-{stamp}.csv", f"inventory-dirs-{stamp}.csv",
            f"inventory-summary-{stamp}.txt", f"_run-log-{stamp}.txt")


@dataclass
class Config:
    ps1: Path
    root: Path
    outdir: Path
    report_dir: Path
    ledger: Path


def load_config() -> Config:
    root = Path(os.environ.get("HYGIENE_AUDIT_ROOT") or DEFAULT_ROOT)
    return Config(
        ps1=Path(os.environ.get("HYGIENE_INVENTORY_PS1")
                 or _REPO_ROOT / "deployment" / "hygiene" / "folder-audit-inventory.ps1"),
        root=root,
        outdir=Path(os.environ.get("HYGIENE_AUDIT_OUTDIR") or DEFAULT_OUTDIR),
        report_dir=Path(os.environ.get("HYGIENE_REPORT_DIR") or root / DEFAULT_REPORT_SUBDIR),
        ledger=Path(os.environ.get("HYGIENE_STAMP_LEDGER_PATH")
                    or _REPO_ROOT / "data" / "state" / "hygiene-drive-stamps.jsonl"),
    )


def _say(msg: str) -> None:
    """Counts-only log line (run_hidden appends stdout to logs\\tasks)."""
    print(f"hygiene-drive: {msg}", flush=True)


def _inside(child: Path, parent: Path) -> bool:
    c = os.path.normcase(os.path.abspath(child)).rstrip("\\/")
    p = os.path.normcase(os.path.abspath(parent)).rstrip("\\/")
    return c == p or c.startswith(p + os.sep)


# ── steps 1-2: preconditions ────────────────────────────────────────────────

def root_available(root: Path) -> bool:
    anchor = drive_io.MOUNT_ANCHOR
    if _inside(root, anchor) or _inside(anchor, root):
        if not drive_io.is_mount_available():
            return False
    try:
        return bool(drive_io.exists(root, retry_seconds=30)) and os.path.isdir(root)
    except drive_io.DriveUnavailable:
        return False


def verify_ps1(ps1: Path, expected: str | None = None) -> str | None:
    """None if the PS1 is the pinned, ASCII-only file; else why not."""
    try:
        data = ps1.read_bytes()
    except OSError:
        return "inventory PS1 not readable"
    if hashlib.sha256(data).hexdigest() != (expected or INVENTORY_PS1_SHA256):
        return "inventory PS1 sha256 does not match the pinned value"
    if any(b > 127 for b in data):
        return "inventory PS1 is not ASCII-only (D-016)"
    return None


# ── steps 3-6: the walk ──────────────────────────────────────────────────────

def listing(d: Path) -> set[str]:
    try:
        return set(os.listdir(d))
    except FileNotFoundError:
        return set()


def _avoid_minute_collision(before: set[str], now_fn=datetime.now, sleep_fn=time.sleep) -> None:
    """The PS1 stamps to the minute and Export-Csv overwrites: if this minute (or
    the next) already has a stamp in the out-dir, wait into a fresh minute."""
    for _ in range(3):
        now = now_fn()
        stamps = {now.strftime("%Y%m%d-%H%M"), (now + timedelta(minutes=1)).strftime("%Y%m%d-%H%M")}
        if not any(s in n for n in before for s in stamps):
            return
        sleep_fn(61 - now.second)


def run_inventory(ps1: Path, root: Path, outdir: Path) -> int:
    """Run the vendored PS1 windowless. Its stdout (the summary, incl. Root/OutDir)
    is captured and dropped -- the runner reads the summary FILE instead."""
    cmd = [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
           "-File", str(ps1), "-Root", str(root), "-OutDir", str(outdir),
           "-MaxHashMB", "0", "-Quiet"]
    try:
        proc = subprocess.run(cmd, creationflags=_NO_WINDOW, capture_output=True, timeout=PS1_TIMEOUT_S,
                              text=True, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return -1
    return int(proc.returncode)


def detect_new_stamp(before: set[str], after: set[str]) -> tuple[str | None, str | None]:
    """(stamp, error). Exactly one new summary, and all four of its files new."""
    new = sorted(m.group(1) for n in after - before if (m := _SUMMARY_NAME_RE.match(n)))
    if not new:
        return None, "no new inventory stamp appeared"
    if len(new) > 1:
        return None, f"{len(new)} new stamps appeared (concurrent run?)"
    stamp = new[0]
    pre = [n for n in stamp_files(stamp) if n in before]
    if pre:
        return None, "stamp collision: files of the new stamp pre-existed (overwritten)"
    return stamp, None


def detect_summaryless_stamp(before: set[str], after: set[str]) -> str | None:
    """The stamp of a walk that wrote files but NO summary, else None. The PS1
    writes the two CSVs, then the summary, then the run log in its finally
    block and exits 0 even when the summary's Set-Content fails (it only logs
    'OUTPUT-WRITE-ERROR (summary)'), so ``detect_new_stamp`` -- keyed on the
    summary -- saw nothing and those files were never ledgered, never rotated
    (D-051 R2 rb2#r2-2). Exactly one such stamp, no new summary at all, and
    none of its four names pre-existing (else it may be someone else's run)."""
    if any(_SUMMARY_NAME_RE.match(n) for n in after - before):
        return None
    new = sorted({m.group(1) for n in after - before if (m := ROTATION_RE.match(n))})
    if len(new) != 1:
        return None
    stamp = new[0]
    if any(n in before for n in stamp_files(stamp)):
        return None
    return stamp


# ── steps 7-9: parse, prior, sanity ─────────────────────────────────────────

@dataclass
class RunInfo:
    stamp: str
    summary: dict
    runlog: dict[str, int] | None

    @property
    def walk_errors(self) -> int | None:
        return None if self.runlog is None else hdr.walk_error_count(self.runlog)


def read_run(d: Path, stamp: str) -> RunInfo | None:
    s = d / f"inventory-summary-{stamp}.txt"
    try:
        summary = hdr.parse_summary(s.read_text(encoding="utf-8-sig", errors="replace"))
    except OSError:
        return None
    try:
        runlog = hdr.count_run_log((d / f"_run-log-{stamp}.txt").read_text(encoding="utf-8-sig", errors="replace"))
    except OSError:
        runlog = None
    return RunInfo(stamp, summary, runlog)


def load_rows(path: Path) -> list[dict] | None:
    """One inventory CSV's rows, or None when it is missing or unreadable. The
    PS1 still exits 0 when Export-Csv fails at open (its RunStatus is COMPLETE
    before the finally block that writes the CSVs), so a missing CSV is a failed
    walk -- an INCOMPLETE report plus a ledger row -- never a traceback that
    leaves no report and unledgered (never-rotated) stamp files (D-051 R1
    rb2-r8#2)."""
    try:
        return hdr.load_csv_rows(path)
    except (OSError, ValueError, csv.Error):   # ValueError covers UnicodeDecodeError
        return None


def read_ledger(path: Path) -> list[dict]:
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
    except FileNotFoundError:
        pass
    return rows


def ledger_state(rows: list[dict]) -> dict[str, dict]:
    """stamp -> its "created" row, with ``rotated`` set once a rotation row exists
    and ``rebaselined`` set once an operator re-baseline row names it."""
    out: dict[str, dict] = {}
    rebased: set[str] = set()
    for r in rows:
        st = r.get("stamp")
        if not isinstance(st, str):
            continue
        if r.get("event") == "created":
            out[st] = dict(r)
        elif r.get("event") == "rotated" and st in out:
            out[st]["rotated"] = True
        elif r.get("event") == "rebaselined":
            rebased.add(st)
    for st in rebased:
        if st in out:
            out[st]["rebaselined"] = True
    return out


def rebaseline_cutoff(ledger: dict[str, dict]) -> str | None:
    """The newest operator re-baselined stamp: no run older than it is a prior or
    part of the high-water mark any more (the 9/21 baseline included)."""
    return max((st for st, r in ledger.items() if r.get("rebaselined")), default=None)


def _is_runner_walk(row: dict | None) -> bool:
    """A ledger "created" row for a full-root, NO-HASH walk this runner launched."""
    return bool(row) and row.get("full_root") is True and row.get("max_hash_mb") == 0


def append_ledger(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def eligible_runs(outdir: Path, current: str, full_root: Path, ledger: dict[str, dict]) -> list[RunInfo]:
    """Every earlier stamp that is a runner run which passed the floor (ledger
    status clean/unverified, not rotated) or that the operator re-baselined onto,
    or the 9/21 baseline, AND whose own summary says COMPLETE on the full root
    with 0 walk errors and both CSVs present -- newest first. Hand and subtree
    runs are never eligible, and neither is any run older than the newest
    re-baseline (``rebaseline_cutoff``) -- the baseline included."""
    out: list[RunInfo] = []
    cutoff = rebaseline_cutoff(ledger)
    stamps = sorted({m.group(1) for n in listing(outdir) if (m := _SUMMARY_NAME_RE.match(n))}, reverse=True)
    for st in stamps:
        if st >= current or (cutoff is not None and st < cutoff):
            continue
        row = ledger.get(st)
        if st != BASELINE_STAMP:
            if not row or row.get("rotated"):
                continue
            if row.get("status") not in ("clean", "unverified") and not row.get("rebaselined"):
                continue
        info = read_run(outdir, st)
        if info is None:
            continue
        sm = info.summary
        if sm.get("run_status") != "COMPLETE" or hdr.norm_root(sm.get("root")) != hdr.norm_root(full_root):
            continue
        if info.walk_errors != 0:
            continue
        if not all((outdir / n).exists() for n in stamp_files(st)[:2]):
            continue
        out.append(info)
    return out


def find_prior(outdir: Path, current: str, full_root: Path, ledger: dict[str, dict]) -> RunInfo | None:
    """The newest eligible earlier run (``eligible_runs``) -- the week-over-week
    comparison run."""
    runs = eligible_runs(outdir, current, full_root, ledger)
    return runs[0] if runs else None


def high_water(runs: list[RunInfo]) -> dict[str, tuple[int, str]]:
    """{'files_total' / 'dirs_total': (largest total, its stamp)} over the newest
    KEEP_RUNNER_STAMPS eligible runs (``runs`` is newest first) -- the runner
    runs that passed the floor, plus the 9/21 baseline only while it is among
    them. The sanity floor is anchored HERE, not on the prior alone: a walk that
    passed 80% of an already-partial prior would otherwise become next week's
    floor, and the floor would ratchet down 20% a week (D-051 R1 rb2-r8#1: 100
    -> 81 -> 65 read CLEAN, with 16 unwalked offenders reported 'resolved').

    BOUNDED (D-051 R2 lens#r2-1 / rb2#r2-0): the never-rotated baseline used to
    anchor the floor forever, so a legitimate cumulative 20% shrink since 9/21
    failed every week and no week could ever pass to move it. Now the anchor is
    the window rotation keeps, and a shrink larger than the floor allows within
    it is accepted only by the operator (``--rebaseline``), never by waiting."""
    hw: dict[str, tuple[int, str]] = {}
    for info in runs[:KEEP_RUNNER_STAMPS]:
        for key in ("files_total", "dirs_total"):
            n = info.summary.get(key) or 0
            if n and (key not in hw or n > hw[key][0]):
                hw[key] = (n, info.stamp)
    return hw


def sanity(info: RunInfo, full_root: Path, prior: RunInfo | None, *, files_rows: int | None,
           dirs_rows: int | None, high: dict[str, tuple[int, str]] | None = None) -> list[str]:
    """Reasons this walk cannot be trusted (empty list = it passed). A row count
    of None = that CSV is missing or unreadable. ``high`` is the high-water mark
    the floor is anchored to (``high_water``); None = the prior alone."""
    sm, why = info.summary, []
    if sm.get("run_status") != "COMPLETE":
        why.append("the inventory did not report COMPLETE")
    if hdr.norm_root(sm.get("root")) != hdr.norm_root(full_root):
        why.append("the inventory root is not the full Founder-OS root")
    if sm.get("max_hash_mb") != 0 or (sm.get("hashed") or 0) != 0:
        why.append("the inventory hashed files (expected a NO-HASH run)")
    if info.walk_errors is None:
        why.append("the run log is missing")
    elif info.walk_errors:
        why.append(f"{info.walk_errors} walk errors logged")
    for rows, key, label in ((files_rows, "files_total", "files"), (dirs_rows, "dirs_total", "folders")):
        if rows is None:
            why.append(f"the {label} CSV is missing or unreadable")
        elif sm.get(key) is None or rows != sm.get(key):
            why.append(f"the {label} CSV row count does not match the summary total")
    if high is None:
        high = high_water([prior] if prior is not None else [])
    for key, label in (("files_total", "files"), ("dirs_total", "folders")):
        if key not in high:
            continue
        now_n, (hw_n, hw_stamp) = sm.get(key) or 0, high[key]
        if now_n < FLOOR_RATIO * hw_n:
            pct = int(FLOOR_RATIO * 100)
            if prior is not None and hw_stamp == prior.stamp:
                why.append(f"{label} walked {now_n} < {pct}% of the prior full run's {hw_n}")
            else:
                why.append(f"{label} walked {now_n} < {pct}% of the high-water full run's {hw_n} "
                           f"(stamp {hw_stamp})")
    return why


# ── steps 10-12: outputs ─────────────────────────────────────────────────────

def write_report(report_dir: Path, date: str, text: str) -> str:
    """'written' or 'refused: <why>'. Exact path, never mkdir, never clobber a
    file this generator did not write."""
    if not _DATE_RE.match(date):
        return "refused: bad date"
    try:
        if not (drive_io.exists(report_dir) and os.path.isdir(report_dir)):
            return "refused: report dir does not exist (never created by this runner)"
        target = report_dir / f"{date}_fndr_hygiene-findings.md"
        if os.path.normcase(os.path.abspath(target.parent)) != os.path.normcase(os.path.abspath(report_dir)):
            return "refused: target is not directly inside the report dir"
        if drive_io.exists(target):
            if not hdr.has_generator_marker(drive_io.read_text(target, encoding="utf-8")):
                return "refused: a same-named file without the generator marker exists"
        drive_io.write_text_atomic(target, text, make_parents=False)
    except drive_io.DriveUnavailable:
        return "refused: G: mount unavailable"
    return "written"


def write_create_only(path: Path, data: bytes) -> bool:
    try:
        with open(path, "xb") as fh:
            fh.write(data)
    except FileExistsError:
        return False
    return True


def rotate(outdir: Path, ledger_path: Path, ledger: dict[str, dict], full_root: Path, *,
           current: str, now_iso: str) -> int:
    """Delete the files of runner-recorded stamps beyond the newest KEEP_RUNNER_STAMPS.
    Returns the number of files removed."""
    own = sorted((st for st, r in ledger.items()
                  if not r.get("rotated") and r.get("full_root") is True and r.get("max_hash_mb") == 0),
                 reverse=True)
    clean = [st for st in own if ledger[st].get("status") == "clean"]
    protect = set(own[:KEEP_RUNNER_STAMPS]) | {BASELINE_STAMP, current}
    if clean:
        protect.add(clean[0])
    removed = 0
    for st in own:
        if st in protect:
            continue
        info = read_run(outdir, st)
        if info is not None and (hdr.norm_root(info.summary.get("root")) != hdr.norm_root(full_root)
                                 or info.summary.get("max_hash_mb") != 0):
            continue   # the summary no longer describes the runner's own run: leave it
        n = 0
        for name in ledger[st].get("files", []):
            m = ROTATION_RE.match(str(name))
            if not m or m.group(1) != st or st == BASELINE_STAMP:
                continue
            p = outdir / name
            if p.parent != outdir or not p.is_file():
                continue
            os.remove(p)
            n += 1
        removed += n
        append_ledger(ledger_path, {"event": "rotated", "stamp": st, "removed": n, "ts": now_iso})
    return removed


# ── the run ──────────────────────────────────────────────────────────────────

def _report_date(stamp: str) -> str:
    return f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}"


def load_prior_rows(prior_dir: Path, prior: RunInfo | None) -> tuple[list[dict], list[dict]] | None:
    """The prior's (files, dirs) rows, or None when either CSV is missing or
    unreadable. Read through ``load_rows`` BEFORE the ledger row is written:
    ``eligible_runs`` checks only that the CSVs exist, and an unguarded read in
    ``build_outputs`` raised after the current stamp was already ledgered
    'clean' -- a traceback, no report, no rotation (D-051 R2 rb2#r2-2)."""
    if prior is None:
        return None
    pf = load_rows(prior_dir / stamp_files(prior.stamp)[0])
    pd = load_rows(prior_dir / stamp_files(prior.stamp)[1])
    return None if pf is None or pd is None else (pf, pd)


def build_outputs(stamp: str, prior_rows: tuple[list[dict], list[dict]] | None, prior: RunInfo | None,
                  info: RunInfo, files: list[dict], dirs: list[dict], status: str):
    now = hdr.categories(files, dirs)
    prior_cats = None if prior_rows is None else hdr.categories(*prior_rows)
    diffs = hdr.diff(now, prior_cats)
    date = _report_date(stamp)
    sidecar_name = f"hygiene-findings-full-{date}.csv"
    facts = hdr.RunFacts(date=date, stamp=stamp, files_total=info.summary.get("files_total") or 0,
                         dirs_total=info.summary.get("dirs_total") or 0, walk_errors=info.walk_errors or 0,
                         prior_stamp=prior.stamp if prior else None, status=status, sidecar_name=sidecar_name)
    md = hdr.render_md(facts, now, diffs)
    sidecar, withheld = hdr.render_full_list_csv(now, diffs)
    ini_rows, ini_lex = hdr.desktop_ini_rows(files)
    return now, diffs, md, sidecar_name, sidecar, withheld, ini_rows, ini_lex


def rebaseline(cfg: Config, stamp: str, ledger: dict[str, dict], *, apply: bool, now_iso: str) -> int:
    """``--rebaseline <stamp>``: accept a legitimate shrink (module docstring).
    Refuses unless ``stamp`` is a full-root NO-HASH walk this runner recorded,
    not rotated, not older than an earlier re-baseline, and passing every
    sanity check except the floor. Counts only."""
    row = ledger.get(stamp)
    if not _is_runner_walk(row):
        _say("REFUSE: --rebaseline names a stamp this runner did not record as a full-root NO-HASH walk")
        return EXIT_REFUSED
    if row.get("rotated"):
        _say("REFUSE: --rebaseline names a rotated stamp (its files are gone)")
        return EXIT_REFUSED
    cutoff = rebaseline_cutoff(ledger)
    if cutoff is not None and stamp < cutoff:
        _say(f"REFUSE: a newer re-baseline ({cutoff}) is already recorded")
        return EXIT_REFUSED
    info = read_run(cfg.outdir, stamp)
    if info is None:
        _say("REFUSE: --rebaseline names a stamp with no readable summary")
        return EXIT_REFUSED
    files = load_rows(cfg.outdir / stamp_files(stamp)[0])
    dirs = load_rows(cfg.outdir / stamp_files(stamp)[1])
    own = sanity(info, cfg.root, None, files_rows=None if files is None else len(files),
                 dirs_rows=None if dirs is None else len(dirs), high={})
    if own:
        _say(f"REFUSE: --rebaseline {stamp}: the walk itself failed {len(own)} checks -- " + "; ".join(own))
        return EXIT_REFUSED
    hw = high_water(eligible_runs(cfg.outdir, stamp, cfg.root, ledger))
    was = ", ".join(f"{k} {n} ({st})" for k, (n, st) in sorted(hw.items())) or "none"
    _say(f"re-baseline {stamp}: files {info.summary.get('files_total')} dirs {info.summary.get('dirs_total')}; "
         f"replaces the high-water [{was}] and retires every older run as prior / anchor")
    if not apply:
        _say("DRY-RUN: nothing written (pass --apply to record the re-baseline in the stamp ledger)")
        return EXIT_OK
    append_ledger(cfg.ledger, {"event": "rebaselined", "stamp": stamp, "ts": now_iso,
                               "files_total": info.summary.get("files_total"),
                               "dirs_total": info.summary.get("dirs_total")})
    _say(f"re-baseline recorded; rebuild this week's report with --from-stamp {stamp} --apply")
    return EXIT_OK


def summaryless_walk(cfg: Config, run_dir: Path, stamp: str, after: set[str], ledger: dict[str, dict], *,
                     apply: bool, now_iso: str) -> int:
    """A walk that wrote files but no summary (``detect_summaryless_stamp``): an
    INCOMPLETE WALK report plus an ``incomplete`` ledger row for the files that
    exist, so the keep-4 rotation reaches them. The row records the runner's own
    invocation (-Root <the audit root> -MaxHashMB 0) -- the summary that would
    have confirmed it was never written."""
    walk_errors = None
    try:
        runlog = (run_dir / stamp_files(stamp)[3]).read_text(encoding="utf-8-sig", errors="replace")
        walk_errors = hdr.walk_error_count(hdr.count_run_log(runlog))
    except OSError:
        pass
    why = ["the inventory summary was not written (the PS1 exited 0 without it)"]
    if walk_errors is None:
        why.append("the run log is missing")
    elif walk_errors:
        why.append(f"{walk_errors} walk errors logged")
    _say(f"INCOMPLETE WALK: stamp {stamp} has no summary ({len(why)} reasons)")
    if not apply:
        _say("DRY-RUN: nothing written")
        return EXIT_INCOMPLETE
    append_ledger(cfg.ledger, {"event": "created", "stamp": stamp, "status": "incomplete", "ts": now_iso,
                               "files": sorted(n for n in stamp_files(stamp) if n in after),
                               "full_root": True, "max_hash_mb": 0, "summary": False})
    runs = eligible_runs(cfg.outdir, stamp, cfg.root, ledger)
    prior = runs[0] if runs else None
    date = _report_date(stamp)
    md = hdr.render_incomplete_md(date=date, stamp=stamp, reasons=why, files_total=None, dirs_total=None,
                                  prior_stamp=prior.stamp if prior else None,
                                  prior_files=prior.summary.get("files_total") if prior else None,
                                  walk_errors=walk_errors)
    _say(f"report: {write_report(cfg.report_dir, date, md)}")
    return EXIT_INCOMPLETE


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Weekly Drive hygiene inventory + report (dry-run default)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--apply", action="store_true", help="write the report, sidecars, ledger, rotation")
    g.add_argument("--dry-run", action="store_true", help="(default) walk into a temp dir, write nothing")
    ap.add_argument("--from-stamp", default=None, help="rebuild from an existing out-dir stamp (no walk)")
    ap.add_argument("--rebaseline", default=None, metavar="STAMP",
                    help="accept a legitimate shrink: make STAMP (a runner walk that failed only the "
                         "floor) the new floor anchor, retiring every older run (ledger row; --apply writes it)")
    a = ap.parse_args(argv)
    apply = bool(a.apply)
    cfg = load_config()
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if _inside(cfg.outdir, cfg.root):
        _say("REFUSE: the out-dir is inside the audit root")
        return EXIT_REFUSED
    if a.from_stamp and not re.fullmatch(_STAMP_RE, a.from_stamp):
        _say("REFUSE: --from-stamp is not a yyyyMMdd-HHmm stamp")
        return EXIT_REFUSED
    if a.rebaseline is not None and (a.from_stamp or not re.fullmatch(_STAMP_RE, a.rebaseline)):
        _say("REFUSE: --rebaseline takes one yyyyMMdd-HHmm stamp and no --from-stamp")
        return EXIT_REFUSED
    # 1. mount
    if not root_available(cfg.root):
        _say("audit root unavailable (G: mount down?) -- no run, no report")
        return EXIT_UNAVAILABLE
    # 2. the vendored PS1
    bad = verify_ps1(cfg.ps1)
    if bad:
        _say(f"REFUSE: {bad}")
        return EXIT_REFUSED

    ledger = ledger_state(read_ledger(cfg.ledger))
    if a.rebaseline is not None:
        return rebaseline(cfg, a.rebaseline, ledger, apply=apply, now_iso=now_iso)
    tmp: Path | None = None
    try:
        if a.from_stamp:
            stamp, run_dir = a.from_stamp, cfg.outdir
            if not (run_dir / stamp_files(stamp)[2]).exists():
                _say("REFUSE: no such stamp in the out-dir")
                return EXIT_REFUSED
        else:
            if apply:
                run_dir = cfg.outdir
            else:
                tmp = Path(tempfile.mkdtemp(prefix="hygiene-drive-dryrun-"))
                run_dir = tmp
            before = listing(run_dir)
            _avoid_minute_collision(before)
            t0 = time.monotonic()
            rc = run_inventory(cfg.ps1, cfg.root, run_dir)
            elapsed = time.monotonic() - t0
            after = listing(run_dir)
            stamp, err = detect_new_stamp(before, after)
            if rc != 0:
                _say(f"inventory PS1 exited {rc} after {elapsed:.0f}s -- no report")
                if apply and stamp:
                    append_ledger(cfg.ledger, {"event": "created", "stamp": stamp, "status": "failed",
                                               "files": sorted(n for n in after - before if ROTATION_RE.match(n)),
                                               "full_root": None, "max_hash_mb": None, "ts": now_iso})
                return EXIT_PS1
            if err or not stamp:
                orphan = detect_summaryless_stamp(before, after)
                if orphan is not None:
                    return summaryless_walk(cfg, run_dir, orphan, after, ledger, apply=apply, now_iso=now_iso)
                _say(f"REFUSE: {err}")
                return EXIT_STAMP
            _say(f"walk done in {elapsed:.0f}s, stamp {stamp}")

        info = read_run(run_dir, stamp)
        if info is None:
            _say("REFUSE: the new stamp has no readable summary")
            return EXIT_STAMP
        if a.from_stamp and hdr.norm_root(info.summary.get("root")) != hdr.norm_root(cfg.root):
            # a hand/subtree run must never overwrite that day's report
            _say("REFUSE: --from-stamp names a run that is not of the full audit root")
            return EXIT_REFUSED
        files = load_rows(run_dir / stamp_files(stamp)[0])
        dirs = load_rows(run_dir / stamp_files(stamp)[1])
        runs = eligible_runs(cfg.outdir, stamp, cfg.root, ledger)
        prior = runs[0] if runs else None
        rows_n = {"files_rows": None if files is None else len(files), "dirs_rows": None if dirs is None else len(dirs)}
        why = sanity(info, cfg.root, prior, high=high_water(runs), **rows_n)
        prior_rows = load_prior_rows(cfg.outdir, prior)
        if prior is not None and prior_rows is None:
            why.append(f"the prior run's (stamp {prior.stamp}) files or folders CSV is missing or unreadable, "
                       "so there is nothing to compare this walk with")
        # The walk's OWN checks (everything but the comparison): when they all pass,
        # only the floor / the prior failed, and a re-baseline onto this stamp is
        # the operator's recovery (the INCOMPLETE report prints it).
        own_ok = not sanity(info, cfg.root, None, high={}, **rows_n)
        status = "incomplete" if why else ("clean" if prior else "unverified")
        full_root = hdr.norm_root(info.summary.get("root")) == hdr.norm_root(cfg.root)
        _say(f"files {info.summary.get('files_total')} dirs {info.summary.get('dirs_total')} "
             f"walk-errors {info.walk_errors} prior {prior.stamp if prior else 'none'} status {status}")

        if apply and not a.from_stamp:
            append_ledger(cfg.ledger, {
                "event": "created", "stamp": stamp, "status": status, "ts": now_iso,
                "files": [n for n in stamp_files(stamp) if (run_dir / n).exists()],
                "full_root": full_root, "max_hash_mb": info.summary.get("max_hash_mb"),
                "files_total": info.summary.get("files_total"), "dirs_total": info.summary.get("dirs_total"),
            })

        date = _report_date(stamp)
        if why:
            # the hint only where the re-baseline would be accepted: the walk is
            # sound and the stamp is (now) a runner-recorded full-root NO-HASH walk
            hint = own_ok and (not a.from_stamp or _is_runner_walk(ledger.get(stamp)))
            md = hdr.render_incomplete_md(
                date=date, stamp=stamp, reasons=why, files_total=info.summary.get("files_total"),
                dirs_total=info.summary.get("dirs_total"), prior_stamp=prior.stamp if prior else None,
                prior_files=prior.summary.get("files_total") if prior else None, walk_errors=info.walk_errors,
                rebaseline_stamp=stamp if hint else None)
            if apply:
                _say(f"INCOMPLETE WALK ({len(why)} reasons) -- report: {write_report(cfg.report_dir, date, md)}")
            else:
                _say(f"DRY-RUN: INCOMPLETE WALK ({len(why)} reasons); nothing written")
            return EXIT_INCOMPLETE

        now, diffs, md, sidecar_name, sidecar, withheld, ini_rows, ini_lex = build_outputs(
            stamp, prior_rows, prior, info, files, dirs, status)
        for key in hdr.CATEGORY_LABELS:
            d = diffs[key]
            _say(f"{key}: now {d.now} last {d.last if d.last is not None else 'n/a'} "
                 f"new {len(d.new) if d.last is not None else 'n/a'}")
        _say(f"desktop.ini {now['desktop-ini'].count} (LEX {now['desktop-ini'].lex}); "
             f"DELETE rows {len(ini_rows)}; sidecar withheld LEX {withheld['lex']} PHI {withheld['phi']}")
        if not apply:
            _say("DRY-RUN: nothing written (no report, no manifest, no sidecar, no ledger, no rotation)")
            return EXIT_OK
        result = write_report(cfg.report_dir, date, md)
        _say(f"report: {result}")
        wrote_side = write_create_only(cfg.outdir / sidecar_name, sidecar)
        _say(f"sidecar: {'written' if wrote_side else 'exists, not overwritten'}")
        if not a.from_stamp:
            ini_name = f"manifest-desktopini-{date}.csv"
            wrote_ini = write_create_only(cfg.outdir / ini_name, hdr.render_manifest_csv(ini_rows))
            _say(f"desktop.ini manifest: {'written' if wrote_ini else 'exists, not overwritten'}")
            removed = rotate(cfg.outdir, cfg.ledger, ledger_state(read_ledger(cfg.ledger)), cfg.root,
                             current=stamp, now_iso=now_iso)
            _say(f"rotation removed {removed} files")
        return EXIT_OK if result == "written" else EXIT_REPORT
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    # load_dotenv only when run as a script: an import-time override=True load
    # would clobber the test suite's env redirects (conftest SCRIPT_CONSTS note).
    try:
        from dotenv import load_dotenv
        load_dotenv(_REPO_ROOT / ".env", override=True)
    except Exception:  # noqa: BLE001 -- the runner needs no secret; .env is optional
        pass
    sys.exit(main())
