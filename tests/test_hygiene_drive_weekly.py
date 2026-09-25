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
import re
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


# D-051 R1 rb2-r8#0 + lens-d082#0: LEX sub-entity / program / lead names OUTSIDE
# the partition -- in _shared\meetings (Fireflies exports) and in 01-HJR-Global
# (the accounting binder) -- were listed verbatim in the KB-ingested .md.
LEX_NAMED_OUTSIDE_08 = [
    "_shared\\meetings\\LBHS Leadership Sync (1).gdoc",
    "_shared\\meetings\\Fireflies Meetings\\Summaries\\Lex-LLC Ops Review (2).gdoc",
    "_shared\\meetings\\lts weekly huddle (1).gdoc",
    "_shared\\meetings\\LLA Ops (1).gdoc",
    "_shared\\meetings\\Shaun Hawkins 1-1 (1).gdoc",          # a named LEX lead (title keyword)
    "_shared\\meetings\\Jared Harker check-in (1).gdoc",      # a named LEX lead (attendee signal)
    "_shared\\meetings\\Case Conference notes (1).gdoc",      # a clinical title
    "_shared\\meetings\\HCBS rates (1).pdf",
    "_shared\\meetings\\Lex Services board (1).gdoc",
    "01-HJR-Global\\accounting\\Lex-LLC recon (1).xlsx",
    "01-HJR-Global\\accounting\\visibility-binder\\02 Non-Lexington\\Copy of Lex Services P&L (1).xlsx",
    "01-HJR-Global\\accounting\\visibility-binder\\02 Non-Lexington\\Copy of LBHS budget (1).xlsx",
    "01-HJR-Global\\accounting\\DDD contract (1).pdf",
    "01-HJR-Global\\accounting\\LTS_budget (1).xlsx",           # '_' is a boundary for the belt
    "01-HJR-Global\\accounting\\LexLLC 2026 (1).xlsx",
    "01-HJR-Global\\projects\\bhrf-expansion\\plan (1).pdf",
]
# Must stay LISTABLE: the vocabulary inside ordinary words, and the HJRG
# 'Non-Lexington' binder folder itself.
NOT_LEX = [
    "01-HJR-Global\\accounting\\visibility-binder\\02 Non-Lexington\\F3 Energy LLC\\inv (1).pdf",
    "09-One-Stop-Nutrition\\Maricopa county permit (1).pdf",
    "02-F3-Energy\\Alex flex complex results (1).pdf",
    "06-HJR-Properties\\Villa lease (1).pdf",
    "02-F3-Energy\\data exports (1).csv",
]


def test_lex_sub_entity_program_and_lead_names_outside_the_partition_are_counts_only():
    md, now, diffs = _render([frow(p, suffix="paren-n") for p in LEX_NAMED_OUTSIDE_08 + NOT_LEX], [])
    for p in LEX_NAMED_OUTSIDE_08:
        assert hdr.is_lex_relpath(p), p
        assert p.rsplit("\\", 1)[-1] not in md, p
    for p in NOT_LEX:
        assert not hdr.is_lex_relpath(p), p
        assert f"`{p}`" in md, p
    assert f"{len(LEX_NAMED_OUTSIDE_08)} LEX (counts only)" in md
    side, withheld = hdr.render_full_list_csv(now, diffs)
    assert withheld["lex"] == len(LEX_NAMED_OUTSIDE_08)
    for p in LEX_NAMED_OUTSIDE_08:
        assert p.rsplit("\\", 1)[-1].encode("utf-8") not in side, p
    # manifest decisions keep the partition-only predicate
    assert not any(hdr.is_lex_partition(p) for p in LEX_NAMED_OUTSIDE_08)


def test_static_walk_phi_folder_segments_are_counts_only_and_imported_not_copied():
    import incremental_sync_static  # noqa: PLC0415 -- the ingest door this report feeds

    assert hdr._static_phi_segments() == frozenset(s.lower() for s in incremental_sync_static.PHI_BLACKLIST_SEGMENTS)
    seg_paths = ["09-One-Stop-Nutrition\\clients\\roster export (1).pdf",
                 "_shared\\meetings\\clients\\weekly roster (1).gdoc",
                 "01-HJR-Global\\accounting\\Clients\\billing list (1).xlsx",
                 "02-F3-Energy\\Consumers\\survey (1).pdf",
                 "_shared\\ehr\\x (1).pdf"]
    fine = "02-F3-Energy\\client-decks\\deck (1).pdf"     # a segment CONTAINING 'client' is not the segment
    md, now, diffs = _render([frow(p, suffix="paren-n") for p in seg_paths + [fine]], [])
    for p in seg_paths:
        assert hdr.is_phi_segment_path(p) and not hdr.is_lex_relpath(p), p
        assert p.rsplit("\\", 1)[-1] not in md, p
    assert f"`{fine}`" in md
    assert f"{len(seg_paths)} withheld by the PHI screen" in md
    side, withheld = hdr.render_full_list_csv(now, diffs)
    assert withheld["phi"] == len(seg_paths) and b"roster export" not in side and b"survey" not in side


def test_the_lex_and_phi_segment_screens_fail_closed(monkeypatch):
    plain = "02-F3-Energy\\plain zzfailclosed (1).pdf"
    assert not hdr.is_lex_relpath(plain) and not hdr.is_phi_segment_path(plain)

    def boom(_transcript):
        raise RuntimeError("detector down")
    try:
        hdr._segment_names_lex.cache_clear()
        monkeypatch.setattr(hdr, "_lex_detector", lambda: boom)
        assert hdr.is_lex_relpath(plain), "a detector error counts the path as LEX"
        hdr._segment_names_lex.cache_clear()
        monkeypatch.setattr(hdr, "_lex_detector", lambda: None)
        assert hdr.is_lex_relpath(plain), "an unavailable detector counts the path as LEX"
        monkeypatch.setattr(hdr, "_static_phi_segments", lambda: None)
        assert hdr.is_phi_segment_path(plain), "an unavailable PHI segment list withholds"
    finally:
        monkeypatch.undo()
        hdr._segment_names_lex.cache_clear()   # never leave fail-closed verdicts cached
    assert not hdr.is_lex_relpath(plain) and not hdr.is_phi_segment_path(plain)


# An INDEPENDENT oracle (not the module's own regexes): 'lexington' anywhere, the
# sub-entity / program codes at any non-letter boundary, and COPA.
_ORACLE_LEX_RE = re.compile(
    r"lexington|(?<![a-z])(?:lex[\s_-]*ll[ac]|lex|lbhs|lla|lts|ddd|hcbs|bhrf|copa)(?![a-z])", re.IGNORECASE)


