"""Code #15 R8 (cq-592baba613f1): the weekly Drive-hygiene inventory + report.

Two layers:
  * cora.hygiene_drive_report -- pure category / diff / render functions, driven
    with in-memory inventory rows;
  * scripts/run_hygiene_drive_weekly.py -- the runner, driven with a FAKE
    powershell (subprocess.run patched) that writes the four inventory files the
    real PS1 writes, plus two Windows-only integration tests that run the REAL
    vendored PS1 on a tmp tree.
Nothing here touches G:, Downloads or the live stamp ledger (tests/conftest.py
redirects every HYGIENE_* path to tmp; each test points them at its own world).
"""

from __future__ import annotations

import builtins
import csv
import hashlib
import io
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

import run_hygiene_drive_weekly as rh  # noqa: E402
from cora import drive_io  # noqa: E402
from cora import hygiene_drive_report as hdr  # noqa: E402

VENDORED_PS1 = REPO / "deployment" / "hygiene" / "folder-audit-inventory.ps1"
FILES_COLS = ["RelPath", "Dir", "Name", "Ext", "Bytes", "CreatedUtc", "ModifiedUtc", "TopLevel", "Depth",
              "Attributes", "IsPlaceholder", "IsDesktopIni", "DriveSuffix", "BaseName", "SizeGroup",
              "Sha256", "HashStatus"]
DIRS_COLS = ["RelPath", "Depth", "FileCount", "FileCountExclDesktopIni", "SubdirCount",
             "RecursiveFileCount", "IsEmptyExclDesktopIni", "HasDriveSuffix"]


# ── inventory row builders (the PS1's column shapes) ────────────────────────

def frow(rel: str, *, suffix: str = "", ini: bool = False, size: int = 10) -> dict:
    parts = rel.split("\\")
    name = parts[-1]
    return {"RelPath": rel, "Dir": "\\".join(parts[:-1]), "Name": name,
            "Ext": name.rsplit(".", 1)[-1].lower() if "." in name else "", "Bytes": str(size),
            "CreatedUtc": "2026-09-01T00:00:00Z", "ModifiedUtc": "2026-09-01T00:00:00Z",
            "TopLevel": parts[0] if len(parts) > 1 else "<root>", "Depth": str(len(parts) - 1),
            "Attributes": "Archive", "IsPlaceholder": "False", "IsDesktopIni": str(ini),
            "DriveSuffix": suffix, "BaseName": "", "SizeGroup": "1", "Sha256": "", "HashStatus": "NOT_NEEDED"}


def drow(rel: str, *, empty: bool = False, suffix: str = "", subdirs: int = 0) -> dict:
    return {"RelPath": rel, "Depth": str(len(rel.split("\\"))), "FileCount": "0",
            "FileCountExclDesktopIni": "0", "SubdirCount": str(subdirs), "RecursiveFileCount": "0",
            "IsEmptyExclDesktopIni": str(empty), "HasDriveSuffix": suffix}


def _csv_bytes(cols, rows) -> bytes:
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=cols, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")


def summary_text(*, root: str, outdir: str, files_total: int, dirs_total: int, status: str = "COMPLETE",
                 max_hash: int = 0, hashed: int = 0, errors: int = 0) -> str:
    return "\r\n".join([
        "HJR Founder OS - Folder Audit Inventory Summary",
        "================================================", "",
        f"Run status:    {status}",
        "Start (local): 2026-09-26 02:40:01", "End (local):   2026-09-26 02:41:30",
        "Elapsed:       00:01:29.0000000", "",
        "Parameters", "----------",
        f"  Root           = {root}", f"  OutDir         = {outdir}",
        "  IncludeOffline = False", f"  MaxHashMB      = {max_hash}", "  HashAll        = False",
        "  Quiet          = True", "",
        "Totals", "------",
        f"  Total files scanned: {files_total}", f"  Total dirs scanned:  {dirs_total}",
        "  Total bytes:         100", "",
        "Hashing results", "---------------",
        f"  HASHED:          {hashed}", "  NOT_NEEDED:      0", "",
        f"Walk/hash errors logged: {errors}",
        "(See _run-log-x.txt for individual walk/hash error messages.)",
    ])


def write_stamp(outdir: Path, stamp: str, files: list[dict], dirs: list[dict], *, root: Path | str,
                status: str = "COMPLETE", max_hash: int = 0, hashed: int = 0,
                runlog: list[str] | None = None, files_total: int | None = None,
                dirs_total: int | None = None) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / f"inventory-files-{stamp}.csv").write_bytes(_csv_bytes(FILES_COLS, files))
    (outdir / f"inventory-dirs-{stamp}.csv").write_bytes(_csv_bytes(DIRS_COLS, dirs))
    runlog = runlog or []
    (outdir / f"inventory-summary-{stamp}.txt").write_bytes(b"\xef\xbb\xbf" + summary_text(
        root=str(root), outdir=str(outdir), files_total=len(files) if files_total is None else files_total,
        dirs_total=len(dirs) if dirs_total is None else dirs_total, status=status, max_hash=max_hash,
        hashed=hashed, errors=len(runlog)).encode("utf-8"))
    (outdir / f"_run-log-{stamp}.txt").write_bytes(
        b"\xef\xbb\xbf" + ("\r\n".join(runlog) if runlog else "No walk or hash errors recorded.").encode("utf-8"))


