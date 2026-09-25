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
    collision -> exit 5; Export-Csv would have overwritten someone's run).
 7. Parses the summary + run log (counts only; the run log's lines carry paths).
 8. SANITY FLOOR: files AND folders >= 80% of the prior full run, 0 walk errors,
    CSV row counts == the summary totals. A failure writes an INCOMPLETE WALK
    report (no lists, no deltas) and exits 1 -- never a clean report on a
    partial walk (the "0 violations" failure mode).
 9. Prior = the newest earlier FULL-ROOT stamp this runner recorded as passing
    the floor (its stamp ledger), or the 2026-09-21 19:48 hashing baseline;
    never a hand/subtree run, never a run that failed the floor.
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
    """stamp -> its "created" row, with ``rotated`` set once a rotation row exists."""
    out: dict[str, dict] = {}
    for r in rows:
        st = r.get("stamp")
        if not isinstance(st, str):
            continue
        if r.get("event") == "created":
            out[st] = dict(r)
        elif r.get("event") == "rotated" and st in out:
            out[st]["rotated"] = True
    return out


def append_ledger(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def find_prior(outdir: Path, current: str, full_root: Path, ledger: dict[str, dict]) -> RunInfo | None:
    """The newest earlier stamp that is a runner run which passed the floor
    (ledger status clean/unverified, not rotated) or the 9/21 baseline, AND whose
    own summary says COMPLETE on the full root with 0 walk errors and both CSVs
    present. Hand and subtree runs are never eligible."""
    stamps = sorted({m.group(1) for n in listing(outdir) if (m := _SUMMARY_NAME_RE.match(n))}, reverse=True)
    for st in stamps:
        if st >= current:
            continue
        row = ledger.get(st)
        if st != BASELINE_STAMP:
            if not row or row.get("rotated") or row.get("status") not in ("clean", "unverified"):
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
        return info
    return None


def sanity(info: RunInfo, full_root: Path, prior: RunInfo | None, *, files_rows: int, dirs_rows: int) -> list[str]:
    """Reasons this walk cannot be trusted (empty list = it passed)."""
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
    if sm.get("files_total") is None or files_rows != sm.get("files_total"):
        why.append("the files CSV row count does not match the summary total")
    if sm.get("dirs_total") is None or dirs_rows != sm.get("dirs_total"):
        why.append("the folders CSV row count does not match the summary total")
    if prior is not None:
        for key, label in (("files_total", "files"), ("dirs_total", "folders")):
            now_n, prior_n = sm.get(key) or 0, prior.summary.get(key) or 0
            if prior_n and now_n < FLOOR_RATIO * prior_n:
                why.append(f"{label} walked {now_n} < {int(FLOOR_RATIO * 100)}% of the prior full run's {prior_n}")
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


def build_outputs(stamp: str, prior_dir: Path, prior: RunInfo | None, info: RunInfo,
                  files: list[dict], dirs: list[dict], status: str):
    now = hdr.categories(files, dirs)
    prior_cats = None
    if prior is not None:
        prior_cats = hdr.categories(hdr.load_csv_rows(prior_dir / stamp_files(prior.stamp)[0]),
                                    hdr.load_csv_rows(prior_dir / stamp_files(prior.stamp)[1]))
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Weekly Drive hygiene inventory + report (dry-run default)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--apply", action="store_true", help="write the report, sidecars, ledger, rotation")
    g.add_argument("--dry-run", action="store_true", help="(default) walk into a temp dir, write nothing")
    ap.add_argument("--from-stamp", default=None, help="rebuild from an existing out-dir stamp (no walk)")
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
        files = hdr.load_csv_rows(run_dir / stamp_files(stamp)[0])
        dirs = hdr.load_csv_rows(run_dir / stamp_files(stamp)[1])
        prior = find_prior(cfg.outdir, stamp, cfg.root, ledger)
        why = sanity(info, cfg.root, prior, files_rows=len(files), dirs_rows=len(dirs))
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
            md = hdr.render_incomplete_md(
                date=date, stamp=stamp, reasons=why, files_total=info.summary.get("files_total"),
                dirs_total=info.summary.get("dirs_total"), prior_stamp=prior.stamp if prior else None,
                prior_files=prior.summary.get("files_total") if prior else None, walk_errors=info.walk_errors)
            if apply:
                _say(f"INCOMPLETE WALK ({len(why)} reasons) -- report: {write_report(cfg.report_dir, date, md)}")
            else:
                _say(f"DRY-RUN: INCOMPLETE WALK ({len(why)} reasons); nothing written")
            return EXIT_INCOMPLETE

        now, diffs, md, sidecar_name, sidecar, withheld, ini_rows, ini_lex = build_outputs(
            stamp, cfg.outdir, prior, info, files, dirs, status)
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