def _lex_vocabulary_hit(text: str) -> bool:
    """Every LEX vocabulary Cora uses, applied to a LISTED line: an independent
    oracle regex, cross_entity_guard's LEX keyword list and the fireflies detector
    per segment -- with the HJRG 'Non-Lexington' binder folder masked (the one
    sanctioned exception, a non-LEX entity folder)."""
    from cora import cross_entity_guard  # noqa: PLC0415
    from cora.connectors.fireflies_connector import classify_lex_meeting  # noqa: PLC0415

    masked = text.replace("Non-Lexington", "Non-Lxngtn")
    if _ORACLE_LEX_RE.search(masked):
        return True
    if "LEX" in cross_entity_guard.detect_entities(masked.replace("\\", " ").replace("_", " ")):
        return True
    return any(classify_lex_meeting({"title": s}).is_lex for s in hdr._segs(masked))


def test_a_rendered_report_lists_zero_lex_vocabulary_names():
    """The contract end to end: a synthetic tree with LEX-named offenders in every
    listed category (NEW and standing, both sides of the cap sort) renders a
    report and a sidecar whose LISTED lines carry no LEX vocabulary at all."""
    lex_files = [frow(p, suffix="paren-n") for p in LEX_NAMED_OUTSIDE_08] + [
        frow("01-HJR-Global\\Copy of LBHS census.xlsx", suffix="copy-of"),
        frow("_shared\\meetings\\Lex Services_2.docx", suffix="underscore-n"),
        frow("02-F3-Energy\\DDD (conflicted copy).pdf", suffix="conflicted"),
        frow("LBHS stray note.txt"),                                  # loose at the root
        frow("00-Founder\\lex-lla notes.md"),                         # loose at 00-Founder
    ]
    lex_dirs = [drow("LTS scratch"),                                  # non-canonical top
                drow("01-HJR-Global\\LBHS 2", suffix="space-2"),
                drow("_shared\\projects\\" + "lbhs-" + "x" * 35),     # a 40-char slug
                drow("01-HJR-Global\\hcbs-empty", empty=True)]
    fine_files = [frow(p, suffix="paren-n") for p in NOT_LEX] + [frow("02-F3-Energy\\Copy of deck.pptx", suffix="copy-of")]
    fine_dirs = [drow("Slack Deep Dive"), drow("02-F3-Energy\\brand 2", suffix="space-2"),
                 drow("04-UFL\\empty", empty=True)]
    prior = (lex_files[:3] + fine_files[:2], [])
    md, now, diffs = _render(lex_files + fine_files, lex_dirs + fine_dirs, prior=prior)
    listed = [ln for ln in md.splitlines() if ln.startswith("- `") or ln.startswith("- NEW `")]
    assert len(listed) >= len(fine_files) + len(fine_dirs), "the fine names are still listed"
    assert [ln for ln in listed if _lex_vocabulary_hit(ln)] == []
    side, _ = hdr.render_full_list_csv(now, diffs)
    rows = list(csv.DictReader(io.StringIO(side.decode("utf-8-sig"))))
    assert rows and [r["relpath"] for r in rows if _lex_vocabulary_hit(r["relpath"])] == []


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
    # D-051 R1: the original fixture ('...\\clients\\patient John Smith ...') now
    # trips two EARLIER screens -- 'patient' is a clinical title the ONE LEX
    # detector calls LEX, and 'clients' is a static-walk PHI segment -- so the
    # name screen is isolated on a name only it catches, and the original path
    # is pinned as withheld (never listed) by whichever screen catches it first.
    phi = "09-One-Stop-Nutrition\\members\\John Smith diagnosis (1).pdf"
    assert not hdr.is_lex_relpath(phi) and not hdr.is_phi_segment_path(phi)
    md, now, diffs = _render([frow(phi, suffix="paren-n")], [])
    assert "John Smith" not in md and "1 withheld by the PHI screen" in md
    side, withheld = hdr.render_full_list_csv(now, diffs)
    assert b"John Smith" not in side and withheld["phi"] == 1
    original = "09-One-Stop-Nutrition\\clients\\patient John Smith diagnosis (1).pdf"
    md, now, diffs = _render([frow(original, suffix="paren-n")], [])
    assert "John Smith" not in md
    side, withheld = hdr.render_full_list_csv(now, diffs)
    assert b"John Smith" not in side and withheld["lex"] + withheld["phi"] == 1


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


def test_the_floor_is_anchored_to_the_high_water_mark_and_cannot_ratchet(world, monkeypatch):
    """D-051 R1 rb2-r8#1: 100 -> 81 (passes, 81 >= 80) -> 65 read CLEAN against the
    81 prior, with 16 never-walked offenders reported 'resolved'."""
    full = [frow(f"02-F3-Energy\\r\\f{i:03d} (1).pdf", suffix="paren-n") for i in range(100)]
    seed_prior(world, "20260919-0240", files=full)
    fake_ps(monkeypatch, world, stamp="20260926-0240", files=full[:81])
    assert rh.main(["--apply"]) == rh.EXIT_OK
    fake_ps(monkeypatch, world, stamp="20261003-0240", files=full[:65])
    assert rh.main(["--apply"]) == rh.EXIT_INCOMPLETE
    md = world.report("2026-10-03").read_text(encoding="utf-8")
    assert "INCOMPLETE WALK" in md and "resolved since last run" not in md
    assert "files walked 65 < 80% of the high-water full run's 100 (stamp 20260919-0240)" in md
    row = [r for r in world.ledger_rows() if r.get("stamp") == "20261003-0240" and r["event"] == "created"]
    assert row and row[0]["status"] == "incomplete"
    # the week-over-week comparison run is still the newest eligible run
    fake_ps(monkeypatch, world, stamp="20261010-0240", files=full)
    assert rh.main(["--apply"]) == rh.EXIT_OK
    assert "stamp `20260926-0240`" in world.report("2026-10-10").read_text(encoding="utf-8")


def test_the_baseline_is_part_of_the_high_water_mark(world, monkeypatch):
    full = [frow(f"02-F3-Energy\\r\\f{i:03d}.pdf") for i in range(100)]
    write_stamp(world.outdir, rh.BASELINE_STAMP, full, BASE_DIRS, root=world.root, max_hash=200)
    seed_prior(world, "20260926-0240", files=full[:82])
    fake_ps(monkeypatch, world, stamp="20261003-0240", files=full[:70])   # 70 >= 80% of 82, < 80% of 100
    assert rh.main(["--apply"]) == rh.EXIT_INCOMPLETE
    md = world.report("2026-10-03").read_text(encoding="utf-8")
    assert f"files walked 70 < 80% of the high-water full run's 100 (stamp {rh.BASELINE_STAMP})" in md
    runs = rh.eligible_runs(world.outdir, "20261010-0240", world.root,
                            rh.ledger_state(rh.read_ledger(world.ledger)))
    assert [r.stamp for r in runs] == ["20260926-0240", rh.BASELINE_STAMP], "incomplete runs are never eligible"
    assert rh.high_water(runs)["files_total"] == (100, rh.BASELINE_STAMP)