# ── the world: env redirects + a fake powershell ────────────────────────────

@dataclass
class World:
    root: Path
    outdir: Path
    report_dir: Path
    ledger: Path
    ps1: Path
    calls: list

    def report(self, date: str = "2026-09-26") -> Path:
        return self.report_dir / f"{date}_fndr_hygiene-findings.md"

    def ledger_rows(self) -> list[dict]:
        return rh.read_ledger(self.ledger)


BASE_FILES = [frow("02-F3-Energy\\reports\\a.pdf"), frow("00-Founder\\CLAUDE.md"),
              frow("02-F3-Energy\\desktop.ini", ini=True)]
BASE_DIRS = [drow("00-Founder"), drow("02-F3-Energy"), drow("02-F3-Energy\\reports")]


@pytest.fixture
def world(tmp_path, monkeypatch) -> World:
    root = tmp_path / "root"
    report_dir = root / "_shared" / "hygiene-pending-moves"
    report_dir.mkdir(parents=True)
    outdir = tmp_path / "out"
    outdir.mkdir()
    ps1 = tmp_path / "fake-inventory.ps1"
    ps1.write_bytes(b"# fake inventory for tests\n")
    ledger = tmp_path / "state" / "stamps.jsonl"
    monkeypatch.setenv("HYGIENE_AUDIT_ROOT", str(root))
    monkeypatch.setenv("HYGIENE_AUDIT_OUTDIR", str(outdir))
    monkeypatch.setenv("HYGIENE_REPORT_DIR", str(report_dir))
    monkeypatch.setenv("HYGIENE_STAMP_LEDGER_PATH", str(ledger))
    monkeypatch.setenv("HYGIENE_INVENTORY_PS1", str(ps1))
    monkeypatch.setattr(rh, "INVENTORY_PS1_SHA256", hashlib.sha256(ps1.read_bytes()).hexdigest())
    return World(root, outdir, report_dir, ledger, ps1, [])


def fake_ps(monkeypatch, world: World, *, stamp: str = "20260926-0240", files=None, dirs=None,
            rc: int = 0, write: bool = True, **kw):
    """Patch subprocess.run with a fake PS1 that writes one stamp into -OutDir."""
    files = list(BASE_FILES if files is None else files)
    dirs = list(BASE_DIRS if dirs is None else dirs)

    def fake(cmd, **kwargs):
        world.calls.append((list(cmd), dict(kwargs)))
        out = Path(cmd[cmd.index("-OutDir") + 1])
        root = cmd[cmd.index("-Root") + 1]
        if write:
            write_stamp(out, stamp, files, dirs, root=root, **kw)
        return subprocess.CompletedProcess(cmd, rc, "", "")
    monkeypatch.setattr(rh.subprocess, "run", fake)
    return fake


def seed_prior(world: World, stamp: str, *, files=None, dirs=None, status: str = "clean",
               ledger: bool = True, root: Path | str | None = None, **kw) -> None:
    files = list(BASE_FILES if files is None else files)
    dirs = list(BASE_DIRS if dirs is None else dirs)
    write_stamp(world.outdir, stamp, files, dirs, root=root or world.root, **kw)
    if ledger:
        rh.append_ledger(world.ledger, {"event": "created", "stamp": stamp, "status": status,
                                        "files": list(rh.stamp_files(stamp)), "full_root": True,
                                        "max_hash_mb": 0, "ts": "2026-09-01T00:00:00Z"})


# ═════════════════════════════════════════════════════════════════════════════
# 1. the pure report
# ═════════════════════════════════════════════════════════════════════════════

def _render(files, dirs, prior=None, **facts):
    now = hdr.categories(files, dirs)
    pc = hdr.categories(*prior) if prior else None
    diffs = hdr.diff(now, pc)
    f = hdr.RunFacts(date="2026-09-26", stamp="20260926-0240", files_total=len(files),
                     dirs_total=len(dirs), walk_errors=0, prior_stamp="20260919-0240" if prior else None,
                     status="clean" if prior else "unverified", sidecar_name="side.csv")
    for k, v in facts.items():
        setattr(f, k, v)
    return hdr.render_md(f, now, diffs), now, diffs