def test_the_ps1_output_write_error_shapes_are_walk_errors():
    """D-051 R1 rb2-r8#2: the exact-head match never counted the PS1's three
    'OUTPUT-WRITE-ERROR (<what>): ...' shapes (folder-audit-inventory.ps1)."""
    c = hdr.count_run_log("\ufeffOUTPUT-WRITE-ERROR (files CSV): denied\r\n"
                          "OUTPUT-WRITE-ERROR (dirs CSV): denied\r\n"
                          "OUTPUT-WRITE-ERROR (summary): denied\r\n"
                          "FATAL-ERROR: boom\r\nFATAL-ERROR-STACK: at line 1\r\n"
                          "WALK-FILE-ERROR: G:\\x :: y\r\nHASH-ERROR: z :: w\r\n")
    assert c["OUTPUT-WRITE-ERROR"] == 3 and c["FATAL-ERROR"] == 1, "the stack line is not a second fatal"
    assert c["WALK-FILE-ERROR"] == 1 and c["HASH-ERROR"] == 1
    assert hdr.walk_error_count(c) == 5


@pytest.mark.parametrize("which", ["files", "dirs"])
@pytest.mark.parametrize("mode", ["missing", "undecodable"])
def test_a_missing_or_unreadable_csv_is_an_incomplete_walk_with_a_ledger_row(world, monkeypatch, which, mode):
    """D-051 R1 rb2-r8#2: Export-Csv failing at open leaves no CSV while the PS1
    still exits 0 -- the runner raised FileNotFoundError: no report, no ledger
    row, so the stamp's other files were never rotated."""
    seed_prior(world, "20260919-0240")
    stamp = "20260926-0240"
    name = f"inventory-{which}-{stamp}.csv"
    label = "files" if which == "files" else "folders"
    runlog = [f"OUTPUT-WRITE-ERROR ({which} CSV): The process cannot access the file"] if mode == "missing" else None
    real = fake_ps(monkeypatch, world, stamp=stamp, runlog=runlog)

    def fake(cmd, **kw):
        cp = real(cmd, **kw)
        target = Path(cmd[cmd.index("-OutDir") + 1]) / name
        if mode == "missing":
            target.unlink()
        else:
            target.write_bytes(b"\xef\xbb\xbf\"RelPath\"\r\n\"\xff\xfe\xfd\"\r\n")
        return cp
    monkeypatch.setattr(rh.subprocess, "run", fake)
    assert rh.main(["--apply"]) == rh.EXIT_INCOMPLETE
    md = world.report().read_text(encoding="utf-8")
    assert "INCOMPLETE WALK" in md and f"the {label} CSV is missing or unreadable" in md
    if mode == "missing":
        assert "1 walk errors logged" in md
    row = [r for r in world.ledger_rows() if r.get("stamp") == stamp and r["event"] == "created"]
    assert row and row[0]["status"] == "incomplete" and row[0]["full_root"] is True
    assert (name in row[0]["files"]) == (mode != "missing"), "the ledger lists exactly the files that exist"
    assert not list(world.outdir.glob("manifest-desktopini-*"))


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


# ═════════════════════════════════════════════════════════════════════════════
# 4. D-051 R2 (rb2#r2-0, rb2#r2-1, rb2#r2-2, lens#r2-1)
# ═════════════════════════════════════════════════════════════════════════════

# rb2#r2-1: LEX-named forms that passed all three screens (fabricated names; the
# leads are the detector's own lead identifiers, already in fireflies_connector).
LEX_NAMED_R2 = [
    "01-HJR-Global\\legal\\COPA_LOI_v3 (1).pdf",                      # '_' is a \b word char
    "_shared\\meetings\\COPA2 diligence (1).gdoc",                    # so is a digit
    "_shared\\meetings\\Jared Harker + Sandy Patel sync (1).gdoc",     # two leads, two sub-entities
    "_shared\\meetings\\Justin Gilmore and Jared Harker (1).gdoc",
    "_shared\\meetings\\Justin_Gilmore 1-1 (1).gdoc",                 # separator-joined
    "_shared\\meetings\\Jared-Harker check-in (1).gdoc",
    "_shared\\meetings\\Shaun_Hawkins budget (1).gdoc",
    "_shared\\meetings\\jeff.montgomery notes (1).gdoc",
    "_shared\\meetings\\Gilmore, Justin review (1).gdoc",             # last-first
    "_shared\\meetings\\Montgomery, Jeff review (1).gdoc",            # a title-keyword lead, last-first
    "01-HJR-Global\\accounting\\LexServices P&L (1).xlsx",           # glued
    "01-HJR-Global\\accounting\\LexLTS payroll (1).xlsx",
    "01-HJR-Global\\accounting\\LTSpayroll (1).xlsx",
    "_shared\\meetings\\BHRFs licensing (1).gdoc",                    # plural
    "01-HJR-Global\\accounting\\LLAs roster (1).xlsx",
    "01-HJR-Global\\accounting\\provider revalidation (1).pdf",      # cross_entity_guard's LEX row
    "01-HJR-Global\\accounting\\Revalidation_packet (1).pdf",
]
# Must stay LISTABLE under the widened belt.
NOT_LEX_R2 = [
    "02-F3-Energy\\copay schedule (1).pdf",
    "02-F3-Energy\\Lexus lease (1).pdf",
    "02-F3-Energy\\llama photos (1).jpg",
    "04-UFL\\Copacabana event (1).pdf",
]


def test_r2_lex_named_forms_every_screen_missed_are_counts_only():
    md, now, diffs = _render([frow(p, suffix="paren-n") for p in LEX_NAMED_R2 + NOT_LEX_R2 + NOT_LEX], [])
    missed = [i for i, p in enumerate(LEX_NAMED_R2) if not hdr.is_lex_relpath(p)]
    assert missed == [], "LEX_NAMED_R2 indexes still listable"
    for p in LEX_NAMED_R2:
        assert p.rsplit("\\", 1)[-1] not in md, p
    for p in NOT_LEX_R2 + NOT_LEX:
        assert not hdr.is_lex_relpath(p), p
        assert f"`{p}`" in md, p
    assert f"{len(LEX_NAMED_R2)} LEX (counts only)" in md
    side, withheld = hdr.render_full_list_csv(now, diffs)
    assert withheld["lex"] == len(LEX_NAMED_R2)
    for p in LEX_NAMED_R2:
        assert p.rsplit("\\", 1)[-1].encode("utf-8") not in side, p


def _fresh_lex_screens() -> None:
    """Drop the once-per-process screen caches (a test that patches a source)."""
    for name in ("_LEX_LEADS", "_LEX_ENTITIES"):
        getattr(hdr, name, []).clear()
    hdr._segment_names_lex.cache_clear()


def test_r2_named_lex_people_come_from_the_org_roles_roster(monkeypatch):
    """(d): the roster's LEX staff, not only the detector's four leads -- and only
    people whose PRIMARY entity is LEX (a founder or finance lead with LEX among
    several entities is not a LEX person). Fabricated registry records."""
    from cora import org_roles

    lex_person = org_roles.RoleRecord(slack_id="U0FAKE1", name="Zorin Quell", role="Program lead", entity="LEX-LLA")
    multi = org_roles.RoleRecord(slack_id="U0FAKE2", name="Pim Vandor", role="Finance", entity="HJRG",
                                 entities=["LEX", "OSN"])
    shapes = ["_shared\\meetings\\{f} {l} 1-1 (1).gdoc", "_shared\\meetings\\{f}_{l}_notes (1).gdoc",
              "_shared\\meetings\\{f}-{l} sync (1).gdoc", "_shared\\meetings\\{l}, {f} review (1).gdoc",
              "01-HJR-Global\\accounting\\{f}.{l} reimbursements (1).xlsx",
              "01-HJR-Global\\hr\\{f} {l}\\offer letter (1).pdf"]
    try:
        monkeypatch.setattr(org_roles, "all_roles", lambda: [lex_person, multi])
        _fresh_lex_screens()
        for sh in shapes:
            assert hdr.is_lex_relpath(sh.format(f="Zorin", l="Quell")), sh
            assert not hdr.is_lex_relpath(sh.format(f="Pim", l="Vandor")), sh
    finally:
        monkeypatch.undo()
        _fresh_lex_screens()


def test_r2_the_live_roster_and_detector_leads_are_counts_only_in_every_shape():
    """Every LEX-primary person in the repo's org-roles.yaml plus every lead the
    detector names, in six filename shapes. Failures report indexes only."""
    from cora import org_roles
    from cora.connectors import fireflies_connector as ff

    names = {r.name for r in org_roles.all_roles() if str(r.entity).upper().startswith("LEX")}
    names |= {ids[-1] for ids, _ in ff._FIREFLIES_PARTICIPANT_SUB_ENTITY} | {ff._SHAUN_IDENTIFIERS[-1]}
    shapes = ["_shared\\meetings\\{f} {l} 1-1 (1).gdoc", "_shared\\meetings\\{f}_{l}_notes (1).gdoc",
              "_shared\\meetings\\{f}-{l} sync (1).gdoc", "_shared\\meetings\\{l}, {f} review (1).gdoc",
              "01-HJR-Global\\accounting\\{f}.{l} reimbursements (1).xlsx",
              "01-HJR-Global\\hr\\{f} {l}\\offer letter (1).pdf"]
    missed = []
    for i, name in enumerate(sorted(names)):
        parts = name.split()
        for j, sh in enumerate(shapes):
            if not hdr.is_lex_relpath(sh.format(f=parts[0], l=parts[-1])):
                missed.append((i, j))
    assert len(names) >= 4 and missed == []


def test_r2_cross_entity_guard_lex_keywords_are_imported_not_copied(monkeypatch):
    from cora import cross_entity_guard as ceg

    lex = ceg._ENTITY_DEFS["LEX"]
    for pat in lex.patterns:
        kw = pat.pattern.replace("\\b", "").replace("\\", "")
        seg = kw.replace(" ", "_")                       # joined the way a filename joins it
        assert hdr.is_lex_relpath(f"01-HJR-Global\\notes\\{seg} (1).pdf"), kw
    probe = "01-HJR-Global\\notes\\zqxwidget ops (1).pdf"
    try:
        _fresh_lex_screens()
        assert not hdr.is_lex_relpath(probe)
        grown = ceg._EntityDef(lex.name, lex.channel_hint, lex.patterns + ceg._compile("zqxwidget"))
        monkeypatch.setitem(ceg._ENTITY_DEFS, "LEX", grown)
        _fresh_lex_screens()
        assert hdr.is_lex_relpath(probe), "a keyword added to the guard's LEX row reaches the report gate"
    finally:
        monkeypatch.undo()
        _fresh_lex_screens()


def test_r2_the_new_lex_screens_fail_closed(monkeypatch):
    plain = "02-F3-Energy\\plain zzfailclosed (1).pdf"
    try:
        _fresh_lex_screens()
        assert not hdr.is_lex_relpath(plain)

        def boom():
            raise RuntimeError("roster unreadable")
        monkeypatch.setattr(hdr, "_lex_person_names", boom)
        _fresh_lex_screens()
        assert hdr._lex_lead_re() is None and hdr.is_lex_relpath(plain), "a failed name load withholds"
        monkeypatch.undo()
        monkeypatch.setattr(hdr, "_lex_person_names", lambda: set())
        _fresh_lex_screens()
        assert hdr.is_lex_relpath(plain), "an EMPTY name list withholds too"
        monkeypatch.undo()
        monkeypatch.setattr(hdr, "_lex_entities", lambda: None)
        _fresh_lex_screens()
        assert hdr.is_lex_relpath(plain), "an unavailable cross-entity keyword list withholds"
    finally:
        monkeypatch.undo()
        _fresh_lex_screens()
    assert not hdr.is_lex_relpath(plain)


# ═════════════════════════════════════════════════════════════════════════════
# 5. D-051 R3 rb2#r3-2: glued / initial lead names and CamelCase codes
# ═════════════════════════════════════════════════════════════════════════════