def test_lex_names_and_paths_never_reach_the_report_or_the_sidecar():
    files = [frow("08-Lexington-Services\\clients\\Jane intake (1).pdf", suffix="paren-n"),
             frow("08-Lexington-Services\\loose (1).pdf", suffix="paren-n"),
             frow("02-F3-Energy\\ok (1).pdf", suffix="paren-n")]
    dirs = [drow("08-Lexington-Services\\projects\\" + "x" * 40), drow("08-Lexington-Services\\empty", empty=True),
            drow("08-Lexington-Services\\reports 2", suffix="space-2")]
    md, now, diffs = _render(files, dirs)
    lex_lines = [ln for ln in md.splitlines() if "Lexington" in ln]
    assert len(lex_lines) == 1 and lex_lines[0].startswith("- LEX (`08-Lexington-Services`"), \
        "the only LEX mention is the fixed counts-only disclaimer"
    assert "Jane" not in md and "loose (1)" not in md and "x" * 40 not in md
    assert "`02-F3-Energy\\ok (1).pdf`" in md
    assert "2 LEX (counts only)" in md
    assert now["slug-40"].lex == 1 and now["empty-dirs"].lex == 1 and now["suffix-dirs"].lex == 1
    side, withheld = hdr.render_full_list_csv(now, diffs)
    assert b"Lexington" not in side and withheld["lex"] == 5


def test_lex_and_copa_named_paths_outside_the_partition_are_counts_only():
    """Measured live 2026-09-24: 'Lexington Entities' folders in the HJRG accounting
    tree and 'Lexington - Progress (16).gdoc' meeting summaries in _shared, plus
    whole-word COPA meeting exports (NDA) -- none under 08-Lexington-Services."""
    lexish = ["_shared\\meetings\\Fireflies Meetings\\Summaries\\Lexington - Progress (16).gdoc",
              "01-HJR-Global\\accounting\\visibility-binder\\01 Lexington Entities\\LLC\\stmt (1).pdf",
              "_shared\\meetings\\COPA diligence sync (2).gdoc"]
    fine = ["01-HJR-Global\\accounting\\visibility-binder\\02 Non-Lexington\\F3 Energy LLC\\inv (1).pdf",
            "09-One-Stop-Nutrition\\Maricopa county permit (1).pdf"]
    md, now, diffs = _render([frow(p, suffix="paren-n") for p in lexish + fine], [])
    for p in lexish:
        assert hdr.is_lex_relpath(p), p
        assert p.rsplit("\\", 1)[-1] not in md
    for p in fine:
        assert not hdr.is_lex_relpath(p), p
        assert f"`{p}`" in md
    assert "3 LEX (counts only)" in md
    side, withheld = hdr.render_full_list_csv(now, diffs)
    assert b"Progress" not in side and b"COPA" not in side and withheld["lex"] == 3


def test_kb_pinned_container_names_are_counts_only():
    pinned = ["00-Founder\\personal-finances\\tax (1).pdf",
              "_shared\\projects\\cora\\notes (1).md",
              "02-F3-Energy\\projects\\capital-raise\\deck (1).pdf",
              "01-HJR-Global\\accounting\\cashflow-ledger\\w (1).csv",
              "00-Founder\\travel-points\\pts (1).xlsx",
              "00-Founder\\insurance\\oneamerica\\p (1).pdf"]
    md, now, diffs = _render([frow(p, suffix="paren-n") for p in pinned], [])
    for p in pinned:
        assert p not in md
        assert p.rsplit("\\", 1)[-1] not in md
    assert "6 under KB-pinned containers (counts only)" in md
    side, _ = hdr.render_full_list_csv(now, diffs)
    rows = list(csv.DictReader(io.StringIO(side.decode("utf-8-sig"))))
    assert {r["zone"] for r in rows} == {"kb-pinned"}, "Downloads sidecar flags them; the KB door never names them"


def test_archive_is_excluded_from_offender_lists_and_counted_separately():
    files = [frow("_archive\\dedup-2026-09\\x (1).pdf", suffix="paren-n"), frow("02-F3-Energy\\y (1).pdf", suffix="paren-n")]
    dirs = [drow("_archive\\02-F3-Energy 2", suffix="space-2"), drow("_archive\\old", empty=True)]
    md, now, _ = _render(files, dirs)
    assert "_archive\\" not in md
    assert now["suffix-paren-n"].count == 1 and now["suffix-paren-n"].archive == 1
    assert now["suffix-dirs"].count == 0 and now["suffix-dirs"].archive == 1
    assert now["empty-dirs"].archive == 1