# The runbook and the gate's docstring said a LEX person is counts-only "however
# joined"; _LEAD_SEP needed a separator and the belt a non-letter right bound.
_R3_LEAD_SHAPES = [
    "_shared\\meetings\\{F}{L}_2026-10-01 (1).mp4",                  # a Zoom export: glued
    "_shared\\meetings\\{f}{l} notes (1).gdoc",                      # glued, lower
    "_shared\\meetings\\{f}{L} sync (1).gdoc",                       # camelCase
    "_shared\\meetings\\{L}{F} review (1).gdoc",                     # last-first glued
    "_shared\\meetings\\{F0}{L} 1-1 (1).gdoc",                       # initial glued
    "_shared\\meetings\\{F0}.{L} check-in (1).gdoc",
    "_shared\\meetings\\{F0}_{L} budget (1).gdoc",
    "_shared\\meetings\\{F0}. {L} review (1).gdoc",
    "01-HJR-Global\\hr\\{F}{L}\\offer letter (1).pdf",
]
LEX_NAMED_R3 = [
    "01-HJR-Global\\accounting\\LexOps budget (1).xlsx",
    "01-HJR-Global\\accounting\\LexPayroll (1).xlsx",
    "01-HJR-Global\\hr\\LexHR handbook (1).pdf",
    "01-HJR-Global\\accounting\\LexBudget_FY27 (1).xlsx",
    "01-HJR-Global\\accounting\\LexFinance (1).xlsx",
    "01-HJR-Global\\accounting\\LEXreport (1).pdf",
    "01-HJR-Global\\accounting\\LEXReport (1).pdf",
    "01-HJR-Global\\accounting\\LLAreport (1).pdf",
    "01-HJR-Global\\legal\\COPAdiligence (1).pdf",
    "01-HJR-Global\\legal\\DDDcontract (1).pdf",
    "_shared\\meetings\\TheLexington board (1).gdoc",
    "01-HJR-Global\\accounting\\HJRLexington (1).xlsx",
]
NOT_LEX_R3 = [
    "02-F3-Energy\\Lexicon flywheel (1).pdf",
    "02-F3-Energy\\LexisNexis export (1).pdf",
    "02-F3-Energy\\LEXUS brochure (1).pdf",
    "02-F3-Energy\\AlexCordova notes (1).gdoc",
    "02-F3-Energy\\FlexSeal order (1).pdf",
    "01-HJR-Global\\02 NonLexington\\binder (1).pdf",
    "02-F3-Energy\\DeliveryOps (1).xlsx",
    "02-F3-Energy\\QBOExport (1).csv",
    "02-F3-Energy\\LLAMA photos (1).jpg",
]


def _shape(sh: str, first: str, last: str) -> str:
    return sh.format(F=first.capitalize(), L=last.capitalize(), f=first.lower(), l=last.lower(),
                     F0=first[0].upper())


def test_r3_a_lex_person_is_counts_only_glued_or_by_initial(monkeypatch):
    """Fabricated registry records: a LEX-primary person in every glued / initial
    shape is withheld; a non-LEX person in the same shapes stays listed."""
    from cora import org_roles

    lex_person = org_roles.RoleRecord(slack_id="U0FAKE1", name="Zorin Quell", role="Program lead", entity="LEX-LLA")
    other = org_roles.RoleRecord(slack_id="U0FAKE2", name="Pim Vandor", role="Finance", entity="HJRG")
    try:
        monkeypatch.setattr(org_roles, "all_roles", lambda: [lex_person, other])
        _fresh_lex_screens()
        for i, sh in enumerate(_R3_LEAD_SHAPES):
            assert hdr.is_lex_relpath(_shape(sh, "Zorin", "Quell")), i
            assert not hdr.is_lex_relpath(_shape(sh, "Pim", "Vandor")), i
        assert not hdr.is_lex_relpath("_shared\\meetings\\Quell review (1).gdoc"), \
            "the last name alone is a documented residual, not claimed"
    finally:
        monkeypatch.undo()
        _fresh_lex_screens()


def test_r3_the_live_leads_are_counts_only_glued_or_by_initial():
    """Every LEX-primary person in the repo's org-roles.yaml plus every detector
    lead, in the glued / initial shapes. Failures report indexes only."""
    from cora import org_roles
    from cora.connectors import fireflies_connector as ff

    names = {r.name for r in org_roles.all_roles() if str(r.entity).upper().startswith("LEX")}
    names |= {ids[-1] for ids, _ in ff._FIREFLIES_PARTICIPANT_SUB_ENTITY} | {ff._SHAUN_IDENTIFIERS[-1]}
    missed = []
    for i, name in enumerate(sorted(names)):
        parts = name.split()
        for j, sh in enumerate(_R3_LEAD_SHAPES):
            if not hdr.is_lex_relpath(_shape(sh, parts[0], parts[-1])):
                missed.append((i, j))
    assert len(names) >= 4 and missed == []


def test_r3_camelcase_and_glued_codes_are_counts_only_and_ordinary_words_stay_listed():
    md, now, diffs = _render([frow(p, suffix="paren-n") for p in LEX_NAMED_R3 + NOT_LEX_R3], [])
    missed = [i for i, p in enumerate(LEX_NAMED_R3) if not hdr.is_lex_relpath(p)]
    assert missed == [], "LEX_NAMED_R3 indexes still listable"
    for p in LEX_NAMED_R3:
        assert p.rsplit("\\", 1)[-1] not in md, p
    for p in NOT_LEX_R3 + NOT_LEX_R2 + NOT_LEX:
        assert not hdr.is_lex_relpath(p), p
    for p in NOT_LEX_R3:
        assert f"`{p}`" in md, p
    assert f"{len(LEX_NAMED_R3)} LEX (counts only)" in md
    assert hdr._split_camel("LexOps") == "Lex Ops" and hdr._split_camel("LEXreport") == "LEX report"
    assert hdr._split_camel("Lexicon") == "Lexicon" and hdr._split_camel("LEXUS") == "LEXUS"


def _weekly(n: int, start: int = 0) -> list[str]:
    from datetime import date, timedelta
    return [(date(2026, 9, 26) + timedelta(days=7 * (start + i))).strftime("%Y%m%d") + "-0240" for i in range(n)]


def _day(stamp: str) -> str:
    return rh._report_date(stamp)