def test_forty_character_slugs_and_x2_folders_are_detected():
    slug40 = "a" * 40
    dirs = [drow("00-Founder\\projects\\" + slug40), drow("02-F3-Energy\\projects\\" + "b" * 39),
            drow("_shared\\projects\\" + "c" * 40), drow("02-F3-Energy\\projects\\" + "d" * 40 + "\\sub"),
            drow("02-F3-Energy\\brand 2", suffix="space-2"), drow("06-HJR-Properties\\leases (1)", suffix="paren-n")]
    md, now, _ = _render([], dirs)
    assert now["slug-40"].items == {"00-Founder\\projects\\" + slug40, "_shared\\projects\\" + "c" * 40}
    assert now["suffix-dirs"].items == {"02-F3-Energy\\brand 2", "06-HJR-Properties\\leases (1)"}


def test_week_over_week_new_vs_standing():
    prior = ([frow("02-F3-Energy\\old (1).pdf", suffix="paren-n"),
              frow("02-F3-Energy\\fixed (1).pdf", suffix="paren-n")], [])
    files = [frow("02-F3-Energy\\old (1).pdf", suffix="paren-n"), frow("02-F3-Energy\\new (1).pdf", suffix="paren-n")]
    md, now, diffs = _render(files, [], prior=prior)
    d = diffs["suffix-paren-n"]
    assert (d.now, d.last, d.delta, d.new, d.resolved) == (2, 2, 0, {"02-F3-Energy\\new (1).pdf"}, 1)
    assert "- NEW `02-F3-Energy\\new (1).pdf`" in md
    assert "- `02-F3-Energy\\old (1).pdf`" in md
    assert md.index("new (1)") < md.index("old (1)"), "NEW offenders are listed first"


def test_phi_screen_withholds_a_listed_name():
    phi = "09-One-Stop-Nutrition\\clients\\patient John Smith diagnosis (1).pdf"
    md, now, diffs = _render([frow(phi, suffix="paren-n")], [])
    assert "John Smith" not in md and "1 withheld by the PHI screen" in md
    side, withheld = hdr.render_full_list_csv(now, diffs)
    assert b"John Smith" not in side and withheld["phi"] == 1


def test_lists_cap_at_fifty_and_the_sidecar_has_the_full_list():
    files = [frow(f"02-F3-Energy\\f{i:03d} (1).pdf", suffix="paren-n") for i in range(60)]
    md, now, diffs = _render(files, [])
    assert sum(1 for line in md.splitlines() if line.startswith("- `02-F3-Energy\\f")) == 50
    assert "10 more in the full-list sidecar" in md
    side, _ = hdr.render_full_list_csv(now, diffs)
    assert len(list(csv.DictReader(io.StringIO(side.decode("utf-8-sig"))))) == 60


def test_recomputed_underscore_count_is_count_only():
    files = [frow("02-F3-Energy\\img_10.png", suffix=""), frow("02-F3-Energy\\img_2.png", suffix="underscore-n")]
    md, now, _ = _render(files, [])
    assert now["suffix-underscore-any"].count == 2 and now["suffix-underscore-n"].count == 1
    assert "img_10.png" not in md, "the superset is a labelled count, never an offender list"


def test_loose_root_noncanonical_top_and_desktop_ini_trend():
    files = [frow("stray.txt"), frow("CLAUDE.md"), frow("desktop.ini", ini=True),
             frow("00-Founder\\loose.md"), frow("02-F3-Energy\\desktop.ini", ini=True)]
    dirs = [drow("00-Founder"), drow("Slack Deep Dive"), drow("_inbox")]
    md, now, _ = _render(files, dirs)
    assert now["loose-root"].items == {"stray.txt"}
    assert now["loose-founder-root"].items == {"00-Founder\\loose.md"}
    assert now["noncanonical-top"].items == {"Slack Deep Dive"}
    assert now["desktop-ini"].count == 2
    assert "desktop.ini (trend only" in md


def test_desktop_ini_delete_rows_are_non_lex_and_apply_schema():
    files = [frow("02-F3-Energy\\desktop.ini", ini=True), frow("08-Lexington-Services\\desktop.ini", ini=True),
             frow("_archive\\x\\desktop.ini", ini=True), frow("02-F3-Energy\\a.pdf"),
             frow("_archive\\08-Lexington-Services\\y\\desktop.ini", ini=True),
             frow("01-HJR-Global\\accounting\\Lexington Entities\\desktop.ini", ini=True)]
    rows, lex = hdr.desktop_ini_rows(files)
    assert lex == 2, "the partition (incl. its archived copies) is excluded -- apply.ps1 gate 2"
    assert [r["source_relpath"] for r in rows] == ["02-F3-Energy\\desktop.ini", "_archive\\x\\desktop.ini",
                                                   "01-HJR-Global\\accounting\\Lexington Entities\\desktop.ini"]
    assert all(r["action"] == "DELETE" and r["reason_code"] == "D3-desktopini" and r["sha256"] == "" for r in rows)
    data = hdr.render_manifest_csv(rows)
    assert data.startswith(hdr.MANIFEST_HEADER_BYTES) and b"08-Lexington" not in data