def test_r2_rebaseline_accepts_a_legitimate_shrink_and_retires_the_baseline(world, monkeypatch):
    """rb2#r2-0 + lens#r2-1: a partition moves out of the tree (78 < 80% of the
    never-rotated 9/21 baseline). Every later week failed the floor, the failure
    was never eligible, so nothing could ever move the anchor -- and the report
    told the operator to wait for the G: mount."""
    full = [frow(f"02-F3-Energy\\r\\f{i:03d}.pdf") for i in range(100)]
    write_stamp(world.outdir, rh.BASELINE_STAMP, full, BASE_DIRS, root=world.root, max_hash=200)
    s = _weekly(4)
    fake_ps(monkeypatch, world, stamp=s[0], files=full)
    assert rh.main(["--apply"]) == rh.EXIT_OK
    for st in s[1:3]:
        fake_ps(monkeypatch, world, stamp=st, files=full[:78])
        assert rh.main(["--apply"]) == rh.EXIT_INCOMPLETE
    md = world.report(_day(s[2])).read_text(encoding="utf-8")
    assert "INCOMPLETE WALK" in md and "files walked 78 < 80%" in md
    assert f"--rebaseline {s[2]} --apply" in md and f"--from-stamp {s[2]} --apply" in md
    # the dry run (default) validates and writes nothing
    ledger_before = world.ledger.read_bytes()
    assert rh.main(["--rebaseline", s[2]]) == rh.EXIT_OK
    assert world.ledger.read_bytes() == ledger_before
    assert rh.main(["--rebaseline", s[2], "--apply"]) == rh.EXIT_OK
    reb = [r for r in world.ledger_rows() if r["event"] == "rebaselined"]
    assert len(reb) == 1 and reb[0]["stamp"] == s[2] and (reb[0]["files_total"], reb[0]["dirs_total"]) == (78, 3)
    # the rebuilt report for the re-baselined week has nothing to compare with
    assert rh.main(["--from-stamp", s[2], "--apply"]) == rh.EXIT_OK
    md = world.report(_day(s[2])).read_text(encoding="utf-8")
    assert "UNVERIFIED" in md and "INCOMPLETE" not in md
    # next week compares with the re-baselined walk and reads CLEAN
    fake_ps(monkeypatch, world, stamp=s[3], files=full[:78])
    assert rh.main(["--apply"]) == rh.EXIT_OK
    md = world.report(_day(s[3])).read_text(encoding="utf-8")
    assert "CLEAN" in md and f"stamp `{s[2]}`" in md
    # the baseline stays on disk (the RIDER B proposer reads it) but anchors nothing
    assert all((world.outdir / n).exists() for n in rh.stamp_files(rh.BASELINE_STAMP))
    runs = rh.eligible_runs(world.outdir, "20991231-2359", world.root, rh.ledger_state(rh.read_ledger(world.ledger)))
    assert [r.stamp for r in runs] == [s[3], s[2]]


def test_r2_rebaseline_refuses_what_it_cannot_vouch_for(world, monkeypatch):
    write_stamp(world.outdir, rh.BASELINE_STAMP, BASE_FILES, BASE_DIRS, root=world.root, max_hash=200)
    seed_prior(world, "20260926-0240")                                              # a sound runner walk
    write_stamp(world.outdir, "20260927-1111", BASE_FILES, BASE_DIRS, root=world.root)  # hand run, not ledgered
    seed_prior(world, "20261003-0240", status="incomplete", runlog=["WALK-FILE-ERROR: x :: y"])
    seed_prior(world, "20261010-0240")
    rh.append_ledger(world.ledger, {"event": "rotated", "stamp": "20261010-0240", "removed": 0})
    seed_prior(world, "20261017-0240", status="incomplete", files_total=len(BASE_FILES) + 2)  # row count mismatch
    before = world.ledger.read_bytes()
    for st in (rh.BASELINE_STAMP, "20260927-1111", "20261003-0240", "20261010-0240", "20261017-0240",
               "20991231-2359"):
        assert rh.main(["--rebaseline", st, "--apply"]) == rh.EXIT_REFUSED, st
    assert rh.main(["--rebaseline", "20260926-0240", "--from-stamp", "20260926-0240", "--apply"]) == rh.EXIT_REFUSED
    assert rh.main(["--rebaseline", "not-a-stamp", "--apply"]) == rh.EXIT_REFUSED
    assert world.ledger.read_bytes() == before, "a refused re-baseline writes nothing"
    seed_prior(world, "20261024-0240")
    assert rh.main(["--rebaseline", "20261024-0240", "--apply"]) == rh.EXIT_OK
    assert rh.main(["--rebaseline", "20260926-0240", "--apply"]) == rh.EXIT_REFUSED, "older than a recorded re-baseline"


def test_r2_the_high_water_is_bounded_to_the_newest_four_eligible_runs(world, monkeypatch):
    """lens#r2-1: the baseline is part of the HIGH-WATER only while it is among the
    newest KEEP_RUNNER_STAMPS eligible runs -- the window rotation keeps.

    D-051 R3 rb2#r3-0: this test used to pin 100 -> 85x4 -> 70 as CLEAN (the window
    alone slid the floor 20% every four eligible weeks: 30% below the baseline with
    no operator action). The window is unchanged; the CUMULATIVE anchor (the
    baseline, never re-baselined) now refuses that 70, and prints the re-baseline."""
    full = [frow(f"02-F3-Energy\\r\\f{i:03d}.pdf") for i in range(100)]
    write_stamp(world.outdir, rh.BASELINE_STAMP, full, BASE_DIRS, root=world.root, max_hash=200)
    s = _weekly(rh.KEEP_RUNNER_STAMPS + 1)
    for st in s[:-1]:
        fake_ps(monkeypatch, world, stamp=st, files=full[:85])
        assert rh.main(["--apply"]) == rh.EXIT_OK
    ledger = rh.ledger_state(rh.read_ledger(world.ledger))
    runs = rh.eligible_runs(world.outdir, s[-1], world.root, ledger)
    assert runs[-1].stamp == rh.BASELINE_STAMP and len(runs) == rh.KEEP_RUNNER_STAMPS + 1
    assert rh.high_water(runs)["files_total"][0] == 85
    assert rh.high_water(runs[-3:])["files_total"] == (100, rh.BASELINE_STAMP), "inside the window it still anchors"
    fake_ps(monkeypatch, world, stamp=s[-1], files=full[:70])     # >= 80% of 85, < 80% of the baseline's 100
    assert rh.main(["--apply"]) == rh.EXIT_INCOMPLETE
    md = world.report(_day(s[-1])).read_text(encoding="utf-8")
    assert "INCOMPLETE WALK" in md and "CLEAN" not in md
    assert (f"files walked 70 < 80% of the 2026-09-21 baseline's 100 (stamp {rh.BASELINE_STAMP}): "
            "a cumulative shrink over 20% is accepted only by a re-baseline") in md
    assert "high-water" not in md, "the window (85) passed it: one reason, the anchor's"
    assert f"--rebaseline {s[-1]} --apply" in md
    assert rh.floor_anchor(runs, ledger, s[-1])["files_total"] == (100, rh.BASELINE_STAMP)