def test_summary_and_run_log_parsing():
    s = hdr.parse_summary("\ufeff" + summary_text(root="G:\\My Drive\\HJR-Founder-OS", outdir="C:\\o",
                                                  files_total=69641, dirs_total=5820, errors=3))
    assert s["run_status"] == "COMPLETE" and s["root"] == "G:\\My Drive\\HJR-Founder-OS"
    assert (s["max_hash_mb"], s["files_total"], s["dirs_total"], s["hashed"], s["errors_logged"]) == (0, 69641, 5820, 0, 3)
    c = hdr.count_run_log("\ufeffWALK-DIR-ERROR: G:\\x :: denied\r\ncontinuation line\r\nHASH-ERROR: y :: z\r\n")
    assert c["WALK-DIR-ERROR"] == 1 and c["HASH-ERROR"] == 1 and hdr.walk_error_count(c) == 1


def test_the_report_module_is_not_bot_loaded():
    for p in (REPO / "src" / "cora").rglob("*.py"):
        if p.name == "hygiene_drive_report.py":
            continue
        assert "hygiene_drive_report" not in p.read_text(encoding="utf-8"), p


# ═════════════════════════════════════════════════════════════════════════════
# 2. the runner
# ═════════════════════════════════════════════════════════════════════════════

def test_a_clean_apply_run_writes_the_report_sidecars_and_ledger(world, monkeypatch):
    seed_prior(world, "20260919-0240")
    files = BASE_FILES + [frow("02-F3-Energy\\new (1).pdf", suffix="paren-n")]
    fake_ps(monkeypatch, world, files=files)
    assert rh.main(["--apply"]) == rh.EXIT_OK
    md = world.report().read_text(encoding="utf-8")
    assert md.startswith(hdr.GENERATOR_MARKER)
    assert "CLEAN" in md and "stamp `20260919-0240`" in md and "- NEW `02-F3-Energy\\new (1).pdf`" in md
    assert (world.outdir / "hygiene-findings-full-2026-09-26.csv").exists()
    ini = (world.outdir / "manifest-desktopini-2026-09-26.csv").read_bytes()
    assert ini.startswith(hdr.MANIFEST_HEADER_BYTES) and b"02-F3-Energy\\desktop.ini" in ini
    created = [r for r in world.ledger_rows() if r["event"] == "created" and r["stamp"] == "20260926-0240"]
    assert created and created[0]["status"] == "clean" and created[0]["full_root"] is True