def test_r3_a_slow_ratchet_is_caught_until_the_operator_rebaselines(world, monkeypatch):
    """rb2#r3-0: 100 -> 81x4 (each CLEAN) -> 65 read CLEAN once the baseline left the
    four-run window, and so on down to 43 in 13 CLEAN weeks. The anchor refuses the
    65; a re-baseline accepts it, and from then on the RE-BASELINED counts anchor --
    from the stamp ledger, after rotation has deleted that stamp's own files."""
    full = [frow(f"02-F3-Energy\\r\\f{i:03d}.pdf") for i in range(100)]
    write_stamp(world.outdir, rh.BASELINE_STAMP, full, BASE_DIRS, root=world.root, max_hash=200)
    s = _weekly(12)
    for st in s[:4]:
        fake_ps(monkeypatch, world, stamp=st, files=full[:81])
        assert rh.main(["--apply"]) == rh.EXIT_OK
    for st in s[4:6]:                                             # every week, not just once
        fake_ps(monkeypatch, world, stamp=st, files=full[:65])
        assert rh.main(["--apply"]) == rh.EXIT_INCOMPLETE
        md = world.report(_day(st)).read_text(encoding="utf-8")
        assert "files walked 65 < 80% of the 2026-09-21 baseline's 100" in md
        assert "resolved since last run" not in md
    assert rh.main(["--rebaseline", s[5], "--apply"]) == rh.EXIT_OK
    for st in s[6:11]:                                            # 55 >= 80% of the re-baselined 65
        fake_ps(monkeypatch, world, stamp=st, files=full[:55])
        assert rh.main(["--apply"]) == rh.EXIT_OK
    led = rh.ledger_state(rh.read_ledger(world.ledger))
    assert led[s[5]].get("rotated") and not (world.outdir / rh.stamp_files(s[5])[2]).exists()
    fake_ps(monkeypatch, world, stamp=s[11], files=full[:50])     # >= 80% of the window's 55, < 52
    assert rh.main(["--apply"]) == rh.EXIT_INCOMPLETE
    md = world.report(_day(s[11])).read_text(encoding="utf-8")
    assert f"files walked 50 < 80% of the newest re-baselined run's 65 (stamp {s[5]})" in md


def test_r3_a_from_stamp_rebuild_before_a_rebaseline_keeps_its_own_floor(world, monkeypatch):
    """rb2#r3-1: eligible_runs dropped every stamp older than the NEWEST re-baseline
    whatever week it served, so rebuilding a week older than it (the INCOMPLETE
    report's own --from-stamp line) found no prior, ran with no floor, and
    overwrote that week's INCOMPLETE report with an UNVERIFIED one carrying the
    partial walk's full lists -- 'no prior full-root run to compare'."""
    full = [frow(f"02-F3-Energy\\r\\f{i:03d} (1).pdf", suffix="paren-n") for i in range(100)]
    write_stamp(world.outdir, rh.BASELINE_STAMP, full, BASE_DIRS, root=world.root, max_hash=200)
    w1, w2, w3 = _weekly(3)
    fake_ps(monkeypatch, world, stamp=w1, files=full[:60])       # a degraded-mount partial walk
    assert rh.main(["--apply"]) == rh.EXIT_INCOMPLETE
    for st in (w2, w3):                                            # then a real partition move
        fake_ps(monkeypatch, world, stamp=st, files=full[:78])
        assert rh.main(["--apply"]) == rh.EXIT_INCOMPLETE
    assert rh.main(["--rebaseline", w3, "--apply"]) == rh.EXIT_OK
    assert rh.main(["--rebaseline", w1, "--apply"]) == rh.EXIT_REFUSED   # older than W3's
    for st, n in ((w1, 60), (w2, 78)):
        assert rh.main(["--from-stamp", st, "--apply"]) == rh.EXIT_INCOMPLETE, st
        md = world.report(_day(st)).read_text(encoding="utf-8")
        assert "INCOMPLETE WALK" in md and "UNVERIFIED" not in md and "f000 (1).pdf" not in md
        assert f"files walked {n} < 80% of the prior full run's 100" in md
        assert f"Prior full run: stamp `{rh.BASELINE_STAMP}`" in md
        # D-051 r4: no dead-end hint -- rebaseline() refuses a week older than W3's
        assert "--rebaseline" not in md, st
    assert not list(world.outdir.glob("hygiene-findings-full-*")), "no sidecar for a pre-re-baseline week"
    led = rh.ledger_state(rh.read_ledger(world.ledger))
    assert [r.stamp for r in rh.eligible_runs(world.outdir, w1, world.root, led)] == [rh.BASELINE_STAMP]
    assert [r.stamp for r in rh.eligible_runs(world.outdir, w3, world.root, led)] == []
    assert rh.rebaseline_cutoff(led) == w3 and rh.rebaseline_cutoff(led, w1) is None
    # the re-baselined week itself still reads UNVERIFIED, as designed
    assert rh.main(["--from-stamp", w3, "--apply"]) == rh.EXIT_OK
    assert "UNVERIFIED" in world.report(_day(w3)).read_text(encoding="utf-8")


def test_r2_the_rebaseline_hint_appears_only_when_the_walk_itself_is_sound(world, monkeypatch):
    seed_prior(world, "20260919-0240")
    fake_ps(monkeypatch, world, runlog=["WALK-DIR-ERROR: G:\\x :: denied"])
    assert rh.main(["--apply"]) == rh.EXIT_INCOMPLETE
    md = world.report().read_text(encoding="utf-8")
    assert "--rebaseline" not in md and "once the G: mount is healthy" in md


def test_r2_a_failed_summary_write_is_an_incomplete_walk_with_a_rotatable_ledger_row(world, monkeypatch):
    """rb2#r2-2 case 1: the PS1 exits 0 when the summary's Set-Content fails, and
    stamp detection keys on the summary -- exit 5, no report, and the stamp's
    CSVs + run log (~27 MB live) were never ledgered, so never rotated."""
    seed_prior(world, "20260919-0240")
    st = "20260926-0240"
    real = fake_ps(monkeypatch, world, stamp=st, runlog=["OUTPUT-WRITE-ERROR (summary): denied"])

    def fake(cmd, **kw):
        cp = real(cmd, **kw)
        (Path(cmd[cmd.index("-OutDir") + 1]) / f"inventory-summary-{st}.txt").unlink()
        return cp
    monkeypatch.setattr(rh.subprocess, "run", fake)
    assert rh.main(["--apply"]) == rh.EXIT_INCOMPLETE
    md = world.report().read_text(encoding="utf-8")
    assert "INCOMPLETE WALK" in md and "the inventory summary was not written" in md
    assert "1 walk errors logged" in md and "--rebaseline" not in md and "stamp `20260919-0240`" in md
    row = [r for r in world.ledger_rows() if r.get("stamp") == st and r["event"] == "created"]
    assert row and row[0]["status"] == "incomplete"
    assert sorted(row[0]["files"]) == sorted([f"inventory-files-{st}.csv", f"inventory-dirs-{st}.csv",
                                              f"_run-log-{st}.txt"])
    assert not list(world.outdir.glob("manifest-desktopini-*"))
    for s2 in _weekly(5, start=1):
        fake_ps(monkeypatch, world, stamp=s2)
        assert rh.main(["--apply"]) == rh.EXIT_OK
    assert not list(world.outdir.glob(f"*-{st}.*")), "the keep-4 rotation reaches the summary-less stamp"


def test_r2_a_failed_summary_write_in_a_dry_run_writes_nothing(world, monkeypatch):
    st = "20260926-0240"
    real = fake_ps(monkeypatch, world, stamp=st, runlog=["OUTPUT-WRITE-ERROR (summary): denied"])

    def fake(cmd, **kw):
        cp = real(cmd, **kw)
        (Path(cmd[cmd.index("-OutDir") + 1]) / f"inventory-summary-{st}.txt").unlink()
        return cp
    monkeypatch.setattr(rh.subprocess, "run", fake)
    assert rh.main([]) == rh.EXIT_INCOMPLETE
    assert not world.ledger.exists() and not any(world.report_dir.iterdir()) and not any(world.outdir.iterdir())


def test_r2_summaryless_stamp_detection():
    files = set(rh.stamp_files("20260926-0240"))
    no_summary = files - {"inventory-summary-20260926-0240.txt"}
    assert rh.detect_summaryless_stamp(set(), no_summary) == "20260926-0240"
    assert rh.detect_summaryless_stamp(set(), files) is None, "a summary appeared: the normal path"
    two = no_summary | {"inventory-files-20260926-0241.csv"}
    assert rh.detect_summaryless_stamp(set(), two) is None
    pre = {"inventory-files-20260926-0240.csv"}
    assert rh.detect_summaryless_stamp(pre, pre | no_summary) is None, "pre-existing files may be someone else's run"
    assert rh.detect_summaryless_stamp(set(), {"manifest-desktopini-2026-09-26.csv"}) is None


@pytest.mark.parametrize("which", ["files", "dirs"])
def test_r2_an_unreadable_prior_csv_is_an_incomplete_walk_not_a_traceback(world, monkeypatch, which):
    """rb2#r2-2 case 2: eligible_runs checks only that the prior's CSVs exist, and
    build_outputs read them unguarded -- a UnicodeDecodeError after the current
    stamp was already ledgered 'clean': no report, no rotation."""
    seed_prior(world, "20260919-0240")
    (world.outdir / f"inventory-{which}-20260919-0240.csv").write_bytes(b"\xef\xbb\xbf\"RelPath\"\r\n\"\xff\xfe\"\r\n")
    fake_ps(monkeypatch, world, stamp="20260926-0240")
    assert rh.main(["--apply"]) == rh.EXIT_INCOMPLETE
    md = world.report().read_text(encoding="utf-8")
    assert "INCOMPLETE WALK" in md and "the prior run's (stamp 20260919-0240)" in md
    assert "--rebaseline 20260926-0240 --apply" in md, "the walk is sound: the recovery is a re-baseline"
    row = [r for r in world.ledger_rows() if r.get("stamp") == "20260926-0240" and r["event"] == "created"]
    assert row and row[0]["status"] == "incomplete"
    assert rh.main(["--rebaseline", "20260926-0240", "--apply"]) == rh.EXIT_OK
    fake_ps(monkeypatch, world, stamp="20261003-0240")
    assert rh.main(["--apply"]) == rh.EXIT_OK
    assert "stamp `20260926-0240`" in world.report("2026-10-03").read_text(encoding="utf-8")


def test_r4_the_cumulative_anchor_follows_growth(world, monkeypatch):
    """D-051 r4: the anchor stayed at the 9/21 baseline's counts while the tree
    GREW, so a tree that grew to 150 could slide 150 -> 121 -> 97 (35%) in CLEAN
    four-week steps: each step passed the moving window AND 80% of the baseline's
    100. The anchor now rises to the largest clean walk since it, so the 97 fails."""
    full = [frow(f"02-F3-Energy\\g\\f{i:03d}.pdf") for i in range(150)]
    write_stamp(world.outdir, rh.BASELINE_STAMP, full[:100], BASE_DIRS, root=world.root, max_hash=200)
    s = _weekly(9)
    for st in s[:4]:                                  # the tree grows to 150
        fake_ps(monkeypatch, world, stamp=st, files=full[:150])
        assert rh.main(["--apply"]) == rh.EXIT_OK
    for st in s[4:8]:                                 # 121 >= 80% of 150: CLEAN
        fake_ps(monkeypatch, world, stamp=st, files=full[:121])
        assert rh.main(["--apply"]) == rh.EXIT_OK
    led = rh.ledger_state(rh.read_ledger(world.ledger))
    runs = rh.eligible_runs(world.outdir, s[8], world.root, led)
    assert rh.floor_anchor(runs, led, s[8])["files_total"][0] == 150
    fake_ps(monkeypatch, world, stamp=s[8], files=full[:97])   # >= 80% of the window's 121
    assert rh.main(["--apply"]) == rh.EXIT_INCOMPLETE
    md = world.report(_day(s[8])).read_text(encoding="utf-8")
    assert "files walked 97 < 80% of the largest clean run's since the anchor 150" in md


def test_r4_a_lead_glued_to_a_neighbouring_word_and_a_mid_word_code_are_counts_only():
    """D-051 r4: a LEX lead glued to another word ("<First><Last>Notes") was matched
    only on the raw segment, and "TheLEXreport" never split around the code. Both
    are now counts-only; ordinary words that merely contain the letters stay listed.
    Failures report indexes only (no names printed)."""
    from cora import org_roles
    names = sorted({r.name for r in org_roles.all_roles() if str(r.entity).upper().startswith("LEX")})
    assert len(names) >= 1
    missed = []
    for i, name in enumerate(names):
        first, last = name.split()[0], name.split()[-1]
        for j, seg in enumerate((f"{first}{last}Notes", f"Notes{first}{last}", f"{first[0]}{last}Resume")):
            if not hdr.is_lex_relpath(f"01-HJR-Global\\hr\\{seg} (1).pdf"):
                missed.append((i, j))
    assert missed == []
    assert hdr.is_lex_relpath("01-HJR-Global\\accounting\\TheLEXreport (1).xlsx")
    for ordinary in ("LEXUS", "Lexicon", "FLEXreport", "LLAMA", "AlexCordova", "LexisNexis"):
        assert not hdr.is_lex_relpath(f"01-HJR-Global\\misc\\{ordinary} (1).pdf"), ordinary