def test_the_powershell_child_is_windowless_no_hash_and_the_vendored_path(world, monkeypatch):
    fake_ps(monkeypatch, world)
    rh.main(["--apply"])
    (cmd, kw), = world.calls
    assert cmd[0] == rh.POWERSHELL
    assert cmd[cmd.index("-File") + 1] == str(world.ps1)
    assert cmd[cmd.index("-MaxHashMB") + 1] == "0" and "-Quiet" in cmd
    assert cmd[cmd.index("-Root") + 1] == str(world.root)
    assert kw["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert kw["timeout"] == rh.PS1_TIMEOUT_S and kw["capture_output"] is True


@pytest.mark.parametrize("rc", [1, 2, 3])
def test_a_failed_ps1_writes_no_report(world, monkeypatch, rc):
    fake_ps(monkeypatch, world, rc=rc, status="ABORTED" if rc == 1 else "COMPLETE", write=(rc == 1))
    assert rh.main(["--apply"]) == rh.EXIT_PS1
    assert not any(world.report_dir.iterdir())
    assert not list(world.outdir.glob("manifest-desktopini-*"))


def test_a_truncated_walk_is_never_a_clean_report(world, monkeypatch):
    many = [frow(f"02-F3-Energy\\r\\f{i}.pdf") for i in range(100)]
    seed_prior(world, "20260919-0240", files=many)
    fake_ps(monkeypatch, world, files=many[:50] + [frow("02-F3-Energy\\x (1).pdf", suffix="paren-n")])
    assert rh.main(["--apply"]) == rh.EXIT_INCOMPLETE
    md = world.report().read_text(encoding="utf-8")
    assert "INCOMPLETE WALK" in md and "files walked 51 < 80%" in md
    assert "x (1).pdf" not in md and "Week over week" not in md, "no lists, no deltas on a partial walk"
    assert not list(world.outdir.glob("manifest-desktopini-*"))
    # next week the incomplete stamp is NOT the prior: the floor stays anchored
    fake_ps(monkeypatch, world, stamp="20261003-0240", files=many)
    assert rh.main(["--apply"]) == rh.EXIT_OK
    assert "stamp `20260919-0240`" in world.report("2026-10-03").read_text(encoding="utf-8")


def test_walk_errors_make_the_walk_incomplete_and_their_paths_never_leak(world, monkeypatch, capsys):
    seed_prior(world, "20260919-0240")
    fake_ps(monkeypatch, world, runlog=["WALK-DIR-ERROR: G:\\x\\08-Lexington-Services\\secret :: denied"])
    assert rh.main(["--apply"]) == rh.EXIT_INCOMPLETE
    md = world.report().read_text(encoding="utf-8")
    assert "1 walk errors logged" in md and "Lexington" not in md and "secret" not in md
    assert "secret" not in capsys.readouterr().out


def test_a_csv_row_count_mismatch_is_incomplete(world, monkeypatch):
    seed_prior(world, "20260919-0240")
    fake_ps(monkeypatch, world, files_total=len(BASE_FILES) + 5)
    assert rh.main(["--apply"]) == rh.EXIT_INCOMPLETE
    assert "row count" in world.report().read_text(encoding="utf-8")


def test_dry_run_writes_nothing_anywhere(world, monkeypatch, capsys):
    seed_prior(world, "20260919-0240")
    fake_ps(monkeypatch, world)
    before_out = sorted(os.listdir(world.outdir))
    before_ledger = world.ledger.read_bytes()
    real_open = builtins.open

    def guarded_open(file, mode="r", *a, **kw):
        if any(ch in mode for ch in "wax+") and "hygiene-drive-dryrun-" not in str(file):
            raise AssertionError(f"dry-run opened {file!r} for writing")
        return real_open(file, mode, *a, **kw)

    def boom(*a, **k):
        raise AssertionError("dry-run reached a write site")
    monkeypatch.setattr(builtins, "open", guarded_open)
    for name in ("write_text_atomic", "write_bytes_atomic", "append_text"):
        monkeypatch.setattr(drive_io, name, boom)
    monkeypatch.setattr(os, "remove", boom)
    monkeypatch.setattr(rh, "append_ledger", boom)
    monkeypatch.setattr(rh, "write_create_only", boom)
    assert rh.main([]) == rh.EXIT_OK
    monkeypatch.setattr(builtins, "open", real_open)
    assert sorted(os.listdir(world.outdir)) == before_out
    assert world.ledger.read_bytes() == before_ledger
    assert not any(world.report_dir.iterdir())
    (cmd, _), = world.calls
    tmp_out = Path(cmd[cmd.index("-OutDir") + 1])
    assert "hygiene-drive-dryrun-" in tmp_out.name and not tmp_out.exists(), "the temp walk (LEX paths) is removed"
    assert "DRY-RUN: nothing written" in capsys.readouterr().out


def test_rotation_deletes_only_the_runners_own_old_stamps(world, monkeypatch):
    own = ["20260725-0240", "20260801-0240", "20260808-0240", "20260815-0240",
           "20260822-0240", "20260829-0240", "20260905-0240"]
    for st in own:
        seed_prior(world, st)
    # a ledger stamp whose summary now describes a SUBTREE run -> never deleted
    write_stamp(world.outdir, own[0], BASE_FILES, BASE_DIRS, root=world.root / "02-F3-Energy")
    write_stamp(world.outdir, "20260921-1948", BASE_FILES, BASE_DIRS, root=world.root, max_hash=200)  # baseline
    write_stamp(world.outdir, "20260922-1316", BASE_FILES, BASE_DIRS, root=world.root / "02-F3-Energy")  # hand subtree
    write_stamp(world.outdir, "20260801-1111", BASE_FILES, BASE_DIRS, root=world.root)  # hand full run, not ledgered
    for keep in ("manifest-dedup-2026-09.csv", "_applied-20260922-094411.csv", "_dryrun-20260922-094143.csv",
                 "manifest-desktopini-2026-09-19.csv"):
        (world.outdir / keep).write_text("x", encoding="utf-8")
    fake_ps(monkeypatch, world)
    assert rh.main(["--apply"]) == rh.EXIT_OK
    present = set(os.listdir(world.outdir))
    gone = {"20260801-0240", "20260808-0240", "20260815-0240"}
    kept = {"20260725-0240", "20260822-0240", "20260829-0240", "20260905-0240", "20260926-0240",
            "20260921-1948", "20260922-1316", "20260801-1111"}
    for st in gone:
        assert not any(st in n for n in present), st
    for st in kept:
        assert all(n in present for n in rh.stamp_files(st)), st
    for keep in ("manifest-dedup-2026-09.csv", "_applied-20260922-094411.csv", "_dryrun-20260922-094143.csv",
                 "manifest-desktopini-2026-09-19.csv"):
        assert keep in present
    rotated = sorted(r["stamp"] for r in world.ledger_rows() if r["event"] == "rotated")
    assert rotated == sorted(gone)


def test_rotation_regex_never_matches_protected_names():
    for name in ("manifest-inventory-files-20260901-0240.csv", "_applied-20260922-094411.csv",
                 "_dryrun-20260922-094143.csv", "inventory-files-20260901-0240.csv.bak",
                 "hygiene-findings-full-2026-09-26.csv", "manifest-desktopini-2026-09-26.csv"):
        assert not rh.ROTATION_RE.match(name), name
    assert rh.ROTATION_RE.match("_run-log-20260901-0240.txt")


def test_the_report_dir_is_never_created(world, monkeypatch):
    world.report_dir.rmdir()
    fake_ps(monkeypatch, world)
    assert rh.main(["--apply"]) == rh.EXIT_REPORT
    assert not world.report_dir.exists()


def test_a_same_named_file_without_the_marker_is_never_clobbered(world, monkeypatch):
    world.report().write_text("hand-written note\n", encoding="utf-8")
    fake_ps(monkeypatch, world)
    assert rh.main(["--apply"]) == rh.EXIT_REPORT
    assert world.report().read_text(encoding="utf-8") == "hand-written note\n"
    world.report().write_text(hdr.GENERATOR_MARKER + "\nold run\n", encoding="utf-8")
    fake_ps(monkeypatch, world, stamp="20260926-0241")
    assert rh.main(["--apply"]) == rh.EXIT_OK
    assert "old run" not in world.report().read_text(encoding="utf-8")


def test_a_ps1_hash_mismatch_refuses_before_any_walk(world, monkeypatch):
    fake_ps(monkeypatch, world)
    monkeypatch.setattr(rh, "INVENTORY_PS1_SHA256", "0" * 64)
    assert rh.main(["--apply"]) == rh.EXIT_REFUSED
    assert world.calls == [] and not any(world.report_dir.iterdir())


def test_an_unavailable_root_is_no_run(world, monkeypatch, tmp_path):
    monkeypatch.setenv("HYGIENE_AUDIT_ROOT", str(tmp_path / "gone"))
    fake_ps(monkeypatch, world)
    assert rh.main(["--apply"]) == rh.EXIT_UNAVAILABLE
    assert world.calls == []


def test_an_out_dir_inside_the_root_is_refused(world, monkeypatch):
    monkeypatch.setenv("HYGIENE_AUDIT_OUTDIR", str(world.root / "_shared"))
    fake_ps(monkeypatch, world)
    assert rh.main(["--apply"]) == rh.EXIT_REFUSED
    assert world.calls == []


def test_the_conftest_default_fails_closed(monkeypatch):
    """No world fixture: the autouse redirect alone must reach neither G: nor a walk."""
    called = []
    monkeypatch.setattr(rh.subprocess, "run", lambda *a, **k: called.append(a))
    assert rh.main(["--apply"]) in (rh.EXIT_UNAVAILABLE, rh.EXIT_REFUSED)
    assert called == []
    cfg = rh.load_config()
    assert "Downloads" not in str(cfg.outdir) and not str(cfg.root).startswith("G:")


def test_stamp_detection_and_collision():
    before = {"inventory-summary-20260919-0240.txt", "inventory-files-20260926-0240.csv"}
    after = before | {"inventory-summary-20260926-0240.txt", "inventory-dirs-20260926-0240.csv"}
    stamp, err = rh.detect_new_stamp(before, after)
    assert stamp is None and "collision" in err
    stamp, err = rh.detect_new_stamp(set(), set(rh.stamp_files("20260926-0240")))
    assert (stamp, err) == ("20260926-0240", None)
    assert rh.detect_new_stamp(before, before)[1] == "no new inventory stamp appeared"


def test_minute_collision_waits_into_a_fresh_minute():
    slept = []
    now = datetime(2026, 9, 26, 2, 40, 5)
    rh._avoid_minute_collision({"inventory-files-20260926-0240.csv"}, now_fn=lambda: now,
                               sleep_fn=slept.append)
    assert slept and slept[0] == 56
    slept.clear()
    rh._avoid_minute_collision({"inventory-files-20260919-0240.csv"}, now_fn=lambda: now, sleep_fn=slept.append)
    assert slept == []


def test_prior_selection_skips_hand_subtree_incomplete_rotated_and_errored_runs(world):
    write_stamp(world.outdir, "20260921-1948", BASE_FILES, BASE_DIRS, root=world.root, max_hash=200)
    write_stamp(world.outdir, "20260922-1316", BASE_FILES, BASE_DIRS, root=world.root / "02-F3-Energy")
    write_stamp(world.outdir, "20260923-0900", BASE_FILES, BASE_DIRS, root=world.root)            # hand full run
    seed_prior(world, "20260924-0240", status="incomplete")
    seed_prior(world, "20260925-0240", runlog=["WALK-FILE-ERROR: x :: y"])
    seed_prior(world, "20260925-0300")
    rh.append_ledger(world.ledger, {"event": "rotated", "stamp": "20260925-0300", "removed": 0})
    led = rh.ledger_state(rh.read_ledger(world.ledger))
    prior = rh.find_prior(world.outdir, "20260926-0240", world.root, led)
    assert prior is not None and prior.stamp == "20260921-1948", "only the 9/21 baseline is eligible here"
    seed_prior(world, "20260925-0400")
    led = rh.ledger_state(rh.read_ledger(world.ledger))
    assert rh.find_prior(world.outdir, "20260926-0240", world.root, led).stamp == "20260925-0400"


def test_from_stamp_rebuilds_without_walking(world, monkeypatch):
    seed_prior(world, "20260919-0240")
    seed_prior(world, "20260926-0240")
    fake_ps(monkeypatch, world)
    assert rh.main(["--from-stamp", "20260926-0240", "--apply"]) == rh.EXIT_OK
    assert world.calls == [] and world.report().exists()
    assert not list(world.outdir.glob("manifest-desktopini-*")), "--from-stamp writes the .md + sidecar only"


def test_from_stamp_refuses_a_subtree_run(world, monkeypatch):
    write_stamp(world.outdir, "20260927-1316", BASE_FILES, BASE_DIRS, root=world.root / "02-F3-Energy")
    assert rh.main(["--from-stamp", "20260927-1316", "--apply"]) == rh.EXIT_REFUSED
    assert not any(world.report_dir.iterdir())


# ═════════════════════════════════════════════════════════════════════════════
# 3. the REAL vendored PS1 (Windows only)
# ═════════════════════════════════════════════════════════════════════════════

def _tree(root: Path) -> None:
    for rel, data in {
        "00-Founder\\notes.md": b"same-size-A",
        "02-F3-Energy\\reports\\a.pdf": b"same-size-A",          # a size group: hashed by default
        "02-F3-Energy\\reports\\a (1).pdf": b"same-size-A",
        "08-Lexington-Services\\clients\\intake (1).pdf": b"lex-content",
        "stray.txt": b"x",
    }.items():
        p = root.joinpath(*rel.split("\\"))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    (root / "_shared" / "hygiene-pending-moves").mkdir(parents=True)
    (root / "04-UFL" / "empty").mkdir(parents=True)


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    return {str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*")}


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell 5.1")
def test_the_vendored_ps1_with_maxhashmb_0_hashes_nothing_and_writes_only_its_outdir(tmp_path):
    root, out = tmp_path / "root", tmp_path / "out"
    _tree(root)
    before = _snapshot(root)
    proc = subprocess.run([rh.POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                           "-File", str(VENDORED_PS1), "-Root", str(root), "-OutDir", str(out),
                           "-MaxHashMB", "0", "-Quiet"], capture_output=True, text=True, timeout=300,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert proc.returncode == 0, proc.stderr
    assert _snapshot(root) == before, "the walk must not touch the audited tree"
    names = sorted(os.listdir(out))
    assert len(names) == 4 and all(rh.ROTATION_RE.match(n) for n in names)
    stamp = rh.ROTATION_RE.match(names[0]).group(1)
    info = rh.read_run(out, stamp)
    assert info.summary["hashed"] == 0 and info.summary["max_hash_mb"] == 0
    assert info.summary["run_status"] == "COMPLETE" and info.walk_errors == 0
    files = hdr.load_csv_rows(out / f"inventory-files-{stamp}.csv")
    assert len(files) == info.summary["files_total"] == 5
    assert {r["HashStatus"] for r in files} <= {"SKIPPED_SIZE", "NOT_NEEDED", "SKIPPED_ZERO"}


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell 5.1")
def test_the_runner_end_to_end_with_the_real_ps1(tmp_path, monkeypatch):
    root, out = tmp_path / "root", tmp_path / "out"
    _tree(root)
    out.mkdir()
    monkeypatch.setenv("HYGIENE_AUDIT_ROOT", str(root))
    monkeypatch.setenv("HYGIENE_AUDIT_OUTDIR", str(out))
    monkeypatch.setenv("HYGIENE_REPORT_DIR", str(root / "_shared" / "hygiene-pending-moves"))
    monkeypatch.setenv("HYGIENE_STAMP_LEDGER_PATH", str(tmp_path / "stamps.jsonl"))
    monkeypatch.setenv("HYGIENE_INVENTORY_PS1", str(VENDORED_PS1))
    assert rh.main(["--apply"]) == rh.EXIT_OK
    reports = list((root / "_shared" / "hygiene-pending-moves").glob("*_fndr_hygiene-findings.md"))
    assert len(reports) == 1
    md = reports[0].read_text(encoding="utf-8")
    assert "UNVERIFIED" in md and "`02-F3-Energy\\reports\\a (1).pdf`" in md and "`stray.txt`" in md
    assert "intake" not in md and "1 LEX (counts only)" in md
    rows = rh.read_ledger(tmp_path / "stamps.jsonl")
    assert rows and rows[0]["full_root"] is True and rows[0]["max_hash_mb"] == 0
